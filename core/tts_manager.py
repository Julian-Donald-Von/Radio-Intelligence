"""
TTS 分发器（管理器）：运行时选择引擎、切换音色、失败降级、显存管理、热重载。

对外只暴露与原 TTSEngine 完全一致的接口：
  - synthesize(text) -> 音频路径或 None（同步，供 radio 直接调用）
  - available         -> 是否有任一引擎可用
  - reconfigure()     -> 运行时读取 config.tts（engine / voice_id），无需重启
  - empty_cache()     -> 引擎切换时释放 GPU 显存（torch.cuda.empty_cache）

引擎优先级（按配置 tts.engine）：edge(云兜底) / chattts(本地·情绪) / melotts(本地·多语·GPU)
主引擎不可用时按「其余可用引擎」自动回退；全部不可用则静音（返回 None，不崩）。

为契合「asyncio 处理预加载与 TTS 合成避免阻塞主播放线程」，另提供：
  - synthesize_async(text) : 在线程池跑同步合成，可被 await
  - preload()              : 预热当前引擎（后台加载模型/会话）
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional

from config.settings import (
    TTS_TMP_DIR, TTS_ENGINE, TTS_SEGMENT_SILENCE_MS, TTS_SEGMENT_SR, user_config)
from core.tts_base import TTSEngine
from core.voice_manager import VoiceManager
from core.plugin import discover_plugins, PLUGIN_DIR

logger = logging.getLogger("MusicAI.TTS")


class TTSManager(TTSEngine):
    name = "manager"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engines: dict[str, TTSEngine] = {}
        self._voice_mgr = VoiceManager()
        self._engine_name = "edge"
        self._voice_id = ""
        self._prev_engine: Optional[str] = None
        # 不调用基类 __init__（基类会要求 _check/reconfigure 抽象实现），
        # 这里直接进入 manager 的 reconfigure。
        self.reconfigure()

    # 基类要求：manager 作为分发器始终「可用」
    def _check(self) -> bool:  # noqa: D401
        return True

    # ---------------- 引擎注册表（内置 + 插件） ----------------
    _registry_built = False
    _loaders: dict = {}

    @classmethod
    def _build_registry(cls) -> None:
        """合并内置引擎（懒加载）与外部 plugins/tts/ 插件，构建 name→加载器 表。"""
        if cls._registry_built:
            return
        loaders: dict = {}

        def _lazy(mod_path: str, cls_name: str):
            def _load() -> Optional[TTSEngine]:
                try:
                    m = __import__(mod_path, fromlist=[cls_name])
                    return getattr(m, cls_name)()
                except Exception as ex:  # noqa
                    logger.error("[TTS] 引擎 %s 加载失败: %s", cls_name, ex)
                    return None
            return _load

        # 内置引擎：保持懒加载，避免启动时即拉起重依赖（torch/CUDA 等）
        # ★ 2026-11-17：TTS 统一到 IndexTTS 2.5（多语言单引擎：ZH/EN/JA）。
        #   旧引擎 chattts / melotts / voicevox **不再注册**——文件仍保留在 core/ 下，
        #   需要回退时把对应两行放回来即可（配置里 engine 改回名字即可生效）。
        # ★★ 2026-11-18 用户最终拍板：项目**只保留 Qwen3-TTS 一个引擎**。
        #   edge 云端兜底也丢弃（原保留作兜底），index/edge/old 全部不注册。
        #   引擎文件保留仅作历史快照，不加载。TTS 不可用时静音，不回退任何引擎。
        # ★ 2026-11-18 用户拍板「彻底不用 IndexTTS」：不再注册 indextts loader。
        #   任何代码路径调 _get_engine("indextts") 都得到 None，绝不可能拉起
        #   tools/indextts/worker.py（那个 .envs/indextts-v2 黑窗口）。
        #   core/tts_indextts.py 文件保留仅作历史快照，不被加载。
        # Qwen3-TTS 1.7B Base Q4（llama-tts.exe 按需合成）。
        # 9 个可选音色（原 Alba Mackenna / Ono Anna + 试听后保留 7 个），每个都覆盖中/英/日全部语种。
        loaders["qwen3tts"] = _lazy("core.tts_qwen3tts", "Qwen3TTSEngine")

        # 外部插件：plugins/tts/ 下的 TTSEngine 子类，按 name（或 plugin_key）注册。
        # 内置 key 优先，外部插件不覆盖核心引擎。
        try:
            discovered = discover_plugins(PLUGIN_DIR / "tts", (TTSEngine,), recursive=False)
            for eng_cls in discovered.values():
                nm = getattr(eng_cls, "name", None) or getattr(eng_cls, "plugin_key", None)
                if not nm:
                    continue
                if nm in loaders:
                    logger.warning("[TTS] 插件引擎 '%s' 与内置冲突，忽略", nm)
                    continue
                loaders[nm] = (lambda c=eng_cls: c())
        except Exception as e:  # noqa
            logger.warning("[TTS] 插件引擎发现失败：%s", e)

        cls._loaders = loaders
        cls._registry_built = True

    @classmethod
    def reload_engines(cls) -> None:
        """重新扫描插件目录，热重载引擎注册表（开发期用）。"""
        cls._registry_built = False
        cls._loaders = {}
        cls._build_registry()

    def _registry_keys(self) -> list:
        if not self._registry_built:
            self._build_registry()
        return list(self._loaders.keys())

    # ---------------- 引擎惰性加载 ----------------
    def _get_engine(self, name: str) -> Optional[TTSEngine]:
        if not self._registry_built:
            self._build_registry()
        with self._lock:
            if name in self._engines:
                return self._engines[name]
        loader = self._loaders.get(name)
        if loader is None:
            return None
        try:
            e = loader()
        except Exception as ex:  # noqa
            logger.error("引擎 %s 加载失败: %s", name, ex)
            return None
        with self._lock:
            if e is not None:
                self._engines[name] = e
        return e

    # ---------------- 配置 / 切换 ----------------
    def reconfigure(self) -> None:
        cfg = user_config.tts
        new_engine = (cfg.get("engine") or "edge").lower()
        new_voice = cfg.get("voice_id", "") or ""
        # 刷新音色注册表（支持设置页「拖拽导入音色」后无需重启即生效）
        try:
            self._voice_mgr.scan()
        except Exception:  # noqa
            pass
        with self._lock:
            # 引擎切换：离开 GPU 引擎时释放其显存
            # （indextts 是独立 worker 进程，empty_cache() 会让它卸载模型但保留进程，
            #   下次切换回来只重载权重、不必重新 import torch）
            if self._prev_engine and self._prev_engine != new_engine:
                old = self._engines.get(self._prev_engine)
                if old is not None and old.name in ("chattts", "melotts", "indextts"):
                    try:
                        old.empty_cache()
                        logger.info("[TTS] 切换引擎 %s→%s，已释放旧引擎显存",
                                    self._prev_engine, new_engine)
                    except Exception:  # noqa
                        pass
            self._engine_name = new_engine
            self._voice_id = new_voice
            self._prev_engine = new_engine

        # 让当前引擎重新读取最新配置（音色/语速/模型目录等），确保运行时切换生效
        try:
            eng = self._get_engine(self._engine_name)
            if eng is not None:
                eng.reconfigure()
        except Exception:  # noqa
            pass

        # 应用音色（若指定）
        if new_voice:
            self._apply_voice(new_voice)
        logger.info("[TTS] 当前引擎=%s 音色=%s", self._engine_name,
                    self._voice_id or "(默认)")

    def _apply_voice(self, voice_id: str) -> None:
        engine, file_path, entry = self._voice_mgr.resolve(voice_id)
        if not engine:
            logger.warning("[TTS] 音色 %s 未注册，忽略", voice_id)
            return
        # 音色所属引擎优先于配置 engine
        with self._lock:
            if engine != self._engine_name:
                # 切换主引擎以匹配音色
                if self._prev_engine and self._prev_engine != engine:
                    old = self._engines.get(self._prev_engine)
                    if old and old.name in ("chattts", "melotts"):
                        old.empty_cache()
                self._engine_name = engine
                self._prev_engine = engine
        eng = self._get_engine(engine)
        if eng is None:
            return
        if engine == "chattts" and entry:
            emb = self._voice_mgr.load_embedding(entry)
            if emb is not None and hasattr(eng, "set_speaker"):
                eng.set_speaker(emb)
        logger.info("[TTS] 已切换音色 %s → 引擎 %s", voice_id, engine)

    def reload_voices(self) -> None:
        self._voice_mgr.scan()

    # ---------------- 合成 ----------------
    @property
    def available(self) -> bool:
        e = self._get_engine(self._engine_name)
        if e and e.available:
            return True
        for name in self._registry_keys():
            if name == self._engine_name:
                continue
            eng = self._get_engine(name)
            if eng and eng.available:
                return True
        return False

    @staticmethod
    def _synth_with_lang(eng, text: str, lang: str | None):
        """按片段语言选音色合成（带 lang 的引擎优先；不支持 lang 的引擎忽略之）。

        口播文案按 <lang>原文</lang> 分片后，非中文/日语片段需要按语种选音色
        （如 <ko> 走 MeloTTS-Korean、<de> 走 Edge de-DE）。Edge 引擎 synthesize
        接受 lang 参数；MeloTTS/ChatTTS 等不接受时回退到无 lang 调用。
        """
        if lang:
            try:
                return eng.synthesize(text, lang=lang)
            except TypeError:
                return eng.synthesize(text)
        return eng.synthesize(text)

    def _schedule_engine_unload(self) -> None:
        """统一出口：任何一次合成之后都排定「静置卸载」。

        为什么放在 manager 而不是引擎：App 里合成会走 `synthesize` /
        `synthesize_unique_tagged` / `synthesize_tagged` 多条路径，引擎自己的
        `synthesize()` 不一定被调用（实测 App 内「已排定」0 次、隔离测试 1 次），
        所以必须在这里统一排定，否则 worker 加载后无人卸载、与 LLM 长期同驻。
        """
        try:
            eng = self._get_engine(self._engine_name)
            fn = getattr(eng, "_schedule_unload", None)
            if fn is not None:
                fn()
        except Exception:  # noqa
            pass

    def synthesize(self, text: str, lang: str | None = None):
        text = (text or "").strip()
        if not text:
            return None
        active = self._get_engine(self._engine_name)
        if active and active.available:
            out = self._synth_with_lang(active, text, lang)
            if out:
                return out
        # ★★ 2026-11-18 用户最终拍板：只留 Qwen3-TTS 一个引擎，
        #   edge 兜底也丢弃 ⇒ 回退链为空。TTS 不可用时静音，不回退任何引擎。
        for name in ():
            if name == self._engine_name:
                continue
            eng = self._get_engine(name)
            if eng and eng.available:
                logger.warning("[TTS] 主引擎 %s 不可用，回退到 %s",
                               self._engine_name, name)
                out = eng.synthesize(text)
                if out:
                    return out
        logger.error("[TTS] 全部引擎不可用，口播静音降级：%s", text[:30])
        return None

    def synthesize_unique(self, text: str, lang: str | None = None):
        """合成音频并返回「独占」临时文件（文件名唯一，不与他人共用内容哈希）。

        背景/防回归：普通 synthesize 按「文本哈希」命名，若两段口播/转场文案恰好
        相同（或含相同关键句），会指向同一个文件；先播完的一方释放文件后，后播的一方
        play_tts 就报 No such file or directory（口播朗读失败）。本方法拷贝出一份
        唯一命名的独占副本并返回，源缓存原件立即释放，从根本上消除跨口播/转场的
        文件名碰撞；并发同文案时加锁保证「先拷后删」的原子性，避免竞态读到已删原件。
        """
        with self._lock:
            # ★ 2026-11-19：透传 lang 给引擎（英文口播用 --tts-lang en），
            #   None 时引擎默认 zh，中文行为完全不变。
            out = self.synthesize(text, lang=lang)
            if not out:
                return None
            src = Path(out)
            if not src.exists() or src.stat().st_size <= 0:
                return out
            dst = TTS_TMP_DIR / f"{uuid.uuid4().hex}{src.suffix or '.wav'}"
            try:
                shutil.copyfile(src, dst)
            except Exception as e:  # noqa
                logger.warning("[TTS] 独占副本失败，回退原路径：%s", e)
                return out
            # 释放共享缓存原件（已被独占副本替代）：不再被同哈希的其它口播误删/堆积。
            try:
                self.release(out)
            except Exception:  # noqa
                pass
            return str(dst)

    # ---------------- 带标签分段合成（协同规则第 3 步） ----------------
    def synthesize_unique_tagged(self, text: str, lang: str | None = None):
        """带语言标签的口播合成（口播/转场用）：按 <lang>原文</lang> 把片段分到对应引擎，
        再拼接成一首「独占」音频（文件名唯一，不与他人共用内容哈希，避免跨口播/转场碰撞）。

        路由规则（修复「日语不调用 VOICEVOX」）：
          - 日文(ja)片段 → VOICEVOX 本地专属引擎（自动拉起 headless 引擎）；
          - 其它语言/未标签(中文叙述)片段 → 「当前生效引擎」(edge/melotts/...)，
            保持默认中文口播行为，不触发 MeloTTS 首载 ~57s 回归；
          - 任一片段合成失败 → 该段回退「当前生效引擎」（含 edge 日文音色），仍失败则该段
            静默跳过；全部失败 → 返回 None（静音降级，与 synthesize 一致）。

        此前口播只用 synthesize_unique(去标签文本) → 只走 active 引擎(默认 edge/melotts)，
        且回退链不含 voicevox，导致日语从未走 VOICEVOX、被 melotts 用中文模型硬读成乱码。
        """
        text = (text or "").strip()
        if not text:
            return None
        from core.tts_segment import split_tagged, concat_wavs_with_silence

        # ★★★ 2026-11-18（用户报「TTS 没生成，播先交给音乐」）：
        #   Qwen3-TTS 是**一个音色覆盖中/英/日全语种**，根本不需要按 <lang> 标签切段。
        #   旧逻辑把带标签文案切成 en/zh/en/zh 4 段 ⇒ 每段各起一个 llama-tts 子进程
        #   （16.7+11.4+13.6+34.4=76s+冷启动）⇒ 撞 90s submit_wait 超时 ⇒ 结果没人要
        #   ⇒ 播 generic。直接整段去标签一次合成：1 次调用 ~15s，且音色更连贯。
        if self._engine_name == "qwen3tts":
            from core.tts_segment import strip_tags as _strip
            # ★ 2026-11-19：透传 lang（英文口播 en），None 时引擎默认 zh 不变
            return self.synthesize_unique(_strip(text), lang=lang)

        segments = split_tagged(text)
        if not segments:
            return None
        # ★ 2026-11-17：**多语言单引擎**（IndexTTS）自己就能出 ja/en，不再把日文片段
        #   推给 VOICEVOX —— 否则每次日文口播都会把 voicevox 引擎整体拉起来（实测
        #   PID 28412 被自动拉起），既多一个常驻进程，也让多引擎音色不统一。
        # ★ 2026-11-18：Qwen3-TTS 同理——两个音色各自覆盖中/英/日全部语种，**不按语种
        #   分工**。必须把它算进多语言引擎，否则日文段会被错误路由到 VOICEVOX，
        #   破坏"选中一个音色、全语种同一个嗓子"的契约（用户明确要求）。
        _multi_lang_engine = (self._engine_name == "qwen3tts")
        # 纯默认语言（无 <lang> 标签）：退化为普通 synthesize_unique，行为完全不变
        # ★ 2026-09-21：**必须送 `strip_tags(text)` 而不是 `text`** —— 旧写法把**带标签的原文**
        #   交给 synthesize_unique，于是模型显式写出的 `<zh>中文</zh>` 会被 TTS 连标签一起读
        #   （"zh中文zh"）。这属于「修了一个派生字段要修每一个写它的地方」那类漏网。
        if all(lang in ("zh", "") for lang, _ in segments):
            from core.tts_segment import strip_tags as _strip
            return self.synthesize_unique(_strip(text))

        slot_paths: list = []
        for lang, seg in segments:
            w = None
            if lang == "ja" and not _multi_lang_engine:
                vox = self._get_engine("voicevox")
                if vox is not None:
                    try:
                        if not vox.available:
                            vox.ensure_running()   # 自动拉起 headless 引擎
                        if vox.available:
                            w = vox.synthesize(seg)
                    except Exception as e:  # noqa
                        logger.warning("[TTS] voicevox 合成失败 lang=ja: %s", e)
                        w = None
                if not w:
                    w = self.synthesize(seg)   # 回退当前生效引擎（含 edge 日文音色）
            else:
                w = self.synthesize(seg, lang=lang)
            if w and Path(w).exists() and Path(w).stat().st_size > 0:
                slot_paths.append(w)
            # 失败片段：静默跳过，不阻塞整首口播
        if not slot_paths:
            logger.error("[TTS] tagged 合成所有片段失败，静音降级：%s", text[:30])
            return None
        # 拼接（统一重采样到目标采样率 / 单声道），片段间加静音
        tmp = TTS_TMP_DIR / f"tagged_{uuid.uuid4().hex}.wav"
        try:
            final = concat_wavs_with_silence(
                slot_paths, tmp,
                silence_ms=int(user_config.tts.get("segment_silence_ms",
                                                    TTS_SEGMENT_SILENCE_MS)),
                target_sr=int(user_config.tts.get("segment_sr", TTS_SEGMENT_SR)))
        except Exception as e:  # noqa
            logger.warning("[TTS] 分段拼接失败: %s", e)
            final = None
        if not final and len(slot_paths) == 1:
            final = slot_paths[0]   # 单片段无需拼接，直接用
        if not final:
            return None
        # 拷出独占副本（与 synthesize_unique 一致：杜绝跨口播/转场文件名碰撞），
        # 并释放共享原件（含拼接临时/各片段缓存），避免堆积。
        src = Path(final)
        dst = TTS_TMP_DIR / f"{uuid.uuid4().hex}{src.suffix or '.wav'}"
        try:
            shutil.copyfile(src, dst)
            try:
                self.release(final)
            except Exception:  # noqa
                pass
            for p in slot_paths:
                try:
                    self.release(p)
                except Exception:  # noqa
                    pass
        except Exception as e:  # noqa
            logger.warning("[TTS] 独占副本失败，回退拼接路径：%s", e)
            return str(final)
        return str(dst)

    def synthesize_tagged(self, text: str, song_id=None):
        """把 LLM 生成的「带标签口播文案」按语言分片、分组并行合成、拼接成一首导听。

        流程：
          LLM 带标签文案 → 正则分片 [(lang, text), ...]
          → 按 TTS_ENGINE 路由表分组（melotts / voicevox）
          → 各组并行批量合成（同语言只加载一次模型，输出多个 WAV）
          → 按原文顺序拼接，片段间加 80ms 静音
          → 缓存为 intro_{song_id}.wav（或按文案哈希缓存）

        任一段合成失败：先回退 Edge 对应语言音色（尽量保真），仍失败则跳过该片段
        （不阻塞整首口播，仅该段静默）。全部失败 → 返回 None（静音降级）。
        """
        text = (text or "").strip()
        if not text:
            return None
        from config.settings import (
            TTS_TMP_DIR, TTS_ENGINE, TTS_SEGMENT_SILENCE_MS, TTS_SEGMENT_SR,
            user_config)
        from core.tts_segment import split_tagged, concat_wavs_with_silence

        if song_id is not None:
            out_path = TTS_TMP_DIR / f"intro_{song_id}.wav"
        else:
            out_path = self._cache_path(text, tag="tagged", ext="wav")
        if out_path.exists() and out_path.stat().st_size > 0:
            return str(out_path)

        segments = split_tagged(text)
        if not segments:
            return None

        # 按 provider 分组
        groups: dict = {}
        for idx, (lang, seg) in enumerate(segments):
            cfg = TTS_ENGINE.get(lang) or TTS_ENGINE.get("zh")
            provider = (cfg or {}).get("provider", "melotts")
            groups.setdefault(provider, []).append((idx, lang, seg))

        slots = [None] * len(segments)
        for provider, items in groups.items():
            eng = self._get_engine(provider)
            if eng is None or not eng.available:
                for (_idx, lang, seg) in items:
                    slots[_idx] = ("__fb__", lang, seg)
                continue
            try:
                outs = eng.synthesize_batch([(lang, seg) for (_i, lang, seg) in items])
            except Exception as e:  # noqa
                logger.error("[TTS] 批量合成异常 provider=%s: %s", provider, e)
                outs = [None] * len(items)
            for (_idx, lang, seg), out in zip(items, outs):
                if out and Path(out).exists() and Path(out).stat().st_size > 0:
                    slots[_idx] = out
                else:
                    slots[_idx] = ("__fb__", lang, seg)

        # 回退：未被任何引擎合成成功的片段 → Edge 对应语言音色（输出 WAV）
        fb = [(i, v[1], v[2]) for i, v in enumerate(slots) if isinstance(v, tuple)]
        if fb:
            edge = self._get_engine("edge")
            if edge is not None and edge.available:
                for (_idx, lang, seg) in fb:
                    try:
                        w = edge.synthesize(seg, lang=lang, as_wav=True)
                    except Exception as e:  # noqa
                        logger.warning("[TTS] 兜底合成失败 lang=%s: %s", lang, e)
                        w = None
                    slots[_idx] = w
            else:
                for (_idx, lang, seg) in fb:
                    slots[_idx] = None

        ordered = [w for w in slots if isinstance(w, str) and w]
        if not ordered:
            logger.error("[TTS] 带标签合成所有片段失败，口播静音降级：%s", text[:30])
            return None
        return concat_wavs_with_silence(
            ordered, out_path,
            silence_ms=int(user_config.tts.get("segment_silence_ms",
                                              TTS_SEGMENT_SILENCE_MS)),
            target_sr=int(user_config.tts.get("segment_sr", TTS_SEGMENT_SR)))

    async def synthesize_async(self, text: str):
        """在线程池执行同步合成，避免阻塞主播放/UI 线程。"""
        return await asyncio.to_thread(self.synthesize, text)

    def preload(self) -> None:
        """预热当前引擎（后台加载模型/会话），降低首次口播延迟。"""
        e = self._get_engine(self._engine_name)
        if e is not None:
            _ = e.available
            logger.info("[TTS] 已预热引擎 %s", self._engine_name)

    def empty_cache(self) -> None:
        for name, eng in self._engines.items():
            if eng.name in ("chattts", "melotts", "indextts"):
                try:
                    eng.empty_cache()
                except Exception:  # noqa
                    pass
        logger.info("[TTS] 已释放全部引擎显存")

    # ---------------- 连接自检（设置页「测试 TTS 连接」用） ----------------
    def diagnose(self) -> dict:
        """探测各 TTS 引擎可用性，返回 {engine: {"ok": bool, "msg": str}}。

        当前生效引擎会被显式标注；其它引擎仅报告是否可用（不影响主流程）。
        同时澄清「engine=edge 是默认配置、正常工作」，并非 melotts 等未连接。
        """
        result: dict = {}
        engines = tuple(self._registry_keys())
        for name in engines:
            eng = self._get_engine(name)
            if eng is None:
                result[name] = {"ok": False, "msg": "引擎加载失败（依赖缺失）"}
            else:
                result[name] = {
                    "ok": bool(eng.available),
                    "msg": "可用" if eng.available else "不可用",
                }
        active = self._engine_name
        result["_active"] = active
        result["_note"] = (
            f"当前生效引擎={active}（默认配置，正常工作，并非其它引擎未连接）。"
            + (" 全部语言片段由 indextts 单引擎合成。" if active == "indextts"
               else (" 日文片段将路由到 voicevox。" if active != "voicevox" else "")))
        return result

    def ensure_voicevox(self, timeout: float = 45.0) -> bool:
        """尝试自动拉起 VOICEVOX headless 引擎（设置页测试 / 首次日文口播时调用）。"""
        eng = self._get_engine("voicevox")
        if eng is None:
            return False
        ok = eng.ensure_running(timeout=timeout)
        return ok

    def ensure_melotts(self, lang: str = "zh") -> bool:
        """预热 MeloTTS 模型（首次会从 HF 镜像下载权重）。

        开应用后台调用：让中文模型在首次口播前就绪，避免现下导致卡顿；
        失败不影响主流程（默认引擎 edge 仍可用，TTSManager 会自动回退）。
        """
        eng = self._get_engine("melotts")
        if eng is None:
            return False
        try:
            return eng.ensure_model(lang) is not None
        except Exception as e:  # noqa
            logger.warning("MeloTTS 预热失败（将回退 edge）: %s", e)
            return False

    def ensure_indextts(self) -> bool:
        """★ 2026-11-18 用户拍板「彻底不用 IndexTTS」：恒 no-op。

        保留方法名只为兼容残留调用方（ai/schedule.py 等）避免 AttributeError；
        绝不拉起 tools/indextts/worker.py（那个 .envs/indextts-v2 黑窗口）。
        """
        return False

    def warm_active(self) -> bool:
        """只预热「当前生效引擎」——避免把用不到的重型引擎也拉起来占显存。

        ★ 2026-11-18：IndexTTS 已彻底退休；Qwen3-TTS 是按需 CLI 进程、
        无 worker/loaded 概念，无需预热。本方法当前不预热任何引擎。
        """
        name = self._engine_name
        # 开了分时批处理时同样不在启动阶段预热 TTS。
        try:
            from ai.tts_batch import batch_enabled
            if batch_enabled() and name in ("indextts", "melotts"):
                logger.info("[TTS] 已启用分时批处理，跳过启动预热（由批次窗口负责）")
                return False
        except Exception:  # noqa
            pass
        if name == "melotts":
            return self.ensure_melotts()
        if name == "voicevox":
            return self.ensure_voicevox()
        logger.info("[TTS] 生效引擎 %s 无需预热", name)
        return False

    # ---------------- 便捷访问 ----------------
    @property
    def voice_manager(self) -> VoiceManager:
        return self._voice_mgr

    @property
    def engine_name(self) -> str:
        return self._engine_name

    def apply_overrides(self, voice=None, rate=None, volume=None) -> None:
        """临时覆盖当前引擎的音色/语速/音量（供设置页试听，不影响 config）。"""
        eng = self._get_engine(self._engine_name)
        if eng is None:
            return
        if voice is not None:
            eng.voice = voice
        if rate is not None:
            eng.rate = rate
        if volume is not None:
            eng.volume = volume
