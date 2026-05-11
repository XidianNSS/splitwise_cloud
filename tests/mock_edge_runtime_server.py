import asyncio
import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from dotenv import load_dotenv
from pydantic import BaseModel

app = FastAPI(title="Mock Edge Model Service")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.getenv("BACKEND_ENV_FILE", str(PROJECT_ROOT / "backend" / ".env")))
if not ENV_FILE.is_absolute():
    ENV_FILE = PROJECT_ROOT / ENV_FILE
load_dotenv(ENV_FILE)

BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8010")
RUNTIME_CALLBACK_URL = f"{BACKEND_BASE_URL}/api/v1/schedule/runtime_callback/edge"
EDGE_RUNTIME_USE_MOCK = os.getenv("EDGE_RUNTIME_USE_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}
RUNTIME_PORT = int(os.getenv("EDGE_RUNTIME_MOCK_PORT", os.getenv("EDGE_RUNTIME_PORT", "7001")))
STEP_DELAY_SECONDS = float(os.getenv("EDGE_RUNTIME_STEP_DELAY_SECONDS", "2.5"))

MODEL_PROFILES = {
    "gpt2": {
        "display_name": "GPT-2",
        "checkpoints": [
            (10, "边端已接收 GPT-2 策略，开始准备加载"),
            (25, "边端正在校验 GPT-2 切分配置"),
            (40, "边端正在加载 GPT-2 权重"),
            (60, "边端正在初始化 GPT-2 推理上下文"),
            (80, "边端正在预热 GPT-2 运行环境"),
            (92, "边端 GPT-2 即将就绪"),
            (100, "边端 GPT-2 加载完成"),
        ],
    },
    "tinyllama": {
        "display_name": "TinyLlama",
        "checkpoints": [
            (12, "边端已接收 TinyLlama 策略，开始准备加载"),
            (28, "边端正在校验 TinyLlama 切分配置"),
            (45, "边端正在加载 TinyLlama 权重"),
            (62, "边端正在初始化 TinyLlama 推理上下文"),
            (82, "边端正在预热 TinyLlama 运行环境"),
            (94, "边端 TinyLlama 即将就绪"),
            (100, "边端 TinyLlama 加载完成"),
        ],
    },
    "llama-3.2-3b": {
        "display_name": "Llama-3.2-3b",
        "checkpoints": [
            (8, "边端已接收 Llama-3.2-3b 策略，开始准备加载"),
            (18, "边端正在分配 Llama-3.2-3b 显存"),
            (32, "边端正在校验 Llama-3.2-3b 切分配置"),
            (50, "边端正在加载 Llama-3.2-3b 权重"),
            (72, "边端正在初始化 Llama-3.2-3b 推理上下文"),
            (90, "边端正在预热 Llama-3.2-3b 运行环境"),
            (100, "边端 Llama-3.2-3b 加载完成"),
        ],
    },
}


class RuntimeDispatchPayload(BaseModel):
    task_id: str
    model_type: str
    decision: dict


@app.post("/load_strategy")
async def load_strategy(payload: RuntimeDispatchPayload):
    print(
        f"📱 [边端模型推理服务] 收到任务 {payload.task_id} 的模型启动请求，"
        f"目标模型 = {payload.model_type}，开始模拟加载..."
    )
    asyncio.create_task(simulate_loading(payload.task_id, payload.model_type))
    return {"status": "accepted", "message": "edge model service startup accepted"}


async def simulate_loading(task_id: str, model_type: str):
    profile = MODEL_PROFILES.get(
        model_type.lower(),
        {
            "display_name": model_type,
            "checkpoints": [
                (10, f"边端已接收 {model_type} 策略，开始准备加载"),
                (25, f"边端正在校验 {model_type} 切分配置"),
                (40, f"边端正在加载 {model_type} 权重"),
                (60, f"边端正在初始化 {model_type} 推理上下文"),
                (80, f"边端正在预热 {model_type} 运行环境"),
                (92, f"边端 {model_type} 即将就绪"),
                (100, f"边端 {model_type} 加载完成"),
            ],
        },
    )
    checkpoints = profile["checkpoints"]
    async with httpx.AsyncClient() as client:
        for progress, message in checkpoints:
            await asyncio.sleep(STEP_DELAY_SECONDS)
            await client.post(
                RUNTIME_CALLBACK_URL,
                json={
                    "task_id": task_id,
                    "status": "ready" if progress == 100 else "loading",
                    "progress": progress,
                    "message": message,
                },
            )


if __name__ == "__main__":
    if not EDGE_RUNTIME_USE_MOCK:
        print("⏭️ EDGE_RUNTIME_USE_MOCK=false，当前不启动 mock 边端模型推理服务。")
        raise SystemExit(0)

    print("=========================================")
    print(f"📱 Mock 边端模型推理服务已启动，监听 {RUNTIME_PORT} 端口...")
    print(f"📱 单步进度间隔: {STEP_DELAY_SECONDS:.1f} 秒")
    print("=========================================")
    uvicorn.run(app, host="0.0.0.0", port=RUNTIME_PORT)
