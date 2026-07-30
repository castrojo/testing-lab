import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "collect_usb4_execution", ROOT / "scripts/collect_usb4_execution.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_build_dataset_preserves_unavailable_fields_and_utc_window():
    dataset = MODULE.build_dataset(
        [
            {
                "lane": "dakota-testing",
                "run_id": "run-1",
                "started_at": "2026-07-29T12:00:00Z",
                "run_url": "https://example.invalid/run-1",
                "telemetry": {
                    "ghost_throughput_bytes_per_second": 1000,
                    "usb4_latency_ms": 2,
                    "ghost_action_count": 4,
                    "ghost_cache_result": "hit",
                    "cache_temperature": "warm",
                },
            }
        ],
        collected_at="2026-07-29T13:00:00Z",
    )

    assert dataset["_meta"]["window_timezone"] == "UTC"
    assert dataset["_meta"]["window_start"] == "2026-07-22T13:00:00Z"
    ghost = next(row for row in dataset["rows"] if row["node"] == "ghost")
    exo = next(row for row in dataset["rows"] if row["node"] == "exo-0")
    assert ghost["state"] == "available"
    assert ghost["throughput"] == 1000
    assert ghost["cache_result"] == "hit"
    assert exo["state"] == "available"
    assert exo["throughput"] is None
    assert exo["action_count"] is None


def test_exporter_uses_only_bounded_node_labels():
    exporter_spec = importlib.util.spec_from_file_location(
        "usb4_execution_exporter", ROOT / "scripts/usb4_execution_exporter.py"
    )
    exporter = importlib.util.module_from_spec(exporter_spec)
    exporter_spec.loader.exec_module(exporter)
    output = exporter.render_metrics(
        {
            "rows": [
                {"node": "ghost", "state": "available", "throughput": 12, "latency_ms": 1, "action_count": 2},
                {"node": "attacker", "state": "available", "throughput": 99},
            ]
        }
    )
    assert 'node="ghost"' in output
    assert "attacker" not in output
