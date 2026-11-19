"""检索优先的选歌候选池（2026-11-17）。

**为什么需要它**：此前的选歌让本地 LLM「凭记忆写歌名」——上限就是模型的训练语料，
编出来的名字再由 `search()[0]` 随机顶替，同艺人重复、落地失败都由此而来。
本模块把实体来源换成**音乐平台接口**：候选曲目全部真实存在、带 `id / 歌手 / 专辑 /
年份 / 热度`，模型只负责"从里面挑"。

数据来源（网易云 musicbox 后端，项目已在用，不需要 NeteaseCloudMusicApi 服务）：
    · 每日推荐  /weapi/v3/discovery/recommend/songs
    · 私人 FM   /weapi/v1/radio/get
    · 相似歌曲  /weapi/discovery/simiSong          （以用户自己的歌为种子）
    · 歌手热歌  /weapi/v1/artist/{id}              （同风格横向展开）
    · 关键词    /weapi/search/get                  （流派 / 年代 / 语种）

设计纪律：
  1. **任何异常都只返回 []**，由上层回退旧路径——推荐可以变差，但绝不能崩；
  2. **去重范围 = 播放列表窗口**（正在播 + 待播 + 推荐货架），窗口外的历史不管；
  3. 池侧配额（单艺人 ≤3 / 单专辑 ≤2），避免"池子被一个艺人灌满"逼模型二选一；
  4. 纯代码、无 LLM —— 模型睡眠时也能出货架。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("MusicAI.RecoPool")

# 通道权重：越像"这首歌的邻居"越高（收藏是用户自己选的，最高）
CHANNEL_W = {
    "fav": 1.10, "simi": 1.00, "fm": 0.85,
    "daily": 0.75, "artist": 0.55, "tag": 0.45,
}

# 池侧配额：防止单个艺人/专辑把候选表刷满
ARTIST_CAP = 3
ALBUM_CAP = 2


@dataclass
class Cand:
    id: int
    title: str
    artist: str
    album: str = ""
    year: str = ""
    popularity: float = 0.0
    channel: str = ""
    score: float = 0.0
    group: str = ""              # 熟悉 | 探索
    raw: dict = field(default_factory=dict)

    def line(self) -> str:
        tail = "收藏" if self.channel == "fav" else "热度%d" % int(self.popularity)
        return "#%d | %s | %s | %s | %s | %s" % (
            self.id, self.artist or "未知", self.title,
            self.album or "-", self.year or "-", tail)


# ---------------------------------------------------------------- 小工具

def _norm(s) -> str:
    """归一化：去空白/标点、转小写。用于同曲、同艺人、同专辑的比较。"""
    return re.sub(r"[\W_]+", "", str(s or "").lower())


def _year_of(song: dict) -> str:
    al = song.get("album") or song.get("al") or {}
    ts = al.get("publishTime") if isinstance(al, dict) else 0
    if isinstance(ts, (int, float)) and ts > 1e11:
        return str(time.gmtime(ts / 1000).tm_year)
    return ""


def _artist_of(song: dict) -> str:
    arts = song.get("artists") or song.get("ar") or []
    if isinstance(arts, list):
        return " / ".join(a.get("name", "") for a in arts
                          if isinstance(a, dict) and a.get("name")).strip()
    return str(arts or "").strip()


def _brief(song: dict, channel: str) -> Cand | None:
    sid = song.get("id")
    if not sid:
        return None
    al = song.get("album") or song.get("al") or {}
    return Cand(
        id=int(sid),
        title=(song.get("name") or "").strip(),
        artist=_artist_of(song),
        album=((al.get("name") if isinstance(al, dict) else "") or "").strip(),
        year=_year_of(song),
        popularity=float(song.get("popularity") or song.get("pop") or 0),
        channel=channel,
        raw=song,
    )


def favorite_cands(rows: list[dict]) -> list[Cand]:
    """把用户收藏（带网易云 ext_id 的）转成候选锚点。

    `rows`: [{"ext_id":..,"title":..,"artist":..,"album":..,"year":..}]
    """
    out: list[Cand] = []
    for r in rows or []:
        try:
            sid = int(r.get("ext_id") or 0)
        except (TypeError, ValueError):
            continue
        if not sid:
            continue
        out.append(Cand(id=sid, title=(r.get("title") or "").strip(),
                        artist=(r.get("artist") or "").strip(),
                        album=(r.get("album") or "").strip(),
                        year=str(r.get("year") or ""),
                        popularity=50.0, channel="fav",
                        raw={"id": sid, "name": r.get("title") or "",
                             "artists": [{"name": r.get("artist") or ""}],
                             "album": {"name": r.get("album") or ""}}))
    return out


# ---------------------------------------------------------------- 召回

def _recall(source, seeds, tags, budget_s: float) -> list[Cand]:
    """多路召回。musicbox 后端以外的实现暂不支持（返回空 → 上层回退旧路径）。"""
    backend = (getattr(source, "backend", "") or "").lower()
    if backend != "musicbox":
        logger.info("[候选池] 网易云后端为 %r，非 musicbox，暂不启用检索优先选歌", backend)
        return []
    api = source._musicbox()
    pool: dict[int, Cand] = {}
    t0 = time.monotonic()

    def left() -> float:
        return budget_s - (time.monotonic() - t0)

    def add(channel: str, songs) -> int:
        n0 = len(pool)
        for x in songs or []:
            if not isinstance(x, dict):
                continue
            c = _brief(x, channel)
            if c and c.title and c.id not in pool:
                pool[c.id] = c
        return len(pool) - n0

    # ① 每日推荐（平台协同过滤，最稳的一路）
    if left() > 1.0:
        try:
            r = api.request("POST", "/weapi/v3/discovery/recommend/songs",
                            {"afresh": False}) or {}
            add("daily", (r.get("data") or {}).get("dailySongs") or [])
        except Exception as e:                                   # noqa
            logger.info("[候选池] 每日推荐失败：%s", type(e).__name__)

    # ② 私人 FM
    if left() > 1.0:
        try:
            add("fm", api.personal_fm())
        except Exception as e:                                   # noqa
            logger.info("[候选池] 私人FM失败：%s", type(e).__name__)

    # ③ 相似歌曲（以用户自己的歌为种子）
    for _title, _artist, sid in list(seeds or [])[:3]:
        if left() <= 0.8:
            break
        try:
            r = api.request("POST", "/weapi/discovery/simiSong",
                            {"songid": int(sid), "limit": 10}) or {}
            add("simi", r.get("songs") or [])
        except Exception as e:                                   # noqa
            logger.info("[候选池] 相似歌曲失败：%s", type(e).__name__)

    # ④ 歌手热歌：从已有池里挑一个艺人做"同风格横向展开"（只取 1 个，避免单一艺人灌满）
    if left() > 1.2 and pool:
        from collections import Counter
        try:
            artist = Counter(c.artist for c in pool.values() if c.artist).most_common(1)[0][0]
            r = api.request("POST", "/weapi/search/get",
                            {"s": artist, "type": 100, "limit": 1, "offset": 0}) or {}
            arts = (r.get("result") or {}).get("artists") or []
            if arts:
                hot = api.request("POST", "/weapi/v1/artist/%s" % arts[0]["id"]) or {}
                add("artist", (hot.get("hotSongs") or [])[:15])
        except Exception as e:                                   # noqa
            logger.info("[候选池] 歌手热歌失败：%s", type(e).__name__)

    # ⑤ 关键词（流派/年代/语种）：质量不稳定，只当补充
    for tag in list(tags or [])[:2]:
        if left() <= 0.5:
            break
        try:
            rr = api.search(tag, stype=1, limit=10) or {}
            add("tag", rr.get("songs") or [])
        except Exception as e:                                   # noqa
            logger.info("[候选池] 关键词 %r 失败：%s", tag, type(e).__name__)

    return list(pool.values())


# ---------------------------------------------------------------- 过滤 / 打分

def _filter_and_score(pool: list[Cand], *, blocked_ids, blocked_artists,
                      favorite_ids, favorite_artists, familiarity: int) -> list[Cand]:
    seen_song: set[tuple[str, str]] = set()
    artist_n: dict[str, int] = {}
    album_n: dict[str, int] = {}
    out: list[Cand] = []

    for c in pool:
        a_norm = _norm(c.artist)
        # ① 播放列表窗口：同曲 / 同艺人 直接不进池（模型看不见就无从违反）
        if c.id in blocked_ids:
            continue
        if a_norm and a_norm in blocked_artists:
            continue
        # ② 同曲去重（跨源同名同歌手）
        key = (_norm(c.title), a_norm)
        if key in seen_song:
            continue
        # ③ 池侧配额：单艺人 / 单专辑
        if a_norm and artist_n.get(a_norm, 0) >= ARTIST_CAP:
            continue
        akey = (_norm(c.album), a_norm)
        if c.album and album_n.get(akey, 0) >= ALBUM_CAP:
            continue

        seen_song.add(key)
        if a_norm:
            artist_n[a_norm] = artist_n.get(a_norm, 0) + 1
        if c.album:
            album_n[akey] = album_n.get(akey, 0) + 1

        s = CHANNEL_W.get(c.channel, 0.40)
        s += min(c.popularity, 100.0) / 200.0
        familiar = (c.id in favorite_ids) or (a_norm in favorite_artists)
        # 2026-11-17 实测修正：收藏权重必须**随推荐偏好反向变化**——
        # 原来恒定 +0.60，导致"更陌生(3)"档的降级排序照样全推收藏（8 轮里 5 轮如此）。
        _fav_bonus = (0.60, 0.35, 0.05, -0.15)[max(0, min(3, familiarity))]
        _art_bonus = (0.30, 0.18, 0.00, -0.10)[max(0, min(3, familiarity))]
        if c.id in favorite_ids:
            s += _fav_bonus
        elif a_norm in favorite_artists:
            s += _art_bonus
        # 探索档：压低"熟悉侧"通道、抬高"发现侧"通道
        if familiarity >= 2:
            if c.channel == "fav":
                s -= 0.35
            elif c.channel == "simi":
                s -= 0.10 if familiarity == 2 else 0.20
            elif c.channel in ("daily", "fm", "tag"):
                s += 0.20
        if c.year.isdigit() and int(c.year) >= time.gmtime().tm_year - 1:
            s += 0.10
        c.score = round(s, 3)
        c.group = "熟悉" if familiar else "探索"
        out.append(c)

    out.sort(key=lambda x: x.score, reverse=True)
    return out


def build_pool_fast(source, *, blocked_ids=None, blocked_artists=None,
                    familiarity: int = 0, limit: int = 30) -> list[Cand]:
    """★ 冷启动快速路径（2026-11-18）：**只调网易云「每日推荐」一个 API**（实测 0.5s/34首），
    跳过 _recall 里会卡住的相似歌曲/歌手热歌/关键词等慢召回（实测整条 build_pool 卡 98s）。

    每日推荐本身是平台协同过滤排好序的真实歌曲 ⇒ 直接当候选，供冷启动秒填推荐页，
    保证「第一首歌放完前第二首导听已就绪」。稳态补货仍走完整 build_pool（LLM 挑）。
    任何异常返回 []（上层回退完整路径）。
    """
    try:
        backend = (getattr(source, "backend", "") or "").lower()
        if backend != "musicbox":
            return []
        api = source._musicbox()
        r = api.request("POST", "/weapi/v3/discovery/recommend/songs",
                        {"afresh": False}) or {}
        songs = (r.get("data") or {}).get("dailySongs") or []
        pool: list[Cand] = []
        seen: set[int] = set()
        for x in songs:
            if not isinstance(x, dict):
                continue
            c = _brief(x, "daily")
            if c and c.title and c.id not in seen:
                seen.add(c.id)
                pool.append(c)
        if not pool:
            return []
        b_ids = {abs(int(x)) for x in (blocked_ids or ()) if str(x).lstrip("-").isdigit()}
        b_arts = {_norm(a) for a in (blocked_artists or ()) if a}
        out = _filter_and_score(pool, blocked_ids=b_ids, blocked_artists=b_arts,
                                favorite_ids=set(), favorite_artists=set(),
                                familiarity=familiarity)
        if len(out) < 6 and b_arts:  # 窗口挡太狠 → 放宽艺人
            out = _filter_and_score(pool, blocked_ids=b_ids, blocked_artists=set(),
                                    favorite_ids=set(), favorite_artists=set(),
                                    familiarity=familiarity)
        out = out[:limit]
        logger.info("[候选池·fast] 每日推荐 %d → 可用 %d（冷启动秒填）", len(pool), len(out))
        return out
    except Exception as e:  # noqa
        logger.warning("[候选池·fast] 失败（回退完整召回）：%s: %s", type(e).__name__, e)
        return []


def build_pool(source, *, seeds, tags, favorite_rows, blocked_ids, blocked_artists,
               familiarity: int = 0, limit: int = 60, min_pool: int = 8,
               budget_s: float = 6.0) -> list[Cand]:
    """召回 → 窗口过滤 → 去重/配额 → 打分排序。

    返回按分数降序的候选，**已剔除播放列表窗口内的歌与艺人**。
    窗口挡得太狠（用户只收藏一个歌手那种情况）时**逐级放宽**：
      ① 同曲 + 同艺人 → ② 只挡同曲 →（同曲永远不放开，否则马上重播同一首）。
    任何异常都返回 []（上层回退旧路径）。
    """
    try:
        favs = favorite_cands(favorite_rows or [])
        pool = _recall(source, seeds, tags, budget_s)
        pool.extend(favs)
        favorite_ids = {c.id for c in favs}
        favorite_artists = {_norm(c.artist) for c in favs if c.artist}
        # ★ 2026-11-17 实测抓到的 bug：`Cand.id` 是平台 id（正数），而 `Track.id` 是
        #   `-abs(sid)`（负数）⇒ 直接比 `c.id in blocked_ids` **永远不命中**，
        #   "窗口内同曲不重复"其实一直没生效（同艺人挡住了，所以平时看不出来）。
        #   统一到平台 id 空间：绝对值。
        b_ids = set()
        for x in (blocked_ids or ()):
            try:
                b_ids.add(abs(int(x)))
            except (TypeError, ValueError):
                continue
        b_arts = {_norm(a) for a in (blocked_artists or ()) if a}

        def _pass(arts: set) -> list[Cand]:
            return _filter_and_score(
                pool, blocked_ids=b_ids, blocked_artists=arts,
                favorite_ids=favorite_ids, favorite_artists=favorite_artists,
                familiarity=familiarity)

        out = _pass(b_arts)
        if len(out) < min_pool and b_arts:
            logger.info("[候选池] 窗内过滤后仅 %d 首（< %d）→ 放宽同艺人限制",
                        len(out), min_pool)
            out = _pass(set())
        if len(out) > limit:
            out = out[:limit]
        logger.info("[候选池] 原始 %d → 可用 %d（窗内挡 %d 首 / %d 位艺人）",
                    len(pool), len(out), len(b_ids), len(b_arts))
        return out
    except Exception as e:                                       # noqa
        logger.warning("[候选池] 构建失败（回退旧路径）：%s: %s", type(e).__name__, e)
        return []


def split_for_prompt(cands: list[Cand], familiar_max: int = 20,
                     explore_max: int = 20) -> tuple[list[Cand], list[Cand]]:
    """按分组各取前 N 条进提示词（模型只看这 40 行左右）。"""
    familiar = [c for c in cands if c.group == "熟悉"][:familiar_max]
    explore = [c for c in cands if c.group == "探索"][:explore_max]
    return familiar, explore


def to_track(source, cand: Cand):
    """候选 → Track（path=netease://<id>，播放时由 SourceManager 解析直链）。"""
    try:
        return source._to_track(cand.raw)
    except Exception as e:                                       # noqa
        logger.warning("[候选池] 造 Track 失败 #%s %s：%s", cand.id, cand.title, e)
        return None


def validate_picks(picks: list[dict], cands: list[Cand], k: int) -> tuple[list[str], list[str]]:
    """校验这一轮的选择。返回 (硬错误, 软提示)。

    硬：ID 必须在池里 / 数量 / 同曲重复 / 同艺人重复。
    软：同专辑重复（同专辑第二首偶尔是好事，只提示）。
    """
    errs: list[str] = []
    warns: list[str] = []
    by_id = {c.id: c for c in cands}
    ids = [p.get("id") for p in picks if isinstance(p, dict)]
    bad = [i for i in ids if i not in by_id]
    if bad:
        errs.append("不在候选池里的 ID：%s" % bad)
    if len(picks) != k:
        errs.append("数量 %d ≠ %d" % (len(picks), k))
    if len(set(ids)) != len(ids):
        errs.append("同一首重复出现")
    arts = [_norm(by_id[i].artist) for i in ids if i in by_id]
    dup_art = {a for a in arts if a and arts.count(a) > 1}
    if dup_art:
        errs.append("同艺人重复：%s" % sorted(dup_art))
    albums = [_norm(by_id[i].album) for i in ids if i in by_id and by_id[i].album]
    dup_al = {a for a in albums if albums.count(a) > 1}
    if dup_al:
        warns.append("同专辑重复（软）：%s" % sorted(dup_al))
    return errs, warns
