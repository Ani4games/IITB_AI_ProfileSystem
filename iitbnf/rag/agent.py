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

    is_question = any(
        user_message.strip().lower().startswith(w)
        for w in ["what", "who", "when", "how", "why", "is", "does", "can", "tell me"]
    ) or user_message.strip().endswith("?")

    if is_question:
        clean_message = normalize_question(user_message)
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

        # ── Tier 2: fall through to model ─────────────────────────────────
        result = rag_chat(clean_message, ctx)
        answer =  result.get("answer", "[No answer generated]")
        words = answer.split(" ")
        for word in words:
            yield word + " "
    else:
        # For report-style requests, stream as before
        yield from rag_stream(ctx, mode=intent["mode"])

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
    is_question = any(
        user_message.strip().lower().startswith(w)
        for w in ["what", "who", "when", "how", "why", "is", "does", "can", "tell me"]
    ) or user_message.strip().endswith("?")

    if is_question:
        clean_message = normalize_question(user_message)
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

        # ── Tier 2: fall through to model ─────────────────────────────────
        result = rag_chat(clean_message, ctx)
        return {
            "answer":     result.get("answer", ""),
            "mode":       intent["mode"],
            "label":      intent["label"],
            "confidence": intent["confidence"],
            "success":    result.get("success", False),
            "tier":       2,
        }
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