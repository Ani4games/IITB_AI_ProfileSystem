"""
routes/admin_panel.py — /admin-panel
=====================================
Cleaned + scalable admin panel blueprint
"""


import traceback
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, make_response
from auth import staff_required
from cache import cache
from db import hr_execute, slots_execute, hr_query
from utils import run_parallel
from models.staff import get_all_members
from models.lab import get_all_lab_users, get_announcements_all

def _build_search_index(members, lab_users):
    """
    Build a single unified PEOPLE array for the JS search.
    Staff and lab users are merged — no tabs, no classifications.
    Each entry carries a `kind` field ("staff"/"lab") used only for
    the avatar colour; the search itself is blind to the distinction.
    """
    people = []

    for m in members:
        name  = (m.get("display_name") or "").strip()
        mid   = m.get("member_id", 0)
        desig = m.get("designation") or ""
        team  = m.get("team") or ""
        parts = name.split()
        init  = "".join(p[0].upper() for p in parts[:2]) if parts else "??"
        people.append({
            "id":   f"{mid:04d}",
            "name": name,
            "sub":  f"{desig} · {team}".strip(" ·"),
            "url":  f"/profile/{mid}",
            "init": init,
            "kind": "staff",
            "s":    f"{name} {desig} {team} {mid:04d}".lower(),
        })

    for u in lab_users:
        fname = u.get("fname") or ""
        lname = u.get("lname") or ""
        name  = f"{fname} {lname}".strip()
        uid   = u.get("memberid", 0)
        pos   = u.get("position") or ""
        dept  = u.get("department") or ""
        parts = name.split()
        init  = "".join(p[0].upper() for p in parts[:2]) if parts else "??"
        people.append({
            "id":   f"{uid:04d}",
            "name": name,
            "sub":  f"{pos} · {dept}".strip(" ·"),
            "url":  f"/lab/{uid}",
            "init": init,
            "kind": "lab",
            "s":    f"{name} {pos} {dept} {uid:04d}".lower(),
        })

    return people


bp = Blueprint("admin_panel", __name__, url_prefix="/admin-panel")


# ── Main panel ────────────────────────────────────────────────────────────────

@bp.route("/", endpoint="index")
@staff_required
def admin_panel_page():

    try:
        results = run_parallel({
            "members":       get_all_members,
            "lab_users":     get_all_lab_users,
            "announcements": get_announcements_all,
        })

        members   = results.get("members", [])
        lab_users = results.get("lab_users", [])
        # ── SEARCH INDEX (unified — no staff/lab split) ───────
        people_index = _build_search_index(members, lab_users)
        # monthly_labels, monthly_data = _get_monthly_chart_data(date.today().year)

        # ── RENDER ───────────────────────────────────────────
        return render_template(
            "admin_panel.html",
            members              = members,
            lab_users            = lab_users,
            announcements        = results.get("announcements", []),
            members_count        = len(members),
            lab_users_count      = len(lab_users),

            # ── JS DATA ───────────────────────────────────────
            
            # Keep legacy names for any template references that still use them
            staff_json           = [p for p in people_index if p["kind"] == "staff"],
            lab_json             = [p for p in people_index if p["kind"] == "lab"],
            people_json          = people_index  # unified search index for all people (staff + lab)
        )

    except Exception as e:
        traceback.print_exc()
        return f"Error loading admin panel: {e}", 500


# ── API ───────────────────────────────────────────────────────────────────────

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


@bp.route("/api/field-options")
@staff_required
def api_field_options():
    """Return field options for the edit modals."""
    return jsonify({
        "positions": [""],
        "departments": [""]
    })


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
