"""
routes/lab_profile.py — /lab/<memberid>
"""
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required, is_full_access
from utils import run_parallel, safe_dict
from models.lab import (get_lab_errors, get_lab_user, get_lab_stats, get_lab_reservations,
                         get_lab_equipment_requests, get_lab_access_log,
                         get_lab_tool_permissions, get_training_report, get_lab_cancellations,
                         get_lab_registration, get_session_reports) 
from models.staff import get_available_years, _get_lab_projects
from models.ai import generate_staff_report

bp = Blueprint("lab_profile", __name__)


@bp.route("/lab/<int:memberid>")
@login_required
def lab_profile(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        flash("You can only view your own lab profile.", "error")
        return redirect(url_for("lab_profile.lab_profile", memberid=session["memberid"]))

    year        = request.args.get("year", type=int) or date.today().year
    avail_years = get_available_years(memberid=memberid)

    data = run_parallel({
        "user":         lambda: get_lab_user(memberid),
        "stats":        lambda: get_lab_stats(memberid),
        "reservations": lambda: get_lab_reservations(memberid, year),
        "requests":     lambda: get_lab_equipment_requests(memberid, year),
        "lab_access":   lambda: get_lab_access_log(memberid, year),
        "tool_perms":   lambda: get_lab_tool_permissions(memberid),
        "projects":     lambda: _get_lab_projects(memberid),
        "training":     lambda: get_training_report(memberid, year),
        'cancellations': lambda: get_lab_cancellations(memberid),
        'errors':        lambda: get_lab_errors(memberid) if is_full_access() else [],
        'registration':  lambda: get_lab_registration(memberid),
        'session_reports': lambda: get_session_reports(memberid),
    })

    if not data.get("user"):
        return render_template("not_found.html", member_id=memberid), 404

    user     = data["user"]
    stats    = data.get("stats",    {})
    projects = data.get("projects", {})

    user_safe  = safe_dict(user)
    ai_summary = generate_staff_report(member_id=memberid, audience="management").get("report", "")

    return render_template("lab_profile.html",
        user=user_safe, stats=stats,
        reservations=data.get("reservations", []),
        requests=data.get("requests",     []),
        lab_access=data.get("lab_access", []),
        tool_perms=data.get("tool_perms", []),
        projects=projects,
        ai_summary=ai_summary,
        training=data.get("training",     []),
        selected_year=year,
        avail_years=avail_years,
        memberid=memberid,
        full_access=is_full_access(),
        cancellations=data.get('cancellations') or [],
        errors=data.get('errors') or [],
        reg=data.get('registration'),
        session_reports=data.get('session_reports') or [],
    )
