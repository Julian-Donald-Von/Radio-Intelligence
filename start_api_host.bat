@echo off
REM Keep this file pure ASCII. Model server lifecycle is managed inside
REM start_api_host.py (core/llamacpp_manager.py) when MUSICAI_AI_BACKEND=llamacpp.
REM This .bat intentionally does nothing but launch Python: no PowerShell,
REM no process management, no file deletion -> cannot delete itself.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "MUSICAI_AI_BACKEND=llamacpp"
set "MUSICAI_LLAMACPP_MODEL=%~dp0models\llm\Qwen3.6-35B-A3B.gguf"
set "MUSICAI_LLAMACPP_EXE=%~dp0tools\llamacpp-b10955\llama-server.exe"

REM 2026-11-18 resident architecture: llama.cpp LLM + Qwen3-TTS Q4 (on-demand CLI).
REM IndexTTS retired - no W8A16 worker.
set "MUSICAI_CORESIDENT=1"
set "MUSICAI_LLAMACPP_NUM_CTX=16384"
set "MUSICAI_LLAMACPP_KV_TYPE=q8_0"

REM [A/B] Water visual regression probe (default = keep the fixes):
REM   To test the pre-bat cache/loading behavior, uncomment the next line,
REM   restart, and compare the water feel. Then revert to default.
REM set "MUSICAI_QT_CACHE=1"
REM set "MUSICAI_SKIP_GPU_BUDGET=1"

echo ============================================================
echo  MusicAI - Liquid Glass UI (FastAPI mode)
echo  Web UI  : http://127.0.0.1:8765
echo  Model server (llama.cpp) is managed by Python; it will be
echo  started/stopped together with this window automatically.
echo ============================================================
echo.

"%~dp0venv\Scripts\python.exe" "%~dp0start_api_host.py"

echo.
echo Host stopped (model server closed).
pause
