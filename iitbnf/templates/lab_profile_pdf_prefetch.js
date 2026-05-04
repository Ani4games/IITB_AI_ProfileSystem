/**
 * LAB PDF PRE-GENERATION — replace the PDF section of lab_profile.html's <script> block
 *
 * Drop-in equivalent of the staff profile PDF script.
 * Uses /lab/<id>/pdf/prefetch, /lab/<id>/pdf/start, /lab/pdf/status/<job_id>,
 * and /lab/pdf/download/<job_id>.
 */

// ── State ───────────────────────────────────────────────────────────────────
const _labPrefetchJobs = {
  pdf: null,   // { job_id, year }
};

// ── Pre-generate on page load ────────────────────────────────────────────────
function _prefetchLabPDF() {
  const year = typeof selected_year !== 'undefined' ? selected_year : new Date().getFullYear();

  fetch(`/lab/${LAB_MEMBERID}/pdf/prefetch?year=${year}`)
    .then(r => r.json())
    .then(d => {
      _labPrefetchJobs.pdf = { job_id: d.job_id, year };
      console.debug('[Lab PDF prefetch] job started:', d.job_id, d.reused ? '(reused)' : '');
    })
    .catch(e => console.warn('[Lab PDF prefetch] failed:', e));
}

requestAnimationFrame(() => setTimeout(_prefetchLabPDF, 0));


// ── Modal helpers ────────────────────────────────────────────────────────────
function openPDFModal() { openModal('pdf-year-modal'); }

async function confirmPDFDownload() {
  const selectedYear = parseInt(document.getElementById('pdf-year-select').value, 10);
  closeModal('pdf-year-modal');
  openModal('pdf-progress-modal');

  const prog = document.getElementById('pdf-prog');
  const msg  = document.getElementById('pdf-status-msg');
  prog.style.width = '5%';

  const cached = _labPrefetchJobs.pdf;
  let job_id;

  if (cached && cached.year === selectedYear && cached.job_id) {
    job_id = cached.job_id;
    msg.textContent = 'PDF was pre-generated — downloading…';
  } else {
    msg.textContent = 'Starting PDF generation…';
    try {
      const res = await fetch(`/lab/${LAB_MEMBERID}/pdf/start?year=${selectedYear}`);
      const d   = await res.json();
      job_id    = d.job_id;
      _labPrefetchJobs.pdf = { job_id, year: selectedYear };
    } catch (e) {
      msg.textContent = 'Failed to start PDF generation.';
      setTimeout(() => closeModal('pdf-progress-modal'), 2000);
      return;
    }
  }

  await _labWaitAndDownload(
    job_id,
    `IITBNF_Lab_${String(LAB_MEMBERID).padStart(4, '0')}_${selectedYear}.pdf`,
  );
}

async function _labWaitAndDownload(jobId, filename) {
  const prog = document.getElementById('pdf-prog');
  const msg  = document.getElementById('pdf-status-msg');

  let pct = parseInt(prog.style.width, 10) || 5;
  const fake = setInterval(() => {
    if (pct < 90) {
      pct += Math.random() * 10;
      prog.style.width = Math.min(pct, 90) + '%';
    }
  }, 300);

  try {
    while (true) {
      await new Promise(r => setTimeout(r, 600));
      const res = await fetch(`/lab/pdf/status/${jobId}`);
      const d   = await res.json();

      if (d.status === 'done') {
        clearInterval(fake);
        prog.style.width = '100%';
        msg.textContent  = 'Download starting…';
        setTimeout(() => {
          closeModal('pdf-progress-modal');
          window.location.href = `/lab/pdf/download/${jobId}`;
        }, 350);
        return;
      }

      if (d.status === 'error') {
        clearInterval(fake);
        msg.textContent = `PDF failed: ${d.error || 'unknown error'}`;
        setTimeout(() => closeModal('pdf-progress-modal'), 2500);
        return;
      }

      if (d.status === 'not_found') {
        clearInterval(fake);
        msg.textContent = 'Job expired — please try again.';
        setTimeout(() => closeModal('pdf-progress-modal'), 2000);
        return;
      }

      msg.textContent = 'Rendering PDF…';
    }
  } catch (e) {
    clearInterval(fake);
    msg.textContent = 'Connection error.';
    setTimeout(() => closeModal('pdf-progress-modal'), 2000);
  }
}
