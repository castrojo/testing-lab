#!/usr/bin/env python3
"""Validate and consume bootc update evidence emitted by an Argo run."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HISTORY_PATH = Path("docs/data/history/boot-update.ndjson")
DATASET_PATH = Path("docs/data/boot-update-telemetry.json")
SOURCE_URL = "https://github.com/projectbluefin/lab/blob/main/docs/data/history/boot-update.ndjson"
RETENTION_DAYS = 180

REQUIRED = {
    "schema_version",
    "recorded_at",
    "workflow_name",
    "lane",
    "bootc_status",
    "deployment",
    "update",
    "rollback",
    "post_update_boot",
}


class RecordError(ValueError):
    """A telemetry artifact does not satisfy the durable contract."""


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RecordError(f"{field} must be boolean")
    return value


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    missing = REQUIRED - record.keys()
    if missing:
        raise RecordError(f"missing fields: {', '.join(sorted(missing))}")
    if record["schema_version"] != "1.0":
        raise RecordError("schema_version must be 1.0")
    if not isinstance(record["recorded_at"], str) or not record["recorded_at"]:
        raise RecordError("recorded_at must be a non-empty ISO8601 string")
    if not isinstance(record["workflow_name"], str) or not record["workflow_name"]:
        raise RecordError("workflow_name must be a non-empty string")
    if not isinstance(record["lane"], str) or not record["lane"]:
        raise RecordError("lane must be a non-empty string")

    deployment = record["deployment"]
    if not isinstance(deployment, dict):
        raise RecordError("deployment must be an object")
    for field in ("image", "digest"):
        if deployment.get(field) is not None and not isinstance(deployment[field], str):
            raise RecordError(f"deployment.{field} must be a string or null")
    status = record["bootc_status"]
    if not isinstance(status, dict):
        raise RecordError("bootc_status must be an object")
    _bool(status.get("observed"), "bootc_status.observed")

    for section in ("update", "rollback", "post_update_boot"):
        value = record[section]
        if not isinstance(value, dict):
            raise RecordError(f"{section} must be an object")
        _bool(value.get("succeeded"), f"{section}.succeeded")
        if value.get("evidence") is not None and not isinstance(value["evidence"], str):
            raise RecordError(f"{section}.evidence must be a string or null")

    return record


def append_record(history: Path, record: dict[str, Any]) -> bool:
    record = normalize_record(record)
    history.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    keys: set[tuple[str, str]] = set()
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing = normalize_record(json.loads(line))
            except (json.JSONDecodeError, RecordError):
                continue
            records.append(existing)
            keys.add((existing["workflow_name"], existing["lane"]))
    key = (record["workflow_name"], record["lane"])
    if key not in keys:
        records.append(record)
        keys.add(key)
        added = True
    else:
        records = [
            record if (item["workflow_name"], item["lane"]) == key else item
            for item in records
        ]
        added = False
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    retained = []
    for item in records:
        try:
            when = datetime.fromisoformat(item["recorded_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff:
            retained.append(item)
    history.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in retained),
        encoding="utf-8",
    )
    return added


def build_dataset(history: Path, collected_at: str) -> dict[str, Any]:
    rows = []
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(normalize_record(json.loads(line)))
            except (json.JSONDecodeError, RecordError):
                continue
    rows.sort(key=lambda row: row["recorded_at"], reverse=True)
    return {
        "schema_version": "1.0",
        "_meta": {
            "page": "boot-update",
            "description": "Bootc deployment and update evidence from ephemeral lab VMs.",
            "generated_at": collected_at,
            "starter_artifact": False,
            "status": "ready" if rows else "unavailable",
        },
        "summary_metrics": [
            {
                "id": "runs",
                "value": len(rows) if rows else None,
                "source_url": SOURCE_URL,
                "collected_at": collected_at,
                "derivation": "Count valid rows in boot-update.ndjson.",
                "state": "available" if rows else "unavailable",
                "state_reason": None if rows else "boot-update.ndjson has no valid telemetry records.",
            },
        ],
        "rows": [
            {
                **row,
                "source_url": SOURCE_URL,
                "collected_at": collected_at,
                "derivation": "Consumed from the Argo bootc telemetry artifact without synthesizing missing values.",
                "state": "available",
                "state_reason": None,
            }
            for row in rows
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    append = commands.add_parser("append")
    append.add_argument("artifact", type=Path)
    append.add_argument("--history", type=Path, default=HISTORY_PATH)
    dataset = commands.add_parser("dataset")
    dataset.add_argument("--history", type=Path, default=HISTORY_PATH)
    dataset.add_argument("--output", type=Path, default=DATASET_PATH)
    args = parser.parse_args(argv)
    if args.command == "append":
        append_record(args.history, json.loads(args.artifact.read_text(encoding="utf-8")))
    else:
        output = build_dataset(args.history, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
