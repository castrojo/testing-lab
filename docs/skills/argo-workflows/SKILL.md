---
name: argo-workflows
description: >
  Authoring, linting, and submitting Argo Workflows and WorkflowTemplates in
  the lab. Use when writing or editing any .yaml file under
  argo/workflow-templates/, argo/bootstrap/, or argo/*.yaml, or when
  debugging a failed workflow run.
metadata:
  context7-sources:
    - /argoproj/argo-workflows
    - /kubernetes/website
    - /oras-project/oras
    - /websites/github_en_rest
---

# Argo Workflows — lab Skill

## When to Use

- Editing any `argo/workflow-templates/*.yaml` or `argo/bootstrap/*.yaml`
- Writing a new pipeline (provision, test, teardown)
- Adding a new `argo/*.yaml` submit-time Workflow
- Debugging a stuck or failed workflow
- Adding a CronWorkflow to `manifests/`

## When NOT to Use

- ArgoCD Application changes → `gitops-argocd.md`
- KubeVirt VM manifest design → `kubevirt-vms.md`
- behave/dogtail test authoring → `test-authoring.md`

## Core Process

The workflow authoring guidance is split by topic:

- [Authoring rules](authoring.md) — template structure, parameters, outputs, hooks, linting, ArgoCD ownership.
- [Common patterns](patterns.md) — image-sync, VM concurrency, result publishing, CronWorkflow traps, Dakota lanes.

## Common Rationalizations

| "The sub-template will see workflow.parameters directly." | It will not. Argo Workflows scopes parameters per-template. Always pass explicitly. |
| "I applied the template with kubectl — it's fine." | ArgoCD selfHeal will overwrite it within minutes. Use git. |
| "The lint passed locally, I'll skip CI." | CI runs against the same offline linter. If it passed locally, it passes in CI. |
| "The template is DEPRECATED, I'll clean it up later." | It will never get cleaned up. Delete it now — `prune: true` handles the rest. |
| "I need each step in the chain to have its own `when` guard." | Use a `dag` with `depends: "prior.Succeeded"` — downstream tasks cascade-omit automatically. |

## Red Flags

- **Host Namespace Crash Risk**: Running containers with `hostPID: true` or `hostIPC: true` (which was previously thought to be needed by loopback installation, but is an anti-pattern). If the container fails or is terminated/deleted, `argoexec` will tear down and SIGTERM all processes in the host namespace, crashing `k3s`, `sshd`, and the node. Avoid `hostPID: true` entirely. Ensure containers-storage and loopback visibility are achieved via volume mounts without host PID exposure.
- **Permission Denied Risk**: Forgetting to mount an `emptyDir` at `/tmp` for containers running as non-root (1000) that need to write results, scripts, or temporary configs to `/tmp`. Inside bootc images, `/tmp` is root-owned and restricted, leading to immediate execution failures.
- Adding a separate log aggregation stack (Loki, Promtail, Vector, etc.) alongside Argo — Argo Server already retains pod logs for the workflow TTL. A separate stack duplicates storage, adds pods/PVCs, and creates a Helm-outside-ArgoCD installation with GitOps debt. `argo logs` covers the same use case.
- **Outage Risk**: Leaving nodes cordoned (`SchedulingDisabled`) after k3s upgrades or manual interventions. This completely blocks system pods (including CoreDNS!) from scheduling, causing cluster-wide DNS timeouts (`read udp i/o timeout`) and a silent, complete cluster outage. Always ensure nodes are uncordoned (`kubectl uncordon`) and `Ready`.
- **Outage Risk**: Setting low memory limits (under 2Gi) for any runner/script step that performs large file transfers (e.g. copying 400MB+ Flatcar update payloads over SCP/kubectl cp). File caching and transfer buffers will instantly trigger the container OOM-killer (exit code 137). Always set memory limits to at least 2Gi–4Gi for transfer-heavy steps.
- `synchronization.semaphore:` (singular) in any pipeline — deprecated, rejected by ArgoCD schema. Use `synchronization.semaphores:` (list with `- configMapKeyRef:` item)
- `spec.schedule:` (singular) on a CronWorkflow — field does not exist in CRD schema; use `spec.schedules:` (array)
- A pipeline with VMs and no `spec.activeDeadlineSeconds` — a stuck VM holds its semaphore slot forever
- A VM pipeline that adds a semaphore without a documented cross-workflow
  capacity need — ordinary VM concurrency is handled by virt-launcher memory
  requests and does not need a lock
- A memory-constrained VM pipeline that omits a documented template-level
  semaphore — concurrent virt-launcher and runner pods can exhaust node memory
- A template that consumes a scarce, node-pinned resource relying only on
  workflow `parallelism` — that limit does not cover simultaneous workflows.
  Put a template-level `synchronization.semaphores` reference on the shared
  template and define its capacity in the GitOps-managed ConfigMap.
- A `dag`/`steps` template that fans out (`withItems`/`withParam`, or several
  sibling tasks) to a semaphore-holding template via `templateRef` but declares
  no template-level `parallelism` — `spec.parallelism` is **not** inherited
  through `templateRef` (only a spec-level `workflowTemplateRef` inherits it),
  so one workflow can hold every slot and starve every other run. Put
  `parallelism` on the fan-out template and keep it below the semaphore limit.
  Enforced by `scripts/check_semaphore_topology.py` in `just lint`; see
  [patterns §15b](patterns.md).
- A `spec.synchronization` semaphore on a pipeline whose children need the same
  key — the parent holds the slot for the whole run and deadlocks its own
  children. Declare the key only on the leaf template that consumes the
  resource.
- A `synchronization` block placed directly on a `dag.tasks[]` entry — Argo's
  schema rejects it. Wrap the `templateRef` in a local `steps` template and put
  the ConfigMap-backed semaphore on that wrapper when only one DAG task needs
  cross-workflow serialization.
- A `steps` or `dag` task calling a sub-template without `arguments:`
- `{{steps.X.outputs...}}` or `{{tasks.X.outputs...}}` used as an input
  parameter *default inside a leaf template* — that scope only exists at
  the steps/dag call site, and unresolved literals reach the shell (live
  failures: catalog-install-lsio-njq2s, -vhbcb). Pass via call-site
  `arguments:`. Corollary: each step is its own pod — never pass file
  paths between steps; pass file *content* via an output parameter
  (`valueFrom.path`, 256KB cap) or an artifact.
- A pipeline with no `onExit` handler (VM will leak on failure)
- A workflow invoking a builder that mounts a named staging PVC without defining
  the matching `volumeClaimTemplates` entry; define the claim at workflow scope
  and set `volumeClaimGC.strategy: OnWorkflowCompletion` so failed builds do not
  leave staging storage behind.
- A workflow that declares `volumeClaimTemplates` without
  `volumeClaimGC.strategy: OnWorkflowCompletion` — completed runs leave staging
  PVCs behind.
- A test that falls back to a direct `bootc switch` after the production
  toggle command fails to stage — this masks the behavior under test; fail the
  step and report the staging failure instead.
- A declared workflow parameter that is not passed to the template or helper
  that consumes it — especially disk/image and host-root parameters. Trace
  every contract parameter through the call-site arguments.
- Any `script:` template without `resources:` limits
- Templates in `argo/workflow-templates/` applied with `kubectl apply` (not via git)
- A PR-dispatching poller that dedups on `pr-number` + `pr-sha` but never
  cancels the superseded run, or that leaves workflows running after their PR
  merges — both drain the `ghost-container-qa` semaphore for ~20 minutes per
  stale run. Supersede on every poll and reap closed PRs; see
  [patterns §20ac](patterns.md).
- Cancelling a PR workflow with `kubectl delete` — that skips `onExit`, so
  `report-final` never runs and the commit is stuck on `pending` forever. Use
  `spec.shutdown: Stop` (what `argo stop` sets).
- Reaping workflows by `bluefin.io/pr-number` alone — PR numbers collide across
  repositories. Always pair it with `bluefin.io/repository`.
- Treating "PR absent from the open-PR API result" as "PR is closed" without
  proving the API call succeeded — a transient GitHub error then mass-cancels
  every live run.
- A `pr-poller` (or any PR-gating workflow) that skips on ANY existing commit status — it must skip only `pending` (in-flight) and `success` (already passed), and re-test on `error`/`failure`. Skipping `error` means stale statuses from deleted workflows permanently block retests.
- A VM or build pipeline that uses a node selector to reach local storage. Use
  scheduler-selected `WaitForFirstConsumer` PVC placement on an explicitly
  configured non-root data mount instead.
- Python inside bash inside YAML (colons + quotes cause parse errors — use `curl`+`jq` instead; never `python3 -c` or heredoc Python; see §16 GitHub Contents API pattern)
- Heredoc `<< 'EOF'` inside a YAML block scalar — indentation breaks the YAML parser. ArgoCD returns `ManifestGenerationError: yaml: could not find expected ':'`. Write scripts to files in initContainers or use inline jq instead.
- **Exclusive GPU contention in nested GNOME QA**: any nested QA target that lets
  GNOME Shell run with mutter's *native* backend takes exclusive DRM master on the
  host's `/dev/dri/card*`. ghost has one GPU, so with `ghost-container-qa` at 6 only
  the first lane ever gets a session; the rest log
  `Failed to open gpu '/dev/dri/card1': ...EBUSY: Device or resource busy`, stall in
  `Failed to make thread 'KMS thread' high priority scheduled`, and GDM loops
  `GdmDisplay: Session never registered, failing`. Force headless via a
  `/etc/systemd/user/org.gnome.Shell@.service.d/*.conf` drop-in with
  `gnome-shell --mode=%i --headless --virtual-monitor WxH`. The host GPU is *not* a
  per-lane resource.
- **Losing `XDG_RUNTIME_DIR` across a display-manager restart**: `systemd-logind`
  destroys `/run/user/<uid>` — and the session bus socket inside it — when the user's
  last session ends. `qecore-headless` stops GDM and SIGTERMs the session, so any
  runner holding `unix:path=/run/user/<uid>/bus` is left pointing at a socket that
  only returns if a brand new login succeeds. Run `loginctl enable-linger <user>`
  before starting the display manager so `user@<uid>.service` and the bus survive.
  This is hardening, not a cure: with the GPU contended, lingering only shifts the
  failure class from `bus-unavailable` to `service-unknown`, because the shell still
  never starts. Fix the GPU contention first.
- A multi-line nested script passed as `podman exec <ctr> bash -c '...'` inside a YAML block scalar. The first apostrophe in the body (e.g. `printf '%s\n'`) silently closes the outer quote, so bash receives mangled text (`printf %sn`) with no parse error. Use `podman exec -i <ctr> bash -s <<'MARKER'` with the terminator at the block-scalar indent column instead.
- `registry.k8s.io/kubectl` used as a shell-capable image — it is distroless, has no bash, nc, or any shell utilities. Use `cgr.dev/chainguard/kubectl:latest-dev` when you need kubectl + bash together
- `ghcr.io/projectbluefin/lab-runner:latest` assumed to contain `skopeo` or `oras` — the live image can omit both. Use pinned `quay.io/skopeo/stable` and bootstrap pinned ORAS when registry referrers are required.
- A WorkflowTemplate file name that doesn't match its `metadata.name` (confuses ArgoCD tracking)
- A shared containerDisk builder that hard-codes its output repository — pass the
  destination repository through check, build, and push templates whenever
  multiple image families share the builder, or one family can clobber another.
- Prebaking a DUT-specific KDE automation binary without pinning the source
  commit and validating the installed server and `inputsynth` paths during the
  image build.
- Templates annotated `DEPRECATED` that haven't been deleted from git
- Two CronWorkflows with the same schedule covering overlapping namespaces
- A `steps` template with the same `when` condition on 3+ sequential steps (convert to `dag` + `depends` chain)
- A CronWorkflow that has a `dry-run` parameter defaulting to `"true"` — it will log `KEEP`/`DELETE` decisions and then do nothing; disk fills silently
- Setting a global Argo `parallelism` / `namespaceParallelism` cap in the workflow-controller-configmap — the real backpressure is Kubernetes pod scheduling (pod resource requests). Remove the cap; let the scheduler self-limit.
- Using `pr-test-N-` as a workflow generateName prefix — use the repo slug: `blu-N-`, `lts-N-`, `dak-N-`, `knu-N-` so k9s and the Argo UI show meaningful names at a glance
- **GC CronWorkflow using `registry.k8s.io/kubectl`** — distroless, no bash; every run exits with `bash: not found` and the GC step is skipped silently. Pods and orphaned objects accumulate until the cluster fills. Use `cgr.dev/chainguard/kubectl:latest-dev`. Symptom: `kubectl get cronworkflow orphan-pod-gc -n argo` shows `LAST SCHEDULE` advancing but pods keep piling up; check the workflow pod logs for `bash: not found`.
- Any `image:` in `argo/` or `manifests/` referencing `:5000` for the local OCI registry — `:5000` is the container-internal Zot port; use the NodePort `<lab-ip>:30500` so non-hostNetwork pods can reach it
- Any `image:` referencing a registry not in the allowlist (`ghcr.io`, `quay.io`, `registry.fedoraproject.org`, `registry.access.redhat.com`, `registry.k8s.io`, `<lab-ip>`, `localhost`) — enforce with the lint gate in `.github/workflows/lint.yaml`
- `depends: "X.Succeeded"` on a task that follows a conditionally-skippable upstream — if upstream is Skipped, the downstream task is Omitted and the whole DAG may appear to succeed even though the chain broke; use `depends: "(X.Succeeded || X.Skipped)"` when the upstream has its own `when` guard
- A downstream `when` condition that references `{{tasks.X.outputs.result}}` where task X has its own `when` guard — if X is Skipped its output is undefined and the downstream task silently skips too. Fix: let X always run; handle the bypass inside the script (see §18).
- A `force=true` rebuild workflow where only 1–2 nodes appear (DAG + a Skipped check) and no build step ever runs — this is the §18 `when`/Skipped output bug, not a semaphore or mutex issue
- Post-processing K8sGPT JSON with `for item in data.get("results", [])` or `len(data["results"])` without normalizing first — namespace-scoped empty scans can emit `"results": null`, which crashes the script and then triggers a second Argo missing-output-path error. Normalize with `results = data.get("results") or []` before iterating or counting.
- Passing `containerdisk-tag`, `ssh-key-secret`, `vm-memory`, or caller-side `namespace` parameters into `bluefin-qa-pipeline`/`dakota-qa-pipeline` after the container-only migration — those callers must send only `image`, `image-tag`, `suites`, `variant`, `branch`, and `testsuite-branch`.
- Any Argo CronWorkflow script template in `argo` namespace without explicit `resources.requests` and `resources.limits` — the `argo-quota` admission check rejects pod creation.
- Any pod containing `initContainers` (like `git-sync` in `run-gnome-tests.yaml`) that lacks explicit `resources.requests` and `resources.limits` blocks — the `argo-quota` admission controller evaluates all containers in a pod (including init containers), and will reject the entire pod if any container lacks resource definitions.
- `orphan-pod-gc` memory capped too low (128Mi) — large pod inventories can OOM the cleanup step (`exit code 137`) and silently skip GC.
- An image poller that writes the new digest to `image-polling-digests` before the downstream QA pipeline succeeds — failures under cluster pressure then drop work permanently (digest is marked seen, no retry on next poll). Persist digest only after `run-pipeline.Succeeded`.
- A success-only poller that blindly writes its captured SHA after a retry or resume — an older workflow can overwrite state advanced by a newer successful run. Capture the stored value during admission and compare it again before the final write.
- Aurora/Bazzite digest pollers running full GNOME suite sets (`smoke,common,developer,software,system`) even though these variants are KDE-focused — this creates 5x VM pressure per trigger and overloads scheduling. Keep Aurora/Bazzite pollers on `suites: system`.
- K8sGPT finding no-endpoint Services for `argocd-applicationset-controller`, `argocd-dex-server`, `argocd-notifications-controller-metrics`, or `kubevirt/virt-exportproxy` — these are documented control-plane exceptions in this cluster shape.
- Commit message not in Conventional Commits format — the pre-commit hook rejects any commit not matching `<type>(<scope>): <description>`. Valid types: `feat fix ci chore docs refactor test build perf revert`
- Removing `suspend: true` from a CronWorkflow, seeing ArgoCD report `Synced`, and stopping there — the live field can silently stay `true`. Always re-check with `kubectl get cronworkflow <name> -o jsonpath='{.spec.suspend}'` after sync.
- A digest-comparison poller (`digest-watch`, `dakota-commit-poller`, etc.) treated as a guarantee that a downstream artifact exists — it only reacts to source digest *changes*, not to the artifact disappearing out-of-band (disk wipe, registry GC). After any disk/registry event, force-rebuild manually; don't wait for the poller.
- A BuildStream poller relying only on the `bst-build` semaphore — execution is serialized, but waiting workflows can still grow without bound. Automated callers must enforce the two-workflow `bluefin.io/bst-workload=true` admission ceiling before submission.
- **Queue Starvation / `activeDeadlineSeconds` Trap**: Leaving a workflow's `activeDeadlineSeconds` at default (or unspecified) when it queues under a template-level semaphore or resource limit. The workflow-level deadline starts ticking upon *submission/creation*, not *execution/scheduling*. If a workflow queues for longer than the global default deadline (e.g., 2h), it gets instantly canceled with `DeadlineExceeded` as soon as it begins running. Always set a generous workflow-level deadline (e.g., 4h/14400s) on queueable templates and dynamic API submission specs.
- **Clock-only Cron serialization**: Spacing a CronWorkflow away from other schedules is not a concurrency guard. When a scheduled workflow shares a scarce VM namespace or runner, reference a ConfigMap-backed template semaphore and document the key; keep the schedule as a trigger only.
- **Secret leakage via shell tracing**: Never use `set -x`/`set -eux` in a script that invokes authenticated APIs or expands secret-bearing variables. Argo retains command output in workflow logs. Disable tracing for the whole script or bracket only non-secret diagnostics with explicit `set +x`/`set -x` boundaries, then inspect logs for credentials before publishing evidence.
- **Assuming registry tools exist in `lab-runner`**: the image does not include
  ORAS or skopeo. Use a pinned tool image (or an explicit, checked bootstrap),
  and validate flags against that pinned release. In particular, ORAS v1.2.3
  `discover` supports `--format json` but not `--depth`.
- **Assuming archive utilities exist in `lab-runner`**: the image includes
  Python, `curl`, and `jq`, but not `tar`. Download archives to a workspace,
  extract them with `python3 -m tarfile`, and remove the archive afterward, or
  use a pinned image that provides the required utility.
- **Doubling braces for generated child workflows**: `{{{{workflow.*}}}}`
  reaches a child Workflow literally and its `when` expressions compare the
  wrong value. When an outer script must emit an Argo expression, build the
  braces at runtime (for example with `printf '\x7b\x7b'`) so the outer
  template does not consume them.
- **PR approval gate bypass**: Routine labels such as `automerge` or `chore/deps` are not maintainer approval. PR-batch templates must stop before build/QA when `pr/needs-review` remains or live GitHub review data lacks a verified maintainer approval.
- **Half-enrolled status reporting**: A repository-dispatch Check Run sender cannot create visible feedback unless the target repository also has its receiver workflow. For enrolled repositories without that receiver, use a direct commit-status reporter with a stable context instead of silently dropping lifecycle updates.
- **Custom metric cardinality**: Never label workflow metrics with workflow
  names, UIDs, commit SHAs, refs, image digests, or build elements. Use only
  constant pipeline identifiers and bounded states. Workflow-level completion
  metrics are safe only when the workflow status truthfully represents the
  publish result; non-blocking or transitional DAG branches must be fixed first.
- A KDE GUI runner that copies the GNOME runner without replacing
  `qecore-headless` and the GNOME daemon — use the VM's
  `selenium-webdriver-at-spi-run`, forward port 4723, and gate test start on
  its `/status` endpoint.
- KDE QA callers must pass the runner's `branch` parameter (not the removed
  `testsuite-branch`) and use the established `aurora-test` namespace. The
  runner must source a generated session environment containing D-Bus,
  Wayland, AT-SPI, `XDG_SESSION_DESKTOP=kde`, and its WebDriver URL before
  starting Selenium.
- KDE runner and teardown containers must use the repository-approved
  digest-pinned `lab-runner` and shell-capable `kubectl` references; never
  reintroduce floating `:latest` or `:latest-dev` tags in those workflows.

## Verification

Before marking any WorkflowTemplate change done:

- [ ] Nested GNOME QA targets force `gnome-shell --headless` and enable
      `loginctl enable-linger` for the test user before starting GDM
- [ ] All VM-running pipelines have `spec.activeDeadlineSeconds` set
- [ ] All queueable templates/dynamic workflows (e.g. `build-containerdisk` and `digest-watch` submit payloads) have a generous workflow-level `activeDeadlineSeconds` (e.g., 14400s / 4h) to avoid queue starvation
- [ ] Any new CronWorkflow uses `spec.schedules:` (array), not `spec.schedule:` (singular)
- [ ] Any scheduled workflow sharing a VM namespace or scarce runner uses a
      documented ConfigMap-backed template semaphore; clock separation alone is
      not treated as serialization
- [ ] All sub-template calls include explicit `arguments:` blocks
- [ ] Pipeline has `onExit: cleanup` handler
- [ ] All pod-running templates have `resources:` requests and limits
- [ ] Every node-pinned, high-memory shared template has a ConfigMap-backed
      template-level semaphore sized to the node's allocatable capacity
- [ ] Any semaphore intended for one DAG task is attached to a wrapper template,
      not directly to `dag.tasks[]`; the wrapper's `steps` call the external
      `templateRef`
- [ ] VM pipelines are scheduler-managed by default; if cross-workflow memory
      contention requires serialization, use a documented template-level
      ConfigMap semaphore with an explicit capacity
- [ ] Change is committed and pushed — not manually applied to cluster
- [ ] `description:` annotation present on the new/modified template
- [ ] File name matches `metadata.name` (e.g. `provision-containerdisk-vm.yaml` for `name: provision-containerdisk-vm`)
- [ ] Any VM pipeline semaphore is justified by documented cross-workflow
      memory contention and is attached at template level, not workflow scope
- [ ] VM pipeline spec has `activeDeadlineSeconds` (1h or 2h) so stuck VMs self-evict
- [ ] No `nodeSelector: kubernetes.io/hostname: ghost` in VM specs — VMs float to any KubeVirt-capable node
- [ ] GitHub Contents API write-backs use curl+jq, not inline Python; output is
      retained through the workflow artifact/log mechanism or a workflow PVC,
      never a root-backed hostPath
- [ ] `kubectl get workflowtemplate -n argo` shows no cluster-only templates (not in git) unless they're intentional bootstrap one-shots
- [ ] No CronWorkflow with a `dry-run` parameter whose default is `"true"` — verify GC jobs actually delete
- [ ] All local OCI registry references use `:30500` (NodePort), not `:5000` (container-internal)
- [ ] `grep -rn 'image:' argo/ manifests/` shows only allowlisted registries: `ghcr.io`, `quay.io`, `registry.fedoraproject.org`, `registry.access.redhat.com`, `registry.k8s.io`, `<lab-ip>`, `localhost`
- [ ] Image pollers update digest state only after QA pipeline success (failed runs must retry on next poll)
- [ ] Poller state writers depend on the downstream task's `.Succeeded` result and reject stale writes when the stored value changed after admission
- [ ] After removing `suspend: true` from a CronWorkflow and syncing, live `spec.suspend` confirmed via `kubectl get -o jsonpath` — not assumed from ArgoCD's `Synced` status alone
- [ ] After any disk wipe/registry migration/Zot cleanup, every affected containerDisk tag manually force-rebuilt rather than assuming a digest-comparison poller will self-heal
- [ ] Custom metrics share identical help text and histogram buckets across
      templates, and labels are limited to low-cardinality constants/states
- [ ] Every PR-gated repository has either a live repository-dispatch receiver
      or an explicit direct commit-status fallback; verify the target workflow
      exists on the repository's default branch before relying on dispatch
- [ ] Registry workflows use a pinned image that actually contains every CLI
      invoked by the script, with flags valid for that exact version
- [ ] KDE GUI runners preserve the GNOME runner's parameter/result contract,
      use `selenium-webdriver-at-spi-run`, forward `4723:4723`, and wait for
      WebDriver readiness before Behave execution
- [ ] Aurora/KDE sabotage runs are explicit, restricted to `aurora-test`, and
      exercise both the nonexistent-binary and killed-`plasmashell` red paths;
      failure results and `kde_faillog` artifacts must be retained before
      teardown
- [ ] KDE soak evidence uses the newest 30 persisted runs and a fixed two-flake
      infrastructure budget; each counted flake has a filed issue URL; the gate
      never requires a consecutive-green streak; promotion remains a human
      decision
