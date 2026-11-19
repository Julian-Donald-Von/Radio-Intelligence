"""
可复用控件：底部播放控制条、正在播放面板、歌词、AI 口播对话框、本地曲库（含浏览）。
视觉风格参考 AIStarRadio：浅色卡片、清晰布局。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPoint, QSize, QByteArray
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel, QSlider,
    QListWidget, QListWidgetItem, QFrame, QComboBox, QLineEdit,
    QTextBrowser, QFileDialog, QSplitter, QSizePolicy, QMenu,
    QStackedWidget, QButtonGroup, QScrollArea,
)
from PySide6.QtGui import QFont, QTextDocument, QCursor, QPixmap, QIcon
import html
import time
import threading

from core.lyrics import parse_lrc
from data.playlist_manager import PlaylistManager


class PlayerControls(QFrame):
    """底部通栏播放控制条：歌曲信息 + 进度 + 播放/上下首 + 音量。"""
    play_toggled = Signal()
    prev_clicked = Signal()
    next_clicked = Signal()
    seek_requested = Signal(int)        # ms
    volume_changed = Signal(float)      # 0~1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("PlayerBar")
        self._total_ms = 0
        self._seeking = False        # 拖动进度条期间为 True，暂停自动回写
        self._build()

    def _build(self):
        # 顶部：当前歌曲信息
        info = QHBoxLayout()
        self.cover = QLabel("🎵")
        self.cover.setStyleSheet("font-size: 22px;")
        self.title = QLabel("未在播放")
        self.title.setObjectName("Title")
        self.title.setStyleSheet("font-size: 15px;")
        self.sub = QLabel("")
        self.sub.setObjectName("Sub")
        info.addWidget(self.cover)
        info.addWidget(self.title)
        info.addWidget(self.sub)
        info.addStretch()

        # 进度
        prog = QHBoxLayout()
        self.progress = QSlider(Qt.Horizontal)
        self.progress.setObjectName("Progress")
        # 拖动期间暂停自动进度回写：否则播放进度定时器会不断把滑块拽回当前
        # 播放位置，表现为"拖不动"。按下置 _seeking，松开才真正 seek。
        self.progress.sliderPressed.connect(self._on_seek_press)
        self.progress.sliderReleased.connect(self._on_seek_release)
        self.progress.sliderMoved.connect(self._on_seek_move)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("Sub")
        prog.addWidget(self.time_label)
        prog.addWidget(self.progress, 1)

        # 控制按钮
        row = QHBoxLayout()
        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setObjectName("Icon")
        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("PlayBtn")
        self.next_btn = QPushButton("⏭")
        self.next_btn.setObjectName("Icon")
        self.prev_btn.clicked.connect(lambda: self.prev_clicked.emit())
        self.play_btn.clicked.connect(lambda: self.play_toggled.emit())
        self.next_btn.clicked.connect(lambda: self.next_clicked.emit())
        row.addStretch()
        row.addWidget(self.prev_btn)
        row.addWidget(self.play_btn)
        row.addWidget(self.next_btn)
        row.addStretch()

        # 音量
        vol = QHBoxLayout()
        vol.addStretch()
        vol_label = QLabel("🔊")
        vol_label.setObjectName("Sub")
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setMaximum(100)
        self.vol_slider.setValue(85)
        self.vol_slider.setFixedWidth(140)
        self.vol_slider.valueChanged.connect(lambda v: self.volume_changed.emit(v / 100.0))
        vol.addWidget(vol_label)
        vol.addWidget(self.vol_slider)
        vol.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addLayout(info)
        layout.addLayout(prog)
        layout.addLayout(row)
        layout.addLayout(vol)

    def set_song_info(self, title: str, sub: str = "") -> None:
        self.title.setText(title or "未在播放")
        self.sub.setText(sub or "")

    def set_playing(self, playing: bool):
        self.play_btn.setText("⏸" if playing else "▶")

    def set_progress(self, ms: int):
        if self._seeking:
            return                      # 拖动中：不回写，避免把滑块拽回播放位置
        if self._total_ms:
            self.progress.setValue(ms)
        self.time_label.setText(f"{_fmt(ms)} / {_fmt(self._total_ms)}")

    # ---------- 进度拖动 ----------
    def _on_seek_press(self) -> None:
        self._seeking = True

    def _on_seek_release(self) -> None:
        self._seeking = False
        self.seek_requested.emit(self.progress.value())

    def _on_seek_move(self, value: int) -> None:
        # 拖动过程中实时刷新时间显示，但不触发 seek（避免频繁跳转）
        if self._total_ms:
            self.time_label.setText(f"{_fmt(value)} / {_fmt(self._total_ms)}")

    def set_total(self, ms: int):
        self._total_ms = ms
        self.progress.setMaximum(ms)

    def set_volume(self, v: float):
        self.vol_slider.setValue(int(v * 100))


class NowPlaying(QFrame):
    """封面 + 标题 + 歌手 + 收藏按钮。"""
    favorite_toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        self.cover = QLabel("🎵")
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet("font-size:48px;")
        self._cover_token = None     # 当前封面归属（ext_id）；防止异步下载串台
        self.title = QLabel("未在播放")
        self.title.setObjectName("Title")
        self.title.setWordWrap(True)
        self.sub = QLabel("")
        self.sub.setObjectName("Sub")
        self.fav_btn = QPushButton("♡ 收藏")
        self.fav_btn.setObjectName("AddBtn")
        self.fav_btn.clicked.connect(lambda: self.favorite_toggled.emit(
            self.fav_btn.text().startswith("♡")))
        layout.addWidget(self.cover, alignment=Qt.AlignCenter)
        layout.addWidget(self.title, alignment=Qt.AlignCenter)
        layout.addWidget(self.sub, alignment=Qt.AlignCenter)
        layout.addWidget(self.fav_btn, alignment=Qt.AlignCenter)

    def set_track(self, title: str, sub: str, favorited: bool = False,
                  cover_token=None):
        self.title.setText(title)
        self.sub.setText(sub)
        self.fav_btn.setText("♥ 已收藏" if favorited else "♡ 收藏")
        # 仅当切歌（封面归属 token 变化）才把封面复位为占位 emoji，避免单纯切换
        # 收藏状态时把已经显示好的封面清回 🎵（点 NowPlaying 红心会再次调用本方法，
        # 此时 token 不变 → 保留当前封面，由调用方决定是否重新拉取）。
        if cover_token != self._cover_token:
            self._cover_token = cover_token
            self.cover.setPixmap(QPixmap())
            self.cover.setText("🎵")
            self.cover.setStyleSheet("font-size:48px;")

    def set_cover(self, data: bytes, token=None) -> None:
        """把下载到的封面图片字节设到封面标签；token 不符说明已切歌，丢弃。"""
        if token is not None and token != self._cover_token:
            return
        pix = QPixmap()
        if pix.loadFromData(QByteArray(data)):
            self.cover.setPixmap(
                pix.scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class BroadcastDialog(QTextBrowser):
    """AI 口播对话框：以"聊天"形式逐条显示 DJ 口播文案与系统提示。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Broadcast")
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.document().setDocumentMargin(10)
        self.document().setDefaultStyleSheet(
            "div.msg{padding:7px 10px;margin:6px 2px;border-radius:9px;line-height:1.45;}"
            "div.dj{background:#e8f0fe;border-left:3px solid #4a8cff;}"
            "div.sys{background:#fbf3da;border-left:3px solid #d9a400;}"
            "b.name{color:#2f6fd0;} b.sysname{color:#a87b00;}"
            "span.ts{color:#9aa0a6;font-size:11px;margin-right:4px;}"
        )
        self.add_message("DJ", "DJ 待命中…欢迎来到 AI 智能电台，点一首歌，我会为你口播介绍。")

    def add_message(self, role: str, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        if role == "DJ":
            cls, icon, name = "dj", "🎙", "DJ 口播"
            name_tag = '<b class="name">%s</b>' % icon
        else:
            cls, icon, name = "sys", "ℹ️", "系统"
            name_tag = '<b class="sysname">%s</b>' % icon
        body = html.escape(text).replace("\n", "<br/>")
        frag = (
            f'<div class="msg {cls}">'
            f'<span class="ts">[{ts}]</span>{name_tag} {name}<br/>{body}'
            f'</div>'
        )
        self.append(frag)
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


class TrackList(QListWidget):
    """通用曲目列表（收藏/歌单/搜索结果/曲库歌曲共用）。"""
    track_activated = Signal(int)   # track id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ids: list[int] = []
        self.itemDoubleClicked.connect(self._on_activate)

    def load(self, tracks, display_fn=str):
        self.clear()
        self._ids = []
        for t in tracks:
            item = QListWidgetItem(display_fn(t))
            self.addItem(item)
            self._ids.append(getattr(t, "id", -1))

    def _on_activate(self, item):
        idx = self.row(item)
        if 0 <= idx < len(self._ids):
            self.track_activated.emit(self._ids[idx])


class SearchBar(QWidget):
    searched = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        self.box = QLineEdit()
        self.box.setPlaceholderText("搜索歌曲 / 歌手 / 专辑")
        self.box.returnPressed.connect(lambda: self.searched.emit(self.box.text()))
        layout.addWidget(self.box)


def _fmt(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


class LyricsView(QListWidget):
    """歌词面板：展示 LRC 文本并高亮当前行。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Lyrics")
        self.setWordWrap(True)
        self.addItem("（暂无歌词）")

    def set_lyrics(self, text: str | None) -> None:
        self.clear()
        for _, line in parse_lrc(text):
            self.addItem(line)
        if self.count() == 0:
            self.addItem("（暂无歌词）")

    def highlight(self, idx: int) -> None:
        if 0 <= idx < self.count():
            self.setCurrentRow(idx)
            self.scrollToItem(self.item(idx))


class LocalLibraryWidget(QWidget):
    """本地曲库：浏览模式（全部 / 按歌手 / 按专辑）+ 分类列表 + 歌曲列表 + 添加按钮。

    选择后通过信号把路径交给主窗口入库（扫描/元数据入库逻辑集中在主窗口）。
    """
    track_activated = Signal(int)
    add_paths_requested = Signal(list)     # 选择的具体文件
    add_folder_requested = Signal(str)     # 选择的文件夹

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all: list = []
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 顶部标题 + 添加按钮
        bar = QHBoxLayout()
        title = QLabel("曲库")
        title.setObjectName("HeaderLabel")
        self.add_files_btn = QPushButton("＋ 添加文件")
        self.add_folder_btn = QPushButton("＋ 添加文件夹")
        self.add_files_btn.setObjectName("AddBtn")
        self.add_folder_btn.setObjectName("AddBtn")
        bar.addWidget(title)
        bar.addStretch()
        bar.addWidget(self.add_files_btn)
        bar.addWidget(self.add_folder_btn)
        layout.addLayout(bar)

        # 浏览模式 + 搜索
        br = QHBoxLayout()
        br.addWidget(QLabel("浏览："))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["全部歌曲", "按歌手浏览", "按专辑浏览"])
        br.addWidget(self.mode_combo)
        self.count_label = QLabel("共 0 首")
        self.count_label.setObjectName("Sub")
        br.addWidget(self.count_label)
        br.addStretch()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索歌曲 / 歌手 / 专辑")
        self.search_box.setMaximumWidth(220)
        br.addWidget(self.search_box)
        layout.addLayout(br)

        # 分类 + 歌曲 列表
        split = QSplitter(Qt.Horizontal)
        self.cat_list = QListWidget()
        self.cat_list.setMaximumWidth(220)
        self.song_list = TrackList()
        split.addWidget(self.cat_list)
        split.addWidget(self.song_list)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)

        # 信号
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.search_box.returnPressed.connect(self._on_search)
        self.cat_list.itemClicked.connect(self._on_category_clicked)
        self.song_list.track_activated.connect(self.track_activated.emit)
        self.add_files_btn.clicked.connect(self._pick_files)
        self.add_folder_btn.clicked.connect(self._pick_folder)

    # ---------------- 对外接口 ----------------
    def load_tracks(self, tracks) -> None:
        self._all = list(tracks)
        self._refresh()

    def show_tracks(self, tracks) -> None:
        """搜索 / 外部结果：扁平展示，并回到"全部"模式。"""
        self.mode_combo.setCurrentIndex(0)
        self.cat_list.clear()
        self.song_list.load(tracks, lambda t: t.display)
        self.count_label.setText(f"共 {len(tracks)} 首")

    # ---------------- 内部 ----------------
    def _refresh(self):
        mode = self.mode_combo.currentIndex()
        self.cat_list.clear()
        if mode == 0:
            self.song_list.load(self._all, lambda t: t.display)
            self.count_label.setText(f"共 {len(self._all)} 首")
            return
        seen: dict = {}
        keyfn = (lambda t: t.artist or "未知歌手") if mode == 1 else (lambda t: t.album or "未知专辑")
        for t in self._all:
            seen[keyfn(t)] = seen.get(keyfn(t), 0) + 1
        for k in sorted(seen):
            item = QListWidgetItem(f"{k}（{seen[k]}）")
            item.setData(Qt.ItemDataRole.UserRole, k)
            self.cat_list.addItem(item)
        self.song_list.load(self._all, lambda t: t.display)
        self.count_label.setText(f"共 {len(self._all)} 首")

    def _on_mode_changed(self):
        self._refresh()

    def _on_category_clicked(self, item):
        k = item.data(Qt.ItemDataRole.UserRole)
        mode = self.mode_combo.currentIndex()
        if mode == 1:
            sel = [t for t in self._all if (t.artist or "未知歌手") == k]
        else:
            sel = [t for t in self._all if (t.album or "未知专辑") == k]
        self.song_list.load(sel, lambda t: t.display)

    def _on_search(self):
        kw = self.search_box.text().strip().lower()
        if not kw:
            self._refresh()
            return
        res = [t for t in self._all if kw in t.title.lower() or kw in t.artist.lower()
               or kw in t.album.lower() or kw in t.genre.lower()]
        self.song_list.load(res, lambda t: t.display)
        self.count_label.setText(f"共 {len(res)} 首")

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择音乐文件", "",
            "音频文件 (*.mp3 *.flac *.m4a *.ogg *.wav *.wma *.aac);;所有文件 (*.*)")
        if files:
            self.add_paths_requested.emit(files)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择音乐文件夹")
        if folder:
            self.add_folder_requested.emit(folder)


# =========================================================================
# 推荐 / 播放队列：列表行 + 列表 + 桌面悬浮窗
# =========================================================================
class QueueRow(QWidget):
    """推荐队列中的单行：标题 + 红心（收藏）+ 叉号（不播放）+ 踩（不喜欢）。"""

    def __init__(self, entry: dict, parent=None) -> None:
        super().__init__(parent)
        self.track_id = entry["id"]
        self._fav = bool(entry.get("favorited"))
        self._dis = bool(entry.get("disliked", False))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self.title = QLabel(f"{entry['title']} — {entry['artist']}")
        self.title.setObjectName("QueueTitle")
        self.title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.title.setWordWrap(True)
        layout.addWidget(self.title, stretch=1)

        self.heart = QPushButton("♥" if self._fav else "♡")
        self.heart.setObjectName("QueueHeart" if self._fav else "QueueHeartOff")
        self.heart.setFixedSize(26, 26)
        self.heart.setCursor(QCursor(Qt.PointingHandCursor))
        layout.addWidget(self.heart)

        self.dislike = QPushButton("👎" if self._dis else "👍")
        self.dislike.setObjectName("QueueDislike" if self._dis else "QueueDislikeOff")
        self.dislike.setFixedSize(26, 26)
        self.dislike.setCursor(QCursor(Qt.PointingHandCursor))
        self.dislike.setToolTip("不喜欢（加入隐藏的不喜欢库，AI 不再推荐）"
                                if not self._dis else "撤销不喜欢")
        layout.addWidget(self.dislike)

        self.xbtn = QPushButton("✕")
        self.xbtn.setObjectName("QueueX")
        self.xbtn.setFixedSize(26, 26)
        self.xbtn.setCursor(QCursor(Qt.PointingHandCursor))
        self.xbtn.setToolTip("不播放（仅移除，不标记不喜欢）")
        layout.addWidget(self.xbtn)

        if entry.get("current"):
            self.setObjectName("QueueRowCurrent")

    def set_favorited(self, fav: bool) -> None:
        self._fav = fav
        self.heart.setText("♥" if fav else "♡")
        self.heart.setObjectName("QueueHeart" if fav else "QueueHeartOff")
        self.heart.setStyleSheet(self.heart.styleSheet())  # 触发 objectName 样式刷新

    def set_disliked(self, dis: bool) -> None:
        self._dis = dis
        self.dislike.setText("👎" if dis else "👍")
        self.dislike.setObjectName("QueueDislike" if dis else "QueueDislikeOff")
        self.dislike.setStyleSheet(self.dislike.styleSheet())  # 触发 objectName 样式刷新
        self.dislike.setToolTip("撤销不喜欢" if dis else
                                "不喜欢（加入隐藏的不喜欢库，AI 不再推荐）")


class QueueListWidget(QListWidget):
    """可滚动的推荐/播放队列，带红心收藏、叉号不播放、踩（不喜欢）。"""
    favorite_clicked = Signal(int, bool)   # track_id, 期望的收藏状态
    remove_clicked = Signal(int)           # track_id（不播放）
    dislike_clicked = Signal(int, bool)    # track_id, 是否标记不喜欢
    row_activated = Signal(int)             # 双击播放

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("QueueList")
        self.setSpacing(4)
        self._rows: list[QWidget] = []

    def mouseDoubleClickEvent(self, event) -> None:
        """双击某行播放。直接按坐标命中行，避免 setItemWidget 后
        itemDoubleClicked 信号不触发导致双击失效。落在 ♥/✕ 按钮上时不触发播放。"""
        index = self.indexAt(event.pos())
        if index.isValid():
            row = self.itemWidget(self.item(index.row()))
            if isinstance(row, QueueRow):
                child = row.childAt(row.mapFromParent(event.pos()))
                if not isinstance(child, QPushButton):
                    self.row_activated.emit(row.track_id)
        super().mouseDoubleClickEvent(event)

    def set_entries(self, entries: list[dict]) -> None:
        self.clear()
        self._rows = []
        for e in entries:
            item = QListWidgetItem()
            row = QueueRow(e)
            row.heart.clicked.connect(
                lambda _=False, tid=e["id"], fav=not row_fav(e):  # noqa
                self.favorite_clicked.emit(tid, fav))
            row.xbtn.clicked.connect(
                lambda _=False, tid=e["id"]: self.remove_clicked.emit(tid))
            row.dislike.clicked.connect(
                lambda _=False, tid=e["id"], dis=not e.get("disliked", False):
                self.dislike_clicked.emit(tid, dis))
            self.addItem(item)
            self.setItemWidget(item, row)
            self._rows.append(row)
        sb = self.verticalScrollBar()
        sb.setValue(0)

    def refresh_fav_states(self, entries: list[dict]) -> None:
        # 仅更新红心/踩状态，避免整列表重建导致闪烁
        for row, e in zip(self._rows, entries):
            if isinstance(row, QueueRow) and row.track_id == e["id"]:
                row.set_favorited(bool(e.get("favorited")))
                row.set_disliked(bool(e.get("disliked", False)))


def row_fav(entry: dict) -> bool:
    return bool(entry.get("favorited"))


def fav_key(track) -> str:
    """收藏状态稳定键（与 ai/radio.py 的 fav_key 逻辑保持一致）：

    在线歌（netease/qq/spotify）用 音源+原生 id，本地/无 ext_id 用 db id。
    用于历史列表按稳定键判定红心，避免在线歌每次推荐重建临时 id 导致红心丢失。
    """
    src = (getattr(track, "source", None) or "")
    ext = getattr(track, "ext_id", None)
    if src in ("netease", "qqmusic", "spotify") and ext not in (None, ""):
        return f"e:{src}:{ext}"
    return f"i:{getattr(track, 'id', None)}"


# =========================================================================
# 推荐页货架：AI 常驻思考的待荐列表（与播放队列解耦），带播放按钮 + 加载态
# =========================================================================
class RecoRow(QWidget):
    """推荐货架单行：标题 + ♥ 收藏 + ✕ 不播放 + 👎 不喜欢 + ▶ 播放。"""

    play_clicked = Signal(int)            # track_id
    favorite_clicked = Signal(int, bool)  # track_id, 期望收藏状态
    remove_clicked = Signal(int)          # track_id（不播放）
    dislike_clicked = Signal(int, bool)   # track_id, 是否标记不喜欢

    def __init__(self, entry: dict, parent=None) -> None:
        super().__init__(parent)
        self.track_id = entry["id"]
        self._fav = bool(entry.get("favorited"))
        self._dis = bool(entry.get("disliked", False))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        title = QLabel(f"{entry['title']} — {entry['artist']}")
        title.setObjectName("QueueTitle")
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        title.setWordWrap(True)
        layout.addWidget(title, stretch=1)

        self.heart = QPushButton("♥" if self._fav else "♡")
        self.heart.setObjectName("QueueHeart" if self._fav else "QueueHeartOff")
        self.heart.setFixedSize(26, 26)
        self.heart.setCursor(QCursor(Qt.PointingHandCursor))
        self.heart.setToolTip("收藏到曲库（我喜欢的音乐）")
        self.heart.clicked.connect(
            lambda: self.favorite_clicked.emit(self.track_id, not self._fav))
        layout.addWidget(self.heart)

        self.dislike = QPushButton("👎" if self._dis else "👍")
        self.dislike.setObjectName("QueueDislike" if self._dis else "QueueDislikeOff")
        self.dislike.setFixedSize(26, 26)
        self.dislike.setCursor(QCursor(Qt.PointingHandCursor))
        self.dislike.setToolTip("不喜欢（加入隐藏的不喜欢库，AI 不再推荐）"
                                if not self._dis else "撤销不喜欢")
        self.dislike.clicked.connect(
            lambda: self.dislike_clicked.emit(self.track_id, not self._dis))
        layout.addWidget(self.dislike)

        self.xbtn = QPushButton("✕")
        self.xbtn.setObjectName("QueueX")
        self.xbtn.setFixedSize(26, 26)
        self.xbtn.setCursor(QCursor(Qt.PointingHandCursor))
        self.xbtn.setToolTip("不播放（仅移除，不标记不喜欢）")
        self.xbtn.clicked.connect(
            lambda: self.remove_clicked.emit(self.track_id))
        layout.addWidget(self.xbtn)

        play_btn = QPushButton("▶")
        play_btn.setObjectName("RecoPlay")
        play_btn.setFixedSize(28, 28)
        play_btn.setCursor(QCursor(Qt.PointingHandCursor))
        play_btn.setToolTip("播放（整批推荐进入播放列表）")
        play_btn.clicked.connect(lambda: self.play_clicked.emit(self.track_id))
        layout.addWidget(play_btn)

    def set_favorited(self, fav: bool) -> None:
        self._fav = fav
        self.heart.setText("♥" if fav else "♡")
        self.heart.setObjectName("QueueHeart" if fav else "QueueHeartOff")
        self.heart.setStyleSheet(self.heart.styleSheet())  # 触发 objectName 样式刷新

    def set_disliked(self, dis: bool) -> None:
        self._dis = dis
        self.dislike.setText("👎" if dis else "👍")
        self.dislike.setObjectName("QueueDislike" if dis else "QueueDislikeOff")
        self.dislike.setStyleSheet(self.dislike.styleSheet())  # 触发 objectName 样式刷新
        self.dislike.setToolTip("撤销不喜欢" if dis else
                                "不喜欢（加入隐藏的不喜欢库，AI 不再推荐）")


class RecoListWidget(QListWidget):
    """推荐货架列表：展示 AI 推荐的待播歌曲，双击或点 ▶ 播放，♥ 收藏 / ✕ 不播放 / 👎 不喜欢。"""

    play_requested = Signal(int)            # track_id
    favorite_clicked = Signal(int, bool)    # track_id, 期望收藏状态
    remove_clicked = Signal(int)            # track_id（不播放）
    dislike_clicked = Signal(int, bool)     # track_id, 是否标记不喜欢
    play_all_requested = Signal()  # 播放全部

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("QueueList")
        self.setSpacing(4)
        self._rows: list[QWidget] = []

    def mouseDoubleClickEvent(self, event) -> None:
        """双击某行播放（与点 ▶ 同语义）。按坐标命中行，避免 setItemWidget 后
        itemDoubleClicked 不触发；落在 ♥/✕/▶ 按钮上时不重复触发播放。"""
        index = self.indexAt(event.pos())
        if index.isValid():
            row = self.itemWidget(self.item(index.row()))
            if isinstance(row, RecoRow):
                child = row.childAt(row.mapFromParent(event.pos()))
                if not isinstance(child, QPushButton):
                    self.play_requested.emit(row.track_id)
        super().mouseDoubleClickEvent(event)

    def set_entries(self, entries: list[dict]) -> None:
        self.clear()
        self._rows = []
        for e in entries:
            item = QListWidgetItem()
            row = RecoRow(e)
            row.play_clicked.connect(self.play_requested)
            row.favorite_clicked.connect(self.favorite_clicked)
            row.remove_clicked.connect(self.remove_clicked)
            row.dislike_clicked.connect(self.dislike_clicked)
            self.addItem(item)
            self.setItemWidget(item, row)
            self._rows.append(row)
        sb = self.verticalScrollBar()
        sb.setValue(0)




class DesktopQueueWindow(QWidget):
    """桌面悬浮窗：无边框、置顶、半透明，显示推荐/播放队列，可拖动。"""
    favorite_clicked = Signal(int, bool)
    remove_clicked = Signal(int)
    dislike_clicked = Signal(int, bool)
    row_activated = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(300, 380)
        self.resize(320, 460)
        self._drag_pos: QPoint | None = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 玻璃卡片容器
        card = QFrame()
        card.setObjectName("DeskCard")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(10, 10, 10, 10)
        cv.setSpacing(8)

        # 顶部拖动条 + 关闭
        bar = QHBoxLayout()
        self.title = QLabel("🎵 播放队列")
        self.title.setObjectName("DeskTitle")
        bar.addWidget(self.title)
        bar.addStretch()
        self.hide_btn = QPushButton("—")
        self.hide_btn.setObjectName("DeskBtn")
        self.hide_btn.setFixedSize(24, 24)
        self.hide_btn.setToolTip("隐藏（可在主窗口工具栏重新打开）")
        self.hide_btn.clicked.connect(self.hide)
        bar.addWidget(self.hide_btn)
        cv.addLayout(bar)

        self.list = QueueListWidget()
        cv.addWidget(self.list, stretch=1)

        hint = QLabel("双击播放 · ♥ 收藏 · ✕ 不播放 · 👎 不喜欢")
        hint.setObjectName("DeskHint")
        cv.addWidget(hint)

        root.addWidget(card)

        # 信号透传
        self.list.favorite_clicked.connect(self.favorite_clicked)
        self.list.remove_clicked.connect(self.remove_clicked)
        self.list.dislike_clicked.connect(self.dislike_clicked)
        self.list.row_activated.connect(self.row_activated)

    def set_entries(self, entries: list[dict]) -> None:
        self.list.set_entries(entries)

    # ---- 拖动 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class SourcePanel(QWidget):
    """通用音源面板（网易云 / QQ音乐 等联网音源复用）。"""
    tracks_loaded = Signal(list)
    track_activated = Signal(int)
    status_changed = Signal(str)

    def __init__(self, key: str, source, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self.source = source
        self._build()

    def _build(self):
        from PySide6.QtWidgets import QPushButton
        layout = QVBoxLayout(self)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.addWidget(QLabel(f"{self.source.label}（需本机/自有服务）"))
        self.api_edit = QLineEdit()
        self.api_edit.setPlaceholderText("服务地址，如 http://127.0.0.1:3000")
        self.api_edit.setText(self.source.cfg.get("api_base", ""))
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setPlaceholderText("Cookie（可选，提升权限）")
        self.cookie_edit.setText(self.source.cfg.get("cookie", ""))
        self.pid_edit = QLineEdit()
        self.pid_edit.setPlaceholderText("歌单 ID（可选，加载默认歌单）")
        self.pid_edit.setText(self.source.cfg.get("default_playlist_id", ""))
        # Spotify：官方 Client Credentials（可选，用于 Bearer 鉴权）
        if self.key == "spotify":
            self.client_id_edit = QLineEdit()
            self.client_id_edit.setPlaceholderText("Client ID（可选·官方 Client Credentials）")
            self.client_id_edit.setText(self.source.cfg.get("client_id", ""))
            self.client_secret_edit = QLineEdit()
            self.client_secret_edit.setEchoMode(QLineEdit.Password)
            self.client_secret_edit.setPlaceholderText("Client Secret（可选）")
            self.client_secret_edit.setText(self.source.cfg.get("client_secret", ""))
            cl.addWidget(self.client_id_edit)
            cl.addWidget(self.client_secret_edit)
        # 网易云：音乐盒（用 uid 导入"我喜欢的音乐"）
        if self.key == "netease":
            self.musicbox_edit = QLineEdit()
            self.musicbox_edit.setPlaceholderText("网易云用户ID（音乐盒：导入我喜欢）")
            self.musicbox_edit.setText(self.source.cfg.get("musicbox_uid", ""))
            self.musicbox_btn = QPushButton("导入音乐盒")
            self.musicbox_btn.setObjectName("AddBtn")
            self.musicbox_btn.clicked.connect(self._import_musicbox)
            cl.addWidget(self.musicbox_edit)
            cl.addWidget(self.musicbox_btn)
        self.load_btn = QPushButton("加载歌单")
        self.load_btn.setObjectName("AddBtn")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索歌曲 / 歌手")
        self.status = QLabel("")
        self.status.setObjectName("Sub")
        self.list = TrackList()

        # 网易云已简化为仅 MusicBox 扫码 + UID 导入，隐藏 HTTP 服务相关字段
        if self.key == "netease":
            self.api_edit.hide()
            self.cookie_edit.hide()
            self.pid_edit.hide()
            self.load_btn.hide()
        else:
            cl.addWidget(self.api_edit)
            cl.addWidget(self.cookie_edit)
            cl.addWidget(self.pid_edit)
            cl.addWidget(self.load_btn)
        cl.addWidget(self.search_edit)
        cl.addWidget(self.status)
        layout.addWidget(card)
        layout.addWidget(self.list, stretch=1)

        self.load_btn.clicked.connect(self._load)
        self.search_edit.returnPressed.connect(self._search)
        self.list.track_activated.connect(self.track_activated.emit)

    def _apply_cfg(self) -> None:
        base = self.api_edit.text().strip()
        cookie = self.cookie_edit.text().strip()
        pid = self.pid_edit.text().strip()
        self.source.cfg["api_base"] = base
        self.source.cfg["cookie"] = cookie
        self.source.cfg["default_playlist_id"] = pid
        if hasattr(self.source, "api_base"):
            self.source.api_base = base.rstrip("/")
        if hasattr(self.source, "session"):
            if cookie:
                self.source.session.headers.update({"Cookie": cookie})
            else:
                self.source.session.headers.pop("Cookie", None)
        # Spotify 凭据
        if hasattr(self, "client_id_edit"):
            cid = self.client_id_edit.text().strip()
            secret = self.client_secret_edit.text().strip()
            self.source.cfg["client_id"] = cid
            self.source.cfg["client_secret"] = secret
            if hasattr(self.source, "set_credentials"):
                self.source.set_credentials(cid, secret)
        # 网易云音乐盒 uid
        if hasattr(self, "musicbox_edit"):
            self.source.cfg["musicbox_uid"] = self.musicbox_edit.text().strip()

    def _import_musicbox(self) -> None:
        self._apply_cfg()
        uid = self.musicbox_edit.text().strip()
        if not uid:
            self.status.setText("请输入网易云用户ID")
            return
        self.status.setText("导入音乐盒中…")
        import threading
        threading.Thread(target=self._do_import_musicbox, args=(uid,), daemon=True).start()

    def _do_import_musicbox(self, uid) -> None:
        try:
            tracks = self.source.musicbox(uid)
            if tracks:
                self.tracks_loaded.emit(tracks)
                self.status_changed.emit(f"音乐盒：载入 {len(tracks)} 首")
            else:
                self.status_changed.emit("音乐盒为空或失败（检查 uid / 服务地址）")
        except Exception as e:  # noqa
            self.status_changed.emit(f"音乐盒失败：{e}")

    def _load(self) -> None:
        self._apply_cfg()
        pid = self.pid_edit.text().strip()
        if not pid:
            self.status.setText("请输入歌单 ID")
            return
        self.status.setText("请求歌单中…")
        import threading
        threading.Thread(target=self._do_load, args=(pid,), daemon=True).start()

    def _do_load(self, pid) -> None:
        try:
            tracks = self.source.playlist_detail(pid)
            self.tracks_loaded.emit(tracks)
            self.status_changed.emit(f"歌单 {pid}：载入 {len(tracks)} 首")
        except Exception as e:  # noqa
            self.status_changed.emit(f"失败：{e}")

    def _search(self) -> None:
        self._apply_cfg()
        kw = self.search_edit.text().strip()
        if not kw:
            return
        self.status.setText("搜索中…")
        import threading
        threading.Thread(target=self._do_search, args=(kw,), daemon=True).start()

    def _do_search(self, kw) -> None:
        try:
            tracks = self.source.search(kw)
            self.tracks_loaded.emit(tracks)
            self.status_changed.emit(f"搜索 {kw}：{len(tracks)} 首")
        except Exception as e:  # noqa
            self.status_changed.emit(f"失败：{e}")


# =========================================================================
# 曲库（我喜欢的音乐）/ 播放历史
# =========================================================================
class LibraryRow(QFrame):
    """曲库单行：封面缩略图 + 歌名 + 歌手。封面实时下载，带 token 防串台。"""

    def __init__(self, entry: dict, parent=None) -> None:
        super().__init__(parent)
        self.track_id = entry["id"]
        self._cover_token = entry["id"]
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)
        self.cover = QLabel()
        self.cover.setFixedSize(40, 40)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet(
            "font-size:20px;background:#eef1f6;border-radius:4px;")
        self.cover.setText("🎵")
        layout.addWidget(self.cover)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel(entry["title"])
        self.title.setObjectName("RowTitle")
        self.artist = QLabel(entry.get("artist") or "未知")
        self.artist.setObjectName("Sub")
        col.addWidget(self.title)
        col.addWidget(self.artist)
        layout.addLayout(col, 1)

    def set_cover(self, data: bytes, token=None) -> None:
        if token is not None and token != self._cover_token:
            return
        pix = QPixmap()
        if pix.loadFromData(QByteArray(data)):
            self.cover.setStyleSheet("border-radius:4px;background:transparent;")
            self.cover.setText("")
            self.cover.setPixmap(
                pix.scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class LibraryWidget(QWidget):
    """曲库 = 我喜欢的音乐。支持来源筛选（全部/本地音乐/网易云/QQ音乐）、
    播放模式（列表/随机/单曲循环）。操作一律走右键菜单：播放 / 下一首播放 /
    添加到推荐列表 / 从曲库删除。另含一个「不喜欢的歌曲」隐藏视图（与喜欢库对称）。"""
    track_activated = Signal(int)      # 双击播放
    play_next_clicked = Signal(int)    # 下一首播放
    add_to_reco_clicked = Signal(int)  # 添加到推荐列表（曲库与推荐列表解耦入口）
    delete_clicked = Signal(int)       # 从曲库删除
    play_all_clicked = Signal()        # 播放全部（替换播放队列）
    play_mode_changed = Signal(str)
    cover_loaded = Signal(int, bytes)   # 封面下载完成 (track_id, 图片字节)
    request_disliked = Signal()         # 请求切换到"不喜欢的歌曲"隐藏视图
    undo_dislike_clicked = Signal(str)  # 撤销不喜欢（传稳定 key）

    _MODES = ["order", "random", "loop_one"]

    @staticmethod
    def _src_label(s) -> str:
        return {"local": "本地", "netease": "网易云", "qqmusic": "QQ音乐",
                "spotify": "Spotify"}.get(s or "local", "本地")

    def __init__(self, parent=None, source_manager=None) -> None:
        super().__init__(parent)
        self._all: list = []
        self._ids: list[int] = []
        self._rows: dict = {}
        self.source_manager = source_manager
        self._disliked_mode = False
        self._disliked_keys: dict = {}   # list_row_index -> key
        self.cover_loaded.connect(self._on_cover_loaded)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        title = QLabel("曲库 · 我喜欢的音乐")
        title.setObjectName("HeaderLabel")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["列表播放", "随机播放", "单曲循环"])
        self.mode_combo.setCurrentIndex(0)
        self.src_combo = QComboBox()
        self.src_combo.addItems(["全部", "本地音乐", "网易云", "QQ音乐", "Spotify"])
        self.count_label = QLabel("共 0 首")
        self.count_label.setObjectName("Sub")
        bar.addWidget(title)
        bar.addStretch()
        bar.addWidget(QLabel("播放"))
        bar.addWidget(self.mode_combo)
        bar.addWidget(QLabel("来源"))
        bar.addWidget(self.src_combo)
        bar.addWidget(self.count_label)
        self.play_all_btn = QPushButton("▶ 播放全部")
        self.play_all_btn.setObjectName("AddBtn")
        self.play_all_btn.setToolTip("把曲库整批替换播放队列并从第一首开播")
        self.play_all_btn.clicked.connect(self.play_all_clicked.emit)
        bar.addWidget(self.play_all_btn)
        self.disliked_btn = QPushButton("👎 不喜欢的歌曲 (0)")
        self.disliked_btn.setObjectName("AddBtn")
        self.disliked_btn.setToolTip("查看/撤销「不喜欢的歌曲」隐藏库（对 AI 避雷生效）")
        self.disliked_btn.clicked.connect(self._toggle_disliked)
        bar.addWidget(self.disliked_btn)
        layout.addLayout(bar)

        hint = QLabel("右键操作 · 双击播放")
        hint.setObjectName("Sub")
        layout.addWidget(hint)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._on_activate)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context)
        layout.addWidget(self.list, 1)

        self.mode_combo.currentIndexChanged.connect(
            lambda i: self.play_mode_changed.emit(self._MODES[i]))
        self.src_combo.currentIndexChanged.connect(self._apply_filter)

    # ---------------- 对外 ----------------
    def load_tracks(self, tracks) -> None:
        self._all = list(tracks)
        self._apply_filter()

    def _apply_filter(self) -> None:
        idx = self.src_combo.currentIndex()
        src_map = {0: None, 1: "local", 2: "netease", 3: "qqmusic", 4: "spotify"}
        src = src_map.get(idx)
        self._tracks = [t for t in self._all
                        if src is None or (t.source or "local") == src]
        self.list.clear()
        self._ids = []
        self._rows = {}
        for t in self._tracks:
            # 曲库不再展示来源（用户要求）；来源仍内部保留用于上方「来源」筛选。
            item = QListWidgetItem()
            entry = {"id": t.id, "title": t.title,
                     "artist": t.artist or "未知"}
            row = LibraryRow(entry)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self._ids.append(t.id)
            self._rows[t.id] = row
            # 封面实时获取（不落库字节）：有 cover URL 则直接下载；
            # 无 cover 时尝试按音源取专辑封面，避免在线歌入库没写 cover 而永久 🎵。
            self._fetch_row_cover(t)
        self.count_label.setText(f"共 {len(self._tracks)} 首")

    # ---------------- 封面实时获取 ----------------
    def _on_cover_loaded(self, tid: int, data: bytes) -> None:
        row = self._rows.get(tid)
        if row is not None:
            row.set_cover(data, token=tid)

    def _fetch_row_cover(self, track) -> None:
        url = getattr(track, "cover", None)
        # 曲目未带封面 URL 时，尝试按音源取专辑封面（如网易云/QQ 音乐）。
        if not url and track.ext_id is not None:
            sm = self.source_manager
            src = sm.get(track.source) if sm is not None else None
            if src is not None and hasattr(src, "album_cover"):
                try:
                    url = src.album_cover(track.ext_id)
                except Exception:  # noqa
                    url = None
        if not url:
            return
        tid = track.id

        def worker():
            from utils.cover_cache import fetch_cover_bytes
            data = fetch_cover_bytes(url, getattr(track, "source", None))
            if data:
                self.cover_loaded.emit(tid, data)
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 内部 ----------------
    def _on_activate(self, item) -> None:
        idx = self.list.row(item)
        if self._disliked_mode:
            key = self._disliked_keys.get(idx)
            if key is None:
                return
            row = self._disliked_rows[idx]
            tid = row.get("track_id")
            if tid:  # 本地歌可直接播放；外部歌无 track_id，忽略双击
                self.track_activated.emit(tid)
            return
        if 0 <= idx < len(self._ids):
            self.track_activated.emit(self._ids[idx])

    def _on_context(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        if self._disliked_mode:
            idx = self.list.row(item)
            key = self._disliked_keys.get(idx)
            if key is None:
                return
            row = self._disliked_rows[idx]
            menu = QMenu(self)
            if row.get("track_id"):
                menu.addAction("▶ 播放",
                               lambda: self.track_activated.emit(row["track_id"]))
            menu.addAction("♻ 撤销不喜欢",
                           lambda: self.undo_dislike_clicked.emit(key))
            menu.exec(self.list.mapToGlobal(pos))
            return
        tid = self._ids[self.list.row(item)]
        menu = QMenu(self)
        menu.addAction("▶ 播放", lambda: self.track_activated.emit(tid))
        menu.addAction("⏭ 下一首播放", lambda: self.play_next_clicked.emit(tid))
        menu.addAction("➕ 添加到推荐列表", lambda: self.add_to_reco_clicked.emit(tid))
        menu.addSeparator()
        menu.addAction("🗑 从曲库删除", lambda: self.delete_clicked.emit(tid))
        menu.exec(self.list.mapToGlobal(pos))

    # ---------------- 不喜欢的歌曲（隐藏库视图） ----------------
    def is_disliked_mode(self) -> bool:
        return self._disliked_mode

    def set_disliked_count(self, n: int) -> None:
        self.disliked_btn.setText(f"👎 不喜欢的歌曲 ({n})")

    def _toggle_disliked(self) -> None:
        if self._disliked_mode:
            # 切回"我喜欢的音乐"
            self._disliked_mode = False
            self.disliked_btn.setText(
                self.disliked_btn.text().replace("👎 不喜欢的歌曲", "👎 不喜欢的歌曲"))
            self.src_combo.show()
            self.mode_combo.show()
            self.play_all_btn.show()
            self.load_tracks(self._all)
        else:
            self._disliked_mode = True
            self.src_combo.hide()
            self.mode_combo.hide()
            self.play_all_btn.hide()
            self.request_disliked.emit()

    def show_disliked(self, rows: list[dict]) -> None:
        """渲染"不喜欢的歌曲"隐藏视图（rows 来自 radio.get_disliked_tracks）。"""
        self._disliked_rows = list(rows)
        self._disliked_keys = {}
        self.list.clear()
        for i, r in enumerate(self._disliked_rows):
            item = QListWidgetItem()
            entry = {
                "id": r.get("track_id") or -1,
                "title": r.get("title") or "未知",
                "artist": r.get("artist") or "未知",
            }
            row = LibraryRow(entry)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self._disliked_keys[i] = r.get("key")
        self.count_label.setText(f"共 {len(self._disliked_rows)} 首（隐藏库·对 AI 避雷）")


class HistoryListWidget(QListWidget):
    """播放历史：带红心（加入曲库）/ 叉号（删除记录）/ 踩（不喜欢）+ 右键菜单。"""
    favorite_clicked = Signal(int, bool)   # track_id, 是否加入曲库
    remove_clicked = Signal(int)           # 删除该历史记录
    dislike_clicked = Signal(int, bool)    # track_id, 是否标记不喜欢
    row_activated = Signal(int)            # 双击播放
    play_next_clicked = Signal(int)        # 下一首播放

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("QueueList")
        self.setSpacing(4)
        self._rows: list = []
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)

    def mouseDoubleClickEvent(self, event) -> None:
        """双击某行播放（与右键菜单无关）。按坐标命中行，避免 setItemWidget 后
        itemDoubleClicked 不触发；落在 ♥/✕ 按钮上时不触发播放。"""
        index = self.indexAt(event.pos())
        if index.isValid():
            row = self.itemWidget(self.item(index.row()))
            if isinstance(row, QueueRow):
                child = row.childAt(row.mapFromParent(event.pos()))
                if not isinstance(child, QPushButton):
                    self.row_activated.emit(row.track_id)
        super().mouseDoubleClickEvent(event)

    def _on_context(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return
        row = self.itemWidget(item)
        if not isinstance(row, QueueRow):
            return
        tid = row.track_id
        menu = QMenu(self)
        menu.addAction("♥ 加入曲库", lambda: self.favorite_clicked.emit(tid, True))
        menu.addAction("⏭ 下一首播放", lambda: self.play_next_clicked.emit(tid))
        menu.addSeparator()
        menu.addAction("✕ 删除记录", lambda: self.remove_clicked.emit(tid))
        menu.exec(self.mapToGlobal(pos))

    def set_entries(self, tracks: list, fav_keys: set | None = None,
                     dislike_keys: set | None = None) -> None:
        self.clear()
        self._rows = []
        fav_keys = fav_keys or set()
        dislike_keys = dislike_keys or set()
        for t in tracks:
            entry = {
                "id": t.id, "title": t.title, "artist": t.artist or "未知",
                "current": False,
                "favorited": fav_key(t) in fav_keys,
                "disliked": fav_key(t) in dislike_keys,
            }
            item = QListWidgetItem()
            row = QueueRow(entry)
            row.heart.clicked.connect(
                lambda _=False, tid=t.id: self.favorite_clicked.emit(tid, True))
            row.xbtn.clicked.connect(
                lambda _=False, tid=t.id: self.remove_clicked.emit(tid))
            row.dislike.clicked.connect(
                lambda _=False, tid=t.id, d=entry["disliked"]:
                self.dislike_clicked.emit(tid, not d))
            self.addItem(item)
            self.setItemWidget(item, row)
            self._rows.append(row)
        sb = self.verticalScrollBar()
        sb.setValue(0)


class PlaylistCenterWidget(QWidget):
    """歌单中心：从网易云 / QQ音乐 浏览、收藏、播放歌单，并统一管理本地自建歌单。

    分组：
      - 我的歌单：登录后取该用户的歌单（依赖音源登录态）。
      - 发现推荐：平台推荐 / 分类歌单（免登录可用）。
      - 收藏歌单：用户主动「收藏」落库的平台歌单（仅收藏才持久化）。
      - 本地歌单：用户在中心内自建的歌单，与平台歌单统一纳管。
    按链接/ID 可直达任意平台歌单详情。

    网络 I/O 全部在后台线程执行，结果通过信号回主线程刷新 UI；
    封面走 utils.cover_cache.fetch_cover_bytes（音源感知头 + 缓存）。
    """
    play_tracks = Signal(list)        # 一批 Track
    play_track = Signal(object)       # 单首 Track
    status = Signal(str)
    cover_ready = Signal(int, bytes)
    grid_loaded = Signal(list, str)
    detail_loaded = Signal(object, list)
    save_done = Signal(int, str, str)  # (playlist_id, platform, ext_id)

    def __init__(self, source_manager, db, parent=None) -> None:
        super().__init__(parent)
        self.sm = source_manager
        self.db = db
        self.pm = PlaylistManager(source_manager, db)
        self._platform = "netease"
        self._group = "discover"
        self._detail = None
        self._detail_tracks = []
        self._cover_seq = 0
        self._cover_targets: dict[int, object] = {}
        self._build()
        self._wire()

    # ---------------- 构建 ----------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # 顶栏：分组切换 + 平台选择 + 链接加载 + 新建本地
        top = QHBoxLayout()
        self._group_buttons = {}
        self.group_local = QButtonGroup(self)
        self.group_local.setExclusive(True)
        for label, key in (("发现推荐", "discover"), ("我的歌单", "mine"),
                            ("收藏歌单", "saved"), ("本地歌单", "local")):
            btn = QPushButton(label)
            btn.setCheckable(True)
            self.group_local.addButton(btn)
            self._group_buttons[btn] = key
            top.addWidget(btn)
        self.platform_combo = QComboBox()
        self.platform_combo.addItem("网易云", "netease")
        self.platform_combo.addItem("QQ音乐", "qqmusic")
        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("粘贴歌单链接或 ID，回车加载")
        self.url_edit.returnPressed.connect(self._load_by_url)
        self.url_btn = QPushButton("加载")
        self.url_btn.setObjectName("AddBtn")
        self.url_btn.clicked.connect(self._load_by_url)
        self.new_local_btn = QPushButton("＋ 新建本地歌单")
        self.new_local_btn.setObjectName("AddBtn")
        self.new_local_btn.clicked.connect(self._new_local)
        top.addWidget(self.platform_combo)
        top.addStretch()
        top.addWidget(self.url_edit)
        top.addWidget(self.url_btn)
        top.addWidget(self.new_local_btn)
        layout.addLayout(top)

        # 栈：网格页 / 详情页
        self.stack = QStackedWidget()
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(120, 120))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setSpacing(14)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setWordWrap(True)
        self.grid.itemDoubleClicked.connect(self._on_card_activated)
        self.grid_page = QWidget()
        gl = QVBoxLayout(self.grid_page)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(self.grid)

        # 详情页
        self.detail_page = QWidget()
        dl = QVBoxLayout(self.detail_page)
        dl.setContentsMargins(0, 0, 0, 0)
        dhead = QVBoxLayout()
        drow = QHBoxLayout()
        self.back_btn = QPushButton("← 返回")
        self.play_all_btn = QPushButton("▶ 播放全部")
        self.play_all_btn.setObjectName("AddBtn")
        self.refresh_btn = QPushButton("↻ 刷新")
        self.save_btn = QPushButton("☆ 收藏")
        drow.addWidget(self.back_btn)
        drow.addStretch()
        drow.addWidget(self.play_all_btn)
        drow.addWidget(self.refresh_btn)
        drow.addWidget(self.save_btn)
        irow = QHBoxLayout()
        self.detail_cover = QLabel("🎵")
        self.detail_cover.setFixedSize(120, 120)
        self.detail_cover.setAlignment(Qt.AlignCenter)
        self.detail_cover.setStyleSheet("font-size:48px;")
        icol = QVBoxLayout()
        self.detail_title = QLabel("歌单")
        self.detail_title.setObjectName("HeaderLabel")
        self.detail_sub = QLabel("")
        self.detail_sub.setObjectName("Sub")
        self.detail_desc = QLabel("")
        self.detail_desc.setObjectName("Sub")
        self.detail_desc.setWordWrap(True)
        icol.addWidget(self.detail_title)
        icol.addWidget(self.detail_sub)
        icol.addWidget(self.detail_desc)
        icol.addStretch()
        irow.addWidget(self.detail_cover)
        irow.addLayout(icol, stretch=1)
        dhead.addLayout(drow)
        dhead.addLayout(irow)
        self.detail_list = QListWidget()
        self.detail_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.detail_list.itemDoubleClicked.connect(self._on_track_activated)
        self.detail_list.customContextMenuRequested.connect(self._track_context_menu)
        dl.addLayout(dhead)
        dl.addWidget(self.detail_list, stretch=1)

        self.stack.addWidget(self.grid_page)
        self.stack.addWidget(self.detail_page)
        layout.addWidget(self.stack, stretch=1)

    def _wire(self) -> None:
        self.group_local.buttonClicked.connect(self._on_group_clicked)
        self.grid_loaded.connect(self._populate_grid)
        self.detail_loaded.connect(self._populate_detail)
        self.cover_ready.connect(self._apply_cover)
        self.save_done.connect(self._on_save_done)
        self.back_btn.clicked.connect(self._back)
        self.play_all_btn.clicked.connect(self._play_all)
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.refresh_btn.clicked.connect(self._refresh_detail)
        # 默认视图
        self.btn_discover = self._btn_for("discover")
        self.btn_discover.setChecked(True)
        self.new_local_btn.setVisible(False)

    # ---------------- 分组 / 平台 ----------------
    def _btn_for(self, key: str) -> QPushButton:
        for b, k in self._group_buttons.items():
            if k == key:
                return b
        return None

    def _on_group_clicked(self, button: QPushButton) -> None:
        group = self._group_buttons.get(button)
        if not group:
            return
        self._group = group
        self.new_local_btn.setVisible(group == "local")
        self._load_grid()

    def _on_platform_changed(self, _idx: int) -> None:
        self._platform = self.platform_combo.currentData() or "netease"
        if self._group in ("mine", "discover", "saved"):
            self._load_grid()

    # ---------------- 加载网格 ----------------
    def _load_grid(self) -> None:
        self.stack.setCurrentWidget(self.grid_page)
        self.status.emit("加载中…")
        threading.Thread(target=self._do_load_grid, daemon=True).start()

    def _do_load_grid(self) -> None:
        group, platform = self._group, self._platform
        payloads: list[dict] = []
        hint = ""
        if group == "mine":
            pls = self.pm.my_playlists(platform)
            payloads = [self._meta_to_payload("live", platform, m) for m in pls]
            if not pls:
                hint = "未登录或暂无歌单：请在设置中登录后查看「我的歌单」"
        elif group == "discover":
            pls = self.pm.discover(platform, limit=30)
            payloads = [self._meta_to_payload("live", platform, m) for m in pls]
        elif group == "saved":
            rows = self.pm.saved_playlists(platform)
            payloads = [self._row_to_payload("saved", r) for r in rows]
        elif group == "local":
            rows = self.pm.local_playlists()
            payloads = [self._row_to_payload("local", r) for r in rows]
        self.grid_loaded.emit(payloads, hint)

    @staticmethod
    def _meta_to_payload(kind, platform, m) -> dict:
        return {"kind": kind, "platform": platform, "ext_id": m.ext_id,
                "name": m.name, "cover": m.cover, "creator": m.creator,
                "track_count": m.track_count, "description": m.description}

    @staticmethod
    def _row_to_payload(kind, r) -> dict:
        return {"kind": kind, "platform": r["platform"], "ext_id": r["ext_id"],
                "playlist_id": r["id"], "name": r["name"], "cover": r["cover"],
                "creator": r["creator"], "track_count": r["track_count"],
                "description": r["description"]}

    def _populate_grid(self, payloads: list[dict], hint: str) -> None:
        self.grid.clear()
        self._cover_targets.clear()
        if hint:
            self.status.emit(hint)
        elif not payloads:
            self.status.emit("暂无歌单")
        else:
            self.status.emit(f"共 {len(payloads)} 个歌单")
        for p in payloads:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, p)
            item.setText(f"{p.get('name') or '未命名'}\n{p.get('creator') or ''}")
            self.grid.addItem(item)
            cover = p.get("cover")
            if cover:
                self._fetch_cover(cover, p.get("platform"), item)

    # ---------------- 详情 ----------------
    def _on_card_activated(self, item: QListWidgetItem) -> None:
        p = item.data(Qt.UserRole)
        if not p:
            return
        self._detail = p
        self.detail_title.setText(p.get("name") or "歌单")
        self.detail_list.clear()
        self.stack.setCurrentWidget(self.detail_page)
        self.status.emit("加载歌单中…")
        threading.Thread(target=self._do_open_detail, args=(p,), daemon=True).start()

    def _do_open_detail(self, p: dict) -> None:
        kind = p.get("kind")
        tracks: list = []
        if kind == "live":
            tracks = self.pm.tracks(p["platform"], p["ext_id"])
        else:
            _row, tracks = self.pm.playlist_obj(p["playlist_id"])
        self._detail_tracks = tracks
        self.detail_loaded.emit(p, tracks)

    def _populate_detail(self, p: dict, tracks: list) -> None:
        self.stack.setCurrentWidget(self.detail_page)
        self.detail_title.setText(p.get("name") or "歌单")
        creator = p.get("creator") or "未知"
        tc = p.get("track_count")
        tc = tc if tc is not None else len(tracks)
        self.detail_sub.setText(f"{creator} · {tc} 首")
        desc = p.get("description") or ""
        self.detail_desc.setText(desc if desc else "（暂无简介）")
        self.detail_desc.setToolTip(desc)
        self.detail_cover.setText("🎵")
        self.detail_cover.setPixmap(QPixmap())
        cover = p.get("cover")
        if cover:
            self._fetch_cover(cover, p.get("platform"), self.detail_cover)
        self.detail_list.clear()
        for t in tracks:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, t)
            item.setText(f"{getattr(t, 'title', '?')} — {getattr(t, 'artist', '?')}")
            self.detail_list.addItem(item)
        self._refresh_save_button()
        self.status.emit(f"已加载 {len(tracks)} 首")

    def _refresh_save_button(self) -> None:
        p = self._detail
        if not p:
            return
        kind = p.get("kind")
        if kind == "live":
            saved = self.pm.is_saved(p["platform"], p["ext_id"])
            self.save_btn.setText("★ 已收藏" if saved else "☆ 收藏")
            self.save_btn.setVisible(True)
            self.refresh_btn.setVisible(True)
        elif kind == "saved":
            self.save_btn.setText("取消收藏")
            self.save_btn.setVisible(True)
            self.refresh_btn.setVisible(False)
        elif kind == "local":
            self.save_btn.setText("🗑 删除歌单")
            self.save_btn.setVisible(True)
            self.refresh_btn.setVisible(False)

    def _back(self) -> None:
        self.stack.setCurrentWidget(self.grid_page)

    def _play_all(self) -> None:
        tracks = getattr(self, "_detail_tracks", [])
        if tracks:
            self.play_tracks.emit(list(tracks))

    def _refresh_detail(self) -> None:
        if self._detail:
            threading.Thread(target=self._do_open_detail, args=(self._detail,),
                             daemon=True).start()

    # ---------------- 收藏 / 删除 ----------------
    def _on_save_clicked(self) -> None:
        p = self._detail
        if not p:
            return
        kind = p.get("kind")
        if kind == "live":
            self.status.emit("收藏中…")
            threading.Thread(target=self._do_save,
                             args=(p["platform"], p["ext_id"]), daemon=True).start()
        elif kind == "saved":
            self.pm.unsave(p["platform"], p["ext_id"])
            self.status.emit("已取消收藏")
            self._back()
            self._load_grid()
        elif kind == "local":
            self.pm.delete_local(p["playlist_id"])
            self.status.emit("已删除本地歌单")
            self._back()
            self._load_grid()

    def _do_save(self, platform, ext_id) -> None:
        pid = self.pm.save(platform, ext_id) or 0
        self.save_done.emit(pid, platform, ext_id)

    def _on_save_done(self, pid: int, platform: str, ext_id: str) -> None:
        if not pid:
            self.status.emit("收藏失败（无法获取歌单信息）")
            return
        old = self._detail or {}
        self._detail = {"kind": "saved", "platform": platform, "ext_id": ext_id,
                        "playlist_id": pid,
                        "name": old.get("name"), "cover": old.get("cover"),
                        "creator": old.get("creator"),
                        "track_count": old.get("track_count"),
                        "description": old.get("description")}
        self._refresh_save_button()
        self.status.emit("已收藏到歌单中心")

    # ---------------- 按链接/ID ----------------
    def _load_by_url(self) -> None:
        text = self.url_edit.text().strip()
        if not text:
            return
        res = self.pm.resolve(text)
        if not res:
            self.status.emit("无法解析该链接 / ID")
            return
        platform, ext_id = res
        p = {"kind": "live", "platform": platform, "ext_id": ext_id}
        self._detail = p
        self.detail_title.setText("加载中…")
        self.detail_list.clear()
        self.stack.setCurrentWidget(self.detail_page)
        self.status.emit("解析成功，加载中…")
        threading.Thread(target=self._do_open_detail, args=(p,), daemon=True).start()

    # ---------------- 本地歌单 ----------------
    def _new_local(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建本地歌单", "歌单名称：")
        if ok and name.strip():
            self.pm.create_local(name.strip())
            self.status.emit(f"已创建本地歌单：{name.strip()}")
            if self._group == "local":
                self._load_grid()

    def _new_local_and_add(self, track) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建本地歌单", "歌单名称：")
        if ok and name.strip():
            pid = self.pm.create_local(name.strip())
            self.pm.add_track_to_local(pid, track)
            self.status.emit(f"已创建并加入：{name.strip()}")

    def _add_track_to_local(self, playlist_id: int, track) -> None:
        self.status.emit("添加到本地歌单…")
        threading.Thread(target=self._do_add_to_local,
                         args=(playlist_id, track), daemon=True).start()

    def _do_add_to_local(self, playlist_id: int, track) -> None:
        try:
            self.pm.add_track_to_local(playlist_id, track)
            self.status.emit("已加入本地歌单")
        except Exception as e:  # noqa
            self.status.emit(f"添加失败：{e}")

    def _remove_track(self, track) -> None:
        if self._detail and self._detail.get("kind") == "local":
            self.pm.remove_track(self._detail["playlist_id"], getattr(track, "id", None))
            self.status.emit("已从歌单移除")
            self._refresh_detail()

    # ---------------- 曲目交互 ----------------
    def _on_track_activated(self, item: QListWidgetItem) -> None:
        track = item.data(Qt.UserRole)
        if track is not None:
            self.play_track.emit(track)

    def _track_context_menu(self, pos: QPoint) -> None:
        item = self.detail_list.itemAt(pos)
        if item is None:
            return
        track = item.data(Qt.UserRole)
        if track is None:
            return
        menu = QMenu(self)
        menu.addAction("▶ 播放这首").triggered.connect(
            lambda _=False: self.play_track.emit(track))
        add_menu = menu.addMenu("＋ 加入本地歌单")
        add_menu.addAction("新建本地歌单…").triggered.connect(
            lambda _=False: self._new_local_and_add(track))
        for r in self.pm.local_playlists():
            act = add_menu.addAction(r["name"])
            act.triggered.connect(
                lambda _=False, pid=r["id"], t=track: self._add_track_to_local(pid, t))
        if self._detail and self._detail.get("kind") == "local":
            menu.addSeparator()
            menu.addAction("从歌单移除").triggered.connect(
                lambda _=False: self._remove_track(track))
        menu.exec(self.detail_list.mapToGlobal(pos))

    # ---------------- 封面 ----------------
    def _fetch_cover(self, url: str, source: str, target: object) -> None:
        cid = self._cover_seq
        self._cover_seq += 1
        self._cover_targets[cid] = target
        threading.Thread(target=self._do_fetch_cover,
                         args=(cid, url, source), daemon=True).start()

    def _do_fetch_cover(self, cid: int, url: str, source: str) -> None:
        from utils.cover_cache import fetch_cover_bytes
        data = fetch_cover_bytes(url, source)
        if data:
            self.cover_ready.emit(cid, data)

    def _apply_cover(self, cid: int, data: bytes) -> None:
        target = self._cover_targets.pop(cid, None)
        if target is None:
            return
        pix = QPixmap()
        if pix.loadFromData(QByteArray(data)):
            pix = pix.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if isinstance(target, QListWidgetItem):
                target.setIcon(QIcon(pix))
            elif isinstance(target, QLabel):
                target.setPixmap(pix)
