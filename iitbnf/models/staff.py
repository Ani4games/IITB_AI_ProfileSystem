"""
models/staff.py — All data queries for staff (hr_portal) profiles.
"""
from datetime import date, timedelta
from collections import defaultdict
from db import hr_query, slots_query
from utils import get_display_name, clean_role, calc_mandatory_days
from models.lab import get_system_owner_track
from cache import cached

# ── Member lists ──────────────────────────────────────────────────────────────

@cached(ttl_seconds=300)
def get_all_members():
    rows = hr_query("""
        SELECT p.member_id, p.designation, p.team, p.email,
               COALESCE(rm.role_name, 'Staff') AS raw_role,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS joined_name
        FROM profile p
        LEFT JOIN role r          ON r.memberid = p.member_id
        LEFT JOIN role_master rm  ON rm.role_id = r.role
        LEFT JOIN slotbooking.login l ON l.memberid = p.member_id
        WHERE NOT (
            (p.email IS NULL OR p.email = '') AND
            (p.designation IS NULL OR p.designation = '') AND
            (p.team IS NULL OR p.team = '')
        )
        AND (p.leaving_date IS NULL OR p.leaving_date = '0000-00-00' OR p.leaving_date >= '2025-01-01')
        AND (p.taken_clearance IS NULL OR p.taken_clearance = 0)
        ORDER BY p.member_id
    """)
    processed = []
    for m in (rows or []):
        joined = (m.get("joined_name") or "").strip()
        m["display_name"] = joined if joined else get_display_name(m["member_id"], m.get("email", ""))
        m["role_name"]    = clean_role(m.get("raw_role"))
        processed.append(m)
    return processed

def get_person(member_id):
    rows = hr_query("""
        SELECT p.*,
               COALESCE(rm.role_name, 'Staff') AS raw_role,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS joined_name,
               l.memberid AS slot_memberid,
               l.position AS slot_position,
               l.department AS slot_department
        FROM profile p
        LEFT JOIN role r          ON r.memberid = p.member_id
        LEFT JOIN role_master rm  ON rm.role_id = r.role
        LEFT JOIN slotbooking.login l ON LOWER(TRIM(l.email)) = LOWER(TRIM(p.email))
        WHERE p.member_id = %s
          AND (p.taken_clearance IS NULL OR p.taken_clearance = 0)
        LIMIT 1
    """, (member_id,))
    if not rows:
        return None
    p = rows[0]
    p["role_name"]    = clean_role(p.get("raw_role"))
    joined = (p.get("joined_name") or "").strip()
    p["display_name"] = joined if joined else get_display_name(p["member_id"], p.get("email", ""))
    return p


def get_permissions(member_id):
    return hr_query("SELECT field FROM user_permission WHERE memberid=%s", (member_id,))


# ── Attendance ────────────────────────────────────────────────────────────────

def get_attendance_stats(member_id, year=None):
    today    = date.today()
    year     = year or today.year

    all_rows = hr_query("""
        SELECT date, time AS entry_time, exit_time
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date)=%s
        ORDER BY date DESC
    """, (member_id, year))

    days_present = len(all_rows or [])
    mandatory    = calc_mandatory_days(year)
    att_pct      = round(days_present / mandatory * 100, 1) if mandatory else 0

    leave_rows = hr_query("""
        SELECT type_of_leave, from_date, to_date
        FROM leaves
        WHERE memberid=%s AND status=1 AND YEAR(from_date) = %s
    """, (member_id, year))

    def count_working_days(from_d, to_d):
        days, current = 0, from_d
        while current <= to_d:
            if current.weekday() < 5:
                days += 1
            current += timedelta(days=1)
        return days

    leave_totals = defaultdict(float)
    for lv in (leave_rows or []):
        try:
            from_d = lv["from_date"] if isinstance(lv["from_date"], date) else date.fromisoformat(str(lv["from_date"]))
            to_d   = lv["to_date"]   if isinstance(lv["to_date"],   date) else date.fromisoformat(str(lv["to_date"]))
            working = count_working_days(from_d, to_d)
            if lv["type_of_leave"] == "HCL":
                working *= 0.5
            leave_totals[lv["type_of_leave"]] += working
        except Exception:
            pass

    max_map = {r["type_of_leave"]: r["max_leaves"] for r in
               (hr_query("SELECT type_of_leave, max_leaves FROM max_leaves WHERE memberid=%s", (member_id,)) or [])}

    leave_summary, util_vals = [], []
    for leave_type, taken in sorted(leave_totals.items()):
        max_a = max_map.get(leave_type)
        util  = round(taken / max_a * 100, 1) if max_a else None
        if util is not None:
            util_vals.append(util)
        leave_summary.append({
            "type_of_leave": leave_type,
            "days_taken": taken,
            "max_allowed": max_a,
            "util_pct": util
        })

    # ✅ ADD TREND HERE
    trend = get_attendance_trend(member_id)

    return {
        "days_present":          days_present,
        "mandatory_days":        mandatory,
        "attendance_pct":        att_pct,
        "leave_summary":         leave_summary,
        "leave_utilisation_pct": round(sum(util_vals) / len(util_vals), 1) if util_vals else 0,
        "recent_log":            (all_rows or [])[:30],
        "trend":                 trend   # ✅ IMPORTANT FIX
    }

@cached(ttl_seconds=120)
def get_available_years(member_id=None, memberid=None):
    """
    Years with data for the year dropdown.
    Returns sorted list (descending) and always includes current year.
    Also returns the best default year — most recent year with actual data.
    """
    years = {date.today().year}
    data_years = set()  # years that actually have data

    if member_id:
        for r in (hr_query("SELECT DISTINCT YEAR(date) AS yr FROM user_attendance WHERE memberid=%s", (member_id,)) or []):
            if r.get("yr"):
                years.add(int(r["yr"]))
                data_years.add(int(r["yr"]))
        for r in (hr_query("SELECT DISTINCT report_year AS yr FROM monthly_report WHERE member_id=%s", (member_id,)) or []):
            if r.get("yr"):
                years.add(int(r["yr"]))
                data_years.add(int(r["yr"]))
    if memberid:
        for r in (slots_query("SELECT DISTINCT YEAR(FROM_UNIXTIME(startdate)) AS yr FROM reservations WHERE memberid=%s", (memberid,)) or []):
            if r.get("yr"):
                years.add(int(r["yr"]))
                data_years.add(int(r["yr"]))
        for r in (slots_query("SELECT DISTINCT YEAR(date_of_request) AS yr FROM equipment_usage_approval WHERE requestedby=%s", (memberid,)) or []):
            if r.get("yr"):
                years.add(int(r["yr"]))
                data_years.add(int(r["yr"]))

    sorted_years = sorted(years, reverse=True)
    return sorted_years, max(data_years) if data_years else date.today().year


# ── Monthly reports & committees ──────────────────────────────────────────────

def get_monthly_reports(member_id, year=None):
    if year:
        return hr_query("""
            SELECT report_year,report_month,status,star,comment,submitted_at
            FROM monthly_report WHERE member_id=%s AND report_year=%s
            ORDER BY FIELD(report_month,'January','February','March','April','May','June',
                'July','August','September','October','November','December')
        """, (member_id, year))
    return hr_query("""
        SELECT report_year,report_month,status,star,comment,submitted_at
        FROM monthly_report WHERE member_id=%s
        ORDER BY report_year DESC,
          FIELD(report_month,'January','February','March','April','May','June',
                'July','August','September','October','November','December')
    """, (member_id,))


def get_committee_involvement(member_id):
    p = hr_query("SELECT email FROM profile WHERE member_id=%s LIMIT 1", (member_id,))
    if not p or not p[0].get("email"):
        return []
    return hr_query("""
        SELECT c.name AS committee_name, c.description, cm.position
        FROM committee_members cm JOIN committees c ON c.id=cm.committee_id
        WHERE cm.email=%s
    """, (p[0]["email"],))


# ── Equipment usage (staff) ───────────────────────────────────────────────────

@cached(ttl_seconds=300)
def _get_uid_from_member_cached(email):
    if not email:
        return None
    r = slots_query("SELECT memberid FROM login WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 1", (email,))
    return r[0]["memberid"] if r else None

def get_staff_owner_track(member_id: int) -> list:
    """
    Ownership span history for a staff member.

    Resolution strategy
    ───────────────────
    1. Resolve HR member_id → slotbooking memberid via _get_uid_from_member.
    2. If that uid already has system_owner_track rows, return them directly.
    3. If the standard resolver returned a uid but it has zero track rows
       (possible when the person registered under a different email), search
       all slotbooking accounts that share the same email-username prefix and
       pick the one with the highest system_owner_track row count.  This
       tiebreaker is intentionally different from _get_uid_from_member, which
       uses reservation count — system owners may have few or zero reservations.
    4. Return [] only when no candidate account can be found at all.
    """
    from models.lab import get_system_owner_track

    uid = _get_uid_from_member(member_id)

    if uid:
        track = get_system_owner_track(uid)
        if track:
            return track

    # ── Fallback: pick the candidate with the most track rows ─────────────
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
        candidates = slots_query("""
            SELECT memberid FROM login
            WHERE LOWER(TRIM(email)) LIKE LOWER(%s)
        """, (f"{email_user}@%",)) or []

    if not candidates:
        candidates = slots_query("""
            SELECT memberid FROM login
            WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 5
        """, (email,)) or []

    if not candidates:
        return get_system_owner_track(uid) if uid else []

    best_uid, best_cnt = uid, 0
    for c in candidates:
        mid = c["memberid"]
        row = slots_query(
            "SELECT COUNT(*) AS cnt FROM system_owner_track WHERE memberid = %s",
            (mid,)
        )
        cnt = int(row[0]["cnt"]) if row else 0
        if cnt > best_cnt:
            best_cnt = cnt
            best_uid = mid

    return get_system_owner_track(best_uid) if best_uid else []


@cached(ttl_seconds=600)
def _get_uid_from_member(member_id):
    """
    Resolve HR member_id to slotbooking memberid.

    Strategy:
      1. Email match (exact, case-insensitive) — most reliable
      2. Name match fallback — handles cases where the person registered
         with a different email in slotbooking (e.g. gmail vs iitb.ac.in)
         Matches on fname + lname against the HR display name.
    """
    p = hr_query("""
        SELECT p.email,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS display_name,
               l.fname AS slot_fname, l.lname AS slot_lname
        FROM profile p
        LEFT JOIN slotbooking.login l ON LOWER(TRIM(l.email)) = LOWER(TRIM(p.email))
        WHERE p.member_id = %s LIMIT 1
    """, (member_id,))
    if not p:
        return None

    row   = p[0]
    email = row.get("email", "")

    # Step 1 — email match
    uid = _get_uid_from_member_cached(email)

    if uid is not None:
        return uid

    # Step 2 — name-based fallback
    # Get name directly from slotbooking by email domain variants,
    # or search all staff positions by name derived from the HR email prefix.
    # Since HR profile has no name fields, we search slotbooking by
    # the email username part (e.g. "anjum04" from "anjum04@gmail.com")
    # combined with position filter — then verify by checking reservations exist.

    # First try: search slotbooking for same email username with any domain
    # No position filter — staff may be registered under any position
    email_user = email.split("@")[0] if "@" in email else ""
    if email_user:
        r = slots_query("""
            SELECT memberid, fname, lname FROM login
            WHERE LOWER(TRIM(email)) LIKE LOWER(%s)
            LIMIT 5
        """, (f"{email_user}@%",))

        if r:
            # If only one result, use it directly
            if len(r) == 1:
                return r[0]["memberid"]
            # If multiple, pick the one with the most reservations (most likely correct)
            best_uid, best_cnt = None, -1
            for candidate in r:
                cnt_row = slots_query(
                    "SELECT COUNT(*) AS cnt FROM reservations WHERE memberid = %s",
                    (candidate["memberid"],)
                )
                cnt = cnt_row[0]["cnt"] if cnt_row else 0
                if cnt > best_cnt:
                    best_cnt = cnt
                    best_uid = candidate["memberid"]
            if best_uid:
                return best_uid

    # Step 3: Use the accidental memberid match in slotbooking to get the name
    # e.g. HR member_id=2457 → slotbooking has memberid=2457 as "Avinash Gangurde"
    # but their REAL slotbooking account is under a different memberid
    # So: get name from slotbooking[memberid=hr_member_id] → search all accounts with same name
    name_row = slots_query("""
        SELECT fname, lname FROM login
        WHERE memberid = %s LIMIT 1
    """, (member_id,))

    if name_row:
        fname = (name_row[0].get("fname") or "").strip()
        lname = (name_row[0].get("lname") or "").strip()
        if fname and lname:
            r = slots_query("""
                SELECT memberid FROM login
                WHERE LOWER(TRIM(fname)) = LOWER(%s)
                  AND LOWER(TRIM(lname)) = LOWER(%s)
                ORDER BY memberid DESC
                LIMIT 5
            """, (fname, lname))

            if r:
                if len(r) == 1:
                    return r[0]["memberid"]
                # Multiple matches — pick the one with most reservations
                best_uid, best_cnt = None, -1
                for candidate in r:
                    cnt_row = slots_query(
                        "SELECT COUNT(*) AS cnt FROM reservations WHERE memberid = %s",
                        (candidate["memberid"],)
                    )
                    cnt = cnt_row[0]["cnt"] if cnt_row else 0
                    if cnt > best_cnt:
                        best_cnt = cnt
                        best_uid = candidate["memberid"]
                if best_uid:
                    return best_uid

    # Step 4: email-based name lookup (last resort)
    name_row = slots_query("""
        SELECT fname, lname FROM login
        WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 1
    """, (email,))

    if not name_row:
        return None

    fname = (name_row[0].get("fname") or "").strip()
    lname = (name_row[0].get("lname") or "").strip()
    if not fname or not lname:
        return None

    r = slots_query("""
        SELECT memberid FROM login
        WHERE LOWER(TRIM(fname)) = LOWER(%s)
          AND LOWER(TRIM(lname)) = LOWER(%s)
        LIMIT 5
    """, (fname, lname))

    if not r:
        return None
    if len(r) == 1:
        return r[0]["memberid"]
    best_uid, best_cnt = None, -1
    for candidate in r:
        cnt_row = slots_query(
            "SELECT COUNT(*) AS cnt FROM reservations WHERE memberid = %s",
            (candidate["memberid"],)
        )
        cnt = cnt_row[0]["cnt"] if cnt_row else 0
        if cnt > best_cnt:
            best_cnt = cnt
            best_uid = candidate["memberid"]
    return best_uid


def get_equipment_stats(member_id, year=None):
    uid = _get_uid_from_member(member_id)
    if uid is None:
        return {"available": False, "total_slots": 0, "tools_used": [],
                "tools_count": 0, "approval_stats": {}, "lab_access_log": []}

    if year:
        date_filter = "AND YEAR(e.date_of_request) = %s"
        date_param  = year
        lab_filter  = "AND YEAR(date_request) = %s"
    else:
        date_filter = "AND e.date_of_request >= %s"
        date_param  = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        lab_filter  = ""

    tools = slots_query(f"""
        SELECT r.name AS tool_name,
               COUNT(e.request_id) AS times_booked,
               SUM(CASE WHEN e.status=3 THEN 1 ELSE 0 END) AS approved,
               SUM(CASE WHEN e.status=0 THEN 1 ELSE 0 END) AS pending
        FROM equipment_usage_approval e
        JOIN resources r ON r.machid = e.equipmentid
        WHERE e.requestedby = %s {date_filter}
        GROUP BY r.machid, r.name ORDER BY times_booked DESC LIMIT 50
    """, (uid, date_param))

    total = slots_query(f"""
        SELECT COUNT(*) AS cnt FROM equipment_usage_approval e
        WHERE e.requestedby = %s {date_filter}
    """, (uid, date_param))

    lab_params = [uid]
    if year: lab_params.append(year)
    lab = slots_query(f"""
        SELECT date_request AS access_date, equipments, access_period, approval
        FROM lab_access WHERE memberid=%s {lab_filter}
        ORDER BY date_request DESC LIMIT 20
    """, tuple(lab_params))

    tools = tools or []
    return {
        "available":    True,
        "total_slots":  total[0]["cnt"] if total else 0,
        "tools_used":   tools,
        "tools_count":  len(tools),
        "approval_stats": {
            "total":    total[0]["cnt"] if total else 0,
            "approved": sum(t.get("approved", 0) for t in tools),
            "pending":  sum(t.get("pending",  0) for t in tools),
        },
        "lab_access_log": lab,
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
    params = (member_id, year) if year else (member_id,)

    rows = hr_query(f"""
        SELECT pt.column_name, pt.old_value, pt.new_value, pt.timestamp,
        TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS updated_by_name
        FROM profile_tracking pt
        LEFT JOIN slotbooking.login l ON l.memberid = pt.updated_by
        WHERE pt.memberid = %s {year_filter}
        ORDER BY pt.timestamp DESC LIMIT 100
    """, params) or []

    # 🔧 FIX: serialize timestamp
    for r in rows:
        if r.get("timestamp"):
            r["timestamp"] = r["timestamp"].isoformat()

    return rows

# ── Anomaly detection ─────────────────────────────────────────────────────────

def get_anomalies(member_id, att, equip):
    alerts = []
    today  = date.today()
    pct    = att.get("attendance_pct", 0)

    if pct == 0 and att.get("mandatory_days", 0) > 0:
        alerts.append({"level": "critical", "message": "No attendance records found this year — possible data issue."})
    elif pct < 75:
        alerts.append({"level": "critical", "message": f"Attendance critically low at {pct}% (threshold: 75%)."})
    elif pct < 85:
        alerts.append({"level": "warning",  "message": f"Attendance below recommended level at {pct}% (threshold: 85%)."})

    q = (today.month - 1) // 3 + 1
    if q > 1:
        prev_q_start = date(today.year, (q - 2) * 3 + 1, 1)
        prev_q_end   = date(today.year, (q - 1) * 3 + 1, 1) - timedelta(days=1)
        curr_q_start = date(today.year, (q - 1) * 3 + 1, 1)
        prev_rows = hr_query("SELECT COUNT(*) AS cnt FROM user_attendance WHERE memberid=%s AND date BETWEEN %s AND %s",
                             (member_id, prev_q_start, prev_q_end))
        curr_rows = hr_query("SELECT COUNT(*) AS cnt FROM user_attendance WHERE memberid=%s AND date >= %s",
                             (member_id, curr_q_start))
        prev_cnt = prev_rows[0]["cnt"] if prev_rows else 0
        curr_cnt = curr_rows[0]["cnt"] if curr_rows else 0
        if prev_cnt > 0:
            change = ((curr_cnt - prev_cnt) / prev_cnt) * 100
            if change <= -30:
                alerts.append({"level": "critical", "message": f"Attendance dropped {abs(round(change))}% vs previous quarter ({prev_cnt} → {curr_cnt} days)."})
            elif change <= -15:
                alerts.append({"level": "warning",  "message": f"Attendance down {abs(round(change))}% vs previous quarter."})
            elif change >= 20:
                alerts.append({"level": "info",     "message": f"Attendance improved {round(change)}% vs previous quarter."})

    for lv in att.get("leave_summary", []):
        if lv.get("util_pct") and lv["util_pct"] >= 90:
            alerts.append({"level": "warning",
                "message": f"{lv['type_of_leave']} leave at {lv['util_pct']}% utilisation ({lv['days_taken']}/{lv['max_allowed']} days)."})

    ap = equip.get("approval_stats", {})
    if ap:
        total    = ap.get("total",    0) or 0
        approved = ap.get("approved", 0) or 0
        rejected = ap.get("rejected", 0) or 0
        if total >= 5 and approved == 0:
            alerts.append({"level": "warning", "message": f"All {total} equipment requests have no approvals — review required."})
        elif total >= 3 and rejected and (rejected / total) > 0.5:
            alerts.append({"level": "warning", "message": f"High equipment rejection rate: {rejected}/{total} requests rejected."})

    if not alerts:
        alerts.append({"level": "info", "message": "No anomalies detected — profile within normal parameters."})
    return alerts


def get_attendance_trend(member_id, year=None):
    today    = date.today()
    year     = year or today.year
    holidays = set()
    results  = []
    max_month = 12 if year < today.year else today.month
    for month in range(1, max_month + 1):
        end   = date(year, month, (date(year, month + 1, 1) - timedelta(days=1)).day if month < today.month else today.day)
        start = date(year, month, 1)
        mandatory = sum(1 for i in range((end - start).days + 1)
                        if (start + timedelta(i)).weekday() < 5 and (start + timedelta(i)) not in holidays)
        rows    = hr_query("SELECT COUNT(*) AS cnt FROM user_attendance WHERE memberid=%s AND date BETWEEN %s AND %s",
                           (member_id, start, end))
        present = rows[0]["cnt"] if rows else 0
        results.append({"month": start.strftime("%b"), "present": present,
                         "mandatory": mandatory, "pct": round(present / mandatory * 100, 1) if mandatory else 0})
    return results

def get_comparative_stats(member_id, att, equip):
    person = hr_query("SELECT team FROM profile WHERE member_id=%s LIMIT 1", (member_id,))
    if not person or not person[0].get("team"):
        return []

    raw_team = person[0]["team"]
    primary  = raw_team.split(",")[0].strip()
    yr_start = date(date.today().year, 1, 1)
    mandatory = calc_mandatory_days()

    team_members = hr_query("""
        SELECT member_id FROM profile
        WHERE team LIKE %s AND member_id != %s AND email IS NOT NULL AND email != ''
        AND (taken_clearance IS NULL OR taken_clearance = 0)
    """, (f"%{primary}%", member_id))
    if not team_members:
        return []

    team_ids     = [r["member_id"] for r in team_members]
    placeholders = ",".join(["%s"] * len(team_ids))
    team_att     = hr_query(f"""
        SELECT memberid, COUNT(*) AS days_present FROM user_attendance
        WHERE memberid IN ({placeholders}) AND date >= %s GROUP BY memberid
    """, (*team_ids, yr_start))

    team_att_days = [float(r["days_present"]) for r in (team_att or [])]
    team_att_avg  = round(sum(team_att_days) / len(team_att_days), 1) if team_att_days else 0
    team_att_pct  = round(team_att_avg / mandatory * 100, 1) if mandatory else 0
    my_att_pct    = att.get("attendance_pct", 0)
    att_diff      = round(my_att_pct - team_att_pct, 1)

    comparisons = [{
        "label": "Attendance", "mine": f"{my_att_pct}%", "team_avg": f"{team_att_pct}%",
        "diff": att_diff, "direction": "up" if att_diff > 0 else ("down" if att_diff < 0 else "equal"),
        "mine_pct": min(my_att_pct, 100), "team_pct": min(team_att_pct, 100),
        "insight": (f"{abs(att_diff)}% above team average" if att_diff > 2 else
                    f"{abs(att_diff)}% below team average" if att_diff < -2 else "On par with team average"),
    }]

    my_leave_util    = att.get("leave_utilisation_pct", 0)
    team_leave_rows  = []
    for tid in team_ids[:20]:
        lv = hr_query("""
            SELECT SUM(DATEDIFF(to_date,from_date)+1) AS taken,
                   (SELECT SUM(max_leaves) FROM max_leaves WHERE memberid=%s) AS max_l
            FROM leaves WHERE memberid=%s AND status=1
        """, (tid, tid))
        if lv and lv[0]["taken"] and lv[0]["max_l"]:
            team_leave_rows.append(round(float(lv[0]["taken"]) / float(lv[0]["max_l"]) * 100, 1))

    if team_leave_rows:
        team_leave_avg = round(sum(team_leave_rows) / len(team_leave_rows), 1)
        leave_diff     = round(my_leave_util - team_leave_avg, 1)
        comparisons.append({
            "label": "Leave Utilisation", "mine": f"{my_leave_util}%", "team_avg": f"{team_leave_avg}%",
            "diff": leave_diff, "direction": "up" if leave_diff > 0 else ("down" if leave_diff < 0 else "equal"),
            "mine_pct": min(my_leave_util, 100), "team_pct": min(team_leave_avg, 100),
            "insight": (f"{abs(leave_diff)}% more leave than team avg" if leave_diff > 5 else
                        f"{abs(leave_diff)}% less leave than team avg" if leave_diff < -5 else "Similar leave usage to team"),
        })

    my_equip        = int(equip.get("total_slots", 0) or 0)
    uid_rows        = [hr_query("SELECT email FROM profile WHERE member_id=%s LIMIT 1", (tid,)) for tid in team_ids[:15]]
    team_equip_cnts = []
    for pr, tid in zip(uid_rows, team_ids[:15]):
        if pr and pr[0].get("email"):
            lr = slots_query("SELECT memberid FROM login WHERE email=%s LIMIT 1", (pr[0]["email"],))
            if lr:
                eq = slots_query("SELECT COUNT(*) AS cnt FROM equipment_usage_approval WHERE requestedby=%s", (lr[0]["memberid"],))
                if eq: team_equip_cnts.append(int(eq[0]["cnt"]))

    if team_equip_cnts:
        team_equip_avg = round(sum(team_equip_cnts) / len(team_equip_cnts), 1)
        equip_diff     = round(my_equip - team_equip_avg, 1)
        max_val        = max(my_equip, team_equip_avg, 1)
        comparisons.append({
            "label": "Equipment Requests", "mine": str(my_equip), "team_avg": str(team_equip_avg),
            "diff": equip_diff, "direction": "up" if equip_diff > 0 else ("down" if equip_diff < 0 else "equal"),
            "mine_pct": round(my_equip / max_val * 100), "team_pct": round(team_equip_avg / max_val * 100),
            "insight": (f"{abs(equip_diff)} more requests than team avg" if equip_diff > 1 else
                        f"{abs(equip_diff)} fewer requests than team avg" if equip_diff < -1 else "Similar equipment usage to team"),
        })

    return {"team": primary, "team_size": len(team_ids), "comparisons": comparisons}


def get_objectives(member_id):
    return hr_query("""
        SELECT d.review_name, f.field_name, f.type_of_field,
               f.order_of_display, d.value
        FROM objective_data d
        JOIN objective_fields f
            ON f.variable_name = d.variable_name
            AND f.review_name = d.review_name
        WHERE d.memberid = %s
        ORDER BY d.review_name DESC, f.order_of_display
    """, (member_id,))


def get_performance_rating(member_id):
    """
    Return per-cycle performance ratings for a staff member.
    Rows are ordered most-recent cycle first.
    """
    rows = hr_query("""
        SELECT review_name, rating, grade, remarks
        FROM performance_rating
        WHERE memberid = %s
        ORDER BY review_name DESC
    """, (member_id,))
    return rows or []


# ── System ownership (staff) ──────────────────────────────────────────────────

def get_staff_system_owned(member_id: int) -> list:
    """Tools for which this staff member is system owner."""
    uid = _get_uid_from_member(member_id)
    if not uid:
        return []
    from models.lab import get_system_owner_tools
    return get_system_owner_tools(uid)

def get_staff_tool_perms_rich(member_id: int) -> list:
    """Enriched tool permissions for a staff member."""
    uid = _get_uid_from_member(member_id)
    if not uid:
        return []
    from models.lab import get_member_tool_permissions
    return get_member_tool_permissions(uid)


def get_staff_reservations(member_id: int, year=None) -> list:
    """
    Slot reservations for a staff member — fetched via slotbooking memberid.
    Staff members use the same reservations table as lab users.
    """
    uid = _get_uid_from_member(member_id)
    if not uid:
        return []
    from models.lab import get_lab_reservations
    return get_lab_reservations(uid, year) or []