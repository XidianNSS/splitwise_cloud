import re

import httpx

from app.core.config import settings


def extract_ip(device_value: str) -> str | None:
    ip_match = re.search(r"(?:\d{1,3}\.){3}\d{1,3}", device_value)
    return ip_match.group(0) if ip_match else None


def resolve_runtime_control_target(node_role: str, device_ip: str) -> tuple[str, int, str]:
    normalized_role = node_role.lower()
    if normalized_role == "edge":
        use_mock = settings.EDGE_RUNTIME_USE_MOCK
        port = settings.EDGE_RUNTIME_MOCK_PORT if use_mock else settings.EDGE_RUNTIME_REAL_PORT
        target_host = settings.EDGE_RUNTIME_MOCK_HOST if use_mock else (settings.EDGE_RUNTIME_REAL_HOST or device_ip)
    elif normalized_role == "cloud":
        use_mock = settings.CLOUD_RUNTIME_USE_MOCK
        port = settings.CLOUD_RUNTIME_MOCK_PORT if use_mock else settings.CLOUD_RUNTIME_REAL_PORT
        target_host = settings.CLOUD_RUNTIME_MOCK_HOST if use_mock else (settings.CLOUD_RUNTIME_REAL_HOST or device_ip)
    else:
        raise ValueError(f"不支持的 runtime 角色: {node_role}")

    mode = "mock" if use_mock else "real"
    return target_host, port, mode


def build_runtime_control_url(node_role: str, device_ip: str) -> str:
    target_host, port, _mode = resolve_runtime_control_target(node_role, device_ip)
    control_path = settings.RUNTIME_CONTROL_PATH or "/load_strategy"
    return f"http://{target_host}:{port}{control_path}"


async def dispatch_strategy_to_runtime(
    *,
    node_role: str,
    device_ip: str,
    payload: dict,
    control_url: str | None = None,
) -> dict:
    runtime_url = control_url or build_runtime_control_url(node_role, device_ip)
    async with httpx.AsyncClient() as client:
        response = await client.post(runtime_url, json=payload, timeout=5.0)
        response.raise_for_status()
        if response.content:
            return response.json()
    return {"status": "accepted"}
