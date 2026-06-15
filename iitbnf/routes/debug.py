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

# Import at module level so the debug route shares the SAME cached instances
# as the rest of the app.  Using __import__() inside the route creates a
# separate module object and bypasses the @cached decorator.
from models.staff import get_all_members
from models.lab   import get_all_lab_users  # type: ignore[attr-defined]

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
@bp.route("/debug/db-connection-info")
@staff_required
def db_connection_info():
    import time
    from db import hr_pool

    results = {}

    conn = hr_pool.get_connection()
    try:
        # Use simple queries that return single values — no description needed
        with conn.cursor() as cur:
            cur.execute("SELECT @@hostname AS hostname")
            row = cur.fetchone()
            hostname = row.get("hostname") if row else "unknown"

        with conn.cursor() as cur:
            cur.execute("SELECT @@port AS port")
            row = cur.fetchone()
            port = row.get("port") if row else "unknown"

        with conn.cursor() as cur:
            cur.execute("SELECT @@socket AS socket")
            row = cur.fetchone()
            socket_path = row.get("socket") if row else "unknown"

        with conn.cursor() as cur:
            cur.execute("SELECT @@version AS version")
            row = cur.fetchone()
            version = row.get("version") if row else "unknown"

        with conn.cursor() as cur:
            cur.execute("SELECT USER() AS user")
            row = cur.fetchone()
            user = row.get("user") if row else "unknown"

        with conn.cursor() as cur:
            cur.execute("SELECT CONNECTION_ID() AS conn_id")
            row = cur.fetchone()
            conn_id = row.get("conn_id") if row else "unknown"

        results["server"] = {
            "hostname":      hostname,
            "port":          port,
            "socket":        socket_path,
            "version":       version,
            "connected_as":  user,
            "connection_id": conn_id,
        }

        # Connection type detection
        results["connection"] = {
            "kind":          conn._kind,   # "connector" or "pymysql"
            "using_pipe":    conn._kind == "connector",
            "client_host":   conn.host,
            "client_port":   conn.port,
        }

    finally:
        hr_pool.return_connection(conn)

    # Latency test — 10 queries
    latencies = []
    for _ in range(10):
        t0 = time.perf_counter()
        c = hr_pool.get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT 1 AS val")
            cur.fetchone()
        hr_pool.return_connection(c)
        latencies.append(round((time.perf_counter() - t0) * 1000, 2))

    results["latency_ms"] = {
        "samples": latencies,
        "min":     min(latencies),
        "max":     max(latencies),
        "avg":     round(sum(latencies) / len(latencies), 2),
    }

    return jsonify(results)
# In debug.py
@bp.route("/debug/reconnect-pool")
@staff_required
def reconnect_pool():
    from db import hr_pool, slots_pool, _drain_and_refill_pool_local

    _drain_and_refill_pool_local(hr_pool)
    _drain_and_refill_pool_local(slots_pool)
    return jsonify({"status": "pools drained and refilled (best-effort)"})
@bp.route("/debug/connector-compare")
@staff_required
def connector_compare():
    import time
    import mysql.connector

    results = {}

    # Test 1: pure Python (current)
    try:
        latencies = []
        for _ in range(10):
            t0 = time.perf_counter()
            conn = mysql.connector.connect(
                unix_socket = "\\\\.\\pipe\\MySQL",
                user        = "root",
                password    = "Ani4MariaDB",
                database    = "hr_portal",
                use_pure    = True,
            )
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT 1 AS val")
            cur.fetchone()
            cur.close()
            conn.close()
            latencies.append(round((time.perf_counter() - t0) * 1000, 2))
        results["pure_python"] = {
            "avg": round(sum(latencies)/len(latencies), 2),
            "min": min(latencies),
            "max": max(latencies),
            "samples": latencies,
        }
    except Exception as e:
        results["pure_python"] = {"error": str(e)}

    # Test 2: C extension
    try:
        latencies = []
        for _ in range(10):
            t0 = time.perf_counter()
            conn = mysql.connector.connect(
                unix_socket = "\\\\.\\pipe\\MySQL",
                user        = "root",
                password    = "Ani4MariaDB",
                database    = "hr_portal",
                use_pure    = False,
            )
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT 1 AS val")
            cur.fetchone()
            cur.close()
            conn.close()
            latencies.append(round((time.perf_counter() - t0) * 1000, 2))
        results["c_extension"] = {
            "avg": round(sum(latencies)/len(latencies), 2),
            "min": min(latencies),
            "max": max(latencies),
            "samples": latencies,
        }
    except Exception as e:
        results["c_extension"] = {"error": str(e)}

    # Test 3: pymysql TCP for baseline comparison
    try:
        import pymysql
        import socket as _socket
        latencies = []
        for _ in range(10):
            t0 = time.perf_counter()
            conn = pymysql.connect(
                host     = "localhost",
                port     = 3306,
                user     = "root",
                password = "Ani4MariaDB",
                database = "hr_portal",
                cursorclass = pymysql.cursors.DictCursor,
            )
            sock = getattr(conn, '_sock', None)
            if sock:
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            conn.close()
            latencies.append(round((time.perf_counter() - t0) * 1000, 2))
        results["pymysql_tcp"] = {
            "avg": round(sum(latencies)/len(latencies), 2),
            "min": min(latencies),
            "max": max(latencies),
            "samples": latencies,
        }
    except Exception as e:
        results["pymysql_tcp"] = {"error": str(e)}

    # Test 4: pooled connection reuse (what the app actually does)
    try:
        from db import hr_pool
        latencies = []
        for _ in range(10):
            t0 = time.perf_counter()
            c = hr_pool.get_connection()
            with c.cursor() as cur:
                cur.execute("SELECT 1 AS val")
                cur.fetchone()
            hr_pool.return_connection(c)
            latencies.append(round((time.perf_counter() - t0) * 1000, 2))
        results["pooled_reuse"] = {
            "avg": round(sum(latencies)/len(latencies), 2),
            "min": min(latencies),
            "max": max(latencies),
            "samples": latencies,
            "note": "This is what the app uses — get from pool, query, return"
        }
    except Exception as e:
        results["pooled_reuse"] = {"error": str(e)}

    return jsonify(results)
@bp.route("/debug/ping-analysis")
@staff_required
def ping_analysis():
    import time
    from db import hr_pool

    results = []
    for i in range(20):
        # Check idle time before getting connection
        # Get connection and measure just the get_connection overhead
        t0 = time.perf_counter()
        c = hr_pool.get_connection()
        get_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Measure just the query
        t1 = time.perf_counter()
        with c.cursor() as cur:
            cur.execute("SELECT 1 AS val")
            cur.fetchone()
        query_ms = round((time.perf_counter() - t1) * 1000, 2)

        # Check last_used on the connection
        idle_before = round(
            time.monotonic() - getattr(c, '_last_used', time.monotonic()), 2
        )

        hr_pool.return_connection(c)
        total_ms = round((time.perf_counter() - t0) * 1000, 2)

        results.append({
            "iteration":   i + 1,
            "get_conn_ms": get_ms,
            "query_ms":    query_ms,
            "total_ms":    total_ms,
            "pinged":      get_ms > 20,  # if get_conn took >20ms, ping fired
        })

        time.sleep(0.1)  # small gap between iterations

    pinged_count = sum(1 for r in results if r["pinged"])
    avg_total = round(sum(r["total_ms"] for r in results) / len(results), 2)
    avg_query = round(sum(r["query_ms"] for r in results) / len(results), 2)
    avg_get   = round(sum(r["get_conn_ms"] for r in results) / len(results), 2)

    t_raw = []
    for _ in range(10):
        t0 = time.perf_counter()
        c = hr_pool.get_connection()
        with c.cursor() as cur:
            cur.execute("DO 1")   # MariaDB no-op, returns nothing, no InnoDB touch
            cur.fetchall()
        hr_pool.return_connection(c)
        t_raw.append(round((time.perf_counter() - t0) * 1000, 2))

    # Add to return dict:
    
    return jsonify({
        "summary": {
            "avg_total_ms":    avg_total,
            "avg_get_conn_ms": avg_get,
            "avg_query_ms":    avg_query,
            "ping_fired_count": pinged_count,
            "out_of":          len(results),
            "do1_latency": {
        "avg": round(sum(t_raw)/len(t_raw), 2),
        "min": min(t_raw),
        "max": max(t_raw),
        "samples": t_raw,
        "note": "DO 1 = pure protocol round-trip, no InnoDB, no table access"
    }
        },
        "iterations": results,
    })

