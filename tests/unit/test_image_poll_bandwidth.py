from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]


def load_manifest(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())


def test_bluefin_testing_has_one_image_digest_parameter():
    manifest = load_manifest("manifests/image-poll-bluefin-testing.yaml")
    parameters = manifest["spec"]["workflowSpec"]["arguments"]["parameters"]
    names = [parameter["name"] for parameter in parameters]
    assert names.count("image-digest") == 1


def test_aurora_watchers_use_distinct_state_keys():
    kinoite = (ROOT / "manifests/image-poll-kinoite-44.yaml").read_text()
    akmods = (ROOT / "manifests/image-poll-akmods-44.yaml").read_text()

    assert 'STATE_KEY="digest-kinoite-44"' in kinoite
    assert 'STATE_KEY="digest-akmods-main-44"' in akmods
    assert 'STATE_KEY="digest-kinoite-44"' not in akmods
    assert 'STATE_KEY="digest-akmods-main-44"' not in kinoite


def test_image_poller_serializes_each_state_key():
    template = load_manifest("argo/workflow-templates/image-poller.yaml")
    mutexes = template["spec"]["synchronization"]["mutexes"]
    assert mutexes == [{"name": "image-poll-{{workflow.parameters.state-key}}"}]


def test_container_runner_preserves_digest_pinning_and_has_no_extra_closer():
    source = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text()
    assert 'TARGET_IMAGE="${IMAGE_REPO}@${IMAGE_DIGEST}"' in source
    assert 'podman pull "${PODMAN_PULL_TLS_ARGS[@]}" "${TARGET_IMAGE}"' in source
    assert source.count("        fi\n        podman run") == 1


def test_nightly_workflows_pass_explicit_digest_contract():
    for name in ("manifests/nightly-smoke-stable.yaml", "manifests/nightly-smoke-lts.yaml"):
        manifest = load_manifest(name)
        parameters = manifest["spec"]["workflowSpec"]["arguments"]["parameters"]
        assert {"name": "image-digest", "value": ""} in parameters
