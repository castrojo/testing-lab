# WorkflowTemplates — Agent Contract

This is the canonical interface for driving the lab. Every supported
operation is a single `argo submit --from workflowtemplate/<name> [-p k=v]`
invocation. No bash, no `kubectl apply`, no SSH.

Conventions:

- All templates live in `argo/workflow-templates/*.yaml` and are reconciled
  to namespace `argo` by the ArgoCD `lab` Application.
- Workflow-level parameters listed below are passed via `-p name=value`.
- Wall-clock targets are warm-cache numbers; cold-cache figures (BIB build
  on a missing golden disk) add ~5–10 min.
- The agent contract: prefer the **top-level** templates (`bluefin-qa-pipeline`,
  `dakota-qa-pipeline`). The supporting templates (provision, run, teardown)
  are called as `templateRef` and rarely submitted directly.

---

## Top-level entry points

### `bluefin-qa-pipeline`

Container-only pipeline: validate the requested suites, fan out
`run-container-tests` inside the published OCI image, publish selected results,
and return a summary. This path does not build a containerDisk, boot a VM, or
SSH into a guest.

| Parameter | Default | Notes |
|---|---|---|
| `image` | `ghcr.io/projectbluefin/bluefin` | Published OCI image repository. |
| `image-tag` | `testing` | Image tag to test. |
| `suites` | `smoke,common,developer,software,system` | Comma list of supported suites. |
| `variant` | `bluefin` | Selects test fixtures. |
| `branch` | `main` | Lab branch passed to the workflow metadata. |
| `testsuite-branch` | `main` | `projectbluefin/testsuite` branch to clone. |

Wall-clock: depends on the selected suite set and container-qa semaphore.

```
argo submit --from workflowtemplate/bluefin-qa-pipeline \
  -p image-tag=testing -p suites=smoke --wait
```

### `knuckle-qa-pipeline`

Builds the Knuckle installer ISO from source, boots a blank KubeVirt VM in
`knuckle-test`, runs a headless install with an explicit `/install-complete`
signal, reboots from the installed disk, rediscovers the new VMI IP, then runs
smoke tests against the installed system.

| Parameter | Default | Notes |
|---|---|---|
| `branch` | `main` | Knuckle source branch to clone and build. |
| `namespace` | `knuckle-test` | KubeVirt namespace for the ephemeral installer VM. |
| `suite` | `smoke` | Single GNOME test suite to run after install. |
| `ssh-key-secret` | `bluefin-test-ssh-key` | Secret in `argo` ns used for installer access and installed-system SSH. |
| `tests-branch` | `main` | Lab branch cloned by `run-gnome-tests`. |

Wall-clock: ~12–20 min depending on ISO build cache and Flatcar download time.

```
argo submit --from workflowtemplate/knuckle-qa-pipeline \
  -p branch=main -p suite=smoke --wait
```

### `iso-e2e-pipeline`

Downloads a published ISO, optionally verifies its SHA256, checks out the
canonical `projectbluefin/iso` harness, and runs smoke plus unattended installer
E2E on a KVM-capable lab node. The `iso-e2e-validation` repository dispatch
supplies immutable candidate metadata and publishes the
`projectbluefin/lab / ISO E2E` commit status.

| Parameter | Default | Notes |
|---|---|---|
| `iso-url` | *(required)* | HTTPS URL for the ISO candidate. |
| `iso-sha256` | empty | Expected SHA256; required by repository dispatch. |
| `iso-ref` | `main` | ISO harness branch, tag, or immutable commit. |
| `image-ref` | `ghcr.io/ublue-os/bluefin` | Image installed by the unattended harness. |
| `image-tag` | `stable` | Image tag installed by the unattended harness. |
| `run-e2e` | `true` | Set `false` for smoke-only validation. |

```
argo submit --from workflowtemplate/iso-e2e-pipeline \
  -p iso-url=https://example.invalid/candidate.iso \
  -p iso-sha256=<sha256> -p iso-ref=<immutable-iso-sha> --wait
```

### `service-catalog-pipeline`

K8s-native service-catalog validation pipeline. Deploys a lane's workload
manifests into an ephemeral namespace, runs the lane's pytest suite, and
tears down on exit. Does not use VMs or GNOME infrastructure — runs
directly against k8s-hosted workloads.

| Parameter | Default | Notes |
|---|---|---|
| `lane` | `media` | Lane name — must match a directory under `tests/service_catalog/<lane>/` and `tests/service_catalog/<lane>/manifests.yaml` |
| `image-tag` | `latest` | Passed to lane manifests (available for future per-lane image selection) |
| `branch` | `main` | Lab branch to clone for manifests and tests |

Wall-clock: ~3–5 min (depends on image pull and test count).

```
just run-service-catalog-smoke                        # media lane, latest
just run-service-catalog-smoke lane=non-media         # non-media lane
just run-service-catalog-smoke lane=media branch=feat/my-branch
```

Pipeline structure:
```
create-namespace → deploy-workload → run-tests → cleanup (onExit)
```

The deploy step reads `tests/service_catalog/<lane>/manifests.yaml` from
the cloned repo and applies it to the ephemeral namespace. Each lane owns
its own manifests — the pipeline is lane-agnostic.

Test runner: `run-service-tests` (see supporting templates below). Uses
the shared helpers in `tests/service_catalog/shared/` for deployment,
persistence, reachability, redeploy, and teardown assertions.

---

## Supporting templates (called via `templateRef`)

### `kde-linux-qa`

Downloads the published KDE Linux hybrid ISO pinned by SHA256, wraps it as a
containerDisk, boots it under KubeVirt OVMF, forwards SSH and port 4723 with
virtctl, and runs the `testsuite` `kde-smoke` suite. The VM is deleted by the
mandatory `onExit` teardown.

```
just run-kde-linux
```

These are exposed only because they are referenced by the entry points;
submit them directly only for diagnosis.

### `run-service-tests` (template: `run-pytest`)

Non-GNOME test runner for service-catalog lanes. Clones this lab repository,
discovers the test suite under `tests/service_catalog/<lane>/`, and runs
pytest with JUnit XML output. Emits a summary line (`N/M pytest checks
passed`) to stdout for Argo/Loki consumption.

Env vars passed to the test container: `TEST_NAMESPACE`, `TEST_LANE`,
`TEST_RESULTS_DIR`. Lane-specific tests import shared helpers from
`tests/service_catalog/shared/` (deploy, persistence, reachability,
redeploy, teardown).

### `bib-build-and-push` (template: `ensure-disk`)

Builds the golden raw disk via `bootc-image-builder` if missing or stale.
Stale detection compares the upstream image digest (via skopeo) against the
`source-digest` marker written next to the disk on hostPath.

Outputs: no `outputs.parameters`; side effect is
`/var/tmp/bluefin-golden/<image-tag>/disk.raw` and `source-digest` on ghost.

### `provision-bluefin-vm` (template: `provision-vm`)

btrfs `cp --reflink=auto` from the golden disk, applies SVirt label, creates
a KubeVirt VM, waits for SSH/IP, emits `vm-ip` as an output parameter.

### `provision-flatcar-vm` (template: `provision-vm`)

Same shape for Flatcar — accepts an `ssh-pubkey` parameter directly instead
of relying on the bluefin-test secret for cloud-init injection.

### `run-gnome-tests` (template: `run-gnome-tests`)

`git-sync` initContainer clones this lab repository → main container SSHes to the VM
IP → installs deps (skipped if present) → runs qecore-headless + behave →
captures `results.json` to pod stdout (Loki + `argo logs`).

Resource limits and `hostNetwork: true` are set on the pod (KubeVirt
masquerade only routes from host netns).

### `run-kde-tests` (template: `run-kde-tests`)

Adapts the GNOME runner contract for Aurora/KDE VMs. It clones the selected
testsuite branch, forwards SSH and WebDriver port 4723 through `virtctl`,
starts the VM's `selenium-webdriver-at-spi-run` service, waits for its
`/status` endpoint, and runs `tests/kde-smoke/features` with Behave. Results
and in-guest PNG screenshots are copied back even when Behave fails, then
persisted under the standard ghost test-results host path. `faillog_*`
directories are retained alongside `.tar.gz` bundles, and the first screenshot
is pushed as the stable `desktop-screenshot` OCI artifact. The GitHub
credential, ORAS tool, screenshot, artifact persistence, and result publication
are required and fail the runner when unavailable. QEMU-level screendumps are
not used because
KubeVirt's `virt-launcher` does not expose a QEMU monitor.

The runner accepts `failure-class: test|infra` and `failure-issue-url`
parameters. A failed run classified as infrastructure must include the URL of
its separate tracking issue; otherwise it is counted as a test failure. Every
run rejects a retry setting other than `BEHAVE_RETRIES=2`.

### `aurora-qa-pipeline` (template: `aurora-qa`)

Runs the Aurora/KDE GUI suite against a KubeVirt VM in `aurora-test`:

```text
build Aurora containerDisk
  → provision VM
    → run-kde-tests
      → collect-vm-logs
```

The pipeline builds `ghcr.io/ublue-os/aurora` into the
`aurora-containerdisk` repository with a 30G disk, then passes the VM to the
existing KDE runner. It has a one-hour `activeDeadlineSeconds`, holds the
`aurora-vm-qa` ConfigMap semaphore for the full run, and always deletes the VM
from its `onExit: teardown` handler. The template is GitOps-managed; live lab
evidence may require a run after the change is merged and reconciled.

Before submitting a live run, confirm that both `aurora-qa-pipeline` and
`run-kde-tests` exist in the `argo` namespace. If either template is absent,
wait for ArgoCD reconciliation; local lint and merged Git history are not live
lab evidence.

KDE soak evidence is a rolling window, not a consecutive streak. The publisher
retains the newest 30 runs and records `failure_class` (`test` or `infra`) plus
the filed issue URL for infrastructure flakes. `BEHAVE_RETRIES=2` is enforced
for every run. The window is qualified only after 30 runs with either at least
29 passes, or at least 28 passes and no more than two infrastructure flakes that
each have a filed issue URL. The two-flake budget is fixed and not configurable:

```bash
just evaluate-kde-soak
```

The command reports `pending` until 30 runs exist and exits non-zero for an
unqualified window. Qualification is evidence only; promotion to CI gating
remains a human decision, and each infrastructure flake must have a separate
filed issue.

### `nightly-kde`

The `nightly-kde` CronWorkflow schedules the `aurora-qa-pipeline` at 04:00 UTC.
The schedule is only a trigger: the pipeline's `aurora-vm-qa` key in the
GitOps-managed `workflow-semaphores` ConfigMap is the serialization guard, so
the soak remains safe if another nightly schedule is enabled or delayed.
The CronWorkflow sets a 90-minute deadline, uses `Forbid` for duplicate
triggers, and retains failed workflows for seven days.

Each live run must publish the structured result and screenshot through
`run-kde-tests`, and persist the result/artifact bundle before teardown. A
qualified 30-run window is evidence only: retain the Argo workflow URL, the
published `docs/results/aurora-testing-smoke.json` history, screenshot URL,
and any filed issue URL for every classified infrastructure flake. Live-run
evidence is required after ArgoCD reconciles the Git change; local lint cannot
substitute for that evidence.

### `aurora-kde-sabotage`

Runs the mandatory red-path proof in the isolated `aurora-test` namespace. It
reuses the real VM and KDE runner, first replacing one launch target with
`/usr/bin/this-does-not-exist`, then killing `plasmashell`. Both runs must
fail, publish failed results, retain `kde_faillog` bundles, and leave no VM
after `onExit` cleanup. Normal Aurora runs default to `sabotage-mode: none`;
the runner rejects sabotage modes outside `aurora-test`.

```bash
just run-aurora-kde-sabotage
```

### `run-flatcar-tests` (template: `run-flatcar-tests`)

Same shape for Flatcar; uses `core` as the SSH user and runs pytest+dogtail
fixtures from `tests/flatcar/`.

### `teardown-bluefin-vm` / `teardown-flatcar-vm`

Delete the VM, wait for the VMI object to drain, then `rm` the per-run
hostDisk clone. Invoked as `onExit` from the pipeline templates.

---

## Dakota BST builds

### `dakota-bst`

Drives the Dakota BuildStream workflow through the BuildBarn remote
execution grid. The workflow builds both `oci/bluefin.bst` (`dakota:testing`) and
`oci/bluefin-nvidia.bst` (`dakota-nvidia:testing`) in parallel. Because the lab
cluster lacks NVIDIA GPU hardware to execute GPU test suites, the NVIDIA build runs
as non-blocking (`continueOn`).

| Parameter | Default | Notes |
|---|---|---|
| `variant` | `default` | `dakota:testing` (default) and `dakota-nvidia:testing` (NVIDIA, non-blocking) |
| `branch` | `main` | dakota branch to clone |

Pipeline: `bst-validate` (fast graph check) → `bst-build` (build + lint).

```
just run-dakota-validate              # bst show only, ~5 min
just run-dakota-build                 # default + nvidia variants
```

### Dakota durable build/publish records

`scripts/publish_dakota_run.py` is the workflow-independent producer for
`docs/data/history/build-runs.ndjson`. Future build and publish onExit templates
write one compact JSON object and call:

```bash
python3 scripts/publish_dakota_run.py publish /workspace/run.json
```

The token is read only from `GITHUB_TOKEN`; never pass it as a command argument
or place it in a clone URL. The publisher clones with a credential-free GitHub
URL, authenticates through `GIT_ASKPASS`, retries non-fast-forward pushes from
the latest `main`, and deletes its working clone. It persists no raw output.

Required input fields are `kind` (`build` or `publish`), `workflow_name`,
terminal `status`, `started_at`, `finished_at`, and a credential-free
`run_url`. Successful publish records also require `digest`. Optional
`commit_sha`, `failure_stage`, short `failure_hint`, `failure_class`, `attempt`,
and numeric `metrics` are validated; `failure_hint` is used only to derive a
normalized failure class and is discarded.

Local contract checks and trailing-window comparisons use:

```bash
just validate-dakota-history
just report-dakota-history 20
```

Reports compare the latest window with the immediately preceding window for
duration p50/p95, failure rate, and same-commit status disagreement. Workflow
wiring remains owned by the build and publish templates; this script does not
change their DAGs.

### Factory PR feedback

The active `pr-label-poller` runs every five minutes and dispatches QA for two
sets of PRs. Pass 1 covers every open PR in the auto-test repos, no label
required:

| Repo | QA path |
|---|---|
| `projectbluefin/common` | smoke suite against bluefin `:testing` |
| `projectbluefin/bluefin` | smoke suite against current `:testing` image |
| `projectbluefin/bluefin-lts` | smoke suite against current `:testing` image |
| `projectbluefin/dakota` | SHA-pinned BuildStream build + container QA |
| `projectbluefin/knuckle` | `knuckle-qa-pipeline` |
| `projectbluefin/testsuite` | smoke + common suites against bluefin `:testing` |

Pass 2 is an org-wide catch-all that also picks up any open `projectbluefin` PR
carrying the `test-on-lab` label. The authoritative repo list is `AUTO_REPOS` in
[`argo/workflow-templates/pr-poller.yaml`](../../argo/workflow-templates/pr-poller.yaml).
Feedback is reported through one repository-specific automated Check Run; no PR
comment is created. (This automated Check Run is distinct from the manual
reviewer comments an operator posts during PR-queue review — see
[`docs/ops/lab-operations.md`](../ops/lab-operations.md) §9.)

**Enrollment is a two-sided contract.** A repo only receives visible feedback
when *both* halves exist:

1. **Sender** — the repo is in `AUTO_REPOS` (or a PR carries `test-on-lab`), so
   the lab dispatches a `lab-check` event to it.
2. **Receiver** — the target repo has `.github/workflows/lab-check.yml` on its
   default branch to turn that dispatch into a Check Run.

If only the sender half exists, the dispatch succeeds (HTTP 204) but **nothing
appears on the PR** — no Check Run, no comment, no error visible to the author.
As of this writing the receiver workflow is present in `bluefin`, `bluefin-lts`,
and `dakota`, and **missing** in `common`, `knuckle`, and `testsuite`, so lab QA
for those three repos runs but is silently dropped on the GitHub side. Adding a
repo to `AUTO_REPOS` without also adding `lab-check.yml` is not a complete
enrollment. See [`docs/ops/RUNBOOK.md`](../ops/RUNBOOK.md) for the diagnosis
steps when QA ran but nothing surfaces on the PR.

The check lifecycle is:

```text
poller creates Argo workflow
  -> queued Check Run
  -> workflow admission
  -> in-progress Check Run
  -> optional Dakota BuildStream build
  -> container QA
  -> onExit collector
  -> completed Check Run
```

The final check contains the requested parameters, phase counts, every
significant workflow node, pod/node placement, timestamps, durations, restart
counts, and Argo failure messages. Raw pod logs stay in the private Argo UI to
avoid copying authenticated output into GitHub.

```bash
just lab-check-status <repo> <pr-number>
```

The Check Run is created by the org-wide MergeRaptor GitHub App. Its private key
remains in GitHub Actions; Kubernetes sends only `repository_dispatch` payloads.
Each repository's `lab-check.yml` must exist on its default branch, or the
dispatch is silently dropped (see the enrollment contract above). The app
installation must grant `checks: write`.

### `zot-candidate-lifecycle`

Reusable single-lane foundation for immutable local Zot candidates. Call the
`candidate-preflight` template before pushing `candidate-<commit-sha>`; it
rejects reused tags and fails under Zot storage pressure. After that lane's QA
passes, call `promote-candidate` with the candidate digest. It pulls the
manifest back by digest, promotes the same digest to `:testing`, verifies the
target, and attaches the versioned ORAS promotion evidence contract.

The optional `zot-writer-auth` docker config keeps the template compatible with
today's anonymous-write registry and becomes required only at the documented
authentication activation gate. See
[Zot candidate promotion](../ops/zot-candidate-promotion.md).

---

## Catalog installs

### `catalog-install-lsio`

Install an app from the linuxserver.io catalog. The workflow fetches the app's
upstream config from the LSIO images API at install time, renders Kubernetes
manifests via `scripts/catalog_install_lsio.py`, and either opens a GitOps PR
(`mode=gitops`, default) or applies the manifests immediately (`mode=imperative`).

| Parameter | Default | Notes |
|---|---|---|
| `app` | *(required)* | Catalog app name (e.g. `jellyfin`) |
| `mode` | `gitops` | `gitops` opens a PR; `imperative` applies directly |
| `namespace` | `catalog-<app>` | Target namespace |
| `image-tag` | `latest` | Image tag to render |
| `base-branch` | `main` | Base branch for the GitOps PR |

GitOps mode:

1. Renders manifests.
2. Runs offline structural validation (`scripts/catalog_validate.py`).
3. Commits `manifests/catalog-apps/<app>/manifest.yaml` to branch `bot/catalog-install-<app>-<ts>`.
4. Opens a PR via the GitHub REST API.

```
argo submit --from workflowtemplate/catalog-install-lsio \
  -p app=jellyfin \
  -p mode=gitops \
  -n argo --watch
```

Installed apps are tracked in `docs/data/catalog/installed.json`.

## CronWorkflows

Lives in `manifests/`, applied via the `lab-infra` ArgoCD app:

| Schedule | Cron | Template called | Purpose |
|---|---|---|---|
| `nightly-smoke` | 02:00 UTC | `bluefin-qa-pipeline` (`testing`) | Catch upstream regressions |
| `nightly-smoke-stable` | 03:00 UTC | `bluefin-qa-pipeline` (`stable`, `smoke`) | Stable image regression coverage |
| `nightly-smoke-lts` | 02:30 UTC | `bluefin-qa-pipeline` (`testing`) | LTS regression coverage |
| `nightly-smoke-lts-stable` | 03:30 UTC | `bluefin-qa-pipeline` (`stable`, `smoke`) | Stable LTS regression coverage |
| `image-poll-dakota` | Every 10 min | `dakota-qa-pipeline` (`testing`) | Active Dakota digest-triggered QA |
| `nightly-dakota` | 03:00 UTC | `dakota-qa-pipeline` (`latest`) | Suspended; does not provide active coverage |
| `orphan-vm-cleanup` | every 2h | inline | GC stale per-run hostDisks in bluefin, flatcar, and knuckle namespaces |

---

## KubeStellar workflows

Reusable WorkflowTemplates in `argo/workflow-templates/`, reconciled by the
`lab` ArgoCD Application. KubeStellar installation and upgrades are
owned by the `kubestellar-applications` ArgoCD parent Application.

### `register-wec`

| Parameter | Default | Notes |
|---|---|---|
| `wec-name` | `ghost` | Cluster name to register with the its1 OCM hub. Labels the ManagedCluster `name=<wec>` after accept. |

SA: `kubestellar-bootstrap` (cluster-admin; klusterlet install writes CRDs).

### `kubestellar-smoke-test`

| Parameter | Default | Notes |
|---|---|---|
| `wec-name` | `ghost` | Target WEC. Verifies BindingPolicy downsync and singleton status upsync via wds1 (`kubeconfig-incluster` key), then cleans up. |

SA: `kubestellar-bootstrap`. Acceptance gate after any core upgrade.

### `kubestellar-platform-verify`

Ordered, read-only platform gate:

```text
verify-datasource → verify-query-surfaces → verify-controller-wiring
  → kubestellar-smoke-test
```

The first three tasks verify Prometheus/cAdvisor and Console kubeconfig wiring,
real KubeStellar/OCM API surfaces and PromQL syntax, then all five controller
scrape jobs. The referenced smoke template is the only task that creates
resources, and it uses only its existing ephemeral BindingPolicy, Namespace,
and Deployment.

| Parameter | Default | Notes |
|---|---|---|
| `wec-name` | `ghost` | Passed explicitly to the final smoke gate. |

Run with `just run-kubestellar-verify`. Read-only checks use the scoped
`kubestellar-observability` ServiceAccount. Argo `templateRef` does not inherit
the referenced WorkflowTemplate's workflow-level identity, so the smoke
template declares `kubestellar-bootstrap` at template level.

---

## Editing this contract

When you add or rename a template, update this file in the same PR. Drift
between templates and this doc is what breaks autonomous agents.
