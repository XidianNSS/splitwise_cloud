import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.algorithm_dispatcher import build_algorithm_request_payload, normalize_algorithm_decision, resolve_runtime_decision
from app.services.model_registry import (
    MODEL_REGISTRY, canonicalize_model_type, list_model_catalog,
    resolve_model_type_key, runtime_model_type, uses_fixed_runtime_strategy,
)
from app.services.runtime_startup_admission import check_runtime_startup_resources


class DeepSeek8BSchedulerTest(unittest.TestCase):
    name = "DeepSeek-R1-Distill-Llama-8B"
    key = "deepseek-r1-distill-llama-8b"

    def decision(self):
        return {"model_type": self.name, "layer_partitions": [
            {"layer_id": i, "head_assignments": [0, 1] * 16, "ffn_assignment": i % 2}
            for i in range(32)
        ]}

    def test_split_ffn_projection_is_explicit_and_preserves_attention(self):
        payload = self.decision()
        payload["layer_partitions"][30]["ffn_assignment"] = 2
        result = normalize_algorithm_decision(self.name, payload)
        self.assertEqual(result["layer_partitions"][30]["ffn_assignment"], 1)
        self.assertEqual(result["layer_partitions"][30]["head_assignments"], [0, 1] * 16)
        self.assertEqual(result["runtime_adjustments"][0]["requested"], 2)
        self.assertEqual(result["runtime_adjustments"][0]["applied"], 1)
        self.assertEqual(payload["layer_partitions"][30]["ffn_assignment"], 2)

    def test_invalid_policy_dimensions_and_assignments_are_rejected(self):
        for change in ("head_count", "layer_count", "duplicate_layer", "head_value", "ffn_value", "model_name", "head_totals"):
            with self.subTest(change=change):
                payload = self.decision()
                if change == "head_count": payload["layer_partitions"][0]["head_assignments"] = [0]
                elif change == "layer_count": payload["layer_partitions"].pop()
                elif change == "duplicate_layer": payload["layer_partitions"][1]["layer_id"] = 0
                elif change == "head_value": payload["layer_partitions"][0]["head_assignments"][0] = 2
                elif change == "ffn_value": payload["layer_partitions"][0]["ffn_assignment"] = 3
                elif change == "model_name": payload["model_type"] = "Llama-3.2-3B"
                elif change == "head_totals": payload["layer_partitions"][0]["edge_heads"] = 31
                with self.assertRaises(ValueError): normalize_algorithm_decision(self.name, payload)

    def test_existing_models_do_not_silently_project_split_ffn(self):
        payload = {"layer_partitions": [{
            "layer_id": i, "head_assignments": [0] * 24, "ffn_assignment": 2,
        } for i in range(28)]}
        with self.assertRaisesRegex(ValueError, "FFN"):
            normalize_algorithm_decision("Llama-3.2-3B", payload)

    def test_policy_integer_fields_reject_coercion(self):
        for field in ("layer_id", "ffn_assignment", "head_assignments"):
            for value in (0.9, 1.9, 2.9, 0.0, "0", True, None):
                with self.subTest(field=field, value=value):
                    payload = self.decision()
                    layer = payload["layer_partitions"][0]
                    if field == "head_assignments":
                        layer[field][0] = value
                    else:
                        layer[field] = value
                    with self.assertRaises(ValueError):
                        normalize_algorithm_decision(self.name, payload)

    def test_optional_head_counts_are_validated_whenever_present(self):
        for field in ("edge_heads", "cloud_heads"):
            for value in ("999", "16", 16.0, True, None, 999):
                with self.subTest(field=field, value=value):
                    payload = self.decision()
                    payload["layer_partitions"][0][field] = value
                    with self.assertRaises(ValueError):
                        normalize_algorithm_decision(self.name, payload)

    def test_valid_integer_counts_remain_supported_for_existing_models(self):
        for name, layers, heads in ((self.name, 32, 32), ("Llama-3.2-3B", 28, 24)):
            with self.subTest(model=name):
                payload = {"layer_partitions": [{
                    "layer_id": i, "head_assignments": [0, 1] * (heads // 2),
                    "ffn_assignment": i % 2, "edge_heads": heads // 2,
                    "cloud_heads": heads // 2,
                } for i in range(layers)]}
                result = normalize_algorithm_decision(name, payload)
                self.assertEqual(len(result["layer_partitions"]), layers)
                self.assertEqual(result["layer_partitions"][0]["edge_head_count"], heads // 2)
                self.assertNotIn("runtime_adjustments", result)

    def test_required_policy_fields_and_head_array_cannot_be_omitted_or_coerced(self):
        for field in ("layer_id", "ffn_assignment", "head_assignments"):
            with self.subTest(missing=field):
                payload = self.decision()
                del payload["layer_partitions"][0][field]
                with self.assertRaises(ValueError):
                    normalize_algorithm_decision(self.name, payload)
        for value in ("01" * 16, None, {"0": 0}):
            with self.subTest(head_array=value):
                payload = self.decision()
                payload["layer_partitions"][0]["head_assignments"] = value
                with self.assertRaises(ValueError):
                    normalize_algorithm_decision(self.name, payload)

    def test_catalog_names_and_generation_capability(self):
        self.assertEqual(resolve_model_type_key(self.name.upper()), self.key)
        self.assertEqual(canonicalize_model_type(self.key), self.name)
        self.assertEqual(runtime_model_type(self.key), self.name)
        item = next(m for m in list_model_catalog() if m["model_type"] == self.name)
        self.assertEqual(item["runtime_model_type"], self.name)
        self.assertEqual(item["capabilities"], ["generation"])
        self.assertEqual(item["deployment_mode"], "standard")
        self.assertEqual(item["strategy_kind"], "algorithm")
        self.assertFalse(uses_fixed_runtime_strategy(self.key))

    def test_algorithm_receives_actual_model_dimensions(self):
        payload = build_algorithm_request_payload(
            model_type=self.name, model_type_key=self.key,
            edge_metrics={"gpu_mem_total_mb": 32768},
            cloud_metrics={"gpu_mem_total_mb": 65536, "accelerator_type": "ascend"},
            network_metrics={}, edge_storage_limit_gb=30,
        )
        for role in ("edge", "cloud"):
            self.assertEqual(payload["env"][role]["model_spec"], {
                "num_hidden_layers": 32, "num_attention_heads": 32,
            })
        self.assertEqual(MODEL_REGISTRY[self.key]["num_key_value_heads"], 8)
        with patch("app.services.algorithm_dispatcher.request_algorithm_decision", new=AsyncMock(return_value={"ok": True})) as call:
            result = asyncio.run(resolve_runtime_decision("task", self.name, self.key, payload))
        call.assert_awaited_once_with("task", self.name, payload)
        self.assertEqual(result, {"ok": True})

    def test_admission_reserves_more_than_the_weights_alone(self):
        with patch("app.services.runtime_startup_admission.settings.MODEL_STARTUP_RESOURCE_CHECK_ENABLED", True):
            self.assertEqual(check_runtime_startup_resources(
                model_type_key=self.key,
                edge_metrics={"gpu_mem_total_mb": 32768, "gpu_mem_used_mb": 1024},
                cloud_metrics={"gpu_mem_total_mb": 65536, "gpu_mem_used_mb": 4096},
            ), [])
            for role in ("edge", "cloud"):
                metrics = {"edge": {"gpu_mem_total_mb": 65536}, "cloud": {"gpu_mem_total_mb": 65536}}
                metrics[role] = {"gpu_mem_total_mb": 16000}
                failures = check_runtime_startup_resources(
                    model_type_key=self.key, edge_metrics=metrics["edge"], cloud_metrics=metrics["cloud"],
                )
                self.assertEqual(len(failures), 1)
