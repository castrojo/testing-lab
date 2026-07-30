import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect_security_telemetry as telemetry  # noqa: E402


def test_collects_cve_severity_history_and_signature_completeness(tmp_path):
    history = tmp_path / "docs/data/history"
    history.mkdir(parents=True)
    (history / "cve-summary.ndjson").write_text(
        json.dumps(
            {
                "recorded_at": "2026-07-28T00:00:00Z",
                "lane": "bluefin-testing",
                "digest": "sha256:a",
                "critical": 1,
                "high": 2,
                "medium": 3,
                "low": 4,
                "total": 10,
            }
        )
        + "\n"
    )
    (history / "release-verdict.ndjson").write_text(
        "\n".join(
            [
                json.dumps({"recorded_at": "2026-07-28T00:00:00Z", "lane": "bluefin-testing", "signature": "passed"}),
                json.dumps({"recorded_at": "2026-07-28T00:00:00Z", "lane": "dakota-testing", "signature": "failed"}),
            ]
        )
        + "\n"
    )

    document = telemetry.collect(tmp_path, "2026-07-29T00:00:00Z")
    metrics = {metric["id"]: metric for metric in document["summary_metrics"]}

    assert metrics["cve_counts_by_severity"]["state"] == "available"
    assert metrics["cve_counts_by_severity"]["history"][0]["severity_counts"]["critical"] == 1
    assert metrics["cve_counts_by_severity"]["history"][0]["severity_counts"]["unknown"] is None
    assert metrics["signature_completeness"]["value"]["completeness"] == 0.5


def test_does_not_infer_unavailable_security_metrics(tmp_path):
    document = telemetry.collect(tmp_path, "2026-07-29T00:00:00Z")
    metrics = {metric["id"]: metric for metric in document["summary_metrics"]}

    for metric_id in ("time_to_patch", "sbom_coverage", "provenance_attestations"):
        assert metrics[metric_id]["state"] == "unavailable"
        assert metrics[metric_id]["value"] is None
        assert metrics[metric_id]["history"] == []
        assert metrics[metric_id]["state_reason"]


def test_uses_published_artifact_outcomes_without_inference(tmp_path):
    history = tmp_path / "docs/data/history"
    history.mkdir(parents=True)
    (history / "cve-summary.ndjson").write_text(
        json.dumps(
            {
                "recorded_at": "2026-07-28T00:00:00Z",
                "lane": "bluefin-testing",
                "digest": "sha256:a",
                "severity_counts": {"critical": 1},
                "state": "available",
                "sbom": {
                    "status": "available",
                    "source_url": "https://example.invalid/sbom.json",
                },
                "provenance": {"status": "unavailable"},
            }
        )
        + "\n"
    )
    (history / "release-verdict.ndjson").write_text("")

    metrics = {
        metric["id"]: metric
        for metric in telemetry.collect(tmp_path, "2026-07-29T00:00:00Z")["summary_metrics"]
    }
    assert metrics["cve_counts_by_severity"]["state"] == "available"
    assert metrics["sbom_coverage"]["state"] == "available"
    assert metrics["sbom_coverage"]["value"]["completeness"] == 1
    assert metrics["provenance_attestations"]["state"] == "unavailable"
    assert "signatures" in metrics["provenance_attestations"]["state_reason"]


def test_skips_unavailable_cve_scan_rows(tmp_path):
    history = tmp_path / "docs/data/history"
    history.mkdir(parents=True)
    (history / "cve-summary.ndjson").write_text(
        json.dumps(
            {
                "recorded_at": "2026-07-28T00:00:00Z",
                "lane": "bluefin-testing",
                "digest": None,
                "state": "unavailable",
                "critical": 99,
            }
        )
        + "\n"
    )
    metrics = {
        metric["id"]: metric
        for metric in telemetry.collect(tmp_path, "2026-07-29T00:00:00Z")["summary_metrics"]
    }
    assert metrics["cve_counts_by_severity"]["state"] == "unavailable"
