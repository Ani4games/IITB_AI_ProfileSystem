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
        # For questions, use rag_chat (non-streaming) and yield the answer
        clean_message = normalize_question(user_message)
        result = rag_chat(clean_message, ctx)
        yield result.get("answer", "[No answer generated]")
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
        result = rag_chat(clean_message, ctx)
        return {
            "answer":     result.get("answer", ""),
            "mode":       intent["mode"],
            "label":      intent["label"],
            "confidence": intent["confidence"],
            "success":    result.get("success", False),
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
