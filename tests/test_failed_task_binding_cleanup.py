"""Failure cleanup must release only the failed task's unused allocation."""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.models import EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services import schedule_orchestrator as orchestrator
from app.services import schedule_task_service as tasks


class FailedTaskBindingCleanupTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        now = datetime.utcnow()
        self.db.add(EdgeSession(
            session_id="session", openwebui_user_id="user", edge_device_id="edge",
            edge_ip="127.0.0.1", cloud_device_id="cloud", model_type="BERT-Base-Uncased",
            status="active", expires_at=now + timedelta(hours=1),
            lease_expires_at=now + timedelta(hours=1),
        ))
        self.task = ScheduleTask(
            task_id="new-task", edge_session_id="session", runtime_binding_id="new-binding",
            model_type="BERT-Base-Uncased", status="running", phase="loading",
            queue_status=orchestrator.LOADING_RUNNING_STATUS,
        )
        self.binding = RuntimeBinding(
            binding_id="new-binding", session_id="session", task_id="new-task",
            edge_slot_id="edge-slot", status="pending",
        )
        self.db.add_all([self.task, self.binding])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_failure_releases_pending_binding_idempotently(self):
        for _ in range(2):
            tasks.fail_task(self.db, self.task, "failed")
            self.db.expire_all()
            self.assertEqual(self.task.status, "failed")
            self.assertEqual(self.binding.status, "released")

    def test_failure_preserves_binding_that_owns_a_slot(self):
        self.db.add(RuntimeSlot(
            slot_id="edge-slot", role="edge", control_url="http://unused",
            owner_binding_id="new-binding", owner_session_id="session", task_id="new-task",
        ))
        self.db.commit()
        tasks.fail_task(self.db, self.task, "failed")
        self.assertEqual(self.binding.status, "pending")

    def test_failure_preserves_binding_of_another_task(self):
        self.binding.task_id = "replacement-task"
        self.db.commit()
        tasks.fail_task(self.db, self.task, "failed")
        self.assertEqual(self.binding.status, "pending")

    def test_failure_preserves_bound_binding(self):
        self.binding.status = "binding"
        self.db.commit()
        tasks.fail_task(self.db, self.task, "failed")
        self.assertEqual(self.binding.status, "binding")

    def test_binding_transition_failure_rolls_back_task_failure(self):
        with patch.object(tasks, "transition_runtime_binding", side_effect=RuntimeError("conflict")):
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                tasks.fail_task(self.db, self.task, "failed")
        self.db.expire_all()
        self.assertEqual(self.task.status, "running")
        self.assertEqual(self.binding.status, "pending")

    def test_busy_model_switch_releases_new_binding_and_preserves_old_slots(self):
        old_binding = RuntimeBinding(
            binding_id="old-binding", session_id="session", task_id="old-task", status="binding",
            edge_slot_id="edge-slot", cloud_slot_id="cloud-slot",
        )
        slots = [RuntimeSlot(
            slot_id=f"{role}-slot", role=role, control_url="http://unused",
            process_state="running", model_state="ready", slot_state="bound",
            owner_binding_id="old-binding", owner_session_id="session", task_id="old-task",
            model_type="Llama-3.2-3B-Instruct",
        ) for role in ("edge", "cloud")]
        self.db.add_all([old_binding, *slots])
        self.db.commit()

        async def fail_switch():
            with patch.object(orchestrator, "fetch_runtime_state", new=AsyncMock(
                side_effect=[{"active_request_count": 0}, {"active_request_count": 1}]
            )):
                try:
                    await orchestrator._reload_session_owned_slots_for_task(
                        self.db, self.task, new_binding=self.binding,
                        edge_slot=slots[0], cloud_slot=slots[1],
                    )
                except RuntimeError as exc:
                    detail = str(exc)
                else:
                    self.fail("busy model switch was accepted")
            self.assertIn("仍有推理请求", detail)
            with patch.object(orchestrator, "promote_waiting_loading_task", new=AsyncMock()):
                await orchestrator.fail_task_and_promote(self.db, self.task, "reload failed", detail)

        asyncio.run(fail_switch())
        self.db.expire_all()
        self.assertEqual(self.task.status, "failed")
        self.assertEqual(self.binding.status, "released")
        self.assertEqual(old_binding.status, "binding")
        for slot in slots:
            self.assertEqual(slot.owner_binding_id, "old-binding")
            self.assertEqual(slot.model_state, "ready")
