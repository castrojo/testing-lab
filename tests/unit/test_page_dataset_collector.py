from copy import deepcopy
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/generate_page_datasets.py'
LEGACY_INPUT_FIXTURE = ROOT / 'tests/unit/fixtures/page-dataset-legacy-inputs.json'


def load_module():
    spec = importlib.util.spec_from_file_location('generate_page_datasets', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def use_pinned_legacy_inputs(monkeypatch, module):
    """Keep legacy result assertions independent of workflow refreshes."""
    fixture = json.loads(LEGACY_INPUT_FIXTURE.read_text())
    monkeypatch.setattr(module, 'iter_surface_cells', lambda root: iter(fixture['surface']))
    monkeypatch.setattr(module, 'load_enrollment_issues', lambda root: fixture['enrollment'])
    monkeypatch.setattr(module, 'load_results_by_relative_path', lambda root: fixture['results'])
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: [])


def test_upstream_dataset_derives_required_families(monkeypatch):
    module = load_module()

    # Monkeypatch load_json to make release lanes available deterministically.
    orig_load_json = module.load_json
    def mock_load_json(path):
        if Path(path).name == 'factory-stats.json':
            data = orig_load_json(path)
            # Restore the 8 lanes to their expected available timestamps for test determinism
            if 'factory' in data and 'images' in data['factory']:
                images = data['factory']['images']
                for variant in ['bluefin', 'bluefin-lts', 'dakota', 'aurora', 'bazzite']:
                    if variant in images:
                        images[variant]['stable_seen_at'] = '2026-06-28T16:10:04Z'
                        images[variant]['testing_seen_at'] = '2026-06-28T16:10:04Z'
                        images[variant]['stable_age_days'] = 1
                        images[variant]['testing_age_days'] = 1
            return data
        return orig_load_json(path)

    monkeypatch.setattr(module, 'load_json', mock_load_json)

    dataset = module.build_upstream_status(ROOT, '2026-06-29T19:22:22Z')

    assert dataset['schema_version'] == 'v1'
    assert dataset['_meta']['page'] == 'upstream'
    assert dataset['_meta']['starter_artifact'] is False
    assert {group['id'] for group in dataset['groups']} == {
        'gnome-os',
        'fedora-bootc',
        'projectbluefin',
        'ublue',
        'cosmic',
    }

    rows = {row['id']: row for row in dataset['rows']}
    assert rows['bluefin-testing']['state'] == 'available'
    assert rows['bluefin-testing']['published_at'] == '2026-06-28T16:10:04Z'
    assert rows['dakota-testing']['state'] == 'available'
    assert rows['gnomeos-nightly']['state'] == 'unavailable'
    assert rows['fedora-bootc-stable']['source_url'].endswith(
        '/manifests/image-poll-fedora-bootc-latest.yaml'
    )

    metrics = {metric['id']: metric for metric in dataset['summary_metrics']}
    # variant-publishers.json now defines 9 image families; upstream rows include
    # non-image lanes (gnomeos, fedora-bootc) so totals drift with the catalog.
    assert metrics['tracked_upstream_lanes']['value'] == 15
    assert metrics['lanes_with_release_data']['value'] == 9
    assert metrics['lanes_without_release_data']['value'] == 6
    assert all(metric['collected_at'] == '2026-06-29T19:22:22Z' for metric in dataset['summary_metrics'])


def test_tests_matrix_derives_rows_from_surface_and_results(monkeypatch):
    module = load_module()
    use_pinned_legacy_inputs(monkeypatch, module)

    dataset = module.build_tests_matrix(ROOT, '2026-06-29T19:22:22Z')

    assert dataset['schema_version'] == 'v3'
    assert dataset['_meta']['page'] == 'tests'
    assert dataset['_meta']['starter_artifact'] is False
    # The pinned surface fixture mirrors the published matrix shape at the
    # baseline; live QA refreshes must not change this contract test.
    assert len(dataset['rows']) == 64

    metrics = {metric['id']: metric for metric in dataset['summary_metrics']}
    assert metrics['published_matrix_rows']['value'] == 64
    assert metrics['rows_with_completed_runs']['value'] == 14
    assert metrics['rows_waiting_for_results']['value'] == 50

    rows = {row['id']: row for row in dataset['rows']}
    assert rows['bluefin-testing-developer']['state'] == 'available'
    assert rows['bluefin-testing-developer']['pass_rate'] == 100.0
    assert rows['bluefin-testing-developer']['history_points'] == 5
    assert rows['bluefin-testing-smoke']['state'] == 'available'
    assert rows['bluefin-testing-smoke']['pass_rate'] == 87.59
    assert rows['bluefin-lts-testing-developer']['state'] == 'available'
    assert rows['dakota-testing-smoke']['screenshot_url'].endswith(
        '/screenshots/dakota-testing-smoke-latest.png'
    )
    # Variants actually enrolled in the test surface must appear; fabricated
    # entries must not.
    assert {row['variant'] for row in dataset['rows']} == {
        'bluefin',
        'bluefin-lts',
        'dakota',
        'flatcar',
        'aurora',
        'bazzite',
        'cosmic',
        'fedora-bootc',
        'fedora-kinoite',
        'fedora-silverblue',
        'gnomeos',
        'snosi',
    }


def test_tests_matrix_derives_branch_safe_qa_run_lifecycle_evidence(monkeypatch):
    module = load_module()
    fixture = ROOT / 'tests/unit/fixtures/qa-run-derivation.ndjson'
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: records)

    dataset = module.build_tests_matrix(ROOT, '2026-08-01T12:00:00Z')
    rows = {row['id']: row for row in dataset['rows']}

    running = rows['bluefin-testing-smoke']
    assert running['evidence_source'] == 'qa-run-v1'
    assert running['evidence_state'] == 'running'
    assert running['started_at'] == '2026-08-01T11:01:00Z'
    assert running['finished_at'] is None
    assert running['digest'] == 'sha256:' + 'b' * 64
    assert running['screenshot_url'] is None
    assert running['screenshot_evidence']['state'] == 'not_applicable'

    # The exact branch key prevents testing evidence from bleeding into stable.
    stable = rows['bluefin-stable-smoke']
    assert stable['evidence_state'] == 'passed'
    assert stable['digest'] == 'sha256:' + 'c' * 64

    early_infra_failure = rows['bluefin-testing-system']
    assert early_infra_failure['evidence_state'] == 'failed'
    assert early_infra_failure['started_at'] == '2026-08-01T10:56:00Z'
    assert early_infra_failure['finished_at'] == '2026-08-01T11:01:00Z'
    assert early_infra_failure['results_evidence']['state'] == 'unavailable'
    assert early_infra_failure['scenarios_total'] is None
    assert early_infra_failure['scenarios_failed'] is None


def test_tests_matrix_marks_stale_at_the_documented_boundary_and_derives_signals(monkeypatch):
    module = load_module()
    fixture = ROOT / 'tests/unit/fixtures/qa-run-derivation.ndjson'
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: records)

    dataset = module.build_tests_matrix(ROOT, '2026-08-01T12:00:00Z')
    rows = {row['id']: row for row in dataset['rows']}

    # Exactly 24 hours is fresh; one second over is stale.
    assert rows['bluefin-testing-developer']['evidence_state'] == 'passed'
    stale = rows['bluefin-testing-software']
    assert stale['evidence_state'] == 'stale'
    assert '24-hour freshness threshold' in stale['evidence_state_reason']

    persistent = rows['dakota-testing-smoke']
    assert persistent['evidence_state'] == 'failed'
    assert persistent['terminal_failure_streak'] == 3
    assert persistent['runs_recorded'] == 3
    assert persistent['repeated_failed_scenarios'] == [
        {'scenario': 'Shell opens', 'failed_runs': 3}
    ]

    flaky = rows['bluefin-lts-testing-smoke']
    assert flaky['flake_flips'] == 2
    assert flaky['runs_recorded'] == 3
    assert flaky['run_history'][-1]['state'] == 'passed'


def test_tests_matrix_keeps_qa_evidence_public_and_does_not_invent_screenshots(monkeypatch):
    module = load_module()
    fixture = ROOT / 'tests/unit/fixtures/qa-run-derivation.ndjson'
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: records)

    dataset = module.build_tests_matrix(ROOT, '2026-08-01T12:00:00Z')
    canonical_rows = [
        row for row in dataset['rows'] if row['evidence_source'] == 'qa-run-v1'
    ]

    assert canonical_rows
    for row in canonical_rows:
        assert row['source_url'].startswith('https://github.com/projectbluefin/lab/')
        assert all(
            history['source_url'] is None
            or history['source_url'].startswith('https://github.com/projectbluefin/lab/')
            for history in row['run_history']
        )

    running = next(row for row in canonical_rows if row['id'] == 'bluefin-testing-smoke')
    assert running['results_evidence']['state'] == 'unavailable'
    assert running['screenshot_evidence'] == {
        'state': 'not_applicable',
        'state_reason': 'No screenshot runner.',
    }


def test_public_evidence_url_allows_only_static_public_destinations():
    module = load_module()

    assert module.public_evidence_url(
        'https://github.com/projectbluefin/lab/blob/main/docs/data/history/qa-runs.ndjson'
    )
    assert module.public_evidence_url(
        'https://projectbluefin.github.io/lab/screenshots/bluefin-testing-smoke-latest.png'
    )
    for value in (
        'http://github.com/projectbluefin/lab',
        'https://localhost/evidence',
        'https://127.0.0.1/evidence',
        'https://10.0.0.1/evidence',
        'https://172.20.0.1/evidence',
        'https://192.168.1.1/evidence',
        'https://169.254.1.1/evidence',
        'https://[::1]/evidence',
        'https://[fd00::1]/evidence',
        'https://argo-server.argo.svc.cluster.local/evidence',
        'https://github.com@127.0.0.1/evidence',
        'https://github.com.evil.example/evidence',
    ):
        assert module.public_evidence_url(value) is None


def test_tests_matrix_rejects_unsafe_legacy_and_enrollment_urls(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: [])
    results = module.load_results_by_relative_path(ROOT)
    legacy_path = 'results/bluefin-testing-smoke.json'
    results[legacy_path] = {
        **results[legacy_path],
        'source_url': 'https://127.0.0.1/private-result',
    }
    monkeypatch.setattr(module, 'load_results_by_relative_path', lambda root: results)
    monkeypatch.setattr(
        module,
        'load_enrollment_issues',
        lambda root: {'aurora': {'issue_url': 'https://argo-server.argo.svc.cluster.local/issues/278'}},
    )

    dataset = module.build_tests_matrix(ROOT, '2026-08-01T12:00:00Z')
    rows = {row['id']: row for row in dataset['rows']}

    assert rows['bluefin-testing-smoke']['source_url'] == (
        'https://github.com/projectbluefin/lab/blob/main/docs/results/bluefin-testing-smoke.json'
    )
    assert rows['aurora-testing-smoke']['enrollment_issue_url'] is None
    assert rows['aurora-testing-smoke']['source_url'] == (
        'https://github.com/projectbluefin/lab/blob/main/docs/data/enrollment-issues.json'
    )


def test_normalize_result_source_url_preserves_allowed_github_and_pages_urls():
    module = load_module()

    github_url = 'https://github.com/projectbluefin/lab/blob/main/docs/results/bluefin-testing-smoke.json'
    pages_url = 'https://projectbluefin.github.io/lab/screenshots/bluefin-testing-smoke-latest.png'

    assert module.normalize_result_source_url('results/bluefin-testing-smoke.json', {'source_url': github_url}) == github_url
    assert module.normalize_result_source_url('results/bluefin-testing-smoke.json', {'source_url': pages_url}) == pages_url


def test_completed_metric_excludes_running_and_stale_qa_evidence(monkeypatch):
    module = load_module()
    fixture = ROOT / 'tests/unit/fixtures/qa-run-derivation.ndjson'
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: records)

    dataset = module.build_tests_matrix(ROOT, '2026-08-01T12:00:00Z')
    rows = {row['id']: row for row in dataset['rows']}
    completed = {
        row['id'] for row in dataset['rows']
        if row['evidence_source'] == 'qa-run-v1'
        and row['evidence_state'] in ('passed', 'failed')
    }
    metrics = {metric['id']: metric for metric in dataset['summary_metrics']}

    assert rows['bluefin-testing-smoke']['evidence_state'] == 'running'
    assert rows['bluefin-testing-software']['evidence_state'] == 'stale'
    assert 'bluefin-testing-smoke' not in completed
    assert 'bluefin-testing-software' not in completed
    assert metrics['rows_with_completed_runs']['value'] == sum(
        1
        for row in dataset['rows']
        if (
            row['evidence_source'] == 'qa-run-v1'
            and row['evidence_state'] in ('passed', 'failed')
        ) or (
            row['evidence_source'] == 'legacy-results-v1'
            and row['last_run']
            and row['result_status'] in ('passed', 'failed')
        )
    )


def test_tests_matrix_v3_schema_validates_legacy_and_canonical_projections(monkeypatch):
    module = load_module()
    fixture = ROOT / 'tests/unit/fixtures/qa-run-derivation.ndjson'
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: records)

    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / 'schemas/v2/tests-matrix.schema.json').read_text())
    dataset = module.build_tests_matrix(ROOT, '2026-08-01T12:00:00Z')
    errors = list(Draft202012Validator(schema).iter_errors(dataset))
    assert not errors, '; '.join(error.message for error in errors)


def test_tests_matrix_v3_schema_rejects_incomplete_page_contract(monkeypatch):
    module = load_module()
    fixture = ROOT / 'tests/unit/fixtures/qa-run-derivation.ndjson'
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    monkeypatch.setattr(module, 'load_qa_run_records', lambda root: records)

    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / 'schemas/v2/tests-matrix.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    dataset = module.build_tests_matrix(ROOT, '2026-08-01T12:00:00Z')

    invalid_payloads = []

    missing_metadata = deepcopy(dataset)
    del missing_metadata['_meta']['generated_at']
    invalid_payloads.append(missing_metadata)

    missing_metric_provenance = deepcopy(dataset)
    del missing_metric_provenance['summary_metrics'][0]['source_url']
    invalid_payloads.append(missing_metric_provenance)

    empty_dimensions = deepcopy(dataset)
    empty_dimensions['dimensions']['suites'] = []
    invalid_payloads.append(empty_dimensions)

    missing_legacy_row_field = deepcopy(dataset)
    del missing_legacy_row_field['rows'][0]['result_status']
    invalid_payloads.append(missing_legacy_row_field)

    missing_canonical_row_field = deepcopy(dataset)
    del missing_canonical_row_field['rows'][0]['evidence_state']
    invalid_payloads.append(missing_canonical_row_field)

    public_urls = deepcopy(dataset)
    public_urls['rows'][0]['source_url'] = (
        'https://github.com/projectbluefin/lab/blob/main/docs/data/tests-matrix.json'
    )
    public_urls['rows'][0]['screenshot_url'] = (
        'https://projectbluefin.github.io/lab/screenshots/bluefin-testing-smoke-latest.png'
    )
    assert not list(validator.iter_errors(public_urls))

    for unsafe_url in (
        'https://localhost/evidence',
        'https://127.0.0.1/evidence',
        'https://10.0.0.1/evidence',
        'https://172.20.0.1/evidence',
        'https://192.168.1.1/evidence',
        'https://169.254.1.1/evidence',
        'https://[::1]/evidence',
        'https://[fd00::1]/evidence',
        'https://argo-server.argo.svc.cluster.local/evidence',
    ):
        private_url = deepcopy(dataset)
        private_url['rows'][0]['source_url'] = unsafe_url
        invalid_payloads.append(private_url)

    for payload in invalid_payloads:
        assert list(validator.iter_errors(payload))


def test_applications_matrix_keeps_bazaar_fallbacks_explicit(monkeypatch):
    module = load_module()
    use_pinned_legacy_inputs(monkeypatch, module)

    dataset = module.build_applications_matrix(ROOT, '2026-06-29T19:22:22Z')

    assert dataset['schema_version'] == 'v1'
    assert dataset['_meta']['page'] == 'applications'
    assert dataset['_meta']['starter_artifact'] is False
    assert [app['id'] for app in dataset['applications']] == ['bazaar', 'firefox']

    metrics = {metric['id']: metric for metric in dataset['summary_metrics']}
    assert metrics['tracked_applications']['value'] == 2
    assert metrics['application_rows']['value'] == 8
    assert metrics['rows_with_primary_app_results']['value'] == 6
    assert metrics['rows_with_fallback_signals']['value'] == 2

    rows = {row['id']: row for row in dataset['rows']}
    bluefin = rows['bazaar-bluefin-testing']
    assert bluefin['state'] == 'available'
    assert bluefin['fallback_signal_count'] == 1
    assert len(bluefin['fallback_signals']) == 1
    assert bluefin['fallback_signals'][0]['state'] == 'unavailable'
    # Scenario order is inherited from the result file, not part of the
    # fallback contract; preserve each upstream name and its casing.
    assert sorted(bluefin['fallback_signals'][0]['matched_scenarios']) == sorted([
        'Bazaar flatpak preinstall file is present',
        'bazaar user service is available',
    ])
    assert rows['bazaar-dakota-testing']['fallback_signal_count'] == 1
    # Real enrolled variants from test-surface software cells must appear;
    # fabricated variants must not.
    assert {row['variant'] for row in dataset['rows']} == {
        'bluefin',
        'bluefin-lts',
        'dakota',
        'aurora',
    }
    assert rows['firefox-bluefin-testing']['state'] == 'available'
    assert rows['firefox-bluefin-testing']['fallback_signal_count'] == 0


def test_homebrew_ecosystem_derives_all_tracked_lanes():
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    assert dataset['schema_version'] == 'v1'
    assert dataset['_meta']['page'] == 'homebrew'
    assert dataset['_meta']['starter_artifact'] is False

    row_ids = {row['id'] for row in dataset['rows']}
    # All 10 lanes from variant-publishers.json must appear
    assert 'bluefin-testing' in row_ids
    assert 'bluefin-stable' in row_ids
    assert 'bluefin-lts-testing' in row_ids
    assert 'bluefin-lts-stable' in row_ids
    assert 'aurora-testing' in row_ids
    assert 'aurora-stable' in row_ids
    assert 'bazzite-testing' in row_ids
    assert 'bazzite-stable' in row_ids
    assert 'dakota-testing' in row_ids
    assert 'flatcar-testing' in row_ids

    metrics = {m['id']: m for m in dataset['summary_metrics']}
    assert metrics['tracked_image_lanes']['value'] >= 10
    assert metrics['lanes_with_brew_data']['value'] == 6
    assert metrics['lanes_awaiting_brew_data']['value'] == 8


def test_homebrew_ecosystem_non_bluefin_rows_are_unavailable_without_brew_data():
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    tapped_variants = {
        row['variant']
        for row in dataset['rows']
        if row['tap_name']
    }
    for row in dataset['rows']:
        if row['variant'] in tapped_variants:
            assert row['state'] == 'available', f"Expected available for {row['id']}"
            assert row['package_count'] is not None
            assert row['install_count'] is not None
            assert row['download_count'] is not None
        else:
            assert row['state'] == 'unavailable', f"Expected unavailable for {row['id']}"
            assert row['state_reason'], f"Missing state_reason for {row['id']}"
            assert row['package_count'] is None
            assert row['install_count'] is None
            assert row['download_count'] is None
        assert row['source_url'], f"Missing source_url for {row['id']}"
        assert row['collected_at'] == '2026-06-29T19:22:22Z'
        assert row['derivation'], f"Missing derivation for {row['id']}"


def test_homebrew_ecosystem_metrics_have_full_provenance():
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    for metric in dataset['summary_metrics']:
        assert metric['source_url'], f"Missing source_url on metric {metric['id']}"
        assert metric['collected_at'] == '2026-06-29T19:22:22Z'
        assert metric['derivation'], f"Missing derivation on metric {metric['id']}"
        assert metric['state'] in {'available', 'unavailable'}


def test_homebrew_ecosystem_awaiting_metric_source_url_is_variant_publishers():
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    metrics = {m['id']: m for m in dataset['summary_metrics']}
    awaiting = metrics['lanes_awaiting_brew_data']
    assert awaiting['source_url'].endswith('/docs/data/variant-publishers.json'), (
        f"Expected variant-publishers.json source_url, got: {awaiting['source_url']}"
    )


def test_homebrew_ecosystem_populates_bluefin_family_from_migrated_source():
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')
    rows = {row['id']: row for row in dataset['rows']}

    assert rows['bluefin-testing']['state'] == 'available'
    assert rows['bluefin-testing']['tap_name'] == 'bluefin/brewfile'
    assert rows['bluefin-testing']['install_count'] > 0
    assert rows['bluefin-testing']['download_count'] > 0

    assert rows['bluefin-lts-stable']['state'] == 'available'
    assert rows['aurora-testing']['state'] == 'unavailable'
    assert rows['flatcar-testing']['state'] == 'unavailable'


def test_homebrew_ecosystem_exposes_transplanted_tap_catalog():
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')
    taps = {tap['name']: tap for tap in dataset['taps']}
    metrics = {metric['id']: metric for metric in dataset['summary_metrics']}

    assert 'bluefin/brewfile' in taps
    assert taps['bluefin/brewfile']['url'] == 'https://github.com/projectbluefin/common'
    assert taps['bluefin/brewfile']['package_count'] == 115
    assert taps['bluefin/brewfile']['variant_scope'] == ['bluefin', 'bluefin-lts']
    assert taps['bluefin/brewfile']['top_packages'][0]['name'] == 'gh'
    assert 'bazzite/brewfile' in taps
    assert taps['bazzite/brewfile']['package_count'] == 20
    assert metrics['lanes_with_brew_data']['value'] == 6
    assert metrics['lanes_awaiting_brew_data']['value'] == 8


def test_homebrew_ecosystem_includes_package_density_structures():
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    assert 'package_leaderboard' in dataset
    assert dataset['package_leaderboard'], 'Expected package leaderboard entries'
    first = dataset['package_leaderboard'][0]
    assert first['tap_name'] == 'bluefin/brewfile'
    assert first['package_count'] == 115
    assert first['install_count'] == 4207114
    assert first['download_count'] == 1566064


def test_homebrew_ecosystem_maps_multiple_taps_by_variant_scope(monkeypatch):
    module = load_module()

    orig_load_optional_json = module.load_optional_json
    fixture = {
        'taps': [
            {
                'name': 'bluefin/brewfile',
                'url': 'https://github.com/projectbluefin/common',
                'description': 'bluefin tap',
                'variant_scope': ['bluefin', 'bluefin-lts'],
                'packages': [
                    {'name': 'gh', 'type': 'formula', 'installs_90d': 10, 'downloads': 5},
                ],
            },
            {
                'name': 'bazzite/brewfile',
                'url': 'https://github.com/ublue-os/bazzite',
                'description': 'bazzite tap',
                'variant_scope': ['bazzite'],
                'packages': [
                    {'name': 'steam', 'type': 'cask', 'installs_90d': 8, 'downloads': 3},
                    {'name': 'mangohud', 'type': 'formula', 'installs_90d': 2, 'downloads': 1},
                ],
            },
        ],
    }

    def mock_load_optional_json(path):
        if Path(path).name == 'homebrew-package-stats-migrated.json':
            return fixture
        return orig_load_optional_json(path)

    monkeypatch.setattr(module, 'load_optional_json', mock_load_optional_json)

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')
    rows = {row['id']: row for row in dataset['rows']}
    metrics = {metric['id']: metric for metric in dataset['summary_metrics']}
    taps = {tap['name']: tap for tap in dataset['taps']}

    assert rows['bluefin-testing']['tap_name'] == 'bluefin/brewfile'
    assert rows['bluefin-testing']['package_count'] == 1
    assert rows['bazzite-testing']['tap_name'] == 'bazzite/brewfile'
    assert rows['bazzite-testing']['state'] == 'available'
    assert rows['bazzite-testing']['package_count'] == 2
    assert rows['bazzite-stable']['state'] == 'available'
    assert rows['bazzite-testing']['install_count'] == 10
    assert rows['bazzite-testing']['download_count'] == 4
    assert metrics['lanes_with_brew_data']['value'] == 6
    assert metrics['lanes_awaiting_brew_data']['value'] == 8
    assert taps['bazzite/brewfile']['package_type_counts'] == {'cask': 1, 'formula': 1}


def test_adoption_metrics_derives_all_tracked_lanes():
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    assert dataset['schema_version'] == 'v1'
    assert dataset['_meta']['page'] == 'adoption'
    assert dataset['_meta']['starter_artifact'] is False

    row_ids = {row['id'] for row in dataset['rows']}
    assert 'bluefin-testing' in row_ids
    assert 'bluefin-stable' in row_ids
    assert 'bluefin-lts-testing' in row_ids
    assert 'aurora-testing' in row_ids
    assert 'bazzite-testing' in row_ids
    assert 'dakota-testing' in row_ids
    assert 'flatcar-testing' in row_ids
    assert 'cosmic-testing' in row_ids or 'cosmic-stable' in row_ids
    assert 'snosi-latest' in row_ids

    metrics = {m['id']: m for m in dataset['summary_metrics']}
    assert metrics['tracked_image_lanes']['value'] >= 10
    assert metrics['lanes_with_pull_data']['value'] == 0
    assert metrics['lanes_with_countme_data']['value'] == 6


def test_adoption_metrics_populates_countme_for_in_scope_variants():
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')
    rows = {row['id']: row for row in dataset['rows']}
    metrics = {metric['id']: metric for metric in dataset['summary_metrics']}

    assert rows['bluefin-testing']['state'] == 'available'
    assert rows['bluefin-testing']['countme_active_devices'] == 3502
    assert rows['bluefin-stable']['countme_active_devices'] == 3502
    assert rows['aurora-testing']['countme_active_devices'] == 2527
    assert rows['bazzite-stable']['countme_active_devices'] == 71550

    assert rows['dakota-testing']['state'] == 'unavailable'
    assert rows['flatcar-testing']['state'] == 'unavailable'
    assert metrics['lanes_with_countme_data']['value'] == 6
    assert metrics['lanes_with_pull_data']['value'] == 0


def test_adoption_metrics_has_trust_cards_from_publishers():
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    trust_cards = {card['variant']: card for card in dataset['trust_cards']}
    # All 6 tracked variants must have a trust card
    assert set(trust_cards.keys()) == {
        'bluefin',
        'bluefin-lts',
        'aurora',
        'bazzite',
        'dakota',
        'flatcar',
        'cosmic',
        'snosi',
        'zirconium',
    }

    bluefin_card = trust_cards['bluefin']
    assert bluefin_card['emits_sbom'] is False
    assert bluefin_card['emits_cosign_attestation'] is False
    assert bluefin_card['emits_cve_scan'] is False
    # Trust card available only when publisher_repo is known
    assert bluefin_card['state'] == 'available'
    assert bluefin_card['source_url']
    assert bluefin_card['collected_at'] == '2026-06-29T19:22:22Z'
    assert bluefin_card['derivation']


def test_adoption_trust_card_unavailable_when_publisher_unknown():
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    trust_cards = {card['variant']: card for card in dataset['trust_cards']}
    # flatcar has null publisher_repo and null org in variant-publishers.json
    flatcar_card = trust_cards['flatcar']
    assert flatcar_card['state'] == 'unavailable', (
        "Trust card for flatcar must be unavailable when publisher_repo is unknown"
    )
    assert flatcar_card['state_reason'], "flatcar trust card must have explicit state_reason"


def test_adoption_metrics_pull_count_always_null():
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    for row in dataset['rows']:
        assert row['pull_count'] is None, f"Expected null pull_count for {row['id']}"
        assert row['source_url'], f"Missing source_url for {row['id']}"
        assert row['collected_at'] == '2026-06-29T19:22:22Z'
        assert row['derivation'], f"Missing derivation for {row['id']}"
        if row['state'] == 'unavailable':
            assert row['state_reason'], f"Missing state_reason for unavailable row {row['id']}"


def test_adoption_metrics_metrics_have_full_provenance():
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    for metric in dataset['summary_metrics']:
        assert metric['source_url'], f"Missing source_url on metric {metric['id']}"
        assert metric['collected_at'] == '2026-06-29T19:22:22Z'
        assert metric['derivation'], f"Missing derivation on metric {metric['id']}"
        assert metric['state'] in {'available', 'unavailable'}


def test_homebrew_ecosystem_names_authoritative_upstream_sources():
    """Derivation and state_reason must name formulae.brew.sh for unavailable rows."""
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    for row in dataset['rows']:
        if row['state'] == 'unavailable':
            assert 'formulae.brew.sh' in row['state_reason'], (
                f"state_reason for {row['id']} must name formulae.brew.sh as the authoritative source"
            )
            assert 'formulae.brew.sh' in row['derivation'], (
                f"derivation for {row['id']} must name formulae.brew.sh"
            )

    metrics = {m['id']: m for m in dataset['summary_metrics']}
    assert 'formulae.brew.sh' in metrics['lanes_with_brew_data']['derivation']
    assert 'formulae.brew.sh' in metrics['lanes_awaiting_brew_data']['derivation']


def test_adoption_metrics_names_authoritative_upstream_sources():
    """Derivation and state_reason must name GHCR/registry API and Fedora countme infrastructure."""
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    for row in dataset['rows']:
        derivation = row['derivation']
        if row['state'] == 'unavailable':
            reason = row['state_reason']
            assert 'GHCR' in reason or 'registry' in reason.lower(), (
                f"state_reason for {row['id']} must name a registry API (GHCR) as pull_count source"
            )
            assert 'countme' in reason.lower(), (
                f"state_reason for {row['id']} must name Fedora countme as countme_active_devices source"
            )
            assert 'GHCR' in derivation or 'registry' in derivation.lower(), (
                f"derivation for {row['id']} must name a registry API"
            )
            assert 'countme' in derivation.lower(), (
                f"derivation for {row['id']} must name countme data"
            )
        else:
            assert 'countme' in derivation.lower(), (
                f"derivation for available row {row['id']} must name countme data source"
            )

    metrics = {m['id']: m for m in dataset['summary_metrics']}
    assert 'GHCR' in metrics['lanes_with_pull_data']['derivation'] or \
           'registry' in metrics['lanes_with_pull_data']['derivation'].lower()
    assert 'countme' in metrics['lanes_with_countme_data']['derivation'].lower()


# --- Provenance fix tests (TDD: added to drive the source_url fix) ---

REPO_HB_MIGRATED_URL = (
    'https://github.com/projectbluefin/lab/blob/main/'
    'docs/data/homebrew-package-stats-migrated.json'
)
REPO_AC_MIGRATED_URL = (
    'https://github.com/projectbluefin/lab/blob/main/'
    'docs/data/adoption-countme-migrated.json'
)
OLD_UPSTREAM_SLUG = 'castrojo/bootc-ecosystem'


def test_homebrew_available_rows_use_repo_owned_source_url():
    """Available Homebrew rows must point to the repo-owned migrated artifact, not castrojo/bootc-ecosystem."""
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    for row in dataset['rows']:
        if row['state'] == 'available':
            assert row['source_url'] == REPO_HB_MIGRATED_URL, (
                f"Row {row['id']} source_url must be the repo-owned migrated artifact, "
                f"got: {row['source_url']}"
            )
            assert OLD_UPSTREAM_SLUG not in row['source_url'], (
                f"Row {row['id']} source_url must not point to {OLD_UPSTREAM_SLUG}"
            )


def test_homebrew_tap_entries_use_repo_owned_source_url():
    """Tap entries transplanted from migrated artifact must use repo-owned source_url."""
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    assert dataset['taps'], "Expected at least one tap entry"
    for tap in dataset['taps']:
        assert tap['source_url'] == REPO_HB_MIGRATED_URL, (
            f"Tap {tap['name']} source_url must be the repo-owned migrated artifact, "
            f"got: {tap['source_url']}"
        )
        assert OLD_UPSTREAM_SLUG not in tap['source_url'], (
            f"Tap {tap['name']} source_url must not point to {OLD_UPSTREAM_SLUG}"
        )


def test_adoption_available_rows_use_repo_owned_source_url():
    """Available Adoption rows must point to the repo-owned migrated artifact, not castrojo/bootc-ecosystem."""
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    for row in dataset['rows']:
        if row['state'] == 'available':
            assert row['source_url'] == REPO_AC_MIGRATED_URL, (
                f"Row {row['id']} source_url must be the repo-owned migrated artifact, "
                f"got: {row['source_url']}"
            )
            assert OLD_UPSTREAM_SLUG not in row['source_url'], (
                f"Row {row['id']} source_url must not point to {OLD_UPSTREAM_SLUG}"
            )


# --- Disclosure tests (TDD: drive explicit provenance text for transplanted signals) ---

def test_homebrew_available_rows_disclose_global_analytics_and_package_count():
    """Available Homebrew rows must state they are global formula analytics and name the transplanted package count."""
    module = load_module()

    dataset = module.build_homebrew_ecosystem(ROOT, '2026-06-29T19:22:22Z')

    available = [r for r in dataset['rows'] if r['state'] == 'available']
    assert available, "Expected at least one available Homebrew row"

    for row in available:
        derivation = row['derivation']
        assert 'global' in derivation.lower(), (
            f"Row {row['id']} derivation must disclose 'global' formula analytics; got: {derivation}"
        )
        assert str(row['package_count']) in derivation, (
            f"Row {row['id']} derivation must name the transplanted package count ({row['package_count']}); got: {derivation}"
        )
        assert 'Bluefin-family' not in derivation, (
            f"Row {row['id']} derivation must avoid Bluefin-family-only wording; got: {derivation}"
        )


def test_adoption_available_rows_disclose_snapshot_week_window():
    """Available Adoption rows must include the countme snapshot week window in their derivation."""
    module = load_module()

    dataset = module.build_adoption_metrics(ROOT, '2026-06-29T19:22:22Z')

    available = [r for r in dataset['rows'] if r['state'] == 'available']
    assert available, "Expected at least one available Adoption row"

    for row in available:
        derivation = row['derivation']
        # The snapshot week dates from adoption-countme-migrated.json must appear
        assert '2026-03-16' in derivation or '2026-03-22' in derivation, (
            f"Row {row['id']} derivation must include the snapshot week window dates; got: {derivation}"
        )
