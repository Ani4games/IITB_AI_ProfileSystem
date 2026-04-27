"""
models/lab.py — All data queries for lab users (slotbooking) profiles.
"""
from datetime import datetime

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
def get_lab_user(memberid):
    rows = slots_query("""
        SELECT l.memberid, l.email, l.fname, l.lname, l.position, l.is_admin,
               l.rollno, l.department, l.supervisor, l.research_area,
               l.expiry_date, l.mobile, l.project_first,
               TRIM(CONCAT(COALESCE(s.fname,''), ' ', COALESCE(s.lname,''))) AS supervisor_name
        FROM login l
        LEFT JOIN login s ON s.memberid = l.supervisor
        LEFT JOIN hr_portal.profile p ON p.member_id = l.memberid
        WHERE l.memberid = %s
          AND (p.member_id IS NULL OR p.leaving_date IS NULL
               OR p.leaving_date = '0000-00-00'
               OR p.leaving_date >= '2026-01-01')
          AND (l.expiry_date IS NULL OR l.expiry_date = '' OR l.expiry_date = '0000-00-00'
               OR (STR_TO_DATE(l.expiry_date, '%%m/%%d/%%Y') IS NOT NULL
                   AND STR_TO_DATE(l.expiry_date, '%%m/%%d/%%Y') >= '2026-01-01'))
        LIMIT 1
    """, (memberid,))
    return rows[0] if rows else None

@cached(ttl_seconds=300)
def get_all_lab_users():
    # Remove the STR_TO_DATE filter — filter in Python instead
    rows = slots_query("""
        SELECT l.memberid, l.email, l.fname, l.lname, l.position, l.department,
               l.expiry_date, l.is_admin
        FROM login l
        LEFT JOIN hr_portal.profile p ON p.member_id = l.memberid
        WHERE (p.member_id IS NULL 
               OR p.leaving_date IS NULL 
               OR p.leaving_date = '0000-00-00'
               OR p.leaving_date >= CURDATE())
        ORDER BY l.position, l.fname, l.lname
    """) or []
    
    # Filter expired users in Python — no per-row function call in SQL
    from datetime import datetime
    today = datetime.today()
    cutoff = datetime(2026, 1, 1)  # or use today
    
    active = []
    for u in rows:
        exp = u.get("expiry_date") or ""
        if not exp or exp in ("", "0000-00-00"):
            active.append(u)
            continue
        try:
            exp_dt = datetime.strptime(exp, "%m/%d/%Y")
            if exp_dt >= cutoff:
                active.append(u)
        except:
            active.append(u)  # unparseable = keep
    return active

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
# ── System owner track ────────────────────────────────────────────────────────
def get_system_owner_track(memberid: int) -> list:
    """
    Correct lifecycle pairing for system owner tracking.
    Handles multiple create/delete cycles per tool.
    """

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

    from collections import defaultdict
    from datetime import datetime
    def days_between(start, end):
        if not start or not end:
            return None
        s = datetime.strptime(start, '%d-%m-%Y')
        e = datetime.strptime(end, '%d-%m-%Y')
        return (e - s).days
    
    tool_map = defaultdict(list)
    
    def fmt(ts):
        return datetime.fromtimestamp(ts).strftime('%d-%m-%Y') if ts else None
    
    # Step 1: group by tool
    for r in rows:
        if not r.get("date"):
            continue  # skip bad rows

        tool_map[r["deviceid"]].append(r)

    result = []

    # Step 2: process each tool timeline
    for deviceid, events in tool_map.items():
        current = None

        for e in events:
            action = (e.get("action") or "").lower().strip()
            ts = e.get("date")

            if action == "create":
                # Start new ownership cycle
                current = {
                    "tool_name": e.get("tool_name"),
                    "category": e.get("category"),
                    "owned_since": fmt(ts),
                    "removed_on": None,
                    "is_active": True
                }

            elif action == "delete":
                if current:
                    # Close current cycle
                    current["removed_on"] = fmt(ts)
                    current["is_active"] = False
                    duration = days_between(current["owned_since"], current["removed_on"])
                    current["duration_days"] = duration if duration is not None else "—"
                    result.append(current)
                    current = None
                else:
                    # Edge case: delete without create
                    result.append({
                        "tool_name": e.get("tool_name"),
                        "category": e.get("category"),
                        "owned_since": None,
                        "removed_on": fmt(ts),
                        "is_active": False
                    })

        # If still active after last event
        if current:
            duration = days_between(current["owned_since"], current["removed_on"])
            current["duration_days"] = duration if duration is not None else "—"
            result.append(current)

    return result
