"""
可插拔音源框架。

每个音源是一个 MusicSource 子类，统一暴露：
  - enabled         是否启用（由用户在"设置"里勾选）
  - login(**kw)     登录（cookie / 手机号和密码等）
  - playlist_detail 歌单详情 -> list[Track]
  - search          搜索     -> list[Track]
  - song_url        取播放直链
  - lyric           取 LRC 歌词

发布版：网易云 / QQ音乐 / 本地 三个音源已拆为插件（plugins/sources/），由 build_registry() 自动发现；核心仅保留 spotify。
用户可在"设置"里启用/停用、填写各自的服务地址与凭据；也能自行新增子类注册到 REGISTRY。
"""
from __future__ import annotations

import logging

from .base import MusicSource
from .spotify import SpotifySource
from core.plugin import discover_plugins, PLUGIN_DIR

logger = logging.getLogger("MusicAI.Source")

# 内置音源（核心代码自带，永不被外部插件覆盖）
BUILTIN_REGISTRY: dict[str, type[MusicSource]] = {
    "spotify": SpotifySource,
}


def discover_source_plugins() -> dict[str, type[MusicSource]]:
    """扫描 plugins/sources/ 下的外部音源插件。"""
    try:
        return discover_plugins(PLUGIN_DIR / "sources", (MusicSource,), recursive=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("音源插件发现失败：%s", e)
        return {}


def build_registry() -> dict[str, type[MusicSource]]:
    """合并内置 + 外部插件。内置 key 优先（外部插件不覆盖核心音源）。"""
    merged: dict[str, type[MusicSource]] = dict(BUILTIN_REGISTRY)
    for key, cls in discover_source_plugins().items():
        if key in merged:
            logger.warning("外部音源插件 '%s' 与内置冲突，已忽略", key)
            continue
        merged[key] = cls
    return merged


# 进程内注册表（启动时构建；调用 reload_source_registry() 可热重载）
REGISTRY: dict[str, type[MusicSource]] = build_registry()


def reload_source_registry() -> dict[str, type[MusicSource]]:
    """重新扫描插件目录，更新 REGISTRY（开发期热重载用）。"""
    global REGISTRY
    REGISTRY = build_registry()
    return REGISTRY


def build_all(cfg: dict) -> dict[str, MusicSource]:
    sources_cfg = (cfg or {}).get("sources", {})
    return {key: cls(sources_cfg.get(key, {})) for key, cls in REGISTRY.items()}


class SourceManager:
    """统一管理所有音源实例，并对外提供"按音源取歌词"等便捷方法。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sources = build_all(cfg)

    def get(self, key: str) -> MusicSource | None:
        return self.sources.get(key)

    def remote_sources(self) -> list[MusicSource]:
        """返回需要联网、在 UI 中以独立 Tab 呈现的音源（不含本地）。"""
        return [s for k, s in self.sources.items() if k != "local"]

    def enrich_metadata(self, track, db=None, allow_live=True):
        """Called in background prefetch. Cache by platform ID, never by title."""
        from data.api_metadata import decode, merge
        import json
        source, ext_id = getattr(track, 'source', ''), getattr(track, 'ext_id', None)
        provider = self.get(source)
        if ext_id is None or provider is None:
            return {}
        cached = db.get_cache('api_metadata_v2', source, ext_id) if db else None
        metadata = decode(cached)
        if cached is None and allow_live and hasattr(provider, 'enrich_metadata'):
            try:
                metadata = provider.enrich_metadata(track) or {}
                if metadata and db:
                    db.set_cache('api_metadata_v2', source, ext_id,
                                 json.dumps(metadata, ensure_ascii=False), ttl_s=30*86400)
            except Exception:
                # Existing metadata survives an unavailable endpoint.
                metadata = {}
        track.api_metadata = merge(getattr(track, 'api_metadata', {}), metadata)
        if db and track.api_metadata:
            db.update_api_metadata(source, ext_id, track.api_metadata)
        return track.api_metadata

    def lyric(self, source_key: str, ext_id) -> str | None:
        s = self.get(source_key)
        if s and ext_id is not None:
            try:
                return s.lyric(ext_id)
            except Exception:  # noqa
                return None
        return None

    def album_cover(self, source_key: str, ext_id) -> str | None:
        """取专辑封面 URL（musicbox / 网易云 API 后端）。失败返回 None。"""
        s = self.get(source_key)
        if s and ext_id is not None and hasattr(s, "album_cover"):
            try:
                return s.album_cover(ext_id)
            except Exception:  # noqa
                return None
        return None

    def album_covers(self, source_key: str, ids) -> dict:
        """批量取封面：{str(ext_id): url}，一次网络往返。

        列表型场景（歌单详情/曲库）必须用它：逐首 album_cover 是 N 次请求
        （实测 104ms/首，100 首≈10s），批量接口 100 首只需 ~150ms。
        音源未实现批量接口时返回空 dict（调用方保持占位封面即可）。
        """
        s = self.get(source_key)
        if s is None or not hasattr(s, "album_covers"):
            return {}
        try:
            return s.album_covers(ids) or {}
        except Exception:  # noqa
            return {}

    def resolve_url(self, track) -> str | None:
        """把只存元数据的远程占位曲目（如 netease://{id}）解析为可播放实时直链。

        曲库只持久化占位地址，播放前由本方法取真实 URL；路由按 track.source。
        解析结果仅用于本次加载，调用方不应写回数据库（避免存死链）。
        """
        s = self.get(getattr(track, "source", "") or "")
        if s is None:
            return None
        try:
            return s.resolve_url(track)
        except Exception:  # noqa
            return None

    def resolve_url_info(self, track) -> dict | None:
        """`resolve_url` 的**带证据**版本（R114）：额外回传 `size` / `time_ms` / `md5`。

        路由与 `resolve_url` 完全一致（同一个音源对象），音源没实现时退回
        `{"url": resolve_url(track)}` ⇒ 判据走 fail-open，由播放侧自愈兜底。
        """
        s = self.get(getattr(track, "source", "") or "")
        if s is None:
            return None
        fn = getattr(s, "resolve_url_info", None)
        if fn is None:
            url = self.resolve_url(track)
            return {"url": url} if url else None
        try:
            return fn(track)
        except Exception:  # noqa
            return None

    def rebuild(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sources = build_all(cfg)
