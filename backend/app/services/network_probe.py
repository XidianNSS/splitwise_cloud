"""网络探测与缓存服务。"""
import asyncio
import json
import logging
import re
import shutil
import time
from typing import Optional

from app.core.config import settings

logger = logging.getLogger("NetworkProbe")


class ProbeCache:
    """缓存边云 IP 对的网络指标结果。"""

    def __init__(self, ttl_seconds: float = 30.0):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, str], tuple[dict, float]] = {}

    def get(self, edge_ip: str, cloud_ip: str) -> Optional[dict]:
        key = (edge_ip, cloud_ip)
        entry = self._cache.get(key)
        if not entry:
            return None

        metrics, ts = entry
        if time.monotonic() - ts > self.ttl_seconds:
            self._cache.pop(key, None)
            return None
        return metrics

    def set(self, edge_ip: str, cloud_ip: str, metrics: dict) -> None:
        self._cache[(edge_ip, cloud_ip)] = (metrics, time.monotonic())

    def clear(self) -> None:
        self._cache.clear()


network_probe_cache = ProbeCache(ttl_seconds=settings.NETWORK_PROBE_CACHE_SECONDS)
probe_semaphore = asyncio.Semaphore(max(1, settings.NETWORK_MAX_CONCURRENT_PROBES))


async def ping_host_with_system_ping(host: str, count: int, timeout: float) -> tuple[float | None, float | None]:
    if not shutil.which("ping"):
        logger.info("未检测到系统 ping 命令，网络 RTT 探测将回退到默认值")
        return None, None

    cmd = [
        "ping",
        "-c",
        str(count),
        "-W",
        str(max(1, int(timeout))),
        host,
    ]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=(count * timeout) + 2)
        output = (stdout or b"").decode("utf-8", errors="ignore")
        if process.returncode not in {0, 1}:
            logger.warning(
                "系统 ping 执行失败，已回退到默认值: host=%s, returncode=%s, stderr=%s",
                host,
                process.returncode,
                (stderr or b"").decode("utf-8", errors="ignore").strip(),
            )
            return None, None

        packet_loss_match = re.search(r"(\d+(?:\.\d+)?)%\s+packet loss", output)
        rtt_match = re.search(
            r"rtt min/avg/max(?:/mdev)? = \d+(?:\.\d+)?/(\d+(?:\.\d+)?)/",
            output,
        )
        if not packet_loss_match or not rtt_match:
            logger.warning("系统 ping 输出无法解析，已回退到默认值: host=%s, output=%s", host, output.strip())
            return None, None

        avg_rtt = round(float(rtt_match.group(1)), 2)
        packet_loss = round(float(packet_loss_match.group(1)), 2)
        return avg_rtt, packet_loss
    except asyncio.TimeoutError:
        logger.warning("系统 ping 超时，已回退到默认值: host=%s", host)
        if process and process.returncode is None:
            process.kill()
            await process.communicate()
        return None, None
    except Exception as exc:
        logger.warning("系统 ping 探测失败，已回退到默认值: host=%s, error=%s", host, exc)
        return None, None


async def ping_host(host: str, count: int, timeout: float) -> tuple[float | None, float | None]:
    return await ping_host_with_system_ping(host, count, timeout)


def _extract_iperf_bits_per_second(payload: dict) -> float | None:
    end = payload.get("end", {}) if isinstance(payload, dict) else {}
    candidates = [
        end.get("sum_received", {}),
        end.get("sum_sent", {}),
        end.get("sum", {}),
    ]
    for summary in candidates:
        if not isinstance(summary, dict):
            continue
        bits_per_second = summary.get("bits_per_second")
        if isinstance(bits_per_second, (int, float)) and bits_per_second > 0:
            return float(bits_per_second)
    return None


async def _run_iperf_bandwidth(target_ip: str, *, reverse: bool) -> float:
    cmd = [
        "iperf3",
        "-c",
        target_ip,
        "-p",
        str(settings.NETWORK_IPERF3_PORT),
        "-t",
        str(settings.NETWORK_IPERF3_DURATION_SECONDS),
        "--json",
    ]
    if reverse:
        cmd.insert(-1, "-R")

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.NETWORK_IPERF3_TIMEOUT_SECONDS,
        )

        payload = {}
        if stdout:
            try:
                payload = json.loads(stdout.decode("utf-8", errors="ignore"))
            except json.JSONDecodeError:
                payload = {}

        bits_per_second = _extract_iperf_bits_per_second(payload)
        if bits_per_second is not None:
            return round(bits_per_second / 1_000_000.0, 2)

        logger.warning(
            "iperf3 带宽探测失败，已回退到默认值: target_ip=%s, reverse=%s, returncode=%s, stderr=%s",
            target_ip,
            reverse,
            process.returncode if process else None,
            (stderr or b"").decode("utf-8", errors="ignore").strip(),
        )
        return 0.0
    except asyncio.TimeoutError:
        logger.warning("iperf3 带宽探测超时，已回退到默认值: target_ip=%s, reverse=%s", target_ip, reverse)
        if process and process.returncode is None:
            process.kill()
            await process.communicate()
        return 0.0
    except Exception as exc:
        logger.warning("iperf3 带宽探测异常，已回退到默认值: target_ip=%s, reverse=%s, error=%s", target_ip, reverse, exc)
        return 0.0


async def measure_bandwidth(target_ip: str) -> dict:
    if not settings.NETWORK_ENABLE_IPERF3:
        return {
            "cloud_to_edge_mbps": 0.0,
            "edge_to_cloud_mbps": 0.0,
            "effective_mbps": 0.0,
        }
    if not shutil.which("iperf3"):
        logger.info("未检测到 iperf3，可用带宽将回退到默认值")
        return {
            "cloud_to_edge_mbps": 0.0,
            "edge_to_cloud_mbps": 0.0,
            "effective_mbps": 0.0,
        }

    cloud_to_edge_mbps = await _run_iperf_bandwidth(target_ip, reverse=False)
    edge_to_cloud_mbps = await _run_iperf_bandwidth(target_ip, reverse=True)

    positive_values = [value for value in (cloud_to_edge_mbps, edge_to_cloud_mbps) if value > 0]
    effective_mbps = round(min(positive_values), 2) if positive_values else 0.0

    return {
        "cloud_to_edge_mbps": cloud_to_edge_mbps,
        "edge_to_cloud_mbps": edge_to_cloud_mbps,
        "effective_mbps": effective_mbps,
    }


async def compute_network_metrics(edge_ip: str, cloud_ip: str) -> dict:
    default_metrics = {
        "edge_rtt_ms": settings.NETWORK_DEFAULT_EDGE_RTT_MS,
        "cloud_rtt_ms": settings.NETWORK_DEFAULT_CLOUD_RTT_MS,
        "edge_to_cloud_rtt_ms": settings.NETWORK_DEFAULT_EDGE_RTT_MS,
        "estimated_bandwidth_mbps": settings.NETWORK_DEFAULT_BANDWIDTH_MBPS,
        "packet_loss": settings.NETWORK_DEFAULT_PACKET_LOSS,
    }

    async with probe_semaphore:
        edge_ping, cloud_ping, bandwidth_result = await asyncio.gather(
            ping_host(edge_ip, settings.NETWORK_PING_COUNT, settings.NETWORK_PING_TIMEOUT_SECONDS),
            ping_host(cloud_ip, settings.NETWORK_PING_COUNT, settings.NETWORK_PING_TIMEOUT_SECONDS),
            measure_bandwidth(edge_ip),
        )

    edge_rtt, edge_loss = edge_ping
    cloud_rtt, cloud_loss = cloud_ping

    resolved_edge_rtt = default_metrics["edge_rtt_ms"] if edge_rtt is None else edge_rtt
    resolved_cloud_rtt = default_metrics["cloud_rtt_ms"] if cloud_rtt is None else cloud_rtt
    measured_bandwidth = float(bandwidth_result.get("effective_mbps", 0.0) or 0.0)
    resolved_bandwidth = measured_bandwidth if measured_bandwidth > 0 else default_metrics["estimated_bandwidth_mbps"]

    loss_candidates = [loss for loss in (edge_loss, cloud_loss) if loss is not None]
    resolved_packet_loss = round(max(loss_candidates), 2) if loss_candidates else default_metrics["packet_loss"]

    metrics = {
        "edge_rtt_ms": round(resolved_edge_rtt, 2),
        "cloud_rtt_ms": round(resolved_cloud_rtt, 2),
        "edge_to_cloud_rtt_ms": round(resolved_edge_rtt, 2),
        "estimated_bandwidth_mbps": round(resolved_bandwidth, 2),
        "packet_loss": resolved_packet_loss,
    }
    logger.info(
        "网络状态采集完成: edge_ip=%s, cloud_ip=%s, metrics=%s, bandwidth_detail=%s",
        edge_ip,
        cloud_ip,
        metrics,
        bandwidth_result,
    )
    return metrics


async def get_network_metrics(edge_ip: str, cloud_ip: str) -> dict:
    cached = network_probe_cache.get(edge_ip, cloud_ip)
    if cached is not None:
        logger.debug("网络探测命中缓存: edge_ip=%s, cloud_ip=%s", edge_ip, cloud_ip)
        return cached

    metrics = await compute_network_metrics(edge_ip, cloud_ip)
    network_probe_cache.set(edge_ip, cloud_ip, metrics)
    return metrics
