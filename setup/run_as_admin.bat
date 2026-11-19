@echo off
:: Run the MusicAI installer as Administrator.
:: All dependencies are installed on D: (nothing is written to C:).
:: Double-click this file. For ML deps (torch/faiss/clap) run instead:
::   powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1" -WithML
setlocal
set "SCRIPT=%~dp0install.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath powershell.exe -Verb RunAs -ArgumentList @('-ExecutionPolicy','Bypass','-File','\"%SCRIPT%\"')"
endlocal
