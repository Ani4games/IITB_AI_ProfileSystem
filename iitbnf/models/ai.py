"""
models/ai.py
------------
Handles all AI report generation via Ollama.

Pipeline:
  1. Extract pre-aggregated data using hr_query / slots_query helpers from db.py
  2. Build a structured context dict
  3. Send to Ollama with audience-specific prompt
  4. Return narrative text

Ollama must be running on http://localhost:11434
"""

from flask import ctx
import requests
import logging
from datetime import date
from db import hr_query, slots_query

logger = logging.getLogger(__name__)

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:latest"
TIMEOUT      = 60  # seconds — local model can be slow on first run


# ── PUBLIC ENTRY POINTS ──────────────────────────────────────────────────────

def generate_staff_report(member_id: int, audience: str) -> dict:
    """
    Generate a written profile report for a staff member.

    Args:
        member_id : hr_portal member ID
        audience  : "individual" or "management"

    Returns:
        {"success": True,  "report": "...narrative..."}
        {"success": False, "error": "...reason..."}
    """
    try:
        context = _build_staff_context(member_id)
        if not context:
            return {"success": False, "error": "Could not retrieve member data."}
        report = _call_ollama(context, audience, profile_type="staff")
        return {"success": True, "report": report}
    except Exception as e:
        logger.error(f"Staff report generation failed for member {member_id}: {e}")
        return {"success": False, "error": "Report generation failed. Please try again."}


def generate_lab_report(memberid: int, audience: str) -> dict:
    """
    Generate a written profile report for a lab user.

    Args:
        memberid : slotbooking login.id  (matches template variable 'memberid')
        audience : "individual" or "management"

    Returns:
        {"success": True,  "report": "...narrative..."}
        {"success": False, "error": "...reason..."}
    """
    try:
        context = _build_lab_context(memberid)
        if not context:
            return {"success": False, "error": "Could not retrieve lab user data."}
        report = _call_ollama(context, audience, profile_type="lab")
        return {"success": True, "report": report}
    except Exception as e:
        logger.error(f"Lab report generation failed for user {memberid}: {e}")
        return {"success": False, "error": "Report generation failed. Please try again."}


# ── CONTEXT BUILDERS ─────────────────────────────────────────────────────────

def _build_staff_context(member_id: int) -> dict | None:
    """Pull and aggregate all profile-relevant data for a staff member."""
    ctx = {}

    # ── Basic identity ────────────────────────────────────────────
    rows = hr_query("""
    SELECT p.designation, p.email, p.joining_date, p.team,
           COALESCE(rm.role_name, 'Staff') AS role_name,
           TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS full_name
    FROM   profile p
    LEFT JOIN role r          ON r.memberid  = p.member_id
    LEFT JOIN role_master rm  ON rm.role_id  = r.role
    LEFT JOIN slotbooking.login l ON l.memberid = p.member_id
    WHERE  p.member_id = %s LIMIT 1
""", (member_id,))

    if not rows:
        return None

    row   = rows[0]
    email = row.get("email", "")

    full_name = (row.get("full_name") or "").strip()
    if not full_name:
        full_name = f"Member #{str(member_id).zfill(4)}"

    ctx["name"]         = full_name
    ctx["designation"]  = row.get("designation", "N/A")
    ctx["role"]         = row.get("role_name", "N/A")
    ctx["team"]         = row.get("team", "N/A")
    ctx["joining_date"] = str(row.get("joining_date", "N/A"))

    # ── Attendance ────────────────────────────────────────────────
    try:
        year = date.today().year

        att = hr_query("""
            SELECT COUNT(*) AS days_present
            FROM   user_attendance
            WHERE  memberid = %s AND YEAR(date) = %s
        """, (member_id, year))

        hol = hr_query("""
            SELECT COUNT(*) AS holidays
            FROM   institute_holidays
            WHERE  YEAR(holiday_date) = %s
        """, (year,))

        lv = hr_query("""
    SELECT COALESCE(SUM(DATEDIFF(to_date, from_date) + 1), 0) AS leaves_taken
    FROM   leaves
    WHERE  memberid = %s AND status = 1 AND YEAR(from_date) = %s
""", (member_id, year))

        days_present = att[0]["days_present"] if att else 0
        holidays     = hol[0]["holidays"]     if hol else 0
        leaves_taken = lv[0]["leaves_taken"]  if lv  else 0

        working_days = max(1, (date.today() - date(year, 1, 1)).days + 1 - holidays)
        att_pct      = round((days_present / working_days) * 100, 1)

        ctx["attendance_pct"] = att_pct
        ctx["days_present"]   = days_present
        ctx["working_days"]   = working_days
        ctx["leaves_taken"]   = int(leaves_taken)
    except Exception as e:
        logger.warning(f"Attendance query failed: {e}")
        ctx["attendance_pct"] = "N/A"

    # ── Performance ───────────────────────────────────────────────
    try:
        perf = hr_query("""
            SELECT AVG(performance_score) AS avg_rating, COUNT(*) AS review_count
            FROM   performance_rating
            WHERE  member_id = %s
        """, (member_id,))

        appr = hr_query("""
            SELECT AVG(CAST(value AS DECIMAL(5,2))) AS appraisal_avg
            FROM   360degree_appraisal_data
            WHERE  appraisal_of = %s
              AND  TRIM(value) REGEXP '^[0-9]+$'
        """, (member_id,))

        comm = hr_query("""
            SELECT COUNT(*) AS committee_count
            FROM   committee_members cm
            JOIN   profile p ON p.email = cm.email
            WHERE  p.member_id = %s
        """, (member_id,))

        ctx["performance_rating"] = round(float(perf[0]["avg_rating"]), 2)  if perf and perf[0]["avg_rating"]  else "N/A"
        ctx["review_cycles"]      = perf[0]["review_count"]                  if perf                            else 0
        ctx["appraisal_avg"]      = round(float(appr[0]["appraisal_avg"]), 2) if appr and appr[0]["appraisal_avg"] else "N/A"
        ctx["committee_count"]    = comm[0]["committee_count"]                if comm                            else 0
    except Exception as e:
        logger.warning(f"Performance query failed: {e}")

    # ── Monthly reports ───────────────────────────────────────────
    try:
        mr = hr_query("""
            SELECT COUNT(*) AS submitted, AVG(star) AS avg_stars
            FROM   monthly_report
            WHERE  member_id = %s
        """, (member_id,))

        ctx["monthly_reports_submitted"] = mr[0]["submitted"] if mr else 0
        ctx["monthly_report_avg_stars"]  = round(float(mr[0]["avg_stars"]), 1) if mr and mr[0]["avg_stars"] else "N/A"
    except Exception as e:
        logger.warning(f"Monthly report query failed: {e}")

    # ── Equipment / slotbooking (via email lookup) ────────────────
    try:
        slot = slots_query("SELECT memberid as id FROM login WHERE email = %s", (email,))
        if slot:
            slot_id = slot[0]["id"]
# get name from registration
            reg = slots_query("SELECT name FROM registration WHERE email = %s", (email,))
            if reg:
                ctx["name"] = reg[0].get("name", "Unknown")
            eq = slots_query("""
                SELECT COUNT(DISTINCT resource_id) AS tools_used,
                       COUNT(*)                    AS total_bookings
                FROM   reservations WHERE user_id = %s
            """, (slot_id,))

            tr = slots_query("SELECT COUNT(*) AS trainings FROM training_report  WHERE user_id = %s", (slot_id,))
            pp = slots_query("SELECT COUNT(*) AS papers    FROM paper_publish    WHERE user_id = %s", (slot_id,))
            fp = slots_query("SELECT COUNT(*) AS projects  FROM faculty_projects WHERE user_id = %s", (slot_id,))

            ctx["tools_used"]     = eq[0]["tools_used"]    if eq else 0
            ctx["total_bookings"] = eq[0]["total_bookings"] if eq else 0
            ctx["trainings"]      = tr[0]["trainings"]      if tr else 0
            ctx["papers"]         = pp[0]["papers"]         if pp else 0
            ctx["projects"]       = fp[0]["projects"]       if fp else 0
    except Exception as e:
        logger.warning(f"Slotbooking context query failed: {e}")

    return ctx


def _build_lab_context(memberid: int) -> dict | None:
    """Pull and aggregate all profile-relevant data for a lab user."""
    ctx = {}

    # ── Basic identity ────────────────────────────────────────────
    rows = slots_query("""
        SELECT l.fname, l.lname, l.email,
            l.position AS category
        FROM   login l
        WHERE  l.memberid = %s LIMIT 1
    """, (memberid,))

    if not rows:
        return None

    row = rows[0]
    ctx["name"]      = (row.get("fname","") + " " + row.get("lname","")).strip() or f"User #{memberid}"
    ctx["category"]  = row.get("category", "N/A")

    # ── Usage data ────────────────────────────────────────────────
    try:
        eq = slots_query("""
            SELECT COUNT(DISTINCT resource_id) AS tools_used,
                   COUNT(*)                    AS total_bookings
            FROM   reservations WHERE user_id = %s
        """, (memberid,))

        tr = slots_query("SELECT COUNT(*) AS trainings FROM training_report              WHERE user_id = %s", (memberid,))
        pp = slots_query("SELECT COUNT(*) AS papers    FROM paper_publish                WHERE user_id = %s", (memberid,))
        fp = slots_query("SELECT COUNT(*) AS projects  FROM faculty_projects             WHERE user_id = %s", (memberid,))
        lu = slots_query("SELECT COALESCE(SUM(hours_used), 0) AS total_hours FROM limit_usage WHERE user_id = %s", (memberid,))
        ap = slots_query("SELECT COUNT(*) AS approved  FROM equipment_usage_approval     WHERE user_id = %s AND status = 3", (memberid,))

        ctx["tools_used"]        = eq[0]["tools_used"]         if eq else 0
        ctx["total_bookings"]    = eq[0]["total_bookings"]      if eq else 0
        ctx["trainings"]         = tr[0]["trainings"]           if tr else 0
        ctx["papers"]            = pp[0]["papers"]              if pp else 0
        ctx["projects"]          = fp[0]["projects"]            if fp else 0
        ctx["total_hours_used"]  = float(lu[0]["total_hours"])  if lu else 0
        ctx["approved_requests"] = ap[0]["approved"]            if ap else 0
    except Exception as e:
        logger.warning(f"Lab usage context query failed: {e}")

    return ctx


# ── OLLAMA CALL ───────────────────────────────────────────────────────────────

def _call_ollama(context: dict, audience: str, profile_type: str) -> str:
    """Send context to Ollama and return the generated narrative."""
    prompt = _build_prompt(context, audience, profile_type)

    payload = {
        "model":  OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.4,
            "num_predict": 300,
        }
    }

    resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _build_prompt(context: dict, audience: str, profile_type: str) -> str:
    """Build the prompt string from context and audience."""
    tone = (
        "a formal, third-person management review report"
        if audience == "management" else
        "an encouraging, first-person professional profile for the individual themselves"
    )
    ptype_label = "staff member" if profile_type == "staff" else "lab user"

    data_block = "\n".join(
        f"  - {k.replace('_', ' ').title()}: {v}"
        for k, v in context.items()
    )

    return f"""You are writing {tone} for a {ptype_label} at the IIT Bombay Nanofabrication Facility (IITBNF).

Use only the data provided below. Do not invent or assume any information not present.
Write exactly 4 to 6 sentences. Be specific — use the actual numbers from the data.
Do not include any headings, bullet points, or formatting — plain prose only.

Data:
{data_block}

Write the profile report now:"""
