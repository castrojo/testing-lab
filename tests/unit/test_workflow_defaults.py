from pathlib import Path

import re

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_image_poller_templates_do_not_self_reference_containerdisk_tag_defaults():
    bluefin_pipeline = (ROOT / "argo/workflow-templates/bluefin-qa-pipeline.yaml").read_text(
        encoding="utf-8"
    )
    image_poller = (ROOT / "argo/workflow-templates/image-poller.yaml").read_text(
        encoding="utf-8"
    )

    assert (
        '- name: containerdisk-tag\n      value: "{{workflow.parameters.image-tag}}"'
        not in bluefin_pipeline
    )
    assert (
        '- name: containerdisk-tag\n        value: "{{workflow.parameters.image-tag}}"'
        not in image_poller
    )


def test_image_poller_cron_manifests_do_not_pass_containerdisk_tag():
    offenders = []

    for manifest in sorted((ROOT / "manifests").glob("image-poll-*.yaml")):
        content = manifest.read_text(encoding="utf-8")
        if "workflowTemplateRef:\n      name: image-poller" not in content:
            continue
        if "containerdisk-tag" in content:
            offenders.append(manifest.name)

    assert not offenders, f"obsolete containerdisk-tag in: {', '.join(offenders)}"


def test_dakota_requires_distributed_capacity_matched_execution():
    config = (ROOT / "manifests/buildstream-remote-cache-config.yaml").read_text(
        encoding="utf-8"
    )
    pipeline = (ROOT / "argo/workflow-templates/dakota-build-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    assert "fetchers: 8" in config
    assert "builders: 4" in config
    assert "pushers: 4" in config
    assert "max-jobs: 12" in config
    assert "nodeSelector:\n        kubernetes.io/hostname: ghost" not in pipeline
    assert "depends: detect-build-mode" in pipeline
    assert "Verified BuildStream remote execution configuration" in pipeline


def test_bst_pipelines_require_fresh_usb4_backed_remote_execution():
    for filename in (
        "dakota-build-pipeline.yaml",
        "cosmic-build-pipeline.yaml",
        "bluefin-server-build-pipeline.yaml",
    ):
        pipeline = (ROOT / "argo/workflow-templates" / filename).read_text(
            encoding="utf-8"
        )

        assert "set -euo pipefail" in pipeline
        assert "for NODE in ghost exo-0" in pipeline
        assert "usb4-link" in pipeline
        assert "usb4-link-observed-at" in pipeline
        assert "kubectl get pods -n buildbarn -l app=worker" in pipeline
        assert "template: bst-build-local" not in pipeline
        assert "name: bst-build-local" not in pipeline


def test_dakota_production_lane_has_no_local_fallback():
    pipeline = (ROOT / "argo/workflow-templates/dakota-build-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    assert "template: run-bst-step" in pipeline
    assert "name: bst-build-re" in pipeline
    assert "name: bst-build-local" not in pipeline
    assert "template: bst-build-local" not in pipeline


def test_usb4_monitor_publishes_a_fresh_observation_on_every_probe():
    monitor = (ROOT / "manifests/usb4-link-monitor.yaml").read_text(
        encoding="utf-8"
    )

    assert "lab.projectbluefin.io/usb4-link-observed-at" in monitor
    assert "date -u +%s" in monitor
    assert "N % 20" not in monitor


def test_no_standalone_cache_warming_buildstream_workflow_remains():
    assert not (
        ROOT / "argo/workflow-templates/dakota-buildstream-warm-cache.yaml"
    ).exists()


def test_testsuite_prs_use_direct_commit_status_reporting():
    poller = (ROOT / "argo/workflow-templates/pr-poller.yaml").read_text(
        encoding="utf-8"
    )
    reporter = (ROOT / "argo/workflow-templates/github-status-reporter.yaml").read_text(
        encoding="utf-8"
    )

    assert '"$REPO" == "projectbluefin/testsuite"' in poller
    assert 'REPORTER="github-status-reporter"' in poller
    assert 'name: ${REPORTER}' in poller
    assert "statuses/${SHA}" in reporter
    assert '--arg context "ghost-lab"' in reporter


def test_ghost_lab_status_reporter_authenticates_with_the_github_token():
    reporter = (ROOT / "argo/workflow-templates/github-status-reporter.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: GITHUB_TOKEN" in reporter
    assert "name: github-token" in reporter
    assert '-H "Authorization: Bearer ${GITHUB_TOKEN}"' in reporter
    assert reporter.count("GITHUB_TOKEN") == 2
    assert "set -x" not in reporter
    assert "echo ${GITHUB_TOKEN}" not in reporter
    assert 'echo "${GITHUB_TOKEN}"' not in reporter


def test_dakota_runner_allows_native_chroot_input_root_execution():
    worker = (ROOT / "manifests/buildbarn-worker.yaml").read_text(encoding="utf-8")
    assert "name: runner" in worker
    assert "privileged: true" in worker
    assert "runAsUser: 0" in worker
    assert "type: spc_t" in worker
    assert "bb-runner-installer:20260722T162832Z-236bcd9" in worker
    assert "bb-worker:20260722T162832Z-236bcd9" in worker
    assert "add: [SYS_CHROOT]" not in worker


def test_buildbarn_runner_uses_stable_tmpdir_after_chroot():
    config = (ROOT / "manifests/buildbarn-config.yaml").read_text(encoding="utf-8")
    assert "setTmpdirEnvironmentVariable:" not in config
    assert "concurrency: 12" in config
    assert "runCommandsAs: { userId: 0, groupId: 0 }" in config
    # Production uses the native build directory: the virtual/FUSE experiment
    # failed startup with "operation not permitted" and is not a valid gate.
    assert "native:" in config
    assert "virtual:" not in config
    assert "buildDirectoryPath: '/worker/build'" in config
    assert "maximumCacheFileCount: 1000000" in config
    assert "maximumCacheSizeBytes: 96 * 1024 * 1024 * 1024" in config
    assert "filePool:" not in config


def test_aurora_containerdisk_builder_isolated_and_prebaked():
    builder = (
        ROOT / "argo/workflow-templates/build-bluefin-migration-containerdisk.yaml"
    ).read_text(encoding="utf-8")

    assert "- name: containerdisk-repo" in builder
    assert 'value: "bluefin-containerdisk"' in builder
    assert "- name: prebake-kde-webdriver" in builder
    assert "selenium-webdriver-at-spi.git" in builder
    assert "d45a21e8f1b3591dc921f0be85f1ecd834cbe413" in builder
    assert "selenium-webdriver-at-spi-inputsynth" in builder
    assert "192.168.1.102:30500/{{inputs.parameters.containerdisk-repo}}:${TAG}" in builder

    aurora = (ROOT / "argo/workflow-templates/aurora-containerdisk-test.yaml").read_text(
        encoding="utf-8"
    )
    assert "value: aurora-test" in aurora
    assert "value: 30G" in aurora
    assert "value: aurora-containerdisk" in aurora
    assert 'value: "true"' in aurora
    assert "key: migration-containerdisk-build" in aurora
    assert "volumeClaimGC:" in aurora
    assert "strategy: OnWorkflowCompletion" in aurora
    assert "volumeClaimTemplates:" in aurora
    assert "name: staging" in aurora
    assert "storage: 100Gi" in aurora


def test_aurora_qa_pipeline_is_vm_based_and_serialized():
    pipeline_path = ROOT / "argo/workflow-templates/aurora-qa-pipeline.yaml"
    assert pipeline_path.exists()
    pipeline = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    spec = pipeline["spec"]

    assert pipeline["metadata"]["name"] == "aurora-qa-pipeline"
    assert spec["entrypoint"] == "aurora-qa"
    assert spec["onExit"] == "teardown"
    assert spec["activeDeadlineSeconds"] == 3600
    assert spec["templates"][0]["name"] == "aurora-qa"
    assert spec["templates"][0]["synchronization"]["semaphores"][0]["configMapKeyRef"] == {
        "name": "workflow-semaphores",
        "key": "aurora-vm-qa",
    }

    tasks = spec["templates"][0]["dag"]["tasks"]
    assert [task["name"] for task in tasks] == [
        "build-aurora-containerdisk",
        "provision-containerdisk-vm",
        "run-kde-tests",
        "collect-logs",
    ]
    assert tasks[0]["template"] == "build-aurora-containerdisk"
    build_template = next(
        template
        for template in spec["templates"]
        if template["name"] == "build-aurora-containerdisk"
    )
    assert build_template["synchronization"]["semaphores"][0]["configMapKeyRef"] == {
        "name": "workflow-semaphores",
        "key": "migration-containerdisk-build",
    }
    assert build_template["steps"][0][0]["templateRef"] == {
        "name": "build-bluefin-migration-containerdisk",
        "template": "build-containerdisk",
    }
    assert tasks[1]["templateRef"] == {
        "name": "provision-containerdisk-vm",
        "template": "provision-vm",
    }
    assert tasks[2]["templateRef"] == {
        "name": "run-kde-tests",
        "template": "run-kde-tests",
    }
    assert tasks[3]["templateRef"] == {
        "name": "collect-vm-logs",
        "template": "collect-vm-logs",
    }

    content = pipeline_path.read_text(encoding="utf-8")
    assert "containerdisk-repo" in content
    assert "value: aurora-containerdisk" in content
    assert "value: aurora-test" in content
    assert "value: 30G" in content
    assert "run-kde-tests.Errored" in content

    semaphores = yaml.safe_load(
        (ROOT / "manifests/workflow-semaphores.yaml").read_text(encoding="utf-8")
    )
    assert semaphores["data"]["aurora-vm-qa"] == "1"


def test_nightly_kde_cron_uses_aurora_pipeline_and_shared_semaphore():
    cron_path = ROOT / "manifests/nightly-kde.yaml"
    cron = yaml.safe_load(cron_path.read_text(encoding="utf-8"))
    spec = cron["spec"]
    workflow_spec = spec["workflowSpec"]

    assert cron["metadata"]["name"] == "nightly-kde"
    assert spec["schedules"] == ["0 4 * * *"]
    assert spec["concurrencyPolicy"] == "Forbid"
    assert workflow_spec["activeDeadlineSeconds"] == 5400
    assert workflow_spec["workflowTemplateRef"] == {"name": "aurora-qa-pipeline"}
    assert {
        parameter["name"]: parameter["value"]
        for parameter in workflow_spec["arguments"]["parameters"]
    }["namespace"] == "aurora-test"

    semaphore = yaml.safe_load(
        (ROOT / "manifests/workflow-semaphores.yaml").read_text(encoding="utf-8")
    )
    assert semaphore["data"]["aurora-vm-qa"] == "1"
    assert "semaphore" in cron_path.read_text(encoding="utf-8")


def test_aurora_qa_pipeline_exposes_safe_kde_sabotage_modes():
    pipeline_path = ROOT / "argo/workflow-templates/aurora-qa-pipeline.yaml"
    pipeline = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    parameters = {
        parameter["name"]: parameter.get("value")
        for parameter in pipeline["spec"]["arguments"]["parameters"]
    }
    assert parameters["sabotage-mode"] == "none"
    assert "sabotage-mode" in pipeline_path.read_text(encoding="utf-8")

    runner = (ROOT / "argo/workflow-templates/run-kde-tests.yaml").read_text(
        encoding="utf-8"
    )
    assert "missing-binary" in runner
    assert "kill-plasmashell" in runner
    assert "this-does-not-exist" in runner
    assert "pkill -x plasmashell" in runner
    assert "unexpectedly passed" in runner
    assert "aurora-test" in runner


def test_aurora_kde_sabotage_workflow_runs_both_controlled_failures():
    path = ROOT / "argo/aurora-kde-sabotage.yaml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert workflow["spec"]["onExit"] == "cleanup"
    tasks = workflow["spec"]["templates"][0]["dag"]["tasks"]
    sabotage_tasks = [
        task for task in tasks if task["name"] in {"missing-binary", "kill-plasmashell"}
    ]
    assert [task["name"] for task in sabotage_tasks] == [
        "missing-binary",
        "kill-plasmashell",
    ]
    content = path.read_text(encoding="utf-8")
    assert "sabotage-mode" in content
    assert "aurora-test" in content
    assert "delete vm" in content.lower()


def test_kde_runner_adapts_gnome_runner_contract_for_webdriver():
    gnome = (ROOT / "argo/workflow-templates/run-gnome-tests.yaml").read_text(
        encoding="utf-8"
    )
    kde = (ROOT / "argo/workflow-templates/run-kde-tests.yaml").read_text(
        encoding="utf-8"
    )

    for parameter in (
        "vm-name",
        "vm-namespace",
        "suite",
        "variant",
        "ssh-user",
        "ssh-key-secret",
        "issue-title",
        "behave-tags",
        "branch",
        "containerdisk-tag",
    ):
        assert f"- name: {parameter}" in gnome
        assert f"- name: {parameter}" in kde

    assert 'value: "aurora-test"' in kde
    assert 'value: "kde-smoke"' in kde
    assert "4723:4723" in kde
    assert "selenium-webdriver-at-spi-run" in kde
    assert "KDE_WEBDRIVER_URL" in kde
    assert "XDG_SESSION_DESKTOP=kde" in kde
    assert "/status" in kde
    assert "publish_test_results.py" in kde
    assert "/var/mnt/ghost-data/test-results" in kde
    assert "- name: failure-class" in kde
    assert "- name: failure-issue-url" in kde
    assert "- name: behave-retries" in kde
    assert 'value: "2"' in kde
    assert "BEHAVE_RETRIES=2" in kde
    assert "Required credential is missing: GITHUB_TOKEN" in kde
    assert "optional: true" not in kde
    assert "ERROR: failed to publish KDE test results" in kde
    assert "qecore-headless" not in kde
    assert "gnome-ponytail-daemon" not in kde


def test_kde_runner_persists_failure_artifacts_without_publishing_screenshots():
    kde = (ROOT / "argo/workflow-templates/run-kde-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert "faillog_" in kde
    assert "python3 -m tarfile -c" in kde
    assert "tar -czf" not in kde
    assert "ARTIFACT_RC=1" in kde
    assert "evidence artifacts were not fully retained" in kde
    assert "scp" in kde
    assert "BEHAVE_RC=0" in kde
    assert "GHCR screenshot publication disabled" in kde
    assert "oras push" not in kde
    assert "SCREENSHOT_IMAGE=" not in kde
    assert "Warning: failed" not in kde
    assert "TESTSUITE_RESULTS_DIR" in kde
    assert "qemu_screendump" not in kde


def test_kde_linux_workflow_calls_native_runner_with_current_contract():
    workflow = (ROOT / "argo/kde-linux-qa.yaml").read_text(encoding="utf-8")
    provision = (
        ROOT / "argo/workflow-templates/provision-kde-linux-vm.yaml"
    ).read_text(encoding="utf-8")

    assert 'value: "aurora-test"' in workflow
    assert "- name: branch" in workflow
    assert "workflow.parameters.branch" in workflow
    assert "testsuite-branch" not in workflow
    assert 'value: "aurora-test"' in provision
    assert "kde-test-namespace" not in workflow


def test_kde_workflow_images_are_digest_pinned():
    paths = (
        ROOT / "argo/kde-linux-qa.yaml",
        ROOT / "argo/aurora-kde-sabotage.yaml",
        ROOT / "argo/workflow-templates/aurora-qa-pipeline.yaml",
        ROOT / "argo/workflow-templates/provision-kde-linux-vm.yaml",
        ROOT / "argo/workflow-templates/run-kde-tests.yaml",
    )
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "ghcr.io/projectbluefin/lab-runner:latest" not in content
        assert "cgr.dev/chainguard/kubectl:latest-dev" not in content


def test_kde_runner_sources_complete_session_environment():
    kde = (ROOT / "argo/workflow-templates/run-kde-tests.yaml").read_text(
        encoding="utf-8"
    )

    for variable in (
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "AT_SPI_BUS_ADDRESS",
        "XDG_SESSION_DESKTOP",
        "KDE_WEBDRIVER_URL",
    ):
        assert variable in kde
    assert "source /tmp/session.env" in kde


def test_cache_only_diagnostic_disables_remote_execution_explicitly():
    config = (ROOT / "manifests/buildstream-remote-cache-config.yaml").read_text(encoding="utf-8")
    assert "remote-execution: {}" not in config


def test_dakota_persists_sources_in_buildbarn():
    config_map = yaml.safe_load(
        (ROOT / "manifests/buildstream-remote-cache-config.yaml").read_text(
            encoding="utf-8"
        )
    )
    config = yaml.safe_load(config_map["data"]["dakota-buildstream.conf"])
    source_servers = config["source-caches"]["servers"]
    pipeline = (ROOT / "argo/workflow-templates/dakota-build-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    assert config["source-caches"]["override-project-caches"] is True
    assert source_servers[:1] == [
        {"url": "https://gbm.gnome.org:11003", "push": False},
    ]
    assert "type: index" in pipeline
    assert "type: storage" in pipeline
    assert "grpc://bb-remote-asset.buildbarn.svc.cluster.local:8984" in pipeline
    assert "grpc://frontend.buildbarn.svc.cluster.local:8980" in pipeline
    assert "override-project-caches: true" in pipeline
    source_cache_block = pipeline.split("source-caches:", 1)[1]
    assert "cache.projectbluefin.io" not in source_cache_block
    # The deployed bb-remote-asset endpoint cannot FetchBlob BuildStream
    # source URNs, so no source-cache server list may point at it. Inspect
    # the lines directly following each source-caches key rather than the
    # remainder of the file, which legitimately mentions bb-remote-asset for
    # artifact indexing.
    for block in pipeline.split("source-caches:")[1:]:
        head = block.splitlines()[:12]
        url_lines = [line for line in head if "url:" in line]
        assert url_lines, "source-caches block missing servers"
        assert not any("bb-remote-asset" in line for line in url_lines)


def test_dakota_patch_sync_fetches_junction_commit_ids():
    pipeline = (ROOT / "argo/workflow-templates/dakota-build-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    assert 'GNOME_COMMIT="${GNOME_REF##*-g}"' in pipeline
    assert 'FDS_COMMIT="${FDS_REF##*-g}"' in pipeline
    assert 'git fetch --depth=1 origin "${GNOME_COMMIT}"' in pipeline
    assert 'git fetch --depth=1 origin "${FDS_COMMIT}"' in pipeline
    assert 'git fetch --depth=1 origin "${GNOME_REF}"' not in pipeline
    assert 'git fetch --depth=1 origin "${FDS_REF}"' not in pipeline


def test_dakota_build_pipeline_includes_non_blocking_nvidia_variant():
    dakota = (ROOT / "argo/workflow-templates/dakota-build-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: build-bluefin" in dakota
    assert "oci/bluefin.bst" in dakota
    assert "name: build-bluefin-nvidia" in dakota
    assert "oci/bluefin-nvidia.bst" in dakota
    assert "tag\n                  value: \"dakota-nvidia\"" in dakota
    default_task = dakota.split("- name: build-bluefin", 1)[1].split(
        "- name: build-bluefin-nvidia", 1
    )[0]
    assert "template: run-bst-step-nonblocking" in dakota
    nonblocking = dakota.split("- name: run-bst-step-nonblocking", 1)[1].split(
        "- name: bst-build-re", 1
    )[0]
    assert "continueOn:" in nonblocking
    assert "continueOn:" not in default_task


KDE_RUNNER_PATH = ROOT / "argo/workflow-templates/run-kde-tests.yaml"

# Variables any bash process can rely on without an explicit definition.
AMBIENT_SHELL_VARS = {
    "BASH", "BASH_VERSION", "EUID", "HOME", "HOSTNAME", "IFS", "LANG",
    "LC_ALL", "LINENO", "LOGNAME", "MACHTYPE", "OLDPWD", "OPTARG", "OPTIND",
    "OSTYPE", "PATH", "PPID", "PWD", "RANDOM", "SECONDS", "SHELL", "SHLVL",
    "TERM", "TMPDIR", "UID", "USER", "_",
}


def _kde_runner_script_and_env():
    template = yaml.safe_load(KDE_RUNNER_PATH.read_text(encoding="utf-8"))[
        "spec"
    ]["templates"][0]
    container = template["container"]
    return "\n".join(container["args"]), {item["name"] for item in container["env"]}


def _strip_single_quoted_spans(script: str) -> str:
    """Drop single-quoted spans; their contents are not shell-expanded."""
    characters = []
    in_single_quote = False
    for char in script:
        if char == "'":
            in_single_quote = not in_single_quote
        elif not in_single_quote:
            characters.append(char)
    return "".join(characters)


def test_kde_runner_defines_every_shell_variable_it_references():
    """A single unbound variable kills the runner under `set -u`.

    Regression guard for the `${IMG_SLUG}` crash: the publish step expanded a
    variable that was never defined, so every KDE run (green, red, or
    sabotaged) died before publishing results.
    """
    script, env_names = _kde_runner_script_and_env()
    # Argo placeholders are interpolated before bash executes the script.
    script = re.sub(r"\{\{[^}]*\}\}", "ARGO", script)
    body = _strip_single_quoted_spans(script)

    defined = AMBIENT_SHELL_VARS | env_names
    for line in body.splitlines():
        for match in (
            re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line),
            re.match(r".*\bread\b.*\s([A-Za-z_][A-Za-z0-9_]*)\s*;?\s*$", line),
            re.match(r"\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", line),
        ):
            if match:
                defined.add(match.group(1))

    referenced = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", body))
    referenced |= set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", body))
    referenced = {
        name for name in referenced if not re.fullmatch(r"[0-9#?@*!$-]", name)
    }

    undefined = referenced - defined
    assert not undefined, (
        "run-kde-tests references undefined shell variables "
        f"(fatal under set -u): {sorted(undefined)}"
    )


def test_kde_runner_publishes_result_outputs_for_evidence_reconciler():
    """The declared outputs must actually be produced by the script.

    `result` has no valueFrom default, so a missing summary file errored the
    task even after a green run, and `failed-scenarios` always fell back to
    "[]", which the QA run evidence reconciler treats as "results
    unavailable".
    """
    doc = yaml.safe_load(KDE_RUNNER_PATH.read_text(encoding="utf-8"))
    template = doc["spec"]["templates"][0]
    script = "\n".join(template["container"]["args"])

    output_names = {
        parameter["name"] for parameter in template["outputs"]["parameters"]
    }
    assert output_names == {"result", "failed-scenarios"}
    assert "/tmp/results/result-summary.txt" in script
    assert "scenarios passed" in script
    assert "/tmp/results/failed-scenarios.json" in script
    # Same bounded failed-scenario list contract as the GNOME runner.
    assert "[A-Za-z0-9 .,:;()/_+-]" in script
    assert "failed_scenarios[:20]" in script


def test_kde_runner_keeps_sabotage_evidence_isolated_per_mode():
    """Sabotage runs must not clobber each other's evidence.

    Both sabotage modes previously persisted into and published from the same
    paths, so the kill-plasmashell run overwrote the missing-binary run's
    results.json, and their intentional failures polluted the KDE soak-gate
    history.
    """
    doc = yaml.safe_load(KDE_RUNNER_PATH.read_text(encoding="utf-8"))
    script = "\n".join(doc["spec"]["templates"][0]["container"]["args"])

    # Non-sabotage runs keep the historical evidence path and suite key.
    assert 'RESULT_DIR="/var/mnt/ghost-data/test-results/{{workflow.name}}/${SUITE}"' in script
    # Sabotage runs get a mode-specific evidence directory and publish suite.
    assert (
        'RESULT_DIR="/var/mnt/ghost-data/test-results/{{workflow.name}}'
        '/${SUITE}-sabotage-${SABOTAGE_MODE}"'
    ) in script
    assert 'PUBLISH_SUITE="${SUITE}-sabotage-${SABOTAGE_MODE}"' in script
    assert 'IMG_SLUG="${VARIANT}"' in script
    assert "- name: CONTAINERDISK_TAG" in KDE_RUNNER_PATH.read_text(
        encoding="utf-8"
    )


def test_kde_runner_sabotage_verdict_requires_recorded_failures():
    """A sabotaged run must go red with recorded errors, never a silent skip."""
    script, _ = _kde_runner_script_and_env()

    assert "SABOTAGE_VERDICT_RC=0" in script
    assert "recorded zero scenarios" in script
    assert "recorded 0 failed scenarios" in script
    assert "no kde_faillog_* bundle was retained" in script
    # The verdict must be able to fail the task, not just log.
    assert 'exit "${SABOTAGE_VERDICT_RC}"' in script
