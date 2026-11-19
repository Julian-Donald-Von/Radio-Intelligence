"""IndexTTS 2.5 引擎（App 侧，Python 3.13）。

架构：**不在本进程 import torch/indextts**。IndexTTS 只能跑在 3.11
（`.envs/indextts-v2`），故真正的推理在常驻 worker 子进程里
（`tools/indextts/worker.py`，本地 HTTP），本类只做：
    · 可用性探测（venv/权重/参考音是否齐）
    · worker 生命周期（按需拉起、健康检查、失败重启、空转卸载）
    · 把 text/lang 转成 HTTP 请求，把 wav 路径交回 TTSManager

对外契约与其它引擎一致：`synthesize(text, lang=None) -> path|None`、
`synthesize_batch([(lang,text),...]) -> [path|None]`、`empty_cache()`、`release()`。

配置（`data_runtime/config.json` 的 `tts` 段，全部可选）：
    indextts_port         worker 端口，默认 8799
    indextts_venv         worker 的 python.exe，默认 <root>/.envs/indextts-v2/Scripts/python.exe
    indextts_model_dir    模型目录，默认 <root>/models/IndexTTS-2.5
    indextts_ref_zh/ja/en 参考音（音色克隆源），默认 data_runtime/f5_voicevox_verification/normal_*.wav
    indextts_timeout      单次合成超时秒，默认 300
    indextts_autostart    是否按需拉起 worker，默认 True
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from config.settings import PROJECT_ROOT, TTS_TMP_DIR
from core.tts_base import TTSEngine

logger = logging.getLogger("MusicAI.TTS")

_LANG_MAP = {"zh": "ZH", "en": "EN", "ja": "JA", "es": "ES", "ar": "AR"}


class IndexTTSEngine(TTSEngine):
    name = "indextts"

    # ---------------- 配置与可用性 ----------------
    def _load_cfg(self) -> None:
        from config.settings import user_config

        t = user_config.tts
        self._port = int(t.get("indextts_port", 8799) or 8799)
        venv = (t.get("indextts_venv") or "").strip()
        self._venv_py = (Path(venv) if venv
                         else PROJECT_ROOT / ".envs/indextts-v2/Scripts/python.exe")
        mdir = (t.get("indextts_model_dir") or "").strip()
        self._model_dir = Path(mdir) if mdir else PROJECT_ROOT / "models/IndexTTS-2.5"
        self._worker_script = PROJECT_ROOT / "tools/indextts/worker.py"
        self._timeout = float(t.get("indextts_timeout", 300) or 300)
        self._autostart = bool(t.get("indextts_autostart", True))
        # ★ 三语统一音色：非空时中/英/日都用这一份参考音（音色克隆源），
        #   留空则按语言各用各的（三份内置参考音同属「四国めたん」，音色本就统一）。
        self._ref_unified = (t.get("indextts_ref") or "").strip()
        # ★ 2026-11-17：合成完**自动卸载模型**（静置若干秒无新请求才卸）。
        #   8GB 卡上 IndexTTS(5.4G) 与 35B(3.5G) 同驻会互相换页，实测 LLM 与 TTS
        #   都掉到半速甚至更慢。改成「用时加载、用完让位」：
        #     · 一段批量合成内的多段间隔很短（< idle）⇒ 不会反复重载；
        #     · 合成结束静置 idle 秒 ⇒ 释放 5.4G，大模型恢复全速（推荐/文案）。
        self._unload_after = bool(t.get("indextts_unload_after_synth", True))
        self._unload_idle_s = float(t.get("indextts_unload_idle_s", 8.0) or 8.0)
        self._unload_timer = None
        self._last_synth_at = 0.0
        # ★ 2026-11-17 task-1：单次合成硬超时（秒，0=沿用 indextts_timeout）。
        #   线程不可强杀，超时后 /shutdown 真杀 worker 进程（释放 5GB），下一条再冷启。
        self._synth_timeout = float(t.get("indextts_synth_timeout_s", 90) or 0)
        # ★ 2026-11-18 常驻模式：合成超时下限 240s。实机 W8A16 + LLM 同卡并发时
        #   单条曾到 148s，旧 90s 会杀 worker ⇒ 重载 62s ⇒ 又超时（恶性循环）。
        #   常驻架构 worker 一直驻留，宁可多等也别杀它。
        self._resident_synth_timeout = 240.0
        if getattr(self, "_inflight_lock", None) is None:
            self._inflight_lock = threading.Lock()
            self._synth_inflight = 0
        # 后处理：与 melotts 共用同一条 ffmpeg 链与同一组 7 个滑块
        self._postprocess_on = bool(t.get("indextts_postprocess", False))
        try:
            from core.tts_postprocess import params_from_config
            self._pp_params = params_from_config(t)
        except Exception:  # noqa
            self._pp_params = None
        self._refs = {
            "ZH": (t.get("indextts_ref_zh") or
                   str(PROJECT_ROOT / "data_runtime/f5_voicevox_verification/normal_zh.wav")),
            "JA": (t.get("indextts_ref_ja") or
                   str(PROJECT_ROOT / "data_runtime/f5_voicevox_verification/normal_ja.wav")),
            "EN": (t.get("indextts_ref_en") or
                   str(PROJECT_ROOT / "data_runtime/f5_voicevox_verification/normal_en.wav")),
        }
        self._base = f"http://127.0.0.1:{self._port}"
        if getattr(self, "_start_lock", None) is None:
            self._start_lock = threading.Lock()

    def _check(self) -> bool:
        self._load_cfg()
        if not self._venv_py.is_file():
            logger.warning("[TTS:indextts] worker python 不存在：%s", self._venv_py)
            return False
        if not (self._model_dir / "config.yaml").is_file():
            logger.warning("[TTS:indextts] 模型目录不完整：%s", self._model_dir)
            return False
        if not self._worker_script.is_file():
            logger.warning("[TTS:indextts] worker 脚本不存在：%s", self._worker_script)
            return False
        return True

    def reconfigure(self) -> None:
        self._load_cfg()

    # ---------------- worker 生命周期 ----------------
    def _req(self, path: str, payload: dict | None = None, timeout: float | None = None):
        url = self._base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout or self._timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _health(self, timeout: float = 2.0) -> dict | None:
        try:
            return self._req("/health", None, timeout=timeout)
        except Exception:
            return None

    def _spawn_worker(self) -> None:
        log = PROJECT_ROOT / "data_runtime/indextts_worker.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        with open(log, "a", encoding="utf-8", errors="ignore") as fh:
            subprocess.Popen(
                [str(self._venv_py), str(self._worker_script), "--port", str(self._port)],
                cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL, stdout=fh,
                stderr=subprocess.STDOUT, creationflags=flags, env=env, close_fds=True)
        logger.info("[TTS:indextts] 已拉起 worker :%d（日志 %s）", self._port, log)

    def ensure_worker(self, wait_s: float = 240.0, load: bool = True) -> bool:
        """确保 worker 在跑且模型已载入；已就绪则幂等返回 True。

        ★ 2026-11-17 task-1：`wait_s` 是**总预算**（等健康检查 + /load 都算在内），
        且 /load 的 HTTP 超时不再用 wait_s+60（旧值可到 300s，是「单曲 370s」的第二条
        300s 级卡死路径）。批量合成路径传入的是 `_synth_timeout`（默认 90s）。
        """
        h = self._health()
        if h and h.get("loaded"):
            return True
        with self._start_lock:
            h = self._health()
            if not (h and h.get("loaded")):
                if not self._autostart:
                    logger.warning("[TTS:indextts] worker 未运行且禁用自启")
                    return False
                t0 = time.time()
                if not h:
                    self._spawn_worker()
                while time.time() - t0 < wait_s:
                    h = self._health()
                    if h:
                        break
                    # 睡到预算边界而不是固定 2s，避免小预算被 sleep 撑破
                    time.sleep(max(0.2, min(2.0, wait_s - (time.time() - t0))))
                if not h:
                    logger.error("[TTS:indextts] worker %.0fs 内未就绪（总预算）", wait_s)
                    return False
                if load and not h.get("loaded"):
                    # 剩余预算给 /load；最少 30s、最多 120s，绝不再回到 300s。
                    _left = max(30.0, min(float(wait_s) - (time.time() - t0), 120.0))
                    try:
                        self._req("/load", {}, timeout=_left)
                    except Exception as e:  # noqa
                        logger.error("[TTS:indextts] /load 失败（超时 %.0fs）：%s", _left, e)
                        return False
                h = self._health() or {}
                logger.info("[TTS:indextts] worker ready loaded=%s gpu=%sMiB（总耗时 %.1fs）",
                            h.get("loaded"), h.get("gpu_mib"), time.time() - t0)
            return bool((self._health() or {}).get("loaded"))

    def warm(self) -> None:
        """预热（不合成）：拉起 worker 并载入模型。"""
        self.ensure_worker()

    # ---------------- 合成 ----------------
    def _synth_begin(self) -> None:
        """一条合成开始：撤销已排定的静置卸载，并登记在途。

        ★ 2026-11-17 task-1：旧实现只在**合成成功后**重排卸载定时器，于是「上一次
        成功」排下的 8s 定时器会在**本次（>8s）合成进行中**到期 → /unload 阻塞在
        worker 推理锁上 → 合成一结束就立刻卸载 → 下一条拿到 None ⇒ AttributeError。
        """
        try:
            if self._unload_timer is not None:
                self._unload_timer.cancel()
                self._unload_timer = None
        except Exception:  # noqa
            pass
        self._last_synth_at = time.time()
        with self._inflight_lock:
            self._synth_inflight += 1

    def _synth_end(self) -> None:
        with self._inflight_lock:
            self._synth_inflight = max(0, self._synth_inflight - 1)

    @staticmethod
    def _is_timeout(e: Exception) -> bool:
        if isinstance(e, (TimeoutError, socket.timeout)):
            return True
        return "timed out" in str(e).lower() or "timeout" in type(e).__name__.lower()

    def synthesize(self, text: str, lang: str | None = None):
        text = (text or "").strip()
        if not text:
            return None
        text = self._truncate(text)
        code = _LANG_MAP.get((lang or "zh").lower(), "ZH")
        out = self._cache_path(text, tag=code, ext="wav")
        if out.exists() and out.stat().st_size > 0:
            return str(out)              # 同文同语言命中缓存（manager 负责独占副本）
        self._synth_begin()
        try:
            return self._synthesize_now(text, code, out)
        finally:
            self._synth_end()

    def _effective_synth_timeout(self) -> float:
        """常驻模式下合成超时下限 240s，避免并发峰值(实测148s)误杀 worker。"""
        base = float(self._synth_timeout or self._timeout or 90.0)
        if os.getenv("MUSICAI_CORESIDENT", "1") != "0":
            return max(base, float(getattr(self, "_resident_synth_timeout", 240.0)))
        return base

    def _synthesize_now(self, text: str, code: str, out) -> str | None:
        # ★ 2026-11-17 task-1：worker 冷启/加载也必须受合成预算约束（默认 90s），
        #   否则「健康等待 240s + /load 300s」仍能把单条拖到分钟级。
        # ★ 2026-11-18：常量走 _effective_synth_timeout（常驻=240s），不再硬编码 90。
        _budget = min(float(self._effective_synth_timeout()), 300.0)
        if not self.ensure_worker(wait_s=_budget):
            logger.error("[TTS:indextts] worker 未就绪（预算 %.0fs），本条跳过", _budget)
            return None
        payload = {"text": text, "lang": code, "out": str(out),
                   "ref": self._ref_unified or self._refs.get(code) or self._refs["ZH"]}
        _timeout = self._effective_synth_timeout()
        try:
            r = self._req("/synth", payload, timeout=_timeout)
        except Exception as e:  # noqa
            if self._is_timeout(e):
                # ★ 线程不可强杀：合成线程还卡在 worker 推理锁里，只能把进程杀掉
                #   （/shutdown 由 ThreadingHTTPServer 的独立线程处理，不占推理锁）。
                logger.error("[TTS:indextts] 单次合成超时（>%.0fs）→ 终止 worker 进程", _timeout)
                self.shutdown_worker()
            else:
                logger.error("[TTS:indextts] 合成请求失败：%s", e)
            return None
        if not r.get("ok"):
            logger.error("[TTS:indextts] 合成失败：%s", r.get("error"))
            return None
        wav = r.get("wav")
        if not wav or not Path(wav).exists() or Path(wav).stat().st_size <= 0:
            return None
        logger.info("[TTS:indextts] %.2fs 音频=%.2fs sr=%s lang=%s chars=%d",
                    r.get("elapsed_s") or 0, r.get("audio_s") or 0,
                    r.get("sr"), code, len(text))
        if self._postprocess_on:
            try:
                from core.tts_postprocess import apply as _apply
                if _apply(wav, self._pp_params):
                    logger.info("[TTS:indextts] 后处理完成")
            except Exception as e:  # noqa
                logger.warning("[TTS:indextts] 后处理失败（保留原始合成）：%s", e)
        self._schedule_unload()
        return str(wav)

    # ---------------- 用完让位：静置后自动卸载 ----------------
    def _schedule_unload(self) -> None:
        """合成成功后重置「静置卸载」定时器（多段连续合成只会在最后卸一次）。"""
        if not self._unload_after:
            logger.info("[TTS:indextts] 静置卸载已关闭（配置）")
            return
        self._last_synth_at = time.time()
        try:
            if self._unload_timer is not None:
                self._unload_timer.cancel()
            t = threading.Timer(self._unload_idle_s, self._maybe_unload)
            t.daemon = True
            t.start()
            self._unload_timer = t
            logger.info("[TTS:indextts] 已排定 %.0fs 后卸载模型", self._unload_idle_s)
        except Exception as e:  # noqa
            logger.warning("[TTS:indextts] 排定卸载失败：%s", e)

    def _maybe_unload(self) -> None:
        """静置超时且确实没新请求时，把 worker 里的模型卸掉，给大模型让显存。

        ★ 2026-11-17 task-1 双闸：
          · 有合成在途（_synth_inflight>0）⇒ 不卸；
          · 正处于 TTS 换模型窗口（llm_paused()）⇒ 不卸（窗口里 TTS 是独占方，
            卸了会让批内后续条目全部失败）。
        卸载请求仍带 idle_s，由 worker 侧最后再判一次（防阻塞式 /unload 在合成
        结束瞬间落地）。
        """
        try:
            with self._inflight_lock:
                if self._synth_inflight > 0:
                    return
            try:
                from ai.tts_batch import llm_paused
                if llm_paused():
                    logger.info("[TTS:indextts] 换模型窗口内，跳过静置卸载")
                    return
            except Exception:  # noqa
                pass
            if time.time() - self._last_synth_at < self._unload_idle_s - 0.5:
                return
            r = self._req("/unload", {"idle_s": self._unload_idle_s}, timeout=10)
            if isinstance(r, dict) and r.get("ok") is False and (r.get("busy")
                                                                 or r.get("too_soon_s")):
                logger.info("[TTS:indextts] 静置卸载被拒（busy=%s too_soon=%ss），2.5s 后重试",
                            r.get("busy"), r.get("too_soon_s"))
                t = threading.Timer(2.5, self._maybe_unload)
                t.daemon = True
                t.start()
                return
            logger.info("[TTS:indextts] 静置 %.0fs，已卸载模型（释放显存给大模型）",
                        self._unload_idle_s)
        except Exception as e:  # noqa
            logger.info("[TTS:indextts] 静置卸载跳过：%s", e)

    def synthesize_batch(self, segments: list) -> list:
        """segments = [(lang, text), ...]；worker 内部串行，逐条合成。"""
        out = [self.synthesize(text, lang=lang) for lang, text in segments]
        # ★ 2026-11-17：批量路径也排定卸载（App 的 tagged 合成会走这里，
        #   不一定经过单条 synthesize，之前就是因为漏了这条才长期同驻）。
        self._schedule_unload()
        return out

    # ---------------- 资源 ----------------
    def empty_cache(self) -> None:
        """释放 worker 里的模型显存（进程保留，避免下次重新 import torch）。

        force=True：这是批处理在「起大模型前」的**计划内让位**，必须绕过静置守卫。
        """
        try:
            self._req("/unload", {"force": True}, timeout=10)
            logger.info("[TTS:indextts] 已请求 worker 卸载模型")
        except Exception:  # noqa
            pass

    def shutdown_worker(self) -> None:
        try:
            self._req("/shutdown", {}, timeout=10)
        except Exception:  # noqa
            pass

    def diagnose(self) -> dict:
        h = self._health()
        return {"name": self.name, "available": bool(self.available),
                "port": getattr(self, "_port", None),
                "venv_python": str(getattr(self, "_venv_py", "")),
                "model_dir": str(getattr(self, "_model_dir", "")),
                "worker": h or {"reachable": False}}
