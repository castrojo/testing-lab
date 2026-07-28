# Lab pull-request merge queue

`main` is protected by the active `main — merge queue` GitHub Ruleset. Incoming
pull requests must enter the queue; do not bypass the queue for routine merges.

## Policy

| Setting | Value |
|---|---|
| Target | `main` |
| Required check | `lint` |
| Required approvals | 0 (the lab's approved bot/dependency lane) |
| Merge method | Squash only |
| Queue grouping | `ALLGREEN` |
| Queue capacity | 5 entries / 5 entries per group |
| Queue wait | 0 minutes |
| Queue check timeout | 30 minutes |
| Branch updates | Non-fast-forward updates blocked; branch deletion enabled after merge |

Path-filtered workflows such as `Docs` and `Test Suite Validation` are not
required ruleset checks. The always-on `Lint` workflow is the queue gate.

## Required workflow invariant

Every workflow named in `required_status_checks` must run for both ordinary pull
requests and merge groups:

```yaml
on:
  pull_request:
    branches: [main]
  merge_group:
    types: [checks_requested]
```

A `pull_request` trigger alone does not run on the temporary
`gh-readonly-queue/main/...` ref. The queue then remains in `AWAITING_CHECKS`
without a check run. See the [GitHub Actions `merge_group` documentation](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#merge_group).

## Automated data pushes

Nothing pushes to `main` from GitHub Actions. The default `GITHUB_TOKEN` holds
**no** ruleset bypass, so a direct push fails with `GH013: Repository rule
violations found for refs/heads/main`.

The dashboard ingestion workflow (`update-test-results.yml`) therefore publishes
through the queue like any other change: it force-pushes regenerated `docs/`
data to `bot/dashboard-data`, opens or reuses a pull request, and enables
auto-merge. The branch is rebuilt from `main` on every run and every file is
regenerated, so the force-push cannot lose work.

A pull request opened with `GITHUB_TOKEN` receives no `pull_request` check runs —
GitHub does not let one workflow trigger another. That is fine here, because the
ruleset evaluates `lint` against the **merge group**, not the pull request. The
queue still gates the merge.

Only two actors hold a bypass, and neither is available to Actions:

| Actor | Used by |
|---|---|
| `OrganizationAdmin` | in-cluster Argo pushes (`scripts/publish_test_results.py`, `argo/workflow-templates/*cve-scan.yaml`) |

If an automated job reports `GH013`, it is pushing to `main` directly. Route it
through `bot/dashboard-data` instead of reaching for a bypass token.

## Queueing a PR

Only queue a PR after its review/approval policy is satisfied and its required
checks pass:

```bash
gh pr checks <number> --repo projectbluefin/lab
gh pr merge <number> --repo projectbluefin/lab --auto --squash
```

Verify the queue entry through GraphQL when troubleshooting:

```bash
gh api graphql -f query='query { repository(owner:"projectbluefin", name:"lab") { pullRequest(number:<number>) { mergeQueueEntry { position state enqueuedAt } } } }'
```

Do not use `--admin` for routine dependency or feature PRs. The organization
administrator bypass exists only for an emergency or for bootstrapping a
configuration change that must land before the queue can validate itself.

## Troubleshooting

- `Protected branch rules not configured for this branch` means the Ruleset is
  missing or inactive; inspect `gh api repos/projectbluefin/lab/rulesets`.
- `AWAITING_CHECKS` with no `merge_group` Actions run means the required
  workflow is missing the `merge_group` trigger.
- `DIRTY` or `UNMERGEABLE` after an earlier queue merge means the PR branch is
  stale. Update it against current `main`, rerun `lint`, and requeue it.
- `GH013: Repository rule violations found` from an automated job means it is
  pushing to `main` with `GITHUB_TOKEN` instead of a bypass-capable token. See
  [Automated data pushes](#automated-data-pushes).
- Never remove the required check just to make the queue advance.

The Ruleset is configured in GitHub repository settings rather than in ArgoCD;
validate it with:

```bash
gh api repos/projectbluefin/lab/rulesets --paginate
gh api repos/projectbluefin/lab/rulesets/<id>
```
