#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / "backend" / ".env")

DEFAULT_EDGE_IP = os.getenv("EDGE_DEVICE_IP", "10.144.144.3")
DEFAULT_PORT = int(os.getenv("IPERF3_TEST_PORT", "5201"))
DEFAULT_DURATION = int(os.getenv("IPERF3_TEST_DURATION_SECONDS", "3"))
DEFAULT_TIMEOUT = int(os.getenv("IPERF3_TEST_TIMEOUT_SECONDS", str(DEFAULT_DURATION + 10)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure edge-cloud bandwidth with iperf3.")
    parser.add_argument("--edge-ip", default=DEFAULT_EDGE_IP, help="Edge device iperf3 server IP.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="iperf3 server port.")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION, help="Test duration in seconds.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Subprocess timeout in seconds.")
    parser.add_argument("--no-save", action="store_true", help="Do not save result to json.")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "tests" / "real_iperf_bandwidth_probe_result.json"),
        help="Path to save JSON result.",
    )
    return parser.parse_args()


def fail(message: str, code: int = 1, detail: str = "") -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    if detail:
        print(detail.strip(), file=sys.stderr)
    raise SystemExit(code)


def local_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def first_dict(value):
    return value if isinstance(value, dict) else {}


def pick_summary(payload: dict) -> tuple[dict, str]:
    end = first_dict(payload.get("end"))
    candidates = [
        (first_dict(end.get("sum_received")), "sum_received"),
        (first_dict(end.get("sum_sent")), "sum_sent"),
        (first_dict(end.get("sum")), "sum"),
    ]
    for summary, source in candidates:
        bps = summary.get("bits_per_second")
        if isinstance(bps, (int, float)) and bps > 0:
            return summary, source
    return {}, "missing"


def run_iperf(target: str, port: int, duration: int, timeout: int, reverse: bool) -> dict:
    iperf3 = shutil.which("iperf3")
    if not iperf3:
        fail("iperf3 is not installed or not in PATH.", 127)

    command = [iperf3, "-c", target, "-p", str(port), "-t", str(duration), "--json"]
    if reverse:
        command.insert(-1, "-R")

    started = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail(f"iperf3 timed out after {timeout}s.", 124, detail=" ".join(command))

    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        fail(
            f"iperf3 failed with exit code {proc.returncode}.",
            proc.returncode,
            detail=proc.stderr or proc.stdout,
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        fail("failed to parse iperf3 JSON output.", 3, detail=f"{exc}\n{proc.stdout[:1000]}")

    summary, source = pick_summary(payload)
    if not summary:
        fail("iperf3 JSON did not contain a positive bits_per_second value.", 3)

    start = first_dict(payload.get("start"))
    connected = start.get("connected") if isinstance(start.get("connected"), list) else []
    connection = connected[0] if connected and isinstance(connected[0], dict) else {}

    bps = float(summary["bits_per_second"])
    local_host = connection.get("local_host") or local_hostname()
    remote_host = connection.get("remote_host") or target
    direction = f"{remote_host} -> {local_host}" if reverse else f"{local_host} -> {remote_host}"

    return {
        "mode": "reverse" if reverse else "normal",
        "direction": direction,
        "target": target,
        "port": port,
        "duration_seconds_requested": duration,
        "duration_seconds_measured": round(float(summary.get("seconds") or duration), 3),
        "elapsed_seconds": round(elapsed, 3),
        "bytes_transferred": int(summary.get("bytes") or 0),
        "bits_per_second": bps,
        "bandwidth_mbps": round(bps / 1_000_000, 2),
        "summary_source": source,
    }


def main() -> int:
    args = parse_args()
    print("=== iperf3 edge-cloud bandwidth probe ===")
    print(f"edge_ip: {args.edge_ip}")
    print(f"port: {args.port}")
    print(f"duration: {args.duration}s")

    cloud_to_edge = run_iperf(args.edge_ip, args.port, args.duration, args.timeout, reverse=False)
    edge_to_cloud = run_iperf(args.edge_ip, args.port, args.duration, args.timeout, reverse=True)

    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cloud_to_edge": cloud_to_edge,
        "edge_to_cloud": edge_to_cloud,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not args.no_save:
        output_path = Path(os.path.expanduser(args.output))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved_json: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
