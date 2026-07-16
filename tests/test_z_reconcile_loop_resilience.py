import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
os.environ.setdefault(
    "SQLITE_DB_PATH",
    str(Path(tempfile.mkdtemp(prefix="splitwise-reconcile-test-")) / "state.db"),
)
sys.path.insert(0, str(BACKEND_DIR))

from app.core import lifespan as lifespan_module
from app.api.v1 import admin_runtime
from app.services import runtime_slot_reconcile_service, slot_reaper


def successful_cycle_report() -> dict:
    return {
        "started_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:00:01",
        "latency_ms": 10.0,
        "expired_sessions": 0,
        "stopped_cloud_slots": [],
        "reconciled_slots": ["cloud-slot-0"],
        "failed_slots": [],
        "waiting_task_promoted": False,
        "errors": [],
    }


class ReconcileLoopResilienceTest(unittest.TestCase):
    def test_db_step_rolls_back_and_closes_failed_session(self) -> None:
        db = MagicMock()

        with patch.object(lifespan_module, "SessionLocal", return_value=db):
            result, error = lifespan_module._run_db_maintenance_step(
                "broken_step",
                MagicMock(side_effect=RuntimeError("database locked")),
            )

        self.assertIsNone(result)
        self.assertEqual(error, "RuntimeError: database locked")
        db.rollback.assert_called_once_with()
        db.close.assert_called_once_with()

    def test_db_session_creation_failure_is_returned_to_cycle(self) -> None:
        with patch.object(
            lifespan_module,
            "SessionLocal",
            side_effect=RuntimeError("database unavailable"),
        ):
            result, error = lifespan_module._run_db_maintenance_step(
                "open_session",
                MagicMock(),
            )

        self.assertIsNone(result)
        self.assertEqual(error, "RuntimeError: database unavailable")

    def test_cycle_continues_after_one_step_fails(self) -> None:
        sessions = [MagicMock() for _ in range(4)]
        stop_idle = MagicMock(return_value=[])
        reconcile_ownership = MagicMock()
        reconcile_slots = AsyncMock(return_value=["cloud-slot-0"])
        promote_waiting = AsyncMock(return_value=False)

        with (
            patch.object(lifespan_module, "SessionLocal", side_effect=sessions),
            patch.object(
                lifespan_module,
                "mark_expired_sessions",
                side_effect=RuntimeError("database locked"),
            ),
            patch.object(lifespan_module, "stop_idle_spawned_cloud_slots", stop_idle),
            patch.object(lifespan_module, "reconcile_runtime_ownership", reconcile_ownership),
            patch.object(lifespan_module, "reconcile_all_runtime_slots", reconcile_slots),
            patch.object(lifespan_module, "promote_waiting_loading_task", promote_waiting),
        ):
            report = asyncio.run(lifespan_module._run_slot_process_reaper_cycle())

        stop_idle.assert_called_once()
        reconcile_ownership.assert_called_once()
        reconcile_slots.assert_awaited_once()
        promote_waiting.assert_awaited_once()
        self.assertEqual(report["reconciled_slots"], ["cloud-slot-0"])
        self.assertEqual(report["errors"][0]["step"], "mark_expired_sessions")
        sessions[0].rollback.assert_called_once_with()
        for db in sessions:
            db.close.assert_called_once_with()

    def test_loop_retries_after_unhandled_cycle_error(self) -> None:
        state = lifespan_module._new_runtime_maintenance_state()
        cycle = AsyncMock(
            side_effect=[
                RuntimeError("unexpected"),
                successful_cycle_report(),
                asyncio.CancelledError(),
            ]
        )

        with (
            patch.object(
                lifespan_module.settings,
                "RUNTIME_SLOT_RECONCILE_INTERVAL_SECONDS",
                0,
            ),
            patch.object(lifespan_module, "_run_slot_process_reaper_cycle", cycle),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(lifespan_module._slot_process_reaper_loop(state))

        self.assertEqual(cycle.await_count, 3)
        self.assertEqual(state["total_cycles"], 2)
        self.assertEqual(state["status"], "healthy")
        self.assertEqual(state["consecutive_failure_count"], 0)
        self.assertFalse(state["task_running"])

    def test_one_runtime_slot_failure_does_not_block_following_slot(self) -> None:
        db = MagicMock()
        query = db.query.return_value
        query.order_by.return_value.all.return_value = [
            SimpleNamespace(slot_id="cloud-slot-0"),
            SimpleNamespace(slot_id="cloud-slot-1"),
        ]
        slot_zero = SimpleNamespace(slot_id="cloud-slot-0")
        slot_one = SimpleNamespace(slot_id="cloud-slot-1")
        query.filter.return_value.first.side_effect = [slot_zero, slot_one]
        failures: list[dict[str, str]] = []

        with patch.object(
            runtime_slot_reconcile_service,
            "reconcile_runtime_slot",
            new=AsyncMock(side_effect=[RuntimeError("broken slot"), slot_one]),
        ):
            reconciled = asyncio.run(
                runtime_slot_reconcile_service.reconcile_all_runtime_slots(
                    db,
                    failed_slots=failures,
                )
            )

        self.assertEqual(reconciled, ["cloud-slot-1"])
        self.assertEqual(failures[0]["slot_id"], "cloud-slot-0")
        db.rollback.assert_called_once_with()

    def test_waiting_task_failure_degrades_cycle_without_raising(self) -> None:
        sessions = [MagicMock() for _ in range(4)]
        with (
            patch.object(lifespan_module, "SessionLocal", side_effect=sessions),
            patch.object(lifespan_module, "mark_expired_sessions", return_value=0),
            patch.object(lifespan_module, "stop_idle_spawned_cloud_slots", return_value=[]),
            patch.object(lifespan_module, "reconcile_runtime_ownership"),
            patch.object(
                lifespan_module,
                "reconcile_all_runtime_slots",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                lifespan_module,
                "promote_waiting_loading_task",
                new=AsyncMock(side_effect=RuntimeError("queue unavailable")),
            ),
        ):
            report = asyncio.run(lifespan_module._run_slot_process_reaper_cycle())

        self.assertEqual(report["errors"][-1]["step"], "promote_waiting_loading_task")
        self.assertFalse(report["waiting_task_promoted"])

    def test_one_idle_process_failure_does_not_block_following_slot(self) -> None:
        db = MagicMock()
        query = db.query.return_value
        query.filter.return_value.all.return_value = [
            SimpleNamespace(slot_id="cloud-slot-0"),
            SimpleNamespace(slot_id="cloud-slot-1"),
        ]
        slot_zero = SimpleNamespace(slot_id="cloud-slot-0")
        slot_one = SimpleNamespace(slot_id="cloud-slot-1")
        query.filter.return_value.first.side_effect = [slot_zero, slot_one]
        failures: list[dict[str, str]] = []

        with patch.object(
            slot_reaper,
            "stop_and_clear_managed_cloud_slot",
            side_effect=[RuntimeError("cannot stop"), (slot_one, True)],
        ):
            stopped = slot_reaper.stop_idle_spawned_cloud_slots(
                db,
                failed_slots=failures,
            )

        self.assertEqual(stopped, ["cloud-slot-1"])
        self.assertEqual(failures[0]["slot_id"], "cloud-slot-0")
        db.rollback.assert_called_once_with()

    def test_done_callback_marks_unexpected_exit_as_stopped(self) -> None:
        state = lifespan_module._new_runtime_maintenance_state()
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("worker died")

        lifespan_module._handle_reaper_task_done(task, state)

        self.assertEqual(state["status"], "stopped")
        self.assertFalse(state["task_running"])
        self.assertEqual(state["last_error"], "RuntimeError: worker died")

    def test_admin_payload_exposes_runtime_maintenance_state(self) -> None:
        state = lifespan_module._new_runtime_maintenance_state()
        state["status"] = "healthy"
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(runtime_maintenance_state=state))
        )

        payload = admin_runtime._runtime_maintenance_payload(request)

        self.assertEqual(payload["status"], "healthy")
        self.assertIsNot(payload, state)


if __name__ == "__main__":
    unittest.main()
