"""MusicAI · 事件总线（Qt Signal → WebSocket 广播）

作用：后端（radio controller）的 Qt Signal 无法直接跨线程推给 asyncio 的 WebSocket，
本模块在中间做桥接：
  - 后端线程（Qt 主线程）调用 bus.emit(event, payload)
  - emit 把事件放进线程安全队列，并唤醒 asyncio 循环
  - asyncio 侧把事件广播给所有已连接的 WebSocket 客户端

⚠ 为什么要这么做：
  原架构用 QWebChannel 的 Signal 直接推给前端，真机实测会丢事件（QVariant 序列化
  不可靠），被迫写轮询对账兜底。改用 WebSocket 后，事件以 JSON 明文推送，
  可靠且可在浏览器 DevTools 的 WS 面板里逐条看到。
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from typing import Any

logger = logging.getLogger("MusicAI.WebAPI.Bus")


class EventBus:
    """线程安全的事件总线：任意线程 emit → asyncio 广播给所有 WebSocket。"""

    def __init__(self) -> None:
        # 线程安全队列：后端线程 put，asyncio 侧 get
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._clients: "set[Any]" = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    # ---------- asyncio 侧 ----------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定事件循环（服务启动时调用一次）。"""
        self._loop = loop

    async def register(self, ws) -> None:
        """注册一个 WebSocket 客户端。"""
        with self._lock:
            self._clients.add(ws)
        logger.info("WS 客户端已连接（当前 %d）", len(self._clients))

    async def unregister(self, ws) -> None:
        with self._lock:
            self._clients.discard(ws)
        logger.info("WS 客户端断开（当前 %d）", len(self._clients))

    async def _broadcast(self, msg: dict) -> None:
        """把一条消息发给所有客户端（坏连接自动剔除）。"""
        dead = []
        with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False, default=str))
            except Exception:  # noqa 客户端已断开
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def run(self) -> None:
        """后台任务：持续把队列里的事件广播出去。"""
        while True:
            try:
                # 用 run_in_executor 避免阻塞事件循环
                msg = await asyncio.get_event_loop().run_in_executor(None, self._q.get)
                await self._broadcast(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa
                logger.warning("事件广播异常: %s", e)

    # ---------- 后端线程侧（可从任意线程调用） ----------

    def emit(self, event: str, payload: Any = None) -> None:
        """后端任意线程调用：把事件排队，由 asyncio 侧广播。

        event  : 事件名，如 'track_changed' / 'playlist_updated'
        payload: 任意可 JSON 序列化的数据
        """
        try:
            self._q.put({"event": event, "payload": payload})
        except Exception as e:  # noqa
            logger.warning("bus.emit 失败(%s): %s", event, e)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


# 全局单例：后端 bridge 与 FastAPI 路由共用
bus = EventBus()
