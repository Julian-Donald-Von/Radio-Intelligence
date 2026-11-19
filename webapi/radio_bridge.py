"""webapi/radio_bridge.py —— 业务逻辑桥接层（Phase 2）

职责：
  1. 持有 AIRadioController 引用
  2. 把 controller 的 Qt 事件（r.on(...)）转发到 EventBus → WebSocket → 前端
  3. 给 FastAPI 路由提供"线程安全"的播放控制方法

⚠ 关键：Qt 线程编排
--------------------
FastAPI 的路由跑在 asyncio / 线程池里，而 AIRadioController 内部大量使用
QTimer / QObject / player，**Qt 对象只能在其所属线程（主线程）操作**，
跨线程直接调用会崩溃或行为异常。

因此所有对 radio 的调用都经 `qt_call()` 编排到 Qt 主线程执行
（队列 + QTimer 轮询 + Future 同步等待结果），与老架构里 QWebChannel 槽
天然跑在主线程的效果等价。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from config.settings import RadioConfig, user_config

logger = logging.getLogger("MusicAI.WebAPI.Bridge")


def _fld(obj, key, default=None):
    """统一取值：`sqlite3.Row` 只支持 `row[k]`，dict/对象支持 `.get`/属性。

    R99-T1 预热线程同时面对这两种形状（`db.all_tracks()` 给 Row、`radio.upcoming`
    给 Track/dict），手写 `hasattr(obj,'get')` 分支容易写错（踩过一次）。
    """
    if obj is None:
        return default
    try:
        g = getattr(obj, "get", None)
        if callable(g):
            v = g(key, default)
            return default if v is None else v
    except Exception:  # noqa
        pass
    try:
        v = obj[key]
        return default if v is None else v
    except Exception:  # noqa
        pass
    return getattr(obj, key, default)


class QtMarshaller:
    """把 callable 编排到 Qt 主线程执行（队列 + QTimer 轮询）。"""

    def __init__(self) -> None:
        self._jobs: "list[tuple[Callable, Future]]" = []
        self._lock = threading.Lock()
        self._timer = None
        self._qt_thread: threading.Thread | None = None   # 记录 Qt 主线程

    def start(self) -> None:
        """在 Qt 主线程调用一次，起 20ms 轮询定时器。

        ⚠ 必须在本方法被调用的线程上有 Qt 事件循环（即 Qt 主线程）。
        若在 uvicorn 线程里调用，QTimer 属于该线程且无事件循环 → timeout
        永不触发 → 每次 call() 都超时（日志表现为"失败:"且消息为空）。
        """
        self._qt_thread = threading.current_thread()
        try:
            from PySide6.QtCore import QTimer

            self._timer = QTimer()
            self._timer.setInterval(20)
            self._timer.timeout.connect(self._drain)
            self._timer.start()
            logger.info("Qt 编排器已启动（20ms 轮询，归属线程=%s）",
                        self._qt_thread.name)
        except Exception as e:  # noqa
            logger.warning("Qt 编排器启动失败，将退化为直接调用: %s: %s",
                           type(e).__name__, e)

    def _drain(self) -> None:
        with self._lock:
            jobs, self._jobs = self._jobs, []
        for fn, fut in jobs:
            try:
                fut.set_result(fn())
            except Exception as e:  # noqa
                fut.set_exception(e)

    def call(self, fn: Callable, timeout: float = 10.0) -> Any:
        """在 Qt 主线程执行 fn 并返回结果（同步等待）。

        三种情况：
          1. 无 Qt 环境（`_timer is None`）→ 直接调用（纯 API 测试可用）
          2. **已在 Qt 线程** → 直接执行。⚠ 若这里也排队等待，会阻塞 Qt 事件
             循环导致 `_drain()` 永不执行 → 死锁超时（实测踩过）。
          3. 非 Qt 线程（FastAPI 线程池）→ 入队，等 Qt 事件循环 drain 出结果。
        """
        if self._timer is None:
            return fn()
        if self._qt_thread is not None and threading.current_thread() is self._qt_thread:
            return fn()
        fut: Future = Future()
        with self._lock:
            self._jobs.append((fn, fut))
        return fut.result(timeout=timeout)

    def pending(self) -> int:
        """★ R119-5a：当前排队等 Qt 主线程执行的任务数。

        可观测面——这个数持续 >0 就说明 Qt 主线程被卡住没在 drain。
        `/api/status` 与看门狗据此就能分辨"接口慢"还是"Qt 卡了"。
        """
        try:
            with self._lock:
                return len(self._jobs)
        except Exception:  # noqa
            return 0


marshaller = QtMarshaller()

# ★ R119-5a：状态轮询的**短超时**（秒）。`/api/playback/state` 被进度条高频轮询，
#   若沿用 marshaller 默认的 10s，Qt 主线程一卡就会把线程池占满 ⇒ 全部接口饿死。
STATE_TIMEOUT_S = float((os.environ.get("MUSICAI_STATE_TIMEOUT_S") or "1.5"))


def _queue_locked(radio) -> bool:
    '''★ 2026-09-19：当前队列来源是否锁定播放模式（推荐/漫游 ⇒ 强制 order）。
    冗余防御：老版本控制器没有 mode_locked() 时返回 False（不锁），绝不因此报错。'''
    try:
        return bool(radio.mode_locked())
    except Exception:  # noqa
        return False


def _throttle_self_priority(level: int = -1) -> None:
    """★ 2026-09-18：把**当前线程**压到后台低优先级（Windows BELOW_NORMAL=-1 /
    LOWEST=-2）——让 PortAudio RT 回调优先占核，缓解"前 20 秒频繁卡顿"
    （重活 = CLAP 整曲分段索引 / beats，换曲后在后台线程跑）。

    ★ 2026-09-19 修复（**重大 · 静默空转**）：本函数原来是裸
    `ctypes.windll.kernel32.SetThreadPriority(GetCurrentThread(), level)`
    —— 在 64 位 Python 上伪句柄 -2 被按 32 位传参 ⇒ 非法句柄 ⇒ 调用失败、
    优先级不变。实测（`_r119_probe_prio2.py`）：修复前读回 0，修复后读回 -2。
    即：本函数自写下之日起**从未生效过**，分析线程一直在 NORMAL 与音频抢核。
    现改为**统一委托** `core.audio_engine._set_self_priority`（显式签名 + 读回校验），
    杜绝"同一件事两份实现、其中一份是坏的"。"""
    try:
        from core.audio_engine import _set_self_priority as _ssp
        _ssp(int(level))
    except Exception:  # noqa
        pass


class RadioBridge:
    """AIRadioController ↔ Web API 的桥。"""

    # R80 起播防护：换曲后 CLAP/分段索引静默期（秒）——起播稳定期不让步
    MOOD_TRACK_GRACE_S = 8.0
    # R95c-P0：**已索引曲目**短路起播保护窗。原 8s 窗的本意是"换曲瞬间 CLAP 权重
    #   加载/推理是磁盘 IO+GPU 风暴，起播稳定期让 RT 优先"（见下方 mood 循环注释）。
    #   但**已索引曲目**走的是 `SegmentIndexer.load_segments`（纯查库，零推理）⇒
    #   风暴根本不成立 → 8s 变成**纯等待**。R95c 量化（analyze_r95c_latency.py）：
    #   端到端最坏 10.0s 里它独占 8.0s，用户观感就是"切歌后水面 8 秒完全不动"。
    #   判据 = track_segments 是否已有该曲（一次 COUNT，走 track_id 索引，带 per-track 缓存）。
    MOOD_TRACK_GRACE_INDEXED_S = 1.5
    # R95c-P1：mood 推送节拍（秒）。原 2.0s → 1.0s。查询本身极轻（纯查库+投影，
    #   `axes != _mood_last` 才真推），换曲后的**首帧语义**因此提前最多 1s。
    MOOD_TICK_S = 1.0

    # R86 P0：在线曲 path 前缀。这些曲目的 path 是 URL/协议串，不能直接当文件
    # 索引；但播放器已把在线流落盘（%TEMP%/musicai_stream），可用落盘文件索引。
    REMOTE_PREFIX = ("http://", "https://", "netease://", "qq://",
                     "spotify://", "kugou://")

    # R86 P0：索引重试纪律——在线曲落盘是异步的（load() 下载期间 stream_local
    # 仍为 None），换曲瞬间取不到就永久放弃会让在线曲永远无语义。改为可重试：
    # 未索引时每隔 RETRY_S 再试，最多 RETRIES 次（幂等，失败静默）。
    INDEX_RETRY_S = 8.0
    INDEX_RETRIES = 3

    # R89：段索引的单 tick 时间预算（秒）。mood 循环每 2s 一 tick；原来一次
    # index_track 会把整曲（4 分钟 ≈ 38 段）跑完才返回，阻塞期间本线程既不出
    # L0 新段、也不刷 L1。改为每 tick 只跑一小片 + 播放头优先，**当前在听的段
    # 在第一个 tick 内就入库**，整曲也在 ~10s 内铺完。详见 SegmentIndexer。
    # ★ 2026-09-18·23:5x：1.0 → 0.6。真机反馈"前 20s 对某一秒反复卡几次" =
    #   段索引 1s 满预算 burst 连续跑把 RT 回调饿死。降到 0.6 让每片更短。
    # ★ R119-2w：切片预算 0.6 → **0.25**（整表重建 1.0 → 0.4，见 SEG_FULL_BUDGET_S）。
    #   依据：`clap_index` 单次实测 **1.38 秒**（本进程内、唯一残留的每曲一次停顿）。
    #   `index_track` 的预算是"跑完当前段再收尾"⇒ 单段推理就是下限；收小预算能把
    #   "多段累积"那一部分削掉。**注意**：若单段推理本身就 ~0.8s，本项只能减轻、
    #   不能根治 —— 根治需常驻子进程 worker（CLAP 权重加载一次）。
    SEG_BUDGET_S = 0.25
    # ★ 2026-09-19（R119-2i）：**整表重建也必须有预算**。
    #   原先调用方写 `budget_s=0.0 if full else SEG_BUDGET_S`，而 `budget_s=0`
    #   在 `SegmentIndexer.index_track` 里表示**无限预算**（跑完为止）⇒ 真机实测
    #   单次 **4.5~6.6 秒**的整进程阻塞（看门狗 `stall_max=1.06s`、回调间隔
    #   `cb_gap_max=1110ms` 同时到位 = 整个 Python 进程没跑），正是用户说的
    #   "开头卡几下 / 歌中间卡一下"。⇒ 给整表重建一个 1.0s 薄片预算，靠既有的
    #   `SEG_GAP_S=2.0` 让路逐 tick 消化（budget>0 时 index_track 会吸收库里
    #   已有段并可续跑，所以拆片安全、不会重复嵌入）。
    SEG_FULL_BUDGET_S = 0.4
    # ★ 2026-09-18·23:5x：片间**让路**（秒）。原 seg_next=0（有进展立刻续）⇒ 连续 burst；
    #   加 gap 后每片之间给音频回调一段独占期（同 WARM_GAP_S 的既有手法）。
    SEG_GAP_S = 2.0

    # R99-T1：段索引**预热**（覆盖率补量：库里 291 首原本只有"播过的"才被索引）。
    #   纪律（三条，缺一不可）：
    #     ① **低优先级**：mood 正在为当前曲索引时（`_seg_busy`）立刻让路 —— 播放头
    #        先行是 R89 的硬约束，预热永远不许把"换曲后 1.5s 内出语义"推后；
    #     ② **互斥**：CLAP 是**全进程单例**（显存纪律）⇒ 预热与 mood 共用
    #        `_seg_lock`，绝不并发推理；
    #     ③ **切片**：单次只跑 `WARM_BUDGET_S`（跑完当前段再收尾），片间
    #        `WARM_GAP_S` 让出 CPU/磁盘给音频回调（≈23ms 周期，饿死就爆音）。
    WARM_BUDGET_S = 0.4
    WARM_GAP_S = 2.0
    WARM_TICK_S = 2.0
    # 同一曲连续 N 次"零新增"就判定"索引源已无更多内容"（元数据时长 > 真实音频
    # 时长时尾段压根不存在 ⇒ 永远"不完整"）。没有这个闸门，预热会在这种曲目上
    # **活锁**（每轮重嵌入一次，白烧 CPU/GPU）。
    WARM_MAX_TRIES = 3

    # ═══ R99-T1c：**预下载**（真·全量段索引覆盖的唯一路径）═══
    # 现状（2026-09-11 取证）：曲库 291 首 = local 4 + netease 216 + qqmusic 71，
    # 而段索引只覆盖 **26 首**；原因是段索引只能索引**能拿到音频文件**的曲目，而
    # 未播放过的在线曲没有落盘流 ⇒ 无论 T1a/T1b 怎么做都补不上。唯一出路是**主动
    # 把音频取回来**（≈5MB/首 ⇒ 全库 1~2GB）。这是一条**独立决策**（带宽+磁盘），
    # 故本机制：① 可一键关（`PRE_DL_ON` / `POST /api/index/predl?on=0`）；
    # ② 有**字节预算**（`PRE_DL_MAX_MB`，用满即停，不无限制吃磁盘）；
    # ③ 最低优先级（播放头索引 > 预热 > 预下载），且**换曲后 3s 内不动手**
    #    （不与当前曲的下载抢带宽 —— 那会直接影响起播延迟）。
    PRE_DL_ON = True
    PRE_DL_MAX_MB = 2048.0     # 单次进程会话的下载预算（MB）；全库 ≈1.5GB ⇒ 够用
    PRE_DL_GAP_S = 3.0         # 相邻两次下载的间隔（让路给播放与索引）
    PRE_DL_TICK_S = 5.0        # 无候选时的轮询间隔
    PRE_DL_SETTLE_S = 3.0      # 换曲后多久才允许预下载（避开当前曲的下载窗口）

    # ═══ R99-T7e：**beats 预热**（EVENTS/瞬时层的数据覆盖率）═══
    # 实测（2026-09-11）：曲库 293 首里 `track_segments` 47 曲（16%）而
    # **`track_beats` 只有 31 曲（10.6%）** ⇒ 用户"瞬间情绪变化（音高/力度/密度）
    # 看不出效果"的**主因是没数据**：`track_beats` 是**另一条索引通道**
    # （`core/beat_events.index_track`），T1b/T1c 只预热了段索引 ⇒ 95% 的歌瞬时层全静默。
    # ⚠ beats 是**整曲阻塞**调用（librosa 整首 + ONNX + 逐块 pyin，十几~几十秒），
    #   与段索引（CLAP/GPU）不同资源 ⇒ 不共用 `_seg_lock`（否则会把
    #   "换曲后 1.5s 出语义"堵几十秒）。改为：
    #     ① `_beat_lock` 只串行 beats-vs-beats（mood 与预热）；
    #     ② `_beatw_busy` 时 mood 的 beats 请求**等待但不消耗重试次数**（当前曲永远优先）；
    #     ③ 换曲静默窗（复用 `PRE_DL_SETTLE_S`）内不做重活。
    BEATW_ON = True
    BEATW_GAP_S = 5.0          # 相邻两首之间的让步（CPU/磁盘给音频回调）
    BEATW_TICK_S = 5.0         # 无候选时的轮询间隔
    BEATW_MAX_TRIES = 2        # 连续 N 次拿不到 beats ⇒ 判死（防活锁）
    # 运行期开关（真机 A/B / `POST /api/index/predl?on=0`）。**必须是类属性**：
    # ① `server.py` 会 `bridge.predl_on = …`，而 E65 按 `hasattr(RadioBridge, X)`
    #    核对 server.py 的每个引用 ⇒ 只写在 `__init__` 里会被判"方法不存在"；
    # ② 赋值 `self.predl_on = False` 生成实例属性，不影响类默认值（无跨实例串扰）。
    # ---- R119-3f：Ollama runner 内存回收（**不牺牲口播速度**的那种）----
    #   真机事实（`docs/r119_stall_root_causes.md` ②）：`llama-server` 常驻后私有提交
    #   涨到 **~13.8G**（模型本体只占**显存 2.9G**）；而**卸载+重载只要 5.4s** 就能回收
    #   **~8G**，且**模型保持热**（下次口播不用等加载）⇒ 这笔交易 = "5.4s 空闲重活"换"8G 内存"。
    #   判据本体在 `ai/model.py::should_recycle_runner`（纯函数 ⇒ 可单测、可"见红"）。
    RUNNER_RECYCLE_ON = True
    RYC_TICK_S = 30.0           # 每 30s 看一次（判据极便宜）
    RYC_NEED_IDLE_TICKS = 2     # 需连续 2 拍（≥60s）非播放 ⇒ 避开换曲过渡窗
    RYC_COMMIT_FLOOR_GB = 6.0   # 提交可用低于此值才动手（宽裕时不折腾）
    RYC_COOLDOWN_S = 1800.0     # 两次回收最短间隔 30 分钟（防抖动）
    ryc_on = RUNNER_RECYCLE_ON  # 运行期开关（类属性：诊断/接口可引用）
    # ---- R119-3g：MTG 手工特征索引的「等空闲」闸门 ----
    #   真机栈证据：`_g3` 的 `feat_index_track`（librosa 颤音扫描 / stft+hpss）是
    #   **每曲一次的 ~1 秒 GIL 重活**，此前**不看播放状态** ⇒ 每次起播附近冻 ~1s。
    #   与 beats 同一处置：协作式让路、有界等待、可回退。
    #   回退：`MUSICAI_FEAT_IDLE_ONLY=0`；等空闲上限：`MUSICAI_FEAT_IDLE_WAIT_S`。
    FEAT_IDLE_ONLY = True
    FEAT_IDLE_WAIT_S = 300.0
    predl_on = PRE_DL_ON

    def __init__(self) -> None:
        self.radio = None
        self._wired = False
        self._spec_stop = threading.Event()
        self._spec_thread: threading.Thread | None = None
        # P4 语义层（docs/water_research_emotional.md §15.3/§15.4）：
        # track_changed/progress 的轻量缓存（mood 线程读，绝不上 Qt 线程）
        self._mood_stop = threading.Event()
        self._mood_thread: threading.Thread | None = None
        self._mood_track: dict | None = None     # {id,path,duration,source}
        self._mood_track_at = 0.0                # 换曲时刻（起播防护用）
        self._mood_pos = 0                       # 最近 progress ms
        self._mood_last: dict | None = None      # 上次推送的轴值（变化才推）
        # ★ 2026-11-18：语义链路自检（治"语义缺席但无从判断"）
        #   _mood_diag：mood 循环每 tick 记录"停在哪道门"，供 /api/semantics/now 读取
        #   _mood_log_at：节流日志时间戳（每类 30s 一条，绝不刷屏）
        self._mood_diag: dict = {"gate": "init"}
        self._mood_log_at: dict[str, float] = {}
        self._mood_src: str = ""
        self._mood_axes_n: int = 0
        # R95c-P0：per-track「起播保护窗」缓存（已索引 → 1.5s / 未索引 → 8.0s）。
        #   避免每 tick 都查一次 track_segments；换曲时按 tid 懒算一次。
        self._mood_grace_cache: dict[int, float] = {}
        self._beats_cache: dict | None = None    # R84a 最近一次下发的 beats（/api/beats 断线重取）
        # R0-C：已索引曲目 → 实际用过的音频文件（在线曲=播放器落盘流）。
        # 没有它，fit_mapping 只能看见本地曲（在线曲 path 是 qq:// 占位符），
        # 在线听歌场景 YAMNet→CLAP 映射永远拟合不出来。
        self._index_src: dict[int, str] = {}
        # 路线 D：DEAM 本地 VA 头缓存（每曲一次离线推理 → 覆盖 CLAP 锚点派生 VA）。
        # _va_cache: track_id -> (valence01, arousal01)；_va_pending 防重入；
        # _va_failed 记推理真实失败（路径暂不可得不入 failed，下次播放可重试）。
        self._va_cache: dict[int, tuple[float, float]] = {}
        self._va_pending: set[int] = set()
        self._va_failed: set[int] = set()
        # R99-T1：段索引预热（覆盖率补量）+ 与 mood 路径互斥（CLAP 全进程单例）
        self._warm_stop = threading.Event()
        self._warm_thread: threading.Thread | None = None
        self._seg_lock = threading.Lock()   # 段推理互斥：mood / 预热 共用同一 extractor
        self._seg_busy = False              # mood 正在为**当前曲**索引 → 预热让路
        self._warm_cursor = 0               # 全库轮转游标（避免总从同几首开始）
        self._warm_tries: dict[int, int] = {}   # tid → 连续"零新增"次数
        self._warm_gave_up: set[int] = set()    # 判定"索引源无更多内容"的 tid
        self._warm_stat = {"done": 0, "failed": 0, "gave_up": 0, "last": ""}
        # R99-T1c：预下载（最低优先级；预算见 PRE_DL_MAX_MB）
        self.predl_on = bool(self.PRE_DL_ON)      # 运行时可关（类属性默认见上）
        self._predl_stop = threading.Event()
        self._predl_thread: threading.Thread | None = None
        self._predl_bytes = 0.0
        self._predl_cursor = 0
        self._predl_stat = {"done": 0, "failed": 0, "skipped": 0, "last": ""}
        # R99-T7e：beats 预热（EVENTS 层覆盖率）
        self.beatw_on = bool(self.BEATW_ON)
        self._beatw_stop = threading.Event()
        self._beatw_thread: threading.Thread | None = None
        self._beat_lock = threading.Lock()   # beats 互斥（mood 与预热共用）
        self._beatw_busy = False             # 预热正在整曲索引 → mood 等待但不烧重试
        self._beatw_cursor = 0
        self._beatw_tries: dict[int, int] = {}
        self._beatw_gave_up: set[int] = set()
        self._beatw_stat = {"done": 0, "failed": 0, "gave_up": 0, "last": ""}
        # R119-3f：Ollama runner 回收（空闲期把膨胀的 runner 卸载+重载；模型保持热）
        self.ryc_on = bool(self.RUNNER_RECYCLE_ON)
        self._runner_stop = threading.Event()
        self._runner_thread: threading.Thread | None = None
        self._runner_idle_ticks = 0     # 连续"非播放"节拍数
        self._runner_last = 0.0         # 上次回收的 monotonic（0 = 从未）
        self._runner_stat = {"n": 0, "skipped": "", "last_seconds": 0.0,
                             "commit_before_gb": -1.0, "commit_after_gb": -1.0,
                             "last_result": ""}

    # ---------------- 生命周期 ----------------

    def attach(self, radio) -> bool:
        """接入 controller 并转发事件到前端。"""
        self.radio = radio
        if radio is None:
            return False
        try:
            self._wire_events()
            self._wired = True
            logger.info("RadioBridge 已接入 controller")
            # ★ R119-3j：把「是否在播放」注册成**全局唯一判据**（`core/playback_state`），
            #   供 `ai/` 层的进程内重活让路（漫游索引重建等，见 `_playing_now`）。
            #   注册失败只是少了"让路"能力，不该拖垮装配 ⇒ 只告警。
            try:
                from core import playback_state as _pbs
                from core.audio_engine import note_gate as _ng
                _pbs.set_provider(self._playing_now)
                _pbs.set_note(_ng)
            except Exception as _e:  # noqa
                logger.warning("playback_state 注册失败（重活让路能力缺失）: %s", _e)
            # ⚠⚠ 关键（R0 事故 2026-09-10 实测定案）：控制器是在**后台线程**
            #   （start_api_host 的 "radio-init"）装配的，其内部
            #   _QtMainThread(QObject) 的线程亲和性落在那个工作线程上 → 后台
            #   _emit 的信号被排进一个**没有事件循环**的线程队列 → 事件永远到不了
            #   本类注册的回调（track_changed / progress / playing /
            #   playlist_updated / reco_updated / dj_speech / lyrics 全丢）。
            #   症状：mood 线程拿不到 _mood_track（恒 None）→ 语义层第一道门即
            #   continue → CLAP 段索引 / beats / L1 全不跑（水面只剩 30Hz 原始音频
            #   响应）；前端因有 REST 轮询（poll 2s / loadCards 5s）兜底，看不出异常。
            #   修法与老栈 ui/bridge_host.attach_radio 同构：在本方法（必须由 Qt
            #   主线程调用，见 start_api_host._attach_radio_async._poll）重建事件桥。
            #   ⚠ 无 QApplication 时必须保持 _qt_main=None（同步 _dispatch 可用）——
            #   否则会从"同步可用"退化成"排进无事件循环的队列 = 静默丢弃"。
            try:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is None:
                    logger.info("无 QApplication：保持同步事件分发（不重建事件桥）")
                else:
                    from ai.radio import _QtMainThread
                    radio._qt_main = _QtMainThread()
                    radio._qt_main.bind(radio._dispatch)
                    logger.info("电台事件桥已重建（绑定到 Qt 主线程）")
            except Exception as e:  # noqa
                logger.warning("重建电台事件桥失败（事件可能延迟）: %s", e)
            self._start_spectrum_pusher()
            self._start_mood_pusher()
            # R67-1：QQ 音源默认启用 + 启动时自动拉起 qq-music-api。
            # 之前 sources.qqmusic.auto_start 字段（default=True）只是配置占位，
            # 实际从未被代码读取，扫码登录经常报 WinError 10061。这里补上：
            # enabled + auto_start + 本机 api_base → 先探 3200 端口，未占则 spawn bat。
            try:
                self._try_launch_qq_music_api()
            except Exception as _qqe:  # noqa
                logger.warning("QQ music-api 启动尝试异常: %s: %s",
                               type(_qqe).__name__, _qqe)
            # ★ 2026-11-16：启动一次性拉起不够 —— 3200 中途挂掉（或由临时会话
            # 拉起后随会话回收）就再也没人管，歌词/取链一直 WinError 10061。
            # 加独立守护线程周期探活并重拉（带冷却，避免风暴）。见 _start_qq_api_guard。
            try:
                self._start_qq_api_guard()
            except Exception as _ge:  # noqa
                logger.warning("QQ music-api 守护线程启动失败: %s: %s",
                               type(_ge).__name__, _ge)
            # R68：EQ 持久化配置 → 应用到新播放器（启动恢复）。
            try:
                self._apply_eq_on_attach()
            except Exception as _eqe:  # noqa
                logger.debug("EQ 启动恢复跳过: %s: %s", type(_eqe).__name__, _eqe)
            return True
        except Exception as e:  # noqa
            logger.warning("RadioBridge 接入失败: %s: %s", type(e).__name__, e)
            return False

    # ---------------- R67-1：QQ music-api 自动拉起 ----------------
    def _try_launch_qq_music_api(self) -> None:
        """QQ 音源 enabled + auto_start + 本机 api_base → 后台拉起 qq-music-api。

        行为：
          1. 读 `sources.qqmusic.{enabled, auto_start, api_base}`；
             auto_start 字段保留（默认 True；环境变量 MUSICAI_QQ_AUTOSTART=false 可关），
             UI 不再展示开关。
          2. 探 127.0.0.1:3200（用 api_base 的 host/port；非本机地址直接跳过）→
             已开服务则直接 return。
          3. 没起 → 找 `tools/qq-music-api/start-qq-api.bat`，subprocess.Popen
             DETACHED_PROCESS | CREATE_NO_WINDOW。失败仅记日志，不抛。
        """
        try:
            qq = (user_config.data.get("sources", {}) or {}).get("qqmusic", {}) or {}
        except Exception:  # noqa
            qq = {}
        if not qq.get("enabled"):
            return
        # env-var 紧急关闭
        if str(os.environ.get("MUSICAI_QQ_AUTOSTART", "")).lower() in ("0", "false", "no"):
            logger.info("QQ music-api 自动拉起已通过 MUSICAI_QQ_AUTOSTART=false 关闭")
            return
        if not qq.get("auto_start", True):
            return
        api_base = (qq.get("api_base") or "").strip()
        if not api_base:
            return
        # 只对 127.0.0.1 / localhost 生效（远程地址不会"自动拉起"）
        from urllib.parse import urlparse
        u = urlparse(api_base)
        host = (u.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return
        port = u.port or 3200
        # 1) 探活（短超时）
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0) as _:
                logger.info("QQ music-api 已在 %s:%d 运行，跳过自动拉起", host, port)
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        # 2) 找 bat
        here = Path(__file__).resolve().parent.parent
        bat = here / "tools" / "qq-music-api" / "start-qq-api.bat"
        if not bat.exists():
            logger.info("QQ music-api 启动脚本未找到（%s），跳过自动拉起", bat)
            return
        try:
            # ★ 2026-11-16：**显式净化环境**——QQ API 是 Node 进程，axios 会读
            # HTTP_PROXY/HTTPS_PROXY。若不净化，本服务继承 geph 9910 死代理 ⇒
            # axios 全部 ProxyError ⇒ 接口 500 ⇒ 直链全失效（多音源失效根因之一）。
            _env = {k: v for k, v in os.environ.items()
                    if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
            # ⚠ stdout/stderr 用真实日志文件，不用 DEVNULL：实测 DEVNULL + DETACHED
            #   下 Node/tsx 启动不稳定，且出错时无从排查。日志落 data_runtime/。
            try:
                _logdir = Path(__file__).resolve().parent.parent / "data_runtime"
                _logdir.mkdir(parents=True, exist_ok=True)
                _qqlog = open(_logdir / "qqapi_stdout.log", "ab", buffering=0)
                _out, _err = _qqlog, subprocess.STDOUT
            except Exception:  # noqa 日志开不了也要能起服务
                _out, _err = subprocess.DEVNULL, subprocess.DEVNULL
            kwargs = {"cwd": str(bat.parent), "stdin": subprocess.DEVNULL,
                      "stdout": _out, "stderr": _err,
                      "close_fds": True, "env": _env}
            if sys.platform == "win32":
                DETACHED_PROCESS = 0x00000008
                CREATE_NO_WINDOW = 0x08000000
                kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NO_WINDOW
            # ★★ 2026-11-16：不再走 bat —— 它有两处致命问题：
            #   ① 末尾 `pause`：stdin=DEVNULL 时立即 EOF 返回，cmd 退出会带走子进程；
            #   ② 内部直接写 `node`，而 node 只在 WorkBuddy 会话的 PATH 里，
            #      脱离会话后 cmd 找不到 node ⇒ 服务起不来（实测 3200 反复挂）。
            # 改为**直接启动 node.exe 绝对路径 + tsx cli.mjs + src/app.ts**，
            # 不经 cmd/bat、不依赖 PATH、不受 stdin 影响 ⇒ DETACHED 后能稳定常驻。
            node_exe = shutil.which("node") or ""
            if not node_exe:
                # 会话外（服务由桌面/任务计划启动）PATH 常不含 WorkBuddy 的 node，
                # 扫描其版本目录兜底，避免写死单一版本号。
                import glob
                _roots = os.path.join(os.path.expanduser("~"),
                                      ".workbuddy", "binaries", "node", "versions", "*")
                for _d in sorted(glob.glob(_roots), reverse=True):
                    _c = os.path.join(_d, "node.exe")
                    if os.path.isfile(_c):
                        node_exe = _c
                        break
            if not node_exe:
                logger.warning("未找到 node.exe，QQ music-api 无法拉起")
                return
            cli = bat.parent / "node_modules" / "tsx" / "dist" / "cli.mjs"
            app = bat.parent / "src" / "app.ts"
            if not cli.exists() or not app.exists():
                logger.warning("QQ music-api 依赖缺失（cli=%s app=%s）", cli, app)
                return
            cmd = [node_exe, str(cli), str(app)]
            subprocess.Popen(cmd, **kwargs)
            logger.info("QQ music-api 已自动拉起（node=%s, DETACHED）", node_exe)
        except Exception as e:  # noqa
            logger.warning("QQ music-api 自动拉起失败: %s: %s", type(e).__name__, e)

    def _qq_api_alive(self, port: int = 3200) -> bool:
        """轻量探活：只做 TCP connect，不依赖 HTTP 业务接口是否正常。"""
        import socket
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.6):
                return True
        except OSError:
            return False

    def _start_qq_api_guard(self) -> None:
        """★ 2026-11-16：3200 端口守护 —— 挂了自动重拉（治歌词/取链 10061）。

        为什么需要：`_try_launch_qq_music_api()` 只在 attach 时调一次；3200 若中途
        退出（或由临时会话以非 DETACHED 方式拉起后随会话回收），后续所有 QQ 请求
        都会 `WinError 10061 积极拒绝`，且**不会自愈**。
        纪律：① 只在音源 enabled + 本机 api_base 时守；② 探活只用 TCP（0.6s）；
        ③ 重拉间隔 ≥20s 冷却，避免进程反复起不来的风暴；④ 守护线程 daemon，
            失败静默，绝不影响播放/语义层。
        """
        try:
            qq = (user_config.data.get("sources", {}) or {}).get("qqmusic", {}) or {}
        except Exception:  # noqa
            qq = {}
        if not qq.get("enabled"):
            return
        if str(os.environ.get("MUSICAI_QQ_AUTOSTART", "")).lower() in ("0", "false", "no"):
            return
        from urllib.parse import urlparse
        try:
            u = urlparse((qq.get("api_base") or "").strip())
        except Exception:  # noqa
            return
        if (u.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            return
        port = u.port or 3200

        guard = getattr(self, "_qq_api_guard_started", False)
        if guard:
            return
        self._qq_api_guard_started = True

        def _loop():
            last_try = 0.0
            while True:
                try:
                    time.sleep(10.0)
                    if self._qq_api_alive(port):
                        continue
                    now = time.monotonic()
                    if now - last_try < 20.0:
                        continue
                    last_try = now
                    logger.warning("[QQ服务] 3200 不可达 → 自动重拉 qq-music-api")
                    self._try_launch_qq_music_api()
                except Exception as e:  # noqa 守护线程绝不因异常退出
                    logger.debug("[QQ服务] 守护线程异常（忽略）: %s", e)

        threading.Thread(target=_loop, daemon=True, name="qq-api-guard").start()
        logger.info("[QQ服务] 3200 守护线程已启动（10s 探活 / 20s 重拉冷却）")

    def detach(self) -> None:
        """断开 controller（频谱/语义推送线程一并停止）。"""
        self._spec_stop.set()
        if self._spec_thread is not None:
            try:
                self._spec_thread.join(timeout=1.0)
            except Exception:  # noqa
                pass
            self._spec_thread = None
        self._mood_stop.set()
        if self._mood_thread is not None:
            try:
                self._mood_thread.join(timeout=1.0)
            except Exception:  # noqa
                pass
            self._mood_thread = None
        # R99-T1：预热线程（可能在 WARM_BUDGET_S 的一片里，join 稍宽）
        self._warm_stop.set()
        if self._warm_thread is not None:
            try:
                self._warm_thread.join(timeout=2.0)
            except Exception:  # noqa
                pass
            self._warm_thread = None
        # R99-T1c：预下载线程（可能卡在一次网络请求上 ⇒ join 更宽；它是 daemon，
        #   且 `_download_stream` 自带 (5,30)s 超时，不会无限期挂住）
        self._predl_stop.set()
        if self._predl_thread is not None:
            try:
                self._predl_thread.join(timeout=3.0)
            except Exception:  # noqa
                pass
            self._predl_thread = None
        # R99-T7e：beats 预热（整曲阻塞调用 ⇒ join 给足；daemon 且自带超时兜底）
        self._beatw_stop.set()
        if self._beatw_thread is not None:
            try:
                self._beatw_thread.join(timeout=5.0)
            except Exception:  # noqa
                pass
            self._beatw_thread = None
        # R119-3f：runner 回收（daemon；一拍里可能正在等 Ollama 重载 ⇒ join 给足）
        self._runner_stop.set()
        if self._runner_thread is not None:
            try:
                self._runner_thread.join(timeout=10.0)
            except Exception:  # noqa
                pass
            self._runner_thread = None
        self.radio = None
        self._wired = False

    # ---------------- 频谱 + P2 特征帧推送（音频响应壁纸/水面节拍） ----------------
    # 守护线程：~30Hz 节拍。每拍读 engine.read_feature_frame() → bus.emit('feat', …)
    # （seq 去重；实际帧率=特征线程 30Hz）；每 3 拍发一次 read_spectrum() →
    # bus.emit('spectrum', …)（R68 频谱通道保持 10Hz 节奏不变）。
    # ⚠ 只在存在 WS 客户端时把 engine 频谱/特征打开并推送（无订阅零开销）。
    # ⚠ read_spectrum/read_feature_frame 只读引擎内带锁快照，不触碰任何 Qt 对象
    #   → 线程安全。
    def _start_spectrum_pusher(self) -> None:
        if self._spec_thread is not None:
            return
        self._spec_stop.clear()

        def _loop():
            from webapi.bus import bus

            last_on = False
            last_seq = -1
            spec_tick = 0
            while not self._spec_stop.wait(0.033):   # ~30Hz（P2 特征帧节拍）
                try:
                    p = getattr(self.radio, "player", None)
                    has_read = p is not None and hasattr(p, "read_spectrum")
                    if not has_read:
                        last_on = False
                        continue
                    on = bus.client_count > 0
                    if on != last_on:
                        try:
                            p.set_spectrum_enabled(on)
                        except Exception:  # noqa
                            pass
                        last_on = on
                    if not on:
                        continue
                    # P2 特征帧（30Hz）：seq 去重后逐帧推送（水面/律动节拍源）
                    read_feat = getattr(p, "read_feature_frame", None)
                    if callable(read_feat):
                        f = read_feat()
                        if f and f.get("seq") != last_seq:
                            last_seq = f.get("seq")
                            bus.emit("feat", f)
                    # 频谱（10Hz，R68 通道节奏不变）：每 3 拍发一次
                    spec_tick += 1
                    if spec_tick >= 3:
                        spec_tick = 0
                        data = p.read_spectrum()
                        if data:
                            bus.emit("spectrum", data)
                except Exception:  # noqa
                    pass

        self._spec_thread = threading.Thread(
            target=_loop, daemon=True, name="spec-push")
        self._spec_thread.start()
        logger.info("频谱/特征推送线程已启动（feat 30Hz + spectrum 10Hz，按 WS 客户端数开关）")

    # ---------------- P4 语义层推送（水面情绪慢变量，§15.3/§15.4） ----------------
    # 守护线程 2s 节拍：position → 段 → 查 track_segments 表（离线分段索引，
    # 常态零推理）→ 14 维语义轴投影 → 段变化时 bus.emit('mood', …)。
    # ⚠ 首播本地曲目时在本线程内做一次性分段索引（6~18s 后台，期间不发 mood，
    #   语义晚到可接受；天然串行防并发 CLAP 推理——显存纪律）。
    # ⚠ 只读 bridge 自己缓存的事件快照（_mood_track/_mood_pos），不触碰 Qt 对象。
    def _local_index_path(self, tr: dict, r) -> str | None:
        """R86 P0：返回可用于离线索引的本地文件路径；不可得 → None。

        本地曲：直接用 path（存在即可）。
        在线曲：用播放器落盘的临时流文件。⚠ 不能拿 URL 相等来配对——引擎里
        current_path 是**解析后的直链**，而 track.path 是 netease:// 之类占位符，
        两者永不相等。安全性改由「同一曲」双重确认保证：
          ① 传入快照 tr 仍是 mood 缓存里的同一个对象（中途未换曲，dict 每次
             track_changed 新建 → 天然版本号）；
          ② 引擎当前播放的是远程流（current_path 以 http(s) 开头）。
        ① 放在最后判定：若读取期间发生换曲 → 身份不符 → 直接放弃，
        宁可不索引也不把别曲音频写进本曲 tid（污染语义表比缺语义更糟）。
        下载未完成时 stream_local 为 None，自然返回 None，下一轮重试即可。
        """
        try:
            path = (tr or {}).get("path") or ""
        except Exception:  # noqa
            return None
        if not path:
            return None
        if not path.startswith(self.REMOTE_PREFIX):
            try:
                return path if Path(path).exists() else None
            except Exception:  # noqa
                return None
        try:
            player = getattr(r, "player", None)
            if player is None:
                return None
            cp = str(getattr(player, "current_path", None) or "")
            sp = getattr(player, "stream_local_path", None)
            ok_remote = cp.startswith(("http://", "https://"))
            ok_same = self._mood_track is tr      # ⚠ 必须最后判定（见 docstring）
            if ok_remote and ok_same and sp and Path(sp).exists():
                return sp
        except Exception:  # noqa
            pass
        return None

    def _resolve_index_src(self, track_id, raw_path) -> str | None:
        """R0-C：为「已被段索引过的曲目」找回可读音频文件（供 fit_mapping 配对）。

        在线曲的 `tracks.path` 是 `qq://` / `netease://` 占位符，`Path.exists()`
        恒假 → fit_mapping 此前**完全看不见在线曲**（用户主要听在线歌时，L1 永远
        不就绪）。这里优先返回本会话索引时记下的落盘流路径（`_index_src`），
        其次才是真实存在的本地路径。纯读字典 + exists 判定，供 mood 线程安全调用。
        """
        try:
            p = self._index_src.get(int(track_id))
        except Exception:  # noqa
            p = None
        try:
            if p and Path(p).exists():
                return p
        except Exception:  # noqa
            pass
        try:
            if raw_path:
                rp = str(raw_path)
                if not rp.startswith(self.REMOTE_PREFIX) and Path(rp).exists():
                    return rp
        except Exception:  # noqa
            pass
        return None

    def _beats_idle_only(self) -> bool:
        """★ 2026-09-19（R119-2j）：**当前是否正在播放**（True ⇒ 应跳过整曲 beats 索引）。

        为什么按"播放中"就跳过：`core.beat_events.index_track` 单次实测 **15~20.6 秒**
        （整曲 `librosa.load` + 逐拍特征 Python 循环），**没有预算参数、不可切分**，
        而它跑在 mood/预热线程里 ⇒ 期间整个 Python 进程**每 ~1 秒被冻一次**
        （看门狗 `stall_max≈0.94s`；stall 在起播后 14.5~20.6s 密集出现
        = 用户说的"歌中间还会卡一下"）。

        取舍：EVENTS/beats 是**补量**性质（水面按拍微动态 + 拍网格），晚一点补无感；
        而它在播放中阻塞十几二十秒 = 明显卡顿 ⇒ 一律等到空闲期（预热线程下一轮）再补。
        回退开关：`MUSICAI_BEATS_IDLE_ONLY=0` ⇒ 恢复旧行为（播放中也索引）。
        """
        if os.environ.get("MUSICAI_BEATS_IDLE_ONLY", "1") == "0":
            return False                       # 显式关闭 → 旧行为
        return self._playing_now()

    def _playing_now(self) -> bool:
        """★ R119-3j：**唯一**的「是否在播放」判据（读 `radio.player.state` + 记诊断）。

        为什么抽出来：这条事实此前只有 `_beats_idle_only` 一处实现，但**需要它的地方
        已经扩散到 `ai/` 层** —— 例如 `ai/recommend.py` 的漫游索引重建实测 **12.5 秒**、
        在**本进程内**跑 CLAP、**完全不看播放状态**（与 beats/特征索引同一类事故）。
        而 `ai/` 拿不到 `RadioBridge` 实例 ⇒ 由本方法经 `core/playback_state`
        注册成**全局唯一判据**（依赖倒置），`ai/` 侧只问 `playback_state.is_playing()`。

        ⚠ 绝不给 `ai/` 再写第二份判断 —— 否则就成了"同一事实两份副本、说法可能不一致"，
          这正是本项目反复踩过的坑（诊断面/优先级都栽过一次）。
        """
        try:
            r = self.radio
            pl = getattr(r, "player", None) if r is not None else None
            # ★ R119-8：**优先「播放意图」**（R95d-Q0 已为同一件事在 `/api/state` 里改过）。
            #   切歌装载窗内引擎瞬时 STOPPED；只看裸 state 会把"正在准备下一首"误判成
            #   "空闲" ⇒ `_g3` 的「等空闲」闸门被骗开 ⇒ 15~20s 的整曲特征索引**正好在
            #   歌刚起播时开跑** ⇒ 播放中每秒被冻 ~1s（真机 `stall_stacks.log` burst 时刻
            #   的忙线程链 = `librosa.hpss ← music_features.index_track ← radio_bridge._g3`）。
            _playing = None
            intent = getattr(r, "is_playing_intent", None) if r is not None else None
            if callable(intent):
                try:
                    _playing = bool(intent())
                except Exception:  # noqa 判据自身出错 → 回退裸 state
                    _playing = None
            if _playing is None:
                st = getattr(pl, "state", None)
                _playing = getattr(st, "name", str(st)) == "PLAYING"
            try:                                  # ★ R119-2o：记决策（判据失效也能看出来）
                from core.audio_engine import note_gate
                note_gate(_playing)
            except Exception:  # noqa
                pass
            return _playing
        except Exception:  # noqa 判据失败 → 不拦（宁可按旧行为）
            return False

    def _feat_idle_gate(self, tid, wait_s=None, stable_s=None) -> bool:
        """★ R119-8：整曲特征索引的「等空闲」闸门（**抽成方法 ⇒ 可单测/可见红**）。

        返回 True = 可以开跑那条 15~20s、每秒把进程冻 ~1s 的重活。

        条件：**连续空闲 ≥ `stable_s`**（不是"此刻空闲"）。
        为什么必须"连续"：切歌装载窗内引擎会瞬时 STOPPED —— 若只看"此刻"，
        闸门会正好在**歌刚起播**那一刻放行（真机事故形态，见模块头 R119-8 注释）。

        等待上限 `wait_s`（默认 `FEAT_IDLE_WAIT_S` / 300s）内没等到就放弃本轮：
        特征索引是**补量**性质，下次放这首会重新排队（幂等）。
        回退开关：`MUSICAI_FEAT_IDLE_ONLY=0`（在调用方判，本函数只做"连续空闲"判定）。
        """
        if wait_s is None:
            try:
                wait_s = float(os.environ.get("MUSICAI_FEAT_IDLE_WAIT_S") or
                               getattr(self, "FEAT_IDLE_WAIT_S", 300.0))
            except Exception:  # noqa
                wait_s = 300.0
        if stable_s is None:
            try:
                stable_s = float(os.environ.get("MUSICAI_FEAT_IDLE_STABLE_S") or
                                 getattr(self, "FEAT_IDLE_STABLE_S", 4.0))
            except Exception:  # noqa
                stable_s = 4.0
        deadline = time.monotonic() + max(0.0, wait_s)
        idle_since = None
        while True:
            if self._beats_idle_only():          # 正在播放/准备 ⇒ 重新计时
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.monotonic()
                elif (time.monotonic() - idle_since) >= stable_s:
                    return True                  # 连续空闲够久 ⇒ 放行
            if time.monotonic() >= deadline:
                logger.debug("[Feat] 未取得「连续空闲 ≥%.1fs」，本轮放弃 tid=%s"
                             "（下次放这首会重新排队）", stable_s, tid)
                return False
            time.sleep(0.5)

    def _mood_grace_for(self, tid, db) -> float:
        """R95c-P0：按「该曲是否已索引」返回起播保护窗（秒）。

        · 已索引（track_segments 有该 track_id 的任意段）→ `MOOD_TRACK_GRACE_INDEXED_S`
          （1.5s，只留起播抖动余量）；此时 mood 循环只会 `load_segments` 查库，
          **不会**触发 CLAP 推理 ⇒ 原窗保护的"IO/GPU 风暴"不存在。
        · 未索引 → 保持原 `MOOD_TRACK_GRACE_S`（8.0s，首次索引确实是重活）。
        · 任何异常（无段表 / tid 非法）→ 保守回退 8.0s。

        带缓存：**只缓存"已索引"这个正向结果**（否则首次播放未索引的曲会永久记住
        8.0s，等它索引完再播同一首时仍然慢）。未索引时不缓存 —— 保护窗只有 8s、
        节拍 1s ⇒ 最多 8 次查询；索引完成后下一个 tick 立刻升级为 1.5s。
        """
        if tid is None:
            return self.MOOD_TRACK_GRACE_S
        try:
            key = int(tid)
        except Exception:  # noqa
            return self.MOOD_TRACK_GRACE_S
        if key in self._mood_grace_cache:
            return self.MOOD_TRACK_GRACE_INDEXED_S
        try:
            if db is not None:
                row = db.conn.execute(
                    "SELECT 1 FROM track_segments WHERE track_id=? LIMIT 1",
                    (key,)).fetchone()
                if row is not None:
                    if len(self._mood_grace_cache) < 512:
                        self._mood_grace_cache[key] = 1
                    return self.MOOD_TRACK_GRACE_INDEXED_S
        except Exception:  # noqa 无段表/表结构不同 → 保守用长窗
            return self.MOOD_TRACK_GRACE_S
        return self.MOOD_TRACK_GRACE_S

    def _start_mood_pusher(self) -> None:
        if self._mood_thread is not None:
            return
        self._mood_stop.clear()

        def _loop():
            _throttle_self_priority(-2)   # ★ 2026-09-18：mood/CLAP 索引线程压 LOWEST，护 RT 音频
            from webapi.bus import bus
            from core.semantic_axis import (
                anchors, project, SegmentIndexer, segment_index_for_position,
                ensure_quantiles, n_segments_for_duration)
            from core.audio_engine import STREAM_SR
            from core.short_term_semantics import (
                ShortTermTracker, fit_mapping, fuse_axes, l1_enabled,
                segments_ready, YAMNET_PATH, MAP_PATH, L1_PERIOD_S)
            from core.beat_events import (
                index_track as beat_index_track,
                beat_enabled, load_track_beats)
            # R99-G3：8 维音乐特征（标定链第①环；纯离线诊断/标定，绝不影响播放）
            from core.music_features import (
                index_track as feat_index_track,
                enabled as feat_enabled, load_features)

            tracker = ShortTermTracker()   # R83b L1 短期语义（懒加载，缺失自动回退）
            l1_last = 0.0
            l1_axes = None
            fit_done = False
            fit_tries = 0                      # R86 P0②：L1 映射拟合失败可重试
            fit_next = 0.0
            quant_done = False                 # R86 P0①：分位数标定失败可重试
            quant_next = 0.0
            # R84a EVENTS 层状态（beat/downbeat+旋律特征；每曲一发）
            beats_track = None
            # R99-G3 状态（8 维特征；每曲只索引一次，失败不重试）
            feat_track = None
            feat_done = False
            beats_sent = False
            beats_tries = 0                    # R86 P0：索引尝试次数（在线曲可重试）
            beats_next = 0.0                   # R86 P0：下次可尝试时刻（节流）
            seg_cache = {}                     # {seg_idx: float32 512 维}
            seg_track = None
            va_track = None                     # 路线 D：本曲是否已触发 DEAM VA 推理
            seg_stale = False                  # 旧 hop 表（段数不符，R83a 迁移）
            seg_tries = 0                      # R86 P0：段索引尝试次数
            seg_next = 0.0                     # R86 P0：下次可尝试时刻
            seg_rebuild_done = False           # R89：旧 hop 表整表重建只做一次
            expected = 0                       # R89：本曲预期段数（完成度判定）
            sent_none = False
            while not self._mood_stop.wait(self.MOOD_TICK_S):
                try:
                    r = self.radio
                    if r is None:
                        self._mood_diag = {"gate": "no_radio", "ts": time.time()}
                        continue
                    # ★★★ 2026-11-18：**不依赖 track_changed 事件**的当前曲校正。
                    #   事件桥万一丢事件，`_mood_track` 会永远停在 None/旧曲 ⇒ 语义层
                    #   整条链路静默（实测过：前端 semOn=false、水面跑基础档）。
                    #   这里每 tick 用 r.current 校正一次（纯读属性，不碰 Qt 主线程）。
                    try:
                        _cur = getattr(r, "current", None)
                        _cid = getattr(_cur, "id", None) if _cur is not None else None
                        if _cid is not None and ((not self._mood_track)
                                                 or self._mood_track.get("id") != _cid):
                            self._mood_track = {
                                "id": _cid,
                                "path": str(getattr(_cur, "path", "") or ""),
                                "duration": float(getattr(_cur, "duration", 0) or 0),
                                "source": str(getattr(_cur, "source", "") or ""),
                            }
                            self._mood_track_at = time.monotonic()
                            self._mood_pos = 0
                            self._mood_last = None
                            self._mood_diag = {"gate": "track_resync", "tid": _cid,
                                               "ts": time.time()}
                            self._mood_log("resync", "[SemAxis] mood 当前曲校正 tid=%s（事件未送达时的兜底）", _cid)
                    except Exception:  # noqa 校正失败不影响原逻辑
                        pass
                    tr = self._mood_track
                    if not tr or tr.get("id") is None:
                        self._mood_diag = {"gate": "no_track", "ts": time.time()}
                        self._mood_log("no_track", "[SemAxis] mood 跳过：无当前曲（track_changed 未送达且 r.current 为空）")
                        continue
                    extractor = getattr(getattr(r, "recommend", None),
                                        "extractor", None)
                    db = getattr(r, "db", None)
                    if extractor is None or db is None:
                        self._mood_diag = {"gate": "no_extractor", "ts": time.time(),
                                           "extractor": extractor is not None, "db": db is not None}
                        self._mood_log("no_ext", "[SemAxis] mood 跳过：extractor/db 未就绪 (extractor=%s db=%s)",
                                       extractor is not None, db is not None)
                        continue
                    # CLAP 就绪才推进（未就绪/已禁用 → 静默，前端保持中性）
                    # R80 起播防护：换曲后不跑 ensure/index（CLAP 权重加载/推理是
                    # 磁盘 IO+GPU 风暴，起播稳定期让 RT 优先）。
                    # R95c-P0：**已索引曲目短路** —— 此时 mood 循环只会查库、零推理，
                    #   风暴不成立 ⇒ 用 1.5s（`MOOD_TRACK_GRACE_INDEXED_S`）而非 8.0s。
                    now = time.monotonic()
                    _grace = self._mood_grace_for(tr.get("id"), db)
                    if now - self._mood_track_at < _grace:
                        self._mood_diag = {"gate": "grace", "ts": time.time(),
                                           "grace_s": round(_grace, 1),
                                           "elapsed_s": round(now - self._mood_track_at, 1)}
                        continue
                    if not anchors.ensure(extractor):
                        self._mood_diag = {"gate": "no_anchors", "ts": time.time()}
                        self._mood_log("anchors", "[SemAxis] mood 跳过：anchors 未就绪（CLAP 锚点/分位数未完成）")
                        continue
                    # R82c 分位数标定：锚点就绪后现算/加载（纯 numpy 查
                    # track_segments 表，零推理；失败静默回退固定尺度）。
                    # R86 P0①：段表此前恒空 → 标定永远失败。改为节流重试（15s），
                    # 在线曲索引落库后自动标定成功 → 轴值按 p10/p50/p90 拉开。
                    if not quant_done and now >= quant_next:
                        quant_next = now + 15.0
                        if ensure_quantiles(db):
                            quant_done = True
                    # R83b L1 短期语义：模型在而映射缺 → 一次性离线拟合（曲库
                    # 段配对岭回归，段间让步；失败/配对不足静默保持纯 L0）。
                    # R86 P0②：拟合需要 track_segments 有段——此前段表恒空 → 首次
                    # 必失败且 fit_done 已被置位 → 永不重试。改为失败可重试
                    # （每 30s、最多 5 次）：等 P0① 的在线曲索引落库后自动拟合成功。
                    # R0-C：段表不足 FIT_MIN_PAIRS 时不消耗重试预算——否则首播
                    # 只索引到 1 首曲就把 5 次重试烧光，之后永不尝试 → L1 永不就绪。
                    if (not fit_done and l1_enabled()
                            and YAMNET_PATH.exists() and not MAP_PATH.exists()
                            and fit_tries < 5 and now >= fit_next
                            and segments_ready(db)):
                        fit_next = now + 30.0
                        fit_tries += 1
                        # path_resolver：把在线曲的占位 path 解析到播放器落盘流
                        if fit_mapping(db, self._resolve_index_src) > 0:
                            fit_done = True
                    tid = tr["id"]
                    # ---- 路线 D：DEAM 本地 VA 头（每曲一次，覆盖 CLAP 锚点 VA）----
                    # 已训模型 models/mer_va_head_deam.npz（MERT-95M layer8 + 3×10s 窗口
                    # ridge λ=30，CV R2 V=0.364/A=0.502）；后台线程跑，不堵 mood 节拍。
                    # 结果 (valence,arousal)∈[0,1] 缓存，覆盖 project() 的 CLAP 派生 VA
                    # （标定差、情绪弱 → 水面"情绪体现得不好"的根因之一）。失败/未就绪
                    # → 维持 CLAP VA（零回归）；路径暂不可得不入 failed（下次播放可重试）。
                    if tid != va_track:
                        va_track = tid
                        if (tid not in self._va_cache and tid not in self._va_failed
                                and tid not in self._va_pending):
                            self._va_pending.add(tid)
                            def _va_job(tid=tid, tr=tr, r=r):
                                try:
                                    # ★ R118（修）：把推理线程压到后台低优先级，避免抢
                                    # PortAudio RT 回调的核 ⇒ 消除"播到十来秒歌曲疯狂卡几下"
                                    # 的卡顿（用户实测 VA 在 ~10s 才到、且与卡顿同时）。
                                    # ★ 2026-09-19 修复：原来这里是**第三份**裸 ctypes
                                    # 实现，与 _throttle_self_priority 同病（64 位上
                                    # 伪句柄被截断 ⇒ 静默空转）。统一委托，证据见该函数
                                    # docstring 与 _r119_probe_prio2.py。
                                    _throttle_self_priority(-2)   # LOWEST，护 RT
                                    p = self._local_index_path(tr, r)
                                    if not p:
                                        return               # 路径未就绪：不标 failed，下次播可重试
                                    from core.audio_engine import note_heavy
                                    # ★ R118（修）：实时路径只取首窗 10s（n_windows=1）提速
                                    # —— VA 早到、不堵 RT；代价是"以曲首 10s 代表全曲情绪"
                                    # （训练用 3 窗均值）。想回精度设 MER_VA_RT_WINDOWS=3。
                                    _nw = int(os.environ.get("MER_VA_RT_WINDOWS", "1"))
                                    _t_va = time.monotonic()   # ★ R119-2h：计时
                                    # ★ R119-2u **根治**：VA 推理搬进**独立子进程**（与 beats 同法）。
                                    #   它在本进程里实测 1.3~1.6s，且有一笔落在播放中（+3.17s），
                                    #   与起播后 1.88s 那次停顿的窗口完全吻合 ⇒ 只要在本进程里跑
                                    #   就必然占 GIL（优先级/闸门都救不了）⇒ 换进程。
                                    #   ⚠ `from mer_emotion_frontend import ...` **只在回退分支里**
                                    #   才 import —— 否则父进程照样会把 torch/模型load 进来。
                                    #   回退开关：`MUSICAI_VA_SUBPROC=0`。
                                    if os.environ.get("MUSICAI_VA_SUBPROC", "1") == "0":
                                        from mer_emotion_frontend import audio_to_emotion_latent
                                        lat = audio_to_emotion_latent(
                                            p, use="local_va", n_windows=_nw)
                                        note_heavy("va_infer",
                                                   (time.monotonic() - _t_va) * 1000.0)
                                    else:
                                        from core.va_proc import run as _va_run
                                        lat = _va_run(p, _nw)
                                        note_heavy("va_infer_subproc",
                                                   (time.monotonic() - _t_va) * 1000.0)
                                        if not lat:
                                            raise ValueError("VA 子进程未产出结果")
                                    v = (float(lat["valence"]) - 1.0) / 8.0
                                    a = (float(lat["arousal"]) - 1.0) / 8.0
                                    # ★ NaN 守卫：va 头偶发 NaN（静音窗）→ NaN!=NaN，
                                    # 走 except 标 failed，绝不把 NaN 写进 _va_cache/emit。
                                    if v != v or a != a:
                                        raise ValueError("DEAM VA 头返回非有限值")
                                    self._va_cache[tid] = (max(0.0, min(1.0, v)),
                                                           max(0.0, min(1.0, a)))
                                except Exception as e:  # noqa 失败绝不影响播放/语义层
                                    logger.warning("[VA] DEAM 头推理失败 tid=%s: %s", tid, e)
                                    self._va_failed.add(tid)
                                finally:
                                    self._va_pending.discard(tid)
                            threading.Thread(target=_va_job, daemon=True,
                                             name="va-deam").start()
                    # ---- R89 段索引（优先 / 增量 / 播放头优先）----
                    # ⚠⚠ 顺序：段索引必须排在 BEATS **之前**。BEATS 是整曲阻塞调用
                    #   （librosa 整首加载 + 30s 分块 ONNX + 逐块 pyin，一首歌十几秒
                    #   到几十秒），原先排在前面会把段索引与 L1 一起堵在同一线程里
                    #   → 换曲后水面长时间拿不到任何新语义（"切歌反应慢"的根因）。
                    #   现在：先按 ~SEG_BUDGET_S 的预算索引**播放头附近**的段（第一
                    #   个 tick 内就能用上），整曲铺完（或连续多次无进展）才放 BEATS。
                    if tid != seg_track:
                        seg_track = tid
                        seg_cache = SegmentIndexer.load_segments(tid, db) \
                            if tid is not None else {}
                        dur = float(tr.get("duration") or 0.0)
                        expected = n_segments_for_duration(dur)
                        seg_stale = bool(seg_cache) and dur > 10.0 \
                            and len(seg_cache) != expected
                        seg_rebuild_done = False
                        seg_tries = 0
                        seg_next = 0.0
                        sent_none = False       # 每曲重新允许一次中性占位推送
                    _mem_ok = True
                    try:
                        _mem_ok = extractor._memory_pressure_ok()
                    except Exception:  # noqa
                        _mem_ok = True
                    if (not seg_cache or seg_stale) and tid is not None \
                            and now >= seg_next and not _mem_ok:
                        self._mood_diag = {"gate": "seg_mem_pressure", "ts": time.time(),
                                           "seg_cache": len(seg_cache or {})}
                        self._mood_log("seg_mem", "[SemAxis] 段索引让路：内存压力检测未通过 tid=%s", tid)
                    if (not seg_cache or seg_stale) and tid is not None \
                            and now >= seg_next \
                            and _mem_ok:
                        i_path = self._local_index_path(tr, r)
                        if not i_path:
                            self._mood_diag = {"gate": "seg_no_src", "ts": time.time(),
                                               "tid": tid, "source": tr.get("source")}
                            self._mood_log("seg_nosrc",
                                           "[SemAxis] 段索引跳过：在线流尚无落盘文件 tid=%s source=%s",
                                           tid, tr.get("source"))
                        if i_path:
                            # 播放头所在段最先索引 → 当前在听的内容马上有语义
                            pseg = segment_index_for_position(
                                self._mood_pos, max(1, expected))
                            # ⚠ 段数不符有两种成因：①旧 hop 表（R83a 迁移残留）→ 必
                            #   **整表重建**（only_missing 会留着错位的旧向量，永远修不
                            #   好）；②元数据时长 > 真实音频时长（尾段压根不存在）→
                            #   重建也没用。故**每曲只允许整表重建一次**，之后仍不符就
                            #   按增量补，避免每 8s 反复全表重嵌入的活锁。
                            full = seg_stale and not seg_rebuild_done
                            # R99-T1：与预热线程互斥 + 抢占标记（预热见 _seg_busy
                            #   立即让路，故这里 acquire 不会被长片挡住）。
                            self._seg_busy = True
                            try:
                                from core.audio_engine import note_heavy
                                _t_seg = time.monotonic()   # ★ R119-2h：计时
                                with self._seg_lock:
                                    n_new = SegmentIndexer.index_track(
                                        tid, i_path, extractor, db,
                                        priority_seg=pseg,
                                        only_missing=not full,
                                        budget_s=(self.SEG_FULL_BUDGET_S if full
                                                  else self.SEG_BUDGET_S),
                                        # ★ 2026-09-21：生产链显式走常驻子进程
                                        #   （本进程零阻塞；失败自动回退本进程）
                                        use_proc=True)
                                # ★ 2026-09-21：走常驻子进程时这段墙钟是
                                #   「**等** worker」，本进程并没有占 GIL ⇒ 用 `~`
                                #   前缀标记（`attribute_stalls` 不把它算作停顿嫌疑）。
                                #   否则 `clap_wait` 那 1~3 s 的区间会与真正的停顿
                                #   相交，把归因带偏。
                                note_heavy("~clap_wait"
                                           if SegmentIndexer.LAST_PATH == "proc"
                                           else "clap_index",
                                           (time.monotonic() - _t_seg) * 1000.0)
                            except Exception as _se:  # noqa ★ 2026-11-18：索引异常可见
                                self._mood_diag = {"gate": "seg_index_error", "ts": time.time(),
                                                   "tid": tid, "error": str(_se)[:160]}
                                self._mood_log("seg_err",
                                               "[SemAxis] 段索引失败 tid=%s: %s: %s",
                                               tid, type(_se).__name__, _se)
                            finally:
                                self._seg_busy = False
                            if full:
                                seg_rebuild_done = True
                            if n_new:
                                seg_tries = 0
                                seg_next = now + self.SEG_GAP_S   # ★ 有进展也**让路**再续（护 RT）
                                seg_cache = SegmentIndexer.load_segments(tid, db)
                                if seg_cache:
                                    # R0-C：记下本曲实际用的音频来源，供 fit_mapping 配对
                                    self._index_src[int(tid)] = i_path
                                    # 重建过后仍缺段 → 判为②"尾段不存在"，不再当 stale
                                    seg_stale = (expected > 0
                                                 and len(seg_cache) < expected
                                                 and not seg_rebuild_done)
                            elif SegmentIndexer.LAST_PATH != "defer":
                                # ★ 2026-09-21：**defer（worker 正在加载）不算失败**。
                                #   否则冷启动那 ~7.6 s（实测）会把 INDEX_RETRIES
                                #   直接打满 ⇒ 后续不再给段索引让路、BEATS 抢先跑，
                                #   属于"静默的质量降级"。defer 时保留原计数。
                                seg_tries += 1
                                seg_next = now + self.INDEX_RETRY_S
                            else:
                                seg_next = now + self.INDEX_RETRY_S
                    # 段索引是否已铺满（BEATS 让路判断用；放在索引尝试之后取最新值）
                    seg_complete = bool(seg_cache) and not seg_stale
                    # ---- R84a EVENTS 层（beat/downbeat+每拍旋律/力度/密度）----
                    # 用户拍板：EVENTS 不做音频响应——beat 网格只做前端"按播放
                    # 时钟查表 → 亚秒级情绪微动态"的数据源。不依赖 CLAP。
                    # R89：整曲阻塞调用，**给段索引让路**——段铺满、或连续
                    # INDEX_RETRIES 次无进展（索引源不可得）后才做，避免把
                    # 换曲后的 L0/L1 一起堵死。
                    if beats_track != tid:
                        beats_track = tid
                        beats_sent = False
                        beats_tries = 0
                        beats_next = 0.0
                    if not beats_sent and (seg_complete
                                           or seg_tries >= self.INDEX_RETRIES):
                        try:
                            b_data = load_track_beats(db, tid)
                            if b_data is not None:
                                self._beats_cache = {"tid": tid, "beats": b_data}
                                bus.emit("beats", self._beats_cache)
                                beats_sent = True
                            elif not beat_enabled():
                                beats_sent = True       # EVENTS 关闭 → 不再尝试
                            # R99-T7e：beats 预热正在整曲索引（十几~几十秒）⇒
                            #   **当前曲优先但要等**：不消耗重试次数、不放弃，
                            #   下一个 tick 再看（`_beatw_busy` 是唯一的等待判据）。
                            elif self._beatw_busy:
                                pass
                            elif beats_tries < self.INDEX_RETRIES \
                                    and now >= beats_next \
                                    and not self._beats_idle_only():   # ★ R119-2j
                                # ★ 播放中不在这里做整曲 beats 索引（见 _beats_idle_only
                                #   docstring：单次 15~20.6s 无预算 ⇒ 每 ~1s 冻一次）。
                                #   刻意**不消费** beats_tries/beats_next：等不播了立刻补。
                                # R86 P0：在线曲落盘异步 → 换曲瞬间取不到可重试；
                                # 索引源改为播放器落盘的本地流文件（见 _local_index_path）
                                b_path = self._local_index_path(tr, r)
                                beats_tries += 1
                                beats_next = now + self.INDEX_RETRY_S
                                if b_path:
                                    # R99-T7e：与预热共用 `_beat_lock` ⇒ 绝不并发两路
                                    #   整曲 beats 索引（那会是 CPU/磁盘风暴，饿死音频回调）
                                    with self._beat_lock:
                                        # ★ R119-2i：**补上测量盲区** —— 上面注释自己
                                        #   就写着"整曲阻塞调用（十几~几十秒）"，却既没预算
                                        #   也没计时；起播后那串 ~1s 停顿若来自它，之前
                                        #   在整个诊断面上都是隐形的。
                                        from core.audio_engine import note_heavy
                                        _t_bt = time.monotonic()
                                        _nb = beat_index_track(tid, b_path, db)  # 幂等
                                        note_heavy("beats_index",
                                                   (time.monotonic() - _t_bt) * 1000.0)
                                        if _nb == -1:
                                            # ★ R119-2r：起播抢占 ⇒ **撤销这次重试计数**，
                                            #   停播后 2s 内就能重来（不消耗 INDEX_RETRIES）
                                            beats_tries = max(0, beats_tries - 1)
                                            beats_next = now + 2.0
                                    b_data = load_track_beats(db, tid)
                                    if b_data is not None:
                                        self._beats_cache = {
                                            "tid": tid, "beats": b_data}
                                        bus.emit("beats", self._beats_cache)
                                        beats_sent = True
                                if not beats_sent \
                                        and beats_tries >= self.INDEX_RETRIES:
                                    beats_sent = True   # 试满仍不可得 → 静默
                        except Exception:  # noqa EVENTS 失败绝不影响播放/语义层
                            pass
                    # ---- R99-G3：8 维音乐特征离线索引（标定链第①环）----
                    # 与 beats 同款门控（段铺满/试满后才做）+ **独立后台线程**
                    # —— 实测 120s 分析窗 ≈4.4s（librosa load + pYIN），
                    #    绝不放在 mood 循环里同步跑（会把语义推送堵死数秒）。
                    # 纯诊断/标定用途：失败只记 log，不影响播放与语义层。
                    if feat_track != tid:
                        feat_track = tid
                        feat_done = False
                    if not feat_done and (seg_complete
                                          or seg_tries >= self.INDEX_RETRIES):
                        try:
                            if not feat_enabled():
                                feat_done = True
                            elif load_features(db, tid) is not None:
                                feat_done = True       # 幂等：已有表直接跳过
                            else:
                                f_path = self._local_index_path(tr, r)
                                if f_path:
                                    def _g3(tid=tid, p=f_path):
                                        try:
                                            # ★ R119-3g：**等空闲再做**。`feat_index_track`（librosa 颤音扫描 / stft+hpss）
                                            #   实测持 GIL ~1 秒（肇事栈见 stall_stacks.log 的 `### STALL over≈1s`）
                                            #   ⇒ 播放中做必然可听地卡 —— 这正是"一曲一次"的元凶。与 beats 同处置：
                                            #   协作式让路 + 有界等待；等不到就放弃本轮（下次放这首会重排队，幂等）。
                                            if os.environ.get("MUSICAI_FEAT_IDLE_ONLY", "1") == "0":
                                                _idle_only = False                     # 显式回退旧行为
                                            else:
                                                _idle_only = bool(getattr(self, "FEAT_IDLE_ONLY", True))
                                            if _idle_only:
                                                # ★ R119-8：改用「**连续**空闲」闸门（可单测）。
                                                #   旧写法只看"此刻"，会被切歌装载窗骗开。
                                                if not self._feat_idle_gate(tid):
                                                    return
                                            seg_vecs = None
                                            try:
                                                seg_vecs = list(
                                                    SegmentIndexer.load_segments(
                                                        tid, db).values())
                                            except Exception:  # noqa 无段→form 维 None
                                                seg_vecs = None
                                            _t0 = time.monotonic()
                                            try:
                                                feat_index_track(
                                                    tid, p, db, seg_vecs=seg_vecs)
                                            finally:
                                                # ★ 补测量盲区：此前 `heavy` 里从来看不到特征索引（"跑了"≠"看得见"）
                                                try:
                                                    from core.audio_engine import note_heavy
                                                    note_heavy("feat_index", (time.monotonic() - _t0) * 1000.0)
                                                except Exception:  # noqa
                                                    pass
                                        except Exception as e:  # noqa
                                            logger.warning(
                                                "[Feat] 后台索引异常 tid=%s: %s", tid, e)

                                    threading.Thread(
                                        target=_g3, daemon=True,
                                        name="g3-features").start()
                                feat_done = True       # 只发起一次，失败不重试
                        except Exception:  # noqa G3 失败绝不影响播放/语义层
                            feat_done = True
                    # ---- L0（段嵌入投影）----
                    seg = -1
                    axes = None
                    if seg_cache:
                        seg = segment_index_for_position(
                            self._mood_pos, len(seg_cache))
                        vec = seg_cache.get(seg)
                        if vec is not None:
                            axes = project(vec)
                    # ---- L1（YAMNet@4s 短时轴）融合 ----
                    # R83c：残差限幅/一致性守卫/凸组合（w=0.3）。
                    # R86 P0②：L1 不再依赖段索引——在线曲段索引未就绪时**单独走
                    # L1**（只需锚点 + 映射矩阵），使在线曲不再长时间 src:none；
                    # 段就绪后自动回到 L0 锚 + L1 残差的主路径。
                    now_m = time.monotonic()
                    if now_m - l1_last >= L1_PERIOD_S:
                        l1_last = now_m
                        l1_axes = None
                        try:
                            player = getattr(r, "player", None)
                            mono = player.read_recent_mono(2.5) if player else None
                        except Exception:  # noqa
                            mono = None
                        if mono is not None:
                            l1_axes = tracker.infer(mono, STREAM_SR)
                    if l1_axes:
                        axes = fuse_axes(axes, l1_axes) if axes \
                            else dict(l1_axes)
                    if axes is None:
                        self._mood_diag = {"gate": "no_axes", "ts": time.time(),
                                           "seg_cache": len(seg_cache or {}),
                                           "src_none_sent": sent_none}
                        self._mood_log("no_axes",
                                       "[SemAxis] mood 无轴可推：段索引=%d 段（L1 也未产出）tid=%s",
                                       len(seg_cache or {}), tid)
                        if not sent_none:      # 无语义（索引/映射均未就绪）→ 一次性中性
                            bus.emit("mood", {"src": "none", "axes": {}})
                            sent_none = True
                        continue
                    sent_none = False
                    # ★ 2026-11-16 修：去重比较口径统一到**实际发出**的 axes。
                    # 修前用 axes（DEAM 覆盖前的 CLAP 派生 VA）比较、却存 emit_axes
                    # （覆盖后），VA 缓存命中后二者在 valence/arousal 上恒不等 ⇒
                    # `axes != _mood_last` 永远为真 ⇒ 每 1s 重复 emit（前端每秒全量
                    # 重算 + semBurst 突变通道反复续期）。先算 emit_axes 再比较即可，
                    # 推送内容本身一字不改。
                    va = self._va_cache.get(tid)
                    emit_axes = axes
                    if va is not None:
                        # 路线 D 覆盖：DEAM VA 就绪则覆盖 CLAP 派生 VA（情绪更准、可见）。
                        emit_axes = dict(axes)
                        emit_axes["valence"] = va[0]
                        # ★ R118（修）：DEAM arousal 是**曲级**量，直接覆盖会把段级
                        # 能量起伏（瞬间爆发）压平 ⇒ "音乐对瞬间爆发不敏感"。改 65/35
                        # 混合：曲级 DEAM（标定准、定基准，权重 0.65）+ 段级 CLAP arousal
                        # （随曲内段落起伏，权重 0.35）⇒ 既保标定又恢复曲内爆发动态，
                        # 同时压低欢快曲的正向激动上限（避免水面密点/过密）。
                        _clap_ar = axes.get("arousal", va[1])
                        emit_axes["arousal"] = max(
                            0.0, min(1.0, 0.65 * va[1] + 0.35 * _clap_ar))
                    if emit_axes != self._mood_last:  # 段变化/轴值变化才推（段级过渡）
                        self._mood_last = emit_axes
                        self._mood_src = "segment" if seg_cache else "l1"
                        self._mood_axes_n = len(emit_axes)
                        self._mood_diag = {"gate": "emit", "ts": time.time(),
                                           "src": self._mood_src, "seg": seg,
                                           "axes_n": self._mood_axes_n}
                        bus.emit("mood", {"src": self._mood_src,
                                          "seg": seg, "axes": emit_axes})
                except Exception:  # noqa 语义层失败绝不影响播放
                    pass

        self._mood_thread = threading.Thread(
            target=_loop, daemon=True, name="mood-push")
        self._mood_thread.start()
        logger.info("语义推送线程已启动（mood 2s 节拍，段变化推 14 维语义轴）")
        self._start_index_warmer()

    # ---------------- R99-T1：段索引预热（覆盖率补量） ----------------

    def _warm_needs_more(self, tid, duration, have: dict) -> bool:
        """该曲是否**还需要**索引：无段 → 是；有段但 < 预期段数 → **是（续跑）**。

        ⚠ 只看"有没有段"是错的：分段索引按 `budget_s` 切片跑（R89），首片往往
        只落 1~3 段。实测（verify_t1_coverage C5）20s 曲首片只落 1/3 段 —— 若把
        "有段"当成已覆盖，这个半截索引会被**永久跳过、再也补不齐**。
        """
        n = int((have or {}).get(int(tid), 0) or 0)
        if n == 0:
            return True
        try:
            from core.semantic_axis import n_segments_for_duration
            want = n_segments_for_duration(float(duration or 0.0))
        except Exception:  # noqa
            return False
        return want > 0 and n < want

    def _warm_candidates(self, db, r) -> list[tuple[int, str]]:
        """可索引候选（(track_id, 本地文件路径)），**按价值排序**、**只含还需索引的**。

        顺序：① 播放队列 `upcoming`（马上要听的，价值最高）→ ② 全库轮转
        （`_warm_cursor` 起，避免每次都从同几首开始）。

        ⚠ **只有能拿到音频文件**的曲目才可预热：
          · 本地曲 → `tracks.path` 存在即可；
          · 在线曲 → 需要**落盘流文件**。R99-T1a 修好稳定缓存键后，落盘流可跨
            启动复用，于是"以前听过一次"的在线曲现在**不用再听一遍**也能索引
            （这正是覆盖率能涨起来的根本原因）。从未播放过、且落盘流不存在的
            在线曲**无可索引音频** —— 要覆盖只能「预下载」，那属**独立决策**
            （带宽/磁盘），不在本方法内偷偷做（见 verify_t1_coverage A12）。
        """
        try:
            have = db.segment_progress()
        except Exception:  # noqa
            have = {}
        out: list[tuple[int, str]] = []
        seen: set[int] = set()

        def _consider(tr):
            tid = _fld(tr, "id")
            try:
                tid = int(tid or 0)
            except Exception:  # noqa
                return
            if not tid or tid in seen or tid in self._warm_gave_up:
                return
            if not self._warm_needs_more(tid, _fld(tr, "duration"), have):
                return
            p = self._warm_path_for(tr, db)
            if not p:
                return
            seen.add(tid)
            out.append((tid, p))

        # ① 播放队列优先（radio.upcoming 是 [{"track": Track}, …]）
        try:
            ups = list(getattr(r, "upcoming", None) or [])
        except Exception:  # noqa
            ups = []
        for e in ups[:8]:
            try:
                tr = e.get("track") if isinstance(e, dict) else getattr(e, "track", None)
                if tr is not None:
                    _consider(tr)
            except Exception:  # noqa
                continue
        # ② 全库轮转
        try:
            allt = db.all_tracks()
        except Exception:  # noqa
            allt = []
        n = len(allt)
        if n:
            cur = int(self._warm_cursor) % n
            for i in range(n):
                try:
                    _consider(allt[(cur + i) % n])
                except Exception:  # noqa
                    continue
        return out

    def _warm_path_for(self, tr, db) -> str | None:
        """单首曲目的可索引音频文件；不可得 → None（**纯查表，绝不下网络**）。

        `tr` 可能是 `sqlite3.Row`（只支持 `tr["k"]`）或 dict/Track（支持 `.get`）
        ⇒ 统一走 `_fld`。
        """
        src = str(_fld(tr, "source") or "")
        raw = str(_fld(tr, "path") or "")
        ext = _fld(tr, "ext_id")
        tid = _fld(tr, "id")
        try:
            tid = int(tid or 0)
        except Exception:  # noqa
            tid = 0
        if src == "local":
            try:
                return raw if raw and Path(raw).exists() else None
            except Exception:  # noqa
                return None
        # 在线曲：① 本会话已索引过 → 直接用记下的落盘流
        p = self._index_src.get(tid) if tid else None
        if p:
            try:
                if Path(p).exists():
                    return p
            except Exception:  # noqa
                pass
        # ② 用 T3 缓存里的 url 反推落盘流（T1a 修好稳定键 ⇒ 跨启动可复用，
        #    于是"以前听过一次"的在线曲**不用再听一遍**也能索引）
        try:
            u = db.get_cache("url", src, ext) if (src and ext) else None
            if u:
                from core.audio_engine import PlayerEngine
                hit = PlayerEngine._stream_cached(str(u))
                if hit:
                    return hit
        except Exception:  # noqa
            pass
        return None

    def _warm_step(self) -> int:
        """预热**单步**（线程循环体；独立方法 ⇒ 测试可直接驱动，不必等 2s 节拍）。

        返回本次新增段数；`-1` = 本步没做事（让路 / 无候选 / 内存紧张 / 未接入）。
        """
        if self._seg_busy:                       # ① 播放头优先，让路
            return -1
        r = self.radio
        if r is None:
            return -1
        db = getattr(r, "db", None)
        ex = getattr(getattr(r, "recommend", None), "extractor", None)
        if db is None or ex is None:
            return -1
        try:
            if not ex._memory_pressure_ok():
                return -1
        except Exception:  # noqa
            return -1
        try:
            cands = self._warm_candidates(db, r)
        except Exception:  # noqa
            return -1
        if not cands:
            return -1                            # 全部候选都已铺满（或不可索引）
        tid, path = cands[0]
        from core.semantic_axis import SegmentIndexer
        self._seg_busy = True                    # 与 mood 对称（自己也占位）
        try:
            with self._seg_lock:                 # ② 互斥：CLAP 是全进程单例
                n_new = SegmentIndexer.index_track(
                    tid, path, ex, db, only_missing=True,       # ③ 切片
                    budget_s=self.WARM_BUDGET_S,
                    use_proc=True)               # ★ 2026-09-21：预热同样走子进程
        finally:
            self._seg_busy = False
        if n_new:
            self._warm_stat["done"] += 1
            self._warm_stat["last"] = f"tid={tid} +{n_new}"
            self._warm_tries.pop(int(tid), None)     # 有进展 → 清零，续跑不设限
            self._index_src[int(tid)] = path         # 供 fit_mapping 复用
        elif SegmentIndexer.LAST_PATH == "defer":
            # ★ 2026-09-21：defer = worker 正在拉起/加载 ⇒ **既不算失败、也不判死**。
            #   worker 冷启动实测 7.6 s；若照旧累计，3 次就会 `_warm_gave_up`
            #   把这首歌**永久踢出预热**（安静的质量损失）。
            self._warm_stat["deferred"] = self._warm_stat.get("deferred", 0) + 1
        else:
            # 零新增：可能只是首片还没到（librosa 加载 > 预算），也可能是索引源
            # 真的没有更多内容 ⇒ 连续 N 次才判死，避免误杀与活锁。
            t = self._warm_tries.get(int(tid), 0) + 1
            self._warm_tries[int(tid)] = t
            if t >= self.WARM_MAX_TRIES:
                self._warm_gave_up.add(int(tid))
                self._warm_stat["gave_up"] += 1
                self._warm_tries.pop(int(tid), None)
            self._warm_stat["failed"] += 1
        # 游标前移，避免卡在同一批失败项上活锁
        self._warm_cursor = (self._warm_cursor + 1) % 100000
        return int(n_new)

    def _start_index_warmer(self) -> None:
        """R99-T1：起预热线程（幂等）。线程只做节拍，逻辑在 `_warm_step`。"""
        if self._warm_thread is not None:
            return
        self._warm_stop.clear()

        def _loop():
            _throttle_self_priority(-2)   # ★ 2026-09-18：预热线程压 LOWEST，护 RT 音频
            # ★ R119-2r：把"播放中让路"注册成整曲 beats 索引的**协作式中断钩子**
            #   （闸门只在开始前判一次 ⇒ 必须让运行中的索引也能被叫停）
            try:
                from core.beat_events import set_cancel_check
                set_cancel_check(self._beats_idle_only)
            except Exception as _e:  # noqa
                logger.debug("beats 中断钩子注册失败: %s", _e)
            while not self._warm_stop.wait(self.WARM_TICK_S):
                try:
                    n = self._warm_step()
                except Exception:  # noqa 预热失败绝不影响播放/语义层
                    n = -1
                if n > 0:
                    self._warm_stop.wait(self.WARM_GAP_S)   # ③ 片间让路

        self._warm_thread = threading.Thread(
            target=_loop, daemon=True, name="seg-warm")
        self._warm_thread.start()
        logger.info("段索引预热线程已启动（低优先级补量，%s 片/%.1fs 让路）",
                    self.WARM_BUDGET_S, self.WARM_GAP_S)
        self._start_predl()
        self._start_beat_warmer()
        self._start_runner_recycle()

    # ---------------- R99-T1c：预下载（全量段索引覆盖的唯一路径） ----------------

    def _predl_candidates(self, db, r) -> list:
        """待预下载的曲目：**还需索引** + 在线 + 尚无可索引音频。

        顺序：播放队列优先 → 全库轮转（同 `_warm_candidates` 的价值序）。
        已在 `_warm_needs_more` 判为"不需要"的（已铺满）直接排除 —— 预下载的唯一
        目的是**给段索引提供音频**，已铺满的曲下回来纯属浪费带宽+磁盘。
        """
        try:
            have = db.segment_progress()
        except Exception:  # noqa
            have = {}
        out, seen = [], set()

        def _consider(tr):
            tid = _fld(tr, "id")
            try:
                tid = int(tid or 0)
            except Exception:  # noqa
                return
            src = str(_fld(tr, "source") or "")
            ext = _fld(tr, "ext_id")
            if not tid or tid in seen or src in ("", "local") or not ext:
                return                      # 本地曲已有文件，无需下载
            if not self._warm_needs_more(tid, _fld(tr, "duration"), have):
                return                      # 已铺满 → 不用下
            try:
                if self._warm_path_for(tr, db):
                    return                  # 已经有落盘流（T1a 稳定键命中）→ 不用下
            except Exception:  # noqa
                pass
            seen.add(tid)
            out.append(tr)

        try:
            ups = list(getattr(r, "upcoming", None) or [])
        except Exception:  # noqa
            ups = []
        for e in ups[:8]:
            try:
                tr = e.get("track") if isinstance(e, dict) else getattr(e, "track", None)
                if tr is not None:
                    _consider(tr)
            except Exception:  # noqa
                continue
        try:
            allt = db.all_tracks()
        except Exception:  # noqa
            allt = []
        n = len(allt)
        if n:
            cur = int(self._predl_cursor) % n
            for i in range(n):
                try:
                    _consider(allt[(cur + i) % n])
                except Exception:  # noqa
                    continue
        return out

    def _predl_settled(self) -> bool:
        """换曲后 `PRE_DL_SETTLE_S` 内**不许**预下载。

        理由：起播那几秒正是当前曲在做自己的下载（`load()` → `_download_stream`），
        预下载此时抢带宽会直接推高**起播延迟**（用户最敏感的指标）。`_mood_track_at`
        是换曲时刻（mood 线程维护），一个单调时钟差就是最省的判据。
        """
        try:
            at = float(getattr(self, "_mood_track_at", 0.0) or 0.0)
        except Exception:  # noqa
            return True
        if at <= 0.0:
            return True                     # 还没换过曲 / 未接入 → 不拦
        return (time.monotonic() - at) >= self.PRE_DL_SETTLE_S

    def _predl_step(self) -> int:
        """预下载**单步**（线程循环体；独立方法 ⇒ 测试可直接驱动）。

        返回本次新增字节数；`0` = 本步没做事（关/让路/预算用尽/无候选/已有文件）。
        """
        if not self.predl_on:
            return 0
        if self._seg_busy:                  # 播放头索引优先
            return 0
        if self._predl_bytes >= self.PRE_DL_MAX_MB * 1024 * 1024:
            return 0                        # 预算用尽 → 停（不无限制吃磁盘）
        if not self._predl_settled():
            return 0
        r = self.radio
        if r is None:
            return 0
        db = getattr(r, "db", None)
        engine = getattr(r, "player", None)
        if db is None or engine is None:
            return 0
        try:
            cands = self._predl_candidates(db, r)
        except Exception:  # noqa
            return 0
        if not cands:
            return 0
        tr = cands[0]
        tid = int(_fld(tr, "id") or 0)
        src = str(_fld(tr, "source") or "")
        ext = _fld(tr, "ext_id")
        try:
            url = self._predl_resolve(r, tr)
        except Exception as e:  # noqa
            logger.warning("[PreDL] 直链解析异常 tid=%s: %s: %s",
                           tid, type(e).__name__, e)
            url = None
        if not url:
            self._predl_stat["failed"] += 1
            self._predl_cursor = (self._predl_cursor + 1) % 100000
            return 0
        from core.audio_engine import PlayerEngine
        if PlayerEngine._stream_cached(url):
            self._predl_stat["skipped"] += 1
            self._predl_cursor = (self._predl_cursor + 1) % 100000
            return 0
        try:
            path = engine._download_stream(url)
        except Exception:  # noqa
            path = None
        if not path:
            self._predl_stat["failed"] += 1
            self._predl_cursor = (self._predl_cursor + 1) % 100000
            return 0
        try:
            nbytes = Path(path).stat().st_size
        except Exception:  # noqa
            nbytes = 0
        self._predl_bytes += float(nbytes)
        self._predl_stat["done"] += 1
        self._predl_stat["last"] = f"tid={tid} {nbytes/1e6:.1f}MB"
        # 记下索引源，预热线程可直接用（免得再靠 url 缓存反推）
        if tid:
            self._index_src[tid] = str(path)
        logger.info("[PreDL] 预下载 %s：%s（累计 %.0fMB / 预算 %.0fMB）",
                    _fld(tr, "title", "?"), Path(path).name,
                    self._predl_bytes / 1e6, self.PRE_DL_MAX_MB)
        self._predl_cursor = (self._predl_cursor + 1) % 100000
        return int(nbytes)

    def _predl_resolve(self, r, tr) -> str | None:
        """解析直链（走 T3 读穿缓存；失败不缓存）。绝不在此写任何缓存行。"""
        fn = getattr(r, "_resolve_url_cached", None)
        if callable(fn):
            return fn(tr)
        fn = getattr(r, "_resolve_with_retry", None)
        if callable(fn):
            return fn(tr)
        return None

    def _start_predl(self) -> None:
        """起预下载线程（幂等）。线程只做节拍，逻辑在 `_predl_step`。"""
        if self._predl_thread is not None:
            return
        self._predl_stop.clear()

        def _loop():
            while not self._predl_stop.wait(self.PRE_DL_TICK_S):
                try:
                    n = self._predl_step()
                except Exception:  # noqa 预下载失败绝不影响播放
                    n = 0
                if n > 0:
                    self._predl_stop.wait(self.PRE_DL_GAP_S)   # 让路

        self._predl_thread = threading.Thread(
            target=_loop, daemon=True, name="track-predl")
        self._predl_thread.start()
        logger.info("预下载线程已启动（最低优先级，预算 %.0fMB，间隔 %.1fs）",
                    self.PRE_DL_MAX_MB, self.PRE_DL_GAP_S)

    def predl_state(self) -> dict:
        """预下载可观测面（真机 F5 / 覆盖率路由一并读）。"""
        return {"on": bool(getattr(self, "predl_on", self.PRE_DL_ON)),
                "mb": round(getattr(self, "_predl_bytes", 0.0) / 1e6, 1),
                "budget_mb": self.PRE_DL_MAX_MB,
                "alive": bool(getattr(self, "_predl_thread", None)
                              and self._predl_thread.is_alive()),
                "settled": self._predl_settled(),
                **dict(getattr(self, "_predl_stat", {}) or {})}

    # ---------------- R99-T7e：beats 预热（EVENTS / 瞬时层覆盖率） ----------------

    def _beatw_candidates(self, db, r) -> list:
        """**还没有 beats** 且**能拿到音频**的曲目（队列优先 → 全库轮转）。

        ⚠ 与段预热同款判据（`_warm_path_for`）—— 本地曲直接可用，在线曲靠
          T1a 的稳定落盘流 / T3 的 url 缓存反推。没有音频就无从索引。
        """
        try:
            have = db.beat_track_ids()
        except Exception:  # noqa
            have = set()
        out, seen = [], set()

        def _consider(tr):
            tid = _fld(tr, "id")
            try:
                tid = int(tid or 0)
            except Exception:  # noqa
                return
            if not tid or tid in seen or tid in have or tid in self._beatw_gave_up:
                return
            if not self._warm_path_for(tr, db):
                return
            seen.add(tid)
            out.append(tr)

        try:
            ups = list(getattr(r, "upcoming", None) or [])
        except Exception:  # noqa
            ups = []
        for e in ups[:8]:
            try:
                tr = e.get("track") if isinstance(e, dict) else getattr(e, "track", None)
                if tr is not None:
                    _consider(tr)
            except Exception:  # noqa
                continue
        try:
            allt = db.all_tracks()
        except Exception:  # noqa
            allt = []
        n = len(allt)
        if n:
            cur = int(self._beatw_cursor) % n
            for i in range(n):
                try:
                    _consider(allt[(cur + i) % n])
                except Exception:  # noqa
                    continue
        return out

    def _beatw_step(self) -> int:
        """beats 预热**单步**：为 1 首缺拍表的曲目做整曲 EVENTS 索引（阻塞）。

        返回 1 = 本次索引成功；0 = 没做事 / 失败（失败计入 `_beatw_tries`，到
        `BEATW_MAX_TRIES` 判死，防"永远做不完"的活锁）。
        ⚠ 整曲阻塞（十几~几十秒）⇒ 只在**独立后台线程**里跑，且持 `_beat_lock`
          与 mood 的 beats 索引互斥；`_beatw_busy` 让 mood 知道"等一下，别烧重试"。
        """
        if not self.beatw_on:
            return 0
        if self._seg_busy or self._beatw_busy:
            return 0                              # 播放头索引优先
        if not self._predl_settled():
            return 0                              # 换曲静默窗内不做重活
        r = self.radio
        if r is None:
            return 0
        db = getattr(r, "db", None)
        if db is None:
            return 0
        try:
            cands = self._beatw_candidates(db, r)
        except Exception:  # noqa
            return 0
        if not cands:
            return 0
        # ★ R119-2j：播放中不做整曲 beats 索引（15~20.6s 无预算 ⇒ 每 ~1s 冻一次）
        if self._beats_idle_only():
            return 0
        tr = cands[0]
        tid = int(_fld(tr, "id") or 0)
        path = self._warm_path_for(tr, db)
        if not tid or not path:
            return 0
        from core.beat_events import index_track as beat_index_track, load_track_beats
        self._beatw_busy = True
        try:
            with self._beat_lock:                # 与 mood 的 beats 索引互斥
                _res = beat_index_track(tid, path, db)  # 幂等；整曲阻塞
            if _res == -1:
                # ★ R119-2r：起播抢占 ⇒ 既不算成功也不算失败（别把可达标的曲误标 gave_up）
                return 0
            ok = load_track_beats(db, tid) is not None
        except Exception:  # noqa
            ok = False
        finally:
            self._beatw_busy = False
        if ok:
            self._beatw_stat["done"] += 1
            self._beatw_stat["last"] = f"tid={tid}"
            self._beatw_tries.pop(tid, None)
        else:
            t = self._beatw_tries.get(tid, 0) + 1
            self._beatw_tries[tid] = t
            if t >= self.BEATW_MAX_TRIES:
                self._beatw_gave_up.add(tid)
                self._beatw_stat["gave_up"] += 1
                self._beatw_tries.pop(tid, None)
            self._beatw_stat["failed"] += 1
        self._beatw_cursor = (self._beatw_cursor + 1) % 100000
        return 1 if ok else 0

    def _start_beat_warmer(self) -> None:
        """起 beats 预热线程（幂等）。线程只做节拍，逻辑在 `_beatw_step`。"""
        if self._beatw_thread is not None:
            return
        self._beatw_stop.clear()

        def _loop():
            while not self._beatw_stop.wait(self.BEATW_TICK_S):
                try:
                    n = self._beatw_step()
                except Exception:  # noqa
                    n = 0
                if n:
                    self._beatw_stop.wait(self.BEATW_GAP_S)

        self._beatw_thread = threading.Thread(
            target=_loop, daemon=True, name="beat-warm")
        self._beatw_thread.start()
        logger.info("beats 预热线程已启动（EVENTS 覆盖率补量，%s 首/%.1fs 让步）",
                    self.BEATW_GAP_S, self.BEATW_GAP_S)

    # ---------------- R119-3f：Ollama runner 内存回收 ----------------

    def _runner_recycle_step(self) -> dict:
        """一拍：**采集事实 → 纯判据 → 必要时回收**。返回可观测的决策记录。

        ⚠ 纪律：只有在「**稳定空闲 + 提交吃紧**」时才动手 —— 重载是 5.4s 重活，
          播放中做必然听得出卡顿（这正是本项目一路在修的那类问题，不能自己再造一个）。
        """
        from ai.model import recycle_ollama_runner, should_recycle_runner
        from core.sysmem import commit_avail_gb

        # ① 事实：是否在播放 —— 复用 `_beats_idle_only()` 的同一判据（一个事实一个出口）
        playing = bool(self._beats_idle_only())
        if playing:
            self._runner_idle_ticks = 0
        else:
            self._runner_idle_ticks += 1

        # ② 事实：提交可用（读不到 → -1.0 ⇒ 判据会**拒绝**动手）
        cab = commit_avail_gb()
        since_last = ((time.monotonic() - self._runner_last)
                      if self._runner_last > 0 else 1e9)

        ok = (bool(getattr(self, "ryc_on", self.RUNNER_RECYCLE_ON))
              and should_recycle_runner(
                  playing=playing, idle_ticks=self._runner_idle_ticks,
                  need_idle_ticks=self.RYC_NEED_IDLE_TICKS,
                  commit_avail_gb=cab, floor_gb=self.RYC_COMMIT_FLOOR_GB,
                  since_last_s=since_last, cooldown_s=self.RYC_COOLDOWN_S))
        if not ok:
            if playing:
                why = "playing"
            elif self._runner_idle_ticks < self.RYC_NEED_IDLE_TICKS:
                why = "idle_ticks=%d/%d" % (self._runner_idle_ticks,
                                            self.RYC_NEED_IDLE_TICKS)
            elif cab < 0:
                why = "no_mem_fact"
            elif cab >= self.RYC_COMMIT_FLOOR_GB:
                why = "commit_ok(%.1fG)" % cab
            else:
                why = "cooldown(%.0fs/%.0fs)" % (since_last, self.RYC_COOLDOWN_S)
            self._runner_stat["skipped"] = why
            return dict(self._runner_stat, acted=False)

        res = recycle_ollama_runner()
        self._runner_last = time.monotonic()
        if res.get("ok"):
            self._runner_stat.update(
                n=self._runner_stat["n"] + 1,
                last_seconds=float(res.get("seconds", 0.0)),
                commit_before_gb=float(res.get("commit_before_gb", -1.0)),
                commit_after_gb=float(res.get("commit_after_gb", -1.0)),
                last_result="ok", skipped="")
        else:
            self._runner_stat["last_result"] = "fail: %s" % res.get("reason", "")
        return dict(self._runner_stat, acted=bool(res.get("ok")))

    def runner_recycle_state(self) -> dict:
        """可观测面：让真机/套件看出「有没有回收过、为什么跳过」。

        ⚠ 全字段 `getattr` 兜底（同 `beatw_state`）：测试里用 `RadioBridge.__new__`
          造桥、或初始化中途失败都可能缺字段，诊断接口**绝不能**因此 500。
        """
        st = dict(getattr(self, "_runner_stat", {}) or {})
        try:
            from core.sysmem import commit_avail_gb
            cab = commit_avail_gb()
        except Exception:  # noqa
            cab = -1.0
        st.update(on=bool(getattr(self, "ryc_on", self.RUNNER_RECYCLE_ON)),
                  alive=bool(getattr(self, "_runner_thread", None)
                             and self._runner_thread.is_alive()),
                  idle_ticks=int(getattr(self, "_runner_idle_ticks", 0)),
                  floor_gb=self.RYC_COMMIT_FLOOR_GB,
                  commit_avail_gb=cab)
        return st

    def dj_speech_state(self) -> dict:
        """★ R119-4v：**口播质量计账** —— "正确的口播"与"退化兜底"各播了几条。

        为什么必须可观测：用户要的**不是**"别有兜底"，而是"**正确的口播别缺席**"
        （原话："不是讨厌兜底口播而是讨厌没有正确的口播"）。而日志里正确句和兜底句
        外观上都是 `dj_speech`，只能靠肉眼在成百上千行里数。这里给出四个计数 + 两个比率，
        一眼看出"这一轮到底是谁在说话"；`intro_fallback_ratio` 长期居高 = 正确口播
        还是来不及（预取/检索/模型任一段变慢），是**回归的早期信号**。

        全字段 `getattr` 兜底（同 `runner_recycle_state`）：测试里用 `RadioBridge.__new__`
        造桥、或初始化中途失败都可能缺字段，诊断接口**绝不能**因此 500。
        """
        r = getattr(self, "radio", None)
        if r is None:
            return {"ok": False, "error": "radio 未接入"}
        try:
            ai = int(getattr(r, "_n_intro_ai", 0) or 0)
            fb = int(getattr(r, "_n_intro_fallback", 0) or 0)
            sp = int(getattr(r, "_n_trans_spoke", 0) or 0)
            sk = int(getattr(r, "_n_trans_skip", 0) or 0)
            tot = ai + fb
            ttot = sp + sk
            return {
                "ok": True,
                "intro_ai": ai,             # 正确的 AI 导听口播（想要的就是它多）
                "intro_fallback": fb,       # 退化兜底句（保底，不算 bug）
                "intro_fallback_ratio": (round(fb / tot, 3) if tot else 0.0),
                "trans_spoke": sp,          # 转场真的出声（该边界有人在说话）
                "trans_silent": sk,         # 该边界没人说话 ⇒ 下一首照常补导听
                "trans_spoke_ratio": (round(sp / ttot, 3) if ttot else 0.0),
                "trans_spoke_now": bool(getattr(r, "_trans_spoke", False)),
            }
        except Exception as e:  # noqa 诊断绝不拖垮状态接口
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    def _start_runner_recycle(self) -> None:
        """起 runner 回收线程（幂等）。线程只做节拍，逻辑在 `_runner_recycle_step`。"""
        if self._runner_thread is not None:
            return
        self._runner_stop.clear()

        def _loop():
            _throttle_self_priority(-2)     # 诊断型线程 ⇒ 压 LOWEST 护 RT 音频
            while not self._runner_stop.wait(self.RYC_TICK_S):
                try:
                    self._runner_recycle_step()
                except Exception:  # noqa  回收失败绝不影响播放/口播
                    pass

        self._runner_thread = threading.Thread(
            target=_loop, daemon=True, name="runner-recycle")
        self._runner_thread.start()
        logger.info("Ollama runner 回收线程已启动（空闲期回收，%.0fs 一拍；"
                    "提交可用 < %.1fG 才动手）", self.RYC_TICK_S, self.RYC_COMMIT_FLOOR_GB)

    def beatw_state(self) -> dict:
        """beats 预热可观测面（真机 F5 / 覆盖率路由一并读）。

        ⚠ 全字段 `getattr` 兜底：本方法是**诊断路由**的唯一实现，
          任何字段缺失都不该让 `/api/index/coverage` 500
          （测试里用 `RadioBridge.__new__` 造桥、真机上初始化失败都可能缺字段）。
        """
        return {"on": bool(getattr(self, "beatw_on", self.BEATW_ON)),
                "alive": bool(getattr(self, "_beatw_thread", None)
                              and self._beatw_thread.is_alive()),
                "busy": bool(getattr(self, "_beatw_busy", False)),
                **dict(getattr(self, "_beatw_stat", {}) or {})}

    def index_coverage(self) -> dict:
        """R99-T1：覆盖率（含预热进度）—— 真机 F5 一行可读。

        `incomplete` 在 db 层算不了（需要 `n_segments_for_duration`，属语义层
        口径）⇒ 这里补：**有索引但段数明显不足 metadata 时长**的曲目（播放到
        后段会掉回中性 = 半残，不能算真覆盖）。
        """
        db = getattr(self.radio, "db", None) if self.radio else None
        out = {"total": 0, "indexed": 0, "missing": 0, "by_source": {},
               "incomplete": 0, "warm": dict(getattr(self, "_warm_stat", {}) or {}),
               "warm_alive": bool(getattr(self, "_warm_thread", None)
                                  and self._warm_thread.is_alive()),
               "predl": self.predl_state(),
               "beatw": self.beatw_state()}
        if db is None:
            return out
        try:
            out.update(db.segment_coverage())
        except Exception:  # noqa
            return out
        try:
            from core.semantic_axis import n_segments_for_duration
            for row in db.all_tracks():
                n_raw = db.get_track_segments(int(row["id"]))
                if not n_raw:
                    continue
                want = n_segments_for_duration(float(row["duration"] or 0.0))
                if want > 0 and len(n_raw) < want:
                    out["incomplete"] += 1
        except Exception:  # noqa
            pass
        return out

    def get_beats(self) -> dict | None:
        """R84a：当前曲已下发的 beats 载荷（WS 断线重连/晚进页的补取口）。

        只回缓存，不现算——首播索引在 mood 线程后台做，未就绪时返回 None
        （前端包络保持中性，下一首自然接上）。
        """
        return self._beats_cache

    @property
    def ready(self) -> bool:
        return self.radio is not None

    @property
    def controller(self):
        """已接入的 AIRadioController（未接入 → None）。

        只读访问口：状态/诊断路由需要拿到 controller（例如核对事件桥线程亲和性），
        而 `self.radio` 是**实例属性**——类级扫描（verify_round44 E65）看不到它，
        路由里直接写 `bridge.radio` 会让「引用不存在的属性」检测器失效。走 property
        既保住该检测器，也不再让外部代码戳实例字段。
        """
        return self.radio

    def audio_diag(self) -> dict:
        """R88：音频链路健康度（PortAudio 状态位 + 音乐队列水位）转发给诊断路由。

        走**方法**而非 property，是为了让 `/api/status` 里 `bridge.audio_diag()`
        同时被 verify_round44 E65 的「引用不存在的属性」扫描（`hasattr` 类级可见）
        与真实调用覆盖；控制器未接入时返回可解释的错误体而非抛异常。
        """
        try:
            ctrl = self.radio
            player = getattr(ctrl, "player", None) if ctrl is not None else None
            if player is None:
                return {"ok": False, "error": "player 未接入"}
            return player.audio_diag()
        except Exception as e:  # noqa 诊断绝不拖垮健康检查
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _emit(self, event: str, payload: Any = None) -> None:
        from webapi.bus import bus

        bus.emit(event, payload)

    def _wire_events(self) -> None:
        """订阅 controller 事件（事件名与 ui/bridge_host.py::_wire_radio_events 一致）。"""
        r = self.radio

        def _sub(name: str, fn):
            try:
                r.on(name, fn)
            except Exception as e:  # noqa
                logger.warning("订阅事件 %s 失败: %s", name, e)

        _sub("track_changed", self._on_track_changed)
        _sub("playing", lambda on: self._emit("playing", bool(on)))
        _sub("progress", lambda ms: (self._cache_mood_pos(ms),
                                     self._emit("progress", int(ms))))
        _sub("dj_speech", lambda text: self._emit("dj_speech", text))
        _sub("thinking", lambda on: self._emit("thinking", bool(on)))
        # R57-3：连跳风暴刹车事件（source_storm）→ 前端 toast
        _sub("radio_error", lambda e: self._emit("radio_error", e))
        _sub("recommend_reason", lambda t: self._emit("recommend_reason", str(t or "")))
        _sub("lyrics", lambda text: self._emit("lyrics", {"text": text}))
        _sub("lyric_line",
             lambda idx, text: self._emit("lyric_line", {"index": idx, "text": text}))
        _sub("playlist_updated",
             lambda entries: self._emit("playlist_updated",
                                        [self.entry_to_dict(e) for e in (entries or [])]))
        _sub("reco_updated",
             lambda entries: self._emit("reco_updated",
                                        [self.entry_to_dict(e) for e in (entries or [])]))

    # ---------------- 数据转换（Qt/Track → 纯 dict，供 JSON 序列化） ----------------

    # P4 语义层：track_changed/progress 的轻量缓存（mood 线程专用快照，
    # 只在本回调线程写 mood 线程读——GIL 原子字段赋值，无需加锁）。
    def _on_track_changed(self, t) -> None:
        try:
            self._mood_track = {
                "id": getattr(t, "id", None),
                "path": str(getattr(t, "path", "") or ""),
                "duration": float(getattr(t, "duration", 0) or 0),
                "source": str(getattr(t, "source", "") or ""),
            }
        except Exception:  # noqa
            self._mood_track = None
        self._mood_track_at = time.monotonic()
        self._mood_pos = 0
        self._mood_last = None
        self._emit("track_changed", self.track_to_dict(t))

    def _cache_mood_pos(self, ms) -> None:
        try:
            self._mood_pos = int(ms)
        except Exception:  # noqa
            pass


    # ⚠⚠ 封面 URL 改写（2026-09-06）：
    #   库里本地封面的历史值是 `file://data_runtime/covers/local_xxx.jpg`
    #   —— 相对路径（host 被当成 data_runtime），**绝对 file:///D:/... 也不行**，
    #   因为前端是 http://127.0.0.1:8765 页面，Chromium 一律拒绝加载 file:// 子资源：
    #       js: Not allowed to load local resource: file://data_runtime/covers/...
    #   所有出口的 cover 字段都必须过这个函数，统一成 `/covers/<文件名>`（HTTP）。
    #   在线 http(s) 封面原样返回；改不动的（裸相对路径等）返回 ""，
    #   由前端程序化占位兜底，绝不能再把 file:// 吐给浏览器。
    @staticmethod
    def _cover_url(raw) -> str:
        s = str(raw or "").strip()
        if not s:
            return ""
        # ⚠⚠ R57 CORS：在线图床（y.gtimg.cn 等）**无 ACAO 头**，http 页面 <img> 直连必被拦
        # （`blocked by CORS policy: No 'Access-Control-Allow-Origin'`）→ http(s) 封面一律
        # 改走本站代理 /api/cover?u=<enc>（server.py 服务端拉图，无浏览器同源限制）。
        if s.startswith(("http://", "https://")):
            # 排除本站自身地址（cover 已在本站，代理会自环）
            if "127.0.0.1:8765" in s or "localhost:8765" in s:
                return s
            try:
                import urllib.parse
                return "/api/cover?u=" + urllib.parse.quote(s, safe="")
            except Exception:  # noqa
                return s
        if s.startswith("/api/cover?") or s.startswith("/covers/"):
            return s
        if s.startswith("data:"):        # data URI（程序化占位封面）原样透出
            return s
        if not s.startswith("file://"):
            # 裸相对路径（如 data_runtime/covers/xxx.jpg）也一并修
            if "/covers/" in s or "\\covers\\" in s or s.startswith("data_runtime"):
                s = "file://" + s.lstrip("/")
            elif s.startswith("/"):
                return s                 # 已是站内路径（/covers/… 已在上分支命中，此处=/x 站点文件）
            elif "." in s.rsplit("/", 1)[-1] and "/" not in s:
                # ⚠ R57-1：纯裸文件名（如 917c1b….jpg，历史脏数据）——
                # 前端 http://127.0.0.1:8765 页面上它会被解析成 /917c1b….jpg → 404。
                # 一律改写成 /covers/<文件名>（封面缓存目录挂载点）。
                return "/covers/" + s.rsplit("/", 1)[-1]
            else:
                return ""                # 占位符（🎵 等）→ 让前端走程序化封面
        rest = s[7:]
        # Windows: file:///D:/... → 去多余斜杠
        if rest.startswith("/") and len(rest) > 3 and rest[2] == ":":
            rest = rest[1:]
        name = rest.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or "." not in name:
            return ""
        return "/covers/" + name

    @classmethod
    def track_to_dict(cls, t) -> dict | None:
        if t is None:
            return None
        try:
            return {
                "id": getattr(t, "id", None),
                "title": getattr(t, "title", "") or "",
                "artist": getattr(t, "artist", "") or "",
                "album": getattr(t, "album", "") or "",
                "cover": cls._cover_url(getattr(t, "cover", "")),
                "duration": float(getattr(t, "duration", 0) or 0),
                "source": getattr(t, "source", "") or "",
                "ext_id": getattr(t, "ext_id", "") or "",
            }
        except Exception:  # noqa
            return None

    @classmethod
    def entry_to_dict(cls, e) -> dict:
        """队列/推荐条目 → dict（条目是 {"track":Track, "reason":str, ...}）。"""
        try:
            if isinstance(e, dict):
                return {
                    "id": e.get("id") or (e.get("track") or {}).get("id")
                        if isinstance(e.get("track"), dict) else e.get("id"),
                    "track": cls.track_to_dict(e.get("track")) if e.get("track") else None,
                    "reason": e.get("reason", "") or "",
                    "current": bool(e.get("current", False)),
                }
            return {"track": cls.track_to_dict(getattr(e, "track", None)),
                    "reason": getattr(e, "reason", "") or "", "current": False}
        except Exception:  # noqa
            return {}

    # ---------------- 播放控制（Phase 2） ----------------

    # ★ R119-5a：状态轮询保护（**类级**，全桥共用同一把门 + 同一份快照）
    _state_gate = threading.Lock()
    _state_cache: dict | None = None

    @classmethod
    def _state_or_fallback(cls, stale_reason: str = "") -> dict:
        """拿不到新快照时：**优先返回上次已知良好快照**，绝不用 position=0 顶替。

        为什么不能用 0 顶替：前端进度条读 position，一次 0 会让滑块**跳回开头**
        （用户正在拖拽时观感极差）。宁可给"旧位置 + stale 标记"。
        """
        if cls._state_cache:
            out = dict(cls._state_cache)
            out["stale"] = True
            out["stale_reason"] = stale_reason
            return out
        return {"ready": True, "playing": False, "position": 0, "duration": 0,
                "title": "", "artist": "", "mode": "order", "current": None,
                "roam_on": False, "dj_on": False, "favorited": False,
                "disliked": False, "stale": True, "stale_reason": stale_reason}

    # ────────────────────────────────────────────────────────────
    # ★ 2026-11-18：语义链路诊断（只读缓存，不碰 Qt / 不加载任何模型）
    # ────────────────────────────────────────────────────────────
    def _mood_log(self, key: str, msg: str, *args) -> None:
        """节流日志：同一 key 30s 最多一条。绝不在 1s 节拍里刷屏。"""
        try:
            now = time.monotonic()
            if now - self._mood_log_at.get(key, 0.0) >= 30.0:
                self._mood_log_at[key] = now
                logger.info(msg, *args)
        except Exception:  # noqa 日志失败绝不影响语义层
            pass

    def mood_diag_snapshot(self) -> dict:
        """返回语义链路当前状态（/api/semantics/now 用；纯读，无阻塞）。"""
        try:
            tr = self._mood_track or {}
            return {
                "gate": self._mood_diag.get("gate"),
                "diag": dict(self._mood_diag),
                "track": {"id": tr.get("id"), "source": tr.get("source"),
                          "path": str(tr.get("path") or "")[:80]},
                "pos_ms": int(self._mood_pos or 0),
                "src": self._mood_src,
                "axes_n": self._mood_axes_n,
                "last_axes": dict(self._mood_last) if self._mood_last else None,
                "thread_alive": bool(self._mood_thread and self._mood_thread.is_alive()),
            }
        except Exception as e:  # noqa
            return {"gate": "diag_error", "error": str(e)}

    def get_state(self) -> dict:
        """播放状态快照（★ R119-5a：短超时 + 并发合并 + 上次快照兜底）。

        事故背景：拖进度条 → 界面卡死 → `python313.dll` 访问违例闪退。
        取证显示 `/api/playback/state` 阻塞在 `QtMarshaller.call(timeout=10.0)`。
        三条加固：
          ① 超时 10s → `STATE_TIMEOUT_S`（默认 1.5s）；
          ② **并发合并**：同一时刻只允许一次在途读取，其余直接吃上次快照
             —— 否则每次轮询都排一个 job，只会给已卡的 Qt 线程继续加压；
          ③ 失败返回**上次已知良好快照**（带 `stale`），不是 position=0。
        """
        # ⚠ 降级字典也要带 roam_on / dj_on：前端 dock 按钮读这两个字段，
        #   缺失会显示 undefined（接口契约必须始终一致）。
        if not self.ready:
            return {"ready": False, "playing": False, "position": 0, "duration": 0,
                    "title": "", "artist": "", "mode": "order", "current": None,
                    "roam_on": False, "dj_on": False, "favorited": False,
                    "disliked": False}

        def _fn():
            r = self.radio
            p = getattr(r, "player", None)
            cur = getattr(r, "current", None)
            playing = False
            try:
                # ⚠ R95d-Q0：报「**意图**播放」而不是瞬时引擎状态。
                #   切歌装载窗内 backend 已同步停旧曲（player=STOPPED），真正起播在后台
                #   线程（解析直链/下载/装载 数百 ms~数秒）。旧写法在这段窗口报
                #   playing=false ⇒ 前端 2s 轮询把 false 灌进 st.playing ⇒ 水面 calm
                #   0.28s 冲顶、h 场 0.5s 掉 79%（"水波被集体清掉"）。
                #   电台此刻是"正在准备下一首"，不是暂停 ⇒ 必须报 True。
                #   真暂停/停止时 `_preparing_play` 已复位 ⇒ 仍正确报 False。
                intent = getattr(r, "is_playing_intent", None)
                playing = bool(intent()) if callable(intent) else (p.state.value == "playing")
            except Exception:
                pass
            pos = 0
            try:
                pos = int(p.get_position())
            except Exception:
                pass
            dur = 0
            try:
                dur = int(getattr(p, "_duration_ms", 0) or 0) or \
                      int((getattr(cur, "duration", 0) or 0) * 1000)
            except Exception:
                pass
            # R49：当前曲的喜欢态（dock ♥ 初始点亮）—— 键规则与 radio.fav_key 一致
            fav = False
            try:
                fav = cur is not None and (
                    r.fav_key(cur) in getattr(r, "_queue_fav", set()))
            except Exception:
                fav = False
            # ⚠ R49-27：当前曲的「不喜欢」态 —— get_state 此前不带 disliked，
            # 前端切歌后 dock 不喜欢按钮沿用上一首点亮态（"不喜欢带进下一首"）。
            # 判定与 radio.is_disliked_track 同源（稳定 key 进 disliked_keys）。
            disliked = False
            try:
                disliked = bool(cur is not None and r.is_disliked_track(cur))
            except Exception:
                disliked = False
            return {
                "ready": True,
                "playing": playing,
                "position": pos,
                "duration": dur,
                "title": getattr(cur, "title", "") or "",
                "artist": getattr(cur, "artist", "") or "",
                "mode": getattr(r, "play_mode", "order"),
                # ★ 2026-09-19：模式门控（前端据此置灰 + 高亮"实际生效"的模式）
                "queue_src": getattr(r, "queue_src", None),
                "mode_effective": (r._mode_effective() if hasattr(r, "_mode_effective")
                                   else getattr(r, "play_mode", "order")),
                "mode_locked": (r.mode_locked() if hasattr(r, "mode_locked") else False),
                "current": self.track_to_dict(cur),
                "favorited": bool(fav),
                "disliked": bool(disliked),
                # dock 上 #d-radio / #d-ai 的点亮态以**后端为权威**，
                # 前端初始化与轮询都据此同步，避免"点了没反应/状态错位"。
                "roam_on": bool(getattr(r, "roam_on", False)),
                "dj_on": bool(getattr(r, "dj_on", False)),
                # ★ 2026-09-19：音频 RT 诊断（starve=队列被抽空 / paFlags&2=回调迟到）
                # ★ 2026-09-19：复用 R88 **已有**的 `RadioBridge.audio_diag()`
                #   （→ player.audio_diag()，已被 verify_r88_audio.py 覆盖）。
                #   一个事实一个出口：不再另造 SoftMixer.diag()。
                "audio": self.audio_diag(),
                # ★ R119-3f：runner 回收可观测面（回收过几次 / 为什么跳过）
                "runner": self.runner_recycle_state(),
                # ★ R119-4v：口播质量（正确的 AI 导听 vs 退化兜底 · 转场出声率）
                "dj": self.dj_speech_state(),
            }

        cls = type(self)
        # ② 并发合并：已有在途读取 ⇒ 直接吃上次快照，绝不排队继续加压
        if not cls._state_gate.acquire(blocking=False):
            return cls._state_or_fallback("busy")
        try:
            st = marshaller.call(_fn, timeout=STATE_TIMEOUT_S)   # ① 短超时
            cls._state_cache = st                                # 成功才更新快照
            return st
        except Exception as e:  # noqa
            logger.warning("get_state 失败: %s: %s", type(e).__name__, e)
            return cls._state_or_fallback(f"{type(e).__name__}")  # ③ 旧快照兜底
        finally:
            cls._state_gate.release()

    def toggle_play(self, force_on: bool = False) -> dict:
        """播放/暂停。

        ⚠ 必须走 radio.toggle_play()，不能直接 player.play()：
          冷启动 player 无 _current_path 会静默无操作（"点 ▶ 没声音"），
          且 player.play() 不发 playing 事件、不处理转场定时器。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}

        def _fn():
            r = self.radio
            try:
                playing = r.player.state.value == "playing"
            except Exception:
                playing = False
            if force_on and not getattr(r, "current", None):
                # 冷启动：后台线程拉起（start_radio 自带重入合并）
                threading.Thread(target=self._kick_radio, daemon=True,
                                 name="radio-kick").start()
                return True
            if force_on == playing:
                return True
            r.toggle_play()
            return True

        try:
            ok = marshaller.call(_fn)
            return {"ok": bool(ok)}
        except Exception as e:  # noqa
            logger.warning("toggle_play 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def _kick_radio(self, seed_track=None) -> None:
        """后台线程起播（start_radio 在 LLM 未就绪时会走 refill，同步会卡死 Qt 主线程）。"""
        try:
            self._emit("thinking", True)
            # ★ 2026-09-19：点播（有 seed）/漫游续推（无 seed）都经这里 ⇒ 标来源。
            #   两者都属"AI 编排"来源 ⇒ 播放模式被锁为 order（见 docs/play_mode_gating_plan.md）。
            #   ⚠ 老控制器没有 set_queue_src 时静默跳过 —— 绝不因标记失败而影响起播。
            _ss = getattr(self.radio, "set_queue_src", None)
            if seed_track is not None:
                if _ss:
                    _ss("onDemand")
                self.radio.start_radio(seed_track=seed_track)
            else:
                if _ss:
                    _ss("roam")
                self.radio.start_radio()
        except Exception as e:  # noqa
            logger.warning("start_radio 失败: %s: %s", type(e).__name__, e)
            self._emit("toast", "起播失败：" + str(e))
        finally:
            self._emit("thinking", False)

    def next_track(self) -> dict:
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.next())
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("next_track 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def prev_track(self) -> dict:
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.prev())
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("prev_track 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def seek(self, ms: int) -> dict:
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.seek(int(ms)))
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("seek 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def set_play_mode(self, mode: str) -> dict:
        """播放模式：order（顺序）/ loop_one（单曲循环）/ random（随机）。

        ⚠ 与老 UI 的约定（电台模式）：random 是**先打乱、再进播放列表**，
        不是"播放时才随机挑一首"——因为 AI 口播是按 upcoming[0] 预生成的，
        取歌时随机会导致"旁白与歌曲对不上"。
        真正的打乱发生在填充列表时（见 ai/schedule.py::_maybe_shuffle）。

        R49：**切到 random 时立即打乱剩余待播**。
        旧实现只在"填充列表时"打乱 → 已经按顺序排好的队列永远不重排，
        用户感知就是"随机按钮没效果"。打乱后队首口播的"下一首"已与新顺序失配，
        作废之（置 None），_prep_next_broadcast 会按新顺序异步重生成 ——
        「列表顺序 = 播放顺序」的口播契约不破。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        allowed = ("order", "loop_one", "random")
        if mode not in allowed:
            return {"ok": False, "error": f"模式必须是 {allowed} 之一"}
        try:
            # ★ 2026-09-19：**后端权威门控**。前端可以绕过 ⇒ 必须在后端再判一次；
            #   而且 play_mode 是**持久化**的：用户上次在曲库设的 random 会在下次漫游时
            #   "静默继承"，所以判据必须是"来源"而不是"用户偏好"。
            if _queue_locked(self.radio):
                return {"ok": False, "error": "漫游/推荐下不可切换播放模式（只按顺序播放）",
                        "mode_effective": "order", "mode_locked": True}

            def _fn():
                r = self.radio
                r.set_play_mode(mode)
                if mode == "random" and not r.mode_locked():
                    try:
                        with r._qlock:
                            if len(r.upcoming) > 1:
                                random.shuffle(r.upcoming)
                                if r.upcoming:
                                    r.upcoming[0]["broadcast"] = None
                        r._prep_next_broadcast()
                    except Exception:  # noqa
                        pass
            marshaller.call(_fn)
            self._emit("play_mode", {"mode": mode,
                                     "mode_effective": ("order" if _queue_locked(self.radio) else mode),
                                     "mode_locked": _queue_locked(self.radio)})
            return {"ok": True, "mode": mode,
                    "mode_effective": ("order" if _queue_locked(self.radio) else mode),
                    "mode_locked": _queue_locked(self.radio)}
        except Exception as e:  # noqa
            logger.warning("set_play_mode 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- Phase 3：队列 + 推荐货架 ----------------

    @classmethod
    def _norm_entry(cls, e) -> dict:
        """条目 → 纯 dict（供 JSON 序列化）。

        ⚠ 封面**绝不同步回源**（R27 铁律）：这些接口会被前端轮询调用，
        任何一次网络请求都会冻结整窗。缺封面的由前端程序化占位 + 异步补。
        """
        if isinstance(e, dict):
            d = dict(e)
        else:
            d = {
                "id": getattr(e, "id", None),
                "title": getattr(e, "title", "未知"),
                "artist": getattr(e, "artist", "未知"),
                "album": getattr(e, "album", ""),
                "source": getattr(e, "source", "local"),
                "ext_id": getattr(e, "ext_id", None),
                "cover": cls._cover_url(getattr(e, "cover", "")),
                "reason": getattr(e, "reason", "") or "",
            }
            trk = getattr(e, "track", None)
            if trk is not None:
                d.setdefault("title", getattr(trk, "title", d["title"]))
                d.setdefault("artist", getattr(trk, "artist", d["artist"]))
                d.setdefault("cover", cls._cover_url(getattr(trk, "cover", "") or d["cover"]))
        d.setdefault("current", False)
        d.setdefault("favorited", False)
        d.setdefault("disliked", False)
        return d

    def get_queue(self) -> dict:
        """播放队列（当前曲 + 待播 upcoming），不兜底推荐货架。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "entries": []}
        try:
            entries = marshaller.call(
                lambda: [self._norm_entry(e)
                         for e in (self.radio.get_playlist_entries() or [])])
            return {"ok": True, "entries": self._mark_fav(entries)}
        except Exception as e:  # noqa
            logger.warning("get_queue 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}

    def _has_online_login(self) -> bool:
        """是否存在已登录的在线音源（用于门控歌单/推荐：未登录不加载）。

        QQ音乐：config sources.qqmusic.cookie 非空（DPAPI 密文，长度足够判断）。
        第三方音源插件：隔离目录 cookie.txt 含登录条目（插件由用户自行添加）。
        """
        try:
            from config.settings import user_config
            d = user_config.data or {}
        except Exception:  # noqa
            d = {}
        try:
            qq = (d.get("sources") or {}).get("qqmusic") or {}
            if str(qq.get("cookie") or "").strip():
                return True
        except Exception:  # noqa
            pass
        try:
            base = Path(__file__).resolve().parents[1] / "data_runtime" / "nembox" / "data" / "netease-musicbox"
            ck = base / "cookie.txt"
            if ck.exists() and "MUSIC_U" in ck.read_text(encoding="utf-8", errors="ignore"):
                return True
        except Exception:  # noqa
            pass
        return False

    def get_recommendations(self) -> dict:
        """推荐页（漫游列表）—— **始终推荐货架，绝不变成播放队列**。

        ⚠ 踩坑记录（用户明确指出"推荐页和播放列表栏搞混了"）：
          旧逻辑是 `get_playlist_entries()`（队列）优先、队列空才回退货架，
          结果**一旦开播，推荐页就被播放队列顶替** —— 用户播放曲库时，
          推荐页里出现的是播放列表，而不是推荐内容。
        → 正确做法：推荐页**固定**读 `get_reco_entries()`（纯推荐货架）；
          播放队列走独立入口 `/api/queue`，两者互不干扰。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "entries": []}
        if not self._has_online_login():
            return {"ok": True, "entries": [], "note": "未登录在线音源，推荐不加载"}

        def _fn():
            entries = self.radio.get_reco_entries() or []
            return [self._norm_entry(e) for e in entries]

        try:
            return {"ok": True, "entries": marshaller.call(_fn)}
        except Exception as e:  # noqa
            logger.warning("get_recommendations 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}

    def get_reco_entries(self) -> dict:
        """推荐货架（与播放队列**复制共存**，不是同一份数据）。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "entries": []}
        try:
            entries = marshaller.call(
                lambda: [self._norm_entry(e)
                         for e in (self.radio.get_reco_entries() or [])])
            return {"ok": True, "entries": self._mark_fav(entries)}
        except Exception as e:  # noqa
            logger.warning("get_reco_entries 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}

    def play_reco(self, track_id) -> dict:
        """点推荐/队列里的某一首 → **按身份**起播。

        ⚠ 绝不能用下标 jumpTo：前端列表下标 ≠ 后端队列下标，且 reco 与队列是
        「复制共存」，用下标必然"点 A 播 B"（老架构反复踩的坑）。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.play_from_reco(track_id))
            return {"ok": True, "track_id": track_id}
        except Exception as e:  # noqa
            logger.warning("play_reco 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def play_track_by_id(self, track_id) -> dict:
        """按数据库 id 直接起播（沿用原 bridge_host L1660 playTrackById 的完整逻辑）。

        ⚠ 踩坑记录：**不存在** `radio.play_track_by_id` 方法（曾直接调用 →
          AttributeError，表现为"点卡片播放没反应"）。正确路径是两步：
            1) 优先在**当前队列**按 id 命中 → `jump_to(idx)`（快，保留队列上下文）
            2) 未命中 → 回库查 Track（`library.by_id` → `db.get_track` 双通道兜底）
               → **后台线程** `start_radio(seed_track=...)`（避免卡死 Qt 主线程）
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            tid = int(track_id)
        except Exception:  # noqa
            return {"ok": False, "error": "track_id 必须是整数"}

        # 1) 优先在当前队列命中 → jump_to
        try:
            def _hit():
                entries = self.radio.get_playlist_entries() or []
                ids = []
                for e in entries:
                    try:
                        ids.append(int(e.get("id") if isinstance(e, dict)
                                       else getattr(e, "id", -1) or -1))
                    except Exception:  # noqa
                        ids.append(-1)
                return ids.index(tid) if tid in ids else -1

            idx = marshaller.call(_hit)
            if idx >= 0:
                marshaller.call(lambda: self.radio.jump_to(idx))
                return {"ok": True, "via": "jump_to", "index": idx}
        except Exception as e:  # noqa
            logger.warning("play_track_by_id 队列命中失败: %s: %s",
                           type(e).__name__, e)

        # 2) 回库查 Track（library.by_id → db.get_track 双通道）
        def _find():
            r = self.radio
            t = None
            try:
                t = r.library.by_id(tid)
            except Exception:  # noqa
                t = None
            if t is None:
                try:
                    row = r.db.get_track(tid)
                    # ⚠ R49-25：db.get_track 返回 sqlite3.Row，直接当 seed 传给
                    # start_radio 会抛 "'sqlite3.Row' object has no attribute 'id'"。
                    # 必须经 Track.from_row 统一转成 Track（library.by_id 已是 Track）。
                    if row is not None:
                        from data.library import Track
                        t = Track.from_row(row)
                except Exception:  # noqa
                    t = None
            return t

        try:
            tr = marshaller.call(_find)
        except Exception as e:  # noqa
            logger.warning("play_track_by_id 回库查询失败: %s: %s",
                           type(e).__name__, e)
            return {"ok": False, "error": str(e)}

        if tr is None:
            self._emit("toast", "未找到该曲目（可能已被删除）")
            return {"ok": False, "error": "未找到该曲目"}

        # 3) 后台线程起播
        threading.Thread(target=self._kick_radio, args=(tr,),
                         daemon=True, name="play-track-by-id").start()
        return {"ok": True, "via": "start_radio", "track_id": tid}

    def get_lyrics(self, track_id=None) -> dict:
        """取歌词（已解析的 [{time, text}]）。track_id 为空 = 当前曲。

        ⚠ 网络调用（sm.lyric）**绝不能**经 marshaller 编排到 Qt 主线程
          （取词要回源，实测数百 ms～数秒）。本方法整体在 FastAPI 线程池执行，
          只有读 `radio.current` 这一步需要回主线程。

        ⚠ 接口名坑（老 UI L2447-2485 踩过）：**不存在** `sm.get_lyrics`，
          真实接口是 `source_manager.lyric(source, ext_id)`。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "lines": []}
        try:
            from core.lyrics import parse_lrc
        except Exception as e:  # noqa
            logger.warning("parse_lrc 导入失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": "歌词解析模块不可用", "lines": []}

        # 1) 读当前曲（改播放状态相关 → 回 Qt 主线程）
        def _cur():
            try:
                return self.radio.current
            except Exception:  # noqa
                return None

        t = None
        if track_id in (None, "", 0):
            try:
                t = marshaller.call(_cur)
            except Exception as e:  # noqa
                logger.warning("get_lyrics 读当前曲失败: %s: %s", type(e).__name__, e)
                return {"ok": False, "error": str(e), "lines": []}
        else:
            def _find():
                r = self.radio
                try:
                    tt = r.library.by_id(int(track_id))
                except Exception:  # noqa
                    tt = None
                if tt is None:
                    try:
                        tt = r.db.get_track(int(track_id))
                    except Exception:  # noqa
                        tt = None
                return tt

            try:
                t = marshaller.call(_find)
            except Exception as e:  # noqa
                logger.warning("get_lyrics 回库查询失败: %s: %s", type(e).__name__, e)
        if t is None:
            return {"ok": False, "error": "未找到该曲目", "lines": []}

        src = getattr(t, "source", "") or ""
        ext = getattr(t, "ext_id", "") or ""
        title = getattr(t, "title", "") or ""
        if not src or src == "local" or not ext:
            return {"ok": False, "error": "本地/无音源曲目，无在线歌词",
                    "lines": [], "title": title}

        # 2) 网络取词 —— 留在本线程
        try:
            from core.lyrics import coerce_lrc_text
        except Exception:  # noqa
            coerce_lrc_text = None
        sm = getattr(self.radio, "source_manager", None) or \
            getattr(self.radio, "_source_manager", None)
        if sm is None or not hasattr(sm, "lyric"):
            return {"ok": False, "error": "音源管理器不可用", "lines": [], "title": title}
        try:
            raw = sm.lyric(src, ext)
        except Exception as e:  # noqa
            logger.warning("取歌词失败(%s/%s): %s: %s", src, ext, type(e).__name__, e)
            return {"ok": False, "error": str(e), "lines": [], "title": title}

        if coerce_lrc_text is not None:
            try:
                lrc_text = coerce_lrc_text(raw) or ""
            except Exception as e:  # noqa
                logger.warning("歌词形态归一失败(%s/%s): %s: %s",
                               src, ext, type(e).__name__, e)
                lrc_text = ""
        else:
            lrc_text = raw if isinstance(raw, str) else ""
        if not lrc_text.strip():
            return {"ok": False, "error": "无歌词", "lines": [], "title": title}
        try:
            lines = [{"time": ts, "text": txt} for ts, txt in parse_lrc(lrc_text)]
        except Exception as e:  # noqa
            logger.warning("歌词解析失败(%s/%s): %s: %s", src, ext, type(e).__name__, e)
            return {"ok": False, "error": str(e), "lines": [], "title": title}
        return {"ok": True, "lines": lines, "title": title, "source": src}

    def queue_play_next(self, track_id) -> dict:
        """插到待播首位（原 UI 右键「下一首播放」）。

        ⚠ R44 前端曾把「下一首播放」接到 /api/playback/next —— 那是**切到下一首**
          （跳过当前曲），语义完全相反。插入待播要走 radio.play_next(track)。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            tid = int(track_id)
        except Exception:  # noqa
            return {"ok": False, "error": "track_id 必须是整数"}

        def _find():
            r = self.radio
            t = None
            try:
                t = r.library.by_id(tid)
            except Exception:  # noqa
                t = None
            if t is None:
                try:
                    t = r.db.get_track(tid)
                except Exception:  # noqa
                    t = None
            return t

        try:
            tr = marshaller.call(_find)
        except Exception as e:  # noqa
            logger.warning("queue_play_next 回库查询失败: %s: %s",
                           type(e).__name__, e)
            return {"ok": False, "error": str(e)}
        if tr is None:
            return {"ok": False, "error": "未找到该曲目"}
        try:
            marshaller.call(lambda: self.radio.play_next(tr))
        except Exception as e:  # noqa
            logger.warning("queue_play_next 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def remove_reco(self, track_id) -> dict:
        """从推荐货架移除；R55：同步从播放队列/当前曲清除（点叉=不播这首）。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.remove_from_reco(int(track_id)))
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("remove_reco 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def remove_from_queue(self, track_id) -> dict:
        """从播放队列移除。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.remove_from_queue(track_id))
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("remove_from_queue 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def play_all_reco(self) -> dict:
        """推荐「播放全部」：**替换**播放队列，整批推荐进队列并从第一首开播。

        ⚠ 与曲库的 play_all_library 是**两个不同入口**（R46-5 用户点名别弄混）：
           推荐 → radio.play_all_reco()     数据源 = reco_buffer（推荐货架）
           曲库 → radio.play_all_library()  数据源 = library（本地曲库）
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.play_all_reco())
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("play_all_reco 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def play_all_library(self) -> dict:
        """曲库「播放全部」：**替换**播放队列，整批曲库进队列并从第一首开播。

        与推荐版同源同理但数据源不同（library），后端 play_all_library 内部
        会 `upcoming = picks[1:]` + `_manual_queue = True`，即**替换**而非追加。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.play_all_library())
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("play_all_library 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- Phase 4：收藏 + 历史 + 曲库 ----------------

    @classmethod
    def _norm_track(cls, t) -> dict:
        """Track 对象 / dict → 纯 dict。

        ⚠ 封面**绝不同步回源**（R27 铁律）：这些接口会被前端轮询，
        任何网络请求都会冻结整窗。缺封面的由前端程序化占位。
        """
        if isinstance(t, dict):
            return {
                "id": t.get("id"),
                "title": t.get("title") or "未知标题",
                "artist": t.get("artist") or "未知歌手",
                "album": t.get("album") or "",
                "source": t.get("source") or "local",
                "ext_id": t.get("ext_id"),
                "cover": cls._cover_url(t.get("cover")),
                "duration": t.get("duration") or 0,
            }
        return {
            "id": getattr(t, "id", None),
            "title": getattr(t, "title", "") or "未知标题",
            "artist": getattr(t, "artist", "") or "未知歌手",
            "album": getattr(t, "album", "") or "",
            "source": getattr(t, "source", "") or "local",
            "ext_id": getattr(t, "ext_id", None),
            "cover": cls._cover_url(getattr(t, "cover", "")),
            "duration": getattr(t, "duration", 0) or 0,
        }

    def get_history(self) -> dict:
        """播放历史。

        ⚠ 曾在切除死代码时被**误删**（按行号切 734-795，而本方法正落在 759-770）。
          是 E65（server.py 引用的 bridge.X 必须真实存在）把它抓回来的 ——
          **按行号批量删除前，必须先确认区间内没有活代码**。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "entries": []}
        try:
            entries = marshaller.call(
                lambda: [self._norm_track(t)
                         for t in (self.radio.get_history() or [])])
            # R49-29：历史里正在播放的那首同样标记 current
            return {"ok": True, "entries": self._mark_current(self._mark_fav(entries))}
        except Exception as e:  # noqa
            logger.warning("get_history 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}

    def remove_history(self, track_id) -> dict:
        """从播放历史移除。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.remove_from_history(int(track_id)))
            self._emit("history_changed", {"track_id": track_id})
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("remove_history 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- 不喜欢（dislike）----------------
    # ⚠ 不喜欢**不是**收藏的反面，它是独立能力：
    #   加入后 AI 会避雷，并从待播队列 / 推荐货架剔除。
    #   key 形如 'i:123'（本地库 id）或 'e:qq:xxx'（在线 source:ext_id）。

    def get_library(self) -> dict:
        """曲库（本地音乐库）。

        ⚠ 与「收藏(favorites)」是**两个不同东西**（曾误删）：
           · 曲库 = 本地导入的全部音乐，原 UI 侧边面板的独立 tab（历史/曲库/队列）
           · 收藏 = 我喜欢的音乐（红心），已按用户要求作为废弃功能整条移除
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "entries": []}
        try:
            entries = marshaller.call(
                lambda: [self._norm_track(t)
                         for t in (self.radio.get_library() or [])])
            # R49-29：标出正在播放的那首（current）→ 前端高亮 + 自动定位
            return {"ok": True, "entries": self._mark_current(self._mark_fav(entries))}
        except Exception as e:  # noqa
            logger.warning("get_library 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}

    def import_library_folder(self, folder: str) -> dict:
        """R62 #9：导入本地文件夹到曲库（设置卡「导入文件夹」→ POST）。

        经 marshaller 回 Qt 主线程调 radio.import_library_folder —— 扫描
        音频文件 + 提取元数据 + 入库 + reload。返回新增条数。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        folder = str(folder or "").strip()
        if not folder:
            return {"ok": False, "error": "文件夹路径不能为空"}
        try:
            n = marshaller.call(lambda: self.radio.import_library_folder(folder))
            self._emit("library_changed", {})
            return {"ok": True, "added": int(n or 0), "folder": folder}
        except Exception as e:  # noqa
            logger.warning("import_library_folder 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def scan_library_roots(self) -> dict:
        """R62 #9：扫描配置的全部曲库根目录（MUSIC_LIBRARY_ROOTS）。返回新增条数。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            n = marshaller.call(lambda: self.radio.scan_library_roots())
            self._emit("library_changed", {})
            return {"ok": True, "added": int(n or 0)}
        except Exception as e:  # noqa
            logger.warning("scan_library_roots 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def delete_from_library(self, track_id) -> dict:
        """从曲库删除一首（侧边面板曲库行 del 按钮 → POST /api/library/delete）。

        ⚠ 这个方法的 def 头曾在某次编辑中丢失，函数体变成上一個方法 return 之后的
          **不可达死代码**；而前端一直在请求 /api/library/delete（404 被 .catch 吞掉），
          于是"曲库删除点了没反应"。现补回实体方法并注册路由。
        ⚠ 删库会改动播放状态 → 必须经 marshaller 回到 Qt 主线程。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.delete_from_library(int(track_id)))
            self._emit("library_changed", {"track_id": track_id})
            return {"ok": True}
        except Exception as e:  # noqa
            logger.warning("delete_from_library 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- 歌单（plist 栏目 · QQ音乐/网易云） ----------------
    # ⚠ 与「曲库(library)」是两个东西，别再接混（R45 用户纠正）：
    #     · plist  = 在线歌单（QQ音乐 / 网易云的榜单、歌单），数据源 = PlaylistManager
    #     · library= 本地曲库（导入到本地的音乐），数据源 = radio.get_library()
    # 网络请求**绝不能**经 marshaller 编排到 Qt 主线程
    # （老架构实测：netease 584ms + qqmusic 重试 ≈2.6s 整窗冻结，见 R26）。
    # FastAPI 路由本就跑在线程池，直接在调用线程做网络即可。

    _PL_CACHE_TTL = 300.0          # discover（平台推荐）TTL，二次请求 0ms
    _PL_FAIL_COOLDOWN = 60.0       # 失败音源短路 60s，不反复白等

    def _pm_get(self):
        """惰性构造并缓存 PlaylistManager（构造开销在 SourceManager）。"""
        pm = getattr(self, "_pm", None)
        if pm is not None:
            return pm
        try:
            from data.sources import SourceManager
            from data.playlist_manager import PlaylistManager
            from config.settings import user_config

            db = getattr(getattr(self, "radio", None), "db", None)
            sm = SourceManager(user_config.data)
            pm = PlaylistManager(sm, db)
            self._pm = pm
            return pm
        except Exception as e:  # noqa
            logger.warning("PlaylistManager 初始化失败: %s: %s", type(e).__name__, e)
            self._pm = None
            return None

    @classmethod
    def _pl_meta_to_dict(cls, m) -> dict:
        """PlaylistMeta / dict → 前端歌单卡片字段。"""
        try:
            d = m if isinstance(m, dict) else m.__dict__
            return {
                "platform": d.get("platform") or "",
                "ext_id": str(d.get("ext_id") or ""),
                "name": d.get("name") or "未命名歌单",
                "cover": cls._cover_url(d.get("cover")),
                "creator": d.get("creator") or "",
                "track_count": d.get("track_count") or 0,
                "description": d.get("description") or "",
            }
        except Exception:  # noqa
            return {}

    def _pl_fetch_parallel(self, plats, fn, timeout: float = 6.0) -> list:
        """多���源**并行**拉取，任一源先返回即采用，慢源不阻塞。

        ⚠ 串行 = 各源耗时累加（netease 差网拖满超时后才轮到 qqmusic，
        「歌单疯狂加载」的根因，见 R27/R28）；并行 = 最慢源。
        """
        if not plats:
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        fail_until = getattr(self, "_pl_fail_until", None)
        if fail_until is None:
            fail_until = self._pl_fail_until = {}
        ex = ThreadPoolExecutor(max_workers=len(plats))
        futs = {}
        by_source: dict[str, list] = {}
        try:
            for p in plats:
                futs[ex.submit(fn, p)] = p
            # 等所有已启用来源各自返回（不是第一个就采用），以聚合多来源。
            for fut in as_completed(futs, timeout=timeout):
                p = futs[fut]
                try:
                    got = fut.result() or []
                    if got:
                        by_source[p] = list(got)
                except Exception as e:  # noqa
                    logger.warning("歌单拉取 %s 失败: %s: %s", p, type(e).__name__, e)
                    fail_until[p] = time.monotonic() + self._PL_FAIL_COOLDOWN
        except Exception as e:  # noqa  as_completed 超时
            logger.warning("歌单并行拉取超时: %s: %s", type(e).__name__, e)
        finally:
            ex.shutdown(wait=False)
        # 按来源交错 + (platform, ext_id) 去重，避免单一来源霸榜 / 重复。
        merged: list = []
        seen: set = set()
        keys = list(by_source.keys())
        max_len = max((len(v) for v in by_source.values()), default=0)
        for i in range(max_len):
            for p in keys:
                arr = by_source[p]
                if i >= len(arr):
                    continue
                m = arr[i]
                k = (getattr(m, "platform", p), getattr(m, "ext_id", None))
                if k in seen:
                    continue
                seen.add(k)
                merged.append(m)
        return merged


    def _enabled_remote_source_keys(self) -> list:
        """当前已启用、可搜索的远程音源 key（歌单货架按此聚合，含第三方插件）。"""
        try:
            from data.sources import SourceManager
            from config.settings import user_config as _uc
            sm = SourceManager(_uc.data)
            return [getattr(s, "key", "") for s in sm.remote_sources()
                    if getattr(s, "key", "")
                    and getattr(s, "enabled", False)
                    and getattr(s, "searchable", True) is not False]
        except Exception as e:  # noqa: BLE001
            logger.warning("_enabled_remote_source_keys 失败: %s", e)
            return []

    def get_playlists(self, group: str = "discover", offset: int = 0,
                      limit: int = 30, source: str = "") -> dict:
        """歌单栏目一页。

        group ∈ discover|mine|saved|local；source ∈ ''（全部）|netease|qqmusic。
        ⚠ 本地歌单(local)无音源概念 → source 恒忽略。
        ⚠ discover 走 TTL 缓存；mine/saved/local 恒重拉（用户数据刚变要立即可见）。
        """
        g = str(group or "discover").strip()
        if g not in ("discover", "mine", "saved", "local"):
            g = "discover"
        if g in ("discover", "mine") and not self._has_online_login():
            return {"ok": True, "items": [], "group": g, "offset": 0, "limit": 0,
                    "note": "未登录在线音源，歌单不加载"}
        src = str(source or "").strip()
        if src not in ("netease", "qqmusic"):
            src = ""
        try:
            off, lim = max(0, int(offset or 0)), max(1, min(60, int(limit or 30)))
        except Exception:  # noqa
            off, lim = 0, 30

        pm = self._pm_get()
        if pm is None:
            return {"ok": False, "error": "歌单管理器不可用", "items": [],
                    "group": g, "offset": off, "limit": lim}

        ck = f"{g}:{src}" if src else g
        cache = getattr(self, "_pl_cache", None)
        if cache is None:
            cache = self._pl_cache = {}
        exhausted = getattr(self, "_pl_exhausted", None)
        if exhausted is None:
            exhausted = self._pl_exhausted = {}
        fail_until = getattr(self, "_pl_fail_until", None)
        if fail_until is None:
            fail_until = self._pl_fail_until = {}

        def _slice(items):
            return {"ok": True, "items": items[off:off + lim],
                    "group": g, "offset": off, "limit": lim, "source": src,
                    "total": len(items)}

        # discover：TTL 缓存命中直接返回（零网络）
        hit = cache.get(ck)
        if g == "discover" and hit and (time.monotonic() - hit[0]) < self._PL_CACHE_TTL:
            return _slice(hit[1])

        try:
            if g == "discover":
                want = min(100, max(60, off + lim))
                # ⚠「已取尽」封顶：某音源可用量可能 < want（实测 qqmusic 上限 40），
                #   旧判断 `len(cache) < want` 永远成立 → 每次翻页都回源 =「疯狂加载」。
                if not (hit and (len(hit[1]) >= want or exhausted.get(ck))):
                    plats = [src] if src else [
                        p for p in self._enabled_remote_source_keys()
                        if time.monotonic() >= fail_until.get(p, 0)]
                    raw = self._pl_fetch_parallel(
                        plats, lambda p: pm.discover(p, limit=want))
                    if raw:
                        exhausted[ck] = len(raw) < want
                        merged = list((hit or (0, []))[1])
                        seen = {(m.get("platform"), m.get("ext_id")) for m in merged}
                        for m in raw:
                            d = self._pl_meta_to_dict(m)
                            k = (d.get("platform"), d.get("ext_id"))
                            if k not in seen:
                                seen.add(k)
                                merged.append(d)
                        cache[ck] = (time.monotonic(), merged)
                return _slice((cache.get(ck) or (0, []))[1])

            # mine / saved / local：用户数据，恒重拉
            if g == "mine":
                plats = [src] if src else self._enabled_remote_source_keys()
                raw = self._pl_fetch_parallel(plats, lambda p: pm.my_playlists(p))
                items = [self._pl_meta_to_dict(m) for m in raw]
            elif g == "saved":
                raw = pm.saved_playlists(src or None) or []
                items = [self._pl_meta_to_dict(m) for m in raw]
            else:
                raw = pm.local_playlists() or []
                items = [self._pl_meta_to_dict(m) for m in raw]
            cache[ck] = (time.monotonic(), items)
            return _slice(items)
        except Exception as e:  # noqa
            logger.warning("get_playlists(%s/%s) 失败: %s: %s",
                           g, src, type(e).__name__, e)
            return _slice((cache.get(ck) or (0, []))[1])

    def get_playlist_tracks(self, platform: str, ext_id: str) -> dict:
        """歌单详情曲目。"""
        pm = self._pm_get()
        if pm is None:
            return {"ok": False, "error": "歌单管理器不可用", "entries": []}
        plat = str(platform or "").strip()
        pid = str(ext_id or "").strip()
        if not plat or not pid:
            return {"ok": False, "error": "platform/ext_id 不能为空", "entries": []}
        try:
            raw = pm.tracks(plat, pid) or []
            # 尽量带出封面：音源层 search/playlist_detail 常丢 picUrl（见 R27）
            entries = []
            for t in raw:
                try:
                    d = t if isinstance(t, dict) else t.__dict__
                except Exception:  # noqa
                    d = {}
                entries.append({
                    "id": d.get("id") or f"{plat}:{pid}:{len(entries)}",
                    "title": d.get("title") or d.get("name") or "未知曲目",
                    "artist": d.get("artist") or "",
                    "album": d.get("album") or "",
                    "source": plat,
                    "ext_id": str(d.get("ext_id") or ""),
                    "cover": self._cover_url(d.get("cover")),
                    "duration": d.get("duration") or 0,
                })
            return {"ok": True, "entries": entries,
                    "platform": plat, "ext_id": pid}
        except Exception as e:  # noqa
            logger.warning("get_playlist_tracks(%s/%s) 失败: %s: %s",
                           plat, pid, type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}

    def _pl_tracks_raw(self, platform: str, ext_id) -> list:
        """原始 Track 对象列表（可供 play_tracks 直接消费）。

        ⚠ 方法名坑（老 UI L1341-1366 踩过）：PlaylistManager 上**没有**
          playlist_tracks 方法，真实方法名是 `tracks(platform, ext_id)`。
          本地歌单（platform='local'）曲目在 playlists/playlist_items 表里，
          走 db.playlist_tracks(int(ext_id))，签名完全不同。
        """
        if ext_id is None or str(ext_id) == "":
            return []
        plat = str(platform or "").strip()
        if plat == "local":
            try:
                db = getattr(getattr(self, "radio", None), "db", None)
                return list(db.playlist_tracks(int(ext_id)) or [])
            except Exception as e:  # noqa
                logger.warning("取本地歌单曲目失败(id=%s): %s: %s",
                               ext_id, type(e).__name__, e)
                return []
        pm = self._pm_get()
        if pm is None:
            return []
        try:
            return list(pm.tracks(plat, ext_id) or [])
        except Exception as e:  # noqa
            logger.warning("取 %s 歌单曲目失败(id=%s): %s: %s",
                           plat, ext_id, type(e).__name__, e)
            return []

    def play_playlist(self, platform: str, ext_id: str, index: int = 0) -> dict:
        """整单入队播放（可选跳到第 index 首）—— 老 UI playPlaylist + jumpTo 合并。

        ⚠ **下标错位坑**（老 UI L5657-5664 实测）：schedule.play_tracks 内部
            current = picks[0]，upcoming = picks[1:]。
            要播 picks[gi]（0-based）：gi=0 不用跳；gi>=1 → jump_to(gi - 1)。
            若直接 jump_to(gi) 会整体错一位 —— 表现为「点第 2 首实际播第 3 首」。

        线程纪律：
            · pm.tracks() 走**网络** → 在本线程（FastAPI 线程池）完成，绝不 marshaller
            · play_tracks / jump_to 改**播放状态** → 必须回 Qt 主线程
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        plat = str(platform or "").strip()
        pid = str(ext_id or "").strip()
        if not plat or not pid:
            return {"ok": False, "error": "platform/ext_id 不能为空"}
        try:
            gi = max(0, int(index or 0))
        except Exception:  # noqa
            gi = 0

        raw = self._pl_tracks_raw(plat, pid)
        if not raw:
            return {"ok": False, "error": "该歌单暂无曲目", "entries": []}

        try:
            marshaller.call(lambda: self.radio.play_tracks(raw, "歌单播放"))
        except Exception as e:  # noqa
            logger.warning("play_playlist 入队失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

        jumped = -1
        if gi > 0 and gi < len(raw):
            jumped = gi - 1

            def _jump():
                try:
                    self.radio.jump_to(jumped)
                except Exception as e:  # noqa
                    logger.warning("歌单跳转失败: %s: %s", type(e).__name__, e)

            try:
                marshaller.call(_jump)
            except Exception as e:  # noqa
                logger.warning("歌单跳转编排失败: %s: %s", type(e).__name__, e)
        return {"ok": True, "count": len(raw), "index": gi, "jumped": jumped}

    def list_dislikes(self) -> dict:
        """不喜欢列表 [{key,title,artist}]。

        R67-4：去掉 `x.get("title") or "未知标题"` 兜底 —— 这是 2025 R62 留下的
        坏味道：API 把 NULL 强行变成 "未知标题"，前端 `it.title || '未知歌曲'`
        兜底永远不触发 → 所有 NULL 行都显示"未知"，无法分辨"真没名字"和"前端
        漏渲染"。现在 API 原样回 title/artist（None 或空串），前端用
        (it.title || '未知歌曲') / (it.artist || '未知歌手') 统一兜底。
        i: 本地曲追加 db 反查回填 —— 本地曲一定有歌名，不应该显示"未知"。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "entries": []}

        def _fn():
            r = self.radio
            rows = r.get_disliked_tracks() or []
            out = []
            for x in rows:
                k = x.get("key", "")
                t = x.get("title"); a = x.get("artist")
                # i: 本地曲回填（本地库一定存了 title/artist，dislike 落库时漏了也能找回）
                if (not t or not a) and k.startswith("i:"):
                    try:
                        tid = int(k.split(":", 1)[1])
                        tr = r.db.get_track(tid)
                        if tr:
                            t = t or tr.get("title"); a = a or tr.get("artist")
                    except Exception:  # noqa
                        pass
                out.append({"key": k, "title": t, "artist": a})
            return out

        try:
            return {"ok": True, "entries": marshaller.call(_fn)}
        except Exception as e:  # noqa
            logger.warning("list_dislikes 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}

    def add_dislike(self, key: str, title: str | None = None,
                    artist: str | None = None) -> dict:
        """加入不喜欢（AI 避雷 + 从待播/推荐货架剔除）。

        ⚠ R62 #23：在线歌（e:…）没有本地 db id，radio.dislike_by_key 落库时
        拿不到 Track 元数据 → title/artist=None → 不喜欢库显示「未知歌曲」。
        前端点 👎 时手上就有完整条目，应把 title/artist 一并传过来透传落库。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "error": "key 不能为空"}
        try:
            def _fn():
                tid = None
                if key.startswith("i:"):
                    try:
                        tid = int(key.split(":", 1)[1])
                    except Exception:  # noqa
                        tid = None
                if tid is not None:
                    self.radio.dislike_by_id(tid, True)
                    return True
                # 在线曲目：按 key 走 radio（⚠ R50：内存 disliked_keys + db 持久化 +
                # 从待播/推荐货架剔除）。旧实现只写 db —— is_disliked_track 判定的是
                # 内存集合，get_state 恒报 false → 前端点亮被 2s 轮询打回。
                # ⚠ R62 #23：透传 title/artist → 落库不再「未知歌曲/未知歌手」。
                self.radio.dislike_by_key(key, True, title=title, artist=artist)
                return True

            marshaller.call(_fn)
            self._emit("dislike", {"key": key, "on": True})
            self._emit("toast", "已加入「不喜欢的音乐」（AI 将避开）")
            return {"ok": True, "key": key}
        except Exception as e:  # noqa
            logger.warning("add_dislike 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def remove_dislike(self, key: str) -> dict:
        """移出不喜欢。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "error": "key 不能为空"}
        try:
            def _fn():
                if key.startswith("i:"):
                    try:
                        self.radio.dislike_by_id(int(key.split(":", 1)[1]), False)
                        return True
                    except Exception:  # noqa
                        pass
                # 在线 key：撤销也要走 radio（内存 disliked_keys.discard，
                # 与 add 侧 dislike_by_key 对称）——旧实现只写 db。
                self.radio.dislike_by_key(key, False)
                return True

            marshaller.call(_fn)
            self._emit("dislike", {"key": key, "on": False})
            self._emit("toast", "已移出「不喜欢的音乐」")
            return {"ok": True, "key": key}
        except Exception as e:  # noqa
            logger.warning("remove_dislike 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def toggle_radio(self, on: bool) -> dict:
        """电台模式（#d-radio）：开 = 立即补满队列并续推（不是仅置标志位）。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.set_roam_on(bool(on)))
            self._emit("radio_mode", bool(on))
            return {"ok": True, "on": bool(on)}
        except Exception as e:  # noqa
            logger.warning("toggle_radio 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def toggle_dj(self, on: bool) -> dict:
        """AI 口播（#d-ai）：开 = 立即预取下一首口播。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            marshaller.call(lambda: self.radio.set_dj_on(bool(on)))
            self._emit("dj_mode", bool(on))
            return {"ok": True, "on": bool(on)}
        except Exception as e:  # noqa
            logger.warning("toggle_dj 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def set_broadcast_language(self, language: str) -> dict:
        """★ 2026-11-19：切换 AI 口播语言（zh/en）。

        口播的 LLM prompt、TTS 语种、英文 12 秒字数口径三者由
        Radio.set_broadcast_language() 一处切换；本方法只把它搬到 Qt 主线程。
        已生成/正在播的当前口播不打断；下一首及待播预取按新语言重建。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        lang = "en" if str(language or "zh").lower().startswith("en") else "zh"
        try:
            marshaller.call(lambda: self.radio.set_broadcast_language(lang))
            self._emit("broadcast_language", lang)
            return {"ok": True, "language": lang}
        except Exception as e:  # noqa
            logger.warning("set_broadcast_language 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def get_broadcast(self) -> dict:
        """获取当前曲的 AI 口播文案（{text, ready}）。"""
        if not self.ready:
            return {"ok": False, "error": "radio 未接入", "text": "", "ready": False}

        def _fn():
            bc = getattr(self.radio, "current_broadcast", None)
            if bc and isinstance(bc, dict):
                return {"text": bc.get("text", ""), "ready": True}
            return {"text": "", "ready": False}

        try:
            d = marshaller.call(_fn)
            return {"ok": True, **d}
        except Exception as e:  # noqa
            logger.warning("get_broadcast 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "text": "", "ready": False}

    def set_favorite(self, on) -> dict:
        """收藏 / 取消收藏当前曲（POST /api/playback/favorite）。

        ⚠ 这个 `def` 头曾在某次编辑中丢失，函数体变成 `get_broadcast` 之后的
          **不可达死代码** —— 注意它跟在一个「所有分支都 return」的 try 后面，
          所以「return 之后还有语句」那种简单扫描**抓不到**（E56 的漏网点）。
          而路由一直在调 `bridge.set_favorite` → AttributeError 500。现补回。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            def _fn():
                r = self.radio
                cur = getattr(r, "current", None)
                if cur is None:
                    return None
                try:
                    is_fav = bool(r.fav_key(cur) in getattr(r, "_queue_fav", set()))
                except Exception:  # noqa
                    is_fav = False
                if bool(on) == is_fav:
                    return is_fav
                return bool(r.toggle_favorite(cur))

            result = marshaller.call(_fn)
            if result is None:
                return {"ok": False, "error": "无当前曲"}
            self._emit("favorite", bool(result))
            return {"ok": True, "favorited": bool(result)}
        except Exception as e:  # noqa
            logger.warning("set_favorite 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def toggle_favorite_track(self, track_id=None, source=None, ext_id=None,
                              title="", artist="", album="", cover="") -> dict:
        """喜欢 / 取消喜欢**指定**曲目（卡片 ♥ 按钮，R49 恢复老 UI 能力）。

        键规则与 radio.fav_key 一致：在线曲 e:{src}:{ext_id}、本地曲 i:{db_id}。
        radio.toggle_favorite 接受带完整字段的对象（老桥接 toggleFavoriteTrack
        同款轻量对象即可），但 **必须含入库所需元数据**：

        ⚠ R72 根因修复：卡片 ♥ 的本质是「把一首歌收进曲库（我喜欢的音乐）」。
          旧实现只传 source/ext_id/track_id 且 title="" → radio 落库
          library.add_track/db.upsert_track 读 track.path 抛 AttributeError
          （在线曲未播过、库里没有对应行时），异常被宽 except 吞掉 →
          **红心点亮但曲库永远不出现**。现在用前端随 POST 带来的完整元数据
          构造对象，并为在线曲补稳定占位 path（与 ai/playback.py 起播落库同款：
          netease://<ext> / qq://<ext>），使 upsert 的 ON CONFLICT(path) 能
          命中/正确插入 —— 已播过的歌点红心依旧幂等，未播过的歌也能真正入库。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            from types import SimpleNamespace
            # 本地曲（source 空/本地）以 db id 为准；在线曲补占位 path 供入库去重。
            # ⚠ path 协议名必须与音源层一致（data/sources/qqmusic.py → qq://<mid>、
            #   netease.py → netease://<sid>、spotify.py → spotify://<tid>）：
            #   只有 path 一致，起播自动 upsert 的行 / 红心 add_track 的行才会
            #   ON CONFLICT(path) 命中同一行（否则同歌两行）。qqmusic 的 source
            #   名是 'qqmusic' 但 path 协议是 'qq'，需显式映射。
            src = str(source or "").strip()
            ext = None if ext_id in (None, "") else str(ext_id)
            path = self._placeholder_path(src, ext)
            ns = SimpleNamespace(
                id=track_id, source=src or "local",
                ext_id=ext,
                path=path,                     # 在线曲稳定占位 path（upsert 主键）
                title=title or "",
                artist=artist or "",
                album=album or "",
                genre="", year=None,
                duration=0.0, bpm=None,
                cover=cover or "",
            )
            result = marshaller.call(lambda: self.radio.toggle_favorite(ns))
            return {"ok": True, "favorited": bool(result)}
        except Exception as e:  # noqa
            logger.warning("toggle_favorite_track 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def _mark_fav(self, entries) -> list:
        """给归一化条目补 `favorited` 标记（R49：卡片 ♥ 初始点亮态）。

        `_norm_entry` 是 classmethod 拿不到 radio，只能在这里补。
        ⚠ 只读内存 set（radio._queue_fav），不做任何 IO —— 这些接口被前端轮询。
        """
        try:
            favs = getattr(self.radio, "_queue_fav", None) or set()
            for d in entries or []:
                if not isinstance(d, dict):
                    continue
                src = d.get("source") or ""
                ext = d.get("ext_id")
                if src and ext not in (None, ""):
                    key = "e:%s:%s" % (src, ext)
                else:
                    key = "i:%s" % d.get("id")
                d["favorited"] = key in favs
        except Exception:  # noqa
            pass
        return entries

    def _mark_current(self, entries) -> list:
        """给列表型条目（曲库 / 历史）补 `current` 标记 —— 正在播放的那首。

        ⚠ R49-29：曲库中正在播放的卡片必须能识别出来（前端据此高亮 +
        自动滚动定位到它）。只读 radio.current_id 比较，不做任何 IO。
        """
        try:
            cur_id = getattr(self.radio, "current_id", None)
            if cur_id is None:
                return entries
            for d in entries or []:
                if isinstance(d, dict) and d.get("id") is not None:
                    d["current"] = (int(d["id"]) == int(cur_id))
        except Exception:  # noqa
            pass
        return entries

    def set_volume(self, vol: float) -> dict:
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            v = max(0.0, min(1.0, float(vol)))
            marshaller.call(lambda: self.radio.set_volume(v))
            return {"ok": True, "volume": v}
        except Exception as e:  # noqa
            logger.warning("set_volume 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- EQ（R68：手动 / 预设 / 动态） ----------------
    # 读/写 user_config eq.* + 引擎热应用。纯参数读写、不碰 Qt 对象；
    # 引擎 set_eq/get_eq 自带锁，可从任意线程安全调用（路由 await _to_thread 直跑）。
    # 持久化键：eq.enabled(bool) / eq.source('manual'|'preset') / eq.preset(str) /
    #          eq.bands(list[10] float dB) / eq.dyn(float 0..1)
    _EQ_DEFAULTS = {
        "enabled": False, "source": "manual", "preset": "flat",
        "bands": [0.0] * 10, "dyn": 0.6,
    }

    def get_eq_state(self) -> dict:
        """合并 user_config 与引擎实际状态（前端打开面板 / 启动恢复用）。"""
        out = dict(self._EQ_DEFAULTS)
        try:
            cfg = (user_config.data.get("eq") or {}) if user_config.data else {}
            for k in ("enabled", "source", "preset", "dyn"):
                if k in cfg:
                    out[k] = cfg[k]
            b = cfg.get("bands")
            if isinstance(b, list) and b:
                nb = [float(x) for x in b]
                out["bands"] = (nb + [0.0] * 10)[:10] if len(nb) < 10 else nb[:10]
            # 引擎侧实际应用状态（applied 反映是否真的落到滤波器）
            p = getattr(getattr(self, "radio", None), "player", None)
            if p is not None and hasattr(p, "get_eq_state"):
                try:
                    out["applied"] = bool(p.get_eq_state().get("applied"))
                except Exception:  # noqa
                    pass
            return {"ok": True, **out}
        except Exception as e:  # noqa
            logger.warning("get_eq_state 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), **dict(self._EQ_DEFAULTS)}

    def set_eq(self, **kw) -> dict:
        """写 EQ 配置：持久化 user_config + 热应用到引擎播放器。

        参数（可选，未传不覆盖）：enabled / source(manual|preset) /
        preset(名称) / bands(10 dB 列表) / dyn(0..1)。
        """
        try:
            node = user_config.data.setdefault("eq", {})
            if "enabled" in kw:
                node["enabled"] = bool(kw["enabled"])
            if "source" in kw:
                node["source"] = "preset" if str(kw["source"]) == "preset" else "manual"
            if "preset" in kw:
                node["preset"] = str(kw["preset"] or "flat")
            if "dyn" in kw:
                node["dyn"] = max(0.0, min(1.0, float(kw["dyn"])))
            if "bands" in kw:
                b = [float(x) for x in (kw["bands"] or [])]
                node["bands"] = (b + [0.0] * 10)[:10] if len(b) < 10 else b[:10]
            user_config.save()
            # 引擎热应用（播放器存在才有效；无 player 时下次应用）
            p = getattr(getattr(self, "radio", None), "player", None)
            eng_ok = False
            if p is not None and hasattr(p, "set_eq"):
                try:
                    st = p.set_eq(enabled=node.get("enabled"),
                                  source=node.get("source"),
                                  preset=node.get("preset"),
                                  bands=node.get("bands"),
                                  dyn=node.get("dyn"))
                    eng_ok = bool(st and st.get("applied"))
                except Exception as e:  # noqa
                    logger.warning("set_eq 引擎应用失败: %s: %s", type(e).__name__, e)
            return {"ok": True, "applied": eng_ok, **self.get_eq_state()}
        except Exception as e:  # noqa
            logger.warning("set_eq 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def _apply_eq_on_attach(self) -> None:
        """attach(radio) 后把持久化 EQ 应用到新播放器（启动恢复）。"""
        try:
            self.get_eq_state()          # 预热合并（含 applied 探测）
            self.set_eq()
        except Exception as e:  # noqa
            logger.debug("_apply_eq_on_attach 跳过: %s", e)

    # ---------------- 漫游滑块（R49-31） ----------------
    def get_familiarity(self) -> dict:
        """当前推荐熟悉度档位（0=更熟悉 … 3=更陌生）+ 四档标签。

        ⚠ 只读属性，回 Qt 主线程读 radio.familiarity。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            def _fn():
                f = getattr(self.radio, "familiarity", 0)
                return {
                    "value": max(0, min(3, int(f or 0))),
                    "labels": list(RadioConfig.FAMILIARITY_LABELS),
                }
            return {"ok": True, **marshaller.call(_fn)}
        except Exception as e:  # noqa
            logger.warning("get_familiarity 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "value": 0,
                    "labels": list(RadioConfig.FAMILIARITY_LABELS)}

    def set_familiarity(self, level: int) -> dict:
        """设置推荐熟悉度档位并持久化（radio.set_familiarity 已处理写盘）。

        ⚠ 改变推荐行为，走 Qt 主线程执行（radio 非线程安全）。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        try:
            v = max(0, min(3, int(level)))
            marshaller.call(lambda: self.radio.set_familiarity(v))
            return {"ok": True, "value": v}
        except Exception as e:  # noqa
            logger.warning("set_familiarity 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- 顶部搜索（R54 · 跨音源；R86 · 可指定音源） ----------------

    # 音源在搜索开关上的短名（滑块上只放 1~4 个字符，长名会被截断）
    _SEARCH_SRC_SHORT = {"qqmusic": "QQ", "netease": "网易", "spotify": "Spotify"}

    def search_sources(self) -> dict:
        """可切换的搜索音源列表（搜索框右侧开关用）。

        返回 {ok, sources:[{key,label,short,enabled}], current}
        - 只列**远程**音源（本地库不参与在线搜索）；
        - **只列「已启用」且「可搜索」的音源**（R86-fix）：曾把
          `plugins/sources/example_source.py`（模板插件，key="example"）也列了进去，
          短名兜底 `k[:4].upper()` 让开关上凭空多出一个「EXAM」；
          未启用的音源（无 api_base / 无凭据）选了也只会空手而归，同样不该出现。
        - **首项是「自动」**（key=""，= 所有音源并行竞速，谁先返回用谁）——
          这是历史默认行为，保留它才不会让"某个音源没启动"时搜索直接空手而归；
        - current = 用户上次选择（ui_prefs.searchSource）；"" = 自动；
          无效值回退自动。
        """
        try:
            from data.sources import SourceManager
            from config.settings import user_config as _uc
            sm = SourceManager(_uc.data)
            items = [{"key": "", "label": "自动（全部音源）", "short": "自动",
                      "enabled": True, "auto": True}]
            for s in sm.remote_sources():
                k = getattr(s, "key", "") or ""
                if not k:
                    continue
                # ⚠ R86-fix：模板/占位插件（searchable=False）与未启用音源一律不进开关
                if getattr(s, "searchable", True) is False:
                    continue
                if not getattr(s, "enabled", False):
                    continue
                items.append({
                    "key": k,
                    "label": getattr(s, "label", k) or k,
                    "short": self._SEARCH_SRC_SHORT.get(k, k[:4].upper()),
                    "enabled": True,
                })
            keys = [i["key"] for i in items]
            cur = str(self._prefs_load().get("searchSource") or "")
            if cur not in keys:
                cur = ""
            return {"ok": True, "sources": items, "current": cur}
        except Exception as e:  # noqa
            logger.warning("search_sources 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "sources": [], "current": ""}

    def search_songs(self, keyword: str, source: str = "") -> dict:
        """跨远程音源搜歌（供顶部搜索框建议/点播）。

        ⚠ 网络调用绝不经 marshaller（否则冻 Qt 主线程）—— 本方法整体在 FastAPI
        线程池执行。各音源**并行**搜（最慢源决定总时长，非各源累加），
        首个命中即返回（老桥 ui/bridge_host.py::searchSongs L1873 同语义）。

        source 非空 = **只搜该音源**（搜索框右侧的「切换搜索源」开关，R86）：
        此时不走「谁先返回用谁」的竞速，只等指定源的结果（超时同样兜底空列表）。

        返回 {ok, entries:[{id,title,artist,album,source,ext_id,cover,duration}]}
          - id：后端可播放的标识；本地为 db id，远程为 0（供 URL 探测失败兜底，
            前端需 source+ext_id 才能播放）
          - source/ext_id：稳定键，点播必须带（在线曲无本地 db id）
          - duration：秒（QQ search 的 interval / 网易云的 dt）—— 点播时透传回
            后端，否则 seed 曲目时长为 0 → **进度条永远不动**（R86 修复）
        """
        kw = (keyword or "").strip()
        if not kw:
            return {"ok": False, "error": "关键词为空", "entries": []}
        want = str(source or "").strip()
        try:
            from data.sources import SourceManager
            from config.settings import user_config as _uc
            sm = SourceManager(_uc.data)
        except Exception as e:  # noqa
            logger.warning("search_songs 音源管理器初始化失败: %s: %s",
                           type(e).__name__, e)
            return {"ok": False, "error": str(e), "entries": []}
        # 2026-11-19：已被用户禁用的插件音源不得继续参与搜索/推荐取数。
        sources = [s for s in sm.remote_sources() if getattr(s, "enabled", False)]
        if want:
            sources = [s for s in sources if getattr(s, "key", "") == want]
            if not sources:
                return {"ok": True, "entries": [], "source": want,
                        "note": f"音源不可用或已停用：{want}"}
        if not sources:
            return {"ok": True, "entries": [], "note": "无可用远程音源"}
        out: list[dict] = []
        limit = 8

        def _run(src) -> list[dict]:
            try:
                tracks = src.search(kw, limit=limit) or []
            except Exception as e:  # noqa
                logger.warning("search_songs %s 失败: %s: %s",
                               getattr(src, "key", "?"), type(e).__name__, e)
                return []
            items = []
            for t in tracks[:limit]:
                ext = getattr(t, "ext_id", None)
                if ext in (None, ""):
                    continue
                try:
                    dur = float(getattr(t, "duration", 0) or 0)
                except (TypeError, ValueError):
                    dur = 0.0
                items.append({
                    # 本地曲目 = 库内 db id（可走 /api/play/track/{id}）；
                    # 在线曲目无本地 db id → 0，前端必须带 source+ext_id 点播
                    "id": getattr(t, "id", None) or 0,
                    "title": getattr(t, "title", "") or "未知",
                    "artist": getattr(t, "artist", "") or "未知",
                    "album": getattr(t, "album", "") or "",
                    "source": getattr(src, "key", "") or "",
                    "ext_id": str(ext),
                    "duration": dur,
                    "cover": self._cover_url(getattr(t, "cover", "")),
                })
            return items

        # ⚠ 并行（R27/R28 教训：按源串行 = 各源耗时累加）。任一源先返回即采用，
        # 慢源不阻塞（后台自然收尾）；全部失败返回空列表由前端兜底。
        # 指定音源时不竞速（只等它），否则用户切了源却拿到别家的结果。
        try:
            with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
                futs = {pool.submit(_run, s): s for s in sources}
                for fut in as_completed(futs, timeout=10):
                    items = fut.result()
                    if items:
                        out = items
                        break
        except Exception as e:  # noqa
            logger.warning("search_songs 并行聚合失败: %s: %s", type(e).__name__, e)
        if out:
            return {"ok": True, "entries": out, "source": out[0].get("source", "")}
        return {"ok": True, "entries": [], "source": want}

    @staticmethod
    def _placeholder_path(source: str, ext_id) -> str | None:
        """在线曲的**稳定占位地址**（曲库 ON CONFLICT(path) 的唯一键）。

        ⚠ R86 根因：搜索点播构造的 seed Track 曾用 `path=""`，而
          `db.upsert_track` 的冲突键就是 path（`ON CONFLICT(path) DO UPDATE`）
          → 每一首「搜来点播」的歌都撞在同一行 `path=''` 上互相覆盖元数据，
          表现为「收藏的是 A 的某歌，回曲库播出来的却是 B 的同一首」。
          占位协议名必须与音源层一致（qqmusic→qq://<mid>、netease→netease://<sid>、
          spotify→spotify://<tid>），这样「起播自动 upsert 的行」与「卡片红心
          add_track 的行」才会命中同一行（否则同歌两行）。
        """
        src = str(source or "").strip()
        ext = "" if ext_id in (None, "") else str(ext_id).strip()
        if not src or not ext or src == "local":
            return None
        proto = {"qqmusic": "qq"}.get(src, src)
        return "%s://%s" % (proto, ext)

    def play_search_track(self, source: str, ext_id: str = "",
                          title: str = "", artist: str = "",
                          album: str = "", cover: str = "",
                          duration: float = 0.0) -> dict:
        """按音源身份起播一首**在线搜索**结果（顶部搜索框点播）。

        ⚠ 搜索结果都是**远程占位曲目**（无本地 db id），不能走 /api/play/track/{id}。
        这里用 search 返回的元数据构造远程 Track（source+ext_id），后台 start_radio
        seed —— 播放时由 radio 内部 source_manager.resolve_url 实时解析直链
        （与老桥 ui/bridge_host.py::playTrackById 处理 'e:src:ext' 同语义）。

        ext_id 类型按源规约还原：纯数字（网易云 id）→ int；字母 songmid（QQ）→ str。

        ⚠ R86 两处修复：
          1) `path` 必须是稳定占位地址（见 _placeholder_path）—— 否则起播入库时
             与其它搜索点播曲目撞同一行 path='' 互相覆盖（"播出来的不是这首"）。
          2) `duration` 透传（QQ search 的 interval / 网易云 dt）—— 曾恒为 0.0，
             于是 player.play(0) → state().duration=0 → **前端进度条永远不动**。
        """
        if not self.ready:
            return {"ok": False, "error": "radio 未接入"}
        src = (source or "").strip()
        ext = str(ext_id or "").strip()
        if not src or not ext:
            return {"ok": False, "error": "source/ext_id 缺失"}
        try:
            # 纯数字 id 还原为 int（netease resolve_url 按 int 处理）
            if ext.isdigit():
                ext = int(ext)
        except Exception:  # noqa
            pass
        try:
            dur = 0.0
            try:
                dur = max(0.0, float(duration or 0.0))
            except (TypeError, ValueError):
                dur = 0.0
            from data.library import Track
            seed = Track(
                id=0, path=(self._placeholder_path(src, ext) or ""),
                title=(title or "未知"),
                artist=(artist or "未知歌手"), album=(album or ""),
                genre="", year=None, duration=dur, bpm=None,
                source=src, ext_id=ext, cover=(cover or None),
            )
        except Exception as e:  # noqa
            logger.warning("play_search_track 构造 Track 失败: %s: %s",
                           type(e).__name__, e)
            return {"ok": False, "error": str(e)}
        try:
            threading.Thread(target=self._kick_radio, args=(seed,),
                             daemon=True, name="play-search").start()
            return {"ok": True, "source": src, "ext_id": str(ext),
                    "path": seed.path, "duration": dur}
        except Exception as e:  # noqa
            logger.warning("play_search_track 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- 前端偏好持久化（R54 · 皮肤/背景/元素显示） ----------------

    @staticmethod
    def _prefs_path() -> Path:
        return Path(__file__).resolve().parents[1] / "data_runtime" / "ui_prefs.json"

    # ═══ R93：偏好写盘必须**串行** ═══
    # 事故：前端一次交互可能并发多个 POST（如切「叠层效果」卡片会同时改 ontop 与 water）。
    # 原实现 = 固定 tmp 名 `ui_prefs.json.tmp` + 「读-改-写」无锁 →
    #   ① 两次并发写同一个 tmp 文件互相截断/续写 → 落盘成「两个 JSON 文档拼接」的**坏文件**；
    #   ② 坏文件让 `_prefs_load` 解析失败 → 静默回退默认 → **用户全部偏好（主题/高亮/
    #      壁纸/音源）一起丢失**，且症状表现为"改了不生效"。
    # 修法：进程级互斥锁串行化「读-改-写」，tmp 名再带 pid/tid 做二次防护。
    _PREFS_LOCK = threading.Lock()

    @staticmethod
    def _prefs_defaults() -> dict:
        return {
            "theme": "default",          # 调色板主题 key（default / aurora / sunset …）
            "bgmode": "poster",          # 背景模式：poster（歌词海报）| flow（流动水样）
            "accent": "",                # 高亮色覆盖（空 = 未覆盖，跟随主题）
            "hideElems": {},             # {元素id: true} 主界面元素隐藏（如 aibar/d-history）
            "skins": [],                 # [{key,name,primary,secondary,accent,dark,light}] 用户调色板
            "beat": True,                # 音频律动：封面随真实频谱呼吸
            "water": True,               # R70 背景水面：on-top 情感水体层（默认开）
            # ═══ R93 · 叠层效果（on-top effect，P4 契约）═══
            # ontop = 叠在「背景效果」之上的动态层，n 选一；'none' 也是一张常驻卡。
            # ⚠ water 保留为**旧键兼容**（前端两向同步写盘）；读时 ontop 优先。
            "ontop": "water",            # 'none' | 'water'（D1：首期两卡）
            # 水面参数组（整组写盘；前端 clamp，后端只做白名单透传，不做嵌套合并）
            "waterOpts": {
                "physics": True,         # 物理增强（R90）
                "eventMode": "emotion",  # 事件层模式：emotion | ripple
                "bank": "bank",          # 波银行：bank（相位累加）| legacy（旧版三波）
                "semStrength": 0.6,      # 性格强度档 0~1
                "semPulse": False,       # 段切换脉冲
                "tier": "Q3",            # 画质档位 Q3/Q2/Q1
                "specExp": -2.0,         # 波纹细度（谱指数；S1 落地前 UI 禁用）
            },
            "wallpaper": "cover",        # R78 背景内容：cover（跟随封面）| default（内置 logo）| custom（导入壁纸）
            # ★ 2026-11-18 新 UI：用户上次选中的自定义壁纸 key（跨刷新/重启保持，
            #    治"上次点啥下次跳默认"）。旧 UI 不读这个键 ⇒ 向后兼容，零影响。
            "customWallpaperKey": "",
            "searchSource": "",          # R86 搜索音源："" = 自动（谁先返回用谁）；否则只搜该音源 key
        }

    @staticmethod
    def _prefs_load() -> dict:
        """读前端偏好（无锁浅读；配置损坏时回退默认，绝不抛）。

        ⚠ R93：`d.update(data)` 是**浅合并** —— 旧库只有 `water`（无 `ontop`）时，
        直接用新默认 `ontop='water'` 会把「用户上次关了水面」重新打开。所以这里补
        两条迁移：① 无 ontop → 由 water 派生；② waterOpts 做一层键级合并（缺键回默认）。
        """
        p = RadioBridge._prefs_path()
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    d = RadioBridge._prefs_defaults()
                    d.update(data)
                    if "ontop" not in data and isinstance(data.get("water"), bool):
                        d["ontop"] = "water" if data["water"] else "none"
                    wo = data.get("waterOpts")
                    if isinstance(wo, dict):
                        merged = dict(RadioBridge._prefs_defaults()["waterOpts"])
                        merged.update(wo)
                        d["waterOpts"] = merged
                    return d
        except Exception as e:  # noqa
            logger.warning("读 ui_prefs 失败，回退默认: %s: %s", type(e).__name__, e)
        return RadioBridge._prefs_defaults()

    @staticmethod
    def _prefs_save(patch: dict, sid=None, seq=None) -> dict:
        """合并写入前端偏好（原子写 tmp → os.replace），返回合并后完整 dict。

        ⚠ 只接受已知键（防污染/未知键落盘）；损坏文件先回退默认再合并。
        ⚠ R93：整个「读-改-写」在 `_PREFS_LOCK` 内串行（并发 POST 会互踩成坏文件）。
        ⚠ R94：**锁只保证"不写坏"，不保证"最后动作者胜"** —— 前端一次拖拽会连发
        几十个 POST，线程池拿锁的顺序与用户操作顺序无关 → 落盘可能停在**中间态**
        （本机实测 5 轮中 2 轮：拖回 −2.0 却存成 −1.8333）。这里再加一道**单调序号闸**：
        同一页面会话（`__sid`）只接受序号更大的写，过期的直接丢弃（返回当前值、不报错）。
        无 `__sid` 的旧调用方（既有 verify / 外部工具）一律照常接受 —— 向后兼容。
        """
        with RadioBridge._PREFS_LOCK:
            cur = RadioBridge._prefs_load()
            try:
                _sid_old = str(cur.get("_sid") or "")
                _seq_old = int(cur.get("_seq") or 0)
            except Exception:  # noqa
                _sid_old, _seq_old = "", 0
            _sid_new = str(sid) if sid else ""
            try:
                _seq_new = int(seq) if seq is not None else 0
            except Exception:  # noqa
                _seq_new = 0
            if _sid_new and _sid_new == _sid_old and _seq_new and _seq_new <= _seq_old:
                return cur                      # 过期写：丢弃（迟到不影响用户终值）
            allowed = set(RadioBridge._prefs_defaults().keys())
            d = dict(cur)
            for k, v in (patch or {}).items():
                if k in allowed:
                    d[k] = v
            if _sid_new:
                d["_sid"] = _sid_new
                d["_seq"] = _seq_new
            p = RadioBridge._prefs_path()
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name("%s.%d.%d.tmp" % (p.name, os.getpid(),
                                                    threading.get_ident()))
                tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(p)
            except Exception as e:  # noqa
                logger.warning("写 ui_prefs 失败: %s: %s", type(e).__name__, e)
            return d

    def get_ui_prefs(self) -> dict:
        """返回前端偏好（主题/背景模式/隐藏元素），供前端初始化与 2s 轮询同步。"""
        try:
            return {"ok": True, "prefs": self._prefs_load()}
        except Exception as e:  # noqa
            logger.warning("get_ui_prefs 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "prefs": self._prefs_defaults()}

    def set_ui_prefs(self, patch: dict, sid=None, seq=None) -> dict:
        """合并保存前端偏好（GET /api/ui/prefs 的对偶）。

        R94：`sid`/`seq` = 页面会话 id + 单调序号，用于丢弃**迟到的过期写**。
        """
        try:
            d = self._prefs_save(patch or {}, sid, seq)
            return {"ok": True, "prefs": d}
        except Exception as e:  # noqa
            logger.warning("set_ui_prefs 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "prefs": self._prefs_defaults()}

    # ---------------- 个性化设置（AI 大模型 / 语音 TTS / 音源 / 通用 / 匹配） ----------------
    # 逐行移植自老架构 ui/bridge_host.py L1926-2100（getSettings / setSetting）。
    # ⚠ 纯 user_config 读写 + 文件操作，不碰 Qt 对象 → 无需 marshaller，线程池直跑。
    #   敏感字段（api_key / cookie / client_secret）**不下发明文**，
    #   仅返回 {path: bool} 的「是否已设置」标记；写入仍允许（留空=不改）。

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
        # 语音 TTS（★ 2026-11-18 统一到 Qwen3-TTS；旧引擎/ffmpeg 后处理选项已退休）
        ("tts", "qwen3tts_voice"), ("tts", "qwen3tts_postprocess"),
        ("tts", "melotts_pp_hs1"), ("tts", "melotts_pp_hs2"), ("tts", "melotts_pp_hs3"),
        ("tts", "melotts_pp_comp_th"), ("tts", "melotts_pp_comp_ratio"),
        ("tts", "melotts_pp_echo"), ("tts", "melotts_pp_loud_i"),
        # ★ IndexTTS 已彻底退休：配置项不再暴露给前端（旧值留 config 里无害，不被加载）
        # 搜索
        ("search", "mode"), ("search", "online_provider"), ("search", "browser_engine"),
        ("search", "browser_url"), ("search", "max_results"),
        # 电台 / 推荐偏好 / 城市
        ("roam_strategy",), ("auto_play",),
        ("rec_pref",), ("geo", "city"),
        # 匹配
        ("match", "fuzzy_enabled"), ("match", "fuzzy_threshold"),
        # 音源（非敏感）—— R67：netease.api_base（手动 API 端点）删除，只留 MusicBox 扫码；
        # qqmusic.auto_start 删除（永远不显示开关，启动时按 enabled 自动拉起行为已并入 attach()）
        ("sources", "netease", "enabled"),
        ("sources", "netease", "musicbox_uid"),
        ("sources", "qqmusic", "enabled"), ("sources", "qqmusic", "api_base"),
        ("sources", "qqmusic", "default_playlist_id"),
        ("sources", "spotify", "enabled"), ("sources", "spotify", "api_base"),
        ("sources", "spotify", "client_id"), ("sources", "spotify", "default_playlist_id"),
        ("sources", "local", "enabled"), ("sources", "local", "roots"),
        # UI
        ("ui", "skin"), ("ui", "background_fit"), ("ui", "background_mode"),
    )
    # 写只敏感字段：get 不下发明文，仅返回 *_set 标记；set 可写入。
    _SETTING_SENSITIVE = (
        ("ai", "api_base"), ("ai", "api_key"),
        ("sources", "netease", "cookie"),
        ("sources", "qqmusic", "cookie"),
        ("sources", "spotify", "client_secret"), ("sources", "spotify", "cookie"),
    )

    @classmethod
    def _setting_paths(cls):
        return cls._SETTING_WHITELIST + cls._SETTING_SENSITIVE

    @staticmethod
    def _cfg_get(data: dict, path) -> tuple:
        """按元组路径取值 → (found: bool, value)。"""
        node = data
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return False, None
        return True, node

    def get_settings(self) -> dict:
        """返回设置白名单（非敏感）+ 敏感字段的「是否已设置」标记。

        老架构 getSettings（bridge_host L1975）的等价实现：
          out      = 白名单字段的当前值（嵌套 dict，前端按点路径取）
          sensitive= {"ai.api_key": True, ...} 只标记是否非空，不含明文
        """
        try:
            from config.settings import user_config
            out: dict = {}
            for path in self._SETTING_WHITELIST:
                ok, val = self._cfg_get(user_config.data, path)
                if not ok:
                    continue
                cur = out
                for key in path[:-1]:
                    cur = cur.setdefault(key, {})
                cur[path[-1]] = val
            sens = out.setdefault("_sensitive", {})
            for path in self._SETTING_SENSITIVE:
                ok, val = self._cfg_get(user_config.data, path)
                sens[".".join(path)] = bool(ok and val)
            return {"ok": True, "values": out, "sensitive": sens}
        except Exception as e:  # noqa
            logger.warning("get_settings 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "values": {}, "sensitive": {}}

    def set_setting(self, path: str, value: str) -> dict:
        """按点路径写 user_config 并落盘：set_setting('ai.web_search', '1')。

        value 按目标字段的**现有类型**自动转换（bool/int/float/str/list）；
        特殊：sources.local.roots 按行拆成列表；rec_pref 夹到 0..3。
        """
        try:
            from config.settings import user_config
        except Exception as e:  # noqa
            return {"ok": False, "error": f"配置模块不可用：{e}"}
        keys = [k for k in str(path or "").split(".") if k]
        if not keys:
            return {"ok": False, "error": "路径为空"}
        allowed = {".".join(p) for p in self._setting_paths()}
        if ".".join(keys) not in allowed:
            logger.warning("set_setting 拒绝非白名单字段: %s", path)
            return {"ok": False, "error": f"字段不在白名单：{path}"}
        try:
            node = user_config.data
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            key = keys[-1]
            if ".".join(keys) == "sources.local.roots":
                v = [r.strip() for r in str(value).splitlines() if r.strip()]
            elif ".".join(keys) == "rec_pref":
                v = max(0, min(3, int(float(value))))
            else:
                cur = node.get(key)
                if isinstance(cur, bool):
                    v = str(value) in ("1", "true", "True", "on", "yes")
                elif isinstance(cur, int):
                    v = int(float(value))
                elif isinstance(cur, float):
                    v = float(value)
                else:
                    v = str(value)
            node[key] = v
            user_config.save()
            self._apply_setting_live(keys, v)
            return {"ok": True, "path": ".".join(keys), "value": v}
        except Exception as e:  # noqa
            logger.warning("set_setting %s 失败: %s: %s", path, type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def _apply_setting_live(self, keys, v) -> None:
        """把设置实时应用到运行中的组件（不重启生效）。

        ⚠ 只写属性、不改播放状态 → 不回 Qt 主线程（避免无谓编排）。
        """
        r = getattr(self, "radio", None)
        if not r:
            return
        try:
            if keys[:1] == ["ai"]:
                b = getattr(r, "brain", None)
                llm = getattr(b, "llm", None) if b else None
                if llm:
                    if keys[-1] in ("backend", "api_base", "api_key", "model",
                                    "llamacpp_host", "ollama_host", "ollama_deep"):
                        # ★ 2026-11-19：后端/凭据类改动必须**重建 LLM 客户端**才生效。
                        #   旧代码只写 user_config 落盘、不动运行中的实例 ⇒
                        #   前端"切换后端"显示成功但实际仍跑旧后端（用户实测）。
                        #   reconfigure() 从 user_config 重读 backend 并重建 client；
                        #   set_setting 已先写盘再调这里，顺序正确。
                        if hasattr(llm, "reconfigure"):
                            llm.reconfigure()
                    elif keys[-1] == "web_search":
                        llm.web_search = bool(v)
                        # ★ 2026-11-18：口播实际用 brain.web_search（generate_dj_script），
                        #   只设 llm.web_search 不生效 ⇒ 同步 brain。
                        if b is not None:
                            b.web_search = bool(v)
                    elif keys[-1] == "thinking":
                        llm.thinking = bool(v)
                        # ★★ 2026-11-18 修复：口播思考开关失效的根因——
                        #   generate_dj_script 传 thinking=self.thinking（brain 的），
                        #   旧代码只设 llm.thinking，brain.thinking 不变 ⇒ 前端开关无效。
                        #   必须同步 brain.thinking：关→budget=0 真关、开→budget>0 真开。
                        if b is not None:
                            b.thinking = bool(v)
            elif keys[:1] == ["tts"]:
                # ★ 2026-11-18：切 Qwen3-TTS 音色即时生效（无需重启）
                tts = getattr(r, "tts", None)
                if tts and hasattr(tts, "reconfigure"):
                    tts.reconfigure()
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
            elif keys[:1] == ["tts"]:
                tts = getattr(r, "tts", None)
                if tts and hasattr(tts, "reconfigure"):
                    try:
                        tts.reconfigure()
                    except Exception:  # noqa
                        pass
            elif keys[:1] == ["sources"]:
                # R85：音源凭据 / API 地址改动后即时同步到运行中的实例，
                #      否则手动粘贴 Cookie 或换 qq-music-api 地址都要重启才生效。
                if keys[:2] == ["sources", "qqmusic"] and keys[-1] in (
                        "cookie", "api_base", "enabled"):
                    self._sync_qq_source(str(v) if keys[-1] == "cookie" else "")
        except Exception as e:  # noqa
            logger.debug("_apply_setting_live 跳过: %s", e)

    # ---- 测试连接（老设置面板的「测试 AI / TTS / VOICEVOX / 测试朗读」） ----

    def test_ai(self) -> dict:
        """测试 AI 大模型连通性（按当前 backend 走 ollama / OpenAI 兼容）。"""
        try:
            from ai.brain import AIBrain
            from config.settings import user_config
            cfg = user_config.data.get("ai", {}) or {}
            backend = str(cfg.get("backend", "api")).lower()
            if backend == "ollama":
                base = cfg.get("ollama_host", "http://127.0.0.1:11434")
                key = ""
                model = cfg.get("ollama_deep", "qwen3.6:35b-a3b")
            elif backend == "llamacpp":
                base = cfg.get("llamacpp_host", "http://127.0.0.1:8080")
                key = ""
                model = ""
            else:
                base = cfg.get("api_base", "")
                key = cfg.get("api_key", "")
                model = cfg.get("model", "gpt-4o-mini")
            ok, msg = AIBrain().test_connection(base, key, model)
            return {"ok": True, "passed": bool(ok), "msg": str(msg)}
        except Exception as e:  # noqa
            logger.warning("test_ai 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "passed": False, "msg": f"测试异常：{e}"}

    def test_tts(self) -> dict:
        """检测 TTS 可用性（★ 2026-11-18 只测 qwen3tts，IndexTTS 已彻底退休）。"""
        try:
            from core.tts import TTSEngine
            mgr = TTSEngine()
            diag = mgr.diagnose() or {}
            eng = mgr._get_engine("qwen3tts")
            d = eng.diagnose() if eng and hasattr(eng, "diagnose") else {}
            ok = bool(d.get("available"))
            parts = [f"qwen3tts:{'OK' if ok else 'X'} 音色={d.get('voice', '?')}"]
            return {"ok": True, "active": getattr(mgr, "_engine_name", "?"),
                    "msg": "当前引擎=" + str(getattr(mgr, "_engine_name", "?")) + "。 " + "；".join(parts)}
        except Exception as e:  # noqa
            logger.warning("test_tts 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "msg": f"检测异常：{e}"}

    def test_tts_sample(self) -> dict:
        """测试朗读：用当前引擎合成示例并交给播放器试听。"""
        try:
            from core.tts import TTSEngine
            eng = TTSEngine()
            eng.reconfigure()
            path = eng.synthesize("这里是 Radio Intelligence 智能电台，正在测试语音合成效果。")
            if not path:
                return {"ok": False, "msg": "TTS 不可用（无可用引擎/离线）"}
            r = getattr(self, "radio", None)
            player = getattr(r, "player", None) if r else None
            if player is not None and hasattr(player, "play_tts"):
                # 播放状态变更 → 回 Qt 主线程
                marshaller.call(lambda: player.play_tts(str(path)))
                return {"ok": True, "msg": "正在试听…"}
            return {"ok": True, "msg": "已生成（未连接播放器）"}
        except Exception as e:  # noqa
            logger.warning("test_tts_sample 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "msg": f"失败：{e}"}

    def test_voicevox(self) -> dict:
        """测试/拉起 VOICEVOX（headless 引擎，日文口播用）。"""
        try:
            from core.tts import TTSEngine
            ok = TTSEngine().ensure_voicevox(timeout=45)
            return {"ok": True, "passed": bool(ok),
                    "msg": "VOICEVOX 已连接，日文口播可用。" if ok
                           else "无法连接 VOICEVOX，日文片段将回退 Edge。"}
        except Exception as e:  # noqa
            logger.warning("test_voicevox 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "passed": False, "msg": f"检测异常：{e}"}

    # ---- 音源扫码登录（netease / qq） ----
    # 移植自 ui/bridge_host.py L2083-2180。二维码在后端生成，
    # 统一输出 480×480 白底 PNG data URL（避免两个音源忽大忽小）。

    _QR_SIZE = 480

    @classmethod
    def _qr_fixed_png(cls, img, size: int = 480) -> str:
        """任意二维码 PIL 图 → size×size 白底居中 PNG data URL（NEAREST 保模块锐利）。"""
        import base64
        import io
        from PIL import Image
        canvas = Image.new("RGB", (size, size), "white")
        qr = img.convert("RGB").resize((size - 16, size - 16), Image.NEAREST)
        canvas.paste(qr, (8, 8))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    @classmethod
    def _qr_png_data_url(cls, url: str) -> str:
        """网易云登录 URL → 固定 480×480 白底 PNG data URL。"""
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").get_image()
        return cls._qr_fixed_png(img)

    @classmethod
    def _normalize_qr_img(cls, img: str) -> str:
        """QQ 代理返回的二维码（data URL / http 图）归一到 480×480；失败原样返回。"""
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
            return cls._qr_fixed_png(Image.open(io.BytesIO(raw)))
        except Exception as e:  # noqa
            logger.warning("二维码归一化失败，使用原图: %s", e)
            return img

    def source_qr_start(self, source: str) -> dict:
        """发起扫码登录 → {ok, source, key, img(dataURL), err}。"""
        source = str(source or "").lower()
        try:
            if source == "netease":
                from plugins.sources.netease import NeteaseSource
                session = NeteaseSource({"backend": "musicbox"}).start_qr_login() or {}
                unikey = session.get("unikey", "")
                url = session.get("url", "")
                if not unikey:
                    return {"ok": False, "source": source,
                            "err": "获取二维码失败：响应缺少登录标识"}
                return {"ok": True, "source": source, "key": unikey,
                        "img": self._qr_png_data_url(url)}
            if source == "qq":
                from config.settings import user_config
                from plugins.sources.qqmusic import QQMusicSource
                api_base = ((user_config.data.get("sources", {}) or {})
                            .get("qqmusic", {}) or {}).get("api_base", "") \
                    or "http://127.0.0.1:3200"
                payload = QQMusicSource({"api_base": api_base}).start_qr_login() or {}
                qrsig = payload.get("qrsig", "")
                if not qrsig:
                    return {"ok": False, "source": source,
                            "err": "获取二维码失败：响应缺少 qrsig"}
                # ptqrtoken 轮询要用，暂存在 bridge 实例上
                self._qr_qq = {"qrsig": qrsig,
                               "ptqrtoken": payload.get("ptqrtoken", "") or ""}
                return {"ok": True, "source": source, "key": qrsig,
                        "img": self._normalize_qr_img(payload.get("img", ""))}
            return {"ok": False, "source": source, "err": "未知音源"}
        except Exception as e:  # noqa
            logger.warning("source_qr_start(%s) 失败: %s", source, e)
            return {"ok": False, "source": source, "err": str(e)}

    def source_qr_poll(self, source: str, key: str) -> dict:
        """轮询扫码状态 → {status: waiting|scanned|expired|ok|error, msg}。"""
        source = str(source or "").lower()
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
                api_base = ((user_config.data.get("sources", {}) or {})
                            .get("qqmusic", {}) or {}).get("api_base", "") \
                    or "http://127.0.0.1:3200"
                st = getattr(self, "_qr_qq", {}) or {}
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
            logger.warning("source_qr_poll(%s) 失败: %s", source, e)
            return {"status": "error", "msg": str(e)}

    def _finalize_ncm_login(self) -> None:
        from config.settings import user_config
        user_config.data.setdefault("sources", {}).setdefault("netease", {}).update(
            {"enabled": True, "backend": "musicbox"})
        try:
            user_config.save()
        except Exception as e:  # noqa
            logger.warning("ncm 登录保存失败: %s", e)

    def _finalize_qq_login(self, result: dict) -> None:
        from config.settings import user_config
        session = (result or {}).get("session") or {}
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
        # ⚠ R85：登录发生在运行期，而运行中的 QQMusicSource 是启动时构建的
        #   —— 只写 user_config 不会刷新它已建立的 requests 会话头，
        #   于是「登录成功了但仍报音源获取失败」。这里显式把 cookie 推给实例。
        self._sync_qq_source(cookie)

    def _sync_qq_source(self, cookie: str = "") -> None:
        """把最新 QQ 凭据/地址同步到运行中的 QQMusicSource（不重建 SourceManager）。

        - 扫码登录成功：传 cookie → 写实例 cfg + 会话头，立即生效；
        - 设置页改 api_base / 手动粘贴 cookie：不传 → 仅重读 cfg 重新应用。
        """
        try:
            r = getattr(self, "radio", None)
            sm = getattr(r, "source_manager", None) or \
                getattr(r, "_source_manager", None)
            src = sm.get("qqmusic") if sm is not None else None
            if src is None:
                if cookie:
                    logger.info("QQ 登录已保存；运行中的实例暂不可用（下次启动生效）")
                return
            if cookie and hasattr(src, "set_cookie"):
                src.set_cookie(cookie)          # 同时写 cfg 与会话头
            if hasattr(src, "apply_cfg"):
                src.apply_cfg()                 # 重读 api_base + cookie
            elif hasattr(src, "refresh_cookie"):
                src.refresh_cookie()
            logger.info("QQ 音源配置已同步到运行中的实例（即时生效）")
        except Exception as e:  # noqa
            logger.warning("QQ 音源配置同步失败（不影响已落盘配置）: %s: %s",
                           type(e).__name__, e)

    # ---------------- 壁纸（皮肤系统 · 导入/列表/激活/删除） ----------------
    # ⚠ 纯文件/注册表操作，不碰 Qt 对象 → 无需 marshaller，线程池直跑即可。
    #   持久化 = plugins/skins/<key>/skin.py（生成插件）+ data/skins/assets/<key>.*
    #   （导入媒体拷贝）；重启后 discover_plugins 重扫 → 壁纸仍在（列表可见）。

    @staticmethod
    def _skin_manager():
        try:
            from core.skin import SkinManager
            return SkinManager()
        except Exception as e:  # noqa
            logger.warning("SkinManager 初始化失败: %s: %s", type(e).__name__, e)
            return None

    def list_wallpapers(self) -> dict:
        """列出全部已导入/可用的皮肤壁纸（供前端「我的壁纸」网格）。

        每条带可访问的缩略图 URL（经 /skin-assets 静态挂载）：
          - image      → 原图（拷贝在 data/skins/assets，缩略直用原图）
          - video/web  → preview 图（无则空，网格占位）
          - 内置 default / qss 皮肤不列（它们不是「壁纸」）。
        """
        try:
            sm = self._skin_manager()
            if sm is None:
                return {"ok": False, "error": "SkinManager 不可用", "items": []}
            items = []
            for m in sm.list_skins():
                kind = m.get("kind") or "qss"
                if kind in ("qss", "color") or m.get("key") == "default":
                    continue  # 只列媒体壁纸
                items.append(self._wallpaper_entry(m))
            return {"ok": True, "items": items,
                    "active": self._active_wallpaper_key(sm)}
        except Exception as e:  # noqa
            logger.warning("list_wallpapers 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e), "items": []}

    @staticmethod
    def _active_wallpaper_key(sm) -> str:
        try:
            return sm.active_key or ""
        except Exception:  # noqa
            return ""

    def _wallpaper_entry(self, m: dict) -> dict:
        """把皮肤 meta 转成前端网格条目（本地路径改写成 HTTP URL）。"""
        from core.plugin import PROJECT_ROOT
        key = m.get("key", "")
        thumb = m.get("preview") or m.get("background") or ""
        url = self._local_to_url(thumb, PROJECT_ROOT)
        # image 皮肤：preview/background 可能为空 → 用 content.path（assets 原图）
        if not url:
            c = m.get("content") or {}
            if c.get("path"):
                url = self._local_to_url(c["path"], PROJECT_ROOT)
        return {
            "key": key,
            "name": m.get("name") or key,
            "kind": m.get("kind") or "image",
            "engine_type": m.get("engine_type"),
            "url": url,
            "preview": m.get("preview"),
            "background": m.get("background"),
            "description": m.get("description") or "",
        }

    def _attach_descriptor(self, sm, key: str, entry: dict) -> dict:
        """给壁纸条目附上渲染 descriptor（路线 A 2026-11-18）。

        前端据此按 type 分派：image→#bgimg / video→#bgvideo / web→iframe /
        scene_preview→预览图。失败不影响原 entry（向后兼容）。
        """
        try:
            desc = sm.get_skin(key).render_descriptor()
            rtype = desc.get("type") or "image"
            media = desc.get("path") or desc.get("url") or ""
            if media and not str(media).startswith(("http://", "https://", "data:")):
                from core.plugin import PROJECT_ROOT
                media = self._local_to_url(str(media), PROJECT_ROOT)
            entry["type"] = rtype
            entry["media"] = media or entry.get("url") or ""
            entry["descriptor"] = {
                "type": rtype, "loop": desc.get("loop", True), "mute": desc.get("mute", True),
                "engine_type": desc.get("engine_type") or (desc.get("extra") or {}).get("engine_type"),
            }
        except Exception as e:  # noqa
            logger.warning("_attach_descriptor(%s) 失败: %s", key, e)
            entry.setdefault("type", entry.get("kind") or "image")
            entry.setdefault("media", entry.get("url") or "")
        return entry

    def _local_to_url(self, p: str, project_root) -> str:
        """本地文件绝对路径 → /skin-assets/<相对 data/skins> URL；http/空原样。"""
        if not p:
            return ""
        if p.startswith(("http://", "https://", "data:")):
            return p
        try:
            pth = Path(p)
            skins_assets = project_root / "data" / "skins" / "assets"
            rel = pth.resolve().relative_to(skins_assets.resolve())
            return "/skin-assets/" + rel.as_posix()
        except (ValueError, OSError):
            # 非 assets 内路径（外部引用的壁纸引擎目录等）：尝试 file 不存在则留空
            return ""

    def activate_wallpaper(self, key: str) -> dict:
        """激活指定壁纸皮肤（持久化 active_key 到 user_config.ui.skin）。"""
        try:
            sm = self._skin_manager()
            if sm is None:
                return {"ok": False, "error": "SkinManager 不可用"}
            if not sm.set_active(key):
                return {"ok": False, "error": f"皮肤不存在：{key}"}
            return {"ok": True, "active": key,
                    "descriptor": sm.render_descriptor()}
        except Exception as e:  # noqa
            logger.warning("activate_wallpaper 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def import_wallpaper_file(self, filename: str, data: bytes) -> dict:
        """导入一张图片壁纸文件（multipart 上传 → 拷贝 assets + 生成皮肤插件）。

        filename 用于扩展名判断与命名。成功后自动激活（用户导入即见）。
        返回 {ok, key, name, url} —— url 是可直接 <img> 的 /skin-assets 地址。
        """
        import tempfile
        try:
            ext = Path(filename or "wallpaper.png").suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"):
                return {"ok": False, "error": f"不支持的图片格式：{ext or '空'}"}
            if not data:
                return {"ok": False, "error": "空文件"}
            sm = self._skin_manager()
            if sm is None:
                return {"ok": False, "error": "SkinManager 不可用"}
            # 写到临时文件再交给导入器（导入器负责拷到 assets + 生成 skin.py）
            fd, tmp = tempfile.mkstemp(suffix=ext, prefix="wp_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                key = sm.import_image(tmp, Path(filename).stem)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            # 构造返回条目（导入器内部已 reload + set_active）
            from core.plugin import PROJECT_ROOT
            url = self._local_to_url(str(
                PROJECT_ROOT / "data" / "skins" / "assets" / f"{key}{ext}"),
                PROJECT_ROOT)
            return {"ok": True, "key": key, "name": Path(filename).stem,
                    "url": url or ""}
        except Exception as e:  # noqa
            logger.warning("import_wallpaper_file 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    def remove_wallpaper(self, key: str) -> dict:
        """删除导入的壁纸皮肤（仅删生成插件目录；若为当前激活则回退 default）。"""
        try:
            sm = self._skin_manager()
            if sm is None:
                return {"ok": False, "error": "SkinManager 不可用"}
            ok = sm.remove_skin(key)
            return {"ok": ok,
                    "active": self._active_wallpaper_key(sm),
                    "error": "" if ok else f"删除失败：{key}"}
        except Exception as e:  # noqa
            logger.warning("remove_wallpaper 失败: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": str(e)}

    # ---------------- 壁纸引擎导入（工程文件夹 / Steam 链接 / zip 上传） ----------------
    # ⚠ 老 UI（ui/bridge_host.py importWallpaper）把 image/video/link/wallpaper 都喂给
    #   SkinManager；新 FastAPI 前端跑在浏览器里读不到本地任意路径 → 分三类：
    #     · source=本地工程绝对路径（桌面端/本机 Python 用，直接 import_from_link）
    #     · source=Steam 创意工坊链接 / 数字 ID（走 import_from_link → 需本地已订阅）
    #     · import_wallpaper_zip（浏览器上传 zip，后端解包找 project.json / 媒体）
    #   纯文件/注册表操作 → 线程池直跑，不碰 Qt。

    def import_wallpaper_engine(self, source: str, name: str = "") -> dict:
        """从链接 / 本地工程路径导入壁纸引擎（scene 退化为 preview 图可渲染）。

        source 三种形态：
          · 本地工程文件夹绝对路径（盘符/UNC）→ import_wallpaper_folder（引用）
          · Steam 创意工坊链接 / 数字 ID → import_from_link（需本地已订阅）
          · 直链 .zip / 图片 / 视频 / 网页 → import_from_link
        """
        try:
            sm = self._skin_manager()
            if sm is None:
                return {"ok": False, "error": "SkinManager 不可用"}
            src = (source or "").strip()
            if not src:
                return {"ok": False, "error": "source 为空"}
            looks_local = re.match(r"^[a-zA-Z]:[\\/]", src) or src.startswith(
                ("file://", "/", "\\\\"))
            if looks_local:
                p = Path(src[7:] if src.startswith("file://") else src)
                if not p.exists() or not (p / "project.json").exists():
                    return {"ok": False,
                            "error": f"找不到壁纸引擎工程 project.json：{p}"}
                key = sm.import_wallpaper_folder(p, name or None, copy=False)
            else:
                key = sm.import_from_link(src, name or None)
            entry = self._wallpaper_entry(
                {"key": key, "name": name or src, "kind": "wallpaper_engine",
                 "preview": "", "background": ""})
            self._attach_descriptor(sm, key, entry)
            return {"ok": True, "key": key, "name": entry.get("name") or key,
                    "url": entry.get("url") or "", "type": entry.get("type"),
                    "media": entry.get("media"), "descriptor": entry.get("descriptor")}
        except Exception as e:  # noqa
            logger.warning("import_wallpaper_engine 失败: %s: %s",
                           type(e).__name__, e)
            return {"ok": False, "error": str(e)[:200]}

    def import_wallpaper_zip(self, filename: str = "", data: bytes = b"") -> dict:
        """浏览器上传的壁纸引擎 zip / 单媒体包 → 解包导入（自动激活）。"""
        import tempfile
        import zipfile
        try:
            if not data:
                return {"ok": False, "error": "空文件"}
            sm = self._skin_manager()
            if sm is None:
                return {"ok": False, "error": "SkinManager 不可用"}
            fd, tmp = tempfile.mkstemp(
                suffix=Path(filename or "wallpaper.zip").suffix.lower() or ".zip",
                prefix="wpzip_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                key = sm._importer._import_zip_file(tmp, None)
                # _import_zip_file 内部走 WallpaperImporter.import_*（不经
                # SkinManager finish）→ 这里显式补注册表刷新 + 自动激活
                sm.reload()
                if key and key in sm._all:
                    sm.set_active(key)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            # 从注册表取真实条目信息
            from core.plugin import PROJECT_ROOT
            item = {"key": key, "name": key, "kind": "wallpaper_engine",
                    "preview": "", "background": ""}
            try:
                meta = sm.list_skins()
                hit = next((m for m in meta if m.get("key") == key), None)
                if hit:
                    item = hit
            except Exception:  # noqa
                pass
            entry = self._wallpaper_entry(item)
            self._attach_descriptor(sm, key, entry)
            return {"ok": True, "key": key, "name": entry.get("name") or key,
                    "url": entry.get("url") or "", "type": entry.get("type"),
                    "media": entry.get("media"), "descriptor": entry.get("descriptor")}
        except Exception as e:  # noqa
            logger.warning("import_wallpaper_zip 失败: %s: %s",
                           type(e).__name__, e)
            return {"ok": False, "error": str(e)[:200]}


# 全局单例
bridge = RadioBridge()
