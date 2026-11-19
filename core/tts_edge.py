"""
Edge 云 TTS 引擎（微软 Azure 在线语音，零配置兜底）。

作为「云兜底」引擎保留：当本地 MeloTTS / VOICEVOX 不可用或用户未配置本地模型时，
仍能正常出声（需联网）。

本次扩展：
  - 支持按语言选音色（lang 参数）：zh/en/ja/es/fr/de/ko/ru 等，供 VOICEVOX/MeloTTS
    不可用时的分段兜底。
  - 支持 as_wav=True：生成后转成 WAV（用于带标签分段管线拼接；无 ffmpeg 时
    尝试用 soundfile 解码 mp3，失败则降级返回 None，由调度器跳过该片段）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from config.settings import TTS_TMP_DIR, TTSConfig, user_config
from core.tts_base import TTSEngine

logger = logging.getLogger("MusicAI.TTS")

# 语言 -> Edge 音色（兜底用；覆盖口播常见语言）
EDGE_LANG_VOICES = {
    "zh": "zh-CN-XiaoxiaoNeural",
    "en": "en-US-GuyNeural",
    "ja": "ja-JP-NanamiNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "ko": "ko-KR-SunHiNeural",
    "ru": "ru-RU-SvetlanaNeural",
}


class EdgeTTSEngine(TTSEngine):
    name = "edge"

    def _check(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except Exception:  # noqa
            logger.warning("edge-tts 未安装/不可用，云 TTS 降级为静默模式")
            return False

    def reconfigure(self) -> None:
        t = user_config.tts
        self.voice = t.get("voice", TTSConfig.VOICE)
        self.rate = t.get("rate", TTSConfig.RATE)
        self.volume = t.get("volume", TTSConfig.VOLUME)

    def _voice_for_lang(self, lang: str | None) -> str:
        if lang and lang in EDGE_LANG_VOICES:
            return EDGE_LANG_VOICES[lang]
        return self.voice

    def _cache_path(self, text: str, tag: str = "", ext: str = "mp3") -> Path:
        key = f"{self.name}|{tag}|{text}"
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        return TTS_TMP_DIR / f"{h}.{ext}"

    async def _synthesize_async(self, text: str, out_path, voice: str) -> None:
        import edge_tts
        communicate = edge_tts.Communicate(
            text, voice, rate=self.rate, volume=self.volume,
        )
        await communicate.save(str(out_path))

    def _gen(self, text: str, out_path, voice: str) -> str | None:
        try:
            asyncio.run(self._synthesize_async(text, out_path, voice))
        except Exception as e:  # noqa
            logger.error("Edge TTS 合成失败: %s", e)
            self._safe_remove(out_path)
            return None
        if not out_path.exists() or out_path.stat().st_size == 0:
            logger.error("Edge TTS 合成结果为空（网络异常？），已丢弃缓存")
            self._safe_remove(out_path)
            return None
        return str(out_path)

    def _mp3_to_wav(self, mp3_path: Path, wav_path: Path) -> bool:
        try:
            import soundfile as sf
            data, sr = sf.read(str(mp3_path), dtype="float32", always_2d=True)
            if data.shape[1] > 1:
                data = data.mean(axis=1, keepdims=True)
            sf.write(str(wav_path), data, sr)
            return True
        except Exception as e:  # noqa
            logger.warning("[TTS] mp3→wav 失败（libsndfile 可能不支持 mp3 解码）: %s",
                           str(e)[:120])
            return False

    def synthesize(self, text: str, lang: str | None = None,
                   as_wav: bool = False):
        text = (text or "").strip()
        if not text:
            return None
        text = self._truncate(text)
        if not self._available:
            logger.info("[TTS静默·edge] %s", text)
            return None
        voice = self._voice_for_lang(lang)

        if as_wav:
            wav_path = self._cache_path(text, tag=f"edge-{lang or 'zh'}-wav", ext="wav")
            if wav_path.exists() and wav_path.stat().st_size > 0:
                return str(wav_path)
            mp3_path = self._cache_path(text, tag=f"edge-{lang or 'zh'}")
            if not self._gen(text, mp3_path, voice):
                return None
            if self._mp3_to_wav(mp3_path, wav_path):
                self._safe_remove(mp3_path)
                return str(wav_path)
            self._safe_remove(mp3_path)
            return None

        # 默认行为（兼容旧调用）：返回 mp3
        out_path = self._cache_path(text, tag=f"edge-{lang or 'zh'}")
        if out_path.exists() and out_path.stat().st_size > 0:
            return str(out_path)
        return self._gen(text, out_path, voice)
