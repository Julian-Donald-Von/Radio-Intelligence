"""
搜歌面板（无内嵌浏览器版本）。

搜索框 + 音源下拉（按「已启用」的联网音源展示，如网易云 / QQ音乐）+ 结果列表。
结果以「封面 + 歌名 + 作者」的卡片行呈现：双击即播（实时取链，不写死 URL），
右键可「下一首播放 / 加入曲库」。封面取 track.cover（搜索时已实时取得的 URL），
后台线程下载，避免卡 UI。

（历史版本曾内嵌 QtWebEngine 浏览器用于看歌曲网页/歌词；按需求已隐藏——播放与
加库均通过 source_manager.resolve_url 实时取直链完成，无需浏览器内核。）
"""
from __future__ import annotations

import logging
import threading
from PySide6.QtCore import Qt, Signal, Slot, QByteArray, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QComboBox, QLabel, QMenu, QFrame,
)

logger = logging.getLogger("MusicAI.Search")

# 音源 key -> 展示名（仅用于下拉）
_SOURCE_LABELS = {
    "netease": "网易云",
    "qqmusic": "QQ音乐",
    "spotify": "Spotify",
}

_ROW_H = 56  # 结果行固定高度（封面 44 + 上下留白）


class SearchResultRow(QFrame):
    """搜索结果的一行：封面 + 歌名 + 作者（+ 来源小标签）。"""

    def __init__(self, track, parent=None) -> None:
        super().__init__(parent)
        self.track = track
        self._cover_token = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("SearchRow")
        self.setFixedHeight(_ROW_H)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(10)

        self.cover = QLabel()
        self.cover.setFixedSize(44, 44)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setObjectName("CoverThumb")
        self.cover.setText("🎵")  # 封面加载前的占位
        row.addWidget(self.cover)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = QLabel(track.title or "未知")
        self.title.setObjectName("TrackTitle")
        self.artist = QLabel(track.artist or "未知")
        self.artist.setObjectName("TrackArtist")
        text.addWidget(self.title)
        text.addWidget(self.artist)
        row.addLayout(text, 1)

        src = _SOURCE_LABELS.get(track.source, track.source or "")
        if src:
            tag = QLabel(src)
            tag.setObjectName("SourceTag")
            row.addWidget(tag)

    def set_cover(self, data: bytes, token=None) -> None:
        if token is not None and token != self._cover_token:
            return
        pix = QPixmap()
        if pix.loadFromData(QByteArray(data)):
            self.cover.setPixmap(
                pix.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class SearchWidget(QWidget):
    """搜歌面板：搜索 + 结果（封面 + 歌名 + 作者）。"""

    play_requested = Signal(object)          # Track：双击即播
    next_play_requested = Signal(object)     # Track：下一首播放
    add_library_requested = Signal(object)   # Track：加入曲库（我喜欢的音乐）
    results_ready = Signal(list)             # 后台搜索完成 → 主线程填充
    cover_loaded = Signal(int, bytes)        # 后台封面下载完成（token, bytes）

    def __init__(self, source_manager, parent=None) -> None:
        super().__init__(parent)
        self.source_manager = source_manager
        self._results: list = []
        self._rows: dict = {}                # track.id -> SearchResultRow
        self.cover_loaded.connect(self._on_cover_loaded)
        self.results_ready.connect(self._populate)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ---- 搜索栏 ----
        bar = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("搜歌：歌名 / 歌手 / 歌词片段…（回车或点搜索）")
        self.input.returnPressed.connect(self._on_search)
        self.search_btn = QPushButton("🔍 搜索")
        self.search_btn.clicked.connect(self._on_search)
        self.src_combo = QComboBox()
        for key, src in self.source_manager.sources.items():
            if key == "local":
                continue
            if getattr(src, "enabled", False):
                self.src_combo.addItem(_SOURCE_LABELS.get(key, key), key)
        if self.src_combo.count() == 0:
            # 兜底：若一个音源都没启用也至少能搜网易云（enabled 判定失败时）
            self.src_combo.addItem("网易云", "netease")
        bar.addWidget(self.input, 1)
        bar.addWidget(self.src_combo)
        bar.addWidget(self.search_btn)
        layout.addLayout(bar)

        hint = QLabel("双击结果即播（实时取链）· 右键可「下一首播放 / 加入曲库」")
        hint.setObjectName("Sub")
        layout.addWidget(hint)

        # ---- 结果列表（封面 + 歌名 + 作者）----
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(self._on_activate)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._on_context)
        self.results.setSpacing(2)
        layout.addWidget(self.results, 1)

    # ---------------- 搜索 ----------------
    def _on_search(self) -> None:
        kw = self.input.text().strip()
        if not kw:
            return
        self.results.clear()
        self.results.addItem("搜索中…")
        src_key = self.src_combo.currentData() or "netease"
        threading.Thread(target=self._do_search, args=(kw, src_key), daemon=True).start()

    def _do_search(self, kw: str, src_key: str) -> None:
        tracks: list = []
        try:
            src = self.source_manager.get(src_key)
            if src is not None:
                tracks = src.search(kw) or []
        except Exception as e:  # noqa
            logger.warning("搜歌失败(%s): %s", src_key, e)
            tracks = []
        self.results_ready.emit(tracks)

    @Slot(list)
    def _populate(self, tracks: list) -> None:
        self._results = list(tracks)
        self._rows = {}
        self.results.clear()
        if not tracks:
            self.results.addItem("未找到结果")
            return
        for t in tracks:
            item = QListWidgetItem(self.results)
            row = SearchResultRow(t)
            item.setSizeHint(QSize(self.results.width() or 600, _ROW_H))
            self.results.setItemWidget(item, row)
            self._rows[t.id] = row
            if t.cover:
                self._fetch_cover(t)

    def _track_by_item(self, item: QListWidgetItem):
        """从某个结果行的 QListWidgetItem 取回对应 Track。"""
        w = self.results.itemWidget(item)
        if isinstance(w, SearchResultRow):
            return w.track
        return None

    # ---------------- 封面（实时下载，后台线程） ----------------
    def _fetch_cover(self, track) -> None:
        url = getattr(track, "cover", None)
        if not url:
            return
        token = track.id
        row = self._rows.get(token)
        if row is not None:
            row._cover_token = token  # 记录 token 防串台

        def worker():
            from utils.cover_cache import fetch_cover_bytes
            data = fetch_cover_bytes(url, getattr(track, "source", None))
            if data:
                self.cover_loaded.emit(token, data)
        threading.Thread(target=worker, daemon=True).start()

    @Slot(int, bytes)
    def _on_cover_loaded(self, token: int, data: bytes) -> None:
        row = self._rows.get(token)
        if row is not None:
            row.set_cover(data, token)

    # ---------------- 交互 ----------------
    def _on_activate(self, item: QListWidgetItem) -> None:
        t = self._track_by_item(item)
        if t is not None:
            self.play_requested.emit(t)

    def _on_context(self, pos) -> None:
        item = self.results.itemAt(pos)
        t = self._track_by_item(item) if item is not None else None
        if t is None:
            return
        menu = QMenu(self)
        menu.addAction("▶ 播放", lambda: self.play_requested.emit(t))
        menu.addAction("⏭ 下一首播放", lambda: self.next_play_requested.emit(t))
        menu.addAction("➕ 加入曲库", lambda: self.add_library_requested.emit(t))
        menu.exec(self.results.mapToGlobal(pos))
