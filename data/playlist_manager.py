"""
歌单管理器：协调音源（网易云 / QQ音乐）与本地数据库，为「歌单中心」UI 提供统一入口。

职责：
- 浏览：我的歌单（登录态）、发现/推荐、按链接/ID 加载。
- 收藏：把喜欢的平台歌单落库（playlists + playlist_items + tracks），仅「收藏」才持久化。
- 本地歌单：用户在中心内自建的歌单（platform='local'），与平台歌单统一纳管。
- 播放：取出某歌单的 Track 列表，交给电台控制器 play_tracks 播放。

设计约定：
- 所有网络 I/O 均由调用方（UI）在后台线程执行；本类方法为同步阻塞调用。
- 浏览（我的/发现/按ID）默认不落库；只有用户显式「收藏」才写库（见用户约定）。
"""
from __future__ import annotations

import logging
import re

from data.sources.base import PlaylistMeta

logger = logging.getLogger("MusicAI.PlaylistManager")

PLATFORM_LABELS = {"netease": "网易云", "qqmusic": "QQ音乐", "local": "本地"}


class PlaylistManager:
    def __init__(self, source_manager, db) -> None:
        self.sm = source_manager
        self.db = db

    # ---------------- 取音源 ----------------
    def _source(self, platform: str):
        return self.sm.get(platform)

    # ---------------- 浏览（不落库） ----------------
    def my_playlists(self, platform: str) -> list[PlaylistMeta]:
        """当前登录用户的歌单列表（含「我喜欢的音乐」）。未登录返回 []。"""
        s = self._source(platform)
        if s is None:
            return []
        try:
            return s.user_playlists() or []
        except Exception as e:  # noqa
            logger.warning("取 %s 我的歌单失败: %s", platform, e)
            return []

    def discover(self, platform: str, category: str | None = None,
                 limit: int = 30) -> list[PlaylistMeta]:
        """发现 / 分类歌单（网友精选碟、推荐等）。免登录可用。"""
        s = self._source(platform)
        if s is None:
            return []
        try:
            return s.recommend_playlists(category=category, limit=limit) or []
        except Exception as e:  # noqa
            logger.warning("取 %s 发现歌单失败: %s", platform, e)
            return []

    def resolve(self, text: str) -> "tuple[str, str] | None":
        """从分享链接 / 文本解析出 (platform, ext_id)。

        先按链接域名路由到对应音源；兜底：纯数字当作网易云歌单 id。
        """
        text = (text or "").strip()
        if not text:
            return None
        if "music.163.com" in text or "163cn.tv" in text:
            s = self._source("netease")
            if s:
                pid = s.resolve_playlist_url(text)
                if pid:
                    return ("netease", pid)
        if "y.qq.com" in text or "c.y.qq.com" in text or "qq.com" in text:
            s = self._source("qqmusic")
            if s:
                pid = s.resolve_playlist_url(text)
                if pid:
                    return ("qqmusic", pid)
        if re.fullmatch(r"\d+", text):
            return ("netease", text)
        return None

    def meta(self, platform: str, ext_id) -> "PlaylistMeta | None":
        s = self._source(platform)
        if s is None:
            return None
        try:
            return s.playlist_meta(ext_id)
        except Exception as e:  # noqa
            logger.warning("取 %s 歌单元信息失败(id=%s): %s", platform, ext_id, e)
            return None

    def tracks(self, platform: str, ext_id) -> list:
        """某平台歌单的实时曲目列表（不落库）。"""
        s = self._source(platform)
        if s is None:
            return []
        try:
            return s.playlist_detail(ext_id) or []
        except Exception as e:  # noqa
            logger.warning("取 %s 歌单曲目失败(id=%s): %s", platform, ext_id, e)
            return []

    # ---------------- 收藏 / 持久化 ----------------
    def is_saved(self, platform: str, ext_id) -> bool:
        if platform == "local":
            return False
        return self.db.is_playlist_saved(platform, ext_id)

    def save(self, platform: str, ext_id, name: str | None = None) -> "int | None":
        """收藏一个平台歌单：抓元信息 + 曲目，落库。返回 playlist 行 id。"""
        if platform == "local":
            return None
        meta = self.meta(platform, ext_id)
        if meta is None:
            return None
        name = name or meta.name
        pid = self.db.upsert_platform_playlist(
            platform, ext_id, name, cover=meta.cover, creator=meta.creator,
            track_count=meta.track_count, description=meta.description)
        tracks = self.tracks(platform, ext_id)
        if tracks:
            self.db.save_playlist_tracks(pid, tracks)
        return pid

    def unsave(self, platform: str, ext_id) -> None:
        if platform == "local":
            return
        self.db.remove_saved_playlist(platform, ext_id)

    # ---------------- 本地 / 收藏歌单（已落库） ----------------
    def local_playlists(self) -> list:
        return self.db.get_playlists("local")

    def saved_playlists(self, platform: str | None = None) -> list:
        """已收藏的平台歌单（platform='netease' / 'qqmusic'）。"""
        rows = self.db.get_playlists(platform) if platform else self.db.get_playlists()
        return [r for r in rows if (r["platform"] or "local") != "local"]

    def playlist_obj(self, playlist_id: int):
        """返回 (meta_row, [Track,...])；本地/收藏歌单都从库里读缓存曲目。"""
        from data.library import Track
        meta = self.db.get_playlist(playlist_id)
        if meta is None:
            return None, []
        rows = self.db.playlist_tracks(playlist_id)
        tracks = [t for t in (Track.from_row(r) for r in rows) if t is not None]
        return meta, tracks

    def create_local(self, name: str) -> int:
        return self.db.create_playlist(name)

    def add_track_to_local(self, playlist_id: int, track) -> None:
        """把一首 Track（本地库或远程占位）加入本地歌单；远程曲先落库再关联。"""
        path = track.path or f"{track.source}://{track.ext_id}"
        row = self.db.get_track_by_path(path)
        if row is None:
            tid = self.db.upsert_track(
                path, track.title, track.artist, track.album, track.genre,
                track.year, track.duration, track.bpm,
                source=track.source, ext_id=track.ext_id, cover=track.cover,
                api_metadata=getattr(track, 'api_metadata', None))
        else:
            tid = row["id"]
            self.db.update_api_metadata(track.source, track.ext_id,
                                        getattr(track, 'api_metadata', None))
        self.db.add_to_playlist(playlist_id, tid)

    def delete_local(self, playlist_id: int) -> None:
        self.db.delete_playlist(playlist_id)

    def remove_track(self, playlist_id: int, track_id: int) -> None:
        def _job(c):
            c.execute(
                "DELETE FROM playlist_items WHERE playlist_id=? AND track_id=?",
                (playlist_id, track_id))
            c.commit()
        self.db._write(_job)
