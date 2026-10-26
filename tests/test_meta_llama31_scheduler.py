import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.algorithm_dispatcher import build_algorithm_request_payload, normalize_algorithm_decision, resolve_runtime_decision
from app.services.model_registry import MODEL_REGISTRY, canonicalize_model_type, list_model_catalog, resolve_model_type_key, runtime_model_type, uses_fixed_runtime_strategy
from app.services.runtime_startup_admission import check_runtime_startup_resources


class MetaLlama31SchedulerTest(unittest.TestCase):
    name = "Meta-Llama-3.1-8B-Instruct"
    key = "meta-llama-3.1-8b-instruct"

    def test_catalog_resolves_exact_runtime_name_and_generation_capability(self):
        self.assertEqual(resolve_model_type_key(self.name.upper()), self.key)
        self.assertEqual(canonicalize_model_type(self.key), self.name)
        self.assertEqual(runtime_model_type(self.key), self.name)
        item = next(row for row in list_model_catalog() if row["model_type"] == self.name)
        self.assertEqual(item["runtime_model_type"], self.name)
        self.assertEqual(item["capabilities"], ["generation"])
        self.assertEqual(item["deployment_mode"], "standard")
        self.assertEqual(item["strategy_kind"], "algorithm")
        self.assertFalse(uses_fixed_runtime_strategy(self.key))

    def test_algorithm_input_uses_32_layers_32_heads_and_dynamic_route(self):
        payload = build_algorithm_request_payload(
            model_type=self.name, model_type_key=self.key,
            edge_metrics={"gpu_mem_total_mb": 65536, "accelerator_type": "ascend"},
            cloud_metrics={"gpu_mem_total_mb": 65536, "accelerator_type": "ascend"},
            network_metrics={}, edge_storage_limit_gb=60,
        )
        for role in ("edge", "cloud"):
            self.assertEqual(payload["env"][role]["model_spec"], {
                "num_hidden_layers": 32, "num_attention_heads": 32,
            })
        self.assertEqual(MODEL_REGISTRY[self.key]["num_key_value_heads"], 8)
        with patch("app.services.algorithm_dispatcher.request_algorithm_decision", new=AsyncMock(return_value={"ok": True})) as call:
            result = asyncio.run(resolve_runtime_decision("meta-task", self.name, self.key, payload))
        call.assert_awaited_once_with("meta-task", self.name, payload)
        self.assertEqual(result, {"ok": True})

    def test_full_strategy_validates_dimensions_and_projects_ffn(self):
        payload = {"model_type": self.name, "layer_partitions": [{
            "layer_id": i, "head_assignments": [0, 1] * 16,
            "ffn_assignment": i % 3,
        } for i in range(32)]}
        result = normalize_algorithm_decision(self.name, payload)
        self.assertEqual(len(result["layer_partitions"]), 32)
        self.assertEqual(result["layer_partitions"][2]["ffn_assignment"], 1)
        self.assertEqual(payload["layer_partitions"][2]["ffn_assignment"], 2)
        payload["layer_partitions"][0]["head_assignments"].pop()
        with self.assertRaises(ValueError):
            normalize_algorithm_decision(self.name, payload)

    def test_bf16_startup_admission_reserves_runtime_capacity(self):
        with patch("app.services.runtime_startup_admission.settings.MODEL_STARTUP_RESOURCE_CHECK_ENABLED", True):
            sufficient = {"gpu_mem_total_mb": 65536, "gpu_mem_used_mb": 4096}
            self.assertEqual(check_runtime_startup_resources(
                model_type_key=self.key, edge_metrics=sufficient, cloud_metrics=sufficient,
            ), [])
            for role, low_memory in (("edge", 16000), ("cloud", 24000)):
                with self.subTest(role=role):
                    metrics = {"edge": sufficient, "cloud": sufficient}
                    metrics[role] = {"gpu_mem_total_mb": low_memory}
                    failures = check_runtime_startup_resources(
                        model_type_key=self.key, edge_metrics=metrics["edge"], cloud_metrics=metrics["cloud"],
                    )
                    self.assertEqual(len(failures), 1)
