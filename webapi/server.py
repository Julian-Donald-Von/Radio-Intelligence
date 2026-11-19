"""MusicAI · FastAPI 服务端（Phase 1 骨架）

前端不再是「QWebChannel + file:// 单文件 HTML」，改为标准 HTTP + WebSocket：
  - 业务逻辑（ai/radio.py / schedule.py / playback.py）零改动，由 bridge 层接入
  - 前端可以用真浏览器打开 http://127.0.0.1:8765 调试（F12 可见 Network/WS）

Phase 1 只做：服务起来 + /api/status + /ws 连通 + 静态站点挂载。
后续 Phase 在此文件按模块追加路由。
"""
from __future__ import annotations

# ★ 2026-11-16：直接 `python -m webapi.server` 启动时也清环境代理（同 start_api_host）
try:
    from utils.no_env_proxy import strip_env_proxy as _strip_proxy
    _strip_proxy()
except Exception:  # noqa
    pass

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from webapi.bus import bus
from webapi.radio_bridge import bridge, marshaller

logger = logging.getLogger("MusicAI.WebAPI")

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"

# 启动时间（用于 /api/status 的 uptime）
_BOOT_TS = time.time()


async def _to_thread(fn, *args, **kwargs):
    """把**同步阻塞**函数丢到线程池执行，避免卡住 asyncio 事件循环。

    ⚠ 歌单拉取要走音源网络（可能数秒），而 def（非 async def）路由虽然由
      FastAPI 自动放到线程池，但这里是 async def —— 直接调用会**阻塞整个
      事件循环**（连 /ws 心跳都停）。必须显式 to_thread。

    ★ R119-5a · **本文件的不变式**：凡 `async def` 路由里调用 bridge 的方法，
      一律写成 `await _to_thread(对应方法, 参数...)`，**绝不直接调用**。
      ⚠ 注释里也别写形如 `bridge.<名字>` 的示例：round44 的 E65 会把它当成
      "真引用了某个 bridge 方法"去做存在性核对（我第一版就这么假红了一次），
      示例请写成不带前缀的形式。
      为什么：bridge 的方法大多经 `QtMarshaller.call()` 回 Qt 主线程取数，
      Qt 一卡就阻塞最长 10s；`async def` 里同步调用会**阻塞整个事件循环**
      （连 /ws 心跳与静态文件都停）⇒ 前端整体假死。
      事故（2026-09-21 00:05）：拖进度条 → `/api/playback/seek` 与
      `/api/playback/state` 双双阻塞事件循环，随后 python313.dll 访问违例闪退。
      自检：`_scan_blocking_routes.py`（恒应 0 处）/ `verify_r119_http_no_block.py` 判据 ①②。
    """
    import asyncio
    import functools

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _detect_ws_support() -> bool:
    """检测 uvicorn 是否装了 WebSocket 支持库（websockets / wsproto）。

    ⚠ 坑：uvicorn 默认**不带** WS 支持。缺库时 /ws 会返回 404 且只在日志里打一行
      WARNING，前端表现为"WebSocket 连接已断开"，很难定位。
      显式检测后暴露在 /api/status 里，前端可直接显示"WS 不受支持"。
    """
    for mod in ("websockets", "wsproto"):
        try:
            __import__(mod)
            return True
        except ImportError:
            continue
    return False


_WS_SUPPORTED = _detect_ws_support()


# ---------------- 生命周期（uvicorn 启停） ----------------
# ⚠ FastAPI 已弃用 @app.on_event("startup"/"shutdown")（DeprecationWarning），
#   统一改用 lifespan 上下文管理器（FastAPI ≥0.93 推荐，语义完全等价）。
#   注意 lifespan 必须在 FastAPI(...) 构造时传入，故定义放在 app 之前。

@asynccontextmanager
async def _lifespan(app_: FastAPI):
    loop = asyncio.get_event_loop()
    # ⚠ 抑制 Windows ProactorEventLoop 的噪音：
    #   客户端（浏览器刷新/关闭）断开时，_call_connection_lost 会对已关闭的
    #   socket 调 shutdown() → 抛 ConnectionResetError，日志里刷一大段 traceback，
    #   但功能完全正常。这里把这类"连接已断开"的异常静音，其余照常上报。
    def _exc_handler(loop_, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            logger.debug("客户端连接已断开（已静音）: %s", exc)
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(_exc_handler)
    bus.attach_loop(loop)
    _bus_task = asyncio.create_task(bus.run())
    # ⚠ 不要在这里 marshaller.start()！
    #   本 lifespan 跑在 **uvicorn 线程**，在那里创建的 QTimer 属于该线程，
    #   而该线程没有 Qt 事件循环 → timeout 永不触发 → 所有播放调用超时。
    #   正确的启动位置是 start_api_host.py 的 Qt 主线程（QApplication 之后）。
    logger.info("Radio Intelligence Web API 已启动（Phase 3 · 播放控制 + 队列/推荐）")
    try:
        yield
    finally:
        # 优雅收尾：停掉事件广播后台任务，避免 uvicorn 退出时悬挂/告警
        _bus_task.cancel()
        try:
            await _bus_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Radio Intelligence Web API",
    description="液态玻璃舱前端的后端接口（替代 QWebChannel）",
    version="0.1.0-phase1",
    lifespan=_lifespan,
)

# 本地开发：允许跨域（前端可能用不同端口/真浏览器打开）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ⚠ 开发期禁用 HTML 缓存
# 现象：改了 index.html 后浏览器仍返回 "304 Not Modified"，加载的是旧页面，
#       表现为"改了前端没变化"—— 与之前 QWebEngine 缓存 file:// 是同一类坑。
# 做法：所有 HTML 响应强制 no-store，保证每次都取最新。
# ⚠⚠ R67 补：光禁 HTML 不够 —— ui-*.js / poster.js / css 这些子资源不带
# Cache-Control，Chromium 会用「启发式新鲜期」（约 (now-Last-Modified)×10%），
# 于是 index.html 是新的、JS 还是旧的 → 表现为"改了没变化"，且 offscreen
# 验证脚本会去测一份**根本不存在于磁盘**的旧逻辑（假绿/假红，极难定位）。
# 现在：非 /api 的静态响应一律 no-store（/api/cover 自己带的长缓存头要保留）。
@app.middleware("http")
async def _no_cache_html(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---------------- 健康检查 ----------------

@app.get("/api/status")
async def status():
    """健康检查：前端连上就调这个，用于确认后端活着。

    ⚠ 返回的字段要与前端约定好，缺字段会显示成 "undefined"
       （Phase 1 实测：前端读 d.version 而这里没返回 → 显示 "vundefined"）。
    """
    # R0：语义层就绪态必须可观测 —— 此前 anchors 构建失败只写一行 warning，
    # 真机表现为「水面一直平均波纹」却无从定位（`track_segments=0` 也无人发现）。
    sem = None
    try:
        from core.semantic_axis import anchors, quantiles_status
        from core.short_term_semantics import l1_enabled, MAP_PATH
        sem = {
            "anchors": anchors.state(),        # {status, last_error}
            "quantiles": quantiles_status(),   # ready | fallback
            "l1_on": l1_enabled(),
            "l1_mapped": MAP_PATH.exists(),    # yamnet_map.npz 是否已拟合
        }
    except Exception as e:  # noqa 诊断字段绝不拖垮健康检查
        sem = {"error": f"{type(e).__name__}: {e}"}
    # R0：控制器事件桥的线程亲和性 —— "gui"=已正确重建（后台 _emit 可投递）；
    # "stale"=仍绑在已退出的 radio-init 装配线程（事件静默丢失 → track_changed/
    # progress 全无 → mood 线程拿不到当前曲 → 语义层整体失效）；"none"=无 Qt。
    # 把「静默失效」变成一眼可查的状态，避免同类问题再潜伏数周。
    qt_bridge = "none"
    try:
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance()
        _r = bridge.controller
        _qm = getattr(_r, "_qt_main", None) if _r is not None else None
        if _qm is not None:
            qt_bridge = "gui" if (_app is not None
                                  and _qm.thread() == _app.thread()) else "stale"
    except Exception:  # noqa
        pass
    # R88：音频链路健康度 —— PortAudio 状态位（bit1=output underflow）+ 音乐队列水位。
    # 此前 `_pa_flags` 埋点**只打 console**，而 logging.basicConfig 没有 FileHandler，
    # 用户报「播放断续/重复」时只能肉眼看启动窗口、无从取证。现在一眼可查：
    # output_underflow=True 即坐实 RT 回调（23ms 预算）被抢 —— 典型是语义索引
    # 跑满 CPU / 4GB 堆上的全代 GC 长停顿。
    audio = None
    try:
        audio = await _to_thread(bridge.audio_diag)
    except Exception as e:  # noqa 诊断字段绝不拖垮健康检查
        audio = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # ★ R119-3f：Ollama runner 内存回收的可观测面（回收过几次 / 为什么跳过）。
    #   ⚠⚠ 教训：**`/api/status` 的载荷与 `radio_bridge.get_state()` 是两份副本**
    #   （同一事实两个出口）—— 新增字段必须**两边都加**。我第一版只加了后者，
    #   真机上 `/api/status` 里根本读不到 `runner`（而我的守卫只查了 radio_bridge，
    #   照样给绿灯）⇒ 守卫已同步改成**两个出口都查**。
    runner = None
    try:
        runner = await _to_thread(bridge.runner_recycle_state)
    except Exception as e:  # noqa 诊断字段绝不拖垮健康检查
        runner = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # ★ R119-4v：口播质量 —— "正确的 AI 导听口播" vs "退化兜底句"各播了几条。
    #   用户要的不是"别有兜底"而是"**正确的口播别缺席**"，故把退化率做成可读出的一等字段。
    dj = None
    try:
        dj = await _to_thread(bridge.dj_speech_state)
    except Exception as e:  # noqa 诊断字段绝不拖垮健康检查
        dj = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "service": "Radio Intelligence Web API",
        "version": app.version,        # ← 前端要读，别漏
        "phase": 1,
        "uptime_s": round(time.time() - _BOOT_TS, 1),
        "ws_clients": bus.client_count,
        # Phase 2：controller 是否已接入（未接入时播放接口会返回 ready=False）
        "radio_ready": bridge.ready,
        "ws_supported": _WS_SUPPORTED,  # uvicorn 是否装了 WebSocket 支持库
        "sem": sem,                     # R0：语义层就绪态（排障入口）
        "qt_bridge": qt_bridge,         # R0：事件桥线程（gui | stale | none）
        "audio": audio,                 # R88：PortAudio underflow + 队列水位
        "runner": runner,               # R119-3f：Ollama runner 回收（n / skipped / 秒数）
        "dj": dj,                       # R119-4v：口播质量（intro_ai / intro_fallback / 比率）
    }


@app.get("/api/ping")
async def ping():
    return {"pong": True}


# ---------------- 本地插件扫描（第一版：本地文件夹，预留 GitHub 市场） ----------------
# 用户 2026-11-19：插件下载后放进 plugins/art-effects/<id>/，点扫描即发现。
# 只扫 manifest + 核对入口文件，不执行任何插件代码；前端再走 ArtHost 门禁两段式加载。
# source=github 为市场预留位（见 webapi/plugin_local.py），本版不实现。
@app.get("/api/plugins/local")
async def api_plugins_local():
    from webapi.plugin_local import scan as scan_local_plugins
    return scan_local_plugins()


@app.get("/api/plugins/local/source")
async def api_plugins_local_source(dir: str = ""):
    """读本地插件入口源码给前端门禁+注入。只在扫描清单内的目录放行，防目录穿越。"""
    from webapi.plugin_local import scan as scan_local_plugins, PLUGIN_ROOT
    listing = scan_local_plugins()
    entry = next((p for p in listing["plugins"] if p["dir"] == dir), None)
    if not entry:
        return JSONResponse({"ok": False, "reason": "E_NOT_REGISTERED", "dir": dir}, status_code=404)
    path = (PLUGIN_ROOT / entry["dir"] / entry["entry"]).resolve()
    if not str(path).startswith(str(PLUGIN_ROOT.resolve())):
        return JSONResponse({"ok": False, "reason": "E_TRAVERSAL"}, status_code=400)
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": "E_READ", "detail": str(e)}, status_code=500)
    return {"ok": True, "id": entry["id"], "manifest": entry["manifest"], "source": src}


# ---- 统一插件清单（美术 + 音源），供设置页 LAB 栏插件管理用 ----
@app.get("/api/plugins/all")
async def api_plugins_all():
    from webapi.plugin_local import scan_all
    return scan_all()


# ---- 音源插件启用/停用：写 config + 热重载 REGISTRY，启用后进 /api/search/sources ----
@app.post("/api/plugins/source/enable")
async def api_plugin_source_enable(payload: dict):
    key = str((payload or {}).get("key", "")).strip()
    enabled = (payload or {}).get("enabled", True)
    if not key:
        return JSONResponse({"ok": False, "reason": "E_NO_KEY"}, status_code=400)
    try:
        from webapi.plugin_local import scan_sources
        from data.sources import reload_source_registry
        from config.settings import user_config
        # 只放行静态扫描清单里的第三方音源 key（内置 key 不让插件接口改）
        found = any(p["id"] == key for p in scan_sources()["plugins"])
        if not found:
            return JSONResponse({"ok": False, "reason": "E_NOT_REGISTERED", "key": key}, status_code=404)
        src_cfg = user_config.data.setdefault("sources", {})
        node = src_cfg.setdefault(key, {})
        node["enabled"] = bool(enabled)
        user_config.save()
        reg = reload_source_registry()   # 热重载：新 key 进入 REGISTRY
        return {"ok": True, "key": key, "enabled": bool(enabled),
                "registered": key in reg, "registry_keys": sorted(reg.keys())}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "reason": "E_ENABLE", "detail": str(e)}, status_code=500)


@app.get("/favicon.ico")
async def favicon():
    """静音浏览器的 favicon 404 噪音（日志里每开一次页面刷一条）。"""
    from fastapi.responses import Response

    return Response(status_code=204)


# ---------------- Phase 4：不喜欢（dislike）+ 历史 ----------------
# ⚠ 「曲库/library」**不是**废弃功能 —— 它是 orbit 上 fav 那一档，
#   数据源 = radio.get_library()（本地导入的全部音乐），R45 用户点名纠正过两次。
#   （上一版注释把两者混为一谈，误导后续编辑，已更正。）
#   不喜欢是独立能力：AI 避雷 + 从待播/推荐货架剔除。
# R49：「喜欢/红心」按用户要求**恢复**（老 UI dock #d-fav + 卡片 ♥ + 礼花）——
#   当前曲走 /api/playback/favorite，指定曲目走 /api/favorite/track（卡片用）。
#   「喜欢」与「不喜欢」并存但语义相反：喜欢=收藏到曲库，不喜欢=AI 避雷。


@app.get("/api/history")
async def history():
    """播放历史。"""
    return await _to_thread(bridge.get_history)


@app.delete("/api/history/{track_id}")
async def history_remove(track_id: int):
    """从播放历史移除。"""
    return await _to_thread(bridge.remove_history, track_id)


@app.get("/api/library")
async def library():
    """曲库（本地音乐库，非收藏）。"""
    return await _to_thread(bridge.get_library)


@app.post("/api/library/delete")
async def library_delete(id: str = ""):
    """从曲库删除一首（侧边面板曲库行的 del 按钮）。

    ⚠ 前端一直请求这个路径，但路由**从未注册** → 404 被 `.catch(()=>{})` 吞掉，
      表现为"点了删除没反应"。R46-2 补上。
    """
    return await _to_thread(bridge.delete_from_library, id)


@app.post("/api/library/import/folder")
async def library_import_folder(folder: str = ""):
    """R62 #9：导入本地文件夹到曲库（设置卡「导入文件夹」）。

    与老 UI import-folder（PySide6 文件对话框 → add_local_folder）等价：
    本机部署时后端与 UI 同机，用户填目录绝对路径 → 后端扫描入库。
    """
    return await _to_thread(bridge.import_library_folder, folder or "")


@app.post("/api/library/scan")
async def library_scan():
    """R62 #9：扫描配置的全部曲库根目录（老 UI scan-lib 等价）。"""
    return await _to_thread(bridge.scan_library_roots)


@app.get("/api/dislikes")
async def dislikes():
    """不喜欢列表 [{key,title,artist}]。"""
    return await _to_thread(bridge.list_dislikes)


# ---------------- 歌单（plist 栏目 · QQ音乐/网易云） ----------------
# ⚠ 与「曲库 /api/library」是两个东西（R45 用户纠正，别再接混）：
#     · 歌单 = 在线歌单（QQ音乐 / 网易云），数据源 PlaylistManager，带音源切换
#     · 曲库 = 本地音乐库，数据源 radio.get_library()
# 网络在 FastAPI 线程池里做（**不**经 marshaller 编排到 Qt 主线程，否则冻窗）。

@app.get("/api/tts/refs")
def tts_refs():
    """列出可选音色（IndexTTS 的克隆源）：内置参考音 + voices/indextts/*.wav。

    克隆方式：`python tools/make_voice_ref.py --speaker <id> --name <名字>`
    （从 VOICEVOX 取声线），生成后这里自动出现，设置页下拉即可选。
    """
    items = [{"label": "默认：四国めたん（内置参考音）", "value": ""}]
    try:
        from config.settings import PROJECT_ROOT
        d = PROJECT_ROOT / "voices" / "indextts"
        if d.is_dir():
            for p in sorted(d.glob("*.wav")):
                items.append({"label": p.stem, "value": str(p)})
    except Exception as e:  # noqa
        logger.warning("/api/tts/refs 扫描失败: %s", e)
    return {"ok": True, "items": items}


@app.get("/api/playlists")
async def playlists(group: str = "discover", offset: int = 0,
                    limit: int = 30, source: str = ""):
    """歌单一页。

    group ∈ discover|mine|saved|local；source ∈ ''（全部）|netease|qqmusic。
    多音源并行拉取（串行会耗时累加 = 「歌单疯狂加载」）。
    """
    return await _to_thread(bridge.get_playlists, group, offset, limit, source)


@app.get("/api/playlists/tracks")
async def playlist_tracks(platform: str = "", ext_id: str = ""):
    """歌单详情曲目。"""
    return await _to_thread(bridge.get_playlist_tracks, platform, ext_id)


@app.post("/api/playlists/play")
async def playlist_play(platform: str = "", ext_id: str = "", index: int = 0):
    """整单入队播放；index>0 时跳到该首（后端内部修正为 jump_to(index-1)）。

    ⚠ 在线歌单曲目**没有本地 db id**，不能用 /api/play/track/{id}（要求 int）。
    """
    return await _to_thread(bridge.play_playlist, platform, ext_id, index)


@app.post("/api/dislike/add")
async def dislike_add(key: str = "", title: str = "", artist: str = ""):
    """加入不喜欢：AI 避雷 + 从待播/推荐货架剔除。
    key 形如 'i:123'（本地库 id）或 'e:qq:xxx'（在线 source:ext_id）。

    ⚠ R62 #23：在线歌（e:…）没有本地 db id，radio.dislike_by_key 落库时
    拿不到 Track 元数据 → 之前 title/artist 传 None → 不喜欢库显示
    「未知歌手/未知歌曲」。前端（卡片/侧边面板）点击时手上有完整条目，
    必须把 title/artist 一起 POST 过来，落库才有真实歌名。"""
    return await _to_thread(bridge.add_dislike, key, title or None, artist or None)


@app.post("/api/dislike/remove")
async def dislike_remove(key: str = ""):
    """移出不喜欢。"""
    return await _to_thread(bridge.remove_dislike, key)


# ---------------- AI 电台 / 口播（dock 上的 #d-radio / #d-ai） ----------------

@app.post("/api/radio/toggle")
async def radio_toggle(on: int = 1):
    """电台模式：开 = 立即补满队列并续推。"""
    return await _to_thread(bridge.toggle_radio, bool(on))


@app.post("/api/dj/toggle")
async def dj_toggle(on: int = 1):
    """AI 口播：开 = 立即预取下一首口播。"""
    return await _to_thread(bridge.toggle_dj, bool(on))


@app.post("/api/broadcast/language")
async def broadcast_language(lang: str = "zh"):
    """切换 AI 口播 / TTS 语言（zh/en）。

    由前端 i18n 开关调用；下一首及待播预取生效，当前已生成的当前口播不打断。
    """
    return await _to_thread(bridge.set_broadcast_language, lang)


@app.get("/api/dj/broadcast")
async def dj_broadcast():
    """获取当前曲的 AI 口播文案。"""
    return await _to_thread(bridge.get_broadcast)


@app.get("/api/beats")
async def beats_snapshot():
    """R84a：当前曲的 beat/downbeat+旋律特征载荷（WS 晚进页/断线重连补取）。

    WS 'beats' 事件每曲一发；此端点回 bridge 缓存的最近一份，未就绪回 null。
    """
    return await _to_thread(bridge.get_beats)


# ---------------- Phase 3：队列 + 推荐货架 ----------------

@app.get("/api/queue")
async def queue():
    """播放队列（当前曲 + 待播）。"""
    return await _to_thread(bridge.get_queue)


@app.get("/api/recommendations")
async def recommendations():
    """漫游列表：当前曲 + 待播；冷启动时回退推荐货架。"""
    return await _to_thread(bridge.get_recommendations)


@app.get("/api/reco")
async def reco_entries():
    """推荐货架（与播放队列复制共存）。"""
    return await _to_thread(bridge.get_reco_entries)


@app.post("/api/reco/play")
async def reco_play(track_id: int = 0):
    """点推荐里的某一首 → **按身份**起播（不用下标，避免"点 A 播 B"）。"""
    return await _to_thread(bridge.play_reco, track_id)


@app.post("/api/reco/remove")
async def reco_remove(track_id: int = 0):
    """从推荐货架移除该曲；R55：同时从播放队列/当前曲同步清除（点叉=不播这首）。"""
    return await _to_thread(bridge.remove_reco, track_id)


@app.post("/api/reco/play-all")
async def reco_play_all():
    """推荐「播放全部」：整批推荐**替换**播放队列并从第一首开播。"""
    return await _to_thread(bridge.play_all_reco)


@app.post("/api/library/play-all")
async def library_play_all():
    """曲库「播放全部」：整批曲库**替换**播放队列并从第一首开播。

    ⚠ 与 /api/reco/play-all 是两个数据源（library vs reco_buffer），别混用
      （R46-5：此前「播放全部」按钮在曲库栏目下也打到了 reco，等于点了曲库播推荐）。
    """
    return await _to_thread(bridge.play_all_library)


@app.post("/api/queue/remove")
async def queue_remove(track_id: int = 0):
    """从播放队列移除。"""
    return await _to_thread(bridge.remove_from_queue, track_id)


@app.get("/api/lyrics")
async def lyrics(track_id: str = ""):
    """歌词（已解析 [{time, text}]）。track_id 留空 = 当前曲。

    ⚠ 走 _to_thread：取词要回源（数百 ms～数秒），不能在事件循环里做。
    """
    return await _to_thread(bridge.get_lyrics, track_id or None)


@app.post("/api/queue/play-next")
async def queue_play_next(track_id: int = 0):
    """插到待播首位（「下一首播放」）。

    ⚠ 与 /api/playback/next 是完全不同的语义：后者是**跳过当前曲**，
      前者只插入队列不动当前播放。
    """
    return await _to_thread(bridge.queue_play_next, track_id)


@app.post("/api/play/track/{track_id}")
async def play_track(track_id: int):
    """按数据库 id 起播一首。"""
    return await _to_thread(bridge.play_track_by_id, track_id)
# ⚠ 全部经 bridge 走 Qt 主线程编排，路由本身只做参数校验与 JSON 包装。

@app.post("/api/window/action")
async def api_window_action(request: Request):
    """无边框窗口控制（2026-11-18）：minimize / maximize / close / move / resize。
    页面自绘标题栏调用；动作排队到 Qt 主线程执行，本接口立即返回。"""
    try:
        body = await request.json()
    except Exception:  # noqa
        body = {}
    from webapi import window_ctl
    return window_ctl.dispatch((body or {}).get("action"), int((body or {}).get("edges") or 0))


@app.get("/api/window/state")
async def api_window_state():
    """窗口状态（是否最大化）——自绘标题栏按钮图标用。"""
    from webapi import window_ctl
    return window_ctl.dispatch("state")


@app.get("/api/semantics/now")
async def api_semantics_now():
    """语义链路自检（2026-11-18）：返回 mood 线程当前停在哪道门 + 最近一次推送的轴。
    纯读 bridge 缓存字段，不碰 Qt、不加载模型 ⇒ 可高频轮询。"""
    return await _to_thread(bridge.mood_diag_snapshot)


@app.get("/api/playback/state")
async def playback_state():
    """播放状态快照（playing / position / duration / 当前曲）。"""
    return await _to_thread(bridge.get_state)


@app.post("/api/playback/toggle")
async def playback_toggle(force_on: bool = False):
    """播放 / 暂停。force_on=true 表示"确保进入播放态"（冷启动会自动拉起电台）。"""
    return await _to_thread(bridge.toggle_play, force_on=force_on)


@app.post("/api/playback/next")
async def playback_next():
    return await _to_thread(bridge.next_track)


@app.post("/api/playback/prev")
async def playback_prev():
    return await _to_thread(bridge.prev_track)


@app.post("/api/playback/seek")
async def playback_seek(ms: int = 0):
    return await _to_thread(bridge.seek, ms)


@app.post("/api/playback/mode")
async def playback_mode(mode: str = "order"):
    """播放模式：order（顺序）/ loop_one（单曲循环）/ random（随机）。

    ⚠ 电台约定：random = 先打乱再进播放列表（保证"列表顺序=播放顺序"，
    AI 口播才不会与歌曲对不上），不是播放时随机挑一首。
    """
    return await _to_thread(bridge.set_play_mode, mode)


@app.post("/api/playback/favorite")
async def playback_favorite(on: int = 1):
    """收藏 / 取消收藏当前曲（on=1 收藏，on=0 取消）。"""
    return await _to_thread(bridge.set_favorite, bool(on))


@app.post("/api/favorite/track")
async def favorite_track(track_id: int | None = None, source: str = "",
                         ext_id: str = "", title: str = "",
                         artist: str = "", album: str = "",
                         cover: str = ""):
    """喜欢 / 取消喜欢**指定**曲目（卡片 ♥ 按钮，R49）。

    ⚠ 在线曲必须带 source+ext_id（稳定键 e:{src}:{ext}），只给 id 会配错键；
      本地曲带 track_id 即可（i:{db_id}）。两者都给时后端 fav_key 自动取舍。

    ⚠ R72：**首次点红心的未播在线曲**要能真正落进「曲库」，必须带上完整元数据
      title/artist/album/cover —— 卡片 ♥ 是把一首还没在库里的歌**收进曲库**的动作，
      后端要拿这些字段 + source/ext_id 拼出稳定占位 path（netease:// / qq://）入库，
      否则 upsert 读 track.path 直接 AttributeError（此前被宽 except 吞掉 =
      红心点亮但永不出现 = 用户报「点了红心不进曲库」）。"""
    return await _to_thread(bridge.toggle_favorite_track, track_id,
                            source or "", ext_id or "", title or "",
                            artist or "", album or "", cover or "")


@app.post("/api/playback/volume")
async def playback_volume(vol: float = 1.0):
    """音量 0.0 ~ 1.0"""
    return await _to_thread(bridge.set_volume, vol)


# ═══════════ EQ（R68：手动 / 预设 / 动态）═══════════
# GET 返回当前 EQ 状态（含持久化 user_config + 引擎 applied 标记），
# 前端启动 / 打开面板时拉取；POST 任意子集更新并热应用。

@app.get("/api/eq")
async def eq_get():
    """当前 EQ 状态：{enabled, source, preset, bands[10], dyn, applied}。"""
    return await _to_thread(bridge.get_eq_state)


@app.post("/api/eq")
async def eq_set(request: Request, enabled: str = "", source: str = "",
                 preset: str = "", dyn: float = -1.0, b0: float = 0.0,
                 b1: float = 0.0, b2: float = 0.0, b3: float = 0.0,
                 b4: float = 0.0, b5: float = 0.0, b6: float = 0.0,
                 b7: float = 0.0, b8: float = 0.0, b9: float = 0.0):
    """更新 EQ 配置（未传字段不覆盖）。

    - enabled: '1'/'0'（总开关）
    - source:  'manual' | 'preset'（手动/预设互斥）
    - preset:  预设名（bass/treble/vocal/podcast/pop/rock/hiphop/classical/loudness）
    - dyn:     动态补偿强度 0..1（-1=不更新）
    - b0..b9:  手动 10 段 dB（仅当**显式传 b<i> 键**时才作为一次完整写；
      允许全 0 → 手动滑杆拖回 / 全部归零可落平直曲线）
    """
    kw: dict = {}
    if enabled != "":
        kw["enabled"] = enabled in ("1", "true", "True", "on")
    if source:
        kw["source"] = source
    if preset:
        kw["preset"] = preset
    if dyn >= 0.0:
        kw["dyn"] = dyn
    # 前端 eqPost 展开 bands 数组时恒带 b0..b9 全部键（含 0）。
    # 这里用真实 query 里是否出现 b<i> 键来区分「显式写全 0」与「默认参数」，
    # 否则全 0 复位会被误判成"没传"而丢失。
    q_keys = set((request.query_params or {}).keys())
    if any(('b' + str(i)) in q_keys for i in range(10)):
        kw["bands"] = [b0, b1, b2, b3, b4, b5, b6, b7, b8, b9]
    return await _to_thread(bridge.set_eq, **kw)


# ═══════════ 漫游滑块：推荐熟悉度（R49-31）═══════════
# 四档：0=更熟悉（贴合曲库品味）→ 3=更陌生（开拓耳界）。
# 只读 GET 供前端初始化（初始档位持久化在 user_config.rec_pref）；
# POST level=0..3 调用 radio.set_familiarity（内部按需写盘）。

@app.get("/api/roam/familiarity")
async def roam_familiarity_get():
    """当前熟悉度档位 + 四档标签。"""
    return await _to_thread(bridge.get_familiarity)


@app.post("/api/roam/familiarity")
async def roam_familiarity_set(level: int = 1):
    """设置熟悉度档位（0~3）。"""
    return await _to_thread(bridge.set_familiarity, level)


# ═══════════ 顶部搜索（R54）═══════════
# 跨音源并行搜（结果给搜索框建议下拉）；点播仍走既有按身份起播接口
# （在线曲 → source+ext_id；本地曲 → /api/play/track/{id}）。

@app.get("/api/search")
async def search(q: str = "", source: str = ""):
    """跨远程音源搜歌（并行，首源命中即返回）。

    source 非空 = 只搜该音源（搜索框右侧「切换搜索源」开关，R86）。
    """
    return await _to_thread(bridge.search_songs, q, source)


@app.get("/api/search/sources")
async def search_sources():
    """可切换的搜索音源列表（含用户上次选择），供搜索框开关渲染。"""
    return await _to_thread(bridge.search_sources)


@app.post("/api/search/play")
async def search_play(source: str = "", ext_id: str = "", title: str = "",
                      artist: str = "", album: str = "", cover: str = "",
                      duration: float = 0.0):
    """按音源身份起播在线搜索结果（顶部搜索框点播；远程曲无本地 db id）。

    ⚠ duration 必须透传：缺失会让 seed 曲目时长=0 → 前端进度条永远不动（R86）。
    """
    return await _to_thread(bridge.play_search_track,
                            source, ext_id, title, artist, album, cover, duration)


# ═══════════ 前端偏好持久化（R54 · 皮肤/背景模式/元素显示）═══════════
# 纯前端视觉状态（不涉 Qt/radio），FastAPI 线程池直读直写本地 JSON。

@app.get("/api/ui/prefs")
async def ui_prefs_get():
    """读前端偏好（主题 / 背景模式 / 高亮色 / 隐藏元素）。"""
    return await _to_thread(bridge.get_ui_prefs)


@app.post("/api/ui/prefs")
async def ui_prefs_set(patch: dict | None = None):
    """合并保存前端偏好（POST body: {theme?, bgmode?, accent?, hideElems?...}）。

    R94：可选 `__sid` / `__seq`（页面会话 id + 单调序号）→ 后端丢弃**迟到的过期写**，
    保证"用户最后一次动作的值"才是落盘值（一次拖拽连发几十个 POST 时必需）。
    """
    body = dict(patch or {})
    sid = body.pop("__sid", None)
    seq = body.pop("__seq", None)
    return await _to_thread(bridge.set_ui_prefs, body, sid, seq)


# ═══════════ 壁纸（皮肤系统 · R63：真实导入+持久化+「我的壁纸」网格）═══════════
# 此前「壁纸引擎」区块只是前端临时预览（FileReader dataURL 喂 #bgimg，重启即失）。
# 现在走真实后端：SkinManager + WallpaperImporter 落 plugins/skins/<key>/skin.py
# + data/skins/assets/（持久化，重启重扫仍在）；前端网格 = GET list 渲染。

@app.get("/api/wallpapers")
async def wallpapers_list():
    """列出已导入/可用的媒体壁纸（含 /skin-assets 缩略图 URL + active key）。"""
    return await _to_thread(bridge.list_wallpapers)


@app.post("/api/wallpapers/import")
async def wallpapers_import(request: Request, filename: str = ""):
    """导入一张图片壁纸（raw body = 图片字节；filename query 带扩展名）。自动激活。"""
    data = await request.body()
    return await _to_thread(bridge.import_wallpaper_file,
                            filename or "wallpaper.png", data)


@app.post("/api/wallpapers/import-engine")
async def wallpapers_import_engine(source: str = "", name: str = ""):
    """壁纸引擎导入：source = Steam 创意工坊链接 / 数字 ID / 本地工程绝对路径。

    纯本机 Python / 桌面端可用；浏览器 http 页读不到本地路径，通常用
    /import-zip（上传导出的工程压缩包）或 Steam 链接（需本地已订阅）。
    """
    return await _to_thread(bridge.import_wallpaper_engine, source, name)


@app.post("/api/wallpapers/import-zip")
async def wallpapers_import_zip(request: Request, filename: str = ""):
    """上传壁纸引擎工程 zip（raw body = zip 字节）。自动解包 + 激活。"""
    data = await request.body()
    return await _to_thread(bridge.import_wallpaper_zip,
                            filename or "wallpaper.zip", data)


@app.post("/api/wallpapers/activate")
async def wallpapers_activate(key: str = ""):
    """激活指定壁纸皮肤（持久化 active_key）。"""
    return await _to_thread(bridge.activate_wallpaper, key)


@app.post("/api/wallpapers/remove")
async def wallpapers_remove(key: str = ""):
    """删除导入的壁纸皮肤（当前激活则回退 default）。"""
    return await _to_thread(bridge.remove_wallpaper, key)


# ═══════════ 个性化设置（R64 · AI 大模型 / 语音 TTS / 音源 / 通用 / 匹配）═══════════
# 移植老架构 QWebChannel 槽 getSettings/setSetting（ui/bridge_host.py L1926-2100）
# 到 HTTP：前端设置面板不再依赖 QWebChannel，直接 fetch 这几个端点。
# ⚠ 纯 user_config 读写 → 线程池直跑；测试连接涉及网络，同样 _to_thread 隔离。

@app.get("/api/settings")
async def settings_get():
    """读设置白名单（敏感字段只回「是否已设置」标记，不下发明文）。"""
    return await _to_thread(bridge.get_settings)


@app.post("/api/settings/set")
async def settings_set(request: Request, path: str = "", value: str = ""):
    """按点路径写一项设置并落盘。

    同时支持两种传法：
      - query：?path=ai.web_search&value=1（兼容老前端）
      - JSON body：{"path":"ai.api_key","value":"..."}（**敏感值走这里**，
        不进 URL / access log / 浏览器历史）。body 优先于 query。
    """
    try:
        body = await request.json()
        if isinstance(body, dict):
            path = str(body.get("path", path) or path)
            raw = body.get("value", None)
            if raw is not None:
                value = str(raw)
    except Exception:
        pass  # 无 body / 非 JSON → 退回 query 参数
    return await _to_thread(bridge.set_setting, path, value)


@app.post("/api/settings/test")
async def settings_test(kind: str = "ai"):
    """测试连接。kind: ai | tts | tts-sample | voicevox。"""
    kind = (kind or "ai").lower()
    fn = {"ai": bridge.test_ai, "tts": bridge.test_tts,
          "tts-sample": bridge.test_tts_sample,
          "voicevox": bridge.test_voicevox}.get(kind)
    if fn is None:
        return {"ok": False, "msg": f"未知测试项：{kind}"}
    return await _to_thread(fn)


@app.post("/api/settings/qr/start")
async def settings_qr_start(source: str = ""):
    """发起音源扫码登录：source=netease|qq → {ok, key, img(dataURL)}。"""
    return await _to_thread(bridge.source_qr_start, source)


@app.get("/api/settings/qr/poll")
async def settings_qr_poll(source: str = "", key: str = ""):
    """轮询扫码状态 → {status: waiting|scanned|expired|ok|error, msg}。"""
    return await _to_thread(bridge.source_qr_poll, source, key)


# ---------------- WebSocket：后端 → 前端 事件推送 ----------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """前端连上后，所有后端事件都从这里推送（JSON: {event, payload}）。"""
    await ws.accept()
    await bus.register(ws)
    try:
        # 发一条欢迎事件，前端可据此确认 WS 通了
        await ws.send_text('{"event":"connected","payload":{"phase":1}}')
        # 保持连接：读客户端消息（目前只用于心跳/保活）
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text('{"event":"pong","payload":null}')
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa
        logger.warning("WS 异常: %s", e)
    finally:
        await bus.unregister(ws)


# ---------------- 事件广播后台任务 ----------------

# ---------------- 封面代理（/api/cover?u=… 服务端拉远程图） ----------------
# ⚠⚠ CORS 事故（R57 用户报告）：
#     `Access to image at 'https://y.gtimg.cn/...' from origin 'http://127.0.0.1:8765'
#      has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header`
#   在线音源封面（qqmusic/netease 图床）**不带 ACAO 头**，前端 http 页面 <img>/
#   canvas 直连必被 Chromium 拦 → 海报/卡片封面不显示。
#   修复：http(s) 封面一律改走本站代理 `/api/cover?u=<urlencoded>`，由服务端拉图
#   回吐（服务端无浏览器同源限制），前端永远只请求站内地址。
#   本地 file/裸路径仍走原 /covers/<文件名> 静态挂载（见下方 COVERS_DIR）。
# ---------------- R99-T1：段索引覆盖率诊断 ----------------

@app.get("/api/index/coverage")
def index_coverage():
    """段索引覆盖率 + 预热进度 + T3 缓存统计（真机 F5 一行可读）。

    为什么必须可读：覆盖率是 T1 的**唯一验收口径**，而它只能真机观测
    （曲库构成、落盘流是否存在都是运行时事实）。静态断言证明不了任何事。
    ⚠ 同步 def（FastAPI 会丢线程池）：要遍历 291 首做 incomplete 判定，
      不能在事件循环里跑。
    """
    out = bridge.index_coverage()
    try:
        ctl = bridge.controller
        if ctl is not None and hasattr(ctl, "cache_stats"):
            out["track_cache"] = ctl.cache_stats()
    except Exception:  # noqa 缓存统计缺失不影响覆盖率读数
        pass
    return out


@app.post("/api/index/predl")
def index_predl(on: int = -1):
    """R99-T1c：预下载开关（真机 A/B + 一键停 —— 它会吃带宽与磁盘）。

    `on=-1`（默认）只读；`on=1/0` 显式开/关。**改完立即生效**（线程读的是
    `bridge.predl_on`），不需要重启。
    """
    if on in (0, 1):
        bridge.predl_on = bool(on)
    return bridge.predl_state()


# ---------------- 封面代理 ----------------
@app.get("/api/cover")
def api_cover_proxy(u: str = ""):
    """服务端代拉远程封面图（无 CORS 限制）。白名单协议 + 3s 超时 + 短路缓存头。"""
    from fastapi.responses import Response
    if not u or not u.startswith(("http://", "https://")):
        return Response(status_code=400)
    try:
        import httpx
        from utils.no_env_proxy import env_proxy_disabled
        # ★ 2026-11-16：忽略环境代理（geph 9910 未监听 → cover proxy 10061）
        r = httpx.get(u, timeout=6.0, follow_redirects=True,
                      trust_env=not env_proxy_disabled(),
                      headers={"User-Agent": "Mozilla/5.0 MusicAI/1.0"})
        if r.status_code != 200 or not r.content:
            logger.warning("cover proxy %s -> HTTP %s", u[:80], r.status_code)
            return Response(status_code=502)
        ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
        # 远程图床常缺 CORS，这里显式补上（前端跨源 fetch 用不到，但 img 站内同源）
        return Response(content=r.content, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:  # noqa
        logger.warning("cover proxy 失败: %s: %s", type(e).__name__, e)
        return Response(status_code=502)


# ---------------- 封面缓存（/covers/xxx.jpg） ----------------
# ⚠⚠ 事故（2026-09-06）：曲库/推荐接口把本地封面原样吐成
#     `file://data_runtime/covers/local_xxx.jpg`，前端是 **http://127.0.0.1:8765**
#     页面，Chromium 直接拒绝跨协议子资源：
#       js: Not allowed to load local resource: file://data_runtime/covers/local_xxx.jpg
#   （注意：即使是**绝对** file:///D:/... 也会被拒 —— http 页面一律不许加载 file://）
#   所以本地封面必须走 HTTP。这里把 utils/cover_cache 的缓存目录挂出来，
#   由 radio_bridge._cover_url() 把 file:// 形态统一改写成 /covers/<文件名>。
COVERS_DIR = (Path(__file__).resolve().parents[1]
              / os.getenv("MUSICAI_DATA_RUNTIME", "data_runtime") / "covers")
if COVERS_DIR.exists():
    app.mount("/covers", StaticFiles(directory=str(COVERS_DIR)), name="covers")
else:
    logger.warning("封面缓存目录不存在，本地封面将无法显示: %s", COVERS_DIR)

# ---------------- 壁纸资源（/skin-assets/xxx） ----------------
# 导入的壁纸图片拷贝在 data/skins/assets/（WallpaperImporter.assets_dir），
# list_wallpapers 返回的缩略图 url 形如 /skin-assets/<文件名>，前端 <img> 直取。
# ⚠ 须在 mount("/") **之前**挂载（Mount("/") 是兜底，会吞掉其后注册的一切）。
SKIN_ASSETS_DIR = Path(__file__).resolve().parents[1] / "data" / "skins" / "assets"
if SKIN_ASSETS_DIR.exists():
    app.mount("/skin-assets", StaticFiles(directory=str(SKIN_ASSETS_DIR)),
              name="skin-assets")
else:
    logger.warning("壁纸资源目录不存在: %s", SKIN_ASSETS_DIR)


# ---------------- UI 入口（2026-11-18 切换：**新 UI 为默认**）----------------
#   /        → 新 UI（newui/，默认入口）
#   /new     → 同上（保留旧书签/旧链接可用）
#   /legacy  → 旧 UI（webapi/static/，**已弃用**：X-UI-Legacy 响应头 + 页面顶部红色横幅）
# 资源：/new-assets/*（新 UI 自己的 js/css）；其余静态资源仍由末尾 Mount("/") 从 static/ 兜底。
# ⚠ 必须在 mount("/") **之前**注册，否则被兜底 Mount 吞掉。
NEWUI_DIR = Path(__file__).resolve().parents[1] / "newui"
LEGACY_DIR = STATIC_DIR

_LEGACY_BANNER = (
    "<script>"
    "console.warn('[MusicAI] 旧 UI 已弃用（legacy），请使用新 UI: /');"
    "(function(){try{var d=document.createElement('div');"
    "d.textContent='\u26a0 \u65e7 UI \u5df2\u5f03\u7528\uff1a\u8bf7\u4f7f\u7528\u65b0 UI http://127.0.0.1:8765/';"
    "d.style.cssText='position:fixed;left:0;right:0;bottom:0;z-index:2147483647;"
    "padding:6px 10px;font:12px/1.4 system-ui;color:#fff;background:rgba(190,35,35,.92);"
    "text-align:center;cursor:pointer';d.onclick=function(){location.href='/';};"
    "document.body.appendChild(d);}catch(e){}})();"
    "</script>"
)

if NEWUI_DIR.exists():
    @app.get("/")
    @app.get("/new")
    @app.get("/new/")
    async def newui_index():
        """默认入口：新 UI（2026-11-18 起 / 直接指向新 UI）。"""
        return FileResponse(str(NEWUI_DIR / "index.html"),
                            headers={"Cache-Control": "no-store"})

    from fastapi.responses import RedirectResponse

    @app.get("/index.html")
    async def legacy_index_html_redirect():
        """直连旧 index.html 一律回新 UI（防止绕过弃用横幅）。"""
        return RedirectResponse("/", status_code=307)

    @app.get("/legacy")
    @app.get("/legacy/")
    async def legacy_index():
        """旧 UI（已弃用）：保留一个版本用于回退，页面内注入弃用横幅。"""
        from fastapi.responses import HTMLResponse
        try:
            html = (LEGACY_DIR / "index.html").read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa
            return JSONResponse({"error": "legacy ui missing", "detail": str(e)}, status_code=404)
        html = html.replace("</body>", _LEGACY_BANNER + "</body>") if "</body>" in html else html + _LEGACY_BANNER
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-UI-Legacy": "1"})

    app.mount("/new-assets", StaticFiles(directory=str(NEWUI_DIR)), name="new-assets")
    logger.info("UI 入口：/ → 新 UI（%s）；/legacy → 旧 UI（已弃用）", NEWUI_DIR)
else:
    logger.warning("newui 目录不存在，/ 将退回旧 UI: %s", NEWUI_DIR)

# ---------------- 静态站点（现有前端，未改动） ----------------
# 必须在所有 API 路由**之后**挂载，否则会吞掉 /api/*
# ⚠ /covers 必须在 "/" 之前挂（Mount("/") 是兜底，会吞掉其后注册的一切）
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:
    logger.warning("静态目录不存在，跳过挂载: %s", STATIC_DIR)


def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("启动 FastAPI：http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main(*(sys.argv[1:] or []))
