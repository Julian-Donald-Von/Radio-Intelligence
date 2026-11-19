# -*- coding: utf-8 -*-
"""R119-16 · **正文回捞客户端**（`GET /fetch` 的使用方 + 落盘缓存 + 句子级抽取）。

为什么值得为它慢几秒：RSS 摘要只有 **107~124 字**，而"创作背景 / 创作历程"全在词条正文里。
实测（真机，真 Chrome）回捞后：晴天 3079 字 / Lemon 3849 / 起风了 1857 /
Bohemian Rhapsody 4835（事实词 9~16 个）—— **厚 15~40 倍**，而且拿到的正是缺的那块。

**放在哪条路径上（关键）**：
  · 预热路径（`prewarm_upcoming`，正在放这首歌时给后面 n 首填缓存）→ `allow_live=True`，
    一次 5~6 s 完全可接受；
  · 口播关键路径（`format_context`）→ 只读缓存（`allow_live=False`），**绝不发网络**。
    理由：R119-9/R119-13 那条"每做一次就卡一次"就是这么来的。

实测（`probe_fetch_endpoint`）：词条正文 4.9~6.4 s/页；服务侧 24 h TTL；
本模块再叠一层 30 天落盘缓存（词条正文几乎不变）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ai import result_kind as RK
from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

PAGE_FETCH_PATH = DATA_DIR / "page_cache.json"
PAGE_CACHE_TTL_S = float(os.environ.get("MUSICAI_PAGE_CACHE_TTL_S") or 30 * 24 * 3600)
PAGE_CACHE_MAX = int(os.environ.get("MUSICAI_PAGE_CACHE_MAX") or 300)
SVC_TIMEOUT_S = float(os.environ.get("MUSICAI_PAGE_FETCH_TIMEOUT") or 60.0)
PAGE_BUDGET_CHARS = int(os.environ.get("MUSICAI_PAGE_BUDGET") or 900)
# 单曲最多回捞几页（1 页百科 ≈ 3000 字，够用；2 页用于"条目 + 平台"互补）
PAGE_N = int(os.environ.get("MUSICAI_PAGE_FETCH_N") or 2)

_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_C_LOCK = threading.Lock()
_C: dict | None = None
_C_DIRTY = [0]

# 事实词 / 句子打分（与 `ai/search._FACT_WORDS` 同源口径，但这里只关心"创作背景"）
_FACT_WORDS = ("收录于", "发行", "专辑", "单曲", "作词", "作曲", "编曲", "词曲", "主题曲",
               "插曲", "片尾曲", "片头曲", "演唱", "创作", "主打", "获得", "获奖",
               "销量", "制作", "原唱", "出道", "乐队", "歌手", "制作人", "改编", "翻唱")
_BG_WORDS = ("创作背景", "创作历程", "背景", "灵感", "评价", "影响", "争议", "榜单",
             "获奖", "纪录", "改编", "合作", "导演", "剧", "电影", "专辑", "EP")
_YEAR_RE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
_SENT_SPLIT = re.compile(r"(?<=[。！？；])")


# ── 落盘缓存 ────────────────────────────────────────────────────────────────
def _load() -> dict:
    global _C
    if _C is not None:
        return _C
    with _C_LOCK:
        if _C is not None:
            return _C
        d: dict = {}
        try:
            if PAGE_FETCH_PATH.is_file():
                raw = json.loads(PAGE_FETCH_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    d = raw
        except Exception as e:                                            # noqa
            logger.debug("[正文缓存] 读取失败（忽略）：%s", e)
        _C = d
        return _C


def _save() -> None:
    if not _C_DIRTY[0]:
        return
    with _C_LOCK:
        if not _C_DIRTY[0]:
            return
        try:
            PAGE_FETCH_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = PAGE_FETCH_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(_C or {}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, PAGE_FETCH_PATH)
            _C_DIRTY[0] = 0
        except Exception as e:                                            # noqa
            logger.debug("[正文缓存] 写盘失败（忽略）：%s", e)


def _key(url: str) -> str:
    """缓存键：**去掉 query/fragment**（`?bst=...` 每次不同，不去会把缓存打穿）。"""
    try:
        p = urllib.parse.urlsplit(url or "")
        return "%s://%s%s" % (p.scheme, p.netloc, p.path)
    except Exception:                                                     # noqa
        return url or ""


def cache_get(url: str):
    d = _load()
    it = d.get(_key(url))
    if not it:
        return None
    if PAGE_CACHE_TTL_S > 0 and time.time() - float(it.get("ts") or 0) > PAGE_CACHE_TTL_S:
        return None
    return it


def cache_put(url: str, text: str, title: str = "") -> None:
    if not text or len(text) < 120:
        return
    d = _load()
    d[_key(url)] = {"ts": time.time(), "chars": len(text), "title": title, "text": text}
    if len(d) > PAGE_CACHE_MAX:
        for k in sorted(d, key=lambda x: float(d[x].get("ts") or 0),
                        reverse=True)[PAGE_CACHE_MAX:]:
            d.pop(k, None)
    _C_DIRTY[0] = 1
    _save()


def cache_stats() -> dict:
    d = _load()
    return {"size": len(d), "path": str(PAGE_FETCH_PATH)}


# ── 取页 ────────────────────────────────────────────────────────────────────
def _svc_url() -> str:
    try:
        from config.settings import user_config
        u = (user_config.data.get("search", {}) or {}).get("browser_url")
        if u:
            return str(u).rstrip("/")
    except Exception:                                                     # noqa
        pass
    return "http://127.0.0.1:8787"


def fetch_page(url: str, kind: str = "auto", *, allow_live: bool = False,
               timeout: float | None = None) -> dict:
    """取一页正文。**缓存优先**；`allow_live=False` 时缺缓存直接返回 `{}`（不发网络）。"""
    if not url:
        return {}
    hit = cache_get(url)
    if hit:
        return {"ok": True, "url": url, "text": hit.get("text") or "",
                "chars": int(hit.get("chars") or 0), "title": hit.get("title") or "",
                "cached": True}
    if not allow_live:
        return {}
    if os.environ.get("MUSICAI_PAGE_FETCH", "1") == "0":
        return {}
    u = _svc_url() + "/fetch?" + urllib.parse.urlencode({"url": url, "kind": kind or "auto"})
    try:
        with _OP.open(u, timeout=timeout or SVC_TIMEOUT_S) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:                                                # noqa
        logger.info("[正文回捞] 失败（%s）：%s" % (url[:60], str(e)[:90]))
        return {}
    if d.get("ok") and d.get("text"):
        cache_put(url, d["text"], d.get("title") or "")
        _C_DIRTY[0] = 1
        _save()
        return d
    logger.info("[正文回捞] 被拒（%s）：%s" % (url[:60], str(d.get("reason"))[:70]))
    return {}


# ── 句子级抽取（L3） ────────────────────────────────────────────────────────
def score_sentence(s: str) -> int:
    """一句话的"值不值得进上下文"：事实词种类 + 年份 + 背景类词。"""
    if not s or len(s) < 8:
        return -99
    sc = sum(1 for w in _FACT_WORDS if w in s)
    sc += 2 * len(set(_YEAR_RE.findall(s)))
    sc += sum(2 for w in _BG_WORDS if w in s)
    if len(s) > 180:                       # 过长的一句本身不好用
        sc -= 2
    return sc


def digest(text: str, cap: int = PAGE_BUDGET_CHARS) -> str:
    """从正文里挑**事实句**拼成摘要（Contextual Compression 的规则化版本，不调模型）。

    依据：句子级抽取（"保留高信息量的句子"）比整段截断在同样预算下密度更高。
    ★ 不变量：**一句都挑不出来时返回空串**（由调用方决定要不要退回原文开头）。
    """
    t = " ".join((text or "").split())
    if not t:
        return ""
    sents = [s.strip() for s in _SENT_SPLIT.split(t) if len(s.strip()) >= 8]
    if not sents:
        return ""
    scored = [(score_sentence(s), i, s) for i, s in enumerate(sents)]
    ranked = sorted(scored, key=lambda x: -x[0])
    picked, used = [], 0
    for sc, idx, s in ranked:
        if sc <= 0:
            break
        if used + len(s) > cap:
            continue
        picked.append((idx, s))
        used += len(s)
        if used >= cap * 0.92:
            break
    picked.sort()                          # ★ 还原原文顺序（读起来才连贯）
    return "".join(s for _i, s in picked)


def enrich_block(results, *, title: str = "", artist: str = "", n: int = PAGE_N,
                 allow_live: bool = False, cap: int = PAGE_BUDGET_CHARS):
    """给一批检索结果配"词条正文"块。返回 `(text, stats)`。

    `text` 为空串表示"这次拿不到正文"（调用方原样用摘要即可，不必分叉逻辑）。
    只挑 `result_kind.FETCH_WORTHY` 的类型，且**跳过同名异物**（实测 `波西米亚人（民族）`
    就是靠这一步挡掉的）。
    """
    from ai.material_guard import song_identity
    out, stat = [], {"picked": [], "skipped": [], "live": allow_live}
    used = 0
    for r in (results or []):
        u = getattr(r, "url", "") or ""
        if not u:
            continue
        kind = RK.classify(getattr(r, "title", "") or "", u,
                           getattr(r, "snippet", "") or "",
                           entity_q=title, artist_q=artist)
        if kind not in RK.FETCH_WORTHY:
            stat["skipped"].append({"title": (getattr(r, "title", "") or "")[:28],
                                    "why": kind})
            continue
        d = fetch_page(u, kind="auto", allow_live=allow_live)
        if not d or not d.get("text"):
            stat["skipped"].append({"title": (getattr(r, "title", "") or "")[:28],
                                    "why": "无缓存" if not allow_live else "回捞失败"})
            continue
        body = digest(d["text"], cap=cap)
        # Reject navigation-only pages and wrong entities even if their URL is trusted.
        if not song_identity((d.get('title') or r.title or '') + ' ' + body, title, artist):
            stat['skipped'].append({'title':(r.title or '')[:28], 'why':'正文与当前歌曲身份不符'})
            continue
        if len(body) < 60:
            stat["skipped"].append({"title": (getattr(r, "title", "") or "")[:28],
                                    "why": "正文无可摘事实句"})
            continue
        out.append({"title": d.get("title") or (getattr(r, "title", "") or ""),
                    "kind": kind, "body": body, "chars": len(body),
                    "cached": bool(d.get("cached")), "url": u})
        used += len(body)
        if len(out) >= max(1, int(n)):
            break
    if not out:
        return "", stat
    lines = []
    for o in out:
        lines.append("- %s（%s）" % (o["title"][:70], o["kind"]))
        lines.append("    " + o["body"])
    stat["picked"] = [{"title": o["title"][:40], "kind": o["kind"], "chars": o["chars"],
                       "cached": o["cached"]} for o in out]
    stat["chars"] = sum(len(x) + 1 for x in lines)
    return "\n".join(lines), stat


def prewarm_pages(tracks, n: int = 3, per_track: int = PAGE_N) -> int:
    """给"接下来 n 首"填**词条正文缓存**（背景线程用；一次 5~6 s/页，可接受）。

    与 `ai.search.prewarm_upcoming` 配套：那边填**摘要**缓存，这里填**正文**缓存。
    """
    if os.environ.get("MUSICAI_PAGE_FETCH", "1") == "0":
        return 0
    done = 0
    for t in list(tracks)[:max(0, int(n))]:
        try:
            if isinstance(t, (tuple, list)):
                title, artist = (t[0], t[1] if len(t) > 1 else "")
            else:
                title = getattr(t, "title", "") or ""
                artist = getattr(t, "artist", "") or ""
        except Exception:                                                 # noqa
            continue
        q = ("%s %s" % (title, artist)).strip()
        if not q:
            continue
        try:
            from ai.search import get_search_provider, rank_and_clean
            raw = get_search_provider().search(q, 10)
            rs = rank_and_clean(raw, q)
            txt, st = enrich_block(rs, title=title, artist=artist, n=per_track,
                                   allow_live=True)
            if txt:
                done += 1
                logger.info("[正文预热] %s → %d 条 / %d 字（%s）",
                            q[:26], len(st.get("picked") or []), st.get("chars") or 0,
                            "；".join("%s:%s" % (p["title"][:14], p["chars"])
                                      for p in (st.get("picked") or [])[:2]))
        except Exception as e:                                            # noqa
            logger.info("[正文预热] 跳过 %s：%s", q[:26], str(e)[:70])
    return done
