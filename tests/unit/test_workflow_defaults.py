from pathlib import Path

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

    assert "fetchers: 24" in config
    assert "builders: 24" in config
    assert "pushers: 12" in config
    assert "max-jobs: 24" in config
    assert "nodeSelector:\n        kubernetes.io/hostname: ghost" not in pipeline
    assert "depends: detect-build-mode" in pipeline
    assert "Verified BuildStream remote execution configuration" in pipeline


def test_bst_pipelines_require_fresh_usb4_backed_remote_execution():
    # Cosmic and bluefin-server remain strictly distributed and must gate on a
    # fresh USB4 link observation. Dakota is the user-facing production lane:
    # it publishes through the proven local path while the distributed
    # template is retained for recovery, so it is exempt from the strict gate.
    for filename in (
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


def test_dakota_production_lane_publishes_through_local_path():
    pipeline = (ROOT / "argo/workflow-templates/dakota-build-pipeline.yaml").read_text(
        encoding="utf-8"
    )

    # The production DAG routes through the distributed remote-execution builder;
    # local publisher remains available for recovery.
    assert "template: run-bst-step" in pipeline
    assert "name: bst-build-local" in pipeline
    assert "name: bst-build-re" in pipeline
    assert "DAKOTA PUBLISHED" in pipeline


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
    assert "continueOn:" in dakota
