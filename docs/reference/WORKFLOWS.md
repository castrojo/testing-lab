# WorkflowTemplates — Agent Contract

This is the canonical interface for driving the lab. Every supported
operation is a single `argo submit --from workflowtemplate/<name> [-p k=v]`
invocation. No bash, no `kubectl apply`, no SSH.

Conventions:

- All templates live in `argo/workflow-templates/*.yaml` and are reconciled
  to namespace `argo` by the ArgoCD `testing-lab` Application.
- Workflow-level parameters listed below are passed via `-p name=value`.
- Wall-clock targets are warm-cache numbers; cold-cache figures (BIB build
  on a missing golden disk) add ~5–10 min.
- The agent contract: prefer the **top-level** templates (`bluefin-qa-pipeline`,
  `bluefin-titan-smoke`). The supporting templates (provision, run, teardown)
  are called as `templateRef` and rarely submitted directly.

---

## Top-level entry points

### `bluefin-qa-pipeline`

Full pipeline: ensure golden disk → reflink + boot a fresh KubeVirt VM →
run test suites → teardown VM on exit.

| Parameter | Default | Notes |
|---|---|---|
| `image` | `ghcr.io/ublue-os/bluefin` | Source image. Tag is appended from `image-tag` for some callers; pass with tag if invoking directly. |
| `image-tag` | `latest` | `latest`, `lts`, etc. Also used as the golden-disk dir name. |
| `namespace` | `bluefin-test` | KubeVirt VM namespace. Use `bluefin-lts-test` for LTS. |
| `suites` | `smoke,developer` | Comma list; valid: `smoke`, `developer`, `software`. |
| `variant` | `bluefin` | Selects test fixtures (e.g. `dakota` for Ghostty). |
| `ssh-key-secret` | `bluefin-test-ssh-key` | Secret in `argo` ns with `id_ed25519`. |

Wall-clock: ~5 min (warm), ~10–14 min (cold BIB rebuild).

```
argo submit --from workflowtemplate/bluefin-qa-pipeline \
  -p image-tag=latest -p suites=smoke --wait
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
| `tests-branch` | `main` | `testing-lab` branch cloned by `run-gnome-tests`. |

Wall-clock: ~12–20 min depending on ISO build cache and Flatcar download time.

```
argo submit --from workflowtemplate/knuckle-qa-pipeline \
  -p branch=main -p suite=smoke --wait
```

### `bluefin-titan-smoke`

Runs smoke tests against the **persistent** titan VMs (`titan-bluefin`,
`titan-lts`). Skips BIB and VM provisioning entirely. Use when iterating on
tests or when BIB is slow/broken.

Prerequisites: both titan VMs running. Fetch IPs:

```
kubectl get vmi titan-bluefin -n bluefin-test -o jsonpath='{.status.interfaces[0].ipAddress}'
kubectl get vmi titan-lts    -n bluefin-lts-test -o jsonpath='{.status.interfaces[0].ipAddress}'
```

| Parameter | Default | Notes |
|---|---|---|
| `vm-ip-latest` | *(required)* | titan-bluefin IP |
| `vm-ip-lts` | *(required)* | titan-lts IP |
| `suite` | `smoke` | Single suite name. |
| `ssh-key-secret` | `bluefin-test-ssh-key` | |
| `issue-title` | `titan smoke run` | Free-text label, appears in pod annotation. |

Wall-clock: ~3 min (test-only, no provisioning).

```
argo submit --from workflowtemplate/bluefin-titan-smoke \
  -p vm-ip-latest=10.42.x.y -p vm-ip-lts=10.42.x.z --wait
```

### `patch-golden-disk`

One-shot maintenance: re-runs the disk configuration step (SSH key,
selinux=0, sudoers) on an existing golden disk without rebuilding it.

| Parameter | Default | Notes |
|---|---|---|
| `image-tag` | `latest` | Disk dir under `/var/tmp/bluefin-golden/`. |

### `service-catalog-pipeline`

K8s-native service-catalog validation pipeline. Deploys a lane's workload
manifests into an ephemeral namespace, runs the lane's pytest suite, and
tears down on exit. Does not use VMs or GNOME infrastructure — runs
directly against k8s-hosted workloads.

| Parameter | Default | Notes |
|---|---|---|
| `lane` | `media` | Lane name — must match a directory under `tests/service_catalog/<lane>/` and `tests/service_catalog/<lane>/manifests.yaml` |
| `image-tag` | `latest` | Passed to lane manifests (available for future per-lane image selection) |
| `branch` | `main` | testing-lab branch to clone for manifests and tests |

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

These are exposed only because they are referenced by the entry points;
submit them directly only for diagnosis.

### `run-service-tests` (template: `run-pytest`)

Non-GNOME test runner for service-catalog lanes. Clones testing-lab,
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

`git-sync` initContainer clones testing-lab → main container SSHes to the VM
IP → installs deps (skipped if present) → runs qecore-headless + behave →
captures `results.json` to pod stdout (Loki + `argo logs`).

Resource limits and `hostNetwork: true` are set on the pod (KubeVirt
masquerade only routes from host netns).

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

The active `pr-label-poller` checks every open Bluefin, Bluefin LTS, and Dakota
PR every five minutes. Bluefin and Bluefin LTS run smoke QA against their
current `:testing` images; Dakota creates a SHA-pinned BuildStream build and
container-QA workflow. Feedback is reported through one repository-specific
Check Run; no PR comment is created.

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
just lab-check-status <bluefin|bluefin-lts|dakota> <pr-number>
```

The Check Run is created by the org-wide MergeRaptor GitHub App. Its private key
remains in GitHub Actions; Kubernetes sends only `repository_dispatch` payloads.
Each repository's `lab-check.yml` must exist on its default branch. The app
installation must grant `checks: write`.

### `dakota-publish-pipeline`

Publishes `192.168.1.102:30500/dakota:testing` and
`192.168.1.102:30500/dakota-nvidia:testing` to the matching
`ghcr.io/projectbluefin/*:testing` tags. Each lane resolves and copies the Zot
image by digest, then fails unless GHCR reports the same digest. The lanes run
independently; a final result task reports both statuses and fails the workflow
if either lane fails.

The workflow requires a Secret named `ghcr-publish-auth` in namespace `argo`.
It is an operator-managed `kubernetes.io/dockerconfigjson` Secret with the
standard `.dockerconfigjson` key and GHCR package write access. The Secret is
not stored in git. Scripts consume the mounted auth file directly and never
enable shell tracing.

ORAS discovery is attempted for each source digest. Referrers are copied
recursively when present. No referrers, an unsupported discovery API, or a
missing ORAS binary is logged explicitly and does not invalidate the image
copy; a failed copy of discovered referrers fails that lane.

The root `onExit` handler writes one compact `kind=publish` record through
`scripts/publish_dakota_run.py`. A passed record carries the verified primary
`dakota:testing` digest; because the workflow succeeds only when both Dakota
lanes succeed, that record represents the aggregate publication run. Failed
runs persist a normalized `publish` failure without raw logs. History
persistence is mandatory: a fetch, validation, commit, or push failure remains
visible as a failed exit-handler node and is never swallowed.

Manual run:

```bash
just run-dakota-publish
```

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

Lives in `manifests/`, applied via the `testing-lab-infra` ArgoCD app:

| Schedule | Cron | Template called | Purpose |
|---|---|---|---|
| `nightly-smoke` | 02:00 UTC | `bluefin-qa-pipeline` (latest) | Catch upstream regressions |
| `nightly-smoke-lts` | 02:30 UTC | `bluefin-qa-pipeline` (lts)    | Same, for LTS branch; first fire builds the missing golden disk |
| `nightly-dakota-publish` | 21:00 UTC | `dakota-publish-pipeline` | Copy both Dakota testing lanes from Zot to GHCR by digest |
| `orphan-vm-cleanup` | every 2h | inline | GC stale per-run hostDisks in bluefin, flatcar, and knuckle namespaces |

---

## KubeStellar workflows

Reusable WorkflowTemplates in `argo/workflow-templates/`, reconciled by the
`testing-lab` ArgoCD Application. KubeStellar installation and upgrades are
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
