"""
routes/lab_profile.py — /lab/<memberid>, /lab/<memberid>/pdf

Performance fixes applied
─────────────────────────
1. PDF rendering now uses WeasyPrint for full CSS support and layout
   flexibility. All PDF templates use only inline system fonts
   (Arial/Helvetica) with no external font references, so WeasyPrint
   renders entirely offline with zero network calls.

2. The lab_profile_pdf.html template uses only inline Arial/sans-serif,
   so no font quality is lost and full CSS layout features are available.

3. PDF table data is limited to 200 rows for reservations and 300 rows
   for equipment requests — rendering very large tables is slow regardless
   of which library is used.
"""

import io
import traceback
from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, make_response
from auth import login_required, is_full_access
from utils import run_parallel, safe_dict
from models.lab import (get_lab_errors, get_lab_user, get_lab_stats, get_lab_reservations,
                         get_lab_equipment_requests, get_lab_access_log,
                        get_lab_cancellations,
                         _get_lab_projects,
                         get_lab_registration, get_session_reports,
                         is_faculty, get_member_tool_permissions,
                         get_system_owner_tools, get_system_owner_track,
                         safe_json)
from models.staff import get_available_years


# ── PDF renderer ─────────────────────────────────────────────────────────────
def _html_to_pdf(html_string: str) -> bytes:
    """
    Convert HTML to PDF bytes using WeasyPrint.

    WeasyPrint offers full modern CSS support — flexbox, grid, border-radius,
    box-shadow — giving much greater layout flexibility than xhtml2pdf.

    All PDF templates in this project use only inline system fonts
    (Arial / Helvetica) with no external <link> or @import references,
    so WeasyPrint renders entirely offline with zero network calls and
    no font-fetch hangs.
    """
    from weasyprint import HTML
    from weasyprint.text.fonts import FontConfiguration

    font_config = FontConfiguration()
    return HTML(
        string   = html_string,
        base_url = None,
    ).write_pdf(
        font_config = font_config,
    )


bp = Blueprint("lab_profile", __name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _sanitize_list(data, limit: int | None = None):
    """
    Convert date/datetime values in a list of dicts to ISO strings.
    Optional `limit` caps the list length before serialising to keep PDF
    rendering fast when a user has thousands of rows.
    """
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


# ========================= LAB PROFILE =========================

@bp.route("/lab/<int:memberid>")
@login_required
def lab_profile(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        flash("You can only view your own lab profile.", "error")
        return redirect(url_for("lab_profile.lab_profile", memberid=session["memberid"]))
    # Block faculty profiles — they belong to the staff profile system
    if is_faculty(memberid):
        flash("This member is faculty and has a staff profile instead.", "info")


    data = run_parallel({
        "user":             lambda: get_lab_user(memberid),
        "avail_years":     lambda: get_available_years(memberid=memberid),
        "stats":            lambda: get_lab_stats(memberid),
        "reservations":     lambda: get_lab_reservations(memberid, year),
        "requests":         lambda: get_lab_equipment_requests(memberid, year),
        "lab_access":       lambda: get_lab_access_log(memberid, year),
        "projects":         lambda: _get_lab_projects(memberid),
        "cancellations":    lambda: get_lab_cancellations(memberid),
        "errors":           lambda: get_lab_errors(memberid) if is_full_access() else [],
        "reg":              lambda: get_lab_registration(memberid),
        "session_reports":  lambda: get_session_reports(memberid),
        "tool_perms_rich":  lambda: get_member_tool_permissions(memberid),
        "system_owned":     lambda: get_system_owner_tools(memberid),
        "owner_track":      lambda: get_system_owner_track(memberid),
    })
    avail_years, best_year = data.get("avail_years") or ([date.today().year], date.today().year)
    year = request.args.get("year", type=int) or best_year
    
    if not data.get("user"):
        return render_template("not_found.html", member_id=memberid), 404

    user     = data["user"]
    stats    = data.get("stats",    {})
    projects = data.get("projects", {})

    user_safe = safe_dict(user)

    return render_template("lab_profile.html",
        user=user_safe, stats=stats,
        reservations=data.get("reservations", []),
        requests=data.get("requests",     []),
        lab_access=data.get("lab_access", []),
        tool_perms=data.get("tool_perms", []),
        projects=projects,
        selected_year=year,
        avail_years=avail_years,
        memberid=memberid,
        full_access=is_full_access(),
        cancellations=data.get("cancellations") or [],
        errors=data.get("errors") or [],
        reg=data.get("reg"),
        session_reports=data.get("session_reports") or [],
        tool_perms_rich=data.get("tool_perms_rich") or [],
        system_owned=data.get("system_owned") or [],
        owner_track=data.get("owner_track") or [],
    )


# ========================= LAB PDF =========================

@bp.route("/lab/<int:memberid>/pdf")
@login_required
def lab_profile_pdf(memberid):
    """Generate a PDF report for a lab user profile."""

    try:
        if not is_full_access() and session.get("memberid") != memberid:
            return "Access restricted.", 403

        year = request.args.get("year", type=int) or date.today().year

        data = run_parallel({
            "user":             lambda: get_lab_user(memberid),
            "stats":            lambda: get_lab_stats(memberid),
            "reservations":     lambda: get_lab_reservations(memberid, year),
            "requests":         lambda: get_lab_equipment_requests(memberid, year),
            "lab_access":       lambda: get_lab_access_log(memberid, year),
            "projects":         lambda: _get_lab_projects(memberid),
            "cancellations":    lambda: get_lab_cancellations(memberid),
            "reg":              lambda: get_lab_registration(memberid),
            "session_reports":  lambda: get_session_reports(memberid),
            "tool_perms_rich":  lambda: get_member_tool_permissions(memberid),
            "system_owned":     lambda: get_system_owner_tools(memberid),
            "owner_track":      lambda: get_system_owner_track(memberid),
        })

        if not data.get("user"):
            return render_template("not_found.html", member_id=memberid), 404

        # Sanitise all list data.
        # Row limits keep PDF rendering fast — xhtml2pdf slows down linearly
        # with the number of table rows it has to lay out.
        reservations    = _sanitize_list(data.get("reservations")    or [], limit=200)
        requests        = _sanitize_list(data.get("requests")        or [], limit=300)
        lab_access      = _sanitize_list(data.get("lab_access")      or [], limit=100)
        cancellations   = _sanitize_list(data.get("cancellations")   or [], limit=100)
        session_reports = _sanitize_list(data.get("session_reports") or [], limit=100)
        tool_perms_rich = _sanitize_list(data.get("tool_perms_rich") or [])
        system_owned    = _sanitize_list(data.get("system_owned")    or [])
        owner_track     = _sanitize_list(data.get("owner_track")     or [])

        # Normalise ownership date fields to a single consistent key name
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

        response = make_response(pdf)
        response.headers["Content-Type"]        = "application/pdf"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="IITBNF_Lab_{str(memberid).zfill(4)}_{year}.pdf"'
        )
        return response

    except Exception as e:
        traceback.print_exc()
        return f"PDF generation failed: {str(e)}", 500
