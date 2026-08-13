from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONTAINERFILE = ROOT / "images/arc-runner/Containerfile"
CONTAINER_RUNNER = ROOT / "argo/workflow-templates/run-container-tests.yaml"
SYSTEMD_RUNNER = ROOT / "argo/workflow-templates/run-systemd-container-tests.yaml"
PUBLISHER = ROOT / "scripts/publish_test_results.py"


def _template(path, name):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return next(template for template in document["spec"]["templates"] if template["name"] == name)


def test_arc_runner_bakes_tools_and_a_python_wheelhouse():
    containerfile = CONTAINERFILE.read_text(encoding="utf-8")

    for package in ("podman", "skopeo", "git", "python3-pip"):
        assert package in containerfile
    assert "/opt/qa-wheels" in containerfile
    for dependency in ('"setuptools<81"', "qecore", "dogtail", "behave"):
        assert dependency in containerfile


def test_container_runner_uses_baked_runner_tools_without_dnf_bootstrap():
    runner = _template(CONTAINER_RUNNER, "run-container-tests")
    source = runner["script"]["source"]

    assert runner["script"]["image"] == "ghcr.io/projectbluefin/arc-runner:latest"
    assert "dnf install -y skopeo" not in source
    assert "dnf install -y git-core" not in source
    assert "--volume /opt/qa-wheels:/opt/qa-wheels:ro" in source
    assert "--cache-dir" in source
    assert "PIP_CACHE_DIR=/var/cache/bluefin-qa-pip" in source
    assert "--no-index --find-links /opt/qa-wheels" in source
    assert "falling back to the configured package index" in source
    container_text = CONTAINER_RUNNER.read_text(encoding="utf-8")
    assert "kubernetes.io/hostname: ghost" in container_text
    assert "path: /var/mnt/ghost-data/local-path" in container_text
    assert "type: Directory" in container_text


def test_target_python_dependencies_remain_target_installs_with_load_bearing_pin():
    container_source = _template(CONTAINER_RUNNER, "run-container-tests")["script"]["source"]
    systemd_source = _template(SYSTEMD_RUNNER, "run-tests")["script"]["source"]

    # These packages exercise the shipped target image, so they must still be
    # installed inside that disposable image rather than imported from the
    # runner's Python environment.
    for source in (container_source, systemd_source):
        assert "qecore" in source
        assert "dogtail" in source
        assert "behave" in source
        assert '"setuptools<81"' in source
        assert "--find-links" in source
        assert "--no-index" in source
        assert "falling back to the configured package index" in source
        assert "bluefin-qa-pip" in source

    assert "Sandbox._attach_version_status_to_report()" in container_source
    assert "pkg_resources" in systemd_source


def test_aggregate_publication_has_one_lab_clone_and_one_batch_push():
    runner_source = _template(CONTAINER_RUNNER, "run-container-tests")["script"]["source"]
    publisher_source = PUBLISHER.read_text(encoding="utf-8")

    assert runner_source.count("github.com/projectbluefin/lab.git") == 1
    assert "--batch-dir" in runner_source
    assert ".publish.lock" in runner_source
    assert "all ${expected_count} suite nodes are terminal" in runner_source
    assert "persist_suite_results || persist_rc=$?" in runner_source
    assert "results-store" in CONTAINER_RUNNER.read_text(encoding="utf-8")
    assert "def publish_batch_results(" in publisher_source
    assert publisher_source.count('["git", "push", "origin", "HEAD:main"]') == 1


def test_systemd_target_preseeds_wheels_without_changing_target_image():
    create_target = _template(SYSTEMD_RUNNER, "create-target")
    target = create_target["resource"]["manifest"]

    assert "name: qa-wheelhouse" in target
    assert "ghcr.io/projectbluefin/arc-runner:latest" in target
    assert "/opt/qa-wheels" in target
    assert "name: pip-cache" in target
    assert 'path: /var/cache/bluefin-qa-pip' in target
    assert 'image: "{{inputs.parameters.image}}:{{inputs.parameters.image-tag}}"' in target
