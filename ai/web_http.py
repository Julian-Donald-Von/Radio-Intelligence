#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 2026-11-17：**纯 HTTPS 搜索引擎通道**（360 / 搜狗）—— 不起浏览器、不靠 Bing。

## 为什么要有这个模块（真机取证，本机 2026-11-17）

同一台机、同一时刻、同一个 query，直接看各家返回：

| query | Bing（HTML 与 RSS 都一样） | 360 | 搜狗 |
|---|---|---|---|
| `水星记 郭顶` | 水星网络（路由器）/ 水星（行星）——**只剩"水星"** | 水星记_360百科 / 郭顶《水星记》 | 水星记_QQ音乐 / 搜狗百科 |
| `電光石火 ハルカトミユキ` | MOLEX 连接器 / 大华技术 | 電光石火_歌曲 / 电光石火(词语) | —— |
| `Landslide Fleetwood Mac` | ✅ 正确（网易云/知乎/Apple） | —— | —— |

⇒ Bing 对**中日文**查询会静默改写/丢词（单 CJK 词 `水星记` 反而正确、加到两个词就崩），
   而 360 / 搜狗 全程正常，且都是 0.5~0.9s 的纯 HTTP。旧 `chain` 把 Bing 放第一顺位，
   于是"第一个非空就采纳"⇒ 垃圾胜出 ⇒ 客户端白名单只能丢弃 ⇒ 口播零事实。

## 合并打分：照抄 SearXNG 的做法（`searx/results.py`，2026-11 上游 master）

    weight = Π(engine_weight) × len(positions)
    score  = Σ (weight / position)

即：**同一 URL 被越多引擎收录、且名次越靠前，分越高**；重复项合并时取
"标题/摘要更长的那一份"（上游 `merge_two_main_results` 的规则）。
这样单引擎的偶然噪声压不过多引擎共识，也不必再赌"哪家今天没被限流"。

## 不做的两件事（有证据的取舍）

- **百度**：`div.result` 只解析出 3 条，且 URL 全是 `baidu.com/link?url=…` 跳转壳
  （真实域名被藏起来，白名单/相关闸全部失效），摘要为空 ⇒ 不进池子。
- **DuckDuckGo**：本机 `html.duckduckgo.com` 30s 超时（不可达）⇒ 不引入。
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request

from lxml import html as _LH

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 显式绕过系统代理（本机 http_proxy 会把 127.0.0.1 与公网一起代理掉，B109 已踩过）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 引擎权重：与 SearXNG 的 `engine.weight` 同义。360 与搜狗中文最稳，bing_rss 只对拉丁文可靠。
ENGINE_WEIGHTS = {"so360": 1.0, "sogou": 0.9, "bing_rss": 0.8}


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read()


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.text_content() or "").strip() if el is not None else ""


def _first_text(item, selectors) -> str:
    for sel in selectors:
        try:
            got = item.cssselect(sel)
        except Exception:                                   # noqa 选择器不支持 → 跳过
            continue
        if got:
            t = _txt(got[0])
            if t:
                return t
    return ""


def _collect(doc, item_sel: str, sn_sels) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    try:
        items = doc.cssselect(item_sel)
    except Exception:                                       # noqa
        return out
    for it in items:
        a = it.cssselect("h3 a") or it.cssselect("a")
        if not a:
            continue
        title = _txt(a[0])
        url = (a[0].get("data-mdurl") or a[0].get("href") or "").strip()
        if not title or not url.startswith("http"):
            continue
        out.append((title, url, _first_text(it, sn_sels)))
    return out


def search_360(query: str, timeout: float = 10.0) -> list[tuple[str, str, str]]:
    """360 搜索（`so.com`）：自然结果 `li.res-list`，广告在 `res-ad` / 标题含"推广"。"""
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
    try:
        doc = _LH.fromstring(_get(url, timeout))
    except Exception:                                       # noqa 网络/解析失败 → 空（降级由调用方处理）
        return []
    out = []
    for title, u, sn in _collect(doc, "li.res-list", (".res-desc", ".res-list-summary")):
        if "推广" in title or "广告" in title:
            continue
        out.append((title, u, sn))
    return out


def search_sogou(query: str, timeout: float = 10.0) -> list[tuple[str, str, str]]:
    """搜狗搜索：结果块 `div.vrwrap`（`h3 a` + `.str-text-info`）。

    搜狗的摘要质量在本机实测最好（自带"歌手/专辑/发行时间"字段），且不支持
    `div.rb` 那种更外侧容器（实测 0 条）——只认 `vrwrap`。
    """
    url = "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": query})
    try:
        doc = _LH.fromstring(_get(url, timeout))
    except Exception:                                       # noqa
        return []
    return _collect(doc, "div.vrwrap",
                    (".str-text-info", ".str_info", ".text-layout", ".space-txt", ".fz-mid"))


# ── URL 归一（去重键）：schema/www/默认端口/尾斜杠/片段/跟踪参数 ──────────────
_TRACK_PARAMS = ("utm_", "spm", "from", "src", "ref", "share", "fr", "ivk_sa")


def norm_url(u: str) -> str:
    try:
        p = urllib.parse.urlsplit((u or "").strip())
    except Exception:                                       # noqa
        return (u or "").strip().lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", p.path or "")
    keep = []
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False):
        if any(k.lower().startswith(t) or k.lower() == t for t in _TRACK_PARAMS):
            continue
        keep.append((k, v))
    q = urllib.parse.urlencode(keep) if keep else ""
    return host + path + (("?" + q) if q else "")


def merge_results(per_engine: dict, top: int = 10) -> list[dict]:
    """多引擎结果合并 + 打分（SearXNG 口径，见模块头）。

    `per_engine`: `{engine_name: [(title, url, snippet), ...]}`（**已按名次排好**）。
    返回 `[{title,url,snippet,engines:[...],positions:[...],score}]`，按 score 降序。
    """
    bucket: dict[str, dict] = {}
    for eng, rows in per_engine.items():
        w = float(ENGINE_WEIGHTS.get(eng, 1.0))
        for pos, row in enumerate(rows, 1):
            # ⚠ 两种形态都要吃：本模块自检给的是 `(title, url, snippet)` 三元组，
            #   而搜索服务侧传的是 `_clean()` 出来的 dict。首版只认三元组，
            #   结果把 dict **按键名解包**成 ("title","url","snippet") ⇒ 全部塌成 1 条
            #   （真机实测：n=1，标题字面就是 "title"）。
            if isinstance(row, dict):
                title, url, sn = (row.get("title") or "", row.get("url") or "",
                                  row.get("snippet") or "")
            else:
                title, url, sn = row
            if not url:
                continue
            key = norm_url(url)
            cur = bucket.get(key)
            if cur is None:
                bucket[key] = {"title": title, "url": url, "snippet": sn,
                               "engines": [eng], "positions": [pos], "w": w}
            else:
                # 上游 `merge_two_main_results`：重复项取**更长**的标题/摘要
                if len(title) > len(cur["title"]):
                    cur["title"] = title
                if len(sn or "") > len(cur["snippet"] or ""):
                    cur["snippet"] = sn
                cur["engines"].append(eng)
                cur["positions"].append(pos)
                cur["w"] = float(cur["w"]) * w
    out = []
    for cur in bucket.values():
        weight = float(cur["w"]) * len(cur["positions"])
        score = sum(weight / float(p) for p in cur["positions"])
        cur["score"] = score
        out.append(cur)
    out.sort(key=lambda x: -x["score"])
    return out[:top]


if __name__ == "__main__":                                  # 手动自检
    import sys
    import time
    for _q in (sys.argv[1:] or ["水星记 郭顶", "Landslide Fleetwood Mac"]):
        _t0 = time.monotonic()
        _r = {"so360": search_360(_q), "sogou": search_sogou(_q)}
        _m = merge_results(_r)
        print("=== %s  %.2fs  360=%d 搜狗=%d  合并后=%d"
              % (_q, time.monotonic() - _t0, len(_r["so360"]), len(_r["sogou"]), len(_m)))
        for _x in _m[:4]:
            print("   %-46s %s  score=%.2f" % (_x["title"][:46], ",".join(_x["engines"]),
                                               _x["score"]))
