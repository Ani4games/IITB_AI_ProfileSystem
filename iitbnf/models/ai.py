"""
models/ai.py
------------
Context-Augmented Generation (CAG) layer.

Pipeline
────────
  1. _build_staff_context / _build_lab_context
       Pull ALL relevant DB data for this person into a flat dict.
       This is the CAG "cache" — the LLM always receives the full
       personal context with zero retrieval overhead for summaries.

  2. _narrative_staff / _narrative_lab
       Template-based prose (no LLM needed, instant fallback).

  3. generate_staff_report / generate_lab_report
       Public entry points for template-based reports.

  4. generate_llm_report
       Passes the pre-built context to pipeline.rag_generate.
       RAG (TF-IDF) is only consulted when the question explicitly
       asks for comparative / policy data (gated in pipeline.py).
"""

import logging
from datetime import date

from flask import ctx
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
        logger.error("Staff report generation failed for member %s: %s", member_id, e)
        return {"success": False, "error": "Report generation failed."}


def generate_lab_report(memberid: int, audience: str = "management") -> dict:
    try:
        context = _build_lab_context(memberid)
        if not context:
            return {"success": False, "error": "Could not retrieve lab user data."}
        report = _narrative_lab(context)
        return {"success": True, "report": report, "context": context}
    except Exception as e:
        logger.error("Lab report generation failed for user %s: %s", memberid, e)
        return {"success": False, "error": "Report generation failed."}


def generate_llm_report(
    profile_type: str,
    profile_id: int,
    audience: str = "management",
) -> dict:
    """
    LLM + CAG report generation.
    Context is built here and injected directly into the LLM prompt.
    Called asynchronously from /api/ai/report — never blocks page load.
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
        logger.error(
            "LLM report generation failed for %s %s: %s", profile_type, profile_id, e
        )
        return {"success": False, "error": "LLM report generation failed."}


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_staff_context(member_id: int) -> dict | None:
    """
    Build a complete flat context dict for a staff member.

    Covers: identity, attendance + leave breakdown, monthly reports,
            slot/equipment activity (with status breakdown), tool permissions,
            system ownership (current + historical), training, publications,
            projects.

    All sections are best-effort — a DB failure in one section does NOT
    abort the rest.
    """
    ctx: dict = {}

    # ── Identity ──────────────────────────────────────────────────────────────
    rows = hr_query("""
        SELECT p.designation, p.email, p.joining_date, p.team,
               p.type_of_appointment, p.qualification, p.p_project_code,
               COALESCE(rm.role_name, 'Staff') AS role_name,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS full_name
        FROM   profile p
        LEFT JOIN role r          ON r.memberid  = p.member_id
        LEFT JOIN role_master rm  ON rm.role_id  = r.role
        LEFT JOIN slotbooking.login l ON l.memberid = p.member_id
        WHERE  p.member_id = %s
          AND (p.taken_clearance IS NULL OR p.taken_clearance = 0)
        LIMIT 1
    """, (member_id,))

    if not rows:
        return None

    row   = rows[0]
    email = row.get("email", "")

    full_name = (row.get("full_name") or "").strip()

    # Fallback: email-username wildcard for the rare case where the
    # slotbooking account has a different memberid but the same email prefix.
    if not full_name and email and "@" in email:
        try:
            prefix = email.split("@")[0]
            r2 = slots_query(
                "SELECT fname, lname FROM login "
                "WHERE LOWER(TRIM(email)) LIKE LOWER(%s) LIMIT 1",
                (f"{prefix}@%",),
            )
            if r2:
                full_name = ((r2[0].get("fname") or "") + " " + (r2[0].get("lname") or "")).strip()
        except Exception:
            pass

    if not full_name:
        full_name = f"Member #{str(member_id).zfill(4)}"

    ctx["name"]             = full_name
    ctx["designation"]      = row.get("designation")         or "N/A"
    ctx["role"]             = row.get("role_name")           or "N/A"
    ctx["team"]             = row.get("team")                or "N/A"
    ctx["joining_date"]     = str(row.get("joining_date")    or "N/A")
    ctx["appointment_type"] = row.get("type_of_appointment") or "N/A"
    ctx["qualification"]    = row.get("qualification")       or "N/A"
    ctx["project_code"]     = row.get("p_project_code")      or "N/A"

    # ── Attendance ────────────────────────────────────────────────────────────
    try:
        year         = date.today().year
        att          = hr_query(
            "SELECT COUNT(*) AS days_present FROM user_attendance "
            "WHERE memberid=%s AND YEAR(date)=%s",
            (member_id, year),
        )
        lv           = hr_query(
            "SELECT COALESCE(SUM(DATEDIFF(to_date,from_date)+1),0) AS leaves_taken "
            "FROM leaves WHERE memberid=%s AND status=1 AND YEAR(from_date)=%s",
            (member_id, year),
        )
        days_present = int(att[0]["days_present"] if att else 0)
        leaves_taken = int(lv[0]["leaves_taken"]  if lv  else 0)
        working_days = calc_mandatory_days(year)
        att_pct      = round(days_present / working_days * 100, 1) if working_days else 0

        ctx["attendance_pct"] = att_pct
        ctx["days_present"]   = days_present
        ctx["working_days"]   = working_days
        ctx["leaves_taken"]   = leaves_taken

        # Per-type leave breakdown for richer LLM context
        lv_detail = hr_query(
            "SELECT type_of_leave, "
            "SUM(DATEDIFF(to_date,from_date)+1) AS days_taken "
            "FROM leaves "
            "WHERE memberid=%s AND status=1 AND YEAR(from_date)=%s "
            "GROUP BY type_of_leave",
            (member_id, year),
        )
        if lv_detail:
            ctx["leave_breakdown"] = ", ".join(
                f"{r['type_of_leave']}: {int(r['days_taken'])} day(s)"
                for r in lv_detail
            )
    except Exception as e:
        logger.warning("Attendance context query failed for %s: %s", member_id, e)
        ctx["attendance_pct"] = "N/A"

    # ── Monthly reports ───────────────────────────────────────────────────────
    try:
        mr = hr_query(
            "SELECT COUNT(*) AS submitted, AVG(star) AS avg_stars, "
            "MAX(report_year) AS latest_year "
            "FROM monthly_report WHERE member_id=%s",
            (member_id,),
        )
        ctx["monthly_reports_submitted"]  = int(mr[0]["submitted"])                   if mr else 0
        ctx["monthly_report_avg_stars"]   = round(float(mr[0]["avg_stars"]), 1)        if mr and mr[0]["avg_stars"] else "N/A"
        ctx["monthly_report_latest_year"] = mr[0]["latest_year"]                       if mr else "N/A"
    except Exception as e:
        logger.warning("Monthly report context failed for %s: %s", member_id, e)

    # ── Slotbooking resolution ────────────────────────────────────────────────
    slot_uid = _resolve_slot_uid(member_id, email)
    ctx["slot_uid"] = slot_uid
    ctx["member_id"] = member_id   # HR memberid for attendance/leave queries

    if slot_uid:
        # ── Reservations ──────────────────────────────────────────────────
        try:
            eq = slots_query(
                "SELECT COUNT(DISTINCT machid) AS tools_used, "
                "COUNT(*) AS total_bookings "
                "FROM reservations WHERE memberid=%s",
                (slot_uid,),
            )
            ctx["tools_used"]     = int(eq[0]["tools_used"])     if eq else 0
            ctx["total_bookings"] = int(eq[0]["total_bookings"])  if eq else 0
        except Exception as e:
            logger.warning("Reservations context failed for uid %s: %s", slot_uid, e)

        # ── Equipment usage requests with full status breakdown ────────────
        try:
            eqa = slots_query("""
                SELECT
                    COUNT(*)                                          AS eq_requests,
                    SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)        AS slot_booked,
                    SUM(CASE WHEN status=1 THEN 1 ELSE 0 END)        AS approved,
                    SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)        AS pending,
                    SUM(CASE WHEN status=2 THEN 1 ELSE 0 END)        AS rejected
                FROM equipment_usage_approval
                WHERE requestedby=%s
            """, (slot_uid,))
            ctx["eq_requests"]    = int(eqa[0]["eq_requests"]  or 0) if eqa else 0
            ctx["eq_slot_booked"] = int(eqa[0]["slot_booked"]  or 0) if eqa else 0
            ctx["eq_approved"]    = int(eqa[0]["approved"]     or 0) if eqa else 0
            ctx["eq_pending"]     = int(eqa[0]["pending"]      or 0) if eqa else 0
            ctx["eq_rejected"]    = int(eqa[0]["rejected"]     or 0) if eqa else 0
        except Exception as e:
            logger.warning("Equipment request context failed for uid %s: %s", slot_uid, e)

        # ── Tool permissions count ────────────────────────────────────────
        try:
            perms = slots_query(
                "SELECT COUNT(*) AS perm_count FROM permissions WHERE memberid=%s",
                (slot_uid,),
            )
            ctx["tool_permissions_count"] = int(perms[0]["perm_count"] or 0) if perms else 0
        except Exception as e:
            logger.warning("Permissions context failed for uid %s: %s", slot_uid, e)

        # ── System ownership — current (system_owner table) ───────────────
        # machid column stores comma-separated IDs so we count splits, not rows.
        try:
            so_rows = slots_query(
                "SELECT machid FROM system_owner WHERE memberid=%s",
                (slot_uid,),
            )
            owned_count = 0
            if so_rows:
                for r in so_rows:
                    raw = str(r.get("machid") or "")
                    owned_count += len(
                        [x for x in raw.split(",") if x.strip().isdigit()]
                    )
            ctx["systems_owned_current"] = owned_count
        except Exception as e:
            logger.warning("System ownership context failed for uid %s: %s", slot_uid, e)

        # ── System ownership — historical (system_owner_track) ────────────
        try:
            sot = slots_query("""
                SELECT
                    SUM(CASE WHEN action='create' THEN 1 ELSE 0 END) AS ever_owned,
                    SUM(CASE WHEN action='delete' THEN 1 ELSE 0 END) AS removed
                FROM system_owner_track
                WHERE memberid=%s
            """, (slot_uid,))
            if sot and sot[0]["ever_owned"] is not None:
                ctx["systems_owned_ever"]        = int(sot[0]["ever_owned"] or 0)
                ctx["systems_ownership_removed"] = int(sot[0]["removed"]    or 0)
        except Exception as e:
            logger.warning("System owner track context failed for uid %s: %s", slot_uid, e)

        # ── Training sessions ─────────────────────────────────────────────
        try:
            tr = slots_query(
                "SELECT COUNT(*) AS trainings FROM training_report WHERE memberid=%s",
                (slot_uid,),
            )
            ctx["trainings"] = int(tr[0]["trainings"] or 0) if tr else 0
        except Exception as e:
            logger.warning("Training context failed for uid %s: %s", slot_uid, e)

        # After the training section
        try:
            from models.staff import get_staff_logbook_stats
            lb = get_staff_logbook_stats(slot_uid)
            if lb:
                ctx["logbook_total_entries"] = lb.get("total_entries", 0)
                ctx["logbook_tools_count"] = lb.get("tools_with_logs", 0)
        except Exception as e:
            logger.warning("Logbook context failed for uid %s: %s", slot_uid, e)
        # ── Publications ──────────────────────────────────────────────────
        try:
            pp = slots_query(
                "SELECT COUNT(*) AS papers FROM paper_publish "
                "WHERE memberid=%s AND approve=1",
                (slot_uid,),
            )
            ctx["papers"] = int(pp[0]["papers"] or 0) if pp else 0
        except Exception as e:
            logger.warning("Publications context failed for uid %s: %s", slot_uid, e)

        # ── Projects ──────────────────────────────────────────────────────
        try:
            fp = slots_query("""
                SELECT
                    COUNT(*)                                     AS projects,
                    SUM(CASE WHEN active=1 THEN 1 ELSE 0 END)   AS active_projects
                FROM faculty_projects
                WHERE memberid=%s
            """, (slot_uid,))
            ctx["projects"]        = int(fp[0]["projects"]        or 0) if fp else 0
            ctx["active_projects"] = int(fp[0]["active_projects"] or 0) if fp else 0
        except Exception as e:
            logger.warning("Projects context failed for uid %s: %s", slot_uid, e)

    return ctx


def _build_lab_context(memberid: int) -> dict | None:
    """
    Build a complete flat context dict for a lab user.

    Covers: identity, registration, reservations, equipment requests
            (with status breakdown), tool permissions, system ownership
            (current + historical via system_owner_track), session reports,
            cancellations, trainings, publications, projects.
    """
    ctx: dict = {}
    ctx["slot_uid"] = memberid  # Default to memberid for any slotbooking queries if resolution fails   
    ctx["member_id"] = memberid    # kept consistent for router and potential future use
    rows = slots_query("""
        SELECT
            l.fname, l.lname, l.email,
            l.position       AS category,
            l.department,
            l.rollno,
            l.research_area,
            l.expiry_date,
            l.mobile,
            TRIM(CONCAT(COALESCE(s.fname,''), ' ', COALESCE(s.lname,'')))
                             AS supervisor_name
        FROM login l
        LEFT JOIN login s ON s.memberid = l.supervisor
        WHERE l.memberid = %s
        LIMIT 1
    """, (memberid,))

    if not rows:
        return None

    row = rows[0]
    ctx["name"]            = (
        ((row.get("fname") or "") + " " + (row.get("lname") or "")).strip()
        or f"User #{memberid}"
    )
    ctx["category"]        = row.get("category")      or "N/A"
    ctx["department"]      = row.get("department")    or "N/A"
    ctx["rollno"]          = row.get("rollno")        or "N/A"
    ctx["research_area"]   = row.get("research_area") or "N/A"
    ctx["supervisor_name"] = (row.get("supervisor_name") or "").strip() or "N/A"
    ctx["expiry_date"]     = row.get("expiry_date")   or "N/A"

    # ── Registration ──────────────────────────────────────────────────────────
    try:
        reg = slots_query(
            "SELECT course, status, project_first "
            "FROM registration WHERE memberid=%s LIMIT 1",
            (memberid,),
        )
        if reg:
            status_map = {2: "Active", 1: "Under Review", 0: "Pending"}
            ctx["reg_course"]  = reg[0].get("course")        or "N/A"
            ctx["reg_status"]  = status_map.get(reg[0].get("status"), "Pending")
            ctx["reg_project"] = reg[0].get("project_first") or "N/A"
    except Exception as e:
        logger.warning("Registration context failed for %s: %s", memberid, e)

    # ── Slot reservations ─────────────────────────────────────────────────────
    try:
        eq = slots_query(
            "SELECT COUNT(DISTINCT machid) AS tools_used, "
            "COUNT(*) AS total_bookings "
            "FROM reservations WHERE memberid=%s",
            (memberid,),
        )
        ctx["tools_used"]     = int(eq[0]["tools_used"]     or 0) if eq else 0
        ctx["total_bookings"] = int(eq[0]["total_bookings"]  or 0) if eq else 0
    except Exception as e:
        logger.warning("Reservations context failed for %s: %s", memberid, e)

    # ── Equipment usage requests with full status breakdown ───────────────────
    try:
        eqa = slots_query("""
            SELECT
                COUNT(*)                                          AS eq_requests,
                SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)        AS approved_requests,
                SUM(CASE WHEN status=1 THEN 1 ELSE 0 END)        AS reviewed,
                SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)        AS pending,
                SUM(CASE WHEN status=2 THEN 1 ELSE 0 END)        AS rejected
            FROM equipment_usage_approval
            WHERE requestedby=%s
        """, (memberid,))
        ctx["eq_requests"]       = int(eqa[0]["eq_requests"]       or 0) if eqa else 0
        ctx["approved_requests"] = int(eqa[0]["approved_requests"] or 0) if eqa else 0
        ctx["eq_pending"]        = int(eqa[0]["pending"]           or 0) if eqa else 0
        ctx["eq_rejected"]       = int(eqa[0]["rejected"]          or 0) if eqa else 0
    except Exception as e:
        logger.warning("Equipment requests context failed for %s: %s", memberid, e)

    # ── Tool permissions ──────────────────────────────────────────────────────
    try:
        perms = slots_query(
            "SELECT COUNT(*) AS perm_count FROM permissions WHERE memberid=%s",
            (memberid,),
        )
        ctx["tool_permissions_count"] = int(perms[0]["perm_count"] or 0) if perms else 0
    except Exception as e:
        logger.warning("Permissions context failed for %s: %s", memberid, e)

    # ── System ownership — current (system_owner table) ───────────────────────
    try:
        so_rows = slots_query(
            "SELECT machid FROM system_owner WHERE memberid=%s",
            (memberid,),
        )
        owned_count = 0
        if so_rows:
            for r in so_rows:
                raw = str(r.get("machid") or "")
                owned_count += len(
                    [x for x in raw.split(",") if x.strip().isdigit()]
                )
        ctx["systems_owned_current"] = owned_count
    except Exception as e:
        logger.warning("System ownership context failed for %s: %s", memberid, e)

    # ── System ownership — historical (system_owner_track) ────────────────────
    try:
        sot = slots_query("""
            SELECT
                SUM(CASE WHEN action='create' THEN 1 ELSE 0 END) AS ever_owned,
                SUM(CASE WHEN action='delete' THEN 1 ELSE 0 END) AS removed
            FROM system_owner_track
            WHERE memberid=%s
        """, (memberid,))
        if sot and sot[0]["ever_owned"] is not None:
            ctx["systems_owned_ever"]        = int(sot[0]["ever_owned"] or 0)
            ctx["systems_ownership_removed"] = int(sot[0]["removed"]    or 0)
    except Exception as e:
        logger.warning("System owner track context failed for %s: %s", memberid, e)

    # ── Session reports ───────────────────────────────────────────────────────
    try:
        sr = slots_query(
            "SELECT COUNT(*) AS session_reports FROM reporting WHERE memberid=%s",
            (memberid,),
        )
        ctx["session_reports"] = int(sr[0]["session_reports"] or 0) if sr else 0
    except Exception as e:
        logger.warning("Session reports context failed for %s: %s", memberid, e)

    # ── Cancellations ─────────────────────────────────────────────────────────
    try:
        cc = slots_query(
            "SELECT COUNT(*) AS cancellations "
            "FROM cancel_reservation WHERE memberid=%s",
            (memberid,),
        )
        ctx["cancellations"] = int(cc[0]["cancellations"] or 0) if cc else 0
    except Exception as e:
        logger.warning("Cancellations context failed for %s: %s", memberid, e)

    # ── Training ──────────────────────────────────────────────────────────────
    try:
        tr = slots_query(
            "SELECT COUNT(*) AS trainings FROM training_report WHERE memberid=%s",
            (memberid,),
        )
        ctx["trainings"] = int(tr[0]["trainings"] or 0) if tr else 0
    except Exception as e:
        logger.warning("Training context failed for %s: %s", memberid, e)

    # ── Publications ──────────────────────────────────────────────────────────
    try:
        pp = slots_query(
            "SELECT COUNT(*) AS papers FROM paper_publish "
            "WHERE memberid=%s AND approve=1",
            (memberid,),
        )
        ctx["papers"] = int(pp[0]["papers"] or 0) if pp else 0
    except Exception as e:
        logger.warning("Publications context failed for %s: %s", memberid, e)

    # ── Projects ──────────────────────────────────────────────────────────────
    try:
        fp = slots_query("""
            SELECT
                COUNT(*)                                   AS projects,
                SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active_projects
            FROM faculty_projects
            WHERE memberid=%s
        """, (memberid,))
        ctx["projects"]        = int(fp[0]["projects"]        or 0) if fp else 0
        ctx["active_projects"] = int(fp[0]["active_projects"] or 0) if fp else 0
    except Exception as e:
        logger.warning("Projects context failed for %s: %s", memberid, e)

    return ctx


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_slot_uid(member_id: int, email: str) -> int | None:
    """
    Resolve HR member_id → slotbooking.login memberid.
    Three strategies in priority order:
      1. Exact email match (case-insensitive)
      2. Email-username wildcard  (handles gmail vs iitb.ac.in variants)
      3. Same numeric memberid in slotbooking (last resort)
    """
    if email:
        r = slots_query(
            "SELECT memberid FROM login "
            "WHERE LOWER(TRIM(email))=LOWER(TRIM(%s)) LIMIT 1",
            (email,),
        )
        if r:
            return r[0]["memberid"]

        prefix = email.split("@")[0] if "@" in email else ""
        if prefix:
            r = slots_query(
                "SELECT memberid FROM login "
                "WHERE LOWER(TRIM(email)) LIKE LOWER(%s) LIMIT 1",
                (f"{prefix}@%",),
            )
            if r:
                return r[0]["memberid"]

    r = slots_query(
        "SELECT memberid FROM login WHERE memberid=%s LIMIT 1",
        (member_id,),
    )
    return r[0]["memberid"] if r else None


def _val(ctx: dict, key: str, fallback: str = "N/A") -> str:
    v = ctx.get(key, fallback)
    return str(v) if v is not None else fallback


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE-BASED NARRATIVE GENERATORS  (zero LLM — instant fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _narrative_staff(ctx: dict) -> dict:
    name        = _val(ctx, "name")
    designation = _val(ctx, "designation")
    role        = _val(ctx, "role")
    team        = _val(ctx, "team")
    joined      = _val(ctx, "joining_date")
    appt        = _val(ctx, "appointment_type")
    qual        = _val(ctx, "qualification")
    proj_code   = _val(ctx, "project_code")

    # ── Identity ──────────────────────────────────────────────────────────────
    id_parts = [f"{name} serves as {designation} within the {team} team at IITBNF"]
    if role not in ("N/A", "Staff"):
        id_parts[0] += f", holding the system role of {role}"
    id_parts[0] += "."
    if joined != "N/A":
        id_parts.append(f"They joined the facility on {joined}.")
    if appt != "N/A":
        id_parts.append(f"The current appointment is on a {appt} basis.")
    if qual != "N/A":
        id_parts.append(f"Recorded qualification: {qual}.")
    if proj_code != "N/A":
        id_parts.append(f"Project code: {proj_code}.")

    # ── Attendance ────────────────────────────────────────────────────────────
    att_pct      = ctx.get("attendance_pct", "N/A")
    days_present = ctx.get("days_present")
    working_days = ctx.get("working_days")
    leaves_taken = ctx.get("leaves_taken", 0)
    leave_detail = ctx.get("leave_breakdown", "")

    if att_pct != "N/A" and days_present is not None:
        qualifier = (
            " This represents a strong attendance record."
            if att_pct >= 90
            else " Attendance is within acceptable range."
            if att_pct >= 75
            else " Attendance is below the recommended 75% threshold."
        )
        att_text = (
            f"{name} has been present for {days_present} out of {working_days} "
            f"working days this year, an attendance rate of {att_pct}%.{qualifier}"
        )
        leave_text = (
            f"A total of {leaves_taken} leave "
            f"{'day has' if leaves_taken == 1 else 'days have'} been recorded this year"
            + (f" ({leave_detail})" if leave_detail else "")
            + "."
        )
        attendance_text = f"{att_text} {leave_text}"
    else:
        attendance_text = "Attendance data is not available for this period."

    # ── Activity ──────────────────────────────────────────────────────────────
    reports     = ctx.get("monthly_reports_submitted", 0)
    avg_stars   = ctx.get("monthly_report_avg_stars", "N/A")
    bookings    = ctx.get("total_bookings", 0)
    tools_used  = ctx.get("tools_used", 0)
    eq_requests = ctx.get("eq_requests", 0)
    slot_booked = ctx.get("eq_slot_booked", 0)
    trainings   = ctx.get("trainings", 0)
    perms_count = ctx.get("tool_permissions_count", 0)
    sys_current = ctx.get("systems_owned_current", 0)
    sys_ever    = ctx.get("systems_owned_ever", 0)

    acts = []
    if reports:
        line = (
            f"{name} has submitted {reports} monthly "
            f"{'report' if reports == 1 else 'reports'}"
        )
        if avg_stars != "N/A":
            line += f" with an average rating of {avg_stars} stars"
        acts.append(line + ".")
    if eq_requests:
        line = (
            f"{eq_requests} equipment usage "
            f"{'request has' if eq_requests == 1 else 'requests have'} been submitted"
        )
        if slot_booked:
            line += f", of which {slot_booked} have been slot-booked"
        acts.append(line + ".")
    if bookings:
        acts.append(
            f"Lab reservation records show {bookings} "
            f"{'reservation' if bookings == 1 else 'reservations'} "
            f"across {tools_used} {'tool' if tools_used == 1 else 'tools'}."
        )
    if perms_count:
        acts.append(
            f"Equipment access permissions are held for {perms_count} "
            f"piece{'s' if perms_count != 1 else ''} of equipment."
        )
    if sys_current:
        acts.append(
            f"Currently assigned as system owner for {sys_current} "
            f"tool{'s' if sys_current != 1 else ''}."
        )
    elif sys_ever:
        acts.append(
            f"Has served as system owner for {sys_ever} "
            f"tool{'s' if sys_ever != 1 else ''} over their tenure."
        )
    if trainings:
        acts.append(
            f"{trainings} training "
            f"{'session has' if trainings == 1 else 'sessions have'} been completed."
        )
    if not acts:
        acts.append("No activity or reporting records are available.")

    # ── Research ──────────────────────────────────────────────────────────────
    papers          = ctx.get("papers", 0)
    projects        = ctx.get("projects", 0)
    active_projects = ctx.get("active_projects", 0)

    res = []
    if papers:
        res.append(
            f"{name} has {papers} approved research "
            f"{'publication' if papers == 1 else 'publications'} on record."
        )
    if projects:
        active_note = f" ({active_projects} currently active)" if active_projects else ""
        res.append(
            f"They are associated with {projects} faculty "
            f"{'project' if projects == 1 else 'projects'}{active_note}."
        )
    if not res:
        res.append("No research publications or project associations are on record.")

    return {
        "identity":   " ".join(id_parts),
        "attendance": attendance_text,
        "activity":   " ".join(acts),
        "research":   " ".join(res),
    }


def _narrative_lab(ctx: dict) -> dict:
    name            = _val(ctx, "name")
    category        = _val(ctx, "category")
    department      = _val(ctx, "department")
    research_area   = _val(ctx, "research_area")
    supervisor_name = _val(ctx, "supervisor_name")

    # ── Identity ──────────────────────────────────────────────────────────────
    if category != "N/A":
        id_text = (
            f"{name} is registered at IITBNF as a {category} user"
            + (
                f" in the {department} department"
                if department not in ("N/A", "")
                else ""
            )
            + ". They have been granted access to the facility's equipment and booking systems."
        )
    else:
        id_text = (
            f"{name} is a registered user at the "
            "IIT Bombay Nanofabrication Facility."
        )

    if supervisor_name != "N/A":
        id_text += f" Supervisor: {supervisor_name}."
    if research_area not in ("N/A", "", "NA"):
        id_text += f" Research focus: {research_area}."

    reg_course = ctx.get("reg_course", "")
    reg_status = ctx.get("reg_status", "")
    if reg_course and reg_course != "N/A":
        id_text += f" Registered course: {reg_course} (status: {reg_status})."

    # ── Usage ─────────────────────────────────────────────────────────────────
    bookings      = ctx.get("total_bookings", 0)
    tools_used    = ctx.get("tools_used", 0)
    eq_requests   = ctx.get("eq_requests", 0)
    approved_req  = ctx.get("approved_requests", 0)
    perms_count   = ctx.get("tool_permissions_count", 0)
    sys_current   = ctx.get("systems_owned_current", 0)
    cancellations = ctx.get("cancellations", 0)
    session_rpts  = ctx.get("session_reports", 0)

    use = []
    if bookings:
        use.append(
            f"{name} has made {bookings} slot "
            f"{'reservation' if bookings == 1 else 'reservations'} "
            f"across {tools_used} "
            f"{'piece' if tools_used == 1 else 'pieces'} of equipment."
        )
    if eq_requests:
        line = (
            f"{eq_requests} equipment usage "
            f"{'request has' if eq_requests == 1 else 'requests have'} been submitted"
        )
        if approved_req:
            line += f", of which {approved_req} have been approved"
        use.append(line + ".")
    if perms_count:
        use.append(
            f"Tool access permissions are held for {perms_count} "
            f"piece{'s' if perms_count != 1 else ''} of equipment."
        )
    if sys_current:
        use.append(
            f"Currently serving as system owner for {sys_current} "
            f"tool{'s' if sys_current != 1 else ''}."
        )
    if cancellations:
        use.append(
            f"{cancellations} reservation "
            f"{'cancellation has' if cancellations == 1 else 'cancellations have'} "
            "been recorded."
        )
    if session_rpts:
        use.append(
            f"{session_rpts} equipment session "
            f"{'report has' if session_rpts == 1 else 'reports have'} been filed."
        )
    if not use:
        use.append(
            f"No equipment reservations or usage records are available for {name}."
        )

    # ── Research ──────────────────────────────────────────────────────────────
    papers          = ctx.get("papers", 0)
    projects        = ctx.get("projects", 0)
    active_projects = ctx.get("active_projects", 0)
    trainings       = ctx.get("trainings", 0)

    res = []
    if papers:
        res.append(
            f"{name} has {papers} approved research "
            f"{'publication' if papers == 1 else 'publications'} "
            "associated with IITBNF."
        )
    if projects:
        active_note = f" ({active_projects} currently active)" if active_projects else ""
        res.append(
            f"They are linked to {projects} faculty "
            f"{'project' if projects == 1 else 'projects'}{active_note}."
        )
    if trainings:
        res.append(
            f"{trainings} equipment training "
            f"{'session has' if trainings == 1 else 'sessions have'} been completed."
        )
    if not res:
        res.append("No research output or training records are currently on file.")

    return {
        "identity": id_text,
        "usage":    " ".join(use),
        "research": " ".join(res),
    }