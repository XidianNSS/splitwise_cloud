import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.db.database import Base, engine
from app.models.models import init_db_data
from app.services.managed_cloud_slot_bootstrap_service import bootstrap_managed_cloud_slots
from app.services.runtime_slot_reconcile_service import reconcile_all_runtime_slots
from app.services.schedule_orchestrator import promote_next_queued_strategy_task
from app.services.schedule_recovery import (
    bootstrap_schedule_queues_on_startup,
    recover_schedule_tasks_on_startup,
)
from app.services.slot_reaper import mark_expired_sessions, stop_idle_spawned_cloud_slots


def ensure_phase2_schema() -> None:
    from sqlalchemy import text

    expected_columns = {
        "grpc_target": "ALTER TABLE runtime_slots ADD COLUMN grpc_target VARCHAR",
        "process_pid": "ALTER TABLE runtime_slots ADD COLUMN process_pid INTEGER",
        "spawned_by_scheduler": "ALTER TABLE runtime_slots ADD COLUMN spawned_by_scheduler INTEGER DEFAULT 0",
        "base_env_name": "ALTER TABLE runtime_slots ADD COLUMN base_env_name VARCHAR",
        "slot_index": "ALTER TABLE runtime_slots ADD COLUMN slot_index INTEGER DEFAULT 0",
    }

    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(runtime_slots)"))}
        for column_name, ddl in expected_columns.items():
            if column_name not in existing:
                conn.execute(text(ddl))

logger = logging.getLogger("AppLifespan")


async def _slot_process_reaper_loop() -> None:
    from app.db.database import SessionLocal
    while True:
        await asyncio.sleep(settings.RUNTIME_SLOT_RECONCILE_INTERVAL_SECONDS)
        db = None
        try:
            db = SessionLocal()
            mark_expired_sessions(db)
            stop_idle_spawned_cloud_slots(db)
            await reconcile_all_runtime_slots(db)
        finally:
            if db is not None:
                db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_phase2_schema()
    init_db_data()

    recover_schedule_tasks_on_startup()
    db = None
    try:
        from app.db.database import SessionLocal
        db = SessionLocal()
        mark_expired_sessions(db)
        await reconcile_all_runtime_slots(db)
        await bootstrap_managed_cloud_slots(db)
    finally:
        if db is not None:
            db.close()

    app.state.schedule_bootstrap_task = asyncio.create_task(
        bootstrap_schedule_queues_on_startup(promote_next_queued_strategy_task)
    )
    app.state.slot_process_reaper_task = asyncio.create_task(_slot_process_reaper_loop())
    logger.info("调度队列恢复任务已启动")

    try:
        yield
    finally:
        schedule_bootstrap_task = getattr(app.state, "schedule_bootstrap_task", None)
        if schedule_bootstrap_task is not None:
            schedule_bootstrap_task.cancel()
        slot_process_reaper_task = getattr(app.state, "slot_process_reaper_task", None)
        if slot_process_reaper_task is not None:
            slot_process_reaper_task.cancel()
