from datetime import datetime

from sqlalchemy.orm import Session

from app.models.models import ScheduleTask
from app.services.schedule_presenter import calc_overall_progress, clamp_progress


def update_task(
    db: Session,
    task: ScheduleTask,
    *,
    status: str | None = None,
    phase: str | None = None,
    phase_progress: int | None = None,
    message: str | None = None,
    edge_progress: int | None = None,
    cloud_progress: int | None = None,
    edge_status: str | None = None,
    cloud_status: str | None = None,
    edge_message: str | None = None,
    cloud_message: str | None = None,
    queue_status: str | None = None,
    queue_position: int | None = None,
    dispatched_at: datetime | None = None,
    strategy_payload: str | None = None,
    error_detail: str | None = None,
    edge_device_id: str | None = None,
    cloud_device_id: str | None = None,
    runtime_binding_id: str | None = None,
    edge_slot_id: str | None = None,
    cloud_slot_id: str | None = None,
    allocated_cloud_slot_id: str | None = None,
    spawned_cloud_slot: str | None = None,
) -> ScheduleTask:
    if status is not None:
        task.status = status
    if phase is not None:
        task.phase = phase
    if message is not None:
        task.message = message
    if edge_progress is not None:
        task.edge_progress = clamp_progress(edge_progress)
    if cloud_progress is not None:
        task.cloud_progress = clamp_progress(cloud_progress)
    if edge_status is not None:
        task.edge_status = edge_status
    if cloud_status is not None:
        task.cloud_status = cloud_status
    if edge_message is not None:
        task.edge_message = edge_message
    if cloud_message is not None:
        task.cloud_message = cloud_message
    if queue_status is not None:
        task.queue_status = queue_status
    if queue_position is not None:
        task.queue_position = queue_position
    if dispatched_at is not None:
        task.dispatched_at = dispatched_at
    if strategy_payload is not None:
        task.strategy_payload = strategy_payload
    if error_detail is not None:
        task.error_detail = error_detail
    if edge_device_id is not None:
        task.edge_device_id = edge_device_id
    if cloud_device_id is not None:
        task.cloud_device_id = cloud_device_id
    if runtime_binding_id is not None:
        task.runtime_binding_id = runtime_binding_id
    if edge_slot_id is not None:
        task.edge_slot_id = edge_slot_id
    if cloud_slot_id is not None:
        task.cloud_slot_id = cloud_slot_id
    if allocated_cloud_slot_id is not None:
        task.allocated_cloud_slot_id = allocated_cloud_slot_id
    if spawned_cloud_slot is not None:
        task.spawned_cloud_slot = spawned_cloud_slot

    if phase_progress is not None:
        task.phase_progress = clamp_progress(phase_progress)
    elif task.phase == "loading":
        task.phase_progress = clamp_progress((task.edge_progress + task.cloud_progress) // 2)

    if task.status == "completed":
        task.queue_status = "done"
        task.queue_position = 0
        task.phase = "completed"
        task.phase_progress = 100
        task.overall_progress = 100
    elif task.status == "failed":
        task.queue_status = "done"
        task.queue_position = 0
        task.overall_progress = calc_overall_progress(task.phase, task.phase_progress)
    else:
        task.overall_progress = calc_overall_progress(task.phase, task.phase_progress)

    task.updated_at = datetime.utcnow()
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def fail_task(db: Session, task: ScheduleTask, message: str, error_detail: str | None = None) -> None:
    update_task(
        db,
        task,
        status="failed",
        message=message,
        error_detail=error_detail or message,
    )
