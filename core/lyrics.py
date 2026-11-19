"""
歌词：LRC 解析 + 取词。

LRC 形如：
  [00:12.34] 某一句歌词
  [00:18.00][00:50.00] 重复句

支持单句多时间戳、乱序时间标签、无时间标签的纯文本行（按出现顺序排）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("MusicAI.Lyrics")

_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def coerce_lrc_text(v) -> str | None:
    """把音源 lyric() 的多种返回形态归一成 LRC 文本（str）或 None。

    ⚠ 这是**业务层**公共函数（core/lyrics.py），供 ai/playback.py（播放时取词）与
    ui/bridge_host.py（前端主动取词）**共用** —— 两处都必须归一，只修一处会漏
    （历史 bug：仅 bridge_host 归一，播放时 playback.py 仍抛
    "'dict' object has no attribute 'splitlines'"）。

    各音源/后端返回形态并不统一：
      - str            ：正常 LRC 文本（netease musicbox 逐行 join、HTTP 后端 lrc.lyric）
      - dict           ：如 {'lyric': '...'} / {'lrc': {...}} / {'data': {...}}（QQ 常见）
      - list[str|dict] ：musicbox 逐行歌词（dict 行取 lineLyric/lyric/text）
      - None           ：无歌词
    """
    for _ in range(5):
        if v is None:
            return None
        if isinstance(v, str):
            return v or None
        if isinstance(v, dict):
            hit = None
            for k in ("lyric", "lrc", "lineLyric", "text", "content",
                      "klyric", "tlyric", "data"):
                if v.get(k):
                    hit = v[k]
                    break
            if hit is None:
                vals = [x for x in v.values() if x]
                if not vals:
                    return None
                hit = vals[0]
            v = hit
            continue
        if isinstance(v, (list, tuple)):
            parts = []
            for item in v:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    s = (item.get("lineLyric") or item.get("lyric")
                         or item.get("text") or item.get("content"))
                    if s:
                        parts.append(str(s))
            return "\n".join(parts) if parts else None
        return str(v) or None
    return None


def parse_lrc(text: str | None) -> list[tuple[float, str]]:
    """返回 [(秒, 歌词行), ...] 按时间升序。"""
    if not text:
        return []
    out: list[tuple[float, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        stamps = _TIME_RE.findall(line)
        content = _TIME_RE.sub("", line).strip()
        if not content:
            continue
        if not stamps:
            # 没有时间戳的纯文本（如歌名/作者），给一个很大的时间让它排最后
            out.append((1e9, content))
            continue
        for mm, ss, ms in stamps:
            mm = int(mm)
            ss = int(ss)
            ms = int((ms + "0" * (3 - len(ms)))[:3]) if ms else 0
            sec = mm * 60 + ss + ms / 1000.0
            out.append((sec, content))
    out.sort(key=lambda x: x[0])
    return out


def line_index_at(lines: list[tuple[float, str]], ms: int) -> int:
    """给定播放进度(ms)，返回当前应高亮的行索引；无匹配返回 -1。"""
    if not lines:
        return -1
    sec = ms / 1000.0
    idx = -1
    for i, (t, _) in enumerate(lines):
        if t > 1e8:  # 无时间戳的尾部文本不计入
            break
        if sec >= t:
            idx = i
        else:
            break
    return idx


def find_local_lrc(audio_path: str) -> str | None:
    """尝试找与音频同名的 .lrc 文件。"""
    p = Path(audio_path)
    cand = p.with_suffix(".lrc")
    if cand.exists():
        try:
            return cand.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa
            return None
    return None
