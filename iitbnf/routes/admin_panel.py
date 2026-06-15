"""
routes/admin_panel.py — /admin-panel
=====================================
Cleaned + scalable admin panel blueprint
"""

import traceback
from flask import Blueprint, render_template, jsonify
from auth import staff_required
from utils import run_parallel
from models.staff import get_all_members
from models.lab import get_all_lab_users

def _build_search_index(members, lab_users):
    """
    Build a single unified PEOPLE array for the JS search.
    Staff and lab users are merged — no tabs, no classifications.
    Each entry carries a `kind` field ("staff"/"lab") used only for
    the avatar colour; the search itself is blind to the distinction.

    IITBNF Staff members appear in both the hr_portal (staff) list AND the
    slotbooking (lab) list.  We deduplicate by excluding any lab user whose
    memberid already has a staff entry, so they appear exactly once with
    kind="staff" pointing to their /profile/<id> page.
    """
    people = []

    # Build a set of member IDs that already have a staff entry
    staff_ids = {m.get("member_id") for m in members}

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

    FACULTY_POSITIONS = {'Faculty', 'Institute Facility', 'NCPRE Academic', 'Project Staff'}

    for u in lab_users:
        uid = u.get("memberid", 0)
        pos = u.get("position") or ""

        # Skip if already represented as a staff entry
        if uid in staff_ids:
            continue

        fname = u.get("fname") or ""
        lname = u.get("lname") or ""
        name  = f"{fname} {lname}".strip()
        dept  = u.get("department") or ""
        parts = name.split()
        init  = "".join(p[0].upper() for p in parts[:2]) if parts else "??"

        # Faculty-type positions link to /profile/ not /lab/
        if pos in FACULTY_POSITIONS:
            url  = f"/profile/{uid}"
            kind = "staff"
        else:
            url  = f"/lab/{uid}"
            kind = "lab"

        people.append({
            "id":   f"{uid:04d}",
            "name": name,
            "sub":  f"{pos} · {dept}".strip(" ·"),
            "url":  url,
            "init": init,
            "kind": kind,
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

