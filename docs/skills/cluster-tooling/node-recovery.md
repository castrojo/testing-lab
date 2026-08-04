---
name: cluster-node-recovery
description: >
  Wedged-node triage without SSH and FCOS memory limit quirks.
---

# Node Recovery and Quirks

> [!CAUTION]
> Host-root recovery and privileged `nsenter` actions are private
> maintainer/operator procedures. Do not use workstation SSH to `ghost` or
> `exo-0`; use Kubernetes API-driven recovery workflows and escalate host
> maintenance through the approved private channel.

## Fedora CoreOS 44 (FCOS) Container Memory Limits and systemd-cgroup v2 Overhead

On Fedora CoreOS, containers scheduled using unified cgroups v2 and the `systemd` cgroup driver undergo scope registration via dbus. This triggers kernel memory allocations, systemd-user accounting, and auditing, which requires a baseline overhead of 12-20 MiB of memory before any user workload even executes.

### 1. Diagnosis
If containers or shims crash instantly during initialization with exit code `128`, check `kubectl describe pod` for:
`failed to create containerd task: ... OCI runtime create failed: container init was OOM-killed (memory limit too low?)`

### 2. Remediation
Always configure container memory limits well above this threshold for nodes running CoreOS.
- **Standard pause/sleep containers**: minimum `32Mi` memory limits (such as in `k3s-firewalld-config`, `mask-sleep-targets`, `registry-mirror-config`, and `inotify-tuning`).
- **Shell-based or kubectl utility containers**: minimum `64Mi` memory limits (such as in `virtio-console-module`).

### 3. SELinux Key Injection Warning
When running privileged pods that mount the host root `/` (`hostPath: /`) and write or create files (like writing public keys to `/home/core/.ssh/authorized_keys`), containerd applies container-specific SELinux labels (`container_file_t` or `home_root_t`). This prevents `sshd` from reading the keys on the host, resulting in `Permission denied (publickey)`.
- **Fix**: Run `nsenter -t 1 -m -u -i -n restorecon -R -v /home/core/.ssh` on the host OS from a privileged container to restore the correct `ssh_home_t` contexts.

## Wedged Node Triage Without SSH (2026-07-09 incident)

When a node shows `Ready` but pods on it are stuck Terminating/Pending for hours, do not trust
the Ready condition alone — check three signals via the k8s API:

1. **Lease vs conditions skew**: `kubectl get lease -n kube-node-lease <node>` fresh while
   `.status.conditions[].lastHeartbeatTime` is hours stale means the kubelet's lease goroutine
   is alive but its pod-sync and status loops are wedged.
2. **Kubelet's own pod view**: `kubectl get --raw /api/v1/nodes/<node>/proxy/pods` — if it
   returns 0 pods while the API server lists pods on the node, those API objects are orphans
   and containers are verifiably gone. That satisfies the documented precondition for
   `kubectl delete pod --grace-period=0 --force` (kubernetes/website:
   force-delete-stateful-set-pod.md — safe only when processes are confirmed terminated).
3. **In-cluster shell probe**: if a recovery shell opens and builtins (`echo`) work but any binary exec hangs,
   host root-filesystem I/O is wedged (D-state) — only a power cycle fixes it. Cordon the node.

Cleanup order: cordon → force-delete orphaned pods (frees scheduler requests) → reschedule
movable workloads → for Released local-path PVs on the dead node, patch
`persistentVolumeReclaimPolicy: Retain` to stop the provisioner's helper-pod retry churn;
flip back to Delete after the node returns. Never delete the PV object while the node is down
(orphans the backing directory).

Known trigger: hostPID build pods SIGTERMing host daemons (issue #268) — the same event can
kill journald/sshd on workers and crash k3s on ghost (systemd restarts it; expect a short
API outage and a wave of `connection refused` workflow errors that self-heal on next cron tick).
