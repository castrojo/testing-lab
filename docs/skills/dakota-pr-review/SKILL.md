---
name: dakota-pr-review
description: >
  Review Dakota pull requests through the lab-backed workflow: dispatch the Dakota
  QA run, keep maintainer decisions tied to fresh evidence, and hand off a clear
  operator summary. Use when a Dakota PR needs lab validation or triage.
metadata:
  context7-sources:
    - /argoproj/argo-workflows
    - /websites/github_en_actions
---

# Dakota PR Review — lab Skill

## When to Use

- Reviewing a PR in `projectbluefin/dakota`
- Deciding whether a Dakota PR is mergeable from the lab side
- Triaging a Dakota PR that failed, stalled, or never got a lab run

## When NOT to Use

- Reviewing non-Dakota repositories → `ci-tooling.md` or `argo-workflows.md`
- Merging a PR directly without a linked lab run
- Reworking the Dakota build pipeline itself → `argo-workflows.md`

---

## Core Process

Dakota PR review is a lab-backed admission process. GitHub Actions status is advisory when the repository's known CI/merge-queue path is broken; the lab result is the merge gate for this workflow.

1. Confirm the PR number, target branch, head SHA, and that it is still open and mergeable.
2. Do not treat `pr/needs-review`, `automerge`, or `chore/deps` labels as approval. Verify maintainer approval when the repository policy requires it.
3. Build the exact PR head SHA with `dakota-build-pipeline` in distributed (`re`) mode. Use the live WorkflowTemplate parameters; do not guess them.
4. Test the resulting image with `dakota-container-qa-pipeline` (image smoke checks) and, when the image supports it, `dakota-qa-pipeline`/`run-container-tests` for the full containerized BDD/GUI suites.
5. Keep each build serialized by the `bst-build` semaphore. Never start competing Dakota BST builds; queue them and process one PR at a time.
6. Capture Argo workflow names, exact SHAs, build mode, test result, and failure logs. A pass must be tied to the same commit that will be merged.
7. If build and E2E pass, recheck the PR head and mergeability, then merge directly. Do not use GitHub merge queue for this process: queue promotion has historically evaluated the wrong/stale commit when GHA is failing.
8. If lab validation fails, do not merge. Classify infrastructure failures separately from source/test failures and rerun only after the blocker is fixed.

## Merge Policy

- The lab is authoritative for this operator flow when Dakota GHA checks are known to fail for unrelated CI or merge-queue reasons.
- Direct merge is allowed only after the exact PR SHA has a successful distributed build and successful required lab E2E/smoke coverage.
- Merge the current head only; if the PR changes after testing, discard the evidence and rerun.
- Do not merge a PR with an unresolved source/test failure, a stale result, or an infrastructure run that never reached the test phase.
- Keep one active Dakota build at a time; QA workflows may run concurrently only when they do not contend for the BST build semaphore.

## Operator Flow

1. List open Dakota PRs and group them by base branch; process oldest/queue-ready first.
2. For each PR, inspect the current head SHA and changed paths, then dispatch the distributed build from the exact branch/ref.
3. Wait for `dakota-build-pipeline` to complete. A parent workflow marked successful with failed child nodes is not a clean pass; inspect the node summary and logs.
4. Run `dakota-container-qa-pipeline` against the built local registry image. Run the full `dakota-qa-pipeline` suites when the PR needs GUI/BDD coverage.
5. Record pass/fail evidence and stop on infrastructure failures such as BuildBarn storage/DNS or worker loss; recover the lab before retrying.
6. On a clean pass, re-read the PR head SHA, confirm it remains mergeable, and merge directly rather than entering merge queue.
7. After merge, verify the merge commit and watch the next build/publish workflow; do not report success merely because the merge API accepted the operation.

## Known Lab Lessons

- The `dakota-pr-import-poller` historically watched `test-on-lab`, while the active Dakota queue uses `clanker-queue`; inspect the live poller/template before assuming labels will dispatch work.
- A Dakota image may have an empty OCI `Cmd`. `run-container-tests` must pass `/sbin/init` explicitly after the image reference or crun fails before systemd starts.
- BuildBarn storage/worker DNS or disappearance errors are infrastructure failures, not PR failures. Confirm storage StatefulSet pods, frontends, scheduler, workers, and the `bst-build` semaphore before retrying.
- A successful Argo parent can hide failed child build nodes. Read the node summary and logs.
- GitHub merge queue is not the merge path for this operator flow when GHA is failing; direct merge follows fresh lab evidence and an exact-SHA recheck.

## Red Flags

- No linked Dakota workflow for a PR that is otherwise ready to merge
- Workflow fails before the Dakota tests run
- Workflow logs contain authenticated command lines, token-bearing variables, or unredacted API output
- A routine Renovate label is treated as maintainer approval
- Operator report lacks evidence or a workflow link
- PR is merged while the latest lab run is still failing or missing

## Verification

- [ ] PR head SHA, base branch, mergeability, and approval state were verified
- [ ] Distributed `dakota-build-pipeline` passed for the exact PR SHA
- [ ] Dakota smoke/E2E workflow passed for the resulting image
- [ ] BuildBarn and Argo child nodes were checked; no hidden failed nodes remain
- [ ] Workflow logs were checked for secret redaction before linking them
- [ ] If merged, the merge was direct (not merge queue) and the post-merge workflow was checked
