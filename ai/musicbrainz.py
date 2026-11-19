#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ R119-27 / P2'：MusicBrainz 检索兜底 —— **免代理可达**的英文音乐结构化源。

为什么是它（真机实测，本机直连 musicbrainz.org）：
  · 0.75s 返回结构化 JSON（艺人 / 专辑 / 发行年 / 时长 / 注释），是口播事实的干净来源；
  · 无代理时 Wikipedia / DuckDuckGo 物理不可达，Bing RSS 的 `mkt=en-US` 又失效
    （返回中文义项页被白名单闸丢弃）→ 外文歌零资料。MusicBrainz 是唯一可达的兜底；
  · 同样覆盖 Bing RSS 本身没条目的中文冷门歌（如「九月」「这一切没有想象的那么糟」）。

合规（MusicBrainz 明确要求带可联系 UA + 限速 ≈1 req/s，无 key）：
  · 模块级节流 `_MIN_INTERVAL`；
  · 绝不抛异常 —— 兜底链最后一跳，任何失败必须平滑让路给「陈旧缓存 / 空串」。

用法：
    from ai.musicbrainz import search_recording, format_facts
    rec = search_recording("Holocene", "Bon Iver")   # -> dict 或 None
    txt = format_facts(rec)                          # -> 口播用短文本 或 ""
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

MB_BASE = "https://musicbrainz.org/ws/2"

# ★ MusicBrainz 要求 UA 含可联系信息（否则 403）。用项目公开身份邮箱。
UA = ("MusicAI-Radio/1.0 (local AI radio; contact: fengdl24@mails.jlu.edu.cn)")

# ★ 显式绕过系统代理：与 bing_rss 同款坑（B109），避免 127.0.0.1/公网一起被代理掉。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ★ 限速 ≈1 req/s（无 key 的礼貌值）。兜底调用频率低，睡眠成本可忽略。
_MIN_INTERVAL = 1.0
_LAST_CALL = [0.0]


def _throttle() -> None:
    _gap = _MIN_INTERVAL - (time.monotonic() - _LAST_CALL[0])
    if _gap > 0:
        time.sleep(_gap)
    _LAST_CALL[0] = time.monotonic()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _year_of(date: str) -> str:
    m = re.search(r"(?:19|20)\d{2}", date or "")
    return m.group(0) if m else ""


def _fmt_length(ms: int) -> str:
    if not ms:
        return ""
    return "%d:%02d" % (ms // 60000, (ms // 1000) % 60)


def search_recording(title: str, artist: str = "", timeout: float = 6.0, album: str = '') -> dict | None:
    """查一首歌的结构化事实。

    返回 `{"title","artist","album","year","length","disambiguation"}` 或 `None`。
    **不抛异常**：网络/超时/解析失败一律 `None`，让调用方走下一兜底。
    """
    t = (title or "").strip()
    if not t:
        return None
    q = 'recording:"%s"' % t.replace('"', "")
    if artist.strip():
        q += ' AND artist:"%s"' % artist.strip().replace('"', "")
    url = MB_BASE + "/recording/?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": 5})
    # ★ 单次重试吸收瞬时限流/超时：兜底链路偶发 503/超时不应让整首歌变零资料。
    data = None
    for _attempt in range(2):
        try:
            _throttle()
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            with _OPENER.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except Exception:                                 # noqa 任何失败 → 重试一次
            time.sleep(0.4)
    if data is None:
        return None
    recs = data.get("recordings") or []
    if not recs:
        return None
    # ★ 优先取「艺人归一化匹配」的条目 —— 防错歌注入（Lemon Tree→米津玄师 那类）。
    from ai.material_guard import contains_entity, primary_artist
    na = primary_artist(artist)
    picked = None
    for rec in sorted(recs, key=lambda r: r.get('first-release-date') or '9999'):
        arts = " ".join(a.get("name", "") for a in (rec.get("artist-credit") or []))
        if _norm(rec.get('title', '')) != _norm(t):
            continue
        if album and not any(_norm(r.get('title')) == _norm(album) for r in rec.get('releases', [])):
            continue
        if not na or contains_entity(arts, na):
            picked = rec
            break
    if picked is None:
        return None
    arts = " ".join(a.get("name", "") for a in (picked.get("artist-credit") or []))
    requested_album = album
    album = ""
    year = ""
    # ★ 优先取「非现场/非合辑」的正式发行作为代表专辑：MusicBrainz 的 releases 里
    #   常混着 live/bootleg/拼接合辑（标题带 "YYYY-MM-DD:" 或 disambiguation=live），
    #   那些当口播「专辑」会误导。挑不到再退而用首条。
    _rels = picked.get("releases") or []
    _fallback = None
    for rel in _rels:
        _rt = rel.get("title", "") or ""
        if requested_album and _norm(_rt) != _norm(requested_album):
            continue
        _tag = (_rt + " " + (rel.get("disambiguation", "") or "")).lower()
        if not _rt:
            continue
        if _fallback is None:
            _fallback = rel
        if ":" in _rt or "live" in _tag or "bootleg" in _tag or "compilation" in _tag:
            continue
        album = _rt
        year = _year_of(rel.get("date", "") or "")
        break
    if not album and _fallback is not None:
        album = _fallback.get("title", "") or ""
        year = _year_of(_fallback.get("date", "") or "")
    return {
        "title": picked.get("title", t),
        "artist": arts,
        "album": album,
        "year": year,
        "first_release_year": _year_of(picked.get('first-release-date', '')),
        "length": _fmt_length(int(picked.get("length") or 0)),
        "disambiguation": picked.get("disambiguation", "") or "",
    }


def format_facts(rec: dict | None) -> str:
    """把结构化事实拼成口播用短文本（格式与 curate_material 注入一致：`[1] 标题\\n    摘要`）。
    返回 "" 表示无料。
    """
    if not rec:
        return ""
    title = rec.get("title") or ""
    artist = rec.get("artist") or ""
    if not title:
        return ""
    line = "[1] %s" % title
    if artist:
        line += "\n    《%s》由 %s" % (title, artist)
    album = rec.get("album") or ""
    disambig = rec.get("disambiguation") or ""
    # ★ 现场/合辑的标题（带 "YYYY-MM-DD:" 或标注 live/bootleg）不适合口播，
    #   丢弃专辑名、保留年份与时长（仍属这首歌的事实）。
    _noisy = (":" in album) or any(
        k in disambig.lower() for k in ("live", "bootleg", "compilation"))
    if _noisy:
        album = ""
    extra = []
    if album:
        extra.append("收录于专辑《%s》" % album)
    if rec.get('first_release_year'):
        extra.append("录音首次发行年份 %s" % rec['first_release_year'])
    elif rec.get("year") and album:
        extra.append("所列专辑版本发行年份 %s（非歌曲首发依据）" % rec["year"])
    if rec.get("length"):
        extra.append("时长 %s" % rec["length"])
    if disambig and not _noisy:
        extra.append("（%s）" % disambig)
    line += ("，" + "，".join(extra) + "。") if extra else "。"
    return line


if __name__ == "__main__":                                # 手动自检
    import sys
    for _t, _a in (("Holocene", "Bon Iver"), ("Kyoto", "Phoebe Bridgers"),
                   ("九月", "周云蓬"), ("Everything", "Daft Punk")):
        _r = search_recording(_t, _a)
        print("=== %s / %s" % (_t, _a))
        print("   ", format_facts(_r) or "（无料）")
