"""
主窗口：组合所有控件与控制器，构成可交互的 AI 电台界面。
布局：顶部工具栏（Logo + 口播开关 + 电台模式 + 风格滑块 + 导入 + 桌面列表 + 设置 + 关于）
 + 左侧导航（推荐 / 曲库 / 播放历史）+ 中心堆叠 + 右侧信息面板 + 底部播放条。
"""
from __future__ import annotations

import sys
import os
import logging
import threading
import time
from pathlib import Path

# 品味画像 JSON 的标识性键名。若某段"口播/推荐理由"文本命中多个，说明模型把画像
# 当成了口播回显——这类内容不应出现在 DJ 口播面板，需过滤掉。
_TASTE_MARKERS = ("显性偏好", "隐性模式", "隐性倾向", "情感需求",
                  "品味关键词", "推荐策略", "听歌场景")


def _looks_like_taste_profile(text: str) -> bool:
    """判断一段文本是否其实是「品味画像」而非真正的口播/推荐理由。"""
    if not text:
        return False
    stripped = text.strip()
    if stripped.startswith("{") and ("显性偏好" in text or "品味关键词" in text):
        return True
    return sum(1 for m in _TASTE_MARKERS if m in text) >= 3

from PySide6.QtCore import Qt, QTimer, Signal, QObject, Slot
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QComboBox, QListWidget, QListWidgetItem,
    QStackedWidget, QMenu, QMessageBox, QFileDialog, QSlider, QFrame,
    QTabWidget, QStatusBar,
)

from config.settings import user_config, RadioConfig
from data.sources import SourceManager
from data.db import Database
from data.library import MusicLibrary, AUDIO_EXT
from core.player import PlayerEngine
from core.tts import TTSEngine
from ai.brain import AIBrain
from ai.recommend import RoamEngine
from ai.radio import AIRadioController
from ui.widgets import (
    PlayerControls, NowPlaying, LyricsView, BroadcastDialog,
    QueueListWidget, DesktopQueueWindow, LibraryWidget, HistoryListWidget,
    RecoListWidget, fav_key,
)
from ui.search_widget import SearchWidget
from ui.widgets import PlaylistCenterWidget
from ui.settings_dialog import SettingsDialog
from tools.websearch_service import launcher as websearch_launcher
from tools import qq_api_launcher
from utils.logger import setup_logger
from core.ui_plugin import UIManager, CLASSIC_KEY

logger = setup_logger()


class _MainThreadBridge(QObject):
    """把后台线程里需要更新 UI 的调用安全地投递到主线程执行。

    例如音源搜索 / 音乐盒导入在后台线程完成网络与入库后，其 UI 收尾
    （刷新曲库列表、写口播消息）必须回到主线程，否则会跨线程操作 QWidget。
    """
    _sig = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._sig.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, fn) -> None:
        fn()


class _LevelSlider(QWidget):
    """四档风格滑块。"""
    changed = Signal(int)

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self._label = QLabel(label)
        self._state = QLabel(RadioConfig.FAMILIARITY_LABELS[0])
        self._state.setObjectName("Sub")
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 3)
        self._slider.setFixedWidth(120)
        self._slider.setValue(0)
        self._slider.valueChanged.connect(self._on_change)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self._label)
        lay.addWidget(self._slider)
        lay.addWidget(self._state)

    def _on_change(self, v):
        self._state.setText(RadioConfig.FAMILIARITY_LABELS[max(0, min(3, v))])
        self.changed.emit(v)

    def set_value(self, v: int):
        self._slider.blockSignals(True)
        self._slider.setValue(max(0, min(3, v)))
        self._state.setText(RadioConfig.FAMILIARITY_LABELS[max(0, min(3, v))])
        self._slider.blockSignals(False)


class MainWindow(QMainWindow):
    apply_done = Signal()  # 设置重配置（重活）在后台线程完成后回调主线程刷新 UI
    toast_requested = Signal(str)  # UI 无关的系统提示（经典=口播栏；液态玻璃舱=toast）

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MusicAI · AI 智能电台")
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self._last_entries: list[dict] = []
        self._cockpit = None  # 当前主界面插件实例（经典界面为 None）
        self._bridge = _MainThreadBridge()  # 后台线程 UI 回调统一投递到主线程
        self._build_backend()
        self._build_active_ui()
        QTimer.singleShot(0, self._init_library)

    # ---------------- 主界面渲染（插件化） ----------------
    def _build_active_ui(self) -> None:
        """按配置的 main_ui 渲染中央界面：插件优先，经典界面回退。

        经典界面（classic）是内置回退，直接构建；其余（如 liquid_glass 液态玻璃舱）
        为 plugins/ui/ 下的可插拔主界面，由 UIManager 发现并通过 build(host) 构建。
        """
        self._cockpit = None
        try:
            mgr = UIManager()
            if mgr.active_is_plugin:
                plugin = mgr.get_ui(mgr.active_key)
                if plugin is not None:
                    cockpit = plugin.build(self)
                    if cockpit is not None:
                        self.setCentralWidget(cockpit)
                        self._cockpit = cockpit
                        # 系统提示 -> 舱内 toast
                        self.toast_requested.connect(cockpit.push_toast)
                        # 液态玻璃舱针对「最大化网页」设计：启动即最大化，铺满整屏画布
                        self.showMaximized()
                        return
            # 经典界面或插件缺失/失败 -> 内置经典
            self._build_ui()
            self._wire()
            self.toast_requested.connect(
                lambda m: self.broadcast.add_message("系统", m))
        except Exception as e:  # noqa
            logger.warning("主界面插件加载失败，回退经典界面：%s", e)
            try:
                self._build_ui()
                self._wire()
                self.toast_requested.connect(
                    lambda m: self.broadcast.add_message("系统", m))
            except Exception:  # noqa
                pass

    # ---------------- 后端 ----------------
    def _build_backend(self):
        self.db = Database()
        self.library = MusicLibrary(self.db)
        self.player = PlayerEngine()
        self.tts = TTSEngine()
        self.brain = AIBrain()
        self.source_manager = SourceManager(user_config.data)
        self.recommend = RoamEngine(self.brain, self.library.tracks)
        self.radio = AIRadioController(
            self.player, self.tts, self.brain, self.library, self.db, self.recommend)
        self.radio.source_manager = self.source_manager
        # 应用启动即清空 LLM/DJ 上下文（最近播放、口播时序等），从干净状态开始。
        self.radio.reset_context()
        # 启动即后台预热 Ollama 模型（载入显存约十几秒），消掉首次推荐的冷启动卡顿。
        threading.Thread(target=self.radio.warmup_llm, daemon=True).start()
        # 启动即后台预热「当前生效的 TTS 引擎」（2026-11-17 改为按需：只暖要用的那个）。
        # 旧行为是无条件同时预热 VOICEVOX + MeloTTS，即便生效引擎是 edge/indextts 也照拉，
        # 白白多一个常驻进程 + 占显存。现在统一走 tts.warm_active()：
        #   indextts → 拉起 3.11 worker 并载模型；melotts → 载语言模型；voicevox → 拉起 headless。
        # 预处理失败仅告警，不影响主流程（合成时引擎会自行再尝试/回退）。
        threading.Thread(target=self.tts.warm_active, daemon=True,
                         name="tts-boot").start()
        # 浏览器内核联网搜索服务：search.mode=="browser" 时静默后台拉起（替代 DDG）。
        # 端口已占用则视为已有实例自动跳过；本机无 Chrome 则服务自行 503、
        # 口播降级无联网资料，不拖垮主程序。失败时仅告警，不影响启动。
        websearch_launcher.autostart_if_browser()
        # QQ 音乐 API 服务（qq-music-api，127.0.0.1:3200）：auto_start 时静默后台拉起，
        # 保证「扫码登录 QQ音乐」按钮开箱即用、不再报连接被拒（WinError 10061）。
        # 端口已占用（手动实例 / 上一轮会话）则自动跳过；失败时仅告警，不影响启动。
        qq_api_launcher.autostart()

        # 配置热重载：config.json 变化时自动 reconfigure（无需重启）
        from utils.config_watcher import ConfigWatcher
        self._cfg_watcher = ConfigWatcher()
        self._cfg_watcher.add(self._on_config_change)
        self._cfg_watcher.start()
        self.apply_done.connect(self._on_apply_done)

    # ---------------- 配置热重载 ----------------
    def _on_config_change(self) -> None:
        """config.json 变化时：重新读取并让各模块热重载（无需重启）。"""
        try:
            user_config.reload()
            self.tts.reconfigure()
            self.brain.reconfigure()
            try:
                from ai.search import rebuild_search_provider
                rebuild_search_provider()
            except Exception:  # noqa
                pass
            # 若运行期把 search.mode 切到 browser，确保搜索服务已静默后台拉起
            try:
                websearch_launcher.autostart_if_browser()
            except Exception:  # noqa
                pass
            logging.getLogger("MusicAI").info(
                "[配置热重载] TTS / LLM / 检索 已重新配置")
        except Exception as e:  # noqa
            logging.getLogger("MusicAI").warning("配置热重载失败: %s", e)

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(10)

        # ===== 顶部工具栏 =====
        top = QHBoxLayout()
        logo = QLabel("🎵 MusicAI · AI 智能电台")
        logo.setObjectName("HeaderLabel")
        top.addWidget(logo)
        top.addStretch()

        # 口播开关（DJ 导听）：关闭则纯音乐、不生成/不播放口播
        self.dj_btn = QPushButton("🎙 口播 DJ")
        self.dj_btn.setObjectName("ToolButton")
        self.dj_btn.setCheckable(True)
        self.dj_btn.setChecked(True)
        # 电台模式（自动续推）：关闭则 5 首放完即止，不自动续推推荐列表
        self.roam_btn = QPushButton("📻 电台模式")
        self.roam_btn.setObjectName("ToolButton")
        self.roam_btn.setCheckable(True)
        self.roam_btn.setChecked(True)
        # 推荐偏好滑块（4 档：更熟悉 ↔ 更陌生）—— 关键推荐变量，持久化于 config
        self.fam_slider = _LevelSlider("推荐偏好")
        # 导入
        self.import_btn = QPushButton("📚 导入音乐")
        self.import_btn.setObjectName("ToolButton")
        import_menu = QMenu(self)
        import_menu.addAction("＋ 添加文件…", self._pick_files)
        import_menu.addAction("＋ 添加文件夹…", self._pick_folder)
        self.import_btn.setMenu(import_menu)
        # 桌面列表（右上角开闭）
        self.desktop_btn = QPushButton("🖥 桌面列表")
        self.desktop_btn.setObjectName("ToolButton")
        self.desktop_btn.setCheckable(True)
        self.desktop_btn.setChecked(True)
        self.settings_btn = QPushButton("⚙ 设置")
        self.settings_btn.setObjectName("ToolButton")
        self.about_btn = QPushButton("ℹ 关于")
        self.about_btn.setObjectName("ToolButton")

        top.addWidget(self.dj_btn)
        top.addWidget(self.roam_btn)
        top.addWidget(self.fam_slider)
        top.addWidget(self.import_btn)
        top.addWidget(self.desktop_btn)
        top.addWidget(self.settings_btn)
        top.addWidget(self.about_btn)
        v.addLayout(top)

        # ===== 主体 =====
        body = QHBoxLayout()
        v.addLayout(body, stretch=1)

        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setMaximumWidth(180)
        self.nav.setMinimumWidth(150)
        body.addWidget(self.nav)

        self.stack = QStackedWidget()

        # 推荐（AI 货架）：AI 常驻"思考"的待荐列表，不自动播；点播放才整批进播放列表
        self.recommend_page = QWidget()
        rp = QVBoxLayout(self.recommend_page)
        rp.setContentsMargins(0, 0, 0, 0)
        rp_top = QHBoxLayout()
        rp_title = QLabel("📋 推荐（AI 智能电台）")
        rp_title.setObjectName("HeaderLabel")
        rp_top.addWidget(rp_title)
        rp_top.addStretch()
        self.reco_playall_btn = QPushButton("▶ 播放全部")
        self.reco_playall_btn.setObjectName("AddBtn")
        rp_top.addWidget(self.reco_playall_btn)
        rp.addLayout(rp_top)
        self.reco_loading = QLabel("推荐在加载中请稍等…")
        self.reco_loading.setObjectName("Sub")
        self.reco_loading.hide()
        rp.addWidget(self.reco_loading)
        self.reco_list = RecoListWidget()
        rp.addWidget(self.reco_list, stretch=1)
        self.stack.addWidget(self.recommend_page)

        # 曲库
        self.library_page = QWidget()
        lp = QVBoxLayout(self.library_page)
        lp.setContentsMargins(0, 0, 0, 0)
        self.lib_view = LibraryWidget(source_manager=self.source_manager)
        lp.addWidget(self.lib_view)
        self.stack.addWidget(self.library_page)

        # 播放历史
        self.history_page = QWidget()
        hp = QVBoxLayout(self.history_page)
        hp.setContentsMargins(0, 0, 0, 0)
        hp_title = QLabel("🕘 播放历史")
        hp_title.setObjectName("HeaderLabel")
        self.history_list = HistoryListWidget()
        hp.addWidget(hp_title)
        hp.addWidget(self.history_list, stretch=1)
        self.stack.addWidget(self.history_page)

        # 播放列表（队列）：当前曲 + 待播，唯一真正播放的源
        self.queue_page = QWidget()
        qp = QVBoxLayout(self.queue_page)
        qp.setContentsMargins(0, 0, 0, 0)
        qp_title = QLabel("🎚 播放列表（队列）")
        qp_title.setObjectName("HeaderLabel")
        qp.addWidget(qp_title)
        self.queue_list = QueueListWidget()
        qp.addWidget(self.queue_list, stretch=1)
        self.stack.addWidget(self.queue_page)

        # 搜歌（搜索内核 + 内嵌浏览器）：搜索网易云 + 结果页看资料/歌词 + 双击即播
        self.search_page = QWidget()
        sp = QVBoxLayout(self.search_page)
        sp.setContentsMargins(0, 0, 0, 0)
        self.search_view = SearchWidget(self.source_manager)
        sp.addWidget(self.search_view)
        self.stack.addWidget(self.search_page)

        # 歌单中心：网易云 / QQ音乐 歌单浏览与收藏 + 本地自建歌单统一管理
        self.playlist_page = QWidget()
        plp = QVBoxLayout(self.playlist_page)
        plp.setContentsMargins(0, 0, 0, 0)
        self.playlist_center = PlaylistCenterWidget(self.source_manager, self.db)
        self.playlist_center.status.connect(
            lambda m: self.status_bar.showMessage(m, 5000))
        plp.addWidget(self.playlist_center)
        self.stack.addWidget(self.playlist_page)
        self._playlist_loaded = False

        body.addWidget(self.stack, stretch=1)

        # ===== 右侧信息面板 =====
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setSpacing(8)
        self.now = NowPlaying()
        self.lyrics = LyricsView()
        self.broadcast = BroadcastDialog()
        self.controls = PlayerControls()

        # R25：「🎯 随机一首」按钮已整体移除（前端 + 后端入口全部去掉）
        roam_row = QHBoxLayout()
        roam_row.addStretch()

        right_layout.addWidget(self.now)
        right_layout.addWidget(QLabel("📜 歌词"), alignment=Qt.AlignLeft)
        right_layout.addWidget(self.lyrics, stretch=1)
        right_layout.addWidget(QLabel("🎙 AI 口播"), alignment=Qt.AlignLeft)
        right_layout.addWidget(self.broadcast, stretch=1)
        self.thinking = QLabel("🤔 AI 正在思考口播…")
        self.thinking.setObjectName("Sub")
        self.thinking.hide()
        right_layout.addWidget(self.thinking)
        right_layout.addLayout(roam_row)
        body.addWidget(right, stretch=1)

        # ===== 底部播放条 =====
        v.addWidget(self.controls)

        # ===== 底部状态栏：AI / 联网 / 上下文状态总览 =====
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_base = ""
        self._refresh_status_bar()

        # ===== 左侧导航项 =====
        self._add_nav("📋 推荐", 0)
        self._add_nav("💿 曲库", 1)
        self._add_nav("🕘 播放历史", 2)
        self._add_nav("🎚 播放列表", 3)
        self._add_nav("🔍 搜歌", 4)
        self._add_nav("🎼 歌单中心", 5)
        self.nav.setCurrentRow(0)

        # ===== 桌面悬浮窗（推荐列表，默认显示） =====
        self.desktop = DesktopQueueWindow(self)
        sg = self.screen().availableGeometry() if self.screen() else None
        if sg is not None:
            self.desktop.move(sg.right() - 340, sg.top() + 80)
        else:
            self.desktop.move(900, 120)
        self.desktop.show()

    def _add_nav(self, name: str, index: int) -> None:
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, index)
        self.nav.addItem(item)

    # ---------------- 连线 ----------------
    def _wire(self):
        c = self.radio
        c.on("track_changed", self._on_track_changed)
        c.on("playing", self.controls.set_playing)
        c.on("thinking", self.thinking.setVisible)
        c.on("dj_speech", lambda s: self._add_dj_text(s))
        c.on("dj_timing", self._on_dj_timing)
        c.on("progress", self.controls.set_progress)
        c.on("recommend_reason",
             lambda r: self._add_dj_text("🎧 推荐理由：" + r)
             if not _looks_like_taste_profile(r) else None)
        c.on("lyrics", self.lyrics.set_lyrics)
        c.on("lyric_line", lambda idx, _: self.lyrics.highlight(idx))
        c.on("favorite", lambda f: None)
        c.on("playlist_updated", self._on_playlist_updated)
        c.on("history_changed", self._on_history_changed)
        c.on("library_changed", self._on_library_changed)
        # 推荐货架（与播放队列解耦）：AI 常驻思考、不自动播
        c.on("reco_updated", self._on_reco_updated)
        c.on("reco_loading", self._on_reco_loading)

        self.controls.play_toggled.connect(c.toggle_play)
        self.controls.prev_clicked.connect(c.prev)
        self.controls.next_clicked.connect(c.next)
        self.controls.seek_requested.connect(c.seek)
        self.controls.volume_changed.connect(c.set_volume)

        self.now.favorite_toggled.connect(self._toggle_fav)
        self.settings_btn.clicked.connect(self._open_settings)
        self.about_btn.clicked.connect(self._on_about)
        self.nav.currentRowChanged.connect(self._on_nav_row)

        # 顶部开关：口播 DJ 开关 ⇒ dj_on；电台模式 ⇒ 自动续推 roam_on
        self.dj_btn.toggled.connect(self._toggle_dj)
        self.roam_btn.toggled.connect(self._toggle_roam)
        self.fam_slider.changed.connect(c.set_familiarity)
        # 初始化滑块到用户持久化的推荐偏好（重启后保留上次设置）
        self.fam_slider.set_value(self.radio.familiarity)
        self.desktop_btn.toggled.connect(lambda v: self.desktop.setVisible(v))
        self.desktop.hide_btn.clicked.connect(
            lambda: (self.desktop.hide(), self.desktop_btn.setChecked(False)))

        # 推荐列表（主窗口 + 桌面悬浮窗）
        self.queue_list.favorite_clicked.connect(
            lambda tid, on: c.favorite_by_id(tid, on))
        self.queue_list.remove_clicked.connect(c.remove_from_queue)
        self.queue_list.dislike_clicked.connect(
            lambda tid, on: c.dislike_by_id(tid, on))
        self.queue_list.row_activated.connect(self._on_queue_row_activated)
        self.desktop.favorite_clicked.connect(
            lambda tid, on: c.favorite_by_id(tid, on))
        self.desktop.remove_clicked.connect(c.remove_from_queue)
        self.desktop.dislike_clicked.connect(
            lambda tid, on: c.dislike_by_id(tid, on))
        self.desktop.row_activated.connect(self._on_queue_row_activated)

        # 推荐页（AI 货架）：点歌把整批货架搬入播放队列并播
        self.reco_list.play_requested.connect(c.play_from_reco)
        self.reco_list.favorite_clicked.connect(
            lambda tid, on: c.favorite_by_id(tid, on))
        self.reco_list.remove_clicked.connect(c.remove_from_reco)
        self.reco_list.dislike_clicked.connect(
            lambda tid, on: c.dislike_by_id(tid, on))
        self.reco_playall_btn.clicked.connect(c.play_all_reco)

        # 曲库
        self.lib_view.track_activated.connect(lambda tid: self._play_id(tid, intro=True))
        self.lib_view.play_next_clicked.connect(c.play_next)
        self.lib_view.add_to_reco_clicked.connect(c.add_to_recommend_list)
        self.lib_view.delete_clicked.connect(c.delete_from_library)
        self.lib_view.play_all_clicked.connect(c.play_all_library)
        self.lib_view.play_mode_changed.connect(c.set_play_mode)
        self.lib_view.undo_dislike_clicked.connect(
            lambda key: c.undo_dislike_by_key(key))
        self.lib_view.request_disliked.connect(self._show_disliked)

        # 播放历史
        self.history_list.favorite_clicked.connect(
            lambda tid, on: c.favorite_by_id(tid, on))
        self.history_list.remove_clicked.connect(c.remove_from_history)
        self.history_list.dislike_clicked.connect(
            lambda tid, on: c.dislike_by_id(tid, on))
        self.history_list.row_activated.connect(lambda tid: self._play_id(tid, intro=True))
        self.history_list.play_next_clicked.connect(c.play_next)

        # 搜歌面板：双击即播 / 下一首播放 / 加入曲库（走 netease:// 实时取链，不存死链）
        self.search_view.play_requested.connect(self._play_search)
        self.search_view.next_play_requested.connect(c.play_next)
        self.search_view.add_library_requested.connect(self._add_from_search)

        # 歌单中心：整批播放 / 单曲播放 / 首次打开懒加载
        self.playlist_center.play_tracks.connect(c.play_tracks)
        self.playlist_center.play_track.connect(
            lambda t: c.play_tracks([t], reason="歌单点播"))
        self.stack.currentChanged.connect(self._on_stack_changed)

    def _on_stack_changed(self, idx: int):
        if idx == 5 and not self._playlist_loaded:
            self._playlist_loaded = True
            self.playlist_center._load_grid()

    def _on_nav_row(self, row: int):
        item = self.nav.item(row)
        if item is None:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is not None:
            self.stack.setCurrentIndex(idx)

    # ---------------- 行为 ----------------
    def _init_library(self):
        self.radio.roam_on = True
        self.radio.dj_on = True
        # 经典界面：同步顶部开关勾选态（插件界面自行管理，跳过）
        if hasattr(self, "dj_btn"):
            self.dj_btn.blockSignals(True)
            self.dj_btn.setChecked(True)
            self.dj_btn.blockSignals(False)
        if hasattr(self, "roam_btn"):
            self.roam_btn.blockSignals(True)
            self.roam_btn.setChecked(True)
            self.roam_btn.blockSignals(False)

        self.library.reload()
        pruned = self.library.prune_missing()
        if pruned:
            logger.info("启动清理了 %d 条缺失文件记录", pruned)
        # 确保本地音乐全部进入曲库（我喜欢的音乐）
        self.library.favorite_all_local()
        # 一次性为「已入库但缺封面」的本地音乐补抽内嵌封面（后台线程，不阻塞 UI）。
        self._backfill_covers_async()
        if not self.library.tracks:
            self.toast_requested.emit(
                "曲库为空，点左下角齿轮 → 设置 → 导入音乐，添加本地音乐后 AI 会为你推荐。")
            n = self.library.scan()
            if n > 0:
                self.toast_requested.emit(
                    f"已从本地目录自动扫描入库 {n} 首（已自动红心点亮）。")
        self.recommend.refresh(self.library.tracks)
        self.radio.mark_taste_dirty()
        # 打开软件：先提交 AI 品味分析并按品味推荐歌曲（歌曲先入推荐列表，纯 AI、不提前占位）。
        # 是否立即开播由 auto_play 决定——开播走 _play_id（会以点播曲开场），不开播则仅
        # 填充推荐列表（待 AI 返回后可见，期间呈「生成中」）。第一首歌的口播在开播时由
        # start_radio 的 opening 路径给出「默认开场白」（地点+天气+温度+歌名）。
        if user_config.data.get("auto_play") and self.library.tracks:
            self._play_id(self.library.tracks[0].id, intro=True)
        else:
            self.radio.start_radio(auto_start=False)
        # 无论是否自动开播，都确保推荐货架（AI 常驻思考）被填充，推荐页始终有歌。
        self.radio.ensure_reco()
        # UI 无关：通过 radio 事件广播，由当前主界面（经典/液态玻璃）自行刷新
        self.radio._emit("library_changed", self.library.favorites())
        self.radio._emit("history_changed", self.radio.get_history())
        self._emit_initial_playlist()

    def _emit_initial_playlist(self):
        entries = self.radio.get_playlist_entries()
        self.radio._emit("playlist_updated", entries)

    def _backfill_covers_async(self) -> None:
        """后台线程补抽本地封面，完成后回主线程刷新曲库视图（避免启动卡顿）。"""
        def worker():
            try:
                n = self.library.backfill_local_covers()
            except Exception as e:  # noqa
                logger.warning("后台补封面失败: %s", e)
                return
            if n:
                self._to_main(lambda: self.radio._emit(
                    "library_changed", self.library.favorites()))
        threading.Thread(target=worker, daemon=True).start()

    def _refresh_library(self):
        self.lib_view.load_tracks(self.library.favorites())

    def _refresh_history(self):
        tracks = self.radio.get_history()
        fav_keys = {fav_key(t) for t in tracks if self.radio.is_favorite_track(t)}
        dislike_keys = {fav_key(t) for t in tracks if self.radio.is_disliked_track(t)}
        self.history_list.set_entries(tracks, fav_keys, dislike_keys)

    def _on_playlist_updated(self, entries: list[dict]) -> None:
        self._last_entries = entries
        self.desktop.set_entries(entries)
        self.queue_list.set_entries(entries)

    # ---------- 推荐货架 ----------
    def _on_reco_updated(self, entries: list[dict]) -> None:
        self._last_reco = entries
        self.reco_list.set_entries(entries)
        if entries:
            self.reco_loading.hide()
        else:
            # 推荐页暂无内容：显示占位提示（AI 仍在后台思考/拉取）
            self.reco_loading.setText("暂无可推荐歌曲，AI 正在思考…")
            self.reco_loading.show()

    def _on_reco_loading(self, loading: bool) -> None:
        if loading:
            self.reco_loading.setText("推荐在加载中请稍等…")
            self.reco_loading.show()

    def _on_history_changed(self, tracks: list) -> None:
        fav_keys = {fav_key(t) for t in tracks if self.radio.is_favorite_track(t)}
        dislike_keys = {fav_key(t) for t in tracks if self.radio.is_disliked_track(t)}
        self.history_list.set_entries(tracks, fav_keys, dislike_keys)

    def _on_library_changed(self, tracks: list) -> None:
        self.lib_view.load_tracks(tracks)
        # 同步"不喜欢的歌曲"隐藏库计数；若当前正处于该隐藏视图则刷新列表
        self.lib_view.set_disliked_count(len(self.radio.disliked_keys))
        if self.lib_view.is_disliked_mode():
            self.lib_view.show_disliked(self.radio.get_disliked_tracks())

    def _show_disliked(self) -> None:
        self.lib_view.show_disliked(self.radio.get_disliked_tracks())

    def _on_queue_row_activated(self, tid: int) -> None:
        # 双击推荐列表某首：跳到该位置（前面的出列并补足）
        for i, e in enumerate(self._last_entries):
            if e["id"] == tid:
                self.radio.jump_to(i)
                return

    def _play_id(self, tid, intro=True):
        # 双击/连点去抖：极短间隔内的重复点播只采纳一次，避免反复全量重启电台拖垮 UI。
        now = time.monotonic()
        if now - getattr(self, "_last_play_ts", 0.0) < 0.5:
            return
        self._last_play_ts = now
        t = self.library.by_id(tid)
        if t:
            self.radio.start_radio(seed_track=t)

    def _play_search(self, t):
        """搜歌结果双击/单击播放：结果是 netease:// 占位 Track，未入曲库，
        直接以它作开场曲开播（start_radio 会实时取链、自动 upsert）。"""
        now = time.monotonic()
        if now - getattr(self, "_last_search_play_ts", 0.0) < 0.5:
            return
        self._last_search_play_ts = now
        self.radio.start_radio(seed_track=t)

    def _add_from_search(self, t):
        """把搜到的联网曲目加入曲库（我喜欢的音乐），按 音源+歌名+歌手 去重。"""
        if self.library.add_remote_track(t):
            self._refresh_library()
            self.broadcast.add_message(
                "系统", f"🎶 已加入曲库：《{t.title}》 - {t.artist or '未知'}")
        else:
            self.broadcast.add_message("系统", f"《{t.title}》已在曲库中。")

    def _fetch_cover(self, track) -> None:
        """后台取专辑封面，下载后回主线程设到 NowPlaying。

        封面归属用 track.id 作 token；若下载期间已切歌，set_cover 会因 token
        不符丢弃过期图，避免串台显示上一首封面。
        优先用曲目自带封面 URL（在线为 http(s)、本地为 file:// 内嵌封面缓存，
        均经 fetch_cover_bytes 统一处理）；本地歌无在线直链但有内嵌封面时也读盘显示，
        仅当既无封面 URL、又无法按 ext_id 取在线封面时才放弃。
        """
        if not track:
            return
        url = getattr(track, "cover", None)
        # 本地歌/无 ext_id 的曲目：没有封面 URL 就确实取不到，直接放弃；
        # 但有 file:// 内嵌封面（url 已取到）时仍要读盘显示，不能在此 return。
        if not url and (track.source in ("local", "", None) or track.ext_id is None):
            return
        token = self._cover_token_for(track)
        self.now._cover_token = token
        # 优先曲目自带封面 URL；在线曲目若搜索时没带封面，再让音源按需取。
        if not url:
            src = self.source_manager.get(track.source)
            if src is not None and hasattr(src, "album_cover"):
                try:
                    url = src.album_cover(track.ext_id)
                except Exception:  # noqa
                    url = None
        if not url:
            return

        def worker():
            from utils.cover_cache import fetch_cover_bytes
            data = fetch_cover_bytes(url, getattr(track, "source", None))
            if data:
                self._to_main(lambda: self.now.set_cover(data, token))
        threading.Thread(target=worker, daemon=True).start()

    def _toggle_fav(self, want_fav):
        if self.radio.current is None:
            return
        self.radio.toggle_favorite(self.radio.current)
        cur = self.radio.current
        # 点收藏只改红心状态：set_track 在 token 不变时会保留已显示的封面，
        # 这里再补一次封面拉取，确保（即便封面尚在加载中）收藏后封面一定回来，
        # 不会停在 🎵。
        # 红心状态读内存（radio._queue_fav），与收藏持久化（后台线程）解耦，
        # 避免点红心时同步读 DB 造成卡顿。
        self.now.set_track(
            cur.title,
            f"{cur.artist} · {cur.album}",
            self.radio.is_favorite_track(cur),
            cover_token=self._cover_token_for(cur))
        self._fetch_cover(cur)

    @staticmethod
    def _cover_token_for(track) -> str | None:
        """NowPlaying 封面归属 token：用 track.id（唯一且覆盖本地/在线），
        避免本地歌 ext_id 为 None 时多首歌 token 撞车导致串台。"""
        return str(track.id) if getattr(track, "id", None) is not None else None

    def _to_main(self, fn) -> None:
        """若当前在后台线程，把 fn 投递到主线程执行；否则直接调用。

        用于后台线程（音源搜索 / 音乐盒导入）完成网络与入库后，把刷新
        曲库列表、写口播消息等 UI 操作安全地切回主线程。
        """
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self._bridge._sig.emit(fn)

    def _toggle_dj(self, on):
        # 口播 DJ 开关：仅门控 TTS 音频输出与口播文字显示；AI 选歌/口播文案生成
        # （"思考"）始终运行，推荐页常驻有歌。关闭 = 纯音乐（不出声、不显字）。
        self.radio.set_dj_on(on)
        self.broadcast.add_message(
            "系统", "🎙 口播 DJ：" + ("已开启" if on
                                     else "已关闭（纯音乐；AI 仍持续思考/推荐）"))

    def _toggle_roam(self, on):
        # 电台模式（自动续推）：关闭则推荐列表放完即止，不自动续推。
        self.radio.set_roam_on(on)
        self.broadcast.add_message(
            "系统", "📻 电台模式（自动续推）：" + ("已开启" if on else "已关闭（放完即止）"))

    def _open_settings(self):
        # 屏蔽配置监视器：设置对话框保存时会改写 config.json，若任由监视器再触发一次
        # 全量重配，会与下方 _apply_settings 重复且卡 UI。对话框期间暂停，关闭后恢复。
        # 注：ConfigWatcher 是轮询型普通类（非 QObject），用 pause()/resume() 而非 blockSignals。
        if getattr(self, "_cfg_watcher", None) is not None:
            self._cfg_watcher.pause()
        dlg = SettingsDialog(self, self.player)
        dlg.applied.connect(self._apply_settings)
        dlg.play_requested.connect(lambda p: self.player.play_sound(p))
        dlg.exec()
        if getattr(self, "_cfg_watcher", None) is not None:
            self._cfg_watcher.resume()

    def _apply_settings(self):
        user_config.reload()
        # 设置页若改了「推荐偏好」，同步到电台控制器与顶部滑块
        self.radio.set_familiarity(user_config.data.get("rec_pref", 0))
        if hasattr(self, "fam_slider"):
            self.fam_slider.set_value(self.radio.familiarity)
        # 轻量重配：立即在主线程完成（快、不卡）
        self.brain.reconfigure()
        self.tts.reconfigure()
        self._refresh_status_bar()
        # 重活（扫库 / 重建音源 / 刷新推荐 / 重分析品味 / 刷 UI）移到后台线程，
        # 让设置对话框即时关闭、不再"卡一下"。完成后由 apply_done 信号回主线程收尾。
        threading.Thread(target=self._apply_settings_bg, daemon=True).start()

    def _apply_settings_bg(self) -> None:
        try:
            self.source_manager.rebuild(user_config.data)
            self.radio.source_manager = self.source_manager
            self.library.scan()
            self.recommend.refresh(self.library.tracks)
            self.radio.mark_taste_dirty()
            # 网易云音乐盒：开启且填了 uid 时自动导入"我喜欢的音乐"
            ncm = user_config.data.get("sources", {}).get("netease", {})
            if ncm.get("enabled") and ncm.get("musicbox_uid"):
                ncm_src = self.source_manager.get("netease")
                if ncm_src:
                    uid = ncm["musicbox_uid"]
                    try:
                        self._auto_import_musicbox(ncm_src, uid)
                    except Exception:  # noqa
                        pass
        except Exception as e:  # noqa
            logging.getLogger("MusicAI").warning("设置重配置（后台）失败: %s", e)
        finally:
            # 回到主线程刷新 UI（信号跨线程安全投递到主线程事件循环）
            self.apply_done.emit()

    def _on_apply_done(self) -> None:
        if hasattr(self, "lib_view"):
            self._refresh_library()
        if self.brain.enabled:
            self.toast_requested.emit("✅ AI 已启用，口播由大模型生成。")
        else:
            self.toast_requested.emit("⚠️ 未配置 AI，口播使用离线模板。")

    # ---------------- 状态栏 ----------------
    def _refresh_status_bar(self) -> None:
        """刷新底部状态栏：AI 状态 + 联网搜索状态（及后端）+ 上下文窗口。"""
        a = user_config.ai
        backend = (a.get("backend", "api") or "api").lower()
        if getattr(self, "brain", None) and self.brain.enabled:
            ai_text = "🤖 AI 已启用"
        else:
            ai_text = "📴 AI 未配置（离线模板）"
        ws = bool(a.get("web_search", False))
        if ws:
            net_text = ("🌐 联网：开（DuckDuckGo）" if backend == "ollama"
                        else "🌐 联网：开（内置工具）")
        else:
            net_text = "🔌 联网：关（纯本地实时）"
        cw = int(a.get("context_window", 3))
        cw_text = "🧠 上下文：纯实时" if cw <= 0 else f"🧠 上下文：最近 {cw} 首"
        self._status_base = f"{ai_text}   ·   {net_text}   ·   {cw_text}"
        if hasattr(self, "status_bar"):
            self.status_bar.showMessage(self._status_base)

    def _on_dj_timing(self, info: dict) -> None:
        """口播计时事件：若本次口播真的走了联网检索，在状态栏短暂高亮提示。"""
        if isinstance(info, dict) and info.get("search"):
            self._status_flash("🌐 本次口播已联网检索（DuckDuckGo）")

    def _status_flash(self, msg: str) -> None:
        """在不丢失常驻状态的前提下，短暂展示一条临时状态。"""
        if not hasattr(self, "status_bar"):
            return
        self.status_bar.showMessage(f"{self._status_base}   ·   {msg}")
        QTimer.singleShot(4000, lambda: self.status_bar.showMessage(self._status_base))

    def _auto_import_musicbox(self, src, uid: str) -> None:
        """后台导入网易云音乐盒（我喜欢的音乐）到曲库。"""
        try:
            tracks = src.musicbox(uid)
            if not tracks:
                self._to_main(lambda: self.broadcast.add_message(
                    "系统", "音乐盒为空或导入失败（检查 uid / 服务地址）。"))
                return
            added = 0
            for t in tracks:
                if self.library.add_remote_track(t):
                    added += 1
            self.library.reload()
            self.recommend.refresh(self.library.tracks)
            self.radio.mark_taste_dirty()
            # 以下 UI 收尾必须回主线程
            self._to_main(lambda: self._finish_musicbox_import(added))
        except Exception as e:  # noqa
            logger.warning("音乐盒自动导入失败: %s", e)
            self._to_main(lambda: self.broadcast.add_message(
                "系统", f"⚠️ 音乐盒导入失败：{e}"))

    def _finish_musicbox_import(self, added: int) -> None:
        self._refresh_library()
        self.broadcast.add_message("系统", f"🎶 音乐盒自动导入 {added} 首到曲库。")

    def _on_about(self):
        QMessageBox.about(
            self, "关于 MusicAI",
            "MusicAI · AI 智能电台音乐软件\n\n"
            "· PySide6 界面（浅色清爽风格）\n"
            "· pygame.mixer 双通道音频引擎 + 音量 Ducking\n"
            "· edge-tts AI 语音口播（可关）\n"
            "· LLM 实时生成口播 + 大模型选歌推荐\n"
            "· 曲库（我喜欢的音乐）/ 推荐列表 / 播放历史\n"
            "· 网易云 / QQ音乐 作为播放直链源（设置里配置）\n",
        )

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择音乐文件", "",
            "音频文件 (*.mp3 *.flac *.m4a *.ogg *.wav *.wma *.aac);;所有文件 (*.*)")
        if files:
            self._add_local_paths(files)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择音乐文件夹")
        if folder:
            self._add_local_folder(folder)

    def _add_local_paths(self, paths: list[str]) -> None:
        n = self.library.add_local_files(paths)
        self.recommend.refresh(self.library.tracks)
        self.radio.mark_taste_dirty()
        self._refresh_library()
        self.broadcast.add_message("系统", f"已添加 {n} 首到本地曲库（已自动红心点亮）。")

    def _add_local_folder(self, folder: str) -> None:
        n = self.library.add_local_folder(folder)
        self.recommend.refresh(self.library.tracks)
        self.radio.mark_taste_dirty()
        self._refresh_library()
        self.broadcast.add_message("系统", f"已从文件夹「{folder}」添加 {n} 首（已自动红心点亮）。")

    def _add_dj_text(self, text: str) -> None:
        """把一段 DJ 文本写入口播面板；若是误入的品味画像则丢弃。"""
        if not text or not text.strip():
            return
        if _looks_like_taste_profile(text):
            return
        self.broadcast.add_message("DJ", text)

    def _on_track_changed(self, track):
        fav = self.db.is_favorite(track.id)
        sub = f"{track.artist} · {track.album}"
        self.now.set_track(track.title, sub, fav,
                           cover_token=self._cover_token_for(track))
        self.controls.set_song_info(track.title, sub)
        self.controls.set_total(int(track.duration * 1000))
        self.controls.set_volume(self.player.volume)
        self._fetch_cover(track)

    def closeEvent(self, event):
        # 应用退出时关闭由本程序自动拉起的浏览器内核搜索服务子进程（不残留挂起）。
        try:
            websearch_launcher.stop_service()
        except Exception:  # noqa
            pass
        # 应用退出时关闭由本程序自动拉起的 qq-music-api 子进程（仅回收本程序拉的，
        # 用户手动 start-qq-api.bat 起的实例 _PROC 为 None，不会被误杀）。
        try:
            qq_api_launcher.stop_service()
        except Exception:  # noqa
            pass
        # 应用退出时关闭由本程序自动拉起的 VOICEVOX 引擎子进程（不残留挂起）。
        try:
            from core.tts_voicevox import stop_voicevox_engine
            stop_voicevox_engine()
        except Exception:  # noqa
            pass
        self.player.shutdown()
        self.db.close()
        super().closeEvent(event)

    # ---------------- 拖拽导入 ----------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if not urls:
            return
        files, folders = [], []
        for u in urls:
            p = u.toLocalFile()
            if not p:
                continue
            if os.path.isdir(p):
                folders.append(p)
            elif os.path.isfile(p) and Path(p).suffix.lower() in AUDIO_EXT:
                files.append(p)
        if files:
            self._add_local_paths(files)
        for f in folders:
            self._add_local_folder(f)
        if files or folders:
            event.acceptProposedAction()
        else:
            event.ignore()


def run() -> None:
    app = QApplication(sys.argv)
    # 全局中文字体：避免中文（歌名 / 提示 / Toast）显示成豆腐块（口口口）。
    # 探测 Microsoft YaHei 等可用中文字族；找不到则保持系统默认。
    try:
        from PySide6.QtGui import QFontDatabase
        _fams = QFontDatabase.families()
        _cjk = next((f for f in ("Microsoft YaHei", "Microsoft YaHei UI", "SimHei",
                                  "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC",
                                  "WenQuanYi Micro Hei", "Malgun Gothic") if f in _fams), "")
        if _cjk:
            app.setFont(QFont(_cjk, 10))
    except Exception:
        pass
    qss = Path(__file__).resolve().parent / "styles.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
