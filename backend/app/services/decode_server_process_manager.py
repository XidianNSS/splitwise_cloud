import asyncio
import os
import signal
import shlex
import socket
import subprocess
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


def allocate_cloud_slot_ports(slot_index: int) -> tuple[int, int]:
    db = SessionLocal()
    try:
        reserved_http_ports, reserved_grpc_ports = collect_allocated_cloud_ports(db)
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


def start_decode_server_process_for_slot(slot_id: str, slot_index: int) -> DecodeSlotProcessInfo:
    http_port, grpc_port = allocate_cloud_slot_ports(slot_index)
    advertised_host = settings.CLOUD_RUNTIME_REAL_HOST or "127.0.0.1"
    control_url = f"http://{advertised_host}:{http_port}/load_strategy"
    grpc_target = f"{advertised_host}:{grpc_port}"

    env = os.environ.copy()
    backend_env_file = env.get("BACKEND_ENV_FILE", "").strip()
    app_env, env_file_name = current_runtime_env_metadata()
    env.update({
        "APP_ENV": app_env,
        "ENV_FILE": env_file_name,
        "BACKEND_ENV_FILE": backend_env_file,
        "SCHEDULE_BACKEND_URL": settings.BACKEND_BASE_URL,
        "CLOUD_RUNTIME_PORT": str(http_port),
        "RUNTIME_PORT": str(http_port),
        "DECODE_GRPC_BIND": f"0.0.0.0:{grpc_port}",
        "DECODE_GRPC_TARGET": grpc_target,
    })
    python_bin = "/home/nss-d/anaconda3/envs/modelsplit/bin/python"
    command = (
        f"cd {shlex.quote(settings.MODELSPLIT_DEV_ROOT)} && "
        f"{shlex.quote(python_bin)} -m app.services.decode_server.app"
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
        )
    finally:
        with suppress(Exception):
            stdout_handle.close()
        with suppress(Exception):
            stderr_handle.close()
    _SLOT_PROCESSES[slot_id] = process
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

    import time
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


async def wait_for_slot_health(control_url: str, *, timeout_seconds: float = 30.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    health_url = control_url.rsplit("/load_strategy", 1)[0] + "/health"
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(health_url, timeout=2.0)
                response.raise_for_status()
            return True
        except Exception:
            await asyncio.sleep(0.5)
    return False


async def start_decode_server_process_for_slot_locked(slot_id: str, slot_index: int) -> DecodeSlotProcessInfo:
    async with _SLOT_PROCESS_LOCK:
        return start_decode_server_process_for_slot(slot_id, slot_index)


async def start_decode_server_process_locked(slot_index: int) -> DecodeSlotProcessInfo:
    async with _SLOT_PROCESS_LOCK:
        slot_id = f"cloud-slot-{slot_index}"
        return start_decode_server_process_for_slot(slot_id, slot_index)
