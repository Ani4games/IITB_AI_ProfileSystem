"""
rag/agent.py — Autonomous intent detection layer
=================================================
Sits between the chat interface and pipeline.py.
Reads the user's natural language request and decides:
  - Which mode to use     → "short" or "executive"
  - How many tokens       → mapped from mode
  - Whether to stream     → based on caller preference

Public API:
    detect_intent(user_message)          → dict  (intent metadata)
    agent_stream(user_message, ctx)      → generator (streams tokens)
    agent_generate(user_message, ctx)    → str   (full response)

No changes needed to ingest.py, retrieve.py, or pipeline.py.
Just import and use this in your routes or chat handler.
"""

import re
import logging

from rag.pipeline import rag_generate, rag_chat
from rag.tier0 import lookup as tier0_lookup
from rag.query_router import route as structured_route
from rag.facility_router import route_facility
from rag.data_gatherer import gather
from rag.intent_router import classify_intent

from llm import is_llm_available

SLM_UNAVAILABLE_MSG = (
    "⚠ SLM not available at the moment — switching to direct lookup mode.\n\n"
)
def _answer_without_slm(question: str, ctx: dict) -> str:
    """
    Best-effort answer using only the 4 tiers (no SLM).
    Returns a string answer or a 'not found' message.
    """
    clean = normalize_question(question)
    
    # Tier 1a: structured route (DB queries, year-specific)
    ans = structured_route(clean, ctx)
    if ans:
        return ans
    
    # Tier 1b: year comparison
    ans = _answer_year_comparison(clean, ctx)
    if ans:
        return ans
    
    # Tier 1c: facility knowledge
    ans = route_facility(clean)
    if ans:
        return ans
    
    # Tier 0: ctx dict lookup
    fast = tier0_lookup(clean, ctx)
    if fast:
        return fast["answer"]
    
    # Tier 1d: structured data gather (template-based formatting without SLM)
    gathered = gather(clean, ctx)
    if gathered:
        return _format_gathered(gathered)
    
    return (
        "I couldn't find a specific answer to that question in the available data. "
    )
logger = logging.getLogger(__name__)

# ── Intent config ─────────────────────────────────────────────────────────────

# Keywords that strongly suggest "executive" mode
EXECUTIVE_KEYWORDS = [
    "executive", "formal", "management", "senior", "board",
    "briefing", "report", "official", "detailed", "comprehensive",
    "full", "complete", "in depth", "in-depth", "thorough",
    "professional", "hr report", "performance review"
]

# Keywords that strongly suggest "short" mode
SHORT_KEYWORDS = [
    "short", "quick", "brief", "summary", "summarize", "overview",
    "snapshot", "tldr", "tl;dr", "highlight", "digest",
    "just", "only", "fast", "simple", "key points", "gist"
]

# Token limits per mode (mirrors pipeline.py)
MODE_TOKENS = {
    "short":     150,
    "executive": 500,
}

# Human-readable labels (for logging / UI feedback)
MODE_LABELS = {
    "short":     "Quick Summary",
    "executive": "Executive Briefing",
}
def normalize_question(question: str) -> str:
    question = question.replace("’", "'").replace("`", "'")
    question = question.replace("'s's", "'s")
    question = re.sub(r"#\s*(\d+)", r"#\1", question)
    question = re.sub(r"\s+", " ", question).strip()
    return question


# ── Intent detection ──────────────────────────────────────────────────────────

def detect_intent(user_message: str) -> dict:
    """
    Analyse the user's message and decide which generation mode to use.

    Returns:
        {
            "mode":        "short" | "executive",
            "label":       "Quick Summary" | "Executive Briefing",
            "max_tokens":  int,
            "confidence":  "high" | "low",    # low = fell back to default
            "raw_message": str
        }
    """
    text = user_message.lower().strip()

    # Score each mode by keyword hits
    exec_hits  = sum(1 for kw in EXECUTIVE_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", text))
    short_hits = sum(1 for kw in SHORT_KEYWORDS     if re.search(rf"\b{re.escape(kw)}\b", text))

    if exec_hits > short_hits:
        mode       = "executive"
        confidence = "high"
    elif short_hits > exec_hits:
        mode       = "short"
        confidence = "high"
    elif exec_hits == short_hits and exec_hits > 0:
        # Tie — lean executive (more informative default)
        mode       = "executive"
        confidence = "low"
    else:
        # No keywords matched — default to short
        mode       = "short"
        confidence = "low"

    logger.info(
        "Intent detected: mode=%s confidence=%s (exec_hits=%d short_hits=%d)",
        mode, confidence, exec_hits, short_hits
    )

    return {
        "mode":        mode,
        "label":       MODE_LABELS[mode],
        "max_tokens":  MODE_TOKENS[mode],
        "confidence":  confidence,
        "raw_message": user_message,
    }
def _format_gathered(gathered: dict) -> str:
    """
    Pure Python fallback — formats pre-fetched data into
    a readable sentence without any model call.
    Used when the SLM fails to follow the formatting template.
    """
    t    = gathered["type"]
    d    = gathered["data"]
    name = d.get("name", "This person")

    if t == "attendance_compare":
        y1, y2 = d["years"][0], d["years"][1]
        diff   = y2["days_present"] - y1["days_present"]
        change = (f"{abs(diff)} more days" if diff > 0
                  else f"{abs(diff)} fewer days" if diff < 0
                  else "the same number of days")
        return (
            f"{name} attended {y1['days_present']} days in {y1['year']} "
            f"and {y2['days_present']} days in {y2['year']} "
            f"— {change} in {y2['year']}."
        )

    if t == "attendance_year":
        return (
            f"{name} was present for {d['days_present']} "
            f"working days in {d['year']}."
        )

    if t == "slot_compare":
        y1, y2 = d["years"][0], d["years"][1]
        diff   = y2["total"] - y1["total"]
        change = (f"{abs(diff)} more" if diff > 0
                  else f"{abs(diff)} fewer" if diff < 0
                  else "the same number of")
        return (
            f"{name} submitted {y1['total']} equipment requests in "
            f"{y1['year']} and {y2['total']} in {y2['year']} "
            f"— {change} requests in {y2['year']}."
        )

    if t == "ownership":
        count = d["count"]
        if count == 0:
            return f"{name} is not currently assigned as system owner for any tools."
        tools = ", ".join(d["tools"])
        return (
            f"{name} is currently assigned as system owner "
            f"for {count} tool{'s' if count != 1 else ''}: {tools}."
        )

    if t == "leave":
        total = d["total"]
        yr    = d["year"]
        bd    = d["breakdown"]
        parts = ", ".join(f"{k}: {v}" for k, v in bd.items())
        return (
            f"{name} took {total} leave day{'s' if total != 1 else ''} "
            f"in {yr}" + (f" ({parts})." if parts else ".")
        )

    return "Data retrieved but could not be formatted."
# ── Public agent API ──────────────────────────────────────────────────────────
# ADD this helper function near the bottom of agent.py (before agent_stream):

def _handle_monthly_attendance(clean_message: str, ctx: dict, slm_ok: bool) -> dict | None:
    """
    Handle single or multi-month attendance queries.
    Returns a result dict if handled, None to fall through.
    """
    MONTH_MAP_ORDERED = [
        ('january', 1), ('february', 2), ('march', 3), ('april', 4),
        ('may', 5), ('june', 6), ('july', 7), ('august', 8),
        ('september', 9), ('october', 10), ('november', 11), ('december', 12),
        ('jan', 1), ('feb', 2), ('mar', 3), ('apr', 4),
        ('jun', 6), ('jul', 7), ('aug', 8), ('sep', 9), ('sept', 9),
        ('oct', 10), ('nov', 11), ('dec', 12),
    ]
    MONTH_DISPLAY = {
        1:'January', 2:'February', 3:'March', 4:'April',
        5:'May', 6:'June', 7:'July', 8:'August',
        9:'September', 10:'October', 11:'November', 12:'December'
    }

    q_lower = clean_message.lower()
    year_match = re.search(r'\b(20\d{2})\b', clean_message)
    year_val = int(year_match.group(1)) if year_match else None

    # Collect ALL distinct months mentioned, preserving order of appearance
    found_months = []
    for name, num in MONTH_MAP_ORDERED:
        if re.search(r'\b' + re.escape(name) + r'\b', q_lower):
            if num not in found_months:
                found_months.append(num)

    if not found_months or not year_val:
        return None

    mid = ctx.get("member_id")
    name = ctx.get("name", "This person")
    if not mid:
        return None

    from db import hr_query as _hrq

    if len(found_months) == 1:
        month_num = found_months[0]
        rows = _hrq(
            "SELECT COUNT(*) AS days FROM user_attendance "
            "WHERE memberid=%s AND MONTH(date)=%s AND YEAR(date)=%s",
            (mid, month_num, year_val)
        )
        days = int(rows[0]['days'] if rows and rows[0] else 0)
        mname = MONTH_DISPLAY.get(month_num, str(month_num))
        answer = (f"{name} was present for {days} working "
                  f"{'day' if days == 1 else 'days'} in {mname} {year_val}.")
        return {"answer": answer, "mode": "attendance_monthly",
                "label": "Monthly Attendance", "confidence": "high",
                "success": True, "tier": 1, "slm_available": slm_ok}
    else:
        # Multi-month: query each month
        month_data = []
        for month_num in found_months:
            rows = _hrq(
                "SELECT COUNT(*) AS days FROM user_attendance "
                "WHERE memberid=%s AND MONTH(date)=%s AND YEAR(date)=%s",
                (mid, month_num, year_val)
            )
            days = int(rows[0]['days'] if rows and rows[0] else 0)
            month_data.append((MONTH_DISPLAY.get(month_num, str(month_num)), days))

        lines = [f"{mname}: {days} {'day' if days == 1 else 'days'} present"
                 for mname, days in month_data]

        if len(month_data) == 2:
            diff = month_data[1][1] - month_data[0][1]
            if diff > 0:
                trend = (f"{name} was present {diff} more "
                         f"{'day' if diff == 1 else 'days'} in "
                         f"{month_data[1][0]} than {month_data[0][0]}.")
            elif diff < 0:
                trend = (f"{name} was present {abs(diff)} fewer "
                         f"{'day' if abs(diff) == 1 else 'days'} in "
                         f"{month_data[1][0]} than {month_data[0][0]}.")
            else:
                trend = f"{name} had the same attendance in both months."
            answer = (f"Monthly attendance comparison for {name} in {year_val}:\n"
                      f"{chr(10).join(lines)}\n{trend}")
        else:
            answer = (f"Monthly attendance for {name} in {year_val}:\n"
                      f"{chr(10).join(lines)}")

        return {"answer": answer, "mode": "attendance_monthly_comparison",
                "label": "Monthly Attendance Comparison", "confidence": "high",
                "success": True, "tier": 1, "slm_available": slm_ok}
# REPLACE agent_stream() in agent.py:
def agent_stream(user_message: str, ctx: dict):
    """
    Streaming wrapper around agent_generate.
    Yields string tokens suitable for SSE consumption.
    Called by ai_routes.py ai_stream() and admin_chat() endpoints.
    """
    result = agent_generate(user_message, ctx)
    answer = result.get("answer", "")
    mode   = result.get("mode", "unknown")
    
    # Emit mode tag so the frontend can show which tier answered
    yield f"[MODE: {mode}]"
    
    # Stream word by word to give typewriter effect
    # For structured/factual answers this is near-instant
    if answer:
        words = answer.split(" ")
        for word in words:
            yield word + " "
    else:
        yield "(No response generated.)"
_qa_response_cache = {} # module-level — survives across calls

# REPLACE agent_generate() in agent.py:

def agent_generate(user_message: str, ctx: dict, history: list = None) -> dict:
    intent = detect_intent(user_message)
    slm_ok = is_llm_available()

    logger.info("Agent generating mode='%s' slm_available=%s for: %s",
                intent["mode"], slm_ok, user_message[:60])
        # For non-question report-style requests with no SLM, use composer

    # Replace the current is_question detection in agent_generate() with:
    QUESTION_STARTERS = (
        "what", "who", "when", "how", "why", "is", "does", "can",
        "tell me", "give me", "show me", "list", "find", "get",
        "explain", "describe", "summarize", "what's", "who's",
        "share", "do", "are", "any", "which", "compare", "difference",
    )
    clean_message = normalize_question(user_message)
    # Short ambiguous queries that are about facility policy, not profile data
    _FACILITY_TRIGGERS = ['working day', 'working hour', 'open hour', 'timing', 
                        'operating hour', 'iitbnf hour', 'iitbnf time', 'iitbnf day']
    if any(t in clean_message.lower() for t in _FACILITY_TRIGGERS):
        fac = route_facility(clean_message)
        if fac:
            return {"answer": fac, "mode": "facility_knowledge",
                    "label": "Facility Knowledge", "confidence": "high",
                    "success": True, "tier": 1.5, "slm_available": slm_ok}
        person_name = ctx.get("name", "")
        if person_name and person_name.lower() not in clean_message.lower():
            # Inject name for pronouns and short follow-up questions
            if clean_message.strip().lower().startswith(("what about", "and in", "how about")):
                clean_message = clean_message + f" for {person_name}"
    # Detect compound questions with "and" joining two intents
    # In agent_generate(), BEFORE the compound question check, add:
    # Don't split if the question contains two years (it's a comparison, not compound)
    year_count = len(re.findall(r'\b20\d{2}\b', clean_message))
    has_compound_marker = bool(re.search(r'\band\b|\balso\b', clean_message, re.I))
    if has_compound_marker and year_count < 2:
        parts = re.split(r'\band\b|\balso\b', clean_message, flags=re.I)
        if len(parts) < 2:
            pass
        else:
            if len(parts) == 2:
                answers = []
                person_name = ctx.get("name", "")
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    if person_name and person_name.lower() not in part.lower():
                        part += f"{person_name}'s {part.lstrip('the').lstrip('their')}"
                    # Try all tiers for each part
                    a = structured_route(part, ctx)
                    if not a:
                        fast = tier0_lookup(part, ctx)
                        if fast:
                            a = fast["answer"]
                    if not a and len(part.split()) > 2:  # only call facility for substantive parts
                        a = route_facility(part)
                    if not a:
                        a = route_facility(part)    
                    if not a:
                    # Return explicit "not found" so both parts always surface
                        a = f"No data found for: '{part.strip()}'" 
                    if a:
                        answers.append(a)
                if len(answers) >= 1:
                    return {"answer": answers[0] + "\n\n" + answers[1],
                            "mode": "compound", "label": "Compound Question",
                            "confidence": "medium", "success": True,
                            "tier": 1, "slm_available": slm_ok}
                elif len(answers) == 1:
                    # Only one part answered — return it but don't claim compound success
                    pass  # fall through to normal processing
    is_question = (
        any(clean_message.strip().lower().startswith(w) for w in QUESTION_STARTERS)
        or clean_message.strip().endswith("?")
        or bool(re.search(r'\b(attendance|equipment|request|slot|activity|usage|project|publication|permission|training|cancel|logbook|reservation|leave|report)\b', clean_message, re.I))
    )
    if not is_question and not slm_ok:
        try:
            from rag.composer import compose_staff_summary, compose_lab_summary
            is_lab = ctx.get("category") is not None
            summary = compose_lab_summary(ctx) if is_lab else compose_staff_summary(ctx)
            return {
                "answer": SLM_UNAVAILABLE_MSG + summary,
                "mode": "composer_fallback",
                "label": "Direct Summary",
                "confidence": "high",
                "success": bool(summary),
                "slm_available": False,
            }
        except Exception as e:
            logger.warning("Composer fallback failed: %s", e)
    if is_question:        
        intent_label, intent_method = classify_intent(clean_message)
        logger.info("[Agent] intent=%s method=%s query=%r", intent_label, intent_method, clean_message[:60])

        # ── FACILITY INFO: early exit regardless of regex/minilm method ──────────
        # Must be checked before the MiniLM wiring block because facility_info
        # can be detected by regex, and the normal tier waterfall (structured_route,
        # tier0_lookup) returns None for policy questions, causing silent fallthrough.
        if intent_label == "facility_info":
            fac = route_facility(clean_message)
            if fac:
                return {"answer": fac, "mode": "facility_knowledge",
                        "label": "Facility Knowledge", "confidence": "high",
                        "success": True, "tier": 1.5, "slm_available": slm_ok}

        # ── ATTENDANCE_MONTHLY: early exit handles single and multi-month ────────
        if intent_label == "attendance_monthly":
            result = _handle_monthly_attendance(clean_message, ctx, slm_ok)
            if result:
                return result

        # ── MiniLM WIRING ────────────────────────────────────────────────────────
        # MiniLM only fires when regex failed — meaning the question used unusual
        # phrasing that keyword matching missed. We inject a canonical keyword into
        # a synthetic query so the existing tier handlers fire correctly.
        # This avoids duplicating any handler logic.
        # NOTE: we only act on MiniLM results, not regex results — if method is
        # "regex" the label is already implicit in the question text and the
        # normal tier waterfall below handles it fine.
        # ADD this block right after: intent_label, intent_method = classify_intent(clean_message)
        # and before: if intent_method == "minilm":


        if intent_method == "minilm":
            _aug = clean_message   # default: pass question unchanged

            if intent_label == "attendance":
                # MiniLM caught phrasing like "how punctual is X" — inject "attendance"
                _aug = clean_message + " attendance"
            if intent_label == "attendance_monthly":
                result = _handle_monthly_attendance(clean_message, ctx, slm_ok)
                if result:
                    return result
            elif intent_label == "compare_attend":
                _aug = clean_message + " compare attendance"
            # ADD before the elif intent_label == "equipment_year" block
        elif intent_label == "equipment_year":
            # Check if question also contains a month name → monthly breakdown
            MONTH_MAP = {
                'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
                'mar': 3, 'march': 3, 'apr': 4, 'april': 4,
                'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
                'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
                'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
                'dec': 12, 'december': 12,
            }
            q_lower = clean_message.lower()
            month_num = next((v for k, v in MONTH_MAP.items() if k in q_lower), None)
            year_match = re.search(r'\b(20\d{2})\b', clean_message)
            year_val = int(year_match.group(1)) if year_match else None
            
            if month_num and year_val:
                uid = ctx.get("slot_uid")
                name = ctx.get("name", "This person")
                if uid:
                    from db import slots_query as _sq
                    rows = _sq("""
                        SELECT COUNT(*) AS total,
                            SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS booked,
                            SUM(CASE WHEN status=0 THEN 1 ELSE 0 END) AS pending,
                            SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) AS rejected,
                            COUNT(DISTINCT equipmentid) AS tools
                        FROM equipment_usage_approval
                        WHERE requestedby=%s 
                        AND MONTH(date_of_request)=%s 
                        AND YEAR(date_of_request)=%s
                    """, (uid, month_num, year_val))
                    month_names = {1:'January',2:'February',3:'March',4:'April',
                                5:'May',6:'June',7:'July',8:'August',
                                9:'September',10:'October',11:'November',12:'December'}
                    mname = month_names.get(month_num, str(month_num))
                    r = rows[0] if rows and rows[0] else {}
                    total = int(r.get('total') or 0)
                    if total == 0:
                        answer = f"{name} has no equipment request data for {mname} {year_val}."
                    else:
                        answer = (f"In {mname} {year_val}, {name} submitted {total} equipment "
                                f"{'request' if total==1 else 'requests'} across "
                                f"{int(r.get('tools') or 0)} tools. "
                                f"Breakdown: {int(r.get('booked') or 0)} slot-booked, "
                                f"{int(r.get('pending') or 0)} pending, "
                                f"{int(r.get('rejected') or 0)} rejected.")
                    return {"answer": answer, "mode": "slot_monthly",
                            "label": "Monthly Slot Activity", "confidence": "high",
                            "success": True, "tier": 1, "slm_available": slm_ok}
            elif intent_label in ("equipment_year", "equipment_count"):
                _aug = clean_message + " equipment requests"

            elif intent_label == "equipment_list":
                if any(kw in clean_message.lower() for kw in ['logbook', 'log book', 'session log', 'entries']):
                    uid = ctx.get("slot_uid")
                    if not uid:
                        from models.staff import _get_uid_from_member
                        uid = _get_uid_from_member(ctx.get("member_id"))
                    if uid:
                        from models.staff import get_staff_logbook_stats
                        # Extract N from "top 3", "top 5" etc.
                        top_n_match = re.search(r'\btop\s+(\d+)\b', clean_message, re.I)
                        top_n = int(top_n_match.group(1)) if top_n_match else 5
                        stats = get_staff_logbook_stats(uid)
                        breakdown = stats.get("breakdown", [])[:top_n]
                        name = ctx.get("name", "This person")
                        if not breakdown:
                            answer = f"{name} has no logbook entries on record."
                        else:
                            lines = [f"{b['tool_name']}: {b['entries']} entries" for b in breakdown]
                            answer = (f"{name}'s top {len(breakdown)} most used equipment by logbook entries: "
                                    + "; ".join(lines) + ".")
                        return {"answer": answer, "mode": "logbook_top_n",
                                "label": "Logbook Top N", "confidence": "high",
                                "success": True, "tier": 1, "slm_available": slm_ok}
                _aug = clean_message + " which tools"

            elif intent_label == "publication":
                _aug = clean_message + " publications papers"

            elif intent_label == "project":
                _aug = clean_message + " projects"

            elif intent_label == "training":
                _aug = clean_message + " training sessions"

            elif intent_label == "cancellation":
                _aug = clean_message + " cancellations"

            elif intent_label == "permission":
                _aug = clean_message + " tool permissions"

            elif intent_label == "system_owner":
                _aug = clean_message + " system owner"

            elif intent_label == "monthly_report":
                _aug = clean_message + " monthly report"

            elif intent_label == "leave":
                _aug = clean_message + " leave days"

            elif intent_label == "admin_stats":
                # Route immediately to facility router — don't wait for tier waterfall
                fac = route_facility(clean_message)
                if fac:
                    return {"answer": fac, "mode": "facility_knowledge",
                            "label": "Facility Knowledge", "confidence": "high",
                            "success": True, "tier": 1.5, "slm_available": slm_ok}

            elif intent_label == "general_profile":
                _aug = clean_message + " designation role"
            elif intent_label == "attendance_year":
                _aug = clean_message + " attendance days present"
            elif intent_label == "attendance_monthly":
                # Extract month name and year from the question
                
                MONTH_MAP = {
                    'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
                    'mar': 3, 'march': 3, 'apr': 4, 'april': 4,
                    'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
                    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
                    'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
                    'dec': 12, 'december': 12,
                }
                q_lower = clean_message.lower()
                month_num = None
                for mname, mnum in MONTH_MAP.items():
                    if mname in q_lower:
                        month_num = mnum
                        break
                year_match = re.search(r'\b(20\d{2})\b', clean_message)
                year_val = int(year_match.group(1)) if year_match else None
                
                if month_num and year_val:
                    mid = ctx.get("member_id")
                    name = ctx.get("name", "This person")
                    if mid:
                        from db import hr_query
                        rows = hr_query(
                            "SELECT COUNT(*) AS days FROM user_attendance "
                            "WHERE memberid=%s AND MONTH(date)=%s AND YEAR(date)=%s",
                            (mid, month_num, year_val)
                        )
                        days = int(rows[0]['days'] if rows and rows[0] else 0)
                        month_names = {1:'January',2:'February',3:'March',4:'April',
                                    5:'May',6:'June',7:'July',8:'August',
                                    9:'September',10:'October',11:'November',12:'December'}
                        mname_display = month_names.get(month_num, str(month_num))
                        answer = (f"{name} was present for {days} working "
                                f"{'day' if days==1 else 'days'} in {mname_display} {year_val}.")
                        return {"answer": answer, "mode": "attendance_monthly",
                                "label": "Monthly Attendance", "confidence": "high",
                                "success": True, "tier": 1, "slm_available": slm_ok}
            elif intent_label == "logbook":
                # Logbook needs a direct DB call — tier0 and structured_route
                # don't have keyword coverage for unusual logbook phrasing
                from models.staff import get_staff_logbook_stats, _get_uid_from_member
                uid = ctx.get("slot_uid") or _get_uid_from_member(ctx.get("member_id"))
                if uid:
                    stats = get_staff_logbook_stats(uid)
                    if stats and stats.get("total_entries", 0) > 0:
                        answer = (
                            f"{ctx.get('name', 'This person')} has {stats['total_entries']} "
                            f"logbook {'entry' if stats['total_entries'] == 1 else 'entries'} "
                            f"across {stats['tools_with_logs']} "
                            f"{'tool' if stats['tools_with_logs'] == 1 else 'tools'}."
                        )
                        return {"answer": answer, "mode": "logbook_direct",
                                "label": "Logbook Stats", "confidence": "high",
                                "success": True, "tier": 1, "slm_available": slm_ok}
                    elif stats:
                        return {"answer": f"{ctx.get('name', 'This person')} has no logbook entries on record.",
                                "mode": "logbook_direct", "label": "Logbook Stats",
                                "confidence": "high", "success": True,
                                "tier": 1, "slm_available": slm_ok}
            elif intent_label == "session_report":
                _aug = clean_message + " session reports filed"

            elif intent_label == "reservation":
                _aug = clean_message + " slot reservations total"
            elif intent_label == "facility_info":
                fac = route_facility(clean_message)
                if fac:
                    return {"answer": fac, "mode": "facility_knowledge",
                            "label": "Facility Knowledge", "confidence": "high",
                            "success": True, "tier": 1.5, "slm_available": slm_ok}
            # For all non-logbook, non-admin_stats labels: try augmented question
            # through the same tier waterfall. If augmentation helped, it fires here.
            # If it still misses (very unusual query), falls through to SLM as before.
            if _aug != clean_message:
                aug_answer = structured_route(_aug, ctx)
                if aug_answer:
                    return {"answer": aug_answer, "mode": "structured_minilm",
                            "label": f"Direct Lookup (MiniLM:{intent_label})",
                            "confidence": "high", "success": True,
                            "tier": 1, "slm_available": slm_ok}
                aug_fast = tier0_lookup(_aug, ctx)
                if aug_fast:
                    return {"answer": aug_fast["answer"], "mode": "factual_minilm",
                            "label": f"Direct Lookup (MiniLM:{intent_label})",
                            "confidence": "high", "success": True,
                            "tier": 0, "slm_available": slm_ok}
                # Augmentation didn't help — fall through to normal waterfall below
                # with the original clean_message (not the augmented one)
        if ctx.get("facility") and not ctx.get("slot_uid"):
            fac = route_facility(clean_message)
            if fac:
                return {"answer": fac, "mode": "facility_knowledge", 
                        "label": "Facility Knowledge", "confidence": "high",
                        "success": True, "tier": 1.5, "slm_available": slm_ok}
        # Always try 4-tier first (these don't need SLM)
        fast_answer = structured_route(clean_message, ctx)
        if fast_answer:
            return {"answer": fast_answer, "mode": "structured", "label": "Direct Lookup",
                    "confidence": "high", "success": True, "tier": 1, "slm_available": slm_ok}
        fast = tier0_lookup(clean_message, ctx)
        if fast:
            return {"answer": fast["answer"], "mode": "factual", "label": "Direct Lookup",
                    "confidence": "high", "success": True, "tier": 0,
                    "intent": fast.get("intent"), "latency_ms": fast.get("latency_ms"),
                    "slm_available": slm_ok}
        year_answer = _answer_year_comparison(clean_message, ctx)
        if year_answer:
            return {"answer": year_answer, "mode": "year_comparison", "label": "Year Comparison",
                    "confidence": "high", "success": True, "tier": 1.5, "slm_available": slm_ok}

        facility_answer = route_facility(clean_message)
        if facility_answer:
            return {"answer": facility_answer, "mode": "facility_knowledge",
                    "label": "Facility Knowledge", "confidence": "high",
                    "success": True, "tier": 1.5, "slm_available": slm_ok}

        # Tier 1.8: template-based formatting (no SLM)
        gathered = gather(clean_message, ctx)
        if gathered:
            answer = _format_gathered(gathered)
            return {"answer": answer, "mode": "data_formatted", "label": "Data + Format",
                    "confidence": "high", "success": True, "tier": 1.8, "slm_available": slm_ok}

        # Tier 2: SLM — only if available
        if not slm_ok:
            answer = _answer_without_slm(clean_message, ctx)
            return {
                "answer": SLM_UNAVAILABLE_MSG + answer,
                "mode": "no_slm_fallback",
                "label": "Direct Lookup",
                "confidence": "low",
                "success": True,
                "slm_available": False,
            }

        result = rag_chat(clean_message, ctx)
        return {"answer": result.get("answer", ""), "mode": intent["mode"],
                "label": intent["label"], "confidence": intent["confidence"],
                "success": result.get("success", False), "tier": 2, "slm_available": True}
    else:
        if not slm_ok:
            try:
                from rag.composer import compose_staff_summary, compose_lab_summary
                is_lab = ctx.get("category") is not None
                summary = compose_lab_summary(ctx) if is_lab else compose_staff_summary(ctx)
                return {
                    "answer": SLM_UNAVAILABLE_MSG + summary,
                    "mode": "composer_fallback", "label": "Direct Summary",
                    "confidence": "high", "success": bool(summary), "slm_available": False,
                }
            except Exception:
                pass

        answer = rag_generate(ctx, audience=(
            "management" if intent["mode"] == "executive" else "individual"
        ))
        return {"answer": answer, "mode": intent["mode"], "label": intent["label"],
                "confidence": intent["confidence"], "success": bool(answer), "slm_available": slm_ok}
# Add this function:
def _extract_years(question: str) -> list[int]:
    return [int(y) for y in re.findall(r'\b(20\d{2})\b', question)]

def _answer_year_comparison(question: str, ctx: dict) -> str | None:
    years = _extract_years(question)
    if len(years) < 2:
        return None
    q_lower = question.lower()
    # Detect what they're comparing
    if any(kw in q_lower for kw in ['slot', 'equipment', 'booking', 'request', 'reservation']):
        return _fetch_slot_comparison(ctx, years)
    if any(kw in q_lower for kw in ['attend', 'present', 'days', 'regular']):
        return _fetch_attendance_comparison(ctx, years)
    return None
# ADD this function to agent.py:
def _fetch_attendance_comparison(ctx: dict, years: list[int]) -> str:
    from db import hr_query
    mid = ctx.get("member_id")
    if not mid:
        return None  # Can't fetch attendance without member ID
    results = []
    for year in sorted(years):
        rows = hr_query(
            "SELECT COUNT(*) AS days FROM user_attendance WHERE memberid=%s AND YEAR(date)=%s",
            (mid, year)
        )
        days = int(rows[0]['days'] if rows and rows[0] else 0)
        results.append(f"{year}: {days} days present.")
    return " | ".join(results)
def _fetch_slot_comparison(ctx: dict, years: list[int]) -> str:
    from db import slots_query
    memberid = ctx.get("slot_uid") or ctx.get("memberid")
    # You'll need to resolve the memberid — see Fix 3
    
    results = []
    for year in sorted(years):
        rows = slots_query("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status=3 THEN 1 ELSE 0 END) AS slot_booked,
                SUM(CASE WHEN status=0 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) AS rejected
            FROM equipment_usage_approval
            WHERE requestedby=%s AND YEAR(date_of_request)=%s
        """, (memberid, year))
        
        if rows and rows[0]:
            r = rows[0]
            results.append(
                f"{year}: {r['total'] or 0} requests, "
                f"{r['slot_booked'] or 0} slot-booked, "
                f"{r['pending'] or 0} pending, "
                f"{r['rejected'] or 0} rejected."
            )
        else:
            results.append(f"{year}: No equipment request data found.")
    
    return " | ".join(results)