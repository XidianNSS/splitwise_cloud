import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.v1.schedule import list_schedule_models
from app.services.algorithm_dispatcher import resolve_runtime_decision
from app.services.model_registry import (
    build_fixed_runtime_decision,
    resolve_model_type_key,
    uses_fixed_runtime_strategy,
)
from app.services.runtime_startup_admission import check_runtime_startup_resources


class BertSchedulerTestCase(unittest.TestCase):
    def test_catalog_exposes_bert_as_encrypted_embeddings(self) -> None:
        catalog = asyncio.run(
            list_schedule_models(current_openwebui_user_id="user-1")
        )
        bert = next(
            item for item in catalog if item["model_type"] == "BERT-Base-Uncased"
        )
        self.assertEqual(bert["capability"], "embeddings")
        self.assertEqual(bert["deployment_mode"], "encrypted")
        self.assertEqual(bert["strategy_kind"], "fixed_bert_encoder")

    def test_fixed_bert_decision_matches_modelsplt_partition_contract(self) -> None:
        decision = build_fixed_runtime_decision(
            "BERT-Base-Uncased",
            "bert-base-uncased",
        )
        self.assertEqual(len(decision["layer_partitions"]), 12)
        for layer_id, layer in enumerate(decision["layer_partitions"]):
            self.assertEqual(layer["layer_id"], layer_id)
            self.assertEqual(layer["head_assignments"], [1] * 12)
            self.assertEqual(layer["ffn_assignment"], 1)

    def test_bert_resolution_does_not_call_algorithm_service(self) -> None:
        with patch(
            "app.services.algorithm_dispatcher.request_algorithm_decision",
            new=AsyncMock(side_effect=AssertionError("algorithm must not be called")),
        ) as algorithm_mock:
            decision = asyncio.run(
                resolve_runtime_decision(
                    "task-bert",
                    "BERT-Base-Uncased",
                    "bert-base-uncased",
                    {"model_type": "BERT-Base-Uncased"},
                )
            )
        algorithm_mock.assert_not_awaited()
        self.assertEqual(decision["strategy_kind"], "fixed_bert_encoder")

    def test_bert_model_key_and_resource_admission(self) -> None:
        self.assertEqual(
            resolve_model_type_key("BERT-Base-Uncased"),
            "bert-base-uncased",
        )
        sufficient = {
            "gpu_mem_total_mb": 4096,
            "gpu_mem_used_mb": 1024,
        }
        self.assertEqual(
            check_runtime_startup_resources(
                model_type_key="bert-base-uncased",
                edge_metrics=sufficient,
                cloud_metrics=sufficient,
            ),
            [],
        )

    def test_generation_models_default_to_algorithm_strategy(self) -> None:
        for model_type_key in (
            "llama-3.2-3b",
            "llama-3.2-3b-instruct",
        ):
            with self.subTest(model_type_key=model_type_key):
                self.assertFalse(uses_fixed_runtime_strategy(model_type_key))

    def test_generation_resolution_calls_algorithm_service(self) -> None:
        expected = {
            "model_type": "Llama-3.2-3B-Instruct",
            "layer_partitions": [{"layer_id": 0}],
        }
        with patch(
            "app.services.algorithm_dispatcher.request_algorithm_decision",
            new=AsyncMock(return_value=expected),
        ) as algorithm_mock:
            decision = asyncio.run(
                resolve_runtime_decision(
                    "task-generation",
                    "Llama-3.2-3B-Instruct",
                    "llama-3.2-3b-instruct",
                    {"model_type": "Llama-3.2-3B-Instruct"},
                )
            )
        algorithm_mock.assert_awaited_once()
        self.assertEqual(decision, expected)


if __name__ == "__main__":
    unittest.main()
