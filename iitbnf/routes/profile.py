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
import threading
_xhtml2pdf_ready = threading.Event()
# ── Job stores ────────────────────────────────────────────────────────────────
# PDF_JOBS  : {job_id: {"status": "processing"|"done"|"error", "file": path}}
# PDF_PREFETCH: {(member_id, year): job_id}  — maps a profile+year → prefetch job
PDF_JOBS: dict     = {}
PDF_PREFETCH: dict = {}

# Lock for PDF_PREFETCH to avoid duplicate concurrent pre-gen for same profile
_prefetch_lock = threading.Lock()

# In profile.py — ADD at module level:
_xhtml2pdf_init_lock = threading.Lock()
_xhtml2pdf_initialized = False

# CHANGE _html_to_pdf:
# REPLACE the entire _html_to_pdf function with:
def _html_to_pdf(html_string: str) -> bytes:
    import io
    from xhtml2pdf import pisa
    _xhtml2pdf_ready.wait(timeout=120)
    def link_callback(uri, rel):
        if uri.startswith("file://"):
            return uri
        return None

    buf = io.BytesIO()
    result = pisa.CreatePDF(
        src=html_string,
        dest=buf,
        encoding="utf-8",
        link_callback=link_callback,
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
    get_staff_tool_perms_rich, get_staff_logbook_stats, 
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
    INT_FIELDS = {"status", "approval", "is_admin", "isworking", "is_active",
                  "activation_status", "isblackout"}
    out = []
    for row in rows:
        clean = {}
        for k, v in row.items():
            if isinstance(v, (datetime, date)):
                clean[k] = v.strftime("%Y-%m-%d")
            elif k in INT_FIELDS:
                try:
                    clean[k] = int(v) if v is not None else 0
                except (TypeError, ValueError):
                    clean[k] = 0
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
    })

    if not data.get("person"):
        return render_template("not_found.html", member_id=member_id), 404

    avail_years, best_year = data.get("avail_years") or ([date.today().year], date.today().year)

    html = render_template(
        "staff_sections.html",
        person        = safe_dict(data["person"]),
        att           = {},
        trend         = [],
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
    with app.app_context():
        try:
            # Small delay — let the page load queries finish first
            # so we don't compete for DB connections
            import time
            time.sleep(2)

            _warmup_uid(member_id)
            from db import slots_query as _sq
            from models.staff import _get_uid_from_member

            uid = _get_uid_from_member(member_id)

            def _owned_counts():
                if not uid:
                    return {"total": 0, "working": 0}
                rows = _sq(
                    "SELECT machid FROM system_owner WHERE memberid=%s", (uid,)
                ) or []
                all_ids = []
                for r in rows:
                    raw = str(r.get("machid") or "")
                    all_ids += [x for x in raw.split(",") if x.strip().isdigit()]
                if not all_ids:
                    return {"total": 0, "working": 0}
                ph = ",".join(["%s"] * len(all_ids))
                working = _sq(
                    f"SELECT COUNT(*) AS cnt FROM resources "
                    f"WHERE machid IN ({ph}) AND isworking=1", tuple(all_ids)
                )
                return {
                    "total":   len(all_ids),
                    "working": int((working or [{}])[0].get("cnt") or 0),
                }

            def _track_counts():
                if not uid:
                    return {"total": 0, "active": 0}
                rows = _sq("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN action='create' THEN 1 ELSE 0 END) -
                        SUM(CASE WHEN action='delete' THEN 1 ELSE 0 END) AS active
                    FROM system_owner_track WHERE memberid=%s
                """, (uid,)) or [{}]
                total  = int(rows[0].get("total") or 0)
                active = max(0, int(rows[0].get("active") or 0))
                return {"total": total, "active": active}

            # Run sequentially, not in parallel — avoids saturating
            # the DB pool while page queries are still running
            print(f"[PDF] Starting DB queries for member {member_id}")
            t0 = time.perf_counter()

            person     = get_person(member_id)
            attendance = get_attendance_stats(member_id, year)
            slot_act   = get_slot_activity(member_id, year)
            perms      = get_permissions(member_id)
            tool_perms = get_staff_tool_perms_rich(member_id)
            owned_c    = _owned_counts()
            track_c    = _track_counts()
            logbook_stats = get_staff_logbook_stats(member_id)

            print(f"[PDF] DB queries done in {round((time.perf_counter()-t0)*1000)}ms")

            if not person:
                PDF_JOBS[job_id] = {"status": "error", "error": "Member not found"}
                return

            tool_perms_list = sanitize_list(tool_perms or [], limit=30)  # limit for PDF readability
            perms_safe = (perms or [])[:50]  # cap permissions list too
            raw_slot = slot_act or {}
            slot_activity = {k: v for k, v in raw_slot.items() if k != "rows"}
            slot_activity["rows"] = []

            system_owned = (
                [{"isworking": 1}] * owned_c["working"] +
                [{"isworking": 0}] * (owned_c["total"] - owned_c["working"])
            )
            system_owner_track = (
                [{"is_active": True}]  * track_c["active"] +
                [{"is_active": False}] * (track_c["total"] - track_c["active"])
            )

            html = render_template(
                "profile_pdf.html",
                person             = safe_dict(person),
                att                = safe_json(attendance or {}),
                slot_activity      = slot_activity,
                logbook_entries    = logbook_stats or {},
                permissions        = perms_safe or [],
                system_owned       = system_owned,
                system_owner_track = system_owner_track,
                tool_perms_rich    = tool_perms_list,
                member_id          = member_id,
                now                = datetime.now().strftime("%d %b %Y, %I:%M %p"),
                selected_year      = year,
            )

            print(f"[PDF] Calling _html_to_pdf...")
            t0  = time.perf_counter()
            pdf = _html_to_pdf(html)
            print(f"[PDF] _html_to_pdf done in {round((time.perf_counter()-t0)*1000)}ms")

            tmp_dir  = os.path.join(os.getcwd(), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, f"{job_id}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf)

            PDF_JOBS[job_id] = {"status": "done", "file": filepath}
            print(f"[PDF] Job {job_id[:8]} complete")

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
        import time as _t
        PDF_JOBS[job_id]   = {"status": "processing", "created_at": _t.time()}
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
    Prefetch — starts a background PDF job for the default year only.
    Other years render on-demand when the user selects them.
    This avoids launching N simultaneous jobs that saturate the DB pool
    and cause the default year to take 1.5+ minutes on members with many years.

    Accepts JSON body: {"years": [2021, 2022, 2023, 2024, 2025], "default_year": 2025}
    Returns: {"jobs": {"2025": "<job_id>"}}
    """
    body    = request.get_json(silent=True) or {}
    years   = body.get("years", [])
    default = body.get("default_year", date.today().year)

    if not years:
        return jsonify({"jobs": {}}), 200

    # Only prefetch the default year — on-demand for others
    result  = {}
    app_obj = current_app._get_current_object()

    try:
        default = int(default)
    except (TypeError, ValueError):
        default = date.today().year

    key = (member_id, default)
    with _prefetch_lock:
        existing_id = PDF_PREFETCH.get(key)
        if existing_id and PDF_JOBS.get(existing_id, {}).get("status") in ("processing", "done"):
            return jsonify({"jobs": {str(default): existing_id}})

        job_id = str(uuid.uuid4())
        PDF_JOBS[job_id]    = {"status": "processing", "created_at": time.time()}
        PDF_PREFETCH[key]   = job_id
        result[str(default)] = job_id

    threading.Thread(
        target=_generate_pdf_job,
        args=(app_obj, job_id, member_id, default),
        daemon=True,
    ).start()

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
        PDF_JOBS[job_id]  = {"status": "processing", "created_at": time.time()}
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
    inline = request.args.get("inline") == "1"
    return send_file(job["file"], as_attachment=not inline)


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
        time.sleep(4)   # stagger after main profile PDF (which sleeps 2s)
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
        time.sleep(6)   # stagger further after main profile PDF + owner PDF
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