import argparse
import asyncio
import json
import sys

from app.db.database import SessionLocal
from app.models.models import Device
from app.services.algorithm_dispatcher import (
    build_algorithm_request_payload,
    derive_edge_storage_limit_gb_from_metrics,
)
from app.services.model_registry import MODEL_REGISTRY, canonicalize_model_type
from app.services.network_probe import get_network_metrics
from app.services.prometheus_metrics import get_prometheus_metrics
from app.services.runtime_dispatcher import extract_ip


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="采集当前环境指标并打印将要发送给算法模块的 JSON，请求不会真正发出。"
    )
    parser.add_argument("--model-type", default="Llama-3.2-3b", help="模型类型，默认 Llama-3.2-3b")
    parser.add_argument("--edge-device-id", default="edge_A", help="边端设备 ID，默认 edge_A")
    parser.add_argument("--cloud-device-id", default="cloud", help="云端设备 ID，默认 cloud")
    parser.add_argument("--edge-ip", default=None, help="可选，手动覆盖边端 IP")
    parser.add_argument("--cloud-ip", default=None, help="可选，手动覆盖云端 IP")
    return parser.parse_args()


async def build_preview_payload(args: argparse.Namespace) -> dict:
    model_type_key = args.model_type.lower()
    if model_type_key not in MODEL_REGISTRY:
        raise ValueError(f"不支持的模型类型: {args.model_type}")
    canonical_model_type = canonicalize_model_type(args.model_type)

    db = SessionLocal()
    try:
        edge_device = db.query(Device).filter(Device.id == args.edge_device_id).first()
        cloud_device = db.query(Device).filter(Device.id == args.cloud_device_id).first()
        if not edge_device:
            raise ValueError(f"未找到边端设备: {args.edge_device_id}")
        if not cloud_device:
            raise ValueError(f"未找到云端设备: {args.cloud_device_id}")

        edge_ip = args.edge_ip or extract_ip(edge_device.value)
        cloud_ip = args.cloud_ip or extract_ip(cloud_device.value)
        if not edge_ip:
            raise ValueError(f"无法从设备 {args.edge_device_id} 解析边端 IP")
        if not cloud_ip:
            raise ValueError(f"无法从设备 {args.cloud_device_id} 解析云端 IP")

        edge_metrics, cloud_metrics, network_metrics = await asyncio.gather(
            get_prometheus_metrics(edge_ip),
            get_prometheus_metrics(cloud_ip),
            get_network_metrics(edge_ip, cloud_ip),
        )
        edge_storage_limit_gb = derive_edge_storage_limit_gb_from_metrics(edge_metrics)

        return build_algorithm_request_payload(
            model_type=canonical_model_type,
            model_type_key=model_type_key,
            edge_metrics=edge_metrics,
            cloud_metrics=cloud_metrics,
            network_metrics=network_metrics,
            edge_storage_limit_gb=edge_storage_limit_gb,
        )
    finally:
        db.close()


async def main() -> int:
    args = parse_args()
    payload = await build_preview_payload(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"构造算法请求 JSON 失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
