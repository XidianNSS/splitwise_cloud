from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import RuntimeSlot
from app.services.decode_server_process_manager import stop_slot_process
from app.services.runtime_slot_service import update_runtime_slot_state


def is_backend_managed_cloud_slot(slot: RuntimeSlot) -> bool:
    return slot.role == "cloud" and bool(getattr(slot, "spawned_by_scheduler", 0))


def clear_slot_owner_fields() -> dict:
    return {
        "slot_state": "free",
        "model_state": "empty",
        "owner_session_id": None,
        "owner_binding_id": None,
        "model_type": None,
        "task_id": None,
        "active_request_count": 0,
        "integrity_status": "unknown",
        "confirmation_status": "none",
        "idle_deadline": None,
        "process_idle_deadline": None,
        "startup_deadline": None,
        "last_used_at": datetime.utcnow(),
    }


def clear_slot_ownership(db: Session, slot: RuntimeSlot, *, process_state: str | None = None) -> RuntimeSlot:
    fields = clear_slot_owner_fields()
    if process_state is not None:
        fields["process_state"] = process_state
    return update_runtime_slot_state(db, slot, **fields)


def _mark_managed_cloud_slot_stop_failed(db: Session, slot: RuntimeSlot) -> RuntimeSlot:
    fields = clear_slot_owner_fields()
    fields.update({
        "process_state": "failed",
        "slot_state": "needs_reconcile",
        "model_state": "failed",
        "process_pid": slot.process_pid,
    })
    return update_runtime_slot_state(db, slot, **fields)


def _mark_managed_cloud_slot_stopped(db: Session, slot: RuntimeSlot) -> RuntimeSlot:
    fields = clear_slot_owner_fields()
    fields.update({
        "process_state": "stopped",
        "process_pid": None,
        "control_url": None,
        "grpc_target": None,
    })
    return update_runtime_slot_state(db, slot, **fields)


def stop_and_clear_managed_cloud_slot(db: Session, slot: RuntimeSlot) -> tuple[RuntimeSlot, bool]:
    if not is_backend_managed_cloud_slot(slot):
        return clear_slot_ownership(db, slot), True

    stopped_ok = stop_slot_process(slot.slot_id, process_pid=slot.process_pid)
    if not stopped_ok:
        if slot.process_pid is None:
            return _mark_managed_cloud_slot_stopped(db, slot), True
        return _mark_managed_cloud_slot_stop_failed(db, slot), False

    return _mark_managed_cloud_slot_stopped(db, slot), True


def prepare_managed_cloud_slot_for_start(db: Session, slot: RuntimeSlot) -> tuple[RuntimeSlot, bool]:
    if not is_backend_managed_cloud_slot(slot):
        return slot, True

    if slot.process_pid is None:
        return slot, True

    stopped_ok = stop_slot_process(slot.slot_id, process_pid=slot.process_pid)
    if not stopped_ok:
        return _mark_managed_cloud_slot_stop_failed(db, slot), False

    return _mark_managed_cloud_slot_stopped(db, slot), True
