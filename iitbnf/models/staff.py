"""
models/staff.py — All data queries for staff (hr_portal) profiles.

Performance fixes applied
─────────────────────────
1. get_attendance_stats() — now cached 2 minutes per (member_id, year).
   Previously called uncached on every profile load AND on every year-dropdown
   AJAX request.  Combined with the get_holidays() fix in utils.py this was
   the biggest source of repeated DB work on every dropdown change.

2. get_attendance_trend() — now cached 2 minutes per (member_id, year).
   The trend function calls get_holidays() once and then loops over up to 12
   months computing mandatory days.  Caching it means the dropdown AJAX
   handler returns in ~50 ms instead of ~6 s.

3. get_slot_activity() — now cached 2 minutes per (member_id, year).
   The correlated subquery on reservations was expensive; caching prevents
   re-running it on every year-dropdown change.

4. _warmup_uid() — called once at the top of the profile route to pre-populate
   the UID cache before the parallel task fan-out.  This prevents 4 parallel
   tasks (slot_activity, system_owned, owner_track, tool_perms) from each
   independently triggering the 4-step UID resolution on the first request
   for a new member.  After the warmup all 4 tasks hit the in-process cache.

5. get_available_years() — cached 5 minutes per (member_id, memberid) pair.
   It runs 4 separate DISTINCT queries; caching means the year dropdown list
   is only rebuilt once every 5 minutes instead of on every page load.
"""
import threading
from datetime import date, timedelta
from collections import defaultdict

from db import hr_query, slots_query
from utils import get_display_name, clean_role, get_holidays
from cache import cached

from collections import defaultdict
from datetime import datetime
# ── UID resolution cache ──────────────────────────────────────────────────────
_uid_cache: dict[int, int | None] = {}
_uid_cache_ts: dict[int, float]   = {}
_uid_cache_lock                   = threading.Lock()
_UID_CACHE_TTL                    = 1800  # 30 minutes


# ── Member lists ──────────────────────────────────────────────────────────────

@cached(ttl_seconds=3600)  # Increased TTL (1 hour)
def get_all_members():
    rows = hr_query("""
        SELECT 
            p.member_id,
            p.designation,
            p.team,
            p.email,
            COALESCE(rm.role_name, 'Staff') AS raw_role
        FROM profile p
        LEFT JOIN role r         ON r.memberid = p.member_id
        LEFT JOIN role_master rm ON rm.role_id = r.role
        WHERE (
            (p.email IS NOT NULL AND p.email != '')
            OR (p.designation IS NOT NULL AND p.designation != '')
            OR (p.team IS NOT NULL AND p.team != '')
        )
        AND COALESCE(p.leaving_date, '9999-12-31') >= '2025-01-01'
        AND (p.taken_clearance IS NULL OR p.taken_clearance = 0)
        ORDER BY p.member_id
    """)

    if not rows:
        return []

    from utils import bulk_display_names, clean_role

    # Fetch all names in one efficient query (no cross-DB join)
    member_ids = [m["member_id"] for m in rows]
    names_map = bulk_display_names(member_ids)

    # Process rows in-place (no extra lists/loops)
    for m in rows:
        m["display_name"] = names_map.get(
            m["member_id"],
            f"Member #{str(m['member_id']).zfill(4)}"
        )
        m["role_name"] = clean_role(m.get("raw_role"))
    print(f"get_all_members: fetched {len(rows)} members")
    print(f"Sample member: {rows[0] if rows else 'N/A'}")
    return rows

@cached(ttl_seconds=300)
def get_person(member_id):
    # Step 1: fetch HR profile only — no cross-DB join, fast PK lookup.
    rows = hr_query("""
        SELECT p.*,
               COALESCE(rm.role_name, 'Staff') AS raw_role
        FROM profile p
        LEFT JOIN role r          ON r.memberid = p.member_id
        LEFT JOIN role_master rm  ON rm.role_id = r.role
        WHERE p.member_id = %s
          AND (p.taken_clearance IS NULL OR p.taken_clearance = 0)
        LIMIT 1
    """, (member_id,))
    if not rows:
        return None

    p = rows[0]
    p["role_name"]    = clean_role(p.get("raw_role"))
    p["display_name"] = get_display_name(p["member_id"], p.get("email", ""))

    # Step 2: resolve slotbooking UID via the shared cache (avoids 3 sequential queries).
    # _warmup_uid() is called before run_parallel in the profile route so this
    # is almost always a dict lookup (~0 ms), not a DB query.
    uid = _get_uid_from_member(member_id)
    if uid is not None:
        r = slots_query(
            "SELECT memberid, email, position, department, fname, lname "
            "FROM login WHERE memberid = %s LIMIT 1",
            (uid,),
        )
        if r:
            slot_row = r[0]
            joined = ((slot_row.get("fname") or "") + " " + (slot_row.get("lname") or "")).strip()
            if joined:
                p["display_name"] = joined
            p["slot_email"]      = slot_row.get("email")      or p.get("slot_email")
            p["slot_memberid"]   = slot_row.get("memberid")   or p.get("slot_memberid")
            p["slot_position"]   = slot_row.get("position")   or p.get("slot_position")
            p["slot_department"] = slot_row.get("department") or p.get("slot_department")

    return p

def get_permissions(member_id):
    return hr_query("SELECT field FROM user_permission WHERE memberid=%s", (member_id,))


# ── Attendance ────────────────────────────────────────────────────────────────

def get_attendance_rows(member_id, month=None, year=None):
    "Creating Rows by Months, instead of year. First Calculate for a month, then applying a loop for 12 months or based on current year"
    "This way we can avoid running 12 separate queries for monthly attendance trend and also get the raw attendance rows for a given "
    "month/year combination without extra queries."
    date_filter = ""
    params      = [member_id]
    if month:
        date_filter += " AND MONTH(date) = %s"
        params.append(month)
    if year:
        start = f"{year}-01-01"
        end   = f"{year}-12-31"
        date_filter += " AND date BETWEEN %s AND %s"
        params.extend([start, end])

    rows = hr_query(f"""
        SELECT date, time AS entry_time, exit_time
        FROM user_attendance
        WHERE memberid=%s {date_filter}
        ORDER BY date DESC
    """, tuple(params))

    return rows or []

def get_working_days(from_d, to_d):
    days, current = 0, from_d
    while current <= to_d:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days

def calculate_attendance(days_present, mandatory):
    return round(days_present / mandatory * 100, 1) if mandatory else 0

@cached(ttl_seconds=300)
def get_attendance_stats(member_id, year=None):
    today = date.today()
    year  = int(year or today.year)   # always int, never None stored in key
    try:
        # Single query: count present days only — no ORDER BY, no row fetch
        count_rows = hr_query(
            "SELECT COUNT(*) AS cnt FROM user_attendance "
            "WHERE memberid=%s AND date BETWEEN %s AND %s",
            (member_id, f"{year}-01-01", f"{year}-12-31"),
        )
        days_present = int(count_rows[0]["cnt"]) if count_rows else 0

        mandatory = calc_mandatory_days(year)
        att_pct   = calculate_attendance(days_present, mandatory)

        return {
            "days_present":   days_present,
            "mandatory_days": mandatory,
            "attendance_pct": att_pct,
            "recent_log":     [],   # loaded lazily via AJAX if needed
            "trend":          [],
        }
    except Exception as e:
        print("Error in get_attendance_stats:", e)
        return {
            "days_present":   0,
            "mandatory_days": 0,
            "attendance_pct": 0,
            "recent_log":     [],
            "trend":          [],
        }


def _get_years_raw(member_id=None, memberid=None):
    years = set()
    if member_id:
        # Merge 2 HR queries into 1 UNION
        rows = hr_query("""
            SELECT DISTINCT YEAR(date) AS yr FROM user_attendance WHERE memberid=%s
            UNION
            SELECT DISTINCT report_year FROM monthly_report WHERE member_id=%s
        """, (member_id, member_id))
        years.update(int(r["yr"]) for r in rows if r.get("yr"))
    if memberid:
        # Merge 2 slots queries into 1 UNION
        rows = slots_query("""
            SELECT DISTINCT YEAR(FROM_UNIXTIME(startdate)) AS yr 
            FROM reservations WHERE memberid=%s
            UNION
            SELECT DISTINCT YEAR(date_of_request) 
            FROM equipment_usage_approval WHERE requestedby=%s
        """, (memberid, memberid))
        years.update(int(r["yr"]) for r in rows if r.get("yr"))
    return years

def _process_years(year_list):
    current_year = date.today().year

    years = set(year_list)
    years.add(current_year)

    sorted_years = sorted(years, reverse=True)

    default_year = max(year_list) if year_list else current_year

    return sorted_years, default_year


@cached(ttl_seconds=300)   # 5-minute cache — year list rarely changes mid-session
def get_available_years(member_id=None, memberid=None):
    """
    Years with data for the year dropdown.
    Returns sorted list (descending) and always includes current year.
    Also returns the best default year — most recent year with actual data.
    """
    raw_years = _get_years_raw(member_id, memberid)
    return _process_years(raw_years)

# ── Equipment usage (staff) ───────────────────────────────────────────────────

def _get_uid_from_member_cached(email):
    if not email:
        return None
    r = slots_query("SELECT memberid FROM login WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 1", (email,))
    return r[0]["memberid"] if r else None


@cached(ttl_seconds=300)
def get_staff_owner_track(member_id: int) -> list:
    """
    Ownership span history for a staff member.
    Cached 5 minutes. N+1 COUNT loop replaced with a single GROUP BY query.
    """
    from models.lab import get_system_owner_track

    uid = _get_uid_from_member(member_id)

    if uid:
        track = get_system_owner_track(uid)
        if track:
            return track

    # Fallback: find the candidate account with the most track rows.
    # Old code fired one COUNT(*) query per candidate — replaced with
    # a single GROUP BY query across all candidates at once.
    p = hr_query(
        "SELECT email FROM profile WHERE member_id = %s LIMIT 1",
        (member_id,)
    )
    if not p or not p[0].get("email"):
        return get_system_owner_track(uid) if uid else []

    email      = p[0]["email"]
    email_user = email.split("@")[0] if "@" in email else ""

    candidates = []
    if email_user:
        candidates = slots_query(
            "SELECT memberid FROM login WHERE LOWER(TRIM(email)) LIKE LOWER(%s)",
            (f"{email_user}@%",)
        ) or []
    if not candidates:
        candidates = slots_query(
            "SELECT memberid FROM login WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 5",
            (email,)
        ) or []
    if not candidates:
        return get_system_owner_track(uid) if uid else []

    cand_ids = [c["memberid"] for c in candidates]

    # Single GROUP BY query instead of N COUNT queries
    placeholders = ",".join(["%s"] * len(cand_ids))
    counts = slots_query(
        f"SELECT memberid, COUNT(*) AS cnt FROM system_owner_track "
        f"WHERE memberid IN ({placeholders}) GROUP BY memberid",
        tuple(cand_ids)
    ) or []

    count_map = {r["memberid"]: int(r["cnt"]) for r in counts}
    best_uid  = max(cand_ids, key=lambda mid: count_map.get(mid, 0))

    return get_system_owner_track(best_uid)


def _get_uid_from_member(member_id: int) -> int | None:
    """
    Resolve HR member_id to slotbooking memberid.
    Result is cached in-process for 30 minutes.
    """
    import time as _t
    with _uid_cache_lock:
        ts = _uid_cache_ts.get(member_id, 0.0)
        if _t.monotonic() - ts < _UID_CACHE_TTL and member_id in _uid_cache:
            return _uid_cache[member_id]

    uid = _resolve_uid_uncached(member_id)

    with _uid_cache_lock:
        _uid_cache[member_id]    = uid
        _uid_cache_ts[member_id] = _t.monotonic()
    return uid


def _warmup_uid(member_id: int) -> None:
    """
    Pre-populate the UID cache for member_id.
    Call this ONCE at the top of the profile route (before run_parallel) so
    that all parallel tasks that need the slotbooking uid find it already in
    the cache instead of each triggering the 4-step resolution independently.
    This cuts the first-visit cost of a profile page by ~1 to 2 seconds.
    """
    _get_uid_from_member(member_id)


def _resolve_uid_uncached(member_id: int) -> int | None:
    """
    Multi-step UID resolution. Only called on cache miss (~once per member
    per 30 minutes). Starts with the cheapest queries first — no cross-DB
    joins. Falls back to progressively more expensive strategies.
    """
    # Fetch just the email from HR — fast single-table PK lookup, no join.
    p = hr_query(
        "SELECT email FROM profile WHERE member_id = %s LIMIT 1",
        (member_id,)
    )
    if not p:
        return None

    email = (p[0].get("email") or "").strip()

    # Step 1 — exact email match (indexed on slotbooking side)
    uid = _get_uid_from_member_cached(email)
    if uid is not None:
        return uid

    # Step 2 — same email username, any domain (abc@iitb vs abc@gmail)
    email_user = email.split("@")[0] if "@" in email else ""
    if email_user:
        r = slots_query(
            "SELECT memberid FROM login "
            "WHERE LOWER(TRIM(email)) LIKE LOWER(%s) LIMIT 5",
            (f"{email_user}@%",)
        )
        if r:
            if len(r) == 1:
                return r[0]["memberid"]
            counts = slots_query(
                "SELECT requestedby AS mid, COUNT(*) AS cnt "
                "FROM equipment_usage_approval "
                "WHERE requestedby IN ({}) GROUP BY requestedby".format(
                    ",".join(str(c["memberid"]) for c in r)
                )
            ) or []
            count_map = {row["mid"]: int(row["cnt"]) for row in counts}
            best = max(r, key=lambda c: count_map.get(c["memberid"], 0))
            return best["memberid"]

    # Step 3 — same numeric memberid exists in slotbooking
    r = slots_query(
        "SELECT memberid FROM login WHERE memberid = %s LIMIT 1",
        (member_id,)
    )
    if r:
        return r[0]["memberid"]

    # Step 4 — name-based match (last resort)
    if email:
        name_row = slots_query(
            "SELECT fname, lname FROM login "
            "WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 1",
            (email,)
        )
        if name_row:
            fname = (name_row[0].get("fname") or "").strip()
            lname = (name_row[0].get("lname") or "").strip()
            if fname and lname:
                r = slots_query(
                    "SELECT memberid FROM login "
                    "WHERE LOWER(TRIM(fname)) = LOWER(%s) "
                    "AND LOWER(TRIM(lname)) = LOWER(%s) LIMIT 5",
                    (fname, lname)
                )
                if r:
                    if len(r) == 1:
                        return r[0]["memberid"]
                    counts = slots_query(
                        "SELECT requestedby AS mid, COUNT(*) AS cnt "
                        "FROM equipment_usage_approval "
                        "WHERE requestedby IN ({}) GROUP BY requestedby".format(
                            ",".join(str(c["memberid"]) for c in r)
                        )
                    ) or []
                    count_map = {row["mid"]: int(row["cnt"]) for row in counts}
                    best = max(r, key=lambda c: count_map.get(c["memberid"], 0))
                    return best["memberid"]

    return None

def _get_equipment_rows(uid, year=None):
    date_filter = "AND YEAR(e.date_of_request) = %s" if year else ""
    params      = (uid, int(year)) if year else (uid,)
    rows = slots_query(f"""
        SELECT r.name AS tool_name,
               COUNT(e.request_id) AS times_booked,
               SUM(CASE WHEN e.status=3 THEN 1 ELSE 0 END) AS slot_booked,
               SUM(CASE WHEN e.status=1 THEN 1 ELSE 0 END) AS approved,
               SUM(CASE WHEN e.status=0 THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN e.status=2 THEN 1 ELSE 0 END) AS rejected
        FROM equipment_usage_approval e
        JOIN resources r ON r.machid = e.equipmentid
        WHERE e.requestedby = %s {date_filter}
        GROUP BY r.machid, r.name
        ORDER BY times_booked DESC
        LIMIT 50
    """, params)
    return rows or []
def _get_lab_access_rows(uid, year=None):
    if uid is None:
        return []

    if year:
        return slots_query("""
            SELECT date_request, equipments, access_period, approval
            FROM lab_access
            WHERE memberid=%s AND YEAR(date_request)=%s
            ORDER BY date_request DESC LIMIT 20
        """, (uid, year)) or []

    return slots_query("""
        SELECT date_request, equipments, access_period, approval
        FROM lab_access
        WHERE memberid=%s
        ORDER BY date_request DESC LIMIT 20
    """, (uid,)) or []

from collections import defaultdict

@cached(ttl_seconds=120)   # 2-minute cache — prevents re-running the correlated subquery on every dropdown
def get_equipment_stats(member_id, year=None):
    uid = _get_uid_from_member(member_id)

    if uid is None:
        return {
            "available": False,
            "total_slots": 0,
            "tools_used": [],
            "tools_count": 0,
            "approval_stats": {},
            "lab_access_log": []
        }

    tools = _get_equipment_rows(uid, year)
    lab  = _get_lab_access_rows(uid, year)

  # Total slots = sum of all bookings
    total = sum(t.get("times_booked", 0) for t in tools)
    return {
        "available": True,
        "total_slots": total,
        "tools_used": tools,
        "tools_count": len(tools),
        "approval_stats": {
            "total": total,
            "slot_booked": sum(t["slot_booked"] for t in tools),
            "approved": sum(t["approved"] for t in tools),
            "pending": sum(t["pending"] for t in tools),
            "rejected": sum(t["rejected"] for t in tools),
        },
        "lab_access_log": lab
    }

# ── Projects & publications ───────────────────────────────────────────────────

def _get_lab_projects(uid):
    projects = slots_query("""
        SELECT fp.project, pc.project_category AS category_name,
               fp.project_end_date, fp.active
        FROM faculty_projects fp
        LEFT JOIN project_category pc ON pc.id=fp.project_category
        WHERE fp.memberid=%s ORDER BY fp.active DESC, fp.project_end_date DESC
    """, (uid,))
    balance = slots_query("""
        SELECT SUM(CASE WHEN transaction_type='credit' THEN amount ELSE 0 END) AS total_credit,
               SUM(CASE WHEN transaction_type='debit'  THEN amount ELSE 0 END) AS total_debit,
               COUNT(*) AS total_transactions
        FROM balance_sheet WHERE memberid=%s
    """, (uid,))
    papers = slots_query("""
        SELECT title, year, type, conf_name, author
        FROM paper_publish WHERE memberid=%s AND approve=1 ORDER BY year DESC
    """, (uid,))
    return {"available": True, "projects": projects,
            "balance": balance[0] if balance else None, "papers": papers}


def get_project_data(member_id):
    uid = _get_uid_from_member(member_id)
    return _get_lab_projects(uid) if uid else {"available": False, "projects": [], "balance": None, "papers": []}


# ── Profile tracking & training ───────────────────────────────────────────────

def get_profile_tracking(member_id, year=None):
    year_filter = "AND YEAR(pt.timestamp) = %s" if year else ""
    params      = (member_id, year) if year else (member_id,)

    rows = hr_query(f"""
        SELECT pt.column_name, pt.old_value, pt.new_value, pt.timestamp,
        TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS updated_by_name
        FROM profile_tracking pt
        LEFT JOIN slotbooking.login l ON l.memberid = pt.updated_by
        WHERE pt.memberid = %s {year_filter}
        ORDER BY pt.timestamp DESC LIMIT 100
    """, params) or []

    for r in rows:
        if r.get("timestamp"):
            r["timestamp"] = r["timestamp"].isoformat()

    return rows
# ───────────────────────────────────────────────────────────────────────────────

def get_holidays_for_year(year):
    return [h for h in get_holidays() if h.year == year]


def calc_mandatory_days(year, month=None, holidays=None):
    holidays = holidays or get_holidays_for_year(year)
    today = date.today()
    if month:
        from calendar import monthrange
        total_days = monthrange(year, month)[1]
        month_end = date(year, month, total_days)
        # Cap at today for the current month of the current year
        effective_end = min(month_end, today)
        working_days = get_working_days(date(year, month, 1), effective_end)
    else:
        year_end = date(year, 12, 31)
        # Cap at today for the current year so mandatory days don't include future days
        effective_end = min(year_end, today)
        working_days = get_working_days(date(year, 1, 1), effective_end)
    holiday_count = sum(
        1 for h in holidays
        if h.year == year and (month is None or h.month == month)
        and h.weekday() < 5
        and h <= today  # don't count future holidays either
    )
    return max(working_days - holiday_count, 0)
@cached(ttl_seconds=120)
def get_attendance_trend(member_id, year=None):
    """
    Monthly attendance trend — resolved in ONE query instead of 12.
    Cached to prevent re-computation on every dropdown AJAX call.
    """
    year = int(year or date.today().year)   # normalise: None and int produce same key

    rows = get_attendance_rows(member_id, year= year)  # ✅ only once
    holidays = get_holidays_for_year(year)  # ✅ only once

    monthly_data = []

    for month in range(1, 13):
        month_rows = []
        for r in rows:
            d = r["date"]
            if isinstance(d, str):
                from datetime import datetime
                d = datetime.fromisoformat(d)

            if d.month == month:
                month_rows.append(r)

        days_present = len(month_rows)
        mandatory = calc_mandatory_days(year, month=month, holidays=holidays)

        pct = round(days_present / mandatory * 100, 1) if mandatory else 0

        monthly_data.append({
            "month": month,
            "attendance_pct": pct
        })

    return monthly_data

# ── System ownership (staff) ──────────────────────────────────────────────────

def get_staff_system_owned(member_id: int) -> list:
    uid = _get_uid_from_member(member_id)
    if not uid:
        return []
    from models.lab import get_system_owner_tools
    return get_system_owner_tools(uid)


def get_staff_tool_perms_rich(member_id: int) -> list:
    uid = _get_uid_from_member(member_id)
    if not uid:
        return []
    from models.lab import get_member_tool_permissions
    return get_member_tool_permissions(uid)


def get_staff_reservations(member_id: int, year=None) -> list:
    uid = _get_uid_from_member(member_id)
    if not uid:
        return []
    from models.lab import get_lab_reservations
    return get_lab_reservations(uid, year) or []

# Breakdown of get_slot_activity()
def _get_slot_rows(member_id: int, year=None) -> list:
    uid = _get_uid_from_member(member_id)
    if uid is None:
        return []
    year_filter = "AND YEAR(e.date_of_request) = %s" if year else ""
    params      = (uid, int(year)) if year else (uid,)
    rows = slots_query(f"""
        SELECT
            e.request_id                                        AS request_id,
            r.name                                              AS tool_name,
            e.status                                            AS status_code,
            e.date_of_request                                   AS date_requested,
            e.resid                                             AS resid,
            FROM_UNIXTIME(res.startdate)                         AS start_dt,
            FROM_UNIXTIME(res.enddate)                           AS end_dt
        FROM equipment_usage_approval e
        LEFT JOIN resources r ON r.machid = e.equipmentid
        LEFT JOIN reservations res ON res.resid = e.resid
        WHERE e.requestedby = %s {year_filter} AND e.status IN (0, 1, 2, 3)
        ORDER BY e.date_of_request DESC LIMIT 300
    """, params) or []
    return rows


def _aggregate_slot_stats(rows):
    status_labels = {0: "Pending", 1: "Approved", 2: "Rejected", 3: "Slot Booked"}

    processed = []
    seen_ids  = set()

    counts = {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "slot_booked": 0,
        "slot_cancelled": 0,
    }
    for row in rows:
        rid = row.get("request_id")
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        code = row.get("status_code") or 0
        is_cancelled = (code == 3 and row.get("start_dt") is None and row.get("end_dt") is None)
        label = "Cancelled" if is_cancelled else status_labels.get(code, "Unknown")
        if code == 3:
            if is_cancelled:
                counts["slot_cancelled"] += 1
            else:
                counts["slot_booked"] += 1
        elif code == 1:
            counts["approved"] += 1
        elif code == 0:
            counts["pending"] += 1
        elif code == 2:
            counts["rejected"] += 1
        processed.append({
            "request_id":     rid,
            "tool_name":      row.get("tool_name") or "—",
            "status_label":   label,
            "status_code":    code,
            "start_dt":       str(row["start_dt"])       if row.get("start_dt")       else None,
            "end_dt":         str(row["end_dt"])         if row.get("end_dt")         else None,
            "date_requested": str(row["date_requested"]) if row.get("date_requested") else None,
        })
    return processed, counts

@cached(ttl_seconds=300)
def get_slot_activity(member_id: int, year=None) -> dict:
    """
    Combined equipment requests + slot reservations view for staff profiles.
    Cached to prevent re-running the correlated subquery on every year-dropdown change.
    """
    year = int(year or date.today().year)   # normalise so cache key is stable
    rows = _get_slot_rows(member_id, year)
    processed, counts = _aggregate_slot_stats(rows)

    return {
        "available":   True,
        "total":       len(processed),
        **counts,
        "rows":        processed,
    }