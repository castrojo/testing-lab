---
name: gitops-argocd
description: >
  ArgoCD GitOps model for the lab: what is and isn't managed, sync
  rules, bootstrap vs managed distinction, and common sync failures. Use when
  working with ArgoCD Applications, adding new templates to git, or
  troubleshooting sync issues.
metadata:
  context7-sources:
    - /argoproj/argo-cd
    - /kubernetes/website
    - /websites/argo-cd_readthedocs_io_en_stable
---

# GitOps / ArgoCD — lab Skill

## When to Use

- Adding a new WorkflowTemplate (which git path? ArgoCD managed or bootstrap?)
- Debugging "my template change isn't showing up in the cluster"
- Adding a new CronWorkflow or manifest
- Understanding why ArgoCD reverted a manual change
- Setting up the repo on a new cluster

## When NOT to Use

- Argo Workflows YAML authoring → `argo-workflows.md`
- VM provisioning failures → `kubevirt-vms.md`

## Core Process

### 1. ArgoCD Applications — know which owns what

| Application | Git path | Namespace | What it manages |
|---|---|---|---|
| `testing-lab` | `argo/workflow-templates/` | argo | WorkflowTemplates |
| `testing-lab-infra` | `manifests/` | argo + others | CronWorkflows, RBAC, NodePorts, ConfigMaps |
| `kubestellar-applications` | selected files in `argocd/` | argocd | KubeStellar PostgreSQL, core, and Console Applications |
| `arc-systems` | `argocd/arc-controller-app.yaml` | arc-systems | ARC controller |
| `arc-runners` | `argocd/arc-runners-app.yaml` | arc-runners | Org `ghost-runners` scale set |
| `arc-runners-personal` (optional) | `argocd/arc-runners-personal-app.yaml` | arc-runners | Personal-account scale set |

Managed Applications use `automated: { prune: true, selfHeal: true }` — per
[Argo CD best practices](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).
App-of-apps sync waves require Application health evaluation. The `argocd-cm`
patch in `manifests/argocd-tuning.yaml` marks child Applications Progressing
until they are both Synced and Healthy, so later waves do not race ahead of
PostgreSQL or KubeStellar core.
The root `testing-lab` and `testing-lab-infra` Applications are applied manually once because
ArgoCD cannot create its own initial definitions. `testing-lab-infra` then creates the
`kubestellar-applications` parent, which owns only the three KubeStellar child
Application manifests selected from `argocd/`.

**`prune: true`** — resources removed from git are deleted from the cluster.
**`selfHeal: true`** — manual cluster changes are reverted within ~3 minutes.

`testing-lab-infra` excludes `manifests/flatcar-update-*.yaml`; those resources are owned by the
separate `flatcar-update` Application. Keep that split to avoid duplicate ownership and
persistent `OutOfSync` drift from overlapping Namespace/ConfigMap management.

### 2. The three-path decision tree

```
New file to add to the repo?
        │
        ├─ Runs every pipeline / changes regularly?
        │   └─ → argo/workflow-templates/   (ArgoCD managed, tested in CI)
        │
        ├─ Runs once to set up the cluster?
        │   └─ → argo/bootstrap/            (NOT ArgoCD managed, apply manually)
        │
        └─ Cluster infrastructure (CronWorkflow, RBAC, NodePort, ConfigMap)?
            └─ → manifests/                 (ArgoCD managed via lab-infra)
```

### 3. The deploy loop

```bash
# Edit a WorkflowTemplate
vim argo/workflow-templates/my-template.yaml

# Lint before committing
argo lint --offline argo/workflow-templates/

# Commit and push — ArgoCD polls or webhooks within ~3 minutes
git add . && git commit -m "feat(templates): ..." && git push

# Force sync if you can't wait
just argocd-sync

# Verify
just argocd-status
```

**Never:**
```bash
kubectl apply -f argo/workflow-templates/my-template.yaml   # ✗ ArgoCD reverts this
argo template create argo/workflow-templates/my-template.yaml  # ✗ same
```

### 4. Bootstrap templates — manual, not GitOps

`argo/bootstrap/` is intentionally **outside** all ArgoCD Applications. These templates
are applied once during cluster setup and left in the cluster as runnable runbooks:

```bash
# Apply all bootstrap templates (once, during cluster setup)
kubectl apply -f argo/bootstrap/ -n argo

# Or just one:
argo submit --from workflowtemplate/install-kubevirt -n argo --wait --log
```

If you add a template to `argo/bootstrap/` and push to main, ArgoCD does nothing —
you must still apply it manually.

### 5. manifests/ uses ServerSideApply

`manifests/` has `ServerSideApply: true` in the ArgoCD Application. This means
manifests **patch** resources rather than replace them. You can add a single key to
a Helm-managed ConfigMap without owning the whole object.

**Consequence:** `generateName:` is forbidden in `manifests/` — ArgoCD needs stable
names to track resources. Always use a fixed `name:`.

**Dynamic ConfigMap Key Avoidance:** When a ConfigMap tracks dynamic, runtime-managed state (such as image digests or the last-seen kernel version), do **not** declare placeholder keys (e.g., `kernel-stable: ""`) in the Git manifest's `data:` block. Under Server-Side Apply, defining a field in Git forces ArgoCD to continuously reconcile and overwrite that specific field, resetting it to the placeholder and triggering infinite polling or build loops. To prevent this, omit the dynamic keys entirely from the Git manifest. Server-Side Apply will bootstrap the empty ConfigMap object and leave dynamically added keys untouched at runtime.

**Exception — intentional runtime-state ConfigMap contract:** If the Git manifest must define an explicit set of runtime keys (for example, to document a lifecycle-state contract with known empty marker keys), scope an `ignoreDifferences` rule to that one ConfigMap and ignore `/data`, then enable `RespectIgnoreDifferences=true` on the Application. This keeps the key contract in git without ArgoCD patching live runtime values back to placeholders.

**Singleton local-path PVC rollout:** When an existing Server-Side Apply managed
Deployment moves from `emptyDir` to a `local-path` `ReadWriteOnce` PVC, keep
`RollingUpdate` but set `maxSurge: 0` and `maxUnavailable: 1`. Changing the live
strategy to `Recreate` in one SSA operation retains the old `rollingUpdate`
field until validation and Kubernetes rejects the update. Zero surge deletes
the old singleton before creating its replacement, avoiding RWO contention
without a two-stage migration. Leave placement to `WaitForFirstConsumer` and
the scheduler; do not add a hostname selector. Size application retention below
PVC capacity so WAL, compaction, or other temporary files cannot fill the
volume.

**Subdirectories and namespaces:** `testing-lab-infra` runs with
`directory.recurse: true` (live-patched 2026-07-25; the Application object is
manually applied, not git-tracked) so nested paths like
`manifests/catalog-apps/<app>/manifest.yaml` sync. It also sets
`CreateNamespace=false` — any bundle targeting a new namespace must include
its own `Namespace` object or the sync fails. Symptom of a missing recurse
flag: files merge to a subdirectory, app reports Synced/Healthy, but the
resources silently never appear.

### 6. Sync status and forced sync

```bash
# Check status
just argocd-status
# or
argocd app get testing-lab
argocd app get testing-lab-infra

# Force sync
just argocd-sync
# or
argocd app sync testing-lab testing-lab-infra --timeout 120
```

If a template change is in git but not yet live:
1. Check `argocd app get testing-lab` — is it Synced?
2. If OutOfSync, run `just argocd-sync`
3. If sync fails, check ArgoCD logs: `kubectl logs -n argocd -l app.kubernetes.io/name=argocd-application-controller`

#### Diagnosing a repo-specific reconciliation stall (not a full ArgoCD outage)

A merged PR can sit unsynced for 10+ minutes even though ArgoCD itself is
healthy. Don't assume the whole controller is down — check whether the stall
is scoped to one repo:

```
kubernetes-mcp-resources_list apiVersion=argoproj.io/v1alpha1 kind=Application namespace=argocd
```

Compare the `REVISION` column across all Applications. If Applications
tracking a *different* repo (e.g. `flatcar-update`) pick up new revisions
during the same window while every Application tracking `projectbluefin/lab`
(`testing-lab`, `testing-lab-infra`, `kubestellar-applications`) stays pinned
to the same old commit SHA across two or more subsequent merges to `main`,
that's a **repo-specific** reconciliation stall, not a generic ArgoCD
failure — and not something a hard-refresh from an individual contributor
should paper over, since it likely has a shared root cause other in-flight
work also depends on. Confirm via `git log --format='%H %ci %s' origin/main`
that the stuck revision is genuinely behind, then treat the fix as merged and
correct, but flag live-verification as blocked pending reconciliation, rather
than forcing a resync that may mask the underlying problem.

#### The WorkflowTemplate Snapshot Gotcha (CRITICAL):
- **Snapshot at Submit Time**: In Argo Workflows, a WorkflowTemplate is snapshotted inside the cluster at the *exact moment a workflow is submitted*.
- **Sync Race Condition**: If you push a fix to git and immediately run `argo submit` or trigger a build, the workflow may snapshot a stale template version if ArgoCD has not yet completed its poll or sync loop.
- **Native Kubernetes Hard Sync Patch**: When a port-forward is unavailable or the local CLI config is out-of-sync, you can bypass the `argocd` CLI and trigger an immediate hard refresh and synchronization of the `testing-lab` (or other) Application directly via `kubectl`:
  ```bash
  kubectl patch app testing-lab -n argocd -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}' --type=merge
  ```
  Always run this patch (or `just argocd-sync`) and verify that the target template's live version (`argo-mcp-get_workflow_template`) incorporates your changes **before** submitting or resubmitting any workflow runs.

### 6b. Port-forward recovery and hard refresh

If the local Argo CD port-forward drops or the CLI cannot reach the API, restart the
forward and verify the server before forcing a sync:

```bash
kubectl -n argocd port-forward svc/argocd-server 18080:80
curl -sf http://127.0.0.1:18080/healthz
```

When the forward is healthy, refresh the Application state before resubmitting a workflow:

```bash
argocd app get testing-lab --refresh --hard-refresh
# or, if the CLI is unavailable, trigger the same refresh via kubectl:
kubectl -n argocd patch application testing-lab \
  --type=merge -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

Use this when a template change is already in git but the live template still appears stale;
the refresh ensures the next sync uses the latest repo content rather than the previous
cached manifest snapshot.

### 7. OCI Helm chart Applications (arc-systems, arc-runners)
For the GitHub Actions Runner Controller setup see [arc-runners.md](arc-runners.md).

### 9. Suspending a broken CronWorkflow permanently

When a CronWorkflow always fails (upstream image broken, build blocked), suspend it **in git**
— a `kubectl patch` will be reverted by ArgoCD selfHeal within ~3 minutes.

```yaml
# In manifests/<name>.yaml — add spec.suspend: true
spec:
  suspend: true       # ArgoCD enforces this; removes it to re-enable
  schedules:
    - "0 * * * *"
```

Commit and push. ArgoCD sets the CronWorkflow's suspend flag and stops scheduling new runs.

**Suspend vs delete:** temporarily broken → suspend. Permanently abandoned → delete the file; ArgoCD prune removes the CronWorkflow.

**Currently suspended:** see the full list in
[`docs/reference/workflow-reference.md`](../../reference/workflow-reference.md#cache-warming-pollers)
("Suspended in the 2026-08 bandwidth cuts"). Notable: `dakota-commit-poller`
(suspended in #609 while the QA lane still requires `bootc install to-disk` on a
dakota image without UKI support) and the aurora/KDE cron cluster.

### 9. Taking GitOps ownership of unmanaged Deployments/Services

When a Deployment or Service exists in the cluster but has no manifest in git (e.g. was created
with `kubectl apply` or a Helm one-shot and then forgotten), ArgoCD will not manage or prune it
unless you add a manifest.

Pattern:
1. Inspect the running resource: `kubectl get deploy <name> -n <ns> -o yaml`
2. Strip generated fields (`resourceVersion`, `uid`, `creationTimestamp`, `managedFields`, `status`)
3. Write the clean manifest to `manifests/<name>.yaml`
4. Commit and push. ArgoCD SSA will take ownership on next sync without restarting the pod (if the spec is identical).
5. **Name the Deployment and Service identically to the existing resource** — SSA patches in place rather than recreating.

> ⚠️ If you change the image or spec while adopting, ArgoCD will roll the pod. Safe for stateless
> workloads; for stateful ones (writable registries, DBs) verify data path continuity first.

### 10. Verify live state before reporting — the four-step check

After pushing a fix and forcing sync, **always** verify the live template before
reporting the fix is deployed or resubmitting a workflow:

```bash
# 1. Confirm git push landed
git log -1 origin/main -- <file>

# 2. Confirm ArgoCD synced the revision
just argocd-status   # both apps: Synced + Healthy

# 3. Confirm the live template has your change
argo-mcp-get_workflow_template name=<template> namespace=argo
# grep for the exact changed value — don't assume

# 4. Submit NEW workflow (templates snapshot at submit time)
# A workflow submitted before step 3 runs the OLD template
```

Reporting "fix deployed" after steps 1-2 only — without step 3 — is a false report.
Submitting a workflow before step 3 wastes a run on the old bug.

This protocol was established after multiple sessions where:
- A fix was pushed and ArgoCD appeared synced, but a field manager conflict meant
  the live Deployment still had the old value
- A new workflow was submitted immediately after push but before ArgoCD synced,
  running the stale template for the full pipeline duration

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I'll apply it manually just this once." | selfHeal: true will revert it within minutes. Use git. |
| "It's in bootstrap/ so ArgoCD won't prune it from the cluster." | Correct — but you still have to `kubectl apply -f argo/bootstrap/ -n argo` to put it there. |
| "I pushed to a feature branch — why isn't it live?" | Both Applications track `main`. Feature branch changes don't sync. |
| "The diff looks right in `argocd app diff`." | Diff shows desired vs actual. Sync makes it actual. |

## Red Flags

- A Deployment or Service running in the cluster with no corresponding manifest in `manifests/` — it is invisible to ArgoCD, will not be pruned or healed, and drifts silently (e.g. `registry:2` ran unmanaged for 18+ days)
- A WorkflowTemplate in `argo/workflow-templates/` that exists only in the cluster (not in git) — ArgoCD will prune it on next sync
- `generateName:` in any file under `manifests/`
- A template that was `kubectl apply`d and is showing as OutOfSync in ArgoCD
- Bootstrap templates accidentally placed in `argo/workflow-templates/` (ArgoCD will prune them if removed from git)
- Reporting a fix as deployed without verifying `argo-mcp-get_workflow_template` shows the new value live
- Submitting a new workflow immediately after a push without waiting for ArgoCD sync confirmation
- **automountServiceAccountToken: false on components that need the Kubernetes API** — setting this globally on a Deployment/StatefulSet pod template blocks all containers (including init containers like `secret-init` in `argocd-redis`) from obtaining their service account tokens. If they run CLI commands (such as `argocd admin redis-initial-password`) that access the cluster API, they will fail immediately with configuration or authentication errors. Always use `automountServiceAccountToken: true` for components that interact with the Kubernetes control plane.
- **Every sync event says "Partial sync operation" and new commits never land** — self-heal is loop-syncing one permanently-drifted resource (check `.status.operationState.operation.sync.resources` and `autoHealAttemptsCount`), pinned to an old revision, which starves full syncs. Known trigger: StatefulSet `volumeClaimTemplates` — the API server injects `apiVersion`/`kind`/Status per template, which reads as permanent drift under server-side diff. Fix: `ignoreDifferences` with `jqPathExpressions: [.spec.volumeClaimTemplates[]?.apiVersion, ...kind, ...status]` plus `RespectIgnoreDifferences=true` (see `argocd/infra-application.yaml`).
- **Missing `arc-github-secret` after ARC reinstall** — the `ghost-runners` scale set cannot create a listener or runner pods. Recreate the secret with `scripts/setup-arc-github-secret.sh` (or the `just setup-arc-github-secret` helper). The script now supports non-interactive recovery with `--app-id`, `--installation-id`, `--pem-path`, or the matching `ARC_*` environment variables, while preserving the interactive fallback when those inputs are omitted. It still requires `gh` CLI auth and a GitHub App private key generated in the App UI; do not hard-code the App ID or Installation ID in docs or scripts.
- **NOAUTH errors in application-controller logs** — the SSA patch in `manifests/argocd-tuning.yaml` owns the controller container spec; any apply that omits `env` wipes the upstream env list including `REDIS_PASSWORD`. The patch must always declare the `REDIS_PASSWORD` secretKeyRef. Symptom: "DiffFromCache error ... NOAUTH Authentication required", degraded/partial syncs.
- **Using `registry.access.redhat.com` (UBI) or `bitnami/*` images** — both banned in this cluster. Use organization-owned containers (`ghcr.io/projectbluefin/lab-runner:latest`) or fallback to `cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795` instead.
- **Choosing a base image without checking the policy** — preference order is: organization-owned FSDK containers (`ghcr.io/projectbluefin/lab-runner:latest`) first, then `cgr.dev/chainguard/*` for general/system infra. Fedora images are allowed when appropriate (not replaced with non-existent alternatives). docker.io is banned except `docker.io/rocm/k8s-device-plugin` (annotate `# registry-lint-ignore`).

## Image Policy

Registry and base-image rules for lab images are documented in [image-policy.md](image-policy.md).

## Verification

Before merging a GitOps change:

- [ ] No Deployment or Service in `local-registry`, `argo`, or other managed namespaces exists only in the cluster — verify with `kubectl get deploy,svc -n <ns>` vs git
- [ ] New WorkflowTemplate is in the correct path (`workflow-templates/` vs `bootstrap/`)
- [ ] `argo lint --offline argo/workflow-templates/` passes
- [ ] No `generateName:` in `manifests/`
- [ ] Pushed to `main` (not a feature branch)
- [ ] After push: `just argocd-status` shows both Applications as `Synced` and `Healthy`
- [ ] `argo-mcp-get_workflow_template name=<template> namespace=argo` confirms the exact changed value is live (not just that sync completed)
- [ ] Any new workflow submitted AFTER the above two checks pass
