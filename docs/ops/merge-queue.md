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
`docs/` data straight to `main`. Machine-generated data does not go through the
queue, so that push relies on a **bypass actor**:

| Actor | Used by |
|---|---|
| `OrganizationAdmin` | in-cluster Argo pushes (`scripts/publish_test_results.py`, `argo/workflow-templates/*cve-scan.yaml`) |
| `DeployKey` | `update-test-results.yml`, via the `DASHBOARD_DEPLOY_KEY` secret |

The default `GITHUB_TOKEN` holds **no** bypass; pushing with it fails with
`GH013: Repository rule violations found for refs/heads/main`. The ingestion job
therefore rewrites its remote to SSH and authenticates with a repository deploy
key that has write access.

### Why generated data does not go through the queue

Routing it through a bot pull request does not work, and the failure is not
obvious:

- `GITHUB_TOKEN` may not start workflows, so a bot-opened pull request gets no
  `pull_request` run of `Lint` and the required `lint` context never reports.
- `workflow_dispatch` is exempt from that restriction, but a dispatched run's
  check is not associated with the pull request, so it cannot satisfy the
  requirement either. Enqueueing fails with
  `Pull request Required status check "lint" is expected`.
- Mirroring the dispatched run into a commit status *does* satisfy the pull
  request, but the merge group the bot then creates gets no `merge_group` run
  for the same reason, so the entry sits at `AWAITING_CHECKS` until it times
  out.

A bypass actor is the only mechanism that works end to end for machine-generated
commits. Human and Renovate pull requests still go through the queue normally.

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

### Baseline test failures deadlock every open PR

If `test-validation` fails on **every** open PR with an identical set of
failures, suspect a broken baseline on `main` rather than the PRs. Confirm it
against a clean checkout before investigating any individual branch:

```bash
git worktree add /tmp/wt-baseline origin/main --detach
cd /tmp/wt-baseline && python3 -m pytest -q tests/unit
```

Pre-existing failures on `main` create a deadlock that no single PR can escape:
each fix-PR clears only a subset, so its own required check still fails, so it
cannot merge, so the baseline is never repaired. Splitting the fixes across
separate PRs makes this worse, not better.

Resolve it by landing **one** PR that greens the whole suite. Keep it
test-only — no manifests, no production code — so it can be reviewed quickly
and carries no cluster risk. Where an assertion encodes a policy that was never
actually implemented, mark it `xfail(strict=False)` with a reason naming the PR
that implements it, rather than deleting the assertion or weakening it to pass:

```python
@pytest.mark.xfail(strict=False, reason="Policy unimplemented; see PR #NNN")
```

`strict=False` matters — the implementing PR will make the test pass, and a
strict marker would then fail the suite it was meant to protect. Remove the
marker in that PR.

Do not reach for `--admin` to break the deadlock; bypassing the queue is
prohibited, and the underlying red baseline would survive the merge.

The Ruleset is configured in GitHub repository settings rather than in ArgoCD;
validate it with:

```bash
gh api repos/projectbluefin/lab/rulesets --paginate
gh api repos/projectbluefin/lab/rulesets/<id>
```
