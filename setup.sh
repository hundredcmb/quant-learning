#!/usr/bin/env bash
# ============================================================
# setup.sh —— 一键初始化项目虚拟环境 .venv（支持 Linux / macOS）
#
# 作用：
#   1. 用命令行参数指定的 veighna Python 创建虚拟环境 .venv
#      （--system-site-packages，继承 vnpy / tushare / TA-Lib / PySide6 等全部依赖）
#   2. 在 .venv 中安装项目自身依赖（requirements.txt）
#
# 用法（-p 必填，脚本不会自动探测 veighna 安装位置）：
#   ./setup.sh -p "/opt/veighna_studio/bin/python"
#   ./setup.sh --python-path "$HOME/veighna_studio/bin/python3"
#
# 常用参数：
#   -p, --python-path <路径>  必填：veighna 自带 python 的完整路径
#   -s, --skip-install        只创建 .venv，不执行 pip install
#   -f, --force               删除现有 .venv 后重建
#   -h, --help                显示帮助
# ============================================================

set -euo pipefail

RepoRoot="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VenvDir="$RepoRoot/.venv"
VenvPython="$VenvDir/bin/python"
Requirements="$RepoRoot/requirements.txt"

PYTHON_PATH=""
SKIP_INSTALL=0
FORCE=0

step() { echo "==> $*"; }
fail() { echo "[错误] $*" >&2; exit 1; }

usage() {
    cat <<EOF
用法: ./setup.sh -p <veighna python 路径> [选项]

必填参数:
  -p, --python-path <路径>   veighna 自带 python 的完整路径（脚本不会自动探测）

选项:
  -s, --skip-install         只创建 .venv，不执行 pip install
  -f, --force                删除现有 .venv 后重建
  -h, --help                 显示本帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--python-path)
            [[ $# -lt 2 ]] && { fail "$1 缺少参数值"; }
            PYTHON_PATH="$2"
            shift 2
            ;;
        -s|--skip-install)
            SKIP_INSTALL=1
            shift
            ;;
        -f|--force)
            FORCE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "未知参数: $1（用 -h 查看帮助）"
            ;;
    esac
done

cd "$RepoRoot"

# ---------- 1. 校验 veighna Python 路径（-p 必填） ----------
step "校验 veighna Python 路径（-p）..."
if [[ -z "$PYTHON_PATH" ]]; then
    fail "未指定 veighna Python 路径。用法（支持 Linux / macOS）：

    ./setup.sh -p \"/opt/veighna_studio/bin/python\"

    -p 为必填参数，必须指向 veighna 自带 python 的完整路径（脚本不会自动探测）。
"
fi
if [[ ! -f "$PYTHON_PATH" ]]; then
    fail "指定的路径不存在: $PYTHON_PATH"
fi
echo "    已指定: $PYTHON_PATH"

# 1a. 校验：能 import vnpy
step "校验 vnpy 可用性: $PYTHON_PATH"
if ! "$PYTHON_PATH" -c "import vnpy" >/dev/null 2>&1; then
    fail "该 Python 无法 import vnpy（$PYTHON_PATH），请确认路径指向 veighna 自带的 Python。"
fi

# ---------- 2. 创建 / 重建 .venv ----------
if [[ -d "$VenvDir" ]]; then
    if [[ $FORCE == 1 ]]; then
        step "检测到已有 .venv，-f 指定，删除后重建 ..."
        rm -rf "$VenvDir"
    else
        step "检测到已有 .venv，跳过创建（如需重建请加 -f）"
    fi
fi

if [[ ! -x "$VenvPython" ]]; then
    step "创建虚拟环境 .venv（--system-site-packages，继承 vnpy 全部依赖）..."
    "$PYTHON_PATH" -m venv --system-site-packages "$VenvDir"
fi

# 2a. 创建后自检：venv 能否 import vnpy
step "自检 .venv 中的 vnpy 可用性 ..."
if ! "$VenvPython" -c "import vnpy" >/dev/null 2>&1; then
    echo "[警告] .venv 无法 import vnpy，请确认 veighna 环境正常，必要时加 -f 重建" >&2
else
    echo "    .venv 已继承 vnpy 环境"
fi

# ---------- 3. 安装项目自身依赖 ----------
if [[ $SKIP_INSTALL == 1 ]]; then
    step "已跳过依赖安装（-s）"
elif [[ -f "$Requirements" ]]; then
    step "安装项目自身依赖（requirements.txt）..."
    if ! "$VenvPython" -m pip install -r "$Requirements"; then
        fail "依赖安装失败（可能是网络问题），可重试或加 -s 跳过。"
    fi
fi

# ---------- 完成 ----------
echo ""
echo "初始化完成！之后统一用以下命令运行脚本："
echo "  .venv/bin/python  <脚本路径>"
echo "  .venv/bin/python  shenwan_industry/web/desktop.pyw   # 桌面窗口（Linux / macOS 直接用 python 即可，无需 pythonw）"
echo "如需重建虚拟环境：./setup.sh -p <veighna python 路径> -f"
