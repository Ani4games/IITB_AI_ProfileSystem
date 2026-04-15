"""
routes/admin_panel.py — /admin-panel
=====================================
Cleaned + scalable admin panel blueprint
"""

import json
import traceback
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, make_response
from auth import staff_required
from cache import cache
from db import hr_execute, slots_execute, hr_query
from utils import run_parallel
from models.staff import get_all_members
from models.lab import get_all_lab_users, get_announcements_all
from models.dashboard import get_system_health, get_expiry_alerts


def _build_search_index(members, lab_users):
    """Build the STAFF and LAB arrays expected by the JS search dropdown."""
    staff_index = []
    for m in members:
        name = (m.get("display_name") or "").strip()
        mid  = m.get("member_id", 0)
        desig = m.get("designation") or ""
        team  = m.get("team") or ""
        parts = name.split()
        init  = "".join(p[0].upper() for p in parts[:2]) if parts else "??"
        staff_index.append({
            "id":   f"{mid:04d}",
            "name": name,
            "sub":  f"{desig} · {team}".strip(" ·"),
            "url":  f"/profile/{mid}",
            "init": init,
            "s":    f"{name} {desig} {team} {mid:04d}".lower(),
        })

    lab_index = []
    for u in lab_users:
        fname = u.get("fname") or ""
        lname = u.get("lname") or ""
        name  = f"{fname} {lname}".strip()
        uid   = u.get("memberid", 0)
        pos   = u.get("position") or ""
        dept  = u.get("department") or ""
        parts = name.split()
        init  = "".join(p[0].upper() for p in parts[:2]) if parts else "??"
        lab_index.append({
            "id":   f"{uid:04d}",
            "name": name,
            "sub":  f"{pos} · {dept}".strip(" ·"),
            "url":  f"/lab/{uid}",
            "init": init,
            "s":    f"{name} {pos} {dept} {uid:04d}".lower(),
        })

    return staff_index, lab_index


def _build_monthly_chart(members):
    """Count active staff per month for the health chart."""
    today = date.today()
    labels, data = [], []
    try:
        rows = hr_query("""
            SELECT MONTH(date) AS mo, COUNT(DISTINCT memberid) AS cnt
            FROM user_attendance
            WHERE YEAR(date) = %s
            GROUP BY MONTH(date)
            ORDER BY MONTH(date)
        """, (today.year,))
        month_map = {r["mo"]: r["cnt"] for r in (rows or [])}
        for mo in range(1, today.month + 1):
            labels.append(date(today.year, mo, 1).strftime("%b"))
            data.append(month_map.get(mo, 0))
    except Exception:
        pass
    return labels, data

# ✅ Blueprint with prefix
bp = Blueprint("admin_panel", __name__, url_prefix="/admin-panel")


# ── Main panel ────────────────────────────────────────────────────────────────

@bp.route("/", endpoint="index")
@staff_required
def admin_panel_page():
    import traceback
    import json

    try:
        results = run_parallel({
            "members":       get_all_members,
            "lab_users":     get_all_lab_users,
            "announcements": get_announcements_all,
            "health":        lambda: get_system_health(),
            "expiry":        lambda: get_expiry_alerts(60),
        })

        from collections import defaultdict

        members   = results.get("members", [])
        lab_users = results.get("lab_users", [])

        # ── ROLE GROUPING (FIXED) ─────────────────────────────
        role_groups_dict = defaultdict(list)

        for m in members:
            role = m.get("designation") or "Unknown"
            role_groups_dict[role].append(m)

        # Convert to UI-friendly structure
        role_groups = [
            {
                "role": role,
                "list": group,
                "count": len(group)
            }
            for role, group in role_groups_dict.items()
        ]

        # Sort by count DESC (prevents Jinja crash)
        role_groups = sorted(role_groups, key=lambda x: x["count"], reverse=True)

        # ── SEARCH + CHART DATA ──────────────────────────────
        staff_index, lab_index = _build_search_index(members, lab_users)
        monthly_labels, monthly_data = _build_monthly_chart(members)

        # Role counts for chart
        rg_counts = {role: len(group) for role, group in role_groups_dict.items()}

        # ── RENDER ───────────────────────────────────────────
        return render_template(
            "admin_panel.html",
            members              = members,
            lab_users            = lab_users,
            announcements        = results.get("announcements", []),
            health               = results.get("health", {}),
            expiry_alerts        = results.get("expiry", []),

            members_count        = len(members),
            lab_users_count      = len(lab_users),

            # ✅ FIXED STRUCTURE (list of dicts, sorted)
            role_groups          = role_groups,

            # ── JS DATA ───────────────────────────────────────
            staff_json           = json.dumps(staff_index),
            lab_json             = json.dumps(lab_index),
            monthly_labels_json  = json.dumps(monthly_labels),
            monthly_data_json    = json.dumps(monthly_data),
            role_groups_json     = json.dumps(rg_counts),
        )

    except Exception as e:
        traceback.print_exc()
        return f"Error loading admin panel: {e}", 500


# ── API ───────────────────────────────────────────────────────────────────────

@bp.route("/api/health")
@staff_required
def api_health():
    return jsonify(get_system_health())


@bp.route("/api/expiry")
@staff_required
def api_expiry():
    days = request.args.get("days", 60, type=int)
    return jsonify(get_expiry_alerts(days))


@bp.route("/api/staff/<int:member_id>")
@staff_required
def api_get_staff(member_id):
    """Return a single staff row for the edit modal."""
    from models.staff import get_person
    p = get_person(member_id)
    if not p:
        return jsonify({}), 404
    # Stringify dates so JS can populate date inputs
    safe = {}
    for k, v in p.items():
        if hasattr(v, "isoformat"):
            safe[k] = v.isoformat()
        else:
            safe[k] = v
    return jsonify(safe)


@bp.route("/api/lab/<int:memberid>")
@staff_required
def api_get_lab(memberid):
    """Return a single lab user row for the edit modal."""
    from models.lab import get_lab_user
    u = get_lab_user(memberid)
    if not u:
        return jsonify({}), 404
    safe = {}
    for k, v in u.items():
        if hasattr(v, "isoformat"):
            safe[k] = v.isoformat()
        else:
            safe[k] = v
    return jsonify(safe)



# ── Staff CRUD ────────────────────────────────────────────────────────────────

@bp.route("/staff/add", methods=["POST"])
@staff_required
def panel_staff_add():
    f = request.form

    result = hr_execute("""
        INSERT INTO profile (designation, team, email, type_of_appointment,
                             qualification, joining_date, iitb_joining_date, p_project_code)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        f.get("designation", "").strip(),
        f.get("team", "").strip(),
        f.get("email", "").strip(),
        f.get("type_of_appointment", "").strip(),
        f.get("qualification", "").strip(),
        f.get("joining_date") or None,
        f.get("iitb_joining_date") or None,
        f.get("p_project_code", "").strip()
    ))

    if result["ok"]:
        cache.delete_pattern("get_all_members")
        flash(f"Staff member added (ID: {result['last_id']}).", "success")
    else:
        flash(f"Error: {result['error']}", "error")

    return redirect(url_for("admin_panel.index") + "#staff")


@bp.route("/staff/edit/<int:member_id>", methods=["POST"])
@staff_required
def panel_staff_edit(member_id):
    f = request.form

    result = hr_execute("""
        UPDATE profile SET designation=%s, team=%s, email=%s,
            type_of_appointment=%s, qualification=%s,
            joining_date=%s, iitb_joining_date=%s, p_project_code=%s
        WHERE member_id=%s
    """, (
        f.get("designation", "").strip(),
        f.get("team", "").strip(),
        f.get("email", "").strip(),
        f.get("type_of_appointment", "").strip(),
        f.get("qualification", "").strip(),
        f.get("joining_date") or None,
        f.get("iitb_joining_date") or None,
        f.get("p_project_code", "").strip(),
        member_id
    ))

    if result["ok"]:
        cache.delete_pattern("get_person")
        cache.delete_pattern("get_all_members")
        flash("Staff member updated.", "success")
    else:
        flash(f"Error: {result['error']}", "error")

    return redirect(url_for("admin_panel.index") + "#staff")


@bp.route("/staff/deactivate/<int:member_id>", methods=["POST"])
@staff_required
def panel_staff_deactivate(member_id):
    from datetime import date

    result = hr_execute(
        "UPDATE profile SET leaving_date=%s WHERE member_id=%s",
        (date.today(), member_id)
    )

    flash("Staff deactivated." if result["ok"] else f"Error: {result['error']}",
          "success" if result["ok"] else "error")

    return redirect(url_for("admin_panel.index") + "#staff")


@bp.route("/staff/delete/<int:member_id>", methods=["POST"])
@staff_required
def panel_staff_delete(member_id):
    result = hr_execute("DELETE FROM profile WHERE member_id=%s", (member_id,))

    flash("Staff deleted." if result["ok"] else f"Error: {result['error']}",
          "success" if result["ok"] else "error")

    return redirect(url_for("admin_panel.index") + "#staff")


# ── Lab CRUD ──────────────────────────────────────────────────────────────────

@bp.route("/lab/add", methods=["POST"])
@staff_required
def panel_lab_add():
    f = request.form

    result = slots_execute("""
        INSERT INTO login (email, fname, lname, position, department,
                           supervisor, research_area, expiry_date, mobile, rollno)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        f.get("email", "").strip(),
        f.get("fname", "").strip(),
        f.get("lname", "").strip(),
        f.get("position", "").strip(),
        f.get("department", "").strip(),
        f.get("supervisor", "").strip() or None,
        f.get("research_area", "").strip(),
        f.get("expiry_date", "").strip(),
        f.get("mobile", "").strip(),
        f.get("rollno", "").strip()
    ))

    flash(f"Lab user added (ID: {result['last_id']})." if result["ok"]
          else f"Error: {result['error']}",
          "success" if result["ok"] else "error")

    return redirect(url_for("admin_panel.index") + "#lab")


@bp.route("/lab/edit/<int:memberid>", methods=["POST"])
@staff_required
def panel_lab_edit(memberid):
    f = request.form

    result = slots_execute("""
        UPDATE login SET email=%s, fname=%s, lname=%s, position=%s,
            department=%s, supervisor=%s, research_area=%s,
            expiry_date=%s, mobile=%s, rollno=%s
        WHERE memberid=%s
    """, (
        f.get("email", "").strip(),
        f.get("fname", "").strip(),
        f.get("lname", "").strip(),
        f.get("position", "").strip(),
        f.get("department", "").strip(),
        f.get("supervisor", "").strip() or None,
        f.get("research_area", "").strip(),
        f.get("expiry_date", "").strip(),
        f.get("mobile", "").strip(),
        f.get("rollno", "").strip(),
        memberid
    ))

    flash("Lab user updated." if result["ok"] else f"Error: {result['error']}",
          "success" if result["ok"] else "error")

    return redirect(url_for("admin_panel.index") + "#lab")


@bp.route("/lab/deactivate/<int:memberid>", methods=["POST"])
@staff_required
def panel_lab_deactivate(memberid):
    result = slots_execute(
        "UPDATE login SET expiry_date='01/01/2000' WHERE memberid=%s",
        (memberid,)
    )

    flash("Lab user deactivated." if result["ok"] else f"Error: {result['error']}",
          "success" if result["ok"] else "error")

    return redirect(url_for("admin_panel.index") + "#lab")


@bp.route("/lab/delete/<int:memberid>", methods=["POST"])
@staff_required
def panel_lab_delete(memberid):
    result = slots_execute("DELETE FROM login WHERE memberid=%s", (memberid,))

    flash("Lab user deleted." if result["ok"] else f"Error: {result['error']}",
          "success" if result["ok"] else "error")

    return redirect(url_for("admin_panel.index") + "#lab")


# ── Announcement CRUD (aliased under /admin-panel prefix) ─────────────────────

from datetime import datetime as _dt
from db import slots_execute as _se

@bp.route("/announcement/add", methods=["POST"])
@staff_required
def panel_announcement_add():
    f = request.form
    text, start_str, end_str = f.get("announcement","").strip(), f.get("start_datetime",""), f.get("end_datetime","")
    if not text or not start_str or not end_str:
        flash("All fields are required.", "error")
        return redirect(url_for("admin_panel.index") + "#announcements")
    try:
        start_ts = int(_dt.strptime(start_str, "%Y-%m-%dT%H:%M").timestamp())
        end_ts   = int(_dt.strptime(end_str,   "%Y-%m-%dT%H:%M").timestamp())
        _se("INSERT INTO announcements (announcement, start_datetime, end_datetime) VALUES (%s,%s,%s)",
            (text, start_ts, end_ts))
        flash("Announcement added.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin_panel.index") + "#announcements")


@bp.route("/announcement/edit/<int:aid>", methods=["POST"])
@staff_required
def panel_announcement_edit(aid):
    f = request.form
    text, start_str, end_str = f.get("announcement","").strip(), f.get("start_datetime",""), f.get("end_datetime","")
    if not text or not start_str or not end_str:
        flash("All fields are required.", "error")
        return redirect(url_for("admin_panel.index") + "#announcements")
    try:
        start_ts = int(_dt.strptime(start_str, "%Y-%m-%dT%H:%M").timestamp())
        end_ts   = int(_dt.strptime(end_str,   "%Y-%m-%dT%H:%M").timestamp())
        _se("UPDATE announcements SET announcement=%s, start_datetime=%s, end_datetime=%s WHERE announcementid=%s",
            (text, start_ts, end_ts, aid))
        flash("Announcement updated.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin_panel.index") + "#announcements")


@bp.route("/announcement/delete/<int:aid>", methods=["POST"])
@staff_required
def panel_announcement_delete(aid):
    try:
        _se("DELETE FROM announcements WHERE announcementid=%s", (aid,))
        flash("Announcement deleted.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin_panel.index") + "#announcements")
