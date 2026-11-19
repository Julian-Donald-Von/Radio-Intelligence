"""
元数据提取：基于 mutagen 读取歌手/专辑/时长/封面等。
支持常见格式：mp3/flac/m4a/ogg/wav。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("MusicAI.Meta")


@dataclass
class TrackMeta:
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    genre: str = ""
    year: int | None = None
    duration: float = 0.0          # 秒
    bpm: float | None = None
    cover: bytes | None = None     # 内嵌封面二进制
    tags: dict = field(default_factory=dict)

    @property
    def display(self) -> str:
        if self.title:
            return f"{self.title} - {self.artist or '未知'}"
        return Path(self.path).stem


def _safe(obj, key, default=""):
    try:
        v = obj.get(key)
        if v is None:
            return default
        return str(v[0])
    except Exception:  # noqa
        return default


def extract(path: str | Path) -> TrackMeta:
    p = Path(path)
    meta = TrackMeta(path=str(p), title=p.stem)
    try:
        from mutagen import File
        audio = File(str(p), easy=False)
        if audio is None:
            return meta
        # 时长
        if audio.info and hasattr(audio.info, "length"):
            meta.duration = float(audio.info.length)

        # 通用标签访问
        def g(*keys):
            for k in keys:
                if k in audio:
                    return _safe(audio, k)
            return ""

        meta.title = g("TIT2", "TITLE", "©nam", "title") or p.stem
        meta.artist = g("TPE1", "ARTIST", "©ART", "artist")
        meta.album = g("TALB", "ALBUM", "©alb", "album")
        meta.albumartist = g("TPE2", "ALBUMARTIST", "aART", "albumartist")
        meta.genre = g("TCON", "GENRE", "©gen", "genre")
        meta.bpm = _to_float(g("TBPM", "TMPO", "bpm"))

        # 年份
        yr = g("TDRC", "TYER", "©day", "date")
        meta.year = _to_int(yr[:4]) if yr else None

        # 内嵌封面
        meta.cover = _extract_cover(audio)
        meta.tags = {k: str(v) for k, v in audio.items()}
    except Exception as e:  # noqa
        logger.warning("元数据读取失败 %s: %s", path, e)
    return meta


def _to_float(s: str):
    try:
        return float(s)
    except Exception:  # noqa
        return None


def _to_int(s: str):
    try:
        return int(s)
    except Exception:  # noqa
        return None


def _extract_cover(audio) -> bytes | None:
    try:
        # ID3 APIC
        for k, v in audio.items():
            if k.startswith("APIC"):
                return bytes(v.data)
        # MP4 covr
        if "covr" in audio:
            return bytes(audio["covr"][0])
        # FLAC picture
        if hasattr(audio, "pictures") and audio.pictures:
            return bytes(audio.pictures[0].data)
    except Exception:  # noqa
        return None
    return None
