import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "collect_bst_cache", ROOT / "scripts/collect_bst_cache.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_extracts_buildbarn_operation_counts_bytes_and_duration():
    metrics = """
buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum{storage_type="cas",backend_type="local",operation="Get"} 120
buildbarn_blobstore_blob_access_operations_blob_size_bytes_count{storage_type="cas",backend_type="local",operation="Get"} 3
buildbarn_blobstore_blob_access_operations_duration_seconds_sum{storage_type="cas",backend_type="local",operation="Get",grpc_code="OK"} 1.5
buildbarn_blobstore_blob_access_operations_duration_seconds_count{storage_type="cas",backend_type="local",operation="Get",grpc_code="OK"} 3
buildbarn_blobstore_blob_access_operations_duration_seconds_count{storage_type="cas",backend_type="local",operation="Get",grpc_code="NotFound"} 1
"""

    row = MODULE.collect_cache_heat(metrics, "storage-0", "cas")

    assert row["requests"] == 4
    assert row["bytes"] == 120
    assert row["duration_seconds"] == 1.5
    assert row["latency_ms"] == 375.0
    assert row["logical_fill_bytes"] is None
    assert row["logical_fill_percent"] is None
    assert row["hit_count"] == 3
    assert row["miss_count"] == 1
    assert row["effectiveness"] == 0.75
    assert row["state"] == "available"


def test_appends_cache_heat_history_without_inventing_hit_metrics(tmp_path, monkeypatch):
    history = tmp_path / "cache-heat.ndjson"
    monkeypatch.setattr(MODULE, "HISTORY_PATH", str(history))

    MODULE.persist_cache_heat(
        [
            {
                "cache_backend": "buildbarn",
                "storage_type": "cas",
                "hit_count": None,
                "miss_count": None,
                "effectiveness": None,
                "latency_ms": None,
                "logical_fill_bytes": None,
                "logical_capacity_bytes": None,
                "logical_fill_percent": None,
                "logical_fill_state": "unavailable",
                "state": "unavailable",
                "state_reason": "hit/miss metrics are not exposed by BuildBarn",
            }
        ],
        "2026-07-29T00:00:00Z",
    )

    record = json.loads(history.read_text().splitlines()[0])
    assert record["schema_version"] == "1.0"
    assert record["recorded_at"] == "2026-07-29T00:00:00Z"
    assert record["hit_count"] is None
    assert record["miss_count"] is None
    assert record["effectiveness"] is None


def test_cache_heat_carries_logical_fill_provenance():
    row = MODULE.collect_cache_heat(
        "",
        "storage-0",
        "cas",
        logical_fill_bytes=50,
        logical_capacity_bytes=200,
        logical_fill_state="available",
        logical_fill_reason=None,
    )

    assert row["logical_fill_bytes"] == 50
    assert row["logical_capacity_bytes"] == 200
    assert row["logical_fill_percent"] == 25.0
    assert row["logical_fill_state"] == "available"
    assert "allocator counters" in row["derivation"]


def test_usb4_telemetry_does_not_invent_distributed_build_measurements(monkeypatch):
    docs = {
        "/api/v1/nodes/ghost": {
            "metadata": {
                "annotations": {
                    "lab.projectbluefin.io/usb4-link": "up",
                    "lab.projectbluefin.io/usb4-link-observed-at": "2026-07-29T00:00:00Z",
                }
            }
        },
        "/api/v1/nodes/exo-0": {
            "metadata": {
                "annotations": {
                    "lab.projectbluefin.io/usb4-link": "up",
                    "lab.projectbluefin.io/usb4-link-observed-at": "2026-07-29T00:00:00Z",
                }
            }
        },
    }
    monkeypatch.setattr(MODULE, "run_json_raw", docs.get)

    telemetry = MODULE.collect_usb4_telemetry("2026-07-29T00:01:00Z")

    assert telemetry["status"] == "up"
    assert telemetry["ghost_link"] == "up"
    assert telemetry["exo0_link"] == "up"
    assert telemetry["bandwidth_gbps"] is None
    assert telemetry["latency_ms"] is None
    assert telemetry["cold_build_duration_min"] is None
    assert telemetry["warm_build_duration_min"] is None
    assert telemetry["speedup_ratio"] is None
    assert telemetry["rechunk_duration_sec"] is None
    assert telemetry["work_distribution"] == {"ghost": None, "exo-0": None}
    assert telemetry["state"] == "unavailable"
    assert telemetry["source_url"]
    assert telemetry["collected_at"] == "2026-07-29T00:01:00Z"
    assert telemetry["derivation"]
    assert telemetry["state_reason"]


def test_usb4_telemetry_is_unavailable_without_measured_link_evidence(monkeypatch):
    monkeypatch.setattr(MODULE, "run_json_raw", lambda path: None)

    telemetry = MODULE.collect_usb4_telemetry("2026-07-29T00:01:00Z")

    assert telemetry["status"] == "unavailable"
    assert telemetry["ghost_link"] is None
    assert telemetry["exo0_link"] is None
    assert telemetry["state"] == "unavailable"
