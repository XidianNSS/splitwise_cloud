import json
import sys
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api.deps import get_current_openwebui_user_id, get_db
from app.api.v1.schedule import router
from app.db.database import Base
from app.models.models import ScheduleTask
from app.services.algorithm_dispatcher import normalize_algorithm_decision


class StrategyAdjustmentsApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1/schedule")

        async def override_user():
            return "strategy-test-user"

        async def override_db():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_current_openwebui_user_id] = override_user
        self.app.dependency_overrides[get_db] = override_db

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.engine.dispose()

    def persist_decision(self, decision, *, model_type="DeepSeek-R1-Distill-Llama-8B", task_id="strategy-test-task"):
        with self.sessions() as db:
            db.add(ScheduleTask(
                task_id=task_id, openwebui_user_id="strategy-test-user",
                model_type=model_type,
                strategy_payload=json.dumps(decision),
            ))
            db.commit()

    async def fetch_strategy(self, *, task_id="strategy-test-task"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver",
        ) as client:
            response = await client.get(f"/api/v1/schedule/tasks/{task_id}/strategy")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["decision"]

    async def test_persisted_projection_survives_endpoint_response_serialization(self):
        payload = {"layer_partitions": [{
            "layer_id": i, "head_assignments": [0, 1] * 16,
            "ffn_assignment": 2 if i == 30 else 1,
        } for i in range(32)]}
        decision = normalize_algorithm_decision("DeepSeek-R1-Distill-Llama-8B", payload)
        self.persist_decision(decision)

        displayed = await self.fetch_strategy()
        self.assertEqual(displayed["runtime_adjustments"], decision["runtime_adjustments"])
        self.assertEqual(displayed["layer_partitions"][30]["ffn_assignment"], 1)
        self.assertEqual(displayed["runtime_adjustments"][0]["requested"], 2)
        self.assertEqual(payload["layer_partitions"][30]["ffn_assignment"], 2)

    async def test_legacy_generation_models_expose_projection_through_api(self):
        for index, name in enumerate(("Llama-3.2-3B", "Llama-3.2-3B-Instruct")):
            with self.subTest(model=name):
                payload = {"layer_partitions": [{
                    "layer_id": i, "head_assignments": [0, 1] * 12,
                    "ffn_assignment": 2 if i == 27 else i % 2,
                } for i in range(28)]}
                decision = normalize_algorithm_decision(name, payload)
                task_id = f"legacy-task-{index}"
                self.persist_decision(decision, model_type=name, task_id=task_id)
                displayed = await self.fetch_strategy(task_id=task_id)
                self.assertEqual(displayed["runtime_adjustments"], decision["runtime_adjustments"])
                self.assertEqual(displayed["layer_partitions"][27]["ffn_assignment"], 1)
                self.assertEqual(displayed["layer_partitions"][0]["ffn_assignment"], 0)
                self.assertEqual(displayed["layer_partitions"][1]["ffn_assignment"], 1)
                self.assertEqual(displayed["edge_head_count_total"], 28 * 12)
                self.assertEqual(displayed["cloud_head_count_total"], 28 * 12)
                self.assertEqual(payload["layer_partitions"][27]["ffn_assignment"], 2)

    async def test_older_strategy_without_adjustments_returns_empty_list(self):
        self.persist_decision({"layer_partitions": [{
            "layer_id": 0, "head_assignments": [0, 1], "ffn_assignment": 1,
        }], "strategy_kind": "algorithm"})
        displayed = await self.fetch_strategy()
        self.assertEqual(displayed["runtime_adjustments"], [])
        self.assertEqual(displayed["strategy_kind"], "algorithm")
