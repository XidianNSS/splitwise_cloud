import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from time import perf_counter
from typing import Any, Awaitable, Callable

from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import Base, SessionLocal, engine
from app.models.models import init_db_data
from app.services.managed_cloud_slot_bootstrap_service import bootstrap_managed_cloud_slots
from app.services.runtime_slot_reconcile_service import reconcile_all_runtime_slots
from app.services.startup_recovery_service import reconcile_runtime_ownership
from app.services.schedule_orchestrator import promote_next_queued_strategy_task, promote_waiting_loading_task
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
        "startup_deadline": "ALTER TABLE runtime_slots ADD COLUMN startup_deadline DATETIME",
        "startup_failure_count": "ALTER TABLE runtime_slots ADD COLUMN startup_failure_count INTEGER DEFAULT 0",
        "retry_after": "ALTER TABLE runtime_slots ADD COLUMN retry_after DATETIME",
        "last_error": "ALTER TABLE runtime_slots ADD COLUMN last_error TEXT",
    }
    expected_task_columns = {
        "edge_strategy_progress": "ALTER TABLE schedule_tasks ADD COLUMN edge_strategy_progress INTEGER DEFAULT 0",
        "edge_integrity_progress": "ALTER TABLE schedule_tasks ADD COLUMN edge_integrity_progress INTEGER DEFAULT 0",
        "edge_runtime_load_progress": "ALTER TABLE schedule_tasks ADD COLUMN edge_runtime_load_progress INTEGER DEFAULT 0",
        "cloud_strategy_progress": "ALTER TABLE schedule_tasks ADD COLUMN cloud_strategy_progress INTEGER DEFAULT 0",
        "cloud_integrity_progress": "ALTER TABLE schedule_tasks ADD COLUMN cloud_integrity_progress INTEGER DEFAULT 0",
        "cloud_runtime_load_progress": "ALTER TABLE schedule_tasks ADD COLUMN cloud_runtime_load_progress INTEGER DEFAULT 0",
    }

    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(runtime_slots)"))}
        for column_name, ddl in expected_columns.items():
            if column_name not in existing:
                conn.execute(text(ddl))
        existing_task_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(schedule_tasks)"))}
        for column_name, ddl in expected_task_columns.items():
            if column_name not in existing_task_columns:
                conn.execute(text(ddl))

logger = logging.getLogger("AppLifespan")


def _new_runtime_maintenance_state() -> dict[str, Any]:
    return {
        "status": "starting",
        "task_running": False,
        "last_started_at": None,
        "last_completed_at": None,
        "last_success_at": None,
        "total_cycles": 0,
        "consecutive_failure_count": 0,
        "last_error": None,
        "last_cycle": None,
    }


def _error_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:512]


def _run_db_maintenance_step(
    step_name: str,
    operation: Callable[[Session], Any],
) -> tuple[Any, str | None]:
    db = None
    try:
        db = SessionLocal()
        return operation(db), None
    except Exception as exc:
        if db is not None:
            with suppress(Exception):
                db.rollback()
        logger.exception("后台维护步骤失败，后续步骤将继续: step=%s", step_name)
        return None, _error_summary(exc)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                logger.exception("后台维护数据库 session 关闭失败: step=%s", step_name)


async def _run_async_db_maintenance_step(
    step_name: str,
    operation: Callable[[Session], Awaitable[Any]],
) -> tuple[Any, str | None]:
    db = None
    try:
        db = SessionLocal()
        return await operation(db), None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if db is not None:
            with suppress(Exception):
                db.rollback()
        logger.exception("后台维护步骤失败，后续步骤将继续: step=%s", step_name)
        return None, _error_summary(exc)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                logger.exception("后台维护数据库 session 关闭失败: step=%s", step_name)


async def _run_slot_process_reaper_cycle() -> dict[str, Any]:
    started_at = datetime.utcnow()
    started = perf_counter()
    errors: list[dict[str, str]] = []

    expired_sessions, error = _run_db_maintenance_step(
        "mark_expired_sessions",
        mark_expired_sessions,
    )
    if error:
        errors.append({"step": "mark_expired_sessions", "error": error})

    idle_slot_failures: list[dict[str, str]] = []
    stopped_cloud_slots, error = _run_db_maintenance_step(
        "stop_idle_spawned_cloud_slots",
        lambda db: stop_idle_spawned_cloud_slots(db, failed_slots=idle_slot_failures),
    )
    if error:
        errors.append({"step": "stop_idle_spawned_cloud_slots", "error": error})
    errors.extend(
        {
            "step": "stop_idle_spawned_cloud_slots",
            "slot_id": failure["slot_id"],
            "error": failure["error"],
        }
        for failure in idle_slot_failures
    )

    _, error = _run_db_maintenance_step(
        "reconcile_runtime_ownership",
        reconcile_runtime_ownership,
    )
    if error:
        errors.append({"step": "reconcile_runtime_ownership", "error": error})

    reconcile_failures: list[dict[str, str]] = []
    reconciled_slots, error = await _run_async_db_maintenance_step(
        "reconcile_all_runtime_slots",
        lambda db: reconcile_all_runtime_slots(db, failed_slots=reconcile_failures),
    )
    if error:
        errors.append({"step": "reconcile_all_runtime_slots", "error": error})
    errors.extend(
        {
            "step": "reconcile_runtime_slot",
            "slot_id": failure["slot_id"],
            "error": failure["error"],
        }
        for failure in reconcile_failures
    )

    waiting_task_promoted = False
    try:
        waiting_task_promoted = await promote_waiting_loading_task()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("后台维护步骤失败，下一轮将重试: step=promote_waiting_loading_task")
        errors.append({"step": "promote_waiting_loading_task", "error": _error_summary(exc)})

    return {
        "started_at": started_at.isoformat(),
        "completed_at": datetime.utcnow().isoformat(),
        "latency_ms": round((perf_counter() - started) * 1000, 3),
        "expired_sessions": int(expired_sessions or 0),
        "stopped_cloud_slots": list(stopped_cloud_slots or []),
        "reconciled_slots": list(reconciled_slots or []),
        "failed_slots": reconcile_failures,
        "waiting_task_promoted": bool(waiting_task_promoted),
        "errors": errors,
    }


async def _cancel_background_task(task: asyncio.Task | None, task_name: str) -> None:
    if task is None:
        return
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("%s 已在 shutdown 前异常结束", task_name)
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("%s 在取消时返回异常", task_name)
    logger.info("%s 已取消", task_name)


def _stop_backend_managed_cloud_slots_on_shutdown() -> None:
    from app.db.database import SessionLocal
    from app.models.models import RuntimeSlot
    from app.services.managed_cloud_slot_cleanup_service import stop_and_clear_managed_cloud_slot

    db = SessionLocal()
    try:
        slots = (
            db.query(RuntimeSlot)
            .filter(
                RuntimeSlot.role == "cloud",
                RuntimeSlot.spawned_by_scheduler == 1,
            )
            .all()
        )

        for slot in slots:
            if slot.process_state not in {"running", "starting", "stopping", "failed", "needs_reconcile"}:
                continue

            logger.info(
                "backend shutdown 正在停止托管 cloud slot: slot_id=%s pid=%s control_url=%s grpc_target=%s state=%s/%s",
                slot.slot_id,
                slot.process_pid,
                slot.control_url,
                slot.grpc_target,
                slot.process_state,
                slot.slot_state,
            )

            cleared_slot, stopped_ok = stop_and_clear_managed_cloud_slot(db, slot)

            if stopped_ok:
                logger.info(
                    "backend shutdown 已清理 cloud slot: slot_id=%s process_state=%s slot_state=%s",
                    cleared_slot.slot_id,
                    cleared_slot.process_state,
                    cleared_slot.slot_state,
                )
            else:
                logger.warning(
                    "backend shutdown 清理 cloud slot 失败: slot_id=%s pid=%s",
                    slot.slot_id,
                    slot.process_pid,
                )
    finally:
        db.close()


async def _slot_process_reaper_loop(
    maintenance_state: dict[str, Any] | None = None,
) -> None:
    state = maintenance_state if maintenance_state is not None else _new_runtime_maintenance_state()
    state["task_running"] = True
    while True:
        await asyncio.sleep(settings.RUNTIME_SLOT_RECONCILE_INTERVAL_SECONDS)
        state["last_started_at"] = datetime.utcnow().isoformat()
        try:
            report = await _run_slot_process_reaper_cycle()
        except asyncio.CancelledError:
            state["task_running"] = False
            raise
        except Exception as exc:
            error = _error_summary(exc)
            state["status"] = "degraded"
            state["last_completed_at"] = datetime.utcnow().isoformat()
            state["total_cycles"] += 1
            state["consecutive_failure_count"] += 1
            state["last_error"] = error
            logger.exception("runtime reconcile 后台循环发生未处理异常，下一轮自动重试")
            continue

        state["last_completed_at"] = report["completed_at"]
        state["last_cycle"] = report
        state["total_cycles"] += 1
        if report["errors"]:
            state["status"] = "degraded"
            state["consecutive_failure_count"] += 1
            last_error = report["errors"][-1]
            location = last_error["step"]
            if last_error.get("slot_id"):
                location += f"/{last_error['slot_id']}"
            state["last_error"] = f"{location}: {last_error['error']}"[:512]
        else:
            state["status"] = "healthy"
            state["last_success_at"] = report["completed_at"]
            state["consecutive_failure_count"] = 0
            state["last_error"] = None


def _handle_reaper_task_done(
    task: asyncio.Task,
    maintenance_state: dict[str, Any],
) -> None:
    maintenance_state["task_running"] = False
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    maintenance_state["status"] = "stopped"
    if exc is None:
        maintenance_state["last_error"] = "runtime reconcile 后台任务意外返回"
        logger.critical("runtime reconcile 后台任务意外返回")
        return
    maintenance_state["last_error"] = _error_summary(exc)
    logger.critical(
        "runtime reconcile 后台任务异常退出",
        exc_info=(type(exc), exc, exc.__traceback__),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_phase2_schema()
    init_db_data()

    recover_schedule_tasks_on_startup()
    db = None
    try:
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
    maintenance_state = _new_runtime_maintenance_state()
    maintenance_state["task_running"] = True
    app.state.runtime_maintenance_state = maintenance_state
    app.state.slot_process_reaper_task = asyncio.create_task(
        _slot_process_reaper_loop(maintenance_state)
    )
    app.state.slot_process_reaper_task.add_done_callback(
        lambda task: _handle_reaper_task_done(task, maintenance_state)
    )
    logger.info("调度队列恢复任务和 runtime reconcile 后台任务已启动")

    try:
        yield
    finally:
        schedule_bootstrap_task = getattr(app.state, "schedule_bootstrap_task", None)
        slot_process_reaper_task = getattr(app.state, "slot_process_reaper_task", None)

        await _cancel_background_task(schedule_bootstrap_task, "schedule_bootstrap_task")
        await _cancel_background_task(slot_process_reaper_task, "slot_process_reaper_task")

        _stop_backend_managed_cloud_slots_on_shutdown()
