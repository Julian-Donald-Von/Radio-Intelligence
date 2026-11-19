"""TTS 音频后处理链（ffmpeg）——各引擎共享。

从 `core/tts_melotts.py` 抽出（2026-11-17，IndexTTS 接入时需要同一条链）：
形态固定为 highshelf×3 + acompressor + aecho(可选) + loudnorm，
数值由设置页 7 个滑块驱动（pp_hs1/2/3、pp_comp_th/ratio、pp_echo、pp_loud_i）。

⚠ 注：用户原始意图用 `aequalizer`（t=h 高架），但 ffmpeg 9.x 起已移除该滤镜，
其「高架」语义由 `highshelf` 等价替代（width_type=h，width 以 Hz 计），音效一致。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("MusicAI.TTS")

# 三段高架中心频率/带宽（Hz，固定）
PP_HS1_F, PP_HS2_F, PP_HS3_F = 120, 250, 3000
PP_HS1_W, PP_HS2_W, PP_HS3_W = 200, 300, 1000
# 回声与响度（固定部分）
PP_ECHO_DELAY, PP_ECHO_DECAY = 45, 0.25
PP_LOUD_TP, PP_LOUD_LRA = -1.5, 11

DEFAULTS = {
    "hs1": 3.0, "hs2": -0.5, "hs3": 2.0,
    "comp_th": -23.0, "comp_ratio": 3.5,
    "echo": 0.0, "loud_i": -17.0,
}


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def params_from_config(tts_cfg) -> dict:
    """从 `user_config.tts` 读 7 个滑块（缺省用 DEFAULTS）。"""
    g = (tts_cfg or {}).get
    return {
        "hs1": _clamp(g("melotts_pp_hs1", DEFAULTS["hs1"]), -12.0, 12.0),
        "hs2": _clamp(g("melotts_pp_hs2", DEFAULTS["hs2"]), -12.0, 12.0),
        "hs3": _clamp(g("melotts_pp_hs3", DEFAULTS["hs3"]), -12.0, 12.0),
        "comp_th": _clamp(g("melotts_pp_comp_th", DEFAULTS["comp_th"]), -40.0, 0.0),
        "comp_ratio": _clamp(g("melotts_pp_comp_ratio", DEFAULTS["comp_ratio"]), 1.0, 10.0),
        "echo": _clamp(g("melotts_pp_echo", DEFAULTS["echo"]), 0.0, 1.0),
        "loud_i": _clamp(g("melotts_pp_loud_i", DEFAULTS["loud_i"]), -24.0, -10.0),
    }


def build_filter(p: dict) -> str:
    """按参数拼 ffmpeg -af 滤镜链。"""
    hs1 = _clamp(p.get("hs1", DEFAULTS["hs1"]), -12.0, 12.0)
    hs2 = _clamp(p.get("hs2", DEFAULTS["hs2"]), -12.0, 12.0)
    hs3 = _clamp(p.get("hs3", DEFAULTS["hs3"]), -12.0, 12.0)
    comp_th = _clamp(p.get("comp_th", DEFAULTS["comp_th"]), -40.0, 0.0)
    comp_ratio = _clamp(p.get("comp_ratio", DEFAULTS["comp_ratio"]), 1.0, 10.0)
    echo = _clamp(p.get("echo", DEFAULTS["echo"]), 0.0, 1.0)
    loud_i = _clamp(p.get("loud_i", DEFAULTS["loud_i"]), -24.0, -10.0)
    echo_part = ""
    if echo > 0.001:
        echo_part = (f"aecho={echo:.2f}:0.4:{PP_ECHO_DELAY}:"
                     f"{PP_ECHO_DECAY:.2f},")
    return (
        f"highshelf=f={PP_HS1_F}:width_type=h:width={PP_HS1_W}:g={hs1:.1f},"
        f"highshelf=f={PP_HS2_F}:width_type=h:width={PP_HS2_W}:g={hs2:.1f},"
        f"highshelf=f={PP_HS3_F}:width_type=h:width={PP_HS3_W}:g={hs3:.1f},"
        f"acompressor=threshold={comp_th:.0f}dB:ratio={comp_ratio:.1f}:"
        f"attack=2:release=100,"
        f"{echo_part}"
        f"loudnorm=I={loud_i:.0f}:TP={PP_LOUD_TP}:LRA={PP_LOUD_LRA}"
    )


def resolve_ffmpeg(configured: str = "") -> str | None:
    """解析 ffmpeg：显式配置 → 项目内置 → imageio-ffmpeg → 系统 PATH。"""
    cands: list[str] = []
    if configured:
        cands.append(configured)
    try:
        from config.settings import FFMPEG_BIN
        if FFMPEG_BIN and os.path.isfile(str(FFMPEG_BIN)):
            cands.append(str(FFMPEG_BIN))
    except Exception:  # noqa
        pass
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa
        pass
    cands.append("ffmpeg")
    for c in cands:
        if not c:
            continue
        if os.path.isfile(c) or shutil.which(c):
            return c
    return None


def apply(in_path, params: dict | None = None, *, ffmpeg: str | None = None,
          configured_ffmpeg: str = "", timeout: int = 90) -> bool:
    """就地跑一遍后处理（成功后覆盖原文件）。失败保留原音频并返回 False。"""
    p = Path(in_path)
    if not p.is_file():
        return False
    ff = ffmpeg or resolve_ffmpeg(configured_ffmpeg)
    if not ff:
        logger.warning("[TTS:post] 未找到 ffmpeg，跳过后处理（保留原始合成）")
        return False
    af = build_filter(params or DEFAULTS)
    out = p.with_suffix(".pp.wav")
    # ⚠ ffmpeg 的 loudnorm 内部会把音频升采样到 192kHz 并**保持输出**，
    #   不锁采样率会让 4 秒口播变成 1.5MB 的 192k 文件（实测）。这里按输入采样率锁回去。
    src_sr = None
    try:
        import soundfile as sf
        src_sr = int(sf.info(str(p)).samplerate)
    except Exception:  # noqa
        src_sr = None
    cmd = [ff, "-hide_banner", "-y", "-i", str(p), "-af", af]
    if src_sr:
        cmd += ["-ar", str(src_sr)]
    cmd.append(str(out))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        if out.exists() and out.stat().st_size > 0:
            os.replace(out, p)
            return True
        return False
    except Exception as e:  # noqa
        logger.warning("[TTS:post] ffmpeg 后处理失败，保留原始合成: %s", e)
        try:
            if out.exists():
                out.unlink()
        except Exception:
            pass
        return False
