/**
 * sparkline.js — tiny inline Chart.js sparklines for metric cards.
 * Uses the Chart.js instance already loaded by base_profile.html /
 * admin_panel.html (cdnjs Chart.js 4.4.1). No new dependency.
 */
(function () {
  const _sparkInstances = {};

  window.renderSparkline = function (canvasId, data, opts) {
    opts = opts || {};
    const canvas = document.getElementById(canvasId);
    if (!canvas || !window.Chart) return null;
    if (!Array.isArray(data) || data.length === 0) return null;

    const kind = opts.kind || 'bar';
    const cssAccent = getComputedStyle(document.documentElement)
      .getPropertyValue('--amber').trim() || '#f5a623';
    const color = opts.color || cssAccent;

    if (_sparkInstances[canvasId]) _sparkInstances[canvasId].destroy();

    const datasets = [{
      data: data,
      backgroundColor: kind === 'bar' ? hexToRgba(color, 0.55) : 'transparent',
      borderColor: color,
      borderWidth: kind === 'bar' ? 0 : 1.5,
      borderRadius: kind === 'bar' ? 2 : 0,
      barPercentage: 0.7,
      categoryPercentage: 0.85,
      pointRadius: 0,
      tension: 0.35,
      fill: kind === 'line' ? 'start' : false,
    }];

    if (opts.threshold != null) {
      datasets.push({
        data: data.map(() => opts.threshold),
        type: 'line',
        borderColor: 'rgba(148,163,184,0.45)',
        borderDash: [3, 3],
        borderWidth: 1,
        pointRadius: 0,
        fill: false,
      });
    }

    const chart = new Chart(canvas.getContext('2d'), {
      type: kind === 'bar' ? 'bar' : 'line',
      data: { labels: data.map((_, i) => i), datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false, beginAtZero: true } },
      },
    });

    _sparkInstances[canvasId] = chart;
    return chart;
  };

  function hexToRgba(hex, alpha) {
    if (hex.startsWith('rgb')) return hex;
    let h = hex.replace('#', '');
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }
})();