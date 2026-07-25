---
name: kubestellar
description: >
  KubeStellar multi-cluster control plane on the lab: WDS/ITS/WEC model,
  install/upgrade via ArgoCD, WEC registration, BindingPolicy authoring,
  smoke testing, and failure modes. Use when working with KubeStellar,
  KubeFlex control planes, OCM ManagedClusters, or BindingPolicies.
metadata:
  context7-sources:
    - /kubestellar/kubestellar
---

# KubeStellar — lab Skill

## When to Use

- Installing, upgrading, or recovering KubeStellar core
- Registering or removing a WEC (Workload Execution Cluster)
- Authoring or debugging BindingPolicies and downsync
- Diagnosing "workload didn't reach the WEC" or "status never came back"

## When NOT to Use

- Console UI operations → `console-dashboard/SKILL.md`
- Adding a k3s node to the shared cluster → `node-lifecycle/SKILL.md`
- General ArgoCD sync issues → `gitops-argocd/SKILL.md`

## Model (30 seconds)

- **WDS** (Workload Description Space, `wds1`): a KubeFlex-hosted API
  server where desired manifests live. ArgoCD and install workflows write
  here, never directly to WECs.
- **ITS** (Inventory & Transport Space, `its1`): the OCM hub — cluster
  registry (`ManagedCluster`) plus transport. Type `host` on this lab: it
  IS the ghost cluster's API server.
- **WEC**: any registered execution cluster. `ghost` self-registers as the
  first WEC. Egress-only from the WEC side (home-firewall friendly).
- **BindingPolicy** (`control.kubestellar.io/v1alpha1`, lives in the WDS):
  `clusterSelectors` (match ManagedCluster labels) x `downsync`
  objectSelectors. Set `wantSingletonReportedState: true` so real WEC
  status upsyncs into the WDS object — without it ArgoCD/Console see specs,
  not truth.

## Install (GitOps, ADR-0003)

Two ArgoCD Applications, applied manually once (argocd/ convention):

```bash
kubectl apply -f argocd/kubestellar-postgres-app.yaml   # MUST exist first
kubectl apply -f argocd/kubestellar-app.yaml
```

Or run the runbook: `argo submit --from workflowtemplate/install-kubestellar -n argo --wait`

**Critical gotcha — postgres deadlock**: the core-chart installs PostgreSQL
via a `helm.sh/hook: post-install` Job. Under ArgoCD that hook maps to
PostSync, which never fires because kubeflex-controller-manager's
`wait-postgresql` init container keeps the sync from turning healthy.
Hence: `kubeflex-operator.installPostgreSQL: false` in the core app and a
separate `kubestellar-postgres` Application whose Helm release name MUST be
`postgres` (the operator waits for pod `postgres-postgresql-0`).

Verify:

```bash
kubectl get controlplanes           # its1 + wds1 SYNCED=True READY=True
kubectl get pods -n kubeflex-system # operator 2/2, postgres-postgresql-0 1/1
kubectl get pods -n wds1-system     # apiserver, controller-manager, kubestellar + transport controllers
```

## Reaching wds1 from inside the cluster

Use the `kubeconfig-incluster` key — the plain `kubeconfig` key points at
`wds1.localtest.me:9443`, unreachable in-cluster:

```bash
kubectl get secret -n wds1-system admin-kubeconfig \
  -o jsonpath='{.data.kubeconfig-incluster}' | base64 -d > /tmp/wds1.kubeconfig
kubectl --kubeconfig /tmp/wds1.kubeconfig get bindingpolicies
```

ClusterIPs are not routable from workstations; do wds1 operations from
in-cluster pods (workflows) only.

## WEC registration

```bash
argo submit --from workflowtemplate/register-wec -n argo --wait --log \
  -p wec-name=<cluster-name>
```

What it does (argo/bootstrap/register-wec.yaml): pinned clusteradm download
(python tarfile extract — lab-runner has no tar), `clusteradm get token` →
`join --singleton --force-internal-endpoint-lookup` → `accept` → labels the
ManagedCluster `name=<wec>` (OCM does NOT set this; lab BindingPolicies
select on it). Runs as the `kubestellar-bootstrap` SA (cluster-admin;
klusterlet install writes CRDs/clusterroles — the scoped `argo` SA cannot).

Verify: `kubectl get managedclusters` → JOINED=True AVAILABLE=True.

## Smoke test (acceptance gate)

```bash
argo submit --from workflowtemplate/kubestellar-smoke-test -n argo --wait --log
```

Applies a BindingPolicy + Namespace + Deployment to wds1, polls for the
downsynced objects on the WEC (downsync is async — never `kubectl wait`
immediately), verifies `readyReplicas` upsyncs back into the wds1 object,
cleans up. Green = downsync AND status upsync both work.

## BindingPolicy authoring rules

- Select clusters by ManagedCluster labels (`name: ghost`, later
  `location-group: ...`). Scheduler-driven; never pin by hostname fields.
- Always `wantSingletonReportedState: true` for singleton placements.
- Workload objects need labels the `objectSelectors` match — namespace AND
  the objects, or the namespace arrives without contents.
- BindingPolicies live in the WDS, committed to git like any manifest once
  the GitOps lane for wds1 exists.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| App sync stuck "waiting for healthy state of kubeflex-controller-manager" | postgres deadlock — see install section; check `kubestellar-postgres` app is Synced/Healthy |
| `clusteradm get token` forbidden | workflow ran as `argo` SA; needs `serviceAccountName: kubestellar-bootstrap` |
| Workflow pod rejected "failed quota: argo-quota" | missing resources requests/limits on the template |
| Downsynced namespace exists but is empty | objectSelectors don't match the inner objects' labels |
| Status never upsyncs | `wantSingletonReportedState` missing, or >1 cluster matched (singleton means one) |
| ManagedCluster stuck JOINED empty | CSR not accepted — rerun `clusteradm accept --clusters <wec>` |
| BindingPolicy matches nothing | ManagedCluster lacks the `name=<wec>` label (register-wec adds it; manual joins must too) |

## Upgrade order

KubeFlex/postgres → core-chart (bump `targetRevision` in
argocd/kubestellar-app.yaml via PR) → Console. Rerun the smoke test after
every core upgrade.
