"""
AI 大脑：编排 LLM 能力（分析偏好 / 生成口播 / 推荐选歌）。

职责分层（与 ai/model.py、ai/prompts.py 配合）：
  - ai/model.py  -> "AI 大模型"层：怎么调模型（连接/调用/诊断）。
  - ai/prompts.py -> "AI 提示词"层：发给模型的具体内容模板。
  - 本文件       -> 编排层：把实时上下文拼进提示词、调用模型、解析结果、离线兜底。

当未配置 API（LLMClient.enabled=False）时，全部走"离线模板"，
保证软件无 key 也能演示完整流程。
"""
from __future__ import annotations

import os
import re
import json
import logging
from typing import Any

from config.settings import user_config, RadioConfig
from ai.model import LLMClient
from ai.prompts import (
    PREF_SYSTEM, TRANSITION_SYSTEM, RECO_EXPLAIN_SYSTEM,
    TASTE_ANALYSIS_PROMPT, BROADCAST_TEMPLATE, BROADCAST_SYSTEM, BROADCAST_SYSTEM_EN,
    BROADCAST_OUTPUT_DEFAULT, TRANSITION_OUTPUT_DEFAULT,
    RECO_PROPOSE_SYSTEM, RECO_PICK_SYSTEM,
)
# strip_tags：去掉口播里的 <lang>...</lang> 标签，供 UI 显示（TTS 仍用带标签原文）
from core.tts_segment import strip_tags

from utils.location import time_slot

logger = logging.getLogger("MusicAI.Brain")


def is_meta_leak(text: str) -> bool:
    """★ 2026-11-19（用户「Sgt Pepper 口播念出了 The prompt says 字数要求」）：
    检测 LLM 把 prompt 元指令/分析/素材草稿当正文吐出来的"元信息泄漏"。

    这类输出不是口播正文，绝不能截断播出（截断只会把垃圾的前半段念给听众）。
    命中任一特征即判为垃圾 → 调用方回退离线文案。
    只匹配明确的元指令措辞，避免误杀正常提到"字数"的正文。
    """
    t = (text or "")[:400]
    markers = (
        "the prompt says", "prompt says", "目标 50", "目标50", "50~70",
        "50-70", "字数要求", "不超过", "含标点", "标点不计",
        "不足 50", "不足50", "超过 90", "超过90", "不合格",
        "let's start by", "fact to use:", "素材罗列", "思考过程",
        "作为 ai", "as an ai", "以下是", "分析如下", "重写如下",
    )
    low = t.lower()
    return any(m.lower() in low for m in markers)


def is_mostly_cjk(text: str, threshold: float = 0.15) -> bool:
    """★ 2026-11-19（用户「切英文没用，文案还是中文」）：
    检测英文口播模式下 LLM 是否回落到中文/日文正文。

    资料里含中文/日文（歌名、背景）时，LLM 偶尔无视英文 SYSTEM 直接吐中文。
    按 CJK 字符占字母+数字+字符的比例判断；歌名专名少量 CJK 不算（阈值0.15）。
    """
    t = (text or "").strip()
    if not t:
        return False
    import re as _re
    cjk = len(_re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", t))
    latin = len(_re.findall(r"[A-Za-z]", t))
    total = cjk + latin
    if total == 0:
        return False
    return (cjk / total) > threshold


def smart_truncate_sentence(text: str, target: int = 120, hard_cap: int = 165) -> str:
    """★ 2026-11-18（用户「断也要断完整的一句，多一点没关系」）：
    超长口播的**智能截断**——从 target 附近**向后找第一个完整句末**（。！？），
    保证截断处是完整一句、字数可略超 target（hard_cap 内）；绝不切在句子中间。

    优先在 [target*0.7, hard_cap] 区间取最早的句子结束点（尽量贴近 target 又完整）；
    找不到句末才退到逗号/分号；再找不到才硬截到 hard_cap（极端防御）。
    """
    t = (text or "").strip()
    if len(t) <= target:
        return t
    limit = min(hard_cap, len(t))
    window = t[:limit]
    # ① 向后找第一个完整句末（。！？），起点不低于 target*0.7，保证不太短
    floor = int(target * 0.7)
    for i, ch in enumerate(window):
        if ch in "。！？" and i + 1 >= floor:
            return t[: i + 1].strip()
    # ② 没有句末 → 退到逗号/分号（至少半句，仍比硬切自然）
    for i, ch in enumerate(window):
        if ch in "，,；;" and i + 1 >= floor:
            return t[: i + 1].strip()
    # ③ 极端：一个标点都没有 → 硬截（无法保证完整句，仅此一种情况）
    return window.strip()


def truncate_sentences_en(text: str, max_words: int = 100) -> str:
    """★ 2026-11-19（用户「英文口播可以略长到150单词，截断只截句子不要逗号断」）：
    英文口播专用截断——**只在完整句末（.!?）断，绝不在逗号断**。

    逐句累加词数，超过 max_words 就停在上一句末；若单句就超上限（罕见），
    保留整句（宁可长，也不在句中/逗号切）。没有句末标点的文本原样返回。
    """
    t = (text or "").strip()
    if not t:
        return t
    import re as _re
    # 按句末标点切，保留标点（. ! ? 及连续省略号）
    parts = _re.findall(r"[^.!?]+(?:[.!?]+(?=\s|$)|$)", t)
    parts = [p for p in parts if p.strip()]
    if len(parts) <= 1:
        return t   # 单句或无法分句 → 宁可长，不乱切
    kept: list[str] = []
    total = 0
    for p in parts:
        wc = len(p.split())
        if kept and total + wc > max_words:
            break
        kept.append(p.strip())
        total += wc
        if total >= max_words:
            break
    return " ".join(kept).strip() or t


def fact_years_hint(search_ctx: str, today_year: int | None = None) -> str:
    """★ 2026-09-21：把资料里的年份换算成"距今 N 年"，**替模型做算术**。

    为什么必须由 app 算（实测，见 `probe_llm_search_usage.py`）：
      提示词里原先写着示例「资料写 2003 年发行、今天是 2026 年 ⇒ 二十多年前的作品」，
      结果小模型**把示例当模板背下来** —— 2018 年的《Lemon》也写成"二十多年前"。
      小模型的算术本就是弱点，而它照抄少样本示例的倾向远强于算对的倾向。
      ⇒ 直接给出算好的数字，并要求它"只许照抄"。

    返回 `"\n\n【资料里出现的年份】2003（距今 23 年）、2018（距今 8 年）"`；
    资料里没有可用年份时返回 `""`（**不注入空标题**，否则模型会对着空标题编）。
    """
    import datetime as _dt
    text = str(search_ctx or "")
    if not text:
        return ""
    this_year = int(today_year or _dt.date.today().year)
    try:
        years = sorted({int(x) for x in
                        re.findall(r"(?:首次发行年份\s*|首发于\s*)((?:19|20)[0-9]{2})(?![0-9])", text)})
    except Exception:                                     # noqa 诊断绝不阻断口播
        return ""
    years = [y for y in years if 1900 <= y <= this_year]
    if not years:
        return ""
    return "\n\n【资料里出现的年份】" + "、".join(
        "%d（距今 %d 年）" % (y, this_year - y) for y in years)


class AIBrain:
    def __init__(self) -> None:
        # "AI 大模型"客户端（连接/调用都委托给它）
        self.llm = LLMClient()
        # 快速模型客户端（仅 ollama 后端且开启快速模型时生效，跑转场/短口播）
        self.llm_fast = None
        self.web_search = False
        self.thinking = False
        # ★ 2026-11-17 task-1（提速）：口播/转场的思考预算（tokens）。
        #   实测 thinking 512 ≈ 23~35s；口播取 256（省 ~18s）并带「正文为空
        #   → 关思考重试一次」的安全网（见 ai/model.py）；转场只要判断
        #   outro/none + 10~25 字，取 64（再省 ~8s），不够由安全网兜底。
        #   0=完全关思考；env 可覆盖（MUSICAI_BC_BUDGET / MUSICAI_TRANS_BUDGET）。
        def _env_budget(name: str, default: int) -> int:
            try:
                v = os.environ.get(name)
                return max(0, int(v)) if v not in (None, "") else default
            except (TypeError, ValueError):
                return default
        self._bc_budget = _env_budget("MUSICAI_BC_BUDGET", 256)
        self._trans_budget = _env_budget("MUSICAI_TRANS_BUDGET", 64)
        # ★ R119-26b：最近已用过的**转场短句**（只存文本，最多 8 条）。
        #   用途：写进下一条转场的 user 消息里，让模型避开已经说过的句式 ——
        #   用户反馈"outro 永远都是一样的句式"，根因之一就是没有任何去重。
        self._recent_transitions: list[str] = []
        self.reconfigure()

    def reconfigure(self) -> None:
        """从用户配置（重）构建 LLM 客户端；ollama 后端额外建快速(3B)客户端。"""
        self.llm.reconfigure()
        a = user_config.ai
        self.web_search = bool(a.get("web_search", False))
        self.thinking = bool(a.get("thinking", False))
        # 本地 Ollama 后端：可选启用 3B 快速模型跑短口播/转场
        if (a.get("backend", "api").lower() == "ollama"
                and a.get("ollama_fast_enabled", True)):
            try:
                if self.llm_fast is None:
                    self.llm_fast = LLMClient(fast=True)
                else:
                    self.llm_fast.reconfigure()
            except Exception as e:  # noqa
                logger.warning("快速模型客户端初始化失败：%s", e)
                self.llm_fast = None
        else:
            self.llm_fast = None

    @property
    def enabled(self) -> bool:
        """便捷访问：底层 LLM 是否可用（已配置 base/key 且客户端初始化成功）。"""
        return self.llm.enabled

    # ---------------- 连接诊断（委托给模型层） ----------------
    def test_connection(self, api_base: str = "", api_key: str = "",
                        model: str = "") -> tuple[bool, str]:
        return LLMClient.test_connection(api_base, api_key, model)

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _safe_json(text: str, default: Any) -> Any:
        if not text:
            return default
        t = text.strip()
        # 去掉可能的 ```json ... ``` 代码围栏包裹
        if t.startswith("```"):
            t = t.strip("`")
            if t.lower().startswith("json"):
                t = t[4:]
            t = t.strip()
        try:
            return json.loads(t)
        except Exception:  # noqa
            # 兜底：截取第一个 { 到最后一个 }
            s = t.find("{"); e = t.rfind("}")
            if s != -1 and e != -1 and e > s:
                try:
                    return json.loads(t[s:e + 1])
                except Exception:  # noqa
                    return default
            return default

    @staticmethod
    def render_template(template: str, mapping: dict) -> str:
        """把 {{key}} 替换为 mapping 中对应值（None 视为空串）。"""
        out = template
        for k, v in mapping.items():
            out = out.replace("{{" + k + "}}", "" if v is None else str(v))
        # ★ R119-3v 兜底：清掉**所有没被 mapping 覆盖**的占位符。
        #   为什么必须做：三份用户提示词（v2）引用了大量 App 未提供的变量
        #   （口播 8 个 / 转场 20 个 / 品味 10 个），原实现只替换已知键 ⇒
        #   它们会以 `{{release_year}}` 这类**字面量**残留进 system 提示，
        #   模型会把花括号当正文读。上方注释写的就是"避免残留字面量"，
        #   故这里把**意图补全**（不是新增行为）。允许 `{{a.b}}` 之类怪写法。
        out = re.sub(r"\{\{[^{}]{0,64}\}\}", "", out)
        return out

    @staticmethod
    def _extract_song_selections(parsed: Any, n: int) -> list:
        """从 LLM 返回的各种 JSON 结构中稳健抠出 [{title, artist}, ...]。

        兼容以下任意形态（模型偶尔会"自由发挥"结构，必须兜底）：
          - 裸数组：[{"title":..., "artist":...}, ...]
          - 包装对象：{"songs":[...]} / {"歌曲":[...]} / 任一值为数组的键
          - 单首歌 dict：{"title":..., "artist":...}
          - 中文键名：{"歌曲名称":..., "歌手":...}
          - 数组中混有字符串歌名（直接当歌名、歌手留空）
        始终返回 list[dict]，最多 n 个；解析不出任何歌名则返回 []。
        """
        if parsed is None:
            return []
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
            # 1) 明确的列表键（中英文都兼容）
            for key in ("songs", "recommendations", "list", "items",
                        "tracks", "歌曲", "推荐", "歌单"):
                v = parsed.get(key)
                if isinstance(v, list):
                    items = v
                    break
                if isinstance(v, dict):   # 单首歌被误包成对象值
                    items = [v]
                    break
            else:
                # 2) 兜底：取唯一 / 最长的数组值，或唯一的对象值
                list_vals = [v for v in parsed.values() if isinstance(v, list)]
                dict_vals = [v for v in parsed.values() if isinstance(v, dict)]
                if len(list_vals) == 1:
                    items = list_vals[0]
                elif list_vals:
                    items = max(list_vals, key=len)
                elif len(dict_vals) == 1:
                    items = [dict_vals[0]]
                else:
                    # 3) 整个 dict 本身就是一首歌
                    items = [parsed]
        else:
            return []

        result = []
        for x in items:
            if isinstance(x, dict):
                title = (x.get("title") or x.get("歌名") or x.get("歌曲名")
                         or x.get("歌曲名称") or x.get("名称") or x.get("name")
                         or "").strip()
                artist = (x.get("artist") or x.get("歌手") or x.get("演唱者")
                          or x.get("artist_name") or "").strip()
            elif isinstance(x, str) and x.strip():
                title, artist = x.strip(), ""
            else:
                continue
            if not title:
                continue
            result.append({"title": title, "artist": artist})
            if len(result) >= n:
                break
        return result

    # ---------------- 对外能力 ----------------
    def analyze_preferences(self, history: list[dict]) -> dict:
        """history: [{title,artist,genre,time_slot}]"""
        if not history:
            return {"tags": [], "summary": "新用户，暂无偏好", "mood_by_time": {}}
        if not self.llm.enabled:
            return self._offline_prefs(history)
        user = "近期播放历史：\n" + "\n".join(
            f"- {h.get('title')} / {h.get('artist')} / {h.get('genre')} / {h.get('time_slot')}"
            for h in history[:50]
        )
        out = self.llm.chat(PREF_SYSTEM, user, expect_json=True)
        return self._safe_json(out, self._offline_prefs(history))

    def analyze_taste(self, tracks: list) -> dict:
        """信息获取：读取用户曲库，产出结构化"品味画像"JSON。

        把曲目信息注入 TASTE_ANALYSIS_PROMPT 的 {{user_music_list}} 占位符，
        该提示词为系统固定（锁定/可隐藏），用户不可改。
        未配置 AI 或调用失败 → 返回离线画像，保证流程不中断。
        """
        songs = [
            {
                "歌曲名": t.title,
                "艺术家": t.artist or "未知",
                "风格标签": t.genre or "",
                "情绪标签": "",
                "歌词主题": "",
                "年代": t.year or "",
            }
            # ★ 2026-11-18：从100首减到30首——品味归纳30首足够，省 70% prefill 时间
            #   （实测品味分析占LLM 140s，是导听饿死的主因之一；用户拍板取前30首）。
            for t in tracks[:30]
        ]
        if not songs:
            return self._offline_taste(tracks)
        if not self.llm.enabled:
            return self._offline_taste(tracks)
        user_list = json.dumps(songs, ensure_ascii=False, indent=2)
        prompt = TASTE_ANALYSIS_PROMPT.replace("{{user_music_list}}", user_list)
        extra = (user_config.prompts.get("taste_output") or "").strip()
        if extra:
            prompt = prompt + "\n\n【额外的品味分析要求（用户指定）】\n" + extra
        out = self.llm.chat("", prompt, expect_json=True)
        profile = self._safe_json(out, self._offline_taste(tracks))
        # ★★ 2026-11-18（用户「品味分析顺便推荐5首，省一次LLM调用」）：
        #   把品味分析顺便吐出的「推荐曲目」暂存，供 _do_refresh_taste 完成后
        #   拿去网易云检索验证、填入推荐货架（首曲期间不另起补推荐LLM）。
        try:
            picks = profile.get("推荐曲目") if isinstance(profile, dict) else None
            self._taste_seed_picks = [p for p in (picks or [])
                                      if isinstance(p, dict) and p.get("歌曲名")]
            if self._taste_seed_picks:
                logger.info("[品味] 顺便产出推荐种子 %d 首（待检索验证）",
                            len(self._taste_seed_picks))
        except Exception as e:  # noqa
            logger.debug("解析品味推荐曲目失败：%s", e)
            self._taste_seed_picks = []
        return profile

    def generate_dj_script(self, ctx: dict, web_search: bool | None = None,
                           fast: bool = False, cache_only: bool = False,
                           language: str = "zh") -> dict:
        # ★ 2026-11-19：language="en" 走英文 SYSTEM prompt（12秒口径）。
        #   默认 zh 保持现有行为；等英文 UI 开关就绪后传 language="en" 即生效。
        """
        生成 DJ 口播文案。重点是"实时生成"——每一首歌都基于当下的真实上下文
        （歌曲/天气/地点/时段/心情/用户品味画像/本次推荐理由）现场组织语言，
        而不是套一个固定模板把占位符填进去。

        系统人设用 DJ_SYSTEM（可叠加用户在设置里写的"口播风格指引"作为可选补充）；
        用户侧消息由 _build_broadcast_user 把实时上下文拼成自然语言，交给大模型发挥。

        返回 dict：{"text": 口播文案, "used_ai": 是否真由大模型生成,
                     "error": 失败原因, "error_kind": 错误分类}。
        未配置 AI 或调用失败时 used_ai=False 并回退离线文案，且会返回真实原因。
        """
        # ★ 2026-11-19：语言标记提前算（grounded 分支/离线兜底都要用）
        _en = (str(language or "zh").lower().startswith("en"))
        if not self.llm.enabled:
            off = self._offline_dj(ctx, language)
            return {
                "text": off,
                "display_text": off,
                "used_ai": False,
                "error": "未配置 AI（离线模板模式）",
            }
        # 用户在设置里写的"口播风格要求"（可编辑；留空则用内置默认 BROADCAST_OUTPUT_DEFAULT）
        guidance = (user_config.prompts.get("broadcast_output")
                    or BROADCAST_OUTPUT_DEFAULT).strip()
        # 本地检索上下文：Ollama 后端无内置联网能力、离线模式强制本地向量库时，
        # 由独立 SearchProvider 取资料并注入口播（不污染下一轮：provider 内部 clear_context）。
        # ★ R119-3d：**先取资料、再拼信息块** —— 这样「资料充分度」才能如实反映
        #   "本轮到底有没有外部事实可用"；否则 fact_status 会漏报，模型仍会照旧瞎编。
        search_ctx = self._search_context(ctx, web_search, cache_only=cache_only)
        # The audited 9B profile uses small scoped tasks; other custom prompts retain
        # the existing path. No context-window or model setting is changed here.
        from ai.broadcast_editor import accepts_guidance, generate as grounded_generate
        client = self.llm_fast if fast and self.llm_fast is not None else self.llm
        if (client.backend == 'ollama' and client.model.lower().startswith('qwen3.5:9b')
                and accepts_guidance(guidance)
                and not _en   # ★ 2026-11-19：英文模式绕过 grounded（它的prompt是中文的，不认language）
                and os.environ.get('MUSICAI_GROUNDED_BROADCAST','1') != '0'):
            related = ''
            if bool(self.web_search if web_search is None else web_search):
                try:
                    from ai.search import get_search_provider
                    related = get_search_provider().format_related_context(
                        ctx.get('title',''),ctx.get('artist',''),ctx.get('album',''),
                        allow_live=not cache_only)
                except Exception as e:
                    logger.info('艺人背景检索跳过：%s',type(e).__name__)
            out, evidence = grounded_generate(client,ctx,search_ctx,related)
            if out.strip():
                metadata_only = evidence.get('status') == 'metadata_only'
                return {'text':out,'display_text':strip_tags(out),'used_ai':not metadata_only,
                        'error':'','evidence':evidence,'material_status':evidence.get('status')}
            # Invalid evidence must not fall through into the old creative prompt.
            title=ctx.get('title') or '这首歌'
            return {'text':f'这首歌叫《{title}》。','display_text':f'这首歌叫《{title}》。',
                    'used_ai':False,'error':'未取得有效的口播依据','evidence':evidence,
                    'material_status':'invalid_evidence_selection'}
        # ★ 2026-09-20「环境专项治理」：**在地环境补充检索**。v2 提示词把城市/天气/街巷
        #   定为 D 级素材并规定"只能来自检索到的资料"，但本管线此前从不检索在地信息
        #   ⇒ 模型拿不到任何有据的城市素材 ⇒ 要么编造（判分记作"环境泄漏"）、要么违规
        #   硬塞城市名。补上这一步后，环境内容成为**有据可依的正常素材**。
        env_ctx = self._env_search_context(ctx, web_search, cache_only=cache_only, search_ctx=search_ctx)
        # ★ 条数只在**这一处**算，fact_status 与资料块共用（避免两处不一致，
        #   上一版就是因此蹦出一个字面 "N 条"）。
        _n_fact = len(re.findall(r"(?m)^\[\d+\]", search_ctx)) if search_ctx else 0
        # ★ 2026-11-17：弱来源条数（前缀〔未核实〕，字面与 `search._WEAK_TAG` 一致）。
        #   已知局限：旧版检索缓存里的文本没有这个前缀 ⇒ 计为 0，不误报。
        _n_weak = len(re.findall(r"(?m)^\[\d+\]〔未核实〕", search_ctx)) if search_ctx else 0
        # ★★ 2026-09-21（第三轮）：**年份换算由 app 侧算好**。
        #   实测（probe_llm_search_usage）：把示例「2003 年 ⇒ 二十多年前」写进提示词后，
        #   模型**把示例当模板背了** —— 2018 年的《Lemon》也被写成"二十多年前"。
        #   小模型的算术本就是弱点，少样本示例的照抄倾向反而更强。
        #   ⇒ 从资料里抽出所有 4 位年份，逐条给出"距今 N 年"，并禁止它自己算。
        _years_txt = fact_years_hint(search_ctx)
        if search_ctx and _n_fact <= 0:
            # 旧缓存里的文本可能没有 [N] 编号 ⇒ 至少算 1 条。
            # ⚠ 不加这个兜底的话，下面两处一个印 max(1,·)、一个印 0 ⇒ **数字不一致**。
            _n_fact = 1
        user, mapping = self._build_broadcast_user(
            ctx,
            position=ctx.get("playlist_position"),
            total=ctx.get("playlist_total"),
            next_title=ctx.get("next_title"),
            has_search_facts=bool(search_ctx),
            env_ctx=env_ctx,
            search_facts_n=_n_fact,
            search_facts_weak_n=_n_weak,
            search_ctx=search_ctx,
        )
        from data.api_metadata import prompt_facts
        api_facts = prompt_facts(ctx.get('api_metadata'))
        if api_facts:
            user += ('\n\n【音乐平台版本资料】\n' + api_facts
                     + '\n这些字段仅描述当前平台版本／专辑。不得据此断言歌曲首发、歌词含义、乐器或情绪。'
                     '检索若是另一张专辑／版本，不得混用年份；冲突时省略日期。')
        if search_ctx:
            # ★★ 2026-09-21：资料块**加名字 + 加硬性要求**。
            #   旧写法是一段"许可式"说明（"可据此写…"）⇒ 实测模型基本不用它
            #   （A/B：有资料 0~1 命中 vs 无资料 1~2 命中，等于白搜）。
            #   现在：① 明说"已检索到 N 条"（模型对具体数量有反应）；
            #        ② 后面挂 4 条**义务式**要求，其中第一条是"必须至少用上 1 条"。
            user = (
                user
                + "\n\n【歌曲事实资料（**本轮已检索到 %d 条**，是本轮唯一可用的事实来源）】\n"
                % _n_fact
                + (("\n（注：其中 %d 条标了〔未核实〕——留着用，别当它不存在；"
                    "讲到那几条时带上「据说 / 据传 / 有说法称」这类口气即可。）\n" % _n_weak)
                   if _n_weak else "")
                + search_ctx
                + _years_txt
                + "\n\n【关于上面这份资料的硬性要求（**必须遵守**）】\n"
                  "1. **必须至少用上其中一条具体事实**：发行年代 / 专辑名 / 词曲作者 / "
                  "制作或发行公司 / 所属影视剧或合作者 / 榜单成绩。"
                  "要能被听众听出来这是**这首歌的事实**，不能只用风格词或情绪充数。\n"
                  "2. 说到年份时，**只能照抄【资料里出现的年份】里给出的「距今 N 年」**"
                  "（那是已经算好的）；**不要自己算**，**更不要复用本提示里任何示例中的数字**"
                  "——实测错误：把 2018 年的歌说成「二十多年前」。"
                  "上面没给出年份时，就只写裸年份（如「2018 年的作品」），不要换算。\n"
                  "3. 资料里**没有**的（专辑名、幕后轶事、销量、乐手、榜单名次）一律不写；"
                  "尤其**绝对不要**编造专辑名——资料里没出现专辑就绝不写「来自专辑…／收录于…／出自…专辑」这类句式（那 100% 是编造，宁可不写）；"
                  "不得超出资料范围。\n"
                  "4. 不要罗列来源、不要念链接、不要写 [1][2] 这类编号。\n"
                  "5. **用上事实的同时，总长度仍必须落在第 1 条要求的 45~60 个汉字**"
                  "（事实最多占一半篇幅）——实测：不加这条时，一有资料就会写到 110 字，"
                  "TTS 要 26 秒，顶穿导听窗口。")
        if env_ctx:
            # ★ 2026-09-21：**写明优先级** —— 实测该块是"城市百科/政府网/旅游攻略"这类
            #   与歌无关的通用资料；不给优先级时模型的注意力被它拉走（= 用户说的
            #   "不检索歌曲信息"）。明确"背景素材、每段最多 1 个意象、不得取代歌曲信息"。
            user = (user + "\n\n【在地环境资料（**检索所得**，是环境类内容的唯一出处，"
                          "但**优先级低于上面的歌曲资料**：只作背景点缀，每段口播最多用 1 个"
                          "在地意象，**不得取代或冲淡歌曲本身的信息**（有歌曲事实时，口播以歌曲事实为主体，"
                          "在地意象只作收尾轻点缀）；据此写**具体事物意象**"
                          "（街 / 菜 / 公园 / 物候），**城市名尽可能不写**（让画面自己带出地域感）；"
                          "不得超出、不得与之冲突；没有出处的一律不写城市名/天气词）】\n" + env_ctx)
        # 把用户在设置里写的"口播风格要求"中的 {{占位符}} 也用真实值替换
        # （避免系统提示里残留 {{song_name}} 等字面量，让信息池反映真实上下文）。
        # ★ 2026-11-19：英文模式用英文 SYSTEM，且**不叠加中文可编辑风格**
        #   （那个是中文口径的，会与英文12秒规则打架）。中文模式行为完全不变。
        system = BROADCAST_SYSTEM_EN if _en else BROADCAST_SYSTEM
        if guidance and not _en:
            system = BROADCAST_SYSTEM + "\n\n【内容输出提示词（用户指定）】\n" \
                     + AIBrain.render_template(guidance, mapping)
        # 选模型：本地 Ollama 后端下，转场/短口播可走 3B 快速模型降低延迟
        client = (self.llm_fast if (fast and self.llm_fast is not None)
                  else self.llm)
        out = client.chat(
            system, user,
            use_temp=not client.skip_temperature,
            # web_search 可被调用方按需覆盖（节流）：None 时用类内开关，
            # 否则用传入值（radio 层按 web_search_interval 决定本次是否真正联网）。
            # 说明：ollama 后端忽略内置 web_search（联网由 SearchProvider 完成并注入）。
            web_search=self.web_search if web_search is None else bool(web_search),
            thinking=self.thinking,       # 思考模式：DJ 口播（非 JSON）可先思考再写，文案更细腻
            budget=self._bc_budget,       # ★ task-1：口播思考预算（默认 256，省 ~18s/条）
        )
        # ★★ 2026-11-18 字数硬闸（用户 14:47 最终拍板，勿再改错）：
        #   · 上限 120 字（硬，超→返工）；理想 60~70；70~120 超了但不返工
        #     （字数偏差是 prompt 没说好，靠改 prompt 解决，不靠返工）。
        #   · 下限 30 字（core 口径，去歌名/标签/标点）：**只有明显过低(<30)才返工**，
        #     因为不是所有歌都有很多可写；30~59 字是正常短文案，绝不误返工。
        #     （旧版 MIN_BC=45 会把 30~45 字正常文案也返工，过激，已改 30。）
        #   · 越界 → 追加针对性硬指令**返工 1 次**（旧版超长返工 2 次=74s）。
        #   · 返工后仍越界才兜底：超长→句界精简；过短→接受（prompt 已尽力）。
        MAX_BC = 120
        MIN_BC = 30
        # ★ 2026-11-19：首条就直接吐元指令垃圾（复述prompt/分析）→ 离线兜底，
        #   不进返工（返工只会再吐一份垃圾、白花20s）。
        if is_meta_leak(out or ""):
            logger.warning("[口播] 首条输出为元指令垃圾(%d字)→直接回退离线文案：%s",
                           len((out or "").strip()), (out or "")[:60])
            out = self._offline_dj(ctx, language)
        try:
            from core.text_metrics import core_len as _core_len
            _song4len = str(ctx.get("song_name") or ctx.get("title") or "")
            _artist4len = str(ctx.get("artist") or "")
            _bc_core = _core_len(out or "", _song4len, _artist4len)
        except Exception:  # noqa
            _bc_core = len((out or "").strip())
        if _en:
            # ★ 2026-11-19 英文模式：跳过中文口径的字数闸返工（中文提示词/字数规则
            #   对英文文案无意义，且会把正常的200+字符英文误判超长）。英文靠
            #   BROADCAST_SYSTEM_EN 的"约12秒"约束自管；这里只做宽泛的硬防御。
            _wc = len((out or "").split())
            # ★ LLM 偶尔无视英文SYSTEM、看到中文/日文资料就吐中文正文 ⇒ 强制返工。
            _wrong_lang = is_mostly_cjk(out or "")
            if _wrong_lang:
                logger.warning("[口播] 英文模式但输出为中文/日文(%d字)→强制返工：%s",
                               len((out or "").strip()), (out or "")[:50])
            # ★ 2026-11-19（用户「英文放宽到150单词」）：上限 55→150词。
            _too_long = _wc > 100 or _wrong_lang  # 100词上限（用户12:22：150太长TTS加载不过来）；或语种错
            _too_short = _wc < 8                  # 明显残句才挡
            MIN_BC = 8                            # 供返回判短用
        else:
            _too_long = bool(out.strip()) and len(out.strip()) > MAX_BC
            _too_short = bool(out.strip()) and _bc_core < MIN_BC
        # ★★★ 2026-11-18（用户拍板「超长恢复返工重写，watchdog 45s」）：
        #   智能截断会把 LLM 吐的"英文草稿/素材罗列"从开头截出来当口播
        #   （实测 Home 播成 "Let's start by acknowledging... Fact to use:"）。
        #   恢复旧行为：超长→让 LLM 重写一遍干净正文（返工会丢草稿），watchdog 放宽到45s。
        _attempt = 0
        while (_too_long or _too_short) and _attempt < 1:
            _attempt += 1
            if _too_long:
                if _en:
                    _lang_fix = ("CRITICAL: your previous answer was in Chinese/Japanese. "
                                 "You MUST write the intro entirely in ENGLISH this time — "
                                 "the reference material's language does not matter. ") if is_mostly_cjk(out or "") else ""
                    _hint = (_lang_fix +
                             "Keep the intro to about 12 seconds spoken (30-45 words). "
                             "Keep ONE concrete detail about the song. "
                             "Output only the spoken lines — no analysis, no restating "
                             "these instructions, no 'The prompt says...' meta text.")
                else:
                    _hint = ("你刚才写超了字数。必须砍到 %d 个汉字以内（含标点），"
                             "只保留：1 条歌曲事实 + 至多 1 个在地意象；删掉所有「像极了…」"
                             "这类尾巴铺垫与重复解释。"
                             "★★ 直接输出干净口播正文，绝对不要写任何非正文内容："
                             "不要 Let's start / Fact to use / The prompt says / 目标 50~70 字 / "
                             "字数要求 / 素材罗列 / 分析 / 思考过程 / 引用或复述我给你的要求。"
                             "只输出要念给听众听的那几句话本身。"
                             % MAX_BC)
                logger.info("DJ 口播超长(%d字) → 砍短返工（1/1，硬上限 %d字）",
                            len(out.strip()), MAX_BC)
            else:
                _hint = ("你刚才写得太短了（正文仅 %d 字，听感像没说完）。必须写到"
                         "%d~%d 字：选定这首歌一个具体锚点（编曲细节/创作轶事/歌词意象），"
                         "展开成一两句有画面的话，别只报歌名和风格。"
                         "★★ 补充内容必须来自这首歌本身（专辑/编曲/歌词/歌手），"
                         "严禁添加与这首歌无关的城市、天气、饮食等环境意象——"
                         "写不出就反复听歌的声音白描，也不要拿通用环境凑字数。"
                         % (_bc_core, MIN_BC, 70))
                logger.info("DJ 口播过短(core=%d字) → 展开返工（1/1，下限 %d字）",
                            _bc_core, MIN_BC)
            system_trim = system + "\n\n【补充硬约束】" + _hint
            out = client.chat(system_trim, user,
                              use_temp=not client.skip_temperature,
                              web_search=self.web_search if web_search is None else bool(web_search),
                              thinking=self.thinking,
                              budget=self._bc_budget)
            try:
                _bc_core = _core_len(out or "", _song4len, _artist4len)
            except Exception:  # noqa
                _bc_core = len((out or "").strip())
            _wc_en = len((out or "").split())   # ★ 英文返工后词数（供句界截断判定）
            # ★★★ 2026-11-19（用户「Sgt Pepper 口播念出 The prompt says 字数要求」）：
            #   返工后若吐的是元指令垃圾（复述prompt/分析/素材），**绝不截断播出**
            #   ——截断只会把垃圾前半段念给听众。回退离线文案（正常中文）。
            if is_meta_leak(out or ""):
                logger.warning("[口播] 返工输出为元指令垃圾(%d字)→回退离线文案：%s",
                               len((out or "").strip()), (out or "")[:60])
                out = self._offline_dj(ctx, language)
            elif (_wc_en if _en else len((out or "").strip())) > (100 if _en else MAX_BC):
                # 正常正文只是稍长 → 智能截完整句（不误杀）
                _tt = out.strip()
                if _en:
                    # ★ 2026-11-19：英文按词数截、**只在句末断，绝不切逗号**
                    #   （Sound of Silence 曾被 smart_truncate_sentence 切在逗号处）
                    out = truncate_sentences_en(_tt, max_words=100)
                    logger.info("[口播] 英文返工后超长(%d词)→句界截断 %d 词",
                                _wc_en, len(out.split()))
                else:
                    out = smart_truncate_sentence(_tt, target=MAX_BC, hard_cap=165)
                    logger.info("[口播] 返工后超长(%d字)→智能截完整句 %d 字",
                                len(_tt), len(out.strip()))
        if out.strip():
            # ★ 《》→ <lang> 规范化（模型偏爱书名号，会毁掉 TTS 语种路由）
            # ★ 补漏标：模型常裸写外文专名（「YMO 的 Rydeen」）⇒ TTS 用中文嗓念。
            #   调用点本来就有曲目信息，用完整词边界精确补标签，纯确定性。
            _song = str(ctx.get("song_name") or ctx.get("title") or ctx.get("song") or "")
            _artist = str(ctx.get("artist") or ctx.get("artist_name") or "")
            _album = str(ctx.get("album") or ctx.get("album_name") or "")
            # ★ 2026-11-16：把本曲专名交给 normalize_lang_tags 纠 `<ja>中文</ja>` 错标。
            #   ⚠⚠ **必须先判语种**：日文歌《夜行》(鬼束ちひろ) 的歌名也是纯汉字，
            #   若无条件把本曲名当"中文专名"传进去，会把**正确的** `<ja>夜行</ja>`
            #   剥掉 ⇒ TTS 拿中文嗓念日文名（日文歌实测抓到，单元测试没覆盖真实调用）。
            #   用 infer_lang_hint（歌手含假名 ⇒ ja、资料含 J-POP ⇒ ja）：
            #     · hint 为空（无外语特征，如周杰伦/晴天）⇒ 视为中文专名，允许纠偏；
            #     · hint = ja/ko/ru ⇒ 本曲是外语歌 ⇒ **不传 zh_names**，标签原样保留。
            _hint = AIBrain.infer_lang_hint(
                " ".join(str(v) for v in ctx.values() if isinstance(v, str)),
                _artist, _song)
            _zh_names = () if _hint else (_song, _artist, _album)
            tagged = AIBrain.normalize_lang_tags(
                out.strip(), zh_names=_zh_names)
            tagged = AIBrain.tag_known_names(
                tagged, _song, _artist, _album,
                hint_lang=AIBrain.infer_lang_hint(
                    " ".join(str(v) for v in ctx.values() if isinstance(v, str)),
                    _artist, _song),
            )
            # ★ 最后一道：把**被转写成拉丁**的专名按资料原文还原（有据可依）。
            #   放在补标之后 —— 还原出来的原文由 _SCRIPT_RULES 定标签，与补标同源。
            tagged = AIBrain.restore_names_from_material(
                tagged,
                " ".join(str(v) for v in ctx.values() if isinstance(v, str)),
                _song, _artist)
            # ★★ 2026-11-17：**歌名必现**（确定性兜底）。
            #   实测（12 条 ×2 臂）：提示词写了"歌名必须出现一次"，模型只有 ~58% 遵守；
            #   而歌名是听众去搜这首歌的唯一线索，也是语言标签路由的锚点 ⇒ 缺了必须补。
            #   补法最小侵入：在最前面加「这首<tag>歌名</tag>，」，再用同一套补标收口。
            try:
                _clean_t = re.sub(r"[\(（].*?[\)）]", "", _song).strip()
                _plain = re.sub(r"<[^>]+>", "", tagged)
                _norm = lambda s: re.sub(r"\s+", "", s or "")
                # 正文本身太短（模型偶发返回空/半句）⇒ 不补前缀，交给上层走静态兜底，
                # 否则会产出一条「这首<en>X</en>，」的残句（实测踩到）。
                if (len(_plain.strip()) >= 20 and _clean_t
                        and _norm(_clean_t) not in _norm(_plain)):
                    _t_tagged = AIBrain.tag_known_names(
                        _clean_t, _clean_t, _artist,
                        hint_lang=AIBrain.infer_lang_hint(
                            " ".join(str(v) for v in ctx.values() if isinstance(v, str)),
                            _artist, _song))
                    # 正文已经用「这首 / 这首歌 / 它」起头时，**替换指代**而不是再加一个
                    # "这首X，" —— 否则会出现「这首蓝莲花，这首钢琴改编版…」这种重复。
                    _m = re.match(r"^(这首歌|这首|这一首|它)", tagged)
                    if _m:
                        tagged = _t_tagged + tagged[_m.end():]
                    else:
                        tagged = f"这首{_t_tagged}，" + tagged
                    logger.info("[口播] 歌名缺失 → 已补：%s", _t_tagged)
            except Exception:  # noqa
                pass
            # ★ 2026-11-17 实测清洗：模型偶尔吐 `[RESPONSE]` 这类标记、写出
            #   `<en>Currents>`（少一个 `<`）、`*SOS*`（markdown 星号）——
            #   这些都会被 TTS 念出来或破坏语种路由，确定性修掉。
            try:
                tagged = re.sub(r"\s*\[[A-Z][A-Z_]{2,}\]\s*", "", tagged)   # [RESPONSE] 等
                tagged = re.sub(r"<en>\s*en\s*</en>", "", tagged)           # 空 en 壳
                tagged = re.sub(r"(<([a-z]{2})>)\1+", r"\1", tagged)        # 重复开标签
                tagged = re.sub(r"(</([a-z]{2})>)\1+", r"\1", tagged)       # 重复闭标签
                # 畸形闭标签：`<en>Currents>` ⇒ `<en>Currents</en>`
                tagged = re.sub(
                    r"<([a-z]{2})>([^<>]+)>",
                    lambda m: f"<{m.group(1)}>{m.group(2)}</{m.group(1)}>",
                    tagged)
                tagged = re.sub(r"\*+([^*\n]+)\*+", r"\1", tagged)          # markdown 粗体
                tagged = tagged.replace("**", "").strip()
                # ★ 2026-11-17：残留的单个书名号（`蓝莲花》`）与模型自检残句
                #   （`(28 chars) -> 太短，需补。`）—— 都是会被 TTS 念出来的垃圾。
                tagged = re.sub(r"[《》]", "", tagged)
                tagged = re.sub(r"[（(]\s*\d+\s*(?:chars?|字)[^）)]*[）)]", "", tagged)
                # ★ 2026-11-18：偶现**裸的末尾字数标注**（无括号），如
                #   "…城里的风换了个人。 59 字" —— 模型把字数统计写进正文，TTS 会念出来。
                #   保守：只剥正文**结尾**、紧跟在句末标点后的 `数字+字`，避免误伤正文里
                #   正常的"写了 59 字"之类表述。
                tagged = re.sub(r"(?<=[。！？])\s*\d+\s*字\s*$", "", tagged).strip()
                tagged = re.sub(r"->\s*太短[^\n]*", "", tagged).strip()
                # 闭标签后多一个 `>`（`</en>>`）—— 会被念成"大于号"
                tagged = re.sub(r"(</[a-z]{2})>>+", r"\1>", tagged)
                # ★★ 2026-11-18：砍掉结尾的「无关环境尾巴」。
                #   实测模型给 Queen《Bohemian Rhapsody》写"长春11月的风已凉"、
                #   给田馥甄《小幸运》写"吉林的秋天，落叶刚多些"——与歌无关，
                #   prompt 明禁但模型照犯。这里确定性识别并删除：末句只含
                #   季节/天气/风/街巷/落叶/炖菜等环境词、且不含任何歌曲相关词，
                #   并且删后正文仍 >=30 字才动手（保守，避免误伤/砍空）。
                _env_tail = re.compile(
                    r"[。！？]\s*[^。！？]{0,40}?"
                    r"(?:城里的风|街角|巷口|秋天|落叶|炖菜|\d+\s*月(?:的)?风|风已凉|风往回吹)"
                    r"[^。！？]{0,20}[。！？]?\s*$")
                _songish = re.compile(
                    r"歌|专辑|编曲|副歌|歌词|单曲|现场|版|调|拍|鼓|贝斯|吉他|"
                    r"嗓|唱|旋律|和弦|格莱美|Concept|Remaster|Live|写了|收录|"
                    r"\d{4}\s*年|那年|当年")
                _m_tail = _env_tail.search(tagged)
                if _m_tail and not _songish.search(_m_tail.group(0)):
                    _kept = tagged[:_m_tail.start()].strip()
                    try:
                        if _cl(_kept) >= 30:
                            logger.info("[口播] 已砍无关环境尾巴：%s", _m_tail.group(0).strip())
                            tagged = _kept
                    except Exception:  # noqa
                        pass
                # ★ 2026-11-17：模型偶发返回空/半句（实测 8 条里 3 条是残句）。
                #   清洗后仍太短的判为失败 ⇒ 上层用预制音频兜底，绝不播残句。
                from core.text_metrics import core_len as _cl
                if _cl(tagged) < 20:
                    logger.warning("[口播] 输出过短/残缺（%d 字），判失败：%r",
                                   _cl(tagged), tagged[:40])
                    return {"text": "", "display_text": "", "used_ai": False,
                            "error": "AI 输出过短或残缺"}
            except Exception:  # noqa
                pass
            return {"text": tagged, "display_text": strip_tags(tagged),
                    "used_ai": True, "error": ""}
        # 已配置 AI 但调用失败/返回空：明确告知，不再假装正常
        err = self.llm.last_error or "AI 返回内容为空"
        kind = self.llm.last_error_kind
        if kind == "content_filter":
            friendly = (
                "内容审核被拦截（provider content_filter 判定为高风险）。"
                "这通常是模型方安全网关误伤——可尝试：①在「设置→AI大模型」"
                "换一个模型/供应商（如 gpt-4o-mini）；②在设置里修改口播提示词，"
                "明确声明这是普通音乐电台内容、绝不涉敏。"
            )
            logger.warning("DJ 口播被内容审核拦截，回退离线文案：%s", err)
            off = self._offline_dj(ctx)
            return {"text": off, "display_text": off, "used_ai": False,
                    "error": friendly, "error_kind": kind}
        logger.warning("DJ 口播 AI 生成失败，回退离线文案：%s", err)
        off = self._offline_dj(ctx)
        return {"text": off, "display_text": off, "used_ai": False,
                "error": err, "error_kind": kind}

    # ---------------- 本地检索上下文（Ollama / 离线模式） ----------------
    def _search_context(self, ctx: dict, web_search: bool | None,
                        cache_only: bool = False) -> str:
        """本地检索上下文。Ollama 后端无内置联网；离线模式强制本地向量库。

        返回注入口播的资料文本；无需检索时返回空串。每轮由 provider 内部
        clear_context 保证不污染下一轮（符合「切换必须清理上一轮上下文」约束）。
        """
        a = user_config.ai
        search_cfg = user_config.data.get("search", {})
        mode = (search_cfg.get("mode") or "online").lower()
        want = bool(self.web_search if web_search is None else web_search)
        # ★ cache_only：调用方（第一首）明确"不要新发检索、但可以吃缓存" ⇒ 不放行到这里。
        if not want and not cache_only:
            return ""
        # 开关开启=强制联网检索：用 DuckDuckGo（SearchProvider）抓取实时资料，
        # 注入 DJ 口播上下文。Ollama/本地后端无内置搜索、必须走这里；
        # 即便 API 后端（Kimi 有内置 $web_search）也并行补充 DDG 资料，保证"开了就真搜"。
        try:
            from ai.search import get_search_provider
            provider = get_search_provider()
            q = self._search_query(ctx)
            # ★ R119-25：检索查询强制「歌名 + 歌手」；打日志核对（用户硬约束）。
            logger.info("[检索·查询] 歌名+歌手检索=「%s」(artist=「%s」)",
                        q, ctx.get("artist", "") or "")
            if mode == "offline":
                max_results = int(search_cfg.get("offline_top_k", 5))
            else:
                max_results = int(search_cfg.get("max_results", 8))
            # ★ `cache_only=True` 时只读缓存（`allow_live=False`），不发网络请求。
            # ★ v4：把歌手名显式传下去 —— 筛选层的「作品锚」用它判"这条是不是
            #   在讲这首（这个人的）歌"；不传的话只能从 query 里切，遇上含空格的
            #   歌手名（`Simon & Garfunkel`）会切错。
            return provider.format_context(q, max_results,
                                           allow_live=not cache_only,
                                           artist=ctx.get("artist", "") or "",
                                           title=ctx.get("title", "") or "",
                                           album=ctx.get("album", "") or "")
        except Exception as e:  # noqa
            logger.warning("本地检索失败，跳过：%s", e)
            return ""

    @staticmethod
    def _search_query(ctx: dict) -> str:
        artist = ctx.get("artist", "") or ""
        title = ctx.get("title", "") or ""
        # ★ R119-4v：检索查询**只用「歌名 + 歌手」**，去掉旧版后缀
        #   "歌曲 创作背景 专辑 评价"。实测该后缀会让 Bing 把整条查询错解成
        #   查歌手姓氏的「汉字字典义」（如「周杰伦 晴天 …」→ 返「周」字词条），
        #   检索全崩、口播退化成复读地点/天气。歌名前置还可绕开 Bing 对生僻
        #   首字（如「椎」）的字典怪癖。裸「歌名 歌手」反而返完美歌曲结果
        #   （百度百科/QQ音乐/网易云），其摘要已含创作背景/专辑信息。
        return f"{title} {artist}".strip()

    _PLACE_REDACT = [
        ("长春市", "这座城"), ("长春", "这座城"), ("北国春城", "这座北方之城"),
        ("净月潭", "那片林子"), ("世纪广场", "那片广场"), ("桂林路", "那条老街"),
        ("这有山", "那座商场"), ("伪满", "那处老建筑"), ("雕塑园", "那座雕塑公园"),
        ("南湖", "那片湖"), ("铁锅炖", "热乎的炖菜"), ("锅包肉", "那道酸甜的炸肉"),
        ("早市", "清晨的市集"), ("夜市", "夜晚的市集"),
    ]

    @staticmethod
    def redact_place_names(text: str) -> str:
        """把在地检索里的具体地名/地标脱敏成中性感官描述，从源头移除"照抄地名"的诱惑。
        9B 小模型对显式禁令（绝不/一律不）要么变生硬、要么找同义词绕过（净月潭→北国春城）；
        把地名从输入里拿掉，模型就只能写氛围，自然不生硬。
        """
        if not text:
            return text
        out = text
        for name, sub in sorted(AIBrain._PLACE_REDACT, key=lambda x: -len(x[0])):
            out = out.replace(name, sub)
        return out

    # ★ 2026-09-21（R119-25·方向A）：本词表是「本首歌情绪/意境种子」的来源。
    #   实测 Bing 对 "{城市} 在地 {主题词}" 与 "{城市} 在地 音乐 演出" 返回**逐字节相同**
    #   的三页（旅游攻略/百度百科/政府网），主题词被完全无视——故 env **联网检索本身无法
    #   逐曲不同**，只能提供恒定城市底色。逐曲差异改由本词表从歌曲自身事实(search_ctx)
    #   里挑情绪/意象词作「种子」，喂给 LLM 在恒定底色里挑同频意象（见 _build_broadcast_user
    #   的 env_mood_seed）。⚠ 这是方向A 的核心：联网查不变，变的是歌曲自身的种子。
    _ENV_THEME_LEXICON = [
        "乡愁", "离别", "青春", "爱情", "孤独", "故乡", "旅行", "城市", "夜晚", "回忆",
        "梦想", "治愈", "怀旧", "忧伤", "欢快", "自由", "时光", "大海", "山海", "雨",
        "风", "雪", "花", "月", "星", "思念", "温暖", "成长", "远方", "烟火",
        "黄昏", "清晨", "漂泊", "宁静", "热烈", "浪漫", "沧桑", "安静", "温柔", "故事",
    ]

    @staticmethod
    def _env_theme_hint(ctx: dict, search_ctx: str) -> str:
        """从已检索到的歌曲事实里挑情绪/意象词，作为在地检索的贴题锚点。

        ⚠ **绝不回退到歌名/歌手**：那是 R119-4v 的坑形——把歌名塞进联网查询
        （"歌名 + 后缀"）会让 Bing 把整条查询错解成查歌手姓氏的汉字字典义，检索全崩、
        口播退化成复读地点/天气。而且 env 查询走 `song=False`，筛选层**跳过了
        作品锚/歌词页闸**（search.py:452-474 仅 `song=True` 生效）⇒ 一旦歌名诱出
        歌曲页，它们原样漏进【在地环境资料】，反而把口播污染成"复读歌曲信息"。
        所以无主题词时**返回空串**，让上层落到安全的「城市名作前导锚点」查询
        （实测安全，见 _env_search_context 的 else 分支）。
        """
        text = " ".join([
            search_ctx or "",
            (ctx.get("genre", "") or ""),
            (ctx.get("song_mood", "") or ""),
        ])
        # Listener mood and names are not evidence of a song's own imagery.
        for name in (ctx.get('title'), ctx.get('artist')):
            if name:
                text = text.replace(name, '')
        hits = [w for w in AIBrain._ENV_THEME_LEXICON if len(w) >= 2 and w in text]
        if hits:
            return " ".join(hits[:3])
        return ""

    # ★ 2026-09-23：在地候选池的「意象词 / 噪声词」词表。
    #   意象词加分，噪声词（行政区划描述 / 旅游通稿）重罚。
    _ENV_IMAGERY = (
        "街", "巷", "市场", "夜市", "摊", "小吃", "美食", "咖啡", "酒", "店",
        "公园", "寺", "神社", "桥", "河", "堤", "海", "山", "码头", "车站",
        "戏院", "剧场", "书店", "唱片", "live", "演出",
        "灯", "霓虹", "声", "气味", "光影",
        "樱花", "梧桐", "银杏", "柳", "雪", "雨", "风", "雾", "蝉", "花",
        "老街", "弄堂", "胡同", "广场", "天台", "铁轨", "电车",
    )
    _ENV_NOISE = (
        "行政区划", "邮政编码", "邮递区号", "面积约", "人口", "下辖", "人民政府",
        "官方旅游", "旅游攻略", "门票", "自由行", "几日游", "行程", "酒店预订",
        "航班", "机场", "签证", "免税", "购物攻略",
        # ★ 2026-09-23 补：实测注入里混进大量「地名志 / 普查 / 地名录」类内容，
        #   它们只有街巷名罗列和行政区划描述，**没有任何可感意象**，
        #   却挤占了预算 ⇒ 模型能用的意象就只剩「老街/巷」那几个，反复用。
        "地名普查", "地名录", "地方志", "普查", "领导小组", "简称", "官方编纂",
        "征集与展播", "收录辖区", "地名文化",
    )

    @staticmethod
    def _curate_env_pool(text: str, max_items: int = 10, max_chars: int = 420) -> str:
        """把在地检索结果压成**高密度的意象句子**，而不是整段的百科条目。

        ★★ 为什么必须是「句子级」而不是「条目级」（2026-09-23 用户点破）：
          条目级排序后剩下的仍多是整段百科——实测洛阳洛龙区那条 485 字里全是
          「居洛阳城周围之郊区 / 行政上一直隶属 / 大禹划定九州 / 市辖区、市政府驻地」，
          **没有一句能写进歌**。模型在里面翻不到可用的意象，就只能吐
          「洛龙区适合独处」这类空话。用户原话：
          「还是搜索注入太多了导致它没能找到更好的意象，不然不可能这么贫瘠」。
          ⇒ 所以问题从来不是"给多给少"，是**给的东西本身没营养**。

        做法：拆句 → 逐句打分（意象词加分 / 噪声词重罚 / 只要正分）→ 去重 →
              取 top N 句 → 字数封顶。输出的是可直接入文的短句，不是词条。
        """
        if not text:
            return ""
        sents: list[str] = []
        for line in text.split("\n"):
            line = re.sub(r"^\d+\.\s*", "", line.strip())
            if not line:
                continue
            # 剥掉「标题 —— 日期·」前缀，只留正文（否则句子里塞满站点名与日期，仍是噪声）
            if "——" in line[:60]:
                line = line.split("——", 1)[1].strip()
            line = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日\s*·\s*", "", line)
            line = re.sub(r"^20\d{2}\s*·\s*", "", line)
            if not line:
                continue
            for s in re.split(r"[。！？；;]", line):
                s = s.strip()
                if 8 <= len(s) <= 130:      # 放宽到 130：意象句常带并列地名，容易被 90 截断
                    sents.append(s)
        seen: set[str] = set()
        scored: list[tuple[int, str]] = []
        for s in sents:
            key = s[:18]
            if key in seen:
                continue
            seen.add(key)
            score = sum(1 for w in AIBrain._ENV_IMAGERY if w in s)
            score -= sum(2 for w in AIBrain._ENV_NOISE if w in s)
            if score >= 0:         # 只要不是「明显是噪声」的句子（负分即含行政区划/攻略词）
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        out: list[str] = []
        total = 0
        for _sc, s in scored:
            if len(out) >= max_items or total + len(s) > max_chars:
                break
            out.append(s)
            total += len(s)
        # ★ 兜底：若一句都没挑出来（该城市资料全是噪声），至少给一句最不差的，
        #   免得在地素材整段消失 ⇒ 模型彻底没得用。
        if not out and sents:
            out.append(sents[0][:100])
        return "\n".join("- " + s for s in out)

    @staticmethod
    def _redact_current_loc(text: str, loc: str) -> str:
        """把**本次给定的城市 / 区名**从在地资料里抹掉——**不依赖固定词表**。

        ★ 为什么必须动态（2026-09-23 实测）：
          `_PLACE_REDACT` 是手写词表，只有 **14 条且全是长春的**（长春市/净月潭/桂林路/
          锅包肉/早市…），西安 / 洛阳 / 河内 / 清迈 一个都不在里面 ⇒ 对这些城市
          `redact_place_names` **完全没生效**，城市名原样进了上下文。
          实测西安那份 485 字资料里「西安/碑林」出现 **25 次**，而最高频的意象词
          （街/墙）只有 7 次 ⇒ **城市名词频是单个意象的 3.5~4 倍**，模型当然最容易抓它。
          （用户原话：「为啥选中城市名的概率那么大？而选中其它意象的概率那么小？」）

        ⇒ 所以真正的杠杆在**数据层**：把城市名从素材里拿掉，意象的显著性自然就上来了。
        """
        if not text or not loc:
            return text
        parts = [p.strip() for p in re.split(r"[・（）()\s、,，/]+", loc) if len(p.strip()) >= 2]
        # 长片段优先，避免「西安」被替换后「碑林区」再也匹配不上
        for p in sorted(set(parts), key=len, reverse=True):
            text = text.replace(p, "这座城")
            # ★ 2026-09-23 补漏：资料里的地名常**不带行政后缀**
            #   （loc 是「碑林区」，正文却写「碑林」）⇒ 只替换完整片段会漏，
            #   实测脱敏后仍残留 4 次「碑林」，模型照抄进了口播。
            #   ⇒ 再把去掉后缀的短形式也换掉（街道/区/市/省/县/镇/路/街）。
            for suf in ("街道", "区", "市", "省", "县", "镇", "路", "街"):
                if p.endswith(suf) and len(p) > len(suf) + 1:
                    short = p[: -len(suf)]
                    if len(short) >= 2:
                        text = text.replace(short, "这座城")
        return text

    # 《》→ <lang> 的字符判定表（按优先级）
    _SCRIPT_RULES = (
        ("ja", r"[\u3040-\u30ff\u31f0-\u31ff]"),          # 假名
        ("ko", r"[\uac00-\ud7af\u1100-\u11ff]"),          # 谚文
        ("ru", r"[\u0400-\u04ff]"),                        # 西里尔
        ("en", r"[A-Za-z]"),                                # 拉丁（兜底）
    )
    _CJK_RE = re.compile(r"[\u4e00-\u9fff]")
    # ★ 中文括号不止《》：人工审阅发现模型还会用〈〉「」『』【】包专名
    #   （如 #48「〈FEARLESS〉」），原先只匹配《》⇒ 漏网，TTS 语种路由依旧失效。
    #   左括号 U+300A《 U+3008〈 U+300C「 U+300E『 U+3010【 U+3014〔 U+FF08（
    #   右括号 U+300B》 U+3009〉 U+300D」 U+300F』 U+3011】 U+3015〕 U+FF09）
    _BANG_RE = re.compile(
        r"[\u300a\u3008\u300c\u300e\u3010\u3014\uff08]([^\u300b\u3009\u300d\u300f"
        r"\u3011\u3015\uff09]{1,60})[\u300b\u3009\u300d\u300f\u3011\u3015\uff09]")
    # ★ 裸尖括号：只写 `<Jóga>` / `<Hysteria>` 而**不带语言码**（人工审阅发现的新错误形态）
    #   它不是合法标签，TTS 语种路由同样失效 ⇒ 一并规范化。
    _ANGLE_RE = re.compile(r"<([^<>/]{1,60})>")
    # ★ 已成对的 `<lang>…</lang>`：用于**自相矛盾修正**（见 normalize_lang_tags）
    _PAIR_TAG_RE = re.compile(r"<([a-zA-Z]{2,4})>([^<>]{1,80})</\1>")
    # ★ 2026-11-16：模型会把开口写成书名号 `《en>YUI</en>`（先写《再改成标签），
    #   `_BANG_RE` 要求成对《》⇒ 匹配不上，畸形形态一路漏到 TTS（《 被当字念/路由失败）。
    #   多曲目实测抓到（YUI/償い 那首）。把 `《xx>` 归一成 `<xx>`，再走既有标签逻辑。
    _BANG_OPEN_TAG_RE = re.compile(r"\u300a([a-zA-Z]{2,4})>")

    @classmethod
    def normalize_lang_tags(cls, text: str,
                            zh_names: tuple[str, ...] = ()) -> str:
        """把口播里的书名号《》规范化成语言标签，中文书名号直接去掉。

        模型六轮不改的顽固习惯 ⇒ 在产出后、送 TTS 前兜底处理，保证语种路由不失效。

        ★ 2026-11-16 `zh_names`：修「`<ja>周杰伦</ja>`／`<ja>晴天</ja>`」——
        :790 的 `if lang in _cjk_script: return m.group(0)` 为保住**纯汉字日文歌名**
        （群青日和／夜行）而放行 ja+纯汉字；但中文专名同样纯汉字，于是被误放进 ja
        标签 ⇒ TTS 拿日语嗓按汉字硬读 = 乱码（真实单曲口播实测踩到）。
        不靠内容猜（猜不出"晴天"是中文还是日文），靠**已知元数据**：调用处传入本曲
        歌名/歌手/专辑，只在配对内容与它们**完全相等**且标签为 ja/ko 时剥标签。
        日文歌的 `<ja>夜行</ja>` 因"夜行"不在 zh_names 里 ⇒ 原样保留。
        """
        if not text:
            return text
        # 已知中文专名（去空白）；只在调用方明确给出时启用，空则逐位回到旧行为
        _zh = {str(n).strip() for n in zh_names if n and str(n).strip()}

        def _is_pure_cjk(s: str) -> bool:
            return bool(cls._CJK_RE.search(s)) and not any(
                re.search(p, s) for _l, p in cls._SCRIPT_RULES if _l != "en")

        def _repl(m: "re.Match[str]") -> str:
            inner = m.group(1).strip()
            if not inner:
                return inner
            if _is_pure_cjk(inner):
                return inner                       # 中文专名：直接写字
            for lang, pat in cls._SCRIPT_RULES:
                if re.search(pat, inner):
                    return "<%s>%s</%s>" % (lang, inner, lang)
            return inner

        # ★ 2026-11-16：先修畸形的 `《en>YUI</en>`（开口书名号+语言码）——
        #   必须在 _BANG_RE 之前：`《en>YUI</en>` 的《没有配对的》，_BANG_RE 匹配不到，
        #   会导致 TTS 把《 当字念/语种路由失效。归一成 `<en>YUI</en>` 后走既有逻辑。
        text = cls._BANG_OPEN_TAG_RE.sub(r"<\1>", text)

        text = cls._BANG_RE.sub(_repl, text)

        # 裸尖括号 `<X>`（无对应 `</X>`）⇒ 按字符推断语种后补全成 `<lang>X</lang>`
        def _repl_angle(m: "re.Match[str]") -> str:
            inner = m.group(1).strip()
            if not inner:
                return m.group(0)
            # 已是规范标签（后面跟着同名闭标签）⇒ 原样保留
            if f"</{inner}>" in text:
                return m.group(0)
            if _is_pure_cjk(inner):
                return inner
            for lang, pat in cls._SCRIPT_RULES:
                if re.search(pat, inner):
                    return "<%s>%s</%s>" % (lang, inner, lang)
            return inner

        text = cls._ANGLE_RE.sub(_repl_angle, text)

        # ★★ 2026-09-23：**已成对标签的自相矛盾修正**。
        #   300 题实测：模型写出 `<en>夜行</en>` —— 「夜行」是纯汉字，<en> 会让 TTS
        #   拿英文嗓去念汉字。此前本函数只处理《》与**裸**尖括号，已成对的**不动**
        #   （为了不破坏正确的 <ja>…</ja>），于是这类错标一路漏到 TTS。
        #   判据：内容**只有汉字**（无拉丁 / 假名 / 谚文 / 西里尔 / 阿拉伯），
        #   而标签却是一个**不可能用汉字书写**的语种 ⇒ 标签与内容矛盾，剥掉标签。
        #   ja / ko 必须放行 —— 日语歌名写成纯汉字（群青日和、夜行）是完全正常的。
        _cjk_script = {"ja", "ko", "zh"}
        # 拉丁字母书写的语种：内容是拉丁字母且标了这些 ⇒ 无需改（本就是拉丁系）
        _latin_script = {"en", "de", "fr", "es", "it", "pt", "nl", "sv", "tr",
                         "pl", "cs", "hu", "fi", "da", "no", "vi", "id", "ms",
                         "tl", "ro", "hr", "sk"}

        def _is_hanzi_only(s: str) -> bool:
            return bool(cls._CJK_RE.search(s)) and not re.search(
                r"[A-Za-z0-9぀-ヿ가-힯Ѐ-ӿ؀-ۿ]", s)

        def _repl_pair(m: "re.Match[str]") -> str:
            lang = m.group(1).lower()
            inner = (m.group(2) or "").strip()
            if not inner:
                return m.group(0)
            # ★ 标签必须**服从内容的文字**（TTS 是按标签挑嗓子的）：
            #   300 题实测三类错标 —— `<ja>Superfly</ja>`（拉丁词标 ja）、
            #   `<en>学生街の喫茶店</en>`（含假名却标 en）、`<es>償い</es>`（日语标西语）。
            #   ⇒ 按 _SCRIPT_RULES 判内容的强制语种，标签不服就改。
            _forced = None
            for _l, _p in cls._SCRIPT_RULES:
                if re.search(_p, inner):
                    _forced = _l
                    break
            if _forced == "en":
                # 拉丁内容 ⇒ **走英语**（用户 2026-09-23 拍板："拉丁走英语是 ok 的"）。
                #   用日/韩/俄/中嗓去念 Latin 字母确实别扭；英文嗓念拉丁字母是安全的。
                #   德/法/西/意 等**拉丁系**标签保留（Du Hast 是德语，德语嗓更准）。
                return m.group(0) if lang in _latin_script else "<en>%s</en>" % inner
            if _forced in ("ja", "ko", "ru"):
                return m.group(0) if lang == _forced else "<%s>%s</%s>" % (_forced, inner, _forced)
            # ★ 2026-11-16：已知中文专名被标 ja/ko ⇒ 剥标签（元数据判据，不猜内容）
            if lang in ("ja", "ko") and inner in _zh:
                return inner
            # 纯汉字：ja/ko/zh 放行，其余语种（不可能用汉字书写）剥掉
            if lang in _cjk_script:
                return m.group(0)
            if _is_hanzi_only(inner):
                return inner
            return m.group(0)

        out = cls._PAIR_TAG_RE.sub(_repl_pair, text)
        # ★ 2026-11-17 实测：模型会写 `<zh>罗大佑</zh>` / `<zh>光阴的故事</zh>`，
        #   而契约是"中文专名直接写字、不加标签"。中文嗓本来就能念，多这层标签
        #   只是让 TTS 路由多一次风险 ⇒ 统一剥掉。
        out = re.sub(r"<zh>(.*?)</zh>", r"\1", out, flags=re.S)
        return out

    @classmethod
    def infer_lang_hint(cls, material: str, artist: str = "", song: str = "") -> str:
        """推断「这个专名」该用哪种语言念 —— 专治**纯汉字**专名。

        为什么需要：300 题里最后剩下 10 条漏标**全是纯汉字日文歌名**
        （粉雪 / 愛人 / 星屑の街 / 時の流れに身をまかせ）—— 纯汉字判不出 ja 还是 zh，
        `_SCRIPT_RULES` 只能放弃。但线索有两处：
          ① **歌手名**的字符（鬼束ちひろ / テレサ・テン 含假名 ⇒ ja）
          ② **检索到的资料**里的语种词（日本 / J-POP / 昭和 / 演歌 …）
        """
        for _l, _p in cls._SCRIPT_RULES:            # ① 歌手名直接含特征文字
            if artist and re.search(_p, artist):
                return _l
        for _l, _p in cls._SCRIPT_RULES:            # 歌手无特征时看歌名
            if song and re.search(_p, song):
                return _l
        t = material or ""                          # ② 资料里的语种线索
        if re.search(r"日本|日语|日文|J-?POP|J-?ROCK|昭和|平成|歌謡|演歌|シティポップ|アイドル",
                     t, re.I):
            return "ja"
        if re.search(r"韩国|韩语|K-?POP|가요|한국", t, re.I):
            return "ko"
        if re.search(r"俄罗斯|俄语|苏联|СССР|Союз", t, re.I):
            return "ru"
        return ""

    @classmethod
    def tag_known_names(cls, text: str, *names: str, hint_lang: str = "") -> str:
        """给正文里**没被标签包裹**的已知专名（歌名 / 歌手）补上语言标签。

        为什么需要（300 题实测）：漏标 90/300（30%），其中 **58 条是明显外文专名**
        —— 模型写了「YMO 的 Rydeen」「Base Ball Bear 的 Bubble」却一个标签都不加，
        TTS 于是拿**中文嗓**去念 Rydeen / Bubble。提示词压不动这个习惯。

        做法是**纯确定性**的：调用点本来就有 song / artist，用**完整词边界**
        在正文里精确匹配并补标签，不碰模型。
        安全阀：① 已在 `<lang>…</lang>` 内部的跳过；② 前后必须是非字母数字汉字假名
        （避免 `Sun` 命中 `sunshine`）；③ 纯汉字专名不标（ja/zh 分不出来，标签也会被
        自相矛盾规则剥掉，标了没意义）。
        """
        if not text:
            return text

        def _spans(s: str) -> list:
            return [(m.start(), m.end()) for m in
                    re.finditer(r"<[a-zA-Z]{2,4}>[^<>]{1,80}</[a-zA-Z]{2,4}>", s)]

        spans = _spans(text)
        for name in sorted({n.strip() for n in names if n and n.strip()},
                           key=len, reverse=True):
            lang = None
            for _l, _p in cls._SCRIPT_RULES:      # 先假名/谚文/西里尔，再拉丁兜底
                if re.search(_p, name):
                    lang = _l
                    break
            if not lang:
                # 纯汉字：靠 hint（歌手字符 / 资料语种词）定语言，没有才跳过。
                #   日语歌名写成纯汉字极常见（粉雪 / 群青日和 / 夜行），
                #   而 `<ja>` 包纯汉字**是合法的**（_repl_pair 对 ja/ko/zh 放行）
                #   ⇒ 标上就真的能让 TTS 用日文嗓念。
                lang = hint_lang if hint_lang in ("ja", "ko") else None
                if not lang:
                    continue
            # ★ 词边界要**分情况**（2026-09-23 实测踩坑）：
            #   对拉丁/假名专名用严格边界（避免 `Sun` 命中 `sunshine`）；
            #   但**汉字专名前后常紧挨汉字**（「レミオロメン的粉雪」「聽爱人」），
            #   严格边界会让「的粉雪」整个匹配不上 ⇒ 4 条日文歌名补标失效。
            #   ⇒ 汉字专名只排除字母数字。拉丁专名另需**大小写不敏感**
            #     （正文写 palette 而曲名是 Palette，实测漏 2 条）。
            _flags = re.I if re.search(r"[A-Za-z]", name) else 0
            if re.search(r"[A-Za-z]", name):
                # ★ 2026-11-17 实测：拉丁专名后面常直接跟汉字（「LONGMAN的<en>spiral</en>」），
                #   旧边界把 CJK 也算"不能相邻" ⇒ 整个漏标。拉丁名只该排除**拉丁/数字**相邻
                #   （避免 `Sun` 命中 `sunshine`），汉字相接是正常写法。
                pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(name)
                                 + r"(?![A-Za-z0-9])", _flags)
            elif re.search(r"[\u4e00-\u9fff]", name):
                pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(name)
                                 + r"(?![A-Za-z0-9])", _flags)
            else:
                pat = re.compile(
                    r"(?<![A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff])" + re.escape(name)
                    + r"(?![A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff])", _flags)
            out, last, hit = [], 0, False
            for m in pat.finditer(text):
                if any(a <= m.start() < b for a, b in spans):
                    continue                      # 已在标签内 ⇒ 不动
                out.append(text[last:m.start()])
                out.append("<%s>%s</%s>" % (lang, name, lang))
                last = m.end()
                hit = True
            if hit:
                out.append(text[last:])
                text = "".join(out)
                spans = _spans(text)              # 位置已变，重算保护区间
        return text

    _NONLATIN = None
    _NONLATIN_PAT = r"[\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]"

    @classmethod
    def restore_names_from_material(cls, text: str, material: str, *names: str) -> str:
        """把口播里**被转写成拉丁**的专名，按资料里的原文写法还原。

        为什么需要（2026-09-23 实测）：模型会把日文/俄文专名罗马字化
        （スキマスイッチ → Sukima Switch、六本木心中 → Roppongin Shinju、
        イルカ → Iruka）。提示词禁令能压住大半，但压不到 100%。
        而**检索资料里通常同时有原文和拉丁写法**：
            [1] ロビンソン（Robinson） - スピッツ - 单曲
            歌曲名《ロビンソン》，别名《Robinson》
            AI(あい) / 美空ひばり(みそら ひばり, Misora Hibari)
        ⇒ 有据可查，不必靠猜。**只在"原文完全没出现、而资料给出的拉丁写法出现了"
          时替换**（命中即收手，只替第一处），把误伤面压到最小。
        """
        if not text:
            return text
        mat = material or ""
        for name in sorted({n.strip() for n in names if n and n.strip()},
                           key=len, reverse=True):
            if not re.search(cls._NONLATIN_PAT, name):
                continue                 # 本身是拉丁/纯汉字 ⇒ 无所谓还原
            if name in text:
                continue                 # 原文已在，没被改写
            lat = ""
            for pat in (r"%s\s*[（(]([A-Za-z][A-Za-z0-9\s'\-\.]{2,40})[）)]" % re.escape(name),
                        r"([A-Za-z][A-Za-z0-9\s'\-\.]{2,40})\s*[（(]%s[）)]" % re.escape(name),
                        r"别名[：: ]?[《【(（]?([A-Za-z][A-Za-z0-9\s'\-\.]{2,40})[》】)）]?"):
                m = re.search(pat, mat)
                if m:
                    lat = m.group(1).strip()
                    break
            if not lat:
                continue                 # 资料没给拉丁写法 ⇒ 无从考证，不动
            lang = ""
            for _l, _p in cls._SCRIPT_RULES:
                if re.search(_p, name):
                    lang = _l
                    break
            rep = ("<%s>%s</%s>" % (lang, name, lang)) if lang else name
            # ★ 优先替换**连标签一起包住**的写法（`<en>Sukima Switch</en>`）。
            #   只换内层会得到嵌套标签 `<en><ja>スキマスイッチ</ja></en>`（实测踩到），
            #   那反而更糟 —— 外层 <en> 仍会让 TTS 走英文嗓。
            tagged = re.compile(r"<([a-zA-Z]{2,4})>\s*" + re.escape(lat) + r"\s*</\1>", re.I)
            if tagged.search(text):
                text = tagged.sub(rep, text, count=1)
                continue
            lat_re = re.compile(r"(?<![A-Za-z0-9])" + re.escape(lat) + r"(?![A-Za-z0-9])", re.I)
            if lat_re.search(text):
                text = lat_re.sub(rep, text, count=1)
        return text

    def _env_search_context(self, ctx: dict, web_search: bool | None,
                            cache_only: bool = False, search_ctx: str = "") -> str:
        """城市 / 在地环境补充检索（2026-09-20 用户要求落地）。

        为什么需要：v2 口播提示词把「城市 / 天气 / 街巷 / 地标」定为 D 级素材，并规定
        **只能来自检索到的资料**；但本管线此前从不检索在地信息（local_flavor 恒空）。
        模型拿不到有据的城市素材 ⇒ 要么编造、要么违规硬塞城市名。补上这一步，
        环境内容就变成**有据可依的正常素材**，「环境泄漏」只在"无据"时才成立。

        查询是**恒定的城市底色**（城市名作前导锚点，不像人名/歌名会被 Bing 误解析成
        汉字字典义，R119-4v 的坑只出现在"歌名 歌手 + 后缀"形态，实测安全）。实测证明：无论查询里
        加什么主题/情绪词（"长春 在地 乡愁 离别"），Bing 都返回逐字节相同的三页
        （旅游攻略/百度百科/政府网），主题词被完全无视——env 检索本身**无法逐曲不同**。
        逐曲差异改由「本首歌情绪种子」(env_mood_seed，见 _build_broadcast_user) 驱动 LLM
        在恒定底色里挑同频意象（此为方向A 的核心杠杆）。
        开关：config.search.env_retrieval（默认 True）/ 环境变量 MUSICAI_ENV_SEARCH=0。
        """
        loc = (ctx.get("location", "") or "").strip()
        if not loc:
            return ""
        search_cfg = user_config.data.get("search", {})
        want = bool(self.web_search if web_search is None else web_search)
        if not want:
            return ""
        if os.environ.get("MUSICAI_ENV_SEARCH", "").strip() in ("0", "false", "False"):
            return ""
        if not bool(search_cfg.get("env_retrieval", True)):
            return ""
        try:
            from ai.search import get_search_provider
            provider = get_search_provider()
            # ★★ 2026-09-21：查询**贴题 + 降量**。
            #   旧式 `f"{loc} 城市 地标 街巷 特色 音乐"` 实测返回
            #   「长春市百度百科 + 长春市政府网 + 长春旅游攻略」—— **与音乐无关**，
            #   而且查询里**没有任何歌的信息** ⇒ 每首歌一字不差地注入同一份 427 字，
            #   用户读到的就是"总是检索同一份信息 / 不检索歌曲信息"。
            #   改为在地**音乐场景**向；条数 3 → 2（它只是背景素材，不该占掉歌曲资料的预算）。
            # ★ 2026-09-23：3 → 8。素材太少模型只能照抄给的两三条；
            #   给足候选它才有余地「挑一个与这首歌同频的意象」。
            k = int(search_cfg.get("env_top_k", 8))
            # ★★ 2026-09-21（R119-25 深化）：查询**跟着当前歌的意境走**。
            #   锚点 = 从已检索歌曲事实里挑出的情绪/意象词（每首不同）=>
            #   不再每首返同一份城市攻略。无主题词（离线/无事实）时**不回退歌名**
            #   —— 歌名+后缀是 R119-4v 坑形（Bing 误解析 + song=False 跳过歌曲闸
            #   会漏进歌曲页），直接落到下面 else 的「城市名作前导」安全查询。
            # ★ 2026-09-21（R119-25·方向A）：退役死代码——实测 Bing 对
            #   "{loc} 在地 {主题词} …" 与 "{loc} 在地 音乐 演出…" 返回逐字节相同的
            #   三条结果（长春旅游攻略/百度百科/政府网），主题词被完全无视
            #   （见 _probe_env_risk.py 实时三档对照）。故「按歌意境变查询」无效，
            #   env 检索只提供恒定城市底色；逐曲差异由「本首歌情绪种子」驱动 LLM
            #   在恒定底色里挑同频意象（方向A 核心杠杆，见 _build_broadcast_user）。
            theme = self._env_theme_hint(ctx, search_ctx)
            # ★★ 2026-09-23 重写：多路扇出，给 LLM 一片素材池让它自己挑同频意象，
            #   而不是只丢两三条片段让它照抄。
            #   ① 不再「无主题词就 return ''」——那会让在地资料整段消失；
            #      改为落到城市基础查询（安全形态：城市名作前导锚点）。
            #   ② 三路查询：主题音乐场景 / 街巷生活 / 地标物候，合并去重。
            #   ③ 每路取 per 条，总量仍受 k 约束（默认 8）。
            # ★★ 2026-09-23：**loc 里的「・」会让 Bing 返回 0 条**。
            #   实测对照：查询带「西安・碑林区」⇒ 4 路里 3 路 ctx=0；
            #   换成空格「西安 碑林区」⇒ 8 组查询全部返回 100~222 字。
            #   ⇒ 拼查询前先把分隔符归一成空格（这是"在地素材只剩 60 字"的真因）。
            _lq = re.sub(r"[・·/／（）()、，,]+", " ", loc).strip()
            _qs: list[str] = []
            if theme:
                _t = theme.split()[0]
                _qs.append(f"{_lq} {_t} 音乐 街区")
                _qs.append(f"{_lq} {_t} 老街 生活 声景")
            # 以下角度均为**实测能拿到素材**的组合（带「生活气息 / 市井 / 晨光 / 早市」
            #   这类词才会返回可感内容；只查「地标 / 街巷」回来的全是地名罗列）
            _qs.append(f"{_lq} 老街 巷 生活气息")
            # ★★ 2026-09-23 实测定稿（**体验式**查询词，而非分类标签）：
            #   关键洞察 —— 搜索引擎对「人的体验说法」（游记 / 随笔 / 听歌感想）匹配得
            #   比「分类标签」好，画面恰好藏在叙事文本里。
            #   逐组实测（感官密度 /100 字 · 定义率）：
            #     现状「老街 巷 生活气息」       2.0 · 1.0
            #     标签式「地标 美食 市集 景色」   0.7 · 1.3  ← 定义率最高，最差
            #     「早晨 街头 人们 开始 一天」   2.5 · 2.2  ★两者均衡最好
            #     「空气里 的 味道 香气 飘」     2.4 · 2.6  ★感官密度最高
            #     「走在 街上 看到 听到」        1.7 · 3.1  ★特隆赫姆最高
            #   ⇒ 换成后三路，净 0 成本（替掉原「早市 / 夜市」两路 + 上一版的感官路）。
            _qs.append(f"{_lq} 早晨 街头 人们 开始 一天")
            _qs.append(f"{_lq} 空气里 的 味道 香气 飘")
            _qs.append(f"{_lq} 走在 街上 看到 听到 感受")
            _seen: set[str] = set()
            _qs2 = [q for q in _qs if not (q in _seen or _seen.add(q))]
            _per = max(2, k // max(1, len(_qs2)))
            _parts: list[str] = []
            for _q in _qs2[:4]:      # 3 → 4：多一路感官查询，换更有画面的素材
                try:
                    _t = provider.format_context(
                        _q, _per, allow_live=not cache_only, song=False)
                except Exception as _e:  # noqa
                    logger.warning("在地检索子查询失败，跳过：%s", _e)
                    continue
                if _t:
                    _parts.append(_t)
            if not _parts:
                return ''
            _raw_env = "\n".join(_parts)
            # ★ 去芜存菁：去重 + 意象打分 + 封顶，避免重复百科条目灌爆上下文
            _raw_env = AIBrain._curate_env_pool(_raw_env)
            # ★ 先跑既有（长春）词表，再**动态**抹掉本次给定的城市名，双保险
            _raw_env = AIBrain.redact_place_names(_raw_env)
            return AIBrain._redact_current_loc(_raw_env, loc)
        except Exception as e:  # noqa
            logger.warning("在地环境检索失败，跳过：%s", e)
            return ""

    @staticmethod
    def _build_broadcast_user(ctx: dict, position=None, total=None,
                             next_title=None, has_search_facts: bool = False,
                             env_ctx: str = "",
                             search_facts_n: int = 0,
                             search_facts_weak_n: int = 0,
                             search_ctx: str = "") -> str:
        """把实时上下文渲染成带标签的信息块（交给大模型现场生成口播）。

        严格遵循 BROADCAST_SYSTEM 的契约：天气只给文字（禁止温度数字），
        可选字段（天气/地点/在地灵感/播放契机）为空时不强行填充。
        """
        w = ctx.get("weather_obj") or {}
        weather = w.get("text", "") or ""   # 仅天气文字，不含温度（口播禁止温度数字）
        # 大模型口播降频：本轮不向大模型提供天气，迫使其不提及天气
        # （默认/离线口播仍使用 ctx 里的真实天气，见 _offline_dj）。
        if ctx.get("suppress_weather"):
            weather = ""
        taste = ctx.get("taste_profile") or {}
        if isinstance(taste, dict):
            # 只给模型「品味关键词」这类极简摘要，不要把整份画像 JSON 灌进上下文——
            # 否则模型容易把画像原样回显成「口播」，导致口播面板出现品味分析 JSON。
            kws = taste.get("品味关键词") or []
            if kws:
                taste_text = "、".join(kws)
            else:
                dom = (taste.get("显性偏好") or {}).get("主导风格") or []
                taste_text = "、".join(dom) if dom else "（暂无明确偏好画像）"
        else:
            taste_text = str(taste) if str(taste) else "（暂无明确偏好画像）"
        # 播放契机（可选）：只描述「当前这首歌」在列表里的位置，
        # 绝不含「接住上一首 / 紧接着下一首」这类转场/串场信息——
        # 预告与衔接下一首是「转场」系统的事，广播只讲当前歌，避免口播越界写成串场。
        if position and total:
            if position == 1:
                play_context = "开场第一首，为本次收听定调"
            elif position == total:
                play_context = "本轮收束的一首，做收尾"
            else:
                play_context = "本轮中间的一首，延续当前收听氛围"
        else:
            play_context = ""
        # ★ R119-3d「灌事实上下文」：**资料充分度**——把"哪些字段有、哪些没有"明写给模型。
        #   小模型编造"专辑幕后/创作背景"，多半不是坏，而是**不知道自己缺料**。
        #   写明"未提供"能显著压低虚构率；再配合 BROADCAST_SYSTEM 的【事实纪律】形成闭环。
        _album = (ctx.get("album", "") or "").strip()
        _genre = (ctx.get("genre", "") or "").strip()
        _have = ["歌名 / 歌手"]
        if _album:
            _have.append("专辑")
        if _genre:
            _have.append("流派")
        if has_search_facts:
            # ★ 2026-09-21：从"有这份资料"改成**祈使句**。
            #   实测：只写"已提供：外部检索资料"时，9B 模型产出的口播里
            #   资料事实命中 **0~1 条**（A/B 对照，见 probe_llm_search_usage.py）
            #   —— "可以据此写"这种许可式措辞对它是**无效**的，必须写成义务。
            # ⚠ 上一版这里写成了静态字符串里的**字面 "N 条"**（模型会照抄）——
            #   必须用**真数字**；条数由调用方算一次传进来（见 `_n_fact`）。
            _n = int(search_facts_n or 0)
            # ★★★ 2026-11-17（用户纠正）：**弱来源要如实计数**。旧版只有二值
            #   "有资料/没资料"，而检索侧新增的〔未核实〕档在 fact_status 里等于隐形
            #   ⇒ 模型一边看到资料、一边被告知"未提供创作背景"，最后选择不信资料。
            _nw = int(search_facts_weak_n or 0)
            _weak_note = ("；其中 %d 条**来源未核实**（标了〔未核实〕）——照样能用，"
                          "但要用「据说 / 据传 / 有说法称」这类口气" % _nw) if _nw > 0 else ""
            _have.append(
                "歌曲事实资料（**已检索到 %d 条**，见下方【歌曲事实资料】；"
                "本轮**必须**至少引用其中 1 条具体事实%s）" % (_n, _weak_note) if _n > 0 else
                "歌曲事实资料（见下方【歌曲事实资料】；本轮**必须**至少引用其中 1 条具体事实）")
        if env_ctx:
            _have.append("在地环境资料（见下方【在地环境资料】，以其为准）")
        _lack = []
        if not _album:
            _lack.append("专辑")
        if not _genre:
            _lack.append("流派")
        if not has_search_facts:
            # 本管线从不提供"创作背景/幕后/销量"这类硬事实（除非有检索资料）
            _lack.append("创作背景 / 专辑幕后 / 获奖销量 / 歌词考据")
        if not env_ctx:
            _lack.append("在地环境资料（城市 / 街巷 / 地标）")
        fact_status = "已提供：" + "、".join(_have)
        if _lack:
            fact_status += "；**未提供**：" + "、".join(_lack) \
                           + "——未提供的**一律不得编造**（见【事实纪律】），" \
                             "信息不足时请从流派/情绪/听感落笔。"
        else:
            fact_status += "。仍须遵守【事实纪律】：不得超出资料范围。"
        # ★ 2026-09-21（R119-25·方向A）：本首歌情绪/意境种子——从已检索歌曲事实里
        #   挑出的情绪/意象词（每首不同、且 factual），作为 LLM 在恒定城市底色里
        #   挑同频意象的锚点，让环境描写逐曲不同、跟歌意境走。这是方向A 的核心杠杆：
        #   联网查询变不出不同材料（Bing 无视主题词），逐曲差异只能由歌曲自身驱动。
        env_mood_seed = AIBrain._env_theme_hint(ctx, search_ctx)
        mapping = {
            "song_name": ctx.get("title", ""),
            "artist": ctx.get("artist", "") or "",
            "album": ctx.get("album", "") or "",
            "genre": ctx.get("genre", "") or "",
            "user_taste_profile": taste_text,
            # 用户推荐偏好（关键变量）：DJ 解说会呼应「更熟悉/更陌生」这一设定，
            # 让口播输出真实反映用户配置的推荐偏好。
            "user_reco_pref": ctx.get("familiarity_label", "") or "",
            "recommendation_reason": ctx.get("recommendation_reason", "") or "",
            "location": ctx.get("location", "") or "",
            "weather": weather,
            "time_of_day": ctx.get("time_slot", "") or "",
            # ★ 2026-09-21：日期 / 星期 / 季节（用户要求"除了时间之外还要有日期"）。
            "date": ctx.get("date", "") or "",
            "date_iso": ctx.get("date_iso", "") or "",
            "weekday": ctx.get("weekday", "") or "",
            "season": ctx.get("season", "") or "",
            # ★ 在地环境（检索所得）：此前恒为空。v2 提示词承诺 D 级素材"只能来自
            #   检索到的资料"，但本管线从不检索在地信息 ⇒ 模型只能编造。现由
            #   _env_search_context 补上；两个字段名同物异名，确保两份提示词都绑定。
            "local_inspiration": env_ctx,
            "local_flavor": env_ctx,
            "env_mood_seed": env_mood_seed,
            "play_context": play_context,
            # 最近播放：广播只用于「避免重复推荐同一首/同风格」，
            # 绝不作为「承接上一首」的话术素材——口播只讲当前这首歌，
            # 提及上一首/下一首是「转场」系统的职责。因此此处清空，
            # 从源头杜绝口播越界写成串场（你贴的那段口播正是吃了上一首/下一首信息才写成桥接）。
            # ★ R119-4x：**解开「最近播放」**（用户 2026-09-20 拍板）。
            #   此前恒为空 —— 旧设计要让口播"只讲当前这首"、交接责任全给转场，
            #   于是两者互不依赖、可以并行。改成串行链后，口播要能自然承接最近
            #   几首留下的话头（但**不替转场做收尾串场**）。条数由
            #   `user_config.ai.context_window` 控制（默认 3；0 = 不保留）。
            "recent_context": ctx.get("recent_context", "") or "",
            # ★ R119-3d「灌事实上下文」：把"哪些字段有、哪些没有"**明说给模型**
            #   —— 这是零成本、且直击幻觉的一招：小模型之所以编造"专辑幕后/创作背景"，
            #   往往不是因为坏，而是因为**不知道自己在缺料**。写明"未提供"能显著压低虚构率。
            "fact_status": fact_status,
        }
        return AIBrain.render_template(BROADCAST_TEMPLATE, mapping), mapping

    def recommend_selections(self, ctx: dict, n: int = 5,
                             familiarity: int = 0, taste: dict | None = None,
                             library_titles: list[str] | None = None,
                             disliked_titles: list[str] | None = None,
                             exclude_titles: list[str] | None = None,
                             seed: int | None = None) -> list[dict]:
        """
        AI 第二步（"选中"）：基于品味画像 + 情境 + 风格滑块，主动推荐接下来适合的歌，
        返回 歌名/歌手 列表：[{"title":..., "artist":...}, ...]。

        注意：这里 AI 只"点歌名"，并不保证曲目一定在库/音源里 —— 真正的"检索"
        由 radio._resolve_selections 完成（本地曲库 -> 直链源），检索不到的会被
        安全替换，绝不会抛异常中断推荐流程。

        - familiarity 0~3：控制风格跳跃幅度（贴合品味 -> 开拓耳界），而非「是否局限于曲库」。
        - library_titles：仅当用户拉高熟悉度(>=1)时传入，作为「可命中」的参考上下文；
          不传入时 AI 纯按品味画像/风格推荐，命中曲库或外部音源皆可。
        - disliked_titles：用户明确"不喜欢"的歌名，提示 AI 避雷（不要推荐这些及高度相似的）。
        未启用 AI 或解析失败返回空列表（调用方会退回离线随机）。
        """
        if not self.llm.enabled:
            return []
        lib_titles = library_titles or []
        disliked_titles = disliked_titles or []
        taste = taste or {}
        taste_text = (json.dumps(taste, ensure_ascii=False)
                      if isinstance(taste, dict) else str(taste))
        w = ctx.get("weather_obj") or {}
        weather = w.get("text", ctx.get("weather", "天气不错"))
        # familiarity 0~3 对应「更熟悉 → 更陌生」：低档严格贴合用户已有品味/曲库
        # （听熟悉的），高档跳出舒适区、探索新风格。注意语义：index 越大越「陌生/
        # 探索」，与 RadioConfig.FAMILIARITY_LABELS 顺序一致（0=更熟悉 … 3=更陌生）。
        pref_label = RadioConfig.FAMILIARITY_LABELS[max(0, min(3, familiarity))]
        fam_hint = [
            # 0 更熟悉：严格贴合品味，优先从其曲库/历史里挑 TA 真正熟悉的歌
            "严格贴合用户的品味画像，优先推荐其『曲库（我喜欢的音乐）/ 播放历史』中"
            "TA 真正熟悉、反复听的歌；风格必须与已有偏好一致，不要引入陌生风格。",
            # 1 略熟悉：以品味为主，熟悉的风格内小幅外拓
            "以用户的品味画像为主，在其熟悉的风格半径内小幅外拓；可适度引入同风格的"
            "新歌，但主体仍是其熟悉、常听的类型。",
            # 2 略陌生：适度跳出舒适区
            "在用户品味基础上做适度风格跳跃，引入一些新鲜但不突兀的歌，约一半熟悉、一半探索。",
            # 3 更陌生：大胆探索
            "大胆突破用户既有品味，推荐风格迥异、开拓耳界的歌（系统从外部音源检索）；"
            "少受曲库约束，给 TA 一些惊喜。",
        ][max(0, min(3, familiarity))]
        # 曲库占比上限（与后台 _audit_reco_tracks 审核一致）：更熟悉→更陌生 分别
        # 80% / 60% / 40% / 20%。即「曲库内歌曲」在推荐里占比不得超过该值。
        caps = (0.8, 0.6, 0.4, 0.2)
        # 库内候选作为「可命中」的参考锚点：当用户偏好偏「熟悉」（0/1）时提供，
        # 帮助 AI 优先命中用户真正熟悉的本地曲目；偏「陌生」（2/3）时不锚定，给探索留空间。
        # （修复原 invert：原逻辑用 familiarity>=1 反而把「更熟悉」档排除在锚定之外，
        #  与标签语义相反；现改为 <=1 锚定熟悉档。）
        lib_block = ""
        if familiarity <= 1 and lib_titles:
            shown = lib_titles[:60]
            lib_block = (
                "\n【用户曲库中已有的歌（仅供参考：若推荐的歌在其中会直接命中本地版本；"
                "不在其中时系统会自动从外部音源检索，无需你特意只挑这些）】\n"
                + "\n".join(f"- {t}" for t in shown)
                + ("\n（仅展示前 60 首，其余同理）" if len(lib_titles) > 60 else "")
                + "\n")
        dislike_block = ""
        if disliked_titles:
            shown_d = disliked_titles[:40]
            dislike_block = (
                "\n【用户明确不喜欢的歌（请勿推荐这些，也不要推荐高度相似的）】\n"
                + "\n".join(f"- {t}" for t in shown_d)
                + ("\n（仅展示前 40 首）" if len(disliked_titles) > 40 else "")
                + "\n")
        exclude_block = ""
        if exclude_titles:
            shown_e = exclude_titles[:60]
            exclude_block = (
                "\n【已在待播/已播放，请勿重复推荐这些】\n"
                + "\n".join(f"- {t}" for t in shown_e)
                + ("\n（仅展示前 60 首）" if len(exclude_titles) > 60 else "")
                + "\n")
        # 曲库占比硬约束提示（与后台 _audit_reco_tracks 审核一致）：告知 AI 本次推荐里
        # 「命中曲库/历史的熟悉歌曲」占比上限，让 AI 主动配平曲库歌 vs 探索外部歌，
        # 减少事后裁剪。更熟悉/略熟悉/略陌生/更陌生 分别为 80/60/40/20%。
        cap_ratio = caps[max(0, min(3, familiarity))]
        user = (
            f"用户品味画像：{taste_text}\n"
            # ★ 2026-09-23：地点**不再明写城市名**。
            #   实测（单一城市压力测试 50 首）：口播里的「此刻在西安碑林」绝大多数
            #   是直接抄 user 消息里的地点字段——现成、零成本，所以提示词怎么禁都没用。
            #   把城市名从 user 拿掉后，点名率 32% → **22%**，且字数与标签判对率都不掉。
            #   城市信息改由【在地环境资料】的意象（街巷/地标/物候）承载。
            f"当下情境：{weather} "
            f"{ctx.get('time_slot', '')} 心情：{ctx.get('mood', '') or '随性'}\n\n"
            f"【用户推荐偏好（关键约束 · 必须遵守）】\n"
            f"用户当前设置的推荐偏好级别为「{pref_label}」（0=更熟悉 → 3=更陌生）。\n"
            f"具体要求：{fam_hint}\n"
            f"【占比约束 · 必须遵守】本次推荐中，用户曲库/历史里「熟悉歌曲」的占比"
            f"不得超过约 {int(cap_ratio * 100)}%；其余应是其尚未拥有、需从外部音源探索的歌"
            f"（系统会自动从外部检索，你无需只挑曲库里已有的）。请主动配平，"
            f"不要全部推熟悉的曲库歌，也不要自作主张偏离该偏好级别。\n"
            f"{lib_block}"
            f"{dislike_block}"
            f"{exclude_block}\n"
            f"请推荐接下来最适合播放的 {n} 首歌（songs 数组必须正好 {n} 首），"
            f"输出 JSON 对象，形如 {{\"songs\":[{{\"title\": 歌名, \"artist\": 歌手}}, ...]}}，"
            f"例如 {{\"songs\":[{{\"title\":\"晴天\",\"artist\":\"周杰伦\"}},"
            f"{{\"title\":\"稻香\",\"artist\":\"周杰伦\"}}]}}。不要任何多余文字。"
        )
        # 多样性注入：每轮一个随机种子 + 已推荐/已播清单，打破「每次开播推荐都一样」
        # 的确定性（Ollama 的 JSON 调用默认 temp=0.3 偏贪婪；即便如此，提示词里的随机
        # 种子也会显著改变点歌结果，使不同次启动 / 不同次补货拿到不同歌曲）。
        if seed is not None:
            variety = (
                f"\n（本轮随机种子：{seed}。请在满足上述偏好级别的前提下，"
                f"优先推荐你【本轮尚未推荐过】的歌，不要每轮都点同一批；")
            if exclude_titles:
                shown_s = exclude_titles[:60]
                variety += ("尤其不要重复下面这些已经推荐 / 播放过的歌：\n"
                            + "\n".join(f"- {t}" for t in shown_s) + "\n")
            variety += "）"
            user += variety
        out = self.llm.chat(RECO_PROPOSE_SYSTEM, user, expect_json=True,
                            json_temperature=0.7)
        parsed = self._safe_json(out, [])
        # 鲁棒解析：兼容裸数组 / {"songs":[...]} / 中文键名 / 单首歌 dict 等任意结构
        return self._extract_song_selections(parsed, n)

    # ---------------- 检索优先的选歌（2026-11-17） ----------------
    def pick_from_candidates(self, cands: list, k: int = 1, ctx: dict | None = None,
                             taste: dict | None = None, familiar_label: str = "",
                             current=None, upcoming: list | None = None,
                             familiarity: int = 0, recent: list | None = None,
                             feedback: str = "") -> list[dict]:
        """从**平台检索到的真实候选**里挑 k 首，只回 #ID + 一句理由。

        与 `recommend_selections` 的区别（后者作为降级路径保留）：
          · 候选由 `ai/reco_pool.py` 给出，全部真实可播 ⇒ 结构上不可能编造歌名；
          · 输出是 ID 而不是 title/artist ⇒ 可以直接按 id 造 Track，不用再"按名搜索"。

        - cands：`reco_pool.Cand` 列表，**已按播放列表窗口过滤过**；
        - ctx：`_reco_ctx()`；current/upcoming 只用于让模型知道"上一首是谁"；
        - feedback：上一轮校验失败时的纠错提示（只重试一次）。

        返回 `[{"id": int, "reason": str}]`；模型不可用 / 解析失败返回 []（调用方回退）。
        """
        if not self.llm.enabled or not cands:
            return []
        from ai.reco_pool import split_for_prompt

        familiar, explore = split_for_prompt(cands)
        ctx = ctx or {}
        taste = taste or {}
        # ★ 2026-11-17 实测修正：探索配额原来固定 max(1, k//3)——k=5、更陌生档时只要 1 首，
        #   模型 5 首里 4 首仍选"熟悉"（收藏）。改成按偏好档加权：越"更陌生"配额越高。
        if explore and k >= 2:
            _ratio = (0.3, 0.3, 0.5, 0.6)[max(0, min(3, familiarity))]
            explore_min = max(1, min(len(explore), int(round(k * _ratio))))
        else:
            explore_min = 0
        explore_rule = (
            "至少 %d 首选自「探索」。" % explore_min
            if explore_min else "这次只要 1 首。")
        system = RECO_PICK_SYSTEM.format(k=k, explore_rule=explore_rule)

        def block(header: str, rows: list) -> str:
            if not rows:
                return ""
            return "【%s】\n%s\n" % (header, "\n".join(c.line() for c in rows))

        tags = taste.get("品味关键词") or taste.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        cur = ""
        if current is not None:
            cur = "《%s》%s（%s）" % (
                getattr(current, "title", "") or "",
                getattr(current, "artist", "") or "",
                getattr(current, "genre", "") or "-")
        # 2026-11-17 A/B 实测：只留「正在播(带曲风) + 最近 3 首」就够模型写出具体衔接；
        # 「待播」对挑选没有贡献（重复已由代码预过滤），删掉省 token。
        rec_txt = "、".join("《%s》" % (getattr(t, "title", "") or "")
                           for t in (recent or [])[:3])
        user = (
            "现在 %s·%s ｜ 偏好：%s ｜ 关键词：%s\n"
            "正在播：%s ｜ 最近：%s\n"
            "%s%s"
            "挑 %d 首。"
            % (ctx.get("time_slot", "") or "", ctx.get("weather", "") or "",
               familiar_label or "未指定", "、".join(tags[:4]) or "无",
               cur or "（未开始）", rec_txt or "无",
               block("熟悉", familiar),
               block("探索", explore),
               k)
        )
        if feedback:
            user += "\n上一轮不合格：%s。请只从上面列表中选。" % feedback
        out = self.llm.chat(system, user, expect_json=True, json_temperature=0.4)
        parsed = self._safe_json(out, {})
        picks = parsed.get("picks") if isinstance(parsed, dict) else parsed
        if not isinstance(picks, list):
            return []
        result: list[dict] = []
        for p in picks:
            if isinstance(p, dict):
                try:
                    pid = int(p.get("id"))
                except (TypeError, ValueError):
                    continue
                reason = str(p.get("reason") or "").strip()[:40]
            elif isinstance(p, (int, str)) and str(p).isdigit():
                pid, reason = int(p), ""
            else:
                continue
            result.append({"id": pid, "reason": reason})
            if len(result) >= k:
                break
        return result

    def decide_transition(self, current: dict, nxt: dict,
                           taste: dict | None = None,
                           from_broadcast: str = "") -> dict:
        """决定「当前 → 下一首」的转场类型与短句。

        `from_broadcast`（R119-4x）：上一首的导听口播文本。串行链里它是上一步的产物，
        写进 user 消息让转场能呼应刚刚说过的话头 —— 这就是"串行"换来的连贯性。
        """
        if not self.llm.enabled:
            return {"transition_type": "intro",
                    "reason": "离线默认：下一首做介绍", "script": ""}
        _, slot_cn = time_slot()
        # ★ 2026-11-17：把本地算出的**客观反差**喂给模型（没有数据时这行是空串，行为不变）。
        #   为什么：库里 genre 0/427、mood/bpm 恒空，模型原先只能靠记忆猜，
        #   实测会编出"BPM 从慢速到快速""两首均为 80 年代"。
        try:
            from core import song_facts as _sf
            _facts_line = _sf.contrast_line(current.get("_facts") or {},
                                            nxt.get("_facts") or {})
        except Exception:  # noqa
            _facts_line = ""
        user = (
            f"当前歌曲：{current.get('title')} - {current.get('artist')} "
            f"({current.get('genre')} / 情绪:{current.get('mood','')} / "
            f"年代:{current.get('year','')} / BPM:{current.get('bpm','')})\n"
            f"下一首歌曲：{nxt.get('title')} - {nxt.get('artist')} "
            f"({nxt.get('genre')} / 情绪:{nxt.get('mood','')} / "
            f"年代:{nxt.get('year','')} / BPM:{nxt.get('bpm','')})\n"
            + (f"{_facts_line}\n" if _facts_line else "")
            + (f"刚才那段导听口播（可自然呼应其话头，但**不要复述**）："
               f"{from_broadcast.strip()}\n" if (from_broadcast or "").strip() else "")
            + f"用户喜好画像：{json.dumps(taste or {}, ensure_ascii=False)}\n"
            f"播放上下文：当前时段（{slot_cn}）、用户最近行为、播放列表主题（由导播自行把握）"
        )
        # ★★ R119-26b：**反模板** —— 把本轮已经说过的转场短句喂回去，
        #   明确禁止复用句式（用户反馈"outro 永远都是一样的句式，很腻"）。
        _hist = list(getattr(self, "_recent_transitions", []) or [])[-8:]
        if _hist:
            user += ("\n【本轮最近已说过的转场短句（**严禁**重复它们的句式、开头，"
                     "或只换几个字重写一遍）】\n"
                     + "\n".join("- " + s for s in _hist) + "\n")
        guidance = (user_config.prompts.get("transition_output") or "").strip()
        system = TRANSITION_SYSTEM
        # ★ 2026-11-17 去重保险：旧配置里存着与内置默认**逐字相同**的一份，
        #   若直接追加就变成两份同样的规则（实测 729×2=1458 字，而产出只有 10~25 字）。
        if guidance and guidance != (TRANSITION_OUTPUT_DEFAULT or "").strip():
            system = TRANSITION_SYSTEM + "\n\n【额外的转场风格要求（用户指定）】\n" + guidance
        out = self.llm.chat(system, user, expect_json=True)
        res = self._safe_json(
            out, {"transition_type": "intro", "reason": "", "script": ""})
        # 记入历史（供下一条避开），只留最近 8 条
        try:
            _s = (res.get("script") or "").strip()
            if _s:
                self._recent_transitions.append(_s)
                del self._recent_transitions[:-8]
        except Exception:                                       # noqa
            pass
        return res

    def explain_recommendation(self, track: dict, reason_hint: str) -> str:
        if not self.llm.enabled:
            return reason_hint or "为你精选的一首。"
        system = user_config.prompts.get("reco_explain") or RECO_EXPLAIN_SYSTEM
        user = (
            f"歌曲：{track.get('title')} - {track.get('artist')} ({track.get('genre')})\n"
            f"关联用户偏好：{reason_hint}"
        )
        out = self.llm.chat(system, user, budget=self._trans_budget)
        return out.strip() or reason_hint

    # ---------------- 离线模板 ----------------
    @staticmethod
    def _offline_prefs(history: list[dict]) -> dict:
        genres = [h.get("genre", "") for h in history if h.get("genre")]
        top = max(set(genres), key=genres.count) if genres else "流行"
        return {
            "tags": [top, "常听华语"],
            "summary": f"偏好{top}，多为华语曲目",
            "mood_by_time": {
                "morning": "轻快唤醒",
                "afternoon": "轻松陪伴",
                "night": "安静独处",
            },
        }

    @staticmethod
    def _offline_taste(tracks: list) -> dict:
        genres = [t.genre for t in tracks if getattr(t, "genre", "")]
        top = max(set(genres), key=genres.count) if genres else "流行"
        return {
            "显性偏好": {"主导风格": [top], "情绪氛围": ["随性"],
                         "年代倾向": "不限", "歌词主题": ["生活"]},
            "隐性倾向": {"声音特征": "未分析", "歌手特质": "未分析",
                         "地域文化倾向": "华语为主", "叙事视角": "未分析",
                         "节奏偏好": "未分析"},
            "听歌场景": {"主要场景": "日常陪伴", "氛围时刻": "随性收听"},
            "推荐策略": {"安全区": [top], "拓展区": [], "惊喜区": [], "避雷区": []},
            "品味关键词": [top, "华语", "陪伴"],
        }

    @staticmethod
    def _offline_dj(ctx: dict, language: str = "zh") -> str:
        """离线口播（LLM 不可用时的兜底文案）。

        ★ 2026-11-19：language="en" 时返回英文离线文案（英文模式 AI 失败兜底）。

        ★ 2026-11-17 重写：**旧模板已废**。旧写法是
        「现在傍晚，长春晴，气温 22℃，为你送上《X》— Y，慢慢听就好」——
        它同时踩了本项目口播契约的三条红线：
          1. 出现城市名（契约要求地名意象化、不照抄地名）
          2. 出现**气温数字**（契约明令禁止）
          3. 天气铺陈挤掉歌曲本身的信息
        新模板只讲歌：谁写的、叫什么，一句把人带进歌里就够。离线时宁可短，
        也不要拿环境信息凑字数（实测用户对这类文案的直接评价是「不对劲」）。
        """
        title = (ctx.get("title") or "").strip()
        artist = (ctx.get("artist") or "").strip()
        if str(language or "zh").lower().startswith("en"):
            if title and artist:
                return f"Up next, {title} by {artist}. Let it speak for itself."
            if title:
                return f"Up next, {title}. Let it speak for itself."
            return "Up next. Let it speak for itself."
        if title and artist:
            return f"接下来这首《{title}》，{artist}的。让它自己说。"
        if title:
            return f"接下来这首《{title}》，让它自己说。"
        return "接下来这一首，让它自己说。"
