from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services.runtime_binding_service import update_runtime_binding
from app.services.runtime_slot_service import update_runtime_slot_state

_ACTIVE_TASK_STATUSES = {"accepted", "running"}
_FINISHED_SESSION_STATUSES = {"closed", "expired"}


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
    fields = {
        'slot_state': 'free',
        'model_state': 'empty',
        'owner_session_id': None,
        'owner_binding_id': None,
        'model_type': None,
        'task_id': None,
        'active_request_count': 0,
        'integrity_status': 'unknown',
        'confirmation_status': 'none',
        'last_used_at': datetime.utcnow(),
    }
    if process_state is not None:
        fields['process_state'] = process_state
    return update_runtime_slot_state(db, slot, **fields)


def _release_binding(binding: RuntimeBinding | None) -> None:
    if binding is None or binding.status == 'released':
        return
    binding.status = 'released'
    binding.updated_at = datetime.utcnow()


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
            _release_binding(binding)

    db.flush()

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
            if binding is not None and binding.status != 'released':
                _release_binding(binding)
            if task is not None and (task.status in _ACTIVE_TASK_STATUSES or task.phase == 'loading'):
                _fail_task(task, '服务重启后检测到运行时状态不一致，请重新发起')
            _clear_slot_owner(db, slot, process_state='stopped' if slot.role == 'cloud' and bool(getattr(slot, 'spawned_by_scheduler', 0)) else 'failed')

    db.commit()


def reconcile_runtime_ownership(db: Session) -> None:
    slots = db.query(RuntimeSlot).all()
    for slot in slots:
        binding = _get_binding(db, slot.owner_binding_id)
        session = _get_session(db, slot.owner_session_id)
        task = _get_task(db, slot.task_id)

        if binding is not None and binding.status == 'released':
            _clear_slot_owner(db, slot)
            continue
        if session is not None and session.status in _FINISHED_SESSION_STATUSES:
            if binding is not None:
                _release_binding(binding)
            _clear_slot_owner(db, slot)
            continue
        if task is not None and task.status in {'failed'}:
            if binding is not None:
                _release_binding(binding)
            _clear_slot_owner(db, slot)
            continue
    db.commit()
