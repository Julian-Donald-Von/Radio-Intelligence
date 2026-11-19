"""
GPU 显存额度划分（本进程只剩 CLAP 在用；TTS 已移出本进程）。

★ 2026-11-17 task-1 语义清理：MeloTTS 已退休，语音合成由**独立 worker 进程**里的
IndexTTS 承担（tools/indextts/worker.py，自带 5.4GB 显存，不受本进程上限约束）。
本进程现在只有 CLAP 音频语义编码在使用 GPU，所以：
  - 本模块设置的 `set_per_process_memory_fraction` 是**本进程天花板**，只约束 CLAP
    及任何偶发的进程内 CUDA 分配；
  - 它**不是**「给 TTS 预留」，也不会限制 IndexTTS（独立进程，各算各的）；
  - 旧键名 `melotts_gpu_fraction` / 常量 `TTS_BUDGET_MB` 保留为兼容别名，新代码不要再用。

历史（保留供追溯）：早先 RTX 4060(8GB) 上分三层划账——Ollama 进程、本进程
（MeloTTS+CLAP）、以及独立进程；MeloTTS 走 GPU 时本进程上限=melotts_gpu_fraction，
其中 clap_gpu_fraction 预留给 CLAP。

注意：本模块必须在「第一次 CUDA 分配」之前调用（main.py 启动最早期）。CLAP 的
load_ckpt() 会触发 CUDA 分配，必须晚于 apply_gpu_budget()。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("MusicAI.GPU")


def apply_gpu_budget() -> None:
    """给本进程（当前只剩 CLAP）划显存上限，避免与 Ollama/水体抢满整卡。

    须在第一次 CUDA 分配前调用。若本机无 CUDA 或 torch 不可用则安全跳过。
    """
    try:
        import torch
    except Exception:  # noqa
        return
    if not torch.cuda.is_available():
        logger.info("[GPU] 无可用 CUDA，跳过显存额度划分（CLAP 将走 CPU）。")
        return
    # 取配置：新键 process_gpu_fraction 优先，旧键 melotts_gpu_fraction 兼容
    try:
        from config.settings import user_config
        _raw = user_config.gpu.get("process_gpu_fraction")
        if _raw is None:
            _raw = user_config.gpu.get("melotts_gpu_fraction", 0.30)
        frac = float(_raw)
    except Exception:  # noqa
        frac = 0.30
    frac = max(0.05, min(0.95, frac))
    # CLAP 子预算：仅语义记账（钳制在 [0, frac]，不能超出本进程总额度）
    clap_frac = clap_gpu_fraction()
    clap_frac = max(0.0, min(clap_frac, frac))
    try:
        torch.cuda.set_per_process_memory_fraction(frac)
        logger.info(
            "[GPU] 本进程显存上限设为整卡 %.0f%%（约 %.0f MiB）：CLAP 预留 %.0f%%；"
            "IndexTTS 走独立 worker，不占本进程预算（MeloTTS 已退休）",
            frac * 100, frac * 8192, clap_frac * 100,
        )
    except Exception as e:  # noqa
        logger.warning("[GPU] 设置本进程显存上限失败（不影响运行）：%s", e)


def clap_gpu_fraction() -> float:
    """CLAP 音频编码的显存份额（占整卡），默认 5%，钳制 [0, 0.5]。

    仅作语义记账：真正的进程上限由 apply_gpu_budget() 一次性设置。
    """
    try:
        from config.settings import user_config
        f = float(user_config.gpu.get("clap_gpu_fraction", 0.05))
    except Exception:  # noqa
        f = 0.05
    return max(0.0, min(0.5, f))


def ollama_max_vram_bytes() -> int:
    """计算应写入 OLLAMA_MAX_VRAM 的字节数（由 start_app.py 调用）。

    优先级：环境变量 MUSICAI_OLLAMA_MAX_VRAM_MB > 运行时配置 gpu.ollama_max_vram_mb
    > 默认 5500 MiB。返回 0 表示不限制。
    """
    mb = 5500
    # 1) 运行时配置（data_runtime/config.json 的 gpu.ollama_max_vram_mb）
    try:
        from config.settings import user_config
        mb = int(float(user_config.gpu.get("ollama_max_vram_mb", mb)))
    except Exception:  # noqa
        pass
    # 2) 环境变量覆盖（最高优先）
    env = (os_environ().get("MUSICAI_OLLAMA_MAX_VRAM_MB") or "").strip()
    if env:
        try:
            mb = int(float(env))
        except Exception:  # noqa
            pass
    if mb <= 0:
        return 0
    return int(mb) * 1024 * 1024


def os_environ():
    import os
    return os.environ
