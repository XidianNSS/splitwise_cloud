import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv

app = FastAPI(title="Mock 算法切分服务")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.getenv("BACKEND_ENV_FILE", str(PROJECT_ROOT / "backend" / ".env")))
if not ENV_FILE.is_absolute():
    ENV_FILE = PROJECT_ROOT / ENV_FILE
load_dotenv(ENV_FILE)
ALGORITHM_USE_MOCK = os.getenv("ALGORITHM_USE_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}
ALGORITHM_DELAY_SECONDS = float(os.getenv("ALGORITHM_MOCK_DELAY_SECONDS", "6.0"))
DEFAULT_MOCK_API_URL = os.getenv("ALGORITHM_MOCK_API_URL", "http://127.0.0.1:5000/infer")
DEFAULT_MOCK_PORT = urlparse(DEFAULT_MOCK_API_URL).port or 5000
ALGORITHM_PORT = int(os.getenv("ALGORITHM_MOCK_PORT", str(DEFAULT_MOCK_PORT)))

GPT2_SAMPLE_LAYER_PARTITIONS = [
    {"layer_id": 0, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 0, "cloud_heads": 12},
    {"layer_id": 1, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 0, "cloud_heads": 12},
    {"layer_id": 2, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 0, "cloud_heads": 12},
    {"layer_id": 3, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 1, "cloud_heads": 11},
    {"layer_id": 4, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 0, "cloud_heads": 12},
    {"layer_id": 5, "head_assignments": [1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 1, "cloud_heads": 11},
    {"layer_id": 6, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 0, "cloud_heads": 12},
    {"layer_id": 7, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 0, "cloud_heads": 12},
    {"layer_id": 8, "head_assignments": [1, 1, 0, 1, 1, 0, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 2, "cloud_heads": 10},
    {"layer_id": 9, "head_assignments": [1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1], "ffn_assignment": 2, "edge_heads": 1, "cloud_heads": 11},
    {"layer_id": 10, "head_assignments": [1, 1, 1, 1, 0, 1, 0, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 2, "cloud_heads": 10},
    {"layer_id": 11, "head_assignments": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "ffn_assignment": 1, "edge_heads": 0, "cloud_heads": 12},
]

LLAMA32_3B_SAMPLE_RESPONSE = {
    "status": "ok",
    "model_type": "Llama-3.2-3b",
    "layer_partitions": [
        {"layer_id": 0, "head_assignments": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 1, "head_assignments": [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 2, "head_assignments": [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 3, "head_assignments": [1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 4, "head_assignments": [0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 5, "head_assignments": [1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 6, "head_assignments": [0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0], "ffn_assignment": 0, "edge_heads": 18, "cloud_heads": 6},
        {"layer_id": 7, "head_assignments": [1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1], "ffn_assignment": 1, "edge_heads": 6, "cloud_heads": 18},
        {"layer_id": 8, "head_assignments": [0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 9, "head_assignments": [1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 10, "head_assignments": [0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 11, "head_assignments": [1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 12, "head_assignments": [0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 13, "head_assignments": [1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 14, "head_assignments": [0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 15, "head_assignments": [1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 16, "head_assignments": [0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1], "ffn_assignment": 0, "edge_heads": 16, "cloud_heads": 8},
        {"layer_id": 17, "head_assignments": [1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0], "ffn_assignment": 1, "edge_heads": 8, "cloud_heads": 16},
        {"layer_id": 18, "head_assignments": [0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 19, "head_assignments": [1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 20, "head_assignments": [0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0], "ffn_assignment": 0, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 21, "head_assignments": [1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1], "ffn_assignment": 1, "edge_heads": 12, "cloud_heads": 12},
        {"layer_id": 22, "head_assignments": [0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1], "ffn_assignment": 0, "edge_heads": 16, "cloud_heads": 8},
        {"layer_id": 23, "head_assignments": [1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0], "ffn_assignment": 1, "edge_heads": 8, "cloud_heads": 16},
        {"layer_id": 24, "head_assignments": [0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1], "ffn_assignment": 0, "edge_heads": 14, "cloud_heads": 10},
        {"layer_id": 25, "head_assignments": [1, 0, 0, 1, 1, 1, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 1, 1, 1, 0], "ffn_assignment": 1, "edge_heads": 10, "cloud_heads": 14},
        {"layer_id": 26, "head_assignments": [0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1], "ffn_assignment": 0, "edge_heads": 8, "cloud_heads": 16},
        {"layer_id": 27, "head_assignments": [1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0], "ffn_assignment": 1, "edge_heads": 16, "cloud_heads": 8},
    ],
}


class InferRequest(BaseModel):
    model_type: str
    prompt_len: int
    env: dict


def build_generic_layer_partitions(num_layers: int, num_heads: int) -> list[dict]:
    layer_partitions = []
    for layer_id in range(num_layers):
        head_assignments = []
        for head_id in range(num_heads):
            assignment = 0 if (layer_id + head_id) % 4 == 0 else 1
            head_assignments.append(assignment)

        edge_heads = sum(1 for assignment in head_assignments if assignment == 0)
        cloud_heads = sum(1 for assignment in head_assignments if assignment == 1)
        layer_partitions.append(
            {
                "layer_id": layer_id,
                "head_assignments": head_assignments,
                "ffn_assignment": 0 if layer_id % 2 == 0 else 1,
                "edge_heads": edge_heads,
                "cloud_heads": cloud_heads,
            }
        )
    return layer_partitions


def build_strategy_response(req: InferRequest) -> dict:
    edge_spec = req.env.get("edge", {}).get("model_spec", {})
    num_layers = int(edge_spec.get("num_hidden_layers", 12) or 12)
    num_heads = int(edge_spec.get("num_attention_heads", 12) or 12)

    if req.model_type.lower() == "gpt2" and num_layers == 12 and num_heads == 12:
        layer_partitions = GPT2_SAMPLE_LAYER_PARTITIONS
    elif req.model_type.lower() in {"llama-3.2-3b", "llama-3.2-3b-instruct"}:
        return LLAMA32_3B_SAMPLE_RESPONSE
    else:
        layer_partitions = build_generic_layer_partitions(num_layers, num_heads)

    return {
        "status": "ok",
        "model_type": req.model_type,
        "layer_partitions": layer_partitions,
    }


@app.post("/infer")
async def infer_strategy(req: InferRequest):
    print(f"\n🧠 [算法端] 收到模型: {req.model_type}，prompt_len={req.prompt_len}")
    print(f"🧠 [算法端] 收到环境 JSON，正在进行切分策略计算模拟 (预计耗时{ALGORITHM_DELAY_SECONDS:.1f}秒)...")
    await asyncio.sleep(ALGORITHM_DELAY_SECONDS)
    return build_strategy_response(req)


if __name__ == "__main__":
    if not ALGORITHM_USE_MOCK:
        print("⏭️ ALGORITHM_USE_MOCK=false，当前不启动 mock 算法服务。")
        raise SystemExit(0)

    print("=========================================")
    print(f"🤖 虚拟算法服务已启动，监听 {ALGORITHM_PORT} 端口...")
    print("等待云端中枢发送策略输入 JSON...")
    print("=========================================")
    uvicorn.run(app, host="0.0.0.0", port=ALGORITHM_PORT)
