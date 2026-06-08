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

from rag.pipeline import rag_stream, rag_generate, rag_chat
from rag.tier0 import lookup as tier0_lookup
from rag.query_router import route as structured_route
from rag.facility_router import route_facility
from rag.data_gatherer import gather
from rag.intent_router import classify_intent
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

def agent_stream(user_message: str, ctx: dict):
    """
    Autonomous streaming agent.
    Detects intent from user_message, then streams tokens from pipeline.

    Usage (in your route/chat handler):
        for token in agent_stream(user_message, ctx):
            emit(token)   # SSE / websocket / print

    Args:
        user_message : raw string from the chat interface
        ctx          : context dict (staff or lab profile)

    Yields:
        str tokens as they are generated
    """
    intent = detect_intent(user_message)
    yield f"[MODE: {intent['label']}]\n\n"

    _q_lower = user_message.strip().lower()
    is_question = (
        any(_q_lower.startswith(w) for w in [
            "what", "who", "when", "how", "why", "is", "does", "can", "tell me",
            "show", "give", "list", "compare", "summarize", "describe", "find",
            "check", "get", "display", "count", "which", "was", "were", "did",
            "has", "have"
        ])
        or user_message.strip().endswith("?")
        or "?" in user_message
        or len(user_message.strip().split()) <= 8  # short queries are almost always questions
    )

    if is_question:
        clean_message = normalize_question(user_message)
        # Two-layer intent classification
        _intent_label, _intent_method = classify_intent(clean_message)
        logger.info(
            "[Agent] intent=%s method=%s query=%r",
            _intent_label, _intent_method, clean_message[:60]
        )
        if _intent_label not in ("general_profile"):
            fast_answer = structured_route(clean_message, ctx)
            if fast_answer:
                yield f"[MODE: Direct Lookup]\n\n"
                yield fast_answer
                return
         # NEW: Year-comparison check (before Tier 0)
        year_answer = _answer_year_comparison(clean_message, ctx)
        if year_answer:
            yield year_answer  # for agent_stream
            return
        if _intent_label in ("admin_stats"):
            facility_answer = route_facility(clean_message)
            if facility_answer:
                yield "[MODE: Facility Knowledge]\n\n"
                yield facility_answer
                return
        # ── Tier 0: answer directly from ctx dict, no model call ──────────
        fast = tier0_lookup(clean_message, ctx)
        if fast:
            yield f"[MODE: Direct Lookup]\n\n"
            yield fast["answer"]
            return
        # ── Tier 1: answer from structured data ───────────────────────────
        structured_data = gather(clean_message, ctx)
        if structured_data:
            answer = _format_gathered(structured_data)
            yield f"[MODE: Structured Data]\n\n"
            yield answer
            return

        # ── Tier 2: fall through to model ─────────────────────────────────
        result = rag_chat(clean_message, ctx)
        answer =  result.get("answer", "[No answer generated]")
        words = answer.split(" ")
        for word in words:
            yield word + " "
    else:
        # For report-style requests, stream as before
        yield from rag_stream(ctx, mode=intent["mode"])
_qa_response_cache = {} # module-level — survives across calls
def agent_generate(user_message: str, ctx: dict) -> dict:
    """
    Autonomous non-streaming agent.
    Detects intent, generates full response, returns structured result.

    Usage:
        result = agent_generate(user_message, ctx)
        print(result["answer"])

    Args:
        user_message : raw string from the chat interface
        ctx          : context dict (staff or lab profile)

    Returns:
        {
            "answer":     str,
            "mode":       str,
            "label":      str,
            "confidence": str,
            "success":    bool
        }
    """
    intent = detect_intent(user_message)

    logger.info("Agent generating mode='%s' for: %s", intent["mode"], user_message[:60])

    # Use rag_chat for question-style input, rag_generate for report-style
    # Detect if it's a question or a generation request
    _q_lower = user_message.strip().lower()
    is_question = (
        any(_q_lower.startswith(w) for w in [
            "what", "who", "when", "how", "why", "is", "does", "can", "tell me",
            "show", "give", "list", "compare", "summarize", "describe", "find",
            "check", "get", "display", "count", "which", "was", "were", "did",
            "has", "have"
        ])
        or user_message.strip().endswith("?")
        or "?" in user_message
        or len(user_message.strip().split()) <= 8  # short queries are almost always questions
    )

    if is_question:
        clean_message = normalize_question(user_message)
        # Two-layer intent classification
        _intent_label, _intent_method = classify_intent(clean_message)
        logger.info(
            "[Agent] intent=%s method=%s query=%r",
            _intent_label, _intent_method, clean_message[:60]
        )
        if _intent_label not in ("general_profile"):
            fast_answer = structured_route(clean_message, ctx)
            if fast_answer:
                return {
                    "answer": fast_answer,
                    "mode": "structured",
                    "label": "Direct Lookup",
                    "confidence": "high",
                    "success": True,
                    "tier": 1,
                }
            # NEW: Year-comparison check (before Tier 0)
        year_answer = _answer_year_comparison(clean_message, ctx)
        if year_answer:
            return {
                "answer": year_answer,
                "mode": "year_comparison",
                "label": "Year Comparison",
                "confidence": "high",
                "success": True,
                "tier": 1.5,
            }
        if _intent_label in ("admin_stats"):
            facility_answer = route_facility(clean_message)
            if facility_answer:
                return {
                    "answer": facility_answer,
                    "mode": "facility_knowledge",
                    "label": "Facility Knowledge",
                    "confidence": "high",
                    "success": True,
                    "tier": 1.5,
                }
        # ── Tier 0: answer directly from ctx dict, no model call ──────────
        fast = tier0_lookup(clean_message, ctx)
        if fast:
            return {
                "answer":     fast["answer"],
                "mode":       "factual",
                "label":      "Direct Lookup",
                "confidence": "high",
                "success":    True,
                "tier":       0,
                "intent":     fast.get("intent"),
                "latency_ms": fast.get("latency_ms"),
            }

        # At module level:

        # In agent_generate(), before result = rag_chat(...):
        cache_key = f"{ctx.get('member_id')}:{clean_message}"
        if cache_key in _qa_response_cache:
            return _qa_response_cache[cache_key]

        # ── Tier 2: fall through to model ─────────────────────────────────
        result = rag_chat(clean_message, ctx)
        _qa_response_cache[cache_key] = {
                        "answer":     result.get("answer", ""),
            "mode":       intent["mode"],
            "label":      intent["label"],
            "confidence": intent["confidence"],
            "success":    result.get("success", False),
            "tier":     2,
        }
        return _qa_response_cache[cache_key]
    else:
        answer = rag_generate(ctx, audience=(
            "management" if intent["mode"] == "executive" else "individual"
        ))
        print(f"[AGENT] Mode selected: {intent['mode']}")
        print(f"[AGENT] Is question: {is_question}")
        return {
            "answer":     answer,
            "mode":       intent["mode"],
            "label":      intent["label"],
            "confidence": intent["confidence"],
            "success":    bool(answer),
        }
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
    return None

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