"""语义轴锚点与 CLAP 离线分段索引（水面 P4 / docs/water_research_emotional.md §15.3/§15.4）。

三层设计（常态零推理）：
  1. 语义锚点（本模块 SemanticAnchors）：14 维语义输入 = VA×2 + 反义轴×8 +
     单极描述子×4。每轴 = 正负两极 prompt 组嵌入差的方向（模板统一
     "This is {X} music."）。锚点嵌入一次性预计算并缓存
     data_runtime/semantic_anchors.npz（prompts 变更自动失效重建）。
     ⚠ 复用 RoamEngine 的 AudioFeatureExtractor（CLAP_Module 全进程唯一，
     显存纪律），绝不第二实例化。
  2. 离线分段索引（SegmentIndexer）：曲目首次播放时后台按 **5s hop / 10s 窗**
     （`SEG_HOP_S` / `SEG_WIN_S` —— R83a 由 15s 改 5s）分段 embed
     （自解码，每段 ≤1.9MB，内存纪律同 RoamEngine），fp16 存
     track_segments 表。运行时 position→段 →查表→投影，零推理。
  3. 投影（project）：embed 与各轴方向做点积 → tanh 软校准到 [0,1]
     （R82c 起优先用曲库抽样分位数标定 [p10,p50,p90] 重映射；未标定回退
     固定尺度 s）。纯 numpy，微秒级。

对外：
  - SemanticAnchors(ensure(extractor)) / project(axes, embed) -> dict[str, float]
  - SegmentIndexer.index_track(track_id, path, extractor, db) -> int
  - segment_index_for_position(pos_ms, duration_s, hop_s) -> int
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import math
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger("MusicAI.SemanticAxis")

# ---------------------------------------------------------------------------
# §15.4 语义锚点集 v1：14 维语义输入
# ---------------------------------------------------------------------------

# 反义语义轴 ×8（key, 负极模板, 正极模板）——驱动表见 §15.4 与 ui-water.js SEM_MAP
AXIS_PAIRS: dict[str, tuple[list[str], list[str]]] = {
    # A1 calm ↔ energetic → waveSpeed/amplitude
    "a1_energetic": (
        ["This is calm music.", "This is soothing peaceful music.",
         "This is mellow relaxed music.", "This is laid-back quiet music."],
        ["This is energetic music.", "This is upbeat dance music.",
         "This is high energy music.", "This is driving powerful music."],
    ),
    # A2 peaceful ↔ tense → damping(反)、rippleRandom、事件规律性
    "a2_tense": (
        ["This is peaceful serene music.", "This is gentle calm music.",
         "This is tranquil soothing music."],
        ["This is tense music.", "This is anxious agitated music.",
         "This is dramatic suspenseful music.", "This is restless urgent music."],
    ),
    # A3 dark ↔ bright → 高光明度/色温（v1 折算 spec/crest 亮度，色彩 P4-2）
    "a3_bright": (
        ["This is dark brooding music.", "This is gloomy somber music."],
        ["This is bright cheerful music.", "This is sunny uplifting music.",
         "This is sparkling radiant music."],
    ),
    # A4 cold ↔ warm → tint 色温（v1 不动色彩，折算 caustic 存在感；P4-2 拍板）
    "a4_warm": (
        ["This is cold icy music.", "This is bleak frigid ambient music."],
        ["This is warm cozy music.", "This is tender heartfelt music."],
    ),
    # A5 sparse ↔ dense → rippleDensity/spatialScale（radBase 反向）
    "a5_dense": (
        ["This is sparse minimal music.", "This is bare music with few instruments."],
        ["This is dense rich music.", "This is layered music with many instruments."],
    ),
    # A6 acoustic ↔ electronic → directionality/flow
    "a6_electronic": (
        ["This is organic acoustic music.", "This is music with real instruments."],
        ["This is electronic music.", "This is synthetic music with synthesizers."],
    ),
    # A7 intimate ↔ expansive → spatialScale/事件分布半径
    "a7_expansive": (
        ["This is intimate close music.", "This is quiet personal music."],
        ["This is expansive grand music.", "This is epic orchestral music."],
    ),
    # A8 smooth ↔ sharp → 冲击锐度（事件 rad 小/amp 高）
    "a8_sharp": (
        ["This is smooth flowing music.", "This is legato gentle music."],
        ["This is sharp percussive music.", "This is staccato rhythmic music."],
    ),
}

# VA 连续两维（锚点回退，§15.4"回归头或锚点回退"；P7 可换回归头）
VA_PAIRS: dict[str, tuple[list[str], list[str]]] = {
    "valence": (
        ["This is sad melancholic music.", "This is depressing sorrowful music."],
        ["This is happy joyful music.", "This is uplifting positive music."],
    ),
    "arousal": (
        ["This is calm slow quiet music.", "This is sleepy drowsy music."],
        ["This is intense exciting music.", "This is loud fast energetic music."],
    ),
}

# 单极描述子 ×4（无干净反义的质感词；高激活才介入）
UNIPOLAR: dict[str, str] = {
    "u_melancholic": "This is melancholic sorrowful music.",
    "u_dreamy": "This is dreamy hazy ethereal music.",
    "u_ambient": "This is ambient atmospheric music with reverb.",
    "u_aggressive": "This is aggressive noisy intense music.",
}

ALL_KEYS = list(VA_PAIRS) + list(AXIS_PAIRS) + list(UNIPOLAR)

# 投影软校准尺度：dot ∈ [-1,1]，tanh(x/s) 中心斜率 1/s。
# R86 P0③ 治本：原 0.12 是按 |dot|≲0.3 的保守上界取的，实测 CLAP 段嵌入与轴
# 方向的点积**典型量级仅 0.02~0.05**（§20.4）→ x=dot/0.12∈±0.42 → 轴值挤在
# 0.40~0.60，语义摆幅只剩满量程 1/5，视觉不可辨。改到实测量级 0.04：
# 典型 |dot|=0.04 → x≈1.0 → 轴值 0.5±0.38，摆幅回到可用区间。
# ⚠ 真正的治本是 P7/R82c 分位数标定（按轴 p10/p50/p90 重映射，见 project）；
#   本常量只是「尚无段可标定」时（首播前几分钟 / 索引未就绪）的回退兜底——
#   R86 P0① 让在线曲也能索引后，ensure_quantiles 才真正有段可标。
PROJ_SCALE = 0.04
# 回退分支的 x 软上限：防异常嵌入（|dot| 远超典型值）把轴值直接顶到 0/1
# 饱和（tanh(3)≈0.995）。标定分支不受此限（分位数天然归一）。
PROJ_X_CLAMP = 3.0

# 分段参数（§15.3；R83a 方案 0：hop 15s→5s —— 语义粒度 ×3 细，播放路径仍
# 零推理（查表）；代价 = 离线索引时间/存储 ×~3（3min 曲 ~35KB，可忽略）。
# 旧 15s 表靠 mood 线程"段数 vs 新 hop 期望段数"失配检测 → 首播自动重索引）
SEG_HOP_S = 5.0      # 段步进（段 k 覆盖 [k*hop, k*hop+win)）
SEG_WIN_S = 10.0     # embed 窗长（CLAP max_len=480000/48000=10s）
SEG_SR = 48000       # CLAP 音频编码采样率

# 锚点缓存
_ANCHOR_CACHE = Path(__file__).resolve().parents[1] / "data_runtime" / "semantic_anchors.npz"

# R82c 分位数标定（§17.4 / §16.3-B1 治本项）：固定 tanh 尺度 s=0.12 使投影
# 中段聚集（轴值挤在 0.4~0.6）→ 语义摆幅视觉不可辨。对曲库抽样段的每轴
# dot 分布取 [p10, p50, p90]，project() 重映射为 tanh((dot-p50)/((p90-p10)/2))
# → 轴值真正拉开。标定文件随 prompts 指纹自动失效；未标定回退 PROJ_SCALE。
_QUANT_CACHE = Path(__file__).resolve().parents[1] / "data_runtime" / "semantic_quantiles.npz"
_quant: dict[str, tuple[float, float, float]] | None = None


def _prompt_version() -> str:
    """prompts 指纹（模板变更 → npz 缓存自动失效）。"""
    h = hashlib.sha256()
    for k in ALL_KEYS:
        h.update(k.encode("utf-8"))
        for s in (AXIS_PAIRS.get(k, ([], []))[0] + AXIS_PAIRS.get(k, ([], []))[1]
                  + ([UNIPOLAR[k]] if k in UNIPOLAR else [])):
            h.update(s.encode("utf-8"))
    return h.hexdigest()[:16]


def segment_index_for_position(pos_ms: float, n_segs: int) -> int:
    """播放位置 → 段下标（段 k 覆盖 [k*hop, (k+1)*hop)），clamp 到 [0, n_segs)。"""
    if n_segs <= 0:
        return -1
    i = int((pos_ms / 1000.0) // SEG_HOP_S)
    return max(0, min(n_segs - 1, i))


def n_segments_for_duration(duration_s: float) -> int:
    """曲长 → 段数（≤10s 一段；否则 ceil((dur-win)/hop)+1）。"""
    d = max(0.0, float(duration_s or 0.0))
    if d <= SEG_WIN_S:
        return 1
    return int(math.ceil((d - SEG_WIN_S) / SEG_HOP_S)) + 1


class SemanticAnchors:
    """14 维语义轴方向向量（懒构建 + npz 缓存）。

    方向 = normalize(mean(pos 嵌入) − mean(neg 嵌入))；单极 = normalize(嵌入)。
    构建需 CLAP 文本编码器（extractor._clap_model，~90 条短模板一次）
    ；结果缓存后加载零推理零显存。
    """

    def __init__(self) -> None:
        self._dirs: dict[str, np.ndarray] | None = None
        # R0：构建失败原因。此前 ensure() 的失败只打一行 warning，真机静默失效
        # （水面一直平均波纹）无法定位；现在落成字段，由 /api/status 暴露。
        # None=未失败；"clap_not_ready"=CLAP 权重未就绪；其它=异常串。
        self.last_error: str | None = None

    def status(self) -> str:
        if self._dirs is not None:
            return "ready"
        try:
            if _ANCHOR_CACHE.exists() and self._load_cache():
                self.last_error = None
                return "ready"
        except Exception as e:  # noqa
            self.last_error = f"cache:{type(e).__name__}: {e}"
            logger.warning("[SemAxis] 锚点缓存读取失败（将重建）: %s", e)
        return "pending"

    def state(self) -> dict:
        """诊断快照（R0：静默失败必须可观测，别只写 log）。"""
        return {"status": self.status(), "last_error": self.last_error}

    def ensure(self, extractor) -> bool:
        """锚点就绪（必要时用 extractor 的 CLAP 文本编码器构建）。CLAP 未就绪→False。"""
        if self._dirs is not None:
            return True
        if self.status() == "ready":
            return True
        model = getattr(extractor, "_clap_model", None)
        if model is None:
            self.last_error = "clap_not_ready"
            return False
        try:
            t0 = time.time()
            dirs: dict[str, np.ndarray] = {}
            for key, (neg, pos) in {**VA_PAIRS, **AXIS_PAIRS}.items():
                e_neg = np.mean(self._embed_texts(model, neg), axis=0)
                e_pos = np.mean(self._embed_texts(model, pos), axis=0)
                d = e_pos - e_neg
                n = np.linalg.norm(d)
                dirs[key] = (d / n).astype(np.float32) if n > 1e-9 \
                    else np.zeros(512, dtype=np.float32)
            for key, tpl in UNIPOLAR.items():
                e = self._embed_texts(model, [tpl])[0]
                n = np.linalg.norm(e)
                dirs[key] = (e / n).astype(np.float32) if n > 1e-9 \
                    else np.zeros(512, dtype=np.float32)
            self._dirs = dirs
            self.last_error = None
            self._save_cache(dirs)
            logger.info("[SemAxis] 语义锚点已构建（%d 轴，%.1fs，缓存 %s）",
                        len(dirs), time.time() - t0, _ANCHOR_CACHE.name)
            return True
        except Exception as e:  # noqa
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("[SemAxis] 语义锚点构建失败（语义层回退中性）: %s", e)
            return False

    @staticmethod
    def _embed_texts(model, texts: list[str]) -> np.ndarray:
        """CLAP 文本编码（roberta 本地分词器，离线）。

        ⚠⚠ R0 事故（2026-09-10 实测定案）：此前写死
        `model.get_text_embedding(list(texts), use_tqdm=False)`，但 laion_clap
        的真实签名是 `get_text_embedding(self, x, tokenizer=None, use_tensor=False)`
        —— **根本没有 use_tqdm 参数**（venv/.../laion_clap/hook.py:193）。于是每次
        调用都抛 TypeError，被 ensure() 的宽 except 吞成一行 warning →
        锚点永远建不起来（semantic_anchors.npz 从未落盘）→ **整个语义层静默失效**
        （症状：水面一直是平均波纹，中/高速响应全无）。
        教训：外部库的可选参数一律**按签名探测后再传**，绝不硬编码（假绿的
        verify 桩当年正是照抄了这个不存在的参数）。
        """
        fn = model.get_text_embedding
        kwargs: dict = {}
        try:
            params = inspect.signature(fn).parameters
            if "use_tqdm" in params:        # 旧/部分版本确实有此参数时再传
                kwargs["use_tqdm"] = False
        except (TypeError, ValueError):     # 无签名的包装对象：退化为零可选参数
            kwargs = {}
        emb = fn(list(texts), **kwargs)
        e = np.asarray(emb, dtype=np.float32)
        n = np.linalg.norm(e, axis=1, keepdims=True)
        n[n < 1e-9] = 1.0
        return e / n

    def _load_cache(self) -> bool:
        z = np.load(_ANCHOR_CACHE, allow_pickle=False)
        if str(z.get("version", "")) != _prompt_version():
            return False
        self._dirs = {k: z[k].astype(np.float32) for k in z.files if k != "version"}
        return all(k in self._dirs for k in ALL_KEYS)

    @staticmethod
    def _save_cache(dirs: dict[str, np.ndarray]) -> None:
        _ANCHOR_CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.savez(_ANCHOR_CACHE, version=np.asarray(_prompt_version()),
                 **{k: v for k, v in dirs.items()})


# 全进程单例（方向向量纯 numpy 只读，线程安全）
anchors = SemanticAnchors()


# ---------------------------------------------------------------------------
# R82c 分位数标定（曲库抽样 → 每轴 dot 分位数 → project 重映射）
# ---------------------------------------------------------------------------

# R98-K1b：语料增量超过这个倍数 → 分位数视为**过期**（重新标定）。
# 背景（`docs/calibration_chain_research.md` §1.2）：存档分位数在旧语料上算，
# 语料从 759 段涨到 1416 段后中心漂 0.03~0.14（14 轴里 13 轴中位偏离 0.5）。
# 在线曲落盘索引生效 ⇒ 语料天天在长 ⇒ 这是**结构性**问题，必须能自动失效。
QUANT_STALE_RATIO = 1.2


def corpus_segments(db) -> int:
    """当前语料的总段数（`track_segments` 行数）；查不到/异常 → 0（不触发重建）。"""
    try:
        r = db.conn.execute("SELECT COUNT(*) AS c FROM track_segments").fetchone()
        return int(r["c"] if r is not None and not isinstance(r, tuple) else r[0])
    except Exception:  # noqa: 表不存在 / 库异常 → 保守返回 0
        return 0


def compute_quantiles(db, max_tracks: int = 60, min_segments: int = 20) -> int:
    """离线标定：抽样已索引曲目的全部段嵌入 → 每轴 dot 分布 → [p10,p50,p90] 存档。

    纯 numpy 查表（段嵌入已在 track_segments 表，零 CLAP 推理），可在任意
    后台线程调用。成功返回参与统计的段数并写缓存；失败/样本不足返回 0。
    """
    global _quant
    dirs = anchors._dirs
    if not dirs:
        return 0
    try:
        rows = db.conn.execute(
            "SELECT DISTINCT track_id FROM track_segments LIMIT ?",
            (int(max_tracks),)).fetchall()
    except Exception:  # noqa 表未建/库异常 → 未标定
        return 0
    dots: dict[str, list[float]] = {k: [] for k in ALL_KEYS}
    for r in rows:
        for vec in SegmentIndexer.load_segments(int(r["track_id"]), db).values():
            e = np.asarray(vec, dtype=np.float32).reshape(-1)
            n = float(np.linalg.norm(e))
            if n < 1e-9:
                continue
            e = e / n
            for k in ALL_KEYS:
                d = dirs.get(k)
                if d is not None:
                    dots[k].append(float(np.dot(e, d)))
    total = len(dots[ALL_KEYS[0]])
    if total < min_segments:
        logger.info("[SemAxis] 分位数标定样本不足（%d 段 < %d），维持固定尺度",
                    total, min_segments)
        return 0
    q: dict[str, tuple[float, float, float]] = {}
    for k in ALL_KEYS:
        arr = np.asarray(dots[k], dtype=np.float64)
        if arr.size < min_segments:
            q[k] = (0.0, 0.0, 0.0)
        else:
            q[k] = (float(np.percentile(arr, 10)),
                    float(np.percentile(arr, 50)),
                    float(np.percentile(arr, 90)))
    try:
        _QUANT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.savez(_QUANT_CACHE,
                 version=np.asarray(_prompt_version()),
                 keys=np.asarray(ALL_KEYS),
                 p10=np.asarray([q[k][0] for k in ALL_KEYS]),
                 p50=np.asarray([q[k][1] for k in ALL_KEYS]),
                 p90=np.asarray([q[k][2] for k in ALL_KEYS]),
                 # R98-K1b：记录标定时的语料规模 → 供自动失效判据
                 n_segments=np.asarray(total),
                 n_tracks=np.asarray(len(rows)))
    except Exception as e:  # noqa 存档失败 → 本进程内存态仍可用
        logger.warning("[SemAxis] 分位数存档失败（进程内生效）: %s", e)
    _quant = q
    logger.info("[SemAxis] 分位数标定完成（%d 段 / %d 曲，缓存 %s）",
                total, len(rows), _QUANT_CACHE.name)
    return total


def load_quantiles(db=None) -> bool:
    """读分位数缓存。

    两种失效（失效 ⇒ 返回 False，由 `ensure_quantiles` 触发重算）：
      1. prompts 指纹不符（既有）；
      2. **R98-K1b 语料增长 > `QUANT_STALE_RATIO`（1.2×）** —— 传入 `db` 才判。
         ⚠ 老存档没有 `n_segments` 字段 ⇒ 视为"规模未知"，**按过期处理**
         （这正是今天要修的那批：2026-09-10 13:33 生成于 759 段语料）。
    """
    global _quant
    if _quant is not None:
        return True
    try:
        z = np.load(_QUANT_CACHE, allow_pickle=False)
        if str(z.get("version", "")) != _prompt_version():
            return False
        if db is not None:
            try:
                arch = int(z["n_segments"]) if "n_segments" in z.files else 0
            except Exception:  # noqa
                arch = 0
            now_n = corpus_segments(db)
            if arch <= 0 or (now_n > 0 and now_n > arch * QUANT_STALE_RATIO):
                logger.info("[SemAxis] 分位数已过期（存档 %d 段 → 当前 %d 段 > %.1f×）→ 重算",
                            arch, now_n, QUANT_STALE_RATIO)
                return False
        keys = [str(k) for k in z["keys"].tolist()]
        p10, p50, p90 = z["p10"], z["p50"], z["p90"]
        _quant = {k: (float(p10[i]), float(p50[i]), float(p90[i]))
                  for i, k in enumerate(keys)}
        return True
    except Exception:  # noqa 无缓存/损坏 → 未标定
        return False


def ensure_quantiles(db, force: bool = False) -> bool:
    """标定就绪（懒加载缓存；缺失则用 db 抽样现算）。供 mood 线程一次性调用。"""
    if not force and _quant is not None:
        return True
    if load_quantiles(db):          # R98-K1b：传 db 才判"语料增长过期"
        return True
    return compute_quantiles(db) > 0


def quantiles_status() -> str:
    return "ready" if (_quant is not None or load_quantiles()) else "fallback"


def project(embed512: np.ndarray) -> dict[str, float]:
    """512 维（L2 归一）段嵌入 → 14 维语义轴值 [0,1]。

    反义轴/VA：val = 0.5 + 0.5·tanh(x)，其中
      · 已标定（R82c 分位数缓存）：x = (dot − p50) / max((p90−p10)/2, ε)
        —— p10→≈0.12、p50→0.5、p90→≈0.88，轴值真正拉开；
      · 未标定：x = clamp(dot / s, ±3)（固定尺度 s=PROJ_SCALE=0.04，R86 P0③ 按实测
        点积量级 0.02~0.05 重定，并加软上限防异常嵌入饱和）。
    单极：val = clamp(0.5 + 0.5·tanh(x), 0, 1)（激活远低于中性 → ≈0.5，
    前端以 (val−0.5)·2 的带符号强度介入）。
    纯 numpy（~14 次点积），可在任意线程调用（_quant 只读）。
    """
    dirs = anchors._dirs or {}
    e = np.asarray(embed512, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(e))
    e = e / n if n > 1e-9 else e
    out: dict[str, float] = {}
    for k in ALL_KEYS:
        d = dirs.get(k)
        if d is None:
            out[k] = 0.5
            continue
        dot = float(np.dot(e, d))
        q = _quant.get(k) if _quant else None
        if q is not None and (q[2] - q[0]) > 1e-4:
            x = (dot - q[1]) / max((q[2] - q[0]) * 0.5, 1e-4)
        else:
            x = max(-PROJ_X_CLAMP, min(PROJ_X_CLAMP, dot / PROJ_SCALE))
        val = round(max(0.0, min(1.0, 0.5 + 0.5 * math.tanh(x))), 4)
        # ★ NaN 守卫：CLAP VA 锚点方向偶发 NaN（dot 含 NaN）→ tanh(NaN)=NaN；
        # 回中性 0.5，避免 NaN 经 mood payload 灌进水面（配合 ui-water.js 的
        # `!Number.isFinite(v) continue` 双保险）。
        out[k] = val if math.isfinite(val) else 0.5
    return out


class SegmentIndexer:
    """§15.3 离线分段索引：5s hop / 10s 窗逐段 embed → fp16 入库。

    ⚠ 内存纪律（RoamEngine 同款）：自解码硬截 10s（每段 ≤1.9MB）、每 4 段只清
    年轻代 `gc.collect(0)`（R88：全代收集会持 GIL 数百 ms → 击穿音频队列），
    整曲就绪那一刻才做一次全代回收 + `torch.cuda.empty_cache()`。任何一段解码
    失败 → 中止并保留已完成段（R89：下次续跑自动跳过已入库段）。
    在线音源的索引源 = 播放器落盘的本地流文件（见 RadioBridge._local_index_path）。
    """

    # ★ 2026-09-21：worker 路径的**切片预算下限**（秒）。
    #   调用方给的 `SEG_BUDGET_S=0.25` 是为了限制**本进程的 GIL 占用**而定的；
    #   走子进程后这个约束不存在 ⇒ 用更大的预算换更少的往返（~215 ms/段 ⇒ 0.8 s ≈ 3~4 段）。
    #   代价只是"mood 线程等结果"的时长；本进程其余部分（音频/WS/UI）全程不受影响。
    SEG_PROC_MIN_BUDGET_S = 0.8

    # ★ 2026-09-21：最近一次 `index_track` **实际走的路径**。调用方据此给
    #   `note_heavy` 起对名字：走子进程时那段墙钟是「**等** worker」，**不是**本进程
    #   的 GIL 停顿 —— 混在一起会把停顿归因带偏（`attribute_stalls` 会把 `~` 前缀
    #   的笔数排除在嫌疑之外）。
    LAST_PATH = ""                 # "proc" / "local" / "defer" / ""
    _DEFER_N = [0]                 # 连续 defer 次数（护栏：worker 一直起不来时兜底）
    _DEFER_MAX = 20                # ≈ 20 × INDEX_RETRY_S(8s) ≈ 2.7 min

    @staticmethod
    def _worker_rows(p, order: list, budget_s: float):
        """★ 2026-09-21：把「解码 + CLAP 前向」交给常驻子进程。

        返回 **`(mode, rows)` 三态**：
          · `("ok", [...])`  —— 子进程结果（段数可能少于请求，正常）
          · `("defer", [])`  —— **worker 正在拉起/加载** ⇒ 本 tick 既不索引、也
            **不回退本进程**。回退就是 ~0.35 s 的进程级停顿，正是要消灭的东西；
            mood 循环下一个 tick（8 s）自会再来，代价只是语义晚一两拍到位。
          · `("fail", None)` —— 硬失败（开关关 / 进程起不来 / 超时 / 报错）⇒ 回退。

        ⚠ 为什么必须区分（真机证据）：旧实现只有"成功 / None 回退"两态，于是
        **worker 冷启动的那 ~2 s 窗口**里每次调用都落回本进程 —— `stall_culprit`
        抓到 play+839~881 s 那 6 笔 `clap_index` 330~357 ms 就是这个窗口。
        父进程在 `collect()` 里只 `time.sleep` 轮询 ⇒ GIL 全程释放 ⇒ 零进程级停顿。
        """
        try:
            from core import clap_proc
            if not clap_proc.enabled():
                return ("fail", None)
            segs = [(int(k), int(k * SEG_HOP_S * 1000)) for k in order]
            seq = clap_proc.submit(str(p), segs, SEG_SR, SEG_WIN_S,
                                   max(float(budget_s or 0.0),
                                       SegmentIndexer.SEG_PROC_MIN_BUDGET_S))
            if seq is None:
                st = clap_proc.status()
                if st in ("idle", "loading"):
                    return ("defer", [])        # 正在拉起/加载 ⇒ 让路，别回退
                return ("fail", None)           # off / down ⇒ 回退本进程
            # 超时：预算 + 单段余量（首段含 librosa 预热）
            got = clap_proc.collect(seq, timeout=max(30.0, float(budget_s or 0.0) + 25.0))
            if got is None:
                return ("fail", None)
            return ("ok", [(int(k), int(ms), bytes(b)) for k, ms, b in got])
        except Exception as e:                       # noqa 诊断绝不拖垮播放
            logger.warning("[SemAxis] worker 段索引失败（回退本进程）: %s", e)
            return ("fail", None)

    @staticmethod
    def index_track(track_id: int, path: str, extractor, db,
                    priority_seg: int | None = None,
                    only_missing: bool = False,
                    budget_s: float = 0.0,
                    use_proc: bool = False) -> int:
        """索引一首歌（可增量 / 可续跑），返回**本次新增**的段数。

        ⚠⚠ R89 增量改造的动机：原实现「整首索引完才写库」，而它又与 BEATS 索引
        **同线程串行**（见 webapi/radio_bridge._start_mood_pusher）—— 换曲后要等
        整曲跑完，水面的 L0 语义才第一次到位；更糟的是这期间连 L1 短期语义都被
        同一个线程堵住。改法：
          · `priority_seg`：按 |k − 播放头段| 升序索引 → **当前正在听的那一段
            最先入库**，换曲后一两个 tick 内水面就有语义可放；
          · `only_missing`：跳过库里已有的段（续跑、以及在线曲流文件随时间增长
            后补齐新段）；
          · `budget_s > 0`：每调用最多花这么久（**跑完当前段**再收尾），把原来
            几十秒的一次性阻塞拆成若干 ~1s 薄片，交给 mood 循环逐 tick 消化。
        部分结果**立即写库**（`set_track_segments` 是整体替换，故会带上库中已有段，
        不会把已索引的段删掉）。CLAP 未就绪 / 本地文件缺失 → 0。
        """
        if getattr(extractor, "_clap_model", None) is None:
            return 0
        p = Path(path)
        if not p.exists():
            return 0
        try:
            import librosa
            import soundfile as sf
            info = sf.info(str(p))
            dur = float(info.frames) / float(info.samplerate)
            n = n_segments_for_duration(dur)
            model = extractor._clap_model
            # R89：先吸收库中已有段（fp16 原样搬回），再决定要跑哪些 k。
            rows: list[tuple[int, int, bytes]] = []
            have: set[int] = set()
            if only_missing or budget_s > 0:
                for r in db.get_track_segments(track_id):
                    b = bytes(r["embedding"])
                    rows.append((int(r["seg_idx"]), int(r["start_ms"]), b))
                    have.add(int(r["seg_idx"]))
            order = [k for k in range(n) if k not in have]
            if priority_seg is not None and order:
                ps = max(0, min(n - 1, int(priority_seg)))
                order.sort(key=lambda k: abs(k - ps))
            t_start = time.monotonic()
            added = 0
            # ═══ ★★ 2026-09-21：**先试常驻子进程**（见 `_worker_rows` docstring）═══
            #   本进程只做 DB 读写（毫秒级）；任何失败（未就绪/超时/报错/空结果）
            #   都落到下面原来的本进程路径 ⇒ 最坏情况 = 今天的行为。
            #   ⚠ 这里**刻意不做** `gc.collect()` / `torch.cuda.empty_cache()`：
            #     那两笔是**本进程**的 GIL 长停顿（实测 ~250 ms，`gc_max_ms` 抓到过），
            #     而 worker 路径在本进程里几乎不产生新对象 ⇒ 不需要，也正好把这一笔去掉。
            #   ⚠ **必须由调用方显式开启**（`use_proc=True`）：自动生效会绕过调用方注入的
            #     替身（假 extractor / 假 db），把 `verify_r89_index_priority` 的
            #     "切片 / 播放头优先 / 不重复嵌入"等契约判据全部打空（实测 34/3 → 26/11）。
            #     生产侧只有 mood 循环与预热线程传 True，其余路径逐位保持旧行为。
            if order and use_proc:
                mode, wrows = SegmentIndexer._worker_rows(p, order, budget_s)
                if mode == "defer":
                    # ★ worker 正在拉起/加载 ⇒ **本 tick 让路**：既不索引，也不回退。
                    #   回退 = 本进程 ~0.35 s 停顿（`stall_culprit` 抓到过 6 笔）。
                    #   护栏：连续让路太久（worker 一直起不来）⇒ 兜底回退，宁可有停顿
                    #   也不能永远不索引。
                    SegmentIndexer._DEFER_N[0] += 1
                    if SegmentIndexer._DEFER_N[0] <= SegmentIndexer._DEFER_MAX:
                        SegmentIndexer.LAST_PATH = "defer"
                        return 0
                    logger.warning("[SemAxis] worker 连续 %d 次未就绪 ⇒ 兜底回退本进程",
                                   SegmentIndexer._DEFER_N[0])
                    SegmentIndexer.LAST_PATH = "local"
                elif mode == "ok":
                    SegmentIndexer._DEFER_N[0] = 0
                    SegmentIndexer.LAST_PATH = "proc"
                    if not wrows:
                        return 0        # worker 说"这些段拿不到" ⇒ 别再用本进程重试一遍
                    rows.extend(wrows)
                    added += len(wrows)
                    rows.sort(key=lambda r: r[0])
                    db.set_track_segments(track_id, rows)
                    logger.info("[SemAxis] 分段索引%s(worker) track=%s：%d 段"
                                "（本次新增 %d，%.0fs）",
                                "完成" if len(rows) >= n else "增量",
                                p.name, len(rows), added, dur)
                    return added
                else:
                    SegmentIndexer.LAST_PATH = "local"
            for k in order:
                offset = k * SEG_HOP_S
                wav, _ = librosa.load(str(p), sr=SEG_SR, mono=True,
                                      offset=offset, duration=SEG_WIN_S,
                                      dtype=np.float32)
                wav = np.ascontiguousarray(np.asarray(wav, dtype=np.float32).reshape(-1))
                if wav.size < SEG_SR:  # 尾段 <1s 无语义，弃
                    break
                emb = model.get_audio_embedding_from_data([wav])
                vec = np.asarray(emb, dtype=np.float32).reshape(-1)
                nrm = float(np.linalg.norm(vec))
                if nrm > 1e-9:
                    vec = vec / nrm
                rows.append((k, int(offset * 1000),
                             vec.astype(np.float16).tobytes()))
                added += 1
                del wav
                time.sleep(0.06)  # R80/R88 爆音让步：段间让出 CPU/磁盘 IO 给播放路径
                if added % 4 == 0:
                    # ⚠⚠ R88：**每 4 段不再做全代 gc.collect()** —— 主进程 RSS 实测
                    #   ~4.2GB，全代收集要遍历整个堆并**持 GIL 数百毫秒**，直接击穿音频
                    #   队列头部（MUSIC_Q_DEPTH 已提到 32 ≈ 743ms）→ PortAudio output
                    #   underflow，听感=每首歌开头一两分钟断续/重复。段内对象（wav /
                    #   512 维 vec）都是 refcount 即时回收的 numpy 数组，不是循环垃圾
                    #   → 只清年轻代即可（毫秒级）。
                    import gc
                    gc.collect(0)
                # R89：时间预算到点 → 收尾（当前段已完整），交回调用方下一 tick 续。
                if budget_s > 0 and (time.monotonic() - t_start) >= budget_s:
                    break
            if not rows:
                return 0
            complete = (len(rows) >= n)
            # R88/R89：全代回收 + CUDA 缓存释放只在**整曲索引就绪**那一刻做一次
            #   （原为每 4 段一次），既保住内存闸门，又不在播放中反复制造 GIL 长停顿。
            if complete:
                import gc
                gc.collect()
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:  # noqa
                    pass
            rows.sort(key=lambda r: r[0])          # seg_idx 升序，便于 load_segments/查表
            db.set_track_segments(track_id, rows)
            logger.info("[SemAxis] 分段索引%s track=%s：%d 段（本次新增 %d，%.0fs）",
                        "完成" if complete else "增量", p.name, len(rows), added, dur)
            return added
        except Exception as e:  # noqa 索引失败不影响播放（语义层回退中性）
            logger.warning("[SemAxis] 分段索引失败 track=%s: %s", path, e)
            return 0

    @staticmethod
    def load_segments(track_id: int, db) -> dict[int, np.ndarray]:
        """读某曲全部分段向量（fp16 → float32），{seg_idx: 512 维}。"""
        out: dict[int, np.ndarray] = {}
        try:
            for r in db.get_track_segments(track_id):
                v = np.frombuffer(bytes(r["embedding"]), dtype=np.float16)
                if v.size == 512:
                    out[int(r["seg_idx"])] = v.astype(np.float32)
        except Exception as e:  # noqa
            logger.warning("[SemAxis] 分段读取失败 track=%s: %s", track_id, e)
        return out
