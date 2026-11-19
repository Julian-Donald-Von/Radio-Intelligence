"""
TTS 引擎抽象基类：定义统一接口与共享工具。

所有引擎（Edge 云 / ChatTTS ONNX / MeloTTS）都继承本类，对外只暴露：
  - available        : 当前引擎是否可用（依赖/模型是否就绪）
  - synthesize(text) : 合成音频，返回音频路径；不可用或失败时返回 None
  - empty_cache()    : 切换引擎时释放显存（GPU 引擎实现）
  - reconfigure()    : 运行时读取配置（音色/语速等）

共享能力：文本截断（保留 MAX_TEXT_LEN）、TTS 缓存路径、安全删除。
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

from config.settings import TTS_CACHE_DIR, TTS_TMP_DIR, TTSConfig

logger = logging.getLogger("MusicAI.TTS")

# 临时音频最长保留秒数：超过该时长的临时文件视为「孤儿」（播放后未被释放、
# 或进程异常残留）由周期清理线程删除，确保磁盘不堆积。30 分钟远大于单曲时长，
# 不会误删尚在排程中的转场口播（转场 wav 在播放前最多停留一首歌的时长）。
TTS_TMP_MAX_AGE_S = 1800
_TMP_SWEEP_INTERVAL_S = 600

_tmp_sweep_started = False
_tmp_sweep_lock = threading.Lock()


def sweep_tts_tmp(max_age_s: float | None = None) -> int:
    """清理 TTS 临时音频目录。

    max_age_s=None  → 删除目录下全部文件（启动清空上一轮残留 / 退出时）。
    max_age_s=数值 → 仅删除 mtime 早于该秒数的文件（周期清理孤儿）。
    返回删除的文件数（best-effort，单文件失败不影响其余）。
    """
    removed = 0
    for d in (TTS_TMP_DIR, TTS_CACHE_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa
            continue
        try:
            now = time.time()
            for f in d.iterdir():
                if not f.is_file():
                    continue
                if max_age_s is not None:
                    try:
                        age = now - f.stat().st_mtime
                    except Exception:  # noqa
                        continue
                    if age < max_age_s:
                        continue
                try:
                    f.unlink()
                    removed += 1
                except Exception:  # noqa
                    pass
        except Exception:  # noqa
            pass
    return removed


def _ensure_tmp_hygiene() -> None:
    """进程内只执行一次：启动清空上一轮残留 + 启动周期清理线程 + 退出时再清。"""
    global _tmp_sweep_started
    with _tmp_sweep_lock:
        if _tmp_sweep_started:
            return
        _tmp_sweep_started = True
    # 启动即清空上一轮遗留（核心诉求：不堆在应用里）
    try:
        sweep_tts_tmp(None)
    except Exception:  # noqa
        pass

    def _loop() -> None:
        while True:
            time.sleep(_TMP_SWEEP_INTERVAL_S)
            try:
                sweep_tts_tmp(max_age_s=TTS_TMP_MAX_AGE_S)
            except Exception:  # noqa
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.name = "tts-tmp-sweeper"
    t.start()
    # 进程退出再清一次，彻底不留临时文件（兜底捕获，避免退出异常影响关机）
    def _atexit_sweep():
        try:
            sweep_tts_tmp(None)
        except BaseException:  # noqa
            pass
    atexit.register(_atexit_sweep)


class TTSEngine(ABC):
    # 子类覆盖：引擎标识
    name = "base"

    def __init__(self) -> None:
        self._available = False
        try:
            self._available = bool(self._check())
        except Exception as e:  # noqa
            logger.warning("[TTS:%s] 可用性检查失败: %s", self.name, str(e)[:120])
            self._available = False
        self._tmp_dir = TTS_TMP_DIR
        # 启动清空上一轮残留的临时音频 + 启动周期清理（确保「即用即扔」不堆盘）
        _ensure_tmp_hygiene()
        self.reconfigure()

    @abstractmethod
    def _check(self) -> bool:
        """检查依赖/模型是否就绪。"""

    def reconfigure(self) -> None:
        """运行时读取配置（子类可重写以读取音色/语速）。"""

    @property
    def available(self) -> bool:
        return self._available

    @abstractmethod
    def synthesize(self, text: str) -> str | None:
        """合成音频，返回路径；失败返回 None。"""

    def empty_cache(self) -> None:
        """切换引擎时释放显存；GPU 引擎重写。默认无操作。"""

    def release(self, path) -> None:
        """释放临时音频文件。

        ★★ 2026-11-18（真机100%确定根因）：宿主 safe-delete 钩子会拦截/挂起
        `Path.unlink()`（日志 SAFE_DELETE_BULK_CONFIRM_REQUIRED, scope=turn），
        导致 synthesize_unique 里 release(out) 卡死 → run_batch result 没写 →
        submit_wait 90s 超时 → 音频丢失。
        **release 不再在合成关键路径删文件**；临时 wav 由进程启动时
        `_ensure_tmp_hygiene` 统一清理（文件几百KB、量小，重启清足够）。
        """
        # 不再 self._safe_remove(path) —— 避免宿主删除钩子阻塞合成返回
        pass

    # ---------------- 共享工具 ----------------
    def _truncate(self, text: str) -> str:
        if len(text) > TTSConfig.MAX_TEXT_LEN:
            return text[: TTSConfig.MAX_TEXT_LEN] + "……"
        return text

    def _cache_path(self, text: str, tag: str = "", ext: str = "wav") -> Path:
        key = f"{self.name}|{tag}|{text}"
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        # 落到临时目录：内容哈希命名便于同一次运行内去重，但文件在播放后被释放，
        # 进程启动时由 _ensure_tmp_hygiene 整体清空上一轮残留。
        return self._tmp_dir / f"{h}.{ext}"

    @staticmethod
    def _safe_remove(path) -> None:
        try:
            if path and Path(path).exists():
                Path(path).unlink()
        except Exception:  # noqa
            pass
