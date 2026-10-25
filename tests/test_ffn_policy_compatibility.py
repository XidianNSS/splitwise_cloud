import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.algorithm_dispatcher import normalize_algorithm_decision, resolve_runtime_decision
from app.services.model_registry import MODEL_REGISTRY, canonicalize_model_type


def generation_models():
    return [(key, spec) for key, spec in MODEL_REGISTRY.items()
            if spec.get("capability", "generation") == "generation"
            and spec.get("strategy_kind", "algorithm") == "algorithm"]


def mixed_policy(name, spec):
    return {"model_type": name, "layer_partitions": [{
        "layer_id": i,
        "head_assignments": [j % 2 for j in range(spec["num_attention_heads"])],
        "ffn_assignment": i % 3,
    } for i in range(spec["num_hidden_layers"])]}


class FfnPolicyCompatibilityTest(unittest.TestCase):
    def test_all_registered_dynamic_generation_models_keep_attention_and_whole_ffns(self):
        for key, spec in generation_models():
            with self.subTest(model=key):
                name = canonicalize_model_type(key)
                payload = mixed_policy(name, spec)
                original = copy.deepcopy(payload)
                result = normalize_algorithm_decision(name, payload)
                expected_adjustments = []
                for raw, applied in zip(payload["layer_partitions"], result["layer_partitions"]):
                    self.assertEqual(applied["head_assignments"], raw["head_assignments"])
                    self.assertEqual(applied["ffn_assignment"], 1 if raw["ffn_assignment"] == 2 else raw["ffn_assignment"])
                    if raw["ffn_assignment"] == 2:
                        expected_adjustments.append(raw["layer_id"])
                self.assertEqual([item["layer_id"] for item in result["runtime_adjustments"]], expected_adjustments)
                for item in result["runtime_adjustments"]:
                    self.assertEqual((item["requested"], item["applied"]), (2, 1))
                    self.assertTrue(item["reason"])
                self.assertEqual(payload, original)

    def test_ffn_normalization_does_not_require_model_registration(self):
        result = normalize_algorithm_decision("unregistered-model", {"layer_partitions": [{
            "layer_id": 0, "head_assignments": [0, 1], "ffn_assignment": 2,
        }]})
        self.assertEqual(result["layer_partitions"][0]["ffn_assignment"], 1)
        self.assertEqual(result["runtime_adjustments"][0]["requested"], 2)

    def test_projection_does_not_relax_ffn_validation_for_other_models(self):
        for key, spec in generation_models():
            for value in (3, -1, 2.9, "2", True, None):
                with self.subTest(model=key, value=value):
                    name = canonicalize_model_type(key)
                    payload = mixed_policy(name, spec)
                    payload["layer_partitions"][0]["ffn_assignment"] = value
                    with self.assertRaises(ValueError):
                        normalize_algorithm_decision(name, payload)

    def test_generation_still_requests_algorithm_before_normalizing_response(self):
        real_client = httpx.AsyncClient
        received = []

        def algorithm(request):
            self.assertEqual(request.method, "POST")
            body = json.loads(request.content)
            received.append(body["model_type"])
            key = body["model_type"].lower()
            return httpx.Response(200, json=mixed_policy(body["model_type"], MODEL_REGISTRY[key]))

        transport = httpx.MockTransport(algorithm)

        def client_factory(**kwargs):
            return real_client(transport=transport, **kwargs)

        with patch("app.services.algorithm_dispatcher.httpx.AsyncClient", side_effect=client_factory):
            for key, spec in generation_models():
                with self.subTest(model=key):
                    name = canonicalize_model_type(key)
                    result = asyncio.run(resolve_runtime_decision("test-task", name, key, {"model_type": name}))
                    self.assertEqual(received[-1], name)
                    self.assertTrue(result["runtime_adjustments"])
                    self.assertTrue(all(row["ffn_assignment"] in (0, 1) for row in result["layer_partitions"]))
        self.assertEqual(len(received), len(generation_models()))
