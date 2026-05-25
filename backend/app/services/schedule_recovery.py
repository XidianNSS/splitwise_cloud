import asyncio
from datetime import datetime

from app.db.database import SessionLocal
from app.models.models import EdgeSession, ScheduleTask
from app.services.schedule_queue import (
    LOADING_RUNNING_STATUS,
    STRATEGY_RUNNING_STATUS,
    recalculate_strategy_queue_positions,
)
from app.services.slot_reaper import cleanup_runtime_slots_for_session, mark_expired_sessions, release_bindings_for_session
from app.services.startup_recovery_service import recover_runtime_ownership_on_startup


async def _cleanup_runtime_slots_for_session_with_new_session(session_id: str) -> None:
    db = SessionLocal()
    try:
        await cleanup_runtime_slots_for_session(db, session_id)
    finally:
        db.close()


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
        mark_expired_sessions(db)
        recover_runtime_ownership_on_startup(db)
        expired_sessions = db.query(EdgeSession).filter(EdgeSession.status == "expired").all()
        for session in expired_sessions:
            release_bindings_for_session(db, session.session_id)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_cleanup_runtime_slots_for_session_with_new_session(session.session_id))
            else:
                loop.create_task(_cleanup_runtime_slots_for_session_with_new_session(session.session_id))
        recalculate_strategy_queue_positions(db)
    finally:
        db.close()


async def bootstrap_schedule_queues_on_startup(promote_next_queued_strategy_task) -> None:
    await asyncio.sleep(2)
    await promote_next_queued_strategy_task()
