from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services.decode_server_process_manager import inspect_slot_process, stop_slot_process
from app.services.managed_cloud_slot_cleanup_service import clear_slot_ownership, stop_and_clear_managed_cloud_slot
from app.services.runtime_control_service import fetch_runtime_state, unload_runtime_slot
from app.services.runtime_slot_service import update_runtime_slot_state


logger = logging.getLogger("RuntimeSlotReconcileService")

_ACTIVE_TASK_STATUSES = {"accepted", "running"}
_ACTIVE_TASK_PHASES = {"strategy", "loading", "completed"}
_FINISHED_SESSION_STATUSES = {"closed", "expired"}
_FINISHED_BINDING_STATUSES = {"released"}
_FINISHED_TASK_STATUSES = {"failed", "completed"}


def _release_grace_deadline() -> datetime | None:
    if settings.RUNTIME_RELEASE_GRACE_SECONDS <= 0:
        return None
    return datetime.utcnow() + timedelta(seconds=settings.RUNTIME_RELEASE_GRACE_SECONDS)


def _release_grace_active(slot: RuntimeSlot) -> bool:
    return slot.idle_deadline is not None and slot.idle_deadline > datetime.utcnow()


def _protect_ready_released_slot(
    db: Session,
    slot: RuntimeSlot,
    *,
    active_request_count: int,
    runtime_model_type: str | None,
    runtime_task_id: str | None,
) -> RuntimeSlot | None:
    if settings.RUNTIME_RELEASE_GRACE_SECONDS <= 0:
        return None
    if slot.idle_deadline is not None and not _release_grace_active(slot):
        return None
    deadline = slot.idle_deadline or _release_grace_deadline()
    if deadline is None:
        return None
    return update_runtime_slot_state(
        db,
        slot,
        process_state="running",
        slot_state="retained",
        model_state="ready",
        active_request_count=active_request_count,
        model_type=runtime_model_type or slot.model_type,
        task_id=runtime_task_id or slot.task_id,
        integrity_status="healthy",
        confirmation_status="passed",
        idle_deadline=deadline,
        process_idle_deadline=None,
        last_used_at=datetime.utcnow(),
    )


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


def _startup_deadline_active(slot: RuntimeSlot) -> bool:
    return slot.startup_deadline is not None and slot.startup_deadline > datetime.utcnow()


def _recover_expired_cloud_startup(
    db: Session,
    slot: RuntimeSlot,
    binding: RuntimeBinding | None,
    task: ScheduleTask | None,
    message: str,
) -> RuntimeSlot:
    expected_pid = slot.process_pid
    if expected_pid is not None:
        stop_slot_process(slot.slot_id, process_pid=expected_pid)
    now = datetime.utcnow()
    failure_count = int(slot.startup_failure_count or 0) + 1
    delay = min(
        settings.CLOUD_SLOT_STARTUP_BACKOFF_BASE_SECONDS * (2 ** max(0, failure_count - 1)),
        settings.CLOUD_SLOT_STARTUP_BACKOFF_MAX_SECONDS,
    )
    if binding is not None and binding.cloud_slot_id == slot.slot_id:
        binding.cloud_slot_id = None
        binding.status = "pending"
        binding.updated_at = now
        db.add(binding)
    if task is not None and _task_is_active(task):
        task.status = "accepted"
        task.phase = "loading"
        task.queue_status = "waiting_cloud_slot"
        task.queue_position = 0
        task.cloud_slot_id = None
        task.allocated_cloud_slot_id = None
        task.spawned_cloud_slot = None
        task.edge_status = "waiting"
        task.cloud_status = "waiting"
        task.message = message
        task.edge_message = "等待可用的边端 runtime slot"
        task.cloud_message = "cloud slot 启动失败，等待退避后重试"
        task.updated_at = now
        db.add(task)
    slot = update_runtime_slot_state(
        db,
        slot,
        process_state="stopped",
        slot_state="free",
        model_state="empty",
        owner_session_id=None,
        owner_binding_id=None,
        model_type=None,
        task_id=None,
        process_pid=None,
        control_url=None,
        grpc_target=None,
        startup_deadline=None,
        startup_failure_count=failure_count,
        retry_after=now + timedelta(seconds=delay),
        last_error=message[:2000],
        confirmation_status="none",
        last_used_at=now,
    )
    db.commit()
    return slot


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

    startup_loading = slot.process_state == "starting" and slot_active_loading
    if startup_loading and slot.startup_deadline is None:
        return update_runtime_slot_state(
            db,
            slot,
            startup_deadline=datetime.utcnow() + timedelta(seconds=settings.CLOUD_SLOT_STARTUP_TIMEOUT_SECONDS),
            last_used_at=datetime.utcnow(),
        )
    if startup_loading and _startup_deadline_active(slot):
        return slot
    if startup_loading:
        return _recover_expired_cloud_startup(
            db,
            slot,
            binding,
            task,
            f"cloud slot {slot.slot_id} 启动超过 {settings.CLOUD_SLOT_STARTUP_TIMEOUT_SECONDS:g} 秒，已退避重试",
        )

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
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
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

    if (
        bool(getattr(slot, "spawned_by_scheduler", 0))
        and not slot.owner_binding_id
        and not slot.owner_session_id
        and ready
        and active_request_count == 0
        and not draining
        and (runtime_model_type or runtime_task_id)
    ):
        protected_slot = _protect_ready_released_slot(
            db,
            slot,
            active_request_count=active_request_count,
            runtime_model_type=runtime_model_type,
            runtime_task_id=runtime_task_id,
        )
        if protected_slot is not None:
            return protected_slot
        try:
            await unload_runtime_slot(db, slot, reason=f'reconcile orphan retained slot {slot.slot_id}')
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

    if binding_released or session_finished:
        if ready and active_request_count == 0 and (runtime_model_type or runtime_task_id):
            if binding is not None:
                _release_binding(db, binding)
            protected_slot = _protect_ready_released_slot(
                db,
                slot,
                active_request_count=active_request_count,
                runtime_model_type=runtime_model_type,
                runtime_task_id=runtime_task_id,
            )
            if protected_slot is not None:
                return protected_slot
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
            if binding is not None:
                _release_binding(db, binding)
            protected_slot = _protect_ready_released_slot(
                db,
                slot,
                active_request_count=active_request_count,
                runtime_model_type=runtime_model_type,
                runtime_task_id=runtime_task_id,
            )
            if protected_slot is not None:
                return protected_slot
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


async def reconcile_all_runtime_slots(
    db: Session,
    *,
    failed_slots: list[dict[str, str]] | None = None,
) -> list[str]:
    """逐个对账 runtime slot，并隔离单个 slot 的意外异常。

    常见的 runtime 网络异常由 ``reconcile_runtime_slot`` 转换为明确的 slot
    状态。这里处理更外层的数据库、进程管理或实现异常，确保一个坏 slot
    不会阻止后续 slot 被检查。
    """
    slot_ids = [slot.slot_id for slot in db.query(RuntimeSlot).order_by(RuntimeSlot.slot_id.asc()).all()]
    reconciled: list[str] = []
    for slot_id in slot_ids:
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == slot_id).first()
            if slot is None:
                continue
            await reconcile_runtime_slot(db, slot)
            reconciled.append(slot_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with suppress(Exception):
                db.rollback()
            error = f"{type(exc).__name__}: {exc}"[:512]
            if failed_slots is not None:
                failed_slots.append({"slot_id": slot_id, "error": error})
            logger.exception("runtime slot 对账失败，已跳过当前 slot: slot_id=%s", slot_id)
    return reconciled
