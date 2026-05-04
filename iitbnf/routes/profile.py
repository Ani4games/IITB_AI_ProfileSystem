"""
routes/profile.py — /profile/<id>, /profile/<id>/pdf,
                    /api/profile/<id>/system-owner-pdf,
                    /api/profile/<id>/system-owner-track-pdf

PDF Pre-generation
──────────────────
PDFs are now pre-generated in the background the moment a profile page loads,
not when the user clicks "Download PDF". The page fires a silent
fetch('/profile/<id>/pdf/prefetch') immediately on load. This starts the
background render job and caches the job_id in PDF_PREFETCH keyed by
(member_id, year). When the user finally clicks the download button, the job
is usually already done — they see instant or near-instant download.

The same pattern is designed to extend to AI narrative generation:
fire-and-forget a /prefetch endpoint on page load, cache the result,
serve it instantly when the user opens the AI panel.

Performance notes
─────────────────
1. _warmup_uid() called before run_parallel() — prevents 4 parallel tasks
   from each triggering the expensive UID resolution on first visit.
2. PDF row limits (300) keep render time predictable.
3. WeasyPrint is pre-warmed at app startup (app.py).
"""
import os
import uuid
import threading
import time
import re
import traceback
from datetime import date, datetime
from flask import (
    Blueprint, render_template, request, make_response,
    jsonify, send_file, url_for, redirect, current_app
)
from auth import staff_required, is_full_access
from models.lab import safe_json
from utils import run_parallel, safe_dict

# ── Job stores ────────────────────────────────────────────────────────────────
# PDF_JOBS  : {job_id: {"status": "processing"|"done"|"error", "file": path}}
# PDF_PREFETCH: {(member_id, year): job_id}  — maps a profile+year → prefetch job
PDF_JOBS: dict     = {}
PDF_PREFETCH: dict = {}

# Lock for PDF_PREFETCH to avoid duplicate concurrent pre-gen for same profile
_prefetch_lock = threading.Lock()


def _html_to_pdf(html_string: str) -> bytes:
    """
    Convert an HTML string to PDF bytes using xhtml2pdf (pisa).

    xhtml2pdf is 5-10x faster than WeasyPrint for these document-style
    templates because it uses ReportLab directly instead of a full browser
    layout engine. All four PDF templates use only CSS that xhtml2pdf
    supports: tables, floats, borders, colours, page-break-inside.
    """
    import io
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    result = pisa.CreatePDF(
        src=html_string,
        dest=buf,
        encoding="utf-8",
    )
    if result.err:
        raise ValueError(f"xhtml2pdf error: {result.err}")
    return buf.getvalue()


from models.staff import (
    get_person, get_attendance_stats, get_slot_activity,
    get_project_data,
    get_permissions,
    get_attendance_trend, get_available_years,
    get_staff_system_owned, get_staff_owner_track,
    get_staff_tool_perms_rich,
    _warmup_uid,
)

bp = Blueprint("profile", __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_date(val):
    if not val:
        return None
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    try:
        return str(val)
    except Exception:
        return None


def sanitize_list(data, limit: int | None = None):
    rows = data or []
    if limit:
        rows = rows[:limit]
    out = []
    for row in rows:
        clean = {}
        for k, v in row.items():
            if isinstance(v, (datetime, date)):
                clean[k] = v.strftime("%Y-%m-%d")
            else:
                clean[k] = v
        out.append(clean)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# NORMAL PROFILE PAGE
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/profile/<int:member_id>")
@staff_required
def profile(member_id):
    start_total = time.time()
    year_req    = request.args.get("year", type=int)
    full_access = is_full_access()

    _warmup_uid(member_id)

    data = run_parallel({
        "person":      lambda: get_person(member_id),
        "avail_years": lambda: get_available_years(member_id=member_id),
        "attendance":  lambda: get_attendance_stats(member_id, year_req or date.today().year),
        "trend":       lambda: get_attendance_trend(member_id, year_req or date.today().year),
    })

    if not data.get("person"):
        return render_template("not_found.html", member_id=member_id), 404

    avail_years, best_year = data.get("avail_years") or ([date.today().year], date.today().year)

    html = render_template(
        "profile.html",
        person        = safe_dict(data["person"]),
        att           = safe_json(data.get("attendance", {})),
        trend         = data.get("trend", []),
        full_access   = full_access,
        selected_year = best_year,
        avail_years   = avail_years,
        member_id     = member_id,
    )

    response = make_response(html)
    response.headers["X-Total-Time"] = f"{round((time.time()-start_total)*1000,2)}ms"
    return response


# ══════════════════════════════════════════════════════════════════════════════
# CORE PDF GENERATION (shared by on-demand + prefetch)
# ══════════════════════════════════════════════════════════════════════════════

def _generate_pdf_job(app, job_id: str, member_id: int, year: int):
    """
    Background thread: renders the staff PDF and stores the result in PDF_JOBS.
    Called by both the prefetch endpoint and the on-demand start endpoint.
    """
    with app.app_context():
        try:
            _warmup_uid(member_id)

            data = run_parallel({
                "person":          lambda: get_person(member_id),
                "attendance":      lambda: get_attendance_stats(member_id, year),
                "slot_activity":   lambda: get_slot_activity(member_id, year),
                "projects":        lambda: get_project_data(member_id),
                "permissions":     lambda: get_permissions(member_id),
                "system_owned":    lambda: get_staff_system_owned(member_id),
                "owner_track":     lambda: get_staff_owner_track(member_id),
                "tool_perms_rich": lambda: get_staff_tool_perms_rich(member_id),
            })

            if not data.get("person"):
                PDF_JOBS[job_id] = {"status": "error", "error": "Member not found"}
                return

            owner_track  = sanitize_list(data.get("owner_track")     or [])
            tool_perms   = sanitize_list(data.get("tool_perms_rich") or [])
            system_owned = sanitize_list(data.get("system_owned")    or [])

            for row in owner_track:
                row["ownership_date"] = safe_date(row.get("owned_since"))
                row["removal_date"]   = safe_date(row.get("removed_on"))

            slot_activity = data.get("slot_activity") or {}
            if slot_activity.get("rows"):
                slot_activity["rows"] = sanitize_list(slot_activity["rows"], limit=300)

            html = render_template(
                "profile_pdf.html",
                person             = safe_dict(data["person"]),
                att                = safe_json(data.get("attendance", {})),
                slot_activity      = slot_activity,
                projects           = data.get("projects", {}),
                permissions        = data.get("permissions", []),
                system_owned       = system_owned,
                system_owner_track = owner_track,
                tool_perms_rich    = tool_perms,
                member_id          = member_id,
                now                = datetime.now().strftime("%d %b %Y, %I:%M %p"),
                selected_year      = year,
            )
            pdf = _html_to_pdf(html)

            tmp_dir  = os.path.join(os.getcwd(), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, f"{job_id}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf)

            PDF_JOBS[job_id] = {"status": "done", "file": filepath}

        except Exception as e:
            traceback.print_exc()
            PDF_JOBS[job_id] = {"status": "error", "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# PDF PRE-GENERATION (fired silently on page load)
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/profile/<int:member_id>/pdf/prefetch")
@staff_required
def prefetch_pdf(member_id):
    """
    Called automatically by the profile page JavaScript immediately on load.
    Starts the PDF render job in the background and records the job_id so
    the download button can find it instantly.

    Returns: {"job_id": str, "already_done": bool}

    If a prefetch for this (member_id, year) already exists and is still
    valid (not errored), we reuse it rather than starting a duplicate job.
    """
    year = request.args.get("year", type=int) or date.today().year
    key  = (member_id, year)

    # Check for an existing usable prefetch job
    with _prefetch_lock:
        existing_job_id = PDF_PREFETCH.get(key)
        if existing_job_id:
            existing = PDF_JOBS.get(existing_job_id, {})
            if existing.get("status") in ("processing", "done"):
                return jsonify({
                    "job_id":       existing_job_id,
                    "already_done": existing.get("status") == "done",
                    "reused":       True,
                })

        # Start a fresh job
        job_id = str(uuid.uuid4())
        PDF_JOBS[job_id]   = {"status": "processing"}
        PDF_PREFETCH[key]  = job_id

    t = threading.Thread(
        target  = _generate_pdf_job,
        args    = (current_app._get_current_object(), job_id, member_id, year),
        daemon  = True,
    )
    t.start()

    return jsonify({"job_id": job_id, "already_done": False, "reused": False})


@bp.route("/profile/<int:member_id>/pdf/prefetch-all", methods=["POST"])
@staff_required
def prefetch_pdf_all_years(member_id):
    """
    Bulk prefetch — starts background PDF jobs for every available year.

    Accepts JSON body: {"years": [2021, 2022, 2023, 2024, 2025]}
    Returns: {"jobs": {"2021": "<job_id>", "2022": "<job_id>", ...}}

    Jobs are staggered 200 ms apart so we don't spike WeasyPrint/DB load
    for members with many years of data.  The default year job fires
    immediately (index 0); all other years are delayed.
    """
    body     = request.get_json(silent=True) or {}
    years    = body.get("years", [])
    default  = body.get("default_year", date.today().year)

    if not years:
        return jsonify({"jobs": {}}), 200

    # Sort so the default year is first (rendered immediately, no delay)
    years = sorted(set(int(y) for y in years if y),
                   key=lambda y: (0 if y == default else 1, -y))

    result   = {}
    app_obj  = current_app._get_current_object()

    for idx, year in enumerate(years):
        key = (member_id, year)

        with _prefetch_lock:
            existing_id = PDF_PREFETCH.get(key)
            if existing_id and PDF_JOBS.get(existing_id, {}).get("status") in ("processing", "done"):
                result[str(year)] = existing_id
                continue

            job_id = str(uuid.uuid4())
            PDF_JOBS[job_id]  = {"status": "processing"}
            PDF_PREFETCH[key] = job_id
            result[str(year)] = job_id

        # Stagger: default year fires immediately, others are delayed 250 ms apart
        delay = idx * 0.25  # seconds
        if delay == 0:
            threading.Thread(
                target=_generate_pdf_job,
                args=(app_obj, job_id, member_id, year),
                daemon=True,
            ).start()
        else:
            def _start_delayed(jid=job_id, yr=year, dl=delay):
                import time as _t
                _t.sleep(dl)
                _generate_pdf_job(app_obj, jid, member_id, yr)
            threading.Thread(target=_start_delayed, daemon=True).start()

    return jsonify({"jobs": result})


# ══════════════════════════════════════════════════════════════════════════════
# PDF ON-DEMAND START (fallback if prefetch hasn't run / year changed)
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/profile/<int:member_id>/pdf/start")
@staff_required
def start_pdf(member_id):
    """
    Explicit on-demand PDF start. First checks if a valid prefetch job already
    exists for this (member_id, year) — if so, returns that job_id immediately
    so the user gets the pre-built PDF with zero extra wait.
    """
    year = request.args.get("year", type=int) or date.today().year
    key  = (member_id, year)

    # Reuse prefetch job if available and not errored
    with _prefetch_lock:
        existing_job_id = PDF_PREFETCH.get(key)
        if existing_job_id:
            existing = PDF_JOBS.get(existing_job_id, {})
            if existing.get("status") in ("processing", "done"):
                return jsonify({"job_id": existing_job_id, "prefetched": True})

        # No valid prefetch — start a fresh job
        job_id = str(uuid.uuid4())
        PDF_JOBS[job_id]  = {"status": "processing"}
        PDF_PREFETCH[key] = job_id

    t = threading.Thread(
        target  = _generate_pdf_job,
        args    = (current_app._get_current_object(), job_id, member_id, year),
        daemon  = True,
    )
    t.start()

    return jsonify({"job_id": job_id, "prefetched": False})


# ══════════════════════════════════════════════════════════════════════════════
# PDF STATUS + DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/profile/pdf/status/<job_id>")
def pdf_status(job_id):
    job = PDF_JOBS.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


@bp.route("/profile/pdf/download/<job_id>")
def download_pdf(job_id):
    job = PDF_JOBS.get(job_id)
    if not job or job["status"] != "done":
        return "Not ready", 404
    return send_file(job["file"], as_attachment=True)


@bp.route("/profile/<int:member_id>/pdf")
def profile_pdf(member_id):
    return redirect(url_for("profile.start_pdf", member_id=member_id))


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM OWNER PDF  (pre-gen pattern — same approach)
# ══════════════════════════════════════════════════════════════════════════════

# Prefetch store for owner PDFs: {member_id: job_id}
_OWNER_PREFETCH: dict  = {}
_OTRACK_PREFETCH: dict = {}
_owner_prefetch_lock   = threading.Lock()


def _generate_owner_pdf_job(app, job_id: str, member_id: int):
    with app.app_context():
        try:
            _warmup_uid(member_id)
            results = run_parallel({
                "person": lambda: get_person(member_id),
                "owned":  lambda: get_staff_system_owned(member_id),
            })
            person_rows = results.get("person")
            if not person_rows:
                PDF_JOBS[job_id] = {"status": "error", "error": "Member not found"}
                return

            person = safe_dict(person_rows)
            owned  = results.get("owned") or []
            name   = person.get("display_name") or f"Member {member_id}"

            html = render_template(
                "system_owner_pdf.html",
                person    = person,
                name      = name,
                owned     = owned,
                member_id = member_id,
                now       = datetime.now().strftime("%d %b %Y, %I:%M %p"),
            )
            pdf      = _html_to_pdf(html)
            tmp_dir  = os.path.join(os.getcwd(), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, f"{job_id}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf)
            PDF_JOBS[job_id] = {"status": "done", "file": filepath}

        except Exception as e:
            traceback.print_exc()
            PDF_JOBS[job_id] = {"status": "error", "error": str(e)}


def _generate_owner_track_pdf_job(app, job_id: str, member_id: int):
    with app.app_context():
        try:
            _warmup_uid(member_id)
            results = run_parallel({
                "person":      lambda: get_person(member_id),
                "owner_track": lambda: get_staff_owner_track(member_id),
            })
            person_rows = results.get("person")
            if not person_rows:
                PDF_JOBS[job_id] = {"status": "error", "error": "Member not found"}
                return

            person      = safe_dict(person_rows)
            owner_track = sanitize_list(results.get("owner_track") or [])

            html = render_template(
                "system_owner_track_pdf.html",
                person      = person,
                owner_track = owner_track,
                member_id   = member_id,
                now         = datetime.now().strftime("%d %b %Y, %I:%M %p"),
            )
            pdf      = _html_to_pdf(html)
            tmp_dir  = os.path.join(os.getcwd(), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, f"{job_id}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf)
            PDF_JOBS[job_id] = {"status": "done", "file": filepath}

        except Exception as e:
            traceback.print_exc()
            PDF_JOBS[job_id] = {"status": "error", "error": str(e)}


# ── Prefetch endpoints for owner PDFs ─────────────────────────────────────────

@bp.route("/api/profile/<int:member_id>/system-owner-pdf/prefetch")
@staff_required
def prefetch_owner_pdf(member_id):
    """Pre-generate the system owner PDF silently on page load."""
    with _owner_prefetch_lock:
        existing_id = _OWNER_PREFETCH.get(member_id)
        if existing_id and PDF_JOBS.get(existing_id, {}).get("status") in ("processing", "done"):
            return jsonify({"job_id": existing_id, "reused": True})

        job_id = str(uuid.uuid4())
        PDF_JOBS[job_id]            = {"status": "processing"}
        _OWNER_PREFETCH[member_id]  = job_id

    threading.Thread(
        target=_generate_owner_pdf_job,
        args=(current_app._get_current_object(), job_id, member_id),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "reused": False})


@bp.route("/api/profile/<int:member_id>/system-owner-track-pdf/prefetch")
@staff_required
def prefetch_owner_track_pdf(member_id):
    """Pre-generate the system owner track PDF silently on page load."""
    with _owner_prefetch_lock:
        existing_id = _OTRACK_PREFETCH.get(member_id)
        if existing_id and PDF_JOBS.get(existing_id, {}).get("status") in ("processing", "done"):
            return jsonify({"job_id": existing_id, "reused": True})

        job_id = str(uuid.uuid4())
        PDF_JOBS[job_id]              = {"status": "processing"}
        _OTRACK_PREFETCH[member_id]   = job_id

    threading.Thread(
        target=_generate_owner_track_pdf_job,
        args=(current_app._get_current_object(), job_id, member_id),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "reused": False})


# ── On-demand start endpoints (reuse prefetch if available) ───────────────────

@bp.route("/api/profile/<int:member_id>/system-owner-pdf/start")
@staff_required
def start_system_owner_pdf(member_id):
    with _owner_prefetch_lock:
        existing_id = _OWNER_PREFETCH.get(member_id)
        if existing_id and PDF_JOBS.get(existing_id, {}).get("status") in ("processing", "done"):
            return jsonify({"job_id": existing_id, "prefetched": True})

        job_id = str(uuid.uuid4())
        PDF_JOBS[job_id]           = {"status": "processing"}
        _OWNER_PREFETCH[member_id] = job_id

    threading.Thread(
        target=_generate_owner_pdf_job,
        args=(current_app._get_current_object(), job_id, member_id),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "prefetched": False})


@bp.route("/api/profile/<int:member_id>/system-owner-track-pdf/start")
@staff_required
def start_system_owner_track_pdf(member_id):
    with _owner_prefetch_lock:
        existing_id = _OTRACK_PREFETCH.get(member_id)
        if existing_id and PDF_JOBS.get(existing_id, {}).get("status") in ("processing", "done"):
            return jsonify({"job_id": existing_id, "prefetched": True})

        job_id = str(uuid.uuid4())
        PDF_JOBS[job_id]              = {"status": "processing"}
        _OTRACK_PREFETCH[member_id]   = job_id

    threading.Thread(
        target=_generate_owner_track_pdf_job,
        args=(current_app._get_current_object(), job_id, member_id),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "prefetched": False})


# ── Legacy redirect URLs ───────────────────────────────────────────────────────

@bp.route("/api/profile/<int:member_id>/system-owner-pdf")
@staff_required
def system_owner_pdf(member_id):
    return redirect(url_for("profile.start_system_owner_pdf", member_id=member_id))


@bp.route("/api/profile/<int:member_id>/system-owner-track-pdf")
@staff_required
def system_owner_track_pdf(member_id):
    return redirect(url_for("profile.start_system_owner_track_pdf", member_id=member_id))


# ══════════════════════════════════════════════════════════════════════════════
# UTIL
# ══════════════════════════════════════════════════════════════════════════════

def extract_year(name):
    m = re.search(r'\d{4}', name)
    return int(m.group()) if m else 0
