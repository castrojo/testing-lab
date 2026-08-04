---
name: meta-skill-improvement
description: >
  Self-learning and skill-maintenance loop for the lab. Run at the
  end of every session that produces non-trivial work, and at the **start**
  of any session to check unanalyzed nightly failures. Enforces the
  write-back loop so the next agent starts smarter than this one did.
  Use when wrapping up any session, before creating a PR, at session start
  to check failures, or when a pattern was discovered through trial and error.
---

# Skill Improvement — lab Self-Learning Loop

Every session produces exactly **two** outputs: the work and the learning.
Output 1 without Output 2 leaves the lab no smarter than before you arrived.

**Canonical skill standard:** [`/addyosmani/agent-skills`](https://context7.com/addyosmani/agent-skills)

---

## When to Use

- **Start of session**: check nightly failures from the past 7 days before doing new work
- End of any session that produced non-trivial code, config, or ops work
- Before creating a PR or marking work done
- When a pattern was discovered by trial and error
- When a tool, API, or cluster behaviour surprised you

## When NOT to Use

- Read-only research sessions with no output
- Doc-only passes that update no skill file and create no new pattern

---

## Core Process

### Step 0 — Check for unanalyzed nightly failures (session start)

Nightly failures are retained for 7 days. Check before starting new work:

```text
argo-mcp-list_workflows namespace=argo status=Failed
```

For each failed workflow:
1. `argo-mcp-logs_workflow <name>` — find the failing step and error message
2. Route to the relevant skill file (Step 1 routing table below)
3. If pattern is already in the skill — the fix may be straightforward; do it now
4. If pattern is new — fix it, then update the skill file before ending the session
5. File a GitHub issue for infra work that goes beyond this session

> Failures older than 7 days are gone. The self-improving loop only works if
> someone runs Step 0 before Argo cleans them up.

### Step 1 — Skill routing

Route by area changed:

| Area changed | Skill file to update |
|---|---|
| `argo/workflow-templates/` or `argo/*.yaml` | [`argo-workflows/SKILL.md`](../argo-workflows/SKILL.md) |
| `provision-vm`, `KubeVirt`, `btrfs reflink` | [`kubevirt-vms/SKILL.md`](../kubevirt-vms/SKILL.md) |
| ArgoCD Applications, `argocd/`, `manifests/` | [`gitops-argocd/SKILL.md`](../gitops-argocd/SKILL.md) |
| `tests/`, `behave`, `dogtail`, `qecore` | [`test-authoring/SKILL.md`](../test-authoring/SKILL.md) |
| `.github/workflows/` | [`ci-tooling/SKILL.md`](../ci-tooling/SKILL.md) |
| `scripts/refresh_factory_stats.py` or `scripts/generate_page_datasets.py` | [`ci-tooling/SKILL.md`](../ci-tooling/SKILL.md) (+ [`astro-dashboard-pages/SKILL.md`](../astro-dashboard-pages/SKILL.md) when page contracts/rendering changed) |
| Repo-wide agent behavior or session hygiene | [`AGENTS.md`](../../../AGENTS.md) |
| Bootstrap cluster setup, `argo/bootstrap/` | [`docs/ops/bootstrap.md`](../../ops/bootstrap.md) |
| Cluster topology, namespaces, RBAC | [`docs/ops/architecture.md`](../../ops/architecture.md) |
| Agent operations, MCP tools | [`docs/reference/agent-cheatsheet.md`](../../reference/agent-cheatsheet.md) |
| Failure modes, architecture | [`docs/ops/RUNBOOK.md`](../../ops/RUNBOOK.md) |

Ask: *"If this finding had been in the skill file when I started, would I have avoided the trial-and-error?"* If yes — update the skill file.

### Step 2 — Context7 freshness

Whenever a skill file covers a named library or tool, verify its examples against current docs **before** updating:

```
DETECT → FETCH → EMBED → CITE
```

1. `resolve-library-id("<library>")` → get the Context7 library ID
2. `query-docs("<id>", "<specific pattern>")` → fetch current docs
3. Embed the verified pattern in the skill
4. Note the library ID in frontmatter: `context7-sources: [/org/project]`

Key library IDs for this repo:
- Argo Workflows: `/argoproj/argo-workflows`
- Argo CD: `/argoproj/argo-cd`

### Step 3 — Canonical skill spec audit

Every skill file must meet the [`/addyosmani/agent-skills`](https://context7.com/addyosmani/agent-skills) standard:

```
✓ Frontmatter: name + description with "Use when" trigger phrases
✓ ## When to Use
✓ ## When NOT to Use
✓ ## Core Process  (numbered workflow)
✓ ## Common Rationalizations  (excuses + rebuttals)
✓ ## Red Flags  (anti-patterns)
✓ ## Verification  (exit criteria checklist)
```

### Step 4 — Documentation hygiene

Skill files are **evergreen procedures**. Remove from any skill file:

| ✗ Remove | ✓ Where it belongs |
|---|---|
| Resolved items (`✅ done`, `PR#123 merged`) | Git history |
| Running gap tables with live issue numbers | GitHub issues |
| Session-dated entries (`2026-06-11: found X`) | Extract the timeless pattern; drop the date |
| Current PR numbers as inline state | File a GitHub issue; link from there |

The model test: *"Read this as a fresh agent with zero session context. Does it tell you how to operate — or does it make you dig through history?"*

### Step 5 — Live gaps → GitHub issues

Any gap, blocker, or open question you cannot fix this session:

```bash
gh issue create --repo projectbluefin/lab \
  --title "infra: <what is broken>" \
  --label "bug" \
  --body "What: ...\nFix: ...\nAutomatable: yes/no"
```

**Do not append gaps to skill files.** Skill files are not backlogs.

### Step 6 — Factory closure (required before handoff)

Before ending any non-trivial session:

1. Write the durable pattern into the routed skill file.
2. If the pattern changes how future agents should operate in this repo, update `.github/copilot-instructions.md` too.
3. If the session touched Pages or generated dashboard data, verify the live build/render still matches the published files.
4. Do not hand off with only code changes; the skill/docs write-back is part of the deliverable.

---

## Skill directory

All skill files live in `docs/skills/`:

```
docs/skills/
├── _template/SKILL.md           Copy this to add a new skill
├── argo-workflows/              WorkflowTemplate authoring and patterns
├── ci-tooling/                  GitHub Actions and CI reliability
├── cluster-tooling/             k3s, storage, BuildStream, node recovery
├── flatcar-node-onboarding/     Flatcar nodes, Nebraska, custom kernels
├── gitops-argocd/               ArgoCD sync, manifests, image policy
├── kubevirt-vms/                Ephemeral VM lifecycle and troubleshooting
├── meta-skill-improvement/      This file — the self-improvement loop
├── test-authoring/              behave/qecore/dogtail and bootc contract tests
└── README.md                    Skill index
```

---

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I'll update the skill next session." | You won't. The loop only compounds if agents write back immediately. |
| "I already know Argo Workflows — no need for Context7." | Training data is stale. Fetch and verify before embedding examples. |
| "The skill file is good enough." | Check for Red Flags and Verification sections — most common gaps. |
| "This pattern is obvious — no need to document it." | If you found it by trial and error, it isn't obvious. Write it. |

## Red Flags

- Session ending with no skill update after discovering a non-obvious pattern
- A pattern being fixed a second time that wasn't written down after the first
- Session ending after Pages/dashboard work without a skill or instructions write-back
- Skill files with no `## Verification` section
- Skill files covering Argo or ArgoCD with unverified code examples
- Any `docs/skills/*/SKILL.md` file missing the canonical sections

## Verification

Before marking any session done:

- [ ] Skill file updated for the area worked in (or created if missing)
- [ ] Code examples verified against Context7 if the skill covers a library
- [ ] Library ID noted in `metadata.context7-sources` frontmatter
- [ ] No resolved items or dated entries left in skill files
- [ ] Any unresolved gaps filed as GitHub issues in `projectbluefin/lab`
- [ ] `.github/copilot-instructions.md` updated if the session revealed a repo-wide behavior change
- [ ] Skill file has: When to Use, When NOT to Use, Core Process, Common Rationalizations, Red Flags, Verification
