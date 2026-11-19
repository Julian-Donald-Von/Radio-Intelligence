#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ R119-2u：MER VA 推理的**子进程**入口（与 beats 同法，根治 GIL 占用）。

为什么必须换进程：`audio_to_emotion_latent` 在**本进程**里实测 **1.3~1.6 秒**，
且真机采样里有一笔落在**播放中**（`va_infer 1584µs @ 距起播 +3.17s`），
与起播后 **1.88 秒**那次停顿的时间窗完全吻合。它和 beats 一样：只要在本进程里跑，
就必然占住 GIL（优先级/闸门都救不了）。

- CLI：`python -m core.va_proc <audio> <out.json> <n_windows>`（只算、不碰别的）
- 库用法：`run(path, n_windows, timeout, cancel)` —— 父进程 `Popen + poll()` 轮询
  （**等待期间释放 GIL** ⇒ 零卡顿），`cancel()` 为真时**直接 kill**（瞬时）。
- 子进程自己把**进程**优先级降到 BELOW_NORMAL（用**显式 argtypes** 的
  `SetPriorityClass`：伪句柄 -1 在 64 位下按 32 位传参会被截断 —— 同族坑见
  `core/audio_engine._win_kernel32`）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger("MusicAI.VAProc")

VA_SUBPROC_TIMEOUT_S = 180.0
_SEQ = [0]
_SEQ_LOCK = threading.Lock()


def _lower_self_priority() -> None:
    """★ 子进程把自己降到 BELOW_NORMAL（别跟播放抢 CPU）。失败静默。"""
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


def compute(audio_path: str, n_windows: int = 1) -> dict:
    """纯计算（子进程内调用）：返回 `{"ok":bool,...}`。"""
    try:
        from mer_emotion_frontend import audio_to_emotion_latent
        lat = audio_to_emotion_latent(audio_path, use="local_va",
                                      n_windows=int(n_windows))
        return {"ok": True,
                "valence": float(lat["valence"]),
                "arousal": float(lat["arousal"])}
    except Exception as e:  # noqa
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def run(audio_path: str, n_windows: int = 1,
        timeout: float | None = None, cancel=None) -> dict | None:
    """★ 在**独立子进程**里算一次 VA（父进程只轮询，GIL 全程释放）。

    `cancel`：可选的无参回调，返回 True ⇒ **直接 kill 子进程**并返回 None。
    返回 `{"valence":.., "arousal":..}`（成功）或 None（失败/被取消/超时）。
    """
    with _SEQ_LOCK:
        _SEQ[0] += 1
        seq = _SEQ[0]
    out = os.path.join(tempfile.gettempdir(),
                       "musicai_va_%d_%d.json" % (os.getpid(), seq))
    try:
        os.remove(out)
    except Exception:  # noqa
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
    cmd = [sys.executable, "-m", "core.va_proc", str(audio_path), out, str(int(n_windows))]
    try:
        p = subprocess.Popen(cmd, cwd=root, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception as e:  # noqa
        logger.warning("[VA] 子进程启动失败: %s", e)
        return None
    lim = float(VA_SUBPROC_TIMEOUT_S if timeout is None else timeout)
    t0 = time.time()
    try:
        while True:
            if p.poll() is not None:
                break
            if cancel is not None:
                try:
                    if cancel():
                        p.kill()
                        logger.info("[VA] 起播抢占 → **kill** VA 子进程（瞬时）")
                        return None
                except Exception:  # noqa
                    pass
            if time.time() - t0 > lim:
                p.kill()
                logger.warning("[VA] VA 子进程超时（%.0fs）→ 放弃", lim)
                return None
            time.sleep(0.2)                     # 释放 GIL：等待期间父进程保持响应
    finally:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:  # noqa
            pass
    if p.returncode != 0 or not os.path.isfile(out):
        logger.warning("[VA] VA 子进程未产出结果（rc=%s）", p.returncode)
        return None
    try:
        d = json.loads(open(out, encoding="utf-8").read())
        if not isinstance(d, dict) or not d.get("ok"):
            logger.warning("[VA] VA 子进程失败: %s", (d or {}).get("error"))
            return None
        return {"valence": float(d["valence"]), "arousal": float(d["arousal"])}
    except Exception as e:  # noqa
        logger.warning("[VA] VA 结果解析失败: %s", e)
        return None
    finally:
        try:
            os.remove(out)
        except Exception:  # noqa
            pass


def _cli(argv: list[str]) -> int:
    if len(argv) < 3:
        return 2
    _lower_self_priority()
    d = compute(argv[1], int(argv[3]) if len(argv) > 3 else 1)
    open(argv[2], "w", encoding="utf-8").write(json.dumps(d))
    return 0 if d.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli(sys.argv))
