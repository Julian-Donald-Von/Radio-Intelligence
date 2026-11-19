"""Qwen3-TTS 1.7B Base GGUF 引擎（App 侧，Python 3.13，无 torch）。

架构：**每次合成起一次 llama-tts.exe 子进程，退出即释放显存**（按需合成、非常驻），
不 import torch、不占 3.11 venv。用 b10955 的 llama-tts.exe + Q4_K_M backbone
+ mmproj-Q8，做参考音频零样本克隆。

音色（★ 2026-11-18 用户两次拍板）：**每个独立可选音色都负责中/英/日全部语种**，
不按语种分工。config.tts.qwen3tts_voice 选（9 个；前 2 个原有，后 7 个 11-18 试听后保留）：
    alba-mackenna  -> voices/alba-mackenna_casual.wav  (Kyutai, 女声)          [原有]
    ono-anna       -> voices/Ono_Anna.flac             (Qwen 内置日语女声)      [原有]
    serena         -> voices/Serena.flac               (真人录音·温柔女声)
    uncle-fu       -> voices/Uncle_Fu.flac             (真人录音·低沉男声/夜场DJ)
    四国めたん      -> voices/四国めたん_normal.wav       (VOICEVOX style 2)
    青山龍星       -> voices/青山龍星_normal.wav         (VOICEVOX style 13)
    もち子さん      -> voices/もち子さん_normal.wav       (VOICEVOX style 20)
    春日部つむぎ    -> voices/春日部つむぎ_normal.wav     (VOICEVOX style 8)
    中部つるぎ      -> voices/中部つるぎ_normal.wav       (VOICEVOX style 94)
选中的音色对所有语言段用同一个参考音频；--tts-lang 仍按段的语言标签走 zh/en/ja。
新增 7 个参考音由 tools/research/gen_voicebank_refs_20261118.py 生成、已复制到 voices/。

已验证参数（2026-11-18 实测，见 docs/QWEN3_TTS_Q4VSQ8_BENCH_20261118.md）：
    -c 4096 -ngl 20 -n 640 --logit-bias 1023-inf
    ★★ 2026-11-18 最终定稿：ngl 20 混合 offload（35B 常驻下必需）。
      显存账：35B fit(4.2G)+TTS ngl99(4.4G)=8.6G>8GB ⇒ 换页 21~42s ⇒ 90s 超时
      ⇒ 口播全默认（真机实证）。ngl20 占~2G，合计 6.2G 不换页，稳定。
      单测 35B 空闲时 99 更快(3.6s vs 17s)，但生产是 35B 常驻忙态，必须 20。
      默认 20，可用 config.tts.qwen3tts_ngl 覆盖。

配置（data_runtime/config.json 的 tts 段，全部可选）：
    qwen3tts_voice     音色 id，默认 alba-mackenna
    qwen3tts_exe       llama-tts.exe 路径
    qwen3tts_model     Q4_K_M gguf 路径
    qwen3tts_mmproj    mmproj-Q8 gguf 路径
    qwen3tts_timeout   单次合成超时秒，默认 120
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from config.settings import PROJECT_ROOT, TTS_TMP_DIR
from core.tts_base import TTSEngine

logger = logging.getLogger("MusicAI.TTS")

# 9 个独立可选音色，每个覆盖全部语种（不按语种分工）。
# key 必须是小写（_load_cfg 会 .strip().lower()；中文无大小写，原样保留）。
_VOICES = {
    "alba-mackenna": "alba-mackenna_casual.wav",
    "ono-anna": "Ono_Anna.flac",
    "serena": "Serena.flac",
    "uncle-fu": "Uncle_Fu.flac",
    "四国めたん": "四国めたん_normal.wav",
    "青山龍星": "青山龍星_normal.wav",
    "もち子さん": "もち子さん_normal.wav",
    "春日部つむぎ": "春日部つむぎ_normal.wav",
    "中部つるぎ": "中部つるぎ_normal.wav",
}
_DEFAULT_VOICE = "alba-mackenna"
# Qwen3-TTS 官方语言码；split_tagged 已输出小写 zh/en/ja
_LANG_MAP = {"zh": "zh", "en": "en", "ja": "ja", "ko": "ko", "fr": "fr",
             "de": "de", "es": "es", "it": "it", "pt": "pt", "ru": "ru"}
_SENT_SPLIT = re.compile(r"(?<=[。！？.!?；;])")
_MAX_CHARS = 380  # Qwen3-TTS 单次调用上限约 2048 帧≈2.5min，保守按字符切段


class Qwen3TTSEngine(TTSEngine):
    name = "qwen3tts"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        super().__init__()

    def _defaults(self) -> dict:
        base = PROJECT_ROOT / "models" / "qwen3_tts_1.7b"
        return {
            "voice": _DEFAULT_VOICE,
            "exe": str(PROJECT_ROOT / "tools" / "llamacpp-b10955" / "llama-tts.exe"),
            "model": str(base / "Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf"),
            "mmproj": str(base / "mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf"),
            "voices_dir": str(base / "voices"),
            "timeout": 120,
            "ngl": 20,  # ★★ 2026-11-18 最终定稿：35B 常驻下必须用混合 offload(20)。
                        #   显存账：35B fit 占 4.2G + TTS ngl99 要 4.4G = 8.6G > 8GB ⇒
                        #   换页飙到 21~42s → 流水线 90s 超时 → 二三首全默认文案（实测）。
                        #   ngl20 占 ~2G，合计 6.2G < 8GB 不换页，稳定 7-10s。
                        #   （35B 空闲时 ngl99 更快，但生产是 35B 常驻忙态。）
        }

    def _load_cfg(self) -> None:
        d = self._defaults()
        try:
            from config.settings import user_config  # type: ignore
            tts = user_config.tts or {}
        except Exception:  # noqa
            tts = {}
        for key in ("voice", "exe", "model", "mmproj", "voices_dir", "timeout", "ngl"):
            d[key] = tts.get("qwen3tts_" + key, d[key]) or d[key]
        try:
            self._ngl = int(d["ngl"])
        except Exception:  # noqa
            self._ngl = 20
        # ★ 后处理：与 IndexTTS/MeloTTS 共用同一条 ffmpeg 链 + 同一组 7 个 pp 滑块
        self._postprocess_on = bool(tts.get("qwen3tts_postprocess", False))
        try:
            from core.tts_postprocess import params_from_config
            self._pp_params = params_from_config(tts)
        except Exception:  # noqa
            self._pp_params = None
        self._voice = str(d["voice"]).strip().lower()
        if self._voice not in _VOICES:
            logger.warning("[TTS:qwen3tts] 未知音色 %s，回退 %s", self._voice, _DEFAULT_VOICE)
            self._voice = _DEFAULT_VOICE
        self._exe = Path(str(d["exe"]))
        self._model = Path(str(d["model"]))
        self._mmproj = Path(str(d["mmproj"]))
        self._voices_dir = Path(str(d["voices_dir"]))
        try:
            self._timeout = float(d["timeout"])
        except Exception:  # noqa
            self._timeout = 120.0

    @property
    def _ref(self) -> Path:
        return self._voices_dir / _VOICES[self._voice]

    def reconfigure(self) -> None:
        self._load_cfg()

    def _check(self) -> bool:
        self._load_cfg()
        ok = self._exe.exists() and self._model.exists() and self._mmproj.exists()
        if not ok:
            logger.warning("[TTS:qwen3tts] 依赖缺失 exe=%s model=%s mmproj=%s",
                           self._exe.exists(), self._model.exists(), self._mmproj.exists())
        return ok

    def available(self) -> bool:
        # 叶片懒检查（音色/路径可能在运行时变更）
        return self._exe.exists() and self._model.exists() and self._mmproj.exists() \
            and self._ref.exists()

    def _chunks(self, text: str) -> list[str]:
        """按句界切成 <= _MAX_CHARS 的段，避免长文本单次调用漂移。"""
        text = (text or "").strip()
        if len(text) <= _MAX_CHARS:
            return [text] if text else []
        out, buf = [], ""
        for sent in _SENT_SPLIT.split(text):
            if not sent:
                continue
            if len(buf) + len(sent) > _MAX_CHARS and buf:
                out.append(buf.strip())
                buf = ""
            buf += sent
        if buf.strip():
            out.append(buf.strip())
        return out

    def _synth_one(self, text: str, lang: str, out: Path) -> bool:
        cmd = [str(self._exe), "-m", str(self._model), "-mm", str(self._mmproj),
               "--tts-speaker-file", str(self._ref),
               "--tts-lang", lang,
               "-p", text, "-o", str(out),
               "-c", "4096", "-ngl", str(self._ngl), "-n", "640",
               "--logit-bias", "1023-inf"]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self._timeout)
            if r.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
                logger.error("[TTS:qwen3tts] 合成失败 lang=%s rc=%s: %s",
                             lang, r.returncode,
                             r.stderr.decode("utf-8", "replace")[-300:] if r.stderr else "")
                return False
            if self._postprocess_on:
                try:
                    from core.tts_postprocess import apply as _pp_apply
                    if _pp_apply(out, self._pp_params):
                        logger.info("[TTS:qwen3tts] 后处理完成 lang=%s", lang)
                except Exception as e:  # noqa
                    logger.warning("[TTS:qwen3tts] 后处理失败（保留原始合成）：%s", e)
            logger.info("[TTS:qwen3tts] 合成 lang=%s voice=%s ngl=%s %.1fs -> %s",
                        lang, self._voice, self._ngl, time.time() - t0, out.name)
            return True
        except subprocess.TimeoutExpired:
            logger.error("[TTS:qwen3tts] 合成超时 lang=%s (%.0fs)", lang, self._timeout)
            return False
        except Exception as e:  # noqa
            logger.error("[TTS:qwen3tts] 合成异常 lang=%s: %s", lang, e)
            return False

    def synthesize(self, text: str, lang: str | None = None) -> str | None:
        text = self._truncate(text or "")
        parts = self._chunks(text)
        if not parts:
            return None
        code = _LANG_MAP.get((lang or "zh").lower(), "zh")
        with self._lock:
            paths: list[Path] = []
            for i, part in enumerate(parts):
                out = self._cache_path(part, tag=f"{self._voice}-{code}-{i}", ext="wav")
                if self._synth_one(part, code, out):
                    paths.append(out)
            if not paths:
                return None
            if len(paths) == 1:
                return str(paths[0])
            from core.tts_segment import concat_wavs_with_silence
            merged = self._cache_path(text, tag=f"{self._voice}-{code}-merged", ext="wav")
            return concat_wavs_with_silence([str(p) for p in paths], str(merged))

    def synthesize_batch(self, segments: list) -> list:
        return [self.synthesize(text, lang=lang) for lang, text in segments]

    def empty_cache(self) -> None:
        # 按需合成：进程退出即释放显存，无需显式卸载
        return None

    def diagnose(self) -> dict:
        return {
            "engine": self.name, "voice": self._voice,
            "exe": str(self._exe), "model": str(self._model),
            "mmproj": str(self._mmproj), "ref": str(self._ref),
            "available": self.available(),
        }
