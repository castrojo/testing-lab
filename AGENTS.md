# Lab — Agent Entry Point

This repo is the infrastructure for automated Bluefin testing: Argo Workflows,
ArgoCD, KubeVirt, and the manifests that run the lab test cluster.

## Start here

1. Read this file.
2. Find the skill for the area you need in [`docs/SKILL.md`](docs/SKILL.md)
   (the skill router, same convention as `projectbluefin/common`) and load
   only that skill.
3. For deterministic commands, check the [`Justfile`](Justfile) or
   [`docs/reference/agent-cheatsheet.md`](docs/reference/agent-cheatsheet.md).
4. For operational failure modes, read [`docs/ops/RUNBOOK.md`](docs/ops/RUNBOOK.md).
5. Before changing anything, check the latest project context in
   [`docs/reference/ubiquitous-language.md`](docs/reference/ubiquitous-language.md).

## What this repo is

- **Factory** — the org-wide OS delivery system across `projectbluefin/bluefin`,
  `bluefin-lts`, `dakota`, and `common`, plus their CI pipelines.
- **Lab** — this repo's QA cluster. Lab data supplements factory data on the
  dashboard and is always labeled as lab-sourced, never as factory health itself.
- **Lane** — a `(variant, branch)` pair such as `bluefin-testing` or
  `bluefin-lts-stable`; the unit of tracking.
- **Release verdict** — per-lane judgment of the latest published digest:
  **good** iff the build succeeded, the lab QA pipeline passed against that
  digest, and cosign signature verification passes. New CVEs are displayed
  alongside the verdict but do not gate it.
- **Operator surfaces** — KubeStellar Console is the sole private
  cluster-administration and single-pane UI. Astro is the public read-only
  reporting surface, and Prometheus is a backend metrics service only.

## Build / test / lint

```bash
just lint              # actionlint + argo lint + registry allowlist
# dashboard build check (when changing src/)
npm ci && npm run build
```

## Boundaries

- `main` uses the GitHub merge queue. When a change passes local validation (`just lint`, `npm test`, `pytest`), agents MUST immediately create and queue PRs to `main` via `gh pr merge <number> --auto --squash`. Do not pause or delay merging verified GitOps changes.
- Do not `kubectl apply` WorkflowTemplates — ArgoCD owns them.
- Do not introduce Grafana or another general-purpose cluster-admin/dashboard
  framework alongside KubeStellar Console.
- Treat the local `ghost` k3s topology as canonical; do not design lab
  automation around cloud or external multi-cluster assumptions.
- Do not SSH into cluster nodes from a workstation; CLI access is via `just`,
  `argo`, and `kubectl`.
- Do not commit transient session artifacts (`TODO-*.md`, `poll-*.log`,
  stale screenshots, etc.) to this repo.
- Cluster-specific hostnames, IPs, and one-off incident notes belong in private
  runbooks, not in this repository.
- Issue tracker for this repo is `projectbluefin/lab`.

## When to Use / When NOT to Use

| Task | Where to go |
|---|---|
| Which skill should I load? | [`docs/SKILL.md`](docs/SKILL.md) |
| Authoring Argo workflow templates (YAML) | [`docs/skills/argo-workflows/SKILL.md`](docs/skills/argo-workflows/SKILL.md) |
| KubeVirt VM provisioning / boot failures | [`docs/skills/kubevirt-vms/SKILL.md`](docs/skills/kubevirt-vms/SKILL.md) |
| ArgoCD sync, GitOps rules, bootstrap vs managed | [`docs/skills/gitops-argocd/SKILL.md`](docs/skills/gitops-argocd/SKILL.md) |
| GNOME behave/qecore/dogtail tests | [`docs/skills/test-authoring/SKILL.md`](docs/skills/test-authoring/SKILL.md) |
| Astro dashboard pages, charts, visual design | [`docs/skills/astro-dashboard-pages/SKILL.md`](docs/skills/astro-dashboard-pages/SKILL.md) |
| Cluster add-ons, k3s, registries, K8sGPT | [`docs/skills/cluster-tooling/SKILL.md`](docs/skills/cluster-tooling/SKILL.md) |
| Flatcar node onboarding | [`docs/skills/flatcar-node-onboarding/SKILL.md`](docs/skills/flatcar-node-onboarding/SKILL.md) |
| KubeStellar core, WECs, BindingPolicies | [`docs/skills/kubestellar/SKILL.md`](docs/skills/kubestellar/SKILL.md) |
| KubeStellar Console deploy/auth/cards | [`docs/skills/console-dashboard/SKILL.md`](docs/skills/console-dashboard/SKILL.md) |
| Add/remove nodes or WECs, BST grid scaling | [`docs/skills/node-lifecycle/SKILL.md`](docs/skills/node-lifecycle/SKILL.md) |
| End of session write-back loop | [`docs/skills/meta-skill-improvement/SKILL.md`](docs/skills/meta-skill-improvement/SKILL.md) |
| Workflow parameter contracts | [`docs/reference/WORKFLOWS.md`](docs/reference/WORKFLOWS.md) |
| Release verdict / dashboard data contracts | [`docs/adr/0002-release-verdict-definition.md`](docs/adr/0002-release-verdict-definition.md), [`docs/reference/page-contracts.md`](docs/reference/page-contracts.md) |
| Architecture / failure modes | [`docs/ops/RUNBOOK.md`](docs/ops/RUNBOOK.md) |
| GitHub `main` merge queue / branch ruleset | [`docs/ops/merge-queue.md`](docs/ops/merge-queue.md) |
| Human contributor workflow | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## Skill maintenance

At the end of any non-trivial session, update the skill file for the area you
changed if you learned something new. Every session produces two outputs: the
work and the learning.
