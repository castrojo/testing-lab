"""Unit coverage for scripts/collect_release_verdict.py.

Covers the pure verdict logic invoked by
.github/workflows/update-test-results.yml: load_json, now_iso, latest_build,
_qa_substatus, qa_input, append_history and the cosign_verify no-binary path.
Network (ghcr_digest) and main() I/O are out of scope.
"""

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect_release_verdict as verdict  # noqa: E402


def gate_row(suite, last_run=None, digest=None, result_status="passed", role="gate",
             variant="bluefin", branch="stable"):
    return {
        "suite": suite,
        "last_run": last_run,
        "digest": digest,
        "result_status": result_status,
        "role": role,
        "variant": variant,
        "branch": branch,
    }


def test_now_iso_is_utc_second_precision():
    stamp = verdict.now_iso()
    assert stamp.endswith("Z")
    assert len(stamp) == len("2026-01-01T00:00:00Z")


def test_load_json_reads_document(tmp_path):
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"a": 1}))
    assert verdict.load_json(path) == {"a": 1}


def test_load_json_returns_none_instead_of_raising(tmp_path):
    assert verdict.load_json(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert verdict.load_json(bad) is None


def test_latest_build_picks_newest_started_at():
    builds = {
        "bluefin-stable": [
            {"started_at": "2026-01-01T00:00:00Z", "id": "old"},
            {"started_at": "2026-03-01T00:00:00Z", "id": "new"},
        ]
    }
    assert verdict.latest_build(builds, "bluefin-stable")["id"] == "new"


def test_latest_build_ignores_runs_without_started_at():
    builds = {"lane": [{"id": "undated"}, {"started_at": "2026-01-01T00:00:00Z", "id": "dated"}]}
    assert verdict.latest_build(builds, "lane")["id"] == "dated"


def test_latest_build_unavailable_for_unknown_or_undated_lane():
    assert verdict.latest_build({}, "lane") is None
    assert verdict.latest_build({"lane": [{"id": "undated"}]}, "lane") is None


def test_qa_substatus_unavailable_when_no_rows_tracked():
    result = verdict._qa_substatus([], None, None, "gate")
    assert result["status"] == "unavailable"
    assert "no gate QA suites tracked" in result["reason"]


def test_qa_substatus_unavailable_when_no_suite_has_run():
    rows = [gate_row("smoke"), gate_row("system")]
    result = verdict._qa_substatus(rows, None, "sha256:abc", "gate")
    assert result["status"] == "unavailable"
    assert "no gate lab QA run has published results" in result["reason"]


def test_qa_substatus_passes_when_every_suite_matches_current_digest():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc"),
        gate_row("system", last_run="2026-02-02T00:00:00Z", digest="sha256:abc"),
    ]
    result = verdict._qa_substatus(rows, "2026-01-01T00:00:00Z", "sha256:abc", "gate")
    assert result["status"] == "passed"
    assert result["reason"] is None
    assert result["last_run"] == "2026-02-02T00:00:00Z"


def test_qa_substatus_pending_when_suite_digest_is_stale():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc"),
        gate_row("system", last_run="2026-02-01T00:00:00Z", digest="sha256:old"),
    ]
    result = verdict._qa_substatus(rows, "2026-01-01T00:00:00Z", "sha256:abcdef012345", "gate")
    assert result["status"] == "pending"
    assert "system" in result["reason"]
    assert "sha256:abcde" in result["reason"]


def test_qa_substatus_pending_reason_says_none_without_a_digest():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z"),
        gate_row("system"),
    ]
    result = verdict._qa_substatus(rows, "2026-01-01T00:00:00Z", None, "gate")
    assert result["status"] == "pending"
    assert "digest none" in result["reason"]


def test_qa_substatus_falls_back_to_build_time_when_digest_missing():
    rows = [gate_row("smoke", last_run="2026-02-01T00:00:00Z")]
    fresh = verdict._qa_substatus(rows, "2026-01-01T00:00:00Z", None, "gate")
    stale = verdict._qa_substatus(rows, "2026-03-01T00:00:00Z", None, "gate")
    assert fresh["status"] == "passed"
    assert stale["status"] == "pending"


def test_qa_substatus_failed_only_when_matching_evidence_failed():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc"),
        gate_row("system", last_run="2026-02-01T00:00:00Z", digest="sha256:abc",
                 result_status="failed"),
    ]
    result = verdict._qa_substatus(rows, "2026-01-01T00:00:00Z", "sha256:abc", "gate")
    assert result["status"] == "failed"
    assert "system" in result["reason"]


def test_qa_substatus_pending_outranks_failed():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:old",
                 result_status="failed"),
        gate_row("system", last_run="2026-02-01T00:00:00Z", digest="sha256:abc"),
    ]
    result = verdict._qa_substatus(rows, "2026-01-01T00:00:00Z", "sha256:abc", "gate")
    assert result["status"] == "pending"


def test_qa_input_unavailable_when_lane_has_no_rows():
    result = verdict.qa_input([], "bluefin", "stable", None, None)
    assert result["status"] == "unavailable"
    assert result["rows"] == []
    assert result["informational"]["status"] == "unavailable"


def test_qa_input_filters_to_the_requested_lane():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc"),
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc",
                 branch="testing"),
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc",
                 variant="dakota"),
    ]
    result = verdict.qa_input(rows, "bluefin", "stable", None, "sha256:abc")
    assert len(result["rows"]) == 1
    assert result["status"] == "passed"


def test_qa_input_informational_failure_never_gates_the_lane():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc"),
        gate_row("developer", last_run="2026-02-01T00:00:00Z", digest="sha256:abc",
                 result_status="failed", role="info"),
    ]
    result = verdict.qa_input(rows, "bluefin", "stable", None, "sha256:abc")
    assert result["status"] == "passed"
    assert result["informational"]["status"] == "failed"
    assert "developer" in result["informational"]["reason"]


def test_qa_input_gate_failure_is_the_lane_status():
    rows = [
        gate_row("smoke", last_run="2026-02-01T00:00:00Z", digest="sha256:abc",
                 result_status="failed"),
        gate_row("developer", last_run="2026-02-01T00:00:00Z", digest="sha256:abc",
                 role="info"),
    ]
    result = verdict.qa_input(rows, "bluefin", "stable", None, "sha256:abc")
    assert result["status"] == "failed"
    assert result["informational"]["status"] == "passed"


def test_qa_input_lane_without_gate_suites_is_unavailable_not_passed():
    rows = [
        gate_row("developer", last_run="2026-02-01T00:00:00Z", digest="sha256:abc",
                 role="info"),
    ]
    result = verdict.qa_input(rows, "bluefin", "stable", None, "sha256:abc")
    assert result["status"] == "unavailable"
    assert result["informational"]["status"] == "passed"


def test_cosign_verify_is_unavailable_without_the_binary(monkeypatch):
    monkeypatch.setattr(verdict.shutil, "which", lambda _name: None)
    ok, detail = verdict.cosign_verify("ghcr.io/projectbluefin/bluefin@sha256:abc")
    assert ok is None
    assert "cosign binary not available" in detail


def history_row(lane="bluefin-stable", digest="sha256:abc", verdict_value="good",
                recorded_at="2026-02-01T00:00:00Z"):
    return {
        "lane": lane,
        "digest": digest,
        "verdict": verdict_value,
        "recorded_at": recorded_at,
    }


def read_history(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_append_history_creates_the_file_and_parent_dir(tmp_path, monkeypatch):
    target = tmp_path / "history" / "release-verdict.ndjson"
    monkeypatch.setattr(verdict, "HISTORY_PATH", target)
    now = verdict.now_iso()

    assert verdict.append_history([history_row(recorded_at=now)], now) == 1
    assert read_history(target)[0]["lane"] == "bluefin-stable"


def test_append_history_skips_unchanged_lane_rows(tmp_path, monkeypatch):
    target = tmp_path / "release-verdict.ndjson"
    monkeypatch.setattr(verdict, "HISTORY_PATH", target)
    now = verdict.now_iso()
    target.write_text(json.dumps(history_row(recorded_at=now)) + "\n")

    assert verdict.append_history([history_row(recorded_at=now)], now) == 0
    assert len(read_history(target)) == 1


def test_append_history_records_digest_or_verdict_changes(tmp_path, monkeypatch):
    target = tmp_path / "release-verdict.ndjson"
    monkeypatch.setattr(verdict, "HISTORY_PATH", target)
    now = verdict.now_iso()
    target.write_text(json.dumps(history_row(recorded_at=now)) + "\n")

    assert verdict.append_history([history_row(digest="sha256:new", recorded_at=now)], now) == 1
    assert verdict.append_history(
        [history_row(digest="sha256:new", verdict_value="bad", recorded_at=now)], now
    ) == 1
    assert len(read_history(target)) == 3


def test_append_history_compares_against_the_latest_row_per_lane(tmp_path, monkeypatch):
    target = tmp_path / "release-verdict.ndjson"
    monkeypatch.setattr(verdict, "HISTORY_PATH", target)
    now = verdict.now_iso()
    target.write_text(
        json.dumps(history_row(digest="sha256:old", recorded_at=now)) + "\n"
        + json.dumps(history_row(digest="sha256:new", recorded_at=now)) + "\n"
    )

    assert verdict.append_history([history_row(digest="sha256:new", recorded_at=now)], now) == 0
    assert verdict.append_history([history_row(digest="sha256:old", recorded_at=now)], now) == 1


def test_append_history_tracks_lanes_independently(tmp_path, monkeypatch):
    target = tmp_path / "release-verdict.ndjson"
    monkeypatch.setattr(verdict, "HISTORY_PATH", target)
    now = verdict.now_iso()
    target.write_text(json.dumps(history_row(recorded_at=now)) + "\n")

    appended = verdict.append_history(
        [history_row(recorded_at=now), history_row(lane="dakota-testing", recorded_at=now)],
        now,
    )
    assert appended == 1
    assert {r["lane"] for r in read_history(target)} == {"bluefin-stable", "dakota-testing"}


def test_append_history_drops_rows_older_than_the_retention_window(tmp_path, monkeypatch):
    target = tmp_path / "release-verdict.ndjson"
    monkeypatch.setattr(verdict, "HISTORY_PATH", target)
    now = verdict.now_iso()
    expired = (
        verdict.datetime.datetime.now(verdict.datetime.timezone.utc)
        - verdict.datetime.timedelta(days=verdict.HISTORY_DAYS + 1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    target.write_text(json.dumps(history_row(digest="sha256:ancient", recorded_at=expired)) + "\n")

    verdict.append_history([history_row(recorded_at=now)], now)

    digests = [r["digest"] for r in read_history(target)]
    assert digests == ["sha256:abc"]


def test_append_history_tolerates_corrupt_existing_lines(tmp_path, monkeypatch):
    target = tmp_path / "release-verdict.ndjson"
    monkeypatch.setattr(verdict, "HISTORY_PATH", target)
    now = verdict.now_iso()
    target.write_text("{not json\n\n" + json.dumps(history_row(recorded_at=now)) + "\n")

    assert verdict.append_history([history_row(digest="sha256:new", recorded_at=now)], now) == 1
    assert len(read_history(target)) == 2
