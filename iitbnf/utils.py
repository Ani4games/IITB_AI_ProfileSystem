"""
utils.py — Shared utility functions: parallel execution, date helpers, formatters.

Performance fixes applied
─────────────────────────
1. get_holidays() is now cached for 1 hour with a module-level in-process
   cache.  Previously it fired "SELECT holiday_date FROM institute_holidays"
   on EVERY call with no caching at all.  It is called inside
   calc_mandatory_days(), which in turn is called by:
     • get_attendance_stats()  (every profile load)
     • get_attendance_trend()  (12 iterations, once per month)
     • get_comparative_stats() (team comparison block)
   On a profile load with year dropdown change this was 13+ identical DB
   queries per request.  Now it is 1 query per hour.

2. calc_mandatory_days() is also cached — same (year) key — so the day-loop
   itself (up to 366 iterations) only runs once per year per process restart.
"""
import time as _time
import threading
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from db import hr_query, slots_query


# ── Holiday cache ─────────────────────────────────────────────────────────────
_holiday_cache: set | None   = None
_holiday_cache_ts: float     = 0.0
_holiday_cache_lock          = threading.Lock()
_HOLIDAY_TTL                 = 3600   # 1 hour — holidays rarely change mid-day

# mandatory-days cache: {year: count}
_mandatory_cache: dict       = {}
_mandatory_cache_lock        = threading.Lock()


def get_holidays() -> set:
    """
    Return set of holiday dates from institute_holidays.
    Result is cached in-process for 1 hour — the table almost never changes
    and querying it 13+ times per profile request was a major bottleneck.
    """
    global _holiday_cache, _holiday_cache_ts
    now = _time.monotonic()
    with _holiday_cache_lock:
        if _holiday_cache is not None and (now - _holiday_cache_ts) < _HOLIDAY_TTL:
            return _holiday_cache

    # Cache miss — fetch from DB (outside the lock to avoid blocking other threads)
    rows = hr_query("SELECT holiday_date FROM institute_holidays")
    fresh = {r["holiday_date"] for r in rows} if rows else set()

    with _holiday_cache_lock:
        _holiday_cache    = fresh
        _holiday_cache_ts = _time.monotonic()

    return fresh


def calc_mandatory_days(year: int | None = None) -> int:
    """
    Count Mon-Fri working days for a year, excluding institute holidays.
    Result is cached per year — the day-loop (up to 366 iterations) only
    runs once per year per process lifetime, saving CPU and the holiday DB
    call on every attendance request.
    """
    today = date.today()
    year  = year or today.year

    with _mandatory_cache_lock:
        if year in _mandatory_cache:
            # For the current year the count grows daily — only use the cached
            # value if we already computed it today.
            cached_val, cached_date = _mandatory_cache[year]
            if year < today.year or cached_date == today:
                return cached_val

    holidays = get_holidays()
    count    = 0
    d        = date(year, 1, 1)
    end_date = min(date(year, 12, 31), today)
    while d <= end_date:
        if d.weekday() < 5 and d not in holidays:
            count += 1
        d += timedelta(days=1)

    with _mandatory_cache_lock:
        _mandatory_cache[year] = (count, today)

    return count


# ── Bulk name resolution ──────────────────────────────────────────────────────

def bulk_display_names(member_ids: list) -> dict:
    """Fetch display names for multiple member IDs in one query."""
    if not member_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(member_ids))
    query = f"SELECT memberid, fname, lname FROM login WHERE memberid IN ({placeholders})"
    rows  = slots_query(query, tuple(member_ids))
    return {
        r["memberid"]: (r["fname"] + " " + r["lname"]).strip()
                       or f"Member #{str(r['memberid']).zfill(4)}"
        for r in (rows or [])
    }


# ── Parallel execution ────────────────────────────────────────────────────────

def run_parallel(tasks: dict, max_workers: int = 8) -> dict:
    """Run a dict of {name: callable} in parallel. Returns {name: result}."""
    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, 8)) as executor:
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


# ── Name / display helpers ────────────────────────────────────────────────────

def get_display_name(member_id: int, email: str) -> str:
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
            "SELECT fname, lname FROM login "
            "WHERE LOWER(TRIM(email))=LOWER(TRIM(%s)) LIMIT 1",
            (email,),
        )
        if rows:
            name = (rows[0]["fname"] + " " + rows[0]["lname"]).strip()
            if name:
                return name
    return f"Member #{str(member_id).zfill(4)}"


def clean_role(role_name: str | None) -> str:
    if not role_name:
        return "Staff"
    return {
        "No Special Permission": "Staff",
        "HR Admin":              "HR Admin",
        "HR Team":               "HR Team",
        "IT Admin":              "IT Admin",
        "IT Team":               "IT Team",
        "Upload Attendance":     "Attendance",
    }.get(role_name, role_name)


def safe_dict(d: dict) -> dict:
    """Coerce all non-primitive values in a dict to strings."""
    return {
        k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
        for k, v in d.items()
    }
