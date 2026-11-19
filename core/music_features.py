"""R99-G3 **8 维音乐特征**（librosa，零新依赖）—— 标定链第 ① 环「音乐维度」。

出处与定位：
  · `docs/master_plan.md` Phase 1 序 7（G3）
  · `docs/water_speed_amp_gems_research.md` §4.4（8 维表 + librosa 实现 + 实测 venv 已装）
  · `docs/calibration_chain_research.md` §2.1（定标方式：**物理量纲层** + **语料分位层**）

标定链全景里这一环此前是 **❌ 完全不存在**（`calibration_chain_research.md` §1）：
```
音频 ──CLAP/YAMNet──> 512 维嵌入
   ① ──8 维音乐特征（librosa）──> 音乐维度       ← ❌ 本模块补上
   ② ──project() + 分位数──> 14 轴 / GEMS 情绪值
   ③ ──SEM_MAP + 平均 + ×0.6 + ±50%──> 水波参数
```

8 维与实现（"现状"列是 G3 之前的盘点）：

| 维度 | 特征 | 本模块实现 | 定标层 | 旧现状 |
|---|---|---|---|---|
| Rhythm 节奏 | BPM / onset 密度 / 拍间隔规律性 | `beat.beat_track` + `onset.onset_detect` | 物理量纲 | 已有（Beat This） |
| Dynamics 力度 | RMS(dBFS) / 动态范围(dB) | `feature.rms` + 分位数 | 物理量纲 | 已有 `env.rms` |
| Melody 旋律 | 音域(半音) / 音程直方图熵 | `pyin` → f0 → 12 音程直方图 | 物理量纲 | 缺 |
| Harmony 和声 | 调性清晰度 / 和声变化率 | `chroma_cqt` + Krumhansl–Schmuckler 模板 | 物理量纲 | 缺 |
| Tone 音色 | 谱心/带宽/滚降/平坦度 | `spectral_*` | 语料分位 | 缺 |
| Expressivity 表情 | 颤音深度 / 滑音率 / 力度起伏 | f0 调制谱 + f0 斜率 + rms 变异 | 语料分位 | 缺 |
| Texture 织体 | 谐波打击比 / 复音度 | `decompose.hpss` + `poly_features` | 语料分位 | 缺 |
| Form 结构 | 重复率 / 段落新奇度 | 段嵌入自相似矩阵（SSM） | 物理量纲 | 半有 |

设计纪律（照抄既有离线索引模块 `core/beat_events.py` 的三条）：
  1. **纯离线**：只在 mood 线程首播时跑一次，结果入 `track_features` 表，
     播放路径**只查表零推理**；
  2. **幂等**：已有表且版本一致 → 直接返回，不重算；
  3. **总开关**：`MUSICAI_FEATURES_OFF=1` 一键关（回退口），且**绝不影响播放**。
  ⚠ RT 让步：pYIN / HPSS 是 CPU 重活 ⇒ 分块 + 块间 `sleep`（R80 纪律，同 CLAP 段索引）。

⚠ **本模块只做"物理量纲层"的 [0,1] 归一（可解释、可复现）；
   "语料分位层"是 K3（Phase 5 序 22），不在本轮** —— 归一公式全部写在
   各 `_dim_*` 的 docstring 里，带常数与出处，供 K3 直接复用/替换。

对外：
  - `extract(y, sr, seg_vecs=None) -> dict`（纯函数，内存数组 → 8 维）
  - `index_track(track_id, path, db, seg_vecs=None) -> bool`（离线入表，幂等）
  - `load_features(db, track_id) -> dict | None`
  - `enabled() -> bool`
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger("MusicAI.MusicFeatures")

# ---------------------------------------------------------------------------
# 常量（全部带出处/理由；改动必须同步改 verify_g3_features.py 的锚点）
# ---------------------------------------------------------------------------
FEAT_SR = 22050            # 与 core/beat_events.BEAT_SR 一致（librosa.load 默认）
MIN_AUDIO_S = 5.0          # <5s 不分析（同 beats：太短没有统计意义）
MAX_ANALYSIS_S = 120.0     # 分析窗上限：pYIN 是 O(n·阈值数)，4min 全曲会拖 mood 线程
ANALYSIS_HOP_S = 1.0       # ≥120s 时取**三段拼**（头/中/尾各 1/3 窗）→ 覆盖结构
RT_SLEEP = 0.05            # 重活块间让步（R80 纪律）
FEAT_VERSION = "g3.v1"     # 落库版本号（K3 定标时按它失效重建）

# --- Rhythm ---
BPM_LO, BPM_HI = 40.0, 200.0        # 归一区间（人耳可感 BPM 常见跨度）
DENS_CAP = 8.0                      # onset 密度上限（onsets/s，超过按 1 计）
# 注：曾加过"八度折叠"（80~160），实测把 60BPM 折成 120 ⇒ 已移除，见 _ry docstring
# --- Dynamics ---
DBFS_LO, DBFS_HI = -60.0, 0.0       # dBFS 归一区间（16bit 有效动态）
DYN_RANGE_CAP = 30.0                # 动态范围上限（dB）
# --- Melody ---
PYIN_FMIN, PYIN_FMAX = 70.0, 1200.0  # 同 beat_events（人声+常见乐器基频域）
PITCH_RANGE_CAP = 24.0              # 音域上限（半音，2 个八度）
VOICED_MIN = 0.40                   # 有声帧占比下限：低于此 ⇒ **判"无旋律"，dim 置 None**
                                    # （实测：白噪被 pYIN 硬造出 f0，voiced 仅 0.29）
# --- Harmony ---
CHORD_RATE_CAP = 0.30               # 和声变化率上限（chroma 帧间 L1，经验值）
# --- Tone ---
CENTROID_CAP = 6000.0               # 谱心上限（Hz，22050Hz 奈奎斯特 11025）
BW_CAP = 4000.0                     # 谱带宽上限（Hz）
ROLLOFF_CAP = 8000.0                # 谱滚降上限（Hz，85% 能量点）
FLAT_GAMMA = 0.5                    # 平坦度压缩指数：flatness 重尾（乐音 ~1e-3、噪声 ~1）
# --- Expressivity ---
VIB_LO, VIB_HI = 3.0, 8.0           # 颤音频带（Hz；人声 vibrato 典型 4~7Hz）
VIB_DEPTH_CAP = 0.6                 # 颤音深度上限（半音，**峰值** +A/-A；±0.6 已是强颤音）
VIB_SCAN_STEP = 0.1                 # 带内扫频步长（Hz）
GLISS_CAP = 6.0                     # 滑音率上限（半音/秒）
GLISS_SMOOTH_S = 0.20               # 滑音率先做 0.2s 滑动平均 —— 否则和弦的
                                    # f0 跳变（实测三和弦逐帧跳 4 半音）会被误判成滑音
RMS_CV_CAP = 1.0                    # 力度起伏上限（std/mean）
# --- Texture ---
PEAK_LO, PEAK_HI = 1.0, 20.0        # 每帧显著谱峰数归一区间（纯音 1 / 密集织体 ≥20）
PEAK_REL = 0.10                     # "显著" = 幅度 ≥ 帧最大值 ×PEAK_REL
# --- Form ---
N_SEG_MIN = 4                       # 少于 4 段不出 form（SSM 无统计意义）
RECUR_THR = 0.90                    # 余弦相似度阈值：>thr 视为"重复"
N_BOUNDARIES_CAP = 6.0              # 段落边界数上限（超过按 1 计）

DIMS = ("rhythm", "dynamics", "melody", "harmony",
        "tone", "expressivity", "texture", "form")


def enabled() -> bool:
    """总开关：`MUSICAI_FEATURES_OFF=1` 一键关（与 Beat This 同构的回退口）。"""
    return os.environ.get("MUSICAI_FEATURES_OFF") != "1"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _clip01(x) -> float:
    try:
        v = float(x)
    except Exception:  # noqa
        return 0.0
    if not np.isfinite(v):
        return 0.0
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _lin(x, lo, hi) -> float:
    """线性归一到 [0,1] 后截断（物理量纲层的统一做法）。"""
    return _clip01((float(x) - lo) / (hi - lo)) if hi > lo else 0.0


def _safe(fn, name):
    """单维失败不影响其他维（该维 `dim` 留 None）——绝不让特征提取拖垮播放。"""
    try:
        return fn()
    except Exception as e:  # noqa
        logger.warning("[Feat] %s 维提取失败：%s", name, e)
        return None


def _analysis_window(y: np.ndarray, sr: int) -> np.ndarray:
    """>MAX_ANALYSIS_S 的曲子取「头 1/3 + 中 1/3 + 尾 1/3」各 1/3 窗拼接。

    理由：单一「只取前 120s」会让 form（结构）与 dynamics（副歌）失真；
    三段拼在**同样的计算量**下把结构覆盖率从 1/3 提到 1（首尾各 40s）。
    """
    n = int(y.size)
    lim = int(MAX_ANALYSIS_S * sr)
    if n <= lim:
        return y
    w = lim // 3
    a = y[:w]
    m0 = max(0, (n - w) // 2)
    b = y[m0:m0 + w]
    c = y[n - w:]
    return np.concatenate([a, b, c])


def _pitch_track(y, sr):
    """**一次** pYIN，供 melody 与 expressivity 共用（pYIN 是全套最贵的算子）。

    实测（8s 音频，i7）：单次 pYIN ≈ 0.5s；melody/expressivity 各跑一次 ⇒ 直接翻倍。
    """
    import librosa
    f0, vo, _ = librosa.pyin(y, fmin=PYIN_FMIN, fmax=PYIN_FMAX, sr=sr,
                             frame_length=2048, hop_length=512)
    f0 = np.asarray(f0, dtype=float)
    vo = np.asarray(vo, dtype=bool) if vo is not None else np.zeros_like(f0, dtype=bool)
    hop_t = 512.0 / float(sr)
    return f0, vo, hop_t


def _ks_profiles() -> np.ndarray:
    """Krumhansl–Schmuckler 调性模板（24 × 12：大调 12 + 小调 12），行已 z-归一。

    出处：Krumhansl (1990) *Cognitive Foundations of Musical Pitch*；
    大调 [6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88]
    小调 [6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17]
    —— 与 `librosa` 官方示例 (`--key` 的 KS 实现) 同值。
    """
    maj = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    mino = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    p = np.vstack([np.roll(maj, -k) for k in range(12)]
                  + [np.roll(mino, -k) for k in range(12)])
    p = (p - p.mean(axis=1, keepdims=True)) / (p.std(axis=1, keepdims=True) + 1e-9)
    return p


# ---------------------------------------------------------------------------
# 各维提取（**只算物理量**；[0,1] 归一在 `_dims` 里统一做，便于 K3 替换）
# ---------------------------------------------------------------------------
def _ry(y, sr):
    """Rhythm：BPM / onset 密度 / 拍间隔规律性。

    ⚠ **不做八度折叠**（曾试过折进 80~160：实测把确凿的 60BPM 点击串折成
    120 ⇒ 反而制造错误）。`beat_track` 的 octave error（180 被估成 89）是
    跟踪器本身的选择，**如实记录**；K3 的语料分位层对"每曲单调"的量纲天然
    稳健，不需要在这里修。
    """
    import librosa
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.asarray(tempo).reshape(-1)[0]) if np.size(tempo) else 0.0
    t = librosa.frames_to_time(beats, sr=sr)
    iv = np.diff(t)
    if iv.size >= 4:
        mu = float(np.mean(iv))
        # 规律性 = 1 − 变异系数（CV）；等间隔 → 1
        reg = _clip01(1.0 - (float(np.std(iv)) / (mu + 1e-9)))
    else:
        reg = 0.0
    onsets = librosa.onset.onset_detect(y=y, sr=sr)
    dens = float(onsets.size) / max(1e-6, y.size / float(sr))
    return {"bpm": bpm, "onset_density": dens, "beat_reg": reg}


def _dy(y, sr):
    """Dynamics：整曲 RMS(dBFS) + 动态范围(dB)。"""
    import librosa
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    rms = rms[rms > 1e-9]
    if rms.size == 0:
        return {"rms_dbfs": -120.0, "dyn_range_db": 0.0}
    mean_db = 20.0 * np.log10(float(np.mean(rms)) + 1e-12)
    p95 = float(np.percentile(rms, 95))
    p5 = float(np.percentile(rms, 5))
    rng = 20.0 * np.log10((p95 + 1e-12) / (p5 + 1e-12))
    return {"rms_dbfs": float(mean_db), "dyn_range_db": float(max(0.0, rng))}


def _me(f0, vo, hop_t):
    """Melody：f0 → 音域(半音) + 音程直方图熵。

    ⚠ **voiced 门控**：pYIN 在噪声/纯打击乐上会硬造 f0（实测白噪 voiced 仅
    0.29 却给出 5.7 半音"音域"）⇒ `voiced < VOICED_MIN` 时把 `voiced_ok`
    置 False，由 `_dims` 把 melody 判为 **None**（没有旋律就不给数，不猜）。
    """
    v = f0[vo & np.isfinite(f0) & (f0 > 0)]
    vfr = float(v.size) / float(max(1, f0.size))
    if v.size < 8:
        return {"pitch_range_semi": 0.0, "interval_entropy": 0.0,
                "voiced": vfr, "voiced_ok": False}
    p5, p95 = np.percentile(v, 5), np.percentile(v, 95)
    rng = 12.0 * np.log2((p95 + 1e-9) / (p5 + 1e-9))
    # 音程直方图（相邻帧半音差取 12 模）→ 归一化香农熵
    d = np.diff(12.0 * np.log2(v[1:] / (v[:-1] + 1e-9)))
    d = np.round(d).astype(int) % 12
    hist = np.bincount(d, minlength=12).astype(float)
    hist /= (hist.sum() + 1e-9)
    nz = hist[hist > 0]
    ent = float(-(nz * np.log(nz)).sum() / np.log(12.0))
    return {"pitch_range_semi": float(max(0.0, rng)),
            "interval_entropy": float(_clip01(ent)),
            "voiced": vfr, "voiced_ok": bool(vfr >= VOICED_MIN)}


def _ha(y, sr):
    """Harmony：调性清晰度（KS 相关）+ 和声变化率。"""
    import librosa
    ch = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    if ch.size == 0 or ch.shape[1] < 4:
        return {"key_clarity": 0.0, "chord_rate": 0.0}
    m = ch.mean(axis=1)
    m = m / (np.linalg.norm(m) + 1e-9)
    prof = _ks_profiles()
    corr = prof @ m                       # 行已 z-归一 ⇒ 点积 = Pearson r
    clarity = _clip01(float(np.max(corr)))
    # 和声变化率 = chroma 帧间 L1 距离均值（列先 L1 归一，去响度影响）
    c = ch / (np.linalg.norm(ch, ord=1, axis=0, keepdims=True) + 1e-9)
    rate = float(np.mean(np.abs(np.diff(c, axis=1))))
    return {"key_clarity": clarity, "chord_rate": rate}


def _to(y, sr):
    """Tone：谱心 / 带宽 / 滚降 / 平坦度（全部无天然零点 ⇒ K3 走语料分位）。"""
    import librosa
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512)) ** 2
    cent = float(np.mean(librosa.feature.spectral_centroid(S=S, sr=sr)))
    bw = float(np.mean(librosa.feature.spectral_bandwidth(S=S, sr=sr)))
    roll = float(np.mean(librosa.feature.spectral_rolloff(S=S, sr=sr, roll_percent=0.85)))
    flat = float(np.mean(librosa.feature.spectral_flatness(S=S)))
    return {"centroid_hz": cent, "bandwidth_hz": bw,
            "rolloff_hz": roll, "flatness": flat}


def _ex(f0, vo, hop_t, y, sr):
    """Expressivity：颤音深度(半音) / 滑音率(半音每秒) / 力度起伏(CV)。

    ★ 颤音**不用"带内能量占比"** —— 实测：稳态正弦（无颤音）因为 pYIN 的
    逐帧抖动，去趋势残差里 3~8Hz 带占比高达 **0.77**，与真颤音（1.0）几乎
    分不开 ⇒ 假阳性。改用**显式解调**：在 [VIB_LO, VIB_HI] 内扫频，取最佳
    正弦拟合的**峰值幅度 A（半音，即 ±A）**作为颤音深度
    —— 实测稳态正弦 ≈0.008、5.5Hz/±0.5 半音的颤音 ≈0.5 ⇒ 区分度 ~60×。
    ★ 滑音率先过 0.2s 滑动平均：实测三和弦的 pYIN 会在三个音之间来回跳
    （逐帧 Δ≈4 半音），不平滑会被误判成"超强滑音"。
    """
    import librosa
    vib, gliss = 0.0, 0.0
    if np.count_nonzero(vo) >= 32:
        v = f0.copy()
        idx = np.arange(v.size)
        ok = vo & np.isfinite(v) & (v > 0)
        if np.count_nonzero(ok) >= 32:
            v[~ok] = np.interp(idx[~ok], idx[ok], v[ok])
        semi = 12.0 * np.log2(np.maximum(v, 1e-6))
        ac = semi - np.polyval(np.polyfit(idx, semi, 1), idx)   # 去线性趋势（滑音）
        t = idx * hop_t
        best = 0.0
        for f in np.arange(VIB_LO, VIB_HI + 1e-9, VIB_SCAN_STEP):
            e = np.exp(-2j * np.pi * f * t)
            # 单频最小二乘幅度（含常数项）→ 取峰峰值 = 2·|A|
            a = float(np.abs(2.0 * np.vdot(e, ac - ac.mean())) / max(1, ac.size))
            if a > best:
                best = a
        vib = float(best)
        # 滑音率：0.2s 滑动平均后的 |d(semi)/dt| 中位数（半音/秒）
        W = max(3, int(GLISS_SMOOTH_S / max(1e-6, hop_t)))
        if semi.size > W * 2:
            sm = np.convolve(semi, np.ones(W) / float(W), mode="valid")
            gliss = float(np.median(np.abs(np.diff(sm))) / hop_t)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    mu = float(np.mean(rms))
    cv = float(np.std(rms)) / (mu + 1e-12) if mu > 1e-9 else 0.0
    return {"vibrato_semi": vib, "gliss_rate": gliss, "rms_cv": cv}


def _te(y, sr):
    """Texture：谐波/打击比（HPSS）+ 显著谱峰数（复音度代理）。

    ⚠ 不用 `librosa.feature.poly_features`：它返回的是多项式拟合系数，量级
    随幅度/分辨率漂移（实测 1e-3 级），跨曲不可比 ⇒ 换**每帧显著谱峰数**
    （纯音 1 / 三和弦 3 / 密集织体 ≥20），量纲明确、可断言。
    """
    import librosa
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    if S.size == 0:
        return {"harmonic_ratio": 0.5, "peak_count": 1.0}
    H, P = librosa.decompose.hpss(S, margin=2.0)
    eh = float(np.sum(H ** 2))
    ep = float(np.sum(P ** 2))
    ratio = eh / (eh + ep + 1e-12)
    mx = np.max(S, axis=0, keepdims=True)
    sig = S >= (mx * PEAK_REL + 1e-12)
    # 逐帧数"连通峰"：显著点里，比左邻低→升沿起点的个数（≈峰个数）
    prev = np.zeros(S.shape[1], dtype=bool)
    cnt = 0
    for i in range(S.shape[1]):
        cur = sig[:, i]
        rise = cur & ~np.concatenate(([False], cur[:-1]))
        cnt += int(np.count_nonzero(rise))
        prev = cur
    n_fr = max(1, S.shape[1])
    return {"harmonic_ratio": float(ratio), "peak_count": float(cnt) / n_fr}


def _fo(seg_vecs):
    """Form：段嵌入自相似矩阵（SSM）→ 重复率 + 新奇度峰数。

    段嵌入来自 `track_segments`（CLAP，已 L2 归一）⇒ 余弦相似度 = 点积。
    · 重复率 = 上三角（|i−j|≥2，排除自邻）中 >RECUR_THR 的单元占比
    · 新奇度 = SSM 对角线上 `librosa.segment.recurrence_matrix` 的
      路径增强 + `subsegment` 峰计数（简化：用滞后自相似下降点计数）
    无段（<N_SEG_MIN）→ None（**不猜**：没有数据就不要造一个数）。
    """
    if not seg_vecs:
        return None
    V = np.asarray([np.asarray(v, dtype=float).reshape(-1) for v in seg_vecs])
    if V.ndim != 2 or V.shape[0] < N_SEG_MIN or V.shape[1] < 2:
        return None
    nrm = np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    V = V / nrm
    S = V @ V.T
    n = S.shape[0]
    iu = np.triu_indices(n, k=2)                 # |i−j| ≥ 2（邻段天然相似，不算结构）
    if iu[0].size == 0:
        return None
    rep = float(np.mean(S[iu] > RECUR_THR))
    # 新奇度：相邻滞后 k=1 的自相似序列的低谷数（谷 = 段落边界）
    lag = np.diag(S, k=1)
    if lag.size >= 3:
        d1 = np.diff(lag)
        # 局部极小（前后都比它高）计 1 个边界
        bd = int(np.sum((d1[:-1] < 0) & (d1[1:] > 0)))
    else:
        bd = 0
    return {"repeat_rate": _clip01(rep), "n_boundaries": bd}


# ---------------------------------------------------------------------------
# [0,1] 归一（**物理量纲层**；K3 会用语料分位层替换，公式与常数留在这里作锚）
# ---------------------------------------------------------------------------
def _dims(raw: dict) -> dict:
    d = {}
    # Rhythm：BPM 与 onset 密度各 .4，规律性 .2（规律性只是"整齐度"，不是"快"）
    r = raw.get("rhythm")
    d["rhythm"] = None if not r else round(
        0.4 * _lin(r["bpm"], BPM_LO, BPM_HI)
        + 0.4 * _lin(r["onset_density"], 0.0, DENS_CAP)
        + 0.2 * _clip01(r["beat_reg"]), 4)
    y_ = raw.get("dynamics")
    d["dynamics"] = None if not y_ else round(
        0.5 * _lin(y_["rms_dbfs"], DBFS_LO, DBFS_HI)
        + 0.5 * _lin(y_["dyn_range_db"], 0.0, DYN_RANGE_CAP), 4)
    m = raw.get("melody")
    # voiced 门控：pYIN 在噪声/纯打击上造不出可信 f0 ⇒ **不猜**，给 None
    d["melody"] = None if (not m or not m.get("voiced_ok", True)) else round(
        0.5 * _lin(m["pitch_range_semi"], 0.0, PITCH_RANGE_CAP)
        + 0.5 * _clip01(m["interval_entropy"]), 4)
    h = raw.get("harmony")
    d["harmony"] = None if not h else round(
        0.6 * _clip01(h["key_clarity"])
        + 0.4 * _lin(h["chord_rate"], 0.0, CHORD_RATE_CAP), 4)
    t = raw.get("tone")
    d["tone"] = None if not t else round((
        _lin(t["centroid_hz"], 0.0, CENTROID_CAP)
        + _lin(t["bandwidth_hz"], 0.0, BW_CAP)
        + _lin(t["rolloff_hz"], 0.0, ROLLOFF_CAP)
        # 平坦度重尾（乐音 ~1e-3、白噪 ~1）⇒ 幂压缩后再归一（γ=FLAT_GAMMA）
        + _clip01(max(0.0, t["flatness"]) ** FLAT_GAMMA)
    ) / 4.0, 4)
    e = raw.get("expressivity")
    d["expressivity"] = None if not e else round((
        _lin(e["vibrato_semi"], 0.0, VIB_DEPTH_CAP)
        + _lin(e["gliss_rate"], 0.0, GLISS_CAP)
        + _lin(e["rms_cv"], 0.0, RMS_CV_CAP)
    ) / 3.0, 4)
    x = raw.get("texture")
    d["texture"] = None if not x else round(
        0.5 * _clip01(x["harmonic_ratio"])
        + 0.5 * _lin(x["peak_count"], PEAK_LO, PEAK_HI), 4)
    f = raw.get("form")
    d["form"] = None if not f else round(
        0.5 * _clip01(f["repeat_rate"])
        + 0.5 * _lin(f["n_boundaries"], 0.0, N_BOUNDARIES_CAP), 4)
    return d


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def extract(y, sr: int = FEAT_SR, seg_vecs=None, with_form: bool = True) -> dict:
    """8 维音乐特征（**纯函数**：numpy 数组进，dict 出；不碰 DB / 不碰文件）。

    返回 `{version, sr, dur_s, raw:{…}, dim:{8 个 0~1 或 None}}`。
    单维失败 → 该维 `dim` 为 `None`（其余照常返回），**绝不整体抛**。
    """
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    sr = int(sr)
    dur = float(y.size) / float(sr)
    out = {"version": FEAT_VERSION, "sr": sr, "dur_s": round(dur, 3),
           "raw": {}, "dim": {k: None for k in DIMS}}
    if y.size < int(MIN_AUDIO_S * sr):
        out["skipped"] = "too_short"
        return out
    if not np.any(np.isfinite(y)) or float(np.max(np.abs(y))) < 1e-6:
        out["skipped"] = "silent"
        return out
    y = _analysis_window(y, sr)
    raw = {}
    for name, fn in (("rhythm", _ry), ("dynamics", _dy),
                     ("harmony", _ha), ("tone", _to)):
        r = _safe(lambda fn=fn: fn(y, sr), name)
        if r is not None:
            raw[name] = r
    # pYIN **只跑一次**，melody / expressivity 共用（它是全套最贵的算子）
    f0, vo, hop_t = _safe(lambda: _pitch_track(y, sr), "pitch_track") \
        or (None, None, 512.0 / float(sr))
    r = _safe(lambda: _me(f0, vo, hop_t), "melody")
    if r is not None:
        raw["melody"] = r
    r = _safe(lambda: _ex(f0, vo, hop_t, y, sr), "expressivity")
    if r is not None:
        raw["expressivity"] = r
    r = _safe(lambda: _te(y, sr), "texture")
    if r is not None:
        raw["texture"] = r
    time.sleep(RT_SLEEP)              # RT 让步（pYIN/HPSS 是重活）
    if with_form:
        r = _safe(lambda: _fo(seg_vecs), "form")
        if r is not None:
            raw["form"] = r
    out["raw"] = {k: {kk: (round(float(vv), 6) if isinstance(vv, (int, float, np.floating))
                           else vv) for kk, vv in v.items()}
                  for k, v in raw.items()}
    out["dim"] = _dims(raw)
    return out


# ---------------------------------------------------------------------------
# 落库（与 beat_events.index_track 同构：幂等 + 静默失败）
# ---------------------------------------------------------------------------
def index_track(track_id: int, path: str, db, seg_vecs=None,
                force: bool = False) -> bool:
    """离线索引一首歌的 8 维特征 → `track_features` 表。

    幂等：已有表且 `version` 一致 → 直接 True（不重算）。
    开关关 / 文件缺失 / 太短 / 静音 → False（**绝不影响播放**）。
    """
    if not enabled():
        return False
    p = Path(path)
    if not p.exists():
        return False
    try:
        if not force and db.has_track_features(track_id):
            d = load_features(db, track_id)
            if d and d.get("version") == FEAT_VERSION:
                return True
        import librosa
        t0 = time.time()
        wav, _ = librosa.load(str(p), sr=FEAT_SR, mono=True, dtype=np.float32)
        wav = np.ascontiguousarray(np.asarray(wav, dtype=np.float32).reshape(-1))
        feats = extract(wav, FEAT_SR, seg_vecs=seg_vecs)
        if feats.get("skipped"):
            logger.info("[Feat] 跳过 track=%s（%s）", p.name, feats["skipped"])
            return False
        db.set_track_features(track_id, json.dumps(
            feats, separators=(",", ":"), ensure_ascii=False),
            version=FEAT_VERSION)
        logger.info("[Feat] 8 维特征入表 track=%s（%.0fs 音频，%.1fs）dim=%s",
                    p.name, feats["dur_s"], time.time() - t0,
                    {k: v for k, v in feats["dim"].items()})
        return True
    except Exception as e:  # noqa 索引失败不影响播放（G3 纯诊断/标定用）
        logger.warning("[Feat] 8 维特征提取失败 track=%s: %s", path, e)
        return False


def load_features(db, track_id: int) -> dict | None:
    """读某曲已索引的 8 维特征；无表/损坏 → None。"""
    try:
        raw = db.get_track_features(track_id)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:  # noqa
        logger.warning("[Feat] 特征解析失败 track=%s: %s", track_id, e)
        return None
