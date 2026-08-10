from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "argo/workflow-templates/bluefin-qa-pipeline.yaml"
SYSTEMD_RUNNER = ROOT / "argo/workflow-templates/run-systemd-container-tests.yaml"
FORBIDDEN = (
    "assert-cd",
    "containerdisk-tag",
    "provision-containerdisk-vm",
    "run-gnome-tests",
    "teardown-vm",
    "qa-vm-fleet",
    "kubectl delete vm",
)


def _systemd_run_tests_template():
    """The `run-tests` template of the native-systemd runner, parsed."""
    import yaml

    document = yaml.safe_load(SYSTEMD_RUNNER.read_text(encoding="utf-8"))
    templates = {
        template["name"]: template for template in document["spec"]["templates"]
    }
    return templates["run-tests"]


def _heredoc(name, text):
    """The body of a quoted `<<'NAME'` heredoc, without its delimiters."""
    match = re.search(r"<<'%s'\n(.*?)\n\s*%s\n" % (name, name), text, re.S)
    assert match is not None, f"{name} heredoc not found"
    return match.group(1)


def _systemd_runner_blocks():
    """Runner source plus each nested heredoc, so assertions can be scoped."""
    runner = _systemd_run_tests_template()["script"]["source"]
    target_suite = _heredoc("TARGET_SUITE", runner)
    return {
        "runner": runner,
        "TARGET_SETUP": _heredoc("TARGET_SETUP", runner),
        "TARGET_SUITE": target_suite,
        "RUN_BEHAVE": _heredoc("RUN_BEHAVE", target_suite),
    }


def _normalize_argo_substitutions(shell):
    """Argo `{{…}}` placeholders replaced by an inert word.

    Argo expands them before bash ever sees the script. They are always
    quoted here, so bash would parse them as literals anyway, but replacing
    them keeps the syntax check honest about what actually runs.
    """
    return re.sub(r"\{\{[^{}]+\}\}", "argo-substitution", shell)


def _bash(argv, stdin):
    if shutil.which("bash") is None:
        pytest.skip("bash is required to check the runner's shell blocks")
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, timeout=120
    )


def _brew_preinstall_settle_block():
    """The `suite=homebrew` settle block of run-behave.sh, on its own.

    Sliced at the suite guard and its column-0 `fi`, so it can be executed
    against stub `systemctl`/`sleep` without the surrounding behave run.
    """
    behave = _systemd_runner_blocks()["RUN_BEHAVE"]
    guard = 'if [[ "${SUITE}" == "homebrew" ]]; then'
    tail = behave[behave.index(guard):]
    end = re.search(r"\nfi\n", tail)
    assert end is not None, "the homebrew settle block has no column-0 `fi`"
    return tail[: end.end()]


# `systemctl` and `sleep` are shell functions so the block's own
# `env XDG_RUNTIME_DIR=… systemctl …` calls resolve to them (`env` is stubbed
# to drop leading assignments). `sleep` counts polls in the parent shell — the
# property reads happen inside `$(…)` subshells, so only the stub's *input*
# can cross that boundary, which is exactly how the state sequences advance.
_SETTLE_STUBS = """
set -euo pipefail
SUITE=homebrew
RUNTIME_DIR=/run/user/1000
XDG_RUNTIME_DIR=/run/user/1000
SLEEPS=0
env() {
  while [[ $# -gt 0 && "$1" == *=* ]]; do shift; done
  "$@"
}
sleep() { SLEEPS=$((SLEEPS + 1)); }
_seq() {
  local -a values
  read -r -a values <<< "$1"
  local idx="${SLEEPS}"
  (( idx >= ${#values[@]} )) && idx=$(( ${#values[@]} - 1 ))
  echo "${values[idx]}"
}
systemctl() {
  local verb="" property="" arg
  for arg in "$@"; do
    case "${arg}" in
      --property=*) property="${arg#--property=}" ;;
      show|status|stop|reset-failed|start) [[ -n "${verb}" ]] || verb="${arg}" ;;
    esac
  done
  if [[ "${verb}" == show ]]; then
    case "${property}" in
      ActiveState) _seq "${ACTIVE_SEQ}" ;;
      SubState) _seq "${SUB_SEQ}" ;;
      LoadState) echo "${LOAD_STATE}" ;;
      ConditionResult) echo "${CONDITION_RESULT}" ;;
      *) echo "" ;;
    esac
    return 0
  fi
  echo "STUB systemctl ${verb}" >&2
  return "${STUB_RC:-0}"
}
"""


def _run_settle_block(
    active_states,
    sub_states,
    load_state="loaded",
    condition_result="yes",
    stub_rc=0,
):
    """Run the homebrew settle block against a scripted unit state sequence.

    Each entry is the state reported after that many stubbed `sleep` calls,
    so `["activating", "active"]` is a run that is in flight on the first
    look and settled one poll later. Returns the completed process; the
    block's diagnostics and every non-`show` systemctl call land on stderr.
    """
    script = "".join(
        [
            _SETTLE_STUBS,
            f'ACTIVE_SEQ="{" ".join(active_states)}"\n',
            f'SUB_SEQ="{" ".join(sub_states)}"\n',
            f'LOAD_STATE="{load_state}"\n',
            f'CONDITION_RESULT="{condition_result}"\n',
            f"STUB_RC={stub_rc}\n",
            _brew_preinstall_settle_block(),
            '\necho "SLEEPS=${SLEEPS}" >&2\n',
        ]
    )
    return _bash(["bash"], script)


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


def test_bluefin_pipeline_accepts_explicit_template_ref_arguments():
    import yaml

    pipeline = yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))
    templates = {template["name"]: template for template in pipeline["spec"]["templates"]}
    parameters = {
        parameter["name"]
        for parameter in templates["pipeline"]["inputs"]["parameters"]
    }

    assert parameters == {
        "image",
        "image-tag",
        "image-digest",
        "suites",
        "variant",
        "branch",
        "testsuite-branch",
        "testsuite-repo",
    }


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


def test_container_runner_pull_retry_budget_survives_slow_contended_pulls():
    # Live incident bluefin-qa-pipeline-42jhj: under real concurrent
    # ghost-container-qa demand (up to 6 simultaneous podman pulls sharing one
    # node's egress), podman's local blob cache carries completed blobs
    # forward across attempts (each retry starts faster than the last), so
    # attempt 3/3 reached the final blob of the image and missed the 480s
    # deadline by only ~47s. PULL_ATTEMPTS=3 was calibrated for the original
    # instant "unexpected EOF" hang, not for this slow-but-progressing
    # pattern. Bumping to 4 attempts (worst case 2010s) gives one more full
    # bounded window -- comfortably more than the ~47s that was missing --
    # while every attempt remains individually timeout-bounded (no unbounded
    # retries) and activeDeadlineSeconds (3600s) is untouched.
    content = (ROOT / "argo/workflow-templates/run-container-tests.yaml").read_text(
        encoding="utf-8"
    )

    assert "PULL_ATTEMPTS=4" in content
    assert "PULL_TIMEOUT_SECONDS=480" in content
    assert (
        'timeout "${PULL_TIMEOUT_SECONDS}" podman pull "${PODMAN_PULL_TLS_ARGS[@]}" "${TARGET_IMAGE}"'
        in content
    )
    assert "activeDeadlineSeconds: 3600" in content


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


def test_native_systemd_runner_accepts_homebrew_in_both_suite_allowlists():
    # The runner guards the suite twice: once before it touches the cluster and
    # once inside the heredoc that writes /workspace/run-behave.sh. A suite that
    # is added to only one of them either never provisions or dies inside the
    # qecore session with an opaque exit 2.
    blocks = _systemd_runner_blocks()
    allow = "smoke|common|developer|software|system|homebrew) ;;"

    assert allow in blocks["runner"]
    assert allow in blocks["RUN_BEHAVE"]
    assert blocks["runner"].count(allow) == 2  # runner + the nested RUN_BEHAVE
    assert blocks["runner"].count("Unsupported container suite: ${SUITE}") == 2


def test_native_systemd_runner_deadline_is_sized_for_the_homebrew_lane():
    # The homebrew lane adds a network-bound cask install and 15 Ptyxis-driven
    # scenarios on top of the shared pull/boot/session cost, so the shared 3600s
    # hang guard no longer bounds the slowest suite this template can run.
    template = _systemd_run_tests_template()
    content = SYSTEMD_RUNNER.read_text(encoding="utf-8")

    assert template["activeDeadlineSeconds"] == 7200
    assert "sized for the slowest lane (suite=homebrew)" in content


def test_native_systemd_runner_budgets_one_cask_install_and_bounds_the_wait():
    # brew-preinstall is content-addressed, so only one run pays the network
    # cost: whichever of the session's in-flight run and the suite's explicit
    # start goes first, the other exits early on the unchanged Brewfile hash.
    # The budget must say that, and the in-flight wait must be capped at the
    # same number the budget spends on that install — an uncapped wait would
    # bound the lane by the deadline instead.
    content = SYSTEMD_RUNNER.read_text(encoding="utf-8")
    behave = _systemd_runner_blocks()["RUN_BEHAVE"]
    budget = content.split("activeDeadlineSeconds:", 1)[0].rsplit("# 2h,", 1)[1]

    assert "one network cask install ~900s" in budget
    assert "content-addressed" in budget
    assert "not once\n    # per restart attempt" in budget
    assert "StartLimitBurst=3 ~900s" not in budget
    assert "BREW_PREINSTALL_WAIT_SECONDS=900" in behave
    assert "BREW_PREINSTALL_POLL_SECONDS=10" in behave
    assert "BREW_PREINSTALL_WAIT_SECONDS (the same 900s)" in budget

    # 600 + 120 + 300 + 300 + 360 + 900 + 2700 = 5280s, and the one path that
    # repeats the install (an in-flight run that fails after being waited out)
    # adds 900 more, which is still inside 7200.
    assert 600 + 120 + 300 + 300 + 360 + 900 + 2700 == 5280
    assert 5280 + 900 < _systemd_run_tests_template()["activeDeadlineSeconds"]
    assert "~88min" in budget
    assert "~17min inside this deadline" in budget


def test_native_systemd_runner_asks_logind_for_the_runtime_path_exactly_once():
    # A second RuntimePath lookup would return an answer nothing has checked
    # against the two sockets asserted in TARGET_SETUP, and could differ.
    blocks = _systemd_runner_blocks()

    assert blocks["runner"].count("--property=RuntimePath") == 1
    assert "--property=RuntimePath" in blocks["TARGET_SETUP"]
    assert "--property=RuntimePath" not in blocks["RUN_BEHAVE"]
    assert (
        "RUNTIME_DIR=$(loginctl show-user bluefin-test --property=RuntimePath"
        " --value 2>/dev/null || true)" in blocks["TARGET_SETUP"]
    )


def test_native_systemd_runner_persists_the_validated_runtime_dir_for_readers():
    # TARGET_SETUP proves the directory carries both sockets, then hands it to
    # the runner and the suite through the same durable /workspace contract the
    # suite inputs use.
    blocks = _systemd_runner_blocks()
    runner_after_setup = blocks["runner"].split("TARGET_SETUP\n", 2)[-1]

    assert (
        "printf '%s\\n' \"${RUNTIME_DIR}\" >/workspace/qa-runtime-dir"
        in blocks["TARGET_SETUP"]
    )
    assert "cat /workspace/qa-runtime-dir" in runner_after_setup
    assert (
        "TARGET_SETUP persisted no user-manager runtime directory at"
        " /workspace/qa-runtime-dir" in runner_after_setup
    )
    assert "loginctl" not in blocks["RUN_BEHAVE"]
    assert "qa-runtime-dir" not in blocks["RUN_BEHAVE"]
    assert 'RUNTIME_DIR=/home/bluefin-test/run' in runner_after_setup
    assert 'RUNTIME_DIR=%q' in blocks["TARGET_SUITE"]
    assert "source /workspace/qa-suite.env" in blocks["RUN_BEHAVE"]


def test_native_systemd_runner_socket_failures_carry_named_diagnostics():
    # Under `set -euo pipefail` a bare `test -S` or `systemctl start` aborts the
    # script before anything can explain it. Every provisioning step captures
    # its exit code; the binary and socket checks decide and report.
    setup = _systemd_runner_blocks()["TARGET_SETUP"]

    assert "report_user_manager_failure() {" in setup
    assert "systemctl status --no-pager --full user@1000.service >&2 || true" in setup
    assert "loginctl show-user bluefin-test >&2 || true" in setup
    assert 'if [[ ! -S "${RUNTIME_DIR}/systemd/private" ]]; then' in setup
    assert 'if [[ ! -S "${RUNTIME_DIR}/bus" ]]; then' in setup
    assert "test -S" not in setup
    assert setup.count("report_user_manager_failure ") == 3
    assert setup.count("exit 1") == 4  # brew binary gate + the three above

    for capture in (
        "systemctl start brew-setup.service || BREW_SETUP_RC=$?",
        "loginctl enable-linger bluefin-test || LINGER_RC=$?",
        "systemctl start user@1000.service || USER_MANAGER_RC=$?",
        "systemctl --user start dbus.socket || DBUS_SOCKET_RC=$?",
    ):
        assert capture in setup

    assert "journalctl --no-pager --unit brew-setup.service >&2 || true" in setup
    assert (
        "exposed no control socket at ${RUNTIME_DIR}/systemd/private" in setup
    )
    assert "exposed no session bus at ${RUNTIME_DIR}/bus" in setup


def test_native_systemd_runner_clears_the_brew_preinstall_restart_race():
    # brew-preinstall.service is Type=oneshot, RemainAfterExit=true,
    # Restart=on-failure, RestartSec=30, StartLimitBurst=3, and is pulled in by
    # graphical-session.target. ActiveState alone cannot tell a healthy run in
    # flight from the auto-restart gap after a failure, so the lane reads
    # SubState too: wait out `activating (start)`, cancel `auto-restart`.
    # Then diagnose, stop (which cancels a queued restart and clears a latched
    # `active`), and reset — all before behave, never after.
    behave = _systemd_runner_blocks()["RUN_BEHAVE"]

    show = behave.index("BREW_PREINSTALL_STATE=$(brew_preinstall_property ActiveState)")
    substate = behave.index(
        "BREW_PREINSTALL_SUBSTATE=$(brew_preinstall_property SubState)"
    )
    wait = behave.index("waiting up to ${BREW_PREINSTALL_WAIT_SECONDS}s")
    diagnose = behave.index("before behave started it")
    stop = behave.index("systemctl --user stop brew-preinstall.service")
    reset = behave.index("systemctl --user reset-failed brew-preinstall.service")
    run = behave.index("python3 -m behave")

    assert show < substate < wait < diagnose < stop < reset < run
    assert "BREW_PREINSTALL_STATE=${BREW_PREINSTALL_STATE:-unknown}" in behave
    assert "BREW_PREINSTALL_SUBSTATE=${BREW_PREINSTALL_SUBSTATE:-unknown}" in behave
    assert (
        'if [[ "${BREW_PREINSTALL_STATE}" == "activating" \\\n'
        '    && "${BREW_PREINSTALL_SUBSTATE}" != auto-restart* ]]; then' in behave
    )
    assert (
        'if [[ "${BREW_PREINSTALL_STATE}" == "inactive" \\\n'
        '    || "${BREW_PREINSTALL_STATE}" == "active" ]]; then' in behave
    )
    # `auto-restart-queued` is the systemd >= 254 spelling; the prefix glob
    # must match both, and neither may be waited out.
    assert behave.count('"${BREW_PREINSTALL_SUBSTATE}" != auto-restart*') == 1
    assert behave.count('"${BREW_PREINSTALL_SUBSTATE}" == auto-restart*') == 1
    assert "systemctl --user is-failed" not in behave

    # inactive/active are settled, but they are also what a unit that systemd
    # never ran looks like — LoadState/ConditionResult make that visible.
    assert "LoadState=$(brew_preinstall_property LoadState)" in behave
    assert "ConditionResult=$(brew_preinstall_property ConditionResult)" in behave

    # Both cleanup steps capture their exit code and report it by name instead
    # of discarding it with `|| true`.
    assert (
        "systemctl --user stop brew-preinstall.service || BREW_PREINSTALL_STOP_RC=$?"
        in behave
    )
    assert (
        "systemctl --user reset-failed brew-preinstall.service"
        " || BREW_PREINSTALL_RESET_RC=$?" in behave
    )
    assert "reset-failed brew-preinstall.service || true" not in behave
    assert 'if [[ "${BREW_PREINSTALL_STOP_RC}" -ne 0 ]]; then' in behave
    assert 'if [[ "${BREW_PREINSTALL_RESET_RC}" -ne 0 ]]; then' in behave
    assert "a queued auto-restart may still race the suite's explicit start" in behave
    assert "the suite may report a stale start limit instead of the real error" in behave

    # The whole block is guarded on the lane, every manager call goes through
    # the validated runtime directory, and a session that disagrees with it is
    # called out rather than silently honoured.
    guarded = behave.split('if [[ "${SUITE}" == "homebrew" ]]; then', 1)[1]
    assert guarded.index("systemctl --user stop brew-preinstall.service") < guarded.index(
        "\nfi\n"
    )
    lines = behave.splitlines()
    user_calls = [
        (lines[index - 1].strip(), line.strip())
        for index, line in enumerate(lines)
        if line.strip().startswith("systemctl --user")
    ]
    assert {call.split()[2] for _, call in user_calls} == {
        "show",
        "status",
        "stop",
        "reset-failed",
    }
    assert all(
        previous == 'env XDG_RUNTIME_DIR="${RUNTIME_DIR}" \\'
        for previous, _ in user_calls
    )
    assert (
        "warning: session XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR:-}' differs from"
        " the lane's '${RUNTIME_DIR}'" in behave
    )


def test_native_systemd_runner_shell_blocks_parse_as_bash():
    # These blocks only ever run inside the target, hours into a workflow, so a
    # syntax error costs a full lane. `bash -n` every one of them here: the
    # runner script, both heredocs it writes into the target, the run-behave.sh
    # body nested inside the second, and the homebrew settle slice the mocked
    # branch tests below execute.
    blocks = dict(_systemd_runner_blocks())
    blocks["brew-preinstall-settle"] = _brew_preinstall_settle_block()

    for name, shell in blocks.items():
        result = _bash(["bash", "-n"], _normalize_argo_substitutions(shell))
        assert result.returncode == 0, f"{name} is not valid bash:\n{result.stderr}"

    # The check is only worth anything if the extraction really produced the
    # script — a silently empty block would pass `bash -n` too.
    assert blocks["RUN_BEHAVE"].startswith("#!/bin/bash")
    assert "python3 -m behave" in blocks["RUN_BEHAVE"]
    assert blocks["brew-preinstall-settle"].rstrip().endswith("\nfi")
    assert "{{" not in _normalize_argo_substitutions(blocks["runner"])


def test_brew_preinstall_settle_waits_out_a_healthy_in_flight_run():
    # ActiveState=activating with SubState=start is the session's own cask
    # install still running. Killing it discards the work the deadline budgets
    # for and leaves the suite a half-applied prefix, so the lane waits.
    result = _run_settle_block(
        active_states=["activating", "activating", "active"],
        sub_states=["start", "start", "exited"],
    )

    assert result.returncode == 0, result.stderr
    stderr = result.stderr
    assert "waiting up to 900s for the in-flight run instead of killing it" in stderr
    assert "is active (exited) after waiting 20s" in stderr
    assert "SLEEPS=2" in stderr
    # It waited first and only then settled the unit for behave.
    assert stderr.index("waiting up to 900s") < stderr.index("STUB systemctl stop")
    assert stderr.index("STUB systemctl stop") < stderr.index(
        "STUB systemctl reset-failed"
    )


def test_brew_preinstall_settle_cancels_a_queued_auto_restart_without_waiting():
    # The auto-restart gap is not a healthy run: there is a queued restart job
    # that reset-failed does not cancel. Diagnose and stop it immediately —
    # waiting out the RestartSec gap would only line the restart up against
    # the suite's own start.
    for substate in ("auto-restart", "auto-restart-queued"):
        result = _run_settle_block(
            active_states=["activating"], sub_states=[substate]
        )

        assert result.returncode == 0, result.stderr
        stderr = result.stderr
        assert "SLEEPS=0" in stderr
        assert "waiting up to" not in stderr
        assert f"was activating ({substate}) before behave started it" in stderr
        assert stderr.index("STUB systemctl status") < stderr.index(
            "STUB systemctl stop"
        )
        assert stderr.index("STUB systemctl stop") < stderr.index(
            "STUB systemctl reset-failed"
        )


def test_brew_preinstall_settle_diagnoses_a_failed_unit_then_clears_it():
    result = _run_settle_block(active_states=["failed"], sub_states=["failed"])

    assert result.returncode == 0, result.stderr
    stderr = result.stderr
    assert "SLEEPS=0" in stderr
    assert "was failed (failed) before behave started it" in stderr
    assert "STUB systemctl status" in stderr
    assert stderr.index("STUB systemctl stop") < stderr.index(
        "STUB systemctl reset-failed"
    )


def test_brew_preinstall_settle_bounds_the_wait_for_a_wedged_run():
    # A run that never leaves `activating` must not hold the lane until the
    # workflow deadline: the wait is capped at BREW_PREINSTALL_WAIT_SECONDS
    # (900s / 10s polls = 90 iterations), then it is diagnosed and cleared
    # like any other unsettled state.
    result = _run_settle_block(
        active_states=["activating"], sub_states=["start"]
    )

    assert result.returncode == 0, result.stderr
    stderr = result.stderr
    assert "SLEEPS=90" in stderr
    assert "is activating (start) after waiting 900s" in stderr
    assert "was activating (start) before behave started it" in stderr
    assert "STUB systemctl status" in stderr
    assert "STUB systemctl stop" in stderr


def test_brew_preinstall_settle_reports_load_state_for_a_settled_unit():
    # `inactive` is indistinguishable from "unit not installed" or "systemd
    # skipped it on ConditionUser/ConditionPathExists" — and a start of either
    # exits 0, so the suite would pass on nothing. Say which one it is.
    missing = _run_settle_block(
        active_states=["inactive"],
        sub_states=["dead"],
        load_state="not-found",
    )
    skipped = _run_settle_block(
        active_states=["inactive"],
        sub_states=["dead"],
        condition_result="no",
    )

    assert missing.returncode == 0, missing.stderr
    assert (
        "is inactive (dead, LoadState=not-found, ConditionResult=yes)"
        " before behave started it" in missing.stderr
    )
    assert "STUB systemctl status" not in missing.stderr  # settled: no dump
    assert skipped.returncode == 0, skipped.stderr
    assert (
        "is inactive (dead, LoadState=loaded, ConditionResult=no)"
        " before behave started it" in skipped.stderr
    )


def test_brew_preinstall_settle_survives_a_failing_stop_and_reset():
    # Neither cleanup step is allowed to abort run-behave.sh under `set -e`,
    # and neither may swallow its exit code: behave still has to run, with the
    # reason the next failure will be blamed on printed by name.
    result = _run_settle_block(
        active_states=["failed"], sub_states=["failed"], stub_rc=1
    )

    assert result.returncode == 0, result.stderr
    stderr = result.stderr
    assert "warning: stopping brew-preinstall.service before behave exited 1" in stderr
    assert "(state was failed/failed)" in stderr
    assert "warning: reset-failed brew-preinstall.service exited 1" in stderr
    assert "SLEEPS=0" in stderr


def test_native_systemd_runner_deletes_the_target_pod_on_termination_signals():
    # activeDeadlineSeconds expiry and `argo terminate` arrive as signals, and
    # an untrapped SIGTERM kills bash without running the EXIT trap — the
    # privileged 8Gi/4CPU target Pod would then wait for owner-reference GC.
    runner = _systemd_runner_blocks()["runner"]

    assert "cleanup_target() {" in runner
    assert (
        'kubectl delete pod "${TARGET_POD}" -n "{{workflow.namespace}}" \\\n'
        "    --ignore-not-found --wait=false || true" in runner
    )
    assert "trap cleanup_target EXIT" in runner
    assert "trap 'cleanup_target; exit 143' TERM" in runner
    assert "trap 'cleanup_target; exit 130' INT" in runner
    assert runner.count("kubectl delete pod") == 1
    assert runner.index("trap cleanup_target EXIT") < runner.index("kubectl wait")


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
    gnome_runner = (ROOT / "argo/workflow-templates/run-gnome-tests.yaml").read_text(
        encoding="utf-8"
    )
    assert ".local/qecore/bin/qecore_*" in gnome_runner
    assert "command -v qecore-headless" in gnome_runner
    assert "qecore-headless is not installed in the VM" in gnome_runner
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


def test_dakota_digest_poller_forwards_remote_digest_to_pipeline_parameter():
    import yaml

    poller = yaml.safe_load(
        (ROOT / "manifests/image-poll-dakota.yaml").read_text(encoding="utf-8")
    )
    pipeline = yaml.safe_load(
        (ROOT / "argo/workflow-templates/dakota-qa-pipeline.yaml").read_text(
            encoding="utf-8"
        )
    )
    pipeline_parameters = {
        parameter["name"]
        for parameter in pipeline["spec"]["arguments"]["parameters"]
    }
    tasks = poller["spec"]["workflowSpec"]["templates"][0]["dag"]["tasks"]
    run_pipeline = next(task for task in tasks if task["name"] == "run-pipeline")
    arguments = {
        parameter["name"]: parameter["value"]
        for parameter in run_pipeline["arguments"]["parameters"]
    }

    assert "image-digest" in pipeline_parameters
    assert arguments["image-digest"] == (
        "{{tasks.check-digest.outputs.parameters.remote-digest}}"
    )


def test_pr_poller_carries_image_digest_into_dakota_qa_workflow():
    poller = (ROOT / "argo/workflow-templates/pr-poller.yaml").read_text(
        encoding="utf-8"
    )

    dakota_block = poller.split("name: qa-dakota", 1)[1].split("name: qa-bluefin", 1)[0]
    assert "- name: image-digest" in dakota_block
    # The inline child-workflow manifest escapes Argo expressions so they are
    # resolved by the child workflow, not the parent poller.
    assert (
        'value: "${ARGO_OPEN}workflow.parameters.image-digest${ARGO_CLOSE}"'
        in dakota_block
    )
    assert "dakota-qa-pipeline" in dakota_block


def test_caller_contract_requires_forked_testsuite_repo_and_branch():
    contract = (ROOT / "docs/skills/argo-workflows/authoring.md").read_text(
        encoding="utf-8"
    )

    assert "- `testsuite-repo`" in contract
    assert "override both `testsuite-repo` and `testsuite-branch`" in contract
