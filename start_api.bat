@echo off
REM Keep this file pure ASCII. cmd.exe mis-parses UTF-8 Chinese comments
REM and tries to execute them as commands. Chinese hints are printed by Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

.\venv\Scripts\python.exe -m webapi.server

echo.
echo Server stopped.
pause
