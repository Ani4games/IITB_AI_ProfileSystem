"""
routes/debug.py — Debug and performance monitoring routes (admin only).
"""
from datetime import datetime
from flask import Blueprint, jsonify
from auth import staff_required
from flask import session
from cache import cache
from db import hr_pool, slots_pool
from models.dashboard import get_system_health

bp = Blueprint("debug", __name__)


@bp.route("/debug-health")
@staff_required
def debug_health():
    return jsonify(get_system_health())


@bp.route("/debug/performance")
@staff_required
def debug_performance():
    if not session.get("is_admin"):
        return "Admin only", 403
    return jsonify({"message": "Performance monitoring enabled"})


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
