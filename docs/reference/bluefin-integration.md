# Bluefin Integration

This homelab instance is the CI backend for Project Bluefin. Selected image-poll
lanes trigger container-only acceptance runs when their registry digest changes;
structured per-suite results are published back into this repo for dashboard and
release consumers.

---

## Images Under Test

| Image | Tag | Trigger | QA path |
|---|---|---|---|
| `ghcr.io/projectbluefin/bluefin` | `testing` | Nightly 02:00 UTC; digest freshness poll every 10 minutes at :00 (QA disabled) | Nightly `bluefin-qa-pipeline` → `run-container-tests` |
| `ghcr.io/projectbluefin/bluefin` | `stable` | Nightly 03:00 UTC; digest poll every 10 minutes at :04 | `image-poller` → `bluefin-qa-pipeline` → `run-container-tests` (full suite) |
| `ghcr.io/projectbluefin/bluefin-lts` | `testing` | Nightly 02:30 UTC; digest freshness poll every 10 minutes at :02 (QA disabled) | Nightly `bluefin-qa-pipeline` → `run-container-tests` |
| `ghcr.io/projectbluefin/bluefin-lts` | `stable` | Nightly 03:30 UTC; digest poll every 10 minutes at :06 | `image-poller` → `bluefin-qa-pipeline` → `run-container-tests` (full suite) |
| `ghcr.io/frostyard/snow` | `latest` | Every 3 hours at :30 + digest change | `image-poller` → `bluefin-qa-pipeline` → `run-container-tests` (`smoke,developer,system`) |
| `ghcr.io/projectbluefin/dakota` | `testing` | Nightly 03:00 UTC; digest freshness poll every 10 minutes at :08 (QA disabled) | Nightly `dakota-qa-pipeline` → `run-container-tests` |

Bluefin and Bluefin-LTS `:testing` lanes use the daily schedules above; their
digest pollers retain freshness state without launching QA. Stable lanes retain
their existing schedules. Dakota follows the same daily-only testing-lane
policy. Never use date tags in automation.

---

## Test Suites

All Bluefin image test scenarios live in **[`projectbluefin/testsuite`](https://github.com/projectbluefin/testsuite)** — the single source of truth. The lab's `run-container-tests` WorkflowTemplate clones `testsuite` (main or a branch) and runs qecore-headless + behave directly inside the published bootc OCI image. VM-backed KubeVirt coverage remains for the workflows that explicitly still need it, but the image-poll path no longer boots or installs a guest.

For the desktop epic's ownership boundary and actionable GNOME coverage
slices, see [`desktop-coverage.md`](desktop-coverage.md). Do not add Bluefin
desktop scenarios to this repository.

Each pipeline run executes one or more suites via the `suites` parameter (comma-separated).

### smoke
GNOME Shell acceptance tests. Validates the desktop is functional after boot.

- Activities button opens and closes the overview
- `dash-to-dock` is present and responds to AT-SPI
- `blur-my-shell` and app-indicator extensions are loaded (Bluefin-specific)
- Top-bar clock and quick-settings accessible via Shell.Eval JS (GNOME Shell 50)
- Screenshot captured at end of each scenario

### developer
Bluefin developer tooling validation. Validates the tools Bluefin ships for developers.

- Homebrew: `brew` in PATH, `brew install` resolves packages
- Podman: `podman run hello-world` succeeds
- Distrobox: `distrobox list` runs without error
- Dev mode (`ujust enable-dev-mode`): systemd service activated, no fatal journal entries
- Ptyxis terminal opens and accepts input

### common
Atomic OS contract and system health tests. Validates Bluefin's immutable-image guarantees.

- `bootc status` reports a known good deployment
- `/usr` is mounted read-only
- XDG portal health + integration
- Flatpak model and state
- polkit rules, shell environment, ujust recipes
- GSettings/dconf defaults, desktop entries, signing assertions

### flatcar (separate pipeline)
Flatcar OS substrate tests. Not part of the Bluefin image pipelines; runs via
`flatcar-smoke-test.yaml`.

---

## Image-Poll Trigger

The Bluefin and Bluefin-LTS image-poll CronWorkflows run every 10 minutes at
staggered offsets (`:00`, `:02`, `:04`, and `:06`) and call the generic
`image-poller` WorkflowTemplate. Dakota's `image-poll-dakota` CronWorkflow runs
at `:08` with `run-qa=false`: it tracks the Dakota testing digest for freshness
only, and daily QA comes from `nightly-dakota` at 03:00 UTC. Each poll run:

1. Resolves the current digest for the target image by inspecting the upstream
   registry directly (never through the zot cache — a tag read triggers zot
   on-demand sync of the full image; see PR #632)
2. Reads the last-known digest from `image-polling-digests` in namespace `argo`
3. If digests match: exits cleanly (no test run)
4. If the digest changed: submits `bluefin-qa-pipeline` for Bluefin/LTS
   (Dakota tracks only); each fans out `run-container-tests`
5. Each selected suite attempts to publish its structured results back into this repo
6. The generic Bluefin/LTS `image-poller` persists its new digest only after the
   downstream workflow succeeds

This means a changed Bluefin or Bluefin-LTS digest triggers
container-only validation within 10 minutes, automatically, with no human action.

---

## Result Publication Pipeline

`run-container-tests` writes `results.json` and, when `github-token` is
available, clones this repository and attempts `scripts/publish_test_results.py`
to merge the new suite outcome into the tracked results data. Publication is
best-effort: a publication warning does not change the suite exit status. The
publication flow is:

1. Execute the selected behave suite inside the bootc OCI image
2. Write `results.json`, `behave-rc.txt`, and a summary file under `/tmp/results`
3. Clone `projectbluefin/lab` with `github-token`
4. Run `scripts/publish_test_results.py` for the image/suite/workflow tuple
5. Attempt to push the updated structured results back to the repo for dashboard
   consumers

The result: a selected Bluefin or Dakota image is tested in containers, and the
repo receives per-suite QA results when publication succeeds, without any
VM-specific artifact handling.

---

## Triggering a Test Run Manually

```bash
# Smoke suite against bluefin:testing
just run-tests

# Smoke suite against bluefin-lts:testing
just run-tests-tag lts-testing

# Run the default testing/lts-testing matrix
just run-tests-matrix

# Submit the container-only workflow directly
argo submit --from workflowtemplate/bluefin-qa-pipeline \
  -p image-tag=testing -p suites=smoke -n argo --watch
```

The `just` wrappers are thin shims around `argo submit`. See `Justfile` for the
full parameter set.

---

## PR Feedback Poller (`pr-label-poller`)

The `pr-label-poller` CronWorkflow runs every 5 minutes and dispatches QA in two
passes:

- **Pass 1 — auto-test repos.** Every open PR in the poller's `AUTO_REPOS` list
  (`projectbluefin/common`, `bluefin`, `bluefin-lts`, `dakota`, `knuckle`,
  `testsuite`) is tested with no label required.
- **Pass 2 — label catch-all.** Any open `projectbluefin` PR labeled
  `test-on-lab` is also picked up.

For each PR it has not already processed (idempotency is tracked by looking for
an existing Argo workflow for that repo + head SHA), it:

1. Identifies the target repo and routes to the matching WorkflowTemplate.
2. Creates the Argo workflow with the PR's branch/SHA as parameters.
3. Sends a `repository_dispatch` (`event_type: "lab-check"`) to the target repo,
   which its `.github/workflows/lab-check.yml` turns into a `testing-lab / <repo>`
   Check Run (queued → in-progress → completed). No commit status or PR comment
   is created.

Feedback only surfaces when the target repo ships the `lab-check.yml` receiver;
see the two-sided enrollment contract in
[`WORKFLOWS.md`](./WORKFLOWS.md) "Factory PR feedback".

To force a run outside `AUTO_REPOS`: add the `test-on-lab` label to a PR in the
`projectbluefin` org. The poller picks it up within 5 minutes.

---

## Dakota

Dakota runs through `dakota-qa-pipeline` rather than `bluefin-qa-pipeline`, but
the QA lane uses the same container-only fan-out through `run-container-tests`.
BuildStream artifact builds remain separate in `dakota-build-pipeline`; the
`image-poll-dakota` lane tracks the resulting `:testing` digest (`run-qa=false`)
and `nightly-dakota` at 03:00 UTC runs the daily QA. Dakota PRs can also
use the `test-on-lab` label via the PR label poller.
