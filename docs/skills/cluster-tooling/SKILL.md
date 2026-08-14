---
name: cluster-tooling
description: "Cluster management tools for the lab: kubectl, k3s, zot, external-secrets, and K8sGPT. Use when managing cluster state, installing cluster add-ons, configuring the OCI registry, or running cluster analysis through MCP."
metadata:
  type: reference
  context7-sources:
    - /helm/helm
    - /k3s-io/k3s
    - /project-zot/zot
    - /websites/prometheus_io
    - /external-secrets/external-secrets
    - /k8sgpt-ai/k8sgpt
    - /apache/buildstream
    - /kubernetes/website
---

# Cluster Tooling — lab

## When to Use

- Managing cluster state, infra add-ons, registry/cache services, or k8s ops runbooks.
- Debugging BuildStream cache behavior for Dakota/Cosmic/BST workflow lanes.

## When NOT to Use

- Argo WorkflowTemplate authoring details → [`argo-workflows/SKILL.md`](../argo-workflows/SKILL.md).
- KubeVirt VM provisioning/test authoring workflows → [`kubevirt-vms/SKILL.md`](../kubevirt-vms/SKILL.md) and [`test-authoring/SKILL.md`](../test-authoring/SKILL.md).

## Core Process

1. Resolve tool/library docs in Context7 first (kubectl/k3s/K8sGPT/BuildStream as needed).
2. Prefer `just` recipes, then `kubectl`/`argo` and other API-driven operations.
   Host-level work is private maintainer maintenance; never use workstation SSH
   to `ghost` or `exo-0` from the public agent path.
3. For BST lanes, configure local and upstream cache fallback in workflow configs:
   - never configure external cache credentials/keys in cluster workflows
   - set `override-project-caches: false` to allow the project's own upstream caches (for example Freedesktop SDK and GNOME OS) to be used as read-only fallbacks, preventing extremely slow, full OS recompilations of basic bootstrap toolchains.
   - point artifact writes at the shared in-cluster Buildbarn frontend (`grpc://frontend.buildbarn.svc.cluster.local:8980`). Persist fetched sources through the paired BuildBarn Remote Asset index (`grpc://bb-remote-asset.buildbarn.svc.cluster.local:8984`, `type: index`) and frontend CAS (`type: storage`), both `push: true`; the external artifact/source cache URLs are read-only fallbacks.
   - keep `source-caches` and `artifacts` populated with the project cache URLs rather than wiping them out; an empty server list forces BuildStream to rebuild bootstrap toolchains locally.
   - when the checkout uses upstream `gnome-build-meta`/`freedesktop-sdk` junctions, mirror their patch queues into the checkout before the build so the cache keys match the upstream remote caches instead of diverging on local patch-set differences.
   - match BuildStream concurrency to live BuildBarn capacity. Dakota uses four
     coordinator fetchers, two builders/pushers for its two one-slot workers,
     and eight jobs per action for the runner CPU limit. Do not serialize a
     healthy distributed build or call cache traffic distributed execution.
4. Validate workflow YAML with `just lint` before push.
5. Confirm live behavior from workflow logs/config output, not assumptions.
6. Never use a root filesystem for persistent workload data or a `hostPath` build
   cache. `manifests/local-path-config.yaml` is the GitOps source for explicit
   node-to-data-mount mappings. It intentionally has no default mapping, so
   PVC provisioning fails on an unconfigured node instead of falling back to
   that node's root disk.

## kagent

`manifests/kagent-apps.yaml` installs kagent as two ArgoCD-managed OCI Helm
Applications: `kagent-crds` in sync wave 0, then `kagent` in wave 1. The
default `ModelConfig` uses the lab's OpenAI-compatible llm-d endpoint
(`http://llm-d-modelserver.llm-d.svc.cluster.local:8000/v1`, model
`local-llm`). The endpoint itself is unauthenticated, but the ADK OpenAI client
requires an API key environment variable, so the chart installs the non-secret
placeholder `sk-local-noauth` as `kagent-openai`. Keep the UI ClusterIP-only; do
not expose another general-purpose dashboard.

Install-specific traps from the first rollout:

- Override the chart's `cr.kagent.dev` images to `ghcr.io` so node pulls use
  the zot GHCR mirror.
- The chart's bundled PostgreSQL defaults to `docker.io/library/postgres` and
  uid/gid 999. The lab uses `cgr.dev/chainguard/postgres:latest`, whose
  `postgres` user is uid/gid 70; set the pod security context accordingly.
- Zot sync globs treat `*` as one repository path segment. Nested repos such as
  `kagent-dev/kagent/controller` need `kagent-dev/**`, not `kagent-dev/*`.
  Bump `lab.projectbluefin.io/config-version` when changing the zot ConfigMap.

### GitHub MCP account wiring

`manifests/kagent-github-mcp.yaml` registers GitHub's hosted MCP endpoint as
`RemoteMCPServer/github-castrojo`; `manifests/kagent-github-agent.yaml` exposes
it as `Agent/github-castrojo`. The Authorization header lives only in the
uncommitted `github-mcp-castrojo` Secret in the `kagent` namespace — never
commit the token. The agent uses the default GitHub toolset but puts
write-capable tools in `requireApproval`; verify changes by checking
`RemoteMCPServer.status.discoveredTools`, waiting for `Agent Ready=True`, and
asking the agent for the authenticated login (`get_me` should return
`castrojo`).

## AMD GPU topology

Both lab nodes are 64 GB Framework Desktop (Strix Halo / Ryzen AI Max+ 395)
machines with an AMD Radeon 8060S iGPU (gfx1151, RDNA 3.5). Each advertises
`amd.com/gpu: 1`.

The `amdgpu-device-plugin` DaemonSet selects nodes via the Node Feature
Discovery label `feature.node.kubernetes.io/pci-0380_1002.present` (AMD display
controller). Do **not** reintroduce the old hand-applied
`lab.projectbluefin.io/amd-gpu` label — it was only ever set on `exo-0`, which
left ghost's GPU unusable for months.

### GPU memory: two ceilings, only one of which you should touch

| Control | Where | Correct value |
|---|---|---|
| BIOS UMA carve-out (`mem_info_vram_total`) | Framework firmware | **minimum (512 MiB)** |
| GTT (`mem_info_gtt_total`) | `ttm.pages_limit` karg | 48 GiB (`12582912` pages) |

`manifests/amdgpu-kargs.yaml` sets the kargs via `rpm-ostree` and annotates each
node `lab.projectbluefin.io/amdgpu-kargs=applied|pending-reboot`. Kargs take
effect only on reboot.

**Do not raise the BIOS carve-out.** It was measured on 2026-08-06 and is a hard
steal from system RAM, not a reallocation — and because GTT is sized from what
remains, raising it makes GPU memory *worse*:

| BIOS UMA | VRAM | MemTotal | GTT |
|---|---|---|---|
| 512 MiB (correct) | 512 MiB | 62.1 GiB | 31.0 GiB → **48 GiB with kargs** |
| 48 GiB (tested, reverted) | 48.0 GiB | **15.4 GiB** | **7.9 GiB** |

At 48 GiB carve-out, Kubernetes saw the node as a 15 GiB machine — too small for
the buildbarn workers, KubeVirt VMs, and KubeStellar control-plane pods it
carries. Minimum carve-out plus a raised `ttm.pages_limit` yields 48 GiB of
GPU-addressable memory *and* keeps all 62 GiB of system RAM.

Note `ttm.page_pool_size` is deliberately unset: it pre-allocates and
permanently removes memory from the OS.

### Verify

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.amd\\.com/gpu,KARGS:.metadata.annotations.lab\\.projectbluefin\\.io/amdgpu-kargs
```

## ROCm inference pause

The local `llm-d` inference workload may be intentionally paused in
`manifests/llm-d.yaml` with `replicas: 0`. Treat that as the expected stopped
state, not a failed deployment. Do not use `kubectl scale` for a durable pause
because ArgoCD self-heal restores the declared state; restore `replicas: 1` in
git to re-enable inference.

That self-heal is fast enough to mislead: a runtime `kubectl scale --replicas=0`
was measured being reverted in **~1 second**, with a replacement pod already
Running. An operator who scales and then checks `kubectl get deploy` sees
`0/1` and concludes it worked.

**The PVC caveat, and when it actually applies.** `manifests/llm-d.yaml` long
carried the opposite advice — scale at runtime, never commit `replicas: 0` —
on the grounds that `llm-d-model-cache` is `local-path`, which is
`WaitForFirstConsumer`, so a PVC with no pod never binds, ArgoCD blocks on it,
and the Deployment is never created. **That deadlock is real but applies only
at first creation.** Once the PVC is `Bound` it stays bound;
`WaitForFirstConsumer` gates the first binding, not the lifetime. So:

| State of `llm-d-model-cache` | Pausing with `replicas: 0` in git |
|---|---|
| `Bound` | Safe — this is the durable pause |
| `Pending` / not yet created | Deadlocks; bring it up at `replicas: 1` first |

Check with `kubectl get pvc -n llm-d` before committing a pause.

If an old ArgoCD operation is still waiting for the pre-pause Deployment,
terminate only that stale operation before syncing the current revision. Remove
only explicitly identified stuck workload pods after the Deployment is scaled
to zero.

## Lightweight Prometheus backend

`manifests/prometheus-lightweight.yaml` is the lab's backend-only metrics
service. Keep it as a single `ClusterIP` deployment: do not add an ingress,
Grafana, Prometheus Operator, or another cluster dashboard.

### Uplink traffic report (LAN + WAN)

For a rolling uplink view, port-forward Prometheus and run:

```bash
kubectl -n kube-system port-forward svc/prometheus 9090:9090
just traffic-report
```

The report calls cAdvisor's node-root counters on the physical `enp191s0`
**uplink interface totals (LAN + WAN)**. `enp191s0` is the lab LAN NIC, not a
clean internet boundary: its counters include node-to-node traffic, LAN
clients, and in-cluster hairpin traffic. Do not present those totals as
external bandwidth.

The report includes a deliberately partial WAN estimate. Zot cache pod RX is
shown as estimated upstream image-pull ingress, and a node's uplink TX minus
the other visible node(s)' RX is shown as estimated non-node egress. These
subtractions are assumptions, not flow data; a missing or negative component
is rendered as unavailable. Other WAN/LAN traffic cannot be separated because
cAdvisor has no remote-address or port labels. Destination-level telemetry
would require a bounded eBPF/flow exporter, which this report does not invent.

The report also compares Zot upstream RX with Zot TX (cache serving) and shows
an approximate byte cache-hit ratio under the assumption that upstream RX is
miss traffic. Its workload sections exclude the known
`hostNetwork: true` `usb4-link-monitor-*` node mirrors, which otherwise just
duplicate node counters. Treat any remaining cAdvisor workload rows as
attribution only, not destination-level internet flow data.

Zot pull counters are included as a ranked image-repository activity signal,
but Zot does not expose per-repository byte totals; do not present those
counts as bandwidth.

**Zot on-demand sync pulls blobs on tag reads.** Any `skopeo inspect` or pull
of a *tag* through the cache (`192.168.1.102:30501/...`) copies the manifest
AND all blobs from upstream when the digest changed — a digest poll through
zot costs a full multi-GB image, not kilobytes. Digest pollers/watchers must
inspect the upstream registry directly (`skopeo inspect docker://ghcr.io/...`
with `--creds "_token:${GITHUB_TOKEN}"` for ghcr); see
`argo/workflow-templates/image-poller.yaml` and the regression test in
`tests/unit/test_image_poll_bandwidth.py` (PR #632).

`traffic_report.py` also ranks workload counters when cAdvisor exposes
Kubernetes namespace/pod/container labels. These are still interface totals,
not network-flow records: the current cAdvisor stack has no remote
address/port or destination-service labels. Do not infer destination traffic
from them. Safe destination reporting requires a separately bounded eBPF or
flow exporter and an explicit allowlisted scrape job.

GitHub request and throttle sections are optional hooks. An exporter may be
scraped only by annotating its pod with `prometheus.io/scrape: "true"`,
`prometheus.io/port`, and
`lab.projectbluefin.io/metrics: github-api`; it must expose bounded
`github_api_requests_total` and `github_api_throttled_total` counters without
tokens, URLs containing credentials, request payloads, or unbounded workflow
labels. The lab does not deploy that exporter or collect GitHub secrets.

- Storage is a `local-path` RWO PVC. The Deployment uses zero-surge rolling
  updates (`maxSurge: 0`, `maxUnavailable: 1`) so two pods never contend for
  the node-local volume.
- The PVC is 50Gi; Prometheus keeps at most 30 days or 45GB, whichever limit is
  reached first. The remaining space is headroom for WAL and compaction.
- Required scrape jobs are `kubernetes-nodes`, `kubernetes-cadvisor`,
  `argo-workflow-controller`, `zot`, `buildbarn`, and the existing
  `kubestellar-*` controller jobs.
- Zot metrics require `extensions.metrics` in both `zot-cache-config` and
  `zot-local-config`; both expose `/metrics` on their existing HTTP port. Bump
- Zot v2.1.1 requires an authenticated user in
  `http.accessControl.metrics.users` once repository access control is enabled.
  Migrate Prometheus to basic auth in the same rollout; do not activate auth
  while the scrape still depends on anonymous `/metrics`.
- Stage Zot authentication separately from activation when anonymous writers
  already exist: commit the htpasswd/Secret contract and auth-ready config,
  migrate every writer, then switch the mounted config and make Secrets required
  in one GitOps rollout. Bump
  each workload's `lab.projectbluefin.io/config-version` annotation when either
  ConfigMap changes because Zot reads the subPath-mounted config only at startup.
- Argo custom build metrics use only constant `pipeline` and bounded `status`
  labels. Never label metrics with workflow name, UID, commit SHA, image digest,
  ref, or element.
- `cosmic-build-pipeline`, `bluefin-server-build-pipeline`, and
  `bst-qa-pipeline` emit completion and duration metrics. Dakota stays omitted
  until its DAG refactor makes overall workflow status match the publish result;
  its current non-blocking branches would record false successes.

After changing scrape configuration, bump
`lab.projectbluefin.io/config-version` on the Prometheus pod template because
there is no config-reloader sidecar. Verify through the Kubernetes API:

```bash
kubectl get pvc -n kube-system prometheus-lightweight-data
kubectl get pods -n kube-system -l app=prometheus-lightweight
kubectl port-forward -n kube-system svc/prometheus 9090:9090
curl -fsS 'http://127.0.0.1:9090/api/v1/targets?state=active' |
  jq -r '.data.activeTargets[] | [.labels.job, .health, .lastError] | @tsv'
```

The custom series are exposed as
`argo_workflows_lab_build_workflow_completed_total` and
`argo_workflows_lab_build_workflow_duration_seconds`.

## Deep-dive topics

- [BuildStream distributed builds and Buildbarn](buildstream.md)
- [Node storage maintenance and migration](storage.md)
- [Node recovery without SSH](node-recovery.md)
- [Changing Zot sync prefixes safely](zot-sync.md)

## Mandatory first step

Before any kubectl, k3s, or K8sGPT operation, look up the current API via Context7:

```
resolve-library-id "/k3s-io/k3s" → get-library-docs
resolve-library-id "/k8sgpt-ai/k8sgpt" → get-library-docs
```

Do not guess flags, chart schema, or MCP method names. The K8sGPT MCP server exposes `analyze`, `cluster-info`, `list-resources`, `get-resource`, `list-namespaces`, `get-logs`, `list-events`, `list-filters`, `add-filters`, `remove-filters`, `list-integrations`, and `config`; verify the current docs before wiring it into a client.

## Tool roles

| Tool | Role |
|------|------|
| `k3s` | Lightweight Kubernetes — cluster runtime |
| `kubectl` | Direct cluster inspection and apply |
| `zot` | OCI registry for test artifacts |
| `external-secrets` | Pulls secrets from vault into k8s Secrets |
| `k8sgpt` | Cluster analysis / MCP troubleshooting bridge |

## Common Rationalizations

- "Ghost has 64 GiB, so the build pod will fit."  
  Fitting is not the same as surviving. VM pods use a higher PriorityClass and
  will preempt a `bst-build` pod for memory. The pod gets deleted, the workflow
  retries, and the build never finishes.

- "I will just retry the workflow again."  
  Retries do not change the resource envelope. Fix the requests, limits, and
  concurrency budget, then retry; do not pin the pod to a preferred node.

- "Two variants should build in parallel to save time."  
  Parallel high-memory pods force one onto ghost where it is preempted. The
  wall-clock savings are lost to retries and partial work. Serialize first;
  parallelize only after the cluster has enough dedicated memory capacity.

- "The semaphore already limits concurrency."  
  The `bst-build` semaphore was set to 3, allowing multiple BST lanes to run
  at once. On a two-node lab where each pod requests 14 GiB, that causes
  collisions and preemptions. Set it to 1 and let the scheduler choose among
  nodes that can satisfy the declared requests.

## Red Flags

- `argo get` shows `pod deleted` for a BST build step.
- `kubectl get events --field-selector reason=Preempted` shows BST pods
  displaced by VM pods on `ghost`.
- Two BST build pods are `Running` at the same time with 14 GiB requests each.
- Builds repeatedly fail fast (seconds to a few minutes) without a build error
  in the container logs.
- A Zot sync-prefix change is called verified because `skopeo inspect` timed
  out, or because ArgoCD reports `Synced`, without any
  `zot_repo_downloads_total` evidence.
- A pull failure against the Zot NodePort is reported as a cluster outage
  without first checking whether the workstation is on Tailscale.

## Verification

- [ ] `just lint` passes after any WorkflowTemplate change.
- [ ] ArgoCD reports `Synced` for `lab` after the push.
- [ ] The submitted build pod is scheduler-admitted without a node selector:
      `kubectl get pod -n argo <pod> -o jsonpath='{.spec.nodeName}'` returns
      a Ready node with adequate allocatable resources.
- [ ] `kubectl get configmap -n argo workflow-semaphores` shows
      `bst-build: "1"`.
- [ ] No `Preempted` events appear for the build pod after 10 minutes.
- [ ] The build progresses past source fetches into artifact pulls/builds.
- [ ] Workflow reaches `Succeeded`, or if it fails, the failure is a real build
      error (not `pod deleted`).
- [ ] Prometheus PVC is `Bound` on `local-path` and survives a rollout restart.
- [ ] Required Prometheus targets report `health: up`; no target exposes a
      user-facing dashboard.
- [ ] Authenticated Zot rollouts preserve anonymous pulls, reject anonymous
      writes, admit the configured writer, and keep the authenticated metrics
      scrape healthy.
- [ ] Build workflow metric labels remain limited to `pipeline` and `status`.
- [ ] After a Zot sync-prefix change, the live DaemonSet shows the new
      `lab.projectbluefin.io/config-version`, and every repository in the
      pre-change `zot_repo_downloads_total` inventory still reports a non-zero
      counter with no error series present.

## GPU inference on Strix Halo (`exo-0`)

`exo-0` is a **64 GB** Framework Desktop (62.1 GiB allocatable), not the 128 GB
box every published Strix Halo guide assumes. Scale all community advice down.

- **Use Vulkan, not ROCm.** On `gfx1151` the Vulkan/RADV backend is the reliable
  llama.cpp path; ROCm is not required for inference. `ghcr.io/ggml-org/llama.cpp`
  publishes **no ROCm tags at all** — only `vulkan`, `cuda`, `musa`, `intel`.
- **The 48 GiB GTT ceiling is not a budget.** GTT is system RAM shared with
  BuildBarn, KubeVirt and KubeStellar. amdgpu GTT pins pages the OOM-killer
  cannot reclaim, so overcommitting deadlocks the node rather than evicting a pod.
- **`kubectl top` cannot measure GPU memory here.** Measured: `top node` reported
  8% while 26.5 GiB was resident in GTT. Read
  `/sys/class/drm/card*/device/mem_info_gtt_used` instead.
- **MoE beats dense ~6-12x.** The node is bandwidth-bound (~85 GB/s
  host-to-device), so *active* parameters set decode speed. A 30B-A3B MoE
  measured 70-74 tok/s at Q6_K; a dense 32B manages ~11 tok/s.
- **Tensor parallelism over the USB4 link is dead.** ~128 collectives/token x
  70-100 us = 9-13 ms/token of stall. Independently measured elsewhere as ~15%
  *slower* than single-node. The link helps load time, not decode.
- **`-fa on` and no-mmap (`--load-mode none`) are mandatory** on Strix Halo.
- **`amdgpu.lockup_timeout=20000`** prevents a spurious "device lost" GPU reset
  that kills long generations; the ~10s default is a desktop tuning. Needs a
  reboot — check the `lab.projectbluefin.io/amdgpu-kargs` node annotation.
- Never raise the BIOS UMA carve-out above its 512 MiB minimum; it steals system
  RAM and *shrinks* GTT.

Two cluster-specific traps that cost real time:

- **Digest-pinned images cannot be pulled.** Node pulls go through the zot mirror
  (`override_path = true`, no upstream fallback) and zot's on-demand sync only
  triggers on *tag* references. A bare `@sha256:` for an uncached image 404s.
  Pin to an immutable per-build tag instead.
- **`replicas: 0` + a `WaitForFirstConsumer` PVC deadlocks ArgoCD.** The PVC
  cannot bind without a pod, ArgoCD blocks its sync waiting for PVC health, and
  so the Deployment is never created at all. Scale at runtime instead of
  committing `replicas: 0`.

See `docs/adr/0007-local-inference-runtime.md`.

## Key references

- Cluster topology: `/AGENTS.md`
- Bootstrap procedure: `/docs/ops/bootstrap.md`
- Recovery: `docs/skills/k3s-cluster-ops` (user skill, load before any cluster recovery)
- K8sGPT MCP config: `~/.copilot/mcp-config.json` on this machine, with `k8sgpt serve --mcp` or `--mcp --mcp-http` as the client target

## K8sGPT usage notes

- Use `k8sgpt analyze --explain` for broad triage.
- Narrow with `--filter=Pod`, `--filter=Deployment`, or `--namespace=<ns>`.
- For assistant integration, prefer the MCP server mode (`k8sgpt serve --mcp`) and register it in Copilot/Claude-style MCP configs.
- For this repo's `k8sgpt-on-demand` Argo template, keep intentionally-idle services in `ignored-services` to avoid known false-positive "Service has no endpoints" noise during stabilization. Remove services from the list once they are expected to have endpoints.
- **K8sGPT reports symptoms, not causes. Correlate before you treat a finding
  as the explanation.** It surfaces every unhealthy object, and the most
  eye-catching one is often unrelated to the behaviour being investigated. A
  finding is a lead, not a diagnosis. Confirm causation against the controller's
  own logs and the object's status before acting:

  ```bash
  kubectl -n <ns> logs deploy/<controller> --tail=400 | grep -ciE 'error|conflict|has been modified'
  kubectl get <kind> <name> -o jsonpath='{.status.conditions}'
  ```

  A worked example: a controller burning sustained network throughput was
  reported alongside a broken Ingress on the same resource. The Ingress was
  genuinely misconfigured but entirely inert; the throughput came from an
  optimistic-concurrency requeue loop (`object has been modified`) in the
  controller, on a resource that reported `Ready=True` throughout. Fixing the
  Ingress would have changed nothing. Two signals distinguish these cases: a
  resource that is `Ready=True` is not the thing failing, and a genuine cause
  produces a matching error frequency in the logs.
- Verified source: `/k8sgpt-ai/k8sgpt`

## Common Rationalizations

- "It only touches cache config, no lint needed." → Wrong; run `just lint` for every workflow YAML change.
- "Project defaults are fine." → Wrong for this lab; project-defined remotes can re-enable external cache push paths.
- "Port 443 refused means cache host down." → Wrong; validate actual BST ports (`11001`/`11002`) and latency behavior.

## Red Flags

- BuildStream configs setting `override-project-caches: true` for pipelines that depend on upstream bootstrap artifacts (like Freedesktop SDK and GNOME OS meta), causing extremely slow and completely cold builds of the entire OS.
- Any BST lane includes external cache host URLs in generated config.
- Docs describe local-first but YAML still allows project cache remotes.

## Verification

- [ ] Workflow templates align `override-project-caches` to `false` for base fallback coverage.
- [ ] No external cache host appears in relevant workflow YAML/scripts.
- [ ] `just lint` passes after edits.
- [ ] Skill content reflects the current shared Buildbarn cache policy.
