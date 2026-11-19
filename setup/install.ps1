#Requires -RunAsAdministrator
# ============================================================
# MusicAI installer (must run as Administrator)
# Goal: create a virtualenv on D: and install all dependencies.
# Nothing is ever written to C: system directories.
# ============================================================
param(
    [switch]$WithML      # add -WithML to also install torch/faiss/clap (~1-2GB)
)

$ErrorActionPreference = "Stop"

# 1) Restart self elevated if not already administrator
if (-not ([Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))) {
    Write-Host "Requesting administrator privileges..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$ProjectDir = "D:\MusicAI"
$VenvDir    = "$ProjectDir\venv"
$PyBase     = "D:\Python311"
$PyExe      = "$PyBase\python.exe"
$PythonUrl  = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
$PythonInstaller = "$env:TEMP\python-3.11.9-amd64.exe"

# 2) Ensure project directory exists
if (-not (Test-Path $ProjectDir)) { New-Item -ItemType Directory -Path $ProjectDir | Out-Null }

# 3) Detect a REAL python.
#    The Microsoft Store "app execution alias" (under WindowsApps) is NOT a real
#    interpreter and must be ignored, otherwise `python` errors with
#    "Python was not found; run without arguments to install from the Microsoft Store".
function Get-RealPython {
    if (Test-Path $PyExe) { return $PyExe }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) {
        $src = $found.Source
        if ($src -like "*WindowsApps*") { return $null }   # ignore Store alias
        return $src
    }
    return $null
}

$py = Get-RealPython
if (-not $py) {
    Write-Host "No real Python detected (the Microsoft Store alias does not count)." -ForegroundColor Cyan
    Write-Host "Downloading Python 3.11.9 to $env:TEMP ..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonInstaller
    Write-Host "Installing Python quietly to $PyBase (no C: system dirs)..." -ForegroundColor Cyan
    Start-Process -Wait -FilePath $PythonInstaller -ArgumentList `
        "/quiet InstallDir=$PyBase PrependPath=1 Include_test=0"
    $py = $PyExe
}
& $py --version

# 4) Create the virtualenv on D:.
#    Rebuild it from scratch so the shebang inside Scripts\pip.exe points to the
#    correct location (renamed/moved venvs leave a broken pip.exe launcher).
if (Test-Path "$VenvDir\Scripts\python.exe") {
    Write-Host "Removing existing virtualenv to fix any broken paths..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $VenvDir
}
Write-Host "Creating virtualenv $VenvDir ..." -ForegroundColor Cyan
& $py -m venv $VenvDir

$Vpy = "$VenvDir\Scripts\python.exe"

# 5) Install dependencies via `python -m pip` (avoids pip.exe launcher shebang issues)
Write-Host "Upgrading pip..." -ForegroundColor Cyan
& $Vpy -m pip install --upgrade pip
Write-Host "Installing core dependencies..." -ForegroundColor Cyan
& $Vpy -m pip install -r "$ProjectDir\requirements.txt"

# 6) (optional) ML heavy dependencies
if ($WithML) {
    Write-Host "Installing ML heavy deps (torch / faiss-cpu / laion-clap)..." -ForegroundColor Cyan
    & $Vpy -m pip install torch --index-url https://download.pytorch.org/whl/cpu
    & $Vpy -m pip install faiss-cpu laion-clap
}

# 7) Done
Write-Host "`nInstallation complete!" -ForegroundColor Green
Write-Host "Run:" -ForegroundColor White
Write-Host "  $Vpy $ProjectDir\main.py" -ForegroundColor Gray
Read-Host "Press Enter to exit"
