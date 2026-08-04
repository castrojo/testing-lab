---
name: cluster-storage
description: >
  Node storage quirks, 4TB SSD migration, and cache offload.
---

# Node Storage Maintenance

> [!CAUTION]
> The host-level diagnostics, filesystem changes, migration steps, and cache
> relocation procedures in this file are **private maintainer/operator
> procedures**, not normal public guidance. They can destroy data or strand
> Kubernetes storage. Never run them from a workstation or use workstation SSH
> to `ghost` or `exo-0`. Routine operations must use `just`, `argo`, `kubectl`,
> GitOps, or an approved API-driven recovery workflow. Retain this detail only
> for a maintainer who has explicitly approved the maintenance window and
> backup plan.

## Private maintainer procedure — host-level NVMe maintenance

## NVMe and PCIe Power Management Quirks on Strix Halo (e.g., exo-0)

On Strix Halo platforms, DRAM-less NVMe controllers (like the Innogrit IG5220 / RainierQX) can experience active I/O timeouts (`QID 32 timeout`) and uninterruptible sleep state (`D` state) due to aggressive PCIe ASPM and APST power state transitions.

`exo-0` has also shown the same timeout pattern on its T-FORCE TM8FFE004T NVMe during heavy rsync/migration load, so treat repeated `nvme1` timeout spam as a real storage-latency issue, not just a transient copy slowdown.

### 1. Diagnosis
Check for timeouts or blocked processes in kernel logs or list active file-locks on raw block devices:
```bash
# Check dmesg for NVMe errors
dmesg | grep -i nvme

# Find lock holders on raw disk
fuser -v /dev/nvme1n1
```

### 2. Remediation Workflow
Follow this precise order of operations to disable power management and reset the device at runtime without a host reboot:

1. **Set PCIe ASPM Policy to Performance**:
   ```bash
   echo performance > /sys/module/pcie_aspm/parameters/policy
   ```
2. **Reset the NVMe Controller**:
   ```bash
   echo 1 > /sys/class/nvme/nvme1/device/reset
   ```
3. **Disable Autonomous Power State Transition (APST)** on the drive:
   ```bash
   nvme set-feature -f 0x0c -V 0 /dev/nvme1
   ```
4. **Settle Udev Triggers**:
   ```bash
   udevadm settle
   ```

### 3. Permanent Bootloader Fix
For permanent stability across host reboots (especially on atomic host operating systems like Bluefin/Silverblue running on `ghost`), APST should be disabled in the kernel command-line. This completely prevents the drive from entering high-latency sleep states that trigger link dropouts under high parallel I/O.

Append the `nvme_core.default_ps_max_latency_us=0` parameter:
```bash
# On atomic hosts (rpm-ostree)
sudo rpm-ostree kargs --append-if-missing="nvme_core.default_ps_max_latency_us=0"
```

A reboot is required to activate the bootloader change. After reboot, verify the setting:
```bash
cat /sys/module/nvme_core/parameters/default_ps_max_latency_us
# Expected output: 0 (APST disabled)
```

### Operator-only: formatting and mounting (Btrfs to XFS migration)

The 4TB local data NVMe drives are mounted at `/var/mnt/ghost-data` on `ghost` and
`/var/mnt/exo0-data` on `exo-0` (node-specific names — do not reuse `ghost-data` as the
mount point name on other hosts, it refers to a specific disk on a specific machine, not
a cluster-wide convention). Both have been transitioned from Btrfs to XFS to optimize for
container builds and BuildStream cache workloads.

**Device path is not consistent across hosts by name alone — verify with `lsblk`/`blkid`
before touching any device.** On `ghost`, the 4TB data drive is `/dev/nvme0n1` and the
system disk is `/dev/nvme1n1`. On `exo-0`, the 4TB data drive is `/dev/nvme1n1` and the
system disk is `/dev/nvme0n1` — this has been a real source of error before the mount
unit was corrected. Never assume the device name; confirm the model/size with
`lsblk -o NAME,SIZE,MODEL` first.

XFS with `reflink=1` provides copy-on-write capability (e.g. `cp --reflink=auto`) identical to Btrfs, but without the high write metadata fragmentation and degradation under OverlayFS / loop-device pressure.

#### Optimal XFS Formatting:
Format the NVMe devices skipping hardware blocks pre-discards (`-K`) and explicitly enabling reflink/copy-on-write and CRC verification (`-m reflink=1,crc=1`):
```bash
mkfs.xfs -f -K -m reflink=1,crc=1 /dev/nvme1n1
```

#### Optimal XFS Mount Options:
For high-concurrency container building, use these options:
- `noatime,nodiratime`: Drastically reduces metadata write wear on the NVMe SSD.
- `logbufs=8,logbsize=256k`: Allocates 8 in-memory log buffers of 256KB to accelerate tiny file overlay metadata updates.
- `allocsize=64m`: Sets sequential disk preallocation block sizes to 64MB to optimize sequential virtual machine `.raw` disk block updates.

```ini
Options=defaults,noatime,nodiratime,logbufs=8,logbsize=256k,allocsize=64m
```

---

## Operator-only: 4TB SSD migration procedures (Btrfs to XFS)

### 1. Migrating `exo-0` (Ephemeral Cache Storage)
`exo-0`'s 4TB drive is `/dev/nvme1n1`, mounted at `/var/mnt/exo0-data`; it holds
non-root local-path PVC data. **`/dev/nvme0n1` on `exo-0` is the live system disk
— never target it.**

1. **Scale down any legacy artifact-server deployment** if one still exists; current BuildStream lanes use the shared Buildbarn frontend and workers rather than a single `bst-artifact-server` pod (removed from `manifests/` — see git history if reviving).
2. **Stop and unmount unit on `exo-0`**:
   ```bash
   ssh core@<worker-ip> "sudo systemctl stop 'var-mnt-exo0\x2ddata.mount'"
   ```
3. **Format to XFS** (only if not already XFS — check with `blkid` first, reformatting destroys data):
   ```bash
   ssh core@<worker-ip> "sudo mkfs.xfs -f -K -m reflink=1,crc=1 /dev/nvme1n1"
   ```
4. **Update systemd mount file**:
   Edit `/etc/systemd/system/var-mnt-exo0\x2ddata.mount` on `exo-0` to specify XFS:
   ```ini
   [Mount]
   What=/dev/nvme1n1
   Where=/var/mnt/exo0-data
   Type=xfs
   Options=defaults,noatime,nodiratime,logbufs=8,logbsize=256k,allocsize=64m
   ```
5. **Reload systemd, mount, and enable**:
   ```bash
   ssh core@<worker-ip> "sudo systemctl daemon-reload && sudo systemctl start 'var-mnt-exo0\x2ddata.mount' && sudo systemctl enable 'var-mnt-exo0\x2ddata.mount'"
   ```
6. **Recreate the non-root local-path base**:
   ```bash
   ssh core@<worker-ip> "sudo mkdir -p /var/mnt/exo0-data/ac.v2 /var/mnt/exo0-data/cas.v2 /var/mnt/exo0-data/raw.v2 /var/mnt/exo0-data/bst-cache /var/mnt/exo0-data/local-path && sudo chmod 777 /var/mnt/exo0-data/bst-cache && sudo chmod 700 /var/mnt/exo0-data/local-path"
   ```
7. **Configure the local-path provisioner through GitOps**:
   `manifests/local-path-config.yaml` must list `exo-0` with
   `/var/mnt/exo0-data/local-path`. It must not contain
   `DEFAULT_PATH_FOR_NON_LISTED_NODES`; omitting that entry makes provisioning
   fail closed on future nodes until their non-root data mount is explicitly
   configured.
8. **Re-enable shared Buildbarn workloads** after the filesystem migration: confirm the Buildbarn frontend/scheduler/storage/worker pods are healthy before resuming heavy BST traffic.

### 2. Migrating `ghost` (Stateful Control Plane Storage)
`ghost` holds persistent states like OCI cache layers in `zot-local` and persistent volume data in `local-path`. This data must be preserved.

1. **Verify destination space on `exo-0` XFS storage**:
   Make sure `exo-0` has sufficient disk space before starting the copy:
   ```bash
   ssh core@<worker-ip> "df -h /var/mnt/exo0-data"
   ```
2. **Stop dependent workloads**:
   Scale down all services writing to `/var/mnt/ghost-data` (Zot registries, persistent volume consumers):
   ```bash
   kubectl scale deployment registry -n local-registry --replicas=0
   kubectl scale deployment zot-cache -n local-registry --replicas=0
   ```
3. **Stop K3s service on `ghost`**:
   Stop the container orchestrator to release all active container storage references, open file descriptors, and mount locks on `/var/mnt/ghost-data`:
   ```bash
   ssh core@<lab-ip> "sudo systemctl stop k3s"
   ```
4. **Back up `/var/mnt/ghost-data` to `exo-0`**:
   Perform a root-elevated rsync using `sudo` and `--rsync-path="sudo rsync"` to preserve numeric UIDs, ACLs, and SELinux contexts over the 40G USB4 link:
   ```bash
   ssh core@<lab-ip> "sudo rsync -aHAXxv --numeric-ids --rsync-path=\"sudo rsync\" /var/mnt/ghost-data/ core@<worker-ip>:/var/mnt/exo0-data/ghost-backup-temp/"
   ```
5. **Unmount on `ghost`**:
   Since K3s is stopped, the unmount will now succeed cleanly without "device is busy" failures:
   ```bash
   ssh core@<lab-ip> "sudo umount /var/mnt/ghost-data"
   ```
6. **Format `ghost` NVMe to XFS**:
   ```bash
   ssh core@<lab-ip> "sudo mkfs.xfs -f -K -m reflink=1,crc=1 /dev/nvme0n1"
   ```
7. **Update `/etc/fstab` on `ghost`**:
   Get the new UUID: `blkid /dev/nvme0n1`.
   Replace the old Btrfs entry in `/etc/fstab` with the new UUID, optimized options, and type `xfs`:
   ```text
   UUID=<new-uuid> /var/mnt/ghost-data xfs defaults,noatime,nodiratime,logbufs=8,logbsize=256k,allocsize=64m 0 0
   ```
8. **Mount `/var/mnt/ghost-data` on `ghost`**:
   ```bash
   ssh core@<lab-ip> "sudo mount -a"
   ```
9. **Restore from backup**:
   Pull the backed-up directories back with precise attributes using root-elevated rsync:
   ```bash
   ssh core@<lab-ip> "sudo rsync -aHAXxv --numeric-ids --rsync-path=\"sudo rsync\" core@<worker-ip>:/var/mnt/exo0-data/ghost-backup-temp/ /var/mnt/ghost-data/"
   ```
10. **Resume services**:
    Restart the K3s engine and scale up your workloads:
    ```bash
    ssh core@<lab-ip> "sudo systemctl start k3s"
    kubectl scale deployment registry -n local-registry --replicas=1
    kubectl scale deployment zot-cache -n local-registry --replicas=1
    ```
11. **Clean up backup**:
    Once all pods are healthy and verified, clean up the backup folders on both hosts:
    ```bash
    ssh core@<worker-ip> "rm -rf /var/mnt/exo0-data/ghost-backup-temp"
    ```
    **This step is easy to skip and has been skipped before** — a stale
    `ghost-backup-temp/` directory was found still consuming ~286G on `exo-0`'s 4TB drive
    well after the migration it supported had completed. Verify with
    `ssh core@<worker-ip> "du -sh /var/mnt/exo0-data/ghost-backup-temp"` before deleting,
    but don't leave it indefinitely — it silently eats into the same 4TB drive that
    `bst-cache` and `local-path` PVCs need.

### Operator-only: offloading host user caches and AI datasets

This is a maintainer-approved migration record, not a recommendation to
relocate user data or uninstall software on a host. If a maintainer approves
the migration, heavy host-level user directories under `/var/home/jorge/` may
be relocated to `/var/mnt/ghost-data/` and replaced with symbolic links.

#### Host-Level Flatpak Removal:
All host-level Flatpaks (both system and user-level) must be completely uninstalled to prevent the root partition (under `/var/lib/flatpak` and `~/.local/share/flatpak`) from saturating. Run these commands to purge all Flatpaks from the host:

```bash
# Uninstall all user-level Flatpaks
flatpak uninstall --user --all -y

# Uninstall all system-level Flatpaks
flatpak uninstall --system --all -y

# Reclaim any dangling references or unused runtimes
flatpak uninstall --unused -y
```

Never install or run Flatpaks directly on the host; keep the host system thin and let container/VM workloads manage their own runtimes.

#### Target Paths on 4TB NVMe SSD:
All folders are stored under `/var/mnt/ghost-data/` with exact ownership of `jorge:jorge` (`1000:1000`) and linked back transparently:
- `/var/home/jorge/.local/share/ramalama` -> `/var/mnt/ghost-data/ramalama` (~278 GB)
- `/var/home/jorge/.cache/buildstream` -> `/var/mnt/ghost-data/user-bst-cache` (~244 GB)
- `/var/home/jorge/.local/share/containers` -> `/var/mnt/ghost-data/user-containers` (~78 GB)
- `/var/home/jorge/.lmstudio` -> `/var/mnt/ghost-data/lmstudio` (~30 GB)
- `/var/home/jorge/.cache/Homebrew` -> `/var/mnt/ghost-data/user-homebrew` (~18 GB)
- `/var/home/jorge/.cache/uv` -> `/var/mnt/ghost-data/user-uv` (~11 GB)

#### Relocation Procedure:
1. Create directories on the 4TB NVMe SSD and grant ownership to the host user:
   ```bash
   sudo mkdir -p /var/mnt/ghost-data/{ramalama,user-bst-cache,user-containers,lmstudio,user-homebrew,user-uv}
   sudo chown -R 1000:1000 /var/mnt/ghost-data/{ramalama,user-bst-cache,user-containers,lmstudio,user-homebrew,user-uv}
   ```
2. High-performance, attribute-preserving sync using rsync:
   ```bash
   rsync -aHAXx --numeric-ids /var/home/jorge/.local/share/ramalama/ /var/mnt/ghost-data/ramalama/
   rsync -aHAXx --numeric-ids /var/home/jorge/.cache/buildstream/ /var/mnt/ghost-data/user-bst-cache/
   rsync -aHAXx --numeric-ids /var/home/jorge/.local/share/containers/ /var/mnt/ghost-data/user-containers/
   rsync -aHAXx --numeric-ids /var/home/jorge/.lmstudio/ /var/mnt/ghost-data/lmstudio/
   rsync -aHAXx --numeric-ids /var/home/jorge/.cache/Homebrew/ /var/mnt/ghost-data/user-homebrew/
   rsync -aHAXx --numeric-ids /var/home/jorge/.cache/uv/ /var/mnt/ghost-data/user-uv/
   ```
3. Swap original directories with symbolic links:
   ```bash
   mv /var/home/jorge/.local/share/ramalama /var/home/jorge/.local/share/ramalama.bak
   ln -s /var/mnt/ghost-data/ramalama /var/home/jorge/.local/share/ramalama
   
   # Repeat for all other directories (buildstream, containers, lmstudio, Homebrew, uv)...
   ```
4. Verify symlinks are correct:
   ```bash
   ls -ld /var/home/jorge/.local/share/ramalama /var/home/jorge/.cache/buildstream /var/home/jorge/.local/share/containers /var/home/jorge/.lmstudio /var/home/jorge/.cache/Homebrew /var/home/jorge/.cache/uv
   ```
5. Safe deletion of backups once validated:
   ```bash
   rm -rf /var/home/jorge/.local/share/ramalama.bak /var/home/jorge/.cache/buildstream.bak /var/home/jorge/.local/share/containers.bak /var/home/jorge/.lmstudio.bak /var/home/jorge/.cache/Homebrew.bak /var/home/jorge/.cache/uv.bak
   ```
