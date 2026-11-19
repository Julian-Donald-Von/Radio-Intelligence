"""R84a EVENTS 层：Beat This! 节拍/小节离线索引 + 每拍旋律·力度·密度特征。

用户拍板（docs/water_research_emotional.md §19.3/§19.5）：EVENTS 层**不做
音频响应**——beat/downbeat 网格绝不直接打波纹；离线抓取节拍网格 + 每拍的
力度(rms)/旋律音高(f0)/音符密度(onset) 特征入表，供前端按本地播放时钟做
**亚秒级情绪微动态**（补充 L0 CLAP 段语义 5s / L1 YAMNet 4s 粒度之间的
毫秒~秒级情绪变化）。

- 载体：Beat This!（ISMIR 2024, CPJKU）ONNX（ashudesai/songbird-models, MIT）：
    mel_spectrogram.onnx  audio_pcm(1,samples)@22050 → mel(1,time,128)
    small0.onnx           spectrogram(b,time,128) → beat/downbeat logits(b,time)
  帧率 50fps（20ms/帧）；本项目实证（2026-09-09）：120BPM 合成节拍 30s
  59/60 命中、间隔全 0.5s，阈值 0.4 无假拍；mel 64ms + beat 514ms / 30s
  （CPU）——纯离线成本，播放路径零推理。
- 与 CLAP 段索引同构：首播一次性入表 track_beats（data/db.py），此后只查表。
- 总开关：MUSICAI_BEAT_OFF=1 一键关（无 'beats' 帧 → 前端包络自动回中性）。
- RT 让步：30s 分块 + 块间 sleep(0.05)（R80 爆音纪律，同 CLAP 段索引）。
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger("MusicAI.BeatEvents")

BEAT_SR = 22050            # Beat This! 输入采样率（mel 模型约定）
CHUNK_S = 30.0             # 分块时长（模型按 ~30s 上下文训练/验证）
PEAK_THR = 0.4             # sigmoid 峰值阈值（实证 0.5 会漏弱拍、0.3 生边界假拍）
PEAK_MIN_GAP_S = 0.2       # 相邻拍最小间隔（≈300BPM 上限）
MIN_BEATS = 8              # 少于此拍数的索引视为无果（过短/检测失败）
DENS_CAP = 6               # 每拍音符密度上限（onset 计数截断）
PYIN_FMIN, PYIN_FMAX = 70.0, 1200.0
# ★ R119-2t：整曲 beats 子进程索引的超时上限（秒）；超时即 kill 放弃
BEAT_SUBPROC_TIMEOUT_S = 300.0

MODELS_DIR = Path(__file__).resolve().parents[1] / "data_runtime" / "models"
MEL_PATH = MODELS_DIR / "mel_spectrogram.onnx"
BEAT_PATH = MODELS_DIR / "small0.onnx"

_mel_sess = None
_beat_sess = None


def beat_enabled() -> bool:
    """总开关：模型在盘且未设 MUSICAI_BEAT_OFF=1。"""
    return (MEL_PATH.exists() and BEAT_PATH.exists()
            and os.environ.get("MUSICAI_BEAT_OFF") != "1")


def _sessions():
    """懒加载 onnxruntime 会话（CPU provider；onnxruntime 已随 R83 入 venv）。"""
    global _mel_sess, _beat_sess
    if _mel_sess is None:
        import onnxruntime as ort
        opt = ort.SessionOptions()
        opt.intra_op_num_threads = 2
        _mel_sess = ort.InferenceSession(
            str(MEL_PATH), sess_options=opt, providers=["CPUExecutionProvider"])
        _beat_sess = ort.InferenceSession(
            str(BEAT_PATH), sess_options=opt, providers=["CPUExecutionProvider"])
    return _mel_sess, _beat_sess


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.asarray(x, dtype=np.float64)))


def _local_max_peaks(sig: np.ndarray, fps: float) -> list[int]:
    """一维局部极大峰值提取（阈值 + 最小间隔）。纯 numpy 邻域，无 scipy 依赖。"""
    out: list[int] = []
    min_gap = max(1, int(round(PEAK_MIN_GAP_S * fps)))
    last = -(10 ** 9)
    for i in range(1, len(sig) - 1):
        v = float(sig[i])
        if v >= PEAK_THR and v >= float(sig[i - 1]) and v >= float(sig[i + 1]) \
                and i - last >= min_gap:
            out.append(i)
            last = i
    return out


def _detect_chunk(wav: np.ndarray) -> tuple[list[float], list[int], float]:
    """单块音频 → (拍时刻 s, 是否小节头 0/1, 帧率 fps)。"""
    mel_sess, beat_sess = _sessions()
    mel = mel_sess.run(None, {"audio_pcm": wav.reshape(1, -1).astype(np.float32)})[0]
    b_logits, d_logits = beat_sess.run(None, {"spectrogram": mel})
    fps = float(mel.shape[1]) / max(1e-6, wav.size / float(BEAT_SR))
    b_sig = _sigmoid(b_logits[0])
    d_sig = _sigmoid(d_logits[0])
    frames = _local_max_peaks(b_sig, fps)
    t0 = time.time()
    del mel, b_logits, d_logits
    times = [round(f / fps, 3) for f in frames]
    downs = [1 if float(d_sig[f]) > 0.5 else 0 for f in frames]
    _ = t0
    return times, downs, fps


def _merge_chunks(chunks: list[tuple[list[float], list[int]]]) \
        -> tuple[list[float], list[int]]:
    """跨块合并拍列表：按时间排序，<0.2s 的近邻去重（保留分数高者，等价
    于保留先到者——分数信息不跨块，简单去重已足够）。"""
    pairs: list[tuple[float, int]] = []
    for ts, ds in chunks:
        pairs.extend(zip(ts, ds))
    pairs.sort(key=lambda p: p[0])
    out_t: list[float] = []
    out_d: list[int] = []
    for t, d in pairs:
        if out_t and t - out_t[-1] < PEAK_MIN_GAP_S:
            continue
        out_t.append(t)
        out_d.append(d)
    return out_t, out_d


def detect_beats(wav: np.ndarray, min_beats: int = MIN_BEATS) \
        -> tuple[list[float], list[int]]:
    """全曲音频（22050 单声道 float32）→ (拍时刻列表, 小节头 0/1 列表)。

    30s 分块推理 + 块间让步；过短（<MIN_BEATS 拍）返回空。
    """
    n = int(CHUNK_S * BEAT_SR)
    chunks = []
    for off in range(0, max(1, wav.size - BEAT_SR // 2), n):
        seg = np.ascontiguousarray(wav[off:off + n])
        if seg.size < BEAT_SR:                       # <1s 尾块无节拍意义，弃
            break
        ts, ds, _fps = _detect_chunk(seg)
        chunks.append(([round(t + off / BEAT_SR, 3) for t in ts], ds))
        time.sleep(0.05)                             # R80 RT 让步
    if not chunks:
        return [], []
    t, d = _merge_chunks(chunks)
    t = [x for x in t if x >= 0.15]                  # 掐块边界起始假拍
    while len(d) > len(t):
        d.pop()
    if len(t) < min_beats:
        return [], []
    return t, d


def _per_beat_features(wav: np.ndarray, t: list[float], dur_s: float) \
        -> tuple[list[float], list[int]]:
    """每拍特征：r=力度（区间 rms 均值 / 全曲 p95，clamp[0,1]）、
    f=旋律音高（区间 voiced 帧 f0 中位数 Hz，无声 0）、n=音符密度
    （区间 onset 计数，cap DENS_CAP）。"""
    import librosa
    hop = 441                                        # 50fps 特征网格
    frame_t = np.arange(0, wav.size, hop) / float(BEAT_SR)
    rms = librosa.feature.rms(y=wav, frame_length=2048, hop_length=hop)[0]
    n_frames = min(len(rms), len(frame_t))
    rms, frame_t = rms[:n_frames], frame_t[:n_frames]
    p95 = float(np.percentile(rms, 95)) if n_frames else 0.0
    onset_env = librosa.onset.onset_strength(y=wav, sr=BEAT_SR, hop_length=hop)
    onsets = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=BEAT_SR, hop_length=hop,
        units="time", backtrack=False)
    # f0 逐 30s 块 pyin（CPU 偏重，块间让步；边界帧拼接近似连续）
    f0_all = np.full(len(frame_t), np.nan)
    ch = int(CHUNK_S * BEAT_SR)
    for off in range(0, max(1, wav.size - BEAT_SR // 2), ch):
        if _cancelled():      # ★ R119-2r：**每块之间**查一次 ⇒ 起播即放弃（粒度≈1~2s）
            raise BeatsCancelled()
        seg = wav[off:off + ch]
        if seg.size < 2048:
            break
        f0 = librosa.pyin(y=seg, fmin=PYIN_FMIN, fmax=PYIN_FMAX, sr=BEAT_SR,
                          frame_length=2048, hop_length=hop)[0]
        i0 = off // hop
        k = min(len(f0), len(f0_all) - i0)
        if k > 0:
            f0_all[i0:i0 + k] = f0[:k]
        time.sleep(0.05)
    rs: list[float] = []
    fs: list[float] = []
    ns: list[int] = []
    bounds = list(t) + [max(dur_s, t[-1] + 0.4) if t else dur_s]
    for i in range(len(t)):
        a, b = bounds[i], bounds[i + 1]
        m = (frame_t >= a) & (frame_t < b)
        if m.any():
            rs.append(round(min(1.0, float(np.mean(rms[m])) / p95), 2) if p95 > 1e-9 else 0.0)
            fv = f0_all[m]
            fv = fv[~np.isnan(fv)]
            fs.append(round(float(np.median(fv)), 1) if fv.size else 0.0)
        else:
            rs.append(0.0)
            fs.append(0.0)
        ns.append(min(DENS_CAP, int(np.count_nonzero((onsets >= a) & (onsets < b)))))
    return rs, fs, ns


class BeatsCancelled(Exception):
    """★ R119-2r：整曲 beats 索引被**协作式抢占**（起播让路）。"""


_CANCEL_CHECK = None


def set_cancel_check(fn) -> None:
    """★ 注册 `fn() -> bool`：返回 True 表示"该让路了"，索引会**尽快放弃**。

    背景：`_per_beat_features` 里逐 30s 块的 `librosa.pyin` 是整曲最重的部分
    （真机实测整笔 **14.5 秒**）。而调用方的闸门只在**开始之前**判定一次 ⇒ 若索引
    开始于暂停期、用户中途按了播放，这 14.5 秒就会**跨过起播**、变成播放中的卡顿。
    本钩子让**运行中**的索引在每块之间查一次（粒度 = 一块 ≈ 1~2s），起播即放弃。
    放弃**不落盘**（本函数只在最后 `db.set_track_beats`）⇒ 幂等、可稍后重试。"""
    global _CANCEL_CHECK
    _CANCEL_CHECK = fn


def _cancelled() -> bool:
    try:
        return bool(_CANCEL_CHECK()) if _CANCEL_CHECK is not None else False
    except Exception:  # noqa
        return False


def _compute_beats_json(path: str):
    """★ R119-2t：**纯计算、绝不碰数据库** —— 整曲 beats → `(JSON 字符串, 拍数)`。

    抽出来的目的：让**子进程**与内联回退路径**共用同一份算法**（保证两者结果一致），
    并且子进程不需要任何数据库连接（JSON 由父进程落库 ⇒ 无多进程写争用）。
    返回 None = 无果/失败。`BeatsCancelled` 原样抛出（供内联路径识别）。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        import librosa
        wav, _ = librosa.load(str(p), sr=BEAT_SR, mono=True, dtype=np.float32)
        wav = np.ascontiguousarray(np.asarray(wav, dtype=np.float32).reshape(-1))
        if wav.size < BEAT_SR * 5:                   # <5s 不索引
            return None
        dur_s = wav.size / float(BEAT_SR)
        ts, ds = detect_beats(wav)
        if not ts:
            logger.info("[Beat] 节拍检测无果 track=%s（%.0fs）", p.name, dur_s)
            return None
        rs, fs, ns = _per_beat_features(wav, ts, dur_s)
        data = json.dumps({"v": 1, "sr": BEAT_SR, "t": ts, "d": ds,
                           "r": rs, "f": fs, "n": ns},
                          separators=(",", ":"), ensure_ascii=False)
        return data, len(ts)
    except BeatsCancelled:
        raise
    except Exception as e:  # noqa 索引失败不影响播放（EVENTS 层静默回退）
        logger.warning("[Beat] 节拍索引失败（计算阶段）track=%s: %s", path, e)
        return None


def _subproc_on() -> bool:
    """★ 默认走子进程（根治）；`MUSICAI_BEATS_SUBPROC=0` 回退内联实现。"""
    return os.environ.get("MUSICAI_BEATS_SUBPROC", "1") != "0"


def _index_inline(track_id: int, path: str, db) -> int:
    """内联实现（**回退口**）：整曲在本进程里算 —— 会占 GIL，只作兜底。"""
    try:
        r = _compute_beats_json(path)
        if r is None:
            return 0
        data, n = r
        db.set_track_beats(track_id, data, n)
        logger.info("[Beat] 节拍索引完成（内联）track=%s：%d 拍", Path(path).name, n)
        return n
    except BeatsCancelled:
        logger.info("[Beat] 起播抢占 → 放弃整曲 beats 索引（内联，%s，不落盘）",
                    Path(path).name)
        return -1
    except Exception as e:  # noqa
        logger.warning("[Beat] 节拍索引失败 track=%s: %s", path, e)
        return 0


def _index_via_subprocess(track_id: int, path: str, db) -> int:
    """★ R119-2t **根治路径**：整曲 beats 索引丢给**独立子进程**，父进程只轮询。

    父进程的等待是 `Popen.poll() + time.sleep(0.2)` ⇒ **释放 GIL**，
    所以"等它算完"这件事本身**不会造成任何卡顿**；
    要放弃就 `kill()` ⇒ 瞬时生效（不像内联路径得等下一个中断点）。"""
    import subprocess
    import tempfile
    out = os.path.join(tempfile.gettempdir(), "musicai_beats_%d.json" % int(track_id))
    try:
        os.remove(out)
    except Exception:  # noqa 不存在最好
        pass
    flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
    cmd = [sys.executable, "-m", "core.beat_events", "--compute", path, out]
    try:
        p = subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parents[1]),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
    except Exception as e:  # noqa 起不来就退回内联（宁慢勿断）
        logger.warning("[Beat] beats 子进程启动失败 → 回退内联: %s", e)
        return _index_inline(track_id, path, db)
    t0 = time.time()
    try:
        while True:
            if p.poll() is not None:
                break
            if _cancelled():
                try:
                    p.kill()
                except Exception:  # noqa
                    pass
                logger.info("[Beat] 起播抢占 → **kill** 整曲 beats 子进程（瞬时，不落盘）")
                return -1
            if time.time() - t0 > BEAT_SUBPROC_TIMEOUT_S:
                try:
                    p.kill()
                except Exception:  # noqa
                    pass
                logger.warning("[Beat] beats 子进程超时（%.0fs）→ 放弃",
                               BEAT_SUBPROC_TIMEOUT_S)
                return 0
            time.sleep(0.2)                 # 释放 GIL：等待期间父进程保持响应
    finally:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:  # noqa
            pass
    if p.returncode != 0 or not os.path.isfile(out):
        logger.warning("[Beat] beats 子进程未产出结果（rc=%s）", p.returncode)
        return 0
    try:
        data = io.open(out, encoding="utf-8").read()
        d = json.loads(data)
        if not isinstance(d, dict) or not d.get("t"):
            return 0
        db.set_track_beats(track_id, data, len(d["t"]))   # ★ 由**父进程**写库
        logger.info("[Beat] 节拍索引完成（子进程）track=%s：%d 拍（%.1fs）",
                    Path(path).name, len(d["t"]), time.time() - t0)
        return len(d["t"])
    except Exception as e:  # noqa
        logger.warning("[Beat] beats 结果解析/落库失败: %s", e)
        return 0
    finally:
        try:
            os.remove(out)
        except Exception:  # noqa
            pass


def index_track(track_id: int, path: str, db) -> int:
    """索引一首歌：beat/downbeat + 每拍特征 → JSON 入 track_beats 表。

    已有表（幂等）/开关关/文件缺失/检测无果 → 0；成功返回拍数。
    ★ R119-2t：默认把整曲计算放进**独立子进程**（见 `_index_via_subprocess` 注释），
    本进程只负责幂等检查 + 轮询 + 落库 ⇒ **零 GIL 占用**。
    `MUSICAI_BEATS_SUBPROC=0` 回退到内联实现。
    """
    if not beat_enabled():
        return 0
    if _cancelled():          # ★ R119-2r：已经该让路 ⇒ 连开头都别做
        return -1
    p = Path(path)
    if not p.exists():
        return 0
    try:
        if db.has_track_beats(track_id):             # 幂等：已有表只回拍数
            d = load_track_beats(db, track_id)
            return len(d["t"]) if d else 0
    except Exception:  # noqa 查表失败按"没有"处理，交给下面重算
        pass
    if _subproc_on():
        return _index_via_subprocess(track_id, path, db)
    return _index_inline(track_id, path, db)


def load_track_beats(db, track_id: int) -> dict | None:
    """读某曲已索引的 beats JSON → dict；无表/损坏 → None。"""
    try:
        raw = db.get_track_beats(track_id)
        if not raw:
            return None
        d = json.loads(raw)
        if isinstance(d, dict) and d.get("t"):
            return d
    except Exception as e:  # noqa
        logger.warning("[Beat] 节拍表读取失败 track=%s: %s", track_id, e)
    return None


# ---------------------------------------------------------------------------
# ★ R119-2t 子进程入口
# ---------------------------------------------------------------------------

def _cli() -> int:  # pragma: no cover
    """`python -m core.beat_events --compute <audio> <out.json>`：只算不落库。

    ★ 进子进程先把**进程优先级**降到 BELOW_NORMAL（别跟播放抢 CPU）。
      ⚠ 必须**显式声明 argtypes/restype** —— `GetCurrentProcess()` 返回伪句柄 `-1`，
        在 64 位下按 32 位传参会被截断成非法句柄、调用静默失败
        （这正是本会话第一个 bug 的同族，见 `core/audio_engine._win_kernel32`）。
    """
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--compute", nargs=2, metavar=("AUDIO", "OUT"))
    a = ap.parse_args()
    if not a.compute:
        return 2
    try:
        import ctypes
        from ctypes import wintypes
        _k = ctypes.WinDLL("kernel32", use_last_error=True)
        _k.GetCurrentProcess.argtypes = []
        _k.GetCurrentProcess.restype = wintypes.HANDLE
        _k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _k.SetPriorityClass.restype = wintypes.BOOL
        _k.SetPriorityClass(_k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL
    except Exception:  # noqa 纯调度提示
        pass
    r = _compute_beats_json(a.compute[0])
    if r is None:
        return 1
    io.open(a.compute[1], "w", encoding="utf-8").write(r[0])
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys as _sys
    _sys.exit(_cli())
