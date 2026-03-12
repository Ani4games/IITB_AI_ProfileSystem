"""
routes/dashboard.py — /dashboard
"""
from datetime import date
from flask import Blueprint, render_template, redirect, url_for, session, request
from auth import login_required
from config import STAFF_POSITIONS
from utils import run_parallel
from models.staff import get_all_members
from models.lab import get_all_lab_users, get_announcements
from models.dashboard import get_system_health, get_expiry_alerts

bp = Blueprint("dashboard", __name__)


@bp.route("/dashboard")
@login_required
def dashboard():
    if not session.get("is_admin") == 1:
        # Staff/Faculty → their staff profile
        if session.get("position") in STAFF_POSITIONS:
            return redirect(url_for("profile.profile", member_id=session["memberid"]))
        return redirect(url_for("lab_profile.lab_profile", memberid=session["memberid"]))

    dash_year       = request.args.get("year", type=int) or date.today().year
    dash_avail_years = list(range(date.today().year, 2014, -1))

    def load_health():
        return get_system_health(dash_year)

    results = run_parallel({
        "members":       get_all_members,
        "lab_users":     get_all_lab_users,
        "health":        load_health,
        "expiry":        lambda: get_expiry_alerts(60),
        "announcements": get_announcements,
    })

    members    = results.get("members",    [])
    lab_users  = results.get("lab_users",  [])
    health     = results.get("health",     {})

    role_groups = {}
    for m in members:
        role_groups.setdefault(m.get("role_name", "Staff"), []).append(m)

    pos_groups = {}
    for u in lab_users:
        pos_groups.setdefault(u.get("position", "Other"), []).append(u)

    return render_template("dashboard.html",
        members=members,
        role_groups=role_groups,
        lab_users=lab_users,
        pos_groups=pos_groups,
        expiry_alerts=results.get("expiry", []),
        health=health,
        announcements=results.get("announcements", []),
        dash_avail_years=dash_avail_years,
        selected_year=dash_year,
        full_access=True,
    )
