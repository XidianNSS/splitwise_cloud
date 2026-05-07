import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db.database import Base, engine
from app.models.models import init_db_data
from app.services.schedule_orchestrator import promote_next_queued_strategy_task
from app.services.schedule_recovery import (
    bootstrap_schedule_queues_on_startup,
    recover_schedule_tasks_on_startup,
)

logger = logging.getLogger("AppLifespan")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    init_db_data()

    recover_schedule_tasks_on_startup()

    app.state.schedule_bootstrap_task = asyncio.create_task(
        bootstrap_schedule_queues_on_startup(promote_next_queued_strategy_task)
    )
    logger.info("调度队列恢复任务已启动")

    try:
        yield
    finally:
        schedule_bootstrap_task = getattr(app.state, "schedule_bootstrap_task", None)
        if schedule_bootstrap_task is not None:
            schedule_bootstrap_task.cancel()
