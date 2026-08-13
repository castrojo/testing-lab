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
    - /argoproj/argo-cd
    - /websites/argo-cd_readthedocs_io_en_stable
    - /websites/prometheus_io
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

## Core Process

1. Change the KubeStellar child Application manifests in `argocd/`.
2. Let `kubestellar-applications` reconcile PostgreSQL, core, then Console.
3. Submit `register-wec` for registration or `kubestellar-platform-verify` for
  ordered platform and smoke acceptance from their managed WorkflowTemplates.
4. Verify ArgoCD health, control-plane readiness, and WEC availability.

## Install (GitOps, ADR-0003)

The `lab-infra` Application reconciles
`manifests/kubestellar-applications.yaml`, which owns exactly three child
Applications in order: PostgreSQL, KubeStellar core, then Console. Installation
and upgrades happen through Git; do not apply the child Applications manually.

```bash
argocd app get kubestellar-applications
argocd app get kubestellar-postgres
argocd app get kubestellar
argocd app get kubestellar-console
```

**Critical gotcha — postgres deadlock**: the core-chart installs PostgreSQL
via a `helm.sh/hook: post-install` Job. Under ArgoCD that hook maps to
PostSync, which never fires because kubeflex-controller-manager's
`wait-postgresql` init container keeps the sync from turning healthy.
Hence: `kubeflex-operator.installPostgreSQL: false` in the core app and a
separate `kubestellar-postgres` Application whose Helm release name MUST be
`postgres` (the operator waits for pod `postgres-postgresql-0`).

**Critical gotcha — PostCreateHook drift**: KubeFlex expands
`PostCreateHook.spec.templates` after creation. The core Application must ignore
that controller-owned field and set `RespectIgnoreDifferences=true`; otherwise
the core remains OutOfSync and the app-of-apps sync wave never advances to
Console.

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

What it does (`argo/workflow-templates/register-wec.yaml`): pinned clusteradm download
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

For the full ordered gate, use:

```bash
just run-kubestellar-verify
```

This verifies the datasource, API/PromQL query surfaces, and all five
controller scrape jobs before composing `kubestellar-smoke-test` as the final
gate. Read-only checks use `kubestellar-observability`; the referenced smoke
template declares `kubestellar-bootstrap` at template level because
`templateRef` does not inherit WorkflowTemplate-level identity.

## BindingPolicy authoring rules

- Select clusters by ManagedCluster labels (`name: ghost`, later
  `location-group: ...`). Scheduler-driven; never pin by hostname fields.
- Always `wantSingletonReportedState: true` for singleton placements.
- Workload objects need labels the `objectSelectors` match — namespace AND
  the objects, or the namespace arrives without contents.
- BindingPolicies live in the WDS, committed to git like any manifest once
  the GitOps lane for wds1 exists.

## Controller metrics

The lab uses the standalone `prometheus-lightweight` deployment, not
Prometheus Operator resources. `manifests/kubestellar-controller-metrics.yaml`
provides Services for the transport controller, status addon controller, and
status agent; the existing KubeFlex and KubeStellar controller-manager Services
provide their kube-rbac-proxy endpoints. Scrape jobs are named
`kubestellar-*`; query controller-runtime, process, REST client, and workqueue
families rather than inventing BindingPolicy or ManifestWork gauges. Prometheus
`role: endpoints` discovery attaches service and backing-pod metadata, so filter
on service and endpoint port names and relabel only namespace, service, pod, and
instance.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| App sync stuck "waiting for healthy state of kubeflex-controller-manager" | postgres deadlock — see install section; check `kubestellar-postgres` app is Synced/Healthy |
| Parent app stuck "waiting for healthy state of Application/kubestellar" | KubeFlex mutated `PostCreateHook.spec.templates`; retain the scoped ignore and `RespectIgnoreDifferences=true` in the core Application |
| Prometheus cAdvisor targets stay `unknown` and the pod restarts | check for `OOMKilled`; WAL replay plus the controller and cAdvisor scrape set needs the committed 512 MiB request and 2 GiB limit, not the original demo sizing |
| Prometheus reaches its memory limit after NFD labels appear | never `labelmap` all `__meta_kubernetes_node_label_*` values onto cAdvisor series; map only `__meta_kubernetes_node_name` to `node`, or NFD's 100+ labels multiply across every container metric |
| Prometheus ConfigMap is Synced but scrape behavior does not change | bump `lab.projectbluefin.io/config-version` on the Deployment pod template with every scrape-config change; Prometheus has no config reloader sidecar |
| `kubeflex-controller-manager` sustains a roughly 1 Hz ControlPlane loop, reports `failed to update final status ... object has been modified`, and looks like external bandwidth | KubeFlex v0.9.1 writes ControlPlane status during infrastructure, post-create-hook, and final readiness phases; the resulting status-update race requeues the controller. KubeFlex also generates an `ingressClassName: nginx` Ingress outside the PostCreateHooks, but does not inspect its load-balancer status; the ControlPlane can be `Ready=True` while the Ingress has an empty status. That endpoint is unused in this internal-only lab, and ingress-nginx reached EOL in March 2026 | Upgrade the core chart to 0.30.0, which carries KubeFlex v0.9.3, and let ArgoCD roll the operator. Keep external reachability off by default; never install ingress-nginx or add a class solely to satisfy KubeFlex's hardcoded artifact. Verify `kubectl -n kubeflex-system logs deploy/kubeflex-controller-manager --tail=50` has no status-conflict loop and compare the `container_network_receive_bytes_total` rate before/after. If conflicts persist on v0.9.3, the remaining fix belongs upstream in KubeFlex rather than in an ingress manifest |
| KubeFlex creates an `ingressClassName: nginx` Ingress in an internal-only k3s lab | The hosted ControlPlane reconcilers create this endpoint unconditionally when `isOpenShift=false`; current KubeFlex chart values expose no disable switch, and the object is not a PostCreateHook template. ADR-0004 intentionally provides no external endpoint, while ingress-nginx reached EOL in March 2026 | Do not install ingress-nginx or a replacement Gateway controller just to claim the object. Keep the lab's off-by-default network policy and track an upstream KubeFlex option/removal; deleting the object alone only causes the owner controller to recreate it |
| `clusteradm get token` forbidden | workflow ran as `argo` SA; needs `serviceAccountName: kubestellar-bootstrap` |
| Workflow pod rejected "failed quota: argo-quota" | missing resources requests/limits on the template |
| Downsynced namespace exists but is empty | objectSelectors don't match the inner objects' labels |
| Status never upsyncs | `wantSingletonReportedState` missing, or >1 cluster matched (singleton means one) |
| ManagedCluster stuck JOINED empty | CSR not accepted — rerun `clusteradm accept --clusters <wec>` |
| BindingPolicy matches nothing | ManagedCluster lacks the `name=<wec>` label (register-wec adds it; manual joins must too) |

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "Applying one child Application manually is faster." | The parent self-heals from Git; change the tracked file instead. |
| "These workflows are bootstrap-only." | `register-wec` and `kubestellar-smoke-test` are reusable, ArgoCD-managed templates. |

## Red Flags

- Manual `kubectl apply` of a KubeStellar child Application
- A reusable KubeStellar WorkflowTemplate under `argo/bootstrap/`
- Core synced before the PostgreSQL child Application is healthy

## Upgrade order

KubeFlex/postgres → core-chart (bump `targetRevision` in
argocd/kubestellar-app.yaml via PR) → Console. Rerun the smoke test after
every core upgrade.

## Verification

- [ ] `kubestellar-applications` and all three child Applications are healthy
- [ ] `its1` and `wds1` report Ready
- [ ] The target ManagedCluster reports Joined and Available
- [ ] `kubestellar-smoke-test` passes after a core upgrade
- [ ] KubeFlex logs remain free of the ControlPlane status-conflict loop after a core-chart upgrade
