"""
音频特征与向量检索：漫游推荐的核心。

理想路径：用 LAION-CLAP 把每段音频编码成 512 维语义向量，
再用 FAISS 做最近邻检索（连续相似 / 风格跳跃）。

降级路径：若 torch/clap 未安装（体积大、需联网），
则基于元数据构造"伪特征向量"（流派 one-hot + 年代/节奏归一化），
仍可用 numpy 暴力检索，保证软件开箱即用。

CLAP 离线化（本机实践）：
  - laion-clap 的权重与 roberta-base 已预下载到本地 models/ 目录：
      models/clap/630k-audioset-best.pt
      models/hf/roberta-base/            （config + 权重 + tokenizer）
  - laion_clap 在导入时会「急切地」加载 bert/bart/roberta 三个文本模型，但我们用
    tmodel='roberta' 只做音频编码，bert/bart 既不会被构建也不会被推理使用。
    因此本模块在 import laion_clap 之前对 transformers 做一次 monkeypatch：
      * roberta-base  -> 重定向到本地 models/hf/roberta-base（离线）
      * bert-base-uncased / facebook/bart-base -> 返回占位空对象（绝不会被调用）
    这样运行期完全不联网、不下载、不浪费 ~800MB 显存/RAM 去加载用不上的文本模型。
  - CLAP 权重通过 load_ckpt(ckpt=本地路径) 加载，不触发 huggingface 下载。
  - 显存：本进程的 GPU 上限由 core/gpu_budget.apply_gpu_budget() 在启动早期设置
    （process_gpu_fraction，兼容旧名 melotts_gpu_fraction；默认 30%）。★ 2026-11-17
    task-1：本进程现在只有 CLAP 在用（clap_gpu_fraction 默认 5%），IndexTTS 跑在
    独立 worker，不占本预算。CLAP 音频编码器约 0.4~0.5GB，落在预留额度内。

对外暴露：
  - extract_features(track) -> np.ndarray
  - build_index(tracks) -> index
  - query(index, vec, k, mode) -> indices
"""
from __future__ import annotations

import logging
import os
import warnings
from pathlib import Path

import numpy as np

logger = logging.getLogger("MusicAI.Features")


def cap_torch_threads_for_audio(torch_mod=None, cpu_count=None) -> int:
    """R88/R89：把 torch 的 CPU 线程数收窄到「总核的三分之一」，给音频 RT/填充留空闲核。

    背景（实机事故）：CLAP 默认 `torch.set_num_threads = 物理核数` → **整曲索引期间
    16 核全满**，sounddevice 的 RT 回调（`blocksize=1024` ≈ **23ms 预算**）与音乐填充
    线程抢不到核 → PortAudio output underflow。用户听感 = **每首歌刚开头的一两分钟
    断续/重复**，索引跑完即恢复，**画面完全不卡**。ONNX 两处（`beat_events` /
    `short_term_semantics`）早已限 2 线程，此处补齐 torch。

    ⚠⚠ R89：**限流不影响 CLAP 的推理吞吐**（因此敢大胆收窄）。源码级依据：
      ① 特征变换在 GPU —— `HTSAT.spectrogram_extractor` / `logmel_extractor`
         （torchlibrosa 的 nn.Module 子模块，见 laion_clap/clap_module/htsat.py
         L676/L680）随 `.cuda()` 一起搬上显卡；`model.get_audio_embedding`
         （clap_module/model.py L734-739）先把输入 `.to(device)` 再算 mel。
      ② 段索引的 CPU 侧只剩 **librosa 解码**（实测 ~35ms/段，不经过 torch 线程池）
         与 **numpy 量化**（`int16_to_float32 = (x/32767.0).astype(np.float32)`，
         laion_clap/training/data.py L93）；`.float()`/`.to(cuda)` 只有 ~2MB 拷贝，
         本来是内存带宽瓶颈而非算力瓶颈。
      ③ ONNX（beats / YAMNet）有自己独立的 2 线程池，跟 torch 无关。
      ⇒ 收窄 torch 线程池对索引速度几乎零影响，纯收益（少线程创建/上下文切换抖动）。

    环境变量 `MUSICAI_TORCH_THREADS`：>0 直接指定；≤0（默认）走「核数 // 3」。
    返回**最终生效的线程数**（0 = 未设/失败，即维持旧行为）。

    抽成模块级纯函数是为了可单测：传入假 torch / 假核数即可验证行为，
    不必真的加载 110s 的 CLAP 权重。
    """
    cpu_n = int(cpu_count if cpu_count is not None else (os.cpu_count() or 4))
    try:
        want = int(os.environ.get("MUSICAI_TORCH_THREADS", "0") or 0)
    except Exception:  # noqa 环境变量乱填 → 走默认
        want = 0
    if want <= 0:
        # ★ 2026-09-18·23:5x：R89 的 //3 在真机仍偶发 underflow → 再收窄到 ≤4。
        #   依据同 docstring 第①②条：CLAP 算力在 GPU，CPU 侧只有 librosa 解码 +
        #   numpy 量化 ⇒ 收窄线程池对索引吞吐几乎零影响，却显著降低对 RT 回调的抢占。
        want = max(2, min(4, cpu_n // 4))
        want = max(1, min(cpu_n - 1, want))   # 小核机器兜底：至少给别处留 1 核
    if torch_mod is None:
        try:
            import torch as torch_mod  # noqa: PLC0414
        except Exception:  # noqa torch 未装 → 无 CLAP 路径
            return 0
    try:
        torch_mod.set_num_threads(want)
        try:
            # ⚠ 必须在任何并行工作发生前调用，否则 RuntimeError → 忽略即可
            torch_mod.set_num_interop_threads(1)
        except Exception:  # noqa
            pass
        # ★ R118：同时把 OpenMP / OpenBLAS / MKL 池的线程数也钉到同一把尺子。
        # cap 之前只管 torch 的 intra-op 池，管不到 numpy 的 BLAS 池——而 librosa
        # 解码、CLAP 的 numpy 预处理、投影 dot 都走这些池，全开会与 sounddevice 的
        # 23ms RT 回调抢核 ⇒ output underflow（"卡顿又出现"的疑似元凶）。
        # BLAS 若在 import 时已读过早的 OMP_NUM_THREADS 则此处 setdefault 不生效，
        # 故真正的生效点仍在 config/settings.py 顶部（BLAS 初始化前）；这里仅作
        # 任何先 import 本模块的路径的兜底 + 与 torch 线程数保持一致。
        for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                   "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ.setdefault(_v, str(want))
        logger.info("[CLAP] torch 线程限为 %d（总核 %d，为音频 RT 保留空闲核；"
                    "BLAS 池同步 %d）", want, cpu_n, want)
        return want
    except Exception as e:  # noqa
        logger.debug("[CLAP] torch 线程限制跳过: %s", e)
        return 0

GENRE_VOCAB = [
    "流行", "摇滚", "民谣", "电子", "古典", "爵士", "hip-hop", "说唱",
    "R&B", "乡村", "金属", "轻音乐", "国风", "灵魂", "舞曲", "另类",
]

# 本地模型根目录（项目根/models）
_MODELS_ROOT = Path(__file__).resolve().parents[1] / "models"
LOCAL_CLAP_PT = _MODELS_ROOT / "clap" / "630k-audioset-best.pt"
LOCAL_ROBERTA = _MODELS_ROOT / "hf" / "roberta-base"

# laion-clap 非融合音频编码固定维度（FAISS / numpy 索引据此建维）。
# CLAP 模式下无论编码成功或兜底，一律输出此维度，避免与 19 维元数据混用导致 vstack 崩溃。
CLAP_DIM = 512

_patch_applied = False


def _clap_checkpoint_valid(path: Path) -> bool:
    """快速校验本地 .pt 是「完整可用」的 zip 归档（末尾含中央目录 PK\\x05\\x06）。

    仅检查文件存在 + 末尾 EOCD 签名，不读取全部内容（1.7GB 太慢）。
    用于把「文件存在但仍在下载 / 被代理截断损坏」的半成品排除在可用之外，
    否则会被误判为可加载、进而产出维度混杂的特征向量拖垮 FeatureIndex。
    完整内容级校验（SHA256）由下载器 tools/chunked_download.py 负责。
    """
    try:
        if not path.exists():
            return False
        size = path.stat().st_size
        if size < 1024:
            return False
        with open(path, "rb") as f:
            f.seek(-65557, 2) if size > 65557 else f.seek(0)
            tail = f.read()
        return b"PK\x05\x06" in tail
    except Exception:  # noqa
        return False



def _patch_transformers_for_offline(transformers) -> None:
    """让 laion_clap 的 import 与推理完全离线、且跳过用不上的 bert/bart 文本模型。

    仅对 tmodel='roberta' 生效；bert/bart 的 from_pretrained 返回轻量占位对象，
    绝不参与后续计算。幂等（模块级 _patch_applied 守卫）。
    """
    global _patch_applied
    if _patch_applied:
        return

    import torch

    class _DummyModule(torch.nn.Module):
        """占位：bert/bart 文本模型，roberta 配置下永不被使用。"""

        def get_text_embedding(self, *a, **k):  # noqa
            raise RuntimeError("bert/bart 占位模型不应被调用（仅 roberta 配置）")

    class _DummyTokenizer:
        """占位：bert/bart 分词器，roberta 配置下永不被使用。"""

        def __call__(self, *a, **k):
            raise RuntimeError("bert/bart 占位分词器不应被调用（仅 roberta 配置）")

        def encode(self, *a, **k):
            raise RuntimeError("bert/bart 占位分词器不应被调用（仅 roberta 配置）")

    # 文本「模型」基类
    _orig_model_fp = transformers.PreTrainedModel.from_pretrained

    @classmethod
    def _model_fp(cls, pretrained_model_name_or_path, *args, **kwargs):
        name = pretrained_model_name_or_path
        if isinstance(name, str):
            if name == "roberta-base":
                name = str(LOCAL_ROBERTA)
            elif name in ("bert-base-uncased", "facebook/bart-base"):
                return _DummyModule()
        return _orig_model_fp.__func__(cls, name, *args, **kwargs)

    transformers.PreTrainedModel.from_pretrained = _model_fp

    # 分词器基类
    _orig_tok_fp = transformers.PreTrainedTokenizerBase.from_pretrained

    @classmethod
    def _tok_fp(cls, pretrained_model_name_or_path, *args, **kwargs):
        name = pretrained_model_name_or_path
        if isinstance(name, str):
            if name == "roberta-base":
                name = str(LOCAL_ROBERTA)
            elif name in ("bert-base-uncased", "facebook/bart-base"):
                return _DummyTokenizer()
        return _orig_tok_fp.__func__(cls, name, *args, **kwargs)

    transformers.PreTrainedTokenizerBase.from_pretrained = _tok_fp

    _patch_applied = True
    logger.info("[CLAP] transformers 已离线化：roberta-base->本地，bert/bart->占位")


class AudioFeatureExtractor:
    def __init__(self, preload: bool = False) -> None:
        self._clap = None
        self._clap_model = None
        self._clap_load_failed = False  # load_ckpt 失败后置位，避免反复重试
        self._clap_loading = False      # 后台加载进行中
        self._clap_oom_aborted = False  # 内存不足后置位 → 本会话永久停用（防 Host stopped）
        self.use_clap = self._try_load_clap()
        if preload and self.use_clap:
            self.preload_clap_async()

    # ---------------- CLAP 权重的后台预加载 ----------------
    # 为什么需要：权重加载实测约 110s（逐张量读盘），原来是在**首次编码**时同步触发，
    # 而 RoamEngine.__init__ → _build() 会立刻给每首歌编码 → 电台启动被卡两分钟。
    # 改为：后台线程加载；未就绪期间一律走「同维元数据兜底」（维度不变，索引不崩）；
    # 就绪后由调用方（RoamEngine.rebuild_async）重建索引，特征自动升级为真实音频语义。

    def preload_clap_async(self) -> bool:
        if not self.use_clap or self._clap_model is not None or self._clap_loading:
            return False
        import threading

        def _run():
            try:
                self._load_clap_model()
            except Exception:  # noqa
                pass
            finally:
                self._clap_loading = False

        self._clap_loading = True
        threading.Thread(target=_run, daemon=True, name="clap-preload").start()
        return True

    def clap_ready(self) -> bool:
        return self._clap_model is not None

    def clap_status(self) -> str:
        """disabled | pending | loading | ready | failed"""
        if not self.use_clap:
            return "disabled"
        if self._clap_model is not None:
            return "ready"
        if getattr(self, "_clap_load_failed", False):
            return "failed"
        return "loading" if self._clap_loading else "pending"

    def _try_load_clap(self) -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
            _patch_transformers_for_offline(transformers)
            # laion_clap 顶层 import 仍走 timm 已废弃的 timm.models.layers 路径
            # （其 clap_module/timm_model.py 未跟上 timm 迁移），会打 FutureWarning。
            # 第三方包问题我们改不了，导入时按消息精确压掉这一条，保持启动日志干净；
            # 只过滤该特定 deprecated 消息，不无差别吞其它警告。
            with warnings.catch_warnings():
                # laion_clap 走 timm 废弃路径 + transformers tokenizer 的
                # clean_up_tokenization_spaces 默认值警告：均来自第三方包导入期
                # 噪音，运行期无影响。按消息精确过滤，不无差别吞其它警告。
                for pat, cat in (
                    (r"Importing from timm\.models\.layers is deprecated.*", FutureWarning),
                    (r"`clean_up_tokenization_spaces` was not set.*", FutureWarning),
                ):
                    warnings.filterwarnings("ignore", message=pat, category=cat)
                import laion_clap  # noqa: F401
                self._clap = laion_clap
            if not LOCAL_CLAP_PT.exists():
                logger.warning(
                    "[CLAP] 本地权重缺失 %s，回退元数据特征（运行 tools/download_clap.py 下载）",
                    LOCAL_CLAP_PT,
                )
                return False
            if not _clap_checkpoint_valid(LOCAL_CLAP_PT):
                logger.warning(
                    "[CLAP] 本地权重不完整/损坏（可能仍在下载），回退元数据特征：%s",
                    LOCAL_CLAP_PT,
                )
                return False
            logger.info("[CLAP] 已就绪（本地权重 + roberta-base 离线），音频语义特征启用")
            return True
        except Exception as e:  # noqa
            logger.info("CLAP 未加载，使用元数据特征降级: %s", e)
            return False

    def extract_features(self, track) -> np.ndarray:
        """返回归一化向量。

        CLAP 启用时**统一输出 CLAP_DIM 维**（编码失败也用同维元数据兜底），
        避免「部分 512 维 + 部分 19 维」混入 FeatureIndex 触发 np.vstack 维度崩溃。
        非本地音源（netease://、qq://、spotify:// 等流媒体 URL）无法被 soundfile
        打开，直接走同维兜底，不浪费一次必然失败的 CLAP 编码重试。
        """
        if self.use_clap:
            p = getattr(track, "path", None)
            is_local = bool(p) and not str(p).startswith(
                ("http://", "https://", "netease://", "qq://", "spotify://", "kugou://"))
            # 只有真实存在、可被 soundfile 打开的本地文件才走 CLAP；形如 '21308328'
            # 的裸 ID / 缺失路径直接走兜底，避免必然失败的编码重试刷屏告警。
            # ⚠ 权重未就绪（后台加载中/未开始）时**直接走兜底，绝不阻塞调用线程**
            # —— 否则每首歌都会触发一次 100s+ 的同步加载，启动被彻底卡死。
            if (is_local and Path(str(p)).exists()
                    and self._clap_model is not None
                    and not getattr(self, "_clap_oom_aborted", False)):
                # ⚠ 内存闸门：重建索引会**连续**编码上百首，只要有一次撑不住就会把
                #   整进程拖到 VirtualAlloc failed → Host stopped。编码前先探一次可用内存。
                if not self._memory_pressure_ok():
                    self._abort_clap_for_session("可用内存低于安全线")
                else:
                    try:
                        return self._clap_features(str(p))
                    except (MemoryError, OSError) as e:
                        # numpy 的 "Unable to allocate ..." 是 MemoryError；
                        # 某些发行版/后端会抛 OSError。**这两种绝不能只 warning 了事**。
                        self._abort_clap_for_session(f"{type(e).__name__}: {e}")
                    except Exception as e:  # noqa
                        logger.warning("[CLAP] 编码失败，回退 %d 维元数据兜底: %s",
                                       CLAP_DIM, e)
            else:
                logger.debug("[CLAP] 跳过编码走 %d 维兜底(%s) path=%s",
                             CLAP_DIM, self.clap_status(), p)
            return self._meta_features_padded(track, CLAP_DIM)
        return self._meta_features(track)

    def _load_clap_model(self):
        """真正加载 CLAP 权重（耗时约 110s，只允许在后台线程或首次编码时调用）。"""
        # ⚠⚠ R88 音频优先：先把 torch 线程数限下来，否则整曲索引期间 16 核全满 →
        #   抢走 sounddevice RT 回调（23ms 预算）→ output underflow（听感：每首歌
        #   刚开头一两分钟断续/重复，画面不卡）。详见 cap_torch_threads_for_audio。
        cap_torch_threads_for_audio()
        import torch
        try:
            self._clap_model = self._clap.CLAP_Module(enable_fusion=False)
            # 直接从本地路径加载权重，不触发 huggingface 下载
            self._clap_model.load_ckpt(ckpt=str(LOCAL_CLAP_PT))
            if torch.cuda.is_available():
                self._clap_model = self._clap_model.cuda()
            logger.info("[CLAP] 权重已加载（%s）", LOCAL_CLAP_PT.name)
        except Exception as e:  # noqa
            self._clap_load_failed = True
            logger.error("[CLAP] 权重加载失败，本次会话回退元数据: %s", e)
            raise

    # ---------- 音频解码上限（内存硬闸门） ----------
    # ⚠⚠ 事故（2026-09-06 实机 crash）：
    #   laion_clap 的 get_audio_embedding_from_filelist 内部是
    #       audio_waveform, _ = librosa.load(f, sr=48000)      # 整文件！无上限
    #       int16_to_float32(float32_to_int16(audio_waveform)) # 再复制两份
    #   遇到长音频（实测 10401720×2 float32 ≈ 79.4MiB，且量化过程再叠两份）
    #   单次编码峰值可达数百 MB；重建 138 首时把进程拖到
    #   `[low_level_alloc.cc] VirtualAlloc failed` → **Host stopped（整进程被杀）**。
    #
    # 而 CLAP 的 max_len 只有 480000 采样 = **10 秒**，多加载的样本根本用不上
    # （rand_trunc 只随机截 10s，fusion 也只切三段）。所以：
    #   · 自己解码，硬截到 CLAP_CLIP_SECONDS，单曲内存恒定 ≈ 1.9MB
    #   · 用 get_audio_embedding_from_data（接受单声道 float32）绕开整文件加载
    CLAP_CLIP_SECONDS = 10.0          # = CLAP 的 max_len 480000 / 48000
    _CLAP_MAX_BYTES_PER_CALL = 8 << 20   # 单曲波形内存上限（8MB，远超 10s×48k×4=1.92MB）

    @classmethod
    def _load_clap_waveform(cls, path: str) -> np.ndarray:
        """解码为 **单声道 / 48kHz / 最多 10 秒** 的 float32 波形。

        返回长度 <= 480000 的一维数组。任何失败都抛异常，由调用方回退兜底。
        """
        import librosa

        wav, _ = librosa.load(
            path, sr=48000, mono=True,
            duration=float(cls.CLAP_CLIP_SECONDS), offset=0.0,
            dtype=np.float32,
        )
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        if wav.size * 4 > cls._CLAP_MAX_BYTES_PER_CALL:
            wav = wav[: cls._CLAP_MAX_BYTES_PER_CALL // 4]
        return np.ascontiguousarray(wav)

    def _clap_features(self, path: str) -> np.ndarray:
        # 真实 CLAP 编码（本地权重，离线）。CLAP_Module 自动选 cuda:0。
        # ⚠ 顺序：OOM 停用标志必须**先于** _clap_load_failed 判定
        #   （_abort_clap_for_session 会同时置 _clap_load_failed=True，
        #    放后面就永远轮不到，报错信息会误导成"权重未就绪"）。
        if getattr(self, "_clap_oom_aborted", False):
            raise RuntimeError("CLAP 已因内存不足永久停用（本次会话）")
        if getattr(self, "_clap_load_failed", False):
            raise RuntimeError("CLAP 权重未就绪（本次会话已回退）")
        if self._clap_model is None:
            # 兜底由 extract_features 分流，这里只在被直接调用时防御
            self._load_clap_model()
        wav = self._load_clap_waveform(str(path))
        if wav.size < 1024:                      # 空/极短音频没有语义可提取
            raise RuntimeError(f"音频过短（{wav.size} 采样），跳过 CLAP 编码")
        try:
            audio = self._clap_model.get_audio_embedding_from_data([wav])
        finally:
            del wav
        vec = np.asarray(audio, dtype=np.float32).reshape(-1)
        return self._normalize(vec)

    @staticmethod
    def _memory_pressure_ok(min_free_mb: int = 512) -> bool:
        """可用物理内存是否还够继续编码。psutil 不可用时保守返回 True。"""
        try:
            import psutil  # noqa
            return psutil.virtual_memory().available >= min_free_mb * 1024 * 1024
        except Exception:  # noqa
            return True

    def _abort_clap_for_session(self, reason: str) -> None:
        """内存/显存撑不住时**永久**停用 CLAP（本会话），不再逐首重试把进程拖死。"""
        self._clap_oom_aborted = True
        self._clap_load_failed = True
        try:
            self._clap_model = None
        except Exception:  # noqa
            pass
        try:
            import gc
            gc.collect()
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa
            pass
        logger.error("[CLAP] 内存不足，本次会话永久停用音频语义特征（改用元数据兜底）：%s",
                     reason)

    def _meta_features(self, track) -> np.ndarray:
        """元数据伪特征（16 维流派 + 年代 + 节奏 + 时长）。"""
        vec = np.zeros(len(GENRE_VOCAB) + 3, dtype=np.float32)
        # 流派 one-hot（模糊匹配）
        g = (track.genre or "").lower()
        for i, name in enumerate(GENRE_VOCAB):
            if name.strip().lower() in g:
                vec[i] = 1.0
        if vec[: len(GENRE_VOCAB)].sum() == 0:
            vec[0] = 1.0  # 默认"流行"桶，避免全零
        # 年代（1900~2030 -> 0~1）
        yr = track.year or 2000
        vec[len(GENRE_VOCAB)] = (yr - 1900) / 130.0
        # 节奏（0~200 BPM -> 0~1）
        bpm = track.bpm or 100.0
        vec[len(GENRE_VOCAB) + 1] = min(bpm, 200) / 200.0
        # 时长（0~600s -> 0~1）
        vec[len(GENRE_VOCAB) + 2] = min(track.duration, 600) / 600.0
        return self._normalize(vec)

    def _meta_features_padded(self, track, dim: int) -> np.ndarray:
        """元数据伪特征，补齐/截断到指定维度（默认 CLAP_DIM），保证索引维度一致。

        仅在 CLAP 已启用、但某首歌编码失败（解码错误/非本地音源）时作为同一维度
        的兜底，使 FeatureIndex 不会因维数混杂而崩溃。前 19 维保留真实元数据信号，
        其余补零后整体归一化。
        """
        v = self._meta_features(track)
        if v.shape[0] < dim:
            v = np.concatenate([v, np.zeros(dim - v.shape[0], dtype=np.float32)])
        elif v.shape[0] > dim:
            v = v[:dim]
        return self._normalize(v)

    @staticmethod
    def _normalize(vec: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(vec)
        if n > 0:
            vec = vec / n
        return vec.astype(np.float32)


class FeatureIndex:
    """基于 FAISS(IndexFlatIP) 的向量索引 —— 查询**优先全程走 FAISS**。

    仅当 faiss 未安装或某次检索异常时，才降级为 numpy 暴力检索（结果语义一致）。

    ⚠ 内积=余弦的前提：所有向量在入索引前必须已 L2 归一化
       （AudioFeatureExtractor.extract_features 已保证）。

    mode 语义：
      continuous -> 与查询向量最相似（内积最大）的 k 首；
      leap       -> 最不相似（内积最小）的 k 首（风格跳跃）。

    leap 的实现技巧：内积线性 ⇒ ⟨-v, x⟩ = -⟨v, x⟩，对 -vec 做 FAISS top-k
    检索即得原 vec 的 bottom-k —— 无需把全库拉回 numpy 排序。
    """

    def __init__(self) -> None:
        self._faiss = None
        self.index = None
        self._backend = "numpy"
        try:
            import faiss  # noqa: F401
            self._faiss = faiss
        except Exception:  # noqa
            logger.info("faiss 未安装，使用 numpy 暴力检索降级")

    @property
    def backend(self) -> str:
        """当前检索后端：faiss | numpy（供调用方/测试判定是否真的在用 FAISS）。"""
        return self._backend

    def build(self, vectors: list[np.ndarray]):
        self.matrix = (
            np.vstack(vectors).astype(np.float32)
            if vectors else np.zeros((0, 1), dtype=np.float32)
        )
        self.dims = self.matrix.shape[1]
        self.index = None
        if self._faiss is not None and self.matrix.shape[0] > 0:
            self.index = self._faiss.IndexFlatIP(self.dims)  # 内积=余弦(已归一化)
            self.index.add(self.matrix)
            self._backend = "faiss"
        else:
            self._backend = "numpy"

    def query(self, vec: np.ndarray, k: int = 5, mode: str = "continuous"):
        """返回命中下标列表（与矩阵行序一致，供调用方映射回曲目列表）。

        mode:
          continuous -> 最相似 k 个（内积降序）
          leap       -> 最不相似 k 个（内积升序 = 风格跳跃/异质）

        FAISS 就绪时本方法**不再触碰 numpy 暴力检索**：
          continuous 直接 index.search(vec)；leap 改检索 -vec（内积线性），
          等价于全库内积升序取前 k。仅当 faiss 缺失或检索抛异常时才降级 numpy，
          两条路径返回语义一致。
        """
        if self.matrix.shape[0] == 0:
            return []
        n = self.matrix.shape[0]
        k = max(1, min(int(k), n))
        vec = np.ascontiguousarray(vec.reshape(1, -1).astype(np.float32))
        if self.index is not None:
            try:
                q = -vec if mode == "leap" else vec
                _, idxs = self.index.search(q, k)
                return [int(i) for i in idxs[0]]
            except Exception as e:  # noqa
                logger.warning("faiss 检索异常，降级 numpy 暴力检索: %s", e)
        sims = self.matrix @ vec.reshape(-1)
        if mode == "leap":
            order = np.argsort(sims)   # 升序：最不相似在前
        else:
            order = np.argsort(-sims)  # 降序：最相似在前
        return [int(i) for i in order[:k]]
