"""
rag/pipeline.py — RAG pipeline: retrieve relevant chunks, inject into prompt,
                  call local LLM via llama-cpp-python for generation.

Called from:
  - models/ai.py         → generate_staff_report / generate_lab_report
  - routes/rag_routes.py → /api/rag/chat

Public API:    rag_generate(ctx, audience)  → str   (full report)
    rag_chat(question, ctx)      → dict  (answer + source chunks)
    rag_status()                 → dict  (health check)
    rag_stream(ctx, mode)        → Generator[str]  (SSE tokens)

"""
import re
import logging
import os
import threading
from pathlib import Path
import platform
from rag.retrieve import retrieve
# [FIX 1] Import collection_size from ingest — the correct implementation.
# The old rag.retrieve.collection_size() called get_index() without unpacking
# the returned 3-tuple, so len() always returned 3 (tuple length) and the
# index was always seen as non-empty even when it was blank.
from rag.ingest import collection_size
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
# [FIX 8] os imported at the top — see module docstring.
MODEL_PATH = Path(__file__).parent.parent / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
RAG_K      = 3
MIN_SCORE  = 0.25

# Generation params — tuned for fast CPU inference on a 1.5B model
# N_CTX=2048: halves KV-cache memory vs 4096, noticeably faster first-token latency
# MAX_TOKENS=200: keeps reports concise; streaming hides the wait anyway
# n_threads=all physical cores on Linux (not just half)
N_CTX       = 2048
N_THREADS   = 4 if platform.system() == "Windows" else max(2, os.cpu_count() or 4)
MAX_TOKENS  = 200                     # shorter = faster; streaming makes it feel instant
CHAT_TOKENS = 120                     # chat answers should be punchy
TEMPERATURE = 0.35                    # slightly lower = less sampling overhead

# ── LLM singleton (loaded once, thread-safe) ──────────────────────────────────
_llm          = None
_llm_lock     = threading.Lock()
_llm_loaded   = False

# [FIX 2] Serialises ALL inference calls — llama-cpp-python is not thread-safe.
_inference_lock = threading.Lock()


def _get_llm():
    """
    Load the GGUF model once per process.
    Thread-safe — concurrent calls wait for the first load to finish.
    Returns the Llama instance, or None if the model file is missing.
    """
    global _llm, _llm_loaded

    if _llm_loaded:
        return _llm

    with _llm_lock:
        if _llm_loaded:            # double-checked inside lock
            return _llm

        if not MODEL_PATH.exists():
            logger.error(
                "Model file not found: %s — "
                "download qwen2.5-1.5b-instruct-q4_k_m.gguf from HuggingFace "
                "and place it in the models/ directory.", MODEL_PATH
            )
            _llm_loaded = True     # mark attempted so we don't retry every call
            return None

        try:
            from llama_cpp import Llama  # noqa: PLC0415
            logger.info(
                "Loading LLM from %s (may take 10-30 s on first load)...", MODEL_PATH
            )
            _llm = Llama(
                model_path   = str(MODEL_PATH),
                n_ctx        = N_CTX,
                n_threads    = N_THREADS,
                n_gpu_layers = 0,      # CPU only
                verbose      = False,
            )
            logger.info("LLM loaded successfully.")
        except Exception as e:
            logger.error("Failed to load LLM: %s", e, exc_info=True)
            _llm = None

        _llm_loaded = True
        return _llm


# ── Internal helpers ──────────────────────────────────────────────────────────

def _format_context(ctx: dict) -> str:
    """
    Convert context dict to clearly-labelled lines for the LLM prompt.

    Explicit labels are used instead of auto-titling the key names so the
    model receives natural English descriptions rather than internal field
    names like 'Eq Slot Booked' or 'Systems Owned Ever'.

    Keys not in the map fall back to auto-titling (forward-compatible with
    new fields added to the context builders).
    """
    # ── Explicit human-readable label map ─────────────────────────────────────
    LABEL: dict[str, str] = {
        # Identity
        "name":                      "Name",
        "designation":               "Designation",
        "role":                      "System Role",
        "team":                      "Team",
        "joining_date":              "Joining Date",
        "appointment_type":          "Appointment Type",
        "qualification":             "Qualification",
        "project_code":              "Project Code",
        # Lab identity extras
        "category":                  "User Category",
        "department":                "Department",
        "rollno":                    "Roll Number",
        "research_area":             "Research Area",
        "supervisor_name":           "Supervisor",
        "expiry_date":               "Access Expiry Date",
        "reg_course":                "Registered Course",
        "reg_status":                "Course Registration Status",
        "reg_project":               "Primary Project (Registration)",
        # Attendance
        "attendance_pct":            "Attendance Rate (%)",
        "days_present":              "Days Present (this year)",
        "working_days":              "Total Working Days (this year)",
        "leaves_taken":              "Total Leave Days Taken",
        "leave_breakdown":           "Leave Type Breakdown",
        # Monthly reports
        "monthly_reports_submitted": "Monthly Reports Submitted",
        "monthly_report_avg_stars":  "Monthly Report Average Rating (stars)",
        "monthly_report_latest_year":"Most Recent Monthly Report Year",
        # Slot reservations
        "total_bookings":            "Slot Reservations (lifetime)",
        "tools_used":                "Distinct Tools Used",
        # Equipment usage requests
        "eq_requests":               "Equipment Usage Requests (total)",
        "eq_slot_booked":            "Equipment Requests — Slot Booked",
        "eq_approved":               "Equipment Requests — Approved",
        "eq_pending":                "Equipment Requests — Pending",
        "eq_rejected":               "Equipment Requests — Rejected",
        # Lab-specific eq keys
        "approved_requests":         "Equipment Requests Approved",
        # Permissions
        "tool_permissions_count":    "Equipment Access Permissions Held",
        # System ownership
        "systems_owned_current":     "Tools Currently Under System Ownership",
        "systems_owned_ever":        "Tools Ever Assigned as System Owner",
        "systems_ownership_removed": "System Ownership Assignments Removed",
        # Activity
        "trainings":                 "Equipment Training Sessions Completed",
        "session_reports":           "Equipment Session Reports Filed",
        "cancellations":             "Reservation Cancellations",
        # Research
        "papers":                    "Approved Research Publications",
        "projects":                  "Faculty Projects (total)",
        "active_projects":           "Faculty Projects (currently active)",
    }

    SKIP_IF_ZERO = {
        "eq_slot_booked", "eq_approved", "eq_pending", "eq_rejected",
        "approved_requests", "tool_permissions_count",
        "systems_owned_current", "systems_owned_ever", "systems_ownership_removed",
        "trainings", "session_reports", "cancellations",
        "papers", "projects", "active_projects",
        "monthly_reports_submitted", "total_bookings", "tools_used", "eq_requests",
    }

    lines = []
    for key, val in ctx.items():
        if val is None:
            continue
        str_val = str(val).strip()
        if str_val in ("N/A", "None", ""):
            continue
        # Skip numeric zeros for "activity" fields — avoids cluttering the
        # prompt with "Equipment Requests — Pending: 0" etc.
        if key in SKIP_IF_ZERO and str_val == "0":
            continue
        label = LABEL.get(key) or key.replace("_", " ").title()
        lines.append(f"{label}: {val}")
    return "\n".join(lines)


def _format_chunks(chunks: list[dict]) -> str:
    """Format retrieved chunks into a numbered reference block."""
    if not chunks:
        return ""
    parts = []
    for i, c in enumerate(chunks, 1):
        source = c.get("source", "unknown")
        text   = c.get("text", "").strip()
        parts.append(f"[{i}] {source}\n{text}")
    return "\n\n".join(parts)


def _build_report_query(ctx: dict) -> str:
    """Build a retrieval query targeted at the most reportable fields."""
    parts = []
    if ctx.get("name"):
        parts.append(ctx["name"])
    if ctx.get("designation"):
        parts.append(ctx["designation"])
    if ctx.get("team"):
        parts.append(f"{ctx['team']} team")
    if "attendance_pct" in ctx and ctx["attendance_pct"] not in ("N/A", None, ""):
        parts.append(f"attendance {ctx['attendance_pct']}% compliance")
    if ctx.get("category"):
        parts.append(ctx["category"])
    if "total_bookings" in ctx and ctx["total_bookings"]:
        parts.append("equipment usage lab reservations")
    if "papers" in ctx and ctx["papers"]:
        parts.append("research publications output")
    return " ".join(parts) if parts else "IITBNF staff profile attendance equipment"


def _safe_truncate_prompt(prompt: str, max_tokens: int) -> str:
    """
    [FIX 3] Truncate the prompt to fit within the model's context window.

    Uses a conservative 3-char-per-token estimate (safer than 4).
    Clips the MIDDLE of the prompt (the RAG reference block) rather than the
    end, so the system message and personnel data are always preserved.
    """
    # chars available for the prompt body
    max_prompt_chars = (N_CTX - max_tokens) * 2.5 * 0.9  # 10% safety margin

    if len(prompt) <= max_prompt_chars:
        return prompt

    # Split on the assistant turn marker so we always keep the structure.
    assistant_tag = "<|im_start|>assistant\n"
    split_idx     = prompt.rfind(assistant_tag)

    if split_idx == -1:
        # Fallback: hard-trim from the front
        logger.warning("Prompt truncated (no assistant tag found) — keeping last %d chars.", max_prompt_chars)
        return prompt[-max_prompt_chars:] + assistant_tag

    header = prompt[:split_idx]
    suffix = assistant_tag

    # Trim the header section from the middle (preserve system + last user block)
    if len(header) > max_prompt_chars - len(suffix):
        keep = max_prompt_chars - len(suffix)
        # Keep the first 20 % (system message) and the last 80 % (user data)
        head_keep = max(0, keep // 5)
        tail_keep = keep - head_keep
        header = header[:head_keep] + "\n[...truncated...]\n" + header[-tail_keep:]
        logger.warning("Prompt header truncated to fit N_CTX=%d.", N_CTX)

    return header + suffix


def _call_llm(prompt: str, max_tokens: int = MAX_TOKENS) -> str:
    """
    Run inference via llama-cpp-python. Returns generated text or ''.
    [FIX 2] Acquires _inference_lock — only one inference at a time.
    [FIX 4] Resets _llm_loaded on unexpected errors so next call can retry.
    """
    global _llm_loaded

    llm = _get_llm()
    if llm is None:
        return ""

    prompt = _safe_truncate_prompt(prompt, max_tokens)

    with _inference_lock:
        try:
            output = llm(
                prompt,
                max_tokens  = max_tokens,
                temperature = TEMPERATURE,
                stop        = ["<|im_end|>", "<|im_start|>"],
                echo        = False,
            )
            return output["choices"][0]["text"].strip()
        except Exception as e:
            logger.error("LLM inference failed: %s", e, exc_info=True)
            # [FIX 4] If the model threw an unexpected error, reset so we
            # attempt a fresh load on the next call rather than silently
            # returning empty strings forever.
            if "llama_decode" in str(e) or "context" in str(e).lower():
                logger.warning("Resetting LLM state for reload on next call.")
                _llm_loaded = False
            return ""


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_report_prompt(ctx: dict, audience: str, chunks: list[dict]) -> str:
    context_block = _format_context(ctx)
    subject_name  = ctx.get("name", "this person")

    # chunks are intentionally left unused — RAG retrieval is disabled to
    # prevent cross-person hallucination (other staff names from the index).

    if audience == "individual":
        tone    = f"Write in second person about {subject_name} (use 'you'). Be encouraging and constructive."
        closing = "End with a brief forward-looking sentence about growth or contribution."
    else:
        tone    = f"Write in third person about {subject_name}. Be factual, concise, and professional."
        closing = "End with a brief overall assessment suitable for a performance review."

    return (
        f"<|im_start|>system\n"
        f"You are an HR analyst for IIT Bombay Nanofabrication Facility (IITBNF). "
        f"Your task is to write a report ONLY about {subject_name}. "
        f"{tone} "
        f"Do not fabricate numbers. Use ONLY the values in the Personnel Data below. "
        f"Do not reference any other person or invent information. "
        f"{closing}<|im_end|>\n"
        f"<|im_start|>user\n"
        f"Generate a concise profile report (3-5 paragraphs) for {subject_name}. "
        f"Cover only sections where data is available: attendance and leave, equipment usage, research output.\n\n"
        f"Personnel Data for {subject_name}:\n---\n{context_block}\n---\n"
        f"Write only about {subject_name} using only the data above.<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )
def _build_chat_prompt(question: str, ctx: dict, chunks: list[dict]) -> str:
    context_block = _format_context(ctx)
    rag_block     = _format_chunks(chunks)

    rag_section = ""
    if rag_block:
        rag_section = (
            "\nRetrieved Records:\n"
            "---\n"
            f"{rag_block}\n"
            "---\n"
        )

    return (
    f"<|im_start|>system\n"
    f"You are an HR analyst assistant for IIT Bombay Nanofabrication Facility (IITBNF). "
    f"Answer questions using only the data provided. Be concise and specific. "
    f"Use actual numbers. Do not invent information. "
    f"If the retrieved records do not clearly contain the answer, say that the information could not be found. "
    f"Never guess names, IDs, years, attendance, slot activity, leave records, or equipment usage.<|im_end|>\n"
    f"<|im_start|>user\n"
    f"{rag_section}"
    f"Personnel Data:\n---\n{context_block}\n---\n"
    f"Question: {question}<|im_end|>\n"
    f"<|im_start|>assistant\n"
)

def _build_executive_prompt(
    ctx:         dict,
    base_summary: str,
    profile_type: str = "staff",
) -> str:
    """
    Executive summary prompt.

    Strategy: give the LLM the composer's clean factual base as a
    grounding reference. It is explicitly told it may ONLY use the
    numbers and facts already present — it elaborates tone and
    structure, never invents data.

    This eliminates hallucination while letting the LLM add the
    professional register, transitions, and interpretive commentary
    that the template system cannot.
    """
    context_block = _format_context(ctx)
    subject_name  = ctx.get("name", "this person")
    is_lab        = profile_type == "lab"

    if is_lab:
        role_desc = (
            f"a {ctx.get('category', 'lab')} user "
            f"from the {ctx.get('department', 'facility')} department"
        )
        focus_para = (
            "Paragraph 3: Equipment and facility usage — "
            "describe slot reservations, equipment requests, approvals, "
            "and session reports using only the numbers provided. "
            "Comment on the level of engagement with facility resources."
        )
    else:
        role_desc = (
            f"{ctx.get('designation', 'staff member')} "
            f"in the {ctx.get('team', 'facility')} team"
        )
        focus_para = (
            "Paragraph 3: Equipment activity and system responsibilities — "
            "cover slot bookings, equipment requests, system ownership, "
            "and tool permissions using only the numbers provided."
        )

    return (
        f"<|im_start|>system\n"
        f"You are a senior HR analyst writing formal executive summaries "
        f"for IIT Bombay Nanofabrication Facility (IITBNF) management. "
        f"You will be given a factual base summary and the raw personnel data "
        f"it was derived from. Your task is to expand this into a formal "
        f"executive summary.\n\n"
        f"STRICT RULES:\n"
        f"- Use ONLY numbers and facts present in the Personnel Data or Base Summary.\n"
        f"- Do NOT invent, estimate, or extrapolate any figures.\n"
        f"- Do NOT mention any other person by name.\n"
        f"- Write in formal third person throughout.\n"
        f"- Do NOT use bullet points or headers — flowing paragraphs only.\n"
        f"- Each paragraph must be 3-5 sentences.\n"
        f"<|im_end|>\n"

        f"<|im_start|>user\n"
        f"Write a formal 4-paragraph executive summary for {subject_name}, "
        f"{role_desc} at IITBNF.\n\n"

        f"Paragraph 1 — Professional Profile: State {subject_name}'s role, "
        f"team, qualification, appointment type, and tenure at the facility. "
        f"Contextualise their position within the facility's operations.\n\n"

        f"Paragraph 2 — Attendance and Punctuality: Report the exact "
        f"attendance percentage and days present. State clearly whether "
        f"this meets, exceeds, or falls below the 75% mandatory threshold. "
        f"Comment on leave patterns if data is available.\n\n"

        f"{focus_para}\n\n"

        f"Paragraph 4 — Research and Academic Output: Cover publications "
        f"and project involvement. If neither is present, state that clearly "
        f"and note this may reflect the nature of the role rather than "
        f"a deficiency.\n\n"

        f"End with one sentence overall assessment of the member's "
        f"engagement with the facility.\n\n"

        f"Base Summary (factual anchor — do not contradict this):\n"
        f"---\n{base_summary}\n---\n\n"

        f"Personnel Data:\n"
        f"---\n{context_block}\n---\n\n"

        f"Write only about {subject_name}. "
        f"Use only the data above.<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )

# ── Public API ────────────────────────────────────────────────────────────────

def rag_generate(ctx: dict, audience: str = "management") -> str:
    """
    Full RAG report generation pipeline.
    NOTE: RAG chunk retrieval is disabled — the ctx dict is the sole data source.
    Injecting TF-IDF chunks from the index caused cross-person hallucination
    (the model picked up other staff members' names from retrieved records).

    Args:
        ctx      : context dict from _build_staff_context / _build_lab_context
        audience : "management" or "individual"
    """
    def rag_generate(ctx: dict, audience: str = "management") -> str:
    # Fast path: TF-IDF composer (no LLM needed)
        try:
            from rag.composer import compose_staff_summary, compose_lab_summary
            # Detect profile type from context shape
            is_lab = "category" in ctx or "session_reports" in ctx
            summary = compose_lab_summary(ctx) if is_lab else compose_staff_summary(ctx)
            if summary and len(summary.split()) > 20:
                logger.info("Composer summary generated (%d words).", len(summary.split()))
                return summary
        except Exception as e:
            logger.warning("Composer failed, falling back to LLM: %s", e)

        # Slow path: GGUF model
        prompt = _build_report_prompt(ctx, audience, [])
        return _call_llm(prompt, max_tokens=MAX_TOKENS)
    # Pass empty chunks — no RAG retrieval to prevent hallucination
    prompt = _build_report_prompt(ctx, audience, [])
    return _call_llm(prompt, max_tokens=MAX_TOKENS)


# ── Comparative / policy trigger keywords ─────────────────────────────────────
# If the user's question contains ANY of these, it means the personal CAG
# context alone is insufficient and we should also consult the TF-IDF index.
_COMPARATIVE_KEYWORDS = {
    "compare", "comparison", "vs", "versus", "better than", "worse than",
    "team average", "average attendance", "how does", "relative to",
    "benchmark", "all staff", "everyone",
}
_POLICY_KEYWORDS = {
    "max leave", "maximum leave", "allowed leave", "leave policy",
    "entitlement", "leave rule", "how many days", "leave limit",
    "holiday", "public holiday", "institute holiday",
}


def _is_comparative_or_policy(question: str) -> bool:
    """
    Return True when the question asks for comparison or policy information
    that cannot be answered from the personal context dict alone.
    This gates the RAG retrieval layer so it only fires when actually needed.
    """
    q = question.lower()
    for kw in _COMPARATIVE_KEYWORDS | _POLICY_KEYWORDS:
        if kw in q:
            return True
    return False


def parse_query(question: str) -> dict:
    """
    Parse a natural-language question into retrieval parameters.
    Only called when _is_comparative_or_policy() returns True.
    """
    q = question.lower()

    parsed: dict = {
        "rewritten_query": question,
        "allowed_types":   None,
        "staff_name":      None,
        "staff_id":        None,
        "year":            None,
    }

    year_match = re.search(r"\b(20\d{2})\b", question)
    if year_match:
        parsed["year"] = int(year_match.group(1))

    id_match = re.search(r"#(\d+)|id\s*(\d+)", q)
    if id_match:
        parsed["staff_id"] = id_match.group(1) or id_match.group(2)

    if any(kw in q for kw in _POLICY_KEYWORDS):
        parsed["allowed_types"] = ["leave_rule"]
    elif "slot" in q or "equipment" in q or "booking" in q or "usage" in q:
        parsed["allowed_types"] = ["slot_activity", "equipment_activity"]
    elif "attendance" in q:
        parsed["allowed_types"] = ["attendance"]
    elif "profile" in q or "designation" in q or "team" in q:
        parsed["allowed_types"] = ["staff_profile"]

    return parsed


def rag_chat(question: str, ctx: dict) -> dict:
    """
    CAG + gated-RAG chat pipeline.

    Strategy
    ────────
    Layer 0 — CAG (always):
        The personal context dict (ctx) is injected directly into the
        prompt. This alone answers ~90% of questions about "this person"
        with zero retrieval latency.

    Layer 1 — RAG (gated):
        Only triggered when the question contains comparative or policy
        keywords (team average, max leave, holiday rules, etc.) that the
        personal context cannot answer.
        Chunks are filtered by score >= MIN_SCORE.
        Subject name isolation: chunks containing another person's name
        are down-ranked so the model does not confuse subjects.

    Returns
    ───────
    {
        "answer":       str,
        "chunks":       list[dict],
        "rag_used":     bool,   ← tells the caller / debug routes
        "success":      bool,
    }
    """
    if not question or not question.strip():
        return {"success": False, "answer": "Please enter a question.",
                "chunks": [], "rag_used": False}

    chunks: list[dict] = []
    rag_used = False

    # ── Gated RAG ─────────────────────────────────────────────────────────────
    if collection_size() > 0 and _is_comparative_or_policy(question):
        rag_used = True
        parsed   = parse_query(question)
        logger.info(
            "CAG+RAG: gated retrieval triggered — "
            "name=%s id=%s year=%s types=%s",
            parsed["staff_name"], parsed["staff_id"],
            parsed["year"], parsed["allowed_types"],
        )
        raw = retrieve(
            parsed["rewritten_query"],
            k=RAG_K,
            allowed_types=parsed["allowed_types"],
            requested_name=parsed["staff_name"],
            requested_id=parsed["staff_id"],
            requested_year=parsed["year"],
        )

        # Subject isolation — drop chunks that mention a DIFFERENT person by name.
        subject_name = (ctx.get("name") or "").lower()
        safe_chunks  = []
        for c in raw:
            if c.get("score", 0) < MIN_SCORE:
                continue
            chunk_text_lower = c.get("text", "").lower()
            chunk_name       = (c.get("staff_name") or "").lower()
            # Keep chunk if: no staff_name tag, OR staff_name matches subject,
            # OR subject name appears in the chunk text itself.
            if (
                not chunk_name
                or chunk_name in subject_name
                or subject_name in chunk_name
                or subject_name in chunk_text_lower
            ):
                safe_chunks.append(c)

        chunks = safe_chunks
        logger.info(
            "RAG retrieved %d raw → %d after subject isolation",
            len(raw), len(chunks),
        )
    else:
        logger.info(
            "CAG-only mode: question does not require comparative/policy data"
        )

    prompt = _build_chat_prompt(question, ctx, chunks)
    answer = _call_llm(prompt, max_tokens=CHAT_TOKENS)

    logger.info("Question: %s | RAG used: %s | Chunks: %d", question, rag_used, len(chunks))

    if not answer:
        return {
            "success": False,
            "answer":  "Could not generate a response. "
                       "Check that the model file is present in models/.",
            "chunks":  chunks,
            "rag_used": rag_used,
        }

    return {
        "success":  True,
        "answer":   answer,
        "chunks":   chunks,
        "rag_used": rag_used,
    }


def rag_status() -> dict:
    """
    Health check — used by debug routes.
    [FIX 7] Does NOT trigger a model load — checks _llm/_llm_loaded directly.
    """
    # Inspect state without triggering a load
    llm_ready = _llm_loaded and _llm is not None
    size      = collection_size()
    return {
        "available":   size > 0 and llm_ready,
        "chunk_count": size,
        "llm_ready":   llm_ready,
        "llm_loaded":  _llm_loaded,
        "model":       MODEL_PATH.name,
        "model_path":  str(MODEL_PATH),
        "model_exists": MODEL_PATH.exists(),
        "backend":     "llama-cpp-python (CPU)",
    }


def rag_stream(ctx: dict, mode: str = "short"):
    """
    Streaming RAG generation — yields text tokens as they are produced.
    Used by the /api/ai/stream SSE endpoint.

    Args:
        ctx  : context dict from _build_staff_context / _build_lab_context
        mode : "short"     → 2-paragraph quick summary (~150 tokens)
               "executive" → 4-paragraph formal summary (~500 tokens)

    Yields:
        str tokens as they are generated by the LLM.

    NOTE: RAG chunk retrieval is intentionally DISABLED for streaming summaries.
    The TF-IDF index contains all staff members' data. When chunks for other
    people are injected alongside the subject's context, the model confuses
    names and produces hallucinated summaries. The ctx dict already contains
    all relevant facts for the subject — no retrieval augmentation is needed.

    [FIX 5] GeneratorExit (client disconnects mid-stream) is caught cleanly.
    [FIX 6] _inference_lock is always released in a finally block even if the
            client disconnects, preventing deadlock on subsequent requests.
    """

    # Try composer first — yields instantly, no model loading
    try:
        from rag.composer import compose_staff_summary, compose_lab_summary
        is_lab  = "category" in ctx or "session_reports" in ctx
        summary = compose_lab_summary(ctx) if is_lab else compose_staff_summary(ctx)
        if summary and len(summary.split()) > 20:
            # Simulate streaming by yielding word by word
            # This keeps the SSE stream alive and the UI feel responsive
            words = summary.split(" ")
            for i, word in enumerate(words):
                yield word + (" " if i < len(words) - 1 else "")
            return
    except Exception as e:
        logger.warning("Composer stream failed, falling back to LLM: %s", e)

    # Fall back to GGUF streaming (existing code unchanged below)
    llm = _get_llm()
    if llm is None:
        yield "[ERROR] Model not loaded — check models/ directory."
        return

    # RAG retrieval is DISABLED for streaming to prevent cross-person hallucination.
    # The ctx dict is the sole source of truth for this person's data.
    context_block = _format_context(ctx)

    # Extract the subject's name for explicit prompt anchoring
    subject_name = ctx.get("name", "this person")

    if mode == "short":
        instruction = (
            f"Write a SHORT 2-paragraph summary of {subject_name}'s profile. "
            f"Use ONLY the Personnel Data below. Do not mention or reference any other person. "
            f"Cover role/team and attendance only. Be concise — maximum 120 words."
        )
        max_tokens = 150
    else:  # executive
        instruction = (
            f"Write a formal EXECUTIVE SUMMARY for {subject_name} in exactly 4 paragraphs for senior management. "
            f"Use ONLY the Personnel Data below. Do not mention or reference any other person. "
            f"Paragraph 1: Identity — state {subject_name}'s role, team, qualification, and tenure. "
            f"Paragraph 2: Attendance — state the exact percentage from the data and whether it is above or below the 75% threshold. "
            f"Paragraph 3: Equipment usage and activity — use only numbers from the data. If a value is 0 or absent, state that clearly. Do not invent activity. "
            f"Paragraph 4: Research output — state publications and projects exactly as given. If none, say so. "
            f"End with one sentence overall assessment. Be factual. Do not pad with vague statements."
        )
        max_tokens = 300

    prompt = (
        f"<|im_start|>system\n"
        f"You are an HR analyst for IIT Bombay Nanofabrication Facility (IITBNF). "
        f"Your task is to write a summary ONLY about {subject_name}. "
        f"Use ONLY the numbers and facts in the Personnel Data section. "
        f"Do not fabricate, extrapolate, or reference any other person or dataset.<|im_end|>\n"
        f"<|im_start|>user\n"
        f"{instruction}\n\n"
        f"Personnel Data for {subject_name}:\n---\n{context_block}\n---\n"
        f"Remember: write only about {subject_name}. Use only the data above.<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )

    prompt = _safe_truncate_prompt(prompt, max_tokens)

    # [FIX 2 + FIX 6] Acquire inference lock before streaming; release in finally.
    _inference_lock.acquire()
    try:
        stream = llm(
            prompt,
            max_tokens  = max_tokens,
            temperature = TEMPERATURE,
            stop        = ["<|im_end|>", "<|im_start|>"],
            echo        = False,
            stream      = True,
        )
        for chunk in stream:
            token = chunk["choices"][0].get("text", "")
            if token:
                yield token
    except GeneratorExit:
        # [FIX 5] Client disconnected mid-stream — log and exit cleanly.
        logger.info("rag_stream: client disconnected — GeneratorExit handled cleanly.")
    except Exception as e:
        logger.error("LLM streaming failed: %s", e, exc_info=True)
        yield "[ERROR] Generation failed."
    finally:
        # [FIX 6] Always release the lock — no matter what happened above.
        _inference_lock.release()
def rag_stream_executive(ctx: dict, profile_type: str = "staff"):
    """
    Executive summary streaming.

    1. Runs the composer instantly to get the factual base.
    2. Feeds base + ctx into the LLM prompt.
    3. Streams LLM output token by token.

    Falls back to composer-only output if the LLM is unavailable,
    so the UI always gets something useful.

    Yields str tokens.
    """
    # ── Step 1: get factual base from composer ────────────────────────────
    base_summary = ""
    try:
        from rag.composer import compose_staff_summary, compose_lab_summary
        is_lab       = profile_type == "lab"
        base_summary = (
            compose_lab_summary(ctx) if is_lab
            else compose_staff_summary(ctx)
        )
        logger.info(
            "Executive mode: composer base ready (%d words).",
            len(base_summary.split()),
        )
    except Exception as e:
        logger.warning("Composer failed in executive mode: %s", e)

    # ── Step 2: try LLM ───────────────────────────────────────────────────
    llm = _get_llm()

    if llm is None:
        # LLM not available — stream the composer output with a note
        logger.warning("LLM not available for executive mode — streaming composer output.")
        yield "[NOTE: LLM model not loaded — showing standard summary]\n\n"
        if base_summary:
            for word in base_summary.split(" "):
                yield word + " "
        else:
            yield "Could not generate summary. Check model path and DB connection."
        return

    # ── Step 3: build prompt anchored to composer base ────────────────────
    prompt = _build_executive_prompt(ctx, base_summary, profile_type)
    prompt = _safe_truncate_prompt(prompt, max_tokens=500)

    logger.info("Streaming executive summary for: %s", ctx.get("name", "unknown"))

    # ── Step 4: stream ─────────────────────────────────────────────────────
    _inference_lock.acquire()
    try:
        stream = llm(
            prompt,
            max_tokens  = 500,        # executive = longer
            temperature = 0.4,        # slightly creative but still professional
            top_p       = 0.92,
            repeat_penalty = 1.15,    # discourage looping on short models
            stop        = ["<|im_end|>", "<|im_start|>"],
            echo        = False,
            stream      = True,
        )
        for chunk in stream:
            token = chunk["choices"][0].get("text", "")
            if token:
                yield token

    except GeneratorExit:
        logger.info("rag_stream_executive: client disconnected.")
    except Exception as e:
        logger.error("Executive stream failed: %s", e, exc_info=True)
        # Graceful fallback mid-stream
        yield "\n\n[Generation interrupted — showing base summary]\n\n"
        if base_summary:
            yield base_summary
    finally:
        _inference_lock.release()