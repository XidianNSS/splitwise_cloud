"""Opt-in isolated real-weight scheduler validation on one unoccupied Ascend card.

Uses private ports, registries, SQLite, logs, and test processes. Never updates
production deployment files. This is a same-host test, not a three-host rollout.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jwt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modelsplit-root", required=True, type=Path)
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--model-name", default="DeepSeek-R1-Distill-Llama-8B")
    parser.add_argument("--physical-npu", required=True, type=int)
    parser.add_argument("--base-port", type=int, default=28310)
    parser.add_argument("--policy-url", default="http://10.144.144.6:8050/infer")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runtime_root = args.modelsplit_root.resolve()
    if not runtime_root.name.startswith("ModelSplit_model_adaptation_tmp_"):
        raise SystemExit("Select the isolated ModelSplit development clone")
    if args.physical_npu == 2:
        raise SystemExit("Physical NPU 2 is reserved; this validator must not use it")
    mapping = subprocess.check_output(["npu-smi", "info", "-m"], text=True)
    match = re.search(rf"^\s*{args.physical_npu}\s+0\s+(\d+)\s", mapping, re.M)
    if match is None:
        raise SystemExit("Cannot prove the selected physical-to-logical NPU mapping")
    visible = match.group(1)

    def assert_no_other_card_users(allowed=()):
        text = subprocess.check_output(["npu-smi", "info"], text=True)
        users = {int(pid) for pid in re.findall(
            rf"\|\s*{args.physical_npu}\s+0\s*\|\s*(\d+)\s*\|", text
        )}
        opened = subprocess.run(
            ["fuser", f"/dev/davinci{args.physical_npu}"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if opened.returncode not in (0, 1):
            raise RuntimeError("Cannot verify NPU device file owners")
        users.update(int(pid) for pid in opened.stdout.split() if pid.isdigit())
        unexpected = users - set(allowed)
        if unexpected:
            raise RuntimeError(f"Selected NPU is occupied by other processes: {sorted(unexpected)}")

    assert_no_other_card_users()
    backend_port, edge_port, coordinator_port, openai_port, prefill_grpc = range(args.base_port, args.base_port + 5)
    cloud_port, cloud_grpc = args.base_port + 10, args.base_port + 11
    for port in (backend_port, edge_port, coordinator_port, openai_port, prefill_grpc, cloud_port, cloud_grpc):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))

    directory = root / ".private" / "deepseek8b-validation" / f"schedule-{int(time.time())}"
    directory.mkdir(parents=True)
    registry = directory / "registry.json"
    registry.write_text(json.dumps({"models": [{
        "model_type": args.model_name, "model_path": str(args.model_path.resolve()),
    }]}))
    report = {"model": args.model_name, "physical_npu": args.physical_npu,
              "visible_device": visible, "same_host_validation": True, "directory": str(directory)}
    report_path = directory / "result.json"
    processes = []
    handles = []
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            env.pop(key)
    key = secrets.token_hex(32)
    base = f"http://127.0.0.1:{backend_port}"
    env.update({
        "ASCEND_RT_VISIBLE_DEVICES": visible, "CUDA_VISIBLE_DEVICES": "",
        "MODEL_DEVICE": "npu:0", "MODEL_NAME": args.model_name,
        "ENV_FILE": "/dev/null", "BACKEND_ENV_FILE": "/dev/null",
        "APP_ENV": "deepseek_validation", "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "4", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "SQLITE_DB_PATH": str(directory / "scheduler.db"),
        "SERVER_HOST": "127.0.0.1", "SERVER_PORT": str(backend_port),
        "SERVER_PUBLIC_BASE_URL": base, "BACKEND_BASE_URL": base, "SCHEDULE_BACKEND_URL": base,
        "OPENWEBUI_JWT_SECRET": key, "OPENWEBUI_SKIP_SIGNATURE_VERIFY": "false",
        "SECRET_KEY": key, "RUNTIME_INTEGRITY_TOKEN": key,
        "ADMIN_USERNAME": "isolated-validation", "ADMIN_PASSWORD": secrets.token_hex(24),
        "MODELSPLIT_DEV_ROOT": str(runtime_root),
        "MODELSPLIT_PYTHON_BIN": "/home/miniconda3/envs/modelsplit/bin/python",
        "ASCEND_ENV_SCRIPT": "/usr/local/Ascend/ascend-toolkit/set_env.sh",
        "CLOUD_SLOT_MAX_COUNT": "1", "CLOUD_SLOT_NPU_DEVICES": "0",
        "CLOUD_SLOT_HTTP_BASE_PORT": str(cloud_port), "CLOUD_SLOT_GRPC_BASE_PORT": str(cloud_grpc),
        "CLOUD_SLOT_LOG_DIR": str(directory / "cloud-logs"), "CLOUD_SLOT_STARTUP_TIMEOUT_SECONDS": "120",
        "CLOUD_SLOT_PROCESS_IDLE_TIMEOUT_SECONDS": "60", "RUNTIME_RELEASE_GRACE_SECONDS": "0",
        "RUNTIME_SLOT_RECONCILE_INTERVAL_SECONDS": "2",
        "EDGE_RUNTIME_REAL_HOST": "127.0.0.1", "EDGE_RUNTIME_REAL_PORT": str(edge_port),
        "CLOUD_RUNTIME_REAL_HOST": "127.0.0.1", "RUNTIME_HOST": "127.0.0.1",
        "EDGE_RUNTIME_PORT": str(edge_port), "PREFILL_GRPC_BIND": f"127.0.0.1:{prefill_grpc}",
        "PREFILL_GRPC_TARGET": f"127.0.0.1:{prefill_grpc}",
        "COORDINATOR_PORT": str(coordinator_port), "OPENAI_API_PORT": str(openai_port),
        "COORDINATOR_BASE_URL": f"http://127.0.0.1:{coordinator_port}",
        "PREFILL_RUNTIME_BASE_URL": f"http://127.0.0.1:{edge_port}",
        "MODEL_REGISTRY_PATH": str(registry), "PREFILL_MODEL_REGISTRY_PATH": str(registry),
        "DECODE_MODEL_REGISTRY_PATH": str(registry), "COORDINATOR_MODEL_REGISTRY_PATH": str(registry),
        "OPENAI_MODEL_REGISTRY_PATH": str(registry),
        "ALGORITHM_USE_MOCK": "false", "ALGORITHM_REAL_API_URL": args.policy_url,
        "PROMETHEUS_URL": "http://127.0.0.1:9091", "ASCEND_IPS": "10.144.144.4",
        "NETWORK_PING_COUNT": "1", "MODELSPLIT_TRACE_LOGGING": "1",
    })

    def spawn(name, command, cwd, extra=None):
        handle = (directory / (name + ".log")).open("w")
        handles.append(handle)
        child_env = dict(env)
        child_env.update(extra or {})
        process = subprocess.Popen(command, cwd=cwd, env=child_env, stdout=handle,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(process)
        return process

    def allowed_test_pids():
        parents = {process.pid for process in processes}
        rows = subprocess.check_output(["ps", "-eo", "pid,ppid"], text=True).splitlines()[1:]
        pairs = [tuple(map(int, row.split())) for row in rows]
        while True:
            children = {pid for pid, ppid in pairs if ppid in parents}
            if children <= parents:
                return parents
            parents.update(children)

    async def exercise():
        backend_python = sys.executable
        seed = """from app.db.database import Base, engine, SessionLocal
from app.models.models import Device
Base.metadata.create_all(engine)
db=SessionLocal()
db.add_all([Device(id='cloud',name='validation-cloud',value='10.144.144.4:9100|10.144.144.4:9500',device_type='cloud'),Device(id='validation-edge',name='validation-edge',value='10.144.144.4:9100|10.144.144.4:9500',device_type='edge')])
db.commit();db.close()
"""
        subprocess.run([backend_python, "-c", seed], cwd=root / "backend", env=env, check=True)
        wrapper = directory / "backend_validation.py"
        wrapper.write_text("""import uvicorn
from app.main import app
from app.services import schedule_orchestrator as orch
from app.core.config import settings
original = orch.get_prometheus_metrics
async def pinned_metrics(ip):
    metrics = dict(await original(ip))
    chips = [chip for chip in metrics.get('chips', []) if str(chip['chip_id']) == '""" + str(args.physical_npu) + """']
    if not chips: raise RuntimeError('Selected NPU is missing from real Prometheus metrics')
    chip = chips[0]
    metrics.update(gpu_mem_total_mb=chip['total_mb'],gpu_mem_used_mb=chip['used_mb'],gpu_util_percent=chip.get('util',0),chips=chips)
    return metrics
orch.get_prometheus_metrics = pinned_metrics
uvicorn.run(app,host='127.0.0.1',port=settings.PORT)
""")
        spawn("backend", [backend_python, str(wrapper)], root / "backend", {"PYTHONPATH": str(root / "backend")})
        runtime_python = env["MODELSPLIT_PYTHON_BIN"]
        spawn("prefill", [runtime_python, "-m", "app.services.prefill_server.app"], runtime_root)
        spawn("coordinator", [runtime_python, "-m", "uvicorn", "app.services.coordinator.app:app", "--host", "127.0.0.1", "--port", str(coordinator_port)], runtime_root)
        spawn("openai", [runtime_python, "-m", "uvicorn", "app.services.openai_api.app:app", "--host", "127.0.0.1", "--port", str(openai_port)], runtime_root)

        async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
            async def ready(url, timeout=150):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError("A test service exited; inspect its private log")
                    try:
                        response = await client.get(url, timeout=3)
                        if response.status_code == 200:
                            return response
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(1)
                raise TimeoutError(url)

            await ready(base + "/")
            for port in (edge_port, coordinator_port, openai_port, cloud_port):
                await ready(f"http://127.0.0.1:{port}/health")
            assert_no_other_card_users(allowed_test_pids())
            token = jwt.encode({"id": "isolated-deepseek8b", "sub": "validation", "exp": int(time.time()) + 3600}, key, algorithm="HS256")
            headers = {"Authorization": "Bearer " + token}
            response = await client.get(base + "/api/v1/schedule/models", headers=headers)
            response.raise_for_status()
            assert any(m["model_type"] == args.model_name for m in response.json())
            response = await client.post(base + "/api/v1/session/init", headers=headers, json={"edge_device_ip": "10.144.144.4"})
            response.raise_for_status()
            session_id = response.json()["session_id"]
            headers["Session-Id"] = session_id
            report["session_id"] = session_id
            response = await client.post(base + "/api/v1/schedule/trigger", headers=headers, json={"model_type": args.model_name})
            response.raise_for_status()
            task_id = response.json()["task_id"]
            report["task_id"] = task_id
            print("Real schedule triggered: " + task_id, flush=True)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                assert_no_other_card_users(allowed_test_pids())
                response = await client.get(base + f"/api/v1/schedule/tasks/{task_id}", headers=headers)
                response.raise_for_status()
                task = response.json()
                print(json.dumps({k: task.get(k) for k in ("status", "phase", "message")}, ensure_ascii=False), flush=True)
                if task["status"] == "failed":
                    raise RuntimeError(json.dumps(task, ensure_ascii=False))
                if task["status"] == "completed":
                    report["task"] = task
                    break
                await asyncio.sleep(2)
            else:
                raise TimeoutError("real schedule did not complete")
            states = {}
            for name, port in (("edge", edge_port), ("cloud", cloud_port)):
                state = (await client.get(f"http://127.0.0.1:{port}/runtime_state")).json()
                assert state["ready"] and state["task_id"] == task_id
                states[name] = state
            report["runtime_states"] = states
            assert states["edge"]["runtime_route"]["cloud_decode_grpc_target"] == f"127.0.0.1:{cloud_grpc}"
            api = f"http://127.0.0.1:{openai_port}/v1/chat/completions"
            payload = {"model": args.model_name, "messages": [{"role": "user", "content": "Name the capital of France. Answer briefly."}], "temperature": 0, "max_tokens": 24}
            response = await client.post(api, json=dict(payload, stream=False))
            response.raise_for_status()
            report["completion"] = response.json()
            assert report["completion"]["choices"][0]["message"]["content"]
            async with client.stream("POST", api, json=dict(payload, stream=True, max_tokens=12)) as response:
                response.raise_for_status()
                events = [line async for line in response.aiter_lines() if line]
            assert "data: [DONE]" in events
            report["stream_event_count"] = len(events)
            response = await client.post(base + "/api/v1/session/close", headers=headers, json={"session_id": session_id})
            response.raise_for_status()
            report["session_closed"] = True
            import sqlite3
            with sqlite3.connect(env["SQLITE_DB_PATH"]) as db:
                binding_statuses = db.execute("select status from runtime_bindings where session_id=?", (session_id,)).fetchall()
                assert binding_statuses and all(row[0] == "released" for row in binding_statuses)
                report["binding_statuses_after_close"] = binding_statuses
            report["passed"] = True

    try:
        asyncio.run(exercise())
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
        for handle in handles:
            handle.close()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("Validation report: " + str(report_path), flush=True)


if __name__ == "__main__":
    main()
