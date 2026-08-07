# Homelab Validation Contracts

This document defines the in-cluster workload validation contracts for the
lab QA factory. It covers the workload matrix (#57), shared-storage
and RWX limits (#62), storage observability surface (#70, #78), the
fleet-client vs. cluster-node boundary (#72), the HTTPS service-exposure lane (#58), and the deferred non-core service follow-up for Home Assistant-class workloads (#69).

For the service-catalog workload contract that all service lanes must satisfy, see [`docs/service-catalog-contract.md`](service-catalog-contract.md) (#66).

---

## 1. In-Cluster Workload Matrix (#57)

The cluster currently validates three workload classes. Each class maps to a
WorkflowTemplate lane, a test module, and a concrete set of persistence and
access guarantees it must prove.

| Workload class | Lane WorkflowTemplate | Test module | What it proves |
|---|---|---|---|
| **General-purpose** | `homelab-substrate` | `tests/homelab_substrate/` | k3s scheduling, pod lifecycle, in-cluster HTTP/TCP reachability, `local-path` PVC allocation |
| **NAS / storage** | `homelab-storage` | `tests/homelab_storage/` | PVC bound on `local-path`, data survives `rollout restart`, `findmnt`/`df`/`lsblk`/ZFS artifacts captured |
| **Service access** | `homelab-access-probe` | `tests/homelab_access/` | Cluster-DNS resolution, TLS handshake, expected-host routing via SNI |
| **Print service** | `homelab-print-service` | `tests/service_catalog/print/` | Config PVC (1Gi) binding and writability, IPP port 631 reachability, PUID/PGID/TZ env injection, state survives `rollout restart` |
| **HTTPS exposure** | `homelab-access-probe` | `tests/homelab_access/test_https_exposure.py` | Certificate subject match, TLS 1.2+ enforcement, HTTPS reachability, wrong-host rejection (§6) |

### Minimum persistence contract per class

#### General-purpose
- Pod scheduled on ghost and reaches Running state.
- HTTP endpoint reachable within the cluster namespace.
- PVC allocates on `local-path` and mounts read-write.

#### NAS / storage
- PVC binds within 60 s with `local-path` storage class.
- Declared mount path is a directory, writable, and survives `rollout restart`.
- Disk-usage, mount table, and block-device evidence is captured as artifacts
  in every run (see §3 for artifact names).
- **RWX blocked until #62 is resolved** — see §2.

#### Service access
- Cluster DNS resolves `<service>.<namespace>.svc.cluster.local`.
- TLS handshake reports a certificate with `Protocol version` in the output.
- Health endpoint returns `access-ok` with `Host:` header routing.

#### Print service
- Config PVC (1Gi `local-path` ReadWriteOnce) binds and is writable at `/config`.
- Config PVC survives a `rollout restart` (sentinel file checked after pod replacement).
- `PUID`, `PGID`, and `TZ` environment variables are present in the container env.
- IPP port 631 is reachable within the cluster namespace (TCP connect succeeds).
- Cluster DNS resolves `homelab-print-service.<namespace>.svc.cluster.local`.
- **USB printer device access deferred to #67** — requires host device passthrough.
- **LAN mDNS autodiscovery deferred to #67** — requires avahi sidecar and NodePort/LoadBalancer exposure.

### Gaps surfaced explicitly
- Media streaming / transcoding lane: not yet defined. GPU passthrough is a
  known blocker; filed as a follow-up under the service-catalog epic.
- ReadWriteMany / shared-media access: blocked by #62.
- Service-to-service auth: deferred to service-catalog auth-gating lane (#61).
- USB printer device access and LAN mDNS discovery: split from print-service base lane into #67.

---

## 2. Shared-Storage / RWX Blocker (#62)

The current k3s cluster uses the **`local-path`** storage provisioner, which
only supports `ReadWriteOnce` (RWO) access mode. This means:

### What can proceed on current storage
| Scenario | Status |
|---|---|
| Single-pod PVC lifecycle (create, mount, write, survive restart) | ✅ Validated by `homelab-storage` |
| Storage observability artifact collection (PVC status, df, lsblk) | ✅ Validated by `homelab-storage` |
| ZFS pool/list evidence collection (conditional on tool presence) | ✅ Validated by `homelab-storage` |
| First restore drill with single-pod PVC | ✅ Unblocked — #60 / #84 |

### What is blocked by the RWX gap
| Scenario | Blocked until |
|---|---|
| NAS-style concurrent write access from multiple pods | RWX-capable storage class (NFS CSI, Longhorn, etc.) |
| Media service with shared read-only media volume | At minimum: `ReadOnlyMany` via NFS |
| Cross-pod restore validation | RWX storage class |
| Service-catalog workloads that share a data directory | Same |

### Minimum evidence to unblock shared-storage
1. A `ReadWriteMany` or `ReadOnlyMany` PVC successfully created on the cluster.
2. Two distinct pods both mounting the same PVC simultaneously.
3. A write from pod A visible from pod B.

Until that evidence exists, every test that depends on shared access **must**
call `pytest.skip` with a reference to this issue (#62):
```python
pytest.skip("RWX/shared-storage scenarios blocked by #62 until ReadWriteMany storage class is available")
```

---

## 3. Storage Observability Surface (#70, #78)

### Generic storage artifacts (always collected)

These artifacts are written to `/tmp/results/` by `test_collects_storage_observability_artifacts`
in `tests/homelab_storage/test_local_path_persistence.py` on every storage-lane run:

| Artifact | Command | What it shows |
|---|---|---|
| `storage-pvc.json` | `kubectl get pvc <name> -o json` | PVC phase, capacity, access modes, storage class, bound PV |
| `storage-disk-usage.txt` | `df -h <mount-path>` | Capacity, used, available, mount point |
| `storage-ownership.txt` | `stat <mount-path>` | UID/GID, permissions, inode |
| `storage-findmnt.txt` | `findmnt <mount-path>` | Filesystem type, source device, mount options |
| `storage-df.txt` | `df -h <mount-path>` | Redundant human-readable capacity snapshot |
| `storage-statfs.txt` | `stat -f <mount-path>` | Block size, total/free/available blocks |
| `storage-lsblk.txt` | `lsblk -f` | Block devices, filesystem labels, UUIDs |
| `storage-pods-before.json` | `kubectl get pods` | Pod state snapshot before rollout restart |
| `storage-pods-after.json` | `kubectl get pods` | Pod state snapshot after rollout restart |
| `storage-restart.txt` | `kubectl rollout restart` | Restart command output |
| `storage-rollout-status.txt` | `kubectl rollout status` | Rollout convergence confirmation |

### ZFS-specific artifacts (collected only when tools present)

| Artifact | Command | Condition |
|---|---|---|
| `storage-zpool.txt` | `zpool status -x` | `command -v zpool` exits 0 |
| `storage-zfs.txt` | `zfs list` | `command -v zfs` exits 0 |

ZFS checks use `|| true` so they degrade gracefully on non-ZFS nodes. Absence
of ZFS evidence is not a test failure; the artifacts will be empty or contain
the "not found" message.

### Relationship to persistence claims
The storage observability artifacts support restart/update persistence
validation as follows:
- **Pre-restart snapshot** (`storage-pods-before.json`) and **post-restart snapshot**
  (`storage-pods-after.json`) prove pod identity changed while the data file survived.
- **`storage-findmnt.txt`** proves the bind mount used the expected filesystem type
  (typically `ext4` on local-path, `zfs` on ZFS-backed nodes).
- **`storage-lsblk.txt`** proves the block device backing the PV is the expected one.
- ZFS artifacts provide pool-health evidence that a failing pool would surface before
  data loss occurs.

---

## 4. Fleet-Client Contract (#72)

### Cluster nodes vs. bootc clients

The lab hardware has two distinct roles that **must not be conflated**:

| Role | Hosts | k3s member | KubeVirt capable | In scope for cluster workload validation |
|---|---|---|---|---|
| **Cluster node** | ghost, exo-0 | Yes | ghost only | Yes |
| **Bootc client** | jorge's Bluefin laptop, other contributor machines | No | No | No |

### What this repo validates for bootc clients
- `bootc status` reports the expected image reference and digest.
- `bootc upgrade --check` exits 0 when no update is pending.
- Staged-deployment and rollback contracts (via ephemeral VM tests in the
  `system/` behave suite, not via live client enrollment).
- `uupd` orchestration smoke (`tests/system/features/uupd.feature`).

### What this repo explicitly does not validate
- Enrolling contributor laptops as k3s agents or KubeVirt nodes.
- MDM / fleet-dashboard product features.
- Bluetooth, Wi-Fi, or peripheral hardware on client machines.
- Any workload that would require the client to run workflow pods.

### Evidence model
Tests that distinguish cluster-member behavior from client behavior should
label their test environment in artifacts:
```python
write_artifact("cluster-node-info.json",
    json.dumps({"hostname": socket.gethostname(), "role": "cluster-node"}))
```
Client-side bootc assertions run inside ephemeral KubeVirt VMs (not on live
client hardware) so evidence is always VM-scoped and cluster-managed.

---

## 5. Local Hostname and Routing Contract (#73)

### First representative contract

The lab validates the following hostname/routing pattern for exposed in-cluster services:

| Layer | Contract | Evidence |
|---|---|---|
| **Cluster DNS** | `<service>.<namespace>.svc.cluster.local` resolves from within the cluster | `getent hosts` in test pod (`access-dns.txt`) |
| **TLS handshake** | Service on port 8443 completes TLS with a valid certificate | `openssl s_client` output with `Protocol version` (`access-openssl.txt`) |
| **SNI routing** | `Host: <hostname>` header routes to the correct backend | `curl -H "Host: <hostname>"` returns `access-ok` (`access-curl.txt`) |

### Separation of concerns
- **Service discovery** (this contract): cluster DNS + in-cluster reachability.
- **TLS issuance**: tracked separately; current test uses a self-signed cert in the fixture.
- **Auth-gating**: deferred to service-catalog auth-gating lane (#61).
- **External/LAN reachability**: `bluespeed.local` reverse proxy patterns are
  tracked under bluespeed; this repo validates only in-cluster service access.

### Non-goals
- Validating ingress controllers or NodePort exposure from outside the cluster.
- Testing ACME/Let's Encrypt certificate rotation.
- Any LAN hostname that requires mDNS or split-horizon DNS on the workstation.

---

## 6. HTTPS Service-Exposure Lane (#58)

This section defines the first HTTPS service-exposure validation lane under the
access/TLS epic (#53). The representative service endpoint is the
`homelab-access` fixture — a Python HTTPS server deployed in-cluster with a
self-signed TLS certificate, SNI-based host routing, and optional basic auth.

### Representative endpoint

| Property | Value |
|---|---|
| **Service** | `homelab-access.<namespace>.svc.cluster.local:8443` |
| **Expected hostname** | `homelab-access.local` |
| **TLS certificate** | Self-signed, CN=`homelab-access.local`, 1-day validity |
| **Protocol** | HTTPS (TLS 1.2+) |
| **Health response** | `access-ok` on `GET /healthz` with correct `Host` header |

### Minimum evidence the lane must capture

Every run of this lane must produce the following evidence artifacts:

| Check | Evidence artifact | Pass criteria |
|---|---|---|
| **Cluster DNS resolution** | `https-dns.txt` | `getent hosts` resolves the service FQDN to a cluster IP |
| **TLS handshake completes** | `https-tls-handshake.txt` | `openssl s_client` output contains `Protocol version` and `Verify return code` |
| **Certificate subject matches** | `https-cert-subject.txt` | Certificate subject CN or SAN matches the expected hostname |
| **TLS version is 1.2 or higher** | `https-tls-version.txt` | Negotiated protocol is `TLSv1.2` or `TLSv1.3` |
| **HTTPS reachability** | `https-reachability.txt` | `curl --resolve` over HTTPS returns HTTP 200 with body `access-ok` |
| **Wrong-host rejection** | `https-wrong-host.txt` | Request with an incorrect `Host` header returns HTTP 421 |

### Fixture deployment

The lane reuses the `homelab-access-probe` WorkflowTemplate's fixture
(§1 Service access class). The fixture deploys:

1. A TLS secret (`homelab-access-tls`) with a self-signed certificate.
2. An auth secret (`homelab-access-auth`) with basic credentials.
3. A Python HTTPS server that validates `Host` headers and optionally
   enforces basic auth.
4. A ClusterIP Service on port 8443.

The HTTPS exposure lane runs with `auth-mode=false` — auth-gating is a
separate concern validated by the auth lane (#61).

### What this lane validates vs. what it defers

| Concern | This lane (#58) | Deferred to |
|---|---|---|
| TLS handshake and certificate presence | ✅ | — |
| Certificate subject/SAN match | ✅ | — |
| TLS version enforcement (1.2+) | ✅ | — |
| HTTPS reachability from within the cluster | ✅ | — |
| Host-header routing correctness | ✅ | — |
| Wrong-host rejection (421) | ✅ | — |
| Basic auth / credential gating | ❌ | #61 (auth-gating lane) |
| ACME / Let's Encrypt certificate issuance | ❌ | Future cert-manager lane |
| External/LAN reachability (NodePort, Ingress) | ❌ | bluespeed / ingress lane |
| Firewall rules / NetworkPolicy enforcement | ❌ | Follow-up under #53 |
| mTLS between services | ❌ | Future service-mesh lane |

### Follow-up work called out explicitly

1. **Auth-gating lane (#61)**: Once this HTTPS lane proves transport security,
   #61 layers credential validation on top. The `auth-mode=true` parameter
   and `test_auth_probe.py` test module are already scaffolded in the
   `homelab-access-probe` WorkflowTemplate but not yet covered by a lane
   definition.

2. **Firewall / NetworkPolicy**: The current lane validates reachability
   within the cluster namespace but does not assert NetworkPolicy rules that
   restrict cross-namespace access. A NetworkPolicy validation lane should be
   filed under #53 once the HTTPS and auth lanes are stable.

3. **Certificate lifecycle**: The fixture uses ephemeral 1-day self-signed
   certs created per workflow run. Validating cert-manager integration,
   renewal, and ACME issuance is out of scope and should be tracked as a
   separate issue.

4. **External exposure**: LAN-facing reverse proxy patterns
   (`bluespeed.local`) and ingress controller validation belong to the
   bluespeed project, not this lane.
## 9. Deferred Non-Core Service Follow-Up: Home Assistant-Class Workloads (#69)

This section formally defines **Home Assistant-class workloads** as deferred
scope under the service-catalog epic (#51). These are non-core homelab
services that have significant infrastructure requirements beyond what the
current lab validates. This section exists to make the deferral explicit and
managed rather than implied.

### What "Home Assistant-class" means

Home Assistant is the representative example, but this class covers any
homelab service that exhibits most of these characteristics:

| Characteristic | Example |
|---|---|
| **Long-lived stateful service** | Home Assistant, Node-RED, Zigbee2MQTT |
| **Persistent configuration database** | SQLite/PostgreSQL on a PVC |
| **Web UI requiring auth-gated HTTPS** | HA dashboard, Node-RED editor |
| **Hardware/peripheral integration** | Zigbee/Z-Wave USB dongles, Bluetooth |
| **LAN service discovery** | mDNS, SSDP, multicast |
| **Addon/plugin ecosystem** | HA integrations, HACS, custom components |
| **Upgrade-sensitive state** | DB migrations on version bump |

### Why this is deferred

Non-core service validation depends on infrastructure that the lab has not
yet proven. Attempting to validate Home Assistant-class workloads before
the prerequisites are durable would either produce false confidence (testing
against ad hoc workarounds) or block on unresolved substrate gaps.

### Entry criteria — all must be met before this work becomes active

| # | Criterion | Tracks to |
|---|---|---|
| 1 | **Shared service-catalog workload contract is defined and implemented** — the minimum deployment, persistence, reachability, and teardown evidence that every service lane must prove | #66 (contract), #79 (pipeline) |
| 2 | **At least one durable media-service lane is running** — proving the service-catalog pipeline works end-to-end with a real workload | #59 → #80 |
| 3 | **At least one durable non-media service lane is running** — proving the pipeline generalizes beyond media workloads | #64 → #81 |
| 4 | **HTTPS exposure lane is validated** — the access/TLS infrastructure that any auth-gated UI depends on | #58 |
| 5 | **Auth-gating lane is validated** — the credential-enforcement layer that any exposed service UI depends on | #61 |
| 6 | **Storage persistence survives restart** — proven by the homelab-storage lane; non-core services depend on this for config/DB durability | `homelab-storage` lane |

### What this repo will validate when entry criteria are met

Once active, the Home Assistant-class lane should validate:

- **Deployment**: Service deploys via raw manifests into a dedicated namespace
  using the shared service-catalog pipeline (#79).
- **Persistence**: Configuration database survives `rollout restart` (same
  contract as homelab-storage, applied to the service's config PVC).
- **HTTPS reachability**: Web UI is reachable over HTTPS within the cluster
  (reuses the access-probe infrastructure from #58).
- **Auth-gating**: Web UI rejects unauthenticated access and accepts valid
  credentials (reuses the auth-gating infrastructure from #61).
- **Health endpoint**: Service-specific health or readiness endpoint returns
  a healthy status after deployment.

### What remains explicitly out of scope even when active

| Concern | Why deferred further |
|---|---|
| **USB/Zigbee/Z-Wave device passthrough** | Requires KubeVirt device passthrough or privileged containers; tracked under #67 |
| **mDNS/SSDP LAN discovery** | Requires host-network or multicast support; tracked under #67 |
| **Addon/plugin ecosystem validation** | Product-scope, not infrastructure validation |
| **Home Assistant OS or Supervised installs** | This repo validates k8s-native container workloads only |
| **VM-backed role validation** | Tracked under #54, explicitly not a blocker for k8s-first lanes |
| **Identity provider / SSO integration** | Deferred from the auth-gating lane; applies here too |

### Relationship to the service-catalog epic (#51)

This issue is child #6 of #51, deliberately sequenced last:

1. ~~Shared workload contract~~ → #66
2. ~~Shared pipeline~~ → #79
3. ~~Media-service lane~~ → #59 → #80
4. ~~Non-media lane~~ → #64 → #81
5. ~~Hardware/discovery splits~~ → #63, #67
6. **Non-core deferred follow-up** → **this section (#69)**

The sequencing is intentional: non-core services consume all of the
infrastructure that the earlier lanes prove. Activating #69 before the
earlier lanes are durable means testing against unvalidated substrate.

### How to activate this lane

When all entry criteria in the table above are met:

1. File a new implementation issue under #51 for the first Home
   Assistant-class workload (e.g., "implement Home Assistant validation
   lane in service-catalog pipeline").
2. The implementation issue should reference this section for scope and
   explicitly inherit the shared workload contract from #66.
3. Move this section's status from "deferred" to "active" in the blockers
   table below (§7).
4. Do not remove this section — it serves as the design record for why the
   work was deferred and what the activation criteria were.

---

## 7. Known Blockers and Deferred Work

| Issue | Status | Dependency |
|---|---|---|
| #62 RWX / shared-storage | ❌ blocked | NFS CSI or Longhorn installation on ghost |
| #63 GPU transcoding lane | ✅ defined | Substrate work from #54 required before tests execute |
| #61 auth-gated service UI | ❌ deferred | service-catalog baseline lane first |
| #60 first restore drill | ⏳ ready | #62 not required for single-pod RWO restore |
| #84 PVC restore drill with backup artifact | ⏳ ready | #62 not required |
| #69 Home Assistant-class workloads | ❌ deferred | All entry criteria in §6 (#66, #79, #80, #81, #58, #61, storage lane) |
| #60 first restore drill | ✅ implemented | `homelab-restore-drill` WorkflowTemplate + `tests/homelab_backup/` |
| #84 PVC restore drill with backup artifact | ✅ implemented | `homelab-restore-drill` WorkflowTemplate + `tests/homelab_backup/` |
| Media service lane | ❌ deferred | #62 (shared mount) + #63 (GPU) |
| #64 first non-media homelab workload lane | ✅ implemented | `homelab-print-service` WorkflowTemplate + `tests/service_catalog/print/` |
| #67 printer-device access + LAN discovery | ✅ defined | Substrate work from #54 required before tests execute |

---

## 7. Print-Service Workload Lane (#64)

First non-media homelab workload lane.  Validates the in-cluster deployment
contract for an OpenPrinting/CUPS-class service (source idea:
`projectbluefin/bluespeed#11`) without hardware attachment or LAN
discovery, which are split into #67.

### Behavior table

| Behavior | Test | Artifact |
|---|---|---|
| Deployment reaches `availableReplicas >= 1` | `test_deployment_becomes_ready` | `print-deployment.json` |
| Pod reaches `Running` state | `test_pod_reaches_running_state` | `print-pods.json` |
| ClusterIP service has endpoints on port 631 | `test_service_has_endpoints` | `print-endpoints.json` |
| Config PVC (1Gi) binds on `local-path` | `test_config_pvc_is_bound` | `print-pvc-homelab-print-config.json` |
| Config PVC reports expected capacity and access modes | `test_config_pvc_capacity_and_access_modes` | same |
| Config PVC uses `local-path` storage class | `test_config_pvc_storage_class` | same |
| `/config` mount is writable | `test_config_mount_is_writable` | — |
| IPP port 631 reachable in-cluster | `test_ipp_port_is_reachable_in_cluster` | `print-reachability.txt` |
| Cluster DNS resolves service FQDN | `test_cluster_dns_resolves_print_service` | `print-dns.txt` |
| `PUID`, `PGID`, `TZ` env vars present | `test_puid_pgid_tz_env_vars_are_present` | `print-env.txt` |
| Config sentinel survives `rollout restart` | `test_config_state_survives_rollout_restart` | `print-rollout-status.txt` |
| Storage observability artifacts collected | `test_collects_storage_observability_artifacts` | `print-config-{df,findmnt,stat}.txt` |
| USB printer device access | `test_usb_printer_device_access_is_out_of_scope_for_base_lane` | `pytest.skip` → #67 |
| LAN mDNS autodiscovery | `test_lan_mdns_discovery_is_out_of_scope_for_base_lane` | `pytest.skip` → #67 |

### Out-of-scope splits

| Behavior | Reason | Tracked |
|---|---|---|
| USB printer device access (`/dev/usb/lp0` hostPath) | Host device passthrough; depends on #54 substrate work | #67 |
| LAN mDNS / avahi self-discovery | Requires avahi sidecar and NodePort/LoadBalancer exposure | #67 |
| Administration UI auth-gating | Deferred beyond #67 | TBD |
| NodePort / LoadBalancer for LAN printing | Out of scope for base in-cluster validation | #67 |

### Dependencies
- #52 homelab storage (local-path PVC contract proven first)
- #53 homelab access (cluster DNS + in-cluster reachability proven first)
- #54 homelab substrate (in-cluster scheduling proven first)

### Fixture image note
The WorkflowTemplate uses `nginx:1.27.5-alpine` on port 631 as a
representative stub (same approach as the media-service lane).  Replace
with `lscr.io/linuxserver/cups:latest` when the lane graduates from
validation to integration testing.  All five deployment-contract behaviors
(PVC, env, port, DNS, restart persistence) are fully representative even
with the stub image.

---

## 8. GPU Transcoding and Hardware-Passthrough Lane (#63)

Split from the base media-service lane (#59).  Defines the in-cluster
validation contract for NVIDIA CUDA/NVENC hardware-accelerated transcoding.
AMD ROCm and Intel QSV paths are deferred; KubeVirt VM-backed passthrough
is tracked in #54.

### Substrate dependency chain

```
#54 substrate (GPU device plugin + driver stack on ghost)
    → #63 this lane (GPU transcoding contract proven)
    → #59 base media lane (unblocked from GPU split)
```

### Required substrate from #54 before this lane executes

| Requirement | How to verify |
|---|---|
| NVIDIA driver loaded on host (ghost) | `nvidia-smi` on host returns GPU name |
| nvidia-container-toolkit installed | `nvidia-ctk --version` succeeds |
| nvidia-device-plugin DaemonSet running | `kubectl get pods -A -l app=nvidia-device-plugin` shows Running |
| Node reports allocatable GPU capacity | `kubectl get nodes -o json` has `nvidia.com/gpu` > 0 in allocatable |
| Container runtimeClass `nvidia` registered | `kubectl get runtimeclass nvidia` exists |

### Behavior table (tests run only when GPU is allocatable)

| Behavior | Test | Artifact |
|---|---|---|
| Node has allocatable GPU capacity | `test_gpu_node_has_allocatable_capacity` | `gpu-node-allocatable.txt` |
| Device plugin DaemonSet is Running | `test_device_plugin_daemonset_is_running` | `gpu-device-plugin-pods.json` |
| GPU Deployment reaches `availableReplicas >= 1` | `test_gpu_deployment_becomes_ready` | `gpu-deployment.json` |
| Pod with GPU resource limit reaches Running | `test_gpu_pod_reaches_running_state` | `gpu-pods.json` |
| GPU resource limit present in pod spec | `test_gpu_resource_limit_present_in_pod_spec` | — |
| /dev/nvidia* device node visible in container | `test_gpu_device_node_visible_in_container` | `gpu-dev-nodes.txt` |
| nvidia-smi reports GPU in container | `test_nvidia_smi_reports_gpu_in_container` | `gpu-nvidia-smi.txt` |
| ffmpeg lists nvenc encoder | `test_ffmpeg_lists_nvenc_encoder` | `gpu-ffmpeg-encoders.txt` |
| ffmpeg hardware transcode completes (1-second clip) | `test_ffmpeg_hardware_transcode_completes` | `gpu-transcode-output.txt` |
| GPU capacity recovers after pod deletion | `test_gpu_resource_is_released_after_pod_deletion` | `gpu-allocatable-after-delete.txt` |
| KubeVirt VM-backed passthrough | `test_kubevirt_vm_gpu_passthrough_is_out_of_scope_for_this_lane` | `pytest.skip` → #54 |
| Multi-GPU / MIG slicing | `test_multi_gpu_and_mig_slicing_is_out_of_scope_for_this_lane` | `pytest.skip` → deferred |
| AMD ROCm / Intel QSV | `test_amd_and_intel_gpu_paths_are_out_of_scope_for_this_lane` | `pytest.skip` → deferred |

### Module-level skip gate

All tests are gated at module import time by `_gpu_allocatable_on_any_node()`.
If no `nvidia.com/gpu` (or `TEST_GPU_RESOURCE_KEY`) resource appears in any
node's allocatable map, the entire module is skipped with a message pointing
to the #54 substrate requirements above.  This means the GPU lane can be
included in standard CI without failing on non-GPU clusters.

### WorkflowTemplate guard step

`check-gpu-prerequisites` runs before namespace creation.  If it fails
(no allocatable GPU, no Running device plugin pods), the workflow exits
with a clear error message listing the three steps needed from #54.  No
cluster resources are created for a non-GPU cluster.

### Out-of-scope splits

| Path | Reason | Tracked |
|---|---|---|
| KubeVirt VM-backed GPU passthrough (VFIO/IOMMU) | Requires additional substrate + #54 | #54 |
| Multi-GPU scheduling / MIG slicing | No multi-GPU hardware in current lab | Deferred |
| Intel QSV / VAAPI (i915/xe) | Deferred until NVIDIA path validated | Deferred |
| #67 printer-device access + LAN discovery | ✅ defined | Substrate work from #54 required before tests execute |

---

## 10. Printer-Device Access and LAN Discovery Lane (#67)

Split from the base non-media print-service lane (#64).  Defines the
validation contracts for USB printer device access and LAN mDNS
auto-discovery — the two hardware/network-heavy paths that are explicitly
out of scope for the base lane.

### Boundary between base lane and this lane

| What | Base lane (#64) | This lane (#67) |
|---|---|---|
| CUPS deployment + PVC | ✅ validated | — |
| IPP port 631 in-cluster reachability | ✅ validated | — |
| Env injection (PUID/PGID/TZ) | ✅ validated | — |
| Config PVC rollout persistence | ✅ validated | — |
| USB device node in container | — | ✅ validated |
| CUPS lpinfo USB URI | — | ✅ validated |
| NodePort/LB service for LAN | — | ✅ validated |
| avahi sidecar running + advertising | — | ✅ validated |
| _ipp._tcp mDNS service record | — | ✅ validated |

### Substrate assumptions from #54

#### USB device-access path
| Requirement | How to verify |
|---|---|
| USB printer attached to ghost node | `lsusb` on host reports printer VID/PID |
| `usblp` or `usbfs` kernel module loaded | `lsmod \| grep usblp` on host |
| Device node exists at expected path | `ls -la /dev/usb/lp0` on host |
| Container runtime device allow-list includes device node | Pod spec carries `securityContext` or device allow-list entry |

#### LAN mDNS / discovery path
| Requirement | How to verify |
|---|---|
| NodePort or LoadBalancer service on port 30631 | `kubectl get svc -A \| grep 631` |
| avahi-daemon sidecar running | sidecar container Ready in pod status |
| avahi service file for _ipp._tcp present | `/etc/avahi/services/cups.service` in sidecar |
| Multicast not filtered on node NIC | `avahi-browse -t _ipp._tcp` from host returns result |

### Behavior table

#### Class 1: TestPrinterDeviceAccessLane (gate: `TEST_USB_PRINTER_DEVICE` env var)

| Behavior | Test | Artifact |
|---|---|---|
| USB device node visible in container | `test_usb_device_node_exists_in_container` | `print-device-node-check.txt` |
| Device node is a character device | `test_usb_device_node_is_character_device` | `print-device-node-ls.txt` |
| CUPS lpinfo reports USB device URI | `test_cups_can_detect_local_usb_printer` | `print-lpinfo.txt` |
| CUPS accepts test print job | `test_cups_accepts_test_print_job` | `print-test-job.txt` |
| Device node r/w permissions allow CUPS | `test_usb_device_permissions_allow_cups_user` | `print-device-permissions.txt` |
| KubeVirt USB passthrough | `test_kubevirt_usb_passthrough_is_out_of_scope` | `pytest.skip` → #54 |

#### Class 2: TestLanDiscoveryLane (gate: `TEST_AVAHI_ENABLED=true`)

| Behavior | Test | Artifact |
|---|---|---|
| IPP service type is NodePort or LoadBalancer | `test_ipp_service_exposed_outside_cluster` | `print-discovery-service.json` |
| NodePort reachable on node IP | `test_nodeport_is_reachable_on_node_ip` | `print-discovery-nodeport-reach.txt` |
| avahi sidecar container is Ready | `test_avahi_sidecar_container_is_running` | `print-discovery-pods.json` |
| avahi-daemon process is active | `test_avahi_daemon_process_is_active` | `print-avahi-process.txt` |
| avahi advertises _ipp._tcp record | `test_avahi_daemon_advertises_ipp_service` | `print-avahi-browse.txt` |
| avahi service file present in /etc/avahi/services/ | `test_avahi_service_file_is_present_in_config` | `print-avahi-services.txt` |
| Auth-gated CUPS UI | `test_auth_gated_cups_ui_is_out_of_scope` | `pytest.skip` → deferred |
| Split-horizon DNS for CUPS | `test_split_horizon_dns_for_cups_is_out_of_scope` | `pytest.skip` → bluespeed |

### WorkflowTemplate parameters

| Parameter | Default | Purpose |
|---|---|---|
| `usb-device-path` | `""` | Set to host device path (e.g. `/dev/usb/lp0`) to enable USB tests |
| `avahi-enabled` | `"false"` | Set to `"true"` to enable mDNS discovery tests |
| `ipp-nodeport` | `"30631"` | NodePort number for external IPP service |

When both parameters are at their defaults, all tests skip with informative messages
and the workflow exits successfully — non-hardware CI is not broken.
