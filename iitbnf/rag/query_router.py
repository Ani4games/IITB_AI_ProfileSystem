"""
rag/query_router.py — Structured Query Handler
================================================
Intercepts questions that can be answered deterministically from the DB.
The SLM only handles truly open-ended / analytical questions.

Query categories handled:
  1. Slot activity        — year-specific + multi-year comparison
  2. Reservations         — year-specific + multi-year comparison
  3. Attendance           — year-specific + multi-year comparison
  4. Tool-specific usage  — which tools, how many times, last used
  5. Monthly breakdown    — month-by-month slot/attendance for a year
  6. Publications         — year-specific paper counts
  7. Projects             — active/total project queries
  8. Training             — training session counts
  9. Cancellations        — cancellation history
 10. Permissions          — tool access list

Returns None if no structured route matches → falls through to SLM.
"""

from os import name
import re
import logging
from db import slots_query, hr_query

logger = logging.getLogger(__name__)

# ── Patterns ──────────────────────────────────────────────────────────────────
YEAR_PATTERN = re.compile(r'\b(20\d{2})\b')
MONTH_NAMES  = {
    'january':1,'february':2,'march':3,'april':4,
    'may':5,'june':6,'july':7,'august':8,
    'september':9,'october':10,'november':11,'december':12,
    'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,
    'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
}
MONTH_DISPLAY = {
    1:'January',2:'February',3:'March',4:'April',
    5:'May',6:'June',7:'July',8:'August',
    9:'September',10:'October',11:'November',12:'December',
}

# ── Keyword groups ────────────────────────────────────────────────────────────
SLOT_KEYWORDS = [
    'slot activity', 'equipment request', 'usage request', 'slot booking',
    'equipment usage', 'how active', 'usage in', 'activity in',
    'equipment summary', 'compare slot', 'compare equipment',
    'compare booking', 'compare usage', 'slot comparison',
    'equipment comparison', 'booking comparison', 'activity comparison',
    'equipment booking', 'slot.*2', 'equipment.*2',
    'how many times', 'request equipment', 'equipment in',
    'booking of', 'usage of',
]
RESERVATION_KEYWORDS = ['reservation','booked slot','slot reserved','booking']
ATTEND_KEYWORDS = [
    'attendance', 'present', 'working day', 'mandatory day',
    'compare attendance', 'attendance comparison', 'attendance change',
    'more regular', 'less regular', 'regular in', 'regular',
    'attendance in', 'how often', 'come to',
    'show attendance', 'attendance for',
]
LEAVE_KEYWORDS       = ['leave','casual leave','earned leave','sick leave','medical leave']
TOOL_KEYWORDS = [
    'which tool', 'what tool', 'which machine', 'what machine',
    'which equipment', 'what equipment', 'used tool', 'used machine',
    'list the machine', 'list the tool', 'list the equipment',
    'list machine', 'list tool', 'list equipment', 'requests approved',
    'most used', 'most used equipment', 'most used tool',
    'has requested', 'has worked with', 'worked with', 'most booked', 'cancelled', 'rejected',
    'top tools', 'top machines', 'top equipment','list top', 'show top',
]
MONTHLY_KEYWORDS     = ['month by month','monthly breakdown','each month',
                        'month wise','monthwise','per month']
PAPER_KEYWORDS       = ['paper','publication','research paper','published']
PROJECT_KEYWORDS     = ['project','faculty project','research project']
TRAINING_KEYWORDS    = ['training','trained','training session']
CANCEL_KEYWORDS      = ['cancel','cancellation','cancelled']
PERM_KEYWORDS        = ['permission','authorized tool','access permission',
                        'which tool permission','tool access']


def _extract_years(question: str) -> list[int]:
    return [int(y) for y in YEAR_PATTERN.findall(question)]


def _extract_month(question: str) -> int | None:
    q = question.lower()
    for name, num in MONTH_NAMES.items():
        if name in q:
            return num
    return None


def _has_any(question: str, keywords: list) -> bool:
    q = question.lower()
    return any(k in q for k in keywords)


def _extract_tool_hint(question: str) -> str | None:
    """
    Extract a partial tool/equipment name from the question.
    Looks for known nanofab tool keywords.
    """
    TOOL_HINTS = [
        'pecvd','lpcvd','sputter','lithograph','evaporat','etch',
        'cvd','pvd','rta','sem','tem','afm','xrd','edx','xps',
        'spin coat','anneal','furnace','implant','oxide','nitride',
        'metal','resist','develop','strip','clean','rinse',
        'probe','measure','inspect','align',
    ]
    q = question.lower()
    for hint in TOOL_HINTS:
        if hint in q:
            return hint
    return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ROUTER
# ══════════════════════════════════════════════════════════════════════════════

def route(question: str, ctx: dict) -> str | None:
    """
    Returns a direct answer string if the question can be answered
    deterministically from the DB. Returns None to fall through to the SLM.
    """
    q     = question.lower().strip()
    years = _extract_years(question)
    uid   = ctx.get("slot_uid")
    mid   = ctx.get("member_id")
    name  = ctx.get("name", "This member")
    # ADD at the top of the try block in route(), before "# 1. Monthly breakdown":

    # ── 0. "Since year" queries ───────────────────────────────────────────
    SINCE_PATTERN = re.compile(r'\bsince\s+(20\d{2})\b', re.I)
    since_match = SINCE_PATTERN.search(question)
    if since_match:
        since_year = int(since_match.group(1))
        if _has_any(q, LEAVE_KEYWORDS):
            return _leaves_since_year(mid, name, since_year)
        if _has_any(q, ATTEND_KEYWORDS):
            return _attendance_since_year(mid, name, since_year)
        if _has_any(q, SLOT_KEYWORDS + ['slot', 'equipment request']):
            return _slot_since_year(uid, name, since_year)
    # ADD after SINCE_PATTERN block, before "# 1. Monthly breakdown":

    # ── 0b. "From year to year" range queries ─────────────────────────
    RANGE_PATTERN = re.compile(r'\bfrom\s+(20\d{2})\s+to\s+(20\d{2})\b', re.I)
    range_match = RANGE_PATTERN.search(question)
    if range_match:
        y_start = int(range_match.group(1))
        y_end   = int(range_match.group(2))
        if y_start <= y_end:
            range_years = list(range(y_start, y_end + 1))
            if _has_any(q, LEAVE_KEYWORDS):
                return _leaves_range(mid, name, range_years)
            if _has_any(q, ATTEND_KEYWORDS):
                return _attendance_range(mid, name, range_years)
            if _has_any(q, SLOT_KEYWORDS + ['slot', 'equipment request']):
                return _slot_range(uid, name, range_years)
            if _has_any(q, RESERVATION_KEYWORDS):
                return _reservation_range(uid, name, range_years)
    try:
        # ── 1. Monthly breakdown ──────────────────────────────────────────────
        # Check this FIRST because it often contains a year too
        if _has_any(q, MONTHLY_KEYWORDS):
            target_year = years[0] if years else None
            if _has_any(q, RESERVATION_KEYWORDS):
                return _monthly_reservations(uid, name, target_year)
            if _has_any(q, SLOT_KEYWORDS + ['slot','equipment','request']):
                return _monthly_slot_activity(uid, name, target_year)
            if _has_any(q, ATTEND_KEYWORDS):
                return _monthly_attendance(mid, name, target_year)
            # Default: slot activity
            return _monthly_slot_activity(uid, name, target_year)

        # ── 2. Tool-specific usage ────────────────────────────────────────────
        if _has_any(q, TOOL_KEYWORDS):
            tool_hint = _extract_tool_hint(q)
            target_year = years[0] if len(years) == 1 else None
            # Extract "top N" / "list N" limit
            top_n_match = re.search(r'\b(top|list|show|first)\s+(\d+)\b', q, re.I)
            top_n = int(top_n_match.group(2)) if top_n_match else None
            return _tool_specific_usage(uid, name, tool_hint, target_year, limit=top_n)
        # ── 3. Multi-year comparisons ─────────────────────────────────────────
        if len(years) >= 2:
            if _has_any(q, RESERVATION_KEYWORDS) and not _has_any(q, SLOT_KEYWORDS):
                return _compare_reservations(uid, name, years)
            if _has_any(q, SLOT_KEYWORDS + ['slot','equipment request']):
                return _compare_slot_activity(uid, name, years)
            if _has_any(q, ATTEND_KEYWORDS):
                return _compare_attendance(mid, name, years)
            if _has_any(q, LEAVE_KEYWORDS):
                return _compare_leaves(mid, name, years)
            if _has_any(q, PAPER_KEYWORDS):
                return _compare_publications(uid, name, years)

        # ── 4. Single-year specific queries ───────────────────────────────────
        if len(years) == 1:
            yr = years[0]
            # Reservations BEFORE slot — more specific, "slot reservation count"
            # would otherwise match SLOT_KEYWORDS first
            if _has_any(q, RESERVATION_KEYWORDS):
                return _reservations_year(uid, name, yr)
            if _has_any(q, SLOT_KEYWORDS + ['slot','equipment request']):
                return _slot_activity_year(uid, name, yr)
            if _has_any(q, ATTEND_KEYWORDS):
                return _attendance_year(mid, name, yr)
            if _has_any(q, LEAVE_KEYWORDS):
                return _leaves_year(mid, name, yr)
            if _has_any(q, PAPER_KEYWORDS):
                return _publications_year(uid, name, yr)

        # ── 5. Non-year structured queries ────────────────────────────────────
        if _has_any(q, PERM_KEYWORDS):
            return _list_permissions(uid, name)

        if _has_any(q, CANCEL_KEYWORDS):
            return _cancellation_summary(uid, name)

        if _has_any(q, TRAINING_KEYWORDS):
            return _training_summary(uid, name)

        if _has_any(q, PROJECT_KEYWORDS):
            return _project_summary(uid, name)

    except Exception as e:
        logger.error("[QueryRouter] Error routing question '%s': %s", question[:60], e)
        return None

    return None


# ══════════════════════════════════════════════════════════════════════════════
# SLOT ACTIVITY
# ══════════════════════════════════════════════════════════════════════════════

def _slot_activity_year(uid, name, year) -> str:
    if not uid:
        return f"Slot booking data is not available for {name}."
    STATUS_FOCUS = {
    'approved': 'approved',
    'booked': 'slot_booked', 
    'rejected': 'rejected',
    'pending': 'pending',
    }
    # If a focus word is detected, lead with that number
    rows = slots_query("""
        SELECT
            COUNT(*)                                            AS total,
            SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)          AS slot_booked,
            SUM(CASE WHEN status=1 THEN 1 ELSE 0 END)          AS approved,
            SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)          AS pending,
            SUM(CASE WHEN status=2 THEN 1 ELSE 0 END)          AS rejected,
            COUNT(DISTINCT equipmentid)                         AS tools_used
        FROM equipment_usage_approval
        WHERE requestedby=%s AND YEAR(date_of_request)=%s
    """, (uid, year))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"{name} has no equipment request data for {year}."
    r = rows[0]
    return (
        f"In {year}, {name} submitted {r['total']} equipment usage "
        f"{'request' if r['total']==1 else 'requests'} across "
        f"{r['tools_used'] or 0} "
        f"{'tool' if (r['tools_used'] or 0)==1 else 'tools'}. "
        f"Breakdown: {r['slot_booked'] or 0} slot-booked, "
        f"{r['approved'] or 0} approved, "
        f"{r['pending'] or 0} pending, "
        f"{r['rejected'] or 0} rejected."
    )


def _compare_slot_activity(uid, name, years) -> str:
    if not uid:
        return f"Slot booking data is not available for {name}."
    
    is_comparison = len(years) == 2 # only add trend for exactly 2 years
    lines = [
        f"Equipment request summary for {name} "
        f"({', '.join(str(y) for y in sorted(years))}):\n"
    ]
    for year in sorted(years):
        rows = slots_query("""
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)      AS slot_booked,
                SUM(CASE WHEN status=1 THEN 1 ELSE 0 END)      AS approved,
                SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)      AS pending,
                SUM(CASE WHEN status=2 THEN 1 ELSE 0 END)      AS rejected,
                COUNT(DISTINCT equipmentid)                     AS tools_used
            FROM equipment_usage_approval
            WHERE requestedby=%s AND YEAR(date_of_request)=%s
        """, (uid, year))
        if rows and rows[0] and rows[0]['total']:
            r = rows[0]
            lines.append(
                f"  {year}: {r['total']} requests | "
                f"{r['tools_used'] or 0} tools | "
                f"{r['slot_booked'] or 0} slot-booked | "
                f"{r['approved'] or 0} approved | "
                f"{r['pending'] or 0} pending | "
                f"{r['rejected'] or 0} rejected"
            )
        else:
            lines.append(f"  {year}: No data found.")
    if is_comparison:
    # Add trend insight if exactly 2 years
        if len(years) == 2 and all(
            rows and rows[0] and rows[0]['total']
            for yr in years
            for rows in [slots_query(
                "SELECT COUNT(*) AS total FROM equipment_usage_approval "
                "WHERE requestedby=%s AND YEAR(date_of_request)=%s", (uid, yr)
            )]
        ):
            pass
        totals = {}
        for yr in sorted(years):
            r = slots_query(
                "SELECT COUNT(*) AS total FROM equipment_usage_approval "
                "WHERE requestedby=%s AND YEAR(date_of_request)=%s", (uid, yr)
            )
            totals[yr] = int(r[0]['total'] if r else 0)
        yr_sorted = sorted(years)
        diff = totals[yr_sorted[1]] - totals[yr_sorted[0]]
        if diff > 0:
            lines.append(f"\n  Trend: +{diff} more requests in {yr_sorted[1]} compared to {yr_sorted[0]}.")
        elif diff < 0:
            lines.append(f"\n  Trend: {diff} fewer requests in {yr_sorted[1]} compared to {yr_sorted[0]}.")
        else:
            lines.append(f"\n  Trend: Same number of requests in both years.")

    return "\n".join(lines)


def _monthly_slot_activity(uid, name, year) -> str:
    if not uid:
        return f"Slot booking data is not available for {name}."
    from datetime import date
    year = year or date.today().year
    rows = slots_query("""
        SELECT
            MONTH(date_of_request)                              AS month,
            COUNT(*)                                            AS total,
            SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)          AS slot_booked,
            SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)          AS pending
        FROM equipment_usage_approval
        WHERE requestedby=%s AND YEAR(date_of_request)=%s
        GROUP BY MONTH(date_of_request)
        ORDER BY month
    """, (uid, year))
    if not rows:
        return f"{name} has no equipment request data for {year}."
    lines = [f"Monthly equipment request breakdown for {name} in {year}:\n"]
    total_year = 0
    for r in rows:
        m_name = MONTH_DISPLAY.get(r['month'], str(r['month']))
        lines.append(
            f"  {m_name:>10}: {r['total']:>3} requests "
            f"({r['slot_booked'] or 0} booked, {r['pending'] or 0} pending)"
        )
        total_year += int(r['total'] or 0)
    lines.append(f"\n  Total {year}: {total_year} requests across {len(rows)} active months.")
    return "\n".join(lines)

def _slot_since_year(uid, name, since_year) -> str:
    if not uid:
        return f"Slot booking data is not available for {name}."
    rows = slots_query("""
        SELECT
            COUNT(*)                                            AS total,
            SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)          AS slot_booked,
            COUNT(DISTINCT equipmentid)                         AS tools_used
        FROM equipment_usage_approval
        WHERE requestedby=%s AND YEAR(date_of_request) >= %s
    """, (uid, since_year))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"{name} has no equipment request data since {since_year}."
    r = rows[0]
    return (
        f"Since {since_year}, {name} submitted {r['total']} equipment usage "
        f"{'request' if r['total']==1 else 'requests'} across "
        f"{r['tools_used'] or 0} "
        f"{'tool' if (r['tools_used'] or 0)==1 else 'tools'}. "
        f"Breakdown: {r['slot_booked'] or 0} slot-booked."
    )

# ══════════════════════════════════════════════════════════════════════════════
# RESERVATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _reservations_year(uid, name, year) -> str:
    if not uid:
        return f"Reservation data is not available for {name}."
    rows = slots_query("""
        SELECT
            COUNT(*)                                                        AS total,
            COUNT(DISTINCT machid)                                          AS tools_used,
            SUM(CASE WHEN activation_status=2 AND isblackout=1 THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN activation_status=1 AND isblackout=1 THEN 1 ELSE 0 END) AS upcoming,
            SUM(CASE WHEN activation_status=0 AND isblackout=1 THEN 1 ELSE 0 END) AS active
        FROM reservations
        WHERE memberid=%s
          AND YEAR(FROM_UNIXTIME(startdate))=%s
          AND isblackout=1
    """, (uid, year))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"{name} has no slot reservation data for {year}."
    r = rows[0]
    return (
        f"In {year}, {name} made {r['total']} slot "
        f"{'reservation' if r['total']==1 else 'reservations'} "
        f"across {r['tools_used'] or 0} "
        f"{'piece' if (r['tools_used'] or 0)==1 else 'pieces'} of equipment. "
        f"Status: {r['completed'] or 0} completed, "
        f"{r['upcoming'] or 0} upcoming, "
        f"{r['active'] or 0} active."
    )


def _compare_reservations(uid, name, years) -> str:
    if not uid:
        return f"Reservation data is not available for {name}."
    is_comparison = len(years) == 2 # only add trend for exactly 2 years
    lines = [
        f"Slot reservation comparison for {name} "
        f"({'  '.join(str(y) for y in sorted(years))}):\n"
    ]
    for year in sorted(years):
        rows = slots_query("""
            SELECT
                COUNT(*)                                                        AS total,
                COUNT(DISTINCT machid)                                          AS tools_used,
                SUM(CASE WHEN activation_status=2 AND isblackout=1 THEN 1 ELSE 0 END) AS completed
            FROM reservations
            WHERE memberid=%s
              AND YEAR(FROM_UNIXTIME(startdate))=%s
              AND isblackout=1
        """, (uid, year))
        if rows and rows[0] and rows[0]['total']:
            r = rows[0]
            lines.append(
                f"  {year}: {r['total']} reservations | "
                f"{r['tools_used'] or 0} tools | "
                f"{r['completed'] or 0} completed"
            )
        else:
            lines.append(f"  {year}: No reservation data found.")
    if is_comparison:
    # Trend
        totals = {}
        for yr in sorted(years):
            r = slots_query(
                "SELECT COUNT(*) AS total FROM reservations "
                "WHERE memberid=%s AND YEAR(FROM_UNIXTIME(startdate))=%s AND isblackout=1",
                (uid, yr)
            )
            totals[yr] = int(r[0]['total'] if r and r[0] else 0)

        if len(years) == 2:
            yr_sorted = sorted(years)
            diff = totals[yr_sorted[1]] - totals[yr_sorted[0]]
            if diff > 0:
                lines.append(f"\n  Trend: +{diff} more reservations in {yr_sorted[1]}.")
            elif diff < 0:
                lines.append(f"\n  Trend: {abs(diff)} fewer reservations in {yr_sorted[1]}.")
            else:
                lines.append(f"\n  Trend: Same number of reservations in both years.")

    return "\n".join(lines)


def _monthly_reservations(uid, name, year) -> str:
    if not uid:
        return f"Reservation data is not available for {name}."
    from datetime import date
    year = year or date.today().year
    rows = slots_query("""
        SELECT
            MONTH(FROM_UNIXTIME(startdate))     AS month,
            COUNT(*)                            AS total,
            COUNT(DISTINCT machid)              AS tools_used
        FROM reservations
        WHERE memberid=%s
          AND YEAR(FROM_UNIXTIME(startdate))=%s
          AND isblackout=1
        GROUP BY MONTH(FROM_UNIXTIME(startdate))
        ORDER BY month
    """, (uid, year))
    if not rows:
        return f"{name} has no reservation data for {year}."
    lines = [f"Monthly slot reservation breakdown for {name} in {year}:\n"]
    total_year = 0
    for r in rows:
        m_name = MONTH_DISPLAY.get(r['month'], str(r['month']))
        lines.append(
            f"  {m_name:>10}: {r['total']:>3} reservations "
            f"across {r['tools_used'] or 0} tools"
        )
        total_year += int(r['total'] or 0)
    lines.append(f"\n  Total {year}: {total_year} reservations across {len(rows)} active months.")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL-SPECIFIC USAGE
# ══════════════════════════════════════════════════════════════════════════════

def _tool_specific_usage(uid, name, tool_hint, year=None, limit=None) -> str:
    if not uid:
        return f"Equipment usage data is not available for {name}."

    year_filter  = "AND YEAR(e.date_of_request) = %s" if year else ""
    params_base  = [uid]
    if year:
        params_base.append(year)

    # Get tools used by this member (filtered by tool hint if provided)
    tool_filter  = "AND LOWER(r.name) LIKE %s" if tool_hint else ""
    if tool_hint:
        params_base.append(f"%{tool_hint}%")
     # Use requested limit or default 15
    display_limit = limit if limit and 1 <= limit <= 50 else 15

    rows = slots_query(f"""
        SELECT
            r.name                                              AS tool_name,
            COUNT(e.request_id)                                 AS times_requested,
            SUM(CASE WHEN e.status=3 THEN 1 ELSE 0 END)        AS slot_booked,
            SUM(CASE WHEN e.status=2 THEN 1 ELSE 0 END)        AS rejected,
            MAX(e.date_of_request)                              AS last_used
        FROM equipment_usage_approval e
        JOIN resources r ON r.machid = e.equipmentid
        WHERE e.requestedby = %s
          {year_filter}
          {tool_filter}
        GROUP BY r.machid, r.name
        ORDER BY times_requested DESC
        LIMIT {display_limit}
    """, tuple(params_base))

    if not rows:
        period = f"in {year}" if year else "overall"
        hint_str = f" matching '{tool_hint}'" if tool_hint else ""
        return f"{name} has no equipment usage records{hint_str} {period}."

    period_str = f"in {year}" if year else "overall"
    top_label = f"Top {display_limit}" if limit else "Equipment usage"
    lines = [f"Equipment usage for {name} {period_str}:\n"]
    for r in rows:
        lines.append(
            f"  {r['tool_name']}: "
            f"{r['times_requested']} requests | "
            f"{r['slot_booked'] or 0} slot-booked | "
            f"{r['rejected'] or 0} rejected | "
            f"last: {str(r['last_used'])[:10] if r['last_used'] else 'unknown'}"
        )
    return "\n".join(lines)
def _tool_permission_date(uid, name, tool_hint) -> str:
    rows = slots_query("""
        SELECT r.name AS tool_name,
               DATE_FORMAT(STR_TO_DATE(p.date, '%%m/%%d/%%Y'), '%%d-%%m-%%Y') AS granted_on
        FROM permissions p
        JOIN resources r ON r.machid = p.machid
        WHERE p.memberid=%s AND LOWER(r.name) LIKE LOWER(%s)
        LIMIT 1
    """, (uid, f"%{tool_hint}%"))
    
    if not rows:
        return f"No permission record found for that tool for {name}."
    return (
        f"{name} was authorized to use {rows[0]['tool_name']} "
        f"on {rows[0]['granted_on']}."
    )
def _logbook_top_tools(uid, name) -> str:
    from models.staff import get_staff_logbook_stats, _get_uid_from_member
    stats = get_staff_logbook_stats(uid)  # already cached
    breakdown = stats.get("breakdown", [])
    if not breakdown:
        return f"{name} has no logbook entries on record."
    top = breakdown[0]
    lines = [f"{b['tool_name']}: {b['entries']} entries" for b in breakdown[:5]]
    return (
        f"{name}'s most used tool by logbook entries is "
        f"{top['tool_name']} ({top['entries']} entries). "
        f"Top tools: {', '.join(lines)}."
    )
# ══════════════════════════════════════════════════════════════════════════════
# ATTENDANCE
# ══════════════════════════════════════════════════════════════════════════════

def _attendance_year(mid, name, year) -> str:
    if not mid:
        return f"Attendance data is not available for {name}."
    rows = hr_query("""
        SELECT COUNT(*) AS days_present
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date)=%s
    """, (mid, year))
    days = int(rows[0]['days_present'] if rows and rows[0] else 0)
    if not days:
        return f"{name} has no attendance records for {year}."
    return f"In {year}, {name} was present for {days} working days."

def _attendance_percentage_value(mid, name, year):
    if not mid:
        return None
    rows = hr_query("""
        SELECT
            COUNT(*) AS days_present,
            (SELECT COUNT(*) FROM working_days WHERE YEAR(date)=%s) AS total_working_days
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date)=%s
    """, (year, mid, year))
    if not rows or not rows[0] or not rows[0]['total_working_days']:
        return None
    days_present = int(rows[0]['days_present'] or 0)
    total_days = int(rows[0]['total_working_days'] or 0)
    return (days_present / total_days * 100) if total_days else 0


def _attendance_percentage(mid, name, year) -> str:
    if not mid:
        return f"Attendance data is not available for {name}."
    rows = hr_query("""
        SELECT
            COUNT(*) AS days_present,
            (SELECT COUNT(*) FROM working_days WHERE YEAR(date)=%s) AS total_working_days
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date)=%s
    """, (year, mid, year))
    if not rows or not rows[0] or not rows[0]['total_working_days']:
        return f"{name} has no attendance records for {year}."
    days_present = int(rows[0]['days_present'] or 0)
    total_days = int(rows[0]['total_working_days'] or 0)
    percentage = (days_present / total_days * 100) if total_days else 0
    return f"In {year}, {name} attended {percentage:.1f}% of working days ({days_present}/{total_days})."


def _attendance_pct_change(mid, name, y1, y2) -> str:
    if not mid:
        return f"Attendance data is not available for {name}."
    rows = hr_query("""
        SELECT
            YEAR(date) AS year,
            COUNT(*) AS days_present
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date) IN (%s, %s)
        GROUP BY YEAR(date)
    """, (mid, y1, y2))
    data = {r['year']: int(r['days_present'] or 0) for r in rows} if rows else {}
    d1 = data.get(y1, 0)
    d2 = data.get(y2, 0)
    pct1 = _attendance_percentage_value(mid, name, y1)
    pct2 = _attendance_percentage_value(mid, name, y2)
    pct1_str = _attendance_percentage(mid, name, y1)
    pct2_str = _attendance_percentage(mid, name, y2)
    change_str = f"{pct2_str} vs {pct1_str}"
    if d1 and d2 and pct1 is not None and pct2 is not None:
        diff = pct2 - pct1
        trend = "increased" if diff > 0 else "decreased" if diff < 0 else "no change"
        change_str += f" → {trend} by {abs(diff):.1f} percentage points"
    return f"Attendance percentage comparison for {name}: {change_str}."


def _compare_attendance(mid, name, years) -> str:
    if not mid:
        return f"Attendance data is not available for {name}."
    is_comparison = len(years) == 2 # only add trend for exactly 2 years
    lines = [
        f"Attendance comparison for {name} "
        f"({'  '.join(str(y) for y in sorted(years))}):\n"
    ]
    totals = {}
    for year in sorted(years):
        rows = hr_query("""
            SELECT COUNT(*) AS days_present
            FROM user_attendance
            WHERE memberid=%s AND YEAR(date)=%s
        """, (mid, year))
        days = int(rows[0]['days_present'] if rows and rows[0] else 0)
        totals[year] = days
        lines.append(f"  {year}: {days} days present.")
    
    if is_comparison:
        if len(years) == 2:
            yr_sorted = sorted(years)
            diff = totals[yr_sorted[1]] - totals[yr_sorted[0]]
            if diff > 0:
                lines.append(f"\n  Trend: +{diff} more days present in {yr_sorted[1]}.")
            elif diff < 0:
                lines.append(f"\n  Trend: {abs(diff)} fewer days present in {yr_sorted[1]}.")
            else:
                lines.append(f"\n  Trend: Same attendance in both years.")

    return "\n".join(lines)


def _monthly_attendance(mid, name, year) -> str:
    if not mid:
        return f"Attendance data is not available for {name}."
    from datetime import date
    year = year or date.today().year
    rows = hr_query("""
        SELECT
            MONTH(date)     AS month,
            COUNT(*)        AS days_present
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date)=%s
        GROUP BY MONTH(date)
        ORDER BY month
    """, (mid, year))
    if not rows:
        return f"{name} has no attendance records for {year}."
    lines = [f"Monthly attendance breakdown for {name} in {year}:\n"]
    total_year = 0
    for r in rows:
        m_name = MONTH_DISPLAY.get(r['month'], str(r['month']))
        lines.append(f"  {m_name:>10}: {r['days_present']:>3} days present")
        total_year += int(r['days_present'] or 0)
    lines.append(f"\n  Total {year}: {total_year} days present.")
    return "\n".join(lines)

def _attendance_since_year(mid, name, since_year) -> str:
    if not mid:
        return f"Attendance data is not available for {name}."
    from datetime import date as _date
    current_year = _date.today().year
    
    rows = hr_query("""
        SELECT YEAR(date) AS yr, COUNT(*) AS days_present
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date) >= %s
        GROUP BY YEAR(date)
        ORDER BY yr
    """, (mid, since_year))
    
    if not rows:
        return f"{name} has no attendance records since {since_year}."
    
    grand_total = 0
    lines = [f"Attendance for {name} since {since_year}:\n"]
    for r in rows:
        days = int(r['days_present'] or 0)
        grand_total += days
        lines.append(f"  {r['yr']}: {days} days present")
    lines.append(f"\n  Total since {since_year}: {grand_total} days present.")
    return "\n".join(lines)
# ══════════════════════════════════════════════════════════════════════════════
# LEAVES
# ══════════════════════════════════════════════════════════════════════════════

def _leaves_year(mid, name, year) -> str:
    if not mid:
        return f"Leave data is not available for {name}."
    rows = hr_query("""
        SELECT
            type_of_leave,
            SUM(DATEDIFF(to_date, from_date) + 1)   AS days_taken
        FROM leaves
        WHERE memberid=%s AND status=1 AND YEAR(from_date)=%s
        GROUP BY type_of_leave
    """, (mid, year))
    if not rows:
        return f"{name} has no approved leave records for {year}."
    lines = [f"Leave taken by {name} in {year}:\n"]
    total = 0
    for r in rows:
        days = int(r['days_taken'] or 0)
        lines.append(f"  {r['type_of_leave']}: {days} day{'s' if days != 1 else ''}")
        total += days
    lines.append(f"\n  Total: {total} leave days in {year}.")
    return "\n".join(lines)

# Add to query_router.py after _leaves_year():

def _leaves_since_year(mid, name, since_year) -> str:
    """Aggregate leave data from since_year to current year."""
    if not mid:
        return f"Leave data is not available for {name}."
    from datetime import date as _date
    current_year = _date.today().year
    years = list(range(since_year, current_year + 1))
    
    rows = hr_query("""
        SELECT YEAR(from_date) AS yr,
               type_of_leave,
               SUM(DATEDIFF(to_date, from_date) + 1) AS days_taken
        FROM leaves
        WHERE memberid=%s AND status=1 AND YEAR(from_date) >= %s
        GROUP BY YEAR(from_date), type_of_leave
        ORDER BY yr, type_of_leave
    """, (mid, since_year))
    
    if not rows:
        return f"{name} has no approved leave records since {since_year}."
    
    # Group by year
    year_data = {}
    for r in rows:
        yr = r['yr']
        if yr not in year_data:
            year_data[yr] = {}
        year_data[yr][r['type_of_leave']] = int(r['days_taken'] or 0)
    
    grand_total = 0
    lines = [f"Leave taken by {name} since {since_year}:\n"]
    for yr in sorted(year_data.keys()):
        yr_total = sum(year_data[yr].values())
        grand_total += yr_total
        bd = ", ".join(f"{k}: {v}d" for k, v in year_data[yr].items())
        lines.append(f"  {yr}: {yr_total} days ({bd})")
    
    lines.append(f"\n  Total since {since_year}: {grand_total} leave days.")
    return "\n".join(lines)

def _compare_leaves(mid, name, years) -> str:
    if not mid:
        return f"Leave data is not available for {name}."
    lines = [
        f"Leave comparison for {name} "
        f"({' vs '.join(str(y) for y in sorted(years))}):\n"
    ]
    for year in sorted(years):
        rows = hr_query("""
            SELECT SUM(DATEDIFF(to_date, from_date) + 1) AS total_days
            FROM leaves
            WHERE memberid=%s AND status=1 AND YEAR(from_date)=%s
        """, (mid, year))
        days = int(rows[0]['total_days'] if rows and rows[0] and rows[0]['total_days'] else 0)
        lines.append(f"  {year}: {days} leave day{'s' if days != 1 else ''} taken.")
    return "\n".join(lines)

def _leave_entitlements(mid, name, can=True) -> str:
    rows = hr_query("""
        SELECT ml.type_of_leave, ml.max_leaves
        FROM max_leaves ml
        WHERE ml.memberid=%s
    """, (mid,))
    
    if not rows:
        # Fall back to role-based entitlements
        rows = hr_query("""
            SELECT ml.type_of_leave, ml.max_leaves
            FROM max_leaves ml
            JOIN role r ON r.memberid=%s
            WHERE ml.memberid = r.memberid
        """, (mid,))
    
    if not rows:
        return f"No leave entitlement data found for {name}."
    
    if can:
        lines = [f"{r['type_of_leave']}: up to {r['max_leaves']} days" for r in rows]
        return f"{name} is entitled to: {', '.join(lines)}."
    else:
        # "can't take" — this needs policy context, not just DB data
        return (
            f"Leave types not listed in {name}'s entitlements "
            f"would require special approval."
        )
# ══════════════════════════════════════════════════════════════════════════════
# PUBLICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _publications_year(uid, name, year) -> str:
    if not uid:
        return f"Publication data is not available for {name}."
    rows = slots_query("""
        SELECT COUNT(*) AS total, GROUP_CONCAT(title SEPARATOR ' | ') AS titles
        FROM paper_publish
        WHERE memberid=%s AND approve=1 AND year=%s
    """, (uid, year))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"{name} has no approved publications recorded for {year}."
    total = int(rows[0]['total'])
    result = f"{name} has {total} approved {'publication' if total==1 else 'publications'} in {year}."
    if rows[0]['titles'] and total <= 5:
        titles = rows[0]['titles'].split(' | ')
        result += " Titles: " + "; ".join(f'"{t}"' for t in titles) + "."
    return result


def _compare_publications(uid, name, years) -> str:
    if not uid:
        return f"Publication data is not available for {name}."
    lines = [
        f"Publication comparison for {name} "
        f"({' vs '.join(str(y) for y in sorted(years))}):\n"
    ]
    for year in sorted(years):
        rows = slots_query(
            "SELECT COUNT(*) AS total FROM paper_publish "
            "WHERE memberid=%s AND approve=1 AND year=%s",
            (uid, year)
        )
        total = int(rows[0]['total'] if rows and rows[0] else 0)
        lines.append(f"  {year}: {total} approved {'publication' if total==1 else 'publications'}.")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# CANCELLATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _cancellation_summary(uid, name) -> str:
    if not uid:
        return f"Cancellation data is not available for {name}."
    rows = slots_query("""
        SELECT
            COUNT(*)                        AS total,
            COUNT(DISTINCT machid)          AS tools_affected,
            MAX(cancel_time)                AS last_cancellation
        FROM cancel_reservation
        WHERE memberid=%s
    """, (uid,))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"{name} has no reservation cancellations on record."
    r = rows[0]
    result = (
        f"{name} has {r['total']} reservation "
        f"{'cancellation' if r['total']==1 else 'cancellations'} on record, "
        f"affecting {r['tools_affected'] or 0} "
        f"{'tool' if (r['tools_affected'] or 0)==1 else 'tools'}."
    )
    if r['last_cancellation']:
        result += f" Most recent cancellation: {str(r['last_cancellation'])[:10]}."
    return result


# ══════════════════════════════════════════════════════════════════════════════
# TRAINING
# ══════════════════════════════════════════════════════════════════════════════

def _training_summary(uid, name) -> str:
    if not uid:
        return f"Training data is not available for {name}."
    rows = slots_query("""
        SELECT
            COUNT(*)                AS total,
            COUNT(DISTINCT machid)  AS tools_trained
        FROM training_report
        WHERE memberid=%s
    """, (uid,))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"{name} has no training sessions on record."
    r = rows[0]
    return (
        f"{name} has completed {r['total']} equipment training "
        f"{'session' if r['total']==1 else 'sessions'} "
        f"across {r['tools_trained'] or 0} "
        f"{'piece' if (r['tools_trained'] or 0)==1 else 'pieces'} of equipment."
    )

def _monthly_training(uid, name, year) -> str:
    rows = slots_query("""
        SELECT MONTH(date) AS month, COUNT(*) AS total
        FROM training_report
        WHERE memberid=%s AND YEAR(date)=%s
        GROUP BY MONTH(date)
        ORDER BY month
    """, (uid, year))
    
    if not rows:
        return f"{name} has no training records for {year}."
    
    lines = [
        f"  {MONTH_DISPLAY[r['month']]}: {r['total']} session{'s' if r['total']!=1 else ''}"
        for r in rows
    ]
    total = sum(r['total'] for r in rows)
    return (
        f"Monthly training breakdown for {name} in {year}:\n"
        + "\n".join(lines)
        + f"\n\n  Total: {total} training sessions."
    )
# ══════════════════════════════════════════════════════════════════════════════
# PROJECTS
# ══════════════════════════════════════════════════════════════════════════════

def _project_summary(uid, name) -> str:
    if not uid:
        return f"Project data is not available for {name}."
    rows = slots_query("""
        SELECT
            COUNT(*)                                        AS total,
            SUM(CASE WHEN active=1 THEN 1 ELSE 0 END)      AS active,
            SUM(CASE WHEN active=0 THEN 1 ELSE 0 END)      AS closed
        FROM faculty_projects
        WHERE memberid=%s
    """, (uid,))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"{name} has no faculty projects on record."
    r = rows[0]
    return (
        f"{name} is associated with {r['total']} faculty "
        f"{'project' if r['total']==1 else 'projects'}: "
        f"{r['active'] or 0} currently active, "
        f"{r['closed'] or 0} closed."
    )


# ══════════════════════════════════════════════════════════════════════════════
# PERMISSIONS
# ══════════════════════════════════════════════════════════════════════════════

def _list_permissions(uid, name) -> str:
    if not uid:
        return f"Permission data is not available for {name}."
    rows = slots_query("""
        SELECT r.name AS tool_name
        FROM permissions p
        JOIN resources r ON r.machid = p.machid
        WHERE p.memberid=%s
        ORDER BY r.name
    """, (uid,))
    if not rows:
        return f"{name} has no tool access permissions on record."
    tool_names = [r['tool_name'] for r in rows]
    if len(tool_names) <= 8:
        tool_str = ", ".join(tool_names)
        return (
            f"{name} holds access permissions for {len(tool_names)} "
            f"{'tool' if len(tool_names)==1 else 'tools'}: {tool_str}."
        )
    else:
        first_five = ", ".join(tool_names[:5])
        return (
            f"{name} holds access permissions for {len(tool_names)} tools. "
            f"Includes: {first_five}, and {len(tool_names)-5} more."
        )
# ADD at bottom of query_router.py:

def _attendance_range(mid, name, years) -> str:
    if not mid:
        return f"Attendance data is not available for {name}."
    lines = [f"Attendance for {name} ({years[0]}–{years[-1]}):\n"]
    grand_total = 0
    for year in years:
        rows = hr_query(
            "SELECT COUNT(*) AS days_present FROM user_attendance "
            "WHERE memberid=%s AND YEAR(date)=%s", (mid, year)
        )
        days = int(rows[0]['days_present'] if rows and rows[0] else 0)
        grand_total += days
        lines.append(f"  {year}: {days} days present")
    lines.append(f"\n  Total ({years[0]}–{years[-1]}): {grand_total} days present.")
    return "\n".join(lines)


def _slot_range(uid, name, years) -> str:
    if not uid:
        return f"Slot booking data is not available for {name}."
    lines = [f"Equipment requests for {name} ({years[0]}–{years[-1]}):\n"]
    grand_total = 0
    for year in years:
        rows = slots_query("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS booked
            FROM equipment_usage_approval
            WHERE requestedby=%s AND YEAR(date_of_request)=%s
        """, (uid, year))
        total  = int(rows[0]['total']  if rows and rows[0] else 0)
        booked = int(rows[0]['booked'] if rows and rows[0] else 0)
        grand_total += total
        lines.append(f"  {year}: {total} requests ({booked} slot-booked)")
    lines.append(f"\n  Total ({years[0]}–{years[-1]}): {grand_total} requests.")
    return "\n".join(lines)


def _leaves_range(mid, name, years) -> str:
    if not mid:
        return f"Leave data is not available for {name}."
    lines = [f"Leave taken by {name} ({years[0]}–{years[-1]}):\n"]
    grand_total = 0
    for year in years:
        rows = hr_query(
            "SELECT SUM(DATEDIFF(to_date,from_date)+1) AS total_days "
            "FROM leaves WHERE memberid=%s AND status=1 AND YEAR(from_date)=%s",
            (mid, year)
        )
        days = int(rows[0]['total_days'] if rows and rows[0] and rows[0]['total_days'] else 0)
        grand_total += days
        lines.append(f"  {year}: {days} leave days")
    lines.append(f"\n  Total ({years[0]}–{years[-1]}): {grand_total} leave days.")
    return "\n".join(lines)


def _reservation_range(uid, name, years) -> str:
    if not uid:
        return f"Reservation data is not available for {name}."
    lines = [f"Slot reservations for {name} ({years[0]}–{years[-1]}):\n"]
    grand_total = 0
    for year in years:
        rows = slots_query(
            "SELECT COUNT(*) AS total FROM reservations "
            "WHERE memberid=%s AND YEAR(FROM_UNIXTIME(startdate))=%s AND isblackout=1",
            (uid, year)
        )
        total = int(rows[0]['total'] if rows and rows[0] else 0)
        grand_total += total
        lines.append(f"  {year}: {total} reservations")
    lines.append(f"\n  Total ({years[0]}–{years[-1]}): {grand_total} reservations.")
    return "\n".join(lines)