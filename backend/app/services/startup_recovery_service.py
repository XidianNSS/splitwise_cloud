from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services.managed_cloud_slot_cleanup_service import clear_slot_ownership, stop_and_clear_managed_cloud_slot
from app.services.runtime_state_transition_service import (
    transition_runtime_binding,
    transition_runtime_slot,
)

_ACTIVE_TASK_STATUSES = {"accepted", "running"}
_FINISHED_SESSION_STATUSES = {"closed", "expired"}


def _release_grace_deadline() -> datetime | None:
    if settings.RUNTIME_RELEASE_GRACE_SECONDS <= 0:
        return None
    return datetime.utcnow() + timedelta(seconds=settings.RUNTIME_RELEASE_GRACE_SECONDS)


def _ready_slot_can_enter_release_grace(slot: RuntimeSlot) -> bool:
    if settings.RUNTIME_RELEASE_GRACE_SECONDS <= 0:
        return False
    if slot.model_state != 'ready':
        return False
    if int(slot.active_request_count or 0) != 0:
        return False
    if slot.idle_deadline is not None and slot.idle_deadline <= datetime.utcnow():
        return False
    return True


def _protect_ready_slot_from_immediate_release(db: Session, slot: RuntimeSlot) -> RuntimeSlot:
    deadline = slot.idle_deadline or _release_grace_deadline()
    return transition_runtime_slot(
        db,
        slot,
        slot_state='retained',
        model_state='ready',
        owner_session_id=None,
        owner_binding_id=None,
        task_id=None,
        idle_deadline=deadline,
        process_idle_deadline=None,
        last_used_at=datetime.utcnow(),
    )


def _get_task(db: Session, task_id: str | None) -> ScheduleTask | None:
    if not task_id:
        return None
    return db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).first()


def _get_session(db: Session, session_id: str | None) -> EdgeSession | None:
    if not session_id:
        return None
    return db.query(EdgeSession).filter(EdgeSession.session_id == session_id).first()


def _get_binding(db: Session, binding_id: str | None) -> RuntimeBinding | None:
    if not binding_id:
        return None
    return db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == binding_id).first()


def _is_trustworthy_completed_binding(
    *,
    task: ScheduleTask | None,
    session: EdgeSession | None,
    edge_slot: RuntimeSlot | None,
    cloud_slot: RuntimeSlot | None,
    binding: RuntimeBinding,
) -> bool:
    if task is None or session is None or edge_slot is None or cloud_slot is None:
        return False
    if task.status != 'completed':
        return False
    if session.status != 'active':
        return False
    if binding.status != 'binding':
        return False
    if edge_slot.slot_state != 'bound' or edge_slot.model_state != 'ready':
        return False
    if cloud_slot.slot_state != 'bound' or cloud_slot.model_state != 'ready':
        return False
    if cloud_slot.confirmation_status != 'passed':
        return False
    return True


def _fail_task(task: ScheduleTask | None, message: str) -> None:
    if task is None:
        return
    if task.status in {'failed'}:
        return
    task.status = 'failed'
    task.error_detail = message
    task.message = message
    task.queue_status = 'done'
    task.queue_position = 0
    task.updated_at = datetime.utcnow()


def _clear_slot_owner(db: Session, slot: RuntimeSlot, *, process_state: str | None = None) -> RuntimeSlot:
    if slot.role == 'cloud' and bool(getattr(slot, 'spawned_by_scheduler', 0)) and process_state == 'stopped':
        cleared_slot, _ = stop_and_clear_managed_cloud_slot(db, slot)
        return cleared_slot
    return clear_slot_ownership(db, slot, process_state=process_state)


def _release_binding(db: Session, binding: RuntimeBinding | None) -> None:
    if binding is None or binding.status == 'released':
        return
    transition_runtime_binding(db, binding, status="released", commit=False)


def _authoritative_binding_id_by_session(db: Session) -> dict[str, str]:
    session_owner_binding: dict[str, str] = {}
    session_owner_score: dict[str, tuple[int, datetime, str]] = {}
    slots = db.query(RuntimeSlot).filter(RuntimeSlot.owner_session_id.isnot(None), RuntimeSlot.owner_binding_id.isnot(None)).all()
    for slot in slots:
        session_id = slot.owner_session_id
        binding_id = slot.owner_binding_id
        if not session_id or not binding_id:
            continue
        score = 2 if slot.slot_state == 'bound' and slot.model_state == 'ready' else 1 if slot.slot_state == 'bound' else 0
        updated_at = slot.updated_at or datetime.min
        candidate = (score, updated_at, binding_id)
        current = session_owner_score.get(session_id)
        if current is None or candidate > current:
            session_owner_score[session_id] = candidate
            session_owner_binding[session_id] = binding_id
    return session_owner_binding


def _task_is_active_for_binding(task: ScheduleTask | None) -> bool:
    return task is not None and task.status in _ACTIVE_TASK_STATUSES and task.queue_status != 'done'


def _task_is_superseded(task: ScheduleTask | None) -> bool:
    return bool(task is not None and task.status == 'failed' and str(task.error_detail or '').startswith('superseded_by_model='))


def _session_has_active_replacement_task(db: Session, session_id: str | None) -> bool:
    if not session_id:
        return False
    return (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.edge_session_id == session_id,
            ScheduleTask.status.in_(list(_ACTIVE_TASK_STATUSES)),
            ScheduleTask.queue_status != 'done',
        )
        .first()
        is not None
    )


def _release_duplicate_session_bindings(db: Session) -> None:
    authoritative = _authoritative_binding_id_by_session(db)
    bindings = db.query(RuntimeBinding).filter(RuntimeBinding.status == 'binding').all()
    updated = False
    for binding in bindings:
        keep_binding_id = authoritative.get(binding.session_id)
        if keep_binding_id is None or binding.binding_id == keep_binding_id:
            continue
        if _task_is_active_for_binding(_get_task(db, binding.task_id)):
            continue
        _release_binding(db, binding)
        updated = True
    if updated:
        db.flush()


def _binding_should_release(
    *,
    task: ScheduleTask | None,
    session: EdgeSession | None,
    edge_slot: RuntimeSlot | None,
    cloud_slot: RuntimeSlot | None,
    binding: RuntimeBinding,
) -> bool:
    if session is None or session.status in _FINISHED_SESSION_STATUSES:
        return True
    if task is None:
        return True
    if task.status in _ACTIVE_TASK_STATUSES:
        return True
    if task.status == 'completed':
        return not _is_trustworthy_completed_binding(
            task=task, session=session, edge_slot=edge_slot, cloud_slot=cloud_slot, binding=binding
        )
    return True


def recover_runtime_ownership_on_startup(db: Session) -> None:
    bindings = db.query(RuntimeBinding).all()
    slots_by_id = {slot.slot_id: slot for slot in db.query(RuntimeSlot).all()}

    for binding in bindings:
        task = _get_task(db, binding.task_id)
        session = _get_session(db, binding.session_id)
        edge_slot = slots_by_id.get(binding.edge_slot_id) if binding.edge_slot_id else None
        cloud_slot = slots_by_id.get(binding.cloud_slot_id) if binding.cloud_slot_id else None

        if _binding_should_release(task=task, session=session, edge_slot=edge_slot, cloud_slot=cloud_slot, binding=binding):
            if task is not None and task.status in _ACTIVE_TASK_STATUSES:
                _fail_task(task, '服务重启后检测到运行时状态不一致，请重新发起')
            elif task is not None and task.status == 'completed' and not _is_trustworthy_completed_binding(
                task=task, session=session, edge_slot=edge_slot, cloud_slot=cloud_slot, binding=binding
            ):
                _fail_task(task, '服务重启后检测到运行时状态不一致，请重新发起')
            _release_binding(db, binding)

    db.flush()
    _release_duplicate_session_bindings(db)

    slots = db.query(RuntimeSlot).all()
    for slot in slots:
        binding = _get_binding(db, slot.owner_binding_id)
        session = _get_session(db, slot.owner_session_id)
        task = _get_task(db, slot.task_id)

        binding_released = binding is None or binding.status == 'released'
        session_finished = session is None or session.status in _FINISHED_SESSION_STATUSES
        task_missing = slot.task_id is not None and task is None
        task_untrusted_completed = task is not None and task.status == 'completed' and not (
            binding is not None and
            session is not None and session.status == 'active' and
            slot.slot_state == 'bound'
        )

        if binding_released or session_finished or task_missing or task_untrusted_completed:
            if _ready_slot_can_enter_release_grace(slot):
                if binding is not None and binding.status != 'released':
                    _release_binding(db, binding)
                _protect_ready_slot_from_immediate_release(db, slot)
                continue
            if binding is not None and binding.status != 'released':
                _release_binding(db, binding)
            if task is not None and (task.status in _ACTIVE_TASK_STATUSES or task.phase == 'loading'):
                _fail_task(task, '服务重启后检测到运行时状态不一致，请重新发起')
            _clear_slot_owner(db, slot, process_state='stopped' if slot.role == 'cloud' and bool(getattr(slot, 'spawned_by_scheduler', 0)) else 'failed')

    db.commit()


def reconcile_runtime_ownership(db: Session) -> None:
    _release_duplicate_session_bindings(db)
    slots = db.query(RuntimeSlot).all()
    for slot in slots:
        binding = _get_binding(db, slot.owner_binding_id)
        session = _get_session(db, slot.owner_session_id)
        task = _get_task(db, slot.task_id)

        binding_missing = slot.owner_binding_id is not None and binding is None
        session_missing = slot.owner_session_id is not None and session is None
        task_missing = slot.task_id is not None and task is None
        binding_released = binding is not None and binding.status == 'released'
        session_finished = session is not None and session.status in _FINISHED_SESSION_STATUSES
        task_failed = task is not None and task.status in {'failed'}

        if task_failed and _task_is_superseded(task) and _session_has_active_replacement_task(db, slot.owner_session_id):
            continue

        if binding_missing or session_missing or task_missing or binding_released or session_finished or task_failed:
            if _ready_slot_can_enter_release_grace(slot):
                if binding is not None and binding.status != 'released':
                    _release_binding(db, binding)
                _protect_ready_slot_from_immediate_release(db, slot)
                continue
            if binding is not None:
                _release_binding(db, binding)
            _clear_slot_owner(db, slot)
            continue
    db.commit()
