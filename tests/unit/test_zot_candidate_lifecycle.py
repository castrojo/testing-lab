import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
ZOT_PATH = ROOT / "manifests/zot-writable.yaml"
GC_PATH = ROOT / "manifests/pr-image-gc.yaml"
WORKFLOW_PATH = ROOT / "argo/workflow-templates/zot-candidate-lifecycle.yaml"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def template_named(workflow, name):
    return next(template for template in workflow["spec"]["templates"] if template["name"] == name)


def test_zot_stages_auth_without_breaking_anonymous_writers():
    manifests = list(yaml.safe_load_all(ZOT_PATH.read_text(encoding="utf-8")))
    config_map, deployment = manifests[:2]
    active = json.loads(config_map["data"]["config.json"])
    authenticated = json.loads(config_map["data"]["config-authenticated.json"])
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    config_mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "config")
    auth_volume = next(volume for volume in deployment["spec"]["template"]["spec"]["volumes"] if volume["name"] == "auth")

    assert "auth" not in active["http"]
    assert config_mount["subPath"] == "config.json"
    assert auth_volume["secret"]["optional"] is True
    assert authenticated["http"]["auth"]["htpasswd"]["path"] == "/etc/zot/auth/htpasswd"
    policy = authenticated["http"]["accessControl"]["repositories"]["**"]
    assert policy["anonymousPolicy"] == ["read"]
    assert policy["defaultPolicy"] == ["read"]
    assert policy["policies"] == [
        {
            "users": ["zot-writer"],
            "actions": ["read", "create", "update", "delete", "detectManifestCollision"],
        }
    ]
    assert authenticated["http"]["accessControl"]["metrics"]["users"] == ["zot-metrics"]
    assert authenticated["storage"] == active["storage"]


def test_zot_retention_is_bounded_and_preserves_testing():
    config_map = next(yaml.safe_load_all(ZOT_PATH.read_text(encoding="utf-8")))
    config = json.loads(config_map["data"]["config.json"])
    storage = config["storage"]
    policy = storage["retention"]["policies"][0]

    assert storage["gc"] is True
    assert storage["gcInterval"] == "24h"
    assert storage["retention"]["dryRun"] is False
    assert policy["repositories"] == ["dakota", "dakota-nvidia"]
    assert policy["keepTags"][0] == {"patterns": ["^testing$"]}
    assert policy["keepTags"][1]["mostRecentlyPushedCount"] == 10
    assert policy["keepTags"][2]["mostRecentlyPushedCount"] == 10


def test_candidate_lifecycle_enforces_preflight_and_digest_promotion():
    workflow = load_yaml(WORKFLOW_PATH)
    preflight = template_named(workflow, "candidate-preflight")
    promotion = template_named(workflow, "promote-candidate")
    entrypoint = template_named(workflow, "promote")
    preflight_source = preflight["script"]["source"]
    promotion_source = promotion["script"]["source"]

    assert "candidate-([0-9a-f]{40}|[0-9a-f]{64})" in preflight_source
    assert "immutable candidate already exists" in preflight_source
    assert "MIN_FREE_BYTES" in preflight_source
    assert "MIN_FREE_PERCENT" in preflight_source
    assert preflight["volumes"][0]["hostPath"]["type"] == "Directory"
    assert preflight["script"]["volumeMounts"][0]["readOnly"] is True
    assert preflight["script"]["image"].startswith(
        "ghcr.io/oras-project/oras:v1.2.3@sha256:"
    )
    assert "if ((" not in preflight_source

    assert '"${SUBJECT}" > /work/pullback-manifest.json' in promotion_source
    assert 'PULLBACK_DIGEST="sha256:$(sha256sum' in promotion_source
    assert '"${TESTING_REF}"' in promotion_source
    assert 'if [ "${TESTING_DIGEST}" != "${EXPECTED_DIGEST}" ]' in promotion_source
    assert "/bin/oras attach" in promotion_source
    assert "/bin/oras discover" in promotion_source
    assert promotion["script"]["image"].startswith(
        "ghcr.io/oras-project/oras:v1.2.3@sha256:"
    )
    assert "application/vnd.projectbluefin.lab.promotion-evidence.v1+json" in str(
        workflow["metadata"]["annotations"]
    )
    assert "set -x" not in promotion_source
    assert "set -eux" not in promotion_source
    assert workflow["spec"]["entrypoint"] == "promote"
    assert entrypoint["dag"]["tasks"][0]["template"] == "promote-candidate"


def test_writer_credentials_are_optional_contracts_not_committed_secrets():
    workflow = load_yaml(WORKFLOW_PATH)
    promotion = template_named(workflow, "promote-candidate")
    secret = next(volume["secret"] for volume in promotion["volumes"] if volume["name"] == "zot-auth")

    assert secret["secretName"] == "zot-writer-auth"
    assert secret["optional"] is True
    assert secret["items"] == [{"key": ".dockerconfigjson", "path": "config.json"}]

    committed = []
    for path in (ROOT / "manifests").glob("*.yaml"):
        for manifest in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(manifest, dict) and manifest.get("kind") == "Secret":
                if manifest.get("metadata", {}).get("name") in {"zot-auth", "zot-writer-auth"}:
                    committed.append(path.name)
    assert not committed


def test_daily_gc_is_auth_ready():
    gc = load_yaml(GC_PATH)
    template = gc["spec"]["workflowSpec"]["templates"][0]
    secret = template["volumes"][0]["secret"]
    source = template["script"]["source"]

    assert gc["spec"]["schedules"] == ["0 3 * * *"]
    assert secret["secretName"] == "zot-writer-auth"
    assert secret["optional"] is True
    assert 'ORAS_AUTH=(--registry-config /auth/config.json)' in source
    assert 'oras manifest delete "${REGISTRY}/${REPO}:${TAG}" --plain-http "${ORAS_AUTH[@]}"' in source


def test_daily_gc_bootstrap_does_not_require_tar():
    gc = load_yaml(GC_PATH)
    source = gc["spec"]["workflowSpec"]["templates"][0]["script"]["source"]

    assert 'curl -sfL "https://github.com/oras-project/oras/releases/download/v1.2.3/oras_1.2.3_linux_amd64.tar.gz"' in source
    assert '-o "${ORAS_DIR}/oras.tar.gz"' in source
    assert 'python3 -m tarfile -e "${ORAS_DIR}/oras.tar.gz" "${ORAS_DIR}"' in source
    assert " | tar xz " not in source
