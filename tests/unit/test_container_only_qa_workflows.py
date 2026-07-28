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
    assert '"${TARGET_IMAGE}" /sbin/init' in content
    assert "systemctl is-active dbus" in content
    assert "systemctl is-active systemd-logind" in content
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


def test_container_runner_exposes_optional_image_digest_parameter():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert "- name: image-digest" in content
    assert 'value: ""' in content
    assert "- name: IMAGE_DIGEST" in content
    assert 'value: "{{inputs.parameters.image-digest}}"' in content


def test_container_runner_uses_digest_pinned_reference_when_digest_provided():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert 'TARGET_IMAGE="${IMAGE_REPO}@${IMAGE_DIGEST}"' in content
    assert 'DIGEST="${IMAGE_DIGEST}"' in content
    assert 'podman pull "${PODMAN_PULL_TLS_ARGS[@]}" "${TARGET_IMAGE}"' in content
    assert 'podman run' in content and '"${TARGET_IMAGE}" /sbin/init' in content


def test_container_runner_skips_remote_digest_resolution_when_digest_provided():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    block = content.split("# Resolve the digest remotely", 1)[1]
    assert 'if [[ -z "${IMAGE_DIGEST:-}" ]]; then' in block
    assert "skopeo inspect" in block


def test_container_runner_readiness_probe_is_informative():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert "systemd readiness probe" in content
    assert "state=${state:-unknown}" in content
    assert "dbus=${dbus_active:-unknown}" in content
    assert "logind=${logind_active:-unknown}" in content
    assert "seat0=${can_graphical:-unknown}" in content


def test_container_runner_creates_runtime_directories_before_gdm():
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert "mkdir -p /run/dbus /run/systemd/seats /run/systemd/users /run/gdm /var/log/gdm" in content
    assert "chown -R gdm:gdm /run/gdm /var/log/gdm" in content
    assert "chmod 755 /run/gdm" in content
    assert 'chage -d "$(date +%Y-%m-%d)" bluefin-test' in content


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


def test_pr_poller_supports_explicit_refresh_mode():
    content = (ROOT / "argo/workflow-templates/pr-poller.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: refresh-existing" in content
    assert 'value: "false"' in content
    assert "name: REFRESH_EXISTING" in content
    assert 'value: "{{workflow.parameters.refresh-existing}}"' in content
    assert 'if [[ "${REFRESH_EXISTING}" == "true" ]]; then' in content
    assert 'kubectl delete workflow -n argo -l "bluefin.io/pr-number=${PR_NUM},bluefin.io/pr-sha=${SHA12}"' in content


def test_pr_label_poller_cron_forwards_refresh_mode_to_workflow_template():
    cron = (ROOT / "manifests/pr-label-poller.yaml").read_text(encoding="utf-8")

    assert "workflowTemplateRef:\n      name: pr-poller" in cron
    assert "- name: refresh-existing" in cron
    assert 'value: "false"' in cron


def test_pr_poller_declares_parameters_used_by_inline_workflow():
    content = (ROOT / "argo/workflow-templates/pr-poller.yaml").read_text(
        encoding="utf-8"
    )

    args_block = content.split("spec:", 1)[1].split("templates:", 1)[0]
    for name in [
        "refresh-existing",
        "repository",
        "commit-sha",
        "pr-number",
        "image",
        "image-tag",
        "image-digest",
        "suites",
        "variant",
        "branch",
        "testsuite-branch",
        "testsuite-repo",
    ]:
        assert f"- name: {name}" in args_block


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


def test_dakota_qa_pipeline_exposes_and_forwards_image_digest():
    dakota = (ROOT / "argo/workflow-templates/dakota-qa-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    assert "- name: image-digest" in dakota
    assert 'value: "{{workflow.parameters.image-digest}}"' in dakota
    assert "name: run-container-tests" in dakota


def test_pr_poller_carries_image_digest_into_dakota_qa_workflow():
    poller = (ROOT / "argo/workflow-templates/pr-poller.yaml").read_text(
        encoding="utf-8"
    )

    dakota_block = poller.split("name: qa-dakota", 1)[1].split("name: qa-bluefin", 1)[0]
    assert "- name: image-digest" in dakota_block
    # The inline child-workflow manifest escapes Argo expressions so they are
    # resolved by the child workflow, not the parent poller.
    assert 'value: "{{{{workflow.parameters.image-digest}}}}"' in dakota_block
    assert "dakota-qa-pipeline" in dakota_block


def test_caller_contract_requires_forked_testsuite_repo_and_branch():
    contract = (ROOT / "docs/skills/argo-workflows/authoring.md").read_text(
        encoding="utf-8"
    )

    assert "- `testsuite-repo`" in contract
    assert "override both `testsuite-repo` and `testsuite-branch`" in contract
