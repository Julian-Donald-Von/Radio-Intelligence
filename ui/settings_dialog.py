"""
设置对话框：在应用内配置
  - AI（大模型）API：base / key / model
  - TTS：音色 / 语速 / 音量（可测试）
  - 音源：网易云 / QQ音乐 / 本地的启用与参数

保存即写入 D:/MusicAI/data_runtime/config.json，并通过 applied 回调通知主窗口热更新。
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QFormLayout, QLineEdit, QCheckBox,
    QComboBox, QPushButton, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QSpinBox,
    QScrollArea, QGroupBox,
    QMessageBox, QSlider,
)
from PySide6.QtCore import Qt, QTimer, Signal, QUrl, QByteArray
from PySide6.QtGui import QImage, QPixmap

from config.settings import user_config, GeoConfig, RadioConfig, VOICES_DIR
from core.voice_manager import VoiceManager
from ai.prompts import (
    RECO_EXPLAIN_SYSTEM, TASTE_ANALYSIS_PROMPT,
    BROADCAST_SYSTEM, BROADCAST_OUTPUT_DEFAULT, TRANSITION_SYSTEM,
)
from utils.network_security import validate_api_base

logger = logging.getLogger("MusicAI.Settings")

# 常用中文 edge-tts 音色（云 TTS 主播音色，按「电台主播」风格标注）
EDGE_VOICES = [
    ("晓晓 · 温柔女主播（推荐）", "zh-CN-XiaoxiaoNeural"),
    ("云希 · 清爽男主播", "zh-CN-YunxiNeural"),
    ("云扬 · 沉稳新闻腔", "zh-CN-YunyangNeural"),
    ("晓伊 · 知性女声", "zh-CN-XiaoyiNeural"),
    ("云健 · 磁性男声", "zh-CN-YunjianNeural"),
    ("晓辰 · 甜美女声", "zh-CN-XiaochenNeural"),
    ("晓涵 · 活力女声", "zh-CN-XiaohanNeural"),
    ("晓梦 · 慵懒女声", "zh-CN-XiaomengNeural"),
    ("台湾晓辰 · 知性女声", "zh-TW-HsiaoChenNeural"),
    ("香港晓佳 · 明快女声", "zh-HK-HiuGaaiNeural"),
]
ZH_VOICES = [vid for _l, vid in EDGE_VOICES]  # 兼容旧引用（音色 id 列表）
# MeloTTS 内置说话人（多语；中文仅 1 种，英文含多种口音）。
# 更多「中文主播音色」可通过设置页拖拽导入自定义 .pth 模型获得。
MELOTTS_SPEAKERS = [
    ("中文 · 默认女声（内置）", "ZH"),
    ("英文 · 美式（内置）", "EN-US"),
    ("英文 · 英式（内置）", "EN-AU"),
    ("英文 · 印度（内置）", "EN_INDIA"),
    ("英文 · 巴西（内置）", "EN-BR"),
    ("英文 · 默认（内置）", "EN-Default"),
    ("西班牙语（内置）", "ES"),
    ("法语（内置）", "FR"),
    ("日语（内置）", "JP"),
    ("韩语（内置）", "KR"),
]


class DropArea(QLabel):
    """可拖拽接收文件的虚线框（用于导入 MeloTTS / ChatTTS 音色）。"""

    filesDropped = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setText("把 .pth / .npy / .pt 文件拖到这里")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(56)
        self.setStyleSheet(
            "border: 2px dashed #888; border-radius: 6px; padding: 8px; color: #888;")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()
RATE_OPTS = ["-50%", "-30%", "-10%", "+0%", "+10%", "+30%", "+50%"]
VOL_OPTS = ["-50%", "-30%", "-10%", "+0%", "+10%", "+30%"]

# 常见 OpenAI 兼容模型（下拉预设，也可手输自定义）
MODEL_CHOICES = [
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
    "claude-3-5-sonnet",
    "claude-3-haiku",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "deepseek-chat",
    "deepseek-reasoner",
    "qwen-max",
    "qwen-plus",
    "qwen-turbo",
    "moonshot-v1-8k",     # Kimi K2.7 短文本生成（8k 上下文）—— 实测可用、不触发风控
    "moonshot-v1-32k",    # Kimi K2.7 长文本生成（32k 上下文）
    # 注意：kimi-k2.6 实测会被供应商内容安全判定为 high risk（content_filter），
    # 故不再作为可选模型；如确需使用，请在 config.json 手动指定并自行承担风控风险。
    "yi-large",
    "llama-3.1-70b",
]
# 温度档位（下拉预设）。不同模型对温度的支持/默认值不同，交给用户选择。
TEMP_CHOICES = ["0.0", "0.3", "0.7", "0.8", "0.9", "1.0", "1.5", "2.0"]


class SettingsDialog(QDialog):
    applied = Signal()
    # 测试朗读：后台合成完成后，回主线程播放音频文件
    play_requested = Signal(str)
    test_status = Signal(str)
    # 测试 AI 连接结果（回主线程显示）
    ai_test_result = Signal(str)
    ncm_qr_ready = Signal(object)
    ncm_qr_result = Signal(object)
    ncm_qr_error = Signal(str)
    ncm_status_ready = Signal(str)
    # QQ音乐扫码登录
    qq_qr_ready = Signal(object)
    qq_qr_result = Signal(object)
    qq_qr_error = Signal(str)
    qq_status_ready = Signal(str)

    def __init__(self, parent=None, player=None) -> None:
        super().__init__(parent)
        self._player = player
        self._voice_mgr = VoiceManager(auto_watch=False)
        self.setWindowTitle("设置 · MusicAI")
        self.resize(520, 460)
        self._build()
        self._load_from_config()
        self.test_status.connect(self.test_btn.setText)
        self.ai_test_result.connect(self.ai_test_label.setText)
        self.ncm_qr_ready.connect(self._show_ncm_qr)
        self.ncm_qr_result.connect(self._handle_ncm_qr_result)
        self.ncm_qr_error.connect(self._handle_ncm_qr_error)
        self.ncm_status_ready.connect(self.ncm_login_status.setText)
        self._ncm_unikey = ""
        self._ncm_polling = False
        self._ncm_qr_dialog = None
        self._ncm_qr_timer = QTimer(self)
        self._ncm_qr_timer.setInterval(1800)
        self._ncm_qr_timer.timeout.connect(self._poll_ncm_qr)
        # QQ音乐扫码登录状态
        self.qq_qr_ready.connect(self._show_qq_qr)
        self.qq_qr_result.connect(self._handle_qq_qr_result)
        self.qq_qr_error.connect(self._handle_qq_qr_error)
        self.qq_status_ready.connect(self.qq_login_status.setText)
        self._qq_qrsig = ""
        self._qq_ptqrtoken = ""
        self._qq_polling = False
        self._qq_qr_dialog = None
        self._qq_qr_timer = QTimer(self)
        self._qq_qr_timer.setInterval(1800)
        self._qq_qr_timer.timeout.connect(self._poll_qq_qr)
        # 打开设置即确认 MusicBox / QQ 本地登录态是否仍有效，避免每次都误以为要重新扫码。
        self._refresh_ncm_login_status()
        self._refresh_qq_login_status()

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.tabs.addTab(self._build_ai(), "AI 大模型")
        self.tabs.addTab(self._build_tts(), "语音 TTS")
        self.tabs.addTab(self._build_sources(), "音源")
        self.tabs.addTab(self._build_general(), "通用")

        btn_row = QHBoxLayout()
        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("Primary")
        self.cancel_btn = QPushButton("取消")
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)
        root.addLayout(btn_row)

        self.save_btn.clicked.connect(self._on_save)
        self.cancel_btn.clicked.connect(self.reject)

    def _build_ai(self) -> QWidget:
        """AI 大模型（后端 / 模型 / 思考模式等）。提示词整合进本页底部，默认隐藏，可点链接快速展开。"""
        inner = QWidget()
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(8, 8, 8, 8)

        fl = QFormLayout()

        # ---- 后端选择（ollama 本地 / api 云端） ----
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("本机 llama.cpp（默认·localhost:8080）", "llamacpp")
        self.backend_combo.addItem("云端 API（OpenAI 兼容）", "api")
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        fl.addRow("AI 后端", self.backend_combo)

        # ---- Ollama 组（本地小模型） ----
        self.ollama_group = QWidget()
        ol = QFormLayout(self.ollama_group)
        ol.setContentsMargins(0, 0, 0, 0)
        self.ollama_host = QLineEdit()
        self.ollama_host.setPlaceholderText("http://127.0.0.1:11434")
        ol.addRow("Ollama 服务地址", self.ollama_host)
        self.ollama_deep = QLineEdit()
        self.ollama_deep.setPlaceholderText("qwen3.5:9b")
        ol.addRow("口播模型（深度）", self.ollama_deep)
        self.ollama_fast = QLineEdit()
        self.ollama_fast.setPlaceholderText("qwen3.5:9b")
        ol.addRow("快速模型（转场/快评）", self.ollama_fast)
        self.ollama_fast_enabled = QCheckBox("启用快速模型（短口播走快速模型，降低延迟）")
        ol.addRow(self.ollama_fast_enabled)
        fl.addRow(self.ollama_group)

        # ---- API 组（云端 OpenAI 兼容） ----
        self.api_group = QWidget()
        al = QFormLayout(self.api_group)
        al.setContentsMargins(0, 0, 0, 0)
        self.ai_base = QLineEdit()
        self.ai_key = QLineEdit()
        self.ai_key.setEchoMode(QLineEdit.Password)
        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        self.ai_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.ai_model.addItems(MODEL_CHOICES)
        self.ai_model.setMinimumWidth(220)
        al.addRow("API 地址", self.ai_base)
        al.addRow("API Key", self.ai_key)
        al.addRow("模型名（可下拉选或手输）", self.ai_model)
        fl.addRow(self.api_group)

        # ---- 温度（不同模型默认/支持不同，交给用户选） ----
        self.temp_combo = QComboBox()
        self.temp_combo.addItems(TEMP_CHOICES)
        fl.addRow("温度（创造性）", self.temp_combo)
        self.skip_temp = QCheckBox("不传温度参数（兼容 o1/o3 等推理模型）")
        self.skip_temp.setToolTip(
            "部分推理模型（如 o1/o3 系列）不接受 temperature 参数，勾选后调用时不传温度，避免 400 报错。")
        fl.addRow(self.skip_temp)

        # ---- 思考 / 极速模式（同时作用于 ollama qwen3 与 api Kimi） ----
        self.thinking = QCheckBox(
            "思考模式（关闭=更快：qwen3.5 关思考后正文干净且 ~1s 出稿；开启=质量更高但慢 60~80s）")
        self.thinking.setToolTip(
            "关闭（默认）：qwen3.5 实测 think=False 时完全不产出思考字段、正文干净，"
            "且生成从 88s 降到约 1s——故默认关闭以保证 DJ 口播即时出稿。\n"
            "开启：正文质量可能更高，但 Ollama 会先生成数千 token 思考，广播生成约 60~80s"
            "（由电台「边播边想」预取吸收，不影响切歌即时性）。\n"
            "云端 Kimi/Moonshot 的思考模式也走此开关。")
        fl.addRow(self.thinking)

        # ---- 联网搜索（本地 qwen/Ollama 走 DuckDuckGo；云端 Kimi/Moonshot 走内置工具） ----
        self.web_search = QCheckBox(
            "联网搜索（DJ 口播引用实时信息；本地 qwen/Ollama 走 DuckDuckGo，Kimi/Moonshot 走内置工具）")
        self.web_search.setToolTip(
            "开启后，DJ 口播会附加实时检索资料：\n"
            "· 本地 Ollama/qwen 模型无内置联网能力，由 DuckDuckGo（零 Key、无网自动跳过）提供检索，"
            "让口播能聊到歌手近况、新专辑等；\n"
            "· 云端 Kimi/Moonshot 走其内置 $web_search 工具。\n"
            "追求极致速度 / 无外网时关闭，口播转为纯本地上下文即时生成。")
        fl.addRow(self.web_search)

        # ---- 联网搜索节流（保护「边播边想」预取窗口） ----
        self.web_search_interval = QSpinBox()
        self.web_search_interval.setRange(0, 20)
        self.web_search_interval.setToolTip(
            "联网搜索节流：真实多轮检索较慢（约 13k token + 多轮往返），"
            "若每首都搜会拖累「边播边想」的预取，导致下一首口播赶不上。"
            "0=仅开场第一首搜索；1=每首都搜；N(>1)=每 N 首搜一次。默认 3。")
        fl.addRow("联网搜索频率（0=仅首歌 / 1=每首 / N=每N首）", self.web_search_interval)

        # ---- 上下文窗口（轻量连贯性，控制「保留最近播放」数量） ----
        self.context_window = QSpinBox()
        self.context_window.setRange(0, 20)
        self.context_window.setToolTip(
            "DJ 口播的「轻量连贯性」：注入最近 N 首已播歌曲，让 DJ 呼应上一首、避免重复推荐。\n"
            "0 = 纯实时、不保留任何历史（极致速度，绝不累积冗长对话）；默认 3（轻量连贯）。\n"
            "数值越大口播越「记得之前播过什么」，但也略增提示词长度。")
        fl.addRow("上下文窗口（保留最近播放数，0=纯实时）", self.context_window)

        # ---- 推荐落地模糊匹配（rapidfuzz，阈值可调） ----
        self.fuzzy_match_enabled = QCheckBox(
            "推荐模糊匹配（AI 歌名近似也能命中本地曲库，如《… (Remastered)》）")
        self.fuzzy_match_enabled.setToolTip(
            "开启后用 rapidfuzz 模糊匹配 AI 推荐的歌名与本地曲库：漏括号/版本后缀"
            "（如《Bohemian Rhapsody (Remastered)》）也能命中《Bohemian Rhapsody》，"
            "显著提高推荐「实际播得出」的成功率。作者命中仍为「准确或包含」（不模糊放宽），防张冠李戴。")
        fl.addRow(self.fuzzy_match_enabled)
        self.fuzzy_threshold = QSpinBox()
        self.fuzzy_threshold.setRange(50, 100)
        self.fuzzy_threshold.setSuffix(" 分")
        self.fuzzy_threshold.setToolTip(
            "模糊匹配阈值（0~100）。分数越高越严格：85 表示歌名 token 相似度需 ≥85% 才命中。"
            "调低更容易命中（但可能误配），调高更准（但漏配增多）。默认 85。")
        fl.addRow("模糊匹配阈值", self.fuzzy_threshold)

        # ---- 测试 AI 连接（诊断 API 是否真能通） ----
        test_row = QHBoxLayout()
        self.test_ai_btn = QPushButton("测试 AI 连接")
        self.test_ai_btn.clicked.connect(self._test_ai)
        self.ai_test_label = QLabel("填好地址/Key/模型后点此验证。")
        self.ai_test_label.setObjectName("Sub")
        self.ai_test_label.setWordWrap(True)
        test_row.addWidget(self.test_ai_btn)
        test_row.addWidget(self.ai_test_label, stretch=1)
        fl.addRow(test_row)
        fl.addRow(QLabel("留空则使用内置离线模板口播（无需联网）。\n兼容 OpenAI 协议，可填任意中转/自建地址。\n注意：多数接口地址需以 /v1 结尾。"))
        outer.addLayout(fl)
        self._build_prompt_section(outer)
        outer.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        self._ai_scroll = scroll
        return scroll

    def _on_backend_changed(self, _idx: int) -> None:
        backend = self.backend_combo.currentData()
        self.ollama_group.setVisible(backend == "ollama")
        self.api_group.setVisible(backend == "api")

    def _build_prompt_section(self, outer) -> None:
        """把提示词整合进「AI 大模型」页底部：默认全部隐藏，提供快速修改链接按需展开。

        · 信息获取提示词（系统固定·只读）与内容输出提示词（可编辑）一并收纳；
        · 点击链接展开对应类别编辑区，再次点击收起——默认收起以免干扰常规设置。
        """
        sep = QLabel("— — — — — — — — — — — — — — — —")
        sep.setObjectName("Sub")
        sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(sep)

        tip = QLabel(
            "提示词（口味分析 / 口播 / 转场 / 推荐理由）已整合到本页，默认隐藏以免干扰。"
            "点击下方链接可快速展开并修改：")
        tip.setObjectName("Sub")
        tip.setWordWrap(True)
        outer.addWidget(tip)

        self.prompt_links = QLabel(
            '<a href="taste">口味分析提示词</a>　|　'
            '<a href="broadcast">口播内容提示词</a>　|　'
            '<a href="transition">转场提示词</a>　|　'
            '<a href="reco">推荐理由提示词</a>'
        )
        self.prompt_links.setOpenExternalLinks(False)
        self.prompt_links.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.prompt_links.linkActivated.connect(self._on_prompt_link)
        outer.addWidget(self.prompt_links)

        self._prompt_groups = {}

        # ---- 口味分析 ----
        g_taste = QGroupBox("口味分析（系统读出你的听歌画像）")
        gt = QVBoxLayout(g_taste)
        gt.addWidget(QLabel("① 信息获取提示词（系统固定·只读）"))
        self.taste_info_view = QTextEdit()
        self.taste_info_view.setPlainText(TASTE_ANALYSIS_PROMPT)
        self.taste_info_view.setReadOnly(True)
        self.taste_info_view.setMinimumHeight(160)
        self.taste_info_view.setAcceptRichText(False)
        gt.addWidget(self.taste_info_view)
        gt.addWidget(QLabel("② 内容输出提示词（可编辑·分析侧重/风格）"))
        self.taste_output_edit = QTextEdit()
        self.taste_output_edit.setPlaceholderText(
            "在系统分析基础上，额外强调的分析侧重（如重点看情绪基调与年代倾向）。")
        self.taste_output_edit.setMinimumHeight(60)
        self.taste_output_edit.setAcceptRichText(False)
        gt.addWidget(self.taste_output_edit)
        g_taste.setVisible(False)
        outer.addWidget(g_taste)
        self._prompt_groups["taste"] = g_taste

        # ---- 口播内容 ----
        g_bc = QGroupBox("口播内容（DJ 口播）")
        gbc = QVBoxLayout(g_bc)
        gbc.addWidget(QLabel("① 信息获取提示词（系统固定·只读：角色 + 变量契约）"))
        self.broadcast_info_view = QTextEdit()
        self.broadcast_info_view.setPlainText(BROADCAST_SYSTEM)
        self.broadcast_info_view.setReadOnly(True)
        self.broadcast_info_view.setMinimumHeight(140)
        self.broadcast_info_view.setAcceptRichText(False)
        gbc.addWidget(self.broadcast_info_view)
        gbc.addWidget(QLabel("② 内容输出提示词（可编辑·口播风格/语气/长度/禁区）"))
        self.broadcast_output_edit = QTextEdit()
        self.broadcast_output_edit.setPlaceholderText(
            "补充风格要求，例如「多用歌曲事实、少用抒情比喻」；留空用内置默认人设。")
        self.broadcast_output_edit.setMinimumHeight(110)
        self.broadcast_output_edit.setAcceptRichText(False)
        gbc.addWidget(self.broadcast_output_edit)
        g_bc.setVisible(False)
        outer.addWidget(g_bc)
        self._prompt_groups["broadcast"] = g_bc

        # ---- 转场 ----
        g_tr = QGroupBox("两首歌之间转场（AI 自行决定 intro/outro/none）")
        gtr = QVBoxLayout(g_tr)
        gtr.addWidget(QLabel("① 信息获取提示词（系统固定·只读）"))
        self.transition_info_view = QTextEdit()
        self.transition_info_view.setPlainText(TRANSITION_SYSTEM)
        self.transition_info_view.setReadOnly(True)
        self.transition_info_view.setMinimumHeight(160)
        self.transition_info_view.setAcceptRichText(False)
        gtr.addWidget(self.transition_info_view)
        gtr.addWidget(QLabel("② 内容输出提示词（可编辑·创作原则/风格参考）"))
        self.transition_output_edit = QTextEdit()
        self.transition_output_edit.setPlaceholderText(
            "创作原则与风格参考，可自由改写（禁止照搬示例原句）。")
        self.transition_output_edit.setMinimumHeight(110)
        self.transition_output_edit.setAcceptRichText(False)
        gtr.addWidget(self.transition_output_edit)
        g_tr.setVisible(False)
        outer.addWidget(g_tr)
        self._prompt_groups["transition"] = g_tr

        # ---- 推荐理由 ----
        g_reco = QGroupBox("推荐理由")
        gr = QVBoxLayout(g_reco)
        gr.addWidget(QLabel("内容输出提示词（可编辑·系统指令）"))
        self.reco_prompt = QTextEdit()
        self.reco_prompt.setPlaceholderText(RECO_EXPLAIN_SYSTEM)
        self.reco_prompt.setMinimumHeight(60)
        self.reco_prompt.setAcceptRichText(False)
        gr.addWidget(self.reco_prompt)
        g_reco.setVisible(False)
        outer.addWidget(g_reco)
        self._prompt_groups["reco"] = g_reco

    def _on_prompt_link(self, key: str) -> None:
        """点击快速修改链接：展开/收起对应提示词编辑区，并滚动到可视位置。"""
        grp = self._prompt_groups.get(key)
        if grp is None:
            return
        grp.setVisible(not grp.isVisible())
        if grp.isVisible() and getattr(self, "_ai_scroll", None) is not None:
            self._ai_scroll.ensureWidgetVisible(grp)

    def _build_tts(self) -> QWidget:
        w = QWidget()
        fl = QFormLayout(w)

        # ---- 引擎切换（melotts / edge / chattts） ----
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("IndexTTS 2.5（多语·本地·GPU）", "indextts")
        self.engine_combo.addItem("Edge 云 TTS（云端兜底）", "edge")
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        fl.addRow("TTS 引擎", self.engine_combo)

        # ---- 音色（随引擎动态变化） ----
        self.tts_voice = QComboBox()
        self.tts_voice.setMinimumWidth(240)
        fl.addRow("音色", self.tts_voice)

        self.tts_rate = QComboBox()
        self.tts_rate.addItems(RATE_OPTS)
        self.tts_vol = QComboBox()
        self.tts_vol.addItems(VOL_OPTS)
        self.test_btn = QPushButton("测试朗读")
        self.test_btn.clicked.connect(self._test_tts)
        fl.addRow("语速", self.tts_rate)
        fl.addRow("音量", self.tts_vol)
        fl.addRow(self.test_btn)

        # ---- 当前自定义 MeloTTS 模型目录 ----
        self.model_dir_label = QLabel("（未设置自定义模型）")
        self.model_dir_label.setObjectName("Sub")
        self.model_dir_label.setWordWrap(True)
        fl.addRow("自定义模型目录", self.model_dir_label)

        # ---- 导入音色 / 模型（拖拽或选择） ----
        import_box = QGroupBox("导入音色 / 模型（更换音色）")
        ib = QVBoxLayout(import_box)
        ib.addWidget(QLabel(
            "拖拽文件到下方虚线框，或点「选择文件」：\n"
            "· .pth  → MeloTTS 主播模型（自动设为当前音色，可更换中文主播声线）\n"
            "· .npy / .pt → ChatTTS 说话人（导入后在上方引擎选 ChatTTS 即可用）"))
        self.drop_area = DropArea()
        self.drop_area.filesDropped.connect(self._import_voice_files)
        ib.addWidget(self.drop_area)
        pick_row = QHBoxLayout()
        self.pick_btn = QPushButton("选择文件…")
        self.pick_btn.clicked.connect(self._pick_voice_file)
        self.clear_model_btn = QPushButton("清除自定义 MeloTTS 模型")
        self.clear_model_btn.clicked.connect(self._clear_melotts_model)
        pick_row.addWidget(self.pick_btn)
        pick_row.addWidget(self.clear_model_btn)
        ib.addLayout(pick_row)
        self.import_status = QLabel("")
        self.import_status.setObjectName("Sub")
        self.import_status.setWordWrap(True)
        ib.addWidget(self.import_status)
        fl.addRow(import_box)

        # ---- 多语言分片 TTS 的可调参数 ----
        self.voicevox_host = QLineEdit()
        self.voicevox_host.setPlaceholderText("127.0.0.1:50021")
        self.voicevox_host.setToolTip(
            "VOICEVOX 日文 TTS 本地服务地址（独立桌面 App，需另装并启动）。"
            "未启动时会自动回退到 Edge 日文音色，不影响主流程。")
        fl.addRow("VOICEVOX 地址（日文）", self.voicevox_host)

        # ---- VOICEVOX 连接自检（确认日文 TTS 服务可达） ----
        self.vv_test_btn = QPushButton("测试 VOICEVOX 连接")
        self.vv_test_btn.clicked.connect(self._test_voicevox)
        self.vv_status = QLabel("未检测；安装并启动 VOICEVOX 后点此验证（默认 127.0.0.1:50021）。")
        self.vv_status.setObjectName("Sub")
        self.vv_status.setWordWrap(True)
        fl.addRow(self.vv_test_btn, self.vv_status)

        self.melotts_gpu = QCheckBox("启用 MeloTTS GPU 加速（已装 cu124 torch，默认开）")
        self.melotts_gpu.setToolTip(
            "默认开启（本机已装 CUDA 12.4 的 PyTorch，MeloTTS 用 GPU 推理更快）。\n"
            "关闭则走 CPU。MeloTTS 与 Ollama 共享 RTX 4060，按需切换、用完释放显存。")
        fl.addRow(self.melotts_gpu)

        # ---- MeloTTS 声音调校（沉稳播报感）：speed / sdp_ratio / noise_scale / noise_scale_w ----
        # 仅当引擎为 melotts 时显示（由 _on_engine_changed 控制可见性）。
        self.melo_tuning = self._build_melotts_tuning()
        fl.addRow(self.melo_tuning)

        self.seg_silence = QSpinBox()
        self.seg_silence.setRange(0, 500)
        self.seg_silence.setSuffix(" ms")
        self.seg_silence.setToolTip(
            "多语言分片拼接时，片段之间的静音间隔，防止不同语种/引擎音色突兀相接。默认 80ms。")
        fl.addRow("分片拼接静音", self.seg_silence)

        # ---- 全局 TTS 引擎连接自检 ----
        # 澄清：日志里「引擎=edge」是默认配置、正常工作，并非 melotts/voicevox 没连上；
        # 各引擎按需启用，未设为当前引擎时本就「未使用」，属正常。
        self.tts_test_btn = QPushButton("测试 TTS 连接")
        self.tts_test_btn.clicked.connect(self._test_tts_connection)
        self.tts_test_status = QLabel(
            "点此检测 edge/melotts/chattts/voicevox 各引擎可用性。"
            "当前生效引擎以配置为准；日文走 voicevox（可自动拉起，无需手动开 App）。")
        self.tts_test_status.setObjectName("Sub")
        self.tts_test_status.setWordWrap(True)
        fl.addRow(self.tts_test_btn, self.tts_test_status)
        return w

    # ---------------- 引擎 / 音色动态联动 ----------------
    def _build_melotts_tuning(self) -> QGroupBox:
        """MeloTTS 声音调校组：4 个滑块（speed/sdp_ratio/noise_scale/noise_scale_w）
        + ffmpeg 后处理开关 + ffmpeg 路径。仅 melotts 引擎可见。"""
        gb = QGroupBox("MeloTTS 声音调校（沉稳播报感）")
        fl = QFormLayout(gb)

        self.melo_speed = QSlider(Qt.Orientation.Horizontal)
        self.melo_speed.setRange(50, 150)        # 0.50 ~ 1.50
        self.melo_speed.setToolTip("语速：越小越慢、越沉稳（默认 0.88）")
        self.melo_sdp = QSlider(Qt.Orientation.Horizontal)
        self.melo_sdp.setRange(0, 50)            # 0.00 ~ 0.50
        self.melo_sdp.setToolTip("韵律随机度：越低越规整、播报感越强（默认 0.12）")
        self.melo_noise = QSlider(Qt.Orientation.Horizontal)
        self.melo_noise.setRange(0, 100)         # 0.00 ~ 1.00
        self.melo_noise.setToolTip("采样噪声：越低越干净稳定（默认 0.32）")
        self.melo_noise_w = QSlider(Qt.Orientation.Horizontal)
        self.melo_noise_w.setRange(0, 100)       # 0.00 ~ 1.00
        self.melo_noise_w.setToolTip("噪声带宽：越低节奏越规整（默认 0.45）")

        self.melo_speed_val = QLabel("0.88")
        self.melo_sdp_val = QLabel("0.12")
        self.melo_noise_val = QLabel("0.32")
        self.melo_noise_w_val = QLabel("0.45")

        def _bind(slider: QSlider, label: QLabel) -> None:
            def _up(v: int) -> None:
                label.setText(f"{v / 100.0:.2f}")
            slider.valueChanged.connect(_up)

        _bind(self.melo_speed, self.melo_speed_val)
        _bind(self.melo_sdp, self.melo_sdp_val)
        _bind(self.melo_noise, self.melo_noise_val)
        _bind(self.melo_noise_w, self.melo_noise_w_val)

        fl.addRow("语速 speed", self._slider_row(self.melo_speed, self.melo_speed_val))
        fl.addRow("韵律 sdp_ratio", self._slider_row(self.melo_sdp, self.melo_sdp_val))
        fl.addRow("噪声 noise_scale", self._slider_row(self.melo_noise, self.melo_noise_val))
        fl.addRow("噪声带宽 noise_scale_w",
                  self._slider_row(self.melo_noise_w, self.melo_noise_w_val))

        # ---- ffmpeg 后处理（强化播报质感） ----
        self.melo_pp = QCheckBox("音频后处理（ffmpeg 均衡/压缩/混响/响度）")
        self.melo_pp.setToolTip(
            "合成后自动经 ffmpeg 做：低频/中频提升 + 高频微亮 + 压缩器(播报感)\n"
            "+ 轻微回声(空间感) + loudnorm 统一响度（沉稳电台感）。")
        fl.addRow(self.melo_pp)
        self.melo_ffmpeg_path = QLineEdit()
        self.melo_ffmpeg_path.setPlaceholderText("留空=自动探测（项目内置/系统 PATH）")
        self.melo_ffmpeg_path.setToolTip(
            "ffmpeg 可执行文件路径；留空则由程序自动探测（优先项目内置完整构建）。")
        fl.addRow("ffmpeg 路径", self.melo_ffmpeg_path)

        # ---- ffmpeg 后处理「参数」滑块（仅在后处理开启时生效） ----
        pp_gb = QGroupBox("ffmpeg 后处理参数（沉稳播报微调）")
        pp_gb.setToolTip("后处理开关开启时，按这 7 个滑块拼出后处理链：\n"
                         "highshelf×3（三段高架）+ 压缩器 + 回声 + loudnorm 统一响度。")
        pfl = QFormLayout(pp_gb)

        def _mk(slider, lo, hi, tooltip):
            slider.setRange(lo, hi)
            slider.setToolTip(tooltip)
            return slider

        self.melo_hs1 = _mk(QSlider(Qt.Orientation.Horizontal), -60, 120,
                            "低频增益(120Hz)：负值削减、正值抬升（默认 +5dB）")
        self.melo_hs2 = _mk(QSlider(Qt.Orientation.Horizontal), -60, 120,
                            "中频增益(250Hz)（默认 +3dB）")
        self.melo_hs3 = _mk(QSlider(Qt.Orientation.Horizontal), -60, 120,
                            "高频微亮(3000Hz)（默认 +2dB）")
        self.melo_comp_th = _mk(QSlider(Qt.Orientation.Horizontal), -40, 0,
                                "压缩器阈值 dB（默认 -20，越低压得越狠）")
        self.melo_comp_ratio = _mk(QSlider(Qt.Orientation.Horizontal), 10, 100,
                                   "压缩比（默认 4:1）")
        self.melo_echo = _mk(QSlider(Qt.Orientation.Horizontal), 0, 100,
                             "回声湿声比例（默认 0.5；0=关闭混响）")
        self.melo_loud_i = _mk(QSlider(Qt.Orientation.Horizontal), -24, -10,
                               "目标响度 I LUFS（默认 -16）")

        self.melo_hs1_val = QLabel("5.0")
        self.melo_hs2_val = QLabel("3.0")
        self.melo_hs3_val = QLabel("2.0")
        self.melo_comp_th_val = QLabel("-20")
        self.melo_comp_ratio_val = QLabel("4.0")
        self.melo_echo_val = QLabel("0.5")
        self.melo_loud_i_val = QLabel("-16")

        def _bind2(slider, label, div, nd):
            def _up(v):
                label.setText(f"{v / div:.{nd}f}")
            slider.valueChanged.connect(_up)

        _bind2(self.melo_hs1, self.melo_hs1_val, 10, 1)
        _bind2(self.melo_hs2, self.melo_hs2_val, 10, 1)
        _bind2(self.melo_hs3, self.melo_hs3_val, 10, 1)
        _bind2(self.melo_comp_th, self.melo_comp_th_val, 1, 0)
        _bind2(self.melo_comp_ratio, self.melo_comp_ratio_val, 10, 1)
        _bind2(self.melo_echo, self.melo_echo_val, 100, 2)
        _bind2(self.melo_loud_i, self.melo_loud_i_val, 1, 0)

        pfl.addRow("低频增益 hs1(120Hz)",
                   self._slider_row(self.melo_hs1, self.melo_hs1_val))
        pfl.addRow("中频增益 hs2(250Hz)",
                   self._slider_row(self.melo_hs2, self.melo_hs2_val))
        pfl.addRow("高频增益 hs3(3k)",
                   self._slider_row(self.melo_hs3, self.melo_hs3_val))
        pfl.addRow("压缩阈值(dB)",
                   self._slider_row(self.melo_comp_th, self.melo_comp_th_val))
        pfl.addRow("压缩比",
                   self._slider_row(self.melo_comp_ratio, self.melo_comp_ratio_val))
        pfl.addRow("回声量",
                   self._slider_row(self.melo_echo, self.melo_echo_val))
        pfl.addRow("目标响度 I",
                   self._slider_row(self.melo_loud_i, self.melo_loud_i_val))
        # 后处理关闭时禁用参数滑块（开了才有意义）
        self.melo_pp.toggled.connect(pp_gb.setEnabled)
        pp_gb.setEnabled(self.melo_pp.isChecked())
        fl.addRow(pp_gb)
        return gb

    @staticmethod
    def _slider_row(slider: QSlider, label: QLabel) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(label)
        w = QWidget()
        w.setLayout(row)
        return w

    def _on_engine_changed(self, _idx: int) -> None:
        self._populate_voice_combo()
        self._refresh_model_dir_label()
        # MeloTTS 声音调校组仅在该引擎下显示
        self.melo_tuning.setVisible(self.engine_combo.currentData() == "melotts")

    def _populate_voice_combo(self) -> None:
        """按当前引擎填充音色下拉：edge=云主播音色；melotts=内置多语说话人+自定义；
        chattts=已导入说话人。"""
        engine = self.engine_combo.currentData() or "edge"
        self.tts_voice.blockSignals(True)
        self.tts_voice.clear()
        if engine == "melotts":
            for label, key in MELOTTS_SPEAKERS:
                self.tts_voice.addItem(label, key)
            custom = (user_config.tts.get("melotts_model_dir", "") or "").strip()
            if custom:
                self.tts_voice.addItem("自定义音色模型（自动选说话人）", "")
            cur = user_config.tts.get("melotts_speaker", "ZH")
        elif engine == "chattts":
            voices = [v for v in self._voice_mgr.list_voices()
                      if v.get("engine") == "chattts"]
            if voices:
                for v in voices:
                    self.tts_voice.addItem(v.get("voice_id", "?"), v.get("voice_id"))
            else:
                self.tts_voice.addItem("（暂无导入说话人，请拖拽 .npy/.pt）", "")
            cur = user_config.tts.get("voice_id", "")
        else:  # edge
            for label, vid in EDGE_VOICES:
                self.tts_voice.addItem(label, vid)
            cur = user_config.tts.get("voice", "zh-CN-XiaoxiaoNeural")
        self.tts_voice.blockSignals(False)
        self._set_combo_data(self.tts_voice, cur)

    def _refresh_model_dir_label(self) -> None:
        d = (user_config.tts.get("melotts_model_dir", "") or "").strip()
        if d:
            self.model_dir_label.setText(f"当前自定义模型：{d}")
        else:
            self.model_dir_label.setText("（未设置自定义模型，使用内置）")

    # ---------------- 导入音色 / 模型 ----------------
    def _pick_voice_file(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择音色 / 模型文件", "", "音色模型 (*.pth *.npy *.pt)")
        if files:
            self._import_voice_files(files)

    def _import_voice_files(self, paths: list) -> None:
        ok = 0
        for p in paths:
            ext = Path(p).suffix.lower()
            try:
                if ext == ".pth":
                    self._import_melotts_model(p)
                    ok += 1
                elif ext in (".npy", ".pt"):
                    self._import_chattts_voice(p)
                    ok += 1
                else:
                    self.import_status.setText(
                        f"⚠ 不支持的文件类型：{Path(p).name}（仅支持 .pth/.npy/.pt）")
            except Exception as e:  # noqa
                self.import_status.setText(f"⚠ 导入失败 {Path(p).name}：{e}")
        if ok:
            self.import_status.setText(f"✅ 已导入 {ok} 个文件（保存后生效）")

    def _import_melotts_model(self, path: str) -> None:
        src = Path(path)
        dst_dir = VOICES_DIR / "melotts" / src.stem
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        shutil.copy2(src, dst)
        user_config.tts["melotts_model_dir"] = str(dst_dir)
        # 自动切到 melotts 并选中「自定义」项
        self.engine_combo.setCurrentIndex(self.engine_combo.findData("melotts"))
        self._populate_voice_combo()
        idx = self.tts_voice.findData("")
        if idx >= 0:
            self.tts_voice.setCurrentIndex(idx)
        self._refresh_model_dir_label()

    def _import_chattts_voice(self, path: str) -> None:
        vid = self._voice_mgr.import_voice(path, None, "chattts")
        # 切到 chattts 并选中刚导入的说话人
        self.engine_combo.setCurrentIndex(self.engine_combo.findData("chattts"))
        self._populate_voice_combo()
        idx = self.tts_voice.findData(vid)
        if idx >= 0:
            self.tts_voice.setCurrentIndex(idx)

    def _clear_melotts_model(self) -> None:
        user_config.tts["melotts_model_dir"] = ""
        if self.engine_combo.currentData() == "melotts":
            self._populate_voice_combo()
        self._refresh_model_dir_label()
        self.import_status.setText("已清除自定义 MeloTTS 模型（将使用内置音色）")

    # ---------------- 试听（按当前引擎/音色） ----------------
    def _test_tts(self):
        self.test_btn.setText("合成中…")
        engine = self.engine_combo.currentData() or "edge"
        voice_key = self.tts_voice.currentData()
        rate = self.tts_rate.currentText()
        vol = self.tts_vol.currentText()
        text = "你好，我是你的 AI 电台主播，现在正在为你伴听，希望这首歌能陪你度过这段时光。"
        threading.Thread(
            target=self._do_test_tts, args=(engine, voice_key, rate, vol, text),
            daemon=True,
        ).start()

    def _do_test_tts(self, engine, voice_key, rate, vol, text):
        try:
            from core.tts import TTSEngine
            saved = dict(user_config.tts)
            try:
                user_config.tts["engine"] = engine
                if engine == "edge":
                    user_config.tts["voice"] = voice_key
                elif engine == "melotts":
                    user_config.tts["melotts_speaker"] = voice_key
                elif engine == "chattts":
                    user_config.tts["voice_id"] = voice_key
                eng = TTSEngine()
                eng.reconfigure()
                eng.apply_overrides(
                    voice_key if engine in ("edge", "chattts") else None,
                    rate, vol)
                path = eng.synthesize(text)
            finally:
                user_config.tts.clear()
                user_config.tts.update(saved)
            if not path:
                self.test_status.emit("⚠️ TTS 不可用（无可用引擎/离线）")
                return
            if self._player is not None:
                self.play_requested.emit(path)
                self.test_status.emit("🔊 正在试听…")
            else:
                self.test_status.emit("✅ 已生成（未连接播放器）")
        except Exception as e:  # noqa
            self.test_status.emit(f"失败：{e}")

    def _build_sources(self) -> QWidget:
        w = QWidget()
        fl = QFormLayout(w)
        # 网易云：仅保留 MusicBox 扫码登录 + 音乐盒 UID 导入
        self.ncm_on = QCheckBox("启用网易云")
        self.ncm_login_btn = QPushButton("扫码登录网易云")
        self.ncm_login_btn.clicked.connect(self._start_ncm_qr_login)
        self.ncm_login_status = QLabel("仅支持 MusicBox 扫码登录；登录信息由 MusicBox 本地保存。")
        self.ncm_login_status.setObjectName("Sub")
        self.ncm_login_status.setWordWrap(True)
        self.ncm_musicbox = QLineEdit()
        self.ncm_musicbox.setPlaceholderText("用于导入「我喜欢的音乐」")
        fl.addRow(self.ncm_on)
        fl.addRow(self.ncm_login_btn)
        fl.addRow(self.ncm_login_status)
        fl.addRow("音乐盒(用户ID)", self.ncm_musicbox)
        # QQ音乐
        self.qq_on = QCheckBox("启用 QQ音乐")
        self.qq_base = QLineEdit()
        self.qq_base.setPlaceholderText("http://127.0.0.1:3200（qq-music-api 本地服务）")
        self.qq_login_btn = QPushButton("扫码登录 QQ音乐")
        self.qq_login_btn.setToolTip(
            "通过本机运行的 qq-music-api 服务生成二维码，用 QQ / 微信 扫码登录；"
            "登录成功后会把 Cookie 自动写入配置（DPAPI 加密），重启后自动复用，无需重新扫码。")
        self.qq_login_btn.clicked.connect(self._start_qq_qr_login)
        self.qq_login_status = QLabel(
            "点击「扫码登录 QQ音乐」获取二维码；登录态由 Cookie 持久化，重启后自动复用。")
        self.qq_login_status.setObjectName("Sub")
        self.qq_login_status.setWordWrap(True)
        self.qq_cookie = QLineEdit()
        self.qq_cookie.setEchoMode(QLineEdit.Password)
        self.qq_cookie.setPlaceholderText("登录后自动填写，也可手动粘贴覆盖")
        self.qq_pid = QLineEdit()
        fl.addRow(self.qq_on)
        fl.addRow("服务地址(qq-music-api)", self.qq_base)
        fl.addRow(self.qq_login_btn)
        fl.addRow(self.qq_login_status)
        fl.addRow("Cookie(登录后自动填写)", self.qq_cookie)
        fl.addRow("默认歌单ID", self.qq_pid)
        # Spotify
        self.spotify_on = QCheckBox("启用 Spotify")
        self.spotify_base = QLineEdit()
        self.spotify_cid = QLineEdit()
        self.spotify_secret = QLineEdit(); self.spotify_secret.setEchoMode(QLineEdit.Password)
        self.spotify_cookie = QLineEdit()
        self.spotify_cookie.setEchoMode(QLineEdit.Password)
        self.spotify_pid = QLineEdit()
        fl.addRow(self.spotify_on)
        fl.addRow("服务地址(可选·兼容代理)", self.spotify_base)
        fl.addRow("Client ID(可选)", self.spotify_cid)
        fl.addRow("Client Secret(可选)", self.spotify_secret)
        fl.addRow("Cookie", self.spotify_cookie)
        fl.addRow("默认歌单ID", self.spotify_pid)
        # 本地
        self.local_on = QCheckBox("扫描本地目录")
        self.local_roots = QTextEdit()
        self.local_roots.setPlaceholderText("每行一个目录，如：\nD:\\CloudMusic\nD:\\Music")
        fl.addRow(self.local_on)
        fl.addRow("本地目录", self.local_roots)
        return w

    # ---------------- 数据绑定 ----------------
    def _build_general(self) -> QWidget:
        """通用设置：手动城市覆盖（解决 IP 定位不准）。"""
        inner = QWidget()
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(8, 8, 8, 8)

        fl = QFormLayout()
        self.city_edit = QLineEdit()
        self.city_edit.setPlaceholderText("如：长春（留空则自动定位）")
        fl.addRow("所在城市（手动覆盖）", self.city_edit)

        # ---- 推荐偏好（熟悉度级别）：关键推荐变量，直接注入 AI 推荐提示词 ----
        self.rec_pref_combo = QComboBox()
        self.rec_pref_combo.addItems(RadioConfig.FAMILIARITY_LABELS)
        fl.addRow("推荐偏好（歌曲）", self.rec_pref_combo)
        fl.addRow(QLabel(
            "控制 AI 推荐歌曲的「熟悉 ↔ 探索」倾向，会作为关键变量注入推荐提示词：\n"
            "· 更熟悉：严格贴合你已有的品味 / 曲库（我喜欢的音乐），听熟悉的；\n"
            "· 更陌生：跳出舒适区，探索风格迥异的新歌。\n"
            "该偏好持久化保存，重启后保留，并会体现在 AI 的推荐解说说辞中。"))

        fl.addRow(QLabel(
            "留空时自动定位：优先用电脑区域，回退到 IP 定位。\n"
            "免费 IP 地理库对部分地区的用户可能不准（如长春被定位成沈阳），\n"
            "此时填写本项即可永久以你填的城市为准，并影响天气与口播文案。"))

        fl.addRow(QLabel(
            f"自动定位失败时的回退城市：{GeoConfig.FALLBACK_CITY}"
            f"（可在 config/settings.py 或环境变量 MUSICAI_FALLBACK_CITY 修改）。"))

        outer.addLayout(fl)
        outer.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        return scroll

    def _load_from_config(self):
        a = user_config.ai
        self._set_combo_data(self.backend_combo, a.get("backend", "api"))
        self.ai_base.setText(a.get("api_base", ""))
        self.ai_key.setText(a.get("api_key", ""))
        self._set_combo(self.ai_model, a.get("model", "gpt-4o-mini"), MODEL_CHOICES)
        self.ollama_host.setText(a.get("ollama_host", "http://127.0.0.1:11434"))
        self.ollama_deep.setText(a.get("ollama_deep", "qwen3.5:9b"))
        self.ollama_fast.setText(a.get("ollama_fast", "qwen3.5:9b"))
        self.ollama_fast_enabled.setChecked(bool(a.get("ollama_fast_enabled", True)))
        self._set_combo(self.temp_combo, str(a.get("temperature", 0.8)), TEMP_CHOICES)
        self.skip_temp.setChecked(bool(a.get("skip_temperature", False)))
        self.web_search.setChecked(bool(a.get("web_search", False)))
        self.thinking.setChecked(bool(a.get("thinking", False)))
        self.web_search_interval.setValue(int(a.get("web_search_interval", 3)))
        self.context_window.setValue(int(a.get("context_window", 3)))
        self.fuzzy_match_enabled.setChecked(bool(user_config.match.get("fuzzy_enabled", True)))
        self.fuzzy_threshold.setValue(int(user_config.match.get("fuzzy_threshold", 85)))
        self._on_backend_changed(self.backend_combo.currentIndex())

        p = user_config.prompts
        self.taste_output_edit.setPlainText(p.get("taste_output", "") or "")
        self.broadcast_output_edit.setPlainText(
            p.get("broadcast_output") or BROADCAST_OUTPUT_DEFAULT)
        self.transition_output_edit.setPlainText(p.get("transition_output", "") or "")
        self.reco_prompt.setPlainText(p.get("reco_explain", "") or "")

        t = user_config.tts
        self._set_combo_data(self.engine_combo, t.get("engine", "edge"))
        self._populate_voice_combo()
        self._set_combo(self.tts_rate, t.get("rate", "+0%"), RATE_OPTS)
        self._set_combo(self.tts_vol, t.get("volume", "+0%"), VOL_OPTS)
        self.voicevox_host.setText(t.get("voicevox_host", "127.0.0.1:50021"))
        self.melotts_gpu.setChecked(bool(t.get("melotts_gpu", True)))
        self.seg_silence.setValue(int(t.get("segment_silence_ms", 80)))
        self.melo_speed.setValue(int(round(float(t.get("melotts_speed", 0.88)) * 100)))
        self.melo_sdp.setValue(int(round(float(t.get("melotts_sdp_ratio", 0.12)) * 100)))
        self.melo_noise.setValue(int(round(float(t.get("melotts_noise_scale", 0.32)) * 100)))
        self.melo_noise_w.setValue(int(round(float(t.get("melotts_noise_scale_w", 0.45)) * 100)))
        self.melo_pp.setChecked(bool(t.get("melotts_postprocess", True)))
        self.melo_ffmpeg_path.setText(t.get("melotts_ffmpeg_path", "") or "")
        # ffmpeg 后处理 7 参数滑块
        self.melo_hs1.setValue(int(round(float(t.get("melotts_pp_hs1", 5.0)) * 10)))
        self.melo_hs2.setValue(int(round(float(t.get("melotts_pp_hs2", 3.0)) * 10)))
        self.melo_hs3.setValue(int(round(float(t.get("melotts_pp_hs3", 2.0)) * 10)))
        self.melo_comp_th.setValue(int(round(float(t.get("melotts_pp_comp_th", -20.0)))))
        self.melo_comp_ratio.setValue(int(round(float(t.get("melotts_pp_comp_ratio", 4.0)) * 10)))
        self.melo_echo.setValue(int(round(float(t.get("melotts_pp_echo", 0.5)) * 100)))
        self.melo_loud_i.setValue(int(round(float(t.get("melotts_pp_loud_i", -16.0)))))
        self._refresh_model_dir_label()

        s = user_config.sources
        n = s.get("netease", {})
        self.ncm_on.setChecked(bool(n.get("enabled")))
        self.ncm_musicbox.setText(n.get("musicbox_uid", ""))
        q = s.get("qqmusic", {})
        self.qq_on.setChecked(bool(q.get("enabled")))
        self.qq_base.setText(q.get("api_base", ""))
        self.qq_cookie.setText(q.get("cookie", ""))
        self.qq_pid.setText(q.get("default_playlist_id", ""))
        sp = s.get("spotify", {})
        self.spotify_on.setChecked(bool(sp.get("enabled")))
        self.spotify_base.setText(sp.get("api_base", ""))
        self.spotify_cid.setText(sp.get("client_id", ""))
        self.spotify_secret.setText(sp.get("client_secret", ""))
        self.spotify_cookie.setText(sp.get("cookie", ""))
        self.spotify_pid.setText(sp.get("default_playlist_id", ""))
        l = s.get("local", {})
        self.local_on.setChecked(bool(l.get("enabled", True)))
        self.local_roots.setPlainText("\n".join(l.get("roots", [])))

        g = user_config.data.get("geo", {})
        self.city_edit.setText(g.get("city", "") or "")
        self.rec_pref_combo.setCurrentIndex(
            max(0, min(3, int(user_config.data.get("rec_pref", 0) or 0))))

    @staticmethod
    def _set_combo(combo: QComboBox, value: str, options: list[str]) -> None:
        if value in options:
            combo.setCurrentText(value)
        else:
            combo.insertItem(0, value)
            combo.setCurrentText(value)

    @staticmethod
    def _set_combo_data(combo: QComboBox, data: str) -> None:
        """按 itemData（而非显示文本）匹配并选中，找不到则 fallback 到首项。"""
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        elif combo.count() > 0:
            combo.setCurrentIndex(0)

    # ---------------- 动作 ----------------
    def _test_voicevox(self):
        """后台检测/自动拉起 VOICEVOX 引擎（headless run.exe），确认日文 TTS 可用。

        现在 VOICEVOX 已内嵌：若本地引擎服务未启动，会自动探测并拉起你安装的
        headless 引擎（无需手动开 VOICEVOX App），拉起成功后日文口播即可用。
        """
        self.vv_test_btn.setEnabled(False)
        host = self.voicevox_host.text().strip() or "127.0.0.1:50021"
        base = host if host.startswith("http") else f"http://{host}"
        base = base.rstrip("/")
        self.vv_status.setText(f"连接/拉起中… {base}")

        def run():
            try:
                from core.tts import TTSEngine
                ok = TTSEngine().ensure_voicevox(timeout=45)
                if ok:
                    self.vv_status.setText(
                        f"✅ VOICEVOX 已连接（自动拉起成功，{base}），日文口播可用。")
                else:
                    self.vv_status.setText(
                        f"⚠ 无法连接/拉起 VOICEVOX（{base}）：请确认已安装 VOICEVOX，"
                        "或在设置中填写 tts.voicevox_exe 路径（留空则自动探测）。"
                        "未连接时日文片段回退 Edge，不影响主流程。")
            except Exception as e:  # noqa
                self.vv_status.setText(f"⚠ 检测异常：{e}")
            finally:
                self.vv_test_btn.setEnabled(True)

        threading.Thread(target=run, daemon=True).start()

    def _test_tts_connection(self):
        """后台探测各 TTS 引擎可用性，并澄清「engine=edge 是默认配置、正常工作」。"""
        self.tts_test_btn.setEnabled(False)
        self.tts_test_status.setText("检测中…")

        def run():
            try:
                from core.tts import TTSEngine
                diag = TTSEngine().diagnose()
                active = diag.get("_active", "?")
                note = diag.get("_note", "")
                parts = []
                for name in ("edge", "melotts", "indextts", "chattts", "voicevox"):
                    d = diag.get(name, {})
                    mark = "✅" if d.get("ok") else "⚠️"
                    parts.append(f"{name}:{mark}{d.get('msg', '')}")
                msg = f"当前生效引擎={active}。\n" + "；".join(parts) + f"\n{note}"
            except Exception as e:  # noqa
                msg = f"检测异常：{e}"
            self.tts_test_status.setText(msg)
            self.tts_test_btn.setEnabled(True)

        threading.Thread(target=run, daemon=True).start()

    def _test_ai(self):
        """后台验证当前填写的 AI 配置是否真的可用，结果回主线程显示。
        同时支持 ollama 本地后端（用对话框当前值临时覆盖，不落盘）。"""
        self.test_ai_btn.setEnabled(False)
        self.ai_test_label.setText("连接测试中…")
        backend = self.backend_combo.currentData() or "api"

        import threading

        def run():
            try:
                from ai.brain import AIBrain
                # 用当前对话框值临时覆盖，使测试反映所选后端（测试不改持久化）
                saved = {
                    "backend": user_config.ai.get("backend"),
                    "ollama_host": user_config.ai.get("ollama_host"),
                    "ollama_deep": user_config.ai.get("ollama_deep"),
                }
                user_config.ai["backend"] = backend
                if backend == "ollama":
                    user_config.ai["ollama_host"] = (
                        self.ollama_host.text().strip() or "http://127.0.0.1:11434")
                    user_config.ai["ollama_deep"] = (
                        self.ollama_deep.text().strip() or "qwen3.5:9b")
                    base = user_config.ai["ollama_host"]
                    key = ""
                    model = user_config.ai["ollama_deep"]
                else:
                    base = self.ai_base.text().strip()
                    key = self.ai_key.text().strip()
                    model = self.ai_model.currentText().strip() or "gpt-4o-mini"
                try:
                    ok, msg = AIBrain().test_connection(base, key, model)
                finally:
                    user_config.ai["backend"] = saved["backend"]
                    user_config.ai["ollama_host"] = saved["ollama_host"]
                    user_config.ai["ollama_deep"] = saved["ollama_deep"]
            except Exception as e:  # noqa
                ok, msg = False, f"测试异常：{e}"
            self.ai_test_result.emit(msg)
            self.test_ai_btn.setEnabled(True)

        threading.Thread(target=run, daemon=True).start()

    # ---------------- 网易云扫码登录 ----------------
    def _start_ncm_qr_login(self):
        self.ncm_login_btn.setEnabled(False)
        self.ncm_login_status.setText("正在获取二维码…")

        def run():
            try:
                from data.sources.netease import NeteaseSource
                session = NeteaseSource({"backend": "musicbox"}).start_qr_login()
                self.ncm_qr_ready.emit(session)
            except Exception as e:  # noqa
                self.ncm_qr_error.emit(f"获取二维码失败：{e}")

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _qr_pixmap(url: str) -> QPixmap:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        scale = 7
        side = len(matrix) * scale
        image = QImage(side, side, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        for row, values in enumerate(matrix):
            for col, dark in enumerate(values):
                if dark:
                    for y in range(row * scale, (row + 1) * scale):
                        for x in range(col * scale, (col + 1) * scale):
                            image.setPixel(x, y, 0xFF000000)
        return QPixmap.fromImage(image)

    def _show_ncm_qr(self, session: dict):
        self._ncm_unikey = session.get("unikey", "")
        if not self._ncm_unikey:
            self._handle_ncm_qr_error("获取二维码失败：响应缺少登录标识")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("网易云扫码登录")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("请使用网易云音乐 App 扫描二维码并在手机上确认。"))
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(self._qr_pixmap(session.get("url", "")))
        layout.addWidget(image)
        status = QLabel("等待扫码…")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(status)
        dlg.resize(330, 410)
        dlg.finished.connect(lambda _: self._ncm_qr_timer.stop())
        self._ncm_qr_dialog = (dlg, status)
        self.ncm_login_status.setText("二维码已打开，等待手机确认。")
        self._ncm_qr_timer.start()
        dlg.open()

    def _poll_ncm_qr(self):
        if self._ncm_polling or not self._ncm_unikey:
            return
        self._ncm_polling = True

        def run():
            try:
                from data.sources.netease import NeteaseSource
                result = NeteaseSource({"backend": "musicbox"}).check_qr_login(self._ncm_unikey)
                self.ncm_qr_result.emit(result)
            except Exception as e:  # noqa
                self.ncm_qr_error.emit(f"登录状态检查失败：{e}")

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _verify_ncm_login() -> str:
        """校验 MusicBox 会话是否真的落地（MUSIC_U 存在），并返回登录昵称。

        仅做"确认"用：会话 cookie 由 NEMbox 自己写到
        ~/.netease-musicbox/cookie.txt，这里读取并额外拉一次账号信息拿昵称。
        任何异常都静默返回空串（不影响登录成功状态）。
        """
        try:
            from NEMbox.api import NetEase
            # cookie_path 是 Constant 类的属性，不是模块级变量；旧写法
            # `from NEMbox.const import cookie_path` 会抛 ImportError，导致
            # 永远检测不到已登录会话。这里做兜底解析。
            try:
                from NEMbox.const import Constant
                cookie_path = Constant.cookie_path
            except Exception:  # noqa
                import NEMbox.const as _nc
                cookie_path = getattr(getattr(_nc, "Constant", _nc), "cookie_path", "")
            if not cookie_path or not os.path.exists(cookie_path):
                return ""
            with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
                if "MUSIC_U" not in fh.read():
                    return ""
            info = NetEase().get_account_info()
            profile = (info or {}).get("profile") or {}
            return (profile.get("nickname") or "").strip()
        except Exception:  # noqa
            return ""

    def _refresh_ncm_login_status(self):
        """后台确认 MusicBox 本地会话是否仍有效，并刷新登录状态标签。

        网易云会话由 NEMbox 持久化在 ~/.netease-musicbox/cookie.txt，
        应用重启后会自动复用，无需重新扫码。本方法在设置对话框打开时
        调用，避免用户误以为每次都要重新登录。
        """
        if not self.ncm_on.isChecked():
            return

        def run():
            nickname = self._verify_ncm_login()
            if nickname:
                self.ncm_status_ready.emit(
                    f"✅ 已登录网易云（MusicBox 本地会话）：{nickname}，重启后自动复用，无需重新扫码。")
                return
            # 昵称拉取失败，但本地 cookie 仍在则仍视为已登录（仅昵称缺失）。
            try:
                from NEMbox.const import Constant
                cookie_path = Constant.cookie_path
            except Exception:  # noqa
                import NEMbox.const as _nc
                cookie_path = getattr(getattr(_nc, "Constant", _nc), "cookie_path", "")
            try:
                alive = bool(cookie_path) and os.path.exists(cookie_path) and (
                    "MUSIC_U" in open(cookie_path, encoding="utf-8", errors="ignore").read())
            except Exception:  # noqa
                alive = False
            if alive:
                self.ncm_status_ready.emit(
                    "✅ 网易云本地会话有效（MusicBox 已记忆登录），可直接使用；如需刷新昵称可重新扫码。")
            else:
                self.ncm_status_ready.emit("⚠ 未检测到有效登录，请点击「扫码登录网易云」。")

        threading.Thread(target=run, daemon=True).start()

    def _handle_ncm_qr_result(self, result: dict):
        self._ncm_polling = False
        code = int(result.get("code") or 0)
        texts = {801: "等待扫码…", 802: "已扫码，请在手机上确认。", 800: "二维码已过期，请重新获取。"}
        if self._ncm_qr_dialog:
            self._ncm_qr_dialog[1].setText(texts.get(code, "正在确认登录…"))
        if code != 803:
            if code == 800:
                self._ncm_qr_timer.stop()
                self.ncm_login_btn.setEnabled(True)
                self.ncm_login_status.setText(texts[800])
            return
        self._ncm_qr_timer.stop()
        self.ncm_on.setChecked(True)
        nickname = self._verify_ncm_login()
        if nickname:
            self.ncm_login_status.setText(
                f"✅ 网易云登录成功：{nickname}，已启用 MusicBox 音源。")
        else:
            self.ncm_login_status.setText(
                "✅ 网易云登录成功，已启用 MusicBox 音源。")
        self.ncm_login_btn.setEnabled(True)
        if self._ncm_qr_dialog:
            self._ncm_qr_dialog[0].accept()
        # Save the source selection immediately; MusicBox stores the actual
        # session cookie itself, so MusicAI never needs to copy it.
        user_config.data["sources"]["netease"].update({"enabled": True, "backend": "musicbox"})
        try:
            user_config.save()
            self.applied.emit()
        except Exception as e:  # noqa
            QMessageBox.warning(self, "登录成功但保存失败", f"请点击“保存”重试。\n{e}")

    def _handle_ncm_qr_error(self, message: str):
        self._ncm_polling = False
        self._ncm_qr_timer.stop()
        self.ncm_login_btn.setEnabled(True)
        self.ncm_login_status.setText("⚠ " + message)
        if self._ncm_qr_dialog:
            self._ncm_qr_dialog[1].setText(message)

    # ---------------- QQ 扫码登录 ----------------
    def _start_qq_qr_login(self):
        self.qq_login_btn.setEnabled(False)
        self.qq_login_status.setText("正在获取二维码…")
        api_base = self.qq_base.text().strip() or "http://127.0.0.1:3200"

        def run():
            try:
                from data.sources.qqmusic import QQMusicSource
                src = QQMusicSource({"api_base": api_base})
                payload = src.start_qr_login()
                self.qq_qr_ready.emit(payload)
            except Exception as e:  # noqa
                self.qq_qr_error.emit(f"获取二维码失败：{e}")

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _qq_qr_pixmap(img_data_url: str) -> QPixmap:
        """把 qq-music-api 返回的 data:image/png;base64,… 解码成 QPixmap。"""
        import base64
        s = (img_data_url or "").strip()
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        try:
            raw = base64.b64decode(s)
        except Exception:
            return QPixmap()
        pm = QPixmap()
        pm.loadFromData(QByteArray(raw))
        return pm

    def _show_qq_qr(self, payload: dict):
        self._qq_qrsig = payload.get("qrsig", "")
        self._qq_ptqrtoken = payload.get("ptqrtoken", "") or ""
        if not self._qq_qrsig:
            self._handle_qq_qr_error("获取二维码失败：响应缺少 qrsig")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("QQ音乐扫码登录")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("请使用 QQ 或 微信 扫描下方二维码，并在手机上确认登录。"))
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(self._qq_qr_pixmap(payload.get("img", "")))
        layout.addWidget(image)
        status = QLabel("等待扫码…")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(status)
        dlg.resize(330, 410)
        dlg.finished.connect(lambda _: self._qq_qr_timer.stop())
        self._qq_qr_dialog = (dlg, status)
        self.qq_login_status.setText("二维码已打开，等待手机确认。")
        self._qq_qr_timer.start()
        dlg.open()

    def _poll_qq_qr(self):
        if self._qq_polling or not self._qq_qrsig:
            return
        self._qq_polling = True

        def run():
            try:
                from data.sources.qqmusic import QQMusicSource
                api_base = self.qq_base.text().strip() or "http://127.0.0.1:3200"
                src = QQMusicSource({"api_base": api_base})
                result = src.check_qr_login(self._qq_qrsig, self._qq_ptqrtoken)
                self.qq_qr_result.emit(result)
            except Exception as e:  # noqa
                self.qq_qr_error.emit(f"登录状态检查失败：{e}")

        threading.Thread(target=run, daemon=True).start()

    def _handle_qq_qr_result(self, result: dict):
        self._qq_polling = False
        if isinstance(result, dict) and result.get("isOk"):
            session = result.get("session") or {}
            cookie = session.get("cookie") or ""
            uin = session.get("uin") or session.get("loginUin") or ""
            self._qq_qr_timer.stop()
            # 写入配置并持久化（cookie 由 DPAPI 加密），立即启用音源。
            d = user_config.data
            qq = d.setdefault("sources", {}).setdefault("qqmusic", {})
            qq["enabled"] = True
            if cookie:
                qq["cookie"] = cookie
            if uin:
                qq["uin"] = uin
            self.qq_on.setChecked(True)
            if cookie:
                self.qq_cookie.setText(cookie)
            try:
                user_config.save()
                self.applied.emit()
                self.qq_login_status.setText(
                    f"✅ QQ音乐登录成功（uin={uin or '已登录'}），已启用并保存登录态，重启后自动复用。")
            except Exception as e:  # noqa
                self.qq_login_status.setText(f"✅ 登录成功，但保存失败：{e}")
            self.qq_login_btn.setEnabled(True)
            if self._qq_qr_dialog:
                self._qq_qr_dialog[0].accept()
            return
        # 未登录：刷新 / 等待
        refresh = bool(isinstance(result, dict) and result.get("refresh"))
        msg = (result.get("message") if isinstance(result, dict) else "") or ""
        if self._qq_qr_dialog:
            self._qq_qr_dialog[1].setText(
                "二维码已失效，请重新获取。" if (refresh or "失效" in msg)
                else "等待扫码…（或在手机上确认）")
        if refresh or "失效" in msg:
            self._qq_qr_timer.stop()
            self.qq_login_btn.setEnabled(True)
            self.qq_login_status.setText(
                "⚠ 二维码已过期，请重新点击「扫码登录 QQ音乐」。")
            return
        # 仍在等待：继续轮询（该代理不区分"已扫未确认"与"未扫"）。

    def _handle_qq_qr_error(self, message: str):
        self._qq_polling = False
        self._qq_qr_timer.stop()
        self.qq_login_btn.setEnabled(True)
        self.qq_login_status.setText("⚠ " + message)
        if self._qq_qr_dialog:
            self._qq_qr_dialog[1].setText(message)

    def _refresh_qq_login_status(self):
        """打开设置时确认 QQ 登录凭据是否已配置（Cookie 持久化在本地）。"""
        if not self.qq_on.isChecked():
            return
        cookie = (user_config.sources.get("qqmusic", {}) or {}).get("cookie", "")
        if cookie:
            self.qq_login_status.setText(
                "✅ 已配置 QQ音乐登录凭据（Cookie 已保存），可直接使用；如需刷新请重新扫码。")
        else:
            self.qq_login_status.setText(
                "未检测到登录凭据，点击「扫码登录 QQ音乐」完成扫码登录。")

    def _on_save(self):
        d = user_config.data
        try:
            ai_base = validate_api_base(self.ai_base.text().strip())
            qq_base = validate_api_base(self.qq_base.text().strip())
            spotify_base = validate_api_base(self.spotify_base.text().strip())
        except ValueError as e:
            QMessageBox.warning(self, "安全设置", str(e))
            return
        d["ai"]["api_base"] = ai_base
        d["ai"]["api_key"] = self.ai_key.text().strip()
        d["ai"]["model"] = self.ai_model.currentText().strip() or "gpt-4o-mini"
        d["ai"]["backend"] = self.backend_combo.currentData() or "api"
        d["ai"]["ollama_host"] = self.ollama_host.text().strip() \
            or "http://127.0.0.1:11434"
        d["ai"]["ollama_deep"] = self.ollama_deep.text().strip() or "qwen3.5:9b"
        d["ai"]["ollama_fast"] = self.ollama_fast.text().strip() or "qwen3.5:9b"
        d["ai"]["ollama_fast_enabled"] = self.ollama_fast_enabled.isChecked()
        try:
            d["ai"]["temperature"] = float(self.temp_combo.currentText())
        except ValueError:
            d["ai"]["temperature"] = 0.8
        d["ai"]["skip_temperature"] = self.skip_temp.isChecked()
        d["ai"]["web_search"] = self.web_search.isChecked()
        d["ai"]["thinking"] = self.thinking.isChecked()
        d["ai"]["web_search_interval"] = self.web_search_interval.value()
        d["ai"]["context_window"] = self.context_window.value()

        d["match"] = {
            "fuzzy_enabled": self.fuzzy_match_enabled.isChecked(),
            "fuzzy_threshold": self.fuzzy_threshold.value(),
        }

        d["prompts"] = {
            "taste_output": self.taste_output_edit.toPlainText().strip(),
            "broadcast_output": self.broadcast_output_edit.toPlainText().strip(),
            "transition_output": self.transition_output_edit.toPlainText().strip(),
            "reco_explain": self.reco_prompt.toPlainText().strip(),
        }

        engine = self.engine_combo.currentData() or "edge"
        d["tts"]["engine"] = engine
        voice_key = self.tts_voice.currentData()
        if engine == "edge":
            d["tts"]["voice"] = voice_key or "zh-CN-XiaoxiaoNeural"
            d["tts"]["voice_id"] = ""
        elif engine == "melotts":
            d["tts"]["melotts_speaker"] = voice_key or "ZH"
            d["tts"]["voice_id"] = ""
        elif engine == "chattts":
            d["tts"]["voice_id"] = voice_key or ""
            # 保留 tts.voice 作为 edge 备用，不清除
        d["tts"]["rate"] = self.tts_rate.currentText()
        d["tts"]["volume"] = self.tts_vol.currentText()
        d["tts"]["voicevox_host"] = self.voicevox_host.text().strip() \
            or "127.0.0.1:50021"
        d["tts"]["melotts_gpu"] = self.melotts_gpu.isChecked()
        d["tts"]["segment_silence_ms"] = self.seg_silence.value()
        d["tts"]["melotts_speed"] = round(self.melo_speed.value() / 100.0, 2)
        d["tts"]["melotts_sdp_ratio"] = round(self.melo_sdp.value() / 100.0, 2)
        d["tts"]["melotts_noise_scale"] = round(self.melo_noise.value() / 100.0, 2)
        d["tts"]["melotts_noise_scale_w"] = round(self.melo_noise_w.value() / 100.0, 2)
        d["tts"]["melotts_postprocess"] = self.melo_pp.isChecked()
        d["tts"]["melotts_ffmpeg_path"] = self.melo_ffmpeg_path.text().strip()
        # ffmpeg 后处理 7 参数滑块
        d["tts"]["melotts_pp_hs1"] = round(self.melo_hs1.value() / 10.0, 1)
        d["tts"]["melotts_pp_hs2"] = round(self.melo_hs2.value() / 10.0, 1)
        d["tts"]["melotts_pp_hs3"] = round(self.melo_hs3.value() / 10.0, 1)
        d["tts"]["melotts_pp_comp_th"] = float(self.melo_comp_th.value())
        d["tts"]["melotts_pp_comp_ratio"] = round(self.melo_comp_ratio.value() / 10.0, 1)
        d["tts"]["melotts_pp_echo"] = round(self.melo_echo.value() / 100.0, 2)
        d["tts"]["melotts_pp_loud_i"] = float(self.melo_loud_i.value())
        # melotts_model_dir 由导入动作维护，此处保留（不清空）

        d["sources"]["netease"] = {
            "enabled": self.ncm_on.isChecked(),
            "backend": "musicbox",
            "musicbox_uid": self.ncm_musicbox.text().strip(),
        }
        d["sources"]["qqmusic"] = {
            "enabled": self.qq_on.isChecked(),
            "api_base": qq_base,
            "cookie": self.qq_cookie.text().strip(),
            "uin": user_config.sources.get("qqmusic", {}).get("uin", ""),
            "default_playlist_id": self.qq_pid.text().strip(),
        }
        d["sources"]["spotify"] = {
            "enabled": self.spotify_on.isChecked(),
            "api_base": spotify_base,
            "client_id": self.spotify_cid.text().strip(),
            "client_secret": self.spotify_secret.text().strip(),
            "cookie": self.spotify_cookie.text().strip(),
            "default_playlist_id": self.spotify_pid.text().strip(),
        }
        roots = [r.strip() for r in self.local_roots.toPlainText().splitlines() if r.strip()]
        d["sources"]["local"] = {
            "enabled": self.local_on.isChecked(),
            "roots": roots or list(user_config.data["sources"]["local"].get("roots", [])),
        }
        d["geo"] = {"city": self.city_edit.text().strip()}
        d["rec_pref"] = max(0, min(3, self.rec_pref_combo.currentIndex()))
        try:
            user_config.save()
        except Exception as e:
            QMessageBox.critical(
                self, "安全存储失败",
                "无法安全保存凭据。请确认 Windows 用户配置文件可用，"
                f"或改用环境变量配置。\n\n{e}")
            return
        self.applied.emit()
        self.accept()
