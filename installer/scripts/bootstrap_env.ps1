#Requires -Version 5.1
# 安装期环境引导：下载 Python 3.13 -> 静默安装到应用目录 -> 建 venv -> pip 安装依赖
# 不写 C 系统目录；所有产物都在 $AppDir 下。
param(
    [Parameter(Mandatory=$true)][string]$AppDir,
    [Parameter(Mandatory=$true)][string]$SourcesJson,
    [switch]$WithML
)

$ErrorActionPreference = "Stop"
$Log = Join-Path $AppDir "data_runtime\install_bootstrap.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
function Say($m){ $line = "$(Get-Date -Format 'HH:mm:ss') $m"; Write-Host $line; Add-Content -Path $Log -Value $line }

$src = Get-Content $SourcesJson -Raw -Encoding UTF8 | ConvertFrom-Json
$py = $src.python
$PyDir = Join-Path $AppDir "Python313"
$PyExe = Join-Path $PyDir "python.exe"
$Venv  = Join-Path $AppDir "venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

# --- 1) 下载 Python（带 sha256 校验）---
if (Test-Path $PyExe) {
    Say "Python313 已存在，跳过下载"
} else {
    $pkg = Join-Path $env:TEMP "python-3.13.14-amd64.exe"
    if (-not (Test-Path $pkg) -or (Get-FileHash $pkg -Algorithm SHA256).Hash.ToLower() -ne $py.sha256.ToLower()) {
        Say "下载 Python 3.13.14 ($([math]::Round($py.bytes/1MB))MB) ..."
        Invoke-WebRequest -Uri $py.url -OutFile $pkg -UseBasicParsing
    }
    $got = (Get-FileHash $pkg -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $py.sha256.ToLower()) { Say "Python 安装包 sha256 不符: $got"; throw "sha256 mismatch" }
    Say "Python 安装包校验通过，静默安装到 $PyDir"
    # 用参数数组并显式给含空格的路径加引号；避免整串 ArgumentList 被误拆
    $instDirArg = 'InstallDir="' + $PyDir + '"'
    $args = @('/quiet', $instDirArg, 'PrependPath=0', 'Include_test=0', 'Include_doc=0')
    $p = Start-Process -FilePath $pkg -ArgumentList $args -Wait -PassThru
    if ($p.ExitCode -ne 0) { Say "Python 安装失败 exit=$($p.ExitCode)"; throw "python install failed" }
}

# --- 2) 建 venv ---
if (-not (Test-Path $VenvPy)) {
    Say "创建 venv ..."
    & $PyExe -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv create failed" }
}

# --- 3) pip 安装依赖（清华镜像，可改 PIP_INDEX_URL）---
$idx = $env:PIP_INDEX_URL
if (-not $idx) { $idx = "https://pypi.tuna.tsinghua.edu.cn/simple" }
Say "pip 安装依赖，镜像=$idx"
$base = @("-m","pip","install","--index-url",$idx,"--disable-pip-version-check")
& $VenvPy @base -r (Join-Path $AppDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Say "基础依赖安装失败"; throw "pip base install failed" }

if ($WithML) {
    Say "安装 ML 依赖（torch/faiss/transformers，约 5GB）..."
    & $VenvPy @base torch==2.6.0 faiss-cpu transformers laion-clap
    if ($LASTEXITCODE -ne 0) { Say "ML 依赖安装失败（核心功能仍可用）" }
}

# --- 4) 自检 ---
& $VenvPy -c "import pygame, requests, fastapi, uvicorn; print('IMPORT_OK')" | Tee-Object -Variable imp
if ($imp -notmatch "IMPORT_OK") { throw "import selfcheck failed" }
Say "环境引导完成: $VenvPy"
$status = Join-Path $AppDir "data_runtime\install_status.json"
@{ python = $PyExe; venv = $VenvPy; ok = $true; at = (Get-Date).ToString("o") } |
    ConvertTo-Json | Set-Content -Path $status -Encoding UTF8
