"""
本地音源：扫描本机音乐目录（元数据由 core/metadata 解析）。
实际扫描逻辑在 data/library.MusicLibrary.scan 中，这里只承载"配置 + 标识"，
使本地目录也能在统一音源框架里被识别。
"""
from __future__ import annotations

from data.sources.base import MusicSource


class LocalSource(MusicSource):
    key = "local"
    label = "本地"

    @property
    def enabled(self) -> bool:
        # 本地库始终可用（除非用户显式关闭扫描）
        return self.cfg.get("enabled", True)

    @property
    def roots(self) -> list[str]:
        return self.cfg.get("roots") or []
