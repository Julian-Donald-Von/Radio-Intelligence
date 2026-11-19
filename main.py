"""
MusicAI 入口。
用法：
  python main.py            启动 AI 智能电台界面
  python main.py --scan     仅扫描并入库本地音乐（不启动界面）
"""
from __future__ import annotations

import sys

from utils.logger import setup_logger

logger = setup_logger()

# GPU 显存额度划分（解放 qwen / 兼顾 melo）：必须在第一次 CUDA 分配前调用，
# 给本进程(MeloTTS)设 per-process 显存上限，避免与 Ollama 抢满整卡。
try:
    from core.gpu_budget import apply_gpu_budget
    apply_gpu_budget()
except Exception:  # noqa
    pass


def main() -> None:
    args = sys.argv[1:]
    if "--scan" in args:
        from data.db import Database
        from data.library import MusicLibrary
        from config.settings import MUSIC_LIBRARY_ROOTS
        db = Database()
        lib = MusicLibrary(db)
        n = lib.scan(MUSIC_LIBRARY_ROOTS)
        logger.info("入库完成：%d 首", n)
        print(f"扫描入库完成，共 {n} 首。")
        return

    from ui.main_window import run
    run()


if __name__ == "__main__":
    main()
