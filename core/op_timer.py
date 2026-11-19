#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ R119-2x：一次性后台任务的「整次调用」计时（供诊断面 `heavy` 点名）。

为什么需要：长跑诊断里出现过"本进程内 1.19 秒的停顿，而所有**已计时**的重活都凑不出
这个量级" ⇒ 说明还有没被计时的重活。与其逐个猜，不如在**线程创建处**统一包一层。

⚠ 只用于**一次性**任务（预下载 / 口播生成 / TTS / 搜索 / 漫游重建 …）。
  **长驻循环别用** —— 那会量出"线程活了几小时"这种没用的值。
"""
from __future__ import annotations

import time


def timed(fn, name: str):
    """包一层计时：整次调用 > 100ms ⇒ `note_heavy(name, ms)` 记一笔。

    任何异常都被吞掉 —— 计时**绝不**影响被包函数的返回值或异常语义。
    """
    def _w(*a, **kw):
        _t = time.monotonic()
        try:
            return fn(*a, **kw)
        finally:
            try:
                _dt = (time.monotonic() - _t) * 1000.0
                if _dt > 100.0:
                    from core.audio_engine import note_heavy
                    note_heavy(name, _dt)
            except Exception:  # noqa 计时绝不拖垮调用方
                pass
    try:
        _w.__name__ = getattr(fn, "__name__", name)
    except Exception:  # noqa
        pass
    return _w
