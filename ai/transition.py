"""AI 电台编排：本模块承载按职责拆出的 mixin 引擎之一。

见 ai/radio.py 的总述：AIRadioController 通过多重继承组合
PlaybackOrchestrator / ScheduleEngine / TransitionHandler，本文件只负责"音频进退与
ducking / 口播播放 / 进度回调"这一职责。所有方法直接操作 self（即控制器实例），共享
状态与跨职责互调经继承链透明解析，行为与原单体实现完全一致。
"""
from __future__ import annotations

import os
import re
import logging
import random
import threading
import time
from pathlib import Path

from utils.weather import get_weather
from utils.location import get_city, time_slot
from config.settings import PlayerConfig, RadioConfig, user_config
from core.lyrics import parse_lrc, line_index_at
from core.tts_segment import strip_tags

# ★★★ 2026-11-18（用户拍板「砍掉转场全部功能」）：播放侧总开关，
#   与 schedule.py 同源（MUSICAI_TRANSITION=1 才恢复）。默认关闭 ⇒ 不排程/不播转场。
try:
    TRANSITION_ENABLED = (os.environ.get("MUSICAI_TRANSITION", "0") == "1")
except Exception:  # noqa
    TRANSITION_ENABLED = False

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
# ★ R119-4s：剥掉模型手写 JSON 时残留的尾部 `"}` / `”}`（只在末尾有 `}` 时才动手）。
_TRAIL_JSON = re.compile(r'["”]?\}\s*$')

# 天气口播机械降频：相邻两次「提供天气」之间至少间隔多少首歌。
WEATHER_MIN_GAP = 5

class TransitionHandler:
        def _gen_transition(self, entry: dict, from_track, to_track,
                            from_broadcast: str = "") -> None:
            """背景为某待播项预生成"与前一首的转场决策"（intro/outro/none + 口播）。

            `from_broadcast`：上一首的**导听口播文本**（R119-4x）。串行链里它是上一步
            的产物，让转场能呼应刚刚说过的话头，而不是两眼一抹黑地硬接。
            """
            if entry.get("_gen_t"):
                return
            entry["_gen_t"] = True
            entry.setdefault(
                "transition",
                {"transition_type": "intro", "reason": "", "script": ""})
            if not self.dj_on:
                return
            threading.Thread(
                target=self._do_gen_transition,
                args=(entry, from_track, to_track, from_broadcast), daemon=True).start()

        def _do_gen_transition(self, entry: dict, from_track, to_track,
                               from_broadcast: str = "",
                               yield_intro: bool = True) -> None:
            # ★★★ R119-4u：**转场主动让路给「当前曲的导听口播」**。
            #   为什么：Ollama 走一把全局串行锁（`ai/model.py::_ollama_lock`），而
            #   `threading.Lock` **不是 FIFO** —— 谁先被唤醒谁拿锁。于是"便宜"的转场
            #   （JSON、不联网）可以插队在"贵"的导听口播前面。用户原话："outro 的优先级
            #   特别高"。这里在**发 LLM 请求之前**看一眼 `_intro_pending`：当前曲的导听
            #   口播还没就绪就先等它（**有上限 20s，绝不卡死**），让口播先进 LLM。
            #   等待期间不持任何锁 ⇒ 不会与口播互相死等。
            #   ⚠ 上限可用 `MUSICAI_TRANSITION_YIELD_S` 覆盖（默认 20s）—— 便于单测
            #     真验"有上界、绝不卡死"，也便于用户在慢机器上调。
            # ★ R119-4x：**串行链里不需要让路** —— 链上这一步本来就排在「它自己那首
            #   歌的导听口播」之后，天然不会插队；故由链传 `yield_intro=False` 跳过，
            #   免得白等 20s 拖长整条链。直接调用 `_gen_transition`（非链路径）时仍保留
            #   这道守卫，且有硬上界，绝不卡死。
            if yield_intro:
                try:
                    _patience = max(0.0, float(os.environ.get("MUSICAI_TRANSITION_YIELD_S")
                                               or 20.0))
                except Exception:  # noqa 环境变量写坏 → 用默认，绝不因配置崩掉转场
                    _patience = 20.0
                _t0 = time.monotonic()
                while (getattr(self, "_intro_pending", False)
                       and (time.monotonic() - _t0) < _patience):
                    time.sleep(0.2)
                if getattr(self, "_intro_pending", False):
                    logger.info("[口播·转场] 导听口播等待超 %.0fs，转场不再让路，继续生成",
                                _patience)
            cinfo = self._song_info(from_track)
            ninfo = self._song_info(to_track)
            try:
                trans = self.brain.decide_transition(
                    cinfo, ninfo, self.taste_profile or {},
                    from_broadcast=from_broadcast)
            except Exception as e:  # noqa
                logger.warning("转场决策失败: %s", e)
                trans = {"transition_type": "intro", "reason": "", "script": ""}
            if not isinstance(trans, dict) or "transition_type" not in trans:
                trans = {"transition_type": "intro", "reason": "", "script": ""}
            trans.setdefault("script", "")
            # ★ R119-4s：**剥掉模型手写 JSON 时残留的收尾符**。实测（用户日志）
            #   `trans["script"]` 尾部会带 `"}` / `”}`（模型把 JSON 的收尾敲进了正文）
            #   ⇒ 会被送 TTS（把右引号念出来），也是用户怀疑的"分词符号有问题"。
            #   ⚠ 只在**末尾出现 `}`** 时才剥 ⇒ 不会误伤正文里的全角引号。
            try:
                _sc = (trans.get("script") or "")
                _sc2 = _TRAIL_JSON.sub("", _sc).strip()
                if _sc2 != _sc:
                    logger.info("[口播·转场] 清理 script 尾部残留：%r -> %r",
                                _sc[-12:], _sc2[-12:])
                    trans["script"] = _sc2
            except Exception:  # noqa
                pass
            # ★ 2026-11-17：**硬约束兜底（代码做，不指望提示词）**。
            #   A/B 实测（docs/_ab_transition_20261117.md）：提示词已经写了"≤25 字、
            #   不出现歌名歌手名"，但同一份提示词仍会随机破功 —— 实测出现过 42 字，
            #   以及「Miles Davis 的冷峻蓝调散去，该听 Niall Horan 的……」
            #   「SZA的气声停了，伍佰的吉他也该进来了」。这两类不能靠模型自觉：
            #     · 超长 ⇒ 送 TTS 会挤占切歌窗口（排程按 10~25 字估算）；
            #     · 点名 ⇒ 外文名字没有 <lang> 标签，会被中文嗓音按字母硬读成乱码。
            #   处理一律**丢弃这条转场**（等于 none）：宁可不说话，也不能说错。
            try:
                _sc3 = (trans.get("script") or "").strip()
                _why = ""
                if _sc3:
                    if len(_sc3) > 28:
                        _why = f"超长 {len(_sc3)} 字"
                    else:
                        _names = []
                        for _t in (from_track, to_track):
                            for _attr in ("title", "artist"):
                                _v = (getattr(_t, _attr, "") or "").strip()
                                for _part in (_v, *_v.split(",")):
                                    _p = _part.strip()
                                    if len(_p) >= 3:      # 太短的词会误伤正常句
                                        _names.append(_p)
                        _hit = next((n for n in _names if n in _sc3), "")
                        if _hit:
                            _why = f"点名「{_hit}」"
                if _why:
                    logger.info("[口播·转场] 丢弃本段（%s）：%r", _why, _sc3[:40])
                    trans["script"] = ""
                    trans["transition_type"] = "none"
                else:
                    trans["script"] = _sc3
            except Exception:  # noqa
                pass
            entry["transition"] = trans
            # ★ 2026-11-17 task-1：**生成成功标记**。边界用 `_gen_ok` 区分
            #   「模型明确说 type=none（不打搅）」与「占位符/根本没生成」——后者必须
            #   走成对静态兜底，前者允许安静。
            trans["_gen_ok"] = True
            # ★ R119-4q：把转场的**类型与文本**打出来 —— 这样"intro/outro 各多少条"
            #   可以直接在日志里数，而不是只能看到送 TTS 时被 melo 顺带打印的片段。
            try:
                logger.info("[口播·转场] type=%s 《%s》→《%s》 %d字 内容：%s",
                            trans.get("transition_type"),
                            getattr(from_track, "title", "?"),
                            getattr(to_track, "title", "?"),
                            len((trans.get("script") or "").strip()),
                            (trans.get("script") or "").replace("\n", " ")[:120])
            except Exception:  # noqa
                pass
            # 提前把转场口播合成好（后台线程，不阻塞 UI），记录 wav 路径与真实时长，
            # 供 _schedule_transition 精确排程、_begin_transition 直接播放并 wait_tts，
            # 保证「转场在前一首结束前 5 秒起播、且必须播完才切下一首」（修复转场被截断）。
            script = (trans.get("script") or "").strip()
            if script and self.dj_on and self.player.audio_ok:
                # ★★ 2026-11-18 task-1：转场音频**不再直连合成**，改为入同一个换模型窗口。
                #   实测直连的代价：与 35B 同驻 GPU 7860MiB、worker 锁死 160s+、
                #   连播放都卡；窗口批处理回填到 entry["transition_wav"] 才安全。
                _enq = getattr(self, "_enqueue_outro_audio", None)
                if _enq is not None:
                    try:
                        _enq(entry, script)
                        logger.info("[口播·转场] 音频已入窗口批处理（不直连 TTS）")
                    except Exception as e:  # noqa
                        logger.warning("[口播·转场] 入音频批失败（播放时静态兜底）: %s", e)
            # 若这正是对"即将播放的下一首"的转场、且转场定时器尚未触发，
            # 用已知脚本长度重排，确保长转场（intro）提前开始并在结尾处正好播完。
            # 必须按「剩余播放时间」重排，否则中途生成/更新转场时会重置到总时长，导致漏排。
            if (self.upcoming and entry is self.upcoming[0]
                    and self._trans_timer is not None and not self._trans_fired
                    and self._cur_dur_ms):
                remaining_ms = max(0.0, self._cur_dur_ms - self.player.get_position())
                self._schedule_transition(remaining_ms / 1000.0)

        # ---------- 转场调度 ----------

        def _schedule_transition(self, duration: float | None, seq=None) -> None:
            # ★ 2026-11-18：转场功能已砍 ⇒ 不排程任何转场定时器
            if not TRANSITION_ENABLED:
                return
            # 记录本转场所属的导航世代；_begin_transition 触发时若令牌已变，说明属于旧歌，丢弃。
            self._trans_seq = seq if seq is not None else getattr(self, "_nav_seq", 0)
            self._cancel_transition()
            if not self.roam_on:
                return
            dur_ms = (duration or 0) * 1000.0
            if dur_ms <= 0:
                return
            # 若已拿到下一首的转场脚本，按其真实/预计 TTS 时长排程，使"转场结束 ≈ 本曲结束"：
            # 长转场（强转场 intro）提前更久开始；短转场（outro）落在结尾 5 秒窗口内。
            # 上限 15s，避免极长转场把起播时间推得过早。
            lead = RadioConfig.TRANSITION_LEAD_MS
            with self._qlock:
                if self.upcoming:
                    tr = (self.upcoming[0].get("transition") or {})
                    ttype = tr.get("transition_type") or "intro"
                    script = (tr.get("script") or "").strip()
                    dur = tr.get("transition_dur_ms") or 0
                    if ttype in ("intro", "outro") and script:
                        est = dur or self._estimate_tts_ms(script)
                        lead = max(RadioConfig.TRANSITION_LEAD_MS, est)
                        lead = min(lead, 15000)
            delay = max(800, int(dur_ms - lead))
            start_lead_ms = dur_ms - delay   # 转场实际启播提前量（目标 3000~5000ms）
            ttype = "?"
            with self._qlock:
                if self.upcoming:
                    ttype = (self.upcoming[0].get("transition") or {}).get(
                        "transition_type") or "intro"
            logger.info(
                "[Timing] 转场排程 《%s》→下一首 start_lead=%.0fms (目标3000~5000) ttype=%s",
                (self.current.title if self.current else "?"), start_lead_ms, ttype)
            self._trans_deadline = time.monotonic() + delay / 1000.0
            self._trans_timer = threading.Timer(delay / 1000.0, self._begin_transition)
            self._trans_timer.daemon = True
            self._trans_timer.start()

        def _cancel_transition(self) -> None:
            if self._trans_timer is not None:
                try:
                    self._trans_timer.cancel()
                except Exception:  # noqa
                    pass
                self._trans_timer = None
            self._trans_fired = False
            self._trans_deadline = None

        def _reschedule_transition_from_position(self) -> None:
            """根据当前真实播放位置重新排程转场；若剩余时间不足则不再排程，等自然结束兜底。"""
            if not self.roam_on or not self._music_started or not self.current:
                return
            if self._transitioning:
                return
            pos_ms = self.player.get_position()
            remaining_ms = max(0.0, self._cur_dur_ms - pos_ms)
            if remaining_ms <= RadioConfig.TRANSITION_LEAD_MS + 1000:
                return
            self._schedule_transition(remaining_ms / 1000.0)

        def _begin_transition(self) -> None:
            """当前歌临近结尾：在前一首快结束的四五秒（或更早，若转场较长）启动「转场」文案。
            用户要求：转场在前一首快结束的四五秒开始、转场结束正好衔接下一首；
            下一首开始时再由其自身的 DJ 导听口播覆盖一小段前奏（intro 重叠）。
            因此这里只播"转场"脚本，口播交给下一首开头的 force_intro。"""
            # ★ 2026-11-18：转场功能已砍 ⇒ 双保险，即使有残留定时器也不播转场
            if not TRANSITION_ENABLED:
                return
            if self._transitioning:
                return
            # 过期转场守卫：本转场属于被切换掉的旧歌，丢弃，避免误切当前歌 / 重复切歌。
            if getattr(self, "_trans_seq", -1) != getattr(self, "_nav_seq", 0):
                logger.info("丢弃过期转场（已切换）")
                self._transitioning = False
                self._trans_fired = False
                return
            # 暂停期间必须禁止转场自动触发；若因竞态到达此处，按剩余时间重新排程。
            if self.player.state.value != "playing":
                self._transitioning = False
                self._trans_fired = False
                self._reschedule_transition_from_position()
                return
            # 根据真实播放位置二次校验：若离结尾还远，说明定时器基于总时长/恢复误差，
            # 重新按剩余时间排程，避免误切歌。
            pos_ms = self.player.get_position()
            dur_ms = self._cur_dur_ms
            remaining_ms = max(0.0, dur_ms - pos_ms)
            if remaining_ms > RadioConfig.TRANSITION_LEAD_MS + 3000:
                logger.info("[Timing] 转场触发时离结尾还远（remaining=%.0fms），重新排程", remaining_ms)
                self._transitioning = False
                self._trans_fired = False
                self._reschedule_transition_from_position()
                return
            self._transitioning = True
            self._trans_fired = True
            with self._qlock:
                if not self.upcoming:
                    if self.roam_on:
                        self._refill()
                if not self.upcoming:
                    self.player.stop()
                    self._transitioning = False
                    self._emit_playlist()
                    return
                nb_entry = self.upcoming[0]
            trans = nb_entry.get("transition") or {
                "transition_type": "intro", "reason": "", "script": ""}
            ttype = (trans.get("transition_type") or "intro")
            script = (trans.get("script") or "").strip()
            # 仅播放"转场"脚本；并让其在歌曲结尾处正好播完：
            # 起播前先等 (5秒 - 转场时长)，使「转场结束 ≈ 本曲结束」。
            # 关键修复：play_tts 是非阻塞的，必须 wait_tts 等转场真正播完，
            # 才允许 fade_out + advance 切下一首——否则会出现「转场没播完就播下一首」。
            # ★★★ R119-4v：**判定"这段边界会不会真的出声"，并据此记账** ——
            #   判据只写这一处（原先那个 if 的条件就是它），别在 `_on_track_ended` 再抄一遍：
            #   本项目铁则是"同一事实两处各存一份 ⇒ 必然漂移"。
            #   为什么要判：转场**没出声**时（type=none / 脚本空 / dj_on 关 / 音频不可用），
            #   下一首**必须照常播它自己的导听口播**，否则连着两段都没有 DJ 说话。
            _will_speak = bool(self.dj_on and ttype in ("intro", "outro")
                               and script and self.player.audio_ok)
            if _will_speak:
                self._trans_spoke = True
                self._n_trans_spoke = getattr(self, "_n_trans_spoke", 0) + 1
            else:
                self._n_trans_skip = getattr(self, "_n_trans_skip", 0) + 1
                logger.info(
                    "[口播·转场] 本段边界无口播（type=%s / 脚本 %d 字 / dj_on=%s / "
                    "audio_ok=%s）⇒ 下一首照常播它自己的导听口播",
                    ttype, len(script), self.dj_on, self.player.audio_ok)
                # ★ 2026-11-17 task-1（用户规则：兜底必须声文一致）：
                #   type=intro/outro 但脚本为空（占位符=从未生成 / 生成失败 / 生成中）
                #   ⇒ 用**成对**静态音频顶声：播的就是显示的那句，绝不"真文案+通用音"。
                #   生成成功且模型明确 type=none（不打搅）时保持安静，不做兜底。
                if ttype in ("intro", "outro") and not trans.get("_gen_ok"):
                    if self._speak_static_pair("转场未生成"):
                        self._trans_spoke = True
                        self._n_trans_spoke = getattr(self, "_n_trans_spoke", 0) + 1
                        self._n_trans_skip = max(0, getattr(self, "_n_trans_skip", 1) - 1)
            if _will_speak:
                wav = nb_entry.get("transition_wav")
                # 文件可能已被提前释放、或同脚本复用缓存路径被别的转场删掉：
                # 运行时重新合成兜底，避免 FileNotFoundError 崩掉转场线程、进而卡住切歌。
                if wav and not Path(wav).exists():
                    wav = None
                if not wav:
                    # ★★ task-1：边界**不再现场直连合成**（会与 LLM 同驻、拖住切歌），
                    #   没有窗口回填就用成对静态兜底。
                    logger.info("[口播·转场] 无预合成音频（不直连），改走成对静态兜底")
                if not wav:
                    # 合成拿不到音频 ⇒ 文字也不能用真文案（否则声文错配）
                    if not self._speak_static_pair("转场音频合成失败"):
                        logger.info("[口播·转场] 静态兜底不可用，本段静默（下一首照常导听）")
                else:
                    actual = nb_entry.get("transition_dur_ms") or self._estimate_tts_ms(script)
                    wait = max(0, int(RadioConfig.TRANSITION_LEAD_MS - actual))
                    if wait > 0:
                        time.sleep(wait / 1000.0)
                    self._emit("dj_speech", script)
                    self.player.duck(PlayerConfig.DUCK_VOLUME)
                    try:
                        self.player.play_tts(wav)
                        self.player.wait_tts()   # 等转场口播播完，再切下一首
                    except Exception as e:  # noqa 文件缺失/损坏：兜底跳过音频，但绝不卡死切歌
                        logger.warning("转场口播播放失败（跳过音频，继续切歌）: %s", e)
                    self.player.unduck(PlayerConfig.RECOVER_FADE_MS)
                    # 转场音频延迟删除：「下一首开播后约 8 秒」再释放（覆盖用户要求的
                    # 5~10 秒窗口）。下一首在转场块结束后立即 _advance 开播，故 8 秒后
                    # ≈ 下一首已播 8 秒；确保转场播放期间及可能的重触发窗口内文件始终存在，
                    # 不再因过早删除导致 FileNotFoundError 崩线程、下一首不播。
                    self._schedule_transition_cleanup(wav)
            # 音乐淡出 → 进入下一首；下一首开场导听（口播）会在其开头重叠前奏播（force_intro）
            self.player.fade_out(RadioConfig.FADE_OUT_MS)
            self._advance(intro_already_played=False)

        def _speak_static_pair(self, reason: str = "") -> bool:
            """成对静态兜底：**播哪句话就显示哪句话**（2026-11-17 用户规则）。

            用 `_generic_static()` 返回的 (text, wav) —— 音频与文本来自同一份 json，
            不可能错配；不经过任何 TTS 引擎，引擎全挂/窗口正开着也能出声。
            """
            try:
                if not (self.dj_on and getattr(self.player, "audio_ok", False)):
                    return False
                get = getattr(self, "_generic_static", None)
                if get is None:
                    return False
                text, wav = get()
                if not (text and wav):
                    return False
                self._emit("dj_speech", text)
                self.player.duck(PlayerConfig.DUCK_VOLUME)
                try:
                    self.player.play_tts(wav)
                    self.player.wait_tts()
                finally:
                    self.player.unduck(PlayerConfig.RECOVER_FADE_MS)
                logger.info("[口播·转场] %s → 成对静态兜底：%s", reason or "未就绪", text[:40])
                return True
            except Exception as e:  # noqa
                logger.warning("[口播·转场] 静态兜底失败（继续切歌）：%s", e)
                return False

        def _schedule_transition_cleanup(self, wav) -> None:
            """转场（outro）音频的清理 —— 统一到「下一条同类替换上一条」规则。

            ★ 2026-11-17（用户指定，与 intro 同一套）：
              · 不再用 8 秒定时器硬删 —— 那种写法遇到切歌/暂停会算错，误删就是这么来的；
              · 改由 `_park_tts_audio("outro", …)` 登记：下一首 outro 播完才删上一条；
              · 文件名带 `bc_outro_<曲目id>_<时间>`，资产目录（tts_static）永不删。
            """
            if not wav:
                return
            try:
                park = getattr(self, "_park_tts_audio", None)
                if park is not None:
                    # 用「下一首」作为归属：转场服务的正是即将播放的那首
                    park("outro", getattr(self, "current", None), wav)
                    return
                threading.Timer(8.0, self.tts.release, args=(wav,)).start()
            except Exception as e:  # noqa
                logger.info("转场音频清理失败（退回定时删除）：%s", e)
                try:
                    threading.Timer(8.0, self.tts.release, args=(wav,)).start()
                except Exception:  # noqa
                    pass

        # ---------- 上下首 ----------

