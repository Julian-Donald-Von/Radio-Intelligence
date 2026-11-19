"""
Spotify 音源。

两种工作模式（按配置自动选择）：
  1) 代理模式（推荐，可播放）：在「设置 → 音源 → Spotify」填写一个
     Spotify 兼容的 API 服务地址（api_base）。社区里存在多种兼容
     NeteaseCloudMusicApi 路由风格（/search、/playlist/detail、/song/url、
     /lyric）的 Spotify 代理，它们会直接返回可播放的直链(url)与歌词(lrc)，
     此时本源与网易云一样可完整播放。
  2) 官方模式（仅元数据）：未填 api_base、但填了 Client ID / Secret 时，
     使用 Spotify 官方 Web API 的 Client Credentials 流拉取 Bearer 令牌，
     进行搜索/歌单等元数据获取。注意：官方 API 不提供直链，返回的曲目
     仅含元数据（path=spotify://id），无法直接播放——需配合上面的代理
     模式或自行解析才能出声。

无论哪种模式，本源都遵循 MusicSource 统一接口，可被电台后台检索与导入。
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from data.library import Track
from .base import MusicSource
from utils.network_security import validate_api_base

logger = logging.getLogger("MusicAI.Spotify")

_TOKEN_LOCK = threading.Lock()


class SpotifySource(MusicSource):
    key = "spotify"
    label = "Spotify"

    def __init__(self, cfg: dict | None = None) -> None:
        super().__init__(cfg)
        try:
            self.api_base = validate_api_base(self.cfg.get("api_base") or "")
        except ValueError as e:
            logger.error("Spotify 代理地址不安全，已禁用代理: %s", e)
            self.api_base = ""
        from utils.no_env_proxy import no_proxy_requests_session
        self.session = no_proxy_requests_session()   # 忽略环境代理（api_base 直连，不走系统代理）
        self._client_id = self.cfg.get("client_id") or ""
        self._client_secret = self.cfg.get("client_secret") or ""
        self._token: str | None = None
        self._token_exp = 0.0

    # ---------------- 凭据 ----------------
    def set_credentials(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._token = None  # 失效，下次重新获取

    @property
    def _use_proxy(self) -> bool:
        return bool(self.api_base)

    # ---------------- 官方 API 令牌 ----------------
    def _ensure_token(self) -> str | None:
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        if not (self._client_id and self._client_secret):
            return None
        with _TOKEN_LOCK:
            try:
                r = self.session.post(
                    "https://accounts.spotify.com/api/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                    timeout=10,
                )
                r.raise_for_status()
                j = r.json()
                self._token = j.get("access_token")
                self._token_exp = time.time() + float(j.get("expires_in", 3600))
                return self._token
            except Exception as e:  # noqa
                logger.warning("Spotify 获取令牌失败: %s", e)
                return None

    def _headers(self) -> dict:
        token = self._ensure_token()
        if token:
            return {"Authorization": f"Bearer {token}",
                    "User-Agent": "MusicAI/1.0"}
        return {"User-Agent": "MusicAI/1.0"}

    # ---------------- 请求 ----------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        if self._use_proxy:
            resp = self.session.get(self.api_base + path, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        # 官方 API（去除前导 /）
        url = "https://api.spotify.com/v1/" + path.lstrip("/")
        resp = self.session.get(url, params=params, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ---------------- 数据获取 ----------------
    def search(self, keyword: str, limit: int = 30) -> list[Track]:
        if self._use_proxy:
            r = self._get("/search", {"keywords": keyword, "limit": limit, "type": 1})
            songs = (r.get("result") or {}).get("songs") or []
            return [self._to_track_proxy(s) for s in songs][:limit]
        r = self._get("search", {"q": keyword, "type": "track", "limit": limit})
        items = (r.get("tracks") or {}).get("items") or []
        return [self._to_track(item) for item in items][:limit]

    def playlist_detail(self, playlist_id) -> list[Track]:
        if self._use_proxy:
            detail = self._get("/playlist/detail", {"id": playlist_id})
            songs = (detail.get("playlist") or {}).get("tracks") or []
            return [self._to_track_proxy(s) for s in songs]
        r = self._get(f"playlists/{playlist_id}/tracks")
        items = r.get("items") or []
        out = []
        for it in items:
            track = it.get("track") if isinstance(it, dict) else None
            if track:
                out.append(self._to_track(track))
        return out

    def song_url(self, song_id) -> str | None:
        if self._use_proxy:
            try:
                r = self._get("/song/url", {"id": str(song_id), "br": 320000})
                data = r.get("data") if isinstance(r.get("data"), list) else [r]
                for d in data:
                    if d.get("url"):
                        return d["url"]
            except Exception as e:  # noqa
                logger.warning("Spotify 取直链失败: %s", e)
        # 官方 API 不提供直链
        return None

    def lyric(self, song_id) -> str | None:
        if self._use_proxy:
            try:
                r = self._get("/lyric", {"id": str(song_id)})
                return (r.get("lrc") or {}).get("lyric") or None
            except Exception as e:  # noqa
                logger.warning("Spotify 取歌词失败: %s", e)
        return None

    # ---------------- 映射 ----------------
    @staticmethod
    def _to_track(item: dict) -> Track:
        """官方 API 返回（仅元数据，无直链）。"""
        tid = item.get("id")
        name = item.get("name", "未知")
        artists = item.get("artists") or []
        artist = " / ".join(a.get("name", "") for a in artists if a.get("name"))
        album = (item.get("album") or {}).get("name", "")
        dur = (item.get("duration_ms") or 0) / 1000.0
        return Track(
            id=-abs(hash(str(tid))) % (10 ** 9),
            path=f"spotify://{tid}",
            title=name,
            artist=artist,
            album=album,
            genre="",
            year=None,
            duration=float(dur),
            bpm=None,
            source="spotify",
            ext_id=tid,
            lyric=None,
        )

    @staticmethod
    def _to_track_proxy(song: dict) -> Track:
        """兼容 NeteaseCloudMusicApi 路由风格的代理返回（可能含 url）。"""
        sid = song.get("id")
        name = song.get("name", "未知")
        ar = song.get("artists") or song.get("ar") or []
        artist = " / ".join(a.get("name", "") for a in ar if a.get("name"))
        al = song.get("album") or song.get("al") or {}
        album = al.get("name", "") if isinstance(al, dict) else ""
        dt = song.get("duration") or song.get("dt") or 0
        url = song.get("url") or ""
        return Track(
            id=-abs(hash(str(sid))) % (10 ** 9),
            path=url or f"spotify://{sid}",
            title=name,
            artist=artist,
            album=album,
            genre="",
            year=None,
            duration=(dt / 1000.0) if dt else 0.0,
            bpm=None,
            source="spotify",
            ext_id=sid,
            lyric=None,
        )
