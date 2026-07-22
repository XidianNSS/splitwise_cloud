import logging
from contextlib import suppress
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot
from app.services.managed_cloud_slot_cleanup_service import stop_and_clear_managed_cloud_slot
from app.services.runtime_control_service import fetch_runtime_state, unload_runtime_slot
from app.services.runtime_state_transition_service import (
    transition_runtime_binding,
    transition_runtime_slot,
)


logger = logging.getLogger("SlotReaper")


def release_grace_deadline() -> datetime | None:
    if settings.RUNTIME_RELEASE_GRACE_SECONDS <= 0:
        return None
    return datetime.utcnow() + timedelta(seconds=settings.RUNTIME_RELEASE_GRACE_SECONDS)


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
        transition_runtime_binding(
            db,
            binding,
            status="released",
            commit=False,
        )
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
            transition_runtime_slot(
                db,
                slot,
                process_state="failed",
                slot_state="needs_reconcile",
                model_state="failed",
                last_used_at=datetime.utcnow(),
            )
            continue

        active_request_count = int(state.get("active_request_count") or 0)
        ready = bool(state.get("ready"))
        draining = bool(state.get("draining"))

        transition_runtime_slot(
            db,
            slot,
            active_request_count=active_request_count,
            last_used_at=datetime.utcnow(),
        )

        if active_request_count != 0 or draining:
            continue

        deadline = release_grace_deadline()
        if ready and deadline is not None:
            transition_runtime_slot(
                db,
                slot,
                slot_state="retained",
                model_state="ready",
                active_request_count=0,
                owner_session_id=None,
                owner_binding_id=None,
                task_id=None,
                idle_deadline=slot.idle_deadline or deadline,
                process_idle_deadline=None,
                last_used_at=datetime.utcnow(),
            )
            released.append(slot.slot_id)
        elif ready or state.get("task_id") or state.get("model_type"):
            try:
                await unload_runtime_slot(
                    db,
                    slot,
                    reason=f"session {session_id} released by slot_reaper",
                )
                released.append(slot.slot_id)
            except Exception:
                transition_runtime_slot(
                    db,
                    slot,
                    slot_state="needs_reconcile",
                    model_state="failed",
                    last_used_at=datetime.utcnow(),
                )
                continue
        else:
            transition_runtime_slot(
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


async def cleanup_runtime_slots_for_task(
    db: Session,
    task_id: str,
    binding_id: str | None,
) -> list[str]:
    """Release only slots that are still owned by one superseded task.

    The ownership is re-read after the runtime-state network call.  This prevents
    an old coroutine from cleaning a slot that was reassigned while it awaited
    the runtime response.
    """
    query = db.query(RuntimeSlot).filter(RuntimeSlot.task_id == task_id)
    if binding_id:
        query = query.filter(RuntimeSlot.owner_binding_id == binding_id)
    slot_ids = [slot.slot_id for slot in query.all()]
    released: list[str] = []

    for slot_id in slot_ids:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
        if slot is None:
            continue
        try:
            state = await fetch_runtime_state(slot)
        except Exception:
            db.expire_all()
            current = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
            if (
                current is not None
                and current.task_id == task_id
                and (not binding_id or current.owner_binding_id == binding_id)
            ):
                transition_runtime_slot(
                    db,
                    current,
                    process_state="failed",
                    slot_state="needs_reconcile",
                    model_state="failed",
                    last_used_at=datetime.utcnow(),
                )
            continue

        db.expire_all()
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
        if (
            slot is None
            or slot.task_id != task_id
            or (binding_id and slot.owner_binding_id != binding_id)
        ):
            logger.info(
                "跳过已重新分配的 task slot 清理: slot_id=%s old_task_id=%s",
                slot_id,
                task_id,
            )
            continue

        active_request_count = int(state.get("active_request_count") or 0)
        ready = bool(state.get("ready"))
        draining = bool(state.get("draining"))
        transition_runtime_slot(
            db,
            slot,
            active_request_count=active_request_count,
            last_used_at=datetime.utcnow(),
        )
        if active_request_count != 0 or draining:
            continue

        deadline = release_grace_deadline()
        if ready and deadline is not None:
            transition_runtime_slot(
                db,
                slot,
                slot_state="retained",
                model_state="ready",
                active_request_count=0,
                owner_session_id=None,
                owner_binding_id=None,
                task_id=None,
                idle_deadline=slot.idle_deadline or deadline,
                process_idle_deadline=None,
                last_used_at=datetime.utcnow(),
            )
            released.append(slot.slot_id)
        elif ready or state.get("task_id") or state.get("model_type"):
            try:
                await unload_runtime_slot(
                    db,
                    slot,
                    reason=f"task {task_id} lost schedule authority",
                )
                released.append(slot.slot_id)
            except Exception:
                transition_runtime_slot(
                    db,
                    slot,
                    slot_state="needs_reconcile",
                    model_state="failed",
                    last_used_at=datetime.utcnow(),
                )
        else:
            transition_runtime_slot(
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



def stop_idle_spawned_cloud_slots(
    db: Session,
    *,
    failed_slots: list[dict[str, str]] | None = None,
) -> list[str]:
    """停止超过进程空闲期限的托管 cloud slot。

    每个 slot 独立处理；进程停止或数据库更新失败时回滚当前事务并继续
    后续 slot，避免单个残留进程阻塞整个后台维护循环。
    """
    now = datetime.utcnow()
    slot_ids = [
        slot.slot_id
        for slot in (
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
    ]
    stopped: list[str] = []
    for slot_id in slot_ids:
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
            if slot is None:
                continue
            _, stopped_ok = stop_and_clear_managed_cloud_slot(db, slot)
            if not stopped_ok:
                error = "managed cloud slot process stop failed"
                if failed_slots is not None:
                    failed_slots.append({"slot_id": slot_id, "error": error})
                logger.warning("空闲托管 cloud slot 停止失败: slot_id=%s", slot_id)
                continue
            stopped.append(slot_id)
        except Exception as exc:
            with suppress(Exception):
                db.rollback()
            error = f"{type(exc).__name__}: {exc}"[:512]
            if failed_slots is not None:
                failed_slots.append({"slot_id": slot_id, "error": error})
            logger.exception("空闲托管 cloud slot 清理异常，已跳过: slot_id=%s", slot_id)
    return stopped
