"""Contracts for the PR poller's GitHub API cadence and request shape."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CRON = ROOT / "manifests/pr-label-poller.yaml"
POLLER = ROOT / "argo/workflow-templates/pr-poller.yaml"


def poller_script():
    doc = yaml.safe_load(POLLER.read_text(encoding="utf-8"))
    (template,) = [
        template
        for template in doc["spec"]["templates"]
        if template["name"] == "poll-labeled-prs"
    ]
    return template["script"]["source"]


def test_pr_poller_uses_conservative_quarter_hour_cadence():
    cron = yaml.safe_load(CRON.read_text(encoding="utf-8"))

    assert cron["spec"]["schedules"] == ["*/15 * * * *"]
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"


def test_auto_repo_scan_uses_full_pull_pages_without_detail_fetches():
    source = poller_script()
    pass_one = source.split("# --- Pass 1:", 1)[1].split("# --- Pass 2:", 1)[0]

    assert (
        "${API}/repos/${REPO}/pulls?state=open&per_page=100&page=${PAGE}"
        in pass_one
    )
    assert pass_one.count("curl -sf") == 1
    assert "/search/issues" not in pass_one
    assert "PR_URL=" not in pass_one
    assert '[[ "$ITEMS" -lt 100 ]] && break' in pass_one
    assert "SCAN_OK=0" in pass_one
    assert "skipping reap this run" in pass_one


def test_label_catch_all_discovers_repositories_then_filters_pull_objects():
    source = poller_script()
    pass_two = source.split("# --- Pass 2:", 1)[1].split("# --- Pass 3:", 1)[0]

    assert (
        "${API}/search/issues?q=${LABEL_QUERY}&per_page=100&page=1" in pass_two
    )
    assert ".repository_url" in pass_two
    assert "${API}/repos/${REPO}/pulls?state=open&per_page=100&page=${PAGE}" in pass_two
    assert "test-on-lab" in pass_two
    assert "PR_URL=" not in pass_two
