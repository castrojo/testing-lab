# Lab Operations Guide

> **Routine work? Load [`/docs/reference/agent-cheatsheet.md`](/docs/reference/agent-cheatsheet.md) first.** This guide is the expanded operations manual: more context, longer procedures, and explicit decision trees.

Pair with:
- [`/docs/reference/agent-cheatsheet.md`](/docs/reference/agent-cheatsheet.md) — canonical command reference
- [`../AGENTS.md`](/agents.md) — policy + architecture
- [`../RUNBOOK.md`](/docs/ops/RUNBOOK.md) — timeless architecture + failure modes
- [`../WORKFLOWS.md`](/docs/reference/WORKFLOWS.md) — WorkflowTemplate parameter contracts
- [`/docs/skills/test-authoring/dogtail-patterns.md`](/docs/skills/test-authoring/dogtail-patterns.md) — GUI test authoring


> [!WARNING]
> **Use the CLI-first hierarchy for cluster operations:** `just` for routine
> lifecycle actions, then `argo` or `kubectl` for direct inspection and control.
> MCP is optional and must not block an operation. The only acceptable SSH path
> in this repo is **in-cluster** access from workflow/probe pods into test VMs
> when the test harness or post-mortem artifact collection requires it.
>
> **Exception:** SSH to ghost is permitted exclusively to start or stop the `k3s` service — you cannot stop the API server via the API itself. See [§ Turning k8s on/off](#turning-k8s-onoff).

---

## 1. The 60-second mental model

```text
image-poller CronWorkflow
  └─ digest comparison against image-polling-digests ConfigMap
       ├─ unchanged ─► exit cleanly
       └─ changed   ─► run-container-tests fan-out
                         ├─ publish per-suite results back to the repo
                         └─ update digest state only after QA succeeds
```

For Bluefin and Dakota image-poll QA, **bootc OCI images are tested directly as containers**. Boot, disk, and install validation are retired from the image-poll path; keep KubeVirt workflows only for lanes that explicitly still need them (Flatcar, Knuckle, migration, and similar).

---

## 2. Picking the right path

| Goal | Preferred path |
|---|---|
| Validate a smoke test or step change | `just run-tests-tag testing` |
| Validate atomic OS contract checks | Use Argo MCP to submit `bluefin-qa-pipeline` with `suites=system` |
| Validate developer or software suites | Use Argo MCP to submit `bluefin-qa-pipeline` with `suites=developer` or `suites=software` |
| Validate a bootc OCI image change | `just run-tests-tag <testing\|lts-testing\|stable\|lts-stable>` or `just run-tests-matrix` |
| Pre-merge gate / promote a passing matrix run | `just run-tests-matrix` |
| Validate Flatcar | `just run-flatcar-smoke` |
| Submit Dakota distributed BST pipeline (default variant only) | `just run-bst-build [ref=testing]` |

Rule: if a `just` recipe exists, use it. Otherwise use `argo` or `kubectl`;
MCP is optional.

Every BST run requires `build-mode=re`, fresh USB4 `up` observations on both
`ghost` and `exo-0`, two Ready BuildBarn workers, and observable worker actions.
If any precondition or remote execution is unhealthy, fail, diagnose, and
repair it. Do not select local, cache-only, Ethernet-backed, or automatic
fallback. A successful local build/push or container E2E is diagnostic evidence
only and does not satisfy this gate. Do not increase jobs, workers, or semaphore
capacity while the full SDK input root or runner remains unhealthy.

For Dakota distributed-build failures, inspect runner logs for `rustc -vV`,
`Permission denied`, invalid `TMPDIR`, CAS materialization errors, BuildBarn
storage DNS failures, and disappeared workers. Confirm the live ConfigMaps and
pods match the checked-in manifests; GitOps/config drift can leave the cluster
running an obsolete virtual/FUSE or `setTmpdirEnvironmentVariable` configuration
even when the repository is fixed. Treat storage/DNS/worker failures as lab
blockers and recover them before judging a PR.

---

## 3. Repo-owner wrappers vs. agent paths

The `Justfile` intentionally keeps local `kubectl` / `argo` convenience wrappers for the repo owner.
Those wrappers are acceptable for Jorge on the workstation, but they are **not** the agent/autonomous path.

For agents and automated systems:
- Workflow reads / logs / control → Argo MCP
- Pod, VM, Secret, and node reads / mutations → Kubernetes MCP
- GitOps changes → edit tracked YAML, push to git, let ArgoCD reconcile
- No SSH to ghost, exo-1, or any node — except `sudo systemctl start|stop k3s` on ghost (see below)

---

## 4. Retrieving evidence

### 4.1 Workflow status and logs

Use MCP first:
- `argo-mcp-list_workflows`
- `argo-mcp-get_workflow`
- `argo-mcp-logs_workflow`
- `argo-mcp-list_workflow_templates`

Repo-owner wrapper when needed:
- `just logs`
- `just list-workflows`

### 4.2 Runner artifacts

The runner echoes `results.json`, `pytest-results.xml`, and `atspi_tree.txt` into pod stderr before exit.
Use `argo logs` or the Argo UI instead of shelling into pods.

### 4.4 Updating files without workstation `scp`

If you need to push helper files or test content into the cluster, do **not** `scp` them to ghost or exo-1 from a workstation. Use a ConfigMap plus a short-lived Job created through Kubernetes MCP (`kubernetes-mcp-resources_create_or_update`):

1. Create or update a ConfigMap containing the files.
2. Create a scheduler-admitted Job that mounts the ConfigMap and writes only to
   its PVC-backed workload volume.
3. Wait for the Job to complete, then clean it up if it was ad hoc.

This keeps file staging API-driven and works even when node SSH is unavailable.

---

## 5. Failure triage

Start with the exact workflow that failed.

### 5.1 `No GITHUB_TOKEN or missing results.json - skipping publication`

1. `kubectl get secret -n argo github-token`
   - **Expected:** secret exists.
   - **If missing:** restore the secret and rerun the workflow.
2. `just logs | grep -n "skipping publication\|results.json"`
   - **Expected:** the failing `run-container-tests` lane is visible in the log.
3. If `results.json` is missing, fix the failing suite or image dependency before rerunning.

### 5.2 `results.json not found` or summary reports `Execution failed`

1. `just logs | grep -n "results.json not found\|Execution failed"`
   - **Expected:** the failing suite appears before the summary line.
2. `argo-mcp-logs_workflow <workflow-name>`
   - **Expected:** enough detail to identify the broken container lane, suite, or dependency install.
3. Rerun the relevant container-only workflow after fixing the image or testsuite issue.

### 5.3 `TypeError` mentioning `requireResult`

1. `just logs | grep -n "requireResult"`
   - **Expected:** a traceback line identifying the failing step file.
2. Replace `findChild(..., requireResult=...)` with `findChildren(...)` or `findChild(..., retry=False)`.
3. Rerun the relevant workflow after fixing the stale test code.

### 5.4 All top-bar scenarios fail together

1. `just logs | grep -n "wait_for_shell.py"`
   - **Expected:** the runner copied and invoked `wait_for_shell.py`.
2. `just logs | grep -n "unsafe_mode"`
   - **Expected:** the session enabled `global.context.unsafe_mode = true`.
3. If either check is missing:
   - **Next:** fix the runner/template in git, push, and submit a new workflow.

### 5.5 `Application "gnome-shell" is running` fails

1. `just logs | grep -n 'Application "gnome-shell" is running'`
   - **Expected:** the failing step appears in the log.
2. Replace that scenario step with `* GNOME Shell is accessible via AT-SPI`.
3. Rerun the affected workflow.

### 5.6 Workflow stuck `Pending`

1. `just list-workflows`
2. `kubernetes-mcp-nodes_top`
3. `kubernetes-mcp-pods_list fieldSelector=status.phase=Pending`
4. `kubernetes-mcp-pods_top all_namespaces=true`
5. If orphaned `virt-launcher-*` capacity is the problem:
   - **Next:** use Kubernetes MCP to create a one-shot Job cloned from `orphan-vm-cleanup`.

### 5.7 `outputs.result` contains debug text

1. `just logs | grep -n 'outputs.result'`
   - **Expected:** the polluted result string appears in the workflow log.
2. Edit the offending `script:` template so debug goes to `>&2`.
3. Run `just lint`, push, and verify ArgoCD reconciliation.

### 5.8 VM stuck `Terminating`

1. Use `kubernetes-mcp-pods_list_in_namespace` to find the matching `virt-launcher-*` pod.
2. Delete that pod with `kubernetes-mcp-pods_delete`.
3. Re-check the VM with `kubernetes-mcp-resources_get`.

### 5.9 `run-gnome-tests` pod errors immediately

1. `argo-mcp-get_workflow <workflow-name>`
   - **Expected:** the failing template or pod name is visible.
2. `argo-mcp-logs_workflow <workflow-name>`
   - **Expected:** enough detail to identify the bad template field.
3. If `volumes:` is nested under `container:`:
   - **Next:** move it to template scope in git, push, and run `just lint`.

### 5.10 Unknown failure class

1. `just logs`
2. `argo logs -n argo <workflow-name>`
3. `argo-mcp-get_workflow <workflow-name>`

Expected outcome: after step 4 or 5 you should have a concrete failing template, step, or VM phase to route back into one of the branches above.

---

## 6. ArgoCD operations

### 6.1 What ArgoCD owns

| Application | Syncs |
|---|---|
| `lab` | `argo/workflow-templates/*.yaml` |
| `lab-infra` | `manifests/*.yaml` |

### 6.2 Decision tree — my template change did not take effect

1. `git log -1 origin/main -- argo/workflow-templates/<file>`
   - **Expected:** the output includes your commit.
2. `just argocd-status`
   - **Expected:** `lab` is synced to a revision that matches or post-dates your commit.
   - **If older:** `just argocd-sync`.
3. `just argocd-status`
   - **Expected:** `lab` is Healthy.
4. `argo-mcp-get_workflow_template <name>`
   - **Expected:** the new value is live.
5. Submit a **new** workflow.
   - **Expected:** the new run sees the new template snapshot.

Do **not** `kubectl apply` a rejected WorkflowTemplate.

---

## 7. Load triage

Use these in order:
1. `just list-workflows`
2. `kubernetes-mcp-nodes_top`
3. `kubernetes-mcp-resources_list apiVersion=kubevirt.io/v1 kind=VirtualMachineInstance`
4. `kubernetes-mcp-pods_list fieldSelector=status.phase=Pending`
5. `kubernetes-mcp-pods_top all_namespaces=true`

Answer three questions in order:
1. Are `run-container-tests` or `image-poller` pods saturating ghost CPU or memory?
2. Are `virt-launcher-*` pods from VM-backed lanes consuming capacity with no corresponding live workflow?
3. Are runner pods pending because CPU or memory is exhausted?

---

## 8. SSH key rotation

The key rotation flow is still valid because it manages the **in-cluster** test-access secret, not workstation SSH.
Use the exact command block in [`/docs/reference/agent-cheatsheet.md`](/docs/reference/agent-cheatsheet.md) §6.

After rotation:
1. Update `manifests/bluefin-test-ssh-pubkey.yaml` with the new base64-encoded public key and push to main.
2. Run a VM-backed validation lane such as `just run-migration-test testing` and `just run-flatcar-smoke`.
3. If fresh workflows still fail SSH, verify the `bluefin-test-ssh-pubkey` secret was updated and ArgoCD synced it.

---

## 9. PR queue mode

1. Run the minimum required lab loop (`just run-tests-tag testing`; use `just run-tests-matrix` for high-risk work).
2. Collect workflow names, behave summaries, and log excerpts via MCP.
3. Keep PR comments minimal: report what ran, pass/fail, and blockers only. Do not duplicate GitHub UI state.
4. Only then label / approve / queue.

---

## 10. Turning k8s on/off

The **only** legitimate reason to SSH from a workstation to the control plane is to start or stop the `k3s` service. The API server cannot shut itself down — SSH is required.

```bash
# Stop all of Kubernetes (API, etcd, all pods go down)
ssh core@<control-plane-ip> "sudo systemctl stop k3s"

# Start it back up
ssh core@<control-plane-ip> "sudo systemctl start k3s"

# Verify
ssh core@<control-plane-ip> "sudo systemctl is-active k3s"
```

Everything else — pod management, workflow control, ConfigMaps, scaling — goes through MCP. No other workstation SSH to ghost is permitted.
