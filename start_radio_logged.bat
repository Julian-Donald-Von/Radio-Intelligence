@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title MusicAI Radio (logged)

REM ============================================================
REM  Same as start_radio.bat, but stdout/stderr are redirected to
REM  data_runtime\app_live_logged*.log so the TTS/LLM pipeline can
REM  be verified afterwards (verify_live_task1.py).
REM ============================================================

cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] venv python not found: %PY%
  pause
  exit /b 1
)
if not exist "%~dp0start_api_host.py" (
  echo [ERROR] start_api_host.py not found next to this .bat
  pause
  exit /b 1
)

set "MUSICAI_AI_BACKEND=llamacpp"
set "MUSICAI_LLAMACPP_MODEL=%~dp0models\llm\Qwen3.6-35B-A3B.gguf"
set "MUSICAI_LLAMACPP_EXE=%~dp0tools\llamacpp-b10955\llama-server.exe"

REM 2026-11-18 resident architecture: llama.cpp LLM + Qwen3-TTS Q4 (IndexTTS retired).
set "MUSICAI_CORESIDENT=1"
set "MUSICAI_LLAMACPP_NUM_CTX=16384"
set "MUSICAI_LLAMACPP_KV_TYPE=q8_0"

netstat -ano | findstr /C:":8765" | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [INFO] Port 8765 already in use - close that instance first.
  pause
  exit /b 0
)

echo ============================================================
echo   MusicAI Radio (logged)  http://127.0.0.1:8765/
echo   - New UI (default)      : http://127.0.0.1:8765/
echo   - Legacy UI (DEPRECATED): http://127.0.0.1:8765/legacy
echo   logs: data_runtime\app_live_logged.log
echo         data_runtime\app_live_logged_err.log
echo   TTS window ledger: data_runtime\tts_windows.jsonl
echo ============================================================
echo.

"%PY%" "%~dp0start_api_host.py" > "%~dp0data_runtime\app_live_logged.log" 2> "%~dp0data_runtime\app_live_logged_err.log"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
  echo [EXIT] App closed - all processes stopped.
  timeout /t 1 /nobreak >nul
  exit /b 0
)
echo [EXIT rc=%RC%] App stopped unexpectedly. Last lines:
powershell -NoProfile -Command "if (Test-Path 'data_runtime\app_ui_switch.log') { Get-Content 'data_runtime\app_ui_switch.log' -Tail 25 }" 2>nul
pause
