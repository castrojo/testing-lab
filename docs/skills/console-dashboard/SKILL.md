---
name: console-dashboard
description: >
  KubeStellar Console operations: deploy/upgrade via ArgoCD, auth and
  exposure policy, marketplace cards, guided missions, and the GitOps vs
  imperative action split. Use when working on the Console deployment or
  wiring dashboard surfaces.
---

# KubeStellar Console — lab Skill

## When to Use

- Deploying, upgrading, or recovering the Console
- Wiring new dashboard surfaces (marketplace cards, missions)
- Questions about auth, exposure, or which actions are GitOps vs imperative

## When NOT to Use

- Public read-only status site → `astro-dashboard-pages/SKILL.md` (the
  Astro Pages site stays the reporting layer; Console is the control layer)
- BindingPolicy/WEC mechanics → `kubestellar/SKILL.md`
- Grafana or another general-purpose admin/dashboard framework → do not add
  one; Prometheus is a backend service, not a parallel UI

## Architecture boundary

KubeStellar Console is the sole private cluster-admin and single-pane UI.
Astro remains public and read-only, and Prometheus remains backend-only. Target
the local `ghost` k3s topology (`wds1`, `its1`, and `ghost` as the WEC); do not
turn cloud or external multi-cluster possibilities into lab requirements.

## Core Process

1. Confirm the pinned Console chart version in
   `argocd/kubestellar-console-app.yaml`.
2. Verify dashboard IDs and Helm values against that exact upstream tag.
3. Configure supported built-ins through `enabledDashboards`; expose additional
   cluster data through precise read-only Kubernetes API permissions.
4. For node hardware and utilization surfaces, verify the dependency chain:
   metrics-server API → Console `metrics.k8s.io` RBAC, plus Node Feature
   Discovery workers → `feature.node.kubernetes.io/*` labels on every node.
5. Keep demo data and dynamic cards disabled, and make all desired-state
   changes through Git for ArgoCD reconciliation.
6. Run `just lint`, client-side Kubernetes YAML/RBAC checks, and
   `git diff --check`.

## Deployment

ArgoCD Application `argocd/kubestellar-console-app.yaml`, continuously
reconciled through the GitOps-managed KubeStellar parent Application. Do not
apply it manually. The `kubestellar-console` chart comes from
`https://kubestellar.github.io/console`, pinned version. Namespace
`kubestellar-console`.

Lab-specific values (do not regress these):

- `persistence.accessModes: [ReadWriteOnce]` + `deployment.strategy.type:
  Recreate` — local-path has no RWX; Recreate avoids the chart-documented
  RWO RollingUpdate mount deadlock.
- `backup.enabled: false` — the chart's backup PVC hardcodes ReadWriteMany
  which local-path cannot provision. DB protection belongs to the
  storage/backup ADR (Velero, user-data-only).
- `service.type: ClusterIP`, `ingress.enabled: false`.
- `clusterName: ghost` and `consoleProject: kubestellar`.
- `enabledDashboards: "dashboard,clusters,cluster-admin,deploy,compute,workloads,deployments,pods,services,nodes,events,gitops,helm,operators,security,storage,network"` — restricts the sidebar to supported built-ins useful for local cluster administration. Do not enable demo or local-agent-only surfaces such as `arcade` or `quantum`.
- `selfUpgrade.enabled: false` and `rbac.resourceQuotasReadOnly: true` — keep
  declarative upgrades and quota changes in Git instead of creating Console
  mutations that ArgoCD immediately reverts.
- Do not configure `kubeconfig.content` for the single-cluster install. Chart
  0.3.34 sets `KUBECONFIG` when it is present, which makes `/health` report
  `in_cluster: false` and routes cards to local-agent/demo fallbacks instead of
  the pod ServiceAccount-backed API.

## Exposure and auth policy (locked, ADR-0003)

Dev-mode = anonymous cluster-admin. It NEVER ships exposed. Current access:

```bash
kubectl -n kubestellar-console port-forward svc/kubestellar-console 8080:8080
# http://localhost:8080
```

Before any ingress/Gateway exposure: create a GitHub OAuth app, store
client id/secret in a Secret referenced via `github.existingSecret`, keep
`auth.allowedGitHubLogins` / `adminGitHubLogins` tight. No exceptions.

## Control model (dual-mode)

- **GitOps actions**: anything declarative — app installs, BindingPolicy
  changes, manifest edits — go through PRs to this repo; ArgoCD syncs;
  KubeStellar downsyncs. The Console links/renders state.
- **Imperative actions**: runtime operations — VM start/stop (KubeVirt
  `spec.running` patch), workflow resubmit, pod logs/exec, rollout
  actions — the Console performs directly via its ServiceAccount.
- Rule of thumb: if it should survive a cluster rebuild, it goes through
  git; if it's an operation on live state, imperative is fine.

## Surfaces

- Multi-cluster resource views cover CRDs generically: ArgoCD Applications,
  Argo Workflows, KubeVirt VMs all appear as resources with status.
- The Console ServiceAccount's chart RBAC reads core resources only. Lab
  surfaces come from `manifests/kubestellar-console-crd-read.yaml`
  (ClusterRole `kubestellar-console-lab-surfaces`, GitOps-managed): CRD
  discovery + read on Argo/ArgoCD/KubeVirt/KubeStellar/OCM groups, plus the
  imperative verbs listed below. If a surface shows "no resources", check
  that ClusterRole before debugging the Console.
- Marketplace cards (kubestellar/console-marketplace) fill dedicated views
  (GitOps, networking, security). Prefer a marketplace card over custom UI.
- Console 0.3.34 does not provision external dashboard/card JSON from files or
  ConfigMaps. The supported contract is the built-in dashboard allowlist plus
  live Kubernetes API resources. Keep `DISABLE_DYNAMIC_CARDS=true`; do not add
  declarative card JSON until the pinned Console release supports a real
  Git-backed provisioning path.

### Per-surface action wiring

| Surface | View in Console | Imperative (Console SA verb) | GitOps (PR to this repo) | Deep-dive fallback |
|---|---|---|---|---|
| ArgoCD apps | Applications CRD status | none — read-only by design | edit `argocd/`, `manifests/`, `argo/workflow-templates/` | ArgoCD UI: `kubectl -n argocd port-forward svc/argocd-server 8081:443` |
| Argo Workflows | Workflow/CronWorkflow CRDs | `create workflows` (resubmit/submit-from) | edit templates in `argo/workflow-templates/` | Argo UI: `kubectl -n argo port-forward svc/argo-server 2746:2746`; `argo logs` |
| KubeVirt VMs | VirtualMachine CRDs | `patch virtualmachines` (`spec.running` start/stop/restart) | VM definitions in `manifests/` | `virtctl console` / `virtctl vnc` |
| KubeStellar | ManagedClusters (its1 surfaces on host) | none | BindingPolicies in git, downsynced via wds1 | `kubestellar/SKILL.md` |
| Catalog apps | index at `docs/data/catalog/*.json` | install workflow (imperative mode) | install workflow (gitops mode, default capture) | `registry-catalog` skill (when it lands) |
| Host OS updates (bootc) | none — stays on the Astro Pages site (decision: lab update data already feeds Astro; no Console card duplication) | none | image/branch changes via factory repos | Astro dashboard, `bluefin.io/*` lanes |
| BuildStream builds | Workflow CRD status (dakota pipeline runs) | `create workflows` (rerun via submit-from) | pipeline templates in Git | BuildBarn/artifact-cache details remain available on Astro |

BindingPolicy note: the `control.kubestellar.io` CRDs live in **wds1**, not
the hosting cluster — `kubectl auth can-i get bindingpolicies` on ghost
correctly answers no. The Console reaches wds1 as a registered cluster.
- **Missions** (`kc-mission-v1`): step sequences with `yaml` (one-click
  apply) and `command` blocks; custom missions are shareable via built-in
  PR flow. The "add your second PC" onboarding flow is committed as
  [`missions/add-your-second-pc.json`](../../../missions/add-your-second-pc.json).
  The Console v0.3.34 does not read custom missions from a ConfigMap/CRD yet;
  import it via **Missions > Local Files > Import**. See
  `node-lifecycle/SKILL.md` for the agent-executable version.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| Pod Pending, backups PVC Pending | backup re-enabled: its PVC is RWX-hardcoded; keep `backup.enabled: false` |
| New pod stuck ContainerCreating on upgrade | strategy reverted to RollingUpdate with RWO PVC; keep Recreate |
| Console shows sample data instead of ghost | remove chart `kubeconfig` values and verify `/health` reports `in_cluster: true`; clear the browser's stale `kc-demo-mode`/auth state once after rollout |
| Browser reloads `/login` while APIs return `token signature is invalid` | the chart-generated JWT changed while the browser retained stale auth. Clear local-storage keys `token`, `auth_token`, `kc_token`, `kc-auth-token`, `kc-has-session`, and `kc-demo-mode`, plus the `kc_auth` and `kc_ux_ctx` cookies for the Console origin. The v0.3.34 logout request cannot recover because stale auth fails before its CSRF-protected cookie cleanup runs. |
| CPU and memory monitors show zero although `kubectl top nodes` works | metrics-server is healthy, but the Console ServiceAccount cannot list `nodes` and `pods` in `metrics.k8s.io`; retain that scoped read permission in `kubestellar-console-lab-surfaces` |
| Hardware inventory has no PCI, storage, CPU, or kernel features | the cluster lacks Node Feature Discovery labels; keep the pinned `node-feature-discovery` ArgoCD Application healthy and require ready workers plus `feature.node.kubernetes.io/*` labels in acceptance |
| ArgoCD reports a duplicate-env ComparisonError | `NO_LOCAL_AGENT` was added to `extraEnv`; remove it because chart 0.3.34 emits it |
| PVC migration hook is ImagePullBackOff on `bitnami/kubectl:latest` | retain the narrowly scoped `kubestellar-console-pvc-migration-image` admission policy; chart 0.3.34 hardcodes the image and offers no values override |
| Console continuously syncs and reruns the PVC migration hook | retain the scoped JWT Secret ignore and `RespectIgnoreDifferences=true`; ArgoCD cannot use the chart's live `lookup`, so its random fallback otherwise changes every render |
| `/api/health` returns "Missing authorization" | expected — auth is enforced; sign in or use a token |
| Anonymous full access | dev-mode without OAuth config — acceptable ONLY via port-forward, never exposed |

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "A card JSON ConfigMap is declarative, so Console will discover it." | Console 0.3.34 has no such provisioning contract; use a supported built-in or Kubernetes API surface. |
| "The project preset is close enough." | It includes demo surfaces; set an explicit cluster-admin allowlist. |
| "Read access can use another wildcard." | Add only the API groups and resource plurals the surface actually queries. |

## Red Flags

- External dashboard or card JSON claimed as automatically provisioned
- `arcade`, `quantum`, or other demo/local-agent-only dashboards enabled
- Invented Prometheus metrics without a live exporter
- Grafana or another general-purpose cluster-admin/dashboard framework
- Dev-mode Console exposed beyond a local port-forward

## Verification

- [ ] Every `enabledDashboards` ID exists in the pinned Console source
- [ ] Demo and dynamic-card environment flags remain disabled
- [ ] Required CRDs have only `get`, `list`, and `watch` access
- [ ] No unsupported card JSON/ConfigMap manifests remain
- [ ] Browser requests do not loop on `401 token signature is invalid`
- [ ] `metrics.k8s.io/v1beta1/nodes` returns non-zero CPU and memory usage
- [ ] Every node has NFD `feature.node.kubernetes.io/*` labels
- [ ] `just lint` and `git diff --check` pass

## Sources

- KubeStellar Console v0.3.34 chart values:
  `deploy/helm/kubestellar-console/values.yaml`
- KubeStellar Console v0.3.34 built-in project dashboards:
  `pkg/api/projects.go` and `web/src/config/routes.ts`
- Context7 `/kubestellar/kubestellar` documents the WDS → ITS → WEC model but
  has no Console library entry; verify Console behavior against the pinned
  upstream tag instead.
- Context7 `/kubernetes-sigs/node-feature-discovery` documents the CPU, kernel,
  memory, network, PCI, storage, system, and USB feature sources used by NFD.

## Upgrade

Bump `targetRevision` in argocd/kubestellar-console-app.yaml via PR. Order:
KubeFlex/postgres → core-chart → Console. After upgrade verify pod
Running, `/` returns 200, auth still enforced.
