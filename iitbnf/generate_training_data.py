# generate_training_data.py — FIXED VERSION
"""
Run locally: python generate_training_data.py
Generates synthetic Q&A pairs from your live DB.
"""

import json
import sys
import random
from db import slots_query, hr_query

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
    for k, v in fields.items():
        if v is None or str(v) in ("", "N/A"):
            continue
        # Skip zero numeric values — 0 means "no data", use negative example instead
        try:
            if float(str(v)) == 0:
                continue
        except (ValueError, TypeError):
            pass
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

def generate():
    pairs = []
    years = [2023, 2024, 2025, 2026]

    # ── Step 1: Fetch members — mirror exactly what lab.py does ──────────────
    print("Fetching lab users...", file=sys.stderr)
    lab_members = slots_query("""
        SELECT memberid, fname, lname, position, department, email
        FROM login
        WHERE STR_TO_DATE(expiry_date, '%m/%d/%Y') >= CURDATE()
          AND (position IS NULL 
               OR position NOT IN ('IITBNF Staff', 'Faculty', 'Institute Facility'))
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
               OR p.leaving_date >= CURDATE())
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
                if rejected:
                    parts.append(f"{rejected} {'was' if rejected==1 else 'were'} rejected.")
                answer = " ".join(parts)
                slot_ctx = {
                    "name": name,
                    "year": year,
                    "eq_requests": total,
                    "eq_slot_booked": booked,
                    "eq_pending": pending,
                    "eq_rejected": rejected,
                    "tools_used": tools,
                }
                for tpl in SLOT_TEMPLATES_YEAR:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(slot_ctx, question, answer))
                    if random.random() < 0.3:
                        pairs.append(_make_negative(question, name))

        # Year-comparison slot
        available_years = sorted(year_slot_data.keys())
        if len(available_years) >= 2:
            for i in range(len(available_years)):
                for j in range(i+1, len(available_years)):
                    y1  = available_years[i]
                    y2  = available_years[j]
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
                        "trend": trend,
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
        paper_count = safe_int(papers[0]['total'] if papers and papers[0] else 0)
        if paper_count > 0:
            answer = (
                f"{name} has {paper_count} approved "
                f"{'publication' if paper_count==1 else 'publications'} "
                f"associated with IITBNF."
            )
            paper_ctx = {
                "name": name,
                "paper_count": paper_count
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
                "total_projects": total,
                "active_projects": active
            }
            for tpl in PROJECT_TEMPLATES:
                question = tpl.format(name=name)
                pairs.append(_format_pair(project_ctx, question, answer))
                if random.random() < 0.3:
                    pairs.append(_make_negative(question, name))

    # ── Step 4: Generate pairs for staff members ──────────────────────────────
    print("Generating staff pairs...", file=sys.stderr)
    for s in staff_members:
        mid   = s['member_id']
        desig = s.get('designation') or 'staff member'
        team  = s.get('team')        or 'Unknown Team'
        email = s.get('email')       or ''

        # Resolve display name from slotbooking
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

        # General identity
        staff_identity_ctx = {"name": name, "designation": desig, "team": team}
        staff_identity_answer = f"{name} is a {desig} in the {team} team at IITBNF."
        for tpl in GENERAL_TEMPLATES:
            question = tpl.format(name=name)
            answer = f"{name} is a {desig} in the {team} team at IITBNF."
            pairs.append(_format_pair(staff_identity_ctx, question, staff_identity_answer))
            if random.random() < 0.3:
                pairs.append(_make_negative(question, name))

        # Per-year attendance (staff use hr_portal attendance)
        year_attend_data = {}
        for year in years:
            att = hr_query("""
                SELECT COUNT(*) AS days_present
                FROM user_attendance
                WHERE memberid=%s AND YEAR(date)=%s
            """, (mid, year))
            days = safe_int(att[0]['days_present'] if att and att[0] else 0)
            if days > 0:
                year_attend_data[year] = days
                answer = (
                    f"In {year}, {name} was present for {days} working "
                    f"{'day' if days==1 else 'days'}."
                )
                att_ctx = {"name": name, "year": year, "days_present": days}
                for tpl in ATTEND_TEMPLATES_YEAR:
                    question = tpl.format(name=name, year=year)
                    pairs.append(_format_pair(att_ctx, question, answer))
                    if random.random() < 0.3:
                        pairs.append(_make_negative(question, name))
        # Year-comparison attendance
        available_att = sorted(year_attend_data.keys())
        if len(available_att) >= 2:
            for i in range(len(available_att)):
                for j in range(i+1, len(available_att)):
                    y1   = available_att[i]
                    y2   = available_att[j]
                    d1   = year_attend_data[y1]
                    d2   = year_attend_data[y2]
                    diff = d2 - d1
                    trend = (
                        f"{name} attended {diff} more days in {y2}"       if diff > 0
                        else f"{name} attended {abs(diff)} fewer days in {y2}" if diff < 0
                        else f"{name} had the same attendance in both years"
                    )
                    answer = (
                        f"Attendance comparison for {name}: "
                        f"{y1} — {d1} days. "
                        f"{y2} — {d2} days. "
                        f"{trend}."
                    )
                    attcompare_ctx = {
                        "name": name,
                        f"days_present_{y1}": d1,
                        f"days_present_{y2}": d2,
                        "trend": trend,
                    }
                    for tpl in COMPARE_ATTEND_TEMPLATES:
                        question = tpl.format(name=name, y1=y1, y2=y2)
                        pairs.append(_format_pair(attcompare_ctx, question, answer))
                        if random.random() < 0.25:
                            pairs.append(_make_negative(question, name))
        # Monthly reports (staff specific)
        mr = hr_query("""
            SELECT COUNT(*) AS submitted, AVG(star) AS avg_stars
            FROM monthly_report
            WHERE member_id=%s
        """, (mid,))
        if mr and mr[0] and safe_int(mr[0]['submitted']) > 0:
            submitted = safe_int(mr[0]['submitted'])
            avg_stars = round(float(mr[0]['avg_stars'] or 0), 1)
            mr_ctx = {"name": name, "monthly_reports_submitted": submitted, "monthly_report_avg_stars": avg_stars}
            question = f"How many monthly reports has {name} submitted?"
            mr_answer = (
                f"{name} has submitted {submitted} monthly "
                f"{'report' if submitted==1 else 'reports'} "
                f"with an average rating of {avg_stars} stars."
                )
            pairs.append(_format_pair(mr_ctx, question, mr_answer))
            if random.random() < 0.3:
                pairs.append(_make_negative(question, name))    

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

    print(f"Written {written} pairs to {output_path}", file=sys.stderr)

    # Sample file for inspection
    with open("training_sample.txt", "w", encoding="utf-8") as f:
        for i, p in enumerate(pairs[:20]):
            f.write(f"--- Example {i+1} ---\n")
            if "text" in p:
                f.write(p["text"])
            else:
                f.write(f"Q: {p.get('instruction', '')}\n")
                f.write(f"A: {p.get('response', '')}\n")
            f.write("\n" + "=" * 60 + "\n\n")

    print("First 20 pairs saved to training_sample.txt", file=sys.stderr)