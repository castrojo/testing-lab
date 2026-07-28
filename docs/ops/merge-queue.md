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

The dashboard ingestion workflow (`update-test-results.yml`) commits regenerated
`docs/` data directly to `main` every five minutes. Machine-generated data does
not go through the queue, so that push depends on a **bypass actor**:

| Actor | Used by |
|---|---|
| `OrganizationAdmin` | in-cluster Argo pushes (`scripts/publish_test_results.py`) |
| `mergeraptor` (Integration, app id `3069633`) | `update-test-results.yml` |

The default `GITHUB_TOKEN` has **no** bypass. A workflow that pushes to `main`
must mint a MergeRaptor installation token and hand it to `actions/checkout`:

```yaml
- name: Mint MergeRaptor token
  id: app-token
  uses: actions/create-github-app-token@<pinned-sha>
  with:
    app-id: ${{ secrets.MERGERAPTOR_APP_ID }}
    private-key: ${{ secrets.MERGERAPTOR_PRIVATE_KEY }}

- uses: actions/checkout@<pinned-sha>
  with:
    token: ${{ steps.app-token.outputs.token }}
```

The GitHub Actions app itself cannot be granted a bypass — the API rejects it
with `Actor GitHub Actions integration must be part of the ruleset source or
owner organization`.

If a data workflow reports `GH013: Repository rule violations found for
refs/heads/main`, it is pushing with `GITHUB_TOKEN`. This is silent for as long
as an earlier step is also failing, so check the push step even when the visible
failure is a test.

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
