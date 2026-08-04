---
name: kubevirt-vms
description: >
  KubeVirt ephemeral VM lifecycle in the lab: containerDisk build,
  VM provisioning, SSH wait, teardown. Use when writing provision-vm templates,
  debugging VM boot failures, or working with KubeVirt manifests.
metadata:
  context7-sources:
    - /kubevirt/kubevirt
    - /kubevirt/user-guide
    - /kubevirt/containerized-data-importer
---

# KubeVirt VMs — lab Skill

## When to Use

- Editing `provision-containerdisk-vm.yaml`, `provision-flatcar-vm.yaml`, `knuckle-qa-pipeline.yaml`
- Debugging VM boot timeouts or SSH readiness failures
- Adding a new image variant
- Enabling a new KubeVirt feature gate
- Understanding why a VM is stuck `Terminating`

## When NOT to Use

- Argo Workflows YAML syntax issues → `argo-workflows.md`
- GNOME/behave test failures → `test-authoring.md`
- ArgoCD sync problems → `gitops-argocd.md`

## Core Process

KubeVirt guidance is split by topic:

- [VM lifecycle](vm-lifecycle.md) — disk placement, containerDisk, SSH injection, scheduling, teardown.
- [VM troubleshooting](vm-troubleshooting.md) — LTS boot, fsetxattr, Flatcar kernel builds, bootupd, UsrMerge.

## Console-driven VM control (imperative path)

The KubeStellar Console ServiceAccount can start/stop/restart VMs
imperatively via `patch virtualmachines` (ClusterRole
`kubestellar-console-lab-surfaces`, see `console-dashboard/SKILL.md`).
Verified live (2026-07-25): a probe VM was started and stopped via
`kubectl patch vm <name> --as=system:serviceaccount:kubestellar-console:kubestellar-console
--type=merge -p '{"spec":{"running":true}}'`; the VMI scheduled on exo-0
with no pinning.

- `spec.running` is deprecated (warning emitted); prefer
  `spec.runStrategy: Always|Halted` in new manifests. Both work today.
- Serial/graphical access: `virtctl console <vm>` / `virtctl vnc <vm>`
  from a workstation with cluster access — the Console GUI does not
  proxy VNC; virtctl is the documented fallback.
- VM *definitions* remain GitOps (manifests in git); only runtime
  start/stop/restart is imperative. Ephemeral test VMs created by
  workflows are exempt (owned by the workflow, cleaned by onExit and
  `orphan-vm-cleanup`).

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "SSH will come up on its own after VMI Ready — just poll longer." | Fedora 41+ `sshd.service` is a dead shim that never starts. No amount of polling helps. Start `sshd.socket` via guest-exec immediately after VMI Ready (section 5). |
| "I'll keep the VM up between runs to save time." | No persistent test VMs. The `orphan-vm-cleanup` CronWorkflow will delete it. |
| "The teardown step can be optional." | A missing `onExit` handler leaks VMs and disk clones on failure. Always required. |
| "containerDisk VMs must pin to ghost." | No VM type requires a ghost pin. All VMs use containerDisk (Bluefin, GnomeOS, Dakota, Flatcar) or PVC (Knuckle). Adding a node requires no YAML changes. |
| "Workflow pods need hostNetwork + nodeSelector: ghost to SSH into VMs." | False. Pod IPs route across nodes via flannel. SSH and kubectl exec work from any node. hostNetwork was a workaround for broken exo-1 flannel — not a KubeVirt requirement. |
| "The zot image from yesterday is still there." | Zot-writable loses its index.json on pod restart. Always check before running the pipeline. |
| "HostDisk feature gate is probably already on." | Verify with `kubectl get kubevirt kubevirt -n kubevirt -o jsonpath='{.spec.configuration}'`. Don't assume. |
| "inotify limits are a kernel concern, not a k8s concern." | KubeVirt virt-handler + containerd exhaust defaults at scale. The `inotify-tuning` DaemonSet is required. |
| "Writing to /mnt/btroot/var/ injects SSH keys into the live system." | `btrfs subvolume list` returns EMPTY for bootc disks — there are no named subvolumes. But disk injection is still unreliable: use KubeVirt accessCredentials instead. |
| "Baking SSH keys into the disk is reliable." | ostree resets etc/ files that exist in image's usr/etc/ at first boot. var/ writes may not survive qemu-img sparse conversion. Use accessCredentials. |
| "The home directory is writable after install -d creates .ssh." | `install -d` creates parent dirs as root:root. Must explicitly chown/chmod the home dir after, or pip install --user fails with EACCES. |

## Red Flags

- A VM provision template with `nodeSelector: kubernetes.io/hostname: ghost` — no VM type requires this anymore (no hostDisk VMs remain)
- An `onExit` handler that doesn't delete both the VM object AND the disk file
- Using `gts` or `lts-hwe` as image tags (they don't exist)
- VMs in namespaces other than the four test namespaces
- Hardcoded IPs in VM templates (use pod IP from `kubectl get pod -l kubevirt.io/vm=...`)
- **Any hostPath for VM disks** — use a containerDisk or PVC instead
- A `wait-for-vm` step that writes debug text to stdout (breaks output parameter capture)
- `registry.k8s.io/kubectl` used as image for a step that needs bash — it is distroless, use `cgr.dev/chainguard/kubectl:latest-dev`
- SSH wait using `nc -z` — `nc` is not available in distroless or minimal images; use `bash -c 'echo >/dev/tcp/${IP}/22'`
- VM boot timeout with no disk or network explanation — check `cat /proc/sys/fs/inotify/max_user_watches` (should be >= 1048576)
- Using BIB (`bib-build-and-push`) for bluefin/bluefin-lts builds — BIB's osbuild Fedora 38 runner has a PCRE2 mismatch with current bluefin images. Use `bootc install to-disk` instead.
- `fsetxattr(security.selinux): Invalid argument` during LTS containerDisk build — `unconfined_service_t` lacks `capability2 mac_admin`; neither `seLinuxOptions` nor `--security-opt` can override the k3s-assigned type. Use the LD_PRELOAD fsetxattr wrapper (section 12).
- `fsetxattr(security.selinux): Operation not supported` during LTS build — wrapper loaded but returning ENOTSUP; change wrapper to return 0 (noop) instead so ostree version mismatch doesn't matter.
- SSH `Permission denied (publickey)` after configure-disk — **do not debug disk injection further**; switch to KubeVirt accessCredentials with qemuGuestAgent (section 2b).
- Using disk injection for SSH keys when accessCredentials is available — disk injection is fragile; accessCredentials is the canonical KubeVirt pattern.
- Enabling `qemu-guest-agent.service` in a generated containerDisk without first
  ensuring the guest contains `qemu-guest-agent`. KubeVirt's
  `qemuGuestAgent` access-credential propagation requires the installed guest
  agent; make the shared builder install, enable, and verify its unit before
  creating the service symlink.
- `pip install --user` failing with EACCES inside VM — home directory owned by root; always chown after `install -d .ssh` (section 2c).
- LTS VM goes `Stopped` immediately after creation — `bluefin-test-ssh-pubkey` secret missing from `bluefin-lts-test` namespace. The manifest must create the secret in **both** `bluefin-test` and `bluefin-lts-test`. Check with `kubectl get secret -n bluefin-lts-test bluefin-test-ssh-pubkey`.
- VM goes `Stopped` with `FailedCreate` and `metadata.labels: must be no more than 63 characters` — VM name exceeds Kubernetes label-value limit. `bluefin-lts-testing-developer-<36-char-uuid>` = 67 chars, fails. `smoke` (5 chars) just passes; `developer` (9 chars) overflows. Fix: use `{{workflow.name}}-{{item}}` instead of `{{workflow.parameters.variant}}-{{item}}-{{workflow.uid}}` — workflow names are short and unique. Fixed in `bluefin-qa-pipeline` commit `7fca070`.
- Orphaned VMs from a prior workflow consuming ghost resources — run `just list-vms` before submitting a new matrix run; delete orphans with `kubernetes-mcp-resources_delete` if present. Four concurrent VMs on ghost can cause VMI Ready timeouts.
- **VM immediately fails with `No disk capacity`** — virt-launcher was evicted because its `ephemeral-storage` limit was exceeded during disk extraction. This usually means `disk.Capacity == nil` in KubeVirt because the containerDisk image is missing the `/containerDisk.json` metadata file. See section 2.
- **SSH always times out with 1800s poll even though VMI is Ready** — Fedora 41+ OpenSSH packaging: `sshd.service` is a dead shim, never starts. `sshd.socket` is enabled but requires explicit activation via guest-exec. Check with `systemctl is-active sshd.socket` via guest agent; if inactive, the `wait-for-vm-ready` template is missing the guest-exec start step (section 5). The 1800s timeout is the smoke alarm, not the root cause.
- **Flatcar containerdisk build: `curl: (6) Could not resolve host: stable.release.flatcar-container.net`** — that domain is NXDOMAIN. Use `stable.release.flatcar-linux.net`. See `provision-flatcar-vm.yaml`.
- **Flatcar containerdisk build: `bzip2: (stdin) is not a bzip2 file`** — old URL returned bare qcow2; new URL returns `.img.bz2`. Run `bzip2 -d` before `qemu-img convert`.
- **Flatcar DaemonSet in ErrImagePull with `wolfi-base:latest-dev`** — that tag does not exist. The correct image is the organization-owned FSDK container `ghcr.io/projectbluefin/lab-runner:latest` (or `cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795` as a fallback). Both already have full tooling including nsenter.
- **DaemonSet pod: `nsenter: can't open /proc/1/ns/mnt: Permission denied`** — pod was created before a DaemonSet rollout that added `seccompProfile: Unconfined`. Delete the pod to force respawn with the new spec.
- **`kubectl exec ... -c compute -- virsh qemu-agent-command` returns Forbidden** — `pods/exec` (verb: create) is missing from the kubevirt-manager Role in the VM namespace. Add it to `manifests/kubevirt-rbac.yaml` for every namespace (`bluefin-test`, `bluefin-lts-test`). RHEL10 `bootc install` only creates `/EFI/redhat/`; copy the shim to the fallback path in the build step. See section 13a.
- **LTS VM SSH never opens and CPU time grows but slowly (8-15 min boot)** — fstab `/boot` or `/boot/efi` entry missing `nofail`+`x-systemd.device-timeout=5s`. The field-aware sed in section 13b MUST cover both `defaults` and `umask=...` option strings. A simple `/defaults/ s/defaults/defaults,nofail/` won't match RHEL10's `/boot/efi` entry.
- **Field-aware fstab sed not patching `/boot/efi`** — the old sed pattern `/defaults/` doesn't match RHEL10 fstab where `/boot/efi` uses `umask=0077,shortname=winnt`. Use the column-4-aware sed from section 13b.

## Verification

Before merging any VM provisioning change:

- [ ] `wait-for-vm-ready` template starts `sshd.socket` via guest-exec (Fedora 41+ packaging); `AccessCredentialsSynchronized` wait added; SSH poll timeout ≤ 300s
- [ ] kubevirt-manager Role in EVERY VM namespace (`bluefin-test`, `bluefin-lts-test`) includes `pods/exec` (verb: create) — required for guest-exec
- [ ] No VM provision template has `nodeSelector: kubernetes.io/hostname: ghost` — no hostDisk VMs remain; all types float freely
- [ ] No `hostNetwork: true` or `nodeSelector: ghost` on SSH/kubectl workflow step pods — flannel handles routing
- [ ] `onExit` teardown deletes VM object; containerDisk teardown is VM-delete only; PVC-backed teardown also deletes the PVC
- [ ] Feature gates checked if adding a new VM capability
- [ ] `just list-vms` shows empty after workflow completion
- [ ] VM disks use containerDisk or PVC volumes; no VM workflow relies on hostPath storage
- [ ] No hardcoded IPs — pod IP derived at runtime via `kubectl get pod`
- [ ] Zot-writable index checked before running pipeline: `wc -c /var/mnt/ghost-data/zot-local/bluefin-containerdisk/index.json` > 100 bytes
- [ ] `bluefin-test-ssh-pubkey` secret exists in **both** `bluefin-test` and `bluefin-lts-test` namespaces
- [ ] Runtime user bootstrap sets home dir ownership (`chown 1001:1001 /var/home/bluefin-test`) before pip/pip3 installs
- [ ] **containerDisk builds**: Image contains `/containerDisk.json` with the correct `capacity` explicitly declared (prevents `No disk capacity` limits)
- [ ] **LTS containerDisk**: disk build includes `/EFI/BOOT/BOOTX64.EFI` fallback creation (section 13a)
- [ ] **LTS containerDisk**: fstab field-aware sed adds `nofail,x-systemd.device-timeout=5s` to ALL `/boot/*` entries (section 13b)
- [ ] **LTS containerDisk**: `bootc install to-disk` uses `--karg=systemd.device-timeout=5` (section 13b)
- [ ] If LTS VM Ready but SSH never opens: check CPU time diagnostic before assuming network or systemd issue

### Bluefin containerDisk SSH injection checklist (DO NOT USE DISK INJECTION)

**The correct approach is KubeVirt accessCredentials (section 2b), not disk injection.**
The checklist below is for diagnosing legacy disk injection failures only.

When debugging `Permission denied (publickey)`:
1. Confirm accessCredentials is in the VM spec: `kubectl get vm -n bluefin-test <name> -o yaml | grep -A15 accessCredentials`
2. Confirm the secret exists: `kubectl get secret -n bluefin-test bluefin-test-ssh-pubkey`
3. Check virt-controller logs for key injection: `kubectl logs -n kubevirt -l kubevirt.io=virt-controller | grep -i 'access\|credential\|authorized'`
4. Confirm qemu-guest-agent is running in VM (required for injection to work)

**Known disk injection failure modes (do not try to fix these — use accessCredentials):**
- `sshd_config.d/` files reset at boot: ostree restores files that exist in image's `usr/etc/`
- `var/` writes missing from running VM: qemu-img sparse conversion may drop newly-written btrfs blocks
- `authorized_keys` baked into disk missing from VM: same cause as above

### containerDisk.json capacity requirement
When building a custom containerDisk from `scratch` (using `buildah`), KubeVirt *requires* a `/containerDisk.json` file at the root of the image to declare the disk capacity:
`{"volumes":[{"image":"disk.qcow2", "capacity":"25Gi"}]}`
Without this file, `virt-controller` defaults the pod's `ephemeral-storage` limit to 50M. This results in the virt-launcher pod being evicted immediately upon extracting the disk image, causing a `No disk capacity` error. Always inject this JSON metadata during the build step.

### 14. Custom Flatcar kernel builds & Portage SLOT errors
When maintaining custom ebuilds in `flatcar/kernel-overlay/` for Flatcar kernel compilation inside the SDK container:
* **Required Metadata**: All custom `.ebuild` files MUST explicitly define both `LICENSE` and `SLOT` variables (e.g., `LICENSE="GPL-2"`, `SLOT="0"`).
* **Portage Masking**: If left undefined, Portage will reject and mask the ebuild packages during SDK compilation with `invalid: SLOT: invalid value: '', SLOT: undefined` errors.

### 15. Default root filesystem for bootc install
* **Missing Filesystem Error**: If a bootc container image does not define a default root filesystem in `/usr/lib/bootc/install/00-<osname>.toml`, running `bootc install to-disk` will fail with: `error: Installing to disk: No root filesystem specified`.
* **The Override**: To bypass this, parameterize the build template and explicitly pass `--filesystem <type>` (e.g., `--filesystem btrfs` or `--filesystem xfs`) during the `bootc install to-disk` command invocation.

### 16. bootupd findmnt bug on btrfs subvolumes (bubblewrap sandbox)
* **Symptom**: During `bootc install to-disk --via-loopback` on btrfs images (like `bluefin:testing` or `dakota`), the installation fails in the `bootupctl` backend step with: `installing bootloader: run bootupctl: run-chroot: running: bwrap: ...: Inspecting filesystem: No such file or directory (os error 2)`.
* **Root Cause**: In bootupd < 0.2.35, the `inspect_filesystem` step runs `findmnt` inside a bubblewrap sandbox. When `--write-uuid` is passed, it attempts to find the boot partition's UUID. On btrfs, `/` inside the sandbox is a subvolume, not a standard mountpoint. This makes `findmnt` fail with ENOENT (os error 2). Additionally, passing `--filesystem` to `bootupctl` invokes `inspect_filesystem_of_dir` on `/` inside the sandbox, which executes `findmnt --mountpoint` on the sandbox root directory and crashes with the exact same error.
* **The Fix**: 
  1. Configure `bootc` to instruct `bootupd` to skip boot partition UUID writing by writing `/etc/bootc/install/05-custom.toml` with `skip-boot-uuid = true` inside the build pod:
     ```toml
     [install.bootupd]
     skip-boot-uuid = true
     ```
  2. Overwrite the `bootupctl` binary inside the target image temporarily during the containerDisk build step with an interceptor wrapper script. This wrapper translates `--filesystem /` into `--device /dev/loopX` (resolving the loop device from `/sys/class/block/loop*p3`), avoiding any filesystem inspection completely while keeping execution 100% compatible. **Important**: The wrapper MUST invoke the renamed binary (`/usr/bin/bootupctl.orig`) using `exec -a bootupctl` to preserve `argv[0]`. Otherwise, the Rust CLI (via `clap` multi-call binary matching) defaults to backend mode and does not recognize the `backend` subcommand, causing the probe to fail with `unrecognized subcommand 'backend'`.
  3. Restore the original `/usr/bin/bootupctl` binary during the post-installation phase before unmounting loop partitions to keep the final golden disk clean and production-ready.

### 17. UsrMerge findmnt circular symlink loops (wrapper deduplication)
* **Symptom**: After deploying a wrapper script inside the image to override a binary (like `findmnt` or `bootupctl`), running the command fails with `Inspecting filesystem: No such file or directory (os error 2)` or results in infinite symlink recursion / command hang.
* **Root Cause**: Modern operating systems (including Fedora, Bluefin, and Dakota) have undergone **UsrMerge**, which symlinks and merges `/usr/sbin`, `/sbin`, and `/bin` into a single physical directory `/usr/bin`. If a loop or set of commands sequentially copies/symlinks a wrapper script to all of `/usr/bin/findmnt`, `/usr/sbin/findmnt`, `/bin/findmnt`, and `/sbin/findmnt` and overwrites them, it overwrites the newly renamed original binary with a circular symlink pointing back to the wrapper, or destroys the wrapper.
* **The Fix**:
  1. **Canonicalize Paths**: Always run `readlink -f` strictly within the chroot context (e.g., `chroot "${DEPLOY_DIR}" readlink -f /bin/findmnt`) to resolve candidate paths. Running `readlink -f` directly on the host using paths prefixed with `${DEPLOY_DIR}` will escape the target image rootfs jail (since `/bin` is an absolute symlink to `/usr/bin`) and resolve to host-level paths, risking host contamination or binary overwrites.
  2. **Deduplicate Target List**: Store only the unique canonical paths in a whitespace-delimited variable, filtering out any duplicates.
  3. **Location-Independent Original Invocation**: Write the wrapper using `"${0}.orig"` with `exec -a` to dynamically invoke the original renamed binary and explicitly preserve the expected `argv[0]`, preventing multicall (clap/busybox) binary execution crashes:
     ```bash
     exec -a "$(basename "$0")" "${0}.orig" "$@"
     ```
  4. **Recursive Restoration**: During the post-installation cleanup phase, avoid targeting hardcoded original paths. Check if the deployment directory exists, then run a dynamic file search using `find` to discover and restore all original files:
     ```bash
     if [ -n "${DEPLOY_DIR}" ] && [ -d "${DEPLOY_DIR}" ]; then
       find "${DEPLOY_DIR}" -name "*.orig" | while read -r orig_file; do
         base_dir=$(dirname "${orig_file}")
         orig_name=$(basename "${orig_file}" .orig)
         mv "${orig_file}" "${base_dir}/${orig_name}"
       done
     fi
     ```
