# -*- coding: utf-8 -*-
"""
UI↔后端 桥接宿主（测试/预览用）。

作用：
  用 PySide6 QWebEngineView 嵌入新版液态玻璃舱 ui/ai-radio-player.html，
  通过 QWebChannel 把 window.MusicAI 暴露给前端 JS。前端设置的契约
  （ping/listSkins/currentSkin/setSkin/getWallpaperDescriptor/importWallpaper/
   listDislikes/addDislike/removeDislike/fileToSource）在此处对接真实后端：

    · 皮肤/壁纸   -> core/skin.py 的 SkinManager / WallpaperImporter（已落地、可落库）
    · 不喜欢的音乐 -> data/db.py 的 dislikes 表（已落地、对 AI 生效）

本宿主是「轻量预览/联调」入口：
  - 不拉起 LLM/Ollama、VOICEVOX、QQ音乐 API、浏览器搜索等重活，启动快、易测。
  - 仅接 MusicAI 已稳定落地的 skin / wallpaper / dislike 后端，验证 UI↔后端适配。
  - 若将来要把舱内播放/歌单/口播也接上，可把 self.radio / self.player 等注入
    Bridge，并在 HTML 里扩展对应 window.MusicAI 方法（契约已预留）。

用法：
  python ui/bridge_host.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 保证无论以何种方式启动（python ui/bridge_host.py / 模块导入），
# 项目根目录都在 sys.path 上，能 import core / data / config 等包。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QObject, QUrl, Signal, Slot, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebChannel import QWebChannel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("BridgeHost")

ROOT = _ROOT
HTML_PATH = ROOT / "ui" / "ai-radio-player.html"


# ---------------- 本地封面代理（HTTP，规避 QWebChannel coverReady 信号偶发丢失） ----------------
# ⚠ 根因：新 UI（HTML/QWebEngine）的封面依赖后端经 QWebChannel 推送的 coverReady(dataUrl)
#   信号；该信号在真机偶发丢失 → 本地 file:// 与防盗链 CDN 封面「时而空白」。老 UI（PySide6）
#   直连加载不受影响。改为浏览器直连本进程内的 HTTP 封面代理：后端用 fetch_cover_bytes
#   （带 referer/鉴权）取字节并流式返回，img 标签跨域加载 localhost 稳定可靠，不再依赖信号。
import http.server
import urllib.parse
import threading

_COVER_PROXY_PORT = 8799
_COVER_PROXY_BASE = f"http://127.0.0.1:{_COVER_PROXY_PORT}"


class _CoverProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if path != "/cover":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parsed.query)
            url = (q.get("url") or [""])[0]
            source = (q.get("source") or [""])[0] or None
            if not url:
                self.send_response(400)
                self.end_headers()
                return
            from utils.cover_cache import fetch_cover_bytes
            data = fetch_cover_bytes(url, source)
            if not data:
                self.send_response(404)
                self.end_headers()
                return
            ctype = "image/jpeg"
            if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
                ctype = "image/png"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                ctype = "image/webp"
            elif data[:3] == b"GIF":
                ctype = "image/gif"
            elif data[:4] == b"%PDF":
                ctype = "application/pdf"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except Exception:  # noqa 单图失败不应拖垮服务
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, *args):  # 静默：避免每图刷日志
        pass


def _start_cover_proxy() -> None:
    try:
        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", _COVER_PROXY_PORT), _CoverProxyHandler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        logger.info("[封面代理] 本地 HTTP 封面服务已启动：%s", _COVER_PROXY_BASE)
    except Exception as e:  # noqa 启动失败不阻断主程序，封面退回直连/旧代理
        logger.warning("[封面代理] 启动失败（封面将退回直连/旧代理）：%s", e)


def _coerce_lrc_text(v):
    """把音源 lyric() 的多种返回形态归一成 LRC 文本（str）或 None。

    各音源/后端返回形态并不统一，实测与代码可见：
      - str            ：正常 LRC 文本（netease musicbox 逐行 join、HTTP 后端 lrc.lyric）
      - dict           ：如 {'lyric': '...'} / {'lrc': {...}} / {'data': {...}}（QQ 常见）
      - list[str|dict] ：musicbox 逐行歌词（dict 行取 lineLyric/lyric/text）
      - None           ：无歌词
    旧实现把返回值直接喂 parse_lrc，遇到 dict 就抛
    "'dict' object has no attribute 'splitlines'"（用户日志已复现）。
    """
    for _ in range(5):
        if v is None:
            return None
        if isinstance(v, str):
            return v or None
        if isinstance(v, dict):
            hit = None
            for k in ("lyric", "lrc", "lineLyric", "text", "content",
                      "klyric", "tlyric", "data"):
                if v.get(k):
                    hit = v[k]
                    break
            if hit is None:
                vals = [x for x in v.values() if x]
                if not vals:
                    return None
                hit = vals[0]
            v = hit
            continue
        if isinstance(v, (list, tuple)):
            parts = []
            for item in v:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    s = (item.get("lineLyric") or item.get("lyric")
                         or item.get("text") or item.get("content"))
                    if s:
                        parts.append(str(s))
            return "\n".join(parts) if parts else None
        return str(v) or None
    return None


class Bridge(QObject):
    """前端 window.MusicAI 的后端实现。

    所有 @Slot 方法都会被 QWebChannel 同步/异步暴露给 JS：
      - 返回值的槽：JS 调用得到返回值（QWebChannel 自动 promise 化）。
      - 无返回值槽：JS 调用即触发后端动作。
    注意：QWebChannel 要求槽参数/返回值可被 Qt 元类型系统识别；
    复杂对象以 dict / list 返回，JS 端会自动转成普通对象/数组。

    支持两种模式：
      轻量模式（默认）：仅 skin / wallpaper / dislike / playlist-meta / favorites，不拉 LLM。
      完整模式（注入 radio）：播放控制 / 推荐 / 歌词 / AI 口播 全部对接 AIRadioController。
    """

    toast = Signal(str)          # 后端 → 前端 toast（系统提示）
    trackChanged = Signal("QVariant")   # 后端 → 前端：当前曲目变化
    playlistUpdated = Signal("QVariant") # 后端 → 前端：播放列表更新
    lyricsReady = Signal("QVariant")      # 后端 → 前端：歌词就绪
    aiSpeech = Signal(str)               # 后端 → 前端：AI 口播文案
    progressChanged = Signal(int)        # 后端 → 前端：播放进度（ms）
    playingChanged = Signal(bool)        # 后端 → 前端：播放状态
    recoUpdated = Signal("QVariant")     # 后端 → 前端：推荐货架更新
    testResult = Signal(str, str)        # 后端 → 前端：测试连接结果 (test_id, 文本)
    qrLoginResult = Signal("QVariant")   # 后端 → 前端：扫码登录轮询结果（dict）
    thinkingChanged = Signal(bool)       # 后端 → 前端：AI 思考状态（口播生成/推荐进行中）
    recommendReason = Signal(str)        # 后端 → 前端：推荐理由（切歌时下发）
    playlistTracksReady = Signal(str, str, "QVariant")  # 后端 → 前端：歌单曲目首页就绪 (platform, ext_id, tracks)
    playlistTracksPage = Signal(str, str, int, "QVariant")  # 后端 → 前端：续页 (platform, ext_id, offset, rows)
    coverReady = Signal(str, str)        # 后端 → 前端：封面 data URL 就绪 (url, dataUrl)
    playModeChanged = Signal(str)        # 后端 → 前端：播放模式变化（dock/栏目/设置面板多入口同步）
    playlistsReady = Signal(str, int, int, "QVariant")  # 后端 → 前端：歌单栏目一页就绪 (group, offset, limit, items)

    def __init__(self, radio=None) -> None:
        super().__init__()
        # 轻量后端：仅初始化已稳定落地的 skin / db（不拉 LLM/音频等重活）。
        from core.skin import SkinManager
        from data.db import Database
        self.skin = SkinManager()
        self.db = Database()
        self.radio = radio   # AIRadioController 实例（完整模式），None=轻量模式
        if radio:
            self._wire_radio_events()
        # ---- 异步资源（R25：把网络请求挪出 Qt 主线程） ----
        # 封面下载线程池：旧 coverDataUrl 是同步 slot，每张封面都在主线程下载
        # （timeout 10s×2 重试），打开歌单详情 = N 张封面串行阻塞 = 整个 UI 卡死。
        self._cover_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coverdl")
        self._cover_jobs = set()
        # 歌单曲目 TTL 缓存：(platform, str(ext_id)) -> (monotonic, list[dict])
        # 缓存的是**全量**曲目；首页/续页都从它切片（大歌单不必重复回源）
        self._pl_tracks_cache = {}
        self._pl_fetching = set()
        # 歌单栏目（R26：discover/mine/saved/local 全量缓存 + 后台线程取回）
        self._col_cache = {}        # group -> (monotonic, list[dict])
        self._col_fetching = set()  # (group, offset) 在取集合（防重入）
        self._discover_fail_until = {}  # 音源短期失败缓存：qqmusic 502 不反复白等
        self._discover_exhausted = {}   # 音源已取尽标记：{cache_key: True}
        logger.info("Bridge 后端就绪：skin.active=%s, 已落地 dislike 库, radio=%s",
                     self.skin.active_key, bool(radio))
        # 启动本地 HTTP 封面代理（封面可靠投递，规避 QWebChannel coverReady 信号丢失）
        try:
            _start_cover_proxy()
        except Exception:  # noqa
            pass

    # ---------------- 连通性 ----------------
    @Slot(result=bool)
    def ping(self) -> bool:
        return True

    @Slot(str, result=bool)
    def reportChannel(self, status: str) -> bool:
        """前端握手后主动上报状态（不依赖 runJavaScript 返回值的可靠诊断）。
        状态：connected / no-qt / no-qwebchannel / no-transport / no-object / error:* """
        logger.info("📡 前端通道状态上报：%s", status)
        return True

    @Slot(result=str)
    def getCoverProxyBase(self) -> str:
        """返回本地 HTTP 封面代理基址（前端 applyCover 经它直连加载封面，可靠）。"""
        return _COVER_PROXY_BASE

    # ---------------- 皮肤 ----------------
    @Slot(result=list)
    def listSkins(self) -> list:
        try:
            return self.skin.list_skins() or []
        except Exception as e:  # noqa
            logger.warning("listSkins 失败: %s", e)
            return []

    @Slot(result=str)
    def currentSkin(self) -> str:
        try:
            return self.skin.active_key
        except Exception:  # noqa
            return "default"

    @Slot(str, result=bool)
    def setSkin(self, key: str) -> bool:
        try:
            ok = self.skin.set_active(key)
            if ok:
                self.toast.emit("已切换皮肤：" + key)
            return ok
        except Exception as e:  # noqa
            logger.warning("setSkin 失败: %s", e)
            return False

    @Slot(result="QVariant")
    def getWallpaperDescriptor(self):
        """返回当前皮肤渲染描述（前端据此刷新背景层）。"""
        try:
            return self.skin.render_descriptor()
        except Exception as e:  # noqa
            logger.warning("getWallpaperDescriptor 失败: %s", e)
            return None

    # ---------------- 壁纸导入 ----------------
    @Slot(str, str, result="QVariant")
    def importWallpaper(self, wtype: str, src: str):
        """经后端 WallpaperImporter 导入壁纸/皮肤，落库并自动激活。

        返回 {ok, name, error}。wtype ∈ image|video|link|wallpaper。
        """
        try:
            sm = self.skin
            name = None
            if wtype == "image":
                key = sm.import_image(src)
                name = key
            elif wtype == "video":
                key = sm.import_video(src)
                name = key
            elif wtype == "link":
                key = sm.import_from_link(src)
                name = key
            elif wtype == "wallpaper":
                # 链接（Steam 创意工坊）或本地工程文件夹统一走 import_from_link
                key = sm.import_from_link(src)
                name = key
            else:
                return {"ok": False, "error": "未知壁纸类型：" + str(wtype)}
            if key:
                self.toast.emit("壁纸已导入并应用：" + (name or key))
                return {"ok": True, "name": name or key, "key": key}
            return {"ok": False, "error": "导入返回空 key（路径/链接可能无效）"}
        except Exception as e:  # noqa
            logger.warning("importWallpaper 失败: %s", e)
            return {"ok": False, "error": str(e)[:120]}

    # ---------------- 不喜欢的音乐（整合进设置面板） ----------------
    def _track_id_by_key(self, key: str):
        """把前端 key（i:123 / e:qq:xxx）解析成 db track_id；解析不到返回 None。"""
        try:
            if key.startswith("i:"):
                return int(key.split(":", 1)[1])
            if key.startswith("e:"):
                parts = key.split(":")
                if len(parts) >= 3:
                    row = self.db.conn.execute(
                        "SELECT id FROM tracks WHERE source=? AND ext_id=? LIMIT 1",
                        (parts[1], parts[2])).fetchone()
                    return row["id"] if row else None
        except Exception:  # noqa
            pass
        return None

    @Slot(result=list)
    def listDislikes(self) -> list:
        """返回 dislike 行的 [{key,title,artist}]；完整模式走 radio 隐藏库（含 AI 避雷状态）。"""
        try:
            if self.radio is not None:
                rows = self.radio.get_disliked_tracks() or []
                return [{"key": r.get("key", ""),
                         "title": r.get("title") or "未知标题",
                         "artist": r.get("artist") or "未知歌手"} for r in rows]
            rows = self.db.dislike_rows()
            out = []
            for r in rows:
                out.append({
                    "key": r["key"],
                    "title": r["title"] or "未知标题",
                    "artist": r["artist"] or "未知歌手",
                })
            return out
        except Exception as e:  # noqa
            logger.warning("listDislikes 失败: %s", e)
            return []

    @Slot(str, result=bool)
    def addDislike(self, key: str) -> bool:
        """key 形如 'i:123'（本地）或 'e:qq:xxx'（在线）。
        完整模式走 radio.dislike_by_id：AI 避雷 + 从待播/推荐货架剔除；否则直写 db。"""
        try:
            if self.radio is not None:
                tid = self._track_id_by_key(key)
                if tid is not None:
                    self.radio.dislike_by_id(tid, True)
                    self.toast.emit("已加入「不喜欢的音乐」（AI 将避开）")
                    return True
            self.db.set_dislike(
                track_id=int(key.split(":", 1)[1]) if key.startswith("i:") else None,
                source=(key.split(":", 2)[1] if key.startswith("e:") else None),
                ext_id=(key.split(":", 2)[2] if key.startswith("e:") else None),
                title=None, artist=None, key=key, on=True,
            )
            self.toast.emit("已加入「不喜欢的音乐」")
            return True
        except Exception as e:  # noqa
            logger.warning("addDislike 失败: %s", e)
            return False

    @Slot(str, result=bool)
    def removeDislike(self, key: str) -> bool:
        try:
            if self.radio is not None:
                tid = self._track_id_by_key(key)
                if tid is not None:
                    self.radio.dislike_by_id(tid, False)
                else:
                    self.radio.undo_dislike_by_key(key)
                self.toast.emit("已从「不喜欢的音乐」移除")
                return True
            self.db.set_dislike(
                track_id=int(key.split(":", 1)[1]) if key.startswith("i:") else None,
                source=(key.split(":", 2)[1] if key.startswith("e:") else None),
                ext_id=(key.split(":", 2)[2] if key.startswith("e:") else None),
                title=None, artist=None, key=key, on=False,
            )
            self.toast.emit("已从「不喜欢的音乐」移除")
            return True
        except Exception as e:  # noqa
            logger.warning("removeDislike 失败: %s", e)
            return False

    # ---------------- 推荐 / 歌单 / 曲库（collections） ----------------
    def _playlist_manager(self):
        if not hasattr(self, "_pm"):
            try:
                from data.sources import SourceManager
                from data.playlist_manager import PlaylistManager
                from config.settings import user_config
                sm = SourceManager(user_config.data)
                self._pm = PlaylistManager(sm, self.db)
            except Exception as e:  # noqa
                logger.warning("PlaylistManager 初始化失败: %s", e)
                self._pm = None
        return self._pm

    @Slot(result="QVariant")
    def discoverPlaylists(self) -> list:
        """发现/推荐歌单（免登录），前端「歌单」页使用。"""
        return self.discoverPlaylistsPaged(0, 30)

    @Slot(int, int, result="QVariant")
    def discoverPlaylistsPaged(self, offset: int, limit: int) -> list:
        """分页取「发现歌单」（**同步**，供旧调用方/对照；新前端走 requestPlaylists）。

        ⚠ R26：这个 slot 曾经在 Qt 主线程做音源网络请求——实测打开歌单栏目
        netease 584ms + qqmusic 失败重试 2s ≈ 2.6s 整窗冻结（「点击歌单卡顿」）。
        前端已切换 `requestPlaylists`（后台线程 + playlistsReady 信号），
        此同步入口保留仅用于向后兼容与卡顿对照（见 verify_round26 B10）。
        """
        self._discover_impl(max(0, int(offset or 0)), max(1, min(60, int(limit or 30))))
        cache = (self._col_cache.get("discover") or (0, []))[1]
        off, lim = max(0, int(offset or 0)), max(1, min(60, int(limit or 30)))
        return cache[off:off + lim]

    def _discover_impl(self, offset: int, limit: int, source: str = None) -> list:
        """取「发现歌单」全量缓存（网络部分；同步 slot 与后台线程共用）。

        source ∈ None|'netease'|'qqmusic'（#140）：指定时只拉该音源、缓存键隔离
        （discover:<src>），与「全部」视图（discover）互不覆盖。

        音源 API 只给 limit、不给 offset，这里用「多拉 + 前端按 offset 切片」实现分页：
        后端缓存整批结果，前端每次只要 [offset, offset+limit) 这段，滚到底再取下一页；
        超出缓存则再向音源多拉一批（limit 翻倍上限 100）。返回空数组 = 没有更多了。
        """
        ck = "discover" if not source else f"discover:{source}"
        cache = self._col_cache.get(ck)
        want = min(100, max(60, offset + max(1, limit)))
        # ⚠ 关键性能修复：某音源的可用总量可能**小于** want（实测 qqmusic 上限 40，
        # 而 want 恒为 60）→ 旧判断 `len(cache) < want` 永远成立 → 每次翻页/滚动
        # 都重新走网络、永不命中缓存，表现为「歌单疯狂加载 / 半天加载不出来」。
        # 因此用「已取尽」标记封顶：一旦某次拉取返回量 < want，即认定该音源到底，
        # 后续直接吃缓存，不再回源。
        exhausted = self._discover_exhausted.get(ck, False)
        if cache is None or (len(cache[1]) < want and not exhausted):
            got = []
            if source in ("netease", "qqmusic"):
                plats = [source]
            else:
                plats = [p for p in ("netease", "qqmusic")
                         if time.monotonic() >= self._discover_fail_until.get(p, 0)]
            if plats:
                # R27：多音源并行拉取。原实现串行（netease 网络差时 15s 超时后
                # 才轮到 qqmusic），「疯狂加载」= 总耗时累加；并行后 = 最慢源。
                # 任一源先返回即采用；慢源不阻塞（shutdown(wait=False) 后台收尾），
                # as_completed timeout 兜底防止慢源拖死本次请求。
                from concurrent.futures import ThreadPoolExecutor, as_completed
                ex = ThreadPoolExecutor(max_workers=len(plats))
                futs = {}
                try:
                    for p in plats:
                        pm = self._playlist_manager()
                        futs[ex.submit(pm.discover, p, limit=want)] = p
                    for fut in as_completed(futs, timeout=6):
                        p = futs[fut]
                        try:
                            got = fut.result() or []
                            if got:
                                break
                        except Exception as e:  # noqa
                            logger.warning("discover %s 失败: %s", p, e)
                            self._discover_fail_until[p] = time.monotonic() + 60
                except Exception as e:  # noqa as_completed 超时
                    logger.warning("discover 并行拉取超时: %s", e)
                finally:
                    ex.shutdown(wait=False)   # 不阻塞等慢源
            if not got:
                return cache[1][offset:offset + limit] if cache else []
            # 是否取尽：返回量 < want 说明该音源到底了，后续直接吃缓存、不再回源
            self._discover_exhausted[ck] = len(got) < want
            # 与旧缓存合并去重（按 platform+ext_id）
            merged = list((cache or (0, []))[1])
            seen = {(m.get("platform"), m.get("ext_id")) for m in merged}
            for m in got:
                d = self._meta_to_dict(m)
                k = (d.get("platform"), d.get("ext_id"))
                if k not in seen:
                    seen.add(k)
                    merged.append(d)
            self._col_cache[ck] = (time.monotonic(), merged)
            cache = self._col_cache[ck]
        return cache[1][offset:offset + limit]

    @Slot(str, int, int, str, result=bool)
    def requestPlaylists(self, group: str, offset: int, limit: int, source: str = None) -> bool:
        """异步取歌单栏目一页：立即返回，结果经 playlistsReady(group, offset, limit, items) 推送。

        group ∈ discover|mine|saved|local；source ∈ None|'netease'|'qqmusic'
        （#140 歌单切换音源：QQ音乐/网易云）。source 仅对 discover/mine/saved 生效，
        local 恒忽略（本地歌单无音源）。缓存键与在飞去重都按 (group, source) 区分，
        故「全部 / QQ / 网易云」三视图各自独立缓存、互不覆盖。
        同步版 discoverPlaylistsPaged 等在 Qt 主线程做音源网络请求（实测 ≈2.6s 整窗冻结），
        R26 全部挪到后台线程；命中全量缓存时同步 emit（零网络）。
        """
        g = str(group or "discover").strip()
        if g not in ("discover", "mine", "saved", "local"):
            g = "discover"
        src = str(source).strip() if source else None
        if src not in ("netease", "qqmusic"):
            src = None
        try:
            off, lim = max(0, int(offset or 0)), max(1, min(60, int(limit or 30)))
        except Exception:  # noqa
            off, lim = 0, 30
        cache_key = g if src is None else f"{g}:{src}"
        # discover 走 TTL 缓存（网络源）；mine/saved/local 总是重新拉取
        # （本地 DB/登录态数据可能刚变：新建歌单、收藏/取消收藏后要立即可见）。
        cache = self._col_cache.get(cache_key)
        if g == "discover" and cache and (time.monotonic() - cache[0]) < self._PL_CACHE_TTL:
            self.playlistsReady.emit(g, off, lim, cache[1][off:off + lim])
            return True
        if (cache_key, off) in self._col_fetching:
            return False   # 同组同音源同 offset 的请求在飞：取完会广播，前端按 group 匹配

        def _run():
            try:
                if g == "discover":
                    items = self._discover_impl(off, lim, src)
                elif g == "mine":
                    items = self._mine_impl(src)
                elif g == "saved":
                    items = [self._meta_to_dict(m) for m in
                             (self._playlist_manager().saved_playlists(src) or [])]
                else:
                    items = [self._meta_to_dict(m) for m in
                             (self._playlist_manager().local_playlists() or [])]
                self._col_cache[cache_key] = (time.monotonic(), items)
            except Exception as e:  # noqa
                logger.warning("歌单栏目取回失败(%s/%s): %s", g, src, e)
                items = list((self._col_cache.get(cache_key) or (0, []))[1])
            finally:
                self._col_fetching.discard((cache_key, off))
            self.playlistsReady.emit(g, off, lim, items[off:off + lim])

        self._col_fetching.add((cache_key, off))
        threading.Thread(target=_run, daemon=True, name="pl-column").start()
        return True

    def _mine_impl(self, source: str = None) -> list:
        """「我的歌单」（需登录；未登录返回空）。同步/异步共用。

        source ∈ None|'netease'|'qqmusic'（#140）：指定时只拉该音源。
        R28：netease/qqmusic **并行**拉取——串行时 netease 网络差会拖满超时
        才轮到 qqmusic，总耗时累加（「歌单疯狂加载」的另一半）；并行后任一源
        先返回即采用，慢源不阻塞（shutdown(wait=False) 后台收尾）。
        """
        pm = self._playlist_manager()
        if not pm:
            return []
        plats = [source] if source in ("netease", "qqmusic") else ["netease", "qqmusic"]
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ex = ThreadPoolExecutor(max_workers=len(plats))
        futs = {}
        try:
            for plat in plats:
                futs[ex.submit(pm.my_playlists, plat)] = plat
            for fut in as_completed(futs, timeout=6):
                plat = futs[fut]
                try:
                    items = fut.result() or []
                    if items:
                        return [self._meta_to_dict(m) for m in items]
                except Exception as e:  # noqa
                    logger.warning("myPlaylists %s 失败: %s", plat, e)
        except Exception as e:  # noqa as_completed 超时
            logger.warning("我的歌单并行拉取超时: %s", e)
        finally:
            ex.shutdown(wait=False)   # 不阻塞等慢源
        return []

    @Slot(int, int, result="QVariant")
    def getFavoritesPage(self, offset: int, limit: int) -> list:
        """分页取「我喜欢的音乐」——前端收藏栏目滚动到底时续拉。

        DB 层可真分页（LIMIT/OFFSET），返回空数组 = 没有更多。
        """
        try:
            off, lim = int(offset or 0), int(limit or 30)
        except Exception:  # noqa
            off, lim = 0, 30
        off, lim = max(0, off), max(1, min(100, lim))
        try:
            rows = self.db.favorites(offset=off, limit=lim)
        except TypeError:
            # 旧版 db.favorites() 不支持分页：退回全量再切片
            try:
                rows = self.db.favorites()[off:off + lim]
            except Exception as e:  # noqa
                logger.warning("getFavoritesPage 失败: %s", e)
                return []
        except Exception as e:  # noqa
            logger.warning("getFavoritesPage 失败: %s", e)
            return []
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "title": r["title"] or "未知标题",
                "artist": r["artist"] or "未知歌手",
                "album": r["album"] or "",
                "source": r["source"] or "local",
                "ext_id": r["ext_id"],
                "cover": self._cover_of_fast(r),
                "duration": self._num(r, "duration"),
            })
        return out

    @Slot(result="QVariant")
    def myPlaylists(self) -> list:
        """我的歌单（需登录；未登录返回空）。同步入口（新前端走 requestPlaylists('mine')）。"""
        return self._mine_impl()

    @Slot(result="QVariant")
    def getFavorites(self) -> list:
        """曲库（我喜欢的音乐）。

        同时带出 cover / duration：orbit「收藏」栏目的卡片要显示封面，
        点卡片起播后进度条要有真实总时长（否则进度条满格/不动）。
        """
        try:
            rows = self.db.favorites()
            out = []
            for r in rows:
                out.append({
                    "id": r["id"],
                    "title": r["title"] or "未知标题",
                    "artist": r["artist"] or "未知歌手",
                    "album": r["album"] or "",
                    "source": r["source"] or "local",
                    "ext_id": r["ext_id"],
                    "cover": self._cover_of_fast(r),
                    "duration": self._num(r, "duration"),
                })
            return out
        except Exception as e:  # noqa
            logger.warning("getFavorites 失败: %s", e)
            return []

    def _source_manager(self):
        """惰性构造并缓存 SourceManager。

        ⚠ data.sources 导出的是 **SourceManager 类**，没有模块级 `source_manager`
        实例 —— 写 `from data.sources import source_manager` 必然 ImportError
        （本文件里另外三处正确写法都是 SourceManager(user_config.data)）。
        构造要读 user_config，所以缓存起来别每次都建。
        """
        sm = getattr(self, "_sm", None)
        if sm is None:
            try:
                from config.settings import user_config
                from data.sources import SourceManager
                sm = SourceManager(user_config.data)
                self._sm = sm
            except Exception as e:  # noqa
                logger.warning("SourceManager 构造失败: %s", e)
                return None
        return sm

    def _cover_of(self, row) -> str:
        """sqlite3.Row / 对象 → 封面 URL（无 cover 时由 source_manager 兜底）。

        ⚠ 必须是**实例方法**：兜底逻辑要调 self._source_manager()。
        曾因插入新方法导致上方 @staticmethod 装饰器错位、这里被当成静态方法，
        于是 self._cover_of(r) 变成 2 个实参 → "takes 1 positional argument but 2 were given"。

        两处修复：
          1) 落库的是旧版 `file://<相对路径>`（见 utils/cover_cache.to_abs_file_url
             的说明），WebEngine 解析不到 → 图片损坏图标。这里统一补成绝对 URL。
          2) 兜底调用 `source_manager.album_cover(ext)` 少传了 source_key
             （真实签名是 album_cover(source_key, ext_id)），必然抛 TypeError
             被 except 吞掉 → 在线曲永远拿不到封面。
        """
        try:
            c = row["cover"]
        except Exception:  # noqa
            c = getattr(row, "cover", None)
        if c:
            try:
                from utils.cover_cache import to_abs_file_url
                return to_abs_file_url(c)
            except Exception:  # noqa
                return c
        try:
            src = row["source"]
            ext = row["ext_id"]
        except Exception:  # noqa
            src = getattr(row, "source", None)
            ext = getattr(row, "ext_id", None)
        if src and ext and src != "local":
            sm = self._source_manager()
            if sm is not None and hasattr(sm, "album_cover"):
                try:
                    return sm.album_cover(src, ext) or ""
                except Exception as e:  # noqa
                    logger.warning("album_cover(%s/%s) 失败: %s", src, ext, e)
            return ""
        return ""

    def _cover_of_fast(self, row) -> str:
        """_cover_of 的「零网络」版本：只做 file:// URL 补全，绝不回源 album_cover。

        为什么需要：列表型 slot（getPlaylistTracks / getFavorites 等）会对**每一行**
        调它——同步 slot 在 Qt 主线程执行，只要有一行缺封面，就多一次网络往返，
        大歌单 = 数十秒串行阻塞（歌单详情卡顿的主因之一）。
        缺封面的行由前端 SongsCover 占位，真实封面走 requestCover 异步补。
        """
        try:
            c = row["cover"]
        except Exception:  # noqa
            c = getattr(row, "cover", None)
        if c:
            try:
                from utils.cover_cache import to_abs_file_url
                return to_abs_file_url(c)
            except Exception:  # noqa
                return c
        return ""

    @Slot(str, str, result=str)
    def coverDataUrl(self, url: str, source: str = "") -> str:
        """把封面取回并转成 data URL；拿不到返回空串。

        为什么要绕后端：
          浏览器里的 <img> 无法携带防盗链 Referer / 自定义 UA，遇到网易云或 QQ 的
          CDN 策略、或音源 API 需要鉴权时就会加载失败；而 utils.cover_cache.fetch_cover_bytes
          内部按音源带正确的请求头（老 Qt UI 一直走这条路，所以老 UI 能显示封面）。
          data URL 还能顺带规避 file:// 页面加载远程图片的混合内容限制。
        """
        if not url:
            return ""
        # 本地缓存文件 / 已是 data URL → 前端直接加载即可，不必绕一圈
        if url.startswith("file://") or url.startswith("data:"):
            return ""
        try:
            from utils.cover_cache import fetch_cover_bytes
            data = fetch_cover_bytes(url, source or None)
        except Exception as e:  # noqa
            logger.warning("封面取回失败(%s): %s", str(url)[:60], e)
            return ""
        if not data:
            return ""
        try:
            import base64
            return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        except Exception:  # noqa
            return ""

    @Slot(str, result=str)
    def wallpaperDataUrl(self, path: str) -> str:
        """本地壁纸文件 → data URL；读不到返回空串。

        为什么需要：前端液态玻璃折射已迁移 WebGL，壁纸要作为 GL 纹理上传；
        file:// 页面属 opaque origin，file:// 图直接 texImage2D 会抛
        SecurityError（coverDataUrl 对 file:// 返回空串、不走这条路），
        data: URL 则完全干净。路径来自后端自己的壁纸描述符，非任意输入。
        """
        try:
            import base64
            p = (path or "").strip()
            if not p:
                return ""
            if p.startswith("file:///"):
                p = p[8:]
            elif p.startswith("file://"):
                p = p[7:]
            p = p.replace("/", os.sep)
            p = os.path.normpath(p)
            if not os.path.isfile(p):
                return ""
            ext = os.path.splitext(p)[1].lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".bmp": "image/bmp",
                    ".gif": "image/gif"}.get(ext, "image/jpeg")
            with open(p, "rb") as f:
                data = f.read()
            return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        except Exception as e:  # noqa
            logger.warning("wallpaperDataUrl 失败(%s): %s", str(path)[:60], e)
            return ""

    @staticmethod
    def _num(row, key: str) -> float:
        try:
            v = row[key]
        except Exception:  # noqa
            v = getattr(row, key, 0)
        try:
            return float(v or 0)
        except Exception:  # noqa
            return 0.0

    @Slot("QVariant", result=bool)
    def addFavorite(self, track) -> bool:
        """收藏一首歌到曲库（先 upsert 再 favorite）。track: {title,artist,album,source,ext_id}。"""
        try:
            t = track if isinstance(track, dict) else {}
            tid = self.db.upsert_track(
                path=t.get("ext_id") or t.get("path") or t.get("title", ""),
                title=t.get("title", "未知"),
                artist=t.get("artist", "未知"),
                album=t.get("album", ""),
                genre="", year=0, duration=0, bpm=0,
                source=t.get("source", "local"),
                ext_id=t.get("ext_id"),
                cover=t.get("cover"),
                api_metadata=t.get('api_metadata'),
            )
            self.db.toggle_favorite(tid)
            self.toast.emit("已收藏：" + t.get("title", ""))
            return True
        except Exception as e:  # noqa
            logger.warning("addFavorite 失败: %s", e)
            return False

    @Slot(str, result=bool)
    def removeFavorite(self, track_id: str) -> bool:
        try:
            self.db.toggle_favorite(int(track_id))
            self.toast.emit("已取消收藏")
            return True
        except Exception as e:  # noqa
            logger.warning("removeFavorite 失败: %s", e)
            return False

    @staticmethod
    def _meta_to_dict(m) -> dict:
        try:
            d = m if isinstance(m, dict) else m.__dict__
            return {
                "platform": d.get("platform"),
                "ext_id": str(d.get("ext_id")),
                "name": d.get("name") or "未命名歌单",
                "cover": d.get("cover") or "",
                "creator": d.get("creator") or "",
                "track_count": d.get("track_count") or 0,
                "description": d.get("description") or "",
            }
        except Exception:  # noqa
            return {}

    # ---------------- 拖放文件（Electron 用 file.path；本宿主用本地路径） ----------------
    @Slot(str,  result=str)
    def fileToSource(self, path: str) -> str:
        return path

    # ================ 完整模式：播放 / 推荐 / 歌词 / AI 口播 ================

    def attach_radio(self, radio) -> bool:
        """运行期挂载 AIRadioController（**先开窗、后台装配、就绪后接上**）。

        与 __init__(radio=...) 等价，但可在窗口已显示之后再调用。
        为什么需要它：_init_radio() 会串行加载 CLAP / LLM 权重（实测约 70s），
        旧实现把它放在创建窗口**之前**，用户点开后要干等一分多钟才看见界面。
        改为先开窗（秒开，皮肤/歌单/收藏立即可用），电台在后台线程装配，
        就绪后经本方法挂上并补推一次推荐/队列，前端自动接管。

        ⚠ 必须在 GUI 线程调用（会创建 QTimer、emit 信号）。重复挂载会被忽略。
        """
        if radio is None or self.radio is not None:
            return False
        self.radio = radio
        try:
            self._wire_radio_events()
        except Exception as e:  # noqa
            logger.warning("attach_radio 事件绑定失败: %s", e)
        # ⚠ 关键：控制器是在**后台线程**装配的，它内部的 _QtMainThread(QObject) 会
        # 把线程亲和性落在那个工作线程上 → 后台 _emit 的信号被排进一个没有事件循环
        # 的线程队列 → 事件永远到不了主线程，前端拿不到推荐/播放事件（表现为
        # 「电台已挂载但界面一直显示演示假歌」）。这里在 GUI 线程重建事件桥。
        try:
            from ai.radio import _QtMainThread
            radio._qt_main = _QtMainThread()
            radio._qt_main.bind(radio._dispatch)
            logger.info("电台事件桥已重建（绑定到 GUI 线程）")
        except Exception as e:  # noqa
            logger.warning("重建电台事件桥失败（事件可能延迟）: %s", e)
        logger.info("AIRadioController 已挂载（完整模式，界面无需重启）")
        try:
            self.toast.emit("AI 电台已就绪")
        except Exception:  # noqa
            pass
        # 装配期间（事件桥还没重建时）产生的 reco/queue 事件会丢：用补推泵兜住，
        # 直到前端拿到第一批真实曲目/队列为止（最多约 3 分钟）。
        self._start_catchup_pump()
        return True

    def _start_catchup_pump(self) -> None:
        """补推泵：装配期丢失的事件靠它找补（拿到第一批真实数据即停）。"""
        state = {"reco": False, "queue": False, "n": 0}

        def _tick():
            r = self.radio
            if r is None:
                return
            state["n"] += 1
            try:
                if not state["reco"]:
                    entries = r.get_reco_entries() or []
                    if entries:
                        self.recoUpdated.emit(
                            [self._reco_entry_dict(e) for e in entries])
                        state["reco"] = True
                        logger.info("补推：推荐货架 %d 首已下发前端", len(entries))
            except Exception:  # noqa
                pass
            try:
                if not state["queue"]:
                    pl = r.get_playlist_entries() or []
                    if pl:
                        self.playlistUpdated.emit(
                            [self._entry_to_dict(e) for e in pl])
                        state["queue"] = True
            except Exception:  # noqa
                pass
            if (state["reco"] and state["queue"]) or state["n"] >= 60:
                return
            QTimer.singleShot(3000, _tick)

        QTimer.singleShot(1500, _tick)

    def _wire_radio_events(self):
        """把 AIRadioController 的事件投递到前端 Signal。"""
        r = self.radio
        try:
            r.on("track_changed", lambda track: self.trackChanged.emit(self._track_to_dict(track)))
            r.on("playlist_updated", lambda entries: self.playlistUpdated.emit(
                [self._entry_to_dict(e) for e in entries]))
            r.on("dj_speech", lambda text: self.aiSpeech.emit(text))
            r.on("lyrics", lambda text: self.lyricsReady.emit({"text": text}))
            r.on("lyric_line", lambda idx, text: self.lyricsReady.emit({"line": text, "index": idx}))
            r.on("progress", lambda ms: self.progressChanged.emit(int(ms)))
            r.on("playing", lambda on: self.playingChanged.emit(bool(on)))
            r.on("reco_updated", lambda entries: self.recoUpdated.emit(
                [self._reco_entry_dict(e) for e in entries]))
            r.on("thinking", lambda on: self.thinkingChanged.emit(bool(on)))
            r.on("recommend_reason", lambda text: self.recommendReason.emit(str(text or "")))
            r.on("history_changed", lambda _h: None)   # 占位：避免事件无回调时被吞
            r.on("library_changed", lambda _l: None)
        except Exception as e:  # noqa
            logger.warning("radio 事件绑定失败: %s", e)
        self._start_state_pump()

    # ---------------- 播放状态泵（前端进度/播放态的兜底驱动） ----------------

    def _start_state_pump(self) -> None:
        """定时把真实播放状态推给前端。

        为什么需要它（三条缺一不可）：
          1) 无音频设备的「静默模式」下 PlayerEngine.play() 直接置 PLAYING 并起
             一个 Timer 兜底结束，**不启动位置轮询线程** → 永远不会 emit "progress"，
             前端进度条/边框进度/歌词时间轴全部静止。
          2) pygame 位置线程只在 PLAYING 时推进，异常退出（如 mixer not initialized
             崩溃）后无任何事件源，前端会永远停留在最后位置。
          3) 前端只靠点击本地 state.playing 判断播放态，与后端真实状态可能不一致
             （比如后端自动续播/暂停）。这里做权威校对。
        仅在数值变化时 emit，避免每 500ms 无谓刷前端。
        """
        self._pump_last = (-1, None)
        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(500)
        self._pump_timer.timeout.connect(self._pump_play_state)
        self._pump_timer.start()

    def _pump_play_state(self) -> None:
        r = self.radio
        if not r:
            return
        try:
            st = self.getPlayState()
        except Exception:  # noqa
            return
        try:
            pos = int(st.get("position") or 0)
            playing = bool(st.get("playing"))
            last_pos, last_playing = self._pump_last
            if last_playing is None or playing != last_playing:
                self.playingChanged.emit(playing)
            if playing and pos != last_pos:
                self.progressChanged.emit(pos)
            self._pump_last = (pos, playing)
        except Exception:  # noqa
            pass

    @staticmethod
    def _reco_entry_dict(e) -> dict:
        """推荐货架 entry → 前端 dict（id/title/artist/current/favorited/disliked）。

        注意：radio 侧的 get_reco_entries() 已返回带 cover 的 dict（含 source/ext_id），
        这里透传即可；对非 dict 对象（老路径）补一份零网络兜底。
        ⚠ 绝不能在这里回源 album_cover：本方法会被同步 slot（getRecoEntries）与
        GUI 线程的信号回调调用，网络会冻结整窗（R27 卡顿根因之一）。
        """
        if isinstance(e, dict):
            out = dict(e)
            out.setdefault("current", False)
            return out
        try:
            return {
                "id": getattr(e, "id", None),
                "title": getattr(e, "title", "未知"),
                "artist": getattr(e, "artist", "未知"),
                "current": False,
                "favorited": False,
                "disliked": False,
                "source": getattr(e, "source", "local") or "local",
                "ext_id": getattr(e, "ext_id", None),
                "cover": getattr(e, "cover", None) or "",
            }
        except Exception:  # noqa
            return {}

    @staticmethod
    def _track_to_dict(t) -> dict:
        """Track 对象 → 前端可消费的 dict。"""
        if t is None:
            return {}
        try:
            d = t if isinstance(t, dict) else {
                "id": getattr(t, "id", None),
                "title": getattr(t, "title", "未知"),
                "artist": getattr(t, "artist", "未知"),
                "album": getattr(t, "album", ""),
                "source": getattr(t, "source", "local"),
                "ext_id": getattr(t, "ext_id", None),
                "duration": getattr(t, "duration", 0),
                "cover": getattr(t, "cover", None),
            }
            return d
        except Exception:  # noqa
            return {}

    def _entry_to_dict(self, e) -> dict:
        """playlist entry → 前端 dict（含封面）。

        ⚠ R27 铁律：封面只走 _cover_of_fast（**零网络**）。
        本方法会被三类「主线程」路径调用：
          1) 同步 slot（getQueue / getRecommendations / getFavorites*）——
             前端轮询每几秒一次，任何一次回源 album_cover 都冻住整窗；
          2) GUI 线程的信号回调（playlistUpdated 经 attach_radio 重建的事件桥
             投递到 GUI 线程）——网络在这里一样冻窗；
          3) 前端高频轮询 getPlayState 时队列重建。
        缺封面的行由前端程序化占位 + 异步 requestCover 兜底；真实封面已在
        音源层/radio 层尽量补齐（search/_resolve_selections 的 R27 修复）。
        """
        try:
            d = e if isinstance(e, dict) else {
                "id": getattr(e, "id", None),
                "title": getattr(e, "title", "未知"),
                "artist": getattr(e, "artist", "未知"),
                "album": getattr(e, "album", ""),
                "source": getattr(e, "source", "local"),
                "ext_id": getattr(e, "ext_id", None),
                "duration": getattr(e, "duration", 0),
            }
            if isinstance(e, dict):
                d.setdefault("title", "未知")
                d.setdefault("artist", "未知")
                d.setdefault("source", "local")
            d.setdefault("current", False)
            d.setdefault("favorited", False)
            d.setdefault("disliked", False)
            d["cover"] = self._cover_of_fast(e) or ""
            return d
        except Exception:  # noqa
            return {}

    # ---- 推荐 / 播放列表 ----

    @Slot(result="QVariant")
    def getRecommendations(self) -> list:
        """推荐列表（漫游模式）：当前曲 + 待播队列。冷启动队列为空时回退推荐货架。"""
        r = self.radio
        if not r:
            return []
        try:
            entries = r.get_playlist_entries()
            if not entries:
                entries = r.get_reco_entries()   # 冷启动：推荐货架兜底，前端漫游列表有内容
            return [self._entry_to_dict(e) for e in entries]
        except Exception as e:  # noqa
            logger.warning("getRecommendations 失败: %s", e)
            return []

    @Slot(result="QVariant")
    def getQueue(self) -> list:
        """播放队列（当前曲 + 待播 upcoming），不兜底推荐货架。前端 now-stack 5 卡片用。"""
        r = self.radio
        if not r:
            return []
        try:
            entries = r.get_playlist_entries()
            return [self._entry_to_dict(e) for e in entries]
        except Exception as e:  # noqa
            logger.warning("getQueue 失败: %s", e)
            return []

    @Slot(result="QVariant")
    def getCurrentTrack(self):
        r = self.radio
        if not r:
            return None
        try:
            return self._track_to_dict(r.current)
        except Exception:  # noqa
            return None

    @Slot(result="QVariant")
    def getHistory(self) -> list:
        r = self.radio
        if not r:
            return []
        try:
            return [self._track_to_dict(t) for t in r.get_history()]
        except Exception:  # noqa
            return []

    @Slot(result="QVariant")
    def getLibrary(self) -> list:
        """曲库（我喜欢的音乐）= 收藏页。轻量模式回退 db 收藏。"""
        r = self.radio
        if r is not None:
            try:
                return [self._track_to_dict(t) for t in r.get_library()]
            except Exception as e:  # noqa
                logger.warning("getLibrary(radio) 失败: %s", e)
                return []
        return self.getFavorites()

    # ---- 播放控制 ----

    @Slot(bool, result=bool)
    def togglePlay(self, force_on: bool) -> bool:
        """播放/暂停。

        ⚠ 必须走 AIRadioController.toggle_play()，绝不能直接调 player.play()：
          1) 冷启动用 start_radio(auto_start=False)（只填推荐货架、不开声），
             此时 player 无 _current_path，player.play() 会直接 return（静默无操作），
             表现为「点 ▶ 没声音、进度条不动」——即"前后端音频没连上"；
          2) player.play() 会把 _seek_offset_ms 清零且 _ever_played=False，
             在暂停态调用会让进度条弹回 0；
          3) player.play() 不发 "playing" 事件、不处理转场定时器，
             前端 playingChanged / progressChanged 永远收不到。
        radio.toggle_play() 内部：player.toggle() + emit("playing") +
        暂停冻结/恢复转场定时器，是老 UI 播放键的同一入口。
        """
        r = self.radio
        if not r:
            return False
        try:
            playing = r.player.state.value == "playing"
            if force_on and not getattr(r, "current", None):
                # 冷启动/电台从未起播：真正拉起电台（推荐填充 → 选曲 → 取链 → 起播）。
                # 放后台线程：QWebChannel 槽跑在 Qt 主线程，而 start_radio 在 LLM 未就绪时
                # 要走 _refill_reco/_refill_initial，同步路径会卡住整个界面。
                self._kick_radio()
                return True
            if force_on == playing:
                return True                      # 状态已一致，避免重复 toggle 造成暂停
            r.toggle_play()
            return True
        except Exception as e:  # noqa
            logger.warning("togglePlay 失败: %s", e)
            return False

    def _kick_radio(self, seed_track=None) -> None:
        """后台拉起电台（start_radio 自带重入合并，重复点击只会采纳最新一次）。"""
        import threading

        def _run():
            try:
                self.thinkingChanged.emit(True)
                if seed_track is not None:
                    self.radio.start_radio(seed_track=seed_track)
                else:
                    self.radio.start_radio()
            except Exception as e:  # noqa
                logger.warning("start_radio 失败: %s", e)
                self.toast.emit("起播失败：" + str(e))
            finally:
                try:
                    self.thinkingChanged.emit(False)
                except Exception:  # noqa
                    pass

        threading.Thread(target=_run, daemon=True, name="radio-kick").start()

    @Slot(result=bool)
    def nextTrack(self) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.next()
            return True
        except Exception as e:  # noqa
            logger.warning("nextTrack 失败: %s", e)
            return False

    @Slot(result=bool)
    def prevTrack(self) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.prev()
            return True
        except Exception as e:  # noqa
            logger.warning("prevTrack 失败: %s", e)
            return False

    @Slot(int, result=bool)
    def jumpTo(self, index: int) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.jump_to(index)
            return True
        except Exception as e:  # noqa
            logger.warning("jumpTo 失败: %s", e)
            return False

    @Slot(int, result=bool)
    def seekTo(self, ms: int) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.player.seek(ms)
            return True
        except Exception as e:  # noqa
            logger.warning("seekTo 失败: %s", e)
            return False

    @Slot(float, result=bool)
    def setVolume(self, vol: float) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.player.set_volume(vol)
            return True
        except Exception as e:  # noqa
            logger.warning("setVolume 失败: %s", e)
            return False

    @Slot(result=bool)
    def startRadio(self) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.start_radio()
            return True
        except Exception as e:  # noqa
            logger.warning("startRadio 失败: %s", e)
            return False

    @Slot(bool, result=bool)
    def toggleRadio(self, on: bool) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.set_roam_on(on)   # 开电台 = 立即补满队列续推（而非仅置标志位）
            return True
        except Exception:  # noqa
            return False

    @Slot(bool, result=bool)
    def toggleDJ(self, on: bool) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.set_dj_on(on)     # 开 DJ = 立即预取下一首口播
            return True
        except Exception:  # noqa
            return False

    @Slot(str, result=bool)
    def setPlayMode(self, mode: str) -> bool:
        """播放模式：order / random / loop_one。变化经 playModeChanged 广播，
        供 dock 图标 / 栏目胶囊 / 设置面板三处入口同步显示。"""
        r = self.radio
        if not r:
            return False
        try:
            r.set_play_mode(mode)
            self.playModeChanged.emit(mode)
            return True
        except Exception:  # noqa
            return False

    @Slot(int, result=bool)
    def setFamiliarity(self, level: int) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.familiarity = max(0, min(3, level))
            from config.settings import user_config
            user_config.data["rec_pref"] = r.familiarity
            user_config.save()
            return True
        except Exception as e:  # noqa
            logger.warning("setFamiliarity 失败: %s", e)
            return False

    # ---- 歌单播放 ----

    def _playlist_tracks_raw(self, platform: str, ext_id) -> list:
        """取歌单内曲目（原始 Track 对象列表）。

        ⚠ 这里修掉一个被静默吞掉的 Bug：
        旧代码调用 pm.playlist_tracks(platform, ext_id)，但 PlaylistManager 上
        **没有这个方法**（真实方法名是 tracks(platform, ext_id)；
        db.playlist_tracks 是「按本地歌单 id 取」、签名也完全不同）。
        结果每次都抛 AttributeError，被外层 except 吞掉后只留一条 warning，
        表现为「点歌单没反应 / 读不到歌单里的歌」。
        """
        if ext_id is None or str(ext_id) == "":
            return []
        plat = (platform or "").strip()
        # 本地歌单：曲目存在 playlists/playlist_items 里，按本地 id 取
        if plat == "local":
            try:
                return list(self.db.playlist_tracks(int(ext_id)) or [])
            except Exception as e:  # noqa
                logger.warning("取本地歌单曲目失败(id=%s): %s", ext_id, e)
                return []
        pm = self._playlist_manager()
        if not pm:
            return []
        try:
            return list(pm.tracks(plat, ext_id) or [])
        except Exception as e:  # noqa
            logger.warning("取歌单曲目失败(%s/%s): %s", plat, ext_id, e)
            return []

    @Slot(str, str, result="QVariant")
    def getPlaylistTracks(self, platform: str, ext_id: str) -> list:
        """列出歌单内的曲目（**只返回，不播放**）—— 前端「打开歌单 → 选曲」用它。

        返回 [{id,title,artist,album,source,ext_id,duration,cover}]，空数组表示取不到。
        """
        out = []
        for t in self._playlist_tracks_raw(platform, ext_id):
            d = self._track_to_dict(t)
            if not d:
                continue
            d.setdefault("title", "未知标题")
            d.setdefault("artist", "未知歌手")
            # 封面只做零网络兜底（本地缓存 URL 补全）：同步 slot 里逐行回源
            # album_cover 会把主线程阻塞数秒；缺封面交给前端 requestCover 异步补
            d["cover"] = self._cover_of_fast(t) or ""
            out.append(d)
        return out

    @Slot(str, str, result=bool)
    def playPlaylist(self, platform: str, ext_id: str) -> bool:
        """获取歌单曲目并开始播放。"""
        r = self.radio
        if not r:
            return False
        tracks = self._playlist_tracks_raw(platform, ext_id)
        if not tracks:
            return False
        try:
            r.play_tracks(tracks, "歌单播放")
            return True
        except Exception as e:  # noqa
            logger.warning("playPlaylist 失败: %s", e)
            return False

    # ---- 异步版列表/封面（R25：网络挪出 Qt 主线程，根治歌单详情卡顿） ----

    _PL_CACHE_TTL = 300  # 秒；歌单曲目短时间内重开不重拉

    def _fill_covers_batch(self, dicts: list) -> int:
        """批量补齐缺封面的行：{source: [ids...]} → 每个音源**一次**网络往返。

        ⚠ 绝不能逐首调 album_cover：实测 104ms/首，100 首 ≈10s 串行；
        而批量 songs_detail 100 首只要 ~150ms。返回补上的封面数。
        """
        need = [d for d in dicts
                if not d.get("cover") and d.get("ext_id")
                and (d.get("source") or "local") != "local"]
        if not need:
            return 0
        sm = self._source_manager()
        if sm is None or not hasattr(sm, "album_covers"):
            return 0
        by_src: dict = {}
        for d in need:
            by_src.setdefault(d["source"], []).append(d)
        filled = 0
        for src, rows in by_src.items():
            # 按 300 分批：批量接口单次上限 300（大歌单可能上千首）
            for i in range(0, len(rows), 300):
                chunk = rows[i:i + 300]
                try:
                    m = sm.album_covers(src, [str(r["ext_id"]) for r in chunk]) or {}
                except Exception as e:  # noqa
                    logger.warning("批量封面失败(%s): %s", src, e)
                    continue
                for r in chunk:
                    c = m.get(str(r["ext_id"]))
                    if c:
                        r["cover"] = c
                        filled += 1
        return filled

    def _pl_tracks_dicts(self, platform: str, ext_id, deep_cover: bool = False) -> list:
        """Track 列表 → 前端 dict 列表。

        deep_cover=False（默认，同步 slot 用）：封面走 _cover_of_fast，零网络。
        deep_cover=True（后台线程用）：额外跑一次 `_fill_covers_batch()`
        把缺封面的行补齐——批量接口，1 次网络往返，不阻塞 Qt 主线程。
        """
        out = []
        for t in self._playlist_tracks_raw(platform, ext_id):
            d = self._track_to_dict(t)
            if not d:
                continue
            d.setdefault("title", "未知标题")
            d.setdefault("artist", "未知歌手")
            d["cover"] = self._cover_of_fast(t) or ""
            out.append(d)
        if deep_cover:
            self._fill_covers_batch(out)
        return out

    _PL_PAGE = 60          # 首页/每页条数（大歌单分页续拉，避免一次取全量）

    def _pl_full(self, plat: str, ext_id: str, on_ready) -> bool:
        """取全量曲目（命中缓存立即回调，否则后台线程取完回调）。
        返回 True 表示已同步回调；False 表示已派发后台任务（稍后回调）。"""
        key = (plat, str(ext_id or ""))
        hit = self._pl_tracks_cache.get(key)
        if hit and (time.monotonic() - hit[0]) < self._PL_CACHE_TTL:
            on_ready(key, hit[1])
            return True
        if key in self._pl_fetching:
            return False   # 已在取：取完由 _pl_fetch 统一广播首页，续页另行请求

        def _run():
            try:
                # 后台线程里批量补封面（分批、不阻塞主线程）
                dicts = self._pl_tracks_dicts(plat, key[1], deep_cover=True)
                self._pl_tracks_cache[key] = (time.monotonic(), dicts)
            except Exception as e:  # noqa
                logger.warning("歌单曲目取回失败(%s/%s): %s", plat, key[1], e)
                dicts = []
            finally:
                self._pl_fetching.discard(key)
            on_ready(key, dicts)

        self._pl_fetching.add(key)
        threading.Thread(target=_run, daemon=True, name="pl-tracks").start()
        return False

    @Slot(str, str, result=bool)
    def requestPlaylistTracks(self, platform: str, ext_id: str) -> bool:
        """异步取歌单**首页**（默认 60 首）：立即返回，结果经 playlistTracksReady 推送。

        为什么异步化：pm.tracks() 底层是在线音源请求（网易云 musicbox/HTTP API），
        同步 slot 期间 Qt 主线程被网络阻塞 —— 「点歌单整窗卡住数秒」。
        为什么分页：千首歌单一次取全量既慢又浪费（前端还要一次渲染上千行）；
        全量在后端缓存后按页切片，续页走 requestPlaylistTracksPage。
        """
        plat = (platform or "").strip()
        if not str(ext_id or ""):
            self.playlistTracksReady.emit(plat, "", [])
            return True

        def _emit(key, dicts):
            self.playlistTracksReady.emit(key[0], key[1], dicts[:self._PL_PAGE])

        self._pl_full(plat, ext_id, _emit)
        return True

    @Slot(str, str, int, int, result=bool)
    def requestPlaylistTracksPage(self, platform: str, ext_id: str,
                                  offset: int, limit: int) -> bool:
        """取续页：立即返回，结果经 playlistTracksPage(platform, ext_id, offset, rows) 推送。

        返回空数组 = 没有更多。命中全量缓存时零网络（同步 emit）。
        """
        plat = (platform or "").strip()
        try:
            off = max(0, int(offset or 0))
            lim = max(1, min(200, int(limit or self._PL_PAGE)))
        except Exception:  # noqa
            off, lim = 0, self._PL_PAGE
        if not str(ext_id or ""):
            self.playlistTracksPage.emit(plat, "", off, [])
            return True

        def _emit(key, dicts):
            self.playlistTracksPage.emit(key[0], key[1], off, dicts[off:off + lim])

        self._pl_full(plat, ext_id, _emit)
        return True

    @Slot(str, str, result=bool)
    def requestCover(self, url: str, source: str = "") -> bool:
        """异步版 coverDataUrl：立即返回，封面经 coverReady 信号 (url, dataUrl) 推送。

        旧同步版每张封面都在 Qt 主线程下载（fetch_cover_bytes timeout 10s、
        失败重试一次）：首次打开歌单详情 = 每行一张封面 × 串行主线程下载 =
        「加载很慢 + 整窗卡死」。改为 4 线程池并发下载，结果统一走信号回填。

        R33 兜底：source 缺失/错误时按 URL 域名推断音源，
        确保网易云/QQ CDN 拿到正确的 Referer，避免前端偶发传空 source 导致 403。
        """
        if not url:
            return False
        # R33 兜底：source 缺失/错误时按 URL 域名推断音源，
        # 确保 Referer 正确（网易云/QQ CDN 对无 Referer 请求直接 403）。
        src = source or ""
        if not src and isinstance(url, str):
            low = url.lower()
            if "music.126.net" in low:
                src = "netease"
            elif "y.qq.com" in low or "y.gtimg.cn" in low:
                src = "qqmusic"
        # 本地缓存 / data URL 前端直接加载即可（与 coverDataUrl 同规则）
        if url.startswith("file://") or url.startswith("data:"):
            return False
        if url in self._cover_jobs:
            return True   # 已在下载，完成时会广播

        def _run(u=url, s=src):
            dataurl = ""
            try:
                from utils.cover_cache import fetch_cover_bytes
                data = fetch_cover_bytes(u, s or None)
                if data:
                    import base64
                    dataurl = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
            except Exception as e:  # noqa
                logger.warning("封面取回失败(%s): %s", str(u)[:60], e)
            finally:
                self._cover_jobs.discard(u)
            self.coverReady.emit(u, dataurl)

        self._cover_jobs.add(url)
        self._cover_pool.submit(_run)
        return True

    @Slot(result=bool)
    def playAllFavorites(self) -> bool:
        """「我喜欢的音乐」播放全部：收藏曲目整体入队（后台线程，不阻塞主线程）。

        收藏行 → Track 对象走 library.by_id / db.get_track 双通道（与
        playTrackById 同路径）；入队交给 radio.play_tracks（后台线程调用，
        与 _kick_radio 同一并发模式）。
        """
        r = self.radio
        if not r:
            return False

        def _run():
            try:
                rows = self.db.favorites() or []
                tracks = []
                for row in rows:
                    t = None
                    try:
                        t = r.library.by_id(int(row["id"]))
                    except Exception:  # noqa
                        t = None
                    if t is None:
                        try:
                            t = r.db.get_track(int(row["id"]))
                        except Exception:  # noqa
                            t = None
                    if t is not None:
                        tracks.append(t)
                if not tracks:
                    self.toast.emit("「我喜欢的音乐」还是空的")
                    return
                r.play_tracks(tracks, "我喜欢的音乐")
            except Exception as e:  # noqa
                logger.warning("playAllFavorites 失败: %s", e)
                self.toast.emit("播放全部失败：" + str(e))

        threading.Thread(target=_run, daemon=True, name="play-favs").start()
        return True

    # ---- 推荐页 / 队列操作（完整模式） ----

    @Slot(result="QVariant")
    def getRecoEntries(self) -> list:
        """推荐货架 entries（与播放队列复制共存）。"""
        r = self.radio
        if not r:
            return []
        try:
            return [self._reco_entry_dict(e) for e in r.get_reco_entries()]
        except Exception as e:  # noqa
            logger.warning("getRecoEntries 失败: %s", e)
            return []

    @Slot(int, result=bool)
    def playFromReco(self, track_id: int) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.play_from_reco(track_id)
            return True
        except Exception as e:  # noqa
            logger.warning("playFromReco 失败: %s", e)
            return False

    @Slot(int, result=bool)
    def removeFromReco(self, track_id: int) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.remove_from_reco(track_id)
            return True
        except Exception as e:  # noqa
            logger.warning("removeFromReco 失败: %s", e)
            return False

    @Slot(int, result=bool)
    def playTrackById(self, track_id: int) -> bool:
        """按数据库 id 直接起播一首（orbit「收藏」栏目点卡片时用）。

        收藏/曲库里的曲目不一定在推荐货架、也不一定在播放队列，play_from_reco /
        jump_to 都用不上；这里回库查出 Track 后交给 start_radio(seed_track=...)
        作为开场曲起播（实时取链 + 自动 upsert，与老 UI 曲库双击同路径）。
        """
        r = self.radio
        if not r:
            return False
        try:
            tid = int(track_id)
        except Exception:  # noqa
            return False
        try:
            t = None
            # 优先在当前队列/推荐里命中（避免回库开销与 id 语义差异）
            try:
                for e in (r.get_playlist_entries() or []):
                    if int(e.get("id") or -1) == tid:
                        idx = [x.get("id") for x in r.get_playlist_entries()].index(tid)
                        r.jump_to(idx)
                        return True
            except Exception:  # noqa
                pass
            # 回库查 Track（MusicLibrary.by_id / Database.get_track 双通道兜底）
            try:
                t = r.library.by_id(tid)
            except Exception:  # noqa
                t = None
            if t is None:
                try:
                    t = r.db.get_track(tid)
                except Exception:  # noqa
                    t = None
            if t is None:
                self.toast.emit("未找到该曲目（可能已被删除）")
                return False
            self._kick_radio(seed_track=t)   # 后台起播，避免卡住 Qt 主线程
            return True
        except Exception as e:  # noqa
            logger.warning("playTrackById 失败: %s", e)
            return False

    @Slot(result=bool)
    def playAllReco(self) -> bool:
        r = self.radio
        if not r:
            return False
        try:
            r.play_all_reco()
            return True
        except Exception as e:  # noqa
            logger.warning("playAllReco 失败: %s", e)
            return False

    @Slot(int, result=bool)
    def removeFromQueue(self, track_id: int) -> bool:
        """队列叉号：跳过这一首（当前曲则切下一首）。"""
        r = self.radio
        if not r:
            return False
        try:
            r.remove_from_queue(track_id)
            return True
        except Exception as e:  # noqa
            logger.warning("removeFromQueue 失败: %s", e)
            return False

    @Slot("QVariant", result=bool)
    def playNextTrack(self, track) -> bool:
        """曲库右键「下一首播放」：插入待播首位。"""
        r = self.radio
        if not r:
            return False
        try:
            r.play_next(track)
            return True
        except Exception as e:  # noqa
            logger.warning("playNextTrack 失败: %s", e)
            return False

    @Slot("QVariant", result=bool)
    def addToRecommendList(self, track) -> bool:
        """曲库右键「添加到推荐列表」：加入队列末尾。"""
        r = self.radio
        if not r:
            return False
        try:
            r.add_to_recommend_list(track)
            return True
        except Exception as e:  # noqa
            logger.warning("addToRecommendList 失败: %s", e)
            return False

    @Slot(int, result=bool)
    def deleteFromLibrary(self, track_id: int) -> bool:
        """曲库删除：移出曲库（取消收藏）并从库删除。"""
        r = self.radio
        if not r:
            return False
        try:
            r.delete_from_library(track_id)
            return True
        except Exception as e:  # noqa
            logger.warning("deleteFromLibrary 失败: %s", e)
            return False

    @Slot(int, result=bool)
    def removeHistory(self, track_id: int) -> bool:
        """播放历史叉号：删除一条历史记录。"""
        r = self.radio
        if not r:
            return False
        try:
            r.remove_from_history(track_id)
            return True
        except Exception as e:  # noqa
            logger.warning("removeHistory 失败: %s", e)
            return False

    @Slot(result="QVariant")
    def getPlayState(self) -> dict:
        """当前播放状态快照：{playing, position, duration, title, artist, mode}。

        ⚠ PlayerEngine 的位置 API 是 get_position()（方法）与 _duration_ms（属性），
        之前误写的 position_ms / duration_ms 两个属性根本不存在，导致本方法恒返回
        全 0 —— 前端拿不到时长/进度，进度条与歌词时间轴全部失效。
        """
        r = self.radio
        if not r:
            return {"playing": False, "position": 0, "duration": 0,
                    "title": "", "artist": "", "mode": "order"}
        try:
            p = r.player
            cur = getattr(r, "current", None)
            return {
                "playing": p.state.value == "playing" if hasattr(p, "state") else False,
                "position": int(p.get_position() if hasattr(p, "get_position") else 0),
                "duration": int(getattr(p, "_duration_ms", 0) or 0)
                            or int((getattr(cur, "duration", 0) or 0) * 1000),
                "title": getattr(cur, "title", "") or "",
                "artist": getattr(cur, "artist", "") or "",
                "mode": getattr(r, "play_mode", "order"),
                # R27：当前曲完整快照（id/cover/source/ext_id），供前端 2s 轮询
                # 精确对账——trackChanged 信号丢失时也能自愈「下一首不更新」。
                "current": self._track_to_dict(cur) or None,
            }
        except Exception as e:  # noqa
            logger.warning("getPlayState 失败: %s", e)
            return {"playing": False, "position": 0, "duration": 0,
                    "title": "", "artist": "", "mode": "order"}

    # ---- 本地文件导入（对应老 UI 的「导入」菜单：选文件 / 选文件夹） ----

    @Slot("QVariant", result=bool)
    def importFiles(self, paths) -> bool:
        """导入本地音频文件到曲库（自动红心点亮，等同老 UI _add_local_paths）。
        paths: 前端拖拽/选择的文件绝对路径列表。"""
        if not paths:
            return False
        if isinstance(paths, str):
            paths = [paths]
        paths = [p for p in paths if isinstance(p, str) and p.strip()]
        if not paths:
            return False
        r = self.radio
        try:
            if r is not None and hasattr(r, "library"):
                n = r.library.add_local_files(paths)
                r.mark_taste_dirty()
                r._emit_playlist()
                r._emit_library() if hasattr(r, "_emit_library") else None
            else:
                from data.library import MusicLibrary
                n = MusicLibrary(self.db).add_local_files(paths)
            if n:
                self.toast.emit(f"已添加 {n} 首到本地曲库（已自动红心点亮）。")
            else:
                self.toast.emit("没有可导入的音频文件（请选择 mp3/flac/m4a/ogg/wav/wma/aac）。")
            return bool(n)
        except Exception as e:  # noqa
            logger.warning("importFiles 失败: %s", e)
            return False

    @Slot(str, result=bool)
    def importFolder(self, folder: str) -> bool:
        """递归导入文件夹下的音频到曲库（等同老 UI _add_local_folder）。"""
        folder = (folder or "").strip()
        if not folder:
            return False
        r = self.radio
        try:
            if r is not None and hasattr(r, "library"):
                n = r.library.add_local_folder(folder)
                r.mark_taste_dirty()
                r._emit_playlist()
                r._emit_library() if hasattr(r, "_emit_library") else None
            else:
                from data.library import MusicLibrary
                n = MusicLibrary(self.db).add_local_folder(folder)
            if n:
                self.toast.emit(f"已从文件夹「{folder}」添加 {n} 首（已自动红心点亮）。")
            else:
                self.toast.emit("该文件夹没有可导入的音频文件。")
            return bool(n)
        except Exception as e:  # noqa
            logger.warning("importFolder 失败: %s", e)
            return False

    # ---- 音源搜索 / 本地曲库扫描 ----

    @Slot(str, result="QVariant")
    def searchSongs(self, keyword: str) -> list:
        """跨远程音源搜歌，返回 [{id,title,artist,album,source,ext_id,cover}]。
        前端搜索框输入 Enter 后调用，结果用于点播。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        try:
            from data.sources import SourceManager
            from config.settings import user_config
            sm = SourceManager(user_config.data)
            out = []
            for s in sm.remote_sources():
                try:
                    tracks = s.search(keyword, limit=8) or []
                    for t in tracks:
                        out.append({
                            "id": getattr(t, "id", None) or f"e:{getattr(s, 'key', '')}:{getattr(t, 'ext_id', '')}",
                            "title": getattr(t, "title", "未知"),
                            "artist": getattr(t, "artist", "未知"),
                            "album": getattr(t, "album", ""),
                            "source": getattr(s, "key", ""),
                            "ext_id": getattr(t, "ext_id", None),
                            "cover": getattr(t, "cover", None),
                        })
                except Exception as e:  # noqa
                    logger.warning("searchSongs %s 失败: %s", getattr(s, "key", "?"), e)
                if out:
                    break   # 首个有结果的音源即返回
            return out
        except Exception as e:  # noqa
            logger.warning("searchSongs 失败: %s", e)
            return []

    @Slot(result=int)
    def scanLibrary(self) -> int:
        """扫描本地音乐目录并入库（曲库=我喜欢的音乐），返回新增/更新数量。"""
        r = self.radio
        try:
            if r is not None and hasattr(r, "library"):
                n = r.library.scan()
            else:
                from data.library import MusicLibrary
                lib = MusicLibrary(self.db)
                n = lib.scan()
            if n:
                self.toast.emit(f"本地曲库扫描完成，新增/更新 {n} 首")
            else:
                self.toast.emit("本地曲库扫描完成（无新增）")
            return int(n or 0)
        except Exception as e:  # noqa
            logger.warning("scanLibrary 失败: %s", e)
            return 0

    # ---- 个性化设置（getSettings / setSetting 读写 user_config，敏感字段不下发） ----
    _SETTING_WHITELIST = (
        # AI 大脑
        ("ai", "backend"), ("ai", "ollama_host"), ("ai", "ollama_deep"),
        ("ai", "ollama_fast"), ("ai", "ollama_fast_enabled"),
        ("ai", "model"), ("ai", "temperature"), ("ai", "skip_temperature"),
        ("ai", "web_search"), ("ai", "thinking"),
        ("ai", "context_window"), ("ai", "web_search_interval"),
        # 提示词
        ("prompts", "taste_output"), ("prompts", "broadcast_output"),
        ("prompts", "transition_output"), ("prompts", "reco_explain"),
        # 语音 TTS（★ 2026-11-18 统一到 Qwen3-TTS；旧引擎/后处理选项已退休）
        ("tts", "qwen3tts_voice"), ("tts", "qwen3tts_postprocess"),
        ("tts", "melotts_pp_hs1"), ("tts", "melotts_pp_hs2"), ("tts", "melotts_pp_hs3"),
        ("tts", "melotts_pp_comp_th"), ("tts", "melotts_pp_comp_ratio"),
        ("tts", "melotts_pp_echo"), ("tts", "melotts_pp_loud_i"),
        # 搜索
        ("search", "mode"), ("search", "online_provider"), ("search", "browser_engine"),
        ("search", "max_results"),
        # 电台 / 推荐偏好 / 城市
        ("roam_strategy",), ("auto_play",),
        ("rec_pref",), ("geo", "city"),
        # 匹配
        ("match", "fuzzy_enabled"), ("match", "fuzzy_threshold"),
        # 音源（非敏感）
        ("sources", "netease", "enabled"), ("sources", "netease", "api_base"),
        ("sources", "netease", "musicbox_uid"),
        ("sources", "qqmusic", "enabled"), ("sources", "qqmusic", "api_base"),
        ("sources", "qqmusic", "default_playlist_id"),
        ("sources", "spotify", "enabled"), ("sources", "spotify", "api_base"),
        ("sources", "spotify", "client_id"), ("sources", "spotify", "default_playlist_id"),
        ("sources", "local", "enabled"), ("sources", "local", "roots"),
        # UI
        ("ui", "skin"), ("ui", "background_fit"), ("ui", "background_mode"),
    )
    # 写只敏感字段：getSettings 不下发明文，仅返回 *_set 标记；setSetting 可写入。
    _SETTING_SENSITIVE = (
        ("ai", "api_base"), ("ai", "api_key"),
        ("sources", "qqmusic", "cookie"),
        ("sources", "spotify", "client_secret"), ("sources", "spotify", "cookie"),
    )

    @Slot(result="QVariant")
    def getSettings(self) -> dict:
        """返回设置白名单（非敏感）+ 敏感字段的 *_set 布尔标记，前端据此渲染。"""
        from config.settings import user_config
        out = {}
        try:
            for path in self._SETTING_WHITELIST:
                node = user_config.data
                ok = True
                for key in path:
                    if isinstance(node, dict) and key in node:
                        node = node[key]
                    else:
                        ok = False
                        break
                if ok:
                    cur = out
                    for key in path[:-1]:
                        cur = cur.setdefault(key, {})
                    cur[path[-1]] = node
            # 敏感字段：只返回是否已设置
            sens = out.setdefault("_sensitive", {})
            for path in self._SETTING_SENSITIVE:
                node = user_config.data
                ok = True
                for key in path:
                    if isinstance(node, dict) and key in node:
                        node = node[key]
                    else:
                        ok = False
                        break
                sens[".".join(path)] = bool(ok and node)
        except Exception as e:  # noqa
            logger.warning("getSettings 失败: %s", e)
        return out

    @Slot(str, str, result=bool)
    def setSetting(self, path: str, value: str) -> bool:
        """按点路径写 user_config 并保存：setSetting('ai.web_search', '1')。
        value 按目标字段类型自动转换（bool/int/float/str/list）。"""
        from config.settings import user_config
        keys = [k for k in path.split(".") if k]
        if not keys:
            return False
        allowed = {".".join(p) for p in self._SETTING_WHITELIST + self._SETTING_SENSITIVE}
        if path not in allowed:
            logger.warning("setSetting 拒绝非白名单字段: %s", path)
            return False
        try:
            node = user_config.data
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            key = keys[-1]
            # 特殊：本地目录列表（换行分隔）
            if path == "sources.local.roots":
                v = [r.strip() for r in str(value).splitlines() if r.strip()]
                node[key] = v
            elif path == "rec_pref":
                v = max(0, min(3, int(float(value))))
                node[key] = v
            else:
                cur = node.get(key)
                if isinstance(cur, bool):
                    v = value in ("1", "true", "True", "on", "yes")
                elif isinstance(cur, int):
                    v = int(float(value))
                elif isinstance(cur, float):
                    v = float(value)
                else:
                    v = str(value)
                node[key] = v
            user_config.save()
            # 运行中生效：关键项实时应用到 radio
            self._apply_setting_live(keys, v)
            self.toast.emit("设置已保存：" + path)
            return True
        except Exception as e:  # noqa
            logger.warning("setSetting %s 失败: %s", path, e)
            return False

    def _apply_setting_live(self, keys, v) -> None:
        """把设置实时应用到运行中的组件（不重启生效）。"""
        r = self.radio
        if not r:
            return
        try:
            if keys[:1] == ["ai"]:
                b = getattr(r, "brain", None)
                if b and hasattr(b, "llm") and b.llm:
                    if keys[-1] == "web_search":
                        b.llm.web_search = bool(v)
                    elif keys[-1] == "thinking":
                        b.llm.thinking = bool(v)
                        b.thinking = bool(v)  # ★ 口播实际用 brain.thinking（radio_bridge 同修）
            elif keys[:1] == ["tts"]:
                # ★ 2026-11-18：切 Qwen3-TTS 音色即时生效（无需重启）
                tts = getattr(r, "tts", None)
                if tts and hasattr(tts, "reconfigure"):
                    tts.reconfigure()
                    logger.info("[设置] TTS 配置 %s 已实时应用", ".".join(keys))
            elif keys[:1] == ["roam_strategy"]:
                b = getattr(r, "brain", None)
                if b:
                    b.roam_strategy = v
            elif keys[:1] == ["rec_pref"]:
                b = getattr(r, "brain", None)
                if b:
                    try:
                        b.familiarity = int(v)
                    except Exception:  # noqa
                        pass
        except Exception:  # noqa
            pass

    # ---- 扫码登录（音源设置） ----

    @Slot(str, result="QVariant")
    def startSourceQrLogin(self, source: str) -> dict:
        """发起扫码登录，返回 {ok, source, key, img(dataURL), err}。
        source: 'netease' | 'qq'。前端拿到 img 后展示二维码并轮询 pollSourceQrLogin。"""
        source = (source or "").lower()
        try:
            if source == "netease":
                from plugins.sources.netease import NeteaseSource
                session = NeteaseSource({"backend": "musicbox"}).start_qr_login() or {}
                unikey = session.get("unikey", "")
                url = session.get("url", "")
                if not unikey:
                    return {"ok": False, "source": source, "err": "获取二维码失败：响应缺少登录标识"}
                self._qr_ncm = {"unikey": unikey}
                return {"ok": True, "source": source, "key": unikey,
                        "img": self._qr_png_data_url(url)}
            if source == "qq":
                from config.settings import user_config
                from plugins.sources.qqmusic import QQMusicSource
                api_base = (user_config.sources.get("qqmusic", {}) or {}).get("api_base", "") \
                    or "http://127.0.0.1:3200"
                payload = QQMusicSource({"api_base": api_base}).start_qr_login() or {}
                qrsig = payload.get("qrsig", "")
                ptqrtoken = payload.get("ptqrtoken", "") or ""
                img = payload.get("img", "")
                if not qrsig:
                    return {"ok": False, "source": source, "err": "获取二维码失败：响应缺少 qrsig"}
                self._qr_qq = {"qrsig": qrsig, "ptqrtoken": ptqrtoken}
                return {"ok": True, "source": source, "key": qrsig,
                        "img": self._normalize_qr_img(img)}
            return {"ok": False, "source": source, "err": "未知音源"}
        except Exception as e:  # noqa
            logger.warning("startSourceQrLogin(%s) 失败: %s", source, e)
            return {"ok": False, "source": source, "err": str(e)}

    @Slot(str, str, result="QVariant")
    def pollSourceQrLogin(self, source: str, key: str) -> dict:
        """轮询扫码登录状态：{status: waiting|scanned|expired|ok|error, msg}。"""
        source = (source or "").lower()
        try:
            if source == "netease":
                from plugins.sources.netease import NeteaseSource
                result = NeteaseSource({"backend": "musicbox"}).check_qr_login(key) or {}
                code = int(result.get("code") or 0)
                if code == 803:
                    self._finalize_ncm_login()
                    return {"status": "ok", "code": code, "msg": "登录成功"}
                if code == 800:
                    return {"status": "expired", "code": code, "msg": "二维码已过期"}
                if code == 802:
                    return {"status": "scanned", "code": code, "msg": "已扫码，请在手机上确认"}
                return {"status": "waiting", "code": code, "msg": "等待扫码"}
            if source == "qq":
                from config.settings import user_config
                from plugins.sources.qqmusic import QQMusicSource
                api_base = (user_config.sources.get("qqmusic", {}) or {}).get("api_base", "") \
                    or "http://127.0.0.1:3200"
                st = getattr(self, "_qr_qq", {})
                result = QQMusicSource({"api_base": api_base}).check_qr_login(
                    key, st.get("ptqrtoken", "")) or {}
                if isinstance(result, dict) and result.get("isOk"):
                    self._finalize_qq_login(result)
                    return {"status": "ok", "code": 0, "msg": "登录成功"}
                refresh = bool(result.get("refresh")) if isinstance(result, dict) else False
                msg = (result.get("message") if isinstance(result, dict) else "") or ""
                if refresh or "失效" in msg:
                    return {"status": "expired", "code": 0, "msg": "二维码已过期"}
                return {"status": "waiting", "code": 0, "msg": "等待扫码"}
            return {"status": "error", "msg": "未知音源"}
        except Exception as e:  # noqa
            logger.warning("pollSourceQrLogin(%s) 失败: %s", source, e)
            return {"status": "error", "msg": str(e)}

    def _finalize_ncm_login(self) -> None:
        from config.settings import user_config
        d = user_config.data
        d.setdefault("sources", {}).setdefault("netease", {}).update(
            {"enabled": True, "backend": "musicbox"})
        try:
            user_config.save()
        except Exception as e:  # noqa
            logger.warning("ncm 登录保存失败: %s", e)

    def _finalize_qq_login(self, result: dict) -> None:
        from config.settings import user_config
        session = result.get("session") or {}
        cookie = session.get("cookie") or ""
        uin = session.get("uin") or session.get("loginUin") or ""
        qq = user_config.data.setdefault("sources", {}).setdefault("qqmusic", {})
        qq["enabled"] = True
        if cookie:
            qq["cookie"] = cookie
        if uin:
            qq["uin"] = uin
        try:
            user_config.save()
        except Exception as e:  # noqa
            logger.warning("qq 登录保存失败: %s", e)
        # ⚠ R85：只落盘不会刷新运行中 QQMusicSource 已建立的会话头 →
        #   「登录成功但仍报音源获取失败」。显式推给实例（见 webapi 同名修复）。
        if cookie:
            try:
                # ⚠ 必须优先取 **radio 持有的那个** SourceManager：_source_manager()
                #   缓存的是本桥自建的副本，与播放时实际调用的实例不是同一个，
                #   注入到副本上等于没注入。
                r = getattr(self, "radio", None)
                sm = getattr(r, "source_manager", None) or \
                    getattr(r, "_source_manager", None) or self._source_manager()
                src = sm.get("qqmusic") if sm is not None else None
                if src is not None:
                    if hasattr(src, "set_cookie"):
                        src.set_cookie(cookie)
                    elif hasattr(src, "refresh_cookie"):
                        src.refresh_cookie()
            except Exception as e:  # noqa
                logger.warning("QQ 凭据注入运行实例失败: %s: %s",
                               type(e).__name__, e)

    _QR_SIZE = 480   # 二维码统一输出尺寸（前端 220px 显示，>2x 采样保持清晰）

    @staticmethod
    def _qr_png_data_url(url: str) -> str:
        """网易云登录 URL → **固定 480×480** 白底 PNG data URL。

        qrcode 库 fit=True 时模块数随 URL 长度变化（21~57 模块），QQ 代理返回的
        二维码图尺寸也参差 —— 直接按原图显示会「忽大忽小」。统一画布后前端
        恒定 220px 平滑缩放，两个音源观感一致。
        """
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").get_image()
        return Bridge._qr_fixed_png(img)

    @staticmethod
    def _qr_fixed_png(img, size: int = 480) -> str:
        """任意二维码 PIL 图 → size×size 白底居中 PNG data URL（NEAREST 保持模块锐利）。"""
        import base64
        import io
        from PIL import Image
        canvas = Image.new("RGB", (size, size), "white")
        qr = img.convert("RGB").resize((size - 16, size - 16), Image.NEAREST)
        canvas.paste(qr, (8, 8))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def _normalize_qr_img(self, img: str) -> str:
        """把 QQ 代理返回的二维码（data URL / http 图）归一到 480×480；失败原样返回。"""
        try:
            import base64
            import io
            from PIL import Image
            if not isinstance(img, str) or not img:
                return img
            if img.startswith("data:"):
                raw = base64.b64decode(img.split(",", 1)[1])
            elif img.startswith("http"):
                from utils.cover_cache import fetch_cover_bytes
                raw = fetch_cover_bytes(img)
            else:
                return img
            return self._qr_fixed_png(Image.open(io.BytesIO(raw)))
        except Exception as e:  # noqa
            logger.warning("二维码归一化失败，使用原图: %s", e)
            return img

    # ---- 测试连接（原始设置面板的「测试 AI / TTS / VOICEVOX」） ----

    @Slot()
    def testAiConnection(self) -> None:
        def run():
            try:
                from ai.brain import AIBrain
                from config.settings import user_config
                backend = user_config.ai.get("backend", "api")
                if backend == "ollama":
                    base = user_config.ai.get("ollama_host", "http://127.0.0.1:11434")
                    key = ""
                    model = user_config.ai.get("ollama_deep", "qwen3.5:9b")
                else:
                    base = user_config.ai.get("api_base", "")
                    key = user_config.ai.get("api_key", "")
                    model = user_config.ai.get("model", "gpt-4o-mini")
                ok, msg = AIBrain().test_connection(base, key, model)
                self.testResult.emit("ai", ("✅ " if ok else "⚠ ") + str(msg))
            except Exception as e:  # noqa
                self.testResult.emit("ai", "测试异常：" + str(e))
        import threading
        threading.Thread(target=run, daemon=True).start()

    @Slot()
    def testTtsConnection(self) -> None:
        def run():
            try:
                from core.tts import TTSEngine
                diag = TTSEngine().diagnose()
                active = diag.get("_active", "?")
                parts = []
                for name in ("edge", "melotts", "chattts", "voicevox"):
                    d = diag.get(name, {})
                    parts.append(f"{name}:{'✅' if d.get('ok') else '⚠️'}{d.get('msg', '')}")
                self.testResult.emit("tts", f"当前引擎={active}。 " + "；".join(parts))
            except Exception as e:  # noqa
                self.testResult.emit("tts", "检测异常：" + str(e))
        import threading
        threading.Thread(target=run, daemon=True).start()

    @Slot()
    def testTtsSample(self) -> None:
        """测试朗读：用当前引擎合成一段示例并播放（等同老 UI 的「测试朗读」）。"""
        def run():
            try:
                from core.tts import TTSEngine
                eng = TTSEngine()
                eng.reconfigure()
                path = eng.synthesize("这里是 MusicAI 智能电台，正在测试语音合成效果。")
                if not path:
                    self.testResult.emit("tts-sample", "⚠️ TTS 不可用（无可用引擎/离线）")
                    return
                r = self.radio
                if r is not None and getattr(r, "player", None) is not None:
                    r.player.play_tts(str(path))
                    self.testResult.emit("tts-sample", "🔊 正在试听…")
                else:
                    self.testResult.emit("tts-sample", "✅ 已生成（未连接播放器）")
            except Exception as e:  # noqa
                self.testResult.emit("tts-sample", "失败：" + str(e))
        import threading
        threading.Thread(target=run, daemon=True).start()

    @Slot()
    def testVoicevox(self) -> None:
        def run():
            try:
                from core.tts import TTSEngine
                ok = TTSEngine().ensure_voicevox(timeout=45)
                self.testResult.emit("voicevox", "✅ VOICEVOX 已连接，日文口播可用。" if ok
                                     else "⚠ 无法连接 VOICEVOX，日文片段将回退 Edge。")
            except Exception as e:  # noqa
                self.testResult.emit("voicevox", "检测异常：" + str(e))
        import threading
        threading.Thread(target=run, daemon=True).start()

    # ---- 歌单收藏 / 本地歌单管理 ----

    @Slot(result="QVariant")
    def listSavedPlaylists(self) -> list:
        pm = self._playlist_manager()
        if not pm:
            return []
        try:
            return [self._meta_to_dict(m) for m in (pm.saved_playlists() or [])]
        except Exception as e:  # noqa
            logger.warning("listSavedPlaylists 失败: %s", e)
            return []

    @Slot(result="QVariant")
    def listLocalPlaylists(self) -> list:
        """本地自建歌单（platform='local'）。"""
        pm = self._playlist_manager()
        if not pm:
            return []
        try:
            return [self._meta_to_dict(m) for m in (pm.local_playlists() or [])]
        except Exception as e:  # noqa
            logger.warning("listLocalPlaylists 失败: %s", e)
            return []

    @Slot(str, str, str, result=bool)
    def savePlaylist(self, platform: str, ext_id: str, name: str) -> bool:
        pm = self._playlist_manager()
        if not pm:
            return False
        try:
            pid = pm.save(platform, ext_id, name or None)
            if pid:
                self.toast.emit("歌单已收藏" + (("：" + name) if name else ""))
                return True
            return False
        except Exception as e:  # noqa
            logger.warning("savePlaylist 失败: %s", e)
            return False

    @Slot(str, str, result=bool)
    def unsavePlaylist(self, platform: str, ext_id: str) -> bool:
        pm = self._playlist_manager()
        if not pm:
            return False
        try:
            pm.unsave(platform, ext_id)
            self.toast.emit("已取消收藏歌单")
            return True
        except Exception as e:  # noqa
            logger.warning("unsavePlaylist 失败: %s", e)
            return False

    @Slot(str, result="QVariant")
    def createLocalPlaylist(self, name: str) -> dict:
        pm = self._playlist_manager()
        if not pm:
            return {}
        try:
            pid = pm.create_local(name or "新建歌单")
            return {"id": pid, "name": name or "新建歌单"} if pid else {}
        except Exception as e:  # noqa
            logger.warning("createLocalPlaylist 失败: %s", e)
            return {}

    @Slot(int, result=bool)
    def deleteLocalPlaylist(self, playlist_id: int) -> bool:
        pm = self._playlist_manager()
        if not pm:
            return False
        try:
            pm.delete_local(playlist_id)
            self.toast.emit("本地歌单已删除")
            return True
        except Exception as e:  # noqa
            logger.warning("deleteLocalPlaylist 失败: %s", e)
            return False

    # ---- 歌词 ----

    def _track_row(self, id=None, source=None, ext_id=None):
        """按 id 或 (source, ext_id) 取 tracks 表行（dict）；查不到返回 None。"""
        try:
            if id is not None:
                row = self.db.conn.execute(
                    "SELECT * FROM tracks WHERE id=? LIMIT 1", (id,)).fetchone()
            else:
                row = self.db.conn.execute(
                    "SELECT * FROM tracks WHERE source=? AND ext_id=? LIMIT 1",
                    (source, ext_id)).fetchone()
            return dict(row) if row else None
        except Exception as e:  # noqa
            logger.warning("tracks 查询失败: %s", e)
            return None

    def _resolve_track(self, track):
        """把前端传来的任意形态解析成可据以取词的 track（dict）；解析不到返回 None。

        收藏曲的典型形态是「只有 db id、没有 source/ext_id」，
        而向音源取词必须有 source+ext_id —— 所以这里要回库补全。"""
        if track is None:
            return None
        if isinstance(track, dict):
            if track.get("source") and track.get("ext_id"):
                return track
            tid = track.get("id")
            if tid is None:
                return track
            # 非数字 id（如搜索结果历史遗留的 's1' 合成 id）不能再 int() 抛穿，
            # 否则 getLyrics 打 WARNING 且取词失败。解析不到就返回 None，前端回退本地合成歌词。
            try:
                return self._track_row(id=int(tid))
            except (ValueError, TypeError):  # noqa
                return None
        if isinstance(track, str):
            s = track.strip()
            if s.startswith("i:"):
                try:
                    return self._track_row(id=int(s[2:]))
                except Exception:  # noqa
                    return None
            if s.startswith("e:"):
                parts = s.split(":", 2)
                if len(parts) >= 3:
                    return self._track_row(source=parts[1], ext_id=parts[2])
                return None
            try:
                return self._track_row(id=int(s))
            except Exception:  # noqa
                return None
        # 对象形态（QWebChannel 偶尔给出带属性的对象）
        src, ext = getattr(track, "source", None), getattr(track, "ext_id", None)
        if src and ext:
            return {"source": src, "ext_id": ext, "id": getattr(track, "id", None)}
        tid = getattr(track, "id", None)
        return self._track_row(id=int(tid)) if tid is not None else None

    @Slot("QVariant", result="QVariant")
    def getLyrics(self, track):
        """获取歌词（返回已解析的 [{time, text}]，无歌词返回 None）。

        入参兼容：None（当前曲）/ dict / 'i:12' / 'e:netease:xxx' / 纯 id。

        ⚠ 这里修掉一个长期失效的 Bug：
          旧实现调用 sm.get_lyrics(t)，而 **get_lyrics 在整个代码库并不存在**
          （真实接口见 ai/playback.py：source_manager.lyric(source, ext_id)），
          于是 hasattr 判断恒为 False，lrc_text 永远是 None、本函数永远返回 None，
          前端只能退回本地合成歌词 —— 表现为「收藏的音乐没有歌词」。
        """
        r = self.radio
        try:
            from core.lyrics import parse_lrc
            t = self._resolve_track(track)
            if t is None and r is not None:
                t = r.current
            if t is None:
                return None
            src = t.get("source") if isinstance(t, dict) else getattr(t, "source", None)
            ext = t.get("ext_id") if isinstance(t, dict) else getattr(t, "ext_id", None)
            lrc_text = ""
            if r is not None and src and src not in ("local", "", None) and ext:
                sm = getattr(r, "source_manager", None) or getattr(r, "_source_manager", None)
                if sm is not None and hasattr(sm, "lyric"):
                    try:
                        # ⚠ 音源返回值形态不统一（str / dict / list），必须归一后再喂
                        # parse_lrc，否则 dict 会抛
                        # "'dict' object has no attribute 'splitlines'"。
                        lrc_text = _coerce_lrc_text(sm.lyric(src, ext)) or ""
                    except Exception as e:  # noqa
                        logger.warning("取歌词失败(%s/%s): %s", src, ext, e)
            if not lrc_text:
                return None
            lines = parse_lrc(lrc_text)
            return [{"time": ts, "text": txt} for ts, txt in lines]
        except Exception as e:  # noqa
            logger.warning("getLyrics 失败: %s", e)
            return None

    # ---- AI 口播 ----

    @Slot(result="QVariant")
    def getAIBroadcast(self):
        """获取当前曲的 AI 口播文案。"""
        r = self.radio
        if not r:
            return None
        try:
            bc = r.current_broadcast
            if bc and isinstance(bc, dict):
                return {"text": bc.get("text", ""), "ready": True}
            return {"text": "", "ready": False}
        except Exception:  # noqa
            return None

    # ---- 收藏（完整模式走 radio.toggle_favorite） ----

    @Slot("QVariant", result=bool)
    def toggleFavoriteTrack(self, track) -> bool:
        """收藏/取消收藏一首歌（完整模式走 radio.toggle_favorite）。

        QWebChannel 会把前端 JS 对象转成 dict，这里统一转成带 id/source/ext_id
        的轻量对象，否则 radio.fav_key 的 getattr 取不到 source/ext_id，
        在线曲会算错收藏键（恒为 i:None）。"""
        r = self.radio
        if not r:
            return False
        try:
            if isinstance(track, dict):
                from types import SimpleNamespace
                track = SimpleNamespace(
                    id=track.get("id"),
                    title=track.get("title") or track.get("name"),
                    artist=track.get("artist"),
                    source=track.get("source"),
                    ext_id=track.get("ext_id"),
                )
            return r.toggle_favorite(track)
        except Exception as e:  # noqa
            logger.warning("toggleFavoriteTrack 失败: %s", e)
            return False


class _ConsoleLogPage(QWebEnginePage):
    """把页面 console 的 warning/error 落日志。

    ⚠ 坑：本版本 PySide6 里 `page.javaScriptConsoleMessage` 是**虚函数**而不是信号，
    `page.javaScriptConsoleMessage.connect(...)` 会抛 AttributeError
    （'builtin_function_or_method' object has no attribute 'connect'）——
    只能子类化 QWebEnginePage 重写它。
    """

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):  # noqa: N802
        # ⚠ Shiboken 枚举**不支持 int()**（TypeError），只能用 .value 取值：
        # Info=0 / Warning=1 / Error=2。写 int(level) 会抛异常被 except 吞掉，
        # 结果是连 console.log 都全量打印。
        try:
            lvl = int(getattr(level, "value", 0))
            warn = int(getattr(QWebEnginePage.JavaScriptConsoleMessageLevel
                               .WarningMessageLevel, "value", 1))
            if lvl < warn:
                return          # 只记 warning 及以上，别被页面的日常 log 淹没
        except Exception:  # noqa
            pass
        logger.warning("页面 console(%s:%s): %s", sourceID or "-", lineNumber, message)


class HostWindow(QMainWindow):
    def __init__(self, radio=None, full_pending: bool = False) -> None:
        super().__init__()
        self._base_title = "MusicAI · 液态玻璃舱（桥接预览）"
        self.setWindowTitle(self._base_title
                            + (" · 完整（AI 电台装配中…）" if full_pending
                               else (" · 完整" if radio else "")))
        self.resize(1280, 800)

        # ═══ R119-28 防闪烁（QWebEngineView on Windows）═══
        # 用户报障：改窗口尺寸 / 被其它窗口遮挡时整窗闪烁。
        # 根因：裸 QWebEngineView 做中央部件时，Qt 的 backing-store 重绘会抢在
        #   Chromium 合成器 present 新帧之前 → 那一帧露出部件默认背景（白）⇒ 闪。
        # 三条标准去闪手段叠加（详见 _patch_flicker.py 头注）：
        #   ① WA_OpaquePaintEvent：禁止 Qt 在 Chromium 出帧前清屏；
        #   ② 中央部件设为深色不透明底（页面主题 --dark #0b0a14），即便有一帧
        #      底色也是深色而非刺眼白闪；
        #   ③ setAutoFillBackground(False)：不再额外擦一次背景。
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("QMainWindow{background:#0b0a14;}")

        self.view = QWebEngineView()
        self.view.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.view.setAutoFillBackground(False)
        self.view.setStyleSheet("background:#0b0a14;")
        # 换成会记日志的 page：真机上前端 JS 报错否则完全静默，
        # 只能靠 offscreen 验证脚本里的 window.__errs 才看得见。
        # ⚠ 必须在 setWebChannel / 注入脚本 / setUrl **之前**换 page。
        try:
            self.view.setPage(_ConsoleLogPage(self.view))
        except Exception as e:  # noqa
            logger.warning("挂载 console 日志 page 失败: %s", e)
        self.bridge = Bridge(radio=radio)

        # ⚠️ 关键：QWebChannel 必须存为实例属性（self.channel），不能是局部变量。
        # page.setWebChannel() 不持有 channel 的所有权；局部变量在 __init__ 返回后被
        # Python 垃圾回收 → C++ QWebChannel 被销毁 → qt.webChannelTransport 永不注入
        # → 前端 new QWebChannel(...) 握手失败（演示 shim 兜底）。这是本轮真正的根因。
        self.channel = QWebChannel()
        self.channel.registerObject("MusicAI", self.bridge)
        self.view.page().setWebChannel(self.channel)

        # 注入 webchannel.js（Qt 自带）：必须用 QWebEngineScript 在 DocumentCreation
        # 阶段注入（早于页面自身 <script>），否则页面里的 `new QWebChannel(...)` 找不到
        # 类 → 握手失败 → 前端退化为演示 shim（无声音/无设置/无歌单）。
        # 注意：不能用 runJavaScript 注入——它跑在 about:blank 上，随 setHtml 被丢弃。
        qwebchannel_js = self._locate_webchannel_js()
        if qwebchannel_js:
            try:
                from PySide6.QtWebEngineCore import QWebEngineScript
                script = QWebEngineScript()
                script.setName("qwebchannel.js")
                script.setSourceCode(qwebchannel_js.read_text(encoding="utf-8"))
                script.setInjectionPoint(QWebEngineScript.DocumentCreation)
                script.setWorldId(QWebEngineScript.MainWorld)
                self.view.page().scripts().insert(script)
                logger.info("已注入 qwebchannel.js（DocumentCreation 阶段）")
            except Exception as e:  # noqa
                logger.warning("注入 qwebchannel.js 失败: %s", e)

        # 用 file:// 直接导航加载（而非 setHtml）——QWebChannel 的 qt.webChannelTransport
        # 注入依赖真实导航，setHtml 会走非导航路径导致 qt 对象缺失 → 握手失败。
        # HTML 是单文件自包含（无外部资源），file:// 完全可用。
        # ⚠ 致命陷阱：QWebEngine 对 file:// 有"页面缓存"层（与 HttpCacheType 管的
        #   HTTP 缓存不是同一层）。实测证明 HttpCacheType.NoCache + URL ?v=<mtime>
        #   query 都不一定能绕开 page cache，导致"改了 HTML 重启仍是旧版本"。
        #   **唯一可靠方案**：每次启动把 HTML 复制到一个全新路径，QWebEngine 看到完全
        #   不同的文件 → 不可能命中任何缓存层。这是开发期强制无缓存的兜底，
        #   配合 NoCacheType 双重保险。
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile
            self.view.page().profile().setHttpCacheType(
                QWebEngineProfile.HttpCacheType.NoCache)
        except Exception as e:  # noqa
            logger.warning("关闭 QWebEngine 缓存失败: %s", e)
        try:
            _tmp_html = os.path.join(
                tempfile.gettempdir(),
                f"musicai_radio_{os.getpid()}_{int(time.time()*1000)}.html")
            shutil.copy2(str(HTML_PATH), _tmp_html)
            _vurl = QUrl.fromLocalFile(_tmp_html)
            logger.info("已复制 HTML 到临时路径防 QWebEngine 缓存: %s", _tmp_html)
        except Exception as e:  # noqa 复制失败退回 mtime query
            logger.warning("复制 HTML 到临时路径失败，fallback 到 mtime query: %s", e)
            _vurl = QUrl.fromLocalFile(str(HTML_PATH))
            try:
                _vurl.setQuery("v=%d" % int(HTML_PATH.stat().st_mtime))
            except Exception:
                pass
        self.view.setUrl(_vurl)
        self.setCentralWidget(self.view)

        # 后端 → 前端 toast
        self.bridge.toast.connect(self._on_toast)

        # 页面加载完成后，校验桥是否连上。
        self.view.loadFinished.connect(self._on_load_finished)

    def mark_radio_ready(self, ok: bool) -> None:
        """电台装配完成（或失败）后刷新标题，让状态在窗口上可见。"""
        try:
            self.setWindowTitle(self._base_title
                                + (" · 完整" if ok else " · 轻量（电台不可用）"))
        except Exception:  # noqa
            pass

    def _locate_webchannel_js(self):
        """定位 Qt 自带的 qwebchannel.js（不同版本路径不同）。"""
        from PySide6 import __version__
        import PySide6
        base = Path(PySide6.__file__).resolve().parent
        candidates = [
            base / "Qt" / "qwebchannel.js",
            base / "resources" / "qwebchannel.js",
            ROOT / "ui" / "qwebchannel.js",
        ]
        for c in candidates:
            if c.exists():
                return c
        # 退路：在 site-packages 全局搜一下
        try:
            import glob
            hits = glob.glob(str(base / "**" / "qwebchannel.js"), recursive=True)
            if hits:
                return Path(hits[0])
        except Exception:  # noqa
            pass
        logger.warning("未找到 qwebchannel.js，请把它放到 ui/ 目录（Qt 自带）。")
        return None

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            logger.warning("页面加载失败（file 协议或 WebEngine 子进程异常）")
            return
        logger.info("页面加载完成，开始 QWebChannel 握手")
        # QWebChannel 握手是异步的（页面脚本在 loadFinished 后才 new QWebChannel），
        # 这里轮询探测 window.MusicAI_CONNECTED，避免误报「未连接」。
        self._probe_attempts = 0
        self._channel_probe_timer = QTimer(self)
        self._channel_probe_timer.timeout.connect(self._channel_probe)
        self._channel_probe_timer.start(500)

    def _channel_probe(self) -> None:
        self._probe_attempts += 1
        # 用 JSON.stringify 返回字符串（PySide6 6.11 对对象返回偶发空值，字符串最可靠）。
        self.view.page().runJavaScript(
            "JSON.stringify({connected: !!window.MusicAI_CONNECTED,"
            " status: window.__channelStatus || 'unknown',"
            " qt: (typeof qt !== 'undefined'),"
            " qwc: (typeof QWebChannel !== 'undefined'),"
            " transport: (typeof qt !== 'undefined' && !!qt.webChannelTransport)});",
            self._on_channel_probe,
        )

    def _on_channel_probe(self, result) -> None:
        connected = False
        diag = str(result) if result is not None else "(空)"
        if isinstance(result, str):
            try:
                import json as _json
                obj = _json.loads(result)
                connected = bool(obj.get("connected"))
                diag = result
            except Exception:  # noqa
                connected = False
        elif isinstance(result, dict):
            connected = bool(result.get("connected"))
        if connected:
            if hasattr(self, "_channel_probe_timer"):
                self._channel_probe_timer.stop()
            logger.info("✅ QWebChannel 握手成功：window.MusicAI 已连接真实后端")
            # 兜底再拉一次后端数据（连接成功时前端已拉，这里确保皮肤/壁纸/设置就绪）
            self.view.page().runJavaScript(
                "(function(){"
                "  if (window.applyWallpaperDescriptor && window.MusicAI && window.MusicAI.getWallpaperDescriptor){"
                "    Promise.resolve(window.MusicAI.getWallpaperDescriptor()).then(function(d){"
                "      if (d && window.applyWallpaperDescriptor) window.applyWallpaperDescriptor(d);"
                "    }).catch(function(){});"
                "  }"
                "  if (typeof loadBackendData === 'function') loadBackendData();"
                "  return true;"
                "})();"
            )
            return
        if self._probe_attempts >= 10:   # ~5s 仍未连接才告警
            if hasattr(self, "_channel_probe_timer"):
                self._channel_probe_timer.stop()
            logger.warning("⚠️ 页面未连上后端（演示 shim 兜底）。诊断：%s", diag)

    def _on_toast(self, msg: str) -> None:
        # 通过 JS 调前端 toast（前端已定义全局 toast()）。
        safe = json.dumps(msg)
        self.view.page().runJavaScript(f"if (window.toast) window.toast({safe});")


def _init_radio():
    """启动完整 AIRadioController 栈（LLM/TTS/player/library/recommend）。

    与 ui/main_window._build_backend 保持同一套装配顺序，返回 (radio, error_msg)；
    radio=None 表示未启动（回退轻量模式）。
    """
    try:
        import threading
        from config.settings import user_config
        from core.player import PlayerEngine
        from core.tts import TTSEngine
        from data.db import Database
        from data.library import MusicLibrary
        from data.sources import SourceManager
        from ai.brain import AIBrain
        from ai.recommend import RoamEngine
        from ai.radio import AIRadioController

        # ★ 2026-09-19：**分段耗时埋点**（先量再优化；下一轮按这些数字决定要不要做结构改造）
        import time as _bt
        _t0 = _bt.perf_counter()

        def _mark(tag):
            nonlocal _t0
            _now = _bt.perf_counter()
            logger.info("[启动耗时] %-18s +%.2fs", tag, _now - _t0)
            _t0 = _now

        db = Database()
        library = MusicLibrary(db)
        player = PlayerEngine()
        _mark("DB+库+播放器")
        tts = TTSEngine()
        _mark("TTSEngine")
        brain = AIBrain()          # ⚠ 含 CLAP 权重加载：真机实测这一块 ~4.9s（最大单块）
        _mark("AIBrain(含CLAP)")
        source_manager = SourceManager(user_config.data)
        recommend = RoamEngine(brain, library.tracks)
        radio = AIRadioController(player, tts, brain, library, db, recommend)
        radio.source_manager = source_manager
        _mark("编排器装配")
        # 启动即清空 LLM/DJ 上下文，从干净状态开始。
        radio.reset_context()
        # 与老 UI ui/main_window._init_library 对齐：打开软件即启用电台漫游/DJ，
        # 并填充「推荐货架」reco_buffer（不自动开播）。否则前端 getRecommendations /
        # getRecoEntries 冷启动都拿不到数据，只能显示演示假歌。
        try:
            radio.roam_on = True
            radio.dj_on = True
            library.reload()
            # R86：修复历史脏数据 —— 搜索点播曾以 path='' 入库（唯一键冲突 →
            # 多首互相覆盖），留下点开播错歌的幽灵行。幂等、不丢数据，失败不影响启动。
            try:
                _fixed = db.repair_remote_paths()
                if _fixed:
                    logger.info("修复在线曲占位 path %d 行（R86）", _fixed)
                    library.reload()
            except Exception as e:  # noqa
                logger.warning("在线曲 path 修复失败（不影响主流程）: %s", e)
            # 补抽联网曲目封面：历史版本导入的 netease/qqmusic 曲目曾以 cover=NULL
            # 落库，收藏/推荐卡片会一直显示程序化占位图。批量补一次（幂等，之后 0 开销）。
            try:
                n = library.backfill_remote_covers(source_manager)
                if n:
                    logger.info("启动补抽联网封面 %d 首", n)
            except Exception as e:  # noqa
                logger.warning("启动补抽联网封面失败（不影响主流程）: %s", e)
            recommend.refresh(library.tracks)
            radio.mark_taste_dirty()
            radio.start_radio(auto_start=False)
            # ★★ 2026-11-18：不再在这里 ensure_reco() —— 此刻 35B 还是冷的，
            #   首次选歌会冷加载 21G ⇒ 推荐慢。改为在 warmup_llm() 预热完成后
            #   立即 ensure_reco()（LLM 刚唤醒就填推荐，最高优先级，见 radio.py）。
            logger.info("电台已启动（推荐将在 LLM 预热后立即填充，未自动开播）")
        except Exception as e:  # noqa
            logger.warning("电台启动填充失败（不影响主流程）: %s", e)
        # ★★ 2026-09-19：**错峰启动**（原来下面这些是同时起跑的）。
        #   真机日志（startup.log）证据：装配 ~13s（其中 CLAP 权重加载 ~4.9s）；
        #   装配一结束 `预加热 Ollama qwen3.5:4b`（日志自称"约需十几秒"）＋ VOICEVOX
        #   ＋ MeloTTS **同时**开始 ⇒ 高峰期叠加 ⇒ 用户"一开始刚打开特别容易卡"。
        #   现在按"对首播的干扰"从轻到重排开，重活让路给首播：
        #     VOICEVOX(独立进程,3s) → 搜索/QQ(子进程,8s) → MeloTTS(载模型,20s) → LLM(GPU,30s)
        #   ⚠ 延迟可用环境变量覆盖（下方 _env_s 的 key），设 0 = 立即（回到旧行为）。
        def _env_s(key, dflt):
            try:
                import os as _os
                return max(0.0, float(_os.environ.get(key, dflt)))
            except Exception:  # noqa
                return float(dflt)

        def _boot_after(delay_s, fn, name, tag):
            def _run():
                try:
                    if delay_s > 0:
                        _bt.sleep(delay_s)
                    logger.info("[启动错峰] %s 起跑（延迟 %.0fs）", tag, delay_s)
                    fn()
                except Exception as _e:  # noqa 预热失败绝不影响启动/播放
                    logger.warning("后台预热 %s 失败: %s", tag, _e)
            threading.Thread(target=_run, daemon=True, name=name).start()

        # ★ 2026-11-17：启动预热改为「只暖当前生效引擎」（tts.warm_active）。
        #   旧行为无条件同时预热 VOICEVOX(3s) + MeloTTS(12s)，即便引擎是 edge/indextts 也照拉。
        _boot_after(_env_s("MUSICAI_BOOT_TTS_S", 6.0),
                    tts.warm_active, "tts-boot", "TTS 引擎")
        # ★ 2026-09-19：用户明确"13 秒不能播可以接受，**聊更重要**" ⇒ 聊天链的预热往回收。
        _boot_after(_env_s("MUSICAI_BOOT_LLM_S", 10.0),
                    radio.warmup_llm, "llm-boot", "LLM 预加热")

        # 联网搜索 / QQ音乐 API（独立子进程，轻）：稍错开即可（失败仅告警）。
        def _boot_aux():
            try:
                from tools.websearch_service import launcher as websearch_launcher
                websearch_launcher.autostart_if_browser()
            except Exception:  # noqa
                pass
            try:
                from tools import qq_api_launcher
                qq_api_launcher.autostart()
            except Exception:  # noqa
                pass
        _boot_after(_env_s("MUSICAI_BOOT_AUX_S", 8.0), _boot_aux, "aux-boot", "搜索/QQ API")
        _mark("radio 就绪（可播/可聊）")
        logger.info("AIRadioController 已启动（完整模式）")
        return radio, None
    except Exception as e:  # noqa
        logger.warning("完整模式启动失败（回退轻量模式）: %s", e)
        return None, str(e)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="启动完整 AI 电台栈（LLM/TTS/播放）")
    args = ap.parse_args()

    # GPU 显存额度（与 main.py 一致，必须在首次 CUDA 分配前调用，给 MeloTTS 留余量）。
    try:
        from core.gpu_budget import apply_gpu_budget
        apply_gpu_budget()
    except Exception:  # noqa
        pass

    # 让 WebEngine 渲染子进程在无显示/沙箱环境下也能启动（CI / 远程桌面常见）。
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    try:
        from PySide6.QtWebEngineCore import QWebEngineSettings
        QWebEngineSettings.defaultSettings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        # 允许 file:// 页面加载 file:// 封面（本地曲）与 http(s) 封面（在线曲），
        # 否则歌曲封面/歌单封面在 file:// 页面下会被 WebEngine 拦截，显示空白。
        QWebEngineSettings.defaultSettings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        QWebEngineSettings.defaultSettings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    except Exception:  # noqa
        pass
    # 软件渲染仅在有显式要求时启用（headless/CI/远程桌面）；真实桌面走原生 GPU，
    # 避免 --use-gl=swiftshader 在独立显卡上强制软渲（掉帧甚至 show() 卡死）。
    if os.environ.get("MUSICAI_SOFT_RENDER"):
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            "--no-sandbox --disable-gpu --use-gl=swiftshader "
            "--enable-unsafe-swiftshader --disable-dev-shm-usage")

    app = QApplication(sys.argv)
    try:
        from PySide6.QtGui import QFont
        app.setFont(QFont("Microsoft YaHei", 10))
    except Exception:  # noqa
        pass
    # ⚠ 先开窗、后装配：_init_radio() 串行加载 CLAP/LLM 权重约 70s，
    # 放在窗口创建之前会让用户干等一分多钟才看到界面。现在界面秒开，
    # 电台在后台线程装配，就绪后 attach_radio() 挂上（前端自动接管）。
    win = HostWindow(radio=None, full_pending=bool(args.full))
    win.show()
    logger.info("界面已显示（%s）",
                "AI 电台后台装配中" if args.full else "轻量模式")
    if args.full:
        _start_radio_async(win)
    sys.exit(app.exec())


def _start_radio_async(win) -> None:
    """后台线程装配 AIRadioController，就绪后在 GUI 线程挂到 bridge。

    ⚠ 用 QTimer 轮询结果而不是直接回调：装配在工作线程，QObject/Signal 只能在
    GUI 线程碰，QTimer 天然跑在 GUI 线程，省一层跨线程调用。
    """
    state = {"done": False, "radio": None, "err": None}

    def _run():
        try:
            radio, err = _init_radio()
            state["radio"], state["err"] = radio, err
        except Exception as e:  # noqa
            state["err"] = f"{type(e).__name__}: {e}"
        finally:
            state["done"] = True

    threading.Thread(target=_run, daemon=True, name="radio-init").start()

    def _poll():
        if not state["done"]:
            QTimer.singleShot(300, _poll)
            return
        radio, err = state["radio"], state["err"]
        if radio is None:
            logger.warning("完整模式装配失败，回退轻量模式：%s", err)
            try:
                win.bridge.toast.emit("AI 电台加载失败，已回退轻量模式")
            except Exception:  # noqa
                pass
            win.mark_radio_ready(False)
            return
        try:
            ok = win.bridge.attach_radio(radio)
            win.mark_radio_ready(bool(ok))
            if not ok:
                logger.warning("挂载电台失败（bridge 已有 radio？）")
        except Exception as e:  # noqa
            logger.warning("挂载电台异常: %s", e)
            win.mark_radio_ready(False)

    QTimer.singleShot(300, _poll)


if __name__ == "__main__":
    main()
