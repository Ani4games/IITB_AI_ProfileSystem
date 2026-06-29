/**
 * heatmap.js — GitHub-style SVG contribution heatmap. Pure SVG, no dependency.
 * mode:'status'    → categorical (attendance: present/absent/holiday/weekend)
 * mode:'intensity' → single-hue opacity scale (equipment usage density)
 */
(function () {
  const DAY_MS = 86400000;
  const STATUS_COLORS = {
    present: 'var(--green, #22c55e)',
    absent:  'var(--red, #f43f5e)',
    holiday: 'var(--faint, #3a4155)',
    weekend: 'transparent',
    none:    'var(--border, rgba(255,255,255,0.06))',
  };

  function isoDate(d) { return d.toISOString().slice(0, 10); }

  function buildYearGrid(year) {
    const start = new Date(Date.UTC(year, 0, 1));
    const end   = new Date(Date.UTC(year, 11, 31));
    const gridStart = new Date(start);
    gridStart.setUTCDate(gridStart.getUTCDate() - gridStart.getUTCDay());
    const days = [];
    for (let d = new Date(gridStart); d <= end; d = new Date(d.getTime() + DAY_MS)) {
      days.push(new Date(d));
    }
    return days;
  }

  function intensityColor(value, max) {
    if (!value) return 'var(--border, rgba(255,255,255,0.06))';
    const t = Math.min(1, value / Math.max(max, 1));
    const alpha = 0.18 + t * 0.75;
    return `rgba(245,166,35,${alpha.toFixed(2)})`;
  }

  window.renderHeatmap = function (containerId, opts) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const year    = opts.year;
    const mode    = opts.mode || 'intensity';
    const values  = opts.values || {};
    const holidays = new Set(opts.holidays || []);
    const cell = opts.cell || 11;
    const gap  = opts.gap  || 3;
    const step = cell + gap;

    const days   = buildYearGrid(year);
    const weeks  = Math.ceil(days.length / 7);
    const width  = weeks * step + 28;
    const height = 7 * step + 18;

    const maxVal = mode === 'intensity'
      ? Math.max(1, ...Object.values(values).filter(v => typeof v === 'number'))
      : 1;

    let svg = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="Activity heatmap for ${year}">`;

    let lastMonth = -1;
    days.forEach((d, i) => {
      const col = Math.floor(i / 7);
      if (d.getUTCDate() <= 7 && d.getUTCMonth() !== lastMonth && d.getUTCFullYear() === year) {
        lastMonth = d.getUTCMonth();
        const label = d.toLocaleDateString('en-US', { month: 'short', timeZone: 'UTC' });
        svg += `<text x="${28 + col * step}" y="10" font-size="9" fill="var(--muted, #94a3b8)" font-family="var(--font-mono, monospace)">${label}</text>`;
      }
    });

    days.forEach((d, i) => {
      const col = Math.floor(i / 7);
      const row = i % 7;
      const x = 28 + col * step;
      const y = 18 + row * step;
      const inYear = d.getUTCFullYear() === year;
      const key = isoDate(d);

      let fill, label;
      if (!inYear) {
        fill = 'transparent'; label = '';
      } else if (mode === 'status') {
        const isWeekend = row === 0 || row === 6;
        const status = holidays.has(key) ? 'holiday' : (values[key] || (isWeekend ? 'weekend' : 'none'));
        fill = STATUS_COLORS[status] || STATUS_COLORS.none;
        label = `${key}: ${status}`;
      } else {
        const v = values[key] || 0;
        fill = intensityColor(v, maxVal);
        label = `${key}: ${v} request${v === 1 ? '' : 's'}`;
      }

      svg += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" rx="2" fill="${fill}"><title>${label}</title></rect>`;
    });

    svg += `</svg>`;
    container.innerHTML = svg;

    if (mode === 'intensity') {
      const legend = document.createElement('div');
      legend.className = 'heatmap-legend';
      legend.innerHTML = `<span>Less</span>${[0.15,0.35,0.55,0.75,0.95].map(a =>
        `<span class="heatmap-legend-swatch" style="background:rgba(245,166,35,${a})"></span>`).join('')}<span>More</span>`;
      container.appendChild(legend);
    }
  };
})();