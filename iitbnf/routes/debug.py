"""
routes/debug.py — Debug and performance monitoring routes (admin only).
"""
import time as _time
from datetime import datetime, date
from flask import Blueprint, jsonify, request
from auth import staff_required
from flask import session
from cache import cache
from db import hr_pool, slots_pool, hr_query, slots_query
from utils import run_parallel

# Import at module level so the debug route shares the SAME cached instances
# as the rest of the app.  Using __import__() inside the route creates a
# separate module object and bypasses the @cached decorator.
from models.staff import get_all_members
from models.lab   import get_all_lab_users, get_announcements_all  # type: ignore[attr-defined]

bp = Blueprint("debug", __name__)

@bp.route("/debug/speed-dashboard")
@staff_required
def speed_dashboard():
    if not session.get("is_admin"):
        return "Admin only", 403
    return jsonify({
        "cache": {
            "size": len(cache._cache),
            "keys": list(cache._cache.keys())[:20],
        },
        "connection_pools": {
            "hr":    {"active": hr_pool._active,    "queue": hr_pool._pool.qsize()},
            "slots": {"active": slots_pool._active, "queue": slots_pool._pool.qsize()},
        },
        "timestamp": datetime.now().isoformat(),
    })


@bp.route("/debug/db-test")
@staff_required
def db_test():
    from db import hr_query, slots_query
    try:
        hr_ok    = bool(hr_query("SELECT 1 AS ok"))
        slots_ok = bool(slots_query("SELECT 1 AS ok"))
        return jsonify({"hr_portal": hr_ok, "slotbooking": slots_ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/debug/timings")
@staff_required
def timings():
    """
    Measures the real wall-clock time of every major function called during
    admin panel and profile page loads.  Hit this URL once to see exactly
    which DB call is slow.

    Usage:
        /debug/timings                    — admin panel functions
        /debug/timings?member_id=189      — profile page functions for member 189
        /debug/timings?member_id=189&cold=1  — bypass cache to see raw DB times
    """
    member_id = request.args.get("member_id", type=int)
    cold      = request.args.get("cold", type=int, default=0)

    if cold:
        cache.clear()

    results = {}

    def t(label, fn):
        t0 = _time.perf_counter()
        try:
            val = fn()
            ms  = round((_time.perf_counter() - t0) * 1000, 1)
            size = len(val) if isinstance(val, (list, dict)) else "n/a"
            results[label] = {"ms": ms, "rows": size, "ok": True}
        except Exception as e:
            ms = round((_time.perf_counter() - t0) * 1000, 1)
            results[label] = {"ms": ms, "error": str(e), "ok": False}

    # ── Always run: admin panel functions ─────────────────────────────────────
    t("hr SELECT 1",              lambda: hr_query("SELECT 1"))
    t("slots SELECT 1",           lambda: slots_query("SELECT 1"))
    t("get_all_members",          lambda: get_all_members())
    t("get_all_lab_users",        lambda: get_all_lab_users())
    t("get_announcements_all",    lambda: get_announcements_all())

    # ── Profile-specific functions ────────────────────────────────────────────
    if member_id:
        from models.staff import (
            get_person, get_attendance_stats, get_attendance_trend,
            get_available_years, get_slot_activity,
            get_staff_system_owned, get_staff_owner_track,
            get_staff_tool_perms_rich, _warmup_uid, _get_uid_from_member,
        )
        year = date.today().year

        t("_warmup_uid",            lambda: _warmup_uid(member_id))
        t("_get_uid_from_member",   lambda: _get_uid_from_member(member_id))
        t("get_person",             lambda: get_person(member_id))
        t("get_available_years",    lambda: get_available_years(member_id=member_id))
        t("get_attendance_stats",   lambda: get_attendance_stats(member_id, year=year))
        t("get_attendance_trend",   lambda: get_attendance_trend(member_id, year=year))
        t("get_slot_activity",      lambda: get_slot_activity(member_id, year=year))
        t("get_staff_system_owned", lambda: get_staff_system_owned(member_id))
        t("get_staff_owner_track",  lambda: get_staff_owner_track(member_id))
        t("get_staff_tool_perms_rich", lambda: get_staff_tool_perms_rich(member_id))

    total_ms = sum(v["ms"] for v in results.values())
    sorted_results = dict(sorted(results.items(), key=lambda x: -x[1]["ms"]))

    return jsonify({
        "total_sequential_ms": round(total_ms, 1),
        "note": "In production these run in parallel — slowest single call dominates.",
        "cold_cache": bool(cold),
        "member_id": member_id,
        "timings": sorted_results,
    })