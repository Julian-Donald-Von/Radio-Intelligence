# -*- coding: utf-8 -*-
r"""口播字数度量：按产品口径数「字数」。

口径（2026-11-17 用户拍板）：
  导听口播 **50~70 字**，且
    · **不含歌名、歌手名**（点名不算字数，也不该靠名字凑数）；
    · **标点不计**；
    · 英文一个单词算一个字；`<ja>…</ja>` 这类语言标签不计。

为什么单独成模块：之前"字数"在提示词里有 45~70 / 45~60 两个口径，代码里又只按
`len(display)` 记原始长度（把歌名、标点全算进去），于是"看起来 92 字、实际正文 60 字"
这种误判没法纠正。
"""
from __future__ import annotations

import re

_TAG = re.compile(r"<[a-zA-Z]{2,3}>|</[a-zA-Z]{2,3}>")
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_WORD = re.compile(r"[A-Za-z0-9]+")
# 标点：中英文标点、空白、引号、破折号等
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


def core_len(text: str, title: str = "", artist: str = "") -> int:
    """按"字号"口径数字数：去掉语言标签、歌名、歌手名、标点后的 CJK 字 + 英文词。"""
    t = _TAG.sub("", text or "")
    for name in _names(title, artist):
        if name:
            t = t.replace(name, "")
    t = _PUNCT.sub(" ", t)
    return len(_CJK.findall(t)) + len(_WORD.findall(t))


def _names(title: str, artist: str) -> list[str]:
    out = []
    for raw in (title, artist):
        s = (raw or "").strip()
        if not s:
            continue
        out.append(s)
        for sep in (",", "，", "&", "、", "/", "feat.", "ft."):
            if sep in s:
                out.extend(x.strip() for x in s.split(sep) if x.strip())
    # 长的先替换，避免"歌手A"被"歌手"截断
    return sorted({n for n in out if len(n) >= 2}, key=len, reverse=True)


def in_band(text: str, title: str = "", artist: str = "",
            lo: int = 50, hi: int = 70) -> tuple[int, bool]:
    n = core_len(text, title, artist)
    return n, (lo <= n <= hi)
