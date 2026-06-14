"""Prometheus 指标查询与缓存服务。"""
import asyncio
import logging
import time
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger("PrometheusMetrics")


class PrometheusMetricsCache:
    """缓存按 (accelerator_type, IP) 的 Prometheus 指标结果。"""

    def __init__(self, ttl_seconds: float = 15.0):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[dict, float]] = {}

    def get(self, key: str) -> Optional[dict]:
        entry = self._cache.get(key)
        if not entry:
            return None
        metrics, ts = entry
        if time.monotonic() - ts > self.ttl_seconds:
            self._cache.pop(key, None)
            return None
        return metrics

    def set(self, key: str, metrics: dict) -> None:
        self._cache[key] = (metrics, time.monotonic())

    def clear(self) -> None:
        self._cache.clear()

# 聚合查询：返回给调度算法的整机视图
PROMETHEUS_QUERY_TEMPLATES = {
    "nvidia": {
        "cpu": '100 - (avg(rate(node_cpu_seconds_total{{instance=~"{ip_regex}",mode="idle"}}[1m])) * 100)',
        "mem": '100 * (1 - node_memory_MemAvailable_bytes{{instance=~"{ip_regex}"}} / node_memory_MemTotal_bytes{{instance=~"{ip_regex}"}})',
        "gpu_util": 'avg(max_over_time(DCGM_FI_DEV_GPU_UTIL{{instance=~"{ip_regex}"}}[2m]))',
        "gpu_used": 'sum(DCGM_FI_DEV_FB_USED{{instance=~"{ip_regex}"}})',
        "gpu_free": 'sum(DCGM_FI_DEV_FB_FREE{{instance=~"{ip_regex}"}})',
    },
    "ascend": {
        "cpu": '100 - (avg(rate(node_cpu_seconds_total{{instance=~"{ip_regex}",mode="idle"}}[1m])) * 100)',
        "mem": '100 * (1 - node_memory_MemAvailable_bytes{{instance=~"{ip_regex}"}} / node_memory_MemTotal_bytes{{instance=~"{ip_regex}"}})',
        "gpu_util": 'avg(ascend_npu_aicore_usage_ratio{{instance=~"{ip_regex}"}}) * 100',
        "gpu_used": 'sum(ascend_npu_hbm_used_megabytes{{instance=~"{ip_regex}"}})',
        "gpu_free": 'sum(ascend_npu_hbm_capacity_megabytes{{instance=~"{ip_regex}"}}) - sum(ascend_npu_hbm_used_megabytes{{instance=~"{ip_regex}"}})',
    },
}

# 分片查询：每张加速卡一行，给前端展示
PER_CHIP_QUERY_TEMPLATES = {
    "nvidia": {
        "util": 'max_over_time(DCGM_FI_DEV_GPU_UTIL{{instance=~"{ip_regex}"}}[2m])',
        "used_mb": 'DCGM_FI_DEV_FB_USED{{instance=~"{ip_regex}"}}',
        "total_mb": 'DCGM_FI_DEV_FB_USED{{instance=~"{ip_regex}"}} + DCGM_FI_DEV_FB_FREE{{instance=~"{ip_regex}"}}',
    },
    "ascend": {
        "util": 'ascend_npu_aicore_usage_ratio{{instance=~"{ip_regex}"}} * 100',
        "used_mb": 'ascend_npu_hbm_used_megabytes{{instance=~"{ip_regex}"}}',
        "total_mb": 'ascend_npu_hbm_capacity_megabytes{{instance=~"{ip_regex}"}}',
    },
}

CHIP_ID_LABEL = {
    "nvidia": "gpu",
    "ascend": "npu_id",
}

prometheus_metrics_cache = PrometheusMetricsCache(ttl_seconds=settings.PROMETHEUS_CACHE_SECONDS)

def resolve_accelerator_type(ip: str) -> str:
    """按 IP 白名单决定走 ascend 模板还是 nvidia 模板。"""
    return "ascend" if ip in settings.ASCEND_IPS else "nvidia"


async def query_prom(client: httpx.AsyncClient, query: str) -> float:
    try:
        response = await client.get(
            f"{settings.PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=settings.PROMETHEUS_QUERY_TIMEOUT,
        )
        data = response.json()
        result = data.get("data", {}).get("result", [])
        if result:
            return float(result[0].get("value", [0, "0"])[1])
        logger.warning("Prometheus 查询结果为空，已回退为 0.0: %s", query)
    except Exception as exc:
        logger.warning("Prometheus 查询失败，已回退为 0.0: %s, error=%s", query, exc)
    return 0.0

async def query_prom_series(client: httpx.AsyncClient, query: str) -> list[dict]:
    """返回 [{labels, value}, ...]，用于分片展示。"""
    try:
        response = await client.get(
            f"{settings.PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=settings.PROMETHEUS_QUERY_TIMEOUT,
        )
        results = response.json().get("data", {}).get("result", [])
        return [
            {"labels": r.get("metric", {}), "value": float(r.get("value", [0, "0"])[1])}
            for r in results
        ]
    except Exception as exc:
        logger.warning("Prometheus 分片查询失败: %s, error=%s", query, exc)
        return []


async def fetch_metrics_from_prometheus(ip: str) -> dict:
    accelerator_type = resolve_accelerator_type(ip)
    ip_regex = f"^{ip}:.*"
    agg_templates = PROMETHEUS_QUERY_TEMPLATES[accelerator_type]
    agg_queries = {name: tpl.format(ip_regex=ip_regex) for name, tpl in agg_templates.items()}
    chip_templates = PER_CHIP_QUERY_TEMPLATES.get(accelerator_type, {})
    chip_queries = {name: tpl.format(ip_regex=ip_regex) for name, tpl in chip_templates.items()}
    
    async with httpx.AsyncClient(trust_env=False) as client:
        agg_results = await asyncio.gather(*(query_prom(client, q) for q in agg_queries.values()))
        chip_series_results = await asyncio.gather(
            *(query_prom_series(client, q) for q in chip_queries.values())
        )

    agg_values = dict(zip(agg_queries.keys(), agg_results))
    cpu = agg_values.get("cpu", 0.0)
    mem = agg_values.get("mem", 0.0)
    gpu_util = agg_values.get("gpu_util", 0.0)
    gpu_used = agg_values.get("gpu_used", 0.0)
    gpu_free = agg_values.get("gpu_free", 0.0)
    gpu_total = gpu_used + gpu_free

    chips: dict[str, dict] = {}
    chip_id_label = CHIP_ID_LABEL.get(accelerator_type)
    if chip_id_label and chip_series_results:
        for metric_name, series_list in zip(chip_queries.keys(), chip_series_results):
            for s in series_list:
                cid = s["labels"].get(chip_id_label)
                if cid is None:
                    continue
                entry = chips.setdefault(cid, {"chip_id": cid})
                for label_key in ("pcie_bus", "product_name"):
                    if label_key in s["labels"]:
                        entry.setdefault(label_key, s["labels"][label_key])
                entry[metric_name] = round(s["value"], 2)

    return {
        "cpu_percent": round(cpu, 2),
        "memory_percent": round(mem, 2),
        "gpu_util_percent": round(gpu_util, 2),
        "gpu_mem_used_mb": round(gpu_used, 2),
        "gpu_mem_total_mb": round(gpu_total, 2) if gpu_total > 0 else 1.0,
        "accelerator_type": accelerator_type,
        "chips": sorted(chips.values(), key=lambda c: c["chip_id"]),
    }


async def get_prometheus_metrics(ip: str) -> dict:
    accelerator_type = resolve_accelerator_type(ip)
    cache_key = f"{accelerator_type}:{ip}"
    cached = prometheus_metrics_cache.get(cache_key)
    if cached is not None:
        logger.debug("Prometheus 指标命中缓存: key=%s", cache_key)
        return cached

    metrics = await fetch_metrics_from_prometheus(ip)
    prometheus_metrics_cache.set(cache_key, metrics)
    return metrics
