"""Small Windows-native secret storage helpers.

Secrets are protected with Windows DPAPI and can only be unprotected by the
same Windows user on the same machine.  The application keeps plaintext only
in memory while it is running.
"""
from __future__ import annotations

import base64
import ctypes
import logging
import os
from ctypes import wintypes

logger = logging.getLogger("MusicAI.Secure")


PREFIX = "dpapi:"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def is_protected(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def protect(value: str) -> str:
    if not value or is_protected(value):
        return value or ""
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    src, _src_buf = _blob(value.encode("utf-8"))
    out = _DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
                   ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), "MusicAI secret", None, None, None, 0,
              ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(out.pbData, out.cbData)
        return PREFIX + base64.b64encode(raw).decode("ascii")
    finally:
        kernel32.LocalFree(out.pbData)


def unprotect(value: str) -> str:
    if not value:
        return ""
    if not is_protected(value):
        # Backward-compatible migration path.  It will be encrypted on save.
        return value
    if os.name != "nt":
        # 在非 Windows 环境（如把 Windows 生成的 config.json 拿到 Linux/macOS
        # 跑）无法用 DPAPI 解密：不再抛异常导致整个应用起不来，而是忽略该密文
        # 并告警，应用以"未配置凭据"的降级模式继续运行，用户重新填写即可。
        logger.warning(
            "DPAPI 密文在当前（非 Windows）环境无法解密，已忽略；"
            "请在 Windows 上重新填写相关凭据。")
        return ""
    raw = base64.b64decode(value[len(PREFIX):], validate=True)
    src, _src_buf = _blob(raw)
    out = _DATA_BLOB()
    crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    fn = crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
                   ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out.pbData)
