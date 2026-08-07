# Bootstrap Guide — Replicating the Ghostlab

This guide walks through setting up the complete lab stack from a bare
metal node. Follow it in order. Each section is idempotent — you can re-run steps
safely.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| x86_64 bare metal | Minimum 16GB RAM, 256GB NVMe, btrfs root or separate btrfs volume for `/var/tmp` |
| Fedora / Bluefin host OS | Tested on Bluefin (bootc, atomic). Any systemd-based distro works. |
| `k3s` installed | See [k3s.io/docs](https://docs.k3s.io/quick-start) — single node or multi-node. **On image-based, atomic systems (Bluefin/Dakota):** always set `INSTALL_K3S_BIN_DIR=/var/usrlocal/bin` |
| ArgoCD installed | `kubectl apply -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml` in `argocd` namespace |
| Argo Workflows installed | See [argo-workflows install](https://argoproj.github.io/argo-workflows/installation/) |
| `kubectl`, `argo`, `argocd`, `just` CLIs | On workstation or admin pod |

> **CNCF stack:** k3s ([Sandbox](https://www.cncf.io/projects/k3s/)), KubeVirt ([Incubating](https://www.cncf.io/projects/kubevirt/)), Argo Workflows + Argo CD ([Graduated](https://www.cncf.io/projects/argo/))

---

## 1. Install KubeVirt (CNCF Incubating)

KubeVirt enables running VMs as Kubernetes workloads. This is the core of the
ephemeral VM testing model.

```bash
# Option A: run the bootstrap WorkflowTemplate (logs install progress)
argo submit --from workflowtemplate/install-kubevirt -n argo --wait --log

# Option B: manual install (same steps, run from workstation)
VERSION=$(curl -s https://storage.googleapis.com/kubevirt-prow/release/kubevirt/kubevirt/stable.txt)
kubectl apply -f https://github.com/kubevirt/kubevirt/releases/download/${VERSION}/kubevirt-operator.yaml
kubectl apply -f https://github.com/kubevirt/kubevirt/releases/download/${VERSION}/kubevirt-cr.yaml
kubectl -n kubevirt wait kv kubevirt --for condition=Available --timeout=300s
```

**Enable required feature gates** (HostDisk is required for the btrfs reflink VM flow):

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

---

## 2. Install Containerized Data Importer (CDI)

CDI is used for disk import workflows (optional if you only use the btrfs reflink path,
but required for some bootstrap templates).

```bash
# Option A: WorkflowTemplate
argo submit --from workflowtemplate/install-cdi -n argo --wait --log

# Option B: manual
VERSION=$(curl -sL https://api.github.com/repos/kubevirt/containerized-data-importer/releases/latest \
  | grep -o 'v[0-9]*\.[0-9]*\.[0-9]*' | head -1)
kubectl apply -f https://github.com/kubevirt/containerized-data-importer/releases/download/${VERSION}/cdi-operator.yaml
kubectl apply -f https://github.com/kubevirt/containerized-data-importer/releases/download/${VERSION}/cdi-cr.yaml
kubectl -n cdi wait cdi cdi --for condition=Available --timeout=300s
```

---

## 3. Install KubeVirt Manager (optional — web UI)

KubeVirt Manager provides a web UI for VM lifecycle management. Exposed at NodePort `:30180`.

```bash
argo submit --from workflowtemplate/install-kubevirt-manager -n argo --wait --log
```

---

## 4. Configure the Test Namespaces

```bash
kubectl create namespace bluefin-test      --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace bluefin-lts-test  --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace flatcar-test      --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace knuckle-test      --dry-run=client -o yaml | kubectl apply -f -
```

Or apply the manifest:
```bash
kubectl apply -f manifests/flatcar-test-namespace.yaml
```

---

## 5. Bootstrap ArgoCD Applications

This repo uses two ArgoCD Applications to keep the cluster in sync with git. Apply
them once; from then on, `git push main` is all you need.

```bash
# Sync WorkflowTemplates (argo/workflow-templates/ → argo namespace)
kubectl apply -f argocd/application.yaml -n argocd

# Sync infra manifests (manifests/ → cluster)
kubectl apply -f argocd/infra-application.yaml -n argocd
```

Or use the `just` wrapper:
```bash
just setup-argocd
```

Both applications use `automated: { prune: true, selfHeal: true }` — resources
removed from git are removed from the cluster, and manual changes are reverted.
This is the [recommended Argo CD GitOps model](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).

`lab-infra` also creates the `kubestellar-applications` app-of-apps,
which reconciles PostgreSQL, KubeStellar core, and KubeStellar Console in that
order. Do not apply the child Applications manually.

---

## 6. Create the SSH Key Secret

The test pipeline uses an ed25519 keypair to SSH into freshly-booted test VMs.
The public key is injected into the running VM by KubeVirt accessCredentials via
qemuGuestAgent at boot; the private key lives in a Kubernetes Secret.

```bash
just setup-ssh-secret
```

This creates `bluefin-test-ssh-key` in the `argo` namespace. Idempotent — skip
if already present.

---

## 7. Configure Ghost-Specific Settings (Strix Halo hardware)

Kernel arguments and SSH login policy are host-level maintenance, not
Argo WorkflowTemplate operations. Do not submit retired workflow-based
helpers for those changes. These are private maintainer procedures; do not use
workstation SSH to `ghost` or `exo-0`, and do not treat host commands as normal
public bootstrap steps.

---

## 9. Add Worker Nodes (optional)

This cluster uses an opt-in model: worker nodes join the cluster manually and can
leave at any time (useful for laptops and gaming machines).

**Full onboarding steps: `/docs/reference/agent-cheatsheet.md` section 14.**

Quick summary:
1. Have a maintainer provision the join token through the approved secure enrollment process; do not retrieve it with workstation SSH.
2. On the new node: `sudo mkdir -p /var/usrlocal/bin` then run the k3s install script with `INSTALL_K3S_BIN_DIR=/var/usrlocal/bin`
3. Disable auto-start: `sudo systemctl disable k3s-agent`
4. Install `~/Justfile` with `just k8s-on/off/status` commands
5. Label from workstation: `kubectl label node <name> node-role.kubernetes.io/worker=true`

**Flannel backend is `host-gw`** — requires all nodes on `<lab-subnet>/24` flat L2.

---

## 10. Verify the Setup

```bash
# ArgoCD applications are healthy and synced
just argocd-status

# All WorkflowTemplates are present
kubectl get workflowtemplate -n argo

# CronWorkflows are scheduled
kubectl get cronworkflow -n argo

# No VMs are running (clean state)
just list-vms
```

Run a smoke test end-to-end to verify the full pipeline:
```bash
just run-tests
```

This submits the container-only `bluefin-qa-pipeline`, which runs the selected
suite directly inside the published OCI image and cleans up its pods on exit.

---

## Bootstrap WorkflowTemplates Reference

These templates live in `argo/bootstrap/` and are **not** managed by ArgoCD.
Run them once during initial cluster setup.

| Template | `argo submit --from` | Purpose |
|---|---|---|
| `install-kubevirt` | `workflowtemplate/install-kubevirt` | Install KubeVirt (CNCF Incubating) |
| `install-cdi` | `workflowtemplate/install-cdi` | Install CDI for disk import |
| `install-kubevirt-manager` | `workflowtemplate/install-kubevirt-manager` | Web UI at :30180 |
| `setup-otel` | `workflowtemplate/setup-otel` | OTel observability stack |

> These templates must be applied to the cluster before they can be run:
> ```bash
> kubectl apply -f argo/bootstrap/ -n argo
> ```
> After initial setup they remain in the cluster as runbooks for re-execution.
>
> Reusable KubeStellar workflows (`register-wec` and
> `kubestellar-smoke-test`) live in `argo/workflow-templates/` and are
> reconciled by ArgoCD; do not apply them from this directory.

---

## Hardware Reference (Ghostlab)

The reference implementation runs on a single node:

| Attribute | Value |
|---|---|
| CPU | AMD Ryzen AI MAX+ 395 (Strix Halo) — 16c/32t |
| RAM | 64GB LPDDR5X |
| Storage | NVMe with btrfs (`/var/tmp` on btrfs for reflink support) |
| GPU | AMD Radeon 8060S (integrated, gfx1151/RDNA 3.5) + ROCm for LLM inference |
| OS | Bluefin (bootc atomic, Fedora-based) |
| Kernel args | `amdgpu.gttsize=49152 ttm.pages_limit=12582912` (48 GiB GTT, applied by `manifests/amdgpu-kargs.yaml`) |
| BIOS UMA carve-out | **minimum (512 MiB)** — raising it steals system RAM and *shrinks* GTT |

`amd_iommu=off` is deliberately **not** set. It measures ~5–12% faster for
inference but risks breaking KubeVirt VFIO passthrough on this cluster.

The btrfs reflink VM clone requires `/var/tmp/bluefin-golden` and
`/var/tmp/bluefin-test` to be on the same btrfs volume. Verify with:
```bash
stat --file-system --format=%T /var/tmp
# should output: btrfs
```

---

## CNCF References

- [CNCF Cloud Native Landscape](https://landscape.cncf.io)
- [KubeVirt — CNCF Incubating](https://www.cncf.io/projects/kubevirt/)
- [k3s — CNCF Sandbox](https://www.cncf.io/projects/k3s/)
- [Argo — CNCF Graduated](https://www.cncf.io/projects/argo/) (Workflows + CD)
- [Argo Workflows Best Practices](https://argoproj.github.io/argo-workflows/cost-optimisation/)
- [Argo CD GitOps Best Practices](https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/)
- [KubeVirt user guide](https://kubevirt.io/user-guide/)
- [bootc — image-based Linux](https://containers.github.io/bootc/)
