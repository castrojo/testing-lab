---
name: ci-tooling
description: >
  GitHub Actions workflow authoring and debugging for lab dashboards and
  automation. Use when changing .github/workflows/, troubleshooting stale Pages
  data, or wiring CI jobs that need private-cluster data.
metadata:
  context7-sources:
    - /websites/github_en_actions
    - /websites/github_en_rest
    - /websites/git-scm
    - /tailscale/tailscale
---

# CI Tooling — GitHub Actions in lab

## When to Use

- Editing `.github/workflows/*.yml`
- Dashboard data is stale, empty, or inconsistent with cluster state
- A workflow needs homelab/private network data
- GitHub Pages shows JSON/JS fetch errors after CI changes
- Onboarding maintainers to the `ghost-runners` ARC scale set or adding a personal-repo scale set

## When NOT to Use

- Argo WorkflowTemplate logic in `argo/workflow-templates/` (use `argo-workflows.md`)
- ArgoCD reconciliation policy work (use `gitops-argocd.md`)
- VM lifecycle/scheduling behavior (use `kubevirt-vms.md`)

## Core Process

1. Confirm runner network model first: GitHub-hosted runners have public internet by default; private network access requires an overlay/VPN setup or a self-hosted runner.
2. For dashboard stats jobs, treat private-cluster snapshots as optional: when live fetch fails, preserve last known live values and set explicit freshness/state flags.
3. Never wipe `recent_runs` or `factory.cluster.nodes` just because a hosted runner cannot reach `<private-subnet>.x.x`; preserve and annotate.
4. Add explicit metadata in JSON (`_meta.live_snapshot_ok`, `_meta.refreshed_at`) so UI can show freshness honestly.
5. For GitHub Pages sites, verify the Pages source and build state before assuming a push is live:
   - `gh api repos/<owner>/<repo>/pages --jq '.source'`
   - `gh api repos/<owner>/<repo>/pages/builds/latest --jq '.status'`
   - Pages can legitimately stay in `building` for a while even after the commit is on `main`.
6. If Cloudflare fronts a Pages site, inspect the live HTML for Rocket Loader rewrites (`data-cf-settings`, `type="...-text/javascript"`). Opt the dashboard entry script out with `data-cfasync="false"` when RL rewrites break execution.
7. For dashboard data jobs, confirm the producer workflow published every generated JSON artifact the UI depends on before declaring a section broken; missing data should render an explicit unavailable state rather than disappearing.
8. For browser-side `fetch`, avoid custom request headers that force CORS preflight against GitHub APIs (for example `Cache-Control` request headers).
9. After push, validate production Pages with a real browser render (not raw HTML fetch only): confirm no loading placeholders and key sections render.
10. Any image freshness/status numbers shown in the dashboard must come from source-of-truth publish metadata: prefer GHCR package tag timestamps (`/orgs/{org}/packages/container/{name}/versions`) for `stable`/`testing` lane recency, then fallback to GitHub Releases metadata (`published_at`, `tag_name`, `html_url`) when package tags are not available.
11. Do not infer image freshness from Argo workflow names, run labels, or generic CI success events. Those are execution signals, not publish timestamps.
12. For every dashboard number that is not self-evident from local files, publish explicit source lineage (source URL + derivation input) or hide the number as unavailable.
13. For page-owned dashboard JSON (`docs/data/*-status.json`, `*-matrix.json`), keep row-oriented contracts stable: every summary metric and every row gets `source_url`, `collected_at`, `derivation`, plus explicit `state`/`state_reason` fields so later collectors can fill values without redesigning the shape.
14. Extract large inline scripts (especially Python/bash blocks over ~10-15 lines) from GHA YAML workflow files into standalone executable scripts under `scripts/`. This enables independent local execution, linting, testing, and modular maintenance.
15. Configure explicit GHA concurrency limits (`concurrency:`) on any automated workflow that commits/pushes files back to git. Use a unique group name (e.g. `group: update-test-results`) and set `cancel-in-progress: true` to prevent race conditions and rebase conflicts when multiple runs trigger in rapid succession.
16. After changing collector logic (`scripts/refresh_factory_stats.py` or `scripts/generate_page_datasets.py`), run the same local refresh sequence as CI (`refresh_factory_stats.py` → `generate_page_datasets.py` → `npm run build`) before handoff.
17. **Network timeouts are mandatory for any cluster-facing call in CI**: every `execSync`, `fetch`, `curl`, `skopeo`, or similar command that reaches `<lab-subnet>.x` or any private endpoint must have an explicit timeout (e.g. `timeout: 2000` for Node.js, `--max-time` for curl, or socket timeout). Without timeouts, a single unreachable endpoint will hang the entire GitHub-hosted runner indefinitely, starving the concurrency group and blocking all downstream runs.
18. **Build-time assertions must handle environment drift**: if a test asserts presence of specific live data (e.g. `:30501` registry port), but the build environment differs from the test environment (GitHub runners → no homelab network access), the assertion will always fail. Instead: allow the code to handle missing data gracefully (e.g. fallback rendering), and update the assertion to accept both the live path and the fallback path. This prevents false CI failures that don't reflect real code bugs.
19. **Prefer the Kubernetes API server's service/pod proxy subresource over new NodePorts/manifests** when a collector needs to reach a ClusterIP-only service or a pod's non-Service-exposed diagnostics port: `kubectl get --raw "/api/v1/namespaces/<ns>/services/<svc>:<port>/proxy/<path>"` or `.../pods/<pod>:<port>/proxy/<path>`. This routes through the API server the runner already reaches (the same reachability `kubectl get nodes` relies on), so no cluster manifest changes are needed to expose a new metrics/status endpoint.
20. When a data source has no direct "value used" gauge (e.g. Buildbarn's block-device-backed storage, which preallocates fixed-size blocks on disk regardless of logical fill), derive an estimate from available counters (e.g. `allocations_total - releases_total` × known block size) and say so explicitly in the row's `derivation` field. Never silently report a physical-allocation number as if it were logical usage.
21. **Use a dedicated CI-only Tailscale tailnet to bridge GitHub-hosted runners into the homelab only for best-effort seeding jobs**, never for required product gates. The production tailnet must remain separate. Pin `tailscale/github-action` to a SHA, use an OAuth client scoped to `auth_keys` write and a dedicated tag (e.g. `tag:ci-cache-seeder`), and start the runner with `--accept-routes=false --ssh=false`. Tag the runner hostname deterministically so Ghost can push to it via MagicDNS. Authenticate each seed request with a short-lived signed JWT and verify it on Ghost together with the Tailscale peer tag via `tailscale whois`. Keep the upstream cache signing key on the runner only; Ghost pushes artifacts to the runner's local cache unsigned, and the runner alone signs and pushes to the upstream cache. Wrap every seeding step and the whole job in `continue-on-error: true`, set explicit `timeout-minutes`, and run teardown with `if: always()` so any Tailscale, network, or cache failure leaves the parent workflow green.
22. **Use ARC container mode (`type: kubernetes`) to keep the runner controller small while heavy work runs in separate cluster pods.** Each workflow job must declare a `container:` image. Offload CPU/memory-intensive work (BST builds, large container builds) to existing Argo WorkflowTemplates via `argo submit --from workflowtemplate/<name> --wait` rather than running it directly in the small step container. Grant the runner service account only the RBAC it needs to create and watch workflows in the target namespace.
23. **Route maintainers to `/docs/ops/maintainer-onboarding.md` for runner access.** The org `ghost-runners` scale set is bound to `https://github.com/projectbluefin` and cannot serve personal repos. A maintainer who wants `ghost-runners` on a personal repo must install the `bluefin-ghost-arc` GitHub App on their personal account and create a second scale set (`ghost-runners-personal`) with a different `githubConfigUrl` and installation secret.
24. **Accumulate run-level history as git-tracked rolling NDJSON** when a page needs trends over time. Store the file under `docs/data/history/<name>.ndjson`, one JSON object per line. Append only terminal runs, deduplicate by the producer's stable run identity (`workflow_name` for Argo; `(plane, run_id)` for generic collectors), prune lines older than the maintainer-chosen retention window, and keep the schema stable (include null placeholders for fields that sources do not yet export).
25. **Required pull-request checks must also trigger on merge groups.** Add `merge_group: {types: [checks_requested]}` beside `pull_request` for every workflow named in a branch ruleset's `required_status_checks`; a `pull_request` trigger alone leaves queued entries waiting forever because merge-queue refs do not emit ordinary pull-request events. Source: `/websites/github_en_actions`.
26. **Publish generated data to a ruleset-protected branch with a repository deploy key, not `GITHUB_TOKEN` and not the merge queue.** `GITHUB_TOKEN` holds no ruleset bypass, so a direct push fails with `GH013: Repository rule violations found`. Routing the data through a pull request instead is a dead end, because `GITHUB_TOKEN` may not start workflows: the bot-opened PR gets no `pull_request` run of the required check, a `workflow_dispatch` run's check is not associated with the PR, and — terminally — a bot-enqueued merge group gets no `merge_group` run, so the entry sits at `AWAITING_CHECKS` until it times out. Only `OrganizationAdmin` and `DeployKey` are usable bypass actors here; the GitHub Actions app is rejected outright (`Actor GitHub Actions integration must be part of the ruleset source or owner organization`). Create a write deploy key, store its private half as a repo secret, and push over SSH. Record the key and the bypass actor in `docs/ops/merge-queue.md`.
27. **Never let a private, in-cluster URL become public dashboard evidence.** Collector fallbacks that reach for an Argo run URL or a LAN address publish an unreachable link to every visitor. Gate evidence links on being publicly resolvable and emit an explicit unavailable state otherwise. This class of bug does not reproduce locally, because a developer PAT carries scopes (`read:packages`) that `GITHUB_TOKEN` lacks, so the public path succeeds on a workstation and silently falls back in CI.
28. **Derive dataset expectations from the data, never hardcode counts.** Tests that assert a fixed number of lanes or cards break the whole ingestion workflow the moment a lane is added. Read the count from the source of truth (`docs/data/variant-publishers.json`).
29. **Git-mutating workflow publishers must keep tokens out of URLs and argv.** Use a credential-free HTTPS remote plus `GIT_ASKPASS`/`GIT_TERMINAL_PROMPT=0`, with the token supplied only through the process environment. If an append-only history push loses a race, fetch/reset a disposable clone to the latest remote branch and replay the validated append before retrying; never force-push or resolve the NDJSON conflict with `-X ours`, because either can discard a concurrent record. Source: `/websites/git-scm`.
30. **Optional fleet telemetry artifacts must be capability-detected.** Run
    `scripts/collect_fleet_telemetry.py` even when the GitHub artifact/cache export
    is absent; it must publish `null` plus `state: unavailable` for active-version
    buckets and continue using only repo-tracked release, upstream, and Kubernetes
    snapshots. Never seed history with fabricated observations.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "If live fetch fails, clearing fields is safer." | Clearing makes the dashboard lie by omission; preserve last known and mark freshness/state. |
| "Custom `Cache-Control` headers are harmless." | They can trigger CORS preflight and block cross-origin GitHub API fetches. |
| "Raw JSON looks right, so UI is fine." | JS/runtime errors can still break rendering; always validate in a browser. |
| "The collector works on my machine, so CI will be fine." | A developer PAT carries scopes `GITHUB_TOKEN` lacks; the public code path succeeds locally and falls back to a private URL in CI. |
| "Generated data should go through the merge queue like every other change." | `GITHUB_TOKEN` may not start workflows, so the bot's PR and its merge group never get the required check. Push with a deploy-key bypass instead. |
| "A green `just lint` means the required `lint` check will pass." | CI's actionlint runs shellcheck; backticks or `$` inside single quotes trip SC2016. Run `actionlint` directly. |

## Red Flags

- Workflow writes empty arrays for cluster/runs after transient network failure
- Dashboard shows `Loading…` or stale placeholder rows for long periods
- Live HTML shows Rocket Loader rewriting the dashboard entry script
- Generated dashboard sections disappear because their JSON artifact was not published
- Browser console logs CORS preflight failures to GitHub API endpoints
- CI changes are declared fixed without checking production Pages render
- Image-status ages are derived from workflow/run activity instead of GHCR package tags or release metadata.
- A dashboard card shows a numeric value without a traceable source URL/evidence path.
- A page-level JSON contract omits row-level provenance or hides missing values by dropping rows.
- Large inline Python or bash blocks (exceeding ~15 lines) are nested in workflow YAML, making testing and linting painful.
- Automated workflows that commit/push back to the git repository lack a concurrency limit block, causing push race conditions.
- **A single CI run hangs indefinitely on a private-network call; the concurrency group is starved** — missing timeout on an unreachable endpoint.
- **Tests fail in CI but pass locally** — the test environment diverges from runner environment (e.g. expects homelab LAN data that GitHub runners cannot reach). Without fallback handling and environment-aware assertions, this creates phantom CI failures that don't reflect real bugs.
- A new NodePort or Ingress manifest is proposed just to let a collector reach a service/pod that already has a ClusterIP or a diagnostics port — the API server proxy subresource reaches it without new cluster state.
- A dashboard row reports "bytes used" for storage that is actually a fixed-size preallocated block device, without noting the value is a derived estimate.
- A Tailscale-bridged seeding job is marked as a required status check or lacks `continue-on-error`, allowing a transient VPN/cache failure to block the main workflow.
- The private cluster (Ghost) is given the upstream cache signing key instead of pushing artifacts unsigned to an ephemeral runner that signs independently.
- A seeding workflow joins the production/home tailnet instead of a dedicated CI tailnet, exposing cluster nodes to an untrusted GitHub-hosted runner.
- Seed requests to Ghost are authenticated with a static bearer token instead of a short-lived signed JWT and Tailscale peer identity.
- **Tailscale is chosen before confirming that ghcr.io/OCI cannot serve BuildStream artifacts and that an on-cluster ARC runner (`runs-on: ghost-runners`) is not sufficient.**
- A container-mode ARC job targets `ghost-runners` without a `container:` block, causing the runner to fail immediately.
- Heavy builds run directly inside a small ARC step container instead of being submitted to an Argo Workflow, leading to OOM or CPU throttling.
- The ARC runner service account is given broad cluster-admin permissions instead of a namespace-scoped Role for workflow submission.
- A maintainer tries to use `runs-on: ghost-runners` from a personal repo instead of creating a separate `ghost-runners-personal` scale set with a personal GitHub App installation.
- A personal-repo scale set reuses the org's `githubConfigUrl: https://github.com/projectbluefin` or the org's installation secret.
- A ruleset requires a check whose workflow has no `merge_group` trigger; queued PRs remain in `AWAITING_CHECKS` with no merge-group workflow run.
- A workflow embeds a token in its clone URL/command arguments, force-pushes generated history, or uses `-X ours` on append-only NDJSON.
- A publishing workflow fails with `GH013: Repository rule violations found` and the proposed fix is to grant `GITHUB_TOKEN` a bypass, or to route the data through a pull request.
- A dashboard evidence link points at an Argo run URL or a LAN address.
- A dataset test asserts a hardcoded number of lanes, variants, or cards.
- A publishing workflow has been red for dozens of consecutive runs and nobody has checked whether a ruleset was created around the last green run (`gh api repos/<owner>/<repo>/rulesets`, compare `created_at` to the last success).

## Verification

### Security history evidence

Use `scripts/collect_security_telemetry.py` to derive security history from
the tracked CVE and release-verdict NDJSON artifacts. Keep unsupported
time-to-patch, SBOM, and provenance-attestation metrics explicitly unavailable
until workflows publish their required artifacts; static publisher capability
flags and a passing signature are not historical evidence for those metrics.

- [ ] Workflow logic preserves last known live snapshot when private endpoint fetch fails
- [ ] `_meta.live_snapshot_ok` and `_meta.refreshed_at` are present and updated
- [ ] GitHub Pages source/build state was checked before declaring a site live
- [ ] If Cloudflare fronts the site, the live HTML keeps the raw dashboard script tag
- [ ] Browser fetch code avoids unnecessary custom headers that trigger preflight
- [ ] Production `https://factory.projectbluefin.io/` renders with real table/cluster content (no loading placeholders)
- [ ] Render validation includes a real browser run (headless is fine) and captures evidence
- [ ] Every `execSync`, `fetch`, or network call to private endpoints (`<lab-subnet>.x`, internal IPs) has an explicit timeout set
- [ ] A publishing workflow was verified by an **unattended scheduled run**, not only a `workflow_dispatch`, and `main` actually advanced
- [ ] `actionlint` was run directly (not only `just lint`) before opening the PR
- [ ] Build-time test assertions accept both live-environment data AND fallback/degraded paths (never assert presence of unreachable data)
- [ ] After fixing network timeouts or assertions, a CI run completed within 10 minutes with no hanging steps
- [ ] Image-status cards derive age from GHCR package tag publish/update timestamps when available, otherwise release `published_at`, and link to exact evidence URLs.
- [ ] Unsupported metrics (no source-of-truth feed) are hidden or explicitly unavailable, never synthesized.
- [ ] Page-level dashboard JSON keeps stable row keys plus row-level provenance/state fields so collector-only follow-up work can populate data without changing the contract.
- [ ] Inline Python/bash blocks over 15 lines are extracted to standalone script files under `scripts/`.
- [ ] Concurrency blocks are added to git-mutating workflows to secure the git-push transaction.
- [ ] Collectors reaching ClusterIP-only services or pod-only diagnostics ports use `kubectl get --raw .../proxy/...` instead of adding new NodePorts/manifests.
- [ ] Derived/estimated usage numbers (no direct source gauge) state the derivation formula and inputs in the row's `derivation` field.
- [ ] Tailscale-bridged seeding jobs use OAuth client + dedicated tag, ephemeral nodes, and `continue-on-error` on both the job and every seeding step.
- [ ] The runner joins a dedicated CI tailnet, not the production tailnet, and starts with `--accept-routes=false --ssh=false`.
- [ ] Seed requests to Ghost are signed JWTs verified for signature, expiry, and claim consistency, plus Tailscale peer tag via `tailscale whois`.
- [ ] Upstream cache signing credentials are stored only in GitHub Secrets and are never copied to or referenced from Ghost.
- [ ] Seeding job teardown runs `if: always()` with `continue-on-error: true` and explicitly logs out of Tailscale / stops local services.
- [ ] Simpler alternatives (ARC runner on Ghost, pure GitHub Actions cron build) were ruled out before adding a Tailscale bridge.
- [ ] ARC container-mode jobs declare a `container:` image and the runner controller pod requests small resources (<=1 CPU / <=1 Gi).
- [ ] Heavy ARC jobs submit Argo Workflows rather than executing the heavy work inside the step container.
- [ ] The ARC runner service account has a namespace-scoped Role for workflow submission, not cluster-admin.
- [ ] A test container-mode workflow completes and produces the expected build artifact or cache seed.
- [ ] New maintainers can follow `/docs/ops/maintainer-onboarding.md` to add `ghost-runners` to an org repo.
- [ ] A personal-repo scale set uses a different `githubConfigUrl` and a different GitHub App installation secret from the org scale set.
- [ ] Every required status check has a workflow trigger for `merge_group` with `types: [checks_requested]`.
- [ ] `/docs/ops/maintainer-onboarding.md` is referenced from any skill or ops doc that discusses ARC runner access.
- [ ] Git publishers use credential-free remotes plus non-interactive credential helpers, and replay append-only updates from the latest remote state after a push race.
