#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 2026-09-21：**Bing RSS 检索通道** —— 纯 HTTPS + XML，**不起浏览器**。

为什么单开这一条（真机实测，本机直连 `www.bing.com`）：
  · 连发 10 个不同查询 **零限流**；每次 **0.31~0.35 s**；返回 9~10 条；
  · 摘要质量**比无头浏览器抓 HTML 更好**（百度百科那条自带"创作背景 / 收录于"）；
  · 而原浏览器链路：`bing` 1.4~5.2 s，且实测出现过「**所有查询都在 ~5.8 s 返回 0 条**」
    的整段限流（Bing 对无头浏览器的风控）—— 这正是"检索太懒"的生产现场：
    服务一慢 → 客户端 6 s 预算耗尽 → 0 条 → 连续 2 次 → **熔断 120 s** → 口播连着
    好几首零事实。

⇒ 定位：**第一顺位通道**。浏览器链路降为兜底；两者都失败才认输。

本模块**只用标准库**（urllib / xml.etree / html / re）⇒ 搜索服务进程与电台进程
都能直接 import，不引入新依赖、不触发重型模块加载。

用法：
    from ai.bing_rss import search_rss
    rows = search_rss("晴天 周杰伦")       # -> [(title, url, snippet), ...]
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse
import urllib.request
from xml.etree import ElementTree as _ET

RSS_BASE = "https://www.bing.com/search"

# ★ 真实浏览器 UA：`format=rss` 端点对 UA 不敏感，但空 UA 会被拒。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ★ 显式绕过系统代理：本机 `http_proxy` 会把 127.0.0.1 与公网一起代理掉
#   （本项目 B109 已踩过同款坑），这里强制直连。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ⚠ `count` 参数实测**无效**（10/20/30/50 都只给 9~10 条）⇒ 不传，省得误导。
_TAGS = ("title", "link", "description")


def _strip(s: str) -> str:
    """去标签 + 反转义 + 压空白（RSS 的 description 里常带 `<b>` 等强调标签）。"""
    s = _html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _parse_xml(body: bytes) -> list[tuple[str, str, str]]:
    """`xml.etree` 解析（首选）。命名空间用「取本地名」的方式兼容。"""
    out: list[tuple[str, str, str]] = []
    try:
        root = _ET.fromstring(body)
    except Exception:                                     # noqa 畸形 XML → 交正则兜底
        return []
    for it in root.iter():
        if (it.tag or "").split("}")[-1] != "item":
            continue
        d = {}
        for ch in it:
            d[(ch.tag or "").split("}")[-1]] = ch.text or ""
        t, u, s = _strip(d.get("title", "")), (d.get("link") or "").strip(), \
            _strip(d.get("description", ""))
        if t and u:
            out.append((t, u, s))
    return out


def _parse_regex(x: str) -> list[tuple[str, str, str]]:
    """正则兜底（XML 有非法字符时 `etree` 会整份失败，那就不该连带丢掉结果）。"""
    out: list[tuple[str, str, str]] = []
    for it in re.findall(r"<item[^>]*>(.*?)</item>", x, re.S):
        def g(tag: str) -> str:
            m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), it, re.S)
            return m.group(1) if m else ""
        t, u = _strip(g("title")), _strip(g("link"))
        if t and u:
            out.append((t, u, _strip(g("description"))))
    return out


def search_rss(query: str, timeout: float = 12.0, mkt: str = "zh-CN") -> list[tuple[str, str, str]]:
    """查一次 Bing RSS，返回 `[(title, url, snippet), ...]`；任何失败给 `[]`。

    **不抛异常**：这条通道是"降级链里的第一跳"，失败必须让调用方平滑换下一跳。

    ★ R119-26：`mkt` 市场参数。默认 `zh-CN`；实测 20 首冷门歌里 7 首外文歌
      （Holocene / Skinny Love / The Night We Met / Kyoto / Apocalypse /
      罪と罰 / 茎）在**中文市场下一律 0 条** —— 中文库里根本没有条目。
      换 `mkt="en-US"` 后能拿到英文维基等权威条目。
    """
    q = (query or "").strip()
    if not q:
        return []
    url = RSS_BASE + "?" + urllib.parse.urlencode(
        {"q": q, "format": "rss", "mkt": mkt})
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/xml,text/xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        })
        with _OPENER.open(req, timeout=timeout) as r:
            body = r.read()
    except Exception:                                     # noqa 网络/超时/风控 → 空
        return []
    rows = _parse_xml(body)
    if not rows:                                          # etree 失败/零条 → 正则再试
        rows = _parse_regex(body.decode("utf-8", "replace"))
    return rows


if __name__ == "__main__":                                # 手动自检
    import sys
    import time
    for _q in (sys.argv[1:] or ["晴天 周杰伦", "Lemon 米津玄師"]):
        t0 = time.monotonic()
        _rs = search_rss(_q)
        print("=== %s  %.2fs  %d 条" % (_q, time.monotonic() - t0, len(_rs)))
        for _t, _u, _s in _rs[:4]:
            print("   %-42s | %s" % (_t[:42], _s[:64]))
