/**
 * timeline.js — merges attendance / slot activity / reservations into
 * one 12-column month-by-month view. Pure CSS bars, no chart library.
 *
 * Call mergeTimelineData() with data you already have from existing
 * endpoints, then renderTimeline() to draw it. No new backend routes —
 * this only re-shapes data the page already fetches.
 */
(function () {
  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  function monthOf(dateStr) {
    if (!dateStr) return null;
    const d = new Date(dateStr);
    return isNaN(d) ? null : d.getMonth(); // 0-indexed
  }

  /**
   * @param attendanceTrend  array from /attendance response: [{month, attendance_pct}]
   * @param slotRows         array from /slot_activity response .rows: [{date_requested}]
   * @param reservationRows  array from /reservations response: [{start_dt}]
   * @returns 12-length array: [{month, attendancePct, slots, reservations}]
   */
  window.mergeTimelineData = function (attendanceTrend, slotRows, reservationRows) {
    const out = MONTHS.map((label, i) => ({ month: i, label, attendancePct: 0, slots: 0, reservations: 0 }));

    (attendanceTrend || []).forEach(m => {
      const idx = (m.month || 1) - 1;
      if (out[idx]) out[idx].attendancePct = m.attendance_pct || 0;
    });

    (slotRows || []).forEach(r => {
      const idx = monthOf(r.date_requested);
      if (idx != null && out[idx]) out[idx].slots += 1;
    });

    (reservationRows || []).forEach(r => {
      const idx = monthOf(r.start_dt);
      if (idx != null && out[idx]) out[idx].reservations += 1;
    });

    return out;
  };

  window.renderTimeline = function (containerId, monthsData) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const maxSlots = Math.max(1, ...monthsData.map(m => m.slots));
    const maxRes   = Math.max(1, ...monthsData.map(m => m.reservations));

    container.innerHTML = `
      <div class="timeline-legend">
        <span><i class="timeline-dot timeline-dot--att"></i> Attendance %</span>
        <span><i class="timeline-dot timeline-dot--slot"></i> Equipment requests</span>
        <span><i class="timeline-dot timeline-dot--res"></i> Reservations</span>
      </div>
      <div class="timeline-grid">
        ${monthsData.map(m => `
          <div class="timeline-col">
            <div class="timeline-bars">
              <div class="timeline-bar timeline-bar--att" style="height:${m.attendancePct}%" title="${m.label}: ${m.attendancePct}% attendance"></div>
              <div class="timeline-bar timeline-bar--slot" style="height:${(m.slots / maxSlots) * 100}%" title="${m.label}: ${m.slots} equipment requests"></div>
              <div class="timeline-bar timeline-bar--res" style="height:${(m.reservations / maxRes) * 100}%" title="${m.label}: ${m.reservations} reservations"></div>
            </div>
            <div class="timeline-label">${m.label}</div>
          </div>
        `).join('')}
      </div>`;
  };
})();