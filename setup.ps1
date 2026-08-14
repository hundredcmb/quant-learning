# ============================================================
# setup.ps1 —— 一键初始化项目虚拟环境 .venv（仅支持 Windows）
#
# 作用：
#   1. 用命令行参数指定的 veighna studio Python 创建虚拟环境 .venv
#      （--system-site-packages，继承 vnpy / tushare / TA-Lib / PySide6 等全部依赖）
#   2. 在 .venv 中安装项目自身依赖（requirements.txt）
#
# 用法（-PythonPath 必填，脚本不会自动探测 veighna 安装位置）：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -PythonPath "D:\Tools\veighna_studio\python.exe"
#
# 常用参数：
#   -PythonPath <路径>  必填：veighna studio 自带 python.exe 的完整路径
#   -SkipInstall        只创建 .venv，不执行 pip install
#   -Force              删除现有 .venv 后重建
# ============================================================

[CmdletBinding()]
param(
    [string]$PythonPath,
    [switch]$SkipInstall,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot     = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenVenv      = Join-Path $RepoRoot ".venv"
$Requirements = Join-Path $RepoRoot "requirements.txt"

function Step { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Fail { param([string]$Msg) Write-Host "[错误] $Msg" -ForegroundColor Red; exit 1 }

Set-Location $RepoRoot

# ---------- 1. 校验 -PythonPath（必填） ----------
Step "校验 veighna Python 路径（-PythonPath）..."
if (-not $PythonPath) {
    $hint = '未指定 veighna Python 路径。用法（仅支持 Windows）：

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -PythonPath "D:\Tools\veighna_studio\python.exe"

    -PythonPath 为必填参数，必须指向 veighna studio 自带 python.exe 的完整路径（脚本不会自动探测）。
'
    Fail $hint
}
$Candidate = $PythonPath.Trim()
if (-not (Test-Path -LiteralPath $Candidate)) {
    Fail "指定的路径不存在: $Candidate"
}
Write-Host "    已指定: $Candidate"

# 1a. 校验：能 import vnpy
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
