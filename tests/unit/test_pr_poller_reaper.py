"""End-to-end behaviour tests for the pr-poller stale-workflow reaper.

The poller's dispatch loop is embedded bash inside a WorkflowTemplate, so these
tests extract the script and run it for real with stubbed `kubectl` and `curl`
on PATH. That exercises the actual shell — quoting, `set -euo pipefail`
interaction, associative-array scoping across process substitution — rather
than asserting on substrings.

Regression context: the poller deduped on `pr-number` + `pr-sha`, so a new push
created a new workflow while the superseded one ran to completion holding a
`ghost-container-qa` slot for ~20 minutes. Workflows for merged/closed PRs were
never cancelled at all.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
POLLER = ROOT / "argo/workflow-templates/pr-poller.yaml"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq is required to run the poller script"
)


def poller_script():
    doc = yaml.safe_load(POLLER.read_text(encoding="utf-8"))
    (template,) = [t for t in doc["spec"]["templates"] if t["name"] == "poll-labeled-prs"]
    return template["script"]["source"]


KUBECTL_STUB = '''#!/usr/bin/env python3
import json, os, sys

state = json.load(open(os.environ["FAKE_STATE"]))
log = open(os.environ["FAKE_LOG"], "a")
argv = sys.argv[1:]


def selector():
    for i, a in enumerate(argv):
        if a == "-l":
            return dict(
                kv.split("=", 1) for kv in argv[i + 1].split(",") if "=" in kv
            )
    return {}


if argv[0] == "get":
    sel = selector()
    items = [
        wf
        for wf in state["workflows"]
        if all(wf["metadata"]["labels"].get(k) == v for k, v in sel.items())
    ]
    if "-o" in argv and argv[argv.index("-o") + 1] == "json":
        print(json.dumps({"items": items}))
    else:
        for wf in items:
            print(wf["metadata"]["name"])
elif argv[0] == "patch":
    name = argv[2]
    log.write("patch %s\\n" % name)
    for wf in state["workflows"]:
        if wf["metadata"]["name"] == name:
            wf.setdefault("spec", {})["shutdown"] = "Stop"
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
elif argv[0] == "create":
    body = sys.stdin.read()
    # The dispatch manifests are unquoted heredocs inside a YAML block scalar
    # inside a WorkflowTemplate. Parsing what the shell actually rendered is the
    # only way to catch an indentation or quoting regression in that nesting.
    try:
        import yaml
        manifest = yaml.safe_load(body)
    except Exception as exc:
        sys.stderr.write("rendered manifest is not valid YAML: %s\\n%s\\n" % (exc, body))
        sys.exit(1)
    if not isinstance(manifest, dict) or manifest.get("kind") != "Workflow":
        sys.stderr.write("rendered manifest is not a Workflow:\\n%s\\n" % body)
        sys.exit(1)
    labels = manifest["metadata"].get("labels", {})
    log.write("create %s %s %s\\n" % (
        labels.get("bluefin.io/repository", "-"),
        labels.get("bluefin.io/pr-number", "-"),
        labels.get("bluefin.io/pr-sha", "-"),
    ))
    print("created-workflow")
elif argv[0] == "delete":
    log.write("delete %s\\n" % " ".join(argv[1:]))
log.close()
'''

CURL_STUB = '''#!/usr/bin/env python3
import json, os, sys, urllib.parse

state = json.load(open(os.environ["FAKE_STATE"]))
log = open(os.environ["FAKE_LOG"], "a")
url = [a for a in sys.argv[1:] if a.startswith("http")][-1]

if "/dispatches" in url:
    log.write("dispatch %s\\n" % url)
    sys.exit(0)

if "/search/issues" in url:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    q = query["q"][0]
    page = int(query.get("page", ["1"])[0])
    if "label:test-on-lab" in q:
        print(json.dumps({"total_count": 0, "items": []}))
        sys.exit(0)
    repo = q.split("repo:")[1].split()[0]
    if repo in state.get("search_fails", []):
        log.write("search-fail %s\\n" % repo)
        sys.exit(22)
    prs = [p for p in state["open_prs"] if p["base"]["repo"]["full_name"] == repo]
    items = [] if page > 1 else [
        {"number": p["number"], "pull_request": {"url": "https://api.github.com/pr/%s/%s" % (repo, p["number"])}}
        for p in prs
    ]
    print(json.dumps({"total_count": len(prs), "items": items}))
    sys.exit(0)

if "/pr/" in url:
    _, repo_owner, repo_name, number = url.rsplit("/", 3)
    repo = "%s/%s" % (repo_owner.rsplit("/", 1)[-1], repo_name)
    for p in state["open_prs"]:
        if p["base"]["repo"]["full_name"] == repo and str(p["number"]) == number:
            print(json.dumps(p))
            sys.exit(0)
    sys.exit(22)

sys.exit(22)
'''


def make_pr(repo, number, sha, title="a change"):
    return {
        "number": number,
        "title": title,
        "base": {"repo": {"full_name": repo}},
        "head": {
            "sha": sha,
            "ref": "feature",
            "repo": {"clone_url": "https://github.com/%s" % repo},
        },
    }


def workflow(name, product, pr, sha12, completed=False, shutdown=None):
    wf = {
        "metadata": {
            "name": name,
            "labels": {
                "bluefin.io/trigger": "pr-auto",
                "bluefin.io/repository": product,
                "bluefin.io/pr-number": str(pr),
                "bluefin.io/pr-sha": sha12,
                "workflows.argoproj.io/completed": "true" if completed else "false",
            },
        },
        "spec": {},
    }
    if shutdown:
        wf["spec"]["shutdown"] = shutdown
    return wf


def run_poller(tmp_path, open_prs, workflows, search_fails=()):
    state = {
        "open_prs": open_prs,
        "workflows": workflows,
        "search_fails": list(search_fails),
    }
    state_file = tmp_path / "state.json"
    log_file = tmp_path / "calls.log"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    log_file.write_text("", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("kubectl", KUBECTL_STUB), ("curl", CURL_STUB)):
        path = bindir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    script = tmp_path / "poller.sh"
    script.write_text(poller_script(), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = "%s:%s" % (bindir, env["PATH"])
    env["GITHUB_TOKEN"] = "stub-token"
    env["REFRESH_EXISTING"] = "false"
    env["FAKE_STATE"] = str(state_file)
    env["FAKE_LOG"] = str(log_file)

    proc = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=120
    )
    lines = log_file.read_text().splitlines()
    stopped = {line.split()[1] for line in lines if line.startswith("patch ")}
    created = [line.split()[1:] for line in lines if line.startswith("create ")]
    return proc, stopped, created


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("poller")


def test_superseded_sha_is_stopped_and_current_sha_survives(tmp_path):
    """A new push cancels the old run instead of letting it burn a slot."""
    prs = [make_pr("projectbluefin/testsuite", 700, "b" * 40)]
    workflows = [
        workflow("testsuite-700-old", "testsuite", 700, "a" * 12),
        workflow("testsuite-700-new", "testsuite", 700, "b" * 12),
    ]
    proc, stopped, _created = run_poller(tmp_path, prs, workflows)
    assert proc.returncode == 0, proc.stderr
    assert "testsuite-700-old" in stopped
    assert "testsuite-700-new" not in stopped


def test_merged_or_closed_pr_workflows_are_reaped(tmp_path):
    """A workflow whose PR left the open set is cancelled."""
    prs = [make_pr("projectbluefin/testsuite", 700, "b" * 40)]
    workflows = [
        workflow("testsuite-700-current", "testsuite", 700, "b" * 12),
        workflow("testsuite-675-merged", "testsuite", 675, "c" * 12),
    ]
    proc, stopped, _created = run_poller(tmp_path, prs, workflows)
    assert proc.returncode == 0, proc.stderr
    assert stopped == {"testsuite-675-merged"}


def test_open_pr_in_another_repo_is_never_reaped_by_number_collision(tmp_path):
    """PR numbers collide across repos; reaping must key on the repository label."""
    prs = [
        make_pr("projectbluefin/testsuite", 727, "d" * 40),
        make_pr("projectbluefin/common", 958, "e" * 40),
    ]
    workflows = [
        workflow("testsuite-727-live", "testsuite", 727, "d" * 12),
        workflow("common-958-live", "common", 958, "e" * 12),
        # Same PR number as the live testsuite run, different repository.
        workflow("common-727-merged", "common", 727, "f" * 12),
    ]
    proc, stopped, _created = run_poller(tmp_path, prs, workflows)
    assert proc.returncode == 0, proc.stderr
    assert "testsuite-727-live" not in stopped
    assert "common-958-live" not in stopped
    assert "common-727-merged" in stopped


def test_a_failed_github_search_never_mass_reaps(tmp_path):
    """A transient API error must not be read as 'every PR was merged'."""
    prs = [make_pr("projectbluefin/testsuite", 727, "d" * 40)]
    workflows = [
        workflow("testsuite-727-live", "testsuite", 727, "d" * 12),
        workflow("testsuite-675-merged", "testsuite", 675, "c" * 12),
    ]
    proc, stopped, _created = run_poller(
        tmp_path, prs, workflows, search_fails=["projectbluefin/testsuite"]
    )
    assert proc.returncode == 0, proc.stderr
    assert stopped == set()
    assert "skipping reap this run" in proc.stderr


def test_zero_open_prs_is_treated_as_suspicious_not_as_mass_merge(tmp_path):
    """An empty open set (auth/scope regression) must not cancel live work."""
    workflows = [workflow("testsuite-727-live", "testsuite", 727, "d" * 12)]
    proc, stopped, _created = run_poller(tmp_path, [], workflows)
    assert proc.returncode == 0, proc.stderr
    assert stopped == set()


def test_reaping_is_idempotent(tmp_path):
    """Workflows already shutting down or completed are left alone."""
    prs = [make_pr("projectbluefin/testsuite", 727, "d" * 40)]
    workflows = [
        workflow("testsuite-727-live", "testsuite", 727, "d" * 12),
        workflow("testsuite-675-stopping", "testsuite", 675, "c" * 12, shutdown="Stop"),
        workflow("testsuite-691-done", "testsuite", 691, "g" * 12, completed=True),
    ]
    proc, stopped, _created = run_poller(tmp_path, prs, workflows)
    assert proc.returncode == 0, proc.stderr
    assert stopped == set()


def test_unlabelled_workflows_are_ignored(tmp_path):
    """Legacy workflows without bluefin.io/repository cannot be attributed."""
    prs = [make_pr("projectbluefin/testsuite", 727, "d" * 40)]
    legacy = workflow("legacy-675", "testsuite", 675, "c" * 12)
    del legacy["metadata"]["labels"]["bluefin.io/repository"]
    workflows = [workflow("testsuite-727-live", "testsuite", 727, "d" * 12), legacy]
    proc, stopped, _created = run_poller(tmp_path, prs, workflows)
    assert proc.returncode == 0, proc.stderr
    assert stopped == set()


def test_cancellation_is_graceful_so_report_final_still_runs():
    """`spec.shutdown: Stop` (not delete) keeps the onExit ghost-lab report."""
    source = poller_script()
    assert '{"spec":{"shutdown":"Stop"}}' in source
    assert "kubectl delete workflow -n argo -l \"bluefin.io/pr-number" in source, (
        "refresh mode may still hard-delete; the reaper must not"
    )


def test_dispatch_cap_is_aligned_with_the_execution_capacity():
    """MAX_DISPATCH is sized in workflows, not raw semaphore slots.

    Each QA pipeline template carries `parallelism` (added in #612 so that a
    fan-out cannot hold every slot, since `spec.parallelism` is not inherited
    through `templateRef`). One workflow therefore holds at most `parallelism`
    slots, and the runner executes `limit // parallelism` workflows at a time.
    Admitting more than that per poll only deepens the queue.
    """
    limit = int(
        yaml.safe_load(
            (ROOT / "manifests/workflow-semaphores.yaml").read_text(encoding="utf-8")
        )["data"]["ghost-container-qa"]
    )
    pipeline_doc = yaml.safe_load(
        (ROOT / "argo/workflow-templates/bluefin-qa-pipeline.yaml").read_text(
            encoding="utf-8"
        )
    )
    (pipeline,) = [
        t for t in pipeline_doc["spec"]["templates"] if t["name"] == "pipeline"
    ]
    parallelism = pipeline["parallelism"]
    source = poller_script()
    (line,) = [
        line for line in source.splitlines() if line.strip().startswith("MAX_DISPATCH=")
    ]
    assert int(line.split("=")[1]) <= limit // parallelism, textwrap.dedent(
        """
        Admitting more workflows per 5-minute poll than the runner can execute
        concurrently (ghost-container-qa limit // pipeline parallelism) only
        deepens the queue.
        """
    )


def test_both_dispatch_paths_render_valid_labelled_workflow_manifests(tmp_path):
    """The nested `kubectl create -f - <<EOF` heredocs must render valid YAML.

    Those manifests are an unquoted heredoc inside a YAML block scalar inside a
    WorkflowTemplate, which is exactly the nesting that silently mangles
    quoting. The stubbed `kubectl create` parses whatever the shell rendered and
    fails the run if it is not a valid `Workflow`.

    `testsuite` takes the inline `pr-pipeline` path; `knuckle` takes the
    `workflowTemplateRef` fallback path. Both must carry the repository label,
    without which the reaper cannot attribute a workflow to a repo.
    """
    prs = [
        make_pr("projectbluefin/testsuite", 800, "1" * 40),
        make_pr("projectbluefin/knuckle", 42, "2" * 40),
    ]
    proc, stopped, created = run_poller(tmp_path, prs, [])
    assert proc.returncode == 0, proc.stderr
    assert stopped == set()
    assert ["testsuite", "800", "1" * 12] in created
    assert ["knuckle", "42", "2" * 12] in created
