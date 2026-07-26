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


def test_publish_lane_preserves_and_verifies_digest_without_logging_credentials():
    workflow = load_yaml(PIPELINE_PATH)
    lane = template_named(workflow, "publish-lane")
    source = lane["script"]["source"]
    secret = lane["volumes"][0]["secret"]

    assert lane["retryStrategy"]["limit"] == "2"
    assert lane["retryStrategy"]["retryPolicy"] == "Always"
    assert lane["retryStrategy"]["backoff"]["maxDuration"] == "2m"
    assert lane["script"]["image"].startswith("quay.io/skopeo/stable@sha256:")
    assert secret["secretName"] == "ghcr-publish-auth"
    assert secret["items"][0]["key"] == ".dockerconfigjson"
    assert (
        workflow["metadata"]["annotations"][
            "bluefin.io/ghcr-publish-auth-secret-type"
        ]
        == "kubernetes.io/dockerconfigjson"
    )
    assert "192.168.1.102:30500" in str(lane["script"]["env"])
    assert '"docker://${SOURCE_REGISTRY}/${IMAGE}@${SOURCE_DIGEST}"' in source
    assert 'if [ "${DESTINATION_DIGEST}" != "${SOURCE_DIGEST}" ]' in source
    assert "set -x" not in source
    assert "set -eux" not in source


def test_publish_lane_handles_oci_referrers_explicitly():
    source = template_named(load_yaml(PIPELINE_PATH), "publish-lane")["script"]["source"]

    assert "oras discover" in source
    assert "oras cp" in source
    assert "--recursive" in source
    assert "ORAS_VERSION=1.2.3" in source
    assert "oras_${ORAS_VERSION}_linux_amd64.tar.gz" in source
    assert "OCI referrers: none present" in source
    assert "OCI referrers: discovery unavailable" in source
    assert "OCI referrers: ORAS bootstrap unavailable" in source


def test_nightly_publish_schedule_and_manual_entrypoint():
    cron = load_yaml(CRON_PATH)
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    assert cron["metadata"]["name"] == "nightly-dakota-publish"
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
