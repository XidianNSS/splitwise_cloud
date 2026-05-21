#!/usr/bin/env bash
set -euo pipefail

echo "=================================================="
echo "🚀 正在启动 Splitwise Cloud Edge 调度中枢..."
echo "=================================================="

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"
ENV_FILE_PATH="${BACKEND_ENV_FILE:-$BACKEND_DIR/.env}"
WYY_ENV_PATH="$BACKEND_DIR/.env.wyy"
if [ "${ENV_FILE_PATH#/}" = "$ENV_FILE_PATH" ]; then
    ENV_FILE_PATH="$PROJECT_ROOT/$ENV_FILE_PATH"
fi

ensure_port_available() {
    local configured_port="$1"
    local service_name="$2"
    if ss -ltn | grep -q ":${configured_port}\b"; then
        echo "🚨 错误: ${service_name} 端口 ${configured_port} 已被占用。"
        echo "💡 当前 .env.wyy 视为固定配置，请先释放端口后再启动，脚本不会自动改写环境变量。"
        exit 1
    fi
}

apply_wyy_port_override_if_needed() {
    if [ "$ENV_FILE_PATH" != "$WYY_ENV_PATH" ]; then
        return
    fi
    local configured_port
    configured_port=$(grep '^SERVER_PORT=' "$ENV_FILE_PATH" | cut -d'=' -f2)
    if [ -z "$configured_port" ]; then
        return
    fi
    ensure_port_available "$configured_port" "backend"
}

VENV_PATH="$PROJECT_ROOT/venv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    echo "📦 正在激活虚拟环境..."
    source "$VENV_PATH"
else
    echo "🚨 错误: 未找到虚拟环境 ($VENV_PATH)！"
    exit 1
fi

echo "🌐 正在拉起 FastAPI 服务与监控大屏..."
if [ -z "${BACKEND_ENV_FILE:-}" ] && [ -f "$WYY_ENV_PATH" ]; then
    echo "💡 检测到开发隔离配置: $WYY_ENV_PATH"
    echo "💡 如需运行并发控制开发版，可使用：BACKEND_ENV_FILE=backend/.env.wyy bash scripts/run_server.sh"
fi
apply_wyy_port_override_if_needed
cd "$BACKEND_DIR"

if [ -f "$ENV_FILE_PATH" ]; then
    echo "🧾 使用环境配置: $ENV_FILE_PATH"
    if [ "$ENV_FILE_PATH" = "$WYY_ENV_PATH" ]; then
        echo "🧪 当前运行的是 WYY 并发控制开发环境（独立端口 / 独立数据库）。"
    fi
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
