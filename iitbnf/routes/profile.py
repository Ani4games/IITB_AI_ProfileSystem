"""
routes/profile.py — /profile/<id>, /profile/<id>/pdf,
                    /api/profile/<id>/system-owner-pdf,
                    /api/profile/<id>/system-owner-track-pdf

Performance fixes applied
─────────────────────────
1. _warmup_uid() called before run_parallel() in the main profile route.
   The UID resolution (_get_uid_from_member) can run up to 4 sequential DB
   queries on a cache miss.  Without the warmup, the 4 parallel tasks that
   each need the slotbooking uid (slot_activity, system_owned, owner_track,
   tool_perms) all trigger the resolver concurrently — each gets a cache
   miss, and all 4 run the 4-step fallback chain simultaneously, producing
   up to 16 extra DB calls on the first visit.  With _warmup_uid() the
   resolution runs once before the fan-out; all 4 parallel tasks then find
   the result in the in-process cache instantly.

2. Duplicate /api/section/staff/<id>/slot_activity route removed.
   The identical endpoint now lives solely in section_routes.py — having it
   registered in two blueprints was causing Flask to emit a warning on every
   startup and occasionally route requests to the wrong handler.

3. PDF row limits added — slot_activity rows capped at 300 for PDF rendering.

4. system_owner_pdf and system_owner_track_pdf use run_parallel to fetch
   person + owned/track concurrently instead of sequentially.

5. PDF rendering uses WeasyPrint for full CSS support and layout flexibility.
   All PDF templates use inline system fonts (Arial/Helvetica) with no
   external font references, so WeasyPrint renders without any network calls.
"""
import os
import uuid
import threading
import time
import re
import traceback
from datetime import date, datetime
from flask import Blueprint, render_template, request, make_response, jsonify, send_file, url_for, redirect,current_app
from auth import staff_required, is_full_access
from models.lab import safe_json
from utils import run_parallel, safe_dict
PDF_JOBS ={}

def _html_to_pdf(html_string: str) -> bytes:
    """
    Convert an HTML string to PDF bytes using WeasyPrint.

    All PDF templates in this project use only inline system fonts
    (Arial / Helvetica) with no external <link> or @import references,
    so WeasyPrint renders entirely offline with zero network calls.

    WeasyPrint offers significantly better CSS support than xhtml2pdf —
    proper flexbox, grid, border-radius, box-shadow, and modern layout
    primitives all render correctly, giving much more design flexibility
    in the PDF templates.
    """
    from weasyprint import HTML, CSS
    from weasyprint.text.fonts import FontConfiguration

    font_config = FontConfiguration()
    pdf_bytes = HTML(
        string   = html_string,
        base_url = None,          # no base URL — templates have no relative assets
    ).write_pdf(
        font_config = font_config,
    )
    if pdf_bytes is None:
        raise ValueError("Failed to generate PDF")
    return pdf_bytes


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


# ========================= HELPERS =========================

def safe_date(val):
    """Convert any date/datetime to string (PDF safe)."""
    if not val:
        return None
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    try:
        return str(val)
    except Exception:
        return None


def sanitize_list(data, limit: int | None = None):
    """Ensure list of dicts is fully JSON + PDF safe. Optional row cap."""
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


# ========================= NORMAL PROFILE =========================

@bp.route("/profile/<int:member_id>")
@staff_required
def profile(member_id):

    start_total = time.time()
    year_req    = request.args.get("year", type=int)
    full_access = is_full_access()

    # Pre-populate the slotbooking UID cache BEFORE the parallel fan-out.
    # This prevents the 4 parallel tasks that each need the uid from all
    # triggering the expensive 4-step resolution concurrently on first visit.
    _warmup_uid(member_id)

    data = run_parallel({
        "person":          lambda: get_person(member_id),
        "avail_years":     lambda: get_available_years(member_id=member_id),
        "attendance":      lambda: get_attendance_stats(member_id, year_req or date.today().year),
        "trend":           lambda: get_attendance_trend(member_id, year_req or date.today().year),
    })

    if not data.get("person"):
        return render_template("not_found.html", member_id=member_id), 404

    avail_years, best_year = data.get("avail_years") or ([date.today().year], date.today().year)

    html = render_template(
        "profile.html",
        person           = safe_dict(data["person"]),
        att              = safe_json(data.get("attendance", {})),
        trend            = data.get("trend", []),
        full_access      = full_access,
        selected_year    = best_year,
        avail_years      = avail_years,
        member_id        = member_id,
    )

    response = make_response(html)
    response.headers["X-Total-Time"] = f"{round((time.time()-start_total)*1000,2)}ms"
    return response


# ========================= PDF PROFILE =========================
def _generate_pdf_job(app, job_id, member_id, year):
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
                return render_template("not_found.html", member_id=member_id), 404

            owner_track  = sanitize_list(data.get("owner_track")     or [])
            tool_perms   = sanitize_list(data.get("tool_perms_rich") or [])
            system_owned = sanitize_list(data.get("system_owned")    or [])

            # Normalise ownership date fields so the template uses a single key
            for row in owner_track:
                row["ownership_date"] = safe_date(row.get("owned_since"))
                row["removal_date"]   = safe_date(row.get("removed_on"))

            # Normalise slot_activity rows (date objects → strings)
            # Cap at 300 rows to keep PDF rendering fast
            slot_activity = data.get("slot_activity") or {}
            if slot_activity.get("rows"):
                slot_activity["rows"] = sanitize_list(slot_activity["rows"], limit=300)

            html = render_template("profile_pdf.html", 
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
                selected_year      = year,)
            pdf  = _html_to_pdf(html)

            tmp_dir = os.path.join(os.getcwd(), "tmp")
            os.makedirs(tmp_dir, exist_ok=True)

            filepath = os.path.join(tmp_dir, f"{job_id}.pdf")
            with open(filepath, "wb") as f:
                f.write(pdf)
            PDF_JOBS[job_id] = {"status": "done", "file": filepath}

        except Exception as e:
            traceback.print_exc()
            PDF_JOBS[job_id] = {"status": "error", "error": str(e)}

@bp.route("/profile/<int:member_id>/pdf/start")
@staff_required
def start_pdf(member_id):
    year = request.args.get("year", type=int) or date.today().year

    job_id = str(uuid.uuid4())
    PDF_JOBS[job_id] = {"status": "processing"}

    thread = threading.Thread(
        target=_generate_pdf_job,
        args=(current_app._get_current_object(), job_id, member_id, year)  # type: ignore
    )
    thread.start()

    return jsonify({"job_id": job_id})
@bp.route("/profile/pdf/download/<job_id>")
def download_pdf(job_id):
    job = PDF_JOBS.get(job_id)

    if not job or job["status"] != "done":
        return "Not ready", 404

    return send_file(job["file"], as_attachment=True)
@bp.route("/profile/pdf/status/<job_id>")
def pdf_status(job_id):
    job = PDF_JOBS.get(job_id)

    if not job:
        return jsonify({"status": "not_found"}), 404

    return jsonify(job)
@bp.route("/profile/<int:member_id>/pdf")
def profile_pdf(member_id):
    return redirect(url_for("profile.start_pdf", member_id=member_id))

# ========================= SYSTEM OWNER PDF =========================

def _generate_owner_pdf_job(app, job_id, member_id):
    """Background thread: renders system-owner PDF and stores result in PDF_JOBS."""
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


@bp.route("/api/profile/<int:member_id>/system-owner-pdf/start")
@staff_required
def start_system_owner_pdf(member_id):
    """Start async system-owner PDF generation. Returns {job_id}."""
    job_id = str(uuid.uuid4())
    PDF_JOBS[job_id] = {"status": "processing"}
    t = threading.Thread(
        target=_generate_owner_pdf_job,
        args=(current_app._get_current_object(), job_id, member_id),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


# Keep old synchronous URL as redirect so existing links don't break
@bp.route("/api/profile/<int:member_id>/system-owner-pdf")
@staff_required
def system_owner_pdf(member_id):
    """Redirect to the async start endpoint (avoids blocking the worker)."""
    return redirect(url_for("profile.start_system_owner_pdf", member_id=member_id))


# ========================= SYSTEM OWNER TRACK PDF =========================

def _generate_owner_track_pdf_job(app, job_id, member_id):
    """Background thread: renders system-owner-track PDF."""
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


@bp.route("/api/profile/<int:member_id>/system-owner-track-pdf/start")
@staff_required
def start_system_owner_track_pdf(member_id):
    """Start async system-owner-track PDF generation. Returns {job_id}."""
    job_id = str(uuid.uuid4())
    PDF_JOBS[job_id] = {"status": "processing"}
    t = threading.Thread(
        target=_generate_owner_track_pdf_job,
        args=(current_app._get_current_object(), job_id, member_id),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


# Keep old synchronous URL as redirect so existing links don't break
@bp.route("/api/profile/<int:member_id>/system-owner-track-pdf")
@staff_required
def system_owner_track_pdf(member_id):
    """Redirect to the async start endpoint (avoids blocking the worker)."""
    return redirect(url_for("profile.start_system_owner_track_pdf", member_id=member_id))


# ========================= UTIL =========================

def extract_year(name):
    m = re.search(r'\d{4}', name)
    return int(m.group()) if m else 0
