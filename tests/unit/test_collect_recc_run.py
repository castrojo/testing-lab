import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect_recc_run as collector  # noqa: E402


def test_parse_run_metadata_normalizes_timestamps_and_duration():
    result = collector.parse_run_metadata(
        {
            "workflow_name": "recc-baseline-cold",
            "mode": "cache-only",
            "started_at": "2026-07-31T14:00:00-04:00",
            "finished_at": "2026-07-31T18:01:02Z",
        }
    )

    assert result["run_id"] == "recc-baseline-cold"
    assert result["started_at"] == "2026-07-31T18:00:00Z"
    assert result["finished_at"] == "2026-07-31T18:01:02Z"
    assert result["duration_seconds"] == 62
    assert result["state"] == "available"


def test_buildstream_parser_keeps_partial_timings_and_element_evidence():
    result = collector.parse_buildstream_output(
        json.dumps(
            {
                "timings": {"wall": "1m 2s", "fetch": 3.5, "build": "40s"},
                "elements": [
                    {
                        "name": "recc-baseline.bst",
                        "state": "cached",
                        "key": "element-key",
                        "cache_origin": "artifact-cache",
                        "digest": "sha256:output",
                    }
                ],
                "fixture_stdout": "hello from fixture",
            }
        )
    )

    assert result["state"] == "available"
    assert result["timings"] == {
        "wall_seconds": 62.0,
        "fetch_seconds": 3.5,
        "build_seconds": 40.0,
        "push_seconds": None,
    }
    assert result["elements"][0]["key"] == "element-key"
    assert result["elements"][0]["cache_origin"] == "artifact-cache"
    assert result["output_digest"] == "sha256:output"
    assert result["fixture_stdout"] == "hello from fixture"
    assert "push_seconds" in result["unavailable_fields"]


def test_buildstream_text_requires_explicit_element_cache_origin_and_digest_fields():
    result = collector.parse_buildstream_output(
        """
        element: recc-baseline.bst
        state: built
        key: element-key
        cache origin: local-build
        output digest: sha256:output
        fixture stdout: hello
        """
    )

    assert result["elements"] == [
        {
            "name": "recc-baseline.bst",
            "state": "built",
            "key": "element-key",
            "cache_origin": "local-build",
            "digest": "sha256:output",
            "unavailable_fields": {},
        }
    ]
    assert result["output_digest"] == "sha256:output"
    assert result["fixture_stdout"] == "hello"


def test_recc_verbose_parser_extracts_cache_and_fallback_evidence():
    result = collector.parse_recc_verbose(
        """
        [RECC]
        action started compile-1
        action completed compile-1
        action cache hit compile-1
        action cache miss compile-2
        fallback to local compile
        compile time: 4.5s
        link duration: 800ms
        [/RECC]
        """
    )

    assert result["action_count"] == 1
    assert result["cache_hits"] == 1
    assert result["cache_misses"] == 1
    assert result["local_fallbacks"] == 1
    assert result["compile_seconds"] == 4.5
    assert result["link_seconds"] == 0.8
    assert result["state"] == "available"


def test_generic_buildstream_log_is_not_recc_evidence():
    result = collector.parse_recc_verbose(
        """
        action started compile-1
        action completed compile-1
        action cache hit compile-1
        fallback to local compile
        compile time: 4.5s
        """
    )

    assert result["state"] == "unavailable"
    assert result["action_count"] is None
    assert result["cache_hits"] is None
    assert "explicit RECC marker" in result["state_reason"]


def test_recc_dedicated_section_is_accepted_without_per_line_markers():
    result = collector.parse_recc_verbose(
        """
        === BEGIN RECC_VERBOSE LOG ===
        action cache hit compile-1
        === END RECC_VERBOSE LOG ===
        """
    )

    assert result["cache_hits"] == 1
    assert result["state"] == "available"


def test_recc_statsd_metrics_provide_action_cache_evidence():
    result = collector.parse_recc_verbose(
        """
        === BEGIN RECC_VERBOSE LOG ===
        [RECC_METRICS] recc.action_cache_hit:2|c
        [RECC_METRICS] recc.action_cache_miss:1|c
        [RECC_METRICS] recc.fallback:1|c
        [RECC_METRICS] recc.execute_local_no_action_result:125|ms
        === END RECC_VERBOSE LOG ===
        """
    )

    assert result["action_count"] == 3
    assert result["cache_hits"] == 2
    assert result["cache_misses"] == 1
    assert result["local_fallbacks"] == 1
    assert result["compile_seconds"] == 0.125
    assert result["state"] == "available"


def test_recc_statsd_counters_are_not_mistaken_for_compile_timings():
    result = collector.parse_recc_verbose(
        """
        === BEGIN RECC_VERBOSE LOG ===
        [RECC_METRICS] recc.action_cache_miss:2|c
        [RECC_METRICS] recc.execute_local_no_action_result:2|c
        === END RECC_VERBOSE LOG ===
        """
    )

    assert result["action_count"] == 2
    assert result["local_fallbacks"] == 2
    assert result["compile_seconds"] is None


def test_collected_run_preserves_recc_statsd_evidence_from_element_log():
    result = collector.collect_run(
        {"run_id": "cache-regression", "mode": "cache-only"},
        recc_verbose="""
        === BuildStream element log: recc-baseline.bst ===
        [RECC_METRICS] recc.action_cache_hit:2|c
        [RECC_METRICS] recc.action_cache_miss:1|c
        [RECC_METRICS] recc.fallback:1|c
        [RECC_METRICS] recc.execute_local_no_action_result:125|ms
        """,
    )

    assert result["recc"]["state"] == "available"
    assert result["recc"]["action_count"] == 3
    assert result["recc"]["cache_hits"] == 2
    assert result["recc"]["cache_misses"] == 1
    assert result["recc"]["local_fallbacks"] == 1
    assert result["recc"]["compile_seconds"] == 0.125


def test_prometheus_names_are_discovered_and_deltas_are_label_aware():
    before = """
    # HELP bb_actions_total actions
    bb_actions_total{worker="ghost"} 4
    bb_actions_total{worker="exo-0"} 2
    bb_queue_seconds_sum 10
    """
    after = """
    bb_actions_total{worker="ghost"} 7
    bb_actions_total{worker="exo-0"} 2
    bb_queue_seconds_sum 13
    """

    assert collector.discover_metric_names(before) == [
        "bb_actions_total",
        "bb_queue_seconds_sum",
    ]
    delta = collector.prometheus_delta(
        before,
        after,
        metric_names=["bb_actions_total", "metric_not_present"],
    )
    deltas = {
        row["labels"]["worker"]: row["delta"]
        for row in delta["metrics"]["bb_actions_total"]
    }
    assert deltas == {"ghost": 3, "exo-0": 0}
    assert delta["unavailable_metrics"] == {
        "metric_not_present": "metric was not present in both snapshots"
    }
    assert delta["worker_deltas"] is None
    assert delta["cas_deltas"] is None


def test_buildbarn_group_summaries_keep_worker_and_cas_numbers_explicit():
    before = """
    buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_count{node="ghost"} 2
    buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_sum{node="ghost"} 4
    buildbarn_builder_in_memory_build_queue_tasks_queued_duration_seconds_sum{node="ghost"} 1
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_count{operation="Get",storage_type="cas"} 3
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum{operation="Get",storage_type="cas"} 100
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_count{operation="Put",storage_type="cas"} 1
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum{operation="Put",storage_type="cas"} 40
    """
    after = """
    buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_count{node="ghost"} 5
    buildbarn_builder_in_memory_build_queue_tasks_executing_duration_seconds_sum{node="ghost"} 10
    buildbarn_builder_in_memory_build_queue_tasks_queued_duration_seconds_sum{node="ghost"} 3
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_count{operation="Get",storage_type="cas"} 5
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum{operation="Get",storage_type="cas"} 180
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_count{operation="Put",storage_type="cas"} 2
    buildbarn_blobstore_blob_access_operations_blob_size_bytes_sum{operation="Put",storage_type="cas"} 70
    """

    delta = collector.prometheus_delta(
        before,
        after,
        metric_names=(
            *collector.BUILDBARN_WORKER_METRICS,
            *collector.BUILDBARN_CAS_METRICS,
        ),
    )

    assert delta["worker_deltas"]["summary"] == {
        "actions_executed": 3,
        "queue_seconds": 2,
        "execution_seconds": 6,
    }
    assert delta["cas_deltas"]["summary"] == {
        "get_requests": 2,
        "put_requests": 1,
        "get_bytes": 80,
        "put_bytes": 30,
    }


def test_missing_sections_are_explicitly_unavailable():
    result = collector.collect_run({"run_id": "empty"})

    assert result["state"] == "available"
    assert result["buildstream"]["state"] == "unavailable"
    assert result["buildstream"]["state_reason"]
    assert result["recc"]["state"] == "unavailable"
    assert result["buildbarn"]["delta"]["state"] == "unavailable"
    assert result["buildbarn"]["before"]["state_reason"]
    assert result["buildstream"]["output_digest"] is None
    assert result["buildstream"]["fixture_stdout"] is None
    assert "output_digest" in result["buildstream"]["unavailable_fields"]
    assert "fixture_stdout" in result["buildstream"]["unavailable_fields"]
    assert result["buildbarn"]["delta"]["worker_deltas"] is None
    assert result["buildbarn"]["delta"]["cas_deltas"] is None


def test_missing_phase_timings_and_element_cache_origin_are_explicitly_unavailable():
    result = collector.collect_run(
        {
            "run_id": "run-1",
            "phases": {"cold": {"state": "success"}, "warm": {"state": "not-run"}},
        },
        buildstream_output=json.dumps(
            {
                "elements": [
                    {
                        "name": "recc-baseline.bst",
                        "state": "built",
                        "key": "element-key",
                    }
                ]
            }
        ),
    )

    assert result["buildstream"]["phases"]["cold"]["state"] == "unavailable"
    assert result["buildstream"]["phases"]["cold"]["timings"]["build_seconds"] is None
    assert result["buildstream"]["phases"]["cold"]["unavailable_fields"]["timings.wall_seconds"]
    assert "element_cache_origin" in result["buildstream"]["unavailable_fields"]
    assert "cache_origin" in result["buildstream"]["elements"][0]["unavailable_fields"]


def test_cli_emits_machine_readable_record_without_raw_artifacts(tmp_path, capsys):
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps({"run_id": "run-1", "mode": "cache-only"}),
        encoding="utf-8",
    )
    buildstream = tmp_path / "buildstream.log"
    buildstream.write_text("wall: 2s\n", encoding="utf-8")

    assert collector.main(
        [
            "--metadata",
            str(metadata),
            "--buildstream-log",
            str(buildstream),
        ]
    ) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["run"]["run_id"] == "run-1"
    assert record["buildstream"]["timings"]["wall_seconds"] == 2.0
    assert "wall: 2s" not in json.dumps(record)
