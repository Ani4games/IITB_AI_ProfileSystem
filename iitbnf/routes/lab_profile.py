"""
routes/lab_profile.py — /lab/<memberid>
"""

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required, is_full_access
from utils import run_parallel, safe_dict
from models.lab import (get_lab_errors, get_lab_user, get_lab_stats, get_lab_reservations,
                         get_lab_equipment_requests, get_lab_access_log,
                         get_lab_tool_permissions, get_lab_cancellations,
                         get_lab_registration, get_session_reports,
                         is_faculty, get_member_tool_permissions,
                         get_system_owner_tools, get_system_owner_track) 
from models.staff import get_available_years, _get_lab_projects

bp = Blueprint("lab_profile", __name__)

@bp.route("/lab/<int:memberid>")
@login_required
def lab_profile(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        flash("You can only view your own lab profile.", "error")
        return redirect(url_for("lab_profile.lab_profile", memberid=session["memberid"]))
    # Block faculty profiles — they belong to the staff profile system
    if is_faculty(memberid):
        flash("This member is faculty and has a staff profile instead.", "info")
        return redirect(url_for("dashboard.dashboard"))
    avail_years, best_year = get_available_years(memberid=memberid)
    year        = request.args.get("year", type=int) or best_year

    data = run_parallel({
        "user":         lambda: get_lab_user(memberid),
        "stats":        lambda: get_lab_stats(memberid),
        "reservations": lambda: get_lab_reservations(memberid, year),
        "requests":     lambda: get_lab_equipment_requests(memberid, year),
        "lab_access":   lambda: get_lab_access_log(memberid, year),
        "tool_perms":   lambda: get_lab_tool_permissions(memberid),
        "projects":     lambda: _get_lab_projects(memberid),
        'cancellations':    lambda: get_lab_cancellations(memberid),
        'errors':           lambda: get_lab_errors(memberid) if is_full_access() else [],
        'reg':              lambda: get_lab_registration(memberid),
        'session_reports':  lambda: get_session_reports(memberid),
        'tool_perms_rich':  lambda: get_member_tool_permissions(memberid),
        'system_owned':     lambda: get_system_owner_tools(memberid),
        'owner_track':      lambda: get_system_owner_track(memberid),
    })

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
        cancellations=data.get('cancellations') or [],
        errors=data.get('errors') or [],
        reg=data.get('reg'),
        session_reports=data.get('session_reports') or [],
        tool_perms_rich=data.get('tool_perms_rich') or [],
        system_owned=data.get('system_owned') or [],
        owner_track=data.get('owner_track') or [],
    )
