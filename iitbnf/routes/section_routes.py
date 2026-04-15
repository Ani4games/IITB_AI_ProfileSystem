"""
routes/section_routes.py — Per-section AJAX data endpoints.

Each endpoint returns JSON for a single profile section,
allowing the frontend to refresh individual sections independently
without a full page reload.

Staff routes:
    GET /api/section/staff/<member_id>/attendance?year=
    GET /api/section/staff/<member_id>/equipment?year=
    GET /api/section/staff/<member_id>/monthly?year=
    GET /api/section/staff/<member_id>/training?year=
    GET /api/section/staff/<member_id>/projects

Lab routes:
    GET /api/section/lab/<memberid>/reservations?year=
    GET /api/section/lab/<memberid>/requests?year=
    GET /api/section/lab/<memberid>/training?year=
    GET /api/section/lab/<memberid>/projects
"""

from datetime import date
from flask import Blueprint, request, jsonify, session
from auth import login_required, is_full_access
from models.staff import (
    get_attendance_stats, get_equipment_stats,
    get_monthly_reports, get_project_data,
    get_staff_reservations, get_attendance_trend
)
from models.lab import (
    get_lab_reservations, get_lab_equipment_requests,
    safe_json
)
from models.staff import _get_lab_projects

bp = Blueprint("section", __name__)

_cur_year = date.today().year


# ══════════════════════════════════════════════════════════════════
# STAFF SECTIONS
# ══════════════════════════════════════════════════════════════════

@bp.route("/api/section/staff/<int:member_id>/attendance")
@login_required
def staff_attendance(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        att = get_attendance_stats(member_id, year)
        trend = get_attendance_trend(member_id, year)  # ✅ NEW
        att["trend"] = trend  # Include trend in response
        return jsonify(safe_json({"success": True, "year": year, "data": att}))
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
        return jsonify(safe_json({"success": True, "year": year, "data": equip}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/staff/<int:member_id>/monthly")
@login_required
def staff_monthly(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        rows = get_monthly_reports(member_id, year) or []
        # Serialize dates
        result = []
        for r in rows:
            row = dict(r)
            for k, v in row.items():
                if hasattr(v, 'isoformat'):
                    row[k] = v.isoformat()
            result.append(row)
        return jsonify(safe_json({"success": True, "year": year, "data": result}))
    except Exception as e:
        return jsonify(safe_json({"success": False, "error": str(e)})), 500

@bp.route("/api/section/staff/<int:member_id>/reservations")
@login_required
def staff_reservations(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        rows = get_staff_reservations(member_id, year) or []
        return jsonify(safe_json({"success": True, "year": year, "data": _serialize(rows)}))
    except Exception as e:
        return jsonify(safe_json({"success": False, "error": str(e)})), 500


@bp.route("/api/section/staff/<int:member_id>/projects")
@login_required
def staff_projects(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    try:
        data = get_project_data(member_id) or {}
        result = {}
        for section, val in data.items():
            if isinstance(val, list):
                rows = []
                for r in val:
                    row = dict(r)
                    for k, v in row.items():
                        if hasattr(v, 'isoformat'):
                            row[k] = v.isoformat()
                    rows.append(row)
                result[section] = rows
            else:
                result[section] = val
        return jsonify(safe_json({"success": True, "data": result}))
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
            if hasattr(v, 'isoformat'):
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
        return jsonify({"success": True, "year": year, "data": _serialize(rows)})
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
        return jsonify({"success": True, "year": year, "data": _serialize(rows)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/lab/<int:memberid>/projects")
@login_required
def lab_projects(memberid):
    if not is_full_access() and session.get("memberid") != memberid:
        return jsonify({"error": "Access restricted."}), 403
    try:
        data = _get_lab_projects(memberid) or {}
        result = {}
        for section, val in data.items():
            if isinstance(val, list):
                result[section] = _serialize(val)
            else:
                result[section] = val
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
