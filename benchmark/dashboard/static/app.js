/* Chart helpers — spec-compliant Chart.js config (dataviz skill):
   2px lines, >=8px markers with 2px surface ring, bars capped at 24px,
   hairline solid grid, legend for >=2 series, tooltips list all series. */

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

function chartTokens() {
  return {
    surface: cssVar('--surface-1'),
    ink: cssVar('--text-primary'),
    muted: cssVar('--text-muted'),
    grid: cssVar('--grid'),
    series: [cssVar('--series-1'), cssVar('--series-2'), cssVar('--series-3')],
  };
}

function baseOptions(t, yMax) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: {
        display: true,
        labels: { color: t.ink, usePointStyle: true, pointStyle: 'line', boxWidth: 24 },
      },
      tooltip: {
        backgroundColor: t.surface,
        titleColor: t.ink,
        bodyColor: t.ink,
        borderColor: t.grid,
        borderWidth: 1,
        usePointStyle: true,
      },
    },
    scales: {
      x: {
        ticks: { color: t.muted },
        grid: { display: false },
        border: { color: t.grid },
      },
      y: {
        beginAtZero: true,
        max: yMax,
        ticks: { color: t.muted },
        grid: { color: t.grid, lineWidth: 1 },
        border: { display: false },
      },
    },
  };
}

const charts = [];

function lineChart(canvasId, labels, seriesMap, yMax) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  const t = chartTokens();
  const datasets = Object.entries(seriesMap).map(([name, data], i) => ({
    label: name,
    data,
    borderColor: t.series[i % t.series.length],
    backgroundColor: t.series[i % t.series.length],
    borderWidth: 2,
    pointRadius: 4,
    pointHoverRadius: 6,
    pointBorderColor: t.surface, // 2px surface ring
    pointBorderWidth: 2,
    spanGaps: true,
    tension: 0,
  }));
  charts.push(new Chart(el, {
    type: 'line',
    data: { labels, datasets },
    options: baseOptions(t, yMax),
  }));
}

function barChart(canvasId, labels, seriesMap, yMax) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  const t = chartTokens();
  const datasets = Object.entries(seriesMap).map(([name, data], i) => ({
    label: name,
    data,
    backgroundColor: t.series[i % t.series.length],
    maxBarThickness: 24,
    borderRadius: { topLeft: 4, topRight: 4 }, // rounded data-end, square baseline
    borderSkipped: 'start',
    // 2px surface gap between adjacent bars
    borderColor: t.surface,
    borderWidth: { left: 1, right: 1, top: 0, bottom: 0 },
  }));
  const opts = baseOptions(t, yMax);
  opts.plugins.legend.labels.pointStyle = 'rect';
  charts.push(new Chart(el, {
    type: 'bar',
    data: { labels, datasets },
    options: opts,
  }));
}

function readJSON(id) {
  const el = document.getElementById(id);
  return el ? JSON.parse(el.textContent) : null;
}

/* Re-render on scheme change so dark mode gets its own validated steps. */
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  location.reload();
});

document.addEventListener('DOMContentLoaded', () => {
  if (typeof window.initCharts === 'function') window.initCharts();
});
