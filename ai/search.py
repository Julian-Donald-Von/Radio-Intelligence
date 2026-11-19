"""
检索层：联网搜索（DuckDuckGo / 浏览器内核）/ 离线本地向量库（sqlite-vec），运行时切换。

抽象基类 SearchProvider 定义统一接口；三个实现：
  - OnlineSearchProvider  : DuckDuckGo（duckduckgo-search，零 API Key）
  - BrowserSearchProvider : 本地「浏览器内核搜索服务」(tools/websearch_service，替代 DDG)
  - OfflineSearchProvider : 本地 SQLite + sqlite-vec 向量检索（数据来自歌曲元数据/艺人 Wiki/歌曲故事）

默认 search.mode="browser"：应用启动时由 tools/websearch_service/launcher 静默后台拉起服务子进程，
service 未启动/无 Chrome 时本 provider 优雅降级为空结果（不影响口播生成）。

关键约束（来自部署规范）：
  - 联网/离线切换必须清理上一轮的搜索上下文，避免信息污染 → 每次 search() 前都
    调用 clear_context()，且返回的都是本次独立、无残留的结果。
  - 所有路径相对项目根（OFFLINE_KB_DIR），可移植。
  - 禁止硬编码任何外部服务依赖（除本地 Ollama）；DuckDuckGo 无需 Key。
"""
from __future__ import annotations

from core.op_timer import timed

import os
import hashlib
import json
import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import DATA_DIR, OFFLINE_KB_DIR, user_config

logger = logging.getLogger("MusicAI.Search")

# ═══ ★★★ 2026-09-21：检索**结果质量**（纯函数，见文件顶部 `_r119_patch_search_quality` 说明）═══
# 量出来的事实（真机 :8787）：`{歌名 歌手}` 的 5 条结果里 **40~60% 是 MV/视频/工具页**，
# 而注入是"按原顺序先到先得" ⇒ 噪声把真正的百科摘要挤出预算 = 用户说的"汇总的内容非常少"。
# 这里做**确定性**的筛/排（不引入模型、不引入新服务）：
#   · 硬门槛：标题+摘要必须含**全部**查询词（挡 `lemon.qq.com 腾讯柠檬清理` 这类跑偏结果）；
#   · 负分：视频/下载站域名 + 标题噪声词；正分：百科/维基/音乐平台 + "创作背景/收录于/专辑"…
#   · **fail-open**：筛完为空就返回原表 —— 宁可给噪声，也绝不把结果清成空。
# ★★★ 2026-09-21（用户明确要求）：**权威来源优先级**（"以百科、音乐介绍为准"）。
#   检索从「很多条里挑」，挑的标准就是来源权威度，层级（高->低）：
#     ① 百科类：baike.baidu.com / wikipedia.org / zh.wikipedia.org / baike.so.com ——
#        讲一首歌最可信的「创作背景/专辑/发行」几乎都在，权重最高(6)。
#     ② 音乐介绍/乐评类：music.163.com / y.music.163.com / douban.com / music.douban.com /
#        allmusic.com / pitchfork.com / rollingstone.com / nme.com / kkbox / streetvoice ——
#        官方简介、乐评、赏析(4)。
#     ③ 音乐平台/试听：y.qq.com / c.y.qq.com / music.apple.com / spotify.com(3)。
#     ④ 歌词库：mojim.com / genius.com(2)，仅作事实补充。
#     ⑤ 视频/下载/社交：全部负分（见 _DOM_BAD），绝不进注入预算。
#   ★ 权重差（百科 6 远高于其它）保证「挑」的时候权威源稳定胜出，不受查询命中数扰动。
_DOM_GOOD = {"baike.baidu.com": 6, "wikipedia.org": 6, "zh.wikipedia.org": 6, "baike.so.com": 6,
             "music.163.com": 4, "y.music.163.com": 4, "douban.com": 4, "music.douban.com": 4,
             "allmusic.com": 4, "pitchfork.com": 4, "rollingstone.com": 4, "nme.com": 4,
             "kkbox.com": 4, "streetvoice.com": 4,
             "y.qq.com": 3, "c.y.qq.com": 3, "music.apple.com": 3, "spotify.com": 3,
             "mojim.com": 2, "genius.com": 2,
             "zhihu.com": 1}
_DOM_BAD = ("v.qq.com", "iqiyi.com", "youku.com", "douyin.com", "mgtv.com", "le.qq.com",
            "kugou.com", "kuwo.cn", "9ku.com", "gequbao", "zhangxinhua", "51vv",
            "bilibili.com", "bilibili.com/video", "weibo.com",
            "y.qq.com/n/ryqq/mv", "music.163.com/#/mv")
# ═══ ★★★ R119-26：**权威来源白名单（硬闸）** ══════════════════════════════
#   用户要求（2026-09-21）："检索来源必须是权威的，不要脏来源、不知名来源"。
#   旧行为只给域名**打分**（`_DOM_GOOD` / `_DOM_BAD`）——脏源只是排后面，
#   在"结果少"时照样会被注入。实测注入块里混着 腾讯视频 / bilibili / 知乎 /
#   今日头条 / 各类歌词站 / 旅游攻略页，模型从中挑"事实"，口播就出现
#   "拼贴感"（不同来源的碎片被当成同一首歌的事实）。
#   ⇒ 改成**硬闸**：歌曲资料只认白名单域名，其余一律丢弃。
#     宁可这一首没资料，也不要不知名来源喂出来的错事实。
#   ⚠ 只作用于**歌曲查询**（song=True）：在地/环境检索的来源是城市百科与
#     本地生活站，不在本白名单内，放进去会把在地素材整段清空。
#   回退开关：`MUSICAI_GATE_DOMAIN=0`（回归对照用，关掉即回到"只打分不硬剔"）。
#   排除项：`zhihu.com`（权重 1）是 UGC 问答，不算权威来源，不进白名单。
_DOM_WHITELIST = tuple(dom for dom, w in _DOM_GOOD.items() if w >= 2)

# ═══ ★★★ 2026-11-17（用户纠正）：白名单从「硬剔」改成「分档」 ═══════════════════
#   用户原话：「它会自己否定一些不够权威但是也可能是真的的那种参考资料，我觉得还是
#   留一下合适……不然就会过滤掉太多的不好的信息」，以及「你看是不是……又接错了」。
#   ⇒ 保留 R119-26 的**优先权**（白名单条目仍排最前、仍先占预算），但**不再当死刑**：
#     · `_DOM_BAD`（视频/下载/社交站）、歌词闸、作品锚闸 ⇒ **仍然硬剔**（脏源口径不变）；
#     · 通过作品锚、只是"域名不认识"的条目 ⇒ 降为**弱来源**：条目前缀〔未核实〕、
#       **限额**注入、永远排在权威条目之后；权威条目够多时**根本不注入**。
#   ★ 为什么不干脆全放行：R119-26 的实测证据仍然成立 —— 混入腾讯视频 / bilibili /
#     知乎 / 头条的碎片，口播会出现"拼贴感"（不同来源的碎片被当成同一首歌的事实）。
#     分档 + 限额 + 显式标注，才既留住"可能是真的"，又不回到拼贴。
#   回退开关：`MUSICAI_GATE_WEAK=0` ⇒ 完全回到 R119-26 的硬剔行为（回归对照用）。
_WEAK_MAX = 2        # 弱来源最多注入几条（再多就是拿噪声挤占事实预算）
_WEAK_FILL = 3       # **内容级**条目少于这个数时才补弱来源（补到 3 条为止）
_WEAK_TAG = "〔未核实〕"   # 注入行前缀；brain.py 用同一串数字数条数（勿改字面）


def _weak_on() -> bool:
    """`MUSICAI_GATE_WEAK=0` ⇒ 关掉弱来源档（回到 R119-26 硬剔）。"""
    return os.environ.get("MUSICAI_GATE_WEAK", "1") != "0"


def _dom_weight(host: str) -> int:
    """域名在白名单里的权重（0 = 不在白名单；最长后缀匹配，取最大权重）。"""
    h = (host or "").lower().strip()
    if not h:
        return 0
    best = 0
    for dom, w in _DOM_GOOD.items():
        if h == dom or h.endswith("." + dom):
            best = max(best, w)
    return best


def _host_of(url: str) -> str:
    """取 URL 的主机名（小写）；畸形 URL 给空串。"""
    try:
        return (url or "").split("/")[2].lower()
    except Exception:                                       # noqa
        return ""


def _is_whitelisted(host: str) -> bool:
    """权威来源判定：主机名**等于或以其为后缀**即命中（如 `y.music.163.com`）。"""
    h = (host or "").lower().strip()
    if not h:
        return False
    return any(h == dom or h.endswith("." + dom) for dom in _DOM_WHITELIST)
_TITLE_BAD = ("mv", "在线看", "在线观看", "完整版视频", "高清", "1080p", "视频", "下载",
              "无损", "伴奏", "铃声", "汉语词语", "词典", "字义", "清理工具", "客户端",
              # ★ v3：词典/翻译类结果（实测 `Bohemian Rhapsody Queen` 会命中
              #   `dictionary.cambridge.org BOHEMIAN 中文翻译`）—— 对"说这首歌"没有价值
              "翻译", "释义", "读音",
              # ★ v3b：实测 `Lemon 米津玄師` 会带出 `lemon（英文单词）_百度百科`
              "英文单词", "单词", "词语", "词组")
# ★★★ v3（2026-09-21）：**候选池倍率**。实测 `{歌名 歌手}` 的 5 条里只有 2 条真含歌的信息
#   （其余是 MV/视频/下载站），而把池子放宽到 10 条后**保留量翻倍**（2 → 4~5 条）。
#   ⇒ `format_context` 向 provider 要 `max_results × 本倍率` 条候选，筛完再取 `max_results` 条。
#     这样"用户设置的是**最终注入几条**"语义不变，但**汇总内容显著变多**。
#   ⚠ 只影响**一次 HTTP** 的返回条数，不增加请求次数；可 `MUSICAI_SEARCH_POOL=1` 关回。
def _pool_mul() -> int:
    # ★ 2026-09-21（用户）：候选池倍率。默认 2 = 向 provider 要 `max_results*2` 条
    #   候选（如 max_results=5 → 10 条），排序时再从「很多条里挑」权威源（百科/音乐介绍）。
    #   这就是用户要的「很多条里挑」（10 条远非「几条」），同时避免池子过大把更多
    #   在地/城市素材灌进上下文、推高「城市名出现率」（与 R119-21 冲突）。
    #   想更激进可 `MUSICAI_SEARCH_POOL=3`（代价：城市名率可能上行）。
    try:
        return max(1, int(os.environ.get("MUSICAI_SEARCH_POOL") or 2))
    except Exception:                                       # noqa
        return 2
_POOL_MUL = _pool_mul()
_TITLE_GOOD = ("百科", "创作背景", "创作历程", "作词", "作曲", "编曲", "收录于", "专辑",
               "发行", "演唱", "主题曲", "词曲", "单曲", "歌词", "简介", "乐评",
               "解读", "赏析", "故事", "资料")


def _q_tokens(query: str) -> list:
    """查询词：按空白切分；中文串整段保留（不做分词 —— 只用于"全含"门槛，不需要精度）。"""
    return [t for t in (query or "").replace("　", " ").split() if t.strip()]


def rank_and_clean(results: list, query: str) -> list:
    """★ 纯函数：给检索结果打分排序并剔除明显噪声。**输入输出都是 SearchResult 列表**。

    规则见上方 `_DOM_GOOD` 注。三条不变量：
      ① 硬门槛只做"**全含**查询词"，不做任何语义判断；
      ② 打分只看域名与标题/摘要关键词（确定性、可复现、可"见红"）；
      ③ **fail-open** —— 结果为空一律返回原始表。
    """
    if not results:
        return results
    toks = [t.lower() for t in _q_tokens(query)]

    # ★ 2026-09-21 v2：门槛从「**全含**」放宽为「**至少命中一个**」。
    #   全含会**误杀**：`米津玄師`（查询用日文「師」）对不上百度百科条目的
    #   `米津玄师`（简体）⇒ 最有用的那条被挡掉（实测 `Lemon` 只剩 1 条，比修前更少）。
    #   改成"任一命中"后，`lemon.qq.com 腾讯柠檬清理` 仍被挡住（它标题里只有中文「柠檬」、
    #   不含 ascii 的 `lemon`），而百科条目因为含 `lemon` 而保留。命中数另计入加分。
    def n_hits(r) -> int:
        if not toks:
            return 0
        blob = ("%s %s" % (r.title or "", r.snippet or "")).lower()
        return sum(1 for t in toks if t in blob)

    kept = [r for r in results if not toks or n_hits(r) >= 1]
    if not kept:
        kept = list(results)                      # ③ fail-open

    def score(r) -> int:
        url = (r.url or "").lower()
        blob = ("%s %s" % (r.title or "", r.snippet or "")).lower()
        s = 0
        for dom, w in _DOM_GOOD.items():
            if dom in url:
                s += w
        for dom in _DOM_BAD:
            if dom in url:
                s -= 5
        for w in _TITLE_BAD:
            if w in blob:
                s -= 3
            if w in (r.title or "").lower():
                s -= 2
        for w in _TITLE_GOOD:
            if w in blob:
                s += 2
        return s

    def total(r) -> int:
        return score(r) + 2 * n_hits(r)           # ★ v2：命中查询词数也计入

    # ★ v3：**去重**（同域名 + 同标题前缀只留一条）—— 宽池下常出现同一首歌的
    #   两个百度百科条目（歌曲 / 专辑），重复占位等于变相减少"汇总内容"。
    dedup, seen = [], set()
    for r in kept:
        try:
            host = (r.url or "").split("/")[2].lower()
        except Exception:                                   # noqa
            host = ""
        key = (host, "".join((r.title or "").split())[:16].lower())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    kept = dedup or kept

    ranked = sorted(kept, key=total, reverse=True)
    # ★★ 2026-09-21 v2：**直接剔除**非正分条目 —— 只排序是不够的：
    #   `format_context` 按预算**先到先得**，排在后面的噪声照样会被注入
    #   （实测 `晴天` 的 648 字里第 3 条就是爱奇艺短剧）。
    #   保底规则：正分 ≥2 条 ⇒ 只留正分；否则「正分 + 其余」补到 2 条；全负 ⇒ 至少留 1 条。
    pos = [r for r in ranked if total(r) > 0]
    if len(pos) >= 2:
        out = pos
    else:
        # ★ v3c：补齐只许用**非负分**的条目 —— 端到端冒烟抓到：旧写法 `rest` 含负分，
        #   于是"补到 2 条"把已知噪声（9ku 下载站）又放回来了。
        #   宁可只给 1 条干净的，也不要凑数塞噪声。
        rest = [r for r in ranked if r not in pos and score(r) >= 0]
        out = (pos + rest)[:2] if (pos + rest) else pos
    if not out:
        out = ranked[:1]
    logger.debug("[检索排序] %s → %s", (query or "")[:30],
                 [("%s…" % (r.title or "")[:14], total(r)) for r in out])
    return out


# ══════════════════════════════════════════════════════════════════════════
# ★★★ 2026-09-21 v4：**资料筛选层**（"这些信息有没有筛选" —— 实测没筛）
#
# 实测（真机 4 首，注入文本里"没用的"占比）：Bohemian 79% / 晴天 74% /
#   起风了 64% / Lemon 1/3。具体是：同名词条（波西米亚民族）、整页歌词、
#   别人的翻唱版歌词页、同名开源工具（oi-wiki lemon）。
# 而提示词告诉模型"这是本轮唯一的事实来源" ⇒ 它拿到的多半是歌词与跑题文字，
#   于是只能写情绪 —— "懒检索"的一半根因在这里。
#
# 四道闸（全部确定性、可单测、可"见红"）：
#   ① 作品锚：这条必须**在讲这首歌** —— `song_identity`：标题必中；**给了歌手则
#      歌手必中**（R119-28 收窄：不再靠"含作品词"给错歌放行）；没给歌手时才要求含作品词
#   ② 歌词页：识别 + **剥离歌词体**（但把 KV 事实捞回来）；非本人的翻唱页直接剔
#   ③ 事实密度：数事实词，密度 0 的排到最后（不与有事实的条目争预算）
#   ④ 去重（沿用 v3）
# ══════════════════════════════════════════════════════════════════════════

# 音乐**作品**类词：出现它才说明这条在讲一首歌/专辑
# ★ R119-28c：从标题/摘要里抽"被点名的歌手"的三个形态（窄闸 `_names_other_artist` 用）
_OTHER_ARTIST_RES = (
    re.compile(r"(?:歌手|演唱者|原唱|演唱|唱)\s*[:：]\s*([^，。、,;；\s\|\-]{1,16})"),
    #   ⚠ 名字字符类**必须排除连接词/虚词**（由是和与及就都而并）：否则
    #     `是由周杰伦演唱` 会捕获成 `由周杰伦` ⇒ 与本人 `周杰伦` 比对不等 ⇒ **误杀本人页**（实测 A3c）。
    re.compile(r"(?:由|是|，|,|\s)([^《》，。、,;；\s\|\-（()）0-9由是和与及就都而并]{1,14}?)\s*(?:演唱|翻唱)"),
)
# ⚠ 曾尝试第三条 `(?:演唱|翻唱)[的是]?(X)` —— **必须不要**：它会把
#   `演唱的一首歌曲` 里的 `一首歌曲` 当成歌手名 ⇒ 判本人页"唱的是别人"⇒ 误杀
#   （实测：周杰伦《晴天》页 A3c 转红）。KV 形态 + `由 X 演唱` 两条已覆盖全部真机案例。
_ARTIST_NAME_STOP = frozenset((
    "是", "的", "由", "一个", "这首", "该曲", "歌曲", "单曲", "专辑",
    "作品", "音乐", "歌", "由是", "演唱", "翻唱",
))
_WORK_HINTS = ("专辑", "单曲", "收录于", "发行", "作词", "作曲", "编曲", "词曲",
               "主题曲", "插曲", "片尾曲", "片头曲", "演唱", "歌手", "乐队", "歌曲",
               "主打", "创作", "榜单", "获奖", "销量", "mv", "ost", "原唱", "同名专辑")
# 事实词：用来量"这条有多少可用事实"
_FACT_WORDS = ("收录于", "发行", "专辑", "单曲", "作词", "作曲", "编曲", "词曲",
               "主题曲", "演唱", "创作", "主打", "获得", "获奖", "奖", "榜",
               "销量", "制作", "原唱", "翻唱", "出道", "乐队", "歌手")
# 歌词页特征（域名/标题）
_LYRIC_MARKS = ("歌词", "lyric", "lrc", "geci", "mojim", "kuge")
# KV 事实：歌词页里也常有的"硬事实"，剥离歌词体时必须捞回来
_KV_FACT = re.compile(r"(作词|作曲|编曲|制作人|原唱|演唱|歌曲名称|词|曲)\s*[:：]\s*"
                      r"([^\s，。；;、|/]{1,24})")
_YEAR_RE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
# ★ L0 归一：**单一来源**。日文汉字折简体 / NFKC / 零宽剥离 / 别名表都收敛到
#   `ai/result_kind.normalize_entity`（与 L1 类型判定共用同一份，杜绝两处漂移）。
#   旧实现在此处另写了一份 20 字 JP2SC + `_NOISE_RE`，已删除（见 R119-16 §2 设计债）。
from ai import result_kind as _RK
from ai.material_guard import song_identity, verified_context, environment_context, primary_artist

_NOISE_RE = re.compile(r"[\s\u3000·・,，。.、:：;；\-—_\|()（）\[\]【】\"'“”‘’!！?？]")


def _norm_entity(s: str) -> str:
    """实体名归一：去空白/标点 + 日文汉字折简体 + NFKC + 零宽剥离 + 别名表 + 小写。

    ⚠ 必须有：查询里是日文 `米津玄師`，而百度百科条目写的是简体 `米津玄师`
      ⇒ 不归一会把**最有价值的那条**判成"不是这首歌"（这类误杀本轮踩过）。
    ⚠ 别名表（`Queen↔皇后乐队`）根上解决"歌手对不上就丢"，见 `result_kind._ALIASES`。
    """
    return _RK.normalize_entity(s)


def _has_lyric_mark(title: str, url: str) -> bool:
    blob = ("%s %s" % (title or "", url or "")).lower()
    return any(m in blob for m in _LYRIC_MARKS)


def _lyric_run_start(text: str) -> int:
    """歌词体起点（找不到给 -1）。**确定性判据**，不需要歌词词典：

    从某处起出现**短句密集段** —— 以标点/空白切出的片段里，连续 ≥4 段
    长度 ≤12 且**不含任何事实词**。歌词恰好是这个形状，而介绍性文字不是
    （介绍句里总有"收录于/专辑/发行/演唱"）。
    """
    t = _NOISE_FREE = " ".join((text or "").split())
    if not t:
        return -1
    segs = [(m.start(), m.group(0)) for m in
            re.finditer(r"[^，。！？；;,.!?\s]+[，。！？；;,.!?]?", t)]
    run: list = []
    for st, seg in segs:
        core = seg.strip("，。！？；;,.!? ")
        short = 0 < len(core) <= 12
        factual = any(w in seg for w in _FACT_WORDS)
        if short and not factual:
            run.append(st)
            if len(run) >= 5:
                # ★★ 硬约束：**歌词体里不可能有事实词**（歌词不会写"收录于/专辑/发行"）。
                #   从候选起点到结尾只要有事实词，就说明这是被标点/空格切碎的**普通叙述**，
                #   不是歌词 —— 实测 `Bohemian Rhapsody` 的百科条目就栽在这里：
                #   「…由罗伊·托马斯·贝克、皇后乐队制作，收录于1975年…」被空格切成短句，
                #   凑够段数后把**最该保留的那一条**判成歌词页丢掉了。
                if not any(w in t[run[0]:] for w in _FACT_WORDS):
                    return run[0]
                run = []          # 尾巴里有事实词 ⇒ 不是歌词体，继续往后扫
        else:
            run = []
    return -1


def _kv_facts(text: str) -> str:
    """从文本里抽 `作词：X / 作曲：X …` 这类硬事实（去重、保序，最多 6 条）。"""
    kv, seen = [], set()
    for m in _KV_FACT.finditer(text or ""):
        k, v = m.group(1), m.group(2).strip()
        if not v or _norm_entity(v) in seen:
            continue
        seen.add(_norm_entity(v))
        kv.append("%s %s" % (k, v))
    return "；".join(kv[:6])


def strip_lyric_body(text: str) -> str:
    """剥掉歌词体，**但保留歌曲页的元信息**（含歌词体之后的 KV 事实）。

    为什么不能只"截到歌词体起点"：实测 `起风了` 的笑匠歌词库页里，
    `作词 : 米果 作曲 : 高橋優 编曲 : 池窪浩一` 正好排在**歌词体之后**
    —— 朴素截断会把这三条最有价值的事实一起丢掉。
    """
    t = " ".join((text or "").split())
    if not t:
        return ""
    kv = _kv_facts(t)
    head = t
    cut = -1
    for mark in ("歌词正文", "歌词内容", "【主歌", "【副歌", "【过渡", "[主歌", "LRC歌词"):
        i = head.find(mark)
        if i >= 0:
            cut = i
            break
    if cut < 0:
        i = _lyric_run_start(head)
        if i > 0:
            cut = i
    if cut > 0:
        head = head[:cut].strip(" -—·|、，,。")
    else:
        # ★ 没切到歌词体 ⇒ **一字不动**（连结尾句号都不许动）。
        #   实测踩到：无条件 `strip("。")` 会把百科那条的句末句号吃掉 ——
        #   虽然只是标点，但它违反"没切就不改"的契约，且会被回归判据逮住。
        head = t
    if kv and _norm_entity(kv) not in _norm_entity(head):
        head = (head + "；" + kv) if head else kv
    return head


def _fact_density(text: str) -> int:
    """事实密度：命中事实词的**种类**数 + 年份个数（不按出现次数，避免刷词）。"""
    t = text or ""
    return sum(1 for w in _FACT_WORDS if w in t) + len(set(_YEAR_RE.findall(t)))


def _cut_sentence(s: str, cap: int) -> str:
    """按**句末**截断（硬切会把半句留下，模型容易照着半句续写）。"""
    s = (s or "").strip()
    if cap <= 0 or len(s) <= cap:
        return s
    head = s[:cap]
    i = max(head.rfind(x) for x in "。！？；")
    if i >= int(cap * 0.5):
        return head[:i + 1]
    return head.rstrip(" ，,、") + "…"


# ★ v4：**在地意象**评分（只用于环境类查询 `song=False` 的排序）。
#   为什么不能用"事实密度"排：实测环境查询里 `长春市人民政府`（一串权责事项通知，
#   日期多 ⇒ 密度高）会被排到 `长春市_百度百科`（真正的城市描述）**前面**，
#   而它恰恰是用户抱怨过的"政府网"噪声。环境素材要的是**画面感**，不是公告日期。
_SCENE_GOOD = ("地标", "街", "巷", "广场", "公园", "剧院", "剧场", "音乐", "演出",
               "livehouse", "艺术", "文化", "商圈", "老城", "历史", "风物",
               "小吃", "美食", "氛围", "夜色", "书店", "咖啡馆", "河", "湖", "桥",
               "景点", "博物馆", "美术馆", "大学", "早市", "夜市")
_SCENE_BAD = ("政府", "通知", "权责", "规划", "印发", "公告", "招标", "采购",
              "人大", "政协", "批复", "会议", "招聘", "事业单位")


def _scene_score(text: str) -> int:
    """在地意象：有画面感的词 +2，行政公告类词 -3。"""
    t = text or ""
    return sum(2 for w in _SCENE_GOOD if w in t) - sum(3 for w in _SCENE_BAD if w in t)


# ═══════════════════════════════════════════════════════════════════════════
# ★ R119-16 · 检索筛选闸 L4/L5/L6（确定性、可"见红"、可回退）
#   判据开关：`MUSICAI_GATE_{DEDUP,MMR,HEADTAIL,ALIAS}=0` 各自关掉对应一级
#   （回归对照用 —— 关掉后该级失效，能反过来证明"是它起了作用"）。
# ═══════════════════════════════════════════════════════════════════════════
def _gate_on(name: str) -> bool:
    """闸级回退开关：`MUSICAI_GATE_<NAME>=0` ⇒ 关掉该级。"""
    return os.environ.get("MUSICAI_GATE_%s" % name, "1") != "0"


def _sim3(a: str, b: str) -> float:
    """字符 3-gram Jaccard 相似度（近重复判据，阈值 0.6）。"""
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if len(a) < 3 or len(b) < 3:
        return 0.0
    ga = {a[i:i + 3] for i in range(len(a) - 2)}
    gb = {b[i:i + 3] for i in range(len(b) - 2)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _dedup_approx(cands: list, *, song: bool) -> list:
    """L4 近似去重：3-gram Jaccard ≥ 0.6 的近重复只留首次出现。
    仅歌曲查询启用（环境查询保持原行为，避免把不同城市的样板页误并）。
    `MUSICAI_GATE_DEDUP=0` ⇒ 退化为精确去重（用于回归对照）。"""
    if not _gate_on("DEDUP") or not song:
        return cands
    out, seen = [], []
    for c in cands:
        if any(_sim3(c["snippet"], s) >= 0.6 for s in seen):
            continue
        seen.append(c["snippet"])
        out.append(c)
    return out


def _mmr_greedy(cands: list, *, base_key: str, lam: float = 0.5,
                sim_thr: float = 0.5, max_per_domain: int = 2) -> list:
    """L5 MMR 多样性：按 base 分从高到低选；每选一条，把它**相似（Jaccard≥sim_thr）**
    的候选分数 ×(1-λ)，再继续选。同时硬约束同域名最多 `max_per_domain` 条。
    `MUSICAI_GATE_MMR=0` ⇒ 退化为按 base 分排序。"""
    if not _gate_on("MMR"):
        return sorted(cands, key=lambda k: -k[base_key])
    items = [dict(c, _s=float(c[base_key])) for c in cands]
    dom: dict = {}
    sel: list = []
    while items:
        elig = [it for it in items if dom.get(it["host"], 0) < max_per_domain]
        if not elig:
            break                       # 同域名已达上限 ⇒ 不再从该域取（硬约束生效）
        best = max(elig, key=lambda it: it["_s"])
        sel.append(best)
        items.remove(best)
        dom[best["host"]] = dom.get(best["host"], 0) + 1
        for it in items:
            if _sim3(it["snippet"], best["snippet"]) >= sim_thr:
                it["_s"] *= (1.0 - lam)
    return sel


def _head_tail(order: list) -> list:
    """L6 lost-in-the-middle：≥3 条时把第 2 名放末尾（[1,2,3..]→[1,3..,2]）。
    `MUSICAI_GATE_HEADTAIL=0` ⇒ 原序。"""
    if not _gate_on("HEADTAIL") or len(order) < 3:
        return order
    return [order[0]] + order[2:] + [order[1]]


# ★ 2026-09-21（R119-25 续）：词义/义项页识别 —— 外文「单词型」歌名
#   （Holocene / Apocalypse / Electric）被检索后端当成"查这个词的含义"，返回
#   词典/百科义项页（全新世 / 启示录 / 英语单词），不是歌。这类页绝不能当歌曲
#   事实注入，否则口播会断言"全新世一万一千七百年前开始"这种错事实（实测）。
_DEF_PAGE_RE = re.compile(
    r"[（(](?:英语单词|词语|词义|释义|义项|术语|地质年代|启示文学术语|地理术语|"
    r"生物术语|化学术语|物理术语|数学术语|成语|俗语)[）)]")
def _is_definition_page(title: str, snippet: str) -> bool:
    if _DEF_PAGE_RE.search(title or ""):
        return True
    s = (snippet or "").lower()
    if any(k in s[:50] for k in ("是一个", "是指", "意为", "英语单词",
                                  "is a ", "refers to", "meaning of")):
        if not any(w in s for w in _WORK_HINTS):
            return True
    return False
def _first_song_like(results, nt, na):
    """fail-open 兜底：**必须命中歌名**才兜底；宁可无资料，不可错歌。

    ★★★ R119-26：这里**删掉了原来的第二轮扫描**（"含歌手 或 含作品词"就放行）。
    它闯的祸（真机复现，见 `_probe_lemontree_out.txt`）：
      《Lemon Tree》- Fool's Garden 的检索里，Bing 把查询丢词成「Lemon」，
      返回 9 条**无一条**是 Fool's Garden（全是米津玄师《Lemon》/ 腾讯柠檬清理 /
      lemon 英文单词 / OI Wiki）。筛选层判这三条"跑题：没提到这首歌"**是对的**
      （归一化歌名 `lemontree` 确实不在它们的标题/摘要里），
      但第二轮扫描见米津玄师百科页含「歌曲/专辑/演唱」就把它捞回来注入，
      理由写着"确为歌曲页" ⇒ 口播对着 Fool's Garden 念「米津玄师 2018 年为
      日剧《非自然死亡》创作」——**整首歌的事实全是错的**。
    删掉后：歌名对不上就是没有资料，交给上层换查询形态（见 `format_context` 双路），
    而不是拿别的歌的事实充数。
    """
    for r in (results or []):
        raw = "%s %s" % (r.title or "", r.snippet or "")
        # ★ R119-28c 修：**必须用归一化后的 blob 去比对 `nt`**。
        #   `nt` 来自 `_norm_entity`（去空格/标点/大小写：`Bohemian Rhapsody`→`bohemianrhapsody`），
        #   而旧写法只做 `.lower()`（→ `bohemian rhapsody`，**带空格**）⇒ 多词歌名**永远比不中**、
        #   fail-open **恒不触发**（实测：Bohemian Rhapsody / After Dark / City Of Stars 全 MISS，
        #   只有单词歌名如 Lemon / 晴天 才命中）。这就是"确有歌曲页却判成无资料"的隐性漏兜底。
        #   修后仍保留 R119-26 的安全性质：**歌名必须命中**（宁可无资料，不可错歌）。
        blob = _norm_entity(raw)
        # ★ R119-28b：与 `curate_material` / `_ctx_relevant` 统一口径 ——
        #   给了歌手就**必须命中歌手**；旧写法 `or any(_WORK_HINTS)` 是同一处漏的
        #   第二份副本：错歌页必然含"专辑/演唱/发行"⇒ 歌手对不上也被兜底捞回，
        #   把主闸（curate_material 歌手闸）的修复**静默撤销**（实测 Sorrow/Mr.Kitty）。
        # ★ R119-28c：与窄闸同口径——歌名须命中，且**没点名别的歌手**。
        if nt and nt in blob and (not na or na in blob) and not _is_definition_page(r.title, r.snippet):
            return r
    return None


def _names_other_artist(blob_raw: str, na: str) -> bool:
    """★ R119-28c：这条**点名了别的歌手**吗（窄口径，治"注入别人的歌")。

    为什么不用"歌手必须命中"：那会把**不含歌手信息**的合法简介片段一并杀掉
    （`《SongY》是一首流行歌曲。` 这类，真机很常见）⇒ 误伤（回归 L5b/L6c）。

    为什么不能只看 `a_hit`：`a_hit=False` 既可能是"点了别人"，也可能是"谁都没点"。

    判据：从 标题+摘要 里抽出被点名的歌手名，存在一个既非本人、也不含本人的 ⇒ True。
      抽取形态：`歌手：X` / `演唱：X` / `原唱：X`（KV），`由 X 演唱` / `X 演唱`（动词）。
      ⚠ 归一用 `_norm_entity`（含别名表）⇒ 译名（Queen↔皇后乐队）不会误判成别人。
    """
    if not na:
        return False
    blob = blob_raw or ""
    got = set()
    for pat in _OTHER_ARTIST_RES:
        for m in pat.findall(blob):
            if m:
                got.add(m.strip())
    for g in got:
        ng = _norm_entity(g)
        if not ng or len(ng) > 14 or ng in _ARTIST_NAME_STOP:
            continue
        if ng == na or na in ng or ng in na:
            continue
        return True
    return False


def _mb_artist_match(got: str, want: str) -> bool:
    """★ 2026-09-23：MusicBrainz 结果的**艺人严格校验**（串歌熔断）。

    为什么需要：`_ctx_relevant` 用的是**子串包含** —— 实测考题「NIGHTMARE / Perfect」
    被 MB 匹配到美国艺人 **Mr Perfect**（"Perfect" ⊂ "Mr Perfect" ⇒ 判据通过），
    口播于是照着编出一张完全不相干的专辑《King Emmanuel Calling, Volume 3》。
    这类错误比"没资料"更糟：**没有资料模型会走兜底，错误资料会让它编得理直气壮。**

    原则：**宁可放弃素材，不可注入错歌**。只认严格等价，外加首字母缩写
    （YMO ↔ Yellow Magic Orchestra，这类简称很常见，错配的概率极低）。
    """
    def _n(s: str) -> str:
        return re.sub(r"[^a-z0-9一-鿿぀-ヿ가-힯]", "", (s or "").lower())
    g, w = _n(got), _n(want)
    if not g or not w:
        return False
    if g == w:
        return True
    # 首字母缩写：YMO ↔ Yellow Magic Orchestra（全称 vs 缩写，双向）
    # ⚠ 必须拿**原串**分词：`_n()` 会把空格一并删掉，g/w 已退化成单个连续 token，
    #   在其上 findall 永远只得 1 段 ⇒ 缩写判据形同虚设（实测 YMO 判 False）。
    def _words(s: str) -> list[str]:
        return [x for x in re.split(r"[^A-Za-z0-9一-鿿぀-ヿ가-힯]+", s or "") if x]

    gw = _words(got)
    ww = _words(want)
    if len(gw) > 1 and "".join(x[0] for x in gw).lower() == w:   # 全称 vs 缩写
        return True
    if len(ww) > 1 and "".join(x[0] for x in ww).lower() == g:   # 缩写 vs 全称
        return True
    return False


def _ctx_relevant(text: str, title: str, artist: str) -> bool:
    """★ R119-26：这份注入文本**是不是在讲这首歌**（换查询形态的判据）。

    为什么不能只看"非空"：实测《Lemon Tree》- Fool's Garden 的检索里 Bing 把查询
    丢词成「Lemon」，注入块 148 字**全是米津玄师《Lemon》** —— 非空，但整首是别的歌。
    为什么不能只看"字数"：只歌名时《成都》会返回「2025 成都旅游攻略」（82 字，
    含"成都"却是城市页）—— 字多字少都不能证明它讲的是这首歌。

    判据（两条取其一）：
      ① 含**歌手**（归一化后）—— 最强的"是这首歌"信号；
      ② 含**歌名** 且 含任一作品词（专辑 / 单曲 / 演唱 / 发行…）—— 歌手译名
         对不上时（如外文译名）的退路。
    """
    return bool(verified_context(text, title, artist))

# ═══════════════════════════════════════════════════════════════════════════
# ★ R119-27/P2：Wikipedia 兜底（**不依赖**——无代理时本机连不上，3s 超时后
#   本会话冷却 10min，静默跳过；将来开代理自动生效，绝不阻塞口播）。
#   只在外文歌 / 零 Bing 素材时才被 `format_context` 调用（最后一道兜底）。
# ═══════════════════════════════════════════════════════════════════════════
_WIKI_DOWN_UNTIL = [0.0]
_WIKI_COOLDOWN_S = 600.0
_WIKI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _wiki_reachable() -> bool:
    """会话内冷却：连不上 Wikipedia 的 10min 内不再试，避免每首歌白等 3s。"""
    return time.monotonic() > _WIKI_DOWN_UNTIL[0]


def _wiki_fallback(title: str, artist: str) -> str:
    """尝试 Wikipedia 抽取一首歌的事实摘要（zh 优先，退化 en）。失败/不可达 → ''。"""
    if not _wiki_reachable():
        return ""
    q = ("%s %s" % (title, artist)).strip()
    if not q:
        return ""
    try:
        _op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        _rows: list = []
        _errored = False
        for lang in ("zh", "en"):
            try:
                api = "https://%s.wikipedia.org/w/api.php" % lang
                with _op.open(urllib.request.Request(
                    api + "?" + urllib.parse.urlencode(
                        {"action": "query", "list": "search", "srsearch": q,
                         "format": "json", "srlimit": 3}),
                    headers={"User-Agent": _WIKI_UA}), timeout=2.5) as r:
                    d = json.loads(r.read().decode("utf-8"))
                _rows = [(lang, x["title"]) for x in
                         d.get("query", {}).get("search", [])]
                if _rows:
                    break
            except Exception:                                   # noqa 该语言失败 → 试下一个
                _errored = True
                continue
        if not _rows:
            # 不可达（无代理）→ 标记冷却，避免每首歌都白等 ~5s；将来开代理自动复效
            if _errored:
                _WIKI_DOWN_UNTIL[0] = time.monotonic() + _WIKI_COOLDOWN_S
            return ""
        _lang = _rows[0][0]
        _titles = "|".join(t for _, t in _rows[:3])
        with _op.open(urllib.request.Request(
            "https://%s.wikipedia.org/w/api.php" % _lang + "?" + urllib.parse.urlencode(
                {"action": "query", "prop": "extracts", "exintro": 1,
                 "explaintext": 1, "titles": _titles, "format": "json",
                 "redirects": 1}),
            headers={"User-Agent": _WIKI_UA}), timeout=2.5) as r:
            d2 = json.loads(r.read().decode("utf-8"))
        pages = d2.get("query", {}).get("pages", {})
        picked = [(pg.get("title", ""), pg.get("extract") or "")
                  for pg in pages.values() if pg.get("extract")]
        if not picked:
            return ""
        _lines = []
        for i, (pt, pe) in enumerate(picked[:2], 1):
            _clean = _clean_snippet(pe)
            _lines.append("[%d] %s" % (i, pt))
            if _clean:
                _lines.append("    " + _cut_sentence(_clean, 420))
        return "\n".join(_lines)
    except Exception:                                           # noqa 不可达/超时 → 冷却后跳过
        _WIKI_DOWN_UNTIL[0] = time.monotonic() + _WIKI_COOLDOWN_S
        return ""


def curate_material(results: list, *, title: str = "", artist: str = "",
                    per_cap: int = 340, budget_chars: int = 1800,
                    song: bool = True) -> tuple:
    """★ v4：把 N 条原始结果筛成**一份只讲这首歌的事实清单**。

    返回 `(text, stats)`：
      `text`  注入用文本；`（`[i] 标题` + 换行 + 缩进后的摘要`）`
      `stats` `{"kept":[{title,host,reason,chars}], "dropped":[{title,host,reason}]}`
              —— 逐条给出**去留理由**，让"筛选"可审计、可断言。

    ⚠ 不变量（R119-26 起是**有条件** fail-open）：全被筛掉时，只有 `_first_song_like`
      确认"歌名确实命中"才兜底 1 条；否则返回空 —— **宁可无资料，不可错歌**。
      （旧版无条件保留第一条，该策略已被真机"错歌注入"推翻；见 A10 / G 用例。）
    """
    nt, na = _norm_entity(title), _norm_entity(artist)
    cands: list = []
    dropped: list = []
    seen = set()

    for r in (results or []):
        t_raw = r.title or ""
        u_raw = r.url or ""
        sn_raw = _clean_snippet(_clean_snippet(r.snippet))
        blob_raw = "%s %s" % (t_raw, sn_raw)
        blob = _norm_entity(blob_raw)
        try:
            host = u_raw.split("/")[2].lower()
        except Exception:                                       # noqa
            host = ""
        # ── 闸 1：作品锚（**只对歌曲查询成立**） ──────────────────────
        #   ⚠ `song=False`（环境/其它语义查询）时**必须跳过**：那类查询里没有歌名/歌手，
        #     按 `{title} {artist}` 切出来的"歌手"是个短语，永远匹配不上 ⇒
        #     城市页会被成片判成"同名异物：非音乐内容"（实测 5 条丢 4 条，
        #     把用户 09-20 要求的在地环境素材又清空了）。
        t_hit = (nt in blob) if nt else True
        a_hit = (na in blob) if na else True
        w_hit = any(w in blob_raw.lower() for w in _WORK_HINTS)
        # ★ 词义/义项页（仅歌曲查询）：外文单词型歌名被当成查词含义，返回的是
        #   词典/百科义项页，不是歌，绝不能当歌曲事实注入。
        if song and _is_definition_page(t_raw, sn_raw):
            dropped.append({"title": t_raw, "host": host,
                            "reason": "词义/义项页（非歌曲）"})
            continue
        if song:
            if not song_identity(blob_raw, title, artist):
                dropped.append({'title':t_raw,'host':host,'reason':'作品身份不足：须同时核对歌名与歌手'})
                continue
            if nt and not t_hit:
                dropped.append({"title": t_raw, "host": host, "reason": "跑题：没提到这首歌"})
                continue
            # ★ R119-28：**给了歌手就必须命中歌手**（与 `_ctx_relevant` 口径对齐）。
            #   旧写法 `and not w_hit` 是"同名异物"闸的漏洞：错歌的百科/平台页
            #   必然含作品词（专辑/演唱/发行…）⇒ 歌手对不上也被放行。
            #   真机取证：《After Dark》-Mr.Kitty 注入了 Aimer 的同名专辑页；
            #   《Sorrow》-Bad Blood 注入了 Sbazzo 的《Sorrow》；《Love Is A Cowboy》
            #   -Kelsea Ballerini 注入了蔡依林的《Love Love Love》。模型拿别人的
            #   事实写当前歌 → 只能是泛化意象/模板腔（用户报障的直接成因）。
            #   `_norm_entity` 已含别名归一，译名（Queen↔皇后乐队）不会误伤。
            #   ★ R119-28c 收窄：判据从"没提到本人"改为"**点名了别人**"——
            #     不含任何歌手信息的合法简介片段（`《X》是一首流行歌曲。`）不再误杀。
            if na and _names_other_artist(blob_raw, na):
                dropped.append({"title": t_raw, "host": host,
                                "reason": "同名异物：唱的是别人（讲的是别的歌手的歌）"})
                continue
        # ── 闸 1.4（2026-11-17）：已知脏域名**不进弱来源档** ──────────────
        #   R119-26 点名要挡的就是这些（腾讯视频 / bilibili / 抖音 / 下载站 / 微博）；
        #   弱来源档只收"域名不认识但内容像样"的，不收"已知是视频和下载站"的。
        if song and _gate_on("DOMAIN") and any(b in (u_raw or "").lower() for b in _DOM_BAD):
            dropped.append({"title": t_raw, "host": host,
                            "reason": "脏来源（视频/下载/社交站）"})
            continue
        # ── 闸 1.5：来源分档（**只对歌曲查询成立**，见 `_DOM_WHITELIST` 注）──
        #   排在作品锚**之后**：先用内容判据（跑题 / 同名异物 / 词义页）定性，
        #   再用来源判据定性 ⇒ 丢弃理由才说得清是"内容不对"还是"来路不正"。
        #   （实测反序时 oi-wiki 的理由变成"非权威来源"，把真正的问题
        #     "它是另一个同名事物"掩盖了，回归断言 A2 / A5 因此转红。）
        #   腾讯视频 / bilibili / 知乎 / 百度知道 / 不知名歌词站一律丢弃 —— 它们要么
        #   在 `_DOM_BAD` 里、要么过不了后面的歌词闸，这条口径**没变**。
        #   ★ 2026-11-17：不在白名单的**不再直接丢**，改打〔未核实〕降档（见 `_WEAK_TAG` 注）。
        _weak = False
        if song and _gate_on("DOMAIN") and not _is_whitelisted(host):
            if _weak_on():
                _weak = True
            else:
                dropped.append({"title": t_raw, "host": host,
                                "reason": "非权威来源（不在白名单）"})
                continue
        # ── 闸 2：歌词页（同样只对歌曲查询成立） ──────────────────────
        #   ⚠ 「歌手对不上就丢」**只对标题/域名明确标注歌词页的条目生效**：
        #     靠启发式（`_lyric_run_start`）判出来的歌词页**只剥体、不丢**。
        #     理由：外国歌手的**中文译名**会让 `a_hit` 为假（`Queen` → `皇后乐队`），
        #     那不是翻唱而是译名；对普通作品页一律丢会把最该留的百科条目误杀（实测）。
        marked_lyrics = _has_lyric_mark(t_raw, u_raw) if song else False
        lyrics = marked_lyrics or (song and _lyric_run_start(sn_raw) >= 0)
        if lyrics:
            if marked_lyrics and na and not a_hit:
                dropped.append({"title": t_raw, "host": host,
                                "reason": "明确歌词页且不是本人唱（多半是翻唱版本）"})
                continue
            sn_new = strip_lyric_body(sn_raw)
            if len(sn_new) < 12:
                dropped.append({"title": t_raw, "host": host,
                                "reason": "纯歌词页：剥掉歌词体后无任何事实"})
                continue
        else:
            sn_new = sn_raw
        # ── 闸 4：去重 ────────────────────────────────────────────────
        key = (host, _norm_entity(t_raw)[:16])
        if key in seen:
            dropped.append({"title": t_raw, "host": host, "reason": "重复（同域名同题）"})
            continue
        seen.add(key)
        # ── 闸 3：事实密度（不丢，只排序用） ─────────────────────────
        sn_new = _cut_sentence(sn_new, per_cap)
        cands.append({"r": r, "title": t_raw, "host": host, "snippet": sn_new,
                      "density": _fact_density(sn_new), "scene": _scene_score(sn_new),
                      "lyrics": lyrics, "weak": _weak,
                      "reason": ("歌词页（已剥歌词体，保留事实）" if lyrics else "保留")
                                + ("｜弱来源" if _weak else "")})

    if not cands and results:                    # fail-open：仅在确有歌曲页时才兜底
        # ★ R119-26：兜底**也必须守白名单** —— 否则刚被闸 1.5 剔掉的脏源会被
        #   兜底原样捞回来（实测：A3c 的「百度知道」歌词页就是这么回来的，
        #   白名单闸形同虚设）。
        # ★ 2026-11-17：弱来源档打开时，兜底池从"只认白名单"放宽成"排除脏域名"——
        #   否则前面刚保住的〔未核实〕条目会被这里二次清空，分档等于白做。
        _pool = results
        if song and _gate_on("DOMAIN"):
            if _weak_on():
                _pool = [r for r in results
                         if not any(b in (r.url or "").lower() for b in _DOM_BAD)]
            else:
                _pool = [r for r in results if _is_whitelisted(_host_of(r.url))]
        _pick = _first_song_like(_pool, nt, na)
        if _pick is not None:
            r0 = _pick
            sn0 = _cut_sentence(_clean_snippet(_clean_snippet(r0.snippet)), per_cap)
            cands = [{"r": r0, "title": r0.title or "", "host": "", "snippet": sn0,
                      "density": _fact_density(sn0), "scene": _scene_score(sn0),
                      "lyrics": False,
                      "weak": bool(song and _gate_on("DOMAIN") and _weak_on()
                                   and not _is_whitelisted(_host_of(r0.url))),
                      "reason": "fail-open 兜底(确为歌曲页)"}]
        # 否则 cands 保持空 -> 返回空文本：宁可无事实，不可错事实

    # ── 闸 4（近似）：3-gram Jaccard ≥ 0.6 的近重复只留一条（仅歌曲查询） ──
    cands = _dedup_approx(cands, song=song)
    # ── 闸 5：MMR 多样性（歌曲=事实密度 + 同域≤2；环境=在地意象） ──────
    if song:
        kept = _mmr_greedy(cands, base_key="density")
        # ── 闸 6：lost-in-the-middle 首尾排序（≥3 条时，第 2 名放末尾） ──
        #   仅歌曲查询启用：环境查询的"政府公告排在城市百科之后"是独立硬约束
        #   （见 verify_search_curation A16），首尾重排会把它破坏，故环境查询不重排。
        kept = _head_tail(kept)
    else:
        kept = sorted(cands, key=lambda k: (-k["scene"], -k["density"]))

    # ── 闸 7（2026-11-17）：**配额**——权威条目永远在前，弱来源只在资料薄时补位 ──
    #   ★ 判据是"**内容级**条目够不够 3 条"，不是"白名单条目够不够"：平台页
    #     （y.qq / apple music / spotify，权重 3）和歌词库（mojim / genius，权重 2）
    #     虽然"权威"，但只报曲目信息、不提供创作背景。实测《Landslide》4 条全是
    #     Apple Music / QQ音乐MV / 网易云单曲（459 字几乎全是样板），而真正讲背景的
    #     知乎专栏 / discogs 被白名单挡在外面 —— 只数条数会让这种"看起来很满、
    #     其实什么也没说"的资料把弱来源配额吃光。故只数权重 ≥ 4 的（百科/乐评/豆瓣）。
    if song and any(k.get("weak") for k in kept):
        _strong_l = [k for k in kept if not k.get("weak")]
        _weak_l = [k for k in kept if k.get("weak")]
        _rich = sum(1 for k in _strong_l if _dom_weight(k["host"]) >= 4)
        _quota = 0 if not _weak_on() else max(0, min(_WEAK_MAX, _WEAK_FILL - _rich))
        kept = _strong_l + _weak_l[:_quota]
        for k in _weak_l[_quota:]:
            dropped.append({"title": k["title"], "host": k["host"],
                            "reason": "弱来源超额（内容级资料已够 %d 条）" % _rich})

    lines: list[str] = []
    used = 0
    for i, k in enumerate(kept, 1):
        # ★ 标题行**不带 URL**：实测 4 条百度百科链接（百分号编码）吃掉 300~400 字，
        #   而提示词本来就要求"不要罗列来源" ⇒ 省下的预算全给事实。
        # ★ 2026-11-17：〔未核实〕**紧贴 `[n]`**（`[2]〔未核实〕标题`）—— brain.py 用
        #   `^\[\d+\]〔未核实〕` 精确数弱来源条数，中间多一个空格就数不到（实测踩过）。
        block = "[%d]%s %s" % (i, _WEAK_TAG if k.get("weak") else "", k["title"])
        if k["snippet"]:
            block += "\n    " + k["snippet"]
        if used + len(block) + 1 > budget_chars and lines:
            break
        lines.append(block)
        used += len(block) + 1
    text = "\n".join(lines)
    stats = {"kept": [{"title": k["title"], "host": k["host"], "reason": k["reason"],
                       "weak": bool(k.get("weak")),
                       "chars": len(k["snippet"]), "density": k["density"],
                       "scene": k["scene"]} for k in kept],
             "weak_n": sum(1 for k in kept if k.get("weak")),
             "dropped": dropped, "chars": len(text)}
    logger.info("[检索筛选] 保留 %d（弱来源 %d）/ 丢弃 %d（%d 字）：%s",
                len(stats["kept"]), stats["weak_n"], len(dropped), len(text),
                "；".join("%s→%s" % (d["title"][:16], d["reason"]) for d in dropped[:4]))
    return text, stats


def _clean_snippet(s: str) -> str:
    """去掉站点样板尾巴与多余空白（"海量高清MV在线看…"、连续空格、"…" 结尾）。"""
    t = " ".join((s or "").split())
    for junk in ("千万正版音乐海量无损", "海量高清MV在线看", "下载，《", "在线试听，更多相关歌"):
        i = t.find(junk)
        # ★ 判据从"样板位置 > 20 字"改为"截断后仍剩 >= 12 字" —— 前者是拍脑袋的
        #   绝对位置，短摘要里永远不触发（verify_search_quality N11 把它钉出来了）；
        #   后者只保护"别把整条摘要剃光"，与摘要长度无关，语义更干净。
        if i >= 0 and len(t[:i].strip()) >= 12:
            t = t[:i].rstrip(" ，。;；、")
    return t

# 联网检索硬超时（秒）：ddgs 在慢网络/跨区域下首轮往返可能 ~10s，且它"忽略"总超时、
# 会按 DDGS_TIMEOUT 逐轮重试——所以这里分两层：
#   - DDGS_TIMEOUT：传给 ddgs 的"每轮"超时（小一点，让它尽早重试而非单次干等）。
#   - SEARCH_TIMEOUT：本模块外层硬上限（守护线程 join = SEARCH_TIMEOUT+3），保证
#     search() 最多阻塞这么久即返回（失败/超时回退空结果，不影响口播生成）。
# 该检索在口播生成的后台线程中【同步】进行，且按 web_search_interval 节流；超时过长会
# 直接挤占"预取口播"的时间预算（实时 DJ 要求口播在上一首歌时长内算完）。故此处刻意收紧：
# 慢网络下宁可快速失败（回退无联网资料），也不要为一首歌卡 12s。
DDGS_TIMEOUT = 4
SEARCH_TIMEOUT = 5

# 浏览器搜索服务：断连/503 时的客户端自愈参数。
# 服务偶发「与页面的连接已断开」会统一返 503；此处做：同引擎退避重试一次 +
# 自动切换备选引擎（bing↔360），尽量让 DJ 口播拿到联网资料而非直接降级为空。
BROWSER_RETRY = 2          # 每个引擎最多尝试次数
BROWSER_BACKOFF = 1.0      # 503/异常后退避秒数

# ★★★ R119-4u：**检索熔断 + 单次调用的墙钟总预算**（用户真机日志钉死）。
#   为什么必须加：检索跑在「口播生成」的关键路径上（`web_search=true` ⇒ 每首口播都要
#   先联网），而服务卡死时旧实现每次最坏 **2 引擎 × 2 次 × 20s + 退避 ≈ 82 秒**
#   ⇒ 导听口播永远赶不上播放窗口（实测 21:14:03 一次爆 60+ 条 `read timeout=20`）
#   ⇒ 只剩迟到兜底句。口播"带联网资料"是加分项、不是前提，故宁可快速失败。
#   · BROWSER_TOTAL_BUDGET_S：**单次 search() 的总上限**，到点就收工（含所有引擎/重试）；
#   · 连续失败 BROWSER_BREAK_AFTER 次 → 打开熔断，冷却期 BROWSER_COOLDOWN_S 内直接返回空。
# ★ 2026-09-21：6.0 → **12.0**。理由：服务端默认引擎已改成 `chain`
#   （bing_rss → bing → 360），当第一跳 RSS 被限流时，服务端会落到**浏览器跳**
#   —— 那一跳自身要付 `RATE_LIMIT_SECONDS=3s` 排队 + 1.4~5s 页面加载，合计可达 ~8s。
#   若客户端预算仍是 6s，会在服务端正干活时先放弃 ⇒ 拿回空结果 ⇒ 连错 2 次熔断 120s
#   ⇒ 又回到"口播连着几首零事实"。放宽到 **8s**（够付 3s 排队 + ~5s 加载）
#   让这条降级出路**真的可达**；又不把"服务死了"的最坏等待拉得太长（旧实现是 82s）。
#   常态不受影响：chain 的第一跳实测 0.31~0.35s，命中缓存则 0.001s。
BROWSER_TOTAL_BUDGET_S = 8.0   # 可用 MUSICAI_SEARCH_BUDGET_S 覆盖
BROWSER_BREAK_AFTER = 2        # 连续失败多少次 → 熔断
BROWSER_COOLDOWN_S = 120.0     # 熔断冷却时长（秒）
_CB_LOCK = threading.Lock()
_CB = {"fails": 0, "until": 0.0, "opened": 0}

# ═══════════════════════════════════════════════════════════════════════════════
# ★★★ 2026-09-21：**检索缓存**（治"生产不像考试那样有资料可用"）
# 为什么要有：考试件对 60 个专名**各检索一次并缓存、全模型复用** ⇒ 资料恒在场；
#   生产每次实时检索，而实时检索有三重夹击（6s 总预算 / 连续失败 2 次熔断 120s /
#   第一首不联网）⇒ 口播经常在"零事实"下生成，看起来就是"不积极调搜索"。
# 设计：
#   · 键 = 提供方 + 引擎 + max_results + 查询（换引擎即失效，保守）；
#   · **存已格式化文本**（注入什么就存什么，命中后完全等价）；
#   · `LIVE` TTL 内视为新鲜（直接返回）；`STALE` 内只作**失败/熔断时的兜底**；
#   · 落盘 + 原子替换 + 容量上限（丢最旧），重启不丢。
# ═══════════════════════════════════════════════════════════════════════════════
def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except Exception:                                       # noqa
        return float(default)


SEARCH_CACHE_TTL_S = _envf("MUSICAI_SEARCH_CACHE_TTL_S", 7 * 24 * 3600.0)   # 新鲜期
SEARCH_CACHE_STALE_S = _envf("MUSICAI_SEARCH_STALE_OK_S", 30 * 24 * 3600.0)  # 可兜底期
SEARCH_CACHE_MAX = int(_envf("MUSICAI_SEARCH_CACHE_MAX", 500))
SEARCH_CACHE_PATH = DATA_DIR / "search_cache.json"

_CC_LOCK = threading.Lock()
_CC: dict | None = None
_CC_DIRTY = [0]


# ★★ 2026-09-21：**缓存版本号** —— 改动结果筛/排版规则时**必须 +1**，否则旧缓存
#   会在 TTL（7 天）内继续把"改之前的文本"喂给口播。实测踩到：改了 `rank_and_clean`
#   之后端到端冒烟仍然"含噪声"，原因就是命中了**改之前**写下的缓存。
# ★★★ 检索**策略版本**：只要改了任何会影响检索结果的东西，**必须 bump 这个号** ——
#   查询变体（`_variants`）、筛选闸（`curate_material` / `song_identity` / 白名单）、
#   在地检索词、脱敏逻辑…… 否则**旧缓存会继续命中，新逻辑"改了等于没改"**。
#
#   2026-09-23 实测踩坑（用户提出）：改完「体验式查询词」重跑，输出一字不差 ——
#   查了半天才发现是 `search_cache.json` 在用旧结果喂它。
#
#   ★ 为什么用**版本号**而不是「每次重启清空缓存」（用户提的另一个方案）：
#     清空会把**仍然有效**的热缓存一起丢掉 ⇒ 首次播放的检索延迟从 ~0 退回 ~14s；
#     版本号只让**受影响**的那批失效，精准，而且不需要人记得去清。
#     ⇒ 只在新策略**确实**改变结果时才 bump（改注释、改日志不算）。
SEARCH_CACHE_VER = "11"     # v8=R119-27 · v9=2026-09-23 体验式查询词（歌曲 3 路 + 在地 4 路全换）
                            #   + L0 别名表（输出顺序与去重都变了）⇒ +1 让旧缓存立即失效。
                            #   检索**输出内容**变了 ⇒ +1 让旧缓存立即失效。
                            #   ★ v10 = 2026-11-17 弱来源分档：注入文本新增 `[n]〔未核实〕`
                            #   前缀、且旧缓存是在"硬剔"口径下算的（压根没有弱来源条目）
                            #   ⇒ 必须失效，否则新旧口径混着用，A/B 对照根本做不了。
                            #   ★ v11 = 2026-11-17 服务端改成多引擎并发合并（360+搜狗+RSS）：
                            #   同一条查询拿到的结果集与排序**整体变了**，旧缓存是"Bing 丢词"
                            #   那套垃圾（水星记→水星网络），必须一起失效。


# ★ 2026-09-23：繁→简映射（够用于维基/百科标题去重的常见差异字；零依赖）
_T2S_MAP = {
    "專": "专", "輯": "辑", "維": "维", "葉": "叶", "華": "华", "進": "进", "樂": "乐",
    "團": "团", "舊": "旧", "時": "时", "間": "间", "語": "语", "詞": "词", "聲": "声",
    "風": "风", "雲": "云", "門": "门", "開": "开", "關": "关", "聽": "听", "見": "见",
    "說": "说", "話": "话", "這": "这", "個": "个", "們": "们", "來": "来", "後": "后",
    "會": "会", "對": "对", "應": "应", "實": "实", "體": "体", "點": "点", "為": "为",
    "與": "与", "還": "还", "過": "过", "動": "动", "業": "业", "發": "发", "現": "现",
    "經": "经", "總": "总", "將": "将", "當": "当", "裡": "里", "麼": "么", "樣": "样",
    "頭": "头", "條": "条", "從": "从", "眾": "众", "產": "产", "東": "东", "種": "种",
    "兩": "两", "邊": "边", "際": "际", "變": "变", "給": "给", "約": "约", "統": "统",
    "處": "处", "領": "领", "導": "导", "員": "员", "區": "区", "單": "单", "據": "据",
    "認": "认", "讓": "让", "論": "论", "計": "计", "設": "设", "備": "备", "熱": "热",
    "態": "态", "決": "决", "權": "权", "護": "护", "環": "环", "壓": "压", "戲": "戏",
    "劇": "剧", "愛": "爱", "戀": "恋", "擁": "拥", "懷": "怀", "憶": "忆", "驚": "惊",
    "題": "题", "願": "愿", "顏": "颜", "顯": "显", "類": "类", "飛": "飞", "飯": "饭",
    "館": "馆", "馬": "马", "驗": "验", "鳥": "鸟", "鳴": "鸣", "黃": "黄", "齡": "龄",
    "選": "选", "釋": "释", "錄": "录", "銷": "销", "獎": "奖", "獨": "独", "獸": "兽",
    "獻": "献", "節": "节", "築": "筑", "續": "续", "纖": "纤", "義": "义", "習": "习",
    "職": "职", "聯": "联", "聲": "声", "聽": "听", "膚": "肤", "臺": "台", "歲": "岁",
}
_T2S_TABLE = str.maketrans(_T2S_MAP)


def _dedup_key(title: str) -> str:
    """标题的繁简归一化去重键（同一条目简繁两版会算出同一个 key）。"""
    if not title:
        return ""
    s = title.translate(_T2S_TABLE)
    s = re.sub(r"\s+", "", s)
    return s[:30]


def _cache_key(provider, query: str, max_results: int) -> str:
    eng = getattr(provider, "engine", "") or ""
    return "v%s|%s|%s|%d|%s" % (SEARCH_CACHE_VER, type(provider).__name__, eng,
                                int(max_results), (query or "").strip())


def _cache_load() -> dict:
    global _CC
    if _CC is not None:
        return _CC
    data: dict = {}
    try:
        if SEARCH_CACHE_PATH.exists():
            raw = json.loads(SEARCH_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items()
                        if isinstance(v, dict) and isinstance(v.get("text"), str)}
    except Exception as e:                                  # noqa 缓存坏了就当空
        logger.warning("[检索缓存] 读取失败（忽略）: %s", e)
        data = {}
    _CC = data
    return _CC


def _cache_save() -> None:
    if not _CC_DIRTY[0]:
        return
    _CC_DIRTY[0] = 0
    try:
        d = _cache_load()
        if len(d) > SEARCH_CACHE_MAX:                       # 丢最旧
            for k in sorted(d, key=lambda k: d[k].get("t", 0))[:len(d) - SEARCH_CACHE_MAX]:
                d.pop(k, None)
        SEARCH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(SEARCH_CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, SEARCH_CACHE_PATH)                  # 原子发布
    except Exception as e:                                  # noqa 缓存写失败绝不影响主流程
        logger.debug("[检索缓存] 写入失败: %s", e)


def cache_get(key: str, max_age_s: float) -> str | None:
    """取缓存文本；不存在 / 超出 `max_age_s` → None。"""
    with _CC_LOCK:
        d = _cache_load()
        it = d.get(key)
        if not isinstance(it, dict):
            return None
        if (time.time() - float(it.get("t") or 0)) > float(max_age_s):
            return None
        return it.get("text") or None


def cache_put(key: str, text: str) -> None:
    if not text:
        return
    with _CC_LOCK:
        d = _cache_load()
        d[key] = {"t": time.time(), "text": text, "n": len(text)}
        _CC_DIRTY[0] = 1
        _cache_save()


def cache_stats() -> dict:
    with _CC_LOCK:
        d = _cache_load()
    now = time.time()
    fresh = sum(1 for v in d.values() if now - float(v.get("t") or 0) <= SEARCH_CACHE_TTL_S)
    return {"n": len(d), "fresh": fresh, "path": str(SEARCH_CACHE_PATH),
            "ttl_s": SEARCH_CACHE_TTL_S, "stale_s": SEARCH_CACHE_STALE_S,
            "hits": _CC_HITS[0], "miss": _CC_MISS[0], "warm": _CC_WARM[0]}


_CC_HITS = [0]
_CC_MISS = [0]
_CC_WARM = [0]
# ★ R119-16：本进程内**已试过正文回捞**的查询。为什么要它：如果这一首根本
#   没有可回捞的条目（全被 L1 判成非条目/异物），正文块标记就永远写不进缓存
#   ⇒ 只看"摘要新鲜"会让我们**每次换曲都白试一遍**（5~12 s 的背景机时）。
_PAGE_TRIED: set = set()


def warm_query(title: str, artist: str = "", max_results: int = 5,
               page_fetch: str = "live") -> str:
    """★ 预热一条查询（后台用）：**只写缓存**，返回值仅作调试。

    查询式与 `ai/brain.py::_search_query` 保持**完全一致**（"歌名 歌手"）——
    否则预热的键和取用的键对不上，等于没预热。
    """
    q = ("%s %s" % (title or "", artist or "")).strip()
    if not q:
        return ""
    try:
        p = get_search_provider()
        txt = p.format_context(q, max_results, page_fetch=page_fetch,
                               artist=artist or "", title=title or "")
        if txt:
            _CC_WARM[0] += 1
        return txt
    except Exception as e:                                  # noqa
        logger.debug("[检索预热] 失败(%s): %s", q[:30], e)
        return ""


def prewarm_upcoming(tracks, n: int = 3, max_results: int = 5) -> int:
    """★ 预热"接下来 n 首"的检索资料（背景线程里调；**已经缓存新鲜的直接跳过**）。

    `tracks`：可迭代的 `(title, artist)` 或带 `.title/.artist` 的对象。
    返回**实际发起**的条数。
    """
    if os.environ.get("MUSICAI_SEARCH_WARM", "1") == "0":
        return 0
    with _CB_LOCK:
        if time.monotonic() < _CB["until"]:
            return 0                                        # 熔断期不折腾服务
    done = 0
    for t in list(tracks)[:max(0, int(n))]:
        try:
            if isinstance(t, (tuple, list)):
                title, artist = (t[0], t[1] if len(t) > 1 else "")
            else:
                title = getattr(t, "title", "") or ""
                artist = getattr(t, "artist", "") or ""
        except Exception:                                   # noqa
            continue
        q = ("%s %s" % (title, artist)).strip()
        if not q:
            continue
        try:
            p = get_search_provider()
            # ★ R119-16：**必须连正文一起判**。只看"摘要新鲜"会永远跳过，
            #   于是正文缓存永远补不上（摘要先落盘、正文后到，是两次写入）。
            _fresh = cache_get(_cache_key(p, q, max_results), SEARCH_CACHE_TTL_S)
            if _fresh and (PAGE_BLOCK_MARK in _fresh or q in _PAGE_TRIED):
                continue
        except Exception:                                   # noqa
            pass
        warm_query(title, artist, max_results)
        if len(_PAGE_TRIED) < 300:
            _PAGE_TRIED.add(q)              # 记下来：本进程别再重复试
        done += 1
    return done


@dataclass
class SearchResult:
    title: str
    snippet: str
    url: str = ""
    source: str = ""           # 来源标识：duckduckgo / local_kb
    artist: str = ""
    meta: dict = field(default_factory=dict)


# ★★★ R119-16：**词条正文块**（"正文回捞"的注入形态）
#
# 实测（真机）：RSS 摘要 107~124 字，而回捞到的词条正文 1857~4835 字（厚 15~40 倍），
# 且拿到的正是"创作背景 / 创作历程"——用户抱怨"汇总内容非常少"的那一块。
#
# ⚠ 分两条路（**这是本设计的关键**）：
#   `cache` —— 口播关键路径：只读预热线程填好的缓存，**零网络**；
#   `live`  —— 预热路径：真回捞（一次 5~6 s），填缓存。
#   把回捞放在关键路径上，就是 R119-9 / R119-13 那条"每做一次就卡一次"的重演。
PAGE_BLOCK_MARK = "【词条正文】"


def _with_page_block(text: str, results: list, title: str, artist: str, mode: str) -> str:
    """把"词条正文"块追加到摘要文本之后。`mode` ∈ {off, cache, live}。"""
    if mode not in ("cache", "live") or not results:
        return text
    if os.environ.get("MUSICAI_PAGE_FETCH", "1") == "0":
        return text
    try:
        from ai import page_fetch as _PF
        blk, st = _PF.enrich_block(results, title=title, artist=artist,
                                   allow_live=(mode == "live"))
    except Exception as e:                                        # noqa
        logger.info("[正文回捞] 跳过：%s", str(e)[:100])
        return text
    if not blk:
        return text
    logger.info("[正文回捞] 追加 %d 字：%s", st.get("chars") or 0,
                "；".join("%s(%s)%d字" % (p["title"][:12], p["kind"], p["chars"])
                          for p in (st.get("picked") or [])[:3]))
    return ((text + "\n\n") if text else "") + PAGE_BLOCK_MARK + "\n" + blk


class SearchProvider(ABC):
    """统一检索接口。所有实现必须保证「每轮上下文隔离」。"""

    # 子类可声明是否联网（用于 UI/日志）
    is_online: bool = False

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """执行一次检索，返回独立、无上一轮残留的结果列表。"""

    def clear_context(self) -> None:
        """清理上一轮搜索上下文。默认无状态；子类可重写。"""
        self._last_query = ""
        self._last_context = None

    def format_related_context(self, title, artist, album, allow_live=True):
        """Artist background is a separate evidence scope, never a song fact."""
        from ai.material_guard import contains_entity
        if not artist or not album:
            return ''
        key = _cache_key(self, f'artist-background:{artist}|{album}', 2)
        cached = cache_get(key, SEARCH_CACHE_TTL_S)
        if cached is not None:
            return cached
        if not allow_live:
            return ''
        main = primary_artist(artist)
        rows = self.search(f'"{main}" "{album}"', 8)
        kept = []
        for row in rank_and_clean(rows, main):
            text = f'{row.title} {row.snippet}'
            # Album co-occurrence disambiguates names such as Dabin / DPR Live.
            if not contains_entity(text, main) or not contains_entity(text, album):
                continue
            if not re.search(r'音乐|歌手|制作|乐队|专辑|\bartist\b|\bmusician\b|\balbum\b', text, re.I):
                continue
            kept.append(f'艺人背景（不代表本曲编曲、歌词或创作缘由）：{row.title}\n{row.snippet[:500]}\n来源：{row.url}')
            if len(kept) == 2:
                break
        result = '\n'.join(kept)
        if result:
            cache_put(key, result)
        return result

    def format_context(self, query: str, max_results: int = 6,
                       allow_live: bool = True, artist: str = "",
                       song: bool = True, page_fetch: str = "cache",
                       title: str = "", album: str = "") -> str:
        actual_title = title or (query.rsplit(artist, 1)[0].strip() if artist and query.endswith(artist) else query.split()[0] if query.split() else '')
        actual_artist = artist or (' '.join(query.split()[1:]) if not title else '')
        text = self._format_context_impl(query, max_results, allow_live, actual_artist,
                                         song, page_fetch, actual_title, album)
        if song:
            return verified_context(text, actual_title, actual_artist, album)
        return environment_context(text, actual_title)

    def _format_context_impl(self, query: str, max_results: int = 6,
                             allow_live: bool = True, artist: str = "",
                             song: bool = True, page_fetch: str = "cache",
                             title: str = "", album: str = "") -> str:
        """把检索结果拼成可用于注入 DJ 提示词的文本块。

        ★ 2026-09-21：`allow_live=False` ⇒ **只读缓存**（新鲜或陈旧都行），绝不发网络请求。
        用途：第一首（`position == 1`）保持"开场白不联网"的既有策略，但仍可**吃缓存**
        （上一轮会话/预热线程已经取回的资料），从而不再"第一首永远没有事实可用"。

        已裁剪：每条 snippet 截断到 SNIPPET_CAP，总量限制在 CTX_BUDGET 以内——
        本地小模型（Ollama qwen3.5:4b）提示词过长会显著变慢，且长 snippet 多为
        噪声；保留要点即可丰富口播，不拖慢生成。
        """
        # ★ R119-27/P1：多查询扇出 + URL 去重合并。Bing 一见「歌名 歌手」就丢后词
        #   （「高级动物 窦唯」→"高级"；「Lemon Tree Fool's Garden」→"Lemon"），
        #   单发「只歌名 / 歌手+歌名」反而干净命中真歌页。三路扇出合并成一个更大的
        #   候选池，再走与 `_format_core` 同款的 rank_and_clean + curate_material
        #   （白名单 / 相关性 / 错歌熔断全部不变）。
        _artist = (artist or "").strip()
        _parts0 = (query or "").split()
        _title = (title or "").strip() or (_parts0[0] if _parts0 else "")
        if not _artist and len(_parts0) > 1:
            _artist = " ".join(_parts0[1:])
        if not song or not allow_live:          # 在地/环境检索维持原行为
            return self._format_core(query, max_results, allow_live, _artist, song,
                                     page_fetch, _title)
        _variants = []
        if _artist:
            _main_artist = primary_artist(_artist)
            _variants.append(f'"{_title}" "{_main_artist}"')
            # ★★ 2026-09-23 实测（用户提出「搜索后面该搭配什么词」）：
            #   `"歌名" "歌手"` 这类查询回来的是**定义页**（发行年 / 专辑 / 榜单），
            #   实测定义率 2.1/100 字、感官词只有 0.2/100 字 —— 事实有了、**画面没有**。
            #   这正是口播写不满、只好拿城市凑的深层原因（用户观察："返回的并不少，
            #   但没用上"—— 其实是**回来的是定义不是画面**）。
            #   加"听感 / 氛围 / 场景"：定义率 2.1 → 1.2、感官词 0.2 → 1.1；
            #   加"听这首歌 / 想起 / 感觉"：定义率进一步逼近 0（Lemon 实测 0.0）。
            #   ⇒ 净增 1 路：砍掉价值最低、最易串歌的「单歌名」路（Lemon Tree→Lemon 那类）。
            _variants.append(f'{_title} {_main_artist} 听感 氛围 场景')
            _variants.append(f'{_title} {_main_artist} 听这首歌 想起 感觉')
        if _title and not _artist:
            _variants.append(_title)
        _seen_v = set(); variants = []
        for v in _variants:
            if v and v not in _seen_v:
                _seen_v.add(v); variants.append(v)
        if not variants:
            variants = [query.strip()]
        text = self._format_fanout(variants, cache_as=query, max_results=max_results,
                                   allow_live=allow_live, artist=_artist, song=song,
                                   page_fetch=page_fetch, title=_title)
        if _ctx_relevant(text, _title, _artist):
            return text
        # A nonempty RSS response can still contain only homonyms. In that case
        # the service's transport-only chain never reaches its alternate engine.
        if hasattr(self, 'search_alternative'):
            alternatives = self.search_alternative(f'"{_title}" "{primary_artist(_artist)}"', max_results * 2)
            alternatives = [r for r in alternatives if song_identity(f'{r.title} {r.snippet}', _title, _artist)]
            alternate_text, _ = curate_material(rank_and_clean(alternatives,query)[:max_results],
                                                title=_title,artist=_artist,song=True,
                                                per_cap=420,budget_chars=1800)
            alternate_text = verified_context(alternate_text,_title,_artist,album)
            if alternate_text:
                cache_put(_cache_key(self,query,max_results),alternate_text)
                return alternate_text
        # ── 路 3：英文市场（mkt=en-US）─────────────────────────────────
        #   ★ R119-26：中文市场下 7 首外文歌全部 0 条（Holocene / Skinny Love /
        #     The Night We Met / Kyoto / Apocalypse / 罪と罰 / 茎），中文库里没有条目。
        #   只在**扇出都没拿到**时才走 ⇒ 中文歌零额外开销（多 1 跳 ≈ 0.3s）。
        if _title:
            try:
                from ai.bing_rss import search_rss
                _q3 = ("%s %s" % (_title, _artist)).strip()
                _rows = search_rss(_q3, timeout=6.0, mkt="en-US")
            except Exception:                                  # noqa
                _rows = []
            if _rows:
                _rs3 = [SearchResult(title=t, snippet=s, url=u, source="rss-en")
                        for t, u, s in _rows]
                _rk3 = rank_and_clean(_rs3, _q3)[:max(1, int(max_results))]
                _t3, _ = curate_material(_rk3, title=_title, artist=_artist,
                                         per_cap=420, budget_chars=3000, song=True)
                if _ctx_relevant(_t3, _title, _artist):
                    cache_put(_cache_key(self, query, max_results), _t3)
                    return _t3
        # ── 路 3.5：MusicBrainz 兜底（R119-27/P2'：免代理可达的英文结构化源）──
        #   Bing RSS 的 mkt=en-US 在本机失效（返回中文义项页被闸丢弃），
        #   Wikipedia 无代理不可达 ⇒ 外文歌 / 零 Bing 素材时，MusicBrainz 是**唯一**
        #   可达的权威英文源（实测 0.75s 返回艺人/专辑/发行年/时长）。
        #   只在「扇出 + en-US 都没拿到」时才走（每首至多 1 次网络，模块内已限速 1/s）。
        if _title and _artist:
            try:
                from ai import musicbrainz as _MB
                _mb = _MB.search_recording(_title, _artist, album=album)
                _mtxt = _MB.format_facts(_mb) if _mb else ""
            except Exception:                                  # noqa
                _mtxt = ""
            # ★★ 2026-09-23：MB 串歌熔断 —— 没有资料好过错误资料。
            #   MB 只会返结构化的"艺人/专辑/年份"，`_ctx_relevant` 的子串匹配挡不住
            #   同名不同人（Perfect ⊂ Mr Perfect），因此这里再过一道艺人严格校验。
            if _mtxt and _mb and not _mb_artist_match((_mb or {}).get("artist", ""), _artist):
                _mtxt = ""
            if _mtxt and _ctx_relevant(_mtxt, _title, _artist):
                cache_put(_cache_key(self, query, max_results), _mtxt)
                return _mtxt
        # ── 路 4：Wikipedia 兜底（R119-27/P2，**不依赖**）──────────────
        #   无代理时本机连不上 Wikipedia → 3s 超时后本会话冷却 10min，静默跳过，
        #   绝不阻塞口播；将来开代理自动生效。只在外文歌/零 Bing 素材时才到这里。
        if _title and _wiki_reachable():
            _wtxt = _wiki_fallback(_title, _artist)
            if _wtxt and _ctx_relevant(_wtxt, _title, _artist):
                cache_put(_cache_key(self, query, max_results), _wtxt)
                return _wtxt
        # ★ 三路都不相关 ⇒ 先回退**陈旧缓存**（A6 落点），仍无才返回空。
        #   ⚠ 陈旧文本未必含歌手/作品词，会被 `_ctx_relevant` 判成"不相关" ⇒
        #     必须在判据之后**显式**再取一次，否则救命资料被判据吞掉（实测 A6 转红）。
        _stale = cache_get(_cache_key(self, query, max_results), SEARCH_CACHE_STALE_S)
        if _stale:
            return _stale
        # 仍无 ⇒ 返回空。旧行为会把不相关的 text 原样返回 ——
        #   那正是"口播念成另一首歌"的入口（Lemon Tree → 米津玄师）。
        return ""

    def _format_core(self, query: str, max_results: int = 6,
                     allow_live: bool = True, artist: str = "",
                     song: bool = True, page_fetch: str = "cache",
                     title: str = "") -> str:
        """format_context 的核心：单条查询的检索→筛选→注入（不含兜底重试）。"""
        # ★★★ 2026-09-21：**缓存优先**（见本文件 `SEARCH_CACHE_TTL_S` 注）。
        #   命中即返回 ⇒ 零网络、零熔断影响；失败时退而喂**陈旧缓存**而不是空串
        #   （上一轮已知的事实远好于"什么都没有"——这正是"口播不再引用检索"的根因之一）。
        _ck = _cache_key(self, query, max_results)
        _hit = cache_get(_ck, SEARCH_CACHE_TTL_S)
        if _hit and (not song or _ctx_relevant(_hit, title, artist)):
            _CC_HITS[0] += 1
            return _hit
        if not allow_live:                     # ★ 只读缓存：陈旧也认，不发网络
            return cache_get(_ck, SEARCH_CACHE_STALE_S) or ""
        _CC_MISS[0] += 1
        # ★ v3：**宽池**（见 `_POOL_MUL` 注）——只放宽 provider 的返回条数，不多发请求。
        # ★ R119-24：候选池倍率**只用于歌曲检索**（fetch many → 挑权威源）。
        #   在地/环境检索（song=False）是背景点缀，放大池子只会把更多「长春百科」
        #   这类带城市名的素材灌进上下文，推高「城市名出现率」，与 R119-21
        #   「城市名尽可能少出现」直接冲突 ⇒ 环境检索不加池（维持 env_top_k 条）。
        _mul = _POOL_MUL if song else 1
        results = self.search(query, max(1, int(max_results) * _mul))
        if not results:
            _stale = cache_get(_ck, SEARCH_CACHE_STALE_S)
            return _stale or ""
        # ★★★ 2026-09-21：**先筛后排再注入**（见 `rank_and_clean` 注）。
        #   为什么必须在这里：`format_context` 是**唯一的注入出口**，而下面按顺序吃
        #   `CTX_BUDGET` 预算 ⇒ 不筛的话噪声会先占满预算、把百科摘要挤掉。
        if song:
            results = [r for r in results if song_identity(f'{r.title} {r.snippet}', title, artist)]
        results = rank_and_clean(results, query)[:max(1, int(max_results))]
        # ★ R119-4g：**素材给足**（用户："很多都来自于素材，搜索素材的预算可以给足一点"）。
        #   实测旧值下：提示词占 85%、素材只占 13% —— 与"内容主要来自素材"完全倒挂。
        #   故 160→220（每条摘要更长）、1400→3000（总量给足）；均可用环境变量现场调。
        # ★ 2026-09-21：220 → **420**。实测百科那条在 220 字处**恰好**截断在
        #   "…收录于周杰伦推出的个人第四张音乐专辑《叶惠美》…" 之后 —— 而后面紧跟的
        #   正是"创作背景"。用户"汇总的内容非常少"有一半就是这么来的。
        # ★★★ 2026-09-21 v4：**筛选层**（作品锚 / 歌词体剥离 / 事实密度 / 去重）。
        #   为什么必须在这里：`format_context` 是**唯一的注入出口**，实测注入文本里
        #   64~79% 是歌词或跑题内容（见 `curate_material` 注）。
        #   歌手名优先用调用方传的（`ai/brain._search_context`）；没传时按
        #   `_search_query` 的既有约定 `{title} {artist}` 从 query 里切。
        _parts = (query or "").split()
        _title = (title or "").strip() or (_parts[0] if _parts else "")
        _artist = (artist or "").strip() or (" ".join(_parts[1:]) if len(_parts) > 1 else "")
        SNIPPET_CAP = int(os.environ.get("MUSICAI_SEARCH_SNIPPET_CAP") or 420)
        CTX_BUDGET = int(os.environ.get("MUSICAI_SEARCH_CTX_BUDGET") or 3000)
        _text, _stats = curate_material(results, title=_title, artist=_artist,
                                       per_cap=SNIPPET_CAP, budget_chars=CTX_BUDGET,
                                       song=song)
        # ★ R119-16：`cache` 模式零网络（默认，热路径）；`live` 模式才真回捞。
        _text = _with_page_block(_text, results, _title, _artist, page_fetch)
        # ★ R119-26：**空结果不落盘**。旧写法无条件写入 ⇒ 一次空检索会被缓存 7 天
        #   （实测主查询「高级动物 窦唯」0.00s 命中空缓存），之后每首都得绕兜底链。
        if _text:
            cache_put(_ck, _text)      # ★ 2026-09-21：命中一次就落盘（下次直接命中）
        return _text

    def _format_fanout(self, queries, *, cache_as: str = "", max_results: int = 6,
                       allow_live: bool = True, artist: str = "", song: bool = True,
                       page_fetch: str = "cache", title: str = "") -> str:
        """★ R119-27/P1：多查询扇出 + URL 去重合并（见 `format_context` 注释）。

        与 `_format_core` 同款管线：缓存优先 → 多查询合并原始结果（按 URL 去重）
        → rank_and_clean → curate_material → 词条正文块。只在 allow_live 时发网络。
        `cache_as`：落盘缓存键用的生产查询（"歌名 歌手"），与各路扇出查询解耦，
        保证与预热线程填的键一致。
        """
        _ck = _cache_key(self, cache_as or (queries[0] if queries else ""), max_results)
        if not allow_live:
            return cache_get(_ck, SEARCH_CACHE_STALE_S) or ""
        _hit = cache_get(_ck, SEARCH_CACHE_TTL_S)
        if _hit and (not song or _ctx_relevant(_hit, title, artist)):
            _CC_HITS[0] += 1
            return _hit
        _CC_MISS[0] += 1
        _mul = _POOL_MUL if song else 1
        seen_urls: set = set()
        seen_titles: set = set()   # ★ 繁简归一化后的标题（抓简繁重复）
        merged: list = []
        for q in queries:
            if not q:
                continue
            try:
                res = self.search(q, max(1, int(max_results) * _mul))
            except Exception:                                   # noqa
                res = []
            for r in (res or []):
                u = (r.url or "").strip()
                if u:
                    if u in seen_urls:
                        continue
                    seen_urls.add(u)
                # ★ 简繁重复：URL 不同但归一化标题相同 ⇒ 丢掉，把名额让给不同素材
                _k = _dedup_key(r.title or "")
                if _k:
                    if _k in seen_titles:
                        continue
                    seen_titles.add(_k)
                merged.append(r)
        if not merged:
            _stale = cache_get(_ck, SEARCH_CACHE_STALE_S)
            return _stale or ""
        if song:
            merged = [r for r in merged if song_identity(f'{r.title} {r.snippet}', title, artist)]
        ranked = rank_and_clean(merged, cache_as or (queries[0] if queries else "")) \
            [:max(1, int(max_results))]
        SNIPPET_CAP = int(os.environ.get("MUSICAI_SEARCH_SNIPPET_CAP") or 420)
        CTX_BUDGET = int(os.environ.get("MUSICAI_SEARCH_CTX_BUDGET") or 3000)
        _text, _stats = curate_material(ranked, title=title, artist=artist,
                                        per_cap=SNIPPET_CAP, budget_chars=CTX_BUDGET,
                                        song=song)
        _text = _with_page_block(_text, ranked, title, artist, page_fetch)
        if _text:
            cache_put(_ck, _text)
        return _text


class OnlineSearchProvider(SearchProvider):
    is_online = True

    def __init__(self) -> None:
        self._last_query = ""
        self._lock = threading.Lock()
        self._ddgs = None
        self._ensure_client()

    def _ensure_client(self):
        if self._ddgs is not None:
            return True
        try:
            # 新版包名为 ddgs，旧版为 duckduckgo_search
            try:
                from ddgs import DDGS  # type: ignore
            except Exception:
                from duckduckgo_search import DDGS  # type: ignore
            self._ddgs = DDGS
            return True
        except Exception as e:  # noqa
            logger.warning("DuckDuckGo 客户端不可用，联网检索降级为空: %s",
                           str(e)[:120])
            self._ddgs = None
            return False

    @property
    def available(self) -> bool:
        return self._ensure_client()

    def clear_context(self) -> None:
        with self._lock:
            self._last_query = ""
            self._last_context = None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.clear_context()  # 每轮清理，避免信息污染
        if not self._ensure_client():
            return []
        try:
            raw = self._safe_ddgs_text(query, max_results)
            out: list[SearchResult] = []
            for item in (raw or []):
                out.append(SearchResult(
                    title=item.get("title", "") or "",
                    snippet=item.get("body", "") or item.get("snippet", "") or "",
                    url=item.get("href", "") or item.get("url", "") or "",
                    source="duckduckgo",
                ))
            logger.info("[检索·联网] %s → %d 条", query[:40], len(out))
            return out
        except Exception as e:  # noqa
            logger.error("DuckDuckGo 检索失败: %s", str(e)[:160])
            return []

    def _safe_ddgs_text(self, query: str, max_results: int) -> list:
        """在守护线程里跑 DuckDuckGo 文本检索，并加硬超时。

        返回原始结果列表；超时/异常时返回 []（由上层回退「无联网资料」，不影响口播）。
        DDG 客户端本身也传 timeout，但部分版本会忽略，故外层再加 join 保险。
        """
        box: dict = {}
        err: dict = {}
        lock = threading.Lock()

        def _worker() -> None:
            try:
                with lock:
                    data = self._ddgs(timeout=DDGS_TIMEOUT).text(
                        query, max_results=max_results, backend="html")
                box["r"] = data
            except Exception as e:  # noqa
                err["e"] = e

        th = threading.Thread(target=timed(_worker, "search_worker"), daemon=True)
        th.start()
        th.join(SEARCH_TIMEOUT + 3)
        if "r" in box:
            return box["r"] or []
        if "e" in err:
            logger.error("DuckDuckGo 检索失败: %s", str(err["e"])[:160])
        else:
            logger.warning("DuckDuckGo 检索超时（%ds），跳过联网资料。",
                           SEARCH_TIMEOUT)
        return []


class OfflineSearchProvider(SearchProvider):
    """本地向量检索：SQLite + sqlite-vec。

    嵌入采用「哈希向量化」（hashing trick）：把中文字符 + 拉丁词映射到固定维度
    浮点向量并 L2 归一化。这是可用的「词汇级」向量检索，无需任何外部模型/网络。
    若要升级为「语义级」检索，只需把 _embed() 换成真实嵌入模型
    （如 bge-small ONNX）——接口不变。
    """

    DIM = 256

    def __init__(self) -> None:
        self._last_query = ""
        self._lock = threading.Lock()
        self._db_path = OFFLINE_KB_DIR / "kb.db"
        self._ready = False
        self._init_db()

    # ---------- 嵌入 ----------
    @staticmethod
    def _tokens(text: str) -> list[str]:
        text = (text or "").lower()
        toks: list[str] = []
        # 拉丁词
        toks += re.findall(r"[a-z0-9]+", text)
        # 中日韩字符逐字（中文按字切更稳）
        toks += re.findall(r"[\u3400-\u9fff\u3040-\u30ff]", text)
        return toks

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for tok in self._tokens(text):
            h = hashlib.md5(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "little") % self.DIM
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    @staticmethod
    def _to_blob(vec: list[float]) -> bytes:
        import struct
        return struct.pack(f"<{len(vec)}f", *vec)

    # ---------- 库初始化 ----------
    def _connect(self):
        """打开一个到知识库的连接，并按需加载 sqlite-vec 扩展。

        sqlite-vec 的 vec0 虚拟表模块是「按连接」注册的：每次新建连接都必须
        重新 sqlite_vec.load(con)，否则该连接使用 vec_docs 时会报
        "no such module: vec0"。这是单元测试从「子串降级」切到「真实向量检索」
        后暴露的关键问题。
        """
        con = self._sqlite3.connect(str(self._db_path))
        if getattr(self, "_vec_available", False):
            try:
                con.enable_load_extension(True)
                import sqlite_vec  # type: ignore
                sqlite_vec.load(con)
                con.enable_load_extension(False)
            except Exception as e:  # noqa
                logger.warning("sqlite-vec 本连接加载失败，向量检索降级: %s",
                               str(e)[:120])
        return con

    def _init_db(self) -> None:
        try:
            import sqlite3
            try:
                import sqlite_vec  # type: ignore
            except Exception as e:  # noqa
                logger.warning("sqlite-vec 未安装，离线检索降级为子串匹配: %s",
                               str(e)[:120])
                self._vec_available = False
            else:
                self._vec_available = True
            self._sqlite3 = sqlite3
            con = self._connect()
            if self._vec_available:
                con.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_docs USING "
                    f"vec0(embedding float[{self.DIM}])"
                )
            con.execute(
                """CREATE TABLE IF NOT EXISTS docs(
                       id INTEGER PRIMARY KEY,
                       title TEXT,
                       artist TEXT,
                       text TEXT,
                       source TEXT)"""
            )
            con.commit()
            con.close()
            self._ready = True
            logger.info("[检索·离线] 知识库就绪：%s", self._db_path)
        except Exception as e:  # noqa
            logger.error("离线知识库初始化失败: %s", str(e)[:160])
            self._ready = False

    @property
    def available(self) -> bool:
        return self._ready

    @property
    def doc_count(self) -> int:
        if not self._ready:
            return 0
        try:
            con = self._connect()
            n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            con.close()
            return int(n)
        except Exception:  # noqa
            return 0

    # ---------- 建库 ----------
    def add_document(self, text: str, title: str = "", artist: str = "",
                     source: str = "local_kb") -> None:
        if not self._ready or not text.strip():
            return
        try:
            con = self._connect()
            cur = con.execute(
                "INSERT INTO docs(title, artist, text, source) VALUES(?,?,?,?)",
                (title, artist, text, source),
            )
            doc_id = cur.lastrowid
            if self._vec_available:
                blob = self._to_blob(self._embed(text))
                con.execute(
                    "INSERT INTO vec_docs(rowid, embedding) VALUES(?, ?)",
                    (doc_id, blob),
                )
            con.commit()
            con.close()
        except Exception as e:  # noqa
            logger.error("离线建库写入失败: %s", str(e)[:160])

    def build_index(self) -> int:
        """从本地歌曲库 + 可选的艺人 Wiki / 歌曲故事文件构建离线索引。

        数据来源（部署规范）：
          - ID3 标签：data/db 中的歌曲元数据（标题/歌手/专辑/风格）
          - 艺人 Wikipedia 离线包：OFFLINE_KB_DIR/artist_wiki.json
          - 歌曲故事数据集：OFFLINE_KB_DIR/song_story.json
        返回新增文档数。
        """
        added = 0
        # 1) 歌曲元数据（ID3）
        try:
            from data.db import Database
            db = Database()
            songs = db.all_songs(limit=20000) if hasattr(db, "all_songs") \
                else db.get_all_songs() if hasattr(db, "get_all_songs") else []
            for s in songs:
                title = getattr(s, "title", None) or (s.get("title") if isinstance(s, dict) else "")
                artist = getattr(s, "artist", None) or (s.get("artist") if isinstance(s, dict) else "")
                if not (title or artist):
                    continue
                genre = getattr(s, "genre", None) or (s.get("genre") if isinstance(s, dict) else "")
                album = getattr(s, "album", None) or (s.get("album") if isinstance(s, dict) else "")
                text = f"{title} - {artist}。专辑：{album}。风格：{genre}。"
                self.add_document(text, title=str(title), artist=str(artist),
                                  source="id3")
                added += 1
        except Exception as e:  # noqa
            logger.warning("从歌曲库构建离线索引失败（可忽略）: %s", str(e)[:120])

        # 2) 艺人 Wiki 离线包
        added += self._load_json_docs("artist_wiki.json", "artist_wiki")
        # 3) 歌曲故事数据集
        added += self._load_json_docs("song_story.json", "song_story")
        logger.info("[检索·离线] 建库完成，新增 %d 条文档", added)
        return added

    def _load_json_docs(self, filename: str, source: str) -> int:
        path = OFFLINE_KB_DIR / filename
        if not path.exists():
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            n = 0
            if isinstance(data, list):
                for item in data:
                    text = item.get("text") or item.get("content") or ""
                    if not text:
                        text = " ".join(f"{k}:{v}" for k, v in item.items())
                    self.add_document(text, title=item.get("title", ""),
                                      artist=item.get("artist", ""), source=source)
                    n += 1
            elif isinstance(data, dict):
                for k, v in data.items():
                    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    self.add_document(text, title=k, source=source)
                    n += 1
            return n
        except Exception as e:  # noqa
            logger.warning("加载 %s 失败: %s", filename, str(e)[:120])
            return 0

    # ---------- 检索 ----------
    def clear_context(self) -> None:
        with self._lock:
            self._last_query = ""
            self._last_context = None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.clear_context()
        if not self._ready:
            return []
        try:
            with self._lock:
                if self._vec_available:
                    return self._search_vec(query, max_results)
                return self._search_substr(query, max_results)
        except Exception as e:  # noqa
            logger.error("离线检索失败: %s", str(e)[:160])
            return []

    def _search_vec(self, query: str, top_k: int) -> list[SearchResult]:
        blob = self._to_blob(self._embed(query))
        con = self._connect()
        # sqlite-vec 要求 KNN 的 LIMIT/k 约束必须直接作用在 vec0 表扫描上，
        # 因此先把最近邻查出来（子查询 + LIMIT），再 JOIN docs 取元数据。
        rows = con.execute(
            "SELECT d.id, d.title, d.artist, d.text, d.source, v.distance "
            "FROM ("
            "  SELECT rowid, distance FROM vec_docs "
            "  WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
            ") v JOIN docs d ON d.id = v.rowid",
            (blob, top_k),
        ).fetchall()
        con.close()
        out = []
        for rid, title, artist, text, source, dist in rows:
            out.append(SearchResult(
                title=title or artist or f"doc#{rid}",
                snippet=(text or "")[:200],
                url="", source=source or "local_kb",
                artist=artist or "",
                meta={"distance": float(dist)},
            ))
        logger.info("[检索·离线] %s → %d 条", query[:40], len(out))
        return out

    def _search_substr(self, query: str, top_k: int) -> list[SearchResult]:
        # sqlite-vec 不可用时的降级：简单子串匹配
        con = self._connect()
        like = f"%{query}%"
        rows = con.execute(
            "SELECT id, title, artist, text, source FROM docs "
            "WHERE text LIKE ? OR title LIKE ? LIMIT ?",
            (like, like, top_k),
        ).fetchall()
        con.close()
        out = []
        for rid, title, artist, text, source in rows:
            out.append(SearchResult(
                title=title or artist or f"doc#{rid}",
                snippet=(text or "")[:200], url="", source=source or "local_kb",
                artist=artist or ""))
        return out


class BrowserSearchProvider(SearchProvider):
    """联网检索：调用本地「浏览器内核搜索服务」（tools/websearch_service）。

    该服务复用本机已安装的 Chrome（无头），经 DrissionPage 抓取必应/360 首页
    自然结果，严格过滤广告/推广；用以替代原 DuckDuckGo 联网工具（更稳定、可抓
    国内版结果）。通过 HTTP 调用，与电台进程解耦——服务未启动时优雅降级为空，
    不影响口播生成。

    启用：在 config.json 设置 search.mode = "browser"（可选 browser_url /
    browser_engine 覆盖默认地址与引擎）。
    """

    is_online = True

    def search_alternative(self, query, max_results=8):
        """One bounded semantic fallback; never loop back to the failed RSS path."""
        import requests
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(f'{self.base_url}/search',
                                       params={'q':query,'engine':'360'},timeout=6)
                response.raise_for_status()
                return [SearchResult(title=r.get('title',''),snippet=r.get('snippet',''),
                                     url=r.get('url',''),source='browser:360')
                        for r in response.json().get('results',[])[:max_results]]
        except Exception as e:
            logger.info('检索相关性兜底跳过：%s',type(e).__name__)
            return []

    def __init__(self, base_url: str | None = None, engine: str | None = None) -> None:
        self._last_query = ""
        self._lock = threading.Lock()
        cfg = user_config.data.get("search", {})
        self.base_url = (base_url or cfg.get("browser_url")
                         or "http://127.0.0.1:8787").rstrip("/")
        # ★ 2026-09-21：默认 `bing` → **`chain`**。`chain` 是**服务端**的降级链
        #   （`bing_rss → bing → 360`，取第一个非空）：客户端发**一次**请求就完成
        #   全链路降级，不必自己一轮轮换引擎、从 6 s 预算里掏时间。
        self.engine = (engine or cfg.get("browser_engine") or "chain").lower()

    @property
    def available(self) -> bool:
        return True

    def clear_context(self) -> None:
        with self._lock:
            self._last_query = ""
            self._last_context = None

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.clear_context()  # 每轮清理，避免信息污染
        try:
            import requests
        except Exception:  # noqa requests 缺失 → 直接降级为空
            return []
        # ★★★ R119-4u：**熔断检查**。冷却期内直接返回空 —— 绝不在这里再等 80 秒。
        now = time.monotonic()
        with _CB_LOCK:
            if now < _CB["until"]:
                return []
        # 主引擎 + 备选引擎：503/断连时自动切换，提升可用性。
        # ★ 2026-09-21：候选取全集（含 `chain` / `bing_rss`）—— 老服务端不认
        #   `chain` 会回 400，那时自动落到 `bing`/`360`，向后兼容。
        engines = [self.engine] + [e for e in ("chain", "bing", "360")
                                   if e != self.engine]
        try:
            budget = float(os.environ.get("MUSICAI_SEARCH_BUDGET_S")
                           or BROWSER_TOTAL_BUDGET_S)
        except Exception:  # noqa 环境变量写坏 → 用默认
            budget = BROWSER_TOTAL_BUDGET_S
        # ★ 单次 search() 的**墙钟总预算**：不论几个引擎、几次重试，到点就收工。
        #   旧实现每次都把「2 引擎 × 2 次 × 20s 超时」跑满 ⇒ 一次检索最坏 82s。
        deadline = now + budget
        last_err = ""
        for engine in engines:
            for attempt in range(BROWSER_RETRY):
                left = deadline - time.monotonic()
                if left <= 0.5:
                    last_err = last_err or "检索总预算耗尽"
                    break
                try:
                    r = requests.get(
                        f"{self.base_url}/search",
                        params={"q": query, "engine": engine},
                        timeout=min(20.0, left),
                        # ★ R119-4u：**显式禁用代理**。本机 `http_proxy`（如 127.0.0.1:10842）
                        #   会把 `127.0.0.1` 也代理掉 —— 这正是本项目 B109 踩过的坑
                        #   （`ai/model.py::_ollama_post` 已同款处理）。实测：带代理时
                        #   `requests` 根本连不到本机搜索服务，只拿到代理的 502；
                        #   而错误信息会伪装成"服务 503/超时"，把归因带偏。
                        proxies={"http": None, "https": None},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        out: list[SearchResult] = []
                        for item in (data.get("results") or [])[:max_results]:
                            out.append(SearchResult(
                                title=item.get("title", "") or "",
                                snippet=item.get("snippet", "") or "",
                                url=item.get("url", "") or "",
                                source="browser",
                            ))
                        if out:
                            with _CB_LOCK:          # 成功 ⇒ 复位熔断计数
                                _CB["fails"] = 0
                            logger.info("[检索·浏览器·%s] %s → %d 条",
                                        engine, query[:40], len(out))
                            return out
                        # 200 但为空：换引擎更可能拿到结果，不再重试同引擎
                        last_err = "空结果"
                        break
                    if r.status_code == 503:
                        detail = (r.json().get("detail") if r.content else "") or "未知错误"
                        last_err = detail
                        if time.monotonic() + BROWSER_BACKOFF >= deadline:
                            break                    # 退避会超预算 ⇒ 直接收工
                        # 服务可能刚断连又重连，短暂退避后重试一次；否则换引擎
                        if attempt < BROWSER_RETRY - 1:
                            time.sleep(BROWSER_BACKOFF)
                            continue
                        break
                    last_err = f"HTTP {r.status_code}"
                    break
                except Exception as e:  # noqa 服务未启动/网络异常 → 退避后重试/换引擎
                    last_err = str(e)[:120]
                    if time.monotonic() + BROWSER_BACKOFF >= deadline:
                        break
                    if attempt < BROWSER_RETRY - 1:
                        time.sleep(BROWSER_BACKOFF)
                        continue
                    break
            if time.monotonic() >= deadline:
                break
        # ★★★ R119-4u：失败累积到阈值 ⇒ 打开熔断，冷却期内后续调用**立即**返回空。
        #   这样"检索服务卡死"的最坏代价从「每次 82 秒 × 每首歌」降到「一次 ~6 秒，
        #   之后 120 秒内零成本」—— DJ 口播立刻回到"用无联网资料正常生成"。
        opened = 0.0
        with _CB_LOCK:
            _CB["fails"] += 1
            if _CB["fails"] >= BROWSER_BREAK_AFTER:
                _CB["until"] = time.monotonic() + BROWSER_COOLDOWN_S
                _CB["fails"] = 0
                _CB["opened"] += 1
                opened = BROWSER_COOLDOWN_S
        if opened:
            logger.warning("[检索·浏览器] 连续失败达阈值 → **熔断 %.0fs**（期间口播改用"
                           "无联网资料生成）：%s", opened, last_err)
        else:
            logger.warning("[检索·浏览器] 服务返回 503：%s", last_err)
        # ★★★ 2026-09-21：**直连 RSS 兜底**。搜索服务进程可能没起来 / 卡死 / 被
        #   回收，而 RSS 通道只需要**能出网** ⇒ 让"服务挂了"不再等于"检索没了"。
        #   自带独立熔断（3 连败 → 120 s 冷却）⇒ 最坏代价仍有界（单次 ~5 s）。
        return _direct_rss(query, max_results)


# ---------------- ★ 直连 RSS 兜底（服务不可用时的最后一跳）----------------
#  实现放在 `ai/bing_rss.py`（只用标准库）⇒ 与**搜索服务进程**共用同一份，
#  不存在"改了一处忘了另一处"。
_DBL = {"fails": 0, "until": 0.0, "open_n": 0, "ok_n": 0}
_DBL_LOCK = threading.Lock()
_RSS_BREAK_AFTER = 3
_RSS_COOLDOWN_S = 120.0
_RSS_TIMEOUT_S = 5.0


def _direct_rss(query: str, max_results: int = 5) -> list[SearchResult]:
    """不走本地搜索服务、**直接**打 Bing RSS（`ai/bing_rss.py`）。

    为什么值得单加这一跳：`ai/search.py` 的既有设计是"服务未启动时优雅降级为空"
    —— 于是搜索服务一旦没拉起来/被回收，口播就**整段没有联网资料**，而这条链路
    其实只需要出网，根本不依赖 Chrome。实测单次 0.31~0.35 s。
    """
    # ★ 一键回退口：`MUSICAI_SEARCH_DIRECT_RSS=0` 关掉这条兜底
    #   （回归套件需要"只剩浏览器链路"的确定环境时用它 —— 见
    #    `verify_r119_u_search_breaker.py` 的说明）。
    if os.environ.get("MUSICAI_SEARCH_DIRECT_RSS", "1") == "0":
        return []
    now = time.monotonic()
    with _DBL_LOCK:
        if now < _DBL["until"]:
            return []
    try:
        from ai.bing_rss import search_rss
    except Exception:                                       # noqa 模块缺失 → 维持旧行为
        return []
    try:
        rows = search_rss(query, timeout=_RSS_TIMEOUT_S)
    except Exception:                                       # noqa search_rss 本身不抛，双保险
        rows = []
    if not rows:
        opened = 0.0
        with _DBL_LOCK:
            _DBL["fails"] += 1
            if _DBL["fails"] >= _RSS_BREAK_AFTER:
                _DBL["until"] = time.monotonic() + _RSS_COOLDOWN_S
                _DBL["fails"] = 0
                _DBL["open_n"] += 1
                opened = _RSS_COOLDOWN_S
        if opened:
            logger.warning("[检索·直连RSS] 连续失败达阈值 → **熔断 %.0fs**", opened)
        return []
    out = [SearchResult(title=t, snippet=s, url=u, source="rss")
           for t, u, s in rows[:max(1, int(max_results))]]
    if out:
        with _DBL_LOCK:
            _DBL["fails"] = 0
            _DBL["ok_n"] += 1
        logger.info("[检索·直连RSS] %s → %d 条（服务不可用，走直连兜底）",
                    query[:40], len(out))
    return out


# ---------------- 工厂：运行时切换 ----------------
_provider_cache: dict[str, SearchProvider] = {}
_provider_lock = threading.Lock()


def get_search_provider(mode: str | None = None) -> SearchProvider:
    """按 config.search.mode 返回对应检索_provider，支持运行时切换。

    切换时旧 provider 的上下文由调用方在 search() 内部统一清理（clear_context），
    因此不存在跨模式的上下文污染。

    注意：provider 的构造（建库/初始化客户端）在锁外进行，避免在持锁状态下
    重入 _provider_lock 造成死锁，也避免慢构造阻塞其它调用方。
    """
    cfg = user_config.data.get("search", {})
    mode = (mode or cfg.get("mode") or "online").lower()
    # 第一次检查（快路径，无构造）
    with _provider_lock:
        cached = _provider_cache.get(mode)
        if cached is not None:
            return cached
    # 锁外构造，避免重入死锁与长临界区
    if mode == "offline":
        p: SearchProvider = OfflineSearchProvider()
    elif mode == "browser":
        p = BrowserSearchProvider()
    else:
        p = OnlineSearchProvider()
    # 第二次检查（Double-Checked Locking）：构造期间可能有他人已建好
    with _provider_lock:
        existing = _provider_cache.get(mode)
        if existing is not None:
            return existing
        _provider_cache[mode] = p
        return p


def rebuild_search_provider(mode: str | None = None) -> SearchProvider:
    """强制重建指定模式的 provider（切换/重配置时调用）。"""
    cfg = user_config.data.get("search", {})
    mode = (mode or cfg.get("mode") or "online").lower()
    with _provider_lock:
        _provider_cache.pop(mode, None)
    # 释放锁后再构造，避免重入 _provider_lock 导致死锁
    return get_search_provider(mode)
