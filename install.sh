#!/usr/bin/env bash
# APSGraph 安装脚本：构建 wheel 并以 pip 安装 apsgraph 命令。
# 用法：
#   ./install.sh                 # 构建并安装（自动适配本机 pip 环境）
#   PYTHON=python3.10 ./install.sh
set -euo pipefail

PYTHON="${PYTHON:-python3}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$REPO_DIR/dist"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "[install.sh] 错误：未找到 $PYTHON（需要 Python >= 3.9）" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "[install.sh] 错误：Python 版本过低，APSGraph 需要 >= 3.9（当前：$($PYTHON -V 2>&1)）" >&2
    exit 1
fi

if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "[install.sh] 错误：$PYTHON 缺少 pip（可先执行 $PYTHON -m ensurepip）" >&2
    exit 1
fi

cd "$REPO_DIR"
mkdir -p "$DIST_DIR"
echo "[install.sh] 构建 wheel..."
"$PYTHON" -m pip wheel . --no-deps -w "$DIST_DIR" >/dev/null

WHEEL="$(ls -t "$DIST_DIR"/apsgraph-*.whl 2>/dev/null | head -1)"
if [ -z "$WHEEL" ]; then
    echo "[install.sh] 错误：wheel 构建失败" >&2
    exit 1
fi
echo "[install.sh] 安装 $WHEEL"

# 依次尝试常规安装方式，兼容 PEP 668（externally-managed）等环境限制
if "$PYTHON" -m pip install --force-reinstall "$WHEEL" >/dev/null 2>&1; then
    :
elif "$PYTHON" -m pip install --user --force-reinstall "$WHEEL" >/dev/null 2>&1; then
    echo "[install.sh] 已按 --user 方式安装"
elif "$PYTHON" -m pip install --break-system-packages --force-reinstall "$WHEEL" >/dev/null 2>&1; then
    echo "[install.sh] 已按 --break-system-packages 方式安装"
elif "$PYTHON" -m pip install --user --break-system-packages --force-reinstall "$WHEEL" >/dev/null 2>&1; then
    echo "[install.sh] 已按 --user --break-system-packages 方式安装"
else
    echo "[install.sh] 错误：pip 安装失败，请手动执行：$PYTHON -m pip install $WHEEL" >&2
    exit 1
fi

echo "[install.sh] 安装完成：$("$PYTHON" -c 'from apsgraph import __version__; print(__version__)' 2>/dev/null || apsgraph --version 2>/dev/null || echo '未知版本')"
echo "[install.sh] 使用 apsgraph --help 查看命令；扫描后可用 apsgraph workbench 启动查询工作台。"
