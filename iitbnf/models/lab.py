"""
models/lab.py — All data queries for lab users (slotbooking) profiles.
"""
from datetime import datetime
import time
from db import slots_query
from utils import run_parallel
from cache import cached
def safe_json(obj):
    from datetime import timedelta, datetime, date

    if isinstance(obj, dict):
        return {k: safe_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [safe_json(v) for v in obj]
    elif isinstance(obj, timedelta):
        return obj.days
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj
@cached(ttl_seconds=300)
def get_lab_user(memberid):
    """
    Single lab user record by memberid.  Cached 5 minutes — the profile
    page and any background PDF job for the same user share this result
    without a second round-trip.

    Expiry filtering is done in SQL using CURDATE() so the query is always
    correct regardless of when the server process was started.
    """
    rows = slots_query("""
        SELECT l.memberid, l.email, l.fname, l.lname, l.position, l.is_admin,
               l.rollno, l.department, l.supervisor, l.research_area,
               l.expiry_date, l.mobile, l.project_first,
               TRIM(CONCAT(COALESCE(s.fname,''), ' ', COALESCE(s.lname,''))) AS supervisor_name
        FROM login l
        LEFT JOIN login s ON s.memberid = l.supervisor
        LEFT JOIN hr_portal.profile p ON p.member_id = l.memberid
        WHERE l.memberid = %s
          AND (p.member_id IS NULL
               OR p.leaving_date IS NULL
               OR p.leaving_date = '0000-00-00'
               OR p.leaving_date >= CURDATE())
          AND (l.expiry_date IS NULL
               OR l.expiry_date = ''
               OR l.expiry_date = '0000-00-00'
               OR STR_TO_DATE(l.expiry_date, '%%m/%%d/%%Y') >= CURDATE())
        LIMIT 1
    """, (memberid,))
    print(f"get_lab_user: fetched {len(rows)} records for memberid={memberid}")
    print(f"Sample record: {rows[0] if rows else 'N/A'}")
    return rows[0] if rows else None

@cached(ttl_seconds=3600)
def get_all_lab_users():
    """
    All active lab users for the admin panel search index.
    Cached 1 hour.

    Active = not expired per slotbooking.login.expiry_date.
    We do NOT cross-reference hr_portal departed staff because memberids
    are not guaranteed unique across both databases — a departed staff ID
    could falsely match an active PhD/MTech/BTech/INUP lab user.
    """
    # Step 1: fetch only non-expired slotbooking users directly in SQL.
    # STR_TO_DATE on expiry_date is unavoidable (stored as MM/DD/YYYY text)
    # but we scope it to rows where expiry_date is non-null/non-empty first,
    # which lets the engine skip the conversion for the majority of rows.
    rows = slots_query("""
        SELECT memberid, email, fname, lname, position, department,
               expiry_date, is_admin
        FROM login
        WHERE expiry_date IS NULL
           OR expiry_date = ''
           OR expiry_date = '0000-00-00'
           OR STR_TO_DATE(expiry_date, '%%m/%%d/%%Y') >= CURDATE()
        ORDER BY fname, lname
    """) or []

    # The SQL query above already filters out expired lab users via expiry_date.
    # We do NOT cross-reference hr_portal.departed members here because:
    #   1. Lab users (Ph.D, M.Tech, B.Tech, INUP, etc.) are managed solely
    #      by slotbooking.login — their expiry_date is the authoritative signal.
    #   2. Numeric memberids are not guaranteed to be unique across both DBs,
    #      so a departed HR staff member's member_id could falsely match an
    #      active lab user's memberid, causing them to disappear from search.
    # print("DEBUG START")

    # print(slots_query("SELECT DATABASE()"))
    # print(slots_query("SELECT COUNT(*) FROM login"))

    # rows = slots_query("""
    #     SELECT memberid FROM login LIMIT 5
    # """)
    # print("Sample rows:", rows)

    # rows = slots_query("""SELECT memberid, email, fname, lname, position, department,
    #            expiry_date, is_admin
    #     FROM login
    #     WHERE expiry_date IS NULL
    #        OR expiry_date = ''
    #        OR expiry_date = '0000-00-00'
    #        OR STR_TO_DATE(expiry_date, '%%m/%%d/%%Y') >= CURDATE()
    #     ORDER BY fname, lname  """)
    # print("Final rows:", len(rows) if rows else rows)

    # print("DEBUG END")
    return rows

@cached(ttl_seconds=300)
def _get_lab_projects(memberid):
    return slots_query("""
        SELECT projectid, project_code, project_title, funding_agency,
               start_date, end_date, status
        FROM faculty_projects WHERE memberid = %s ORDER BY start_date DESC
    """, (memberid,)) or []

def get_lab_reservations(memberid, year=None):
    year_filter = "AND YEAR(FROM_UNIXTIME(res.startdate)) = %s" if year else ""
    params = (memberid, year) if year else (memberid,)
    return slots_query(f"""
        SELECT res.resid,
               FROM_UNIXTIME(res.startdate) AS start_dt,
               FROM_UNIXTIME(res.enddate)   AS end_dt,
               r.name AS tool_name, res.summary, res.project,
               CASE
                 WHEN res.activation_status=2 AND res.isblackout=1 THEN 'Completed'
                 WHEN res.activation_status=1 AND res.isblackout=1 THEN 'Upcoming'
                 WHEN res.activation_status=0 AND res.isblackout=1 THEN 'Active'
               END AS booking_status
        FROM reservations res
        JOIN resources r ON r.machid = res.machid
        WHERE res.memberid = %s
        AND res.isblackout = 1
        AND res.activation_status IN (0, 1, 2)
          {year_filter}
        ORDER BY res.startdate DESC LIMIT 200
    """, params)

def get_lab_equipment_requests(memberid, year=None):
    year_filter = "AND YEAR(e.date_of_request) = %s" if year else ""
    params = (memberid, year) if year else (memberid,)
    return slots_query(f"""
        SELECT e.request_id, r.name AS tool_name, e.requesttype,
               e.substrate, e.date_of_request, e.date_of_approval,
               e.status, e.project_code, e.comment
        FROM equipment_usage_approval e
        JOIN resources r ON r.machid = e.equipmentid
        WHERE e.requestedby=%s {year_filter}
        ORDER BY e.date_of_request DESC
        LIMIT 300
    """, params)

def get_lab_access_log(memberid, year=None):
    year_filter = "AND YEAR(date_request) = %s" if year else ""
    params = (memberid, year) if year else (memberid,)
    return slots_query(f"""
        SELECT date_request AS access_date, equipments, access_period, approval
        FROM lab_access WHERE memberid=%s {year_filter}
        ORDER BY date_request DESC LIMIT 100
    """, params)

#(ttl_seconds=1800)
@cached(ttl_seconds=300)
def get_lab_stats(memberid):
    def cnt(q, p):
        r = slots_query(q, p)
        return int(r[0]["cnt"]) if r and r[0] and r[0]["cnt"] else 0

    return run_parallel({
        "reservations": lambda: cnt("SELECT COUNT(*) AS cnt FROM reservations WHERE memberid=%s", (memberid,)),
        "requests":     lambda: cnt("SELECT COUNT(*) AS cnt FROM equipment_usage_approval WHERE requestedby=%s", (memberid,)),
        "papers":       lambda: cnt("SELECT COUNT(*) AS cnt FROM paper_publish WHERE memberid=%s AND approve=1", (memberid,)),
        "projects":     lambda: cnt("SELECT COUNT(*) AS cnt FROM faculty_projects WHERE memberid=%s", (memberid,)),
    })

def get_announcements():
    import time as _time
    now = int(_time.time())
    return slots_query("""
        SELECT announcementid, announcement, start_datetime, end_datetime
        FROM announcements WHERE start_datetime <= %s AND end_datetime >= %s
        ORDER BY announcementid DESC
    """, (now, now)) or []

def get_announcements_all():
    return slots_query("""
        SELECT announcementid, announcement, start_datetime, end_datetime
        FROM announcements ORDER BY announcementid DESC
    """) or []
@cached(ttl_seconds=300)
def get_lab_cancellations(memberid):
    return slots_query("""
        SELECT c.resid,
               r.name AS tool_name,
               FROM_UNIXTIME(c.startdate) AS start_dt,
               FROM_UNIXTIME(c.enddate)   AS end_dt,
               c.reason,
               c.cancel_time
        FROM cancel_reservation c
        LEFT JOIN resources r ON r.machid = c.machid
        WHERE c.memberid = %s
        ORDER BY c.cancel_time DESC
    """, (memberid,))

def get_lab_errors(memberid):
    return slots_query("""
        SELECT e.machid, e.resid,
               r.name AS tool_name,
               e.error_details,
               e.action_taken,
               e.status,
               e.timestamp,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS resolved_by
        FROM error_reporting e
        LEFT JOIN resources r ON r.machid = e.machid
        LEFT JOIN login l ON l.memberid = e.action_taken_by
        WHERE e.memberid = %s
        ORDER BY e.status ASC, e.timestamp DESC
    """, (memberid,))

def get_lab_registration(memberid):
    """Registration details with cosupervisor name resolution."""
    rows = slots_query("""
        SELECT r.course, r.project_first, r.project_second,
               r.status, r.date as reg_date,
               NULLIF(NULLIF(TRIM(r.cosupervisor), 'NA'), '') AS cosupervisor_raw,
               TRIM(CONCAT(COALESCE(co.fname,''), ' ', COALESCE(co.lname,''))) AS cosupervisor_name
        FROM registration r
        LEFT JOIN login co ON co.memberid = CAST(r.cosupervisor AS UNSIGNED)
        WHERE r.memberid = %s LIMIT 1
    """, (memberid,))
    return rows[0] if rows else None

def get_session_reports(memberid):
    """Equipment session reports submitted by a lab user after usage."""
    return slots_query("""
        SELECT rp.resid,
               r.name AS tool_name,
               rp.report_details,
               FROM_UNIXTIME(rp.datetime) AS submitted_at
        FROM reporting rp
        LEFT JOIN resources r ON r.machid = rp.machid
        WHERE rp.memberid = %s
        ORDER BY rp.datetime DESC
        LIMIT 100
    """, (memberid,)) or []

# ── Faculty position constant ─────────────────────────────────────────────────
FACULTY_POSITIONS = (
    'Faculty', 'IITBNF Staff', 'Institute Facility',
    'NCPRE Academic', 'Project Staff'
)

def is_faculty(memberid) -> bool:
    """Returns True if this member holds a faculty-type position."""
    row = slots_query(
        "SELECT position FROM login WHERE memberid = %s LIMIT 1",
        (memberid,)
    )
    if not row:
        return False
    return (row[0].get("position") or "") in FACULTY_POSITIONS

# ── Resources / Equipment detail ──────────────────────────────────────────────

def get_member_tool_permissions(memberid: int) -> list:
    """
    Tool permissions for a member — enriched with resource details
    including operator names and faculty incharge.
    """
    return slots_query("""
        SELECT r.machid, r.name AS tool_name, 
       r.operator_name1,
       r.operator_name2,
       TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS faculty_name,
       DATE_FORMAT(STR_TO_DATE(p.date, '%%m/%%d/%%Y'), '%%d-%%m-%%Y') AS permission_date
FROM permissions p
JOIN resources r    ON r.machid = p.machid
LEFT JOIN login l   ON l.memberid = r.faculty_incharge
WHERE p.memberid = %s
ORDER BY r.name
    """, (memberid,)) or []

# ── System owner ──────────────────────────────────────────────────────────────

from datetime import datetime

def get_system_owner_tools(memberid: int) -> list:
    """
    Tools for which this member is listed as system owner.
    system_owner.machid is a comma-separated string of machids.
    """
    rows = slots_query(
        "SELECT machid, date FROM system_owner WHERE memberid = %s",
        (memberid,)
    ) or []

    # Collect ALL machids from comma-separated strings in one pass
    all_ids = []
    date_map = {}
    for row in rows:
        raw = str(row.get("machid") or "")
        ids = [i.strip() for i in raw.split(",") if i.strip().isdigit()]
        for mid in ids:
            all_ids.append(int(mid))
            date_map[int(mid)] = row.get("date")  # store date per machid

    if not all_ids:
        return []

    # ONE query instead of N queries
    placeholders = ",".join(["%s"] * len(all_ids))
    tools = slots_query(f"""
        SELECT machid, name, category, location, type_of_tool,
               operator_name, isworking
        FROM resources WHERE machid IN ({placeholders})
    """, tuple(all_ids)) or []

    results = []
    for t in tools:
        t = dict(t)
        raw_date = date_map.get(t["machid"])
        t["ownership_date"] = None
        if raw_date:
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y"):
                try:
                    t["ownership_date"] = datetime.strptime(
                        str(raw_date).strip(), fmt
                    ).strftime("%d-%m-%Y")
                    break
                except:
                    continue
        results.append(t)
    return results

# This is the detailed tool usage history for system owner tracking, showing create/delete events with dates and durations.
import time
from collections import defaultdict
from datetime import datetime

# This is the detailed tool usage history for system owner tracking, showing create/delete events with dates and durations.
@cached(ttl_seconds=300)
def get_system_owner_track(memberid: int) -> list:
    t0 = time.time()
    
    rows = slots_query("""
        SELECT
            t.deviceid,
            r.name AS tool_name,
            r.category,
            t.action,
            t.date
        FROM system_owner_track t
        LEFT JOIN resources r ON r.machid = t.deviceid
        WHERE t.memberid = %s
        ORDER BY t.deviceid, t.date ASC
    """, (memberid,)) or []
    
    t1 = time.time()
    print(f"[TIMING] SQL query: {(t1-t0)*1000:.2f}ms, rows={len(rows)}")
    
    # Step 1: Parse timestamps to date objects
    t2 = time.time()
    def to_date(ts):
        return datetime.fromtimestamp(ts).date() if ts else None
    
    tool_map = defaultdict(list)
    parsed_count = 0
    skipped_count = 0
    
    for r in rows:
        date_obj = to_date(r.get("date"))
        if not date_obj:
            skipped_count += 1
            continue
        parsed_count += 1
        r["_date_obj"] = date_obj
        tool_map[r["deviceid"]].append(r)
    
    t3 = time.time()
    print(f"[TIMING] Parse dates + group by tool: {(t3-t2)*1000:.2f}ms")
    print(f"        - Parsed: {parsed_count} rows, Skipped: {skipped_count} rows")
    print(f"        - Unique tools: {len(tool_map)}")
    
    # Step 2: Process timeline for each tool
    t4 = time.time()
    result = []
    
    for deviceid, events in tool_map.items():
        current = None
        
        for e in events:
            action = (e.get("action") or "").lower().strip()
            date_obj = e["_date_obj"]
            
            if action == "create":
                current = {
                    "tool_name": e.get("tool_name"),
                    "category": e.get("category"),
                    "owned_since": date_obj,
                    "removed_on": None,
                    "is_active": True
                }
            
            elif action == "delete":
                if current:
                    current["removed_on"] = date_obj
                    current["is_active"] = False
                    current["duration_days"] = (date_obj - current["owned_since"]).days
                    result.append(current)
                    current = None
                else:
                    result.append({
                        "tool_name": e.get("tool_name"),
                        "category": e.get("category"),
                        "owned_since": None,
                        "removed_on": date_obj,
                        "is_active": False,
                        "duration_days": "—",
                    })
        
        if current:
            current["duration_days"] = "—"  # Still active
            result.append(current)
    
    t5 = time.time()
    print(f"[TIMING] Process timeline logic: {(t5-t4)*1000:.2f}ms")
    print(f"        - Result entries before formatting: {len(result)}")
    
    # Step 3: Convert dates to strings
    t6 = time.time()
    for item in result:
        if item.get("owned_since"):
            item["owned_since"] = item["owned_since"].strftime('%d-%m-%Y')
        if item.get("removed_on"):
            item["removed_on"] = item["removed_on"].strftime('%d-%m-%Y')
    
    t7 = time.time()
    print(f"[TIMING] Format dates to strings: {(t7-t6)*1000:.2f}ms")
    
    # Total time
    print(f"[TIMING] TOTAL function time: {(t7-t0)*1000:.2f}ms")
    print("-" * 50)
    
    return result