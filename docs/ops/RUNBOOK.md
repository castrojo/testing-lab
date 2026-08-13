# Bluefin QA Pipeline — Runbook

> Timeless architecture and failure-mode reference. For commands see [docs/reference/agent-cheatsheet.md](../reference/agent-cheatsheet.md). For long-form operator procedures see [docs/ops/lab-operations.md](lab-operations.md).

## Architecture summary

```
Git push / image digest change / manual submit
        │
        ▼
Argo Workflow (argo namespace)
        │
        ├─ bluefin-qa-pipeline  ─► run-container-tests inside the published OCI image
        ├─ dakota-qa-pipeline   ─► run-container-tests inside the published OCI image
        └─ explicit VM lanes    ─► provision, run, and tear down ephemeral KubeVirt VMs
```

Two steady-state execution paths exist:

| Path | Purpose | Persistent state |
|---|---|---|
| Container-only Bluefin/Dakota path | Image and PR QA | No persistent VM or host-disk state |
| Explicit VM-backed lanes | Flatcar smoke and Knuckle installer QA | Ephemeral KubeVirt resources; no shared Bluefin golden disk |

### Container-only QA contract

`bluefin-qa-pipeline` and `dakota-qa-pipeline` both fan out
`run-container-tests` inside the published OCI image. The runner clones
`projectbluefin/testsuite`, starts the nested systemd/Wayland session, and
attempts to publish `results.json` when `GITHUB_TOKEN` is available. A
publication warning does not change the suite exit status.

The Bluefin/LTS testing and Dakota digest pollers remain staggered at minutes
`:00`, `:02`, and `:08` of each ten-minute interval for freshness tracking, but
their QA trigger is disabled. Daily testing-lane coverage comes from
`nightly-smoke` at 02:00 UTC, `nightly-smoke-lts` at 02:30 UTC, and
`nightly-dakota` at 03:00 UTC.

## Cluster topology

| Host | Role | IP | Notes |
|---|---|---|---|
| ghost | k3s control-plane + KubeVirt compute | `<ghost-ip>` | Runs VM workloads and Argo control-plane services |
| exo-0 | k3s worker | `<exo-0-ip>` | Build and workflow pods |
| Argo UI | external entrypoint | `http://<ghost-ip>:32746` | Host-local service also exposed on port 2746 |
| Loki | log aggregation | `http://<ghost-ip>:30100` | Captures workflow pod logs |
| ArgoCD | GitOps controller | `https://<ghost-ip>` | Reconciles this repo into the cluster |

Container QA runs on the control-plane graphical seat on ghost. Flatcar VMs float
across KubeVirt-capable nodes. Knuckle VMs co-schedule on the node holding their
local-path PVC. Other workflow pods may land on exo-0 according to template
constraints.

## GitOps ownership

| Area | Source of truth | Reconciler |
|---|---|---|
| WorkflowTemplates | `argo/workflow-templates/*.yaml` | ArgoCD application `lab` |
| Cluster infra and CronWorkflows | `manifests/*.yaml` | ArgoCD application `lab-infra` |
| Operator entrypoints | `Justfile` | Local operator / MCP tooling |

The repo is intentionally GitOps-first: cluster state should converge from git, not from manual template applies or node SSH.

## Operator access model

- Use Kubernetes MCP and Argo MCP for workstation-side cluster reads and mutations.
- Prefer the `just` entrypoints when they exist; they are the human-facing wrappers around the same API-driven workflow.
- Do not SSH from a workstation into `ghost` or `exo-0` for inspection, recovery, or file transfer.
- In-workflow SSH into explicit test VMs and probe-pod-to-guest SSH remain valid because
  they originate inside the cluster and are part of the test harness, not node administration.
- Host storage migration, node bootstrap, and host-service recovery are private
  maintainer procedures; they are not normal public workstation guidance.

## Image, disk, and VM model

| Object | Backing location | Used by | Notes |
|---|---|---|---|
| Published OCI image | Registry image and digest | Bluefin/Dakota container-only QA | `bluefin-qa-pipeline` and `dakota-qa-pipeline` run `run-container-tests`; no VM or disk is created |
| Flatcar test VM | Ephemeral KubeVirt VM with generated Flatcar containerDisk | `flatcar-smoke-test` | Provisioned from the current Flatcar image and torn down after the run; no golden disk or hostDisk |
| Knuckle test VM | Ephemeral KubeVirt VM with per-run local-path PVC and ISO containerDisk | `knuckle-qa-pipeline` | Teardown removes the VM, root-disk PVC, and per-run ISO containerDisk |

The `bluefin-test-ssh-key` Kubernetes secret in namespace `argo` is used only for
in-cluster access to explicit VM-backed test guests. Container-only Bluefin/Dakota
QA does not require SSH, golden disks, or hostDisk state.

## Test execution stack

| Component | Responsibility |
|---|---|
| `run-container-tests` | Run Bluefin/Dakota GUI and contract suites inside the target OCI image |
| `git-sync` initContainer | Clone the requested repo ref into the runner pod |
| `run-gnome-tests` | Copy suites to the VM and orchestrate execution |
| `qecore-headless` | Start the Wayland GNOME session inside the VM |
| `dogtail` | Traverse and interact with the AT-SPI tree |
| `gnome-ponytail-daemon` | Translate AT-SPI coordinates into Wayland input |
| `Shell.Eval` | Handle GNOME Shell 50 top-bar interactions that AT-SPI cannot drive reliably |
| Loki | Preserve logs and emitted test artifacts after pod cleanup |

## GNOME Shell 50 constraints

- Clock, quick-settings, and calendar interactions are not reliably actionable through AT-SPI alone.
- `global.context.unsafe_mode = true` must be enabled before top-bar interaction.
- `findChild(..., requireResult=...)` is not a supported dogtail pattern in this repo's stack.
- `findChildren(...)` and `findChild(..., retry=False)` are the canonical presence-check APIs.

## Service-catalog pipeline

A second execution path validates homelab service workloads directly in
Kubernetes, without VMs or GNOME infrastructure:

```
Argo Workflow (argo namespace)
        │
        ├─ create-namespace       ─► ephemeral namespace svc-<lane>-<uid>
        ├─ deploy-workload        ─► clone repo, kubectl apply lane manifests
        ├─ run-service-tests      ─► pytest against live k8s workload
        └─ cleanup (onExit)       ─► delete namespace
```

| Property | Value |
|---|---|
| Entry point | `just run-service-catalog-smoke` or `argo submit --from workflowtemplate/service-catalog-pipeline` |
| Parameters | `lane` (default: media), `image-tag` (default: latest), `branch` (default: main) |
| Wall-clock | ~3–5 min |
| Evidence | pytest JUnit XML + per-check artifacts in pod logs (Loki) |

### Adding a new lane

1. Create `tests/service_catalog/<lane>/manifests.yaml` with the workload's
   Kubernetes manifests (Deployment, Service, PVC, Secrets, ConfigMaps).
2. Create `tests/service_catalog/<lane>/test_<lane>.py` importing helpers
   from `tests/service_catalog/shared/` (deploy, persistence, reachability,
   redeploy, teardown).
3. Run: `just run-service-catalog-smoke lane=<lane>`
4. The pipeline creates a namespace, applies manifests, runs tests, cleans up.

### Inspecting results

- `argo logs @latest` — test output including summary and artifact list.
- Loki query: `{namespace="argo"} |= "svc-catalog"` — all service-catalog
  workflow logs.
- JUnit XML is emitted to the pod stdout and captured by Argo's log
  archival. No separate artifact upload path is needed.

## Common failure modes

| Symptom | Root cause | Durable fix |
|---|---|---|
| `Permission denied (publickey)` during SSH wait | Ephemeral VM cloud-init or installer key provisioning did not install the expected authorized key | Verify the `ssh-pubkey`/`ssh-key-secret` inputs and cloud-init or installer completion. For the `bluefin-migration-test`/`provision-containerdisk-vm` path, confirm `bluefin-test-ssh-pubkey` is wired through `accessCredentials.sshPublicKey` with `qemuGuestAgent`, then wait for VMI `AccessCredentialsSynchronized=True` before retrying SSH; inspect VMI and runner logs |
| Workflow hangs before GUI steps start | VM boot or SSH readiness never completed | Inspect VMI readiness and runner logs, then re-run the appropriate recovery path |
| `TypeError` involving `requireResult` | Stale dogtail step pattern | Replace with `findChildren(...)` or `findChild(..., retry=False)` |
| Clock / quick-settings scenarios miss their targets | GNOME Shell AT-SPI geometry gap | Drive the interaction via `Shell.Eval` |
| `outputs.result` contains debug text | Script template wrote extra stdout | Send debug output to stderr and reserve stdout for the actual result |
| VM stuck `Terminating` | KubeVirt controller race with launcher cleanup | Delete the `virt-launcher-*` pod and let reconciliation finish |
| `run-gnome-tests` pod fails at startup | Workflow template structure error, often misplaced `volumes:` | Fix the template in git and let ArgoCD reconcile it |
| WorkflowTemplate change appears ignored | Workflow was submitted before the new template was reconciled | Verify ArgoCD revision, wait or sync, then submit a new workflow |
| Container QA exits 1 before `useradd` | NSS-only `video`, `render`, or `input` groups can make `groupadd` succeed without adding `/etc/group` entries | Re-check `/etc/group` after `groupadd`; materialize the NSS entry or a fallback GID before calling `useradd -G`, then reconcile `run-container-tests` |
| Container QA exits 1 at `Failed to create required local group render` (video passes, render fails) | The nested provisioning script ran under `podman exec ... bash -c '...'`. The first apostrophe inside the block (from `printf '%s\n'`) closed the outer quote, so bash actually received `printf %sn` — no trailing newline. `video` was appended without a newline and `render` was spliced onto the same line, so `grep "^render:"` failed | Feed nested scripts over stdin (`podman exec -i ... bash -s <<'MARKER'`) instead of `bash -c '...'`, and guard appends to `/etc/group` with a trailing-newline check. Never reintroduce `bash -c '...'` for multi-line nested scripts |
| Container QA fails right after "Installing test-only Python dependencies" with a bare `exit status 1` | `pip install --quiet` swallowed the real error (PEP 668 externally-managed interpreter, missing pip, or a transient index failure) | The install now retries 3×, adds `--break-system-packages` when supported, and dumps the full pip log plus `python3`/`pip` versions on failure. Read the surfaced log rather than re-guessing |
| Container QA fails in `wait_for_shell.py` with `Shell.Eval not ready: ... Could not connect: No such file or directory` (error class `bus-unavailable`) until the readiness budget expires | **RESOLVED (closes the #611 open item).** ghost has one physical GPU and mutter's native backend takes *exclusive* DRM master on `/dev/dri/card*`. `ghost-container-qa` allows 6 concurrent lanes, so only the first nested GNOME session works; every other lane logs `Failed to open gpu '/dev/dri/card1': GDBus.Error:System.Error.EBUSY: Device or resource busy`, stalls ~50s in `Failed to make thread 'KMS thread' high priority scheduled`, and GDM loops `GdmDisplay: Session never registered, failing` forever. When `qecore-headless` then stops GDM and SIGTERMs the session, logind destroys `/run/user/1000` with the session bus inside it, and no replacement session ever registers to recreate it — hence a permanently absent socket, not a client-side race. GDM itself is healthy and `/etc/gdm/custom.conf` keeps `AutomaticLogin=bluefin-test` across the restart; it churns greeter sessions because every *display* dies, ending at `GdmLocalDisplayFactory: maximum number of display failures reached. Giving up.` An isolated A/B on ghost proved the headless drop-in is necessary and sufficient, and that `enable-linger` alone is not: a linger-only lane keeps the bus and the uid-1000 manager alive (`Linger=yes`) yet still fails, with the error class merely shifting from `bus-unavailable=137` to `service-unknown=151` | `run-container-tests` now (a) drops `/etc/systemd/user/org.gnome.Shell@.service.d/10-headless.conf` forcing `gnome-shell --headless --virtual-monitor 1920x1080` for the greeter and the autologin session, so no lane claims DRM master, and (b) as hardening runs `loginctl enable-linger bluefin-test` before starting GDM so `user@1000.service` and `/run/user/1000/bus` survive the `qecore-headless` GDM restart. **Operating rule: nothing in a nested QA target may take DRM master — the host GPU is exclusive and is not a per-lane resource.** Never respond to this symptom by raising the `wait_for_shell.py` timeout; the socket is absent indefinitely, not late |
| Native-systemd QA (`run-systemd-container-tests`) loses the `bluefin-test` session bus after qecore restarts GDM, leaving only the `gdm-greeter` bus; `org.gnome.Shell@user.service` times out on `Failed to make thread 'KMS thread' high priority scheduled: Timeout was reached`, gnome-shell aborts, and GDM reports `Session never registered, failing` | The same exclusive-DRM-master root cause as the row above, reached through a different runner: the shell ran with mutter's *native* backend and contended for the node's single GPU. The `KMS thread` message is the tell — a headless virtual monitor never drives DRM/KMS. Observed live in workflow `chairlift-diagnose-smoke-mhkxg` (suite=smoke) | `run-systemd-container-tests` installs the same `/etc/systemd/user/org.gnome.Shell@.service.d/10-headless.conf` drop-in in `TARGET_SETUP`, outside the suite guard (every desktop suite starts a shell) and before qecore touches GDM. Keep `--unsafe-mode` in the drop-in's `ExecStart=` — the reset overrides the unit line qecore rewrites, and without it every `Shell.Eval` returns `(false, '')`. Same operating rule: nothing in a nested QA target may take DRM master. Do not raise the `wait_for_shell.py` timeout; after the failed restart no session ever registers, so the socket is absent indefinitely |
| Container QA gets past the GNOME readiness gate, runs real checks, then reports `0 scenarios passed, 0 failed, N skipped` plus `HOOK-ERROR in after_all: AssertionError: No scenario matched tags`, with `qecore-headless startup failed: unrecoverable headless errors` and `headless: Issue was detected and might need attention` carrying `("stat: cannot statx '/run/user/1000/dconf/user': No such file or directory\n", 1, CalledProcessError(...))` | `/run/user/<uid>/dconf/user` is dconf's one-byte shm invalidation flag, not a persistent file. `dconf_shm_flag()` unlinks it on **every** write to the user database and `dconf_shm_open()` recreates it on the next read, so under a live session the path blinks in and out of existence. `qecore-headless`'s `verify_file_ownership()` tests `os.path.isfile()` and then runs a *separate* `sudo stat -c '%U %G' <path>`; spawning sudo is a tens-of-milliseconds window, so an ordinary dconf write between the two lookups makes stat exit 1 and qecore raise a non-recoverable failure, after which `before_scenario` skips every scenario. Reproduced on a live lane at ~10% (103 ENOENT hits per 1019 isfile-true iterations) with concurrent dconf read/write traffic. This is unrelated to `enable-linger`: qecore already returns early when the file is simply absent, so only the *disagreement* between the two lookups is fatal | Provisioning appends `|| true` to that one stat command in the installed `qecore-headless`, asserts the edit applied and that the script still parses, and fails the step otherwise. A vanished file then yields rc 0 with no `root` in the output, so qecore correctly does nothing, while a genuinely root-owned file still stats cleanly and is still removed. **Operating rule: never treat `/run/user/<uid>/dconf/user` as stable, and never respond to this symptom by sleeping, polling, or waiting for dconf to settle — there is no quiescent state while a session is running.** |
| Container QA scenarios fail with `RuntimeError: User 'bluefin-test' does not have write permissions for '/dev/uinput'` even though the user is in `input` | Podman gives the nested target its own tmpfs `/dev` — a different device and inode from both the pod's and the node's — and materializes `uinput` there as mode `0600 root:root` with **no group**. Group membership can never grant access to a node that has no group bit set | The nested provisioning now runs `chgrp input /dev/uinput && chmod 0660 /dev/uinput` after creating the test user. The node is lane-local, so this cannot affect concurrent lanes or ghost itself. Verify with `podman exec bluefin-qa-target ls -l /dev/uinput` |
| Every container QA scenario logs `ModuleNotFoundError: No module named 'pkg_resources'` from `qecore/sandbox.py` | qecore's `_attach_version_status_to_report()` imports `pkg_resources`, which ships only with setuptools, was dropped in setuptools 81, and is not seeded into fresh Python 3.12+ environments. `@non_critical_execution` catches it, so scenarios still run — this is lost version reporting and log noise, not a failure cause | The nested provisioning installs `setuptools<81` alongside `qecore dogtail behave`. Do not read a drop in this count as a drop in scenario failures; the two are independent |
| Container QA scenarios fail with `Cannot reach VM at 127.0.0.1 over SSH after 5 attempts: rc=255` | The scenario drives a device under test over SSH, but a container lane runs behave *inside* the target and has no sshd. This is a suite-selection defect, not a lane defect | Fix in `projectbluefin/testsuite`: tag the scenario `@vm_only`, which the suite hooks skip when `/run/.containerenv` is present. Never add an sshd to the nested target to make these pass |
| `pr-image-gc` exits 127 while bootstrapping ORAS | `lab-runner` does not provide `tar`, so a `curl | tar` pipeline fails | Download the archive and extract it with `python3 -m tarfile -e`; reconcile the GitOps-managed manifest before retrying |
| Service-catalog deploy step fails with "No manifests found" | Lane directory missing `manifests.yaml` | Create `tests/service_catalog/<lane>/manifests.yaml` per the contract |
| Service-catalog test step fails with "No test suite" | Lane directory missing under `tests/service_catalog/` | Create the lane test directory with at least one `test_*.py` file |
| Service-catalog namespace stuck terminating | Finalizer or PVC not released | Check for stuck PVCs or pods with `kubectl get all -n <ns>`, delete manually if needed |
| `lab-infra` sync wedged "waiting for healthy state of DaemonSet/..." | A DaemonSet pod is unhealthy on some node (e.g. hostPath missing on that host), blocking every subsequent manifests/ change | Fix or scope the DaemonSet (capability-label nodeSelector), then terminate the stuck operation so ArgoCD retries: `kubectl patch application lab-infra -n argocd --type=merge -p '{"status":{"operationState":{"phase":"Terminating"}}}'` |
| KubeStellar app sync stuck at kubeflex-controller-manager | Postgres hook deadlock under ArgoCD | Keep `installPostgreSQL: false` + separate `kubestellar-postgres` app; see `docs/skills/kubestellar/SKILL.md` |
| `kubeflex-controller-manager` consumes sustained host RX and repeats ControlPlane reconciliation about once per second | KubeFlex v0.9.1's status writes race across infrastructure, PostCreateHook, and final-readiness phases. A `wds1` Ingress with `ingressClassName: nginx` is a separate, inert child resource; its empty load-balancer status is not used by the hook readiness checks and is not the requeue cause | Upgrade `argocd/kubestellar-app.yaml` to core-chart 0.30.0 (KubeFlex v0.9.3), reconcile through ArgoCD, and verify the controller logs plus Prometheus `container_network_receive_bytes_total` rate. Do not install nginx or add an ingress class just to suppress the diagnostic |
| `test-lane` nodes sit `Pending` for tens of minutes with no pods created, and `kubectl get wf <name> -o json \| jq .status.synchronization` shows `waiting` on `ghost-container-qa` | A single QA pipeline held most of the 6 semaphore slots. `spec.parallelism` is not inherited through `templateRef`, so poller-dispatched runs (pr-poller's inline `pr-pipeline`, `image-poller`) fanned out every `withItems` lane at once | The `pipeline` templates now carry template-level `parallelism: 2`, which survives `templateRef`. Never fix this by raising `ghost-container-qa`; that only moves the threshold. Verify with `python3 scripts/check_semaphore_topology.py argo/` and see [patterns §15b](../skills/argo-workflows/patterns.md) |
| Several workflows running concurrently for the same PR at different SHAs, or workflows still running for merged PRs | The poller deduped on `pr-number` + `pr-sha` and never cancelled superseded or closed-PR runs; each stale run held a `ghost-container-qa` slot for ~20 minutes | `pr-poller` now supersedes (newest SHA wins) on every poll and reaps workflows whose PR left the open set, using `spec.shutdown: Stop` so `report-final` still publishes a terminal `ghost-lab` status. Inspect with `kubectl get wf -n argo -l bluefin.io/trigger=pr-auto -L bluefin.io/repository,bluefin.io/pr-number,bluefin.io/pr-sha`. Reaping is skipped for any repo whose open-PR enumeration hit an API error or returned zero PRs. See [patterns §20ac](../skills/argo-workflows/patterns.md) |
| A PR's `ghost-lab` status is stuck on `pending` after its workflow disappeared | The workflow was hard-deleted, so its `onExit` `report-final` handler never ran | Never `kubectl delete` a live PR workflow; use `kubectl patch workflow <name> -n argo --type merge -p '{"spec":{"shutdown":"Stop"}}'`. To clear an already-stranded status, re-run the poller with `refresh-existing=true` |
| Workflow pod rejected `failed quota: argo-quota` | Template missing resources requests/limits | Add explicit cpu+memory requests and limits to every container/script |
| Lab QA ran but no `testing-lab / <repo>` Check Run appears on the factory PR | Half-enrolled repo — the poller dispatched `lab-check` but the target repo has no `.github/workflows/lab-check.yml` receiver, so the dispatch is silently dropped | Confirm the poll succeeded: `kubectl get workflows -n argo -l workflows.argoproj.io/cron-workflow=pr-label-poller`. Confirm the per-PR QA workflow exists and completed. Then confirm the receiver exists: `gh api repos/projectbluefin/<repo>/contents/.github/workflows/lab-check.yml` — a `404` means the repo is only sender-enrolled (in `AUTO_REPOS`) and needs the receiver added on the target repo's default branch. See [`docs/skills/argo-workflows/patterns.md`](../skills/argo-workflows/patterns.md) §20aa. |

### KubeStellar / Console failure modes

See `docs/skills/kubestellar/SKILL.md` (downsync, WEC join, RBAC) and
`docs/skills/console-dashboard/SKILL.md` (Console recovery, exposure policy).
Upgrade order: KubeFlex/postgres -> core-chart -> Console; rerun
`kubestellar-smoke-test` after every core upgrade.

## Historical notes

Date-stamped iteration lessons were removed in the ponytail audit (commit
81f0cc6f); recover them from git history if needed.
Keep this file timeless: architecture, topology, and durable failure modes only.
