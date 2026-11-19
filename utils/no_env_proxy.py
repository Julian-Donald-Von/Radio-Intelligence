"""让 MusicAI 的网络请求**忽略系统/环境代理**（默认开启）。

背景（2026-11-16）：
  Windows 用户级 HTTP_PROXY/HTTPS_PROXY 指向 geph 的 127.0.0.1:9910。
  9910 未监听时，requests/httpx 的 trust_env=True 会自动走它 →
  `ProxyError ... 9910 [WinError 10061]`：网易云二维码拿不到 unikey、
  cover proxy 502、QQ/网易歌单失败。本软件（含本机 3200 代理）不需要
  系统代理，统一在客户端层关掉环境代理读取。

两道防线：
  1. `strip_env_proxy()` —— 启动入口调一次，从**进程环境**删掉代理变量，
     一网打尽 requests/httpx/huggingface_hub/urllib 等所有读环境变量的库。
  2. 各客户端层 `no_proxy_requests_session()` / `requests_get()` / `httpx_kw()` ——
     即便某库在别处被新建，也不读环境代理。

默认忽略；设 MUSICAI_IGNORE_ENV_PROXY=0 恢复读系统代理。
只影响"从环境变量继承的代理"，不影响显式传入的 proxies= / api_base 直连。
"""
from __future__ import annotations

import os

import requests

_ENV_FLAG = "MUSICAI_IGNORE_ENV_PROXY"

#: 需要从进程环境里清掉的代理变量（大小写 + ALL_PROXY）
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy")


def strip_env_proxy() -> list[str]:
    """从**当前进程**环境删除代理变量，覆盖 requests/httpx/huggingface_hub/
    urllib 等所有读环境变量的库（含它们内部的子进程）。

    ⚠ 只影响本进程及其子进程，**不改 Windows 系统/用户级环境变量**
      —— geph 等代理软件照常工作，其他软件不受影响。
    返回被清掉的变量名列表（便于日志/测试）。默认执行；MUSICAI_IGNORE_ENV_PROXY=0 跳过。
    """
    if not env_proxy_disabled():
        return []
    removed = [k for k in _PROXY_VARS if k in os.environ]
    for k in removed:
        os.environ.pop(k, None)
    return removed


def env_proxy_disabled() -> bool:
    """默认忽略环境代理；显式 MUSICAI_IGNORE_ENV_PROXY=0 时恢复。"""
    return os.getenv(_ENV_FLAG, "1").strip() not in ("0", "false", "False", "no")


def no_proxy_requests_session() -> requests.Session:
    s = requests.Session()
    if env_proxy_disabled():
        s.trust_env = False
    return s


def requests_get(url: str, **kwargs):
    """requests.get 的无环境代理版本。

    ⚠ 不能给 requests.get 传 trust_env= —— 那是 Session 的属性不是请求参数
    （2026-11-16 实测：Session.request() got an unexpected keyword argument）。
    这里用一个临时 Session（trust_env=False）发请求，行为与 requests.get 一致。
    """
    if not env_proxy_disabled():
        import requests
        return requests.get(url, **kwargs)
    with no_proxy_requests_session() as s:
        return s.get(url, **kwargs)


def httpx_kw() -> dict:
    """给 httpx.get 用的关键字参数（httpx 支持 trust_env=）。"""
    return {"trust_env": False} if env_proxy_disabled() else {}
