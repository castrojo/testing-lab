import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

const repo = process.cwd();

function html(file) {
  return readFileSync(path.join(repo, file), 'utf8');
}

// Lane coverage is derived from the same source of truth the generator uses, so adding a
// variant or branch never requires editing a magic number here.
function trackedLaneCount() {
  return Object.values(trackedVariants()).reduce(
    (total, details) => total + (details.branches || []).length,
    0,
  );
}

function trackedVariants() {
  const publishers = JSON.parse(
    readFileSync(path.join(repo, 'docs/data/variant-publishers.json'), 'utf8'),
  );
  return publishers.variants || {};
}

test('adoption page renders summary metrics, lane details, trust cards, chart containers, and explicit unavailable states', () => {
  execFileSync('npm', ['run', 'build'], {
    cwd: repo,
    stdio: 'pipe',
    encoding: 'utf8',
  });

  assert.equal(
    existsSync(path.join(repo, 'docs/adoption/index.html')),
    true,
    'adoption page should exist after build',
  );

  const adoptionPage = html('docs/adoption/index.html');

  // Summary metrics section
  assert.match(
    adoptionPage,
    /Tracked image lanes/i,
    'adoption page shows tracked image lanes summary metric',
  );
  assert.match(
    adoptionPage,
    /adoption-summary-metrics/,
    'adoption page renders summary metrics section with correct aria label',
  );

  // Unavailable pull/countme gap is explicit, not silently missing
  assert.match(
    adoptionPage,
    /pull.count data.*unavailable|unavailable.*pull.count|No registry pull-count data|pull_count.*null|pull data.*pending/i,
    'adoption page keeps unavailable pull-count state explicit',
  );
  assert.match(
    adoptionPage,
    /countme|active.device|Fedora countme/i,
    'adoption page references countme/active-device data concept',
  );
  assert.match(
    adoptionPage,
    /Lane coverage/i,
    'adoption page renders the lane coverage panel',
  );
  assert.match(
    adoptionPage,
    /flatcar-testing/i,
    'adoption page shows the flatcar testing lane',
  );

  // Lane detail rows — contract has 10 rows
  assert.match(adoptionPage, /bluefin-testing/i, 'adoption page shows bluefin testing lane');
  assert.match(adoptionPage, /bluefin-stable/i, 'adoption page shows bluefin stable lane');
  assert.match(adoptionPage, /aurora/i, 'adoption page shows aurora lanes');
  assert.match(adoptionPage, /bazzite/i, 'adoption page shows bazzite lanes');
  assert.match(adoptionPage, /dakota/i, 'adoption page shows dakota lanes');
  assert.match(adoptionPage, /flatcar/i, 'adoption page shows flatcar lane');

  // Trust summary cards
  assert.match(
    adoptionPage,
    /trust-summary-cards|adoption-trust/,
    'adoption page renders trust summary section',
  );
  assert.match(adoptionPage, /projectbluefin/i, 'adoption page lists projectbluefin publisher');
  assert.match(adoptionPage, /ublue-os/i, 'adoption page lists ublue-os publisher');

  // ECharts chart containers
  assert.match(
    adoptionPage,
    /adoption-coverage-chart/,
    'adoption page renders the coverage chart container',
  );
  assert.match(
    adoptionPage,
    /adoption-trust-chart/,
    'adoption page renders the trust coverage chart container',
  );

  // Serialized page data for ECharts
  assert.match(
    adoptionPage,
    /adoption-page-data/,
    'adoption page serializes client chart data',
  );

  // Evidence links present
  assert.match(
    adoptionPage,
    /https:\/\/github\.com\/projectbluefin\/bluefin/,
    'adoption page links bluefin evidence',
  );
  assert.match(
    adoptionPage,
    /https:\/\/github\.com\/ublue-os\/bazzite/,
    'adoption page links bazzite evidence',
  );

  // Dataset provenance block
  assert.match(adoptionPage, /adoption-metrics\.json/i, 'adoption page links raw dataset');
  assert.match(adoptionPage, /schema_version|Collector-derived/i, 'adoption page shows provenance info');

  assert.match(
    adoptionPage,
    /distro-wide countme/i,
    'adoption page discloses that transplanted values are distro-wide countme snapshots',
  );
  assert.match(
    adoptionPage,
    /2026-03-16/,
    'adoption page discloses the snapshot week window',
  );

  // Partial coverage — transplanted data
  assert.match(adoptionPage, /\d+ of \d+ lanes have data/i, 'adoption hero reflects partial coverage');
  assert.match(adoptionPage, /No registry pull-count data/i, 'adoption page keeps pull-count gaps explicit');
  assert.match(adoptionPage, /3502|2,527|71,550/i, 'adoption page renders migrated countme values');
});

test('adoption page renders partial countme coverage while keeping pull gaps explicit', () => {
  const adoptionPage = html('docs/adoption/index.html');
  assert.match(adoptionPage, /\d+ of \d+ lanes have data/i);
  assert.match(adoptionPage, /71,550|71550/i);
  assert.match(adoptionPage, /No registry pull-count data/i);
  assert.doesNotMatch(adoptionPage, /Adoption signals are pending collection/i);
});

test('adoption-metrics.json contract satisfies the page model contract', () => {
  const datasetPath = path.join(repo, 'docs/data/adoption-metrics.json');
  const dataset = JSON.parse(readFileSync(datasetPath, 'utf8'));

  assert.ok(Array.isArray(dataset.summary_metrics), 'summary_metrics is an array');
  assert.ok(Array.isArray(dataset.trust_cards), 'trust_cards is an array');
  assert.ok(Array.isArray(dataset.rows), 'rows is an array');

  assert.equal(
    dataset.rows.length,
    trackedLaneCount(),
    'adoption dataset has one lane row per tracked variant-branch in variant-publishers.json',
  );
  assert.equal(
    dataset.trust_cards.length,
    Object.keys(trackedVariants()).length,
    'adoption dataset has one trust card per tracked variant in variant-publishers.json',
  );

  // All rows carry a valid state enum value; every unavailable row carries an explicit non-empty
  // state_reason. This is a permanent behavioral contract — it holds whether 0 or all lanes
  // have pull/countme data.
  for (const row of dataset.rows) {
    assert.ok(
      row.state === 'available' || row.state === 'unavailable',
      `row ${row.id} state is a valid enum value`,
    );
    if (row.state === 'unavailable') {
      assert.ok(
        typeof row.state_reason === 'string' && row.state_reason.trim().length > 0,
        `unavailable row ${row.id} carries an explicit non-empty state_reason`,
      );
    }
    // Required provenance fields on every row
    assert.ok(row.source_url, `row ${row.id} has source_url`);
    assert.ok(row.collected_at, `row ${row.id} has collected_at`);
    assert.ok(row.derivation, `row ${row.id} has derivation`);
  }

  // Every trust card must have required provenance fields
  for (const card of dataset.trust_cards) {
    assert.ok(card.variant, `trust card has variant`);
    assert.ok(card.source_url, `trust card ${card.variant} has source_url`);
  }

  // Summary metrics must be internally consistent with the rows array so the page model
  // has one source of truth to draw from.
  const pullMetric = dataset.summary_metrics.find((m) => m.id === 'lanes_with_pull_data');
  const trackedLanesMetric = dataset.summary_metrics.find((m) => m.id === 'tracked_image_lanes');
  const countmeMetric = dataset.summary_metrics.find((m) => m.id === 'lanes_with_countme_data');
  assert.ok(pullMetric, 'lanes_with_pull_data metric exists');
  assert.ok(trackedLanesMetric, 'tracked_image_lanes metric exists');
  assert.ok(countmeMetric, 'lanes_with_countme_data metric exists');
  assert.equal(
    trackedLanesMetric.value,
    dataset.rows.length,
    'tracked_image_lanes summary metric matches rows.length',
  );
  assert.equal(
    pullMetric.value,
    dataset.rows.filter((r) => r.pull_count !== null).length,
    'lanes_with_pull_data summary metric matches actual non-null pull_count row count',
  );
  assert.equal(
    countmeMetric.value,
    dataset.rows.filter((r) => r.countme_active_devices !== null).length,
    'lanes_with_countme_data summary metric matches actual non-null countme row count',
  );
});
