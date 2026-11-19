# -*- coding: utf-8 -*-
"""R119-3f · 系统内存快照 —— **唯一实现**，不要再复制 `GlobalMemoryStatusEx`。

背景（铁则「先查这事实有几份副本」）
  `GlobalMemoryStatusEx` 在本仓库已出现 **2 份生产副本**
  （`start_api_host._warn_if_memory_tight` 与 `watch_audio.py`）。
  按 B116/反重复纪律，新增代码一律走这里，避免出现第 3 份。

读法（务必按名字区分，别混读）
  · `commit_avail_gb`  = `ullAvailPageFile` 除法 ⇒ **提交（commit）还能用多少**
    —— 这个才是"整机级顿卡"的先行指标：逼近 0 ⇒ 内存管理器压缩/换出 ⇒ 硬缺页 ⇒ 进程被冻。
  · `commit_total_gb`  = 提交上限（≈ 物理内存 + 页面文件）。
  · `phys_avail_gb`    = 物理可用（不含页面文件），参考用。
  · `load_pct`         = `dwMemoryLoad`（0~100）。

所有函数**绝不抛异常**：读不到就返回 `{}` / `-1.0`。诊断永远不许拖垮主流程。
"""
from __future__ import annotations

import ctypes

GB = 1024.0 ** 3


class _MEMORYSTATUSEX(ctypes.Structure):
    # 字段顺序与 Win32 MEMORYSTATUSEX 完全一致，缺一不可
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def snapshot() -> dict:
    """→ `{phys_avail_gb, commit_avail_gb, commit_total_gb, load_pct}`；失败 → `{}`。"""
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return {}
        return {
            "phys_avail_gb": round(st.ullAvailPhys / GB, 2),
            "commit_avail_gb": round(st.ullAvailPageFile / GB, 2),
            "commit_total_gb": round(st.ullTotalPageFile / GB, 2),
            "load_pct": int(st.dwMemoryLoad),
        }
    except Exception:  # noqa  非 Windows / 调用失败
        return {}


def commit_avail_gb(default: float = -1.0) -> float:
    """提交可用（GB）。读不到 → `default`（默认 -1.0，调用方据此**不要**做危险动作）。"""
    try:
        return float(snapshot().get("commit_avail_gb", default))
    except Exception:  # noqa
        return float(default)
