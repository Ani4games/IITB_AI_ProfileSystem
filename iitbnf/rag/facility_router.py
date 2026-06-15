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
                  'iit bombay nanofabrication facility']   

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
                     'how many leaves','working hours','operating hours']

CONTACT_KEYWORDS  = ['contact','who to contact','reach out','support',
                     'help desk','report issue','equipment problem',
                     'access problem','who do i talk']

USER_CAT_KEYWORDS = ['user category','what is phd user','what is mtech',
                     'inup','industry user','pdf user','project staff',
                     'type of user','user type']


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
