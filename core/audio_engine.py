"""
音频引擎：基于 sounddevice(PortAudio) + soundfile 的"单流软件混音"实现。

架构说明（替代 pygame.mixer 版本 core/player.py，对外 API 完全兼容）：
  - 单个 sd.OutputStream（44100Hz / 立体声 / float32）作为唯一出声口。
  - 内部三个"音源槽"在回调里软件混音：
      * music 槽：歌曲，流式（sf.SoundFile 分块读，seek 为 O(1) 文件跳转）
      * tts 槽  ：AI 口播（整段载入分块入队），音量独立
      * fx 槽   ：提示音/试听（整段载入分块入队）
  - Ducking：口播时把 music 槽增益压到 DUCK_VOLUME，结束渐强恢复。
  - 回调线程只做取块 + 乘增益 + 求和 + clip（RT 安全）；解码在生产者线程。
  - 播完检测内置：music 槽"已送帧 == 总帧 且 队列空"即触发 ended。

健壮性：无声卡/初始化失败时 audio_ok=False，自动进入"静默模式"——UI 与
逻辑照常运行，play() 用 duration 兜底触发 ended 保证自动续播可用。
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from config.settings import PlayerConfig, SHORT_AUDIO_RATIO
from utils.network_security import validate_media_url

logger = logging.getLogger("MusicAI.Player")

# 统一输出格式
STREAM_SR = 44100
STREAM_CH = 2
STREAM_DTYPE = "float32"
# 回调块大小（帧）。越小延迟越低、回调越频；1024@44.1k ≈ 23ms，够低且稳。
BLOCK_FRAMES = 1024
# 生产者->回调的有界队列容量（块数）。过大起播延迟高，过小易饿。
# ⚠ R88：24（≈557ms）在「语义索引跑满 CPU + 4GB 堆 GC 停顿」下会被击穿 → 提到 32
#   （≈743ms）纯播放无副作用（只多几十 ms 输出延迟，内存 +256KB），换来抗抖动余量。
# ⚠ 2026-09-18·23:5x：32（≈743ms）**仍会被前 20s 的 CLAP 段索引 burst 抽空**
#   （真机：卡顿集中在前 20s、开关口播影响不大 ⇒ 不是 TTS，是情绪识别抢核饿死
#    **生产者 fill 线程**）→ 提到 64（≈1.49s）。代价仅 +512KB 内存 + 起播多几十 ms，
#   换来约 1.5s 的抗饿死余量。
MUSIC_Q_DEPTH = 64
# ★ 2026-09-19：play() 落子时**批量预填**的块数（旧值 = 1 块）。
#   "每首歌一开始卡"的直接成因：切歌瞬间队列是空的，而此刻正是 CLAP 段索引 / VA
#   推理开始抢 CPU 的时候 ⇒ 生产者跟不上 ⇒ pump() 取不到块 → 补静音 = 可听的卡顿。
#   预填 32 块（≈0.74s）给起播一层厚垫（解码 0.74s 音频只花几十 ms，可忽略）。
PLAY_PREFILL_BLOCKS = 32
FIXED_Q_DEPTH = 16

# 频谱分析（音频响应壁纸用）：
#   每 SPEC_DECIM 个回调块（≈46ms@44.1k → ~21Hz 更新）对"即将送出"的混音
#   做一次 1024 点 rfft，按对数频带聚成 SPEC_BANDS 段能量，供 UI 律动。
#   ⚠ 只在 _spec_on=True（存在 WS 客户端）时计算，关闭时回调零开销。
#   ⚠ RT 安全：1024 点 rfft 实测 ~20µs，远小于 23ms 回调预算。
SPEC_BANDS = 16
SPEC_DECIM = 2
SPEC_FMIN = 30.0      # 最低分析频 Hz（低于此视为 DC/次声，弃）
SPEC_FMAX = 16000.0   # 最高分析频 Hz


_WIN_K32 = None          # 惰性初始化的 kernel32（含线程优先级 API 的**显式签名**）
_PRIO_WARNED: set = set()
# ★ 2026-09-19（R119-2）：MMCSS 注册失败时的 GetLastError（0=成功 / -1=异常 / >0=Win32 错误码）
_MMCSS_LAST_ERR = 0

# ★ 2026-09-19（R119-2n）：**GC 取证 + 调参**。
#   真机证据：本曲 `cb_us_max=8ms`（回调体不慢）却同时有 `cb_gap_max=983ms` 与看门狗
#   `stall_max=0.97s` —— **两者同量级** ⇒ 是"整个进程停止运行"。而能同时冻住
#   看门狗线程与 RT 回调线程的机制只有一个：**GC 的 stop-the-world**。
#   诱因：RT 回调里 `spec.feed` 每帧 `np.hanning(frames)` 新建数组（每秒 43 次分配）。
#   本块只做**测量**（`gc.callbacks`，官方钩子）与**阈值放宽**，不改任何音频语义。
#   关掉调参：`MUSICAI_GC_TUNE=0`；自定义阈值：`MUSICAI_GC_THRESH="20000,50,1000"`。
_GC_STAT = {"max_ms": 0.0, "gen": -1, "n": 0, "at": 0.0, "_t0": 0.0, "n_at_play": 0}
# ★ R119-2o：beats 闸门**决策计数** —— 用来判别"那 13s 索引是在暂停期(该拦没拦?)还是播放中(闸门失效?)"
_GATE_STAT = {"blocked": 0, "allowed": 0}


def note_gate(blocked: bool) -> None:
    """★ 记一次闸门决策（True=拦下/播放中，False=放行）。纯计数、失败静默。"""
    try:
        _GATE_STAT["blocked" if blocked else "allowed"] += 1
    except Exception:  # noqa
        pass
_GC_ARMED = False


def _gc_watch(phase, info):
    """★ 记录每次 GC 的耗时（`start`/`stop` 两相）。RT 之外、零风险。"""
    try:
        if phase == "start":
            _GC_STAT["_t0"] = time.perf_counter()
        else:
            dt = (time.perf_counter() - _GC_STAT.get("_t0", 0.0)) * 1000.0
            _GC_STAT["n"] = int(_GC_STAT["n"]) + 1
            if dt > _GC_STAT["max_ms"]:
                _GC_STAT["max_ms"] = round(dt, 1)
                _GC_STAT["gen"] = int(info.get("generation", -1))
                _GC_STAT["at"] = round(time.monotonic(), 2)
    except Exception:  # noqa
        pass


# ★ R119-2y：**停顿时的全线程栈取证**。
#   为什么必须用 `faulthandler` 而不是自己 dump：进程被冻住时（GIL 被某个线程握着）
#   **我们自己的采样线程也跑不起来** ⇒ "检测到停顿再 dump"只能拍到"停顿已结束"的状态。
#   `faulthandler.dump_traceback_later` 的看门狗在 **C 层**、**不需要 GIL** 就能把所有
#   线程的栈写到文件 —— 这正是标准库给"程序卡死"准备的工具。
#   ★ R119-5b **用法（已改为按需）**：
#     `stall_dump_ctl(True/False)` 只是**总开关**（播放期间开）；
#     真正的转储挂载由 `stall_dump_burst()` 完成，而它只被 `_stall_tick`
#     （20Hz 进程级停顿看门狗）在**单拍超额 ≥ `MUSICAI_STALL_TRIGGER_S`（默认 1.0s）**时调用。
#     ⚠ 为什么不能"播放期间一直周期性转储"：`dump_traceback_later(repeat=True)` 是
#       **无条件每 thr 秒转储**，而正常长任务（LLM 30~60s / MeloTTS CPU 合成 6s）
#       必然超过任何固定间隔 ⇒ 拍到的绝大多数是"根本没卡"的栈（真机实测：一次武装
#       4 张全是推荐 LLM + TTS），既污染证据又抢 GIL 写盘。
#     开关：`MUSICAI_STALL_DUMP=0` 关闭取证。
#     可调：`MUSICAI_STALL_TRIGGER_S`（触发阈，默认 1.0s）、
#           `MUSICAI_STALL_DUMP_S`（burst 内每张间隔，默认 0.5s）、
#           `MUSICAI_STALL_DUMP_MAX`（每轮 burst 最多几张，默认 4）。
_STALL_DUMP_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data_runtime", "logs", "stall_stacks.log")
_stall_dump_on = [False]        # 取证**总开关**（播放期间开）——不再等于"已挂 faulthandler"
_stall_dump_armed = [False]     # faulthandler 当前是否已挂（防重复挂）
_stall_dump_base = [0]          # 本轮 burst 的**起始文件偏移**（限次只算增量）
_stall_dump_fh = [None]         # 复用句柄：每 burst 新开会在 faulthandler 里攒句柄
_stall_dump_guard = [False]     # 守卫线程单例标志
_stall_dump_stop = threading.Event()   # 守卫线程退出信号（进程退出时自然停）
_STALL_DUMP_MAX = int(os.environ.get("MUSICAI_STALL_DUMP_MAX") or "4")
# ★ R119-5b：**触发阈**（秒）。只有看门狗单拍超额 ≥ 这个值才取证。
#   为什么必须"按需"：`dump_traceback_later(repeat=True)` 是**无条件周期转储**，
#   而正常长任务（LLM 30~60s / MeloTTS CPU 合成 6s）必然超过任何固定间隔
#   ⇒ 周期性取证拍到的绝大多数是"根本没卡"的栈（真机实测 4 张全是推荐 LLM + TTS）。
_STALL_DUMP_TRIGGER_S = float(os.environ.get("MUSICAI_STALL_TRIGGER_S") or "1.0")


def _count_stall_dumps(path: str, since: int = 0) -> int:
    """数 `since` 偏移**之后**写了几次转储（每次以 `Timeout (0:....)!` 开头）。

    ★ R119-5b：`since` 是必须的 —— 旧版数**整个文件**的累计值，导致"某次会话
      拍满 4 张"之后，后续每次武装都立刻判定超限 ⇒ **永久失去取证能力**
      （真机实测：101 次武装、0 张新转储）。限次口径必须是**本轮 burst 的增量**。
    """
    try:
        with open(path, "rb") as f:
            f.seek(max(0, int(since)))
            return f.read().count(b"Timeout (")
    except Exception:  # noqa
        return 0


def _stall_dump_disarm(reason: str = "") -> None:
    """撤掉 faulthandler（幂等）。"""
    try:
        if _stall_dump_armed[0]:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
    except Exception:  # noqa
        pass
    _stall_dump_armed[0] = False
    if reason:
        logger.info("停顿取证收手：%s", reason)


def stall_dump_burst(reason: str = "") -> bool:
    """★ R119-5b：**按需**取证 —— 只在"真的观察到停顿"时挂一次 burst。

    返回是否真的挂上。幂等：已挂则不再挂（避免把限额浪费在同一段停顿上）。
    """
    if not _stall_dump_on[0] or _stall_dump_armed[0]:
        return False
    try:
        import faulthandler
        os.makedirs(os.path.dirname(_STALL_DUMP_PATH), exist_ok=True)
        _rotate_log(_STALL_DUMP_PATH, 1024 * 1024)
        if _stall_dump_fh[0] is None:
            _stall_dump_fh[0] = open(_STALL_DUMP_PATH, "a",
                                     encoding="utf-8", buffering=1)
        f = _stall_dump_fh[0]
        f.write("\n===== stall dump burst (%s) @ %.3f =====\n" % (reason, time.time()))
        _stall_dump_base[0] = os.path.getsize(_STALL_DUMP_PATH)
        thr = float(os.environ.get("MUSICAI_STALL_DUMP_S", "0.5"))
        faulthandler.dump_traceback_later(thr, repeat=True, file=f)
        _stall_dump_armed[0] = True
        _start_stall_dump_guard()
        logger.info("停顿取证已挂：%s，%.2fs 一跳，最多 %d 张",
                    reason or "stall", thr, _STALL_DUMP_MAX)
        return True
    except Exception as e:  # noqa
        logger.debug("停顿取证挂载失败: %s", e)
        return False


def _start_stall_dump_guard() -> None:
    """★ R119-5b「限次」守卫：**单例常驻**，只数本轮 burst 的增量，够数即收手。

    为什么常驻而不是每次武装起一个线程：旧版每次 `stall_dump_ctl(True)` 都新起
    一个线程，反复武装会攒线程（且与新口径下的"总开关"语义不符）。
    """
    if _stall_dump_guard[0]:
        return
    _stall_dump_guard[0] = True

    def _run():
        while not _stall_dump_stop.wait(0.5):
            if not _stall_dump_armed[0]:
                continue
            if _STALL_DUMP_MAX <= 0:
                continue
            n = _count_stall_dumps(_STALL_DUMP_PATH, _stall_dump_base[0])
            if n >= _STALL_DUMP_MAX:
                _stall_dump_disarm("本轮已达上限 %d 张" % _STALL_DUMP_MAX)

    threading.Thread(target=_run, daemon=True, name="stall-dump-guard").start()


def stall_dump_ctl(on: bool) -> None:
    """★ 播放期间开/关"停顿栈取证"**总开关**。幂等；失败静默（纯诊断）。

    ★ R119-5b 语义变更：**本函数不再挂 faulthandler**（旧版在这里就
      `dump_traceback_later(..., repeat=True)` ⇒ 播放期间无条件周期转储）。
      现在它只开关"要不要取证"，真正挂载由 `stall_dump_burst()` 在
      `_stall_tick` 观测到真停顿时按需完成 —— 正常长任务**一张都不拍**。
    """
    try:
        want = bool(on) and os.environ.get("MUSICAI_STALL_DUMP", "1") != "0"
        if want == _stall_dump_on[0]:
            return
        if want:
            os.makedirs(os.path.dirname(_STALL_DUMP_PATH), exist_ok=True)
            _stall_dump_on[0] = True
            _start_stall_dump_guard()
            logger.info("停顿取证就绪（**按需**：单拍超额 ≥ %.1fs 才转储到 %s）",
                        _STALL_DUMP_TRIGGER_S, _STALL_DUMP_PATH)
        else:
            _stall_dump_on[0] = False
            _stall_dump_disarm("总开关关闭")
    except Exception as e:  # noqa
        logger.debug("停顿栈取证开关失败: %s", e)


def _rotate_log(path: str, cap: int) -> None:
    """文件超上限就截断（只留最近一半），避免长时间诊断把盘写满。"""
    try:
        if os.path.isfile(path) and os.path.getsize(path) > cap:
            with open(path, "rb") as f:
                f.seek(-cap // 2, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(b"[...rotated...]\n" + tail)
    except Exception:  # noqa
        pass


def tune_gc() -> None:
    """★ 幂等：装 GC 钩子 + 放宽阈值（+ 延迟 `freeze`）。失败一律静默。"""
    global _GC_ARMED
    if _GC_ARMED:
        return
    _GC_ARMED = True
    try:
        import gc
        if _gc_watch not in gc.callbacks:
            gc.callbacks.append(_gc_watch)
        if os.environ.get("MUSICAI_GC_TUNE", "1") != "0":
            raw = os.environ.get("MUSICAI_GC_THRESH", "20000,50,100000")
            a, b, c = (int(v) for v in raw.split(","))
            gc.set_threshold(a, b, c)
            # ★ R119-2r：gen2（第三个数）刻意给到 100000 ⇒ **自动全扫几乎不再触发**。
            #   实测：阈值放宽 + `gc.freeze()` 之后单次 gen2 仍是 **250ms**（stop-the-world）
            #   ⇒ 只能把它关掉；代价是环状垃圾要靠 monitor 线程在**非播放时**显式回收兜底
            #   （见 `PlayerEngine._monitor_loop` 里的空闲期 `gc.collect()`）。
            logger.info("GC 阈值已放宽为 %d/%d/%d（自动全扫≈关闭，空闲期显式回收兜底）",
                        a, b, c)

            def _freeze_later():
                # 等模型/索引载完再 freeze：把长期存活对象移出常规扫描集
                try:
                    time.sleep(25.0)
                    n = gc.freeze()
                    logger.info("gc.freeze() 完成（%s 个对象移入永久代）", n)
                except Exception:  # noqa
                    pass

            import threading as _th
            _th.Thread(target=_freeze_later, daemon=True, name="gc-freeze").start()
    except Exception as e:  # noqa
        logger.debug("GC 调参跳过: %s", e)

# ★ 2026-09-21：**停顿"点名"**。补充取证链的最后一环：原来的 `_stall_*` 只记"距起播秒数"，
#   而 `_HEAVY_RING` 记的是单调时钟 —— 两者**没有公共键** ⇒ 知道"卡了 0.3 s"却不知道"谁卡的"
#   （实测：`stall_stacks.log` 里全是 `### STALL` 标记、**一张栈都没有**，因为转储阈值 1.0 s
#   高于实测停顿 0.2~0.47 s）。本函数用**时间区间相交**把它们对上，纯读、零副作用。
_STALL_ATTR_WIN_S = float(os.environ.get("MUSICAI_STALL_ATTR_WIN_S") or "0.15")


def attribute_stalls(stalls: list, heavy: list,
                     win_s: float = _STALL_ATTR_WIN_S, top: int = 4) -> list:
    """★ 2026-09-21：把每个停顿与"当时结束的重活"配对。

    `stalls`：`[(t_end_abs, over_s), ...]`（`_stall_abs` 的形状）
    `heavy` ：`[(t_end_abs, name, ms), ...]`（`_HEAVY_RING` 的形状）
    判据：某笔重活的**运行区间** `[t_end−ms/1000, t_end]` 与停顿窗 `[t−over−win, t+win]`
          有交集 ⇒ 它（至少）是嫌疑之一。返回最近 `top` 条：
          `[{t, over, ops:[[name, ms], ...]}, ...]`（**ops 为空 = 嫌疑不在本进程的 note_heavy 覆盖
          范围内**，那时才需要去看线程栈）。
    ⚠ 这是**证据关联**不是因果证明：ops 非空且唯一 ⇒ 基本可定案；多条 ⇒ 需结合栈。
    """
    out = []
    for t, over in list(stalls)[-top:]:
        lo, hi = t - float(over or 0.0) - float(win_s), t + float(win_s)
        ops = []
        for he, name, ms in list(heavy):
            try:
                t1 = float(he)
                d = float(ms) / 1000.0
            except Exception:                       # noqa
                continue
            if (t1 >= lo) and ((t1 - d) <= hi):
                # ★ 2026-09-21：`~name` = **已知的非阻塞等待**（例：`~clap_wait`
                #   = 等常驻子进程回结果）。它不占本进程 GIL ⇒ 不能当停顿嫌疑。
                if str(name).startswith("~"):
                    continue
                ops.append([str(name), round(float(ms), 1)])
        out.append({"t": round(float(t), 3), "over": round(float(over or 0.0), 3),
                    "ops": ops})
    return out


# ★ 2026-09-19（R119-2h）：**重活计时环**。
#   起因：起播后 ~9s 出现一次 **1.0s 整进程冻结**（看门狗 `stall_max=1.029s` 与回调间隔
#   `cb_gap_max=1032ms` **同时**到位 ⇒ 整个进程没跑），而内存已宽裕（提交余量 13~14GB）
#   ⇒ 不是换页，是**某段重活占住了 GIL**。起播 +9s 正落在 `MOOD_TRACK_GRACE_S=8s` 之后
#   ⇒ 头号嫌疑 = 语义管线（CLAP 段索引 / VA 推理）。
#   非 RT 线程都可以 `note_heavy("名字", 毫秒)` 记一笔；诊断面按耗时倒序暴露最近若干笔。
#   只留最近 16 笔，纯追加 + 浮点，开销可忽略。
_HEAVY_RING: list = []
_HEAVY_LOCK = threading.Lock()


def note_heavy(name: str, ms: float) -> None:
    """★ 记一次「重活」耗时（**非 RT 线程**调用）。用于定位"整进程冻结"的肇事者。"""
    try:
        with _HEAVY_LOCK:
            _HEAVY_RING.append((round(time.monotonic(), 3), str(name),
                                round(float(ms), 1)))
            del _HEAVY_RING[:-16]
    except Exception:  # noqa 诊断绝不拖垮调用方
        pass


def _win_kernel32():
    """★ 2026-09-19：返回带**显式 argtypes/restype** 的 kernel32 句柄。

    为什么必须有这个：`ctypes.windll.kernel32.GetCurrentThread()` 默认
    `restype=c_int`，返回伪句柄 -2 这个 **32 位** 值；再作为实参传给
    `SetThreadPriority` 时按 32 位写寄存器 ⇒ 句柄变成 0x00000000FFFFFFFE
    （**非法**）⇒ 调用返回 FALSE、线程优先级纹丝不动 —— **静默空转**。

    实测（`_r119_probe_prio2.py`，同线程 set 后立刻读回）：
        裸写法   set(2) → 返回 0、读回 0   （没生效）
        显式签名 set(2) → 返回 1、读回 2   （生效）
    ⇒ 本项目此前所有"抬高音频线程 / 压低分析线程"的改动**从未生效过**。
    返回 None = 不可用（非 Windows / 加载失败），调用方应静默跳过。"""
    global _WIN_K32
    if _WIN_K32 is None:
        try:
            import ctypes
            from ctypes import wintypes
            d = ctypes.WinDLL("kernel32", use_last_error=True)
            d.GetCurrentThread.argtypes = []
            d.GetCurrentThread.restype = wintypes.HANDLE
            d.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
            d.SetThreadPriority.restype = wintypes.BOOL
            d.GetThreadPriority.argtypes = [wintypes.HANDLE]
            d.GetThreadPriority.restype = ctypes.c_int
            _WIN_K32 = d
        except Exception:  # noqa
            _WIN_K32 = False
    return _WIN_K32 or None


def _enter_mmcss() -> bool:
    """★ 2026-09-19：把**当前线程**注册进 Windows MMCSS（多媒体类调度服务）的
    "Pro Audio" 类 —— 这是 Windows 上音频线程的**官方正确做法**，远优于裸
    `SetThreadPriority`：

      - 系统据此把本线程识别为音频线程 ⇒ 优先获得 CPU，且**不易被普通线程抢占**；
      - 直接对症本项目的病根：「回调是 Python 函数，必须抢 GIL，而下载/解码/
        预取线程在起播前 20s 抢得凶」⇒ underflow ⇒ 听感卡几下。
        注册 MMCSS 后**不必一个个去追那些线程**，一次覆盖。
      - Pro Audio 类在现代 Windows 上**不禁用**系统节能/降频（不影响蓝牙与省电）。

    返回 True = 注册成功。非 Windows / avrt 不可用 ⇒ False（只是少一层保护，不报错）。
    句柄有意不 AvRevert：本线程与输出流同生命周期，进程退出即回收。

    ⚠⚠ **每线程只能成功注册一次**（2026-09-19 真机实测：同线程第二次调用返回 0/False，
       即便中间改过优先级也不恢复）。所以：
         ① 只允许在 RT 回调**首帧**调一次（`_callback` 用 `_rt_boosted` 闸门保证）；
         ② 任何"再试一次"都是徒劳 —— 不要把返回 False 读成"随机失败"；
         ③ 别在主线程/别的线程先调它，否则真正的 RT 线程就永远是 False。
       诊断面 `mmcss=False` 的含义因此是"**本线程注册未成功**"，而 `rt_boosted=False`
       才是"根本没试过"——两者要分开读（见 `PlayerEngine.audio_diag` 字段说明）。"""
    global _MMCSS_LAST_ERR
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        # ★ 2026-09-19（R119-2）：`use_last_error=True` 才能取到真实错误码。
        #   实测：RT 线程上本调用会失败（最可能是 PortAudio 的 WASAPI 线程**已经**
        #   注册过 MMCSS —— 每线程只能注册一次，于是我们这次必然返回 0）。
        #   失败**不代表没护住**：真正的证据是 `_read_self_priority()` 读回的 `rt_prio`。
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        task = wintypes.DWORD(0)
        h = avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task))
        _MMCSS_LAST_ERR = 0 if h else int(ctypes.get_last_error())
        return bool(h)
    except Exception:  # noqa 纯调度提示
        _MMCSS_LAST_ERR = -1
        return False


def _read_self_priority() -> int:
    """★ 2026-09-19（R119-2）：读回**当前线程**的真实优先级（-999 = 读不到）。

    为什么必须有这个：Windows 上 `SetThreadPriority` 会**静默失败**（见
    `_win_kernel32` docstring），而 MMCSS 在 RT 线程上又常注册不上（PortAudio 自己
    已注册）。**只有读回的数**才能证明"回调线程到底被护住了没有"。"""
    try:
        k = _win_kernel32()
        if k is None:
            return -999
        return int(k.GetThreadPriority(k.GetCurrentThread()))
    except Exception:  # noqa
        return -999


def _set_self_priority(level: int) -> None:
    # ★ 2026-09-19：约定 level 取值 —— 2 = TIME_CRITICAL(15)、1 = ABOVE_NORMAL、
    #   0 = NORMAL、-1 = BELOW_NORMAL、-2 = LOWEST。
    """★ 2026-09-18：把**当前线程**的 OS 调度优先级设为 level。
    Windows：+1=ABOVE_NORMAL、0=NORMAL、-1=BELOW_NORMAL、-2=LOWEST。
    用途：把音频**生产者(fill)**线程抬到 ABOVE_NORMAL，使它高于后台 CLAP/段索引线程
    （后者是 LOWEST）—— 光压低分析线程不够，必须同时抬高音频生产者。
    POSIX：只能降不能升（nice 负值需特权），升优先级静默跳过。失败一律静默（纯调度提示）。"""
    try:
        if os.name == "nt":
            import ctypes
            # ★ 2026-09-19：level=2 映射到 TIME_CRITICAL(15)（音频 RT 专用）
            _lv = 15 if int(level) >= 2 else int(level)
            k = _win_kernel32()
            if k is None:
                return
            th = k.GetCurrentThread()
            if not k.SetThreadPriority(th, _lv):
                if _lv not in _PRIO_WARNED:      # 失败只告警一次，别刷屏
                    _PRIO_WARNED.add(_lv)
                    logger.warning("SetThreadPriority(%d) 失败 err=%d（优先级未生效）",
                                   _lv, ctypes.get_last_error())
                return
            # ★ 读回校验：把"静默失效"变成"看得见的告警"（本项目铁则）
            _got = k.GetThreadPriority(th)
            if _got != _lv and _lv not in _PRIO_WARNED:
                _PRIO_WARNED.add(_lv)
                logger.warning("线程优先级未生效：期望 %d，实际 %d", _lv, _got)
        elif level < 0:
            os.nice(max(1, min(10, -int(level))))
    except Exception:  # noqa
        pass


class PlayerState(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


def _resample_to_stream(x: np.ndarray, sr: int) -> np.ndarray:
    """把任意采样率音频重采样到 STREAM_SR（用 scipy resample_poly，精确有理因子）。
    44100 与 22050/11025/48000/24000/16000 等都是整数比，质量高且快。"""
    if sr == STREAM_SR:
        return x
    from fractions import Fraction
    from scipy import signal as _sig
    # 找最简有理近似（scipy resample_poly 需要整数 up/down）
    f = Fraction(STREAM_SR, sr).limit_denominator(1000)
    up, down = f.numerator, f.denominator
    y = _sig.resample_poly(x, up, down, axis=0)
    return np.ascontiguousarray(y, dtype=np.float32)


def _to_stereo_float(x: np.ndarray, sr: int) -> np.ndarray:
    """统一成 (N,2) float32 + 重采样到 STREAM_SR（单声道复制 / 多声道截断）。"""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    elif x.shape[1] > 2:
        x = x[:, :2]
    x = _resample_to_stream(x, sr)
    return np.ascontiguousarray(x, dtype=np.float32)


class ParametricEQ:
    """RBJ Biquad EQ 链。滤波器状态(zi)在实例内**跨块持久**（research 实测：
    每块重建 = 结果错误 + 爆音）。默认无频段 = flat 旁路（零开销）。"""

    def __init__(self, sr: int = STREAM_SR) -> None:
        from scipy import signal as _sig
        self.sr = sr
        self._sig = _sig
        self._sos = np.empty((0, 6), dtype=np.float64)
        self._zi: np.ndarray | None = None

    @staticmethod
    def _peak(sr, f0, gain_db, q):
        """RBJ peaking。返回 scipy sos 6 列 [b0,b1,b2,a0,a1,a2]。
        ⚠ 曾漏 a0 列（返回 5 元组）→ sosfilt 报 'sos must be shape (n,6)'；
           add_* 从没真正跑通过（EQ 恒 flat 旁路），set_bands 首用即暴露。"""
        A = 10 ** (gain_db / 40.0)
        w0 = 2 * np.pi * f0 / sr
        cw, sw = np.cos(w0), np.sin(w0)
        alpha = sw / (2 * q)
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
        return (b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0)

    @staticmethod
    def _lowshelf(sr, f0, gain_db, q=0.707):
        A = 10 ** (gain_db / 40.0)
        w0 = 2 * np.pi * f0 / sr
        cw, sw = np.cos(w0), np.sin(w0)
        alpha = sw / (2 * q)
        sq = 2 * np.sqrt(A) * alpha
        b0 = A * ((A + 1) - (A - 1) * cw + sq)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - sq)
        a0 = (A + 1) + (A - 1) * cw + sq
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - sq
        return (b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0)

    @staticmethod
    def _highshelf(sr, f0, gain_db, q=0.707):
        A = 10 ** (gain_db / 40.0)
        w0 = 2 * np.pi * f0 / sr
        cw, sw = np.cos(w0), np.sin(w0)
        alpha = sw / (2 * q)
        sq = 2 * np.sqrt(A) * alpha
        b0 = A * ((A + 1) + (A - 1) * cw + sq)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - sq)
        a0 = (A + 1) - (A - 1) * cw + sq
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - sq
        return (b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0)

    def add_peak(self, f0, gain_db=0.0, q=1.0) -> None:
        self._sos = np.vstack([self._sos, self._peak(self.sr, f0, gain_db, q)])
        self._zi = None

    def add_lowshelf(self, f0, gain_db=0.0, q=0.707) -> None:
        self._sos = np.vstack([self._sos, self._lowshelf(self.sr, f0, gain_db, q)])
        self._zi = None

    def add_highshelf(self, f0, gain_db=0.0, q=0.707) -> None:
        self._sos = np.vstack([self._sos, self._highshelf(self.sr, f0, gain_db, q)])
        self._zi = None

    def reset(self) -> None:
        self._sos = np.empty((0, 6), dtype=np.float64)
        self._zi = None

    # 10 段 ISO 倍频中心（预设/手动 EQ 共用，见 docs/eq_research_r68.md §3.1）
    EQ_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
    EQ_BAND_COUNT = len(EQ_BANDS)

    def set_bands(self, gains_db, freqs=None, q=1.41,
                  shelf_low=True, shelf_high=True) -> None:
        """整体重建 EQ 链：由每频段 dB 增益 → biquad 链（_sos 原子换入）。

        - 频点与现有 _sos 一致时**延续旧 _zi**（丢弃重建会在换曲线瞬间把滤波
          器状态"断头"→ 可闻咔哒/爆音，docstring 已有此警告）；
          段数变化才重建 _zi。
        - 首段低频用 lowshelf、末段高频用 highshelf（更贴近目标形状、避免
          31Hz/16kHz 处的峰值外溢）；其余为 Q=q 的 peak。
        - 所有增益 clamp 到 ±12dB，防手动+动态叠加后削波。
        """
        freqs = list(freqs) if freqs is not None else list(self.EQ_BANDS)
        if not gains_db:
            gains_db = [0.0] * len(freqs)
        gains = [max(-12.0, min(12.0, float(g))) for g in gains_db]
        if len(gains) != len(freqs):
            raise ValueError("set_bands: len(gains_db) != len(freqs)")
        sos_list = []
        for i, (f0, g) in enumerate(zip(freqs, gains)):
            if g == 0.0:
                continue            # 0dB 段不加滤波器（旁路该段，省算力）
            if shelf_low and i == 0:
                sos_list.append(self._lowshelf(self.sr, f0, g, q))
            elif shelf_high and i == len(freqs) - 1:
                sos_list.append(self._highshelf(self.sr, f0, g, q))
            else:
                sos_list.append(self._peak(self.sr, f0, g, q))
        new_sos = (np.vstack(sos_list) if sos_list
                   else np.empty((0, 6), dtype=np.float64))
        # 段数不变 → 延续旧 _zi（防爆音）；否则重建状态
        if new_sos.shape[0] == self._sos.shape[0] and self._sos.shape[0] > 0:
            new_zi = self._zi
        else:
            new_zi = None
        self._sos = new_sos
        self._zi = new_zi

    def process(self, x: np.ndarray) -> np.ndarray:
        if self._sos.shape[0] == 0:
            return x
        if self._zi is None:
            zi0 = self._sig.sosfilt_zi(self._sos)          # (n,2)
            self._zi = np.repeat(zi0[:, :, None], 2, axis=2)  # (n,2,2)
        y, self._zi = self._sig.sosfilt(self._sos, x, axis=0, zi=self._zi)
        return y.astype(np.float32, copy=False)


# ---------------------------------------------------------------------------
# 频谱分析（音频响应壁纸：真实频谱 → WS → UI 律动）
# ---------------------------------------------------------------------------

SPEC_HZ = 20.0            # 频谱分析线程节拍（与原"每 2 块算一次"≈21.5Hz 等价）
SPEC_RING_BLOCKS = 6      # RT→分析 环形缓冲容量（块；6×1024≈140ms，抗调度抖动）


class SpectrumAnalyzer:
    """对"即将送出"的混音块做轻量频谱分析，聚成 SPEC_BANDS 段能量。

    ★ 2026-09-19（R119-2q）**架构对齐**：原实现在 **RT 回调内**直接做
    `mean + 加窗 + rfft + 16 段 Python 循环` —— 真机分段计时实测它占了回调的
    **87%**（`feed` 段 12.7ms / 回调总 14.6ms，预算 23.2ms）。
    现在改成与 `FeatureAnalyzer` **同一个模式**：
      · `feed()`（RT 内）：**只做一次 memcpy** 进环形缓冲（零分配、零 FFT）；
      · `_loop()`（独立线程、SPEC_HZ 节拍）：取**最新一块**算 FFT 与频带能量；
      · `read()`：契约不变（仍返回副本）。
    ⇒ RT 侧开销从 ~12.7ms 降到 ~10µs 量级，安全余量放大约 7 倍。
    仍保留原设计约束：`enabled=False` 时 RT 侧零开销；分析失败**绝不干扰出声**。
    """

    def __init__(self, bands: int = SPEC_BANDS,
                 fmin: float = SPEC_FMIN, fmax: float = SPEC_FMAX,
                 sr: int = STREAM_SR) -> None:
        self.bands = bands
        self.sr = sr
        self.enabled = False
        self._decim = SPEC_DECIM
        self._count = 0
        # 预计算对数频带边界（FFT bin 索引，含端点）
        nfft = BLOCK_FRAMES
        freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
        edges = np.geomspace(fmin, min(fmax, sr / 2 - 1), bands + 1)
        self._bin_edges = np.searchsorted(freqs, edges).astype(np.intp)
        self._bin_edges[-1] = len(freqs)  # 封顶到奈奎斯特
        self._nfft = nfft
        self._latest: list[float] = [0.0] * bands
        self._lock = threading.Lock()
        # ★ R119-2q：RT→分析 环形缓冲；**窗在构造时算一次**（不再每帧分配）
        self._ring = np.zeros((SPEC_RING_BLOCKS, nfft, STREAM_CH), dtype=np.float32)
        self._mono = np.zeros(nfft, dtype=np.float32)   # 分析线程的临时单声道缓冲
        self._win = np.hanning(nfft) if nfft > 1 else None
        self._w = 0
        self._r = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="spec-analyzer")
        self._thread.start()

    # RT 回调线程调用：喂一块即将送出的混音 (frames, ch) → **只入环**
    def feed(self, mixed: np.ndarray) -> None:
        if not self.enabled:
            return
        self._count += 1
        if self._count % self._decim != 0:
            return
        # ★ R119-2q：RT 侧只做一次 memcpy —— 原实现在这里算 rfft（占回调 87% 时间）
        try:
            n = mixed.shape[0] if mixed.shape[0] <= self._nfft else self._nfft
            self._ring[self._w % SPEC_RING_BLOCKS, :n] = mixed[:n]
            self._w += 1
        except Exception:  # noqa 入环失败不干扰出声
            pass

    def _loop(self) -> None:
        interval = 1.0 / SPEC_HZ
        while not self._stop.is_set():
            if not self.enabled:
                self._r = self._w            # 关闭态：写读指针对齐，重开后立即用新数据
                self._stop.wait(0.1)
                continue
            t0 = time.perf_counter()
            try:
                self._step()
            except Exception:  # noqa 分析失败不干扰出声
                pass
            delay = interval - (time.perf_counter() - t0)
            self._stop.wait(delay if delay > 0 else 0.001)

    def _step(self) -> None:
        w = self._w
        if w <= self._r:
            return
        self._r = w                          # 视觉只需最新一块
        self._analyze(self._ring[(w - 1) % SPEC_RING_BLOCKS])

    def _analyze(self, blk: np.ndarray) -> None:
        """原来的频带聚合数学**逐行保留**，只是搬到了独立线程。"""
        n = blk.shape[0]
        if n <= 0:
            return
        x = self._mono[:n]
        np.mean(blk[:n], axis=1, out=x)
        if n > 1 and self._win is not None:
            x = x * self._win[:n]
        mag = np.abs(np.fft.rfft(x))
        # 频带聚合：每带 RMS
        seg_rms = []
        for i in range(self.bands):
            lo, hi = self._bin_edges[i], self._bin_edges[i + 1]
            if hi <= lo:
                seg_rms.append(0.0)
                continue
            seg = mag[lo:hi]
            seg_rms.append(float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0)
        # 整块响度（dB，-70 下限）
        total = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        loud_db = 20.0 * np.log10(total + 1e-9)
        loud = max(0.0, min(1.0, (loud_db + 70.0) / 60.0))  # -70..+10 → 0..1
        # 每带：相对全块的比例（谱形状，0~1 归一）+ 响度整体缩放，软拐平滑
        denom = max(float(np.sqrt(np.mean(np.asarray(seg_rms) ** 2))), 1e-9)
        out = []
        for v in seg_rms:
            shape = v / denom          # 0..1 相对强度（峰值带≈1）
            vv = shape * shape * loud  # 平方让弱带更弱（更接近视觉频谱）
            # ★ R119-2q：显式转 Python float —— `round(np.float64,4)` 仍返回 np.float64，
            #   直接进 JSON 有序列化风险（数值不变，只是类型更稳）。
            out.append(float(round(max(0.0, min(1.0, float(vv))), 4)))
        with self._lock:
            self._latest = out

    # 非 RT 线程调用：取最近一帧段能量（0~1，len=bands）
    def read(self) -> list[float]:
        with self._lock:
            return list(self._latest)


# ---------------------------------------------------------------------------
# P2 实时特征帧（Emotional Water 节拍通道：30Hz rms/频带/谱通量/onset）
# ---------------------------------------------------------------------------

FEAT_HZ = 30.0            # 特征帧率（独立线程节拍；与 RT 回调 43Hz 解耦）
FEAT_RING_BLOCKS = 10     # RT→分析 环形缓冲容量（块；10×1024≈231ms 抗调度抖动）
FEAT_WIN = 2048           # 分析窗样本数（≈46ms@44.1k；频率分辨率 ≈21.5Hz）
# 三频带边界（Hz）：bass( kick/bass) / mid(人声/主体) / high(镲片/气声)
FEAT_BANDS = ((30.0, 160.0), (160.0, 2000.0), (2000.0, 12000.0))
FEAT_FLUX_FMAX = 2000.0   # 谱通量积分上限（节拍能量主导区：kick/snare/bass）
FEAT_FLUX_HIST = 45       # 自适应阈值历史长度（≈1.5s@30Hz，median 鲁棒）
FEAT_ONSET_RATIO = 1.9    # onset 阈值 = median(flux 历史)×ratio + delta
FEAT_ONSET_DELTA = 0.008  # 阈值绝对下限（安静段的噪声地板）
FEAT_ONSET_GAP = 0.25     # 相邻 onset 最小间隔 s（同一起音的衰减回声不重复计数；
                          #   前端水面另有 900ms 冷却，此处对齐视觉消费粒度）
FEAT_ONSET_MIN_LOUD = 0.04  # 响度门（静音/极安静段不触发起音）


class FeatureAnalyzer:
    """P2 实时音频特征帧：RT 回调只入环形缓冲，独立线程 30Hz 出帧。

    帧结构（bus.emit('feat', frame) → 前端 window.__feat）：
      {seq, t, rms, bass, mid, high, flux, onset, ob, om, oh}
      · rms/bass/mid/high ∈ 0..1，与 SpectrumAnalyzer 同配方（shape²×loud），
        保证前端 10Hz 回退通道与 30Hz 通道视觉同尺度（涟漪观感连续）；
      · flux = 归一化谱正差分（谱通量，30~2000Hz 节拍主导区），帧内按谱峰
        归一 → 与用户音量/EQ 增益无关的"音色变化率"；
      · onset = flux 超过自适应阈值（median×ratio+delta）+ 响度门 + 最小间隔
        —— 经典 adaptive-threshold onset detection，替代前端 10Hz 低音上升沿。
      · ob/om/oh = R82b 三频带独立 onset 位（同参数各自自校准；旧前端忽略
        这些字段自动走全局 onset 路径——向后兼容）。

    RT 安全（铁律：回调线程绝不重活）：feed() 仅往**预分配 numpy 环**写一块
    （slot 切片赋值 = 纯 memcpy，零分配零锁；deque.append 偶发触发 block
    malloc，在 RT 线程内是概率性 underrun 源——R80 爆音加固）；
    rfft/包络/onset 全部在特征线程算。enabled=False（无 WS 客户端）时
    回调零拷贝、线程低频空转，与 SpectrumAnalyzer 同一订阅闸门。
    """

    def __init__(self, autostart: bool = True) -> None:
        self.enabled = False
        # 预分配环形缓冲（RT 写入零分配）：_w/_r 单调递增计数，
        # slot = _ring[_w % FEAT_RING_BLOCKS]；消费侧落后超容量只保最新。
        self._ring = np.zeros((FEAT_RING_BLOCKS, BLOCK_FRAMES, STREAM_CH),
                              dtype=np.float32)
        self._w = 0
        self._r = 0
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._seq = 0
        self._stop = threading.Event()
        # 分析窗频带 bin 边界（FEAT_WIN 点 rfft 预计算）
        freqs = np.fft.rfftfreq(FEAT_WIN, 1.0 / STREAM_SR)
        self._bin_edges = [np.searchsorted(freqs, (lo, hi)) for lo, hi in FEAT_BANDS]
        self._flux_bins = (int(np.searchsorted(freqs, SPEC_FMIN)),
                           int(np.searchsorted(freqs, FEAT_FLUX_FMAX)))
        self._win = np.hanning(FEAT_WIN).astype(np.float32)
        # 持久滑动窗：跨步累积单声道样本，恒定取最近 FEAT_WIN 个真实样本。
        # ⚠ 不能用"本步 drain 多少就补零到窗长"——每步到达块数 1~2 交替，
        #   补零比例跳变 → 频谱泄漏振荡 → 稳态音乐被误判成连续 onset
        #   （R78f 探针实测 5s 和弦误报 27 次，改持久窗后归零）。
        self._acc = np.empty(0, dtype=np.float32)
        self._prev_mag: np.ndarray | None = None
        self._flux_hist: "deque[float]" = deque(maxlen=FEAT_FLUX_HIST)
        self._prev_flux = 0.0
        self._last_onset_t = 0.0
        # R82b 频带分源 onset：每频带独立的 flux 历史/上一帧谱/上一帧 flux/onset
        # 时刻（同全局 onset 同参数：median 自适应阈值 + 上升沿 + 响度门 + gap）。
        # 三源异步是前端水面"碰撞随机化"的根本（§17/R82b）——同一拍三频带
        # 不同时过阈 + 前端异质冷却 → 触发相位天然散开。
        self._b_hist = [deque(maxlen=FEAT_FLUX_HIST) for _ in range(3)]
        self._b_prev_mag: list[np.ndarray | None] = [None, None, None]
        self._b_prev_flux = [0.0, 0.0, 0.0]
        self._b_last_onset = [0.0, 0.0, 0.0]
        # R83b L1 短期语义：单声道环形缓冲（最近 ~3s），供 YAMNet 短期追踪取
        # 音频（研究文档 §18）。RT 内仅 mean+memcpy（1024×2 求均值 ≈ µs 级，
        # 仍属零分配纪律），读取在 mood 线程（非 RT）。
        self._mono_cap = int(STREAM_SR * 3.0)
        self._mono = np.zeros(self._mono_cap, dtype=np.float32)
        self._mw = 0            # 单调写入计数（样本数）
        self._mono_reads = 0    # 上次读取时的 _mw（新鲜度：暂停后无新数据 → None）
        # autostart=False 供单测/verify 确定性手动步进（不与内部线程双步进竞争）
        self._thread = (threading.Thread(target=self._loop, daemon=True,
                                         name="feat-analyzer")
                        if autostart else None)
        if self._thread is not None:
            self._thread.start()

    # RT 回调线程调用：把"即将送出"的混音块拷入预分配环 slot（纯 memcpy，零分配）
    def feed(self, mixed: np.ndarray) -> None:
        if not self.enabled:
            return
        try:
            slot = self._ring[self._w % FEAT_RING_BLOCKS]
            n = mixed.shape[0] if mixed.shape[0] <= BLOCK_FRAMES else BLOCK_FRAMES
            slot[:n] = mixed[:n]
            # R83b：单声道环同步写入（mean 是唯一的非纯 memcpy 运算，µs 级）
            mono = mixed[:n].mean(axis=1)
            cap = self._mono_cap
            mstart = self._mw % cap
            first = min(n, cap - mstart)
            self._mono[mstart:mstart + first] = mono[:first]
            if n > first:
                self._mono[:n - first] = mono[first:]
            self._mw += n
            self._w += 1
        except Exception:  # noqa 环形缓冲异常不干扰出声
            pass

    # 特征线程：30Hz 节拍出帧（落后不追帧，宁可丢帧保实时）
    def _loop(self) -> None:
        interval = 1.0 / FEAT_HZ
        while not self._stop.is_set():
            if not self.enabled:
                self._drain()          # 关闭态丢弃旧块 → 重开后立即用新数据
                self._stop.wait(0.1)
                continue
            t0 = time.perf_counter()
            try:
                self._step()
            except Exception:  # noqa 分析失败不干扰出声
                pass
            delay = interval - (time.perf_counter() - t0)
            self._stop.wait(delay if delay > 0 else 0.001)

    def _drain(self) -> None:
        # 关闭态：写读指针对齐丢弃积压 → 重开后立即用新数据（零分配）
        self._r = self._w

    def _step(self) -> None:
        w = self._w
        r = self._r
        if w - r > FEAT_RING_BLOCKS:      # 消费侧 clamp：落后超容量只保最新
            r = w - FEAT_RING_BLOCKS
        if r >= w:
            return
        n = w - r
        start = r % FEAT_RING_BLOCKS
        if start + n <= FEAT_RING_BLOCKS:  # 环内连续段（大多数情况，零拷贝视图）
            blocks = self._ring[start:start + n]
        else:                              # 环回绕段（本线程内拷贝一次，RT 外）
            first = FEAT_RING_BLOCKS - start
            blocks = np.concatenate((self._ring[start:],
                                     self._ring[:n - first]), axis=0)
        self._r = w
        x_new = blocks[0] if n == 1 else blocks.reshape(-1, STREAM_CH)
        # 单声道 → 追加进持久滑动窗 → 恒定取最近 FEAT_WIN 个真实样本
        mono = np.asarray(x_new.mean(axis=1), dtype=np.float32)
        self._acc = np.concatenate((self._acc, mono))[-FEAT_WIN:]
        if self._acc.shape[0] < 512:
            return                       # 流刚启动样本不足，不出帧
        x = self._acc                    # 窗长 ≤ FEAT_WIN（右侧=最新样本）
        # 响度（与 SpectrumAnalyzer 同配方：-70..+10 dB → 0..1）
        total = float(np.sqrt(np.mean(x * x)))
        loud = max(0.0, min(1.0, (20.0 * np.log10(total + 1e-9) + 70.0) / 60.0))
        # 预热期窗长 < FEAT_WIN：右对齐切窗（最新样本贴右缘；bin 数随窗长变，
        # 下方 flux 的 shape 一致性守卫会跳过窗长不足帧的差分 → 预热期无 onset）
        mag = np.abs(np.fft.rfft(x * self._win[-x.shape[0]:]))
        # 三频带：shape² × loud（视觉尺度与 16 段频谱一致）
        band_rms = []
        for lo, hi in self._bin_edges:
            seg = mag[lo:hi]
            band_rms.append(float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0)
        denom = max(float(np.sqrt(np.mean(np.square(band_rms)))), 1e-9)
        bands = [max(0.0, min(1.0, (v / denom) ** 2 * loud)) for v in band_rms]
        # 谱通量（帧内按谱峰归一 → 与音量无关的音色变化率；静音帧=0）
        lo, hi = self._flux_bins
        seg = mag[lo:hi]
        peak = float(seg.max()) if seg.size else 0.0
        norm = (seg / peak) if peak >= 1e-6 else np.zeros_like(seg)
        flux = 0.0
        if self._prev_mag is not None and self._prev_mag.shape == norm.shape:
            flux = float(np.mean(np.maximum(0.0, norm - self._prev_mag)))
        self._prev_mag = norm.copy()
        # onset：自适应阈值（median 历史，对密集/稀疏节拍都自校准）
        #   + 上升沿（flux ≥ 上帧 → 只在冲高的上升段触发，衰减尾波不重复计数）
        #   + 响度门 + 最小间隔。
        now = time.monotonic()
        self._flux_hist.append(flux)
        if len(self._flux_hist) >= 10:
            thr = float(np.median(self._flux_hist)) * FEAT_ONSET_RATIO \
                + FEAT_ONSET_DELTA
        else:
            thr = 0.05                    # 预热期保守阈值
        onset = (flux > thr and flux >= self._prev_flux
                 and loud >= FEAT_ONSET_MIN_LOUD
                 and (now - self._last_onset_t) >= FEAT_ONSET_GAP)
        if onset:
            self._last_onset_t = now
        self._prev_flux = flux
        # R82b 频带分源 onset：每频带独立谱通量 + 自适应阈值（同全局同参数，
        # 三频带各自的 median 历史自校准；前端按 ob/om/oh 三计数器消费）
        band_on = [False, False, False]
        for bi in range(3):
            lo2, hi2 = self._bin_edges[bi]
            seg2 = mag[lo2:hi2]
            pk2 = float(seg2.max()) if seg2.size else 0.0
            nrm2 = (seg2 / pk2) if pk2 >= 1e-6 else np.zeros_like(seg2)
            f2 = 0.0
            pm = self._b_prev_mag[bi]
            if pm is not None and pm.shape == nrm2.shape:
                f2 = float(np.mean(np.maximum(0.0, nrm2 - pm)))
            self._b_prev_mag[bi] = nrm2.copy()
            self._b_hist[bi].append(f2)
            if len(self._b_hist[bi]) >= 10:
                thr2 = float(np.median(self._b_hist[bi])) * FEAT_ONSET_RATIO \
                    + FEAT_ONSET_DELTA
            else:
                thr2 = 0.05
            band_on[bi] = (f2 > thr2 and f2 >= self._b_prev_flux[bi]
                           and loud >= FEAT_ONSET_MIN_LOUD
                           and (now - self._b_last_onset[bi]) >= FEAT_ONSET_GAP)
            if band_on[bi]:
                self._b_last_onset[bi] = now
            self._b_prev_flux[bi] = f2
        self._seq += 1
        frame = {
            "seq": self._seq,
            "t": round(time.time(), 3),
            "rms": round(loud, 4),
            "bass": round(bands[0], 4),
            "mid": round(bands[1], 4),
            "high": round(bands[2], 4),
            "flux": round(flux, 4),
            "onset": bool(onset),
            "ob": bool(band_on[0]),
            "om": bool(band_on[1]),
            "oh": bool(band_on[2]),
        }
        with self._lock:
            self._latest = frame

    # 非 RT 线程调用：取最近一帧特征（副本，防竞态）
    def read(self) -> dict | None:
        with self._lock:
            return dict(self._latest) if self._latest else None

    def mono_write_count(self) -> int:
        """单声道环写入计数（暂停时冻结 → 读取方可判新鲜度）。"""
        return self._mw

    def read_recent_mono(self, seconds: float) -> np.ndarray | None:
        """R83b 非 RT：取最近 seconds 秒单声道音频（stream SR float32）。

        新鲜度门：自上次读取后无新写入（暂停/停播）→ None——L1 拿旧音频
        当新语义 = 冻结轴值，必须避免。样本不足 / 未积累 → None。
        """
        if self._mw == self._mono_reads:
            return None
        self._mono_reads = self._mw
        n = min(int(seconds * STREAM_SR), self._mono_cap, self._mw)
        if n <= 0:
            return None
        start = (self._mw - n) % self._mono_cap
        if start + n <= self._mono_cap:
            return self._mono[start:start + n].copy()
        first = self._mono_cap - start
        return np.concatenate((self._mono[start:], self._mono[:n - first]))

    def close(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# 音源槽
# ---------------------------------------------------------------------------

class _BaseSource:
    """音源槽基类。回调线程消费 queue 并向 out_pos 累计"已真正送出的帧"。

    约定：playing_flag=True 表示"有内容在播，回调应持续取块"；当回调发现
    out_pos 已达 total 且队列空，会自己把 playing_flag 置 False（自动结束）。
    控制线程切歌/暂停/停止时也会主动置 False 并清队列。
    """

    def __init__(self, qdepth: int) -> None:
        self.queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=qdepth)
        self.out_pos = 0
        self.total = 0
        self.gain = 1.0
        self.playing_flag = False
        # ★ 2026-09-19：RT 侧"消费者取不到块(本帧补静音)"单调计数 = **卡顿本体**。
        #   整数自增、RT 零开销；monitor 线程读它做节流诊断。
        self._starve = 0
        # ★ 2026-09-19（R119-2）：最近一次取出的**块长**（-1 = 还没取出过）。供 RT 侧
        #   比较"块长 vs 回调帧数"—— 不等时 `pump` 会**静默**尾部补静音或丢弃余量。
        self._last_blk_len = -1
        # ★ R119-2b：队列被清空的次数。播放中多出一次 ⇒ 内容跳变（现有计数器看不见）
        self._clear_n = 0
        self.eq: ParametricEQ | None = None

    def clear(self) -> None:
        self._clear_n += 1          # ★ R119-2b：计数（诊断用；RT 侧只读）
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def _at_end(self) -> bool:
        return self.total > 0 and self.out_pos >= self.total and self.queue.qsize() == 0

    # 回调线程调用：从队列取一块并写入 mixed（不足补静音）。
    # 返回 True 表示本槽仍 active（还应继续被拉取）。
    def pump(self, mixed: np.ndarray, frames: int) -> bool:
        if not self.playing_flag:
            return False
        try:
            blk = self.queue.get_nowait()
        except queue.Empty:
            # ★ 2026-09-19：本帧消费者**没拿到任何数据** → 本槽直接补静音。
            #   这就是"卡"的本体（RT 安全：整数自增，零分配零锁）。
            self._starve += 1
            # 队列空但未到 total：由生产者补块（music）或属短暂饥饿，保持 active
            if self._at_end():
                self.playing_flag = False
                return False
            return self.playing_flag
        n = min(blk.shape[0], frames)
        self._last_blk_len = int(blk.shape[0])   # ★ R119-2：供 RT 侧判"块长是否一致"
        mixed[:n] += blk[:n] * self.gain
        self.out_pos += n
        if self._at_end():
            self.playing_flag = False
            return False
        return True


class MusicSource(_BaseSource):
    """流式歌曲源：持有一个 sf.SoundFile，生产者线程按需读块入队。

    R81 重采样重构（爆音根因修复）：旧实现把每 1024 源帧独立
    resample_poly → 非 44.1k 源的块长 ≠ BLOCK_FRAMES（48k 源实测
    941/1024）→ pump 每块尾部插 83 帧静音，每 21ms 断一次 = 周期性
    「噪点爆炸声」（且只炸非 44.1k 的歌，用户实测歌间差异）。
    改为流式重叠重采样（overlap-save，双端可靠区约束）：
      · 每次读 need=L+half 源帧（down 整数倍）→ resample_poly(tail+块)
        → 只产出滤波窗口完全落在真实数据内的输出段
        [m_lo, buf_len-half 对应帧] → 输出域缓冲 _obuf；
      · 全局输出网格连续：N（下一产出全局输出索引）与
        M=buf起点坐标×up/down（整数，因 buf 起点恒为 down 整数倍）
        → 产出起点 m_start = max(m_lo, N-M)，相位自动对齐（有理时钟）；
      · _read_block 恒吐恰好 BLOCK_FRAMES 帧块（EOF 末块补零）；
      · total/seek/对齐读头全部换算到输出域（out_pos 语义不变，
        顺带修复 48k 歌 total 虚高 8.8% 导致的进度/结束判定漂移）。
    """

    RESAMPLE_CHUNK = 16384  # 源域每次重采样的基础新样本数（≈0.34s@48k）

    def __init__(self) -> None:
        super().__init__(MUSIC_Q_DEPTH)
        self._file: sf.SoundFile | None = None
        self._path: str | None = None
        self._src_sr = STREAM_SR
        self._up = 1                # 有理重采样 up/down（44.1k 源 = 1/1 直读）
        self._down = 1
        self._half = 0              # resample_poly 默认滤波器半长（源域帧）
        self._tail: np.ndarray | None = None   # 源域重叠尾 (t,2) float32
        self._tail_len = 0                     # tail 帧数（down 整数倍）
        self._obuf = np.empty((0, STREAM_CH), dtype=np.float32)  # 输出域缓冲
        self._eof = False
        self._N = 0                 # 下一产出的全局输出索引（相位连续锚）
        self._open_lock = threading.Lock()
        self._fill_lock = threading.Lock()
        self.eq = ParametricEQ(STREAM_SR)   # 默认 flat

    @property
    def path(self) -> str | None:
        return self._path

    def open(self, path: str) -> bool:
        with self._open_lock:
            self._close_locked()
            try:
                f = sf.SoundFile(path)   # 不能传 samplerate（对已有文件会报错）
            except Exception as e:  # noqa
                logger.error("MusicSource 打开失败 %s: %s", path, e)
                return False
            self._file = f
            self._path = path
            self._src_sr = int(f.samplerate)
            if self._src_sr != STREAM_SR:
                from fractions import Fraction
                frac = Fraction(STREAM_SR, self._src_sr).limit_denominator(1000)
                self._up, self._down = frac.numerator, frac.denominator
                # 与 resample_poly 默认滤波器同参：半长 = 10·max(up,down)；
                # tail 取 2×半长（向上取整到 down 整数倍，保持相位网格）
                self._half = 10 * max(self._up, self._down)
                self._tail_len = -(-2 * self._half // self._down) * self._down
            else:
                self._up, self._down, self._half, self._tail_len = 1, 1, 0, 0
            self.total = int(round(f.frames * self._up / self._down))  # 输出域总帧
            self._reset_resampler(0)
            self.out_pos = 0
            self.playing_flag = False
            self.clear()
            return True

    def _reset_resampler(self, n0: int = 0) -> None:
        """重置重采样会话。n0 = 会话起始的全局输出索引（seek/align 后 =
        源起点×up/down，恒整数因起点取整到 down 倍数）。"""
        self._tail = None
        self._obuf = np.empty((0, STREAM_CH), dtype=np.float32)
        self._eof = False
        self._N = int(n0)

    def _close_locked(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:  # noqa
                pass
            self._file = None
        self._path = None
        self.total = 0
        self.out_pos = 0
        self.playing_flag = False
        self._reset_resampler()
        self.clear()

    def close(self) -> None:
        with self._open_lock:
            self._close_locked()

    def start_play(self, arm: bool = True) -> None:
        """从头播放：读头归零、**清队列**，再把 playing 立起来。

        ★ 2026-09-19（R119-2e）**顺序修复**。原实现是「先 `playing_flag=True`，
        再 `clear()`」⇒ 存在一段"flag 已真、队列却空"的窗口，窗口里每一次 RT 回调
        都 `pump()` 取不到块 ⇒ **补静音**（`_starve` 直证）。
        真机实测（用户机）：`starve=89`；本地按"换曲后 400ms 读不出数据"复现：
        6 次换曲 `starve=101`（16.8/次 ≈ **391ms 散碎静音**）= "开头卡几下"。
        现在**先清队列**，再立 flag，窗口消失。

        `arm=False`：只归零+清队列、**不**立 flag —— 供 `play()` 走
        「清 → 同步预填 → 最后立 flag」的顺序（`_fill_loop` 也以该 flag 为闸门）。"""
        with self._open_lock:
            if self._file is None:
                return
            try:
                self._file.seek(0)
            except Exception:  # noqa
                pass
            self.out_pos = 0
            self._reset_resampler(0)
            self.clear()                    # ★ 先清
            self.playing_flag = bool(arm)   # ★ 后立

    def seek_to(self, frame: int) -> None:
        """跳转到绝对帧（输出域 STREAM_SR 帧）；读头移动、out_pos 归位。
        不改变 playing_flag（由调用方定）。"""
        with self._open_lock:
            if self._file is None:
                return
            if self._up == 1:
                src_frame = int(frame)
            else:
                # 输出帧 → 源帧，向下取整到 down 整数倍（保持重采样相位对齐）
                src_frame = (int(round(int(frame) * self._down / self._up))
                             // self._down) * self._down
            try:
                self._file.seek(src_frame)
            except Exception as e:  # noqa
                logger.warning("MusicSource seek 失败: %s", e)
                return
            self.out_pos = int(frame)
            self.clear()
            self._reset_resampler(src_frame * self._up // self._down)

    def align_read_head(self) -> None:
        """把读头回卷到"已送出的最后一帧"（pause 后 resume 无缝用）。"""
        with self._open_lock:
            if self._file is None:
                return
            try:
                if self._up == 1:
                    self._file.seek(int(self.out_pos))
                    return
                # 输出帧 → 源帧（down 整数倍）+ 预读重叠尾：resume 后
                # 重采样滤波连续（否则冷启动半长瞬态 = resume 爆音）
                src_frame = (int(round(self.out_pos * self._down / self._up))
                             // self._down) * self._down
                t0 = max(0, src_frame - self._tail_len)
                tail = None
                if src_frame - t0 > 0:
                    self._file.seek(t0)
                    tx = self._file.read(src_frame - t0, dtype="float32",
                                         always_2d=True)
                    if tx is not None and tx.shape[0] == src_frame - t0:
                        tail = tx
                self._file.seek(src_frame)
                self._tail = tail
                self._obuf = np.empty((0, STREAM_CH), dtype=np.float32)
                self._eof = False
                self._N = src_frame * self._up // self._down
            except Exception as e:  # noqa
                logger.warning("MusicSource align 失败: %s", e)

    def _fill_obuf(self, n: int) -> bool:
        """重采样路径：读 need=L+half 源帧（down 整数倍）→ resample_poly(tail+块)
        → 只产出滤波窗口完全落在真实数据内的输出段（左端瞬态由 tail 覆盖、
        右端瞬态留待下次 call 以重叠重算）→ 追加输出域缓冲。
        返回是否读到新数据。"""
        with self._open_lock:
            if self._file is None:
                return False
            try:
                need = max(int(n) + self._half, self._down)
                need = -(-need // self._down) * self._down   # down 整数倍
                p0 = self._file.tell()                        # 本次读取起始源坐标
                x = self._file.read(need, dtype="float32", always_2d=True)
            except Exception as e:  # noqa
                logger.warning("MusicSource read 失败: %s", e)
                return False
        if x is None or x.shape[0] == 0:
            self._eof = True
            return False
        tail = self._tail
        buf = x if tail is None or tail.shape[0] == 0 \
            else np.concatenate((tail, x), axis=0)
        tlen = tail.shape[0] if tail is not None else 0
        buf_start = p0 - tlen              # buf 全局源坐标起点（down 整数倍）
        y = _resample_to_stream(np.ascontiguousarray(buf, dtype=np.float32),
                                self._src_sr)
        # 可靠产出区（overlap-save）：输出 m 的滤波窗口 [m·d/u ± half]
        # 须完全落在 buf 的真实数据内。左端由 tail（≥2×half）保证；
        # 右端预留 half 由下次 call 重算（EOF 例外：文件尾允许淡出）。
        m_lo = 0 if tail is None or tail.shape[0] == 0 \
            else int(-(-self._half * self._up // self._down))   # ceil(half·u/d)
        m_hi = y.shape[0] if self._eof or x.shape[0] < need \
            else int((buf.shape[0] - self._half) * self._up // self._down)
        # 全局输出网格连续：M = buf 起点的全局输出索引（整数，因 buf 起点
        # 为 down 整数倍）；N = 下一要产出的全局输出索引。
        M = buf_start * self._up // self._down
        m_start = max(m_lo, self._N - M, 0)
        m_end = min(max(m_hi, m_start), y.shape[0])
        if m_end > m_start:
            self._obuf = np.concatenate((self._obuf, y[m_start:m_end]), axis=0) \
                if self._obuf.shape[0] else y[m_start:m_end]
            self._N = M + m_end
        if not self._eof and x.shape[0] >= need:
            self._tail = buf[-self._tail_len:]   # 源域重叠尾（真实音频）
        else:
            self._eof = True
            self._tail = None
        return True

    def _read_block(self, n: int) -> np.ndarray | None:
        if self._up == 1 and self._down == 1:
            # 44.1k 源直读（read(n) 块长恰 = n，EOF 末块可短）
            # ⚠ 判据必须是 up==down==1：up=1/down=2（88.2k）也走重采样路径
            with self._open_lock:
                if self._file is None:
                    return None
                try:
                    x = self._file.read(n, dtype="float32", always_2d=True)
                except Exception as e:  # noqa
                    logger.warning("MusicSource read 失败: %s", e)
                    return None
            if x is None or x.shape[0] == 0:
                return None
            x = _to_stereo_float(x, STREAM_SR)
            if self.eq is not None:
                x = self.eq.process(x)
            return x
        # 重采样路径：从输出域缓冲取恰好 BLOCK_FRAMES（EOF 末块补零——
        # pump 依赖块长恒等于回调帧数，短块=插静音=爆音）
        while self._obuf.shape[0] < BLOCK_FRAMES and not self._eof:
            self._fill_obuf(max(n, self.RESAMPLE_CHUNK))
        if self._obuf.shape[0] == 0:
            return None
        blk = self._obuf[:BLOCK_FRAMES]
        self._obuf = self._obuf[BLOCK_FRAMES:]
        if blk.shape[0] < BLOCK_FRAMES:
            blk = np.concatenate(
                (blk, np.zeros((BLOCK_FRAMES - blk.shape[0], STREAM_CH),
                               dtype=np.float32)))
        if self.eq is not None:
            blk = self.eq.process(blk)
        return np.ascontiguousarray(blk, dtype=np.float32)

    def fill(self, n: int) -> bool:
        """生产者调用：尽力把队列填满。返回是否仍有更多数据（False=已到 EOF）。"""
        more = True
        with self._fill_lock:
            while True:
                if self.queue.full():
                    break
                blk = self._read_block(n)
                if blk is None:
                    more = False
                    break
                try:
                    self.queue.put_nowait(blk)
                except queue.Full:
                    break
        return more


class FixedSource(_BaseSource):
    """一次性音源（TTS/音效）：set() 时整段载入并分块入队。"""

    def __init__(self, qdepth: int) -> None:
        super().__init__(qdepth)
        self._lock = threading.Lock()

    def set(self, path: str | Path) -> bool:
        with self._lock:
            self.clear()
            self.playing_flag = False
            try:
                f = sf.SoundFile(str(path))
                try:
                    src_sr = int(f.samplerate)
                    data = f.read(dtype="float32", always_2d=True)
                finally:
                    f.close()
            except Exception as e:  # noqa
                logger.error("FixedSource 载入失败 %s: %s", path, e)
                self.total = 0
                return False
            data = _to_stereo_float(data, src_sr)
            self.total = int(data.shape[0])
            self.out_pos = 0
            blocks = [data[i:i + BLOCK_FRAMES]
                      for i in range(0, data.shape[0], BLOCK_FRAMES)]
            # 先置 playing_flag=True，回调才会并发消费，避免队列满后死等。
            self.playing_flag = True
            for b in blocks:
                while True:
                    try:
                        self.queue.put_nowait(b)
                        break
                    except queue.Full:
                        time.sleep(0.002)
            return True


# ---------------------------------------------------------------------------
# 软混音器：唯一 PortAudio 输出流 + 回调
# ---------------------------------------------------------------------------

class SoftMixer:
    def __init__(self, device: int | None = None) -> None:
        self.device = device
        # ★ 2026-09-18：默认输出端点名快照。device=None 时跟随系统默认；启动后才连
        #   蓝牙耳机 → 默认端点变化 → follow_default_device() 重建流跟随（否则流永远
        #   钉在启动时的旧设备 = 蓝牙无声 / 录屏（跟随默认端点）录不到本流）。
        self._device_name: str | None = None
        self._last_reinit = 0.0     # ★ PortAudio 重枚举节流戳（仅空闲期刷新设备表）
        # ★ 2026-09-19（R119-2g）：**退避**用的当前间隔 + 累计重查次数。
        #   起因：慢路径原来过了 5s 就无条件重建流（"重建完才比设备名"）⇒ 用户真机
        #   日志里 45s 内白拆 9 次（~6.2s 一次）；探针 60s 模拟得 13 次。
        self._dev_reinit_gap = 5.0
        self._dev_reinit_n = 0
        self.stream: sd.OutputStream | None = None
        self.music = MusicSource()
        self.tts = FixedSource(FIXED_Q_DEPTH)
        self.fx = FixedSource(FIXED_Q_DEPTH)
        # 频谱分析器（音频响应壁纸）：默认关闭；PlayerEngine.set_spectrum_enabled
        # 在有 WS 客户端订阅时打开。close() 时一并停。
        self.spec = SpectrumAnalyzer()
        # P2 实时特征帧（Emotional Water 节拍通道）：与 spec 同一 enabled 闸门
        # （有 WS 客户端才开）；RT 回调只入环形缓冲，分析在独立线程。
        self.feat = FeatureAnalyzer()
        # R80 爆音确诊埋点：PortAudio 回调 status 标志（bit0=input overflow /
        # bit1=output underflow）。RT 内只做整数 OR（零分配零锁），monitor 线程
        # 读+节流日志——「噪点爆炸声」听感即 output underflow 的直接证据。
        self._pa_flags = 0
        self._lock = threading.Lock()
        # ★ 2026-09-19：**回调内不再分配**。原实现每次回调 `np.zeros((frames,2))`
        #   ⇒ 每秒 ~43 次分配 + GC 压力，而回调预算只有 23ms。改为预分配后复用。
        #   安全性：下游 `spec.feed`/`feat.feed` 均"立即计算"或"拷贝入环"，不留引用。
        self._mixbuf = None
        self._rt_boosted = False     # RT 线程优先级是否已拉高（见 _callback 首行）
        self._rt_mmcss = False       # 是否成功注册进 Windows MMCSS "Pro Audio"
        # ★ 2026-09-19（R119-2）**RT 侧量化埋点**：全为整数/浮点自增，零分配零锁，
        #   可安全在回调里写；只在诊断时读。目的 = 把"还有一点点卡"变成可读的数。
        #   · cb_us_max/cb_us_last ：回调**整体**耗时（µs）。预算 1024/44100 ≈ **23.2ms**
        #     ⇒ max 逼近 20ms = 回调本身太慢；**远小于 1ms = 卡顿不在回调里**。
        #   · rt_prio ：回调首帧**读回**的真实线程优先级（-999 = 读不到）。
        #   · uf_count/uf_after_play ：underflow 次数 + 每次距最近一次 play 的秒数
        #     ⇒ 直接检验「卡在起播后 20s 内」这个经验判断。
        #   · blk_short/blk_long/cb_frames_bad ：`pump` 的**静默空隙路径**
        #     （块长 ≠ 回调帧数 ⇒ 尾部无声补静音 / 多余数据被丢）—— 原先无任何计数器。
        self._cb_us_max = 0.0
        self._cb_us_last = 0.0
        # ★ R119-2m：回调整体的三个阶段各自的最大耗时（µs）—— 用来钉死"回调体 11 秒"
        #   到底卡在取块 / 输出 / 分析，还是**量测污染**（跨 `mark_play_start` 重置的调用）。
        self._cb_t_pump_max = 0.0
        self._cb_t_out_max = 0.0
        self._cb_t_feed_max = 0.0
        self._cb_n = 0
        self._rt_prio = -999
        self._uf_count = 0
        self._uf_after_play = []
        self._play_t0 = 0.0
        self._blk_short = 0
        self._blk_long = 0
        self._cb_frames_bad = 0
        # ★ R119-2b：内容层（同样被所有既有计数器忽略）
        #   · clip_n   ：发生硬削波（|x|>1 被 np.clip 削平）的回调数 ⇒ 听感"毛/破"
        #   · peak_max ：观察到的混音峰值 ⇒ 增益裕量（>1.0 即已在削）
        self._clip_n = 0
        self._peak_max = 0.0
        # ★ R119-2c：**进程级停顿**观测（用户："界面水面随声音一起卡，集中在前 20 秒"）
        #   · stall_n / stall_max : 看门狗超额次数 + 最大超额秒数（>0.15s 记一次）
        #   · stall_at            : 每次停顿**距起播的秒数** ⇒ 与前 20 秒对表
        #   · cb_gap_max          : 相邻回调起点最大间隔（ms；期望 = frames/sr ≈ 23.2ms）
        #                          即使 PortAudio 没报 underflow，也能看出回调被推后多久
        self._t0 = time.monotonic()
        self._cb_last_t = 0.0
        self._cb_gap_max = 0.0
        self._stall_stop = threading.Event()
        self._stall_started = False
        self._stall_last = time.monotonic()
        self._stall_n = 0
        self._stall_abs = []          # ★ 2026-09-21：[(绝对时刻, 超额秒数)] 供停顿点名
        self._stall_max = 0.0
        self._stall_at = []

    def open(self) -> bool:
        with self._lock:
            if self.stream is not None:
                return True
            try:
                _kw = dict(samplerate=STREAM_SR, channels=STREAM_CH,
                           dtype=STREAM_DTYPE, blocksize=BLOCK_FRAMES,
                           callback=self._callback, device=self.device)
                _extra = None
                # ★★ 兜底副本：**不含**下面注入的 WASAPI device 覆盖。
                #   2026-09-19 事故根因之二：旧代码把 device 直接写进 _kw，导致
                #   "回退到旧行为"那条也在用 WASAPI 设备 ⇒ 兜底形同虚设。
                _kw_pristine = dict(_kw)
                if self.device is None:
                    # ★ 2026-09-18：优先走 Windows **WASAPI 共享模式**的默认输出端点。
                    #   旧实现用 sounddevice 默认 host API（Windows = MME），两个后果：
                    #   ① 蓝牙耳机是 **WASAPI** 默认端点，而 MME 默认常是内置扬声器 ⇒
                    #      "先开 app 后连蓝牙耳机"永远不出声（两套 API 默认设备不同）；
                    #   ② 录屏/直播走 **WASAPI loopback** 抓默认渲染端点，本流在 MME ⇒
                    #      录不到；且 MME 重采样质量低 ⇒ "声音像低分辨率"。
                    #   ⚠ 但 WASAPI 共享只吃端点原生采样率 ⇒ 必须 auto_convert（见上）。
                    _di, _extra = self._wasapi_default_out()
                    if _di is not None:
                        _kw["device"] = _di

                def _open(extra, use_high, kw):
                    kk = dict(kw)
                    if extra is not None:
                        kk["extra_settings"] = extra
                    return sd.OutputStream(latency="high", **kk) if use_high \
                        else sd.OutputStream(**kk)

                try:
                    # ① WASAPI 共享（auto_convert）+ latency=high
                    self.stream = _open(_extra, True, _kw)
                    logger.info("音频输出流已打开（WASAPI共享=%s，device=%s，blocksize=%d）",
                                _extra is not None, _kw.get("device"), BLOCK_FRAMES)
                except Exception as _le:  # noqa
                    logger.info("①WASAPI(auto_convert)失败，回退②同设备直连：%s", _le)
                    try:
                        self.stream = _open(None, True, _kw)
                    except Exception as _le2:  # noqa
                        logger.info("②同设备直连失败，回退③PortAudio 默认设备(旧行为)：%s", _le2)
                        try:
                            self.stream = _open(None, True, _kw_pristine)
                        except Exception as _le3:  # noqa
                            logger.info("③旧行为失败，最后兜底：%s", _le3)
                            self.stream = _open(None, False, _kw_pristine)
                self.stream.start()
            except Exception as e:  # noqa
                logger.warning("音频输出流打开失败（进入静默模式）: %s", e)
                try:
                    if self.stream is not None:
                        self.stream.close()
                except Exception:  # noqa
                    pass
                self.stream = None
                return False
            self._device_name = self._default_out_name()
            return True

    @staticmethod
    def _wasapi_default_out():
        """★ 2026-09-18：(device_index, extra_settings) —— Windows **WASAPI 共享模式**
        的默认输出端点；失败/非 Windows 返回 (None, None)（调用方回退默认 host API）。"""
        try:
            ha = sd.query_hostapis()
            wi = next(i for i, h in enumerate(ha) if "WASAPI" in (h.get("name") or ""))
            di = int(ha[wi].get("default_output_device", -1) or -1)
            if di < 0:
                return (None, None)
            try:
                # ★★ auto_convert=True 是**关键**（2026-09-19 事故修复）：
                #   WASAPI 共享模式只接受端点的**原生采样率**，不会替你换算 ——
                #   本机 Realtek 插孔原生 96000，硬要 44100 ⇒ PaErrorCode -9997
                #   "Invalid sample rate" ⇒ open() 失败 ⇒ 整站静默（已实测）。
                #   auto_convert 让 PortAudio/WASAPI 帮我们做 44100 ↔ 原生率 的换算。
                extra = sd.WasapiSettings(exclusive=False, auto_convert=True)
            except Exception:  # noqa 老版本 PortAudio 无该参数 → 逐级降级
                try:
                    extra = sd.WasapiSettings(exclusive=False)
                except Exception:  # noqa
                    extra = None
            return (di, extra)
        except Exception:  # noqa
            return (None, None)

    @staticmethod
    def _default_out_name() -> str | None:
        """当前**默认输出端点**名（用于检测"默认设备变了"）。
        ★ 2026-09-18：优先取 **WASAPI** 默认端点名 —— 与 open() 实际使用的端点同一个，
        否则（取 MME 名）会在"蓝牙耳机连上（WASAPI 默认变）"时检测不到、不重建流。"""
        try:
            ha = sd.query_hostapis()
            wi = next(i for i, h in enumerate(ha) if "WASAPI" in (h.get("name") or ""))
            di = int(ha[wi].get("default_output_device", -1) or -1)
            if di >= 0:
                return str(sd.query_devices(di).get("name") or "") or None
        except Exception:  # noqa
            pass
        try:
            info = sd.query_devices(kind="output")
            name = str(info.get("name") or "")
            return name or None
        except Exception:  # noqa
            return None

    def _reopen_stream(self, reinit: bool = False) -> None:
        """关当前输出流并按（可能已变化的）默认端点重开。
        reinit=True 时先 `sd._terminate()/_initialize()` 刷新 PortAudio 的**设备枚举
        快照** —— 后连接的蓝牙耳机只有 re-init 后才可见（PortAudio 只在 init 时枚举）。
        ⚠ 仅应在**空闲（未播放）**时用 reinit=True：会短暂关闭输出流。"""
        with self._lock:
            st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
            except Exception:  # noqa
                pass
            try:
                st.close()
            except Exception:  # noqa
                pass
        if reinit:
            try:
                sd._terminate()
                sd._initialize()
            except Exception as e:  # noqa
                logger.debug("PortAudio 重初始化失败: %s", e)
        self.open()          # device=None → 绑定（新的）系统默认

    def follow_default_device(self, allow_reinit: bool = False) -> bool:
        """★ 2026-09-18：跟随系统默认输出端点（典型：先开 app、后连蓝牙耳机）。
        ① 快路径：PortAudio 缓存的默认端点名变了 → 立即重建流；
        ② 慢路径（allow_reinit=True，**仅空闲期**调用）：PortAudio 重新枚举设备后再看
           默认端点是否变化 —— 后连的蓝牙只有 re-init 才出现在设备表里。
        返回 True = 已切换；device 显式指定者不动。"""
        if self.device is not None:
            return False
        name = self._default_out_name()
        if name and name != self._device_name:
            logger.info("输出设备变化：%s → %s，重建输出流跟随系统默认",
                        self._device_name, name)
            self._reopen_stream()
            return True
        if not allow_reinit:
            return False
        # ★ 2026-09-19（R119-2g）**退避**：旧实现过了 5s 闸门就**无条件**
        #   `_reopen_stream(reinit=True)`（= `sd._terminate()` + `_initialize()` +
        #   关流 + 重开），**重建完才去比设备名** ⇒ 设备没变也照样每 5~6s 白拆一次。
        #   实测：用户日志 45s 内 9 次（~6.2s/次）；探针 60s 模拟 **13 次**。
        #   代价：周期性关流 + 每轮一次设备枚举；且"待播/暂停时按播放"的时机可能
        #   正好撞上重建。⇒ 未变化就退避（5→10→20s 封顶），真变了立刻回 5s 快查档。
        #   蓝牙"自动跟上"不受影响：刚开 app 时正是 5s 快查档 ⇒ 最多等 5s。
        now = time.monotonic()
        if now - self._last_reinit < self._dev_reinit_gap:
            return False
        self._last_reinit = now
        before = self._device_name
        self._dev_reinit_n += 1
        self._reopen_stream(reinit=True)
        if self._device_name and self._device_name != before:
            logger.info("空闲期刷新：检测到新默认端点 %s，输出流已跟随", self._device_name)
            self._dev_reinit_gap = 5.0          # 真变了 → 回快查档
            return True
        self._dev_reinit_gap = min(20.0, self._dev_reinit_gap * 2.0)
        logger.debug("空闲期刷新：默认端点未变（%s），%.0fs 后再查（累计重查 %d 次）",
                     self._device_name, self._dev_reinit_gap, self._dev_reinit_n)
        return False

    def close(self) -> None:
        try:
            self._stall_stop.set()     # ★ R119-2c：停看门狗
        except Exception:  # noqa
            pass
        with self._lock:
            st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
            except Exception:  # noqa
                pass
            try:
                st.close()
            except Exception:  # noqa
                pass
        self.music.close()
        self.tts.clear()
        self.tts.playing_flag = False
        self.fx.clear()
        self.fx.playing_flag = False
        self.feat.close()

    def mark_play_start(self) -> None:
        """★ 2026-09-19（R119-2）：记下"本次起播"的时刻（**非 RT** 线程调用）。

        用途：`_callback` 里每次 underflow 都记一条"距起播多少秒" ⇒ 直接检验
        「卡顿集中在起播后 20s 内」这个长期经验判断，不必再靠回忆。"""
        self._play_t0 = time.monotonic()
        # ★ R119-2h：**按曲重置峰值**。此前这些 max 是"开机以来"的 ⇒ 启动期
        #   （CLAP / LLM 预热，实测一口气把 cb_us_max 顶到 **43 s**）之后，
        #   读数完全看不出当前这张唱片的情况。现在每张唱片从 0 起算。
        #   （累计型计数器 cb_n/starve/uf_count/q_clear 仍保持单调，便于跨曲对账。）
        self._cb_us_max = 0.0
        self._cb_gap_max = 0.0
        # ★ R119-2s：**先清 list、再清 max**。两者之间若被看门狗 tick 插入，会得到
        #   `_stall_max`=新值 而 `_stall_at`=[] 的**自相矛盾**读数（我据此误判过两次
        #   "本曲零停顿"）。按此顺序：插入的 tick 写入会被随后的 `_stall_max = 0.0`
        #   覆盖、而它的 append 落在**即将被丢弃**的旧列表上 ⇒ 两个字段保持一致。
        self._stall_at = []
        self._stall_max = 0.0
        # ★ R119-2n：分段 max **也要按曲重置**。本轮漏了 ⇒ `cb_t_feed_max` 一直被
        #   开机以来的最大值污染（读到 797ms，而本曲回调体其实只有 8ms），
        #   差点把"量测污染"误判成真卡。
        self._cb_t_pump_max = 0.0
        self._cb_t_out_max = 0.0
        self._cb_t_feed_max = 0.0
        # ★ R119-2p：GC 的"单次最大"也按曲重置（`n` 保持累计）—— 否则读到的 272.7ms
        #   可能来自 `gc.freeze()`（启动 25s 后）之前的时期，判断不了调参到底有没有用。
        try:
            _GC_STAT["max_ms"] = 0.0
            _GC_STAT["n_at_play"] = int(_GC_STAT["n"])
        except Exception:  # noqa
            pass
        self._start_stall_watch()      # ★ R119-2c：顺便拉起进程级停顿看门狗（幂等）

    def _stall_tick(self, period: float = 0.05) -> float:
        """★ R119-2c：看门狗**单次节拍**（抽成方法 ⇒ 可单测、可"见红"）。

        返回本拍"超额秒数" = 实际间隔 − 期望间隔。>0.15s 即记一次停顿，
        并记下**距起播的秒数**（用于与"前 20 秒"对表）。"""
        now = time.monotonic()
        over = (now - self._stall_last) - period
        self._stall_last = now
        if over > 0.15:
            self._stall_n += 1
            # ★ R119-2z：往**同一份**停顿栈文件写一行标记 ⇒ 能把"卡"与"栈"对齐
            #   （否则 1.9MB 的 dump 里分不清哪几次是"卡住时拍的"）。
            if _stall_dump_on[0]:
                try:
                    with open(_STALL_DUMP_PATH, "a", encoding="utf-8") as _f:
                        _f.write("\n### STALL over=%.3fs  stall_n=%d  t=%.1f\n"
                                 % (over, self._stall_n, now))
                except Exception:  # noqa
                    pass
            # ★ R119-5b：**真超阈值才取证**。正常的长任务（LLM / TTS）不会让
            #   本看门狗单拍超额（它只测"这一拍比 50ms 晚了多少"）⇒ 零噪声。
            if over >= _STALL_DUMP_TRIGGER_S:
                stall_dump_burst("over=%.2fs n=%d" % (over, self._stall_n))
            if over > self._stall_max:
                self._stall_max = over
            # ★ R119-2y：改成**滚动**（原来 16 条上限 ⇒ 满了之后 `stall_at` 不再增长、
            #   而 `stall_max` 继续更新 ⇒ 两个字段自相矛盾，我据此误判过"本曲零停顿"）。
            ref = self._play_t0 or self._t0
            self._stall_at.append(round(now - ref, 2))
            del self._stall_at[:-16]
            # ★ 2026-09-21：同时记**绝对时刻 + 超额秒数** —— 这是与 `_HEAVY_RING`
            #   （单调时钟）对上的唯一键；只有"距起播秒数"是关联不上的（见 attribute_stalls）。
            self._stall_abs.append((now, float(over)))
            del self._stall_abs[:-16]
        return over

    def _stall_watch_loop(self) -> None:
        """★ R119-2c：进程级停顿看门狗（20Hz，daemon）。

        ⚠ 优先级**刻意不降级**（NORMAL）：若连 NORMAL 线程都被饿死 150ms 以上，
        那本身就说明"整机/整进程被占满"——正是我们要抓的现象。降级会让它假报警。"""
        try:
            _set_self_priority(0)          # NORMAL：不要被我们自己的降级策略影响读数
        except Exception:  # noqa
            pass
        self._stall_last = time.monotonic()
        while not self._stall_stop.wait(0.05):
            self._stall_tick(0.05)

    def _start_stall_watch(self) -> None:
        """幂等启动看门狗（从 `mark_play_start` 拉起；只在前台播放路径需要它）。"""
        if self._stall_started:
            return
        self._stall_started = True
        threading.Thread(target=self._stall_watch_loop,
                         daemon=True, name="stall-watch").start()

    # PortAudio status 位（自定映射，与 monitor 日志语义一致）
    PA_INPUT_OVERFLOW = 1     # paInputOverflow
    PA_OUTPUT_UNDERFLOW = 2   # paOutputUnderflow（爆音/断流元凶）
    PA_INPUT_UNDERFLOW = 4    # paInputUnderflow

    def _callback(self, outdata, frames, time_info, status) -> None:  # RT 线程
        # R80 爆音确诊埋点：status 是 sounddevice.CallbackFlags（__bool__=
        # 底层 _flags 非零 → 正常回调恒 False 零开销，异常态才进本分支）。
        # ⚠ CallbackFlags 无公开 .flags int 属性（真机 R80b 回归实证），
        #   int()/getattr(flags) 都会 TypeError → 必须走公开 bool 属性。
        # ★ R119-2c：回调**间隔**（起点到起点）。PortAudio 不报 underflow 时，
        #   这个数仍能暴露"回调被推迟了多久"（用户耳朵听到的那一下）。
        _now_m = time.monotonic()
        if self._cb_last_t:
            _gap_ms = (_now_m - self._cb_last_t) * 1000.0
            if _gap_ms > self._cb_gap_max:
                self._cb_gap_max = _gap_ms
        self._cb_last_t = _now_m
        _t_beg = time.perf_counter()   # ★ R119-2：回调整体耗时埋点（~60ns 开销）
        if status:
            f = self._pa_flags
            if status.output_underflow:
                f |= self.PA_OUTPUT_UNDERFLOW
                # ★ R119-2：次数 + **距最近一次 play 的秒数**（检验"起播 20s 内"）
                self._uf_count += 1
                if self._play_t0 and len(self._uf_after_play) < 16:
                    self._uf_after_play.append(
                        round(time.monotonic() - self._play_t0, 2))
            if status.input_overflow:
                f |= self.PA_INPUT_OVERFLOW
            if status.input_underflow:
                f |= self.PA_INPUT_UNDERFLOW
            self._pa_flags = f
        # ★ 2026-09-19：**RT 线程自设优先级**（只做一次）。
        #   顺序有意：先 SetThreadPriority(15)，再注册 MMCSS —— MMCSS 的
        #   "Pro Audio" 档优先级更高，放最后让它成为最终裁定者。
        if not self._rt_boosted:
            self._rt_boosted = True
            _set_self_priority(2)          # 2 = TIME_CRITICAL(15)
            self._rt_mmcss = _enter_mmcss()
            # ★ R119-2：**读回真值**。`mmcss=False` 不代表没护住（PortAudio 可能自己
            #   已注册 MMCSS）—— 只有 rt_prio 能回答"回调线程到底在什么优先级上"。
            self._rt_prio = _read_self_priority()

        # ★ 2026-09-19：预分配 + fill(0) 复用，取代每帧 np.zeros（见 __init__ 注释）。
        if self._mixbuf is None or self._mixbuf.shape[0] != frames:
            self._mixbuf = np.zeros((frames, STREAM_CH), dtype=np.float32)
        mixed = self._mixbuf
        mixed.fill(0.0)
        _t1 = time.perf_counter()          # ★ R119-2m：阶段①取块（队列/生产者）
        self.music.pump(mixed, frames)
        # ★ R119-2：**静默空隙探测**。`pump` 里 `n = min(块长, frames)`：块长 < frames
        #   ⇒ 尾部**无声**补静音；块长 > frames ⇒ 余量**被直接丢弃**。原先两者都没有
        #   计数器，所以这条路径出问题也看不见（静默家族）。
        _bl = self.music._last_blk_len
        if _bl >= 0:
            if _bl < frames:
                self._blk_short += 1
            elif _bl > frames:
                self._blk_long += 1
        if frames != BLOCK_FRAMES:
            self._cb_frames_bad += 1
        self.tts.pump(mixed, frames)
        self.fx.pump(mixed, frames)
        _t2 = time.perf_counter()          # ★ R119-2m：阶段②输出（clip + outdata）
        # ★ R119-2b：削波/峰值探测。用 max()/min() 两个**归约** ⇒ 不分配临时数组
        #   （`np.max(np.abs(mixed))` 会每帧新建临时数组，破坏"回调零分配"）。
        _hi = float(mixed.max())
        _lo = float(mixed.min())
        _pk = _hi if _hi > -_lo else -_lo
        if _pk > self._peak_max:
            self._peak_max = _pk
        if _pk > 1.0:
            self._clip_n += 1
        np.clip(mixed, -1.0, 1.0, out=mixed)
        outdata[:] = mixed
        # 音频响应：对"即将送出"的混音做轻量频谱（enabled 关闭时零开销）
        self.spec.feed(mixed)
        # P2 特征帧：RT 侧仅入环形缓冲（memcpy 级；分析在独立线程，enabled 关时零拷贝）
        self.feat.feed(mixed)

        # ★ R119-2m：阶段③分析（spec/feat）—— 与总耗时一起结算
        _t3 = time.perf_counter()
        # ★ R119-2：回调耗时（含 spec/feat）—— 放**最后**，覆盖全链
        _t_end = time.perf_counter()
        _dt_us = (_t_end - _t_beg) * 1e6
        self._cb_us_last = _dt_us
        self._cb_n += 1
        # ★ R119-2m：三段分别取 max（每次 3 次减法，纳秒级开销）
        _p = (_t1 - _t_beg) * 1e6
        _o = (_t2 - _t1) * 1e6
        _f = (_t3 - _t2) * 1e6
        if _p > self._cb_t_pump_max:
            self._cb_t_pump_max = _p
        if _o > self._cb_t_out_max:
            self._cb_t_out_max = _o
        if _f > self._cb_t_feed_max:
            self._cb_t_feed_max = _f
        if _dt_us > self._cb_us_max:
            self._cb_us_max = _dt_us


# ---------------------------------------------------------------------------
# 对外门面：与 pygame 版 PlayerEngine API 完全兼容
# ---------------------------------------------------------------------------

class PlayerEngine:
    def __init__(self) -> None:
        self._mixer = SoftMixer()
        self._audio_ok = self._mixer.open()
        self._gc_last = time.monotonic()   # ★ R119-2r：空闲期显式回收的节流戳
        self._idle_since = 0.0             # ★ R119-2w：进入"非播放"的时刻（0=正在播）

        self._state = PlayerState.STOPPED
        self._current_path: str | None = None
        # R86 P0：在线曲下载落盘的本地临时文件路径（_download_stream 产物）。
        # 语义层（CLAP 段索引 / Beat This! beats 索引）需要本地文件才能离线分析，
        # 而在线曲的 path 是 URL → 之前被索引门控直接跳过 → 语义层在真机从未生效。
        # 这里记住落盘路径，供 mood 线程按「当前播放曲」取用（仅本地曲为 None）。
        self._stream_local: str | None = None
        self._target_volume = PlayerConfig.MUSIC_VOLUME
        self._ducking = False

        self._mixer.music.gain = self._target_volume
        self._mixer.tts.gain = 1.0
        self._mixer.fx.gain = 1.0

        self._seek_offset_ms = 0
        self._duration_ms = 0
        # R86：从音频文件本身探测到的真实时长（毫秒）。在线曲（搜索点播 / 部分
        # 漫游曲）元数据常缺 duration → play(0) 让 _duration_ms=0 → 后端
        # state().duration=0 → 前端进度条恒 0（"进度条不动"）。load() 成功后用
        # soundfile 探测一次作为兜底，play() 在未传时长时回退到它。
        self._file_duration_ms = 0
        self._seek_guard_until = 0.0
        self._ever_played = False
        self._ended_cb = None
        self._position_cb = None

        # ================= EQ（R68：手动/预设/动态三层合成） =================
        # 语义：eq_enabled 总闸；eq_source = 'manual' | 'preset'（互斥，面板二选一）；
        #   eq_preset 预设名；eq_bands 手动 10 段 dB；eq_dyn 动态补偿强度 0..1。
        #   三层合成 = 手动或预设的静态曲线 ⊕ 动态等响补偿（音量越低提越多）。
        self._eq_lock = threading.RLock()
        self._eq_enabled = False
        self._eq_source = 'manual'
        self._eq_preset = 'flat'
        self._eq_bands = [0.0] * ParametricEQ.EQ_BAND_COUNT
        self._eq_dyn = 0.6
        self._eq_applied_ok = False     # 最近一次 set_eq 是否已落到引擎（audio_ok 且已应用）

        self._fill_thread: threading.Thread | None = None
        self._fill_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        # R80 爆音确诊：monitor 线程读 SoftMixer._pa_flags 的上一次快照 + 节流戳
        self._pa_flags_last = 0
        self._pa_warn_t = 0.0
        # ★ 2026-09-18：默认输出端点跟随检查的节流戳（monitor 线程每 ~2s 一次）
        self._dev_check_t = 0.0
        # ★ 2026-09-19：卡顿诊断（RT 侧空队列计数快照 + 节流戳）
        self._starve_last = 0
        self._starve_t = 0.0
        # ★ 2026-09-19：输出流缺失时的重开节流戳（自愈）
        self._dev_retry_t = 0.0
        self._position_thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._mix_lock = threading.Lock()
        self._ramp_lock = threading.Lock()
        # 平滑过渡可取消：新 play/stop/seek 会终止旧 ramp 线程，防止 fade 收尾把
        # 新歌 gain 拉错（pygame 版 fade 同步无此问题；新引擎异步 ramp 必须可取消）。
        self._ramp_stop = threading.Event()
        self._ramp_stop.set()   # 初始无 ramp 在跑

        if self._audio_ok:
            self._start_fill_thread()
            self._start_monitor_thread()

    # ================= 基础控制 =================
    @property
    def duration_ms(self) -> int:
        """当前曲目总时长（毫秒）。元数据优先，缺失时 = 文件探测值（R86）。"""
        return int(self._duration_ms or self._file_duration_ms or 0)

    def load(self, path: str) -> bool:
        self._current_path = path
        self._file_duration_ms = 0
        self._stream_local = None      # R86 P0：非在线曲恒为 None，避免串曲
        if path and (path.startswith("http://") or path.startswith("https://")):
            local = self._download_stream(path)
            if not local:
                logger.warning("远程音源下载失败: %s", path)
                self._state = PlayerState.STOPPED
                return False
            self._stream_local = local   # R86 P0：记住落盘路径供语义层索引
            path = local
        if not path or not Path(path).exists():
            logger.warning("音源文件不存在，跳过加载: %s", path)
            self._state = PlayerState.STOPPED
            return False
        # R86：探测真实时长（毫秒）—— 只读文件头，开销极小。元数据缺 duration 的
        # 在线曲靠它拿到进度条总长（play() 未传时长时回退此值）。
        try:
            info = sf.info(path)
            if info.samplerate:
                self._file_duration_ms = int(info.frames / info.samplerate * 1000)
        except Exception as e:  # noqa
            logger.debug("时长探测失败（用元数据兜底）: %s: %s", type(e).__name__, e)
        if not self._audio_ok:
            self._state = PlayerState.STOPPED
            return True
        with self._mix_lock:
            ok = self._mixer.music.open(path)
        if not ok:
            self._state = PlayerState.STOPPED
            return False
        self._state = PlayerState.STOPPED
        return True

    # R99-T1a：落盘流缓存键。
    # ⚠ 旧实现 `fp = d / f"{abs(hash(url))}{suffix}"` 有一个**致命缺陷**：Python
    #   字符串哈希受 `PYTHONHASHSEED` **进程级随机化**（本机 `sys.flags.hash_randomization=1`），
    #   同一 URL 每次启动算出**不同文件名** ⇒ 跨进程**永不命中** ⇒ 每首歌每次启动
    #   都重下一遍。实测 `%TEMP%/musicai_stream` 1004 个文件 / 5.0GB 中有
    #   **675 个是多余的重复**（3.1GB）；其中一首被完整下载了 **24 次**
    #   （同尺寸、md5 一致）。改用 sha1 前 20 hex（80 bit）—— 稳定、跨进程一致、
    #   碰撞概率可忽略。副作用（正向）：落盘流从此可**跨启动复用**，也就成了
    #   段索引（T1）可用的持久音频源，无需再听一遍才能索引。
    STREAM_CACHE_DIR = "musicai_stream"

    @staticmethod
    def _stream_cache_key(url: str) -> str:
        """稳定缓存键（跨进程一致）。**不要**改回 `hash()`。"""
        return hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:20]

    @staticmethod
    def _stream_cached(url: str) -> str | None:
        """已有落盘流 → 返回路径（调用方**一次网络都不打**）。

        后缀（mp3/flac/wav）由响应 Content-Type 决定、查缓存时不可知 ⇒ 三个都试。
        静态方法：段索引预热线程只有 url 没有引擎实例（R99-T1）。
        """
        import tempfile
        d = Path(tempfile.gettempdir()) / PlayerEngine.STREAM_CACHE_DIR
        key = PlayerEngine._stream_cache_key(url)
        for suf in (".mp3", ".flac", ".wav"):
            fp = d / f"{key}{suf}"
            try:
                if fp.exists() and fp.stat().st_size > 0:
                    return str(fp)
            except Exception:  # noqa
                continue
        return None

    def _download_stream(self, url: str) -> str | None:
        max_bytes = 256 * 1024 * 1024
        part = None
        try:
            import requests
            import tempfile
            url = validate_media_url(url)
            # R99-T1a：先查缓存 —— 命中即复用，连请求都不发。
            hit = self._stream_cached(url)
            if hit:
                logger.info("[engine] 落盘流命中，跳过下载: %s", Path(hit).name)
                return hit
            resp = requests.get(url, timeout=(5, 30), stream=True,
                                allow_redirects=True)
            resp.raise_for_status()
            if resp.url != url:
                validate_media_url(resp.url)
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > max_bytes:
                raise ValueError("远程音源超过 256 MB 限制")
            ct = (resp.headers.get("Content-Type") or "").lower()
            suffix = ".mp3"
            if "flac" in ct or url.lower().endswith(".flac"):
                suffix = ".flac"
            elif "wav" in ct or url.lower().endswith(".wav"):
                suffix = ".wav"
            d = Path(tempfile.gettempdir()) / PlayerEngine.STREAM_CACHE_DIR
            d.mkdir(parents=True, exist_ok=True)
            # ⚠ 用 `PlayerEngine.STREAM_CACHE_DIR` 而不是 `self.…`：与静态的
            #   `_stream_cached` **同一种取法**。写成 `self.` 时，任何没有该属性的
            #   调用方（例如只用本方法的轻量桩/预热线程）都会 AttributeError 被外层
            #   `except` 吞成"下载失败"，且**无声**（R99-T1c 实测踩到）。
            fp = d / f"{self._stream_cache_key(url)}{suffix}"
            # ⚠⚠ 必须**原子落盘**：缓存键现在跨进程稳定 ⇒ 半截文件会被下次启动
            #   当成命中**永久复用**（旧实现每次换新名，反而掩盖了这个问题）。
            part = fp.with_name(fp.name + ".part")
            with open(part, "wb") as f:
                total = 0
                for chunk in resp.iter_content(1 << 16):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("远程音源超过 256 MB 限制")
                    f.write(chunk)
            os.replace(part, fp)          # 同目录 ⇒ 同文件系统 ⇒ 原子
            return str(fp)
        except Exception as e:  # noqa
            try:                          # 半截文件绝不留在缓存目录里
                if part and Path(part).exists():
                    Path(part).unlink()
            except Exception:  # noqa
                pass
            logger.error("下载远程音源失败: %s", e)
            return None

    def _prefill_music(self, src, blocks: int | None = None) -> int:
        """★ 2026-09-19（R119-2e）：同步预填音乐队列，**不依赖 `playing_flag`**。

        从 `play()` 内联搬出来，供 `play()` 与 `seek()` 共用（原先只有 play 有预填，
        `seek()` 清完队列就空着走 ⇒ 拖进度条同样有一条几百毫秒的静音缝）。
        返回预填到的块数；任何异常都不影响主流程（预填只是"提前量"）。"""
        # ★ R119-3a：预填 = **正在起播**（`play()`/`seek()` 共用本方法）⇒ 一并清零
        #   "空闲起点"，让 `seek()` 路径也堵住"空闲期全量 GC 撞起播窗"。
        self._idle_since = 0.0
        n = 0
        try:
            want = PLAY_PREFILL_BLOCKS if blocks is None else int(blocks)
            for _ in range(max(0, want)):
                if src.queue.full():
                    break
                blk = src._read_block(BLOCK_FRAMES)
                if blk is None:
                    break
                src.queue.put_nowait(blk)
                n += 1
        except Exception as _e:  # noqa
            logger.debug("[engine] 预填失败（不影响主流程）: %s", _e)
        return n

    def play(self, duration: float | None = None) -> None:
        if not self._current_path:
            logger.warning("[engine] play 无 current_path，忽略")
            return
        # ★ R119-3a（2026-09-19）：**起播即刻清零"空闲起点"**（置位要尽可能早）。
        #   真机矛盾读数：`stall_at=1.05s / stall_max=0.238s` 与
        #   `gc_max_ms=261.7ms (gen2)` **同时**出现 ⇒ 那次空闲期全量 GC 冻在了起播窗上。
        #   成因：本方法里 `_state` 要到**预填结束**才置 PLAYING（实测这段窗 ~1.0s），
        #   而 `_monitor_loop` 的空闲回收只判 `_state != PLAYING` ⇒ 整段起播窗都被
        #   判成"空闲"，上一轮遗留的 `_idle_since`（早已 >5s）立刻满足条件 ⇒
        #   250ms stop-the-world 正好砸在刚起播、队列还浅的时刻 ⇒ **可听的卡**；
        #   又因 `_gc_last` 有 90s 节流 ⇒ 频率≈**每曲一次**（与"一曲基本上一次"吻合）。
        self._idle_since = 0.0
        # R67-7：去掉「PAUSED → resume 不重头」的快捷分支 —— 旧语义在快速切歌
        # 比赛下会让 play() 误走 resume（仅置 playing_flag=True、不 fill 队列），
        # sounddevice 回调消费空队列 → out_pos 永远 0 → 进度条不动，UI 完全卡死。
        # 现在 play() 永远走完整重置路径：start_play + 同步预填 1 块。
        # （"从暂停处继续"语义由显式 resume() 入口保留，play() 永远是"从头放"。）
        self._ever_played = False
        self._seek_offset_ms = 0
        # R86：元数据给的时长优先；缺失（0/None）时回退 load() 探测到的文件真实时长
        # ——否则 _duration_ms=0 → state().duration=0 → 前端进度条永远不动。
        meta_ms = int((duration or 0) * 1000)
        real_ms = int(getattr(self, "_file_duration_ms", 0) or 0)
        # ★★★ R114（2026-09-13）**短音频守卫（单向）**。
        #   现实缺陷：在线曲的直链**可能是降级/试听片段**（实测同一首歌拿到 20.0 s 而
        #   元数据是 300 s），或下载被截断。此时若照旧按元数据报时长，用户看到的是
        #   「播几秒就自动切歌，而进度条按 300 s 铺、怎么拖都对不上」——两个症状同源。
        #   ⇒ 只允许**变短**（`real < meta × SHORT_AUDIO_RATIO`），**绝不拉长**
        #   ⇒ 正常曲目走原分支、行为逐位不变；只有"音频明显短于声明"时才改口径。
        #   置 `_short_audio=True` 供上层（播放编排）失效这条坏直链缓存，实现自愈。
        self._short_audio = False
        self._short_audio_ms = 0
        if meta_ms > 0 and 0 < real_ms < meta_ms * SHORT_AUDIO_RATIO:
            logger.warning(
                "[engine] 音频短于元数据：真实 %.1fs < 元数据 %.1fs × %.2f ⇒ **按真实时长播**"
                "（%s；判为降级片段/截断下载，播放侧将失效该直链缓存）",
                real_ms / 1000.0, meta_ms / 1000.0, SHORT_AUDIO_RATIO,
                Path(self._current_path or "").name)
            self._duration_ms = real_ms
            self._short_audio = True
            self._short_audio_ms = real_ms
        else:
            # ★ 2026-09-18：以**文件真实时长**为权威（real 优先）。
            #   元数据标签常不准/张冠李戴（用户实测：部分 44.1k 曲 meta 比真实长几秒
            #   → 音频已播完而进度条还差几秒没走满 → 直接跳下一首）。real 由 sf.info
            #   从文件头探测，恒等于实际会播的长度；探测失败(real=0)才回退元数据。
            self._duration_ms = real_ms or meta_ms
        self._cancel_ramp()   # 终止旧 fade 线程，避免其把新歌 gain 拉向过期目标
        logger.info("[engine] play dur=%.3fs path=%s", duration or 0,
                    Path(self._current_path or "").name)
        try:
            # ★ R119-2：为"起播后 N 秒内的 underflow"计时（诊断面 uf_after_play）
            self._mixer.mark_play_start()
        except Exception:  # noqa
            pass
        if not self._audio_ok:
            self._state = PlayerState.PLAYING
            if duration:
                try:
                    threading.Timer(max(1.0, float(duration)), self._fire_ended).start()
                except Exception:  # noqa
                    pass
            return
        src = self._mixer.music
        with self._mix_lock:
            if src._file is None:
                # stop() 已关闭文件但 current_path 仍在：重开，避免卡在 PLAYING 永不 ended
                if not src.open(self._current_path):
                    logger.warning("[engine] play 重开文件失败: %s", self._current_path)
                    return
            # ★ R119-2e：**先不立 flag**（后续"清 → 预填 → 立"三步走）。
            src.start_play(arm=False)
            src.gain = self._target_volume if not self._ducking else PlayerConfig.DUCK_VOLUME
            logger.info("[engine] play 落子 gain=%.2f ducking=%s total=%d sr=%d resample=%s",
                        src.gain, self._ducking, src.total, src._src_sr,
                        "no" if src._up == 1 else f"{src._up}/{src._down}")
            # R67-7 + ★2026-09-19：同步**批量预填**（旧值 1 块）。
            #   sounddevice 回调 ~23ms 一次，但 fill 线程是 2ms 轮询。
            #   ⚠ "每首歌一开始卡"的直接成因：play() 落子时队列**是空的**，而换曲瞬间
            #   正是 CLAP 段索引 / VA 推理开始抢 CPU 的时刻 ⇒ 生产者跟不上 ⇒ pump()
            #   取不到块 → 补静音 = 可听的卡顿（RT 侧 `_starve` 就是它的直证）。
            #   预填 PLAY_PREFILL_BLOCKS 块给起播一层厚垫（解码开销几十 ms，可忽略）。
            # ★ R119-2e：此刻 **flag 仍是假** ⇒ 回调不会来消费
            #   （`pump` 见 flag=False 直接返回，**也不计 starve** ⇒ 不再补静音）。
            _n = self._prefill_music(src)
            # ★ 预填完**最后**立 flag：队列此时最多已有 32 块（≈740ms），
            #   生产者线程（同样以该 flag 为闸门）也从此刻起接力。
            src.playing_flag = True
            logger.debug("[engine] play 预填 %d 块 → 立 playing_flag", _n)
        self._state = PlayerState.PLAYING
        self._start_position_tracker()

    @property
    def short_audio(self) -> bool:
        """R114：本次播放的音频是否**明显短于元数据**（降级片段 / 截断下载）。

        ★ 必须对外暴露：播放编排要靠它失效那条坏直链缓存（否则 6h TTL 会把
        "播 20 s 就切歌"钉死半天）。项目铁则——**能力只在代码内部 = 不存在**。
        """
        return bool(getattr(self, "_short_audio", False))

    def set_ended_callback(self, cb) -> None:
        self._ended_cb = cb

    def _fire_ended(self) -> None:
        if self._state != PlayerState.PLAYING:
            return
        self._state = PlayerState.STOPPED
        # 去重 + 复位：关掉音乐槽文件并清零终止状态，杜绝 monitor 对同一终止
        # 状态二次 fire（radio advance 到新歌 load 完成前有数百 ms 空窗，旧歌
        # 残留状态若不清理，monitor 会再判一次 ended → 重复 ended 造成跳两首/
        # 跳回）。关文件后若需重播，play() 会按 current_path 重开并恢复 total。
        try:
            self._mixer.music.close()
        except Exception:  # noqa
            self._mixer.music.playing_flag = False
            self._mixer.music.total = 0
        logger.info("[engine] _fire_ended（播完，音乐槽已复位）")
        cb = self._ended_cb
        if cb:
            try:
                cb()
            except Exception as e:  # noqa
                logger.warning("ended 回调异常: %s", e)

    def pause(self) -> None:
        if self._state == PlayerState.PLAYING and self._audio_ok:
            self._cancel_ramp()   # 暂停时终止 fade 线程，避免音量被继续拉走
            with self._mix_lock:
                src = self._mixer.music
                src.playing_flag = False
                src.clear()
                # 读头回卷到已送出帧：resume 从"听过的最后一帧"继续，不重不漏
                src.align_read_head()
            self._state = PlayerState.PAUSED
            logger.info("[engine] pause @out_pos=%d", self._mixer.music.out_pos)

    def resume(self) -> None:
        if self._state == PlayerState.PAUSED and self._audio_ok:
            self._cancel_ramp()
            with self._mix_lock:
                src = self._mixer.music
                src.clear()
                src.playing_flag = True
                # 恢复目标音量（pause 期间旧 fade 已被终止，此处确保音量归位）
                src.gain = self._target_volume if not self._ducking else PlayerConfig.DUCK_VOLUME
            self._state = PlayerState.PLAYING
            logger.info("[engine] resume @out_pos=%d", self._mixer.music.out_pos)

    def stop(self, unduck: bool = False) -> None:
        self._cancel_ramp()   # 终止旧 fade 线程，避免其继续把 gain 拉向 0/旧目标
        if self._audio_ok:
            with self._mix_lock:
                src = self._mixer.music
                src.playing_flag = False
                src.close()          # 释放文件句柄；play() 会按 current_path 重开
        self._state = PlayerState.STOPPED
        self._stop_flag.set()
        logger.info("[engine] stop unduck=%s (was out_pos=%d)", unduck,
                    self._mixer.music.out_pos)
        if unduck:
            self._ducking = False
            if self._audio_ok:
                with self._mix_lock:
                    self._mixer.music.gain = self._target_volume

    def toggle(self) -> None:
        if self._state == PlayerState.PLAYING:
            self.pause()
        elif self._state == PlayerState.PAUSED:
            self.resume()
        else:
            self.play()

    # ================= 进度 / 音量 =================
    def seek(self, ms: int) -> None:
        """跳转：O(1) 文件指针 seek + 清队列重填（soundfile 原生，无需 pygame 的
        play(0,start) 整文件重载）。跳转后从目标位置起播。"""
        if not self._audio_ok or not self._current_path:
            return
        self._cancel_ramp()   # 终止旧 fade 线程
        target = max(0, int(ms))
        src = self._mixer.music
        with self._mix_lock:
            if src._file is None and not src.open(self._current_path):
                return
            # ★ R119-2e：`seek_to` 内部会 `clear()`，若 flag 仍为真则从清空到重填之间
            #   同样是"取不到块 ⇒ 补静音"的窗口（拖进度条会听到一下断）。
            #   先**按下** flag：既消除该窗口，也挡住生产者用**旧位置**补块（内容串音）。
            _was_playing = bool(src.playing_flag)
            src.playing_flag = False
            src.seek_to(int(target / 1000.0 * STREAM_SR))
            src.gain = self._target_volume if not self._ducking else PlayerConfig.DUCK_VOLUME
            self._prefill_music(src)
            src.playing_flag = _was_playing   # ★ 恢复原状态：暂停/停止时 seek 不会偷偷开播
        self._state = PlayerState.PLAYING
        self._ever_played = True
        # ⚠⚠ 这里**必须置 0**，不能置 target。
        #   上面 `src.seek_to(...)` 已把读头 out_pos 归位到**绝对帧**（seek_to 内部
        #   `self.out_pos = int(frame)`），而 get_position() 算的是
        #       abs = _seek_offset_ms + out_pos/SR*1000
        #   若此处再记 `_seek_offset_ms = target`，abs 就变成 **2 倍 target**
        #   （跳到 60s → 上报 120s，超过时长还被夹到末尾）。
        #   表现：音频确实跳过去了，但后端上报位置翻倍 → 前端 adoptRemotePosition
        #   判定"偏差 > 1200ms"采信 → 进度条弹走、歌词跟着弹走（R66 用户点名）。
        #   语义：offset 只在"out_pos 从 0 起算"时才需要（即 play() 从头播），
        #   seek 后 out_pos 本身就是绝对值 → offset 归 0。
        self._seek_offset_ms = 0
        self._seek_guard_until = time.monotonic() + 0.7
        logger.info("[engine] seek -> %dms (out_pos 归位 %d)", target, src.out_pos)
        self._start_position_tracker()

    def get_position(self) -> int:
        if not self._audio_ok or self._state == PlayerState.STOPPED:
            return 0
        src = self._mixer.music
        pos_ms = int(src.out_pos / STREAM_SR * 1000)
        abs_pos = self._seek_offset_ms + pos_ms
        if self._duration_ms and abs_pos > self._duration_ms:
            abs_pos = self._duration_ms
        return max(0, abs_pos)

    def music_busy(self) -> bool:
        if not self._audio_ok:
            return False
        src = self._mixer.music
        return bool(src.playing_flag or (src.total > 0 and src.out_pos < src.total))

    @property
    def volume(self) -> float:
        return self._target_volume

    def set_volume(self, v: float) -> None:
        self._target_volume = max(0.0, min(1.0, v))
        if self._audio_ok and not self._ducking:
            with self._mix_lock:
                self._mixer.music.gain = self._target_volume
        # 动态 EQ：用户音量变化 → 更新等响补偿曲线（D2 决策：仅音量，不含频谱）
        self._refresh_dynamic_eq()

    # ================= EQ（R68：手动/预设/动态三层合成） =================
    # 10 段 ISO 预设（dB；Q=1.41；数值核自 docs/eq_research_r68.md §3.2，
    # snipsound + everappz 两源合成取整）。'flat' = 全 0 走旁路零开销。
    EQ_PRESETS: dict[str, list[float]] = {
        'flat':       [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        'bass':       [8, 6, 4, 0, 0, 0, 0, 0, 0, 0],
        'treble':     [0, 0, 0, 0, 0, 0, 0, 3, 5, 6],
        'vocal':      [0, 0, -1, -3, 1, 3, 3, 4, 1, 0],
        'podcast':    [-6, -3, 0, -2, 0, 0, 0, 3, 1, 0],
        'pop':        [4, 4, 2, 0, 0, 0, 0, 2, 2, 0],
        'rock':       [5, 4, 3, 1, 0, 0, 1, 3, 4, 4],
        'hiphop':     [7, 5, 2, 0, -2, 0, 1, 0, 3, 2],
        'classical':  [1, 2, 1, 1, 0, 0, 0, 1, 2, 2],
        'loudness':   [4, 3, 0, 0, 0, 0, 0, 3, 4, 5],
    }
    EQ_PRESET_NAMES = list(EQ_PRESETS.keys())
    # 动态等响补偿参数（Fletcher–Munson；参考 Audyssey Dynamic EQ / EasyEffects
    # Bass Loudness 的量级）：音量 < EQ_DYN_V_ON 才介入，越低补偿越强。
    EQ_DYN_V_ON = 0.35
    EQ_DYN_MAX_LOW = 6.0      # 全补偿时 lowshelf(31/62/125 用低架于 120Hz 一带) 上限 dB
    EQ_DYN_MAX_HIGH = 4.0     # 高频 highshelf 上限 dB

    # 手动/预设（静态层）选当前有效曲线；'flat' 全 0
    def _static_curve(self):
        if self._eq_enabled and self._eq_source == 'preset':
            return list(self.EQ_PRESETS.get(self._eq_preset,
                                            self.EQ_PRESETS['flat']))
        return list(self._eq_bands)

    # 动态补偿层：输入 = 实际用户音量（target_volume，忽略 ducking 瞬态）
    def _dyn_curve(self, vol: float | None = None) -> list[float]:
        if not self._eq_enabled or self._eq_dyn <= 0.0:
            return [0.0] * ParametricEQ.EQ_BAND_COUNT
        v = self._target_volume if vol is None else vol
        v = max(0.0, min(1.0, v))
        if v >= self.EQ_DYN_V_ON:
            return [0.0] * ParametricEQ.EQ_BAND_COUNT
        k = (1.0 - v / self.EQ_DYN_V_ON) * min(1.0, max(0.0, self._eq_dyn))
        # 31/62/125 提低频 + 8k/16k 提高频（等响曲线形状）；中频不动
        out = [0.0] * ParametricEQ.EQ_BAND_COUNT
        for i in (0, 1, 2):
            out[i] = round(k * self.EQ_DYN_MAX_LOW, 2)
        for i in (8, 9):
            out[i] = round(k * self.EQ_DYN_MAX_HIGH, 2)
        return out

    # 三层合成（clamp ±12dB）→ 应用到 MusicSource.eq
    def _apply_eq_locked(self) -> None:
        if not self._audio_ok:
            self._eq_applied_ok = False
            return
        static = self._static_curve()
        dyn = self._dyn_curve()
        final = [max(-12.0, min(12.0, s + d))
                 for s, d in zip(static, dyn)]
        try:
            eq = self._mixer.music.eq
            eq.set_bands(final, q=1.41)   # 内部原子换 _sos + 同段数延续 _zi
            self._eq_applied_ok = True
        except Exception as e:  # noqa
            logger.warning("[engine] EQ 应用失败: %s", e)
            self._eq_applied_ok = False

    def set_eq(self, *, enabled: bool | None = None, source: str | None = None,
               preset: str | None = None, bands: list | None = None,
               dyn: float | None = None) -> dict:
        """控制线程调（非 RT）；桥/路由把 user_config 值透传进来。
        只写配置 + 重建系数；播放中立即生效，重启由桥按持久化值恢复。"""
        with self._eq_lock:
            if enabled is not None:
                self._eq_enabled = bool(enabled)
            if source is not None:
                self._eq_source = 'preset' if source == 'preset' else 'manual'
            if preset is not None:
                p = (preset or 'flat').lower()
                self._eq_preset = p if p in self.EQ_PRESETS else 'flat'
            if bands is not None:
                b = [float(x) for x in bands]
                n = ParametricEQ.EQ_BAND_COUNT
                if len(b) >= n:
                    self._eq_bands = b[:n]
                elif len(b) > 0:
                    self._eq_bands = (b + [0.0] * n)[:n]
                # 空列表 = 复位手动
                else:
                    self._eq_bands = [0.0] * n
            if dyn is not None:
                self._eq_dyn = max(0.0, min(1.0, float(dyn)))
            self._apply_eq_locked()
            return self.get_eq_state_locked()

    def get_eq_state_locked(self) -> dict:
        return {
            'enabled': bool(self._eq_enabled),
            'source': self._eq_source,
            'preset': self._eq_preset,
            'bands': [round(x, 2) for x in self._eq_bands],
            'dyn': round(self._eq_dyn, 2),
            'applied': bool(self._eq_applied_ok),
        }

    def get_eq_state(self) -> dict:
        with self._eq_lock:
            return self.get_eq_state_locked()

    def _refresh_dynamic_eq(self) -> None:
        """音量变化时刷新动态层。仅在动态补偿已启用时做事；
        非音频就绪（offscreen/静默）下直接跳过，恢复期由桥再应用。"""
        if not self._eq_enabled or self._eq_dyn <= 0.0:
            return
        try:
            with self._eq_lock:
                self._apply_eq_locked()
        except Exception:  # noqa
            pass

    # ================= Ducking =================
    def duck(self, level: float | None = None) -> None:
        if not self._audio_ok:
            self._ducking = True
            return
        self._cancel_ramp()   # 立即压低：终止旧 fade 线程，防止其把 gain 又拉回去
        level = level if level is not None else PlayerConfig.DUCK_VOLUME
        self._ducking = True
        with self._mix_lock:
            self._mixer.music.gain = level

    def unduck(self, fade_ms: int | None = None) -> None:
        if not self._audio_ok:
            self._ducking = False
            return
        self._cancel_ramp()   # 终止仍在跑的旧 fade（尤其 fade_out），从当前值平滑恢复
        fade_ms = fade_ms if fade_ms is not None else PlayerConfig.RECOVER_FADE_MS
        self._ducking = False
        self._ramp_to(self._target_volume, fade_ms)

    # ================= 平滑过渡 =================
    def fade_to(self, target: float, fade_ms: int) -> None:
        if not self._audio_ok:
            return
        self._cancel_ramp()
        self._ramp_to(max(0.0, min(1.0, target)), max(1, fade_ms))

    def fade_out(self, fade_ms: int | None = None) -> None:
        if not self._audio_ok:
            self._ducking = True
            return
        self._cancel_ramp()
        self._ducking = True
        self._ramp_to(0.0, fade_ms if fade_ms is not None else PlayerConfig.RECOVER_FADE_MS)

    def fade_in(self, fade_ms: int | None = None, target: float | None = None) -> None:
        if not self._audio_ok:
            self._ducking = False
            return
        self._cancel_ramp()   # 终止旧 fade_out（切歌场景），从当前值平滑爬升
        self._ducking = False
        tgt = target if target is not None else self._target_volume
        dur = fade_ms if fade_ms is not None else PlayerConfig.RECOVER_FADE_MS
        threading.Thread(target=self._ramp_to, args=(tgt, dur), daemon=True).start()

    def _cancel_ramp(self) -> None:
        """终止当前/未开始的 ramp 线程。新 play/stop/seek/fade 前调用，
        防止旧 fade 收尾线程继续把 gain 拉向过期目标（如 fade_out 拉到 0
        会连带把新歌静音）。"""
        self._ramp_stop.set()
        # 若 ramp 线程正持有 _ramp_lock 循环 sleep，等它退出（最长 ~150ms/步）
        # —— 不阻塞太久；ramp 线程每步都检查 stop 标志，会在 50ms 内让出。
        with self._ramp_lock:
            pass

    def _ramp_to(self, target: float, fade_ms: int) -> None:
        if not self._audio_ok:
            return
        with self._ramp_lock:
            # 本轮 ramp 使用独立 stop 事件：_cancel_ramp 置位后本循环立即退出，
            # 且不覆盖最终 gain（避免旧 fade_out 把新歌拉静音）。
            self._ramp_stop.clear()
            ev = self._ramp_stop
            try:
                start = self._mixer.music.gain
            except Exception:  # noqa
                start = self._target_volume
            steps = max(1, fade_ms // 50)
            step_ms = max(1, fade_ms / steps)
            for i in range(1, steps + 1):
                if ev.is_set():
                    return  # 被新 play/stop/seek/fade 取消，放弃剩余渐变
                with self._mix_lock:
                    self._mixer.music.gain = start + (target - start) * (i / steps)
                time.sleep(step_ms / 1000.0)
            if not ev.is_set():
                with self._mix_lock:
                    self._mixer.music.gain = target

    # ================= TTS 通道 =================
    def play_tts(self, wav_path: str | Path) -> None:
        if not self._audio_ok:
            return
        with self._mix_lock:
            self._mixer.tts.set(str(wav_path))

    def is_tts_playing(self) -> bool:
        if not self._audio_ok:
            return False
        src = self._mixer.tts
        return bool(src.playing_flag and not src._at_end())

    def stop_tts(self) -> None:
        if not self._audio_ok:
            return
        try:
            with self._mix_lock:
                src = self._mixer.tts
                src.playing_flag = False
                src.clear()
                src.total = 0
                src.out_pos = 0
        except Exception:  # noqa
            pass

    def wait_tts(self) -> None:
        if not self._audio_ok:
            return
        src = self._mixer.tts
        while src.playing_flag and not src._at_end():
            time.sleep(0.05)

    # ================= 音效 =================
    def play_sound(self, path: str | Path) -> None:
        if not self._audio_ok:
            logger.info("静默模式，跳过试听播放: %s", path)
            return
        try:
            with self._mix_lock:
                self._mixer.fx.set(str(path))
        except Exception as e:  # noqa
            logger.error("试听播放失败: %s", e)

    # ================= 内部线程 =================
    def _start_fill_thread(self) -> None:
        if self._fill_thread and self._fill_thread.is_alive():
            return
        self._fill_stop.clear()
        self._fill_thread = threading.Thread(target=self._fill_loop, daemon=True)
        self._fill_thread.start()

    def _fill_loop(self) -> None:
        # ★ 2026-09-18：音频**生产者**线程抬到 ABOVE_NORMAL(+1)。
        #   前 20s 卡顿的真链条：CLAP/索引 burst 占核 → **本线程被饿死** → 音乐队列
        #   (MUSIC_Q_DEPTH) 被抽空 → PortAudio 没数据 → output underflow（爆音/卡）。
        #   只把分析线程压 LOWEST 不够（它们仍占核），必须同时抬高生产者。
        _set_self_priority(1)
        # R67-7：try/except 兜底 —— 旧实现裸跑循环，src.fill() 一旦抛异常
        # （如 soundfile 在被打断时偶发 read 失败）线程就静默死掉，
        # 此后回调消费空队列、out_pos 不再增加、进度条永远 0。
        try:
            while not self._fill_stop.is_set():
                src = self._mixer.music
                if src.playing_flag and src._file is not None:
                    try:
                        src.fill(BLOCK_FRAMES)   # EOF 时 fill 返回 False，但不改 playing_flag
                    except Exception as _fe:  # noqa
                        logger.debug("[engine] fill 异常（线程继续）: %s", _fe)
                time.sleep(0.002)
        except Exception as _e:  # noqa
            logger.warning("[engine] fill 线程异常退出: %s: %s", type(_e).__name__, _e)

    def _start_monitor_thread(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _idle_gc_due(self) -> bool:
        """★ R119-3a：**空闲期全量回收的唯一判据**（抽成方法 ⇒ 可单测、可"见红"）。

        返回 True 才允许做一次 stop-the-world 的 `gc.collect()`。三个必要条件：
          ① `_idle_since == 0`（正在播 **或正在起播**）⇒ 一律不许回收；
          ② "非播放"已**稳定持续 ≥5s**（避开换曲/起播过渡窗）；
          ③ 距上次回收 ≥90s（节流）。
        副作用：按 `_state` 维护 `_idle_since`（进入非播放 ⇒ 盖章；播放 ⇒ 清零）。

        ⚠ 为什么 `play()` 必须把 `_idle_since` **提前**清零：`play()` 里 `_state` 要到
          **预填结束**才置 PLAYING（实测该窗 ~1.0s），若只判 `_state != PLAYING`，
          整段起播窗都会被算成"空闲" ⇒ 那次 250ms 全扫正好砸在起播上（见 `play()` 顶部注释）。
        """
        if self._state != PlayerState.PLAYING:
            if self._idle_since <= 0:
                self._idle_since = time.monotonic()
        else:
            self._idle_since = 0.0
        return (self._idle_since > 0
                and time.monotonic() - self._idle_since >= 5.0
                and time.monotonic() - self._gc_last >= 90.0)

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.is_set():
            # R80 爆音确诊：PortAudio status 标志变化 → 节流日志（10s 最多一条）
            try:
                flags = self._mixer._pa_flags
                if flags != self._pa_flags_last:
                    new = flags & ~self._pa_flags_last
                    self._pa_flags_last = flags
                    now = time.monotonic()
                    if now - self._pa_warn_t >= 10.0:
                        self._pa_warn_t = now
                        if new & 2:
                            logger.warning(
                                "PortAudio output underflow（输出断流 → "
                                "听感爆音/咔哒声确诊；RT 回调预算被抢占）")
                        elif new & 1:
                            logger.warning(
                                "PortAudio input overflow（输入溢出，"
                                "通常不影响播放听感）")
                        else:
                            logger.warning("PortAudio status flags 变化: %s", new)
            except Exception:  # noqa 确诊埋点绝不干扰监控主逻辑
                pass
            # ★ 2026-09-18：默认输出端点跟随（后连蓝牙耳机 / 系统切换默认设备）。
            #   每 ~2s 检查一次；只在默认设备名变化时重建流（换设备一次，非每轮）。
            _nowm = time.monotonic()
            if _nowm - self._dev_check_t >= 2.0:
                self._dev_check_t = _nowm
                try:
                    if self._mixer.stream is None:
                        # ★ 2026-09-19 自愈：输出流没了（设备曾不可用 / open 失败）
                        #   → 每 5s 无条件重试。没有这一条，open() 失败一次 = **永久静默**，
                        #   只有重启后端能救（本次事故的直接放大器）。
                        _tm = time.monotonic()
                        if _tm - self._dev_retry_t >= 5.0:
                            self._dev_retry_t = _tm
                            logger.info("[engine] 输出流缺失，尝试重新打开…")
                            self._mixer.open()
                    else:
                        self._mixer.follow_default_device(
                            allow_reinit=(self._state != PlayerState.PLAYING))
                except Exception:  # noqa
                    pass
            # ★ R119-2r：**非播放时**显式全量回收（自动 gen2 已基本关闭之后的兜底）。
            #   播放中绝不 collect（那就是 stop-the-world）；间隔 90s，单次实测 ~250ms。
            # ★ R119-2y：停顿栈取证跟着播放状态开关（一件幂等的诊断接线）
            try:
                from core.audio_engine import stall_dump_ctl as _sdc
            except Exception:  # noqa
                _sdc = None
            if _sdc is not None:
                _sdc(self._state == PlayerState.PLAYING)
            # ★ R119-2w：**必须"稳定空闲"才回收**。原来只判 `state != PLAYING`，
            #   而**换曲空隙本身就是非播放**⇒ 那次 ~250ms 的 full GC 会撞在新曲起播上
            #   （实测：本曲内唯一一次 stall 与一次 gen2 255.9ms 同时出现）。
            #   现在要求"非播放已持续 ≥5s"，避开换曲过渡窗。
            #   ★ R119-3a：判据已抽成 `_idle_gc_due()`（可单测/可"见红"）；并配合
            #   `play()`/`_prefill_music()` 在**起播即刻**把 `_idle_since` 清零
            #   —— 否则"起播窗内 `_state` 还不是 PLAYING"会被误判成空闲。
            try:
                if self._idle_gc_due():
                    self._gc_last = time.monotonic()
                    import gc as _gc
                    _tg = time.monotonic()
                    _gc.collect()
                    logger.debug("[gc] 空闲期全量回收 %.0f ms",
                                 (time.monotonic() - _tg) * 1000.0)
            except Exception:  # noqa
                pass
            if self._audio_ok and self._state == PlayerState.PLAYING:
                src = self._mixer.music
                # ★ 2026-09-19 卡顿诊断：RT 侧"取不到块"计数节流上报，并附**队列深度**。
                #   深度贴 0 = 生产者(fill)被饿死（CPU 被抢）→ 继续降分析线程压力；
                #   深度很满 = 不是队列问题 → 别再往"抢 CPU"方向找，转向 RT/设备缓冲。
                try:
                    _sv = int(src._starve)
                    if _sv != self._starve_last:
                        _sm = time.monotonic()
                        if _sm - self._starve_t >= 3.0:
                            self._starve_t = _sm
                            logger.warning(
                                "[卡顿诊断] 空队列帧累计=%d（本段 +%d）队列=%d/%d —— "
                                "队列贴0=生产者被饿死(CPU被抢)；队列很满=非队列问题",
                                _sv, _sv - self._starve_last,
                                src.queue.qsize(), src.queue.maxsize)
                            self._starve_last = _sv
                except Exception:  # noqa 诊断绝不干扰监控主逻辑
                    pass
                # 播完 = 读头到 EOF、队列已空、已送帧达总帧、且不在 seek 保护窗
                if src.playing_flag is False and src.total > 0 and src._at_end() and \
                        time.monotonic() > self._seek_guard_until:
                    if src.out_pos > 0:
                        self._ever_played = True
                    self._fire_ended()
            time.sleep(0.15)

    # ---- 进度轮询（对外行为与 pygame 版一致）----
    def _start_position_tracker(self) -> None:
        if not self._audio_ok:
            return
        if self._position_thread and self._position_thread.is_alive():
            return
        self._stop_flag.clear()
        self._position_thread = threading.Thread(target=self._track_position, daemon=True)
        self._position_thread.start()

    def restart_position_tracker(self) -> None:
        if not self._audio_ok:
            return
        old = self._position_thread
        if old is not None and old.is_alive():
            self._stop_flag.set()
            old.join(timeout=1.0)
        self._position_thread = None
        self._stop_flag.clear()
        self._position_thread = threading.Thread(target=self._track_position, daemon=True)
        self._position_thread.start()

    def _track_position(self) -> None:
        while not self._stop_flag.is_set():
            if self._state == PlayerState.PLAYING and self._audio_ok:
                pos = self.get_position()
                if pos > 0:
                    self._ever_played = True
                if self._position_cb and pos >= 0:
                    try:
                        self._position_cb(pos)
                    except Exception:  # noqa
                        pass
            time.sleep(0.25)

    def set_position_callback(self, cb) -> None:
        self._position_cb = cb

    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @property
    def stream_local_path(self) -> str | None:
        """R86 P0：在线曲已落盘的本地临时文件路径；本地曲/未落盘 → None。

        语义层索引用它把在线曲也纳入离线分析（CLAP 段 + beats 表）。
        调用方须自行确认引擎当前播放的正是目标曲（比对 current_path），
        否则可能把上一首的音频索引到本曲 tid 上。
        """
        return self._stream_local

    @property
    def audio_ok(self) -> bool:
        return self._audio_ok

    # ================= R88 音频健康度探针 =================
    def audio_diag(self) -> dict:
        """音频链路健康度快照（供 `/api/status` 观测，一眼区分「爆音/断续」病因）。

        背景（R88）：`_pa_flags` 埋点（R80）此前**只打 console**，而
        `logging.basicConfig` 没有 FileHandler → 用户报「播放断续」时无从取证，
        只能靠肉眼看启动窗口。这里把它变成可查询字段。

        字段：
          · pa_flags / output_underflow —— PortAudio 状态位（bit1=输出断流，
            即爆音/断续的直接证据；RT 回调被抢占时置位，**只增不减**）；
          · queue_blocks / queue_depth —— 音乐队列当前水位/容量。水位贴近 depth
            说明生产跟得上；**反复打空 = 饥饿**；
          · state —— 播放器状态名（PLAYING/PAUSED/STOPPED）；
          · ★ R119-2（量化"还有一点点卡"，字段含义见 `SoftMixer.__init__`）：
            `cb_us_max/cb_us_last` 回调整体耗时 µs（预算 23.2ms ⇒ **远小于 1ms 说明
            卡顿不在回调里**）；`rt_prio` 回调线程**读回**的真实优先级（-999=读不到；
            `mmcss=False` 但 `rt_prio` 已高 ⇒ 无妨，多半是 PortAudio 自己已注册 MMCSS）；
            `uf_count/uf_after_play` underflow 次数 + 每次距起播秒数（查"起播 20s 内"）；
            `blk_short/blk_long/cb_frames_bad` `pump` 的**静默空隙路径**（块长≠回调帧数）；
            `mmcss_err` MMCSS 注册失败的 Win32 错误码；
            ★ R119-2c `stall_n/stall_max/stall_at` —— **进程级停顿**（看门狗超额次数 /
              最大超额秒数 / 每次距起播秒数）。水面与本进程"一起卡"的直证：
              水面吃的是本进程推的 WS 语义数据，本进程一停它就冻。`stall_at` 用来看
              "是否集中在前 20 秒"。`cb_gap_max` 相邻回调**起点**最大间隔（ms；期望
              ≈23.2ms）—— 即使 PortAudio 不报 underflow，也能看出回调被推后多少；
            `clip_n/peak_max` 硬削波次数 + 混音峰值（**内容层**失真，既有计数器看不见）；
            `q_clear` 音乐队列被清次数（**播放中**多出 ⇒ 内容跳变，`starve` 可能仍为 0）；
          · starve / rt_boosted / mmcss —— ★ 2026-09-19（R119）新增，用于**区分病因**：
            `starve` = `pump()` 取不到块（补静音）的次数 ⇒ **队列被抽空**的直证；
            `rt_boosted` / `mmcss` = RT 回调的优先级保护是否**真的生效**。
            ★ `starve` 与 `output_underflow` **不同源**：前者查生产者，后者查 RT 回调
              延迟（GIL / 线程优先级）。若 `rt_boosted/mmcss` 为 False ⇒ 保护**静默失效**，
              此时把 `output_underflow=False` 读成"没事"是错的（因为压根没护上）。
        """
        try:
            mx = self._mixer
            flags = int(getattr(mx, "_pa_flags", 0) or 0)
            # ⚠ PA_* 状态位常量定义在 **SoftMixer**（RT 回调所在类）上，不在 PlayerEngine
            #   —— 这里必须从 mixer 取，否则 AttributeError（R88 自测第一版就踩了）。
            out_uf = int(getattr(mx, "PA_OUTPUT_UNDERFLOW", 2))
            in_of = int(getattr(mx, "PA_INPUT_OVERFLOW", 1))
            mq = getattr(mx.music, "queue", None)
            return {
                "ok": True,
                "pa_flags": flags,
                "output_underflow": bool(flags & out_uf),
                "input_overflow": bool(flags & in_of),
                "queue_blocks": int(mq.qsize()) if mq is not None else -1,
                "queue_depth": int(getattr(mq, "maxsize", 0) or 0),
                "state": getattr(self._state, "name", str(self._state)),
                "audio_ok": bool(self._audio_ok),
                # ★ 2026-09-19（R119）：只补 R88 没有的三个字段（其余不重复造）
                "starve": int(getattr(getattr(mx, "music", None), "_starve", -1)),
                "rt_boosted": bool(getattr(mx, "_rt_boosted", False)),
                "mmcss": bool(getattr(mx, "_rt_mmcss", False)),
                # ★ R119-2：把"还有一点点卡"变成可读的数（字段含义见 SoftMixer.__init__）
                "rt_prio": int(getattr(mx, "_rt_prio", -999)),
                "cb_us_max": round(float(getattr(mx, "_cb_us_max", 0.0)), 1),
                "cb_us_last": round(float(getattr(mx, "_cb_us_last", 0.0)), 1),
                # ★ R119-2m：三段 max（µs）—— 判读见 __init__ 注释
                "cb_t_pump_max": round(float(getattr(mx, "_cb_t_pump_max", 0.0)), 1),
                "cb_t_out_max": round(float(getattr(mx, "_cb_t_out_max", 0.0)), 1),
                "cb_t_feed_max": round(float(getattr(mx, "_cb_t_feed_max", 0.0)), 1),
                # ★ R119-2n：GC 取证 —— 最长一次 stop-the-world 及其代数（判据见 _GC_STAT 注释）
                "gc_max_ms": float(_GC_STAT["max_ms"]),
                "gc_max_gen": int(_GC_STAT["gen"]),
                "gc_n": int(_GC_STAT["n"]),          # 累计（开机以来）
                # ★ R119-2y：停顿栈取证文件（空 = 未开启）
                "stall_dump": _STALL_DUMP_PATH if _stall_dump_on[0] else "",
                "gc_n_play": int(_GC_STAT.get("n_at_play", 0)),   # ★ 本曲起点时的累计值
                # ★ R119-2o：beats 闸门决策（blocked=播放中被拦 / allowed=放行）
                "beats_gate_blocked": int(_GATE_STAT["blocked"]),
                "beats_gate_allowed": int(_GATE_STAT["allowed"]),
                "cb_n": int(getattr(mx, "_cb_n", 0)),
                "uf_count": int(getattr(mx, "_uf_count", 0)),
                "uf_after_play": list(getattr(mx, "_uf_after_play", [])),
                "blk_short": int(getattr(mx, "_blk_short", 0)),
                "blk_long": int(getattr(mx, "_blk_long", 0)),
                "cb_frames_bad": int(getattr(mx, "_cb_frames_bad", 0)),
                # ★ R119-2b：内容层（硬削波 / 峰值裕量 / 队列被清次数）
                # ★ R119-2c：进程级停顿（"界面水面随声音一起卡、集中在前 20 秒"）
                "stall_n": int(getattr(mx, "_stall_n", 0)),
                "stall_max": round(float(getattr(mx, "_stall_max", 0.0)), 3),
                "stall_at": list(getattr(mx, "_stall_at", [])),
                # ★ 2026-09-21：**停顿点名** —— 每次停顿时"当时有哪笔重活刚结束"。
                #   ops 非空 ⇒ 基本可定案；ops 为空 ⇒ 嫌疑不在 note_heavy 覆盖范围（要看线程栈）。
                #   读法：看最近几条的 ops 名字是否**每次一样**（周期 8.4 s ≈ INDEX_RETRY_S）。
                "stall_culprit": attribute_stalls(
                    list(getattr(mx, "_stall_abs", [])), list(_HEAVY_RING)),
                "cb_gap_max": round(float(getattr(mx, "_cb_gap_max", 0.0)), 1),
                "clip_n": int(getattr(mx, "_clip_n", 0)),
                "peak_max": round(float(getattr(mx, "_peak_max", 0.0)), 4),
                "q_clear": int(getattr(getattr(mx, "music", None), "_clear_n", -1)),
                "mmcss_err": int(_MMCSS_LAST_ERR),
                # ★ R119-2g：空闲期"无谓重查设备"的累计次数（退避修好后应涨得很慢）
                "dev_reinit_n": int(getattr(mx, "_dev_reinit_n", -1)),
                # ★ R119-2h：最近若干笔"重活"按耗时倒序 [[名字, 毫秒], ...]
                #   ⇒ 一眼看出整进程冻结是谁干的（VA 推理 / CLAP 段索引 / …）
                #   每笔 = [名字, 毫秒, **距起播秒数**]（无起播参照时给 -1）
                #   ⇒ 可直接与 `stall_at` 对表："这次停顿是哪笔重活干的"
                "heavy": sorted(
                    ([[n, ms, (round(t - mx._play_t0, 2) if mx._play_t0 else -1)]
                      for t, n, ms in list(_HEAVY_RING)]),
                    key=lambda x: -x[1])[:6],
            }
        except Exception as e:  # noqa 诊断字段绝不拖垮调用方
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ================= 频谱分析（音频响应壁纸） =================
    def set_spectrum_enabled(self, on: bool) -> None:
        """开/关频谱 + P2 特征帧分析（无订阅时关闭可省 RT/线程开销）。线程安全。

        P2：feat 特征帧通道与频谱共用同一订阅闸门（有 WS 客户端才开），
        桥接层只调这一个入口，两通道同步启停。
        R80 爆音二分开关：环境变量 MUSICAI_FEAT_OFF=1 一键关特征通道
        （频谱照常），前端水面自动回退 10Hz 频谱通道，观感不变——
        用于确诊「特征线程是否加剧 underrun」。
        """
        try:
            self._mixer.spec.enabled = bool(on)
        except Exception:  # noqa
            pass
        try:
            feat_on = bool(on) and os.environ.get("MUSICAI_FEAT_OFF", "0") != "1"
            self._mixer.feat.enabled = feat_on
        except Exception:  # noqa
            pass

    def read_spectrum(self) -> list[float] | None:
        """最近一帧频段能量（0~1，len=SPEC_BANDS）；未开/无数据返回 None。"""
        try:
            s = self._mixer.spec
            if not s.enabled:
                return None
            return s.read()
        except Exception:  # noqa
            return None

    def read_feature_frame(self) -> dict | None:
        """P2：最近一帧实时特征 {seq,t,rms,bass,mid,high,flux,onset}；未开返回 None。"""
        try:
            f = self._mixer.feat
            if not f.enabled:
                return None
            return f.read()
        except Exception:  # noqa
            return None

    def read_recent_mono(self, seconds: float):
        """R83b：最近 seconds 秒单声道音频（L1 短期语义追踪用）。

        未开/无新数据（暂停）/不足 → None。仅 mood 线程（非 RT）调用。
        """
        try:
            f = self._mixer.feat
            if not f.enabled:
                return None
            return f.read_recent_mono(seconds)
        except Exception:  # noqa
            return None

    def shutdown(self) -> None:
        self._stop_flag.set()
        self._fill_stop.set()
        self._monitor_stop.set()
        self._state = PlayerState.STOPPED
        try:
            self._mixer.close()
        except Exception:  # noqa
            pass
