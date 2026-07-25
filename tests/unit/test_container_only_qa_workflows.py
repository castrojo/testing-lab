from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "argo/workflow-templates/bluefin-qa-pipeline.yaml"
FORBIDDEN = (
    "assert-cd",
    "containerdisk-tag",
    "provision-containerdisk-vm",
    "run-gnome-tests",
    "teardown-vm",
    "qa-vm-fleet",
    "kubectl delete vm",
)


def test_bluefin_image_poll_qa_is_container_only():
    content = PIPELINE.read_text(encoding="utf-8")
    assert "name: run-container-tests" in content
    assert all(token not in content for token in FORBIDDEN)


def test_image_poller_has_no_containerdisk_parameter_or_reference():
    content = (ROOT / "argo/workflow-templates/image-poller.yaml").read_text(
        encoding="utf-8"
    )
    assert "containerdisk-tag" not in content
    assert "build-containerdisk" not in content


def test_bluefin_container_only_pipeline_preserves_all_suite_lanes():
    content = PIPELINE.read_text(encoding="utf-8")
    assert "withItems: [smoke, common, developer, software, system]" in content
    assert 'value: "{{item}}"' in content


def test_bluefin_pipeline_validates_raw_suites_against_exact_allow_list():
    content = PIPELINE.read_text(encoding="utf-8")
    assert "- name: validate-suites" in content
    assert '- name: suites\n            value: "{{workflow.parameters.suites}}"' in content
    assert '- name: SUITES\n        value: "{{inputs.parameters.suites}}"' in content
    assert 'IFS=\',\' read -r -a raw_suites <<< "$SUITES"' in content
    assert "{{inputs.parameters.suites}}" not in content.split("source: |", 1)[1]
    assert "case \"${suite}\" in" in content
    assert "smoke|common|developer|software|system) ;;" in content


def test_bluefin_test_lane_depends_on_suite_validation():
    content = PIPELINE.read_text(encoding="utf-8")
    assert 'depends: "validate-suites.Succeeded"' in content
    assert content.index("- name: validate-suites") < content.index("- name: test-lane")


def test_run_container_tests_explicitly_allows_system_suite():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )
    assert "smoke|common|developer|software|system" in content
    assert "Unsupported container suite: ${SUITE}" in content


def test_container_runner_uses_a_nested_systemd_target_with_bounded_resources():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert "privileged: true" in content
    assert 'ephemeral-storage: 12Gi' in content
    assert 'ephemeral-storage: 24Gi' in content
    assert "podman run --detach --systemd=always" in content
    assert "--network host" in content
    assert "--volume /etc/resolv.conf:/etc/resolv.conf:ro" in content
    assert '"${IMAGE}" /sbin/init' in content
    assert "systemctl is-active dbus systemd-logind" in content
    assert "useradd -m -u 1000" in content
    assert "bluefin-test ALL=(ALL) NOPASSWD: ALL" in content
    assert "AutomaticLogin=bluefin-test" in content
    assert "InitialSetupEnable=False" in content
    assert "pgrep -u 1000 -f gnome-session" in content
    assert "--user 1000:1000" in content
    assert "podman exec" in content
    assert "podman rm --force" in content
    assert "--shm-size" not in content
    assert "provision-containerdisk-vm" not in content
    assert "bootc install to-disk" not in content


def test_native_systemd_runner_uses_a_scheduler_managed_target_pod():
    content = (
        ROOT / "argo/workflow-templates/run-systemd-container-tests.yaml"
    ).read_text(encoding="utf-8")

    assert "action: create" in content
    assert "kind: Pod" in content
    assert "setOwnerReference: true" in content
    assert "serviceAccountName: argo" in content
    assert 'command: ["/usr/lib/systemd/systemd"]' in content
    assert '--timeout=600s' in content
    assert "privileged: true" in content
    assert "kubectl exec" in content
    assert 'tee /workspace/resolv.conf < /etc/resolv.conf' in content
    assert 'rm -f /etc/resolv.conf' in content
    assert "bash -s <<'TARGET_SETUP'" in content
    assert "bluefin-test:x:1000:1000" in content
    assert "today=$(( $(date +%s) / 86400 ))" in content
    assert "bluefin-test ALL=(ALL) NOPASSWD: ALL" in content
    assert "runuser -u bluefin-test -- env" in content
    assert "qecore-headless" in content
    assert "run-behave.sh" in content
    assert "qa-suite.env" in content
    assert "results.json" in content
    assert "behave-rc.txt" in content
    assert "cat /workspace/results.json > /tmp/results/results.json" in content
    assert "kubectl delete pod" in content
    assert "nodeSelector:" not in content
    assert "containerDisk" not in content
    assert "bootc install to-disk" not in content


def test_pr_poller_uses_the_exact_testsuite_pr_source():
    content = (ROOT / "argo/workflow-templates/pr-poller.yaml").read_text(
        encoding="utf-8"
    )

    assert "HEAD_REPO=$(echo \"$PR\" | jq -r '.head.repo.clone_url')" in content
    assert 'TESTSUITE_REPO="$HEAD_REPO"' in content
    assert "- name: testsuite-repo" in content
    assert "value: ${TESTSUITE_REPO}" in content


def test_container_runner_never_falls_back_to_a_different_testsuite_revision():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert 'git clone --depth 1 --branch "${TSBRANCH}" "${TSREPO}"' in content
    assert "falling back to main" not in content


def test_image_poll_qa_has_no_legacy_containerdisk_producer():
    deleted_assets = (
        ROOT / "argo/workflow-templates/build-containerdisk.yaml",
        ROOT / "argo/workflow-templates/digest-watch.yaml",
        ROOT / "manifests/digest-watch-cron.yaml",
        ROOT / "tests/unit/test_build_containerdisk_workflow.py",
    )

    assert all(not path.exists() for path in deleted_assets)

    matrix = (ROOT / "argo/bluefin-test-matrix.yaml").read_text(encoding="utf-8")
    semaphores = (ROOT / "manifests/workflow-semaphores.yaml").read_text(
        encoding="utf-8"
    )
    assert "name: run-container-tests" in matrix
    assert "build-containerdisk" not in matrix
    assert "containerdisk-tag" not in matrix
    assert "qa-vm-fleet" not in semaphores
    assert "\n  containerdisk-build:" not in semaphores


def test_unrelated_vm_workflows_keep_their_shared_helpers():
    shared_templates = (
        ROOT / "argo/workflow-templates/provision-containerdisk-vm.yaml",
        ROOT / "argo/workflow-templates/run-gnome-tests.yaml",
        ROOT / "argo/workflow-templates/teardown-vm.yaml",
        ROOT / "argo/workflow-templates/collect-vm-logs.yaml",
    )

    assert all(path.exists() for path in shared_templates)

    knuckle = (ROOT / "argo/workflow-templates/knuckle-qa-pipeline.yaml").read_text(
        encoding="utf-8"
    )
    migration = (ROOT / "argo/workflow-templates/bluefin-migration-test.yaml").read_text(
        encoding="utf-8"
    )
    assert "name: run-gnome-tests" in knuckle
    assert "name: teardown-vm" in knuckle
    assert "name: provision-containerdisk-vm" in migration
    assert "name: teardown-vm" in migration


def test_migration_rebuilds_its_own_containerdisk_source():
    builder = ROOT / "argo/workflow-templates/build-bluefin-migration-containerdisk.yaml"
    migration = (ROOT / "argo/workflow-templates/bluefin-migration-test.yaml").read_text(
        encoding="utf-8"
    )

    assert builder.exists()
    assert "name: build-bluefin-migration-containerdisk" in migration
    assert "template: build-containerdisk" in migration
    assert "value: 'true'" in migration
    assert migration.index("name: build-bluefin-migration-containerdisk") < migration.index(
        "name: provision-containerdisk-vm"
    )
    assert "volumeClaimTemplates:" in migration
    assert "name: staging" in migration
    assert "volumeClaimTemplates:" not in builder.read_text(encoding="utf-8")
    assert "key: migration-containerdisk-build" in migration
    assert "activeDeadlineSeconds: 86400" in migration


def test_lts_smoke_recipe_uses_lts_image_and_variant():
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    assert 'if [[ "{{ tag }}" == lts-* ]]; then' in justfile
    assert 'image="ghcr.io/projectbluefin/bluefin-lts"' in justfile
    assert 'image_tag="${image_tag#lts-}"' in justfile
    assert 'variant="bluefin-lts"' in justfile
    assert '-p variant="${variant}"' in justfile


def test_migration_recipe_does_not_advertise_an_unsupported_lts_alias():
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    assert "just run-migration-test lts-testing" not in justfile


def test_scheduled_and_pr_image_qa_do_not_pass_vm_parameters():
    files = [
        ROOT / "argo/workflow-templates/pr-poller.yaml",
        *sorted((ROOT / "manifests").glob("image-poll-*.yaml")),
        ROOT / "manifests/nightly-smoke.yaml",
        ROOT / "manifests/nightly-smoke-lts.yaml",
        ROOT / "manifests/nightly-dakota.yaml",
    ]
    forbidden = ("containerdisk-tag", "ssh-key-secret", "vm-memory")

    for path in files:
        content = path.read_text(encoding="utf-8")
        assert all(token not in content for token in forbidden), path.name


def test_dakota_and_cosmic_qa_are_container_only():
    for name in ("dakota-qa-pipeline.yaml", "cosmic-qa-pipeline.yaml"):
        content = (ROOT / "argo/workflow-templates" / name).read_text(encoding="utf-8")
        assert "name: run-container-tests" in content
        assert "provision-containerdisk-vm" not in content
        assert "run-gnome-tests" not in content


def test_cosmic_qa_uses_a_published_bootc_image():
    cosmic = (ROOT / "argo/workflow-templates/cosmic-qa-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    assert 'value: "cosmic-pr-33"' in cosmic


def test_caller_contract_requires_forked_testsuite_repo_and_branch():
    contract = (ROOT / "docs/skills/argo-workflows/authoring.md").read_text(
        encoding="utf-8"
    )

    assert "- `testsuite-repo`" in contract
    assert "override both `testsuite-repo` and `testsuite-branch`" in contract
