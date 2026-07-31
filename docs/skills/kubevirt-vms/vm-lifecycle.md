---
name: kubevirt-lifecycle
description: >
  KubeVirt VM lifecycle: disks, SSH injection, scheduling, teardown, golden disks.
---

## Core Process

### 1. Disk placement — all VM data goes on ghost-data SSD

Ghost has two storage devices:
- `/dev/nvme0n1p3` → mounted at `/var` — OS partition (~1.9T, **do not fill**)
- `/dev/sdb` → mounted at `/var/mnt/ghost-data` — data SSD (~1.9T, use this for VM disks)

**Rule: all VM disk files and build staging MUST live under `/var/mnt/ghost-data/`.**
Putting VM disk images on `/var/tmp` (nvme) will fill the OS partition and trigger
kubelet disk-pressure, evicting all pods and crashing the cluster.

| Pipeline | Disk storage location |
|---|---|
| Bluefin containerDisk build staging | `/var/mnt/ghost-data/bluefin-cd-build/` |
| LLM model cache | `/var/mnt/ghost-data/llm-models/` |

**Never use `/var/tmp` for VM disk files.** It is on the nvme OS partition.

### 2. Bluefin VM model — containerDisk (no hostDisk files)

Bluefin test VMs use KubeVirt `containerDisk` — an OCI image containing the qcow2
disk image, stored in the local Zot registry. No reflink, no hostDisk, no golden disk.

**Critical `containerDisk.json` requirement:** When building a custom containerDisk from `scratch` (using `buildah`), KubeVirt *requires* a `/containerDisk.json` file at the root of the image to declare the disk capacity:
```json
{"volumes":[{"image":"disk.qcow2", "capacity":"25Gi"}]}
```
Without this file, `virt-controller` attempts to fetch the uncompressed size from the registry API. If that fails (e.g. against an insecure local registry without proper credentials), KubeVirt defaults the pod's `ephemeral-storage` limit to 50M. This results in the virt-launcher pod being evicted immediately upon extracting the disk image, causing a `No disk capacity` error. Always inject this JSON metadata during the build step.

```
build-containerdisk (build-containerdisk.yaml)
  ├─ check       — skopeo: exists in <lab-ip>:30500/bluefin-containerdisk:<tag>?
  ├─ install-to-disk  — podman run bootc install to-disk → /mnt/ghost-data/bluefin-cd-build/<tag>/disk.raw
  ├─ configure-disk   — inject test user, SSH, GDM autologin, sudoers, selinux=0
  └─ convert-and-push — qemu-img raw→qcow2, buildah OCI wrap, push to zot:30500
         │
provision-containerdisk-vm (provision-containerdisk-vm.yaml)
  └─ VM boots from containerDisk: <lab-ip>:30500/bluefin-containerdisk:<tag>
```

Check if a containerDisk exists:
```bash
# API-driven registry check
skopeo inspect --tls-verify=false docker://<lab-ip>:30500/bluefin-containerdisk:testing
```

**Zot data loss:** The `zot-writable` pod (port 30500) can lose its manifest
index on a pod or k3s restart even though blobs may still exist. Check the
registry through its API before running the pipeline; rebuild if the manifest
is unavailable. Do not inspect the host path with workstation SSH.

### 2a. Native bootc OCI boot — what is and isn't possible

**KubeVirt cannot boot a bootc OCI image directly without disk preparation.** This is a hard
constraint of the current KubeVirt architecture. Summary of what was verified:

**What the bootc OCI image contains (verified on `bluefin:testing`):**
- Kernel: `/usr/lib/modules/<version>/vmlinuz`  (e.g. `7.0.12-201.fc44.x86_64`)
- Initramfs: `/usr/lib/modules/<version>/initramfs.img`
- These paths are accessible via `KubeVirt kernelBoot.container`

**Why `kernelBoot.container` alone is not enough:**
- `kernelBoot.container` extracts kernel + initramfs from an OCI image and passes them to QEMU
  as `-kernel`/`-initrd` — it does NOT provide a root filesystem
- The bootc/ostree initramfs requires `root=UUID=<uuid>` and `ostree=/ostree/boot.1/default/<hash>/0`
  — both set by `bootc install to-disk` at disk creation time
- Without an ostree-deployed root disk the VM fails to mount `/` and panics

**Why `containerDisk` cannot use the raw bootc OCI image:**
- KubeVirt `containerDisk` expects a disk image file at `/disk/` inside the OCI image (raw or qcow2)
- A bootc OCI image contains OS filesystem layers, not a disk image — KubeVirt rejects it
- CDI `DataVolume` registry source has the same constraint

**Verified boot cmdline structure (reference for debugging):**
```
BOOT_IMAGE=(hd0,gpt3)/boot/ostree/default-<hash>/vmlinuz-7.0.12-201.fc44.x86_64
root=UUID=<disk-uuid> rw selinux=0 ostree=/ostree/boot.1/default/<hash>/0
```

**The minimum required disk prep is `bootc install to-disk`.** This can be done:
1. As a pre-built containerdisk (current: build-containerdisk → Zot → containerDisk) — schedulable on any node
2. Inline in the workflow: `bootc install to-disk /path/to/disk.raw` → `hostDisk` — ghost-local only

**Diagnosing kernel/initramfs paths in a running VM (guest-agent):**
```bash
kubectl exec -n <ns> <virt-launcher-pod> -c compute -- \
  virsh qemu-agent-command 1 \
  '{"execute":"guest-exec","arguments":{"path":"cat","arg":["/proc/cmdline"],"capture-output":true}}'
# Decode result: base64 -d <<< <out-data>
```

### 2b_btrfs. Bluefin btrfs disk layout (bootc install to-disk output)

`bootc install to-disk` creates:
- `p1` = BIOS boot (1M)
- `p2` = EFI (512M)
- `p3` = btrfs root (14.5G)

**`btrfs subvolume list` returns EMPTY** — there are NO named btrfs subvolumes in a
bootc-installed Bluefin disk. Mount the toplevel with no `subvol=` option:
```bash
mount -t btrfs ${LOOP}p3 /mnt/btroot
```

The ostree deployment structure at boot:
- `/etc` is bind-mounted from `ostree/deploy/default/deploy/<hash>.0/etc/` (btrfs)
- `/var` is the real content at `ostree/deploy/default/var/` (btrfs, NOT a subvolume)
- `/` is composefs overlay (read-only lower layer from image)

**Post-install etc/ injection caveat (ostree 3-way merge):**
Files that already exist in the image's `usr/etc/` (e.g. `sshd_config.d/10-test.conf`,
`sshd_config`) get RESET to image content at first boot by the ostree merge.
NEW files added to `deploy/.../etc/` (e.g. `etc/passwd` entries, `etc/sudoers.d/`,
`etc/gdm/custom.conf`) survive if they have no counterpart in the image's `usr/etc/`.

**Do not rely on disk injection for SSH keys.** Use KubeVirt accessCredentials instead
(see section 2b). Even `var/` writes confirmed by VERIFY may not survive the
qemu-img raw→qcow2 conversion (sparse block allocation issue).

### 2b. SSH key injection — use KubeVirt accessCredentials (canonical pattern)

**Never bake SSH keys into the disk image.** Use KubeVirt's native mechanism:

```yaml
# In the VirtualMachine spec:
spec:
  template:
    spec:
      accessCredentials:
        - sshPublicKey:
            source:
              secret:
                secretName: bluefin-test-ssh-pubkey
            propagationMethod:
              qemuGuestAgent:
                users:
                  - root
```

The secret must exist in the **same namespace as the VM** (e.g. `bluefin-test`) and
contain only the public key value:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: bluefin-test-ssh-pubkey
  namespace: bluefin-test
type: Opaque
data:
  key: <base64 of "ssh-ed25519 AAAA...">
```

KubeVirt virt-controller injects the key via QEMU guest agent after the VM boots.
The VM must have `qemu-guest-agent` running (Bluefin has it). No disk modifications needed.
The key is visible to sshd within seconds of the QEMU guest agent starting.

**Requirements:** KubeVirt v1.8+ (confirmed present), qemu-guest-agent in VM (confirmed).
**Why not disk injection:** ostree resets etc/ files that exist in usr/etc/ at first boot;
var/ writes may not survive qemu-img sparse conversion.

**qemu-guest-agent variant gap:** `bluefin:testing` has `qemu-guest-agent.service` enabled by
default. `bluefin-lts:testing` and `aurora:testing` do NOT. Without the
guest agent the `AccessCredentialsSynchronized` condition never becomes True and `wait-for-vm`
times out. `build-containerdisk.yaml` works around this by explicitly symlinking the service
into `multi-user.target.wants` during the build's post-install phase — this is already done
and must be preserved when editing that template.

**New variant checklist — required before a new image tag's VMs can run:**

| Item | File |
|---|---|
| Namespace created | `manifests/<variant>-test-namespace.yaml` |
| SSH pubkey secret in namespace | same file (bluefin-test-ssh-pubkey Secret) |
| RBAC (kubevirt-manager Role + RoleBinding for argo SA) | `manifests/kubevirt-rbac.yaml` |
| `accessCredentials` lists both `root` and `bluefin-test` | `provision-containerdisk-vm.yaml` |
| CronWorkflow for nightly smoke | `manifests/nightly-<variant>.yaml` |
| Disk size measured and set per-variant | `argo/workflow-templates/build-containerdisk.yaml` default + `digest-watch.yaml` per-variant override |

Missing RBAC for a new namespace is the #1 cause of `wait-for-vm` exit 1 (`kubectl wait vmi` returns 403 Forbidden).

### 2c. Runtime user creation (more reliable than disk injection)

Even if `bluefin-test` user was added to `etc/passwd` during disk build, never assume
the home directory has correct ownership. Create the user and home directory at runtime
via root SSH immediately after root SSH is confirmed working:

```bash
# In the test runner, after root SSH is ready:
ROOT_SSH "
  # Create user and group robustly to handle potential UID/GID 1001 conflicts
  if ! id bluefin-test &>/dev/null; then
    if ! getent group bluefin-test &>/dev/null; then
      groupadd -g 1001 bluefin-test 2>/dev/null || groupadd bluefin-test 2>/dev/null || true
    fi
    G_ARG=\"\"
    if getent group bluefin-test &>/dev/null; then
      G_ARG=\"-g bluefin-test\"
    elif getent group 1001 &>/dev/null; then
      G_ARG=\"-g 1001\"
    fi
    useradd -m -u 1001 \${G_ARG} -G wheel -s /bin/bash -d /var/home/bluefin-test bluefin-test
  fi
  # Always fix home dir ownership — install -d creates parent dirs as root
  mkdir -p /var/home/bluefin-test
  chown 1001:1001 /var/home/bluefin-test
  chmod 750 /var/home/bluefin-test
  # Set sudoers
  echo 'bluefin-test ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/bluefin-test
  chmod 440 /etc/sudoers.d/bluefin-test
  # Set up SSH keys
  install -d -m 700 -o 1001 -g 1001 /var/home/bluefin-test/.ssh
  echo '${SSH_PUBKEY}' > /var/home/bluefin-test/.ssh/authorized_keys
  chown 1001:1001 /var/home/bluefin-test/.ssh/authorized_keys
  chmod 600 /var/home/bluefin-test/.ssh/authorized_keys
"
```

**IMPORTANT:** `install -d -m 700 -o 1001 -g 1001 /var/home/bluefin-test/.ssh` creates
`.ssh` with the right mode/owner BUT creates the PARENT `/var/home/bluefin-test/` as
root:root (mode 755). This makes pip install --user fail with EACCES when trying to
create `.local`. Always explicitly `chown 1001:1001 /var/home/bluefin-test` after.

### 2. Required KubeVirt feature gates

Two feature gates must be enabled in the `kubevirt` CR. If VM creation fails with
`feature gate is not enabled in kubevirt-config`, this is cluster drift — fix via GitOps:

```bash
kubectl patch kubevirt kubevirt -n kubevirt --type=merge --patch='
{
  "spec": {
    "configuration": {
      "developerConfiguration": {
        "featureGates": ["HostDisk", "ExperimentalIgnitionSupport"]
      }
    }
  }
}'
```

Persist this in `manifests/` so ArgoCD maintains it.

### 3. VM node scheduling

**containerDisk VMs** (Bluefin test VMs) do NOT need to be pinned to ghost. They use
OCI images from the local Zot registry and have no hostPath dependency. They can run on
any KubeVirt-capable node.

```yaml
# containerDisk VM — no nodeSelector needed; floats to any KubeVirt-capable node
spec:
  domain:
    devices:
      disks:
        - name: containerdisk
          disk: {}
  volumes:
    - name: containerdisk
      containerDisk:
        image: <lab-ip>:30500/bluefin-containerdisk:testing
```

**No hostDisk VMs remain.** All VM types use containerDisk or PVC — they schedule freely on any KubeVirt-capable node:
- **ContainerDisk VMs** (Bluefin, GnomeOS, Dakota, Flatcar): OCI image from Zot. No nodeSelector.
- **PVC-backed VMs** (Knuckle): `local-path` RWO PVC; KubeVirt auto-schedules on the PVC's node.

**nodeSelector and hostNetwork are NOT required for SSH/kubectl workflow steps.**
Pod IPs are routable across nodes via flannel and `kubectl exec` goes through
the API server. Workflow storage must use PVCs rather than hostPath volumes.

KubeVirt capacity is whatever nodes are currently online with
`kubevirt.io/schedulable: "true"` and `virt-handler` running.
No Argo global parallelism cap — Kubernetes pod scheduling (8 Gi/VM request) is the
real backpressure, so pods queue naturally when node RAM is exhausted.

**VM memory by image type:**
- bluefin `:testing` → 8 Gi (full GNOME + Wayland + AT-SPI + dogtail)
- bluefin-lts `:testing` → 8 Gi (same desktop stack — 4 Gi was wrong)
- LTS smoke PRs do NOT get a reduced allocation; same 8 Gi applies

### 4. hwprofile: standard vs full-hw

`provision-variant-vm` supports two hardware profiles:

```yaml
- name: hw-profile
  value: standard    # default: no TPM, no watchdog
# or
- name: hw-profile
  value: full-hw     # adds TPM 2.0, ich9 audio, i6300esb watchdog (for hardware-suite tests)
```

Use `full-hw` only when the test explicitly requires hardware attestation or watchdog behavior.

### 5. SSH readiness wait pattern

The canonical way to wait for a VM to be SSH-accessible:

```bash
# Step 1: wait for VMI Ready condition
kubectl wait vmi -n "${NS}" "${VM}" \
  --for=condition=Ready --timeout=600s

# Step 2: start sshd.socket via QEMU guest agent (Fedora 41+ OpenSSH packaging workaround)
# Fedora 41+ ships sshd.service as a compatibility shim that NEVER auto-starts at boot.
# Only sshd.socket listens on TCP 22, and it requires explicit activation.
# Without this, SSH polls time out on every Bluefin VM.
VIRT_POD=$(kubectl get pod -n "${NS}" -l "kubevirt.io/vm=${VM}" \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n "${NS}" "${VIRT_POD}" -c compute -- \
  virsh qemu-agent-command 1 \
  '{"execute":"guest-exec","arguments":{"path":"systemctl","arg":["start","sshd.socket"],"capture-output":false}}' \
  >&2 || echo "WARNING: guest-exec for sshd.socket failed, SSH poll will be the arbiter" >&2

# Step 3: wait for SSH key injection to complete
kubectl wait vmi -n "${NS}" "${VM}" \
  --for=condition=AccessCredentialsSynchronized --timeout=120s

# Step 4: get pod IP
POD_IP=$(kubectl get pod -n "${NS}" -l "kubevirt.io/vm=${VM}" \
  -o jsonpath='{.items[0].status.podIP}')

# Step 5: wait for SSH port to be open (300s is plenty once sshd.socket is started)
timeout 300 bash -c \
  "until bash -c 'echo >/dev/tcp/${POD_IP}/22' 2>/dev/null; do sleep 5; done"

# Emit IP to stdout (captured as output parameter)
echo "${POD_IP}"
```

**Common failure:** `outputs.result` contains debug text. Always send debug to `>&2`.

**RBAC requirement:** `kubectl exec` on `virt-launcher` pods requires `pods/exec` (verb: create)
on the `pods/exec` sub-resource in the VM namespace. If this is missing, the guest-agent exec
fails with `Error from server (Forbidden)` and SSH will time out. Add it to the kubevirt-manager
Role in every VM namespace (`bluefin-test`, `bluefin-lts-test`).

**Why not just `systemctl enable sshd.socket` in the image?** `systemctl enable` writes a symlink
into the OCI image's `/usr/lib/systemd/system/sockets.target.wants/`. The image does have this
symlink — but it does NOT appear in `multi-user.target.wants/`, so socket activation does not
fire until `sockets.target` is reached later in the boot ordering. The race is consistent: the
VM reports Ready before sockets.target fully activates. The explicit guest-exec start is the
reliable fix; it runs at a known point (after VMI Ready) with no race.

**Diagnosing sshd status via guest agent (for debugging):**
```bash
kubectl exec -n <ns> <virt-launcher-pod> -c compute -- \
  virsh qemu-agent-command 1 \
  '{"execute":"guest-exec","arguments":{"path":"systemctl","arg":["is-active","sshd.socket"],"capture-output":true}}'
# Then decode: base64 -d <<< <out-data value>
# "inactive" = not started, "active" = listening on TCP 22
```

### 6. Teardown — always via onExit, never skip

Every pipeline must include an `onExit` teardown that:
1. Deletes the KubeVirt VM object: `kubectl delete vm "${VM}" -n "${NS}"`
2. Deletes the reflinked disk file: `rm -f "${DISK_PATH}"`

Teardown deletes the VM and any PVC-backed disk through the Kubernetes API; it
must not rely on a host-local file or node placement.

Orphaned VMs (from force-deleted workflows) are cleaned by the `orphan-vm-cleanup`
CronWorkflow every 2 hours.

### 7. VM namespaces

| Variant | Namespace |
|---|---|
| `latest` | `bluefin-test` |
| `lts` | `bluefin-lts-test` |
| Flatcar | `flatcar-test` |
| Knuckle installer | `knuckle-test` |

Never create VMs in `argo` or `argocd` namespaces.

### 8. Checking for stuck VMs

```bash
just list-vms
# Expected output when idle: empty (no VMs)
```

If VMs are stuck `Terminating`:
```bash
# Delete the virt-launcher pod and let reconciliation finish
kubectl delete pod -n <namespace> -l kubevirt.io/vm=<vm-name> --force
```

### 9. Golden disk management

Bluefin VMs no longer use golden disk hostPath files. They use `containerDisk` (OCI image).
The disk build pipeline is `build-containerdisk` — see section 2 above.

For Flatcar, disk image is downloaded at workflow start, converted qcow2→raw, wrapped as containerDisk (OCI), and pushed to Zot. No ghost-local disk files.

**Flatcar download URL (2026):** `https://stable.release.flatcar-linux.net/amd64-usr/<version>/flatcar_production_qemu_image.img.bz2`
Old domain `stable.release.flatcar-container.net` is NXDOMAIN — do not use.
Images ship as `.img.bz2` (bzip2-compressed qcow2) — decompress with `bzip2 -d` before `qemu-img convert`.
Knuckle uses a per-workflow PVC. GnomeOS uses containerDisk. No VM type writes to ghost disk anymore.

`gts` and `lts-hwe` tags do NOT exist. Never use them.

### 10. Node inotify limits — required for KubeVirt

KubeVirt virt-handler, containerd, and podman together consume thousands of inotify
watches. When exhausted, VM boot fails silently (SSH never becomes ready) and container
errors appear. The `inotify-tuning` DaemonSet in `manifests/` raises limits on all nodes:

```
fs.inotify.max_user_watches=1048576
fs.inotify.max_user_instances=512
```

If you see VM boot timeouts that aren't explained by disk or network issues, check:
```bash
cat /proc/sys/fs/inotify/max_user_watches   # should be >= 1048576
```

The DaemonSet applies this on every node restart. Do not remove it.
