#!/usr/bin/env python3
"""Build an evidence-backed USB-4 execution heatmap dataset.

The collector intentionally preserves nulls.  Link state is not execution
telemetry, and a missing workflow output must remain unavailable.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

NODES = ("ghost", "exo-0")
WINDOW_DAYS = 7
HISTORY_PATH = Path("docs/data/history/build-runs.ndjson")
OUTPUT_PATH = Path("docs/data/usb4-execution.json")
SOURCE_URL = "https://github.com/projectbluefin/lab/blob/main/docs/data/history/build-runs.ndjson"


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def load_records(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _node_rows(record: dict[str, Any], collected_at: str) -> list[dict[str, Any]]:
    telemetry = record.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    source_url = record.get("run_url") if isinstance(record.get("run_url"), str) else SOURCE_URL
    timestamp = record.get("started_at")
    rows = []
    for node in NODES:
        prefix = "ghost_" if node == "ghost" else "exo0_"
        values = {
            "throughput_bytes_per_second": telemetry.get(f"{prefix}throughput_bytes_per_second"),
            "latency_ms": telemetry.get("usb4_latency_ms"),
            "action_count": telemetry.get(f"{prefix}action_count"),
            "cache_result": telemetry.get(f"{prefix}cache_result"),
            "cache_hit_count": telemetry.get(f"{prefix}cache_hit_count"),
            "cache_miss_count": telemetry.get(f"{prefix}cache_miss_count"),
            "temperature": telemetry.get("cache_temperature"),
        }
        measured = any(value is not None for value in values.values())
        rows.append(
            {
                "id": f"{record.get('run_id', 'unknown')}-{node}",
                "node": node,
                "timestamp": timestamp,
                "throughput": values["throughput_bytes_per_second"],
                "throughput_unit": "bytes_per_second",
                "latency_ms": values["latency_ms"],
                "action_count": values["action_count"],
                "cache_result": values["cache_result"],
                "cache_hit_count": values["cache_hit_count"],
                "cache_miss_count": values["cache_miss_count"],
                "temperature": values["temperature"],
                "state": "available" if measured else "unavailable",
                "state_reason": None
                if measured
                else "Workflow telemetry output did not include USB-4 execution measurements.",
                "source_url": source_url,
                "collected_at": collected_at,
                "derivation": (
                    "Read bounded USB-4 execution fields from the validated workflow "
                    "telemetry output parameter; no values are inferred from link state."
                ),
            }
        )
    return rows


def build_dataset(
    records: list[dict[str, Any]],
    *,
    collected_at: str,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    collected = parse_timestamp(collected_at)
    if collected is None:
        raise ValueError("collected_at must be timezone-aware ISO8601")
    start = collected - timedelta(days=window_days)
    selected = []
    for record in records:
        started = parse_timestamp(record.get("started_at"))
        if started is None or not start <= started <= collected:
            continue
        if record.get("lane") not in {None, "dakota-testing", "dakota-build-pipeline"}:
            continue
        selected.append(record)
    rows = [row for record in selected for row in _node_rows(record, collected_at)]
    return {
        "schema_version": "1",
        "_meta": {
            "page": "usb4-execution",
            "description": "USB-4 distributed execution evidence for ghost and exo-0.",
            "generated_at": collected_at,
            "status": "ready"
            if any(row["state"] == "available" for row in rows)
            else "unavailable",
            "state_reason": None
            if any(row["state"] == "available" for row in rows)
            else "No measured USB-4 workflow telemetry records fall in the UTC window.",
            "window_start": start.isoformat().replace("+00:00", "Z"),
            "window_end": collected_at,
            "window_timezone": "UTC",
        },
        "nodes": list(NODES),
        "window_days": window_days,
        "summary_metrics": [
            {
                "id": "execution_records",
                "label": "Execution telemetry records",
                "value": sum(row["state"] == "available" for row in rows) or None,
                "unit": "records",
                "source_url": SOURCE_URL,
                "collected_at": collected_at,
                "derivation": "Count rows with at least one measured USB-4 execution field.",
                "state": "available" if any(row["state"] == "available" for row in rows) else "unavailable",
                "state_reason": None if any(row["state"] == "available" for row in rows) else "No measured USB-4 fields were published.",
            }
        ],
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=Path, default=HISTORY_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--collected-at")
    args = parser.parse_args()
    collected_at = args.collected_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    dataset = build_dataset(load_records(args.history), collected_at=collected_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
