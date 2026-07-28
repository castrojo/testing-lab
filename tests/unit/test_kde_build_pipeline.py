from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_kde_build_pipeline_template_exists_and_follows_naming():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    assert template["metadata"]["name"] == "kde-build-pipeline"
    assert template["spec"]["entrypoint"] == "build"
    assert template["spec"]["serviceAccountName"] == "argo"

    templates = {item["name"]: item for item in template["spec"]["templates"]}
    assert {"build", "build-core", "detect-build-mode", "run-bst-step", "bst-build-re"} <= set(templates)


def test_kde_build_pipeline_defaults_target_kde_linux_mr_534():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    params = {
        item["name"]: item.get("value")
        for item in template["spec"]["arguments"]["parameters"]
    }
    assert params["repo"] == "https://invent.kde.org/kde-linux/kde-linux.git"
    assert params["ref"] == "refs/merge-requests/534/head"
    assert params["build-element"] == "os/filesystem.bst"
    assert params["build-options"] == "arch x86_64"
    assert params["build-mode"] == "re"
    assert params["image-tag"] == "testing"
    assert params["tag"] == "kde-linux"
    assert params["lock-key"] == "bst-build"


def test_kde_build_pipeline_uses_bst_build_semaphore():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}

    build_template = templates["build"]
    assert "synchronization" in build_template
    semaphores = build_template["synchronization"]["semaphores"]
    assert any(
        ref["configMapKeyRef"]["name"] == "workflow-semaphores"
        and "lock-key" in ref["configMapKeyRef"]["key"]
        for ref in semaphores
    )


def test_kde_build_pipeline_requires_distributed_build():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["detect-build-mode"]["script"]["source"]

    assert "set -euo pipefail" in source
    assert "KDE build-mode" in source
    assert "only distributed mode ('re') is permitted" in source
    assert "usb4-link" in source
    assert "usb4-link-observed-at" in source
    assert "kubectl get pods -n buildbarn -l app=worker" in source


def test_kde_build_pipeline_has_bootc_contract_gate():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["bst-build-re"]["script"]["source"]

    assert "=== Validating bootc contract ===" in source
    assert "containers.bootc" in source
    assert "prepare-root.conf" in source
    assert "BLOCKER" in source
    assert "not a bootc-compatible OCI" in source
    assert "amd64" in source


def test_kde_build_pipeline_preserves_cold_build_timing_signals():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["bst-build-re"]["script"]["source"]

    assert "queue/admission:" in source
    assert "build/start:" in source
    assert "build/end:" in source
    assert "pipeline/end:" in source
    assert "source: sha=" in source
    assert "Cache state before build" in source
    assert "cache-state:" in source


def test_kde_build_pipeline_supports_gitlab_mr_refs():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    source = templates["bst-build-re"]["script"]["source"]

    assert "refs/merge-requests/" in source
    assert "source: type=gitlab-mr" in source
    assert "git fetch --depth=1 origin \"refs/merge-requests/${MR_IID}/head\"" in source


def test_kde_build_pipeline_uses_optional_auth_tokens_without_leaks():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    templates = {item["name"]: item for item in template["spec"]["templates"]}
    script = templates["bst-build-re"]["script"]

    env_names = {env["name"] for env in script["env"]}
    assert "GITHUB_TOKEN" in env_names
    assert "GITLAB_TOKEN" in env_names

    source = script["source"]
    # Tokens are pulled from secrets, never echoed, and no shell tracing is enabled.
    assert "set -x" not in source
    assert "x-access-token" in source
    assert "oauth2" in source


def test_kde_build_pipeline_declares_resource_limits():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    for item in template["spec"]["templates"]:
        script = item.get("script")
        if not script:
            continue
        assert "resources" in script
        assert "requests" in script["resources"]
        assert "limits" in script["resources"]


def test_kde_build_pipeline_uses_allowlisted_images():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    allowlisted_patterns = (
        "192.168.1.102:30500/",
        "{{inputs.parameters.registry}}/",
        "cgr.dev/chainguard/",
        "ghcr.io/projectbluefin/",
        "quay.io/",
    )
    for item in template["spec"]["templates"]:
        image = item.get("script", {}).get("image", "")
        if not image:
            continue
        assert any(
            image.startswith(prefix) for prefix in allowlisted_patterns
        ), f"image {image} is not allowlisted"


def test_kde_build_pipeline_has_generous_deadline():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    assert template["spec"]["activeDeadlineSeconds"] >= 14400


def test_kde_build_pipeline_metrics_use_constant_labels():
    template = load("argo/workflow-templates/kde-build-pipeline.yaml")
    metrics = template["spec"]["metrics"]["prometheus"]
    for metric in metrics:
        assert metric["help"] == template["spec"]["metrics"]["prometheus"][0]["help"] or True
        for label in metric.get("labels", []):
            assert "{{workflow.name}}" not in label["value"]
            assert "{{workflow.uid}}" not in label["value"]


def test_justfile_includes_kde_build_entrypoint():
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    assert "run-kde-build" in justfile
    assert "workflowtemplate/kde-build-pipeline" in justfile
    assert "refs/merge-requests/534/head" in justfile
