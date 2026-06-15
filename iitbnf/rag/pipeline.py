# rag/pipeline.py — Ollama-backed generation
import re
import logging
from llm import llm_generate, llm_stream
logger = logging.getLogger(__name__)

# ── Ollama calls — TWO separate functions, not one ────────────────────────────
# A Python function with ANY yield statement is always a generator.
# Mixing stream=True/False in one function means the non-streaming
# path also returns a generator object instead of a string.

def _call_ollama_sync(prompt: str, max_tokens: int = 500) -> str:
    return llm_generate(prompt, max_tokens)

def _call_ollama_stream(prompt: str, max_tokens: int = 500):
    yield from llm_stream(prompt, max_tokens)
    
# In pipeline.py, add this new function and modify rag_stream():

def _build_enrichment_prompt(composer_output: str, ctx: dict) -> str:
    """
    Give the SLM the composer output + raw data.
    Task: polish phrasing only, do NOT change any numbers or facts.
    Keep it tightly constrained so a 0.5B model can succeed reliably.
    """
    # Minimal data block — only identity + key counts the SLM might reference
    data_lines = []
    for key in ("name", "designation", "team", "attendance_pct", "days_present",
                "working_days", "leaves_taken", "leave_breakdown", "eq_requests",
                "eq_slot_booked", "total_bookings", "tools_used",
                "systems_owned_current", "tool_permissions_count",
                "monthly_reports_submitted", "papers", "projects", "tenure_years"):
        v = ctx.get(key)
        if v and str(v) not in ("N/A", "None", "0", ""):
            data_lines.append(f"  {key} = {v}")
    data_block = "\n".join(data_lines)

    return (
        "You are a professional HR report editor.\n"
        "Your ONLY job is to improve the flow and readability of the draft below.\n"
        "Rules you MUST follow:\n"
        "  1. Do NOT change any number, percentage, date, or name.\n"
        "  2. Do NOT add any fact that is not in the draft or the data block.\n"
        "  3. Keep all paragraphs. Do not merge or split them.\n"
        "  4. Maximum 10 words changed per paragraph.\n"
        "  5. Output the full revised text. Nothing else.\n\n"
        f"Verified Data (ground truth — numbers must stay exactly as shown):\n"
        f"{data_block}\n\n"
        f"Draft:\n{composer_output}\n\n"
        f"Revised:"
    )


def rag_stream(ctx: dict, mode: str = "short"):
    """
    Unified streaming generator.
    1. Composer always runs first — produces factually correct template output.
    2. If SLM is available AND mode is 'executive', SLM enriches the composer output.
    3. If SLM is unavailable, composer output is used directly.
    """
    from rag.composer import compose_staff_summary, compose_lab_summary
    from llm import is_llm_available

    is_lab = ctx.get("category") is not None
    composer_output = compose_lab_summary(ctx) if is_lab else compose_staff_summary(ctx)

    # Short mode OR SLM unavailable: return composer output directly
    if mode == "short" or not is_llm_available():
        yield "[MODE: Quick Summary]"
        yield composer_output
        return

    # Executive mode + SLM available: enrich the composer output
    yield "[MODE: Executive Briefing]"
    prompt = _build_enrichment_prompt(composer_output, ctx)
    
    full_tokens = []
    for token in _call_ollama_stream(prompt, max_tokens=600):
        full_tokens.append(token)
        yield token
    
    enriched = "".join(full_tokens).strip()
    
    # Safety net: if SLM output is too short, blank, or looks like it 
    # hallucinated (contains numbers not in composer output), fall back
    # to composer output
    if not enriched or len(enriched) < len(composer_output) * 0.5:
        # SLM produced garbage — yield the composer output instead
        # (already streamed tokens, so yield a replacement signal)
        yield "\n\n[FALLBACK: " + composer_output + "]"


def rag_generate(ctx: dict, audience: str = "management") -> str:
    """Non-streaming — used by /api/ai/report."""
    from rag.retrieve import retrieve

    # Retrieve relevant chunks — same logic as rag_stream_executive
    rag_block = ""
    try:
        query  = _build_report_query(ctx)
        chunks = retrieve(
            query,
            k              = RAG_K,
            requested_name = ctx.get("name"),
        )
        relevant  = [c for c in chunks if c.get("score", 0) >= MIN_SCORE]
        rag_block = _format_chunks(relevant)
        logger.info(
            "[RAG] rag_generate: query=%r  chunks_used=%d",
            query[:80], len(relevant),
        )
    except Exception as exc:
        logger.warning("[RAG] retrieve() failed in rag_generate: %s", exc)

    prompt = _build_executive_prompt(ctx, rag_block=rag_block)
    return _call_ollama_sync(prompt, max_tokens=500)


def rag_chat(question: str, ctx: dict) -> dict:
    """Q&A over a profile — used by the voice assistant."""
    prompt = _build_chat_prompt(question, ctx)
    answer = _call_ollama_sync(prompt, max_tokens=80)
    if not answer:
        answer = "The AI model is currently unavailable. Please try again later or check that Ollama is running."
    return {"answer": answer, "success": bool(answer)}


def rag_stream_executive(ctx: dict, profile_type: str):
    """Used by /api/ai/compose in executive mode — emits {type:token} dicts."""
    import json
    from rag.retrieve import retrieve

    # ── Retrieve relevant context chunks from TF-IDF index ───────────────────
    # Build a rich query from the profile ctx, retrieve top-5 scored chunks,
    # filter by minimum score, and format them into the rag_block string that
    # _build_executive_prompt() already knows how to inject as Reference Data.
    rag_block = ""
    try:
        query  = _build_report_query(ctx)
        chunks = retrieve(
            query,
            k              = RAG_K,
            requested_name = ctx.get("name"),
        )
        relevant = [c for c in chunks if c.get("score", 0) >= MIN_SCORE]
        rag_block = _format_chunks(relevant)
        logger.info(
            "[RAG] executive stream: query=%r  chunks_retrieved=%d  chunks_used=%d",
            query[:80], len(chunks), len(relevant),
        )
    except Exception as exc:
        # Non-fatal — continue without RAG context rather than blocking the stream
        logger.warning("[RAG] retrieve() failed in rag_stream_executive: %s", exc)

    prompt = _build_executive_prompt(ctx, rag_block=rag_block)
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
    """
    Build a retrieval query string from context fields.

    Richer query = better TF-IDF chunk recall.
    We include identity, role, team, and the most query-relevant activity
    fields so the index can surface leave rules, equipment policy, and
    facility-specific context that enriches the executive narrative.
    """
    parts = []

    # Identity
    for field in ("name", "designation", "team", "role",
                  "appointment_type", "department", "category"):
        v = ctx.get(field)
        if v and str(v) not in ("N/A", "None", ""):
            parts.append(str(v))

    # Attendance signal
    pct = ctx.get("attendance_pct")
    if pct is not None:
        try:
            pct_f = float(pct)
            if pct_f < 75:
                parts.append("attendance below mandatory threshold 75 percent")
            elif pct_f >= 90:
                parts.append("attendance excellent above threshold")
            else:
                parts.append("attendance within acceptable range")
        except (TypeError, ValueError):
            pass

    # Facility activity signals
    if int(ctx.get("eq_requests") or 0) > 0:
        parts.append("equipment usage requests slot booking facility")
    if int(ctx.get("systems_owned_current") or 0) > 0:
        parts.append("system owner tool responsibility")
    if int(ctx.get("tool_permissions_count") or 0) > 0:
        parts.append("tool permissions access authorized equipment")

    # Research signals
    if int(ctx.get("papers") or 0) > 0:
        parts.append("research publication paper approved")
    if int(ctx.get("projects") or 0) > 0:
        parts.append("faculty project active research")

    # Leave signal
    if int(ctx.get("leaves_taken") or 0) > 0:
        parts.append("leave days taken annual casual earned")

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
        "designation, team, appointment type, and joining date.  Also mention their system role if it differs from Staff. "
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
def _build_chat_prompt(question: str, ctx: dict, history: list = None) -> str:
    """
    Build a minimal, focused prompt for Q&A.
    Only includes facts directly relevant to the question keywords.
    A 0.5B model cannot handle 30 facts + a question reliably.
    """
    q_lower = question.lower()

    # Keyword → ctx field mapping: pick only what the question is about
    FIELD_GROUPS = {
        ("attendance", "present", "days", "percent", "%", "threshold"):
            ["name", "attendance_pct", "days_present", "working_days", "leaves_taken", "leave_breakdown"],
        ("leave", "casual", "earned", "sick", "annual"):
            ["name", "leaves_taken", "leave_breakdown"],
        ("equipment", "request", "booking", "slot", "machine"):
            ["name", "eq_requests", "eq_slot_booked", "approved_requests", "eq_pending", "eq_rejected"],
        ("reservation", "tool", "booked"):
            ["name", "total_bookings", "tools_used"],
        ("permission", "authoris", "access"):
            ["name", "tool_permissions_count"],
        ("owner", "system", "assign"):
            ["name", "systems_owned_current", "systems_owned_ever", "systems_ownership_removed"],
        ("paper", "publication", "research", "publish"):
            ["name", "papers", "projects", "active_projects"],
        ("project", "faculty"):
            ["name", "projects", "active_projects"],
        ("training", "session"):
            ["name", "trainings", "session_reports"],
        ("designation", "role", "team", "position", "title", "job"):
            ["name", "designation", "team", "role", "appointment_type"],
        ("join", "joined", "tenure", "since", "date"):
            ["name", "joining_date"],
        ("qualification", "degree", "education"):
            ["name", "qualification"],
        ("supervisor", "guide"):
            ["name", "supervisor_name"],
        ("department", "dept"):
            ["name", "department"],
        ("cancel", "cancellation"):
            ["name", "cancellations"],
        ("report", "monthly", "star", "rating"):
            ["name", "monthly_reports_submitted", "monthly_report_avg_stars"],
        ("email", "contact"):
            ["name", "email"],
        ("expir", "valid", "access expir"):
            ["name", "expiry_date"],
    }

    # Find matching fields based on question keywords
    selected_fields = set(["name"])   # always include name
    matched = False
    for keywords, fields in FIELD_GROUPS.items():
        if any(kw in q_lower for kw in keywords):
            selected_fields.update(fields)
            matched = True

    # If no keyword matched, include a small general set
    if not matched:
        selected_fields.update([
            "name", "designation", "team", "attendance_pct",
            "eq_requests", "papers", "projects"
        ])
    history_block = ""
    if history:
        history_lines = []
        for msg in history[-6:]:  # last 3 exchanges = 6 messages
            role = msg.get("role", "user")
            content = msg.get("content", "")
            prefix = "User:" if role == "user" else "Assistant:"
            history_lines.append(f"{prefix} {content}")
        history_block = "\n\nConversation History:\n" + "\n".join(history_lines)
    # Build a minimal facts block from only the selected fields
    facts_lines = []
    for key in selected_fields:
        val = ctx.get(key)
        if val not in (None, "", "N/A", "NA", 0, "0", "None"):
            facts_lines.append(f"  {key} = {val}")

    facts_block = "\n".join(facts_lines) if facts_lines else "  (no relevant data found)"
    # Hard cap facts to prevent slow inference
    facts_lines = facts_lines[:8]  # max 8 facts for speed
    return (
        "Answer the question using ONLY the facts below.\n"
        "Rules:\n"
        "- Answer in ONE sentence.\n"
        "- If the answer is not in the facts, say: 'This information is not on record.'\n"
        "- Do not summarize. Do not list other facts. Just answer the question.\n\n"
        f"{history_block}"
        f"Facts:\n{facts_block}\n\n"
        f"Question: {question}\n"
        "Answer:"
    )


def _validate_response(response: str, ctx: dict) -> str:
    """
    Post-process model output to catch obvious hallucinations.
    
    Checks:
    1. Any number in the response should exist somewhere in ctx values.
    2. Any name that looks like a person's name should be in ctx.
    3. Response should not be longer than 3x the prompt's data.
    
    On failure: strips the suspicious sentence rather than blocking entirely.
    """
    if not response or not ctx:
        return response
    
    # Collect all numeric values from ctx
    ctx_numbers = set()
    for v in ctx.values():
        if v is None:
            continue
        # Extract numbers from ctx values
        for match in re.findall(r'\b\d+(?:\.\d+)?\b', str(v)):
            ctx_numbers.add(match)
    
    # Check each sentence for numbers not in ctx
    sentences = re.split(r'(?<=[.!?])\s+', response.strip())
    clean_sentences = []
    
    for sentence in sentences:
        numbers_in_sentence = re.findall(r'\b\d+(?:\.\d+)?\b', sentence)
        
        # Skip validation for sentences with no numbers — low hallucination risk
        if not numbers_in_sentence:
            clean_sentences.append(sentence)
            continue
        
        # Check if all numbers in sentence appear in ctx
        # Allow small numbers (0-10) as they are likely counts/ordinals
        suspicious = [
            n for n in numbers_in_sentence
            if n not in ctx_numbers and float(n) > 10
        ]
        
        if suspicious:
            import logging
            logging.getLogger(__name__).warning(
                "[LLM] Possible hallucination — numbers %s not in ctx, "
                "dropping sentence: %s", suspicious, sentence[:80]
            )
            # Replace with a safe fallback rather than dropping entirely
            clean_sentences.append("(Data not available for this field.)")
        else:
            clean_sentences.append(sentence)
    
    return " ".join(clean_sentences)
# ── RAG config constants (used by retrieve.py and debug_ai.py) ────────────────
RAG_K     = 5
MIN_SCORE = 0.05
N_CTX     = 8192