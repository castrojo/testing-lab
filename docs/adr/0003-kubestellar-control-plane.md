# ADR 0003 — KubeStellar control plane and Console dashboard

Status: Accepted
Date: 2026-07-24

## Context

Bluefin Server needs a unified, multi-node home control plane: one GUI for
every node, service, container, and VM ("Proxmox killer" positioning). This
lab is the proving ground. Research (mid-2026) compared KubeStellar Console,
Headlamp, and the archived kubestellar/ui + Headlamp plugin.

This is a Kubernetes tech showcase: CNCF-first, supplant Proxmox features
rather than clone them.

## Decisions

### Topology: shared cluster grows node by node; KubeStellar federates clusters

- A user's additional PCs join the existing k3s cluster as nodes (k3s agent
  token). Live migration and single scheduling domain preserved.
- KubeStellar's WDS/ITS/WEC model federates *clusters* (home cluster, ghost
  lab, future sites) — not individual PCs.
- KubeStellar core v0.29.0 via the OCI Helm core-chart, deployed by ArgoCD
  (`argocd/kubestellar-app.yaml`): `its1` (type `host`, reuses ghost's API
  server) + `wds1` (type `k8s`, KubeFlex-hosted API server).
- ghost self-registers as the first WEC via OCM `clusteradm` (bootstrap
  workflow `register-wec`).

### Status upsync

BindingPolicies for single-WEC workloads set `wantSingletonReportedState:
true` so the real WEC status upsyncs into the WDS object — this is how
ArgoCD (watching the WDS) sees true workload health.

### GUI: KubeStellar Console

- KubeStellar Console (github.com/kubestellar/console) is the control GUI:
  actively maintained, multi-cluster, imperative-capable (edit/patch/delete,
  logs, exec), marketplace, missions engine for guided flows.
- In-cluster Helm deploy needs no kc-agent (uses ServiceAccount).
- Auth: GitHub OAuth with allowlists from day one. Dev-mode (anonymous
  cluster-admin) never ships exposed.
- Archived and banned: kubestellar/ui, kubestellar/ui-plugins, all KCP-era
  docs. Headlamp is a fallback only if the Console evaluation gate finds
  gaps in raw resource operations.
- The Astro Pages dashboard remains the public read-only reporting surface;
  the Console is the private control surface.

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
- Storage decision deferred to a future ADR (local-path + Velero data-only
  is the leading candidate under the reproducible-everything stance).
