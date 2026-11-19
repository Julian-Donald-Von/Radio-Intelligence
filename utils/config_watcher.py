"""
配置热重载监视器：轮询 config.json 的 mtime，变化时回调已注册的回调。

满足部署规范「所有配置必须通过 config.json 读取，支持热重载」：
  - 用户在设置页保存 → config.json 落盘 → 监视器检测到 mtime 变化 →
    触发回调（重新 reload 配置 + 各模块 reconfigure），无需重启应用。

回调在主线程之外（守护线程）触发，回调内自行保证线程安全。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from config.settings import USER_CONFIG_PATH

logger = logging.getLogger("MusicAI.Config")


class ConfigWatcher:
    def __init__(self, path=USER_CONFIG_PATH, interval: float = 2.0) -> None:
        self.path = Path(path)
        self.interval = interval
        self._callbacks: list = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mtime = self._current_mtime()
        self._paused = False

    def add(self, cb) -> None:
        self._callbacks.append(cb)

    def _current_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except Exception:  # noqa
            return 0.0

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            try:
                m = self._current_mtime()
            except Exception:  # noqa
                continue
            if m and m != self._mtime:
                self._mtime = m
                if self._paused:
                    # 暂停期间不触发回调，但已消费本次 mtime，避免恢复后误重复触发。
                    logger.info("[配置热重载] 已暂停，跳过本次回调（mtime 已记）")
                    continue
                logger.info("[配置热重载] 检测到 config.json 变化，触发回调")
                for cb in self._callbacks:
                    try:
                        cb()
                    except Exception as e:  # noqa
                        logger.warning("配置热重载回调失败: %s", e)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("[配置热重载] 监视已启动（间隔 %.0fs）", self.interval)

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        """暂停回调触发（仍继续刷新 mtime 基准，避免恢复后误触发旧变更）。"""
        self._paused = True

    def resume(self) -> None:
        """恢复回调触发。"""
        self._paused = False
