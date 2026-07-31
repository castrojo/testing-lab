from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
PIPELINE_PATH = ROOT / "argo/workflow-templates/dakota-publish-pipeline.yaml"
CRON_PATH = ROOT / "manifests/nightly-dakota-publish.yaml"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def template_named(workflow, name):
    return next(template for template in workflow["spec"]["templates"] if template["name"] == name)


def test_publish_pipeline_has_two_independent_gated_lanes():
    workflow = load_yaml(PIPELINE_PATH)
    pipeline = template_named(workflow, "publish-all")
    tasks = {task["name"]: task for task in pipeline["dag"]["tasks"]}

    assert workflow["metadata"]["name"] == "dakota-publish-pipeline"
    assert workflow["spec"]["activeDeadlineSeconds"] == 14400
    assert pipeline["synchronization"]["semaphores"][0]["configMapKeyRef"] == {
        "name": "workflow-semaphores",
        "key": "dakota-publish",
    }
    assert tasks["publish-dakota"]["arguments"]["parameters"][0]["value"] == "dakota"
    assert (
        tasks["publish-dakota-nvidia"]["arguments"]["parameters"][0]["value"]
        == "dakota-nvidia"
    )
    assert "publish-dakota.Failed" in tasks["verify-lane-results"]["depends"]
    assert "publish-dakota-nvidia.Failed" in tasks["verify-lane-results"]["depends"]


def test_publish_lane_is_disabled_and_carries_no_ghcr_credentials():
    workflow = load_yaml(PIPELINE_PATH)
    lane = template_named(workflow, "publish-lane")
    source = lane["script"]["source"]

    assert lane["retryStrategy"]["limit"] == "2"
    assert lane["retryStrategy"]["retryPolicy"] == "Always"
    assert lane["retryStrategy"]["backoff"]["maxDuration"] == "2m"
    assert lane["script"]["image"].startswith("quay.io/skopeo/stable@sha256:")
    assert "ghcr-publish-auth" not in str(workflow)
    assert "ghcr-publish-auth-secret-type" not in str(
        workflow["metadata"].get("annotations", {})
    )
    for template in workflow["spec"]["templates"]:
        for volume in template.get("volumes", []) or []:
            assert "secret" not in volume, (
                f"{template['name']}: unexpected secret volume {volume}"
            )
    assert "192.168.1.102:30500" in str(lane["script"]["env"])
    assert "GHCR push destination is forbidden" in source
    assert "exit 1" in source
    assert "skopeo copy" not in source
    assert "oras" not in source
    assert "set -x" not in source
    assert "set -eux" not in source


def test_publish_lane_handles_oci_referrers_explicitly():
    source = template_named(load_yaml(PIPELINE_PATH), "publish-lane")["script"]["source"]

    assert "oras discover" not in source
    assert "oras cp" not in source
    assert "skopeo copy" not in source
    assert "GHCR push destination is forbidden" in source


def test_publish_pipeline_persists_compact_history_from_on_exit():
    workflow = load_yaml(PIPELINE_PATH)
    history = template_named(workflow, "publish-run-history")
    source = history["script"]["source"]
    env = {item["name"]: item for item in history["script"]["env"]}

    assert workflow["spec"]["onExit"] == "publish-run-history"
    assert env["WORKFLOW_STATUS"]["value"] == "{{workflow.status}}"
    assert env["STARTED_AT"]["value"] == "{{workflow.creationTimestamp.RFC3339}}"
    assert env["GITHUB_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "github-token",
        "key": "token",
    }
    assert "publish_dakota_run.py publish" in source
    assert 'kind: "publish"' in source
    assert 'status: "failed"' in source
    assert 'failure_class: "publish"' in source
    assert "raw.githubusercontent.com/projectbluefin/lab/main/scripts/publish_dakota_run.py" in source


def test_publish_history_failure_is_visible_and_credentials_are_not_logged():
    source = template_named(load_yaml(PIPELINE_PATH), "publish-run-history")["script"][
        "source"
    ]

    assert "Dakota publish history persistence failed" in source
    assert "|| true" not in source
    assert "set -x" not in source
    assert "set -eux" not in source
    assert "x-access-token" not in source
    assert "${GITHUB_TOKEN}" not in source


def test_nightly_publish_schedule_and_manual_entrypoint():
    cron = load_yaml(CRON_PATH)
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    assert cron["metadata"]["name"] == "nightly-dakota-publish"
    assert cron["spec"]["suspend"] is True
    assert cron["spec"]["schedules"] == ["0 21 * * *"]
    assert cron["spec"]["timezone"] == "UTC"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert (
        cron["spec"]["workflowSpec"]["workflowTemplateRef"]["name"]
        == "dakota-publish-pipeline"
    )
    assert "run-dakota-publish:" in justfile
    assert "--from workflowtemplate/dakota-publish-pipeline" in justfile


def test_publish_secret_is_a_contract_not_a_committed_secret():
    committed_secrets = []
    for path in (ROOT / "manifests").glob("*.yaml"):
        manifests = yaml.safe_load_all(path.read_text(encoding="utf-8"))
        for manifest in manifests:
            if isinstance(manifest, dict) and manifest.get("kind") == "Secret":
                if manifest.get("metadata", {}).get("name") == "ghcr-publish-auth":
                    committed_secrets.append(path.name)

    assert not committed_secrets


def test_publish_history_never_emits_digestless_success_record():
    """
    publish_dakota_run.py rejects passed publish records without a digest.

    GHCR publication is disabled, so no lane can produce a destination digest.
    The onExit history template must therefore never build a success-shaped
    record; it skips history entirely on a Succeeded workflow status.
    """
    source = template_named(load_yaml(PIPELINE_PATH), "publish-run-history")["script"][
        "source"
    ]

    assert 'status: "passed"' not in source
    assert "--arg digest" not in source
    assert 'if [ "${WORKFLOW_STATUS}" = Succeeded ]; then' in source
    skip_index = source.index("skipping publish history")
    publish_index = source.index("publish_dakota_run.py publish")
    assert skip_index < publish_index
    assert "exit 0" in source


def test_nightly_publish_cron_suspension_does_not_affect_other_schedules():
    """Only the GHCR publisher is suspended; unrelated crons keep running."""
    active = []
    for path in sorted((ROOT / "manifests").glob("*.yaml")):
        for manifest in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(manifest, dict) or manifest.get("kind") != "CronWorkflow":
                continue
            name = manifest["metadata"]["name"]
            if manifest["spec"].get("suspend") is not True:
                active.append(name)

    assert "nightly-dakota-publish" not in active
    assert active, "expected unrelated CronWorkflows to remain active"
