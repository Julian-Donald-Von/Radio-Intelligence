"""
MusicAI · 液态玻璃舱 Web UI 启动器（新版前端）

为什么用 .py 而不是 .bat：
  Windows 批处理对 goto / 括号块 / 编码极其敏感，一个语法错误就会「窗口一闪而过」且看不到报错；
  本启动器用 Python 启动，所有 stdout/stderr（含 bridge_host 的完整 traceback）都会落盘到
  data_runtime/webui.log，并在异常时暂停等待按键，杜绝静默闪退。

职责：
  1. 确保使用项目自带 venv 运行；
  2. 本地 qwen 口播 / DuckDuckGo 联网依赖本机 Ollama 服务，启动前探活，未运行则后台拉起（不阻塞）；
  3. 启动新版 Web UI（ui/bridge_host.py --full = 完整后端栈 + 液态玻璃舱前端）。

保留 start.bat / main.py 用于启动「老 UI」（PySide6 原生界面），便于对比。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "data_runtime", "webui.log")
VENV_PY = os.path.join(HERE, "venv", "Scripts", "python.exe")
BRIDGE_PY = os.path.join(HERE, "ui", "bridge_host.py")
OLLAMA = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"),
    "Programs", "Ollama", "ollama.exe",
)

# 让子进程输出实时落盘 + 打印到控制台
_logf = open(LOG_PATH, "a", encoding="utf-8", errors="replace")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        _logf.write(line + "\n")
        _logf.flush()
    except Exception:
        pass


def ollama_up() -> bool:
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "3", "http://127.0.0.1:11434/api/tags"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def ensure_ollama() -> None:
    # GPU 显存额度：拉起 ollama serve 前写入 OLLAMA_MAX_VRAM，给 MeloTTS 留位。
    try:
        from core.gpu_budget import ollama_max_vram_bytes
        _max = ollama_max_vram_bytes()
        if _max > 0:
            os.environ["OLLAMA_MAX_VRAM"] = str(_max)
            log(f"已为 Ollama 设置显存上限 {_max // (1024 * 1024)} MiB（解放 qwen / 兼顾 melo）。")
    except Exception as e:  # noqa
        log(f"设置 OLLAMA_MAX_VRAM 失败（沿用默认）：{e}")
    # ★ R119-4v：**KV cache 量化 + flash attention** —— 大上下文下省显存的关键杠杆。
    #   默认 KV 是 f16：实测 64K 窗口光 KV 就要 ~2GB，直接把 9b 挤出显存（GPU 占比仅 64%）；
    #   q8_0 减半（→73%），q4_0 再减半（→80%）。q8_0 质量损失极小（实测语言标签仍正确），
    #   故默认取 q8_0。两项**只在 ollama serve 启动时读取**，且量化 KV 要求 flash attention
    #   已打开 ⇒ 必须由本启动器拉起才生效；若 Ollama 已由系统/托盘常驻，则沿用其自身环境。
    os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    os.environ.setdefault("OLLAMA_KV_CACHE_TYPE", "q8_0")
    log("已为 Ollama 设置 KV cache=q8_0 + flash attention（大上下文下省显存）。")
    if ollama_up():
        log("Ollama 服务已运行（本地 qwen 口播 / 联网可用）。")
        return
    if os.path.exists(OLLAMA):
        log("未检测到 Ollama 服务，正在后台拉起（约数秒）...")
        try:
            subprocess.Popen(
                [OLLAMA, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x00000008,  # DETACHED_PROCESS
            )
        except Exception as e:
            log(f"后台拉起 Ollama 失败（不影响软件启动）：{e}")
        for _ in range(20):
            time.sleep(1)
            if ollama_up():
                log("Ollama 服务已就绪。")
                return
        log("Ollama 仍未就绪，AI 口播将走离线模板（软件仍可正常使用）。")
    else:
        log("未找到 Ollama，AI 口播将走离线模板（联网/本地模型不可用）。")


def main() -> int:
    log("=" * 60)
    log("MusicAI · Web UI（液态玻璃舱）启动器开始执行")

    if not os.path.exists(VENV_PY):
        log(f"错误：未找到虚拟环境：{VENV_PY}")
        log("请先按部署文档创建 venv 并安装依赖。")
        return 1
    if not os.path.exists(BRIDGE_PY):
        log(f"错误：未找到前端宿主文件：{BRIDGE_PY}")
        return 1

    ensure_ollama()

    log("正在运行 MusicAI Web UI（完整后端 + 液态玻璃舱前端）...")
    try:
        proc = subprocess.Popen(
            [VENV_PY, BRIDGE_PY, "--full"],
            cwd=HERE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            try:
                print(line, end="")
            except Exception:
                pass
            try:
                _logf.write(line)
                _logf.flush()
            except Exception:
                pass
        rc = proc.wait()
        log(f"Web UI 退出（返回码 {rc}）。")
        return rc
    except Exception:
        log("启动 Web UI 时发生异常：")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception:
        try:
            log("启动器顶层异常：")
            log(traceback.format_exc())
        except Exception:
            pass
        code = 1

    if code not in (0, None):
        log(f"程序异常退出（码={code}）。日志见：{LOG_PATH}")
        try:
            input("按回车键关闭窗口...")
        except Exception:
            pass
    sys.exit(code if code is not None else 0)
