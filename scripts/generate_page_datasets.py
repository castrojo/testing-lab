#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_SLUG = 'projectbluefin/lab'
PAGES_BASE = 'https://factory.projectbluefin.io'

# Gating split per ADR 0002 gating amendment: smoke/system (and flatcar for the
# flatcar lane) are gate; developer/software/common are informational.
SUITE_ROLES = {
    'smoke': 'gate',
    'system': 'gate',
    'flatcar': 'gate',
    'developer': 'info',
    'software': 'info',
    'common': 'info',
}

TEST_RUNS_HISTORY_PATH = Path('docs/data/history/test-runs.ndjson')
TEST_RUNS_HISTORY_DAYS = 180

ENROLLMENT_ISSUES_PATH = Path('docs/data/enrollment-issues.json')


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load_json(path: Path):
    with path.open() as handle:
        return json.load(handle)


def repo_blob_url(relative_path: str) -> str:
    return f'https://github.com/{REPO_SLUG}/blob/main/{relative_path}'


def pages_url(relative_path: str) -> str:
    return f'{PAGES_BASE}/{relative_path.lstrip("/")}'


def normalize_result_source_url(relative_path: str, result: dict) -> str:
    return result.get('source_url') or repo_blob_url(f'docs/{relative_path}')


def row_state(last_run: str | None) -> tuple[str, str | None]:
    if last_run:
        return 'available', None
    return 'unavailable', 'Result file exists, but no completed run is published for this matrix cell yet.'


# testsuite renamed its "system" contract-check directory to "lifecycle"; the Argo
# pipelines alias it back (see argo/workflow-templates/run-gnome-tests.yaml:298-301).
SUITE_TO_TESTSUITE_DIR = {
    'system': 'lifecycle',
}


def load_enrollment_issues(root: Path) -> dict[str, dict]:
    """Return variant -> issue metadata for variants not yet enrolled in QA."""
    path = root / ENROLLMENT_ISSUES_PATH
    if not path.exists():
        return {}
    try:
        doc = load_json(path)
        return (doc.get('variants') or {})
    except Exception:
        return {}


def _iter_test_run_records(result: dict, variant: str, branch: str, suite: str, collected_at: str):
    """Yield normalized test-run records from a docs/results/*.json file."""
    role = SUITE_ROLES.get(suite, 'info')
    status = result.get('status')
    failed_scenarios = list(result.get('failed_scenarios') or [])
    current_wf = result.get('workflow_name')
    if current_wf and result.get('last_run') and status in ('passed', 'failed'):
        yield {
            'recorded_at': collected_at,
            'variant': variant,
            'branch': branch,
            'suite': suite,
            'role': role,
            'workflow_name': current_wf,
            'status': status,
            'scenarios_total': result.get('scenarios', 0),
            'scenarios_failed': result.get('failed', 0),
            'failed_scenarios': failed_scenarios,
            'digest': result.get('digest'),
        }
    for h in result.get('history', []) or []:
        run_date = h.get('run_date') or h.get('run')
        wf = h.get('workflow_name')
        hstatus = h.get('status')
        if not run_date or not wf or hstatus not in ('passed', 'failed'):
            continue
        yield {
            'recorded_at': collected_at,
            'variant': variant,
            'branch': branch,
            'suite': suite,
            'role': role,
            'workflow_name': wf,
            'status': hstatus,
            'scenarios_total': h.get('scenarios', 0),
            'scenarios_failed': h.get('failed', 0),
            'failed_scenarios': failed_scenarios if wf == current_wf else [],
            'digest': None,
        }


def append_test_runs_history(root: Path, results_by_path: dict[str, dict], surface_cells: list[dict], collected_at: str) -> int:
    """Seed/append docs/data/history/test-runs.ndjson from docs/results/*.json.

    Dedup key: (variant, branch, suite, workflow_name).
    Retention: lines older than TEST_RUNS_HISTORY_DAYS are dropped.
    """
    path = root / TEST_RUNS_HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    existing_keys: set[tuple[str, str, str, str]] = set()
    if path.exists():
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                existing.append(rec)
                existing_keys.add((
                    rec.get('variant'),
                    rec.get('branch'),
                    rec.get('suite'),
                    rec.get('workflow_name'),
                ))

    path_to_cell = {cell['results_path']: cell for cell in surface_cells}
    new_records = []
    for rel_path, result in results_by_path.items():
        cell = path_to_cell.get(rel_path)
        if not cell:
            continue
        variant = cell['variant']
        branch = cell['branch']
        suite = cell['suite']
        for record in _iter_test_run_records(result, variant, branch, suite, collected_at):
            key = (record['variant'], record['branch'], record['suite'], record['workflow_name'])
            if key in existing_keys:
                continue
            new_records.append(record)
            existing_keys.add(key)

    all_records = existing + new_records
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TEST_RUNS_HISTORY_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    all_records = [r for r in all_records if (r.get('recorded_at') or collected_at) >= cutoff]

    with path.open('w') as handle:
        for record in all_records:
            handle.write(json.dumps(record, separators=(',', ':')) + '\n')
    return len(new_records)


def _row_flake_signal(result: dict | None) -> tuple[int, int]:
    """Return (status_flips, runs_recorded) for a result file's chronological runs."""
    if not result:
        return 0, 0
    runs = []
    seen: set[tuple[str, str]] = set()
    current_wf = result.get('workflow_name')
    if result.get('last_run') and current_wf and result.get('status') in ('passed', 'failed'):
        key = (result['last_run'], current_wf)
        if key not in seen:
            seen.add(key)
            runs.append({'run_date': result['last_run'], 'status': result['status']})
    for h in result.get('history', []) or []:
        run_date = h.get('run_date') or h.get('run')
        wf = h.get('workflow_name')
        status = h.get('status')
        if not run_date or not wf or status not in ('passed', 'failed'):
            continue
        key = (run_date, wf)
        if key in seen:
            continue
        seen.add(key)
        runs.append({'run_date': run_date, 'status': status})
    runs.sort(key=lambda r: r['run_date'])
    flips = sum(1 for i in range(1, len(runs)) if runs[i - 1]['status'] != runs[i]['status'])
    return flips, len(runs)


def warn_if_surface_drifted_from_testsuite(root: Path) -> None:
    """Best-effort, log-only guard against docs/data/test-surface.json drifting from the
    real projectbluefin/testsuite feature inventory (the way the fabricated 'aurora' and
    'bazzite' rows drifted in undetected). Never fails the build — network/rate-limit
    failures are swallowed, matching the fallback pattern in src/pages/applications.astro.
    """
    try:
        tree_json = subprocess.run(
            [
                'curl', '-fs', '-H', 'User-Agent: lab-builder', '--max-time', '3',
                'https://api.github.com/repos/projectbluefin/testsuite/git/trees/main?recursive=1',
            ],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        tree = json.loads(tree_json)
        feature_dirs = {
            entry['path'].split('/')[1]
            for entry in tree.get('tree', [])
            if entry.get('type') == 'blob' and entry['path'].startswith('tests/') and entry['path'].endswith('.feature')
        }
    except Exception:
        return

    surface = load_json(root / 'docs/data/test-surface.json')
    tracked_suites = {cell['suite'] for cell in surface.get('surface', [])}
    for suite in sorted(tracked_suites):
        testsuite_dir = SUITE_TO_TESTSUITE_DIR.get(suite, suite)
        if testsuite_dir not in feature_dirs:
            print(
                f"WARNING: test-surface.json tracks suite '{suite}' (testsuite dir "
                f"'{testsuite_dir}') but projectbluefin/testsuite has no tests/{testsuite_dir}/ "
                "features. The matrix may be tracking a renamed or removed suite.",
                file=sys.stderr,
            )


def iter_surface_cells(root: Path):
    surface = load_json(root / 'docs/data/test-surface.json')
    for cell in surface.get('surface', []):
        yield cell


def load_results_by_relative_path(root: Path) -> dict[str, dict]:
    results = {}
    for result_path in (root / 'docs/results').glob('*.json'):
        relative_path = result_path.relative_to(root / 'docs').as_posix()
        results[relative_path] = load_json(result_path)
    return results


def build_upstream_status(root: Path, collected_at: str) -> dict:
    stats = load_json(root / 'docs/data/factory-stats.json')
    publishers = load_json(root / 'docs/data/variant-publishers.json')
    images = ((stats.get('factory') or {}).get('images') or {})

    groups = [
        {
            'id': 'gnome-os',
            'label': 'GNOME OS',
            'description': 'GNOME OS upstream images used for lab expansion and comparison.',
            'source_url': repo_blob_url('argo/workflow-templates/provision-gnomeos-vm.yaml'),
            'collected_at': collected_at,
            'derivation': 'Known upstream scope from the GNOME OS provisioning workflow tracked in git.',
        },
        {
            'id': 'fedora-bootc',
            'label': 'Fedora bootc',
            'description': 'Fedora bootc upstream streams with digest pollers tracked in git.',
            'source_url': repo_blob_url('manifests/image-poll-fedora-bootc-latest.yaml'),
            'collected_at': collected_at,
            'derivation': 'Known upstream scope from Fedora bootc image poller manifests tracked in git.',
        },
        {
            'id': 'projectbluefin',
            'label': 'Project Bluefin variants',
            'description': 'Bluefin family images published by projectbluefin.',
            'source_url': repo_blob_url('docs/data/variant-publishers.json'),
            'collected_at': collected_at,
            'derivation': 'Derived from variant publisher mapping already published in docs/data.',
        },
        {
            'id': 'ublue',
            'label': 'Universal Blue derivatives',
            'description': 'Derivative desktop images published by ublue-os.',
            'source_url': repo_blob_url('docs/data/variant-publishers.json'),
            'collected_at': collected_at,
            'derivation': 'Derived from variant publisher mapping already published in docs/data.',
        },
        {
            'id': 'cosmic',
            'label': 'COSMIC desktop',
            'description': 'COSMIC desktop OCI images built in-cluster via the cosmic-build-pipeline Argo WorkflowTemplate.',
            'source_url': repo_blob_url('argo/workflow-templates/cosmic-build-pipeline.yaml'),
            'collected_at': collected_at,
            'derivation': 'Known in-cluster build scope from the cosmic-build-pipeline WorkflowTemplate tracked in git.',
        },
    ]

    rows = []
    for variant, details in (publishers.get('variants') or {}).items():
        org = details.get('org')
        if org not in {'projectbluefin', 'ublue-os', 'razorfinos'}:
            continue
        group = 'projectbluefin' if org == 'projectbluefin' else ('cosmic' if org == 'razorfinos' else 'ublue')
        repo = details.get('publisher_repo')
        releases_url = f'https://github.com/{repo}/releases' if repo else repo_blob_url('docs/data/variant-publishers.json')
        image_summary = images.get(variant, {})
        for branch in details.get('branches') or []:
            row_id = f'{variant}-{branch}'
            published_at = image_summary.get(f'{branch}_seen_at')
            freshness_age_days = image_summary.get(f'{branch}_age_days')
            state = 'available' if published_at else 'unavailable'
            if org == 'razorfinos':
                state_reason = None if published_at else (
                    'COSMIC image is built in-cluster via the cosmic-build-pipeline Argo WorkflowTemplate '
                    'and exported to the local Zot registry — no external GitHub release timestamp is collected yet.'
                )
            else:
                state_reason = None if published_at else 'No published release timestamp is present in docs/data/factory-stats.json for this lane.'
            rows.append(
                {
                    'id': row_id,
                    'group': group,
                    'variant': variant,
                    'display_name': f'{variant} {branch}',
                    'publisher_repo': repo,
                    'org': org,
                    'branch': branch,
                    'published_at': published_at,
                    'freshness_age_days': freshness_age_days,
                    'open_prs': None,
                    'state': state,
                    'state_reason': state_reason,
                    'source_url': image_summary.get(f'{branch}_source_url') or releases_url,
                    'collected_at': collected_at,
                    'derivation': (
                        f'Join docs/data/variant-publishers.json branches with '
                        f'docs/data/factory-stats.json factory.images.{variant}.{branch}_seen_at/{branch}_age_days.'
                    ),
                }
            )

    rows.extend(
        [
            {
                'id': 'gnomeos-nightly',
                'group': 'gnome-os',
                'variant': 'gnomeos',
                'display_name': 'GNOME OS nightly',
                'publisher_repo': None,
                'org': None,
                'branch': 'nightly',
                'published_at': None,
                'freshness_age_days': None,
                'open_prs': None,
                'state': 'unavailable',
                'state_reason': 'Known GNOME OS workflow exists, but no repo-owned artifact publishes a nightly release timestamp yet.',
                'source_url': repo_blob_url('argo/workflow-templates/provision-gnomeos-vm.yaml'),
                'collected_at': collected_at,
                'derivation': 'Scope placeholder derived from the existing GNOME OS provisioning workflow tracked in git.',
            },
            {
                'id': 'fedora-bootc-stable',
                'group': 'fedora-bootc',
                'variant': 'fedora-bootc',
                'display_name': 'Fedora bootc stable',
                'publisher_repo': 'fedora/fedora-bootc',
                'org': 'fedora',
                'branch': 'stable',
                'published_at': None,
                'freshness_age_days': None,
                'open_prs': None,
                'state': 'unavailable',
                'state_reason': 'Known Fedora bootc poller exists, but no repo-owned artifact publishes a stable release timestamp yet.',
                'source_url': repo_blob_url('manifests/image-poll-fedora-bootc-latest.yaml'),
                'collected_at': collected_at,
                'derivation': 'Map the git-tracked latest poller manifest to the stable Fedora bootc lane until repo data publishes release timestamps.',
            },
            {
                'id': 'fedora-bootc-testing',
                'group': 'fedora-bootc',
                'variant': 'fedora-bootc',
                'display_name': 'Fedora bootc testing',
                'publisher_repo': 'fedora/fedora-bootc',
                'org': 'fedora',
                'branch': 'testing',
                'published_at': None,
                'freshness_age_days': None,
                'open_prs': None,
                'state': 'unavailable',
                'state_reason': 'Known Fedora bootc poller exists, but no repo-owned artifact publishes a testing release timestamp yet.',
                'source_url': repo_blob_url('manifests/image-poll-fedora-bootc-rawhide.yaml'),
                'collected_at': collected_at,
                'derivation': 'Map the git-tracked rawhide poller manifest to the testing Fedora bootc lane until repo data publishes release timestamps.',
            },
        ]
    )

    release_rows = [row for row in rows if row.get('published_at')]
    unavailable_rows = [row for row in rows if row.get('state') == 'unavailable']

    return {
        'schema_version': 'v1',
        '_meta': {
            'page': 'upstream',
            'description': 'Collector-derived contract for the multipage upstream status view.',
            'generated_at': collected_at,
            'starter_artifact': False,
            'status': 'partial' if unavailable_rows else 'ready',
        },
        'summary_metrics': [
            {
                'id': 'tracked_upstream_lanes',
                'label': 'Tracked upstream lanes',
                'value': len(rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/variant-publishers.json'),
                'collected_at': collected_at,
                'derivation': 'Count concrete upstream rows assembled from publisher mappings and known workflow placeholders.',
            },
            {
                'id': 'lanes_with_release_data',
                'label': 'Lanes with published release data',
                'value': len(release_rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/factory-stats.json'),
                'collected_at': collected_at,
                'derivation': 'Count upstream rows whose published_at is present in docs/data/factory-stats.json.',
            },
            {
                'id': 'lanes_without_release_data',
                'label': 'Lanes awaiting collectors',
                'value': len(unavailable_rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/page-contracts.md'),
                'collected_at': collected_at,
                'derivation': 'Count upstream rows still marked unavailable after deriving from repo-owned inputs.',
            },
        ],
        'groups': groups,
        'rows': rows,
    }


def build_tests_matrix(root: Path, collected_at: str) -> dict:
    results_by_path = load_results_by_relative_path(root)
    enrollment = load_enrollment_issues(root)
    surface_cells = list(iter_surface_cells(root))
    rows = []
    variants = set()
    branches = set()
    suites = set()

    # Normal rows from the tracked test surface, excluding variants that are not
    # yet enrolled in the QA pipeline (those are emitted uniformly below).
    for cell in surface_cells:
        variant = cell['variant']
        branch = cell['branch']
        suite = cell['suite']
        if variant in enrollment:
            continue
        relative_results_path = cell['results_path']
        result = results_by_path.get(relative_results_path, {})
        last_run = result.get('last_run')
        state, state_reason = row_state(last_run)
        scenarios_total = result.get('scenarios', 0)
        scenarios_failed = result.get('failed', 0)
        pass_rate = None
        if scenarios_total:
            pass_rate = round(((scenarios_total - scenarios_failed) / scenarios_total) * 100, 2)
        screenshot_path = cell.get('screenshot_path')
        screenshot_url = result.get('screenshot_url')
        if not screenshot_url and screenshot_path:
            screenshot_url = pages_url(screenshot_path)
        flake_flips, runs_recorded = _row_flake_signal(result)

        rows.append(
            {
                'id': f'{variant}-{branch}-{suite}',
                'variant': variant,
                'branch': branch,
                'suite': suite,
                'role': SUITE_ROLES.get(suite, 'info'),
                'result_status': result.get('status', 'missing'),
                'last_run': last_run,
                'workflow_name': result.get('workflow_name'),
                'digest': result.get('digest'),
                'scenarios_total': scenarios_total,
                'scenarios_failed': scenarios_failed,
                'pass_rate': pass_rate,
                'history_points': len(result.get('history', [])),
                'results_path': relative_results_path,
                'screenshot_path': screenshot_path,
                'screenshot_url': screenshot_url,
                'state': state,
                'state_reason': state_reason,
                'enrollment_issue_url': None,
                'flake_flips': flake_flips,
                'runs_recorded': runs_recorded,
                'source_url': normalize_result_source_url(relative_results_path, result),
                'collected_at': collected_at,
                'derivation': (
                    f'Join docs/data/test-surface.json row with docs/{relative_results_path}; '
                    'compute pass_rate when scenarios_total > 0; attach suite role and flake metrics.'
                ),
            }
        )
        variants.add(variant)
        branches.add(branch)
        suites.add(suite)

    # Ensure the full suite dimension exists even if the surface is empty.
    suites.update(SUITE_ROLES.keys())

    # Uniformly emit unavailable rows for every unenrolled variant x suite.
    enrollment_reason = 'Not enrolled in the QA pipeline yet; tracking issue open.'
    for variant, details in sorted(enrollment.items()):
        issue_url = details.get('issue_url')
        for suite in sorted(suites):
            rows.append(
                {
                    'id': f'{variant}-testing-{suite}',
                    'variant': variant,
                    'branch': 'testing',
                    'suite': suite,
                    'role': SUITE_ROLES.get(suite, 'info'),
                    'result_status': 'missing',
                    'last_run': None,
                    'workflow_name': None,
                    'digest': None,
                    'scenarios_total': 0,
                    'scenarios_failed': 0,
                    'pass_rate': None,
                    'history_points': 0,
                    'results_path': None,
                    'screenshot_path': None,
                    'screenshot_url': None,
                    'state': 'unavailable',
                    'state_reason': enrollment_reason,
                    'enrollment_issue_url': issue_url,
                    'flake_flips': 0,
                    'runs_recorded': 0,
                    'source_url': issue_url or repo_blob_url('docs/data/enrollment-issues.json'),
                    'collected_at': collected_at,
                    'derivation': (
                        'Unenrolled variant row derived from docs/data/enrollment-issues.json; '
                        'no QA results are collected yet.'
                    ),
                }
            )
            variants.add(variant)
            branches.add('testing')

    # Seed/append the rolling test-runs history from every result file we joined.
    appended_history = append_test_runs_history(root, results_by_path, surface_cells, collected_at)

    completed_rows = [row for row in rows if row['state'] == 'available']
    unavailable_rows = [row for row in rows if row['state'] == 'unavailable']
    flaky_rows = [row for row in rows if row.get('flake_flips', 0) >= 2]

    return {
        'schema_version': 'v2',
        '_meta': {
            'page': 'tests',
            'description': (
                'Collector-derived contract for the multipage tests matrix view. '
                'Schema v2 adds suite_roles, unenrolled variant rows, flake metrics, '
                'and git-tracked test-runs.ndjson history.'
            ),
            'generated_at': collected_at,
            'starter_artifact': False,
            'status': 'partial' if unavailable_rows else 'ready',
        },
        'summary_metrics': [
            {
                'id': 'published_matrix_rows',
                'label': 'Published matrix rows',
                'value': len(rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/test-surface.json'),
                'collected_at': collected_at,
                'derivation': 'Count rows emitted by the collector for the tests matrix.',
            },
            {
                'id': 'rows_with_completed_runs',
                'label': 'Rows with completed runs',
                'value': len(completed_rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/results'),
                'collected_at': collected_at,
                'derivation': 'Count matrix rows whose joined docs/results/*.json file has last_run set.',
            },
            {
                'id': 'rows_waiting_for_results',
                'label': 'Rows waiting for results',
                'value': len(unavailable_rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/test-surface.json'),
                'collected_at': collected_at,
                'derivation': 'Count matrix rows still marked unavailable after joining published results and enrollment issues.',
            },
            {
                'id': 'flaky_rows',
                'label': 'Flaky matrix rows',
                'value': len(flaky_rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/history/test-runs.ndjson'),
                'collected_at': collected_at,
                'derivation': 'Count matrix rows with at least two pass/fail status flips in their recorded run history.',
            },
        ],
        'suite_roles': SUITE_ROLES,
        'dimensions': {
            'variants': sorted(variants),
            'branches': sorted(branches),
            'suites': sorted(suites),
        },
        'rows': rows,
    }


def bazaar_fallback_signals(relative_results_path: str, result: dict, collected_at: str) -> list[dict]:
    matches = [scenario for scenario in result.get('failed_scenarios', []) if 'bazaar' in scenario.lower()]
    if not matches:
        return []
    return [
        {
            'suite': result.get('suite'),
            'matched_scenarios': matches,
            'status': result.get('status'),
            'last_run': result.get('last_run'),
            'workflow_name': result.get('workflow_name'),
            'state': 'unavailable',
            'state_reason': 'Coarse fallback only: Bazaar evidence comes from scenario-name substring matching in a non-application suite.',
            'source_url': normalize_result_source_url(relative_results_path, result),
            'collected_at': collected_at,
            'derivation': f'Case-insensitive /bazaar/ match against failed_scenarios in docs/{relative_results_path}.',
        }
    ]


def build_applications_matrix(root: Path, collected_at: str) -> dict:
    results_by_path = load_results_by_relative_path(root)
    software_cells = [cell for cell in iter_surface_cells(root) if cell['suite'] == 'software']
    rows = []

    applications = [
        {
            'id': 'bazaar',
            'display_name': 'Bazaar',
            'scope': 'v1',
            'primary_suite': 'software',
            'fallback_suites': ['common'],
            'state': 'available',
            'state_reason': None,
            'source_url': repo_blob_url('docs/data/page-contracts.md'),
            'collected_at': collected_at,
            'derivation': 'Catalog entry from the page contract: applications v1 includes Bazaar and Firefox.',
        },
        {
            'id': 'firefox',
            'display_name': 'Firefox',
            'scope': 'v1',
            'primary_suite': 'software',
            'fallback_suites': [],
            'state': 'available',
            'state_reason': None,
            'source_url': repo_blob_url('docs/data/page-contracts.md'),
            'collected_at': collected_at,
            'derivation': 'Catalog entry from the page contract: applications v1 includes Bazaar and Firefox.',
        },
    ]

    rows_with_primary_results = 0
    rows_with_fallbacks = 0
    for application in applications:
        app_id = application['id']
        app_name = application['display_name']
        for cell in software_cells:
            variant = cell['variant']
            branch = cell['branch']
            relative_results_path = cell['results_path']
            primary_result = results_by_path.get(relative_results_path, {})
            primary_last_run = primary_result.get('last_run')
            if primary_last_run:
                rows_with_primary_results += 1

            fallback_signals = []
            if app_id == 'bazaar':
                fallback_relative_path = f'results/{variant}-{branch}-common.json'
                fallback_result = results_by_path.get(fallback_relative_path, {})
                fallback_signals = bazaar_fallback_signals(fallback_relative_path, fallback_result, collected_at)
            if fallback_signals:
                rows_with_fallbacks += 1

            state = 'available' if primary_last_run else 'unavailable'
            state_reason = None if primary_last_run else (
                f'No completed {app_name}-specific software result is published for this variant/branch; '
                'fallback signals remain coarse only.'
            )
            rows.append(
                {
                    'id': f'{app_id}-{variant}-{branch}',
                    'app_id': app_id,
                    'variant': variant,
                    'branch': branch,
                    'primary_suite': 'software',
                    'primary_result_status': primary_result.get('status', 'missing'),
                    'primary_last_run': primary_last_run,
                    'scenario_total': primary_result.get('scenarios'),
                    'scenario_failed': primary_result.get('failed'),
                    'fallback_signal_count': len(fallback_signals),
                    'fallback_signals': fallback_signals,
                    'state': state,
                    'state_reason': state_reason,
                    'source_url': normalize_result_source_url(relative_results_path, primary_result),
                    'collected_at': collected_at,
                    'derivation': (
                        f'Seed row from docs/data/test-surface.json software cells for {app_name}; '
                        f'join docs/{relative_results_path} for primary evidence.'
                        + (
                            ' Attach coarse Bazaar fallback signals from matching non-application results.'
                            if app_id == 'bazaar'
                            else ' No fallback suite is configured for Firefox yet.'
                        )
                    ),
                }
            )

    unavailable_rows = [row for row in rows if row['state'] == 'unavailable']

    return {
        'schema_version': 'v1',
        '_meta': {
            'page': 'applications',
            'description': 'Collector-derived contract for the multipage applications matrix view.',
            'generated_at': collected_at,
            'starter_artifact': False,
            'status': 'partial' if unavailable_rows else 'ready',
        },
        'summary_metrics': [
            {
                'id': 'tracked_applications',
                'label': 'Tracked applications',
                'value': len(applications),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/page-contracts.md'),
                'collected_at': collected_at,
                'derivation': 'Count applications[] entries in this collector-derived artifact.',
            },
            {
                'id': 'application_rows',
                'label': 'Application matrix rows',
                'value': len(rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/test-surface.json'),
                'collected_at': collected_at,
                'derivation': 'Count software suite rows in docs/data/test-surface.json.',
            },
            {
                'id': 'rows_with_primary_app_results',
                'label': 'Rows with primary app results',
                'value': rows_with_primary_results,
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/results'),
                'collected_at': collected_at,
                'derivation': 'Count application rows whose software suite has a completed result with last_run.',
            },
            {
                'id': 'rows_with_fallback_signals',
                'label': 'Rows with fallback Bazaar signals',
                'value': rows_with_fallbacks,
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/results/bluefin-testing-common.json'),
                'collected_at': collected_at,
                'derivation': 'Count application rows that picked up coarse Bazaar fallback signals from published non-application results.',
            },
        ],
        'applications': applications,
        'rows': rows,
    }


def iter_tracked_lanes(publishers: dict):
    """Yield (variant, branch, details) for every tracked lane."""
    for variant, details in (publishers.get('variants') or {}).items():
        for branch in details.get('branches') or []:
            yield variant, branch, details


def load_optional_json(path: Path):
    if not path.exists():
        return None
    return load_json(path)


def build_homebrew_tap_stats(tap: dict) -> dict:
    packages = tap.get('packages', [])
    install_count = sum(pkg.get('installs_90d', 0) for pkg in packages)
    download_count = sum(pkg.get('downloads', 0) for pkg in packages)
    type_counts = {}
    for pkg in packages:
        pkg_type = pkg.get('type', 'unknown')
        type_counts[pkg_type] = type_counts.get(pkg_type, 0) + 1
    top_packages = sorted(
        packages,
        key=lambda pkg: (pkg.get('installs_90d', 0), pkg.get('downloads', 0)),
        reverse=True,
    )[:10]
    return {
        'package_count': len(packages),
        'install_count': install_count,
        'download_count': download_count,
        'package_type_counts': type_counts,
        'top_packages': [
            {
                'name': pkg.get('name'),
                'type': pkg.get('type'),
                'installs_90d': pkg.get('installs_90d', 0),
                'downloads': pkg.get('downloads', 0),
            }
            for pkg in top_packages
        ],
    }


def build_homebrew_ecosystem(root: Path, collected_at: str) -> dict:
    publishers = load_json(root / 'docs/data/variant-publishers.json')
    migrated = load_optional_json(root / 'docs/data/homebrew-package-stats-migrated.json') or {'taps': []}
    tap_by_variant = {
        variant: tap
        for tap in migrated.get('taps', [])
        for variant in tap.get('variant_scope', [])
    }
    tap_stats_by_name = {
        tap.get('name'): build_homebrew_tap_stats(tap)
        for tap in migrated.get('taps', [])
    }

    taps = []
    for tap in migrated.get('taps', []):
        tap_stats = tap_stats_by_name.get(tap.get('name'), {})
        taps.append(
            {
                'id': tap['name'].replace('/', '-'),
                'name': tap['name'],
                'url': tap['url'],
                'description': tap.get('description'),
                'variant_scope': tap.get('variant_scope', []),
                'package_count': tap_stats.get('package_count', 0),
                'install_count': tap_stats.get('install_count', 0),
                'download_count': tap_stats.get('download_count', 0),
                'package_type_counts': tap_stats.get('package_type_counts', {}),
                'top_packages': tap_stats.get('top_packages', []),
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/homebrew-package-stats-migrated.json'),
                'collected_at': collected_at,
                'derivation': 'Transplanted from repo-owned docs/data/homebrew-package-stats-migrated.json.',
            }
        )

    rows = []
    for variant, branch, details in iter_tracked_lanes(publishers):
        tap = tap_by_variant.get(variant)
        if tap:
            tap_stats = tap_stats_by_name.get(tap.get('name'), {})
            package_count = tap_stats.get('package_count', 0)
            rows.append(
                {
                    'id': f'{variant}-{branch}',
                    'variant': variant,
                    'branch': branch,
                    'tap_name': tap['name'],
                    'tap_url': tap['url'],
                    'package_count': package_count,
                    'install_count': tap_stats.get('install_count', 0),
                    'download_count': tap_stats.get('download_count', 0),
                    'state': 'available',
                    'state_reason': None,
                    'source_url': repo_blob_url('docs/data/homebrew-package-stats-migrated.json'),
                    'collected_at': collected_at,
                    'derivation': (
                        f'Global formula analytics from formulae.brew.sh transplanted for a {package_count}-package tap '
                        f'from repo-owned docs/data/homebrew-package-stats-migrated.json. '
                        f'Numbers are not lane-attributable installs — the same values appear on every branch row '
                        f'for this variant because the source has no branch dimension.'
                    ),
                }
            )
            continue

        repo = details.get('publisher_repo')
        releases_url = (
            f'https://github.com/{repo}/releases'
            if repo
            else repo_blob_url('docs/data/variant-publishers.json')
        )
        rows.append(
            {
                'id': f'{variant}-{branch}',
                'variant': variant,
                'branch': branch,
                'tap_name': None,
                'tap_url': None,
                'package_count': None,
                'install_count': None,
                'download_count': None,
                'state': 'unavailable',
                'state_reason': (
                    'No Homebrew analytics data from formulae.brew.sh or upstream tap repos is tracked in '
                    'docs/data/ for this lane. Collector will populate install_count/download_count once a '
                    'repo-owned artifact fetched from those sources is committed.'
                ),
                'source_url': releases_url,
                'collected_at': collected_at,
                'derivation': (
                    f'Lane derived from docs/data/variant-publishers.json {variant}.branches; '
                    'no Homebrew analytics data (formulae.brew.sh or upstream tap repos) found in docs/data/.'
                ),
            }
        )

    lanes_with_brew = [row for row in rows if row['state'] == 'available']
    lanes_without_brew = [row for row in rows if row['state'] == 'unavailable']
    package_leaderboard = sorted(
        [
            {
                'tap_name': tap.get('name'),
                'package_count': stats.get('package_count', 0),
                'install_count': stats.get('install_count', 0),
                'download_count': stats.get('download_count', 0),
            }
            for tap_name, stats in tap_stats_by_name.items()
            for tap in migrated.get('taps', [])
            if tap.get('name') == tap_name
        ],
        key=lambda entry: (entry['package_count'], entry['install_count']),
        reverse=True,
    )

    return {
        'schema_version': 'v1',
        '_meta': {
            'page': 'homebrew',
            'description': 'Collector-derived contract for the Homebrew ecosystem tab.',
            'generated_at': collected_at,
            'starter_artifact': False,
            'status': 'partial' if lanes_without_brew else 'ready',
        },
        'summary_metrics': [
            {
                'id': 'tracked_image_lanes',
                'label': 'Tracked image lanes',
                'value': len(rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/variant-publishers.json'),
                'collected_at': collected_at,
                'derivation': 'Count all variant-branch lanes from docs/data/variant-publishers.json.',
            },
            {
                'id': 'lanes_with_brew_data',
                'label': 'Lanes with Homebrew data',
                'value': len(lanes_with_brew),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/variant-publishers.json'),
                'collected_at': collected_at,
                'derivation': (
                    'Count lanes with Homebrew analytics data from formulae.brew.sh or upstream tap repos '
                    'present in docs/data/.'
                ),
            },
            {
                'id': 'lanes_awaiting_brew_data',
                'label': 'Lanes awaiting Homebrew data',
                'value': len(lanes_without_brew),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/variant-publishers.json'),
                'collected_at': collected_at,
                'derivation': (
                    'Count lanes with no Homebrew analytics data from formulae.brew.sh or upstream tap repos '
                    'in docs/data/.'
                ),
            },
        ],
        'taps': taps,
        'package_leaderboard': package_leaderboard,
        'rows': rows,
    }


def build_adoption_metrics(root: Path, collected_at: str) -> dict:
    publishers = load_json(root / 'docs/data/variant-publishers.json')
    migrated_countme = load_optional_json(root / 'docs/data/adoption-countme-migrated.json') or {'distros': {}}
    countme_by_variant = migrated_countme.get('distros', {})
    historical = load_optional_json(root / 'docs/data/adoption-historical-raw.json') or {}

    trust_cards = []
    for variant, details in (publishers.get('variants') or {}).items():
        repo = details.get('publisher_repo')
        org = details.get('org')
        publisher_known = bool(repo and org)
        trust_cards.append(
            {
                'variant': variant,
                'publisher_repo': repo,
                'org': org,
                'emits_sbom': details.get('emits_sbom', False),
                'emits_cve_scan': details.get('emits_cve_scan', False),
                'emits_cosign_attestation': details.get('emits_cosign_attestation', False),
                'state': 'available' if publisher_known else 'unavailable',
                'state_reason': (
                    None if publisher_known else
                    'publisher_repo and org are unknown for this variant; '
                    'trust-summary card requires repo-owned evidence to be meaningful.'
                ),
                'source_url': (
                    f'https://github.com/{repo}'
                    if repo
                    else repo_blob_url('docs/data/variant-publishers.json')
                ),
                'collected_at': collected_at,
                'derivation': (
                    f'Trust metadata for {variant} read directly from '
                    'docs/data/variant-publishers.json emits_sbom/emits_cve_scan/emits_cosign_attestation fields.'
                ),
            }
        )

    rows = []
    week_start = migrated_countme.get('week_start', '')
    week_end = migrated_countme.get('week_end', '')
    for variant, branch, details in iter_tracked_lanes(publishers):
        repo = details.get('publisher_repo')
        releases_url = (
            f'https://github.com/{repo}/releases'
            if repo
            else repo_blob_url('docs/data/variant-publishers.json')
        )
        countme_value = countme_by_variant.get(variant)
        rows.append(
            {
                'id': f'{variant}-{branch}',
                'variant': variant,
                'branch': branch,
                'pull_count': None,
                'countme_active_devices': countme_value,
                'state': 'available' if countme_value is not None else 'unavailable',
                'state_reason': None if countme_value is not None else (
                    'No registry pull-count data (GHCR or container registry API) or active-device data '
                    '(Fedora countme infrastructure) is tracked in docs/data/ for this lane.'
                ),
                'source_url': repo_blob_url('docs/data/adoption-countme-migrated.json') if countme_value is not None else releases_url,
                'collected_at': collected_at,
                'derivation': (
                    f'Distro-wide countme active-device count transplanted from repo-owned '
                    f'docs/data/adoption-countme-migrated.json (snapshot week {week_start} to {week_end}). '
                    f'The same value is reused for each tracked branch because the source has no branch dimension.'
                    if countme_value is not None
                    else f'Lane derived from docs/data/variant-publishers.json {variant}.branches; no registry pull-count data (GHCR API) or Fedora countme data found in docs/data/.'
                ),
            }
        )

    lanes_with_pull = [row for row in rows if row.get('pull_count') is not None]
    lanes_with_countme = [row for row in rows if row.get('countme_active_devices') is not None]
    unavailable_rows = [row for row in rows if row['state'] == 'unavailable']

    return {
        'schema_version': 'v1',
        '_meta': {
            'page': 'adoption',
            'description': 'Collector-derived contract for the Adoption metrics tab.',
            'generated_at': collected_at,
            'starter_artifact': False,
            'status': 'partial' if unavailable_rows else 'ready',
        },
        'summary_metrics': [
            {
                'id': 'tracked_image_lanes',
                'label': 'Tracked image lanes',
                'value': len(rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/variant-publishers.json'),
                'collected_at': collected_at,
                'derivation': 'Count all variant-branch lanes from docs/data/variant-publishers.json.',
            },
            {
                'id': 'lanes_with_pull_data',
                'label': 'Lanes with image pull data',
                'value': len(lanes_with_pull),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/variant-publishers.json'),
                'collected_at': collected_at,
                'derivation': (
                    'Count lanes whose pull_count is non-null after joining container registry API data '
                    '(e.g., GHCR package statistics) from docs/data/.'
                ),
            },
            {
                'id': 'lanes_with_countme_data',
                'label': 'Lanes with countme data',
                'value': len(lanes_with_countme),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/variant-publishers.json'),
                'collected_at': collected_at,
                'derivation': (
                    'Count lanes whose countme_active_devices is non-null after joining '
                    'Fedora countme infrastructure data from docs/data/.'
                ),
            },
        ],
        'trust_cards': trust_cards,
        'rows': rows,
        'countme_trend': historical.get('countme_trend'),
        'quay_trend': historical.get('quay_trend'),
        'dora_comparison': historical.get('dora_comparison'),
        'os_version': historical.get('os_version'),
        'openssf_scorecard': historical.get('openssf_scorecard'),
        'oci_best_practices': historical.get('oci_best_practices'),
    }


# The real, currently-shipping OS image build pipelines: actual GitHub Actions
# workflows in the image repos, publicly queryable via the GitHub REST API from
# any GitHub-hosted runner (no cluster/LAN/ARC access needed at all). Each is
# joined against docs/data/factory-stats.json['image_builds'], populated by
# fetch_image_build_history() in scripts/refresh_factory_stats.py.
BUILD_PIPELINE_CATALOG = [
    {
        'id': 'bluefin-stable',
        'display_name': 'Bluefin — stable',
        'description': 'Bluefin main-branch image build (published as the :stable tag).',
        'repo': 'projectbluefin/bluefin',
        'workflow_path': '.github/workflows/build-image-testing.yml',
    },
    {
        'id': 'bluefin-testing',
        'display_name': 'Bluefin — testing',
        'description': 'Bluefin testing-branch image build (published as the :testing tag).',
        'repo': 'projectbluefin/bluefin',
        'workflow_path': '.github/workflows/build-image-testing.yml',
    },
    {
        'id': 'bluefin-next',
        'display_name': 'Bluefin — next (sealed)',
        'description': 'Bluefin sealed/next preview image build.',
        'repo': 'projectbluefin/bluefin',
        'workflow_path': '.github/workflows/build-image-next.yml',
    },
    {
        'id': 'bluefin-lts-stable',
        'display_name': 'Bluefin LTS — stable',
        'description': 'Bluefin LTS main-branch image build (published as the :stable tag).',
        'repo': 'projectbluefin/bluefin-lts',
        'workflow_path': '.github/workflows/build-regular.yml',
    },
    {
        'id': 'bluefin-lts-testing',
        'display_name': 'Bluefin LTS — testing',
        'description': 'Bluefin LTS testing-branch image build (published as the :testing tag).',
        'repo': 'projectbluefin/bluefin-lts',
        'workflow_path': '.github/workflows/build-regular.yml',
    },
    {
        'id': 'bluefin-lts-hwe',
        'display_name': 'Bluefin LTS HWE',
        'description': 'Bluefin LTS build with the Hardware Enablement (HWE) kernel.',
        'repo': 'projectbluefin/bluefin-lts',
        'workflow_path': '.github/workflows/build-regular-hwe.yml',
    },
    {
        'id': 'bluefin-lts-nvidia',
        'display_name': 'Bluefin LTS Nvidia',
        'description': 'Bluefin LTS build with proprietary Nvidia driver layering.',
        'repo': 'projectbluefin/bluefin-lts',
        'workflow_path': '.github/workflows/build-nvidia.yml',
    },
    {
        'id': 'dakota',
        'display_name': 'Dakota',
        'description': 'Dakota bootc image build (x86_64), published to ghcr.io/projectbluefin/dakota.',
        'repo': 'projectbluefin/dakota',
        'workflow_path': '.github/workflows/build.yml',
    },
    {
        'id': 'dakota-aarch64',
        'display_name': 'Dakota (aarch64)',
        'description': 'Dakota bootc image build for the aarch64 architecture.',
        'repo': 'projectbluefin/dakota',
        'workflow_path': '.github/workflows/build-aarch64.yml',
    },
    {
        'id': 'cosmic-stable',
        'display_name': 'COSMIC — stable',
        'description': 'COSMIC desktop OCI image built in-cluster via the cosmic-build-pipeline Argo WorkflowTemplate (BST build, exported to local Zot :30500).',
        'repo': 'RazorfinOS-org/cosmic-build-meta',
        'workflow_path': 'argo/workflow-templates/cosmic-build-pipeline.yaml',
        'source': 'argo',
        'argo_pipeline_key': 'cosmic-build-pipeline',
    },
    {
        'id': 'cosmic-nvidia',
        'display_name': 'COSMIC — Nvidia',
        'description': 'COSMIC desktop OCI image with proprietary Nvidia driver layering, built in parallel with cosmic-stable by the cosmic-build-pipeline.',
        'repo': 'RazorfinOS-org/cosmic-build-meta',
        'workflow_path': 'argo/workflow-templates/cosmic-build-pipeline.yaml',
        'source': 'argo',
        'argo_pipeline_key': 'cosmic-build-pipeline',
    },
    {
        'id': 'cosmic-qa',
        'display_name': 'COSMIC — QA pipeline',
        'description': 'End-to-end COSMIC QA pipeline: smoke, developer, and system suites run against a KubeVirt VM provisioned from the local Zot containerDisk.',
        'repo': 'RazorfinOS-org/cosmic-build-meta',
        'workflow_path': 'argo/workflow-templates/cosmic-qa-pipeline.yaml',
        'source': 'argo',
        'argo_pipeline_key': 'cosmic-qa-pipeline',
    },
    {
        'id': 'snosi-latest',
        'display_name': 'Snosi — latest',
        'description': 'Snosi (Snow OS) image build on the latest branch, published to ghcr.io/frostyard/snow.',
        'repo': 'frostyard/snow',
        'workflow_path': '.github/workflows/build.yml',
    },
    {
        'id': 'aurora-stable',
        'display_name': 'Aurora — stable',
        'description': 'Aurora main-branch image build (published as the :stable tag).',
        'repo': 'ublue-os/aurora',
        'workflow_path': '.github/workflows/build_ublue.yml',
    },
    {
        'id': 'aurora-testing',
        'display_name': 'Aurora — testing',
        'description': 'Aurora testing-branch image build (published as the :testing tag).',
        'repo': 'ublue-os/aurora',
        'workflow_path': '.github/workflows/build_ublue.yml',
    },
    {
        'id': 'bazzite-stable',
        'display_name': 'Bazzite — stable',
        'description': 'Bazzite main-branch image build (published as the stable release tag).',
        'repo': 'ublue-os/bazzite',
        'workflow_path': '.github/workflows/build_ublue.yml',
    },
    {
        'id': 'bazzite-testing',
        'display_name': 'Bazzite — testing',
        'description': 'Bazzite testing-branch image build (published as the :testing tag).',
        'repo': 'ublue-os/bazzite',
        'workflow_path': '.github/workflows/build_ublue.yml',
    },
]


def build_builds_matrix(root: Path, collected_at: str) -> dict:
    factory_stats_path = root / 'docs/data/factory-stats.json'
    factory_stats = load_json(factory_stats_path) if factory_stats_path.exists() else {}
    image_builds = factory_stats.get('image_builds') or {}
    build_history = factory_stats.get('build_history') or {}

    rows = []
    for entry in BUILD_PIPELINE_CATALOG:
        is_argo = entry.get('source') == 'argo'
        if is_argo:
            argo_key = entry.get('argo_pipeline_key') or entry['id']
            history = build_history.get(argo_key) or []
        else:
            history = image_builds.get(entry['id']) or []
        history_points = [
            {
                'id': run.get('id'),
                'overall': run.get('overall'),
                'started_at': run.get('started_at'),
                'finished_at': run.get('finished_at'),
                'duration_min': run.get('duration_min'),
                'run_url': run.get('run_url'),
                'branch': run.get('branch'),
            }
            for run in history
        ]
        last_run = history_points[-1] if history_points else None
        completed = [p for p in history_points if p['overall'] in ('passed', 'fail')]
        passed = [p for p in completed if p['overall'] == 'passed']
        success_rate = round((len(passed) / len(completed)) * 100, 1) if completed else None
        durations = [p['duration_min'] for p in completed if p['duration_min'] is not None]
        avg_duration_min = round(sum(durations) / len(durations), 1) if durations else None

        if last_run:
            state = 'available'
            state_reason = None
        else:
            state = 'unavailable'
            if is_argo:
                state_reason = (
                    'No completed Argo workflow run has been published yet for this pipeline, '
                    'or the cluster API was unreachable on the last collector run.'
                )
            else:
                state_reason = (
                    'No GitHub Actions run has been published yet for this workflow/branch '
                    'combination, or the GitHub API request failed on the last collector run.'
                )

        if is_argo:
            ci_url = last_run.get('run_url') if last_run else 'http://192.168.1.102:32746/workflows/argo'
            source_url = repo_blob_url(entry['workflow_path'])
            derivation = (
                f"Join BUILD_PIPELINE_CATALOG with docs/data/factory-stats.json "
                f"build_history['{entry.get('argo_pipeline_key', entry['id'])}'], "
                f"populated by live Argo workflow scraping against the cluster."
            )
        else:
            ci_url = last_run.get('run_url') if last_run else f"https://github.com/{entry['repo']}/actions/workflows/{Path(entry['workflow_path']).name}"
            source_url = f"https://github.com/{entry['repo']}/blob/main/{entry['workflow_path']}"
            derivation = (
                "Join BUILD_PIPELINE_CATALOG with docs/data/factory-stats.json "
                f"image_builds['{entry['id']}'], populated by a live GitHub Actions "
                f"workflow-runs query against {entry['repo']}."
            )

        rows.append(
            {
                'id': entry['id'],
                'display_name': entry['display_name'],
                'description': entry['description'],
                'source': entry.get('source', 'github-actions'),
                'last_run': last_run,
                'history_points': history_points,
                'success_rate': success_rate,
                'avg_duration_min': avg_duration_min,
                'runs_tracked': len(history_points),
                'ci_url': ci_url,
                'state': state,
                'state_reason': state_reason,
                'source_url': source_url,
                'collected_at': collected_at,
                'derivation': derivation,
            }
        )

    available_rows = [row for row in rows if row['state'] == 'available']
    unavailable_rows = [row for row in rows if row['state'] != 'available']
    total_runs_tracked = sum(row['runs_tracked'] for row in rows)

    return {
        'schema_version': 'v1',
        '_meta': {
            'page': 'builds',
            'description': 'Collector-derived contract for the OS image Builds page.',
            'generated_at': collected_at,
            'starter_artifact': False,
            'status': 'partial' if unavailable_rows else 'ready',
        },
        'summary_metrics': [
            {
                'id': 'build_pipelines_tracked',
                'label': 'Build pipelines tracked',
                'value': len(rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('scripts/generate_page_datasets.py'),
                'collected_at': collected_at,
                'derivation': 'Count entries in BUILD_PIPELINE_CATALOG.',
            },
            {
                'id': 'build_pipelines_with_history',
                'label': 'Pipelines with published run history',
                'value': len(available_rows),
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/factory-stats.json'),
                'collected_at': collected_at,
                'derivation': "Count rows whose factory-stats.json image_builds entry is non-empty.",
            },
            {
                'id': 'build_runs_tracked',
                'label': 'Runs in tracked history',
                'value': total_runs_tracked,
                'unit': 'count',
                'state': 'available',
                'state_reason': None,
                'source_url': repo_blob_url('docs/data/factory-stats.json'),
                'collected_at': collected_at,
                'derivation': 'Sum of history_points length across all rows (capped at 20 runs per pipeline).',
            },
        ],
        'rows': rows,
    }


def write_page_datasets(root: Path, collected_at: str) -> dict[str, dict]:
    data_dir = root / 'docs/data'
    datasets = {
        'upstream-status.json': build_upstream_status(root, collected_at),
        'tests-matrix.json': build_tests_matrix(root, collected_at),
        'builds-matrix.json': build_builds_matrix(root, collected_at),
        'applications-matrix.json': build_applications_matrix(root, collected_at),
        'homebrew-ecosystem.json': build_homebrew_ecosystem(root, collected_at),
        'adoption-metrics.json': build_adoption_metrics(root, collected_at),
    }
    for name, payload in datasets.items():
        (data_dir / name).write_text(json.dumps(payload, indent=2) + '\n')
    return datasets


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate page-owned dashboard datasets.')
    parser.add_argument('--root', default='.', help='Repository root')
    parser.add_argument('--collected-at', default=None, help='ISO8601 timestamp override')
    args = parser.parse_args()

    root = Path(args.root).resolve()
    collected_at = args.collected_at or now_utc_iso()
    warn_if_surface_drifted_from_testsuite(root)
    
    # Run GitOps dashboard collector scripts (same dir, so importable directly)
    import refresh_gitops_stats, collect_app_resources, check_gitops_policy, refresh_factory_stats
    for collector in (refresh_gitops_stats, collect_app_resources, check_gitops_policy, refresh_factory_stats):
        try:
            collector.main()
        except Exception as exc:  # match old subprocess check=False behavior
            print(f"warning: {collector.__name__} failed: {exc}", file=sys.stderr)


    write_page_datasets(root, collected_at)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
