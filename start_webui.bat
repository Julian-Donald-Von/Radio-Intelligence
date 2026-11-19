@echo off
set "HERE=%~dp0"
set "PY=%HERE%venv\Scripts\python.exe"
if not exist "%PY%" goto :novenv
"%PY%" "%HERE%start_webui.py"
goto :eof

:novenv
echo [ERROR] Virtual environment not found: %PY%
echo Please create venv and install dependencies first.
echo Run:  powershell -ExecutionPolicy Bypass -File setup/install.ps1
pause
