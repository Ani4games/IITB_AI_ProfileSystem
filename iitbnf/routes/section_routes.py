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
    get_attendance_stats, get_equipment_stats, get_staff_logbook_stats, get_staff_owner_track,
    get_staff_reservations, get_attendance_trend,
    get_slot_activity, get_staff_system_owned, get_staff_tool_perms_rich,
    get_staff_session_reports, get_staff_cancellations, get_staff_lab_access,
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
        trend_result = results.get("trend") or {}
        att["trend"]       = trend_result.get("data", []) if isinstance(trend_result, dict) else trend_result
        att["trend_start"] = trend_result.get("start_month", 1) if isinstance(trend_result, dict) else 1
        att["trend_end"]   = trend_result.get("end_month", 12)  if isinstance(trend_result, dict) else 12
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

# ADD after staff_tool_perms()

@bp.route("/api/section/staff/<int:member_id>/session_reports")
@login_required
def staff_session_reports(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    try:
        data = get_staff_session_reports(member_id)
        return _cached_json(safe_json({"success": True, "data": _serialize(data)}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/staff/<int:member_id>/cancellations")
@login_required
def staff_cancellations(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    try:
        data = get_staff_cancellations(member_id)
        return _cached_json(safe_json({"success": True, "data": _serialize(data)}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/section/staff/<int:member_id>/lab_access")
@login_required
def staff_lab_access(member_id):
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    year = request.args.get("year", type=int) or _cur_year
    try:
        data = get_staff_lab_access(member_id, year)
        return _cached_json(safe_json({"success": True, "year": year, "data": _serialize(data)}))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# Same pattern for lab:
@bp.route("/api/section/lab/<int:memberid>/system_owned")
@login_required  
def lab_system_owned(memberid):
    from models.lab import get_system_owner_tools
    return _cached_json({"data": get_system_owner_tools(memberid)})
# ── ADD THIS BLOCK TO THE END OF section_routes.py ──────────────────────────
#
# GET /api/section/tool/<machid>/session_log?limit=30
#
# Fetches the last N rows from t_<machid> in slotbooking.
# Returns column names dynamically so the frontend can render any schema.
# Joins reservations so we can show who made each booking alongside the
# raw instrument data.
#
# Safe against SQL injection: machid is typed as int by Flask, and the
# table name is constructed only from that validated integer.
# ────────────────────────────────────────────────────────────────────────────

@bp.route("/api/section/tool/<int:machid>/session_log")
@login_required
def tool_session_log(machid):
    """
    Fetch the last N rows from t_<machid> joined with reservations.
    Uses the shared _get_logbook_tables() cache instead of information_schema
    queries (which are 300-800ms each on MariaDB).
    """
    if not is_full_access():
        return jsonify({"success": False, "error": "Access restricted."}), 403

    limit = min(request.args.get("limit", 30, type=int), 200)
    member_id = request.args.get("member_id", type=int)
    if not member_id:
        return jsonify({"success": False, "error": "member_id required"}), 400
    try:
        from db import slots_query
        from models.staff import _get_logbook_tables

        # ── Step 1: verify the table exists via shared cache (0ms on hit) ──────
        logbook_tables = _get_logbook_tables()
        if f"t_{machid}" not in logbook_tables:
            return jsonify({
                "success": False,
                "error":   f"No session log table found for tool {machid}."
            }), 404

        # ── Step 2: fetch column names — one information_schema query ──────────
        # We still need columns once, but we've already saved one query above.
        col_rows = slots_query(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema = 'slotbooking' AND table_name = %s "
            "ORDER BY ORDINAL_POSITION",
            (f"t_{machid}",)
        )
        table_cols = [r["COLUMN_NAME"] for r in (col_rows or [])]

        if not table_cols:
            return jsonify({
                "success": False,
                "error":   "Could not retrieve column metadata."
            }), 500

        # ── Step 3: fetch data joined with reservations ───────────────────────
        rows = slots_query(f"""
            SELECT
                lg.*,
                FROM_UNIXTIME(res.startdate) AS booking_start,
                FROM_UNIXTIME(res.enddate)   AS booking_end,
                TRIM(CONCAT(
                    COALESCE(l.fname, ''), ' ', COALESCE(l.lname, '')
                )) AS member_name,
                l.memberid AS member_id,
                l.position AS member_position
            FROM `t_{machid}` lg
            LEFT JOIN reservations res ON res.resid = lg.reservation_id
            LEFT JOIN login l           ON l.memberid = res.memberid
            WHERE res.memberid = %s
            ORDER BY lg.reservation_id DESC
            LIMIT %s
        """, (member_id, limit))

        if rows is None:
            rows = []

        # ── Step 4: normalise for JSON ────────────────────────────────────────
        from datetime import datetime, date as _date
        from decimal import Decimal

        def _safe(v):
            if v is None:
                return None
            if isinstance(v, (datetime, _date)):
                return str(v)
            if isinstance(v, Decimal):
                return float(v)
            return v

        clean_rows = [
            {k: _safe(v) for k, v in row.items()}
            for row in rows
        ]

        # ── Step 5: column list for the frontend header ───────────────────────
        context_cols    = ["member_name", "booking_start", "booking_end"]
        instrument_cols = [c for c in table_cols if c != "reservation_id"]
        all_cols = (
            ["reservation_id"]
            + context_cols
            + [c for c in instrument_cols if c not in context_cols]
        )

        return jsonify({
            "success": True,
            "machid":  machid,
            "columns": all_cols,
            "rows":    clean_rows,
            "total":   len(clean_rows),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@bp.route("/api/section/staff/<int:member_id>/logbook")
@login_required
def staff_logbook(member_id):
    """
    Logbook entries filled by this staff member across all t_<machid> tables.
    Returns total entry count, number of distinct tools with logs, and a
    per-tool breakdown sorted by entry count descending.

    Deferred fetch — called independently from the frontend AFTER the main
    secondary sections load, so it never blocks attendance/slot/perms display.
    Cached 5 minutes in get_staff_logbook_stats().
    """
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403
    try:
        data = get_staff_logbook_stats(member_id)
        return _cached_json(safe_json({"success": True, "data": data}), max_age=300)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500