"""AI 电台编排：本模块承载按职责拆出的 mixin 引擎之一。

见 ai/radio.py 的总述：AIRadioController 通过多重继承组合
PlaybackOrchestrator / ScheduleEngine / TransitionHandler，本文件只负责"音频进退与
ducking / 口播播放 / 进度回调"这一职责。所有方法直接操作 self（即控制器实例），共享
状态与跨职责互调经继承链透明解析，行为与原单体实现完全一致。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path

from utils.weather import get_weather
from utils.location import get_city, time_slot
from config.settings import PlayerConfig, RadioConfig, SHORT_AUDIO_RATIO, user_config
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

# 天气口播机械降频：相邻两次「提供天气」之间至少间隔多少首歌。
WEATHER_MIN_GAP = 5

class PlaybackOrchestrator:
        def _play_current(self, force_intro: bool = False,
                          wait_intro: bool = False) -> None:
            if self.current is None:
                return
            self._play_track(self.current, self.current_broadcast, fade_in=False,
                             force_intro=force_intro, wait_intro=wait_intro)

        # ═══ 切歌前「同步停旧曲」（R49-23）═══
        # 根因：旧曲的 pygame 缓冲要等 _start_audio 后台线程真正 load() 新歌才停，
        # 中间隔着音源解析/下载（远程数百 ms~数秒），期间旧歌继续响 → 「前一首
        # 还会放几秒」。且旧曲 fade_out / ducking 会遗留 _ducking 标志，污染新歌。
        # 手动切歌（next/prev/jump_to/start_radio/play 系）在 _play_track 之前同步调用，
        # 立即：停音乐通道 + 停 TTS 通道（迟到口播不再叠到新歌）+ 复位 ducking 音量。
        def _stop_old_audio(self) -> None:
            try:
                self.player.stop(unduck=True)   # 立即停旧曲 + 复位音量状态
            except Exception:  # noqa
                pass
            try:
                # 停掉上一首的迟到口播/转场朗读，避免旧旁白叠在新歌上继续念
                if self.player.audio_ok and self.player.is_tts_playing():
                    self.player.stop_tts()
            except Exception:  # noqa
                pass
            try:
                # 位置跟踪线程已被 stop() 置 flag 即将退出：这里不等它自然死，
                # 直接重启，保证新歌起播后进度/结束检测立刻可用（不丢进度）。
                self.player.restart_position_tracker()
            except Exception:  # noqa
                pass
            self._music_started = False

        # ═══ R95d-Q0：「正在准备播放」意图标记 ═══
        # 根因（代码实证，见 docs/water_physics_research.md §10.1）：`_play_track` 起手
        # 就**同步**停旧曲（player → STOPPED），而真正起播在**后台线程**（解析直链 /
        # 下载 / 装载，数百 ms~数秒）。这段装载窗内 `/api/state` 报 `playing=false`
        # ⇒ 前端 2s 轮询把 false 灌进 `st.playing` ⇒ `calm` 0.28s 冲顶、h 场 0.5s 掉
        # 79%（用户观感「水波像被集体清掉」）。⚠ 关键：**切歌路径从不发 WS
        # `playing=false`** —— 那个 false 是**纯误报**（电台不是暂停，是在准备下一首）。
        # 修法：对外报**意图**（正在播 **或** 正在准备播），而不是瞬时引擎状态。
        def _set_preparing(self, seq) -> None:
            self._preparing_play = True
            self._preparing_seq = seq

        def _clear_preparing(self, seq=None) -> None:
            """复位意图标记。`seq` 给定时**仅当它仍是当期意图**才清 —— 过期加载
            线程不得清掉新一次切歌刚置起的标记（否则又回到误报）。"""
            try:
                if seq is not None and getattr(self, "_preparing_seq", None) not in (None, seq):
                    return
            except Exception:  # noqa
                pass
            self._preparing_play = False
            self._preparing_seq = None

        def is_playing_intent(self) -> bool:
            """「正在播放 **或** 正在准备播放」——供 `/api/state` 对外报意图（R95d-Q0）。

            暂停/停止时引擎为 paused/stopped 且意图已复位 ⇒ 仍正确报 False，
            与「用户点暂停」的 WS `playing=false` 语义一致（不吞真暂停）。
            """
            try:
                if self.player.state.value == "playing":
                    return True
            except Exception:  # noqa
                pass
            return bool(getattr(self, "_preparing_play", False))

        def _play_track(self, track, broadcast, fade_in: bool = False,
                        force_intro: bool = False, wait_intro: bool = False) -> None:
            if track is None:
                return
            prev_title = getattr(self.current, "title", None) if self.current else None
            logger.info(
                "[radio] _play_track 落子 《%s》 prev_current=%s force_intro=%s wait_intro=%s "
                "music_started=%s intro_pending=%s nav=%s seq=%s",
                track.title, prev_title, force_intro, wait_intro,
                getattr(self, "_music_started", None),
                getattr(self, "_intro_pending", None),
                getattr(self, "_nav_seq", None), getattr(self, "_play_seq", None))
            # External recommendations are ephemeral objects at first. Persist
            # their metadata before logging playback, without favoriting them:
            # the library is the user's likes, not an automatic play cache.
            # 注意：持久化的是占位地址 track.path（netease:// / qq://），不是已解析的临时直链。
            # ⚠ R49-24：upsert **必须带 cover** —— 旧写法漏传 cover，在线曲目
            # 入库时 cover=NULL，之后红心收藏进曲库的卡片永远无封面。
            # ⚠ R86：在线曲若缺 path（搜索点播的 seed 曾用 path=""），必须补
            #   **稳定占位地址** 再 upsert —— tracks 的唯一键就是 path，
            #   空串会让所有在线曲撞同一行互相覆盖（"曲库点开播的不是这首"）。
            if track.source not in ("local", "", None) and self.db.get_track(track.id) is None:
                path = track.path or self.db.placeholder_path(track.source,
                                                              track.ext_id)
                if path:
                    track.path = path
                track.id = self.db.upsert_track(
                    track.path, track.title, track.artist, track.album,
                    track.genre, track.year, track.duration, track.bpm,
                    source=track.source, ext_id=track.ext_id,
                    cover=getattr(track, "cover", None),
                    api_metadata=getattr(track, 'api_metadata', None),
                )
            # 音频加载/播放放到后台线程，避免远程下载或播放初始化阻塞 UI 主线程
            # （手动切歌 / 首歌开场时尤为明显，否则界面会卡顿约 1.5s）。
            # 直链解析也放在 _start_audio 里做（带重试），既"播放前检测音源"，
            # 又不阻塞 UI / 续播定时器线程。
            # 捕获导航世代令牌：本次播放意图的唯一标识。若用户在我们后台加载/起播
            # 期间切到别的歌，令牌会变化，_start_audio 据此自我放弃，避免过期线程
            # 互相踩踏（快速连点 next/prev/next 时不再卡顿、不再把旧歌加载出来）。
            seq = self._bump_nav()
            # ⚠ R95d-Q0：置「准备播放」意图 —— 覆盖下面后台线程的整个装载窗
            # （解析直链 / 下载 / load 数百 ms~数秒），期间 /api/state 必须报
            # playing=True，否则前端轮询误判"暂停"把水波集体清掉。
            self._set_preparing(seq)
            # ⚠ R49-23：手动切歌「同步停旧曲」—— 必须赶在 _start_audio 后台线程
            # 拿锁加载之前就停掉旧音乐缓冲，否则从点击到新歌真正出声之间旧曲仍在响。
            # （_start_audio 内还会二次兜底停一次，此处保证"零残留窗口"）
            if self.player.state.value != "stopped":
                self._stop_old_audio()
            threading.Thread(
                target=self._start_audio,
                args=(track, broadcast, fade_in, force_intro, wait_intro, seq),
                daemon=True).start()

        def _start_audio(self, track, broadcast, fade_in, force_intro, wait_intro,
                         seq) -> None:
            """后台真正加载并播放曲目；UI 线程已先行返回，不再被 I/O 阻塞。

            播放前在此做「音源检测」：远程占位地址解析为实时直链（带重试，应对
            API 瞬时抖动），本地音源检查文件是否还在。任一环节失败都跳过本曲并
            续播下一首（_skip_unplayable），确保单点音源失效不会卡死整个电台。

            导航世代守卫：进入与落子前比对 seq 与 self._nav_seq；若用户已切到别的歌
            （令牌被推进），本次过期加载立即放弃、不碰任何共享状态，避免旧线程把
            过期歌加载出来 / 乱发 track_changed / 误排转场，从根上消除快速切歌卡顿。
            """
            # 入口守卫：本次播放意图已被更新的导航取代，直接放弃。
            if getattr(self, "_nav_seq", 0) != seq:
                logger.info("跳过过期音频加载（已切换）: %s", track.title)
                self._clear_preparing(seq)   # R95d-Q0（当期意图已被接管 → 守卫内不清）
                return
            # 1) 远程音源占位地址（netease:// / qq:// / spotify://）：
            #    曲库只存元数据，播放前实时解析直链。R99-T3 起走**读穿缓存**
            #    （`_resolve_url_cached`：track_cache(url) 命中即返回，TTL 6h），
            #    未命中才真的解析并写回；失败**不入缓存**，仍按"音源失效"跳过。
            play_path = track.path
            if (self.source_manager
                    and track.source not in ("local", "", None)
                    and not str(play_path).startswith(("http://", "https://", "file://"))):
                real = self._resolve_url_cached(track)
                if real:
                    play_path = real
                else:
                    logger.warning("音源解析失败（音源丢失），跳过播放: %s", track.title)
                    self._emit("dj_speech", f"⚠️ 音源失效，已跳过：《{track.title}》")
                    self._clear_preparing(seq)   # R95d-Q0：放弃本曲 → 不再是"准备播放"
                    self._skip_unplayable(track)
                    return
            # 2) 本地音源：文件可能被移动 / 删除
            if not str(play_path).startswith(("http://", "https://")):
                local = str(play_path)[7:] if str(play_path).startswith("file://") else str(play_path)
                if not Path(local).exists():
                    logger.warning("本地音源文件丢失，跳过播放: %s (%s)", track.title, play_path)
                    self._emit("dj_speech", f"⚠️ 本地文件丢失，已跳过：《{track.title}》")
                    self._clear_preparing(seq)   # R95d-Q0：同上
                    self._skip_unplayable(track)
                    return
            # 每开始一首新歌都重置"口播是否已播"标记。否则手动切歌/「随机一首」
            # （force_intro=True、wait_intro=False 的分支不会重置该标记）会沿用上一首
            # 遗留的 True，导致本首口播在生成完成后被 _speak_intro 的早返回吞掉、永不播放。
            self._intro_spoken = False
            # ★ R119-4v：同一处重置"本段边界有没有人说过话" —— 它是 `_on_track_ended`
            #   决定"下一首要不要播导听口播"的依据（见该处注释）。必须与上面的
            #   `_intro_spoken` **同一时点**重置，否则会跨歌串味。
            self._trans_spoke = False
            # 串行化「加载+起播」，避免并发切歌时 pygame 混音器被重入调用而卡死 UI。
            # 等待 _audio_lock 期间可能已被切换：进入锁后第一时间再校验一次。
            with self._audio_lock:
                if getattr(self, "_nav_seq", 0) != seq:
                    logger.info("加载前已切换，放弃: %s", track.title)
                    self._clear_preparing(seq)   # R95d-Q0
                    return
                # ⚠ R49-23：真正换歌时，加载新音源**前**先同步停旧曲 —— 旧写法要等
                # load() 完成（远程解析/下载数百 ms~数秒）才停掉旧缓冲，期间旧曲
                # 继续响（"前一首还会放几秒"）。这里只要播放器还停在旧曲上就立即
                # stop(unduck=True) + 打断迟到口播；player 已是 STOPPED（无旧声）
                # 则跳过（load/play 幂等，不需停）。
                if self.player.state.value != "stopped":
                    self._stop_old_audio()
                if not self.player.load(play_path):
                    # 加载/下载失败（远程直链过期、网络抖动、文件损坏等）：
                    # 不静默卡死，跳过本曲续播下一首，保证播放链不断。
                    self._emit("dj_speech", f"⚠️ 音源无法加载，已跳过：《{track.title}》")
                    self._clear_preparing(seq)   # R95d-Q0：同上
                    self._skip_unplayable(track)
                    return
            # 加载成功后、落子前最后校验：防止等待 _audio_lock 期间被切换（已被 superseded）。
            if getattr(self, "_nav_seq", 0) != seq:
                logger.info("加载完成前已切换，放弃落子: %s", track.title)
                self._clear_preparing(seq)   # R95d-Q0
                return
            # ⚠ R86：元数据缺 duration（搜索点播 / 部分漫游曲）时，用播放器从文件
            #   探测到的真实时长回填。否则 track.duration=0 → player.play(0) →
            #   state().duration=0 → **前端进度条永远不动**，且 _schedule_transition(0)
            #   直接早退（AI 转场口播也一并丢失）。
            try:
                if not (track.duration or 0) and self.player.duration_ms:
                    track.duration = self.player.duration_ms / 1000.0
                    logger.info("回填曲目时长（文件探测）: 《%s》= %.1fs",
                                track.title, track.duration)
            except Exception:  # noqa
                pass
            # 成功加载 → 重置连续失效计数（避免把历史偶发失败累积成"暂停电台"）
            self._consecutive_skips = 0
            self.current_id = track.id
            self._cur_dur_ms = (track.duration or 0) * 1000.0
            self.db.log_play(track.id, time_slot()[0])
            self._emit("track_changed", track)
            self.lyrics_text = track.lyric
            self._parse_and_emit_lyrics()
            threading.Thread(target=self._fetch_lyrics, args=(track,), daemon=True).start()

            want_intro = force_intro and self.dj_on and self.player.audio_ok
            if want_intro and wait_intro:
                # 首歌开场：等 AI 口播「开始朗读」的此刻才启动音乐，做到先口播后放歌。
                if broadcast is not None:
                    self._intro_pending = True
                    self._intro_spoken = False
                    self._emit("thinking", True)
                    self._after_music_started(track)
                    self._play_seq = seq
                    threading.Thread(
                        target=self._speak_intro, args=(broadcast, seq), daemon=True).start()
                else:
                    # 口播仍在后台生成：先不发声，等生成好后由 _do_gen_broadcast 补播；
                    # 看门狗兜底，防止 LLM/TTS 失败导致永久静音。
                    self._intro_pending = True
                    self._intro_spoken = False
                    self._emit("thinking", True)
                    self._arm_intro_watchdog()
                    self._after_music_started(track)
            elif want_intro:
                # 普通导听（手动跳歌 / 自动续播）：音乐先响，口播叠在前奏。
                # 只要 broadcast 非 None 就调 _speak_intro（文字必显示，朗读按 wav 异步）；
                # 不再用 intro_ready(wav) 门控，避免 TTS 稍慢/失败时文字也不显示。
                # 若 broadcast 仍在后台生成（边播边想迟到），先让音乐响、把口播挂起为
                # 「迟到等待」——_do_gen_broadcast 就绪后补播（绝不丢弃，也不重启动音乐）。
                with self._audio_lock:
                    self.player.play(track.duration)
                    if fade_in:
                        self.player.fade_in(RadioConfig.FADE_IN_MS)
                # R95d-Q0：音乐已真正起播 → 意图兑现，交回引擎状态权威
                self._clear_preparing(seq)
                self._emit("playing", True)
                self._music_started = True
                self._play_seq = seq
                if broadcast is not None:
                    self._intro_pending = False
                    self._intro_spoken = False
                    self._emit("thinking", True)
                    threading.Thread(
                        target=self._speak_intro, args=(broadcast, seq), daemon=True).start()
                else:
                    self._intro_pending = True
                    self._intro_spoken = False
                    self._emit("thinking", True)
                    self._arm_intro_watchdog()   # 兜底：迟到口播生成失败也补默认开场白
                self._schedule_transition(track.duration, seq)
                self._after_music_started(track)
            else:
                # 无导听：直接播放
                with self._audio_lock:
                    self.player.play(track.duration)
                    if fade_in:
                        self.player.fade_in(RadioConfig.FADE_IN_MS)
                # R95d-Q0：音乐已真正起播 → 意图兑现，交回引擎状态权威
                self._clear_preparing(seq)
                self._emit("playing", True)
                self._music_started = True
                self._play_seq = seq
                self._intro_pending = False
                self._schedule_transition(track.duration, seq)
                self._after_music_started(track)

        def _after_music_started(self, track) -> None:
            """音乐真正开始（或即将开始）后，预生成下一首口播 / 转场决策。"""
            self._record_recent(track)
            self._prep_next_broadcast()

        def _record_recent(self, track) -> None:
            """把刚开播的歌记入「最近播放」轻量缓冲（仅存「《歌名》— 歌手」字符串）。

            用于口播连贯性上下文；受 _RECENT_CAP 与 config context_window 双重约束，
            不累积无限历史（契合「不保留过多上下文、追求极致速度」）。
            """
            # 防御：即便 __init__ 未运行（如测试桩用 __new__ 构造）也绝不崩。
            buf = getattr(self, "_recent_played", None)
            if buf is None:
                buf = []
                self._recent_played = buf
            cap = getattr(self, "_RECENT_CAP", 30)
            if not self._is_track(track):
                return
            label = f"《{track.title}》" + (f"— {track.artist}" if track.artist else "")
            if buf and buf[-1] == label:
                return  # 同一首重复起播不重复记
            buf.append(label)
            if len(buf) > cap:
                self._recent_played = buf[-cap:]

        def _recent_context_text(self) -> str:
            """取最近 context_window 首已播歌曲的轻量文本（用于口播连贯）；

            context_window<=0 时返回空串（纯实时、不保留历史、极致速度）。
            """
            try:
                n = int(user_config.ai.get("context_window", 3) or 0)
            except (TypeError, ValueError):
                n = 3
            if n <= 0:
                return ""
            buf = getattr(self, "_recent_played", None)
            if not buf:
                return ""
            window = buf[-n:]
            return " / ".join(window)

        def _speak_intro(self, broadcast: dict, seq=None) -> None:
            if getattr(self, "_intro_spoken", False):
                return
            self._intro_spoken = True
            # 导航世代守卫：本次口播属于已被切换掉的旧歌时，不朗读、也不为它启动音乐，
            # 避免过期导听抢占当前歌的播放 / 转场。
            seq = seq if seq is not None else getattr(self, "_play_seq", 0)
            if getattr(self, "_nav_seq", 0) != seq:
                logger.info("丢弃过期导听口播（已切换）")
                return
            # ★★ 2026-11-17：**归属校验** —— 再核一次这段口播是不是当前这首的。
            #   只靠导航序号不够：实测出现过"上一首的口播念在下一首上"。
            try:
                _bid = broadcast.get("track_id")
                _cur = getattr(self.current, "id", None)
                if _bid is not None and _cur is not None and _bid != _cur:
                    # ★★ 2026-11-18（用户「文案有音频无」）：id 不等**不能直接丢弃**！
                    #   推荐曲目是负数临时 id(-33861231)，入库播放后变正数 id(1086)，
                    #   两者没有数值关系 ⇒ 明明是同一首歌却被判错配、广播被丢 ⇒ 音频丢失。
                    #   用 title 兜底核对：标题相同就是同一首，放行。
                    _btitle = (broadcast.get("title") or "").strip()
                    _cur_title = (getattr(self.current, "title", "") or "").strip()
                    if _btitle and _cur_title and _btitle == _cur_title:
                        logger.info("[口播] id 不同但标题一致（负数临时id已入库），放行：%s", _btitle)
                    else:
                        logger.info("[口播] 丢弃错配口播：广播属于 id=%s《%s》，当前是 id=%s《%s》",
                                    _bid, _btitle, _cur, _cur_title)
                        return
            except Exception:  # noqa
                pass
            # 若音乐尚未启动（延迟开场/首歌曲），此刻——口播开始朗读——才启动音乐（压低音量），
            # 实现用户要的「先说口播、再放歌」。若音乐已在响（非首歌曲「边播边想」迟到口播），
            # 绝不重启动音乐，只压低音量叠前奏朗读。
            if getattr(self, "_intro_pending", False):
                self._intro_pending = False
                self._cancel_intro_watchdog()
                if not self._music_started:
                    logger.info(
                        "[radio] _speak_intro 启动音乐 《%s》（口播就绪，先口播后放歌）",
                        (self.current.title if self.current else "?"))
                    self.player.duck(PlayerConfig.DUCK_VOLUME)
                    self.player.play(self._cur_dur_ms / 1000.0 if self._cur_dur_ms else None)
                    self._emit("playing", True)
                    self._schedule_transition(
                        self._cur_dur_ms / 1000.0 if self._cur_dur_ms else 0.0, seq)
                    self._music_started = True
                    self._play_seq = seq
                else:
                    # 音乐已响：只压低音量，等下方 play_tts 叠播口播（不重启动、不重排转场）。
                    self.player.duck(PlayerConfig.DUCK_VOLUME)
            # 异步朗读（不阻塞）：若 wav 已就绪直接播；否则用 synthesize 现合成后播。
            # 口播走非阻塞 play_tts，叠在下一首前奏上朗读（设计如此：intro 重叠模式）。
            # 注意：转场在上一首结尾处已 wait_tts 播完才切歌，故口播触发时 TTS 通道
            # 已空闲，二者在时间上天然错开，不会互相打断（无需额外串行化）。
            wav = broadcast.get("wav")
            # ★★★ 2026-11-18（用户「文案有音频无」彻底根治）：
            #   broadcast 可能是**旧的 entry 对象**（负数临时 id、wav 未回填），
            #   而音频早已合成好、回填进了 _bc_map 的纯 title key（`|{title}`）。
            #   切歌时先按 title 从 map 补 wav，再决定兜底——避免"音频明明有却播静态句"。
            if not wav:
                try:
                    _m = getattr(self, "_bc_map", None)
                    _t = (broadcast.get("title") or getattr(self.current, "title", "") or "").strip()
                    if _m and _t:
                        _hit = _m.get(f"|{_t}")
                        if isinstance(_hit, dict) and _hit.get("wav"):
                            wav = _hit["wav"]
                            logger.info("[口播] 从映射按标题补回已合成音频：%s", _t)
                except Exception as e:  # noqa
                    logger.debug("按标题补 wav 失败：%s", e)
            # ★★★ 2026-11-17 task-1（用户规则：兜底必须声文一致）：
            #   音频没就绪（批处理还没回填/合成失败）时，**整段改用成对静态**
            #   —— 下面显示的句子就是播放的音频，不再出现「真文案 + 通用音」的错配。
            #   代价：这一首的 AI 文案不显示（用户明确接受，一致性优先）。
            #   必须放在 emit dj_speech **之前**，否则界面仍是真文案、声音已是通用句。
            if self.dj_on and self.player.audio_ok and not wav:
                try:
                    _st = getattr(self, "_generic_static", None)
                    if _st is not None:
                        _gt, _gw = _st()
                        if _gt and _gw:
                            broadcast = dict(broadcast)
                            broadcast["text"] = _gt
                            wav = _gw
                            logger.info("[口播] wav 未就绪 → 改用成对静态（文字=音频同一句）")
                except Exception as e:  # noqa
                    logger.info("[口播] 成对静态兜底读取失败：%s", e)
            # dj_on 关闭：仅门控 TTS 输出与口播文字显示；AI 文案此前已生成（思考不断）。
            # 关闭时直接放歌（音乐已在上方随 _intro_pending 启动），不朗读、不显字。
            if self.dj_on:
                # ★ R119-4v：记一笔"**正确的**导听口播真的播了"（/api/status → dj）。
                self._n_intro_ai = getattr(self, "_n_intro_ai", 0) + 1
                self._emit("dj_speech", broadcast.get("text", ""))
            self._emit("thinking", False)
            if self.dj_on and self.player.audio_ok:
                self.player.duck(PlayerConfig.DUCK_VOLUME)
                self._play_broadcast_audio(broadcast, wav)
                # ⚠ R49-26：unduck 必须等口播**真正播完**再恢复音量 —— 旧写法在
                # play_tts（非阻塞）之后立即 unduck，口播还响着音乐就被拉回 0.85，
                # 用户感知为"压了一下立刻弹回、口播听不清"。
                # _play_broadcast_audio 可能内部现合成 wav，故无条件 wait_tts
                # （TTS 通道此刻空闲则立即返回，不阻塞）；播完再平滑恢复目标音量。
                try:
                    self.player.wait_tts()
                except Exception:  # noqa
                    pass
            self.player.unduck(PlayerConfig.RECOVER_FADE_MS)

        def _park_tts_audio(self, kind: str, track, wav) -> str:
            """按用户规则「下一条同类播完才删上一条」保管口播音频。

            规则（2026-11-17 用户指定）：
              · 下一首的 intro 播完 → 删上一首的 intro；outro 同理，各管各的；
              · 音频文件名显式带 kind + 曲目 id，便于核对、从根上防误删；
              · `tts_static/` 下的随包资产**永不删除**。
            返回实际保存路径（可能因拷贝失败退回原路径）。
            """
            if not wav:
                return wav
            try:
                import shutil
                from pathlib import Path as _P
                from config.settings import TTS_TMP_DIR, PROJECT_ROOT
                static_dir = str((PROJECT_ROOT / "data_runtime/tts_static").resolve())
                reg = getattr(self, "_tts_parked", None)
                if reg is None:
                    reg = self._tts_parked = {}
                # 先把新文件拷成「带 id」的名字（资产不拷、直接登记）
                src = _P(wav)
                if not str(src.resolve()).startswith(static_dir):
                    tid = getattr(track, "id", "") or "na"
                    dst = TTS_TMP_DIR / f"bc_{kind}_{tid}_{int(time.time())}.wav"
                    try:
                        shutil.copyfile(src, dst)
                        new_path = str(dst)
                    except Exception:  # noqa
                        new_path = wav
                else:
                    new_path = wav
                # 再删上一条同类（它的播放早已结束）
                old = reg.get(kind)
                reg[kind] = new_path
                if old and old != new_path:
                    try:
                        op = _P(old)
                        if (op.is_file() and op.name.startswith(f"bc_{kind}_")
                                and not str(op.resolve()).startswith(static_dir)):
                            op.unlink()
                            logger.info("[口播] 删除上一首 %s 音频：%s", kind, op.name)
                    except Exception:  # noqa
                        pass
                return new_path
            except Exception as e:  # noqa
                logger.info("[口播] 保管音频失败（不影响播放）：%s", e)
                return wav

        def _discard_tts_later(self, wav, text: str | None = None) -> None:
            """口播音频延迟释放（删除临时文件），实现「即用即扔」不堆盘。

            用户要求：口播保留到「这首歌结束」再丢，不要播完即删。因此持有时间取
            「TTS 音频时长 + 余量」与「当前歌曲时长 + 5s」的较大值——既保证播完
            不中断，又确保在本首歌结束前后才清理（被跳过的歌曲最多残留至 30 分钟
            周期清理线程兜底删除）。配合 synthesize_unique 的唯一文件名，杜绝跨
            口播/转场的同名文件被彼此提前释放导致「口播朗读失败」。
            """
            if not wav:
                return
            # ★★ 2026-11-17 关键修复：**绝不允许删除随包发布的静态音频**。
            #   开场白/通用过渡句放在 data_runtime/tts_static/ 下，是**资产**不是临时文件。
            #   之前播放静态音频也走本函数，"即用即扔" 把它们逐个删掉 ⇒ 后续取不到音频，
            #   表现为「文案退回『交给耳朵』+ 没有声音」（实测 afternoon/evening/night/
            #   generic 四个文件全部被删）。
            try:
                from pathlib import Path as _P
                from config.settings import PROJECT_ROOT
                if str(_P(wav).resolve()).startswith(
                        str((PROJECT_ROOT / "data_runtime/tts_static").resolve())):
                    logger.info("[口播] 静态开场白不删除（资产）：%s", _P(wav).name)
                    return
            except Exception:  # noqa
                pass

            def _cleanup() -> None:
                dur_ms = self._audio_duration_ms(wav)
                if not dur_ms and text:
                    dur_ms = self._estimate_tts_ms(text)
                tts_s = (dur_ms or 15000) / 1000.0
                song_s = (getattr(self, "_cur_dur_ms", 0) or 0) / 1000.0
                # 至少播完 + 2s 余量；有歌曲时长时按「歌曲结束 + 5s」保留。
                wait = max(tts_s + 2.0, song_s + 5.0)
                wait = min(600.0, wait)
                try:
                    time.sleep(wait)
                except Exception:  # noqa
                    pass
                try:
                    self.tts.release(wav)
                except Exception:  # noqa
                    pass

            t = threading.Thread(target=_cleanup, daemon=True)
            t.name = "tts-discard"
            t.start()

        def _play_broadcast_audio(self, broadcast: dict, wav) -> None:
            """播放口播语音：wav 已就绪则直接播；否则用整段 synthesize 现合成后播。
            全部在后台线程异步进行（play_tts 本身不阻塞播放通道），不调用 wait_tts。
            仅当 dj_on 为真才真正出声（dj_on 关闭 = 只关 TTS 输出）。播放后登记延迟
            释放临时音频文件（即用即扔）。

            健壮性兜底（防「文案在但不出声」）：wav 路径存在但文件已被删除（旧构建的
            内容哈希同名误删、seek 干扰、或任何边缘情况）时，用 broadcast 文案重新合成
            一份独占 wav 再播——保证「有文案就一定出声」，杜绝口播被静默吞掉。
            """
            if not self.dj_on:
                return
            text = broadcast.get("text", "") or ""
            # ★ 2026-11-17 诊断（必达）：播放侧到底有没有拿到音频、各开关什么状态。
            #   之前"有文案没声音"时日志里一片空白，无法判断是「没做出来」还是
            #   「做出来没播」。这一行只打日志，不改变任何行为。
            logger.info("[口播] 播放检查 text=%d字 wav=%s audio_ok=%s dj_on=%s",
                        len(text), bool(wav),
                        bool(getattr(self.player, "audio_ok", False)),
                        bool(getattr(self, "dj_on", False)))
            try:
                # wav 缺失（被提前释放/同哈希误删/seek 干扰等）→ 用文案重新合成兜底。
                if wav and not Path(wav).exists():
                    wav = None
                if not wav:
                    if text:
                        # 同样走统一入口（批处理窗口），避免直连 TTS 与大模型同驻
                        wav = (self._synth_text(text, timeout=6.0)
                               if hasattr(self, "_synth_text")
                               else self.tts.synthesize_unique(text))
                    # ★★ 2026-11-17 task-1（用户规则：兜底必须声文一致）：
                    #   现合成也拿不到音频时，改成成对静态（文字=音频同一句），
                    #   并同步 emit，避免界面还停在真实文案上。
                    if not wav and hasattr(self, "_generic_static"):
                        try:
                            _gt, wav = self._generic_static()
                            if wav:
                                if _gt:
                                    text = _gt
                                    self._emit("dj_speech", text)
                                logger.info("[口播] 现合成失败 → 成对静态顶声（文字同句）")
                        except Exception:  # noqa
                            pass
                if wav and self.player.audio_ok:
                    self.player.play_tts(wav)  # 非阻塞：仅触发 TTS 通道播放，立即返回
                    self._discard_tts_later(wav, text)
            except Exception as e:  # noqa
                logger.warning("口播朗读失败（不影响文字显示）：%s", e)

        # ---------- 开场导听看门狗（防止 LLM/TTS 失败导致音乐永不响） ----------

        def _arm_intro_watchdog(self) -> None:
            self._cancel_intro_watchdog()
            # ★★ 2026-11-18（用户拍板「超长恢复返工重写，watchdog 45s」）：
            #   超长要返工重写（~20s）+ 首条生成（~20s）≈ 40s，故非首曲给 45s 覆盖；
            #   首曲（音乐未响）保持默认 5s 快速出声（固定开场白，零 LLM）。
            _started = bool(getattr(self, "_music_started", False))
            _wait_s = (45.0 if _started
                       else RadioConfig.INTRO_MAX_WAIT_MS / 1000.0)
            self._intro_timer = threading.Timer(_wait_s, self._start_intro_fallback)
            self._intro_timer.daemon = True
            self._intro_timer.start()

        def _cancel_intro_watchdog(self) -> None:
            t = getattr(self, "_intro_timer", None)
            if t is not None:
                try:
                    t.cancel()
                except Exception:  # noqa
                    pass
                self._intro_timer = None

        def _start_intro_fallback(self) -> None:
            if not getattr(self, "_intro_pending", False):
                return
            self._intro_pending = False
            self._cancel_intro_watchdog()
            # ★ R119-4s：**进入前**记录"音乐是否已在响" —— 用来区分"首曲开场"与"迟到口播"。
            #   两者不能用同一句兜底：开场白刻意带地点/天气/温度（向听众问好），
            #   而迟到口播发生在收听中途，报环境既突兀又违反口播契约。
            _was_started = bool(getattr(self, "_music_started", False))
            # 兜底分支：首歌曲音乐未响 → 立即开播；非首歌曲（迟到口播）音乐已在响 → 不重启。
            if not self._music_started:
                if self.dj_on and self.current is not None and self.current_broadcast is None:
                    logger.info("开场导听未就绪 → 音乐立即开播，并用「默认开场白」兜底口播。")
                elif not self.dj_on:
                    logger.info("开场导听超时/未就绪，dj_on 关闭 → 纯音乐开播（无口播）。")
                else:
                    logger.info("开场导听超时/未就绪，但 AI 口播已就绪 → 正常开播。")
                self.player.play(self._cur_dur_ms / 1000.0 if self._cur_dur_ms else None)
                logger.info(
                    "[radio] _start_intro_fallback 启动音乐 《%s》",
                    (self.current.title if self.current else "?"))
                # R95d-Q0：兜底开场（口播超时/未就绪）同样兑现意图
                self._clear_preparing(getattr(self, "_play_seq", None))
                self._emit("playing", True)
                self._schedule_transition(self._cur_dur_ms / 1000.0 if self._cur_dur_ms else 0.0)
                self._music_started = True
            else:
                # 音乐已在响：绝不重启动音乐；仅按下方条件补朗读兜底口播。
                if self.dj_on and self.current is not None and self.current_broadcast is None:
                    logger.info("迟到口播未就绪 → 音乐已响，补「默认开场白」兜底口播（不重启）。")
            self._emit("thinking", False)
            # 第一首口播兜底：AI 文案未就绪时，播「默认开场白」（地点+天气+温度+歌名）。
            # 仅 dj_on 开启才出声/显字（关闭 = 纯音乐）。AI 已"思考"，此处只是朗读兜底。
            if self.dj_on and self.current is not None and self.current_broadcast is None:
                try:
                    # ★ R119-4s：**迟到**（音乐已在响）⇒ 中性句；**首曲**⇒ 开场白。
                    text = (self._late_intro_text(self.current) if _was_started
                            else self._opening_text(self.current))
                    wav = None
                    # ★★ 2026-11-18（用户报「第一首播成'这一首先交给音乐'」）：
                    #   首曲（音乐未响）**直接用按时段的开场白音频**，且锁定不被后续的
                    #   _bc_map 真实文案 / generic 静态覆盖。用户明确："完全放弃第一首导听"
                    #   —— 第一首就该播固定开场白，不是 AI 文案，更不是通用过渡句。
                    _first_opening_locked = False
                    if not _was_started and hasattr(self, "_opening_static"):
                        try:
                            _ot, _ow = self._opening_static()
                            if _ow and Path(_ow).exists():
                                text, wav = _ot or text, _ow
                                _first_opening_locked = True
                                self._opening_done = True
                                logger.info("[口播] 首曲锁定时段开场白：%s", (_ot or "")[:30])
                                # ★ 2026-11-18：品味分析 hook 已移到
                                #   schedule.py::_do_gen_broadcast 的 opening 分支
                                #   （首曲实际走那条路径，这里不重复触发）。
                        except Exception as e:  # noqa
                            logger.warning("[口播] 首曲开场白读取失败：%s", e)
                    # ★ 2026-11-17（接线修复）：先查**本次会话已生成**的口播映射。
                    #   队列重建会让 entry 换对象，但生成结果在这张表里还在。
                    #   只查本进程内存（不落盘、不复用旧音频）；命中即用，用完删。
                    _map_hit = False
                    try:
                        m = getattr(self, "_bc_map", None)
                        if m and hasattr(self, "_bc_key") and self.current is not None:
                            key = self._bc_key(self.current)

                            def _norm(s):
                                """归一化：去空白、去标点、小写 —— 标题匹配只认内容。"""
                                import re as _re
                                return _re.sub(r"[\s\W_]+", "", (s or "").lower())

                            nt = _norm(getattr(self.current, "title", ""))
                            hit = None
                            hit_key = None
                            # 1) 精确 key
                            if key in m:
                                hit_key = key
                                hit = m.pop(key)
                            # 2) 归一化标题（id 变化、标点/大小写差异都能对上）
                            if hit is None and nt:
                                for k in list(m):
                                    kt = k.split("|", 1)[-1]
                                    if _norm(kt) == nt or _norm(k) == nt:
                                        hit_key = k
                                        hit = m.pop(k)
                                        break
                            if hit is None:
                                logger.info("[口播] 映射未命中 key=%s title=%s | 表内 keys=%s",
                                            key, getattr(self.current, "title", "?"),
                                            list(m)[:8])
                            elif hit.get("text"):
                                text = hit["text"]
                                wav = hit.get("wav")
                                _map_hit = True
                                logger.info("[口播] 映射命中(%s)，用已生成的文案：《%s》%d 字",
                                            hit_key, getattr(self.current, "title", "?"),
                                            len(text))
                    except Exception as e:  # noqa
                        logger.info("[口播] 查映射失败：%s", e)
                    # ★★ 2026-11-17（补漏）：**这条兜底路径也必须走离线预生成音频**。
                    #   实测：首曲 5 秒看门狗先触发 → 进到这里 → 仍用旧模板文案现合成
                    #   ⇒ 用户听到的还是「城市+天气+气温XX℃」的老开场白，还把 TTS
                    #   拉起来和大模型抢显存（GPU 7805/8188）。
                    #   规则：首曲用按时段选的开场白，迟到口播用通用过渡句；
                    #   两者都不经过任何 TTS 引擎，引擎全挂也能出声。
                    # ★ 2026-11-17（按用户设计简化）：**不复用**，现用现生成；
                    #   只有「本进程第一首」用时段开场白，其余一律通用过渡句。
                    #   之前用 `_music_started` 判断是错的：看门狗会先把音乐起来、
                    #   再把标记置 True，于是首曲也被当成「迟到」，放了通用句。
                    _is_first_ever = not getattr(self, "_opening_done", False)
                    # ★★ 2026-11-17：**已经命中真实文案时不许被兜底覆盖**。
                    #   实测：映射命中拿到 125 字文案后，这段静态兜底又把它盖成
                    #   「这一首先交给音乐…」，等于白命中。
                    _has_real_text = bool(text) and _map_hit
                    try:
                        if not _has_real_text and not _first_opening_locked:
                            if _is_first_ever and hasattr(self, "_opening_static"):
                                gtext, gwav = self._opening_static()
                            elif hasattr(self, "_generic_static"):
                                gtext, gwav = self._generic_static()
                            else:
                                gtext, gwav = "", None
                            if gwav:
                                text, wav = gtext, gwav
                                self._opening_done = True
                                logger.info("[口播] 兜底音频：%s | %s",
                                            "首曲时段开场白" if _is_first_ever else "通用过渡",
                                            gtext[:30])
                        else:
                            logger.info("[口播] 使用已生成文案，跳过兜底覆盖")
                    except Exception as e:  # noqa
                        logger.warning("读取离线开场白失败，退回现合成: %s", e)
                    # ★★ 2026-11-17：**有文案但没音频**时必须补上声音。
                    #   实测：命中拿到 105 字文案，但该曲音频从未合成（回填 0 次），
                    #   而"有文案就跳过兜底"的保护把声音也去掉了 ⇒ 只有文字、没声。
                    #   策略：现合成一次（短超时）；仍失败就播通用音频，文字保持不变。
                    if _map_hit and not wav and text and not _first_opening_locked:
                        try:
                            _fn = getattr(self, "_synth_text", None)
                            if _fn is not None:
                                wav = _fn(text, timeout=12.0)
                                if wav:
                                    logger.info("[口播] 命中无音频 → 现合成成功")
                            if not wav and hasattr(self, "_generic_static"):
                                _gt, wav = self._generic_static()
                                if wav:
                                    # ★ task-1：兜底必须声文一致 —— 文字同步换成同一句
                                    text = _gt or text
                                    logger.info("[口播] 现合成失败 → 成对静态顶声（文字同句）")
                        except Exception as e:  # noqa
                            logger.info("[口播] 补音频失败：%s", e)
                    if text:
                        # ★ R119-4v：记一笔"退化兜底真的播了"（与上面的 AI 计数配对，
                        #    `/api/status → dj.intro_fallback_ratio` 即"这一轮有多少条
                        #    口播是退化的"）。兜底**保留** —— 用户明确："作为一个退化的实例"。
                        self._n_intro_fallback = getattr(self, "_n_intro_fallback", 0) + 1
                        self._emit("dj_speech", text)
                    # ★★★ 2026-11-18 P0-D（用户报「口播在歌曲中间播放、时间没对齐音乐」）：
                    #   `_was_started=True` 表示音乐**已经在响**（看门狗 5s 超时起播，
                    #   或迟到口播回填）。此时绝不能 play_tts + duck —— 那等于把导听
                    #   硬插进正在播的歌里，听感突兀且违反「导听只在歌前」契约。
                    #   改为：迟到只显示文字（上面已 _emit），音频丢弃、不 duck、不 wait。
                    #   首曲开场（音乐未响）才朗读兜底音频。
                    if _was_started:
                        logger.info("[口播] 音乐已在响 → 迟到口播只显示文字、不插播音频"
                                    "（%d 字）", len(text or ""))
                        # 不 play_tts / 不 duck / 不 wait_tts / 不 park
                        return
                    if self.player.audio_ok:
                        # 预生成的兜底音频已就位（wav 非空）→ 直接用，不碰 TTS 引擎；
                        # 否则才走统一合成入口（批处理窗口/直连）。
                        if not wav:
                            wav = (self._synth_text(text, timeout=6.0)
                                   if text and hasattr(self, "_synth_text") else None)
                        if not wav and hasattr(self, "_generic_static"):
                            # ★ task-1：仍拿不到音频 → 成对静态顶声，并同步改字（声文一致）
                            _gt2, wav = self._generic_static()
                            if wav:
                                text = _gt2 or text
                                if text:
                                    self._emit("dj_speech", text)
                                logger.info("[口播] 现合成失败 → 成对静态顶声（文字同句）")
                        if wav:
                            self.player.duck(PlayerConfig.DUCK_VOLUME)
                            self.player.play_tts(wav)
                            # ★ 2026-11-17（用户规则）：不按时间删，改由「下一条同类」
                            #   替换时才删上一条；文件名带 kind+曲目 id 防误删。
                            self._park_tts_audio("intro", getattr(self, "current", None), wav)
                            # R49-26：等口播播完再恢复音量（旧写法立即 unduck，
                            # 口播还响着音乐就被拉回目标音量）。
                            try:
                                self.player.wait_tts()
                            except Exception:  # noqa
                                pass
                            self.player.unduck(PlayerConfig.RECOVER_FADE_MS)
                            # ★ 2026-11-17：播完即丢 —— 删掉落盘的口播记录（wav 由
                            #   _discard_tts_later 删除），避免长期占盘。
                            try:
                                if hasattr(self, "_bc_drop"):
                                    self._bc_drop(getattr(self, "current", None))
                            except Exception:  # noqa
                                pass
                except Exception as e:  # noqa
                    logger.warning("默认开场白兜底朗读失败（不影响播放）：%s", e)

        # ---------- 歌词 ----------

        def _parse_and_emit_lyrics(self) -> None:
            self.lyric_lines = parse_lrc(self.lyrics_text)
            self._lyric_idx = -1
            self._emit("lyrics", self.lyrics_text)

        def _fetch_lyrics(self, track) -> None:
            if track.lyric:
                return
            if self.source_manager and track.source not in ("local", "", None):
                src = str(track.source)
                ext = getattr(track, "ext_id", None)
                db = getattr(self, "db", None)
                try:
                    # ⚠ 音源 lyric() 返回形态不统一（str / dict / list[dict]），
                    #   必须归一后再交给 parse_lrc —— 否则 dict 会抛
                    #   "'dict' object has no attribute 'splitlines'"（播放时取词路径）。
                    #   注意：bridge_host.getLyrics 是「前端主动取词」，本处是「播放时取词」，
                    #   两条路径都必须归一，只修一处会漏。
                    # R99-T3：歌词**永久**缓存（kind='lyric'，TTL=0）。歌词不像直链会过期，
                    #   同一首歌第二次播放/预取不该再走一次网络。
                    text = None
                    _bst = getattr(self, "_bump_cache_stat", None)   # 混入缺失时静默跳过
                    if db is not None and ext:
                        try:
                            text = db.get_cache("lyric", src, ext)
                        except Exception:  # noqa
                            text = None
                        if text and _bst:
                            _bst("lyric", "hit")
                    if not text:
                        if db is not None and ext and _bst:
                            _bst("lyric", "miss")
                        text = coerce_lrc_text(
                            self.source_manager.lyric(track.source, track.ext_id))
                        if text and db is not None and ext:
                            try:
                                db.set_cache("lyric", src, ext, text)
                            except Exception:  # noqa
                                pass
                    if text:
                        self.lyrics_text = text
                        self._parse_and_emit_lyrics()
                except Exception as e:  # noqa
                    logger.warning("取歌词失败: %s", e)

        # ---------- 口播（按列表顺序预生成并存储） ----------

        def toggle_play(self) -> None:
            was_playing = self.player.state.value == "playing"
            self.player.toggle()
            is_playing = self.player.state.value == "playing"
            # R95d-Q0：用户主动暂停 → 播放意图一并撤销（否则"准备中"会顶着报 playing）
            if not is_playing:
                self._clear_preparing()
            self._emit("playing", is_playing)
            if was_playing and not is_playing:
                # 暂停：取消转场定时器并记录剩余时间，防止暂停期间自动切歌。
                self._pause_transition()
            elif not was_playing and is_playing:
                # 恢复：按剩余播放时间重新排程转场。
                self._resume_transition()

        def _pause_transition(self) -> None:
            """暂停时冻结转场定时器（wall-clock 不再推进），避免暂停期间自动切歌。"""
            if self._trans_timer is not None:
                self._cancel_transition()

        def _resume_transition(self) -> None:
            """恢复播放时按当前真实播放位置重新排程转场。"""
            self._reschedule_transition_from_position()

        def seek(self, ms: int) -> None:
            self.player.seek(ms)
            # 拖动进度条后按新的剩余时间重新排程，避免旧定时器基于旧位置触发。
            if self._music_started and self.player.state.value == "playing":
                self._reschedule_transition_from_position()

        def set_volume(self, v: float) -> None:
            self.player.set_volume(v)

        def _on_progress(self, ms: int) -> None:
            # 过期进度守卫：本次进度回调属于已被切换掉的旧歌时，丢弃，避免进度条跳回旧歌。
            if getattr(self, "_nav_seq", 0) != getattr(self, "_play_seq", 0):
                return
            self._emit("progress", ms)
            if self.lyric_lines:
                idx = line_index_at(self.lyric_lines, ms)
                if idx != self._lyric_idx:
                    self._lyric_idx = idx
                    text = self.lyric_lines[idx][1] if idx >= 0 else ""
                    self._emit("lyric_line", idx, text)

        # ---------- 自动续播兜底 ----------

        def _on_track_ended(self) -> None:
            if self._transitioning:
                return
            # 过期结束事件守卫：本次「播放结束」属于已被切换掉的旧歌，忽略，
            # 避免旧歌自然结束时误触发 _advance 把当前歌又切走一首（快速切歌后常见）。
            if getattr(self, "_nav_seq", 0) != getattr(self, "_play_seq", 0):
                return
            # ★★★ R114（2026-09-13）：**短音频自愈**。
            #   在线曲的直链可能是降级/试听片段（实测同一首歌 20.0 s vs 元数据 300 s），
            #   而坏链会被 `track_cache` 的 6h TTL 钉死 ⇒ 用户实测"同一首反复播 9 次
            #   全是 20 s、次次自动切歌"。这里在"播完"时回头核对一次：
            #   若结束位置远小于声明时长 ⇒ 失效该曲的直链缓存，**下次播放重新取链**。
            self._invalidate_short_audio()
            if self.roam_on or self.upcoming:
                # ★★★ R119-4v：**把「转场真出过声」当作 `intro_already_played`**，
                #   绝不拿"DJ 开关开着"当替身判据。
                #   旧写法 `self.dj_on` 的真实语义是"刚才已经有人说过话了" —— 而这件事
                #   从没被验证过。转场**没出声**的三种常见情形：
                #     ① `transition_type == "none"`（脚本 0 字，模型判断"不打搅"）；
                #     ② 脚本为空 / 合成失败 / `audio_ok` 假；
                #     ③ **短音频**：在线曲直链被降级成 20s 试听而元数据写 300s（R114 已记录），
                #        转场定时器按**声明时长**排在 295s，音频先结束 ⇒ 走的就是这条路径。
                #   三者都会让下一首的导听口播**被错误抑制** ⇒ 听众连着两段没有 DJ，
                #   而那份**正确的口播其实早就生成好了**。
                self._advance(intro_already_played=bool(
                    getattr(self, "_trans_spoke", False)))

        def _invalidate_short_audio(self) -> None:
            """R114：识别「短音频」并**失效其直链缓存**（自愈）。失败绝不影响播放。

            两条独立判据（**都要看**，只靠一条会漏）：
              · 结束位置 ≪ 声明时长（用播放位置，不依赖引擎内部状态）；
              · 引擎的 `short_audio`（用**文件真实时长**，更权威但需要能探测到时长）。

            ⚠ 失效动作**委托** `self._invalidate_url_cache()` —— 那是唯一入口
            （R114 的另一条触发"加载失败"在 `_skip_unplayable` 里调同一个它）。
            本项目已有"同一事实两处各存一份 ⇒ 必然漂移"的前科，这里不再分叉。
            """
            try:
                dur = float(getattr(self, "_cur_dur_ms", 0) or 0)
                if dur <= 3000:
                    return
                pos = float(self.player.get_position() or 0)
                by_pos = pos < dur * SHORT_AUDIO_RATIO
                by_eng = bool(getattr(self.player, "short_audio", False))
                if not (by_pos or by_eng):
                    return
                self._invalidate_url_cache(
                    self.current,
                    why=f"短音频（结束位置 {pos / 1000.0:.1f}s ≪ 声明 {dur / 1000.0:.1f}s，"
                        f"引擎判定={by_eng}）")
            except Exception as e:  # noqa 自愈失败绝不影响播放
                logger.debug("[R114] 短音频自愈失败（忽略）: %s", e)

