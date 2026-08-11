import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin, get_db
from app.models.models import Device, EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask, User
from app.services.schedule_presenter import (
    serialize_runtime_binding,
    serialize_runtime_slot,
    serialize_task,
)
from app.services.schedule_queue import LOADING_RUNNING_STATUS, WAITING_CLOUD_SLOT_STATUS

router = APIRouter()

RUNTIME_STATE_TIMEOUT_SECONDS = 5.0
RECENT_TASK_LIMIT = 20


def _runtime_maintenance_payload(request: Request) -> dict[str, Any]:
    state = getattr(request.app.state, "runtime_maintenance_state", None)
    if not isinstance(state, dict):
        return {
            "status": "stopped",
            "task_running": False,
            "last_started_at": None,
            "last_completed_at": None,
            "last_success_at": None,
            "total_cycles": 0,
            "consecutive_failure_count": 0,
            "last_error": "runtime maintenance state is unavailable",
            "last_cycle": None,
        }
    return dict(state)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _extract_first_ip(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.replace("|", ",").split(","):
        candidate = part.strip().split(":", 1)[0].strip()
        if candidate:
            return candidate
    return None


def _serialize_device(device: Device | None) -> dict[str, Any] | None:
    if device is None:
        return None
    return {
        "id": device.id,
        "name": device.name,
        "value": device.value,
        "device_type": device.device_type,
        "primary_ip": _extract_first_ip(device.value),
    }


def _serialize_edge_session(session: EdgeSession | None, devices_by_id: dict[str, Device]) -> dict[str, Any] | None:
    if session is None:
        return None
    edge_device = devices_by_id.get(session.edge_device_id)
    cloud_device = devices_by_id.get(session.cloud_device_id)
    return {
        "session_id": session.session_id,
        "openwebui_user_id": session.openwebui_user_id,
        "edge_device_id": session.edge_device_id,
        "edge_device_name": edge_device.name if edge_device else session.edge_device_id,
        "edge_ip": session.edge_ip,
        "cloud_device_id": session.cloud_device_id,
        "cloud_device_name": cloud_device.name if cloud_device else session.cloud_device_id,
        "model_type": session.model_type,
        "status": session.status,
        "last_active_at": _iso(session.last_active_at),
        "lease_expires_at": _iso(session.lease_expires_at),
        "created_at": _iso(session.created_at),
        "updated_at": _iso(session.updated_at),
    }


async def _fetch_runtime_state(control_url: str | None) -> tuple[dict[str, Any] | None, str | None]:
    if not control_url:
        return None, "missing control_url"
    base_url = control_url.rsplit("/", 1)[0].rstrip("/")
    if not base_url:
        return None, "invalid control_url"
    try:
        async with httpx.AsyncClient(timeout=RUNTIME_STATE_TIMEOUT_SECONDS, trust_env=False) as client:
            response = await client.get(f"{base_url}/runtime_state")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return None, "runtime_state response is not an object"
            return payload, None
    except httpx.TimeoutException:
        return None, "runtime_state request timeout"
    except httpx.HTTPError as exc:
        return None, str(exc)
    except ValueError as exc:
        return None, f"invalid runtime_state json: {exc}"


def _slot_alerts(slot: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    slot_id = slot.get("slot_id")
    role = slot.get("role")
    process_state = slot.get("process_state")
    model_state = slot.get("model_state")
    slot_state = slot.get("slot_state")
    active_request_count = int(slot.get("active_request_count") or 0)
    integrity_status = slot.get("integrity_status")
    confirmation_status = slot.get("confirmation_status")

    def add(level: str, message: str) -> None:
        alerts.append({"level": level, "source": "slot", "slot_id": slot_id, "role": role, "message": message})

    if slot_state == "needs_reconcile":
        add("warning", "runtime slot 状态需要 reconcile")
    if slot_state == "bound" and process_state != "running":
        add("critical", "slot 已绑定但进程未运行")
    if integrity_status == "failed":
        add("critical", "模型完整性校验失败")
    if confirmation_status == "failed":
        add("critical", "cloud/edge 完整性确认失败")
    if slot.get("runtime_state_error") and process_state == "running" and model_state != "loading":
        add("warning", f"读取 runtime_state 失败: {slot['runtime_state_error']}")
    if active_request_count > 0:
        add("info", f"当前有 {active_request_count} 个活跃推理请求")
    if role == "cloud" and model_state == "loading" and slot.get("updated_at"):
        try:
            updated_at = datetime.fromisoformat(slot["updated_at"])
            if datetime.utcnow() - updated_at.replace(tzinfo=None) > timedelta(minutes=15):
                add("warning", "模型加载状态持续超过 15 分钟")
        except ValueError:
            pass
    return alerts


def _task_alerts(task: dict[str, Any]) -> list[dict[str, Any]]:
    if task.get("status") != "failed":
        return []
    return [
        {
            "level": "critical",
            "source": "task",
            "task_id": task.get("task_id"),
            "message": task.get("error_detail") or task.get("message") or "调度任务失败",
        }
    ]

def _build_summary(slots: list[dict[str, Any]], loading_queue: list[dict[str, Any]], recent_tasks: list[dict[str, Any]]) -> dict[str, int]:
    cloud_slots = [slot for slot in slots if slot.get("role") == "cloud"]
    return {
        "cloud_slot_total": len(cloud_slots),
        "cloud_slot_running": sum(1 for slot in cloud_slots if slot.get("process_state") == "running"),
        "cloud_slot_ready": sum(1 for slot in cloud_slots if slot.get("model_state") == "ready"),
        "cloud_slot_bound": sum(1 for slot in cloud_slots if slot.get("slot_state") == "bound"),
        "active_request_total": sum(int(slot.get("active_request_count") or 0) for slot in slots),
        "waiting_task_total": len(loading_queue),
        "failed_task_total": sum(1 for task in recent_tasks if task.get("status") == "failed"),
    }


@router.get("/overview", summary="【Admin】查询云端运行态总览")
async def get_runtime_overview(
    request: Request,
    admin_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    del admin_user

    devices = db.query(Device).all()
    devices_by_id = {device.id: device for device in devices}
    sessions = db.query(EdgeSession).all()
    sessions_by_id = {session.session_id: session for session in sessions}

    runtime_slots = (
        db.query(RuntimeSlot)
        .order_by(RuntimeSlot.role.asc(), RuntimeSlot.slot_index.asc(), RuntimeSlot.slot_id.asc())
        .all()
    )
    slot_payloads = [serialize_runtime_slot(slot) for slot in runtime_slots]

    runtime_state_results = await asyncio.gather(
        *[
            _fetch_runtime_state(slot.get("control_url")) if slot.get("process_state") == "running" else asyncio.sleep(0, result=(None, None))
            for slot in slot_payloads
        ]
    )
    for slot, (runtime_state, runtime_state_error) in zip(slot_payloads, runtime_state_results, strict=False):
        slot["runtime_state"] = runtime_state
        slot["runtime_state_error"] = runtime_state_error
        session = sessions_by_id.get(slot.get("owner_session_id") or "")
        slot["owner_session"] = _serialize_edge_session(session, devices_by_id)

    bindings = (
        db.query(RuntimeBinding)
        .order_by(RuntimeBinding.updated_at.desc(), RuntimeBinding.created_at.desc(), RuntimeBinding.binding_id.desc())
        .all()
    )
    binding_payloads = []
    for binding in bindings:
        payload = serialize_runtime_binding(binding)
        payload["session"] = _serialize_edge_session(sessions_by_id.get(binding.session_id), devices_by_id)
        binding_payloads.append(payload)

    loading_tasks = (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.status.in_(["accepted", "running"]),
            ScheduleTask.queue_status.in_([LOADING_RUNNING_STATUS, WAITING_CLOUD_SLOT_STATUS]),
        )
        .order_by(ScheduleTask.created_at.asc(), ScheduleTask.task_id.asc())
        .all()
    )
    loading_queue = [serialize_task(task) for task in loading_tasks]

    recent_tasks = (
        db.query(ScheduleTask)
        .order_by(ScheduleTask.updated_at.desc(), ScheduleTask.created_at.desc(), ScheduleTask.task_id.desc())
        .limit(RECENT_TASK_LIMIT)
        .all()
    )
    recent_task_payloads = []
    for task in recent_tasks:
        payload = serialize_task(task)
        payload["edge_device"] = _serialize_device(devices_by_id.get(task.edge_device_id or ""))
        payload["cloud_device"] = _serialize_device(devices_by_id.get(task.cloud_device_id or ""))
        payload["session"] = _serialize_edge_session(sessions_by_id.get(task.edge_session_id or ""), devices_by_id)
        recent_task_payloads.append(payload)

    edge_sessions = [
        _serialize_edge_session(session, devices_by_id)
        for session in sorted(sessions, key=lambda item: item.updated_at or item.created_at or datetime.min, reverse=True)
    ]

    alerts: list[dict[str, Any]] = []
    for slot in slot_payloads:
        alerts.extend(_slot_alerts(slot))
    for task in recent_task_payloads:
        alerts.extend(_task_alerts(task))

    maintenance = _runtime_maintenance_payload(request)
    if maintenance["status"] == "degraded":
        alerts.append(
            {
                "level": "warning",
                "source": "maintenance",
                "message": f"runtime reconcile 后台维护异常: {maintenance.get('last_error') or 'unknown error'}",
            }
        )
    elif maintenance["status"] == "stopped":
        alerts.append(
            {
                "level": "critical",
                "source": "maintenance",
                "message": f"runtime reconcile 后台任务已停止: {maintenance.get('last_error') or 'unknown error'}",
            }
        )

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "maintenance": maintenance,
        "summary": _build_summary(slot_payloads, loading_queue, recent_task_payloads),
        "devices": [_serialize_device(device) for device in devices],
        "edge_sessions": edge_sessions,
        "cloud_slots": [slot for slot in slot_payloads if slot.get("role") == "cloud"],
        "edge_slots": [slot for slot in slot_payloads if slot.get("role") == "edge"],
        "bindings": binding_payloads,
        "loading_queue": loading_queue,
        "recent_tasks": recent_task_payloads,
        "alerts": alerts,
    }
