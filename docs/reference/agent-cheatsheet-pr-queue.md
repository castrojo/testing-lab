---
name: agent-cheatsheet-pr-queue
description: >
  PR queue / verification report notes extracted from the agent cheatsheet.
---

# PR Queue Mode — Vanguard Lab Strike Report


Mandatory gate for `knuckle`, `dakota`, and this repo's PRs.

1. Run the lab loop end-to-end — `just run-tests-tag testing` minimum, `just run-tests-matrix` for high-risk changes.
2. Collect **real evidence** using CLI tools:
   - Workflow status/steps → `argo get -n argo <name>` / `argo list -n argo`
   - Log output → `argo logs -n argo <name>`
   - Pod state → `kubectl get pods -n argo`
   - VMI state only for VM-backed lanes → `kubectl get vmi -A`
3. Keep the PR comment minimal: what ran, pass/fail, and blockers only. Never paste raw Argo logs until they have been checked for credentials. This is a *manual reviewer comment*, distinct from the automated `testing-lab / <repo>` Check Run the `pr-label-poller` posts — do not restate the automated check's result.
4. Treat `pr/needs-review` as a hard human gate; `automerge` and `chore/deps` do not prove maintainer approval.
5. Only then apply `agent-tested` and approve / queue.

Hard exit checklist:

- [ ] Real lab evidence exists for the lane under test.
- [ ] Evidence was collected via CLI tools (`argo`, `kubectl`).
- [ ] The entire loop was tested, not isolated commands.
- [ ] A canonical Vanguard report with real data is posted on the PR.
- [ ] Argo logs contain no GitHub tokens or authenticated command lines.
- [ ] Any blocker is filed as an issue in the owning repo.

---
