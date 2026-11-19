"""
ChatTTS ONNX TTS 引擎（本地、显存 ~1.2GB，支持情绪标签）。

依赖 onnxruntime-gpu，模型放在 ./models/chattts-onnx/。
ONNX 推理走 CUDA（CUDAExecutionProvider），显存不足时自动回退 CPU。

支持的情绪标签（原样透传进文本，ChatTTS 原生识别）：
  [break_6] [laugh] [uv_break] [speed_9] 等。

模型约定（部署脚本会把对应权重放进 ./models/chattts-onnx/）：
  - encoder.onnx   : 文本/ token → 语义嵌入
  - decoder.onnx   : 嵌入 → 梅尔频谱（如导出为自回归整体，则一次出频谱）
  - vocoder.onnx   : 梅尔频谱 → 波形(wav)
  - tokenizer 复用官方 ChatTTS 包的 tokenizer（若已 pip install ChatTTS）；
    缺失时退化为字符级兜底（仍本地、仍可出声，但韵律较弱）。
  - 可选 chattts_config.json：{"sample_rate": 24000}

任何依赖/权重缺失 → available=False → 上层切到其它引擎或静音（不崩溃）。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from config.settings import CHATTTS_DIR, GPUConfig, user_config
from core.tts_base import TTSEngine

logger = logging.getLogger("MusicAI.TTS")

# ChatTTS 原生情绪标签（仅用于校验/透传提示）
EMOTION_TAGS = ("[break_0]", "[break_2]", "[break_4]", "[break_6]", "[break_8]",
                "[laugh]", "[uv_break]", "[speed_0]", "[speed_2]", "[speed_4]",
                "[speed_6]", "[speed_8]", "[speed_9]")

REQUIRED_FILES = ("encoder.onnx", "decoder.onnx", "vocoder.onnx")


class ChatTTSEngine(TTSEngine):
    name = "chattts"

    def _check(self) -> bool:
        try:
            import onnxruntime as ort  # noqa: F401
        except Exception as e:  # noqa
            logger.warning("onnxruntime 未安装，ChatTTS 引擎不可用: %s",
                           str(e)[:120])
            return False
        missing = [f for f in REQUIRED_FILES if not (CHATTTS_DIR / f).exists()]
        if missing:
            # 仅当 ChatTTS 是「当前选中引擎」时才报警；非选中引擎（如默认 melotts）
            # 缺失权重属正常降级，降级为 info，避免每次启动刷屏告警。
            if (user_config.tts.get("engine") == "chattts"):
                logger.warning("ChatTTS 权重缺失（%s 下缺：%s），引擎不可用",
                               CHATTTS_DIR, missing)
            else:
                logger.info("ChatTTS 权重缺失（%s 下缺：%s），已跳过（当前引擎=%s，非 chattts）",
                            CHATTTS_DIR, missing, user_config.tts.get("engine"))
            return False
        return True

    def reconfigure(self) -> None:
        t = user_config.tts
        self.voice_id = t.get("voice_id", "")
        self.rate = t.get("rate", "+0%")
        self.emotion_tags = bool(t.get("emotion_tags", True))
        self._sessions = None
        self._tokenizer = None
        self._lock = threading.Lock()
        self._sample_rate = self._load_sr()

    def _load_sr(self) -> int:
        cfg = CHATTTS_DIR / "chattts_config.json"
        if cfg.exists():
            try:
                import json
                return int(json.loads(cfg.read_text(encoding="utf-8"))
                           .get("sample_rate", 24000))
            except Exception:  # noqa
                pass
        return 24000

    # ---------------- ONNX 会话 ----------------
    def _build_sessions(self):
        if self._sessions is not None:
            return self._sessions
        import onnxruntime as ort
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if GPUConfig.DEVICE_INDEX:
            providers = [
                ("CUDAExecutionProvider",
                 {"device_id": int(GPUConfig.DEVICE_INDEX)}),
                "CPUExecutionProvider",
            ]
        enc = ort.InferenceSession(str(CHATTTS_DIR / "encoder.onnx"),
                                   providers=providers)
        dec = ort.InferenceSession(str(CHATTTS_DIR / "decoder.onnx"),
                                   providers=providers)
        voc = ort.InferenceSession(str(CHATTTS_DIR / "vocoder.onnx"),
                                   providers=providers)
        self._sessions = (enc, dec, voc)
        logger.info("[TTS:chattts] ONNX 会话就绪（CUDA 优先）")
        return self._sessions

    def _get_tokenizer(self):
        if self._tokenizer is not None:
            return self._tokenizer
        try:
            from ChatTTS import ChatTTS  # type: ignore
            self._tokenizer = ChatTTS.tokenizer
        except Exception:  # noqa
            self._tokenizer = None
        return self._tokenizer

    def _tokenize(self, text: str):
        tok = self._get_tokenizer()
        if tok is not None:
            try:
                return tok.tokenize(text)
            except Exception:  # noqa
                pass
        # 兜底：字符级（仅保证可跑通，韵律弱）
        return list(text)

    # ---------------- 推理 ----------------
    def synthesize(self, text: str):
        text = (text or "").strip()
        if not text:
            return None
        text = self._truncate(text)
        if not self._available:
            logger.info("[TTS静默·chattts] %s", text)
            return None
        out_path = self._cache_path(text, tag=self.voice_id or "default")
        if out_path.exists() and out_path.stat().st_size > 0:
            return str(out_path)
        try:
            with self._lock:
                enc, dec, voc = self._build_sessions()
                # 可选：应用音色 embedding（VoiceManager 提供的 .npy/.pt）
                spk = self._load_speaker_embedding()
                tokens = self._tokenize(text)
                emb = enc.run(None, self._enc_feed(tokens, spk))[0]
                mel = dec.run(None, self._dec_feed(emb, spk))[0]
                wav = voc.run(None, {"mel": mel})[0]
            self._save_wav(wav, out_path)
            if not out_path.exists() or out_path.stat().st_size == 0:
                self._safe_remove(out_path)
                return None
            return str(out_path)
        except Exception as e:  # noqa
            logger.error("ChatTTS 合成失败: %s", e)
            self._safe_remove(out_path)
            return None

    # feed 构造：兼容常见 ChatTTS ONNX 导出输入名
    def _enc_feed(self, tokens, spk):
        return {"text": tokens} if isinstance(tokens, str) else {"tokens": tokens}

    def _dec_feed(self, emb, spk):
        feed = {"embedding": emb}
        if spk is not None:
            feed["spk"] = spk
        return feed

    def _load_speaker_embedding(self):
        # 音色 embedding 由 VoiceManager 经 set_speaker() 注入；默认 None（用模型默认音色）
        return getattr(self, "_spk", None)

    def set_speaker(self, embedding) -> None:
        self._spk = embedding

    def _save_wav(self, wav, out_path: Path) -> None:
        import numpy as np
        import wave
        data = np.squeeze(np.asarray(wav)).astype(np.float32)
        # 归一化到 int16
        peak = float(np.max(np.abs(data))) or 1.0
        pcm = (data / peak * 32767.0).astype("<i2")
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm.tobytes())

    def empty_cache(self) -> None:
        self._sessions = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa
            pass
