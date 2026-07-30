#!/usr/bin/env python3
"""Build historical, evidence-backed security telemetry.

This collector joins only artifacts already published by the lab:
``cve-summary.ndjson`` and ``release-verdict.ndjson``.  It deliberately does
not infer patch latency, SBOM coverage, or provenance-attestation coverage from
publisher capability flags or from a successful signature check.  Those
metrics remain explicitly unavailable until a workflow publishes the required
artifact evidence.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "docs/data/history"
OUT_PATH = ROOT / "docs/data/security-telemetry.json"

SEVERITIES = ("critical", "high", "medium", "low", "negligible", "unknown")
SOURCE_CVE = "https://github.com/projectbluefin/lab/blob/main/docs/data/history/cve-summary.ndjson"
SOURCE_VERDICT = "https://github.com/projectbluefin/lab/blob/main/docs/data/history/release-verdict.ndjson"
SOURCE_CVE_ARTIFACT = "https://github.com/projectbluefin/lab/blob/main/docs/data/cve-summary.json"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _state(reason: str | None, source_url: str | None, collected_at: str, derivation: str) -> dict[str, Any]:
    return {
        "state": "available" if reason is None else "unavailable",
        "state_reason": reason,
        "source_url": source_url,
        "collected_at": collected_at,
        "derivation": derivation,
    }


def cve_history(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if "state" in row and row.get("state") != "available":
            continue
        counts = row.get("severity_counts")
        if not isinstance(counts, dict):
            counts = {severity: row.get(severity) for severity in SEVERITIES}
        counts = {severity: counts.get(severity) for severity in SEVERITIES}
        if not row.get("recorded_at") or not row.get("lane"):
            continue
        result.append(
            {
                "recorded_at": row["recorded_at"],
                "lane": row["lane"],
                "digest": row.get("digest"),
                "severity_counts": counts,
                "fixable": row.get("fixable"),
                "total": row.get("total"),
                "sbom": row.get("sbom"),
                "provenance": row.get("provenance"),
                **_state(
                    None,
                    SOURCE_CVE,
                    row["recorded_at"],
                    "Existing cve-summary.ndjson severity counts grouped by scan time and lane.",
                ),
            }
        )
    return result


def signature_history(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_time: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "failed": 0, "unavailable": 0}
    )
    for row in rows:
        status = row.get("signature")
        recorded_at = row.get("recorded_at")
        if recorded_at:
            if status in ("passed", "failed"):
                by_time[recorded_at][status] += 1
            else:
                by_time[recorded_at]["unavailable"] += 1
    result = []
    for recorded_at in sorted(by_time):
        counts = by_time[recorded_at]
        total = counts["passed"] + counts["failed"] + counts["unavailable"]
        result.append(
            {
                "recorded_at": recorded_at,
                "passed": counts["passed"],
                "failed": counts["failed"],
                "unavailable": counts["unavailable"],
                "known": counts["passed"] + counts["failed"],
                "total": total,
                "completeness": counts["passed"] / total if total else None,
                **_state(
                    None if total and not counts["unavailable"] else "release-verdict history contains incomplete signature outcomes",
                    SOURCE_VERDICT,
                    recorded_at,
                    "Release-verdict history signature outcomes; unavailable/pending outcomes remain in the denominator.",
                ),
            }
        )
    return result


def _artifact_status(row: dict[str, Any], name: str) -> str | None:
    """Read an explicitly published artifact outcome; never infer one."""
    artifact = row.get(name)
    if isinstance(artifact, dict):
        status = artifact.get("status") or artifact.get("state")
        return status if status in ("available", "unavailable", "failed", "pending") else None
    status = row.get(f"{name}_status")
    return status if status in ("available", "unavailable", "failed", "pending") else None


def artifact_history(
    rows: Iterable[dict[str, Any]], name: str, label: str
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"available": 0, "unavailable": 0, "failed": 0, "pending": 0}
    )
    for row in rows:
        recorded_at = row.get("recorded_at") or row.get("collected_at")
        status = _artifact_status(row, name)
        if recorded_at and status:
            buckets[recorded_at][status] += 1

    result = []
    for recorded_at in sorted(buckets):
        counts = buckets[recorded_at]
        known = counts["available"] + counts["unavailable"] + counts["failed"]
        result.append(
            {
                "recorded_at": recorded_at,
                "available": counts["available"],
                "unavailable": counts["unavailable"],
                "failed": counts["failed"],
                "pending": counts["pending"],
                "known": known,
                "completeness": (
                    counts["available"] / known if known else None
                ),
                **_state(
                    None
                    if counts["available"] or counts["failed"]
                    else f"{label} artifact outcomes are explicitly unavailable or pending",
                    SOURCE_CVE_ARTIFACT,
                    recorded_at,
                    f"Published {name} artifact outcomes; capability flags and signatures are not used as evidence.",
                ),
            }
        )
    return result


def unavailable_metric(metric_id: str, reason: str, collected_at: str) -> dict[str, Any]:
    return {
        "id": metric_id,
        "value": None,
        "history": [],
        **_state(reason, None, collected_at, "No supported workflow/artifact evidence is published yet."),
    }


def collect(root: Path = ROOT, collected_at: str | None = None) -> dict[str, Any]:
    collected_at = collected_at or now_iso()
    cve_rows = cve_history(read_ndjson(root / "docs/data/history/cve-summary.ndjson"))
    verdict_rows = read_ndjson(root / "docs/data/history/release-verdict.ndjson")
    signature_rows = signature_history(verdict_rows)
    sbom_rows = artifact_history(cve_rows, "sbom", "SBOM")
    provenance_rows = artifact_history(cve_rows, "provenance", "Provenance attestation")

    cve_metric = {
        "id": "cve_counts_by_severity",
        "value": cve_rows[-1] if cve_rows else None,
        "history": cve_rows,
        **_state(
            None if cve_rows else "cve-summary.ndjson has no valid scan records",
            SOURCE_CVE if cve_rows else None,
            collected_at,
            "Existing cve-summary.ndjson scan records, preserving null fields and scan provenance.",
        ),
    }
    signature_metric = {
        "id": "signature_completeness",
        "value": signature_rows[-1] if signature_rows else None,
        "history": signature_rows,
        **_state(
            None if signature_rows else "release-verdict.ndjson has no signature outcomes",
            SOURCE_VERDICT if signature_rows else None,
            collected_at,
            "Existing release-verdict.ndjson signature outcomes grouped by collection time.",
        ),
    }

    metrics = [
        cve_metric,
        unavailable_metric(
            "time_to_patch",
            "No per-CVE first-seen and fixed/resolved timestamps are published by the current grype workflow.",
            collected_at,
        ),
        {
            "id": "sbom_coverage",
            "value": sbom_rows[-1] if sbom_rows else None,
            "history": sbom_rows,
            **_state(
                None
                if sbom_rows and any(r["available"] for r in sbom_rows)
                else "No historical SBOM artifact evidence is published; publisher capability flags are not evidence.",
                SOURCE_CVE_ARTIFACT if sbom_rows else None,
                collected_at,
                "Published SBOM artifact outcomes keyed to scan records; no inference from image metadata or signatures.",
            ),
        },
        signature_metric,
        {
            "id": "provenance_attestations",
            "value": provenance_rows[-1] if provenance_rows else None,
            "history": provenance_rows,
            **_state(
                None
                if provenance_rows and any(r["available"] for r in provenance_rows)
                else "No historical provenance-attestation artifact evidence is published; signatures do not prove provenance attestations.",
                SOURCE_CVE_ARTIFACT if provenance_rows else None,
                collected_at,
                "Published provenance-attestation outcomes keyed to scan records; signatures are not treated as provenance evidence.",
            ),
        },
    ]
    available = sum(metric["state"] == "available" for metric in metrics)
    return {
        "schema_version": "1.0",
        "_meta": {
            "page": "security",
            "description": "Historical security evidence from lab-published workflow artifacts.",
            "generated_at": collected_at,
            "status": "live" if available else "unavailable",
        },
        "summary_metrics": metrics,
    }


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(collect(), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
