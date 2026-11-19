"""R83b L1 短期语义追踪：YAMNet(ONNX) → 线性映射对齐 CLAP 空间（研究文档 §18）。

三层语义时钟（§18.4）：
  L0 CLAP 段语义（离线索引 5s hop，播放零推理）—— 锚
  L1 本模块：YAMNet@L1_PERIOD_S（CPU 数 ms）→ 1024 维嵌入 → 映射矩阵 W
     （曲库段配对岭回归离线拟合，1024→512）→ 伪 CLAP 向量 → project()（复用
     R82c 分位数标定）→ 14 轴短时值 → EMA → fuse_axes() 与 L0 凸组合
  L2 前端 semStep 慢变量兜底（τ≈4s，不变）

设计依据：动态 MER 天花板 CCC 0.3~0.4（W28 DSAML）→ L1 只可信"趋势/相对
变化"，不可信绝对值 → 残差限幅 + 方向冲突守卫，绝不替代 L0。
RT 铁律：推理只跑 mood 线程（非 RT）；音频取自 FeatureAnalyzer 单声道环
（R83b），绝不新开采集路径。暂停 → 单声道环无新写入 → read_recent_mono
返回 None → L1 轴值冻结，不拿旧音频当新语义。

开关：MUSICAI_L1_OFF=1 一键关（回退纯 L0）；模型/映射缺失 → 自动静默回退。
"""
from __future__ import annotations

import logging
import os
import time
from math import gcd
from pathlib import Path

import numpy as np

logger = logging.getLogger("MusicAI.ShortTermSem")

YAMNET_SR = 16000          # YAMNet 输入采样率（mel 前端烧进图，仅要原始波形）
YAMNET_DIM = 1024          # YAMNet 嵌入维度
CLAP_DIM = 512

L1_PERIOD_S = 4.0          # L1 更新周期（决策点 3：4s 起步，真机再调）
L1_WEIGHT = 0.3            # 凸组合权重（决策点 5）
L1_RESIDUAL_MAX = 0.15     # 每轴残差限幅（决策点 5）
L1_CONFLICT = 0.4          # 方向冲突守卫：极性反且 |差| 超此值 → 只信 L0
EMA_ALPHA = 0.6            # L1 轴值 EMA（2 拍 ≈ 8s 全带，防抖）

_ROOT = Path(__file__).resolve().parents[1]
YAMNET_PATH = _ROOT / "data_runtime" / "models" / "yamnet.onnx"
MAP_PATH = _ROOT / "data_runtime" / "yamnet_map.npz"

FIT_MAX_TRACKS = 12        # 拟合抽样上限（曲）
FIT_MAX_PAIRS = 240        # 拟合配对上限（段）
FIT_MIN_PAIRS = 60
FIT_RIDGE = 1e-2
FIT_SLEEP = 0.02           # 段间让步（mood 线程内跑，给 RT/磁盘让路）
# --- R99-T2 留出法泛化验证 ---
HOLDOUT_FRAC = 0.25        # 留出**曲**比例（按曲切，不按段 —— 见 split_by_track）
HOLDOUT_SEED = 20260911    # 固定种子 ⇒ 切分可复现
EVAL_MAX_TRACKS = 40       # 评估抽样上限（曲；比拟合多，为留出集留样本）
EVAL_PATH = _ROOT / "data_runtime" / "yamnet_map_eval.json"
EVAL_VERSION = "t2.v1"


def l1_enabled() -> bool:
    """L1 总开关（MUSICAI_L1_OFF=1 关；模型/映射齐备才真正生效）。"""
    return os.environ.get("MUSICAI_L1_OFF", "0") != "1"


def fuse_axes(l0: dict | None, l1: dict | None,
              w: float = L1_WEIGHT, rmax: float = L1_RESIDUAL_MAX,
              conflict: float = L1_CONFLICT) -> dict:
    """L0 段轴与 L1 短时轴凸融合（残差限幅 + 方向冲突守卫）。纯函数，可单测。

    · 残差限幅：|l1−l0| 截到 ±rmax → 便宜模型的漂移永远撼不动段锚；
    · 方向冲突守卫：l1 与 l0 极性相反（跨 0.5 两侧）且 |差|>conflict → 该轴
      本拍只信 L0（趋势可能错，段语义是锚）；
    · L1 缺轴 → 该轴保持 L0。
    """
    if not l1:
        return dict(l0 or {})
    out: dict = {}
    for k, v0 in (l0 or {}).items():
        v1 = l1.get(k)
        try:
            v1 = float(v1)
        except (TypeError, ValueError):
            out[k] = v0
            continue
        d = v1 - float(v0)
        if (v1 - 0.5) * (float(v0) - 0.5) < 0 and abs(d) > conflict:
            out[k] = v0
            continue
        out[k] = round(max(0.0, min(1.0, float(v0) + w * max(-rmax, min(rmax, d)))), 4)
    return out


class ShortTermTracker:
    """YAMNet 短期语义追踪器（懒加载；所有方法仅限非 RT 线程）。"""

    def __init__(self) -> None:
        self._sess = None
        self._w: np.ndarray | None = None
        self._ema: dict[str, float] | None = None
        self._dead = False    # 模型/映射缺失或加载失败 → 本进程不再重试

    def ensure(self) -> bool:
        """就绪（onnxruntime 会话 + 映射矩阵）。不可用/关开关 → False。"""
        if self._sess is not None and self._w is not None:
            return True
        if self._dead or not l1_enabled():
            return False
        if not (YAMNET_PATH.exists() and MAP_PATH.exists()):
            self._dead = True
            return False
        try:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2
            so.inter_op_num_threads = 1
            self._sess = ort.InferenceSession(
                str(YAMNET_PATH), sess_options=so,
                providers=["CPUExecutionProvider"])
            z = np.load(MAP_PATH, allow_pickle=False)
            w = np.asarray(z["w"], dtype=np.float32)
            if w.shape != (YAMNET_DIM, CLAP_DIM):
                raise ValueError(f"映射矩阵形状异常: {w.shape}")
            self._w = w
            self._ema = None
            logger.info("[ShortSem] L1 就绪（yamnet.onnx + 映射 %d 对）",
                        int(z.get("n_pairs", 0)))
            return True
        except Exception as e:  # noqa 模型/运行时问题 → 静默回退纯 L0
            logger.warning("[ShortSem] L1 初始化失败（回退纯 L0）: %s", e)
            self._dead = True
            return False

    def infer(self, mono: np.ndarray, src_sr: int) -> dict | None:
        """单声道音频（stream SR）→ 16k 重采样 → YAMNet → 映射 → 轴值（EMA 后）。

        样本不足一帧窗（<1.2s）→ None；推理失败 → None（本拍跳过）。
        """
        if not self.ensure():
            return None
        try:
            import scipy.signal as sps
            from core.semantic_axis import project
            x = np.asarray(mono, dtype=np.float32).reshape(-1)
            if x.size < int(1.2 * src_sr):
                return None
            if src_sr != YAMNET_SR:
                g = gcd(int(src_sr), YAMNET_SR)
                x = sps.resample_poly(x, YAMNET_SR // g, src_sr // g)
            x = np.clip(x, -1.0, 1.0).astype(np.float32)
            outs = self._sess.run(None, {"waveform": x})
            v = np.asarray(outs[1], dtype=np.float32)   # [frames, 1024] 嵌入
            v = v.mean(axis=0)
            y = v @ self._w                              # 1024 → 512（伪 CLAP 空间）
            n = float(np.linalg.norm(y))
            y = y / n if n > 1e-9 else y
            axes = project(y)
            if self._ema is None:
                self._ema = dict(axes)
            else:
                for k in self._ema:
                    if k in axes:
                        self._ema[k] = round(
                            self._ema[k] + EMA_ALPHA * (axes[k] - self._ema[k]), 4)
            return dict(self._ema)
        except Exception as e:  # noqa 单拍失败不影响播放
            logger.warning("[ShortSem] L1 推理失败（本拍跳过）: %s", e)
            return None


def resolve_pair_audio(track_id, raw_path, path_resolver=None):
    """R0-C：把 `tracks.path` 解析成「可读音频文件」Path；不可得 → None。

    在线曲的 path 是 `qq://` / `netease://` 占位符，`Path.exists()` 恒假，
    所以拟合配对时必须允许调用方（RadioBridge）把曲目映射到**播放器落盘的临时
    流文件**。抽成纯函数便于单测（不触碰 onnx / librosa）。
    """
    try:
        p = Path(raw_path or "")
        if raw_path and p.exists():
            return p
    except Exception:  # noqa 非法路径
        pass
    if path_resolver is None:
        return None
    try:
        alt = path_resolver(track_id, raw_path)
    except Exception:  # noqa 解析失败按不可用处理
        return None
    if not alt:
        return None
    try:
        ap = Path(alt)
        return ap if ap.exists() else None
    except Exception:  # noqa
        return None


def segments_ready(db, min_pairs: int = FIT_MIN_PAIRS) -> bool:
    """R0-C：段表里的段数是否已达拟合下限（配对前的廉价前置门控）。

    为什么需要：`fit_mapping` 在配对不足时返回 0，而 mood 线程的 fit_tries
    是"每 30s 一次、共 5 次"的有限预算——首播只索引到 1 首曲（段数不足）就把
    重试次数烧光，之后即便又播了几首也再不会尝试 → 在线库的 L1 永远不就绪。
    先用总段数做上界门控（纯 SQL，零推理），够了才进入昂贵的拟合。
    """
    try:
        row = db.conn.execute("SELECT COUNT(*) FROM track_segments").fetchone()
    except Exception:  # noqa 无段表
        return False
    return bool(row) and int(row[0]) >= int(min_pairs)


def _open_yamnet():
    """打开 YAMNet ONNX 会话；模型缺失/ort 不可用 → None（静默回退）。"""
    if not YAMNET_PATH.exists():
        return None
    try:
        import onnxruntime as ort
        return ort.InferenceSession(str(YAMNET_PATH),
                                    providers=["CPUExecutionProvider"])
    except Exception as e:  # noqa
        logger.warning("[ShortSem] YAMNet 会话不可用: %s", e)
        return None


def _tracks_with_segments(db, limit: int):
    """按段数降序返回 [(track_id, n_seg)]；无段表 → []。"""
    try:
        return [(int(r["track_id"]), int(r["c"])) for r in db.conn.execute(
            "SELECT track_id, COUNT(*) AS c FROM track_segments "
            "GROUP BY track_id ORDER BY c DESC LIMIT ?", (int(limit),)).fetchall()]
    except Exception as e:  # noqa
        logger.warning("[ShortSem] 段表不可读: %s", e)
        return []


def collect_pairs(db, path_resolver=None, max_tracks: int = FIT_MAX_TRACKS,
                  max_pairs: int = FIT_MAX_PAIRS, track_ids=None,
                  sess=None, sleep: float = FIT_SLEEP,
                  embed_fn=None) -> list[tuple[int, np.ndarray, np.ndarray]]:
    """收集 (track_id, YAMNet 向量, CLAP 段向量) 配对。

    · CLAP 段向量来自 `track_segments`（已入库，零推理）；
    · YAMNet 向量现算（librosa 解码 + CPU 推理），`embed_fn` 可注入替换
      ⇒ **T2 的合成留出法测试就是靠它**（不依赖真实模型/音频）。
    · `track_ids` 非空 ⇒ 只收这些曲（留出法按曲切分后分别调用）。
    """
    if sess is None:
        if embed_fn is None:
            sess = _open_yamnet()
            if sess is None:
                return []
    try:
        import librosa
    except Exception as e:  # noqa
        if embed_fn is None:
            logger.warning("[ShortSem] librosa 不可用: %s", e)
            return []
        librosa = None  # noqa  合成模式不需要
    from core.semantic_axis import SEG_WIN_S
    rows = _tracks_with_segments(db, max_tracks)
    if track_ids is not None:
        want = set(int(t) for t in track_ids)
        rows = [r for r in rows if r[0] in want]
    out: list[tuple[int, np.ndarray, np.ndarray]] = []
    for tid, _c in rows:
        if len(out) >= max_pairs:
            break
        try:
            tp = db.conn.execute("SELECT path FROM tracks WHERE id=?",
                                 (tid,)).fetchone()
        except Exception:  # noqa
            continue
        if tp is None:
            continue
        path = tp["path"] if not isinstance(tp, dict) else tp.get("path")
        p = resolve_pair_audio(tid, path, path_resolver)
        if p is None and embed_fn is None:
            continue
        for row in db.get_track_segments(tid):
            if len(out) >= max_pairs:
                break
            ref = np.frombuffer(bytes(row["embedding"]), dtype=np.float16)
            if ref.size != CLAP_DIM:
                continue
            v = None
            if embed_fn is not None:
                v = embed_fn(tid, int(row["start_ms"]), ref)
            else:
                try:
                    wav, _ = librosa.load(str(p), sr=YAMNET_SR, mono=True,
                                          offset=int(row["start_ms"]) / 1000.0,
                                          duration=SEG_WIN_S, dtype=np.float32)
                except Exception:  # noqa 单段解码失败跳过
                    continue
                if wav.size < int(0.9 * YAMNET_SR * SEG_WIN_S):
                    continue
                try:
                    outs = sess.run(None, {"waveform": np.ascontiguousarray(wav)})
                    v = np.asarray(outs[1], dtype=np.float32).mean(axis=0)
                except Exception:  # noqa
                    continue
            if v is None:
                continue
            out.append((tid, np.asarray(v, dtype=np.float32).reshape(-1),
                        ref.astype(np.float32)))
            if sleep:
                time.sleep(sleep)
    return out


def fit_pairs(pairs, ridge: float = FIT_RIDGE) -> np.ndarray:
    """岭回归拟合 W [1024→512]；`pairs = [(tid, x, y)]`。

    与 R83b 原式**逐位一致**（`xᵀx + λI` 解 `xᵀy`），只是抽成可复用函数
    ⇒ T2 的留出法用**同一份代码**拟合训练集，不会出现"训练/评估两条实现"。
    """
    if not pairs:
        raise ValueError("fit_pairs: 配对为空")
    x = np.stack([p[1] for p in pairs]).astype(np.float64)
    y = np.stack([p[2] for p in pairs]).astype(np.float64)
    a = x.T @ x + float(ridge) * np.eye(x.shape[1], dtype=np.float64)
    return np.linalg.solve(a, x.T @ y).astype(np.float32)


def cos_of(w: np.ndarray, pairs) -> float:
    """映射后的伪 CLAP 向量与真 CLAP 段向量的**平均余弦相似度**。"""
    if not pairs:
        return 0.0
    x = np.stack([p[1] for p in pairs]).astype(np.float32)
    y = np.stack([p[2] for p in pairs]).astype(np.float32)
    yh = x @ w
    nh = np.linalg.norm(yh, axis=1, keepdims=True)
    ny = np.linalg.norm(y, axis=1, keepdims=True)
    return float(np.mean(np.sum((yh / np.maximum(nh, 1e-9))
                                * (y / np.maximum(ny, 1e-9)), axis=1)))


def cos_chance(pairs, seed: int = HOLDOUT_SEED) -> float:
    """**随机基线**：随机打乱配对后的平均余弦。

    用途：证明留出集上的 cos **显著高于"随便配"** —— 否则"cos=0.6"这个数
    本身没有意义（CLAP 段向量之间天然高相似）。
    """
    if len(pairs) < 2:
        return 0.0
    y = np.stack([p[2] for p in pairs]).astype(np.float32)
    rng = np.random.default_rng(int(seed))
    y2 = y[rng.permutation(len(pairs))]
    ny = np.linalg.norm(y, axis=1, keepdims=True)
    n2 = np.linalg.norm(y2, axis=1, keepdims=True)
    return float(np.mean(np.sum((y / np.maximum(ny, 1e-9))
                                * (y2 / np.maximum(n2, 1e-9)), axis=1)))


def split_by_track(pairs, holdout_frac: float = HOLDOUT_FRAC,
                   seed: int = HOLDOUT_SEED):
    """**按曲**切分训练/留出 —— ⚠ 绝不按段切：同一首歌的段若两边都有就是
    **数据泄漏**（记忆 §2：103 对拟合 52 万参，`cos=.9999` 只是训练自洽）。

    返回 (train_pairs, test_pairs, train_tracks, test_tracks)。
    """
    tids = sorted({int(p[0]) for p in pairs})
    rng = np.random.default_rng(int(seed))
    order = rng.permutation(len(tids))
    n_test = int(round(len(tids) * float(holdout_frac)))
    n_test = max(1, min(len(tids) - 1, n_test)) if len(tids) >= 2 else 0
    test_set = {tids[i] for i in order[:n_test]}
    train = [p for p in pairs if int(p[0]) not in test_set]
    test = [p for p in pairs if int(p[0]) in test_set]
    return train, test, sorted({int(p[0]) for p in train}), sorted(test_set)


def evaluate_mapping(pairs, ridge: float = FIT_RIDGE,
                     holdout_frac: float = HOLDOUT_FRAC,
                     seed: int = HOLDOUT_SEED) -> dict:
    """★ R99-T2 留出法泛化验证（纯函数：`pairs` 进，报告出）。

    判据（解锁 T6 的门）：
      · `cos_test` ≥ `cos_chance` + 0.10  ⇒ 学到的映射**有信息量**；
      · `gap = cos_train − cos_test` ≤ 0.15 ⇒ **没有严重过拟合**；
      · `n_test_tracks ≥ 3` ⇒ 留出集不是一两首歌的偶然。
    报告同时给**逐曲**留出 cos（min/p10/中位），便于定位"是哪首歌泛化差"。
    """
    if not pairs:
        return {"ok": False, "reason": "no_pairs"}
    train, test, tr_t, te_t = split_by_track(pairs, holdout_frac, seed)
    if not train or not test:
        return {"ok": False, "reason": "split_empty",
                "n_train": len(train), "n_test": len(test)}
    w = fit_pairs(train, ridge)
    c_tr, c_te = cos_of(w, train), cos_of(w, test)
    base = cos_chance(pairs, seed)
    per = {}
    for t in te_t:
        sub = [p for p in test if int(p[0]) == t]
        if sub:
            per[int(t)] = round(cos_of(w, sub), 4)
    vals = sorted(per.values())
    return {
        "ok": True, "version": EVAL_VERSION, "ridge": float(ridge),
        "holdout_frac": float(holdout_frac), "seed": int(seed),
        "n_pairs": len(pairs), "n_train": len(train), "n_test": len(test),
        "n_train_tracks": len(tr_t), "n_test_tracks": len(te_t),
        "cos_train": round(c_tr, 4), "cos_test": round(c_te, 4),
        "cos_chance": round(base, 4),
        "gap": round(c_tr - c_te, 4),
        "lift": round(c_te - base, 4),
        "per_track_test": per,
        "test_cos_min": round(vals[0], 4) if vals else None,
        "test_cos_median": round(vals[len(vals) // 2], 4) if vals else None,
        "verdict": ("PASS" if (c_te >= base + 0.10 and (c_tr - c_te) <= 0.15
                               and len(te_t) >= 3) else "FAIL"),
    }


def fit_mapping(db, path_resolver=None) -> int:
    """曲库段配对拟合 YAMNet→CLAP 空间映射 W（岭回归，1024→512）。返回配对数。

    配对来源 = track_segments 已存 CLAP 段向量（零 CLAP 推理）+ 同段音频的
    YAMNet 嵌入（librosa 解码 + CPU 推理）。在 mood 线程一次性调用（有段间
    让步）；成功写 data_runtime/yamnet_map.npz。配对不足/模型缺失 → 0。

    ⚠ R0-C：`tracks.path` 对在线曲是 `qq://` / `netease://` 占位符，永远
    `Path.exists()==False` → 此前**只有本地曲能进配对池**。用户主要听在线曲时，
    L1（中/高速语义）因此永不就绪。新增 `path_resolver(track_id, raw_path) -> str|None`
    回调，由调用方（RadioBridge）用它把在线曲映射到「播放器落盘的临时流文件」。
    """
    from core.semantic_axis import SEG_HOP_S
    t0 = time.time()
    # R99-T2：收集/拟合/评估全部走**共用**的 collect_pairs + fit_pairs + cos_of
    # ⇒ fit_mapping 与留出法评估用同一份实现，不会出现"两条拟合代码"。
    pairs = collect_pairs(db, path_resolver=path_resolver,
                          max_tracks=FIT_MAX_TRACKS, max_pairs=FIT_MAX_PAIRS)
    if len(pairs) < FIT_MIN_PAIRS:
        logger.info("[ShortSem] 映射拟合配对不足（%d < %d），L1 保持未就绪",
                    len(pairs), FIT_MIN_PAIRS)
        return 0
    w = fit_pairs(pairs, FIT_RIDGE)
    cos = cos_of(w, pairs)
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savez(MAP_PATH, w=w, n_pairs=len(pairs), hop=np.asarray(SEG_HOP_S),
             cos=np.asarray(cos))
    logger.info("[ShortSem] 映射拟合完成：%d 对（%.1fs），平均余弦 %.3f → %s",
                len(pairs), time.time() - t0, cos, MAP_PATH.name)
    return len(pairs)
