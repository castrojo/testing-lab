#!/usr/bin/env python3
import sys
import os
import json
import re
import subprocess
import shutil
import urllib.request
from pathlib import Path
import argparse
from datetime import datetime, timezone

from evaluate_kde_soak import evaluate_kde_soak, is_trusted_github_issue_url


VALID_FAILURE_CLASSES = {"test", "infra"}

def ghcr_digest(repository, tag):
    """Resolve a public GHCR tag to its manifest digest anonymously."""
    try:
        tok_req = urllib.request.Request(
            f"https://ghcr.io/token?scope=repository:{repository}:pull"
        )
        with urllib.request.urlopen(tok_req, timeout=15) as r:
            token = json.load(r)["token"]
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{repository}/manifests/{tag}",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ", ".join(
                    [
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                    ]
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.headers.get("Docker-Content-Digest")
    except Exception:
        return None

def resolve_digest_for_slug(img_slug):
    mapping = {
        "bluefin-testing": ("projectbluefin/bluefin", "testing"),
        "bluefin-stable": ("projectbluefin/bluefin", "stable"),
        "bluefin-lts-testing": ("projectbluefin/bluefin-lts", "testing"),
        "bluefin-lts-stable": ("projectbluefin/bluefin-lts", "stable"),
        "dakota-testing": ("projectbluefin/dakota", "testing"),
        "dakota-stable": ("projectbluefin/dakota", "stable"),
    }
    if img_slug in mapping:
        repo, tag = mapping[img_slug]
        return ghcr_digest(repo, tag)
    return None

def run_cmd(cmd, cwd=None, env=None, check=True):
    safe_cmd = [
        re.sub(r"(https://x-access-token:)[^@]+@", r"\1***@", arg)
        for arg in cmd
    ]
    print(f"Running command: {' '.join(safe_cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"Command failed with exit code {result.returncode}")
        print(f"STDOUT:\n{result.stdout}")
        print(f"STDERR:\n{result.stderr}")
        sys.exit(result.returncode)
    return result

def parse_results_and_build_update(
    data,
    existing_data,
    current_utc,
    workflow_name,
    img_slug,
    suite,
    digest=None,
    failure_class="test",
    failure_issue_url=None,
):
    if failure_class not in VALID_FAILURE_CLASSES:
        raise ValueError(f"failure_class must be one of {sorted(VALID_FAILURE_CLASSES)}")

    failed_scenarios = []
    failed_scenarios_detailed = []
    scenarios_total = 0
    scenarios_failed = 0
    total_duration = 0.0

    for feature in data:
        for element in feature.get('elements', []):
            if element.get('type') == 'scenario':
                scenarios_total += 1
                
                # Sum up the step durations for this scenario
                scenario_duration = 0.0
                failing_step_name = ""
                failing_step_error = ""
                
                for step in element.get('steps', []):
                    step_result = step.get('result', {})
                    step_duration = step_result.get('duration', 0.0)
                    scenario_duration += step_duration
                    
                    if step_result.get('status') == 'failed':
                        failing_step_name = step.get('name', 'Unnamed Step')
                        raw_error = step_result.get('error_message', '')
                        if isinstance(raw_error, list):
                            failing_step_error = '\n'.join(raw_error).strip()
                        else:
                            failing_step_error = str(raw_error).strip()
                
                total_duration += scenario_duration
                
                if element.get('status') == 'failed':
                    scenarios_failed += 1
                    scenario_name = element.get('name', 'Unnamed Scenario')
                    failed_scenarios.append(scenario_name)
                    
                    if not failing_step_error:
                        failing_step_error = "No stack trace recorded."
                    
                    failed_scenarios_detailed.append({
                        "scenario_name": scenario_name,
                        "duration_seconds": round(scenario_duration, 2),
                        "failing_step": failing_step_name or "Unknown Step",
                        "error_message": failing_step_error
                    })

    status = "passed" if scenarios_failed == 0 else "failed"
    if status == "passed":
        failure_class = "none"
        failure_issue_url = None
    elif failure_class == "infra" and not is_trusted_github_issue_url(failure_issue_url):
        raise ValueError("failure_issue_url must be a trusted GitHub issue URL for an infra failure")

    history = []
    if existing_data:
        history = existing_data.get("history", [])

    # Add the current run to history
    new_history_entry = {
        "run_date": current_utc,
        "workflow_name": workflow_name,
        "status": status,
        "scenarios": scenarios_total,
        "failed": scenarios_failed,
        "duration_seconds": round(total_duration, 2)
    }
    if digest:
        new_history_entry["digest"] = digest
        
    history.insert(0, new_history_entry)
    # Keep the complete soak window; the evaluator uses the newest 30 entries.
    history = history[:30]

    # Ensure all pre-existing entries in history also have a "duration_seconds" key (default to 0.0 if not present)
    for entry in history:
        if "duration_seconds" not in entry:
            entry["duration_seconds"] = 0.0
        if entry.get("status") == "passed":
            entry.setdefault("failure_class", "none")
        else:
            entry.setdefault("failure_class", "test")
        entry.setdefault("failure_issue_url", None)

    new_history_entry["failure_class"] = failure_class
    new_history_entry["failure_issue_url"] = failure_issue_url

    # Construct updated structure
    screenshot_url = f"https://projectbluefin.github.io/lab/screenshots/{img_slug}-{suite}-latest.png"
    updated_data = {
        "variant": f"{img_slug}",
        "suite": suite,
        "last_run": current_utc,
        "workflow_name": workflow_name,
        "status": status,
        "scenarios": scenarios_total,
        "failed": scenarios_failed,
        "failed_scenarios": failed_scenarios,
        "failed_scenarios_detailed": failed_scenarios_detailed,
        "duration_seconds": round(total_duration, 2),
        "screenshot_url": screenshot_url,
        "history": history
    }
    updated_data["soak"] = evaluate_kde_soak(history)
    if digest:
        updated_data["digest"] = digest
    return updated_data

def _load_json(path, description):
    try:
        with open(path, "r", encoding="utf-8") as result_file:
            return json.load(result_file)
    except Exception as exc:
        print(f"ERROR: Failed to parse {description} {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _resolve_digest(img_slug, digest):
    if digest:
        return digest
    print(f"No digest provided. Attempting anonymous resolution for slug {img_slug}...")
    resolved = resolve_digest_for_slug(img_slug)
    if resolved:
        print(f"Successfully resolved digest: {resolved}")
    else:
        print("Digest resolution skipped or failed.")
    return resolved


def _clone_repo(github_token, repo_dir):
    repo_dir = os.path.abspath(repo_dir)
    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)
    repo_url = f"https://x-access-token:{github_token}@github.com/projectbluefin/lab.git"
    run_cmd(["git", "clone", "--depth", "1", repo_url, repo_dir])
    return repo_dir


def _write_updated_result(
    results_json_path,
    repo_dir,
    img_slug,
    suite,
    workflow_name,
    digest,
    failure_class="test",
    failure_issue_url=None,
):
    data = _load_json(results_json_path, "behave results")
    results_dir = os.path.join(repo_dir, "docs", "results")
    os.makedirs(results_dir, exist_ok=True)
    result_filename = f"{img_slug}-{suite}.json"
    result_filepath = os.path.join(results_dir, result_filename)

    existing_data = None
    if os.path.exists(result_filepath):
        existing_data = _load_json(result_filepath, "existing result")

    updated_data = parse_results_and_build_update(
        data=data,
        existing_data=existing_data,
        current_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        workflow_name=workflow_name,
        img_slug=img_slug,
        suite=suite,
        digest=digest,
        failure_class=failure_class,
        failure_issue_url=failure_issue_url,
    )
    with open(result_filepath, "w", encoding="utf-8") as result_file:
        json.dump(updated_data, result_file)
    return f"docs/results/{result_filename}"


def _commit_and_push(repo_dir, paths, workflow_name, description):
    run_cmd(["git", "config", "user.name", "github-actions[bot]"], cwd=repo_dir)
    run_cmd(
        [
            "git",
            "config",
            "user.email",
            "github-actions[bot]@users.noreply.github.com",
        ],
        cwd=repo_dir,
    )
    run_cmd(["git", "add", *paths], cwd=repo_dir)
    diff_check = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=repo_dir, check=False)
    if diff_check.returncode == 0:
        print("No changes to test results. Skipping push.")
        return False

    commit_msg = (
        f"chore: update test results for {description} ({workflow_name})\n\n"
        "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
    )
    run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_dir)
    run_cmd(["git", "push", "origin", "HEAD:main"], cwd=repo_dir)
    return True


def publish_batch_results(
    batch_dir,
    img_slug,
    workflow_name,
    github_token,
    digest=None,
    repo_dir=None,
):
    """Publish all suite JSON files from one QA workflow in one git transaction.

    Each suite writes ``<batch_dir>/<suite>/results.json``. The caller may
    supply an already-cloned repository so the workflow performs exactly one
    clone and this function performs exactly one push.
    """
    if not github_token:
        raise ValueError("github_token is empty")

    result_paths = sorted(Path(batch_dir).glob("*/results.json"))
    if not result_paths:
        raise FileNotFoundError(f"no suite results found below {batch_dir}")

    digest = _resolve_digest(img_slug, digest)
    temporary_repo = repo_dir is None
    if temporary_repo:
        repo_dir = os.path.join(os.getcwd(), ".lab-repo-clone")
        _clone_repo(github_token, repo_dir)

    try:
        published_paths = []
        suites = []
        for result_path in result_paths:
            suite = result_path.parent.name
            published_paths.append(
                _write_updated_result(
                    str(result_path),
                    repo_dir,
                    img_slug,
                    suite,
                    workflow_name,
                    digest,
                )
            )
            suites.append(suite)
        pushed = _commit_and_push(
            repo_dir,
            published_paths,
            workflow_name,
            f"{img_slug} suites {','.join(suites)}",
        )
        if pushed:
            print(
                "SUCCESS: Pushed aggregated test results for "
                f"{img_slug} ({', '.join(suites)}) back to repository!"
            )
        return pushed
    finally:
        if temporary_repo:
            shutil.rmtree(repo_dir, ignore_errors=True)


def _publish_single(
    results_json_path,
    img_slug,
    suite,
    workflow_name,
    github_token,
    digest=None,
    failure_class="test",
    failure_issue_url=None,
    repo_dir=None,
):
    if not github_token:
        print("ERROR: github_token is empty.", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(results_json_path):
        print(f"ERROR: {results_json_path} not found.", file=sys.stderr)
        sys.exit(2)

    digest = _resolve_digest(img_slug, digest)
    temporary_repo = repo_dir is None
    if temporary_repo:
        repo_dir = os.path.join(os.getcwd(), ".lab-repo-clone")
        _clone_repo(github_token, repo_dir)
    try:
        path = _write_updated_result(
            results_json_path,
            repo_dir,
            img_slug,
            suite,
            workflow_name,
            digest,
            failure_class,
            failure_issue_url,
        )
        pushed = _commit_and_push(repo_dir, [path], workflow_name, f"{img_slug}-{suite}")
        if pushed:
            print(f"SUCCESS: Pushed updated test results for {img_slug}-{suite} back to repository!")
    finally:
        if temporary_repo:
            shutil.rmtree(repo_dir, ignore_errors=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--batch-dir":
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--batch-dir", required=True)
        parser.add_argument("--img-slug", required=True)
        parser.add_argument("--workflow-name", required=True)
        parser.add_argument("--github-token", required=True)
        parser.add_argument("--digest", default=None)
        parser.add_argument("--repo-dir", default=None)
        args = parser.parse_args()
        try:
            publish_batch_results(
                batch_dir=args.batch_dir,
                img_slug=args.img_slug,
                workflow_name=args.workflow_name,
                github_token=args.github_token,
                digest=args.digest,
                repo_dir=args.repo_dir,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
        return

    if len(sys.argv) < 6:
        print(
            "Usage: publish_test_results.py <results_json_path> <img_slug> "
            "<suite> <workflow_name> <github_token> [digest] [failure_class] "
            "[failure_issue_url]"
        )
        sys.exit(1)

    _publish_single(
        results_json_path=sys.argv[1],
        img_slug=sys.argv[2],
        suite=sys.argv[3],
        workflow_name=sys.argv[4],
        github_token=sys.argv[5],
        digest=sys.argv[6] if len(sys.argv) > 6 else None,
        failure_class=sys.argv[7] if len(sys.argv) > 7 else "test",
        failure_issue_url=sys.argv[8] if len(sys.argv) > 8 else None,
    )

if __name__ == "__main__":
    main()
