"""
AI 大模型客户端：封装与「模型后端」的连接与调用（"AI 大模型"层）。

设计原则——与 ai/prompts.py（"AI 提示词"层）彻底分离：
  - 本文件只负责"怎么调模型"（构建 client、单次 chat、连接诊断、错误归类）；
  - 不关心发什么内容；提示词全部集中在 ai/prompts.py，由 ai/brain.py 组装后传入。

支持两种后端，运行时通过 config.json 的 ai.backend 切换，无需重启：
  - api    : OpenAI 兼容远程端点（混元 / Gemini / DeepSeek / GPT / Kimi 等），
              使用官方 openai SDK，支持内置 $web_search 工具与思考模式。
  - ollama : 本机 Ollama 本地小模型（统一用 qwen3.5:4b，深度/快速同款），
             无需 API Key，数据不出本机；联网搜索由独立 SearchProvider 提供。

三层口播架构对应：模板兜底（无 LLM）→ 快速 LLM（3B）→ 深度 LLM（7B/API）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from config.settings import user_config
from utils.logger import redact
from utils.network_security import validate_api_base

logger = logging.getLogger("MusicAI.Model")


def normalize_llm_api_base(value: str) -> str:
    """把用户填成完整 endpoint 的地址归一成 OpenAI 兼容的 base URL。

    常见误填：把 ``https://host/v1/chat/completions`` 整条贴进"API 地址"，
    openai 客户端会在其后**再拼一次** ``/chat/completions`` ⇒ 请求打到
    ``/v1/chat/completions/chat/completions``（404 / 假成功）。
    只剥掉结尾的 ``/chat/completions``（可带斜杠），不动其它路径。
    """
    base = (value or "").strip().rstrip("/")
    suffix = "/chat/completions"
    if base.lower().endswith(suffix):
        base = base[:-len(suffix)].rstrip("/")
    return base

# 全局 Ollama 串行锁：本机 Ollama 同一时刻只能高效跑一个请求，并发会让请求排队，
# 导致单轮延迟从 ~8s 飙到 60~97s 并触发 45s 超时失败。所有 Ollama 调用（广播/转场/
# 品味/推荐）统一走这把锁串行执行，既消除排队爆炸，又让 keep_alive 的常驻显存生效。
_ollama_lock = threading.Lock()

# ===========================================================================
# R119-3f · Ollama runner「内存回收」—— 不牺牲口播速度的那种
# ---------------------------------------------------------------------------
# 真机事实（详见 `docs/r119_stall_root_causes.md` ②，2026-09-19）
#   · `llama-server` 常驻后 私有提交涨到 **13.8 GB**，而模型本体只占**显存 2.91 GB**；
#   · 卸载后 → **128 MB**；重载后稳定 **5.68 GB**（**冷加载只要 5.4 s**）；
#   · ⇒ 一次「卸载 + 重载」可回收 **~8 GB**，而模型**保持热**（下次口播**不用等加载**）。
#   · 「每请求累积」**已被证伪**（30 次串行请求 +227MB 后完全平台化）；并发也排除
#     （`1/1 seqs`、`n_parallel=1`，且本文件这把 `_ollama_lock` 已把调用串行化）
#     ⇒ **机制未定论** ⇒ 不猜，只做**有界回收**。
# 纪律：**只在「稳定空闲 + 提交吃紧」时做**（重载是 5.4s 重活，播放中做必听得出）。
# ===========================================================================
RUNNER_RECYCLE_MARK = "R119-3f"
# ★ R119-3l：各家「思考结束」标记表（`_strip_think` 用），**以实测为准**。
#   不同家族写法不同，漏一个的后果是二选一：
#     · 把整段思考当正文 ⇒ TTS 念英文思维链；
#     · 或把带思考的响应判成"空正文" ⇒ 口播全退化成离线文案。
#   已确认：Granite 4.2 = `</think>`（实测原文 "…as the answer.\n</think>\n今晚深夜…"）。
#   其余为常见变体；拉完候选模型后逐个 `think=True` 实测，缺的再补（见日志 ㉘）。
#   ⚠ 用**拼接**写：这些标签在部分工具链里会被吞（本项目已踩过）。
_THINK_CLOSE_TAGS = (
    "<" + "/" + "think" + ">",        # Granite 4.2（实测）
    "<" + "/" + "thinking" + ">",
    "<" + "/" + "thought" + ">",
)



# ★ R119-4v：`num_ctx` 的**唯一来源**。此前三处各写各的（_chat_ollama 8192 /
#   recycle_ollama_runner 4096 / chat_with_tools 4096），改生产值时漏改另两处 ⇒
#   · 回收用 4096 重载、下一次口播却要 8192 ⇒ Ollama 判定装载参数不一致 ⇒ **再冷加载一次**，
#     回收等于白做，而那次冷加载正好砸在口播上；
#   · 工具调用把 runner 拉到 4096，下一轮口播又拉回 8192 ⇒ **乒乓重载**。
#   收敠到一处后，任何调用方都不可能再漂移。可用 MUSICAI_OLLAMA_NUM_CTX 覆盖。
DEFAULT_NUM_CTX = 8192


def ollama_num_ctx() -> int:
    """Ollama 上下文窗口的**唯一取值点**（生产/回收/工具调用三处共用）。"""
    try:
        return int(os.environ.get("MUSICAI_OLLAMA_NUM_CTX") or DEFAULT_NUM_CTX)
    except (TypeError, ValueError):
        return DEFAULT_NUM_CTX


def should_recycle_runner(*, playing: bool, idle_ticks: int, need_idle_ticks: int,
                          commit_avail_gb: float, floor_gb: float,
                          since_last_s: float, cooldown_s: float) -> bool:
    """**纯判据**（不碰 IO ⇒ 可单测、可"见红"）：现在能否安全回收一次 runner。

    五个必要条件，缺一不可：
      ① `not playing` —— 重载是 5.4s 重活，播放中做必然卡；
      ② `idle_ticks >= need_idle_ticks` —— 已连续空闲若干个节拍，避开换曲过渡窗；
      ③ `commit_avail_gb >= 0` —— 该事实**读得到**；读不到内存就**绝不动手**；
      ④ `commit_avail_gb < floor_gb` —— 内存宽裕时不折腾；
      ⑤ `since_last_s >= cooldown_s` —— 冷却，防抖动式反复重载。
    """
    return (not playing
            and idle_ticks >= need_idle_ticks
            and commit_avail_gb >= 0.0
            and commit_avail_gb < floor_gb
            and since_last_s >= cooldown_s)


def _ollama_post(host: str, payload: dict, timeout: float) -> dict:
    """向本机 Ollama 发一个 JSON POST。

    刻意**不用 requests**：少一层依赖，且离线可用。**显式禁用代理**——本机
    `http_proxy` 会拦 `127.0.0.1`（B109 踩过的坑）。
    """
    import json as _json
    import urllib.request as _ur

    req = _ur.Request(host + "/api/generate",
                      data=_json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    op = _ur.build_opener(_ur.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        return _json.loads(r.read() or b"{}")


def recycle_ollama_runner(host: str | None = None, model: str | None = None) -> dict:
    """卸载再重载 Ollama runner 以回收宿主内存；**模型保持热**（不牺牲口播速度）。

    ★ 全程持有 `_ollama_lock`：绝不与真实口播请求交错——否则可能打断一次正在生成
      的请求，或让 Ollama 加载出第二份 runner。
    ★ 重载用**与生产完全相同的装载参数**（`num_ctx` 由 `ollama_num_ctx()` 唯一提供 / `num_gpu=-1`）：这两个是
      「装载参数」，不一致会导致下一次请求**再重载一次**，等于白做。
    返回 `{"ok": bool, ...}`；**任何失败都不抛**（回收是优化，绝不能影响主流程）。
    """
    try:
        a = user_config.ai
        if (a.get("backend") or "api").lower() != "ollama":
            return {"ok": False, "reason": "backend != ollama"}
        host = (host or a.get("ollama_host") or "http://127.0.0.1:11434").rstrip("/")
        model = model or a.get("ollama_deep") or "qwen3.6:35b-a3b"
        from core.sysmem import commit_avail_gb as _cab
        before = _cab()
        t0 = time.perf_counter()
        with _ollama_lock:
            # ① 卸载：keep_alive=0 ⇒ 立即释放 runner（实测 /api/ps 随即归零）
            _ollama_post(host, {"model": model, "keep_alive": 0}, timeout=60)
            time.sleep(0.4)          # 给 Ollama 回收子进程的时间
            # ② 重载：与生产同参（见上方 ★）
            _ollama_post(host, {"model": model, "prompt": "1", "stream": False,
                                "keep_alive": -1, "think": False,
                                "options": {"num_ctx": ollama_num_ctx(), "num_gpu": -1,
                                            "num_predict": 1}},
                         timeout=300)
        dt = time.perf_counter() - t0
        after = _cab()
        logger.info("Ollama runner 已回收：用时 %.1fs，提交可用 %.2fG → %.2fG",
                    dt, before, after)
        return {"ok": True, "seconds": round(dt, 2), "model": model,
                "commit_before_gb": before, "commit_after_gb": after}
    except Exception as e:  # noqa  回收失败绝不影响播放/口播
        logger.warning("Ollama runner 回收失败（忽略）：%s: %s", type(e).__name__, e)
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}



class LLMClient:
    """统一的模型客户端，支持 api / ollama 两种后端，以及快速/深度模型切换。"""

    def __init__(self, fast: bool = False) -> None:
        # fast=True 时（仅 ollama 后端生效）使用 3B 快速模型跑短口播/转场
        self._fast = fast
        self._client = None
        self.api_base = ""
        self.api_key = ""
        self.model = "gpt-4o-mini"
        self.temperature = 0.8
        self.skip_temperature = False
        self.web_search = False
        self.thinking = False
        self.enabled = False
        self.last_error = ""
        self.last_error_kind = ""
        self._content_filter_until = 0.0
        self._safe_mode = False
        # 并发上限：所有出网 LLM 调用（推荐/品味/转场/口播，含联网搜索多轮）
        # 共享这一个信号量，避免「很多歌同时联网搜索」时瞬间打爆共享的 OpenAI
        # 客户端连接池、触发 Kimi 限流，进而把后台线程与 UI 一起拖死。
        # 默认允许 3 路并发：既保留并行预取的速度，又不至于把客户端压垮。
        self._llm_sem = threading.Semaphore(3)
        self.reconfigure()

    # ---------------- 配置 ----------------
    def reconfigure(self, backend: str | None = None,
                    model: str | None = None) -> None:
        """从用户配置读取 AI 参数并（重）构建 LLM 客户端。

        backend / model 为可选覆盖（用于运行时热重载或显式指定快速模型）；
        省略时回退到 config.json 的 ai.*。
        """
        a = user_config.ai
        self.backend = (backend or a.get("backend") or "api").lower()
        self.ollama_host = a.get("ollama_host") or "http://127.0.0.1:11434"
        self.ollama_deep = a.get("ollama_deep") or "qwen3.6:35b-a3b"
        self.ollama_fast = a.get("ollama_fast") or "qwen3.6:35b-a3b"
        self.ollama_fast_enabled = bool(a.get("ollama_fast_enabled", True))
        # ★ 2026-11-16：llamacpp 直连后端（脱离 Ollama 的受控思考路径）
        self.llamacpp_host = (a.get("llamacpp_host") or "http://127.0.0.1:8080").rstrip("/")
        try:
            self.llamacpp_budget = int(a.get("llamacpp_reasoning_budget", 512))
        except (TypeError, ValueError):
            self.llamacpp_budget = 512
        try:
            self.llamacpp_num_ctx = int(a.get("llamacpp_num_ctx", 8192))
        except (TypeError, ValueError):
            self.llamacpp_num_ctx = 8192

        self.last_error = ""
        self.last_error_kind = ""
        self._client = None

        if self.backend == "ollama":
            # 本地后端：无需 key；默认深度模型，快速模式走 3B
            if self._fast and self.ollama_fast_enabled:
                self.model = model or self.ollama_fast
            else:
                self.model = model or self.ollama_deep
            self.enabled = True
            return

        if self.backend == "llamacpp":
            # ★ 2026-11-16：直连 llama.cpp server，不需要 API key。
            # `model` 字段在此后端仅作**标识**（服务端加载哪个 GGUF 由它自己决定），
            # 请求体不传 model —— /v1/chat/completions 对单实例服务可省略。
            self.model = model or a.get("llamacpp_model") or "local-llamacpp"
            self.enabled = bool(self.llamacpp_host)
            if not self.enabled:
                self.last_error = "llamacpp_host 未配置"
            return

        # ---- API 后端（OpenAI 兼容） ----
        self.api_key = a.get("api_key", "") or ""
        self.model = model or a.get("model", "gpt-4o-mini") or "gpt-4o-mini"
        self.temperature = float(a.get("temperature", 0.8) or 0.8)
        self.skip_temperature = bool(a.get("skip_temperature", False))
        self.web_search = bool(a.get("web_search", False))
        self.thinking = bool(a.get("thinking", False))
        try:
            self.api_base = validate_api_base(normalize_llm_api_base(a.get("api_base", "")))
        except ValueError as e:
            self.api_base = ""
            self.last_error = str(e)
        self.enabled = bool(self.api_base and self.api_key)
        if self.enabled:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    base_url=self.api_base, api_key=self.api_key, timeout=30,
                )
            except Exception as e:  # noqa
                logger.warning("LLM 客户端初始化失败，转离线模式: %s", redact(e))
                self.last_error = f"客户端初始化失败: {redact(e)}"
                self.enabled = False

    @property
    def backend_name(self) -> str:
        return self.backend

    # ---------------- 单次调用 ----------------
    def _wait_llm_window(self, max_wait: float | None = None) -> bool:
        """若正处于「TTS 换模型窗口」（大模型被停），阻塞等待窗口结束。

        返回 True=可以发请求；False=**仍在窗口**（已等满上限），调用方必须放弃本次
        调用、让上层走离线模板 —— 绝不能把请求发到还没起来的服务上（实测会 10061，
        并且日志会误报「窗口结束」）。

        只在分时批处理启用时可能发生；未启用时 `llm_paused()` 恒为 False，开销是一次函数调用。
        """
        if max_wait is None:
            try:
                max_wait = max(1.0, float(os.environ.get("MUSICAI_LLM_WAIT_S") or 180.0))
            except (TypeError, ValueError):
                max_wait = 180.0
        try:
            from ai.tts_batch import llm_paused
        except Exception:
            return True
        if not llm_paused():
            return True
        t0 = time.monotonic()
        logger.info("[LLM] 正处于 TTS 换模型窗口，调用排队等待（最多 %.0fs）", max_wait)
        while llm_paused() and (time.monotonic() - t0) < max_wait:
            time.sleep(0.4)
        waited = time.monotonic() - t0
        if llm_paused():
            # ★ 实测窗口可达 327~382s > 旧上限 120s：超时后继续发请求只会撞 10061，
            #   还会被日志误读成「窗口结束」。这里明确放弃，交给上层模板兜底。
            logger.warning("[LLM] 仍在 TTS 换模型窗口（已等 %.1fs），放弃本次调用", waited)
            self.last_error_kind = "tts_window"
            self.last_error = "TTS 换模型窗口未结束，本次 LLM 调用放弃"
            return False
        if waited > 0.5:
            logger.info("[LLM] 等待 %.1fs 后窗口结束，继续调用", waited)
        return True

    def chat(self, system: str, user: str, expect_json: bool = False,
             temperature: float | None = None, use_temp: bool = True,
             web_search: bool | None = None,
             thinking: bool | None = None,
             json_temperature: float | None = None,
             budget: int | None = None) -> str:
        """统一入口：按后端分发到 _chat_api / _chat_ollama。

        返回模型正文；任何失败（不可达/超时/内容审核）均返回空字符串，
        由上层（brain）降级到模板口播。
        """
        if not self.enabled:
            return ""
        # ★ 2026-11-17：TTS 分时批处理会短暂停掉 llamacpp 服务（约 20~60s，为了把显存
        #   让给 IndexTTS）。这段时间里任何 LLM 调用都会 Errno 10061 连接被拒 ⇒ 上层
        #   误报「AI 调用失败」并走兜底。这里统一**排队等待**窗口结束，而不是直接失败。
        if not self._wait_llm_window():
            return ""
        if time.monotonic() < self._content_filter_until:
            self.last_error_kind = "content_filter"
            self.last_error = "模型内容审核冷却中，请稍后再试"
            return ""
        if self.backend == "ollama":
            return self._chat_ollama(system, user, expect_json, temperature,
                                     use_temp, web_search, thinking,
                                     json_temperature)
        if self.backend == "llamacpp":
            try:
                from ai.tts_batch import llm_enter, llm_exit
            except Exception:  # noqa
                llm_enter = llm_exit = None
            if llm_enter is not None:
                llm_enter()
            try:
                return self._chat_llamacpp(system, user, expect_json, temperature,
                                           use_temp, thinking, json_temperature,
                                           budget_override=budget)
            finally:
                if llm_exit is not None:
                    llm_exit()
        return self._chat_api(system, user, expect_json, temperature,
                              use_temp, web_search, thinking, json_temperature)

    # ---------------- API 后端（OpenAI 兼容） ----------------
    def _chat_api(self, system: str, user: str, expect_json: bool = False,
                  temperature: float | None = None, use_temp: bool = True,
                  web_search: bool | None = None,
                  thinking: bool | None = None,
                  json_temperature: float | None = None) -> str:
        if self._client is None:
            return ""
        try:
            messages = [{"role": "user", "content": user}]
            if system and system.strip():
                messages.insert(0, {"role": "system", "content": system})
            kwargs: dict = {
                "model": self.model,
                "messages": messages,
            }
            if use_temp and temperature is not None:
                kwargs["temperature"] = temperature
            elif use_temp:
                kwargs["temperature"] = self.temperature
            if expect_json:
                kwargs["response_format"] = {"type": "json_object"}
                # 结构化调用：允许 caller 通过 json_temperature 覆盖默认采样温度，
                # 例如推荐场景传较高温度以打破「每次开播推荐都一样」的确定性。
                if json_temperature is not None:
                    kwargs["temperature"] = json_temperature
                kwargs["timeout"] = 90
            if not self._safe_mode and (web_search is True or
                    (web_search is None and self.web_search)) and \
                    self._supports_web_search() and not expect_json:
                kwargs["tools"] = [{
                    "type": "builtin_function",
                    "function": {"name": "$web_search"},
                }]
            eff_thinking = (not self._safe_mode) and bool(
                thinking if thinking is not None else self.thinking)
            if eff_thinking and self._supports_thinking() and not expect_json:
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                kwargs["timeout"] = 60
            if "tools" in kwargs:
                kwargs["timeout"] = max(kwargs.get("timeout") or 0, 90)
            MAX_TOOL_ROUNDS = 4
            with self._llm_sem:
                for _ in range(MAX_TOOL_ROUNDS + 1):
                    resp = self._client.chat.completions.create(**kwargs)
                    msg = resp.choices[0].message
                    finish_reason = getattr(resp.choices[0], "finish_reason", None)
                    if getattr(msg, "content", None):
                        return msg.content or ""
                    tool_calls = getattr(msg, "tool_calls", None)
                    if not tool_calls or finish_reason != "tool_calls" \
                            or "tools" not in kwargs:
                        break
                    messages.append(self._assistant_msg_to_dict(msg))
                    for tc in tool_calls:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except Exception:  # noqa
                            args = {}
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.function.name,
                            "content": json.dumps(args, ensure_ascii=False),
                        })
            logger.warning("联网搜索工具循环 %d 轮仍未拿到正文。", MAX_TOOL_ROUNDS)
            return ""
        except Exception as e:  # noqa
            self.last_error = f"{type(e).__name__}: {redact(e)}"
            self.last_error_kind = self.classify_error(e)
            if self.last_error_kind == "content_filter":
                self._safe_mode = True
                retry = self._safe_content_retry(expect_json)
                if retry:
                    self.last_error = ""
                    self.last_error_kind = ""
                    return retry
                self._content_filter_until = time.monotonic() + 45
            logger.error("LLM 调用失败: %s", redact(e))
            return ""

    # ---------------- llama.cpp 直连后端（2026-11-16，脱离 Ollama） ----------------
    def _chat_llamacpp(self, system: str, user: str, expect_json: bool = False,
                       temperature: float | None = None, use_temp: bool = True,
                       thinking: bool | None = None,
                       json_temperature: float | None = None,
                       budget_override: int | None = None,
                       _retry: bool = True) -> str:
        """直连 llama.cpp server 的 /v1/chat/completions（不经 Ollama）。

        ⭐ 为什么必须参数层而非提示词层（2026-11-16 实测）：
          同一 qwen3.6:35b + 同一音乐提示词：
            · Ollama + 提示词层约束（"最多6句/以标记开头"）⇒ 模型把答案写进
              thinking 通道、正文仍为空（模型遵守提示词能力差）；
            · 无界思考（budget=-1）⇒ 反刍用满 2048 token，`finish_reason=length`、
              正文 0 字；
            · **budget=512 参数层** ⇒ 强制收尾标记出现、`finish_reason=stop`、
              正文 622 字，给定 state 与撤销记忆全部正确。
          ⇒ 思考长度必须用 `reasoning_budget_tokens` 参数硬控，不靠提示词。

        进程生命周期：由外部（用户脚本/服务）管理，本方法**只请求、不自启进程**。
        服务不可达时返回 ""，上层 brain 降级到模板口播（与 ollama 路径同纪律）。
        """
        import json as _json
        import urllib.request as _ur
        import urllib.error as _ur_err

        messages = [{"role": "user", "content": user}]
        if system and system.strip():
            messages.insert(0, {"role": "system", "content": system})

        # 思考预算决策（纯参数，不碰提示词）：
        #   · JSON 结构化调用：始终关思考（与 response_format/grammar 冲突风险）
        #   · 其他调用：实例配置 self.llamacpp_budget；调用方可显式 thinking 覆盖
        #     thinking=False → 0（关）；thinking=True → 用配置预算（≤0 时给默认 512）
        if expect_json:
            budget = 0
        else:
            want = self.thinking if thinking is None else bool(thinking)
            # 注意：llamacpp 后端默认就用受控思考（这正是脱离 Ollama 的意义），
            # 不沿用 self.thinking=False 的 Ollama 默认值；显式 thinking=False 才关。
            if thinking is None:
                budget = self.llamacpp_budget
            elif want:
                budget = self.llamacpp_budget if self.llamacpp_budget > 0 else 512
            else:
                budget = 0
        # ★ 2026-11-17 task-1（提速）：调用方可按调用覆盖思考预算。
        #   口播/转场实测「thinking 512 ≈ 35s@14.5tok/s」，降到 128 可省 ~25s/条。
        #   budget=0 表示完全关思考；None 表示沿用后端默认。
        if budget_override is not None and not expect_json:
            budget = max(0, int(budget_override))

        # 输出预算：口播 300 / JSON 800；开思考时必须给足（思考消耗预算内 token）。
        # ★ 2026-11-17 task-1：原来 budget>0 时固定 2048，实测口播正文只有 140~500 token，
        #   2048 只是上限但会放跑「写飞」的长尾。改成 budget+640（128→768、512→1152），
        #   既够「思考+正文」，又把最坏时延封住。
        if expect_json:
            _np = 800
        elif budget > 0:
            _np = min(2048, int(budget) + 640)
        else:
            _np = 300
        payload: dict = {
            "messages": messages,
            "max_tokens": _np,
            "stream": False,
        }
        if use_temp:
            payload["temperature"] = (json_temperature if expect_json and json_temperature is not None
                                      else (temperature if temperature is not None else self.temperature))
            payload["top_p"] = 0.95
            payload["top_k"] = 20
            payload["min_p"] = 0
        if budget > 0:
            # ★ APMP 同款参数层受控：到点注入收尾标记，强制把思考切进正文。
            # ★★ 2026-11-18（用户拍板）：默认开 preserve_thinking=True ——
            #   让思考链跨工具调用/多轮存活（Qwen 系支持；AREX 模板不支持会被
            #   llama.cpp 静默忽略、无害）。口播是单轮，不影响；agentic 链受益。
            #   extra_template_kwargs 仍可覆盖（评测脚本用）。
            _ctk = {"enable_thinking": True, "preserve_thinking": True}
            try:
                _ctk.update(getattr(self, "extra_template_kwargs", {}) or {})
            except Exception:  # noqa
                pass
            payload.update({
                "chat_template_kwargs": _ctk,
                "reasoning_format": "deepseek",
                "reasoning_budget_tokens": int(budget),
                "reasoning_budget_start_tag": "<think>",
                "reasoning_budget_end_tags": ["</think>"],
                "reasoning_budget_message": "\n[MUSIC_BUDGET_END]\n",
            })
        else:
            # ★★ 2026-11-18 修复：关思考时用 deepseek 格式而非 none。
            #   `reasoning_format:"none"` 会让 JSON grammar 拒绝 <think>，但
            #   Qwen3.5/3.6 默认仍吐 <think> 空壳 ⇒ 400
            #   "Unexpected empty grammar stack after accepting piece: <think>"
            #   ⇒ JSON 模式（转场 decide_transition）全失败、转场 0/12。
            #   改 deepseek：enable_thinking=false 下 <think> 被正确剥离、JSON 正常。
            _ctk_off = {"enable_thinking": False}
            try:
                _ctk_off.update(getattr(self, "extra_template_kwargs", {}) or {})
            except Exception:  # noqa
                pass
            payload.update({
                "chat_template_kwargs": _ctk_off,
                "reasoning_format": "deepseek",
            })
        if expect_json:
            payload["response_format"] = {"type": "json_object"}

        req = _ur.Request(
            self.llamacpp_host + "/v1/chat/completions",
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            # 全局串行（复用 _ollama_lock）：本机单实例并发会排队拖死，
            # 与 Ollama 路径共用同一把锁，避免两个后端同时压垮本机模型。
            with _ollama_lock:
                with _ur.urlopen(req, timeout=180) as resp:
                    raw = _json.load(resp)
            choice = (raw.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            # ★ deepseek 格式下思考走独立的 reasoning_content 字段，content 本身
            #   **就是正文**（实测 257 字、无 <think> 标记）。不能套用 Ollama 的
            #   `_strip_think()`——它对"找不到 </think>"一律返回空（那是给内联
            #   思考用的安全策略），会把正常正文全清掉（实测踩到，content 257→0）。
            # 只有 content 真的内联了 <think> 才剥（防模板差异），否则原样返回。
            if budget > 0 and any(m in content for m in _THINK_CLOSE_TAGS):
                content = self._strip_think(content)
            else:
                # 关思考时服务端仍可能吐一个空的 `<think>\n\n</think>` 外壳
                # （实测：`'<think>\n\n</think>\n\n2'`）；只剥**开头紧邻的空块**，
                # 不碰正文里可能出现的正常尖括号内容。
                import re as _re
                content = _re.sub(r"^\s*<think>\s*</think>\s*", "", content).strip()
            # ★ 2026-11-17 task-1（安全网）：思考预算太小可能把正文挤空。
            #   实测口径：budget=512 能稳出正文；128/256 未验证，最怕 finish_reason=length
            #   或思考占满后正文为空。这里**关掉思考重试一次**（只重试一次，不会递归）。
            if not content and budget > 0 and _retry:
                logger.warning("[LLM] 思考预算 %d 未产出正文（finish=%s）→ 关思考重试一次",
                               budget, choice.get("finish_reason"))
                return self._chat_llamacpp(system, user, expect_json, temperature,
                                           use_temp, thinking, json_temperature,
                                           budget_override=0, _retry=False)
            if os.getenv("MUSICAI_LLM_DEBUG") == "1":
                logger.info("[LLM] budget=%d finish=%s 正文=%d字",
                            budget, choice.get("finish_reason"), len(content))
            self.last_error = ""
            self.last_error_kind = ""
            return content
        except Exception as e:  # noqa
            self.last_error = f"{type(e).__name__}: {redact(e)}"
            self.last_error_kind = self.classify_error(e)
            logger.warning("llamacpp 调用失败（%s）: %s", self.llamacpp_host, redact(e))
            return ""

    # ---------------- Ollama 后端（本地小模型） ----------------
    def _chat_ollama(self, system: str, user: str, expect_json: bool = False,
                     temperature: float | None = None, use_temp: bool = True,
                     web_search: bool | None = None,
                     thinking: bool | None = None,
                     json_temperature: float | None = None) -> str:
        """调用本机 Ollama。联网搜索由 SearchProvider 在 brain 层完成并注入提示词，
        本方法不处理 web_search（本地小模型不支持 Kimi 的 $web_search）。

        thinking 控制 qwen3.5 系列的思考（reasoning）：
          - 关闭（默认）：实测 think=False 时 Ollama 完全不产出思考字段、正文干净，
            且从 88s 降到 ~1s，故默认关闭以保证 DJ 口播即时出稿。
          - 开启：正文质量可能更高，但会多生成数千 token 思考，广播生成约 60~80s。
          - JSON 结构化调用（品味/推荐/转场）始终关闭思考，避免与 format 冲突且加速。
        """
        try:
            from ollama import Client
        except Exception as e:  # noqa
            logger.error("ollama 未安装: %s", redact(e))
            self.last_error = f"ollama 未安装: {redact(e)}"
            self.last_error_kind = "other"
            return ""
        try:
            # timeout=120：单轮「冷加载+生成」最坏约 13s，常驻后 ~8s；留出充足余量
            # 让慢但有效的生成跑完，而不是像旧值 45s 那样把 60~97s 的并发排队请求
            # 一律判超时丢弃（那正是「口播全变默认开场白」的根因）。
            client = Client(host=self.ollama_host, timeout=120)
            messages = [{"role": "user", "content": user}]
            if system and system.strip():
                messages.insert(0, {"role": "system", "content": system})
            # thinking 优先级：JSON 调用始终关；否则 调用参数 > 实例配置(ai.thinking) > 默认关
            # （qwen3.5 默认关思考：think=False 干净且 ~1s 出稿，见上文 docstring）
            if expect_json:
                think = False
            else:
                # 思考模式仅在「模型/供应商确实支持」时开启。本地 Ollama 的 4B 小模型
                # （qwen3.5:4b）对 Ollama 的 think=True 参数兼容极差——实测开启后无论
                # num_predict 取 300/800/1500，整段响应都返回空正文，直接毁掉口播/转场。
                # 故与 API 路径（_chat_api，line~171）保持一致：用 _supports_thinking()
                # 门控，Ollama 后端一律视为不支持、think=False。用户若在设置里勾了
                # ai.thinking=True，仅对 Moonshot/Kimi 等真支持思考的 API 后端生效。
                want = self.thinking if thinking is None else thinking
                think = bool(want) and self._supports_thinking()
            # 统一 Ollama 选项：
            #  - num_ctx：取自 ollama_num_ctx()（默认 8192，可用 MUSICAI_OLLAMA_NUM_CTX 覆盖）；留出充足上下文窗（系统提示 + 模板 + 联网检索注入的资料
            #    合计常超 3k token）；过小会截断输入、偶发导致空正文。4B 模型下 prefill
            #    仍可接受，且 Ollama 会在超窗时自动扩容，此处只兜底下限。
            #  - num_predict：输出封顶，防止本地小模型偶发「车轱辘话」无限生成把单轮拖到
            #    几十秒；广播 300 / JSON 结构化 800 均留足余量（广播 TTS 上限 180 字）。
            #  - num_gpu=-1：所有层驻 GPU，禁止 CPU offload（用户明确要求；放任 CPU
            #    推理会把单轮拖到几十秒甚至超时）。
            #  ⚠ keep_alive=-1 是 chat() 的「顶层参数」，不是 options 字段！
            #    ollama 0.6.2 的 Options 类型里没有 keep_alive；此前误放进 opts 导致
            #    从未生效，模型按默认 ~5m 自动卸载、下轮冷加载。已实测签名确认。
            # ★ R119-3k：开了思考就必须**给足输出预算**。实测 granite4.2:3b 的推理约
            #   1k token 且**内联在同一次响应里**，`num_predict=300` 会"思考没完就被截断"
            #   ⇒ 正文空（`done_reason=length`）。故 think=True 时放宽到 2000。
            _np = 800 if expect_json else (2000 if think else 300)
            # ★ R119-4b：**num_ctx 4096 → 16384**。铁证（2026-09-19 实测，同一段提示词
            #   只改窗口）：num_ctx=4096 时 `prompt_eval_count` 只有 **2050**，而真实长度是
            #   **6168** ⇒ **砍掉 2/3，且砍的是最前面** —— 被砍掉的正是 `BROADCAST_SYSTEM`
            #   开头的「多语言标签契约」和用户提示词的 §0~§3。
            #   后果实测：6 个模型 × 8 样本 **48/48 一个语言标签都没打**（日文会被 TTS 按
            #   汉字硬读成乱码）；而把窗口放到 16384 后，**同一个模型立刻打出了 `<ja>…</ja>`**。
            #   显存代价很小：4096→16384 仅 +~480 MiB。可用 `MUSICAI_OLLAMA_NUM_CTX` 覆盖。
            opts: dict = {
                # ★ R119-4o：**16384 → 8192**。实测 8192 就足以不截断（真实提示词长度 6168 token），
            #   而 16384 会把 Ollama 占用从 5337 → 5599 MiB、并把口播 LLM 耗时塌到 **82 秒**
            #   （真机日志：84 次调用中位数 82060ms ⇒ 挤显存 ⇒ 部分层落 CPU）
            #   ⇒ 口播错过播放窗口 ⇒ intro/outro 都放不出来（用户报到处退回默认口播）。
            #   仍可用 MUSICAI_OLLAMA_NUM_CTX 覆盖。
                "num_ctx": ollama_num_ctx(),
                "num_predict": _np,
                "num_gpu": -1,
            }
            if use_temp and temperature is not None:
                opts["temperature"] = temperature
            elif use_temp:
                opts["temperature"] = self.temperature
            kwargs: dict = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": bool(think),
                "options": opts,
                "keep_alive": -1,  # 顶层参数：模型常驻显存（不是 options 字段）
            }
            if expect_json:
                # Ollama 用 format='json'（字符串）约束 JSON 输出；
                # 注意：不能用 OpenAI 的 {"type":"json_object"}（Ollama 会报
                # "Unrecognized schema" 400）。本地小模型 JSON 较慢，默认降温提高稳定性；
                # 但推荐等需要多样性的场景可通过 json_temperature（如 0.7）调高采样，
                # 打破「每次开播推荐都一样」的确定性（配合 brain 层注入的随机种子）。
                kwargs["format"] = "json"
                kwargs["options"]["temperature"] = (
                    json_temperature if json_temperature is not None else 0.3)
            # 全局串行：本机 Ollama 同一时刻只能高效跑一个请求，并发会排队导致
            # 60~97s 的极端延迟与超时失败。串行后每请求稳定 ~8s 完成，单首歌 3~4 分钟
            # 时长足以让「边播边想」预取在切歌前算完，绝不会再出现「口播全变默认」。
            with _ollama_lock:
                resp = client.chat(**kwargs)
                msg = (resp or {}).get("message", {}) or {}
                content = (msg.get("content") or "").strip()
                if think:
                    # ★ R119-3k：切掉内联思考（不切 = 英文推理进 TTS）
                    content = self._strip_think(content)
                # 非结构化调用（广播/转场）偶发返回空正文：根因通常是「模型/参数不兼容」
                # —— 典型如本地 qwen3.5:4b 开了 think=True（Ollama 思考参数兼容差，开启后
                # 整段响应为空）；本模块已在上方用 _supports_thinking() 门控，Ollama 后端
                # 一律 think=False，从根上消除这类空正文。此外保留退避重试作为兜底：
                # 即便仍有偶发退化生成（只吐标点/换行、或聊天模板偶产空 assistant 消息），
                # 重试 + 轻微扰动参数多能救回；JSON 结构化调用对启动时延敏感，不在此重试。
                if not content and not expect_json:
                    for _attempt in range(2):  # 首 + 2 次重试 = 最多 3 次
                        logger.warning(
                            "Ollama 返回空正文，第 %d 次重试（%s）。",
                            _attempt + 1, self.model)
                        time.sleep(1.5)  # 短暂退避：等 GPU 争用缓解、模型退出退化态
                        # 重试轻微扰动参数：升温 + 放宽 num_predict，规避退化生成
                        _ro = dict(opts)
                        _ro["temperature"] = min(
                            1.0, float(opts.get("temperature", 0.8)) + 0.1)
                        _ro["num_predict"] = max(
                            int(opts.get("num_predict", 300)), 400)
                        _rk = dict(kwargs)
                        _rk["options"] = _ro
                        try:
                            resp = client.chat(**_rk)
                        except Exception as e:  # noqa
                            logger.warning("Ollama 重试调用异常：%s", redact(e))
                            continue
                        msg = (resp or {}).get("message", {}) or {}
                        content = (msg.get("content") or "").strip()
                        if think:
                            content = self._strip_think(content)
                        if content:
                            break
            return content
        except Exception as e:  # noqa
            self.last_error = f"{type(e).__name__}: {redact(e)}"
            self.last_error_kind = self.classify_error(e)
            logger.error("Ollama 调用失败: %s", redact(e))
            return ""

    # ---------------- 工具调用 / Function Calling ----------------
    def chat_with_tools(self, messages: list[dict], tools: list[dict],
                        expect_json: bool = False) -> tuple[str, list[dict]]:
        """发送带 tools 的请求，返回 (正文, tool_calls 列表)。

        用于验证/使用模型的 Function Calling 能力（如天气/检索工具）。
        两个后端均支持：Ollama 原生 tools；API 后端走 OpenAI tools。
        """
        if not self.enabled:
            return "", []
        try:
            if self.backend == "ollama":
                from ollama import Client
                client = Client(host=self.ollama_host, timeout=120)
                kwargs: dict = {
                    "model": self.model, "messages": messages,
                    "tools": tools, "stream": False,
                    # 与 _chat_ollama 一致：num_gpu 全层驻 GPU、keep_alive 顶层常驻
                    "options": {"num_ctx": ollama_num_ctx(), "num_gpu": -1},
                    "keep_alive": -1,
                }
                if expect_json:
                    kwargs["format"] = "json"
                with _ollama_lock:
                    resp = client.chat(**kwargs)
                msg = (resp or {}).get("message", {}) or {}
                return (msg.get("content") or ""), msg.get("tool_calls") or []
            # API 后端
            if self._client is None:
                return "", []
            kwargs = {"model": self.model, "messages": messages, "tools": tools}
            if expect_json:
                kwargs["response_format"] = {"type": "json_object"}
                kwargs["timeout"] = 90
            with self._llm_sem:
                resp = self._client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            tcs = getattr(msg, "tool_calls", None) or []
            parsed = []
            for tc in tcs:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:  # noqa
                    args = {}
                parsed.append({"name": tc.function.name, "arguments": args})
            return (getattr(msg, "content", None) or ""), parsed
        except Exception as e:  # noqa
            logger.error("工具调用失败: %s", redact(e))
            self.last_error = f"{type(e).__name__}: {redact(e)}"
            self.last_error_kind = self.classify_error(e)
            return "", []

    def verify_tool_use(self) -> tuple[bool, str]:
        """验证当前模型是否支持 Function Calling / Tool Use。

        构造一个最简单的「取数字平方」工具，看模型是否回 tool_calls。
        返回 (是否支持, 人类可读信息)。
        """
        tools = [{
            "type": "function",
            "function": {
                "name": "square",
                "description": "返回输入数字的平方",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "输入数字"}
                    },
                    "required": ["x"],
                },
            },
        }]
        messages = [{"role": "user",
                     "content": "请调用 square 工具计算 7 的平方，不要解释。"}]
        content, tcs = self.chat_with_tools(messages, tools)
        if tcs:
            names = [t.get("name") for t in tcs]
            return True, f"✅ 模型支持 Function Calling（返回工具：{names}）"
        if content:
            return False, (f"⚠️ 模型未返回工具调用，仅生成文本："
                           f"{content[:40]}（可能该模型/后端不支持 tool use）")
        return False, "❌ 未收到任何响应（模型不可达或调用失败）"

    @staticmethod
    def _assistant_msg_to_dict(msg) -> dict:
        """把 assistant 消息转成可回传的 dict，保留 tool_calls / 思考内容。"""
        d: dict = {"role": "assistant"}
        content = getattr(msg, "content", None)
        if content is not None:
            d["content"] = content
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "",
                    },
                }
                for tc in tcs
            ]
        rc = getattr(msg, "reasoning_content", None)
        if rc is None and hasattr(msg, "model_extra"):
            rc = (msg.model_extra or {}).get("reasoning_content")
        if rc is not None:
            d["reasoning_content"] = rc
        return d

    def _safe_content_retry(self, expect_json: bool) -> str:
        """One neutral retry after a provider-side false positive."""
        try:
            system = (
                "你是音乐播放器的文本助手。只处理歌曲名称、歌手、风格和播放场景；"
                "不要讨论健康、心理、暴力、违法或成人主题。输出简短、中性的中文内容。"
            )
            user = ("请完成普通音乐播放辅助任务。"
                    "如果需要 JSON，请仅返回合法 JSON；否则只写一句简短电台口播。")
            kwargs: dict = {"model": self.model,
                            "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]}
            if expect_json:
                kwargs["response_format"] = {"type": "json_object"}
            resp = self._client.chat.completions.create(**kwargs)
            return (resp.choices[0].message.content or "").strip()
        except Exception as retry_error:  # noqa
            logger.warning("LLM 安全降级重试失败: %s", redact(retry_error))
            return ""

    def _supports_web_search(self) -> bool:
        """判断当前供应商是否支持 Moonshot/Kimi 的 $web_search 内置工具。"""
        base = (self.api_base or "").lower()
        model = (self.model or "").lower()
        return ("moonshot" in base) or model.startswith("moonshot") \
            or model.startswith("kimi")

    def _supports_thinking(self) -> bool:
        """判断当前供应商/模型是否支持「思考模式」(extended thinking / reasoning)。

        ★ R119-3k：**不再按"后端 == ollama"一刀切**，改为**按模型能力**判定。
          历史原因：本地 `qwen3.5:4b` 在 Ollama 上 `think=True` 实测**只出思考、正文 0 字**
          （见 2026-09-19 日志 ⑳x：np=3000 时思考 5938 字、正文 0 字），所以当时
          "Ollama 一律 False"是**对的**。但 `granite4.2` 系列是**单权重内建 thinking 开关**
          （实测 `think=True` 能把推理+`</think>`+正文都吐出来）⇒ 一刀切会把可用能力封死。
          原则：**按模型名白名单放行**，未知模型维持 False（保守 = 不破坏现有口播）。
        """
        base = (self.api_base or "").lower()
        model = (self.model or "").lower()
        if ("moonshot" in base) or model.startswith("moonshot") or model.startswith("kimi"):
            return True
        if (self.backend or "").lower() == "ollama":
            # 已知**可**思考（实测过）：granite4.2 全系
            return model.startswith("granite4.2") or model.startswith("granite4.1")
        return False

    @staticmethod
    def _strip_think(text: str) -> str:
        """★ R119-3k：把 Ollama **内联输出**的思考切掉，只留正文。

        为什么必须自己切：实测 Ollama 0.34.2 + `granite4.2:3b` 在 `think=True` 时
        **不会**把推理放进 `resp.message.thinking`（该字段为空），而是把它**内联写进
        content**，并以 `</think>` 收尾 —— 实测原文：
            `... I'll present this as the answer.\n</think>\n今晚深夜，欢迎收听…`
        不切的话，这段**英文推理会被直接送去 TTS 念出来**（口播变成英语思维链）。

        找不到任何收尾标记 ⇒ 说明整段都还是思考（被 num_predict 截断）⇒ **返回空串**，
        交给上层的"空正文重试 / 离线回退"处理。**绝不能把思考当正文返回。**
        """
        if not text:
            return ""
        for mark in _THINK_CLOSE_TAGS:
            i = text.rfind(mark)
            if i >= 0:
                return text[i + len(mark):].strip()
        return ""

    # ---------------- 连接诊断 ----------------
    @staticmethod
    def test_connection(api_base: str = "", api_key: str = "",
                        model: str = "") -> tuple[bool, str]:
        """诊断：用给定参数发一个最小请求，验证后端是否真的可用。

        返回 (是否成功, 人类可读信息)。不依赖实例状态。
        - backend=ollama 时（从 config 读取）测试本机 Ollama 服务；
        - 否则按 OpenAI 兼容接口测试。
        """
        # 按"传入参数"推断被测后端，而不是只看 config：
        # 调用方（设置页）可能正想测试一个**与当前 config 不同**的后端。
        # 判据：传了 api_key ⇒ 一定是远程 API（本地后端不需要 key）；
        #       否则回退 config.backend。
        if api_key:
            backend = "api"
        else:
            backend = (user_config.ai.get("backend") or "api").lower()
        if backend == "ollama":
            host = api_base or user_config.ai.get("ollama_host") \
                or "http://127.0.0.1:11434"
            model = model or user_config.ai.get("ollama_deep") or "qwen3.6:35b-a3b"
            try:
                from ollama import Client
                client = Client(host=host)
                resp = client.chat(model=model,
                                   messages=[{"role": "user",
                                              "content": "ping，请只回复 ok"}],
                                   stream=False)
                content = ((resp or {}).get("message", {}) or {}).get(
                    "content", "") or ""
                return True, f"✅ Ollama 连接成功（模型：{model}），返回：{content[:24]}"
            except Exception as e:  # noqa
                return False, (f"❌ Ollama 连接失败（已退役后端，建议改用 llamacpp）："
                               f"{type(e).__name__}: {redact(e)}")
        if backend == "llamacpp":
            host = (api_base or user_config.ai.get("llamacpp_host")
                    or "http://127.0.0.1:8080").rstrip("/")
            try:
                import urllib.request
                with urllib.request.urlopen(host + "/health", timeout=8) as r:
                    health = r.read(120).decode("utf-8", "replace")
                return True, f"✅ 本机 llama.cpp 连接成功（{host}）：{health[:60]}"
            except Exception as e:  # noqa
                return False, (f"❌ 本机 llama.cpp 连接失败：{type(e).__name__}: "
                               f"{redact(e)}\n常见原因：llama-server 未启动；"
                               f"本项目由 start_api_host.py 自动托管它，请确认 8080 端口。")
        # ---- API 后端 ----
        try:
            base = validate_api_base(normalize_llm_api_base(api_base))
        except ValueError as e:
            return False, f"❌ 地址不安全：{e}"
        key = api_key or ""
        model = model or "gpt-4o-mini"
        if not base or not key:
            return False, "请先填写 API 地址和 Key 再测试。"
        # 地址可带 /v1 也可不带（不同厂商路径不同），且已自动兼容误填的完整 endpoint，
        # 所以不再对"没有 /v1"发误导提示。
        hint = ""
        try:
            from openai import OpenAI
            client = OpenAI(base_url=base, api_key=key, timeout=20)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping，请只回复 ok"}],
                max_tokens=16,
            )
            content = (resp.choices[0].message.content or "").strip()
            return True, f"✅ 连接成功（模型：{model}），返回：{content[:24]}{hint}"
        except Exception as e:  # noqa
            msg = f"❌ 连接失败：{type(e).__name__}: {redact(e)}"
            if "404" in str(e):
                msg += "\n常见原因：地址缺少 /v1 结尾，或模型名不存在。"
            elif "401" in str(e) or "403" in str(e) or "Authentication" in str(e):
                msg += "\n常见原因：API Key 无效 / 未授权。"
            elif "timeout" in str(e).lower() or "Connect" in str(e):
                msg += "\n常见原因：网络不通 / 地址无法访问（是否被墙或需代理）。"
            return False, msg + hint

    # ---------------- 错误归类 ----------------
    @staticmethod
    def classify_error(e: Exception) -> str:
        """把 LLM 异常归类，方便 UI 给出针对性提示。"""
        s = str(e).lower()
        if ("content_filter" in s or "high risk" in s or "moderat" in s
                or "审核" in s or "safety" in s or "敏感" in s):
            return "content_filter"
        if "timeout" in s or "connect" in s or "timed out" in s:
            return "network"
        if "401" in s or "403" in s or "authentication" in s or "unauthorized" in s:
            return "auth"
        if "404" in s or "not found" in s:
            return "notfound"
        if "400" in s or "bad request" in s:
            return "badrequest"
        return "other"
