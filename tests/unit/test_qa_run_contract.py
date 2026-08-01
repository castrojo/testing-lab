import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "publish_qa_run.py"


def load_module():
    spec = importlib.util.spec_from_file_location("publish_qa_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workflow(*, phase, finished_at=None, with_result=False, kde=False):
    output_parameters = [{"name": "result", "value": "10/10 scenarios passed"}]
    if with_result:
        output_parameters.append(
            {"name": "failed-scenarios", "value": '["Shell opens"]'}
        )
    nodes = {
        "node": {
            "displayName": "run-kde-tests" if kde else "run-container-tests",
            "outputs": {"parameters": output_parameters} if with_result else {},
        }
    }
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
                    {"name": "suites", "value": "smoke,system"},
                ]
            },
        },
        "status": {
            "phase": phase,
            "startedAt": "2026-08-01T14:01:00Z",
            "finishedAt": finished_at,
            "nodes": nodes,
        },
    }


def test_running_snapshot_has_explicit_unavailable_artifacts_and_public_safe_provenance():
    module = load_module()

    record = module.normalize_workflow(
        workflow(phase="Running"),
        "2026-08-01T14:02:00Z",
    )

    assert record["lifecycle"]["state"] == "running"
    assert record["lane"] == {
        "name": "bluefin-testing",
        "variant": "bluefin",
        "branch": "testing",
        "suite": "smoke,system",
        "state": "available",
        "state_reason": None,
    }
    assert record["image"]["state"] == "available"
    assert record["artifacts"]["results"]["state"] == "unavailable"
    assert record["artifacts"]["screenshot"]["state"] == "not_applicable"
    assert record["provenance"]["workflow_url"] is None
    assert record["provenance"]["workflow_url_state"] == "unavailable"
    assert "192.168." not in json.dumps(record)


def test_terminal_snapshot_retains_result_and_failure_evidence():
    module = load_module()

    record = module.normalize_workflow(
        workflow(phase="Failed", finished_at="2026-08-01T14:03:00Z", with_result=True),
        "2026-08-01T14:04:00Z",
    )

    assert record["lifecycle"]["state"] == "terminal"
    assert record["artifacts"]["results"]["state"] == "available"
    assert record["artifacts"]["results"]["failed_scenarios"] == ["Shell opens"]
    assert record["failure"] == {"state": "failed", "phase": "Failed"}


def test_snapshot_identity_changes_for_lifecycle_evidence_not_reobservation():
    module = load_module()
    running = workflow(phase="Running")

    first = module.normalize_workflow(running, "2026-08-01T14:02:00Z")
    repeated = module.normalize_workflow(running, "2026-08-01T14:03:00Z")
    terminal = module.normalize_workflow(
        workflow(phase="Succeeded", finished_at="2026-08-01T14:04:00Z", kde=True),
        "2026-08-01T14:05:00Z",
    )

    assert first["snapshot_id"] == repeated["snapshot_id"]
    assert first["observed_at"] != repeated["observed_at"]
    assert terminal["snapshot_id"] != first["snapshot_id"]
    assert terminal["artifacts"]["screenshot"]["state"] == "available"


def test_private_or_secret_like_image_parameters_are_not_exported():
    module = load_module()
    unsafe = workflow(phase="Running")
    unsafe["spec"]["arguments"]["parameters"][1]["value"] = "192.168.1.2:5000/secret-image"

    record = module.normalize_workflow(unsafe, "2026-08-01T14:02:00Z")

    assert record["image"]["reference"] is None
    assert record["image"]["state"] == "unavailable"
    assert "192.168." not in json.dumps(record)


def test_public_value_rejects_private_hosts_without_dropping_normal_identifiers():
    module = load_module()

    for value in (
        "bluefin",
        "testing",
        "ghcr.io/projectbluefin/bluefin",
        "https://github.com/projectbluefin/lab/blob/main/docs/data/history/qa-runs.ndjson",
    ):
        assert module.public_value(value) == value
    for value in (
        "127.0.0.1",
        "127.1",
        "127.0.1",
        "2130706433",
        "0177.0.0.1",
        "0x7f.0.0.1",
        "0x7f000001",
        "127.1:5000/image",
        "0x7f.1:5000/image",
        "192.168.1.2:5000",
        "[fd00::1]:5000",
        "[fe80::1]:5000",
        "localhost",
        "localhost:5000",
        "10.0.0.2:5000/image",
        "172.20.0.2:5000/image",
        "192.168.1.2:5000/image",
        "169.254.1.2:5000/image",
        "[fd00::1]:5000/image",
        "registry.argo.svc:5000/image",
        "argo-server.argo.svc.cluster.local",
        "https://argo-server.argo.svc.cluster.local/evidence",
        "https://127.0.0.1/evidence",
    ):
        assert module.public_value(value) is None


def test_terminal_snapshot_retains_valid_failed_scenarios_without_a_result_summary():
    module = load_module()
    run = workflow(phase="Failed", finished_at="2026-08-01T14:03:00Z", with_result=True)
    run["status"]["nodes"]["node"]["outputs"]["parameters"] = [
        {"name": "failed-scenarios", "value": '["Shell opens"]'},
    ]

    record = module.normalize_workflow(run, "2026-08-01T14:04:00Z")

    assert record["artifacts"]["results"] == {
        "state": "available",
        "state_reason": None,
        "provenance": "Argo Workflow status failed-scenarios output parameter",
        "failed_scenarios": ["Shell opens"],
    }
    assert record["failure"] == {"state": "failed", "phase": "Failed"}


def test_empty_or_placeholder_result_output_is_not_execution_evidence():
    module = load_module()
    run = workflow(phase="Error", finished_at="2026-08-01T14:03:00Z", with_result=True)
    run["status"]["nodes"]["node"]["outputs"]["parameters"] = [
        {"name": "result", "value": "No results generated"},
        {"name": "failed-scenarios", "value": "[]"},
    ]

    record = module.normalize_workflow(run, "2026-08-01T14:04:00Z")

    assert record["artifacts"]["results"]["state"] == "unavailable"
    assert record["failure"] == {"state": "failed", "phase": "Error"}


def test_malformed_failed_scenarios_are_unavailable_not_a_publisher_error():
    module = load_module()
    run = workflow(phase="Failed", finished_at="2026-08-01T14:03:00Z", with_result=True)
    run["status"]["nodes"]["node"]["outputs"]["parameters"] = [
        {"name": "failed-scenarios", "value": '[{"unsafe":"shape"}]'},
    ]

    record = module.normalize_workflow(run, "2026-08-01T14:04:00Z")

    assert record["artifacts"]["results"]["state"] == "unavailable"
    assert record["failure"] == {"state": "failed", "phase": "Failed"}


def test_desktop_runners_publish_result_summaries_only_from_structured_results():
    import yaml

    for name in ("run-gnome-tests.yaml", "run-kde-tests.yaml"):
        content = (ROOT / "argo/workflow-templates" / name).read_text(encoding="utf-8")
        template = yaml.safe_load(content)["spec"]["templates"][0]
        outputs = {item["name"]: item["valueFrom"] for item in template["outputs"]["parameters"]}

        assert outputs["result"] == {"path": "/tmp/results/result-summary.txt"}
        assert outputs["failed-scenarios"] == {
            "path": "/tmp/results/failed-scenarios.json",
            "default": "[]",
        }
        assert "results.json" in content
        assert "result-summary.txt" in content

def test_image_tag_defines_the_matrix_branch_before_source_branch():
    module = load_module()
    run = workflow(phase="Running")
    run["spec"]["arguments"]["parameters"].append({"name": "branch", "value": "main"})
    record = module.normalize_workflow(run, "2026-08-01T14:02:00Z")

    assert record["lane"]["branch"] == "testing"


def test_multi_suite_parent_emits_per_suite_task_node_evidence():
    module = load_module()
    parent = workflow(phase="Failed", finished_at="2026-08-01T14:04:00Z")
    parent["status"]["nodes"] = {
        "smoke": {
            "displayName": "run-container-tests",
            "phase": "Succeeded",
            "startedAt": "2026-08-01T14:01:00Z",
            "finishedAt": "2026-08-01T14:02:00Z",
            "inputs": {"parameters": [{"name": "suite", "value": "smoke"}]},
            "outputs": {"parameters": [{"name": "result", "value": "smoke passed"}]},
        },
        "system": {
            "displayName": "run-container-tests",
            "phase": "Failed",
            "startedAt": "2026-08-01T14:01:00Z",
            "finishedAt": "2026-08-01T14:04:00Z",
            "inputs": {"parameters": [{"name": "suite", "value": "system"}]},
        },
    }

    records = module.normalize_workflows(parent, "2026-08-01T14:05:00Z")
    by_suite = {record["lane"]["suite"]: record for record in records}

    assert set(by_suite) == {"smoke", "system"}
    assert by_suite["smoke"]["lane"]["branch"] == "testing"
    assert by_suite["smoke"]["lifecycle"]["phase"] == "Succeeded"
    assert by_suite["system"]["lifecycle"]["phase"] == "Failed"
    assert by_suite["system"]["failure"] == {"state": "failed", "phase": "Failed"}


def test_multi_suite_parent_without_suite_nodes_is_excluded():
    module = load_module()

    assert module.normalize_workflows(
        workflow(phase="Running"),
        "2026-08-01T14:02:00Z",
    ) == []


def test_schema_rejects_empty_nested_objects_and_contradictory_lifecycle():
    import pytest

    Draft202012Validator = pytest.importorskip("jsonschema").Draft202012Validator
    module = load_module()
    single_suite = workflow(phase="Succeeded", finished_at="2026-08-01T14:03:00Z", with_result=True)
    for parameter in single_suite["spec"]["arguments"]["parameters"]:
        if parameter["name"] == "suites":
            parameter["value"] = "smoke"
    record = module.normalize_workflow(single_suite, "2026-08-01T14:04:00Z")
    schema = json.loads((ROOT / "schemas/v2/qa-run.schema.json").read_text())
    validator = Draft202012Validator(schema)

    assert not list(validator.iter_errors(record))

    empty_lane = json.loads(json.dumps(record))
    empty_lane["lane"] = {}
    assert list(validator.iter_errors(empty_lane))

    contradictory = json.loads(json.dumps(record))
    contradictory["lifecycle"]["state"] = "running"
    assert list(validator.iter_errors(contradictory))

    unsafe_scenarios = json.loads(json.dumps(record))
    unsafe_scenarios["artifacts"]["results"]["failed_scenarios"] = ["x"] * 21
    assert list(validator.iter_errors(unsafe_scenarios))

    with pytest.raises(module.RecordError):
        module.validate_record(unsafe_scenarios)

    sensitive_scenarios = json.loads(json.dumps(record))
    sensitive_scenarios["artifacts"]["results"]["failed_scenarios"] = ["Reset password"]
    assert list(validator.iter_errors(sensitive_scenarios))

    with pytest.raises(module.RecordError):
        module.validate_record(sensitive_scenarios)

    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert "workflow_uid" in schema["required"]


def test_qa_run_derivation_fixture_conforms_to_the_contract():
    import pytest

    Draft202012Validator = pytest.importorskip("jsonschema").Draft202012Validator
    schema = json.loads((ROOT / "schemas/v2/qa-run.schema.json").read_text())
    validator = Draft202012Validator(schema)
    fixture = ROOT / "tests/unit/fixtures/qa-run-derivation.ndjson"

    for line in fixture.read_text().splitlines():
        assert not list(validator.iter_errors(json.loads(line)))
