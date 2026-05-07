from app.core.config import settings
from app.services.model_registry import MODEL_REGISTRY


def _normalize_float(metrics: dict, key: str) -> float:
    return float(metrics.get(key, 0.0) or 0.0)


def _calculate_free_gpu_mem_mb(metrics: dict) -> float | None:
    gpu_total_mb = _normalize_float(metrics, "gpu_mem_total_mb")
    gpu_used_mb = _normalize_float(metrics, "gpu_mem_used_mb")
    if gpu_total_mb <= 1.0:
        return None
    return max(gpu_total_mb - gpu_used_mb, 0.0)


def _check_single_device(
    *,
    device_label: str,
    metrics: dict,
    min_free_gpu_mem_mb: float,
) -> str | None:
    memory_percent = _normalize_float(metrics, "memory_percent")
    if memory_percent >= settings.MODEL_STARTUP_MAX_MEMORY_PERCENT:
        return (
            f"{device_label} 内存使用率过高: "
            f"memory_percent={memory_percent:.2f}, "
            f"threshold={settings.MODEL_STARTUP_MAX_MEMORY_PERCENT:.2f}"
        )

    free_gpu_mem_mb = _calculate_free_gpu_mem_mb(metrics)
    if free_gpu_mem_mb is not None and free_gpu_mem_mb < min_free_gpu_mem_mb:
        return (
            f"{device_label} 剩余显存不足: "
            f"free_gpu_mem_mb={free_gpu_mem_mb:.2f}, "
            f"required={min_free_gpu_mem_mb:.2f}"
        )

    return None


def check_runtime_startup_resources(
    *,
    model_type_key: str,
    edge_metrics: dict,
    cloud_metrics: dict,
) -> list[str]:
    if not settings.MODEL_STARTUP_RESOURCE_CHECK_ENABLED:
        return []

    model_spec = MODEL_REGISTRY[model_type_key]
    failures: list[str] = []

    edge_failure = _check_single_device(
        device_label="边端设备",
        metrics=edge_metrics,
        min_free_gpu_mem_mb=float(model_spec.get("edge_min_free_gpu_mem_mb", 0.0) or 0.0),
    )
    if edge_failure:
        failures.append(edge_failure)

    cloud_failure = _check_single_device(
        device_label="云端设备",
        metrics=cloud_metrics,
        min_free_gpu_mem_mb=float(model_spec.get("cloud_min_free_gpu_mem_mb", 0.0) or 0.0),
    )
    if cloud_failure:
        failures.append(cloud_failure)

    return failures
