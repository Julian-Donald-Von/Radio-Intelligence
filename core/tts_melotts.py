"""
MeloTTS 本地 TTS 引擎（PyTorch 原生，多语言，支持 GPU 加速）。

依赖官方 melotts 包（import 名为 melo，PyPI 包名 melotts），首次使用时按语言从
HuggingFace 自动下载对应模型权重（ZH/EN/ES/FR/JP/KR）。推理可用 CUDA
（device='cuda:0'）加速，与 Ollama 共享 RTX 4060，用完后 empty_cache 释放显存。

多语言路由：zh/en/es/fr 由本引擎统一按语言加载模型合成；ja 由 VOICEVOX 处理
（见 core/tts_voicevox.py）。同语言片段只加载一次模型即批量合成
（见 synthesize_batch），满足「MeloTTS 批次：一次模型加载，输出多个音频」。

失败降级：依赖缺失/模型下载失败 → synthesize_batch 对应片段返回 None → 上层切到
其它引擎或静音。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

# 国内访问 HuggingFace 较慢/受限，默认走 hf-mirror 镜像，确保模型权重可下载。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from config.settings import user_config, FFMPEG_BIN
from core.tts_base import TTSEngine

logger = logging.getLogger("MusicAI.TTS")

# MeloTTS 合成后的 ffmpeg 后处理链：强化「沉稳播报感」。
# 低频/中频抬升 + 高频微亮 + 压缩器(播报质感) + 轻微回声(空间感) + loudnorm 统一响度。
# 注：用户原始意图用 aequalizer（t=h 高架），但 ffmpeg 9.x 起已移除 aequalizer，
# 其「高架」语义由 dedicated 的 highshelf 滤镜等价替代（width 用 width_type=h 以 Hz 计），
# 音频效果与原始设想一致。
# 链形态固定为 highshelf×3 + acomppressor + aecho(可选) + loudnorm，但具体数值由
# 设置页 7 个滑块驱动，运行时由 _build_postprocess_filter() 拼装（见下）。
_PP_HS1_F, _PP_HS2_F, _PP_HS3_F = 120, 250, 3000  # 三段高架中心频率（Hz，固定）
_PP_HS1_W, _PP_HS2_W, _PP_HS3_W = 200, 300, 1000  # 三段高架带宽（Hz，固定）
_PP_ECHO_DELAY, _PP_ECHO_DECAY = 45, 0.25        # aecho 延迟/衰减（固定）
_PP_LOUD_TP, _PP_LOUD_LRA = -1.5, 11              # loudnorm 真峰/范围（固定）

# 2 字母语言标签 -> (melo 语言代码, speaker key 首选)
# 注意 melo 的英文有多种口音（EN-US/EN-BR/EN_INDIA/EN-AU/EN-Default）。
_LANG_MAP = {
    "zh": ("ZH", "ZH"),
    "en": ("EN", "EN-Default"),
    "es": ("ES", "ES"),
    "fr": ("FR", "FR"),
    "ja": ("JP", "JP"),
    "ko": ("KR", "KR"),
}


def _import_melo_tts():
    """导入 melotts 的 TTS 类（PyPI 包名 melotts，但 import 名为 melo）。

    ⚠ 两个候选都会把真实异常抛出（不静默吞）：历史上 HF 下载超时/证书失败会让
    `from melo.api import TTS` 抛错，此处若再 fallback 到 melotts 报「No module
    named 'melotts'」会把真实原因（网络/依赖）完全掩盖（R72 教训）。这里收集
    全部候选异常，优先透出非 ImportError 的真实异常供诊断。
    """
    import importlib
    last_err = None
    for mod in ("melo", "melotts"):
        try:
            api = importlib.import_module(f"{mod}.api")
            return api.TTS
        except Exception as e:  # noqa
            last_err = e
    if last_err is not None and not isinstance(last_err, ModuleNotFoundError):
        raise last_err
    raise ModuleNotFoundError(
        "melotts 未安装（import 名 melo/melotts 均不可用）。"
        " 若已安装但报错，多为 HF 下载失败：请检查 HF_ENDPOINT 是否可达。"
    ) from last_err


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def MELOTTS_USE_GPU() -> bool:
    """是否用 GPU 跑 MeloTTS：用户设置页优先（默认开启），环境变量兜底。"""
    val = user_config.tts.get("melotts_gpu", True)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on", "y")


class _MeloTTSWrapper:
    """按语言惰性加载并缓存 MeloTTS 模型（含自定义音色模型）。

    返回元组固定为 (model, spk2id_dict, device)：spk2id_dict 是说话人键→id 的映射，
    供引擎按需挑选说话人（支持「更换音色」）。
    """

    def __init__(self) -> None:
        self._models: dict = {}
        self._lock = threading.Lock()
        self._model_dir = ""   # 自定义音色模型目录（设置页拖拽/选择导入）

    def set_model_dir(self, model_dir: str) -> None:
        """设置自定义 MeloTTS 模型目录；变更后清空已加载模型以便重新加载。"""
        nd = (model_dir or "").strip()
        if nd != self._model_dir:
            self._model_dir = nd
            self._models.clear()

    def _device(self) -> str:
        if MELOTTS_USE_GPU():
            try:
                import torch
                if torch.cuda.is_available():
                    return "cuda:0"
            except Exception:  # noqa
                pass
        return "cpu"

    @staticmethod
    def _resolve_speaker(spk2id: dict, melo_lang: str, pref: str):
        """从 spk2id 解析说话人 id（兼容 dict / HParams）。"""
        spk = spk2id
        if not isinstance(spk, dict):
            try:
                spk = dict(spk)
            except Exception:  # noqa
                spk = {}
        if pref in spk:
            return spk[pref]
        if melo_lang == "EN":
            for cand in ("EN-Default", "EN-US", "EN-BR", "EN_INDIA", "EN-AU"):
                if cand in spk:
                    return spk[cand]
        return next(iter(spk.values())) if spk else 0

    def _load_builtin(self, TTS, melo_lang: str, device: str):
        model = TTS(language=melo_lang, device=device)
        spk = model.hps.data.spk2id
        if not isinstance(spk, dict):
            spk = dict(spk)
        logger.info("[TTS:melotts] 已加载内置语言 %s (device=%s, speakers=%s)",
                    melo_lang, device, list(spk.keys()))
        return model, spk, device

    def _load_custom(self, TTS, melo_lang: str, device: str):
        """尝试从 self._model_dir 加载自定义音色模型（失败抛异常交回退）。"""
        if not self._model_dir:
            raise FileNotFoundError("未设置自定义模型目录")
        d = Path(self._model_dir)
        if not d.is_dir():
            raise NotADirectoryError(f"自定义模型目录不存在：{self._model_dir}")
        # 启发式：取目录下最大的 .pth 作为 model_path，并优先匹配 vocoder*.pth
        pths = sorted(d.glob("*.pth"), key=lambda p: p.stat().st_size, reverse=True)
        if not pths:
            raise FileNotFoundError(f"目录内无 .pth 模型：{self._model_dir}")
        model_path = pths[0]
        vocoder = next((p for p in pths if "vocoder" in p.name.lower()), None)
        kwargs = {"language": melo_lang, "device": device}
        if vocoder is not None:
            kwargs["vocoder_path"] = str(vocoder)
        kwargs["model_path"] = str(model_path)
        try:
            model = TTS(**kwargs)
        except TypeError:
            # 旧版 melo 不支持自定义路径参数：退化为内置加载（目录被忽略）
            model = TTS(language=melo_lang, device=device)
        spk = model.hps.data.spk2id
        if not isinstance(spk, dict):
            spk = dict(spk)
        logger.info("[TTS:melotts] 已加载自定义音色 %s (device=%s, speakers=%s)",
                    melo_lang, device, list(spk.keys()))
        return model, spk, device

    def get(self, lang: str):
        # 未知语言回退中文
        melo_lang, _pref = _LANG_MAP.get(lang, _LANG_MAP["zh"])
        with self._lock:
            if melo_lang in self._models:
                return self._models[melo_lang]
            try:
                TTS = _import_melo_tts()
            except Exception as e:  # noqa
                logger.error("melotts 未安装: %s", e)
                return None
            device = self._device()
            loaded = None
            # 优先自定义音色；失败自动回退内置（绝不因自定义模型坏而不可用）
            if self._model_dir:
                try:
                    loaded = self._load_custom(TTS, melo_lang, device)
                except Exception as e:  # noqa
                    logger.warning("[TTS:melotts] 自定义模型加载失败，回退内置: %s",
                                   str(e)[:160])
            if loaded is None:
                try:
                    loaded = self._load_builtin(TTS, melo_lang, device)
                except Exception as e:  # noqa
                    logger.error("[TTS:melotts] 加载语言 %s 失败: %s", melo_lang, e)
                    return None
            self._models[melo_lang] = loaded
            return loaded

    def empty_cache(self) -> None:
        self._models.clear()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa
            pass


class MeloTTSEngine(TTSEngine):
    name = "melotts"

    def _check(self) -> bool:
        """轻量可用性检查：仅用 importlib.util.find_spec 探测 melotts 包是否存在，**不**
        实际执行其沉重 import（import melo 会连带拉起 transformers 等约 15s，且打印
        FutureWarning）。真正的 melo 包 import 与模型加载延迟到首次 synthesize /
        ensure_melotts（均在后台线程），避免阻塞启动主线程导致 UI 迟迟出不来。

        代价：若包存在但运行时依赖损坏，_check 仍报可用，后续 synthesize 会失败并
        由 TTSManager 回退链降级到 edge（行为与「坏安装→不可用」等价，仅 diagnose
        面板可能短暂误报可用），不影响主流程。
        """
        try:
            import importlib.util
            if importlib.util.find_spec("melo") is not None:
                return True
            if importlib.util.find_spec("melotts") is not None:
                return True
        except Exception:  # noqa
            pass
        logger.warning("melotts 未安装，MeloTTS 引擎不可用（将回退 edge）")
        return False

    def ensure_model(self, lang: str = "zh"):
        """预热/下载指定语言模型，返回 (model, sid, device) 或 None。

        首次会按 HF_ENDPOINT（已设 hf-mirror.com）下载权重；失败时返回 None，
        上层可据此回退其它引擎。供开应用后台预热使用，避免首次口播才现下导致卡顿。
        """
        if not self._available:
            return None
        try:
            return self._wrapper.get(lang)
        except Exception as e:  # noqa
            logger.error("MeloTTS 预热语言 %s 失败: %s", lang, e)
            return None

    def reconfigure(self) -> None:
        t = user_config.tts
        self.rate = t.get("rate", "+0%")
        # 音色（说话人 key）：空 = 引擎默认（自动取首个说话人）
        self._speaker = t.get("melotts_speaker", "ZH") or ""
        # ---- MeloTTS 合成参数（设置页「声音调校」滑块） ----
        self.speed = _clamp(float(t.get("melotts_speed", 0.88)), 0.5, 1.5)
        self.sdp_ratio = _clamp(float(t.get("melotts_sdp_ratio", 0.12)), 0.0, 0.5)
        self.noise_scale = _clamp(float(t.get("melotts_noise_scale", 0.32)), 0.0, 1.0)
        self.noise_scale_w = _clamp(float(t.get("melotts_noise_scale_w", 0.45)), 0.0, 1.0)
        # ---- ffmpeg 后处理开关 + 二进制路径 + 7 个后处理参数（滑块） ----
        self.postprocess = bool(t.get("melotts_postprocess", True))
        self.ffmpeg_path = (t.get("melotts_ffmpeg_path", "") or "").strip()
        # ffmpeg 后处理参数（设置页「后处理参数」滑块）。改任意值即触发重新合成
        # （_melo_cache_path 哈希已含这些参数）。
        self.pp_hs1 = _clamp(float(t.get("melotts_pp_hs1", 5.0)), -12.0, 12.0)
        self.pp_hs2 = _clamp(float(t.get("melotts_pp_hs2", 3.0)), -12.0, 12.0)
        self.pp_hs3 = _clamp(float(t.get("melotts_pp_hs3", 2.0)), -12.0, 12.0)
        self.pp_comp_th = _clamp(float(t.get("melotts_pp_comp_th", -20.0)), -40.0, 0.0)
        self.pp_comp_ratio = _clamp(float(t.get("melotts_pp_comp_ratio", 4.0)), 1.0, 10.0)
        self.pp_echo = _clamp(float(t.get("melotts_pp_echo", 0.5)), 0.0, 1.0)
        self.pp_loud_i = _clamp(float(t.get("melotts_pp_loud_i", -16.0)), -24.0, -10.0)
        self._ffmpeg_exe: str | None = None   # 懒解析，命中后缓存
        self._wrapper = _MeloTTSWrapper()
        # 注入自定义音色模型目录（设置页「拖拽导入」写入 melotts_model_dir）
        self._wrapper.set_model_dir(t.get("melotts_model_dir", "") or "")
        # RLock：synthesize_batch 在持锁循环内会调用 _wrapper.get（同样加锁），
        # 非可重入 Lock 会死锁，故用 RLock。
        self._lock = threading.RLock()

    def _melo_cache_path(self, text: str, lang: str) -> Path:
        """带参数哈希的缓存路径：滑块/后处理开关/后处理参数变化即作废旧缓存、重新合成。"""
        params = (f"{self.speed:.2f}_{self.sdp_ratio:.2f}_"
                  f"{self.noise_scale:.2f}_{self.noise_scale_w:.2f}_"
                  f"{int(self.postprocess)}_"
                  f"{self.pp_hs1:.1f}_{self.pp_hs2:.1f}_{self.pp_hs3:.1f}_"
                  f"{self.pp_comp_th:.0f}_{self.pp_comp_ratio:.1f}_"
                  f"{self.pp_echo:.2f}_{self.pp_loud_i:.0f}")
        return self._cache_path(text, tag=f"melotts-{lang}-{params}")

    def _resolve_ffmpeg(self) -> str | None:
        """解析 ffmpeg 二进制：配置路径 → 项目内置 → imageio-ffmpeg → 系统 PATH。"""
        if self._ffmpeg_exe:
            return self._ffmpeg_exe
        cands: list[str] = []
        if self.ffmpeg_path:
            cands.append(self.ffmpeg_path)
        # 项目内置完整构建（tools/ffmpeg/bin/ffmpeg.exe）
        if FFMPEG_BIN and os.path.isfile(str(FFMPEG_BIN)):
            cands.append(str(FFMPEG_BIN))
        # imageio-ffmpeg 自带（若已安装）
        try:
            import imageio_ffmpeg
            cands.append(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:  # noqa
            pass
        # 系统 PATH（用户自行安装）
        cands.append("ffmpeg")
        for c in cands:
            if not c:
                continue
            if os.path.isfile(c) or shutil.which(c):
                self._ffmpeg_exe = c
                logger.info("[TTS:melotts] ffmpeg 位于 %s", c)
                return c
        return None

    def _pp_params(self) -> dict:
        """当前 7 个后处理滑块的取值（交给共享后处理模块）。"""
        return {"hs1": self.pp_hs1, "hs2": self.pp_hs2, "hs3": self.pp_hs3,
                "comp_th": self.pp_comp_th, "comp_ratio": self.pp_comp_ratio,
                "echo": self.pp_echo, "loud_i": self.pp_loud_i}

    def _build_postprocess_filter(self) -> str:
        """按设置页 7 个后处理滑块拼出 ffmpeg 滤镜链（实现在共享模块）。"""
        from core.tts_postprocess import build_filter
        return build_filter(self._pp_params())

    def _postprocess(self, in_path: Path) -> bool:
        """用 ffmpeg 后处理链（按滑块参数拼装）强化播报质感。失败则保留原音频、返回 False。"""
        from core.tts_postprocess import apply as _apply
        ff = self._resolve_ffmpeg()
        if not ff:
            logger.warning("[TTS:melotts] 未找到 ffmpeg，跳过音频后处理（保留原始合成）")
            return False
        return _apply(in_path, self._pp_params(), ffmpeg=ff)

    def _resolve(self, lang: str):
        """按语言加载模型并解析说话人 id，返回 (model, sid, device) 或 None。"""
        loaded = self._wrapper.get(lang)
        if loaded is None:
            return None
        model, spk2id, device = loaded
        melo_lang, _pref = _LANG_MAP.get(lang, _LANG_MAP["zh"])
        sid = _MeloTTSWrapper._resolve_speaker(spk2id, melo_lang, self._speaker)
        return model, sid, device

    def synthesize(self, text: str, lang: str | None = None):
        text = (text or "").strip()
        if not text:
            return None
        text = self._truncate(text)
        if not self._available:
            logger.info("[TTS静默·melotts] %s", text)
            return None
        out_path = self._melo_cache_path(text, "zh")
        if out_path.exists() and out_path.stat().st_size > 0:
            return str(out_path)
        try:
            resolved = self._resolve("zh")
            if resolved is None:
                return None
            model, sid, _dev = resolved
            self._tts_to_file(model, sid, out_path, text)
            if not out_path.exists() or out_path.stat().st_size == 0:
                self._safe_remove(out_path)
                # 偶发 GPU/分配抖动可能产出空文件：重试一次（清缓存后重合成）
                try:
                    resolved = self._resolve("zh")
                    if resolved is not None:
                        self._tts_to_file(resolved[0], resolved[1], out_path, text)
                except Exception:  # noqa
                    pass
                if not out_path.exists() or out_path.stat().st_size == 0:
                    self._safe_remove(out_path)
                    return None
            # 合成成功 → ffmpeg 后处理（均衡/压缩/混响/响度）
            if self.postprocess:
                self._postprocess(out_path)
            return str(out_path)
        except Exception as e:  # noqa
            logger.error("MeloTTS 合成失败: %s", e)
            self._safe_remove(out_path)
            return None

    def _tts_to_file(self, model, sid, out_path, text: str) -> None:
        """调 melo 的 tts_to_file，注入当前「声音调校」参数。"""
        model.tts_to_file(
            text, sid, str(out_path),
            sdp_ratio=self.sdp_ratio,
            noise_scale=self.noise_scale,
            noise_scale_w=self.noise_scale_w,
            speed=self.speed,
        )

    def synthesize_batch(self, segments: list) -> list:
        """批量合成：segments = [(lang, text), ...]。

        按语言选模型，同语言只加载一次（缓存），逐段合成到各自 WAV。
        说话人按配置（melotts_speaker）+ 各语言 spk2id 解析，支持「更换音色」。
        返回与 segments 等长的路径列表（失败项为 None）。
        """
        results: list = []
        with self._lock:
            for lang, text in segments:
                text = self._truncate(text)
                if not text:
                    results.append(None)
                    continue
                resolved = self._resolve(lang)
                if resolved is None:
                    results.append(None)
                    continue
                model, sid, _dev = resolved
                out_path = self._melo_cache_path(text, lang)
                if out_path.exists() and out_path.stat().st_size > 0:
                    results.append(str(out_path))
                    continue
                try:
                    self._tts_to_file(model, sid, out_path, text)
                    if out_path.exists() and out_path.stat().st_size > 0:
                        if self.postprocess:
                            self._postprocess(out_path)
                        results.append(str(out_path))
                    else:
                        self._safe_remove(out_path)
                        results.append(None)
                except Exception as e:  # noqa
                    logger.error("MeloTTS 批量合成失败 lang=%s: %s", lang, e)
                    self._safe_remove(out_path)
                    results.append(None)
        return results

    def empty_cache(self) -> None:
        try:
            self._wrapper.empty_cache()
        except Exception:  # noqa
            pass
