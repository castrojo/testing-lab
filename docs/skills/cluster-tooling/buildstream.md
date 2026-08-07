---
name: cluster-buildstream
description: >
  Use when operating USB4 admission, BuildStream distributed builds, Buildbarn
  recovery, or the isolated RECC pilot evidence path.
metadata:
  context7-sources:
    - /apache/buildstream
---

# BuildStream and Distributed Builds

## USB4 is a hard BuildStream admission requirement

When the ghost<->exo-0 USB4 link is down (see RUNBOOK), all cross-node traffic
falls back to 2.5GbE, but **no BuildStream build may run**. Repair the link and
wait for fresh `lab.projectbluefin.io/usb4-link=up` observations on both nodes
before submitting or retrying. An Ethernet-backed, cache-only, runner-local, or
remote-cache-only run is not an acceptable substitute.

- **Shared Buildbarn storage**: artifact and source caches use the scheduler-managed
  Buildbarn storage service. Do not add node-local `hostPath` caches; they bypass
  Kubernetes storage accounting and can fill a node root filesystem.
- **Build capacity is admitted, not assumed:** derive Buildbarn runner slots
  from live allocatable CPU, memory, and storage. Never reserve capacity by
  pinning a build to a node.
- **Zot pull-through** on every node (`registry-mirror-config` DaemonSet) keeps
  image pulls off the WAN and off cross-node paths.
- **BuildStream workspaces** use workflow PVCs. With `WaitForFirstConsumer`,
  Kubernetes selects a schedulable node and the local-path provisioner binds
  the PVC below that node's configured data mount. Do not select a node or
  bind-mount a cache path to influence placement.
- **BST lane policy:** Dakota, COSMIC, and Bluefin Server accept only
  `build-mode=re`. Before admission, every lane requires a fresh USB4 `up`
  annotation and a Ready BuildBarn worker on both `ghost` and `exo-0`.
  Runner-local, cache-only, Ethernet-backed, automatic fallback, and
  remote-cache-only execution are prohibited. Before treating a run as
  distributed, verify its generated `projects.<name>.remote-execution`
  configuration, BuildStream RE startup, and current worker action activity.
  Dakota uses a two-slot `bst-build` semaphore and workflow-owned 200Gi
  `local-path` cache PVCs. The distributed workflow builds both `oci/bluefin.bst`
  (`dakota:testing`) and `oci/bluefin-nvidia.bst` (`dakota-nvidia:testing`). NVIDIA
  builds run in parallel with continueOn (non-blocking) as the lab does not have GPU
  hardware to test NVIDIA runtime execution. The Dakota
  commit poller pins the checkout to the exact GitHub SHA
  it observed.
- **Buildbarn RE sandbox device nodes**: `bb_runner` with
  `chrootIntoInputRoot: true` can fail when `/dev/null`, `/dev/zero`,
  `/dev/random`, and `/dev/urandom` are missing inside the chroot. The cluster-side
  fix is now in the manifests: `manifests/buildbarn-worker.yaml` creates a
  minimal `/worker/dev` tree with those nodes, and `manifests/buildbarn-config.yaml`
  sets `bb_runner.devDirectoryPath` to `/worker/dev`. That removes the old
  device-node failure mode without requiring a cache-only fallback for this
  specific issue. A failing RE action must stop the build for repair; never
  route the work to a local or Ethernet-backed fallback.

Capacity guard: node memory *requests* must leave room for the 32Gi runner.
Orphaned 8Gi test VMs from failed image-poll runs are the usual thief — check
`kubectl describe node | grep -A8 "Allocated resources"` and delete VMs whose
parent workflow is terminal.

`manifests/orphan-vm-cleanup.yaml` runs every 30 minutes and deletes VMs whose
parent Argo workflow is gone or in a terminal phase. The cleanup looks up the
`argo-workflow` label in the VM's own namespace; image-poller and QA workflows
run in their test namespace, not in `argo`.

Also check for completed Jobs whose pods still reserve large memory requests;
Kubernetes counts a pod's request against node capacity until the Job (and its
pods) is deleted. Example: a finished `atspi-cacheonly` Job held a 14Gi request
and blocked Buildbarn storage scheduling on `exo-0` until the Job was removed.

## Lessons for future agents

The 2026-07-22 Dakota investigation established the following decision tree:

1. **Verify admission before debugging compilation.** Fresh USB4 timestamps, node
   readiness, two Ready BuildBarn workers, and workflow resource requests/limits
   are prerequisites. An initial graph-validation rejection was quota/admission
   related because a step lacked resources; it was not a Dakota compiler result.
2. **Prove runner correctness with a small action, then prove the full root.** A
   tiny REAPI input root executing proves connectivity only. The gate remains red
   until the full SDK root completes CAS materialization and an action executes
   `rustc -vV` successfully.
3. **Separate evidence classes.** Local BuildStream success, Podman/Zot push
   success, and container E2E are useful diagnostics, but none substitutes for
   a successful distributed `build-mode=re` build. Never report overall green
   from those results alone.
4. **Check live-vs-git configuration.** Before retrying, compare the live
   `buildbarn-config` and worker/runner pods with the repository. Stale ConfigMaps
   can preserve virtual/FUSE directories or `setTmpdirEnvironmentVariable` after
   the source manifest has moved to native directories. Reconcile through GitOps
   and wait for both worker pairs to restart before testing.
5. **Do not tune capacity around a correctness failure.** Keep one action slot per
   runner and current BuildStream/semaphore limits until full-root materialization
   is reliable. Low utilization during a failed action is expected and is not a
   reason to add workers or jobs.

## Rechunking a Dakota BuildStream OCI export

`scripts/rechunk_bst_image.sh` is the reusable layout-to-layout transform for
Dakota BST exports:

```bash
FAKECAP_RESTORE=/src/files/fakecap/fakecap-restore \
  scripts/rechunk_bst_image.sh \
  /workspace/export \
  /workspace/rechunked \
  /src/files/fakecap-manifest.tsv > /workspace/rechunk-metrics.json
```

The script uses the existing privileged builder tools (`podman`, `skopeo`,
`jq`, overlayfs, and Dakota's compiled `fakecap-restore`); it installs nothing.
It pins chunkah v0.6.0, physically restores the manifest xattrs, emits at most
128 compressed layers, prunes `/sysroot/`, preserves `containers.bootc`, and
removes `ostree.commit` plus `ostree.final-diffid`. Stdout is one JSON object
with input/output digests, compressed bytes, layer count, duration, and tool
version. Logs go to stderr.

In Argo, keep build/export, rechunk, publish, and metrics persistence as
artifact-connected DAG tasks. Pass the Dakota manifest/helper paths into this
script; do not duplicate the transform inline in Workflow YAML.

Sources:

- [chunkah v0.6.0 usage and bootc compatibility](https://github.com/coreos/chunkah/blob/v0.6.0/README.md)
- [Project Bluefin chunkah action](https://github.com/projectbluefin/actions/blob/main/bootc-build/chunka/action.yml)
- [Dakota fakecap convention](https://github.com/projectbluefin/dakota/blob/main/Justfile)

## BST build scheduling: avoid preemption

BST build pods use the `bst-build` PriorityClass. Long distributed builds lose all
progress when preempted, so `bst-build` (1,500,000) is set higher than
`lab-test-vm` (1,000,000). Builds therefore take scheduling precedence over
short-lived image-poll VMs, while still remaining below kubevirt/system critical
classes.

Symptom of the old lower-priority setting: `argo get` shows `pod deleted` and
`kubectl get events --field-selector reason=Preempted` lists the build pod
displaced by a VM pod. If this returns, verify the PriorityClass values:

```bash
kubectl get priorityclass bst-build lab-test-vm
```

Mitigations:

1. **Keep `bst-build` above `lab-test-vm`.** `manifests/bst-build-priorityclass.yaml`
owns the value. Raising it lets builds preempt polling VMs instead of the
reverse.

2. **Use verified parallel BST capacity.** Keep independent work concurrent when
BuildBarn workers and node requests have safe headroom. Respect actual BuildStream graph dependencies and reassess live worker and node
capacity before raising or lowering the two-slot `bst-build` limit. NVIDIA
variants are built in parallel with continueOn (non-blocking).

3. **Clear stale semaphore holders before reducing capacity.** The semaphore in
`manifests/workflow-semaphores.yaml` gates all BST build lanes. Confirm terminal
workflows do not retain locks, then set `bst-build` to the safe live worker
capacity instead of serializing independent work by default.

4. **Verify the fix live.** After submission, confirm the pod is Running and on a
node with enough free requested memory:

```bash
kubectl get pod -n argo <pod-name> -o custom-columns='NODE:.spec.nodeName'
kubectl describe node <node> | grep -A8 "Allocated resources"
kubectl get events -n argo --field-selector reason=Preempted --sort-by='.lastTimestamp'
```

## Queueing, cleanup, and Buildbarn recovery

When the cluster is already hot, the fastest recovery is usually to stop the noise
instead of submitting more work:

1. Delete stale terminal workflows first; leave the newest healthy run in place.
2. Delete orphaned VMs/PVCs whose parent workflow is already terminal so they do
   not keep memory or storage reservations pinned.
3. Gate the expensive lane at the template level with the semaphores in
   `manifests/workflow-semaphores.yaml`; workflow-level mutexes are not enough for
   `workflowTemplateRef` / `templateRef` callers.
4. If Buildbarn storage pods stay `Pending` after a StatefulSet or PVC change, verify
   the PVC bindings and storage pods before resubmitting a build. The cluster health
   signal is `kubectl -n buildbarn get pvc` plus `kubectl -n buildbarn get pods`.

This pattern keeps the cluster from falling into a feedback loop where duplicate
pollers, stale build runs, and orphaned VMs all compete for the same memory and
storage budget.

## BuildStream 2.x Distributed Builds and Caching

BuildStream 2.x uses the cluster's shared Buildbarn deployment for artifact
cache writeback and remote execution. Workflow-local state belongs on a
PVC-backed workspace, while the shared Buildbarn frontend provides cluster-wide
artifact reuse. Neither cache layer may use a root-backed `hostPath`.

### 0. Mandatory remote execution & RECC compiler distribution

Dakota builds must use BuildBarn remote execution regardless of transport path.
If remote execution is unhealthy, fail the workflow, diagnose it, and repair the
grid; do not fall back to a runner-local or cache-only build. The
`remote-execution.conf` ConfigMap key is appended under the Dakota project in
the generated BuildStream configuration. A healthy run has two Ready workers,
two action slots, and observable current worker actions.

The isolated `bst2` RECC pilot is a nested sandbox workload and must run its
script container with `privileged: true` and `seLinuxOptions.type: spc_t`.
Without that runtime contract, BuildStream's bubblewrap sandbox fails before
compiler execution with `Can't mount proc on /newroot/proc: Operation not
permitted`. This requirement is specific to the pilot container; it does not
enable nested RECC or relax the production runner admission gate.

**RECC (Remote Execution C/C++ Compiler) integration:**
The lab has a checkout-local RECC overlay and worker-local `buildbox-casd`
preparation seam. The isolated operator pilot is the only supported cache
experiment; production BuildStream/QA/warmup lanes fail closed until a pinned
runner demonstrably consumes BuildStream's `remoteApisSocketPath` and stages
the pod-local LocalCAS socket.
- **Macro level:** BuildStream schedules element builds across BuildBarn workers.
- **Micro level (RECC):** RECC can cache or distribute individual C/C++
  compiler invocations inside one element; it is distinct from outer
  BuildStream remote execution. Linking and non-C++ toolchains remain local
  unless separately wrapped.
- **Configuration:** `recc-environment.conf` supplies the shared server,
  CAS, action-cache, project-root, and policy values. Toolchain elements must
  supply `RECC_REMOTE_PLATFORM_chrootRootDigest`; the lab does not invent a
  global digest or set `RECC_PREFIX`.
- **Pilot:** Run `just run-recc-baseline mode=cache-only cache-policy=both`.
  Keep the source revision, pinned freedesktop-sdk provider, `bst2` image,
  and worker set fixed. See `docs/reference/recc-baseline.md` and the measured
  results in `docs/research/2026-07-31-recc-run-results.md`.
- **Evidence boundary:** A successful outer BuildStream build is not RECC
  evidence. The collector preserves missing RECC action fields as
  `unavailable`; current runs have real BuildStream/BuildBarn timing and CAS
  data but no trustworthy RECC hit/miss metrics.

### RECC pilot evidence handoff

The operator-only `recc-baseline-pipeline` must use BuildStream's configured
`logdir` as the sandbox-to-workflow evidence path. The fixture writes its
StatsD output under `%{build-root}`, prints each record with a
`[RECC_METRICS]` marker into the element build log, and removes the file before
installing the deterministic artifact. The workflow points `logdir` at its
`/work` volume and preserves complete RECC-marked lines; do not put metrics in
`%{install-root}`, a checked-out artifact, or a host mount.

This preserves artifact determinism while making action-cache hits/misses,
local fallbacks, and compiler timing available to the collector. A cache-only
or upload-local-build pilot must fail closed when the BuildStream logdir has no
valid RECC StatsD metric evidence rather than report zeros or infer a warm hit from outer
BuildStream success. This follows BuildStream's documented writable
`%{build-root}`/`%{install-root}` sandbox paths and user-configured `logdir`
(`source: /apache/buildstream`).

### RECC pilot evidence handoff

The operator-only `recc-baseline-pipeline` must use BuildStream's configured
`logdir` as the sandbox-to-workflow evidence path. The fixture writes its
StatsD output under `%{build-root}`, prints each record with a
`[RECC_METRICS]` marker into the element build log, and removes the file before
installing the deterministic artifact. The workflow points `logdir` at its
`/work` volume and extracts only RECC-marked lines; do not put metrics in
`%{install-root}`, a checked-out artifact, or a host mount.

This preserves artifact determinism while making action-cache hits/misses,
local fallbacks, and compiler timing available to the collector. A cache-only
or upload-local-build pilot must fail closed when the BuildStream logdir has no
RECC evidence rather than report zeros or infer a warm hit from outer
BuildStream success. This follows BuildStream's documented writable
`%{build-root}`/`%{install-root}` sandbox paths and user-configured `logdir`
(`source: /apache/buildstream`).

### 1. Shared Buildbarn frontend
- **Endpoint**: `grpc://frontend.buildbarn.svc.cluster.local:8980`
- **Role**: CAS/AC artifact writes and reads; execute-forwarding for BuildStream actions that use the in-cluster execution grid
- **Deployment**: Frontend, scheduler, storage shards, and workers are defined under `manifests/buildbarn-*.yaml` and run in the `buildbarn` namespace

Cache heat is persisted by `scripts/collect_bst_cache.py` in
`docs/data/history/cache-heat.ndjson`. BuildBarn's current diagnostics expose
`Get` counts, byte sums, duration sums, and gRPC status counts. The collector
treats `OK` as a hit and `NotFound` as a miss; if those status counters are unavailable, it keeps
`hit_count`, `miss_count`, and `effectiveness` null rather than estimating from
storage occupancy or allocator counters.

### 2. BuildStream client config
  The build pods should generate a deterministic `buildstream.conf` that keeps upstream source caches read-only and pushes artifacts to the shared Buildbarn frontend first. Dakota's coordinator remains bounded by its two one-slot BuildBarn workers; do not increase BuildStream jobs, worker count, or semaphore capacity while remote execution or CAS materialization is unhealthy:
 
```yaml
scheduler:
  network-retries: 8
  fetchers: 4
  builders: 2
  pushers: 2
build:
  max-jobs: 8
artifacts:  override-project-caches: false
  servers:
  - url: grpc://frontend.buildbarn.svc.cluster.local:8980
    push: true
  - url: https://cache.projectbluefin.io:11001
    push: false
  - url: https://cache.freedesktop-sdk.io:11001
    push: false
  - url: https://gbm.gnome.org:11003
    push: false
source-caches:
  override-project-caches: true
  servers:
  - url: https://gbm.gnome.org:11003
    push: false
  - url: https://cache.freedesktop-sdk.io:11001
    push: false
```

Repeat the same override and server ordering at the project level so the primary project uses the same cache policy as the top-level config.

**Lab-wide source-cache rule (verified 2026-07-22 / 2026-07-27):** the deployed `bb-remote-asset` image (`ghcr.io/buildbarn/bb-remote-asset:20241031T230517Z-4926e8e`) cannot handle BuildStream source-key URNs. Pushing sources to `grpc://bb-remote-asset.buildbarn.svc.cluster.local:8984` (`type: index`) produces `FetchDirectory ... PERMISSION_DENIED` / `HTTP Fetching of directories is not supported!` and, because the remote-asset asset cache reads through the unsharded storage headless Service, `could not get action from action cache: Object not found`. Until the endpoint is upgraded/configured for URNs, **every** lab BuildStream pipeline (`dakota-build-pipeline`, `cosmic-build-pipeline`, `bluefin-server-build-pipeline`, `bst-qa-pipeline`) must set `source-caches.override-project-caches: true` and list only the upstream read-only source caches. Leaving it `false` inherits junction source-cache entries and causes the build to spend minutes retrying a source push before failing. Artifact/RE cache writes may still use the BuildBarn frontend.

**Worker recovery note (verified 2026-07-22):** a failed virtual/FUSE BuildBarn experiment left stale `bb_worker` mounts on the node-local worker path, causing subsequent `volume-init` failures (`Transport endpoint is not connected`). Recovery was performed through a temporary privileged, host-PID recovery pod using `nsenter -t 1 -m -- umount -l /var/lib/buildbarn/worker/build`, followed by worker-pod recreation. The virtual/FUSE experiment also exposed `rustc -vV: Permission denied` in `gnomeos-deps/bootc.bst`. The runner was upgraded from the 2026-05-27 BuildBarn pair to the coordinated 2026-07-22 worker/installer pair, made privileged/root with `spc_t`, and configured with `/tmp` and `/var/tmp` per-action symlinks plus one action slot. Direct REAPI isolation proved the tiny input root reaches the runner, while the full SDK root stalls during CAS materialization before command execution. The frontend was restarted to load current shard configuration; a 31 MiB CAS blob then read through the frontend in 0.08s, but a fresh full-root action still timed out after 10 minutes. The sharded CAS is inconsistent for the full root: some blobs exist only on storage-0 while storage-1 reports NotFound, and worker failures report `Shard 0/1: context canceled` during input fetch. The distributed gate remains red until CAS replication/routing and full-root materialization are repaired. BuildStream's default config is merged with the supplied config; to force a cache-only local diagnostic, explicitly add a top-level `remote-execution: {}` rather than merely omitting the remote-execution snippet.

**Controlled retry update (2026-07-23):** after reconciling native runner configuration and restarting both worker pods, the Dakota retry uploaded SDK input roots and reached remote command execution, demonstrating progress beyond the historical `rustc`/TMPDIR failure. However, both actions failed when the old workers disappeared during execution, and the retry was still in artifact pulls afterward. Do not call this green; worker continuity and the full build/publish/digest/E2E chain still require proof.

**Distributed NVIDIA Policy:** The distributed Dakota workflow builds both default (`oci/bluefin.bst`) and NVIDIA (`oci/bluefin-nvidia.bst`) variants in parallel. Because the lab cluster lacks NVIDIA GPU hardware to execute GPU test suites, the NVIDIA build runs as non-blocking (`continueOn`). When the NVIDIA build succeeds, its image (`dakota-nvidia:testing`) is published directly to Zot.

**Registry publication verified 2026-07-22:** the local Zot registry accepts anonymous pushes over its configured insecure HTTP endpoint. Publish with Podman (`podman push --tls-verify=false localhost/dakota:testing docker://<ghost-ip>:30500/dakota:testing`) rather than rootful Skopeo, which may look at `/run/containers/storage` and fail for a rootless session. Verify the registry digest with `skopeo inspect --tls-verify=false` and pull the registry tag back before smoke testing.

**GPU validation boundary (updated 2026-08-06):** the NVIDIA image contains `/usr/sbin/nvidia-smi`, but the lab has no NVIDIA hardware — no node advertises `nvidia.com/gpu` and no NVIDIA device plugin is running. Docker's `--gpus all` fails because the host has no communicating NVIDIA driver; Podman CDI fails because `nvidia.com/gpu=all` is unresolvable. NVIDIA GPU runtime validation is therefore not actionable on this cluster. Both nodes *do* advertise `amd.com/gpu: 1` (Radeon 8060S / gfx1151, ROCm) via `manifests/amdgpu-device-plugin.yaml`, so AMD-side GPU validation is possible — see `docs/skills/cluster-tooling/SKILL.md` § "AMD GPU topology".

### Verified CAS materialization findings (2026-07-22)

Direct REAPI isolation separates the failure stages:

- A tiny input root reaches the BuildBarn runner and executes immediately.
- The full SDK input root stalls during native CAS materialization before the runner starts.
- The target tree contains executable /usr/bin/rustc and /usr/bin/cargo entries.
- Restarting stale frontend and scheduler pods made a 31 MiB frontend CAS read complete in 0.08s, but a fresh full-root action still exceeded ten minutes.
- A correctly configured virtual/FUSE worker was tested and failed at startup with Failed to expose build directory mount: operation not permitted; production therefore remains native.
- The current native configuration uses the large persistent hardlink cache and inputDownloadConcurrency: 128. The distributed gate remains red until a full SDK input root materializes reliably.

### 3. BuildStream parser constraints
- **No top-level `source:` key**: `buildstream.conf` does not support a top-level `source:` block.
- **Nested under `scheduler`**: fetch / retry / network settings belong under `scheduler:`.
- **Sequence writes in Argo scripts**: prefer `echo "..." >> file` over multiline heredocs when generating config in YAML script blocks.

### 4. PVC-backed workflow workspace
Each BuildStream pod mounts a workflow PVC at `/root/.cache/buildstream` when
state must persist between workflow steps. It must use the `local-path` StorageClass
with the explicit GitOps node-to-data-mount mapping. Never use `/var/tmp`, a
root filesystem, or a node-local `hostPath` cache.

### 5. Private maintainer procedure — Buildbarn durable shard backup / restore

> [!CAUTION]
> This section is retained for maintainers who have explicitly approved a
> storage maintenance window and backup plan. It is not a normal public recipe:
> host-path copies, retained PVC/PV deletion, and restore commands can destroy
> data. Never run it from a workstation or use workstation SSH to `ghost` or
> `exo-0`; use the approved private operator channel. Routine Buildbarn
> operations remain API-driven through `just`, `argo`, `kubectl`, and GitOps.

#### What is durable vs. disposable
- **Durable**: the `storage` StatefulSet's per-ordinal `local-path` PVCs. `manifests/buildbarn-storage.yaml` defines two replicas (`storage-0`, `storage-1`) with **required** podAntiAffinity, plus two PVCs per ordinal: `cas` mounted at `/storage-cas` and `ac` mounted at `/storage-ac`.
- **Not replicas of the same bytes**: `manifests/buildbarn-config.yaml` shards both CAS and AC across two equally weighted shards (`"0"` and `"1"` with `weight: 1` each). That means `storage-0` and `storage-1` each own part of the keyspace. Losing one shard without a backup loses roughly half of the CAS blobs and AC entries permanently.
- **Disposable**: workflow-local BuildStream PVC contents are safe to wipe after
  the workflow is terminal. Do **not** treat them as a substitute for backing
  up Buildbarn storage PVCs, and never replace them with a root-backed
  `hostPath`.

#### First inspect the live shard mapping
Command patterns in this subsection were verified against current Kubernetes docs via Context7 (`/kubernetes/website`).

Never assume yesterday's path layout is still true. Before any backup or restore, record the live PVC → PV → node → host-path mapping:

```bash
kubectl get configmap local-path-config -n kube-system -o jsonpath='{.data.config\.json}{"\n"}'

for claim in cas-storage-0 ac-storage-0 cas-storage-1 ac-storage-1; do
  pv=$(kubectl get pvc -n buildbarn "$claim" -o jsonpath='{.spec.volumeName}')
  kubectl get pv "$pv" -o jsonpath="$claim"' node={.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]} path={.spec.local.path}{"\n"}'
done
```

`manifests/local-path-config.yaml` defines explicit per-node paths:
- `ghost` is mapped to `/var/mnt/ghost-data/local-path`
- `exo-0` is mapped to `/var/mnt/exo0-data/local-path`

This ensures that both nodes write their local-path persistent volume data directly to their respective 4TB NVMe SSD drives instead of the root system partition. Always verify the live configuration via:
`kubectl get configmap local-path-config -n kube-system -o jsonpath='{.data.config\.json}{"\n"}'`

#### Why `rsync --sparse` is the right tool here
This storage is **not** shaped like the old multi-million-file BuildStream cache. The live shard layout is sparse block-device files:
- `/storage-cas`: `blocks`, `key_location_map`, `persistent_state/state`
- `/storage-ac`: `blocks`, `key_location_map`, `persistent_state/state`

Use `rsync` with `--sparse`; do **not** use a naive `tar | ssh | tar` pipe that inflates sparse files and gives poor restartability.

#### Backup procedure
1. **Quiesce writers first.** Do not back up while BST jobs are actively pushing new CAS/AC entries.
   ```bash
   kubectl get workflows -n argo
   kubectl scale deployment/frontend deployment/scheduler deployment/bb-remote-asset -n buildbarn --replicas=0
   kubectl scale statefulset/storage -n buildbarn --replicas=0
   kubectl wait --for=delete pod -l app=storage -n buildbarn --timeout=180s
   ```
2. **Record the live PV paths** with the mapping commands above.
3. **Create backup roots on the opposite host** so one node loss does not take the live shard and its backup together.
   ```bash
   STAMP=$(date -u +%Y%m%dT%H%M%SZ)

   ssh core@<worker-ip> "sudo mkdir -p /var/mnt/exo0-data/buildbarn-backups/storage-1/${STAMP}/cas /var/mnt/exo0-data/buildbarn-backups/storage-1/${STAMP}/ac"
   ssh core@<lab-ip> "sudo mkdir -p /var/mnt/ghost-data/buildbarn-backups/storage-0/${STAMP}/cas /var/mnt/ghost-data/buildbarn-backups/storage-0/${STAMP}/ac"
   ```
4. **Back up `storage-1` (ghost) onto exo-0's 4TB drive.**
   ```bash
   ssh core@<lab-ip> "sudo rsync -aHAXSx --numeric-ids --info=progress2 -e 'ssh -c aes128-gcm@openssh.com' /var/mnt/ghost-data/local-path/<cas-storage-1-pv-dir>/ core@<worker-ip>:/var/mnt/exo0-data/buildbarn-backups/storage-1/${STAMP}/cas/"
   ssh core@<lab-ip> "sudo rsync -aHAXSx --numeric-ids --info=progress2 -e 'ssh -c aes128-gcm@openssh.com' /var/mnt/ghost-data/local-path/<ac-storage-1-pv-dir>/ core@<worker-ip>:/var/mnt/exo0-data/buildbarn-backups/storage-1/${STAMP}/ac/"
   ```
5. **Back up `storage-0` (exo-0) onto ghost.**
   ```bash
   ssh core@<worker-ip> "sudo rsync -aHAXSx --numeric-ids --info=progress2 -e 'ssh -c aes128-gcm@openssh.com' /var/mnt/ghost-data/local-path/<cas-storage-0-pv-dir>/ core@<lab-ip>:/var/mnt/ghost-data/buildbarn-backups/storage-0/${STAMP}/cas/"
   ssh core@<worker-ip> "sudo rsync -aHAXSx --numeric-ids --info=progress2 -e 'ssh -c aes128-gcm@openssh.com' /var/mnt/ghost-data/local-path/<ac-storage-0-pv-dir>/ core@<lab-ip>:/var/mnt/ghost-data/buildbarn-backups/storage-0/${STAMP}/ac/"
   ```
6. **Verify the copy before resuming traffic.**
   ```bash
   ssh core@<lab-ip> "sudo rsync -aHAXSxn --delete /var/mnt/ghost-data/local-path/<cas-storage-1-pv-dir>/ core@<worker-ip>:/var/mnt/exo0-data/buildbarn-backups/storage-1/${STAMP}/cas/"
   ssh core@<lab-ip> "sudo rsync -aHAXSxn --delete /var/mnt/ghost-data/local-path/<ac-storage-1-pv-dir>/ core@<worker-ip>:/var/mnt/exo0-data/buildbarn-backups/storage-1/${STAMP}/ac/"
   ssh core@<worker-ip> "sudo rsync -aHAXSxn --delete /var/mnt/ghost-data/local-path/<cas-storage-0-pv-dir>/ core@<lab-ip>:/var/mnt/ghost-data/buildbarn-backups/storage-0/${STAMP}/cas/"
   ssh core@<worker-ip> "sudo rsync -aHAXSxn --delete /var/mnt/ghost-data/local-path/<ac-storage-0-pv-dir>/ core@<lab-ip>:/var/mnt/ghost-data/buildbarn-backups/storage-0/${STAMP}/ac/"
   ```
   Then compare file lists and logical sizes on source vs. destination:
   ```bash
   sudo find <dir> -type f -printf '%P %s\n' | sort
   sudo du -sh <dir>
   sudo du -sh --apparent-size <dir>
   ```
   Expect the same three-file layout per volume (`blocks`, `key_location_map`, `persistent_state/state`) and matching apparent sizes.
7. **Bring Buildbarn back.**
   ```bash
   kubectl scale statefulset/storage -n buildbarn --replicas=2
   kubectl rollout status statefulset/storage -n buildbarn --timeout=180s
   kubectl scale deployment/frontend deployment/scheduler deployment/bb-remote-asset -n buildbarn --replicas=1
   kubectl rollout status deployment/frontend -n buildbarn --timeout=180s
   kubectl rollout status deployment/scheduler -n buildbarn --timeout=180s
   kubectl rollout status deployment/bb-remote-asset -n buildbarn --timeout=180s
   ```

#### Restore procedure
1. **Quiesce Buildbarn** using the same scale-down sequence as the backup procedure.
2. **Identify the failed ordinal and its old PVs.**
   ```bash
   kubectl get pvc -n buildbarn
   kubectl get pv | grep 'buildbarn/.*storage-[01]'
   ```
3. **Decide where the replacement shard should live before recreating PVCs.**
   - If you are restoring `storage-1` on `ghost`, the live path should stay under ghost's `local-path` base.
   - If you are restoring `storage-0` onto `exo-0`'s 4TB drive, first fix `local-path-config` so `exo-0` maps to `/var/mnt/exo0-data/local-path`; otherwise a recreated PV will land back on `/var/mnt/ghost-data/local-path` on `exo-0`'s system disk.
4. **Delete only the failed ordinal's retained PVCs/PVs** after confirming you have a good backup.
   ```bash
   kubectl delete pvc -n buildbarn cas-storage-0 ac-storage-0
   kubectl delete pv <cas-storage-0-pv> <ac-storage-0-pv>
   ```
   Substitute ordinal `1` if the ghost shard failed.
5. **Recreate fresh empty PVCs/PVs by scaling storage back up, then record the new host paths.**
   ```bash
   kubectl scale statefulset/storage -n buildbarn --replicas=2
   kubectl get pvc -n buildbarn -w
   ```
   Once the new claims are bound, rerun the PVC → PV → node → path lookup and capture the new target directories.
6. **Scale storage back down again before copying data into the fresh PV paths.**
   ```bash
   kubectl scale statefulset/storage -n buildbarn --replicas=0
   kubectl wait --for=delete pod -l app=storage -n buildbarn --timeout=180s
   ```
7. **Restore the backed-up shard into the new host directories.**
   ```bash
   sudo rsync -aHAXSx --numeric-ids --delete --info=progress2 <backup-root>/cas/ <new-cas-pv-path>/
   sudo rsync -aHAXSx --numeric-ids --delete --info=progress2 <backup-root>/ac/  <new-ac-pv-path>/
   ```
8. **Bring the storage shard back, then the clients.**
   ```bash
   kubectl scale statefulset/storage -n buildbarn --replicas=2
   kubectl rollout status statefulset/storage -n buildbarn --timeout=180s
   kubectl scale deployment/frontend deployment/scheduler deployment/bb-remote-asset -n buildbarn --replicas=1
   kubectl rollout status deployment/frontend -n buildbarn --timeout=180s
   kubectl rollout status deployment/scheduler -n buildbarn --timeout=180s
   kubectl rollout status deployment/bb-remote-asset -n buildbarn --timeout=180s
   kubectl get pods -n buildbarn -o wide
   kubectl get endpointslice -n buildbarn -l kubernetes.io/service-name=storage
   ```

#### Post-restore verification
- **Filesystem check**: rerun `find ... -printf '%P %s\n' | sort`, `du -sh`, and `du -sh --apparent-size` against the restored host paths and compare them with the backup copy.
- **Pod readiness**: `storage-0` and `storage-1` must both be `Running`, and `kubectl rollout status statefulset/storage -n buildbarn` must succeed.
- **Client reachability**: `frontend`, `scheduler`, and `bb-remote-asset` must be `Available`, and the `storage` headless Service must show endpoints for both storage pods.
- **End-to-end smoke test**: run one lightweight BST workflow that exercises CAS/AC and remote execution:
  ```bash
  argo submit -n argo --from workflowtemplate/bst-qa-pipeline --watch
  ```
  Do not declare the restore complete until that workflow succeeds against the restored shard.

### 6. Buildbarn message-size floor
BuildStream can issue large CAS upload batches while importing bootstrap seed artifacts. Keep the Buildbarn config's gRPC message size high enough for those uploads:

```jsonnet
maximumMessageSizeBytes: 64 * 1024 * 1024
```

If the value is too low, BuildStream lanes can fail with `Unable to upload <N> blobs to remote CAS`.
When `buildbarn-config` changes, also bump the `buildbarn-config-revision` pod-template annotations in:
- `manifests/buildbarn-frontend.yaml`
- `manifests/buildbarn-scheduler.yaml`
- `manifests/buildbarn-storage.yaml`
- `manifests/buildbarn-worker.yaml`
