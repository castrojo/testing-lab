import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const pageSource = readFileSync(
  path.join(process.cwd(), 'src/pages/index.astro'),
  'utf8',
);

test('homepage build-health derivation deduplicates records by repository natural key', () => {
  assert.match(
    pageSource,
    /(?:new\s+Map|Set)[\s\S]{0,500}run\.plane[\s\S]{0,200}run\.run_id/i,
    'build-health derivation should keep one record per plane and run id',
  );
});

test('homepage build-health derivation averages the two middle durations for even samples', () => {
  assert.match(
    pageSource,
    /(?:length\s*%\s*2\s*===?\s*0|length\s*%\s*2\s*!==?\s*1)[\s\S]{0,300}(?:sortedDurations|durations)[\s\S]{0,300}(?:\/\s*2|\+\s*.*\/\s*2)/,
    'median derivation should average both middle values for an even sample',
  );
});

test('homepage renders running-only build-health windows instead of unavailable state', () => {
  assert.match(
    pageSource,
    /completedPublishBuilds\.length\s*>\s*0[\s\S]{0,120}\|\|[\s\S]{0,120}runningPublishBuilds\.length\s*>\s*0/,
    'build-health section should render when the window contains only running builds',
  );
});

test('homepage build-health outcomes bucket timestamps by UTC date', () => {
  assert.match(
    pageSource,
    /new\s+Date\(\s*run\.started_at\s*\)\.toISOString\(\)\.slice\(\s*0\s*,\s*10\s*\)/,
    'daily outcome buckets should derive dates from UTC-normalized timestamps',
  );
});

test('overview and chart scripts expose history context for missing or partial data', () => {
  assert.match(pageSource, /sample_count:\s*publishBuildHistory\.length/);
  assert.match(pageSource, /last_updated:\s*buildHistoryLastUpdated/);
  assert.match(pageSource, /samples:\s*\{publishBuildHistory\.length\}/);

  const buildsCharts = readFileSync(
    path.join(process.cwd(), 'src/scripts/builds-charts.js'),
    'utf8',
  );
  assert.match(buildsCharts, /const addContext\s*=/);
  assert.match(buildsCharts, /No terminal runs recorded for this lane\.[\s\S]{0,200}addContext/);

  const testsCharts = readFileSync(
    path.join(process.cwd(), 'src/scripts/tests-charts.js'),
    'utf8',
  );
  assert.match(testsCharts, /const addContext\s*=/);
  assert.match(testsCharts, /No test run history published yet\.[\s\S]{0,200}addContext/);
  assert.match(testsCharts, /Current snapshot/);
});
