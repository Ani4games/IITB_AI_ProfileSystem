"""
models/lab.py — All data queries for lab users (slotbooking) profiles.
"""
from db import slots_query
from cache import cached
from utils import run_parallel


def get_lab_user(memberid):
    rows = slots_query("""
        SELECT l.memberid, l.email, l.fname, l.lname, l.position, l.is_admin,
               l.rollno, l.department, l.supervisor, l.research_area,
               l.expiry_date, l.mobile, l.project_first,
               TRIM(CONCAT(COALESCE(s.fname,''), ' ', COALESCE(s.lname,''))) AS supervisor_name
        FROM login l
        LEFT JOIN login s ON s.memberid = l.supervisor
        WHERE l.memberid = %s LIMIT 1
    """, (memberid,))
    return rows[0] if rows else None


def get_all_lab_users():
    return slots_query("""
        SELECT memberid, email, fname, lname, position, department, expiry_date, is_admin
        FROM login
        WHERE (expiry_date IS NULL OR expiry_date = '' OR expiry_date = '0000-00-00'
               OR STR_TO_DATE(expiry_date, '%%m/%%d/%%Y') >= CURDATE())
        ORDER BY position, fname, lname
    """)


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
        ORDER BY e.date_of_request DESC LIMIT 200
    """, params)


def get_lab_access_log(memberid, year=None):
    year_filter = "AND YEAR(date_request) = %s" if year else ""
    params = (memberid, year) if year else (memberid,)
    return slots_query(f"""
        SELECT date_request AS access_date, equipments, access_period, approval
        FROM lab_access WHERE memberid=%s {year_filter}
        ORDER BY date_request DESC LIMIT 100
    """, params)


def get_lab_tool_permissions(memberid):
    return slots_query("""
        SELECT r.name AS tool_name, r.category, r.location, p.date AS permission_date
        FROM permissions p JOIN resources r ON r.machid=p.machid
        WHERE p.memberid=%s ORDER BY r.name
    """, (memberid,))


@cached(ttl_seconds=1800)
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


def get_training_report(memberid, year=None):
    year_filter = "AND YEAR(tr.trained_on) = %s" if year else ""
    params = (memberid, year) if year else (memberid,)
    return slots_query(f"""
        SELECT tr.run_type, tr.run_no, tr.req_on, tr.trained_on,
               tr.read_material, tr.comment, r.name AS tool_name,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS trainer_name
        FROM training_report tr
        JOIN resources r ON r.machid = tr.tool
        LEFT JOIN login l ON l.memberid = tr.trainedby
        WHERE tr.memberid = %s {year_filter}
        ORDER BY tr.trained_on DESC LIMIT 200
    """, params) or []


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
               r.tool_name,
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
               r.status, r.reg_date,
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