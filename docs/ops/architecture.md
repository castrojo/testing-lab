# Ghost Lab Architecture

> This document describes the **current, physically-verified** Ghost Lab hardware and
> network topology. Only `ghost` and `exo-0` have a live point-to-point USB4 link today.
> Any multi-node daisy-chain mesh, ZFS storage tier, or Remote Execution grid described
> in earlier drafts of this document was aspirational and did not match deployed hardware
> — it has been removed. Extend this doc only after physically verifying new links/nodes
> (see /docs/ops/RUNBOOK.md:108-116 for the incident this caused previously).

## Hardware

| Host | Role | Form Factor |
|---|---|---|
| `ghost` | k3s control-plane + KubeVirt compute | Existing workstation |
| `exo-0` | k3s worker | Framework Desktop |

Other opt-in workers (`exo-1`, `exo-2`, `hamilton`) join the cluster over
standard Ethernet only — none currently have a physical USB4/Thunderbolt link to `ghost`.

---

## Networking: Dual-Homed k3s (ghost + exo-0 only)

`ghost` and `exo-0` each have two active network interfaces:

### Control Plane + LAN — Gigabit Ethernet

- k3s control-plane traffic (API server, etcd, kubelet heartbeats)
- External LAN access and ingress to cluster services
- All other nodes reach the cluster exclusively over this Ethernet LAN

### Data Plane — Point-to-Point USB4 Link (ghost <-> exo-0 only)

> **STATUS (2026-07-09): LINK UP AND ROUTED.** After the 2026-07-08 EC/PD failure
> (see /docs/ops/RUNBOOK.md), a full power drain of both nodes restored the physical link.
> `thunderbolt0` is up on both hosts, static IPs and table-40 policy routing are
> applied and persisted in NetworkManager, and cross-node pod traffic is confirmed
> flowing over USB4 (~0.16 ms pod-to-pod RTT).
>
> **Important Hardware Slot Mapping**: On AMD Framework laptops (both 13 and 16), only **Slot 1 (back left)** and **Slot 4 (back right)** support USB4 / Thunderbolt 4. If the cable is plugged into Slot 2 or Slot 3, the link state will remain `none` indefinitely. Ensure the USB4 cable is connected strictly to Slot 1 or Slot 4 on both machines to bring the link up and initialize `thunderbolt0`. On Framework Desktops all rear Type-C ports are USB4.
> 
> The complete network design for East-West CNI route separation is fully specified below.

- `ghost`'s `thunderbolt0` (static IP `10.99.0.1/30`) and `exo-0`'s `thunderbolt0`
  (static IP `10.99.0.2/30`) form a direct, switchless point-to-point USB4 link (40 Gbps).
  The IPs are persisted in NetworkManager profiles (`Wired connection 1` on ghost,
  `Wired connection 2` on exo-0) with `ipv4.method manual`, so they survive reboots.
- Intended for pod-to-pod (East-West) traffic between these two nodes specifically:
  build artifact transfer, cache I/O.
- Live link state and its Unix observation time are published as
  `lab.projectbluefin.io/usb4-link: up|down` and
  `lab.projectbluefin.io/usb4-link-observed-at` by the `usb4-link-monitor`
  DaemonSet (`manifests/usb4-link-monitor.yaml`). Every BST pipeline admits
  BuildBarn remote execution only when both node observations are fresh and
  `up`, with a Ready worker on each node. A transport or worker failure fails
  the workflow for repair; Ethernet, local, and cache-only fallbacks are
  forbidden.
- **Traffic Steering Design (Linux Policy Routing)**: Since the default k3s flannel CNI is
  configured with `flannel-backend: host-gw` (Host Gateway, no VxLAN encapsulation), routing
  can be optimized natively at the host kernel layer using policy routing rules (`ip rule`).
  This completely isolates pod-to-pod build traffic over USB4 while guaranteeing that k3s
  control-plane, API server, and etcd traffic remains strictly on the Ethernet LAN.
  
  **Deployed configuration (persisted in NetworkManager, survives reboots)**:

  1. Static IPs live in the NM profiles (`ipv4.method manual`):
     - ghost (`Wired connection 1`): `10.99.0.1/30`
     - exo-0 (`Wired connection 2`): `10.99.0.2/30`

  2. Policy routing on `ghost` routes the `exo-0` pod subnet (`10.42.1.0/24`) over USB4:
     ```bash
     sudo nmcli con mod "Wired connection 1" \
       +ipv4.routes "10.42.1.0/24 10.99.0.2 table=40" \
       +ipv4.routing-rules "priority 5209 to 10.42.1.0/24 table 40"
     ```

  3. Policy routing on `exo-0` routes the `ghost` pod subnet (`10.42.0.0/24`) over USB4:
     ```bash
     sudo nmcli con mod "Wired connection 2" \
       +ipv4.routes "10.42.0.0/24 10.99.0.1 table=40" \
       +ipv4.routing-rules "priority 5209 to 10.42.0.0/24 table 40"
     ```

  ### Persistent DNS Routing Override over Ethernet (Core SRE Guarantee)

  While the USB4 link is highly optimized for high-bandwidth data plane traffic (such as build artifact transfers), routing lightweight control plane discovery traffic—specifically **DNS queries and responses**—over USB4 creates a single point of failure (SPOF) if the USB4 cable is unplugged or drops.

  To isolate control and data plane traffic properly, **all CoreDNS and DNS traffic is routed strictly over the Ethernet LAN**. This is enforced at priority `5208` (higher than the USB4 route priority `5209`), steering port 53 UDP and TCP queries/responses via the `main` table:

  1. **DNS routing rules** applied on both `ghost` and `exo-0`:
     - Outbound queries to CoreDNS: `from all ipproto udp dport 53 lookup main pref 5208`
     - Outbound queries to CoreDNS (TCP): `from all ipproto tcp dport 53 lookup main pref 5208`
     - Outbound replies from CoreDNS: `from all ipproto udp sport 53 lookup main pref 5208`
     - Outbound replies from CoreDNS (TCP): `from all ipproto tcp sport 53 lookup main pref 5208`

  2. **Automated Enforcement (usb4-link-monitor DaemonSet)**:
     These routing overrides are dynamically injected and verified every 15 seconds by the `usb4-link-monitor` DaemonSet using host PID/mount namespace execution (`nsenter` with `/usr/sbin/ip` on the host), making the setup completely self-healing and immune to manual operator changes or NetworkManager profile resets.

  Verify with `ip route get 10.42.1.10 dport 53` on ghost — it must show that it is routed via the Ethernet LAN interface, whereas data-plane IPs route via `thunderbolt0`.

  Verify with `ip route get 10.42.1.10` on ghost — it must show
  `via 10.99.0.2 dev thunderbolt0 table 40`.

  Since these rules are evaluated in custom routing table `40` before the `main` table, they override
  flannel's default Ethernet routes for pod-to-pod transit between the two nodes, while remaining
  completely invisible and immune to flannel/k3s route reconciliation. If any other node joins
  the cluster over the LAN, its traffic continues over Ethernet, preventing any route isolation.

- **TSO must stay disabled on `thunderbolt0`.** flannel runs in `host-gw` mode,
  so cross-node pod traffic is plain IP **forwarding** — and TCP segmentation
  offload on this driver mishandles forwarded frames. Measured 2026-08-21: pod
  to pod ran at **244 KB/s** with TSO on versus **380 MB/s** with it off, caused
  by a 15% TCP retransmit rate. Host-locally-generated traffic over the same
  link is unaffected at 1.10 GB/s, so raw link tests look perfectly healthy and
  hide the fault completely. `gso` and `gro` were measured individually and are
  not implicated; they stay on.

  `ethtool` settings are **not** persisted by NetworkManager and reset on reboot
  or `thunderbolt0` re-init, so the `usb4-link-monitor` DaemonSet reconciles
  `ethtool -K thunderbolt0 tso off` on the same 15-second loop as the DNS rules.

  Verify with `ethtool -k thunderbolt0 | grep tcp-segmentation-offload` on both
  nodes — it must read `off`. Tracked in issue #662.


---


---

## Storage (as deployed today)

### ghost

- `nvme1n1` — system disk, btrfs (ostree/bootc root + `/var` + `/var/home`)
- `nvme0n1` ("ghost-data", 3.7T) — workload/scratch storage, formatted **XFS** (migrated
  from btrfs 2026-07-03; XFS chosen for lower per-file metadata overhead on the CAS
  workload below, and to avoid btrfs COW/checksum overhead on data that doesn't need it)
  - `zot-local/` — Zot registry local image store (durable; backed up before any reformat)
  - `local-path/` — k3s `local-path-provisioner` PVC scratch storage, including the
    BuildStream `buildbox-casd` CAS cache (~1.3M small objects, sharded `00`-`ff`) — the
    only `local-path` content treated as durable; other PVCs here are disposable
    build-job scratch data and are not backed up

### exo-0 / other workers

`exo-0` uses `/var/mnt/exo0-data` as its non-root workload disk. Local-path
PVCs are node-local; there is no Longhorn, ZFS, or cross-node replication in
this cluster today. `manifests/local-path-config.yaml` explicitly maps `ghost`
and `exo-0` to their own data mounts and has no default mapping. Never
provision workload storage on a root filesystem.

### Metrics storage

The backend-only `prometheus-lightweight` Deployment stores up to 30 days or
45GB of time series on a 50Gi `local-path` PVC. The size gap reserves space for
WAL and compaction. It scrapes node and cAdvisor metrics, the Argo workflow
controller, both Zot registries, BuildBarn, and KubeStellar controllers.
Prometheus has ClusterIP access only; KubeStellar Console remains the private
operator UI, and Grafana is intentionally absent.

---

## Compute Model

Standard k3s workload scheduling — Argo Workflows dispatch KubeVirt VM provisioning and
test pods across whichever nodes are schedulable.

A Buildbarn Remote Execution (REAPI) grid **is** deployed, in the `buildbarn` namespace,
and is the canonical build cache/remote-exec mechanism for BuildStream (`bst`) jobs:

- `frontend` — Deployment, 2 replicas, **preferred** podAntiAffinity (degrades gracefully
  to co-location if a node is unavailable). Single entrypoint at
  `frontend.buildbarn.svc.cluster.local:8980` for artifact cache, source blob
  storage, and remote-execution traffic from `bst` clients.
- `scheduler` — Deployment, 1 replica (single point of failure by design; see
  `production-hardening-*` follow-ups).
- `storage` — StatefulSet, 2 replicas, **required** podAntiAffinity + per-pod
  local-path PVCs. `WaitForFirstConsumer` and the Kubernetes scheduler choose
  placement; no node selector or root-disk fallback is permitted.
- `worker` — DaemonSet, one pod per schedulable node, each with a `runner` sidecar
  (`CAP_SYS_CHROOT`) that executes BuildStream REAPI actions dispatched by the scheduler.
- `bb-remote-asset` — Deployment, source-cache Remote Asset index at
  `bb-remote-asset.buildbarn.svc.cluster.local:8984`.

BuildStream jobs (`dakota-build-pipeline`, `cosmic-build-pipeline`,
`bluefin-server-build-pipeline`, `bst-qa-pipeline`) point
their artifact and remote-execution config at the shared frontend. Source caches use
the paired Remote Asset index and frontend CAS,
so build actions are genuinely distributed across the `worker` DaemonSet pods on both
`ghost` and `exo-0` rather than executing locally on a single node. The `bst-build` client
pod itself also carries a required podAntiAffinity so concurrent builds spread across
nodes instead of stacking on one.

## GitHub Actions Runners

A self-hosted Actions Runner Controller (ARC) bridge lets projectbluefin
maintainers trigger GitHub Actions jobs on the Ghost cluster without keeping fat
runner pods idle.

- **Controller:** `arc-systems` namespace, `gha-runner-scale-set-controller`
  0.9.3. It listens to GitHub and creates ephemeral runner pods.
- **Org scale set:** `arc-runners` namespace, `runnerScaleSetName: ghost-runners`,
  `githubConfigUrl: https://github.com/projectbluefin`. Only repos covered by the
  `bluefin-ghost-arc` GitHub App installation can request these runners.
- **Container mode:** each Actions job declares a `container:` image and runs as a
  separate Kubernetes pod. Heavy work is submitted back into Argo Workflows
  (e.g., `argo submit --from workflowtemplate/dakota-build-pipeline
  --wait`) so the small runner pod coordinates while real build pods consume
  cluster CPU/memory.
- **Personal scale set:** maintainers who want the same runners on personal repos
  install the `bluefin-ghost-arc` app on their personal account and use a second
  scale set (`ghost-runners-personal`) with a different `githubConfigUrl` and
  installation secret.

Example workflow: `.github/workflows/example-container-mode-build.yml`.  
Access, auth, and troubleshooting: `/docs/ops/maintainer-onboarding.md`.
