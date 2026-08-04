# k3s Control Plane Tuning — ghost

Canonical reference for tuning the k3s server on ghost.
Applied once via `/etc/rancher/k3s/config.yaml` and a service restart.
This is not a GitOps manifest — it lives outside ArgoCD's reconciliation scope.

> [!CAUTION]
> This is a private maintainer/operator procedure for host-level maintenance,
> not a public agent recipe. Do not SSH from a workstation to `ghost` or
> `exo-0`; use the approved private maintenance channel. Routine cluster
> inspection and workload control remain API-driven through `just`, `argo`, and
> `kubectl`.

---

## Why

k3s ships with defaults sized for general-purpose clusters. Ghost runs a
five-node homelab cluster (one control-plane + two current workers + up to three
planned Framework desktops) under a KubeVirt + Argo Workflows workload that
creates and destroys many objects quickly. Three defaults cause measurable waste:

**API server watch-cache** is unbounded. Every informer — and ArgoCD, Argo
Workflows, and KubeVirt each run several — reserves cache memory proportional to
object churn. A bounded cache of 100 entries per resource type is sufficient for
this object volume and dramatically reduces idle RSS.

**etcd** does not compact or snapshot automatically. On a busy cluster, the
keyspace grows without limit until etcd slows or hits its quota. Periodic
compaction every hour and a 4 GiB quota cap keep the database healthy without
manual intervention.

**Controller-manager** defaults to 20 concurrent sync workers for Deployments
and ReplicaSets. For five nodes that is 4x more goroutines than useful, burning
CPU and scheduler overhead during Argo burst phases.

---

## Configuration

Place the following block in `/etc/rancher/k3s/config.yaml` on ghost. The file
is cumulative — if it already exists, merge rather than replace.

```yaml
# /etc/rancher/k3s/config.yaml on ghost
# Apply once; requires: sudo systemctl restart k3s
# Workers reconnect automatically within ~30s.

kube-apiserver-arg:
  - "default-watch-cache-size=100"
  - "event-ttl=30m"
  - "max-requests-inflight=100"
  - "max-mutating-requests-inflight=50"
  - "profiling=false"

kube-controller-manager-arg:
  - "leader-elect=false"
  - "node-monitor-period=60s"
  - "node-monitor-grace-period=180s"
  - "concurrent-deployment-syncs=2"
  - "concurrent-replicaset-syncs=2"
  - "concurrent-statefulset-syncs=1"
  - "concurrent-gc-syncs=2"

etcd-arg:
  - "auto-compaction-mode=periodic"
  - "auto-compaction-retention=1h"
  - "quota-backend-bytes=4294967296"
  - "heartbeat-interval=250"
  - "election-timeout=2500"

etcd-snapshot-schedule-cron: "0 */12 * * *"
etcd-snapshot-retention: 5
etcd-snapshot-compress: true
```

**Argument notes:**

| Argument | Value | Rationale |
|---|---|---|
| `default-watch-cache-size` | 100 | Caps per-resource watch-cache entries; still fast for informers on a small cluster |
| `event-ttl` | 30m | Default 1h; events are noisy and short-lived — Argo logs are the durable record |
| `max-requests-inflight` | 100 | Default 400; adequate for five nodes + Argo burst |
| `max-mutating-requests-inflight` | 50 | Default 200; adequate for VM create/delete cycles |
| `profiling` | false | Saves a small amount of CPU; no pprof endpoint needed in production |
| `leader-elect` | false | Single control-plane; disabling saves lease churn |
| `node-monitor-grace-period` | 180s | 3× the monitor period; avoids false `NotReady` on slow VM create phases |
| `concurrent-*-syncs` | 1–2 | Right-sized for five nodes; reduces goroutine pressure during Argo bursts |
| `auto-compaction-retention` | 1h | Compact every hour; prevents keyspace bloat from ephemeral VM objects |
| `quota-backend-bytes` | 4 GiB | Hard cap; etcd logs a warning at 80%; alerts before it blocks writes |
| `heartbeat-interval` | 250ms | Default 100ms; reduces etcd election noise on a LAN |
| `election-timeout` | 2500ms | 10× heartbeat; standard ratio for stable single-node etcd |

---

## Apply Procedure

1. In the approved private host-maintenance session (not workstation SSH), edit
   the file:

   ```
   sudo nano /etc/rancher/k3s/config.yaml
   ```

   Merge the block above. If the file does not exist, create it.

2. Restart k3s:

   ```
   sudo systemctl restart k3s
   ```

   The API server is unavailable for roughly 10–15 seconds while etcd and the
   API server restart. Workers lose contact and reconnect automatically; expect
   all nodes `Ready` again within 30 seconds.

3. Verify:

   ```
   kubectl get nodes
   ```

   All nodes should report `Ready`. If a worker does not recover within 60
   seconds, check `journalctl -u k3s-agent -n 50` on that worker.

4. Confirm etcd compaction is active:

   ```
   kubectl -n kube-system logs -l component=etcd --tail=20 | grep compact
   ```

   You should see periodic compaction log lines within the first hour.

---

## What Was Skipped and Why

**`--watch-cache=false`** — Disabling the watch cache entirely forces every
informer to bypass the in-memory cache and read directly from etcd. ArgoCD,
KubeVirt, and Argo Workflows each hold persistent watches across many resource
types; disabling the cache would significantly increase etcd read load and
latency. Bounding the cache size (`default-watch-cache-size=100`) achieves the
memory goal without the etcd penalty.

**Disabling specific controllers** (`--controllers=-attachdetach,-pv-binder`) —
KubeVirt requires both the `attachdetach` and `pv-binder` controllers to manage
disk attachment for VMs. These cannot be disabled without breaking VM
provisioning.

**Cilium CNI migration** — Replacing Flannel with Cilium would reduce kube-proxy
overhead and add eBPF-based network policy. For a five-node homelab under this
workload, the operational complexity of a full CNI migration is not justified by
the marginal gain. Revisit if the cluster grows beyond ten nodes or network
policy becomes a requirement.

**`--disable-kube-proxy`** — Paired with Cilium; not applicable while Flannel is
the CNI.

---

## Framework Desktop Nodes

When adding Framework laptop or desktop nodes as k3s workers, no changes to this
file are required. Workers join via the standard `K3S_URL` / `K3S_TOKEN`
registration and are immediately schedulable.

The semaphore-tuner CronWorkflow (`manifests/semaphore-tuner.yaml`) recomputes
`max-containerdisk-vms` and `max-hostdisk-vms` slots hourly from live node
allocatable memory. New nodes are reflected automatically within one hour of
joining the cluster. No manual slot edits are needed.

Do not add node selectors to steer workloads toward local disks. Define an
explicit non-root local-path mapping for the node, then let
`WaitForFirstConsumer` and the Kubernetes scheduler co-locate the PVC consumer
with its selected volume.
