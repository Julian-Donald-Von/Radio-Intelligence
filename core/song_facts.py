# -*- coding: utf-8 -*-
r"""歌曲客观事实（本地，零网络）：给转场/口播判断"反差"提供硬数据。

为什么需要（2026-11-17 实测）：
  · 库里 `genre` 0/427，`_song_info()` 又把 mood/bpm 写死为空；
  · 于是转场提示词里"按流派、年代、情绪、BPM 判断"全部落空，
    模型只能靠记忆猜，实测会编出"BPM 从慢速到快速""两首均为 80 年代"。

本模块只做三件事：
  1. 从 `tracks.api_metadata.release_date` / `tracks.year` 取**发行年**；
  2. 从 `track_features`（audio_features g3.v1）取 **bpm / 能量 / 亮度 / 人声占比**；
  3. 把两首歌的这些数字算成一句**客观对比**，直接塞进转场 user 消息。

设计约束：
  · 纯本地、无网络、失败静默（取不到就留空，绝不阻断转场）；
  · `BPM` 与`亮度`按**倍频折叠**（检测器常把 65 判成 130），避免假反差；
  · 结果按 (title, artist) 进程内缓存，避免每首歌重复查库。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data_runtime" / "musicai.db"
EXTRA_PATH = Path(__file__).resolve().parent.parent / "data_runtime" / "song_facts.json"

_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str], dict] = {}
_MAX_CACHE = 512
_EXTRA: dict = {}
_EXTRA_MTIME = 0.0


def _load_extra() -> dict:
    """读 `song_facts.json`（由 tools/backfill_facts.py 用本机 QQ API 回填）。按 mtime 失效。"""
    global _EXTRA, _EXTRA_MTIME
    try:
        m = EXTRA_PATH.stat().st_mtime
    except Exception:
        return _EXTRA
    if m != _EXTRA_MTIME:
        try:
            _EXTRA = json.loads(EXTRA_PATH.read_text(encoding="utf-8")) or {}
            _EXTRA_MTIME = m
        except Exception:
            _EXTRA = {}
    return _EXTRA

# 年份差 / BPM 倍率 / 能量差 / 人声差 —— 超过任一条就算"反差大"
YEAR_GAP_STRONG = 20
BPM_RATIO_STRONG = 1.30
ENERGY_GAP_STRONG = 6.0


def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", (s or "").strip().lower())


def _fold_ratio(a: float, b: float) -> float:
    """把两个正整数倍率折叠到 [1, 2]：65 与 130 视为同一速度，比值 1.0。"""
    try:
        lo, hi = sorted((float(a), float(b)))
        if lo <= 0:
            return 1.0
        r = hi / lo
        while r > 2.0:
            r /= 2.0
        while r < 1.0:
            r *= 2.0
        return r
    except Exception:
        return 1.0


def _parse_year(meta: str | None) -> int | None:
    try:
        d = json.loads(meta or "{}")
    except Exception:
        return None
    for key in ("release_date", "album_date"):
        v = d.get(key)
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, str):
            m = re.search(r"(19|20)\d{2}", v)
            if m:
                return int(m.group(0))
    return None


def _lang_of(title: str) -> str:
    t = title or ""
    if re.search(r"[\u3040-\u30ff]", t):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", t):
        return "ko"
    if re.search(r"[\u4e00-\u9fff]", t):
        return "zh"
    return "latin"


def _lang_pair(a: str, b: str) -> tuple[str, str] | None:
    """返回两侧语种标签；**纯汉字/纯汉字这一对不可辨**（zh 与日文汉字同形）⇒ 返回 None。"""
    ta, tb = a or "", b or ""
    cjk_a = bool(re.search(r"[\u4e00-\u9fff]", ta))
    cjk_b = bool(re.search(r"[\u4e00-\u9fff]", tb))
    if cjk_a and cjk_b:
        # 只有一侧出现假名/谚文才敢判；两侧都是纯汉字 ⇒ 不判语种
        if not (re.search(r"[\u3040-\u30ff\uac00-\ud7af]", ta)
                or re.search(r"[\u3040-\u30ff\uac00-\ud7af]", tb)):
            return None
    return _lang_of(a), _lang_of(b)


_LANG_CN = {"zh": "中文", "ja": "日文", "ko": "韩文", "latin": "拉丁字母"}


def facts(title: str, artist: str = "") -> dict:
    """取一首歌的客观事实；取不到的字段留空。带进程内缓存。"""
    key = (_norm(title), _norm(artist))
    with _LOCK:
        if key in _CACHE:
            return dict(_CACHE[key])
    out: dict = {"title": title or "", "artist": artist or "",
                 "year": None, "bpm": None, "energy_db": None,
                 "bright_hz": None, "voiced": None, "lang": _lang_of(title),
                 "album": "", "year_src": ""}
    extra = _load_extra().get(f"{_norm(title)}|{_norm(artist)}")
    if extra and extra.get("year"):
        out["year"] = int(extra["year"])
        out["album"] = extra.get("album") or ""
        out["year_src"] = extra.get("source") or "extra"
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "select id, year, api_metadata from tracks "
                "where lower(title)=lower(?) order by ifnull(api_metadata,'')<>'' desc limit 1",
                (title,)).fetchone()
            if row is None and title:
                row = conn.execute(
                    "select id, year, api_metadata from tracks "
                    "where title like ? order by length(title) asc limit 1",
                    (f"%{title}%",)).fetchone()
            if row:
                tid, year, meta = row
                _db_year = _parse_year(meta) or (int(year) if year else None)
                if _db_year:
                    out["year"] = _db_year
                    out["year_src"] = "db"
                f = conn.execute(
                    "select data from track_features where track_id=? "
                    "order by rowid desc limit 1", (tid,)).fetchone()
                if f:
                    d = json.loads(f[0] or "{}")
                    raw = d.get("raw") or {}
                    out["bpm"] = (raw.get("rhythm") or {}).get("bpm")
                    out["energy_db"] = (raw.get("dynamics") or {}).get("rms_dbfs")
                    out["bright_hz"] = (raw.get("tone") or {}).get("centroid_hz")
                    out["voiced"] = (raw.get("melody") or {}).get("voiced")
        finally:
            conn.close()
    except Exception:
        pass
    with _LOCK:
        if len(_CACHE) >= _MAX_CACHE:
            _CACHE.clear()
        _CACHE[key] = dict(out)
    return out


def contrast(a: dict, b: dict) -> dict:
    """两首歌的客观反差。`a` 当前曲、`b` 下一首（都用 facts() 的结果）。"""
    res = {"year_gap": None, "bpm_ratio": None, "energy_gap": None,
           "lang_change": False, "strong": False, "reasons": []}
    pair = _lang_pair(a.get("title", ""), b.get("title", ""))
    if pair and pair[0] != pair[1]:
        res["lang_change"] = True
    if a.get("year") and b.get("year"):
        gap = abs(int(a["year"]) - int(b["year"]))
        res["year_gap"] = gap
        if gap >= YEAR_GAP_STRONG:
            res["strong"] = True
            res["reasons"].append(f"年代差 {gap} 年")
    if a.get("bpm") and b.get("bpm"):
        r = _fold_ratio(a["bpm"], b["bpm"])
        res["bpm_ratio"] = round(r, 2)
        if r >= BPM_RATIO_STRONG:
            res["strong"] = True
            res["reasons"].append(f"速度差 {r:.2f} 倍")
    if a.get("energy_db") is not None and b.get("energy_db") is not None:
        g = abs(float(a["energy_db"]) - float(b["energy_db"]))
        res["energy_gap"] = round(g, 1)
        if g >= ENERGY_GAP_STRONG:
            res["strong"] = True
            res["reasons"].append(f"响度差 {g:.1f} dB")
    if res["lang_change"]:
        res["strong"] = True
        res["reasons"].append(f"语种 {_LANG_CN.get(a.get('lang'),'?')}→{_LANG_CN.get(b.get('lang'),'?')}")
    return res


def contrast_line(a: dict, b: dict) -> str:
    """给模型看的一行客观数据；一个字段都没有时返回空串。"""
    got = False
    parts = []
    if a.get("year") and b.get("year"):
        parts.append(f"发行年 {a['year']}→{b['year']}")
        got = True
    if a.get("bpm") and b.get("bpm"):
        parts.append(f"BPM {round(a['bpm'])}→{round(b['bpm'])}")
        got = True
    if a.get("energy_db") is not None and b.get("energy_db") is not None:
        parts.append(f"响度 {a['energy_db']:.1f}→{b['energy_db']:.1f} dBFS")
        got = True
    if a.get("lang") != b.get("lang"):
        pair = _lang_pair(a.get("title", ""), b.get("title", ""))
        if pair:
            parts.append(f"语种 {_LANG_CN.get(pair[0],'?')}→{_LANG_CN.get(pair[1],'?')}")
            got = True
    if not got:
        return ""
    c = contrast(a, b)
    tail = ("；判定：反差大（" + "、".join(c["reasons"]) + "）") if c["strong"] else "；判定：接得上"
    return "【客观数据】" + "，".join(parts) + tail
