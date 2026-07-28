# Bluefin Integration

This homelab instance is the CI backend for Project Bluefin. Every image publish
results in a full acceptance test run with zero human intervention; structured
per-suite results are published back into this repo for dashboard and release
consumers.

---

## Images Under Test

| Image | Tag | Trigger | QA path |
|---|---|---|---|
| `ghcr.io/projectbluefin/bluefin` | `testing` | Nightly 02:00 UTC + digest change | `image-poller` → `bluefin-qa-pipeline` → `run-container-tests` |
| `ghcr.io/projectbluefin/bluefin-lts` | `testing` | Nightly 02:30 UTC + digest change | `image-poller` → `bluefin-qa-pipeline` → `run-container-tests` |
| `ghcr.io/frostyard/snow` | `latest` | Every 3 hours + digest change | Poller → `bluefin-qa-pipeline` container lane |
| `ghcr.io/projectbluefin/dakota` | latest BST build | Nightly 03:00 UTC + BST build | `dakota-qa-pipeline` → `run-container-tests` |

`:testing` is the only production branch tested continuously. `:stable` runs are
supported but not scheduled by default. Never use `:latest` or date tags in
automation — `bluefin-lts` has no `:latest` tag.

---

## Test Suites

All Bluefin image test scenarios live in **[`projectbluefin/testsuite`](https://github.com/projectbluefin/testsuite)** — the single source of truth. The lab's `run-container-tests` WorkflowTemplate clones `testsuite` (main or a branch) and runs qecore-headless + behave directly inside the published bootc OCI image. VM-backed KubeVirt coverage remains for the workflows that explicitly still need it, but the image-poll path no longer boots or installs a guest.

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

Hourly CronWorkflows (`image-poll-bluefin-testing`, `image-poll-lts-testing`) call
the `image-poller` WorkflowTemplate. Each run:

1. Pulls the current digest for the target image from ghcr.io
2. Reads the last-known digest from `image-polling-digests` in namespace `argo`
3. If digests match: exits cleanly (no test run)
4. If the digest changed: submits `bluefin-qa-pipeline`, which fans out `run-container-tests`
5. Each selected suite publishes its structured results back into this repo
6. Only after the downstream workflow succeeds does `image-poller` persist the new digest

This means every new Bluefin image publish triggers container-only validation
within one hour, automatically, with no human action.

---

## Result Publication Pipeline

`run-container-tests` writes `results.json` and then, when `github-token` is
available, clones this repository and runs `scripts/publish_test_results.py` to
merge the new suite outcome into the tracked results data. The publication flow is:

1. Execute the selected behave suite inside the bootc OCI image
2. Write `results.json`, `behave-rc.txt`, and a summary file under `/tmp/results`
3. Clone `projectbluefin/lab` with `github-token`
4. Run `scripts/publish_test_results.py` for the image/suite/workflow tuple
5. Push the updated structured results back to the repo for dashboard consumers

The result: a Bluefin release is published → tests run in containers → the repo
receives per-suite QA results without any VM-specific artifact handling.

---

## Triggering a Test Run Manually

```bash
# Smoke suite against bluefin:testing
just run-tests

# Smoke suite against bluefin-lts:testing
just run-tests-tag lts-testing

# Run the default testing/lts-testing matrix
just run-tests-matrix

# Submit a named workflow directly
argo submit argo/bluefin-smoke-test.yaml --watch
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
the QA lane now uses the same container-only fan-out through `run-container-tests`.
BuildStream artifact builds remain separate in `dakota-build-pipeline`. Dakota PRs
can also use the `test-on-lab` label via the PR label poller.
