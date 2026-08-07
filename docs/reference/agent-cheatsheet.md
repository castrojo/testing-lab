# Agent Cheatsheet — read this first, then stop

> Deterministic, recipe-only reference for running the lab cluster.
> Designed to be the **single file a weak-capability agent needs to load** for routine cluster operations.
>
> If your task is not in this file, escalate to:
> - [`/docs/ops/lab-operations.md`](/docs/ops/lab-operations.md) — long-form procedures
> - [`/docs/reference/WORKFLOWS.md`](/docs/reference/WORKFLOWS.md) — WorkflowTemplate parameter contracts
> - [`/docs/ops/RUNBOOK.md`](/docs/ops/RUNBOOK.md) — architecture + failure-mode index
> - [`projectbluefin/testsuite`](https://github.com/projectbluefin/testsuite) — writing GUI tests
> - [`/AGENTS.md`](/AGENTS.md) — hard policy and tenets

> [!NOTE]
> **CLI/API-first.** Tool hierarchy: `just` (lifecycle recipes) → `argo`/`kubectl`
> (cluster ops). Routine/public-agent SSH is limited to workflow/probe pods
> connecting to explicit test VMs. Retained host-maintenance SSH is
> operator-only through an approved private channel; never use workstation SSH
> to administer `ghost` or `exo-0`.
> MCP tools are optional — never block on them. One bash call beats a tool search + MCP roundtrip every time.

## 1. Command selector — what should I run?

| Situation | Run |
|---|---|
| Validate a smoke test or step change | `just run-tests-tag testing` |
| Validate atomic OS contract checks | `argo submit -n argo --from workflowtemplate/bluefin-qa-pipeline -p suites=system` |
| Validate developer or software suites | `argo submit -n argo --from workflowtemplate/bluefin-qa-pipeline -p suites=developer` |
| Pre-merge gate / promote a passing matrix run | `just run-tests-matrix` |
| Validate a single Bluefin tag end-to-end | `just run-tests-tag <testing\|lts-testing>` |
| Validate released (stable) image | `just run-tests-tag stable` or `just run-tests-tag lts-stable` |
| Validate a bootc OCI image change | `just run-tests-tag <testing\|lts-testing\|stable\|lts-stable>` or `just run-tests-matrix` |
| Validate the Flatcar lane | `just run-flatcar-smoke` |
| Run on-demand K8sGPT cluster triage | `just run-k8sgpt` |
| Check exo-0 kernel canary status (7.1 target) | `kubectl get node exo-0 -o jsonpath='{.status.nodeInfo.kernelVersion}{"\n"}'` |
| Submit Dakota BST build pipeline (default variant only) | `just run-bst-build [ref=testing]` |
| Run Dakota containerized smoke QA (no VM, works for composefs-oci) | `just run-dakota-container-qa [image-tag=testing] [variant=dakota]` |
| Trigger the Dakota PR batch workflow | `argo submit -n argo --from workflowtemplate/dakota-pr-batch-pipeline -p pr-numbers=<number> --wait` |
| Tail the most recent workflow's logs | `just logs` |
| List workflows / VMs | `just list-workflows` · `just list-vms` |
| ArgoCD status / force sync | `just argocd-status` · `just argocd-sync` |
| Lint Argo YAML | `just lint` |
| Refresh dashboard data contracts locally | `gh issue list --repo projectbluefin/lab --label bug --state open --limit 50 --json number,title,url,labels,createdAt > /tmp/bugs-raw.json && ISSUE_COUNT=$(gh issue list --repo projectbluefin/lab --state open --limit 200 --json number \| jq length) && PR_COUNT=$(gh pr list --repo projectbluefin/lab --state open --limit 200 --json number \| jq length) && MERGED_7D=$(gh pr list --repo projectbluefin/lab --state merged --limit 200 --json mergedAt \| jq "[.[] \| select(.mergedAt > \"$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)\")] \| length") && python3 scripts/refresh_factory_stats.py "$ISSUE_COUNT" "$PR_COUNT" "$MERGED_7D" && python3 scripts/generate_page_datasets.py --root . && npm run build` |
| Bootstrap repo-owner workstation access | §9 |

Rule: **if a `just` recipe exists, use it.** Otherwise use `argo`/`kubectl` directly; do not wait for MCP.

Every BST submission requires `build-mode=re` and fresh USB4 `up` observations
on both `ghost` and `exo-0`; the workflow rejects any other state. Local,
cache-backed, Ethernet-backed, automatic-fallback, and remote-cache-only paths
are prohibited. Confirm the generated BuildStream configuration, both Ready
BuildBarn workers, and live worker action activity before calling a run
distributed.

## AMD ROCm GPU readiness — quick checks

Both lab nodes are 64 GB Framework Desktop (Strix Halo) machines with an AMD
iGPU. The device plugin selects nodes via the Node Feature Discovery label
`feature.node.kubernetes.io/pci-0380_1002.present`, so no hand-applied label is
needed — a node with an AMD display controller is picked up automatically.
ArgoCD owns `manifests/`; do not `kubectl apply` these files.

```bash
# Which nodes advertise a GPU?
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.amd\\.com/gpu

kubectl rollout status daemonset/amdgpu-device-plugin -n kube-system --timeout=300s
```

Expected outcome: every node with an AMD iGPU advertises `1` or more
`amd.com/gpu` allocatable units, and an `amdgpu-device-plugin` pod is `Running`
on each.

### GPU-addressable memory

Two independent ceilings apply, and both have bitten this lab:

```bash
# Per-node GPU memory ceilings (VRAM is BIOS-set, GTT is kernel-set)
for n in ghost exo-0; do
  pod=$(kubectl -n kube-system get pod -l app=usb4-link-monitor \
    -o jsonpath="{.items[?(@.spec.nodeName=='$n')].metadata.name}")
  echo "== $n"
  kubectl -n kube-system exec "$pod" -- nsenter -t 1 -m -- sh -c \
    'cat /sys/class/drm/card1/device/mem_info_vram_total /sys/class/drm/card1/device/mem_info_gtt_total'
done
```

- **VRAM** (`mem_info_vram_total`) is the BIOS UMA setting and can only be
  changed in the Framework Desktop firmware. A node reporting 512 MiB is
  misconfigured; a correctly configured node reports the full 64 GiB. ROCm
  allocates device memory from VRAM, so 512 MiB cannot host a large model
  regardless of quantization.
- **GTT** (`mem_info_gtt_total`) is capped by `ttm.pages_limit`, which defaults
  to half of system RAM. `manifests/amdgpu-kargs.yaml` raises it via
  `rpm-ostree kargs`. Kargs take effect on next boot; check progress with:

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,KARGS:.metadata.annotations.lab\\.projectbluefin\\.io/amdgpu-kargs
```

`pending-reboot` means the kargs are staged but the node is still running the
old kernel command line.

## Local LLM deployment — quick checks

The lab also has a repo-managed vLLM deployment in `manifests/llm-d.yaml` for `unsloth/Llama-3.2-3B-Instruct`. It is enabled by default, pinned to `exo-0`, and requests one `amd.com/gpu` device so the ROCm device plugin exposes the GPU to vLLM. A preemptive priority class lets it take over the node when needed.

```bash
kubectl -n llm-d get deploy llm-d-modelserver
kubectl -n llm-d get pods -w
kubectl -n llm-d get svc llm-d-modelserver
kubectl -n llm-d logs -l app.kubernetes.io/name=llm-d-modelserver -c modelserver --tail=100
```

The endpoint is exposed on NodePort `30800` and can be queried at `http://<exo-0-ip>:30800/v1/models` after the pod reaches `Running`.

## Flatcar kernel lifecycle — quick checks

Use these for lifecycle-state inspection and manual gate runs:

```bash
kubectl get configmap flatcar-kernel-lifecycle-state -n argo -o yaml
argo cron list -n argo | grep flatcar-kernel-gate
argo submit -n argo --from workflowtemplate/flatcar-kernel-gate
```

## 2. Failure triage — symptom → exact next command

Run `just logs` first. Then match a row. **Bluefin and Dakota image-poll QA are now container-only** — rows mentioning VM, VMI, or SSH apply only to VM-backed lanes such as Flatcar, Knuckle, or other explicit KubeVirt workflows.

| Symptom in logs | Run next |
|---|---|
| `No GITHUB_TOKEN or missing results.json - skipping publication` | `kubectl get secret -n argo github-token` — secret must exist; then inspect `just logs` for the failing suite before rerunning. |
| `results.json not found` or summary reports `Execution failed` | `just logs | grep -n "results.json not found\|Execution failed"` → identify the failing `run-container-tests` lane, then rerun after fixing the image or suite issue. |
| Expected image-poll rerun never starts after a new publish | `kubectl get configmap image-polling-digests -n argo -o yaml` — compare the stored digest with the workflow log; stale state means the previous run already claimed that digest. |
| VMI `NotFound` 1 second after VM creation | Same as above — KubeVirt refused to start VM due to missing accessCredentials secret; VM status will be `Stopped` |
| `TypeError: ... requireResult` | Fix the step in the upstream `projectbluefin/testsuite` patterns (`findChildren(...)` / `retry=False`) |
| `Application "gnome-shell" is running` step fails | Replace it with `* GNOME Shell is accessible via AT-SPI` |
| All top-bar scenarios fail | Confirm `wait_for_shell.py` is present in the copied suite and that the runner re-asserts `unsafe_mode` |
| `outputs.result` is `Waiting...` or other debug text | Send debug output to `>&2`; keep stdout for the result only |
| VM stuck `Terminating` | `kubectl delete pod -n bluefin-test $(kubectl get pod -n bluefin-test -l kubevirt.io/vm=<name> -o name)` |
| `qemu-img: command not found` (Flatcar prep) | Use `quay.io/fedora/fedora:latest` for the Flatcar prep image |
| exo-0 not on expected 7.1 kernel | `kubectl get node exo-0 -o jsonpath='{.status.nodeInfo.kernelVersion}{"\n"}'` then verify Nebraska packages: `curl -s http://<control-plane-ip>:30802/api/v1/apps/e96281a6-d1af-4bde-9a0a-97b76e56dc57/packages \| jq '.[-5:]'` |
| Kernel poller keeps retriggering wrong versions | Check state: `kubectl get configmap flatcar-kernel-polling-state -n argo -o yaml` and verify CronWorkflow policy is `Forbid`: `kubectl get cronworkflow flatcar-kernel-poller -n argo -o jsonpath='{.spec.concurrencyPolicy}{"\n"}'` |
| `run-gnome-tests` pod errors immediately | Fix the WorkflowTemplate in git; `volumes:` must live at template scope, not under `container:` |
| Workflow stuck `Pending` | Run §3 |
| Workflow stuck on a `NotReady` node / pod never progresses | `kubectl get nodes`; if the worker is `NotReady`, `argo stop -n argo <workflow>` and submit a fresh run so the scheduler can place it on a healthy node (often `ghost`) |
| Template change did not take effect | Run §4 |
| Lab QA ran but no `testing-lab / <repo>` Check Run on the factory PR | `gh api repos/projectbluefin/<repo>/contents/.github/workflows/lab-check.yml` — a `404` means the repo is sender-enrolled (in the poller's `AUTO_REPOS`) but missing the receiver workflow, so the `lab-check` dispatch is silently dropped. See [`/docs/ops/RUNBOOK.md`](/docs/ops/RUNBOOK.md) failure modes. |

If no row matches:

```text
1. just logs
2. argo logs -n argo <workflow-name> --follow
3. argo get -n argo <workflow-name>
```

## 3. Capacity triage — cluster feels slow

```text
1. just list-workflows
2. kubectl top nodes
3. kubectl get vmi -A
4. kubectl get pods -A --field-selector=status.phase=Pending
5. kubectl top pods -A
```

| Symptom | Action |
|---|---|
| Workflows `Pending` | `kubectl top nodes` to identify the current CPU hog before submitting more work |
| Node has `DiskPressure` | Do not submit builds. Inspect PV node affinity and `kube-system/local-path-config`; every eligible node needs an explicit non-root data path and there must be no default root-disk fallback. |
| Many `virt-launcher-*` pods with no corresponding live workflow | `argo submit -n argo --from workflowtemplate/orphan-vm-cleanup` |

Per-template ceilings live in [`/AGENTS.md`](/AGENTS.md) under **Resource Limits**.

## 4. ArgoCD — my template change did not take effect

### kubectl handles ALL Argo/ArgoCD resources

**`kubectl get/apply/delete` works for any CRD, including:**

| Resource | apiVersion | kind |
|---|---|---|
| ArgoCD Application | `argoproj.io/v1alpha1` | `Application` |
| Argo Workflow | `argoproj.io/v1alpha1` | `Workflow` |
| Argo CronWorkflow | `argoproj.io/v1alpha1` | `CronWorkflow` |
| Argo WorkflowTemplate | `argoproj.io/v1alpha1` | `WorkflowTemplate` |

**Trigger an ArgoCD sync:**
```bash
KUBECONFIG=~/.kube/bluespeed.yaml kubectl -n argocd annotate application testing-lab \
  argocd.argoproj.io/refresh=normal --overwrite
# or via argocd CLI:
argocd app sync testing-lab
```

**If the local ArgoCD port-forward drops**, restart it and verify the health endpoint
before syncing or resubmitting a workflow:
```bash
kubectl -n argocd port-forward svc/argocd-server 18080:80
curl -sf http://127.0.0.1:18080/healthz
```

**Read ArgoCD Application state:**
```bash
KUBECONFIG=~/.kube/bluespeed.yaml kubectl get application testing-lab-infra -n argocd \
  -o jsonpath='{.status.sync.status} {.status.health.status}'
```
Key fields: `.status.operationState.phase`, `.status.sync.status`, `.status.operationState.message`, `.status.operationState.operation.sync.revision`

**Cancel a stuck operation** (PreSync hook looping):
```bash
KUBECONFIG=~/.kube/bluespeed.yaml kubectl patch application testing-lab -n argocd \
  --type=json -p='[{"op":"remove","path":"/operation"}]'
```

```text
1. git log -1 origin/main -- argo/workflow-templates/<file>
   -> expected: your commit is visible on origin/main.
   -> if not: push first.

2. just argocd-status
   -> expected: `testing-lab` is synced to a revision that matches or post-dates your commit.
   -> if older: just argocd-sync

3. just argocd-status
   -> expected: `testing-lab` is Healthy.
   -> if not Healthy: inspect the reported condition, fix the rejected field in git, push again, then repeat step 2.

4. argo template get -n argo <name>
   -> expected: the new field value is live.
   -> if still old: rerun `just argocd-sync`, wait for health, then re-check.

5. Was the workflow submitted before the reconcile finished?
   -> workflows snapshot the template at submit time.
   -> submit a NEW workflow.
```

Do **not** `kubectl apply` a rejected WorkflowTemplate.

## 5. CronWorkflow ops — pause / resume / backfill

```bash
# List all cron workflows
argo cron list -n argo

# Suspend during a debugging session:
argo cron suspend nightly-smoke -n argo
argo cron suspend nightly-smoke-lts -n argo

# Resume:
argo cron resume nightly-smoke -n argo
argo cron resume nightly-smoke-lts -n argo

# Backfill / run now:
argo submit -n argo --from cronworkflow/nightly-smoke
argo submit -n argo --from cronworkflow/orphan-vm-cleanup
```

| Name | Schedule (UTC) | Purpose |
|---|---|---|
| `nightly-smoke` | 02:00 | `bluefin-qa-pipeline` (`testing`) |
| `nightly-smoke-lts` | 02:30 | `bluefin-qa-pipeline` (`lts-testing`) |
| `orphan-vm-cleanup` | every 30 min | Clean orphan test VMs |

Any patch that must survive beyond a short debug session also needs a matching git change under `manifests/`.

---

## 6. Test-VM key rotation — deliberate, high-risk

This rotates the SSH key used **in-cluster** by workflow pods to reach test VMs. It is not SSH from a workstation — `ssh-keygen` runs locally only to generate key material, which is then stored in a k8s Secret.

```bash
# 1. Generate a new key locally (do not commit it):
ssh_key=$(mktemp)
ssh-keygen -t ed25519 -f "${ssh_key}" -N "" -C "bluefin-test-suite@ghost"

# 2. Replace the client secret (used by workflow pods to SSH into VMs):
kubectl create secret generic bluefin-test-ssh-key \
  --from-file=id_ed25519="${ssh_key}" \
  --from-file=id_ed25519.pub="${ssh_key}.pub" \
  -n argo --dry-run=client -o yaml | kubectl apply -f -

# 3. Replace the server-side public key (used by KubeVirt accessCredentials
#    to inject authorized_keys into VMs via QEMU guest agent):
PUB_KEY=$(cat "${ssh_key}.pub")
kubectl create secret generic bluefin-test-ssh-pubkey \
  --from-literal="key=${PUB_KEY}" \
  -n bluefin-test --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic bluefin-test-ssh-pubkey \
  --from-literal="key=${PUB_KEY}" \
  -n bluefin-lts-test --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

shred -u "${ssh_key}" "${ssh_key}.pub"

# 4. Update manifests/bluefin-test-ssh-pubkey.yaml with the new base64 key
#    so ArgoCD manages the secret going forward.

# 5. Confirm via VM-backed runs:
just run-migration-test testing
just run-flatcar-smoke

# 6. Verify the new fingerprint:
kubectl get secret bluefin-test-ssh-key -n argo \
  -o jsonpath='{.data.id_ed25519\.pub}' | base64 -d | ssh-keygen -lf -
```

SSH key rotation now has two parts:
- `bluefin-test-ssh-key` (argo ns): private+public key for the SSH client (workflow pods)
- `bluefin-test-ssh-pubkey` (VM ns): public key for KubeVirt accessCredentials injection

VM-backed lanes inject SSH keys at boot through KubeVirt qemuGuestAgent
accessCredentials rather than baking them into disk images.

---

## 6.5. Dakota PR review and repair loop

Use the [Dakota PR review skill](../skills/dakota-pr-review/SKILL.md) for the
repeatable pre-merge path. Do not rely on the older batch workflow as the merge
gate when Dakota GHA or merge queue is unhealthy.

For each open PR:

1. Confirm the current head SHA and mergeability.
2. Submit `dakota-build-pipeline` with that exact SHA and `build-mode=re`.
3. Run `dakota-container-qa-pipeline` against the built local registry image,
   then `dakota-qa-pipeline` for required BDD/GUI coverage.
4. If a scoped PR defect is found, fix it on the PR branch, push the new SHA,
   rebuild, and rerun E2E. Old evidence is invalid after a push.
5. Merge directly only after the fresh lab build and E2E pass; do not enter merge queue.

The older batch workflow remains useful for validation-only runs:

```bash
argo submit -n argo --from workflowtemplate/dakota-pr-batch-pipeline \
  -p pr-numbers=<number> \
  --wait
```

## 7. PR queue and ARC runners

- [PR queue / verification report notes](agent-cheatsheet-pr-queue.md)
- [ARC runners on ghost](agent-cheatsheet-arc-runners.md)

## 8. Safe cleanup — what you may delete

| Resource | Safe? |
|---|---|
| VM in `bluefin-test` / `bluefin-lts-test` / `flatcar-test`, with no live workflow | Yes — delete the single VM or run `orphan-vm-cleanup` |
| `just delete-vms` | Only for full teardown when you intentionally accept that all test VMs in those namespaces will be deleted |
| Workflows in `argo` | Yes — `just delete-workflows` |

Single-VM deletion:

```bash
kubectl delete vm -n bluefin-test <name>
```

---

## 9. Bootstrap — one-time, fresh cluster access

```bash
just setup-argocd
just argocd-sync
just run-tests-tag testing
# Optional for VM-backed lanes only:
just setup-ssh-secret
```

---

## 10. Self-check before claiming cluster healthy

```bash
1. just argocd-status
2. argo cron list -n argo
3. just list-vms
4. just list-workflows
5. just run-tests-tag testing
```

Expected steady state:
- both ArgoCD applications are Synced + Healthy
- all three CronWorkflows are present
- no idle test VMs remain after workflows finish
- the most recent container-only smoke run is green

---

## 12. Discover live cluster facts — do not trust stale docs

| Fact | Command |
|---|---|
| SSH key fingerprint | `kubectl get secret bluefin-test-ssh-key -n argo -o jsonpath='{.data.id_ed25519\.pub}' \| base64 -d \| ssh-keygen -lf -` |
| Live WorkflowTemplate body | `argo template get -n argo <name>` |
| CronWorkflow schedules | `argo cron list -n argo` |
| ArgoCD revision in cluster | `just argocd-status` |
| Pending pods | `kubectl get pods -A --field-selector=status.phase=Pending` |

---

## 13. llm-d local inference node

`llm-d` is managed by the `testing-lab-infra` ArgoCD Application (`manifests/llm-d.yaml`) and is **enabled by default** with one replica.
The vLLM container requests one `amd.com/gpu` device so the ROCm device plugin exposes the GPU into the pod.

**Model choice:** the deployment serves `unsloth/Llama-3.2-3B-Instruct` through vLLM on the OpenAI-compatible endpoint at `http://<ghost-ip>:30800/v1`.
The pod uses a dedicated preemptive `PriorityClass` so it can evict lower-priority work when needed.

**Check status (expected default):**
```bash
kubectl -n llm-d get deploy llm-d-modelserver -o jsonpath='{.spec.replicas}{"\n"}'   # expect 1
kubectl get pods -n llm-d                                                             # expect one Running pod
kubectl get node exo-0 -o jsonpath='{.status.allocatable.amd\.com/gpu}{"\n"}'       # expect >= 1
```

**Temporarily disable:**
```bash
kubectl -n llm-d scale deploy/llm-d-modelserver --replicas=0
```

**Re-enable (restore desired default):**
```bash
kubectl -n llm-d scale deploy/llm-d-modelserver --replicas=1
```

**If pod is stuck Pending:** Check two things:
1. AMD ROCm device plugin registered: `kubectl get pods -n kube-system | grep amdgpu` — look for `amdgpu-device-plugin`. After a k3s restart the plugin needs a pod delete/respawn to re-register with kubelet. Verify `amd.com/gpu` appears in `kubectl get node ghost -o jsonpath='{.status.allocatable}'`.
2. Memory fits: ghost has ~62.5Gi allocatable; the manifest requests 16Gi/24Gi and 1 GPU. Check for other large pods consuming RAM if you see `Insufficient memory`.

**If k3s is down** (kubectl returns "connection refused"):
k3s can stop after host sleep/resume. Do not recover it with workstation SSH.
Host-service recovery is a private maintainer procedure; escalate through the
approved operator channel. Once the API returns, delete the
`amdgpu-device-plugin` pod through Kubernetes so it re-registers with the
current kubelet socket:
```bash
kubectl delete pod -n kube-system -l app.kubernetes.io/name=amdgpu-device-plugin
```

Verify device-plugin health through the API:
```bash
kubectl get daemonset amdgpu-device-plugin -n kube-system
kubectl get node exo-0 -o jsonpath='{.status.allocatable.amd\.com/gpu}{"\n"}'
```

**If pod is CrashLoopBackOff:** Check the container logs first:
```bash
kubectl logs -n llm-d <pod-name> -c modelserver
```
The model will be cached in the pod's `emptyDir` at `/root/.cache/huggingface` until the pod is deleted.

**Key constraints:**
- The default deployment stays baseline-compliant and uses ordinary pod networking; if you revisit this later for tighter ROCm IPC tuning, you may need to re-test with host-network settings in a dedicated namespace
- The pod is intentionally not pinned to a specific node, so it can land on whichever node later exposes the GPU resource
- `unsloth/Llama-3.2-3B-Instruct` is intentionally small enough to be practical on the local lab node while still giving a useful chat experience
- For long prompts, raise `--max-model-len` or use a larger model later; for short local use, the current defaults are a good fit

---

## 14. Node onboarding — adding a worker to the cluster

> [!CAUTION]
> Node onboarding is a private maintainer/operator procedure, not a routine
> agent recipe. Never retrieve a join token with workstation SSH to `ghost` or
> `exo-0`; a maintainer must provision the token through an approved secure
> channel. The commands below run on the new node or through the Kubernetes API.

All nodes in this cluster run image-based, atomic operating systems (Bluefin, Dakota, Bazzite — ostree-based).
`/usr/local/bin` is a symlink to `/var/usrlocal/bin` (the writable overlay). The k3s install
script must be told to use this path or it fails on a fresh system.

### Provision the join token

The k3s join token is a host secret. A maintainer provisions it through the
approved private enrollment process; do not copy it from a cluster node with
workstation SSH.

### Bootstrap a new worker node (run ON the new node, with sudo)

```bash
# 1. Ensure writable bin directory exists (required on ostree image-based systems)
sudo mkdir -p /var/usrlocal/bin

# 2. Install k3s agent — joins the cluster immediately
curl -sfL https://get.k3s.io | \
  K3S_URL="https://<control-plane-ip>:6443" \
  K3S_TOKEN="<token from above>" \
  INSTALL_K3S_BIN_DIR="/var/usrlocal/bin" \
  sh -s -

# 3. Disable auto-start — nodes opt in to the cluster (see Justfile below)
sudo systemctl disable k3s-agent

# 4. Install sleep inhibitor (prevents suspend while k3s is active — critical for laptops)
sudo tee /etc/systemd/system/k3s-sleep-inhibit.service << 'EOF'
[Unit]
Description=Inhibit sleep while k3s agent is running
BindsTo=k3s-agent.service
After=k3s-agent.service

[Service]
Type=simple
ExecStart=/usr/bin/systemd-inhibit --what=sleep:handle-lid-switch --who=k3s --why="k3s running - use just k8s-off before travel" --mode=block sleep infinity
Restart=on-failure
RestartSec=5
EOF

sudo mkdir -p /etc/systemd/system/k3s-agent.service.d
sudo tee /etc/systemd/system/k3s-agent.service.d/sleep-inhibit.conf << 'EOF'
[Unit]
Wants=k3s-sleep-inhibit.service
EOF

sudo systemctl daemon-reload
```

### Install the cluster Justfile in the node's home directory

```bash
cat > ~/Justfile << 'EOF'
# Cluster controls — opt in/out of the ghost k3s cluster
# k8s-on  — join the cluster (laptop stays awake while connected)
# k8s-off — leave the cluster (safe to travel, close lid, suspend)

k8s-on:
    sudo systemctl enable --now k3s-agent
    @echo "k3s agent started — sleep/lid inhibited while connected"

k8s-off:
    sudo systemctl stop k3s-agent
    sudo systemctl disable k3s-agent
    @echo "k3s agent stopped — normal sleep restored"

k8s-status:
    @systemctl is-active k3s-agent 2>/dev/null && echo "k8s: ON (inhibiting sleep)" || echo "k8s: OFF (normal sleep)"
EOF
```

### Label the node and verify

```bash
# From workstation / ghost
KUBECONFIG=~/.kube/bluespeed.yaml kubectl label node <hostname> \
  node-role.kubernetes.io/worker=true --overwrite

KUBECONFIG=~/.kube/bluespeed.yaml kubectl get nodes -o wide
```

Expected: new node appears as `Ready  worker`.

### Passwordless sudo for agents

```bash
sudo bash -c 'echo -e "Defaults:jorge !requiretty\njorge ALL=(ALL) NOPASSWD: ALL" \
  > /etc/sudoers.d/zzz-jorge && chmod 440 /etc/sudoers.d/zzz-jorge'
```

### Node offboarding — removing a worker

```bash
# 1. Drain node
KUBECONFIG=~/.kube/bluespeed.yaml kubectl drain <hostname> --ignore-daemonsets --delete-emptydir-data

# 2. Delete from cluster
KUBECONFIG=~/.kube/bluespeed.yaml kubectl delete node <hostname>

# 3. Clean up node
sudo /var/usrlocal/bin/k3s-agent-uninstall.sh
```

### Key facts for atomic OS nodes

- **Binary path:** `/var/usrlocal/bin/k3s` (`INSTALL_K3S_BIN_DIR=/var/usrlocal/bin`)
- **Flannel backend:** `host-gw` (pure L2 routes, all nodes on `<lab-subnet>/24`)
- **Upgrades:** managed by system-upgrade-controller via `manifests/k3s-upgrade-plans.yaml`
- **Version skew:** agents must not be newer than the server (ghost)

### BIOS UMA carve-out — leave at minimum

Measured on exo-0 (2026-08-06). The Framework Desktop UMA setting is a hard
carve-out from system RAM, not a reallocation, and GTT is sized from what is
left over:

| BIOS UMA | `mem_info_vram_total` | `MemTotal` | `mem_info_gtt_total` |
|---|---|---|---|
| 512 MiB (correct) | 512 MiB | 62.1 GiB | 31.0 GiB |
| 48 GiB (tested, reverted) | 48.0 GiB | 15.4 GiB | 7.9 GiB |

Raising it cost 47 GiB of system RAM and cut GTT by 4×. Keep the carve-out at
the minimum and get GPU capacity from `ttm.pages_limit` instead.
