"""
数据层：SQLite 存储歌单 / 收藏 / 播放历史 / 用户偏好。
纯 sqlite3，无第三方依赖。

并发模型（彻底消灭「database is locked」）：
  - 显式 URI 连接参数 ?journal_mode=WAL&synchronous=NORMAL（配合 uri=True 打开），
    打开后再补 PRAGMA 确保生效：WAL 允许多读一写且不阻塞读；synchronous=NORMAL
    在 WAL 下安全且更快（仅断电落盘瞬间有极小风险）。
  - 「所有写操作」统一投递到单一后台线程的 queue.Queue，由该线程串行执行，
    从根本上杜绝多线程（UI 红心点击 + 播放结束记录 + 后台特征/扫描入库）并发写
    导致的锁超时；不再依赖 busy_timeout 重试。读操作走主连接（WAL 下与写并发安全）。
"""
from __future__ import annotations

import logging
import json
import queue
import sqlite3
import threading
import time
from pathlib import Path

from config.settings import DB_PATH

logger = logging.getLogger("MusicAI.DB")

# 写线程等待单条任务完成的超时（秒）。正常每个写任务极快；此值仅防极端挂死。
_WRITE_TIMEOUT = 30

# R99-T3：track_cache 的**出厂 TTL**（秒）。0 = 永久。单一来源 —— 别在调用点另写数字。
#   url    6h  ：直链会过期（网易/QQ 的 CDN 签名），太短则每次播放都重解析
#   lyric  永久 ：歌词不变（换版本删行即可）
#   meta   30d  ：曲目元数据变动极少
#   search 7d   ：实时检索结果，太旧会"引用过期信息"
CACHE_TTL = {
    "url": 6 * 3600.0,
    "lyric": 0.0,
    "meta": 30 * 86400.0,
    "search": 7 * 86400.0,
}


def _row_col(r, name: str, idx: int, default=None):
    """安全取列：兼容 sqlite3.Row（按名）与裸 tuple（按下标）；越界/缺失 -> default。
    防御性：连接偶发未设 row_factory 或 SELECT 列序变动时，get_cache 不应崩。"""
    try:
        if isinstance(r, sqlite3.Row):
            return r[name]
        return r[idx]
    except Exception:  # noqa 越界 / 类型错配
        return default

class _WriteWorker:
    """单一后台线程，串行消费所有写任务（闭包），杜绝并发写锁冲突。"""

    def __init__(self, uri: str, busy_timeout: int = 5000) -> None:
        self._uri = uri
        self._busy = busy_timeout
        self._queue: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._conn: sqlite3.Connection | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="db-writer")
        self._thread.start()

    def _run(self) -> None:
        try:
            conn = sqlite3.connect(self._uri, uri=True)
            conn.row_factory = sqlite3.Row
            # 注意：SQLite 的 PRAGMA 大多不支持 `?` 占位符绑定，必须用字面量拼接。
            conn.execute(f"PRAGMA busy_timeout={int(self._busy)}")
            self._conn = conn
        except Exception as e:  # noqa
            logger.error("[DB] 写线程建连失败，所有写操作将报错: %s", e)
            # 仍消费队列并快速报错，避免调用方无限等待
            while not self._stop.is_set():
                item = self._queue.get()
                if item is None:
                    break
                _fn, holder, ev = item
                holder["error"] = RuntimeError("数据库不可用")
                ev.set()
                try:
                    self._queue.task_done()
                except Exception:  # noqa
                    pass
            return

        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            fn, holder, ev = item
            try:
                holder["value"] = fn(self._conn)
            except Exception as e2:  # noqa
                holder["error"] = e2
            finally:
                ev.set()
                try:
                    self._queue.task_done()
                except Exception:  # noqa
                    pass
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa
            pass

    def submit(self, fn, timeout: int = _WRITE_TIMEOUT):
        holder: dict = {}
        ev = threading.Event()
        self._queue.put((fn, holder, ev))
        if not ev.wait(timeout):
            raise TimeoutError("SQLite 写队列超时（可能存在长事务/死锁）")
        if "error" in holder:
            raise holder["error"]
        return holder.get("value")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put(None)
        except Exception:  # noqa
            pass
        self._thread.join(timeout=5)


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 显式 URI 连接参数：WAL + synchronous=NORMAL（uri=True 打开）。
        uri = f"{self.path.as_uri()}?journal_mode=WAL&synchronous=NORMAL"
        # 主连接：负责读（WAL 下与写并发安全）；写操作改走 _writer 串行队列。
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            # URI 里的 journal_mode/synchronous 在某些 sqlite 版本不生效，显式再打一次确保。
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA foreign_keys=ON")
        except Exception as e:  # noqa
            logger.warning("设置 SQLite PRAGMA 失败（降级默认）: %s", e)
        # 写线程（独立连接 + 串行队列）
        self._writer = _WriteWorker(uri, busy_timeout=5000)
        self._init()

    def _init(self) -> None:
        c = self.conn.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            title TEXT, artist TEXT, album TEXT, genre TEXT,
            year INT, duration REAL, bpm REAL, added_at REAL,
            source TEXT DEFAULT 'local', ext_id TEXT
        );
        CREATE TABLE IF NOT EXISTS favorites (
            track_id INTEGER PRIMARY KEY,
            added_at REAL,
            FOREIGN KEY(track_id) REFERENCES tracks(id)
        );
        CREATE TABLE IF NOT EXISTS dislikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER,
            source TEXT,
            ext_id TEXT,
            title TEXT,
            artist TEXT,
            key TEXT UNIQUE,
            added_at REAL
        );
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'local',
            ext_id TEXT,
            cover TEXT,
            creator TEXT,
            track_count INT,
            description TEXT,
            created_at REAL,
            synced_at REAL,
            UNIQUE(platform, ext_id)
        );
        CREATE TABLE IF NOT EXISTS playlist_items (
            playlist_id INTEGER, track_id INTEGER,
            position INT, PRIMARY KEY(playlist_id, track_id),
            FOREIGN KEY(playlist_id) REFERENCES playlists(id),
            FOREIGN KEY(track_id) REFERENCES tracks(id)
        );
        CREATE TABLE IF NOT EXISTS play_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER, played_at REAL,
            time_slot TEXT, completed INT DEFAULT 0,
            FOREIGN KEY(track_id) REFERENCES tracks(id)
        );
        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY, value TEXT, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS track_segments (
            track_id INTEGER,
            seg_idx INTEGER,
            start_ms INTEGER,
            embedding BLOB,
            PRIMARY KEY(track_id, seg_idx)
        );
        CREATE TABLE IF NOT EXISTS track_beats (
            track_id INTEGER PRIMARY KEY,
            data TEXT,
            n_beats INTEGER,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS track_features (
            track_id INTEGER PRIMARY KEY,
            version TEXT,
            data TEXT,
            created_at REAL
        );
        /* R99-T3：远程数据缓存（url / lyric / meta / search）+ TTL。
           key = "kind|source|ext_id"（单一主键，避免四个 kind 各建一表）。
           ttl_s = 0 ⇒ 永久；其余按 created_at + ttl_s 惰性过期（读时判，无后台清扫）。
           动机：曲库只存占位地址，播放前每次 `resolve_url` 都走网络（R96 §2.1）；
           加 TTL 后同一首歌的直链/歌词在窗口内只取一次 —— 但**不会**把死链写死，
           因为 TTL 到期即失效重建（旧实现"不写回库"就是为了躲死链）。 */
        CREATE TABLE IF NOT EXISTS track_cache (
            key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            source TEXT,
            ext_id TEXT,
            value TEXT,
            created_at REAL,
            ttl_s REAL
        );
        CREATE INDEX IF NOT EXISTS idx_track_cache_kind ON track_cache(kind);
        """)
        self.conn.commit()
        # Additive, idempotent migration. Fail visibly rather than silently losing metadata.
        columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(tracks)')}
        if 'api_metadata' not in columns:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN api_metadata TEXT NOT NULL DEFAULT '{}'")
            self.conn.commit()
        # 兼容迁移：旧库可能没有 source / ext_id / cover 列
        for col, ddl in (
            ("source", "ALTER TABLE tracks ADD COLUMN source TEXT DEFAULT 'local'"),
            ("ext_id", "ALTER TABLE tracks ADD COLUMN ext_id TEXT"),
            ("cover", "ALTER TABLE tracks ADD COLUMN cover TEXT"),
        ):
            try:
                self.conn.execute(ddl)
            except Exception:  # noqa
                pass
        # playlists 表扩展（平台歌单支持）：加列 + 唯一索引
        for ddl in (
            "ALTER TABLE playlists ADD COLUMN platform TEXT NOT NULL DEFAULT 'local'",
            "ALTER TABLE playlists ADD COLUMN ext_id TEXT",
            "ALTER TABLE playlists ADD COLUMN cover TEXT",
            "ALTER TABLE playlists ADD COLUMN creator TEXT",
            "ALTER TABLE playlists ADD COLUMN track_count INT",
            "ALTER TABLE playlists ADD COLUMN description TEXT",
            "ALTER TABLE playlists ADD COLUMN synced_at REAL",
        ):
            try:
                self.conn.execute(ddl)
            except Exception:  # noqa
                pass
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_playlist_platform_ext "
                "ON playlists(platform, ext_id)")
        except Exception:  # noqa
            pass
        self.conn.commit()

    # 写操作统一入口：投递闭包到写线程串行执行，返回其结果。
    def _write(self, fn, timeout: int = _WRITE_TIMEOUT):
        return self._writer.submit(fn, timeout)

    # ---------- tracks ----------
    def upsert_track(self, path, title, artist, album, genre, year, duration, bpm,
                     source: str = "local", ext_id=None, cover: str | None = None,
                     api_metadata: dict | None = None) -> int:
        def _job(c: sqlite3.Connection) -> int:
            from data.api_metadata import merge
            previous = c.execute('SELECT api_metadata FROM tracks WHERE path=?', (path,)).fetchone()
            metadata = json.dumps(merge(previous['api_metadata'] if previous else {}, api_metadata), ensure_ascii=False)
            c.execute(
                """INSERT INTO tracks(path,title,artist,album,genre,year,duration,bpm,added_at,source,ext_id,cover,api_metadata)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     title=excluded.title, artist=excluded.artist, album=excluded.album,
                     genre=COALESCE(NULLIF(excluded.genre,''),tracks.genre),
                     year=COALESCE(NULLIF(excluded.year,0),tracks.year), duration=excluded.duration,
                     bpm=COALESCE(NULLIF(excluded.bpm,0),tracks.bpm), source=excluded.source, ext_id=excluded.ext_id,
                     cover=excluded.cover, api_metadata=excluded.api_metadata""",
                (path, title, artist, album, genre, year, duration, bpm, time.time(),
                 source, None if ext_id is None else str(ext_id),
                 None if cover is None else str(cover), metadata),
            )
            c.commit()
            return c.execute("SELECT id FROM tracks WHERE path=?", (path,)).fetchone()["id"]
        return self._write(_job)

    def update_api_metadata(self, source, ext_id, metadata):
        """Backfill only the exact platform ID; leave favorites and track identity intact."""
        from data.api_metadata import merge
        def job(c):
            rows = c.execute('SELECT id,api_metadata FROM tracks WHERE source=? AND ext_id=?',
                             (source, str(ext_id))).fetchall()
            for row in rows:
                c.execute('UPDATE tracks SET api_metadata=? WHERE id=?',
                          (json.dumps(merge(row['api_metadata'], metadata),ensure_ascii=False),row['id']))
            c.commit()
            return len(rows)
        return self._write(job)

    def get_track(self, track_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()

    # 占位协议名（必须与 data/sources/*.py 一致：qqmusic→qq://<mid> …）
    _SRC_PROTO = {"qqmusic": "qq"}

    @classmethod
    def placeholder_path(cls, source: str, ext_id) -> str | None:
        """在线曲的稳定占位地址；无法构造（本地 / 缺 ext_id）时返回 None。"""
        src = str(source or "").strip()
        ext = "" if ext_id in (None, "") else str(ext_id).strip()
        if not src or not ext or src == "local":
            return None
        return "%s://%s" % (cls._SRC_PROTO.get(src, src), ext)

    def repair_remote_paths(self) -> int:
        """修复「在线曲 path 为空」的历史脏数据（R86）。

        背景：搜索点播曾用 `path=""` 入库，而 tracks 的唯一键就是 path
        （`ON CONFLICT(path) DO UPDATE`）→ 所有搜来点播的歌撞同一行互相覆盖，
        曲库/历史里留下 path='' 的行（点它播出来的往往不是这首歌）。

        修复策略（幂等、不丢数据）：
          · 按 (source, ext_id) 重建占位 path 写回该行；
          · 若该 path 已被另一行占用（真正的正主）→ 把缺失的封面/时长补过去后
            删除这条空 path 的重复行（等同 ON CONFLICT 的合并语义）；
          · ext_id 缺失（无从重建）→ 跳过，只记日志，绝不猜。

        ⚠ 只动 `source` 非空且非 local、且 path 为 NULL/空串的行；本地曲目一律不碰。
        返回修复（含合并删除）的行数。
        """
        def _job(c: sqlite3.Connection) -> int:
            rows = c.execute(
                "SELECT id, source, ext_id, cover, duration FROM tracks "
                "WHERE (path IS NULL OR path='') "
                "AND source IS NOT NULL AND source NOT IN ('', 'local')"
            ).fetchall()
            fixed = 0
            for r in rows:
                new_path = self.placeholder_path(r["source"], r["ext_id"])
                if not new_path:
                    logger.info("跳过无法重建 path 的在线曲行 id=%s（source=%s ext_id=%s）",
                                r["id"], r["source"], r["ext_id"])
                    continue
                other = c.execute("SELECT id FROM tracks WHERE path=?",
                                  (new_path,)).fetchone()
                if other is not None and other["id"] != r["id"]:
                    # 正主已存在 → 把缺失元数据补过去再删重复行（不丢封面/时长）
                    c.execute(
                        "UPDATE tracks SET cover=COALESCE(cover, ?), "
                        "duration=CASE WHEN COALESCE(duration,0)>0 THEN duration "
                        "ELSE COALESCE(?,0) END WHERE id=?",
                        (r["cover"], r["duration"], other["id"]))
                    for tbl in ("favorites", "play_history", "playlist_items",
                                "track_segments", "track_beats"):
                        try:
                            c.execute(f"UPDATE OR IGNORE {tbl} SET track_id=? "
                                      "WHERE track_id=?", (other["id"], r["id"]))
                            c.execute(f"DELETE FROM {tbl} WHERE track_id=?", (r["id"],))
                        except Exception:  # noqa
                            pass
                    c.execute("DELETE FROM tracks WHERE id=?", (r["id"],))
                    logger.info("合并空 path 重复行 id=%s → id=%s（path=%s）",
                                r["id"], other["id"], new_path)
                else:
                    c.execute("UPDATE tracks SET path=? WHERE id=?",
                              (new_path, r["id"]))
                    logger.info("修复在线曲占位 path：id=%s → %s", r["id"], new_path)
                fixed += 1
            c.commit()
            return fixed
        try:
            return self._write(_job)
        except Exception as e:  # noqa
            logger.warning("repair_remote_paths 失败（不影响启动）: %s: %s",
                           type(e).__name__, e)
            return 0

    def get_track_by_path(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tracks WHERE path=?", (path,)).fetchone()

    def all_tracks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tracks ORDER BY artist, album, title").fetchall()

    def delete_track(self, track_id: int) -> None:
        def _job(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM favorites WHERE track_id=?", (track_id,))
            c.execute("DELETE FROM play_history WHERE track_id=?", (track_id,))
            c.execute("DELETE FROM playlist_items WHERE track_id=?", (track_id,))
            # 离线索引表同步清理（防孤儿数据；track_segments 旧版漏删，一并补上）
            for tbl in ("track_segments", "track_beats"):
                try:
                    c.execute(f"DELETE FROM {tbl} WHERE track_id=?", (track_id,))
                except Exception:  # noqa
                    pass
            c.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            c.commit()
        self._write(_job)

    def set_cover(self, track_id: int, cover: str | None) -> None:
        """只更新封面字段（不触碰其它列，避免 upsert_track 需要全字段的冗余）。

        供联网曲目封面补抽（backfill_remote_covers）批量落库用。
        """
        def _job(c: sqlite3.Connection) -> None:
            c.execute("UPDATE tracks SET cover=? WHERE id=?",
                      (None if cover is None else str(cover), track_id))
            c.commit()
        self._write(_job)

    # ---------- track_segments（P4/D3：CLAP 离线分段语义索引） ----------
    def set_track_segments(self, track_id: int,
                           rows: list[tuple[int, int, bytes]]) -> int:
        """整体替换某曲的分段语义向量（原子：先删后插同一事务）。

        rows: [(seg_idx, start_ms, fp16_blob), ...]；embedding 为 512 维 fp16
        原始字节（1024B/段，3 分钟曲 ≈ 12 段 ≈ 12KB）。索引是**一次性离线
        成本**，运行时只查表零推理（docs/water_research_emotional.md §15.3）。
        """
        def _job(c: sqlite3.Connection) -> int:
            c.execute("DELETE FROM track_segments WHERE track_id=?", (track_id,))
            c.executemany(
                "INSERT INTO track_segments(track_id,seg_idx,start_ms,embedding) "
                "VALUES(?,?,?,?)",
                [(track_id, int(i), int(ms), sqlite3.Binary(b))
                 for i, ms, b in rows])
            c.commit()
            return len(rows)
        return self._write(_job)

    def get_track_segments(self, track_id: int) -> list[sqlite3.Row]:
        """按段序返回 (seg_idx, start_ms, embedding bytes)；无索引返回空列表。"""
        try:
            return self.conn.execute(
                "SELECT seg_idx, start_ms, embedding FROM track_segments "
                "WHERE track_id=? ORDER BY seg_idx", (track_id,)).fetchall()
        except Exception:  # noqa 表未建/库异常 → 视同未索引
            return []

    def has_track_segments(self, track_id: int) -> bool:
        try:
            r = self.conn.execute(
                "SELECT 1 FROM track_segments WHERE track_id=? LIMIT 1",
                (track_id,)).fetchone()
            return r is not None
        except Exception:  # noqa
            return False

    def segment_coverage(self) -> dict:
        """R99-T1：段索引覆盖率（**可观测**才有意义 —— 没有它无法判断补量有没有效果）。

        返回 {total, indexed, missing, by_source:{src:{total,indexed,missing}}}。
        （`incomplete`「有索引但段数不足」需 `n_segments_for_duration` 参与，
        属语义层口径 ⇒ 由 `RadioBridge.index_coverage()` 补，避免 db 反向依赖 core。）

        ⚠ 分母是**曲库全部曲目**（`tracks`），不是"播过的曲目" —— 旧口径只统计
        播放过的曲目，会把覆盖率虚报成 100%。
        """
        out = {"total": 0, "indexed": 0, "missing": 0, "by_source": {}}
        try:
            rows = self.conn.execute(
                "SELECT t.id, t.source, "
                "  (SELECT COUNT(*) FROM track_segments s WHERE s.track_id=t.id) nseg "
                "FROM tracks t").fetchall()
        except Exception:  # noqa 表未建
            return out
        for r in rows:
            nseg = int(r["nseg"] or 0)
            do_indexed = nseg > 0
            src = r["source"] or "?"
            d = out["by_source"].setdefault(
                src, {"total": 0, "indexed": 0, "missing": 0})
            out["total"] += 1
            d["total"] += 1
            if do_indexed:
                out["indexed"] += 1
                d["indexed"] += 1
            else:
                out["missing"] += 1
                d["missing"] += 1
        return out

    def clear_track_segments(self, track_id: int) -> None:
        """只删某曲的分段索引（覆盖率为观测口径服务；删除曲目时走 delete_track）。"""
        def _job(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM track_segments WHERE track_id=?", (track_id,))
            c.commit()
        self._write(_job)

    def indexed_track_ids(self) -> set[int]:
        """已有段索引的 track_id 集合（"有任意段"；**不等于**"已铺满"）。"""
        try:
            return {int(r["track_id"]) for r in self.conn.execute(
                "SELECT DISTINCT track_id FROM track_segments")}
        except Exception:  # noqa
            return set()

    def beat_track_ids(self) -> set[int]:
        """已有 EVENTS（track_beats）的 track_id 集合（R99-T7e beats 预热用）。"""
        try:
            return {int(r["track_id"]) for r in self.conn.execute(
                "SELECT DISTINCT track_id FROM track_beats")}
        except Exception:  # noqa 表未建
            return set()

    def segment_progress(self) -> dict[int, int]:
        """{track_id: 已有段数}（一次聚合查询）。

        ⚠ 预热线程**必须**用这个而不是 `indexed_track_ids()`：分段索引是按
        `budget_s` **切片**跑的（R89），一次只铺几段 ⇒ "有段"离"铺满"很远。
        只看"有没有段"会把半截索引当成已完成，**永远补不齐**（实测：20s 曲
        首片只落 1/3 段，随后每轮都被跳过）。
        """
        try:
            return {int(r["track_id"]): int(r["n"]) for r in self.conn.execute(
                "SELECT track_id, COUNT(*) n FROM track_segments GROUP BY track_id")}
        except Exception:  # noqa
            return {}

    # ---------- track_beats（R84a：EVENTS 层 beat/downbeat+旋律特征离线索引） ----------
    def set_track_beats(self, track_id: int, data: str, n_beats: int) -> None:
        """整体替换某曲的节拍/特征 JSON（与 track_segments 同构：离线一次入
        表，播放路径只查表零推理）。data 为 core/beat_events.index_track
        产出的紧凑 JSON：{v,sr,t:[拍时刻s],d:[小节头],r:[力度],f:[f0],n:[密度]}。
        """
        def _job(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT OR REPLACE INTO track_beats(track_id,data,n_beats,created_at) "
                "VALUES(?,?,?,?)",
                (track_id, data, int(n_beats), time.time()))
            c.commit()
        self._write(_job)

    def get_track_beats(self, track_id: int) -> str | None:
        """读某曲 beats JSON 原文；无索引返回 None。"""
        try:
            r = self.conn.execute(
                "SELECT data FROM track_beats WHERE track_id=?",
                (track_id,)).fetchone()
            return r["data"] if r else None
        except Exception:  # noqa 表未建/库异常 → 视同未索引
            return None

    def has_track_beats(self, track_id: int) -> bool:
        try:
            r = self.conn.execute(
                "SELECT 1 FROM track_beats WHERE track_id=? LIMIT 1",
                (track_id,)).fetchone()
            return r is not None
        except Exception:  # noqa
            return False

    # ---------- track_features（R99-G3：8 维音乐特征离线索引，标定链第①环） ----------
    def set_track_features(self, track_id: int, data: str,
                           version: str = "") -> None:
        """整体替换某曲的 8 维音乐特征 JSON（与 track_beats 同构：离线一次入
        表，播放路径只查表零推理）。data 由 `core/music_features.extract`
        产出：{version, sr, dur_s, raw:{7 维物理量}, dim:{8 维 0~1}}。
        `version` 单独落列 ⇒ K3（语料分位定标）可按版本号批量失效重建。
        """
        def _job(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT OR REPLACE INTO track_features(track_id,version,data,created_at) "
                "VALUES(?,?,?,?)",
                (track_id, str(version or ""), data, time.time()))
            c.commit()
        self._write(_job)

    def get_track_features(self, track_id: int) -> str | None:
        """读某曲 8 维特征 JSON 原文；无索引返回 None。"""
        try:
            r = self.conn.execute(
                "SELECT data FROM track_features WHERE track_id=?",
                (track_id,)).fetchone()
            return r["data"] if r else None
        except Exception:  # noqa 表未建/库异常 → 视同未索引
            return None

    def has_track_features(self, track_id: int) -> bool:
        try:
            r = self.conn.execute(
                "SELECT 1 FROM track_features WHERE track_id=? LIMIT 1",
                (track_id,)).fetchone()
            return r is not None
        except Exception:  # noqa
            return False

    # ---------- track_cache（R99-T3：远程数据 TTL 缓存） ----------
    def set_cache(self, kind: str, source: str, ext_id, value,
                  ttl_s: float | None = None) -> str:
        """写一条缓存。`ttl_s=None` ⇒ 取该 kind 的**出厂 TTL**（见 CACHE_TTL）；
        显式传数可覆盖（测试/调试）。返回主键（便于断言）。

        value 一律按 **str** 存（URL / 歌词原文 / JSON 文本）—— 调用方自行序列化，
        这样"缓存"这一层不需要知道上层的数据形状（改形状不动缓存）。
        """
        k = self.cache_key(kind, source, ext_id)
        ttl = float(CACHE_TTL.get(kind, 0.0) if ttl_s is None else ttl_s)

        def _job(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT OR REPLACE INTO track_cache"
                "(key,kind,source,ext_id,value,created_at,ttl_s) VALUES(?,?,?,?,?,?,?)",
                (k, str(kind), None if source is None else str(source),
                 None if ext_id is None else str(ext_id),
                 "" if value is None else str(value), time.time(), ttl))
            c.commit()
        self._write(_job)
        return k

    def get_cache(self, kind: str, source: str, ext_id):
        """读缓存；**未命中 / 已过期 / 表未建** 一律返回 None。

        ⚠ 过期是**惰性**的：读到过期行时顺手删掉（best-effort，删失败不影响返回值）。
        返回 (value, age_s, ttl_s) 的 value 部分？—— 不：只返回 value，
        需要年龄/统计的走 `cache_stats()` / `get_cache_row()`（保持主路径零多余字段）。
        """
        k = self.cache_key(kind, source, ext_id)
        try:
            r = self.conn.execute(
                "SELECT value, created_at, ttl_s FROM track_cache WHERE key=?",
                (k,)).fetchone()
        except Exception:  # noqa 表未建/库异常 → 视同未命中
            return None
        if r is None:
            return None
        ttl = float(_row_col(r, "ttl_s", 2) or 0.0)
        if ttl > 0 and (time.time() - float(_row_col(r, "created_at", 1) or 0.0)) > ttl:
            try:
                self.del_cache(kind, source, ext_id)
            except Exception:  # noqa
                pass
            return None
        return _row_col(r, "value", 0)

    def get_cache_row(self, kind: str, source: str, ext_id) -> dict | None:
        """诊断用：返回 {value, age_s, ttl_s, fresh}（不过滤过期，只看现状）。"""
        k = self.cache_key(kind, source, ext_id)
        try:
            r = self.conn.execute(
                "SELECT value, created_at, ttl_s FROM track_cache WHERE key=?",
                (k,)).fetchone()
        except Exception:  # noqa
            return None
        if r is None:
            return None
        age = max(0.0, time.time() - float(_row_col(r, "created_at", 1) or 0.0))
        ttl = float(_row_col(r, "ttl_s", 2) or 0.0)
        return {"value": _row_col(r, "value", 0), "age_s": age, "ttl_s": ttl,
                "fresh": (ttl <= 0) or (age <= ttl)}

    def del_cache(self, kind: str, source: str, ext_id) -> None:
        k = self.cache_key(kind, source, ext_id)

        def _job(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM track_cache WHERE key=?", (k,))
            c.commit()
        self._write(_job)

    def prune_cache(self, now: float | None = None) -> int:
        """删掉全部已过期行，返回删除条数（维护窗口用；主路径不调用）。"""
        t0 = time.time() if now is None else float(now)

        def _job(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "DELETE FROM track_cache WHERE ttl_s > 0 AND (?-created_at) > ttl_s",
                (t0,))
            c.commit()
            return cur.rowcount or 0
        return int(self._write(_job) or 0)

    def cache_stats(self) -> dict:
        """按 kind 汇总 {n, fresh, expired, oldest_age_s} + 总条数（真机 F5 可读）。"""
        out = {"total": 0, "by_kind": {}}
        try:
            rows = self.conn.execute(
                "SELECT kind, created_at, ttl_s FROM track_cache").fetchall()
        except Exception:  # noqa 表未建
            return out
        now = time.time()
        for r in rows:
            kind = r["kind"] or "?"
            age = max(0.0, now - float(r["created_at"] or 0.0))
            ttl = float(r["ttl_s"] or 0.0)
            fresh = (ttl <= 0) or (age <= ttl)
            d = out["by_kind"].setdefault(kind, {"n": 0, "fresh": 0, "expired": 0,
                                                 "oldest_age_s": 0.0})
            d["n"] += 1
            d["fresh" if fresh else "expired"] += 1
            if age > d["oldest_age_s"]:
                d["oldest_age_s"] = round(age, 1)
            out["total"] += 1
        return out

    @staticmethod
    def cache_key(kind: str, source: str, ext_id) -> str:
        """缓存主键单一来源（4 个取用点必须同源：set/get/get_row/del，
        否则写进去的键和读出来的键不一致 ⇒ 静默永不命中，且不报错）。"""
        return f"{kind}|{source or ''}|{ext_id or ''}"

    # ---------- favorites ----------
    def toggle_favorite(self, track_id: int) -> bool:
        def _job(c: sqlite3.Connection) -> bool:
            cur = c.execute("SELECT 1 FROM favorites WHERE track_id=?", (track_id,)).fetchone()
            if cur:
                c.execute("DELETE FROM favorites WHERE track_id=?", (track_id,))
                c.commit()
                return False
            c.execute("INSERT INTO favorites(track_id,added_at) VALUES(?,?)",
                      (track_id, time.time()))
            c.commit()
            return True
        return self._write(_job)

    def is_favorite(self, track_id: int) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM favorites WHERE track_id=?", (track_id,)).fetchone())

    def favorite_ids(self) -> set[int]:
        """返回全部已收藏曲目的 id（用于播放列表/推荐/历史红心状态的会话内初始化）。"""
        try:
            return {r[0] for r in self.conn.execute("SELECT track_id FROM favorites").fetchall()}
        except Exception:  # noqa
            return set()

    def favorites(self, source: str | None = None,
                  offset: int | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        """曲库（我喜欢的音乐）。可按音源过滤（source='local' / 'netease' / 'qqmusic'）。

        offset/limit 可选：给了就走真分页（前端「收藏」栏目滚动到底续拉用），
        不给则与旧行为完全一致（全量），不影响既有调用方。
        """
        if limit is None:
            if source:
                return self.conn.execute(
                    "SELECT t.* FROM favorites f JOIN tracks t ON t.id=f.track_id "
                    "WHERE t.source=? ORDER BY f.added_at DESC", (source,)).fetchall()
            return self.conn.execute(
                "SELECT t.* FROM favorites f JOIN tracks t ON t.id=f.track_id "
                "ORDER BY f.added_at DESC").fetchall()
        where = " WHERE t.source=?" if source else ""
        args: list = [source] if source else []
        sql = ("SELECT t.* FROM favorites f JOIN tracks t ON t.id=f.track_id"
               + where + " ORDER BY f.added_at DESC LIMIT ? OFFSET ?")
        args.extend([int(limit), int(offset or 0)])
        return self.conn.execute(sql, args).fetchall()

    def set_favorite(self, track_id: int, on: bool) -> bool:
        """设置某曲是否"在曲库（喜欢）"。返回最终状态。"""
        def _job(c: sqlite3.Connection) -> bool:
            cur = c.execute("SELECT 1 FROM favorites WHERE track_id=?", (track_id,)).fetchone()
            if on and not cur:
                c.execute("INSERT INTO favorites(track_id,added_at) VALUES(?,?)",
                          (track_id, time.time()))
                c.commit()
                return True
            if not on and cur:
                c.execute("DELETE FROM favorites WHERE track_id=?", (track_id,))
                c.commit()
                return False
            return on
        return self._write(_job)

    # ---------- dislikes（隐藏的"不喜欢的歌"库，对 AI 生效） ----------
    def set_dislike(self, track_id: int | None, source: str | None,
                    ext_id, title: str | None, artist: str | None,
                    key: str, on: bool) -> bool:
        """设置某曲是否"不喜欢"。key 为稳定键（音源+原生 id / db id），
        保证跨会话、跨临时 id 识别同一首歌。返回最终是否不喜欢。"""
        def _job(c: sqlite3.Connection) -> bool:
            cur = c.execute("SELECT 1 FROM dislikes WHERE key=?", (key,)).fetchone()
            if on and not cur:
                c.execute(
                    "INSERT INTO dislikes(track_id,source,ext_id,title,artist,key,added_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (track_id, source, ext_id, title, artist, key, time.time()))
                c.commit()
                return True
            if not on and cur:
                c.execute("DELETE FROM dislikes WHERE key=?", (key,))
                c.commit()
                return False
            return on
        return self._write(_job)

    def dislike_keys(self) -> set[str]:
        """返回全部"不喜欢"的稳定键集合（用于会话内初始化红心/避雷状态）。"""
        try:
            return {r[0] for r in self.conn.execute("SELECT key FROM dislikes").fetchall()}
        except Exception:  # noqa
            return set()

    def disliked_meta(self) -> list[tuple[str, str]]:
        """返回全部不喜欢的 (title, artist)，喂给 AI 避雷（含外部源歌曲，
        因为外部歌也落了 title/artist，无需入库即可生效）。"""
        try:
            return [(r["title"] or "", r["artist"] or "")
                    for r in self.conn.execute(
                        "SELECT title, artist FROM dislikes").fetchall()]
        except Exception:  # noqa
            return []

    def dislike_rows(self) -> list[sqlite3.Row]:
        """曲库「不喜欢的歌曲」隐藏视图：按加入时间倒序返回全部不喜欢记录
        （含本地/外部；外部歌仅存元数据，无 track_id）。"""
        try:
            return self.conn.execute(
                "SELECT id, track_id, source, ext_id, title, artist, key "
                "FROM dislikes ORDER BY added_at DESC").fetchall()
        except Exception:  # noqa
            return []

    # ---------- playlists ----------
    def create_playlist(self, name: str) -> int:
        # 本地自建歌单：platform='local'，ext_id 用 name 保证唯一可 upsert
        return self.upsert_platform_playlist("local", name, name)

    def add_to_playlist(self, playlist_id: int, track_id: int) -> None:
        def _job(c: sqlite3.Connection) -> None:
            pos = c.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM playlist_items WHERE playlist_id=?",
                (playlist_id,)).fetchone()[0]
            c.execute(
                "INSERT OR IGNORE INTO playlist_items(playlist_id,track_id,position) VALUES(?,?,?)",
                (playlist_id, track_id, pos))
            c.commit()
        self._write(_job)

    def playlist_tracks(self, playlist_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.* FROM playlist_items p JOIN tracks t ON t.id=p.track_id "
            "WHERE p.playlist_id=? ORDER BY p.position", (playlist_id,)).fetchall()

    def list_playlists(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM playlists ORDER BY created_at").fetchall()

    # ---------- playlists（平台歌单 / 收藏） ----------
    def upsert_platform_playlist(self, platform, ext_id, name, cover=None, creator=None,
                                 track_count=None, description=None) -> int:
        """插入或更新一个平台/收藏歌单，按 (platform, ext_id) 唯一。返回 playlist id。"""
        def _job(c: sqlite3.Connection) -> int:
            c.execute(
                """INSERT INTO playlists(name,platform,ext_id,cover,creator,track_count,description,synced_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform,ext_id) DO UPDATE SET
                     name=excluded.name, cover=excluded.cover, creator=excluded.creator,
                     track_count=excluded.track_count, description=excluded.description,
                     synced_at=excluded.synced_at""",
                (name, platform, str(ext_id) if ext_id is not None else None,
                 cover, creator, track_count, description, time.time()))
            c.commit()
            if ext_id is not None:
                return c.execute("SELECT id FROM playlists WHERE platform=? AND ext_id=?",
                                 (platform, str(ext_id))).fetchone()["id"]
            return c.execute(
                "SELECT id FROM playlists WHERE platform=? AND ext_id IS NULL AND name=?",
                (platform, name)).fetchone()["id"]
        return self._write(_job)

    def get_playlist(self, playlist_id: int):
        return self.conn.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()

    def get_playlists(self, platform: str | None = None) -> list[sqlite3.Row]:
        if platform:
            return self.conn.execute(
                "SELECT * FROM playlists WHERE platform=? ORDER BY created_at",
                (platform,)).fetchall()
        return self.conn.execute("SELECT * FROM playlists ORDER BY created_at").fetchall()

    def save_playlist_tracks(self, playlist_id: int, tracks: list) -> None:
        """把一批 Track 入库（按 source+ext_id 去重）并重建该歌单的曲目关联。"""
        def _job(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
            pos = 0
            for t in tracks:
                path = t.path or f"{t.source}://{t.ext_id}"
                c.execute(
                    """INSERT INTO tracks(path,title,artist,album,genre,year,duration,bpm,added_at,source,ext_id,cover)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                         title=excluded.title, artist=excluded.artist, album=excluded.album,
                         genre=excluded.genre, year=excluded.year, duration=excluded.duration,
                         bpm=excluded.bpm, source=excluded.source, ext_id=excluded.ext_id,
                         cover=excluded.cover""",
                    (path, t.title, t.artist, t.album, t.genre, t.year, t.duration, t.bpm,
                     time.time(), t.source,
                     None if t.ext_id is None else str(t.ext_id),
                     None if t.cover is None else str(t.cover)))
                tid = c.execute("SELECT id FROM tracks WHERE path=?", (path,)).fetchone()["id"]
                c.execute(
                    "INSERT OR IGNORE INTO playlist_items(playlist_id,track_id,position) VALUES(?,?,?)",
                    (playlist_id, tid, pos))
                pos += 1
            c.execute("UPDATE playlists SET track_count=? WHERE id=?", (len(tracks), playlist_id))
            c.commit()
        self._write(_job)

    def playlist_with_tracks(self, playlist_id: int):
        meta = self.get_playlist(playlist_id)
        tracks = self.playlist_tracks(playlist_id)
        return meta, tracks

    def is_playlist_saved(self, platform, ext_id) -> bool:
        row = self.conn.execute(
            "SELECT id FROM playlists WHERE platform=? AND ext_id=?",
            (platform, str(ext_id))).fetchone()
        return row is not None

    def remove_saved_playlist(self, platform, ext_id) -> None:
        def _job(c: sqlite3.Connection) -> None:
            row = c.execute("SELECT id FROM playlists WHERE platform=? AND ext_id=?",
                            (platform, str(ext_id))).fetchone()
            if row:
                pid = row["id"]
                c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
                c.execute("DELETE FROM playlists WHERE id=?", (pid,))
            c.commit()
        self._write(_job)

    def delete_playlist(self, playlist_id: int) -> None:
        def _job(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
            c.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            c.commit()
        self._write(_job)

    # ---------- history ----------
    def log_play(self, track_id: int, time_slot: str) -> None:
        def _job(c: sqlite3.Connection) -> None:
            c.execute("INSERT INTO play_history(track_id,played_at,time_slot) VALUES(?,?,?)",
                      (track_id, time.time(), time_slot))
            c.commit()
        self._write(_job)

    def recent_history(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.*, h.time_slot FROM play_history h JOIN tracks t ON t.id=h.track_id "
            "ORDER BY h.played_at DESC LIMIT ?", (limit,)).fetchall()

    def history_for_analysis(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT t.title,t.artist,t.genre,h.time_slot FROM play_history h "
            "JOIN tracks t ON t.id=h.track_id ORDER BY h.played_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- preferences ----------
    def set_pref(self, key: str, value: str) -> None:
        def _job(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO preferences(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()))
            c.commit()
        self._write(_job)

    def get_pref(self, key: str, default: str = "") -> str:
        r = self.conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def close(self) -> None:
        try:
            self._writer.stop()
        except Exception:  # noqa
            pass
        try:
            self.conn.close()
        except Exception:  # noqa
            pass
