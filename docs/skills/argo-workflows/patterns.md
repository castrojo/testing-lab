---
name: argo-patterns
description: >
  Recurring Argo Workflows patterns for QA, publishing, and scheduling.
---

# Argo Workflows Common Patterns

### 14. Decoupling slow build steps from test pipelines (image-sync pattern)

Any pipeline step that conditionally runs a slow build (compilation, disk conversion)
belongs in a **separate CronWorkflow**, not inline in the test pipeline. The test pipeline
asserts the artifact exists and fails fast — it never triggers a rebuild.

**Two-component design:**

```
[digest-watch CronWorkflow, every 5 min]
  step 1 (skopeo): GET current GHCR image digest (authenticated via github-token secret)
  step 2 (curl → k8s API): GET stored digest from ConfigMap containerdisk-source-digests
  match?    → exit 0 (skip)
  mismatch? → PATCH ConfigMap with new digest (claim it, create if 404)
              POST Workflow JSON to k8s API (async build)

[test pipeline (bluefin-qa-pipeline)]
  assert-cd: skopeo inspect Zot → tag exists? → proceed
                                → missing?  → exit 1 "containerdisk not ready"
```

**Rules:**
- Digest watch uses `quay.io/skopeo/stable@sha256:c7d3c512612f52805023cd38351081dad7e2729fc13d14b701e47c7c8bdd6615` (has skopeo + curl, no kubectl needed):
  ```bash
  # Authenticated digest fetch — works for all GHCR images (public + org-restricted)
  LIVE_DIGEST=$(skopeo inspect \
    --no-tags \
    --format '{{.Digest}}' \
    --creds "_token:${GITHUB_TOKEN}" \
    "docker://${IMAGE}:${IMAGE_TAG}" 2>/dev/null)
  ```
  Anonymous GHCR token API returns a 60-char non-JWT token that produces 404 on manifest
  requests — do NOT use the anonymous token endpoint. Use PAT via `--creds "_token:PAT"`.
- `quay.io/skopeo/stable@sha256:c7d3c512612f52805023cd38351081dad7e2729fc13d14b701e47c7c8bdd6615` does **not** include `python3` or `jq`. Keep digest comparison
  shell-only (`tr`/`sed`) or install tooling explicitly; otherwise stored digest reads collapse to
  empty and every poll cycle submits duplicate `build-cd-sync-*` workflows.
- Use in-cluster k8s API (SA token at `/var/run/secrets/kubernetes.io/serviceaccount/`)
  with `curl` for all ConfigMap and Workflow CRUD — no kubectl image needed.
- **HTTP status detection trap**: `curl -sf -w "%{http_code}" ... || echo "000"` appends
  "000" to curl's stdout output when curl fails. Use a tmpfile instead:
  ```bash
  HTTP_CODE_FILE=$(mktemp)
  curl -s -w "%{http_code}" -o /dev/null ... > "${HTTP_CODE_FILE}" || true
  HTTP=$(cat "${HTTP_CODE_FILE}"); rm -f "${HTTP_CODE_FILE}"
  ```
- The ConfigMap (`containerdisk-source-digests`) stores **GHCR source digests**, not Zot
  containerdisk digests — the two images are different (source bootc OCI vs qcow2 OCI containerDisk)
- ConfigMap is patched by the workflow, NOT managed by ArgoCD. Do not put it in `manifests/`.
  Create it in the first workflow run via POST if PATCH returns 404.
- Submitting a build via k8s API (no extra image dependency):
  ```bash
  curl -sf --cacert "${CACERT}" \
    -H "Authorization: Bearer ${SA_TOKEN}" \
    -H "Content-Type: application/json" \
    -X POST \
    "${KS}/apis/argoproj.io/v1alpha1/namespaces/argo/workflows" \
    -d '{"apiVersion":"argoproj.io/v1alpha1","kind":"Workflow","metadata":{"generateName":"build-cd-sync-testing-","namespace":"argo"},"spec":{"workflowTemplateRef":{"name":"build-containerdisk"},"arguments":{"parameters":[{"name":"image","value":"..."}]}}}'
  ```
- `assert-cd` in the test pipeline uses the existing `build-containerdisk/check` template
  but must **exit 1 on missing**, not just output `"missing"` (the original `check` template
  is non-failing — write a new `assert` template that calls skopeo and fails on empty result)

**Why ConfigMap over Zot annotation:**
- Zot annotations require `oras` tooling to set post-push; ConfigMap needs only `curl`
- The ConfigMap stores the *source* digest, not the containerdisk digest — conceptually different

### 15. VM concurrency — k8s native scheduling (no semaphores)

VM concurrency is managed by the **k8s scheduler via virt-launcher pod memory requests**, not Argo semaphores. When a node has insufficient RAM, the virt-launcher pod stays Pending. When a VM finishes, resources free up and the scheduler picks the next Pending pod. FIFO ordering follows workflow creation timestamp.

**Do not add `spec.synchronization.semaphores` to VM pipeline specs.** The semaphore approach was removed because:
- Slots were held at workflow scope, not VM-live scope — a 3h pipeline held a slot during build/assert/teardown, not just while the VM was running
- Build workflows (no VM) held VM slots, starving actual test workflows
- The slot count was a manually-maintained number that drifted from actual hardware

**What to do instead:** ensure VM specs have explicit memory requests so the scheduler has accurate data:
```yaml
domain:
  memory:
    guest: "{{inputs.parameters.vm-memory}}"   # KubeVirt sets virt-launcher request from this
```

**All pipelines still need `activeDeadlineSeconds`** so stuck VMs self-evict:
```yaml
activeDeadlineSeconds: 3600   # 1h for containerdisk, 7200 for knuckle
```

**VMs float to any KubeVirt-capable node** — no `nodeSelector: kubernetes.io/hostname: ghost` in VM specs. The registry-mirror-config DaemonSet writes the Zot HTTP registry config to all nodes.

### 15b. Semaphore topology: hold the key at one level, cap every fan-out

Two rules govern every ConfigMap-backed semaphore in `manifests/workflow-semaphores.yaml`.
`scripts/check_semaphore_topology.py` enforces both in `just lint`.

**Rule 1 — declare the key on the leaf that consumes the resource, never on
`spec.synchronization`.** A workflow-level semaphore is held for the *entire*
run, including the orchestration time before and after the real work. If a
parent holds `ghost-container-qa` while its `test-lane` children also need it,
the parent starves its own children and nothing can ever make progress:
the holder is waiting on the resource it is holding. `ghost-container-qa` is
declared exactly once, on `run-container-tests/run-container-tests`.

**Rule 2 — `spec.parallelism` does NOT travel through `templateRef`.** This is
the trap. Only a *spec-level* `workflowTemplateRef` inherits workflow-level
fields (`parallelism`, `activeDeadlineSeconds`, `workflowMetadata`). A
`templateRef` inside a `dag.tasks[]`/`steps[]` entry imports the single named
template and nothing else.

```yaml
# pr-poller's inline pr-pipeline and image-poller both do this:
- name: qa-bluefin
  templateRef:
    name: bluefin-qa-pipeline   # spec.parallelism: 2 is SILENTLY DROPPED
    template: pipeline
```

So `bluefin-qa-pipeline`'s `parallelism: 2` protected only direct submissions.
Poller-dispatched runs fanned out all five `withItems` lanes at once and a
single workflow held 5 of the 6 `ghost-container-qa` slots. Fix: put
`parallelism` on the **template** that fans out, where it survives `templateRef`:

```yaml
templates:
  - name: pipeline
    parallelism: 2          # survives templateRef; keep it < the semaphore limit
    dag:
      tasks:
        - name: test-lane
          withItems: [smoke, common, developer, software, system]
          templateRef: { name: run-container-tests, template: run-container-tests }
```

Invariant to preserve: `per-template parallelism < semaphore limit`, so at
least two distinct workflows can always progress and no single pipeline can
monopolise the runner. Raising the semaphore limit is never a fix on its own —
it only moves the threshold.

### 15a. Locking one DAG build task

`synchronization` is a template-level field; it is not valid on an individual
`dag.tasks[]` entry. When one task must share a cross-workflow semaphore with
another builder, invoke a local wrapper template from the DAG and put the
semaphore on that wrapper:

```yaml
tasks:
  - name: build
    template: serialized-build
templates:
  - name: serialized-build
    synchronization:
      semaphores:
        - configMapKeyRef:
            name: workflow-semaphores
            key: migration-containerdisk-build
    steps:
      - - name: invoke-builder
          templateRef:
            name: shared-builder
            template: build
          arguments:
            parameters:
              - name: image
                value: "{{workflow.parameters.image}}"
```

This keeps the lock around only the build task while preserving the
cross-WorkflowTemplate `templateRef`. For diagnostic collection, include all
terminal upstream states when appropriate:
`(tests.Succeeded || tests.Failed || tests.Errored)`.

### 16. GitHub Contents API or Standalone Git Push-back — Prefer Standalone Python for Complex Files

When a workflow pod needs to push a simple file to a GitHub repo, use `curl` + `jq` inside the bash script (Contents API).

However, for complex updates (such as parsing BDD/behave test results, merging with historical runs, and capping the history), **never use inline python or complex inline bash blocks**. Instead, extract the logic into a **standalone Python script** inside the repository (e.g. `scripts/publish_test_results.py`), clone the repository dynamically within the container using `GITHUB_TOKEN`, and run the script locally to perform a standard git transaction (`git clone` → update → `git commit` → `git push`).

**Pattern for Standalone Git Push-back:**
```yaml
        if [[ -n "${GITHUB_TOKEN:-}" ]]; then
          echo "Publishing test results back to lab repository..." >&2
          rm -rf /tmp/lab-code
          git clone --depth 1 "https://x-access-token:${GITHUB_TOKEN}@github.com/projectbluefin/lab.git" /tmp/lab-code
          python3 /tmp/lab-code/scripts/publish_test_results.py /tmp/results/results.json "${IMG_SLUG}" "${SUITE}" "{{workflow.name}}" "${GITHUB_TOKEN}" || echo "Warning: failed to publish test results" >&2
        else
          echo "No GITHUB_TOKEN - skipping test results publication" >&2
        fi
```

#### KDE GUI runner: persist guest artifacts before re-raising failures

KDE GUI tests run through a WebDriver service inside the VM, so screenshots and
`faillog_*` diagnostics are written in the guest session. Set the shared
results directory in both environments, copy it back with `scp` after Behave
returns (including non-zero returns), archive each failure directory with
`python3 -m tarfile` (the runner has no `tar`), then copy the complete directory
to the runner's `/var/mnt/ghost-data/test-results` hostPath. Surface archive
failures after persistence and publication instead of silently dropping them;
re-raise the saved Behave status only after that cleanup.

Require the GitHub credential, screenshot, and ORAS CLI. Publish the in-guest
screenshot with the ORAS CLI's `path:media-type` syntax (verified against
Context7 `/oras-project/oras`); missing inputs or a failed upload must fail the
runner after artifact persistence:

```bash
oras push "${SCREENSHOT_IMAGE}:${PUSH_TAG}" \
  --annotation "io.github.projectbluefin.caller_repo=projectbluefin/lab" \
  "${SHOT}:image/png"
```

Use guest-side screenshots for KubeVirt. `virt-launcher` does not expose a
QEMU monitor, so QEMU-level screendump helpers are not a valid fallback.

**Contents API Pattern (for simple single-file writes, verified against Context7 `/websites/github_en_rest`):**
```bash
# GET current file sha (required for updates)
CURRENT=$(curl -sf \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/OWNER/REPO/contents/PATH/file.json" || echo "{}")
FILE_SHA=$(echo "$CURRENT" | jq -r '.sha // empty')

# Build payload with jq — no Python, no heredocs
CONTENT=$(echo "$PAYLOAD_OBJ" | base64 -w0)
BODY=$(jq -nc \
  --arg msg "commit message" \
  --arg content "$CONTENT" \
  --arg sha "$FILE_SHA" \
  'if $sha != "" then {message:$msg,content:$content,sha:$sha} else {message:$msg,content:$content} end')

# PUT — sha required for updates, omit for new files
HTTP_CODE=$(curl -sf -w "%{http_code}" -o /tmp/response.json \
  -X PUT \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  -d "$BODY" "https://api.github.com/repos/OWNER/REPO/contents/PATH/file.json")
```

Key rules:
- `sha` field required when updating an existing file; omit for new files (404 on GET = new file)
- `content` must be base64 encoded; use `base64 -w0` (no line wraps)
- `X-GitHub-Api-Version: 2022-11-28` header required by current GitHub API
- Retain output through Argo logs/artifacts or a workflow PVC — never a
  root-backed hostPath
- Concurrent pipeline exits conflict on SHA → last writer wins; 409 = silent skip. Acceptable for metrics files.

#### Container-only QA runner: publish digest-pinned results

When starting the nested target with Podman, pass `/sbin/init` explicitly after the
image reference. Some bootc OCI images have an empty image `Cmd`; relying on
`--systemd=always` alone then makes crun fail with `cannot find `` in $PATH` before
systemd starts.

`run-container-tests` runs inside a privileged `quay.io/podman/stable:latest` container, which does not include `git` or `skopeo`. When publishing BDD evidence back to the lab repo:

1. Install tooling if it is missing: `command -v skopeo >/dev/null || dnf install -y skopeo` and `command -v git >/dev/null || dnf install -y git-core`.
2. Resolve the digest of `{{inputs.parameters.image}}:{{inputs.parameters.image-tag}}` with `skopeo inspect --no-tags --format '{{.Digest}}' "docker://${IMAGE}"`. Treat a missing digest as a non-fatal warning.
3. Compute the image slug as `IMG_SLUG="${VARIANT}-${IMAGE_TAG}"` so the result file name matches the contract used by `run-gnome-tests` (e.g. `bluefin-stable-smoke.json`).
4. Treat the git clone and `publish_test_results.py` as required evidence
   publication. Their failures must fail the test workflow after cleanup.
5. Pass the resolved digest as the optional sixth positional argument to `publish_test_results.py` so the collector can match QA evidence to the currently published image digest.


**Why no inline Python or heredocs (root cause):** YAML `source: |` literal blocks use indentation to determine block extent. Any line at column 0 (including unindented `python3 -c "...\nimport json\n..."` continuation lines, or heredoc bodies like `<<'EOF'\nimport json\n`) terminates the block — YAML treats those lines as new top-level keys. The `yaml: could not find expected ':'` error is the symptom. Fix: use `jq` one-liners, keep everything on the same indented line, or `--rawfile` to read from a pre-staged file.

**onExit dashboard update pattern (bluefin-qa-pipeline + dakota-qa-pipeline):**
```yaml
- name: update-factory-stats
  script:
    image: quay.io/fedora/fedora:latest
    command: [bash]
    env:
      - name: GITHUB_TOKEN
        valueFrom:
          secretKeyRef:
            name: github-token
            key: token
    source: |
      set -euo pipefail
      API_URL="https://api.github.com/repos/projectbluefin/lab/contents/docs/data/factory-stats.json"
      # Fetch JSON file + SHA
      CURRENT=$(curl -sf -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github+json" "${API_URL}" || echo "{}")
      FILE_SHA=$(echo "$CURRENT" | jq -r '.sha // empty')
      [[ -z "$FILE_SHA" ]] && echo "No SHA — skipping" && exit 0
      STATS=$(echo "$CURRENT" | jq -r '.content // ""' | tr -d '\n' | base64 -d \
        | jq '.')
      # Build run entry with jq — no Python, no heredocs
      NEW_RUN=$(jq -nc --arg id "{{workflow.name}}" --arg overall "pass_or_fail" \
        '{id:$id,overall:$overall,...}')
      UPDATED=$(echo "$STATS" | jq -c --argjson run "$NEW_RUN" --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '.recent_runs = ([$run] + (.recent_runs // []) | .[:15]) | ._meta.generated = $now')
      BODY=$(jq -nc --arg msg "chore: update dashboard run data" \
        --arg content "$(echo "$UPDATED" | base64 -w0)" --arg sha "$FILE_SHA" \
        '{message:$msg,content:$content,sha:$sha}')
      curl -sf -w "%{http_code}" -o /dev/null -X PUT \
        -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github+json" \
        -H "Content-Type: application/json" \
        -d "$BODY" "${API_URL}"
```
The real implementation in `bluefin-qa-pipeline.yaml` also fetches per-suite result files into `/tmp/suite-scores/` and merges them via `jq --argjson` one-liners before building `NEW_RUN`.

### 17. CronWorkflow — `schedules` not `schedule`

CronWorkflow uses `schedules` (plural array), not `schedule` (singular string). The singular field does not exist in the CRD schema — ArgoCD's ServerSideApply validation will reject it.

```yaml
# ✗ WRONG — rejected by ArgoCD schema validation
spec:
  schedule: "0 * * * *"

# ✅ CORRECT
spec:
  schedules:
    - "0 * * * *"
```

Verified against Context7 `/argoproj/argo-workflows` CronWorkflow spec docs.

CronWorkflows also cannot be invoked via `workflowTemplateRef` — if you need a CronWorkflow to be submittable manually, extract its logic into a WorkflowTemplate and have the CronWorkflow reference it with `workflowTemplateRef`.

### 17a. Authenticated digest-preserving registry publication

For a scheduled Zot-to-GHCR publisher, keep the reusable logic in a
WorkflowTemplate and point the CronWorkflow at it. Mount an operator-managed
`kubernetes.io/dockerconfigjson` Secret as an auth file; do not expose registry
credentials through parameters, command arguments, stdout, or shell tracing.

Resolve the source tag to a digest, copy `source@digest`, and inspect the
destination tag with the same auth file. A lane succeeds only when the
destination digest exactly matches the source digest. Put a semaphore on the
entrypoint DAG so separate publication workflows serialize while independent
image lanes within one workflow can still run in parallel.

Use terminal-status dependencies for the result task:

```yaml
depends: >-
  (lane-a.Succeeded || lane-a.Failed || lane-a.Errored) &&
  (lane-b.Succeeded || lane-b.Failed || lane-b.Errored)
```

Pass each `{{tasks.<lane>.status}}` to the result task and fail it unless every
lane succeeded. Bound each lane with `retryStrategy.limit` and backoff.

After the image copy, use `oras discover` against the source digest. If
referrers exist, recursively copy them with the destination registry auth file.
Log explicit `none present` or `discovery unavailable` states as non-fatal;
failure to copy referrers that were discovered is a lane failure. Verify the
destination digest after this optional evidence step.

Do not rely on `lab-runner:latest` for registry clients. Use the pinned Skopeo
image and bootstrap a pinned ORAS release with its included `curl` and `tar`;
if that unauthenticated bootstrap is unavailable, log the missing referrer
capability explicitly and continue with digest verification.

```bash
# Source: Context7 /oras-project/oras
oras discover --plain-http --format json --depth 1 "${SOURCE}@${DIGEST}"
oras cp --recursive --from-plain-http \
  --to-registry-config /auth/config.json --no-tty \
  "${SOURCE}@${DIGEST}" "${DESTINATION}:testing"
```

For durable run history, attach a root `onExit` template and pass
`{{workflow.status}}`, `{{workflow.name}}`, and
`{{workflow.creationTimestamp.RFC3339}}` through environment variables. Build a
compact record with `jq`, then invoke the validated publisher CLI. Do not append
`|| true`: persistence is part of the workflow contract, so an exit-handler
failure must remain visible and make an otherwise successful run fail. Never
put the GitHub token in a clone URL or command argument; expose it only as
`GITHUB_TOKEN` from a Secret and let the CLI's `GIT_ASKPASS` path consume it.

> Source: Context7 `/argoproj/argo-workflows` exit-handler and global-variable
> documentation. Exit handlers always run and receive the terminal workflow
> status; `workflow.creationTimestamp.RFC3339` is available in the exit handler.

### 18. `when` condition trap — never reference a Skipped task's outputs

**Verified against Context7 `/argoproj/argo-workflows` enhanced-depends-logic docs:**
> "If a downstream task references outputs from a task that was Skipped or Omitted,
> those references will resolve to empty strings."

So `'{{tasks.check.outputs.result}}' != 'exists'` becomes `'' != 'exists'` = `true` when
the upstream is Skipped. In theory the build should run. In practice (Argo v4.0.5), we
observed the downstream task never being scheduled at all — the controller logs show
`"was unable to obtain the node"` for it and the workflow stalls with only 2 nodes.

The safe, version-independent fix: **never put a `when` guard on the task that owns the
gate output. Move the bypass inside the script.**

**Example of the fragile pattern:**
```yaml
- name: check
  when: "'{{inputs.parameters.force}}' != 'true'"   # Skipped when force=true
  template: check

- name: build
  depends: "(check.Succeeded || check.Skipped)"
  when: "'{{tasks.check.outputs.result}}' != 'exists'"  # resolves to '' when Skipped → unpredictable
  template: build
```

**The robust fix:**
```yaml
- name: check           # always runs — no 'when' on this task
  template: check       # script handles force=true internally

- name: build
  depends: "check.Succeeded"
  when: "'{{tasks.check.outputs.result}}' != 'exists'"   # ✅ always defined
  template: build
```

Inside the `check` script:
```bash
if [[ "{{inputs.parameters.force}}" == "true" ]]; then
  echo "missing"   # short-circuit — always rebuild
  exit 0
fi
# … real existence check …
```

**Rule:** If a task has a `when` guard AND downstream tasks reference its outputs,
remove the `when` guard and move the bypass into the script body.

**Symptoms of this bug:**
- Workflow shows phase `Running` but only 1–2 nodes (the DAG + the Skipped task)
- No `install-to-disk` or equivalent node ever created
- Controller logs show `"was unable to obtain the node"` for the downstream task (normal reconciliation noise)
- `force=true` workflows submitted after a digest change never actually build

### 19. `when` condition values with hyphens must be quoted or avoided

Argo's `when` expression parser (expr-lang based) treats an unquoted hyphenated
string as a subtraction expression. A condition like:

```yaml
when: "{{inputs.parameters.mode}} == cache-only"
```

expands to `cache-only == cache-only`, which fails with:

```
Value 'cache' cannot be used with the modifier '-', it is not a number
```

**Fixes:**

1. Quote the literal: `when: "{{inputs.parameters.mode}} == 'cache-only'"` works
   for values that expr-lang can parse as a single quoted string.
2. Safer: avoid hyphens in the enumerated value entirely. Use `local` instead of
   `cache-only` and quote the comparison: `when: "{{inputs.parameters.mode}} == 'local'"`.

Always lint after changing `when` expressions, then submit a test workflow to
verify the DAG branches are scheduled as expected before relying on the path in
production.

### 20. Mutex contention from stuck failed builds

The `ghost-heavy-compute` mutex (on the `install-to-disk` template) allows only one
concurrent build at a time. Failed workflows that were stopped via `shutdown: Stop` **release
the mutex**, but workflows that exit with a non-zero script error may hold the mutex until
the workflow GC TTL clears them.

**Check what holds the mutex:**
```bash
kubectl logs -n argo -l app=workflow-controller --since=2m 2>/dev/null \
  | grep -i "ghost-heavy\|mutex\|Could not acquire"
```

**Stop a workflow holding the mutex:**
```bash
kubectl patch workflow <name> -n argo -p '{"spec":{"shutdown":"Stop"}}' --type=merge
```

**Dakota lanes and the mutex:** keep the lanes separate.
- `dakota-commit-poller` → `bst-commit-poller` → `dakota-build-pipeline`
  (BuildStream publish lane) is expected to run.
- `image-poll-dakota` → `dakota-qa-pipeline` is the active container-only QA lane.
If mutex contention appears, stop stale failed workflows holding `ghost-heavy-compute`; do not
blanket-stop all Dakota build-publish runs or suspend the active QA poller.

### 20a. Dakota BuildStream publish lane output tags

`dakota-build-pipeline` exports the built `oci/bluefin.bst` artifact to the local Zot
registry. NVIDIA variants are disabled in the distributed clean-build workflow. The published
tag must match the projectbluefin/dakota image contract:

- Base variant → `<lab-ip>:30500/dakota:testing`

Do not publish these as `:latest` from the cluster lane; `:testing` is the testing-branch
stream and `:stable` is promoted separately from `main`. Keeping the cluster lane on `:testing`
prevents accidental overwrites of the stable/production stream and makes the artifact identity
obvious to downstream lab jobs.

If the template is retagged, update the dashboard fallback writable-repos list in
`src/pages/index.astro` and `src/pages/userspace.astro` to match the new repository names.

### 20aa. Report factory PR workflows through one GitHub Check Run

Factory PR validation for the repos in the poller's `AUTO_REPOS` list
(`projectbluefin/common`, `bluefin`, `bluefin-lts`, `dakota`, `knuckle`,
`testsuite`), plus any PR carrying the `test-on-lab` label, each use one native
Check Run named `testing-lab / <repository>`, owned by the existing MergeRaptor
GitHub App. Do not post PR comments or a parallel commit status for the same
result. (This automated Check Run is a different channel from the manual
reviewer comments an operator posts during PR-queue review; do not conflate the
two.)

The auth boundary is deliberate:

1. The lab uses its existing GitHub credential only to send a
   `repository_dispatch` event (`event_type: "lab-check"`) to the target
   repository.
2. The target repository's `lab-check.yml` mints a short-lived MergeRaptor
   installation token from the existing GitHub Actions org secrets.
3. MergeRaptor creates or updates the Check Run for the exact PR head SHA.

Enrollment is a **two-sided contract**: dispatching from the lab (sender) only
produces visible feedback when the target repo also ships
`.github/workflows/lab-check.yml` on its default branch (receiver). A repo added
to `AUTO_REPOS` without that receiver workflow is *half-enrolled* — the dispatch
returns HTTP 204 and the Argo QA runs, but no Check Run, comment, or error ever
appears on the PR. Adding a repo to lab PR feedback therefore means editing both
sides. As of this writing `common`, `knuckle`, and `testsuite` are dispatched to
but lack the receiver workflow, so their results are silently dropped.

Never copy the MergeRaptor private key into Kubernetes. Keep the dispatch
payload nested and bounded. Include workflow parameters, phase counts,
pod-to-node placement, node timings, and failure messages. Do not copy raw pod
logs into GitHub because retained workflow logs may contain authenticated API
output; link the private Argo workflow instead.

The PR poller must create the Argo workflow before dispatching the queued check.
If the queued dispatch fails, delete that new workflow so the next five-minute
poll retries the entire operation, then return success from that PR handler so
one GitHub API failure does not abort processing the remaining PRs in the poll
cycle. The generated workflow sends an `in_progress` update at admission and a
`completed` update from `onExit`.

### 20ab. Bound BuildStream admission before the semaphore queue

The `bst-build` semaphore limits execution to one pipeline, but a semaphore by
itself permits an unbounded list of waiting workflows. Automated callers must
also count active workflows labeled `bluefin.io/bst-workload=true` and defer
when two are already admitted: one may execute while one waits.

The generic PR poller runs at minute `0/5`; Dakota and Cosmic source pollers run
at minute `2/5` and `4/5`. This staggering makes the count-and-submit guard
deterministic for automatic traffic. Source pollers must persist a new commit
SHA only after the referenced build succeeds, so deferred or failed work is
retried.

MergeRaptor requires `checks: write`. GitHub's Checks endpoints require GitHub
App authentication; classic PATs and OAuth apps cannot update checks.

> Source: `/websites/github_en_rest` — Check runs and repository dispatch.

### 20ac. Reap superseded and closed-PR workflows in the poller

Deduping on `bluefin.io/pr-number` + `bluefin.io/pr-sha` means a new push
creates a *new* workflow, but nothing ever cancels the old one. Combined with
workflows that keep running after their PR merges, the queue accumulates runs
whose results are already worthless while each holds a `ghost-container-qa`
slot for ~20 minutes. Measured drain during one incident: PR #675 with 4
concurrent workflows for 4 SHAs, #691 with 4, #697 with 3, #724 with 4, plus 24
workflows for already-merged PRs.

`pr-poller` therefore does three things beyond dispatch:

1. **Supersede** — for every open PR, stop any in-flight workflow whose
   `pr-sha` differs from the current head. This runs *before* the dispatch cap
   and *before* the dedup guard, because superseding is about the PR's current
   head, not about whether this poll happens to dispatch.
2. **Reap** — after the open-PR passes, stop any in-flight workflow whose PR is
   no longer in the open set.
3. **Align admission with capacity** — `MAX_DISPATCH` is capped at
   `ghost-container-qa limit / pipeline parallelism`, i.e. the number of
   *workflows* the runner can execute concurrently, not the raw slot count.
   Since §15b each pipeline holds at most `parallelism` slots, so with a limit
   of 6 and `parallelism: 2` the runner runs 3 workflows at a time and
   `MAX_DISPATCH=3`. Admitting more per 5-minute poll cannot make anything
   finish sooner; it only deepens the queue. If either number changes, this one
   must be rederived.

**Cancel with `spec.shutdown: Stop`, never `kubectl delete`.** `Stop` is what
`argo stop` sets: running pods are terminated but the `onExit` handler still
executes, so `report-final` publishes a terminal `ghost-lab` status. A hard
delete skips `onExit` and strands the commit on `pending` forever.

```bash
kubectl patch workflow "${wf}" -n argo --type merge -p '{"spec":{"shutdown":"Stop"}}'
```

**Three safety rules, all load-bearing:**

- Only reap products whose open-PR enumeration completed with **zero** API
  errors. A transient GitHub failure must never be read as "every PR merged".
- Only reap products that returned **at least one** open PR. A sudden empty
  result set is far more likely to be an auth/scope regression than a mass
  merge.
- Only consider workflows carrying `bluefin.io/repository`. PR numbers collide
  across repositories, so an unlabelled workflow can never be safely
  attributed. Every label selector keyed on `pr-number` must also key on
  `bluefin.io/repository`.

Idempotency comes for free: workflows already carrying `spec.shutdown`, or
labelled `workflows.argoproj.io/completed=true`, are filtered out, so the pass
is a no-op on every subsequent 5-minute run.

### 20b. Dakota verification: use containerized QA when VM path is blocked

Dakota images are built from a composefs-oci backend that declares `bootloader = "systemd"` but
does not ship a UKI. The lab's standard VM QA path (`build-containerdisk` → `bootc install to-disk`
→ KubeVirt VM) therefore fails with `bootupd is required for ostree-based installs` because bootc
1.16.2 bails for systemd-boot ostree installs when no UKI is present. Until Dakota ships a UKI
(or bootc gains a composefs-oci install path), VM-boot verification is blocked.

WorkflowTemplate: `dakota-container-qa-pipeline`

- Runs image-level smoke checks directly inside a pod built from the target OCI image.
- Verifies Dakota identity (`/etc/os-release`), presence of key binaries (`podman`, `flatpak`,
  `gnome-shell`, `bootc`), bootc install config, and valid `bootc status` JSON.
- Requires no `bootc install`, no containerDisk, and no `provision-containerdisk-vm`.
- GUI behave suites (`smoke`/`developer` via `qecore-headless`) cannot run inside a pod because
  `qecore-headless` requires a full systemd/GDM session.

Default invocation for a fresh `dakota:testing` build:

```bash
argo submit --from workflowtemplate/dakota-container-qa-pipeline \
  -p image=<lab-ip>:30500/dakota \
  -p image-tag=testing \
  -p variant=dakota \
  -n argo --watch
```

NVIDIA image builds are disabled for this clean distributed-build path.

Keep the VM-based `dakota-qa-pipeline` suspended until a successful `build-containerdisk` run
proves the VM-boot path works.

### 22. Per-workflow ephemeral storage — volumeClaimTemplates

### 21. Native-systemd desktop QA

`run-systemd-container-tests` is the container-native desktop QA probe. An
Argo `resource` template creates a privileged target Pod with systemd as PID 1
and a Workflow owner reference; its runner executes qecore inside the target,
never directly under Argo emissary PID 1.

- Keep both target and runner scheduler-driven: no `nodeSelector`, hostPath,
  VMI, raw disk, or containerDisk.
- Use memory-backed `emptyDir` for `/run` and `emptyDir` for `/workspace`. The
  target requests `2 CPU`, `4Gi` memory, and `20Gi` ephemeral storage, with
  limits of `4 CPU`, `8Gi` memory, and `40Gi` ephemeral storage.
- Wait for systemd plus active `dbus` and `systemd-logind` before running
  qecore; print its journal and fail if that state is unavailable.
- Because mounting a new `/run` invalidates the image resolver symlink, copy
  the runner Pod's Kubernetes resolver into the target before Git or pip use.
- Target image pulls through Zot can take up to 600 seconds; size the Argo
  `resource` template timeout accordingly.
- Do not overwrite qecore's desktop session environment with fake `/home`
  runtime-bus values; the real login session D-Bus socket and bus address must
  remain intact.
- Pass test-suite inputs through a durable target file, not environment
  variables: qecore does not forward arbitrary env vars into the desktop
  session.
- Delete the owner-referenced target in the runner's EXIT trap as a prompt
  cleanup fallback.

This runner validates the OCI userspace, systemd/logind startup, resolver
repair, qecore, and GDM bootstrap. Tests requiring a bootloader, kernel,
initramfs, or physical hardware remain outside its scope.

**Not yet the production caller path:** a full smoke suite currently loses its
GNOME D-Bus/Wayland session, so use this probe only for targeted desktop
bootstrap validation until that instability is resolved.

For pipelines that need shared scratch space across steps (e.g. installer binaries, target disks),
use Argo's `volumeClaimTemplates` at the workflow spec level. Argo auto-creates the PVC at workflow
start and auto-deletes it on completion — no manual cleanup step needed.

```yaml
spec:
  volumeClaimTemplates:
    - metadata:
        name: workspace
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: local-path   # explicit non-root node mapping in GitOps
        resources:
          requests:
            storage: 30Gi

  templates:
    - name: my-step
      script:
        volumeMounts:
          - name: workspace
            mountPath: /mnt/workspace
```

**RWO PVC + KubeVirt VM co-location:** When a VM uses a `persistentVolumeClaim` volume backed
by a RWO PVC, KubeVirt automatically schedules the VM on the same node as the PVC — no explicit
`nodeSelector` needed. Source: /kubevirt/user-guide — "When using local devices or ReadWriteOnce
(RWO) PVCs, affinity rules on VMs sharing storage ensure they are scheduled on the same node."

The local-path provisioner configuration must contain an explicit non-root data
mount for every eligible node. It has no default path: a PVC on an unconfigured
node must fail provisioning rather than write to the root filesystem.

**Namespace constraint:** `volumeClaimTemplates` creates the PVC in the workflow's own namespace
(`argo`). If a VM in a different namespace (`knuckle-test`) needs a disk, create a dedicated PVC
in that namespace via a `resource:` step, and delete it in `onExit`.

```yaml
# registry-lint-ignore not needed — no image ref
- name: create-rootdisk-pvc
  resource:
    action: apply
    manifest: |
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: "{{workflow.name}}-rootdisk"
        namespace: knuckle-test
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: local-path
        resources:
          requests:
            storage: 30Gi
```

**containerDisk OCI format** (source: /kubevirt/user-guide):
```dockerfile
FROM scratch
ADD --chown=107:107 disk.raw /disk/
```
UID 107 = qemu. Required — omitting `--chown` causes VM boot failure (permission denied on disk).

### 23. Log access — Argo is sufficient, no separate stack needed

Argo Server retains all workflow pod logs for the workflow TTL period (7 days success,
30 days failure via `workflow-controller-configmap`). No separate log aggregation stack
(Loki, Promtail, etc.) is needed for a homelab CI cluster.

**Retrieve logs:**
```bash
# most recent workflow
just logs                              # alias: argo logs -n argo @latest

# specific workflow
argo logs -n argo <workflow-name>

# specific pod/container
kubectl logs -n argo <pod> -c main

# via MCP
argo-mcp-logs_workflow <workflow-name>
```

**Why a separate log stack is redundant:**
- Pod logs are already captured and served by the Argo Server
- Artifacts (`results.json`, `atspi_tree.txt`) echo to stderr — accessible via `argo logs`
- Cross-workflow queries → `argo list -n argo` then `argo logs` per workflow
- Adding Loki + Promtail duplicates storage, adds 2–3 pods, and a 10Gi PVC for no
  additional capability that `argo logs` doesn't already provide

### 23. CronWorkflow `suspend` field can survive a git removal — verify live, don't trust ArgoCD "Synced"

Removing `spec.suspend: true` from a CronWorkflow's git manifest and syncing does **not**
reliably clear the live field, even when ArgoCD reports the resource `Synced` and the sync
`operationState` says `Succeeded`. This was observed directly: after removing `suspend: true`
from 10 CronWorkflow manifests, committing, pushing, and force-syncing (`annotate
argocd.argoproj.io/refresh=hard`), ArgoCD reported all 10 as `Synced` — but `kubectl get
cronworkflow <name> -o jsonpath='{.spec.suspend}'` still returned `true` on every one of them.

**Always verify the live field directly after removing it from git — never trust the
ArgoCD sync/resource status alone for boolean fields that may have been set by a prior
apply.** If live state doesn't match git after a confirmed sync, patch it directly:

```bash
kubectl patch cronworkflow -n argo <name> --type=merge -p '{"spec":{"suspend":false}}'
```

Root cause not conclusively identified (suspected Server-Side Apply field-ownership —
a boolean field set by an earlier field manager isn't cleared just because a later
manifest omits it). Treat any boolean/scalar field removal from a CronWorkflow the same
way: confirm live state with `kubectl get -o jsonpath`, don't stop at "ArgoCD says Synced".

### 24. Digest-comparison pollers can't detect out-of-band artifact loss

`digest-watch` (and similarly-shaped pollers) only rebuild an artifact when the **upstream
source digest changes** vs a ConfigMap-stored value. They have no way to notice that the
artifact itself disappeared for an unrelated reason (disk wipe, PVC reset, registry GC)
while the upstream digest stayed the same — the poller will keep reporting "no change,
skipping" indefinitely even though the artifact is gone and every downstream consumer
(e.g. `assert-cd` in a QA pipeline) is failing.

**This happened concretely:** a ghost XFS migration wiped the local Zot registry.
`bluefin-containerdisk` was completely absent, but `ghcr.io/projectbluefin/bluefin:testing`'s
digest hadn't changed, so `digest-watch` never rebuilt it. `bluefin-qa-pipeline` would have
failed indefinitely without manual intervention.

**Recovery:** manually submit the build Workflow directly with `force=true`, bypassing the
digest comparison:
```bash
kubectl create -f - <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: manual-build-cd-<tag>-
  namespace: argo
spec:
  workflowTemplateRef:
    name: build-containerdisk
  arguments:
    parameters:
      - {name: image, value: "ghcr.io/projectbluefin/<repo>"}
      - {name: image-tag, value: "<upstream-tag>"}
      - {name: containerdisk-tag, value: "<zot-tag>"}
      - {name: force, value: "true"}
EOF
```

**Implemented:** digest-comparison pollers that gate a downstream `assert-cd`-style check
now probe the destination registry for artifact existence and force-rebuild the containerDisk
when the artifact is missing or when the upstream source digest changed. This covers disk
wipes, registry migration, and manual Zot cleanup without waiting for a separate recovery
step.

### 25. Configure registries mirror and security policies before running container builds

When running `podman build`, `bootc install`, or other image/pull operations inside a privileged Argo workflow container, you must configure any custom registries mirror files (such as `/etc/containers/registries.conf.d/bluefin-local-zot.conf` to hook up the local Zot pull-through cache) and security policy files (such as `/etc/containers/policy.json`) BEFORE executing those container operations. 

In particular, if the base image being pulled or built has a strict production signature policy built into its `/etc/containers/policy.json` (as is the case with Bluefin/Aurora production images), `bootc install` and other podman/skopeo pull tasks will reject pulling unsigned images from local registries or GHCR with exit code 125 ("Source image rejected: A signature was required, but no signature exists"). Overwriting the pod container's local `/etc/containers/policy.json` with an insecure policy (e.g. `"type": "insecureAcceptAnything"`) prevents this exit-125 failure.

This is extremely critical to understand if a workflow ever uses `hostPID: true`. If a pod using `hostPID: true` exits with failure (or is terminated/timed out), the `argoexec` process teardown signals all processes in its view — which in a host PID namespace means **every host process**, killing host daemons like `k3s`, `sshd`, and `systemd-journald` and crashing the node. Therefore, `hostPID: true` and `hostIPC: true` must NOT be used in build containers. Bypassing signature checks using `policy.json` prevents exit-125 crashes, but removing `hostPID` entirely is the primary safety guarantee.

**Correct order of execution:**
1. Configure containers-storage graphroot.
2. Write registry mirror configuration files under `/etc/containers/registries.conf.d/`.
3. Overwrite `/etc/containers/policy.json` with `insecureAcceptAnything` to bypass signature checks.
4. Run container build or install operations (e.g., `podman build --tls-verify=false -t ...` or `bootc install to-disk ...`).
5. Run container push operations (e.g., `podman push ...`).

### 26. Avoid permission denied errors on /tmp for non-root containers

If an Argo workflow container template is configured to run as a non-root user (such as `runAsUser: 1000` in `run-container-tests.yaml`), and needs to write results, temporary configurations, or scripts under `/tmp`, it can easily fail with `Permission denied` (exit code 1). This happens because `/tmp` inside the bootc rootfs image is typically owned by root with restricted permissions.

The clean, standard Kubernetes/Argo solution is to mount an `emptyDir: {}` volume on `/tmp` inside the pod container. This provides a fresh, fully-writable `/tmp` filesystem that is owned by the executing non-root user (1000) and completely isolates test execution from any image-baked `/tmp` permission constraints.

**Implementation pattern:**
```yaml
    container:
      image: "{{inputs.parameters.image}}:{{inputs.parameters.image-tag}}"
      securityContext:
        runAsUser: 1000
        runAsGroup: 1000
      volumeMounts:
        - mountPath: /tmp
          name: tmp
    volumes:
      - name: tmp
        emptyDir: {}

### 27. BuildStream Pipeline Resource Right-Sizing and Scheduler-Driven Affinities

When designing or updating BuildStream compilation pipelines (e.g. `dakota-build-pipeline` and `cosmic-build-pipeline`), right-size all step-level resource requests and limits to maximize cluster capacity and prevent scheduling bottlenecks:

- **RE Coordinator/Driver Pods**: The remote execution build driver (e.g., `bst-build-re`) only orchestrates execution, downloads metadata, and transfers sparse artifact layers; its native CPU/memory usage is minimal (~47m CPU, ~926Mi memory). Keep its resource requests right-sized at `2 CPU` and `4Gi` memory (with limits at `4 CPU` and `8Gi` memory) to prevent massive node capacity stranding.
- **Local/Serial Builder Pods**: Local compile templates (e.g. `bst-build-local`) can spike up to 15.9 CPU cores but rarely exceed ~9.6GiB of memory and ~0.36GiB of container-overlay filesystem storage (since the BuildStream artifact cache is mapped directly to a hostPath or PVC). Right-size requests to `16 CPU` and `16Gi` memory with a `10Gi` ephemeral storage request (limits: `32 CPU`, `32Gi` memory, `50Gi` ephemeral storage) to avoid stranding resources while leaving ample compiling headroom.
- **Preferred Node Affinities**: Avoid hard node pinnings (like `nodeSelector: kubernetes.io/hostname: exo-0`) on build templates. Instead, utilize a `preferredDuringSchedulingIgnoredDuringExecution` preferred node affinity targeting the primary build node (e.g., `exo-0` with weight 100) to keep cache locality warm under normal conditions, while enabling the Kubernetes scheduler to gracefully schedule build pods onto other available nodes (such as `exo-1`) when the primary is overloaded or undergoing maintenance. This fully aligns with scheduler-driven placement policies.

```
