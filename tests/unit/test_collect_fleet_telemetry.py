import json
from pathlib import Path

from scripts.collect_fleet_telemetry import build_dataset


def write_json(root: Path, name: str, value: dict):
    path = root / "docs/data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_missing_artifact_is_explicit_and_does_not_seed_history(tmp_path):
    write_json(tmp_path, "release-verdict.json", {"rows": []})
    write_json(tmp_path, "upstream-status.json", {"rows": []})
    write_json(tmp_path, "app-resource-usage.json", {"applications": []})

    dataset = build_dataset(tmp_path, "2026-07-29T00:00:00Z")

    row = dataset["active_version_histogram"][0]
    assert row["count"] is None
    assert row["state"] == "unavailable"
    assert not (tmp_path / "docs/data/history/active-versions.ndjson").read_text()


def test_builds_historical_rows_from_real_snapshots(tmp_path):
    write_json(tmp_path, "release-verdict.json", {
        "rows": [{
            "lane": "bluefin-testing",
            "branch": "testing",
            "digest": "sha256:abc",
            "build": {"finished_at": "2026-07-27T00:00:00Z"},
        }]
    })
    write_json(tmp_path, "upstream-status.json", {
        "rows": [{
            "id": "bluefin-testing",
            "variant": "bluefin",
            "branch": "testing",
            "freshness_age_days": 2,
        }]
    })
    write_json(tmp_path, "app-resource-usage.json", {
        "applications": [{"name": "argo", "pods_count": 2}]
    })
    write_json(tmp_path, "fleet-telemetry-input.json", {
        "active_versions": [{"version": "41.20260727", "count": 3, "lanes": ["bluefin-testing"]}]
    })

    dataset = build_dataset(tmp_path, "2026-07-29T00:00:00Z")

    assert dataset["active_version_histogram"][0]["state"] == "available"
    assert dataset["digest_age"][0]["digest_age_days"] == 2
    assert dataset["upstream_lag"][0]["upstream_lag_days"] == 2
    assert dataset["workload_health"][0]["health"] == "healthy"
    assert json.loads((tmp_path / "docs/data/history/workload-health.ndjson").read_text().splitlines()[0])["pods_count"] == 2
