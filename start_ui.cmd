@echo off
chcp 65001 >nul
title MusicAI Radio Intelligence
cd /d D:\MusicAI
echo Starting MusicAI Web UI (FastAPI) ...
echo.
rem WebEngine 在受限/沙箱环境下渲染子进程会被拦（表现：窗口全黑、无 QtWebEngineProcess.exe）。
rem --no-sandbox 解除该限制；若仍黑屏，把下一行改成 --no-sandbox --disable-gpu（走软件渲染）。
set QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox
rem 2026-11-18 resident architecture: llama.cpp LLM + Qwen3-TTS Q4 (IndexTTS retired).
set MUSICAI_AI_BACKEND=llamacpp
set "MUSICAI_LLAMACPP_MODEL=%~dp0models\llm\Qwen3.6-35B-A3B.gguf"
set "MUSICAI_LLAMACPP_EXE=%~dp0tools\llamacpp-b10955\llama-server.exe"
set MUSICAI_CORESIDENT=1
set MUSICAI_LLAMACPP_NUM_CTX=16384
set MUSICAI_LLAMACPP_KV_TYPE=q8_0
venv\Scripts\python.exe -u start_api_host.py
echo.
echo === exited, code=%ERRORLEVEL% ===
pause
