# -*- coding: utf-8 -*-
"""直链「到底能播多长」的判据（R114）—— **单一来源，纯函数，无网络**。

## 为什么需要它（实测缺陷，不是推测）

在线曲的直链由音源 API 现场解析。**同一条直链（同一首歌）可能拿到的对象长度不同**：
实测 `netease` 的 `songs_url` 对同一 sid 既可能给完整对象
（`…/jdymusic/obj/…`，`size=4,814,515` / `time=300,840 ms`，文件名 = 声明 md5），
也可能给**降级短对象**（`…/jd-musicrep-ts/…`，`size=321,036` / **20.0 s**）。
（用户观察到的"会员与非会员取到的时长不同"即此。）

缺陷放大器在缓存侧：`ai/radio.py::_resolve_url_cached` 是**读穿缓存**，
写回判据只有 `if url:` —— **只防"没取到"，不防"取到了但被降级"** ⇒ 一条 20 s 的降级直链
被当成好链写进 `track_cache`（`CACHE_TTL["url"] = 6h`）⇒ **这首歌在这 6 小时内每次播都只有 20 s**，
且因为 URL 不变、`sha1(url)` 音频缓存也跟着命中，**连重下都不会发生** ⇒ 表现成
「播几秒就自动切歌、进度条拖不动、但别的歌都正常」。

⇒ 本模块只提供**判据**（不做 IO）。调用方拿判据决定：是否写缓存 / 是否重取。

## 设计原则：**单向宽松 —— 宁可不拦，不可误杀**

两条判据都刻意取得**很保守**，只会**漏判**（漏判由播放侧的自愈兜底），
绝不误杀正常文件：

* `time_ms`（API 自己声明的时长）低于元数据时长的 `MIN_RATIO = 0.5` 才算坏
  —— 真实降级案例是 **0.067（20s/300s）**，离阈值很远；而元数据本身有误差
  （实测有整曲 240 s 被标 300 s 的情况）也不会被误杀。
* `size` 只跟一个**码率下限** `MIN_BYTES_PER_SEC = 8000`（= **64 kbps**）换算出的字节数比
  —— 低于任何正常音乐码率，所以只可能漏判。
* **任何信息缺失 ⇒ 一律 fail-open**（判为 ok）：判据是优化，绝不能因为缺字段就阻断播放。
"""
from __future__ import annotations

# ★ 阈值**单一来源**在 `config/settings.py`（`core/audio_engine.py` 的短音频守卫
#   要用同一个数，而 `core/` 不依赖 `data/` ⇒ 放两边都已 import 的 config 层）。
from config.settings import SHORT_AUDIO_RATIO as MIN_RATIO

#: 「声明时长 / 元数据时长」低于此比例即判降级短链（实测降级案例 = 0.067，余量极大）
__all__ = ["MIN_RATIO", "MIN_BYTES_PER_SEC", "OK", "NO_URL", "DECLARED_SHORT",
           "SIZE_SHORT", "UNKNOWN", "expected_min_bytes", "judge", "is_degraded",
           "describe"]

#: 保守码率下限（字节/秒）= 64 kbps。只用来把时长换成一个"绝不该低于"的字节数。
MIN_BYTES_PER_SEC = 8000

#: 判据结论里的原因码（调用方/断言按它归属，别比对自由文本）
OK = "ok"
NO_URL = "no-url"
DECLARED_SHORT = "declared-short"
SIZE_SHORT = "size-short"
UNKNOWN = "unknown-duration"


def _as_float(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0          # 顺手挡 NaN


def expected_min_bytes(duration_s) -> int:
    """元数据时长 ⇒ **绝不该低于**的字节数（按 64 kbps 下限）。

    时长缺失/非法 ⇒ `0`（= 不设下限，调用方据此 fail-open）。
    """
    d = _as_float(duration_s)
    if d <= 0:
        return 0
    return int(d * MIN_BYTES_PER_SEC)


def judge(info: dict | None, duration_s) -> tuple[bool, str]:
    """判断一份直链信息是否**足量**。返回 `(ok, reason)`；`reason` 取本模块的原因码。

    `info` 形如 `{"url": str, "size": int|None, "time_ms": int|None, "md5": str|None}`
    （见 `MusicSource.resolve_url_info`）。**只读，不改 info。**
    """
    if not info or not info.get("url"):
        return False, NO_URL

    meta_ms = _as_float(duration_s) * 1000.0
    declared_ms = _as_float(info.get("time_ms"))
    size = int(_as_float(info.get("size")))

    # 判据①：API 自己声明的时长明显短于元数据 ⇒ 它自己承认这是短对象
    if meta_ms > 0 and declared_ms > 0 and declared_ms < meta_ms * MIN_RATIO:
        return False, DECLARED_SHORT

    # 判据②：字节数低于"按时长算出的绝对下限" ⇒ 内容必然被截短
    floor = expected_min_bytes(duration_s)
    if floor > 0 and size > 0 and size < floor:
        return False, SIZE_SHORT

    # 信息不全 ⇒ fail-open（判据是优化，不能因为缺字段就阻断播放）
    if meta_ms <= 0 or (declared_ms <= 0 and size <= 0):
        return True, UNKNOWN
    return True, OK


def is_degraded(info: dict | None, duration_s) -> bool:
    """`judge()` 的布尔便捷形式（**只对"明确判坏"返回 True**，fail-open 情形返回 False）。"""
    ok, reason = judge(info, duration_s)
    return (not ok) and reason in (NO_URL, DECLARED_SHORT, SIZE_SHORT)


def describe(info: dict | None, duration_s) -> str:
    """一行可读摘要，**给日志用**（与判据同源，避免日志与判据各写一套数字）。"""
    info = info or {}
    size = _as_float(info.get("size"))
    tms = _as_float(info.get("time_ms"))
    dur = _as_float(duration_s)
    ok, reason = judge(info, dur)
    dec = f"{tms / 1000.0:.1f}s" if tms > 0 else "?"
    sz = f"{int(size)}B" if size > 0 else "?"
    meta = f"{dur:.1f}s" if dur > 0 else "?"
    return (f"{reason}{'' if ok else '(判坏)'} "
            f"declared={sz}/{dec} meta={meta} url={'有' if info.get('url') else '无'}")
