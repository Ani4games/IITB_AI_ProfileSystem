# generate_training_data.py — FIXED VERSION
"""
Run locally: python generate_training_data.py
Generates synthetic Q&A pairs from your live DB.
"""

import json
import sys
import random

from distro import name
from db import slots_query, hr_query

# Add this function before generate()
PARAPHRASE_VARIANTS = {
    "attendance_year": [
        "how many days was {name} present in {year}",
        "what was {name}'s attendance in {year}",
        "how often did {name} come in {year}",
        "was {name} regular in {year}",
        "attendance record of {name} for {year}",
        "how many working days did {name} attend in {year}",
        "did {name} come to work regularly in {year}",
        "check {name}'s attendance for {year}",
        "show me {name} attendance {year}",
        "how punctual was {name} in {year}",
        "{name} attendance {year}",
        "days present {name} {year}",
        "how much did {name} attend in {year}",
        "was {name} at work in {year}",
    ],
    "slot_year": [
        "how many equipment requests did {name} make in {year}",
        "what was {name}'s slot activity in {year}",
        "how active was {name} on equipment in {year}",
        "{name} equipment usage {year}",
        "how many times did {name} request equipment in {year}",
        "slot bookings of {name} in {year}",
        "equipment requests {name} {year}",
        "how many slots did {name} book in {year}",
        "check {name} equipment activity for {year}",
        "{name} lab usage {year}",
        "what equipment did {name} use in {year}",
        "how busy was {name} on equipment in {year}",
    ],
    "compare_slot": [
        "compare {name} slot activity {y1} and {y2}",
        "how much did {name} equipment usage change from {y1} to {y2}",
        "was {name} more active in {y1} or {y2}",
        "{name} equipment {y1} vs {y2}",
        "difference in {name} requests between {y1} and {y2}",
        "slot comparison {name} {y1} {y2}",
        "how many more requests did {name} make in {y2} compared to {y1}",
        "did {name} use more equipment in {y1} or {y2}",
        "compare {name} slot activity in {y1} and {y2}",
        "difference in {name} equipment requests between {y1} and {y2}",
    ],
    "compare_attendance": [
        "compare {name} attendance {y1} and {y2}",
        "was {name} more regular in {y1} or {y2}",
        "attendance comparison {name} {y1} {y2}",
        "how did {name} attendance change from {y1} to {y2}",
        "difference in {name} attendance {y1} vs {y2}",
        "did {name} attend more in {y1} or {y2}",
    ],
    "publications": [
        "how many publications does {name} have",
        "what papers has {name} published",
        "research output of {name}",
        "{name} publications",
        "how many papers does {name} have",
        "does {name} have any papers",
        "published works of {name}",
        "{name} research papers",
        "does {name} have any papers",
        "published works of {name}",
        "{name} research papers",
    ],
    "projects": [
        "how many projects is {name} associated with",
        "what projects does {name} have",
        "{name} projects",
        "is {name} working on any projects",
        "active projects of {name}",
        "how many active projects does {name} have",
        "what projects has {name} worked on",
        "list {name}'s project contributions",
        "which projects is {name} involved in",
        "what is {name}'s role in the projects",
    ],
    "system_owner": [
        "how many systems does {name} own",
        "which tools is {name} responsible for",
        "what equipment does {name} manage",
        "system ownership of {name}",
        "what tools does {name} own",
        "is {name} a system owner",
        "{name} system owner",
    ],
    "tool_permissions": [
        "how many tool permissions does {name} have",
        "which tools is {name} authorized for",
        "what equipment can {name} use",
        "{name} permissions",
        "how many tools is {name} authorized to use",
        "tool access of {name}",
        "what tools can {name} access",
        "which equipment is {name} allowed to use",
        "list {name}'s tool permissions",
        "what is {name}'s access level for equipment",
    ],
    "training": [
        "how many training sessions has {name} completed",
        "training record of {name}",
        "how many trainings did {name} attend",
        "{name} training sessions",
        "has {name} completed any training",
        "equipment training {name}",
    ],
    "cancellations": [
        "how many cancellations does {name} have",
        "how many reservations did {name} cancel",
        "{name} cancellations",
        "did {name} cancel any bookings",
        "cancellation record {name}",
    ],
    "session_reports": [
        "how many session reports has {name} filed",
        "how many reports did {name} submit",
        "{name} session reports",
        "equipment usage reports {name}",
        "how many usage reports does {name} have",
    ],
    "general": [
        "who is {name}",
        "tell me about {name}",
        "what is {name}'s role",
        "what does {name} do",
        "what department is {name} in",
        "what is {name}'s designation",
        "give me info on {name}",
        "{name} profile",
        "describe {name}",
    ],
    "leave_year": [
        "how many leave days did {name} take in {year}",
        "what is {name}'s leave record for {year}",
        "how many days was {name} on leave in {year}",
        "{name} leave {year}",
        "leave taken by {name} in {year}",
        "days off {name} {year}",
    ],
    "monthly_report": [
        "how many monthly reports has {name} submitted",
        "monthly report count for {name}",
        "how many reports did {name} file",
        "{name} report submissions",
        "what is {name}'s report rating",
        "monthly report stars {name}",
    ],
    "reservations_year": [
        "how many reservations did {name} make in {year}",
        "slot reservations of {name} in {year}",
        "{name} reservations {year}",
        "how many slots did {name} reserve in {year}",
    ],
    "compare_reservations": [
        "compare {name} reservations in {y1} and {y2}",
        "how did {name} slot bookings change from {y1} to {y2}",
        "was {name} more active in {y1} or {y2} for reservations",
    ],
    "reservation_no_year": [
        "how many total reservations does {name} have",
        "what is {name}'s lifetime slot booking count",
        "how many slots has {name} booked overall",
    ],
    "attendance_month_year": [
        "what was {name}'s attendance in {month} {year}",
        "how many days was {name} present in {month} {year}",
        "attendance of {name} for {month} {year}",
        "{name} attendance {month} {year}",
        "how regular was {name} in {month} {year}",
        "days present {name} {month} {year}",
        "was {name} at work in {month} {year}",
        "{name} {month} {year} attendance",
        "check {name} attendance for {month_abbr} {year}",
        "how much did {name} come in {month} {year}",
    ],
}

# ── Question templates ────────────────────────────────────────────────────────

SLOT_TEMPLATES_YEAR = [
    "How many equipment requests did {name} make in {year}?",
    "What was {name}'s slot activity in {year}?",
    "How active was {name} on equipment usage in {year}?",
    "Tell me about {name}'s equipment usage in {year}.",
    "What is the slot booking record of {name} for {year}?",
    "How many times did {name} request equipment in {year}?",
    "Give me {name}'s equipment request summary for {year}.",
]

ATTEND_TEMPLATES_YEAR = [
    "How many days was {name} present in {year}?",
    "What was {name}'s attendance in {year}?",
    "How often did {name} come to the institute in {year}?",
    "Tell me {name}'s attendance record for {year}.",
    "Was {name} regular in {year}?",
    "How many working days did {name} attend in {year}?",
]

COMPARE_SLOT_TEMPLATES = [
    "Compare {name}'s slot activity in {y1} and {y2}.",
    "How did {name}'s equipment usage change from {y1} to {y2}?",
    "What is the difference in {name}'s equipment requests between {y1} and {y2}?",
    "Compare equipment booking of {name} for {y1} vs {y2}.",
    "Show slot activity comparison of {name} for {y1} and {y2}.",
]

COMPARE_ATTEND_TEMPLATES = [
    "Compare {name}'s attendance in {y1} and {y2}.",
    "How did {name}'s attendance change from {y1} to {y2}?",
    "Was {name} more regular in {y1} or {y2}?",
    "Show attendance comparison for {name} between {y1} and {y2}.",
]

TOOL_TEMPLATES = [
    "Which equipment does {name} use most?",
    "What tools has {name} worked with?",
    "List the machines {name} has requested.",
    "What is {name}'s most used equipment?",
    "Which tools did {name} use?",
]

GENERAL_TEMPLATES = [
    "Who is {name}?",
    "Tell me about {name}.",
    "What is {name}'s role?",
    "What department is {name} in?",
    "What is {name}'s designation?",
]

RESERVATION_TEMPLATES_YEAR = [
    "How many reservations did {name} make in {year}?",
    "What was {name}'s slot reservation count in {year}?",
    "How many slots did {name} book in {year}?",
    "Tell me {name}'s reservation history for {year}.",
]

PROJECT_TEMPLATES = [
    "How many projects is {name} associated with?",
    "What projects does {name} have?",
    "Is {name} working on any active projects?",
    "Tell me about {name}'s research projects.",
]

PAPER_TEMPLATES = [
    "How many publications does {name} have?",
    "What papers has {name} published?",
    "How many research papers has {name} submitted?",
    "Tell me about {name}'s publications.",
]
# ADD these after existing templates

TRAINING_TEMPLATES = [
    "How many training sessions has {name} completed?",
    "What is {name}'s training record?",
    "How many equipment trainings did {name} attend?",
    "Has {name} completed any training?",
]
TRAINING_ANSWERS = [
    "{name} has completed {count} equipment training sessions.",
    "{name} attended {count} equipment training sessions.",
    "{count} equipment training sessions have been completed by {name}.",
    "A total of {count} training sessions are on record for {name}.",
    "{name} has a training record of {count} completed sessions.",
    
]
CANCELLATION_TEMPLATES = [
    "How many cancellations does {name} have?",
    "How many reservations did {name} cancel?",
    "What is {name}'s cancellation count?",
    "Did {name} cancel any bookings?",
]
CANCELLATION_ANSWERS = [
    "{name} has {count} reservation {cancel_word} on record.",
    "A total of {count} {cancel_word} {verb} been recorded for {name}.",
    "{name} cancelled {count} {'reservation' if count==1 else 'reservations'} in total.",
    "{count} reservation {cancel_word} {verb} recorded against {name}.",
]
SESSION_REPORT_TEMPLATES = [
    "How many session reports has {name} filed?",
    "How many equipment usage reports did {name} submit?",
    "What is {name}'s session report count?",
]
SESSION_REPORT_ANSWERS = [
    "{name} has filed {count} equipment session {report_word}.",
    "{count} equipment session {report_word} {verb} been submitted by {name}.",
    "{name} submitted {count} session {report_word} following equipment usage.",
    "A total of {count} session {report_word} {verb} filed by {name}.",
]
TOOL_PERM_TEMPLATES = [
    "How many tool permissions does {name} have?",
    "Which tools is {name} authorized to use?",
    "How many equipment access permissions does {name} hold?",
    "What equipment is {name} authorized for?",
]
TOOL_PERM_ANSWERS = [
    "{name} holds access permissions for {count} {piece_word} of equipment.",
    "Equipment access has been granted to {name} for {count} {piece_word}.",
    "{name} is authorized to use {count} {piece_word} of equipment.",
    "{count} equipment access {perm_word} {verb} granted to {name}.",
]
SYSTEM_OWNER_TEMPLATES = [
    "How many systems does {name} own?",
    "Which tools is {name} assigned as system owner for?",
    "How many tools is {name} responsible for?",
    "Is {name} a system owner?",
]
SYSTEM_OWNER_ANSWERS = [
    "{name} is currently assigned as system owner for {count} {tool_word}.",
    "{name} holds system ownership responsibilities for {count} {tool_word}.",
    "Currently, {name} serves as system owner for {count} {tool_word}.",
    "{count} {tool_word} {verb} currently assigned to {name} as system owner.",
]
RESERVATION_NO_YEAR_TEMPLATES = [
    "How many total reservations does {name} have?",
    "What is {name}'s lifetime slot booking count?",
    "How many slots has {name} booked overall?",
]

COMPARE_RESERVATION_TEMPLATES = [
    "Compare {name}'s reservations in {y1} and {y2}.",
    "How did {name}'s slot bookings change from {y1} to {y2}?",
    "Was {name} more active in {y1} or {y2} for reservations?",
]

LEAVE_TEMPLATES_YEAR = [
    "How many leave days did {name} take in {year}?",
    "What is {name}'s leave record for {year}?",
    "How many days was {name} on leave in {year}?",
    "What type of leaves did {name} take in {year}?",
]

PUBLICATION_TEMPLATES_STAFF = [
    "How many publications does {name} have?",
    "What papers has {name} published?",
    "How many approved publications does {name} have?",
    "Does {name} have any research papers?",
]

PROJECT_TEMPLATES_STAFF = [
    "How many projects is {name} associated with?",
    "What projects does {name} have?",
    "Is {name} working on any active projects?",
    "How many active projects does {name} have?",
]

LOGBOOK_TEMPLATES = [
    "How many logbook entries does {name} have?",
    "How many session log entries has {name} filled?",
    "How many instrument logs has {name} submitted?",
]

SLOT_ACTIVITY_STAFF_TEMPLATES = [
    "How many equipment requests did {name} submit in {year}?",
    "What was {name}'s slot activity in {year}?",
    "How many equipment usage requests did {name} make in {year}?",
    "How active was {name} on equipment in {year}?",
    "Tell me {name}'s equipment request summary for {year}.",
]
def _resolve_staff_uid(member_id: int, email: str) -> int | None:
    """Resolve HR member_id → slotbooking memberid. Same logic as models/staff.py."""
    if email:
        r = slots_query(
            "SELECT memberid FROM login WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 1",
            (email,)
        )
        if r:
            return r[0]["memberid"]
        prefix = email.split("@")[0] if "@" in email else ""
        if prefix:
            r = slots_query(
                "SELECT memberid FROM login WHERE LOWER(TRIM(email)) LIKE LOWER(%s) LIMIT 1",
                (f"{prefix}@%",)
            )
            if r:
                return r[0]["memberid"]
    r = slots_query(
        "SELECT memberid FROM login WHERE memberid = %s LIMIT 1",
        (member_id,)
    )
    return r[0]["memberid"] if r else None
def safe_int(val, default=0):
    try:
        return int(val or default)
    except Exception:
        return default

def _build_context_block(fields: dict) -> str:
    """
    Convert a dict of field→value into a readable context block.
    Skips zero counts, None, and empty values — these become negatives instead.
    Converts any list/dict values to clean strings.
    """
    lines = []
    items = list(fields.items())
    random.shuffle(items)  # Shuffle to prevent positional bias in training
    for k, v in items:
        if v is None or str(v) in ("", "N/A"):
            continue
        # Convert list of dicts (tool usage) to a clean comma-separated string
        if isinstance(v, list):
            clean = ", ".join(
                item.get("tool_name", str(item)) if isinstance(item, dict) else str(item)
                for item in v
            )
            lines.append(f"  {k} = {clean}")
        else:
            lines.append(f"  {k} = {v}")
    return "Context:\n" + "\n".join(lines) if lines else "Context:\n  (no data)"


def _make_negative(question: str, name: str) -> dict:
    """
    Refusal example — context has only the name, not the answer.
    Teaches: if the number isn't in Context, say not available.
    Uses LF line endings explicitly to prevent Windows CRLF corruption.
    """
    context = f"Context:\n  name = {name}"
    text = (
        "<|im_start|>system\n"
        "Answer questions using ONLY the data in the Context block. "
        "If the answer is not in the Context, respond with exactly: "
        "'This information is not available in the provided data.'\n"
        "<|im_end|>\n"
        f"<|im_start|>user\n{context}\n\nQuestion: {question}<|im_end|>\n"
        "<|im_start|>assistant\n"
        "This information is not available in the provided data.<|im_end|>"
    )
    # Force LF only — critical for JSONL compatibility
    return {"text": text.replace("\r\n", "\n").replace("\r", "\n")}


def _format_pair(context_fields: dict, question: str, answer: str) -> dict:
    """
    Format a positive Q&A pair in Qwen chat template format with context.
    Forces LF line endings to prevent Windows CRLF corruption in JSONL.
    """
    context_block = _build_context_block(context_fields)
    # If context has no real data (only name), make this a negative instead
    lines = [l for l in context_block.split("\n") if l.strip() and l.strip() != "Context:"]
    has_data = any(
        not l.strip().startswith("name =") and not l.strip() == "(no data)"
        for l in lines
    )
    if not has_data:
        return _make_negative(question, context_fields.get("name", "this person"))

    text = (
        "<|im_start|>system\n"
        "Answer questions using ONLY the data in the Context block. "
        "Every number in your answer must appear in the Context. "
        "If the answer is not in the Context, respond with exactly: "
        "'This information is not available in the provided data.'\n"
        "<|im_end|>\n"
        f"<|im_start|>user\n{context_block}\n\nQuestion: {question}<|im_end|>\n"
        f"<|im_start|>assistant\n{answer}<|im_end|>"
    )
    # Force LF only — critical for JSONL compatibility
    return {"text": text.replace("\r\n", "\n").replace("\r", "\n")}

def _pick_answer(template_list, **kwargs):
    """Pick a random answer template and fill it with kwargs."""
    return random.choice(template_list).format(**kwargs)
def generate():
    pairs = []
    years = [y for y in range (2007, 2026)]  # Last 20 years of data for richer temporal patterns

    # ── Step 1: Fetch members — mirror exactly what lab.py does ──────────────
    print("Fetching lab users...", file=sys.stderr)
    lab_members = slots_query("""
        SELECT memberid, fname, lname, position, department, email
        FROM login
        WHERE STR_TO_DATE(expiry_date, '%m/%d/%Y') >= CURDATE()
          AND (position IS NULL 
               OR position NOT IN ('IITBNF Staff'))
        ORDER BY memberid
    """) or []

    print(f"Found {len(lab_members)} lab users", file=sys.stderr)

    # ── Step 2: Fetch staff members — mirror exactly what staff.py does ───────
    print("Fetching staff members...", file=sys.stderr)
    staff_members = hr_query("""
        SELECT
            p.member_id,
            p.designation,
            p.team,
            p.email,
            COALESCE(rm.role_name, 'Staff') AS role_name
        FROM profile p
        LEFT JOIN role r         ON r.memberid  = p.member_id
        LEFT JOIN role_master rm ON rm.role_id  = r.role
        WHERE (p.leaving_date IS NULL
               OR p.leaving_date = '0000-00-00'
               OR p.leaving_date >= '2026-01-01')
          AND (p.taken_clearance IS NULL OR p.taken_clearance = 0)
        ORDER BY p.member_id
    """) or []

    print(f"Found {len(staff_members)} staff members", file=sys.stderr)

    # ── Step 3: Generate pairs for lab users ──────────────────────────────────
    print("Generating lab user pairs...", file=sys.stderr)
    for m in lab_members:
        fname = (m.get('fname') or '').strip()
        lname = (m.get('lname') or '').strip()
        name  = f"{fname} {lname}".strip()

        if not name or name == ' ':
            name = f"User {m['memberid']}"

        uid   = m['memberid']
        dept  = m.get('department') or 'Unknown Department'
        pos   = m.get('position')   or 'lab user'

        # General identity
        identity_ctx = {"name": name, "position": pos, "department": dept}
        identity_answer = f"{name} is a {pos} in the {dept} department."
        for tpl in GENERAL_TEMPLATES:
            question = tpl.format(name=name)
            pairs.append(_format_pair(identity_ctx, question, identity_answer))
            # Add negative ~30% of the time
            if random.random() < 0.3:
                pairs.append(_make_negative(question, name))

        # Per-year slot activity
        year_slot_data = {}
        for year in years:
            eq = slots_query("""
                SELECT
                    COUNT(*)                                        AS total,
                    SUM(CASE WHEN status=3 THEN 1 ELSE 0 END)      AS booked,             
                    SUM(CASE WHEN status=1 THEN 1 ELSE 0 END)      AS approved,
                    SUM(CASE WHEN status=0 THEN 1 ELSE 0 END)      AS pending,
                    SUM(CASE WHEN status=2 THEN 1 ELSE 0 END)      AS rejected,
                    COUNT(DISTINCT equipmentid)                     AS tools
                FROM equipment_usage_approval
                WHERE requestedby=%s AND YEAR(date_of_request)=%s
            """, (uid, year))

            if eq and eq[0] and safe_int(eq[0]['total']) > 0:
                r       = eq[0]
                total   = safe_int(r['total'])
                booked  = safe_int(r['booked'])
                pending = safe_int(r['pending'])
                approved= safe_int(r['approved'])
                rejected= safe_int(r['rejected'])
                tools   = safe_int(r['tools'])
                year_slot_data[year] = r

                # Replace the answer construction for slot activity with this:
                parts = [
                    f"In {year}, {name} submitted {total} equipment usage "
                    f"{'request' if total==1 else 'requests'} across {tools} "
                    f"{'tool' if tools==1 else 'tools'}."
                ]
                if booked:
                    parts.append(f"{booked} {'was' if booked==1 else 'were'} slot-booked.")
                if pending:
                    parts.append(f"{pending} {'was' if pending==1 else 'were'} pending.")
                if approved:
                    parts.append(f"{approved} {'was' if approved==1 else 'were'} approved.")
                if rejected:
                    parts.append(f"{rejected} {'was' if rejected==1 else 'were'} rejected.")
                answer = " ".join(parts)
                slot_ctx = {
                    "name": name,
                    "year": year,
                    "eq_requests": total,
                    "eq_slot_booked": booked,
                    "eq_pending": pending,
                    "eq_approved": approved,
                    "eq_rejected": rejected,
                    "tools_used": tools,
                }
                for tpl in PARAPHRASE_VARIANTS["slot_year"]:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(slot_ctx, question, answer))
                    if random.random() < 0.2:
                        pairs.append(_make_negative(question, name))

        # Year-comparison slot
        available_years = sorted(year_slot_data.keys())
        if len(available_years) >= 2:
            all_pairs = [(available_years[i], available_years[j])
             for i in range(len(available_years))
             for j in range(i+1, len(available_years))]
            random.shuffle(all_pairs)
            for y1, y2 in all_pairs[:8]:
                    t1  = safe_int(year_slot_data[y1]['total'])
                    t2  = safe_int(year_slot_data[y2]['total'])
                    diff = t2 - t1
                    trend = (
                        f"+{diff} more requests in {y2}"   if diff > 0
                        else f"{abs(diff)} fewer requests in {y2}" if diff < 0
                        else "same number of requests in both years"
                    )
                    answer = (
                        f"Comparing {name}'s equipment activity: "
                        f"In {y1}, {t1} requests. "
                        f"In {y2}, {t2} requests. "
                        f"This represents {trend}."
                    )
                    compare_ctx = {
                        "name": name,
                        f"eq_requests_{y1}": t1,
                        f"eq_requests_{y2}": t2,
                    }
                    for tpl in COMPARE_SLOT_TEMPLATES:
                        question = tpl.format(name=name, y1=y1, y2=y2)
                        pairs.append(_format_pair(compare_ctx, question, answer))
                        if random.random() < 0.25:
                            pairs.append(_make_negative(question, name))

        # Per-year reservations
        for year in years:
            res = slots_query("""
                SELECT COUNT(*) AS total
                FROM reservations
                WHERE memberid=%s
                  AND YEAR(FROM_UNIXTIME(startdate))=%s
                  AND isblackout=1
            """, (uid, year))
            total = safe_int(res[0]['total'] if res and res[0] else 0)
            if total > 0:
                answer = (
                    f"{name} made {total} slot "
                    f"{'reservation' if total==1 else 'reservations'} in {year}."
                )
                reservation_ctx = {
                    "name": name,
                    "year": year,
                    "reservations": total
                }
                for tpl in RESERVATION_TEMPLATES_YEAR:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(reservation_ctx, question, answer))
                    if random.random() < 0.3:
                        pairs.append(_make_negative(question, name))

        # Tool usage
        tools = slots_query("""
            SELECT r.name AS tool_name, COUNT(*) AS times
            FROM equipment_usage_approval e
            JOIN resources r ON r.machid = e.equipmentid
            WHERE e.requestedby=%s
            GROUP BY r.machid, r.name
            ORDER BY times DESC
            LIMIT 5
        """, (uid,))

        if tools:
            top_tool  = tools[0]['tool_name']
            tool_list = ", ".join(t['tool_name'] for t in tools)
            answer = (
                f"{name}'s most used equipment is {top_tool}. "
                f"Tools used overall: {tool_list}."
            )
            tool_ctx = {
                "name": name,
                "tools_used": tools
            }
            for tpl in TOOL_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(tool_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # Publications
        papers = slots_query("""
            SELECT COUNT(*) AS total
            FROM paper_publish
            WHERE memberid=%s AND approve=1
        """, (uid,))
        pp_count = safe_int(papers[0]['total'] if papers and papers[0] else 0)
        if pp_count > 0:
            answer = (
                f"{name} has {pp_count} approved "
                f"{'publication' if pp_count==1 else 'publications'} "
                f"associated with IITBNF."
            )
            paper_ctx = {
                "name": name,
                "papers": pp_count
            }
            for tpl in PAPER_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(paper_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # Projects
        proj = slots_query("""
            SELECT
                COUNT(*)                                    AS total,
                SUM(CASE WHEN active=1 THEN 1 ELSE 0 END)  AS active
            FROM faculty_projects
            WHERE memberid=%s
        """, (uid,))
        if proj and proj[0] and safe_int(proj[0]['total']) > 0:
            total  = safe_int(proj[0]['total'])
            active = safe_int(proj[0]['active'])
            answer = (
                f"{name} is associated with {total} faculty "
                f"{'project' if total==1 else 'projects'}, "
                f"of which {active} "
                f"{'is' if active==1 else 'are'} currently active."
            )
            project_ctx = {
                "name": name,
                "projects": total,
                "active_projects": active
            }
            for tpl in PROJECT_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(project_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))
    # ── Tool permissions (lab) ────────────────────────────────────────────
        perms = slots_query(
            "SELECT COUNT(*) AS total FROM permissions WHERE memberid=%s", (uid,)
        )
        perm_count = safe_int(perms[0]['total'] if perms and perms[0] else 0)
        if perm_count > 0:
            answer = (f"{name} holds access permissions for {perm_count} "
                      f"{'piece' if perm_count==1 else 'pieces'} of equipment.")
            perm_ctx = {"name": name, "tool_permissions_count": perm_count}
            for tpl in TOOL_PERM_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(perm_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── Training (lab) ────────────────────────────────────────────────────
        tr = slots_query(
            "SELECT COUNT(*) AS total FROM training_report WHERE memberid=%s", (uid,)
        )
        tr_count = safe_int(tr[0]['total'] if tr and tr[0] else 0)
        if tr_count > 0:
            answer = (f"{name} has completed {tr_count} equipment training "
                      f"{'session' if tr_count==1 else 'sessions'}.")
            tr_ctx = {"name": name, "trainings": tr_count}
            for tpl in TRAINING_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(tr_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── Cancellations (lab) ───────────────────────────────────────────────
        cc = slots_query(
            "SELECT COUNT(*) AS total FROM cancel_reservation WHERE memberid=%s", (uid,)
        )
        cc_count = safe_int(cc[0]['total'] if cc and cc[0] else 0)
        if cc_count > 0:
            answer = (f"{name} has {cc_count} reservation "
                      f"{'cancellation' if cc_count==1 else 'cancellations'} on record.")
            cc_ctx = {"name": name, "cancellations": cc_count}
            for tpl in CANCELLATION_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(cc_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── Session reports (lab) ─────────────────────────────────────────────
        sr = slots_query(
            "SELECT COUNT(*) AS total FROM reporting WHERE memberid=%s", (uid,)
        )
        sr_count = safe_int(sr[0]['total'] if sr and sr[0] else 0)
        if sr_count > 0:
            rw = 'report' if sr_count == 1 else 'reports'
            vb = 'has' if sr_count == 1 else 'have'
            answer = _pick_answer(SESSION_REPORT_ANSWERS, name=name, count=sr_count, report_word=rw, verb=vb)
            sr_ctx = {"name": name, "session_reports": sr_count}
            for tpl in SESSION_REPORT_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(sr_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── System ownership (lab) ────────────────────────────────────────────
        so_rows = slots_query(
            "SELECT machid FROM system_owner WHERE memberid=%s", (uid,)
        ) or []
        owned_count = 0
        for r in so_rows:
            raw = str(r.get("machid") or "")
            owned_count += len([x for x in raw.split(",") if x.strip().isdigit()])
        if owned_count > 0:
            answer = (f"{name} is currently assigned as system owner for {owned_count} "
                      f"{'tool' if owned_count==1 else 'tools'}.")
            so_ctx = {"name": name, "systems_owned_current": owned_count}
            for tpl in SYSTEM_OWNER_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(so_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── Compare reservations (lab) ────────────────────────────────────────
        year_res_data = {}
        for year in years:
            res = slots_query(
                "SELECT COUNT(*) AS total FROM reservations WHERE memberid=%s "
                "AND YEAR(FROM_UNIXTIME(startdate))=%s AND isblackout=1",
                (uid, year)
            )
            total = safe_int(res[0]['total'] if res and res[0] else 0)
            if total > 0:
                year_res_data[year] = total
        avail_res = sorted(year_res_data.keys())
        if len(avail_res) >= 2:
            for i in range(len(avail_res)):
                for j in range(i+1, len(avail_res)):
                    y1, y2 = avail_res[i], avail_res[j]
                    r1, r2 = year_res_data[y1], year_res_data[y2]
                    diff = r2 - r1
                    trend = (f"+{diff} more in {y2}" if diff > 0
                             else f"{abs(diff)} fewer in {y2}" if diff < 0
                             else "same in both years")
                    answer = (f"Reservation comparison for {name}: "
                              f"{y1} — {r1}. {y2} — {r2}. {trend}.")
                    ctx = {"name": name, f"reservations_{y1}": r1, f"reservations_{y2}": r2}
                    for tpl in PARAPHRASE_VARIANTS["compare_reservations"]:
                        question = tpl.format(name=name, y1=y1, y2=y2)
                        pairs.append(_format_pair(ctx, question, answer))
                        if random.random() < 0.25:
                            pairs.append(_make_negative(question, name))
            # Add inside the lab_members loop, after existing sections:
        if m.get('position') and pos not in ('', 'N/A'):
            cat_ctx = {"name": name, "category": pos, "department": dept}
            question = f"what category is {name}"
            answer = f"{name} is a {pos} user in the {dept} department."
            pairs.append(_format_pair(cat_ctx, question, answer))

        supervisor_rows = slots_query(
            "SELECT TRIM(CONCAT(COALESCE(s.fname,''), ' ', COALESCE(s.lname,''))) AS sup_name "
            "FROM login l LEFT JOIN login s ON s.memberid = l.supervisor "
            "WHERE l.memberid=%s LIMIT 1", (uid,)
        )
        if supervisor_rows and supervisor_rows[0].get('sup_name', '').strip():
            sup_name = supervisor_rows[0]['sup_name'].strip()
            sup_ctx = {"name": name, "supervisor_name": sup_name}
            for q in [
                f"who is {name}'s supervisor",
                f"who supervises {name}",
                f"what is {name}'s guide name",
                f"supervisor of {name}",
            ]:
                pairs.append(_format_pair(sup_ctx, q, 
                    f"{name}'s supervisor is {sup_name}."))
    # ── Step 4: Generate pairs for staff members ──────────────────────────────
    print("Generating staff pairs...", file=sys.stderr)
    for s in staff_members:
        mid   = s['member_id']
        desig = s.get('designation') or 'staff member'
        team  = s.get('team')        or 'Unknown Team'
        email = s.get('email')       or ''

        # Resolve display name
        name = None
        if email:
            r = slots_query(
                "SELECT fname, lname FROM login "
                "WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) LIMIT 1",
                (email,)
            )
            if r:
                fname = (r[0].get('fname') or '').strip()
                lname = (r[0].get('lname') or '').strip()
                name  = f"{fname} {lname}".strip()
        if not name:
            name = f"Member {str(mid).zfill(4)}"

        # Resolve slotbooking UID — CRITICAL for all slot queries
        uid = _resolve_staff_uid(mid, email)

        # ── Identity ──────────────────────────────────────────────────────────
        staff_identity_ctx = {"name": name, "designation": desig, "team": team}
        staff_identity_answer = f"{name} is a {desig} in the {team} team at IITBNF."
        for tpl in GENERAL_TEMPLATES:
            question = tpl.format(name=name)
            pairs.append(_format_pair(staff_identity_ctx, question, staff_identity_answer))
            if random.random() < 0.3:
                pairs.append(_make_negative(question, name))

        # ── Attendance per year ────────────────────────────────────────────────
        year_attend_data = {}
        for year in years:
            att = hr_query(
                "SELECT COUNT(*) AS days_present FROM user_attendance "
                "WHERE memberid=%s AND YEAR(date)=%s",
                (mid, year)
            )
            days = safe_int(att[0]['days_present'] if att and att[0] else 0)
            if days > 0:
                year_attend_data[year] = days
                answer = f"In {year}, {name} was present for {days} working {'day' if days==1 else 'days'}."
                att_ctx = {"name": name, "year": year, "days_present": days}
                for tpl in PARAPHRASE_VARIANTS["attendance_year"]:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(att_ctx, question, answer))
                    if random.random() < 0.3:
                        pairs.append(_make_negative(question, name))
        # ── Per-month attendance ──────────────────────────────────────────────
        MONTH_NAMES_FULL = {
            1:'January',2:'February',3:'March',4:'April',
            5:'May',6:'June',7:'July',8:'August',
            9:'September',10:'October',11:'November',12:'December'
        }
        MONTH_ABBR = {
            1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
            7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'
        }
        for year in years:
            for month_num in range(1, 13):
                att_month = hr_query(
                    "SELECT COUNT(*) AS days_present FROM user_attendance "
                    "WHERE memberid=%s AND YEAR(date)=%s AND MONTH(date)=%s",
                    (mid, year, month_num)
                )
                days = safe_int(att_month[0]['days_present'] if att_month and att_month[0] else 0)
                if days > 0:
                    month_name = MONTH_NAMES_FULL[month_num]
                    month_abbr = MONTH_ABBR[month_num]
                    answer = (f"{name} was present for {days} working "
                            f"{'day' if days==1 else 'days'} in {month_name} {year}.")
                    att_month_ctx = {
                        "name": name,
                        "year": year,
                        "month": month_name,
                        "days_present": days
                    }
                    for tpl in PARAPHRASE_VARIANTS["attendance_month_year"]:
                        question = tpl.format(
                            name=name, year=year,
                            month=month_name, month_abbr=month_abbr
                        )
                        pairs.append(_format_pair(att_month_ctx, question, answer))
                        if random.random() < 0.25:
                            pairs.append(_make_negative(question, name))
        # ── Compare attendance ─────────────────────────────────────────────────
        available_att = sorted(year_attend_data.keys())
        if len(available_att) >= 2:
            for i in range(len(available_att)):
                for j in range(i+1, len(available_att)):
                    y1, y2 = available_att[i], available_att[j]
                    d1, d2 = year_attend_data[y1], year_attend_data[y2]
                    diff = d2 - d1
                    trend = (f"{name} attended {diff} more days in {y2}" if diff > 0
                             else f"{name} attended {abs(diff)} fewer days in {y2}" if diff < 0
                             else f"{name} had the same attendance in both years")
                    answer = (f"Attendance comparison for {name}: "
                              f"{y1} — {d1} days. {y2} — {d2} days. {trend}.")
                    ctx = {"name": name, f"days_present_{y1}": d1, f"days_present_{y2}": d2}
                    for tpl in PARAPHRASE_VARIANTS["compare_attendance"]:
                        question = tpl.format(name=name, y1=y1, y2=y2)
                        pairs.append(_format_pair(ctx, question, answer))
                        if random.random() < 0.25:
                            pairs.append(_make_negative(question, name))

        # ── Leave per year ────────────────────────────────────────────────────
        for year in years:
            lv = hr_query("""
                SELECT type_of_leave, SUM(DATEDIFF(to_date,from_date)+1) AS days_taken
                FROM leaves WHERE memberid=%s AND status=1 AND YEAR(from_date)=%s
                GROUP BY type_of_leave
            """, (mid, year))
            if lv:
                breakdown = {r['type_of_leave']: safe_int(r['days_taken']) for r in lv}
                total = sum(breakdown.values())
                bd_str = ", ".join(f"{k}: {v} day{'s' if v!=1 else ''}" for k, v in breakdown.items())
                answer = (f"{name} took {total} leave {'day' if total==1 else 'days'} in {year}"
                          + (f" ({bd_str})." if bd_str else "."))
                lv_ctx = {"name": name, "year": year, "leaves_taken": total, "leave_breakdown": bd_str}
                for tpl in PARAPHRASE_VARIANTS["leave_year"]:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(lv_ctx, question, answer))
                    if random.random() < 0.3:
                        pairs.append(_make_negative(question, name))

        # ── Monthly reports ───────────────────────────────────────────────────
        mr = hr_query(
            "SELECT COUNT(*) AS submitted, AVG(star) AS avg_stars FROM monthly_report WHERE member_id=%s",
            (mid,)
        )
        if mr and mr[0] and safe_int(mr[0]['submitted']) > 0:
            submitted = safe_int(mr[0]['submitted'])
            avg_stars = round(float(mr[0]['avg_stars'] or 0), 1)
            mr_ctx = {"name": name, "monthly_reports_submitted": submitted, "monthly_report_avg_stars": avg_stars}
            question = f"How many monthly reports has {name} submitted?"
            mr_answer = (f"{name} has submitted {submitted} monthly "
                         f"{'report' if submitted==1 else 'reports'} "
                         f"with an average rating of {avg_stars} stars.")
            pairs.append(_format_pair(mr_ctx, question, mr_answer))
            if random.random() < 0.3:
                pairs.append(_make_negative(question, name))

        # ── All slotbooking sections (require uid) ────────────────────────────
        if not uid:
            continue  # No slotbooking account found — skip slot sections

        # ── Slot activity per year ────────────────────────────────────────────
        year_slot_data = {}
        for year in years:
            eq = slots_query("""
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS booked,
                    SUM(CASE WHEN status=0 THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) AS rejected,
                    COUNT(DISTINCT equipmentid) AS tools
                FROM equipment_usage_approval
                WHERE requestedby=%s AND YEAR(date_of_request)=%s
            """, (uid, year))
            if eq and eq[0] and safe_int(eq[0]['total']) > 0:
                r = eq[0]
                total   = safe_int(r['total'])
                booked  = safe_int(r['booked'])
                pending = safe_int(r['pending'])
                rejected= safe_int(r['rejected'])
                tools   = safe_int(r['tools'])
                year_slot_data[year] = r
                parts = [f"In {year}, {name} submitted {total} equipment usage "
                         f"{'request' if total==1 else 'requests'} across {tools} "
                         f"{'tool' if tools==1 else 'tools'}."]
                if booked:  parts.append(f"{booked} {'was' if booked==1 else 'were'} slot-booked.")
                if pending: parts.append(f"{pending} {'was' if pending==1 else 'were'} pending.")
                if rejected:parts.append(f"{rejected} {'was' if rejected==1 else 'were'} rejected.")
                answer = " ".join(parts)
                slot_ctx = {"name": name, "year": year, "eq_requests": total,
                            "eq_slot_booked": booked, "eq_pending": pending,
                            "eq_rejected": rejected, "tools_used": tools}
                for tpl in PARAPHRASE_VARIANTS["slot_year"]:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(slot_ctx, question, answer))
                    if random.random() < 0.2:
                        pairs.append(_make_negative(question, name))

        # ── Compare slot activity ─────────────────────────────────────────────
        available_slot = sorted(year_slot_data.keys())
        if len(available_slot) >= 2:
            for i in range(len(available_slot)):
                for j in range(i+1, len(available_slot)):
                    y1, y2 = available_slot[i], available_slot[j]
                    t1 = safe_int(year_slot_data[y1]['total'])
                    t2 = safe_int(year_slot_data[y2]['total'])
                    diff = t2 - t1
                    trend = (f"+{diff} more requests in {y2}" if diff > 0
                             else f"{abs(diff)} fewer requests in {y2}" if diff < 0
                             else "same number of requests in both years")
                    answer = (f"Equipment request comparison for {name}: "
                              f"In {y1}, {t1} requests. In {y2}, {t2} requests. {trend}.")
                    ctx = {"name": name, f"eq_requests_{y1}": t1, f"eq_requests_{y2}": t2}
                    for tpl in PARAPHRASE_VARIANTS["compare_slot"]:
                        question = tpl.format(name=name, y1=y1, y2=y2)
                        pairs.append(_format_pair(ctx, question, answer))
                        if random.random() < 0.25:
                            pairs.append(_make_negative(question, name))

        # ── Reservations per year ─────────────────────────────────────────────
        year_res_data = {}
        for year in years:
            res = slots_query("""
                SELECT COUNT(*) AS total, COUNT(DISTINCT machid) AS tools
                FROM reservations WHERE memberid=%s
                AND YEAR(FROM_UNIXTIME(startdate))=%s AND isblackout=1
            """, (uid, year))
            total = safe_int(res[0]['total'] if res and res[0] else 0)
            if total > 0:
                year_res_data[year] = total
                answer = (f"{name} made {total} slot {'reservation' if total==1 else 'reservations'} "
                          f"in {year}.")
                res_ctx = {"name": name, "year": year, "reservations": total}
                for tpl in PARAPHRASE_VARIANTS["reservations_year"]:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(res_ctx, question, answer))
                    if random.random() < 0.3:
                        pairs.append(_make_negative(question, name))

        # Total lifetime reservations
        res_total = slots_query(
            "SELECT COUNT(*) AS total FROM reservations WHERE memberid=%s AND isblackout=1",
            (uid,)
        )
        rt = safe_int(res_total[0]['total'] if res_total and res_total[0] else 0)
        if rt > 0:
            answer = f"{name} has made {rt} slot {'reservation' if rt==1 else 'reservations'} in total."
            res_total_ctx = {"name": name, "total_reservations": rt}
            for tpl in PARAPHRASE_VARIANTS["reservation_no_year"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(res_total_ctx, question, answer))

        # ── Compare reservations ──────────────────────────────────────────────
        avail_res = sorted(year_res_data.keys())
        if len(avail_res) >= 2:
            for i in range(len(avail_res)):
                for j in range(i+1, len(avail_res)):
                    y1, y2 = avail_res[i], avail_res[j]
                    r1, r2 = year_res_data[y1], year_res_data[y2]
                    diff = r2 - r1
                    trend = (f"+{diff} more in {y2}" if diff > 0
                             else f"{abs(diff)} fewer in {y2}" if diff < 0
                             else "same number in both years")
                    answer = (f"Reservation comparison for {name}: "
                              f"{y1} — {r1}. {y2} — {r2}. {trend}.")
                    ctx = {"name": name, f"reservations_{y1}": r1, f"reservations_{y2}": r2}
                    for tpl in PARAPHRASE_VARIANTS["compare_reservations"]:
                        question = tpl.format(name=name, y1=y1, y2=y2)
                        pairs.append(_format_pair(ctx, question, answer))

        # ── Tool permissions ──────────────────────────────────────────────────
        perms = slots_query(
            "SELECT COUNT(*) AS total FROM permissions WHERE memberid=%s", (uid,)
        )
        perm_count = safe_int(perms[0]['total'] if perms and perms[0] else 0)
        if perm_count > 0:
            pw = 'piece' if perm_count == 1 else 'pieces'
            pmw = 'permission' if perm_count == 1 else 'permissions'
            vb = 'has' if perm_count == 1 else 'have'
            answer = _pick_answer(TOOL_PERM_ANSWERS, name=name, count=perm_count, piece_word=pw, perm_word=pmw, verb=vb)
            perm_ctx = {"name": name, "tool_permissions_count": perm_count}
            for tpl in PARAPHRASE_VARIANTS["tool_permissions"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(perm_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── System ownership ──────────────────────────────────────────────────
        so_rows = slots_query(
            "SELECT machid FROM system_owner WHERE memberid=%s", (uid,)
        ) or []
        owned_count = 0
        if so_rows:
            for r in so_rows:
                raw = str(r.get("machid") or "")
                owned_count += len([x for x in raw.split(",") if x.strip().isdigit()])
        if owned_count > 0:
            tw = 'tool' if owned_count == 1 else 'tools'
            vb = 'is' if owned_count == 1 else 'are'
            answer = _pick_answer(SYSTEM_OWNER_ANSWERS, name=name, count=owned_count, tool_word=tw, verb=vb)
            so_ctx = {"name": name, "systems_owned_current": owned_count}
            for tpl in PARAPHRASE_VARIANTS["system_owner"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(so_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))
        # ── Training ──────────────────────────────────────────────────────────
        tr = slots_query(
            "SELECT COUNT(*) AS total FROM training_report WHERE memberid=%s", (uid,)
        )
        tr_count = safe_int(tr[0]['total'] if tr and tr[0] else 0)
        if tr_count > 0:
            # NEW — random answer from paraphrase list
            sw = 'session' if tr_count == 1 else 'sessions'
            vb = 'has' if tr_count == 1 else 'have'
            answer = _pick_answer(
                TRAINING_ANSWERS,
                name=name, count=tr_count, session_word=sw, verb=vb
            )
            tr_ctx = {"name": name, "trainings": tr_count}
            for tpl in PARAPHRASE_VARIANTS["training"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(tr_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── Cancellations ─────────────────────────────────────────────────────
        cc = slots_query(
            "SELECT COUNT(*) AS total FROM cancel_reservation WHERE memberid=%s", (uid,)
        )
        cc_count = safe_int(cc[0]['total'] if cc and cc[0] else 0)
        if cc_count > 0:
            answer = (f"{name} has {cc_count} reservation "
                      f"{'cancellation' if cc_count==1 else 'cancellations'} on record.")
            cc_ctx = {"name": name, "cancellations": cc_count}
            for tpl in PARAPHRASE_VARIANTS["cancellations"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(cc_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── Session reports ───────────────────────────────────────────────────
        sr = slots_query(
            "SELECT COUNT(*) AS total FROM reporting WHERE memberid=%s", (uid,)
        )
        sr_count = safe_int(sr[0]['total'] if sr and sr[0] else 0)
        if sr_count > 0:
            rw = 'report' if sr_count == 1 else 'reports'
            vb = 'has' if sr_count == 1 else 'have'
            answer = _pick_answer(SESSION_REPORT_ANSWERS, name=name, count=sr_count, report_word=rw, verb=vb)
            sr_ctx = {"name": name, "session_reports": sr_count}
            for tpl in PARAPHRASE_VARIANTS["session_reports"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(sr_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))    
        
                # ── Zero-value negative pairs for staff (fields confirmed present but empty) ──
        # For any metric where a staff member has NO data, generate a "zero answer"
        # pair rather than skipping silently. This is the Change 1 complement:
        # _build_context_block now keeps zero values, so we need matching training
        # examples that show how to correctly answer "0" vs "not available".
        for metric_label, count_val, templates, ans_fn in [
            ("trainings",     tr_count,      TRAINING_TEMPLATES,       
            lambda c: f"{name} has completed 0 equipment training sessions."),
            ("cancellations", cc_count,      CANCELLATION_TEMPLATES,   
            lambda c: f"{name} has 0 reservation cancellations on record."),
            ("session_reports", sr_count,    SESSION_REPORT_TEMPLATES, 
            lambda c: f"{name} has filed 0 equipment session reports."),
        ]:
            if count_val == 0:
                zero_ctx = {"name": name, metric_label: 0}
                zero_answer = ans_fn(name)
                for tpl in templates:
                    question = tpl.format(name=name)
                    pairs.append(_format_pair(zero_ctx, question, zero_answer))
        # ── Publications (staff) ──────────────────────────────────────────────
        pp = slots_query(
            "SELECT COUNT(*) AS total FROM paper_publish WHERE memberid=%s AND approve=1", (uid,)
        )
        pp_count = safe_int(pp[0]['total'] if pp and pp[0] else 0)
        if pp_count > 0:
            answer = (f"{name} has {pp_count} approved research "
                      f"{'publication' if pp_count==1 else 'publications'} associated with IITBNF.")
            pp_ctx = {"name": name, "papers": pp_count}
            for tpl in PARAPHRASE_VARIANTS["publications"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(pp_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

        # ── Projects (staff) ──────────────────────────────────────────────────
        fp = slots_query("""
            SELECT COUNT(*) AS total, SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active
            FROM faculty_projects WHERE memberid=%s
        """, (uid,))
        if fp and fp[0] and safe_int(fp[0]['total']) > 0:
            total  = safe_int(fp[0]['total'])
            active = safe_int(fp[0]['active'])
            answer = (f"{name} is associated with {total} faculty "
                      f"{'project' if total==1 else 'projects'}, "
                      f"of which {active} {'is' if active==1 else 'are'} currently active.")
            fp_ctx = {"name": name, "projects": total, "active_projects": active}
            for tpl in PARAPHRASE_VARIANTS["projects"]:
                question = tpl.format(name=name)
                pairs.append(_format_pair(fp_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))
        # Add this block inside generate(), after all member loops, before the shuffle

    # Follow-up conversation pairs
    # FOLLOWUP_PAIRS = [
    #     {
    #         "context_key": "attendance_pct",
    #         "question": "is that above the threshold",
    #         "answer_fn": lambda ctx: (
    #             "Yes, {pct}% is above the 75% mandatory threshold.".format(pct=ctx.get("attendance_pct"))
    #             if float(ctx.get("attendance_pct", 0)) >= 75
    #             else "No, {pct}% is below the 75% mandatory threshold.".format(pct=ctx.get("attendance_pct"))
    #         )
    #     },
    #     {
    #         "context_key": "attendance_pct",
    #         "question": "what does that mean",
    #         "answer_fn": lambda ctx: (
    #             "An attendance of {pct}% means the staff member meets the facility requirement.".format(pct=ctx.get("attendance_pct"))
    #             if float(ctx.get("attendance_pct", 0)) >= 75
    #             else "An attendance of {pct}% means the staff member is below the 75% mandatory minimum and may need follow-up.".format(pct=ctx.get("attendance_pct"))
    #         )
    #     },
    #     {
    #         "context_key": "eq_requests",
    #         "question": "how many were approved",
    #         "answer_fn": lambda ctx: (
    #             "{approved} of the {total} equipment requests were slot-booked or approved.".format(
    #                 approved=ctx.get("eq_slot_booked", 0),
    #                 total=ctx.get("eq_requests", 0)
    #             )
    #         )
    #     },
    #     {
    #         "context_key": "papers",
    #         "question": "how many papers",
    #         "answer_fn": lambda ctx: (
    #             "{name} has {papers} approved publication{s}.".format(
    #                 name=ctx.get("name"),
    #                 papers=ctx.get("papers"),
    #                 s="s" if int(ctx.get("papers", 0)) != 1 else ""
    #             )
    #         )
    #     },
    # ]

    # Add facility knowledge pairs
    facility_pairs = _generate_facility_pairs()
    pairs.extend(facility_pairs)
    print(f"  Facility pairs : {len(facility_pairs)}", file=sys.stderr)
    # ── Step 5: Shuffle and return ────────────────────────────────────────────
    random.shuffle(pairs)

    print(
        f"\nTotal pairs generated: {len(pairs)}",
        file=sys.stderr
    )
    print(
        f"  Lab user pairs : from {len(lab_members)} users",
        file=sys.stderr
    )
    print(
        f"  Staff pairs    : from {len(staff_members)} staff",
        file=sys.stderr
    )

    return pairs
def _generate_facility_pairs() -> list:
    """
    Static facility knowledge Q&A pairs.
    These teach the SLM about IITBNF policies, processes, and structure
    so it can answer facility-level questions without hitting the DB.
    """
    pairs = []
    
    FACILITY_QA = [
        # About IITBNF
        ("what is iitbnf", 
         "IITBNF is the IIT Bombay Nanofabrication Facility, a Class 100/1000 cleanroom at IIT Bombay, Powai, Mumbai. It provides fabrication and characterization services to researchers from IIT Bombay and external institutions."),
        ("where is iitbnf located",
         "IITBNF is located at IIT Bombay, Powai, Mumbai — 400076, Maharashtra, India."),
        ("what are iitbnf operating hours",
         "IITBNF operates Monday to Friday, 9:00 AM to 6:00 PM."),
        # Attendance policy
        ("what is the attendance policy at iitbnf",
         "IITBNF requires staff to maintain a minimum attendance of 75% of working days per year. Staff below this threshold may be flagged for management review."),
        ("what is the mandatory attendance threshold",
         "The mandatory attendance threshold at IITBNF is 75% of working days per year."),
        ("what happens if attendance is below 75 percent",
         "Staff with attendance below the 75% mandatory threshold may be flagged for management review."),
        # Leave types
        ("what types of leave are available at iitbnf",
         "Leave types available at IITBNF include: CL (Casual Leave), EL (Earned Leave), ML (Medical Leave), and RL (Restricted Leave)."),
        ("what is casual leave",
         "CL stands for Casual Leave, one of the leave types available to IITBNF staff."),
        ("what is earned leave",
         "EL stands for Earned Leave, one of the leave types available to IITBNF staff."),
        # Booking process
        ("how do i book equipment at iitbnf",
         "To book equipment at IITBNF: (1) Submit an equipment usage request through the slotbooking portal. (2) The system owner or faculty incharge reviews and approves the request. (3) Once approved, your slot booking is confirmed. (4) Use the equipment during your booked slot. (5) Submit a session report after use."),
        ("what is the equipment booking process",
         "Equipment booking at IITBNF involves submitting a usage request on the slotbooking portal, getting approval from the system owner or faculty incharge, then using the equipment during your confirmed slot and submitting a session report afterwards."),
        # User categories
        ("what is a phd user at iitbnf",
         "A PhD user at IITBNF is a doctoral researcher working towards a PhD degree who has been granted access to use facility equipment."),
        ("what is an inup user",
         "An INUP user is a visiting researcher at IITBNF under the INUP (Indian Nanoelectronics Users Programme) national nanofabrication programme."),
        ("what is a pdf user",
         "A PDF user at IITBNF is a Postdoctoral Fellow conducting research at the facility."),
        # Roles
        ("what is a system owner at iitbnf",
         "A system owner at IITBNF is a staff member assigned responsibility for specific equipment, including overseeing maintenance and handling operational issues for that tool."),
        ("what does a system owner do",
         "A system owner at IITBNF is responsible for a specific piece of equipment. They oversee the tool's operational status, coordinate maintenance, and handle error reports related to that equipment."),
        # Equipment categories
        ("what types of equipment does iitbnf have",
         "IITBNF equipment categories include: Deposition (PECVD, LPCVD, Sputtering, Evaporation), Lithography (Spin coaters, Mask aligners, E-beam), Etching (RIE, ICP, Wet bench), Characterization (SEM, TEM, AFM, XRD, XPS), Thermal (Diffusion furnaces, RTA), and Metrology (Profilometers, Ellipsometers)."),
        # Contact
        ("who do i contact for equipment problems at iitbnf",
         "For equipment problems at IITBNF, contact the System Owner for that specific equipment."),
        ("who do i contact for login or access issues",
         "For login or access issues at IITBNF, contact the IT Admin through the portal."),
        ("who handles attendance queries at iitbnf",
         "Attendance queries at IITBNF are handled by the HR Team."),
    ]
    
    # Generate paraphrase variants for each QA pair
    QUESTION_VARIANTS = [
        lambda q: q,
        lambda q: q + "?",
        lambda q: "can you tell me " + q,
        lambda q: "please explain: " + q,
        lambda q: "i want to know " + q,
    ]
    
    for question_base, answer in FACILITY_QA:
        ctx = {"facility": "IITBNF", "topic": question_base}
        for variant_fn in QUESTION_VARIANTS:
            question = variant_fn(question_base)
            pairs.append(_format_pair(ctx, question, answer))
    
    return pairs
if __name__ == "__main__":
    pairs = generate()

    if not pairs:
        print("ERROR: No pairs generated. Check DB connection.", file=sys.stderr)
        sys.exit(1)

    # Write JSONL directly — avoids Windows stdout redirect issues
    output_path = "training_data.jsonl"
    written = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for p in pairs:
            if "text" in p:
                p["text"] = p["text"].replace("\r\n", "\n").replace("\r", "\n")
            line = json.dumps(p, ensure_ascii=False)
            line = line.replace("\r\n", "\n").replace("\r", "\n")
            f.write(line + "\n")
            written += 1

    print(f"Written {written} pairs to {output_path}", file=sys.stderr)

    # Sample file for inspection
    with open("training_sample.txt", "w", encoding="utf-8") as f:
        for i, p in enumerate(pairs[:60]):
            f.write(f"--- Example {i+1} ---\n")
            if "text" in p:
                f.write(p["text"])
            else:
                f.write(f"Q: {p.get('instruction', '')}\n")
                f.write(f"A: {p.get('response', '')}\n")
            f.write("\n" + "=" * 60 + "\n\n")

    print("First 60 pairs saved to training_sample.txt", file=sys.stderr)