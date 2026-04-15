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

import logging
import os
import threading
from pathlib import Path
import platform
from retrieve import retrieve
# [FIX 1] Import collection_size from ingest — the correct implementation.
# The old rag.retrieve.collection_size() called get_index() without unpacking
# the returned 3-tuple, so len() always returned 3 (tuple length) and the
# index was always seen as non-empty even when it was blank.
from ingest import collection_size

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
# [FIX 8] os imported at the top — see module docstring.
MODEL_PATH = Path(__file__).parent.parent / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
RAG_K      = 3
MIN_SCORE  = 0.25

# Generation params
N_CTX       = 4096                  
N_THREADS   = 4 if platform.system() == "Windows" else max(1, os.cpu_count() // 2)
MAX_TOKENS  = 350                     # concise report — faster generation
CHAT_TOKENS = 200                     # concise chat responses
TEMPERATURE = 0.4

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
    """Convert context dict to readable lines for the prompt."""
    lines = []
    for key, val in ctx.items():
        if val is not None and str(val).strip() not in ("N/A", "None", "0", ""):
            label = key.replace("_", " ").title()
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
    rag_block     = _format_chunks(chunks)

    if audience == "individual":
        tone    = "Write in second person (use 'you'). Be encouraging and constructive. Acknowledge strengths before areas for improvement."
        closing = "End with a brief forward-looking sentence about growth or contribution."
    else:
        tone    = "Write in third person. Be factual, concise, and professional. Suitable for a supervisor or HR manager."
        closing = "End with a brief overall assessment suitable for a performance review."

    rag_section = ""
    if rag_block:
        rag_section = (
            "\nReference Data (retrieved from IITBNF records — use to ground your commentary):\n"
            "---\n"
            f"{rag_block}\n"
            "---\n"
        )

    return (
        f"<|im_start|>system\n"
        f"You are an HR analyst for IIT Bombay Nanofabrication Facility (IITBNF). "
        f"{tone} Do not fabricate numbers. Use only the actual values from the data provided. "
        f"{closing}<|im_end|>\n"
        f"<|im_start|>user\n"
        f"Generate a concise profile report (3-5 paragraphs). Cover only sections where data "
        f"is available: attendance and leave, equipment usage, research output.\n"
        f"{rag_section}"
        f"Personnel Data:\n---\n{context_block}\n---<|im_end|>\n"
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
        f"If the answer cannot be determined, say so clearly.<|im_end|>\n"
        f"<|im_start|>user\n"
        f"{rag_section}"
        f"Personnel Data:\n---\n{context_block}\n---\n"
        f"Question: {question}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def rag_generate(ctx: dict, audience: str = "management") -> str:
    """
    Full RAG report generation pipeline.
    Retrieves relevant chunks, injects into prompt, calls local LLM.
    Returns generated narrative string, or "" on failure.

    Args:
        ctx      : context dict from _build_staff_context / _build_lab_context
        audience : "management" or "individual"
    """
    chunks = []
    if collection_size() > 0:
        query  = _build_report_query(ctx)
        raw    = retrieve(query, k=RAG_K)
        chunks = [c for c in raw if c.get("score", 0) >= MIN_SCORE]

    prompt = _build_report_prompt(ctx, audience, chunks)
    return _call_llm(prompt, max_tokens=MAX_TOKENS)


def rag_chat(question: str, ctx: dict) -> dict:
    """
    RAG chat pipeline for the /api/rag/chat endpoint.

    Args:
        question : user's natural language question
        ctx      : context dict for this person (staff or lab)

    Returns:
        {
            "answer":  str,
            "chunks":  [{"text": str, "source": str, "score": float}, ...],
            "success": bool
        }
    """
    if not question or not question.strip():
        return {"success": False, "answer": "Please enter a question.", "chunks": []}

    chunks = []
    if collection_size() > 0:
        raw    = retrieve(question, k=RAG_K)
        chunks = [c for c in raw if c.get("score", 0) >= MIN_SCORE]

    prompt = _build_chat_prompt(question, ctx, chunks)
    answer = _call_llm(prompt, max_tokens=CHAT_TOKENS)

    if not answer:
        return {
            "success": False,
            "answer":  "Could not generate a response. Check that the model file is present in models/.",
            "chunks":  chunks,
        }

    return {
        "success": True,
        "answer":  answer,
        "chunks":  chunks,
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

    [FIX 5] GeneratorExit (client disconnects mid-stream) is caught cleanly.
    [FIX 6] _inference_lock is always released in a finally block even if the
            client disconnects, preventing deadlock on subsequent requests.
    """
    llm = _get_llm()
    if llm is None:
        yield "[ERROR] Model not loaded — check models/ directory."
        return

    # Build RAG context
    chunks = []
    if collection_size() > 0:
        query  = _build_report_query(ctx)
        raw    = retrieve(query, k=RAG_K)
        chunks = [c for c in raw if c.get("score", 0) >= MIN_SCORE]

    context_block = _format_context(ctx)
    rag_block     = _format_chunks(chunks)
    rag_section   = (
        "\nReference Data:\n---\n" + rag_block + "\n---\n"
    ) if rag_block else ""

    if mode == "short":
        instruction = (
            "Write a SHORT 2-paragraph summary of this person's profile. "
            "Cover attendance and key activity only. Be concise — maximum 120 words."
        )
        max_tokens = 150
    else:  # executive
        instruction = (
            "Write a formal EXECUTIVE SUMMARY in exactly 4 paragraphs for senior management. "
            "Paragraph 1: Identity, role, team, qualification, tenure. "
            "Paragraph 2: Attendance — state the exact percentage and whether it is above or below the 75% threshold. "
            "Paragraph 3: Equipment usage and activity — if no data exists, state that clearly. Do not invent activity. "
            "Paragraph 4: Research output — state publications and projects exactly. If none, say so. "
            "End with one sentence overall assessment. Be factual. Do not pad with vague statements."
        )
        max_tokens = 300

    prompt = (
        f"<|im_start|>system\n"
        f"You are an HR analyst for IIT Bombay Nanofabrication Facility (IITBNF). "
        f"Do not fabricate numbers. Use only actual values from the data.<|im_end|>\n"
        f"<|im_start|>user\n"
        f"{instruction}\n"
        f"{rag_section}"
        f"Personnel Data:\n---\n{context_block}\n---<|im_end|>\n"
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