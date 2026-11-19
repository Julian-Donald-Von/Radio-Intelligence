"""
带标签分段 TTS 管线（协同规则第 3 步）：

LLM 生成带标签文案 → 正则分片 → [(lang, text), ...]
→ 调度器按 provider 分组并行合成（MeloTTS 批次 / VOICEVOX 批次）
→ 按原文顺序拼接（片段间加 80ms 静音，防突兀）
→ 缓存为 intro_{song_id}.wav

本模块只负责「分片」与「拼接」两个纯函数；合成由 TTSManager 调度各引擎完成。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

logger = logging.getLogger("MusicAI.TTS")

# ═══ ★★★ 2026-09-21：语言标签的**归一化 + 健壮解析** ═══
# 为什么重写：旧实现 `_TAG_RE = r"<([a-zA-Z]{2})>(.*?)</\1>"` 有三个真实缺陷 ——
#   ① 只认**恰好两个字母** ⇒ `<jpn>椎名林檎</jpn>`（ISO 639-2）整段不匹配，**连标签字面量
#      一起送进 TTS**（会读成 "jpn椎名林檎jpn"），且 `<jp>` 拿不到 VOICEVOX 快路径；
#   ② 闭合标签靠 `</\1>` 反向引用 ⇒ **大小写必须逐字节相同**，`<JA>…</ja>` 不匹配；
#   ③ 未闭合标签（`<ja>椎名林檎` 到行尾）整段漏成中文叙述。
# 实测依据（考试件 80 题，lang_acc=0.80）：16 条错里 **2 条是"格式没被认出来"**（pred=None），
#   而非判断错 —— 这一块**纯解析**、可零成本救回。
#
# 归一化的目标集合 = `config.TTS_ENGINE` 的键（权威单一来源）：zh/en/ja/es/fr/ko/de(+ru 预留)。
# ⚠ 只做**别名折叠**，**绝不猜语言**：不在表里的码原样保留（由下游 `synthesize(lang=…)`
#   自己决定回退），避免"解析层擅自改判"掩盖真实问题。
_LANG_ALIAS = {
    "ja": "ja", "jp": "ja", "jpn": "ja",
    "en": "en", "eng": "en", "english": "en",
    "zh": "zh", "cn": "zh", "chi": "zh", "zho": "zh", "cmn": "zh", "chs": "zh",
    "ko": "ko", "kr": "ko", "kor": "ko",
    "de": "de", "deu": "de", "ger": "de",
    "es": "es", "spa": "es",
    "fr": "fr", "fra": "fr", "fre": "fr",
    "ru": "ru", "rus": "ru",
}

# 开标签 `<xx>` / 闭标签 `</xx>`：2~3 个字母，或常见英文语言名；**大小写不敏感**。
# ⚠ 码形态要含 **区域后缀**（`zh-CN` / `en-US` / `pt-BR`）—— 只用 `[A-Za-z]{2,3}` 时
#   `<zh-CN>` 根本不是一个 token ⇒ 标签字面量会漏进正文（实测踩到：第一次自测就现形）。
_TAG_TOKEN = re.compile(
    r"</?([A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?"
    r"|english|chinese|japanese|korean|german|spanish|french"
    r"|russian|italian|portuguese)>",
    re.IGNORECASE,
)


# ★ 2026-09-21：模型会**模仿提示词示例里的反引号**，把标签包成 `<zh>晴天</zh>`。
#   实测（`probe_llm_search_usage.py`）两首曲目都出现，且会同 TTS/UI 文本一起落盘。
#   提示词侧已把所有反引号去掉（`ai/prompts.py` 现在 0 个），这里是**防御层**：
#   万一模型仍然吐出来，也不能让它进 TTS。
#   ⚠ 只对**紧贴语言标签**的 Markdown 记号下刀 —— 不动正文普通字符，
#     维持本模块"无标签正文逐位不变"的契约。
_MD_BEFORE_TAG = re.compile(
    r"[`*_~]+\s*(?=</?[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?>)", re.IGNORECASE)
_MD_AFTER_TAG = re.compile(
    r"(</?[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?>)\s*[`*_~]+", re.IGNORECASE)


_PSEUDO_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9_-]{0,29}>")


def _drop_pseudo_tag(m: "re.Match") -> str:
    """形如标签但**不是合法语言码**的 `<...>`（实测出现过 `<Lemon>`）⇒ 删掉。

    ⚠ 只在 `_TAG_TOKEN` **不认**它时才删 —— 合法语言标签一个都不动
      （否则会把自己刚加的标签吃掉，整条链路失效）。
    """
    return m.group(0) if _TAG_TOKEN.fullmatch(m.group(0)) else ""


def clean_markdown(text: str) -> str:
    """归一模型误加的 Markdown 记号与**非法伪标签**；无标签时逐位不变。幂等。

    两件事：
      ① 紧贴语言标签的 Markdown 记号（`` `<ja>原文</ja>` `` → ``<ja>原文</ja>``）；
      ② 形如标签但语言码非法的 `<...>`（如 `<Lemon>`）整段删掉 —— 它不是合法
         token，会**原样留在正文里**进 UI/TTS。
    """
    if not text:
        return text or ""
    out = _MD_AFTER_TAG.sub(r"\1", _MD_BEFORE_TAG.sub("", text))
    return _PSEUDO_TAG.sub(_drop_pseudo_tag, out)


def normalize_lang(code: str) -> str:
    """把模型可能写出的语言码折到 `TTS_ENGINE` 的键；认不出就**原样返回小写**（不猜）。

    `normalize_lang('jpn') -> 'ja'`、`normalize_lang('KR') -> 'ko'`、
    `normalize_lang('zh-CN') -> 'zh'`、`normalize_lang('tlh') -> 'tlh'`（未知不猜）。
    """
    c = str(code or "").strip().lower().replace("_", "-")
    if c in _LANG_ALIAS:
        return _LANG_ALIAS[c]
    if "-" in c:                     # zh-CN / en-US / pt-BR …
        head = c.split("-", 1)[0]
        if head in _LANG_ALIAS:
            return _LANG_ALIAS[head]
    return c


def split_tagged(text: str) -> list[tuple[str, str]]:
    """把带标签的口播文案切成 [(lang, text), ...]（**健壮版**，见上方 `_LANG_ALIAS` 注）。

    规则：
      - 未包裹在标签中的文字视为中文（'zh'）；
      - 语言码经 `normalize_lang` 归一（大小写/别名/三字母 ISO 全吃）；
      - **未闭合**的标签按"到下一个标签或行尾"收尾（旧版直接漏成中文）；
      - 同族闭合才算收尾；不同族闭合忽略；孤立闭合丢弃；
      - 仅保留非空片段。
    例：'下面播放<ja>椎名林檎</ja>的<JPN>丸の内サディスティック</JPN>'
      → [('zh','下面播放'), ('ja','椎名林檎'), ('zh','的'),
         ('ja','丸の内サディスティック')]      # 第二个标签旧版**整段认不出**
    """
    text = (text or "").strip()
    if not text:
        return []
    segments: list[tuple[str, str]] = []
    pos = 0
    open_lang: str | None = None
    buf: list[str] = []

    def flush(lang: str | None, parts: list[str]) -> None:
        inner = "".join(parts).strip()
        if lang is not None and inner:
            segments.append((lang, inner))

    text = clean_markdown(text)          # ★ 先剥掉模型误加的反引号/星号
    for m in _TAG_TOKEN.finditer(text):
        raw = m.group(0)
        closing = raw.startswith("</")
        code = normalize_lang(m.group(1))
        chunk = text[pos:m.start()]
        pos = m.end()
        if open_lang is None:
            pre = chunk.strip()
            if pre:
                segments.append(("zh", pre))
            if not closing:
                open_lang, buf = code, []
            # 孤立闭合标签：丢弃，不产生片段
            continue
        # 已在标签内
        if chunk.strip():
            buf.append(chunk)
        if closing and code == open_lang:
            flush(open_lang, buf)
            open_lang, buf = None, []
        elif not closing:
            # 嵌套/漏闭合的开标签：先把已攒的内容按**上一个**标签收尾，再开新的
            flush(open_lang, buf)
            open_lang, buf = code, []
        # 不同族的闭合标签：忽略（内容继续归当前标签）

    tail = text[pos:]
    if open_lang is not None:
        buf.append(tail)
        flush(open_lang, buf)
    else:
        t = tail.strip()
        if t:
            segments.append(("zh", t))
    return segments


def strip_tags(text: str) -> str:
    """去掉所有语言标签，仅保留原文（用于 UI 显示）。

    ⚠ 只删标签 token、**不重建文本** —— 这样无标签的正文逐位不变（旧实现用
    `_TAG_RE.sub` 也只删标签，行为一致；新版把别名/未闭合/大小写一并覆盖）。
    """
    if not text:
        return ""
    return _TAG_TOKEN.sub("", clean_markdown(text)).strip()


def concat_wavs_with_silence(paths: list, out_path,
                             silence_ms: int = 80,
                             target_sr: int = 24000) -> str | None:
    """把若干 WAV 按序拼接，片段之间插入 `silence_ms` 毫秒静音。

    - 统一重采样到 target_sr（VOICEVOX 24000 与 MeloTTS 44100 混拼需要）。
    - 统一为单声道（口播导听足够；避免声道数不一致导致拼接失败）。
    - 全部失败/为空时返回 None。
    返回最终 WAV 路径。
    """
    valid = [Path(p) for p in paths if p and Path(p).exists()
             and Path(p).stat().st_size > 0]
    if not valid:
        logger.error("[TTS] 拼接失败：无任何有效音频片段")
        return None

    arrays = []
    for p in valid:
        try:
            data, sr = sf.read(str(p), dtype="float32", always_2d=True)
        except Exception as e:  # noqa
            logger.warning("[TTS] 读取音频失败，跳过 %s: %s", p.name, e)
            continue
        # 统一单声道（取均值）
        if data.shape[1] > 1:
            data = data.mean(axis=1, keepdims=True)
        # 重采样到目标采样率
        if sr != target_sr:
            try:
                g = int(np.gcd(int(sr), int(target_sr)))
                up = int(target_sr) // g
                down = int(sr) // g
                # 注意：resample_poly 返回长度可能与 round(len*up/down) 差 1，
                # 不可预分配后赋值（会 broadcast 失败），逐声道重采样后按列堆叠。
                chans = [resample_poly(data[:, c], up, down)
                         for c in range(data.shape[1])]
                data = np.stack(chans, axis=1).astype("float32")
            except Exception as e:  # noqa
                logger.warning("[TTS] 重采样失败，跳过 %s: %s", p.name, e)
                continue
        arrays.append(data)

    if not arrays:
        logger.error("[TTS] 拼接失败：有效音频重采样后为空")
        return None

    silence_samples = int(target_sr * max(0, silence_ms) / 1000.0)
    silence = np.zeros((silence_samples, arrays[0].shape[1]), dtype="float32")

    chunks = []
    for i, arr in enumerate(arrays):
        chunks.append(arr)
        if i != len(arrays) - 1:
            chunks.append(silence)
    out = np.concatenate(chunks, axis=0)
    try:
        sf.write(str(out_path), out, target_sr)
    except Exception as e:  # noqa
        logger.error("[TTS] 写入拼接结果失败: %s", e)
        return None
    return str(out_path)
