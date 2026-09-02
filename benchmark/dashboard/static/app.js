/* Dashboard behaviour: Chart.js helpers with fixed vendor colour slots,
   run selection → compare bar, findings-matrix popover / diff toggle /
   column hover. No build step, no external requests. */

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

function chartTokens() {
  return {
    surface: cssVar('--surface-1'),
    ink: cssVar('--text-primary'),
    muted: cssVar('--text-muted'),
    grid: cssVar('--grid'),
    series: [1, 2, 3, 4, 5, 6].map((i) => cssVar(`--series-${i}`)),
    neutral: cssVar('--status-neutral'),
  };
}

/* Vendor slot → colour; slots beyond the palette fall back to neutral. */
function seriesColorFor(slot, t) {
  t = t || chartTokens();
  return slot != null && slot < t.series.length ? t.series[slot] : t.neutral;
}

function baseOptions(t, yMax) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: {
        display: true,
        position: 'bottom',
        labels: { color: t.ink, usePointStyle: true, pointStyle: 'line', boxWidth: 24, boxHeight: 8, padding: 14 },
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
      x: { ticks: { color: t.muted }, grid: { display: false }, border: { color: t.grid } },
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

/* series: [{label, data, slot}] */
function lineChart(canvasId, labels, series, yMax) {
  const el = document.getElementById(canvasId);
  if (!el || typeof Chart === 'undefined') return;
  const t = chartTokens();
  const datasets = series.map((s, i) => {
    const color = seriesColorFor(s.slot != null ? s.slot : i, t);
    return {
      label: s.label,
      data: s.data,
      borderColor: color,
      backgroundColor: color,
      borderWidth: 2,
      pointRadius: 4,
      pointHoverRadius: 6,
      pointBorderColor: t.surface,
      pointBorderWidth: 2,
      spanGaps: true,
      tension: 0,
    };
  });
  charts.push(new Chart(el, { type: 'line', data: { labels, datasets }, options: baseOptions(t, yMax) }));
}

/* series: [{label, data, slot?}] — grouped bars, one colour per series. */
function barChart(canvasId, labels, series, yMax) {
  const el = document.getElementById(canvasId);
  if (!el || typeof Chart === 'undefined') return;
  const t = chartTokens();
  const datasets = series.map((s, i) => ({
    label: s.label,
    data: s.data,
    backgroundColor: seriesColorFor(s.slot != null ? s.slot : i, t),
    maxBarThickness: 22,
    borderRadius: { topLeft: 3, topRight: 3 },
    borderSkipped: 'start',
    borderColor: t.surface,
    borderWidth: { left: 1, right: 1, top: 0, bottom: 0 },
  }));
  const opts = baseOptions(t, yMax);
  opts.plugins.legend.labels.pointStyle = 'rect';
  charts.push(new Chart(el, { type: 'bar', data: { labels, datasets }, options: opts }));
}

function readJSON(id) {
  const el = document.getElementById(id);
  return el ? JSON.parse(el.textContent) : null;
}

/* ---------- runs list: selection → compare bar ---------- */

function initRunSelection() {
  const boxes = Array.from(document.querySelectorAll('input[data-run-select]'));
  if (!boxes.length) return;
  const bar = document.getElementById('compare-bar');
  const count = document.getElementById('compare-count');
  const ids = document.getElementById('compare-ids');
  const go = document.getElementById('compare-go');
  const clear = document.getElementById('compare-clear');
  const KEY = 'bench.compare.selected';

  let stored = null;
  try { stored = JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (_) { stored = null; }
  const preset = Array.isArray(stored) ? stored : (readJSON('default-compare') || []);
  boxes.forEach((b) => { b.checked = preset.includes(Number(b.dataset.runSelect)); });

  function selected() {
    return boxes.filter((b) => b.checked).map((b) => Number(b.dataset.runSelect)).sort((a, b) => a - b);
  }
  function render() {
    const sel = selected();
    boxes.forEach((b) => b.closest('.run-row').classList.toggle('selected', b.checked));
    try { sessionStorage.setItem(KEY, JSON.stringify(sel)); } catch (_) { /* private mode */ }
    if (!bar) return;
    bar.hidden = sel.length === 0;
    count.textContent = `${sel.length} run${sel.length === 1 ? '' : 's'} selected`;
    ids.textContent = sel.length ? '#' + sel.join(', #') : '';
    go.disabled = sel.length < 1;
    go.href = '/compare?runs=' + sel.join(',');
  }
  boxes.forEach((b) => b.addEventListener('change', render));
  if (clear) clear.addEventListener('click', () => { boxes.forEach((b) => { b.checked = false; }); render(); });
  render();
}

/* ---------- findings matrix ---------- */

function initMatrix() {
  const table = document.querySelector('table.matrix');
  if (!table) return;
  const dlg = document.getElementById('cell-popover');

  // click a cell → popover with the judge detail
  table.addEventListener('click', (ev) => {
    const c = ev.target.closest('.c[data-detail]');
    if (!c || !dlg) return;
    let d;
    try { d = JSON.parse(c.dataset.detail); } catch (_) { return; }
    renderPopover(dlg, d, c.dataset.label);
    dlg.showModal();
  });
  if (dlg) {
    dlg.addEventListener('click', (ev) => { if (ev.target === dlg) dlg.close(); });
    const x = dlg.querySelector('.close');
    if (x) x.addEventListener('click', () => dlg.close());
  }

  // diff-only toggle
  const diff = document.getElementById('diff-toggle');
  if (diff) {
    const url = new URL(location.href);
    const apply = () => {
      table.classList.toggle('diff-only', diff.checked);
      url.searchParams.set('diff', diff.checked ? '1' : '0');
      history.replaceState(null, '', url);
    };
    diff.addEventListener('change', apply);
    apply();
  }

  // column hover highlight
  let last = null;
  table.addEventListener('mouseover', (ev) => {
    const cell = ev.target.closest('td[data-col], th[data-col]');
    const col = cell ? cell.dataset.col : null;
    if (col === last) return;
    if (last !== null) table.querySelectorAll(`[data-col="${last}"]`).forEach((e) => e.classList.remove('col-hover'));
    if (col !== null) table.querySelectorAll(`[data-col="${col}"]`).forEach((e) => e.classList.add('col-hover'));
    last = col;
  });
  table.addEventListener('mouseleave', () => {
    if (last !== null) table.querySelectorAll(`[data-col="${last}"]`).forEach((e) => e.classList.remove('col-hover'));
    last = null;
  });
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function renderPopover(dlg, d, label) {
  const head = dlg.querySelector('.pop-title');
  const body = dlg.querySelector('.pop-body');
  const sev = (s) => (s ? `<span class="sev sev-${esc(s)}">${esc(s)}</span>` : '<span class="muted">—</span>');
  head.innerHTML = `<span class="chip-status st-${esc(d.status)}"><span class="g">${esc(d.glyph)}</span>${esc(label)}</span>
    <span class="mono">${esc(d.golden_id)}</span>
    <span class="muted small">run #${esc(d.run_id)} · ${esc(d.case_id)} · rep ${esc(d.repetition)}</span>`;
  const rows = [];
  if (d.scored_as) rows.push(['Scored as', esc(d.scored_as)]);
  rows.push(['Expected (key)', `<div class="quote">${esc(d.expected_description)}</div><div style="margin-top:4px">${sev(d.expected_severity)}${d.optional ? ' <span class="tag-opt">optional</span>' : ''}${d.retired ? ' <span class="tag-retired">retired from key</span>' : ''}</div>`]);
  if (d.actual_description || d.actual_weakness_id) {
    rows.push(['Reported by app', `<div class="quote">${esc(d.actual_description || '(see classification)')}</div><div style="margin-top:4px">${sev(d.actual_severity)}${d.sev_mismatch ? ` <span class="small muted">severity ${d.sev_mismatch === 'up' ? 'rated higher' : d.sev_mismatch === 'down' ? 'rated lower' : 'differs'} than the key</span>` : ''} <span class="muted small">weakness #${esc(d.actual_weakness_id)}</span></div>`]);
  }
  if (d.confidence != null || d.counted != null) rows.push(['Matcher', `confidence <b>${esc(d.confidence || 'n/a')}</b> · ${d.counted ? 'counted' : 'not counted'}`]);
  if (d.justification) rows.push(['Matcher says', esc(d.justification)]);
  if (d.category) rows.push(['Classifier', `<span class="mono">${esc(d.category)}</span>${d.category_reason ? ' — ' + esc(d.category_reason) : ''}`]);
  if (d.dup) rows.push(['Duplicates', 'Another reported weakness was classified as a duplicate of this hit.']);
  if (d.missed_where || d.missed_note) rows.push(['Evidence', `${d.missed_where ? '<div><b>Where:</b> ' + esc(d.missed_where) + '</div>' : ''}${d.missed_note ? '<div><b>Note:</b> ' + esc(d.missed_note) + '</div>' : ''}`]);
  if (d.error_stage) rows.push(['Error', `<b>${esc(d.error_stage)}</b> ${esc(d.error_detail)}`]);
  if (d.case_result_id) rows.push(['', `<a href="/runs/${esc(d.run_id)}/cases/${esc(d.case_result_id)}">Open case detail →</a>`]);
  body.innerHTML = '<dl>' + rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('') + '</dl>';
}

/* Re-render on scheme change so dark mode gets its own validated steps. */
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => location.reload());

document.addEventListener('DOMContentLoaded', () => {
  if (typeof window.initCharts === 'function') window.initCharts();
  initRunSelection();
  initMatrix();
});
