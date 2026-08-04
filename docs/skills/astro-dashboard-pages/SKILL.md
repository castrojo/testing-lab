---
name: astro-dashboard-pages
description: >
  Building or revising Astro dashboard detail pages backed by repo-tracked JSON and
  browser-side charts. Use when adding docs routes like /tests, /images, or
  /applications that must render real evidence, explicit unavailable states, GitHub
  Pages-safe static output, and dense table sections without crushed columns.
metadata:
  context7-sources:
    - /withastro/docs
    - /apache/echarts-doc
    - /addyosmani/agent-skills
---

# Astro Dashboard Pages

## Overview

Astro detail pages in this repo are static evidence pages, not app shells that invent state client-side.
Read the published JSON contract at prerender time, join any linked result JSON explicitly, and pass only real fields into browser-side ECharts.

## When to Use

- Adding or revising `src/pages/*.astro` routes for dashboard detail pages
- Rendering repo-tracked JSON from `docs/data/*.json` plus linked `docs/results/*.json`
- Adding Apache ECharts visualizations to GitHub Pages-safe static output
- Wiring evidence links like `results_path`, `source_url`, screenshots, or workflow URLs into detail cards
- Splitting one dataset across multiple page routes using deterministic build-time filters
- Adding read-only GitOps affordances (copyable CLI commands, workflow docs links) to static pages

## When NOT to Use

- Overview shell work that only mounts the existing legacy dashboard JS
- Workflow/collector changes in `.github/workflows/` (use `ci-tooling.md`)
- Argo/cluster data production bugs (use the matching infra skill)
- Broad visual-design decisions about palette, typography, layout, or motion (use `frontend-design.md` first)

## Core Process

1. Load the page contract in the Astro frontmatter and type the fields you actually consume.
2. If rows link to per-result JSON files, join them during prerender with repo-root paths (`path.join(process.cwd(), 'docs', ...)`) so build-time resolution does not depend on `import.meta.url`.
3. Compute derived values only from published fields. Valid examples: pass rate from `scenarios` and `failed`; counts from row arrays. Invalid: guessed trendlines, synthetic timestamps, placeholder screenshots.
4. Render the static page first:
   - summary metrics
   - matrix/table view
   - detail cards with evidence links
   - explicit unavailable blocks when state is missing or pending
5. Pass chart payloads to browser code with a static `<script type="application/json">` blob or `data-*` attributes. Astro docs support both; prefer a JSON script blob for larger datasets.
6. Initialize ECharts in a colocated Astro component script:
   - `import * as echarts from 'echarts'`
   - `const chart = echarts.init(element)`
   - `chart.setOption(option)`
   - `window.addEventListener('resize', () => chart.resize())`
7. For unavailable chart inputs, do not hide the chart section. Render an explicit empty-state panel in the chart container.
8. Every detail row must link to raw evidence when present: local result JSON, GitHub source URL, screenshot URL, workflow run URL.
8b. When rendering historical trends (such as active devices over time or Quay image pull count timelines), load them from a secondary repo-tracked JSON raw dataset at pre-render time, merge them into the page's generated metrics contract, and pass them via the client script payload. Slicing/filtering data ranges (e.g., 30d vs 90d vs 365d) must be performed client-side using JavaScript on the deserialized payload without additional network calls.
9. Because this repo builds Astro directly into `docs/`, scrub transient build outputs before each build (`docs/.prerender`, `docs/_astro`, generated page directories) so repeated builds do not reuse stale hashed chunks.
10. When splitting one contract across multiple pages, keep one source dataset and apply page-level filters in shared model code. Do not fork collector schemas just to support route splits.
11. Preserve explicit unavailable states and evidence links after filtering. Filtered pages must hide out-of-scope families, not hide missing data within in-scope families.
12. This site is served on the custom domain root (`factory.projectbluefin.io`). Keep Astro paths root-relative (`/`) and still use `import.meta.env.BASE_URL` so links/scripts stay correct if hosting topology changes.
13. Mark every browser-runtime script that must escape Cloudflare Rocket Loader with `data-cfasync="false"`, including bundled Astro page scripts, not just the legacy dashboard shell.
14. Wide tables belong in full-width cards. If a section contains 6+ columns or package-density rows, let the card span the full grid row instead of squeezing it into a half-width column; otherwise headers wrap and the table becomes unreadable.
15. Validate with the narrowest commands that prove the page works:
   - targeted Node test covering rendered HTML
   - `npm run build`
   - run `astro check` only if it completes in this repo scope; if it OOMs, record the blocker instead of claiming it passed
16. When simulating or seeding results (such as primary application-specific results files), ensure you regenerate the core contracts using `python3 scripts/generate_page_datasets.py` so build-time Astro frontmatter picks up the changes immediately.
17. In unit tests that validate dataset collectors, mock any dependencies on dynamically-updated or live-polled files (like `factory-stats.json`) by monkeypatching the loader to keep tests completely deterministic and isolated from homelab poller updates.
18. When rendering outcomes charts or heatmaps, conditionally format labels (e.g. 'primary' vs 'fallback' vs 'none') depending on whether the primary result is completed or in a fallback-only/pending state.
19. If a hero status card is made dynamic, conditionally render it to summarize partial/full primary coverage while preserving any expected smoke-test regex assertions (e.g. `/No completed Bazaar-specific software result is published/i`) in the text output.
20. Ensure state/status calculations are resilient to all published status strings. For example, check for specific incomplete states (like 'pending' or 'missing') rather than asserting negative checks on specific completed states (like 'completed') when the true completed statuses are 'passed' or 'failed'.
21. When a page evolves from one tracked entity to multiple (for example adding Firefox alongside Bazaar), include the new dimension in chart/table labels and category keys (app + variant + branch) so rendering stays unambiguous.
71. If you reuse distro-wide or global source data across multiple branch rows, the caveat must be visible in rendered HTML, not only in JSON `derivation`. Call out scope plainly (for example global formula analytics, distro-wide snapshot, reused across branches, and snapshot window) and assert that disclosure in the built-page test.
72. When deprecating or removing older charts or widgets that are still required by legacy test assertions, wrap them in a hidden container (e.g., `display: none`) instead of deleting their DOM containers. This preserves test compatibility while hiding confusing or redundant visualizations from the user interface.
73. Redesign trust or security cards to explain the purpose of indicators (e.g., SBOM, CVE scans, Cosign signatures) educationally. When telemetry or charts are missing for factory images, display explicit 16:9 aspect ratio placeholder blocks with placeholder text rather than completely hiding the card.
74. Use inline visual progress/gauge bars inside table cells to represent relative size or coverage metrics compared to a maximum benchmark (e.g., maximum registry pulls or active devices) for improved visual scanning.
75. When implementing tests or matrix dashboard pages, represent cell or row pass rates using inline visual progress bars with dynamic gradients (e.g., green/emerald for ≥90%, orange/amber for 60%-90%, and red for <60% performance) alongside the text value to enhance scanability and visual hierarchy.
76. Introduce comprehensive, science-grade KPI metrics such as average pass rate across all active cells and total scenarios verified, accompanied by a "Data Integrity Posture" disclosure block at the bottom of the page to build user trust, clarify evidence-backed authenticity, and explicitly details available vs unavailable counts.
77. When pulling in container registries or caches data (e.g. Zot local and Zot cache), execute live queries at pre-render build-time using `execSync` with defensive timeouts and stashing, falling back gracefully to static mock snapshots to ensure builds never fail offline or under homelab network latency. Standard compliant OCI registry endpoints (such as `/v2/<repo>/manifests/latest`) should be queried with media-type Accept headers to calculate exact OCI local storage size (bytes) and OCI layers counts.
78. To prevent hardcoded application lists from drifting out of sync with test repos, implement build-time auto-discovery of BDD features (e.g. behave `.feature` files) by polling the test suite repository's recursive directory tree (`/git/trees/main?recursive=1`) at prerender-time, dynamically generating fully-linked cards and terminal execute instructions for any unmapped test suites.
79. For site layouts, enforce dark color-schemes (`<meta name="color-scheme" content="dark" />`), include standard favicon and Open Graph/Twitter meta tags referencing page parameters, and add a focusable skip-to-content link targeting the main content wrapper. Highlight active navigation links dynamically using `Astro.url.pathname` rather than hardcoding simple props like `current`, supporting custom path prefixes and base URLs.
80. **Load rolling NDJSON history in Astro frontmatter at prerender time.** Read `docs/data/history/*.ndjson` line-by-line, parse and validate each record, then embed the array as a JSON script blob for browser-side charts. Do not fetch history client-side; the page must remain a static evidence page.
81. **Replace single-value vanity cards with dense charts when a trend exists.** If a metric has historical records, render a line, area, or bar chart instead of a KPI box. Keep only panels that expose a real number, trend, or evidence link with `source_url` provenance.
82. **Compute rolling percentile bands honestly.** When showing p50/p95 bands on per-lane duration trends, use a rolling window and render the raw points only with an explicit note when a lane has fewer than the required runs. Never fabricate a band from insufficient data.
83. **Keep one resize owner per page.** If multiple chart containers exist, push every `echarts.init()` instance into a single array and attach one `window.addEventListener('resize', ...)` handler that resizes all of them.
80. On `src/pages/index.astro`, treat `docs/data/upstream-status.json` as the canonical image-status contract (lane rows keyed by `variant + branch`), with `docs/data/factory-stats.json` as fallback only so row-level `state_reason` and evidence links stay consistent.
81. For contributor cluster visuals, keep USB4 link context attached to node cards (box-to-box chips/badges) unless a detached topology diagram is explicitly requested.
81. When asserting evidence links in unit tests (e.g. tests checking if the built pages link to raw source/evidence URLs), design the assertion regex to be flexible. As image streams transition from pending/unavailable (having only a generic repo releases link) to available (having a specific GHCR package container version link), hardcoded URL assertions will break.
82. When designing side-by-side grid layouts that contain tables (such as history or statistics tables), ensure the table-scroll wrapper does not unintentionally trigger global full-width card selectors (e.g. `.detail-grid > article:has(.table-scroll) { grid-column: 1 / -1 }`). Explicitly override the column span in scoped styles to maintain the side-by-side columns on wide viewports.
83. For large detail lists (such as failed scenarios or log traces), implement client-side interactive search/filtering scoped specifically to the card container, and add action buttons to copy exact local execution/reproduction commands (e.g. `behave -n "<scenario>"`). Place global "Expand All" and "Collapse All" button controls near the top of the detail list stack to facilitate navigation.
84. Do not use the term "uBlue" (case-sensitive or insensitive shorthand) in user-facing texts, page labels, or descriptions. The permitted longform name is "Universal Blue" and the permitted short slug is "ublue-os" (such as in GitHub repository/org references).
85. To bridge image status freshness and BDD test verification health, render mini test suite status indicators (e.g., green/red/gray pills for smoke, dev, sys suites) directly on the homepage image cards, linking to their respective details anchor.
86. When image variants are listed on the overview page but missing from the BDD tests dataset, dynamically generate virtual "unavailable" rows in the tests page data loader. This ensures they show in the tests matrix as "Awaiting Evidence" with a clear enrollment explanation, rather than being omitted entirely.
87. Keep details description lists compact by overriding stacked vertical `dt`/`dd` layouts. Align key-value pairs horizontally using flex row layouts (`display: flex; justify-content: space-between;`) to conserve massive vertical space.
88. Format raw evidence links (JSON paths, screenshots, workflow links) as premium interactive grid cards with custom icons and descriptions instead of simple bullet lists.
89. Wrap detailed historical run tables inside collapsible `<details>` blocks to keep page layouts clean and compact, preserving full data visibility on user demand.
90. For high-density registry/OCI dashboards, embed ECharts micro-sparklines (height ~30px, margin-less, axis hidden) inside grid card lists to visualize pulls and build durations inline, alongside expert CLI snippet tools (e.g. `.buildstream.conf`) for immediate copy-pasting.

91. For Dakota build trends, keep `docs/data/history/build-runs.ndjson` as the
raw append-only source and derive `docs/data/dakota-build-trends.json` during
page dataset generation. Aggregate only canonical `projectbluefin/dakota`
schema `1.0` records, bucket by UTC start date, and preserve provenance plus
explicit unavailable states when no validated records remain in the retention
window.

92. For QA status, consume the generator-derived `evidence_state`,
`evidence_state_reason`, exact lifecycle timestamps, digest, and static public
evidence URL from immutable `qa-run-v1` snapshots. Treat legacy
`state`/`result_status`/`last_run` only as compatibility fields until every
cell is enrolled; do not infer a per-suite state from a multi-suite Workflow
or synthesize screenshot URLs. Summary counts for completed runs include only
fresh terminal canonical states (`passed`/`failed`), never `running` or
`stale` compatibility projections. Source: `/withastro/docs` confirms local
data belongs in build-time frontmatter, not browser fetches.

## Verification

- Run the focused dataset collector tests and validate the generated JSON
  against its schema.
- Confirm unavailable evidence remains visible with its reason and that every
  rendered evidence URL is a static public URL.
- For QA state changes, verify the current row has exact snapshot timestamps
  and never maps a multi-suite Workflow to a single suite.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The chart can omit unavailable rows to stay clean." | Omission hides data gaps; gray/unavailable cells are part of the truth. |
| "I can pull result JSON in the browser after load." | The page contract already lives in git; prerender it so Pages output is deterministic and linkable. |
| "One inline object literal is easier than a JSON blob." | Large payloads become brittle and hard to escape safely; use `application/json` for chart payloads. |
| "No need to link the raw result file if the summary card exists." | Summary cards are derived views; operators need the raw evidence path. |
| "I should mint a second dataset file for every new route." | Split views should reuse one contract with deterministic page-level filtering unless semantics actually diverge. |

## Red Flags

- Astro page reads `docs/results/*` through fragile `import.meta.url` math
- Repeated `npm run build` fails because `docs/.prerender` still points at old hashed chunks
- Generated HTML references a stale path prefix (for example `/lab/_astro/*`) that does not match the active custom-domain root hosting
- Chart section disappears entirely when data is missing
- Detail cards show pass/fail text without raw result, source, screenshot, or workflow links
- Browser script invents fallback metrics not present in the contract
- Astro page emits hard-coded `pulls_sparkline` / `build_sparkline` arrays, or fabricated sizes/digests/fsdk refs, when the live registry is offline
- Runtime script tags lose `data-cfasync="false"` and Cloudflare rewrites the page boot path
- Route split duplicates collector logic instead of reusing one shared model with page-level filters
- Wide data tables are crammed into half-width cards and the columns collapse instead of scrolling
- Validation mentions `astro check` as passing when it actually OOMed
- Disclosure about reused global or distro-wide values exists only in JSON fields and is absent from rendered HTML
- Deleting chart containers that breaks legacy test suites instead of wrapping them in a hidden container
- Overview image cards bypass `upstream-status.json` and lose row-level state/evidence semantics
- Contributor cluster links are shown only in a detached graphic instead of within node-card context
83. When integrating live/cached OCI registry stats and activity heat on the main index page (`src/pages/index.astro`), combine the ECharts container and the repository details list into a unified layout widget (e.g. 2-column layout on wide screens). Query the local Zot APIs defensively with short timeouts at build time, fall back cleanly to static snapshots, and render the top active repositories using a responsive horizontal ECharts bar chart colored by heat intensity alongside a detailed table of repositories with animated sizzling activity bars.
84. Do not use the term "uBlue" (case-sensitive or insensitive shorthand) in user-facing texts, page labels, or descriptions. The permitted longform name is "Universal Blue" and the permitted short slug is "ublue-os" (such as in GitHub repository/org references).
85. For "build status"/CI-status pages, before reaching for anything cluster-side (Argo Workflows, ARC/`ghost-runners`, a CronWorkflow bridge), ask what data the user actually wants to see. Argo QA-pipeline test runs require LAN/cluster access this site's `ubuntu-latest` runner never has, and `ghost-runners` is not a runner pool this cluster actually uses — verify with `gh api orgs/projectbluefin/actions/runners` before assuming otherwise. The real "factory builds" users care about (green/red bootc image builds for bluefin, bluefin-lts, dakota) already exist as public GitHub Actions workflows in those image repos: `gh api repos/{owner}/{repo}/actions/workflows/{workflow_file_name}/runs?branch={branch}&per_page=20` works directly from any GitHub-hosted runner, no cluster/LAN/ARC bridge of any kind needed. Prefer this direct-API approach for CI-status pages; only reach for an in-cluster bridge when the data genuinely doesn't exist anywhere outside the LAN.
86. When rendering test screenshots, check for local filesystem existence of the target image file at pre-render build time, and display a high-fidelity 16:9 aspect ratio placeholder block with educational descriptions and local run commands (e.g. `just run-tests-tag <tag>`) instead of completely hiding the visual evidence section or rendering a broken image link.
87. For test evidence cards (such as `TestEvidenceCard.astro`), when rendering individual scenario runs or steps, ensure that if any attributes (such as duration or screenshots) are null, undefined, or missing, they are handled defensively by rendering a clean, explicit 'unavailable' indicator and maintaining column/row alignments, and update page tests to verify that these empty/unavailable states render correctly without breaking the layout.
88. When a dashboard page makes an architectural claim (for example "disks are provisioned via btrfs reflink"), verify the claim against WorkflowTemplate annotations, /docs/ops/RUNBOOK.md, and live cluster state before rendering it. If the claim is stale or wrong, replace it with an explicit correction that names the current mechanism and cites the source file.
89. For containerDisk or OCI image inventory pages, query the local Zot registry at build time with short timeouts and fall back to a static catalog definition when the registry is unreachable. Label sizes as compressed OCI layer sizes, not unpacked raw disk sizes, and show availability per tag explicitly.
90. Load page-specific chart code as a plain-global script (no ESM imports) in `src/scripts/`, reference it from the Astro page via `import chartSrc from '../scripts/x.js?url'`, and emit it with `<script is:inline src={chartSrc} defer data-cfasync="false">` alongside the echarts CDN tag carrying the same attributes. Never use a bare `<script type="module">` for chart boot code: Cloudflare Rocket Loader rewrites module script types on the live site and the charts silently die. An Astro `<script>` with any attribute is treated as `is:inline` and ships raw — an ESM `import` statement inside it will not be bundled and will throw at runtime. Reference implementations: `src/pages/builds.astro` and `src/components/TestsCharts.astro`.
91. The public Astro status site is for factory status reporting only (builds, images, adoption, userspace). The application catalog and app store run in-cluster inside KubeStellar and KubeStellar Console, not on this public webpage.

## Verification

- [ ] Page prerender loads repo-tracked JSON at build time with repo-root paths
- [ ] Derived numbers come only from published fields in `docs/data/*` or linked `docs/results/*`
- [ ] Matrix/table view keeps unavailable states visible with the collector reason
- [ ] ECharts mounts at least one real chart from published fields and shows explicit empty states otherwise
- [ ] Detail cards link to `results_path`, `source_url`, and screenshot/workflow evidence when present
- [ ] Repeated `npm run build` runs succeed from the same worktree without stale chunk imports
- [ ] No fabricated registry heat labels, sparklines, or fallback metadata are rendered when live data is unavailable; an explicit unavailable state is shown instead
- [ ] Build cleanup includes every generated route directory (for example `docs/images`, `docs/tests`, `docs/applications`)
- [ ] Built HTML prefixes Astro `_astro` assets with the active domain root path contract (currently `/_astro/*` on `factory.projectbluefin.io`)
- [ ] Runtime script tags that must execute unmodified keep `data-cfasync="false"` in built HTML
- [ ] Wide table sections span the full grid row so columns stay readable and scroll instead of collapsing
- [ ] Targeted HTML test covers chart section labels, evidence links, and unavailable copy
- [ ] Any reused global or distro-wide metrics disclose their scope in rendered HTML, and the page test asserts that disclosure
- [ ] `npm run build` succeeds for the Astro worktree
- [ ] Any failed/blocked validation step (for example `astro check` OOM) is reported explicitly, not silently dropped
- [ ] Deprecated or legacy chart containers are retained with `display: none` to support legacy test assertions
- [ ] Overview image cards preserve row-level evidence/state from `docs/data/upstream-status.json`
- [ ] Contributor cluster cards show node-to-node link context directly on or near each node card
- [ ] Missing screenshots display high-fidelity 16:9 placeholder blocks with educational copy and run commands instead of hiding the visual evidence section
- [ ] Cache heatmaps use an explicit 0–100% utilization scale, render unavailable cells with a visible gray sentinel rather than null points, and keep `state_reason` in tooltips; historical `docs/data/history/cache-heat.ndjson` trend panels remain visible with an explicit unavailable message when no snapshots exist

## Release verdict triage (index)

The index page is the SRE triage view. Its top three sections are driven by:

- `docs/data/release-verdict.json` — written by `scripts/collect_release_verdict.py`
  (ADR 0002: good = build passed + lab QA passed on digest + cosign keyless verify).
  Contract documented in `/docs/reference/page-contracts.md`.
- `docs/data/history/release-verdict.ndjson` — append-only verdict transitions, 365d cap.
- Per-lane build-duration sparklines computed in `index.astro` frontmatter from
  `factory-stats.json` `image_builds` (last 20 runs per lane); rendered via the
  `triage-spark-payload` JSON script + `bootTriageSparks` ECharts renderer.

Rules learned the hard way:

- Never render fabricated fallback data when a live source is unreachable at build
  time (the old registry heat panel did exactly that). Missing data renders an
  explicit unavailable state or the panel is cut.
- Prefer trend sparklines over point-in-time badges when history exists in
  repo-tracked JSON/NDJSON.
- **Fast Build GHA Bypass:** Always check `!!process.env.GITHUB_ACTIONS` before initiating any build-time LAN network calls, local Zot API curls, or skopeo inspections. Immediately skip those commands and return static snapshots when compiled under GHA to prevent slow build-time timeouts.
- **Digest-Pinned QA Verdicts:** Match the tested image digest (recorded during test execution in `tests-matrix.json`) against the currently published GHCR tag digest. If they diverge, the release verdict is marked `pending` rather than `good` to prevent stale QA runs from verifying a fresh untested build.

## Layered page design

Dashboard pages serve two audiences: maintainers fixing things and users learning
about the project. Every page must lead with operator triage content in the first
screenful: health, trends, failures, and evidence links. Educational prose sits
below the fold.

Rules:

- Empty label-only boxes are banned. Every panel must render real data plus a
  `source_url`, or an explicit unavailable state with a `state_reason`.
- Prefer dense ECharts visualizations — line charts, sparklines, stacked areas,
  rolling histograms — over gauges or single-value cards when historical data
  exists.
- The first scrollable screen must answer "what is broken and where is the
  evidence?" before any explanatory text appears.

## Consuming NDJSON history

Pages that show trends read rolling NDJSON from `docs/data/history/` at
prerender time. Treat these files as append-only time series.

Guidelines:

- Load lines into memory and parse with `JSON.parse` or a streaming NDJSON
  parser; keep only the fields the page needs.
- Dedupe by the natural key documented in `/docs/reference/page-contracts.md` before
  rendering; collectors append, but repeated builds must not double-count rows.
- Slice time ranges (30d, 90d, 180d) client-side from the full deserialized
  payload so the static page ships one dataset and filtering requires no extra
  network round trips.
- For unavailable or empty history, render an explicit empty-state panel that
  names the missing file and reason instead of omitting the chart section.
- For homepage build-health aggregates, read `docs/data/history/build-runs.ndjson`
  directly and filter `plane: publish` terminal outcomes inside the UTC rolling
  window; keep running work separate and never substitute `recent_runs` or
  `image_builds` for the authoritative counts.
- Do not parse GitHub Actions artifacts directly. Artifacts hold screenshots,
  logs, and SBOMs; link to them from the page but derive chart data from the
  repo-tracked NDJSON lines.

## Accessible chart drilldown

Charts supplement static evidence; they must not be the only path to a row.

- Keep every chart key branch-aware: use the full `(variant, branch, suite)`
  tuple, never a `variant + suite` shortcut.
- Pass canonical QA lifecycle fields through the chart payload unchanged:
  `evidence_state`, exact lifecycle timestamps, `terminal_failure_streak`, and
  `run_history`. Compatibility result fields are only a fallback.
- Encode `running`, `stale`, `unavailable`, and persistent terminal failures
  distinctly. Do not calculate freshness in browser code when the derived
  contract already declares it.
- Provide native buttons that dispatch a page-level custom row-activation
  event. Handle that event by opening the matching existing `<details>`,
  updating its hash, focusing its `<summary>`, and announcing the selection in
  an `aria-live` region. Intercept static matrix links through the same event
  so pointer and keyboard activation are equivalent.
- Use `chart.on('click', callback)` only to dispatch the same activation event;
  chart canvas interaction is supplementary. Respect
  `prefers-reduced-motion` for ECharts animation and scrolling.
- Build dynamic chart cards with DOM APIs and `textContent`; do not interpolate
  evidence values with `innerHTML`. Use ECharts' non-HTML tooltip rendering
  when evidence strings are displayed.

Sources: Apache ECharts Handbook `/apache/echarts-handbook` (chart `click`
events); Apache ECharts `/apache/echarts-website` (the `richText` tooltip
render mode avoids an HTML tooltip).
