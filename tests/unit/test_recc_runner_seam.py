import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
GATED_TEMPLATES = (
    "dakota-build-pipeline.yaml",
    "cosmic-build-pipeline.yaml",
    "bluefin-server-build-pipeline.yaml",
    "bst-cache-warm.yaml",
)
# Containers the outer BuildStream remote-execution contract actually needs.
OUTER_RE_CONTAINERS = ("worker", "runner")


def test_buildbarn_worker_prepares_pod_local_localcas_socket():
    manifest = yaml.safe_load(
        (ROOT / "manifests/buildbarn-worker.yaml").read_text(encoding="utf-8")
    )
    pod_spec = manifest["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in pod_spec["containers"]}
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    volume_init = next(
        container
        for container in pod_spec["initContainers"]
        if container["name"] == "volume-init"
    )
    volume_init_command = volume_init["command"][-1]

    assert "recc-casd" in containers
    for name, fd in (("stdin", 0), ("stdout", 1), ("stderr", 2)):
        assert f"ln -sfn /proc/self/fd/{fd} /worker/dev/{name}" in volume_init_command
    casd = containers["recc-casd"]
    assert casd["image"].startswith("192.168.1.102:30500/bst2@sha256:")
    assert "--cas-remote=grpc://frontend.buildbarn.svc.cluster.local:8980" in casd[
        "args"
    ]
    assert "--ac-remote=grpc://frontend.buildbarn.svc.cluster.local:8980" in casd[
        "args"
    ]
    assert "--exec-remote=grpc://frontend.buildbarn.svc.cluster.local:8980" in casd[
        "args"
    ]
    assert "--bind=unix:/run/buildbarn/recc/casd.sock" in casd["args"]
    assert {"name": "nested-reapi", "mountPath": "/run/buildbarn/recc"} in casd[
        "volumeMounts"
    ]
    assert casd["readinessProbe"]["exec"]["command"] == [
        "test",
        "-S",
        "/run/buildbarn/recc/casd.sock",
    ]
    assert casd["livenessProbe"]["exec"]["command"] == [
        "test",
        "-S",
        "/run/buildbarn/recc/casd.sock",
    ]
    assert {"name": "nested-reapi", "mountPath": "/run/buildbarn/recc"} in containers[
        "runner"
    ]["volumeMounts"]
    assert volumes["nested-reapi"] == {"name": "nested-reapi", "emptyDir": {}}


def test_recc_casd_sidecar_resolves_its_binary_through_path():
    manifest = yaml.safe_load(
        (ROOT / "manifests/buildbarn-worker.yaml").read_text(encoding="utf-8")
    )
    containers = {
        container["name"]: container
        for container in manifest["spec"]["template"]["spec"]["containers"]
    }

    # Nothing in this repo pins the bst2 install prefix, so an absolute path
    # would be an unproven guess. PATH lookup matches the lab's historical
    # buildbox-casd manifest against the same image family.
    assert containers["recc-casd"]["command"] == ["buildbox-casd"]
    assert not any(
        part.startswith("/") and part.endswith("buildbox-casd")
        for part in containers["recc-casd"]["command"]
    )


def test_shared_lab_recc_contract_uses_supported_environment_keys():
    config_map = yaml.safe_load(
        (ROOT / "manifests/buildstream-remote-cache-config.yaml").read_text(
            encoding="utf-8"
        )
    )
    environment = yaml.safe_load(config_map["data"]["recc-environment.conf"])

    assert environment == {
        "RECC_SERVER": "frontend.buildbarn.svc.cluster.local:8980",
        "RECC_CAS_SERVER": "frontend.buildbarn.svc.cluster.local:8980",
        "RECC_ACTION_CACHE_SERVER": "frontend.buildbarn.svc.cluster.local:8980",
        "RECC_ACTION_UNCACHEABLE": "0",
        "RECC_PROJECT_ROOT": "/workspace",
        "RECC_VERBOSE": "1",
    }
    assert "RECC_PREFIX" not in environment


def test_recc_seam_does_not_enable_unsupported_runner_fields_or_host_namespaces():
    config = (ROOT / "manifests/buildbarn-config.yaml").read_text(encoding="utf-8")
    worker = (ROOT / "manifests/buildbarn-worker.yaml").read_text(encoding="utf-8")
    element = (ROOT / "bst-prototype/elements/recc-baseline.bst").read_text(
        encoding="utf-8"
    )
    docs = (ROOT / "docs/reference/recc-runner-seam.md").read_text(
        encoding="utf-8"
    )

    assert "readinessCheckingPathnames:" not in config
    assert "\n      remoteApisSocketPath:" not in config
    assert "remote-apis-socket" not in element
    assert "unix:/tmp/casd.sock" not in element
    assert "hostPID:" not in worker
    assert "hostIPC:" not in worker
    assert "Shared RECC config contract" in docs
    assert "## Concrete blocker" in docs
    assert "remoteApisSocketPath" in docs


def test_all_buildstream_pipelines_mount_the_shared_recc_contract():
    for filename in (
        "bluefin-server-build-pipeline.yaml",
        "cosmic-build-pipeline.yaml",
        "dakota-build-pipeline.yaml",
        "bst-qa-pipeline.yaml",
    ):
        pipeline = (
            ROOT / "argo/workflow-templates" / filename
        ).read_text(encoding="utf-8")
        assert "- key: recc-environment.conf" in pipeline
        assert "path: recc-environment.conf" in pipeline


def test_production_lanes_keep_the_overlay_mounted_but_do_not_invoke_it():
    """The documented rollback keeps nested RECC out of production lanes.

    The helper remains mounted as part of the shared config contract for the
    operator-only baseline and a future runner-capability rollout, but the
    production lanes must not invoke it while the deployed runner lacks
    ``remoteApisSocketPath`` support.
    """

    for filename in (
        "dakota-build-pipeline.yaml",
        "cosmic-build-pipeline.yaml",
        "bluefin-server-build-pipeline.yaml",
        "bst-qa-pipeline.yaml",
    ):
        pipeline = (
            ROOT / "argo/workflow-templates" / filename
        ).read_text(encoding="utf-8")

        assert "- key: apply_recc_overlay.py" in pipeline
        assert "path: apply_recc_overlay.py" in pipeline
        assert "python3 /etc/buildstream/apply_recc_overlay.py" not in pipeline
        assert "kubectl get configmap buildbarn-config -n buildbarn" not in pipeline
        assert "RECC admission rejected" not in pipeline
        assert "remote-apis-socket" not in pipeline


def test_every_mandatory_recc_lane_is_refused_by_the_shared_overlay():
    """The kinds the templates pass must all be mandatory-RECC adapters."""

    overlay = (ROOT / "scripts/apply_recc_overlay.py").read_text(encoding="utf-8")
    namespace: dict = {}
    exec(compile(overlay, "apply_recc_overlay.py", "exec"), namespace)
    adapters = namespace["ADAPTERS"]

    for kind in ("dakota", "cosmic", "bluefin-server", "bst-qa"):
        assert adapters[kind].production, kind
    # Only the operator-driven baseline fixture may use the pilot flags.
    assert not adapters["bst-prototype"].production


def test_outer_admission_does_not_probe_the_unavailable_nested_runner():
    config = yaml.safe_load(
        (ROOT / "manifests/buildbarn-config.yaml").read_text(encoding="utf-8")
    )
    runner_jsonnet = config["data"]["runner.jsonnet"]
    probe = re.compile(r"^[ \t]*remoteApisSocketPath[ \t]*:", re.MULTILINE)

    # The deployed runner has no such field, so production lanes use the
    # documented outer-remote-execution rollback until a capable runner lands.
    assert not probe.search(runner_jsonnet)
    assert probe.search(runner_jsonnet.rstrip() + "\n  remoteApisSocketPath: 'x',")

    for template in GATED_TEMPLATES:
        text = (ROOT / "argo/workflow-templates" / template).read_text(
            encoding="utf-8"
        )
        assert "kubectl get configmap buildbarn-config -n buildbarn" not in text
        assert "remoteApisSocketPath[[:space:]]*:" not in text
        assert "RECC admission rejected" not in text


def test_cache_warmup_reuses_the_outer_remote_build_templates():
    for filename in (
        "dakota-build-pipeline.yaml",
        "cosmic-build-pipeline.yaml",
        "bluefin-server-build-pipeline.yaml",
    ):
        pipeline = (
            ROOT / "argo/workflow-templates" / filename
        ).read_text(encoding="utf-8")
        warmup = pipeline.index("name: build-warmup")
        assert pipeline.index("template: build-core", warmup) > warmup

    cache_warm = (
        ROOT / "argo/workflow-templates/bst-cache-warm.yaml"
    ).read_text(encoding="utf-8")
    for pipeline in (
        "dakota-build-pipeline",
        "cosmic-build-pipeline",
        "bluefin-server-build-pipeline",
    ):
        assert f"name: {pipeline}" in cache_warm
    assert cache_warm.count("template: build-warmup") == 3


def test_recc_overlay_configmap_embeds_the_lab_owned_helper():
    config_map = yaml.safe_load(
        (ROOT / "manifests/buildstream-remote-cache-config.yaml").read_text(
            encoding="utf-8"
        )
    )
    helper = (ROOT / "scripts/apply_recc_overlay.py").read_text(encoding="utf-8")

    assert config_map["data"]["recc-endpoint"] == (
        "grpc://frontend.buildbarn.svc.cluster.local:8980"
    )
    assert config_map["data"]["apply_recc_overlay.py"] == helper


def _worker_containers() -> list[str]:
    manifest = yaml.safe_load(
        (ROOT / "manifests/buildbarn-worker.yaml").read_text(encoding="utf-8")
    )
    return [
        container["name"]
        for container in manifest["spec"]["template"]["spec"]["containers"]
    ]


def _gate_pattern(template: str, node: str) -> str:
    text = (ROOT / "argo/workflow-templates" / template).read_text(encoding="utf-8")
    match = re.search(r'grep -Eq "(\^\$\{NODE\}[^"]+)"', text)
    assert match, f"{template} has no worker readiness gate"
    return match.group(1).replace("${NODE}", node)


def _status_line(node: str, readiness: dict[str, bool]) -> str:
    """Reproduce the gate's kubectl jsonpath output for one worker pod."""

    def ready(name: str) -> str:
        return "true" if readiness[name] else "false" if name in readiness else ""

    return (
        f"{node}|Running"
        f"|worker={ready('worker')}"
        f"|runner={ready('runner')}"
    )


def test_worker_admission_gates_match_containers_by_name_not_by_count():
    containers = _worker_containers()
    assert set(OUTER_RE_CONTAINERS) <= set(containers)
    # The DaemonSet carries at least one preparation sidecar beyond the outer
    # remote-execution pair; the gates must not count containers.
    assert len(containers) > len(OUTER_RE_CONTAINERS)

    for template in GATED_TEMPLATES:
        text = (ROOT / "argo/workflow-templates" / template).read_text(
            encoding="utf-8"
        )
        assert "containerStatuses[*].ready" not in text
        assert "true true" not in text
        for name in OUTER_RE_CONTAINERS:
            assert f'containerStatuses[?(@.name=="{name}")]' in text
        assert "recc-casd" not in text


def test_worker_admission_gates_ignore_the_recc_preparation_sidecar():
    healthy = _status_line("ghost", {"worker": True, "runner": True})
    degraded_worker = _status_line("ghost", {"worker": False, "runner": True})
    degraded_runner = _status_line("ghost", {"worker": True, "runner": False})
    missing_worker = "ghost|Running|worker=|runner=true"

    for template in GATED_TEMPLATES:
        pattern = _gate_pattern(template, "ghost")
        # A three-container worker whose recc-casd sidecar is unready is still
        # admitted: that socket is a separate health boundary.
        assert re.search(pattern, healthy)
        assert not re.search(pattern, degraded_worker)
        assert not re.search(pattern, degraded_runner)
        assert not re.search(pattern, missing_worker)
        assert not re.search(pattern, healthy.replace("Running", "Pending"))
        assert not re.search(pattern, healthy.replace("ghost", "exo-0"))


def test_outer_worker_readiness_is_decoupled_from_the_nested_socket():
    config = (ROOT / "manifests/buildbarn-config.yaml").read_text(encoding="utf-8")
    manifest = yaml.safe_load(
        (ROOT / "manifests/buildbarn-worker.yaml").read_text(encoding="utf-8")
    )
    containers = {
        container["name"]: container
        for container in manifest["spec"]["template"]["spec"]["containers"]
    }

    # Outer BuildBarn admission must not depend on the unused nested socket.
    assert "readinessCheckingPathnames:" not in config
    assert "readinessProbe" not in containers["worker"]
    assert "readinessProbe" not in containers["runner"]
    # The sidecar keeps its own probes on that socket.
    assert containers["recc-casd"]["readinessProbe"]["exec"]["command"] == [
        "test",
        "-S",
        "/run/buildbarn/recc/casd.sock",
    ]
