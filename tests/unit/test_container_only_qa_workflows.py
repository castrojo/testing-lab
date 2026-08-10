from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile

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
    """The body of a quoted `<<'NAME'` heredoc, without its delimiters.

    The redirect may carry trailing tokens (`<<'PY' || RC=$?`), which bash
    applies to the command, not the heredoc — so match to end of line rather
    than demanding the newline immediately after the delimiter.
    """
    match = re.search(r"<<'%s'[^\n]*\n(.*?)\n\s*%s\n" % (name, name), text, re.S)
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


def _run_tests_budget_comment():
    """The YAML comment block directly above `activeDeadlineSeconds`.

    Comments do not survive parsing, so this walks the raw file backwards from
    the setting to the first non-comment line, which is the whole rationale and
    nothing else.
    """
    lines = SYSTEMD_RUNNER.read_text(encoding="utf-8").splitlines()
    index = next(
        i for i, line in enumerate(lines) if line.strip().startswith("activeDeadlineSeconds:")
    )
    start = index
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    assert start < index, "activeDeadlineSeconds carries no explanatory comment"
    return "\n".join(lines[start:index])


def _budget_entries(comment, marker):
    """Every `<marker>: <seconds> - <description>` entry in a budget comment."""
    return [
        (int(seconds), description.strip())
        for seconds, description in re.findall(
            rf"^\s*#\s*{marker}:\s*(\d+)\s*-\s*(.*)$", comment, re.M
        )
    ]


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
# property reads happen inside a process substitution, so only the stub's
# *input* can cross that boundary, which is exactly how the state sequences
# advance. `show` answers in the `key=value` form the block parses, honouring
# systemd's real behaviour of printing one line per requested property; with
# SHOW_EMPTY=1 it prints nothing at all, which is what an unreachable manager
# or an unreadable property looks like.
_SETTLE_STUBS = """
set -euo pipefail
# The block reads properties with `2>/dev/null`, so the stub announces each
# `show` on fd 3 — a duplicate of the captured stderr that the block's own
# redirect cannot swallow. That is how the tests see *how many* calls were
# made and which properties each one asked for.
exec 3>&2
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
  local verb="" arg property
  local -a properties=()
  for arg in "$@"; do
    case "${arg}" in
      --property=*) properties+=("${arg#--property=}") ;;
      show|status|stop|reset-failed|start) [[ -n "${verb}" ]] || verb="${arg}" ;;
    esac
  done
  if [[ "${verb}" == show ]]; then
    echo "STUB show: ${properties[*]}" >&3
    if [[ "${SHOW_EMPTY:-0}" == 1 ]]; then
      return 0
    fi
    for property in "${properties[@]}"; do
      case "${property}" in
        ActiveState) echo "ActiveState=$(_seq "${ACTIVE_SEQ}")" ;;
        SubState) echo "SubState=$(_seq "${SUB_SEQ}")" ;;
        LoadState) echo "LoadState=${LOAD_STATE}" ;;
        ConditionResult) echo "ConditionResult=${CONDITION_RESULT}" ;;
        ConditionTimestamp) echo "ConditionTimestamp=${CONDITION_TIMESTAMP}" ;;
        *) echo "${property}=" ;;
      esac
    done
    return 0
  fi
  echo "STUB systemctl ${verb}" >&2
  # A stop that fails leaves the unit in whatever state it was really in, and
  # the block re-reads it to decide how bad that is. Let a test say which.
  if [[ "${verb}" == stop && -n "${AFTER_STOP_ACTIVE:-}" ]]; then
    ACTIVE_SEQ="${AFTER_STOP_ACTIVE}"
    SUB_SEQ="${AFTER_STOP_SUB}"
  fi
  return "${STUB_RC:-0}"
}
"""


def _run_settle_block(
    active_states,
    sub_states,
    load_state="loaded",
    condition_result="yes",
    condition_timestamp="Sun 2026-08-09 12:00:00 EDT",
    show_empty=False,
    stub_rc=0,
    after_stop=None,
):
    """Run the homebrew settle block against a scripted unit state sequence.

    Each entry is the state reported after that many stubbed `sleep` calls,
    so `["activating", "active"]` is a run that is in flight on the first
    look and settled one poll later. `show_empty` makes every property read
    return nothing, the way an unreachable user manager would; `after_stop`
    is an `(ActiveState, SubState)` pair the unit moves to once `stop` has
    run, which is what the block re-reads when that stop fails. Returns the
    completed process; the block's diagnostics and every systemctl call land
    on stderr.
    """
    after_stop_active, after_stop_sub = after_stop or ("", "")
    script = "".join(
        [
            _SETTLE_STUBS,
            f'ACTIVE_SEQ="{" ".join(active_states)}"\n',
            f'SUB_SEQ="{" ".join(sub_states)}"\n',
            f'AFTER_STOP_ACTIVE="{after_stop_active}"\n',
            f'AFTER_STOP_SUB="{after_stop_sub}"\n',
            f'LOAD_STATE="{load_state}"\n',
            f'CONDITION_RESULT="{condition_result}"\n',
            f'CONDITION_TIMESTAMP="{condition_timestamp}"\n',
            f"SHOW_EMPTY={1 if show_empty else 0}\n",
            f"STUB_RC={stub_rc}\n",
            _brew_preinstall_settle_block(),
            '\necho "SLEEPS=${SLEEPS}" >&2\n',
        ]
    )
    return _bash(["bash"], script)


def _behave_summary_block():
    """The runner's scenario-tally python heredoc, ready to execute.

    Extracted from the parsed template, so this is the source that really
    runs on the cluster — not a copy that can drift.
    """
    return _heredoc("PY", _systemd_run_tests_template()["script"]["source"])


def _run_behave_summary(tmp_path, features, suite="homebrew", tags=""):
    """Execute the tally block over a synthetic behave JSON report.

    Only the hardcoded results path is rewritten, to a pytest tmp file; the
    counting, the printed summary and the empty-run guard are the shipped
    code. Returns the completed process.
    """
    results = tmp_path / "results.json"
    results.write_text(json.dumps(features), encoding="utf-8")
    block = _behave_summary_block().replace(
        '"/tmp/results/results.json"', repr(str(results))
    )
    assert str(results) in block, "results path substitution did not apply"

    if shutil.which("python3") is None:  # pragma: no cover - python3 runs this
        pytest.skip("python3 is required to check the runner's summary block")
    return subprocess.run(
        ["python3", "-c", block],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "SUITE": suite, "BEHAVE_TAGS": tags},
    )


def _feature(*statuses):
    return {"elements": [{"status": status} for status in statuses]}


def test_behave_summary_reports_the_tally_for_a_real_run():
    # Guard rail for the guard rail: the empty-run check below is only
    # meaningful if a populated report still counts and still passes.
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as workdir:
        result = _run_behave_summary(
            Path(workdir), [_feature("passed", "passed"), _feature("passed")]
        )

    assert result.returncode == 0, result.stderr
    assert "3/3 scenarios passed" in result.stdout


def test_behave_summary_counts_failures_without_failing_itself():
    # behave's own exit code owns failure reporting; this block only tallies,
    # so a failed scenario must not turn into a second, differently-worded
    # error from the summary.
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as workdir:
        result = _run_behave_summary(
            Path(workdir), [_feature("passed", "failed", "passed")]
        )

    assert result.returncode == 0, result.stderr
    assert "2/3 scenarios passed" in result.stdout


def test_behave_summary_refuses_a_zero_scenario_run():
    # behave exits 0 when --tags matches nothing, so a typo'd tag used to
    # print "0/0 scenarios passed" and report a green lane that validated
    # nothing. Targeted tag runs are the whole point of behave-tags, so this
    # is exactly when the false green would happen.
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as workdir:
        result = _run_behave_summary(
            Path(workdir), [], suite="homebrew", tags="--tags @chairlfit"
        )

    assert result.returncode != 0, result.stdout
    assert "no scenarios ran" in result.stderr
    # The diagnostic has to name the suite and the tags, or the operator is
    # left rerunning the lane to find out which typo caused it.
    assert "homebrew" in result.stderr
    assert "@chairlfit" in result.stderr


def test_behave_summary_refuses_a_report_whose_features_are_all_empty():
    # A tag can match a feature file but none of its scenarios, which yields
    # features with empty `elements` rather than an empty report.
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as workdir:
        result = _run_behave_summary(Path(workdir), [_feature(), _feature()])

    assert result.returncode != 0, result.stdout
    assert "no scenarios ran" in result.stderr


def test_runner_fails_the_lane_when_the_summary_block_refuses():
    # The block's exit code has to reach the runner's exit status, and it has
    # to rank below qecore's and behave's so a real failure still reports its
    # own cause rather than "no scenarios ran".
    runner = _systemd_run_tests_template()["script"]["source"]
    tail = runner[runner.index("BEHAVE_RC=$(cat /tmp/results/behave-rc.txt)"):]

    assert "SUMMARY_RC=0" in tail
    assert "<<'PY' || SUMMARY_RC=$?" in tail
    assert 'exit "${SUMMARY_RC}"' in tail
    assert tail.index('exit "${QECORE_RC}"') < tail.index('exit "${BEHAVE_RC}"')
    assert tail.index('exit "${BEHAVE_RC}"') < tail.index('exit "${SUMMARY_RC}"')


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

    assert template["activeDeadlineSeconds"] == 7200
    assert "sized for the slowest lane (suite=homebrew)" in _run_tests_budget_comment()


def test_native_systemd_runner_budgets_one_cask_install_and_bounds_the_wait():
    # The budget comment is the only record of *why* the deadline is what it
    # is, so check the arithmetic rather than the prose: pull the phase and
    # headroom seconds straight out of the comment, add the in-flight wait cap
    # read from run-behave.sh, and require the totals to fit. Asserting on
    # sentences instead would only pin the line wrapping, and hard-coding the
    # sum on both sides of an `==` would assert nothing at all.
    comment = _run_tests_budget_comment()
    behave = _systemd_runner_blocks()["RUN_BEHAVE"]
    deadline = _systemd_run_tests_template()["activeDeadlineSeconds"]

    phases = _budget_entries(comment, "phase")
    headroom = _budget_entries(comment, "headroom")
    phase_total = sum(seconds for seconds, _ in phases)
    headroom_total = sum(seconds for seconds, _ in headroom)
    wait_cap = int(
        re.search(r"^\s*BREW_PREINSTALL_WAIT_SECONDS=(\d+)$", behave, re.M).group(1)
    )
    poll = int(
        re.search(r"^\s*BREW_PREINSTALL_POLL_SECONDS=(\d+)$", behave, re.M).group(1)
    )

    assert len(phases) >= 5, f"the budget lists too few phases: {phases}"
    assert headroom, "the budget names no cost it expects the headroom to absorb"

    # The lane's own expected cost, then the worst case the reviewer has to
    # believe: every headroom item paid on the same run.
    assert phase_total + wait_cap <= deadline
    assert phase_total + headroom_total <= deadline

    # An in-flight run is waited out instead of killed, so the wait cap spends
    # the same seconds the budget already allocates to that one install. A
    # larger cap would let the wait alone outgrow its own budget line; a
    # smaller one would abandon a run the budget says it can afford.
    install = [seconds for seconds, text in phases if "cask install" in text]
    assert install == [wait_cap], (
        f"the in-flight wait cap ({wait_cap}s) must equal the single"
        f" cask-install phase, found {install}"
    )
    assert wait_cap % poll == 0

    # The comment states the minutes it derives from those numbers; recompute
    # them so a phase edit that leaves the prose behind fails here.
    assert f"~{round(phase_total / 60)}min" in comment
    assert f"~{round((deadline - phase_total) / 60)}min of headroom" in comment
    assert (
        f"~{round((deadline - phase_total - headroom_total) / 60)}min inside the"
        " deadline" in comment
    )

    # The restart attempts the session can burn before run-behave.sh ever
    # samples the unit are real wall clock that no phase line covers; the
    # budget has to say the headroom absorbs them, and say what bounds them.
    assert any("Restart=on-failure" in text for _, text in headroom)
    assert "StartLimitBurst=3 within StartLimitIntervalSec=600" in comment

    # The claim the single install phase rests on.
    assert "content-addressed" in comment
    assert f"BREW_PREINSTALL_WAIT_SECONDS (the same {wait_cap}s)" in comment


def test_native_systemd_runner_forces_headless_gnome_shell_before_qecore():
    # Live evidence, workflow chairlift-diagnose-smoke-mhkxg: qecore-headless
    # stops and restarts GDM, org.gnome.Shell@user.service then times out with
    # "Failed to make thread 'KMS thread' high priority scheduled: Timeout was
    # reached", gnome-shell aborts, GDM answers "Session never registered,
    # failing", and the bluefin-test session bus disappears leaving only
    # gdm-greeter. That is mutter's *native* backend contending for exclusive
    # DRM master on ghost's single GPU — the class run-container-tests already
    # closed with a user-unit drop-in. The native-systemd runner shares the
    # host GPU and the qecore GDM restart, so it needs the same drop-in, in
    # place before qecore ever touches GDM.
    blocks = _systemd_runner_blocks()
    setup = blocks["TARGET_SETUP"]

    drop_in = "\n".join(
        [
            "mkdir -p /etc/systemd/user/org.gnome.Shell@.service.d",
            'printf "%s\\n" \\',
            '  "[Service]" \\',
            '  "ExecStart=" \\',
            '  "ExecStart=/usr/bin/gnome-shell --mode=%i --unsafe-mode'
            ' --headless --virtual-monitor 1920x1080" \\',
            "  >/etc/systemd/user/org.gnome.Shell@.service.d/10-headless.conf",
        ]
    )
    assert drop_in in setup, (
        "TARGET_SETUP must install the headless org.gnome.Shell@.service"
        " drop-in verbatim"
    )

    # The GPU is a host resource, not a per-suite one, so this cannot sit
    # behind a suite guard: every native-systemd desktop suite starts a shell.
    guard = 'if [[ "${SUITE}" == "homebrew" ]]; then'
    assert setup.index(drop_in) < setup.index(guard)

    # It must land before anything can start a user manager that would load
    # the unit without it, and before qecore restarts GDM.
    assert setup.index(drop_in) < setup.index("systemctl start user@1000.service")
    invocation = re.search(r"^\s*qecore-headless \\$", blocks["runner"], re.M)
    assert invocation is not None, "the runner never invokes qecore-headless"
    assert blocks["runner"].index(drop_in) < invocation.start()

    # The `ExecStart=` reset overrides the unit line qecore-headless rewrites
    # to append `--unsafe-mode`, so the flag must be repeated in the drop-in or
    # every Shell.Eval call (wait_for_shell.py, dogtail) returns `(false, '')`.
    assert "--unsafe-mode --headless --virtual-monitor 1920x1080" in setup


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

    sample = behave.index("brew_preinstall_sample() {")
    first_read = behave.index("\n  brew_preinstall_sample\n")
    wait = behave.index("waiting up to ${BREW_PREINSTALL_WAIT_SECONDS}s")
    diagnose = behave.index("before behave started it")
    stop = behave.index("systemctl --user stop brew-preinstall.service")
    reset = behave.index("systemctl --user reset-failed brew-preinstall.service")
    run = behave.index("python3 -m behave")

    assert sample < first_read < wait < diagnose < stop < reset < run
    assert re.search(
        r'if \[\[ "\$\{BREW_PREINSTALL_STATE\}" == "activating" \\\n'
        r'\s*&& "\$\{BREW_PREINSTALL_SUBSTATE\}" != auto-restart\* \]\]; then',
        behave,
    )
    assert re.search(
        r'if \[\[ "\$\{BREW_PREINSTALL_STATE\}" == "inactive" \\\n'
        r'\s*\|\| "\$\{BREW_PREINSTALL_STATE\}" == "active" \]\]; then',
        behave,
    )
    # `auto-restart-queued` is the systemd >= 254 spelling; the prefix glob
    # must match both, and neither may be waited out.
    assert 'BREW_PREINSTALL_SUBSTATE}" != auto-restart*' in behave
    assert 'BREW_PREINSTALL_SUBSTATE}" == auto-restart*' in behave
    assert "systemctl --user is-failed" not in behave

    # One `systemctl show` per sample, setting both variables together: two
    # reads can straddle a transition and pair states the unit never held.
    # `--value` is unusable for that, since systemctl orders the output itself.
    assert behave.count("brew_preinstall_sample() {") == 1
    assert (
        "--property=ActiveState --property=SubState 2>/dev/null || true" in behave
    )
    assert "brew_preinstall_property" not in behave
    assert "--property=ActiveState --value" not in behave
    assert 'BREW_PREINSTALL_STATE="${value:-unknown}"' in behave
    assert 'BREW_PREINSTALL_SUBSTATE="${value:-unknown}"' in behave
    # The parse loop must stay in the calling shell. Piping `systemctl show`
    # into `while read` puts it in a subshell, where both assignments are
    # discarded and every branch below silently sees `unknown`.
    assert behave.count('done <<< "${sample}"') == 2
    assert "systemctl --user show" in behave
    assert "show brew-preinstall.service | while" not in behave

    # inactive/active are settled, but they are also what a unit that systemd
    # never ran looks like. ConditionResult=no alone cannot say which: only an
    # empty ConditionTimestamp distinguishes "never evaluated" from
    # "evaluated and failed", so both are read and a verdict is printed.
    assert "brew_preinstall_condition_report() {" in behave
    assert re.search(
        r"--property=LoadState --property=ConditionResult \\\n"
        r"\s*--property=ConditionTimestamp 2>/dev/null \|\| true",
        behave,
    )
    assert 'if [[ -z "${stamp}" ]]; then' in behave
    assert "conditions never evaluated" in behave
    assert "conditions evaluated and failed" in behave
    assert "conditions evaluated and passed" in behave
    assert "$(brew_preinstall_condition_report)" in behave

    # A wait that can last a quarter of an hour has to keep saying so, or the
    # lane is indistinguishable from a runner that stopped producing output.
    assert "BREW_PREINSTALL_PROGRESS_SECONDS=60" in behave
    assert (
        "is still activating (${BREW_PREINSTALL_SUBSTATE}) after"
        " ${BREW_PREINSTALL_WAITED}s of ${BREW_PREINSTALL_WAIT_SECONDS}s" in behave
    )

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
    assert "the suite may report a stale start limit instead of the real error" in behave

    # A failed stop is not one hazard but three, and the pre-stop sample cannot
    # say which: re-read, then name the one that actually applies. A live
    # install left running is the serious one — the suite's start would then
    # run brew concurrently against the same prefix.
    failed_stop = behave.split(
        'if [[ "${BREW_PREINSTALL_STOP_RC}" -ne 0 ]]; then', 1
    )[1]
    assert failed_stop.index("brew_preinstall_sample") < failed_stop.index(
        "warning: stopping brew-preinstall.service"
    )
    assert "BREW_PREINSTALL_STOPPED_FROM=" in failed_stop
    assert (
        "(state was ${BREW_PREINSTALL_STOPPED_FROM}, now"
        " ${BREW_PREINSTALL_STATE}/${BREW_PREINSTALL_SUBSTATE})" in failed_stop
    )
    assert (
        "warning: a live brew-preinstall.service install is still running; the"
        " suite's explicit start will run brew concurrently against the same"
        " Homebrew prefix" in failed_stop
    )
    assert (
        "warning: brew-preinstall.service still holds a queued auto-restart"
        " that may race the suite's explicit start" in failed_stop
    )
    assert (
        "warning: brew-preinstall.service is still active, so RemainAfterExit"
        " keeps it latched and the suite's explicit start may be a no-op"
        in failed_stop
    )

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
        previous.endswith('env XDG_RUNTIME_DIR="${RUNTIME_DIR}" \\')
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


def test_brew_preinstall_settle_reports_the_condition_verdict_for_a_settled_unit():
    # `inactive` is indistinguishable from "unit not installed" or "systemd
    # skipped it on ConditionUser/ConditionPathExists" — and a start of either
    # exits 0, so the suite would pass on nothing. ConditionResult alone cannot
    # separate them either: systemd reports `no` both for a unit whose
    # conditions were evaluated and failed *and* for one it has never tried to
    # start (including not-found), and only the empty ConditionTimestamp marks
    # the second. Read both and state which case it is.
    missing = _run_settle_block(
        active_states=["inactive"],
        sub_states=["dead"],
        load_state="not-found",
        condition_result="no",
        condition_timestamp="",
    )
    skipped = _run_settle_block(
        active_states=["inactive"],
        sub_states=["dead"],
        condition_result="no",
        condition_timestamp="Sun 2026-08-09 12:00:00 EDT",
    )
    clean = _run_settle_block(active_states=["inactive"], sub_states=["dead"])

    assert missing.returncode == 0, missing.stderr
    assert (
        "is inactive (dead, LoadState=not-found, ConditionResult=no,"
        " ConditionTimestamp=<empty>, conditions never evaluated, so systemd"
        " has not tried to start this unit) before behave started it"
        in missing.stderr
    )
    assert "STUB systemctl status" not in missing.stderr  # settled: no dump

    assert skipped.returncode == 0, skipped.stderr
    assert (
        "is inactive (dead, LoadState=loaded, ConditionResult=no,"
        " ConditionTimestamp=Sun 2026-08-09 12:00:00 EDT, conditions evaluated"
        " and failed, so the start was skipped) before behave started it"
        in skipped.stderr
    )

    assert clean.returncode == 0, clean.stderr
    assert "conditions evaluated and passed) before behave started it" in clean.stderr
    # The same ConditionResult=no is reported for both of the first two, so the
    # verdict — not the raw property — is what tells them apart.
    assert "ConditionResult=no" in missing.stderr
    assert "ConditionResult=no" in skipped.stderr
    assert "conditions never evaluated" not in skipped.stderr


def test_brew_preinstall_settle_samples_active_state_and_sub_state_atomically():
    # Two `systemctl show` calls can straddle a transition and hand the block
    # an ActiveState/SubState pair the unit never actually held — for example
    # `activating` from before a failure with `failed` from after it, which
    # would send the lane down the wrong branch. One call, both properties.
    result = _run_settle_block(
        active_states=["activating", "active"], sub_states=["start", "exited"]
    )

    assert result.returncode == 0, result.stderr
    shows = re.findall(r"^STUB show: (.*)$", result.stderr, re.M)
    state_reads = [
        request
        for request in shows
        if "ActiveState" in request or "SubState" in request
    ]

    assert state_reads, result.stderr
    assert all(request == "ActiveState SubState" for request in state_reads)
    # The condition properties are their own single read, and never mixed into
    # the state sample: `--value` would order a mixed read by systemd's rules,
    # not the requested ones.
    assert "LoadState ConditionResult ConditionTimestamp" in shows


def test_brew_preinstall_settle_reports_progress_while_it_waits():
    # A wedged install can hold the lane here for 15 minutes. Silence for that
    # long is indistinguishable from a runner that died, so the wait reports
    # every BREW_PREINSTALL_PROGRESS_SECONDS against a known state.
    wedged = _run_settle_block(active_states=["activating"], sub_states=["start"])
    brief = _run_settle_block(
        active_states=["activating", "activating", "active"],
        sub_states=["start", "start", "exited"],
    )

    assert wedged.returncode == 0, wedged.stderr
    progress = re.findall(
        r"is still activating \(start\) after (\d+)s of 900s", wedged.stderr
    )

    assert [int(seconds) for seconds in progress] == list(range(60, 901, 60))

    # A run that settles inside the first interval says nothing extra.
    assert brief.returncode == 0, brief.stderr
    assert "is still activating" not in brief.stderr


def test_brew_preinstall_settle_degrades_when_property_reads_are_empty():
    # An unreachable user manager, or a property systemctl cannot answer,
    # prints nothing at all. That must not read as a settled state: `unknown`
    # falls through to the diagnostic branch and is still stopped and reset,
    # so behave starts from a known place instead of the lane aborting under
    # `set -euo pipefail`.
    result = _run_settle_block(
        active_states=["activating"], sub_states=["start"], show_empty=True
    )

    assert result.returncode == 0, result.stderr
    stderr = result.stderr
    assert "SLEEPS=0" in stderr  # unknown is not activating: nothing to wait for
    assert "was unknown (unknown) before behave started it" in stderr
    assert "STUB systemctl status" in stderr
    assert stderr.index("STUB systemctl stop") < stderr.index(
        "STUB systemctl reset-failed"
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
    assert "(state was failed/failed, now failed/failed)" in stderr
    assert "warning: reset-failed brew-preinstall.service exited 1" in stderr
    assert "(last observed state failed/failed)" in stderr
    assert "SLEEPS=0" in stderr
    # Nothing survived the stop here, so none of the three specific hazards
    # may be claimed.
    assert "a live brew-preinstall.service install is still running" not in stderr
    assert "still holds a queued auto-restart" not in stderr
    assert "RemainAfterExit keeps it latched" not in stderr


def test_brew_preinstall_settle_names_the_hazard_a_failed_stop_left_behind():
    # A failed stop is three different problems and the pre-stop sample cannot
    # say which, so the block re-reads the unit. A live install is the serious
    # one: the suite's start would run a second brew against the same prefix.
    live = _run_settle_block(
        active_states=["failed"],
        sub_states=["failed"],
        stub_rc=1,
        after_stop=("activating", "start"),
    )
    queued = _run_settle_block(
        active_states=["failed"],
        sub_states=["failed"],
        stub_rc=1,
        after_stop=("activating", "auto-restart-queued"),
    )
    latched = _run_settle_block(
        active_states=["failed"],
        sub_states=["failed"],
        stub_rc=1,
        after_stop=("active", "exited"),
    )

    assert live.returncode == 0, live.stderr
    assert "(state was failed/failed, now activating/start)" in live.stderr
    assert (
        "warning: a live brew-preinstall.service install is still running; the"
        " suite's explicit start will run brew concurrently against the same"
        " Homebrew prefix" in live.stderr
    )
    assert "still holds a queued auto-restart" not in live.stderr

    assert queued.returncode == 0, queued.stderr
    assert "(state was failed/failed, now activating/auto-restart-queued)" in queued.stderr
    assert (
        "warning: brew-preinstall.service still holds a queued auto-restart"
        " that may race the suite's explicit start" in queued.stderr
    )
    assert "a live brew-preinstall.service install is still running" not in queued.stderr

    assert latched.returncode == 0, latched.stderr
    assert "(state was failed/failed, now active/exited)" in latched.stderr
    assert (
        "warning: brew-preinstall.service is still active, so RemainAfterExit"
        " keeps it latched and the suite's explicit start may be a no-op"
        in latched.stderr
    )
    assert "a live brew-preinstall.service install is still running" not in latched.stderr


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
