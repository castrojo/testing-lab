const payloadNode = document.getElementById('tests-chart-data');
let payload = {};

try {
  payload = payloadNode ? JSON.parse(payloadNode.textContent ?? '{}') : {};
} catch {
  payload = {};
}

const rows = Array.isArray(payload.rows) ? payload.rows : [];
const testRuns = Array.isArray(payload.testRuns) ? payload.testRuns : [];
const charts = [];
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const stateValues = { unavailable: 0, stale: 1, running: 2, failed: 3, passed: 4 };

const asText = (value, fallback = 'Unavailable') =>
  typeof value === 'string' && value.trim() ? value.trim() : fallback;

const validDate = (value) => {
  if (typeof value !== 'string' || !value) return null;
  return Number.isNaN(new Date(value).getTime()) ? null : value;
};

const formatTimestamp = (value) => {
  const timestamp = validDate(value);
  return timestamp
    ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(new Date(timestamp)) + ' UTC'
    : 'Unavailable';
};

const laneSuiteName = (row) => `${asText(row.variant)} ${asText(row.branch)} · ${asText(row.suite)}`;
const laneSuiteKey = (row) => JSON.stringify([row.variant, row.branch, row.suite]);

const evidenceState = (row) => {
  const state = asText(row?.evidence_state, '').toLowerCase();
  if (Object.hasOwn(stateValues, state)) return state;
  const lifecycleState = asText(row?.state, '').toLowerCase();
  if (lifecycleState === 'running') return 'running';
  if (lifecycleState === 'terminal') {
    return asText(row?.phase, '').toLowerCase() === 'succeeded' ? 'passed' : 'failed';
  }
  if (lifecycleState && lifecycleState !== 'available') return 'unavailable';
  const legacy = asText(row?.result_status ?? row?.status, '').toLowerCase();
  return legacy === 'passed' || legacy === 'failed' ? legacy : 'unavailable';
};

const toPassRate = (total, failed) => {
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(failed)) return null;
  return Number((((total - failed) / total) * 100).toFixed(2));
};

const timestampFor = (run) =>
  validDate(run?.finished_at)
  ?? validDate(run?.observed_at)
  ?? validDate(run?.started_at)
  ?? validDate(run?.recorded_at)
  ?? validDate(run?.run_date);

const historyFor = (row) => {
  const publishedHistory = Array.isArray(row.run_history) && row.run_history.length
    ? row.run_history
    : Array.isArray(row.details?.history) && row.details.history.length
      ? row.details.history
      : testRuns.filter((run) => laneSuiteKey(run) === laneSuiteKey(row));

  return publishedHistory
    .map((run) => {
      const timestamp = timestampFor(run);
      return timestamp
        ? {
            ...run,
            timestamp,
            state: evidenceState(run),
            total: Number.isFinite(run.scenarios_total) ? run.scenarios_total : run.scenarios,
            failed: Number.isFinite(run.scenarios_failed) ? run.scenarios_failed : run.failed,
          }
        : null;
    })
    .filter(Boolean)
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
};

const stateDescription = (row) => {
  const state = evidenceState(row);
  const reason = asText(row.evidence_state_reason ?? row.state_reason, '');
  const observed = formatTimestamp(row.observed_at ?? row.finished_at ?? row.started_at);
  const streak = Number(row.terminal_failure_streak);
  const persistence = Number.isFinite(streak) && streak > 1
    ? `; persistent failure streak: ${streak}`
    : '';
  return `${state}${reason ? ` — ${reason}` : ''}; observed: ${observed}${persistence}`;
};

const renderUnavailable = (element, message) => {
  const empty = document.createElement('div');
  empty.className = 'chart-panel__empty';
  empty.textContent = message;
  element.replaceChildren(empty);
};

const dispatchActivation = (row) => {
  document.dispatchEvent(new CustomEvent('tests:activate-row', {
    bubbles: true,
    detail: {
      id: row.id,
      variant: row.variant,
      branch: row.branch,
      suite: row.suite,
    },
  }));
};

const findDetails = (detail) => {
  if (detail?.id) {
    const direct = document.getElementById(detail.id);
    if (direct?.matches('details')) return direct;
  }

  if (detail?.variant && detail?.branch && detail?.suite) {
    const direct = document.getElementById(`${detail.variant}-${detail.branch}-${detail.suite}`);
    if (direct?.matches('details')) return direct;
  }

  return [...document.querySelectorAll('details[data-test-card]')].find((card) =>
    card.dataset.testVariant === detail?.variant
    && card.dataset.testBranch === detail?.branch
    && card.dataset.testSuite === detail?.suite,
  ) ?? null;
};

const announce = (message) => {
  const liveRegion = document.getElementById('tests-chart-announcer');
  if (liveRegion) liveRegion.textContent = message;
};

const activateDetails = (detail, updateHash = true) => {
  const details = findDetails(detail);
  if (!details) {
    announce('Matching evidence details are unavailable.');
    return;
  }

  details.open = true;
  const id = details.id;
  if (updateHash && id) {
    history.replaceState(null, '', `#${encodeURIComponent(id)}`);
  }

  details.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
  const summary = details.querySelector('summary');
  summary?.focus({ preventScroll: true });
  announce(`Selected ${laneSuiteName(detail)}. Evidence details opened.`);
};

document.addEventListener('tests:activate-row', (event) => {
  activateDetails(event.detail);
});

document.addEventListener('click', (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const chartControl = target?.closest('[data-tests-chart-select]');
  if (chartControl) {
    dispatchActivation({
      id: chartControl.dataset.testId,
      variant: chartControl.dataset.testVariant,
      branch: chartControl.dataset.testBranch,
      suite: chartControl.dataset.testSuite,
    });
    return;
  }

  const matrixLink = target?.closest('.matrix-table__link[href^="#"]');
  if (!matrixLink) return;
  const id = matrixLink.getAttribute('href')?.slice(1);
  if (!id) return;
  event.preventDefault();
  dispatchActivation({ id: decodeURIComponent(id) });
});

document.addEventListener('keydown', (event) => {
  if (event.key !== ' ') return;
  const target = event.target instanceof Element ? event.target : null;
  const matrixLink = target?.closest('.matrix-table__link[href^="#"]');
  const id = matrixLink?.getAttribute('href')?.slice(1);
  if (!id) return;
  event.preventDefault();
  dispatchActivation({ id: decodeURIComponent(id) });
});

window.addEventListener('hashchange', () => {
  const id = decodeURIComponent(window.location.hash.slice(1));
  if (id) activateDetails({ id }, false);
});

const palette = [
  '#38bdf8', '#4ade80', '#f59e0b', '#ec4899', '#a78bfa',
  '#f43f5e', '#10b981', '#3b82f6', '#f97316', '#22d3ee',
];

const renderTrends = () => {
  const element = document.getElementById('tests-chart-trends');
  if (!element) return;
  if (!window.echarts) {
    renderUnavailable(element, 'Chart library unavailable. Use the matrix and evidence cards below.');
    return;
  }

  const seriesRows = rows
    .map((row) => ({ row, history: historyFor(row) }))
    .filter(({ history }) => history.length);
  if (!seriesRows.length) {
    renderUnavailable(element, 'No timestamped QA-run history is published yet.');
    return;
  }

  const series = seriesRows.map(({ row, history }, index) => ({
    name: laneSuiteName(row),
    type: 'line',
    smooth: false,
    showSymbol: true,
    symbolSize: 6,
    emphasis: { focus: 'series' },
    lineStyle: { width: 2 },
    itemStyle: { color: palette[index % palette.length] },
    data: history.map((run) => ({
      value: [run.timestamp, toPassRate(run.total, run.failed)],
      row,
      run,
    })),
  }));

  const chart = window.echarts.init(element);
  chart.setOption({
    animation: !reducedMotion,
    backgroundColor: 'transparent',
    grid: { left: 56, right: 28, top: 48, bottom: 48 },
    legend: { type: 'scroll', top: 0, textStyle: { color: '#cbd5e1' } },
    tooltip: {
      trigger: 'axis',
      renderMode: 'richText',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: 'rgba(125, 211, 252, 0.35)',
      textStyle: { color: '#e2e8f0' },
      formatter: (items) => items.map((item) => {
        const { row, run } = item.data;
        const rate = item.value[1] ?? 'Unavailable';
        return `${item.seriesName}\n${formatTimestamp(run.timestamp)}\nPass rate: ${rate}%\n${stateDescription({ ...row, ...run })}`;
      }).join('\n\n'),
    },
    xAxis: {
      type: 'time',
      axisLabel: { color: '#94a3b8' },
      axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
      splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.08)' } },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: { color: '#94a3b8', formatter: '{value}%' },
      splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.12)' } },
    },
    series,
  });
  chart.on('click', (params) => dispatchActivation(params.data?.row));
  charts.push(chart);
};

const renderHeatmap = () => {
  const element = document.getElementById('tests-chart-heatmap');
  if (!element) return;
  if (!window.echarts) {
    renderUnavailable(element, 'Chart library unavailable. Use the matrix and evidence cards below.');
    return;
  }

  const suites = Array.isArray(payload.suites) && payload.suites.length
    ? payload.suites
    : [...new Set(rows.map((row) => row.suite).filter(Boolean))];
  const lanes = [...new Map(rows.map((row) => [JSON.stringify([row.variant, row.branch]), {
    variant: row.variant,
    branch: row.branch,
    label: `${asText(row.variant)} · ${asText(row.branch)}`,
  }])).values()];
  if (!suites.length || !lanes.length) {
    renderUnavailable(element, 'No branch-aware suite dimensions are published yet.');
    return;
  }

  const cellMap = new Map(rows.map((row) => [laneSuiteKey(row), row]));
  const data = [];
  lanes.forEach((lane, y) => {
    suites.forEach((suite, x) => {
      const row = cellMap.get(JSON.stringify([lane.variant, lane.branch, suite]));
      const state = row ? evidenceState(row) : 'unavailable';
      const streak = Number(row?.terminal_failure_streak);
      const label = state === 'passed'
        ? 'PASS'
        : state === 'failed'
          ? `FAIL${streak > 1 ? ` ×${streak}` : ''}`
          : state === 'running'
            ? 'RUN'
            : state === 'stale'
              ? 'STALE'
              : '—';
      data.push({
        value: [x, y, stateValues[state]],
        label,
        row,
        tooltip: row
          ? `${lane.label} · ${suite}\n${stateDescription(row)}`
          : `${lane.label} · ${suite}\nUnavailable — no published row`,
      });
    });
  });

  const chart = window.echarts.init(element);
  chart.setOption({
    animation: !reducedMotion,
    backgroundColor: 'transparent',
    grid: { left: 120, right: 28, top: 24, bottom: 88 },
    tooltip: {
      renderMode: 'richText',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: 'rgba(125, 211, 252, 0.35)',
      textStyle: { color: '#e2e8f0' },
      formatter: (params) => params.data.tooltip,
    },
    xAxis: {
      type: 'category',
      data: suites,
      axisLabel: { color: '#cbd5e1', interval: 0, rotate: 25 },
      axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
    },
    yAxis: {
      type: 'category',
      data: lanes.map((lane) => lane.label),
      axisLabel: { color: '#cbd5e1' },
      axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
    },
    visualMap: {
      show: false,
      pieces: [
        { value: stateValues.passed, color: '#16a34a' },
        { value: stateValues.failed, color: '#dc2626' },
        { value: stateValues.running, color: '#2563eb' },
        { value: stateValues.stale, color: '#d97706' },
        { value: stateValues.unavailable, color: '#475569' },
      ],
    },
    series: [{
      name: 'Evidence state',
      type: 'heatmap',
      label: {
        show: true,
        color: '#f8fafc',
        fontSize: 10,
        formatter: (params) => params.data.label,
      },
      data,
      itemStyle: { borderColor: 'rgba(255,255,255,0.04)', borderWidth: 1, borderRadius: 4 },
      emphasis: { itemStyle: { borderColor: '#38bdf8', borderWidth: 2 } },
    }],
  });
  chart.on('click', (params) => {
    if (params.data?.row) dispatchActivation(params.data.row);
  });
  charts.push(chart);
};

const appendTextElement = (parent, tagName, className, text) => {
  const element = document.createElement(tagName);
  element.className = className;
  element.textContent = text;
  parent.appendChild(element);
  return element;
};

const renderFlakes = () => {
  const container = document.getElementById('tests-chart-flakes');
  if (!container) return;

  const notableRows = rows.filter((row) =>
    Number(row.flake_flips) >= 1 || Number(row.terminal_failure_streak) >= 2,
  );
  if (!notableRows.length) {
    const empty = document.createElement('div');
    empty.className = 'flake-panel__empty';
    empty.textContent = 'No flaky rows or persistent terminal failure streaks are published.';
    container.replaceChildren(empty);
    return;
  }

  const grid = document.createElement('div');
  grid.className = 'flake-grid';
  grid.setAttribute('role', 'list');

  for (const row of notableRows) {
    const item = document.createElement('div');
    item.className = 'flake-item';
    item.setAttribute('role', 'listitem');
    const header = document.createElement('div');
    header.className = 'flake-item__header';
    const title = document.createElement('button');
    title.type = 'button';
    title.className = 'flake-item__title';
    title.dataset.testsChartSelect = '';
    title.dataset.testId = row.id;
    title.dataset.testVariant = row.variant;
    title.dataset.testBranch = row.branch;
    title.dataset.testSuite = row.suite;
    title.setAttribute('aria-controls', row.id);
    title.textContent = laneSuiteName(row);
    header.appendChild(title);
    appendTextElement(header, 'span', `pill pill--${evidenceState(row)}`, evidenceState(row));
    item.appendChild(header);

    const flips = Number(row.flake_flips);
    const streak = Number(row.terminal_failure_streak);
    const meta = [
      flips > 0 ? `${flips} pass/fail flip${flips === 1 ? '' : 's'}` : null,
      streak >= 2 ? `${streak} consecutive terminal failures` : null,
      Number.isFinite(Number(row.runs_recorded)) ? `${row.runs_recorded} legacy runs` : null,
    ].filter(Boolean).join(' · ');
    appendTextElement(item, 'div', 'flake-item__meta', meta);

    const history = historyFor(row);
    if (history.length && window.echarts) {
      const sparkline = document.createElement('div');
      sparkline.className = 'flake-sparkline';
      item.appendChild(sparkline);
      const chart = window.echarts.init(sparkline);
      chart.setOption({
        animation: !reducedMotion,
        backgroundColor: 'transparent',
        grid: { left: 0, right: 0, top: 4, bottom: 4 },
        xAxis: { type: 'category', show: false, data: history.map((_, index) => index) },
        yAxis: { type: 'value', min: 0, max: 100, show: false },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          borderColor: 'rgba(125, 211, 252, 0.35)',
          textStyle: { color: '#e2e8f0' },
          formatter: (items) => {
            const run = history[items[0].dataIndex];
            return `${formatTimestamp(run.timestamp)}\n${evidenceState(run)} · ${toPassRate(run.total, run.failed) ?? 'Unavailable'}%`;
          },
        },
        series: [{
          type: 'line',
          smooth: false,
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 2, color: '#f59e0b' },
          itemStyle: { color: '#f59e0b' },
          data: history.map((run) => toPassRate(run.total, run.failed)),
        }],
      });
      chart.on('click', () => dispatchActivation(row));
      charts.push(chart);
    }
    grid.appendChild(item);
  }
  container.replaceChildren(grid);
};

renderTrends();
renderHeatmap();
renderFlakes();

if (window.location.hash) {
  activateDetails({ id: decodeURIComponent(window.location.hash.slice(1)) }, false);
}

if (charts.length) {
  window.addEventListener('resize', () => {
    charts.forEach((chart) => chart.resize());
  });
}
