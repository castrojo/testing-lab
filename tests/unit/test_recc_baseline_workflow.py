from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "argo/workflow-templates/recc-baseline-pipeline.yaml"
FIXTURE = ROOT / "bst-prototype/elements/recc-baseline.bst"


def _manifest():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run_template():
    return next(
        template
        for template in _manifest()["spec"]["templates"]
        if template["name"] == "run"
    )


def test_recc_fixture_generates_separate_launcher_and_compiler_arguments():
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    variables = fixture["variables"]
    build_commands = fixture["config"]["build-commands"]
    command = build_commands[0]

    assert variables["compiler"] == "/usr/bin/g++"
    assert variables["compiler_launcher"] == ""
    assert variables["(?)"][0]["recc != \"buildstream-only\""]["compiler_launcher"] == (
        "recc"
    )
    assert fixture["environment"]["CXX"] == "%{compiler}"
    assert fixture["environment"]["CXX_LAUNCHER"] == "%{compiler_launcher}"
    assert '"${CXX_LAUNCHER}" "${CXX}" "$@"' in command
    assert command.count("run_cxx ") == 3
    assert command.count(" -c ") == 2
    assert '"$CXX" ' not in command


def test_recc_baseline_is_an_isolated_operator_only_template():
    manifest = _manifest()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert manifest["metadata"]["name"] == "recc-baseline-pipeline"
    assert manifest["metadata"]["labels"]["bluefin.io/operator-only"] == "true"
    assert manifest["spec"]["entrypoint"] == "run"
    assert "templateRef" not in text
    assert "dakota-build-pipeline" not in text
    assert "cosmic" not in text
    assert "bluefin-server" not in text


def test_parameters_target_recc_baseline_and_fail_closed_provider_defaults():
    parameters = {
        parameter["name"]: parameter["value"]
        for parameter in _manifest()["spec"]["arguments"]["parameters"]
    }
    assert parameters["mode"] == "buildstream-only"
    assert parameters["run-id"] == ""
    assert parameters["cache-policy"] == "cold"
    assert parameters["recc-provider"] == (
        "freedesktop-sdk.bst:components/buildbox.bst"
    )
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'ELEMENT="recc-baseline.bst"' in text
    assert "required element bst-prototype/elements/${ELEMENT}" in text
    assert "recc-provider is empty" in text


def test_buildstream_cache_is_ephemeral_and_no_host_usr_is_mounted():
    run = _run_template()
    volumes = {volume["name"]: volume for volume in run["volumes"]}
    assert "ephemeral" in volumes["bst-cache"]
    assert (
        volumes["bst-cache"]["ephemeral"]["volumeClaimTemplate"]["spec"]["resources"][
            "requests"
        ]["storage"]
        == "20Gi"
    )
    mounts = run["script"]["volumeMounts"]
    assert all(not mount["mountPath"].startswith("/usr") for mount in mounts)
    assert all("hostPath" not in volume for volume in run["volumes"])
    assert "servers: []" in WORKFLOW.read_text(encoding="utf-8")


def test_prototype_pins_compiler_and_recc_provider_junction():
    project = yaml.safe_load((ROOT / "bst-prototype/project.conf").read_text())
    fixture = yaml.safe_load(FIXTURE.read_text())
    junction = yaml.safe_load(
        (ROOT / "bst-prototype/elements/freedesktop-sdk.bst").read_text()
    )

    assert "include/aliases.yml" in project["(@)"]
    assert "plugins" in project
    assert project["plugins"][0]["sources"] == ["git_repo"]
    assert "freedesktop-sdk.bst:components/gcc.bst" in fixture["build-depends"]
    assert junction["sources"][0]["ref"].endswith(
        "57149392fe26548b0e7c50a2e171e3aac005a412"
    )


def test_buildstream_config_uses_existing_configmap_key():
    run = _run_template()
    config = next(
        volume["configMap"]
        for volume in run["volumes"]
        if volume["name"] == "buildstream-config"
    )
    assert config["name"] == "buildstream-remote-cache"
    assert config["items"] == [
        {"key": "dakota-buildstream.conf", "path": "buildstream.conf"},
        {"key": "recc-environment.conf", "path": "recc-environment.conf"},
        {"key": "apply_recc_overlay.py", "path": "apply_recc_overlay.py"},
    ]


def test_recc_modes_require_pilot_overlay_and_remote_execution_is_blocked():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--pilot-cache-only" in text
    assert '--recc-provider "${RECC_PROVIDER}"' in text
    assert "remote-execution is blocked" in text
    assert "--runner-capability" not in text
    assert (
        'if [[ "${MODE}" != "buildstream-only" && -z "${RECC_PROVIDER}" ]]'
        in text
    )
    assert text.index('if [[ "${MODE}" != "buildstream-only"') < text.index(
        "Applying operator-only RECC cache pilot overlay"
    )


def test_metadata_outputs_preserve_remote_cache_and_unavailable_fields():
    run = _run_template()
    output_names = {
        parameter["name"]: parameter["valueFrom"]["path"]
        for parameter in run["outputs"]["parameters"]
    }
    assert output_names["metadata"] == "/work/recc-baseline-metadata.json"
    assert output_names["evidence"] == "/work/recc-baseline-evidence.json"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '"evidence_capture"' in text
    assert '"unavailable_fields"' in text
    assert "remote RECC cache preserved" in text
    assert "--prometheus-before" in text
    assert "--prometheus-after" in text
    assert "/work/recc.log" in text
    assert "find /root/.cache/buildstream" in text
    assert "capture_buildbarn_metrics" in text
    assert "EVIDENCE_FAILED=1" in text
    assert "|| true" not in text[text.index("RECC evidence collector"):text.index("trap finalize EXIT")]


def test_justfile_exposes_all_operator_parameters():
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    assert "run-recc-baseline *args:" in justfile
    assert "workflowtemplate/recc-baseline-pipeline" in justfile
    for parameter in ("mode", "run-id", "cache-policy", "recc-provider"):
        assert f"-p {parameter}=" in justfile


def test_justfile_named_recc_arguments_are_preserved_for_parsing():
    result = subprocess.run(
        [
            "just",
            "--dry-run",
            "--justfile",
            str(ROOT / "Justfile"),
            "run-recc-baseline",
            "cache-policy=both",
            "recc-provider=freedesktop-sdk.bst:components/buildbox.bst",
            "mode=cache-only",
            "run-id=warm-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert (
        "for arg in cache-policy=both "
        "recc-provider=freedesktop-sdk.bst:components/buildbox.bst "
        "mode=cache-only run-id=warm-run;"
    ) in output
    assert 'mode=*) MODE="${arg#mode=}" ;;' in output
    assert 'run-id=*) RUN_ID="${arg#run-id=}" ;;' in output
    assert 'cache-policy=*) CACHE_POLICY="${arg#cache-policy=}" ;;' in output
    assert 'recc-provider=*) RECC_PROVIDER="${arg#recc-provider=}" ;;' in output
