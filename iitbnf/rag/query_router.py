"""
query_router.py — Structured query handler
Intercepts questions that need live DB data (year comparisons,
specific counts, date ranges) before they reach the SLM.
The SLM only handles truly open-ended questions.
"""
import re
from db import slots_query, hr_query

YEAR_PATTERN = re.compile(r'\b(20\d{2})\b')

SLOT_KEYWORDS    = ['slot', 'equipment', 'booking', 'request', 'reservation', 'machine']
ATTEND_KEYWORDS  = ['attendance', 'present', 'days', 'leave']
PROJECT_KEYWORDS = ['project', 'paper', 'publication']

def route(question: str, ctx: dict) -> str | None:
    """
    Returns a direct answer string if the question can be answered
    deterministically. Returns None to fall through to the SLM.
    """
    q = question.lower()
    years = [int(y) for y in YEAR_PATTERN.findall(question)]
    uid = ctx.get("slot_uid")
    member_id = ctx.get("member_id")  # for HR queries

    # Multi-year comparison
    if len(years) >= 2:
        if any(k in q for k in SLOT_KEYWORDS):
            return _compare_slot_activity(uid, years)
        if any(k in q for k in ATTEND_KEYWORDS):
            return _compare_attendance(member_id, years)

    # Single year specific query
    if len(years) == 1:
        if any(k in q for k in SLOT_KEYWORDS):
            return _slot_activity_year(uid, years[0])
        if any(k in q for k in ATTEND_KEYWORDS):
            return _attendance_year(member_id, years[0])
    
    # # Reservation comparison
    # if any(k in q for k in ['reservation', 'booked slot']):
    #     if len(years) >= 2:
    #         return _compare_reservations(uid, years)
    #     elif len(years) == 1:
    #         return _reservations_year(uid, years[0])

    # # Tool-specific questions
    # tool_match = re.search(r'(pecvd|lpcvd|sputte|lithograph|evapor|etch)', q, re.I)
    # if tool_match and uid:
    #     return _tool_specific_usage(uid, tool_match.group(1), years)

    # # Monthly breakdown
    # if 'month' in q and len(years) == 1:
    #     return _monthly_breakdown(uid, years[0])

    return None  # fall through to SLM


def _compare_slot_activity(uid, years):
    if not uid:
        return "Slot booking data is not available for this profile."
    lines = [f"Slot activity comparison for {', '.join(str(y) for y in sorted(years))}:"]
    for year in sorted(years):
        rows = slots_query("""
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)      AS slot_booked,
                SUM(CASE WHEN status=1 THEN 1 ELSE 0 END)      AS approved,
                SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)      AS pending,
                SUM(CASE WHEN status=2 THEN 1 ELSE 0 END)      AS rejected
            FROM equipment_usage_approval
            WHERE requestedby=%s AND YEAR(date_of_request)=%s
        """, (uid, year))
        if rows and rows[0] and rows[0]['total']:
            r = rows[0]
            lines.append(
                f"  {year}: {r['total']} total requests — "
                f"{r['slot_booked'] or 0} slot-booked, "
                f"{r['approved'] or 0} approved, "
                f"{r['pending'] or 0} pending, "
                f"{r['rejected'] or 0} rejected."
            )
        else:
            lines.append(f"  {year}: No equipment request data found.")
    return "\n".join(lines)


def _slot_activity_year(uid, year):
    if not uid:
        return "Slot booking data is not available for this profile."
    rows = slots_query("""
        SELECT
            COUNT(*)                                        AS total,
            SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)      AS slot_booked,
            SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)      AS pending,
            SUM(CASE WHEN status=2 THEN 1 ELSE 0 END)      AS rejected,
            COUNT(DISTINCT equipmentid)                     AS tools_used
        FROM equipment_usage_approval
        WHERE requestedby=%s AND YEAR(date_of_request)=%s
    """, (uid, year))
    if not rows or not rows[0] or not rows[0]['total']:
        return f"No slot activity data found for {year}."
    r = rows[0]
    return (
        f"In {year}: {r['total']} equipment requests across "
        f"{r['tools_used'] or 0} tools — "
        f"{r['slot_booked'] or 0} slot-booked, "
        f"{r['pending'] or 0} pending, "
        f"{r['rejected'] or 0} rejected."
    )


def _attendance_year(member_id, year):
    if not member_id:
        return "Attendance data is not available."
    rows = hr_query("""
        SELECT COUNT(*) AS days_present
        FROM user_attendance
        WHERE memberid=%s AND YEAR(date)=%s
    """, (member_id, year))
    days = int(rows[0]['days_present'] if rows else 0)
    return f"In {year}, {days} days were recorded as present."


def _compare_attendance(member_id, years):
    if not member_id:
        return "Attendance data is not available."
    lines = [f"Attendance comparison for {', '.join(str(y) for y in sorted(years))}:"]
    for year in sorted(years):
        rows = hr_query("""
            SELECT COUNT(*) AS days_present
            FROM user_attendance
            WHERE memberid=%s AND YEAR(date)=%s
        """, (member_id, year))
        days = int(rows[0]['days_present'] if rows else 0)
        lines.append(f"  {year}: {days} days present.")
    return "\n".join(lines)