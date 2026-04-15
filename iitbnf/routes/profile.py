"""
routes/profile.py — /profile/<id> and /profile/<id>/pdf
"""

import time
import re
from datetime import date, datetime
from flask import Blueprint, render_template, request, make_response
from auth import staff_required, is_full_access
from models.lab import safe_json
from utils import run_parallel, safe_dict

from models.staff import (
    get_person, get_attendance_stats, get_equipment_stats,
    get_project_data, get_monthly_reports, get_committee_involvement,
    get_permissions, get_profile_tracking,
    get_attendance_trend, get_available_years,
    get_staff_system_owned, get_staff_owner_track,
    get_staff_tool_perms_rich, get_staff_reservations
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
    except:
        return None


def sanitize_list(data):
    """Ensure list of dicts is fully JSON + PDF safe."""
    out = []
    for row in data or []:
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

    # Resolve slotbooking uid ONCE here so we can pass it to get_available_years.
    # Without this, equipment/reservation years (e.g. 2013 for member 189) are
    # never included because those tables live in slotbooking, not hr_portal.
    from models.staff import _get_uid_from_member
    _slot_uid = _get_uid_from_member(member_id)
    avail_years, best_year = get_available_years(member_id=member_id, memberid=_slot_uid)
    year = request.args.get("year", type=int) or best_year
    full_access = is_full_access()

    data = run_parallel({
        "person": lambda: get_person(member_id),
        "attendance": lambda: get_attendance_stats(member_id, year),
        "equipment": lambda: get_equipment_stats(member_id, year),
        "projects": lambda: get_project_data(member_id),
        "monthly": lambda: get_monthly_reports(member_id, year),
        "committees": lambda: get_committee_involvement(member_id),
        "permissions": lambda: get_permissions(member_id),
        "tracking": lambda: get_profile_tracking(member_id, year) if full_access else [],
        "reservations": lambda: get_staff_reservations(member_id, year),
        "system_owned": lambda: get_staff_system_owned(member_id),
        "owner_track": lambda: get_staff_owner_track(member_id),
        "tool_perms_rich": lambda: get_staff_tool_perms_rich(member_id),
        "trend": lambda: get_attendance_trend(member_id) or []
    })

    if not data.get("person"):
        return render_template("not_found.html", member_id=member_id), 404

    person = safe_dict(data["person"])

    html = render_template(
        "profile.html",
        person=person,
        att=safe_json(data.get("attendance", {})),
        equip=data.get("equipment", {}),
        projects=data.get("projects", {}),
        monthly=data.get("monthly", []),
        committees=data.get("committees", []),
        permissions=data.get("permissions", []),
        trend=data.get("trend", []),
        training=data.get("training", []),
        tracking=data.get("tracking", []),
        full_access=full_access,
        selected_year=year,
        avail_years=avail_years,
        member_id=member_id,
        reservations=data.get("reservations", []),
        system_owned=data.get("system_owned", []),
        owner_track=data.get("owner_track", []),
        tool_perms_rich=data.get("tool_perms_rich", []),
    )

    response = make_response(html)
    response.headers["X-Total-Time"] = f"{round((time.time()-start_total)*1000,2)}ms"
    return response


# ========================= PDF PROFILE =========================

@bp.route("/profile/<int:member_id>/pdf")
@staff_required
def profile_pdf(member_id):

    import traceback

    try:
        year = request.args.get("year", type=int) or date.today().year
        full_access = is_full_access()

        data = run_parallel({
            "person": lambda: get_person(member_id),
            "attendance": lambda: get_attendance_stats(member_id, year),
            "equipment": lambda: get_equipment_stats(member_id, year),
            "projects": lambda: get_project_data(member_id),
            "monthly": lambda: get_monthly_reports(member_id, year),
            "committees": lambda: get_committee_involvement(member_id),
            "permissions": lambda: get_permissions(member_id),
            "tracking": lambda: get_profile_tracking(member_id, year) if full_access else [],
            "reservations": lambda: get_staff_reservations(member_id, year),
            "owner_track": lambda: get_staff_owner_track(member_id),
            "tool_perms_rich": lambda: get_staff_tool_perms_rich(member_id),
        })

        if not data.get("person"):
            return render_template("not_found.html", member_id=member_id), 404

        # ✅ SANITIZE CRITICAL DATA (THIS FIXES YOUR ISSUE)
        owner_track = sanitize_list(data.get("owner_track"))
        reservations = sanitize_list(data.get("reservations"))
        training = sanitize_list(data.get("training"))
        tracking = sanitize_list(data.get("tracking"))
        tool_perms = sanitize_list(data.get("tool_perms_rich"))

        # 🔥 FIX FIELD MISMATCH (VERY IMPORTANT)
        for row in owner_track:
            row["ownership_date"] = safe_date(row.get("owned_since"))
            row["removal_date"] = safe_date(row.get("removed_on"))

        html = render_template(
            "profile_pdf.html",
            person=safe_dict(data["person"]),
            att=safe_json(data.get("attendance", {})),
            equip=data.get("equipment", {}),
            projects=data.get("projects", {}),
            permissions=data.get("permissions", []),
            trend=get_attendance_trend(member_id),
            training=training,
            tracking=tracking,
            reservations=reservations,
            system_owner_track=owner_track,  # matches template
            tool_perms_rich=tool_perms,
            member_id=member_id,
            now=datetime.now().strftime("%d %b %Y, %I:%M %p"),
            selected_year=year,
        )

        # ================= PDF GENERATION =================

        from weasyprint import HTML

        pdf = HTML(
            string=html,
            base_url=request.host_url
        ).write_pdf()

        response = make_response(pdf)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="IITBNF_Profile_{member_id}_{year}.pdf"'
        )

        return response

    except Exception as e:
        traceback.print_exc()
        return f"PDF generation failed: {str(e)}", 500


# ========================= UTIL =========================

def extract_year(name):
    m = re.search(r'\d{4}', name)
    return int(m.group()) if m else 0