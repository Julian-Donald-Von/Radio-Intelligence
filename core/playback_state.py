# -*- coding: utf-8 -*-
"""R119-3j · 「播放器是否在播放」的**唯一事实来源**。

为什么要有这个模块（铁则：一个事实一个出口 / 先查"这事实有几份副本"）
  此前这条事实**只有一份实现**：`webapi/radio_bridge.py::_beats_idle_only()`
  （读 `self.radio.player.state`，并 `note_gate()` 记一笔决策）。但需要它的地方
  已经扩散到 **`ai/` 层**（`ai/recommend.py` 的漫游索引重建、`ai/schedule.py` 的
  推荐缓冲填充…），而 `ai/` 拿不到 `RadioBridge` 实例。

  ⇒ 不给 `ai/` 再写第二份判断（那就会出现"两份副本、说法不一致"的经典事故），
    改成**依赖倒置**：由 `radio_bridge` 在装配时把判据**注册**进来，
    `ai/` 侧只问 `is_playing()`。

用途：长跑在**本进程**里的重活（持 GIL 的 Python/NumPy 循环）在动手前先问一句
  —— 「现在在播放吗？」—— 在播放就**让路**（等到空闲再做，或本轮放弃）。
  这是本项目修"整进程冻结"的统一手法（beats / 特征索引 / 漫游重建…）。

⚠ 这是**只读、绝不抛**的模块：任何异常一律当"不在播放"（宁可按旧行为跑，
  也不能因为诊断把主流程搞崩）。
"""
from __future__ import annotations

_provider = [None]      # 由 radio_bridge 装配时注入：callable() -> bool
_note = [None]          # 可选：把"决策"记进诊断面（radio_bridge 的 note_gate）


def set_provider(fn) -> None:
    """注册判据：`fn() -> bool`（True = 正在播放）。传 None 撤销。"""
    _provider[0] = fn


def set_note(fn) -> None:
    """注册"决策记录"回调：`fn(playing: bool)`（用于 `gate` 诊断计数）。可空。"""
    _note[0] = fn


def is_playing() -> bool:
    """现在是否在播放。**读不到一律返回 False**（= 不拦，按旧行为）。"""
    try:
        fn = _provider[0]
        if fn is None:
            return False
        return bool(fn())
    except Exception:  # noqa
        return False


def wait_until_idle(timeout_s: float = 300.0, poll_s: float = 0.5) -> bool:
    """**协作式让路**：等到"不在播放"为止。

    返回 True = 已空闲（可以做重活）；False = 等超时（调用方应**放弃本轮**）。
    `timeout_s <= 0` 表示只探一次、不等待。

    为什么要有上限：等太久会让这个后台任务永远做不完（活锁）。放弃本轮 + 下次
    重试（调用方保证幂等）比"死等"好。
    """
    if not is_playing():
        _record(False)
        return True
    if timeout_s <= 0:
        _record(True)
        return False
    import time as _t
    _deadline = _t.monotonic() + float(timeout_s)
    while is_playing() and _t.monotonic() < _deadline:
        _t.sleep(poll_s)
    playing = is_playing()
    _record(playing)
    return not playing


def _record(playing: bool) -> None:
    try:
        fn = _note[0]
        if fn is not None:
            fn(bool(playing))
    except Exception:  # noqa
        pass
