from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.models import RuntimeSlot
import logging

from app.services.decode_server_process_manager import current_runtime_env_metadata, start_decode_server_process_for_slot_locked, wait_for_slot_health
from app.services.managed_cloud_slot_cleanup_service import prepare_managed_cloud_slot_for_start, stop_and_clear_managed_cloud_slot
from app.services.runtime_slot_service import ensure_runtime_slot
from app.services.runtime_state_transition_service import transition_runtime_slot
from app.core.config import settings


logger = logging.getLogger("ManagedCloudSlotBootstrap")


async def bootstrap_managed_cloud_slots(db: Session) -> None:
    _, env_file_name = current_runtime_env_metadata()

    slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-0").first()
    if slot is None:
        slot = ensure_runtime_slot(
            db,
            slot_id="cloud-slot-0",
            role="cloud",
            process_state="stopped",
            slot_index=0,
            spawned_by_scheduler=True,
            base_env_name=env_file_name,
        )
    else:
        slot = ensure_runtime_slot(
            db,
            slot_id="cloud-slot-0",
            role="cloud",
            process_state=slot.process_state,
            slot_index=0,
            spawned_by_scheduler=True,
            base_env_name=env_file_name,
            process_pid=slot.process_pid,
        )

    if slot.process_state == "running" and slot.control_url:
        health_ok = await wait_for_slot_health(slot.control_url, timeout_seconds=5.0)
        if health_ok:
            return
        slot, stopped_ok = stop_and_clear_managed_cloud_slot(db, slot)
        if not stopped_ok:
            logger.exception("cloud-slot-0 旧进程无法停止，保持 failed/needs_reconcile，backend 继续启动")
            return

    slot, prepared_ok = prepare_managed_cloud_slot_for_start(db, slot)
    if not prepared_ok:
        logger.exception("cloud-slot-0 残留进程无法清理，保持 failed/needs_reconcile，backend 继续启动")
        return

    process_info = await start_decode_server_process_for_slot_locked("cloud-slot-0", 0)
    health_ok = await wait_for_slot_health(
        process_info.control_url,
        timeout_seconds=settings.CLOUD_SLOT_STARTUP_TIMEOUT_SECONDS,
        slot_id=process_info.slot_id,
        process_pid=process_info.process_pid,
    )
    if not health_ok:
        from app.services.decode_server_process_manager import stop_slot_process
        stopped_ok = stop_slot_process(process_info.slot_id, process_pid=process_info.process_pid)
        failure_count = int(getattr(slot, "startup_failure_count", 0) or 0) + 1
        if stopped_ok:
            transition_runtime_slot(
                db,
                slot,
                process_state="stopped",
                slot_state="free",
                model_state="empty",
                process_pid=None,
                control_url=None,
                grpc_target=None,
                startup_deadline=None,
                startup_failure_count=failure_count,
                retry_after=None,
                last_error="cloud-slot-0 backend bootstrap 健康检查失败",
            )
            logger.error("cloud-slot-0 启动后健康检查失败，进程已停止，后续调度可重新启动")
        else:
            transition_runtime_slot(
                db,
                slot,
                process_state="failed",
                slot_state="needs_reconcile",
                model_state="failed",
                process_pid=process_info.process_pid,
                startup_failure_count=failure_count,
                last_error="cloud-slot-0 backend bootstrap 健康检查失败且进程无法停止",
            )
            logger.error("cloud-slot-0 启动后健康检查失败且进程无法停止，slot 已标记 failed/needs_reconcile")
        return

    slot = ensure_runtime_slot(
        db,
        slot_id=process_info.slot_id,
        role="cloud",
        control_url=process_info.control_url,
        grpc_target=process_info.grpc_target,
        process_state="running",
        slot_index=0,
        spawned_by_scheduler=True,
        base_env_name=env_file_name,
        process_pid=process_info.process_pid,
    )
    transition_runtime_slot(
        db,
        slot,
        process_state="running",
        slot_state="free",
        model_state="empty",
        owner_session_id=None,
        owner_binding_id=None,
        model_type=None,
        task_id=None,
        process_idle_deadline=None,
        startup_deadline=None,
        startup_failure_count=0,
        retry_after=None,
        last_error=None,
    )
