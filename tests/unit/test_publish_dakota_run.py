import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import publish_dakota_run as history  # noqa: E402


def compact_record(**overrides):
    record = {
        "kind": "build",
        "workflow_name": "dakota-build-pipeline-abc12",
        "status": "passed",
        "started_at": "2026-07-26T10:00:00-04:00",
        "finished_at": "2026-07-26T14:30:00Z",
        "run_url": "https://argo.example.test/workflows/argo/dakota-build-pipeline-abc12",
        "commit_sha": "a" * 40,
        "metrics": {"queue_seconds": 12, "image_size_bytes": 123456},
    }
    record.update(overrides)
    return record


def stored_record(index, *, status="passed", duration=10, commit=None, kind="build", metrics=None):
    started = datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    record = compact_record(
        kind=kind,
        workflow_name=f"dakota-{kind}-{index}",
        status=status,
        started_at=started.isoformat(),
        finished_at=(started + timedelta(seconds=duration)).isoformat(),
        recorded_at=(started + timedelta(seconds=duration)).isoformat(),
        commit_sha=commit or f"{index:040x}",
        metrics=metrics or {},
    )
    if status == "failed":
        record["failure_hint"] = "BuildBarn worker unavailable"
    if kind == "publish" and status == "passed":
        record["digest"] = "sha256:" + "b" * 64
    return history.normalize_record(record)


def test_normalizes_compact_record_to_dashboard_contract():
    record = history.normalize_record(compact_record())

    assert record["schema_version"] == "1.0"
    assert record["plane"] == "lab"
    assert record["record_type"] == "build"
    assert record["run_id"] == record["workflow_name"]
    assert record["started_at"] == "2026-07-26T14:00:00Z"
    assert record["finished_at"] == "2026-07-26T14:30:00Z"
    assert record["recorded_at"] == "2026-07-26T14:30:00Z"
    assert record["duration_min"] == 30
    assert record["metrics"]["workflow_duration_seconds"] == 1800
    assert record["failure_class"] is None
    assert record["failure_stage"] is None
    assert record["telemetry"] == {}


def test_normalizes_measured_execution_telemetry_and_keeps_unavailable_fields_null():
    record = history.normalize_record(
        compact_record(
            telemetry={
                "build_seconds": 42,
                "push_seconds": 3,
                "zot_push": "passed",
                "ghcr_push": "unavailable",
                "second_run_speedup": None,
            }
        )
    )
    assert record["telemetry"]["build_seconds"] == 42
    assert record["telemetry"]["zot_push"] == "passed"
    assert record["telemetry"]["second_run_speedup"] is None


def test_rejects_conflicting_derived_workflow_duration():
    with pytest.raises(history.RecordError, match="must match the record timestamps"):
        history.normalize_record(compact_record(metrics={"workflow_duration_seconds": 1}))


def test_accepts_legacy_stored_record_without_derived_duration():
    record = history.normalize_record(compact_record())
    record["metrics"].pop("workflow_duration_seconds")

    assert history.validate_stored_record(record) == record


@pytest.mark.parametrize(
    ("failure_class", "hint", "stage", "expected"),
    [
        ("auth", None, None, "authentication"),
        ("re", None, None, "remote-execution"),
        (None, "context deadline exceeded", None, "timeout"),
        (None, "dial tcp: connection refused", None, "network"),
        (None, "no space left on device", None, "storage"),
        (None, None, "push-image", "publish"),
        (None, "BuildStream element failed", None, "build"),
        (None, "pod was OOMKilled", None, "infrastructure"),
        (None, "unexpected failure", None, "unknown"),
    ],
)
def test_normalizes_failure_classes(failure_class, hint, stage, expected):
    record = history.normalize_record(
        compact_record(
            status="failed",
            failure_class=failure_class,
            failure_hint=hint,
            failure_stage=stage,
        )
    )

    assert record["failure_class"] == expected
    assert "failure_hint" not in record


@pytest.mark.parametrize(
    "change",
    [
        {"raw_logs": "do not store me"},
        {"metrics": {"github_token": 1}},
        {"metrics": {"duration": "slow"}},
        {"run_url": "https://user:secret@example.test/run"},
        {"run_url": "https://example.test/run?token=secret"},
        {"workflow_name": "bad\nworkflow"},
        {"failure_stage": "Bearer ghp_abcdefghijklmnopqrstuvwxyz"},
        {"failure_stage": "Traceback (most recent call last):"},
        {"started_at": "2026-07-26T10:00:00"},
        {"finished_at": "2026-07-26T09:00:00Z"},
        {"status": "running"},
        {"commit_sha": "not-a-sha"},
    ],
)
def test_rejects_unsafe_or_invalid_records(change):
    with pytest.raises(history.RecordError):
        history.normalize_record(compact_record(**change))


def test_successful_publish_requires_digest():
    with pytest.raises(history.RecordError, match="require digest"):
        history.normalize_record(compact_record(kind="publish"))


def test_passed_record_rejects_failure_details():
    with pytest.raises(history.RecordError, match="cannot contain failure"):
        history.normalize_record(compact_record(failure_stage="build"))


def test_append_is_idempotent_and_preserves_append_only_file(tmp_path):
    path = tmp_path / "history.ndjson"
    first = history.normalize_record(compact_record())

    assert history.append_record(path, first)
    before = path.read_bytes()
    assert not history.append_record(path, first)
    assert path.read_bytes() == before

    second = history.normalize_record(
        compact_record(
            workflow_name="dakota-build-pipeline-def34",
            started_at="2026-07-26T15:00:00Z",
            finished_at="2026-07-26T15:10:00Z",
        )
    )
    assert history.append_record(path, second)
    assert [json.loads(line)["workflow_name"] for line in path.read_text().splitlines()] == [
        first["workflow_name"],
        second["workflow_name"],
    ]


def test_append_rejects_conflicting_duplicate_workflow(tmp_path):
    path = tmp_path / "history.ndjson"
    original = history.normalize_record(compact_record())
    conflict = history.normalize_record(compact_record(metrics={"queue_seconds": 99}))
    history.append_record(path, original)

    with pytest.raises(history.RecordError, match="different record content"):
        history.append_record(path, conflict)


def test_validate_history_checks_canonical_records_and_duplicate_names(tmp_path):
    path = tmp_path / "history.ndjson"
    record = history.normalize_record(compact_record())
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert history.validate_history(path) == 1

    path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(history.RecordError, match="duplicate workflow_name"):
        history.validate_history(path)


def test_validate_history_rejects_malformed_ndjson(tmp_path):
    path = tmp_path / "history.ndjson"
    path.write_text('{"workflow_name":"ok"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(history.RecordError, match="invalid JSON"):
        history.validate_history(path)


def test_percentile_uses_linear_interpolation():
    assert history.percentile([10, 20, 30, 40], 0.5) == 25
    assert history.percentile([10, 20, 30, 40], 0.95) == 38.5
    assert history.percentile([], 0.95) is None


def test_report_compares_trailing_failure_flakiness_and_percentiles():
    records = [
        stored_record(0, duration=10, commit="1" * 40),
        stored_record(1, duration=20, commit="1" * 40),
        stored_record(2, duration=30),
        stored_record(3, duration=40, status="failed"),
        stored_record(4, duration=50, commit="2" * 40),
        stored_record(5, duration=60, status="failed", commit="2" * 40),
        stored_record(6, duration=70, metrics={"queue_seconds": 30}),
        stored_record(7, duration=80, status="failed", metrics={"queue_seconds": 50}),
    ]

    report = history.build_report(records, window=4)[0]

    assert report["current"]["duration_seconds"] == {"p50": 65, "p95": 78.5}
    assert report["previous"]["duration_seconds"] == {"p50": 25, "p95": 38.5}
    assert report["current"]["failure_rate_pct"] == 50
    assert report["previous"]["failure_rate_pct"] == 25
    assert report["current"]["flakiness_rate_pct"] == 100
    assert report["previous"]["flakiness_rate_pct"] == 0
    assert report["current"]["metrics"]["queue_seconds"] == {"p50": 40, "p95": 49}
    assert report["comparison"] == {
        "duration_p50_seconds_delta": 40,
        "duration_p95_seconds_delta": 40,
        "failure_rate_delta_pp": 25,
        "flakiness_rate_delta_pp": 100,
    }


def test_report_filters_record_type():
    records = [
        stored_record(0),
        stored_record(1, kind="publish", status="failed"),
    ]

    report = history.build_report(records, window=5, kind="publish")

    assert len(report) == 1
    assert report[0]["record_type"] == "publish"
    assert records[1]["plane"] == "lab"


def test_build_trend_dataset_aggregates_daily_throughput_and_duration():
    records = [
        stored_record(0, duration=60),
        stored_record(1, duration=120, status="failed"),
        stored_record(2, duration=180, kind="publish"),
    ]

    dataset = history.build_trend_dataset(records, "2026-07-29T00:00:00Z")

    assert dataset["schema_version"] == "v1"
    assert dataset["_meta"]["page"] == "dakota-build-trends"
    assert dataset["_meta"]["status"] == "ready"
    rows = {row["id"]: row for row in dataset["rows"]}
    assert rows["build-2026-07-01"]["throughput"] == 2
    assert rows["build-2026-07-01"]["duration_seconds"] == {
        "p50": 90,
        "p95": 117,
        "avg": 90,
    }
    assert rows["build-2026-07-01"]["passed"] == 1
    assert rows["build-2026-07-01"]["failed"] == 1
    assert rows["publish-2026-07-01"]["throughput"] == 1
    assert rows["publish-2026-07-01"]["duration_seconds"]["avg"] == 180
    assert all(row["state"] == "available" for row in dataset["rows"])
    assert all(row["source_url"] and row["derivation"] for row in dataset["rows"])


def test_build_trend_dataset_preserves_unavailable_state_without_records():
    dataset = history.build_trend_dataset([], "2026-07-29T00:00:00Z")

    assert dataset["rows"] == []
    assert dataset["_meta"]["status"] == "unavailable"
    metrics = {metric["id"]: metric for metric in dataset["summary_metrics"]}
    assert metrics["dakota_runs"]["value"] is None
    assert metrics["dakota_runs"]["state"] == "unavailable"
    assert metrics["dakota_runs"]["state_reason"]


def test_build_trend_dataset_exposes_execution_matrix_without_fabricating_values():
    record = stored_record(
        0,
        metrics={},
    )
    record["telemetry"] = {
        "build_seconds": 42,
        "zot_push": "passed",
        "ghcr_push": "unavailable",
        "second_run_speedup": None,
    }
    dataset = history.build_trend_dataset([record], "2026-07-29T00:00:00Z")
    row = dataset["execution_matrix"][0]
    assert row["phases"]["build_seconds"] == 42
    assert row["phases"]["clone_seconds"] is None
    assert row["zot_push"] == "passed"
    assert row["second_run_speedup"] is None
    assert row["state"] == "available"


def test_git_credentials_stay_out_of_askpass_file(tmp_path):
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    env = history.git_env(auth_dir, "super-secret-token")

    assert env["GITHUB_TOKEN"] == "super-secret-token"
    assert "super-secret-token" not in Path(env["GIT_ASKPASS"]).read_text()
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_publish_never_places_token_in_git_arguments(monkeypatch, tmp_path):
    calls = []

    def fake_git(args, *, cwd, env):
        calls.append((args, cwd, env))
        return subprocess.CompletedProcess(["git", *args], 0)

    monkeypatch.setattr(history, "run_git", fake_git)
    monkeypatch.setattr(history, "append_record", lambda path, record: True)
    record = history.normalize_record(compact_record())

    assert history.publish_record(record, token="super-secret-token", work_dir=tmp_path)
    assert all("super-secret-token" not in arg for args, _, _ in calls for arg in args)
    assert any(args[0] == "clone" and history.REPO_URL in args for args, _, _ in calls)
    assert any(args[0] == "push" and args[-1] == "HEAD:main" for args, _, _ in calls)


def test_publish_replays_append_from_latest_main_after_push_race(monkeypatch, tmp_path):
    commands = []
    appends = []
    pushes = iter((1, 0))

    def fake_git(args, *, cwd, env):
        commands.append(args)
        return subprocess.CompletedProcess(["git", *args], next(pushes) if args[0] == "push" else 0)

    monkeypatch.setattr(history, "run_git", fake_git)
    monkeypatch.setattr(history, "append_record", lambda path, record: appends.append(path) or True)

    assert history.publish_record(
        history.normalize_record(compact_record()), token="secret", work_dir=tmp_path
    )
    assert len(appends) == 2
    assert ["fetch", "origin", "main"] in commands
    assert ["reset", "--hard", "origin/main"] in commands


def test_cli_validate_and_report(tmp_path, capsys):
    record_path = tmp_path / "record.json"
    history_path = tmp_path / "history.ndjson"
    record_path.write_text(json.dumps(compact_record()), encoding="utf-8")

    assert history.main(["append", str(record_path), "--history", str(history_path)]) == 0
    assert capsys.readouterr().out.strip() == "appended"
    assert history.main(["validate-history", "--history", str(history_path)]) == 0
    assert capsys.readouterr().out.strip() == "valid: 1 Dakota records"
    assert history.main(["report", "--history", str(history_path), "--window", "5"]) == 0
    assert "dakota-testing build: n=1" in capsys.readouterr().out
