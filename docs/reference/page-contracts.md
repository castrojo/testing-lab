# Page-oriented dashboard data contracts

These contracts split the factory dashboard into page-owned JSON files so each deep page can load only the data it needs.

## Shared rules

All files follow the same starter pattern:

- `schema_version`: contract version for the file.
- `_meta`: artifact-level metadata (`page`, `description`, `generated_at`, `starter_artifact`, `status`).
- `summary_metrics[]`: page headline metrics. Every metric row must include `source_url`, `collected_at`, and `derivation`.
- `rows[]`: the primary page records. Every row must include:
  - `source_url`: canonical evidence link for the row.
  - `collected_at`: when this JSON row was assembled.
  - `derivation`: how the row was computed from source inputs.
  - `state`: `available` or `unavailable`.
  - `state_reason`: explicit reason when the row cannot support runtime claims yet.
- Placeholder values use `null` plus `state: "unavailable"`; collectors must never invent values.

## `docs/data/upstream-status.json`

Purpose: one row per tracked upstream stream for both `/upstream` (non-Bluefin families) and `/bluefin` (Bluefin, Bluefin-LTS, Dakota) pages.

### Top-level shape

- `_meta`
- `summary_metrics[]`
- `groups[]`: logical families shown in the page nav/filtering.
- `rows[]`: concrete upstream streams.

### Row shape

| Field | Meaning |
| --- | --- |
| `id` | Stable stream id (`bluefin-testing`, `fedora-bootc-stable`) |
| `group` | `gnome-os`, `fedora-bootc`, `projectbluefin`, `ublue` |
| `variant` | Product/stream name |
| `display_name` | Human label for the page |
| `publisher_repo` | Source repo when known |
| `org` | Owning org when known |
| `branch` | Stream/tag tracked by the collector |
| `published_at` | Upstream release publish time |
| `freshness_age_days` | Days since `published_at` |
| `open_prs` | Optional repo pressure signal |
| `state` / `state_reason` | Explicit availability contract |
| `source_url` / `collected_at` / `derivation` | Provenance for the row |

`published_at` for image lanes is sourced from the strongest available publish signal in this order:
1. GHCR package tag timestamps for lane tags (`stable`, `testing`) when available.
2. GitHub Releases `published_at` when package tag timestamps are unavailable.

## `docs/data/tests-matrix.json`

Purpose: one row per `(variant, branch, suite)` result for the `/tests` page.

### Top-level shape

- `_meta`
- `summary_metrics[]`
- `suite_roles`: map of suite name to `"gate"` or `"info"`.
- `dimensions`: distinct variants/branches/suites for filters.
- `rows[]`: concrete matrix cells.

### Row shape

| Field | Meaning |
| --- | --- |
| `id` | Stable matrix key (`bluefin-testing-smoke`) |
| `variant` / `branch` / `suite` | Page filter dimensions |
| `role` | `"gate"` for gating suites (`smoke`, `system`, `flatcar`) or `"info"` for informational suites (`developer`, `software`, `common`) |
| `result_status` | Published status from `docs/results/*.json` |
| `last_run` | Workflow completion time for the current cell |
| `workflow_name` | Workflow evidence for drill-down |
| `scenarios_total` / `scenarios_failed` | Current scenario counts |
| `pass_rate` | Derived percentage or `null` when unavailable |
| `history_points` | Count of historical entries already published |
| `results_path` / `screenshot_path` / `screenshot_url` | Artifact links |
| `state` / `state_reason` | Explicit availability contract |
| `enrollment_issue_url` | Link to the enrollment tracking issue for unenrolled variants; `null` for normal rows |
| `flake_flips` | Number of pass/fail status transitions across the row's recorded run history |
| `runs_recorded` | Number of runs recorded for this row in `docs/data/history/test-runs.ndjson` |
| `source_url` / `collected_at` / `derivation` | Provenance for the row |

## `docs/data/applications-matrix.json`

Purpose: app-first rows for the `/applications` page. V1 currently tracks Bazaar and Firefox.

### Top-level shape

- `_meta`
- `summary_metrics[]`
- `applications[]`: app catalog entries.
- `rows[]`: one row per `(app_id, variant, branch)`.

### Application catalog shape

| Field | Meaning |
| --- | --- |
| `id` | Stable app id (`bazaar`, `firefox`) |
| `display_name` | Page label |
| `scope` | Current rollout scope (`v1`) |
| `primary_suite` | Preferred evidence source |
| `fallback_suites` | Coarser stop-gap evidence sources |
| `source_url` / `collected_at` / `derivation` | Provenance for the catalog entry |

### Row shape

| Field | Meaning |
| --- | --- |
| `id` | Stable key (`bazaar-bluefin-testing`, `firefox-bluefin-testing`) |
| `app_id` | Foreign key into `applications[]` |
| `variant` / `branch` | Page filter dimensions |
| `primary_suite` | Intended app evidence lane |
| `primary_result_status` | Published status for the primary suite |
| `primary_last_run` | Latest run for the primary suite |
| `scenario_total` / `scenario_failed` | App result totals when available |
| `fallback_signal_count` | Number of coarse fallback signals attached |
| `fallback_signals[]` | Optional coarse evidence rows (same provenance rules) |
| `state` / `state_reason` | Explicit availability contract |
| `source_url` / `collected_at` / `derivation` | Provenance for the row |

## Starter-artifact intent

These files are implementation-ready contracts plus honest seed data. Later collector work should replace starter `unavailable` rows with live evidence, not redesign the shape.

## `docs/data/homebrew-ecosystem.json`

Purpose: one row per tracked image lane, integrated into the `/adoption` page as supplementary ecosystem context. Covers Homebrew tap/package install and download statistics per `(variant, branch)`.

### Top-level shape

- `_meta`
- `summary_metrics[]`
- `taps[]`: Homebrew taps this repo explicitly tracks (empty until a repo-owned artifact fetched from formulae.brew.sh or upstream tap repos is added).
- `rows[]`: one row per `(variant, branch)` from `docs/data/variant-publishers.json`.

### Row shape

| Field | Meaning |
| --- | --- |
| `id` | Stable lane id (`bluefin-testing`, `aurora-stable`) |
| `variant` | Image variant name |
| `branch` | Stream/tag (`testing`, `stable`) |
| `tap_name` | Homebrew tap name when known, else `null` |
| `tap_url` | Canonical tap URL when known, else `null` |
| `install_count` | Total installs from brew stats artifact, or `null` when unavailable |
| `download_count` | Total downloads from brew stats artifact, or `null` when unavailable |
| `state` / `state_reason` | Explicit availability contract |
| `source_url` / `collected_at` / `derivation` | Provenance for the row |

### Summary metrics

| id | Meaning |
| --- | --- |
| `tracked_image_lanes` | Total lanes from `variant-publishers.json` |
| `lanes_with_brew_data` | Lanes with Homebrew analytics data from formulae.brew.sh or upstream tap repos present in docs/data/ |
| `lanes_awaiting_brew_data` | Lanes with no Homebrew analytics data from formulae.brew.sh or upstream tap repos in docs/data/ |

## `docs/data/adoption-metrics.json`

Purpose: executive-readable adoption view for the `/adoption` page. Covers image pull counts from container registry APIs (GHCR), active-device estimates from Fedora countme infrastructure, and trust/provenance coverage per tracked image lane.

### Top-level shape

- `_meta`
- `summary_metrics[]`
- `trust_cards[]`: one card per tracked variant with static trust/provenance metadata.
- `rows[]`: one row per `(variant, branch)` with pull and countme signals from authoritative upstream sources.

### Trust card shape

| Field | Meaning |
| --- | --- |
| `variant` | Image variant name |
| `publisher_repo` | Source repo when known |
| `org` | Owning org |
| `emits_sbom` | Whether the publisher emits an SBOM |
| `emits_cve_scan` | Whether the publisher emits a CVE scan |
| `emits_cosign_attestation` | Whether the publisher emits a cosign attestation |
| `state` / `state_reason` | `available` when `publisher_repo` and `org` are known; `unavailable` with explicit `state_reason` when the publisher is unknown (e.g., flatcar) |
| `source_url` / `collected_at` / `derivation` | Provenance for the card |

### Row shape

| Field | Meaning |
| --- | --- |
| `id` | Stable lane id (`bluefin-testing`, `bluefin-lts-stable`) |
| `variant` | Image variant name |
| `branch` | Stream/tag |
| `pull_count` | Registry pull count from container registry API (e.g., GHCR package statistics), or `null` when unavailable |
| `countme_active_devices` | Active device estimate from Fedora countme infrastructure, or `null` when unavailable |
| `state` / `state_reason` | Explicit availability contract |
| `source_url` / `collected_at` / `derivation` | Provenance for the row |

### Summary metrics

| id | Meaning |
| --- | --- |
| `tracked_image_lanes` | Total lanes from `variant-publishers.json` |
| `lanes_with_pull_data` | Lanes with pull_count from container registry API (e.g., GHCR) present in docs/data/ |
| `lanes_with_countme_data` | Lanes with countme_active_devices from Fedora countme infrastructure present in docs/data/ |

## release-verdict.json (index / SRE triage)

Written by `scripts/collect_release_verdict.py` (ADR 0002). One row per production lane
(`bluefin-stable`, `bluefin-testing`, `bluefin-lts-stable`, `bluefin-lts-testing`, `dakota-testing`).

### Top-level shape

- `schema_version`, `_meta` (`generated_at`, `description`, `status`)
- `summary_metrics[]`: `lanes_good` and related counters
- `rows[]`: one verdict row per lane

### Row shape

| Field | Meaning |
| --- | --- |
| `id` / `lane` | Stable lane id |
| `variant` / `branch` | Image variant and stream |
| `image_ref` | GHCR image reference |
| `digest` | Current manifest digest resolved anonymously from GHCR, or `null` when unavailable |
| `verdict` | `good` iff build passed AND lab QA passed on this digest AND cosign keyless verify passed; `bad` when any input failed; `pending` when an input has no evidence for this digest |
| `build` / `qa` / `signature` | Each `{status: passed\|failed\|pending\|unavailable, detail, source_url}` |
| `security_regression` | CVE regression signal, displayed alongside but never gating (ADR 0002) |
| `state` / `state_reason` | Explicit availability contract |
| `source_url` / `collected_at` / `derivation` | Provenance for the row |

Notes:
- Signature verification is keyless (`--certificate-identity-regexp '^https://github.com/projectbluefin/'`,
  GitHub Actions OIDC issuer). Verify results are cached by digest in the previous JSON.
- QA evidence must reference the current digest's build or newer; stale evidence yields `pending`, never `good`.
- CVE counts are displayed alongside but never gate the verdict (ADR 0002).

## history/release-verdict.ndjson

Rolling append-only history of verdict transitions. One line per `(lane, digest)` change:
`{recorded_at, lane, digest, verdict, build, qa, signature}`. Retention: 365 days; the collector
prunes older lines on each run. Rows never rewrite — a new digest or changed verdict appends.

## NDJSON history contracts

Time-series evidence accumulates as git-tracked rolling NDJSON under
`docs/data/history/`. Collectors append new lines, deduplicate by natural key,
and retain raw lines for 180 days. GitHub Actions artifacts store only bulky
evidence (screenshots, logs, SBOMs); dashboards link to those artifacts but
never parse them directly.

### Shared rules

- Append-only at write time; duplicate natural keys are ignored during ingest.
- Each line is a self-contained JSON object.
- Retention: raw lines kept for 180 days. Compaction beyond 180 days is a future
  concern and is documented as out-of-scope here.
- Provenance fields (`source_url`, `recorded_at`) are required on every line.

### `docs/data/history/test-runs.ndjson`

One line per suite run.

Producer: `scripts/collect_test_history.py` (appends after each lab run).

Consumer: `/tests` page and index triage mini-suite indicators.

| Field | Type | Meaning |
| --- | --- | --- |
| `recorded_at` | ISO8601 | When the history line was recorded |
| `variant` | string | Image variant (`bluefin`, `bluefin-lts`, `dakota`, ...) |
| `branch` | string | Stream (`testing`, `stable`) |
| `suite` | string | Suite name (`smoke`, `system`, `developer`, `software`, `common`, `flatcar`) |
| `role` | `"gate"` or `"info"` | Whether the suite is gating or informational for the lane |
| `workflow_name` | string | Argo workflow that executed the suite |
| `status` | `"passed"` or `"failed"` | Terminal suite status |
| `scenarios_total` | int | Total scenarios executed |
| `scenarios_failed` | int | Scenarios that failed |
| `failed_scenarios` | string[] | Names of failed scenarios, empty when none |
| `digest` | string or null | Tested image digest when known |

Dedup key: `(variant, branch, suite, workflow_name)`.

### `docs/data/history/build-runs.ndjson`

One line per terminal build run.

Producer: `scripts/collect_factory_builds.py` for `plane=publish` (GitHub Actions
runs of `projectbluefin/{bluefin,bluefin-lts,dakota}`) and
`scripts/collect_lab_builds.py` for `plane=lab` (Argo pipeline build runs).

Consumer: `/builds` page, index build-duration sparklines, release-verdict build
input.

| Field | Type | Meaning |
| --- | --- | --- |
| `recorded_at` | ISO8601 | When the history line was recorded |
| `plane` | `"publish"` or `"lab"` | `publish` for upstream GHA builds; `lab` for Argo pipeline runs |
| `repo` | string | Source repository (`projectbluefin/bluefin`, ...) |
| `lane` | string | Lane id (`bluefin-testing`, ...) |
| `run_id` | string | Unique run identifier from the producing system |
| `status` | `"passed"` or `"failed"` | Terminal build status |
| `started_at` | ISO8601 | Build start time |
| `finished_at` | ISO8601 | Build end time |
| `duration_min` | number | Duration in minutes |
| `run_url` | string | Canonical run URL |
| `failure_stage` | string or null | Failed stage name when `status` is `failed` |

Dedup key: `(plane, run_id)`.

### `docs/data/history/cve-summary.ndjson`

One line per lane per nightly grype scan.

Producer: `nightly-cve-scan` CronWorkflow (lab-side grype scan of published lane
digests), collected by `scripts/collect_cve_summary.py`.

Consumer: `/security` page, release-verdict CVE regression display.

| Field | Type | Meaning |
| --- | --- | --- |
| `recorded_at` | ISO8601 | When the scan was recorded |
| `lane` | string | Lane id (`bluefin-testing`, ...) |
| `digest` | string or null | Scanned image digest |
| `critical` | int | Critical CVE count |
| `high` | int | High CVE count |
| `medium` | int | Medium CVE count |
| `low` | int | Low CVE count |
| `fixable` | int | CVEs with available fixes |
| `total` | int | Total CVE count |

This file is non-gating per ADR 0002. It is displayed alongside the verdict as a
trend signal.

### `docs/data/enrollment-issues.json`

Mapping of unenrolled matrix variants to their open tracking issues in
`projectbluefin/lab`.

Producer: `scripts/collect_enrollment_issues.py`.

Consumer: `/tests` matrix page.

```json
{
  "schema_version": "1.0",
  "_meta": { "generated_at": "...", "description": "...", "status": "..." },
  "variants": {
    "aurora": { "issue_number": 123, "issue_url": "https://github.com/projectbluefin/lab/issues/123" },
    "bazzite": { "issue_number": 124, "issue_url": "https://github.com/projectbluefin/lab/issues/124" },
    "fedora-kinoite": { ... },
    "fedora-silverblue": { ... },
    "snosi": { ... },
    "cosmic": { ... },
    "gnomeos": { ... },
    "fedora-bootc": { ... }
  }
}
```

Policy: unenrolled variants stay in the tests matrix greyed out, each cell
linking its enrollment issue. The collector emits these rows; the page never
fabricates them.
