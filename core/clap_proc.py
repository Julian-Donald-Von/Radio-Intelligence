#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 2026-09-21：CLAP **段索引**的常驻子进程 worker（根治"每做一次分段检索就卡一次"）。

为什么必须换进程（真机证据）
  · `/api/status` 的 `heavy` 环：`clap_index` 单次 **~290 ms**，且重复发生；
  · 同一时刻 `stall_stacks.log` 的看门狗记录 **0.2~0.3 s 的进程级停顿**
    （`### STALL over=0.2~0.3s`，`stall_n` 连续增长）—— 与用户"每做一次分段检索，
    播放器都会卡一次"完全吻合；
  · 机制：`SegmentIndexer.index_track` 在**本进程**里做 `librosa.load`（解码）
    + `model.get_audio_embedding_from_data`（CLAP 前向）。只要在本进程里跑，就必然
    占住 GIL / 抢 CPU；线程优先级（已压 LOWEST）与切片预算（已收到 0.25 s）都**只能减轻**。
  · 与 VA 推理是同一条处置（`core/va_proc.py`，2026-09-19），区别是 CLAP 权重加载贵
    ⇒ 必须**常驻**，不能按请求起进程。

设计（沿 `va_proc.py` 的**文件 spool** 协议，但常驻）
  · 父进程：`.submit()` 写 `req_<seq>.json`；`.collect()` 轮询 `res_<seq>.json`
    （轮询期间 `time.sleep` ⇒ **GIL 全程释放**，本进程零卡顿）。
  · 子进程：`python -m core.clap_proc serve <spool>`；**CUDA_VISIBLE_DEVICES=-1 强制 CPU**
    （整卡只剩 ~0.2 GB，父进程的 CLAP 已在显存里，子进程上 GPU 必 OOM）。
    ⚠ `CUDA_VISIBLE_DEVICES=""`（空串）在 Windows 上**不生效**（实测 `is_available()` 仍为
    True、仍在吃显存）—— 必须用 **`-1`**。
    实测（`_probe_clap_cpu_cost.py`，4 线程，AfterFirst 稳态）：
      **解码 ~45 ms + CLAP 前向 ~170 ms ≈ 215 ms/段** ⇒ 整曲 38 段 **~18~20 s**，纯后台。
    ⇒ 比现状**更快且零阻塞**：现状是每片 0.25 s 预算在**本进程**里跑（每片 290 ms 停顿），
      且片间还要 `SEG_GAP_S=2 s` 让路 ⇒ 整曲铺满要 ~85 s。
  · **失败即回退**：worker 未就绪 / 超时 / 报错 / 结果不完整 ⇒ 调用方走原来的**本进程**路径
    （`MUSICAI_SEG_SUBPROC=0` 也可以显式关掉）。最坏情况 = 今天的行为，不会更差。

协议（JSON，行内不传二进制）
  请求 `req_<seq>.json`：{"seq":n, "path":str, "segs":[[k, offset_ms], ...],
                          "sr":int, "win":float, "budget_s":float}
  响应 `res_<seq>.json`：{"ok":true, "rows":[[k, start_ms, b64_fp16], ...], "added":n}
                        / {"ok":false, "error":str}
"""
from __future__ import annotations

import base64
import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time

logger = logging.getLogger("MusicAI.ClapProc")

SPOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data_runtime", "clap_spool")
_HEART = os.path.join(SPOOL, "parent.heartbeat")
_IDLE_EXIT_S = 1800.0       # 子进程空闲多久自杀（父进程没了也别留孤儿）
#   ★ 2026-09-21：900 → 1800。父侧现在会 `defer` 而不是回退本进程（见
#   `core/semantic_axis.py`），所以即使这里退出也**不产生卡顿**，只是语义
#   晚 ~2 s 到位；但仍不希望一动albums 就冷启动，故放宽到 30 min。
_HB_PERIOD_S = 10.0         # 父进程心跳线程的周期（秒）
#   ★ 10 s（原 30 s）：子阈 45 s ⇒ **4 倍余量**；父进程被强杀后，孤儿 worker
#   最长存活也从 180 s 压到 45 s。
_HEART_STALE_S = 45.0       # 心跳超过这么久没动 ⇒ 判父进程已死，子进程退出
_READY_TMO_S = 300.0        # 首启（含 CLAP 加载）最长等待
_WB_PERIOD_S = 1.0          # 子进程写 `worker.heartbeat` 的最小间隔（秒）
_WB_STALE_S = 6.0           # `worker.heartbeat` 超过这么久没动 ⇒ worker 已死

_SEQ = [0]
_LOCK = threading.Lock()
_PROC: list = [None]        # [Popen]（单槽列表：闭包内可写；空列表会 IndexError）
_READY: list = [False]      # [bool] —— worker 已上报 ready（模型加载完）
_HB_THREAD: list = [None]   # [Thread] 父进程心跳线程（只起一条）
_REAL_PID: list = [0]       # [int] worker **真解释器**的 pid（壳的 pid 不算）


def enabled() -> bool:
    """总开关（`MUSICAI_SEG_SUBPROC=0` 一键回本进程）。"""
    return os.environ.get("MUSICAI_SEG_SUBPROC", "1") != "0"


def _req_path(seq: int) -> str:
    return os.path.join(SPOOL, "req_%08d.json" % seq)


def _res_path(seq: int) -> str:
    return os.path.join(SPOOL, "res_%08d.json" % seq)


def _ready_flag() -> str:
    return os.path.join(SPOOL, "ready")


def _wbeat_path() -> str:
    """★★ 子进程「我还活着」的时间戳文件。

    为什么必须另立一个文件：`venv/Scripts/python.exe` 是**壳** ⇒
    `Popen.pid != 子进程真实 pid`（实测 2568 vs 21800），所以
    "旗里的 pid == Popen.pid" 这种判活方式**在本机恒假**。
    改成"子进程自己持续跳"——与 pid 无关，跨壳/跨重启都稳。"""
    return os.path.join(SPOOL, "worker.heartbeat")


def _ensure_spool() -> None:
    os.makedirs(SPOOL, exist_ok=True)


def heartbeat() -> None:
    """父进程「我还活着」（调用方在每个 mood tick 里顺手调；失败静默）。"""
    try:
        _ensure_spool()
        with open(_HEART, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except Exception:  # noqa
        pass


def _heartbeat_loop() -> None:
    """每 `_HB_PERIOD_S` 跳一次。

    ⚠ 用**分段 sleep**（≤1 s 一片）而不是一次 `sleep(_HB_PERIOD_S)`：
    否则周期被调小时，已经进入的长 sleep 还得走完 —— 验收 A2 就这么被误判红过。
    """
    while True:
        heartbeat()
        t_end = time.time() + float(_HB_PERIOD_S)
        while time.time() < t_end:
            time.sleep(0.5)


def _ensure_heartbeat() -> None:
    """★ 起一条**心跳线程**：把"父进程还活着"这件事与业务调用**解耦**。

    ⚠ 为什么必须有（真机踩到）：旧实现里 `heartbeat()` 只在 `submit/collect/
    `wait_ready/ensure_worker` 内被调；于是**只要十几分钟没有段索引请求**
    （听一张专辑的中段、或一曲索引已铺满），worker 就按 `_HEART_STALE_S=180`
    判定"父已死"而自杀 —— 下一次换曲时它得冷启动（~2 s 加载 CLAP），
    期间 `submit()` 返回 None ⇒ 落回**本进程**索引 ⇒ 又是 ~0.35 s 进程级停顿。
    真机 `stall_culprit` 抓到的那 6 笔 `clap_index`（330~357 ms）就是它。
    """
    if _HB_THREAD[0] is not None:
        return
    t = threading.Thread(target=_heartbeat_loop, name="ClapProcHB", daemon=True)
    _HB_THREAD[0] = t
    t.start()


def last_flag() -> dict:
    """读就绪旗的原始内容（**不验活**）；坏了给 {}。"""
    try:
        return json.loads(open(_ready_flag(), encoding="utf-8").read())
    except Exception:  # noqa
        return {}


def ready() -> bool:
    """worker 就绪 **且进程确实活着**。

    ★★ 旧实现只看旗文件在不在 ⇒ **旗会撒谎**：worker 退出后旗仍在，于是 `ready()`
    恒 True、`ready_detail()` 还会报一个**早已不存在的 pid**（实测：旗写 pid=26800，
    进程表里根本没有 26800）。后果是 `ensure_worker` 的重启逻辑等于被废掉，
    整条子进程方案静默失效、退回本进程卡顿。

    新判据**两条都要满足**：
      ① `_PROC[0]` 这个 Popen 还在跑（`poll() is None`）；
      ② 旗里的 `pid` == **该 Popen 的 pid**（旗是上一代 worker 留下的 ⇒ 不算数）。
    """
    p = _PROC[0] if _PROC else None
    if p is None or p.poll() is not None:
        _READY[0] = False
        return False
    # ★★ 别再比 pid：`venv/Scripts/python.exe` 是**壳**（`pyvenv.cfg` 的
    #   `command = <base python> -m venv ...`）⇒ `Popen.pid` 是壳的 pid、
    #   旗里是**真解释器**的 pid，两者**永不相等**（实测 2568 vs 21800）。
    #   第一版就是栽在这上面：整条 worker 方案被静默废掉，`wait_ready` 连续超时。
    #   改用**子进程自己的心跳**判活 —— 与 pid 无关，外壳/重启都不影响。
    if not worker_beating():
        _READY[0] = False
        return False
    try:
        d = last_flag()
        if bool(d.get("ok")):
            try:
                _REAL_PID[0] = int(d.get("pid") or 0)
            except Exception:  # noqa
                pass
            _READY[0] = True
            return True
    except Exception:  # noqa
        pass
    _READY[0] = False
    return False


def worker_beating() -> bool:
    """`worker.heartbeat` 是否新鲜（⇒ 子进程真在跑，与 pid 无关）。"""
    try:
        return (time.time() - os.path.getmtime(_wbeat_path())) <= _WB_STALE_S
    except Exception:  # noqa
        return False


def status() -> str:
    """`off`（开关关）/ `idle`（还没拉起来）/ `loading` / `ready` / `down`。

    调用方（`SegmentIndexer._worker_rows`）据此区分「正在加载 ⇒ 本 tick 让路」
    与「硬失败 ⇒ 回退本进程」—— 这两者的处置**完全不同**。
    """
    if not enabled():
        return "off"
    p = _PROC[0] if _PROC else None
    if p is None:
        return "idle"
    if p.poll() is not None:
        return "down"
    # ⚠ 壳进程的 `poll()` 只说明**壳**在；壳退出而真解释器还在（或反之）都可能。
    #   所以"down"再补一条：心跳已停 = 真死了。
    if not worker_beating():
        return "ready" if ready() else "down"
    return "ready" if ready() else "loading"


def ready_detail() -> dict:
    """worker 自报就绪详情（`device`/`cuda`/`load_s`/`pid`）；**未验活则给 {}**。

    ★ 为什么要有：**"子进程到底走没走 CPU"必须可自证** —— 靠外部看整卡占用会被
    应用自身的显存波动污染（实测验过一次假红：+616 MiB 其实来自应用侧）。
    ★ 但自证本身也必须**先验活**：否则死掉的 worker 留下的旗会报出一个不存在的 pid。
    """
    if not ready():
        return {}
    return last_flag()


def alive() -> bool:
    p = _PROC[0] if _PROC else None
    return bool(p is not None and p.poll() is None)


def ensure_worker(start: bool = True) -> bool:
    """幂等拉起 worker；返回"进程在"（不代表模型已就绪 —— 那个看 `ready()`）。"""
    if not enabled():
        return False
    _ensure_heartbeat()          # ★ 先把心跳线程立起来，再谈拉起 worker
    if alive():
        return True
    with _LOCK:
        if alive():
            return True
        try:
            _ensure_spool()
            # 清掉上一轮的残留：半截请求会让 worker 白跑；res_* 是没人取的旧结果
            for pat in ("req_*.json", "res_*.json"):
                for f in glob.glob(os.path.join(SPOOL, pat)):
                    try:
                        os.remove(f)
                    except Exception:  # noqa
                        pass
            # ★★ 关键：**删掉上一代的 ready 旗 + worker 心跳**。否则新 worker
            #   还在加载 CLAP 时 `ready()` 就会因为"旗仍在 / 心跳还新鲜"而返回 True。
            for _f in (_ready_flag(), _wbeat_path()):
                try:
                    os.remove(_f)
                except Exception:  # noqa
                    pass
            _READY[0] = False
            if start:
                env = dict(os.environ)
                # ★ 强制 CPU：整卡只剩 ~0.2 GB，子进程上 GPU 必 OOM
                env["CUDA_VISIBLE_DEVICES"] = "-1"
                env["OMP_NUM_THREADS"] = env.get("MUSICAI_CLAPPROC_THREADS", "4")
                env["MKL_NUM_THREADS"] = env["OMP_NUM_THREADS"]
                env["OPENBLAS_NUM_THREADS"] = env["OMP_NUM_THREADS"]
                env["TOKENIZERS_PARALLELISM"] = "false"
                flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                p = subprocess.Popen(
                    [sys.executable, "-m", "core.clap_proc", "serve", SPOOL],
                    cwd=root, env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=flags)
                _PROC[0] = p
                _READY[0] = False        # 旗刚被删；等它带着**新 pid**出现才算就绪
                heartbeat()
                logger.info("[CLAPProc] worker 已启动 pid=%s（CPU）", p.pid)
        except Exception as e:  # noqa
            logger.warning("[CLAPProc] worker 启动失败: %s", e)
            return False
    return alive()


def wait_ready(timeout: float = _READY_TMO_S, cancel=None) -> bool:
    """等到 `ready` 旗出现（首启要加载 CLAP 权重，实测 ~3~30 s）。父进程只 sleep ⇒ 不占 GIL。"""
    if ready():
        return True
    if not ensure_worker():
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ready():
            return True
        if not alive():
            return False
        if cancel is not None:
            try:
                if cancel():
                    return False
            except Exception:  # noqa
                pass
        heartbeat()
        time.sleep(0.2)
    return False


def submit(path: str, segs: list, sr: int, win: float,
           budget_s: float = 0.0) -> int | None:
    """投一个任务，返回 seq（None = 投不出去 ⇒ 调用方走本进程回退）。

    ⚠ **不在这里 `wait_ready()`**：首启要加载 CLAP 权重（实测 1.6~30 s），若在此阻塞会
    把调用方（mood 线程）堵住 —— 那正是我们要消灭的东西。改为"未就绪就返回 None"，
    调用方本 tick 回退本进程、下个 tick 再试 ⇒ 加载期零阻塞、零风险。"""
    if not enabled() or not segs:
        return None
    if not ensure_worker():
        return None
    if not ready():
        return None
    with _LOCK:
        _SEQ[0] += 1
        seq = _SEQ[0]
    req = {"seq": seq, "path": str(path),
           "segs": [[int(k), int(off)] for k, off in segs],
           "sr": int(sr), "win": float(win), "budget_s": float(budget_s)}
    tmp = _req_path(seq) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(req, f)
        os.replace(tmp, _req_path(seq))          # 原子发布：worker 不会读到半截
        heartbeat()
        return seq
    except Exception as e:  # noqa
        logger.warning("[CLAPProc] 投递失败: %s", e)
        return None


def collect(seq: int, timeout: float = 60.0) -> list | None:
    """等结果。返回 `[(seg_idx, start_ms, fp16_bytes), ...]`；None = 失败/超时（调用方回退）。"""
    t0 = time.time()
    rp = _res_path(seq)
    while time.time() - t0 < timeout:
        if os.path.isfile(rp):
            try:
                raw = open(rp, encoding="utf-8").read()
            except Exception:  # noqa
                time.sleep(0.02)
                continue
            finally:
                try:
                    os.remove(rp)
                except Exception:  # noqa
                    pass
            try:
                d = json.loads(raw)
            except Exception as e:  # noqa
                logger.warning("[CLAPProc] 结果解析失败: %s", e)
                return None
            if not d.get("ok"):
                logger.warning("[CLAPProc] worker 报错: %s", d.get("error"))
                return None
            out = []
            for k, start_ms, b64 in d.get("rows", []):
                try:
                    out.append((int(k), int(start_ms), base64.b64decode(b64)))
                except Exception:  # noqa
                    return None
            return out
        if not alive():
            _READY[0] = False        # ★ 进程没了 ⇒ 就绪状态立即失效（旗可能还在）
            logger.warning("[CLAPProc] worker 已退出（rc=%s）",
                           _PROC[0].poll() if _PROC else "?")
            return None
        heartbeat()
        time.sleep(0.02)          # ★ 释放 GIL：本进程在等结果期间完全可响应
    logger.warning("[CLAPProc] 超时（%.0fs seq=%d）", timeout, seq)
    return None


def _kill_tree(pid: int) -> None:
    """Windows 上整棵树杀掉（`/T`）。壳进程被杀不等于真解释器退出。"""
    if not pid:
        return
    try:
        import signal
        os.kill(int(pid), signal.SIGTERM)
    except Exception:  # noqa 退路
        pass
    if os.name != "nt":
        return
    try:
        import subprocess as _sp
        _sp.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10)
    except Exception:  # noqa
        pass


def shutdown() -> None:
    """收掉 worker —— **优先按真 pid 杀整棵树**。

    ⚠ 只 `Popen.kill()` 会杀**壳**；真解释器可能变孤儿继续轮询 `req_*.json`
    （两只 worker 同时消费同一请求 = 重复算 + 结果互相覆盖）。
    """
    real = int(_REAL_PID[0] or 0)
    if real:
        _kill_tree(real)
    p = _PROC[0] if _PROC else None
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except Exception:  # noqa
            pass
    try:
        _kill_tree(p.pid if p is not None else 0)
    except Exception:  # noqa
        pass
    _READY[0] = False
    _REAL_PID[0] = 0


# ────────────────────────── 子进程侧 ──────────────────────────

_EX = [None]


_WB_LAST = [0.0]


def _beat_self() -> None:
    """子进程写 `worker.heartbeat`（**≥1 s 一次**，失败静默）。

    父进程的 `ready()` 靠它判活 —— 这是唯一不依赖 pid 的活体证据。
    """
    try:
        now = time.time()
        if now - _WB_LAST[0] < _WB_PERIOD_S:
            return
        _WB_LAST[0] = now
        with open(os.path.join(SPOOL, "worker.heartbeat"), "w",
                  encoding="utf-8") as f:
            f.write(str(int(now)))
    except Exception:  # noqa
        pass


def _lower_self_priority() -> None:
    """子进程把自己降到 BELOW_NORMAL（别跟播放抢 CPU）。失败静默。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.SetPriorityClass.restype = wintypes.BOOL
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL
    except Exception:  # noqa 纯调度提示
        pass


def _extractor():
    if _EX[0] is None:
        from core.audio_features import AudioFeatureExtractor
        ex = AudioFeatureExtractor(preload=False)
        ex.preload_clap_async()
        _EX[0] = ex
    return _EX[0]


def _compute(req: dict) -> dict:
    """子进程内：解码 + CLAP 前向。**单段推理时长就是下限**（跑完当前段再收尾）。"""
    import numpy as np
    ex = _extractor()
    if not ex.clap_ready():
        return {"ok": False, "error": "CLAP 未就绪"}
    try:
        import librosa
    except Exception as e:  # noqa
        return {"ok": False, "error": "librosa 不可用: %s" % e}
    model = ex._clap_model
    sr = int(req["sr"]); win = float(req["win"]); budget = float(req.get("budget_s") or 0.0)
    rows = []
    t0 = time.monotonic()
    for k, off_ms in req["segs"]:
        try:
            wav, _ = librosa.load(req["path"], sr=sr, mono=True,
                                  offset=float(off_ms) / 1000.0, duration=win,
                                  dtype=np.float32)
        except Exception:  # noqa 单段失败 → 中止（保留已完成段）
            break
        wav = np.ascontiguousarray(np.asarray(wav, dtype=np.float32).reshape(-1))
        if wav.size < sr:            # 尾段 <1s 无语义（与主进程同判据）
            break
        emb = model.get_audio_embedding_from_data([wav])
        vec = np.asarray(emb, dtype=np.float32).reshape(-1)
        nrm = float(np.linalg.norm(vec))
        if nrm > 1e-9:
            vec = vec / nrm
        rows.append([int(k), int(off_ms),
                     base64.b64encode(vec.astype(np.float16).tobytes()).decode("ascii")])
        del wav
        if budget > 0 and (time.monotonic() - t0) >= budget:
            break
    return {"ok": True, "rows": rows, "added": len(rows)}


def _serve(spool: str) -> int:
    global SPOOL, _HEART, _IDLE_EXIT_S, _HEART_STALE_S
    SPOOL = spool
    _HEART = os.path.join(SPOOL, "parent.heartbeat")
    _lower_self_priority()
    os.makedirs(SPOOL, exist_ok=True)
    logger.info("[CLAPProc] worker 就绪，开始加载 CLAP（CPU）…")
    last_beat = time.time()
    # 先把模型加载完再挂 ready 旗（父进程据此避免"第一发必超时"）
    try:
        ex = _extractor()
        t0 = time.time()
        while not ex.clap_ready() and time.time() - t0 < _READY_TMO_S:
            time.sleep(0.2)
        if ex.clap_ready():
            # ★ 把**自己真实用的设备**写进 ready 旗 —— "子进程到底走没走 CPU"必须可自证，
            #   不能靠外部看整卡占用（父进程/应用自身也在涨显存 ⇒ 读数噪声大，验过一次假红）。
            dev = "unknown"
            try:
                import torch
                cuda = bool(torch.cuda.is_available())
                try:
                    dev = str(next(ex._clap_model.parameters()).device)
                except Exception:  # noqa
                    dev = "cuda" if cuda else "cpu"
            except Exception:  # noqa
                cuda = False
            # ★ 先让父进程看见"我在跳"，再挂就绪旗（`ready()` 两个都要）
            _beat_self()
            with open(_ready_flag(), "w", encoding="utf-8") as f:
                json.dump({"ok": True, "device": dev, "cuda": cuda,
                           "load_s": round(time.time() - t0, 1),
                           "pid": os.getpid()}, f)
            logger.info("[CLAPProc] CLAP 就绪（%.1f s，device=%s，cuda=%s，pid=%s）",
                        time.time() - t0, dev, cuda, os.getpid())
        else:
            logger.warning("[CLAPProc] CLAP 加载超时")
            return 1
    except Exception as e:  # noqa
        logger.warning("[CLAPProc] CLAP 加载失败: %s", e)
        return 1
    while True:
        _beat_self()                       # ★ 每 ≤1 s 跳一次（父进程据此判活）
        try:
            reqs = sorted(glob.glob(os.path.join(SPOOL, "req_*.json")))
        except Exception:  # noqa
            reqs = []
        if not reqs:
            # 父进程心跳超时（父已死）或自身空闲过久 ⇒ 退出，别留孤儿
            try:
                if os.path.isfile(_HEART):
                    if time.time() - os.path.getmtime(_HEART) > _HEART_STALE_S:
                        logger.info("[CLAPProc] 父进程心跳过期 → 退出")
                        return 0
                elif time.time() - last_beat > 60:
                    return 0
            except Exception:  # noqa
                pass
            if time.time() - last_beat > _IDLE_EXIT_S:
                logger.info("[CLAPProc] 空闲 %.0fs → 退出", _IDLE_EXIT_S)
                return 0
            time.sleep(0.05)
            continue
        last_beat = time.time()
        for rp in reqs:
            try:
                req = json.loads(open(rp, encoding="utf-8").read())
            except Exception:  # noqa
                try:
                    os.remove(rp)
                except Exception:  # noqa
                    pass
                continue
            seq = int(req.get("seq") or 0)
            try:
                res = _compute(req)
            except Exception as e:  # noqa
                res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            outp = os.path.join(SPOOL, "res_%08d.json" % seq)
            try:
                with open(outp + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(res, f)
                os.replace(outp + ".tmp", outp)
            except Exception:  # noqa
                pass
            try:
                os.remove(rp)
            except Exception:  # noqa
                pass


def _cli(argv: list) -> int:
    if len(argv) >= 3 and argv[1] == "serve":
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        return _serve(argv[2])
    print("usage: python -m core.clap_proc serve <spool_dir>")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
else:
    try:
        import atexit as _atexit
        _atexit.register(shutdown)     # 父进程正常退出 ⇒ 顺手收掉 worker
    except Exception:  # noqa
        pass
