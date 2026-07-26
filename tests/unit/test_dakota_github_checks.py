from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_github_check_reporter_exposes_detailed_safe_results():
    path = ROOT / "argo/workflow-templates/github-check-reporter.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = {template["name"]: template for template in document["spec"]["templates"]}

    assert document["metadata"]["name"] == "github-check-reporter"
    assert {"send", "final", "collect"} <= templates.keys()

    send_source = templates["send"]["script"]["source"]
    collect_source = templates["collect"]["script"]["source"]

    assert 'event_type: "lab-check"' in send_source
    assert "Authorization: Bearer ${GITHUB_TOKEN}" in send_source
    assert "projectbluefin/bluefin|projectbluefin/bluefin-lts|projectbluefin/dakota" in send_source
    assert "### Pod placement" in collect_source
    assert "### Workflow nodes" in collect_source
    assert "## Failure diagnostics" in collect_source
    assert "containerStatuses[]?.restartCount" in collect_source
    assert "kubectl logs" not in collect_source
    assert "raw pod logs" in collect_source


def test_dakota_pr_workflow_updates_one_check_without_comments():
    poller = (
        ROOT / "argo/workflow-templates/pr-poller.yaml"
    ).read_text(encoding="utf-8")
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    assert "dispatch_lab_check()" in poller
    assert "onExit: report-final" in poller
    assert "name: report-start" in poller
    assert "name: github-check-reporter" in poller
    assert "name: qa-bluefin" in poller
    assert "projectbluefin/bluefin-lts" in poller
    assert "value: in_progress" in poller
    assert "template: final" in poller
    assert "failed to create queued GitHub check" in poller
    assert "kubectl delete workflow" in poller
    dispatch_failure = poller.index("failed to create queued GitHub check")
    assert "return 0" in poller[dispatch_failure : dispatch_failure + 300]
    assert "gh pr comment" not in poller
    assert "/issues/comments" not in poller

    assert "lab-check-status repo pr_number:" in justfile
    assert 'select(.name == "testing-lab / {{ repo }}"' in justfile
    assert "lab-report pr_number" not in justfile
