"""
音乐库：扫描本地目录、抽取元数据、入库并建立可播放曲目列表。
支持歌手/专辑浏览与按关键词搜索。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import MUSIC_LIBRARY_ROOTS, user_config
from core.metadata import extract, TrackMeta
from data.db import Database
from utils.cover_cache import save_local_cover

logger = logging.getLogger("MusicAI.Library")

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".wma", ".aac"}


def _coerce_ext(v) -> object:
    """把库里存的 ext_id（统一存成字符串）还原成源需要的类型。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    if s.isdigit():
        return int(s)
    return s


def _row_cover(r) -> str | None:
    """安全取封面 URL（sqlite3.Row 的 `in` 判定不可靠，直接按名索引更稳）。"""
    try:
        v = r["cover"]
        return v if v else None
    except (IndexError, KeyError):
        return None


@dataclass
class Track:
    """UI / 推荐统一使用的轻量曲目对象。

    source : 来自哪个音源（local / netease / qqmusic …）
    ext_id : 音源原生 ID（如网易云正数歌曲 id、QQ 的 songmid），用于取直链/歌词
    lyric  : 已获取的 LRC 歌词文本（可选，部分音源搜索时一并返回）
    """
    id: int
    path: str
    title: str
    artist: str
    album: str
    genre: str
    year: int | None
    duration: float
    bpm: float | None
    source: str = "local"
    ext_id: object = None
    lyric: str | None = None
    cover: str | None = None  # 封面 URL/指针（绝不存图片字节）：在线为 http(s) URL，本地音乐为 file:// 缓存路径
    api_metadata: dict = field(default_factory=dict)

    @staticmethod
    def from_row(r) -> "Track | None":
        """sqlite3.Row → Track 的**唯一稳定转换**（R49-25）。

        ⚠ 必须统一走这里：start_radio / play_tracks / jump_to 等播放路径在
        seed=db.get_track() 或 playlist_tracks() 返回 Row 时直接访问 .id/.title
        会抛 AttributeError（'sqlite3.Row' object has no attribute 'id'）。
        本方法还集中处理两件易漏的事：
          1) cover 列 → Track.cover（缺省 None，播放路径自动 upsert 时若不带
             cover，收藏卡片会无封面 —— R49-24 根因之一）；
          2) ext_id 文本 → 数值（_coerce_ext），保证稳定键 e:{src}:{ext} 一致。
        Row 为 None / 缺关键列时返回 None，绝不抛异常。
        """
        if r is None:
            return None
        try:
            from data.api_metadata import decode
            keys = r.keys()
            return Track(
                r["id"], r["path"], r["title"], r["artist"], r["album"],
                r["genre"], r["year"], r["duration"], r["bpm"],
                source=r["source"] or "local",
                ext_id=_coerce_ext(r["ext_id"]),
                cover=_row_cover(r),
                api_metadata=decode(r['api_metadata']) if 'api_metadata' in keys else {},
            )
        except Exception:  # noqa: 缺列/类型异常统一返回 None，调用方兜底
            return None

    @property
    def display(self) -> str:
        return f"{self.title} - {self.artist or '未知'}"


class MusicLibrary:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: list[Track] = []
        self._extra: list[Track] = []   # 非本地来源（如网易云），不入库

    # ---------------- 扫描入库 ----------------
    def scan(self, roots: list[str] | None = None) -> int:
        if roots is None:
            try:
                roots = list(user_config.data["sources"]["local"]["roots"])
            except Exception:  # noqa
                roots = MUSIC_LIBRARY_ROOTS
        count = 0
        for root in roots:
            r = Path(root)
            if not r.exists():
                continue
            for f in r.rglob("*"):
                if f.suffix.lower() in AUDIO_EXT:
                    try:
                        meta: TrackMeta = extract(f)
                        # 本地音乐内嵌封面落盘为 file:// URL 写入 tracks.cover，
                        # UI 的 fetch_cover_bytes 走本地读盘分支显示封面（不再 🎵）。
                        cover_url = save_local_cover(str(f), meta.cover) if meta.cover else None
                        self.db.upsert_track(
                            str(f), meta.title, meta.artist, meta.album,
                            meta.genre, meta.year, meta.duration, meta.bpm,
                            cover=cover_url,
                        )
                        count += 1
                    except Exception as e:  # noqa
                        logger.warning("扫描失败 %s: %s", f, e)
        self.reload()
        # 本地音乐导入即点亮红心（曲库 = 我喜欢的音乐）
        self.favorite_all_local()
        logger.info("扫描完成，新增/更新 %d 首", count)
        return count

    def favorite_all_local(self) -> None:
        """把全部本地曲目标记为"在曲库（喜欢）"，使导入/扫描即红心点亮。"""
        for t in self._cache:
            if t.source in ("local", "", None):
                self.db.set_favorite(t.id, True)

    def backfill_local_covers(self) -> int:
        """一次性迁移：为已入库但封面为空的本地曲目补抽内嵌封面。

        仅处理 source='local' 且 cover 为空且文件仍存在的曲目（联网曲目用在线 URL，
        本就不在本地；缺失文件由 prune_missing 先行清理）。幂等：封面已存在则不再处理。
        返回成功补到封面的数量；后续启动因封面已填充，查询返回 0 行、几乎无开销。
        """
        try:
            rows = self.db.conn.execute(
                "SELECT id,path,title,artist,album,genre,year,duration,bpm "
                "FROM tracks WHERE source='local' AND (cover IS NULL OR cover='')"
            ).fetchall()
        except Exception as e:  # noqa
            logger.warning("补封面查询失败: %s", e)
            return 0
        if not rows:
            return 0
        done = 0
        for r in rows:
            p = r["path"]
            if not p or not Path(p).exists():
                continue
            try:
                meta = extract(p)
                if not meta.cover:
                    continue
                url = save_local_cover(p, meta.cover)
                if url:
                    self.db.upsert_track(
                        p, meta.title or r["title"], meta.artist or r["artist"],
                        meta.album or r["album"], meta.genre or r["genre"],
                        meta.year if meta.year is not None else r["year"],
                        meta.duration or r["duration"],
                        meta.bpm if meta.bpm is not None else r["bpm"],
                        source="local", cover=url,
                    )
                    done += 1
            except Exception as e:  # noqa
                logger.warning("补封面失败 %s: %s", p, e)
        if done:
            self.reload()
            logger.info("本地封面补抽完成，新增 %d 首有封面", done)
        return done

    def backfill_remote_covers(self, source_manager) -> int:
        """一次性迁移：为已入库但封面为空的联网曲目（netease/qqmusic 等）批量补封面。

        与 backfill_local_covers 对应：本地曲目靠内嵌封面，联网曲目靠
        source_manager.album_covers 批量接口（每音源一次网络往返，≤300 首/批）。
        幂等：封面已存在则不再处理；失败静默返回 0，下次启动重试。
        返回成功补到封面的数量。
        """
        try:
            rows = self.db.conn.execute(
                "SELECT id, source, ext_id FROM tracks "
                "WHERE (cover IS NULL OR cover='') AND source IS NOT NULL "
                "AND source NOT IN ('local','') AND ext_id IS NOT NULL AND ext_id<>''"
            ).fetchall()
        except Exception as e:  # noqa
            logger.warning("联网封面补抽查询失败: %s", e)
            return 0
        if not rows:
            return 0
        if source_manager is None or not hasattr(source_manager, "album_covers"):
            return 0
        by_src: dict = {}
        for r in rows:
            by_src.setdefault(r["source"], []).append(r)
        done = 0
        for src, recs in by_src.items():
            # 按 300 分批：批量接口单次上限 300（曲库一般远小于此，仍留保护）
            for i in range(0, len(recs), 300):
                chunk = recs[i:i + 300]
                try:
                    m = source_manager.album_covers(
                        src, [str(r["ext_id"]) for r in chunk]) or {}
                except Exception as e:  # noqa
                    logger.warning("批量封面失败(%s): %s", src, e)
                    continue
                for r in chunk:
                    c = m.get(str(r["ext_id"]))
                    if c:
                        try:
                            self.db.set_cover(r["id"], c)
                            done += 1
                        except Exception as e:  # noqa
                            logger.warning("写封面失败(%s, id=%s): %s",
                                           src, r["id"], e)
        if done:
            self.reload()
            logger.info("联网封面补抽完成，新增 %d 首有封面", done)
        return done

    # ---------------- 手动添加本地音乐 ----------------
    def add_local_files(self, paths: list[str]) -> int:
        """把用户手动选择的若干音频文件入库（并自动红心点亮），返回成功数量。"""
        count = 0
        for f in paths:
            p = Path(f)
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
                continue
            try:
                meta = extract(p)
                cover_url = save_local_cover(str(p), meta.cover) if meta.cover else None
                self.db.upsert_track(
                    str(p), meta.title, meta.artist, meta.album,
                    meta.genre, meta.year, meta.duration, meta.bpm,
                    cover=cover_url,
                )
                count += 1
            except Exception as e:  # noqa
                logger.warning("添加文件失败 %s: %s", p, e)
        self.reload()
        self.favorite_all_local()
        logger.info("手动添加 %d 个文件，入库 %d 首", len(paths), count)
        return count

    def add_local_folder(self, folder: str) -> int:
        """把用户选择的整个文件夹（递归）入库，返回新增数量。"""
        return self.scan([folder])

    def reload(self) -> None:
        rows = self.db.all_tracks()
        self._cache = [t for t in (Track.from_row(r) for r in rows) if t is not None]

    def prune_missing(self) -> int:
        """删除数据库中本地文件已缺失的曲目记录，返回清理数量。

        应对"入库后因文件移动/删除导致记录残留"的情况，避免点击时崩溃。

        ⚠️ 关键防回归：仅清理「本地音乐」（source='local'）中文件确实缺失的记录。
        联网曲目（netease/qqmusic/spotify）使用 netease:// / qq:// 占位路径，
        并非真实文件路径 —— 若不加 source 判断，Path.exists() 恒为假会把所有在线
        红心曲库记录误删，表现为"重启后曲库里的在线音乐全被清空"（用户报告 bug）。
        """
        removed = 0
        for t in list(self._cache):
            if (t.source or "local") != "local":
                continue  # 联网曲目占位路径不是文件，绝不清理
            if t.path and not Path(t.path).exists():
                try:
                    self.db.delete_track(t.id)
                    removed += 1
                    logger.info("已清理缺失文件记录: %s", t.path)
                except Exception as e:  # noqa
                    logger.warning("清理失败 %s: %s", t.path, e)
        if removed:
            self.reload()
        return removed

    @property
    def tracks(self) -> list[Track]:
        if not self._cache:
            self.reload()
        return self._cache + self._extra

    def add_extra(self, tracks: list[Track]) -> None:
        """追加非本地来源曲目（如网易云），不写入 db，且按 id 去重。"""
        existing = {t.id for t in self._extra}
        for t in tracks:
            if t.id not in existing:
                self._extra.append(t)
                existing.add(t.id)

    def by_id(self, tid: int) -> Track | None:
        for t in self.tracks:
            if t.id == tid:
                return t
        return None

    def add_track(self, track: "Track") -> int:
        """把一首（含直链源）曲目写入库（供"红心进曲库"或导入联网音源使用）。

        一次性把 source / ext_id 写进库，避免远程曲目被误存成 local 导致
        去重（音源+歌名+歌手）失效、且后续取直链/歌词时找不到原生 id。
        """
        tid = self.db.upsert_track(
            track.path, track.title, track.artist, track.album,
            track.genre, track.year, track.duration, track.bpm,
            source=track.source or "local",
            ext_id=track.ext_id,
            cover=track.cover,
            api_metadata=getattr(track, 'api_metadata', None),
        )
        self.db.set_favorite(tid, True)
        self.reload()
        return tid

    def add_remote_track(self, track: "Track") -> bool:
        """导入一首直链源曲目（网易云/QQ音乐/Spotify），按 音源+歌名+歌手 去重。

        返回是否真正新增（已存在则跳过，避免重复红心/重复入库）。
        """
        if not self._cache:
            self.reload()
        for t in self._cache:
            if (t.source == track.source and t.title == track.title
                    and t.artist == track.artist):
                return False
        self.add_track(track)
        return True

    def favorites(self, source: str | None = None) -> list[Track]:
        """曲库（我喜欢的音乐）曲目列表，可按音源过滤。"""
        rows = self.db.favorites(source)
        return [t for t in (Track.from_row(r) for r in rows) if t is not None]

    # ---------------- 浏览 ----------------
    def artists(self) -> list[str]:
        return sorted({t.artist for t in self.tracks if t.artist})

    def albums(self, artist: str | None = None) -> list[str]:
        if artist:
            return sorted({t.album for t in self.tracks if t.artist == artist and t.album})
        return sorted({t.album for t in self.tracks if t.album})

    def by_artist(self, artist: str) -> list[Track]:
        return [t for t in self.tracks if t.artist == artist]

    def search(self, kw: str) -> list[Track]:
        kw = kw.lower()
        return [t for t in self.tracks if kw in t.title.lower() or kw in t.artist.lower()
                or kw in t.album.lower() or kw in t.genre.lower()]
