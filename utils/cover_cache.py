"""专辑封面加载工具：带内存 + 磁盘缓存、音源感知请求头、失败重试；
同时支持把本地音频文件内嵌封面保存为缓存文件，供 UI 直接显示。

为什么单独抽出来：
  * 原来 SearchWidget / LibraryWidget / NowPlaying 各自内联 `requests.get`，
    且写死 `Referer: https://y.qq.com/` —— 对网易云 CDN 是错的，导致网易云封面
    取不到、永久显示 🎵。
  * 没有缓存：每次进曲库/搜歌都重新下载，既慢又浪费带宽（也是"卡"的来源之一）。
  * 本地音乐 `track.cover` 为 None，即使文件内嵌了封面也显示 🎵。

用法（调用方仍在自己的后台线程里跑，避免阻塞 UI）：
    from utils.cover_cache import fetch_cover_bytes
    data = fetch_cover_bytes(track.cover, track.source)
    if data: self.cover_loaded.emit(token, data)   # 走信号回主线程 setPixmap
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("MusicAI.Cover")

_DATA_DIR = os.getenv("MUSICAI_DATA_RUNTIME", "data_runtime")
_CACHE_DIR = Path(_DATA_DIR) / "covers"

# 音源 -> 防盗链请求头（网易云与 QQ 的 Referer 不同，写错就 403/空图）
_HEADERS: dict[str, dict[str, str]] = {
    "netease": {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
    "qqmusic": {"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"},
    "spotify": {"User-Agent": "Mozilla/5.0"},
}
_DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}

_MEM: dict[str, bytes] = {}
_LOCK = threading.Lock()


def _cache_file(url: str) -> Path:
    """在线封面缓存文件名：按 URL sha1。"""
    return _CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg")


def _local_cache_file(audio_path: str) -> Path:
    """本地音频内嵌封面缓存文件名：按音频文件路径 sha1，带 local_ 前缀避免与 URL 缓存冲突。"""
    return _CACHE_DIR / ("local_" + hashlib.sha1(audio_path.encode("utf-8")).hexdigest() + ".jpg")


def to_abs_file_url(url: str) -> str:
    """把 `file://<相对路径>` 补成绝对 `file:///D:/...` URL；其它形态原样返回。

    为什么需要：
      save_local_cover 旧版返回 f"file://{cf.as_posix()}"，而 _CACHE_DIR 是
      **相对路径** → 落库形如 "file://data_runtime/covers/local_xxx.jpg"。
      按 URL 语法，这里 data_runtime 会被当成 **host**，路径部分为空，
      QWebEngine 解析不到文件 → 显示"图片损坏"图标。
      老 Qt UI 是 `_load_file()` 剥掉 "file://" 后按裸路径 read_bytes()，
      所以一直没暴露；只有走 URL 的 Web UI 会炸。

      另外在 file:// 页面里，相对路径会以**页面所在目录**为基准解析
      （file:///D:/MusicAI/ui/ + data_runtime/...），同样找不到文件。
    """
    if not url or not isinstance(url, str):
        return url
    if not url.startswith("file://"):
        return url
    rest = url[7:]
    if rest.startswith("/"):        # 已是绝对形式 file:///D:/... 或 file:///home/...
        return url
    try:
        p = Path(rest)
        if not p.is_absolute():
            # 相对路径：依次尝试「当前工作目录」与「本文件上两级（项目根）」
            cands = [Path.cwd() / rest,
                     Path(__file__).resolve().parent.parent / rest]
            for c in cands:
                if c.exists():
                    p = c
                    break
            else:
                p = Path.cwd() / rest
        return p.resolve().as_uri()
    except Exception:  # noqa
        return url


def _load_file(url_or_path: str) -> bytes | None:
    """读取本地文件（支持 file:// 或裸路径）并返回字节。"""
    p = url_or_path
    if p.startswith("file://"):
        p = p[7:]
    # Windows 路径 file:///C:/... 三个斜杠
    if p.startswith("/") and len(p) > 3 and p[2] == ":":
        p = p[1:]
    try:
        return Path(p).read_bytes()
    except Exception:  # noqa
        return None


def save_local_cover(audio_path: str, data: bytes | None = None) -> str | None:
    """把本地音频文件的内嵌封面保存到磁盘缓存。

    若 data 为 None，自动从 audio_path 读取；否则直接写入传入的字节。
    返回 `file://...` 形式的 URL，可写入 tracks.cover 供 fetch_cover_bytes 读取。
    """
    if data is None:
        from core.metadata import extract
        try:
            data = extract(audio_path).cover
        except Exception:  # noqa
            return None
    if not data or len(data) < 64:
        return None
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cf = _local_cache_file(audio_path)
        cf.write_bytes(data)
        # ⚠ 必须写**绝对** file URL：_CACHE_DIR 是相对路径，
        #   f"file://{相对路径}" 会被解析成 host=data_runtime，Web UI 加载不到。
        return cf.resolve().as_uri()
    except Exception as e:  # noqa
        logger.warning("保存本地封面缓存失败 %s: %s", audio_path, e)
    return None


def fetch_cover_bytes(url: str, source: str | None = None,
                      timeout: int = 10) -> bytes | None:
    """取专辑封面字节。命中内存/磁盘缓存立即返回；未命中则按音源请求头下载并落盘缓存。
    支持在线 URL 与本地 file:// 路径。失败返回 None，调用方保持 🎵 占位即可。"""
    if not url:
        return None

    # 1) 内存缓存
    with _LOCK:
        cached = _MEM.get(url)
    if cached is not None:
        return cached

    # 2) 本地文件（file:// 或裸路径）：直接读盘
    if url.startswith("file://") or Path(url).exists():
        data = _load_file(url)
        if data and len(data) > 64:
            with _LOCK:
                _MEM[url] = data
            return data
        # file:// 读不到就结束，不要再走网络
        if url.startswith("file://"):
            return None

    # 3) 磁盘缓存（跨重启生效，避免重复下载）
    cf = _cache_file(url)
    if cf.exists():
        try:
            data = cf.read_bytes()
            if len(data) > 64:
                with _LOCK:
                    _MEM[url] = data
                return data
        except Exception:  # noqa 读坏就当没有，重新下载
            pass

    # 4) 网络下载（音源感知头 + 一次重试）
    try:
        from utils.no_env_proxy import requests_get as _get
        hdr = _HEADERS.get(source or "", _DEFAULT_HEADERS)
        last = None
        for _ in range(2):
            try:
                r = _get(url, headers=hdr, timeout=timeout)
                if r.status_code == 200 and len(r.content) > 64:
                    data = r.content
                    with _LOCK:
                        _MEM[url] = data
                    try:
                        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        cf.write_bytes(data)
                    except Exception:  # noqa 写缓存失败不致命
                        pass
                    return data
                last = f"HTTP {r.status_code}"
            except Exception as e:  # noqa
                last = f"{type(e).__name__}: {e}"
        logger.warning("封面获取失败 %s (source=%s, %s)", url[:90], source, last)
    except Exception as e:  # noqa
        logger.warning("封面获取异常 %s: %s", url[:90], e)
    return None
