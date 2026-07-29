#!/usr/bin/env python3
import sys
import os
import json
import subprocess
import shutil
import urllib.request
from datetime import datetime, timezone

from evaluate_kde_soak import evaluate_kde_soak


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
    print(f"Running command: {' '.join(cmd)}")
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
    elif failure_class == "infra" and not failure_issue_url:
        raise ValueError("failure_issue_url is required for an infra failure")

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

def main():
    if len(sys.argv) < 6:
        print("Usage: publish_test_results.py <results_json_path> <img_slug> <suite> <workflow_name> <github_token> [digest]")
        sys.exit(1)

    results_json_path = sys.argv[1]
    img_slug = sys.argv[2]
    suite = sys.argv[3]
    workflow_name = sys.argv[4]
    github_token = sys.argv[5]
    digest = sys.argv[6] if len(sys.argv) > 6 else None
    failure_class = sys.argv[7] if len(sys.argv) > 7 else "test"
    failure_issue_url = sys.argv[8] if len(sys.argv) > 8 else None

    if not digest:
        print(f"No digest provided. Attempting anonymous resolution for slug {img_slug}...")
        digest = resolve_digest_for_slug(img_slug)
        if digest:
            print(f"Successfully resolved digest: {digest}")
        else:
            print("Digest resolution skipped or failed.")

    if not github_token:
        print("ERROR: github_token is empty. Skipping publication.")
        sys.exit(0)

    if not os.path.exists(results_json_path):
        print(f"WARNING: {results_json_path} not found. Skipping publication.")
        sys.exit(0)

    # 1. Parse behave results.json
    try:
        with open(results_json_path, 'r') as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to parse {results_json_path}: {e}")
        sys.exit(0)

    current_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 2. Clone projectbluefin/lab to a temporary directory
    temp_dir = "/tmp/lab-repo-clone"
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

    repo_url = f"https://x-access-token:{github_token}@github.com/projectbluefin/lab.git"
    run_cmd(["git", "clone", "--depth", "1", repo_url, temp_dir])

    # 3. Locate or create docs/results/<img_slug>-<suite>.json
    results_dir = os.path.join(temp_dir, "docs", "results")
    os.makedirs(results_dir, exist_ok=True)
    result_filename = f"{img_slug}-{suite}.json"
    result_filepath = os.path.join(results_dir, result_filename)

    existing_data = None
    if os.path.exists(result_filepath):
        try:
            with open(result_filepath, 'r') as f:
                existing_data = json.load(f)
        except Exception as e:
            print(f"WARNING: Failed to parse existing results file {result_filepath}: {e}")

    updated_data = parse_results_and_build_update(
        data=data,
        existing_data=existing_data,
        current_utc=current_utc,
        workflow_name=workflow_name,
        img_slug=img_slug,
        suite=suite,
        digest=digest,
        failure_class=failure_class,
        failure_issue_url=failure_issue_url,
    )

    with open(result_filepath, 'w') as f:
        json.dump(updated_data, f)

    # 4. Commit and push back to git
    # Set config
    run_cmd(["git", "config", "user.name", "github-actions[bot]"], cwd=temp_dir)
    run_cmd(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=temp_dir)

    # Git add and commit
    run_cmd(["git", "add", f"docs/results/{result_filename}"], cwd=temp_dir)

    # Check if there are changes before committing
    diff_check = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=temp_dir, check=False)
    if diff_check.returncode == 0:
        print("No changes to test results. Skipping push.")
        # Clean up
        shutil.rmtree(temp_dir)
        sys.exit(0)

    commit_msg = f"chore: update test results for {img_slug}-{suite} ({workflow_name})\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
    run_cmd(["git", "commit", "-m", commit_msg], cwd=temp_dir)

    # Git push back to main
    run_cmd(["git", "push", "origin", "HEAD:main"], cwd=temp_dir)
    print(f"SUCCESS: Pushed updated test results for {img_slug}-{suite} back to repository!")

    # Clean up
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    main()
