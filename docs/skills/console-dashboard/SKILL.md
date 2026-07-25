---
name: console-dashboard
description: >
  KubeStellar Console operations: deploy/upgrade via ArgoCD, auth and
  exposure policy, marketplace cards, guided missions, and the GitOps vs
  imperative action split. Use when working on the Console deployment or
  wiring dashboard surfaces.
metadata:
  context7-sources:
    - /kubestellar/console
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

## Deployment

ArgoCD Application `argocd/kubestellar-console-app.yaml` (applied manually
once), chart `kubestellar-console` from
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

### Per-surface action wiring

| Surface | View in Console | Imperative (Console SA verb) | GitOps (PR to this repo) | Deep-dive fallback |
|---|---|---|---|---|
| ArgoCD apps | Applications CRD status | none — read-only by design | edit `argocd/`, `manifests/`, `argo/workflow-templates/` | ArgoCD UI: `kubectl -n argocd port-forward svc/argocd-server 8081:443` |
| Argo Workflows | Workflow/CronWorkflow CRDs | `create workflows` (resubmit/submit-from) | edit templates in `argo/workflow-templates/` | Argo UI: `kubectl -n argo port-forward svc/argo-server 2746:2746`; `argo logs` |
| KubeVirt VMs | VirtualMachine CRDs | `patch virtualmachines` (`spec.running` start/stop/restart) | VM definitions in `manifests/` | `virtctl console` / `virtctl vnc` |
| KubeStellar | ManagedClusters (its1 surfaces on host) | none | BindingPolicies in git, downsynced via wds1 | `kubestellar/SKILL.md` |
| Catalog apps | index at `docs/data/catalog/*.json` | install workflow (imperative mode) | install workflow (gitops mode, default capture) | `registry-catalog` skill (when it lands) |

BindingPolicy note: the `control.kubestellar.io` CRDs live in **wds1**, not
the hosting cluster — `kubectl auth can-i get bindingpolicies` on ghost
correctly answers no. The Console reaches wds1 as a registered cluster.
- **Missions** (`kc-mission-v1`): step sequences with `yaml` (one-click
  apply) and `command` blocks; custom missions are shareable via built-in
  PR flow. The "add your second PC" onboarding flow is committed as
  [`missions/add-your-second-pc.json`](../../missions/add-your-second-pc.json).
  The Console v0.3.34 does not read custom missions from a ConfigMap/CRD yet;
  import it via **Missions > Local Files > Import**. See
  `node-lifecycle/SKILL.md` for the agent-executable version.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| Pod Pending, backups PVC Pending | backup re-enabled: its PVC is RWX-hardcoded; keep `backup.enabled: false` |
| New pod stuck ContainerCreating on upgrade | strategy reverted to RollingUpdate with RWO PVC; keep Recreate |
| `/api/health` returns "Missing authorization" | expected — auth is enforced; sign in or use a token |
| Anonymous full access | dev-mode without OAuth config — acceptable ONLY via port-forward, never exposed |

## Upgrade

Bump `targetRevision` in argocd/kubestellar-console-app.yaml via PR. Order:
KubeFlex/postgres → core-chart → Console. After upgrade verify pod
Running, `/` returns 200, auth still enforced.
