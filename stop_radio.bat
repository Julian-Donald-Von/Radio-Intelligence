@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Radio Intelligence - stop

REM ============================================================
REM  Stop Radio Intelligence cleanly.
REM  Kills only OUR processes: llama-server, the :8765 port owner
REM  (whole tree), and any start_api_host.py host. Then WAITS for
REM  the port to be released before reporting success.
REM  Keep this file pure ASCII + CRLF (cmd.exe mis-parses UTF-8/LF).
REM ============================================================

echo Stopping Radio Intelligence ...

REM 1) llama.cpp model server
taskkill /IM llama-server.exe /F >nul 2>&1

REM 2) whoever is LISTENING on :8765  (kill tree)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":8765" ^| findstr /C:"LISTENING"') do (
  echo   killing PID %%p holding :8765
  taskkill /PID %%p /F /T >nul 2>&1
)

REM 3) app host by command line (kill tree; catches non-port children too)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$pat='start_api_host'; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $pat } | ForEach-Object { try { & taskkill /PID $_.ProcessId /F /T 2>$null | Out-Null; Write-Host ('  stopped pid ' + $_.ProcessId) } catch {} }"

REM 4) wait (max ~12s) for the port to actually free
set "N=0"
:waitloop
netstat -ano | findstr /C:":8765" | findstr /C:"LISTENING" >nul 2>&1
if errorlevel 1 goto portfree
set /a N+=1
if %N% GEQ 12 goto portbusy
timeout /t 1 /nobreak >nul
goto waitloop

:portfree
echo Port 8765 released.
goto done

:portbusy
echo [WARN] Port 8765 is STILL busy after 12s.
echo        Open Task Manager - Details, sort by PID, end the python.exe
echo        that owns port 8765 (netstat -ano ^| findstr :8765).

:done
echo Done. Press any key to close.
pause >nul