"""
models/ai.py
------------
Template-based profile generation. No external AI dependencies.

Pipeline:
  1. _build_staff_context / _build_lab_context  — pull DB data
  2. _narrative_staff / _narrative_lab           — generate prose sections
  3. generate_staff_report / generate_lab_report — public entry points
"""

import logging
from datetime import date
from db import hr_query, slots_query
from utils import calc_mandatory_days

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS
# ══════════════════════════════════════════════════════════════════════════════

def generate_staff_report(member_id: int, audience: str = "management") -> dict:
    try:
        context = _build_staff_context(member_id)
        if not context:
            return {"success": False, "error": "Could not retrieve member data."}
        report = _narrative_staff(context)
        return {"success": True, "report": report, "context": context}
    except Exception as e:
        logger.error(f"Staff report generation failed for member {member_id}: {e}")
        return {"success": False, "error": "Report generation failed."}


def generate_lab_report(memberid: int, audience: str = "management") -> dict:
    try:
        context = _build_lab_context(memberid)
        if not context:
            return {"success": False, "error": "Could not retrieve lab user data."}
        report = _narrative_lab(context)
        return {"success": True, "report": report, "context": context}
    except Exception as e:
        logger.error(f"Lab report generation failed for user {memberid}: {e}")
        return {"success": False, "error": "Report generation failed."}



def generate_llm_report(profile_type: str, profile_id: int, audience: str = "management") -> dict:
    """
    LLM + RAG report generation — called asynchronously from the frontend
    via /api/ai/report so it never blocks page load.
    """
    try:
        if profile_type == "lab":
            context = _build_lab_context(profile_id)
        else:
            context = _build_staff_context(profile_id)

        if not context:
            return {"success": False, "error": "Could not retrieve member data."}

        from rag.pipeline import rag_generate
        narrative = rag_generate(context, audience=audience)

        if not narrative:
            return {"success": False, "error": "LLM unavailable — model not loaded."}

        return {"success": True, "report": narrative}
    except Exception as e:
        logger.error(f"LLM report generation failed for {profile_type} {profile_id}: {e}")
        return {"success": False, "error": "LLM report generation failed."}


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_staff_context(member_id: int) -> dict | None:
    ctx = {}

    # ── Basic identity ─────────────────────────────────────────────────────
    rows = hr_query("""
        SELECT p.designation, p.email, p.joining_date, p.team,
               p.type_of_appointment, p.qualification,
               COALESCE(rm.role_name, 'Staff') AS role_name,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS full_name
        FROM   profile p
        LEFT JOIN role r          ON r.memberid  = p.member_id
        LEFT JOIN role_master rm  ON rm.role_id  = r.role
        LEFT JOIN slotbooking.login l ON LOWER(TRIM(l.email)) = LOWER(TRIM(p.email))
        WHERE  p.member_id = %s
          AND (p.taken_clearance IS NULL OR p.taken_clearance = 0)
        LIMIT 1
    """, (member_id,))

    if not rows:
        return None

    row   = rows[0]
    email = row.get("email", "")

    full_name = (row.get("full_name") or "").strip()
    if not full_name:
        full_name = f"Member #{str(member_id).zfill(4)}"

    ctx["name"]             = full_name
    ctx["designation"]      = row.get("designation", "N/A")
    ctx["role"]             = row.get("role_name", "N/A")
    ctx["team"]             = row.get("team", "N/A")
    ctx["joining_date"]     = str(row.get("joining_date", "N/A"))
    ctx["appointment_type"] = row.get("type_of_appointment", "N/A")
    ctx["qualification"]    = row.get("qualification", "N/A")

    # ── Attendance ─────────────────────────────────────────────────────────
    try:
        year = date.today().year
        att  = hr_query("""
            SELECT COUNT(*) AS days_present
            FROM   user_attendance
            WHERE  memberid = %s AND YEAR(date) = %s
        """, (member_id, year))
        lv   = hr_query("""
            SELECT COALESCE(SUM(DATEDIFF(to_date, from_date) + 1), 0) AS leaves_taken
            FROM   leaves
            WHERE  memberid = %s AND status = 1 AND YEAR(from_date) = %s
        """, (member_id, year))

        days_present = att[0]["days_present"] if att else 0
        leaves_taken = lv[0]["leaves_taken"]  if lv  else 0
        working_days = calc_mandatory_days(year)  # correctly excludes weekends + holidays
        att_pct      = round((days_present / working_days) * 100, 1)

        ctx["attendance_pct"] = att_pct
        ctx["days_present"]   = days_present
        ctx["working_days"]   = working_days
        ctx["leaves_taken"]   = int(leaves_taken)
    except Exception as e:
        logger.warning(f"Attendance query failed: {e}")
        ctx["attendance_pct"] = "N/A"


    # ── Monthly reports ────────────────────────────────────────────────────
    try:
        mr = hr_query("""
            SELECT COUNT(*) AS submitted, AVG(star) AS avg_stars
            FROM   monthly_report WHERE member_id = %s
        """, (member_id,))
        ctx["monthly_reports_submitted"] = mr[0]["submitted"]                        if mr else 0
        ctx["monthly_report_avg_stars"]  = round(float(mr[0]["avg_stars"]), 1)       if mr and mr[0]["avg_stars"] else "N/A"
    except Exception as e:
        logger.warning(f"Monthly report query failed: {e}")

    # ── Equipment / slotbooking ────────────────────────────────────────────
    try:
        # Try email match first, then name fallback
        slot = slots_query(
            "SELECT memberid FROM login WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 1",
            (email,)
        )
        if not slot and ctx.get("name") and len(ctx["name"].split()) >= 2:
            parts  = ctx["name"].split()
            fname, lname = parts[0], parts[-1]
            slot = slots_query("""
                SELECT memberid FROM login
                WHERE LOWER(TRIM(fname)) = LOWER(%s)
                  AND LOWER(TRIM(lname)) = LOWER(%s)
                  AND position IN ('IITBNF Staff', 'Faculty', 'Institute Facility',
                                   'NCPRE Academic', 'Project Staff')
                LIMIT 1
            """, (fname, lname))
        if slot:
            slot_id = slot[0]["memberid"]
            eq  = slots_query("""
                SELECT COUNT(DISTINCT machid) AS tools_used, COUNT(*) AS total_bookings
                FROM   reservations WHERE memberid = %s
            """, (slot_id,))
            eqa = slots_query(
                "SELECT COUNT(*) AS eq_requests FROM equipment_usage_approval WHERE requestedby = %s",
                (slot_id,)
            )
            tr  = slots_query("SELECT COUNT(*) AS trainings FROM training_report  WHERE memberid = %s", (slot_id,))
            pp  = slots_query("SELECT COUNT(*) AS papers    FROM paper_publish    WHERE memberid = %s AND approve = 1", (slot_id,))
            fp  = slots_query("SELECT COUNT(*) AS projects  FROM faculty_projects WHERE memberid = %s", (slot_id,))

            ctx["tools_used"]     = eq[0]["tools_used"]      if eq  else 0
            ctx["total_bookings"] = eq[0]["total_bookings"]   if eq  else 0
            ctx["eq_requests"]    = eqa[0]["eq_requests"]     if eqa else 0
            ctx["trainings"]      = tr[0]["trainings"]        if tr  else 0
            ctx["papers"]         = pp[0]["papers"]           if pp  else 0
            ctx["projects"]       = fp[0]["projects"]         if fp  else 0
    except Exception as e:
        logger.warning(f"Slotbooking context query failed: {e}")

    return ctx


def _build_lab_context(memberid: int) -> dict | None:
    ctx = {}

    rows = slots_query("""
        SELECT l.fname, l.lname, l.email, l.position AS category
        FROM   login l WHERE l.memberid = %s LIMIT 1
    """, (memberid,))

    if not rows:
        return None

    row          = rows[0]
    ctx["name"]     = (row.get("fname","") + " " + row.get("lname","")).strip() or f"User #{memberid}"
    ctx["category"] = row.get("category", "N/A")

    try:
        eq  = slots_query("""
            SELECT COUNT(DISTINCT machid) AS tools_used, COUNT(*) AS total_bookings
            FROM   reservations WHERE memberid = %s
        """, (memberid,))
        eqa = slots_query(
            "SELECT COUNT(*) AS eq_requests FROM equipment_usage_approval WHERE requestedby = %s",
            (memberid,)
        )
        tr  = slots_query("SELECT COUNT(*) AS trainings FROM training_report  WHERE memberid = %s", (memberid,))
        pp  = slots_query("SELECT COUNT(*) AS papers    FROM paper_publish    WHERE memberid = %s AND approve = 1", (memberid,))
        fp  = slots_query("SELECT COUNT(*) AS projects  FROM faculty_projects WHERE memberid = %s", (memberid,))
        ap  = slots_query("SELECT COUNT(*) AS approved  FROM equipment_usage_approval WHERE requestedby = %s AND status = 3", (memberid,))

        ctx["tools_used"]        = eq[0]["tools_used"]      if eq  else 0
        ctx["total_bookings"]    = eq[0]["total_bookings"]   if eq  else 0
        ctx["eq_requests"]       = eqa[0]["eq_requests"]     if eqa else 0
        ctx["trainings"]         = tr[0]["trainings"]        if tr  else 0
        ctx["papers"]            = pp[0]["papers"]           if pp  else 0
        ctx["projects"]          = fp[0]["projects"]         if fp  else 0
        ctx["approved_requests"] = ap[0]["approved"]         if ap  else 0
    except Exception as e:
        logger.warning(f"Lab usage context query failed: {e}")

    return ctx


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _val(ctx: dict, key: str, fallback: str = "N/A") -> str:
    v = ctx.get(key, fallback)
    return str(v) if v is not None else fallback


# ══════════════════════════════════════════════════════════════════════════════
# NARRATIVE GENERATORS
# ══════════════════════════════════════════════════════════════════════════════

def _narrative_staff(ctx: dict) -> dict:
    name        = _val(ctx, "name")
    designation = _val(ctx, "designation")
    role        = _val(ctx, "role")
    team        = _val(ctx, "team")
    joined      = _val(ctx, "joining_date")
    appt        = _val(ctx, "appointment_type")
    qual        = _val(ctx, "qualification")

    # ── Identity ───────────────────────────────────────────────────────────
    identity_parts = [f"{name} serves as {designation} within the {team} team at IITBNF"]
    if role and role != "N/A":
        identity_parts[0] += f", holding the system role of {role}"
    identity_parts[0] += "."
    if joined and joined != "N/A":
        identity_parts.append(f"They joined the facility on {joined}.")
    if appt and appt != "N/A":
        identity_parts.append(f"The current appointment is on a {appt} basis.")
    if qual and qual != "N/A":
        identity_parts.append(f"Their recorded qualification is {qual}.")

    # ── Attendance ─────────────────────────────────────────────────────────
    att_pct      = ctx.get("attendance_pct", "N/A")
    days_present = ctx.get("days_present")
    working_days = ctx.get("working_days")
    leaves_taken = ctx.get("leaves_taken", 0)

    if att_pct != "N/A" and days_present is not None:
        att_line = (
            f"{name} has been present for {days_present} out of {working_days} "
            f"working days this year, reflecting an attendance rate of {att_pct}%."
        )
        if att_pct >= 90:
            att_line += " This represents a strong attendance record."
        elif att_pct >= 75:
            att_line += " Attendance is within acceptable range."
        else:
            att_line += " Attendance is below the recommended threshold."
        leave_line = (
            f"A total of {leaves_taken} leave "
            f"{'day has' if leaves_taken == 1 else 'days have'} been recorded for the current year."
        )
        attendance_text = f"{att_line} {leave_line}"
    else:
        attendance_text = "Attendance data is not available for this period."


    # ── Activity ───────────────────────────────────────────────────────────
    reports      = ctx.get("monthly_reports_submitted", 0)
    avg_stars    = ctx.get("monthly_report_avg_stars", "N/A")
    bookings     = ctx.get("total_bookings", 0)
    tools_used   = ctx.get("tools_used", 0)
    trainings    = ctx.get("trainings", 0)
    eq_requests  = ctx.get("eq_requests", 0)

    activity_parts = []
    if reports:
        r_line = f"{name} has submitted {reports} monthly {'report' if reports == 1 else 'reports'}"
        if avg_stars != "N/A":
            r_line += f", with an average rating of {avg_stars} stars"
        activity_parts.append(r_line + ".")
    if eq_requests:
        activity_parts.append(
            f"A total of {eq_requests} equipment usage "
            f"{'request has' if eq_requests == 1 else 'requests have'} been submitted."
        )
    if bookings:
        activity_parts.append(
            f"Lab reservation records show {bookings} "
            f"{'reservation' if bookings == 1 else 'reservations'} "
            f"across {tools_used} {'tool' if tools_used == 1 else 'tools'}."
        )
    if trainings:
        activity_parts.append(
            f"{trainings} training {'session has' if trainings == 1 else 'sessions have'} been completed."
        )
    if not activity_parts:
        activity_parts.append("No activity or reporting records are available.")
    activity_text = " ".join(activity_parts)

    # ── Research ───────────────────────────────────────────────────────────
    papers   = ctx.get("papers", 0)
    projects = ctx.get("projects", 0)

    if papers or projects:
        research_parts = []
        if papers:
            research_parts.append(
                f"{name} has {papers} approved research "
                f"{'publication' if papers == 1 else 'publications'} on record."
            )
        if projects:
            research_parts.append(
                f"They are associated with {projects} faculty "
                f"{'project' if projects == 1 else 'projects'}."
            )
        research_text = " ".join(research_parts)
    else:
        research_text = "No research publications or project associations are currently on record."

    return {
        "identity":    " ".join(identity_parts),
        "attendance":  attendance_text,
        "activity":    activity_text,
        "research":    research_text,
    }


def _narrative_lab(ctx: dict) -> dict:
    name     = _val(ctx, "name")
    category = _val(ctx, "category")

    # ── Identity ───────────────────────────────────────────────────────────
    if category and category != "N/A":
        identity_text = (
            f"{name} is registered at IITBNF as a {category} user. "
            f"They have been granted access to the facility's equipment and booking systems."
        )
    else:
        identity_text = f"{name} is a registered user at the IIT Bombay Nanofabrication Facility."

    # ── Usage ──────────────────────────────────────────────────────────────
    bookings     = ctx.get("total_bookings", 0)
    tools_used   = ctx.get("tools_used", 0)
    eq_requests  = ctx.get("eq_requests", 0)
    approved_req = ctx.get("approved_requests", 0)

    usage_parts = []
    if bookings:
        usage_parts.append(
            f"{name} has made {bookings} slot "
            f"{'reservation' if bookings == 1 else 'reservations'} "
            f"across {tools_used} {'piece' if tools_used == 1 else 'pieces'} of equipment."
        )
    if eq_requests:
        usage_parts.append(
            f"A total of {eq_requests} equipment usage "
            f"{'request has' if eq_requests == 1 else 'requests have'} been submitted."
        )
    if approved_req:
        usage_parts.append(
            f"{approved_req} of these "
            f"{'request has' if approved_req == 1 else 'requests have'} been approved."
        )
    if not usage_parts:
        usage_parts.append(f"No equipment reservations or usage records are available for {name}.")
    usage_text = " ".join(usage_parts)

    # ── Research ───────────────────────────────────────────────────────────
    papers    = ctx.get("papers", 0)
    projects  = ctx.get("projects", 0)
    trainings = ctx.get("trainings", 0)

    research_parts = []
    if papers:
        research_parts.append(
            f"{name} has {papers} approved research "
            f"{'publication' if papers == 1 else 'publications'} associated with IITBNF."
        )
    if projects:
        research_parts.append(
            f"They are linked to {projects} faculty "
            f"{'project' if projects == 1 else 'projects'}."
        )
    if trainings:
        research_parts.append(
            f"{trainings} equipment training "
            f"{'session has' if trainings == 1 else 'sessions have'} been completed."
        )
    if not research_parts:
        research_parts.append("No research output or training records are currently on file.")
    research_text = " ".join(research_parts)

    return {
        "identity": identity_text,
        "usage":    usage_text,
        "research": research_text,
    }
