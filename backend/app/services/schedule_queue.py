from sqlalchemy.orm import Session

from app.models.models import ScheduleTask
from app.services.schedule_task_service import update_task

STRATEGY_RUNNING_STATUS = "running_strategy"
STRATEGY_QUEUED_STATUS = "queued_strategy"
LOADING_RUNNING_STATUS = "running_loading"
WAITING_CLOUD_SLOT_STATUS = "waiting_cloud_slot"


def find_running_strategy_task(db: Session) -> ScheduleTask | None:
    return (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.queue_status == STRATEGY_RUNNING_STATUS,
            ScheduleTask.status.in_(["accepted", "running"]),
            ScheduleTask.phase == "strategy",
        )
        .order_by(ScheduleTask.created_at.asc(), ScheduleTask.task_id.asc())
        .first()
    )


def count_queued_strategy_tasks(db: Session) -> int:
    return (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.queue_status == STRATEGY_QUEUED_STATUS,
            ScheduleTask.status == "accepted",
            ScheduleTask.phase == "strategy",
        )
        .count()
    )


def recalculate_strategy_queue_positions(db: Session) -> None:
    queued_tasks = (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.queue_status == STRATEGY_QUEUED_STATUS,
            ScheduleTask.status == "accepted",
            ScheduleTask.phase == "strategy",
        )
        .order_by(ScheduleTask.created_at.asc(), ScheduleTask.task_id.asc())
        .all()
    )
    for index, queued_task in enumerate(queued_tasks, start=1):
        update_task(db, queued_task, queue_position=index)


def promote_next_strategy_task(db: Session) -> ScheduleTask | None:
    if find_running_strategy_task(db):
        return None

    next_task = (
        db.query(ScheduleTask)
        .filter(
            ScheduleTask.queue_status == STRATEGY_QUEUED_STATUS,
            ScheduleTask.status == "accepted",
            ScheduleTask.phase == "strategy",
        )
        .order_by(ScheduleTask.created_at.asc(), ScheduleTask.task_id.asc())
        .first()
    )
    if not next_task:
        return None

    next_task = update_task(
        db,
        next_task,
        queue_status=STRATEGY_RUNNING_STATUS,
        queue_position=0,
    )
    recalculate_strategy_queue_positions(db)
    return next_task
