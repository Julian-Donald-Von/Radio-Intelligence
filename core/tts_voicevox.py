"""
VOICEVOX TTS 引擎（本地 headless 引擎，日文专属）。

VOICEVOX 提供独立的「引擎」（vv-engine/run.exe），默认在
http://127.0.0.1:50021 提供 HTTP 合成接口。本应用直接复用你已安装的
VOICEVOX 引擎，无需再手动打开 VOICEVOX 桌面 App：

- 需要日文 TTS 时，若本地引擎服务未启动，自动探测并拉起 headless 引擎
  （run.exe，无 GUI 窗口），等待 /version 就绪后即可合成；
- 应用退出时自动关闭该引擎子进程，做到「内嵌、不单独挂起」。

合成仍走原 HTTP 接口：/audio_query 取合成参数 → /synthesis 取 WAV 音频。
失败降级：引擎拉起失败/合成失败 → 返回 None，由调度器回退到 Edge 日文音色。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from config.settings import VOICEVOX_HOST, user_config
from core.tts_base import TTSEngine

logger = logging.getLogger("MusicAI.TTS")

# 进程级：被本应用拉起的 VOICEVOX 引擎子进程（用于退出时回收，避免残留挂起）。
_VOX_PROC = None
_VOX_LOCK = threading.Lock()


def _candidate_exe_paths(override: str | None) -> list[str]:
    """返回 VOICEVOX headless 引擎（run.exe）的可能路径，按优先级排序。"""
    cands: list[str] = []
    if override:
        cands.append(override)
    # 用户把 VOICEVOX 装到了 D 盘（与项目安装偏好一致）
    cands.append(r"D:\Program Files\voicevox\vv-engine\run.exe")
    cands.append(r"C:\Program Files\VOICEVOX\vv-engine\run.exe")
    cands.append(r"C:\Program Files (x86)\VOICEVOX\vv-engine\run.exe")
    # 官方独立引擎包（github releases 解压目录）
    local = os.path.expandvars(r"%LOCALAPPDATA%\voicevox-engine")
    if local:
        for root, _dirs, files in os.walk(local):
            if "run.exe" in files:
                cands.append(os.path.join(root, "run.exe"))
                break
    return [c for c in cands if c and os.path.isfile(c)]


def stop_voicevox_engine() -> None:
    """终止本应用拉起的 VOICEVOX 引擎子进程（应用退出时调用）。

    仅回收由 ensure_running 启动的进程，绝不碰用户手动启动的 VOICEVOX App。
    """
    global _VOX_PROC
    with _VOX_LOCK:
        proc = _VOX_PROC
        _VOX_PROC = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            logger.info("[TTS:voicevox] 关闭内嵌 VOICEVOX 引擎子进程 (pid=%s)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa
                proc.kill()
    except Exception as e:  # noqa
        logger.warning("[TTS:voicevox] 关闭引擎子进程失败: %s", str(e)[:120])


class VoicevoxEngine(TTSEngine):
    name = "voicevox"
    # 默认 speaker（四国めたん 在 VOICEVOX 标准库中的常见 id；解析失败时用它兜底）
    DEFAULT_SPEAKER_ID = 2

    def __init__(self) -> None:
        host = (user_config.tts.get("voicevox_host") or VOICEVOX_HOST)
        self._base = f"http://{host}"
        self._host, self._port = self._parse_host(host)
        self._speaker_name = "四国めたん"
        self._speaker_id: int | None = None
        self._lock = threading.Lock()
        self._exe = (user_config.tts.get("voicevox_exe") or "").strip()
        super().__init__()

    @staticmethod
    def _parse_host(host: str):
        h, p = (host.split(":") + ["50021"])[:2]
        try:
            return h or "127.0.0.1", int(p or 50021)
        except ValueError:
            return "127.0.0.1", 50021

    def _check(self) -> bool:
        try:
            import requests  # noqa: F401
        except Exception as e:  # noqa
            logger.warning("requests 未安装，VOICEVOX 引擎不可用: %s", str(e)[:120])
            return False
        try:
            import requests
            r = requests.get(f"{self._base}/version", timeout=2)
            return r.status_code == 200
        except Exception as e:  # noqa
            logger.info("VOICEVOX 服务不可达（未启动？）: %s", str(e)[:120])
            return False

    def ensure_running(self, timeout: float = 45.0) -> bool:
        """确保本地 VOICEVOX 引擎服务可用：若已可达直接返回；否则自动探测并拉起
        headless 引擎（run.exe），等待其 /version 就绪。失败返回 False（上层回退）。"""
        if self._check():
            return True
        cands = _candidate_exe_paths(self._exe)
        if not cands:
            logger.warning(
                "[TTS:voicevox] 未找到 VOICEVOX 引擎(run.exe)，请在设置页填写 "
                "tts.voicevox_exe 或手动启动 VOICEVOX。日文口播将回退 Edge。")
            return False
        exe = cands[0]
        engine_dir = str(Path(exe).parent)
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            logger.info("[TTS:voicevox] 自动拉起 headless 引擎: %s (cwd=%s)", exe, engine_dir)
            proc = subprocess.Popen(
                [exe, "--host", self._host, "--port", str(self._port)],
                cwd=engine_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as e:  # noqa
            logger.error("[TTS:voicevox] 拉起引擎失败: %s", e)
            return False

        global _VOX_PROC
        with _VOX_LOCK:
            # 只管理本应用拉起的第一个引擎进程
            if _VOX_PROC is None:
                _VOX_PROC = proc

        # 轮询等待 /version 就绪（引擎加载 ONNX 模型可能需十几秒）
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                logger.error("[TTS:voicevox] 引擎进程意外退出（退出码 %s）", proc.returncode)
                return False
            try:
                import requests
                r = requests.get(f"{self._base}/version", timeout=2)
                if r.status_code == 200:
                    self._available = True
                    logger.info("[TTS:voicevox] 引擎已就绪，日文口播可用。")
                    return True
            except Exception:  # noqa
                pass
            time.sleep(0.5)
        logger.error("[TTS:voicevox] 引擎启动超时（%ss 内未就绪）", timeout)
        return False

    def reconfigure(self) -> None:
        t = user_config.tts
        host = (t.get("voicevox_host") or VOICEVOX_HOST)
        self._base = f"http://{host}"
        self._host, self._port = self._parse_host(host)
        self._exe = (t.get("voicevox_exe") or "").strip()
        self.rate = t.get("rate", "+0%")
        self.volume = t.get("volume", "+0%")
        # speaker 名称可能随配置变化，重置以便重新解析
        self._speaker_id = None

    # ---------------- speaker 解析 ----------------
    def _resolve_speaker(self) -> int:
        if self._speaker_id is not None:
            return self._speaker_id
        try:
            import requests
            r = requests.get(f"{self._base}/speakers", timeout=5)
            r.raise_for_status()
            for sp in r.json():
                if sp.get("name") == self._speaker_name:
                    styles = sp.get("styles") or []
                    if styles:
                        self._speaker_id = int(styles[0]["id"])
                        logger.info("[TTS:voicevox] 解析 speaker '%s' -> id=%s",
                                    self._speaker_name, self._speaker_id)
                        return self._speaker_id
        except Exception as e:  # noqa
            logger.warning("VOICEVOX 解析 speaker 失败，用默认 id=%s: %s",
                           self.DEFAULT_SPEAKER_ID, str(e)[:120])
        self._speaker_id = self.DEFAULT_SPEAKER_ID
        return self._speaker_id

    def _speed_scale(self) -> float:
        """把 rate(+N%/-N%) 折算成 VOICEVOX 的 speedScale（1.0 为原速）。"""
        import re
        m = re.search(r"([+-]?\d+)", self.rate or "")
        pct = int(m.group(1)) if m else 0
        # 语速 +N% → 更快 → speedScale 更大
        return max(0.5, min(2.0, 1.0 + pct / 100.0))

    # ---------------- 合成 ----------------
    def _synth_one(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        # 合成前确保引擎已拉起（首次/被关闭后自动恢复），拉起失败则回退上层。
        if not self._available:
            if not self.ensure_running():
                logger.info("[TTS静默·voicevox] 引擎不可用，日文口播回退 Edge。")
                return None
        out_path = self._cache_path(text, tag=f"voicevox-{self._speaker_id}")
        if out_path.exists() and out_path.stat().st_size > 0:
            return str(out_path)
        try:
            import requests
            sid = self._resolve_speaker()
            q = requests.post(
                f"{self._base}/audio_query",
                params={"text": text, "speaker": sid}, timeout=30)
            q.raise_for_status()
            query = q.json()
            # 应用语速（不覆盖用户未设置的其它参数）
            query["speedScale"] = self._speed_scale()
            syn = requests.post(
                f"{self._base}/synthesis",
                params={"speaker": sid}, json=query, timeout=60)
            syn.raise_for_status()
            with open(out_path, "wb") as f:
                f.write(syn.content)
            if not out_path.exists() or out_path.stat().st_size == 0:
                self._safe_remove(out_path)
                return None
            return str(out_path)
        except Exception as e:  # noqa
            logger.error("VOICEVOX 合成失败: %s", e)
            self._safe_remove(out_path)
            return None

    def synthesize(self, text: str):
        text = (text or "").strip()
        if not text:
            return None
        # 与 synthesize_batch 一致：未就绪时自动拉起 headless 引擎，避免单句合成在
        # 引擎未启动时永远返回 None（此前口播单句路径从未触发 VOICEVOX）。
        if not self._available:
            try:
                if not self.ensure_running():
                    logger.info("[TTS静默·voicevox] 引擎不可用，日文口播回退。")
                    return None
            except Exception as e:  # noqa
                logger.warning("[TTS:voicevox] 拉起失败: %s", e)
                return None
        return self._synth_one(text)

    def synthesize_batch(self, segments: list) -> list:
        """segments: list of (lang, text)。VOICEVOX 只处理日文，但其它语言也尽力合成。

        返回与 segments 等长的 wav 路径列表（失败项为 None）。
        """
        results: list = []
        with self._lock:
            for _lang, text in segments:
                results.append(self._synth_one(text))
        return results

    def empty_cache(self) -> None:
        # VOICEVOX 是外部服务，本进程无显存可释放
        pass
