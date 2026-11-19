"""start_api_host.py —— FastAPI 版一体化启动器（Phase 2）

与老 start_webui.bat 的区别：
  老：QWebEngineView 加载 file:// HTML，前后端靠 QWebChannel 通信（脆弱）
  新：QWebEngineView 加载 http://127.0.0.1:8765（本机 FastAPI），
      前后端靠 HTTP + WebSocket 通信（可在真浏览器 F12 调试）

启动顺序沿用老架构验证过的模式（先开窗、后装配）：
  1. 起 QApplication
  2. 后台线程起 uvicorn（FastAPI 立刻可用，radio_ready=False）
  3. 开窗并指向 http://127.0.0.1:8765
  4. 后台线程装配 AIRadioController（加载 CLAP/LLM 约 70s）
  5. QTimer 轮询，就绪后 attach 到 webapi.radio_bridge（radio_ready 变 True）
  6. 进入 Qt 事件循环
"""
from __future__ import annotations

# ★ 2026-11-19：发布副本使用独立 NEMbox 配置目录（空登录态），
#   避免读取 live 的 ~/.netease-musicbox/cookie.txt（登录态不得跨机器保存）。
import os as _os
_NEMBASE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data_runtime", "nembox")
for _sub in ("config", "data", "cache"):
    _os.makedirs(_os.path.join(_NEMBASE, _sub), exist_ok=True)
_os.environ["XDG_CONFIG_HOME"] = _os.path.join(_NEMBASE, "config")
_os.environ["XDG_DATA_HOME"] = _os.path.join(_NEMBASE, "data")
_os.environ["XDG_CACHE_HOME"] = _os.path.join(_NEMBASE, "cache")

# ★ 2026-11-16：软件不需要系统代理。在任何第三方库/子进程启动前清掉进程内
# 的代理环境变量（geph 9910 失效会拖垮网易云二维码/cover/weather/hf-mirror）。
# 只影响本进程，不改 Windows 用户级环境变量。MUSICAI_IGNORE_ENV_PROXY=0 可跳过。
try:
    from utils.no_env_proxy import strip_env_proxy as _strip_proxy
    _removed_proxy = _strip_proxy()
    if _removed_proxy:
        print(f"[启动] 已忽略环境代理变量: {', '.join(_removed_proxy)}")
except Exception as _e:  # noqa 工具缺失不应阻止启动
    print(f"[启动] 忽略环境代理设置失败（继续）: {_e}")

import logging
import os
import socket
import sys
import threading
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# ★ 2026-11-18 task-1：**始终落一份文件日志**。此前只有 start_radio_logged.bat 重定向
#   stdout 时才有日志；用户直接双击启动时，TTS 窗口/回填/同驻全看不见，排障只能靠猜。
try:
    from logging.handlers import RotatingFileHandler
    _LOG_DIR = Path(__file__).resolve().parent / "data_runtime"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _fh = RotatingFileHandler(_LOG_DIR / "app_auto.log", maxBytes=20 * 1024 * 1024,
                              backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception:  # noqa 日志失败绝不影响启动
    pass
logger = logging.getLogger("MusicAI.APIHost")

# ★ 发布版：每次启动扫描 plugins/ 与 models/ 两个独立文件夹（失败不影响启动）
try:
    from core.release_catalog import run as _release_scan
    _release_scan()
except Exception as _e:  # noqa
    logger.warning('[release] 启动扫描失败（继续）: %s', _e)

API_HOST = "127.0.0.1"
API_PORT = 8765
API_URL = f"http://{API_HOST}:{API_PORT}"


def _port_in_use(host: str = API_HOST, port: int = API_PORT) -> bool:
    """探测端口是否已被占用（TCP connect 试探）。"""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _is_our_webapi() -> bool:
    """端口上是否已是本项目 Web API（GET / 含 <title>Radio Intelligence）。"""
    try:
        with urllib.request.urlopen(f"{API_URL}/", timeout=1.5) as r:
            body = r.read(4096).decode("utf-8", "ignore")
            return "Radio Intelligence" in body or "ai-radio" in body
    except Exception:  # noqa
        return False


def _precheck_port() -> None:
    """⚠ 已有一个实例在跑时，第二个实例 bind 8765 会 10048 失败；
    若继续装配 radio 会形成「幽灵双开」（窗口指向旧实例、资源白耗）。
    启动前预检：端口已被本项目占用 → 明确提示并退出。"""
    if not _port_in_use():
        return
    if _is_our_webapi():
        logger.warning(
            "检测到 8765 已有本项目实例在运行（http://127.0.0.1:8765）。\n"
            "  如需重启，请先关闭旧实例（任务管理器结束旧 python 进程）再启动本程序；\n"
            "  或直接使用已在运行的实例。本次启动已取消，避免端口冲突(10048)与双开。"
        )
    else:
        logger.error(
            "端口 %s:%d 已被其它程序占用，无法启动 Web API。\n"
            "  请先释放该端口（netstat -ano | findstr 8765 → 结束对应 PID）后重试。",
            API_HOST, API_PORT,
        )
    sys.exit(3)


def _tune_gil_switch() -> None:
    """★ 2026-09-19：**把 GIL 交接从默认 5ms 缩到 1ms**。

    音频 RT 回调是个 Python 函数 ⇒ 每次都要抢 GIL。默认 5ms 的切换粒度下，
    别的线程（下载的 Python 循环 / librosa 解码 / 预取组装）一次持锁就可能让
    23ms 的回调预算被吃穿 ⇒ `PortAudio output underflow` ⇒ 听感"卡几下"。
    缩短切换间隔是这条链路的经典对症手段（代价：切换开销略增，对本应用可忽略）。
    注：numpy/torch 的大算子会主动释放 GIL，故本刀主要治**纯 Python 循环**。"""
    try:
        import sys as _sys
        if hasattr(_sys, "setswitchinterval"):
            _sys.setswitchinterval(0.001)
            logger.info("GIL 切换间隔已设为 1ms（护音频 RT 回调）")
    except Exception as e:  # noqa
        logger.warning("设置 GIL 切换间隔失败（不影响播放）: %s", e)


def _warn_if_memory_tight() -> None:
    """★ 2026-09-19（R119-2f）：**提交量/物理内存余量**自检（只读，失败静默）。

    为什么值得开机就喊一声：实测"界面与声音一起卡"的真因是**系统提交量被占满**
    （提交上限 45.74 GB 只剩 1.57 GB ⇒ 内存管理器压缩 6.74 GB + 换出我方页面
    ⇒ 再访问是硬缺页 ⇒ 整进程冻结 ~0.9s）。这类现象**看起来像音频 bug，其实不是**，
    没有这条日志就只能靠猜。本函数不改变任何行为，只把事实写进日志。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _MSX(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MSX()
        st.dwLength = ctypes.sizeof(_MSX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return
        G = 1024.0 ** 3
        avail_phys = st.ullAvailPhys / G
        avail_commit = st.ullAvailPageFile / G
        logger.info("内存自检：物理可用 %.2f GB / 提交可用 %.2f GB（占用 %d%%）",
                    avail_phys, avail_commit, st.dwMemoryLoad)
        # 阈值可用环境变量覆盖（便于强制演练"见红"：如 MUSICAI_MEM_WARN_COMMIT_GB=999）
        _ct = float(os.environ.get("MUSICAI_MEM_WARN_COMMIT_GB", "2.0") or 2.0)
        _pt = float(os.environ.get("MUSICAI_MEM_WARN_PHYS_GB", "1.5") or 1.5)
        if avail_commit < _ct or avail_phys < _pt:
            logger.warning(
                "⚠ 系统内存紧张：物理可用 %.2f GB、**提交可用仅 %.2f GB**。"
                "这种情况会造成**整机级别的顿卡**（内存管理器压缩/换出页面 ⇒ 再访问是硬缺页），"
                "表现是「界面和声音一起卡」，**不是音频代码的问题**。"
                "建议：关掉不用的重内存程序；本项目的口播 LLM（Ollama qwen3.5:9b）"
                "因 `ai/model.py` 的 keep_alive=-1 会**常驻**并占数 GB 提交量，"
                "不用时可用 `ollama stop qwen3.5:9b` 释放。",
                avail_phys, avail_commit)
    except Exception as e:  # noqa 自检绝不拖垮启动
        logger.debug("内存自检跳过: %s", e)


def start_api_server() -> None:
    """后台线程起 uvicorn（daemon，随主进程退出）。"""
    import uvicorn
    from webapi.server import app

    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")


def _install_devtools(win):
    """给宿主窗口装一个「内嵌 DevTools」——**F12 / Ctrl+Shift+I** 即开。

    ⚠ 为什么必须是内嵌（而不是"用浏览器打开 127.0.0.1:8765 按 F12"）：
      ① 本机设了 `HTTP_PROXY=http://127.0.0.1:3111`，浏览器直连 localhost 会被代理
         拦截（实测 `curl http://127.0.0.1:8765/docs` → **502 Bad Gateway**）→ 页面
         能显示但 WebSocket/接口连不上，等于调不了；
      ② 边界情况（`__water` 只在 QWebEngineView 实例里）也只有内嵌 DevTools
         看得到同一个 `window`。
    另外尝试开启 `DeveloperExtrasEnabled`（让**右键 → 检查**也能开，对付 canvas 抢右键）——
    但本版 PySide6 **未暴露该枚举**，故为尽力而为（见下）。

    返回：`open_devtools()` 可编程调用（供验收脚本用，不依赖按键）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    # 让 canvas 上的右键菜单出现「检查元素」。
    # ⚠ 本机 PySide6(6.x) 的 WebAttribute **没有** DeveloperExtrasEnabled（实测
    #   `type object 'WebAttribute' has no attribute 'DeveloperExtrasEnabled'`），
    #   故用 getattr 探测：有就开，没有就只靠快捷键（F12 路径完全不依赖它）。
    _extras = getattr(QWebEngineSettings.WebAttribute, "DeveloperExtrasEnabled", None)
    if _extras is not None:
        try:
            win.page().settings().setAttribute(_extras, True)
        except Exception as e:  # noqa
            logger.warning("DeveloperExtrasEnabled 设置失败: %s", e)
    else:
        logger.info("本版 PySide6 无 DeveloperExtrasEnabled → 右键「检查」不可用，"
                    "请用 F12 / Ctrl+Shift+I（功能等价）")

    holder = {"dev": None}

    def open_devtools():
        if holder["dev"] is None:
            d = QWebEngineView()
            d.setWindowTitle("MusicAI DevTools（__water 调试台）")
            d.resize(1180, 760)
            # 关键：DevTools 页面绑定到**主页面** —— 看到的是同一个 window
            win.page().setDevToolsPage(d.page())
            holder["dev"] = d
        holder["dev"].show()
        holder["dev"].raise_()
        holder["dev"].activateWindow()
        logger.info("DevTools 已打开（F12 开关；Console 里可直接用 __water）")
        return True

    for seq in ("F12", "Ctrl+Shift+I"):
        try:
            sc = QShortcut(QKeySequence(seq), win)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(open_devtools)
        except Exception as e:  # noqa
            logger.warning("快捷键 %s 绑定失败: %s", seq, e)
    return open_devtools


def _apply_runtime_env_ai_config() -> None:
    """★ 2026-11-16：启动时的**内存级** AI 后端覆盖（不写磁盘）。

    由 `start_api_host.bat` 设置 `MUSICAI_AI_BACKEND=llamacpp` 时，把用户配置对象
    在**进程内**切到直连 llama.cpp server（bat 负责拉起/关闭服务进程）。
    ⚠ 只改内存，不调 `user_config.save()` ⇒ 用户的 config.json 一字不动，
      下次不用 bat 直接启动时仍是原来的后端。

    ★★ 2026-11-19 补强（发行副本）：裸 `python -u start_api_host.py` 启动（无 bat env）
       时 config.json 里 backend 已是 llamacpp，但旧逻辑第一条就 return ⇒
       `llamacpp_manager.start()` 从不执行 ⇒ 只能搭外部 8080 便车，外部一死
       口播静默全回退离线文案（"又不播音频/不生成文案"）。现在无 env 时也按
       config.backend 拉起**包内** exe/model，任何启动方式都能自管 LLM。
    """
    import os
    backend = (os.environ.get("MUSICAI_AI_BACKEND") or "").strip().lower()
    _from_config = not backend
    if _from_config:
        try:
            from config.settings import user_config as _uc
            backend = ((_uc.data.get("ai") or {}).get("backend") or "").strip().lower()
        except Exception:  # noqa
            pass
    if backend != "llamacpp":
        return
    # 裸启动时补包内路径/参数（env 显式给出则尊重，不覆盖）
    if _from_config:
        from pathlib import Path as _P
        _root = _P(__file__).resolve().parent
        for _k, _rel in (
            ("MUSICAI_LLAMACPP_EXE", r"tools\llamacpp-b10955\llama-server.exe"),
            ("MUSICAI_LLAMACPP_MODEL", r"models\llm\Qwen3.6-35B-A3B.gguf"),
        ):
            if not os.environ.get(_k):
                _cand = _root / _rel
                if _cand.is_file():
                    os.environ[_k] = str(_cand)
        os.environ.setdefault("MUSICAI_LLAMACPP_NUM_CTX", "16384")
        os.environ.setdefault("MUSICAI_LLAMACPP_KV_TYPE", "q8_0")
        os.environ.setdefault("MUSICAI_CORESIDENT", "1")
    try:
        from config.settings import user_config
        ai = user_config.data.setdefault("ai", {})
        ai["backend"] = "llamacpp"
        ai["llamacpp_reasoning_budget"] = int(
            os.environ.get("MUSICAI_LLAMACPP_REASONING_BUDGET") or ai.get("llamacpp_reasoning_budget") or 512)
        ai["llamacpp_num_ctx"] = int(
            os.environ.get("MUSICAI_LLAMACPP_NUM_CTX") or ai.get("llamacpp_num_ctx") or 8192)

        # ★★ 2026-11-16 端口接线修复：**端口以管理器为唯一来源**。
        #   事故：config.json 里 llamacpp_host 残留实验值 2719，而管理器拉起的是
        #   8080 ⇒ 客户端连 2719 → WinError 10061 → 口播全回退离线文案。
        #   修法：先拉起管理器，再用它的实际端口覆盖 ai['llamacpp_host']，
        #   config 里写错也无所谓；仅 env 显式 MUSICAI_LLAMACPP_HOST 时尊重用户指定。
        _mgr_port = 8080
        try:
            from core import llamacpp_manager as _mgr
            _mgr_port = _mgr._port()
            # 进程生命周期由 Python 管理（不用旧的复杂 PowerShell bat：真机双击
            # 出现"闪退 + bat 自身消失"）。start() 内部 atexit 注册清理。
            if not _mgr.start():
                logger.warning("[启动] llama.cpp server 未就绪；口播将按 llamacpp 配置请求后降级模板")
        except Exception as le:  # noqa
            logger.warning("[启动] llama.cpp server 启动异常（继续）: %s: %s",
                           type(le).__name__, le)
        _env_host = os.environ.get("MUSICAI_LLAMACPP_HOST")
        ai["llamacpp_host"] = _env_host or f"http://127.0.0.1:{_mgr_port}"
        logger.info("[启动] AI 后端=llamacpp，host=%s（端口以管理器为准，config 残留值已忽略），budget=%s；仅内存覆盖不落盘",
                    ai["llamacpp_host"], ai["llamacpp_reasoning_budget"])
    except Exception as e:  # noqa 覆盖失败不该阻止启动
        logger.warning("[启动] llamacpp 运行时配置失败（继续原后端）: %s: %s",
                       type(e).__name__, e)


_SHUTTING = False   # 防重入：aboutToQuit 与 exec 返回后只清理一次

def _graceful_shutdown(exit_now: bool = False) -> None:
    """★ 2026-11-18：**关窗即全清**（用户："关掉窗口就应自动杀全部进程"）。

    关窗 → app.exec() 返回 → 这里依次停：网桥/电台、音频引擎、CLAP 子进程、
    llama-server，最后（exit_now）用 os._exit 兜底 —— 因为项目里存在非 daemon
    线程（播放器/uvicorn 线程池等），只靠 sys.exit 可能被它们拖住，表现就是
    "窗口关了但进程还在、8765 还被占"。
    所有步骤独立 try/except：任何一步失败都不影响后续清理。
    """
    import logging as _lg
    global _SHUTTING
    if _SHUTTING:
        return
    _SHUTTING = True
    log = _lg.getLogger("MusicAI.Exit")
    log.info("[退出] 窗口已关闭，开始清理进程…")
    # 1) 网桥（会停播放器 / 事件泵 / Qt 编排器）
    try:
        from webapi.radio_bridge import bridge
        bridge.detach()
        log.info("[退出] radio bridge 已 detach")
    except Exception as e:  # noqa
        log.debug("[退出] bridge.detach 跳过: %s", e)
    # 2) 音频引擎
    try:
        from core import audio_engine as _ae
        _ae.shutdown()
        log.info("[退出] audio_engine 已关闭")
    except Exception as e:  # noqa
        log.debug("[退出] audio_engine.shutdown 跳过: %s", e)
    # 3) CLAP 常驻子进程
    try:
        from core import clap_proc as _cp
        _cp.shutdown()
        log.info("[退出] CLAP worker 已关闭")
    except Exception as e:  # noqa
        log.debug("[退出] clap_proc.shutdown 跳过: %s", e)
    # 4) llama-server（管理器的 stop 也是 atexit 备份）
    try:
        from core import llamacpp_manager as _mgr
        _mgr.stop()
        log.info("[退出] llama-server 已停止")
    except Exception as e:  # noqa
        log.debug("[退出] llamacpp_manager.stop 跳过: %s", e)
    # 5) 兜底：终止本进程的**直接子进程**（若还有残留）
    try:
        import psutil
        me = psutil.Process()
        kids = me.children(recursive=False)
        for c in kids:
            try:
                c.terminate()
            except Exception:
                pass
        if kids:
            log.info("[退出] 终止残留子进程 %d 个", len(kids))
    except Exception:  # noqa
        pass
    try:
        for h in _lg.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
    except Exception:
        pass
    if exit_now:
        # 跳过非 daemon 线程的 join，确保进程真的退出、端口/显存/模型全部释放
        os._exit(0)


def main(full: bool = True) -> None:
    # ★★ 2026-11-16：**GPU 显存额度必须在首次 CUDA 分配前设定**（给水体的 WebGL
    #   留显存）。此前只在 ui/bridge_host.py::main() 调 apply_gpu_budget()，
    #   而 FastAPI 启动器走的是 start_api_host.py → _init_radio() → AIBrain()，
    #   **绕过 bridge_host.main() ⇒ GPU 预算从未生效**。
    #   后果：melotts_gpu=true 后 MeloTTS 无上限吃显存，挤占水体渲染 ⇒ "水体特别卡"。
    #   配置 gpu.melotts_gpu_fraction=0.15 / clap=0.05 ⇒ 本进程最多 20%（约 1.6GB），
    #   其余 80% 留给 WebGL / 其他 GPU 消费者。★ 2026-11-17 task-1：MeloTTS 已退休，
    #   本上限实际只约束 CLAP；IndexTTS 在独立 worker（不受此限）。
    #   失败不阻断启动。
    # ★ A/B：MUSICAI_SKIP_GPU_BUDGET=1 ⇒ 跳过（回到"无预算"状态对照水体观感）
    if os.environ.get("MUSICAI_SKIP_GPU_BUDGET", "") not in ("1", "true", "True"):
        try:
            from core.gpu_budget import apply_gpu_budget
            apply_gpu_budget()
        except Exception as _ge:  # noqa
            logger.warning("[启动] GPU 显存额度设置失败（继续）: %s: %s",
                           type(_ge).__name__, _ge)
    else:
        logger.info("[启动] MUSICAI_SKIP_GPU_BUDGET=1 ⇒ 跳过 GPU 显存预算（A/B 对照）")
    _apply_runtime_env_ai_config()
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except Exception as e:  # noqa
        logger.error("无法导入 QWebEngineView: %s", e)
        raise

    app = QApplication(sys.argv)
    # ★ 2026-11-18：应用显示名 + 品牌图标（Radio Intelligence）。
    #   图标取 webapi/static/brand/logo_small.png（同目录还有 logo.png 大图）。
    _BRAND_ICON = Path(__file__).resolve().parent / "webapi" / "static" / "brand" / "logo_small.png"
    if not _BRAND_ICON.exists():
        _BRAND_ICON = Path(__file__).resolve().parent / "webapi" / "static" / "brand" / "logo.png"
    try:
        app.setApplicationName("Radio Intelligence")
        app.setApplicationDisplayName("Radio Intelligence")
    except Exception:  # noqa
        pass
    try:
        from PySide6.QtGui import QFont, QIcon
        app.setFont(QFont("Microsoft YaHei", 10))
        if _BRAND_ICON.exists():
            app.setWindowIcon(QIcon(str(_BRAND_ICON)))
    except Exception:  # noqa
        pass

    # ⚠ 关键：QtMarshaller 的 QTimer **必须在 Qt 主线程（本线程）创建**。
    #   若在 FastAPI 的 startup（uvicorn 线程）里创建，那个线程没有 Qt 事件循环，
    #   QTimer.timeout 永不触发 → 每次 marshaller.call() 都死等到超时，
    #   日志表现为 "get_state 失败:" 且**消息为空**（因为 str(TimeoutError()) == ''）。
    try:
        from webapi.radio_bridge import marshaller
        marshaller.start()
        logger.info("Qt 编排器已在主线程启动（QTimer 归属主线程）")
    except Exception as e:  # noqa
        logger.warning("Qt 编排器启动失败: %s", e)

    # 0) 端口预检：已有本项目实例则退出（防 10048 幽灵双开）
    _precheck_port()

    # 0b) ★ 2026-09-19：GIL 切换间隔（必须在任何重活之前设好）
    _tune_gil_switch()

    # 0c) ★ 2026-09-19（R119-2f）：内存/提交量自检（只读、非阻塞；紧张时给出可读告警）
    _warn_if_memory_tight()

    # 0d) ★ 2026-09-19（R119-2n）：GC 取证 + 阈值放宽（唯一能"全线程同冻"的机制）
    try:
        from core.audio_engine import tune_gc
        tune_gc()
    except Exception as _e:  # noqa
        logger.debug("GC 调参跳过: %s", _e)

    # 1) FastAPI 先起来（前端立刻能连，radio_ready 仍是 False）
    threading.Thread(target=start_api_server, daemon=True, name="uvicorn").start()
    logger.info("FastAPI 正在启动：%s", API_URL)

    # 2) 窗口：直接指向本机 API（不再是 file://）
    win = QWebEngineView()
    # ★ 2026-11-18：窗口标题改为品牌名；窗口/任务栏图标用品牌图。
    win.setWindowTitle("Radio Intelligence")
    # ★ 2026-11-19：**消除放大/缩放动画时的白边**——QWebEngine 默认页面底色是白色，
    #   geometry 动画期间新扩展出来的窗口区域尚未被网页重绘，会先露一条白边再"跟上"。
    #   把页面底色设成应用同款深色，动画期间露出的就是深色，观感连续。
    try:
        from PySide6.QtGui import QColor as _QColor
        win.page().setBackgroundColor(_QColor("#0b0b12"))
        logger.info("[Window] 页面底色已设为深色（消除缩放动画白边）")
    except Exception as _e:  # noqa
        logger.warning("[Window] 设置页面底色失败: %s", _e)
    # ★★ 2026-11-18：**无边框窗口 + 自绘标题栏**（用户："外边框要变成应用里设计过的"）。
    #   页面通过 fetch('/api/window/action') → FastAPI → window_ctl → Qt 主线程执行。
    #   MUSICAI_NATIVE_FRAME=1 ⇒ 回退系统原生边框（排障用）。
    _native_frame = os.environ.get("MUSICAI_NATIVE_FRAME", "") in ("1", "true", "True")
    if not _native_frame:
        try:
            from PySide6.QtCore import Qt as _Qt
            win.setWindowFlags(_Qt.Window | _Qt.FramelessWindowHint)
            win.setMinimumSize(960, 620)
            logger.info("[Window] 无边框窗口已启用（自绘标题栏；MUSICAI_NATIVE_FRAME=1 可回退）")
        except Exception as _we:  # noqa
            logger.warning("[Window] 无边框设置失败，沿用系统边框: %s", _we)
    try:
        from PySide6.QtCore import QObject, Slot, Qt as _Qt2
        class _WindowActions(QObject):
            """窗口动作（必须由 Qt 主线程执行；HTTP 侧用 QueuedConnection 排队）。"""
            def __init__(self, w):
                super().__init__(w)
                self._w = w
            # ★ 2026-11-18：最大化/最小化/还原改成**缓动动画**，不再硬切。
            #   无边框窗口原生的 showMaximized/showMinimized 在 Windows 上是瞬跳；
            #   这里用 QPropertyAnimation 对窗口几何做 190ms OutCubic 过渡，
            #   最小化先缩向屏幕底部中央再真正 minimized，从任务栏恢复时再展开。
            def _anim_geo(self, start, end, dur, done=None):
                """窗口几何缓动。

                ⚠ 2026-11-19 修复两处"不够流畅"：
                  ① 动画前**临时解除最小尺寸**（min 960×620 会让最小化缩不下去 → 卡住跳动），
                     结束后恢复；
                  ② 缓动改 OutQuint（收尾更柔），时长 190→230ms。
                """
                w = self._w
                old_min = None
                try:
                    old_min = w.minimumSize()
                    if old_min is not None and old_min.width() > 120:
                        w.setMinimumSize(120, 90)
                except Exception:  # noqa
                    old_min = None
                def _restore_min():
                    if old_min is not None:
                        try: w.setMinimumSize(old_min)
                        except Exception: pass
                try:
                    from PySide6.QtCore import QPropertyAnimation, QEasingCurve
                    a = QPropertyAnimation(w, b"geometry", w)
                    a.setDuration(dur)
                    a.setStartValue(start)
                    a.setEndValue(end)
                    a.setEasingCurve(QEasingCurve.OutQuint)
                    a.finished.connect(_restore_min)
                    if done is not None:
                        a.finished.connect(done)
                    a.start()
                    self._geo_anim = a          # 持引用防 GC
                except Exception:
                    try: w.setGeometry(end)
                    except Exception: pass
                    _restore_min()
                    if done is not None:
                        try: done()
                        except Exception: pass

            def _screen_geo(self):
                try:
                    return self._w.screen().availableGeometry()
                except Exception:
                    from PySide6.QtCore import QRect
                    return QRect(0, 0, 1280, 800)

            @Slot()
            def doMinimize(self):
                from PySide6.QtCore import QRect
                w = self._w
                cur = w.geometry()
                self._pre_min_geo = QRect(cur)
                sc = self._screen_geo()
                tw = max(160, int(cur.width() * 0.55))
                th = max(110, int(cur.height() * 0.55))
                tx = sc.center().x() - tw // 2
                ty = sc.bottom() - th - 6
                tgt = QRect(tx, ty, tw, th)

                def _done():
                    w.showMinimized()
                    try: w.setGeometry(cur)
                    except Exception: pass
                self._anim_geo(cur, tgt, 210, _done)

            @Slot()
            def doMaximize(self):
                from PySide6.QtCore import QRect
                w = self._w
                if w.isFullScreen():
                    w.showNormal()
                    return
                cur = w.geometry()
                if w.isMaximized():
                    tgt = self._norm_geo or w.normalGeometry()
                    w.showNormal()          # 先退出最大化（瞬间回到 normal）
                    w.setGeometry(cur)      # 再拉回满屏位置，从满屏缓动到 normal
                    w.show()                # 确保可见（showNormal 后已可见）
                    self._anim_geo(cur, tgt, 230)
                else:
                    self._norm_geo = QRect(cur)
                    tgt = self._screen_geo()
                    def _done():
                        w.showMaximized()
                    self._anim_geo(cur, tgt, 230, _done)

            # 从任务栏恢复：展开动画（poll minimized 状态变化）
            def _tick_min_restore(self):
                w = self._w
                try:
                    m = w.isMinimized()
                except Exception:
                    return
                if getattr(self, "_was_min", False) and not m:
                    from PySide6.QtCore import QRect
                    tgt = getattr(self, "_pre_min_geo", None) or w.normalGeometry()
                    try:
                        c = w.geometry()
                        small = QRect(c.center().x() - c.width() // 4,
                                      c.bottom() - c.height() // 4,
                                      max(160, c.width() // 2),
                                      max(110, c.height() // 2))
                        w.setGeometry(small)
                        self._anim_geo(small, tgt, 220)
                    except Exception:
                        pass
                self._was_min = m
            @Slot()
            def doFullscreen(self):
                """F11：全屏 / 退出全屏（无边框窗口）。"""
                if self._w.isFullScreen():
                    self._w.showNormal()
                else:
                    self._w.showFullScreen()

            def is_fullscreen(self):
                return bool(self._w.isFullScreen())

            @Slot()
            def doClose(self):
                self._w.close()
            @Slot()
            def doMove(self):
                wh = self._w.windowHandle()
                if wh is not None:
                    wh.startSystemMove()
            @Slot(int)
            def doResize(self, edges):
                wh = self._w.windowHandle()
                if wh is not None:
                    try:
                        wh.startSystemResize(_Qt2.Edges(int(edges)))
                    except Exception:
                        pass
        from webapi import window_ctl as _wc
        _win_actions = _WindowActions(win)
        _wc.bind(_win_actions)
        try:
            from PySide6.QtCore import QTimer as _QT
            _anim_timer = _QT(win)
            _anim_timer.setInterval(120)
            _anim_timer.timeout.connect(_win_actions._tick_min_restore)
            _anim_timer.start()
        except Exception:  # noqa
            pass
    except Exception as _we:  # noqa
        logger.warning("[Window] 动作桥绑定失败（窗控不可用）: %s", _we)
    # 注入"桌面壳"标志：页面据此显示自绘标题栏（浏览器打开时不显示）
    if not _native_frame:
        try:
            from PySide6.QtWebEngineCore import QWebEngineScript as _QS
            _flag = _QS()
            _flag.setName("musicai-desktop-flag")
            _flag.setInjectionPoint(_QS.InjectionPoint.DocumentCreation)
            _flag.setWorldId(_QS.ScriptWorldId.MainWorld)
            _flag.setRunsOnSubFrames(False)
            _flag.setSourceCode("window.__MUSICAI_DESKTOP__=true;")
            win.page().scripts().insert(_flag)
        except Exception as _we:  # noqa
            logger.warning("[Window] 桌面标志注入失败: %s", _we)
    try:
        from PySide6.QtGui import QIcon as _QIcon
        if _BRAND_ICON.exists():
            win.setWindowIcon(_QIcon(str(_BRAND_ICON)))
    except Exception:  # noqa
        pass
    win.resize(1280, 800)
    # ★★ 2026-11-16：**QWebEngine 的 HTTP 缓存必须显式关掉**。
    #   老 UI(ui/bridge_host.py:2639-2648) 早就踩过并修了，但 FastAPI 版启动器
    #   这里只 setUrl，没设 NoCache ⇒ Qt 窗口可能一直拿缓存里的**旧版 ui-water.js**，
    #   表现为"改了前端没变化 / 换 bat 后水面跳变与削峰回来"（磁盘和服务端都是新版，
    #   只有窗口里跑的是旧版）。服务端 no-store 中间件管不到 Qt 自己的缓存层。
    #   与老 UI 同款纪律：NoCache + URL 带 JS mtime 版本戳双重保险。
    # ★ 2026-11-16 A/B 开关：用户反馈"改 bat 后水体味道变了"，怀疑是加载行为变化。
    #   `MUSICAI_QT_CACHE=1` ⇒ 保留缓存 + 不加版本戳（回到 22:44 之前的加载方式），
    #   用于真机对照。默认仍 NoCache（防旧 JS 残留）。
    _qt_keep_cache = os.environ.get("MUSICAI_QT_CACHE", "") in ("1", "true", "True")
    try:
        from PySide6.QtWebEngineCore import QWebEngineProfile
        if _qt_keep_cache:
            logger.info("[启动] MUSICAI_QT_CACHE=1 ⇒ 保留 QWebEngine 缓存（A/B 对照模式）")
        else:
            win.page().profile().setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
            logger.info("[启动] QWebEngine HTTP 缓存已关闭（防旧 JS 残留）")
    except Exception as e:  # noqa 关闭失败不该阻止启动
        logger.warning("[启动] 关闭 QWebEngine 缓存失败: %s: %s", type(e).__name__, e)
    _page_url = QUrl(API_URL)
    if not _qt_keep_cache:
        try:
            _static = Path(__file__).resolve().parent / "webapi" / "static"
            _mtime = 0
            for _name in ("ui-water.js", "ui-state.js", "index.html"):
                _p = _static / _name
                if _p.exists():
                    _mtime = max(_mtime, int(_p.stat().st_mtime))
            if _mtime:
                _page_url.setQuery("v=%d" % _mtime)
        except Exception:  # noqa 版本戳失败不影响加载
            pass
    win.setUrl(_page_url)
    win.show()
    logger.info("界面已显示，指向 %s", _page_url.toString())

    # ★★ 2026-11-18：**窗口级快捷键**（不依赖网页 JS，QWebEngine 里 F5/F11 网页拿不到时也生效）：
    #   F5 / Ctrl+R = 重新加载页面；F11 = 全屏/退出全屏；Esc = 退出全屏。
    #   之前用户"按 F5 没反应"：网页里虽加了 keydown 监听，但若页面是旧缓存/焦点在
    #   窗口容器上，事件到不了页面 → 用 QShortcut(Qt.ApplicationShortcut) 兜底。
    try:
        from PySide6.QtGui import QShortcut, QKeySequence
        from PySide6.QtCore import Qt as _QtKey
        def _short_reload():
            try:
                logger.info("[快捷键] 重新加载页面")
                win.reload()
            except Exception as _e:  # noqa
                logger.warning("[快捷键] reload 失败: %s", _e)
        def _short_fullscreen():
            try:
                if win.isFullScreen():
                    win.showNormal()
                else:
                    win.showFullScreen()
            except Exception as _e:  # noqa
                logger.warning("[快捷键] fullscreen 失败: %s", _e)
        def _short_escape():
            """Esc 只在全屏时退出全屏，不做别的。"""
            try:
                if win.isFullScreen():
                    win.showNormal()
            except Exception:  # noqa
                pass
        for _seq, _fn in (("F5", _short_reload), ("Ctrl+R", _short_reload),
                          ("F11", _short_fullscreen), ("Esc", _short_escape)):
            _sc = QShortcut(QKeySequence(_seq), win)
            _sc.setContext(_QtKey.ApplicationShortcut)
            _sc.activated.connect(_fn)
        logger.info("[快捷键] F5/Ctrl+R 刷新、F11/Esc 全屏 已绑定（窗口级）")
    except Exception as _e:  # noqa
        logger.warning("[快捷键] 绑定失败（不影响播放）: %s", _e)
    # 2b) 内嵌 DevTools（F12 / Ctrl+Shift+I / 右键→检查）—— 绕开浏览器代理问题
    try:
        _install_devtools(win)
    except Exception as e:  # noqa
        logger.warning("DevTools 装配失败（不影响播放）: %s", e)
    if os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING"):
        logger.info("远程调试已开：Chrome 打开 http://127.0.0.1:%s",
                    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"])

    # 3) 后台装配 radio，就绪后接入 API 网桥
    if full:
        _attach_radio_async()

    # ★ 2026-11-18：关最后一个窗口 → 退出应用 → 走统一清理（见 _graceful_shutdown）
    try:
        app.setQuitOnLastWindowClosed(True)
    except Exception:  # noqa
        pass
    app.aboutToQuit.connect(lambda: _graceful_shutdown(exit_now=False))
    # 控制台 Ctrl+C / 任务管理器"结束任务"也走同一条清理路径
    try:
        import signal as _sig
        def _on_sig(signum, frame):  # noqa
            logger.info("[退出] 收到信号 %s，准备清理", signum)
            try:
                app.quit()
            except Exception:
                _graceful_shutdown(exit_now=True)
        for _s in (_sig.SIGINT, _sig.SIGTERM):
            try:
                _sig.signal(_s, _on_sig)
            except Exception:  # noqa
                pass
    except Exception:  # noqa
        pass
    _rc = app.exec()
    _graceful_shutdown(exit_now=True)   # 不会返回
    sys.exit(_rc)


def _attach_radio_async() -> None:
    """后台装配 AIRadioController → 接入 webapi.radio_bridge。

    ⚠ 与 ui/bridge_host.py::_start_radio_async 同构：装配在工作线程，
    用 QTimer（天然 GUI 线程）轮询结果，避免跨线程碰 QObject。
    """
    from PySide6.QtCore import QTimer

    state = {"done": False, "radio": None, "err": None}

    def _run():
        try:
            from ui.bridge_host import _init_radio
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
            logger.warning("AIRadioController 装配失败，API 将保持未接入状态：%s", err)
            return
        try:
            from webapi.radio_bridge import bridge
            ok = bridge.attach(radio)
            logger.info("AIRadioController 已接入 Web API（ok=%s）", ok)
        except Exception as e:  # noqa
            logger.warning("接入 Web API 失败: %s", e)

    QTimer.singleShot(300, _poll)


if __name__ == "__main__":
    main(full=True)
