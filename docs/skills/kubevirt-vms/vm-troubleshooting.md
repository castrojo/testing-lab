---
name: kubevirt-troubleshooting
description: >
  OS-specific KubeVirt VM boot and build troubleshooting.
---

### 13. LTS (RHEL10) VM boot — EFI fallback and fstab stalls

**Bluefin-LTS uses RHEL10 (el10) base images. They differ from Fedora in two critical
ways for KubeVirt VM boot:**

#### a. OVMF cannot find the bootloader (no `/EFI/BOOT/BOOTX64.EFI`)

KubeVirt uses OVMF with ephemeral NVRAM (no stored boot entries). OVMF falls back to the
well-known path `/EFI/BOOT/BOOTX64.EFI`. 

| Image base | EFI path created by bootc | Fallback created? |
|---|---|---|
| Fedora (bluefin:testing) | `/EFI/fedora/` | **Yes** — `bootc install` creates fallback |
| RHEL10 (bluefin-lts:testing) | `/EFI/redhat/` | **No** — fallback is missing |

**Symptom:** VM shows KubeVirt condition `Ready=True` but SSH never opens. CPU time
stays at ~40-50s over 12+ minutes (VM is idle at OVMF boot manager screen, not
executing any OS code). Screenshot pixel analysis shows cyan (0,170,170) / gray
background = VGA text mode = OVMF UI.

**Diagnosing with CPU time:**
```bash
# CPU time increasing = VM executing code (good: systemd starting)
# CPU time flat/stalled relative to wall clock = VM idle (bad: stuck at OVMF/GRUB)
kubectl get vmi -n bluefin-lts-test <vm-name> -o jsonpath='{.status.cpuTime}'
# 43s CPU in 12+ min wall clock → VM completely idle → OVMF issue
```

**Fix:** Copy the shim EFI binary to the OVMF fallback path during disk build:
```bash
# In build-containerdisk configure-disk step:
EFI_MOUNT=/mnt/efi   # mounted EFI partition
SHIM=$(find "\${EFI_MOUNT}/EFI/redhat/" -name "shim*.efi" | head -1)
if [ -n "\${SHIM}" ]; then
    mkdir -p "\${EFI_MOUNT}/EFI/BOOT"
    cp "\${SHIM}" "\${EFI_MOUNT}/EFI/BOOT/BOOTX64.EFI"
fi
```

This is already implemented in `argo/workflow-templates/build-containerdisk.yaml`. If
you are authoring a new LTS VM build pipeline, include this step.

#### b. fstab UUID mounts stall boot without a device timeout

RHEL10 bootc `install to-disk` generates `/etc/fstab` entries for `/boot` and `/boot/efi`
that reference partition UUIDs without `nofail`. In a KubeVirt VM, the EFI partition is
exposed via `virtio` — systemd sees it as a new block device. If the device is slow to
appear (race with virtio enumeration), systemd waits indefinitely.

**Fedora** fstab `/boot/efi` options: `defaults` — systemd hits its default device timeout.
**RHEL10** fstab `/boot/efi` options: `umask=0077,shortname=winnt` — no `nofail`, and the
sed pattern `/defaults/` doesn't match, leaving the stall in place.

**Symptoms:** VM boot takes 8-15+ minutes; `systemd-analyze blame` shows
`dev-disk-by\x2duuid-*.device` taking 8+ minutes (the full systemd unit activation timeout).

**Fix — two-part:**
1. Add `--karg=systemd.device-timeout=5` to the `bootc install to-disk` command.
2. Add `nofail,x-systemd.device-timeout=5s` to ALL `/boot/*` fstab entries using a
   field-aware sed (column 4 = mount options, regardless of content):

```bash
# Field-aware sed: match lines containing /boot in column 2 (whitespace-delimited),
# append ,nofail,x-systemd.device-timeout=5s to column 4 if not already present.
# This works for BOTH 'defaults' AND 'umask=0077,shortname=winnt' option strings.
sed -i '/[[:space:]]\/boot/{ /nofail/! s/^\([^[:space:]]*[[:space:]]\+\)\([^[:space:]]*[[:space:]]\+\)\([^[:space:]]*[[:space:]]\+\)\([^[:space:]]*\)/\1\2\3\4,nofail,x-systemd.device-timeout=5s/ }' /mnt/etc/fstab
```

3. Add a `DefaultDeviceTimeoutSec=5` systemd drop-in as belt-and-suspenders:
```bash
mkdir -p /mnt/etc/systemd/system.conf.d
printf '[Manager]\nDefaultDeviceTimeoutSec=5\n' > \
  /mnt/etc/systemd/system.conf.d/99-vm-device-timeout.conf
```

**Why not just `nofail` without the timeout?** `nofail` only suppresses boot failure, not
the wait. Systemd still waits for the default device timeout (90s) before giving up.
`x-systemd.device-timeout=5s` limits the wait to 5 seconds.

The `--karg` adds the timeout as a kernel argument that applies even before systemd reads
fstab. Belt-and-suspenders: karg + fstab + drop-in.

**Important:** All fstab sed must run INSIDE the build container where fstab lives at
`/mnt/etc/fstab` (the mounted disk), NOT at `/etc/fstab` (the container's fstab).

### 12. Cross-policy LTS containerDisk builds — fsetxattr EINVAL

When building the LTS containerDisk on a bluefin (non-LTS) ghost host, `bootc install to-disk`
fails with:
```
fsetxattr(security.selinux): Invalid argument
```

**Root cause:** The LTS image contains SELinux file labels (types like `container_t`, etc.) not
present in the host's in-memory SELinux policy. The kernel's `selinux_inode_setxattr` returns
`EINVAL` for unknown types. This is only absorbed if `has_cap_mac_admin()` returns true, which
requires both CAP_MAC_ADMIN (satisfied by `--privileged`) AND an SELinux AVC check for
`capability2 { mac_admin }` in the process's SELinux type.

**Why `seLinuxOptions.type=spc_t` does not fix it:** k3s/containerd assigns
`unconfined_service_t` to ALL containers regardless of `seLinuxOptions`. Confirmed via
`/proc/self/attr/current` diagnostic.

**Why `--security-opt label=type:spc_t` does not fix it:** Same — k3s/containerd overrides
the SELinux type to `unconfined_service_t` for privileged containers.

**Actual fix — LD_PRELOAD wrapper:**
The `fsetxattr` interceptor converts `EINVAL` → `0` for `security.*` xattrs, silently
dropping unknown labels. The installed VM boots with `selinux=0` so missing xattrs are
irrelevant. Mount it into the inner container via a dedicated bind mount.

> **Build this into the runner image — do not compile it at runtime.** Installing a
> toolchain inside a running pod is banned (see
> [`image-policy.md`](../gitops-argocd/image-policy.md)); it is unreproducible and
> invisible to the registry allowlist. The `.so` below is a build-time artifact.

```dockerfile
# In the runner image's Containerfile — compiled once, shipped as an artifact.
RUN printf '%s\n' \
  '#define _GNU_SOURCE' '#include <dlfcn.h>' '#include <string.h>' \
  '#include <errno.h>' '#include <stddef.h>' \
  'typedef int (*fn_t)(int,const char*,const void*,size_t,int);' \
  'int fsetxattr(int fd,const char*n,const void*v,size_t s,int f){' \
  '  static fn_t real;' \
  '  if(!real)real=(fn_t)dlsym(RTLD_NEXT,"fsetxattr");' \
  '  int r=real(fd,n,v,s,f);' \
  '  if(r==-1&&errno==EINVAL&&n&&strncmp(n,"security.",9)==0){errno=0;return 0;}' \
  '  return r;}' > /tmp/fsetxattr_wrap.c && \
    gcc -shared -fPIC -o /usr/local/lib/fsetxattr_wrapper.so /tmp/fsetxattr_wrap.c -ldl && \
    rm /tmp/fsetxattr_wrap.c
```

```bash
# In the outer script: the wrapper already exists in the image.
mkdir -p /tmp/bluefin-cd-preload
cp /usr/local/lib/fsetxattr_wrapper.so /tmp/bluefin-cd-preload/
chcon -t lib_t /tmp/bluefin-cd-preload/fsetxattr_wrapper.so 2>/dev/null || true

# In the podman run command:
podman run --rm --privileged ... \
  -e LD_PRELOAD=/preload/fsetxattr_wrapper.so \
  -v /tmp/bluefin-cd-preload:/preload \
  ... ${IMAGE} bash -c "bootc install to-disk ..."
```

**Important notes:**
- Compile to `/tmp/bluefin-cd-preload/` (outer container tmpfs), NOT to the staging hostPath.
  Files on the hostPath (`/mnt/staging/`) get a SELinux file label (`svirt_sandbox_file_t`)
  that blocks ld.so in the inner container.
- Use `chcon -t lib_t` to ensure the .so has a loadable label.
- The `ld.so: cannot be preloaded` error at container startup is a red herring — it fires
  before mounts are set up for the bash entrypoint, but the wrapper IS loaded correctly when
  `bootc` exec's later.
- `ENOTSUP` (not `EINVAL`) means the wrapper loaded but the LTS ostree version doesn't skip
  ENOTSUP in all paths. Solution: return `0` (noop success) instead of `ENOTSUP`.

See `argo/workflow-templates/build-containerdisk.yaml` for the canonical implementation.



**Do not use `bootc-image-builder` (BIB) for bluefin/bluefin-lts golden disk builds.**

BIB invokes osbuild internally, which spawns a Fedora 38 runner chroot to execute
build stages (`org.osbuild.selinux`). That runner's `setfiles` is linked against
PCRE2 10.44, but bluefin images ship SELinux `.bin` policy files compiled for PCRE2 10.46+.
This mismatch breaks every time the Fedora base advances. BIB has no way to override
the internal runner version.

**The correct approach:** use the `build-containerdisk` WorkflowTemplate. It runs `bootc install to-disk`
inside the container image itself (the image is its own installer), then wraps the output as an OCI
containerDisk pushed to the local Zot registry. No golden disk file, no BIB, no osbuild.

```yaml
# Reference the build-containerdisk WorkflowTemplate directly:
templateRef:
  name: build-containerdisk
  template: build-containerdisk
arguments:
  parameters:
  - name: image-tag
    value: lts-testing
  - name: image
    value: ghcr.io/projectbluefin/bluefin-lts:testing
```

Staging disk is written to `/var/mnt/ghost-data/bluefin-cd-build/<tag>/disk.raw` during the build
and removed after the OCI image is pushed. See `argo/workflow-templates/build-containerdisk.yaml`
for the canonical implementation.

If you see the error:
```
setfiles: Regex version mismatch, expected: 10.46 actual: 10.44
```
That is the osbuild Fedora 38 runner PCRE2 mismatch. Switch to `bootc install to-disk`.

