# Workflow Reference

This doc covers Argo Workflows and WorkflowTemplates. For the GitHub Actions
bridge that submits Argo Workflows from ephemeral ARC runners, see
`.github/workflows/example-container-mode-build.yml` and
`/docs/ops/maintainer-onboarding.md`.

## Table of Contents
- [Pipelines](#pipelines)
  - [bluefin-qa-pipeline](#bluefin-qa-pipeline)
  - [dakota-qa-pipeline](#dakota-qa-pipeline)
  - [dakota-build-pipeline](#dakota-build-pipeline)
  - [cosmic-build-pipeline](#cosmic-build-pipeline)
  - [bluefin-server-build-pipeline](#bluefin-server-build-pipeline)
  - [bst-qa-pipeline](#bst-qa-pipeline)
  - [knuckle-qa-pipeline](#knuckle-qa-pipeline)
- [Supporting Templates](#supporting-templates)
- [Distributed Build/RE Grid](#distributed-buildre-grid)
- [Cache Warming (Pollers)](#cache-warming-pollers)
- [Nightly Schedule](#nightly-schedule)
- [Priority Classes](#priority-classes)
- [Resource Profiles](#resource-profiles)

## Pipelines

### bluefin-qa-pipeline
- **Purpose:** Run Bluefin/Bluefin-LTS suites directly inside the published bootc OCI
  image. There is no containerDisk build, VM boot, or SSH stage on this path.
- **Parameters:** `image`, `image-tag`, `suites`, `variant`, `branch`, `pr-number`, `sha`,
  `repo`, `testsuite-branch`, `testsuite-repo`.
- **DAG:** `validate-suites` → parallel `test-lane` items (`smoke`, `common`, `developer`,
  `software`, `system`, filtered by `suites`) using `run-container-tests`.
- **Each lane:** `run-container-tests` clones `projectbluefin/testsuite`, boots a
  Wayland session with `dbus-run-session` + `qecore-headless`, runs `behave`,
  publishes per-suite results back to this repo when `github-token` is present,
  and returns a summary file to the workflow.
- **Image-poll trigger flow:** `image-poller` compares GHCR digests against
  `image-polling-digests`, submits this pipeline on change, and only writes the
  new digest back after the workflow succeeds.
- **Just recipe:** `run-tests`, `run-tests-tag`, `run-tests-matrix`.

### dakota-qa-pipeline
- **Purpose:** Run Dakota suites directly inside the published bootc OCI image using
  the same container-only fan-out model as `bluefin-qa-pipeline`.
- **Parameters:** `image`, `image-tag`, `suites`, `variant`, `branch`, `pr-number`, `sha`,
  `repo`, `testsuite-branch`, `testsuite-repo`.
- **DAG:** `validate-suites` → parallel `test-lane` items (`smoke`, `common`, `developer`,
  `software`, `system`) through `run-container-tests`.
- **Just recipe:** `run-dakota-qa`.
- **PR review:** use the [Dakota PR review skill](../skills/dakota-pr-review/SKILL.md). Build the exact PR SHA first, then run smoke and required E2E suites against the resulting image. If lab validation identifies a scoped PR defect, repair the PR branch, rebuild from its new SHA, rerun E2E, and merge directly only after a fresh pass; do not use merge queue when Dakota GHA is known-broken.

### dakota-build-pipeline
- **Purpose:** The actual BuildStream compile step for Dakota — builds
  `oci/bluefin.bst` and pushes the image to the local Zot registry under the tag
  given by `image-tag` (default `testing`). NVIDIA variants are built
  non-blocking and pushed with the same tag.
- **Parameters:** `repo`, `ref`, `commit-sha`, `image-tag` (default `testing`),
  `registry`, `build-mode`, `lock-key`.
- **Distribution:** `build-mode=re` is mandatory. A fresh USB4 `up` observation
  and a Ready BuildBarn worker are required on both `ghost` and `exo-0` before
  admission. Cache-only, Ethernet-backed, automatic fallback, runner-local
  execution, and remote-cache-only execution are failures, not alternatives.
  Scheduler-driven placement selects the coordinator; no task pins it to a node.
- **Capacity:** The coordinator uses four fetchers, two BuildStream builders and
  pushers, and eight jobs per action. Each of the two BuildBarn workers exposes
  one action slot. This capacity must not be increased until remote execution and
  full SDK CAS materialization are healthy. The workflow verifies its generated
  remote-execution configuration before it invokes BuildStream.
- **Priority:** `priorityClassName: bst-build` keeps the coordinator ahead of
  short-lived lab test workloads.
- **Who triggers it automatically:** the `dakota-commit-poller` CronWorkflow
  through the shared `bst-commit-poller` template (see
  [Cache Warming](#cache-warming-pollers)). The poller resolves the current
  GitHub SHA for `dakota:testing` and passes that exact commit into the local
  BuildStream run, so the lab build checks out the same source revision that
  GitHub is building instead of drifting to a later branch tip.
- **PR path:** `pr-poller` dispatches a combined build+QA workflow for Dakota PRs
  that builds the exact PR head SHA and tags the resulting image with that SHA
  before running the container QA suites against it.

**Distributed-gate rule:** A Dakota PR build is valid only when a fresh
`build-mode=re` run completes for the exact PR head SHA and its pushed registry
image is verified. Local, cache-only, or fallback builds are diagnostic evidence
and do not satisfy the distributed gate. Inspect failed child nodes even when the
Argo parent phase is successful; classify BuildBarn storage/DNS/worker failures as
infrastructure blockers and repair the lab before retrying.

### cosmic-build-pipeline
- **Purpose:** BuildStream compile pipeline for the default COSMIC image
  (`oci/cosmic/image.bst`) and push it to local Zot. NVIDIA variants are
  intentionally disabled for clean distributed builds.
- **Safety guards (aligned with dakota):**
  `activeDeadlineSeconds: 14400` (workflow), `activeDeadlineSeconds: 5400` (step),
  `retryStrategy: limit=2, retryPolicy=Always`, `GRPC_POLL_STRATEGY=poll`,
  `GRPC_ENABLE_FORK_SUPPORT=1`, `request-timeout: 900`,
  `scheduler.network-retries: 4`, `scheduler.fetchers: 1`.
- **Cache policy:** uses the same shared Buildbarn remote cache/execution path as
  Dakota, via the checked-in `buildstream-remote-cache` config, with
  `override-project-caches: false` and explicit upstream artifact/source cache URLs
  listed as read-only fallbacks while Buildbarn handles artifact writes.

### bluefin-server-build-pipeline
- **Purpose:** BuildStream compile pipeline for Bluefin Server elements
  (`oci/bluefin-server-ddi.bst`, `oci/bluefin-server-installer.bst`) and push to local Zot.
- **Safety guards (aligned with dakota/cosmic):**
  `activeDeadlineSeconds: 14400` (workflow), `activeDeadlineSeconds: 5400` (step),
  `retryStrategy: limit=2, retryPolicy=Always`, `GRPC_POLL_STRATEGY=poll`,
  `GRPC_ENABLE_FORK_SUPPORT=1`, `request-timeout: 900`,
  `scheduler.network-retries: 4`, `scheduler.fetchers: 1`.
- **Cache policy:** uses the shared Buildbarn frontend (`frontend.buildbarn.svc.cluster.local:8980`) for artifact cache writes and remote execution; the current BuildStream image in this cluster does not accept the legacy `remoteasset:` config block, so the config omits it. The checked-in `buildstream-remote-cache` config leaves project cache overrides disabled and lists the project's own upstream artifact/source cache URLs as read-only fallbacks.

### bst-qa-pipeline
- **Purpose:** Smoke-tests the Buildbarn distributed remote-execution grid itself
  by running a trivial BuildStream element through it.
- **Cache + RE wiring:** artifact cache writes, remote execution, and remote asset
  fetches all flow through the shared Buildbarn frontend and remote-asset service
  (`frontend.buildbarn.svc.cluster.local:8980` and
  `bb-remote-asset.buildbarn.svc.cluster.local:8984`). The project cache remotes
  are Buildbarn-only. See [Distributed Build/RE Grid](#distributed-buildre-grid).
- **Known limitation:** the current test element (`hello.bst`, an `import` kind)
  proves config wiring (BuildStream connects to the frontend with no errors) but
  never actually dispatches an action through the scheduler to a worker — verified
  by checking the CAS blocks file (zero bytes written). A real build-dispatch
  test element would be needed to prove end-to-end RE execution conclusively.

### knuckle-qa-pipeline
- **Purpose:** Build the Knuckle installer ISO/binary, provision a blank VM, run
  the headless installer in-cluster, boot the installed system, rediscover SSH
  reachability, and run smoke tests.
- **Parameters:** `branch`, `namespace`, `suite`, `ssh-key-secret`, `tests-branch`.
- **DAG:** `clone-source` → `build-installer` → `provision-target-vm` →
  `boot-installer` → `wait-install-complete` → `transition-to-installed` →
  `discover-installed-ip` → `run-smoke-tests`; `onExit: teardown`
  (`teardown-vm` → `cleanup-installer-artifacts`).
- **Disk:** PVC-backed (`local-path`), not hostDisk/hostPath — KubeVirt
  co-schedules the VM automatically on the PVC's node, no explicit
  `nodeSelector` needed.
- **Just recipe:** None currently; submit the WorkflowTemplate directly or use
  the nightly CronWorkflow.

## Supporting Templates

| Template | Role |
| --- | --- |
| `image-poller` | Digest-comparison trigger for the bootc image-poll lane. Flow: fetch GHCR digest → compare with `image-polling-digests` → submit `bluefin-qa-pipeline` on change → persist digest only after downstream success. |
| `run-container-tests` | Shared container-only runner for Bluefin/Dakota bootc images. Clones `projectbluefin/testsuite`, starts a Wayland session in the target OCI image, runs `behave`, publishes per-suite results back to this repo, and writes a summary file for workflow outputs. |
| `provision-flatcar-vm` / `provision-gnomeos-vm` | VM-backed provisioning paths for the lanes that still need KubeVirt. Both set `priorityClassName: lab-test-vm`. |
| `teardown-vm` | Deletes any KubeVirt test VM (and hostDisk/PVC where applicable). |
| `run-gnome-tests` | Shared VM-backed test runner. Clones `projectbluefin/testsuite`, waits for SSH, installs dependencies, copies `tests/<suite>`, and runs `behave` against a live guest. |
| `run-incluster-tests` | Shared in-cluster pytest runner. Git-syncs `lab`, runs a pytest module against a live k8s workload, emits JUnit XML. |

## Distributed Build/RE Grid

Two independent distributed-build mechanisms exist on the cluster — they solve
different problems and do not overlap:

| Mechanism | What it distributes | Used by |
| --- | --- | --- |
| k8s scheduler (no pin) | Full privileged bootc OCI builds (needs real FUSE/mount-namespace access) | `dakota-build-pipeline`, `bluefin-server-build-pipeline` |
| Buildbarn (`buildbarn` namespace) | BuildStream cache writes and remote-execution actions (chroot-only sandbox, `CAP_SYS_CHROOT`) | `dakota-build-pipeline`, `cosmic-build-pipeline`, `bluefin-server-build-pipeline`, `bst-qa-pipeline` |

Buildbarn topology (2 storage shards, 1 scheduler, 2 frontend replicas, 1
worker+runner DaemonSet pair per node — storage replicas spread with
`podAntiAffinity`) is defined in `manifests/buildbarn-*.yaml`. Every BST lane
requires the real BuildBarn execution grid over a fresh USB4 link between
`ghost` and `exo-0`. If a link, worker, or action is unavailable, the workflow
must fail for repair; it must not use an Ethernet, local, or cache-only fallback.

## Cache Warming (Pollers)

| CronWorkflow | Interval | Triggers | Keeps warm |
| --- | --- | --- | --- |
| `dakota-commit-poller` | every 5 min at minute +2 | shared `bst-commit-poller` → `dakota-build-pipeline` when `dakota:testing` changes | Dakota BuildStream cache/execution path |
| `cosmic-commit-poller` | every 5 min at minute +4 | shared `bst-commit-poller` → `cosmic-build-pipeline` when `cosmic-build-meta:main` changes | Cosmic BuildStream cache/execution path |
| `image-poll-bluefin-{testing,stable}` / `image-poll-lts-{testing,stable}` | 10 min | `image-poller` when the respective GHCR tag digest changes | Bluefin/LTS container-only QA freshness plus result publication |
| `image-poll-snosi-latest` | 30 min past every 3 hours | `bluefin-qa-pipeline` when `ghcr.io/frostyard/snow:latest` changes | Snosi GNOME desktop image coverage |
| `flatcar-kernel-poller` | 10 min | `flatcar-kernel-build` when kernel.org's latest stable version changes | Flatcar kernel build cache |
| `flatcar-kernel-gate` | 30 min | (gate/promotion check, see `/docs/skills/flatcar-node-onboarding/SKILL.md`) | N/A |

Dakota/Cosmic/Bluefin Server/BST lanes now use the shared Buildbarn frontend for
cache writes and remote execution while leaving upstream mirrors read-only. Cold
runs may fetch from upstream source origins, but cache writes stay in-cluster via
Buildbarn.

**`bluefin-server-build-pipeline` has no poller at all** — it is manual-trigger
only and uses the same USB4-gated BuildBarn remote-execution contract as every
other BST lane.

**`nightly-dakota` does not warm anything** — it's wired to `dakota-qa-pipeline`
(test runner against pre-built images), not `dakota-build-pipeline` (the actual
compile step). The real Dakota cache-warming trigger is `dakota-commit-poller`. It must not be
interpreted as proof of a green distributed build: the poller succeeds only when
the remote BuildStream workflow, image export, registry push, and configured
validation path succeed.

The Dakota and Cosmic commit CronWorkflows share one implementation. It compares
the source SHA, defers when two BST workflows are already admitted, invokes the
repository-specific build template, and writes the SHA only after that build
succeeds. The schedules are staggered behind the PR poller to avoid simultaneous
admission bursts.

**Current contract:** `image-poller` must not update `image-polling-digests`
until `run-pipeline.Succeeded`. If the digest is written before QA passes, the
poller will treat the image as already seen and silently skip the failed lane on
the next cycle.

## Nightly Schedule

| CronWorkflow | Time (UTC) | Pipeline | Parameters |
| --- | --- | --- | --- |
| `nightly-smoke` | 02:00 | `bluefin-qa-pipeline` | `image=ghcr.io/projectbluefin/bluefin`, `image-tag=testing`, `suites=smoke,developer,system`, `variant=bluefin` |
| `nightly-smoke-lts` | 02:30 | `bluefin-qa-pipeline` | `image=ghcr.io/projectbluefin/bluefin-lts`, `image-tag=testing`, `suites=smoke,developer,system`, `variant=bluefin-lts` |
| `nightly-dakota` | — | `dakota-qa-pipeline` | Tests pre-built images only — does not build/warm cache. Currently `suspend: true`. |
| `nightly-knuckle` | 03:30 | `knuckle-qa-pipeline` | `branch=main`, `namespace=knuckle-test`, `suite=smoke`, `tests-branch=main` |

## Priority Classes

| PriorityClass | Value | Applied to |
| --- | --- | --- |
| `lab-test-vm` | 1,000,000, `PreemptLowerPriority` | All explicit VM-backed KubeVirt test VMs (`provision-flatcar-vm`, `provision-gnomeos-vm`, and `knuckle-qa-pipeline`'s VM specs) |
| `bst-build` | (see `manifests/bst-build-priorityclass.yaml`) | Heavy/long BuildStream compiles: `dakota-build-pipeline`, `bluefin-server-build-pipeline`, `flatcar-kernel-build`'s VM spec |

Test VMs are meant to win resource contention over background build workloads —
`lab-test-vm`'s higher priority value plus `PreemptLowerPriority` enforces this
against any pod using `bst-build`.

## Resource Profiles

Pod resource requests/limits used by workflow steps:

| Template | CPU req/limit | Memory req/limit |
| --- | --- | --- |
| `run-container-tests` | 1 / 2 | 2Gi / 4Gi |
| `wait-for-vm-ready` | 100m / 500m | 128Mi / 256Mi |
| `run-gnome-tests` | 1 / 2 | 1Gi / 2Gi |
| `dakota-build-pipeline/bst-build` | 8 / 16 | 16Gi / 32Gi |
| `cosmic-build-pipeline/bst-build` | 4 / 8 | 14Gi / 28Gi |
| `bluefin-server-build-pipeline/bst-build` | 6 / 10 | 16Gi / 30Gi |
| `knuckle build-installer` | 4 / 4 | 8Gi / 8Gi |
| `knuckle write-ignition` | 100m / 500m | 128Mi / 256Mi |
| `knuckle boot-installer` | 1 / 2 | 1Gi / 2Gi |
| `knuckle wait-install-complete` / `transition-to-installed` / `discover-installed-ip` | 250m / 1 | 256Mi / 512Mi |
| `knuckle cleanup-installer-artifacts` | 50m / 200m | 64Mi / 128Mi |
