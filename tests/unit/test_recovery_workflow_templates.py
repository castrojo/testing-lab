from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_template(name: str) -> dict:
    return yaml.safe_load(
        (ROOT / "argo" / "workflow-templates" / name).read_text(encoding="utf-8")
    )


def parameter_names(template: dict) -> set[str]:
    return {
        parameter["name"]
        for parameter in template["spec"]["arguments"]["parameters"]
    }


def template_names(template: dict) -> set[str]:
    return {item["name"] for item in template["spec"]["templates"]}


def test_toggle_testing_rebase_is_gitops_managed_and_covers_both_directions():
    template = load_template("toggle-testing-rebase.yaml")

    assert template["metadata"]["name"] == "toggle-testing-rebase"
    assert "description" in template["metadata"]["annotations"]
    assert {
        "image",
        "disk-image",
        "start-tag",
        "target-tag",
        "namespace",
        "test-root",
        "ssh-key-secret",
    } <= parameter_names(template)
    assert template["spec"]["onExit"] == "teardown"
    assert template["spec"]["volumeClaimGC"]["strategy"] == "OnWorkflowCompletion"
    assert {
        "run-toggle-testing",
        "reboot-and-wait",
        "verify-bootc-state",
        "teardown",
    } <= template_names(template)

    steps = template["spec"]["templates"][0]["steps"]
    step_names = [group[0]["name"] for group in steps]
    assert step_names == [
        "ensure-disk",
        "provision",
        "toggle-forward",
        "reboot-forward",
        "verify-forward",
        "toggle-back",
        "reboot-back",
        "verify-back",
        "emit-telemetry",
        "publish-telemetry",
    ]
    content = (ROOT / "argo" / "workflow-templates" / "toggle-testing-rebase.yaml").read_text(
        encoding="utf-8"
    )
    assert "bootc switch" not in content
    assert content.count("workflow.parameters.disk-image") >= 2
    assert content.count("workflow.parameters.test-root") >= 3


def test_migration_upgrade_wraps_migration_sequence_with_disk_build():
    template = load_template("migration-upgrade-test.yaml")

    assert template["metadata"]["name"] == "migration-upgrade-test"
    assert "description" in template["metadata"]["annotations"]
    assert {
        "legacy-image",
        "legacy-image-tag",
        "chunkah-image",
        "chunkah-image-tag",
        "storage-variant",
        "namespace",
        "golden-root",
        "test-root",
        "ssh-key-secret",
    } <= parameter_names(template)
    assert template["spec"]["onExit"] == "teardown"
    assert template["spec"]["volumeClaimGC"]["strategy"] == "OnWorkflowCompletion"
    steps = template["spec"]["templates"][0]["steps"]
    assert [group[0]["name"] for group in steps] == [
        "ensure-disk",
        "provision",
        "migration",
    ]
    assert steps[-1][0]["templateRef"]["name"] == "bluefin-migration-test"
    assert steps[-1][0]["templateRef"]["template"] == "migration-sequence"
    content = (ROOT / "argo" / "workflow-templates" / "migration-upgrade-test.yaml").read_text(
        encoding="utf-8"
    )
    assert content.count("workflow.parameters.golden-root") >= 1
    assert content.count("workflow.parameters.test-root") >= 2
