import asyncio
from datetime import datetime

from app.db.database import SessionLocal
from app.models.models import ScheduleTask
from app.services.schedule_queue import (
    LOADING_RUNNING_STATUS,
    STRATEGY_RUNNING_STATUS,
    recalculate_strategy_queue_positions,
)


def recover_schedule_tasks_on_startup() -> None:
    db = SessionLocal()
    try:
        stale_strategy_tasks = (
            db.query(ScheduleTask)
            .filter(
                ScheduleTask.queue_status == STRATEGY_RUNNING_STATUS,
                ScheduleTask.status.in_(["accepted", "running"]),
            )
            .all()
        )
        stale_loading_tasks = (
            db.query(ScheduleTask)
            .filter(
                ScheduleTask.queue_status == LOADING_RUNNING_STATUS,
                ScheduleTask.status.in_(["accepted", "running"]),
            )
            .all()
        )

        for task in stale_strategy_tasks + stale_loading_tasks:
            task.status = "failed"
            task.error_detail = "服务重启导致任务中断，请重新发起"
            task.message = "服务重启导致任务中断，请重新发起"
            task.queue_status = "done"
            task.queue_position = 0
            task.updated_at = datetime.utcnow()
            db.add(task)

        db.commit()
        recalculate_strategy_queue_positions(db)
    finally:
        db.close()


async def bootstrap_schedule_queues_on_startup(promote_next_queued_strategy_task) -> None:
    await asyncio.sleep(2)
    await promote_next_queued_strategy_task()
