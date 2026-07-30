from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "argo/kde-linux-qa.yaml"
PROVISION = ROOT / "argo/workflow-templates/provision-kde-linux-vm.yaml"
RUNNER = ROOT / "argo/workflow-templates/run-kde-tests.yaml"


def test_kde_linux_lane_uses_its_dedicated_namespace_and_suite_identity():
    content = WORKFLOW.read_text(encoding="utf-8")

    assert 'value: "kde-test"' in content
    assert "- name: suite" in content
    assert 'value: "kde-smoke"' in content
    assert "- name: variant" in content
    assert 'value: "kde-linux"' in content


def test_kde_linux_lane_is_checksum_pinned_and_ovmf_teardown_backed():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    provision = PROVISION.read_text(encoding="utf-8")

    assert "https://files.kde.org/kde-linux/" in workflow
    assert "name: image-sha256" in workflow
    assert "sha256sum -c -" in provision
    assert "bootloader:" in provision
    assert "efi:" in provision
    assert "onExit: cleanup" in workflow
    assert "template: teardown-vm" in workflow


def test_kde_linux_runner_requires_ssh_webdriver_and_persists_evidence():
    content = RUNNER.read_text(encoding="utf-8")

    assert '"${VIRTCTL}" port-forward' in content
    assert "until ssh" in content
    assert 'until curl --fail --silent "${KDE_WEBDRIVER_URL}/status"' in content
    assert 'KDE session D-Bus address is unavailable' in content
    assert 'KDE Wayland display is unavailable' in content
    assert 'export QT_ACCESSIBILITY=1' in content
    assert 'export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1' in content
    assert 'python3 -m behave "tests/${SUITE}/features"' in content
    assert "RESULT_DIR=\"/var/mnt/ghost-data/test-results/{{workflow.name}}/${SUITE}\"" in content
    assert 'git clone --depth 1 \\' in content
    assert '/results/lab-code' in content
    assert '/workspace/lab-code' not in content
