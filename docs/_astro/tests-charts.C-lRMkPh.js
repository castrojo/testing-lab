const payloadNode = document.getElementById('tests-chart-data');
const payload = payloadNode ? JSON.parse(payloadNode.textContent ?? '{}') : {};
const rows = Array.isArray(payload.rows) ? payload.rows : [];
const testRuns = Array.isArray(payload.testRuns) ? payload.testRuns : [];
const charts = [];

const renderUnavailable = (element, message) => {
  element.innerHTML = `<div class="chart-panel__empty">${message}</div>`;
};

const formatUtc = (value) => value
  ? `${new Date(value).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })} UTC`
  : 'Unavailable';

const addContext = (element, items, label = 'UTC history', generatedAt = null) => {
  const timestamps = items
    .map((item) => new Date(item.recorded_at).getTime())
    .filter((value) => Number.isFinite(value))
    .sort((a, b) => a - b);
  const note = document.createElement('div');
  note.className = 'chart-panel__note';
  note.style.cssText = 'color:#64748b;font-size:0.72rem;margin-top:0.5rem;';
  note.textContent = `${label} · ${items.length} sample${items.length === 1 ? '' : 's'} · ${timestamps.length ? `${formatUtc(timestamps[0])} – ${formatUtc(timestamps[timestamps.length - 1])}` : 'window unavailable'} · last updated ${formatUtc(generatedAt || timestamps[timestamps.length - 1])}`;
  element.parentElement?.appendChild(note);
};

const palette = [
  '#38bdf8', '#4ade80', '#f59e0b', '#ec4899', '#8b5cf6',
  '#f43f5e', '#10b981', '#3b82f6', '#f97316', '#a78bfa',
  '#22d3ee', '#f472b6',
];

const laneSuiteKey = (run) => `${run.variant}-${run.branch}-${run.suite}`;
const laneSuiteName = (run) => `${run.variant} ${run.branch} · ${run.suite}`;

const groupTestRuns = () => {
  const groups = new Map();
  for (const run of testRuns) {
    const key = laneSuiteKey(run);
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(run);
  }
  for (const [, list] of groups) {
    list.sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime());
  }
  return [...groups.entries()];
};

const toPassRate = (total, failed) => {
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(failed)) return null;
  return Number((((total - failed) / total) * 100).toFixed(2));
};

const renderTrends = () => {
  const element = document.getElementById('tests-chart-trends');
  if (!element) return;

  const groups = groupTestRuns();
  if (!groups.length) {
    renderUnavailable(element, 'No test run history published yet.');
    addContext(element, testRuns, 'UTC history', payload.generatedAt);
    return;
  }
  addContext(element, testRuns, 'UTC history', payload.generatedAt);

  const series = groups.map(([key, runs], index) => {
    const run = runs[0];
    const data = runs.map((r) => ({
      value: [r.recorded_at, toPassRate(r.scenarios_total, r.scenarios_failed)],
      status: r.status,
      failed: r.scenarios_failed,
      total: r.scenarios_total,
      workflow: r.workflow_name,
    }));
    return {
      name: laneSuiteName(run),
      type: 'line',
      smooth: false,
      showSymbol: true,
      symbolSize: 6,
      emphasis: { focus: 'series' },
      lineStyle: { width: 2 },
      itemStyle: { color: palette[index % palette.length] },
      data,
    };
  });

  const chart = echarts.init(element);
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 56, right: 28, top: 48, bottom: 48 },
    legend: { type: 'scroll', top: 0, textStyle: { color: '#cbd5e1' } },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      borderColor: 'rgba(125, 211, 252, 0.35)',
      textStyle: { color: '#e2e8f0' },
      formatter: (items) =>
        items
          .map((item) => {
            const data = item.data;
            return `${item.marker}<strong>${item.seriesName}</strong><br/>${new Date(data.value[0]).toLocaleString()}<br/>Pass rate: ${data.value[1] ?? '—'}%<br/>Status: ${data.status}<br/>Failed: ${data.failed}/${data.total}<br/>Workflow: ${data.workflow ?? 'Unavailable'}`;
          })
          .join('<hr style="border-color: rgba(148, 163, 184, 0.2)">'),
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
  charts.push(chart);
};

const renderHeatmap = () => {
  const element = document.getElementById('tests-chart-heatmap');
  if (!element) return;

  const suites = Array.isArray(payload.suites) ? payload.suites : [];
  const variants = Array.isArray(payload.variants) ? payload.variants : [];
  if (!suites.length || !variants.length) {
    renderUnavailable(element, 'No suite/variant dimensions published yet.');
    addContext(element, [], 'Current snapshot', payload.generatedAt);
    return;
  }
  addContext(element, [], 'Current snapshot', payload.generatedAt);

  const cellMap = new Map();
  for (const row of rows) {
    const key = `${row.variant}:${row.suite}`;
    const existing = cellMap.get(key);
    if (!existing || row.branch === 'testing') {
      cellMap.set(key, row);
    }
  }

  const data = [];
  variants.forEach((variant, y) => {
    suites.forEach((suite, x) => {
      const row = cellMap.get(`${variant}:${suite}`);
      let value = -1;
      let label = '';
      let tooltip = '';
      if (!row) {
        value = -1;
        label = '—';
      } else if (row.state !== 'available') {
        value = -1;
        label = row.enrollment_issue_url ? '#' : '—';
      } else if (row.result_status === 'passed') {
        value = 1;
        label = 'PASS';
      } else if (row.result_status === 'failed') {
        value = 0;
        label = 'FAIL';
      }

      if (row) {
        if (row.state !== 'available') {
          tooltip = `${variant} / ${suite}<br/>unavailable<br/>${row.state_reason ?? ''}`;
        } else {
          tooltip = `${variant} / ${suite}<br/>${row.result_status}<br/>${row.pass_rate ?? '—'}% · ${row.scenarios_failed}/${row.scenarios_total} failed`;
        }
      } else {
        tooltip = `${variant} / ${suite}<br/>no published row`;
      }

      data.push({
        value: [x, y, value],
        label,
        tooltip,
        rowId: row?.id ?? null,
      });
    });
  });

  const chart = echarts.init(element);
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 84, right: 28, top: 24, bottom: 88 },
    tooltip: {
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
      data: variants,
      axisLabel: { color: '#cbd5e1' },
      axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
    },
    visualMap: {
      show: false,
      min: -1,
      max: 1,
      inRange: {
        color: ['#334155', '#dc2626', '#16a34a'],
      },
    },
    series: [
      {
        name: 'Result',
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
      },
    ],
  });
  chart.on('click', (params) => {
    if (params.data?.rowId) {
      const el = document.getElementById(params.data.rowId);
      el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (el && el.tagName === 'DETAILS') {
        el.setAttribute('open', '');
      }
    }
  });
  charts.push(chart);
};

const renderFlakes = () => {
  const container = document.getElementById('tests-chart-flakes');
  if (!container) return;

  const flakyRows = rows.filter((row) => row.state === 'available' && Number(row.flake_flips) >= 1);
  const totalRuns = testRuns.length;

  if (!flakyRows.length) {
    container.innerHTML = `<div class="flake-panel__empty">No flaky rows detected across ${totalRuns} recorded runs.</div>`;
    addContext(container, testRuns, 'UTC history', payload.generatedAt);
    return;
  }
  addContext(container, testRuns, 'UTC history', payload.generatedAt);

  const grid = document.createElement('div');
  grid.className = 'flake-grid';

  for (const row of flakyRows) {
    const history = testRuns
      .filter((r) => r.variant === row.variant && r.branch === row.branch && r.suite === row.suite)
      .sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime());

    const item = document.createElement('div');
    item.className = 'flake-item';
    const chartId = `flake-sparkline-${row.id}`;
    item.innerHTML = `
      <div class="flake-item__header">
        <span class="flake-item__title">${row.variant} ${row.branch} · ${row.suite}</span>
        <span class="pill pill--${row.result_status}">${row.result_status}</span>
      </div>
      <div class="flake-item__meta">${row.flake_flips} flip${row.flake_flips === 1 ? '' : 's'} · ${row.runs_recorded} runs</div>
      <div id="${chartId}" class="flake-sparkline"></div>
    `;
    grid.appendChild(item);

    if (history.length) {
      const chartDiv = item.querySelector(`#${chartId}`);
      const chart = echarts.init(chartDiv);
      chart.setOption({
        backgroundColor: 'transparent',
        grid: { left: 0, right: 0, top: 4, bottom: 4 },
        xAxis: { type: 'category', show: false, data: history.map((_, i) => i) },
        yAxis: { type: 'value', min: 0, max: 100, show: false },
        tooltip: {
          trigger: 'axis',
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          borderColor: 'rgba(125, 211, 252, 0.35)',
          textStyle: { color: '#e2e8f0' },
          formatter: (items) => {
            const idx = items[0].dataIndex;
            const run = history[idx];
            const rate = toPassRate(run.scenarios_total, run.scenarios_failed);
            return `${run.workflow_name}<br/>${run.status} · ${rate ?? '—'}%`;
          },
        },
        series: [
          {
            type: 'line',
            smooth: false,
            symbol: 'circle',
            symbolSize: 5,
            lineStyle: { width: 2, color: '#f59e0b' },
            itemStyle: { color: '#f59e0b' },
            areaStyle: {
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(245, 158, 11, 0.35)' },
                { offset: 1, color: 'rgba(245, 158, 11, 0)' },
              ]),
            },
            data: history.map((r) => toPassRate(r.scenarios_total, r.scenarios_failed)),
          },
        ],
      });
      charts.push(chart);
    }
  }

  container.appendChild(grid);
};

renderTrends();
renderHeatmap();
renderFlakes();

if (charts.length) {
  window.addEventListener('resize', () => {
    charts.forEach((chart) => chart.resize());
  });
}
