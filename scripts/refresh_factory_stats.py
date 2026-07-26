import json, glob, datetime, sys, subprocess, re, os, hashlib, math
from pathlib import Path


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def append_build_runs_ndjson(stats, image_build_catalog, now):
    """Append terminal build-run records to the rolling NDJSON history file.

    Keeps one row per (plane, run_id) and prunes entries older than 180 days.
    Running/pending runs are skipped and appended once they reach a terminal
    state on a later collector pass.
    """
    history_dir = Path('docs/data/history')
    history_dir.mkdir(parents=True, exist_ok=True)
    ndjson_path = history_dir / 'build-runs.ndjson'
    retention_days = 180
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=retention_days)

    def normalize_status(status):
        if status == 'passed':
            return 'passed'
        if status in ('fail', 'failed'):
            return 'failed'
        return None

    repo_by_lane = {entry['id']: entry['repo'] for entry in image_build_catalog}

    existing_keys = set()
    kept_lines = []
    if ndjson_path.exists():
        with open(ndjson_path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                recorded = parse_iso(rec.get('recorded_at'))
                if recorded and recorded < cutoff:
                    continue
                key = (rec.get('plane'), rec.get('run_id'))
                existing_keys.add(key)
                kept_lines.append(line)

    new_records = []

    # Publish plane: upstream GitHub Actions image builds.
    for lane, runs in (stats.get('image_builds') or {}).items():
        repo = repo_by_lane.get(lane)
        if not repo:
            continue
        for run in runs:
            run_id = str(run.get('id') or '')
            if not run_id:
                continue
            status = normalize_status(run.get('overall'))
            if not status:
                continue
            duration_min = run.get('duration_min')
            if not isinstance(duration_min, (int, float)) or not math.isfinite(duration_min):
                continue
            key = ('publish', run_id)
            if key in existing_keys:
                continue
            new_records.append({
                'recorded_at': now,
                'plane': 'publish',
                'repo': repo,
                'lane': lane,
                'run_id': run_id,
                'status': status,
                'started_at': run.get('started_at'),
                'finished_at': run.get('finished_at'),
                'duration_min': duration_min,
                'run_url': run.get('run_url'),
                'failure_stage': None,
            })

    # Lab plane: Argo Workflows build pipelines on the homelab.
    for lane, runs in (stats.get('build_history') or {}).items():
        for run in runs:
            run_id = str(run.get('id') or '')
            if not run_id:
                continue
            status = normalize_status(run.get('overall'))
            if not status:
                continue
            duration_min = run.get('duration_min')
            if not isinstance(duration_min, (int, float)) or not math.isfinite(duration_min):
                continue
            key = ('lab', run_id)
            if key in existing_keys:
                continue
            new_records.append({
                'recorded_at': now,
                'plane': 'lab',
                'repo': 'projectbluefin/lab',
                'lane': lane,
                'run_id': run_id,
                'status': status,
                'started_at': run.get('started_at'),
                'finished_at': run.get('finished_at'),
                'duration_min': duration_min,
                'run_url': run.get('run_url'),
                'failure_stage': None,
            })

    if new_records:
        with open(ndjson_path, 'w') as handle:
            for line in kept_lines:
                handle.write(line + '\n')
            for rec in new_records:
                handle.write(json.dumps(rec, separators=(',', ':')) + '\n')
    elif kept_lines:
        # Retention-only pass: rewrite the pruned file even if nothing new arrived.
        with open(ndjson_path, 'w') as handle:
            for line in kept_lines:
                handle.write(line + '\n')


def main():
    issue_count = int(sys.argv[1]) if sys.argv[1:] and sys.argv[1].isdigit() else 0
    pr_count    = int(sys.argv[2]) if sys.argv[2:] and sys.argv[2].isdigit() else 0
    merged_7d   = int(sys.argv[3]) if sys.argv[3:] and sys.argv[3].isdigit() else 0

    stats_path = 'docs/data/factory-stats.json'
    telemetry_path = 'docs/data/factory-telemetry.json'
    with open(stats_path) as f:
        stats = json.load(f)

    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def run_json(cmd):
        try:
            out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL, timeout=15)
            return json.loads(out)
        except Exception:
            return None

    def sha256_file(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"

    def argo_ui_url(workflow_name):
        # Argo Workflows run on the cluster, not GitHub Actions; link straight to
        # the Argo Server UI for the actual workflow instance.
        return f"http://192.168.1.102:32746/workflows/argo/{workflow_name}"

    def phase_to_overall(phase):
        if phase in ('Running', 'Pending'):
            return 'running'
        if phase == 'Succeeded':
            return 'passed'
        if phase in ('Failed', 'Error'):
            return 'fail'
        return 'pending'

    def infer_trigger(name):
        if not name:
            return 'manual'
        if name.startswith('nightly-'):
            return 'nightly'
        if name.startswith('image-poll-') or name.startswith('digest-watch-'):
            return 'poller'
        if re.match(r'^[a-z]+-\d+-', name):
            return 'pr-poller'
        return 'manual'

    # Pipelines that produce a build artifact (image, containerdisk, kernel), as
    # opposed to maintenance/polling CronWorkflows (orphan-*-gc, image-poll-*, etc).
    BUILD_PIPELINE_PREFIXES = (
        'bluefin-qa-pipeline',
        'dakota-qa-pipeline',
        'knuckle-qa-pipeline',
        'bst-qa-pipeline',
        'build-containerdisk',
        'build-cd-sync',
        'flatcar-kernel-build',
        'bluefin-server-build-pipeline',
        'dakota-build-pipeline',
        'cosmic-build-pipeline',
        'cosmic-qa-pipeline',
    )

    def pipeline_base_name(name):
        # Argo generateName workflows append a random 5-char suffix
        # (build-containerdisk-dsrlm); CronWorkflow instances append an epoch
        # timestamp (orphan-pod-gc-1783047600). Strip either to get a stable key.
        stripped = re.sub(r'-[a-z0-9]{5}$', '', name)
        stripped = re.sub(r'-\d{9,}$', '', stripped)
        return stripped

    def build_pipeline_key(name):
        base = pipeline_base_name(name)
        for prefix in BUILD_PIPELINE_PREFIXES:
            if base == prefix or base.startswith(f'{prefix}-'):
                return prefix
        return None

    def infer_label(params, wf_name):
        p = {x.get('name'): x.get('value') for x in (params or [])}
        variant = p.get('variant')
        tag = p.get('image-tag')
        image = p.get('image', '')
        if variant and tag:
            return f'{variant}:{tag}'
        if image and tag:
            img_name = image.rsplit('/', 1)[-1]
            return f'{img_name}:{tag}'
        if image:
            return image.rsplit('/', 1)[-1]
        if wf_name.startswith('dakota'):
            return 'dakota:latest'
        return None

    def safe_int(val, default=0):
        try:
            return int(val)
        except Exception:
            return default

    def parse_mem_gib(mem):
        if not mem:
            return None
        if mem.endswith('Ki'):
            return round(int(mem[:-2]) / (1024 * 1024))
        if mem.endswith('Mi'):
            return round(int(mem[:-2]) / 1024)
        if mem.endswith('Gi'):
            return int(mem[:-2])
        return None

    def parse_mem_bytes(mem):
        if not mem:
            return None
        try:
            if mem.endswith('Ki'):
                return int(mem[:-2]) * 1024
            if mem.endswith('Mi'):
                return int(mem[:-2]) * 1024 * 1024
            if mem.endswith('Gi'):
                return int(mem[:-2]) * 1024 * 1024 * 1024
            if mem.endswith('Ti'):
                return int(mem[:-2]) * 1024 * 1024 * 1024 * 1024
            return int(mem)
        except Exception:
            return None

    def parse_cpu_millicores(cpu):
        if cpu is None:
            return None
        s = str(cpu)
        try:
            if s.endswith('n'):
                return float(s[:-1]) / 1_000_000.0
            if s.endswith('u'):
                return float(s[:-1]) / 1000.0
            if s.endswith('m'):
                return float(s[:-1])
            return float(s) * 1000.0
        except Exception:
            return None

    def summarize_image_status(recent_runs):
        image_map = {
            'bluefin': {
                'repo': 'projectbluefin/bluefin',
                'stable_selector': {'tag_regex': r'^stable-'},
                'testing_selector': {'tag_regex': r'^testing-'},
                'ghcr_package': 'bluefin',
            },
            'bluefin-lts': {
                'repo': 'projectbluefin/bluefin-lts',
                'stable_selector': {'tag_regex': r'^stable-'},
                'testing_selector': {'tag_regex': r'^testing-'},
                'ghcr_package': 'bluefin-lts',
            },
            'dakota': {
                'repo': 'projectbluefin/dakota',
                'stable_selector': {'tag_regex': r'^stable-'},
                'testing_selector': {'tag_regex': r'^testing-'},
                'ghcr_package': 'dakota',
            },
            'aurora': {
                'repo': 'ublue-os/aurora',
                'stable_selector': {'tag_regex': r'^stable-'},
                'testing_selector': {'tag_regex': r'^testing-'},
            },
            'bazzite': {
                'repo': 'ublue-os/bazzite',
                'stable_selector': {'tag_regex': r'^[0-9]'},
                'testing_selector': {'tag_regex': r'^testing-'},
            },
            'snosi': {
                'repo': 'frostyard/snow',
                'stable_selector': {'tag_regex': r'^stable-'},
                'testing_selector': {'tag_regex': r'^latest-'},
                'ghcr_package': 'snow',
            },
        }

        now_dt = datetime.datetime.now(datetime.timezone.utc)

        def release_match(release, selector):
            if not selector or not isinstance(release, dict):
                return False
            if release.get('draft'):
                return False
            tag_name = release.get('tag_name') or ''
            if not re.search(selector.get('tag_regex', r'.*'), tag_name, re.IGNORECASE):
                return False
            prerelease = selector.get('prerelease')
            if prerelease is None:
                return True
            return bool(release.get('prerelease')) == bool(prerelease)

        def first_release(releases, selector):
            for rel in (releases or []):
                if release_match(rel, selector):
                    return rel
            return None

        def ghcr_tag_snapshot(package_name, tag_name):
            if not package_name or not tag_name:
                return None
            versions = run_json(f"gh api orgs/projectbluefin/packages/container/{package_name}/versions?per_page=100")
            if not isinstance(versions, list):
                return None
            for version in versions:
                tags = (((version.get('metadata') or {}).get('container') or {}).get('tags') or [])
                if tag_name in tags:
                    return {
                        'seen_at': version.get('updated_at'),
                        'source_url': version.get('html_url'),
                        'tag': tag_name,
                    }
            return None

        def fallback_run_labels(image, branch):
            labels = [f'{image}:{branch}']
            if image == 'dakota' and branch == 'testing':
                labels.append('dakota:latest')
            if image == 'bluefin' and branch == 'testing':
                labels.append('bluefin:latest')
            if image == 'bluefin-lts' and branch == 'testing':
                labels.append('bluefin-lts:latest')
            return labels

        def fallback_seen_from_runs(image, branch):
            if not isinstance(recent_runs, list):
                return None
            labels = set(fallback_run_labels(image, branch))
            for run in recent_runs:
                if run.get('overall') != 'passed':
                    continue
                if run.get('label') not in labels:
                    continue
                seen_at = run.get('finished_at') or run.get('started_at')
                if seen_at:
                    return {
                        'seen_at': seen_at,
                        'run_url': run.get('run_url'),
                        'run_id': run.get('id'),
                    }
            return None

        summary = {}
        for image, cfg in image_map.items():
            repo = cfg['repo']
            releases = run_json(f"gh api repos/{repo}/releases?per_page=100")
            if not isinstance(releases, list):
                releases = []
            stable_rel = first_release(releases, cfg.get('stable_selector'))
            testing_rel = first_release(releases, cfg.get('testing_selector'))

            stable_seen_at = stable_rel.get('published_at') if stable_rel else None
            testing_seen_at = testing_rel.get('published_at') if testing_rel else None
            stable_fallback = None
            testing_fallback = None
            if not stable_seen_at:
                stable_fallback = fallback_seen_from_runs(image, 'stable')
                stable_seen_at = stable_fallback.get('seen_at') if stable_fallback else None
            if not testing_seen_at:
                testing_fallback = fallback_seen_from_runs(image, 'testing')
                testing_seen_at = testing_fallback.get('seen_at') if testing_fallback else None
            stable_ghcr = ghcr_tag_snapshot(cfg.get('ghcr_package'), 'stable')
            testing_ghcr = ghcr_tag_snapshot(cfg.get('ghcr_package'), 'testing')
            if stable_ghcr and stable_ghcr.get('seen_at'):
                stable_seen_at = stable_ghcr['seen_at']
            if testing_ghcr and testing_ghcr.get('seen_at'):
                testing_seen_at = testing_ghcr['seen_at']
            stable_dt = parse_iso(stable_seen_at)
            testing_dt = parse_iso(testing_seen_at)

            summary[image] = {
                'repo': repo,
                'source': 'github-releases',
                'releases_api': f"https://api.github.com/repos/{repo}/releases",
                'stable_seen_at': stable_seen_at,
                'stable_age_days': max(0, int((now_dt - stable_dt).total_seconds() // 86400)) if stable_dt else None,
                'stable_tag': (stable_ghcr.get('tag') if stable_ghcr else None) or (stable_rel.get('tag_name') if stable_rel else None) or (stable_fallback.get('run_id') if stable_fallback else None),
                'stable_source_url': (stable_ghcr.get('source_url') if stable_ghcr else None) or (stable_rel.get('html_url') if stable_rel else None) or (stable_fallback.get('run_url') if stable_fallback else None),
                'testing_seen_at': testing_seen_at,
                'testing_age_days': max(0, int((now_dt - testing_dt).total_seconds() // 86400)) if testing_dt else None,
                'testing_tag': (testing_ghcr.get('tag') if testing_ghcr else None) or (testing_rel.get('tag_name') if testing_rel else None) or (testing_fallback.get('run_id') if testing_fallback else None),
                'testing_source_url': (testing_ghcr.get('source_url') if testing_ghcr else None) or (testing_rel.get('html_url') if testing_rel else None) or (testing_fallback.get('run_url') if testing_fallback else None),
            }
        return summary

    # --- Open bugs ---
    raw = []
    if Path('/tmp/bugs-raw.json').exists():
        try:
            with open('/tmp/bugs-raw.json') as f:
                raw = json.load(f)
        except Exception as e:
            print(f"Warning loading /tmp/bugs-raw.json: {e}")
    else:
        # Fallback to gh CLI if available
        try:
            gh_out = run_cmd("gh issue list --repo projectbluefin/lab --state open --json number,title,url,createdAt --limit 50")
            if gh_out:
                raw = json.loads(gh_out)
        except Exception:
            pass

    existing = {b['number']: b for b in stats.get('open_bugs', [])}
    bugs = []
    for i in raw:
        prev = existing.get(i['number'], {})
        bugs.append({
            'number':     i['number'],
            'title':      i['title'],
            'url':        i['url'],
            'created_at': i['createdAt'],
            'affects':    prev.get('affects', []),
            'area':       prev.get('area', 'unknown'),
        })
    stats['open_bugs'] = bugs


    # --- Aggregate test coverage from result files ---
    total_scenarios = 0
    total_failed = 0
    coverage_by_suite = {}
    images_with_results = set()
    newest_result_ts = None
    for path in glob.glob('docs/results/*.json'):
        try:
            with open(path) as f:
                d = json.load(f)
            if d.get('status') not in ('pending', None) and d.get('last_run'):
                s = d.get('suite', 'unknown')
                v = d.get('variant', 'unknown')
                total_scenarios += d.get('scenarios', 0)
                total_failed    += d.get('failed', 0)
                images_with_results.add(v)
                if s not in coverage_by_suite:
                    coverage_by_suite[s] = {'images': 0, 'scenarios': 0, 'failed': 0}
                coverage_by_suite[s]['images']    += 1
                coverage_by_suite[s]['scenarios'] += d.get('scenarios', 0)
                coverage_by_suite[s]['failed']    += d.get('failed', 0)
                ts = d.get('last_run')
                if ts and (newest_result_ts is None or ts > newest_result_ts):
                    newest_result_ts = ts
        except Exception:
            pass
    cov = stats.setdefault('test_coverage', {})
    if total_scenarios > 0:
        cov['scenarios_total']  = total_scenarios
        cov['scenarios_failed'] = total_failed
    cov['images_with_results']  = len(images_with_results)
    cov['coverage_by_suite']    = coverage_by_suite

    # --- GitHub counts ---
    stats.setdefault('github', {}).setdefault('testing_lab', {})
    stats['github']['testing_lab']['open_issues'] = issue_count
    stats['github']['testing_lab']['open_prs']    = pr_count
    if merged_7d > 0:
        stats['github']['testing_lab']['prs_merged_7d'] = merged_7d

    # --- Hive/org-wide counts (source: GitHub search API) ---
    hive_open_issues = run_json("gh api search/issues -f q='org:projectbluefin is:issue is:open' --jq .total_count")
    hive_open_prs = run_json("gh api search/issues -f q='org:projectbluefin is:pr is:open' --jq .total_count")
    if isinstance(hive_open_issues, int) and isinstance(hive_open_prs, int):
        stats.setdefault('hive', {})
        stats['hive']['open_issues'] = hive_open_issues
        stats['hive']['open_prs'] = hive_open_prs
        stats['hive']['source'] = 'github-search-api'
    else:
        stats.pop('hive', None)

    # --- Optional live Argo snapshot ---
    argo = run_json("curl -k -sS --max-time 12 https://192.168.1.102:32746/api/v1/workflows/argo")
    recent_runs = []
    build_history = {}
    newest_live_ts = None
    live_runs_ok = False
    runs_all_time = 0
    runs_7d = 0
    seven_days_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
    if argo and isinstance(argo.get('items'), list):
        for wf in argo['items']:
            md = wf.get('metadata', {})
            st = wf.get('status', {})
            spec = wf.get('spec', {})
            name = md.get('name')
            started = st.get('startedAt') or md.get('creationTimestamp')
            finished = st.get('finishedAt')
            if not started:
                continue
            params = ((spec.get('arguments') or {}).get('parameters') or [])
            phase = st.get('phase')
            overall = phase_to_overall(phase)
            duration_min = None
            try:
                sdt = datetime.datetime.fromisoformat(started.replace('Z', '+00:00'))
                edt = datetime.datetime.fromisoformat((finished or now).replace('Z', '+00:00'))
                duration_min = max(0, round((edt - sdt).total_seconds() / 60))
            except Exception:
                pass
            label = infer_label(params, name)
            run = {
                'id': name,
                'overall': overall,
                'label': label,
                'started_at': started,
                'finished_at': finished,
                'duration_min': duration_min,
                'trigger': infer_trigger(name),
                'run_url': argo_ui_url(name),
            }
            recent_runs.append(run)
            runs_all_time += 1
            started_dt = parse_iso(started)
            if started_dt and started_dt >= seven_days_ago:
                runs_7d += 1
            for ts in (started, finished):
                if ts and (newest_live_ts is None or ts > newest_live_ts):
                    newest_live_ts = ts

            pipeline_key = build_pipeline_key(name)
            if pipeline_key:
                build_history.setdefault(pipeline_key, []).append(run)

        recent_runs.sort(key=lambda r: r.get('started_at') or '', reverse=True)
        stats['recent_runs'] = recent_runs[:25]
        pipelines = stats.setdefault('pipelines', {})
        containerdisk = pipelines.setdefault('containerdisk', {})
        containerdisk['runs_7d'] = runs_7d
        containerdisk['runs_all_time'] = runs_all_time
        live_runs_ok = True

        # Retain up to the last 20 runs per build pipeline, oldest first, so
        # the Builds page can render a real sparkline instead of one flat
        # cross-pipeline list.
        for key, runs in build_history.items():
            runs.sort(key=lambda r: r.get('started_at') or '')
            build_history[key] = runs[-20:]
        stats['build_history'] = build_history
    else:
        stats.setdefault('recent_runs', [])
        stats.setdefault('build_history', {})

    # --- Real OS image build history (GitHub Actions, public API, no cluster needed) ---
    # These are the actual bootc image build pipelines for bluefin/bluefin-lts/dakota -
    # the thing users mean by "factory builds". Runs on ubuntu-latest fine: this is a
    # plain GitHub REST API call, not the LAN-only Argo/k8s API.
    IMAGE_BUILD_CATALOG = [
        {'id': 'bluefin-stable', 'repo': 'projectbluefin/bluefin', 'workflow': 'build-image-testing.yml', 'branch': 'main'},
        {'id': 'bluefin-testing', 'repo': 'projectbluefin/bluefin', 'workflow': 'build-image-testing.yml', 'branch': 'testing'},
        {'id': 'bluefin-next', 'repo': 'projectbluefin/bluefin', 'workflow': 'build-image-next.yml', 'branch': None},
        {'id': 'bluefin-lts-stable', 'repo': 'projectbluefin/bluefin-lts', 'workflow': 'build-regular.yml', 'branch': 'main'},
        {'id': 'bluefin-lts-testing', 'repo': 'projectbluefin/bluefin-lts', 'workflow': 'build-regular.yml', 'branch': 'testing'},
        {'id': 'bluefin-lts-hwe', 'repo': 'projectbluefin/bluefin-lts', 'workflow': 'build-regular-hwe.yml', 'branch': None},
        {'id': 'bluefin-lts-nvidia', 'repo': 'projectbluefin/bluefin-lts', 'workflow': 'build-nvidia.yml', 'branch': None},
        {'id': 'dakota', 'repo': 'projectbluefin/dakota', 'workflow': 'build.yml', 'branch': 'testing'},
        {'id': 'dakota-aarch64', 'repo': 'projectbluefin/dakota', 'workflow': 'build-aarch64.yml', 'branch': 'testing'},
        {'id': 'snosi-latest', 'repo': 'frostyard/snow', 'workflow': 'build.yml', 'branch': 'latest'},
        {'id': 'aurora-stable', 'repo': 'ublue-os/aurora', 'workflow': 'build_ublue.yml', 'branch': 'main'},
        {'id': 'aurora-testing', 'repo': 'ublue-os/aurora', 'workflow': 'build_ublue.yml', 'branch': 'testing'},
        {'id': 'bazzite-stable', 'repo': 'ublue-os/bazzite', 'workflow': 'build_ublue.yml', 'branch': 'main'},
        {'id': 'bazzite-testing', 'repo': 'ublue-os/bazzite', 'workflow': 'build_ublue.yml', 'branch': 'testing'},
    ]

    def gh_run_to_overall(run):
        status = run.get('status')
        conclusion = run.get('conclusion')
        if status != 'completed':
            return 'running'
        if conclusion == 'success':
            return 'passed'
        if conclusion in ('failure', 'timed_out', 'startup_failure'):
            return 'fail'
        if conclusion == 'cancelled':
            return 'pending'
        return 'pending'

    def gh_run_duration_min(run):
        try:
            started = datetime.datetime.fromisoformat(run['run_started_at'].replace('Z', '+00:00'))
            updated = datetime.datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
            return max(0, round((updated - started).total_seconds() / 60))
        except Exception:
            return None

    image_builds = {}
    for entry in IMAGE_BUILD_CATALOG:
        query = f"repos/{entry['repo']}/actions/workflows/{entry['workflow']}/runs?per_page=20"
        if entry['branch']:
            query += f"&branch={entry['branch']}"
        doc = run_json(f"gh api '{query}'")
        runs = []
        if doc and isinstance(doc.get('workflow_runs'), list):
            for run in doc['workflow_runs']:
                runs.append({
                    'id': str(run.get('id')),
                    'overall': gh_run_to_overall(run),
                    'started_at': run.get('run_started_at') or run.get('created_at'),
                    'finished_at': run.get('updated_at') if run.get('status') == 'completed' else None,
                    'duration_min': gh_run_duration_min(run) if run.get('status') == 'completed' else None,
                    'run_url': run.get('html_url'),
                    'branch': run.get('head_branch'),
                })
            runs.sort(key=lambda r: r.get('started_at') or '')
            image_builds[entry['id']] = runs[-20:]
        else:
            # Preserve whatever was already committed rather than clobbering it
            # with an empty list on a transient GitHub API failure.
            existing = stats.get('image_builds', {}).get(entry['id'])
            if existing:
                image_builds[entry['id']] = existing
    stats['image_builds'] = image_builds

    # --- Append terminal build runs to rolling NDJSON history ---
    append_build_runs_ndjson(stats, IMAGE_BUILD_CATALOG, now)

    # --- Optional live cluster node snapshot ---
    node_doc = run_json("kubectl get nodes -o json --request-timeout=8s")
    metrics_doc = run_json("kubectl get --raw '/apis/metrics.k8s.io/v1beta1/nodes'")
    live_nodes_ok = False
    if node_doc and isinstance(node_doc.get('items'), list):
        existing_nodes = {}
        try:
            existing_nodes = {n.get('name'): n for n in (((stats.get('factory') or {}).get('cluster') or {}).get('nodes') or []) if n.get('name')}
        except Exception:
            existing_nodes = {}
        usage_by_node = {}
        seen_nodes = set()
        if metrics_doc and isinstance(metrics_doc.get('items'), list):
            for item in metrics_doc.get('items', []):
                md = item.get('metadata') or {}
                usage = item.get('usage') or {}
                usage_by_node[md.get('name')] = {
                    'cpu_m': parse_cpu_millicores(usage.get('cpu')),
                    'mem_b': parse_mem_bytes(usage.get('memory')),
                }
        nodes = []
        total_ram = 0
        for n in node_doc['items']:
            md = n.get('metadata', {})
            st = n.get('status') or {}
            cap = (n.get('status') or {}).get('capacity') or {}
            conds = (n.get('status') or {}).get('conditions') or []
            ready = next((c.get('status') == 'True' for c in conds if c.get('type') == 'Ready'), False)
            labels = md.get('labels') or {}
            role = 'worker'
            if labels.get('node-role.kubernetes.io/control-plane') is not None or labels.get('node-role.kubernetes.io/master') is not None:
                role = 'control-plane'
            ram_gb = parse_mem_gib(cap.get('memory'))
            if ram_gb:
                total_ram += ram_gb
            node_name = md.get('name')
            usage = usage_by_node.get(node_name, {})
            cap_cpu_m = parse_cpu_millicores(cap.get('cpu'))
            cap_mem_b = parse_mem_bytes(cap.get('memory'))
            cpu_pct = None
            mem_pct = None
            if usage.get('cpu_m') is not None and cap_cpu_m and cap_cpu_m > 0:
                cpu_pct = round((usage.get('cpu_m') / cap_cpu_m) * 100, 1)
            if usage.get('mem_b') is not None and cap_mem_b and cap_mem_b > 0:
                mem_pct = round((usage.get('mem_b') / cap_mem_b) * 100, 1)
            seen_nodes.add(node_name)
            prev = existing_nodes.get(node_name, {})
            load_hist = list(prev.get('load_1m_history') or [])
            if cpu_pct is not None:
                load_hist.append(cpu_pct)
            load_hist = [round(float(v), 1) for v in load_hist if isinstance(v, (int, float, str)) and str(v).strip() != ''][-12:]
            nodes.append({
                'name': node_name,
                'status': 'ready' if ready else 'not-ready',
                'role': role,
                'ram_gb': ram_gb,
                'cpu_threads': safe_int(cap.get('cpu')),
                'os_image': ((st.get('nodeInfo') or {}).get('osImage')),
                'kernel_version': ((st.get('nodeInfo') or {}).get('kernelVersion')),
                'kubelet_version': ((st.get('nodeInfo') or {}).get('kubeletVersion')),
                'cpu_usage_pct': cpu_pct,
                'mem_usage_pct': mem_pct,
                'load_1m_history': load_hist,
            })
        for node_name, prev in existing_nodes.items():
            if node_name in seen_nodes:
                continue
            nodes.append({
                'name': node_name,
                'status': 'not-ready',
                'role': prev.get('role', 'worker'),
                'ram_gb': prev.get('ram_gb'),
                'cpu_threads': prev.get('cpu_threads'),
                'os_image': prev.get('os_image'),
                'kernel_version': prev.get('kernel_version'),
                'kubelet_version': prev.get('kubelet_version'),
                'cpu_usage_pct': prev.get('cpu_usage_pct'),
                'mem_usage_pct': prev.get('mem_usage_pct'),
                'load_1m_history': list(prev.get('load_1m_history') or []),
            })
        factory = stats.setdefault('factory', {})
        cluster = factory.setdefault('cluster', {})
        cluster['nodes'] = nodes
        cluster['total_ram_gb'] = total_ram
        live_nodes_ok = True
    else:
        factory = stats.setdefault('factory', {})
        cluster = factory.setdefault('cluster', {})
        cluster.setdefault('nodes', [])

    factory = stats.setdefault('factory', {})
    factory['images'] = summarize_image_status(stats.get('recent_runs', []))

    meta = stats.setdefault('_meta', {})
    data_points = [x for x in [newest_live_ts, newest_result_ts, meta.get('generated')] if x]
    meta['generated'] = max(data_points) if data_points else now
    meta['refreshed_at'] = now
    meta['live_snapshot_ok'] = bool(live_runs_ok and live_nodes_ok)
    source_ts = [parse_iso(ts) for ts in [newest_live_ts, newest_result_ts] if ts]
    source_ts = [ts for ts in source_ts if ts is not None]
    freshness_threshold = 180
    freshness_minutes = int(round((datetime.datetime.now(datetime.timezone.utc) - max(source_ts)).total_seconds() / 60)) if source_ts else None
    freshness_state = 'unknown'
    if freshness_minutes is not None:
        freshness_state = 'fresh' if freshness_minutes <= freshness_threshold else 'stale'
    meta['freshness_minutes'] = freshness_minutes
    meta['freshness'] = {
        'age_minutes': freshness_minutes,
        'threshold_minutes': freshness_threshold,
        'state': freshness_state,
    }
    meta['source'] = 'github-actions/update-test-results'

    with open(stats_path, 'w') as f:
        json.dump(stats, f, indent=2)

    # --- Write dashboard history rollups ---
    recent_runs = stats.get('recent_runs', [])
    by_day = {}
    for run in recent_runs:
        started = run.get('started_at')
        if not started:
            continue
        day = started[:10]
        bucket = by_day.setdefault(day, {'date': day, 'throughput': 0, 'passed': 0, 'running': 0, 'failed': 0, 'durations': []})
        bucket['throughput'] += 1
        bucket['passed'] += 1 if run.get('overall') == 'passed' else 0
        bucket['running'] += 1 if run.get('overall') == 'running' else 0
        bucket['failed'] += 1 if run.get('overall') == 'fail' else 0
        if run.get('overall') == 'passed' and isinstance(run.get('duration_min'), (int, float)):
            bucket['durations'].append(run['duration_min'])
    rollups = []
    for day in sorted(by_day):
        bucket = by_day[day]
        throughput = bucket['throughput']
        reliability = round((bucket['passed'] / throughput) * 100) if throughput else 0
        speed_min = round(sorted(bucket['durations'])[len(bucket['durations']) // 2]) if bucket['durations'] else 0
        pressure = bucket['running'] + bucket['failed']
        rollups.append({
            'date': day,
            'throughput': throughput,
            'reliability': reliability,
            'speed_min': speed_min,
            'pressure': pressure,
        })

    history_path = 'docs/data/factory-history.json'
    with open(history_path, 'w') as f:
        json.dump({
            'window_days': 7,
            'generated_from': 'factory-stats.json',
            'rollups': rollups[-7:],
        }, f, indent=2)

    # --- Public telemetry contract for dashboard evidence ---
    expected_paths = []
    test_surface = run_json("cat docs/data/test-surface.json")
    if isinstance(test_surface, dict):
        for cell in test_surface.get('surface', []):
            rp = cell.get('results_path')
            if isinstance(rp, str) and rp.startswith('results/'):
                expected_paths.append(f"docs/{rp}")
    expected_paths = sorted(set(expected_paths))
    observed_paths = [p for p in expected_paths if Path(p).exists()]
    missing_docs = [p.replace('docs/', '', 1) for p in expected_paths if not Path(p).exists()]
    expected_result_docs = len(expected_paths)
    observed_result_docs = len(observed_paths)
    coverage_ratio = round((observed_result_docs / expected_result_docs), 4) if expected_result_docs else 0.0

    snapshot_state = freshness_state
    if freshness_state == 'unknown':
        snapshot_state = 'unknown'
    elif not meta.get('live_snapshot_ok'):
        snapshot_state = 'degraded'

    collector_run_id = os.environ.get('GITHUB_RUN_ID')
    collector_url = f"https://github.com/projectbluefin/lab/actions/runs/{collector_run_id}" if collector_run_id else "https://github.com/projectbluefin/lab/actions"
    collector_sha = os.environ.get('GITHUB_SHA') or ''
    collector_commit_url = f"https://github.com/projectbluefin/lab/commit/{collector_sha}" if collector_sha else "https://github.com/projectbluefin/lab/commits/main"

    inputs = []
    for p in sorted(Path('docs/results').glob('*.json')):
        try:
            inputs.append({'path': str(p), 'sha256': sha256_file(p)})
        except Exception:
            continue

    recent_runs = stats.get('recent_runs', [])
    completed_runs = [r for r in recent_runs if r.get('overall') in ('passed', 'fail')]
    passed_runs = [r for r in completed_runs if r.get('overall') == 'passed']
    running_now = len([r for r in recent_runs if r.get('overall') == 'running'])
    scenarios_total = stats.get('test_coverage', {}).get('scenarios_total', 0)
    scenarios_failed = stats.get('test_coverage', {}).get('scenarios_failed', 0)
    metric_evidence = [{'kind': 'collector-run', 'url': collector_url}]
    if collector_sha:
        metric_evidence.append({'kind': 'commit', 'url': collector_commit_url})

    telemetry = {
        'schema_version': 'v2',
        'snapshot': {
            'generated_at': meta['generated'],
            'window': {'type': 'rolling', 'hours': 24},
            'state': snapshot_state,
            'age_minutes': meta['freshness']['age_minutes'],
            'threshold_minutes': meta['freshness']['threshold_minutes'],
        },
        'lineage': {
            'collector': {
                'workflow': '.github/workflows/update-test-results.yml',
                'run_id': collector_run_id,
                'run_url': collector_url,
                'job': 'update',
                'commit_sha': collector_sha,
                'commit_url': collector_commit_url,
            },
            'inputs_digest_sha256': f"sha256:{hashlib.sha256(''.join([i['sha256'] for i in inputs]).encode()).hexdigest()}",
            'inputs': inputs,
        },
        'coverage': {
            'expected_result_docs': expected_result_docs,
            'observed_result_docs': observed_result_docs,
            'coverage_ratio': coverage_ratio,
            'missing_docs': missing_docs,
        },
        'metrics': [
            {
                'id': 'suite_pass_rate_24h',
                'label': 'Suite pass rate (24h)',
                'value': round((len(passed_runs) / len(completed_runs)) * 100, 2) if completed_runs else None,
                'unit': 'percent',
                'numerator': len(passed_runs),
                'denominator': len(completed_runs),
                'formula': 'passed_suite_runs / completed_suite_runs * 100',
                'window_hours': 24,
                'confidence': 'high' if completed_runs else 'low',
                'state': snapshot_state if completed_runs else 'unknown',
                'evidence': metric_evidence,
            },
            {
                'id': 'scenario_pass_rate_24h',
                'label': 'Scenario pass rate (24h)',
                'value': round(((scenarios_total - scenarios_failed) / scenarios_total) * 100, 2) if scenarios_total else None,
                'unit': 'percent',
                'numerator': max(0, scenarios_total - scenarios_failed),
                'denominator': scenarios_total,
                'formula': '(scenarios_total - scenarios_failed) / scenarios_total * 100',
                'window_hours': 24,
                'confidence': 'medium' if scenarios_total else 'low',
                'state': snapshot_state if scenarios_total else 'unknown',
                'evidence': metric_evidence,
            },
            {
                'id': 'queue_pressure_now',
                'label': 'Queue pressure (now)',
                'value': running_now,
                'unit': 'count',
                'numerator': running_now,
                'denominator': max(1, len(recent_runs)),
                'formula': 'count(runs where overall=running)',
                'window_hours': 1,
                'confidence': 'high',
                'state': snapshot_state,
                'evidence': metric_evidence,
            },
        ],
        'errors': [] if snapshot_state in ('fresh', 'stale') else [
            {
                'source': 'live_cluster_snapshot',
                'reason': 'private endpoint unavailable or incomplete from GitHub-hosted runner',
                'effect': 'public telemetry downgraded to unknown/degraded',
            }
        ],
    }

    with open(telemetry_path, 'w') as f:
        json.dump(telemetry, f, indent=2)

    bugs_count = len(bugs)
    print(f"Updated {stats_path}: {bugs_count} open bugs, {total_scenarios} scenarios, {len(images_with_results)} images with results")

if __name__ == '__main__':
    main()
