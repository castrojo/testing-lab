"""Unit coverage for scripts/check_gitops_policy.py.

`check_gitops_policy.py` is the sole producer of `docs/data/policy-compliance.json`,
the dataset the compliance page renders and `scripts/generate_page_datasets.py`
consumes. It had no unit tests at all, so the registry allowlist, the hostPath
scanner, the node-pin scanner and the compliance-score arithmetic could all
regress silently — a broken parser would simply report a 100% score with zero
checks instead of failing.

These tests cover the pure helpers (`run_cmd`, `extract_images_from_yaml`,
`is_registry_allowed`) and drive `main()` end to end against a temporary
repository tree with the live `kubectl` call stubbed.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "scripts/check_gitops_policy.py"

_spec = importlib.util.spec_from_file_location("check_gitops_policy", POLICY)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


# --------------------------------------------------------------------------
# run_cmd
# --------------------------------------------------------------------------


def test_run_cmd_returns_stripped_stdout():
    assert policy.run_cmd("printf '  hello  \\n'") == "hello"


def test_run_cmd_returns_none_on_failure():
    """Any failure (non-zero exit, timeout, missing binary) degrades to None."""
    assert policy.run_cmd("exit 7") is None


# --------------------------------------------------------------------------
# extract_images_from_yaml
# --------------------------------------------------------------------------


def test_extract_images_collects_plain_and_quoted_references():
    content = (
        "spec:\n"
        "  containers:\n"
        "    - image: ghcr.io/projectbluefin/bluefin:latest\n"
        '    - image: "quay.io/fedora/fedora:41"\n'
        "    - image: 'registry.k8s.io/pause:3.9'\n"
    )
    assert policy.extract_images_from_yaml(content) == [
        "ghcr.io/projectbluefin/bluefin:latest",
        "quay.io/fedora/fedora:41",
        "registry.k8s.io/pause:3.9",
    ]


def test_extract_images_skips_commented_lines():
    content = "  # image: docker.io/library/nginx:latest\n  image: ghcr.io/ok/app:1"
    assert policy.extract_images_from_yaml(content) == ["ghcr.io/ok/app:1"]


@pytest.mark.parametrize("marker", ["image-lint-ignore", "registry-lint-ignore"])
def test_extract_images_honours_inline_lint_ignore_markers(marker):
    content = f"  image: docker.io/library/nginx:latest # {marker}"
    assert policy.extract_images_from_yaml(content) == []


def test_extract_images_skips_helm_templates_and_prose():
    content = (
        "  image: {{ .Values.image }}\n"
        "  image: some }} broken template\n"
        "  image: declares the upstream image\n"
        "  image: $IMAGE_REF\n"
        "  image: ghcr.io/real/app:1\n"
    )
    assert policy.extract_images_from_yaml(content) == ["ghcr.io/real/app:1"]


def test_extract_images_returns_empty_for_manifest_without_images():
    assert policy.extract_images_from_yaml("kind: ConfigMap\ndata:\n  a: b\n") == []


# --------------------------------------------------------------------------
# is_registry_allowed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/projectbluefin/bluefin:latest",
        "quay.io/fedora/fedora:41",
        "registry.fedoraproject.org/fedora:41",
        "registry.k8s.io/pause:3.9",
        "cgr.dev/chainguard/static:latest",
        "192.168.1.102/local/app:dev",
        "localhost/local/app:dev",
        "localhost:5000/local/app:dev",
    ],
)
def test_is_registry_allowed_accepts_allowlisted_registries(image):
    assert policy.is_registry_allowed(image) is True


@pytest.mark.parametrize(
    "image",
    [
        "docker.io/library/nginx:latest",
        "index.docker.io/library/nginx:latest",
        "gcr.io/distroless/static:nonroot",
        "library/nginx:latest",
        "nginx",
        "nginx:1.27",
    ],
)
def test_is_registry_allowed_rejects_everything_else(image):
    assert policy.is_registry_allowed(image) is False


def test_is_registry_allowed_exempts_explicitly_ignored_images():
    """The ROCm device plugin is the one sanctioned docker.io exception."""
    assert "docker.io/rocm/k8s-device-plugin" in policy.IGNORED_IMAGES
    assert policy.is_registry_allowed("docker.io/rocm/k8s-device-plugin") is True
    # The exemption is exact-match only; a tagged variant is still a violation.
    assert policy.is_registry_allowed("docker.io/rocm/k8s-device-plugin:1.0") is False


def test_is_registry_allowed_strips_registry_port_before_matching():
    assert policy.is_registry_allowed("ghcr.io:443/projectbluefin/app:1") is True


# --------------------------------------------------------------------------
# main() — git manifest scan
# --------------------------------------------------------------------------


def _run_main(tmp_path, monkeypatch, manifests=None, kubectl=None):
    """Execute main() inside an isolated repo tree and return the report."""
    for relpath, body in (manifests or {}).items():
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    monkeypatch.setattr(policy, "run_cmd", lambda cmd: kubectl)
    monkeypatch.chdir(tmp_path)
    policy.main()

    report = json.loads(
        (tmp_path / "docs/data/policy-compliance.json").read_text(encoding="utf-8")
    )
    return report, {rule["id"]: rule for rule in report["rules"]}


def test_main_writes_the_expected_report_envelope(tmp_path, monkeypatch):
    report, rules = _run_main(tmp_path, monkeypatch)

    assert report["schema_version"] == "v1"
    assert report["_meta"]["page"] == "compliance"
    assert report["_meta"]["generated_at"].endswith("Z")
    assert set(rules) == {
        "registry_allowlist_git",
        "registry_allowlist_cluster",
        "no_root_storage_git",
        "no_hard_node_pins_git",
        "no_hard_node_pins_cluster",
    }


def test_main_tolerates_missing_manifest_directories(tmp_path, monkeypatch):
    report, rules = _run_main(tmp_path, monkeypatch)

    assert report["_meta"]["git_manifests_scanned"] == 0
    assert report["score"] == 100.0
    assert all(rule["status"] == "passed" for rule in rules.values())


def test_main_scans_both_manifest_roots(tmp_path, monkeypatch):
    manifests = {
        "manifests/app.yaml": "  image: ghcr.io/ok/app:1\n",
        "manifests/nested/other.yaml": "  image: quay.io/ok/app:1\n",
        "argo/workflow-templates/wt.yaml": "  image: ghcr.io/ok/wt:1\n",
    }
    report, rules = _run_main(tmp_path, monkeypatch, manifests=manifests)

    assert report["_meta"]["git_manifests_scanned"] == 3
    assert rules["registry_allowlist_git"]["total_checked"] == 3
    assert rules["registry_allowlist_git"]["status"] == "passed"


def test_main_flags_banned_registry_in_git_manifest(tmp_path, monkeypatch):
    manifests = {
        "manifests/app.yaml": "  image: ghcr.io/ok/app:1\n  image: docker.io/library/nginx:1\n"
    }
    _, rules = _run_main(tmp_path, monkeypatch, manifests=manifests)

    rule = rules["registry_allowlist_git"]
    assert rule["status"] == "failed"
    assert rule["total_checked"] == 2
    assert rule["violations_count"] == 1
    assert rule["violations"][0]["source"] == "git:manifests/app.yaml"
    assert "docker.io/library/nginx:1" in rule["violations"][0]["detail"]


def test_main_flags_unapproved_hostpath_and_allows_sanctioned_prefixes(
    tmp_path, monkeypatch
):
    manifests = {
        "manifests/bad.yaml": (
            "volumes:\n"
            "  - name: data\n"
            "    hostPath:\n"
            "      path: /srv/data\n"
        ),
        "manifests/good.yaml": (
            "volumes:\n"
            "  - name: logs\n"
            "    hostPath:\n"
            "      path: /var/log/pods\n"
            "  - name: ghost\n"
            "    hostPath:\n"
            "      path: /var/mnt/ghost-data\n"
        ),
    }
    _, rules = _run_main(tmp_path, monkeypatch, manifests=manifests)

    rule = rules["no_root_storage_git"]
    assert rule["total_checked"] == 3
    assert rule["violations_count"] == 1
    assert rule["violations"][0]["source"] == "git:manifests/bad.yaml"
    assert "/srv/data" in rule["violations"][0]["detail"]


def test_main_flags_hard_node_pin_in_git_manifest(tmp_path, monkeypatch):
    manifests = {
        "manifests/pinned.yaml": (
            "kind: Deployment\n"
            '  nodeSelector: {"kubernetes.io/hostname": "node-a"}\n'
        )
    }
    _, rules = _run_main(tmp_path, monkeypatch, manifests=manifests)

    rule = rules["no_hard_node_pins_git"]
    assert rule["status"] == "failed"
    assert rule["violations_count"] == 1
    assert "node-a" in rule["violations"][0]["detail"]


def test_main_allows_hard_node_pin_inside_a_daemonset(tmp_path, monkeypatch):
    manifests = {
        "manifests/ds.yaml": (
            "kind: DaemonSet\n"
            '  nodeSelector: {"kubernetes.io/hostname": "node-a"}\n'
        )
    }
    _, rules = _run_main(tmp_path, monkeypatch, manifests=manifests)

    rule = rules["no_hard_node_pins_git"]
    assert rule["status"] == "passed"
    assert rule["total_checked"] == 1
    assert rule["violations_count"] == 0


def test_main_reports_but_survives_an_unreadable_manifest(tmp_path, monkeypatch):
    manifests = {"manifests/app.yaml": "  image: ghcr.io/ok/app:1\n"}
    (tmp_path / "manifests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "manifests/broken.yaml").write_bytes(b"\xff\xfe image: bad\n")

    report, rules = _run_main(tmp_path, monkeypatch, manifests=manifests)

    assert report["_meta"]["git_manifests_scanned"] == 2
    assert rules["registry_allowlist_git"]["status"] == "passed"


# --------------------------------------------------------------------------
# main() — live cluster scan
# --------------------------------------------------------------------------


def _pods(*items):
    return json.dumps({"items": list(items)})


def _pod(name, namespace, images, node_selector=None, owner_kind=None):
    spec = {"containers": [{"image": img} for img in images]}
    if node_selector:
        spec["nodeSelector"] = node_selector
    metadata = {"name": name, "namespace": namespace}
    if owner_kind:
        metadata["ownerReferences"] = [{"kind": owner_kind}]
    return {"metadata": metadata, "spec": spec}


def test_main_marks_live_snapshot_unavailable_when_kubectl_fails(
    tmp_path, monkeypatch
):
    report, rules = _run_main(tmp_path, monkeypatch, kubectl=None)

    assert report["_meta"]["live_snapshot_ok"] is False
    assert report["_meta"]["live_pods_scanned"] == 0
    assert rules["registry_allowlist_cluster"]["total_checked"] == 0


def test_main_marks_live_snapshot_unavailable_on_malformed_json(tmp_path, monkeypatch):
    report, _ = _run_main(tmp_path, monkeypatch, kubectl="not json at all")

    assert report["_meta"]["live_snapshot_ok"] is False


def test_main_scans_init_containers_alongside_containers(tmp_path, monkeypatch):
    pod = {
        "metadata": {"name": "p", "namespace": "apps"},
        "spec": {
            "containers": [{"image": "ghcr.io/ok/app:1"}],
            "initContainers": [{"image": "docker.io/library/busybox:1"}],
        },
    }
    report, rules = _run_main(tmp_path, monkeypatch, kubectl=_pods(pod))

    assert report["_meta"]["live_snapshot_ok"] is True
    assert report["_meta"]["live_pods_scanned"] == 1
    rule = rules["registry_allowlist_cluster"]
    assert rule["total_checked"] == 2
    assert rule["violations_count"] == 1
    assert rule["violations"][0]["source"] == "cluster:apps/p"


def test_main_flags_hard_pinned_workload_pod(tmp_path, monkeypatch):
    pod = _pod(
        "web",
        "apps",
        ["ghcr.io/ok/app:1"],
        node_selector={"kubernetes.io/hostname": "node-a"},
    )
    _, rules = _run_main(tmp_path, monkeypatch, kubectl=_pods(pod))

    rule = rules["no_hard_node_pins_cluster"]
    assert rule["status"] == "failed"
    assert rule["violations_count"] == 1
    assert rule["violations"][0]["source"] == "cluster:apps/web"


@pytest.mark.parametrize(
    "namespace,owner_kind",
    [
        ("kube-system", None),
        ("kubevirt", None),
        ("argocd", "ReplicaSet"),
        ("local-registry", "DaemonSet"),
    ],
)
def test_main_exempts_approved_infrastructure_from_node_pin_rule(
    tmp_path, monkeypatch, namespace, owner_kind
):
    pod = _pod(
        "infra",
        namespace,
        ["ghcr.io/ok/app:1"],
        node_selector={"kubernetes.io/hostname": "node-a"},
        owner_kind=owner_kind,
    )
    _, rules = _run_main(tmp_path, monkeypatch, kubectl=_pods(pod))

    rule = rules["no_hard_node_pins_cluster"]
    assert rule["total_checked"] == 1
    assert rule["violations_count"] == 0
    assert rule["status"] == "passed"


def test_main_ignores_pods_without_a_hostname_selector(tmp_path, monkeypatch):
    pod = _pod(
        "web",
        "apps",
        ["ghcr.io/ok/app:1"],
        node_selector={"kubernetes.io/os": "linux"},
    )
    _, rules = _run_main(tmp_path, monkeypatch, kubectl=_pods(pod))

    assert rules["no_hard_node_pins_cluster"]["total_checked"] == 0


# --------------------------------------------------------------------------
# main() — compliance score arithmetic
# --------------------------------------------------------------------------


def test_score_is_the_rounded_pass_ratio_across_every_rule(tmp_path, monkeypatch):
    manifests = {
        "manifests/app.yaml": (
            "  image: ghcr.io/ok/a:1\n"
            "  image: ghcr.io/ok/b:1\n"
            "  image: docker.io/library/nginx:1\n"
        )
    }
    report, rules = _run_main(tmp_path, monkeypatch, manifests=manifests)

    total_checked = sum(r["total_checked"] for r in rules.values())
    total_violations = sum(r["violations_count"] for r in rules.values())
    assert (total_checked, total_violations) == (3, 1)
    assert report["score"] == 66.7


def test_score_is_100_when_there_is_nothing_to_check(tmp_path, monkeypatch):
    """No manifests and no cluster must not divide by zero."""
    report, _ = _run_main(tmp_path, monkeypatch)

    assert report["score"] == 100.0


def test_score_is_zero_when_every_check_violates(tmp_path, monkeypatch):
    manifests = {"manifests/app.yaml": "  image: docker.io/library/nginx:1\n"}
    report, _ = _run_main(tmp_path, monkeypatch, manifests=manifests)

    assert report["score"] == 0.0
