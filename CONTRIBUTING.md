# Contributing

## What this repo is
`lab` is the infrastructure repo for automated Bluefin testing: k3s, Argo Workflows, ArgoCD, KubeVirt, and supporting manifests. It is separate from `projectbluefin/testsuite`, which holds most test content.

## Who should change this repo
Most contributors should work in `projectbluefin/testsuite`. Use this repo when you are changing lab infrastructure, workflow templates, cluster manifests, or VM orchestration.

## Prerequisites
- `kubectl`
- `argocd` CLI
- access to the test cluster, or a local QEMU/KubeVirt setup you can validate against
- familiarity with the GitOps flow in `/AGENTS.md`

## Development workflow
- Read `/AGENTS.md` and `/docs/reference/agent-cheatsheet.md` first
- Make infra changes in `argo/workflow-templates/`, `manifests/`, or `argocd/`
- Validate locally with:
```bash
just lint
python3 scripts/validate-docs.py
actionlint .github/workflows/*.yml .github/workflows/*.yaml
```
- Push Git-tracked changes; ArgoCD reconciles them from `main`
- All pull requests to `main` enter the GitHub merge queue after required checks pass; see [`docs/ops/merge-queue.md`](docs/ops/merge-queue.md)

## Pull requests
- Open PRs against `main`
- Keep changes scoped and explain the operational impact
- Required-check workflows must include the `merge_group` trigger so queued PRs receive status results
- Do not treat this repo like a general-purpose newcomer project; it assumes access to the Bluefin lab and knowledge of the workflow stack
