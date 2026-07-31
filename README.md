# Self Hosted Cloud Native Operating System Factory
### Introducing the first "Agentic OS Factory" that isn't made up bullshit!

#### [Factory Dashboard](https://factory.projectbluefin.io)

> A production-quality, fully GitOps-driven QA pipeline for testing
> [bootc](https://containers.github.io/bootc/) (image-based Linux) deployments,
> built entirely on CNCF projects running on the local `ghost` k3s cluster.
> This instance is deployed as the CI infrastructure for [Project Bluefin](https://projectbluefin.io).
> The productized form of this will ships as [Bluefin Server](https://projectbluefin.io/server) someday. Welcome.
>

---

## What This Is

This repo is a reference implementation of a CNCF-native homelab designed for bootc
image testing. For the Bluefin and Dakota image-poll lanes, the lab now runs
GUI and contract suites directly inside the published OCI images as Kubernetes
pods. VM-backed boot and install validation remain only for workflows that still
explicitly need KubeVirt (Flatcar, Knuckle, migration, and similar lanes).
Everything is declared in git, reconciled by ArgoCD, and orchestrated by Argo
Workflows. GitOps.

This instance runs as the CI infrastructure for Project Bluefin — selected image
poll lanes trigger fully automated test runs with zero human intervention:
image-poller checks the digest, compares it with stored state, fans out
`run-container-tests`, publishes per-suite results back into this repo, and only
then records the new digest. This is Bluefin Server's first usecase.

See [/docs/reference/bluefin-integration.md](/docs/reference/bluefin-integration.md) for the full
image-poll → container test → result publication pipeline.

> The C and C Music Factory is mastery and full of jams that has to be
>
> -- Freedom Williams

---

## Continuous Image Integration & Testing

The lab continuously validates the core operating system family across multiple hardware-profile targets and variants:

| Image | Tag | Schedule / Trigger | Purpose / Suite |
|---|---|---|---|
| `ghcr.io/projectbluefin/bluefin` | `testing` | Nightly 02:00 UTC + 10-minute digest polling | Container-only: digest `smoke`; nightly `smoke,developer,system` |
| `ghcr.io/projectbluefin/bluefin` | `stable` | Nightly 03:00 UTC + 10-minute digest polling | Container-only: digest `smoke,common,developer,software,system`; nightly `smoke` |
| `ghcr.io/projectbluefin/bluefin-lts` | `testing` | Nightly 02:30 UTC + 10-minute digest polling | Container-only: digest `smoke`; nightly `smoke,developer,system` |
| `ghcr.io/projectbluefin/bluefin-lts` | `stable` | Nightly 03:30 UTC + 10-minute digest polling | Container-only: digest `smoke,common,developer,software,system`; nightly `smoke` |
| `ghcr.io/ublue-os/aurora` | `testing`, `stable` | Every 3 hours + OCI digest polling | KDE variant validation (system suite) |
| `ghcr.io/frostyard/snow` | `latest` | Every 3 hours + on every OCI digest change | Snosi GNOME desktop profile (smoke/developer/system suites) |
| `ghcr.io/projectbluefin/dakota` | `testing` | 10-minute digest polling; nightly trigger suspended | BuildStream (BST) flatcar-substrate variant; container-only QA after publication |

**Image-poll trigger:** image-poll CronWorkflows check OCI registry digests against
`image-polling-digests` (10-minute lanes for Bluefin/LTS/Dakota; slower pollers for
other images). When a digest changes, `image-poller` fans out
`run-container-tests` and only persists the new digest after QA succeeds.

**Result publication:** each selected `run-container-tests` lane writes
`results.json`, then runs `scripts/publish_test_results.py` to push structured
per-suite results back into this repo for dashboard consumers.

See [/docs/reference/bluefin-integration.md](/docs/reference/bluefin-integration.md) for full details.

---



| Layer | Project | CNCF Status | Role |
|---|---|---|---|
| Kubernetes | [k3s](https://k3s.io) | [Sandbox](https://www.cncf.io/projects/k3s/) | Lightweight local cluster |
| VM workloads | [KubeVirt](https://kubevirt.io) | [Incubating](https://www.cncf.io/projects/kubevirt/) | Ephemeral test VMs on bare metal |
| CI/CD | [Argo Workflows](https://argoproj.github.io/argo-workflows/) | [Graduated](https://www.cncf.io/projects/argo/) | DAG pipeline orchestration |
| GitOps | [Argo CD](https://argo-cd.readthedocs.io) | [Graduated](https://www.cncf.io/projects/argo/) | Declarative cluster state from git |
| Control plane | [KubeStellar](https://kubestellar.io) | CNCF Sandbox | WDS/ITS control plane for the local lab |
| Private administration | [KubeStellar Console](https://github.com/kubestellar/console) | KubeStellar project | Sole private cluster-admin and single-pane UI |
| Metrics | [Prometheus](https://prometheus.io) | [Graduated](https://www.cncf.io/projects/prometheus/) | Backend metrics service; not a dashboard |
| Public reporting | [Astro](https://astro.build) on GitHub Pages | — | Read-only Factory and Lab reporting |
| Observability | [Grafana Loki](https://grafana.com/oss/loki/) | CNCF landscape | Log aggregation for workflow pods |
| Images | [OCI](https://opencontainers.org) + [bootc](https://containers.github.io/bootc/) | Standard | Atomic OS image format |

> All pipelines run on commodity x86_64 hardware (single Ryzen AI node, 64GB RAM).
> The canonical target is the local `ghost` k3s topology. Local workers can
> join it, but lab design and verification do not assume cloud infrastructure
> or external clusters.

<img width="926" height="988" alt="image" src="https://github.com/user-attachments/assets/718ee70c-aa7d-470e-ab9f-e526ee7c39ea" />


---

## Architecture

```
image-poller CronWorkflow
        │
        ▼
  digest comparison (`image-polling-digests`)
        │
        ├─ unchanged ───────────────► exit cleanly
        │
        └─ changed ─────────────────► `run-container-tests` fan-out
                                      │
                                      ├─ qecore + behave inside the bootc OCI image
                                      ├─ publish per-suite results back to this repo
                                      └─ update stored digest only after QA succeeds
```

**GitOps loop:**

```
git push main
    │
    ▼
ArgoCD polls (or webhook)
    │
    ├─ argo/workflow-templates/ ──► WorkflowTemplates reconciled in cluster
    └─ manifests/               ──► CronWorkflows, RBAC, infra reconciled in cluster
```

**Operator surfaces:**

- KubeStellar Console is the sole private cluster-admin and single-pane UI.
- Astro remains the public read-only reporting surface.
- Prometheus remains backend-only. Do not add Grafana or a parallel
  general-purpose cluster-admin/dashboard framework.

---

## Repository Layout

```
lab/
├── README.md                         # This file
├── AGENTS.md                         # Agent entry point
├── docs/ops/RUNBOOK.md               # Timeless architecture + failure modes
├── docs/reference/WORKFLOWS.md       # WorkflowTemplate agent contract
├── Justfile                          # Operator convenience wrappers
│
├── argo/
│   ├── workflow-templates/           # ← ArgoCD (lab App) auto-syncs these
│   │   ├── bluefin-qa-pipeline.yaml      container-only Bluefin suite fan-out
│   │   ├── bluefin-migration-test.yaml   bootc switch migration validation
│   │   ├── bluefin-service-catalog-pipeline.yaml  service catalog smoke lanes
│   │   ├── run-container-tests.yaml      behave + qecore inside the bootc OCI image
│   │   ├── run-gnome-tests.yaml          VM-backed behave + qecore + Dogtail tests
│   │   ├── run-incluster-tests.yaml      in-cluster (kubectl-based) tests
│   │   ├── run-flatcar-tests.yaml        Flatcar OS test runner
│   │   ├── provision-flatcar-vm.yaml     provision Flatcar test VM (hostDisk)
│   │   ├── provision-gnomeos-vm.yaml     provision GNOME OS test VM
│   │   ├── teardown-vm.yaml          delete explicit VM-backed test guests
│   │   ├── collect-vm-logs.yaml          gather VM journal logs post-test
│   │   ├── dakota-build-pipeline.yaml   Dakota BST build pipeline (default variant only; NVIDIA disabled)
│   │   ├── bst-commit-poller.yaml       Shared Dakota/Cosmic commit polling and BST admission
│   │   ├── dakota-qa-pipeline.yaml      container-only Dakota suite fan-out
│   │   ├── knuckle-qa-pipeline.yaml     Knuckle installer QA pipeline
│   │   ├── image-poller.yaml            Digest poller: compare → run-container-tests → publish → persist
│   │   ├── pr-poller.yaml               PR label poller for CI gate
│   │   ├── register-wec.yaml             register a cluster with KubeStellar
│   │   ├── kubestellar-smoke-test.yaml   verify downsync + status upsync
│   │   ├── kubestellar-platform-verify.yaml  ordered platform acceptance gate
│   │   ├── ghost-cleanup.yaml           Clear stale podman lock files on ghost
│   │
│   ├── bootstrap/                    # ← NOT ArgoCD managed — run once to set up cluster
│   │   ├── README.md                     bootstrap guide
│   │   ├── install-kubevirt.yaml         install KubeVirt (CNCF Incubating)
│   │   ├── install-cdi.yaml             install Containerized Data Importer
│   │   ├── install-kubevirt-manager.yaml install KubeVirt Manager web UI
│   │   ├── install-test-vms.yaml        apply initial test VM manifests
│   │   └── setup-otel.yaml              deploy OTel observability stack
│   │
│   ├── bluefin-smoke-test.yaml       submit: single-image smoke run
│   ├── bluefin-test-matrix.yaml      submit: parallel testing + lts-testing matrix
│   ├── bluefin-service-catalog-smoke.yaml  submit: service catalog smoke
│   ├── flatcar-smoke-test.yaml       submit: Flatcar smoke run
│   ├── gnomeos-access-spike.yaml     submit: GNOME OS accessibility spike
│   └── one-shot-delete-golden-disks.yaml  emergency: delete all golden disks to reclaim space
│
├── manifests/                        # ← ArgoCD (lab-infra App) auto-syncs these
│   ├── nightly-smoke.yaml                CronWorkflow: nightly bluefin:testing @ 02:00 UTC
│   ├── nightly-smoke-lts.yaml            CronWorkflow: nightly bluefin-lts:testing @ 02:30 UTC
│   ├── nightly-dakota.yaml               CronWorkflow: nightly dakota @ 03:00 UTC
│   ├── nightly-knuckle.yaml              CronWorkflow: nightly knuckle @ 03:30 UTC
│   ├── orphan-vm-cleanup.yaml            CronWorkflow: clean orphaned VMs every 2h
│   ├── orphan-pod-gc.yaml                CronWorkflow: GC orphaned pods
│   ├── golden-disk-gc.yaml               CronWorkflow: GC stale golden disks
│   ├── pr-image-gc.yaml                  CronWorkflow: GC PR container images
│   ├── image-poll-bluefin-testing.yaml   CronWorkflow: poll bluefin:testing digest
│   ├── image-poll-bluefin-stable.yaml    CronWorkflow: poll bluefin:stable digest
│   ├── image-poll-bluefin-main.yaml      CronWorkflow: poll bluefin:latest digest
│   ├── image-poll-lts-testing.yaml       CronWorkflow: poll bluefin-lts:testing digest
│   ├── image-poll-lts-stable.yaml        CronWorkflow: poll bluefin-lts:stable digest
│   ├── image-poll-dakota.yaml             CronWorkflow: poll dakota:testing digest
│   ├── image-poll-snosi-latest.yaml      CronWorkflow: poll snosi snow:latest digest
│   ├── pr-label-poller.yaml              CronWorkflow: poll PR labels for CI gate
│   ├── workflow-controller-configmap.yaml TTL patch (7d success, 30d failure)
│   ├── argo-default-sa-rbac.yaml         Argo executor RBAC
│   ├── argo-server-auth.yaml             Argo server auth config
│   ├── argo-server-nodeport.yaml         NodePort for external Argo API access
│   ├── kubevirt-feature-gates.yaml       KubeVirt feature gate config (HostDisk, Ignition)
│   ├── kubevirt-rbac.yaml                KubeVirt RBAC for workflow pods
│   ├── homelab-runner-rbac.yaml          homelab-runner SA + ClusterRole
│   ├── homelab-access-auth.yaml          homelab access auth config
│   ├── flatcar-test-namespace.yaml       Flatcar test namespace
│   ├── gnomeos-test-namespace.yaml       GNOME OS test namespace
│   ├── gnomeos-smbios-hook.yaml          GNOME OS SMBIOS firmware hook
│   ├── bluefin-test-ssh-pubkey.yaml      SSH public key for VM accessCredentials injection
│   ├── bst-build-priorityclass.yaml      PriorityClass for BST build pods
│   ├── lab-test-vm-priorityclass.yaml    PriorityClass for lab test VM pods
│   ├── bst-cache-warm.yaml               BST cache warm manifest
│   ├── inotify-tuning.yaml               inotify kernel parameter tuning
│   ├── loki-config.yaml                  Loki log aggregation config
│   ├── promtail-config.yaml              Loki log scraping config
│   ├── registry-mirror-config.yaml       DaemonSet: write containerd hosts.toml mirror config
│   ├── zot-cache.yaml                    Zot pull-through cache (port 30501, all upstreams)
│   └── zot-writable.yaml                 Zot writable registry (port 30500)
│
├── argocd/
│   ├── application.yaml              ArgoCD App: argo/workflow-templates → cluster
│   └── infra-application.yaml        ArgoCD App: manifests/ → cluster
│
├── tests/
│   ├── smoke/features/               Phase 1: GNOME Shell, Activities, top-bar
│   ├── developer/features/           Phase 2: terminal, Homebrew, Podman, micro
│   ├── software/features/            Phase 3: Flatpak, Bazaar, GNOME Software
│   ├── system/features/              Phase 4: bootc contract, atomic OS assertions
│   └── flatcar/features/             Phase 5: Flatcar systemd + container tests
│
└── docs/
    ├── bootstrap.md                  ← how to replicate this lab from scratch
    ├── agent-cheatsheet.md           canonical command reference
    ├── lab-operations.md             long-form operator procedures
    ├── dogtail-testing.md            GUI test authoring + debugging
    ├── bluefin-integration.md        image-poll → container test → result publication pipeline
    └── /docs/reference/WORKFLOWS.md                  full WorkflowTemplate reference (resource profiles, runtime paths)
```

---

## Test Phases

| Phase | Suite | Trigger |
|---|---|---|
| 1 — Smoke | `smoke` | Every PR, nightly |
| 2 — Developer tooling | `developer` | Nightly, targeted |
| 3 — Software management | `software` | Targeted |
| 4 — Atomic OS contract | `system` | Nightly, every image build |
| 5 — Flatcar substrate | `flatcar` | Dedicated workflow |
| — Migration validation | `migration` | On rechunk → chunkah switches |
| — Dakota BST | `dakota` | Every Dakota PR; see [Dakota PR review](docs/skills/dakota-pr-review/SKILL.md) for exact-SHA build, E2E, repair, and direct-merge policy |

---

## GitOps Model

This repo follows [Argo CD best practices](https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/)
with two ArgoCD Applications that own distinct resource classes:

| Application | Syncs path | Namespace | prune | selfHeal |
|---|---|---|---|---|
| `lab` | `argo/workflow-templates/` | argo | ✅ | ✅ |
| `lab-infra` | `manifests/` | argo (+ others) | ✅ | ✅ |

**Rules:**
1. Edit files in `argo/workflow-templates/` or `manifests/` → push to `main` → ArgoCD reconciles within ~3 minutes.
2. **Never** `kubectl apply` WorkflowTemplates directly — ArgoCD will overwrite it.
3. **Never** `argo create workflow-template` for production templates — same reason.
4. Bootstrap templates in `argo/bootstrap/` are **not** in any ArgoCD sync path — run them once by hand during cluster setup.

---

## Getting Started

See **[/docs/ops/bootstrap.md](/docs/ops/bootstrap.md)** for the complete lab setup guide.

**TL;DR for an existing k3s + KubeVirt cluster:**

```bash
git clone https://github.com/projectbluefin/lab
cd lab

# 1. Bootstrap ArgoCD Applications (once)
just setup-argocd

# 2. Create SSH key secret for VM access (once)
just setup-ssh-secret

# 3. Push — ArgoCD reconciles all WorkflowTemplates automatically
git push origin main

# 4. Run smoke tests
just run-tests
```

---

## Cluster Topology

| Host | Role | Specs |
|---|---|---|
| ghost | k3s control-plane + KubeVirt compute | Ryzen AI MAX+ 395, 16c/32t, 64GB RAM |
| exo-0 | k3s worker | Framework Desktop |

**Namespaces:**

| Namespace | Purpose |
|---|---|
| `argo` | Argo Workflows + ArgoCD (control plane) |
| `argocd` | ArgoCD controller |
| `bluefin-test` | `latest` test VMs |
| `bluefin-lts-test` | `lts` test VMs |
| `flatcar-test` | Flatcar test VMs |
| `gnomeos-test` | GNOME OS test VMs |
| `llm-d` | Local inference namespace (vLLM on ROCm; managed by ArgoCD with 1 GPU-backed replica) |
| `local-registry` | Zot writable registry (30500) + pull-through cache (30501) |
| `arc-systems` | ARC controller + listener pods |
| `arc-runners` | ARC ephemeral runner pods (empty when no jobs queued) |
| `mcp` | Kubernetes MCP server |

---

## Key Design Decisions

**btrfs reflink over CDI/PVC** — Golden disk is a single `.raw` file on `hostPath`.
Each test run reflinking it takes ~24ms (copy-on-write, near-zero extra disk). No
CDI DataVolume overhead, no registry round-trips. Teardown is `rm -f disk.raw`.

**No persistent test VMs** — All VMs are ephemeral. Every pipeline provisions a
fresh VM on start and destroys it via `onExit` handler. `just list-vms` should
show zero VMs when no workflows are running.

**API-only operator model** — All cluster reads and mutations go through the
Kubernetes API (MCP tools or `just` wrappers). No SSH to the cluster host for
operations. The only SSH in this system is **in-cluster**: workflow pods SSHing
into freshly-booted test VMs to run behave steps.

**One administration pane** — KubeStellar Console is the private UI for the
canonical local `ghost` k3s topology. Astro reports public read-only status,
and Prometheus supplies backend metrics. New operational views belong in the
Console rather than Grafana or another general-purpose dashboard framework.

**WorkflowTemplate over inline DAG** — All reusable pipeline logic lives in
`WorkflowTemplate` objects in `argo/workflow-templates/`. Submit-time `Workflow`
files in `argo/` reference templates via `workflowTemplateRef` or `templateRef`.
This lets ArgoCD own the template lifecycle while keeping submission flexible.

---

## Writing New Tests

1. Add a `.feature` file under `tests/<suite>/features/`.
2. Add step definitions in `tests/<suite>/features/steps/`.
3. Tag new scenarios `@wip` until stable.
4. Submit a run: `just run-tests` (smoke) or `just run-tests-tag lts-testing`.

See [`projectbluefin/testsuite`](https://github.com/projectbluefin/testsuite) for AT-SPI test authoring.

---

## Documentation Map

| Doc | Purpose |
|---|---|
| [README.md](README.md) | Architecture overview (this file) |
| [AGENTS.md](AGENTS.md) | Agent entry point |
| [docs/reference/WORKFLOWS.md](/docs/reference/WORKFLOWS.md) | WorkflowTemplate submit interface / agent contract |
| [docs/reference/workflow-reference.md](/docs/reference/workflow-reference.md) | Full WorkflowTemplate reference |
| [docs/reference/bluefin-integration.md](/docs/reference/bluefin-integration.md) | Image-poll → container test → result publication pipeline |
| [docs/ops/bootstrap.md](/docs/ops/bootstrap.md) | How to replicate this lab from scratch |
| [docs/ops/RUNBOOK.md](/docs/ops/RUNBOOK.md) | Timeless architecture + failure-mode reference |
| [docs/reference/agent-cheatsheet.md](/docs/reference/agent-cheatsheet.md) | Canonical command reference |
| [docs/ops/lab-operations.md](/docs/ops/lab-operations.md) | Long-form operator procedures |
| [projectbluefin/testsuite](https://github.com/projectbluefin/testsuite) | GUI test authoring + debugging |

---

## Related Projects

- [Project Bluefin](https://projectbluefin.io) — primary subject under test; selected image-poll lanes validate new digests
- [ublue-os/bluefin](https://github.com/ublue-os/bluefin) — upstream Bluefin image builds
- [Project Dakota](https://github.com/projectbluefin/dakota) — BST-built Bluefin variant; Dakota PRs use the lab-authoritative [Dakota PR review process](docs/skills/dakota-pr-review/SKILL.md) and `dakota-qa-pipeline`
- [projectbluefin/testsuite](https://github.com/projectbluefin/testsuite) — shared behave suites and container QA inputs
- [projectbluefin/actions](https://github.com/projectbluefin/actions) — shared GitHub Actions workflows around the release pipeline
- [bootc](https://containers.github.io/bootc/) — image-based Linux standard
- [KubeVirt](https://kubevirt.io) — CNCF Incubating, VM workloads on Kubernetes
- [Argo Workflows](https://argoproj.github.io/argo-workflows/) — CNCF Graduated
- [Argo CD](https://argo-cd.readthedocs.io) — CNCF Graduated
- [k3s](https://k3s.io) — CNCF Sandbox
