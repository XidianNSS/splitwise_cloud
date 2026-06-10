from __future__ import annotations

from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services.decode_server_process_manager import inspect_slot_process
from app.services.managed_cloud_slot_cleanup_service import clear_slot_ownership, stop_and_clear_managed_cloud_slot
from app.services.runtime_control_service import fetch_runtime_state, unload_runtime_slot
from app.services.runtime_slot_service import update_runtime_slot_state


_ACTIVE_TASK_STATUSES = {"accepted", "running"}
_ACTIVE_TASK_PHASES = {"strategy", "loading", "completed"}
_FINISHED_SESSION_STATUSES = {"closed", "expired"}
_FINISHED_BINDING_STATUSES = {"released"}
_FINISHED_TASK_STATUSES = {"failed", "completed"}


def _get_session(db: Session, session_id: str | None) -> EdgeSession | None:
    if not session_id:
        return None
    return db.query(EdgeSession).filter(EdgeSession.session_id == session_id).first()


def _get_binding(db: Session, binding_id: str | None) -> RuntimeBinding | None:
    if not binding_id:
        return None
    return db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == binding_id).first()


def _get_task(db: Session, task_id: str | None) -> ScheduleTask | None:
    if not task_id:
        return None
    return db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).first()


def _task_is_active(task: ScheduleTask | None) -> bool:
    if task is None:
        return False
    if task.status not in _ACTIVE_TASK_STATUSES:
        return False
    return task.phase in _ACTIVE_TASK_PHASES


def _slot_is_active_loading(slot: RuntimeSlot, task: ScheduleTask | None) -> bool:
    if slot.slot_state != 'bound':
        return False
    if slot.model_state != 'loading':
        return False
    return _task_is_active(task)


def _clear_slot_ownership(db: Session, slot: RuntimeSlot, *, process_state: str | None = None) -> RuntimeSlot:
    if slot.role == 'cloud' and bool(getattr(slot, 'spawned_by_scheduler', 0)) and process_state == 'stopped':
        cleared_slot, _ = stop_and_clear_managed_cloud_slot(db, slot)
        return cleared_slot
    return clear_slot_ownership(db, slot, process_state=process_state)


def _mark_task_failed_if_active(db: Session, task: ScheduleTask | None, message: str) -> None:
    if task is None or not _task_is_active(task):
        return
    task.status = 'failed'
    task.error_detail = message
    task.message = message
    task.queue_status = 'done'
    task.queue_position = 0
    task.updated_at = datetime.utcnow()
    db.add(task)


def _release_binding(db: Session, binding: RuntimeBinding | None) -> None:
    if binding is None:
        return
    if binding.status not in _FINISHED_BINDING_STATUSES:
        binding.status = 'released'
        binding.updated_at = datetime.utcnow()
        db.add(binding)


async def _reconcile_spawned_cloud_slot(db: Session, slot: RuntimeSlot) -> RuntimeSlot:
    session = _get_session(db, slot.owner_session_id)
    binding = _get_binding(db, slot.owner_binding_id)
    task = _get_task(db, slot.task_id)
    has_owner_binding = bool(slot.owner_binding_id)
    has_owner_session = bool(slot.owner_session_id)

    binding_released = has_owner_binding and (
        binding is None or binding.status in _FINISHED_BINDING_STATUSES
    )
    session_finished = has_owner_session and (
        session is None or session.status in _FINISHED_SESSION_STATUSES
    )
    task_active = _task_is_active(task)
    slot_active_loading = _slot_is_active_loading(slot, task)
    task_finished = task is not None and task.status in _FINISHED_TASK_STATUSES
    process_alive = inspect_slot_process(slot.slot_id) is not None
    if not process_alive and slot.process_pid:
        try:
            import os
            os.kill(slot.process_pid, 0)
        except OSError:
            process_alive = False
        else:
            process_alive = True

    if not process_alive:
        if slot_active_loading:
            return update_runtime_slot_state(
                db,
                slot,
                process_state='starting',
                slot_state='bound',
                model_state='loading',
                last_used_at=datetime.utcnow(),
            )
        if task_active:
            _mark_task_failed_if_active(db, task, f'cloud slot {slot.slot_id} 进程已丢失，任务已失败')
        if binding is not None:
            _release_binding(db, binding)
        return _clear_slot_ownership(db, slot, process_state='stopped')

    base_url = slot.control_url.removesuffix('/load_strategy') if slot.control_url else ''
    health_ok = False
    if base_url:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f'{base_url}/health')
                response.raise_for_status()
            health_ok = True
        except httpx.HTTPError:
            health_ok = False

    if not health_ok:
        if slot_active_loading:
            return update_runtime_slot_state(
                db,
                slot,
                process_state='starting',
                slot_state='bound',
                model_state='loading',
                last_used_at=datetime.utcnow(),
            )
        if task_active:
            _mark_task_failed_if_active(db, task, f'cloud slot {slot.slot_id} 健康检查失败，任务已失败')
        if binding is not None:
            _release_binding(db, binding)
        cleared_slot, stopped_ok = stop_and_clear_managed_cloud_slot(db, slot)
        if stopped_ok:
            return cleared_slot
        return cleared_slot

    try:
        state = await fetch_runtime_state(slot)
    except Exception:
        if slot_active_loading:
            return update_runtime_slot_state(
                db,
                slot,
                process_state='starting',
                slot_state='bound',
                model_state='loading',
                last_used_at=datetime.utcnow(),
            )
        return update_runtime_slot_state(
            db,
            slot,
            process_state='failed',
            slot_state='needs_reconcile',
            model_state='failed',
            last_used_at=datetime.utcnow(),
        )

    active_request_count = int(state.get('active_request_count') or 0)
    ready = bool(state.get('ready'))
    draining = bool(state.get('draining'))
    runtime_model_type = state.get('model_type')
    runtime_task_id = state.get('task_id')

    # backend 托管的 warm cloud slot：
    # 进程已启动，但还没有绑定 session / binding / task，也还没加载模型。
    # 这种 slot 应该保持 running/free/empty，等待后续 /load_strategy，
    # 不能因为 owner 为空就被 reconcile 清理掉。
    if (
        bool(getattr(slot, "spawned_by_scheduler", 0))
        and slot.slot_state == "free"
        and not slot.owner_binding_id
        and not slot.owner_session_id
        and not slot.task_id
        and active_request_count == 0
        and not ready
        and not draining
        and not runtime_model_type
        and not runtime_task_id
    ):
        return update_runtime_slot_state(
            db,
            slot,
            process_state="running",
            slot_state="free",
            model_state="empty",
            active_request_count=0,
            model_type=None,
            task_id=None,
            last_used_at=datetime.utcnow(),
        )

    if binding_released or session_finished:
        if ready and active_request_count == 0 and (runtime_model_type or runtime_task_id):
            try:
                await unload_runtime_slot(db, slot, reason=f'reconcile release for slot {slot.slot_id}')
                return db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot.slot_id).first()
            except Exception:
                return update_runtime_slot_state(
                    db,
                    slot,
                    process_state='failed',
                    slot_state='needs_reconcile',
                    model_state='failed',
                    last_used_at=datetime.utcnow(),
                )
        return _clear_slot_ownership(db, slot, process_state='stopped')
    if task_finished and active_request_count == 0 and not ready and not runtime_model_type and not runtime_task_id:
        return update_runtime_slot_state(
            db,
            slot,
            process_state='running',
            slot_state='bound' if slot.owner_binding_id else 'free',
            model_state='empty',
            active_request_count=0,
            last_used_at=datetime.utcnow(),
        )
    if not ready and not runtime_model_type and active_request_count == 0 and not slot.owner_binding_id:
        return _clear_slot_ownership(db, slot, process_state='running')
    return update_runtime_slot_state(
        db,
        slot,
        process_state='running',
        slot_state='bound' if slot.owner_binding_id else 'free',
        model_state='draining' if draining else ('ready' if ready else 'empty'),
        active_request_count=active_request_count,
        model_type=runtime_model_type or slot.model_type,
        task_id=runtime_task_id or slot.task_id,
        integrity_status='healthy' if ready else slot.integrity_status,
        confirmation_status='passed' if ready else slot.confirmation_status,
        last_used_at=datetime.utcnow(),
    )


async def _reconcile_base_or_edge_slot(db: Session, slot: RuntimeSlot) -> RuntimeSlot:
    session = _get_session(db, slot.owner_session_id)
    binding = _get_binding(db, slot.owner_binding_id)
    task = _get_task(db, slot.task_id)
    binding_released = binding is None or binding.status in _FINISHED_BINDING_STATUSES
    session_finished = session is None or session.status in _FINISHED_SESSION_STATUSES
    task_finished = task is not None and task.status in _FINISHED_TASK_STATUSES
    try:
        state = await fetch_runtime_state(slot)
    except Exception:
        if binding_released or session_finished or task_finished:
            if binding is not None:
                _release_binding(db, binding)
            return _clear_slot_ownership(db, slot, process_state='failed')
        return update_runtime_slot_state(
            db,
            slot,
            process_state='failed',
            slot_state='needs_reconcile',
            model_state='failed',
            last_used_at=datetime.utcnow(),
        )

    active_request_count = int(state.get('active_request_count') or 0)
    ready = bool(state.get('ready'))
    draining = bool(state.get('draining'))
    runtime_model_type = state.get('model_type')
    runtime_task_id = state.get('task_id')

    if binding_released or session_finished:
        if ready and active_request_count == 0 and (runtime_model_type or runtime_task_id):
            try:
                await unload_runtime_slot(db, slot, reason=f'reconcile release for slot {slot.slot_id}')
                refreshed = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot.slot_id).first()
                return update_runtime_slot_state(
                    db,
                    refreshed,
                    process_state='running',
                    last_used_at=datetime.utcnow(),
                )
            except Exception:
                return update_runtime_slot_state(
                    db,
                    slot,
                    process_state='failed',
                    slot_state='needs_reconcile',
                    model_state='failed',
                    last_used_at=datetime.utcnow(),
                )
        return update_runtime_slot_state(
            db,
            slot,
            process_state='running',
            slot_state='free',
            model_state='empty',
            owner_session_id=None,
            owner_binding_id=None,
            model_type=None,
            task_id=None,
            active_request_count=0,
            integrity_status='unknown',
            confirmation_status='none',
            last_used_at=datetime.utcnow(),
        )

    if not ready and not runtime_model_type and active_request_count == 0 and not slot.owner_binding_id:
        return update_runtime_slot_state(
            db,
            slot,
            process_state='running',
            slot_state='free',
            model_state='empty',
            active_request_count=0,
            integrity_status='unknown',
            confirmation_status='none',
            last_used_at=datetime.utcnow(),
        )

    return update_runtime_slot_state(
        db,
        slot,
        process_state='running',
        slot_state='bound' if slot.owner_binding_id else 'free',
        model_state='draining' if draining else ('ready' if ready else 'empty'),
        active_request_count=active_request_count,
        model_type=runtime_model_type or slot.model_type,
        task_id=runtime_task_id or slot.task_id,
        integrity_status='healthy' if ready else slot.integrity_status,
        confirmation_status='passed' if ready else slot.confirmation_status,
        last_used_at=datetime.utcnow(),
    )


async def reconcile_runtime_slot(db: Session, slot: RuntimeSlot) -> RuntimeSlot:
    if slot.role == 'cloud' and bool(getattr(slot, 'spawned_by_scheduler', 0)):
        return await _reconcile_spawned_cloud_slot(db, slot)
    return await _reconcile_base_or_edge_slot(db, slot)


async def reconcile_all_runtime_slots(db: Session) -> list[str]:
    slot_ids = [slot.slot_id for slot in db.query(RuntimeSlot).order_by(RuntimeSlot.slot_id.asc()).all()]
    reconciled: list[str] = []
    for slot_id in slot_ids:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
        if slot is None:
            continue
        await reconcile_runtime_slot(db, slot)
        reconciled.append(slot_id)
    return reconciled
