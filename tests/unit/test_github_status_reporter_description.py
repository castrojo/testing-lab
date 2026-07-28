"""Regression tests for the ghost-lab commit-status description length.

GitHub rejects commit statuses whose description exceeds 140 characters with
HTTP 422 ("Validation failed: Description is too long"). The reporter used to
truncate only the summary and then prepend the title, so the composed
description could still overflow.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
REPORTER = ROOT / "argo/workflow-templates/github-status-reporter.yaml"
MAX_DESCRIPTION = 140

CURL_STUB = """#!/usr/bin/env bash
printf '%s' "${@: -1}" > "${PAYLOAD_OUT}"
printf '201'
"""


def _send_script_source():
    template = yaml.safe_load(REPORTER.read_text(encoding="utf-8"))
    for entry in template["spec"]["templates"]:
        if entry["name"] == "send":
            return entry["script"]["source"]
    raise AssertionError("github-status-reporter has no 'send' template")


def _run_reporter(tmp_path, title, summary):
    """Execute the real send script with a stubbed curl and return the payload."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(CURL_STUB, encoding="utf-8")
    curl.chmod(0o755)

    payload_out = tmp_path / "payload.json"
    script = tmp_path / "send.sh"
    script.write_text(_send_script_source(), encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "PAYLOAD_OUT": str(payload_out),
            "GITHUB_TOKEN": "unused-in-test",
            "REPOSITORY": "projectbluefin/testsuite",
            "SHA": "0" * 40,
            "PR_NUMBER": "660",
            "CHECK_STATE": "completed",
            "CONCLUSION": "failure",
            "WORKFLOW_NAME": "testsuite-660-docs-ci-zsn6n",
            "WORKFLOW_URL": "https://argo.example.com/workflows/argo/testsuite-660",
            "CHECK_TITLE": title,
            "CHECK_SUMMARY": summary,
        }
    )

    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    raw = payload_out.read_bytes()
    raw.decode("utf-8")  # a mid-multibyte clip would raise here
    return json.loads(raw)


needs_tools = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="requires bash and jq",
)


@needs_tools
@pytest.mark.parametrize(
    "title,summary",
    [
        ("Testsuite lab validation failed", "PR #660 finished as Failed. " + "x" * 400),
        ("T" * 400, "PR #660 finished as Failed."),
        ("T" * 400, "S" * 400),
        ("é" * 400, "ü" * 400),
        ("Testsuite lab validation passed", ""),
        ("", "PR #660 finished as Succeeded."),
    ],
)
def test_composed_description_never_exceeds_github_limit(tmp_path, title, summary):
    payload = _run_reporter(tmp_path, title, summary)
    description = payload["description"]

    assert description, "description must never be empty"
    assert len(description) <= MAX_DESCRIPTION, description
    assert payload["state"] in {"error", "failure", "pending", "success"}
    assert payload["context"] == "ghost-lab"


@needs_tools
def test_truncated_description_is_marked_with_an_ellipsis(tmp_path):
    payload = _run_reporter(tmp_path, "Testsuite lab validation failed", "s" * 400)

    assert payload["description"].startswith("Testsuite lab validation failed: ")
    assert payload["description"].endswith("...")
    assert len(payload["description"]) == MAX_DESCRIPTION


@needs_tools
def test_short_description_is_passed_through_unchanged(tmp_path):
    payload = _run_reporter(tmp_path, "Testsuite lab validation passed", "PR #660 ok.")

    assert payload["description"] == "Testsuite lab validation passed: PR #660 ok."


def test_reporter_does_not_truncate_the_summary_before_prepending_the_title():
    source = _send_script_source()

    assert "${CHECK_SUMMARY:0:140}" not in source
    assert '--arg description "${DESCRIPTION}"' in source
    assert "140" in source
