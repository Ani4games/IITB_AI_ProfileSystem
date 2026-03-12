"""
utils.py — Shared utility functions: parallel execution, date helpers, formatters.
"""
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from db import hr_query, slots_query
from cache import cached


# ── Parallel execution ────────────────────────────────────────────────────────
def run_parallel(tasks, max_workers=3):
    """Run a dict of {name: callable} in parallel. Returns {name: result}."""
    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, 3)) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result(timeout=10)
            except Exception as e:
                print(f"Error in {name}: {e}")
                results[name] = None
    return results


def parallel_profile_load(member_id, functions_dict):
    """Alias kept for backward compatibility."""
    return run_parallel(functions_dict, max_workers=3)


# ── Date / calendar helpers ───────────────────────────────────────────────────
@cached(ttl_seconds=3600)
def get_holidays():
    """Return set of holiday dates from institute_holidays."""
    rows = hr_query("SELECT holiday_date FROM institute_holidays")
    return {r["holiday_date"] for r in rows} if rows else set()


def calc_mandatory_days(year=None):
    """Count Mon-Fri working days for a year, excluding institute holidays."""
    today    = date.today()
    year     = year or today.year
    holidays = get_holidays()
    count, d = 0, date(year, 1, 1)
    end_date = min(date(year, 12, 31), today)
    while d <= end_date:
        if d.weekday() < 5 and d not in holidays:
            count += 1
        d += timedelta(days=1)
    return count


# ── Name / display helpers ────────────────────────────────────────────────────
@cached(ttl_seconds=3600)
def get_display_name(member_id, email):
    """Resolve display name from slotbooking.login. Fallback to member ID."""
    rows = slots_query(
        "SELECT fname, lname FROM login WHERE memberid = %s LIMIT 1", (member_id,)
    )
    if rows:
        name = (rows[0]["fname"] + " " + rows[0]["lname"]).strip()
        if name:
            return name
    if email:
        rows = slots_query(
            "SELECT fname, lname FROM login WHERE LOWER(TRIM(email))=LOWER(TRIM(%s)) LIMIT 1",
            (email,),
        )
        if rows:
            name = (rows[0]["fname"] + " " + rows[0]["lname"]).strip()
            if name:
                return name
    return f"Member #{str(member_id).zfill(4)}"


def clean_role(role_name):
    if not role_name:
        return "Staff"
    return {
        "No Special Permission": "Staff",
        "HR Admin":  "HR Admin",
        "HR Team":   "HR Team",
        "IT Admin":  "IT Admin",
        "IT Team":   "IT Team",
        "Upload Attendance": "Attendance",
    }.get(role_name, role_name)


def safe_dict(d):
    """Coerce all non-primitive values in a dict to strings."""
    return {
        k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
        for k, v in d.items()
    }
