import requests
import json
import time
import os
import jwt
import uuid
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.getenv("BACKEND_ENV_FILE", str(PROJECT_ROOT / "backend" / ".env")))
if not ENV_FILE.is_absolute():
    ENV_FILE = PROJECT_ROOT / ENV_FILE
load_dotenv(ENV_FILE)

BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8010")
SESSION_INIT_URL = f"{BACKEND_BASE_URL}/api/v1/session/init"
TRIGGER_URL = f"{BACKEND_BASE_URL}/api/v1/schedule/trigger"
TASK_URL_TEMPLATE = f"{BACKEND_BASE_URL}/api/v1/schedule/tasks/{{task_id}}"
STRATEGY_URL_TEMPLATE = f"{BACKEND_BASE_URL}/api/v1/schedule/tasks/{{task_id}}/strategy"
EDGE_FRONTEND_USE_MOCK = os.getenv("EDGE_FRONTEND_USE_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}
OPENWEBUI_JWT_SECRET = os.getenv("OPENWEBUI_JWT_SECRET", "")
OPENWEBUI_JWT_ALGORITHM = os.getenv("OPENWEBUI_JWT_ALGORITHM", "HS256")
OPENWEBUI_SKIP_SIGNATURE_VERIFY = os.getenv("OPENWEBUI_SKIP_SIGNATURE_VERIFY", "").strip().lower() in {"1", "true", "yes", "on"}
EDGE_DEVICE_IP = os.getenv("EDGE_DEVICE_IP", "10.144.144.3")
EDGE_FRONTEND_MOCK_MODEL_TYPE = os.getenv("EDGE_FRONTEND_MOCK_MODEL_TYPE", "Llama-3.2-3b")
# 默认使用一个独立的模拟 OpenWebUI 用户 ID。
# 如果需要联调真实 OpenWebUI 用户，可通过环境变量 OPENWEBUI_MOCK_USER_ID 覆盖。
OPENWEBUI_MOCK_USER_ID = os.getenv("OPENWEBUI_MOCK_USER_ID", "mock-openwebui-user")
OPENWEBUI_MOCK_EXPIRE_SECONDS = int(os.getenv("OPENWEBUI_MOCK_EXPIRE_SECONDS", "3600"))

payload = {
    "model_type": EDGE_FRONTEND_MOCK_MODEL_TYPE
}

def build_mock_openwebui_token(openwebui_user_id: str) -> str:
    claims = {
        "id": openwebui_user_id,
        "exp": int(time.time()) + OPENWEBUI_MOCK_EXPIRE_SECONDS,
        "jti": str(uuid.uuid4()),
    }

    if OPENWEBUI_SKIP_SIGNATURE_VERIFY:
        return jwt.encode(
            claims,
            "dev-openwebui-skip-verify",
            algorithm=OPENWEBUI_JWT_ALGORITHM,
        )

    if not OPENWEBUI_JWT_SECRET:
        raise RuntimeError("请先设置环境变量 OPENWEBUI_JWT_SECRET，再运行 OpenWebUI token 联调脚本")

    return jwt.encode(
        claims,
        OPENWEBUI_JWT_SECRET,
        algorithm=OPENWEBUI_JWT_ALGORITHM,
    )


print("🚀 边缘端正在使用 OpenWebUI token 初始化会话，并发送推理触发请求...")
try:
    if not EDGE_FRONTEND_USE_MOCK:
        print("⏭️ EDGE_FRONTEND_USE_MOCK=false，当前不启动 mock 边端前端脚本。")
        raise SystemExit(0)

    openwebui_token = build_mock_openwebui_token(OPENWEBUI_MOCK_USER_ID)

    auth_headers = {"Authorization": f"Bearer {openwebui_token}"}

    init_response = requests.post(
        SESSION_INIT_URL,
        headers=auth_headers,
        json={"edge_device_ip": EDGE_DEVICE_IP},
        timeout=10,
    )
    init_response.raise_for_status()
    init_data = init_response.json()
    session_id = init_data["session_id"]
    print("🧭 会话初始化成功:")
    print(json.dumps(init_data, indent=2, ensure_ascii=False))

    headers = {
        **auth_headers,
        "Session-Id": session_id,
    }

    response = requests.post(TRIGGER_URL, json=payload, headers=headers, timeout=10)
    response.raise_for_status()

    accepted = response.json()
    task_id = accepted["task_id"]
    print(f"📡 云端受理成功，task_id = {task_id}")
    print(json.dumps(accepted, indent=2, ensure_ascii=False))

    strategy_fetched = False
    while True:
        task_response = requests.get(
            TASK_URL_TEMPLATE.format(task_id=task_id),
            headers=auth_headers,
            timeout=10,
        )
        task_response.raise_for_status()
        task_data = task_response.json()
        print(
            f"⏱️ 任务进度 | status={task_data['status']} | "
            f"phase={task_data['phase']} | "
            f"phase_progress={task_data['phase_progress']} | "
            f"overall_progress={task_data['overall_progress']} | "
            f"message={task_data['message']} | "
            f"edge_message={task_data.get('edge_message')} | "
            f"cloud_message={task_data.get('cloud_message')}"
        )

        if task_data["phase"] == "loading" and not strategy_fetched:
            strategy_response = requests.get(
                STRATEGY_URL_TEMPLATE.format(task_id=task_id),
                headers=auth_headers,
                timeout=10,
            )
            strategy_response.raise_for_status()
            strategy_data = strategy_response.json()
            print("🧩 已拉取切分策略:")
            print(json.dumps(strategy_data, indent=2, ensure_ascii=False))

            decision = strategy_data.get("decision", {})
            print(
                "🧮 全局统计 | "
                f"edge_head_count_total={decision.get('edge_head_count_total')} | "
                f"cloud_head_count_total={decision.get('cloud_head_count_total')}"
            )

            first_layer = strategy_data.get("decision", {}).get("layer_partitions", [{}])[0]
            if first_layer:
                print(
                    "🧮 第 0 层统计 | "
                    f"edge_head_count={first_layer.get('edge_head_count')} | "
                    f"cloud_head_count={first_layer.get('cloud_head_count')} | "
                    f"head_assignments_len={len(first_layer.get('head_assignments', []))}"
                )
            strategy_fetched = True

        if task_data["status"] in {"completed", "failed"}:
            print("📦 最终任务结果:")
            print(json.dumps(task_data, indent=2, ensure_ascii=False))
            break

        time.sleep(1)
except Exception as e:
    print(f"❌ 请求失败: {e}")
