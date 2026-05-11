#!/usr/bin/env bash
set -euo pipefail

echo "=================================================="
echo "🚀 正在启动 Splitwise Cloud Edge 调度中枢..."
echo "=================================================="

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"
ENV_FILE_PATH="${BACKEND_ENV_FILE:-$BACKEND_DIR/.env}"

VENV_PATH="$PROJECT_ROOT/venv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    echo "📦 正在激活虚拟环境..."
    source "$VENV_PATH"
else
    echo "🚨 错误: 未找到虚拟环境 ($VENV_PATH)！"
    exit 1
fi

echo "🌐 正在拉起 FastAPI 服务与监控大屏..."
cd "$BACKEND_DIR"

if [ -f "$ENV_FILE_PATH" ]; then
    echo "🧾 使用环境配置: $ENV_FILE_PATH"
    set -a
    source "$ENV_FILE_PATH"
    set +a
else
    echo "⚠️ 未找到环境配置文件: $ENV_FILE_PATH，将使用代码默认值。"
fi

export BACKEND_ENV_FILE="$ENV_FILE_PATH"

if [ "${OPENWEBUI_SKIP_SIGNATURE_VERIFY:-false}" = "true" ]; then
    echo "🧪 当前处于 OpenWebUI 跳过验签模式，适用于开发联调。"
elif [ -z "${OPENWEBUI_JWT_SECRET:-}" ]; then
    echo "⚠️ 未检测到 OPENWEBUI_JWT_SECRET，OpenWebUI token exchange 接口将返回配置未完成提示。"
fi

python -m app.main
