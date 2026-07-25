import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

const repo = process.cwd();

function html(file) {
  return readFileSync(path.join(repo, file), 'utf8');
}

before(() => {
  execFileSync('npm', ['run', 'build'], {
    cwd: repo,
    stdio: 'pipe',
    encoding: 'utf8',
  });
});

test('Astro build emits multipage factory routes into docs', () => {

  const expectedFiles = [
    'docs/index.html',
    'docs/images/index.html',
    'docs/tests/index.html',
    'docs/applications/index.html',
    'docs/adoption/index.html',
    'docs/userspace/index.html',
    'docs/about/index.html',
    'docs/evidence/index.html',
  ];

  for (const file of expectedFiles) {
    assert.equal(existsSync(path.join(repo, file)), true, `${file} should exist after build`);
  }

  assert.match(html('docs/index.html'), /factory-dashboard/, 'overview keeps the dashboard shell');
  assert.doesNotMatch(html('docs/index.html'), /factory-dashboard\.js/, 'legacy dashboard script is removed');
  assert.doesNotMatch(html('docs/index.html'), /factory-dashboard\.css/, 'legacy dashboard style is removed');
  assert.match(html('docs/index.html'), /Is the last release good\?/i, 'overview leads with the release verdict triage section');
  assert.match(html('docs/index.html'), /class="nodes-grid"/, 'overview renders contributor nodes');
  assert.match(html('docs/index.html'), /class="image-status-grid"/, 'overview renders image status section');
  assert.match(html('docs/index.html'), /href="\/images\/"/, 'overview links to images at domain root');
  assert.match(html('docs/index.html'), /site-nav__link[^>]*>Overview</, 'top nav shows Overview tab');
  assert.match(html('docs/tests/index.html'), /src="\/_astro\/tests-charts\.[^"]+\.js" defer data-cfasync="false"/, 'tests page keeps the Rocket-Loader-exempt chart script');
  assert.match(html('docs/images/index.html'), /src="\/_astro\/upstream-page\.[^"]+"[^>]* data-cfasync="false"/, 'images page keeps Cloudflare-safe chart script');
  const adoptionPage = html('docs/adoption/index.html');
  assert.match(adoptionPage, /data-cfasync="false"/, 'adoption page keeps Cloudflare-safe chart script');
  assert.match(html('docs/images/index.html'), /Image status/, 'images page renders');
  assert.match(html('docs/tests/index.html'), /Tests/, 'tests page renders');
  assert.match(html('docs/applications/index.html'), /Applications/, 'applications page renders');
  assert.match(adoptionPage, /Homebrew/, 'adoption page renders integrated Homebrew content');
  assert.match(adoptionPage, /Adoption/, 'adoption page renders');
  assert.match(html('docs/about/index.html'), /Bluefin QA — Methodology/i, 'about page renders methodology');
  assert.match(html('docs/applications/index.html'), /Bazaar/, 'applications page calls out Bazaar scope');
  assert.match(html('docs/index.html'), /href="\/adoption\/"/, 'overview links to adoption at domain root');
  assert.match(html('docs/index.html'), /href="\/userspace\/"/, 'overview links to userspace at domain root');
  assert.match(html('docs/index.html'), /Ryzen AI MAX\+/i, 'overview renders control-plane Ryzen AI specs');
  assert.match(html('docs/index.html'), /Is the last release good\?/i, 'overview renders release verdict triage section');
  assert.match(html('docs/index.html'), /class="verdict-grid"/, 'overview renders per-lane verdict cards');
  assert.match(html('docs/index.html'), /class="spark-grid"/, 'overview renders build duration sparklines');
  assert.match(html('docs/index.html'), /What's degrading\?/i, 'overview renders degradation panel');
  assert.doesNotMatch(html('docs/index.html'), /Zot OCI Registry Cache & Heat/i, 'fabricated registry heat panel is removed from overview');
  assert.match(html('docs/images/index.html'), /Unavailable|pending|coming soon/i, 'subpages show explicit unavailable state');

  // Regression guard: chart scripts must load echarts globally from a CDN classic script
  // and never rely on a bare `import ... from 'echarts'` inside a type="module" script
  // whose src came from a `?url` import. Astro never bundles/resolves that pattern, so the
  // import throws `Failed to resolve module specifier "echarts"` in every real browser and
  // the chart silently fails to render (caught this only via a real headless-browser check,
  // never via these text assertions alone).
  function decodeInlineScriptSource(pageHtml, matchToken) {
    // The chart script may be emitted either as a separate built asset (its filename
    // contains matchToken) or inlined as a base64 data: URI when small enough for
    // Vite's asset-inlining threshold — a data: URI never contains the original
    // filename, so match either shape rather than requiring the token in the src.
    const allScriptSrcs = [...pageHtml.matchAll(/<script src="([^"]+)"[^>]*>/g)].map((m) => m[1]);
    const src = allScriptSrcs.find((s) => s.includes(matchToken))
      ?? allScriptSrcs.find((s) => !s.includes('cdn.jsdelivr.net'));
    assert.ok(src, `expected to find a local chart script tag near ${matchToken}`);
    if (src.startsWith('data:')) {
      const base64 = src.split(',')[1];
      return Buffer.from(base64, 'base64').toString('utf8');
    }
    return readFileSync(path.join(repo, 'docs', src), 'utf8');
  }

  for (const [page, scriptToken] of [
    ['docs/images/index.html', 'upstream-page'],
    ['docs/builds/index.html', 'builds-charts'],
  ]) {
    const pageHtml = html(page);
    assert.match(
      pageHtml,
      /cdn\.jsdelivr\.net\/npm\/echarts/,
      `${page} loads echarts globally from CDN before its chart script`,
    );
    assert.doesNotMatch(
      pageHtml,
      new RegExp(`type="module"[^>]*${scriptToken}`),
      `${page} chart script is not an unresolved ES module import`,
    );
    const scriptSource = decodeInlineScriptSource(pageHtml, scriptToken);
    assert.doesNotMatch(
      scriptSource,
      /import\s+\*\s+as\s+echarts\s+from\s+['"]echarts['"]/,
      `${scriptToken}.js has no unresolved bare echarts import`,
    );
  }
});

test('tests page renders matrix views, chart mounts, evidence links, and unavailable states', () => {
  const testsPage = html('docs/tests/index.html');

  assert.match(testsPage, /Pass-rate trends per lane-suite/i, 'tests page shows pass-rate trend chart section');
  assert.match(testsPage, /Suite\/variant heatmap/i, 'tests page shows suite variant heatmap section');
  assert.match(testsPage, /Flaky rows and run history sparklines/i, 'tests page shows flaky rows panel');
  assert.match(testsPage, /bluefin-testing-smoke/i, 'tests page renders available matrix row details');
  assert.match(testsPage, /results\/bluefin-testing-smoke\.json/i, 'tests page links results evidence');
  assert.match(testsPage, /Not Enrolled|Awaiting Test Run Evidence/i, 'tests page keeps unavailable states explicit');

  // KPI and lane verdict strip
  assert.match(testsPage, /Tests at a glance/i, 'tests page renders KPI scorecard');
  assert.match(testsPage, /Lanes good/i, 'tests page renders lanes good KPI');
  assert.match(testsPage, /Gating pass rate/i, 'tests page renders gating pass rate KPI');
  assert.match(testsPage, /Flaky rows/i, 'tests page renders flaky rows KPI');
  assert.match(testsPage, /progress-bar/i, 'tests page renders inline progress bars for pass rates');
  assert.match(testsPage, /Triage & local execution runbook/i, 'tests page renders triage runbook section');

  // Toggles, copying, and search
  assert.match(testsPage, /Expand All/i, 'tests page renders Expand All toggle button');
  assert.match(testsPage, /Collapse All/i, 'tests page renders Collapse All toggle button');
  assert.match(testsPage, /failure-search-input/i, 'tests page renders scoped failure search filter inputs');
  assert.match(testsPage, /behave -n /i, 'tests page renders local reproduction behave commands');
  assert.match(testsPage, /toast-notification/i, 'tests page renders toast notification styling');
});

test('images page renders grouped views, chart mounts, evidence links, and unavailable states', () => {
  const imagesPage = html('docs/images/index.html');

  assert.match(imagesPage, /Stream availability by family/i, 'images page shows grouped availability chart section');
  assert.match(imagesPage, /Release freshness by stream/i, 'images page shows freshness chart section');
  assert.match(imagesPage, /Release timeline/i, 'images page shows release timeline chart section');
  assert.match(imagesPage, /Dakota testing/i, 'images page includes projectbluefin streams');
  assert.match(imagesPage, /https:\/\/github\.com\/(orgs\/projectbluefin\/packages\/container\/dakota|projectbluefin\/dakota\/releases)/i, 'images page links projectbluefin evidence');
  assert.match(imagesPage, /No published release timestamp is present in docs\/data\/factory-stats\.json for this stream\./i, 'images page keeps unavailable reason explicit');
  assert.match(imagesPage, /https:\/\/github\.com\/ublue-os\/aurora\/releases/i, 'images page links non-bluefin evidence');
  assert.match(imagesPage, /Fedora Silverblue|Fedora Kinoite/i, 'images page references Silverblue and Kinoite upstream parent OSes');
  assert.match(imagesPage, /https:\/\/fedoraproject\.org\/silverblue\/|https:\/\/fedoraproject\.org\/kinoite\//i, 'images page links upstream Silverblue and Kinoite homepages');
  assert.match(imagesPage, /upstream-availability-chart|upstream-freshness-chart|upstream-timeline-chart|upstream-distribution-chart|upstream-brackets-chart|upstream-poller-heatmap-chart/, 'images page renders chart containers');
});

test('userspace page renders FSDK containers, registry metadata, and charts', () => {
  const userspacePage = html('docs/userspace/index.html');

  assert.match(userspacePage, /Freedesktop SDK Container Images/i, 'userspace page renders main title');
  assert.match(userspacePage, /FSDK images built/i, 'userspace page renders build KPI card');
  assert.match(userspacePage, /OCI local storage/i, 'userspace page renders Zot OCI local storage card');
  assert.match(userspacePage, /Registry Layers/i, 'userspace page renders Zot layers count card');
  assert.match(userspacePage, /Freedesktop SDK Container Images/i, 'userspace page renders the FSDK container images heading');
  assert.match(userspacePage, /Freedesktop SDK custom containers/i, 'userspace page renders tracked custom containers section');
  assert.match(userspacePage, /fsdk\/lab-runner/i, 'userspace page includes lab-runner OCI image');
  assert.match(userspacePage, /elements\/oci\/lab-runner\.bst/i, 'userspace page includes buildstream element path');
  assert.match(userspacePage, /Curated Resource Optimization/i, 'userspace page explains why some elements are unbuilt');
  assert.match(userspacePage, /podman pull 192\.168\.1\.102:30500\/fsdk\/lab-runner:latest/i, 'userspace page renders copyable podman pull commands');
  assert.match(userspacePage, /GitHub Source/i, 'userspace page links FSDK containers directly to GitHub');
  assert.match(userspacePage, /userspace-registry-dist-chart|userspace-fsdk-status-chart/, 'userspace page renders chart containers');
  assert.match(userspacePage, /FSDK version/i, 'userspace page renders Freedesktop SDK version label');
  assert.match(userspacePage, /Digest/i, 'userspace page renders OCI Digest label');
  assert.match(userspacePage, /Git Commit Ref/i, 'userspace page renders Git Commit Reference label');
  assert.match(userspacePage, /Registry details & cache repositories/i, 'userspace page renders Registry details & cache repositories header');
  assert.doesNotMatch(userspacePage, /Registry Heat \/ Activity/i, 'fabricated registry heat column is removed');
  assert.doesNotMatch(userspacePage, /Sizzling/i, 'fabricated sizzling heat metric label is removed');
  assert.doesNotMatch(userspacePage, /heat-glowing-bar/i, 'fabricated heat glowing bar is removed');
  assert.doesNotMatch(userspacePage, /pulls_sparkline|build_sparkline/i, 'fabricated pull/build sparkline arrays are not emitted');
  assert.doesNotMatch(userspacePage, /25\.08\.13|sha256:0b6a015c|sha256:9e7ed032|83\.1 MB/i, 'fabricated fallback metadata values are not emitted');
  assert.match(userspacePage, /unavailable|fallback cache/i, 'userspace page renders an explicit unavailable/fallback state');
  assert.match(userspacePage, /Raw dataset/i, 'userspace page keeps the raw dataset provenance link');
});

test('evidence page renders triage charts, coordinate overlays, gallery, and data integrity disclosure', () => {
  const evidencePage = html('docs/evidence/index.html');

  assert.match(evidencePage, /Evidence Ledger/i, 'evidence page renders main title');
  assert.match(evidencePage, /Diagnostic Telemetry Metrics/i, 'evidence page renders diagnostic metrics section');
  assert.match(evidencePage, /Failure Triage Scatter Plot/i, 'evidence page renders triage scatter plot section');
  assert.match(evidencePage, /AT-SPI Coordinate Map Overlay/i, 'evidence page renders coordinate overlay section');
  assert.match(evidencePage, /Visual Evidence Gallery/i, 'evidence page renders visual evidence gallery section');
  assert.match(evidencePage, /Failure Runbooks/i, 'evidence page renders failure runbooks section');
  assert.match(evidencePage, /Provenance: run metrics from docs\/data\/factory-stats\.json/i, 'evidence page renders the provenance footer line');
});
