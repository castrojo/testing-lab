# Bluefin QA Pipeline — Runbook

> Timeless architecture and failure-mode reference. For commands see [docs/reference/agent-cheatsheet.md](../reference/agent-cheatsheet.md). For long-form operator procedures see [docs/ops/lab-operations.md](lab-operations.md).

## Architecture summary

```
Git push / manual submit
        │
        ▼
Argo Workflow (argo namespace)
        │
        ├─ ensure-disk (optional) ─► build or verify golden disk on ghost hostPath
        ├─ provision-vm           ─► reflink golden disk into a per-run KubeVirt VM
        ├─ run-gnome-tests        ─► runner pod clones repo, SSHes VM, executes qecore + behave
        └─ teardown (onExit)      ─► delete VM + per-run hostDisk clone
```

Two steady-state execution paths exist:

| Path | Purpose | Persistent state |
|---|---|---|
| Titan fast path | Test-only iteration against always-on VMs | Titan disk under `/var/home/jorge/VMs/titans/...` |
| Fresh VM path | Image and golden-disk validation | Golden disk under `/var/tmp/bluefin-golden/<tag>/disk.raw` |

## Cluster topology

| Host | Role | IP | Notes |
|---|---|---|---|
| ghost | k3s control-plane + KubeVirt compute | `<ghost-ip>` | Runs VM workloads and Argo control-plane services |
| exo-1 | k3s worker | `<exo-1-ip>` | Workflow pods only |
| Argo UI | external entrypoint | `http://<ghost-ip>:32746` | Host-local service also exposed on port 2746 |
| Loki | log aggregation | `http://<ghost-ip>:30100` | Captures workflow pod logs |
| ArgoCD | GitOps controller | `https://<ghost-ip>` | Reconciles this repo into the cluster |

All KubeVirt VMs are pinned to ghost. Workflow pods may land on ghost or exo-1 depending on template constraints.

## GitOps ownership

| Area | Source of truth | Reconciler |
|---|---|---|
| WorkflowTemplates | `argo/workflow-templates/*.yaml` | ArgoCD application `testing-lab` |
| Cluster infra and CronWorkflows | `manifests/*.yaml` | ArgoCD application `testing-lab-infra` |
| Operator entrypoints | `Justfile` | Local operator / MCP tooling |

The repo is intentionally GitOps-first: cluster state should converge from git, not from manual template applies or node SSH.

## Operator access model

- Use Kubernetes MCP and Argo MCP for workstation-side cluster reads and mutations.
- Prefer the `just` entrypoints when they exist; they are the human-facing wrappers around the same API-driven workflow.
- Do not SSH from a workstation into `ghost` or `exo-1` for inspection, recovery, or file transfer.
- In-workflow SSH into test VMs and probe-pod-to-titan SSH remain valid because they originate inside the cluster and are part of the test harness, not node administration.

## Image, disk, and VM model

| Object | Backing location | Used by | Notes |
|---|---|---|---|
| Golden disk (`latest`) | `/var/tmp/bluefin-golden/latest/disk.raw` | Fresh-VM pipeline | Built by `bib-build-and-push` |
| Golden disk (`lts`) | `/var/tmp/bluefin-golden/lts/disk.raw` | Fresh-VM pipeline | Built by `bib-build-and-push` |
| Titan Bluefin disk | `/var/home/jorge/VMs/titans/titan-bluefin/image/disk.raw` | `titan-bluefin` | Persistent fast-path disk |
| Titan LTS disk | `/var/home/jorge/VMs/titans/image/disk.raw` | `titan-lts` | Persistent fast-path disk |
| Per-run hostDisk clone | `/var/tmp/bluefin-golden/*-runs/...` | Provisioned fresh VMs | Removed by teardown or orphan cleanup |

The SSH secret lives in the `bluefin-test-ssh-key` Kubernetes secret in namespace `argo`.
Golden disks can be patched by workflow after key rotation; titan disk key refresh is intentionally human-gated. <!-- TODO: replace with MCP tool when available -->

## Test execution stack

| Component | Responsibility |
|---|---|
| `git-sync` initContainer | Clone the requested repo ref into the runner pod |
| `run-gnome-tests` | Copy suites to the VM and orchestrate execution |
| `qecore-headless` | Start the Wayland GNOME session inside the VM |
| `dogtail` | Traverse and interact with the AT-SPI tree |
| `gnome-ponytail-daemon` | Translate AT-SPI coordinates into Wayland input |
| `Shell.Eval` | Handle GNOME Shell 50 top-bar interactions that AT-SPI cannot drive reliably |
| Loki | Preserve logs and emitted test artifacts after pod cleanup |

## GNOME Shell 50 constraints

- Clock, quick-settings, and calendar interactions are not reliably actionable through AT-SPI alone.
- `global.context.unsafe_mode = true` must be enabled before top-bar interaction.
- `findChild(..., requireResult=...)` is not a supported dogtail pattern in this repo's stack.
- `findChildren(...)` and `findChild(..., retry=False)` are the canonical presence-check APIs.

## Service-catalog pipeline

A second execution path validates homelab service workloads directly in
Kubernetes, without VMs or GNOME infrastructure:

```
Argo Workflow (argo namespace)
        │
        ├─ create-namespace       ─► ephemeral namespace svc-<lane>-<uid>
        ├─ deploy-workload        ─► clone repo, kubectl apply lane manifests
        ├─ run-service-tests      ─► pytest against live k8s workload
        └─ cleanup (onExit)       ─► delete namespace
```

| Property | Value |
|---|---|
| Entry point | `just run-service-catalog-smoke` or `argo submit --from workflowtemplate/service-catalog-pipeline` |
| Parameters | `lane` (default: media), `image-tag` (default: latest), `branch` (default: main) |
| Wall-clock | ~3–5 min |
| Evidence | pytest JUnit XML + per-check artifacts in pod logs (Loki) |

### Adding a new lane

1. Create `tests/service_catalog/<lane>/manifests.yaml` with the workload's
   Kubernetes manifests (Deployment, Service, PVC, Secrets, ConfigMaps).
2. Create `tests/service_catalog/<lane>/test_<lane>.py` importing helpers
   from `tests/service_catalog/shared/` (deploy, persistence, reachability,
   redeploy, teardown).
3. Run: `just run-service-catalog-smoke lane=<lane>`
4. The pipeline creates a namespace, applies manifests, runs tests, cleans up.

### Inspecting results

- `argo logs @latest` — test output including summary and artifact list.
- Loki query: `{namespace="argo"} |= "svc-catalog"` — all service-catalog
  workflow logs.
- JUnit XML is emitted to the pod stdout and captured by Argo's log
  archival. No separate artifact upload path is needed.

## Common failure modes

| Symptom | Root cause | Durable fix |
|---|---|---|
| `Permission denied (publickey)` during SSH wait | Golden disk or titan disk contains an old public key | Re-patch or rebuild the golden disk; escalate titan key refresh to a human operator |
| Workflow hangs before GUI steps start | VM boot or SSH readiness never completed | Inspect VMI readiness and runner logs, then re-run the appropriate recovery path |
| `TypeError` involving `requireResult` | Stale dogtail step pattern | Replace with `findChildren(...)` or `findChild(..., retry=False)` |
| Clock / quick-settings scenarios miss their targets | GNOME Shell AT-SPI geometry gap | Drive the interaction via `Shell.Eval` |
| `outputs.result` contains debug text | Script template wrote extra stdout | Send debug output to stderr and reserve stdout for the actual result |
| VM stuck `Terminating` | KubeVirt controller race with launcher cleanup | Delete the `virt-launcher-*` pod and let reconciliation finish |
| `run-gnome-tests` pod fails at startup | Workflow template structure error, often misplaced `volumes:` | Fix the template in git and let ArgoCD reconcile it |
| WorkflowTemplate change appears ignored | Workflow was submitted before the new template was reconciled | Verify ArgoCD revision, wait or sync, then submit a new workflow |
| Service-catalog deploy step fails with "No manifests found" | Lane directory missing `manifests.yaml` | Create `tests/service_catalog/<lane>/manifests.yaml` per the contract |
| Service-catalog test step fails with "No test suite" | Lane directory missing under `tests/service_catalog/` | Create the lane test directory with at least one `test_*.py` file |
| Service-catalog namespace stuck terminating | Finalizer or PVC not released | Check for stuck PVCs or pods with `kubectl get all -n <ns>`, delete manually if needed |
| `testing-lab-infra` sync wedged "waiting for healthy state of DaemonSet/..." | A DaemonSet pod is unhealthy on some node (e.g. hostPath missing on that host), blocking every subsequent manifests/ change | Fix or scope the DaemonSet (capability-label nodeSelector), then terminate the stuck operation so ArgoCD retries: `kubectl patch application testing-lab-infra -n argocd --type=merge -p '{"status":{"operationState":{"phase":"Terminating"}}}'` |
| KubeStellar app sync stuck at kubeflex-controller-manager | Postgres hook deadlock under ArgoCD | Keep `installPostgreSQL: false` + separate `kubestellar-postgres` app; see `docs/skills/kubestellar/SKILL.md` |
| Workflow pod rejected `failed quota: argo-quota` | Template missing resources requests/limits | Add explicit cpu+memory requests and limits to every container/script |
| Lab QA ran but no `testing-lab / <repo>` Check Run appears on the factory PR | Half-enrolled repo — the poller dispatched `lab-check` but the target repo has no `.github/workflows/lab-check.yml` receiver, so the dispatch is silently dropped | Confirm the poll succeeded: `kubectl get workflows -n argo -l workflows.argoproj.io/cron-workflow=pr-label-poller`. Confirm the per-PR QA workflow exists and completed. Then confirm the receiver exists: `gh api repos/projectbluefin/<repo>/contents/.github/workflows/lab-check.yml` — a `404` means the repo is only sender-enrolled (in `AUTO_REPOS`) and needs the receiver added on the target repo's default branch. See [`docs/skills/argo-workflows/patterns.md`](../skills/argo-workflows/patterns.md) §20aa. |

### KubeStellar / Console failure modes

See `docs/skills/kubestellar/SKILL.md` (downsync, WEC join, RBAC) and
`docs/skills/console-dashboard/SKILL.md` (Console recovery, exposure policy).
Upgrade order: KubeFlex/postgres -> core-chart -> Console; rerun
`kubestellar-smoke-test` after every core upgrade.

## Historical notes

Date-stamped iteration lessons were removed in the ponytail audit (commit
81f0cc6f); recover them from git history if needed.
Keep this file timeless: architecture, topology, and durable failure modes only.
