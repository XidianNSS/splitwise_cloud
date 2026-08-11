import json
import logging

import httpx

from app.core.config import settings
from app.services.model_registry import (
    MODEL_REGISTRY,
    build_fixed_runtime_decision,
    uses_fixed_runtime_strategy,
)

logger = logging.getLogger("AlgorithmDispatcher")

DEFAULT_PROMPT_LEN = 96


def normalize_device_runtime_label(metrics: dict) -> str:
    total_mb = float(metrics.get("gpu_mem_total_mb", 0.0) or 0.0)
    if total_mb <= 1.0:
        return "cpu"

    if metrics.get("accelerator_type") == "ascend":
        return "npu:0"

    return "cuda:0"


def build_algorithm_request_payload(
    *,
    model_type: str,
    model_type_key: str,
    edge_metrics: dict,
    cloud_metrics: dict,
    network_metrics: dict,
    edge_storage_limit_gb: float,
) -> dict:
    model_spec = MODEL_REGISTRY[model_type_key]
    return {
        "model_type": model_type,
        "prompt_len": DEFAULT_PROMPT_LEN,
        "env": {
            "edge": {
                "device": normalize_device_runtime_label(edge_metrics),
                "model_spec": {
                    "num_hidden_layers": model_spec["num_hidden_layers"],
                    "num_attention_heads": model_spec["num_attention_heads"],
                },
                "metrics": edge_metrics,
                "storage_limit_gb": round(edge_storage_limit_gb, 2),
            },
            "cloud": {
                "device": normalize_device_runtime_label(cloud_metrics),
                "model_spec": {
                    "num_hidden_layers": model_spec["num_hidden_layers"],
                    "num_attention_heads": model_spec["num_attention_heads"],
                },
                "metrics": cloud_metrics,
            },
            "network": network_metrics,
        },
    }


def normalize_algorithm_decision(model_type: str, payload: dict) -> dict:
    status = str(payload.get("status", "")).strip().lower()
    if status and status != "ok":
        raise ValueError(f"算法模块返回异常状态: {status}")

    raw_layers = payload.get("layer_partitions")
    if not isinstance(raw_layers, list) or not raw_layers:
        raise ValueError("算法模块未返回有效的 layer_partitions")

    normalized_layers = []
    for raw_layer in raw_layers:
        if not isinstance(raw_layer, dict):
            raise ValueError("算法模块返回的 layer_partitions 格式非法")

        head_assignments = [int(assignment) for assignment in raw_layer.get("head_assignments", [])]
        edge_head_count = sum(1 for assignment in head_assignments if assignment == 0)
        cloud_head_count = sum(1 for assignment in head_assignments if assignment == 1)

        provided_edge_heads = raw_layer.get("edge_heads")
        provided_cloud_heads = raw_layer.get("cloud_heads")
        if isinstance(provided_edge_heads, int):
            edge_head_count = provided_edge_heads
        if isinstance(provided_cloud_heads, int):
            cloud_head_count = provided_cloud_heads

        normalized_layers.append(
            {
                "layer_id": int(raw_layer.get("layer_id", 0)),
                "head_assignments": head_assignments,
                "ffn_assignment": int(raw_layer.get("ffn_assignment", 0)),
                "edge_head_count": edge_head_count,
                "cloud_head_count": cloud_head_count,
            }
        )

    return {
        "model_type": str(payload.get("model_type") or model_type),
        "layer_partitions": normalized_layers,
    }


def derive_edge_storage_limit_gb_from_metrics(edge_metrics: dict) -> float:
    """
    当前将 storage_limit_gb 解释为“边端可用显存预算(GB)”。
    直接由已有 GPU 指标推导：
    available_vram_gb = (gpu_mem_total_mb - gpu_mem_used_mb) / 1024
    若 GPU 指标异常，则回退为 16GB。
    """
    gpu_total_mb = float(edge_metrics.get("gpu_mem_total_mb", 0.0) or 0.0)
    gpu_used_mb = float(edge_metrics.get("gpu_mem_used_mb", 0.0) or 0.0)
    gpu_available_mb = gpu_total_mb - gpu_used_mb

    if gpu_total_mb <= 1.0 or gpu_available_mb <= 0:
        logger.warning("边端可用显存预算推导失败，已回退为默认 16GB: metrics=%s", edge_metrics)
        return 16.0

    return round(gpu_available_mb / 1024.0, 2)


async def request_algorithm_decision(task_id: str, model_type: str, raw_input_json: dict) -> dict:
    logger.info(
        "发送策略计算请求: task_id=%s, payload=%s",
        task_id,
        json.dumps(raw_input_json, ensure_ascii=False),
    )

    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(
            settings.ALGORITHM_API_URL,
            json=raw_input_json,
            timeout=settings.ALGORITHM_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

    decision_result = normalize_algorithm_decision(model_type, response.json())
    logger.info(
        "策略计算完成: task_id=%s, algorithm_result=%s",
        task_id,
        json.dumps(decision_result, ensure_ascii=False),
    )
    return decision_result


async def resolve_runtime_decision(
    task_id: str,
    model_type: str,
    model_type_key: str,
    raw_input_json: dict,
) -> dict:
    """Resolve either a deterministic protocol or an algorithm decision."""
    if uses_fixed_runtime_strategy(model_type_key):
        decision = build_fixed_runtime_decision(model_type, model_type_key)
        logger.info(
            "使用固定 runtime 协议，跳过切分算法服务: task_id=%s model=%s",
            task_id,
            model_type,
        )
        return decision
    return await request_algorithm_decision(task_id, model_type, raw_input_json)
