#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_ENV_FILE="$PROJECT_ROOT/backend/.env.dev"

if [ ! -f "$BACKEND_ENV_FILE" ]; then
    echo "🚨 错误: 未找到开发环境配置文件 ($BACKEND_ENV_FILE)！"
    exit 1
fi

echo "=================================================="
echo "🛠️ 正在启动 Splitwise Cloud Edge 调度中枢（开发隔离配置）..."
echo "=================================================="
echo "🧾 使用环境配置: $BACKEND_ENV_FILE"

export BACKEND_ENV_FILE
exec bash "$SCRIPT_DIR/run_server.sh"
