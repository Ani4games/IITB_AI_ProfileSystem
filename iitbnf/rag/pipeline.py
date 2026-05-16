# rag/pipeline.py — Ollama-backed generation

import json
import logging
import requests
from config import OLLAMA_URL, OLLAMA_MODEL, AI_MODE

logger = logging.getLogger(__name__)

# ── Ollama calls — TWO separate functions, not one ────────────────────────────
# A Python function with ANY yield statement is always a generator.
# Mixing stream=True/False in one function means the non-streaming
# path also returns a generator object instead of a string.

def _call_ollama_sync(prompt: str, max_tokens: int = 500) -> str:
    """
    Non-streaming Ollama call. Returns the full response as a string.
    Used by: rag_generate(), rag_chat()
    """
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {
            "num_predict": max_tokens,
            "temperature": 0.4,
            "top_p":       0.9,
        },
    }
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            stream=False,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        logger.error("Ollama not reachable at %s — is it running?", OLLAMA_URL)
        return ""
    except Exception as e:
        logger.error("Ollama sync request failed: %s", e)
        return ""


def _call_ollama_stream(prompt: str, max_tokens: int = 500):
    """
    Streaming Ollama call. Yields string tokens one at a time.
    Used by: rag_stream(), rag_stream_executive(), digest_session_reports_stream()
    """
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  True,
        "options": {
            "num_predict": max_tokens,
            "temperature": 0.4,
            "top_p":       0.9,
        },
    }
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        logger.error("Ollama not reachable at %s — is it running?", OLLAMA_URL)
        yield "[ERROR] Ollama is not running. Run: ollama serve"
        return
    except Exception as e:
        logger.error("Ollama stream request failed: %s", e)
        yield f"[ERROR] {e}"
        return

    for line in resp.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line)
            token = chunk.get("response", "")
            if token:
                yield token
            if chunk.get("done"):
                break
        except json.JSONDecodeError:
            continue

def rag_stream(ctx: dict, mode: str = "short"):
    """Streaming token generator — used by /api/ai/stream SSE endpoint."""
    from rag.composer import compose_staff_summary, compose_lab_summary
    from rag.retrieve import retrieve
    
    # Short mode: composer only, no LLM
    if mode == "short":
        is_lab  = ctx.get("category") is not None  # lab ctx has 'category'
        summary = compose_lab_summary(ctx) if is_lab else compose_staff_summary(ctx)
        yield f"[MODE: Quick Summary]\n\n"
        yield summary
        return

    # Executive mode: build prompt, call Ollama
    yield f"[MODE: Executive Briefing]\n\n"
    prompt = _build_executive_prompt(ctx)
    yield from _call_ollama_stream(prompt, max_tokens=500)


def rag_generate(ctx: dict, audience: str = "management") -> str:
    """Non-streaming — used by /api/ai/report."""
    mode   = "executive" if audience == "management" else "short"
    prompt = _build_executive_prompt(ctx)
    return _call_ollama_sync(prompt, max_tokens=500)


def rag_chat(question: str, ctx: dict) -> dict:
    """Q&A over a profile — used by the voice assistant."""
    prompt = _build_chat_prompt(question, ctx)
    answer = _call_ollama_sync(prompt, max_tokens=300)
    return {"answer": answer, "success": bool(answer)}


def rag_stream_executive(ctx: dict, profile_type: str):
    """Used by /api/ai/compose in executive mode — emits {type:token} dicts."""
    import json
    prompt = _build_executive_prompt(ctx)
    for token in _call_ollama_stream(prompt, max_tokens=600):
        yield json.dumps({"type": "token", "content": token})


def digest_session_reports_stream(tool_name: str, rows: list):
    """Summarise session reports for one tool — used by /api/ai/session-digest."""
    if not rows:
        yield "No session reports found for this tool."
        return
    
    reports_text = "\n".join(
        f"- [{r.get('submitted_at', '?')}] {r.get('report_details', '')}"
        for r in rows[:30]
    )
    prompt = (
        f"You are summarising equipment session reports for {tool_name} "
        f"at IIT Bombay Nanofabrication Facility.\n\n"
        f"Reports:\n{reports_text}\n\n"
        f"Write exactly 3 concise bullet points summarising: "
        f"common usage patterns, any recurring issues, and overall condition. "
        f"Be factual. No preamble."
    )
    yield from _call_ollama_stream(prompt, max_tokens=250)


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_report_query(ctx: dict) -> str:
    """Build a retrieval query string from context fields."""
    parts = []
    if ctx.get("name"):        parts.append(ctx["name"])
    if ctx.get("designation"): parts.append(ctx["designation"])
    if ctx.get("team"):        parts.append(ctx["team"])
    if ctx.get("role"):        parts.append(ctx["role"])
    return " ".join(parts)


def _format_context(ctx: dict) -> str:
    return "\n".join(
        f"{k}: {v}"
        for k, v in ctx.items()
        if v and str(v) not in ("N/A", "None", "0", "")
    )


def _format_chunks(chunks: list) -> str:
    return "\n".join(c["text"] for c in chunks[:5]) if chunks else ""


def _build_executive_prompt(ctx: dict, rag_block: str = "") -> str:
    context_block = _format_context(ctx)
    rag_section = f"Reference Data:\n{rag_block}\n\n" if rag_block else ""

    return (
        "You are an HR data reporter. Output ONLY the 4 paragraphs below. "
        "No introduction. No conclusion. No markdown. No bold text. No asterisks. "
        "No phrases like 'I am pleased' or 'it is worth noting'. "
        "Only use facts from the data. If a fact is missing, write 'Not on record'.\n\n"

        f"{rag_section}"
        f"Data:\n{context_block}\n\n"

        "Write exactly this structure, replacing the brackets with real values:\n\n"

        "Paragraph 1 (Identity): Write one sentence stating the person's name, "
        "designation, team, appointment type, and  iitb joining date.  Also mention their system role if it differs from Staff. "
        "2 to 3 sentences.\n\n"

        "Paragraph 2 (Attendance): Write one sentence stating the exact attendance "
        "percentage this year and whether it is above or below the 75% mandatory threshold. State the number of leave days taken and the leave type "
        "breakdown if available. 2 to 3 sentences\n\n"

        "Paragraph 3 — Facility Activity: Cover equipment usage requests, slot bookings, "
        "reservations, tool permissions, and system ownership (current and historical). "
        "Include training sessions completed. If none exist, write 'No equipment activity on record.' "
        "3 to 4 sentences.\n\n"

        "Paragraph 4 — Research Output: State the number of approved publications, "
        "active and total projects, and monthly reports submitted with average rating if available. "
        "If none, write 'No research output on record.' "
        "2 sentences.\n\n"

        "Begin paragraph 1 now, starting with the person's name:"
    )
def _build_chat_prompt(question: str, ctx: dict) -> str:
    context_block = _format_context(ctx)
    return (
        "You are an HR assistant for IITBNF. Answer the question using only "
        "the personnel data provided. Be concise and factual.\n\n"
        f"Personnel Data:\n---\n{context_block}\n---\n\n"
        f"Question: {question}\nAnswer:"
    )


# ── RAG config constants (used by retrieve.py and debug_ai.py) ────────────────
RAG_K     = 5
MIN_SCORE = 0.05
N_CTX     = 4096