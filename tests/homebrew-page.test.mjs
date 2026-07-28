import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const repo = process.cwd();

function html(file) {
  return readFileSync(path.join(repo, file), 'utf8');
}

// Lane coverage is derived from the same source of truth the generator uses, so adding a
// variant or branch never requires editing a magic number here.
function trackedLaneCount() {
  const publishers = JSON.parse(
    readFileSync(path.join(repo, 'docs/data/variant-publishers.json'), 'utf8'),
  );
  return Object.values(publishers.variants || {}).reduce(
    (total, details) => total + (details.branches || []).length,
    0,
  );
}

test('homebrew page renders summary metrics, lane details, explicit unavailable states, and chart containers', () => {
  execFileSync('npm', ['run', 'build'], {
    cwd: repo,
    stdio: 'pipe',
    encoding: 'utf8',
  });

  const homebrewPage = html('docs/adoption/index.html');

  assert.match(
    homebrewPage,
    /Tracked image lanes/i,
    'homebrew page shows tracked image lanes metric',
  );
  assert.match(
    homebrewPage,
    /Lanes with Homebrew data/i,
    'homebrew page shows lanes with brew data metric',
  );
  assert.match(
    homebrewPage,
    /Lanes awaiting Homebrew data/i,
    'homebrew page shows lanes awaiting brew data metric',
  );

  assert.match(
    homebrewPage,
    /bluefin|testing/i,
    'homebrew page includes at least one tracked lane',
  );
  assert.match(
    homebrewPage,
    /aurora/i,
    'homebrew page includes aurora lane',
  );

  assert.match(homebrewPage, /bluefin\/brewfile/i, 'homebrew page shows the transplanted tap');
  assert.match(homebrewPage, /Lanes with Homebrew data/i, 'homebrew metrics still render');

  assert.match(
    homebrewPage,
    /homebrew-lanes-chart/,
    'homebrew page renders lane status chart container',
  );

  assert.match(
    homebrewPage,
    /adoption-page-data/,
    'homebrew page serializes client chart data',
  );

  assert.match(
    homebrewPage,
    /https:\/\/github\.com\/projectbluefin\/lab\/blob\/main\/docs\/data\/variant-publishers\.json/,
    'homebrew page links source evidence from variant-publishers.json',
  );

  assert.match(
    homebrewPage,
    /homebrew-ecosystem\.json/,
    'homebrew page references the raw homebrew-ecosystem dataset',
  );

  assert.match(
    homebrewPage,
    /global formula analytics/i,
    'homebrew page discloses that transplanted values are global formula analytics',
  );
  assert.match(
    homebrewPage,
    /115-package tap/i,
    'homebrew page discloses that transplanted values include full tap scope reused across branches',
  );

  assert.match(
    homebrewPage,
    /Package leaderboard/i,
    'homebrew page renders package leaderboard section',
  );
  assert.match(
    homebrewPage,
    /claude-code|gemini-cli|gh/i,
    'homebrew page renders package-level leaderboard entries',
  );
  assert.match(
    homebrewPage,
    /bazzite\/brewfile/i,
    'homebrew page renders imported bazzite tap coverage',
  );
  assert.match(
    homebrewPage,
    /Tap density across tracked lanes/i,
    'homebrew page renders tap density section',
  );
  assert.match(
    homebrewPage,
    /Packages in tap scope/i,
    'homebrew page renders richer tap density metrics',
  );
  assert.match(
    homebrewPage,
    /Unavailable<\/span>/i,
    'homebrew page keeps explicit unavailable states visible in density lanes',
  );
});

test('homebrew data tables span the full grid width and stay scrollable when squeezed', () => {
  const homebrewPage = html('docs/adoption/index.html');

  const cssHref = homebrewPage.match(/href="(\/_astro\/SiteLayout\.[A-Za-z0-9_]+\.css)"/);
  assert.ok(cssHref, 'homebrew page links a compiled SiteLayout stylesheet');

  const css = html(path.join('docs', cssHref[1].replace(/^\//, '')));

  assert.match(
    css,
    /\.detail-grid>article:has\(\.table-scroll\)\{grid-column:1\/-1\}/,
    'table cards span the full grid row so 6-7 column tables are not crushed into a half-width cell',
  );
  assert.match(
    css,
    /\.data-table\{[^}]*min-width:48rem[^}]*\}/,
    'data tables have a min-width floor so .table-scroll scrolls horizontally instead of crushing columns',
  );
  assert.match(
    css,
    /\.table-scroll\{[^}]*overflow-x:auto[^}]*\}/,
    'table-scroll wrapper enables horizontal scrolling on narrow viewports',
  );
});

test('homebrew page renders migrated tap coverage instead of the starter empty state', () => {
  const homebrewPage = html('docs/adoption/index.html');
  assert.match(homebrewPage, /Homebrew data: \d+ of \d+ lanes available/i);
  assert.match(homebrewPage, /bluefin\/brewfile/i);
  assert.doesNotMatch(homebrewPage, /No Homebrew analytics data is published for any tracked lane/i);
});

test('homebrew-ecosystem.json contract satisfies the page model contract', () => {
  const datasetPath = path.join(repo, 'docs/data/homebrew-ecosystem.json');
  const dataset = JSON.parse(readFileSync(datasetPath, 'utf8'));

  assert.ok(Array.isArray(dataset.summary_metrics), 'summary_metrics is an array');
  assert.ok(Array.isArray(dataset.taps), 'taps is an array');
  assert.ok(Array.isArray(dataset.rows), 'rows is an array');

  assert.equal(
    dataset.rows.length,
    trackedLaneCount(),
    'homebrew dataset has one lane row per tracked variant-branch in variant-publishers.json',
  );

  const trackedMetric = dataset.summary_metrics.find((m) => m.id === 'tracked_image_lanes');
  const withDataMetric = dataset.summary_metrics.find((m) => m.id === 'lanes_with_brew_data');
  const awaitingMetric = dataset.summary_metrics.find((m) => m.id === 'lanes_awaiting_brew_data');

  assert.ok(trackedMetric, 'tracked_image_lanes summary metric exists');
  assert.ok(withDataMetric, 'lanes_with_brew_data summary metric exists');
  assert.ok(awaitingMetric, 'lanes_awaiting_brew_data summary metric exists');

  assert.equal(
    trackedMetric.value,
    dataset.rows.length,
    'tracked_image_lanes value agrees with actual row count',
  );
  assert.equal(
    withDataMetric.value + awaitingMetric.value,
    trackedMetric.value,
    'lanes_with_brew_data + lanes_awaiting_brew_data === tracked_image_lanes',
  );

  const derivedAvailable = dataset.rows.filter((r) => r.state === 'available').length;
  const derivedAwaiting = dataset.rows.filter((r) => r.state !== 'available').length;
  assert.equal(
    withDataMetric.value,
    derivedAvailable,
    'lanes_with_brew_data matches row-derived count of available-state rows',
  );
  assert.equal(
    awaitingMetric.value,
    derivedAwaiting,
    'lanes_awaiting_brew_data matches row-derived count of non-available-state rows',
  );

  for (const row of dataset.rows) {
    assert.ok(row.id, `row has id`);
    assert.ok(row.source_url, `row ${row.id} has source_url`);
    assert.ok(row.collected_at, `row ${row.id} has collected_at`);
    assert.ok(row.derivation, `row ${row.id} has derivation`);
    assert.ok(row.state, `row ${row.id} has state`);
    if (row.state !== 'available') {
      assert.ok(row.state_reason, `unavailable row ${row.id} has state_reason`);
    }
  }
});
