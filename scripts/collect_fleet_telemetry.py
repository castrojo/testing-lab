#!/usr/bin/env python3
"""Collect evidence-backed fleet drift telemetry.

The collector is deliberately conservative: optional artifact/cache inputs are
used when present, while absent live sources produce explicit unavailable rows.
Historical files are append-only rolling snapshots and never seeded with
invented observations.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(".")
DATA = ROOT / "docs/data"
HISTORY = DATA / "history"
PAGES_URL = "https://factory.projectbluefin.io"
REPO_URL = "https://github.com/projectbluefin/lab"
RETENTION_DAYS = 180


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def evidence(path: str, collected_at: str, derivation: str) -> dict[str, str]:
    return {
        "source_url": f"{REPO_URL}/blob/main/{path}",
        "collected_at": collected_at,
        "derivation": derivation,
    }


def unavailable(reason: str, path: str, collected_at: str, derivation: str) -> dict[str, Any]:
    return {
        **evidence(path, collected_at, derivation),
        "state": "unavailable",
        "state_reason": reason,
    }


def available(path: str, collected_at: str, derivation: str) -> dict[str, str]:
    return {
        **evidence(path, collected_at, derivation),
        "state": "available",
        "state_reason": None,
    }


def append_history(path: Path, records: list[dict[str, Any]], collected_at: str) -> None:
    """Append new keyed observations and retain only the rolling window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    keys: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            recorded = record.get("recorded_at")
            if isinstance(recorded, str):
                try:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(recorded.replace("Z", "+00:00"))
                    if age.days > RETENTION_DAYS:
                        continue
                except ValueError:
                    continue
            key = json.dumps({k: record.get(k) for k in ("recorded_at", "lane", "version", "workload")}, sort_keys=True)
            keys.add(key)
            existing.append(record)
    for record in records:
        key = json.dumps({k: record.get(k) for k in ("recorded_at", "lane", "version", "workload")}, sort_keys=True)
        if key not in keys:
            existing.append(record)
            keys.add(key)
    path.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in existing), encoding="utf-8")


def load_inputs() -> tuple[dict[str, Any], str]:
    """Load repo-tracked snapshots and an optional artifact/cache payload."""
    sources = {
        "release": read_json(DATA / "release-verdict.json") or {},
        "upstream": read_json(DATA / "upstream-status.json") or {},
        "workloads": read_json(DATA / "app-resource-usage.json") or {},
    }
    artifact_path = os.environ.get("FLEET_TELEMETRY_ARTIFACT", "")
    candidates = [Path(artifact_path)] if artifact_path else [
        DATA / "artifacts/fleet-telemetry.json",
        DATA / "fleet-telemetry-input.json",
    ]
    for candidate in candidates:
        artifact = read_json(candidate)
        if artifact is not None:
            sources["artifact"] = artifact
            return sources, str(candidate)
    return sources, "unavailable"


def build_dataset(root: Path = ROOT, collected_at: str | None = None) -> dict[str, Any]:
    global DATA, HISTORY
    DATA, HISTORY = root / "docs/data", root / "docs/data/history"
    collected_at = collected_at or now_iso()
    sources, artifact_path = load_inputs()
    release_rows = sources["release"].get("rows") or []
    upstream_rows = sources["upstream"].get("rows") or []
    workload_rows = sources["workloads"].get("applications") or []
    artifact = sources.get("artifact") or {}

    versions = artifact.get("active_versions") if isinstance(artifact, dict) else None
    histogram: list[dict[str, Any]] = []
    active_history: list[dict[str, Any]] = []
    if isinstance(versions, list):
        for item in versions:
            if not isinstance(item, dict) or not item.get("version"):
                continue
            row = {
                "version": item["version"],
                "count": item.get("count") if isinstance(item.get("count"), int) else None,
                "lanes": item.get("lanes") if isinstance(item.get("lanes"), list) else [],
                **(available("docs/data/artifacts/fleet-telemetry.json", collected_at,
                    "Read active_versions from the repo-owned fleet telemetry artifact/cache export.")),
            }
            if row["count"] is None:
                row.update(state="unavailable", state_reason="Artifact listed a version without a numeric count.")
            histogram.append(row)
            active_history.append({"recorded_at": collected_at, "version": item["version"], "count": row["count"]})
    else:
        histogram.append({
            "version": None,
            "count": None,
            "lanes": [],
            **unavailable(
                "No fleet active-version artifact or cache export is available.",
                "docs/data/artifacts/fleet-telemetry.json",
                collected_at,
                "Expected a repo-owned artifact/cache export with active_versions[].",
            ),
        })

    digest_age_rows = []
    for row in release_rows:
        lane = row.get("lane") or row.get("id")
        finished = (row.get("build") or {}).get("finished_at")
        age_days = None
        if isinstance(finished, str):
            try:
                age_days = round((datetime.fromisoformat(collected_at.replace("Z", "+00:00")) -
                                  datetime.fromisoformat(finished.replace("Z", "+00:00"))).total_seconds() / 86400, 2)
            except ValueError:
                pass
        item = {"lane": lane, "digest": row.get("digest"), "digest_age_days": age_days}
        if row.get("digest") and age_days is not None:
            item.update(available("docs/data/release-verdict.json", collected_at,
                                  "Compute age from the latest release-verdict digest build finished_at."))
            item["stale"] = age_days > (7 if row.get("branch") in ("testing", "nightly") else 14)
        else:
            item.update(unavailable("Digest or publish completion timestamp is unavailable.",
                                    "docs/data/release-verdict.json", collected_at,
                                    "Release-verdict row lacks a digest or build finished_at."))
            item["stale"] = None
        digest_age_rows.append(item)

    lag_rows = []
    for row in upstream_rows:
        item = {"id": row.get("id"), "variant": row.get("variant"), "branch": row.get("branch"),
                "freshness_age_days": row.get("freshness_age_days"), "upstream_lag_days": row.get("freshness_age_days")}
        if isinstance(item["upstream_lag_days"], (int, float)):
            item.update(available("docs/data/upstream-status.json", collected_at,
                                  "Use upstream-status freshness_age_days as the published upstream lag signal."))
        else:
            item.update(unavailable("Upstream publish timestamp is unavailable.",
                                    "docs/data/upstream-status.json", collected_at,
                                    "Upstream-status row has no freshness_age_days value."))
        lag_rows.append(item)

    health_rows = []
    for row in workload_rows:
        pods = row.get("pods_count")
        item = {"workload": row.get("name"), "pods_count": pods}
        if isinstance(pods, int):
            item.update(available("docs/data/app-resource-usage.json", collected_at,
                                  "Read workload pod count from the repo-tracked Kubernetes resource snapshot."))
            item["health"] = "healthy" if pods > 0 else "empty"
        else:
            item.update(unavailable("Kubernetes workload snapshot has no pod count.",
                                    "docs/data/app-resource-usage.json", collected_at,
                                    "Expected applications[].pods_count from the Kubernetes collector."))
            item["health"] = None
        health_rows.append(item)

    append_history(HISTORY / "active-versions.ndjson", active_history, collected_at)
    append_history(HISTORY / "digest-age.ndjson", [
        {"recorded_at": collected_at, "lane": row["lane"], "digest_age_days": row["digest_age_days"]}
        for row in digest_age_rows if row["digest_age_days"] is not None
    ], collected_at)
    append_history(HISTORY / "upstream-lag.ndjson", [
        {"recorded_at": collected_at, "lane": row["id"], "upstream_lag_days": row["upstream_lag_days"]}
        for row in lag_rows if row["state"] == "available"
    ], collected_at)
    append_history(HISTORY / "workload-health.ndjson", [
        {"recorded_at": collected_at, "workload": row["workload"], "health": row["health"], "pods_count": row["pods_count"]}
        for row in health_rows if row["state"] == "available"
    ], collected_at)

    source = artifact_path if artifact_path != "unavailable" else "docs/data/release-verdict.json"
    return {
        "schema_version": "v1",
        "_meta": {
            "page": "index",
            "description": "Historical fleet drift telemetry from release, upstream, Kubernetes, and optional artifact evidence.",
            "generated_at": collected_at,
            "status": "live" if artifact_path != "unavailable" or release_rows or upstream_rows or workload_rows else "unavailable",
            "source": source,
        },
        "summary_metrics": [
            {"id": "active_versions", "label": "Active versions", "value": len([r for r in histogram if r.get("state") == "available"]),
             **evidence("docs/data/history/active-versions.ndjson", collected_at, "Count available active-version histogram buckets.")},
            {"id": "stale_digests", "label": "Stale digests", "value": sum(1 for r in digest_age_rows if r.get("stale") is True),
             **evidence("docs/data/release-verdict.json", collected_at, "Count digest-age rows beyond branch freshness threshold.")},
            {"id": "unhealthy_workloads", "label": "Unhealthy workloads", "value": sum(1 for r in health_rows if r.get("health") not in (None, "healthy")),
             **evidence("docs/data/app-resource-usage.json", collected_at, "Count workloads without a positive pod snapshot.")},
        ],
        "active_version_histogram": histogram,
        "digest_age": digest_age_rows,
        "upstream_lag": lag_rows,
        "workload_health": health_rows,
        "history": {
            "active_versions": "data/history/active-versions.ndjson",
            "digest_age": "data/history/digest-age.ndjson",
            "upstream_lag": "data/history/upstream-lag.ndjson",
            "workload_health": "data/history/workload-health.ndjson",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    output = args.root / "docs/data/fleet-telemetry.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_dataset(args.root), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
