import asyncio
import logging
import os
import signal
import shlex
import socket
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.config import settings
from app.db.database import SessionLocal
from app.services.runtime_slot_service import collect_allocated_cloud_ports


@dataclass
class DecodeSlotProcessInfo:
    slot_id: str
    slot_index: int
    http_port: int
    grpc_port: int
    control_url: str
    grpc_target: str
    process_pid: int


_SLOT_PROCESSES: dict[str, subprocess.Popen] = {}
_SLOT_PROCESS_LOCK = asyncio.Lock()
logger = logging.getLogger("DecodeServerProcessManager")
_PROXY_ENV_NAMES = {
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
}


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _choose_available_port(start_port: int, *, reserved_ports: set[int] | None = None) -> int:
    reserved = reserved_ports or set()
    candidate = start_port
    while candidate in reserved or _port_in_use(candidate):
        candidate += 1
    return candidate


def allocate_cloud_slot_ports(slot_index: int, *, slot_id: str | None = None) -> tuple[int, int]:
    db = SessionLocal()
    try:
        reserved_http_ports, reserved_grpc_ports = collect_allocated_cloud_ports(
            db,
            exclude_slot_id=slot_id,
        )
    finally:
        db.close()
    http_port = _choose_available_port(
        settings.CLOUD_SLOT_HTTP_BASE_PORT + slot_index,
        reserved_ports=reserved_http_ports,
    )
    grpc_port = _choose_available_port(
        settings.CLOUD_SLOT_GRPC_BASE_PORT + slot_index,
        reserved_ports=reserved_grpc_ports.union({http_port}),
    )
    while grpc_port == http_port:
        grpc_port = _choose_available_port(grpc_port + 1, reserved_ports=reserved_grpc_ports.union({http_port}))
    return http_port, grpc_port


def current_runtime_env_metadata() -> tuple[str, str]:
    env = os.environ.copy()
    backend_env_file = env.get("BACKEND_ENV_FILE", "").strip()
    env_file_name = Path(backend_env_file).name if backend_env_file else ""
    app_env = "prod" if env_file_name == ".env.prod" else ("wyy" if env_file_name == ".env.wyy" else env.get("APP_ENV", "prod"))
    return app_env, env_file_name or env.get("ENV_FILE", ".env")

def _normalize_slot_model_device(raw_device: str) -> str:
    value = (raw_device or "").strip().lower()
    if not value:
        return ""

    if value == "cpu":
        return "cpu"

    if value.startswith(("npu:", "cuda:")):
        return value

    # CLOUD_SLOT_NPU_DEVICES=0,1 时自动解释成 npu:0,npu:1
    if value.isdigit():
        return f"npu:{value}"

    # 兼容写成 ascend:0 的情况，内部统一给 ModelSplit 使用 npu:0
    if value.startswith("ascend:"):
        return "npu:" + value.split(":", 1)[1]

    raise ValueError(f"非法 CLOUD_SLOT_NPU_DEVICES 设备项: {raw_device!r}")


def _configured_fallback_model_device() -> str:
    # 优先用已经加载进 os.environ 的 MODEL_DEVICE。
    # ModelSplit 子进程会继承这个变量；如果本函数按 slot 注入了 MODEL_DEVICE，
    # 由于 ModelSplit load_dotenv 默认不覆盖已有环境变量，所以子进程注入值优先。
    return (os.environ.get("MODEL_DEVICE") or "").strip()


def _model_device_for_cloud_slot(slot_index: int) -> str:
    devices = settings.CLOUD_SLOT_NPU_DEVICES

    if not devices:
        return _normalize_slot_model_device(_configured_fallback_model_device())

    if slot_index < len(devices):
        return _normalize_slot_model_device(devices[slot_index])

    if settings.CLOUD_SLOT_ALLOW_NPU_OVERSUBSCRIPTION:
        return _normalize_slot_model_device(devices[slot_index % len(devices)])

    raise RuntimeError(
        f"cloud slot index {slot_index} 没有对应 NPU 设备；"
        f"CLOUD_SLOT_NPU_DEVICES={','.join(devices)!r}。"
        "如果确实要多个 slot 共享 NPU，请显式设置 "
        "CLOUD_SLOT_ALLOW_NPU_OVERSUBSCRIPTION=true。"
    )

def start_decode_server_process_for_slot(slot_id: str, slot_index: int) -> DecodeSlotProcessInfo:
    http_port, grpc_port = allocate_cloud_slot_ports(slot_index, slot_id=slot_id)
    advertised_host = settings.CLOUD_RUNTIME_REAL_HOST or "127.0.0.1"
    control_url = f"http://{advertised_host}:{http_port}/load_strategy"
    grpc_target = f"{advertised_host}:{grpc_port}"

    env = os.environ.copy()
    for proxy_env_name in _PROXY_ENV_NAMES:
        env.pop(proxy_env_name, None)
    backend_env_file = env.get("BACKEND_ENV_FILE", "").strip()
    app_env, env_file_name = current_runtime_env_metadata()
    slot_model_device = _model_device_for_cloud_slot(slot_index)

    env.update({
        "APP_ENV": app_env,
        "ENV_FILE": env_file_name,
        "BACKEND_ENV_FILE": backend_env_file,
        "SCHEDULE_BACKEND_URL": settings.BACKEND_BASE_URL,
        "CLOUD_RUNTIME_PORT": str(http_port),
        "RUNTIME_PORT": str(http_port),
        "DECODE_GRPC_BIND": f"0.0.0.0:{grpc_port}",
        "DECODE_GRPC_TARGET": grpc_target,
        "CLOUD_SLOT_ID": slot_id,
        "CLOUD_SLOT_INDEX": str(slot_index),
    })

    if slot_model_device:
        env["MODEL_DEVICE"] = slot_model_device
    python_bin = settings.MODELSPLIT_PYTHON_BIN
    ascend_env_script = settings.ASCEND_ENV_SCRIPT

    source_ascend_env = ""
    if ascend_env_script:
        source_ascend_env = f"source {shlex.quote(ascend_env_script)} && "

    command = (
        f"set -eo pipefail && "
        f"echo \"[cloud-slot] slot_id={slot_id} "
        f"slot_index={slot_index} "
        f"MODEL_DEVICE=${{MODEL_DEVICE:-}} "
        f"HTTP={http_port} "
        f"GRPC={grpc_port}\" >&2 && "
        f"{source_ascend_env}"
        f"cd {shlex.quote(settings.MODELSPLIT_DEV_ROOT)} && "
        f"exec {shlex.quote(python_bin)} -m app.services.decode_server.app"
    )
    log_dir = Path('/tmp/modelsplit_phase2_logs')
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{slot_id}.out.log"
    stderr_path = log_dir / f"{slot_id}.err.log"
    stdout_handle = stdout_path.open('ab')
    stderr_handle = stderr_path.open('ab')
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    finally:
        with suppress(Exception):
            stdout_handle.close()
        with suppress(Exception):
            stderr_handle.close()
    _SLOT_PROCESSES[slot_id] = process
    logger.info(
        "启动 cloud decode slot: slot_id=%s slot_index=%s pid=%s http_port=%s grpc_port=%s "
        "model_device=%s control_url=%s grpc_target=%s python=%s root=%s",
        slot_id,
        slot_index,
        process.pid,
        http_port,
        grpc_port,
        env.get("MODEL_DEVICE", ""),
        control_url,
        grpc_target,
        python_bin,
        settings.MODELSPLIT_DEV_ROOT,
    )
    return DecodeSlotProcessInfo(
        slot_id=slot_id,
        slot_index=slot_index,
        http_port=http_port,
        grpc_port=grpc_port,
        control_url=control_url,
        grpc_target=grpc_target,
        process_pid=process.pid,
    )


def inspect_slot_process(slot_id: str) -> subprocess.Popen | None:
    process = _SLOT_PROCESSES.get(slot_id)
    if process is None:
        return None
    if process.poll() is not None:
        _SLOT_PROCESSES.pop(slot_id, None)
        return None
    return process


def stop_slot_process(slot_id: str, *, process_pid: int | None = None) -> bool:
    process = inspect_slot_process(slot_id)
    if process is not None:
        if process_pid is not None and process.pid != process_pid:
            logger.warning(
                "拒绝停止已被新进程接管的 cloud slot: slot_id=%s expected_pid=%s current_pid=%s",
                slot_id,
                process_pid,
                process.pid,
            )
            return False
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        finally:
            _SLOT_PROCESSES.pop(slot_id, None)
        return True

    if process_pid is None:
        return False

    try:
        os.kill(process_pid, signal.SIGTERM)
    except ProcessLookupError:
        _SLOT_PROCESSES.pop(slot_id, None)
        return True
    except OSError:
        return False

    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            os.kill(process_pid, 0)
        except ProcessLookupError:
            _SLOT_PROCESSES.pop(slot_id, None)
            return True
        except OSError:
            return False
        time.sleep(0.1)

    try:
        os.kill(process_pid, signal.SIGKILL)
    except ProcessLookupError:
        _SLOT_PROCESSES.pop(slot_id, None)
        return True
    except OSError:
        return False

    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            os.kill(process_pid, 0)
        except ProcessLookupError:
            _SLOT_PROCESSES.pop(slot_id, None)
            return True
        except OSError:
            return False
        time.sleep(0.1)
    return False


def _stderr_tail(slot_id: str, *, max_chars: int = 1200) -> str:
    path = Path("/tmp/modelsplit_phase2_logs") / f"{slot_id}.err.log"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_chars * 2), os.SEEK_SET)
            return handle.read().decode("utf-8", errors="replace")[-max_chars:].strip()
    except OSError:
        return ""


async def wait_for_slot_health(
    control_url: str,
    *,
    timeout_seconds: float = 30.0,
    slot_id: str | None = None,
    process_pid: int | None = None,
) -> bool:
    started = asyncio.get_event_loop().time()
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    health_url = control_url.rsplit("/load_strategy", 1)[0] + "/health"
    while asyncio.get_event_loop().time() < deadline:
        if slot_id and process_pid is not None:
            process = _SLOT_PROCESSES.get(slot_id)
            if process is not None and process.pid == process_pid:
                exit_code = process.poll()
                if exit_code is not None:
                    if _SLOT_PROCESSES.get(slot_id) is process:
                        _SLOT_PROCESSES.pop(slot_id, None)
                    logger.error(
                        "cloud slot 启动进程提前退出: slot_id=%s pid=%s exit_code=%s elapsed=%.2fs stderr_tail=%r",
                        slot_id,
                        process_pid,
                        exit_code,
                        asyncio.get_event_loop().time() - started,
                        _stderr_tail(slot_id),
                    )
                    return False
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(health_url, timeout=2.0)
                response.raise_for_status()
            return True
        except Exception:
            await asyncio.sleep(0.5)
    logger.error(
        "cloud slot 健康检查超时: slot_id=%s pid=%s timeout=%.2fs stderr_tail=%r",
        slot_id,
        process_pid,
        timeout_seconds,
        _stderr_tail(slot_id) if slot_id else "",
    )
    return False


async def start_decode_server_process_for_slot_locked(slot_id: str, slot_index: int) -> DecodeSlotProcessInfo:
    async with _SLOT_PROCESS_LOCK:
        return start_decode_server_process_for_slot(slot_id, slot_index)


async def start_decode_server_process_locked(slot_index: int) -> DecodeSlotProcessInfo:
    async with _SLOT_PROCESS_LOCK:
        slot_id = f"cloud-slot-{slot_index}"
        return start_decode_server_process_for_slot(slot_id, slot_index)
