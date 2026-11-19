"""llama.cpp server 的**进程生命周期**管理（2026-11-16）。

为什么从 .bat 移到 Python：
  .bat 里用 PowerShell Start-Process -WindowStyle Hidden + Stop-Process -Force，
  真机双击后出现"闪退 + bat 文件自身消失"（根因未完全定位，但复杂 bat 命令
  是唯一变量）。改成 Python 直接 subprocess.Popen + atexit 清理：
    · 无 PowerShell 中间层、无 -WindowStyle Hidden、无 bat 拼写/引号问题；
    · atexit 保证正常退出/异常退出都关掉模型进程，不留孤儿 21GB；
    · 进程句柄在内存里，不依赖 pid 文件，杜绝误删。

只在本进程内管理；不读/写用户 config.json。开关走环境变量
`MUSICAI_AI_BACKEND=llamacpp`（由 start_api_host.py 的运行时钩子设置）。
"""
from __future__ import annotations

import atexit
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("MusicAI.LlamaCppMgr")

_PROC = None
_LOG_FH = None
_OWNED = False


def _exe_path() -> Path:
    return Path(os.getenv(
        "MUSICAI_LLAMACPP_EXE",
        r"C:\Users\DonaldVon\AppData\Local\Programs\Ollama\lib\ollama\llama-server.exe"))


def _model_path() -> Path:
    # ★★ 2026-11-18 用户拍板：项目只用 Qwen3.6 35B A3B 一个 LLM，
    #   AREX-Turbo 完全丢弃（不再默认选它）。35B 21G 装不进 8GB 卡 ⇒
    #   用 --fit 部分 offload（见 start()：fit-target 2800，实测 ~29.9 t/s）。
    #   可用 MUSICAI_LLAMACPP_MODEL 显式覆盖。
    return Path(os.getenv(
        "MUSICAI_LLAMACPP_MODEL",
        r"D:\.ollama\blobs\sha256-d372de8e934898a59e6ccfabc3368474711384d8f1fd4d22d87a3f0a45400cdc"))


def _port() -> int:
    try:
        return int(os.getenv("MUSICAI_LLAMACPP_PORT", "8080"))
    except (TypeError, ValueError):
        return 8080


def _ctx() -> int:
    # ★★ 2026-11-18 P0：8192→16384。真机 llamacpp_stdout.log 有 **62 次**
    #   `request (9902~9943 tokens) exceeds the available context size (8192)` ⇒
    #   HTTP 400 ⇒ 口播直接失败（app_auto.log 多处 `llamacpp 调用失败 HTTP Error 400`）。
    #   口播 prompt（含播放列表 + 历史上下文）稳定 ~9900 tokens，8192 必拒。
    #   16384 留足余量；KV f16 下 656MiB→1.3GiB，但模型层本就在 CPU、显存有余量
    #   （实测 fit 后模型仅占 ~4.7GB/8GB）。需要更省可配 MUSICAI_LLAMACPP_KV_TYPE=q8_0
    #   （16384 时 KV ~656MiB，等于原 8192 f16）。
    try:
        return int(os.getenv("MUSICAI_LLAMACPP_NUM_CTX", "16384"))
    except (TypeError, ValueError):
        return 16384


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def health_ok(port: int | None = None) -> bool:
    import urllib.request
    p = port or _port()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{p}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _listener(port: int | None = None) -> tuple[int, str]:
    """返回监听该端口的 (pid, cmdline)；取不到时 (0, "")。

    ★ 用 psutil 读**真实 cmdline**，不猜：只有拿到命令才能判断那个实例是不是
      按本项目配置起的（见 `_is_our_config`）。psutil 缺失 ⇒ 返回 (0, "")。
    """
    try:
        import psutil
    except Exception:                                       # noqa
        return 0, ""
    p = port or _port()
    try:
        for c in psutil.net_connections(kind="tcp"):
            try:
                if not c.laddr or c.laddr.port != p or c.status != "LISTEN":
                    continue
                return c.pid, " ".join(psutil.Process(c.pid).cmdline() or [])
            except Exception:                               # noqa 进程已退出/无权限
                continue
    except Exception as e:                                  # noqa
        logger.debug("[llamacpp] 枚举连接失败：%s", e)
    return 0, ""


def _is_our_config(cmd: str) -> bool:
    """该实例是否是**本管理器**拉起的。

    ★★ 2026-11-18 同步架构：签名随模式变——
      · 常驻模式（MUSICAI_CORESIDENT!=0）：cmd 含 `-ngl 99`，签名 = `-ngl`
      · 旧分时模式：cmd 含 `--fit-target`，签名 = `--fit-target`
    ⚠ 必须与 start() 的 cmd 构造保持一致，否则新实例被判"非本配置"→
      被 _reclaim 定点回收 → 无限重启。取不到 cmdline 时判 True 不误杀。
    """
    if not cmd:
        return True
    _coresident = os.getenv("MUSICAI_CORESIDENT", "1") != "0"
    sig = "-ngl" if _coresident else "--fit-target"
    return sig in cmd


def _reclaim(pid: int, cmd: str) -> bool:
    """定点回收"配置不对"的 llama-server 实例。

    ⚠ 安全闸：**只回收 cmdline 含 `llama-server` 的进程**，绝不误杀别的占用者。
    """
    if not pid or "llama-server" not in (cmd or "").lower():
        logger.warning("[llamacpp] 不回收（pid=%s，cmd=%s）", pid, (cmd or "")[:80])
        return False
    try:
        import psutil
        pr = psutil.Process(pid)
    except Exception as e:                                  # noqa
        logger.warning("[llamacpp] 回收失败（%s）", e)
        return False
    try:
        pr.terminate()
        try:
            pr.wait(timeout=8)
        except Exception:                                   # noqa
            pr.kill()
            try:
                pr.wait(timeout=5)
            except Exception:                               # noqa
                pass
        logger.info("[llamacpp] 已回收 pid=%s", pid)
        return True
    except Exception as e:                                  # noqa
        logger.warning("[llamacpp] 回收 pid=%s 失败：%s", pid, e)
        return False


def start(wait_s: float = 150.0) -> bool:
    """拉起 llama.cpp server 并等就绪。返回 True=可用。"""
    global _PROC, _LOG_FH, _OWNED
    if health_ok():
        # ★★★ 2026-11-17：**健康 ≠ 可用**。真机取证：8080 上跑着一个遗留的实验进程
        #   `llama-server.exe ... -ngl 99 --no-warmup`（启动时没注 CUDA 后端 DLL）⇒
        #   /health 200 一切正常，但 llama.cpp 打印 "no usable GPU found" 后**纯 CPU**
        #   推理：实测 572 token 花了 60.3s（~9.5 tok/s），显存只占 722MB；而管理器
        #   拉起的 `--fit on --fit-target 4600` 实例短生成 4.9s。
        #   旧逻辑"health_ok 就复用不接管"会把这种实例**永久收养**，用户从此一直用
        #   CPU 版大模型（=当初"口播 140 秒"的复发路径，且重启应用也治不好）。
        #   ⇒ 复用前先核对**配置签名**：含 `--fit-target` 才是本管理器拉起的。
        #     不是 ⇒ 定点回收（只回收 cmdline 含 llama-server 的进程），再用本配置拉起。
        _pid, _cmd = _listener(port=_port())
        if _is_our_config(_cmd):
            logger.info("[llamacpp] 端口 %d 已有**本项目配置**的健康服务（pid=%s），复用不接管",
                        _port(), _pid or "?")
            return True
        logger.warning("[llamacpp] 端口 %d 上的健康服务**不是本项目配置**（pid=%s）：%s",
                       _port(), _pid or "?", (_cmd or "取不到 cmdline")[:160])
        # ★ 2026-11-18：常驻模式本身就用 `-ngl 99`（全 GPU），签名判定已按模式区分，
        #   这里只在"非本配置"时提示。历史病因仍是"未注入 CUDA DLL 的遗留进程"。
        logger.warning("[llamacpp] 典型病因：遗留进程未注入 CUDA 后端 DLL "
                       "⇒ /health 正常但纯 CPU 推理（实测 9.5 tok/s，GPU 版快 5~10 倍）")
        if _reclaim(_pid, _cmd):
            logger.warning("[llamacpp] 已回收该实例 → 改用本项目配置重新拉起")
        else:
            logger.warning("[llamacpp] 回收失败/无法判定，暂按现有实例复用（口播可能很慢）")
            return True
    exe, model = _exe_path(), _model_path()
    if not exe.is_file() or not model.is_file():
        logger.warning("[llamacpp] 路径缺失（exe=%s model=%s），跳过", exe, model)
        return False
    logdir = Path(__file__).resolve().parent.parent / "data_runtime"
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        _LOG_FH = open(logdir / "llamacpp_stdout.log", "ab", buffering=0)
    except Exception as e:  # noqa
        logger.warning("[llamacpp] 日志打开失败（继续）: %s", e)
        _LOG_FH = subprocess.DEVNULL
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    # ⚠⚠ 2026-11-16 **纠正此前误判**：llama-server.exe **支持 GPU**，但需要显式
    #   注入 CUDA 后端 DLL（见下方 GGML_BACKEND_PATH）。此前报 "no usable GPU found"
    #   是因为直连时没设该环境变量——Ollama 官方启动会自动注入，我们漏了 ⇒
    #   35B 纯 CPU ⇒ 口播 140 秒。补上后实测短生成 **8.1s → 3.1s（2.6×）**。
    #   显存策略见下方 --fit-target（margin 语义：留多少空闲给水体/CLAP）。
    #   MeloTTS 也走 GPU（tts.melotts_gpu=true，热合成 CPU 1.4s → GPU 0.4s），
    #   本进程 GPU 预算 15%（core/gpu_budget.py）保证不挤占水体。
    # ★★ 2026-11-18 实测纠正（task-1）：`--fit-target` 是**每卡要留出的空闲显存**
    #   （llama-server --help: "target margin per device"），不是"给模型用多少"！
    #   旧值 4600 = 启动时留 4.6GB 空闲 ⇒ 模型只敢用 ~3.0GB（28 t/s）；
    #   App 运行时水体和 CLAP 已占 ~1.5GB，可用更少 ⇒ 模型只用到 ~2GB（13 t/s）。
    #   实测（q4 KV、6 首审计）：margin 4600→3.0GB/28.0t/s；3400→4.2GB/29.5；
    #   2800→4.7GB/29.9；2400→5.4GB/30.1；5600→2.0GB/11.5（更差）。
    #   取 2800：既把模型抬到 ~4.7GB（+7% 单测、App 内预期更多），又给水体/CLAP
    #   留 ~2.8GB 余量。可用 MUSICAI_LLAMACPP_FIT_MB 覆盖。
    try:
        _fit_mb = int(os.getenv("MUSICAI_LLAMACPP_FIT_MB", "2800"))
    except (TypeError, ValueError):
        _fit_mb = 2800
    # ★★ 2026-11-18 用户拍板：项目只用 35B（21G），8GB 卡装不下 ⇒
    #   无论是否常驻模式都必须用 --fit 部分 offload（fit-target 2800 实测 ~29.9 t/s）。
    #   旧逻辑"常驻=-ngl99"是给 AREX Q4 的（已丢弃），不能套到 35B。
    #   TTS 走独立 llama-tts.exe 按需合成，与 LLM 分时共存。
    _coresident = os.getenv("MUSICAI_CORESIDENT", "1") != "0"
    cmd = [str(exe), "-m", str(model), "--host", "127.0.0.1",
           "--port", str(_port()), "-c", str(_ctx()), "-np", "1"]
    cmd += ["--fit", "on", "--fit-target", str(_fit_mb),
            "--load-mode", "mmap", "-b", "512", "-ub", "512"]
    logger.info("[llamacpp] 35B fit 部分 offload（fit-target %d，常驻=%s）", _fit_mb, _coresident)
    cmd += ["--no-mmproj", "--no-warmup", "--no-webui"]
    # ★ 2026-11-17 task-1（可选省显存，默认关）：KV cache 量化。
    #   本模型 KV ≈ 82 KiB/token（41 层 × 2 kv-head × 256×(K+V) × 2B）：
    #   n_ctx=8192 ⇒ f16 ~656MiB / q8_0 ~328MiB / q4_0 ~164MiB。
    #   量化 V cache 需要 flash-attn，这里一并打开（-fa on）。
    #   用法：MUSICAI_LLAMACPP_KV_TYPE=q8_0|q4_0（默认 f16）；旧开关 KV_Q8=1 兼容。
    _kv_type = (os.getenv("MUSICAI_LLAMACPP_KV_TYPE") or "").strip().lower()
    if not _kv_type and _coresident:
        _kv_type = "q8_0"   # ★ 常驻模式默认 q8_0（16K ctx 下 ~656MiB，省显存给 TTS）
    if not _kv_type and os.getenv("MUSICAI_LLAMACPP_KV_Q8", "0") in ("1", "true", "True"):
        _kv_type = "q8_0"
    if _kv_type in ("q8_0", "q4_0", "q5_0", "q5_1", "q4_1"):
        cmd += ["--cache-type-k", _kv_type, "--cache-type-v", _kv_type, "-fa", "on"]
        logger.info("[llamacpp] KV cache 量化已启用：%s + flash-attn（省 KV 显存）", _kv_type)
    elif _kv_type not in ("", "f16", "fp16"):
        logger.warning("[llamacpp] 未知 KV 类型 %r，按 f16 处理", _kv_type)
    # ★★★ 2026-11-16 最关键修复：**必须显式指定 CUDA 后端 DLL**。
    #   事故：首版没设 GGML_BACKEND_PATH ⇒ llama.cpp 找不到 CUDA backend ⇒
    #   打印 "no usable GPU found, --gpu-layers option will be ignored" ⇒
    #   35B MoE **纯 CPU 推理**，口播 140 秒才出（用户实测）。
    #   真相：llama-server.exe 本身支持 GPU，Ollama 官方启动时会自动注入
    #   GGML_BACKEND_PATH=cuda_v12/ggml-cuda.dll；我们直连时必须自己设。
    #   APMP 实验 probe_v5.py:117 正是这么做的（当时 GPU 正常）。
    #   ⚠ 不是"这个二进制不支持 GPU"，是我漏了环境变量——纠正此前误判。
    _env = {k: v for k, v in os.environ.items()
            if not k.startswith("LLAMA_ARG_")}
    _cuda_dir = exe.parent / "cuda_v12"
    _cuda_dll = _cuda_dir / "ggml-cuda.dll"
    if _cuda_dll.is_file():
        _env["GGML_BACKEND_PATH"] = str(_cuda_dll)
        _env["PATH"] = str(_cuda_dir) + os.pathsep + str(exe.parent) \
                       + os.pathsep + _env.get("PATH", "")
        logger.info("[llamacpp] CUDA 后端已注入: %s", _cuda_dll)
    else:
        logger.warning("[llamacpp] CUDA 后端 DLL 缺失（%s），将纯 CPU 推理（口播会慢）", _cuda_dll)
    try:
        _PROC = subprocess.Popen(
            cmd, cwd=str(exe.parent), stdin=subprocess.DEVNULL,
            stdout=_LOG_FH, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True, env=_env)
        _OWNED = True
    except Exception as e:  # noqa
        logger.warning("[llamacpp] 启动失败: %s: %s", type(e).__name__, e)
        return False
    logger.info("[llamacpp] 已拉起 PID=%d，等待 /health（最多 %.0fs）",
                _PROC.pid, wait_s)
    atexit.register(stop)  # 正常/异常退出都清理，不留孤儿
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait_s:
        if health_ok():
            logger.info("[llamacpp] 就绪，用时 %.1fs", time.monotonic() - t0)
            return True
        if _PROC.poll() is not None:
            logger.warning("[llamacpp] 进程提前退出 rc=%s，见日志", _PROC.returncode)
            return False
        time.sleep(1.0)
    logger.warning("[llamacpp] %.0fs 内未就绪，见日志", wait_s)
    return False


def stop() -> None:
    """关闭本模块拉起的进程（不动外部已有服务）。"""
    global _PROC, _LOG_FH, _OWNED
    if not _OWNED or _PROC is None:
        return
    try:
        if _PROC.poll() is None:
            _PROC.terminate()
            try:
                _PROC.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _PROC.kill()
        logger.info("[llamacpp] 已关闭 PID=%d", _PROC.pid)
    except Exception as e:  # noqa
        logger.debug("[llamacpp] 关闭异常（忽略）: %s", e)
    finally:
        _PROC = None
        _OWNED = False
        if _LOG_FH is not None and _LOG_FH is not subprocess.DEVNULL:
            try:
                _LOG_FH.close()
            except Exception:  # noqa
                pass
            _LOG_FH = None
