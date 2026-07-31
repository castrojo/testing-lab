# Testing Lab Copilot Instructions

Use [`../AGENTS.md`](/AGENTS.md) for repo policy and architecture, and use [`../docs/agent-cheatsheet.md`](/docs/reference/agent-cheatsheet.md) for the canonical command reference.

Keep only these repo-specific inline reminders:

- Use `just` entrypoints first; do not duplicate command tables here.
- No SSH to ghost or exo-0.
- KubeStellar Console is the sole private cluster-admin/single-pane UI for the
  canonical local `ghost` k3s topology; Astro stays public and read-only,
  Prometheus stays backend-only, and Grafana or parallel dashboard frameworks
  are out of scope.
- No `kubectl apply` for `argo/workflow-templates/` or `manifests/`; edit git-tracked YAML and let ArgoCD reconcile it.
- VM-backed test runs use ephemeral KubeVirt VMs — no persistent titan VMs. Container-only
  Bluefin/Dakota QA does not create VMs; `just list-vms` should show empty when no VM
  workflows run.
- After pushing a fix, verify the live template via `argo-mcp-get_workflow_template` before resubmitting — templates snapshot at submit time.
- Never enable shell tracing in Argo scripts that call authenticated APIs; workflow logs must be inspected for secret redaction before being linked to PRs.
- Treat `pr/needs-review` as a hard maintainer gate; `automerge` and `chore/deps` labels alone do not authorize a PR-batch run.
- For `docs/` Pages changes, wait for `gh api repos/<owner>/<repo>/pages/builds/latest --jq '.status'` to be `built` and confirm the live page shows the new HTML/JS before claiming success.
- Keep generated dashboard JSON in sync with the workflow that writes it; missing data should render an explicit unavailable state, not disappear silently.
- Wide dashboard tables belong in full-width cards; if a table has many columns, let the card span the full grid row so the columns can scroll instead of collapsing.
- Compare existing subdomains before DNS/TLS changes; keep static GitHub Pages hosts behind the established Cloudflare-proxied pattern unless GitHub cert issuance is confirmed.
- If Cloudflare fronts a Pages site, opt the dashboard entry script out of Rocket Loader with `data-cfasync="false"` and verify the live HTML preserves the raw script tag.
- PR queue work is only complete with real lab evidence.
- `main` uses the GitHub merge queue; queue routine PRs with `gh pr merge <number> --auto --squash`, never `--admin`.
- Any workflow named in the `main` ruleset's required checks must include `merge_group: {types: [checks_requested]}` as well as its normal `pull_request` trigger, or queued PRs wait forever in `AWAITING_CHECKS`.
- ARC container-mode runners require a `container:` block on every job and offload heavy work to Argo Workflows; point maintainers to `/docs/ops/maintainer-onboarding.md` for access/auth and the personal-repo scale-set pattern.
- At end of any non-trivial session, run the self-improvement loop in `/docs/skills/meta-skill-improvement/SKILL.md` and update the relevant skill file(s) with the durable pattern before handoff.
