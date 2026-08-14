# ============================================================
# setup.ps1 —— 一键初始化项目虚拟环境 .venv
#
# 作用：
#   1. 定位本机 veighna studio 自带的 Python（手动指定优先，其次自动探测）
#   2. 用该 Python 创建虚拟环境 .venv（--system-site-packages，
#      继承 vnpy / tushare / TA-Lib / PySide6 等客户端全部依赖）
#   3. 在 .venv 中安装项目自身依赖（requirements.txt）
#
# 手动指定 veighna Python 的三种方式（优先级从高到低）：
#   方式一（推荐）：命令行参数
#       .\setup.ps1 -PythonPath "D:\Tools\veighna_studio\python.exe"
#   方式二：环境变量 VNPY_PYTHON
#       $env:VNPY_PYTHON = "D:\Tools\veighna_studio\python.exe"
#       .\setup.ps1
#   方式三：本地文件 .pythonpath（已被 .gitignore 忽略）
#       在仓库根目录新建 .pythonpath，第一行写 veighna python 完整路径
#
# 都不指定时自动探测：常见安装目录 -> C:\ / D:\ 盘扫描 -> PATH 中的 python
#
# 常用参数：
#   -PythonPath <路径>  手动指定 veighna python（也可用 VNPY_PYTHON / .pythonpath）
#   -SkipInstall        只创建 .venv，不执行 pip install
#   -Force              删除现有 .venv 后重建
# ============================================================

[CmdletBinding()]
param(
    [string]$PythonPath = $env:VNPY_PYTHON,
    [switch]$SkipInstall,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot      = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenVenv       = Join-Path $RepoRoot ".venv"
$PythonPathFile = Join-Path $RepoRoot ".pythonpath"
$Requirements  = Join-Path $RepoRoot "requirements.txt"

function Step { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Fail { param([string]$Msg) Write-Host "[错误] $Msg" -ForegroundColor Red; exit 1 }

Set-Location $RepoRoot

# ---------- 1. 定位 veighna python ----------
Step "定位 veighna studio 自带的 Python ..."
$Candidate = $null

# 1a. 手动指定（-PythonPath 参数 / VNPY_PYTHON 环境变量）
if ($PythonPath) {
    $Candidate = $PythonPath.Trim()
    Write-Host "    已通过参数/环境变量指定: $Candidate"
}
# 1b. 本地 .pythonpath 文件（第一行写路径）
elseif (Test-Path -LiteralPath $PythonPathFile) {
    $Candidate = (Get-Content -LiteralPath $PythonPathFile -TotalCount 1 | Select-Object -First 1).Trim()
    if ($Candidate) { Write-Host "    已从 .pythonpath 文件读取: $Candidate" }
}
# 1c. 自动探测
if (-not $Candidate) {
    $commonPaths = @(
        "C:\veighna_studio\python.exe",
        "D:\veighna_studio\python.exe",
        "$env:LOCALAPPDATA\Programs\veighna_studio\python.exe",
        "$env:ProgramFiles\veighna_studio\python.exe",
        "$env:ProgramFiles(x86)\veighna_studio\python.exe"
    )
    $Candidate = $commonPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($Candidate) { Write-Host "    常见安装目录命中: $Candidate" }
}
if (-not $Candidate) {
    Step "常见目录未命中，扫描 C:\ 与 D:\ 盘中的 veighna_studio ..."
    $Candidate = Get-ChildItem -Path "C:\", "D:\" -Filter "python.exe" -Recurse -Depth 5 -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "veighna" } |
        Select-Object -First 1 -ExpandProperty FullName
    if ($Candidate) { Write-Host "    盘符扫描命中: $Candidate" }
}
if (-not $Candidate) {
    Step "盘符扫描未命中，尝试 PATH 中的 python（需能 import vnpy）..."
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Candidate = $cmd.Source }
    if ($Candidate) { Write-Host "    PATH 命中: $Candidate" }
}

if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) {
    Fail "未找到 veighna studio 的 python。请手动指定，例如：`n    .\setup.ps1 -PythonPath `"D:\Tools\veighna_studio\python.exe`""
}

# 1d. 校验：确实能 import vnpy
Step "校验 vnpy 可用性: $Candidate"
& $Candidate -c "import vnpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "该 Python 无法 import vnpy（$Candidate），请确认路径指向 veighna studio 自带的 Python。"
}

# ---------- 2. 创建 / 重建 .venv ----------
$VenvPython = Join-Path $VenVenv "Scripts\python.exe"
if (Test-Path -LiteralPath $VenVenv) {
    if ($Force) {
        Step "检测到已有 .venv，-Force 指定，删除后重建 ..."
        Remove-Item -LiteralPath $VenVenv -Recurse -Force
    }
    else {
        Step "检测到已有 .venv，跳过创建（如需重建请加 -Force）"
    }
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Step "创建虚拟环境 .venv（--system-site-packages，继承 vnpy 全部依赖）..."
    & $Candidate -m venv --system-site-packages $VenVenv
    if ($LASTEXITCODE -ne 0) { Fail "创建 .venv 失败，请检查后重试。" }
}

# 2a. 创建后自检：venv 能否 import vnpy
Step "自检 .venv 中的 vnpy 可用性 ..."
& $VenvPython -c "import vnpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[警告] .venv 无法 import vnpy，请确认 veighna 环境正常，必要时加 -Force 重建" -ForegroundColor Yellow
}
else {
    Write-Host "    .venv 已继承 vnpy 环境" -ForegroundColor Green
}

# ---------- 3. 安装项目自身依赖 ----------
if ($SkipInstall) {
    Step "已跳过依赖安装（-SkipInstall）"
}
else {
    if (Test-Path -LiteralPath $Requirements) {
        Step "安装项目自身依赖（requirements.txt）..."
        & $VenvPython -m pip install -r $Requirements
        if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败（可能是网络问题），可重试或加 -SkipInstall 跳过。" }
    }
}

# ---------- 完成 ----------
Write-Host ""
Write-Host "初始化完成！之后统一用以下命令运行脚本：" -ForegroundColor Green
Write-Host "  .venv\Scripts\python.exe  <脚本路径>" -ForegroundColor Green
Write-Host "  .venv\Scripts\pythonw.exe  shenwan_industry\web\desktop.pyw   # 桌面窗口" -ForegroundColor Green
Write-Host "如需重建虚拟环境：.\setup.ps1 -Force" -ForegroundColor Green
