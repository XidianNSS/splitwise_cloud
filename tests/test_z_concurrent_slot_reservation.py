import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


os.environ.setdefault(
    "SQLITE_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="splitwise-cloud-concurrency-test-"), "test.db"),
)

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "backend"))

from app.db.database import Base, SessionLocal, engine
from app.models.models import Device, EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services.decode_server_process_manager import _SLOT_PROCESSES, stop_slot_process
from app.services.runtime_slot_reconcile_service import reconcile_runtime_slot
from app.services.runtime_slot_service import ensure_runtime_slot
from app.services.runtime_state_transition_service import (
    RuntimeTransitionConflict,
    transition_runtime_slot,
)
from app.services.schedule_orchestrator import (
    RuntimeSlotCapacityUnavailable,
    _reserve_edge_slot_for_task,
    allocate_cloud_slot_for_task,
    dispatch_loading_task,
    handle_runtime_progress,
    promote_waiting_loading_task,
)


class ConcurrentSlotReservationTest(unittest.TestCase):
    def setUp(self) -> None:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        _SLOT_PROCESSES.clear()

    def _add_task(self, task_id: str, binding_id: str, session_id: str) -> None:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            db.add(EdgeSession(
                session_id=session_id,
                openwebui_user_id="user",
                edge_device_id="edge-device",
                edge_ip="127.0.0.1",
                cloud_device_id="cloud",
                model_type="Llama-3.2-3B-Instruct",
                status="active",
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=now + timedelta(hours=2),
                lease_expires_at=now + timedelta(hours=2),
            ))
            db.add(RuntimeBinding(
                binding_id=binding_id,
                session_id=session_id,
                task_id=task_id,
                edge_slot_id=f"edge-{session_id}",
                cloud_slot_id=None,
                status="pending",
            ))
            db.add(ScheduleTask(
                task_id=task_id,
                openwebui_user_id="user",
                edge_session_id=session_id,
                runtime_binding_id=binding_id,
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                queue_status="running_loading",
                edge_slot_id=f"edge-{session_id}",
                strategy_payload='{"layer_partitions": []}',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))
            db.commit()
        finally:
            db.close()

    def test_stale_slot_snapshot_cannot_overwrite_new_allocation(self) -> None:
        setup_db = SessionLocal()
        try:
            ensure_runtime_slot(
                setup_db,
                slot_id="edge-slot-shared",
                role="edge",
                process_state="running",
            )
        finally:
            setup_db.close()

        stale_db = SessionLocal()
        owner_db = SessionLocal()
        try:
            stale_slot = stale_db.query(RuntimeSlot).filter(
                RuntimeSlot.slot_id == "edge-slot-shared"
            ).one()
            current_slot = owner_db.query(RuntimeSlot).filter(
                RuntimeSlot.slot_id == "edge-slot-shared"
            ).one()
            transition_runtime_slot(
                owner_db,
                current_slot,
                slot_state="bound",
                model_state="loading",
                owner_session_id="session-new",
                owner_binding_id="binding-new",
                task_id="task-new",
                model_type="Llama-3.2-3B-Instruct",
            )

            with self.assertRaises(RuntimeTransitionConflict):
                transition_runtime_slot(
                    stale_db,
                    stale_slot,
                    slot_state="free",
                    model_state="empty",
                )
        finally:
            stale_db.close()
            owner_db.close()

        verify_db = SessionLocal()
        try:
            slot = verify_db.query(RuntimeSlot).filter(
                RuntimeSlot.slot_id == "edge-slot-shared"
            ).one()
            self.assertEqual(slot.owner_binding_id, "binding-new")
            self.assertEqual(slot.task_id, "task-new")
            self.assertEqual(slot.slot_state, "bound")
        finally:
            verify_db.close()

    def test_stopped_slot_is_reserved_before_startup_await(self) -> None:
        db = SessionLocal()
        try:
            ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="stopped",
            )
        finally:
            db.close()
        self._add_task("task-1", "binding-1", "session-1")
        self._add_task("task-2", "binding-2", "session-2")

        process_info = MagicMock(
            slot_id="cloud-slot-0",
            slot_index=0,
            control_url="http://127.0.0.1:19020/load_strategy",
            grpc_target="127.0.0.1:59020",
            process_pid=11111,
        )

        async def exercise() -> tuple[RuntimeSlot, Exception]:
            health_entered = asyncio.Event()
            release_health = asyncio.Event()

            async def delayed_health(*args, **kwargs):
                health_entered.set()
                await release_health.wait()
                return True

            db1 = SessionLocal()
            db2 = SessionLocal()
            try:
                task1 = db1.query(ScheduleTask).filter(ScheduleTask.task_id == "task-1").one()
                task2 = db2.query(ScheduleTask).filter(ScheduleTask.task_id == "task-2").one()
                with (
                    patch(
                        "app.services.schedule_orchestrator.start_decode_server_process_for_slot_locked",
                        new=AsyncMock(return_value=process_info),
                    ),
                    patch(
                        "app.services.schedule_orchestrator.wait_for_slot_health",
                        new=AsyncMock(side_effect=delayed_health),
                    ),
                    patch("app.services.schedule_orchestrator.settings.CLOUD_SLOT_MAX_COUNT", 1),
                ):
                    first = asyncio.create_task(allocate_cloud_slot_for_task(db1, task1))
                    await health_entered.wait()
                    with self.assertRaises(RuntimeSlotCapacityUnavailable) as raised:
                        await allocate_cloud_slot_for_task(db2, task2)
                    release_health.set()
                    slot, _ = await first
                return slot, raised.exception
            finally:
                db1.close()
                db2.close()

        slot, second_error = asyncio.run(exercise())
        self.assertIn("没有可原子预留", str(second_error))
        self.assertEqual(slot.owner_binding_id, "binding-1")
        self.assertEqual(slot.task_id, "task-1")
        db = SessionLocal()
        try:
            task2 = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-2").one()
            self.assertIsNone(task2.cloud_slot_id)
            self.assertIsNone(task2.allocated_cloud_slot_id)
        finally:
            db.close()

    def test_pid_mismatch_cannot_stop_newer_process(self) -> None:
        process = MagicMock()
        process.pid = 22222
        process.poll.return_value = None
        _SLOT_PROCESSES["cloud-slot-0"] = process

        self.assertFalse(stop_slot_process("cloud-slot-0", process_pid=11111))
        process.terminate.assert_not_called()
        self.assertIs(_SLOT_PROCESSES["cloud-slot-0"], process)

    def test_three_edges_competing_for_two_cloud_slots_reserve_exactly_two(self) -> None:
        db = SessionLocal()
        try:
            for index in range(2):
                slot = ensure_runtime_slot(
                    db,
                    slot_id=f"cloud-slot-{index}",
                    role="cloud",
                    control_url=f"http://127.0.0.1:{19020 + index}/load_strategy",
                    grpc_target=f"127.0.0.1:{59020 + index}",
                    slot_index=index,
                    spawned_by_scheduler=True,
                    process_state="running",
                )
                transition_runtime_slot(db, slot, slot_state="free", model_state="empty")
        finally:
            db.close()
        for index in range(3):
            self._add_task(f"task-{index}", f"binding-{index}", f"session-{index}")

        async def reserve(task_id: str):
            task_db = SessionLocal()
            try:
                task = task_db.query(ScheduleTask).filter(ScheduleTask.task_id == task_id).one()
                return await allocate_cloud_slot_for_task(task_db, task)
            finally:
                task_db.close()

        async def exercise():
            with patch("app.services.schedule_orchestrator.settings.CLOUD_SLOT_MAX_COUNT", 2):
                return await asyncio.gather(
                    *(reserve(f"task-{index}") for index in range(3)),
                    return_exceptions=True,
                )

        results = asyncio.run(exercise())
        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(successes), 2)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeSlotCapacityUnavailable)
        self.assertEqual({result[0].slot_id for result in successes}, {"cloud-slot-0", "cloud-slot-1"})
        self.assertEqual({result[0].task_id for result in successes}, {"task-0", "task-1"})

    def test_two_sessions_on_same_physical_edge_cannot_share_edge_slot(self) -> None:
        self._add_task("task-1", "binding-1", "session-1")
        self._add_task("task-2", "binding-2", "session-2")
        db = SessionLocal()
        try:
            edge_slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-shared-device",
                role="edge",
                control_url="http://127.0.0.1:9001/load_strategy",
                process_state="running",
            )
            task1 = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-1").one()
            task2 = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-2").one()
            reserved = asyncio.run(_reserve_edge_slot_for_task(db, edge_slot, task1))
            self.assertEqual(reserved.owner_binding_id, "binding-1")
            refreshed = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == edge_slot.slot_id).one()
            with self.assertRaises(RuntimeSlotCapacityUnavailable):
                asyncio.run(_reserve_edge_slot_for_task(db, refreshed, task2))
        finally:
            db.close()

    def test_strict_fifo_retries_blocked_head_before_later_edge(self) -> None:
        self._add_task("task-head", "binding-head", "session-head")
        self._add_task("task-later", "binding-later", "session-later")
        db = SessionLocal()
        try:
            head = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-head").one()
            later = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-later").one()
            head.status = later.status = "accepted"
            head.queue_status = later.queue_status = "waiting_cloud_slot"
            head.created_at = datetime.utcnow() - timedelta(seconds=10)
            later.created_at = datetime.utcnow()
            db.add_all([head, later])
            db.commit()
        finally:
            db.close()

        async def exercise(dispatch_mock):
            self.assertTrue(await promote_waiting_loading_task())
            await asyncio.sleep(0)
            retry_db = SessionLocal()
            try:
                head = retry_db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-head").one()
                head.status = "accepted"
                head.queue_status = "waiting_cloud_slot"
                retry_db.add(head)
                retry_db.commit()
            finally:
                retry_db.close()
            self.assertTrue(await promote_waiting_loading_task())
            await asyncio.sleep(0)
            return [call.args[0] for call in dispatch_mock.await_args_list]

        with patch(
            "app.services.schedule_orchestrator.dispatch_loading_task",
            new=AsyncMock(),
        ) as dispatch_mock:
            promoted_ids = asyncio.run(exercise(dispatch_mock))
        self.assertEqual(promoted_ids, ["task-head", "task-head"])
        db = SessionLocal()
        try:
            later = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-later").one()
            self.assertEqual(later.queue_status, "waiting_cloud_slot")
        finally:
            db.close()

    def test_slot_in_backoff_is_skipped_without_blocking_healthy_slot(self) -> None:
        self._add_task("task-1", "binding-1", "session-1")
        db = SessionLocal()
        try:
            backed_off = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="stopped",
            )
            transition_runtime_slot(
                db,
                backed_off,
                slot_state="free",
                model_state="empty",
                startup_failure_count=2,
                retry_after=datetime.utcnow() + timedelta(minutes=5),
                last_error="startup failed",
            )
            healthy = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-1",
                role="cloud",
                control_url="http://127.0.0.1:19021/load_strategy",
                grpc_target="127.0.0.1:59021",
                slot_index=1,
                spawned_by_scheduler=True,
                process_state="running",
            )
            transition_runtime_slot(db, healthy, slot_state="free", model_state="empty")
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-1").one()
            slot, spawned = asyncio.run(allocate_cloud_slot_for_task(db, task))
            self.assertEqual(slot.slot_id, "cloud-slot-1")
            self.assertFalse(spawned)
        finally:
            db.close()

    def test_runtime_progress_for_one_edge_does_not_mutate_another_task(self) -> None:
        self._add_task("task-1", "binding-1", "session-1")
        self._add_task("task-2", "binding-2", "session-2")
        db = SessionLocal()
        try:
            for index in (1, 2):
                slot = ensure_runtime_slot(
                    db,
                    slot_id=f"edge-session-{index}",
                    role="edge",
                    control_url=f"http://127.0.0.1:{9000 + index}/load_strategy",
                    process_state="running",
                )
                if index == 1:
                    binding = db.query(RuntimeBinding).filter(
                        RuntimeBinding.binding_id == "binding-1"
                    ).one()
                    binding.status = "binding"
                    transition_runtime_slot(
                        db,
                        slot,
                        slot_state="bound",
                        model_state="loading",
                        owner_session_id="session-1",
                        owner_binding_id="binding-1",
                        task_id="task-1",
                        model_type="Llama-3.2-3B-Instruct",
                    )
                    db.add(binding)
                    db.commit()
                else:
                    transition_runtime_slot(db, slot, slot_state="free", model_state="empty")
        finally:
            db.close()

        payload = SimpleNamespace(
            task_id="task-1",
            status="loading",
            progress=50,
            message="edge one loading",
            stage="runtime_load",
            node_role="edge",
        )
        result = asyncio.run(handle_runtime_progress(payload, callback_role="edge"))
        self.assertEqual(result["status"], "success")
        db = SessionLocal()
        try:
            task1 = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-1").one()
            task2 = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-2").one()
            slot1 = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "edge-session-1").one()
            slot2 = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "edge-session-2").one()
            self.assertEqual(task1.edge_runtime_load_progress, 50)
            self.assertEqual(task2.edge_runtime_load_progress, 0)
            self.assertEqual(slot1.model_state, "loading")
            self.assertEqual(slot2.model_state, "empty")
        finally:
            db.close()

    def test_third_edge_dispatch_waits_instead_of_failing_when_cloud_pool_is_full(self) -> None:
        self._add_task("task-1", "binding-1", "session-1")
        db = SessionLocal()
        try:
            db.add_all([
                Device(id="edge-1", name="edge-1", value="10.0.0.1:9100", device_type="edge"),
                Device(id="cloud", name="cloud", value="10.0.0.10:9100", device_type="cloud"),
            ])
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-1").one()
            task.edge_device_id = "edge-1"
            task.cloud_device_id = "cloud"
            edge_slot = ensure_runtime_slot(
                db,
                slot_id=task.edge_slot_id,
                role="edge",
                control_url="http://10.0.0.1:9001/load_strategy",
                process_state="running",
            )
            db.add(task)
            db.commit()
        finally:
            db.close()

        with (
            patch(
                "app.services.schedule_orchestrator._reserve_edge_slot_for_task",
                new=AsyncMock(return_value=edge_slot),
            ),
            patch(
                "app.services.schedule_orchestrator.allocate_cloud_slot_for_task",
                new=AsyncMock(side_effect=RuntimeSlotCapacityUnavailable("pool full")),
            ),
        ):
            asyncio.run(dispatch_loading_task("task-1"))

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-1").one()
            binding = db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == "binding-1").one()
            self.assertEqual(task.status, "accepted")
            self.assertEqual(task.queue_status, "waiting_cloud_slot")
            self.assertIsNone(task.cloud_slot_id)
            self.assertIsNone(task.allocated_cloud_slot_id)
            self.assertEqual(binding.status, "pending")
            self.assertIsNone(binding.cloud_slot_id)
        finally:
            db.close()

    def test_reconcile_preserves_starting_until_deadline_then_requeues(self) -> None:
        self._add_task("task-1", "binding-1", "session-1")
        db = SessionLocal()
        try:
            binding = db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == "binding-1").one()
            binding.cloud_slot_id = "cloud-slot-0"
            binding.status = "binding"
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-1").one()
            task.cloud_slot_id = "cloud-slot-0"
            task.allocated_cloud_slot_id = "cloud-slot-0"
            slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="starting",
            )
            slot = transition_runtime_slot(
                db,
                slot,
                process_state="starting",
                slot_state="bound",
                model_state="loading",
                owner_session_id="session-1",
                owner_binding_id="binding-1",
                task_id="task-1",
                model_type="Llama-3.2-3B-Instruct",
                startup_deadline=datetime.utcnow() + timedelta(seconds=60),
            )
            db.add_all([binding, task])
            db.commit()

            preserved = asyncio.run(reconcile_runtime_slot(db, slot))
            self.assertEqual(preserved.process_state, "starting")
            self.assertEqual(preserved.owner_binding_id, "binding-1")

            preserved.startup_deadline = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
            with patch(
                "app.services.runtime_slot_reconcile_service.stop_slot_process",
                return_value=True,
            ):
                recovered = asyncio.run(reconcile_runtime_slot(db, preserved))
            self.assertEqual(recovered.process_state, "stopped")
            self.assertEqual(recovered.slot_state, "free")
            self.assertGreater(recovered.startup_failure_count, 0)
            self.assertIsNotNone(recovered.retry_after)
            db.refresh(task)
            db.refresh(binding)
            self.assertEqual(task.queue_status, "waiting_cloud_slot")
            self.assertIsNone(task.cloud_slot_id)
            self.assertEqual(binding.status, "pending")
            self.assertIsNone(binding.cloud_slot_id)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
