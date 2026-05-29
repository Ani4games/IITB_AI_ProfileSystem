/**
 * profile_shared.js
 * ═══════════════════════════════════════════════════════════════
 * Shared JavaScript for BOTH profile.html and lab_profile.html.
 *
 * What lives here:
 *  - Theme toggle
 *  - Modal open / close + keyboard / backdrop handlers
 *  - PDF split button: state machine, year picker, poll loop,
 *    waitAndDownload (status URL is page-specific — see below)
 *  - escapeHtml, _humanCol, _statusTag utilities
 *  - Session log inline loader + AI session-digest streamer
 *  - Recent-profile localStorage writer
 *
 * What does NOT live here (stays in the page):
 *  - Page-specific data constants (MEMBER_ID, _initialYear, etc.)
 *  - Page-specific PDF URL prefix (/profile/ vs /lab/)
 *  - Page-specific section loaders (loadSecondarySections, etc.)
 *  - Chart initialisation (needs page-specific canvas IDs)
 *  - AI compose engine (tab-specific targeting)
 *
 * Usage:
 *   1. In <head>:  <script>/* theme init snippet *\/</script>
 *   2. Define page constants in an inline <script> before this file:
 *        const MEMBER_ID    = {{ member_id }};
 *        const _initialYear = {{ selected_year }};
 *        const _availYears  = {{ avail_years | tojson | safe }};
 *        const _PDF_STATUS_BASE  = '/profile/pdf/status/';   // or '/lab/pdf/status/'
 *        const _PDF_DOWNLOAD_BASE= '/profile/pdf/download/'; // or '/lab/pdf/download/'
 *        const _PDF_PREFETCH_URL = '/profile/{{ member_id }}/pdf/prefetch-all'; // page-specific
 *        const _PDF_START_URL    = '/profile/{{ member_id }}/pdf/start';        // page-specific
 *   3. Load this file:  <script src="/static/profile_shared.js"></script>
 *   4. Page-specific init code goes in an inline <script> after this file.
 * ═══════════════════════════════════════════════════════════════
 */

/* ── THEME ──────────────────────────────────────────────────────── */
function toggleTheme() {
  const h = document.documentElement;
  const n = h.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  h.setAttribute('data-theme', n);
  localStorage.setItem('iitbnf_theme', n);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = n === 'dark' ? '☀ Light' : '☾ Dark';
}

// Set button label on page load (after DOM is ready)
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent =
    document.documentElement.getAttribute('data-theme') === 'dark' ? '☀ Light' : '☾ Dark';
});

/* ── MODAL SYSTEM ───────────────────────────────────────────────── */
// Pages can extend this by calling openModal/closeModal.
// Chart init hooks: pages push entries into _modalOpenHooks:
//   _modalOpenHooks['att-modal'] = initAttChart;
const _modalOpenHooks = {};

function openModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add('open');
  document.body.style.overflow = 'hidden';
  const hook = _modalOpenHooks[id];
  if (hook) hook();
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('open');
  document.body.style.overflow = '';
}

// Escape key closes all open modals
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-backdrop.open').forEach(m => {
      m.classList.remove('open');
      document.body.style.overflow = '';
    });
  }
});

// Clicking the backdrop itself (not the box) closes the modal
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal-backdrop') && e.target.classList.contains('open')) {
    e.target.classList.remove('open');
    document.body.style.overflow = '';
  }
});

/* ── UTILITY FUNCTIONS ──────────────────────────────────────────── */
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

const _COL_LABELS = {
  reservation_id: 'Res. ID',
  member_name:    'User',
  booking_start:  'Session Start',
  booking_end:    'Session End',
};

function _humanCol(col) {
  if (_COL_LABELS[col]) return _COL_LABELS[col];
  return col
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/\b\w/g, c => c.toUpperCase());
}

function _statusTag(r) {
  if (r.status_label === 'Cancelled') return '<span class="stag stag-orange">Cancelled</span>';
  if (r.status_code === 3)            return '<span class="stag stag-green">Booked</span>';
  if (r.status_code === 2)            return '<span class="stag stag-red">Rejected</span>';
  if (r.status_code === 1)            return '<span class="stag stag-blue">Approved</span>';
  return '<span class="stag stag-amber">Pending</span>';
}

/* ── PDF SPLIT BUTTON ───────────────────────────────────────────── */
// Per-page constants that MUST be set before this file loads:
//   _PDF_STATUS_BASE   — e.g. '/profile/pdf/status/'  or '/lab/pdf/status/'
//   _PDF_DOWNLOAD_BASE — e.g. '/profile/pdf/download/' or '/lab/pdf/download/'
//   _PDF_START_URL     — e.g. '/profile/189/pdf/start?year='
//   _PDF_PREFETCH_INIT — function called once to fire the prefetch request(s)

const _prefetchJobs = new Map(); // year → job_id (main profile PDF)

function _setPDFBtnState(state) {
  const dot = document.getElementById('pdf-btn-dot');
  if (!dot) return;
  if (state === 'rendering') {
    dot.style.cssText = 'width:6px;height:6px;border-radius:50%;background:#f59e0b;opacity:0.6;display:inline-block;animation:pulse 1.2s ease-in-out infinite;flex-shrink:0;';
  } else if (state === 'ready') {
    dot.style.cssText = 'width:6px;height:6px;border-radius:50%;background:#22c55e;opacity:1;display:inline-block;box-shadow:0 0 6px #22c55e;flex-shrink:0;';
  } else {
    dot.style.cssText = 'width:6px;height:6px;border-radius:50%;background:var(--amber);opacity:0.35;display:inline-block;flex-shrink:0;';
  }
}

let _btnPollTimer = null;
function _startBtnPoll(jobId) {
  if (_btnPollTimer) clearInterval(_btnPollTimer);
  _setPDFBtnState('rendering');
  _btnPollTimer = setInterval(async () => {
    try {
      const r = await fetch(_PDF_STATUS_BASE + jobId);
      const d = await r.json();
      if (d.status === 'done') {
        clearInterval(_btnPollTimer); _btnPollTimer = null;
        _setPDFBtnState('ready');
      } else if (d.status === 'error' || d.status === 'not_found') {
        clearInterval(_btnPollTimer); _btnPollTimer = null;
        _setPDFBtnState('idle');
      }
    } catch (_) {}
  }, 2000);
}

// Main button click — if already done, download immediately; else show picker
async function handlePDFClick() {
  const jid = _prefetchJobs.get(_initialYear);
  if (jid) {
    try {
      const r = await fetch(_PDF_STATUS_BASE + jid);
      const d = await r.json();
      if (d.status === 'done') { window.location.href = _PDF_DOWNLOAD_BASE + jid; return; }
    } catch (_) {}
  }
  togglePDFPicker({ stopPropagation: () => {} });
}

function togglePDFPicker(e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const picker = document.getElementById('pdf-year-picker');
  const caret  = document.getElementById('pdf-caret-btn');
  if (!picker) return;
  const isOpen = picker.classList.toggle('open');
  if (caret) caret.classList.toggle('open', isOpen);
  if (isOpen) {
    // Refresh status dots on each year option
    (_availYears || []).forEach(y => {
      const dot = document.getElementById(`pdf-year-status-${y}`);
      if (!dot) return;
      const jid = _prefetchJobs.get(y);
      if (!jid) return;
      fetch(_PDF_STATUS_BASE + jid)
        .then(r => r.json())
        .then(d => {
          dot.className = 'pdf-year-status ' +
            (d.status === 'done' ? 'ready' : d.status === 'processing' ? 'rendering' : '');
        }).catch(() => {});
    });
    setTimeout(() => document.addEventListener('click', _closePDFPicker, { once: true }), 0);
  }
}

function _closePDFPicker(e) {
  const wrap = document.getElementById('pdf-split-wrap');
  if (wrap && wrap.contains(e.target)) return;
  const picker = document.getElementById('pdf-year-picker');
  const caret  = document.getElementById('pdf-caret-btn');
  if (picker) picker.classList.remove('open');
  if (caret)  caret.classList.remove('open');
}

async function pickPDFYear(year) {
  const picker = document.getElementById('pdf-year-picker');
  const caret  = document.getElementById('pdf-caret-btn');
  if (picker) picker.classList.remove('open');
  if (caret)  caret.classList.remove('open');

  const jid = _prefetchJobs.get(year);
  if (jid) {
    try {
      const r = await fetch(_PDF_STATUS_BASE + jid);
      const d = await r.json();
      if (d.status === 'done') { window.location.href = _PDF_DOWNLOAD_BASE + jid; return; }
    } catch (_) {}
  }

  openModal('pdf-progress-modal');
  const prog = document.getElementById('pdf-prog');
  const msg  = document.getElementById('pdf-status-msg');
  if (prog) prog.style.width = '5%';

  let activeJid = jid;
  if (!activeJid) {
    if (msg) msg.textContent = 'Starting PDF generation…';
    try {
      const res = await fetch(_PDF_START_URL + year);
      const d   = await res.json();
      activeJid = d.job_id;
      _prefetchJobs.set(year, activeJid);
    } catch (err) {
      if (msg) msg.textContent = 'Failed to start PDF generation.';
      setTimeout(() => closeModal('pdf-progress-modal'), 2000);
      return;
    }
  } else {
    if (msg) msg.textContent = 'PDF is rendering — almost ready…';
  }

  await _waitAndDownload(activeJid);
}

async function _waitAndDownload(jobId) {
  const prog = document.getElementById('pdf-prog');
  const msg  = document.getElementById('pdf-status-msg');
  let pct = parseInt((prog && prog.style.width) || '5', 10) || 5;
  const fake = setInterval(() => {
    if (pct < 92) {
      pct += pct < 60 ? Math.random() * 12 : Math.random() * 4;
      if (prog) prog.style.width = Math.min(pct, 92) + '%';
    }
  }, 250);

  try {
    let first = true;
    while (true) {
      if (!first) await new Promise(r => setTimeout(r, 300));
      first = false;
      const r = await fetch(_PDF_STATUS_BASE + jobId);
      const d = await r.json();
      if (d.status === 'done') {
        clearInterval(fake);
        if (prog) prog.style.width = '100%';
        if (msg)  msg.textContent = 'Download starting…';
        _setPDFBtnState('ready');
        setTimeout(() => {
          closeModal('pdf-progress-modal');
          window.location.href = _PDF_DOWNLOAD_BASE + jobId;
        }, 280);
        return;
      }
      if (d.status === 'error') {
        clearInterval(fake);
        if (msg) msg.textContent = `PDF generation failed: ${d.error || 'unknown error'}`;
        setTimeout(() => closeModal('pdf-progress-modal'), 2500);
        return;
      }
      if (d.status === 'not_found') {
        clearInterval(fake);
        if (msg) msg.textContent = 'Job expired — please try again.';
        setTimeout(() => closeModal('pdf-progress-modal'), 2000);
        return;
      }
      if (msg) msg.textContent = 'Rendering PDF…';
    }
  } catch (e) {
    clearInterval(fake);
    if (msg) msg.textContent = 'Connection error during PDF generation.';
    setTimeout(() => closeModal('pdf-progress-modal'), 2000);
  }
}

// Secondary PDF downloads (system-owner PDFs, etc.)
// _altPrefetchJobs: { key: job_id } — page sets this up
const _altPrefetchJobs = {};

async function _downloadWithPrefetch(cachedJobId, startUrl, onNewJob) {
  openModal('pdf-progress-modal');
  const prog = document.getElementById('pdf-prog');
  const msg  = document.getElementById('pdf-status-msg');
  if (prog) prog.style.width = '5%';

  let job_id = cachedJobId;
  if (job_id) {
    try {
      const check = await fetch(_PDF_STATUS_BASE + job_id);
      const d = await check.json();
      if (d.status === 'error' || d.status === 'not_found') job_id = null;
    } catch (_) { job_id = null; }
  }

  if (!job_id) {
    if (msg) msg.textContent = 'Starting PDF generation…';
    try {
      const res = await fetch(startUrl);
      const d   = await res.json();
      job_id    = d.job_id;
      if (onNewJob) onNewJob(job_id);
    } catch (e) {
      if (msg) msg.textContent = 'Failed to start PDF generation.';
      setTimeout(() => closeModal('pdf-progress-modal'), 2000);
      return;
    }
  } else {
    if (msg) msg.textContent = 'PDF was pre-generated — downloading…';
  }

  await _waitAndDownload(job_id);
}

/* ── SESSION LOG INLINE LOADER ──────────────────────────────────── */
async function _loadSessionLog(machid, toolName, panel, limit, memberId) {
  panel.innerHTML = `<p style="color:var(--muted);font-size:1.25rem;padding:.5rem 0;">Loading session log for <strong>${escapeHtml(toolName)}</strong>…</p>`;
  try {
    const url =`/api/section/tool/${machid}/session_log?limit=${limit}&member_id=${memberId}`;
    const r = await fetch(url);
    const d = await r.json();
    if (!d.success) {
      panel.innerHTML = `<p style="color:var(--red);font-size:1.25rem;padding:.5rem 0;">${escapeHtml(d.error || 'Could not load session log.')}</p>`;
      return;
    }
    if (!d.rows?.length) {
      panel.innerHTML = `<p style="color:var(--muted);font-size:1.25rem;padding:.5rem 0;font-style:italic;">No session log entries found.</p>`;
      return;
    }

    const SKIP = new Set(['member_id', 'member_position']);
    const FIXED_COLS = ['reservation_id', 'member_name', 'booking_start', 'booking_end'];
    // Using FIXED_COLS instead of d.columns when building the table header and rows

    const thCells = FIXED_COLS.map(c =>
      `<th style="font-family:var(--font-mono);font-size:1.00rem;color:var(--amber);letter-spacing:.07em;text-transform:uppercase;padding:.3rem .55rem;text-align:left;border-bottom:1px solid var(--border2);white-space:nowrap;">${_humanCol(c)}</th>`
    ).join('');

    const tdRows = d.rows.map(row => {
      const cells = FIXED_COLS.map(c => {
        const val = row[c];
        const display = (val === null || val === undefined || val === '') ? '—' : String(val);
        let style = 'padding:.3rem .55rem;border-bottom:1px solid var(--border);font-size:1.25rem;vertical-align:middle;';
        let content = escapeHtml(display);
        if (c === 'reservation_id')   style += 'font-family:var(--font-mono);color:var(--amber);';
        else if (c === 'member_name') style += 'font-weight:600;';
        else if (c === 'booking_start' || c === 'booking_end')
          style += 'font-family:var(--font-mono);font-size:1.00rem;color:var(--muted);';
        else if (c === 'Status') {
          const v = display.toLowerCase();
          if (v === 'on' || v === 'ok') content = `<span class="stag stag-green">${escapeHtml(display)}</span>`;
          else if (v === 'off')         content = `<span class="stag stag-muted">${escapeHtml(display)}</span>`;
        } else if (c === 'baseline_run') {
          content = display === '1'
            ? `<span class="stag stag-amber">Baseline</span>`
            : `<span class="stag stag-muted">Normal</span>`;
        } else if (c === 'Remarks' || c === 'remarks') {
          style += 'color:var(--muted);font-size:1.00rem;max-width:180px;white-space:normal;word-break:break-word;';
        }
        return `<td style="${style}">${content}</td>`;
      }).join('');
      return `<tr onmouseover="this.style.background='var(--surface3)'" onmouseout="this.style.background=''">${cells}</tr>`;
    }).join('');

    const loadMoreBtn = (d.total >= limit && limit < 200)
      ? `· <button onclick="_loadSessionLog(${machid},'${escapeHtml(toolName)}',document.getElementById('sys-log-panel-${machid}'),200)"
               style="background:none;border:none;color:var(--blue);font-family:var(--font-mono);font-size:1.00rem;cursor:pointer;padding:0;">Load all (max 200)</button>`
      : '';

    panel.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.5rem;gap:.5rem;flex-wrap:wrap;">
        <span style="font-family:var(--font-mono);font-size:.6rem;color:var(--amber);letter-spacing:.1em;text-transform:uppercase;">◈ Session Log — ${escapeHtml(toolName)}</span>
        <span style="font-family:var(--font-mono);font-size:.58rem;color:var(--faint);">${d.total} rows shown ${loadMoreBtn}</span>
      </div>
      <div style="overflow-x:auto;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--faint) transparent;">
        <table style="width:100%;border-collapse:collapse;min-width:480px;">
          <thead style="position:sticky;top:0;background:var(--surface2);z-index:1;"><tr>${thCells}</tr></thead>
          <tbody>${tdRows}</tbody>
        </table>
      </div>`;
  } catch (e) {
    panel.innerHTML = `<p style="color:var(--red);font-size:1.00rem;padding:.5rem 0;">Network error: ${escapeHtml(e.message)}</p>`;
  }
}

/* ── AI SESSION DIGEST STREAMER ─────────────────────────────────── */
let _openDigest = null;

function toggleSessionDigest(machid, toolName) {
  const row   = document.getElementById(`digest-row-${machid}`);
  const panel = document.getElementById(`digest-panel-${machid}`);
  const btn   = document.getElementById(`digest-btn-${machid}`);
  if (!row || !panel) return;

  const isOpen = row.style.display !== 'none';
  if (_openDigest && _openDigest !== machid) {
    const p = document.getElementById(`digest-row-${_openDigest}`);
    const b = document.getElementById(`digest-btn-${_openDigest}`);
    if (p) p.style.display = 'none';
    if (b) b.textContent = '✦ AI Digest';
  }
  if (isOpen) { row.style.display = 'none'; if (btn) btn.textContent = '✦ AI Digest'; _openDigest = null; return; }

  row.style.display = '';
  _openDigest = machid;
  if (panel.innerHTML.trim()) { if (btn) btn.textContent = '✦ AI Digest'; return; }
  if (btn) btn.textContent = '✦ Generating…';
  _streamSessionDigest(machid, toolName, panel, btn);
}

function _streamSessionDigest(machid, toolName, panel, btn) {
  panel.innerHTML = `
    <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.55rem;">
      <span style="font-family:var(--font-mono);font-size:1.25rem;color:var(--amber);letter-spacing:.1em;text-transform:uppercase;">✦ AI Session Digest — ${escapeHtml(toolName)}</span>
      <span id="digest-status-${machid}" style="font-family:var(--font-mono);font-size:1.25rem;color:var(--faint);">Generating…</span>
    </div>
    <div id="digest-output-${machid}" style="font-family:var(--font-body);font-size:1.25rem;line-height:1.8;color:var(--fg);white-space:pre-wrap;min-height:2rem;"></div>`;

  const outputEl = document.getElementById(`digest-output-${machid}`);
  const statusEl = document.getElementById(`digest-status-${machid}`);
  const es = new EventSource(`/api/ai/session-digest?machid=${machid}&tool_name=${encodeURIComponent(toolName)}`);
  let fullText = '', meta = null;

  es.onmessage = e => {
    const raw = e.data;
    if (raw === '[DONE]') {
      es.close();
      if (btn) btn.textContent = '✦ AI Digest';
      if (statusEl && meta) statusEl.textContent = `${meta.useful} report${meta.useful !== 1 ? 's' : ''}`;
      return;
    }
    if (raw.startsWith('[ERROR]')) {
      es.close();
      if (outputEl) outputEl.textContent = raw;
      if (statusEl) statusEl.textContent = 'Error';
      if (btn) btn.textContent = '✦ AI Digest';
      return;
    }
    try {
      const p = JSON.parse(raw);
      if (p?.type === 'meta') { meta = p; return; }
      if (typeof p === 'string') { fullText += p; if (outputEl) outputEl.textContent = fullText; }
    } catch (_) {}
  };
  es.onerror = () => {
    es.close();
    if (outputEl && !fullText) outputEl.textContent = 'Connection error.';
    if (statusEl) statusEl.textContent = 'Error';
    if (btn) btn.textContent = '✦ AI Digest';
  };
}

/* ── RECENT PROFILES WRITER ─────────────────────────────────────── */
// Call this from the page's inline <script> with the current profile data.
// Example: _writeRecentProfile('0189', 'Dr. A Sharma', '/profile/189', 'staff');
function _writeRecentProfile(id, name, url, type) {
  try {
    const KEY  = 'iitbnf_recent_profiles';
    const MAX  = 8;
    const item = { id, name, url, type };
    let list   = JSON.parse(localStorage.getItem(KEY) || '[]');
    list = list.filter(r => !(r.id === id && r.type === type));
    list.unshift(item);
    if (list.length > MAX) list = list.slice(0, MAX);
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch (e) { /* localStorage blocked */ }
}
