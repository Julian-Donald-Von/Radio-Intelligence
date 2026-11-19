# -*- coding: utf-8 -*-
"""window_ctl.py — 无边框窗口控制桥（HTTP → Qt 主线程）。

背景（2026-11-18）：主窗口改为**无边框 + 自绘标题栏**（用户："外边框要变成
应用里面设计过的关闭按钮，而不是系统预制边框"）。

桥接选型：PySide6 6.11 的 `:/qtwebchannel/qwebchannel.js` 取不到 ⇒ **不用 QWebChannel**，
改为页面 `fetch('/api/window/action')` → FastAPI → 本模块 → `QMetaObject.invokeMethod`
（QueuedConnection）在 Qt 主线程执行。同源、零额外依赖、跨线程安全。

安全：只监听 127.0.0.1（本机应用），且动作白名单限定为窗口控制。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Q_ARG, QMetaObject, Qt

logger = logging.getLogger("MusicAI.WindowCtl")

_WIN = None            # 由 start_api_host 绑定（QObject，含 doXxx slots）
_EDGES = {"left": 1, "top": 2, "right": 4, "bottom": 8}   # Qt::Edges 位


def bind(win_obj) -> None:
    """绑定窗口动作 QObject（主线程创建）。"""
    global _WIN
    _WIN = win_obj
    logger.info("[Window] 无边框窗口控制桥已绑定")


def available() -> bool:
    return _WIN is not None


def dispatch(action: str, edges: int = 0) -> dict:
    """执行一个窗口动作（非阻塞：排队到 Qt 主线程）。"""
    if _WIN is None:
        return {"ok": False, "reason": "no-window"}
    a = str(action or "").strip().lower()
    try:
        if a == "minimize":
            QMetaObject.invokeMethod(_WIN, "doMinimize", Qt.QueuedConnection)
        elif a == "maximize":
            QMetaObject.invokeMethod(_WIN, "doMaximize", Qt.QueuedConnection)
        elif a == "close":
            QMetaObject.invokeMethod(_WIN, "doClose", Qt.QueuedConnection)
        elif a == "fullscreen":
            QMetaObject.invokeMethod(_WIN, "doFullscreen", Qt.QueuedConnection)
        elif a == "move":
            QMetaObject.invokeMethod(_WIN, "doMove", Qt.QueuedConnection)
        elif a == "resize":
            QMetaObject.invokeMethod(_WIN, "doResize", Qt.QueuedConnection, Q_ARG(int, int(edges)))
        elif a == "state":
            # 同步读状态（简单属性，可在调用线程直接读）
            try:
                full = bool(_WIN.is_fullscreen()) if hasattr(_WIN, "is_fullscreen") else None
            except Exception:
                full = None
            try:
                return {"ok": True, "maximized": bool(_WIN.is_maximized()), "fullscreen": full}
            except Exception:
                return {"ok": True, "maximized": None, "fullscreen": full}
        else:
            return {"ok": False, "reason": "unknown-action"}
        return {"ok": True}
    except Exception as e:  # noqa
        logger.debug("[Window] action=%s 失败: %s", a, e)
        return {"ok": False, "reason": str(e)}
