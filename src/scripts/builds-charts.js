const payloadNode = document.getElementById('builds-chart-data');
const payload = payloadNode ? JSON.parse(payloadNode.textContent ?? '{}') : {};
const runs = Array.isArray(payload.runs) ? payload.runs : [];
const lanes = payload.lanes || { publish: [], lab: [] };
const laneMeta = payload.laneMeta || {};
const dakotaTrends = Array.isArray(payload.dakotaTrends) ? payload.dakotaTrends : [];
const dakotaExecutionMatrix = Array.isArray(payload.dakotaExecutionMatrix) ? payload.dakotaExecutionMatrix : [];
const dakotaTrendState = payload.dakotaTrendState || { available: false };
const charts = [];

const STATUS_COLOR = {
  passed: '#4ade80',
  failed: '#f87171',
  running: '#38bdf8',
};

const TREND_COLOR = '#38bdf8';
const P50_COLOR = '#94a3b8';
const BAND_COLOR = 'rgba(56, 189, 248, 0.18)';

const renderUnavailable = (element, message) => {
  element.innerHTML = `<div class="chart-empty">${message}</div>`;
};

const formatUtc = (value) => value
  ? `${new Date(value).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })} UTC`
  : 'Unavailable';

const addContext = (element, items, label = 'UTC window') => {
  const timestamps = items.map((item) => parseTime(item.started_at)).filter((value) => value !== null);
  const lastUpdated = items
    .map((item) => parseTime(item.recorded_at || item.started_at))
    .filter((value) => value !== null)
    .sort((a, b) => b - a)[0];
  const note = document.createElement('div');
  note.className = 'chart-panel__note';
  note.textContent = `${label} · ${items.length} sample${items.length === 1 ? '' : 's'} · ${timestamps.length ? `${formatUtc(timestamps[0])} – ${formatUtc(timestamps[timestamps.length - 1])}` : 'window unavailable'} · last updated ${formatUtc(lastUpdated)}`;
  element.parentElement?.appendChild(note);
};

const parseTime = (value) => {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : null;
};

const groupBy = (items, key) => {
  const map = {};
  for (const item of items) {
    const value = item[key];
    if (!map[value]) map[value] = [];
    map[value].push(item);
  }

  return map;
};

const percentile = (values, p) => {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  if (sorted.length === 1) return sorted[0];
  const idx = (sorted.length - 1) * p;
  const lower = Math.floor(idx);
  const upper = Math.ceil(idx);
  if (lower === upper) return sorted[lower];
  return sorted[lower] * (upper - idx) + sorted[upper] * (idx - lower);
};

const commonChartOptions = (extra = {}) => ({
  backgroundColor: 'transparent',
  textStyle: { fontFamily: 'Inter, sans-serif' },
  tooltip: {
    trigger: 'axis',
    backgroundColor: 'rgba(15, 23, 42, 0.95)',
    borderColor: 'rgba(125, 211, 252, 0.35)',
    textStyle: { color: '#e2e8f0' },
  },
  grid: { left: 56, right: 24, top: 24, bottom: 56, containLabel: false },
  xAxis: {
    type: 'time',
    axisLabel: { color: '#94a3b8', fontSize: 11 },
    axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
    splitLine: { show: false },
  },
  yAxis: {
    type: 'value',
    name: 'min',
    nameTextStyle: { color: '#64748b', padding: [0, 0, 0, -32] },
    axisLabel: { color: '#94a3b8', fontSize: 11 },
    splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.1)' } },
  },
  ...extra,
});

const renderLaneChart = (lane, plane) => {
  const element = document.getElementById(`builds-chart-${plane}-lane-${lane}`);
  if (!element) return;

  const laneRuns = (groupBy(runs, 'lane')[lane] || [])
    .filter((r) => r.plane === plane)
    .sort((a, b) => parseTime(a.started_at) - parseTime(b.started_at));

  if (laneRuns.length === 0) {
    renderUnavailable(element, 'No terminal runs recorded for this lane.');
    addContext(element, laneRuns);
    return;
  }
  addContext(element, laneRuns);

  const timedRuns = laneRuns.filter((r) => Number.isFinite(r.duration_min));
  const times = timedRuns.map((r) => parseTime(r.started_at));
  const p50 = [];
  const p95 = [];
  const bandFloor = [];
  const bandSpread = [];

  for (let i = 0; i < laneRuns.length; i++) {
    const windowStart = Math.max(0, i - 4);
    const window = timedRuns.slice(windowStart, i + 1).map((r) => r.duration_min);
    const median = percentile(window, 0.5);
    const upper = percentile(window, 0.95);
    const t = times[i];
    p50.push([t, median]);
    p95.push([t, upper]);
    bandFloor.push([t, median]);
    bandSpread.push([t, upper !== null && median !== null ? upper - median : null]);
  }

  const showBand = laneRuns.length >= 5;
  const meta = laneMeta[lane] || {};

  const series = [
    {
      name: 'Duration',
      type: 'line',
      showSymbol: laneRuns.length <= 30,
      symbolSize: 6,
      smooth: false,
      lineStyle: { color: TREND_COLOR, width: 2 },
      itemStyle: { color: TREND_COLOR },
      data: timedRuns.map((r) => [parseTime(r.started_at), r.duration_min, r]),
    },
  ];

  if (showBand) {
    series.push(
      {
        name: 'p50',
        type: 'line',
        showSymbol: false,
        smooth: true,
        lineStyle: { color: P50_COLOR, width: 2, type: 'dashed' },
        itemStyle: { color: P50_COLOR },
        data: p50,
      },
      {
        name: 'p50 floor',
        type: 'line',
        stack: 'band',
        showSymbol: false,
        smooth: true,
        lineStyle: { opacity: 0 },
        data: bandFloor,
      },
      {
        name: 'p50–p95 band',
        type: 'line',
        stack: 'band',
        showSymbol: false,
        smooth: true,
        lineStyle: { opacity: 0 },
        areaStyle: { color: BAND_COLOR },
        data: bandSpread,
      },
    );
  }

  const chart = echarts.init(element);
  chart.setOption(
    commonChartOptions({
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(15, 23, 42, 0.95)',
        borderColor: 'rgba(125, 211, 252, 0.35)',
        textStyle: { color: '#e2e8f0' },
        formatter: (items) => {
          const lines = [];
          const date = items[0]?.axisValueLabel ?? '';
          lines.push(`<strong>${meta.display_name || lane}</strong><br/>${date}`);
          for (const item of items) {
            if (item.seriesName === 'p50 floor' || item.seriesName === 'p50–p95 band') continue;
            const val = item.value?.[1] ?? item.value;
            if (val == null) continue;
            const unit = item.seriesName === 'Duration' && item.data?.[2]?.status
              ? ` · ${item.data[2].status}`
              : '';
            lines.push(`${item.marker}${item.seriesName}: <strong>${val} min</strong>${unit}`);
          }
          return lines.join('<br/>');
        },
      },
      legend: {
        top: 0,
        right: 0,
        textStyle: { color: '#cbd5e1', fontSize: 11 },
        data: showBand ? ['Duration', 'p50', 'p50–p95 band'] : ['Duration'],
      },
      series,
    }),
  );
  charts.push(chart);

  if (!showBand) {
    const note = document.createElement('div');
    note.className = 'chart-panel__note';
    note.textContent = `${laneRuns.length} run${laneRuns.length === 1 ? '' : 's'} — percentile band needs 5+`;
    element.parentElement.appendChild(note);
  }
};

lanes.publish.forEach((lane) => renderLaneChart(lane, 'publish'));
lanes.lab.forEach((lane) => renderLaneChart(lane, 'lab'));

// ── Dakota historical throughput and duration ──
const dakotaElement = document.getElementById('builds-chart-dakota-trends');
if (dakotaElement) {
  if (!dakotaTrendState.available || dakotaTrends.length === 0) {
    renderUnavailable(
      dakotaElement,
      `Dakota trend unavailable — ${dakotaTrendState.reason || 'no validated rows published.'}`,
    );
  } else {
    const rows = [...dakotaTrends].sort((a, b) => a.date.localeCompare(b.date));
    const dates = [...new Set(rows.map((row) => row.date))];
    const seriesFor = (recordType, field) => dates.map((date) => {
      const row = rows.find((item) => item.date === date && item.record_type === recordType);
      return row ? row[field] : null;
    });
    const durationFor = (recordType, field) => dates.map((date) => {
      const row = rows.find((item) => item.date === date && item.record_type === recordType);
      return row?.duration_seconds?.[field] ?? null;
    });
    const chart = echarts.init(dakotaElement);
    chart.setOption({
      backgroundColor: 'transparent',
      textStyle: { fontFamily: 'Inter, sans-serif' },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(15, 23, 42, 0.95)',
        borderColor: 'rgba(125, 211, 252, 0.35)',
        textStyle: { color: '#e2e8f0' },
      },
      legend: { top: 0, textStyle: { color: '#cbd5e1', fontSize: 11 } },
      grid: { left: 52, right: 58, top: 40, bottom: 40, containLabel: false },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { color: '#94a3b8', fontSize: 11 },
        axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
      },
      yAxis: [
        {
          type: 'value',
          name: 'runs',
          nameTextStyle: { color: '#64748b' },
          axisLabel: { color: '#94a3b8', fontSize: 11 },
          splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.1)' } },
        },
        {
          type: 'value',
          name: 'seconds',
          nameTextStyle: { color: '#64748b' },
          axisLabel: { color: '#94a3b8', fontSize: 11 },
          splitLine: { show: false },
        },
      ],
      series: [
        { name: 'Build runs', type: 'bar', yAxisIndex: 0, data: seriesFor('build', 'throughput'), itemStyle: { color: '#38bdf8' } },
        { name: 'Publish runs', type: 'bar', yAxisIndex: 0, data: seriesFor('publish', 'throughput'), itemStyle: { color: '#a78bfa' } },
        { name: 'Build avg duration', type: 'line', yAxisIndex: 1, connectNulls: false, data: durationFor('build', 'avg'), itemStyle: { color: '#4ade80' }, lineStyle: { color: '#4ade80', width: 2 } },
        { name: 'Publish avg duration', type: 'line', yAxisIndex: 1, connectNulls: false, data: durationFor('publish', 'avg'), itemStyle: { color: '#fbbf24' }, lineStyle: { color: '#fbbf24', width: 2 } },
      ],
    });
    charts.push(chart);
    const note = document.createElement('div');
    note.className = 'chart-panel__note';
    note.textContent = `${rows.length} daily operation row${rows.length === 1 ? '' : 's'} · generated ${formatUtc(dakotaTrendState.collectedAt)}`;
    dakotaElement.parentElement?.appendChild(note);
  }
}

const matrixElement = document.getElementById('builds-chart-dakota-execution-matrix');
if (matrixElement) {
  if (!dakotaExecutionMatrix.length) {
    renderUnavailable(matrixElement, 'Execution telemetry unavailable — no measured phase records were published.');
  } else {
    const phases = ['clone_seconds', 'fetch_seconds', 'build_seconds', 'export_seconds', 'push_seconds'];
    const labels = phases.map((phase) => phase.replace('_seconds', ''));
    const runsForMatrix = dakotaExecutionMatrix.slice(-40);
    const values = [];
    runsForMatrix.forEach((run, runIndex) => {
      phases.forEach((phase, phaseIndex) => {
        const value = run.phases?.[phase];
        values.push([phaseIndex, runIndex, Number.isFinite(value) ? value : -1, run]);
      });
    });
    const chart = echarts.init(matrixElement);
    chart.setOption({
      backgroundColor: 'transparent',
      tooltip: {
        position: 'top',
        formatter: (params) => {
          const run = params.data?.[3];
          const value = params.data?.[2];
          if (!run || value < 0) return `${params.name || 'phase'}: unavailable`;
          return `${run.date} · ${run.record_type}<br/>${labels[params.data[0]]}: <strong>${value}s</strong><br/>status: ${run.status}`;
        },
      },
      grid: { left: 110, right: 24, top: 20, bottom: 48 },
      xAxis: { type: 'category', data: labels, axisLabel: { color: '#94a3b8' } },
      yAxis: {
        type: 'category',
        data: runsForMatrix.map((run) => `${run.date} ${run.id.slice(-8)}`),
        axisLabel: { color: '#94a3b8', fontSize: 10 },
      },
      visualMap: {
        min: 0,
        max: Math.max(1, ...values.filter((item) => item[2] >= 0).map((item) => item[2])),
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        textStyle: { color: '#94a3b8' },
        inRange: { color: ['#0f172a', '#0ea5e9', '#f59e0b', '#ef4444'] },
        outOfRange: { color: '#334155' },
      },
      series: [{ type: 'heatmap', data: values, label: { show: false } }],
    });
    charts.push(chart);
  }
}

// ── Daily build outcomes (stacked area: passed/failed per day) ──
const dailyElement = document.getElementById('builds-chart-daily-outcomes');
if (dailyElement) {
  const byDay = {};
  for (const run of runs) {
    const timestamp = parseTime(run.started_at);
    const day = timestamp === null ? null : new Date(timestamp).toISOString().slice(0, 10);
    if (!day) continue;
    const bucket = byDay[day] || { date: day, passed: 0, failed: 0, running: 0 };
    if (run.status === 'passed') bucket.passed += 1;
    if (run.status === 'failed') bucket.failed += 1;
    if (run.status === 'running') bucket.running += 1;
    byDay[day] = bucket;
  }

  const days = Object.values(byDay).sort((a, b) => a.date.localeCompare(b.date));

  if (days.length === 0) {
    renderUnavailable(dailyElement, 'No terminal runs recorded for daily outcome chart.');
    addContext(dailyElement, runs);
  } else {
    addContext(dailyElement, runs);
    const chart = echarts.init(dailyElement);
    chart.setOption({
      backgroundColor: 'transparent',
      textStyle: { fontFamily: 'Inter, sans-serif' },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: 'rgba(15, 23, 42, 0.95)',
        borderColor: 'rgba(125, 211, 252, 0.35)',
        textStyle: { color: '#e2e8f0' },
        formatter: (items) => {
          const date = items[0]?.axisValueLabel ?? '';
          const lines = [`<strong>${date}</strong>`];
          for (const item of items) {
            lines.push(`${item.marker}${item.seriesName}: <strong>${item.value}</strong>`);
          }
          return lines.join('<br/>');
        },
      },
      legend: {
        top: 0,
        textStyle: { color: '#cbd5e1', fontSize: 12 },
      },
      grid: { left: 48, right: 24, top: 40, bottom: 40, containLabel: false },
      xAxis: {
        type: 'category',
        data: days.map((d) => d.date),
        axisLabel: { color: '#94a3b8', fontSize: 11 },
        axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
      },
      yAxis: {
        type: 'value',
        name: 'runs',
        nameTextStyle: { color: '#64748b' },
        axisLabel: { color: '#94a3b8', fontSize: 11 },
        splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.1)' } },
      },
      series: [
        {
          name: 'Passed',
          type: 'bar',
          stack: 'total',
          data: days.map((d) => d.passed),
          itemStyle: { color: STATUS_COLOR.passed, borderRadius: [0, 0, 0, 0] },
          areaStyle: { color: STATUS_COLOR.passed },
        },
        {
          name: 'Failed',
          type: 'bar',
          stack: 'total',
          data: days.map((d) => d.failed),
          itemStyle: { color: STATUS_COLOR.failed, borderRadius: [4, 4, 0, 0] },
          areaStyle: { color: STATUS_COLOR.failed },
        },
        {
          name: 'Running',
          type: 'bar',
          stack: 'total',
          data: days.map((d) => d.running),
          itemStyle: { color: STATUS_COLOR.running, borderRadius: [4, 4, 0, 0] },
          areaStyle: { color: STATUS_COLOR.running },
        },
      ],
    });
    charts.push(chart);
  }
}

// Single resize owner for all charts on this page.
if (charts.length) {
  window.addEventListener('resize', () => {
    charts.forEach((chart) => chart.resize());
  });
}
