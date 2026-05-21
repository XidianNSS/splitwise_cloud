from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.decode_server_process_manager import current_runtime_env_metadata, start_decode_server_process_for_slot, wait_for_slot_health
from app.services.runtime_slot_service import ensure_runtime_slot, update_runtime_slot_state


async def bootstrap_managed_cloud_slots(db: Session) -> None:
    _, env_file_name = current_runtime_env_metadata()

    slot = ensure_runtime_slot(
        db,
        slot_id="cloud-slot-0",
        role="cloud",
        process_state="stopped",
        slot_index=0,
        spawned_by_scheduler=True,
        base_env_name=env_file_name,
    )

    if slot.process_state == "running" and slot.control_url:
        health_ok = await wait_for_slot_health(slot.control_url, timeout_seconds=5.0)
        if health_ok:
            return

    process_info = start_decode_server_process_for_slot("cloud-slot-0", 0)
    health_ok = await wait_for_slot_health(process_info.control_url)
    if not health_ok:
        from app.services.decode_server_process_manager import stop_slot_process
        stop_slot_process(process_info.slot_id, process_pid=process_info.process_pid)
        raise RuntimeError("cloud-slot-0 启动后健康检查失败")

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
    update_runtime_slot_state(
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
    )
