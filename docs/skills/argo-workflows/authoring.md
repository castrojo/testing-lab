---
name: argo-authoring
description: >
  Argo Workflows template authoring rules and structure for the lab. Use when
  creating or changing WorkflowTemplates, CronWorkflows, or workflow metadata.
metadata:
  context7-sources:
    - /argoproj/argo-workflows
---

# Argo Workflows Authoring Rules

## Core Process

### 1. Template structure rules

Every WorkflowTemplate in `argo/workflow-templates/` must follow this shape:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: WorkflowTemplate
metadata:
  name: my-template
  namespace: argo
  annotations:
    description: |
      One-paragraph description of what this template does and when to use it.
  labels:
    app.kubernetes.io/part-of: bluefin-test-suite
spec:
  serviceAccountName: argo
  templates:
    - name: entrypoint-template
      inputs:
        parameters:
          - name: my-param
            value: "default"
      # ...
```

- `serviceAccountName: argo` on every WorkflowTemplate
- `namespace: argo` always
- `description:` annotation on every template — one paragraph saying what it does
- `volumeMounts` for a `script` or `container` belong inside that executor
  block, while matching `volumes` belong on the surrounding template. Argo's
  strict decoder rejects executor mounts placed directly on the template.
- Template names are unique within one WorkflowTemplate. Give an entrypoint
  and its executable step distinct names (for example, `toggle-testing` and
  `run-toggle-testing`) rather than reusing the same name.

### 1b. Debugging a workflow stuck on a dead node

If a workflow step never progresses and the pod stays Pending/Terminating on a worker that is `NotReady`, stop the workflow and resubmit it rather than waiting for the stuck pod to recover:

1. `kubectl get nodes` — identify any `NotReady` worker.
2. `argo stop -n argo <workflow>` — stop the stuck workflow and let Argo clear the pod.
3. `argo submit ...` — submit a fresh workflow; the scheduler will place it on a healthy node such as `ghost`.
4. Verify with `kubectl get pods -n argo -l workflows.argoproj.io/workflow=<name> -o wide` and confirm the new pod lands on a `Ready` node.

This is especially relevant for cache-heavy BST builds because the workflow uses
PVC-backed workspace state and shared Buildbarn caches; a fresh run can be
placed on any healthy node without depending on a node-local root-disk cache.

For distributed BuildStream runs, fix the shared config or template first and then stop/re-submit the workflow once; do not let Argo keep retrying a pod that is failing for a known configuration reason. Repeated retries burn node CPU and memory, overfill the namespace queue, and make the cluster look resource-constrained even when the underlying config issue is trivial.

A BuildStream run is only "distributed" when BuildBarn remote execution is actually engaged over USB4. Remote artifact caches alone are not distributed builds. Before admission, require fresh `lab.projectbluefin.io/usb4-link=up` observations and Ready workers on both `ghost` and `exo-0`. Before calling any BuildStream build distributed, require three independent pieces of evidence: (1) `projects.<name>.remote-execution` appears in the generated BuildStream config for the project, (2) BuildStream startup logs report a Remote Execution Configuration pointing at the BuildBarn frontend, and (3) current action activity is visible on BuildBarn workers (`kubectl logs -n buildbarn` or BuildBarn dashboards). Cache-only, Ethernet-backed, or local-driver BuildStream behavior is a failed operational state, not a supported fallback, and must fail fast.

### 2. Parameter passing — always explicit

Sub-templates never inherit parameters from the caller scope. Pass every parameter explicitly:

```yaml
# ✅ CORRECT — explicit argument passing
- name: pipeline
  steps:
  - - name: build
      template: build-step
      arguments:
        parameters:
        - name: variant
          value: '{{workflow.parameters.variant}}'
        - name: image-tag
          value: '{{workflow.parameters.image-tag}}'

# ✗ WRONG — sub-template cannot see workflow.parameters directly
- name: pipeline
  steps:
  - - name: build
      template: build-step   # missing arguments: — lint will catch this
```

> Verified against: `/argoproj/argo-workflows` — WorkflowTemplate docs

### 3. Referencing external templates

Use `templateRef` for cross-WorkflowTemplate calls:

```yaml
- name: run-tests
  depends: "provision.Succeeded"
  templateRef:
    name: run-gnome-tests          # WorkflowTemplate name
    template: run-gnome-tests      # template name within that WorkflowTemplate
  arguments:
    parameters:
    - name: vm-ip
      value: "{{tasks.provision.outputs.parameters.vm-ip}}"
```

`templateRef` imports the referenced template body, not the referenced
WorkflowTemplate's workflow-level `serviceAccountName`. A
`serviceAccountName` declared on the referenced template itself is preserved,
so put elevated identity only on that template and keep parent verification
templates on a scoped ServiceAccount.

> Source: `/argoproj/argo-workflows` — Workflow Templates, “Referencing other
> WorkflowTemplates”; `TemplateRef` fields (`name`, `template`, `clusterScope`).

### 3a. Container-only QA caller contract

The container-only QA templates (`bluefin-qa-pipeline`, `dakota-qa-pipeline`,
and any CronWorkflow/PR caller that feeds them) accept only the OCI-centric
payload:

- `image`
- `image-tag`
- `image-digest` (optional; required when the caller resolved an exact OCI digest)
- `suites`
- `variant`
- `branch`
- `testsuite-branch`
- `testsuite-repo`

Do **not** pass legacy VM-era parameters such as `containerdisk-tag`,
`ssh-key-secret`, `vm-memory`, or caller-side `namespace` blocks. For testsuite
PRs, override both `testsuite-repo` and `testsuite-branch` with the PR head
repository and branch; keep the canonical repository and `main` for other
repos.

### 3b. QA workflow evidence enrollment

To enroll a top-level QA WorkflowTemplate in the canonical evidence reconciler,
add a stable label through `spec.workflowMetadata`, not only through the
WorkflowTemplate object's metadata:

```yaml
spec:
  workflowMetadata:
    labels:
      bluefin.io/evidence-contract: qa-run-v1
```

`workflowMetadata` is applied to Workflows created from the template (including
`workflowTemplateRef` callers), so the reconciler can select real run objects.
Pass an `image-digest` parameter whenever a caller has resolved one; retain an
explicit empty default for manual tag-based submissions. Source:
`/argoproj/argo-workflows`, Workflow Templates > Adding labels/annotations to
Workflows with workflowMetadata.

For a digest-poller DAG, pass the poller's
`{{tasks.check-digest.outputs.parameters.remote-digest}}` explicitly to the QA
pipeline's declared `image-digest` parameter. A matching task dependency does
not propagate the value on its own. Source: `/argoproj/argo-workflows`,
Workflow Inputs > Pass Outputs Between DAG Tasks.

When a CronWorkflow defines its own `workflowSpec` and calls a QA pipeline only
through a DAG task `templateRef`, it creates the parent Workflow itself; the
referenced template does not create a child Workflow. Put the evidence label on
the CronWorkflow's own `spec.workflowMetadata` as well:

```yaml
spec:
  workflowMetadata:
    labels:
      bluefin.io/evidence-contract: qa-run-v1
  workflowSpec:
    templates:
      - name: pipeline
        dag:
          tasks:
            - name: run-qa
              templateRef:
                name: dakota-qa-pipeline
                template: pipeline
```

Source: `/argoproj/argo-workflows`, Cron Workflows > CronWorkflow Spec >
`workflowSpec` and `workflowMetadata`.

### 4. Output parameters — use `script` with stdout

For steps that produce a value consumed by downstream steps, write the result to stdout and nothing else:

```yaml
- name: wait-for-vm-ready
  script:
    image: cgr.dev/chainguard/kubectl:latest-dev
    command: [bash]
    source: |
      # Send all debug output to stderr
      echo "Waiting for VMI..." >&2
      kubectl wait vmi ...
      # Only the result goes to stdout
      echo "${POD_IP}"
  outputs: {}    # outputs.result captures the last stdout line automatically
```

Then consume via `{{steps.wait-for-vm.outputs.result}}`.

#### No artifact repository configured? use output parameters, not artifacts

If a template emits file output but the cluster has no Argo artifact repository configured, `outputs.artifacts` fails at wait time with:

`You need to configure artifact storage`

Use `outputs.parameters.valueFrom.path` instead for small/medium JSON payloads:

```yaml
outputs:
  parameters:
    - name: result-json
      valueFrom:
        path: /tmp/results/result.json
```

This keeps workflows self-contained while still exposing machine-readable output in workflow status (`argo get` / `kubectl get wf -o json`).

#### Aggregating JSON output parameters from loops

When a step runs `withItems` or `withParam` and each iteration emits a JSON object via `outputs.parameters.valueFrom.path`, referencing `{{steps.<step>.outputs.parameters.<name>}}` returns a JSON array. Argo stores each value as a string, so the aggregated array may contain either JSON-encoded strings or already-parsed objects depending on the runtime version and how the value was produced. Make the consumer robust to both shapes:

```bash
# inside the aggregate step
SUMMARIES='{{steps.scan-lane.outputs.parameters.summary}}'
echo "$SUMMARIES" | jq '
  (if (length > 0 and (.[0] | type) == "string") then map(fromjson) else . end) as $items |
  $items[] | .
'
```

This avoids `jq` parse errors when the aggregated values arrive as strings and keeps the template compatible if Argo later normalizes them to objects.

**`ghcr.io/projectbluefin/lab-runner:latest`** is the preferred, organization-owned FSDK container for pollers, GC, and CronWorkflows that need `kubectl`, `curl`, `jq`, and a full shell. Do not assume it contains registry clients: verified 2026-08, the live `latest` image carries bash, curl, git, jq, python3, and kubectl, but **not** `skopeo`, `oras`, or `tar`. Use the digest-pinned `quay.io/skopeo/stable` image for shell+skopeo steps (or the distroless org `ghcr.io/projectbluefin/skopeo` for shell-free `container:` steps) and the digest-pinned `ghcr.io/oras-project/oras` image when referrer handling is required.

For steps that still use other images, **`cgr.dev/chainguard/kubectl:latest-dev`** can be used as a fallback if it needs both `kubectl` and `bash`. `registry.k8s.io/kubectl` is distroless (no shell — `nc`, `bash /dev/tcp` all fail).

If a step needs shell features (`mkdir`, redirection, `jq`/`awk` parsing, heredocs), do **not** assume a vendor CLI image has `/bin/sh`. Third-party tool images are often distroless. Either:

- use the organization-owned `ghcr.io/projectbluefin/lab-runner:latest` image,
- run the binary directly with `container.command`/`args` and avoid shell syntax entirely, or
- switch to a shell-capable base image (`cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795`, `cgr.dev/chainguard/kubectl:latest-dev`) and use only the tools that image already carries — installing or fetching a CLI inside the step is a banned runtime install (see [`gitops-argocd/image-policy.md`](../gitops-argocd/image-policy.md)).

If an upstream FSDK container rebuild lags and a mutable tag's tool list regresses, do **not** paper over it with an inline bootstrap/fallback wrapper (`command -v oras || curl ...`) — a runtime download is an ungoverned, offline-fragile dependency banned by `image-policy.md`. Pin the image by digest at a known-good build, or move the step to an image that already carries the tool, and fix the image contents at build time.

A runtime `/bin/sh: not found` or missing-coreutils failure from a CLI image usually means the image is distroless, not that the WorkflowTemplate syntax is wrong.

### 5. Always use `onExit` or `hooks` for teardown

Every pipeline that provisions a VM must have a guaranteed teardown. When the pipeline is run directly as a workflow, root-level `spec.onExit` executes:

```yaml
spec:
  entrypoint: pipeline
  onExit: cleanup     # runs on success, failure, and error
  templates:
  - name: cleanup
    steps:
    - - name: teardown
        templateRef:
          name: teardown-vm
          template: teardown-vm
        arguments:
          parameters:
          - name: vm-name
            value: "{{workflow.parameters.vm-name}}"
```

#### The `templateRef` Trap: Use Step-Level `hooks` for Invoked Pipelines
If a pipeline is invoked as a task via `templateRef` from another WorkflowTemplate (such as `image-poller`), **the root-level `spec.onExit` of the called template is completely ignored**. If a test step fails, subsequent sequential steps (like an explicit `teardown` step) are skipped, leaving the VM orphaned.

To guarantee teardown in all entrypoints (direct and via `templateRef`), define a step-level lifecycle hook using `hooks.exit` on the test execution task itself:

```yaml
    - - name: run-tests
        templateRef:
          name: run-gnome-tests
          template: run-gnome-tests
        arguments:
          parameters:
          - name: vm-ip
            value: "{{steps.provision.outputs.parameters.vm-ip}}"
          # ...
        hooks:
          exit:
            templateRef:
              name: teardown-vm
              template: teardown-vm
            arguments:
              parameters:
              - name: vm-name
                value: "{{inputs.parameters.vm-name}}"
              - name: namespace
                value: "{{inputs.parameters.namespace}}"
```

Step-level hooks are fully supported on DAG tasks and steps inside `WorkflowTemplates`, executing the teardown template immediately on step exit regardless of whether the step succeeded, failed, or timed out.

### 6. Resource limits — required on all script/container templates

Every pod-running template needs explicit resource requests and limits. Reference values from /AGENTS.md:

```yaml
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 512Mi
```

### 7. Conditional chains: use `dag` + `depends` instead of repeating `when`

When multiple sequential steps are all guarded by the same `when` condition, convert from `steps` to `dag` and put the `when` only on the first task. Subsequent tasks use `depends: "prior.Succeeded"` — if the first task is Skipped, downstream tasks are automatically Omitted:

```yaml
# ✗ VERBOSE — same when condition repeated on every step
steps:
  - - name: check
      template: check-disk
  - - name: pull
      when: "{{steps.check.outputs.result}} != exists"
      template: pull-image
  - - name: build
      when: "{{steps.check.outputs.result}} != exists"  # redundant
      template: build-image
  - - name: configure
      when: "{{steps.check.outputs.result}} != exists"  # redundant
      template: configure-disk

# ✅ CLEAN — one when, cascade-omit via depends chain
dag:
  tasks:
    - name: check
      template: check-disk
    - name: pull
      depends: "check.Succeeded"
      when: "{{tasks.check.outputs.result}} != exists"
      template: pull-image
    - name: build
      depends: "pull.Succeeded"   # Omitted if pull was Skipped
      template: build-image
    - name: configure
      depends: "build.Succeeded"  # Omitted if build was Omitted
      template: configure-disk
```

Argo DAG semantics: a task with `depends: "X.Succeeded"` is **Omitted** (not an error) when X is Skipped. The overall DAG succeeds if all non-Omitted tasks succeeded.

**Optional upstream:** when a task has its own `when` guard and a downstream task must run regardless of whether the upstream was skipped, use OR:

```yaml
- name: run-system
  depends: "(run-software.Succeeded || run-software.Skipped)"
  when: "'{{workflow.parameters.suites}}' =~ 'system'"
```

This fires `run-system` whether `run-software` succeeded or was skipped by its own `when` condition.

#### BuildStream workflows: remote execution is mandatory; cache is not distribution

BuildStream workflows in this lab must run against the BuildBarn remote-execution grid. A workflow that only mounts the shared cache ConfigMap or points at remote artifact servers is not distributed and is not a valid operational fallback.

Evidence required before calling a BuildStream run distributed:

1. Generated BuildStream config contains `projects.<name>.remote-execution` for the project being built.
2. BuildStream startup logs show a Remote Execution Configuration loaded (frontend address, instance name, and platform properties).
3. BuildBarn workers show current action activity for the run (scheduled/executing actions matching the BuildStream platform properties).

Practical config generation: mount the shared `buildstream-remote-cache` ConfigMap at `/etc/buildstream`, copy `buildstream.conf` into a temp file, and append a per-project override block. The override must include the `remote-execution` project block pointing at the BuildBarn frontend; artifact/cache server blocks alone are insufficient. Point artifact writes at `grpc://frontend.buildbarn.svc.cluster.local:8980` and list upstream read-only cache servers (`https://gbm.gnome.org:11003`, `https://cache.freedesktop-sdk.io:11001`, `https://cache.projectbluefin.io:11001`) for fallback reads. The current BuildStream image used by these workflows does not accept the legacy `remoteasset:` block, so the override omits it.

When the project uses upstream `gnome-build-meta`/`freedesktop-sdk` junctions, mirror their patch queues into the checkout before the build so the cache keys match the upstream caches instead of diverging on local patch-set differences. Junction refs can be Git-describe strings rather than remote names; fetch the trailing full commit ID after `-g` and check out `FETCH_HEAD`, rather than fetching the full descriptive ref. This is the pattern used by `dakota-build-pipeline`, `cosmic-build-pipeline`, and `bluefin-server-build-pipeline`.

If any of the three evidence items above are missing, stop and fix the config before running. Do not proceed with cache-only or local-driver execution as a normal mode.

### 7b. Queueing and deduplication: gate the template, not just the workflow

Heavy VM and build workflows should be admitted through a semaphore or a deduplication guard before they fan out. In this repo, `manifests/workflow-semaphores.yaml` defines cluster-wide semaphores for the `qa-vm-fleet`, `containerdisk-build`, and `bst-build` lanes, and the heavy templates (`bluefin-qa-pipeline`, `dakota-qa-pipeline`, `image-poller`, `digest-watch`, and `dakota-build-pipeline`) use that admission path to stop duplicate or overlapping runs.

Important: workflow-level synchronization is not enough when the caller uses `workflowTemplateRef` or `templateRef` to dispatch a different WorkflowTemplate. Argo Workflows resolves those calls as separate template invocations, so the lock must live on the called template or the shared admission path. Apply the semaphore at the template that actually does the expensive work, not on the parent workflow wrapper.

```yaml
spec:
  templates:
    - name: heavy-work
      synchronization:
        semaphore:
          configMapKeyRef:
            name: workflow-semaphores
            key: qa-vm-fleet
```

The live pattern in this repo is to place the semaphore on the heavy child template and keep the parent workflow thin; the parent simply passes parameters and exits. This prevents poll bursts from generating unbounded VM fleets and starving node memory requests.

### 8. File names must match `metadata.name`

WorkflowTemplate file names in `argo/workflow-templates/` must match the resource's `metadata.name`. Divergence (e.g. `provision-vm.yaml` containing `name: provision-containerdisk-vm`) confuses ArgoCD tracking and grep-based navigation:

```
# ✗ WRONG — file name diverged from resource name
argo/workflow-templates/provision-vm.yaml  →  metadata.name: provision-containerdisk-vm

# ✅ CORRECT — file name matches resource name
argo/workflow-templates/provision-containerdisk-vm.yaml  →  metadata.name: provision-containerdisk-vm
```

ArgoCD tracks by GVK + resource name, not filename. A rename is safe — just git mv and push.

### 8b. Renaming or consolidating templates: lint order matters

When a template is renamed, merged, or deleted, standalone submit Workflows that reference the new name will fail `argo lint` until the live server has the new template. Validate in this order:

1. Offline lint the WorkflowTemplate directory first (cross-file refs resolve without a server):
   ```bash
   argo lint --offline argo/workflow-templates/
   ```
2. Push to `main` and let ArgoCD sync, or force sync with a port-forward:
   ```bash
   kubectl port-forward svc/argocd-server -n argocd 18080:443 &
   argocd app sync lab
   ```
3. Verify the live template exists before resubmitting dependent workflows:
   ```bash
   argo-mcp-get_workflow_template name=<new-template> namespace=argo
   # or
   kubectl get workflowtemplate -n argo <new-template>
   ```
4. Only then run the full lint, including standalone submit Workflows:
   ```bash
   just lint
   ```

### 9. Dead templates: prune promptly, don't leave DEPRECATED annotations

When a WorkflowTemplate is superseded:
1. Delete the file from `argo/workflow-templates/` in the same PR that removes the dependency
2. Automated sync with `prune: true` will remove it on the next ArgoCD cycle, but a manual `argocd app sync` without `--prune` will report "requires pruning" and leave the old template in the cluster. To prune immediately:
   ```bash
   argocd app sync lab --prune
   ```
3. Do not leave templates with `DEPRECATED` annotations in git — they accumulate and confuse agents

One-shot bootstrap templates (`install-*`, `setup-*`) should not persist indefinitely
in the cluster. If they have no git backing, `kubectl delete workflowtemplate -n argo
<name>` is safe since ArgoCD won't recreate what isn't in git.

Two CronWorkflows at the same schedule covering overlapping namespaces → consolidate into one. Check `kubectl get cronworkflows -n argo` before adding a new cleanup job.

### 11. Templates snapshot at submit time — always sync before resubmit

Argo snapshots the full WorkflowTemplate body into the Workflow object at submit time.
A workflow submitted before ArgoCD synced a fix will run the **old** template, even if
the live cluster template has since been updated.

**Always verify ArgoCD has synced before resubmitting after a template fix:**

```bash
# Confirm revision on cluster matches your push
just argocd-status   # check both apps show Synced + Healthy

# Then verify the live template has your change
argo-mcp-get_workflow_template name=<template> namespace=argo
# grep for the specific changed value

# Only then submit
```

If you report a fix is deployed without verifying the live template, you will waste the
next run on the same bug. Verification is not optional.

#### BuildStream RE `No workers exist` means queue-key mismatch first, not worker outage

When BuildStream logs show:

`FAILED_PRECONDITION: No workers exist ... platform {"properties":[{"name":"ISA","value":"x86-64"},{"name":"OSFamily","value":"linux"}]}`

check Buildbarn worker platform registration before changing workflow behavior:

1. Confirm workers are actually running (`kubectl get pods -n buildbarn`).
2. Check `manifests/buildbarn-config.yaml` `worker.jsonnet` runner `platform.properties`.
3. Ensure worker properties match BuildStream action properties (`ISA=x86-64`, `OSFamily=linux`).
4. Do not disable `remote-execution` or switch to a local driver as a workaround; solve the queue-key mismatch while keeping the mandatory RE path intact.

#### BuildStream CAS upload failures: fail fast, do not fall back to local-only

**Symptom:** Dakota logs show `Unable to upload N blobs to remote CAS` during bootstrap fetch/checkouts,
usually at 15-30+ minute mark when very large trees (gcc-stage1, freedesktop-sdk) trigger multi-thousand-blob
batches that exceed gRPC message size limits (default 4 MiB in bazel-remote).

**Root cause:** buildbox-casd batches BuildStream digests into remote CAS `BatchUpdateBlobs` gRPC calls.
Very large staged trees (freedesktop-sdk bootstrap) generate 37K+ digests in a single call, exceeding
the server's `MaxRecvMsgSize` ceiling. Neither bazel-remote nor Buildbarn frontend expose CLI flags to override this.

**Policy:** This is a remote-execution infrastructure failure. Do **not** switch the Dakota lane to pod-local cache-only execution as a routine fix. Cache-only or local-driver BuildStream behavior is a failed operational state and must fail fast. The correct remediation is to fix the RE/CAS infrastructure (frontend `MaxRecvMsgSize`, batching limits, or worker configuration), not to mask it by moving execution local.

**Response:** Stop the workflow, repair the USB4 link, BuildBarn service, or
worker configuration, and resubmit after the gate is healthy. Do not create a
pod-local diagnostic lane in a tracked workflow or use it to warm caches.

```bash
# Lint workflow-templates (offline, cross-file refs resolve)
argo lint --offline argo/workflow-templates/

# Lint bootstrap templates
argo lint --offline argo/bootstrap/

# Lint standalone submit Workflows (online, needs live server)
argo lint argo/bluefin-smoke-test.yaml
```

Or use the convenience wrapper: `just lint`

### 12. ArgoCD ownership — never apply manually

`argo/workflow-templates/` is managed by the `lab` ArgoCD Application with `prune: true` and `selfHeal: true`. Manual `kubectl apply` or `argo create workflow-template` for templates in this directory is forbidden — ArgoCD will overwrite or conflict.

`argo/bootstrap/` is **not** ArgoCD managed. Apply manually once:
```bash
kubectl apply -f argo/bootstrap/ -n argo
```

### 13. TTL and podGC — always set

Prevent accumulation of completed workflow pods:

```yaml
spec:
  podGC:
    strategy: OnWorkflowSuccess   # delete pods on success; keep on failure for debugging
  ttlStrategy:
    secondsAfterCompletion: 86400  # 24h for successful runs
    secondsAfterFailure: 604800    # 7d for failed runs (matches controller configmap)
```

## When to Use

- Creating or changing an Argo WorkflowTemplate, Workflow, or CronWorkflow.
- Adding labels, parameters, task references, resource limits, or lifecycle
  policy to a workflow.

## When NOT to Use

- ArgoCD Application ownership or sync policy changes; use the GitOps skill.
- KubeVirt VM design or test-suite behavior changes; use the corresponding
  KubeVirt or test-authoring skill.

## Common Rationalizations

| Rationalization | Reality |
| --- | --- |
| "The referenced template's labels will label the CronWorkflow run." | A DAG `templateRef` is not a child Workflow; put selection labels on the CronWorkflow's `spec.workflowMetadata`. |
| "The template linted, so it is live." | GitOps changes must be reconciled and the live template verified before submitting a run. |

## Red Flags

- A reconciler selects labels that exist only on a referenced template while a
  CronWorkflow creates the parent Workflow inline.
- A workflow task relies on inherited parameters or emits a combined suite
  result as though it were per-suite evidence.
- A GitOps-managed workflow was applied manually.

## Verification

- [ ] `just lint` passes.
- [ ] Every top-level Workflow or CronWorkflow selected by an external
  reconciler has the required label on the created Workflow metadata.
- [ ] ArgoCD has reconciled the tracked manifest, and the live template was
  verified before resubmission.
