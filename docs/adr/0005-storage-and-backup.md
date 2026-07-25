# ADR 0005 — Storage and backup: local-path + Velero data-only

Status: Accepted
Date: 2026-07-24

## Context

The lab cluster (`ghost` + `exo-0`) runs the upstream Rancher Local Path
Provisioner with a `nodePathMap` that pins data directories to node-local data
disks (`/var/mnt/ghost-data/local-path`, `/var/mnt/exo0-data/local-path`). No
storage is ever provisioned on root disks, and workload placement is left to
the scheduler: `local-path` uses `WaitForFirstConsumer` binding so PVCs follow
the pods that land on each node.

Stateful workloads today are:

| Workload | Storage type | Backup relevance |
|---|---|---|
| KubeFlex PostgreSQL (`kubeflex-system/postgres-postgresql-0`) | Bitnami `postgresql` chart PVC on default StorageClass (`local-path`) | User data: WDS control-plane state |
| KubeStellar Console DB (`kubestellar-console`) | `local-path` PVC (chart backup PVC disabled because it hard-codes `RWX`) | User data: Console state |
| `zot-local` registry (`local-registry/registry`) | `hostPath` on `/var/mnt/ghost-data/zot-local` | User data: locally built/pushed images — migrate to `local-path` PVC before Velero backup |
| `zot-cache` pull-through cache | `hostPath` on `/var/mnt/ghost-data/zot-cache` | Reproducible: exclude |
| BuildBarn CAS/AC shards (`buildbarn/storage`) | `local-path` PVCs per StatefulSet replica | Reproducible but expensive to rebuild: include |
| BuildBarn worker node cache | `hostPath` on `/var/lib/buildbarn/worker` | Reproducible: exclude |
| ARC runner work volumes | `local-path` PVCs created per runner pod | Ephemeral CI state: exclude |

Everything else in the cluster is reproducible from GitOps, image-based OS
artifacts, and declarative KubeVirt `VirtualMachine` manifests. KubeVirt VMs use
containerdisks that are ephemeral; there are no persisted VM disks today.

Velero File System Backup (restic/kopia node agent) supports PVC-backed
volumes but does **not** back up `hostPath` volumes. Workloads using `hostPath`
for user data must move to PVCs before they can be protected.

Per ADR-0003, the Proxmox parity items are dropped: there is no
Proxmox-Backup-Server equivalent and no instant-boot VM restore. Backup scope
is therefore **user data only** (PVC contents), not cluster-state or VM
snapshots.

## Decision

**Keep `local-path` as the default StorageClass. Add Velero for file-level,
data-only backup of PVCs using restic/kopia. Defer Longhorn until explicit
migration triggers are hit.**

### Why local-path stays

- It is already working, minimal, and keeps storage on data disks without a
  storage controller per node.
- It satisfies today's RWO-only workloads and scheduler-driven placement.
- It keeps the cluster storage story simple across the single k3s scheduling
  domain described in ADR-0003.

### Why Velero

- Velero is a CNCF-graduated project that backs up Kubernetes resources and
  PVC contents via file-level backup (restic by default; kopia as the newer
  upstream path). This matches the "data-only" scope because `local-path` has
  no volume-snapshot support.
- It is GitOps-friendly: `Schedule`, `Backup`, `Restore`, and backup-location
  configuration live in git; only credentials are sealed.
- It is per-cluster, so it does not assume a single-cluster future: each WEC
  in a future KubeStellar topology can point its own Velero installation at a
  shared or separate object-storage target.

### Why Longhorn is deferred

Longhorn is a CNCF-incubating block storage system with snapshots, RWX, and
built-in replication. It is useful, but it adds a storage controller, replica
management, and node resource overhead that the current homelab does not need.
The decision to adopt it is tied to concrete capability gaps, not to
speculative future scale.

## Consequences

- One additional in-cluster controller (Velero) is accepted under the
  controller-acceptance rule in ADR-0001: desired state lives in git and
  credentials are sealed.
- Backup and restore operate at the PVC level. Restoring a workload means
  restoring its PVC and redeploying the pod; this is not an instant-boot VM
  restore.
- Restic/kopia file-level backup consumes CPU, memory, and I/O during backup
  windows; backups should be scheduled off-peak.
- `local-path` is RWO-only. Restores must respect node affinity: the restored
  PVC will bind to the node that provisioned it, so the consuming pod must be
  schedulable there.
- Caches and ephemeral volumes (`zot-cache`, BuildBarn worker cache, ARC
  runner work volumes) are intentionally excluded from backup. Re-creating
  them is faster and safer than restoring stale cache state.
- VM containerdisks are not backed up. New VM manifests pull current images
  from the registry.

## Migration triggers

Adopt Longhorn (or another CSI block/RWX solution) if **any** of the following
becomes true:

1. **KubeVirt live migration is required** between `ghost` and `exo-0` or any
   future node in the same k3s cluster. Live migration needs volumes that can
   be detached and re-attached, which `local-path` cannot do.
2. **A workload needs ReadWriteMany (RWX)** persistent storage. The
   KubeStellar Console chart, for example, hard-codes a backup PVC with
   `ReadWriteMany`; enabling its built-in backup requires RWX.
3. **A second PC joins the same k3s cluster** and RWO node-local PVCs become a
   scheduling or availability problem for stateful workloads.
4. **A data-loss event exceeds the recovery-time or recovery-point objectives**
   that Velero file-level backups can meet on `local-path`.
5. **KubeStellar multi-WEC topology requires replicated or portable storage**
   that cannot be satisfied by per-cluster object-storage backups.

Until one of these triggers fires, `local-path` + Velero remains the storage
and backup story.

## Implementation sketch: Velero

### Prerequisite: migrate `zot-local` to a PVC

`zot-local` currently stores images on a `hostPath` volume
(`/var/mnt/ghost-data/zot-local`), which Velero cannot back up. Before labeling
it for backup, replace the `hostPath` volume with a `local-path` PVC:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: zot-local-data
  namespace: local-registry
  labels:
    lab.projectbluefin.io/backup: "true"
spec:
  storageClassName: local-path
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 200Gi
```

In the `registry` Deployment, swap the `hostPath` volume for the PVC:

```yaml
volumes:
  - name: data
    persistentVolumeClaim:
      claimName: zot-local-data
```

Because `local-path` is RWO and the Deployment uses `Recreate`, the pod will
re-attach the same PVC after a restart. The PVC will bind on the node where the
pod lands, following existing scheduler placement.

### Backup target

Install via the upstream Helm chart and an object-storage target. The default
target for a homelab is an external NAS that exposes an S3-compatible endpoint
(e.g., TrueNAS SCALE, Synology). If no NAS exists, deploy a self-hosted
S3-compatible store on a data disk. Cloud object storage is the off-site
option.

### Backup target options

| Option | Default? | Notes |
|---|---|---|
| External NAS S3 endpoint | Yes | No new storage controller in the cluster; use existing homelab NAS. |
| Self-hosted S3 on a data disk | Fallback | Acceptable only when no NAS/cloud target exists; must not run on root disk. |
| Cloud object storage | Off-site | S3, B2, Wasabi, etc. Good for disaster recovery, adds external dependency. |

### Example Helm values

```yaml
# argocd/velero-app-values.yaml — committed to git
configuration:
  backupStorageLocation:
    - name: default
      provider: aws
      bucket: bluefin-lab-velero
      prefix: ghost
      config:
        region: us-east-1
        s3ForcePathStyle: true
        s3Url: https://nas.home:9000
      credential:
        name: velero-cloud-credentials
        key: cloud
  volumeSnapshotLocation: []
  defaultVolumesToFsBackup: true

initContainers:
  - name: velero-plugin-for-aws
    image: velero/velero-plugin-for-aws:v1.10.0
    imagePullPolicy: IfNotPresent
    volumeMounts:
      - mountPath: /target
        name: plugins

deployNodeAgent: true
nodeAgent:
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: "1"
      memory: 1Gi

schedules:
  daily-user-data:
    schedule: "0 6 * * *"
    template:
      storageLocation: default
      includedNamespaces:
        - "*"
      excludedNamespaces:
        - local-registry
      labelSelector:
        matchLabels:
          lab.projectbluefin.io/backup: "true"
      defaultVolumesToFsBackup: true
      ttl: 720h0m0s
```

### Labeling convention

PVCs that contain user data are labeled:

```yaml
metadata:
  labels:
    lab.projectbluefin.io/backup: "true"
```

Apply this label to the KubeFlex PostgreSQL PVC, the KubeStellar Console PVC,
the BuildBarn storage PVCs, and the `zot-local` PVC after the migration above.
Do not apply it to `zot-cache`, BuildBarn worker caches, or ARC runner work
volumes.

### Credential handling

Store object-storage credentials in a Kubernetes Secret created from a
sealed-secrets `SealedSecret` (ADR-0001 allows sealed-secrets). Do not commit
plaintext credentials to git.

### Multi-WEC consideration

Each cluster runs its own Velero installation and writes to a distinct prefix
under the same object-storage bucket (e.g., `ghost/`, `site-b/`). A shared
bucket is fine; shared cluster-scoped state is not. Restore operations are
always scoped to a single cluster.
