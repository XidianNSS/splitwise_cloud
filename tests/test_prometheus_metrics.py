import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

TMPDIR = tempfile.mkdtemp(prefix="splitwise-cloud-prom-test-")
os.environ["SQLITE_DB_PATH"] = os.path.join(TMPDIR, "test_cloud_edge.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services import prometheus_metrics


class PrometheusMetricsBestChipTest(unittest.IsolatedAsyncioTestCase):
    async def test_multi_chip_metrics_use_chip_with_largest_free_memory(self) -> None:
        async def fake_query_prom(_client, query):
            for name, expected_query in prometheus_metrics.PROMETHEUS_QUERY_TEMPLATES["ascend"].items():
                if expected_query.format(ip_regex="^10.144.144.4:.*") == query:
                    return {
                        "cpu": 11.0,
                        "mem": 22.0,
                        "gpu_util": 55.0,
                        "gpu_used": 900.0,
                        "gpu_free": 1100.0,
                    }[name]
            raise AssertionError(f"unexpected aggregate query: {query}")

        async def fake_query_prom_series(_client, query):
            formatted = {
                name: template.format(ip_regex="^10.144.144.4:.*")
                for name, template in prometheus_metrics.PER_CHIP_QUERY_TEMPLATES["ascend"].items()
            }
            if query == formatted["util"]:
                return [
                    {"labels": {"npu_id": "0"}, "value": 10.0},
                    {"labels": {"npu_id": "1"}, "value": 80.0},
                ]
            if query == formatted["used_mb"]:
                return [
                    {"labels": {"npu_id": "0"}, "value": 800.0},
                    {"labels": {"npu_id": "1"}, "value": 100.0},
                ]
            if query == formatted["total_mb"]:
                return [
                    {"labels": {"npu_id": "0"}, "value": 1000.0},
                    {"labels": {"npu_id": "1"}, "value": 1000.0},
                ]
            raise AssertionError(f"unexpected chip query: {query}")

        with (
            patch.object(prometheus_metrics, "resolve_accelerator_type", return_value="ascend"),
            patch.object(prometheus_metrics, "query_prom", new=AsyncMock(side_effect=fake_query_prom)),
            patch.object(prometheus_metrics, "query_prom_series", new=AsyncMock(side_effect=fake_query_prom_series)),
        ):
            metrics = await prometheus_metrics.fetch_metrics_from_prometheus("10.144.144.4")

        self.assertEqual(metrics["gpu_mem_used_mb"], 100.0)
        self.assertEqual(metrics["gpu_mem_total_mb"], 1000.0)
        self.assertEqual(metrics["gpu_util_percent"], 80.0)
        self.assertEqual(metrics["chips"][1]["chip_id"], "1")
        self.assertEqual(metrics["chips"][1]["used_mb"], 100.0)

    async def test_aggregate_metrics_are_kept_when_chip_metrics_are_unavailable(self) -> None:
        async def fake_query_prom(_client, query):
            for name, expected_query in prometheus_metrics.PROMETHEUS_QUERY_TEMPLATES["nvidia"].items():
                if expected_query.format(ip_regex="^10.144.144.6:.*") == query:
                    return {
                        "cpu": 11.0,
                        "mem": 22.0,
                        "gpu_util": 33.0,
                        "gpu_used": 444.0,
                        "gpu_free": 555.0,
                    }[name]
            raise AssertionError(f"unexpected aggregate query: {query}")

        with (
            patch.object(prometheus_metrics, "resolve_accelerator_type", return_value="nvidia"),
            patch.object(prometheus_metrics, "query_prom", new=AsyncMock(side_effect=fake_query_prom)),
            patch.object(prometheus_metrics, "query_prom_series", new=AsyncMock(return_value=[])),
        ):
            metrics = await prometheus_metrics.fetch_metrics_from_prometheus("10.144.144.6")

        self.assertEqual(metrics["gpu_mem_used_mb"], 444.0)
        self.assertEqual(metrics["gpu_mem_total_mb"], 999.0)
        self.assertEqual(metrics["gpu_util_percent"], 33.0)
        self.assertEqual(metrics["chips"], [])


if __name__ == "__main__":
    unittest.main()
