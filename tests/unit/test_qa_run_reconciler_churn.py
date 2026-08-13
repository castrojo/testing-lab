import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "publish_qa_run.py"


def load_module():
    spec = importlib.util.spec_from_file_location("publish_qa_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow():
    return {
        "metadata": {
            "name": "bluefin-qa-abcde",
            "uid": "12345678-1234-1234-1234-123456789abc",
            "creationTimestamp": "2026-08-01T14:00:00Z",
        },
        "spec": {
            "workflowTemplateRef": {"name": "bluefin-qa-pipeline"},
            "arguments": {
                "parameters": [
                    {"name": "variant", "value": "bluefin"},
                    {"name": "image", "value": "ghcr.io/projectbluefin/bluefin"},
                    {"name": "image-tag", "value": "testing"},
                    {"name": "image-digest", "value": "sha256:" + "a" * 64},
                    {"name": "suites", "value": "smoke"},
                ]
            },
        },
        "status": {
            "phase": "Running",
            "startedAt": "2026-08-01T14:01:00Z",
            "nodes": {
                "node": {
                    "displayName": "run-container-tests",
                    "outputs": {},
                }
            },
        },
    }


def test_published_state_short_circuits_before_clone(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    record = module.normalize_workflow(workflow(), "2026-08-01T14:02:00Z")
    state_file = tmp_path / "published-state.json"
    state_file.write_text(
        json.dumps(module.publication_state([record])),
        encoding="utf-8",
    )

    def unexpected_git(*args, **kwargs):
        raise AssertionError("clone must not run for a covered candidate")

    monkeypatch.setattr(module, "run_git", unexpected_git)

    assert module.publish([record], tmp_path, state_file=state_file) == 0
    assert not (tmp_path / ".qa-run-history").exists()


def test_missing_or_malformed_state_falls_back_to_clone(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    record = module.normalize_workflow(workflow(), "2026-08-01T14:02:00Z")
    state_file = tmp_path / "published-state.json"
    state_file.write_text("{not-json", encoding="utf-8")
    calls = []

    def fake_git(args, *, cwd, env):
        calls.append(args)
        if args[0] == "clone":
            history = Path(args[-1]) / module.HISTORY_PATH
            history.parent.mkdir(parents=True)
            history.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(module, "run_git", fake_git)

    assert module.publish([record], tmp_path, state_file=state_file) == 1
    assert calls[0] == [
        "clone",
        "--depth",
        "1",
        "--filter=blob:none",
        "--sparse",
        module.REPO_URL,
        str(tmp_path / ".qa-run-history"),
    ]
    assert module.read_publication_state(state_file) == module.publication_state([record])["records"]


def test_missing_github_token_remains_an_explicit_error(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(module.RecordError, match="GITHUB_TOKEN is required"):
        module.publish([], tmp_path)


def test_reconciler_uses_hourly_schedule_and_configmap_publisher():
    documents = list(
        yaml.safe_load_all(
            (ROOT / "manifests" / "qa-run-reconciler.yaml").read_text(encoding="utf-8")
        )
    )
    configmap = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and document["metadata"]["name"] == "qa-run-publisher"
    )
    cron = next(
        document
        for document in documents
        if document["kind"] == "CronWorkflow"
        and document["metadata"]["name"] == "qa-run-reconciler"
    )
    source = cron["spec"]["workflowSpec"]["templates"][0]["script"]["source"]

    assert cron["spec"]["schedules"] == ["0 * * * *"]
    assert "raw.githubusercontent.com" not in source
    assert "python3 /opt/qa-run/publish_qa_run.py" in source
    assert "--state-file /workspace/published-state.json" in source
    assert configmap["data"]["publish_qa_run.py"] == SCRIPT.read_text(encoding="utf-8")
    assert {
        "name": "publisher",
        "configMap": {
            "name": "qa-run-publisher",
            "items": [{"key": "publish_qa_run.py", "path": "publish_qa_run.py"}],
        },
    } in cron["spec"]["workflowSpec"]["volumes"]
