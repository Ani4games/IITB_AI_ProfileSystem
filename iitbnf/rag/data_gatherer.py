"""
rag/data_gatherer.py
====================
Detects what structured data a question needs,
fetches it from DB, returns it as a clean dict.

The SLM never touches the DB. It only receives
pre-fetched data and a formatting instruction.
"""

import re
from db import hr_query, slots_query

YEAR_PATTERN = re.compile(r'\b(20\d{2})\b')

def gather(question: str, ctx: dict) -> dict | None:
    """
    Returns {"type": str, "data": dict, "template": str}
    or None if no structured data needed.
    """
    q     = question.lower()
    years = [int(y) for y in YEAR_PATTERN.findall(question)]
    uid   = ctx.get("slot_uid")
    mid   = ctx.get("member_id")
    name  = ctx.get("name", "This person")

    # ── Attendance comparison ──────────────────────────────────
    if len(years) == 2 and any(k in q for k in 
                               ['attend', 'present', 'days']):
        return _gather_attendance_compare(mid, name, years)

    # ── Single year attendance ─────────────────────────────────
    if len(years) == 1 and any(k in q for k in 
                               ['attend', 'present', 'days']):
        return _gather_attendance_year(mid, name, years[0])

    # ── Equipment/slot comparison ──────────────────────────────
    if len(years) == 2 and any(k in q for k in 
                               ['slot', 'equipment', 'request',
                                'booking', 'reservation']):
        return _gather_slot_compare(uid, name, years)

    # ── System ownership ──────────────────────────────────────
    if any(k in q for k in ['system owner', 'owns', 'assigned tool',
                              'responsible for']):
        return _gather_ownership(uid, name)

    # ── Leave breakdown ───────────────────────────────────────
    if any(k in q for k in ['leave', 'casual', 'earned', 'sick']):
        yr = years[0] if years else None
        return _gather_leave(mid, name, yr)

    return None


def _gather_attendance_compare(mid, name, years):
    data = {"name": name, "years": []}
    for yr in sorted(years):
        rows = hr_query(
            "SELECT COUNT(*) AS days FROM user_attendance "
            "WHERE memberid=%s AND YEAR(date)=%s",
            (mid, yr)
        )
        days = int(rows[0]['days'] if rows and rows[0] else 0)
        data["years"].append({"year": yr, "days_present": days})

    y1, y2 = data["years"][0], data["years"][1]
    diff   = y2["days_present"] - y1["days_present"]
    trend  = "more" if diff > 0 else "fewer" if diff < 0 else "same"

    return {
        "type": "attendance_compare",
        "data": data,
        "template": (
            f"EXAMPLE:\n"
            f"name: Alice, year1: 2023 days=180, year2: 2024 days=210\n"
            f"OUTPUT: Alice attended 180 days in 2023 and 210 days in 2024 "
            f"— 30 more days in 2024.\n\n"
            f"NOW FORMAT:\n"
            f"name: {name}, "
            f"year1: {y1['year']} days={y1['days_present']}, "
            f"year2: {y2['year']} days={y2['days_present']}\n"
            f"OUTPUT:"
        )
    }


def _gather_attendance_year(mid, name, year):
    rows = hr_query(
        "SELECT COUNT(*) AS days FROM user_attendance "
        "WHERE memberid=%s AND YEAR(date)=%s",
        (mid, year)
    )
    days = int(rows[0]['days'] if rows and rows[0] else 0)

    return {
        "type": "attendance_year",
        "data": {"name": name, "year": year, "days_present": days},
        "template": (
            f"EXAMPLE:\n"
            f"name: Bob, year: 2024, days_present: 195, mandatory: 239\n"
            f"OUTPUT: Bob was present for 195 working days in 2024.\n\n"
            f"NOW FORMAT:\n"
            f"name: {name}, year: {year}, days_present: {days}\n"
            f"OUTPUT:"
        )
    }


def _gather_slot_compare(uid, name, years):
    data = {"name": name, "years": []}
    for yr in sorted(years):
        rows = slots_query("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS booked,
                SUM(CASE WHEN status=0 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) AS rejected
            FROM equipment_usage_approval
            WHERE requestedby=%s AND YEAR(date_of_request)=%s
        """, (uid, yr))
        r = rows[0] if rows and rows[0] else {}
        data["years"].append({
            "year":    yr,
            "total":   int(r.get('total')   or 0),
            "booked":  int(r.get('booked')  or 0),
            "pending": int(r.get('pending') or 0),
            "rejected":int(r.get('rejected') or 0),
        })

    y1, y2 = data["years"][0], data["years"][1]

    return {
        "type": "slot_compare",
        "data": data,
        "template": (
            f"EXAMPLE:\n"
            f"name: Alice, year1: 2023 total=20 booked=15, "
            f"year2: 2024 total=30 booked=25\n"
            f"OUTPUT: Alice submitted 20 equipment requests in 2023 "
            f"(15 slot-booked) and 30 in 2024 (25 slot-booked) "
            f"— an increase of 10 requests.\n\n"
            f"NOW FORMAT:\n"
            f"name: {name}, "
            f"year1: {y1['year']} total={y1['total']} booked={y1['booked']}, "
            f"year2: {y2['year']} total={y2['total']} booked={y2['booked']}\n"
            f"OUTPUT:"
        )
    }


def _gather_ownership(uid, name):
    rows = slots_query(
        "SELECT machid FROM system_owner WHERE memberid=%s", (uid,)
    ) or []
    
    all_ids = []
    for r in rows:
        raw = str(r.get("machid") or "")
        all_ids += [x.strip() for x in raw.split(",") if x.strip().isdigit()]

    tools = []
    if all_ids:
        ph   = ",".join(["%s"] * len(all_ids))
        tool_rows = slots_query(
            f"SELECT name, isworking FROM resources "
            f"WHERE machid IN ({ph})",
            tuple(all_ids)
        ) or []
        tools = [r['name'] for r in tool_rows]

    tool_list = ", ".join(tools) if tools else "none"

    return {
        "type": "ownership",
        "data": {"name": name, "tools": tools, "count": len(tools)},
        "template": (
            f"EXAMPLE:\n"
            f"name: Bob, tools: PECVD, Sputter\n"
            f"OUTPUT: Bob is currently assigned as system owner "
            f"for 2 tools: PECVD and Sputter.\n\n"
            f"NOW FORMAT:\n"
            f"name: {name}, tools: {tool_list}\n"
            f"OUTPUT:"
        )
    }


def _gather_leave(mid, name, year):
    yr_filter = "AND YEAR(from_date)=%s" if year else ""
    params    = (mid, year) if year else (mid,)
    rows = hr_query(
        f"SELECT type_of_leave, "
        f"SUM(DATEDIFF(to_date,from_date)+1) AS days "
        f"FROM leaves WHERE memberid=%s AND status=1 {yr_filter} "
        f"GROUP BY type_of_leave",
        params
    ) or []

    breakdown = {r['type_of_leave']: int(r['days'] or 0) for r in rows}
    total     = sum(breakdown.values())
    yr_label  = str(year) if year else "overall"
    breakdown_str = ", ".join(f"{k}: {v}" for k, v in breakdown.items())

    return {
        "type": "leave",
        "data": {"name": name, "year": yr_label,
                 "total": total, "breakdown": breakdown},
        "template": (
            f"EXAMPLE:\n"
            f"name: Alice, year: 2024, total: 12, "
            f"breakdown: CL: 5, EL: 7\n"
            f"OUTPUT: Alice took 12 leave days in 2024 "
            f"— 5 casual and 7 earned.\n\n"
            f"NOW FORMAT:\n"
            f"name: {name}, year: {yr_label}, "
            f"total: {total}, breakdown: {breakdown_str}\n"
            f"OUTPUT:"
        )
    }