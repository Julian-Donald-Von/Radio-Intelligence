"""AI 电台编排：本模块承载按职责拆出的 mixin 引擎之一。

见 ai/radio.py 的总述：AIRadioController 通过多重继承组合
PlaybackOrchestrator / ScheduleEngine / TransitionHandler，本文件只负责"音频进退与
ducking / 口播播放 / 进度回调"这一职责。所有方法直接操作 self（即控制器实例），共享
状态与跨职责互调经继承链透明解析，行为与原单体实现完全一致。
"""
from __future__ import annotations

from core.op_timer import timed

import logging
import os
import random
import socket
import threading
import time
from urllib.parse import urlparse

from utils.weather import get_weather
from utils.location import get_city, time_slot
from config.settings import PlayerConfig, RadioConfig, user_config
from core.lyrics import parse_lrc, line_index_at, coerce_lrc_text
from core.tts_segment import strip_tags

try:
    # 模糊匹配：提高 AI 推荐歌名「落地命中」本地曲库的成功率（漏括号/版本后缀也能中）。
    from rapidfuzz import fuzz as _fuzz
    _HAVE_FUZZ = True
except Exception:  # noqa: rapidfuzz 未装时退回精确/包含匹配，不阻断推荐
    _fuzz = None
    _HAVE_FUZZ = False

from PySide6.QtCore import QObject, Signal, Slot, Qt
from PySide6.QtWidgets import QApplication

logger = logging.getLogger("MusicAI.Radio")

# ★ 2026-11-17：检索优先的选歌（ai/reco_pool.py）。默认开；
#   `MUSICAI_RECO_GROUNDED=0` 立刻退回旧的「模型自由点歌名 + 按名搜索」路径。
RECO_GROUNDED_ENV = "MUSICAI_RECO_GROUNDED"


def reco_grounded_enabled() -> bool:
    return os.environ.get(RECO_GROUNDED_ENV, "1") != "0"

# 天气口播机械降频：相邻两次「提供天气」之间至少间隔多少首歌。
WEATHER_MIN_GAP = 5

# ★★★ R119-4u：**每首歌起播时，最多为待播队列的前几首预生成「导听口播」**。
#   为什么必须限流（用户真机日志 2026-09-19 21:13 钉死，详见 _prep_next_broadcast）：
#     `play_all_library()`（cap=100）/ `play_tracks()`（cap=200）会把待播队列一次性填到
#     上百首，而旧实现对**每一首**都发两条 LLM 请求（导听口播 + 转场）⇒ 一次开播就在
#     唯一的 Ollama 串行锁前堆 ~200 条任务：**转场**（便宜）成批先跑完（用户看到的
#     「一下子加载一堆 outro」），而**当前曲真正要播的导听口播**排在几百条之后，
#     等它拿到锁时歌早播完了 ⇒ 迟到兜底 ⇒「intro 不生成，只有兜底句」。
#   只需「先于它自己开播」备好 ≈ 一首歌的提前量。（可用 MUSICAI_BROADCAST_PREFETCH_N 覆盖）
# ★ 2026-11-17：默认 2 → 4。实测「整首 4 分钟都没生成出第二首口播」：
#   35B 单条口播 15~40s，串行链只排 2 首时，首曲一结束第二首常常还没轮到；
#   排 4 首能在第一首歌的时间里把后面几首备好（多排不会更慢，链是串行的）。
try:
    BROADCAST_PREFETCH_N = max(1, int(os.environ.get("MUSICAI_BROADCAST_PREFETCH_N")
                                      or 4))
except Exception:  # noqa 环境变量写坏时退回默认，绝不因配置崩掉电台
    BROADCAST_PREFETCH_N = 4

# ★★ 2026-11-18 LLM/TTS 流水线：常驻模式下口播链改成
#   「LLM 生成一条 → submit_wait 同步等 TTS 合成 → 再生成下一条」严格串行，
#   使 LLM 与 TTS 永不同时占 GPU（实测并发时 TTS 合成 208s、错峰仅 5s）。
#   牺牲第一首即时口播（用户认可），换后面全部歌的稳定和快速。
#   MUSICAI_TTS_PIPELINE=0 可关，退回旧异步 submit。
try:
    TTS_PIPELINE = (os.environ.get("MUSICAI_TTS_PIPELINE", "1") != "0")
except Exception:  # noqa
    TTS_PIPELINE = True
try:
    TTS_PIPELINE_WAIT_S = max(30.0, float(os.environ.get("MUSICAI_TTS_PIPELINE_WAIT_S")
                                          or 90.0))
except Exception:  # noqa
    TTS_PIPELINE_WAIT_S = 90.0

# ★★★ 2026-11-18（用户拍板「砍掉转场全部功能」）：
#   转场 LLM 排版在导听前、每次占 35B 几十秒，把导听饿死（切歌只能播"先交给音乐"）。
#   用户决定彻底不要转场。总开关一处关闭生成侧+播放侧；保留代码便于回退。
#   设 MUSICAI_TRANSITION=1 可恢复（临时验证用）。
try:
    TRANSITION_ENABLED = (os.environ.get("MUSICAI_TRANSITION", "0") == "1")
except Exception:  # noqa
    TRANSITION_ENABLED = False


class ScheduleEngine:
        def _local_pool(self) -> list:
            """曲库（我喜欢的音乐）中的本地曲目，作为"曲库内"候选。"""
            try:
                favs = self.library.favorites(source="local")
            except Exception:  # noqa
                favs = [t for t in self.library.tracks if t.source in ("local", "", None)]
            return favs

        def _disliked_titles(self) -> list[str]:
            """收集"用户明确不喜欢"的歌名/歌手，传给 AI 避雷（含外部源歌曲）。

            优先从持久化的「不喜欢的歌」隐藏库（db.disliked_meta）读取——
            外部源歌曲即便未入库、也不在当前播放列表里，只要被标记过不喜欢，
            其标题/歌手就会被喂给 AI 避雷，跨会话持续生效。
            """
            out: set[str] = set()
            try:
                for title, artist in self.db.disliked_meta():
                    if title:
                        out.add(f"{title} - {artist or '未知'}")
            except Exception:  # noqa
                pass
            # 兜底：本会话内存里（库 + 当前/待播）遇到的不喜欢的歌也补进去
            for t in self.library.tracks:
                if t.id in self.disliked and t.title:
                    out.add(f"{t.title} - {t.artist or '未知'}")
            with self._qlock:
                self._clean_upcoming()  # 防御：保证下方 e["track"] 读取安全
                pool = [self.current] + [e["track"] for e in self.upcoming]
            for t in pool:
                if t is not None and t.id in self.disliked and t.title:
                    out.add(f"{t.title} - {t.artist or '未知'}")
            return list(out)

        def _audit_reco_tracks(self, tracks: list, familiarity: int) -> list:
            """审核 AI 推荐结果（仅在曲库 > 100 首时生效硬约束）：

            1) 剔除「不喜欢的歌」（隐藏库，对 AI 避雷生效，覆盖外部歌，用稳定键）；
            2) 按熟悉度档位限制「曲库内的歌」占比上限，让「更熟悉→更陌生」偏好
               真正决定推荐里「曲库歌 vs 探索外源歌」的比例，而非摆设：
               更熟悉/略熟悉/略陌生/更陌生 分别 80% / 60% / 40% / 20%。

            审核策略：外部歌（曲库外）优先全部保留；曲库歌在超限时裁掉多余的，
            使 曲库/(曲库+外部) ≤ 上限。无外部歌可稀释时（极端情况，一般是外部
            音源未配置）跳过占比审核、保留曲库歌，避免推荐被裁空导致电台断流。
            """
            if not tracks:
                return tracks
            # 1) 不喜欢过滤（稳定键，覆盖外部歌；self.disliked 覆盖本会话内存 id）
            kept = [t for t in tracks
                    if self.fav_key(t) not in self.disliked_keys and t.id not in self.disliked]
            if not kept:
                return kept
            # 2) 曲库（我喜欢的音乐）> 100 首才审核占比
            try:
                fav_ids = self.db.favorite_ids()
            except Exception:  # noqa
                fav_ids = set()
            if len(fav_ids) <= 100:
                return kept
            caps = (0.8, 0.6, 0.4, 0.2)
            cap = caps[max(0, min(3, familiarity))]
            libs = [t for t in kept if t.id in fav_ids]
            externals = [t for t in kept if t.id not in fav_ids]
            if not externals:
                # 无外部候选可稀释：无法强制压低占比，保留曲库歌（避免推荐清空），仅记录
                logger.info("[RecoAudit] 无外部候选，跳过占比审核（曲库 %d 首未裁剪）",
                            len(libs))
                return kept
            # 允许的最大曲库歌数 = floor(cap * 外部数 / (1-cap))，保证 曲库/(曲库+外部) <= cap
            allowed_lib = int(cap * len(externals) / (1 - cap))
            if len(libs) <= allowed_lib:
                return kept
            trimmed = libs[:allowed_lib]
            logger.info(
                "[RecoAudit] 熟悉度=%s 曲库占比上限=%.0f%%：曲库歌 %d→%d，外部保留 %d",
                familiarity, cap * 100, len(libs), len(trimmed), len(externals))
            return externals + trimmed

        def _external_pool(self, n: int = 8) -> list:
            """从直链源（网易云/QQ音乐）按品味检索候选（best-effort，失败返回空）。"""
            if not self.source_manager:
                return []
            out: list = []
            try:
                taste = self.taste_profile or {}
                kw = ""
                if isinstance(taste, dict):
                    tags = taste.get("品味关键词") or taste.get("tags") or []
                    if isinstance(tags, list) and tags:
                        kw = str(tags[0])
                if not kw:
                    kw = "华语 热门"
                for key in ("netease", "qqmusic"):
                    src = self.source_manager.get(key)
                    if not src or not src.enabled:
                        continue
                    try:
                        res = src.search(kw)
                    except Exception as e:  # noqa
                        logger.warning("直链源检索失败 %s: %s", key, e)
                        continue
                    for r in res[: max(2, n // 2)]:
                        out.append(r)
                        if len(out) >= n:
                            break
                    if len(out) >= n:
                        break
            except Exception as e:  # noqa
                logger.warning("外部候选检索异常: %s", e)
            return out

        def _candidates(self, k: int = 40, include_external: bool = False,
                        allow_played: bool = False) -> tuple[list, set[int]]:
            """返回 (候选曲目, 曲库内 id 集合)，排除不喜欢/已播/当前/待播。

            include_external 默认 False：主线程的填充路径只取本地候选，绝不触网，
            避免主线程被直链源检索阻塞导致界面卡死；外部漫游检索只在后台
            _resolve_selections 中进行。

            allow_played 默认 False：常规取歌排除「已播」以避免短期重复（电台不该
            立刻重播刚放过的歌）。但当新鲜候选已耗尽（曲库小 / 已播完）时，续推逻辑
            会显式传 allow_played=True，允许回放已播歌曲——否则推荐列表会一路掉到 0、
            电台彻底停播。回放兜底仍排除 current/upcoming（不立刻重播正在排的歌）。
            """
            self._clean_upcoming()  # 防御：保证下方读取 e["track"].id 不会因脏条目崩溃
            local = self._local_pool()
            external = self._external_pool(k) if include_external else []
            with self._qlock:
                busy = {e["track"].id for e in self.upcoming}
            if self.current_id is not None:
                busy.add(self.current_id)
            pool = []
            seen = set()
            for t in local + external:
                if t.id in self.disliked or t.id in busy:
                    continue
                # 常规排除已播；仅当允许回放（新鲜候选耗尽）时才纳入已播歌曲
                if (not allow_played) and t.id in self.played:
                    continue
                if t.id in seen:
                    continue
                seen.add(t.id)
                pool.append(t)
            local_ids = {t.id for t in local}
            if len(pool) > k:
                pool = random.sample(pool, k)
            return pool, local_ids

        # ---------- 入口填充 ----------

        def _fill(self, n: int) -> int:
            """补充 n 首待播。

            - AI 开启：纯 AI 填充——不提前用本地曲「占位」。先提交 AI 品味分析、由 AI 按
              品味推荐歌曲（歌曲先入推荐列表）；列表在 AI 返回前以「生成中」呈现，开场由
              _maybe_start_deferred 在 AI 填充完成后选曲开播（见 start_radio）。即便本地
              候选为空也照常触发 AI（可从外部音源补）。
            - AI 关闭：用本地曲库同步占位填充，保证立即有歌可放（离线兜底）。
            返回实际追加的离线兜底数量（AI 路径返回 0）。
            """
            if n <= 0:
                return 0
            llm_enabled = bool(getattr(self.brain, "llm", None)
                               and self.brain.llm.enabled)
            cands, local_ids = self._candidates(max(n * 4, 20))
            if llm_enabled:
                # 统一推荐管线：AI 先入推荐页（单一暂存池），本方法从推荐页引流进播放列表。
                # 即  AI → 推荐页(reco_buffer) → 播放列表(upcoming) → 播放。
                threading.Thread(target=timed(self._fill_from_reco, "reco_fill"),
                                 args=(n, cands, local_ids), daemon=True).start()
                return 0
            # 无 AI：用本地曲库同步占位填充，保证立即有歌可放（离线兜底）。
            # 新鲜候选耗尽时允许回放已播歌曲，避免电台彻底停播。
            replay = False
            if not cands:
                cands, local_ids = self._candidates(max(n * 4, 20), allow_played=True)
                replay = True
            picks = self._offline_picks(cands, n) if cands else []
            added = self._append_picks(picks, n, allow_played=replay)
            self._emit_playlist()
            return added

        def _maybe_shuffle(self, items: list) -> list:
            """播放模式 random：歌曲**进入播放列表之前**先打乱顺序。

            ⚠ 关键约定（与用户的既定设计一致）：random 必须在「填充列表」这一步落实，
            **不能**在 _advance 取歌时才随机挑一首。因为 AI 口播是按 upcoming[0]
            预先生成的（_prep_next_broadcast），若取歌时才随机，播的就不是已经备好
            旁白的那首 → "旁白与歌曲对不上"。
            先打乱再入列 ⇒ 播放列表顺序 = 实际播放顺序，AI 直接从播放列表取歌，
            前端列表视图与真实播放顺序也保持一致。
            """
            if getattr(self, "play_mode", "order") == "random" and len(items) > 1:
                random.shuffle(items)
            return items

        def _append_picks(self, picks: list, n: int, allow_played: bool = False) -> int:
            self._clean_upcoming()  # 防御：下方读取 e["track"].id
            added = 0
            with self._qlock:
                for t in self._maybe_shuffle(list(picks)):
                    if added >= n:
                        break
                    if t.id in self.disliked:
                        continue
                    # 常规排除已播；仅回放兜底（allow_played）时允许已播歌曲入库
                    if (not allow_played) and t.id in self.played:
                        continue
                    if any(e["track"].id == t.id for e in self.upcoming):
                        continue
                    entry = self._make_entry(t, "为你精选的一首。")
                    self.upcoming.append(entry)
                    added += 1
            return added

        def _local_fallback(self, want: int) -> None:
            """AI 不可用 / 彻底失败时的兜底：用本地曲库歌把推荐列表补满。

            仅作兜底（AI 正常时列表完全由 AI 推荐构成，不调用本方法），避免日常
            场景下推荐列表混入曲库红心占位歌。
            """
            with self._qlock:
                up_len = len(self.upcoming)
            # 待播队列永远以 PLAYLIST_SIZE 为目标（当前曲是独立计数的，不占队列名额）。
            # 与 _refill 的 target 保持一致，避免「带当前曲时少补一首」导致队列卡在 4 首。
            target = RadioConfig.PLAYLIST_SIZE
            need = target - up_len
            if need <= 0:
                return
            cands, _ = self._candidates(max(need * 4, 20))
            replay = False
            if not cands:
                # 新鲜候选耗尽（曲库小 / 已播完）：允许回放已播歌曲兜底，电台不彻底停播
                cands, _ = self._candidates(max(need * 4, 20), allow_played=True)
                replay = True
            picks = self._offline_picks(cands, need) if cands else []
            self._append_picks(picks, need, allow_played=replay)
            self._emit_playlist()

        def _fill_from_reco(self, n: int, cands: list, local_ids: set[int]) -> None:
            """后台：从「推荐页（reco_buffer，单一 AI 暂存池）」复制 n 首到播放列表。

            这是 LLM 开启时播放列表的唯一填充路径，实现统一的推荐管线：
                AI 推荐 → 推荐页(reco_buffer) →（本方法复制）→ 播放列表(upcoming) → 播放
            推荐页是电台的「待荐库」（AI 常驻思考、可点播、保持流动），播放列表是「最终
            播放队列」。播放列表不再独立调 AI，避免「推荐页填满却与播放列表无关」的割裂。

            关键语义是「复制」不是「移动」：
            - 从 reco_buffer 复制 n 首到 upcoming（带原 AI 理由），原条目仍留在推荐页；
            - 推荐页因此不会随着播放列表填充而被抽空，始终保持「满架 + 流动刷新」；
            - 一首歌只在它被真正播放（成为 current 或进入 played）时才从 reco_buffer 清理，
              随后 _refill_reco 会异步补一首新的，形成「播一首、补一首」的持续流动。

            - 抽完后 _refill_reco() 让推荐页回到满架（如已有歌曲被播放，会清理并补新货）；
            - 若推荐页不足 n 首（冷启动 / AI 抖动）：差额「不」由本方法直接调 _ai_pick_tracks
              补货——AI 推荐是「推荐页独占」的（见 _ai_pick_tracks 注释），播放列表只能从
              推荐页复制。缺口「绝不」用本地曲库补齐：播放列表里每一首都必须先出现在推荐页，
              缺口留空由推荐页持续回补 + 稳态 _refill 自然补入，杜绝「推荐页没有的歌混进播放
              列表」。仅当「播放列表 AND 推荐页同时为空」（AI 彻底不可用）才紧急本地兜底防静默；
            - 复制曲目理由为泛用「AI 推荐」时，后台 _enrich_pulled_reasons 补全详细理由
              （DJ 口播文案），不阻塞复制；
            - 开场（_need_start）时，末尾 _maybe_start_deferred() 选曲开播。
            cands/local_ids 暂未在本路径使用（本地兜底由 _local_fallback 自算），保留签名
            以兼容调用方。
            """
            if not getattr(self.brain, "llm", None) or not self.brain.llm.enabled:
                # 理论上不会走到（llm 关闭走 _fill 离线分支）；兜底本地补位并收尾。
                self._local_fallback(n)
                self._maybe_start_deferred()
                return
            self._emit("thinking", True)
            need = max(1, int(n))
            # 1) 从推荐页「复制」n 首到播放列表（推荐页仍保留这些条目，保持满架）
            with self._qlock:
                exclude = (set(self.played) | {self.current_id} | self.disliked)
                up_ids = {e["track"].id for e in self.upcoming
                          if self._is_track(e.get("track"))}
                pulled: list = []
                for e in self.reco_buffer:
                    t = e.get("track")
                    if len(pulled) >= need:
                        break
                    if not self._is_track(t) or t.id in exclude or t.id in up_ids:
                        continue
                    pulled.append(e)          # 复制：不修改 reco_buffer
            # random 模式：先打乱再入列（保证列表顺序 = 实际播放顺序，口播才对得上）
            pulled = self._maybe_shuffle(list(pulled))
            with self._qlock:
                for e in pulled:
                    if not any(x["track"].id == e["track"].id
                               for x in self.upcoming):
                        self.upcoming.append(e)
            # 2) 让推荐页回到满架。如本首歌刚被播放，_clean_reco 会清理它并触发 AI 补新货，
            #    实现「播一首、补一首」的流动。复制本身不会减少 reco_buffer 数量。
            self._refill_reco()
            # 3) 播放列表「绝不」用本地曲库兜底补满——所有入列歌必须先过推荐页。推荐页暂不足
            #    时（冷启动/AI 抖动）缺口留空，靠推荐页持续回补 + 稳态 _refill 自然把 AI 推荐
            #    复制进来（_ai_reco_fill 收尾会触发 _refill，形成「推荐页→播放列表」闭环）。
            #    这样播放列表里绝不会出现推荐页没有的歌。若 AI 彻底不可用（推荐页始终为空），
            #    电台保持静默、由 UI 提示，绝不拿本地曲库冒充推荐。（LLM 关闭时走 _fill 离线
            #    分支，那才是图书馆播放模式，与推荐页无关。）
            # 4) 后台补全复制曲目的推荐理由（DJ 口播文案），不阻塞复制
            if pulled:
                threading.Thread(target=timed(self._enrich_pulled_reasons, "enrich"),
                                 args=(list(pulled),), daemon=True).start()
            self._emit_playlist()
            self._emit("thinking", False)
            self._maybe_start_deferred()

        def _enrich_pulled_reasons(self, entries: list) -> None:
            """后台：为从推荐页引流进播放列表的曲目补全 AI 推荐理由（DJ 口播文案）。

            仅当理由仍是泛用「AI 推荐」时才补全（已是详细理由的随机一首等不重复生成），
            避免无谓的 AI 调用。补全后更新 entry 并刷新播放列表 UI。
            """
            for entry in entries:
                t = entry.get("track")
                if not self._is_track(t):
                    continue
                if entry.get("reason") not in (None, "", "AI 推荐"):
                    continue
                try:
                    reason = self.brain.explain_recommendation(
                        {"title": t.title, "artist": t.artist, "genre": t.genre},
                        "AI 推荐")
                except Exception:  # noqa
                    continue
                if not reason:
                    continue
                entry["reason"] = reason
                self._emit_playlist()

        def _fuzzy_match_local(self, title: str, artist: str, local: list, threshold: int):
            """在本地曲库里为 (title, artist) 找最佳命中；返回 Track 或 None。

            规则（严格保证"落地准确"）：
              - 歌名：精确相等立即命中；否则用 rapidfuzz.token_set_ratio 模糊匹配，
                分数 >= 阈值才纳入候选（token 化吸收括号/版本/Remastered 等后缀）。
              - 作者：给定 artist 时必须「准确或包含」才算数；本地 artist 为空时
                仅允许歌名精确命中（避免把无署名曲模糊误配到别的歌）。
            这样 AI 推荐《Bohemian Rhapsody (Remastered)》也能命中本地《Bohemian Rhapsody》，
            同时不会因作者宽松而张冠李戴。
            """
            if not title:
                return None
            best = None
            best_score = -1
            for t in local:
                tn = (t.title or "").strip().lower()
                an = (t.artist or "").strip().lower()
                if not tn:
                    continue
                # 作者门控：给定 artist 时必须准确或包含；本地无作者信息则仅允许歌名精确命中
                if artist:
                    if an:
                        if not (artist == an or artist in an or an in artist):
                            continue
                    elif tn != title:
                        continue  # 本地 artist 空 + 歌名不精确 -> 不模糊放宽，跳过
                # 歌名精确优先（立即命中，最稳）
                if tn == title:
                    return t
                # 模糊：token_set_ratio 吸收括号/版本后缀
                try:
                    score = _fuzz.token_set_ratio(title, tn)
                except Exception:  # noqa
                    score = 0
                if score >= threshold and score > best_score:
                    best_score = score
                    best = t
            return best

        def _resolve_selections(self, selections: list[dict], n: int,
                                 allow_local_pad: bool = True):
            """把 AI 选中的 {title, artist} 列表检索成真实可播放曲目。

            检索顺序：本地曲库 -> 直链源（网易云/QQ音乐，best-effort）。
            - 命中：加入结果；
            - 检索不到：记录到 missing（绝不抛异常、不报错中断）；
            - 最终若不足 n 首：用本地曲库随机补位（标记 substituted），
              保证推荐列表数量稳定、不空、不卡。

            返回 (tracks, missing_titles, substituted_ids)。
            """
            tracks: list = []
            used: set[int] = set()
            # ★★★ 2026-11-17（用户复报「大模型青睐 Bon Iver 的老问题又出现了」）：
            #   旧去重只比 `hit.id`，而外部源曲目每次都是新 `_ext_seq` 自减 ⇒ id 天然
            #   各不相同，**同歌/同艺人重复根本挡不住**（实测一轮 5 首里 Bon Iver 占 2 席）。
            #   这里加两道**确定性**闸（不依赖模型听话）：
            #     ① 同曲去重：键 = (归一歌名, 归一歌手)，跨源同样生效；
            #     ② 同艺人上限：同一歌手/乐队最多 1 首，超了跳过、由后续候选补位。
            #   因为上游 `_ai_pick_tracks` 一次要 `want+3` 首，跳过 1~2 首仍能凑满 want。
            seen_song: set[tuple[str, str]] = set()
            artist_n: dict[str, int] = {}
            ARTIST_CAP = 1
            dup_skipped: list[str] = []
            missing: list[str] = []
            substituted: set[int] = set()
            local = self._local_pool()
            self._clean_upcoming()  # 防御：保证下方 busy 读取 e["track"].id 安全

            def norm(s):
                return (s or "").strip().lower()

            # 模糊匹配开关与阈值（设置页可调；作者命中保持「准确或包含」，绝不模糊放宽）。
            _thr = 85
            _fuzzy_on = False
            try:
                _thr = int(float(user_config.match.get("fuzzy_threshold", 85)))
                _fuzzy_on = _HAVE_FUZZ and bool(user_config.match.get("fuzzy_enabled", True))
            except Exception:  # noqa
                pass
            _thr = max(50, min(100, _thr))

            for sel in selections:
                title = norm(sel.get("title"))
                artist = norm(sel.get("artist"))
                if not title:
                    continue
                hit = None
                # 1) 本地曲库（优先，命中率最高）：
                #    - 精确匹配歌名（歌手对得上或 AI 没给歌手）立即命中；
                #    - 否则用 rapidfuzz 模糊匹配（token_set_ratio 吸收括号/版本/Remastered
                #      等后缀，如《Bohemian Rhapsody (Remastered)》命中《Bohemian Rhapsody》），
                #      歌手命中严格保持「准确或包含」，防止张冠李戴。
                if _fuzzy_on:
                    hit = self._fuzzy_match_local(title, artist, local, _thr)
                else:
                    for t in local:
                        if norm(t.title) == title and (not artist or artist in norm(t.artist)):
                            hit = t
                            break
                    if hit is None and artist:
                        for t in local:
                            tn = norm(t.title)
                            an = norm(t.artist)
                            if (title in tn or tn in title) and (artist in an or an in artist):
                                hit = t
                                break
                # 2) 直链源 best-effort（仅在后台执行，不会卡主线程）
                if hit is None and self.source_manager:
                    for key in ("netease", "qqmusic"):
                        src = self.source_manager.get(key)
                        if not src or not src.enabled:
                            continue
                        try:
                            res = src.search(sel.get("title", ""))
                        except Exception as e:  # noqa
                            logger.warning("直链源检索失败 %s: %s", key, e)
                            continue
                        if res:
                            r0 = res[0]
                            self._ext_seq -= 1
                            from data.library import Track as _Track
                            # MusicSource.search() returns Track objects.  Keep
                            # the real remote URL; replacing it with "netease:id"
                            # makes PlayerEngine treat the track as a local path.
                            if isinstance(r0, _Track):
                                hit = _Track(
                                    id=self._ext_seq, path=r0.path,
                                    title=r0.title or sel.get("title", ""),
                                    artist=r0.artist or sel.get("artist", ""),
                                    album=r0.album or "", genre=r0.genre or "",
                                    year=r0.year, duration=float(r0.duration or 0),
                                    bpm=r0.bpm, source=r0.source or key,
                                    ext_id=r0.ext_id, lyric=r0.lyric,
                                    # ⚠ R27：重建时丢 cover 会让推荐货架/播放列表
                                    # 全部没有封面（漫游卡片只剩程序化占位图）。
                                    cover=getattr(r0, "cover", None) or None,
                                )
                            else:
                                # Compatibility with older/custom sources that
                                # still return mapping-shaped search results.
                                hit = _Track(
                                    id=self._ext_seq,
                                    path=r0.get("path") or r0.get("url") or
                                         f"{key}:{r0.get('id')}",
                                    title=r0.get("title", sel.get("title")),
                                    artist=r0.get("artist", ""),
                                    album=r0.get("album", ""), genre="", year=None,
                                    duration=float(r0.get("duration", 0) or 0),
                                    bpm=None, source=key,
                                    ext_id=r0.get("ext_id") or r0.get("id"),
                                    cover=r0.get("cover") or None,   # R27：别丢封面
                                )
                            break
                if hit is not None:
                    # ★ 2026-11-17：同曲 / 同艺人闸（见上方 `seen_song` 注）。
                    _key = (norm(getattr(hit, "title", "") or title),
                            norm(getattr(hit, "artist", "") or artist))
                    _an = _key[1]
                    if hit.id in used:
                        continue
                    if _key in seen_song:
                        dup_skipped.append("%s-%s(同曲)" % (_key[0], _an))
                        continue
                    if _an and artist_n.get(_an, 0) >= ARTIST_CAP:
                        dup_skipped.append("%s-%s(同艺人已 %d 首)"
                                           % (_key[0], _an, artist_n.get(_an, 0)))
                        continue
                    used.add(hit.id)
                    seen_song.add(_key)
                    if _an:
                        artist_n[_an] = artist_n.get(_an, 0) + 1
                    tracks.append(hit)
                else:
                    # 检索不到：仅记录，稍后补位；不报错、不中断
                    missing.append(f"《{sel.get('title', '')}》-{sel.get('artist', '')}")
                if len(tracks) >= n:
                    break
            if dup_skipped:
                logger.info("[reco-dedup] 跳过 %d 首：%s", len(dup_skipped),
                            "；".join(dup_skipped[:6]))

            # 不足 n 首：用本地池随机补位（标记为"检索错误"替换曲）。
            # 仅在 allow_local_pad=True 时（离线兜底场景）启用；AI 精排路径传 False，
            # 避免日常推荐列表混入曲库红心占位歌。
            if len(tracks) < n and allow_local_pad:
                pool = [t for t in local if t.id not in used and t.id not in self.disliked
                        and t.id not in self.played and t.id != self.current_id]
                with self._qlock:
                    busy = {e["track"].id for e in self.upcoming}
                pool = [t for t in pool if t.id not in busy]
                # ★ 2026-11-17：补位**也要守上面的两道闸**，否则"跳过同艺人"刚腾出的位置
                #   会被补位随机塞回来的同一艺人补上（实测：pad=True 时 Bon Iver 又变回 3 首）。
                pool = [t for t in pool
                        if (norm(getattr(t, "title", "")),
                            norm(getattr(t, "artist", ""))) not in seen_song
                        and (not norm(getattr(t, "artist", ""))
                             or artist_n.get(norm(getattr(t, "artist", "")), 0) < ARTIST_CAP)]
                random.shuffle(pool)
                for t in pool:
                    if len(tracks) >= n:
                        break
                    if t.id in used:
                        continue
                    used.add(t.id)
                    substituted.add(t.id)
                    tracks.append(t)
            return tracks, missing, substituted

        def _offline_picks(self, cands: list, n: int) -> list:
            """离线候选：依据"曲库播放模式"取歌。

            注意：曲库播放模式（order/random/loop_one）与漫游策略（continuous/leap）
            是两个不同维度的概念，以前把 play_mode 直接当漫游策略传入 RoamEngine，
            导致 random 模式实际并不随机。这里显式区分：
              - random      -> 真随机取样（打破顺序/向量）
              - order/默认  -> 基于当前曲的向量相似（continuous，或风格跳跃 leap）
              - 无当前曲（开场 / AI 关闭 / 兜底）-> 无法做相似推荐，退化为随机取样；
                这是离线同步填充（无 AI 或 AI 失败兜底）的取歌方式，保证列表非空、可播。
                （启用 AI 时 _fill 走纯 AI 路径，不会用本分支提前占位。）
            """
            try:
                if self.play_mode == "random":
                    picks = random.sample(cands, min(n, len(cands)))
                elif hasattr(self.recommend, "suggest") and self.current is not None:
                    # 风格滑块偏"更陌生"时用 leap（风格跳跃），否则 continuous（连续相似）
                    strategy = "leap" if self.familiarity >= 2 else "continuous"
                    picks = self.recommend.suggest(cands, n, strategy, current=self.current)
                else:
                    # 无当前曲（开场 / AI 关闭 / 兜底）：无法做相似推荐，退化为随机取样。
                    picks = random.sample(cands, min(n, len(cands)))
            except Exception:  # noqa
                picks = []
            if len(picks) < n:
                extra = [t for t in cands if t not in picks]
                picks += random.sample(extra, min(n - len(picks), len(extra)))
            return picks[:n]

        def _refill(self) -> None:
            # 手动队列（如曲库「播放全部」）进行中：不 AI 续填，保全用户队列不被截断。
            if getattr(self, "_manual_queue", False):
                return
            with self._qlock:
                cur_exists = self.current is not None
                up_len = len(self.upcoming)
            # 推荐列表（待播）始终保持满 PLAYLIST_SIZE 首；缺口交给 _fill → AI 续推。
            target = RadioConfig.PLAYLIST_SIZE
            if cur_exists and not self.roam_on:
                return  # 电台模式关闭：不自动续推
            need = target - up_len
            if need > 0:
                self._fill(need)

        # ---------- 入口：开始电台 ----------

        def _refill_initial(self) -> None:
            """初始：填满 PLAYLIST_SIZE 首待播。"""
            self._fill(RadioConfig.PLAYLIST_SIZE)

        # ---------- 推荐货架（reco_buffer）：AI 常驻思考、不自动播 ----------
        # ╔══════════════════════════════════════════════════════════════════════╗
        # ║ 所有权模型（务必遵循，避免回归）                                    ║
        # ║ 1) AI 推荐(reco)：推荐页 reco_buffer「独占」。只由 _ai_pick_tracks +   ║
        # ║    _ai_reco_fill 填充，绝不进播放列表；播放列表仅靠 _fill_from_reco   ║
        # ║    从推荐页「复制」到 upcoming。即 AI推荐 → 推荐页 → 播放列表 → 播放。║
        # ║    复制语义：一首歌可同时存在于推荐页和播放列表，直到真正播放才从推荐页清理， ║
        # ║    保证推荐页始终满架、持续流动，而不是被播放列表一次搬空。              ║
        # ║ 1.5) 播放列表「绝不」用本地曲库兜底：进入播放列表的每首歌都必须先出现在推荐页。 ║
        # ║    缺口留空由推荐页回补 + 稳态 _refill（及 _ai_reco_fill 收尾触发的 _refill）  ║
        # ║    补入。AI 彻底不可用（推荐页始终空）时电台保持静默、由 UI 提示，绝不拿本地    ║
        # ║    曲库冒充推荐。这样播放列表里绝不会出现推荐页没有的歌。                  ║
        # ║ 2) AI 口播(broadcast)：全局 DJ 功能，锚定「播放列表 upcoming」——      ║
        # ║    只由 _prep_next_broadcast 为 upcoming 里的歌生成口播/转场，绝不为   ║
        # ║    reco_buffer 单独生成。所有口播「以播放列表为准」。                  ║
        # ║ 3) 冷启动例外：刚开软件时播放列表尚空、无法预取口播，故「初始加载归    ║
        # ║    推荐页管」——先让 reco_buffer 填满（_refill_reco），以便第一时间     ║
        # ║    出具首段口播；一旦开始播放（_after_music_started），口播即归播放    ║
        # ║    列表管（为 upcoming 里的歌预取）。                                  ║
        # ╚══════════════════════════════════════════════════════════════════════╝
        # 推荐页只由 AI 填充，绝不回退本地曲库：AI 偶发空/网络抖动/尚未就绪时自动重试，
        # 直至补齐或重试耗尽（耗尽则保留已获得的部分，由 UI 占位提示说明）。
        RECO_MAX_ATTEMPTS = 4          # AI 补货最大尝试次数（含首次）
        RECO_RETRY_BASE_S = 3.0        # 重试基础间隔（秒），逐次翻倍退避
        RECO_REFILL_MIN = 3            # ★ 2026-11-18：货架低于此数才补货（防频繁联网+快速切歌切空）

        def _refill_reco(self, attempt: int = 1) -> None:
            """补满推荐货架 reco_buffer 到 PLAYLIST_SIZE（仅 AI 填充，不回退本地曲库）。

            与播放队列（upcoming）是「复制共存」关系：货架里的歌可以同时被复制到播放队列，
            一首歌只有在真正被播放（进入 current/played）或被 dislike 时才会从货架清理。
            因此货架始终面向用户展示、保持「满架 + 流动刷新」；LLM 开启走后台 AI 选歌；
            LLM 尚未就绪（预热中）则定时重试，待其就绪后由 AI 补齐；LLM 确认未启用则保持空
            （由占位提示说明），绝不拿本地曲库冒充推荐。
            并发调用会被合并：若已有后台补货在进行，只累加 pending 需求，避免重复线程。
            """
            self._clean_reco()
            with self._qlock:
                _have = len(self.reco_buffer)
                need = RadioConfig.PLAYLIST_SIZE - _have
            # ★★ 2026-11-18（用户「货架少于一定数量再补一次，防快速切歌切空」）：
            #   不再缺1首就补1首（太频繁、每次都要联网+LLM ~100s）。改为低于阈值
            #   （RECO_REFILL_MIN=3）才触发，一次补满到 PLAYLIST_SIZE(5)。
            #   货架还有≥3首时不折腾 LLM，快速切歌也有缓冲、不会切空。
            if _have >= self.RECO_REFILL_MIN:
                self._emit_reco()
                return
            if need <= 0:
                self._emit_reco()
                return
            if self._reco_loading:
                self._reco_pending_need = max(self._reco_pending_need, need)
                return
            self._reco_pending_need = 0
            llm_enabled = bool(getattr(self.brain, "llm", None)
                               and self.brain.llm.enabled)
            if llm_enabled:
                # 置位 loading 后再起线程，避免 ensure_reco 等并发调用重复起线程。
                self._reco_loading = True
                threading.Thread(target=timed(self._ai_reco_fill, "ai_reco_fill"),
                                 args=(need, 1), daemon=True).start()
            else:
                # LLM 未就绪 / 未启用：不回退本地曲库。仍在预热则定时重试；
                # 重试耗尽（确认未启用）则保持空，由 UI 占位提示说明。
                if attempt < self.RECO_MAX_ATTEMPTS and self._reco_retry_timer is None:
                    self._emit("reco_loading", True)
                    self._schedule_reco_retry(attempt + 1)
                else:
                    self._emit_reco()

        def _schedule_reco_retry(self, attempt: int) -> None:
            """LLM 尚未就绪时，延时重试补货（避免回退本地曲库）。"""
            if self._reco_retry_timer is not None:
                return
            def _go() -> None:
                self._reco_retry_timer = None
                self._reco_loading = False
                self._refill_reco(attempt)
            self._reco_retry_timer = threading.Timer(self.RECO_RETRY_BASE_S, _go)
            self._reco_retry_timer.daemon = True
            self._reco_retry_timer.start()

        def _ai_pick_tracks(self, want: int) -> list:
            """AI 选歌核心：返回 recommend_selections → 回库检索 得到的真实可播 Track 列表。

            ⚠️ 这是「推荐页独占」的 AI 推荐入口：只能由推荐货架管线
            （_refill_reco → _ai_reco_fill）调用，用于填充 reco_buffer。
            播放列表(upcoming)「绝不」直接调用本方法补货——它只从 reco_buffer 引流
            （见 _fill_from_reco）。这样 AI 推荐始终先落地推荐页，再统一进入播放列表，
            避免推荐页与播放列表变成两条割裂的推荐流。失败/空返回 []。
            """
            if not getattr(self.brain, "llm", None) or not self.brain.llm.enabled:
                logger.warning("[reco-pick] LLM 未启用/未接入 → 选歌直接返回空（货架只靠 AI）")
                return []
            # ★★ 2026-11-18（用户「第一批从网易云挑，之后 AI 基于品味干活」）：
            #   冷启动（推荐页+播放列表都空）**不同步等品味分析**（实测 96s，会堵死
            #   第一批推荐）。品味分析已在启动时由 warmup_llm 后台发起；第一批走
            #   网易云快路径直出。稳态补货（reco_buffer 非空）才同步 _ensure_taste，
            #   确保后续 AI 用品味画像挑歌——绝不闲置 AI。
            with self._qlock:
                _cold_start = (not self.reco_buffer and not self.upcoming)
            if not _cold_start:
                self._ensure_taste()
            # ★★ 2026-11-17：**检索优先**（用户拍板"选歌必须调 API，否则只有模型训练语料"）。
            #   候选由平台接口召回、实体不由模型提供 ⇒ 结构上不可能编造。
            #   任何一环失败都静默回退到下面的旧路径，绝不比现状更差。
            if reco_grounded_enabled():
                try:
                    got = self._ai_pick_grounded(int(want))
                except Exception as e:                            # noqa
                    logger.warning("[reco] 检索优先选歌异常（回退旧路径）：%s", e)
                    got = []
                if got:
                    return self._audit_reco_tracks(got, self.familiarity)
                logger.info("[reco] 检索优先无结果 → 回退自由生成")
            sel_n = max(1, int(want)) + 3
            try:
                taste = self.taste_profile or {}
                local_pool = self._local_pool()
                # 可达外部源（网易云 / QQ / Spotify）是否至少启用一个。全部关闭时，
                # AI 的可达宇宙 = 本地曲库——否则 AI 只能提议曲库外的歌、检索全部落空、
                # 货架空转（R76 诊断实证：LLM 每轮 8/8 提议、8/8 未落地 → 货架永久空）。
                ext_ok = bool(
                    getattr(self, "source_manager", None)
                    and any(
                        (getattr(self.source_manager.get(k), "enabled", False))
                        for k in ("netease", "qqmusic", "spotify")
                        if self.source_manager.get(k) is not None))
                if ext_ok:
                    # 有在线源可探索曲库外：库内标题只给前 60 做锚点；并排除用户已收藏，
                    # 避免推荐页退化成「喜欢列表补货」（外部歌补位，货架保持发现感）。
                    lib_titles = [f"{t.title} - {t.artist or '未知'}"
                                  for t in local_pool[:60]]
                    pad = False
                else:
                    # 全离线：本地曲库 = 唯一可播宇宙 → 标题尽量多给 AI 命中（不再只给
                    # 60 首），且**不**排除收藏（曲库≈「我喜欢的音乐」，排除即候选清零）；
                    # 检索缺口允许本地补位（allow_local_pad），保证货架/电台不为空。
                    lib_titles = [f"{t.title} - {t.artist or '未知'}"
                                  for t in local_pool[:120]]
                    pad = True
                disliked = self._disliked_titles()
                # 排除：当前曲 + 已推荐货架里的歌 + 本会话已播/历史，避免「每次补货都点同一批」
                # （配合 brain 层注入的随机种子，破解「多次开播推荐都一样」的确定性）。
                with self._qlock:
                    reco_titles = [e["track"].title for e in self.reco_buffer
                                   if self._is_track(e.get("track")) and e["track"].title]
                exclude = []
                if self.current is not None and self.current.title:
                    exclude.append(self.current.title)
                exclude += reco_titles
                exclude += [t.title for t in self.library.tracks if t.id in self.played]
                exclude += [t.title for t in self.history[:20]
                            if getattr(t, "title", None)]
                # 仅在有在线源时排除收藏；全离线时曲库即候选宇宙，排除会把 AI 逼到
                # 「提议无法检索的外歌」→ 货架空（R76 修复前每次开播都复现）。
                if ext_ok:
                    try:
                        for _fid in self.db.favorite_ids():
                            _fr = self.db.get_track(_fid)
                            if _fr and _fr["title"]:
                                exclude.append(_fr["title"])
                    except Exception:  # noqa
                        pass
                exclude_titles = exclude or None
                seed = random.randint(1, 1_000_000)
                sels = self.brain.recommend_selections(
                    self._reco_ctx(), sel_n, self.familiarity, taste, lib_titles,
                    disliked_titles=disliked, exclude_titles=exclude_titles,
                    seed=seed)
            except Exception as e:  # noqa
                logger.warning("AI 选歌提议失败: %s", e)
                self._note_llm_failure("recommend", f"选歌推荐：{e}")
                return []
            if not sels:
                logger.info("[reco-pick] LLM 首轮提议为空（want=%d 档=%s），重试一次",
                            want, self.familiarity)
                try:
                    sels = self.brain.recommend_selections(
                        self._reco_ctx(), sel_n, self.familiarity, taste, lib_titles,
                        disliked_titles=disliked, exclude_titles=exclude_titles,
                        seed=random.randint(1, 1_000_000))
                except Exception:  # noqa
                    sels = []
            if not sels:
                logger.warning("[reco-pick] LLM 两轮提议均为空（want=%d），本次返回空货架",
                               want)
                return []
            logger.info("[reco-pick] LLM 提议 %d/%d 首", len(sels), sel_n)
            try:
                tracks, _missing, _sub = self._resolve_selections(
                    sels, want + 3, allow_local_pad=pad)
            except Exception as e:  # noqa
                logger.warning("检索推荐曲目失败: %s", e)
                return []
            if not tracks:
                logger.warning(
                    "[reco-pick] LLM 提议 %d 首全部未落地（missing 示例: %s）→ 本次返回空",
                    len(sels), _missing[:3])
            else:
                logger.info("[reco-pick] 落地 %d/%d 首（missing=%d sub=%d）",
                            len(tracks), len(sels), len(_missing), len(_sub))
            # 审核：曲库 > 100 首时按熟悉度档位控占比 + 跳过不喜欢的歌
            _pre_audit = len(tracks)
            tracks = self._audit_reco_tracks(tracks, self.familiarity)
            if not tracks and _pre_audit:
                logger.warning("[reco-pick] 审核裁剪后为空（审核前 %d 首，疑似全被不喜欢/占比裁掉）",
                               _pre_audit)
            return tracks or []

        # ---------------- 检索优先的选歌（2026-11-17） ----------------

        def _reco_window(self):
            """播放列表窗口 = 正在播 + 待播 + 推荐货架。

            去重只在这个窗口内做（用户 2026-11-17 拍板）：窗口外的历史不管，
            否则"上批来过就排除"会白白错过好歌。返回 (ids, artists, current, upcoming)。
            """
            with self._qlock:
                self._clean_upcoming()
                current = self.current if self._is_track(self.current) else None
                ups = [e["track"] for e in self.upcoming
                       if self._is_track(e.get("track"))]
                shelf = [e["track"] for e in self.reco_buffer
                         if self._is_track(e.get("track"))]
            win = ([current] if current is not None else []) + ups + shelf
            # ★ 2026-11-17 实测：只按"窗口"去重，同一首歌会在 4 首之后被重新点回来
            #   （8 轮测试里第 8 轮点了第 4 轮那首）。艺人仍只看窗口（用户口径不动），
            #   但**同一首歌**额外看最近 N 首已播，避免"刚放完又点回来"。
            try:
                _n = max(0, int(os.environ.get("MUSICAI_RECO_SONG_HISTORY", "8")))
            except (TypeError, ValueError):
                _n = 8
            recent = ([t for t in list(self.history[:_n]) if self._is_track(t)]
                      if _n else [])
            ids = {t.id for t in win} | {t.id for t in recent}
            artists = {(getattr(t, "artist", "") or "").strip()
                       for t in win if (getattr(t, "artist", "") or "").strip()}
            return ids, artists, current, ups

        def _reco_seeds(self, limit: int = 3) -> list:
            """相似歌曲的种子：最近播放优先，不够再拿收藏补。"""
            out, seen = [], set()
            for t in list(self.history[:6]):
                ext = getattr(t, "ext_id", None)
                if (getattr(t, "source", "") or "") != "netease" or not ext:
                    continue
                try:
                    sid = int(ext)
                except (TypeError, ValueError):
                    continue
                if sid in seen:
                    continue
                seen.add(sid)
                out.append((getattr(t, "title", ""), getattr(t, "artist", ""), sid))
                if len(out) >= limit:
                    return out
            for r in self._reco_favorite_rows(limit):
                if r["ext_id"] in seen:
                    continue
                seen.add(r["ext_id"])
                out.append((r["title"], r["artist"], r["ext_id"]))
                if len(out) >= limit:
                    break
            return out

        def _reco_favorite_rows(self, limit: int = 12) -> list:
            """用户收藏里带网易云 ext_id 的（作为「熟悉」组的候选锚点）。"""
            rows = []
            try:
                fav_ids = list(self.db.favorite_ids())
            except Exception:                                     # noqa
                return rows
            random.shuffle(fav_ids)
            for fid in fav_ids[:max(limit * 3, 24)]:
                try:
                    r = self.db.get_track(fid)
                except Exception:                                 # noqa
                    continue
                if not r:
                    continue
                # db.get_track 返回 sqlite3.Row（支持下标、不支持 .get）
                def _g(key, default=""):
                    try:
                        v = r[key]
                    except (KeyError, IndexError, TypeError):
                        return default
                    return default if v is None else v
                if (_g("source") or "") != "netease" or not _g("ext_id"):
                    continue
                try:
                    rows.append({"ext_id": int(_g("ext_id")),
                                 "title": _g("title"),
                                 "artist": _g("artist"),
                                 "album": _g("album"),
                                 "year": _g("year")})
                except (TypeError, ValueError):
                    continue
                if len(rows) >= limit:
                    break
            return rows

        def _llm_ready_fast(self) -> bool:
            """0.4s 内判断本地模型是否醒着。

            模型是"用时唤醒、闲时睡眠"的：端口没开时 `chat()` 会立刻失败，
            但**正在加载**时可能挂到超时。这里先探端口，避免推荐线程被拖住。
            """
            llm = getattr(self.brain, "llm", None)
            if llm is None or not getattr(llm, "enabled", False):
                return False
            backend = (getattr(llm, "backend", "") or "").lower()
            if backend not in ("llamacpp", "ollama"):
                return True                       # API 后端：直接试
            host = (getattr(llm, "llamacpp_host", "") if backend == "llamacpp"
                    else getattr(llm, "ollama_host", "")) or ""
            try:
                u = urlparse(host)
                h = u.hostname or "127.0.0.1"
                p = u.port or (8080 if backend == "llamacpp" else 11434)
            except Exception:                                     # noqa
                h, p = "127.0.0.1", 8080
            try:
                with socket.create_connection((h, int(p)), timeout=0.4):
                    return True
            except OSError:
                return False

        def _ai_pick_grounded(self, want: int) -> list:
            """检索优先：平台召回 → 本地 LLM 只挑 #ID → 校验 → 造 Track。

            返回 Track 列表（可能少于 want）；拿不到候选 / 造不出 Track 都返回 []，
            由调用方回退旧的自由生成路径。
            """
            from ai import reco_pool

            want = max(1, int(want))
            sm = getattr(self, "source_manager", None)
            src = sm.get("netease") if sm else None
            if src is None or not getattr(src, "enabled", False):
                return []
            if (getattr(src, "backend", "") or "").lower() != "musicbox":
                return []

            blocked_ids, blocked_artists, current, ups = self._reco_window()
            taste = self.taste_profile if isinstance(self.taste_profile, dict) else {}
            tags = taste.get("品味关键词") or taste.get("tags") or []
            tags = [str(x) for x in tags[:2]] if isinstance(tags, list) else []

            # ★★★ 2026-11-18（用户「冷启动直接从网易云 recommend API 挑」）：
            #   冷启动（推荐页+播放列表都空）时，用 build_pool_fast 只调每日推荐(0.5s)，
            #   跳过会卡住 98s 的相似歌曲/歌手热歌慢召回，且每日推荐已排序、不需 LLM 挑
            #   ⇒ 推荐秒出，保证第一首歌放完前第二首导听就绪。稳态补货仍走完整挑歌。
            with self._qlock:
                _cold = (not self.reco_buffer and not self.upcoming)
            _cold_fast = False
            if _cold:
                try:
                    fast = reco_pool.build_pool_fast(
                        src, blocked_ids=blocked_ids, blocked_artists=blocked_artists,
                        familiarity=self.familiarity, limit=max(12, want * 3))
                    if fast:
                        # ★ 2026-11-18（用户「推荐直出别取前五首，库里随机取五首」）：
                        #   每日推荐直出保速度，但按分数排序取前5首每次都一样、缺乏发现感。
                        #   从候选池里随机抽 want 首（候选已过滤窗口内重复/艺人），既快又有变化。
                        import random as _rnd
                        cands = _rnd.sample(fast, min(want, len(fast)))
                        _cold_fast = True
                        logger.info("[reco·cold] 冷启动快速选歌 %d 首（每日推荐直出·随机抽，跳过LLM挑）",
                                    len(cands))
                except Exception as e:  # noqa
                    logger.warning("[reco·cold] 快速选歌失败，回退完整流程：%s: %s",
                                   type(e).__name__, e)

            t0 = time.perf_counter()
            if not _cold_fast:
                cands = reco_pool.build_pool(
                    src, seeds=self._reco_seeds(), tags=tags,
                    favorite_rows=self._reco_favorite_rows(),
                    blocked_ids=blocked_ids, blocked_artists=blocked_artists,
                    familiarity=self.familiarity, limit=max(24, want * 8),
                    min_pool=max(6, want * 4))
            if not cands:
                logger.info("[reco·grounded] 候选池为空（窗口挡住 %d 首 / %d 位艺人）",
                            len(blocked_ids), len(blocked_artists))
                return []
            logger.info("[reco·grounded] 候选 %d 首，召回 %.1fs（窗内挡 %d 首 / %d 位艺人）",
                        len(cands), time.perf_counter() - t0,
                        len(blocked_ids), len(blocked_artists))

            label = RadioConfig.FAMILIARITY_LABELS[max(0, min(3, self.familiarity))]
            picks: list = []
            # ★ 冷启动快速路径：每日推荐已排序，跳过 LLM 挑（用户「冷启动不思考直接挑」）
            if self.brain.llm.enabled and self._llm_ready_fast() and not _cold_fast:
                try:
                    picks = self.brain.pick_from_candidates(
                        cands, want, ctx=self._reco_ctx(), taste=taste,
                        familiar_label=label, current=current, upcoming=ups,
                        familiarity=self.familiarity, recent=self.history[:3])
                except Exception as e:                            # noqa
                    logger.warning("[reco·grounded] 模型挑选异常：%s", e)
                    picks = []
                errs, warns = (reco_pool.validate_picks(picks, cands, want)
                               if picks else (["模型未给出结果"], []))
                if errs and picks:
                    # 只重试一次，并把不合格原因回喂（"少给了 N 首"/"ID 不在表里"）
                    logger.info("[reco·grounded] 首轮不合格（%s）→ 回喂一次",
                                "；".join(errs))
                    try:
                        picks = self.brain.pick_from_candidates(
                            cands, want, ctx=self._reco_ctx(), taste=taste,
                            familiar_label=label, current=current, upcoming=ups,
                            familiarity=self.familiarity, recent=self.history[:3],
                            feedback="；".join(errs))
                    except Exception:                             # noqa
                        picks = []
                    errs, warns = (reco_pool.validate_picks(picks, cands, want)
                                   if picks else (["模型未给出结果"], []))
                for w in warns or []:
                    logger.info("[reco·grounded] 软提示：%s", w)
                if errs:
                    logger.info("[reco·grounded] 两轮均不合格（%s）→ 用平台排序",
                                "；".join(errs))
                    picks = []
            else:
                logger.info("[reco·grounded] 模型未就绪 → 直接用平台排序（不等待唤醒）")

            by_id = {c.id: c for c in cands}
            if not picks:
                picks = [{"id": c.id, "reason": ""} for c in cands[:want]]

            reasons = getattr(self, "_reco_reasons", None)
            if reasons is None:
                reasons = self._reco_reasons = {}
            elif len(reasons) > 200:                 # 只缓存"最近推过的"，别无限长
                reasons.clear()
            tracks = []
            for p in picks:
                c = by_id.get(p.get("id"))
                if c is None:
                    continue
                tr = reco_pool.to_track(src, c)
                if tr is None:
                    continue
                reason = (p.get("reason") or "").strip()
                if reason:
                    reasons[tr.id] = reason
                tracks.append(tr)
                if len(tracks) >= want:
                    break
            logger.info("[reco·grounded] 产出 %d/%d 首（%s）",
                        len(tracks), want,
                        "模型挑选" if any(p.get("reason") for p in picks) else "平台排序")
            return tracks

        def _ai_reco_fill(self, want: int, attempt: int = 1) -> None:
            """后台：为推荐货架拉取 AI 推荐并入库（仅 AI，不回退本地曲库）。

            AI 选歌异常 / 返回空（Ollama 抖动、超时、或建议未在曲库解析到）时，不再用
            本地曲库冒充推荐——而是重试（最多 RECO_MAX_ATTEMPTS 次），最终补齐或保留
            已获得的部分；推荐页在 AI 思考期间以「加载中」呈现，AI 返回即刷新。
            """
            self._reco_loading = True
            self._emit("reco_loading", True)
            # ★★ 2026-11-18（用户「货架少于阈值再补，用品味种子，防切空」）：
            #   优先消费品味分析时顺便产出、已验证过的推荐种子（秒填，不用联网+LLM）。
            #   种子不够才走下面的联网召回+LLM挑。种子用完自动清空，之后恢复正常补货。
            self._fill_reco_from_taste_picks()
            with self._qlock:
                _still = RadioConfig.PLAYLIST_SIZE - len(self.reco_buffer)
            if _still <= 0:
                logger.info("[reco-fill] 品味种子已补满货架，跳过联网选歌")
                self._reco_loading = False
                self._emit("reco_loading", False)
                self._emit_reco()
                return
            want = _still
            try:
                tracks = self._ai_pick_tracks(want)
            except Exception as e:  # noqa
                logger.warning("推荐货架 AI 选歌异常，稍后重试: %s", e)
                self._note_llm_failure("recommend", f"推荐货架：{e}")
                tracks = []
            appended = 0
            with self._qlock:
                existing = {e["track"].id for e in self.reco_buffer
                           if self._is_track(e.get("track"))}
                for t in tracks:
                    # 实时重算排除集：防止补货期间当前曲/已播/不喜欢进入推荐页；
                    # 不再排除 upcoming——播放列表与推荐页是「复制共存」关系，一首歌可以
                    # 同时出现在两个列表里，直到它被真正播放才从推荐页清理。
                    # 同时排除「不喜欢的歌」隐藏库（覆盖外部歌，用稳定键）：
                    # 与 _audit_reco_tracks 的避雷保持一致，防止并发间隙漏网。
                    exclude = (set(self.played) | {self.current_id} | self.disliked)
                    if (t.id in existing or t.id in exclude
                            or self.fav_key(t) in self.disliked_keys):
                        continue
                    # 2026-11-17：检索优先路径会给每首配一句"为什么选它"，
                    # 直接当推荐理由用 ⇒ 不再为每首歌单独调一次 explain_recommendation。
                    _reason = (getattr(self, "_reco_reasons", {}) or {}).get(t.id) or "AI 推荐"
                    self.reco_buffer.append(self._make_entry(t, _reason))
                    existing.add(t.id)
                    appended += 1
                    if len(self.reco_buffer) >= RadioConfig.PLAYLIST_SIZE:
                        break
            with self._qlock:
                still_need = RadioConfig.PLAYLIST_SIZE - len(self.reco_buffer)
            logger.info("[reco-fill] attempt=%d want=%d 实际入库=%d（货架 %d/%d 仍缺=%d）",
                        attempt, want, appended,
                        RadioConfig.PLAYLIST_SIZE - still_need,
                        RadioConfig.PLAYLIST_SIZE, still_need)
            self._reco_loading = False
            self._emit("reco_loading", False)
            self._emit_reco()
            # 推荐页填满后：若电台正在播放或正在开场（current 非空 或 等待开场），立即把
            # 新推荐复制到播放列表并（如需）开播，形成「推荐页→播放列表」的流动闭环。否则
            # （仅打开软件未自动开播，current 为空且非等待开场）不动播放列表，保持空、等用户
            # 点歌。这样冷启动时无需本地曲库兜底也能从填满的推荐页开播，避免「推荐页没有的歌
            # 混进播放列表」。
            if ((self.current is not None or getattr(self, "_need_start", False))
                    and not self._manual_queue):
                self._refill()
            # 仍不足：AI 偶发空/网络抖动 → 自动重试补齐，绝不回退本地曲库。
            if still_need > 0 and attempt < self.RECO_MAX_ATTEMPTS:
                if self._reco_retry_timer is None:
                    def _go() -> None:
                        self._reco_retry_timer = None
                        self._ai_reco_fill(want, attempt + 1)
                    self._reco_retry_timer = threading.Timer(
                        self.RECO_RETRY_BASE_S * attempt, _go)
                    self._reco_retry_timer.daemon = True
                    self._reco_retry_timer.start()
            elif self._reco_pending_need > 0:
                # 补货期间若有新的缺口需求，继续补
                pending = self._reco_pending_need
                self._reco_pending_need = 0
                self._refill_reco()

        def _clean_reco(self) -> int:
            """清理推荐货架：移除已失效、已播、当前曲或被不喜欢的条目。

            注意：不再因为一首歌同时在播放列表（upcoming）中就把它从推荐页清理掉。
            推荐页与播放列表是「复制」关系而非「互斥」关系，一首歌可以同时存在于两个列表，
            直到它被真正播放（进入 played/current）或 dislike 才从推荐页移出。
            返回被移除的数量，供调用方判断是否需要触发补货。
            """
            removed = 0
            with self._qlock:
                exclude = (set(self.played) | {self.current_id} | self.disliked)
                cleaned = []
                for e in self.reco_buffer:
                    t = e.get("track")
                    if not self._is_track(t) or t.id in exclude:
                        removed += 1
                        continue
                    cleaned.append(e)
                self.reco_buffer = cleaned
            return removed

        def get_reco_entries(self) -> list[dict]:
            """推荐页展示用 entries（不含已播/当前曲/不喜欢的歌）。

            播放列表（upcoming）中的歌仍会在推荐页展示——二者是复制共存关系，
            推荐页保持满架与流动感，直到歌曲真正被播放才消失。
            """
            entries = []
            with self._qlock:
                exclude = (set(self.played) | {self.current_id} | self.disliked)
                for e in self.reco_buffer:
                    t = e.get("track")
                    if not self._is_track(t) or t.id in exclude:
                        continue
                    entries.append({
                        "id": t.id, "title": t.title,
                        "artist": t.artist or "未知", "current": False,
                        "duration": getattr(t, "duration", 0) or 0,
                        "source": getattr(t, "source", "local") or "local",
                        "ext_id": getattr(t, "ext_id", None),
                        "cover": getattr(t, "cover", None) or "",
                        "album": getattr(t, "album", "") or "",
                        "favorited": self.fav_key(t) in self._queue_fav,
                        "disliked": self.fav_key(t) in self.disliked_keys,
                    })
            return entries

        def _emit_reco(self) -> None:
            now = time.monotonic()
            if self._reco_timer is not None:
                # 已排了一次延迟合并发射，仅标记脏，等它触发即可
                self._reco_dirty = True
                return
            if now - self._last_reco_emit < 0.12:
                # 发射过快：推迟 120ms 合并一次，避免主线程序建控件过频导致卡顿
                self._reco_dirty = True
                self._reco_timer = threading.Timer(0.12, self._flush_reco)
                self._reco_timer.daemon = True
                self._reco_timer.start()
                return
            self._last_reco_emit = now
            self._emit("reco_updated", self.get_reco_entries())

        def _flush_reco(self) -> None:
            """定时器回调：把最近一次「脏」的推荐页合并发射一次。"""
            self._reco_timer = None
            if self._reco_dirty:
                self._reco_dirty = False
                self._last_reco_emit = time.monotonic()
                self._emit("reco_updated", self.get_reco_entries())

        def ensure_reco(self) -> None:
            """启动/补满推荐货架（AI 常驻思考）。低于 PLAYLIST_SIZE 即补，保持推荐页
            常驻有歌——即用户要求的「播放一首、补充一首」连续流，绝不因某次消费而清空。"""
            with self._qlock:
                below = len(self.reco_buffer) < RadioConfig.PLAYLIST_SIZE
            if below and not self._reco_loading:
                self._refill_reco()

        def play_from_reco(self, track_id) -> None:
            """推荐页点播：被点歌曲成为当前曲并立即播，其余推荐仍保留在货架上，
            并同时复制到播放队列。推荐页不清空，只移除已被点的那一首（随后会被
            _refill_reco 补齐）。不等待 LLM 口播加载即播。

            ⚠ 本轮修复「点了 A 却播了 B / 点了没反应」两个表现：
              1) id 比较统一按字符串归一化 —— QWebChannel 传来的可能是 int 也可能是
                 str，而 reco 曲目 id 是临时负数，此前 `e["track"].id == track_id`
                 一旦类型不一致就静默落空，走进「不在货架 → 只补货、不播」的分支，
                 前端表现为点了没反应或随后播了别的歌。
              2) 货架里找不到时回退到播放队列（upcoming）按身份跳转 —— 推荐货架与
                 队列是「复制共存」，被点过的歌可能已经只存在于队列里。
            """
            key = str(track_id)
            with self._qlock:
                clicked = next((e for e in self.reco_buffer
                                if self._is_track(e.get("track"))
                                and str(e["track"].id) == key), None)
            if clicked is None:
                # 货架里没有 → 该曲可能已只在播放队列里，按身份跳到它（不靠下标）
                with self._qlock:
                    qi = next((i for i, e in enumerate(self.upcoming)
                               if self._is_track(e.get("track"))
                               and str(e["track"].id) == key), None)
                if qi is not None:
                    self.jump_to(qi)
                    return
                # 确实不在任何列表：补满货架并刷新，宁可不播也不播错歌
                self._refill_reco()
                return
            with self._qlock:
                # 其余推荐保留在货架，并复制到播放队列（复制语义：货架不空）
                remaining = [e for e in self.reco_buffer
                             if self._is_track(e.get("track"))
                             and str(e["track"].id) != key]
                self.upcoming = remaining
            # ★ 2026-09-19：来源 = 推荐 ⇒ 强制 order（口播契约）
            self.set_queue_src("reco")
            self._emit_reco()           # 货架仍在，UI 刷新
            self._refill_reco()         # AI 后台补齐被点走的那一首
            self.start_radio(seed_track=clicked["track"], auto_start=True)

        def remove_from_reco(self, track_id: int) -> None:
            """推荐页叉号「不播放」：从推荐货架移除该曲（仅不播放这首，不标记不喜欢）。

            ═══ R55 语义修正（用户原话："推荐曲目里面点叉删除的没有在播放列表
            里面删除，这个逻辑也需要优化"）═══
            旧行为：只从 reco_buffer（推荐货架）移除；推荐货架与播放队列是
            「复制共存」—— 删除货架**不影响已复制进队列/正在播的同一首**，
            用户点完叉发现这首照常在播放列表里排队甚至照常开播。
            新行为：货架移除后**同步清理播放队列**：
              · 正在播放的就是这首 → next() 跳过（复用 remove_from_queue 语义）
              · 该曲在 upcoming 待播 → 一并剔除，并刷新列表
            货架随后由 _refill_reco 补齐；"标记不喜欢"仍由单独的 👎 按钮负责。"""
            # 与 play_from_reco 一致：id 统一按字符串比较，避免 int/str 不一致静默漏删
            key = str(track_id)
            with self._qlock:
                before = len(self.reco_buffer)
                self.reco_buffer = [
                    e for e in self.reco_buffer
                    if not (self._is_track(e.get("track"))
                            and str(e["track"].id) == key)]
                removed = before != len(self.reco_buffer)
            if removed:
                self._emit_reco()
                self._refill_reco()   # 维持满架：补一首新的待荐
            # ═══ R55：同步清播放队列（当前曲 / 待播）═══
            # 正在播的就是这首 → 切下一首跳过；在待播里 → 剔除并刷新。
            # 语义与 remove_from_queue 对齐：先防御性清理 upcoming 脏条目，
            # 再按"是否当前曲"分流；next() 触发完整切歌，必须在 _qlock 外调用。
            try:
                self._clean_upcoming()
            except Exception:  # noqa 防御：极端脏数据不应阻断删除
                pass
            if str(self.current_id) == key:
                try:
                    self.next()
                except Exception:  # noqa 极端情况：队列空/未起播等
                    logger.exception("remove_from_reco 跳过当前曲失败")
                return
            with self._qlock:
                qbefore = len(self.upcoming)
                self.upcoming = [
                    e for e in self.upcoming
                    if not (self._is_track(e.get("track"))
                            and str(e["track"].id) == key)]
                qremoved = qbefore != len(self.upcoming)
            if qremoved:
                self._refill()
                self._emit_playlist()

        def play_all_reco(self) -> None:
            """推荐页「播放全部」：从货架第一首开始播放，整批推荐复制到播放队列。

            与 play_from_reco 一致采用复制语义：推荐货架仍保留这些条目，
            随着歌曲逐首播放才逐首清理并补新货。"""
            with self._qlock:
                first = (self.reco_buffer[0]["track"].id
                         if self.reco_buffer and self._is_track(
                             self.reco_buffer[0].get("track")) else None)
            if first is None:
                self._refill_reco()
                return
            self.play_from_reco(first)

        def play_all_library(self) -> None:
            """曲库「播放全部」：把曲库（我喜欢的音乐）整批替换播放队列并从第一首开播。
            符合「所有待播放曲目进播放列表、从队列播放」。手动队列期间不 AI 续填（保全用户队列），
            播完后再交还 AI 续推。"""
            tracks = self.library.favorites() or self.library.tracks
            if not tracks:
                self._emit("dj_speech", "曲库为空，无法播放全部。")
                return
            # 限制单次入队数量，避免超大队列拖垮 UI（其余由播完后的 AI 续推补足）。
            cap = 100
            # ★ 2026-09-19：**random 必须在建队列时打乱**（否则"播放全部"永远按曲库顺序，
            #   随机按钮等于没生效 —— 用户真机反馈）。见 radio._shuffle_if_random 的说明。
            picks = self._shuffle_if_random(tracks[:cap])
            with self._qlock:
                self.upcoming = [self._make_entry(t, "曲库播放") for t in picks[1:]]
                self._manual_queue = True
            self.set_queue_src("library")   # ★ 来源=曲库 ⇒ 顺序/随机/单曲循环都允许
            self._emit_playlist()
            self.start_radio(seed_track=picks[0], auto_start=True)

        def play_tracks(self, tracks: list, reason: str = "歌单播放") -> None:
            """播放任意一批 Track（歌单 / 收藏 / 本地歌单等）：替换播放队列并从第一首开播。

            与 play_all_library 一致：手动队列期间不 AI 续填（保全用户队列），播完后再交还
            AI 续推。tracks 为 Track 对象列表（可含远程占位曲目，播放时由 source_manager
            按 track.source 解析实时直链）。
            """
            if not tracks:
                self._emit("dj_speech", "没有可播放的曲目。")
                return
            cap = 200
            picks = self._shuffle_if_random(tracks[:cap])   # ★ 同上：歌单/收藏也吃随机
            with self._qlock:
                self.upcoming = [self._make_entry(t, reason) for t in picks[1:]]
                self._manual_queue = True
            self.set_queue_src("playlist")  # ★ 来源=歌单/收藏 ⇒ 同样允许三种模式
            self._emit_playlist()
            self.start_radio(seed_track=picks[0], auto_start=True)

        def _pop_to_current(self) -> None:
            with self._qlock:
                if self.upcoming:
                    entry = self.upcoming.pop(0)
                    self.current = entry["track"]
                    self.current_id = entry["track"].id
                    self.current_broadcast = entry["broadcast"]
                    self.played.add(self.current_id)
                    self.history.insert(0, self.current)

        # ---------- 播放当前曲 ----------

        def _make_entry(self, track, reason: str) -> dict:
            # 口播/转场「分组逻辑」保持不变：每首歌仍是一个独立 entry，broadcast 仍挂在
            # entry 上、仍由 _gen_broadcast/_do_gen_broadcast 生成、仍由 _prep_next_broadcast
            # 统一预取。这里刻意「不立即生成口播」——歌曲进入列表时不再抢跑算口播，而是等
            # 第一首开始播放后由 _after_music_started → _prep_next_broadcast 为待播列表里的
            # 歌生成口播与转场（见用户要求的「第一首播放后再生成后续口播/转场」）。
            # 第一首歌的口播走独立的「默认开场白」路径（见 start_radio /
            # _start_playback_if_needed 中 opening=True 的 _gen_broadcast）。
            entry = {"track": track, "broadcast": None, "reason": reason,
                     "_gen": False, "transition":
                         {"transition_type": "intro", "reason": "", "script": ""}}
            return entry

        # ---------- 第一首默认开场白 ----------

        def _late_intro_text(self, track) -> str:
            """★ R119-4s：**非首曲「迟到口播」的兜底句**（绝不复用开场白）。

            为什么必须分开：`_opening_text` 是**为第一首歌**设计的"向听众问好"，
            **刻意**带 地点 + 天气 + 温度（见其 docstring）。但"迟到口播"发生在
            **听众已在收听中**的第 N 首 —— 此时报城市/天气/温度既突兀，
            又直接违反本项目的口播契约（地点天气要意象化、禁止温度数字）。
            实测症状：播到第 N 首时念出"长春, 雷阵雨, 气温 16℃. 让我们用《…》开启新的一天."
            ⇒ 用户报的"老是提天气、地点"就是这条复用造成的。

            这里只给一句**与歌曲有关、不提环境、不提"新的一天"**的短句（短而真）。
            """
            title = getattr(track, "title", "") or ""
            artist = getattr(track, "artist", "") or ""
            # ★ 2026-11-17：优先用**离线预生成的通用过渡文案**（与音频同一句）
            try:
                text, _wav = self._generic_static()
                if text:
                    return text
            except Exception:  # noqa
                pass
            if title and artist:
                return f"这首《{title}》，{artist}的，交给耳朵。"
            if title:
                return f"这首《{title}》，交给耳朵。"
            return "这首，交给耳朵。"

        # ---------- ★ 2026-11-17：口播按曲目身份缓存（队列重建后仍能认回） ----------

        @staticmethod
        def _bc_key(track) -> str:
            return f"{getattr(track, 'id', '')}|{getattr(track, 'title', '')}"

        def _track_stale(self, track) -> bool:
            """★ 2026-11-18：这首是否已播过/不在播放视野 ⇒ TTS 合成该跳过。

            仍"有价值"的情形：正在播（current）或还在待播队列（upcoming）。
            其余（已进 played、被移除）⇒ 陈旧，不再浪费 worker 合成没人听的口播。
            """
            try:
                tid = getattr(track, "id", None)
                cur = getattr(self, "current", None)
                if cur is not None and getattr(cur, "id", None) == tid:
                    return False
                with self._qlock:
                    if any(getattr(e.get("track"), "id", None) == tid
                           for e in self.upcoming):
                        return False
                return tid in getattr(self, "played", set())
            except Exception:  # noqa 判不出时保守当作"不陈旧"（宁可多做别漏）
                return False

        @staticmethod
        def _bc_dir():
            """口播落盘目录（合成→落盘→播完即删，不长期占空间）。"""
            from pathlib import Path as _P
            from config.settings import PROJECT_ROOT
            d = PROJECT_ROOT / "data_runtime/tts_broadcast"
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa
                pass
            return d

        def _bc_persist(self, track, bc) -> None:
            """把生成好的口播（文案 + wav 路径）落盘，供重启/队列重建后复用。"""
            try:
                if not bc or not bc.get("text"):
                    return
                import hashlib as _h
                import json as _json
                key = self._bc_key(track)
                name = _h.md5(key.encode("utf-8")).hexdigest()[:16]
                self._bc_dir().joinpath(f"{name}.json").write_text(
                    _json.dumps({"key": key, "text": bc.get("text"),
                                 "wav": bc.get("wav"), "raw": bc.get("raw"),
                                 "title": getattr(track, "title", ""),
                                 "at": time.time()}, ensure_ascii=False),
                    encoding="utf-8")
            except Exception as e:  # noqa
                logger.info("[口播] 落盘失败（不影响播放）：%s", e)

        def _recall_broadcast(self, track):
            """读回这首歌已落盘的口播（进程重启/队列重建后仍可用）。"""
            if track is None:
                return None
            try:
                import hashlib as _h
                import json as _json
                from pathlib import Path as _P
                key = self._bc_key(track)
                name = _h.md5(key.encode("utf-8")).hexdigest()[:16]
                p = self._bc_dir().joinpath(f"{name}.json")
                # 同 id 不同标题（改名/加后缀）时按标题兜底扫一遍
                if not p.is_file():
                    title = (getattr(track, "title", "") or "").strip()
                    for q in self._bc_dir().glob("*.json"):
                        try:
                            d = _json.loads(q.read_text(encoding="utf-8"))
                        except Exception:  # noqa
                            continue
                        if title and (d.get("title") or "").strip() == title:
                            p = q
                            break
                if not p.is_file():
                    return None
                d = _json.loads(p.read_text(encoding="utf-8"))
                wav = d.get("wav") or ""
                if not d.get("text"):
                    return None
                if wav and not _P(wav).is_file():
                    # 音频已随播放删除 ⇒ 文案可复用，音频走兜底/重合成
                    return {"text": d["text"], "wav": None, "raw": d.get("raw")}
                logger.info("[口播] 磁盘找回已生成口播《%s》", d.get("title") or key)
                return {"text": d["text"], "wav": wav or None, "raw": d.get("raw")}
            except Exception as e:  # noqa
                logger.info("[口播] 磁盘认回失败：%s", e)
                return None

        def _bc_drop(self, track) -> None:
            """播完即丢：删除落盘的文案记录（wav 由 release 流程删除）。"""
            try:
                import hashlib as _h
                key = self._bc_key(track)
                name = _h.md5(key.encode("utf-8")).hexdigest()[:16]
                p = self._bc_dir().joinpath(f"{name}.json")
                if p.is_file():
                    p.unlink()
            except Exception:  # noqa
                pass

        def _kick_taste_background(self) -> None:
            """★ 2026-11-18（用户「货架慢慢补，后台让它慢慢跑」）：
            首曲开场后台起品味分析，**不阻塞导听流水线、不卡时间点**。
            LLM 单槽位自然排队：导听先发起就先跑，品味随后慢慢跑；
            品味只影响后续推荐货架（慢慢补即可），绝不为它推迟导听。
            防重复：已在分析/已有结果/已排程则跳过。
            """
            if getattr(self, "_taste_scheduled", False):
                return
            self._taste_scheduled = True
            def _go():
                try:
                    if (getattr(self, "taste_profile", None) is None
                            and not getattr(self, "_taste_computing", False)
                            and hasattr(self, "refresh_taste")):
                        self.refresh_taste()
                        logger.info("[品味] 后台慢慢分析（不阻塞导听/推荐）")
                except Exception as e:  # noqa
                    logger.warning("后台品味分析启动失败：%s", e)
            # 短暂让路 8s：让导听流水线的 LLM 先发起、占住 -np1 槽位；
            # 品味随后慢慢跑（货架慢慢补，用户明确不急）。
            threading.Timer(8.0, _go).start()

        def _sync_opening_broadcast(self, track):
            """★★ 2026-11-18：同步生成首曲开场白 broadcast（根治"首曲没播默认口播"）。

            首曲开场白是**现成 wav、零 LLM**，根本不该走 _gen_broadcast 的后台线程
            + 竞态补播（实测 current is track 竞态失败 ⇒ 开场白丢弃、watchdog 直接起音乐）。
            这里同步准备好 {"text","wav","track_id"}，调用方直接赋给 current_broadcast
            再 _play_track，保证 _speak_intro 一定拿得到、立即朗读。
            """
            try:
                # ★ 2026-11-19：英文模式没有英文静态开场白。返回 None 让调用方
                #   回退到 _gen_broadcast(opening=True)，再由 _do_gen_broadcast
                #   把 opening 翻成 False，走英文 LLM 口播；绝不播中文固定开场白。
                if getattr(self, "broadcast_language", "zh") != "zh":
                    logger.info("[口播] 英文模式：跳过中文静态开场白，走英文 AI 口播")
                    return None
                text, wav = self._opening_static()
                if not wav:
                    return None
                from pathlib import Path as _Path
                if not _Path(wav).exists():
                    return None
                # ★ 开场白是**通用音频**（不涉及具体歌曲），绝不能打 track_id：
                #   首曲 track 常是负数临时 id，入库后变正数 ⇒ _speak_intro 归属校验
                #   「广播 id != 当前 id」会把它当错配丢弃 ⇒ 首播无声/延迟。
                bc = {"text": text or "", "wav": wav}
                self._opening_done = True
                logger.info("[口播] 首曲同步锁定时段开场白音频：%s", wav)
                return bc
            except Exception as e:  # noqa
                logger.warning("同步开场白准备失败：%s", e)
                return None

        def _opening_static(self):
            """首曲开场白（**离线预生成音频，随包发布**）。返回 (text, wav 路径|None)。

            设计约束（用户 2026-11-17 明确）：
              1. 音频必须随包存在 ⇒ 即使 IndexTTS 完全没加载/坏了，首曲开场白照样能播；
                 播放只是 `player.play_tts(wav)`，不经过任何 TTS 引擎。
              2. 文案不涉及具体歌曲/天气/城市（固定音频没法知道），只做「身份 + 氛围」。
              3. 按时段选一档：opening_<slot>.wav 优先，退化到 opening.wav，
                 再没有才回退「模板文案 + 现合成」。

            产物与文案由 `tools/make_opening_audio.py` 生成（时段：清晨/正午/午后/傍晚/深夜）。
            """
            try:
                import json as _json
                from pathlib import Path as _Path
                from config.settings import PROJECT_ROOT, user_config
                base = (user_config.tts.get("opening_audio") or "").strip()
                root = (_Path(base).parent if base
                        else PROJECT_ROOT / "data_runtime/tts_static")
                # ★ 2026-11-18：静态兜底按 Qwen3-TTS 音色分目录（用户要求"针对特定音色"）。
                #   先找当前音色那套，缺档再回退根目录旧音频（平滑过渡）。
                _voice = (user_config.tts.get("qwen3tts_voice") or "").strip()
                if not base and _voice and (root / _voice).is_dir():
                    root = root / _voice
                # 时段 key：morning / noon / afternoon / evening / night
                slot = "night"
                try:
                    from utils.location import time_slot
                    slot = time_slot(None)[0]
                except Exception:  # noqa
                    pass
                # 查找顺序：本时段 → 任意时段（缺档也不退老模板）→ 旧单文件
                cands = [root / f"opening_{slot}.wav"]
                try:
                    cands += sorted(root.glob("opening_*.wav"),
                                    key=lambda p: p.stat().st_mtime, reverse=True)
                except Exception:  # noqa
                    pass
                cands.append(root / "opening.wav")
                for wav in cands:
                    if not (wav.is_file() and wav.stat().st_size > 1000):
                        continue
                    text = ""
                    meta = wav.with_suffix(".json")
                    if meta.is_file():
                        try:
                            text = (_json.loads(meta.read_text(encoding="utf-8"))
                                    .get("text") or "").strip()
                        except Exception:  # noqa
                            text = ""
                    if not text:
                        text = (user_config.tts.get("opening_text") or "").strip()
                    if wav.name != f"opening_{slot}.wav":
                        logger.info("[口播] 本时段开场白缺失，改用 %s", wav.name)
                    return (text or "欢迎收听。接下来的这段时间，交给音乐。"), str(wav)
                return "", None
            except Exception as e:  # noqa
                logger.warning("[口播] 读取固定开场白失败: %s", e)
                return "", None

        def _generic_static(self):
            """通用过渡口播（**离线预生成，随包发布**）。返回 (text, wav|None)。

            用途：任何一首歌的口播没赶上（切歌太快 / 模型没加载完）时顶上。
            同样不涉及具体歌曲，只说一句「先交给音乐」——不假装知道是哪首。
            播放它不经过任何 TTS 引擎，所以引擎全挂也能出声。
            """
            try:
                import json as _json
                from pathlib import Path as _Path
                from config.settings import PROJECT_ROOT, user_config
                base = (user_config.tts.get("opening_audio") or "").strip()
                root = (_Path(base).parent if base
                        else PROJECT_ROOT / "data_runtime/tts_static")
                # ★ 2026-11-18：按当前 Qwen3-TTS 音色选对应目录（与开场白一致）
                _voice = (user_config.tts.get("qwen3tts_voice") or "").strip()
                if not base and _voice and (root / _voice).is_dir():
                    root = root / _voice
                wav = root / "opening_generic.wav"
                if not (wav.is_file() and wav.stat().st_size > 1000):
                    return "", None
                text = ""
                meta = wav.with_suffix(".json")
                if meta.is_file():
                    try:
                        text = (_json.loads(meta.read_text(encoding="utf-8"))
                                .get("text") or "").strip()
                    except Exception:  # noqa
                        text = ""
                return (text or "这一首先交给音乐，剩下的，我稍后补上。"), str(wav)
            except Exception as e:  # noqa
                logger.warning("[口播] 读取通用兜底口播失败: %s", e)
                return "", None

        def _opening_text(self, track) -> str:
            """第一首歌的「默认开场白」——**直接用离线预生成的文案**。

            ★ 2026-11-17 用户明确：开场白已经有现成的**文案 + 音频**
            （`data_runtime/tts_static/opening_<时段>.wav` 与同名 json），
            旧写法「地点 + 天气 + 温度 + 让我们用《歌名》开启新的一天」**不要了**：
            它踩了本项目口播契约的三条红线（城市名、气温数字、环境挤掉歌曲），
            而且和预制音频的文案对不上（声文不一致）。
            """
            try:
                text, _wav = self._opening_static()
                if text:
                    return text
            except Exception:  # noqa
                pass
            return "欢迎收听。接下来的这段时间，交给音乐。"

        # ---------- 防御：track 参数归一化 ----------

        @staticmethod
        def _is_track(obj) -> bool:
            """判断一个对象是否可当作 Track 使用（有 id 与 title 属性）。"""
            return obj is not None and hasattr(obj, "id") and hasattr(obj, "title")

        def _resolve_track(self, arg):
            """把「Track 或 track_id(int)」统一解析成 Track 对象。

            UI 的队列/曲库行只持有 track_id，右键「下一首播放」「加入推荐列表」
            会传 int 进来；而这些方法需要 Track。这里在边界处把 int 还原成 Track，
            避免把裸 id 当成 track 存进 upcoming（会导致后续读取 e["track"].id 崩溃）。
            """
            if self._is_track(arg):
                return arg
            if isinstance(arg, int):
                tid = arg
                # 1) 当前曲 / 历史 / 待播队列里直接找 Track 对象
                if self._is_track(self.current) and self.current.id == tid:
                    return self.current
                for t in getattr(self, "history", []) or []:
                    if self._is_track(t) and t.id == tid:
                        return t
                with self._qlock:
                    for e in self.upcoming:
                        tr = e.get("track")
                        if self._is_track(tr) and tr.id == tid:
                            return tr
                # 2) 曲库内存（与 UI 同一来源，最权威且无 DB 锁竞争）
                #    后台扫描/收藏等 DB 写入可能造成「database is locked」瞬时失败，
                #    因此优先用纯内存的 library.by_id 解析，绕开锁竞争。
                try:
                    t = self.library.by_id(tid)
                    if self._is_track(t):
                        return t
                except Exception:  # noqa
                    pass
                # 3) 曲库数据库（sqlite3.Row → 经 Track.from_row 转成 Track；
                #    ⚠ 旧写法直接 _is_track(row) 判定 —— Row 没有 .id/.title 属性，
                #    恒为 False，导致「临时负 id / 不在内存缓存」的曲目永远解析失败，
                #    红心收藏无法入库（R49-24）。）
                try:
                    row = self.db.get_track(tid)
                    if row is not None:
                        from data.library import Track
                        t = Track.from_row(row)
                        if self._is_track(t):
                            return t
                except Exception as e:  # noqa 不再静默吞掉，便于诊断锁竞争等瞬态错误
                    logger.warning("get_track(%s) 异常（已转曲库兜底）：%s",
                                   tid, str(e)[:120])
                # 4) 本地曲库兜底
                for t in self._local_pool():
                    if getattr(t, "id", None) == tid:
                        return t
                logger.warning("无法把 track_id=%s 解析为 Track，已跳过该操作。", tid)
                return None
            logger.warning("收到的 track 参数既不是 Track 也不是 int: %r", arg)
            return None

        def _clean_upcoming(self) -> None:
            """丢弃任何 track 字段不是 Track 对象的脏 entry（防御 UI/外部传入 id
            导致 upcoming 中毒）。在读取 upcoming 的热路径入口调用，保证单条脏数据
            不会拖垮整个 UI / 推荐流程。"""
            with self._qlock:
                bad = [e for e in self.upcoming if not self._is_track(e.get("track"))]
                if bad:
                    self.upcoming = [e for e in self.upcoming
                                     if self._is_track(e.get("track"))]
                    logger.warning("清理了 %d 个脏推荐条目（track 非 Track 对象）。",
                                   len(bad))

        def _prewarm_search(self, n: int = 3) -> int:
            """★ 2026-09-21：后台预热接下来 n 首的检索资料（只写缓存，失败静默）。

            为什么在 `schedule` 这层做：这里正好知道"当前播到哪、后面排了什么"，
            且此时**当前曲还在播放** ⇒ 这段墙钟是免费的（口播早已生成好）。
            """
            try:
                from ai import search as _s
                ups = list(getattr(self, "upcoming", []) or [])[:max(0, int(n))]
                pairs = []
                for e in ups:
                    t = e.get("track") if isinstance(e, dict) else None
                    if t is None:
                        continue
                    manager = getattr(self, 'source_manager', None)
                    if manager and hasattr(manager, 'enrich_metadata'):
                        manager.enrich_metadata(t, self.db)
                    pairs.append((getattr(t, "title", "") or "",
                                  getattr(t, "artist", "") or ""))
                if not pairs:
                    return 0
                k = _s.prewarm_upcoming(pairs, n=n)
                if k:
                    logger.info("[检索预热] 已为接下来 %d 首准备好资料（缓存）", k)
                return k
            except Exception as e:      # noqa 预热是加分项，绝不冒泡
                logger.debug("[检索预热] 跳过: %s", e)
                return 0

        def _gen_broadcast(self, entry: dict, position=None, total=None, next_title=None,
                           opening: bool = False, force: bool = False) -> None:
            """后台为某 entry 生成并存储口播（文案+TTS）。

            opening=True 时生成「第一首默认开场白」（地点+天气+温度+歌名），不走大模型、
            离线/无 AI 也必有开场导听；否则走常规 AI DJ 口播。
            """
            if entry.get("_gen") and not force:
                return
            if force:
                entry["_gen"] = False   # 切歌兜底：绕过可能卡死的幂等标志，强制重生成
            entry["_gen"] = True
            threading.Thread(
                target=timed(self._do_gen_broadcast, "llm_broadcast"),
                args=(entry, position, total, next_title, opening), daemon=True).start()

        def _do_gen_broadcast(self, entry: dict, position, total, next_title,
                              opening: bool = False, skip_tts: bool = False) -> None:
            track = entry.get("track")
            if not self._is_track(track):
                # 防御：条目 track 非 Track 对象（历史中毒数据），跳过生成避免崩溃。
                logger.warning("跳过口播生成：条目 track 非 Track 对象。")
                entry["_gen"] = True
                return
            opening_fallback = None
            # ★ 2026-11-19：英文模式跳过中文固定开场白的分支；直接走常规 LLM
            #   （brain 会按 broadcast_language=en 选英文 SYSTEM + 12 秒口径）。
            if opening and getattr(self, "broadcast_language", "zh") != "zh":
                opening = False
                logger.info("[口播] 英文模式：首曲走英文 AI 口播")
            if opening:
                # ★ 2026-11-17（用户拍板）：首曲改用**离线预生成的固定开场白音频**。
                #   理由：首曲 AI 口播在冷启动下必然赶不上 5 秒导听窗口（LLM 起热 +
                #   IndexTTS 载模型），退化到模板又浪费 IndexTTS 音色。
                #   固定开场白不涉及具体歌曲 ⇒ 预生成一次即可，播放时**零延迟直出**，
                #   既不走 LLM 也不现合成。生成方式：tools/make_opening_audio.py
                text, wav = self._opening_static()
                if not wav:
                    # 没有预生成音频时才回退到「模板文案」，但**不再直连合成**
                    # （task-1：直连会与大模型同驻，单条可卡到 90s；播放侧有成对静态兜底）
                    text = self._opening_text(track)
                    wav = None
                    logger.info("[口播] 未找到固定开场白音频，回退模板文案（音频走静态兜底）")
                else:
                    logger.info("[口播] 首曲播放固定开场白音频：%s", wav)
                # ★ 2026-11-18：后台慢慢起品味分析（不阻塞导听），见 _kick_taste_background。
                self._kick_taste_background()
                entry["broadcast"] = (
                    {"text": text, "wav": wav} if (text or wav) else None)
                entry["_gen"] = True
                entry["_gen_at"] = time.monotonic()
                entry["_gen_seq"] = self._broadcast_seq
                self._emit_playlist()
                # ★★ 2026-11-18（用户「首曲没播默认口播」）：
                #   首曲 opening 分支在后台线程跑，此处 `self.current is track`
                #   常因竞态失败（current 正在 pop 设置中）⇒ 不设 current_broadcast、
                #   不补播 ⇒ watchdog 5s 后直接起音乐、开场白丢失。
                #   改为 **标题/id 双重匹配**（不靠对象身份）。**不给 broadcast 打
                #   track_id**：开场白是通用音频，首曲负数临时 id 入库后变正数，
                #   打上去会被 _speak_intro 归属校验当错配丢弃（首播无声延迟）。
                _cur_track = getattr(self, "current", None)
                _same = (track is not None and _cur_track is not None
                         and (getattr(_cur_track, "id", None) == getattr(track, "id", None)
                              or getattr(_cur_track, "title", None) == getattr(track, "title", None)))
                if _same and self.current_broadcast is None and entry["broadcast"]:
                    self.current_broadcast = entry["broadcast"]
                    if getattr(self, "_intro_pending", False):
                        self._emit("thinking", True)
                        threading.Thread(
                            target=timed(self._speak_intro, "tts_intro"),
                            args=(entry["broadcast"], self._play_seq),
                            daemon=True).start()
                elif _same and getattr(self, "_intro_pending", False):
                    self._start_intro_fallback()
                return
            # ---- 常规 AI DJ 口播路径 ----
            # 第一首（position==1）开场白不依赖品味画像，跳过分析以加速首曲口播与音频；
            # 品味画像改由后续歌曲口播/推荐在后台按需触发（一次性，不影响后续个性化）。
            if position != 1:
                self._ensure_taste()   # 若尚未分析，现分析品味画像（后台，不卡主线程）
            # 天气机械降频：本会话每生成一次口播序号 +1；仅当距上次提供天气已隔
            # WEATHER_MIN_GAP 首以上、且确有天气数据时才"给大模型提供天气"。
            # 注意：天气数据始终留在 ctx（默认/离线口播照常带天气）；降频只决定
            # "本轮是否把天气喂给大模型"（suppress_weather），从源头减少大模型提天气。
            with self._seq_lock:
                self._broadcast_seq += 1
                gap = self._broadcast_seq - self._last_weather_seq
                w = self.weather or {}
                offer_weather = (gap >= WEATHER_MIN_GAP) and bool(w.get("text"))
                if offer_weather:
                    self._last_weather_seq = self._broadcast_seq
            ctx = self._build_ctx(
                track, position=position, total=total, next_title=next_title,
                suppress_weather=not offer_weather)
            ctx["recommendation_reason"] = entry.get("reason", "")
            # 开场/第一首（position==1）不做联网检索：开场白是问候语，无需实时资料；
            # 去掉这次多轮 $web_search（Kimi 内置工具常 >30s）可让首曲口播与音频显著提前。
            # 后续歌曲仍按用户"口播实时"的设定逐首检索。
            do_search = self._should_web_search() and position != 1
            t_llm0 = time.monotonic()
            try:
                # ★ 2026-09-21：第一首**不新发检索**（保持"开场白要快"），但允许**吃缓存** ——
                #   预热线程/上一轮会话取回的资料在这里以毫秒级命中，不再"第一首永远零事实"。
                # ★ 2026-11-19：语言**快照冻结**——本任务从生成到TTS都用同一个 lang，
                #   中途用户切语言不影响在途任务（根治中英错配）。
                _lang_snapshot = str(getattr(self, "broadcast_language", "zh") or "zh")
                entry["_broadcast_lang"] = _lang_snapshot
                meta = self.brain.generate_dj_script(
                    ctx, web_search=do_search,
                    cache_only=(position == 1 and self._should_web_search()),
                    language=_lang_snapshot)
            except Exception as e:  # noqa
                logger.warning("口播生成失败: %s", e)
                # ★ 2026-11-17：首曲保底 —— AI 不可用时仍保证有开场导听（模板文案）。
                #   这条路径**不弹「AI 调用错误」**：冷启动时 LLM 还没热起来是常态，
                #   弹错误会让人以为整套坏了，而实际上开场白照常播。
                if opening_fallback:
                    try:
                        w = (self._synth_text(opening_fallback, timeout=6.0)
                             if (self.dj_on and self.player.audio_ok
                                 and hasattr(self, "_synth_text"))
                             else None)
                        entry["broadcast"] = {"text": opening_fallback, "wav": w}
                        entry["_gen"] = True
                        entry["_gen_at"] = time.monotonic()
                        self._emit_playlist()
                        logger.info("[口播] 首曲 AI 未就绪，已用开场白兜底（不提示错误）")
                        if (self.current is track and self.current_broadcast is None
                                and entry["broadcast"]):
                            self.current_broadcast = entry["broadcast"]
                            if getattr(self, "_intro_pending", False):
                                self._emit("thinking", True)
                                threading.Thread(
                                    target=timed(self._speak_intro, "tts_intro"),
                                    args=(entry["broadcast"], self._play_seq),
                                    daemon=True).start()
                                return
                    except Exception as e2:  # noqa
                        logger.warning("开场白保底合成失败: %s", e2)
                # 非首曲（或首曲保底也失败）：这才是真的需要提示的情况
                if not opening:
                    self._note_llm_failure("broadcast", f"口播生成：{e}")
                # 开场导听仍在等待：生成失败则直接放歌，避免永久静音
                if self.current is track and getattr(self, "_intro_pending", False):
                    self._start_intro_fallback()
                return
            t_llm1 = time.monotonic()
            # ★★ 2026-11-17（用户明确）：AI 没产出时**不要**用 brain 的"离线文案"
            #   （那套是「城市+天气+气温XX℃+为你送上」的模板，和口播风格直接冲突）。
            #   项目里已经有**预制文案 + 预制音频**：
            #     · 首曲 → `tts_static/opening_<时段>.wav`（+同名 json 里的文案）
            #     · 其余 → `tts_static/opening_generic.wav`（"这一首先交给音乐…"）
            #   两者都不经过 TTS 引擎，声文一致、零延迟；这里直接把 meta 换成它们。
            _static_wav = None
            if not meta.get("used_ai"):
                try:
                    _st_text, _st_wav = (self._opening_static() if opening
                                         else self._generic_static())
                    if _st_text:
                        meta = {"text": _st_text, "display_text": _st_text,
                                "used_ai": False, "error": meta.get("error", "")}
                        _static_wav = _st_wav
                        logger.info("[口播] AI 未产出 → 改用预制兜底（%s）：%s",
                                    "首曲开场白" if opening else "通用过渡",
                                    _st_text[:28])
                except Exception:  # noqa
                    pass
            # 带标签原文用于 TTS；去标签版用于 UI 显示
            raw = meta.get("text", "")
            display = meta.get("display_text") or strip_tags(raw)
            # ★ 2026-11-18：正文字数提到 try 外，供下方「TTS 入队闸门」用
            _core_n = 0
            # ★ R119-4q：**把导听口播的文本打出来**。此前这里只打了耗时、没打文本，
            #   导致日志里"看不到 intro 文字"，容易被误读成"intro 没生成"。
            #   同时也把**带标签原文**打出来（便于核对 <ja> 之类是否真的在）。
            try:
                # ★ 2026-11-17：新增 `core` = 按产品口径的**正文字数**（去语言标签、
                #   去歌名/歌手名、去标点、英文按词）。规格 50~70；超界打 WARNING，
                #   便于用日志直接验收，而不用人肉数字数。
                from core.text_metrics import core_len
                _core_n = core_len(display or "", getattr(track, "title", "") or "",
                                   getattr(track, "artist", "") or "")
                logger.info("[口播·导听] 《%s》 display=%d字 raw=%d字 core=%d字(不含名字) 内容：%s",
                            getattr(track, "title", "?"), len(display or ""),
                            len(raw or ""), _core_n,
                            (display or "").replace("\n", " ")[:160])
                if meta.get("used_ai") and _core_n and not (50 <= _core_n <= 90):
                    logger.warning("[口播·导听] 字数越界：core=%d字（目标 50~70，上限 90）《%s》",
                                   _core_n, getattr(track, "title", "?"))
            except Exception:  # noqa 日志绝不影响主流程
                pass
            # ★★ 2026-11-17（关键顺序修正）：**文案一产出就登记，早于 TTS 合成**。
            #   实测：`[口播·导听]` 打出来了，但登记一条都没有 ⇒ 代码在中间的 TTS
            #   合成段卡住/异常，永远走不到登记。于是「文案明明生成了却用不上」，
            #   而且音频失败还会连带丢文案。登记提前后，两者解耦。
            try:
                _m = getattr(self, "_bc_map", None)
                if _m is None:
                    _m = self._bc_map = {}
                _tk = (getattr(track, "title", "") or "").strip()
                if display:
                    _m[self._bc_key(track)] = {"text": display, "wav": None, "raw": raw}
                    if _tk:
                        _m[f"|{_tk}"] = {"text": display, "wav": None, "raw": raw}
                logger.info("[口播] 文案登记 key=%s title=%s 表内=%d 条",
                            self._bc_key(track), _tk, len(_m))
            except Exception as e:  # noqa
                logger.info("[口播] 文案登记异常：%s", e)
            t_tts0 = time.monotonic()
            # 用带标签原文(raw)做 TTS 路由：日文 <ja> 片段走 VOICEVOX，中文叙述走当前
            # 生效引擎(melotts/edge)；display(去标签)仅用于 UI 显示。此前用 display 合成
            # 导致日语被当前引擎硬读成乱码、且回退链不含 voicevox，日语从不被 VOICEVOX 朗读。
            # synthesize_unique_tagged 对无标签的纯中文文案会退化为普通 synthesize_unique，
            # 行为与旧代码一致，无回归。raw 为空时退用 display，避免偶发丢音频。
            # dj_on 仅门控 TTS 音频输出：关闭时仍生成文案（AI 思考），不合成音频。
            tts_src = raw or display
            # 预制音频已就位 ⇒ 不再合成（省一次 TTS，也避开与大模型抢显存）
            want_audio = bool(tts_src and self.dj_on and self.player.audio_ok
                              and not _static_wav)
            # ★★★ 2026-11-18（用户拍板）：**只有字数检验过关的文案才传 TTS**。
            #   返工中的中间产物/返工后仍越界的（如 713 字超长、精简后仍不达标）
            #   一律不传 TTS —— 否则 TTS 队列被长文案堵死、单条 40s+ ⇒ 90s 超时
            #   ⇒ 二三首全默认文案。只让 50~120 字的过关文案入队，时间永远来得及。
            #   文案本身仍登记（UI 有字），仅音频降级为无/静态兜底。
            if want_audio and meta.get("used_ai") and _core_n > 120:
                logger.info("[口播·TTS闸门] core=%d字 超上限120（返工未过关）→ 不传 TTS，"
                            "文案保留、音频走兜底：《%s》", _core_n,
                            getattr(track, "title", "?"))
                want_audio = False
            # ★★ 2026-11-18（用户拍板「完全放弃第一首导听」）：队首导听只留文案，
            #   不提交 TTS。让 TTS 首次加载(100s)在第一首播放期完成，流水线从
            #   队首转场开始跑。第一首播放用静态兜底开场白（零延迟），听感不变。
            if skip_tts:
                want_audio = False
                logger.info("[口播·流水线] 《%s》队首导听：文案已生成，按约定跳过 TTS"
                            "（流水线从本首转场开始）", getattr(track, "title", "?"))
            logger.info("[口播] 合成闸门 tts_src=%s dj_on=%s audio_ok=%s → want_audio=%s",
                        bool(tts_src), bool(self.dj_on),
                        bool(getattr(self.player, "audio_ok", False)), want_audio)
            wav = _static_wav
            # ★ 2026-11-17 分时批处理（MUSICAI_TTS_BATCH=1 才启用）：
            #   **只对「不是当前正在播的这首」走批处理**——当前曲的导听必须立刻可用，
            #   而批处理窗口内大模型会被停掉约 20~40s，不能卡在用户眼前。
            #   其余（预取给后面几首的）先入队，由后台统一「停 LLM → 批量合成 → 起 LLM」。
            _batcher = self._tts_batcher() if want_audio else None
            if want_audio and _batcher is not None:
                key = f"bc-{self._broadcast_seq}-{id(entry)}"
                try:
                    self._tts_pending[key] = entry
                    entry["_tts_key"] = key
                    _stale = (lambda tk=track: self._track_stale(tk))
                    # ★★ 2026-11-18 流水线：常驻模式下**同步等合成**（LLM 与 TTS
                    #   严格错峰，永不同时占 GPU）。牺牲第一首即时性，换后面全稳。
                    #   链是串行的：本步等 TTS 完成才生成下一条 ⇒ 不会再有
                    #   「LLM 连发 4 条把 GPU 占满、TTS 拖到 208s」。
                    if TTS_PIPELINE:
                        # ★ 前置闸门：流水线第一条提交前，先同步确保 TTS worker
                        #   loaded=true（首次加载 100s）。否则请求撞加载窗口 ⇒
                        #   90s submit_wait 超时 ⇒ 整条静音（实测首条即如此）。
                        #   预热在后台跑，这里补一道同步等待，只做一次。
                        if not getattr(self, "_tts_ready_checked", False):
                            try:
                                # ★ 2026-11-18：只有 IndexTTS 才有"worker 首次加载 100s"
                                #   的前置等待。Qwen3-TTS 是按需 CLI 子进程、退出即释放，
                                #   没有 loaded 状态——无条件调 ensure_indextts() 会在
                                #   qwen3tts 模式下白白拉起一个 IndexTTS worker（黑窗口）。
                                _engine = getattr(self.tts, "_engine_name", "")
                                if _engine == "indextts" and hasattr(self.tts, "ensure_indextts"):
                                    self.tts.ensure_indextts()
                                self._tts_ready_checked = True
                                logger.info("[口播·流水线] TTS 就绪检查完成（引擎=%s）", _engine)
                            except Exception as e:  # noqa
                                logger.warning("[口播·流水线] TTS ready 等待异常（继续）: %s", e)
                                self._tts_ready_checked = True
                        # ★ 2026-11-19：登记语言快照（合成用任务的lang，不读实时全局）
                        _snap = entry.get("_broadcast_lang")
                        _batcher.set_item_lang(key, ("en" if str(_snap or "").startswith("en") else ("zh" if _snap else None)))
                        wav = _batcher.submit_wait(
                            key, tts_src, timeout=TTS_PIPELINE_WAIT_S,
                            stale_check=_stale, front=False)
                        logger.info("[口播·流水线] 《%s》文案→TTS 同步完成（%s）",
                                    getattr(track, "title", "?"),
                                    "有声" if wav else "静音/超时")
                    elif self.current is track:
                        # 旧分时架构：当前曲最多等 5s；等不到让音乐先走，回调补
                        _snap = entry.get("_broadcast_lang")
                        _batcher.set_item_lang(key, ("en" if str(_snap or "").startswith("en") else ("zh" if _snap else None)))
                        wav = _batcher.submit_wait(key, tts_src, timeout=5.0,
                                                   stale_check=_stale)
                    else:
                        _snap = entry.get("_broadcast_lang")
                        _batcher.set_item_lang(key, ("en" if str(_snap or "").startswith("en") else ("zh" if _snap else None)))
                        _batcher.submit(key, tts_src, stale_check=_stale)
                except Exception as e:  # noqa 批处理失败绝不影响文案
                    logger.warning("口播入批失败（不再直连，交给静态兜底）: %s", e)
                    self._tts_pending.pop(key, None)
                    wav = None
            elif want_audio:
                # ★ 2026-11-17：**不再在链线程里同步合成** —— 交给音频线程，
                #   链子立刻往下走，避免被 worker 冷加载（20~60s）堵死整条文案链。
                self._enqueue_audio(entry, track, tts_src)
            t_tts1 = time.monotonic()
            entry["broadcast"] = (
                {"text": display, "wav": wav, "raw": raw,
                 # ★ 2026-11-17：给广播记上**归属**（曲目 id + 标题）。
                 #   播放前用它核对"这段口播到底是不是当前这首的"，杜绝
                 #   切歌竞态下把上一首的口播念在下一首上（用户实测听到过错配）。
                 "track_id": getattr(track, "id", None),
                 "title": getattr(track, "title", "")}
                if (display or wav) else None)
            # ★★ 2026-11-17：音频合成完把 wav 回填进映射（文案是合成前登记的，
            #   当时 wav=None；不回填则播放时只命中文字、没有声音）。
            try:
                if wav:
                    _mm = getattr(self, "_bc_map", None)
                    if _mm:
                        _k1 = self._bc_key(track)
                        _k2 = f"|{(getattr(track, 'title', '') or '').strip()}"
                        for _k in (_k1, _k2):
                            if _k in _mm:
                                _mm[_k]["wav"] = wav
                        logger.info("[口播] 音频回填映射 key=%s", _k1)
            except Exception as e:  # noqa
                logger.info("[口播] 音频回填失败：%s", e)
            # ★★ 2026-11-17（实测：17:10 已生成的《Lemon》口播在 17:11 队列重建后丢失）：
            #   待播队列会被重建（roam/推荐补货），重建后是**新的 dict 对象**，
            #   原来 entry 上挂的 broadcast 就跟着丢了 ⇒ 播到那首时只能走兜底。
            #   这里按「曲目身份」在实例上留一份，重建后能认回来（见 _recall_broadcast）。
            # ★ 2026-11-17（接线修复）：生成结果同时登记到**会话内映射**。
            #   队列重建后 entry 会换成新对象，旧对象上的 broadcast 随之丢失，
            #   导致「文案已生成却用不上」。这张表只活在本进程内，不落盘、不复用旧音频。
            try:
                bc_ = entry.get("broadcast")
                _m = getattr(self, "_bc_map", None)
                if _m is None:
                    _m = self._bc_map = {}
                _tk = (getattr(track, "title", "") or "").strip()
                if bc_:
                    _m[self._bc_key(track)] = bc_
                    if _tk:
                        _m[f"|{_tk}"] = bc_      # 纯标题键：id 变化时仍能命中
                logger.info("[口播] 映射登记 key=%s title=%s 表内=%d 条 broadcast=%s",
                            self._bc_key(track), _tk, len(_m), bool(bc_))
            except Exception as e:  # noqa
                logger.info("[口播] 映射登记异常：%s", e)
            entry["_gen_at"] = time.monotonic()
            entry["_gen_seq"] = self._broadcast_seq
            # ★★ 2026-09-21：**提前填喂检索缓存** —— 用"当前这首歌还在放"的墙钟，把
            #   接下来几首的检索资料先取回来落盘。轮到它们时 `format_context()` 是
            #   **纯缓存命中**（毫秒级），与当时的网络/搜索服务状态完全解耦。
            #   这正是考试件"资料恒在场"的做法（60 个专名各检索一次、缓存复用）。
            #   放在生成成功之后 ⇒ 绝不吃掉本曲口播的时间预算。
            try:
                threading.Thread(target=timed(self._prewarm_search, "search_warm"),
                                 args=(3,), daemon=True).start()
            except Exception:  # noqa 预热失败绝不影响口播
                pass
            # ---- 计时埋点：口播生成（LLM+TTS）耗时 ----
            llm_ms = (t_llm1 - t_llm0) * 1000.0
            tts_ms = (t_tts1 - t_tts0) * 1000.0
            logger.info(
                "[Timing] 口播生成 seq=%s 《%s》 联网=%s llm=%.0fms tts=%.0fms total=%.0fms",
                self._broadcast_seq, track.title, "是" if do_search else "否",
                llm_ms, tts_ms, llm_ms + tts_ms)
            self._emit("dj_timing", {
                "phase": "gen", "seq": self._broadcast_seq, "title": track.title,
                "search": do_search, "llm_ms": round(llm_ms),
                "tts_ms": round(tts_ms), "total_ms": round(llm_ms + tts_ms),
            })
            # 口播生成完毕：通知 UI 刷新推荐列表，使歌曲随口播就绪"逐渐"出现在列表里
            # （信号经 120ms 合并去抖，不会因并发生成而高频重建控件）。
            self._emit_playlist()
            # 真正由大模型生成成功：清掉上一次失败提示，避免重复刷屏
            if meta.get("used_ai"):
                self._last_fail = None
            elif getattr(self.brain, "llm", None) and self.brain.llm.enabled and meta.get("error"):
                # 已配置 AI 但生成失败（如内容审核/超时）：提示一次。
                # ★ 2026-11-17：**首曲不提示** —— 冷启动时 LLM 常没热起来，此时离线文案
                #   照样会播；把「AI 调用失败」推到口播栏会让人以为整套坏了。
                if opening:
                    logger.info("[口播] 首曲 AI 未就绪（%s），已用离线文案，不提示错误",
                                meta.get("error"))
                else:
                    self._note_llm_failure("broadcast", f"口播生成：{meta.get('error')}")
            # 若这正是当前曲且等待导听，立即播
            # ★★ 2026-11-18（用户「San Francisco 默认文案无口播」）：
            #   旧判断 `self.current is track` 靠对象身份——切歌/队列重建后 current 是
            #   新对象、entry 是旧的 ⇒ 补播永远不触发 ⇒ 导听晚到就"默认文案无口播"。
            #   改 title/id 匹配（同 opening 分支），且兜底后也允许真导听覆盖补播。
            _cur_track = getattr(self, "current", None)
            _is_cur = (track is not None and _cur_track is not None
                       and (getattr(_cur_track, "id", None) == getattr(track, "id", None)
                            or (getattr(_cur_track, "title", "") or "").strip()
                               == (getattr(track, "title", "") or "").strip()))
            if _is_cur and entry["broadcast"]:
                self.current_broadcast = entry["broadcast"]
                # 音乐已响（非首曲迟到导听）：即使 _intro_pending 已被 watchdog 清掉，
                # 也要补播真导听音频——_speak_intro 会叠播、不该被"先交给音乐"顶替。
                self._emit("thinking", True)
                threading.Thread(
                    target=timed(self._speak_intro, "tts_intro"),
                    args=(entry["broadcast"], self._play_seq),
                    daemon=True).start()
            elif _is_cur and getattr(self, "_intro_pending", False):
                # 口播生成了但为空（如离线兜底无文案 / TTS 失败）：不再等待，直接放歌
                self._start_intro_fallback()

        # ---------- ★ 2026-11-17：TTS 分时批处理（停大模型 → 批量合成 → 起大模型） ----------

        # ---------- ★ 2026-11-17：音频线程（把 TTS 从文案链里解耦） ----------
        # 为什么要有：实测 `want_audio=True` 之后 `synthesize_unique_tagged()` 会卡住
        # 等 worker 冷加载（20~60s），把整条文案链堵死 —— 表现为「文案都有、声音一直
        # 没有」。改成：链线程只出文案，音频丢给**独立一条线程**消费队列。
        # 线程安全边界：本线程只与 TTS worker 说话；LLM 调用仍在链线程里，
        # llama-server 保持 `-np 1` 串行（实测并发请求会排队，不会失败）。
        def _ensure_audio_worker(self) -> None:
            if getattr(self, "_audio_thread", None) is not None:
                return
            import queue as _q
            self._audio_q = _q.Queue()
            self._audio_thread = threading.Thread(
                target=self._audio_loop, daemon=True, name="tts-audio")
            self._audio_thread.start()
            logger.info("[口播] 音频线程已启动（TTS 与文案链解耦）")

        def _audio_loop(self) -> None:
            """⚠ 当前无人使用（`_audio_q` 没有任何 `put`）；保留仅为历史兼容。
            若将来启用：**必须**改走 `_enqueue_audio`/TTSBatch，禁止在此直连 TTS
            （会绕过换模型窗口，与大模型抢显存）。"""
            while True:
                item = self._audio_q.get()
                if item is None:
                    return
                entry, track, text = item
                t0 = time.monotonic()
                try:
                    wav = self.tts.synthesize_unique_tagged(
                        text, lang=getattr(self.tts, "language", None))
                except Exception as e:  # noqa
                    logger.warning("[口播] 音频线程合成失败：%s", e)
                    wav = None
                took = time.monotonic() - t0
                if wav:
                    try:
                        bc = entry.get("broadcast")
                        if isinstance(bc, dict):
                            bc["wav"] = wav
                        m = getattr(self, "_bc_map", None)
                        if m:
                            for k in (self._bc_key(track),
                                      f"|{(getattr(track, 'title', '') or '').strip()}"):
                                if k in m:
                                    m[k]["wav"] = wav
                        logger.info("[口播] 音频线程完成 %.1fs，已回填《%s》",
                                    took, getattr(track, "title", "?"))
                        self._emit_playlist()
                    except Exception as e:  # noqa
                        logger.info("[口播] 回填失败：%s", e)
                else:
                    logger.info("[口播] 音频线程未产出（%.1fs）《%s》",
                                took, getattr(track, "title", "?"))

        def _on_audio_done(self, key, wav) -> None:
            """TTSBatch 完成回调：把 wav 回填到 entry + 映射，并通知 UI。"""
            ent = getattr(self, "_audio_pending", {}).pop(key, None)
            if ent is None:
                return
            kind = "intro"
            if len(ent) == 3:
                entry, track, kind = ent
            else:
                entry, track = ent
            try:
                if wav and kind == "outro":
                    # ★ 2026-11-18 task-1：转场音频也由窗口产出，回填到 transition_wav
                    entry["transition_wav"] = wav
                    try:
                        entry["transition_dur_ms"] = self._audio_duration_ms(wav)
                    except Exception:  # noqa
                        pass
                    logger.info("[口播·转场] 音频完成并回填《%s》",
                                getattr(track, "title", "?") if track else "?")
                    self._emit_playlist()
                elif wav:
                    bc = entry.get("broadcast")
                    if isinstance(bc, dict):
                        bc["wav"] = wav
                    m = getattr(self, "_bc_map", None)
                    if m:
                        for k in (self._bc_key(track),
                                  f"|{(getattr(track, 'title', '') or '').strip()}"):
                            if k in m:
                                m[k]["wav"] = wav
                    logger.info("[口播] 音频完成并回填《%s》", getattr(track, "title", "?"))
                    self._emit_playlist()
                else:
                    logger.info("[口播] 音频未产出（%s）《%s》", kind,
                                getattr(track, "title", "?") if track else "?")
            except Exception as e:  # noqa
                logger.info("[口播] 音频回填失败：%s", e)

        def _on_tts_done(self, key, wav):
            """★ 2026-11-18 流水线统一回调：按 key 前缀分派到口播/音频回填。

            流水线模式下口播(bc-)与转场音频(a-)共用同一个 TTSBatch 队列，
            故 on_done 需要一个统一入口，再按 key 分流到原有两类处理。
            """
            if not key:
                return
            if key.startswith("a-"):
                self._on_audio_done(key, wav)
            else:
                self._on_batch_ready(key, wav)

        def _enqueue_audio(self, entry, track, text, kind: str = "intro") -> None:
            """把合成交给 TTSBatch：它会「停 LLM → 合成 → 起 LLM」。

            为什么不用裸线程：8GB 卡上 TTS(5.4G) 与 35B(3.4G) 同驻会互相换页，
            实测音频线程加载 worker 拖到 3~4 分钟仍不完成、一条都出不来。
            走 TTSBatch 的换模型窗口，合成是**独占显存**的，20~40 秒就能出。
            """
            try:
                # ★★ 2026-11-18 流水线：转场音频提交到**口播同一个** TTSBatch 队列，
                #   避免第二个实例异步合成时与下一条 LLM 抢 GPU（用户「一个就够了」）。
                if TTS_PIPELINE:
                    _b = self._tts_batcher()
                    if _b is None:
                        return
                    # ★ 修复：流水线分支提前 return，跳过了旧分支里 `_audio_pending={}`
                    #   的初始化 ⇒ 每个转场音频入队都 AttributeError ⇒ 静态兜底。
                    if getattr(self, "_audio_pending", None) is None:
                        self._audio_pending = {}
                    key = f"a-{kind}-{int(time.time() * 1000) % 100000}-{id(entry)}"
                    self._audio_pending[key] = (entry, track, kind)
                    _b.submit(key, text,
                              stale_check=(lambda tk=track: self._track_stale(tk)))
                    logger.info("[口播·流水线] 转场音频入同一队列《%s》（%s）",
                                getattr(track, "title", "?") if track else "?", kind)
                    return
                if getattr(self, "_audio_batch", None) is None:
                    from ai.tts_batch import TTSBatch
                    from core import llamacpp_manager as _llm
                    self._audio_pending = {}
                    # ★ task-1（摊薄窗口固定成本）：窗口固定开销 ~80s（停 3 + 起 17 +
                    #   KV 预热 60），1 条也照付。批大小 4；开窗规则由 TTSBatch 控制
                    #   （队列 ≥3 条 或 最老条目等 20s），可用
                    #   MUSICAI_AUDIO_BATCH_N / MUSICAI_TTS_OPEN_N / MUSICAI_TTS_OPEN_DELAY_S 覆盖。
                    n = max(1, int(os.environ.get("MUSICAI_AUDIO_BATCH_N", "4")))
                    try:
                        _idle = max(0.0, float(os.environ.get("MUSICAI_AUDIO_BATCH_IDLE_S")
                                               or 6.0))
                    except (TypeError, ValueError):
                        _idle = 6.0
                    self._audio_batch = TTSBatch(
                        self.tts, batch_size=n, idle_s=_idle,
                        on_done=self._on_audio_done,
                        llm_manager=_llm, warm_llm=self._warm_llm_prefix)
                    logger.info("[口播] 音频批处理已启动（批 %d idle %.0fs，合成时短暂停 LLM）",
                                n, _idle)
                key = f"a-{kind}-{int(time.time() * 1000) % 100000}-{id(entry)}"
                self._audio_pending[key] = (entry, track, kind)
                # ★ 2026-11-18：带 stale_check，合成前若这首已播过则跳过，省 worker
                self._audio_batch.submit(key, text,
                                         stale_check=(lambda tk=track: self._track_stale(tk)))
                logger.info("[口播] 已入音频批处理《%s》（%s）",
                            getattr(track, "title", "?") if track else "?", kind)
            except Exception as e:  # noqa
                # ★ task-1：不再「退回同步合成」——那会绕过换模型窗口、与大模型同驻，
                #   单条可能卡到分钟级。留空由播放侧成对静态兜底（声文一致）。
                logger.warning("[口播] 入音频批处理失败（不再直连合成，交给静态兜底）：%s", e)
                if getattr(self, "_audio_pending", None) is not None:
                    self._audio_pending.pop(key, None)

        def _enqueue_outro_audio(self, entry, text) -> None:
            """转场（outro）音频走同一个换模型窗口。

            ★ 2026-11-18 task-1：旧实现是转场线程里**直连**合成（会与大模型同驻，
            实测 GPU 7860MiB、合成拖到分钟级）——现在统一入 TTSBatch，由窗口产出，
            回填到 `entry["transition_wav"]`；没有就由播放侧成对静态兜底。
            """
            if not (text or "").strip():
                return
            self._enqueue_audio(entry, getattr(self, "current", None), text, kind="outro")

        def _synth_text(self, text: str, timeout: float = 5.0):
            """**统一合成入口**：批处理可用时走仲裁窗口；不可用则**不再直连引擎**。

            为什么统一：8GB 卡上 TTS（5.6GB）与大模型（4.4GB）不能同驻。
            只要有一条路径绕过仲裁窗口直连 TTS，就会把 worker 拉起来与大模型抢显存
            （实测：兜底朗读直连 ⇒ GPU 7782/8188 ⇒ 推荐补全被拖到 66 秒；更极端的
            是单条合成卡满 300s urllib 超时 ⇒「单曲 370s / 405s」）。
            ★ 2026-11-17 task-1：批处理未启用时直接返回 None，由调用方走「成对静态
            兜底」（文字=音频同一句）。绝不再回退直连。
            """
            if not text:
                return None
            b = self._tts_batcher()
            if b is None:
                logger.info("[口播] 批处理未启用 → 跳过直连合成，交给成对静态兜底")
                return None
            key = f"tx-{int(time.time() * 1000) % 1000000}-{abs(hash(text)) % 10000}"
            try:
                wav = b.submit_wait(key, text, timeout=timeout)
            except Exception as e:  # noqa
                logger.warning("批处理合成异常（不再直连）: %s", e)
                return None
            if wav:
                self._tts_pending.pop(key, None)
            return wav

        def _tts_batcher(self):
            """惰性创建批处理协调器；MUSICAI_TTS_BATCH!=1 时返回 None（走原同步路径）。"""
            b = getattr(self, "_tts_batch", None)
            if b is not None:
                return b
            if getattr(self, "_tts_batch_off", False):
                return None
            try:
                from ai.tts_batch import TTSBatch, batch_enabled
                if not batch_enabled():
                    self._tts_batch_off = True
                    return None
                from core import llamacpp_manager
                self._tts_pending = {}
                n = max(1, int(os.environ.get("MUSICAI_TTS_BATCH_N", "5")))
                self._tts_batch = TTSBatch(
                    self.tts, batch_size=n, idle_s=6.0,
                    on_done=self._on_tts_done,   # ★ 流水线：统一分派口播/转场回填
                    llm_manager=llamacpp_manager,
                    warm_llm=self._warm_llm_prefix)
                logger.info("[口播] TTS 批处理已启用（批大小 %d%s）", n,
                            " · 流水线（口播+转场单队列）" if TTS_PIPELINE else " · 分时")
                return self._tts_batch
            except Exception as e:  # noqa 初始化失败绝不能影响口播主链
                logger.warning("TTS 批处理初始化失败，退回同步合成: %s", e)
                self._tts_batch_off = True
                return None

        def _on_batch_ready(self, key, wav):
            """批处理完成：把 wav 填回条目、清标记、刷新列表。"""
            entry = getattr(self, "_tts_pending", {}).pop(key, None)
            if entry is None:
                return
            try:
                bc = entry.get("broadcast")
                if isinstance(bc, dict):
                    if wav:
                        bc["wav"] = wav
                    else:
                        logger.warning("[口播] 批处理未产出音频（该条将静音）")
                entry["_tts_key"] = None
                self._emit_playlist()
            except Exception as e:  # noqa
                logger.warning("批处理回调填回失败: %s", e)

        def _warm_llm_prefix(self):
            """大模型重启后预热 KV 前缀（一次性成本，避免首条口播整段预填充）。

            ★★ 2026-11-18 P0：旧实现 `llm.chat("预热","预热")` 走默认 budget=512
              + 开思考 → max_tokens=1152 → 实测 **41~71s/窗**，且生成的内容全是
              丢弃的废话。预热的唯一目的是把系统提示词的 KV prefill 掉，
              **根本不需要生成长文**。改为 budget=0（关思考、max_tokens=300）
              后，prefill 照做、生成骤减。真机验证脚本 _verify_tts_batch.py:57-68
              用的正是 max_tokens=4 的极短请求。
            """
            try:
                llm = getattr(self.brain, "llm", None)
                if llm is None or not getattr(llm, "enabled", False):
                    return
                llm.chat("预热", "预热", thinking=False, budget=0)
                logger.info("[口播] 大模型 KV 前缀已预热（短生成）")
            except Exception as e:  # noqa
                logger.info("前缀预热跳过: %s", e)

        @staticmethod
        def _queue_sig(up: list) -> str:
            """队列指纹：前 4 首的「id|标题」拼接哈希。

            用途：随机/漫游会在链子安装后重排队列，快照与实际播放错位。
            指纹变了就说明顺序变了，链子必须按新顺序重装。
            """
            try:
                import hashlib as _h
                parts = []
                for e in up[:4]:
                    tr = e.get("track")
                    parts.append(f"{getattr(tr, 'id', '')}|{getattr(tr, 'title', '')}"
                                 if tr is not None else "-")
                return _h.md5("␟".join(parts).encode("utf-8")).hexdigest()[:12]
            except Exception:  # noqa
                return ""

        def _prep_next_broadcast(self) -> None:
            """为待播列表里的歌补生成口播（背景），并附带列表位置信息；
            同时为每首待播歌预生成"与前一首之间的转场决策"（背景）。"""
            self._clean_upcoming()  # 防御：下方读取 e["track"].title / .id
            with self._qlock:
                up = list(self.upcoming)
            self._prep_sig = self._queue_sig(up)   # ★ 记下本次预取依据的队列顺序
            self._prefetch_track_cache(up)     # R99-T4：同一入口顺带预检索（不另开定时器）
            total = 1 + len(up)
            prev = self.current
            # ★★★ 2026-11-18（用户「一首播放时，下一首=upcoming[0]」）：
            #   当前曲绝不该由串行链生成导听（它用固定开场白）。首曲启动存在时序窗口：
            #   _prep_next_broadcast 可能在 _pop_to_current 之前跑，此时 upcoming[0]
            #   仍是当前首曲 ⇒ 误生成一份用不上的 AI 导听，和补推荐抢 -np1 槽位。
            #   这里直接过滤掉「track == current」的条目，无论时序怎样都不生成。
            _cur_id = getattr(self.current, "id", None)
            if _cur_id is not None:
                up = [e for e in up
                      if getattr(e.get("track"), "id", None) != _cur_id]
                total = 1 + len(up)
            # ★★★ R119-4u：**只预生成「马上要用到的前 N 首」，且转场只做队首那一首**。
            #   为什么（用户真机日志钉死，2026-09-19 21:13）：
            #     `play_all_library()`（cap=100）/ `play_tracks()`（cap=200）会把待播队列
            #     **一次性填到上百首**，而旧实现对 `up` 里**每一首**都发两条 LLM 请求
            #     （导听口播 + 转场）。于是在唯一的一首 Ollama 串行锁前瞬间堆了几百条任务：
            #     **转场**（便宜、不联网）成批先跑完 —— 用户看到的正是「一下子加载一堆
            #     outro」；而**当前曲真正要播的导听口播**排在几百条之后，等它拿到锁时歌
            #     早播完了 ⇒ 触发迟到兜底 ⇒「intro 不生成，只有『这首《X》，Y 的，
            #     交给耳朵。』」。日志实证：21:13:03→21:13:55 连发 **50+ 条
            #     `[口播·转场]`**，同段里 `[Timing] 口播生成` 只有 **1 条**。
            #   · 转场只对**队首**有意义（它决定本曲结尾播什么）；其余歌等它升到队首时
            #     由下一轮 `_prep_next_broadcast` 生成（`_gen_t` 幂等守卫保证不重复）。
            #     而且那时算出的「前一首」才是真的前一首 —— 队列会滚动，提前 100 首算出来
            #     的 prev 到播出时早已不是那一首。
            #   · 导听口播只需「先于它自己开播」备好 ≈ 一首歌的提前量，故前 N 首足够。
            # ★★★ R119-4x：**组装一条串行链**，交给单 worker 按顺序内联执行：
            #     口播(歌1) → 转场(歌1→歌2) → 口播(歌2) → 转场(歌2→歌3) → …
            #   为什么改（用户 2026-09-20 拍板）：口播与转场现在要**互相看得见**
            #   （转场吃上一首的导听口播文本；下一首口播吃「最近播放」上下文），
            #   顺序就必须确定；而并行时它们是三条线程抢同一把**非 FIFO** 的 Ollama 锁。
            #   与旧 R119-4u 的区别：那时"转场只做队首"是为了避免转场成批插队；
            #   现在改为串行 ⇒ 转场只可能排在**它自己那首歌的口播之后**，
            #   天然不会挤占导听口播，故链内每首都给转场（正好是用户要的序列）。
            #   ⚠ 幂等守卫（`_gen` / `_gen_t`）必须在**入链时刻**同步置位 ——
            #     否则 `_prep_next_broadcast` 重入会把同一首歌重复入链。
            steps: list = []
            head_steps: list = []                  # ★ 队首那首的导听（必须最先要）
            trans_steps: list = []                 # ★ 只收「队首 → 下一首」这一条转场
            prev_entry = None                      # None ⇒ "上一环"就是当前正在播的这首
            for i, e in enumerate(up[:BROADCAST_PREFETCH_N]):
                nxt = up[i + 1]["track"].title if i + 1 < len(up) else None
                if not e.get("_gen"):
                    e["_gen"] = True
                    _st = {"kind": "b", "i": i, "entry": e, "position": i + 2,
                           "total": total, "next_title": nxt}
                    # ★★ 2026-11-18（用户报「首曲换成 AI 文案，不是固定开场白」）：
                    #   串行链预生成抢在 radio.py 开播前把 upcoming[0] 生成成 AI 文案，
                    #   等开播时 broadcast 已被占 ⇒ opening=True 的固定开场白进不来。
                    #   修：本进程还没播过歌（current is None）时，队首步骤也走 opening，
                    #   让串行链用 _opening_static() 固定开场白；切歌后 current 非空则不打，
                    #   正常用 AI 文案（避免旧 skip_tts 那个「每首轮到队首都被跳过」的坑）。
                    if (i == 0 and getattr(self, "current", None) is None
                            and getattr(self, "broadcast_language", "zh") == "zh"):
                        _st["opening"] = True
                    (head_steps if i == 0 else steps).append(_st)
                # ★★★ 2026-11-17 task-1（用户拍板）：**转场只做队首那一首**。
                #   为什么（真机日志 20:13~20:19 实证）：旧实现给前 4 首都排了转场，
                #   而 4 条导听 LLM 实测合计 339.7s > 本曲 204s ⇒ 队首转场排在第 5 步，
                #   等轮到它时歌早播完（20:17:07「脚本 0 字」就是这么来的）。
                #   其余歌的转场等它**升到队首**时由下一轮 `_prep_next_broadcast`
                #   生成（`_gen_t` 幂等守卫保证不重复），那时算出的前一首才是真的前一首。
                if TRANSITION_ENABLED and i == 0 and prev is not None and not e.get("_gen_t"):
                    e["_gen_t"] = True
                    e.setdefault("transition",
                                 {"transition_type": "intro", "reason": "", "script": ""})
                    trans_steps.append({"kind": "t", "i": i, "entry": e,
                                        "from_track": prev,
                                        "to_track": e["track"], "src": prev_entry})
                prev = e["track"]
                prev_entry = e
            # ★★★ 2026-11-18（用户重新拍板流水线顺序）：**本首转场 → 下首导听 → 其余**。
            #   用户原话：「一首歌之内，先把本首转场和下首开场做好；先推荐（一首歌内先
            #   补推荐），然后出它的转场，出下一首的导听，整个流水线就是这个顺序」。
            #   即一首歌的时间窗内：① 推荐（软闸让路先行，见 _chain_worker）→
            #   ② 本首转场 → ③ 下首导听。转场吃的是**本首**（正在播的）导听文本，
            #   那在上一轮已生成好、存于 entry，不依赖本轮的 head_steps ⇒ 对调安全。
            #   （旧顺序 head→trans 是 11-17 定的，当时为让转场吃到队首导听；现按用户
            #    新顺序改为 trans→head，转场更早出、下首导听紧随其后，都在本首窗内完成。）
            steps = trans_steps + head_steps + steps
            if steps:
                self._chain_install(steps, getattr(self, "_nav_seq", 0))
            if len(up) > BROADCAST_PREFETCH_N:
                logger.info("[串行链] 待播 %d 首 → 本轮只串行预生成前 %d 首"
                            "（队首导听→队首转场→其余导听；其余转场升到队首再生成）",
                            len(up), BROADCAST_PREFETCH_N)
            # ★★ 2026-11-17：**补一次迟到的重触发**。
            #   队列常常是在起播之后才逐渐填满的（推荐分几次回来），而旧实现只在
            #   「音乐起播 / 切歌」时触发预取 ⇒ 后填进来的那几首永远没人生成。
            #   这里自续一次：仍有未生成条目且没切歌，就 40s 后再看一遍。
            try:
                _need = any(not e.get("_gen") for e in up[:max(BROADCAST_PREFETCH_N, 4)])
                # ★ task-1：转场现在只做队首那条 ⇒ 队首转场没生成也要重试，
                #   否则它要等到下一首切歌才可能被补（边界只能吃静态兜底）。
                if up and not up[0].get("_gen_t"):
                    _need = True
                if _need:
                    nav = getattr(self, "_nav_seq", 0)
                    t = threading.Timer(40.0, self._prep_retry, args=(nav,))
                    t.daemon = True
                    t.start()
            except Exception:  # noqa
                pass

        def _prep_retry(self, nav_seq: int) -> None:
            """40s 后的预取重试：没切歌、且队列有变化/仍有待生成条目时再跑一次。"""
            try:
                if getattr(self, "_nav_seq", 0) != nav_seq:
                    return
                if self.current is None:
                    return
                with self._qlock:
                    up = list(self.upcoming)
                sig = self._queue_sig(up)
                old = getattr(self, "_prep_sig", "")
                if sig and sig != old:
                    # ★ 队列被重排（随机/漫游补货）⇒ 旧链生成的歌已经不在该播的位置，
                    #   必须按新顺序重装，否则又是「生成了却播不到」。
                    logger.info("[串行链] 队列顺序变化 %s → %s，按新顺序重装", old or "?", sig)
                    for e in up:
                        e.pop("_gen_t", None)   # 转场按新前驱重算
                    self._prep_next_broadcast()
                    return
                _need = any(not e.get("_gen") for e in up)
                if up and not up[0].get("_gen_t"):
                    _need = True
                if _need:
                    logger.info("[串行链] 队列仍有未生成口播/队首转场 → 追加一轮预取")
                    self._prep_next_broadcast()
            except Exception as e:  # noqa
                logger.warning("预取重试失败: %s", e)

        # ---------- ★ R119-4x：口播 / 转场 串行链 ----------

        def _chain_state(self) -> None:
            """惰性补齐链状态（mixin 无 __init__，测试桩用 __new__ 构造也绝不崩）。"""
            if getattr(self, "_chain_lock", None) is None:
                self._chain_lock = threading.RLock()
                self._chain_steps = None
                self._chain_gen = 0
                self._chain_nav_seq = 0
                self._chain_running = False

        def _chain_install(self, steps: list, nav_seq: int) -> None:
            """安装一条串行链；**只保留最新的一条**（旧 worker 见世代号变了即中止）。

            nav_seq 是控制器导航世代（切歌/重播自增）：链执行期间用户若切了歌，
            整条链作废 —— 否则会给已经不在队列里的歌生成口播/转场。
            """
            self._chain_state()
            started = False
            with self._chain_lock:
                self._chain_steps = list(steps)
                self._chain_nav_seq = nav_seq
                self._chain_gen += 1
                if not self._chain_running:
                    self._chain_running = True
                    started = True
            logger.info("[串行链] 已安装 %d 步（世代 %d）：%s", len(steps), self._chain_gen,
                        " → ".join(("口播" if s["kind"] == "b" else "转场") for s in steps))
            if started:
                threading.Thread(target=timed(self._chain_worker, "llm_chain"),
                                 daemon=True).start()

        def _chain_critical_wait(self) -> None:
            """**给关键任务让路**（有硬上界，绝不卡死）：当前曲的导听口播还没出声时，
            先等一等再走下一步 —— 免得链上的一步正好占住那把 Ollama 串行锁。

            ⚠ 上限可用 `MUSICAI_CHAIN_YIELD_S` 覆盖（默认 8s）；等待期间不持任何锁，
              因此不会与口播互相死等。超时后**继续做**，绝不因为让路而丢步骤。
            """
            try:
                # ★ 2026-11-19：默认 8s→60s。实测手动切歌后当前曲导听 LLM+新音色TTS
                #   要 40~170s，8s 让路后串行链就抢走 LLM 单槽位 ⇒ 当前曲排到 110s
                #   才出文案。延长到 60s 让当前曲先于队首预取完成（watchdog 45s 清
                #   _intro_pending 会自然提前结束让路，加上界不会死等/丢步骤）。
                budget = max(0.0, float(os.environ.get("MUSICAI_CHAIN_YIELD_S") or 60.0))
            except Exception:  # noqa 环境变量写坏 → 用默认
                budget = 60.0
            t0 = time.monotonic()
            while getattr(self, "_intro_pending", False) and (time.monotonic() - t0) < budget:
                time.sleep(0.2)

        def _chain_rollback(self, steps) -> int:
            """★ R119-6a：回滚**未执行步骤**的幂等标志（`_gen` / `_gen_t`）。

            为什么必须有：这两个标志在**入链时刻**就置了位（防重入），而链是
            **可被中止**的（切歌 / 新链接管 / 单步失败）。中止后若不回滚，
            那一首就永久停留在"已生成" ⇒ 之后**没有任何一轮**会为它生成
            ⇒ 它开播时 `current_broadcast is None` ⇒ 走兜底开场白。
            用户现象正是："口播不像在上一首播放时预生成好的，而像放到这一首才现生成"。

            ⚠ 必须跳过"**当前链**里已重新入队"的同名步骤（同 entry 同 kind）——
              否则同一首会被生成两份（一份进旧链、一份进新链）。
            """
            self._chain_state()
            with self._chain_lock:                    # RLock：可在持锁分支里安全再入
                live = {(id(s.get("entry")), s.get("kind"))
                        for s in (self._chain_steps or [])}
            n = 0
            for st in (steps or []):
                if (id(st.get("entry")), st.get("kind")) in live:
                    continue
                e = st.get("entry")
                if not isinstance(e, dict):
                    continue
                flag = "_gen" if st.get("kind") == "b" else "_gen_t"
                if e.pop(flag, None) is not None:
                    n += 1
            if n:
                logger.info("[串行链] 已回滚 %d 个未执行步骤的幂等标志"
                            "（下一轮会重新预生成，绝不永久跳过）", n)
            return n

        def _chain_worker(self) -> None:
            """串行执行链：口播(歌i) → 转场(歌i→歌i+1) → 口播(歌i+1) → …"""
            self._chain_state()
            while True:
                with self._chain_lock:
                    steps = self._chain_steps
                    gen = self._chain_gen
                    nav = self._chain_nav_seq
                    self._chain_steps = None
                    if not steps:
                        self._chain_running = False
                        return
                # ★★★ 2026-11-18（用户定死的整体顺序）：
                #   「一首歌开始播放 → recommend 出空缺 → 立刻 AI 补一个 →
                #     补完才做本首转场 + 下首导听。先保证音乐不枯竭，再保证转场/导听。」
                #   推论：每条新链执行前，先等当前补推荐(_reco_loading)完成，
                #   再进转场/导听流水线。这样补推荐独占 LLM 先跑，音乐货架先满，
                #   流水线让路——不会像之前那样口播抢先占 LLM 把推荐挤到最后。
                #   每首歌一步不落：用 gen 判新链，每条链只让路一次。
                if gen != getattr(self, "_chain_last_yield_gen", -1):
                    self._chain_last_yield_gen = gen
                    _t0 = time.monotonic()
                    # ★★ 2026-11-18（用户「导听优先，品味后台并行」）：
                    #   不再等品味分析！实测守卫生等品味112s，把转场+导听推迟到首曲
                    #   快结束才启动 ⇒ 切歌时下首导听没生成好（切歌后51s才出）。
                    #   现在只等补推荐（先保证音乐不枯竭），随即启动导听流水线；
                    #   品味分析后台并行跑（LLM排队在导听后、慢点无所谓，只影响后续推荐）。
                    while getattr(self, "_reco_loading", False) and (time.monotonic() - _t0) < 180.0:
                        time.sleep(0.5)
                    logger.info("[串行链] 补推荐让路完成（%.1fs）→ 转场/导听流水线（世代 %d；品味后台并行）",
                                time.monotonic() - _t0, gen)
                _nav_logged = False
                for idx, st in enumerate(steps):
                    with self._chain_lock:
                        if gen != self._chain_gen:
                            logger.info("[串行链] 世代已更新（新链接管），本链在第 %d 步中止", idx + 1)
                            # ★ R119-6a：被顶掉时也要回滚剩余步骤 —— 否则那些
                            #   已置位的 `_gen`/`_gen_t` 会把它们**永久跳过**。
                            self._chain_rollback(steps[idx:])
                            break
                    if getattr(self, "_nav_seq", 0) != nav:
                        # ★ R119-6b：**不再整链作废**（那会让还没做的步骤连同标志一起
                        #   被丢下 ⇒ 那几首永久不被预生成）。世代号只留作日志线索，
                        #   真正的取舍交给下面的**逐步**有效性判定。
                        if not _nav_logged:
                            logger.info("[串行链] 导航世代已变（切歌）→ 转为**逐步**校验"
                                        "（不再整链作废）")
                            _nav_logged = True
                    self._chain_critical_wait()
                    # ★ 2026-11-19：让路（最长60s）期间语言/队列可能已切换（_invalidate
                    #   会 +chain_gen）。等完必须再验一次世代，否则旧语言步骤在让路后
                    #   继续用旧快照生成 ⇒ 中英错配。gen 变了就中止本步并回滚标志。
                    with self._chain_lock:
                        if gen != self._chain_gen:
                            logger.info("[串行链] 让路后世代已变（语言/队列切换）→ 中止第 %d 步",
                                        idx + 1)
                            self._chain_rollback([st])
                            break
                    if not self._chain_step_valid(st):
                        logger.info("[串行链] 第 %d 步目标已不在待播队列 → 跳过并放开标志",
                                    idx + 1)
                        self._chain_rollback([st])
                        continue
                    try:
                        self._chain_run_step(st)
                    except Exception as e:  # noqa 单步失败绝不让整条链崩掉
                        logger.warning("[串行链] 步骤失败（跳过并继续）: %s", e)
                        # ★ R119-6a：失败也要放开标志 —— 否则"生成失败"同样
                        #   等于永久跳过（下一轮至少还能再试一次）。
                        self._chain_rollback([st])
                # 本链跑完（或被新链 break）→ 回到外层：有更新的链就接着做，没有就退出。

        def _chain_step_valid(self, st: dict) -> bool:
            """★ R119-6b：这一步**现在还有意义**吗？

            判据 = 目标 entry **仍在待播队列**（同对象）或**就是当前正在播的那首**。

            为什么不能用"导航世代（`_nav_seq`）变了就整链作废"（R119-4x 的旧规则）：
              切歌那一瞬间 `_after_music_started` 就会**立刻**跑新一轮
              `_prep_next_broadcast`，而旧 worker 要到**下一个步骤边界**才察觉世代变了
              （每步 LLM 5~30s + TTS 6s）⇒ 新一轮在"标志还是 True"时组链、**跳过那两首**；
              旧 worker 事后才回滚标志，此刻**没有任何链持有它们**；
              等到再下一轮，那首歌已升为**当前曲**、不在 `upcoming` 里 ⇒ 永远不生成。
              真机现象正是："口播不像在上一首播放时预生成好的，而像放到这一首才现生成"
              （并伴随转场缺失、开播时 `_start_intro_fallback` 兜底）。
            改判"逐步有效性"后：该跳过的照样跳过（不会给已出队的歌生成），
            但**每一步都各自判定**，不会把还没做的整批丢掉。
            """
            e = st.get("entry")
            try:
                with self._qlock:
                    if any(x is e for x in self.upcoming):
                        return True
                cur = getattr(self, "current", None)
                return bool(cur is not None and isinstance(e, dict)
                            and e.get("track") is cur)
            except Exception:  # noqa
                return True     # 判定失败一律"当作有效"：宁可多做，绝不永久跳过

        def _chain_run_step(self, st: dict) -> None:
            """执行一步。口播直接内联 `_do_gen_broadcast`；转场带上**上一环的口播文本**。"""
            if st["kind"] == "b":
                self._do_gen_broadcast(st["entry"], st["position"], st["total"],
                                       st.get("next_title"), st.get("opening", False),
                                       skip_tts=bool(st.get("skip_tts", False)))
                return
            # ---- 转场：吃上一环（上一首歌）的导听口播文本，让衔接有据可依 ----
            entry = st["entry"]
            with self._qlock:
                still = any(e is entry for e in self.upcoming)
            if not still:
                logger.info("[串行链] 转场目标已不在待播队列，跳过")
                return
            src = st.get("src")
            if src is None:
                cb = getattr(self, "current_broadcast", None) or {}
                fb = (cb.get("text") or "")
            else:
                fb = ((src.get("broadcast") or {}).get("text") or "")
            self._do_gen_transition(st["entry"], st["from_track"], st["to_track"],
                                    from_broadcast=fb, yield_intro=False)

        # ---------- R99-T4：预检索（「边播边想」范式） ----------
        def _prefetch_track_cache(self, upcoming, n: int = 2) -> int:
            """把待播列表**前 n 首**的直链/歌词提前取好（后台线程，幂等，不阻塞）。

            复用口播 `_gen_broadcast` 的两条范式：
              · **幂等守卫**：`entry["_pf"]`（in-flight 标记）+ `_prefetching` 集合 ——
                同一首歌在同一时刻只会有一条预取线程（缓存在库，跨进程/跨轮天然幂等）；
              · **后台线程**：预取绝不占播放线程（`resolve_url` 是网络 IO）。
            与口播的区别：口播是"把文案想出来"，预取是"把数据拿回来"，两者都发生在
            「音乐已起播」之后，所以挂在同一个入口（`_prep_next_broadcast`）最省。
            返回本轮**新启动**的预取条数（0 = 全部已就绪/在途 —— 可断言）。
            """
            if n <= 0:
                return 0
            db = getattr(self, "db", None)
            if db is None:
                return 0
            inflight = getattr(self, "_prefetching", None)
            if inflight is None:
                inflight = set()
                self._prefetching = inflight
            started = 0
            for e in upcoming[:max(0, int(n))]:
                track = e.get("track") if isinstance(e, dict) else None
                if not self._is_track(track):
                    continue
                src = str(getattr(track, "source", "") or "")
                ext = getattr(track, "ext_id", None)
                if not src or src == "local" or not ext:
                    continue                      # 本地曲无需预取
                k = f"{src}|{ext}"
                if e.get("_pf") or k in inflight:
                    continue                      # 幂等：在途或已启动过
                if db.get_cache("url", src, ext):
                    e["_pf"] = True               # 已就绪（库里有货）→ 只打标记
                    continue
                e["_pf"] = True
                inflight.add(k)
                started += 1
                threading.Thread(target=timed(self._do_prefetch, "prefetch"),
                                 args=(track, k), daemon=True).start()
            return started

        def _do_prefetch(self, track, key: str) -> None:
            """预取线程体：直链（TTL 6h）+ 歌词（永久）。任何异常都吞掉并解锁。

            ⚠ 预取失败**不写缓存**（与 `_resolve_url_cached` 同契约）——
            瞬时失败被缓存 6h 会让这首歌"坏一整天"。失败只解锁，下次入口再试。
            """
            try:
                src = str(getattr(track, "source", "") or "")
                ext = getattr(track, "ext_id", None)
                db = getattr(self, "db", None)
                fn = getattr(self, "_resolve_with_retry", None)
                if db is not None and src and ext and fn:
                    url = db.get_cache("url", src, ext)
                    if not url:
                        url = fn(track)
                        if url:
                            db.set_cache("url", src, ext, url)
                    if not db.get_cache("lyric", src, ext):
                        sm = getattr(self, "source_manager", None)
                        text = coerce_lrc_text(sm.lyric(src, ext)) if sm is not None else None
                        if text:
                            db.set_cache("lyric", src, ext, text)
            except Exception as e:  # noqa 预取是优化：失败绝不影响播放
                logger.debug("预取失败（下轮重试）: %s", e)
            finally:
                st = getattr(self, "_prefetching", None)
                if st is not None:
                    st.discard(key)

        def prefetch_state(self) -> dict:
            """R99-T4 可观测面（真机/套件可断言）：在途预取数 + 本轮启动计数。"""
            st = getattr(self, "_prefetching", None)
            return {"inflight": len(st) if st is not None else 0,
                    "flagged": sum(1 for e in list(getattr(self, "upcoming", []) or [])
                                   if isinstance(e, dict) and e.get("_pf"))}

        @staticmethod
        def _song_info(track) -> dict:
            info = {
                "title": track.title,
                "artist": track.artist or "未知",
                "genre": track.genre or "",
                "mood": "",
                "bpm": "",
            }
            # ★ 2026-11-17：补**客观事实**（本地零网络）。库里 genre 0/427、mood 恒空，
            #   转场提示词原先在"没有任何数据"上判断流派/年代/BPM ⇒ 实测会编。
            #   取不到就留原样，绝不因为查库失败影响转场。
            try:
                from core import song_facts as _sf
                f = _sf.facts(track.title, track.artist or "")
                info["_facts"] = f
                if f.get("year"):
                    info["year"] = f["year"]
                if f.get("bpm"):
                    info["bpm"] = round(float(f["bpm"]))
                if f.get("energy_db") is not None:
                    info["energy_db"] = round(float(f["energy_db"]), 1)
            except Exception:  # noqa
                pass
            return info

        @staticmethod
        def _estimate_tts_ms(text: str) -> int:
            """粗略估算中文 TTS 时长(ms)，用于转场排程对齐（约 4.5 字/秒）。"""
            if not text:
                return 0
            return max(1500, int(len(text) * 220))

        @staticmethod
        def _audio_duration_ms(path: str | None) -> int:
            """读取音频真实时长(ms)；非 WAV（如 edge 产出的 mp3）返回 0，交由估算兜底。"""
            if not path:
                return 0
            try:
                import wave
                with wave.open(str(path), "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                    if rate > 0:
                        return int(frames / rate * 1000)
            except Exception:  # noqa
                return 0
            return 0

        def get_playlist_entries(self) -> list[dict]:
            entries = []
            if self.current is not None:
                entries.append(self._entry_dict(self.current, True))
            with self._qlock:
                for e in self.upcoming:
                    entries.append(self._entry_dict(e["track"], False))
            return entries

        def _entry_dict(self, track, current: bool) -> dict:
            # 「推荐/播放队列」里的红心只反映用户显式点过红心的歌（self._queue_fav），
            # 不再直接读 db.is_favorite——否则 favorite_all_local 把全部本地歌标为收藏，
            # 会导致推荐列表（尤其当前播放的本地歌）被自动点亮红心。
            # 曲库(我喜欢的音乐)仍由 library.favorites() 显示全部本地红心，互不影响。
            # disliked 同理：反映用户显式点过「不喜欢」的歌（隐藏库，对 AI 生效）。
            return {
                "id": track.id, "title": track.title,
                "artist": track.artist or "未知", "current": current,
                "duration": getattr(track, "duration", 0) or 0,
                "source": getattr(track, "source", "local") or "local",
                "ext_id": getattr(track, "ext_id", None),
                "cover": getattr(track, "cover", None) or "",
                "album": getattr(track, "album", "") or "",
                "favorited": self.fav_key(track) in self._queue_fav,
                "disliked": self.fav_key(track) in self.disliked_keys,
            }

        def get_history(self) -> list:
            return list(self.history)

        def get_library(self) -> list:
            try:
                return self.library.favorites()
            except Exception:  # noqa
                return []

        # ---------- 事件发射 ----------

        def _emit_playlist(self) -> None:
            self._clean_upcoming()  # 防御：丢弃任何 track 非 Track 的脏条目
            now = time.monotonic()
            if self._playlist_timer is not None:
                # 已排了一次延迟合并发射，仅标记脏，等它触发即可
                self._playlist_dirty = True
                return
            if now - self._last_playlist_emit < 0.12:
                # 发射过快：推迟 120ms 合并一次，避免主线程序建控件过频导致卡顿
                self._playlist_dirty = True
                self._playlist_timer = threading.Timer(0.12, self._flush_playlist)
                self._playlist_timer.daemon = True
                self._playlist_timer.start()
                return
            self._last_playlist_emit = now
            self._emit("playlist_updated", self.get_playlist_entries())

        def _flush_playlist(self) -> None:
            """定时器回调：把最近一次「脏」的播放列表合并发射一次。"""
            self._playlist_timer = None
            if self._playlist_dirty:
                self._playlist_dirty = False
                self._last_playlist_emit = time.monotonic()
                self._emit("playlist_updated", self.get_playlist_entries())

        def _emit_history(self) -> None:
            self._emit("history_changed", self.get_history())

        def _emit_library(self) -> None:
            self._emit("library_changed", self.get_library())

        # ---------- 播放控制 ----------

