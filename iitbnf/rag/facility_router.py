"""
rag/facility_router.py — Facility knowledge query handler
==========================================================
Handles questions about the facility itself — teams, process,
equipment categories, policies — without hitting the SLM.

These are deterministic answers from the knowledge base.
Falls through to RAG/SLM for questions not covered here.
"""

import re
import logging
from db import slots_query

logger = logging.getLogger(__name__)

# ── Keyword groups ────────────────────────────────────────────────────────────
ABOUT_KEYWORDS = ['what is iitbnf','about iitbnf','about the facility',
                  'what is this facility','tell me about iitbnf',
                  'where is iitbnf','iitbnf located','facility location',
                  'where is iit bombay nanofabrication',   
                  'where is the facility',                 
                  'location of iitbnf',                    
                  'address of iitbnf',                     
                  'iit bombay nanofabrication facility',
                  'working days of iitbnf','iitbnf working days',
                  'iitbnf timing','iitbnf hours','iitbnf open',
                  'timings', 'what time does', 'when is iitbnf open',
                  'opening hours', 'working hours', 'operating hours'] 

TEAM_KEYWORDS     = ['team','staff','who works','roles','positions',
                     'hr team','it team','faculty incharge',
                     'who manages','who is responsible']

PROCESS_KEYWORDS  = ['how to book','booking process','how do i request',
                     'how to use','register','registration process',
                     'how to access','get access','apply for access',
                     'slot booking','equipment booking']

EQUIPMENT_KEYWORDS= ['what equipment','list of equipment','which machine',
                     'available tool','what tools','equipment available',
                     'what can i use','facility equipment','cleanroom tool']

POLICY_KEYWORDS   = ['attendance policy','leave policy','75 percent',
                     'mandatory attendance','threshold','leave type',
                     'how many leaves','working hours','operating hours',
                     'working days','work hours','opening hours','open hours',
                     'timing','timings','working time','what time','open time',
                     'facility hours','lab hours','when is','when does']

CONTACT_KEYWORDS  = ['contact','who to contact','reach out','support',
                     'help desk','report issue','equipment problem',
                     'access problem','who do i talk']

USER_CAT_KEYWORDS = ['user category','what is phd user','what is mtech',
                     'inup','industry user','pdf user','project staff',
                     'type of user','user type']

# Keywords that signal the user wants the LIVE equipment list (names from DB)
# rather than the static category overview.
EQUIPMENT_LIST_KEYWORDS = [
    'list all equipment', 'list equipment', 'show all equipment',
    'show equipment', 'all tools', 'all machines', 'all instruments',
    'name of equipment', 'names of equipment', 'which equipment do you have',
    'which tools are there', 'what machines are there',
]

# Keywords that signal a question about ONE specific named tool
# (status, capability, working condition) rather than the full catalog.
TOOL_DETAIL_KEYWORDS = [
    'tell me about', 'what is', 'how does', 'what does',
    'is working', 'is down', 'status of', 'working condition',
    'specs of', 'capability of', 'used for',
]

# Known tool/category name fragments — used to detect that a question is
# about a SPECIFIC instrument rather than the facility in general.
TOOL_NAME_HINTS = [
    'pecvd', 'lpcvd', 'sputter', 'evaporat', 'rie', 'icp', 'wet bench',
    'wet etch', 'sem', 'tem', 'afm', 'xrd', 'xps', 'edx',
    'furnace', 'rta', 'anneal', 'profilometer', 'ellipsometer',
    'mask aligner', 'spin coater', 'e-beam', 'ebeam lithograph',
    'lithograph', 'iv measurement', 'cv measurement',
]


def _has_any(question: str, keywords: list) -> bool:
    q = question.lower()
    return any(k in q for k in keywords)


def route_facility(question: str) -> str | None:
    q = question.lower().strip()

    try:
        # ── Live DB stats FIRST — before keyword groups ────────────────────
        # These must come before TEAM_KEYWORDS (which contains 'staff') and
        # PROCESS_KEYWORDS (which contains 'register') to avoid being
        # shadowed by those broader keyword matches.
        if 'how many staff' in q or 'total staff' in q or 'active staff' in q or 'how many staff member' in q:
            return _live_staff_count()

        if 'how many user' in q or 'total user' in q or 'how many lab user' in q or 'active user' in q or 'how many registered' in q:
            return _live_user_count()

        if 'how many equipment' in q or 'total equipment' in q or 'how many tool' in q:
            return _live_equipment_count()

        # ── Live equipment LIST (real tool names from DB) ───────────────────
        if _has_any(q, EQUIPMENT_LIST_KEYWORDS):
            return _live_equipment_list()

        # ── Specific named tool — live status/detail lookup ─────────────────
        tool_hint = next((h for h in TOOL_NAME_HINTS if h in q), None)
        if tool_hint and _has_any(q, TOOL_DETAIL_KEYWORDS + ['equipment', 'tool', 'machine']):
            return _live_tool_detail(tool_hint)

        if any(k in q for k in ['working day', 'work day', 'working hour', 'work hour',
                                'opening hour', 'open hour', 'timing', 'what time',
                                'when open', 'when does iitbnf', 'facility hour',
                                'operating hour', 'lab hour']):
            return _attendance_policy()

        # ── Keyword groups after ───────────────────────────────────────────
        if _has_any(q, ABOUT_KEYWORDS):
            return _about_facility()

        if _has_any(q, TEAM_KEYWORDS):
            return _about_teams()

        if _has_any(q, PROCESS_KEYWORDS):
            return _booking_process()

        if _has_any(q, EQUIPMENT_KEYWORDS):
            return _equipment_overview()

        if _has_any(q, POLICY_KEYWORDS):
            return _attendance_policy()

        if _has_any(q, CONTACT_KEYWORDS):
            return _contact_guide()

        if _has_any(q, USER_CAT_KEYWORDS):
            return _user_categories()

    except Exception as e:
        logger.error("[FacilityRouter] Error: %s", e)
        return None

    return None


# ── Static knowledge answers ──────────────────────────────────────────────────

def _about_facility() -> str:
    return (
        "IITBNF (IIT Bombay Nanofabrication Facility) is located at "
        "IIT Bombay, Powai, Mumbai — 400076, Maharashtra, India. "
        "It is a state-of-the-art Class 100/1000 cleanroom facility "
        "providing fabrication and characterization services to researchers "
        "from IIT Bombay and external institutions under programs like NPMASS "
        "and NCPRE. The facility operates Monday to Friday, 9:00 AM to 6:00 PM."
    )


def _about_teams() -> str:
    return (
        "IITBNF has the following roles and teams:\n\n"
        "  IITBNF Staff    — Core facility staff managing lab operations and equipment.\n"
        "  Faculty         — Supervisors overseeing research projects.\n"
        "  System Owner    — Staff assigned responsibility for specific equipment.\n"
        "  HR Admin/Team   — Manages attendance, leave, and personnel records.\n"
        "  IT Admin/Team   — Manages the slotbooking system and user accounts.\n"
        "  Attendance Team — Uploads and verifies daily attendance records.\n\n"
        "Each piece of equipment has a designated System Owner who coordinates "
        "maintenance and handles operational issues."
    )


def _booking_process() -> str:
    return (
        "Equipment booking process at IITBNF:\n\n"
        "  Step 1 — Submit an equipment usage request through the slotbooking portal.\n"
        "  Step 2 — The system owner or faculty incharge reviews and approves the request.\n"
        "  Step 3 — Once approved, your slot booking is confirmed.\n"
        "  Step 4 — Use the equipment during your booked slot.\n"
        "  Step 5 — Submit a session report after equipment use.\n\n"
        "For new users: Register through the portal first. "
        "Registration requires supervisor details and project/course information. "
        "Access is granted after review and approval by facility staff."
    )


def _equipment_overview() -> str:
    return (
        "IITBNF equipment categories:\n\n"
        "  Deposition      — PECVD, LPCVD, Sputtering, Evaporation systems\n"
        "  Lithography     — Spin coaters, Mask aligners, E-beam lithography\n"
        "  Etching         — RIE, ICP etching, Wet bench stations\n"
        "  Characterization— SEM, TEM, AFM, XRD, XPS, EDX systems\n"
        "  Thermal         — Diffusion furnaces, RTA, Anneal ovens\n"
        "  Metrology       — Profilometers, Ellipsometers, IV measurement\n\n"
        "Each equipment has a System Owner responsible for its operation. "
        "Access to specific equipment requires approved permissions."
    )


def _attendance_policy() -> str:
    return (
        "IITBNF attendance and leave policy:\n\n"
        "  Working hours       : 9:00 AM to 6:00 PM.\n"
        "  Break time          : 1 hour for lunch, between 1 pm to 2 pm.\n"
        "  Opening hours       : 9:00 AM to 5:00 PM.\n"
        "  Mandatory threshold : 75% attendance of working days per year.\n"
        "  Staff below 75%     : May be flagged for management review.\n"
        "  Working days        : Monday to Friday, excluding institute holidays.\n\n"
        "Leave types available:\n"
        "  CL — Casual Leave\n"
        "  EL — Earned Leave\n"
        "  ML — Medical Leave\n"
        "  RL — Restricted Leave\n\n"
        "Attendance is recorded daily and tracked on an annual basis."
    )


def _contact_guide() -> str:
    return (
        "Who to contact for common issues at IITBNF:\n\n"
        "  Equipment problems  — Contact the System Owner for that equipment.\n"
        "  Access/login issues — Contact IT Admin through the portal.\n"
        "  Attendance queries  — Contact the HR Team.\n"
        "  Registration help   — Contact facility staff directly.\n"
        "  Leave applications  — Submit through the HR portal.\n"
        "  Slot booking help   — Contact IT Team or facility staff."
    )


def _user_categories() -> str:
    return (
        "IITBNF user categories:\n\n"
        "  Ph.D          — Doctoral researchers working towards a PhD.\n"
        "  M.Tech        — Postgraduate students pursuing M.Tech.\n"
        "  M.Tech RA     — M.Tech students on research assistantship.\n"
        "  B.Tech        — Undergraduate students on project internship.\n"
        "  INUP          — Visiting researchers under the INUP programme.\n"
        "  PDF           — Postdoctoral fellows conducting research.\n"
        "  Industry User — Industry users accessing facility for commercial R&D.\n"
        "  Project Staff — Staff supporting research operations on project basis.\n"
        "  Faculty       — Faculty members supervising research."
    )


# ── Live DB queries for facility stats ────────────────────────────────────────

def _live_staff_count() -> str:
    from db import hr_query
    rows = hr_query("""
        SELECT COUNT(*) AS total FROM profile
        WHERE (taken_clearance IS NULL OR taken_clearance = 0)
          AND (leaving_date IS NULL OR leaving_date = '0000-00-00'
               OR leaving_date >= CURDATE())
    """)
    total = int(rows[0]['total'] if rows and rows[0] else 0)
    return f"IITBNF currently has {total} active staff members on record."


def _live_user_count() -> str:
    rows = slots_query("""
        SELECT COUNT(*) AS total FROM login
        WHERE STR_TO_DATE(expiry_date, '%m/%d/%Y') >= CURDATE()
    """)
    total = int(rows[0]['total'] if rows and rows[0] else 0)
    return f"IITBNF currently has {total} active registered lab users."


def _live_equipment_count() -> str:
    rows = slots_query("""
        SELECT
            COUNT(*)                                        AS total,
            SUM(CASE WHEN isworking=1 THEN 1 ELSE 0 END)   AS working,
            SUM(CASE WHEN isworking=0 THEN 1 ELSE 0 END)   AS down
        FROM resources
    """)
    if not rows or not rows[0]:
        return "Equipment count data is not available."
    r = rows[0]
    return (
        f"IITBNF has {r['total']} pieces of equipment registered: "
        f"{r['working'] or 0} currently operational, "
        f"{r['down'] or 0} currently down for maintenance."
    )


def _live_equipment_list() -> str:
    """
    Returns the full live list of equipment names grouped by category,
    pulled directly from slotbooking.resources. Falls back to the static
    category overview if the DB query fails or returns nothing.
    """
    try:
        rows = slots_query("""
            SELECT name, category, isworking
            FROM resources
            ORDER BY category, name
        """) or []
    except Exception as e:
        logger.error("[FacilityRouter] _live_equipment_list DB error: %s", e)
        rows = []

    if not rows:
        return _equipment_overview()

    grouped: dict[str, list[str]] = {}
    for r in rows:
        cat = r.get("category") or "Other"
        tag = r["name"] if r.get("isworking") else f"{r['name']} (down)"
        grouped.setdefault(cat, []).append(tag)

    lines = [f"IITBNF currently has {len(rows)} pieces of registered equipment:\n"]
    for cat, names in grouped.items():
        lines.append(f"  {cat}: " + ", ".join(names))
    lines.append(
        "\nFor details on a specific instrument, ask e.g. "
        "\"tell me about the PECVD\" or \"is the SEM working?\"."
    )
    return "\n".join(lines)


def _live_tool_detail(tool_hint: str) -> str:
    """
    Returns live details (status, category, location, operator) for a
    specific tool matched by a keyword hint (e.g. 'pecvd', 'sem', 'afm').
    Combines the live DB record with the static catalog description when
    available, so the answer covers both "what it does" and "is it working".
    """
    try:
        rows = slots_query(
            "SELECT name, category, location, type_of_tool, "
            "operator_name, isworking FROM resources "
            "WHERE LOWER(name) LIKE %s LIMIT 5",
            (f"%{tool_hint}%",)
        ) or []
    except Exception as e:
        logger.error("[FacilityRouter] _live_tool_detail DB error: %s", e)
        rows = []

    if not rows:
        return None  # let the caller fall through to RAG/static catalog

    lines = []
    for r in rows:
        status = "operational" if r.get("isworking") else "currently down"
        loc = f" Located in {r['location']}." if r.get("location") else ""
        op = f" Operator: {r['operator_name']}." if r.get("operator_name") else ""
        lines.append(
            f"{r['name']} ({r.get('category') or 'equipment'}) is {status}.{loc}{op}"
        )
    return "\n".join(lines)