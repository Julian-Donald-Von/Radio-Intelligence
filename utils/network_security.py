"""Validation and bounded download helpers for user-configured endpoints."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def _host_is_loopback(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    if h in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def validate_api_base(value: str, *, allow_local_http: bool = True) -> str:
    """Return a normalized API base or raise ValueError.

    HTTP is intentionally limited to loopback so local Netease/meting services
    remain supported without allowing credentials over a remote cleartext link.
    """
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return ""
    p = urlsplit(raw)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise ValueError("API 地址必须是 http(s) URL")
    if p.username or p.password or p.query or p.fragment:
        raise ValueError("API 地址不能包含用户名、密码、查询参数或片段")
    if p.scheme == "http" and (not allow_local_http or not _host_is_loopback(p.hostname)):
        raise ValueError("远程 API 必须使用 HTTPS；HTTP 仅允许本机回环地址")
    return raw


def validate_media_url(value: str) -> str:
    raw = (value or "").strip()
    p = urlsplit(raw)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise ValueError("远程音源必须是 http(s) URL")
    if p.username or p.password:
        raise ValueError("远程音源 URL 不能包含内嵌凭据")
    return raw
