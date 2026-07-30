from pathlib import Path

import pytest

from scripts import collect_boot_update_telemetry as telemetry


def record(**overrides):
    value = {
        "schema_version": "1.0",
        "recorded_at": "2026-07-29T20:00:00Z",
        "workflow_name": "toggle-testing-abc",
        "lane": "bluefin-testing",
        "bootc_status": {"observed": True, "evidence": "bootc status --json"},
        "deployment": {"image": "ghcr.io/projectbluefin/bluefin:testing", "digest": "sha256:abc"},
        "update": {"succeeded": True, "evidence": "verify-forward"},
        "rollback": {"succeeded": True, "evidence": "verify-back"},
        "post_update_boot": {"succeeded": True, "evidence": "reboot-forward"},
    }
    value.update(overrides)
    return value


def test_normalize_rejects_invented_or_malformed_status():
    with pytest.raises(telemetry.RecordError, match="must be boolean"):
        telemetry.normalize_record(record(update={"succeeded": "yes"}))


def test_append_is_deduplicated_and_dataset_preserves_identity(tmp_path: Path):
    history = tmp_path / "boot-update.ndjson"
    assert telemetry.append_record(history, record())
    assert not telemetry.append_record(history, record())
    dataset = telemetry.build_dataset(history, "2026-07-29T21:00:00Z")
    assert dataset["_meta"]["status"] == "ready"
    assert dataset["rows"][0]["deployment"]["digest"] == "sha256:abc"
    assert dataset["rows"][0]["update"]["succeeded"] is True


def test_dataset_is_explicitly_unavailable_without_evidence(tmp_path: Path):
    dataset = telemetry.build_dataset(tmp_path / "missing.ndjson", "2026-07-29T21:00:00Z")
    assert dataset["_meta"]["status"] == "unavailable"
    assert dataset["summary_metrics"][0]["value"] is None


def test_toggle_workflow_emits_the_contract():
    content = (
        Path(__file__).resolve().parents[2]
        / "argo/workflow-templates/toggle-testing-rebase.yaml"
    ).read_text(encoding="utf-8")
    assert "emit-boot-update-telemetry" in content
    assert "boot-update-telemetry.json" in content
    assert "onExit: teardown" in content
    assert "publish-boot-update-telemetry" in content
    assert 'RECORDED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"' in content
    assert "record_boot_update_telemetry.py" in content
    assert '--recorded-at "${RECORDED_AT}"' in content
    assert "verify-forward" in content
    assert "verify-back" in content
