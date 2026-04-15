"""
models/dashboard.py — System health, expiry alerts, and dashboard-level queries.
"""
import calendar as cal
from datetime import date, timedelta, datetime
from db import hr_query, slots_query
from utils import calc_mandatory_days, get_display_name, run_parallel

#(ttl_seconds=1800)
def get_system_health(year=None):
    today    = date.today()
    year     = 2026              # locked to 2026 — ignore any year parameter
    yr_start = date(year, 1, 1)
    yr_end   = date(year, 12, 31) if year < today.year else today

    def safe_count(fn, q, p=None):
        try:
            r = fn(q, p) if p else fn(q)
            return int(r[0]["cnt"]) if r and r[0] and r[0]["cnt"] else 0
        except Exception:
            return 0

    counts = run_parallel({
        # Match get_all_members() exactly — no email filter
        "total_staff":     lambda: safe_count(hr_query,
            "SELECT COUNT(*) AS cnt FROM profile WHERE "
            "NOT ((email IS NULL OR email = '') AND (designation IS NULL OR designation = '') AND (team IS NULL OR team = '')) "
            "AND (leaving_date IS NULL OR leaving_date = '0000-00-00' OR leaving_date >= CURDATE()) "
            "AND (taken_clearance IS NULL OR taken_clearance = 0)"),
        # Match get_all_lab_users() exactly — excludes faculty positions
        "total_lab":       lambda: safe_count(slots_query,
            "SELECT COUNT(*) AS cnt FROM login WHERE "
            "position NOT IN ('Faculty','IITBNF Staff','Institute Facility','NCPRE Academic','Project Staff') "
            "AND (expiry_date IS NULL OR expiry_date = '' "
            "OR expiry_date = '0000-00-00' OR STR_TO_DATE(expiry_date, '%%m/%%d/%%Y') >= CURDATE())"),
        # Publications — 2026 only (will show 0 until papers are logged this year)
        "total_papers":    lambda: safe_count(slots_query,
            "SELECT COUNT(*) AS cnt FROM paper_publish WHERE approve=1 AND year = %s", year),
        "total_tools":     lambda: safe_count(slots_query, "SELECT COUNT(*) AS cnt FROM resources"),
        "active_projects": lambda: safe_count(slots_query,
            "SELECT COUNT(*) AS cnt FROM faculty_projects WHERE active=1 "
            "AND (YEAR(project_end_date) >= %s OR project_end_date IS NULL OR project_end_date = '0000-00-00')", year),
    })

    try:
        mandatory = calc_mandatory_days(year)
        att_rows  = hr_query(
            "SELECT COUNT(*) AS days FROM user_attendance WHERE date BETWEEN %s AND %s GROUP BY memberid HAVING days > 0",
            (yr_start, yr_end))
        avg_pct = round(sum(float(r["days"]) for r in att_rows) / len(att_rows) / mandatory * 100, 1) if att_rows and mandatory else 0
    except Exception:
        avg_pct = 0

    try:
        positions = slots_query("""
            SELECT position, COUNT(*) AS cnt FROM login
            WHERE position IS NOT NULL AND position != ''
            AND (expiry_date IS NULL OR expiry_date = '' OR expiry_date = '0000-00-00'
                 OR STR_TO_DATE(expiry_date, '%%m/%%d/%%Y') >= CURDATE())
            GROUP BY position ORDER BY cnt DESC
        """)
    except Exception:
        positions = []

    months = []
    if year < today.year:
        month_range = range(1, 12)
        def get_bounds(m):
            return date(year, m, 1), date(year, m, cal.monthrange(year, m)[1])
    else:
        month_range = range(4, -1, -1)
        def get_bounds(i):
            m = today.month - i
            y = today.year
            if m <= 0: m += 12; y -= 1
            return date(y, m, 1), date(y, m, cal.monthrange(y, m)[1])

    for i in month_range:
        try:
            start, end = get_bounds(i)
            rows = hr_query(
                "SELECT COUNT(DISTINCT memberid) AS active FROM user_attendance WHERE date BETWEEN %s AND %s",
                (start, end))
            label = start.strftime("%b %Y" if year < today.year else "%b")
            months.append({"month": label, "active": int(rows[0]["active"]) if rows else 0})
        except Exception:
            months.append({"month": "—", "active": 0})

    return {
        "total_staff":     counts.get("total_staff",     0),
        "total_lab":       counts.get("total_lab",       0),
        "avg_attendance":  avg_pct,
        "active_projects": counts.get("active_projects", 0),
        "total_papers":    counts.get("total_papers",    0),
        "total_tools":     counts.get("total_tools",     0),
        "positions":       positions,
        "monthly_active":  months,
        "selected_year":   year,
    }


#(ttl_seconds=1800)
def get_expiry_alerts(days_ahead=60):
    today  = date.today()
    cutoff = today + timedelta(days=days_ahead)
    alerts = []

    staff_rows = hr_query("""
        SELECT member_id, designation, team, email, leaving_date FROM profile
        WHERE leaving_date IS NOT NULL AND leaving_date != '0000-00-00'
          AND leaving_date BETWEEN %s AND %s ORDER BY leaving_date
    """, (today, cutoff))
    for r in (staff_rows or []):
        days_left = (r["leaving_date"] - today).days
        alerts.append({
            "type": "staff", "level": "critical" if days_left <= 14 else "warning",
            "name": get_display_name(r["member_id"], r.get("email", "")),
            "detail": r.get("designation") or "Staff",
            "date": str(r["leaving_date"]), "days_left": days_left,
            "link": f"/profile/{r['member_id']}",
        })

    lab_rows = slots_query("""
        SELECT memberid, fname, lname, position, expiry_date FROM login
        WHERE expiry_date IS NOT NULL
          AND STR_TO_DATE(expiry_date, '%%m/%%d/%%Y') BETWEEN %s AND %s
        ORDER BY STR_TO_DATE(expiry_date, '%%m/%%d/%%Y') LIMIT 20
    """, (today, cutoff))
    for r in (lab_rows or []):
        try:
            exp = datetime.strptime(r["expiry_date"], "%m/%d/%Y").date()
            days_left = (exp - today).days
            alerts.append({
                "type": "lab", "level": "critical" if days_left <= 14 else "warning",
                "name": f"{r['fname']} {r['lname']}".strip(),
                "detail": r.get("position") or "Lab User",
                "date": str(exp), "days_left": days_left,
                "link": f"/lab/{r['memberid']}",
            })
        except Exception:
            pass

    alerts.sort(key=lambda x: x["days_left"])
    return alerts
