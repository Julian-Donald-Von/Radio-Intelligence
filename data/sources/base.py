"""
音源抽象基类。
所有具体音源（网易云 / QQ音乐 / 本地）都继承它，并向曲库提供统一的 Track 列表。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from data.library import Track


@dataclass
class PlaylistMeta:
    """跨音源统一的歌单元信息（封面 / 名 / 创建者 / 曲目数）。"""
    platform: str
    ext_id: str
    name: str
    cover: str | None = None
    creator: str | None = None
    track_count: int | None = None
    description: str | None = None


class MusicSource:
    # 子类覆盖
    key: str = "base"
    label: str = "Base"
    # 是否可作为「搜索源」出现在顶部搜索框的切换开关里（R86-fix）。
    # 模板 / 占位插件（如 plugins/sources/example_source.py）置 False，
    # 否则会以示例身份污染真实音源列表（其 key="example" 走短名兜底会显示成「EXAM」）。
    searchable: bool = True

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg: dict = cfg or {}

    # ---------------- 通用 ----------------
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    # ---------------- 子类按需实现 ----------------
    def login(self, **kwargs) -> bool:
        return True

    def playlist_detail(self, playlist_id) -> list[Track]:
        return []

    # ---------------- 歌单（发现 / 浏览） ----------------
    def playlist_meta(self, playlist_id) -> Optional[PlaylistMeta]:
        """歌单元信息（封面 / 名 / 创建者 / 曲目数）。默认无实现。"""
        return None

    def user_playlists(self) -> list[PlaylistMeta]:
        """当前登录用户的歌单列表（「我的歌单」）。默认空。"""
        return []

    def recommend_playlists(self, category: str | None = None, limit: int = 30) -> list[PlaylistMeta]:
        """平台推荐 / 分类歌单。默认空。"""
        return []

    def resolve_playlist_url(self, text: str) -> Optional[str]:
        """把歌单分享链接 / 文本解析为 playlist_id。默认返回 None。"""
        return None

    def search(self, keyword: str, limit: int = 30) -> list[Track]:
        return []

    def song_url(self, song_id) -> Optional[str]:
        return None

    def resolve_url(self, track) -> Optional[str]:
        """把只存元数据的占位曲目（如 `netease://{id}`）解析为可播放直链。

        R114：基类补一个「无实现」默认值 —— 此前只有具体音源才有此方法，
        任何缺它的音源会走 `AttributeError` 被上层 `except` 吞掉（行为一样，但
        错误更隐晦）。子类照旧覆盖。
        """
        return None

    def resolve_url_info(self, track) -> Optional[dict]:
        """解析直链，并**带上"这份直链能给多少"的证据**（R114，见 `url_health.py`）。

        返回 `{"url": str, "size": int|None, "time_ms": int|None, "md5": str|None}`；
        取不到直链返回 `None`。

        ★ **为什么要多返回这几个字段**：实测同一首歌的直链可能指向**降级短对象**
        （20 s）而非完整曲（300 s），而只判"有没有 url"分辨不出来 ⇒ 降级链会被
        当成好链写进 6 小时缓存。有了 API 自己声明的 `size`/`time` 就能当场验货。

        默认实现退回 `resolve_url()`（只给 url ⇒ 判据走 fail-open，
        由播放侧的短音频自愈兜底）；有能力的音源（如 `NeteaseSource`）覆盖它。
        """
        url = self.resolve_url(track)
        return {"url": url} if url else None

    def lyric(self, song_id) -> Optional[str]:
        return None
