from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot
from app.services.decode_server_process_manager import stop_slot_process
from app.services.runtime_control_service import fetch_runtime_state, unload_runtime_slot
from app.services.runtime_slot_service import update_runtime_slot_state


def mark_expired_sessions(db: Session) -> int:
    now = datetime.utcnow()
    sessions = (
        db.query(EdgeSession)
        .filter(
            EdgeSession.status == "active",
            EdgeSession.lease_expires_at <= now,
        )
        .all()
    )
    for session in sessions:
        session.status = "expired"
        session.updated_at = now
        session.last_active_at = now
        db.add(session)
    if sessions:
        db.commit()
    return len(sessions)


def release_bindings_for_session(db: Session, session_id: str) -> int:
    bindings = db.query(RuntimeBinding).filter(RuntimeBinding.session_id == session_id).all()
    for binding in bindings:
        binding.status = "released"
        binding.updated_at = datetime.utcnow()
        db.add(binding)
    if bindings:
        db.commit()
    return len(bindings)


async def cleanup_runtime_slots_for_session(db: Session, session_id: str) -> list[str]:
    binding_ids = [
        binding.binding_id
        for binding in db.query(RuntimeBinding).filter(RuntimeBinding.session_id == session_id).all()
    ]
    if not binding_ids:
        return []

    slots = (
        db.query(RuntimeSlot)
        .filter(RuntimeSlot.owner_binding_id.in_(binding_ids))
        .all()
    )
    released: list[str] = []
    for slot in slots:
        try:
            state = await fetch_runtime_state(slot)
        except Exception:
            update_runtime_slot_state(
                db,
                slot,
                slot_state="needs_reconcile",
                model_state="failed",
                last_used_at=datetime.utcnow(),
            )
            continue

        active_request_count = int(state.get("active_request_count") or 0)
        ready = bool(state.get("ready"))
        draining = bool(state.get("draining"))

        update_runtime_slot_state(
            db,
            slot,
            active_request_count=active_request_count,
            last_used_at=datetime.utcnow(),
        )

        if active_request_count != 0 or draining:
            continue

        if ready or state.get("task_id") or state.get("model_type"):
            try:
                await unload_runtime_slot(
                    db,
                    slot,
                    reason=f"session {session_id} released by slot_reaper",
                )
                released.append(slot.slot_id)
            except Exception:
                update_runtime_slot_state(
                    db,
                    slot,
                    slot_state="needs_reconcile",
                    model_state="failed",
                    last_used_at=datetime.utcnow(),
                )
                continue
        else:
            update_runtime_slot_state(
                db,
                slot,
                slot_state="free",
                model_state="empty",
                owner_session_id=None,
                owner_binding_id=None,
                model_type=None,
                task_id=None,
                active_request_count=0,
                last_used_at=datetime.utcnow(),
            )
            released.append(slot.slot_id)
    return released



def stop_idle_spawned_cloud_slots(db: Session) -> list[str]:
    now = datetime.utcnow()
    slots = (
        db.query(RuntimeSlot)
        .filter(
            RuntimeSlot.role == "cloud",
            RuntimeSlot.spawned_by_scheduler == 1,
            RuntimeSlot.slot_state == "free",
            RuntimeSlot.process_state == "running",
            RuntimeSlot.process_idle_deadline.isnot(None),
            RuntimeSlot.process_idle_deadline <= now,
        )
        .all()
    )
    stopped: list[str] = []
    for slot in slots:
        stopped_ok = stop_slot_process(slot.slot_id, process_pid=slot.process_pid)
        if not stopped_ok:
            update_runtime_slot_state(
                db,
                slot,
                process_state="failed",
                slot_state="needs_reconcile",
                last_used_at=datetime.utcnow(),
            )
            continue
        update_runtime_slot_state(
            db,
            slot,
            process_state="stopped",
            process_pid=None,
            process_idle_deadline=None,
            last_used_at=datetime.utcnow(),
        )
        stopped.append(slot.slot_id)
    return stopped
