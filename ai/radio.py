"""
AI 电台编排控制器：把播放引擎、TTS、AI大脑、漫游推荐串成"智能电台"。

新版模型（按产品需求重构）：
  - 推荐列表（播放列表）固定 5 首：当前正在播放 + 待播 4 首。
  - 每首歌在"进入列表"时就由 AI 预生成并存储好口播（文案+TTS）；
    播到它时正好播放这段口播（首歌作为开场导听；之后歌曲在转场间隙播放），
    歌曲播放完成即释放该首及其口播。
  - 播到下一首时，它前面的歌自动跳出列表、口播释放，AI 按喜好补一首（带口播）；
    点中间某首时，它前面的歌全部出列，并补足对应数量的新歌（带口播）。
  - 播过的歌进入播放历史。
  - 候选池 = 本地曲库（我喜欢的音乐）+ 网易云/QQ音乐（直链源）；由"风格滑块"
    （更熟悉↔更跳跃，4 档）提示 AI 在"曲库内 / 曲库外"之间权衡。
  - 口播开关（dj_on）：关闭则纯音乐、不生成/不播放口播。
  - 电台模式（roam_on）：关闭则 5 首放完即止，不自动续推。
  - 曲库播放模式（play_mode）：列表/随机/单曲循环，用于非 AI 续推时的取歌。

事件（on(event, cb)）：
  playlist_updated(entries)   # entries: [{id,title,artist,current,favorited}]
  history_changed(tracks)     # 播放历史（Track 列表，最新在前）
  library_changed(tracks)     # 曲库（我喜欢的音乐）Track 列表
  track_changed(track) / playing(bool) / dj_speech(text)
  recommend_reason(text) / progress(ms) / favorite(bool)
  thinking(bool) / lyrics(text) / lyric_line(idx,text)
  radio_error(dict)           # R57-3 连跳风暴刹车（kind=source_storm → 前端 toast）
"""
from __future__ import annotations

import logging
import random
import threading
import time

from utils.weather import get_weather
from utils.location import get_city, now_date, time_slot
from config.settings import PlayerConfig, RadioConfig, user_config
from core.lyrics import parse_lrc, line_index_at
from core.tts_segment import strip_tags
# R114：直链「够不够长」的判据（**单一来源**，纯函数、无网络；见其模块 docstring）
from data.sources.url_health import describe as _url_desc
from data.sources.url_health import is_degraded as _url_is_degraded
from data.sources.url_health import judge as _url_judge

#: R114：取链后判为**降级短链**时最多再取几次。
#: 依据（实测）：对同一 sid 连取 12 次**全部**返回完整对象 ⇒ 重取几乎必中；
#: 而一旦把降级链写进 6 小时缓存，这首歌就会"坏半天"（旧注释已警告过该危害）。
DEGRADED_RESOLVE_TRIES = 3

#: R114：**缓存直链的复核年龄**（秒）。超过此年龄的命中会做一次极廉价的
#: `Range: bytes=0-0` 探活（一个请求、只读响应头）。
#: 依据（实测 2026-09-13 夜）：`track_cache` 里 **25 条 netease 直链在 ~1 小时内全部变 403**
#: （签名过期），而 `CACHE_TTL["url"] = 6h` ⇒ 不复核就会拿死链去下载 ⇒ 403 ⇒ 加载失败 ⇒ **跳歌**
#: （连跳 3 首还会触发 `SKIP_STORM_LIMIT` 风暴刹车、把电台暂停）。
#: 取 15 min：**短时间反复播（最常见）依然零网络**（不破坏 T3 的初衷），只复核"放了一会儿的"条目。
URL_RECHECK_AGE_S = 900.0


def _probe_url_health(url: str, duration_s, timeout: float = 3.0) -> str:
    """极廉价的直链探活：只发 `Range: bytes=0-0`，从 `Content-Range` 读**服务端总长**。

    返回 `"ok"` / `"bad"`（**明确**坏：HTTP 403/404/410，或长度不足）/
    `"unknown"`（网络抖动、不支持 Range 等 ⇒ 一律 **fail-open**，绝不因为探活失败而拦播放）。
    """
    try:
        import requests
        r = requests.get(url, headers={"Range": "bytes=0-0"},
                         timeout=timeout, stream=True)
        code = int(r.status_code)
        cr = r.headers.get("Content-Range") or ""
        cl = r.headers.get("Content-Length")
        r.close()
        if code in (403, 404, 410):
            return "bad"                 # 签名过期 / 已下线：确定不可用
        if code >= 400:
            return "unknown"             # 其它 4xx/5xx 当歧义处理
        total = None
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except ValueError:
                total = None
        if total is None and code == 200 and cl:
            try:
                total = int(cl)
            except ValueError:
                total = None
        if not total:
            return "unknown"             # 拿不到总长 ⇒ 不判断
        ok, _why = _url_judge({"url": url, "size": total}, duration_s)
        return "ok" if ok else "bad"
    except Exception:  # noqa 探活只做优化，任何异常都 fail-open
        return "unknown"

try:
    # 模糊匹配：提高 AI 推荐歌名「落地命中」本地曲库的成功率（漏括号/版本后缀也能中）。
    from rapidfuzz import fuzz as _fuzz
    _HAVE_FUZZ = True
except Exception:  # noqa: rapidfuzz 未装时退回精确/包含匹配，不阻断推荐
    _fuzz = None
    _HAVE_FUZZ = False

from PySide6.QtCore import QObject, Signal, Slot, Qt
from PySide6.QtWidgets import QApplication

logger = logging.getLogger("MusicAI.Radio")

# 天气口播机械降频：相邻两次「提供天气」之间至少间隔多少首歌。
# 既保证不连续，也把整体频率压到约 1/WEATHER_MIN_GAP 以下（多数口播不含天气）。
WEATHER_MIN_GAP = 5


class _QtMainThread(QObject):
    """把后台线程的回调安全地投递到 Qt 主线程执行。

    后台线程（品味分析 / 口播生成 / 转场 / 选歌 / 进度回调等）通过 _emit
    触发的 UI 回调，若直接在后台线程执行会跨线程操作 QWidget，
    触发 'QObject::setParent: Cannot set parent, new parent is in a
    different thread' 警告甚至崩溃。此桥只在使用 QApplication（有 GUI 主
    事件循环）时启用；无头 / 纯逻辑测试环境没有 QApplication，则退化
    （见 _emit：此时本就不触碰 UI）。
    """
    _sig = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._sig.connect(self._handle, Qt.QueuedConnection)
        self._handler = None

    def bind(self, handler) -> None:
        self._handler = handler

    def post(self, event: str, args: tuple) -> None:
        self._sig.emit((event, args))

    @Slot(object)
    def _handle(self, payload) -> None:
        if self._handler:
            self._handler(payload[0], payload[1])


from ai.playback import PlaybackOrchestrator
from ai.schedule import ScheduleEngine
from ai.transition import TransitionHandler

class AIRadioController(PlaybackOrchestrator, ScheduleEngine, TransitionHandler):
    def __init__(self, player, tts, brain, library, db, recommend) -> None:
        self.player = player
        self.tts = tts
        self.brain = brain
        self.library = library
        self.db = db
        self.recommend = recommend

        self.current = None                 # 正在播放的 Track
        self.current_id = None
        self.current_broadcast = None       # {"text","wav"} 当前歌口播
        self._music_started = False         # 当前曲音乐是否已真正起播（防止迟到口播重启动音乐）
        self.upcoming: list = []            # 待播 entries（每项是 dict）——「播放列表（队列）」
        self.reco_buffer: list = []         # 推荐页货架 entries（AI 常驻"思考"的待荐池，不自动播）
        self._reco_loading = False          # 推荐页是否正在后台拉取（UI 据此显示"加载中"）
        # ★ 2026-11-17：检索优先选歌（ai/reco_pool.py）产出的「为什么选它」，
        #   key = track.id。_ai_reco_fill 建 entry 时直接取用，省掉每首一次 LLM 调用。
        self._reco_reasons: dict = {}
        self._manual_queue = False          # 手动队列进行中（如曲库播放全部）：期间不 AI 续填，保全用户队列
        # ★★ 2026-09-19：**队列来源** —— 决定播放模式是否可用（方案 docs/play_mode_gating_plan.md）。
        #   曲库/歌单 = 用户自己排的列表 ⇒ 顺序/随机/单曲循环**都允许**；
        #   推荐/漫游 = AI 排的列表，且**转场口播按 upcoming[0] 预生成** ⇒ 只允许 order，
        #   否则"下一首是《X》"的旁白会与实播失配（见 radio_bridge.set_play_mode 的注释）。
        #   取值：'library' | 'playlist' | 'reco' | 'roam' | 'onDemand' | None
        self.queue_src: str | None = None
        self.history: list = []             # 已播放 Track（最新在前）
        self.played: set[int] = set()       # 本会话播放过的 id（避免短期重复）
        self.disliked: set[int] = set()     # 用户"不喜欢"的 id（避雷）
        self._consecutive_skips = 0          # 连续音源失效计数（用于跳过续播时防死循环）

        self.weather = None
        self.city = None
        self.roam_on = True                 # 电台模式（自动续推）
        self.dj_on = True                   # 口播开关
        # 推荐偏好（熟悉度级别）0~3：从用户持久化配置载入，保证重启后仍保留。
        # 0=更熟悉(严格贴合品味/曲库) … 3=更陌生(跳出舒适区探索)。
        # 该值直接注入推荐提示词（见 ai/brain.recommend_selections）与口播上下文，
        # 决定 AI 选歌「熟悉↔探索」倾向，是推荐系统的关键变量。
        self.familiarity = max(0, min(3, int(user_config.data.get("rec_pref", 0) or 0)))
        # R67-3：播放模式持久化层 — 重启恢复上次的 order/random/loop_one（旧实现纯内存，重启总回 order）
        _pm = user_config.data.get("playback_mode", "order")
        self.play_mode = _pm if _pm in ("order", "random", "loop_one") else "order"
        self.taste_profile: dict | None = None
        self.last_recommend_reason: str = ""
        self._taste_from_history: bool = False  # 最近一次品味画像来源：True=播放历史
        # 上下文窗口（口播连贯性 ↔ 极致速度）：仅保留最近若干首已播歌曲的轻量记录，
        # 绝不累积无限历史。具体保留几首由 config ai.context_window 决定（0=不保留）。
        self._recent_played: list[str] = []
        self._RECENT_CAP = 30            # 内部硬上限（实际用于口播的条数受 context_window 约束）

        self._callbacks: dict = {}
        # 跨线程安全：后台线程经 _emit 触发的 UI 回调，统一投递到 Qt 主线程执行
        self._qt_main = None
        app = QApplication.instance()
        if app is not None:
            self._qt_main = _QtMainThread()
            self._qt_main.bind(self._dispatch)
        self._qlock = threading.RLock()
        self._advance_lock = threading.Lock()  # 防 _advance 重入（位置回调/Timer 并发）
        # 导航世代令牌：每次切歌/重播自增，作为「本次播放意图」的唯一标识。
        # 后台音频加载 / 转场定时器 / 播放结束回调在落子前比对令牌，若已被更新的导航
        # 取代则放弃本次操作——杜绝过期线程互相踩踏导致的「切歌卡顿 / 列表出现两首相同
        # 歌 / 旧歌自然结束时误切下一首」等问题（快速连点 next/prev/next 时尤为明显）。
        self._nav_seq = 0
        self._trans_seq = 0      # 当前已排程转场所属的导航世代
        # ★ 2026-11-19：口播语言（zh/en）。默认 zh；英文 UI 开关就绪后调
        #   set_broadcast_language("en") 一处切换 LLM prompt + TTS 语种 + 字数口径。
        self.broadcast_language = "zh"
        # ★ R119-4x：**口播 / 转场 串行链**（用户 2026-09-20 拍板：由并行改串行）。
        #   顺序 = 口播(歌1) → 转场(歌1→歌2) → 口播(歌2) → 转场(歌2→歌3) → …
        #   为什么串行：口播与转场要互相看得见（转场吃上一首口播文本、下一首口播吃
        #   「最近播放」上下文），顺序就必须确定；并行时三条线程抢同一把**非 FIFO** 的
        #   Ollama 锁，谁先谁后不可控（详见 ai/schedule.py::_chain_install 的注释）。
        #   `_chain_steps=None` 表示当前无链；`_chain_gen` 是世代号，新链安装即 +1，
        #   旧 worker 发现世代变了就中止（队列会滚动，旧链算出的"下一首"可能已过期）。
        self._chain_steps: list | None = None
        self._chain_gen: int = 0
        self._chain_nav_seq: int = 0
        self._chain_running: bool = False
        self._chain_lock = threading.RLock()
        self._play_seq = 0       # 当前实际在播歌曲所属的导航世代
        self._transitioning = False
        self._trans_timer: threading.Timer | None = None
        self._trans_fired = False        # 转场定时器是否已触发（用于背景重排时避免重复触发）
        # ★★★ R119-4v：**「这段边界到底真的出过声没有」** —— 与 `_trans_fired`（定时器
        #   是否触发）是两件不同的事：**触发 ≠ 出声**（`type=none` / 脚本空 / 合成失败）。
        #   它是 `_on_track_ended` 决定"下一首还要不要播导听口播"的**唯一**依据。
        #   （旧代码拿 `self.dj_on` 当替身判据 ⇒ 转场没出声时，下一首那份**已经生成好的
        #   正确导听口播会被一起抑制掉** ⇒ 用户听到"连着两段没有 DJ"。
        #   用户原话："不是讨厌兜底口播，而是讨厌没有正确的口播"。）
        self._trans_spoke = False
        # 口播可观测计数（经 /api/status 的 `dj` 段暴露）：让"正确口播 vs 退化兜底"一眼可数。
        self._n_intro_ai = 0             # 正确的 AI 导听口播真的播了
        self._n_intro_fallback = 0       # 退化兜底句真的播了（R119-4s 的中性句）
        self._n_trans_spoke = 0          # 转场真的出声（该边界有人在说话）
        self._n_trans_skip = 0           # 该边界没人说话 ⇒ 下一首照常补导听
        self._trans_deadline: float | None = None   # 转场定时器预定的绝对触发时间（monotonic）
        self._cur_dur_ms = 0.0           # 当前歌曲时长(ms)，供转场按脚本长度排程
        self._intro_pending = False      # 开场导听口播尚未就绪时的待播标记
        self._intro_timer = None         # 开场导听看门狗定时器
        self._intro_spoken = False       # 防止同一首歌重复播口播

        self.source_manager = None
        self._ext_seq = 0                  # 外部源检索结果的会话内唯一 id 计数
        # 推荐/播放队列里「用户显式红心过 / 已在曲库收藏」的曲目 id。
        # 启动时从 DB 载入（见 _load_fav_state），使上一会话或曲库里收藏的歌
        # 在播放列表/推荐/历史中正确显示红心——否则跨会话收藏的歌会显示成空心。
        # 与曲库 favorite_all_local 自动全红心解耦：曲库照常显示全部本地歌红心，
        # 但推荐队列只在用户真正点过红心或确实在 favorites 表时才亮。
        # 推荐/播放队列/历史里「用户显式红心过 / 已在曲库收藏」的曲目键集合。
        # 用「稳定键」而非裸 id：在线歌（网易云/QQ/Spotify）用 音源+原生 id 作键，
        # 本地/无 ext_id 的歌用 db id 作键。这样跨会话、跨「推荐每次重建的临时 id」都
        # 能稳定识别同一首歌，避免「收藏了但 UI 不显示」的卡红心问题。
        # 启动时从 DB 载入（见 _load_fav_state），使上一会话收藏的歌在列表里正确显示红心。
        # 与曲库 favorite_all_local 自动全红心解耦：本地自动收藏不进此集合（曲库 tab 单独显示）。
        self._queue_fav: set[str] = set()
        self._load_fav_state()
        # 「不喜欢的歌」稳定键集合（跨会话持久化，对 AI 避雷生效）。
        # 与 _queue_fav 对称：在线歌用 音源+原生 id，本地/无 ext_id 用 db id。
        self.disliked_keys: set[str] = set()
        try:
            self.disliked_keys = set(self.db.dislike_keys())
        except Exception:  # noqa
            self.disliked_keys = set()
        self.lyrics_text: str | None = None
        self.lyric_lines: list = []
        self._lyric_idx = -1

        self.player.set_position_callback(self._on_progress)
        self.player.set_ended_callback(self._on_track_ended)
        # 天气在后台线程拉取；品味画像改为"需要推荐时才分析"（见 _ensure_taste），
        # 不在启动/定时自行调用，避免无谓的 AI 请求与潜在卡顿。
        self.refresh_context()
        self._taste_dirty = True      # 首次推荐/口播前需要现分析
        self._taste_computing = False
        self._last_fail = None        # LLM 失败提示去重用

        # 播放列表发射节流：后台线程在推荐/口播密集时可能频繁触发 playlist_updated，
        # 直接在主线程序建列表控件会让 UI 卡顿。这里合并 120ms 内的多次发射为一次。
        self._last_playlist_emit = 0.0
        self._playlist_dirty = False
        self._playlist_timer: threading.Timer | None = None

        # 推荐页发射节流：与播放列表同理，reco_updated 也可能在补货/点播插入时
        # 短时间密集触发，合并 120ms 内的多次发射为一次，避免主线程序建控件卡顿。
        self._last_reco_emit = 0.0
        self._reco_dirty = False
        self._reco_timer: threading.Timer | None = None
        self._reco_pending_need = 0    # 加载期间又产生的补货需求，加载完继续补
        self._reco_retry_timer: threading.Timer | None = None  # AI 偶发空/未就绪时的重试定时器

        # 音频播放串行化：pygame 混音器非线程安全，多个 start_radio/切歌并发调用
        # player.load/play 会卡死 UI。用独立锁把「加载+起播」互斥起来，杜绝并发播放。
        self._audio_lock = threading.Lock()
        # start_radio 重入保护：双击/连点会在极短时间内多次调用全量重启电台，
        # 并发重置 current、清空待播、叠加多个 _start_audio 线程导致卡死。这里把
        # 并发调用合并为「采纳最新开场曲」一次执行，避免重入拖垮播放器。
        self._start_lock = threading.Lock()
        self._starting = False
        self._pending_seed: tuple | None = None

        # 天气口播机械降频：统计本会话已生成的口播序号，以及上一次「提供天气」的序号。
        # 两者间隔 < WEATHER_MIN_GAP 时，本次口播的 ctx 中不含天气字段，
        # 从源头杜绝"连续/高频重复天气口播"。
        self._broadcast_seq = 0
        self._last_weather_seq = 0
        # 序号自增不是原子操作：_prep_next_broadcast 会并发起多个 _do_gen_broadcast
        # 线程，若不加锁会出现"同一 seq 号被两段不同歌曲占用"的竞态（日志里看到
        # 同一序号对应两首不同歌、即"口播生成了两遍"）。用独立锁保护自增与天气降频。
        self._seq_lock = threading.Lock()
        # 异步填充开场标记：LLM 开启时 start_radio 触发后台 _fill_from_reco（从推荐页
        # 引流）填充待播，但此刻待播可能仍为空，不能立即选曲；待 _fill_from_reco 末尾
        # 置位后由
        # _maybe_start_deferred 真正选曲开播，避免电台静默。
        self._need_start = False

    # ---------- 事件 ----------

    def on(self, event: str, cb) -> None:
        self._callbacks[event] = cb

    def _dispatch(self, event: str, args: tuple) -> None:
        """在主线程实际执行事件回调（由 _emit 统一调度）。"""
        cb = self._callbacks.get(event)
        if cb:
            try:
                cb(*args)
            except Exception as e:  # noqa
                logger.warning("回调 %s 异常: %s", event, e)

    def _emit(self, event: str, *args):
        """触发事件回调。

        若当前在后台线程且存在 Qt 主事件循环，则把回调投递到主线程执行，
        避免后台线程直接操作 QWidget（跨线程 setParent 警告 / 崩溃）。
        主线程内、或纯逻辑（无 QApplication）环境下则直接同步调用。
        """
        if self._qt_main is not None and \
           threading.current_thread() is not threading.main_thread():
            self._qt_main.post(event, args)
        else:
            self._dispatch(event, args)

    def _bump_nav(self) -> int:
        """推进导航世代并返回新值（受 _qlock 保护）。每次切歌/重播都应经此自增，
        使后台过期任务可据此自我放弃，避免过期音频加载 / 转场 / 结束回调互相踩踏。"""
        with self._qlock:
            self._nav_seq += 1
            return self._nav_seq

    def _note_llm_failure(self, kind: str, what: str) -> None:
        """去重地提示一次 AI 调用失败（避免每一首歌都刷屏）。

        kind   : 错误分类（如 recommend / taste / broadcast / content_filter）
        what   : 这次失败发生在哪一步
        仅在"已配置 AI 但调用失败"时调用；离线模式不提示。
        """
        key = (kind, what)
        if getattr(self, "_last_fail", None) == key:
            return
        self._last_fail = key
        # ★ 2026-11-17（用户要求）：**只记日志，不进口播栏**。
        #   口播栏只该显示真正的口播内容；把「AI 调用失败」推到那里会让人以为整套坏了，
        #   而且失败时原本就有兜底口播在播，提示纯属噪音。
        logger.warning("AI 调用失败（%s / %s），已回退离线方案，不影响播放", kind, what)

    # ---------- 上下文 ----------

    def refresh_context(self) -> None:
        threading.Thread(target=self._do_refresh_context, daemon=True).start()

    def warmup_llm(self) -> None:
        """后台预加载 Ollama 模型到显存，避免首次推荐因模型冷加载而卡 ~12s。

        qwen3.5:4b 首次调用要把模型载入 GPU 显存（实测 ~12s），之后才稳定 ~1.5s。
        在软件启动、用户尚未点歌时提前静默加载一次，让后续推荐/口播的首次等待
        大幅缩短。仅对 ollama 后端生效；API 后端（云端）无需预热。失败静默忽略。
        """
        try:
            llm = getattr(self.brain, "llm", None)
            if not llm or not llm.enabled:
                return
            # ★★★ 2026-11-18（用户「推荐要在 LLM 刚唤醒就加载、优先级最高」）：
            #   LLM 进程此时已 /health 就绪（模型已载入），**立即并行填推荐** ——
            #   不等下面这次预热 chat 返回。实测预热 chat（21G 惰性载入首 token）
            #   要卡近 2 分钟；若把 ensure_reco 放它后面，推荐就被白白拖 2 分钟。
            #   推荐选歌自己也会调 LLM，与预热 chat 共享同一个已加载模型，不冲突。
            # ★★ 2026-11-18（用户「分析完品味不能让 AI 闲着」）：LLM 一就绪就
            #   后台启动品味分析（AI 从启动就开始干活）；实测它要 ~96s，必须尽早起跑。
            #   第一批推荐走网易云快路径、**不调 LLM**，故不与品味分析抢 -np1 槽位。
            # ★★ 2026-11-18（用户修正）：品味分析**不在启动时跑**，而是搭首曲
            #   固定开场白的富余时间（首曲不调 LLM，有 3-4 分钟空档）——见
            #   playback.py 首曲锁定开场白处触发 refresh_taste()。分析完再进流水线
            #   （转场→导听→转场），一点不耽误。启动阶段只管秒填第一批推荐。
            logger.info("[启动] LLM 已就绪 → 立即填推荐货架（最高优先级，不等预热 chat）")
            try:
                self.ensure_reco()
            except Exception as e:  # noqa
                logger.warning("填推荐失败：%s", e)
            # ★★ 2026-11-18：llamacpp 后端**不做额外 chat 预热**。
            #   35B 在 llamacpp_manager 起进程时已加载模型（/health 就绪即热），
            #   这个预热 chat 是 ollama 冷加载时代的产物。实测它卡近 2 分钟，
            #   而 llama-server 是 `-np 1` 单槽位 ⇒ 占住唯一槽位堵死推荐选歌
            #   （推荐 0 首的根因）。只对 ollama 保留预热。
            if llm.backend == "ollama":
                logger.info("预加热 Ollama LLM（%s，后台）", getattr(llm, "model", "?"))
                llm.chat("你是音乐电台的 AI 助手。", "请只回复两个字：好的",
                         expect_json=False)
            else:
                logger.info("[启动] %s 后端模型已随进程加载（/health 就绪），跳过 chat 预热",
                            llm.backend)
        except Exception as e:  # noqa
            logger.warning("LLM 预热失败（不影响功能，首次推荐会自行加载）: %s", e)

    def _do_refresh_context(self) -> None:
        try:
            # 双冗余定位：优先电脑位置信息，回退 IP；以电脑位置为准
            self.city = get_city()
            self.weather = get_weather(self.city)
            if self.weather.get("city"):
                self.city = self.weather["city"]
        except Exception as e:  # noqa
            logger.warning("上下文刷新失败: %s", e)
            self.weather = {"text": "晴", "temp": 22, "city": self.city or "本地"}

    def refresh_taste(self) -> None:
        # ★★ 2026-11-18：同步置 _taste_computing 标志，再起线程。
        #   旧实现直接起线程跑 _do_refresh_taste（绕过 _ensure_taste），从不设标志 ⇒
        #   串行链让路守卫读 _taste_computing 恒 False ⇒ 0.0s 放行、转场抢先抢 LLM。
        #   现在标志在分析期间恒 True，守卫能正确等到品味分析（顺便出推荐种子）完成。
        if getattr(self, "_taste_computing", False):
            return   # 已在分析，不重复起
        self._taste_computing = True
        def _run():
            try:
                self._do_refresh_taste()
            finally:
                self._taste_computing = False
        threading.Thread(target=_run, daemon=True).start()

    def _taste_source_tracks(self) -> list:
        """品味分析的「数据源」：**优先用曲库（用户实际拥有的音乐集合）**。

        曲库（我喜欢的音乐）才是用户真实品味的直接反映。电台运行期间的「播放历史」
        大多是 AI 自己推荐的歌，用它们当品味样本会把画像带偏（等于让 AI 分析自己的
        推荐），因此不再作为主数据源。仅在曲库为空（尚未导入任何音乐）的极端情况下，
        才回退到播放历史，保证画像不空。

        返回 track-like 对象（sqlite3.Row 或 Track 均可，analyze_taste 只读
        title/artist/genre/year）；曲库较大时随机抽取至多 60 首作为样本，避免整库
        稀释成「收藏夹清单」且控制 token 成本。
        """
        def _row_get(r, key, default=""):
            # sqlite3.Row 仅支持 r[key]、**不**支持 r.title 属性访问；
            # 普通对象/字典只支持 getattr。两者兼容。
            try:
                v = r[key]
                return v if v is not None else default
            except Exception:  # noqa
                return getattr(r, key, default)

        def _row_to_track(r) -> object:
            # analyze_taste / _offline_taste 都用 t.title 属性读取，统一归一成
            # 带属性的轻量对象，避免线上 sqlite3.Row 直接属性访问报错回退。
            return type("HT", (), {
                "title": _row_get(r, "title"),
                "artist": _row_get(r, "artist"),
                "genre": _row_get(r, "genre", ""),
                "year": _row_get(r, "year", ""),
            })()

        # 主数据源：曲库（我喜欢的音乐）。用户要求品味分析「最多只上传最新 100 首
        # 歌、不上传过多」——既控 token 成本，也避免整库稀释成「收藏夹清单」。
        # 按 added_at 倒序取最新 100 首（最新添加的在最前），库不足 100 首则全取。
        try:
            rows = self.db.conn.execute(
                "SELECT * FROM tracks ORDER BY added_at DESC LIMIT 100").fetchall()
        except Exception:  # noqa
            rows = []
        if rows:
            self._taste_from_history = False
            return [_row_to_track(r) for r in rows]

        # 极端回退：曲库为空 → 播放历史（避免画像完全为空）
        self._taste_from_history = True
        rows: list = []
        try:
            rows = list(self.db.recent_history(limit=100) or [])
        except Exception:  # noqa
            rows = []
        seen = set()
        hist = []
        for r in rows:
            title = _row_get(r, "title")
            artist = _row_get(r, "artist")
            key = (title, artist)
            if key in seen:
                continue
            seen.add(key)
            hist.append(_row_to_track(r))
        return hist

    def _do_refresh_taste(self) -> None:
        # ★★ 2026-11-18（真机品味分析重复跑2次、每次140s饿死导听）：
        #   _ensure_taste 虽有判断，但 _kick_taste_background→refresh_taste→本方法
        #   直接调用绕过了判断，UI 的 mark_taste_dirty 又让它反复跑。
        #   这里做**最终防线**：已有画像且非脏就绝不重复调 LLM；并发用锁串行。
        if not getattr(self, "_taste_run_lock", None):
            self._taste_run_lock = threading.Lock()
        if not self._taste_run_lock.acquire(blocking=False):
            logger.info("[品味] 已有分析在跑，跳过重复调用")
            return
        try:
            if self.taste_profile is not None and not getattr(self, "_taste_dirty", False):
                logger.info("[品味] 画像已就绪且未变脏，跳过重复分析")
                return
            tracks = self._taste_source_tracks()
            self.taste_profile = self.brain.analyze_taste(tracks)
            logger.info("品味画像分析完成（来源=%s，%d 首）",
                        "播放历史" if getattr(self, "_taste_from_history", False)
                        else "曲库", len(tracks))
            # ★★ 2026-11-18（用户「品味分析顺便推荐5首，去重后填货架」）：
            #   品味分析已产出推荐种子，拿去网易云检索验证（真实可播才入库），
            #   省掉首曲期间那次独立的补推荐 LLM 调用（那次要 100s+）。
            self._fill_reco_from_taste_picks()
        except Exception as e:  # noqa
            logger.warning("品味画像分析失败，回退离线画像: %s", e)
            # 关键：品味分析失败（LLM 抖动/超时）时，绝不能把画像置空 —— 空画像会让
            # 推荐退化为「无依据通用热歌」(如 Shape of You)，必须回退到基于数据源的离线画像。
            try:
                fallback = self._taste_source_tracks()
                self.taste_profile = self.brain._offline_taste(fallback)
            except Exception:  # noqa
                self.taste_profile = None
            if getattr(self.brain, "llm", None) and self.brain.llm.enabled:
                self._note_llm_failure("taste", f"品味分析：{e}")
        finally:
            try:
                self._taste_run_lock.release()
            except Exception:  # noqa
                pass

    def _fill_reco_from_taste_picks(self) -> None:
        """★ 2026-11-18：品味分析顺便吐的推荐种子 → 网易云检索验证 → 填推荐货架。

        用户设计：首曲期间只跑一次 LLM（品味分析），它顺便给 5 首新歌建议；
        这里逐首拿去网易云搜，搜到的真实可播曲目去重后入库，省掉独立的补推荐
        LLM 调用（实测那次挑1首要 100s+，是首曲导听抢占/联网召回叠加所致）。
        搜不到的（LLM 编造的）直接丢弃，绝不入库——守住「推荐必须真实」铁则。
        """
        picks = getattr(self.brain, "_taste_seed_picks", None) or []
        if not picks:
            return
        # 种子**不在开头清空**：品味分析完成时货架可能是满的（首批直出已填5首），
        # 这时加不进去、必须留着种子，等后续货架低于阈值时再消费。只在末尾 added>0 时清。
        sm = getattr(self, "source_manager", None)
        src = sm.get("netease") if sm else None
        if src is None or not getattr(src, "enabled", False):
            return
        added = 0
        with self._qlock:
            existing = {e["track"].id for e in self.reco_buffer
                        if self._is_track(e.get("track"))}
            exist_keys = {self.fav_key(e["track"]) for e in self.reco_buffer
                          if self._is_track(e.get("track"))}
        exclude = set(getattr(self, "played", set())) | {getattr(self, "current_id", None)}
        for p in picks:
            title = (p.get("歌曲名") or "").strip()
            artist = (p.get("艺术家") or "").strip()
            if not title:
                continue
            try:
                res = src.search(f"{title} {artist}".strip()) or []
            except Exception as e:  # noqa
                logger.info("[品味推荐] 检索失败 %s: %s", title, type(e).__name__)
                continue
            # 取第一首真实结果；优先标题匹配的（防搜到无关歌）
            hit = None
            for t in res:
                tt = (getattr(t, "title", "") or "").lower()
                if title.lower() in tt or tt in title.lower():
                    hit = t
                    break
            if hit is None and res:
                hit = res[0]
            if hit is None:
                logger.info("[品味推荐] 未找到真实歌曲，丢弃：%s - %s", title, artist)
                continue
            with self._qlock:
                if (hit.id in existing or self.fav_key(hit) in exist_keys
                        or hit.id in exclude):
                    continue
                # ★ 用户修正：首批「每日推荐直出」保留（保漫游速度），品味推荐只做
                #   **补充**——货架未满就追加，已满则不硬塞（去重由 existing/exist_keys 保证）。
                #   不插队首、不截断首批：首批直出和品味推荐共存，播放消耗后自然流动。
                if len(self.reco_buffer) >= RadioConfig.PLAYLIST_SIZE:
                    continue
                self.reco_buffer.append(self._make_entry(hit, "品味分析推荐"))
                existing.add(hit.id)
                exist_keys.add(self.fav_key(hit))
                added += 1
        logger.info("[品味推荐] 品味分析种子 %d 首 → 验证入库 %d 首（货架 %d，补充首批直出）",
                    len(picks), added, len(self.reco_buffer))
        if added:
            # 真正消费了（至少入库1首）→ 清空种子，避免后续重复验证
            try:
                self.brain._taste_seed_picks = []
            except Exception:  # noqa
                pass
            self._emit_reco()

    def _ensure_taste(self) -> None:
        """需要推荐/口播时才现分析品味画像（只在后台线程调用，不卡主线程）。

        带脏标记与并发保护：仅在"从未分析过"或"曲库变更后标记为脏"时
        才真正发起一次 LLM 调用；多个后台线程同时进入时只算一次。

        省 token：电台模式（roam_on）关闭时不再上传品味分析到 LLM（关闭电台即
        不需要 AI 推荐依据）。原 `force=True` 破例通道是给「随机一首」用的，
        R25 该功能下线后已无调用方，参数一并删除。
        品味数据源优先「曲库（用户实际拥有的音乐）」，
        曲库为空时才回退播放历史（见 _taste_source_tracks）。
        """
        if not self.roam_on:
            return
        if self.taste_profile is not None and not self._taste_dirty:
            return
        if getattr(self, "_taste_computing", False):
            return
        self._taste_computing = True
        try:
            self._do_refresh_taste()
        finally:
            self._taste_computing = False
            self._taste_dirty = False

    def mark_taste_dirty(self) -> None:
        """标记品味画像需要重新分析（曲库变更后调用，下次推荐时才真正分析）。"""
        self._taste_dirty = True

    def _reco_ctx(self) -> dict:
        _, slot_cn = time_slot()
        _iso, _date_cn, _wd, _season = now_date()      # ★ 日期（见 location.now_date）
        mood = self.db.get_pref("mood", "")
        w = self.weather or {}
        return {
            "taste_profile": self.taste_profile or {},
            "location": self.city or "本地",
            "weather": w.get("text", "天气不错"),
            "weather_obj": w,
            "time_slot": slot_cn,
            "date": _date_cn, "date_iso": _iso, "weekday": _wd, "season": _season,
            "mood": mood,
            # 推荐偏好（关键变量）：直接决定 AI 选歌的「熟悉↔探索」倾向，
            # 由 recommend_selections 注入提示词约束，并被广播上下文引用。
            "familiarity": self.familiarity,
            "familiarity_label": RadioConfig.FAMILIARITY_LABELS[
                max(0, min(3, self.familiarity))],
        }

    def _build_ctx(self, track, position=None, total=None, next_title=None,
                   suppress_weather: bool = False) -> dict:
        _, slot_cn = time_slot()
        _iso, _date_cn, _wd, _season = now_date()      # ★ 日期（见 location.now_date）
        mood = self.db.get_pref("mood", "")
        w = self.weather or {}
        # 天气数据始终保留在上下文里：默认（离线）口播照常带天气。
        # 仅当 suppress_weather=True（大模型口播的降频回合）时，才在"给大模型的信息"中
        # 隐去天气（见 _build_broadcast_user），但离线回退仍使用真实天气。
        weather_sentence = f"{w.get('text', '晴')}，气温 {w.get('temp', 22)}℃"
        weather_obj = w
        return {
            "weather": weather_sentence,
            "weather_obj": weather_obj,
            "suppress_weather": suppress_weather,
            "location": self.city or "本地",
            "time_slot": slot_cn,
            # ★ 2026-09-21：日期（用户要求）。给模型一个"今天"的锚点，
            #   它才能把检索资料里的**发行年份**变成"距现在多久"这类有画面的说法。
            "date": _date_cn, "date_iso": _iso, "weekday": _wd, "season": _season,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "genre": track.genre,
            "bpm": getattr(track, 'bpm', None),
            "api_metadata": getattr(track, 'api_metadata', {}) or {},
            "mood": mood,
            "taste_profile": self.taste_profile or {},
            "recommendation_reason": self.last_recommend_reason,
            "playlist_position": position,
            "playlist_total": total,
            "next_title": next_title,
            # 推荐偏好（关键变量）：注入口播上下文，让 DJ 解说呼应「更熟悉/更陌生」
            # 这一用户偏好，使推荐输出（解说文案）真实反映用户配置。
            "familiarity": self.familiarity,
            "familiarity_label": RadioConfig.FAMILIARITY_LABELS[
                max(0, min(3, self.familiarity))],
            # 上下文窗口（轻量连贯性）：最近若干首已播歌曲，受 config context_window 约束；
            # 0 时为空（纯实时、不累积历史）。绝不传整段对话历史。
            "recent_context": self._recent_context_text(),
        }

    # ---------- 候选池 ----------

    def start_radio(self, seed_track=None, auto_start: bool = True) -> None:
        """启动电台。seed_track 指定开场曲；否则用推荐填充。

        LLM 开启时，推荐列表由后台 _fill_from_reco（从推荐页引流）异步填充：主线程此刻
        待播可能还是空的，不能立刻 _pop_to_current（否则 current 为空、电台静默）。此时
        标记 _need_start，待 _fill_from_reco 末尾的 _maybe_start_deferred 把第一首选为
        当前曲并开播。
        LLM 关闭时 _fill 同步填充，走原有「立即选曲开播」路径。

        auto_start=False 用于「打开软件但不自动开播」：仅提交 AI 品味分析并按品味推荐
        歌曲（歌曲先入推荐列表），不置位 _need_start，待用户点播才真正开播；
        推荐列表在 AI 返回前以「生成中」呈现。auto_start=True（默认，以及点播
        路径）才会在 AI 填充完成后自动开播。无论哪种路径，第一首歌都用「默认开场白」
        （地点+天气+温度+歌名，见 _do_gen_broadcast 的 opening 分支）。

        重入保护：双击/连点会在极短时间内多次全量重启电台，并发重置状态、叠加多个
        _start_audio 线程，导致 pygame 播放器卡死 UI。这里把并发调用合并为「采纳最新
        开场曲」一次执行——正在开播流程中到达的请求仅记录最新 seed，待本次收尾时再
        重启一次，绝不并发重入。
        """
        # 原子地检查+置位「正在开播」，避免两次 start_radio 同时进入主体。
        with self._start_lock:
            if self._starting:
                # 记录期间收到的最新开场请求，末尾 finally 会采纳它（避免丢失最后一次点击）。
                self._pending_seed = (seed_track, auto_start)
                return
            self._starting = True
        try:
            self._clean_upcoming()  # 防御：下方读取 e["track"].id
            self._need_start = False
            with self._qlock:
                if seed_track is not None:
                    # 以指定歌曲开场：从待播中剔除它，避免重复计数
                    self.upcoming = [e for e in self.upcoming
                                     if e["track"].id != seed_track.id]
                    self.current = seed_track
                    self.current_id = seed_track.id
                    self.current_broadcast = None
                    self._music_started = False
                    self.played.add(seed_track.id)
                    if not any(t.id == seed_track.id for t in self.history):
                        self.history.insert(0, seed_track)
                elif self.current is None:
                    if auto_start:
                        # 自动开播：填「播放队列」并开播（原有逻辑）。
                        if not self.upcoming:
                            self._need_start = True
                            # 冷启动清空播放列表缓存：丢弃上一轮/残留的待播（尤其是不该
                            # 出现的本地兜底曲），保证「播放列表只从推荐页引流」的纯净性。
                            self.upcoming = []
                            # 冷启动「初始加载归推荐页管」：先让推荐页(reco_buffer)
                            # 开始补满，以便播放列表可从推荐页尽快引流、尽早出口播。
                            self._refill_reco()
                            self._refill_initial()
                        if self.upcoming and not self._need_start:
                            # 同步填充（无 LLM / 已就绪）：立即选曲开播
                            self._need_start = False
                            self._pop_to_current()
                    else:
                        # 打开软件不自动开播：填「推荐货架」reco_buffer（AI 常驻思考），
                        # 播放队列保持为空、不置位 _need_start、不自动播。用户点推荐页
                        # 歌曲时才把货架整批搬入播放队列（见 play_from_reco）。
                        self._refill_reco()
            self._emit_playlist()
            # 为"当前曲"（尤其是用户点播的 seed）生成口播——否则开场导听会消失。
            # 该临时 entry 不入列表，仅用于把生成结果回填到 self.current_broadcast。
            if self.current is not None:
                self._refill()  # 选曲后补齐缺口
                next_title = self.upcoming[0]["track"].title if self.upcoming else None
                seed_entry = {"track": self.current, "broadcast": None,
                              "reason": self.last_recommend_reason or "用户点播",
                              "_gen": False}
                # ★★ 2026-11-18 根治：首曲固定开场白**同步准备**（零 LLM、现成 wav），
                #   不走 _gen_broadcast 后台线程（竞态会丢口播）。同步设 current_broadcast
                #   后 _play_current 立即拿到、_speak_intro 立刻播，watchdog 不会误判跳过。
                self._kick_taste_background()   # 后台慢慢分析，不阻塞导听
                _bc = self._sync_opening_broadcast(self.current)
                if _bc is not None:
                    self.current_broadcast = _bc
                    seed_entry["broadcast"] = _bc
                # 同步失败才回退后台 opening（极端情况：音频文件缺失）
                else:
                    self._gen_broadcast(
                        seed_entry, position=1, total=1 + len(self.upcoming),
                        next_title=next_title, opening=True)
                self._play_current(force_intro=self.dj_on, wait_intro=True)
            # else：异步填充（LLM 开启且开场曲尚未就绪）—— 留待 _fill_from_reco 末尾
            #       _maybe_start_deferred 选曲并开播，期间推荐列表先以"生成中"呈现。
        finally:
            # 无论主体是否异常，都释放「正在开播」标记；若有期间到达的最新请求则采纳。
            with self._start_lock:
                self._starting = False
                pending = self._pending_seed
                self._pending_seed = None
            if pending is not None:
                self.start_radio(seed_track=pending[0], auto_start=pending[1])

    def _maybe_start_deferred(self) -> None:
        """后台填充完成后，若尚处于「等待开场」状态则真正开播。"""
        if getattr(self, "_need_start", False):
            self._need_start = False
            self._start_playback_if_needed()

    def _start_playback_if_needed(self) -> None:
        """异步填充完成后：若尚未选曲，取待播首首作为当前曲并开播，保证电台不会静默。"""
        if self.current is not None:
            return
        with self._qlock:
            has = bool(self.upcoming)
        if not has:
            return
        self._pop_to_current()
        self._emit_playlist()
        self._refill()  # 选曲后补足待播
        next_title = self.upcoming[0]["track"].title if self.upcoming else None
        seed_entry = {"track": self.current, "broadcast": None,
                      "reason": self.last_recommend_reason or "电台开场",
                      "_gen": False}
        # ★★ 2026-11-18 根治：首曲固定开场白同步准备（零 LLM、现成 wav），
        #   不走后台线程竞态。同步设 current_broadcast 保证 _speak_intro 立即播。
        self._kick_taste_background()   # 后台慢慢分析，不阻塞导听
        _bc = self._sync_opening_broadcast(self.current)
        if _bc is not None:
            self.current_broadcast = _bc
            seed_entry["broadcast"] = _bc
        else:
            self._gen_broadcast(
                seed_entry, position=1, total=1 + len(self.upcoming),
                next_title=next_title, opening=True)
        self._play_current(force_intro=self.dj_on, wait_intro=True)

    def reset_context(self) -> None:
        """应用启动时清空 LLM/DJ 的「上下文」：最近播放轻量缓冲、口播时序计数等，
        确保每次启动都从干净状态开始（DJ 不残留上一会话的连贯上下文），
        避免「重启后 DJ 还在呼应上一次收听过的歌」。

        注意：本进程每次启动这些字段本就是空的；本方法主要用于
        （1）显式满足「启动即清空」的诉求；（2）应用内「停止电台再重开」时
        也能真正重置，而不是带着上一轮上下文继续。
        """
        self._recent_played = []
        self._taste_profile = None
        self._taste_dirty = True
        self._taste_from_history = False
        self._broadcast_seq = 0
        self._last_weather_seq = 0
        self._last_fail = None
        self.reco_buffer = []
        self._reco_loading = False
        if self._reco_retry_timer is not None:
            try:
                self._reco_retry_timer.cancel()
            except Exception:  # noqa
                pass
            self._reco_retry_timer = None
        self._manual_queue = False
        logger.info("[radio] 已清空 LLM/DJ 上下文（应用启动）")

    def _should_web_search(self) -> bool:
        """联网搜索开关：手动强制开启（用户要求开关=强制联网，不让 AI 自动决定）。

        - 开关关 → 永不联网；
        - 开关开 → 每首歌都真正联网检索（DuckDuckGo，qwen/Ollama 后端），
          为 DJ 口播注入实时资料。不再做 interval 节流（那会让"开了却没搜"）。
        """
        return bool(getattr(self.brain, "web_search", False))

    def _resolve_url_cached(self, track) -> str | None:
        """R99-T3 **读穿缓存**：先查 `track_cache(url)`，未命中再走网络并写回（TTL 6h）。

        为什么必须带 TTL（而不是像旧实现那样"绝不写回库"）：曲库只存占位地址，
        每次播放/预取都要一次网络往返（R96 §2.1 实测）。TTL 解决了两难 ——
        窗口内只取一次，到期即失效重建，所以**不会把死链写死**（那正是旧注释的顾虑）。

        三条边界：
          · 本地曲（source 为空/`local`）与无 `ext_id` 的曲 **不进缓存**：直接透传
            （本地曲的 path 本身就是可播地址，缓存它只会多一次查表）；
          · **失败不写缓存**：瞬时 503/限流若被缓存 6h = 这首歌"坏一整天"
            （比每次重试更糟）；重试逻辑仍在未命中路径上；
          · DB 不可用 → 静默降级为纯网络（缓存是优化，不是依赖）。
        返回真实直链；失败返回 None（调用方照旧走"音源失效"分支）。

        ★★★ R114（2026-09-13）**补上第三条边界的一个漏洞：只防"没取到"，没防"取到了但被降级"**。
        实测缺陷：同一首歌的直链**可能指向降级短对象**（`…/jd-musicrep-ts/…`，**20 s**）
        而不是完整曲（`…/jdymusic/obj/…`，300 s）——用户观察到的"会员/非会员取到的时长不同"。
        旧判据只有 `if url:` ⇒ **降级短链被当成好链写进 6h 缓存** ⇒ 之后每次播都只有 20 s；
        且因 URL 不变、`sha1(url)` 的落盘流也跟着命中，**连重下都不会发生** ⇒
        表现成「播几秒就自动切歌、进度条拖不动、别的歌却正常」。
        ⇒ 现在**取链即验货**（`data/sources/url_health.py`）：不健康 ⇒ **不写缓存**
        且**原地重取**（`DEGRADED_RESOLVE_TRIES`）；全部不健康仍返回 best-effort URL
        （不阻断播放），并由播放侧的**短音频自愈**删掉这条坏缓存。
        """
        db = getattr(self, "db", None)
        src = str(getattr(track, "source", "") or "")
        ext = getattr(track, "ext_id", None)
        if db is None or not src or src == "local" or not ext:
            return self._resolve_with_retry(track)
        try:
            hit = db.get_cache("url", src, ext)
        except Exception:  # noqa 缓存读异常视同未命中（绝不让优化拖垮播放）
            hit = None
        _bst = getattr(self, "_bump_cache_stat", None)   # 混入缺失时静默跳过
        if hit and self._cache_hit_ok(track, src, ext, hit, db):
            if _bst:
                _bst("url", "hit")
            return hit
        if hit and _bst:
            _bst("url", "hit-stale")     # 命中但复核判坏 ⇒ 当未命中（会重取并重写）
        if _bst:
            _bst("url", "miss")
        url, info = self._resolve_checked(track)
        if url:
            # ★ R114：**验货通过才写缓存** —— 否则这首歌会被钉死一个 6h 的坏链
            if _url_is_degraded(info, getattr(track, "duration", None)):
                if _bst:
                    _bst("url", "rejected")
                logger.warning(
                    "[R114] 直链验货不通过 ⇒ **不写缓存**（%s/%s，%s）—— 下次播放会重新取链",
                    src, ext, _url_desc(info, getattr(track, "duration", None)))
                return url
            try:
                db.set_cache("url", src, ext, url)
            except Exception:  # noqa
                pass
        return url

    def _cache_hit_ok(self, track, src, ext, url, db) -> bool:
        """命中的缓存直链**是否仍然可用**（R114）。年轻条目**直接放行（零网络）**。

        ★ 为什么必须复核：直链带签名会过期（实测 netease **~1 小时内全部 403**），
        而 `CACHE_TTL["url"]` 是 **6h** ⇒ 不复核就会拿死链去下载 ⇒ 加载失败 ⇒ 跳歌。
        复核本身极廉价（`Range: bytes=0-0`，只读响应头），且**只对年龄 ≥ `URL_RECHECK_AGE_S`
        的条目**做 ⇒ 短时间反复播（最常见）完全不损 T3 的"命中零网络"。
        判坏 ⇒ 删掉该条并返回 False（调用方按未命中重取）。
        `unknown`（网络抖动等）⇒ **fail-open 放行**，绝不因探活失败而拦播放。
        """
        try:
            row = db.get_cache_row("url", src, ext) or {}
            age = float(row.get("age_s") or 0.0)
        except Exception:  # noqa 读不出年龄 ⇒ 放行
            return True
        if age < URL_RECHECK_AGE_S:
            return True
        verdict = _probe_url_health(url, getattr(track, "duration", None))
        if verdict == "bad":
            # ⚠ 委托唯一入口（`_invalidate_url_cache`），别在这里再写一次 `db.del_cache`
            #   —— `verify_t3_cache.py` 的 `D14` 会数 `ai/` 里 `db.del_cache("url"` 的出现次数。
            self._invalidate_url_cache(track, why=f"缓存直链复核判坏（age={age:.0f}s）")
            return False
        return True

    def _invalidate_url_cache(self, track, why: str = "") -> bool:
        """**失效一首歌的直链缓存**（R114 自愈）—— 唯一入口，别在别处再写 `del_cache("url")`。

        两种触发（都实测过）：
          · **短音频**：拿到降级/试听片段（20 s）或下载被截断 ⇒ 播完位置 ≪ 声明时长；
          · **加载失败**：直链已过期（403）⇒ `_download_stream` 失败 ⇒ 该曲被跳过。
        没有这一步，坏链会被 6h TTL 钉死 ⇒ 同一首每次都坏（用户实测"反复播 9 次全是 20 s"）。
        返回是否真的删了；任何异常一律吞掉（自愈永远不许影响播放）。
        """
        try:
            db = getattr(self, "db", None)
            src = str(getattr(track, "source", "") or "")
            ext = getattr(track, "ext_id", None)
            if db is None or not src or src == "local" or not ext:
                return False
            db.del_cache("url", src, ext)
            logger.warning("[R114] 已失效直链缓存 %s/%s（原因：%s）⇒ 下次播放会重新取链",
                           src, ext, why or "未注明")
            return True
        except Exception as e:  # noqa 自愈失败绝不影响播放
            logger.debug("[R114] 失效直链缓存失败（忽略）: %s", e)
            return False

    def _resolve_info_with_retry(self, track, attempts: int = 3) -> dict | None:
        """取一次「直链 + 证据」（`resolve_url_info`），失败重试（沿用 R99 的重试口径）。

        ⚠ 音源未实现 `resolve_url_info`（或测试桩只有 `resolve_url`）时**自动退回**
        `resolve_url` 并包成 `{"url": ...}` ⇒ 判据走 fail-open，行为与旧版一致。
        """
        sm = getattr(self, "source_manager", None)
        if sm is None:
            return None
        fn = getattr(sm, "resolve_url_info", None)
        last_err = None
        for i in range(max(1, attempts)):
            try:
                if fn is not None:
                    info = fn(track)
                    if info and info.get("url"):
                        return info
                else:
                    url = sm.resolve_url(track)
                    if url:
                        return {"url": url}
            except Exception as e:  # noqa 解析过程异常也视作失败并重试
                last_err = e
            if i < attempts - 1:
                time.sleep(0.5)
        if last_err is not None:
            logger.warning("resolve_url_info 异常（已重试 %d 次）: %s", attempts, last_err)
        return None

    def _resolve_checked(self, track) -> tuple[str | None, dict | None]:
        """取直链并**验货**（R114）。返回 `(url, info)`（`url=None` ⇒ 彻底取不到）。

        三档结果：
          · 取不到 ................ 沿用失败重试；仍无 ⇒ `(None, None)`（老路"音源失效"）；
          · 取到但不健康 .......... **再取**（最多 `DEGRADED_RESOLVE_TRIES` 次）；
          · 取到且健康 / 全部不健康 ⇒ 返回 `(url, info)`，**是否写缓存由调用方判**。
        """
        dur = getattr(track, "duration", None)
        tries = int(getattr(RadioConfig, "DEGRADED_RESOLVE_TRIES",
                            DEGRADED_RESOLVE_TRIES) or DEGRADED_RESOLVE_TRIES)
        best: tuple[str | None, dict | None] = (None, None)
        for i in range(max(1, tries)):
            info = self._resolve_info_with_retry(track)
            url = (info or {}).get("url") or None
            if not url:
                return best if best[0] else (None, None)
            best = (url, info)
            if not _url_is_degraded(info, dur):
                return best
            logger.warning("[R114] 第 %d/%d 次取链判为**降级短链**（%s）⇒ 重取",
                           i + 1, tries, _url_desc(info, dur))
            if i < tries - 1:
                time.sleep(0.5)
        logger.warning("[R114] 重取 %d 次仍为降级短链 ⇒ 本次按 best-effort 播，但不写缓存",
                       tries)
        return best

    def _bump_cache_stat(self, kind: str, what: str) -> None:
        """运行期缓存命中计数（真机可读；不进任何热判断）。"""
        try:
            d = getattr(self, "_cache_stats", None)
            if d is None:
                d = {}
                self._cache_stats = d
            d[f"{kind}_{what}"] = int(d.get(f"{kind}_{what}", 0)) + 1
        except Exception:  # noqa
            pass

    def cache_stats(self) -> dict:
        """R99-T3 可观测面：{命中/未命中计数} ∪ {表内条数与新鲜度}（真机 F5 一行可读）。"""
        out = dict(getattr(self, "_cache_stats", None) or {})
        try:
            out["table"] = self.db.cache_stats() if getattr(self, "db", None) else {}
        except Exception:  # noqa
            out["table"] = {}
        return out

    def _resolve_with_retry(self, track, attempts: int = 3) -> str | None:
        """把远程占位曲目解析为实时直链，失败重试以应对 API 瞬时抖动。

        按理通过 API 取直链不应失败，但实际会遇到限流 / 503 / 偶发空响应，
        重试可消化绝大多数瞬时故障，避免无辜跳歌。"""
        last_err = None
        for i in range(attempts):
            try:
                url = self.source_manager.resolve_url(track)
            except Exception as e:  # noqa: 解析过程异常也视作失败并重试
                url = None
                last_err = e
            if url:
                return url
            if i < attempts - 1:
                time.sleep(0.5)
        if last_err is not None:
            logger.warning("resolve_url 异常（已重试 %d 次）: %s", attempts, last_err)
        return None

    def _skip_unplayable(self, track) -> None:
        """一首歌音源失效（解析失败 / 文件丢失 / 加载失败）时，跳过它并续播下一首，
        确保播放不被单点失败卡死。连续失效过多则暂停电台，避免死循环。

        ⚠ R57-3 修复「播放的根本站不住 / 一次连跳好几首」：
        旧阈值 `max(len(upcoming)+3, 6)` 随队列长度增长 —— 待播恒 5 首满时
        等价于「连跳 8 首才停」，用户观感 = 疯狂连跳风暴（日志实测 08-25 连跳 7 首
        才触发暂停，每首仅隔 1.2s）。音源登录态失效 / API 批量失败时，逐首 resolve_url
        全部落空，表现为"点一首跳一首、自动播放也站不住"。
        新策略：
          1) 固定刹车阈值 SKIP_STORM_LIMIT=3：**连跳 3 首即停**，不等 7~8 首风暴跑完。
          2) 暂停时广播 radio_error（前端可 toast），不再只是 dj_speech 文字。
          3) 暂停后顺带清空待播队列：这批候选大概率同源同病（登录态/网络），
             留着也只是再触发一轮风暴。"""
        if not self._is_track(track):
            return
        # ★ R114：这条链刚刚**加载失败**（下载 403 / 文件丢失 / 直链失效）⇒ **失效它的直链缓存**。
        #   实测 netease 签名直链 ~1 小时内即 403，而 `CACHE_TTL["url"]` 是 6h ⇒ 若不删，
        #   同一首歌在 TTL 内**每次都跳**（且连跳会触发下面的风暴刹车把电台暂停）。
        #   放在 `_consecutive_skips` 之前：无论后面是否刹车，这条坏链都该被清掉。
        self._invalidate_url_cache(track, why="加载失败（音源失效/直链过期）")
        self._consecutive_skips += 1
        cap = getattr(RadioConfig, "SKIP_STORM_LIMIT", 3) or 3
        if self._consecutive_skips >= cap:
            logger.warning("连续 %d 首音源失效，电台暂停（R57-3 风暴刹车）",
                           self._consecutive_skips)
            self._emit("radio_error", {
                "kind": "source_storm",
                "title": getattr(track, "title", "") or "",
                "count": self._consecutive_skips,
                "message": "连续多首音源失效，电台已暂停。请检查网络或音源登录状态。",
            })
            self._emit("dj_speech", "连续多首音源失效，电台已暂停。请检查网络或音源登录状态。")
            try:
                self.player.stop()
            except Exception:  # noqa
                pass
            self._clear_preparing()      # R95d-Q0：电台已停 → 清意图（不再误报 playing）
            self._emit("playing", False)
            self.current = None
            with self._qlock:
                self.upcoming = []          # 同源候选清空，避免再来一轮风暴
            self._consecutive_skips = 0
            self._emit_playlist()
            return
        # 防御：清除待播队列里与失效曲同 id 的条目（含单曲循环重入），避免无限循环
        with self._qlock:
            self.upcoming = [
                e for e in self.upcoming
                if not self._is_track(e.get("track"))
                or getattr(e["track"], "id", None) != track.id
            ]
        self._advance(intro_already_played=False, manual=False, _from_skip=True)

    def _advance(self, intro_already_played: bool = False, manual: bool = False,
                 _from_skip: bool = False) -> None:
        # 重入防护：_on_track_ended（位置轮询线程）与 _begin_transition（Timer 线程）
        # 都可能触发；若同时到达，后者直接丢弃，避免并发 _advance 互相踩踏导致卡死。
        # 防御：即便 __init__ 未运行（测试桩用 __new__ 构造）也不崩。
        if not getattr(self, "_advance_lock", None):
            self._advance_lock = threading.Lock()
        if not self._advance_lock.acquire(blocking=False):
            return
        try:
            self._clean_upcoming()  # 防御：下方读取 e["track"].id / .title
            self._cancel_transition()
            self._transitioning = False
            was_first = self.current is None
            with self._qlock:
                if not self.upcoming:
                    if getattr(self, "_manual_queue", False):
                        # 手动队列（曲库播放全部等）播完：交还 AI 续推
                        self._manual_queue = False
                    if self.roam_on:
                        # ★ 2026-09-19：用户队列（曲库/歌单）播完 → 交还 AI 续推 ⇒ 来源变 'roam'
                        self.queue_src = "roam"
                        self._refill()
                if not self.upcoming:
                    self._emit("dj_speech", "推荐已用完，电台停止。可重新开启电台模式或手动点播。")
                    self.player.stop()
                    self._clear_preparing()   # R95d-Q0：队列播完 → 清意图
                    self._emit("playing", False)
                    self.current_broadcast = None
                    self._emit_playlist()
                    return
                # ⚠ 不要在这一步做随机取歌：AI 口播是按 upcoming[0] 预先生成的
                # （_prep_next_broadcast），若等到播放时才随机挑一首，播的就不是
                # 已经备好旁白的那首 → "旁白与歌曲对不上"。
                # random 的正确落点是**填充播放列表时打乱顺序**
                # （见 _shuffle_upcoming_if_random），保证列表顺序 = 实际播放顺序，
                # AI 从播放列表取歌即可。
                entry = self.upcoming.pop(0)
            logger.info(
                "[radio] _advance 切歌: 《%s》→《%s》 manual=%s skip=%s intro_already=%s",
                (getattr(self.current, "title", None) if self.current else None),
                entry["track"].title, manual, _from_skip, intro_already_played)
            # 释放上一首及其口播
            self.current_broadcast = None
            self.current = entry["track"]
            self.current_id = entry["track"].id
            self.current_broadcast = entry["broadcast"]
            # 确保「成为当前曲的这首歌」有口播：正常情况它此前已在待播列表里被
            # _prep_next_broadcast 预生成好（第一首开播后、且每首开播都会再触发一次，
            # 由 _gen/_gen_t 守卫保证幂等）；若因跳转插入等路径未被预生成
            # （entry["broadcast"] 仍为 None），这里补生成，避免这首歌无声无口播。
            # 若是「第一首」（was_first，即电台尚未播放过任何歌），用默认开场白
            # （地点+天气+温度+歌名）；否则用常规 AI DJ 口播。
            # dj_on 仅门控音频输出，故「始终生成文案」（AI 持续思考）。
            if self.current_broadcast is None:
                up = list(self.upcoming)
                total = 1 + len(up)
                nxt = up[0]["track"].title if up else None
                if was_first:
                    # ★★ 2026-11-18 根治：首曲固定开场白**同步准备**，不走后台线程
                    #   （竞态会丢口播）。直接给 current_broadcast，_play_track 立即可播。
                    self._kick_taste_background()   # 后台慢慢分析，不阻塞导听
                    _bc = self._sync_opening_broadcast(entry["track"])
                    if _bc is not None:
                        self.current_broadcast = _bc
                        entry["broadcast"] = _bc
                    else:
                        self._gen_broadcast(entry, position=1, total=total,
                                            next_title=nxt, opening=True, force=True)
                else:
                    self._gen_broadcast(entry, position=2, total=total, next_title=nxt, force=True)
            # ---- 计时埋点：预取兜住检测（本首口播就绪时刻距"成为当前曲"的提前量）----
            gen_at = entry.get("_gen_at")
            if gen_at is not None:
                lead_ms = (time.monotonic() - gen_at) * 1000.0
                dur_ms = (entry["track"].duration or 0) * 1000.0
                ready = lead_ms >= 0
                logger.info(
                    "[Timing] 预取就绪提前量 seq=%s 《%s》 lead=%.0fms song=%.0fms %s",
                    entry.get("_gen_seq"), entry["track"].title, lead_ms, dur_ms,
                    "✅已备好(先想好再播)" if ready else "⚠️未备好(将重叠/看门狗)")
                self._emit("dj_timing", {
                    "phase": "prefetch", "seq": entry.get("_gen_seq"),
                    "title": entry["track"].title, "lead_ms": round(lead_ms),
                    "song_ms": round(dur_ms), "ready": ready,
                })
            self.played.add(self.current_id)
            self.history.insert(0, self.current)
            self.last_recommend_reason = entry.get("reason", "")
            # 单曲循环：把当前曲放回待播尾部，保证不会断
            # （音源失效跳过的场景下不重入，否则失效曲会无限循环）
            # ★ 2026-09-19：用**有效模式**（漫游/推荐下强制 order ⇒ 不会把 AI 队列变成单曲循环）
            if self._mode_effective() == "loop_one" and not manual and not _from_skip:
                loop_entry = self._make_entry(self.current, "单曲循环")
                with self._qlock:
                    # ⚠ 必须插到**队首**：append 会让它排在整个待播队列之后，
                    # 用户感知是"先把队列播完才重复"，等于单曲循环没生效。
                    self.upcoming.insert(0, loop_entry)
            self._refill()
            # 播放进行中：同步把推荐货架补满（AI 常驻思考，保证推荐页始终有歌可点）。
            self._refill_reco()
            force_intro = self.dj_on and not intro_already_played
            self._play_track(self.current, self.current_broadcast, fade_in=True,
                             force_intro=force_intro)
            self._emit("recommend_reason", self.last_recommend_reason)
            self._emit_history()
            self._emit_playlist()
        finally:
            self._advance_lock.release()

    def next(self, strategy: str | None = None) -> None:
        """手动下一首（跳过当前）。转场口播播放中忽略，避免与转场收尾的 advance 竞争。"""
        if strategy:
            self.play_mode = strategy
        if getattr(self, "_transitioning", False):
            return
        self._advance(intro_already_played=False, manual=True)

    def prev(self) -> None:
        """回到上一首（从历史）。转场口播播放中忽略，避免与转场收尾的 advance 竞争；
        并与 _advance 互斥，避免快速连点导致状态错乱 / 播放列表出现两首相同歌。"""
        if getattr(self, "_transitioning", False):
            return
        if not self._advance_lock.acquire(blocking=False):
            return
        try:
            self._clean_upcoming()  # 防御：下方读取 e["track"].id
            self._cancel_transition()
            if len(self.history) >= 2:
                old = self.current                       # 即将离开的当前曲
                prev_track = self.history[1]
                self.history = [self.history[0]] + self.history[2:]
                self.current = prev_track
                self.current_id = prev_track.id
                with self._qlock:
                    # 把「刚离开的当前曲」放回待播首位，保证可再「下一首」回到它；
                    # 绝不能把 prev_track（现在是 current）塞进 upcoming，否则会与
                    # current 重复显示成两首相同的歌。同时去重并裁剪到固定长度。
                    if (old is not None
                            and self._is_track(old)
                            and not any(e["track"].id == old.id
                                        for e in self.upcoming
                                        if self._is_track(e.get("track")))):
                        self.upcoming.insert(0, self._make_entry(old, "回到上一首"))
                    # 防御：若 prev_track 因任何原因仍在待播里（它现在是 current），移除
                    self.upcoming = [
                        e for e in self.upcoming
                        if self._is_track(e.get("track"))
                        and e["track"].id != prev_track.id
                    ]
                    # 当前曲独立计数，待播恒为 PLAYLIST_SIZE-1 首
                    self.upcoming = self.upcoming[: RadioConfig.PLAYLIST_SIZE - 1]
                self._play_track(prev_track, None, fade_in=True, force_intro=self.dj_on)
                self._emit_playlist()
        finally:
            self._advance_lock.release()

    def _pull_from_reco_sync(self, n: int) -> int:
        """同步从推荐货架 reco_buffer 复制 n 首到播放列表 upcoming（只复制不移动）。

        供 jump_to 等「需立即拿到可播曲目」的同步路径使用——避免依赖 _fill 的异步
        后台线程导致 pop(0) 在队列空时崩溃（pop from empty list）。复制语义与
        _fill_from_reco 一致（只复制不移动）。返回实际复制数，永不抛异常。"""
        with self._qlock:
            exclude = (set(self.played) | {self.current_id} | self.disliked)
            up_ids = {e["track"].id for e in self.upcoming
                      if self._is_track(e.get("track"))}
            pulled: list = []
            for e in self.reco_buffer:
                t = e.get("track")
                if len(pulled) >= n:
                    break
                if not self._is_track(t) or t.id in exclude or t.id in up_ids:
                    continue
                pulled.append(e)
            for e in pulled:
                if not any(x["track"].id == e["track"].id for x in self.upcoming):
                    self.upcoming.append(e)
        return len(pulled)

    def jump_to(self, index: int) -> None:
        """点击列表第 index 首（0=当前，1..4=待播）：它前面的歌出列并补足。
        转场口播播放中忽略，避免与转场收尾的 advance 竞争；并与 _advance 互斥，
        避免快速连点导致状态错乱。"""
        if getattr(self, "_transitioning", False):
            return
        if not self._advance_lock.acquire(blocking=False):
            return
        try:
            self._clean_upcoming()  # 防御：下方读取 e["track"]
            self._cancel_transition()
            with self._qlock:
                if index <= 0:
                    return
                # 释放前面 index 首（含当前）
                released = [self.current] + [e["track"] for e in self.upcoming[: index - 1]]
                self.upcoming = self.upcoming[index - 1:]
                if not self.upcoming:
                    # ⚠ 同步保底：LLM 开启时 _refill→_fill 仅起后台线程立即返回，
                    #   upcoming 仍空，直接 pop(0) 会抛「pop from empty list」。
                    #   先同步从已满架的推荐货架 reco_buffer 复制至少 1 首（只复制不移动），
                    #   保证 pop 不崩；其余缺口由下方 _fill（AI 后台续推）补足。
                    self._pull_from_reco_sync(1)
                if not self.upcoming:
                    # 货架也空（极端冷启动 / AI 彻底不可用）：本地兜底 1 首，杜绝静默崩溃。
                    self._local_fallback(1)
                entry = self.upcoming.pop(0)
            self.current_broadcast = None
            for t in released:
                if t is not None:
                    self.played.discard(t.id)
            self.current = entry["track"]
            self.current_id = entry["track"].id
            self.current_broadcast = entry["broadcast"]
            self.played.add(self.current_id)
            self.history.insert(0, self.current)
            need = index  # 补足被释放的数量
            self._fill(need)
            self._play_track(self.current, self.current_broadcast, fade_in=True,
                             force_intro=self.dj_on)
            self._emit_history()
            self._emit_playlist()
            # 当前曲变化后刷新推荐页（排除新当前曲/新队列），并保持 5 首
            self._refill_reco()
        finally:
            self._advance_lock.release()

    # R25：「随机一首」功能整体下线 —— 入口 recommend_one_now 与专属内部实现
    # （_ai_recommend_one / _do_recommend_one / _fill_reco_reason）一并删除。
    # 推荐/起播统一走 start_radio / 推荐货架点播，不再有"跳过队列直接推一首"的旁路。

    # ---------- 队列 / 曲库 操作（UI 调用） ----------

    def toggle_favorite(self, track) -> bool:
        """红心：把歌加入/移出曲库（我喜欢的音乐）。直链源先入库。

        重活（DB 写入 + 曲库 reload）后台化，避免主线程卡顿；红心状态先在内存
        切换并立即广播，UI 瞬时响应（修复「点红心卡一下」）。
        """
        if track is None:
            return False
        key = self.fav_key(track)
        # 内存立即切换：与当前内存状态相反（_queue_fav 在启动时已从 DB 正确载入）
        new_fav = key not in self._queue_fav
        if new_fav:
            self._queue_fav.add(key)
        else:
            self._queue_fav.discard(key)
        self._emit("favorite", new_fav)
        self._emit_playlist()
        self._emit_history()   # 当前曲可能在历史里，点红心后立即刷新历史
        self._emit_reco()      # 推荐货架红心即时刷新（否则要等到下一首播放才刷新）
        # 重活后台化：入库（若需要）+ 写收藏，完成后回主线程刷新曲库视图
        # ⚠ R49-24：把**原 Track 引用**一起传给后台 —— 推荐/货架的在线曲目 id 是
        #   临时负 id，后台仅凭 id 重解析必然失败（解析不到 → 永不入库）。
        #   持久化只依赖 Track 的元数据 + cover，负 id 在 add_track 内部会被
        #   upsert 重分配成稳定 db id，不构成问题。
        threading.Thread(target=self._fav_persist,
                         args=(track, new_fav, key), daemon=True).start()
        return new_fav

    def _load_fav_state(self) -> None:
        """启动时把 DB 中已收藏的曲目键载入 _queue_fav，保证播放列表/推荐/历史
        的红心状态跨会话一致（此前 _queue_fav 仅含本次会话显式点过的，导致上一会话
        或曲库收藏的歌在播放列表里不显示红心）。

        注意：本地音乐通过 ``favorite_all_local`` 被自动标记为"在曲库"，这些歌在曲库
        列表显示红心；推荐/历史/播放列表里则统一按「稳定键」判断——本地歌用 db id 键
        （``i:{tid}``），在线歌用 音源+原生 id 键（``e:{src}:{ext}``），与运行时
        toggle_favorite 写入的键完全一致，因此收藏状态在任意列表都能稳定亮红心。

        用「稳定键」重建：在线歌（netease/qq/spotify）用 音源+原生 id；其余用 db id。
        这样即便推荐每次重建临时 id，同一首歌的红心也能稳定匹配。
        """
        try:
            all_ids = self.db.favorite_ids()
            keys: set[str] = set()
            for tid in all_ids:
                row = self.db.get_track(tid)
                if row is None:
                    continue
                src = (row["source"] or "local")
                ext = row["ext_id"]
                if src in ("netease", "qqmusic", "spotify") and ext not in (None, ""):
                    keys.add(f"e:{src}:{ext}")
                else:
                    keys.add(f"i:{tid}")
            self._queue_fav = keys
        except Exception:  # noqa
            self._queue_fav = set()

    def fav_key(self, track) -> str:
        """收藏状态稳定键：在线歌用 音源+原生 id，本地/无 ext_id 用 db id。

        关键修复：推荐每次为同一首在线歌生成新的临时 id，若只用 id 作键，
        上一会话收藏的歌在新会话会因 id 变化而匹配不上、红心丢失（卡红心）。
        用音源+原生 id 可在跨会话/跨临时 id 下稳定识别同一首歌。
        """
        src = (getattr(track, "source", None) or "")
        ext = getattr(track, "ext_id", None)
        if src in ("netease", "qqmusic", "spotify") and ext not in (None, ""):
            return f"e:{src}:{ext}"
        return f"i:{getattr(track, 'id', None)}"

    def is_favorite_track(self, track) -> bool:
        """按稳定键判定某 Track 是否已收藏（供 UI 列表用）。"""
        return self.fav_key(track) in self._queue_fav

    def is_favorite(self, track_id: int) -> bool:
        """按 db id 判定收藏（保留给按 id 查询的场景）。"""
        return f"i:{track_id}" in self._queue_fav

    # ---------- 不喜欢的歌（隐藏库，对 AI 生效） ----------
    def is_disliked_track(self, track) -> bool:
        """按稳定键判定某 Track 是否被标记为"不喜欢"。"""
        return self.fav_key(track) in self.disliked_keys

    def dislike_by_id(self, track_id: int, on: bool) -> None:
        """👎 标记/取消"不喜欢"：进入/移出隐藏的不喜欢库，并对 AI 避雷生效。

        - on=True：解析出 Track，按稳定键写入 db.dislikes（持久化，跨会话生效），
          加入 disliked_keys 与本会话排除集；若该曲正在待播/推荐货架中则一并移除。
        - on=False：从 db.dislikes 与 disliked_keys 移除（撤销不喜欢）。
        同步刷新播放列表/推荐货架/历史/曲库（隐藏库视图）。
        """
        track = self._resolve_track(track_id)
        key = self.fav_key(track) if track is not None else f"i:{track_id}"
        title = getattr(track, "title", None)
        artist = getattr(track, "artist", None)
        src = (getattr(track, "source", None) if track is not None else None)
        ext = (getattr(track, "ext_id", None) if track is not None else None)
        tid = track.id if track is not None else track_id
        if on:
            self.disliked_keys.add(key)
            self.disliked.add(tid)
        else:
            self.disliked_keys.discard(key)
            self.disliked.discard(tid)
        # 持久化到隐藏的不喜欢库（key 唯一，重复点击幂等）
        self.db.set_dislike(tid if tid is not None else None, src, ext,
                            title, artist, key, on)
        if on:
            # ⚠ R62 #20：👎 = 放进不喜欢 + **移除播放** —— 若标记的恰是
            # 当前正在播的曲目，须 next() 跳过（与 remove_from_queue 同款）；
            # 否则 👎 正在放的歌只清待播、current 继续播 → "移除播放"落空。
            if track is not None and track.id == self.current_id:
                self._clean_upcoming()
                self.next()
            else:
                # 不喜欢则立即把它从待播/推荐货架清掉，避免继续播放/推荐
                self._clean_upcoming()
                with self._qlock:
                    self.upcoming = [e for e in self.upcoming
                                     if not (self._is_track(e.get("track"))
                                             and self.fav_key(e["track"]) == key)]
                    reco_before = len(self.reco_buffer)
                    self.reco_buffer = [e for e in self.reco_buffer
                                        if not (self._is_track(e.get("track"))
                                                and self.fav_key(e["track"]) == key)]
                    reco_removed = reco_before != len(self.reco_buffer)
                self._emit_playlist()
                if reco_removed:
                    self._emit_reco()
                    self._refill_reco()
            self._emit("dj_speech", f"👎 已标记不喜欢：《{title or '未知'}》")
        else:
            self._emit("dj_speech", f"♻ 已撤销不喜欢：《{title or '未知'}》")
        self._emit_history()
        self._emit_library()

    def get_disliked_tracks(self) -> list[dict]:
        """隐藏的"不喜欢的歌曲"库内容，供曲库页浏览/撤销。

        返回每行 dict：{row_id, track_id, source, ext_id, title, artist, key}。
        本地歌有 track_id；外部歌仅含元数据（无 track_id）。
        """
        try:
            return [dict(r) for r in self.db.dislike_rows()]
        except Exception:  # noqa
            return []

    def undo_dislike_by_key(self, key: str) -> None:
        """按稳定 key 撤销"不喜欢"：从隐藏库移除（外部歌也按 key 精确撤销）。"""
        self.disliked_keys.discard(key)
        # 同步从本会话排除集移除：找出内存里 key 匹配的所有 track id
        pool = list(self.history)
        if self.current is not None:
            pool.append(self.current)
        with self._qlock:
            pool += [e["track"] for e in self.upcoming]
        for t in pool:
            if t is not None and self.fav_key(t) == key:
                self.disliked.discard(t.id)
        self.db.set_dislike(None, None, None, None, None, key, False)
        self._emit("dj_speech", "♻ 已撤销不喜欢")
        self._emit_history()
        self._emit_library()

    def dislike_by_key(self, key: str, on: bool = True,
                       title: str | None = None,
                       artist: str | None = None) -> None:
        """按稳定 key 标记/取消「不喜欢」（在线歌无本地 db id 时的入口）。

        ⚠ R50：与 dislike_by_id 同语义 —— **内存 disliked_keys + db 持久化 +
        从待播/推荐货架剔除**三处必须一致。旧 bridge 实现（在线分支）只写 db：
        is_disliked_track 判定的是内存集合，不加的话 get_state 恒报 false，
        前端点亮态会被 2s 轮询打回（"点了不喜欢没反应"）。
        本方法是 undo_dislike_by_key 的 add 侧对称方法。

        ⚠ R62 #23：在线曲按 key 无法解析回完整 Track，调用方（前端卡片/
        侧边面板）点 👎 时手上就有条目元数据 → 通过 title/artist 透传落库，
        否则 dislikes 表里 title/artist 恒 NULL → 界面显示「未知歌手/未知歌曲」。
        解析得到 Track 时以 Track 为准（self._resolve_meta(key)）。
        """
        key = str(key or "").strip()
        if not key:
            return
        if not on:
            self.undo_dislike_by_key(key)
            return
        # R62 #23：先尝试按 key 从内存/DB 解析真实元数据，调用方没传时兜底
        meta = self._resolve_dislike_meta(key)
        if title is None:
            title = meta.get("title")
        if artist is None:
            artist = meta.get("artist")
        self.disliked_keys.add(key)
        # ⚠ R62 #20：👎 = 放进不喜欢 + 移除播放 —— key 若匹配当前正在播的曲，
        # 立即 next() 跳过（在线歌无 db id，用 fav_key 判定；与 dislike_by_id
        # 的 current 处理同语义）。否则 👎 正在放的歌 current 继续放。
        is_current = (self.current is not None
                      and self._is_track(self.current)
                      and self.fav_key(self.current) == key)
        with self._qlock:
            self.upcoming = [e for e in self.upcoming
                             if not (self._is_track(e.get("track"))
                                     and self.fav_key(e["track"]) == key)]
            reco_before = len(self.reco_buffer)
            self.reco_buffer = [e for e in self.reco_buffer
                                if not (self._is_track(e.get("track"))
                                        and self.fav_key(e["track"]) == key)]
            reco_removed = reco_before != len(self.reco_buffer)
        src, ext = ((key.split(":", 2)[1], key.split(":", 2)[2])
                    if key.startswith("e:") else (None, None))
        self.db.set_dislike(None, src, ext, title, artist, key, True)
        self._clean_upcoming()
        if is_current:
            # 标记的恰是正在播的歌：next() 换歌。⚠ 不要提前 return —— 与
            # dislike_by_id current 分支对称，dj_speech/history/library 反馈照发。
            self.next()
        else:
            self._emit_playlist()
            if reco_removed:
                self._emit_reco()
                self._refill_reco()
        self._emit("dj_speech", f"👎 已标记不喜欢：《{title or '未知'}》")
        self._emit_history()
        self._emit_library()

    def _resolve_dislike_meta(self, key: str) -> dict:
        """R62 #23：按稳定 key 尝试解析出真实歌名/歌手（前端没传时的兜底）。

        先在内存各池（current/upcoming/reco_buffer/history）按 fav_key 找，
        找不到再查 db.dislike_rows()（重复标记时保留旧元数据）→ 都没有返回空。
        """
        if not key:
            return {}
        try:
            for t in self._iter_track_pool():
                if t is not None and self.fav_key(t) == key:
                    return {"title": getattr(t, "title", None),
                            "artist": getattr(t, "artist", None)}
            for r in self.db.dislike_rows():
                if r["key"] == key:
                    return {"title": r["title"], "artist": r["artist"]}
        except Exception:  # noqa
            pass
        return {}

    def _iter_track_pool(self):
        """遍历当前曲/待播/推荐货架/历史里的 Track（只读，供元数据兜底解析）。"""
        if self.current is not None:
            yield self.current
        with self._qlock:
            for e in list(self.upcoming):
                t = e.get("track")
                if t is not None:
                    yield t
            for e in list(self.reco_buffer):
                t = e.get("track")
                if t is not None:
                    yield t
        for t in list(self.history):
            yield t

    def favorite_by_id(self, track_id: int, on: bool) -> None:
        """♥ 收藏/取消：红心状态立即在内存更新（UI 立刻响应），
        重活（DB 写入 + 曲库 reload）放到后台线程，避免主线程卡顿
        （修复「点红心卡一下」）。任何持久化异常都不影响已在内存生效的红心状态。
        """
        # 1) 内存立即更新：保证 UI 红心瞬时反映（不依赖 DB 落地）
        track = self._resolve_track(track_id)
        key = self.fav_key(track) if track is not None else f"i:{track_id}"
        if on:
            self._queue_fav.add(key)
        else:
            self._queue_fav.discard(key)
        # 2) 立即刷新播放列表/推荐/历史红心（内存已更新；120ms 去抖合并，体感即时）
        self._emit_playlist()
        self._emit_reco()    # 同步推荐货架红心状态（推荐页 ♥ 立即可见）
        self._emit_history()  # 同步历史红心状态（历史页 ♥ 立即可见，修复卡红心）
        # 3) 重活后台化：入库（若需要）+ 写收藏 + 曲库 reload，完成后刷新曲库视图
        threading.Thread(target=self._fav_persist,
                         args=(track_id, on, key), daemon=True).start()

    def _fav_persist(self, track, on: bool, key: str) -> None:
        """后台：把收藏状态持久化（入库/写 favorites 表 + 曲库 reload），
        完成后回主线程刷新曲库视图。异常不影响内存已生效的红心状态。

        ⚠ R49-24：参数从 track_id(int) 改为**直接接收 Track 引用**。
          旧实现传 track.id 再 _resolve_track 重解析 —— 推荐/货架在线曲目是
          临时负 id，DB/内存都解析不到，导致 add_track 永不执行 = 红心不入库。
          这里持久化只依赖 Track 元数据 + cover；正负 id 都会被 add_track /
          set_favorite 归一化为稳定 db id。
        """
        try:
            if track is None:
                return
            if self._in_db(track):
                # 已在库：直接写收藏标记（on/off 都能正确切换）。
                # ⚠ 临时负 id 的在线曲目此刻可能因上次播放自动 upsert 已在库，
                #   _in_db 用负 id 查不到 → 走下方 add_track（幂等，安全）。
                row = self.db.get_track(track.id)
                if row is not None:
                    self.db.set_favorite(track.id, on)
                else:
                    self._fav_upsert_and_set(track, on)
            elif on:
                # 首次收藏：推荐/外部歌曲自动入库（我喜欢的音乐）；
                # add_track 内部会把 track.id 重分配成稳定 db id、写入 favorites、reload。
                # ⚠ 兼容：极端情况 add_track 因 id 冲突抛错时兜底 upsert。
                try:
                    self.library.add_track(track)
                except Exception:  # noqa
                    self._fav_upsert_and_set(track, on)
            else:
                # off：按**稳定键反向查库**删除收藏 —— 在线漫游曲此刻在库里的
                # 行是用「稳定占位 path + source/ext_id」存的（bridge / 起播时
                # upsert），track.id 是临时负 id 查不到 → 旧实现直接不落库 =
                # 红心取消了但歌永远留在曲库（R72 用户同款心智：红心=曲库开关）。
                row = self._db_row_by_track(track)
                if row is not None:
                    self.db.set_favorite(row["id"], False)
            self._emit_library()
        except Exception as e:  # noqa
            logger.warning("收藏持久化失败（红心内存状态已生效）: %s", e)

    def _db_row_by_track(self, track):
        """按 Track 的稳定标识（source+ext_id / path / id）反向查库中行。

        在线曲（漫游推荐/已播放自动落库）在库里的行用「占位 path + source+
        ext_id」标识，track.id 是临时负 id → 不能只靠 id 查。"""
        try:
            src = (getattr(track, "source", "") or "").strip()
            ext = getattr(track, "ext_id", None)
            path = getattr(track, "path", None) or ""
            if path:
                row = self.db.get_track_by_path(path)
                if row is not None:
                    return row
            if src not in ("local", "") and ext not in (None, ""):
                for r in self.db.all_tracks() or []:
                    if (r["source"] or "") == src and str(r["ext_id"] or "") == str(ext):
                        return r
            tid = getattr(track, "id", None)
            if tid is not None:
                return self.db.get_track(tid)
        except Exception:  # noqa
            return None
        return None

    def _fav_upsert_and_set(self, track, on: bool) -> None:
        """把 track 直接 upsert 入库（携带 cover，R49-24）并写收藏标记。

        ⚠ R72：兜底路径必须容忍**缺 path** 的轻量对象（历史上卡片 ♥ 传的
        SimpleNamespace 没有 path/title 等字段）—— 在线曲（source+ext_id）
        用稳定占位 path 合成（netease://<ext> / qq://<ext>，与 ai/playback.py
        起播落库同款，保证 ON CONFLICT(path) 幂等），否则 upsert 主键列
        为 NULL/空会让同歌反复建行且无法去重。"""
        src = (getattr(track, "source", "") or "").strip() or "local"
        ext = getattr(track, "ext_id", None)
        path = getattr(track, "path", None) or ""
        if not path and src not in ("local", "") and ext not in (None, ""):
            # ⚠ 协议名与音源层一致（qqmusic 音源 path 用 qq://，见 data/sources）
            proto = {"qqmusic": "qq"}.get(src, src)
            path = "%s://%s" % (proto, ext)
        tid = self.db.upsert_track(
            path, getattr(track, "title", "") or "",
            getattr(track, "artist", "") or "",
            getattr(track, "album", "") or "",
            getattr(track, "genre", "") or "",
            getattr(track, "year", None),
            getattr(track, "duration", 0.0) or 0.0,
            getattr(track, "bpm", None),
            source=src,
            ext_id=ext,
            cover=getattr(track, "cover", None),
            api_metadata=getattr(track, 'api_metadata', None),
        )
        self.db.set_favorite(tid, on)
        try:
            self.library.reload()
        except Exception:  # noqa
            pass

    def _in_db(self, track) -> bool:
        return self.db.get_track(track.id) is not None

    def remove_from_queue(self, track_id: int) -> None:
        """叉号「不播放」：从待播移除，仅跳过这一首，不做任何"不喜欢"标记。

        若点的是「当前正在播放」的歌（列表第 0 行），则切下一首跳过，
        避免叉号对正在播放的歌"点了没反应"的错觉。

        注意：叉号（✕）语义是「不播放这首」，与「不喜欢」（👎，进入隐藏的不喜欢库、
        对 AI 生效）是两回事。是否标记不喜欢由单独的 👎 按钮负责。
        """
        self._clean_upcoming()  # 防御：下方读取 e["track"].id
        if track_id == self.current_id:
            self.next()  # 跳过当前，交给标准下一首逻辑（含导航令牌守卫）
            return
        with self._qlock:
            before = len(self.upcoming)
            self.upcoming = [e for e in self.upcoming if e["track"].id != track_id]
            removed = before != len(self.upcoming)
        if removed:
            self._refill()
            self._emit_playlist()

    def play_next(self, track) -> None:
        """曲库右键"下一首播放"：插入到待播首位。

        注：UI 只持有 track_id（int），这里在边界处还原成 Track 再存储，
        避免把裸 id 当成 track 存进 upcoming。"""
        track = self._resolve_track(track)
        if track is None:
            return
        with self._qlock:
            entry = self._make_entry(track, "下一首播放")
            self.upcoming.insert(0, entry)
            # 插入首位后，原首位(now index 1)的转场是「从当前曲→原首位」，关系错乱；
            # 清掉其 _gen_t 让它用正确的 prev(新首位) 重新生成转场，否则转场文案串味。
            if len(self.upcoming) > 1:
                self.upcoming[1].pop("_gen_t", None)
        self._emit_playlist()
        # 关键修复：立即为「下一首播放」生成口播+转场并重新排程转场定时器，
        # 使其在前一首结束前就有足够时间就绪（否则切歌时口播来不及播、转场为空）。
        # _prep_next_broadcast 仅后台起线程生成、立即返回，不阻塞 UI；
        # 已生成的条目受 _gen/_gen_t 守卫跳过，仅新插入的首位真正(重新)生成。
        try:
            self._prep_next_broadcast()
        except Exception as e:  # noqa 绝不让「下一首播放」操作因口播生成失败而抛错
            logger.warning("play_next 预生成口播/转场失败（不影响插入）: %s", e)

    def add_to_recommend_list(self, track) -> None:
        """曲库右键"添加到推荐列表"：把指定曲目加入推荐列表（待播队列末尾）。

        这是曲库与推荐列表解耦的关键入口——曲库（我喜欢的音乐）是独立的收藏
        集合，不会自动灌入推荐列表；用户想让某首歌进推荐队列时，显式点这个。
        不改动曲库收藏、不触发自动漫游填充（维持用户手动编排的意图）。

        注：UI 只持有 track_id（int），这里在边界处还原成 Track 再存储。
        """
        track = self._resolve_track(track)
        if track is None:
            return
        self._clean_upcoming()  # 防御：下方读取 e["track"].id
        with self._qlock:
            if track.id == self.current_id:
                return
            if any(e["track"].id == track.id for e in self.upcoming):
                self._emit("dj_speech", f"《{track.title}》已在推荐列表中。")
                return
            self.upcoming.append(self._make_entry(track, "手动加入推荐列表"))
        self._emit("dj_speech", f"➕ 已加入推荐列表：《{track.title}》 - {track.artist or '未知'}")
        self._emit_playlist()

    def delete_from_library(self, track_id: int) -> None:
        """曲库右键"删除"：移出曲库（取消收藏）并从库删除。"""
        track = self._resolve_track(track_id)
        key = self.fav_key(track) if track is not None else f"i:{track_id}"
        self.db.set_favorite(track_id, False)
        self._queue_fav.discard(key)
        try:
            self.db.delete_track(track_id)
        except Exception:  # noqa
            pass
        self.library.reload()
        self._emit_library()
        self._emit_playlist()
        self._emit_history()   # 曲库删除后刷新历史红心状态

    def import_library_folder(self, folder: str) -> int:
        """R62 #9：导入本地文件夹到曲库（设置卡「导入文件夹」）。

        复用 MusicLibrary.scan 的音频发现/元数据提取/入库逻辑 —— 传一个
        用户指定目录（不必在 MUSIC_LIBRARY_ROOTS 内）。返回新增条数。
        ⚠ 仅当目录存在且是文件夹时扫描；其它情况抛 ValueError 由上层转错误。
        """
        import os
        from pathlib import Path
        if not folder or not str(folder).strip():
            raise ValueError("文件夹路径不能为空")
        p = Path(str(folder).strip())
        if not p.is_dir():
            raise ValueError(f"目录不存在：{p}")
        # MusicLibrary.scan 内部已做 reload + favorite_all_local（导入即红心点亮）
        n = self.library.scan([str(p)])
        self._emit_library()
        return int(n or 0)

    def scan_library_roots(self) -> int:
        """R62 #9：扫描配置的全部曲库根目录（MUSIC_LIBRARY_ROOTS）。返回新增条数。"""
        n = self.library.scan()
        self._emit_library()
        return int(n or 0)

    def remove_from_history(self, track_id: int) -> None:
        """播放历史的叉号：删除一条历史记录。"""
        with self._qlock:
            self.history = [t for t in self.history if t.id != track_id]
        self._emit_history()

    # ---------- 开关 / 设置 ----------

    def set_broadcast_language(self, language: str) -> None:
        """★ 2026-11-19：切换口播语言（zh/en）——给英文 UI 开关的唯一入口。

        一处切换三件事：① LLM 用中/英文 SYSTEM prompt；② TTS 用 zh/en 语种念；
        ③ 字数口径（英文按12秒词数）。默认 zh，行为与以前完全一致。
        切换后丢弃待播队列里已预生成的中文口播/转场，让下一轮按新语言重建；
        当前正在播的这首不打断（音频已经出声，不能中途换语言）。
        """
        lang = "en" if str(language or "zh").lower().startswith("en") else "zh"
        changed = (lang != getattr(self, "broadcast_language", "zh"))
        self.broadcast_language = lang
        # TTS 对象共享：设置 language 属性，worker 合成时读取并透传给引擎
        if getattr(self, "tts", None) is not None:
            self.tts.language = (None if lang == "zh" else "en")
        logger.info("[口播] 口播语言已切换为 %s（下一轮新生成生效）", lang)
        if changed:
            self._invalidate_queued_broadcasts()
        self._emit_playlist()

    def _invalidate_queued_broadcasts(self) -> None:
        """语言切换后重建待播口播（内部方法）。

        - 清掉 upcoming 里的中文 broadcast / `_gen` / `_gen_t` 幂等位；
        - 让在途串行链的世代 +1，旧步骤在下一步自行回滚；
        - 若仍有待播，立即按新语言重新预取。
        当前 current_broadcast 保持不动，避免正在播的音频/文案被劫持。
        """
        try:
            with self._qlock:
                for e in self.upcoming:
                    if not isinstance(e, dict):
                        continue
                    e["broadcast"] = None
                    e["_gen"] = False
                    e["_gen_t"] = False
            if getattr(self, "_chain_lock", None) is not None:
                with self._chain_lock:
                    self._chain_gen = int(getattr(self, "_chain_gen", 0)) + 1
                    self._chain_steps = None
            if self.upcoming:
                self._prep_next_broadcast()
        except Exception as e:  # noqa
            logger.warning("[口播] 语言切换后重建待播口播失败：%s", e)

    def set_dj_on(self, on: bool) -> None:
        self.dj_on = bool(on)
        if self.dj_on:
            self._prep_next_broadcast()

    def set_roam_on(self, on: bool) -> None:
        self.roam_on = bool(on)
        if self.roam_on:
            self._refill()
            self._emit_playlist()

    def set_familiarity(self, level: int) -> None:
        """设置推荐偏好（熟悉度级别 0~3），并持久化到用户配置。

        仅当值真正变化时才写盘，避免滑块拖动时反复触发 config.json 重写。
        该偏好是推荐系统的关键变量：会被注入 AI 推荐提示词（决定选歌的
        「熟悉↔探索」倾向）与口播上下文（DJ 解说会呼应这一偏好）。
        """
        v = max(0, min(3, int(level)))
        if v == self.familiarity:
            return
        self.familiarity = v
        try:
            user_config.data["rec_pref"] = v
            user_config.save()
        except Exception as e:  # noqa
            logger.warning("保存推荐偏好失败（仅本次会话生效）: %s", e)

    def set_queue_src(self, src: str | None) -> None:
        """★ 2026-09-19：标记当前队列的**来源**（曲库/歌单/推荐/漫游/点播）。"""
        self.queue_src = src

    def mode_locked(self) -> bool:
        """★ 2026-09-19：推荐/漫游下播放模式被锁（强制 order）。"""
        return self.queue_src in ("reco", "roam")

    def _shuffle_if_random(self, seq: list) -> list:
        """★ 2026-09-19：**填充顺序 = 播放顺序** —— random 时在**建队列那一刻**打乱。
        ⚠ 这就是 L990 注释里引用、但**从未实现**的 `_shuffle_upcoming_if_random`。
        为什么不能在 `_advance` 里随机：AI 转场口播按 `upcoming[0]` **预生成**
        （见 `_advance` 内的注释）—— 打乱必须发生在"建队列/填队列"时，
        这样"列表顺序 = 实际播放顺序"的口播契约才不破。
        推荐/漫游下 `_mode_effective()` 恒为 order ⇒ 这里是**空操作**（天然安全）。"""
        import random as _rnd
        out = list(seq)
        if self._mode_effective() == "random" and len(out) > 1:
            _rnd.shuffle(out)
        return out

    def _mode_effective(self) -> str:
        """★ 2026-09-19：本帧**实际生效**的播放模式。
        推荐/漫游强制 'order' —— 这两类队列的顺序就是 AI 的编排，且口播已按 upcoming[0]
        预生成；打乱会让旁白与实播失配。其余来源（曲库/歌单/点播）用用户的 play_mode。"""
        return "order" if self.mode_locked() else self.play_mode

    def set_play_mode(self, mode: str) -> None:
        if mode not in ("order", "random", "loop_one"):
            return
        if mode == self.play_mode:
            return
        self.play_mode = mode
        # R67-3：持久化（重启恢复 = 上一档位；之前是纯内存，重启回 order）
        try:
            user_config.data["playback_mode"] = mode
            user_config.save()
        except Exception as e:  # noqa
            logger.warning("保存播放模式失败（仅本次会话生效）: %s", e)

    # ---------- 对外快照（UI 初始化用） ----------

