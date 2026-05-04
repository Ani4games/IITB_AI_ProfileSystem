"""
routes/lab_profile.py — /lab/<memberid>, /lab/<memberid>/pdf

PDF Pre-generation
──────────────────
Same pattern as profile.py: PDF rendering begins in the background the
moment the page loads, not when the user clicks download. The page fires
/lab/<id>/pdf/prefetch silently on load; when the user clicks the PDF
button the job is usually already done.

Performance notes
─────────────────
1. PDF table data is capped at 200 rows (reservations) / 300 rows (equipment
   requests) to keep render time predictable.
2. WeasyPrint is pre-warmed at app startup.
3. Lab profile page PDF pre-generation is keyed by (memberid, year).
"""

import io
import os
import uuid
import threading
import traceback
from datetime import date, datetime
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, make_response, jsonify, send_file, current_app
)
from auth import login_required, is_full_access
from utils import run_parallel, safe_dict
from models.lab import (
    get_lab_errors, get_lab_user, get_lab_stats, get_lab_reservations,
    get_lab_equipment_requests, get_lab_access_log,
    get_lab_cancellations,
    _get_lab_projects,
    get_lab_registration, get_session_reports,
    is_faculty, get_member_tool_permissions,
    get_system_owner_tools, get_system_owner_track,
    safe_json,
)
from models.staff import get_available_years


# ── PDF job stores ────────────────────────────────────────────────────────────
LAB_PDF_JOBS: dict     = {}
LAB_PDF_PREFETCH: dict = {}   # {(memberid, year): job_id}
_lab_prefetch_lock     = threading.Lock()


# ── PDF renderer ──────────────────────────────────────────────────────────────
def _html_to_pdf(html_string: str) -> bytes:
    """xhtml2pdf (pisa) — 5-10x faster than WeasyPrint for table/float layouts."""
    import io
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    result = pisa.CreatePDF(src=html_string, dest=buf, encoding="utf-8")
    if result.err:
        raise ValueError(f"xhtml2pdf error: {result.err}")
    return buf.getvalue()


bp = Blueprint("lab_profile", __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitize_list(data, limit: int | None = None):
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
# LAB PROFILE PAGE
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/lab/<int:memberid>")
@login_required
def lab_profile(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        flash("You can only view your own lab profile.", "error")
        return redirect(url_for("lab_profile.lab_profile", memberid=session["memberid"]))
    if is_faculty(memberid):
        flash("This member is faculty and has a staff profile instead.", "info")

    avail_data = get_available_years(memberid=memberid)
    avail_years, best_year = avail_data if avail_data else ([date.today().year], date.today().year)
    year = request.args.get("year", type=int) or best_year

    data = run_parallel({
        "user":            lambda: get_lab_user(memberid),
        "stats":           lambda: get_lab_stats(memberid),
        "reservations":    lambda: get_lab_reservations(memberid, year),
        "requests":        lambda: get_lab_equipment_requests(memberid, year),
        "lab_access":      lambda: get_lab_access_log(memberid, year),
        "projects":        lambda: _get_lab_projects(memberid),
        "cancellations":   lambda: get_lab_cancellations(memberid),
        "errors":          lambda: get_lab_errors(memberid) if is_full_access() else [],
        "reg":             lambda: get_lab_registration(memberid),
        "session_reports": lambda: get_session_reports(memberid),
        "tool_perms_rich": lambda: get_member_tool_permissions(memberid),
        "system_owned":    lambda: get_system_owner_tools(memberid),
        "owner_track":     lambda: get_system_owner_track(memberid),
    })

    if not data.get("user"):
        return render_template("not_found.html", member_id=memberid), 404

    user_safe = safe_dict(data["user"])

    return render_template(
        "lab_profile.html",
        user             = user_safe,
        stats            = data.get("stats", {}),
        reservations     = data.get("reservations", []),
        requests         = data.get("requests", []),
        lab_access       = data.get("lab_access", []),
        tool_perms       = data.get("tool_perms", []),
        projects         = data.get("projects", {}),
        selected_year    = year,
        avail_years      = avail_years,
        memberid         = memberid,
        full_access      = is_full_access(),
        cancellations    = data.get("cancellations") or [],
        errors           = data.get("errors") or [],
        reg              = data.get("reg"),
        session_reports  = data.get("session_reports") or [],
        tool_perms_rich  = data.get("tool_perms_rich") or [],
        system_owned     = data.get("system_owned") or [],
        owner_track      = data.get("owner_track") or [],
    )


# ══════════════════════════════════════════════════════════════════════════════
# CORE PDF GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _generate_lab_pdf_job(app, job_id: str, memberid: int, year: int):
    """
    Background thread: renders the lab PDF and stores the result in LAB_PDF_JOBS.
    Called by both the prefetch endpoint and the on-demand start endpoint.
    """
    with app.app_context():
        try:
            data = run_parallel({
                "user":            lambda: get_lab_user(memberid),
                "stats":           lambda: get_lab_stats(memberid),
                "reservations":    lambda: get_lab_reservations(memberid, year),
                "requests":        lambda: get_lab_equipment_requests(memberid, year),
                "lab_access":      lambda: get_lab_access_log(memberid, year),
                "projects":        lambda: _get_lab_projects(memberid),
                "cancellations":   lambda: get_lab_cancellations(memberid),
                "reg":             lambda: get_lab_registration(memberid),
                "session_reports": lambda: get_session_reports(memberid),
                "tool_perms_rich": lambda: get_member_tool_permissions(memberid),
                "system_owned":    lambda: get_system_owner_tools(memberid),
                "owner_track":     lambda: get_system_owner_track(memberid),
            })

            if not data.get("user"):
                LAB_PDF_JOBS[job_id] = {"status": "error", "error": "User not found"}
                return

            reservations    = _sanitize_list(data.get("reservations")    or [], limit=200)
            requests        = _sanitize_list(data.get("requests")        or [], limit=300)
            lab_access      = _sanitize_list(data.get("lab_access")      or [], limit=100)
            cancellations   = _sanitize_list(data.get("cancellations")   or [], limit=100)
            session_reports = _sanitize_list(data.get("session_reports") or [], limit=100)
            tool_perms_rich = _sanitize_list(data.get("tool_perms_rich") or [])
            system_owned    = _sanitize_list(data.get("system_owned")    or [])
            owner_track     = _sanitize_list(data.get("owner_track")     or [])

            for row in owner_track:
                if "owned_since" in row and "ownership_date" not in row:
                    row["ownership_date"] = row.get("owned_since")
                if "removed_on" in row and "removal_date" not in row:
                    row["removal_date"] = row.get("removed_on")

            user     = safe_dict(data["user"])
            stats    = data.get("stats")    or {}
            projects = data.get("projects") or {}
            reg      = data.get("reg")

            html = render_template(
                "lab_profile_pdf.html",
                user            = user,
                stats           = stats,
                reservations    = reservations,
                requests        = requests,
                lab_access      = lab_access,
                cancellations   = cancellations,
                session_reports = session_reports,
                tool_perms_rich = tool_perms_rich,
                system_owned    = system_owned,
                owner_track     = owner_track,
                projects        = projects,
                reg             = reg,
                memberid        = memberid,
                selected_year   = year,
                now             = datetime.now().strftime("%d %b %Y, %I:%M %p"),
            )
            pdf = _html_to_pdf(html)

            tmp_dir  = os.path.join(os.getcwd(), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, f"{job_id}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf)

            LAB_PDF_JOBS[job_id] = {"status": "done", "file": filepath}

        except Exception as e:
            traceback.print_exc()
            LAB_PDF_JOBS[job_id] = {"status": "error", "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# PDF PRE-GENERATION (fired silently on page load)
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/lab/<int:memberid>/pdf/prefetch")
@login_required
def prefetch_lab_pdf(memberid):
    """
    Called automatically by the lab profile page JS immediately on load.
    Starts the PDF render job in the background and returns a job_id.
    """
    if not is_full_access() and session.get("memberid") != memberid:
        return jsonify({"error": "Access restricted"}), 403

    year = request.args.get("year", type=int) or date.today().year
    key  = (memberid, year)

    with _lab_prefetch_lock:
        existing_id = LAB_PDF_PREFETCH.get(key)
        if existing_id:
            existing = LAB_PDF_JOBS.get(existing_id, {})
            if existing.get("status") in ("processing", "done"):
                return jsonify({
                    "job_id":       existing_id,
                    "already_done": existing.get("status") == "done",
                    "reused":       True,
                })

        job_id = str(uuid.uuid4())
        LAB_PDF_JOBS[job_id]   = {"status": "processing"}
        LAB_PDF_PREFETCH[key]  = job_id

    threading.Thread(
        target  = _generate_lab_pdf_job,
        args    = (current_app._get_current_object(), job_id, memberid, year),
        daemon  = True,
    ).start()

    return jsonify({"job_id": job_id, "already_done": False, "reused": False})


# ══════════════════════════════════════════════════════════════════════════════
# PDF ON-DEMAND START (fallback if prefetch hasn't run / year changed)
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/lab/<int:memberid>/pdf/start")
@login_required
def start_lab_pdf(memberid):
    """
    Explicit on-demand start. Reuses prefetch job if available.
    """
    if not is_full_access() and session.get("memberid") != memberid:
        return jsonify({"error": "Access restricted"}), 403

    year = request.args.get("year", type=int) or date.today().year
    key  = (memberid, year)

    with _lab_prefetch_lock:
        existing_id = LAB_PDF_PREFETCH.get(key)
        if existing_id:
            existing = LAB_PDF_JOBS.get(existing_id, {})
            if existing.get("status") in ("processing", "done"):
                return jsonify({"job_id": existing_id, "prefetched": True})

        job_id = str(uuid.uuid4())
        LAB_PDF_JOBS[job_id]  = {"status": "processing"}
        LAB_PDF_PREFETCH[key] = job_id

    threading.Thread(
        target  = _generate_lab_pdf_job,
        args    = (current_app._get_current_object(), job_id, memberid, year),
        daemon  = True,
    ).start()

    return jsonify({"job_id": job_id, "prefetched": False})


# ══════════════════════════════════════════════════════════════════════════════
# PDF STATUS + DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/lab/pdf/status/<job_id>")
@login_required
def lab_pdf_status(job_id):
    job = LAB_PDF_JOBS.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


@bp.route("/lab/pdf/download/<job_id>")
@login_required
def lab_pdf_download(job_id):
    job = LAB_PDF_JOBS.get(job_id)
    if not job or job["status"] != "done":
        return "Not ready", 404
    return send_file(job["file"], as_attachment=True)


# ── Legacy synchronous endpoint (kept for backwards compat) ───────────────────
@bp.route("/lab/<int:memberid>/pdf")
@login_required
def lab_profile_pdf(memberid):
    """
    Redirect to the async start endpoint.
    Old bookmarked URLs continue to work.
    """
    if not is_full_access() and session.get("memberid") != memberid:
        return "Access restricted.", 403
    year = request.args.get("year", type=int) or date.today().year
    return redirect(url_for("lab_profile.start_lab_pdf", memberid=memberid, year=year))
