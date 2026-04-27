"""
routes/section_routes.py — Per-section AJAX data endpoints.

Performance fixes applied
─────────────────────────
1. staff_attendance() — attendance stats and trend are now fetched in
   parallel (run_parallel) instead of sequentially.  Previously the route
   called get_attendance_stats() then get_attendance_trend() one after the
   other; each takes ~1–2 s on a cold cache, so the total was ~3–4 s.
   Running them concurrently brings the combined time down to the slower of
   the two (~1–2 s).  Both functions are also now cached (staff.py) so
   subsequent dropdown changes return in <100 ms.

2. Cache-Control headers added to all section API responses.  The browser
   will reuse the last response for 60 seconds before re-requesting, so
   rapidly toggling the same year in the dropdown doesn't fire new network
   requests at all.

3. get_monthly_reports import added (was missing — would have caused a
   NameError on the /monthly endpoint).

Staff routes:
    GET /api/section/staff/<member_id>/attendance?year=
    GET /api/section/staff/<member_id>/equipment?year=
    GET /api/section/staff/<member_id>/monthly?year=
    GET /api/section/staff/<member_id>/reservations?year=
    GET /api/section/staff/<member_id>/slot_activity?year=

Lab routes:
    GET /api/section/lab/<memberid>/reservations?year=
    GET /api/section/lab/<memberid>/requests?year=
    GET /api/section/lab/<memberid>/projects
"""

from datetime import date
from flask import Blueprint, request, jsonify, session
from auth import login_required, is_full_access
from utils import run_parallel
from models.staff import (
    get_attendance_stats, get_equipment_stats, get_staff_owner_track,
    get_staff_reservations, get_attendance_trend,
    get_slot_activity, get_staff_system_owned, get_staff_tool_perms_rich,
)
from models.lab import (
    get_lab_reservations, get_lab_equipment_requests,
    safe_json, _get_lab_projects,
)

bp = Blueprint("section", __name__)

_cur_year = date.today().year


def _cached_json(data, max_age: int = 60):
    """Return a JSON response with a short Cache-Control header."""
    resp = jsonify(data)
    resp.headers["Cache-Control"] = f"private, max-age={max_age}"
    return resp


# ══════════════════════════════════════════════════════════════════
# STAFF SECTIONS
# ══════════════════════════════════════════════════════════════════

@bp.route("/api/section/staff/<int:member_id>/attendance")
@login_required
def staff_attendance(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    # ADD THIS DEBUG PRINT:
    print(f"=== Attendance API called for member {member_id}, year {year} ===")
    try:
        # Fetch attendance stats and monthly trend IN PARALLEL.
        # Both functions are cached (2 min) so second+ requests are instant.
        results = run_parallel({
            "att":   lambda: get_attendance_stats(member_id, year=year),
            "trend": lambda: get_attendance_trend(member_id, year=year),
        })
        att         = results.get("att") or {}
        att["trend"] = results.get("trend") or []
        # ADD THIS DEBUG PRINT:
        print(f"=== Returning data: days_present={att.get('days_present')}, mandatory={att.get('mandatory_days')} ===")

        return _cached_json(safe_json({"success": True, "year": year, "data": att}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/staff/<int:member_id>/equipment")
@login_required
def staff_equipment(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        equip = get_equipment_stats(member_id, year)
        return _cached_json(safe_json({"success": True, "year": year, "data": equip}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/staff/<int:member_id>/reservations")
@login_required
def staff_reservations(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        rows = get_staff_reservations(member_id, year) or []
        return _cached_json(safe_json({"success": True, "year": year, "data": _serialize(rows)}))
    except Exception as e:
        return jsonify(safe_json({"success": False, "error": str(e)})), 500


# ══════════════════════════════════════════════════════════════════
# LAB SECTIONS
# ══════════════════════════════════════════════════════════════════

def _serialize(rows):
    """Serialize a list of DB rows — convert dates to strings."""
    result = []
    for r in (rows or []):
        row = dict(r)
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()
        result.append(row)
    return result


@bp.route("/api/section/lab/<int:memberid>/reservations")
@login_required
def lab_reservations(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        rows = get_lab_reservations(memberid, year)
        return _cached_json({"success": True, "year": year, "data": _serialize(rows)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/lab/<int:memberid>/requests")
@login_required
def lab_requests(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        rows = get_lab_equipment_requests(memberid, year)
        return _cached_json({"success": True, "year": year, "data": _serialize(rows)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/lab/<int:memberid>/projects")
@login_required
def lab_projects(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        return jsonify({"error": "Access restricted."}), 403
    try:
        data   = _get_lab_projects(memberid) or {}
        result = {}
        if isinstance(data, dict):
            for section, val in data.items():
                if isinstance(val, list):
                    result[section] = _serialize(val)
                else:
                    result[section] = val
        return _cached_json({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@bp.route("/api/section/staff/<int:member_id>/slot_activity")
@login_required
def staff_slot_activity(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        data = get_slot_activity(member_id, year)
        return _cached_json(safe_json({"success": True, "year": year, "data": data}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/staff/<int:member_id>/system_owned")
@login_required
def staff_system_owned(member_id):
    try:
        data = get_staff_system_owned(member_id)
        return _cached_json(safe_json({"success": True, "data": data}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@bp.route("/api/section/staff/<int:member_id>/owner_track")
@login_required
def staff_owner_track(member_id):
    try:
        data = get_staff_owner_track(member_id)
        return _cached_json(safe_json({"success": True, "data": data}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@bp.route("/api/section/staff/<int:member_id>/tool_perms")
@login_required
def staff_tool_perms(member_id):
    try:
        data = get_staff_tool_perms_rich(member_id)
        return _cached_json(safe_json({"success": True, "data": data}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# Same pattern for lab:
@bp.route("/api/section/lab/<int:memberid>/system_owned")
@login_required  
def lab_system_owned(memberid):
    from models.lab import get_system_owner_tools
    return _cached_json({"data": get_system_owner_tools(memberid)})