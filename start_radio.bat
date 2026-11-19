@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title MusicAI Radio

REM ============================================================
REM  MusicAI Radio launcher (FastAPI + web UI + Qwen3-TTS)
REM  Keep this file pure ASCII: cmd.exe mis-parses UTF-8 comments.
REM  Everything it needs lives in this folder; nothing is installed.
REM ============================================================

cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] venv python not found: %PY%
  echo         Run: powershell -ExecutionPolicy Bypass -File setup\install.ps1
  pause
  exit /b 1
)
if not exist "%~dp0start_api_host.py" (
  echo [ERROR] start_api_host.py not found next to this .bat
  pause
  exit /b 1
)

REM Backend must be llamacpp: the app manages llama-server itself.
set "MUSICAI_AI_BACKEND=llamacpp"
set "MUSICAI_LLAMACPP_MODEL=%~dp0models\llm\Qwen3.6-35B-A3B.gguf"
set "MUSICAI_LLAMACPP_EXE=%~dp0tools\llamacpp-b10955\llama-server.exe"

REM 2026-11-18 resident architecture: llama.cpp LLM + Qwen3-TTS Q4 (on-demand CLI).
REM Qwen3-TTS is spawned per synthesis (exits & frees VRAM). IndexTTS retired.
set "MUSICAI_CORESIDENT=1"
set "MUSICAI_LLAMACPP_NUM_CTX=16384"
set "MUSICAI_LLAMACPP_KV_TYPE=q8_0"

REM Port check: if 8765 is already listening, just open the existing UI.
netstat -ano | findstr /C:":8765" | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [INFO] Port 8765 is already in use - Radio Intelligence is probably running.
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":8765" ^| findstr /C:"LISTENING"') do echo        PID %%p is listening on 8765
  echo        Opening http://127.0.0.1:8765/
  echo        To restart: run stop_radio.bat ^(it kills the :8765 owner and waits^), then run this again.
  start "" "http://127.0.0.1:8765/"
  pause
  exit /b 0
)

echo ============================================================
echo   MusicAI Radio   http://127.0.0.1:8765/
echo   - New UI (default)      : http://127.0.0.1:8765/
echo   - Legacy UI (DEPRECATED): http://127.0.0.1:8765/legacy
echo ------------------------------------------------------------
echo   - llama.cpp model server is managed by the app; Qwen3-TTS
echo     spawns per synthesis and frees VRAM when done.
echo   - First start takes ~30s (33B model load). Keep it open.
echo   - Opening / closing voices: settings - TTS - voice
echo ============================================================
echo.

"%PY%" "%~dp0start_api_host.py"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  echo [EXIT] App closed - all processes stopped.
  timeout /t 1 /nobreak >nul
  exit /b 0
)
echo [EXIT rc=%RC%] App stopped unexpectedly. Last lines:
powershell -NoProfile -Command "if (Test-Path 'data_runtime\app_ui_switch.log') { Get-Content 'data_runtime\app_ui_switch.log' -Tail 25 }" 2>nul
pause
