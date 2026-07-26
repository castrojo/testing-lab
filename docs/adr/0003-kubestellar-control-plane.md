# ADR 0003 — KubeStellar control plane and Console dashboard

Status: Accepted
Date: 2026-07-24

## Context

Bluefin Server needs a unified, multi-node home control plane: one GUI for
every node, service, container, and VM ("Proxmox killer" positioning). This
lab is the proving ground, with the local `ghost` k3s topology as its canonical
target. Research (mid-2026) compared KubeStellar Console, Headlamp, and the
archived kubestellar/ui + Headlamp plugin.

This is a Kubernetes tech showcase: CNCF-first, supplant Proxmox features
rather than clone them.

## Decisions

### Topology: shared cluster grows node by node; KubeStellar federates clusters

- A user's additional PCs join the existing k3s cluster as nodes (k3s agent
  token). Live migration and single scheduling domain preserved.
- The lab implementation targets the local `ghost` k3s cluster, with `wds1`
  and `its1`. Cloud providers and external multi-cluster layouts are not
  assumptions for lab manifests, workflows, or verification.
- KubeStellar's WDS/ITS/WEC model federates *clusters* (home cluster, ghost
  lab, possible future sites) — not individual PCs. That capability does not
  make additional clusters part of the current lab topology.
- KubeStellar core v0.29.0 via the OCI Helm core-chart, deployed by ArgoCD
  (`argocd/kubestellar-app.yaml`): `its1` (type `host`, reuses ghost's API
  server) + `wds1` (type `k8s`, KubeFlex-hosted API server).
- ghost self-registers as the first WEC via OCM `clusteradm` (managed
  WorkflowTemplate `register-wec`).

### Status upsync

BindingPolicies for single-WEC workloads set `wantSingletonReportedState:
true` so the real WEC status upsyncs into the WDS object — this is how
ArgoCD (watching the WDS) sees true workload health.

### GUI: KubeStellar Console

- KubeStellar Console (github.com/kubestellar/console) is the lab's sole
  private cluster-admin and single-pane UI: actively maintained,
  imperative-capable (edit/patch/delete, logs, exec), with a marketplace and
  missions engine for guided flows.
- In-cluster Helm deploy needs no kc-agent (uses ServiceAccount).
- Auth: GitHub OAuth with allowlists from day one. Dev-mode (anonymous
  cluster-admin) never ships exposed.
- Archived and banned: kubestellar/ui, kubestellar/ui-plugins, all KCP-era
  docs.
- The Astro Pages dashboard remains the public read-only reporting surface;
  it does not become a cluster-admin UI.
- Prometheus is a backend metrics service for automation and acceptance
  checks, not a user-facing dashboard. Console 0.3.34 does not provision
  Git-backed Prometheus cards, so its admin views use live Kubernetes APIs.
- Grafana and parallel general-purpose cluster-admin/dashboard frameworks are
  not introduced. Specialized CLI and native troubleshooting views may remain,
  but they do not compete as a second single pane.

### Deliberate drops (supplant, don't clone)

| Proxmox feature | Disposition |
|---|---|
| LXC containers | Dropped — pods are the only container model |
| SPICE desktop streaming | Dropped — headless server VMs; noVNC covers install/debug |
| PBS instant-boot restore | Not needed — everything is reproducible from git + images; backup scope is user data only (PVC snapshots) |
| 2-node corosync quorum | Solved by shipped config — k3s runs 1..n nodes; sqlite single-server or embedded etcd at 3+ |

## Consequences

- ~<2 GiB steady-state RAM for KubeFlex + core + Console; fits N100-class.
- ArgoCD owns the wds1 content path (GitOps); the Console owns imperative
  runtime actions. Both paths documented per surface.
- Metrics remain backend infrastructure; operational UI work extends the
  Console rather than creating a Grafana or alternate dashboard stack.
- Storage decision deferred to a future ADR (local-path + Velero data-only
  is the leading candidate under the reproducible-everything stance).
