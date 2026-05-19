import os
import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

TMPDIR = tempfile.mkdtemp(prefix="splitwise-cloud-dev-test-")
TEST_DB_PATH = os.path.join(TMPDIR, "test_cloud_edge.db")
os.environ["SQLITE_DB_PATH"] = TEST_DB_PATH
os.environ.setdefault("OPENWEBUI_SKIP_SIGNATURE_VERIFY", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SERVER_PORT", "18131")
os.environ.setdefault("SERVER_PUBLIC_BASE_URL", "http://127.0.0.1:18131")
os.environ.setdefault("BACKEND_BASE_URL", "http://127.0.0.1:18131")
os.environ.setdefault("RUNTIME_INTEGRITY_TOKEN", "wyy-local-aloepri-integrity")

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
import sys
sys.path.insert(0, str(BACKEND_DIR))

from app.db.database import Base, SessionLocal, engine
from app.main import create_app
from app.api.deps import get_current_openwebui_user_id
from app.models.models import Device, EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services.runtime_binding_service import create_runtime_binding, update_runtime_binding
from app.services.runtime_slot_service import ensure_runtime_slot, update_runtime_slot_state
from app.services.schedule_orchestrator import dispatch_loading_task, promote_waiting_loading_task
from app.services.slot_reaper import cleanup_runtime_slots_for_session, mark_expired_sessions, release_bindings_for_session, stop_idle_spawned_cloud_slots
from app.services.decode_server_process_manager import allocate_cloud_slot_ports


class SessionAndSlotLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    def setUp(self) -> None:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            db.add_all([
                Device(id="cloud", name="cloud", value="127.0.0.1:9100|127.0.0.1:9400", device_type="cloud"),
                Device(id="edge_A", name="edge_A", value="127.0.0.1:9101|127.0.0.1:9401", device_type="edge"),
            ])
            db.commit()
        finally:
            db.close()
        self.app = create_app()
        self.app.dependency_overrides[get_current_openwebui_user_id] = lambda: "user-1"
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.app.dependency_overrides.clear()

    def _create_session(self, *, status: str = "active", lease_expires_at: datetime | None = None) -> EdgeSession:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            session = EdgeSession(
                session_id="session-1",
                openwebui_user_id="user-1",
                edge_device_id="edge_A",
                edge_ip="127.0.0.1",
                cloud_device_id="cloud",
                model_type="Llama-3.2-3B-Instruct",
                status=status,
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=lease_expires_at or (now + timedelta(hours=2)),
                lease_expires_at=lease_expires_at or (now + timedelta(hours=2)),
            )
            db.add(session)
            db.commit()
            db.refresh(session)
            return session
        finally:
            db.close()

    def test_session_heartbeat_refreshes_lease(self) -> None:
        self._create_session()
        response = self.client.post("/api/v1/session/heartbeat", json={"session_id": "session-1"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(payload["status"], "active")
        self.assertIn("lease_expires_at", payload)

    def test_session_close_marks_closed(self) -> None:
        self._create_session()
        response = self.client.post("/api/v1/session/close", json={"session_id": "session-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "closed")

    def test_spawned_slot_cleanup_sets_process_idle_deadline(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-1",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-1",
            )
            ensure_runtime_slot(
                db,
                slot_id="cloud-slot-1",
                role="cloud",
                control_url="http://127.0.0.1:19115/load_strategy",
                grpc_target="127.0.0.1:52165",
                slot_index=1,
                spawned_by_scheduler=True,
                process_state="running",
                process_pid=12345,
                base_env_name=".env.wyy",
            )
            update_runtime_slot_state(
                db,
                db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first(),
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=db.query(RuntimeBinding).filter(RuntimeBinding.session_id == "session-1").first().binding_id,
                model_type="Llama-3.2-3B-Instruct",
                task_id="task-1",
            )
        finally:
            db.close()

        async def run_cleanup() -> None:
            db = SessionLocal()
            try:
                slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
                self.assertIsNotNone(slot)
                with patch("app.services.runtime_control_service.httpx.AsyncClient") as client_cls:
                    client = AsyncMock()
                    state_response = MagicMock()
                    state_response.json.return_value = {
                        "ready": True,
                        "draining": False,
                        "task_id": "task-1",
                        "model_type": "Llama-3.2-3B-Instruct",
                        "active_request_count": 0,
                    }
                    unload_response = MagicMock()
                    unload_response.json.return_value = {"unloaded": True, "reason": "test"}
                    client.get = AsyncMock(return_value=state_response)
                    client.post = AsyncMock(return_value=unload_response)
                    client.__aenter__.return_value = client
                    client.__aexit__.return_value = False
                    client_cls.return_value = client
                    await cleanup_runtime_slots_for_session(db, "session-1")
            finally:
                db.close()

        import asyncio
        asyncio.run(run_cleanup())

        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(slot)
            self.assertEqual(slot.slot_state, "free")
            self.assertEqual(slot.model_state, "empty")
            self.assertEqual(slot.process_state, "running")
            self.assertIsNotNone(slot.process_idle_deadline)
            self.assertEqual(slot.process_pid, 12345)
        finally:
            db.close()

    def test_stop_idle_spawned_cloud_slots_stops_process(self) -> None:
        db = SessionLocal()
        try:
            slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-1",
                role="cloud",
                control_url="http://127.0.0.1:19115/load_strategy",
                grpc_target="127.0.0.1:52165",
                slot_index=1,
                spawned_by_scheduler=True,
                process_state="running",
                process_pid=54321,
                base_env_name=".env.wyy",
            )
            update_runtime_slot_state(
                db,
                slot,
                slot_state="free",
                model_state="empty",
                process_idle_deadline=datetime.utcnow() - timedelta(seconds=1),
            )
            with patch("app.services.slot_reaper.stop_slot_process", return_value=True) as stop_mock:
                stopped = stop_idle_spawned_cloud_slots(db)
            self.assertEqual(stopped, ["cloud-slot-1"])
            stop_mock.assert_called_once_with("cloud-slot-1", process_pid=54321)
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(slot)
            self.assertEqual(slot.process_state, "stopped")
            self.assertIsNone(slot.process_pid)
            self.assertIsNone(slot.process_idle_deadline)
        finally:
            db.close()

    def test_stop_idle_spawned_cloud_slots_marks_reconcile_when_process_stop_fails(self) -> None:
        db = SessionLocal()
        try:
            slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-1",
                role="cloud",
                control_url="http://127.0.0.1:19115/load_strategy",
                grpc_target="127.0.0.1:52165",
                slot_index=1,
                spawned_by_scheduler=True,
                process_state="running",
                process_pid=67890,
                base_env_name=".env.wyy",
            )
            update_runtime_slot_state(
                db,
                slot,
                slot_state="free",
                model_state="empty",
                process_idle_deadline=datetime.utcnow() - timedelta(seconds=1),
            )
            with patch("app.services.slot_reaper.stop_slot_process", return_value=False) as stop_mock:
                stopped = stop_idle_spawned_cloud_slots(db)
            self.assertEqual(stopped, [])
            stop_mock.assert_called_once_with("cloud-slot-1", process_pid=67890)
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(slot)
            self.assertEqual(slot.process_state, "failed")
            self.assertEqual(slot.slot_state, "needs_reconcile")
            self.assertEqual(slot.process_pid, 67890)
        finally:
            db.close()

    def test_session_close_triggers_runtime_cleanup(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-1",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
        finally:
            db.close()
        with patch("app.api.v1.session.cleanup_runtime_slots_for_session", new=AsyncMock(return_value=["edge-slot-edge_A"])) as cleanup_mock:
            response = self.client.post("/api/v1/session/close", json={"session_id": "session-1"})
        self.assertEqual(response.status_code, 200)
        cleanup_mock.assert_awaited_once()

    def test_runtime_slot_service_creates_and_updates_slot(self) -> None:
        db = SessionLocal()
        try:
            slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113")
            self.assertEqual(slot.slot_state, "free")
            slot = update_runtime_slot_state(
                db,
                slot,
                slot_state="bound",
                model_state="ready",
                model_type="Llama-3.2-3B-Instruct",
                task_id="task-1",
                active_request_count=0,
            )
            self.assertEqual(slot.slot_state, "bound")
            self.assertEqual(slot.model_state, "ready")
            self.assertEqual(slot.task_id, "task-1")
        finally:
            db.close()

    def test_runtime_binding_service_creates_and_releases_binding(self) -> None:
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-1",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                partition_digest="sha256:test",
            )
            self.assertEqual(binding.status, "binding")
            binding = update_runtime_binding(db, binding, status="released")
            self.assertEqual(binding.status, "released")
        finally:
            db.close()

    def test_slot_reaper_marks_expired_sessions_and_releases_bindings(self) -> None:
        expired_time = datetime.utcnow() - timedelta(minutes=5)
        self._create_session(lease_expires_at=expired_time)
        db = SessionLocal()
        try:
            create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-1",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            expired_count = mark_expired_sessions(db)
            released_count = release_bindings_for_session(db, "session-1")
            self.assertEqual(expired_count, 1)
            self.assertEqual(released_count, 1)
            session = db.query(EdgeSession).filter(EdgeSession.session_id == "session-1").first()
            binding = db.query(RuntimeBinding).filter(RuntimeBinding.session_id == "session-1").first()
            self.assertIsNotNone(session)
            self.assertIsNotNone(binding)
            self.assertEqual(session.status, "expired")
            self.assertEqual(binding.status, "released")
        finally:
            db.close()

    def test_waiting_task_is_promoted_after_session_close_cleanup(self) -> None:
        db = SessionLocal()
        try:
            busy_slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113")
            update_runtime_slot_state(
                db,
                busy_slot,
                slot_state="bound",
                model_state="ready",
                owner_binding_id="binding-1",
                owner_session_id="session-1",
                model_type="Llama-3.2-3B-Instruct",
                task_id="task-busy",
            )
            waiting_task = ScheduleTask(
                task_id="task-promote",
                openwebui_user_id="user-1",
                edge_session_id="session-2",
                runtime_binding_id="binding-2",
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="waiting",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=0,
                cloud_progress=0,
                edge_status="pending",
                cloud_status="pending",
                queue_status="waiting_cloud_slot",
                queue_position=0,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                edge_message="pending",
                cloud_message="pending",
                strategy_payload='{"layer_partitions": []}',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(waiting_task)
            db.commit()
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-0").first()
            self.assertIsNotNone(slot)
            update_runtime_slot_state(
                db,
                slot,
                slot_state="free",
                model_state="empty",
                owner_binding_id=None,
                owner_session_id=None,
                model_type=None,
                task_id=None,
            )
        finally:
            db.close()

        import asyncio
        with patch("app.services.schedule_orchestrator.dispatch_loading_task", new=AsyncMock()) as dispatch_mock:
            promoted = asyncio.run(promote_waiting_loading_task())

        self.assertTrue(promoted)
        dispatch_mock.assert_awaited_once_with("task-promote")

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-promote").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.queue_status, "running_loading")
            self.assertEqual(task.status, "running")
            self.assertIn("cloud slot 已空闲", task.message)
        finally:
            db.close()

    def test_second_task_allocates_new_cloud_slot_when_cloud_slot_is_busy(self) -> None:
        db = SessionLocal()
        try:
            busy_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
                slot_index=0,
            )
            update_runtime_slot_state(
                db,
                busy_slot,
                slot_state="bound",
                process_state="running",
                model_state="ready",
                owner_binding_id="binding-1",
                owner_session_id="session-1",
                model_type="Llama-3.2-3B-Instruct",
                task_id="task-busy",
            )
            waiting_task = ScheduleTask(
                task_id="task-waiting",
                openwebui_user_id="user-1",
                edge_session_id="session-2",
                runtime_binding_id="binding-2",
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="ready to dispatch",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=0,
                cloud_progress=0,
                edge_status="pending",
                cloud_status="pending",
                queue_status="running_loading",
                queue_position=0,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                edge_message="pending",
                cloud_message="pending",
                strategy_payload='{"layer_partitions": []}',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(waiting_task)
            db.commit()
        finally:
            db.close()

        process_info = MagicMock(
            slot_id="cloud-slot-1",
            slot_index=1,
            http_port=19114,
            grpc_port=52164,
            control_url="http://127.0.0.1:19114/load_strategy",
            grpc_target="127.0.0.1:52164",
            process_pid=54321,
        )

        import asyncio
        with (
            patch("app.services.schedule_orchestrator.start_decode_server_process", return_value=process_info),
            patch("app.services.schedule_orchestrator.wait_for_slot_health", new=AsyncMock(return_value=True)),
            patch("app.services.schedule_orchestrator.dispatch_strategy_to_runtime", new=AsyncMock(return_value={"status": "accepted"})),
        ):
            asyncio.run(dispatch_loading_task("task-waiting"))

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-waiting").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.queue_status, "running_loading")
            self.assertEqual(task.status, "running")
            self.assertEqual(task.cloud_slot_id, "cloud-slot-1")
            self.assertEqual(task.allocated_cloud_slot_id, "cloud-slot-1")
        finally:
            db.close()

    def test_schedule_runtime_observability_endpoints(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-obs",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                partition_digest="sha256:obs",
            )
            ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112")
            ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113")
            waiting_task = ScheduleTask(
                task_id="task-obs",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id=binding.binding_id,
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=50,
                message="waiting for slot",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=0,
                cloud_progress=0,
                edge_status="pending",
                cloud_status="pending",
                queue_status="waiting_cloud_slot",
                queue_position=0,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                edge_message="waiting",
                cloud_message="waiting",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(waiting_task)
            db.commit()
        finally:
            db.close()

        slots_response = self.client.get(
            "/api/v1/schedule/runtime/slots",
            headers={"Authorization": "Bearer dev-token"},
        )
        bindings_response = self.client.get(
            "/api/v1/schedule/runtime/bindings",
            headers={"Authorization": "Bearer dev-token"},
        )
        queue_response = self.client.get(
            "/api/v1/schedule/queue/loading",
            headers={"Authorization": "Bearer dev-token"},
        )

        self.assertEqual(slots_response.status_code, 200)
        self.assertEqual(bindings_response.status_code, 200)
        self.assertEqual(queue_response.status_code, 200)

        slots_payload = slots_response.json()
        bindings_payload = bindings_response.json()
        queue_payload = queue_response.json()

        self.assertEqual(len(slots_payload), 2)
        self.assertEqual(bindings_payload[0]["partition_digest"], "sha256:obs")
        self.assertEqual(queue_payload[0]["queue_status"], "waiting_cloud_slot")
        self.assertEqual(queue_payload[0]["runtime_binding_id"], bindings_payload[0]["binding_id"])

    def test_same_session_cannot_accept_second_active_task(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-active",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                phase_progress=30,
                overall_progress=65,
                message="already running",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=30,
                cloud_progress=30,
                edge_status="loading",
                cloud_status="loading",
                queue_status="running_loading",
                queue_position=0,
                edge_message="loading",
                cloud_message="loading",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(task)
            db.commit()
        finally:
            db.close()

        response = self.client.post(
            "/api/v1/schedule/trigger",
            headers={
                "Authorization": "Bearer dev-token",
                "Session-Id": "session-1",
            },
            json={"model_type": "Llama-3.2-3B-Instruct"},
        )
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["task_id"], "task-active")
        self.assertIn("未完成", payload["message"])


    def test_allocate_cloud_slot_ports_avoids_ports_reserved_by_existing_slots(self) -> None:
        db = SessionLocal()
        try:
            ensure_runtime_slot(
                db,
                slot_id="cloud-slot-1",
                role="cloud",
                control_url="http://127.0.0.1:19115/load_strategy",
                grpc_target="127.0.0.1:52165",
                slot_index=1,
                spawned_by_scheduler=True,
                process_state="stopped",
            )
        finally:
            db.close()

        http_port, grpc_port = allocate_cloud_slot_ports(2)
        self.assertNotEqual(http_port, 19115)
        self.assertNotEqual(grpc_port, 52165)

    def test_allocate_cloud_slot_reuses_stopped_free_slot(self) -> None:
        db = SessionLocal()
        try:
            ensure_runtime_slot(
                db,
                slot_id="cloud-slot-1",
                role="cloud",
                control_url="http://127.0.0.1:19115/load_strategy",
                grpc_target="127.0.0.1:52165",
                slot_index=1,
                spawned_by_scheduler=True,
                process_state="stopped",
            )
            task = ScheduleTask(
                task_id="task-reuse-stopped-slot",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-1",
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="ready",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=0,
                cloud_progress=0,
                edge_status="pending",
                cloud_status="pending",
                queue_status="running_loading",
                queue_position=0,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                edge_message="pending",
                cloud_message="pending",
                strategy_payload='{"layer_partitions": []}',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(task)
            db.commit()
            db.refresh(task)

            from app.services.schedule_orchestrator import allocate_cloud_slot_for_task
            from app.models.models import RuntimeSlot

            stopped_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(stopped_slot)
        finally:
            db.close()

        process_info = MagicMock(
            slot_id="cloud-slot-1",
            slot_index=1,
            http_port=19115,
            grpc_port=52165,
            control_url="http://127.0.0.1:19115/load_strategy",
            grpc_target="127.0.0.1:52165",
            process_pid=54321,
        )

        import asyncio
        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-reuse-stopped-slot").first()
            with (
                patch("app.services.schedule_orchestrator.start_decode_server_process_for_slot", return_value=process_info) as restart_mock,
                patch("app.services.schedule_orchestrator.wait_for_slot_health", new=AsyncMock(return_value=True)),
            ):
                slot, spawned = asyncio.run(allocate_cloud_slot_for_task(db, task, "127.0.0.1"))
            self.assertTrue(spawned)
            self.assertEqual(slot.slot_id, "cloud-slot-1")
            restart_mock.assert_called_once_with("cloud-slot-1", 1)
        finally:
            db.close()

    def test_phase2_dispatch_loading_spawns_second_cloud_slot(self) -> None:
        db = SessionLocal()
        try:
            busy_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
                slot_index=0,
            )
            update_runtime_slot_state(
                db,
                busy_slot,
                slot_state="bound",
                process_state="running",
                model_state="ready",
                owner_binding_id="binding-busy",
                owner_session_id="session-busy",
                model_type="Llama-3.2-3B-Instruct",
                task_id="task-busy",
            )
            waiting_task = ScheduleTask(
                task_id="task-phase2",
                openwebui_user_id="user-1",
                edge_session_id="session-2",
                runtime_binding_id="binding-2",
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="ready to dispatch",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=0,
                cloud_progress=0,
                edge_status="pending",
                cloud_status="pending",
                queue_status="running_loading",
                queue_position=0,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                edge_message="pending",
                cloud_message="pending",
                strategy_payload='{"layer_partitions": []}',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(waiting_task)
            db.commit()
        finally:
            db.close()

        process_info = MagicMock(
            slot_id="cloud-slot-1",
            slot_index=1,
            http_port=19114,
            grpc_port=52164,
            control_url="http://127.0.0.1:19114/load_strategy",
            grpc_target="127.0.0.1:52164",
            process_pid=43210,
        )

        import asyncio
        with (
            patch("app.services.schedule_orchestrator.start_decode_server_process", return_value=process_info),
            patch("app.services.schedule_orchestrator.wait_for_slot_health", new=AsyncMock(return_value=True)),
            patch("app.services.schedule_orchestrator.dispatch_strategy_to_runtime", new=AsyncMock(return_value={"status": "accepted"})),
        ):
            asyncio.run(dispatch_loading_task("task-phase2"))

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-phase2").first()
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(task)
            self.assertIsNotNone(slot)
            self.assertEqual(task.cloud_slot_id, "cloud-slot-1")
            self.assertEqual(task.allocated_cloud_slot_id, "cloud-slot-1")
            self.assertEqual(task.spawned_cloud_slot, "cloud-slot-1")
            self.assertEqual(slot.grpc_target, "127.0.0.1:52164")
            self.assertEqual(slot.process_pid, 43210)
            self.assertEqual(int(slot.spawned_by_scheduler), 1)
        finally:
            db.close()

    def test_phase2_dispatch_failure_rolls_back_spawned_cloud_slot(self) -> None:
        db = SessionLocal()
        try:
            busy_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
                slot_index=0,
            )
            update_runtime_slot_state(
                db,
                busy_slot,
                slot_state="bound",
                process_state="running",
                model_state="ready",
                owner_binding_id="binding-busy",
                owner_session_id="session-busy",
                model_type="Llama-3.2-3B-Instruct",
                task_id="task-busy",
            )
            waiting_task = ScheduleTask(
                task_id="task-fail-dispatch",
                openwebui_user_id="user-1",
                edge_session_id="session-2",
                runtime_binding_id="binding-2",
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="ready to dispatch",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=0,
                cloud_progress=0,
                edge_status="pending",
                cloud_status="pending",
                queue_status="running_loading",
                queue_position=0,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                edge_message="pending",
                cloud_message="pending",
                strategy_payload='{"layer_partitions": []}',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(waiting_task)
            db.commit()
        finally:
            db.close()

        process_info = MagicMock(
            slot_id="cloud-slot-1",
            slot_index=1,
            http_port=19114,
            grpc_port=52164,
            control_url="http://127.0.0.1:19114/load_strategy",
            grpc_target="127.0.0.1:52164",
            process_pid=43210,
        )

        import asyncio
        with (
            patch("app.services.schedule_orchestrator.start_decode_server_process", return_value=process_info),
            patch("app.services.schedule_orchestrator.wait_for_slot_health", new=AsyncMock(return_value=True)),
            patch("app.services.schedule_orchestrator.dispatch_strategy_to_runtime", new=AsyncMock(side_effect=[{"status": "accepted"}, RuntimeError("cloud dispatch failed")])),
            patch("app.services.decode_server_process_manager.stop_slot_process", return_value=True) as stop_mock,
        ):
            asyncio.run(dispatch_loading_task("task-fail-dispatch"))

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-fail-dispatch").first()
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.status, "failed")
            self.assertIsNotNone(slot)
            self.assertEqual(slot.slot_state, "free")
            self.assertEqual(slot.model_state, "empty")
            self.assertEqual(slot.process_state, "stopped")
            self.assertIsNone(slot.process_pid)
            stop_mock.assert_called_once_with("cloud-slot-1", process_pid=43210)
        finally:
            db.close()

    def test_runtime_confirmation_cloud_relays_to_edge(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            db.add(
                ScheduleTask(
                    task_id="task-confirm",
                    openwebui_user_id="user-1",
                    edge_session_id="session-1",
                    runtime_binding_id="binding-confirm",
                    model_type="Llama-3.2-3B-Instruct",
                    status="running",
                    phase="loading",
                    phase_progress=50,
                    overall_progress=75,
                    message="loading",
                    edge_device_id="edge_A",
                    cloud_device_id="cloud",
                    edge_progress=50,
                    cloud_progress=50,
                    edge_status="loading",
                    cloud_status="loading",
                    queue_status="running_loading",
                    queue_position=0,
                    edge_slot_id="edge-slot-edge_A",
                    cloud_slot_id="cloud-slot-1",
                    allocated_cloud_slot_id="cloud-slot-1",
                    edge_message="loading",
                    cloud_message="loading",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
            ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            ensure_runtime_slot(db, slot_id="cloud-slot-1", role="cloud", control_url="http://127.0.0.1:19114/load_strategy")
            db.commit()
        finally:
            db.close()

        with patch("app.api.v1.schedule.forward_cloud_confirmation_to_edge", new=AsyncMock(return_value=(True, None))):
            response = self.client.post(
                "/api/v1/schedule/runtime/confirmation/cloud",
                headers={
                    "Authorization": "Bearer wyy-local-aloepri-integrity",
                },
                json={
                    "task_id": "task-confirm",
                    "cloud_slot_id": "cloud-slot-1",
                    "model_type": "Llama-3.2-3B-Instruct",
                    "server_param_digest": "sha256:server",
                    "partition_digest": "sha256:partition",
                    "timestamp": 1234567890,
                    "nonce": "nonce-1",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["matched"])

        db = SessionLocal()
        try:
            cloud_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(cloud_slot)
            self.assertEqual(cloud_slot.confirmation_status, "passed")
        finally:
            db.close()

if __name__ == "__main__":
    unittest.main()
