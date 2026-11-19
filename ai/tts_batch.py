"""TTS 批量预合成协调器（分时复用显存）。

问题：8GB 卡装不下 35B 大模型（~4.4GB）与 IndexTTS（~5.6GB）。二者只能轮番使用。

做法：把「文案已生成、只差语音」的条目攒成一批，然后
    停大模型 → 批量合成 → 重启大模型（+ 前缀预热）
期间大模型不可用，故本模块默认**关闭**，由 `MUSICAI_TTS_BATCH=1` 打开，
且批次窗口内通过 `llm_paused()` 暴露状态给其它调用方做降级。

用法（在 ai/schedule.py 里）：
    self._tts_batch = TTSBatch(self.tts, on_done=cb)   # 开关打开时
    ...
    self._tts_batch.submit(key, text)                  # 文案好了先入队
    # cb(key, wav_path) 在批处理完成后回调

可测试性：`flush(timeout)` 强制立即跑一批并等待完成，便于脚本验证。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("MusicAI.TTS")

# ★ 2026-11-17 task-1：窗口结果同时落一份机器可读账本 —— 即使 App 是双击启动、
#   stdout 没重定向到文件，事后也能验收「每窗条数/停起时长/回填比例」。
_WINDOW_LOG = Path(__file__).resolve().parents[1] / "data_runtime" / "tts_windows.jsonl"

_PAUSED = threading.Event()          # 批次窗口内为 True（大模型不可用）

# 在途 LLM 调用计数：批处理**停大模型前**必须等它归零，
# 否则会把正在生成的请求掐断（实测 ConnectionReset 10054 → 口播退化）。
_INFLIGHT = 0
_INFLIGHT_LOCK = threading.Lock()


def llm_enter() -> None:
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1


def llm_exit() -> None:
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT = max(0, _INFLIGHT - 1)


def llm_inflight() -> int:
    with _INFLIGHT_LOCK:
        return _INFLIGHT


def llm_paused() -> bool:
    """其它 LLM 调用方可用它判断「现在是否处于 TTS 换模型窗口」。"""
    return _PAUSED.is_set()


def batch_enabled() -> bool:
    """是否启用 TTS 攒批协调器。

    ★★ 2026-11-18 同步架构：常驻模式下**也**启用攒批（TTS 模型常驻 GPU，
    攒批只是把多条合成请求排队，不再停/起 LLM），所以返回 True。
    真正"是否停 LLM"由 coresident_mode() 在 TTSBatch 内部决定。
    """
    if coresident_mode():
        return True
    return os.environ.get("MUSICAI_TTS_BATCH", "0") == "1"


def coresident_mode() -> bool:
    """★ 2026-11-18：LLM + TTS 同卡常驻架构开关（默认开）。

    True  = TTS 不停止/重启 LLM，两者各自常驻（W8A16 + AREX Q4 全 GPU）；
    False = 退回旧分时架构（TTS 窗口内停 LLM，攒批合成后重启）。
    用 MUSICAI_CORESIDENT=0 可强制退回旧架构。
    """
    return os.environ.get("MUSICAI_CORESIDENT", "1") != "0"


class TTSBatch:
    def __init__(self, tts, *, batch_size: int = 5, idle_s: float = 6.0,
                 on_done: Callable[[str, str | None], None] | None = None,
                 llm_manager=None, warm_llm: Callable[[], None] | None = None):
        self._tts = tts
        self._batch_size = max(1, int(batch_size))
        self._idle_s = max(0.0, float(idle_s))
        self._on_done = on_done
        # ★★ 2026-11-18 同步架构：常驻模式下**忽略**传入的 llm_manager/warm_llm，
        #   这样即使调用方（schedule.py 的两条旁路）仍传 llm_manager，也不会停/起 LLM。
        #   这是"不漏接"的根治点——不用逐个改 schedule.py 的创建点。
        if coresident_mode():
            self._llm = None
            self._warm_llm = None
            logger.info("[TTS批] 常驻模式：不停止/重启大模型（LLM+TTS 同卡并行）")
        else:
            self._llm = llm_manager
            self._warm_llm = warm_llm
        self._q: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = False
        # ★ 2026-11-17 task-1：开窗前等在途 LLM 收尾的上限；等不到就**放弃本窗**
        #   （退避重试），绝不 terminate 正在生成的服务（旧逻辑 90s 后硬停 ⇒ 10054）。
        try:
            self._stop_wait_s = max(0.0, float(os.environ.get("MUSICAI_TTS_STOP_WAIT_S")
                                               or 30.0))
        except (TypeError, ValueError):
            self._stop_wait_s = 30.0
        try:
            self._retry_s = max(1.0, float(os.environ.get("MUSICAI_TTS_RETRY_S") or 20.0))
        except (TypeError, ValueError):
            self._retry_s = 20.0
        # ★ 2026-11-17 task-1（窗口限长）：每窗最多 N 条、从开窗起最长 S 秒。
        #   旧行为「一窗排空」会把窗口拖到 4~6 分钟，期间所有口播生成退化为离线模板。
        try:
            self._window_n = max(1, int(os.environ.get("MUSICAI_TTS_WINDOW_N") or 3))
        except (TypeError, ValueError):
            self._window_n = 3
        try:
            self._window_s = max(10.0, float(os.environ.get("MUSICAI_TTS_WINDOW_S") or 120.0))
        except (TypeError, ValueError):
            self._window_s = 120.0
        # ★ 2026-11-17 task-1（攒批）：队列 ≥N 条、或最老条目等够 D 秒才开窗。
        #   窗口固定成本 ~80s（停+起+KV 预热），1 条就开窗等于每条都付 80s；
        #   攒到 3 条后每条摊 ~27s。首个条目最多等 D 秒（默认 20s）。
        try:
            self._min_open = max(1, int(os.environ.get("MUSICAI_TTS_OPEN_N") or 2))
        except (TypeError, ValueError):
            self._min_open = 2
        try:
            self._max_delay = max(1.0, float(os.environ.get("MUSICAI_TTS_OPEN_DELAY_S") or 45.0))
        except (TypeError, ValueError):
            self._max_delay = 45.0
        self._at: dict[str, float] = {}
        self._front_keys: set[str] = set()   # ★ P0-C：紧急（当前曲）条目标记
        self._stale: dict[str, object] = {}  # ★ 2026-11-18：key→stale_check 回调（陈旧跳过合成）
        # ★ 2026-11-19：key→语言快照（zh/en）。合成时按 key 读，语言切换不影响在途任务。
        self._item_lang: dict[str, str | None] = {}
        self._truncated = False
        self._aborted = False
        # 每条完成的 wav 与唤醒事件（供 submit_wait 同步等待「当前曲导听」）
        self._results: dict[str, str | None] = {}
        self._events: dict[str, threading.Event] = {}
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="tts-batch")
        self._thread.start()
        logger.info("[TTS批] 已启动：batch_size=%d 开窗≥%d条或等%.0fs 等在途≤%.0fs 窗口≤%d条/%.0fs",
                    self._batch_size, self._min_open, self._max_delay,
                    self._stop_wait_s, self._window_n, self._window_s)

    # ---------------- 入队 ----------------
    def submit(self, key: str, text: str, *, front: bool = False,
               stale_check=None) -> bool:
        """入队；同 key 去重（★ 2026-11-18 P0）。

        ★★ P0-B（去重）：旧实现无条件 append，串行链回滚 + 世代更新会对同一首歌
          重复 submit（用户看到「总在做已播过的歌」），TTS 队列里堆满重复条目，
          每条都付 20~100s 合成 ⇒ 越积越晚。`_at` 记录入队时刻，仍在队中即视为重复。

        ★★ P0-C（紧急插队）：`front=True` 把条目插到队首 —— 当前曲导听用它抢在
          队里那一堆旧歌前面，避免「TTS 一直在做已播过的歌、下一首永远排不上」。

        ★★ 2026-11-18 时效性：`stale_check` 是可调用函数，合成前调用；返回 True
          表示这首已经播过/来不及了 ⇒ 跳过合成，把 worker 让给还来得及的歌。

        返回 True=新入队；False=重复被忽略。
        """
        if not (text or "").strip():
            return False
        with self._cv:
            # 去重：已在队列中的 key 不重复入队（正在合成/已出队的由 _results/_events 管）
            if any(k == key for k, _ in self._q):
                logger.info("[TTS批] 去重跳过 %s（队列已有，%d 条）", key, len(self._q))
                return False
            ev = self._events.get(key)
            if ev is None:
                self._events[key] = threading.Event()
            self._at[key] = time.time()
            if stale_check is not None:
                self._stale[key] = stale_check
            item = (key, text)
            if front:
                self._front_keys.add(key)
                self._q.insert(0, item)
            else:
                self._q.append(item)
            n = len(self._q)
            self._cv.notify_all()
        logger.info("[TTS批] 入队 %s%s（队列 %d）", key, " [紧急]" if front else "", n)
        return True

    def set_item_lang(self, key: str, lang: str | None) -> None:
        """★ 2026-11-19：登记某 key 的语言快照（zh/en），合成时按 key 读取。
        必须在 submit/submit_wait 之后、worker 开窗之前调用（入队即登记）。
        """
        with self._lock:
            if lang:
                self._item_lang[key] = lang

    def pending(self) -> int:
        with self._lock:
            return len(self._q)

    def submit_wait(self, key: str, text: str, timeout: float = 5.0,
                    stale_check=None, front: bool = True):
        """入队并最多等 `timeout` 秒；超时返回 None（音频稍后由回调补上）。

        ★★ 2026-11-18 流水线模式：口播链用它做「LLM 生成→TTS 合成→下一条」的
          严格串行，`timeout` 给足（默认 5s 不够合成，调用方传 90s）。
          `front` 默认 True 保持向后兼容（当前曲导听插队）；流水线里传 False。
        """
        self.submit(key, text, front=front, stale_check=stale_check)
        # ★★ 2026-11-18（用户「TTS 又超时，就差这一个」）：
        #   旧实现只靠 event.wait；实测 run_batch 明明「软闸门已开」=合成完成，
        #   但 submit_wait 仍 90s 超时 ⇒ event 与 result 的 set/pop 存在竞态错配。
        #   改为 **event + 轮询 _results 双保险**：只要合成结果出现就立刻返回，
        #   不再因 event 没 set 而白等满 90s（音频丢失）。
        deadline = time.monotonic() + timeout
        with self._lock:
            ev = self._events.get(key)
            if key in self._results:
                return self._results.get(key)
        while time.monotonic() < deadline:
            if ev is not None and ev.wait(0.5):
                with self._lock:
                    if key in self._results:
                        return self._results.get(key)
                # ★ 2026-11-18：event set 但结果暂不在（竞态：另一等待者刚取走 /
                #   result 尚未写入）→ **不立即返回**，继续轮询到 deadline。
                #   旧逻辑此处直接 return None ⇒ 明明合成完成却判"静音/超时"。
                #   event 置一次后 wait 恒 True，故把 ev 置 None 避免空转忙等。
                ev = None
            with self._lock:
                if key in self._results:      # 轮询兜底：event 错配也能拿到结果
                    return self._results.get(key)
                # ★★★ 2026-11-19 删除"最近合成缓存"兜底（用户「放InstantCrush播Dynamite」）：
                #   该兜底把上一首刚合成的 wav 当成当前这首返回（0.35s 秒返回），
                #   造成**歌与音频错配**（真机铁证）。用户自启后 agent hook 不存在，
                #   当年为绕 hook 加的兜底既多余又有害。现在只认自己 key 的 result。
        logger.warning("[TTS批] submit_wait 超时 %.0fs key=%s（队列=%d 结果keys=%d）",
                       timeout, key, len(self._q), len(self._results))
        return None

    # ---------------- 批处理 ----------------
    def _take_batch(self, force: bool = False) -> list:
        """取一批。force=False 时按「攒批」规则等：≥min_open 条 或 最老条目等够 max_delay。

        ★★ 2026-11-18：常驻模式下 TTS 不换模型、无窗口固定成本，攒批等待
        （45s）只会白白拖延口播时效性 ⇒ **有条目立即开窗**，不等攒批。
        """
        with self._cv:
            if not self._q:
                return []
            if coresident_mode():
                force = True   # 常驻：跳过攒批等待，来一条合一条
            if not force:
                while not self._stop:
                    n = len(self._q)
                    if n == 0:
                        return []
                    oldest = self._at.get(self._q[0][0], time.time())
                    age = time.time() - oldest
                    if n >= self._min_open or age >= self._max_delay:
                        break
                    # 每次最多等 2s：新条目入队会 notify，能立刻重新判断
                    wait = max(0.2, min(self._max_delay - age, 2.0))
                    self._cv.wait(timeout=wait)
            # ★ P0-C：先挑紧急（当前曲导听）条目，再按 FIFO 补齐，避免被积压旧歌淹没
            urgent = [it for it in self._q if it[0] in self._front_keys][: self._batch_size]
            rest = [it for it in self._q if it[0] not in self._front_keys]
            items = urgent + (rest[: self._batch_size - len(urgent)] if len(urgent) < self._batch_size else [])
            if items:
                item_set = set(items)
                self._q = [it for it in self._q if it not in item_set]
                for k, _ in items:
                    self._front_keys.discard(k)
            return items

    def _stop_llm(self) -> float:
        """开窗：先置暂停闸，再有界等待在途 LLM 收尾。

        返回 >=0：停服耗时；返回 -1：本窗放弃（在途调用迟迟不收尾，绝不能硬停——
        硬停会把正在生成的口播文案掐成 10054 并退化成离线模板）。
        """
        # ★★★ 2026-11-18（用户拍板「流水线串行：LLM 必须等 TTS 回填，
        #   没回填不能发下一个任务，不然并发肯定慢」）：
        #   常驻模式（_llm=None）**不卸载模型**，但必须关软闸门 _PAUSED ——
        #   让 ai/model.py 的 _wait_llm_window 挡住新 LLM 文案任务，实现
        #   「LLM 生成一条 → 关闸 → TTS 合成回填 → 开闸 → 才生成下一条」严格串行。
        #   旧逻辑常驻模式直接 return 0 不关闸 ⇒ LLM 与 TTS 并发抢卡 ⇒ 21~42s 慢合成。
        _PAUSED.set()                     # 先置闸：停服期间新 LLM 调用只会排队
        # ★★★ 2026-11-18 复现根治（心跳日志实锤）：常驻模式**绝不等在途归零**！
        #   心跳抓到「仍有2个LLM在途(已等30s)放弃本窗」⇒ 窗口反复放弃 ⇒ 90s 超时。
        #   那2个在途是品味分析/后台任务，不是本条TTS的文案（流水线里它已llm_exit）。
        #   Qwen3-TTS 是独立CLI子进程、与LLM并行无冲突 ⇒ 常驻模式关闸后立即开窗。
        #   只有分时架构（_llm 非None、要停LLM腾显存）才需要等在途归零。
        if self._llm is None:
            logger.info("[TTS批] 常驻模式：软闸门已关，立即开窗（不等在途）")
            return 0.0
        deadline = time.time() + self._stop_wait_s
        while llm_inflight() > 0 and time.time() < deadline:
            time.sleep(0.2)
        if llm_inflight() > 0:
            logger.warning("[TTS批] 仍有 %d 个 LLM 调用在途（已等 %.0fs），放弃本窗",
                           llm_inflight(), self._stop_wait_s)
            _PAUSED.clear()
            return -1.0
        # 在途归零后再静置 0.25s：让「刚通过暂停检查、还没 llm_enter」的调用看到闸门
        # 已关（它会在 _wait_llm_window 里排队），减少停服瞬间的 10054 竞态。
        time.sleep(0.25)
        t0 = time.time()
        try:
            self._llm.stop()
        except Exception as e:  # noqa
            logger.warning("[TTS批] 停大模型失败：%s", e)
        logger.info("[TTS批] 大模型已停（%.1fs），开始批量合成", time.time() - t0)
        return time.time() - t0

    def _start_llm(self) -> float:
        if self._llm is None:
            # ★★ 2026-11-18：常驻模式不重启模型，但**必须开闸** ——
            #   与 _stop_llm 里新加的 _PAUSED.set() 配对，否则闸门永久关闭、
            #   LLM 请求全部卡死（历史 bug：注释见 _stop_llm）。
            _PAUSED.clear()
            logger.info("[TTS批] 常驻模式：软闸门已开（TTS 已回填，LLM 恢复）")
            return 0.0
        # ★ 必须先把 TTS 模型卸掉再起大模型：5.6GB(IndexTTS) + 4.4GB(35B) > 8GB，
        #   两者同驻会让双方都掉到分钟级（实测 GPU 7804/8188 仍在换页）。
        try:
            self._tts.empty_cache()
            logger.info("[TTS批] 已卸载 TTS 模型，准备重启大模型")
        except Exception as e:  # noqa
            logger.warning("[TTS批] 卸载 TTS 模型失败：%s", e)
        t0 = time.time()
        try:
            ok = self._llm.start()
            logger.info("[TTS批] 大模型重启 %s（%.1fs）", "成功" if ok else "失败", time.time() - t0)
        except Exception as e:  # noqa
            logger.warning("[TTS批] 启动大模型失败：%s", e)
        _PAUSED.clear()
        # 前缀预热：重启后 KV 缓存为空，第一条真实请求会吃掉整段系统提示词预填充。
        # ★ 实测这笔是 58~66s（35B mmap 惰性载入），必须在日志里单列，否则会被误算进
        #   「起 LLM 16.5s」里，让窗口成本看起来只有一半。
        if self._warm_llm is not None:
            t_w = time.time()
            try:
                self._warm_llm()
                logger.info("[TTS批] KV 前缀预热 %.1fs", time.time() - t_w)
            except Exception as e:  # noqa
                logger.warning("[TTS批] 前缀预热失败：%s", e)
        return time.time() - t0

    def _drain_queue(self) -> list:
        """把当前队列全部取走（置闸后调用；含等待期间新到的条目）。"""
        out: list = []
        while True:
            more = self._take_batch(force=True)
            if not more:
                return out
            out += more

    def run_batch(self, items: list) -> dict:
        """对一批条目执行「停 LLM → 合成 → 起 LLM」，返回每条耗时。

        ★ 2026-11-17 task-1：
          · 先置暂停闸再有界等在途；等不到就整窗放弃并**放回队列**（退避重试），
            不再硬停正在生成的 LLM 请求；
          · 置闸成功后**再排空一次队列**，把等待期间新到的条目并入同一窗；
          · 每条开始/结束都有日志，窗口有总账（停/合成/起/预热）。
        """
        if not items:
            return {}
        t_all = time.time()
        t_stop = self._stop_llm()
        if t_stop < 0:
            self._requeue(items)
            self._aborted = True
            try:
                _WINDOW_LOG.parent.mkdir(parents=True, exist_ok=True)
                with _WINDOW_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                         "items": len(items), "ok": 0,
                                         "aborted": True}, ensure_ascii=False) + "\n")
            except Exception:  # noqa
                pass
            return {}
        self._aborted = False
        self._truncated = False
        more = self._drain_queue()
        if more:
            items = list(items) + more
            logger.info("[TTS批] 队列已排空：本窗共 %d 条", len(items))
        # ★ 2026-11-17 task-1（窗口限长）：每窗最多 N 条（默认 3），超出的**放回队列**
        #   留给下一窗（不是丢弃）。配合下面的时间上限，避免 4 分钟长窗把口播全拖成模板。
        if len(items) > self._window_n:
            extra = items[self._window_n:]
            items = items[:self._window_n]
            self._requeue(extra)
            self._truncated = True
            logger.info("[TTS批] 超过每窗条数上限 %d，%d 条留到下一窗",
                        self._window_n, len(extra))
        rows = {}
        try:
            for idx, (key, text) in enumerate(items, 1):
                # 时间上限：从开窗（停 LLM）算起。第一条永远执行（否则会永远轮不到），
                # 之后每开始一条前检查；到点就收尾，剩余放回队列。
                if idx > 1 and (time.time() - t_all) >= self._window_s:
                    extra = items[idx - 1:]
                    self._requeue(extra)
                    self._truncated = True
                    logger.info("[TTS批] 窗口已达 %.0fs 上限，剩余 %d 条留到下一窗",
                                self._window_s, len(extra))
                    break
                # ★★ 2026-11-18 时效性：合成前判陈旧（这首歌已播过/来不及了）⇒
                #   跳过合成，wav=None 回调给上层（上层据此只用文字/不填回）。
                #   省下 worker 时间给还来得及的歌 —— 治「总在做已播过的歌」。
                with self._lock:
                    _stale_fn = self._stale.pop(key, None)
                if _stale_fn is not None:
                    try:
                        _is_stale = bool(_stale_fn())
                    except Exception:  # noqa
                        _is_stale = False
                    if _is_stale:
                        logger.info("[TTS批] %s 已陈旧（歌已播过/来不及）→ 跳过合成", key)
                        rows[key] = {"s": 0.0, "wav": None, "stale": True}
                        with self._lock:
                            self._results[key] = None
                            ev = self._events.pop(key, None)
                            self._at.pop(key, None)
                            self._front_keys.discard(key)
                        if ev is not None:
                            ev.set()
                        if self._on_done is not None:
                            try:
                                self._on_done(key, None)
                            except Exception:  # noqa
                                pass
                        continue
                t0 = time.time()
                wav = None
                logger.info("[TTS批] 开始合成 %s（%d/%d）", key, idx, len(items))
                try:
                    # ★ 2026-11-19：用**任务自己的语言快照**（_item_lang[key]），
                    #   不读实时 tts.language —— 否则切换语言后，在途的旧语言文案会用新嗓念。
                    #   有快照按快照；无快照回退 tts.language（默认 None→引擎 zh，行为不变）。
                    _lang = self._item_lang.pop(key, None)
                    if _lang is None:
                        _lang = getattr(self._tts, "language", None)
                    wav = self._tts.synthesize_unique_tagged(text, lang=_lang)
                except Exception as e:  # noqa
                    logger.error("[TTS批] %s 合成失败：%s", key, e)
                # ★ 2026-11-19：删除 _last_synth 写入（与 submit_wait 兜底配套删除，
                #   根治"歌与音频错配"）。result 只按 key 写入自己的 _results[key]。
                rows[key] = {"s": round(time.time() - t0, 2), "wav": wav}
                logger.info("[TTS批] %s 合成 %.2fs -> %s", key, rows[key]["s"], wav)
                with self._lock:
                    self._results[key] = wav
                    ev = self._events.pop(key, None)
                    self._at.pop(key, None)
                    self._stale.pop(key, None)
                    self._front_keys.discard(key)
                if ev is not None:
                    ev.set()
                if self._on_done is not None:
                    try:
                        self._on_done(key, wav)
                    except Exception as e:  # noqa
                        logger.warning("[TTS批] 回调失败 %s：%s", key, e)
        finally:
            logger.info("[TTS批·心跳] finally 开始 _start_llm")
            try:
                t_start = self._start_llm()
                logger.info("[TTS批·心跳] finally _start_llm 返回 %.1f", t_start)
            except Exception as _e:  # noqa
                logger.error("[TTS批·心跳] finally _start_llm 异常：%s", _e, exc_info=True)
                t_start = -1.0
        logger.info("[TTS批] 一批 %d 条完成：停 %.1fs + 合成合计 %.1fs + 起(含预热) %.1fs = %.1fs",
                    len(rows), t_stop,
                    sum(r["s"] for r in rows.values()), t_start,
                    time.time() - t_all)
        self._record_window(rows, t_stop, t_start, t_all)
        return rows

    def _record_window(self, rows: dict, t_stop: float, t_start: float, t_all: float) -> None:
        if os.environ.get("MUSICAI_TTS_NO_LEDGER") == "1":
            return
        try:
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "items": len(rows),
                "ok": sum(1 for r in rows.values() if r.get("wav")),
                "stop_s": round(float(t_stop), 1),
                "synth_s": round(sum(float(r.get("s") or 0) for r in rows.values()), 1),
                "start_s": round(float(t_start), 1),
                "total_s": round(time.time() - t_all, 1),
                "apps": [{"key": k, "s": r.get("s"), "ok": bool(r.get("wav"))}
                         for k, r in rows.items()],
            }
            _WINDOW_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _WINDOW_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa 账本失败绝不影响主链
            logger.info("[TTS批] 窗口账本写入失败：%s", e)

    def _requeue(self, items: list) -> None:
        if not items:
            return
        with self._cv:
            self._q[:0] = items
            self._cv.notify_all()

    def flush(self, timeout: float = 900.0) -> dict:
        """立即把当前队列全部跑完（同步阻塞，供脚本/测试验证）。"""
        out = {}
        t0 = time.time()
        while time.time() - t0 < timeout:
            items = self._take_batch(force=True)
            if not items:
                break
            out.update(self.run_batch(items))
        return out

    def _loop(self) -> None:
        # ★ 2026-11-18 复现心跳：记录每轮，定位 worker 停摆点
        _hb = {"n": 0, "last_q": -1}
        while not self._stop:
            try:
                _hb["n"] += 1
                with self._lock:
                    _qnow = len(self._q)
                # 队列非空但长时间没取出 → 打点（每20轮=~4s一次的稀疏心跳）
                if _qnow and (_hb["n"] % 20 == 0 or _qnow != _hb["last_q"]):
                    logger.info("[TTS批·心跳] loop#%d 队列=%d paused=%s inflight=%d",
                                _hb["n"], _qnow, _PAUSED.is_set(), llm_inflight())
                _hb["last_q"] = _qnow
                items = self._take_batch()
                if items:
                    logger.info("[TTS批·心跳] loop#%d 开窗 %d 条 -> run_batch",
                                _hb["n"], len(items))
                    self.run_batch(items)
                    logger.info("[TTS批·心跳] loop#%d run_batch 返回", _hb["n"])
                    if self._aborted or self._truncated:
                        logger.info("[TTS批] 本窗%s，%.0fs 后处理下一窗",
                                    "已放弃" if self._aborted else "已限长收尾",
                                    self._retry_s)
                        time.sleep(self._retry_s)
                else:
                    time.sleep(0.2)
            except Exception as e:  # noqa
                logger.error("[TTS批·心跳] 循环异常：%s", e, exc_info=True)
                time.sleep(1.0)

    def stop(self) -> None:
        self._stop = True
        with self._cv:
            self._cv.notify_all()
