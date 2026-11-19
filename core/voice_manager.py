"""
音色管理系统：扫描 ./voices/、注册音色、运行时切换、导入新音色、自动识别。

音色注册表 ./voices/config.json 每条记录格式（部署规范）：
  {"voice_id": "my_voice", "file": "my_voice.npy",
   "engine": "chattts", "emotion_tags": true}

- engine: chattts | edge
- 支持的音色文件：.npy(说话人嵌入) / .pt(torch state)
- 注：本地多语 TTS 已统一改用 MeloTTS（按语言自动加载模型），不再需要用户导入 .onnx 语音包
- import_voice(file_path, name, engine_type)：复制进 ./voices/ 并写入注册表
- watch()：后台轮询 ./voices/，用户拖放/复制的新文件自动识别并注册（无需重启）
- load_embedding(entry)：把 .npy/.pt/.onnx 载入为说话人向量，供引擎使用
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from config.settings import VOICES_CONFIG, VOICES_DIR

logger = logging.getLogger("MusicAI.Voice")

# 扩展名 → 推断引擎（自动识别用）
_EXT_ENGINE = {
    ".npy": "chattts",   # 说话人嵌入
    ".pt": "chattts",    # torch state
}


class VoiceManager:
    def __init__(self, auto_watch: bool = True) -> None:
        self._lock = threading.RLock()
        self._voices: list[dict] = []
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self.scan()
        if auto_watch:
            self.start_watch()

    # ---------------- 扫描 / 加载 ----------------
    def scan(self) -> list[dict]:
        """（重新）读取 ./voices/config.json，规范化为列表。"""
        with self._lock:
            entries: list[dict] = []
            if VOICES_CONFIG.exists():
                try:
                    data = json.loads(VOICES_CONFIG.read_text(encoding="utf-8"))
                except Exception as e:  # noqa
                    logger.warning("读取音色配置失败: %s", e)
                    data = []
                if isinstance(data, list):
                    entries = data
                elif isinstance(data, dict):
                    if "voices" in data and isinstance(data["voices"], list):
                        entries = data["voices"]
                    else:
                        entries = [data]
            # 校验文件存在性，缺失则标记
            for e in entries:
                fp = VOICES_DIR / e.get("file", "")
                e["_resolved"] = str(fp) if fp.exists() else None
                e.setdefault("engine", _EXT_ENGINE.get(
                    Path(e.get("file", "")).suffix.lower(), "chattts"))
            self._voices = entries
            logger.info("[音色] 已加载 %d 条音色", len(entries))
            return entries

    def list_voices(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._voices]

    def get(self, voice_id: str) -> dict | None:
        with self._lock:
            for v in self._voices:
                if v.get("voice_id") == voice_id:
                    return dict(v)
        return None

    def resolve(self, voice_id: str) -> tuple[str, str | None, dict | None]:
        """返回 (engine, file_path, entry)。"""
        v = self.get(voice_id)
        if not v:
            return "", None, None
        return v.get("engine", "chattts"), v.get("_resolved"), v

    # ---------------- 注册 / 导入 ----------------
    def _save(self) -> None:
        VOICES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = VOICES_CONFIG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._voices, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(VOICES_CONFIG)

    def import_voice(self, file_path: str, name: str,
                     engine_type: str) -> str:
        """复制音色文件到 ./voices/ 并注册，返回 voice_id。"""
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"音色文件不存在：{file_path}")
        ext = src.suffix.lower()
        voice_id = name or src.stem
        dst = VOICES_DIR / f"{voice_id}{ext}"
        # 避免覆盖：重名追加序号
        i = 1
        while dst.exists():
            dst = VOICES_DIR / f"{voice_id}_{i}{ext}"
            i += 1
        shutil.copy2(src, dst)
        entry = {
            "voice_id": dst.stem,
            "file": dst.name,
            "engine": engine_type or _EXT_ENGINE.get(ext, "chattts"),
            "emotion_tags": True,
        }
        with self._lock:
            # 同 voice_id 覆盖，否则追加
            replaced = False
            for idx, v in enumerate(self._voices):
                if v.get("voice_id") == entry["voice_id"]:
                    self._voices[idx] = entry
                    replaced = True
                    break
            if not replaced:
                self._voices.append(entry)
            self._save()
        logger.info("[音色] 已导入 %s → %s", dst.name, entry["engine"])
        return entry["voice_id"]

    def load_embedding(self, entry: dict):
        """载入说话人向量（.npy/.pt），供 ChatTTS 使用。"""
        fp = entry.get("_resolved")
        if not fp:
            return None
        p = Path(fp)
        try:
            if p.suffix.lower() == ".npy":
                import numpy as np
                return np.load(str(p))
            if p.suffix.lower() == ".pt":
                import torch
                return torch.load(str(p), map_location="cpu")
            if p.suffix.lower() == ".onnx":
                # 历史 .onnx 语音包（Piper）：MeloTTS 已不再使用，这里不载入向量
                return None
        except Exception as e:  # noqa
            logger.warning("载入音色向量失败 %s: %s", fp, e)
        return None

    # ---------------- 自动识别新文件 ----------------
    def _auto_register_new(self) -> int:
        try:
            files = {p.name for p in VOICES_DIR.iterdir()
                     if p.is_file() and p.suffix.lower() in _EXT_ENGINE}
        except Exception:  # noqa
            return 0
        with self._lock:
            registered = {v.get("file") for v in self._voices}
            added = 0
            for fname in files:
                if fname in registered or fname == "config.json":
                    continue
                ext = Path(fname).suffix.lower()
                entry = {
                    "voice_id": Path(fname).stem,
                    "file": fname,
                    "engine": _EXT_ENGINE.get(ext, "chattts"),
                    "emotion_tags": True,
                    "auto_detected": True,
                }
                self._voices.append(entry)
                added += 1
                logger.info("[音色] 自动识别新音色 %s → %s",
                            fname, entry["engine"])
            if added:
                self._save()
            return added

    def start_watch(self, interval: float = 3.0) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()

        def _loop():
            while not self._watch_stop.is_set():
                try:
                    self._auto_register_new()
                except Exception:  # noqa
                    pass
                self._watch_stop.wait(interval)

        self._watch_thread = threading.Thread(target=_loop, daemon=True)
        self._watch_thread.start()
        logger.info("[音色] 自动识别监视已启动（间隔 %.0fs）", interval)

    def stop_watch(self) -> None:
        self._watch_stop.set()
