import asyncio
import os
import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

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
from app.api.deps import get_current_openwebui_user_id, get_db
from app.models.models import Device, EdgeSession, RuntimeBinding, RuntimeSlot, ScheduleTask
from app.services.runtime_binding_service import create_runtime_binding
from app.services.runtime_slot_service import ensure_runtime_slot
from app.services.runtime_state_transition_service import (
    transition_runtime_binding,
    transition_runtime_slot,
)
from app.services.schedule_orchestrator import (
    _runtime_state_has_loaded_model,
    _runtime_state_is_loading,
    dispatch_loading_task,
    promote_waiting_loading_task,
)
from app.services.slot_reaper import cleanup_runtime_slots_for_session, mark_expired_sessions, release_bindings_for_session, stop_idle_spawned_cloud_slots
from app.services.decode_server_process_manager import allocate_cloud_slot_ports
from app.services.managed_cloud_slot_bootstrap_service import bootstrap_managed_cloud_slots
from app.services.runtime_slot_reconcile_service import reconcile_all_runtime_slots, reconcile_runtime_slot
from app.services.startup_recovery_service import recover_runtime_ownership_on_startup, reconcile_runtime_ownership
from app.services.schedule_recovery import recover_schedule_tasks_on_startup


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
        async def override_openwebui_user_id() -> str:
            return "user-1"

        async def override_db():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_current_openwebui_user_id] = (
            override_openwebui_user_id
        )
        self.app.dependency_overrides[get_db] = override_db

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def _request(self, method: str, path: str, **kwargs):
        async def send_request():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send_request())

    def _create_session(
        self,
        *,
        session_id: str = "session-1",
        status: str = "active",
        lease_expires_at: datetime | None = None,
    ) -> EdgeSession:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            session = EdgeSession(
                session_id=session_id,
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

    def test_stale_runtime_task_id_is_not_treated_as_loaded_or_loading(self) -> None:
        state = {
            "ready": False,
            "draining": False,
            "task_id": "task-old",
            "model_type": None,
            "active_request_count": 0,
        }

        self.assertFalse(_runtime_state_has_loaded_model(state))
        self.assertFalse(_runtime_state_is_loading(state))

    def test_runtime_model_or_draining_state_remains_protected(self) -> None:
        self.assertTrue(
            _runtime_state_has_loaded_model(
                {
                    "ready": False,
                    "draining": False,
                    "task_id": "task-current",
                    "model_type": "Llama-3.2-3B-Instruct",
                }
            )
        )
        self.assertTrue(
            _runtime_state_is_loading(
                {
                    "ready": False,
                    "draining": True,
                    "task_id": "task-current",
                    "model_type": None,
                }
            )
        )

    def test_session_heartbeat_refreshes_lease(self) -> None:
        self._create_session()
        response = self._request("POST", "/api/v1/session/heartbeat", json={"session_id": "session-1"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(payload["status"], "active")
        self.assertIn("lease_expires_at", payload)

    def test_session_close_marks_closed(self) -> None:
        self._create_session()
        response = self._request("POST", "/api/v1/session/close", json={"session_id": "session-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "closed")

    def test_runtime_progress_callbacks_require_integrity_token(self) -> None:
        import asyncio
        from fastapi import HTTPException
        from app.api.deps import verify_runtime_integrity_token

        for role in ("edge", "cloud"):
            path = f"/api/v1/schedule/runtime_callback/{role}"
            route = next(route for route in self.app.routes if getattr(route, "path", None) == path)
            self.assertIn(
                verify_runtime_integrity_token,
                [dependency.call for dependency in route.dependant.dependencies],
            )

        with self.assertRaises(HTTPException) as missing:
            asyncio.run(verify_runtime_integrity_token(None))
        with self.assertRaises(HTTPException) as invalid:
            asyncio.run(verify_runtime_integrity_token("Bearer wrong-token"))
        self.assertEqual(missing.exception.status_code, 401)
        self.assertEqual(invalid.exception.status_code, 401)
        self.assertIsNone(asyncio.run(verify_runtime_integrity_token(
            "Bearer wyy-local-aloepri-integrity"
        )))

    def test_session_close_atomically_cancels_active_task_and_binding(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-close-active",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-close-active",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="strategy",
                queue_status="running_strategy",
                edge_slot_id="edge-slot-edge_A",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            binding = RuntimeBinding(
                binding_id="binding-close-active",
                session_id="session-1",
                task_id=task.task_id,
                edge_slot_id="edge-slot-edge_A",
                status="pending",
            )
            db.add_all([task, binding])
            db.commit()
        finally:
            db.close()

        with patch("app.api.v1.session.cleanup_runtime_slots_for_session", new=AsyncMock(return_value=[])):
            response = self._request("POST", "/api/v1/session/close", json={"session_id": "session-1"})
        self.assertEqual(response.status_code, 200)

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-close-active").one()
            binding = db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == "binding-close-active").one()
            session = db.query(EdgeSession).filter(EdgeSession.session_id == "session-1").one()
            self.assertEqual(session.status, "closed")
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.queue_status, "done")
            self.assertIn("session_closed", task.error_detail)
            self.assertEqual(binding.status, "released")
        finally:
            db.close()

    def test_session_close_during_metrics_collection_prevents_runtime_dispatch(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-close-race",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-close-race",
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="strategy",
                queue_status="running_strategy",
                edge_slot_id="edge-slot-edge_A",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add_all([
                RuntimeBinding(
                    binding_id="binding-close-race",
                    session_id="session-1",
                    task_id=task.task_id,
                    edge_slot_id="edge-slot-edge_A",
                    status="pending",
                ),
                task,
            ])
            db.commit()
        finally:
            db.close()

        import asyncio
        from app.services.schedule_orchestrator import (
            close_session_schedule_state,
            process_schedule_task,
        )

        async def exercise(resolve_mock, dispatch_mock) -> None:
            metrics_entered = asyncio.Event()
            metrics_release = asyncio.Event()

            async def delayed_metrics(*_args, **_kwargs):
                metrics_entered.set()
                await metrics_release.wait()
                return {}

            with (
                patch(
                    "app.services.schedule_orchestrator.get_prometheus_metrics",
                    new=AsyncMock(side_effect=delayed_metrics),
                ),
                patch(
                    "app.services.schedule_orchestrator.get_network_metrics",
                    new=AsyncMock(return_value={}),
                ),
            ):
                running = asyncio.create_task(process_schedule_task(
                    "task-close-race",
                    "user-1",
                    "session-1",
                    {"model_type": "Llama-3.2-3B-Instruct"},
                ))
                await metrics_entered.wait()
                close_db = SessionLocal()
                try:
                    session = close_db.query(EdgeSession).filter(
                        EdgeSession.session_id == "session-1"
                    ).one()
                    close_session_schedule_state(close_db, session)
                finally:
                    close_db.close()
                metrics_release.set()
                await running

            resolve_mock.assert_not_awaited()
            dispatch_mock.assert_not_awaited()

        with (
            patch(
                "app.services.schedule_orchestrator.resolve_runtime_decision",
                new=AsyncMock(),
            ) as resolve_mock,
            patch(
                "app.services.schedule_orchestrator.dispatch_loading_task",
                new=AsyncMock(),
            ) as dispatch_mock,
        ):
            asyncio.run(exercise(resolve_mock, dispatch_mock))

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-close-race").one()
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.queue_status, "done")
        finally:
            db.close()

    def test_spawned_slot_cleanup_retains_ready_runtime_during_release_grace(self) -> None:
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
            transition_runtime_slot(
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
            self.assertEqual(slot.slot_state, "retained")
            self.assertEqual(slot.model_state, "ready")
            self.assertEqual(slot.process_state, "running")
            self.assertIsNotNone(slot.idle_deadline)
            self.assertIsNone(slot.process_idle_deadline)
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
            transition_runtime_slot(
                db,
                slot,
                slot_state="free",
                model_state="empty",
                process_idle_deadline=datetime.utcnow() - timedelta(seconds=1),
            )
            with patch("app.services.managed_cloud_slot_cleanup_service.stop_slot_process", return_value=True) as stop_mock:
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
            transition_runtime_slot(
                db,
                slot,
                slot_state="free",
                model_state="empty",
                process_idle_deadline=datetime.utcnow() - timedelta(seconds=1),
            )
            with patch("app.services.managed_cloud_slot_cleanup_service.stop_slot_process", return_value=False) as stop_mock:
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
            response = self._request("POST", "/api/v1/session/close", json={"session_id": "session-1"})
        self.assertEqual(response.status_code, 200)
        cleanup_mock.assert_awaited_once()

    def test_cleanup_ready_runtime_sets_release_grace_instead_of_unload(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-1",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-edge_A",
                role="edge",
                control_url="http://127.0.0.1:9001/load_strategy",
            )
            transition_runtime_slot(
                db,
                slot,
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id="task-1",
                model_type="Llama-3.2-3B-Instruct",
            )
            with patch("app.services.slot_reaper.fetch_runtime_state", new=AsyncMock(return_value={
                "ready": True,
                "draining": False,
                "active_request_count": 0,
                "model_type": "Llama-3.2-3B-Instruct",
                "task_id": "task-1",
            })), patch("app.services.slot_reaper.unload_runtime_slot", new=AsyncMock()) as unload_mock:
                import asyncio
                released = asyncio.run(cleanup_runtime_slots_for_session(db, "session-1"))
            self.assertEqual(released, ["edge-slot-edge_A"])
            self.assertEqual(unload_mock.await_count, 0)
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "edge-slot-edge_A").first()
            self.assertEqual(slot.model_state, "ready")
            self.assertEqual(slot.slot_state, "retained")
            self.assertIsNotNone(slot.idle_deadline)
            self.assertIsNone(slot.owner_session_id)
            self.assertIsNone(slot.owner_binding_id)
            self.assertIsNone(slot.task_id)
        finally:
            db.close()

    def test_session_cleanup_tolerates_concurrent_reconcile_release(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-1",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-edge_A",
                role="edge",
                control_url="http://127.0.0.1:9001/load_strategy",
            )
            transition_runtime_slot(
                db,
                slot,
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id="task-1",
                model_type="Llama-3.2-3B-Instruct",
            )

            async def release_during_runtime_probe(_slot):
                concurrent_db = SessionLocal()
                try:
                    current = (
                        concurrent_db.query(RuntimeSlot)
                        .filter(RuntimeSlot.slot_id == "edge-slot-edge_A")
                        .one()
                    )
                    transition_runtime_slot(
                        concurrent_db,
                        current,
                        slot_state="retained",
                        model_state="ready",
                        owner_session_id=None,
                        owner_binding_id=None,
                        task_id=None,
                    )
                finally:
                    concurrent_db.close()
                return {
                    "ready": True,
                    "draining": False,
                    "active_request_count": 0,
                    "model_type": "Llama-3.2-3B-Instruct",
                    "task_id": "task-1",
                }

            with patch(
                "app.services.slot_reaper.fetch_runtime_state",
                new=AsyncMock(side_effect=release_during_runtime_probe),
            ):
                import asyncio

                released = asyncio.run(
                    cleanup_runtime_slots_for_session(db, "session-1")
                )
            self.assertEqual(released, [])
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = (
                db.query(RuntimeSlot)
                .filter(RuntimeSlot.slot_id == "edge-slot-edge_A")
                .one()
            )
            self.assertEqual(slot.slot_state, "retained")
            self.assertEqual(slot.model_state, "ready")
            self.assertIsNone(slot.owner_session_id)
            self.assertIsNone(slot.owner_binding_id)
        finally:
            db.close()

    def test_reconcile_released_ready_slot_respects_release_grace(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-1",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            transition_runtime_binding(db, binding, status="released")
            slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-edge_A",
                role="edge",
                control_url="http://127.0.0.1:9001/load_strategy",
            )
            transition_runtime_slot(
                db,
                slot,
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id="task-1",
                model_type="Llama-3.2-3B-Instruct",
            )
            ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
            )
            with patch("app.services.runtime_slot_reconcile_service.fetch_runtime_state", new=AsyncMock(return_value={
                "ready": True,
                "draining": False,
                "active_request_count": 0,
                "model_type": "Llama-3.2-3B-Instruct",
                "task_id": "task-1",
                "runtime_route": {
                    "cloud_slot_id": "cloud-slot-0",
                    "cloud_control_url": "http://127.0.0.1:19113/load_strategy",
                    "cloud_decode_grpc_target": "127.0.0.1:52163",
                },
            })), patch("app.services.runtime_slot_reconcile_service.unload_runtime_slot", new=AsyncMock()) as unload_mock:
                import asyncio
                asyncio.run(reconcile_runtime_slot(db, slot))
            self.assertEqual(unload_mock.await_count, 0)
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "edge-slot-edge_A").first()
            self.assertEqual(slot.model_state, "ready")
            self.assertEqual(slot.slot_state, "retained")
            self.assertIsNotNone(slot.idle_deadline)
            self.assertIsNone(slot.owner_session_id)
            self.assertIsNone(slot.owner_binding_id)
            self.assertIsNone(slot.task_id)
        finally:
            db.close()

    def test_reconcile_released_edge_ready_unloads_when_cloud_peer_is_empty(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            edge_slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-edge_A",
                role="edge",
                control_url="http://127.0.0.1:9001/load_strategy",
                process_state="running",
            )
            transition_runtime_slot(
                db,
                edge_slot,
                slot_state="retained",
                model_state="ready",
                owner_session_id=None,
                owner_binding_id=None,
                task_id=None,
                model_type="Llama-3.2-3B-Instruct",
                idle_deadline=datetime.utcnow() + timedelta(seconds=120),
            )
            ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19114/load_strategy",
                grpc_target="127.0.0.1:52164",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="running",
            )

            async def fake_fetch_runtime_state(target_slot):
                if target_slot.slot_id == "edge-slot-edge_A":
                    return {
                        "ready": True,
                        "draining": False,
                        "active_request_count": 0,
                        "model_type": "Llama-3.2-3B-Instruct",
                        "task_id": "task-stale-edge-route",
                        "runtime_route": {
                            "cloud_slot_id": "cloud-slot-0",
                            "cloud_control_url": "http://127.0.0.1:19114/load_strategy",
                            "cloud_decode_grpc_target": "127.0.0.1:52164",
                        },
                    }
                if target_slot.slot_id == "cloud-slot-0":
                    return {
                        "ready": False,
                        "draining": False,
                        "active_request_count": 0,
                        "model_type": None,
                        "task_id": None,
                    }
                raise AssertionError(f"unexpected slot {target_slot.slot_id}")

            async def fake_unload_runtime_slot(
                db,
                slot,
                *,
                reason,
                timeout=10.0,
                preserve_reservation=False,
            ):
                del reason, timeout, preserve_reservation
                transition_runtime_slot(
                    db,
                    slot,
                    slot_state="free",
                    model_state="empty",
                    owner_session_id=None,
                    owner_binding_id=None,
                    task_id=None,
                    model_type=None,
                    active_request_count=0,
                )
                return {"unloaded": True}

            with patch(
                "app.services.runtime_slot_reconcile_service.fetch_runtime_state",
                new=AsyncMock(side_effect=fake_fetch_runtime_state),
            ), patch(
                "app.services.runtime_slot_reconcile_service.unload_runtime_slot",
                new=AsyncMock(side_effect=fake_unload_runtime_slot),
            ) as unload_mock:
                import asyncio

                asyncio.run(reconcile_runtime_slot(db, edge_slot))
            self.assertEqual(unload_mock.await_count, 1)
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = (
                db.query(RuntimeSlot)
                .filter(RuntimeSlot.slot_id == "edge-slot-edge_A")
                .first()
            )
            self.assertEqual(slot.slot_state, "free")
            self.assertEqual(slot.model_state, "empty")
            self.assertIsNone(slot.model_type)
            self.assertIsNone(slot.task_id)
        finally:
            db.close()

    def test_edge_runtime_without_route_is_not_a_healthy_warm_instance(self) -> None:
        import asyncio
        from app.services.runtime_slot_reconcile_service import (
            _edge_runtime_route_has_ready_cloud_peer,
        )

        db = SessionLocal()
        try:
            edge_slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-edge_A",
                role="edge",
                control_url="http://127.0.0.1:9001/load_strategy",
            )
            healthy = asyncio.run(_edge_runtime_route_has_ready_cloud_peer(
                db,
                edge_slot,
                {
                    "ready": True,
                    "model_type": "Llama-3.2-3B-Instruct",
                    "task_id": "task-without-route",
                },
                runtime_model_type="Llama-3.2-3B-Instruct",
            ))
            self.assertFalse(healthy)
        finally:
            db.close()

    def test_dispatch_reclaims_retained_cloud_slot_before_new_load(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-new",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            retained_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="running",
                process_pid=12345,
            )
            transition_runtime_slot(
                db,
                retained_slot,
                slot_state="retained",
                model_state="ready",
                task_id="task-old",
                model_type="Llama-3.2-3B-Instruct",
                idle_deadline=datetime.utcnow() + timedelta(hours=2),
            )
            task = ScheduleTask(
                task_id="task-new",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id=binding.binding_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                phase_progress=0,
                overall_progress=70,
                message="ready to dispatch",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_status="pending",
                cloud_status="pending",
                queue_status="running_loading",
                queue_position=0,
                edge_message="pending",
                cloud_message="pending",
                strategy_payload='{"layer_partitions": []}',
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(task)
            db.commit()
        finally:
            db.close()

        async def fake_fetch_runtime_state(_slot):
            return {
                "ready": True,
                "draining": False,
                "task_id": "task-old",
                "model_type": "Llama-3.2-3B-Instruct",
                "active_request_count": 0,
            }

        async def fake_unload_runtime_slot(db, slot, *, reason, timeout=10.0, preserve_reservation=False):
            del reason, timeout
            return transition_runtime_slot(
                db,
                slot,
                slot_state="bound" if preserve_reservation else "free",
                model_state="loading" if preserve_reservation else "empty",
                owner_session_id=slot.owner_session_id if preserve_reservation else None,
                owner_binding_id=slot.owner_binding_id if preserve_reservation else None,
                model_type=slot.model_type if preserve_reservation else None,
                task_id=slot.task_id if preserve_reservation else None,
                active_request_count=0,
                confirmation_status="none",
                idle_deadline=None,
                process_idle_deadline=None,
                last_used_at=datetime.utcnow(),
            )

        import asyncio
        with (
            patch("app.services.schedule_orchestrator.fetch_runtime_state", new=AsyncMock(side_effect=fake_fetch_runtime_state)),
            patch("app.services.schedule_orchestrator.unload_runtime_slot", new=AsyncMock(side_effect=fake_unload_runtime_slot)) as unload_mock,
            patch("app.services.schedule_orchestrator.start_decode_server_process_locked", new=AsyncMock()) as start_mock,
            patch("app.services.schedule_orchestrator.settings.CLOUD_SLOT_MAX_COUNT", 1),
            patch("app.services.schedule_orchestrator.dispatch_strategy_to_runtime", new=AsyncMock(return_value={"status": "accepted"})),
        ):
            asyncio.run(dispatch_loading_task("task-new"))

        self.assertEqual(unload_mock.await_count, 1)
        self.assertEqual(start_mock.await_count, 0)
        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-0").first()
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-new").first()
            self.assertEqual(slot.slot_state, "bound")
            self.assertEqual(slot.model_state, "loading")
            self.assertEqual(slot.owner_binding_id, binding.binding_id)
            self.assertEqual(task.status, "running")
        finally:
            db.close()

    def test_runtime_slot_service_creates_and_updates_slot(self) -> None:
        db = SessionLocal()
        try:
            slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113")
            self.assertEqual(slot.slot_state, "free")
            slot = transition_runtime_slot(
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
            binding = transition_runtime_binding(db, binding, status="released")
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
            transition_runtime_slot(
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
            transition_runtime_slot(
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
            self.assertIn("FIFO", task.message)
        finally:
            db.close()

    def test_second_task_allocates_new_cloud_slot_when_cloud_slot_is_busy(self) -> None:
        self._create_session(session_id="session-2")
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
            transition_runtime_slot(
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
            db.add(RuntimeBinding(
                binding_id="binding-2",
                session_id="session-2",
                task_id=waiting_task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id=None,
                status="pending",
            ))
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
            patch("app.services.schedule_orchestrator.start_decode_server_process_locked", new=AsyncMock(return_value=process_info)),
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


    def test_startup_recovery_releases_stale_loading_task_and_slot(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-stale-loading",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-stale",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                queue_status="running_loading",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_status="loading",
                cloud_status="loading",
            )
            db.add(task)
            db.commit()
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-stale-loading",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            task.runtime_binding_id = binding.binding_id
            db.add(task)
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113/load_strategy", spawned_by_scheduler=True, process_state="running")
            transition_runtime_slot(db, edge_slot, slot_state="bound", model_state="loading", owner_session_id="session-1", owner_binding_id=binding.binding_id, task_id="task-stale-loading")
            transition_runtime_slot(db, cloud_slot, slot_state="bound", model_state="loading", owner_session_id="session-1", owner_binding_id=binding.binding_id, task_id="task-stale-loading")
            recover_runtime_ownership_on_startup(db)
            db.refresh(task)
            db.refresh(binding)
            db.refresh(edge_slot)
            db.refresh(cloud_slot)
            self.assertEqual(task.status, "failed")
            self.assertEqual(binding.status, "released")
            self.assertIsNone(edge_slot.owner_binding_id)
            self.assertIsNone(cloud_slot.owner_binding_id)
        finally:
            db.close()

    def test_startup_recovery_fails_untrustworthy_completed_task(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-bad-completed",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-completed",
                model_type="Llama-3.2-3B-Instruct",
                status="completed",
                phase="completed",
                queue_status="done",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_status="ready",
                cloud_status="ready",
            )
            db.add(task)
            db.commit()
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-bad-completed",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            task.runtime_binding_id = binding.binding_id
            db.add(task)
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113/load_strategy", spawned_by_scheduler=True, process_state="running")
            transition_runtime_slot(db, edge_slot, slot_state="bound", model_state="ready", owner_session_id="session-1", owner_binding_id=binding.binding_id, task_id="task-bad-completed")
            transition_runtime_slot(db, cloud_slot, slot_state="bound", model_state="ready", confirmation_status="failed", owner_session_id="session-1", owner_binding_id=binding.binding_id, task_id="task-bad-completed")
            recover_runtime_ownership_on_startup(db)
            db.refresh(task)
            db.refresh(binding)
            self.assertEqual(task.status, "failed")
            self.assertEqual(binding.status, "released")
        finally:
            db.close()

    def test_reconcile_runtime_ownership_clears_released_binding_owner(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-released-binding",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            transition_runtime_binding(db, binding, status="released")
            slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113/load_strategy", spawned_by_scheduler=True, process_state="running")
            transition_runtime_slot(db, slot, slot_state="bound", model_state="ready", owner_session_id="session-1", owner_binding_id=binding.binding_id, task_id="task-released-binding")
            reconcile_runtime_ownership(db)
            db.refresh(slot)
            self.assertEqual(slot.slot_state, "retained")
            self.assertEqual(slot.model_state, "ready")
            self.assertIsNotNone(slot.idle_deadline)
            self.assertIsNone(slot.owner_session_id)
            self.assertIsNone(slot.owner_binding_id)
            self.assertIsNone(slot.task_id)
        finally:
            db.close()

    def test_reconcile_runtime_ownership_clears_missing_binding_owner(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113/load_strategy", spawned_by_scheduler=True, process_state="running")
            transition_runtime_slot(db, slot, slot_state="bound", model_state="ready", owner_session_id="session-1", owner_binding_id="binding-missing", task_id="task-missing-binding")
            reconcile_runtime_ownership(db)
            db.refresh(slot)
            self.assertEqual(slot.slot_state, "retained")
            self.assertEqual(slot.model_state, "ready")
            self.assertIsNotNone(slot.idle_deadline)
            self.assertIsNone(slot.owner_session_id)
            self.assertIsNone(slot.owner_binding_id)
            self.assertIsNone(slot.task_id)
        finally:
            db.close()

    def test_reconcile_runtime_ownership_clears_missing_session_owner(self) -> None:
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-ghost",
                task_id="task-missing-session",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113/load_strategy", spawned_by_scheduler=True, process_state="running")
            transition_runtime_slot(db, slot, slot_state="bound", model_state="ready", owner_session_id="session-ghost", owner_binding_id=binding.binding_id, task_id="task-missing-session")
            reconcile_runtime_ownership(db)
            db.refresh(slot)
            db.refresh(binding)
            self.assertEqual(binding.status, "released")
            self.assertEqual(slot.slot_state, "retained")
            self.assertEqual(slot.model_state, "ready")
            self.assertIsNotNone(slot.idle_deadline)
            self.assertIsNone(slot.owner_session_id)
            self.assertIsNone(slot.owner_binding_id)
            self.assertIsNone(slot.task_id)
        finally:
            db.close()

    def test_startup_recovery_does_not_fake_stop_managed_cloud_slot_when_process_stop_fails(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-recovery-stop-fails",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            transition_runtime_binding(db, binding, status="released")
            slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:9010/load_strategy",
                grpc_target="127.0.0.1:51100",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="running",
                process_pid=98765,
                base_env_name=".env.prod",
            )
            transition_runtime_slot(
                db,
                slot,
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id="task-recovery-stop-fails",
                idle_deadline=datetime.utcnow() - timedelta(seconds=1),
            )
            with patch("app.services.managed_cloud_slot_cleanup_service.stop_slot_process", return_value=False) as stop_mock:
                recover_runtime_ownership_on_startup(db)
            db.refresh(slot)
            stop_mock.assert_called_once_with("cloud-slot-0", process_pid=98765)
            self.assertEqual(slot.process_state, "failed")
            self.assertEqual(slot.slot_state, "needs_reconcile")
            self.assertEqual(slot.model_state, "failed")
            self.assertEqual(slot.process_pid, 98765)
        finally:
            db.close()

    def test_startup_recovery_stops_managed_cloud_slot_before_marking_stopped(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-recovery-stop-ok",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            transition_runtime_binding(db, binding, status="released")
            slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:9010/load_strategy",
                grpc_target="127.0.0.1:51100",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="running",
                process_pid=98766,
                base_env_name=".env.prod",
            )
            transition_runtime_slot(
                db,
                slot,
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id="task-recovery-stop-ok",
                idle_deadline=datetime.utcnow() - timedelta(seconds=1),
            )
            with patch("app.services.managed_cloud_slot_cleanup_service.stop_slot_process", return_value=True) as stop_mock:
                recover_runtime_ownership_on_startup(db)
            db.refresh(slot)
            stop_mock.assert_called_once_with("cloud-slot-0", process_pid=98766)
            self.assertEqual(slot.process_state, "stopped")
            self.assertEqual(slot.slot_state, "free")
            self.assertEqual(slot.model_state, "empty")
            self.assertIsNone(slot.process_pid)
            self.assertIsNone(slot.owner_binding_id)
        finally:
            db.close()

    def test_startup_recovery_preserves_trustworthy_completed_binding(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-good-completed",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-good",
                model_type="Llama-3.2-3B-Instruct",
                status="completed",
                phase="completed",
                queue_status="done",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_status="ready",
                cloud_status="ready",
            )
            db.add(task)
            db.commit()
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-good-completed",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            task.runtime_binding_id = binding.binding_id
            db.add(task)
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113/load_strategy", spawned_by_scheduler=True, process_state="running")
            transition_runtime_slot(db, edge_slot, slot_state="bound", model_state="ready", owner_session_id="session-1", owner_binding_id=binding.binding_id, task_id="task-good-completed")
            transition_runtime_slot(db, cloud_slot, slot_state="bound", model_state="ready", confirmation_status="passed", owner_session_id="session-1", owner_binding_id=binding.binding_id, task_id="task-good-completed")
            recover_runtime_ownership_on_startup(db)
            db.refresh(task)
            db.refresh(binding)
            self.assertEqual(task.status, "completed")
            self.assertEqual(binding.status, "binding")
        finally:
            db.close()

    def test_startup_recovery_uses_new_session_for_async_cleanup(self) -> None:
        expired_at = datetime.utcnow() - timedelta(minutes=1)
        self._create_session(status="expired", lease_expires_at=expired_at)
        db = SessionLocal()
        try:
            create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-expired-cleanup",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
        finally:
            db.close()

        scheduled = []

        class _FakeLoop:
            def create_task(self, coro):
                scheduled.append(coro)
                return coro

        with patch('app.services.schedule_recovery.asyncio.get_running_loop', return_value=_FakeLoop()):
            recover_schedule_tasks_on_startup()

        self.assertEqual(len(scheduled), 1)
        import asyncio
        asyncio.run(scheduled[0])

    def test_bootstrap_managed_cloud_slot_zero_degrades_when_health_fails(self) -> None:
        db = SessionLocal()
        try:
            process_info = MagicMock(
                slot_id='cloud-slot-0',
                slot_index=0,
                http_port=19114,
                grpc_port=52164,
                control_url='http://127.0.0.1:19114/load_strategy',
                grpc_target='127.0.0.1:52164',
                process_pid=32109,
            )
            with (
                patch('app.services.managed_cloud_slot_bootstrap_service.start_decode_server_process_for_slot_locked', new=AsyncMock(return_value=process_info)),
                patch('app.services.managed_cloud_slot_bootstrap_service.wait_for_slot_health', new=AsyncMock(return_value=False)),
                patch('app.services.decode_server_process_manager.stop_slot_process', return_value=True),
            ):
                import asyncio
                asyncio.run(bootstrap_managed_cloud_slots(db))
        finally:
            db.close()

        db = SessionLocal()
        try:
            slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-0').first()
            self.assertIsNotNone(slot)
            self.assertEqual(slot.process_state, 'stopped')
            self.assertEqual(slot.slot_state, 'free')
            self.assertEqual(slot.model_state, 'empty')
            self.assertIsNone(slot.process_pid)
            self.assertEqual(slot.startup_failure_count, 1)
            self.assertIsNotNone(slot.last_error)
        finally:
            db.close()

    def test_runtime_progress_updates_three_stage_fields(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-progress-stages",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="loading",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_status="loading",
                cloud_status="loading",
                queue_status="running_loading",
                queue_position=0,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            binding = RuntimeBinding(
                binding_id="binding-progress-stages",
                session_id="session-1",
                task_id=task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                status="binding",
            )
            task.runtime_binding_id = binding.binding_id
            edge_slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-edge_A",
                role="edge",
                control_url="http://127.0.0.1:19112/load_strategy",
            )
            cloud_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
            )
            transition_runtime_slot(
                db,
                edge_slot,
                slot_state="bound",
                model_state="loading",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id=task.task_id,
                model_type=task.model_type,
            )
            transition_runtime_slot(
                db,
                cloud_slot,
                slot_state="bound",
                model_state="loading",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id=task.task_id,
                model_type=task.model_type,
            )
            db.add_all([binding, task])
            db.commit()
        finally:
            db.close()

        import asyncio
        from types import SimpleNamespace
        from app.services.schedule_orchestrator import handle_runtime_progress

        asyncio.run(handle_runtime_progress(SimpleNamespace(
            task_id="task-progress-stages",
            status="loading",
            progress=60,
            message="runtime load progressing",
            stage="runtime_load",
            node_role="edge",
        )))
        asyncio.run(handle_runtime_progress(SimpleNamespace(
            task_id="task-progress-stages",
            status="loading",
            progress=50,
            message="integrity progressing",
            stage="integrity",
            node_role="cloud",
        )))

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-progress-stages").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.edge_runtime_load_progress, 60)
            self.assertEqual(task.cloud_integrity_progress, 50)
            self.assertEqual(task.edge_progress, 18)
            self.assertEqual(task.cloud_progress, 15)
        finally:
            db.close()

    def test_schedule_status_response_includes_stage_progress_fields(self) -> None:
        db = SessionLocal()
        try:
            task = ScheduleTask(
                task_id="task-stage-response",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                phase_progress=50,
                overall_progress=75,
                message="stage fields",
                edge_device_id="edge_A",
                cloud_device_id="cloud",
                edge_progress=88,
                cloud_progress=77,
                edge_strategy_progress=100,
                edge_integrity_progress=80,
                edge_runtime_load_progress=60,
                cloud_strategy_progress=100,
                cloud_integrity_progress=70,
                cloud_runtime_load_progress=40,
                edge_status="loading",
                cloud_status="loading",
                edge_message="edge",
                cloud_message="cloud",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(task)
            db.commit()
        finally:
            db.close()

        response = self._request(
            "GET",
            "/api/v1/schedule/tasks/task-stage-response",
            headers={"Authorization": "Bearer dev-token"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["edge_strategy_progress"], 100)
        self.assertEqual(payload["edge_integrity_progress"], 80)
        self.assertEqual(payload["edge_runtime_load_progress"], 60)
        self.assertEqual(payload["cloud_strategy_progress"], 100)
        self.assertEqual(payload["cloud_integrity_progress"], 70)
        self.assertEqual(payload["cloud_runtime_load_progress"], 40)

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

        slots_response = self._request(
            "GET",
            "/api/v1/schedule/runtime/slots",
            headers={"Authorization": "Bearer dev-token"},
        )
        bindings_response = self._request(
            "GET",
            "/api/v1/schedule/runtime/bindings",
            headers={"Authorization": "Bearer dev-token"},
        )
        queue_response = self._request(
            "GET",
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

    def test_same_session_rejects_same_model_second_active_task(self) -> None:
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

        response = self._request(
            "POST",
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
        self.assertIn("相同模型", payload["message"])

    def test_same_session_model_switch_supersedes_active_task(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            old_binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-active",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            task = ScheduleTask(
                task_id="task-active",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id=old_binding.binding_id,
                model_type="Llama-3.2-3B",
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

        with patch("app.services.schedule_orchestrator.process_schedule_task", new=AsyncMock()) as process_mock:
            response = self._request(
                "POST",
                "/api/v1/schedule/trigger",
                headers={
                    "Authorization": "Bearer dev-token",
                    "Session-Id": "session-1",
                },
                json={"model_type": "Llama-3.2-3B-Instruct"},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertNotEqual(payload["task_id"], "task-active")
        process_mock.assert_called_once()

        db = SessionLocal()
        try:
            old_task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-active").first()
            new_task = db.query(ScheduleTask).filter(ScheduleTask.task_id == payload["task_id"]).first()
            session = db.query(EdgeSession).filter(EdgeSession.session_id == "session-1").first()
            old_binding = db.query(RuntimeBinding).filter(RuntimeBinding.task_id == "task-active").first()
            self.assertEqual(old_task.status, "failed")
            self.assertIn("取代", old_task.message)
            self.assertEqual(new_task.model_type, "Llama-3.2-3B-Instruct")
            self.assertEqual(session.model_type, "Llama-3.2-3B-Instruct")
            self.assertEqual(old_binding.status, "binding")
        finally:
            db.close()


    def test_decode_process_manager_derives_env_from_backend_env_file(self) -> None:
        from app.services.decode_server_process_manager import start_decode_server_process_for_slot

        process = MagicMock(pid=24680)
        with (
            patch.dict(os.environ, {"BACKEND_ENV_FILE": "/tmp/backend/.env.prod"}, clear=False),
            patch("app.services.decode_server_process_manager.allocate_cloud_slot_ports", return_value=(9011, 51101)),
            patch("app.services.decode_server_process_manager.subprocess.Popen", return_value=process) as popen_mock,
        ):
            info = start_decode_server_process_for_slot("cloud-slot-1", 1)

        self.assertEqual(info.http_port, 9011)
        self.assertEqual(info.grpc_port, 51101)
        self.assertEqual(info.control_url, "http://127.0.0.1:9011/load_strategy")
        self.assertEqual(info.grpc_target, "127.0.0.1:51101")

        env = popen_mock.call_args.kwargs["env"]
        self.assertEqual(env["APP_ENV"], "prod")
        self.assertEqual(env["ENV_FILE"], ".env.prod")
        self.assertEqual(env["BACKEND_ENV_FILE"], "/tmp/backend/.env.prod")
        from app.core.config import settings
        self.assertEqual(env["SCHEDULE_BACKEND_URL"], settings.BACKEND_BASE_URL)
        self.assertEqual(env["CLOUD_RUNTIME_PORT"], "9011")
        self.assertEqual(env["RUNTIME_PORT"], "9011")
        self.assertEqual(env["DECODE_GRPC_BIND"], "0.0.0.0:51101")
        self.assertEqual(env["DECODE_GRPC_TARGET"], "127.0.0.1:51101")

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

        with patch(
            "app.services.decode_server_process_manager._port_in_use",
            return_value=False,
        ):
            http_port, grpc_port = allocate_cloud_slot_ports(2)
        self.assertNotEqual(http_port, 19115)
        self.assertNotEqual(grpc_port, 52165)

    def test_allocate_cloud_slot_reuses_stopped_free_slot(self) -> None:
        self._create_session()
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
                process_pid=98765,
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
            db.add(RuntimeBinding(
                binding_id="binding-1",
                session_id="session-1",
                task_id=task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id=None,
                status="pending",
            ))
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
                patch("app.services.decode_server_process_manager.stop_slot_process", return_value=True) as stop_mock,
                patch("app.services.schedule_orchestrator.start_decode_server_process_for_slot_locked", new=AsyncMock(return_value=process_info)) as restart_mock,
                patch("app.services.schedule_orchestrator.wait_for_slot_health", new=AsyncMock(return_value=True)),
            ):
                slot, spawned = asyncio.run(allocate_cloud_slot_for_task(db, task, "127.0.0.1"))
            self.assertTrue(spawned)
            self.assertEqual(slot.slot_id, "cloud-slot-1")
            stop_mock.assert_called_once_with("cloud-slot-1", process_pid=98765)
            restart_mock.assert_called_once_with("cloud-slot-1", 1)
        finally:
            db.close()

    def test_phase2_dispatch_loading_spawns_second_cloud_slot(self) -> None:
        self._create_session(session_id="session-2")
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
            transition_runtime_slot(
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
            db.add(RuntimeBinding(
                binding_id="binding-2",
                session_id="session-2",
                task_id=waiting_task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id=None,
                status="pending",
            ))
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
            patch("app.services.schedule_orchestrator.start_decode_server_process_locked", new=AsyncMock(return_value=process_info)),
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

    def test_same_session_reload_reuses_existing_cloud_slot(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            old_binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-old",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="running",
                process_pid=12345,
                base_env_name=".env.wyy",
            )
            transition_runtime_slot(
                db,
                edge_slot,
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=old_binding.binding_id,
                task_id="task-old",
                model_type="Llama-3.2-3B-Instruct",
                process_state="running",
            )
            transition_runtime_slot(
                db,
                cloud_slot,
                slot_state="bound",
                model_state="ready",
                owner_session_id="session-1",
                owner_binding_id=old_binding.binding_id,
                task_id="task-old",
                model_type="Llama-3.2-3B-Instruct",
                process_state="running",
                confirmation_status="passed",
                process_idle_deadline=datetime.utcnow() + timedelta(minutes=5),
            )
            new_binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-reload",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            task = ScheduleTask(
                task_id="task-reload",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id=new_binding.binding_id,
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="reload",
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
        finally:
            db.close()

        async def fake_fetch_runtime_state(slot):
            return {
                "ready": True,
                "draining": False,
                "task_id": slot.task_id or "task-old",
                "model_type": "Llama-3.2-3B-Instruct",
                "active_request_count": 0,
            }

        async def fake_unload_runtime_slot(db, slot, *, reason, timeout=10.0, preserve_reservation=False):
            del reason, timeout, preserve_reservation
            transition_runtime_slot(
                db,
                slot,
                slot_state="free",
                model_state="empty",
                owner_session_id=None,
                owner_binding_id=None,
                model_type=None,
                task_id=None,
                active_request_count=0,
                confirmation_status="none",
                idle_deadline=None,
                process_idle_deadline=None,
                last_used_at=datetime.utcnow(),
            )
            return {"unloaded": True}

        import asyncio
        with (
            patch("app.services.schedule_orchestrator.fetch_runtime_state", new=AsyncMock(side_effect=fake_fetch_runtime_state)),
            patch("app.services.schedule_orchestrator.unload_runtime_slot", new=AsyncMock(side_effect=fake_unload_runtime_slot)),
            patch("app.services.schedule_orchestrator.dispatch_strategy_to_runtime", new=AsyncMock(return_value={"status": "accepted"})),
            patch("app.services.schedule_orchestrator.start_decode_server_process_locked", new=AsyncMock()) as start_mock,
        ):
            asyncio.run(dispatch_loading_task("task-reload"))

        start_mock.assert_not_called()
        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-reload").first()
            old_binding = db.query(RuntimeBinding).filter(RuntimeBinding.task_id == "task-old").first()
            new_binding = db.query(RuntimeBinding).filter(RuntimeBinding.task_id == "task-reload").first()
            cloud_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-0").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.cloud_slot_id, "cloud-slot-0")
            self.assertEqual(task.allocated_cloud_slot_id, "cloud-slot-0")
            self.assertEqual(task.spawned_cloud_slot, None)
            self.assertEqual(task.status, "running")
            self.assertIsNotNone(old_binding)
            self.assertEqual(old_binding.status, "released")
            self.assertIsNotNone(new_binding)
            self.assertEqual(new_binding.status, "binding")
            self.assertIsNotNone(cloud_slot)
            self.assertEqual(cloud_slot.owner_binding_id, new_binding.binding_id)
            self.assertIsNone(cloud_slot.process_idle_deadline)
        finally:
            db.close()

    def test_dispatch_loading_waits_when_runtime_is_still_loading_previous_task(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-wait-dispatch",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="running",
            )
            transition_runtime_slot(db, edge_slot, slot_state="free", model_state="empty", process_state="running")
            transition_runtime_slot(db, cloud_slot, slot_state="free", model_state="empty", process_state="running")
            task = ScheduleTask(
                task_id="task-wait-dispatch",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id=binding.binding_id,
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="dispatch",
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
        finally:
            db.close()

        import asyncio
        import httpx
        request = httpx.Request("POST", "http://runtime/load_strategy")
        response = httpx.Response(409, request=request, json={"detail": "runtime loading already in progress"})
        conflict = httpx.HTTPStatusError("conflict", request=request, response=response)

        with patch("app.services.schedule_orchestrator.dispatch_strategy_to_runtime", new=AsyncMock(side_effect=[conflict, conflict])):
            asyncio.run(dispatch_loading_task("task-wait-dispatch"))

        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-wait-dispatch").first()
            edge_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "edge-slot-edge_A").first()
            cloud_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-0").first()
            self.assertEqual(task.status, "accepted")
            self.assertEqual(task.queue_status, "waiting_cloud_slot")
            self.assertIn("等待", task.message)
            self.assertEqual(edge_slot.owner_binding_id, task.runtime_binding_id)
            self.assertEqual(edge_slot.task_id, task.task_id)
            self.assertEqual(cloud_slot.owner_binding_id, task.runtime_binding_id)
            self.assertEqual(cloud_slot.task_id, task.task_id)
        finally:
            db.close()

    def test_same_session_reload_rejects_when_runtime_is_busy(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            old_binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-old",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
                grpc_target="127.0.0.1:52163",
                slot_index=0,
                spawned_by_scheduler=True,
                process_state="running",
            )
            transition_runtime_slot(db, edge_slot, slot_state="bound", model_state="ready", owner_session_id="session-1", owner_binding_id=old_binding.binding_id, task_id="task-old", model_type="Llama-3.2-3B-Instruct", process_state="running")
            transition_runtime_slot(db, cloud_slot, slot_state="bound", model_state="ready", owner_session_id="session-1", owner_binding_id=old_binding.binding_id, task_id="task-old", model_type="Llama-3.2-3B-Instruct", process_state="running", confirmation_status="passed")
            new_binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-reload-busy",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            task = ScheduleTask(
                task_id="task-reload-busy",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id=new_binding.binding_id,
                model_type="Llama-3.2-3B-Instruct",
                status="accepted",
                phase="loading",
                phase_progress=0,
                overall_progress=0,
                message="reload",
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
        finally:
            db.close()

        async def busy_runtime_state(_slot):
            return {
                "ready": True,
                "draining": False,
                "task_id": "task-old",
                "model_type": "Llama-3.2-3B-Instruct",
                "active_request_count": 1,
            }

        import asyncio
        with (
            patch("app.services.schedule_orchestrator.fetch_runtime_state", new=AsyncMock(side_effect=busy_runtime_state)),
            patch("app.services.schedule_orchestrator.unload_runtime_slot", new=AsyncMock()) as unload_mock,
            patch("app.services.schedule_orchestrator.start_decode_server_process_locked", new=AsyncMock()) as start_mock,
        ):
            asyncio.run(dispatch_loading_task("task-reload-busy"))

        unload_mock.assert_not_called()
        start_mock.assert_not_called()
        db = SessionLocal()
        try:
            task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-reload-busy").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.status, "failed")
            self.assertIn("当前会话原有 slot 重加载失败", task.message)
        finally:
            db.close()

    def test_reconcile_runtime_ownership_releases_duplicate_session_bindings(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            db.add_all([
                ScheduleTask(
                    task_id="task-keep",
                    openwebui_user_id="user-1",
                    edge_session_id="session-1",
                    runtime_binding_id="binding-placeholder",
                    model_type="Llama-3.2-3B-Instruct",
                    status="completed",
                    phase="completed",
                    queue_status="done",
                    edge_slot_id="edge-slot-edge_A",
                    cloud_slot_id="cloud-slot-0",
                    edge_device_id="edge_A",
                    cloud_device_id="cloud",
                    edge_status="ready",
                    cloud_status="ready",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                ),
                ScheduleTask(
                    task_id="task-dup",
                    openwebui_user_id="user-1",
                    edge_session_id="session-1",
                    runtime_binding_id="binding-placeholder-2",
                    model_type="Llama-3.2-3B-Instruct",
                    status="completed",
                    phase="completed",
                    queue_status="done",
                    edge_slot_id="edge-slot-edge_A",
                    cloud_slot_id="cloud-slot-1",
                    edge_device_id="edge_A",
                    cloud_device_id="cloud",
                    edge_status="ready",
                    cloud_status="ready",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                ),
            ])
            db.commit()
            keep_binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-keep",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
            )
            duplicate_binding = create_runtime_binding(
                db,
                session_id="session-1",
                task_id="task-dup",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-1",
            )
            keep_task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-keep").first()
            dup_task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-dup").first()
            keep_task.runtime_binding_id = keep_binding.binding_id
            dup_task.runtime_binding_id = duplicate_binding.binding_id
            db.add(keep_task)
            db.add(dup_task)
            slot = ensure_runtime_slot(db, slot_id="cloud-slot-0", role="cloud", control_url="http://127.0.0.1:19113/load_strategy", spawned_by_scheduler=True, process_state="running")
            transition_runtime_slot(db, slot, slot_state="bound", model_state="ready", owner_session_id="session-1", owner_binding_id=keep_binding.binding_id, task_id="task-keep", confirmation_status="passed")
            reconcile_runtime_ownership(db)
            db.refresh(keep_binding)
            db.refresh(duplicate_binding)
            self.assertEqual(keep_binding.status, "binding")
            self.assertEqual(duplicate_binding.status, "released")
        finally:
            db.close()

    def test_phase2_dispatch_failure_rolls_back_spawned_cloud_slot(self) -> None:
        self._create_session(session_id="session-2")
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
            transition_runtime_slot(
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
            db.add(RuntimeBinding(
                binding_id="binding-2",
                session_id="session-2",
                task_id=waiting_task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id=None,
                status="pending",
            ))
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
            patch("app.services.schedule_orchestrator.start_decode_server_process_locked", new=AsyncMock(return_value=process_info)),
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
            task = ScheduleTask(
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
            binding = RuntimeBinding(
                binding_id="binding-confirm",
                session_id="session-1",
                task_id=task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-1",
                status="binding",
            )
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(db, slot_id="cloud-slot-1", role="cloud", control_url="http://127.0.0.1:19114/load_strategy")
            transition_runtime_slot(
                db,
                edge_slot,
                slot_state="bound",
                model_state="loading",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id=task.task_id,
                model_type=task.model_type,
            )
            transition_runtime_slot(
                db,
                cloud_slot,
                slot_state="bound",
                model_state="loading",
                owner_session_id="session-1",
                owner_binding_id=binding.binding_id,
                task_id=task.task_id,
                model_type=task.model_type,
            )
            db.add_all([binding, task])
            db.commit()
        finally:
            db.close()

        import asyncio
        from app.api.v1.schedule import confirm_cloud_runtime_integrity
        from app.schemas.schemas import CloudRuntimeConfirmationRequest

        db = SessionLocal()
        try:
            with patch(
                "app.api.v1.schedule.forward_cloud_confirmation_to_edge",
                new=AsyncMock(return_value=(True, None)),
            ) as forward_mock:
                response = asyncio.run(confirm_cloud_runtime_integrity(
                    CloudRuntimeConfirmationRequest(
                        task_id="task-confirm",
                        cloud_slot_id="cloud-slot-1",
                        model_type="Llama-3.2-3B-Instruct",
                        server_param_digest="d" * 64,
                        partition_digest="e" * 64,
                        timestamp=1234567890,
                        nonce="nonce-1",
                    ),
                    None,
                    db,
                ))
        finally:
            db.close()

        self.assertTrue(response.matched)
        forwarded_payload = forward_mock.await_args.kwargs["payload"]
        self.assertEqual(forwarded_payload.server_param_digest, "d" * 64)
        self.assertEqual(forwarded_payload.partition_digest, "e" * 64)

        db = SessionLocal()
        try:
            cloud_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").first()
            self.assertIsNotNone(cloud_slot)
            self.assertEqual(cloud_slot.confirmation_status, "passed")
        finally:
            db.close()

    def test_stale_ready_progress_cannot_mutate_reallocated_edge_slot(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            old_task = ScheduleTask(
                task_id="task-old-callback",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-old-callback",
                model_type="Llama-3.2-3B-Instruct",
                status="failed",
                phase="loading",
                queue_status="done",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            new_task = ScheduleTask(
                task_id="task-new-owner",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-new-owner",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                queue_status="running_loading",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                allocated_cloud_slot_id="cloud-slot-0",
                edge_status="loading",
                cloud_status="loading",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            old_binding = RuntimeBinding(
                binding_id="binding-old-callback",
                session_id="session-1",
                task_id=old_task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                status="released",
            )
            new_binding = RuntimeBinding(
                binding_id="binding-new-owner",
                session_id="session-1",
                task_id=new_task.task_id,
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-0",
                status="binding",
            )
            edge_slot = ensure_runtime_slot(
                db,
                slot_id="edge-slot-edge_A",
                role="edge",
                control_url="http://127.0.0.1:19112/load_strategy",
            )
            cloud_slot = ensure_runtime_slot(
                db,
                slot_id="cloud-slot-0",
                role="cloud",
                control_url="http://127.0.0.1:19113/load_strategy",
            )
            for slot in (edge_slot, cloud_slot):
                transition_runtime_slot(
                    db,
                    slot,
                    slot_state="bound",
                    model_state="loading",
                    owner_session_id="session-1",
                    owner_binding_id=new_binding.binding_id,
                    task_id=new_task.task_id,
                    model_type=new_task.model_type,
                    confirmation_status="none",
                )
            db.add_all([old_task, new_task, old_binding, new_binding])
            db.commit()
        finally:
            db.close()

        import asyncio
        from types import SimpleNamespace
        from app.services.schedule_orchestrator import (
            cleanup_task_after_lease_loss,
            handle_runtime_progress,
        )

        result = asyncio.run(handle_runtime_progress(SimpleNamespace(
            task_id="task-old-callback",
            status="ready",
            progress=100,
            message="late ready",
            stage="runtime_load",
            node_role="edge",
        )))
        self.assertEqual(result["status"], "success")
        self.assertIn("已过期", result["message"])

        with patch(
            "app.services.slot_reaper.fetch_runtime_state",
            new=AsyncMock(),
        ) as fetch_mock:
            asyncio.run(cleanup_task_after_lease_loss(
                "task-old-callback",
                "superseded by task-new-owner",
            ))
        fetch_mock.assert_not_awaited()

        db = SessionLocal()
        try:
            edge_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "edge-slot-edge_A").one()
            new_task = db.query(ScheduleTask).filter(ScheduleTask.task_id == "task-new-owner").one()
            self.assertEqual(edge_slot.task_id, "task-new-owner")
            self.assertEqual(edge_slot.owner_binding_id, "binding-new-owner")
            self.assertEqual(edge_slot.model_state, "loading")
            self.assertEqual(new_task.edge_status, "loading")
            self.assertEqual(new_task.edge_runtime_load_progress, 0)
        finally:
            db.close()

    def test_stale_cloud_confirmation_cannot_mutate_reallocated_slot(self) -> None:
        self._create_session()
        db = SessionLocal()
        try:
            old_task = ScheduleTask(
                task_id="task-old-confirm",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-old-confirm",
                model_type="Llama-3.2-3B-Instruct",
                status="failed",
                phase="loading",
                queue_status="done",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-1",
                allocated_cloud_slot_id="cloud-slot-1",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            new_task = ScheduleTask(
                task_id="task-new-confirm",
                openwebui_user_id="user-1",
                edge_session_id="session-1",
                runtime_binding_id="binding-new-confirm",
                model_type="Llama-3.2-3B-Instruct",
                status="running",
                phase="loading",
                queue_status="running_loading",
                edge_slot_id="edge-slot-edge_A",
                cloud_slot_id="cloud-slot-1",
                allocated_cloud_slot_id="cloud-slot-1",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add_all([
                old_task,
                new_task,
                RuntimeBinding(
                    binding_id="binding-old-confirm",
                    session_id="session-1",
                    task_id=old_task.task_id,
                    edge_slot_id="edge-slot-edge_A",
                    cloud_slot_id="cloud-slot-1",
                    status="released",
                ),
                RuntimeBinding(
                    binding_id="binding-new-confirm",
                    session_id="session-1",
                    task_id=new_task.task_id,
                    edge_slot_id="edge-slot-edge_A",
                    cloud_slot_id="cloud-slot-1",
                    status="binding",
                ),
            ])
            edge_slot = ensure_runtime_slot(db, slot_id="edge-slot-edge_A", role="edge", control_url="http://127.0.0.1:19112/load_strategy")
            cloud_slot = ensure_runtime_slot(db, slot_id="cloud-slot-1", role="cloud", control_url="http://127.0.0.1:19114/load_strategy")
            for slot in (edge_slot, cloud_slot):
                transition_runtime_slot(
                    db,
                    slot,
                    slot_state="bound",
                    model_state="loading",
                    owner_session_id="session-1",
                    owner_binding_id="binding-new-confirm",
                    task_id="task-new-confirm",
                    model_type="Llama-3.2-3B-Instruct",
                    confirmation_status="none",
                )
            db.commit()
        finally:
            db.close()

        import asyncio
        from app.api.v1.schedule import confirm_cloud_runtime_integrity
        from app.schemas.schemas import CloudRuntimeConfirmationRequest

        db = SessionLocal()
        try:
            with patch("app.api.v1.schedule.forward_cloud_confirmation_to_edge", new=AsyncMock()) as forward_mock:
                response = asyncio.run(confirm_cloud_runtime_integrity(
                    CloudRuntimeConfirmationRequest(
                        task_id="task-old-confirm",
                        cloud_slot_id="cloud-slot-1",
                        model_type="Llama-3.2-3B-Instruct",
                        server_param_digest="sha256:server",
                        partition_digest="sha256:partition",
                        timestamp=1234567890,
                        nonce="late-nonce",
                    ),
                    None,
                    db,
                ))
        finally:
            db.close()
        self.assertFalse(response.matched)
        forward_mock.assert_not_awaited()

        db = SessionLocal()
        try:
            cloud_slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-1").one()
            self.assertEqual(cloud_slot.task_id, "task-new-confirm")
            self.assertEqual(cloud_slot.owner_binding_id, "binding-new-confirm")
            self.assertEqual(cloud_slot.confirmation_status, "none")
        finally:
            db.close()

def test_reconcile_dead_spawned_slot_releases_to_free_stopped(self) -> None:
    self._create_session(status='closed')
    db = SessionLocal()
    try:
        task = ScheduleTask(
            task_id='task-dead',
            openwebui_user_id='user-1',
            edge_session_id='session-1',
            runtime_binding_id='binding-dead',
            model_type='Llama-3.2-3B-Instruct',
            status='completed',
            phase='completed',
            phase_progress=100,
            overall_progress=100,
            message='done',
            edge_device_id='edge_A',
            cloud_device_id='cloud',
            edge_progress=100,
            cloud_progress=100,
            edge_status='ready',
            cloud_status='ready',
            queue_status='done',
            queue_position=0,
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-1',
            allocated_cloud_slot_id='cloud-slot-1',
            edge_message='done',
            cloud_message='done',
            strategy_payload='{"layer_partitions": []}',
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()
        binding = create_runtime_binding(
            db,
            session_id='session-1',
            task_id='task-dead',
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-1',
        )
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-1',
            role='cloud',
            control_url='http://127.0.0.1:19117/load_strategy',
            grpc_target='127.0.0.1:52167',
            slot_index=1,
            spawned_by_scheduler=True,
            process_state='running',
            process_pid=999999,
            base_env_name='.env.wyy',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='bound',
            model_state='ready',
            owner_session_id='session-1',
            owner_binding_id=binding.binding_id,
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-dead',
        )
        import asyncio
        asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-1').first()
        binding = db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == binding.binding_id).first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'stopped')
        self.assertEqual(slot.slot_state, 'free')
        self.assertEqual(slot.model_state, 'empty')
        self.assertIsNone(slot.owner_binding_id)
        self.assertIsNone(slot.process_pid)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.status, 'released')
    finally:
        db.close()

def test_reconcile_needs_reconcile_spawned_slot_returns_to_free_stopped(self) -> None:
    db = SessionLocal()
    try:
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-2',
            role='cloud',
            control_url='http://127.0.0.1:19118/load_strategy',
            grpc_target='127.0.0.1:52168',
            slot_index=2,
            spawned_by_scheduler=True,
            process_state='failed',
            process_pid=999998,
            base_env_name='.env.wyy',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='needs_reconcile',
            model_state='failed',
            owner_session_id='session-ghost',
            owner_binding_id='binding-ghost',
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-ghost',
        )
        import asyncio
        asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-2').first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'stopped')
        self.assertEqual(slot.slot_state, 'free')
        self.assertEqual(slot.model_state, 'empty')
        self.assertIsNone(slot.owner_session_id)
        self.assertIsNone(slot.owner_binding_id)
    finally:
        db.close()

def test_reconcile_orphan_failed_managed_cloud_slot_without_pid_returns_to_free_stopped(self) -> None:
    db = SessionLocal()
    try:
        slot = ensure_runtime_slot(
            db,
            slot_id="cloud-slot-orphan",
            role="cloud",
            control_url="http://127.0.0.1:19119/load_strategy",
            grpc_target="127.0.0.1:52169",
            slot_index=3,
            spawned_by_scheduler=True,
            process_state="failed",
            process_pid=None,
            base_env_name=".env.prod",
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state="needs_reconcile",
            model_state="failed",
            owner_session_id=None,
            owner_binding_id=None,
            model_type=None,
            task_id=None,
        )
        import asyncio
        asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == "cloud-slot-orphan").first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, "stopped")
        self.assertEqual(slot.slot_state, "free")
        self.assertEqual(slot.model_state, "empty")
        self.assertIsNone(slot.process_pid)
        self.assertIsNone(slot.owner_session_id)
        self.assertIsNone(slot.owner_binding_id)
    finally:
        db.close()

def test_reconcile_finished_task_keeps_ready_managed_cloud_slot_bound(self) -> None:
    self._create_session()
    db = SessionLocal()
    try:
        task = ScheduleTask(
            task_id='task-finished-ready',
            schedule_id='sched-finished-ready',
            openwebui_user_id='user-1',
            edge_device_id='edge_A',
            cloud_device_id='cloud',
            edge_ip='10.0.0.1',
            cloud_ip='10.0.0.2',
            user_id='user-1',
            model_name='Llama-3.2-3B-Instruct',
            model_type='Llama-3.2-3B-Instruct',
            status='completed',
            phase='completed',
            queue_status='done',
            message='done',
            edge_status='ready',
            cloud_status='ready',
        )
        db.add(task)
        db.commit()
        binding = create_runtime_binding(
            db,
            session_id='session-1',
            task_id='task-finished-ready',
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-1',
        )
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-1',
            role='cloud',
            control_url='http://127.0.0.1:19115/load_strategy',
            grpc_target='127.0.0.1:52165',
            slot_index=1,
            spawned_by_scheduler=True,
            process_state='running',
            process_pid=999995,
            base_env_name='.env.wyy',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='bound',
            model_state='ready',
            owner_session_id='session-1',
            owner_binding_id=binding.binding_id,
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-finished-ready',
            confirmation_status='pending',
            integrity_status='unknown',
        )
        with patch('app.services.runtime_slot_reconcile_service.inspect_slot_process', return_value=object()), \
             patch('app.services.runtime_slot_reconcile_service.fetch_runtime_state', new=AsyncMock(return_value={
                 'ready': True,
                 'draining': False,
                 'active_request_count': 0,
                 'model_type': 'Llama-3.2-3B-Instruct',
                 'task_id': 'task-finished-ready',
             })), \
             patch('app.services.runtime_slot_reconcile_service.unload_runtime_slot', new=AsyncMock()) as unload_mock:
            import asyncio
            asyncio.run(reconcile_runtime_slot(db, slot))
            self.assertEqual(unload_mock.await_count, 0)
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-1').first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'running')
        self.assertEqual(slot.slot_state, 'bound')
        self.assertEqual(slot.model_state, 'ready')
        self.assertEqual(slot.confirmation_status, 'passed')
        self.assertEqual(slot.integrity_status, 'healthy')
        self.assertEqual(slot.owner_session_id, 'session-1')
    finally:
        db.close()


def test_reconcile_healthy_slot_keeps_bound_ready_running(self) -> None:
    self._create_session()
    db = SessionLocal()
    try:
        binding = create_runtime_binding(
            db,
            session_id='session-1',
            task_id='task-live',
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-0',
        )
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-0',
            role='cloud',
            control_url='http://127.0.0.1:19114/load_strategy',
            grpc_target='127.0.0.1:52164',
            slot_index=0,
            spawned_by_scheduler=False,
            process_state='running',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='bound',
            model_state='failed',
            owner_session_id='session-1',
            owner_binding_id=binding.binding_id,
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-live',
            confirmation_status='none',
            integrity_status='unknown',
        )
        with patch('app.services.runtime_slot_reconcile_service.fetch_runtime_state', new=AsyncMock(return_value={
            'ready': True,
            'draining': False,
            'active_request_count': 0,
            'model_type': 'Llama-3.2-3B-Instruct',
            'task_id': 'task-live',
        })):
            import asyncio
            asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-0').first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'running')
        self.assertEqual(slot.slot_state, 'bound')
        self.assertEqual(slot.model_state, 'ready')
        self.assertEqual(slot.integrity_status, 'healthy')
        self.assertEqual(slot.confirmation_status, 'passed')
    finally:
        db.close()

def test_reconcile_loading_managed_cloud_slot_tolerates_transient_health_failure(self) -> None:
    self._create_session()
    db = SessionLocal()
    try:
        task = ScheduleTask(
            task_id='task-loading',
            schedule_id='sched-1',
            openwebui_user_id='user-1',
            edge_device_id='edge_A',
            cloud_device_id='cloud',
            edge_ip='10.0.0.1',
            cloud_ip='10.0.0.2',
            user_id='user-1',
            model_name='Llama-3.2-3B-Instruct',
            model_type='Llama-3.2-3B-Instruct',
            status='accepted',
            phase='loading',
            queue_status='running_loading',
            message='loading',
            edge_status='loading',
            cloud_status='loading',
        )
        db.add(task)
        db.commit()
        binding = create_runtime_binding(
            db,
            session_id='session-1',
            task_id='task-loading',
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-1',
        )
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-1',
            role='cloud',
            control_url='http://127.0.0.1:19115/load_strategy',
            grpc_target='127.0.0.1:52165',
            slot_index=1,
            spawned_by_scheduler=True,
            process_state='running',
            process_pid=999996,
            base_env_name='.env.wyy',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='bound',
            model_state='loading',
            owner_session_id='session-1',
            owner_binding_id=binding.binding_id,
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-loading',
        )
        with patch('app.services.runtime_slot_reconcile_service.inspect_slot_process', return_value=None), \
             patch('app.services.managed_cloud_slot_cleanup_service.stop_slot_process', return_value=True), \
             patch('app.services.runtime_slot_reconcile_service.fetch_runtime_state', new=AsyncMock(side_effect=RuntimeError('warming up'))):
            import asyncio
            asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-1').first()
        task = db.query(ScheduleTask).filter(ScheduleTask.task_id == 'task-loading').first()
        binding = db.query(RuntimeBinding).filter(RuntimeBinding.binding_id == binding.binding_id).first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'starting')
        self.assertEqual(slot.slot_state, 'bound')
        self.assertEqual(slot.model_state, 'loading')
        self.assertEqual(slot.owner_binding_id, binding.binding_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, 'accepted')
        self.assertEqual(task.phase, 'loading')
        self.assertEqual(binding.status, 'active')
    finally:
        db.close()


def test_reconcile_base_or_edge_unreachable_clears_missing_binding_owner(self) -> None:
    self._create_session()
    db = SessionLocal()
    try:
        slot = ensure_runtime_slot(
            db,
            slot_id='edge-slot-edge_A',
            role='edge',
            control_url='http://127.0.0.1:19112/load_strategy',
            process_state='running',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='bound',
            model_state='ready',
            owner_session_id='session-1',
            owner_binding_id='binding-missing',
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-edge-missing-binding',
        )
        with patch('app.services.runtime_slot_reconcile_service.fetch_runtime_state', new=AsyncMock(side_effect=RuntimeError('down'))):
            import asyncio
            asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'edge-slot-edge_A').first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'failed')
        self.assertEqual(slot.slot_state, 'free')
        self.assertEqual(slot.model_state, 'empty')
        self.assertIsNone(slot.owner_binding_id)
        self.assertIsNone(slot.owner_session_id)
    finally:
        db.close()

def test_reconcile_base_or_edge_unreachable_marks_needs_reconcile(self) -> None:
    self._create_session()
    db = SessionLocal()
    try:
        binding = create_runtime_binding(
            db,
            session_id='session-1',
            task_id='task-edge',
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-0',
        )
        slot = ensure_runtime_slot(
            db,
            slot_id='edge-slot-edge_A',
            role='edge',
            control_url='http://127.0.0.1:19112/load_strategy',
            process_state='running',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='bound',
            model_state='ready',
            owner_session_id='session-1',
            owner_binding_id=binding.binding_id,
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-edge',
        )
        with patch('app.services.runtime_slot_reconcile_service.fetch_runtime_state', new=AsyncMock(side_effect=RuntimeError('down'))):
            import asyncio
            asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'edge-slot-edge_A').first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'failed')
        self.assertEqual(slot.slot_state, 'needs_reconcile')
        self.assertEqual(slot.model_state, 'failed')
    finally:
        db.close()

def test_reconcile_released_binding_clears_slot_owner(self) -> None:
    self._create_session()
    db = SessionLocal()
    try:
        binding = create_runtime_binding(
            db,
            session_id='session-1',
            task_id='task-release',
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-0',
        )
        transition_runtime_binding(db, binding, status='released')
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-0',
            role='cloud',
            control_url='http://127.0.0.1:19114/load_strategy',
            grpc_target='127.0.0.1:52164',
            slot_index=0,
            process_state='running',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='bound',
            model_state='empty',
            owner_session_id='session-1',
            owner_binding_id=binding.binding_id,
            model_type='Llama-3.2-3B-Instruct',
            task_id='task-release',
        )
        with patch('app.services.runtime_slot_reconcile_service.fetch_runtime_state', new=AsyncMock(return_value={
            'ready': False,
            'draining': False,
            'active_request_count': 0,
            'model_type': None,
            'task_id': None,
        })):
            import asyncio
            asyncio.run(reconcile_runtime_slot(db, slot))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-0').first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.slot_state, 'free')
        self.assertEqual(slot.model_state, 'empty')
        self.assertIsNone(slot.owner_binding_id)
        self.assertIsNone(slot.owner_session_id)
    finally:
        db.close()

def test_reconcile_all_runtime_slots_processes_multiple_slots(self) -> None:
    db = SessionLocal()
    try:
        ensure_runtime_slot(
            db,
            slot_id='cloud-slot-1',
            role='cloud',
            control_url='http://127.0.0.1:19117/load_strategy',
            grpc_target='127.0.0.1:52167',
            slot_index=1,
            spawned_by_scheduler=True,
            process_state='failed',
            process_pid=999997,
            base_env_name='.env.wyy',
        )
        ensure_runtime_slot(
            db,
            slot_id='edge-slot-edge_A',
            role='edge',
            control_url='http://127.0.0.1:19112/load_strategy',
            process_state='running',
        )
        slot_cloud = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-1').first()
        slot_edge = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'edge-slot-edge_A').first()
        transition_runtime_slot(db, slot_cloud, slot_state='needs_reconcile', model_state='failed')
        transition_runtime_slot(db, slot_edge, slot_state='free', model_state='empty')
        with patch('app.services.runtime_slot_reconcile_service.fetch_runtime_state', new=AsyncMock(return_value={
            'ready': False,
            'draining': False,
            'active_request_count': 0,
            'model_type': None,
            'task_id': None,
        })):
            import asyncio
            slot_ids = asyncio.run(reconcile_all_runtime_slots(db))
        self.assertIn('cloud-slot-1', slot_ids)
        self.assertIn('edge-slot-edge_A', slot_ids)
    finally:
        db.close()


def test_bootstrap_managed_cloud_slot_zero_starts_decode_when_absent(self) -> None:
    db = SessionLocal()
    try:
        process_info = MagicMock(
            slot_id='cloud-slot-0',
            slot_index=0,
            http_port=19114,
            grpc_port=52164,
            control_url='http://127.0.0.1:19114/load_strategy',
            grpc_target='127.0.0.1:52164',
            process_pid=32100,
        )
        with (
            patch('app.services.managed_cloud_slot_bootstrap_service.start_decode_server_process_for_slot_locked', new=AsyncMock(return_value=process_info)),
            patch('app.services.managed_cloud_slot_bootstrap_service.wait_for_slot_health', new=AsyncMock(return_value=True)),
        ):
            import asyncio
            asyncio.run(bootstrap_managed_cloud_slots(db))
    finally:
        db.close()

    db = SessionLocal()
    try:
        slot = db.query(RuntimeSlot).filter(RuntimeSlot.slot_id == 'cloud-slot-0').first()
        self.assertIsNotNone(slot)
        self.assertEqual(slot.process_state, 'running')
        self.assertEqual(slot.slot_state, 'free')
        self.assertEqual(slot.model_state, 'empty')
        self.assertEqual(slot.spawned_by_scheduler, 1)
        self.assertEqual(slot.process_pid, 32100)
    finally:
        db.close()

def test_allocate_cloud_slot_reuses_stopped_cloud_slot_zero(self) -> None:
    db = SessionLocal()
    try:
        task = ScheduleTask(
            task_id='task-slot0-reuse',
            openwebui_user_id='user-1',
            edge_session_id='session-1',
            runtime_binding_id='binding-slot0-reuse',
            model_type='Llama-3.2-3B-Instruct',
            status='accepted',
            phase='loading',
            phase_progress=0,
            overall_progress=0,
            message='waiting',
            edge_device_id='edge_A',
            cloud_device_id='cloud',
            edge_progress=0,
            cloud_progress=0,
            edge_status='pending',
            cloud_status='pending',
            queue_status='running_loading',
            queue_position=0,
            edge_slot_id='edge-slot-edge_A',
            cloud_slot_id='cloud-slot-0',
            allocated_cloud_slot_id='cloud-slot-0',
            edge_message='pending',
            cloud_message='pending',
            strategy_payload='{"layer_partitions": []}',
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(task)
        db.commit()
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-0',
            role='cloud',
            control_url='http://127.0.0.1:19114/load_strategy',
            grpc_target='127.0.0.1:52164',
            slot_index=0,
            spawned_by_scheduler=True,
            process_state='stopped',
            base_env_name='.env.wyy',
        )
        transition_runtime_slot(db, slot, slot_state='free', model_state='empty')
        process_info = MagicMock(
            slot_id='cloud-slot-0',
            slot_index=0,
            http_port=19114,
            grpc_port=52164,
            control_url='http://127.0.0.1:19114/load_strategy',
            grpc_target='127.0.0.1:52164',
            process_pid=32101,
        )
        with (
            patch('app.services.schedule_orchestrator.start_decode_server_process_for_slot_locked', new=AsyncMock(return_value=process_info)),
            patch('app.services.schedule_orchestrator.wait_for_slot_health', new=AsyncMock(return_value=True)),
        ):
            import asyncio
            cloud_slot, spawned = asyncio.run(allocate_cloud_slot_for_task(db, task, '127.0.0.1'))
        self.assertEqual(cloud_slot.slot_id, 'cloud-slot-0')
        self.assertTrue(spawned)
    finally:
        db.close()

def test_stop_idle_managed_cloud_slot_zero_stops_process(self) -> None:
    db = SessionLocal()
    try:
        slot = ensure_runtime_slot(
            db,
            slot_id='cloud-slot-0',
            role='cloud',
            control_url='http://127.0.0.1:19114/load_strategy',
            grpc_target='127.0.0.1:52164',
            slot_index=0,
            spawned_by_scheduler=True,
            process_state='running',
            process_pid=32102,
            base_env_name='.env.wyy',
        )
        transition_runtime_slot(
            db,
            slot,
            slot_state='free',
            model_state='empty',
            process_idle_deadline=datetime.utcnow() - timedelta(seconds=1),
        )
        with patch('app.services.managed_cloud_slot_cleanup_service.stop_slot_process', return_value=True):
            stopped = stop_idle_spawned_cloud_slots(db)
        self.assertEqual(stopped, ['cloud-slot-0'])
    finally:
        db.close()


if __name__ == "__main__":
    unittest.main()
