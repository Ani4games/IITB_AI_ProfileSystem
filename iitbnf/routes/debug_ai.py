"""
routes/debug_ai.py — AI Summarizer Diagnostics
===============================================
Step-by-step debug endpoints for the AI summarizer pipeline.
Shows exactly what happens at each stage for staff vs lab profiles.

Routes:
    GET /debug/ai/context/<profile_type>/<id>   — show raw context dict
    GET /debug/ai/query/<profile_type>/<id>     — show retrieval query + RAG chunks
    GET /debug/ai/prompt/<profile_type>/<id>    — show full prompt sent to LLM
    GET /debug/ai/full/<profile_type>/<id>      — run full pipeline, show all steps

Usage:
    /debug/ai/full/staff/888
    /debug/ai/full/lab/2524

Access: full_access only. Remove this blueprint before production deployment.
"""

import json
import time
from flask import Blueprint, jsonify, request
from auth import login_required, is_full_access
from models.ai import _build_staff_context, _build_lab_context
import time
from models.staff import (
        _get_uid_from_member, get_person, get_attendance_stats,
        get_equipment_stats, get_staff_reservations,
        get_staff_system_owned, get_staff_owner_track,
        get_staff_tool_perms_rich
    )
from rag.retrieve import retrieve, collection_size, WORD_VEC_BACKEND, DEFAULT_K
from rag.pipeline import (
        _build_report_query, _format_context, _format_chunks,
        RAG_K, MIN_SCORE, N_CTX
    )
bp = Blueprint("debug_ai", __name__)


def _get_ctx(profile_type: str, profile_id: int):
    """Build context dict for either staff or lab."""
    if profile_type == "lab":
        return _build_lab_context(profile_id)
    return _build_staff_context(profile_id)


# ── Step 1: Context ───────────────────────────────────────────────────────────

@bp.route("/debug/ai/context/<profile_type>/<int:profile_id>")
@login_required
def debug_context(profile_type, profile_id):
    """Shows the raw context dict built from the database."""
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403

    t0  = time.perf_counter()
    ctx = _get_ctx(profile_type, profile_id)
    ms  = round((time.perf_counter() - t0) * 1000, 2)

    if not ctx:
        return jsonify({
            "step":    "1 — Context Build",
            "status":  "FAILED",
            "reason":  "No data returned — member may not exist, may have taken_clearance=1, or DB query failed.",
            "profile_type": profile_type,
            "profile_id":   profile_id,
        }), 404

    return jsonify({
        "step":         "1 — Context Build",
        "status":       "OK",
        "elapsed_ms":   ms,
        "profile_type": profile_type,
        "profile_id":   profile_id,
        "field_count":  len(ctx),
        "context":      {k: str(v) for k, v in ctx.items()},
        "empty_fields": [k for k, v in ctx.items() if not v or str(v) in ("N/A", "0", "None", "")],
    })


# ── Step 2: RAG Query + Retrieval ─────────────────────────────────────────────

@bp.route("/debug/ai/query/<profile_type>/<int:profile_id>")
@login_required
def debug_query(profile_type, profile_id):
    """Shows the retrieval query and retrieved RAG chunks."""
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403

    ctx = _get_ctx(profile_type, profile_id)
    if not ctx:
        return jsonify({"step": "2 — RAG Query", "status": "FAILED", "reason": "Context build failed."}), 404


    query      = _build_report_query(ctx)
    index_size = collection_size()

    t0     = time.perf_counter()
    raw    = retrieve(
    query,
    k=DEFAULT_K,
    backend=WORD_VEC_BACKEND,
    allowed_types=None,
    requested_name=None,
    requested_id=None,
    requested_year=None,
) if index_size > 0 else []
    ms     = round((time.perf_counter() - t0) * 1000, 2)
    chunks = [c for c in raw if c.get("score", 0) >= MIN_SCORE]

    return jsonify({
        "step":            "2 — RAG Query & Retrieval",
        "status":          "OK" if query else "WARNING — empty query",
        "query":           query,
        "query_empty":     not bool(query.strip()),
        "index_size":      index_size,
        "elapsed_ms":      ms,
        "chunks_retrieved": len(raw),
        "chunks_above_threshold": len(chunks),
        "min_score":       MIN_SCORE,
        "chunks": [
            {
                "rank":   i + 1,
                "source": c.get("source"),
                "score":  round(c.get("score", 0), 4),
                "text":   c.get("text", "")[:200] + "…"
            }
            for i, c in enumerate(raw)
        ],
    })


# ── Step 3: Prompt ────────────────────────────────────────────────────────────

@bp.route("/debug/ai/prompt/<profile_type>/<int:profile_id>")
@login_required
def debug_prompt(profile_type, profile_id):
    """Shows the full prompt that would be sent to the LLM."""
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403

    mode = request.args.get("mode", "short")
    ctx  = _get_ctx(profile_type, profile_id)
    if not ctx:
        return jsonify({"step": "3 — Prompt Build", "status": "FAILED", "reason": "Context build failed."}), 404

    query  = _build_report_query(ctx)
    raw    = retrieve(
    query,
    k=DEFAULT_K,
    backend=WORD_VEC_BACKEND,
    allowed_types=None,
    requested_name=None,
    requested_id=None,
    requested_year=None,
) if collection_size() > 0 else []
    chunks = [c for c in raw if c.get("score", 0) >= MIN_SCORE]

    context_block = _format_context(ctx)
    rag_block     = _format_chunks(chunks)
    rag_section   = f"\nReference Data:\n---\n{rag_block}\n---\n" if rag_block else ""

    if mode == "short":
        instruction = (
            "Write a SHORT 2-paragraph summary of this person's profile. "
            "Cover attendance and key activity only. Be concise — maximum 120 words."
        )
        max_tokens = 150
    else:
        instruction = (
            "Write a formal EXECUTIVE SUMMARY in exactly 4 paragraphs for senior management. "
            "Paragraph 1: Identity, role, team, qualification, tenure. "
            "Paragraph 2: Attendance — state the exact percentage and whether it is above or below the 75% threshold. "
            "Paragraph 3: Equipment usage and activity — if no data exists, state that clearly. Do not invent activity. "
            "Paragraph 4: Research output — state publications and projects exactly. If none, say so. "
            "End with one sentence overall assessment. Be factual. Do not pad with vague statements."
        )
        max_tokens = 500

    prompt = f"""<|im_start|>system
You are an HR analyst for IIT Bombay Nanofabrication Facility (IITBNF). Do not fabricate numbers. Use only actual values from the data.<|im_end|>
<|im_start|>user
{instruction}
{rag_section}
Personnel Data:
---
{context_block}
---<|im_end|>
<|im_start|>assistant
"""

    estimated_tokens = len(prompt) // 4
    fits_in_context  = (estimated_tokens + max_tokens) <= N_CTX

    return jsonify({
        "step":                 "3 — Prompt Build",
        "status":               "OK" if fits_in_context else "WARNING — prompt may be truncated",
        "mode":                 mode,
        "estimated_prompt_tokens": estimated_tokens,
        "max_tokens":           max_tokens,
        "n_ctx":                N_CTX,
        "fits_in_context":      fits_in_context,
        "prompt_char_length":   len(prompt),
        "context_block_chars":  len(context_block),
        "rag_block_chars":      len(rag_block),
        "prompt":               prompt,
    })


# ── Step 4: Full pipeline run ─────────────────────────────────────────────────

@bp.route("/debug/ai/full/<profile_type>/<int:profile_id>")
@login_required
def debug_full(profile_type, profile_id):
    """
    Runs the full AI summarizer pipeline step by step and reports
    exactly where it succeeds or fails.
    """
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403

    mode   = request.args.get("mode", "short")
    report = {"profile_type": profile_type, "profile_id": profile_id, "mode": mode, "steps": []}

    # ── Step 1: Context ───────────────────────────────────────────
    t0  = time.perf_counter()
    ctx = _get_ctx(profile_type, profile_id)
    ms  = round((time.perf_counter() - t0) * 1000, 2)

    if not ctx:
        report["steps"].append({
            "step": "1 — Context Build", "status": "FAILED", "elapsed_ms": ms,
            "reason": "No data returned. Check: member exists, taken_clearance=0, DB connection."
        })
        report["verdict"] = "FAILED at Step 1"
        return jsonify(report), 404

    report["steps"].append({
        "step": "1 — Context Build", "status": "OK", "elapsed_ms": ms,
        "field_count": len(ctx),
        "name": ctx.get("name", "MISSING"),
        "has_attendance": "attendance_pct" in ctx,
        "attendance_pct": ctx.get("attendance_pct"),
        "empty_fields": [k for k, v in ctx.items() if not v or str(v) in ("N/A", "0", "None", "")],
    })

    # ── Step 2: RAG query ─────────────────────────────────────────
    from rag.pipeline import _build_report_query, RAG_K, MIN_SCORE, N_CTX
    

    query      = _build_report_query(ctx)
    index_size = collection_size()

    t0     = time.perf_counter()
    raw    = retrieve(
    query,
    k=DEFAULT_K,
    backend=WORD_VEC_BACKEND,
    allowed_types=None,
    requested_name=None,
    requested_id=None,
    requested_year=None,
) if index_size > 0 else []
    ms     = round((time.perf_counter() - t0) * 1000, 2)
    chunks = [c for c in raw if c.get("score", 0) >= MIN_SCORE]

    report["steps"].append({
        "step": "2 — RAG Retrieval", "status": "OK" if query else "WARNING",
        "elapsed_ms": ms,
        "query": query,
        "query_empty": not bool(query.strip()),
        "index_size": index_size,
        "chunks_retrieved": len(raw),
        "chunks_above_threshold": len(chunks),
    })

    # ── Step 3: Prompt ─────────────────────────────────────────────
    from rag.pipeline import _format_context, _format_chunks

    context_block    = _format_context(ctx)
    rag_block        = _format_chunks(chunks)
    estimated_tokens = (len(context_block) + len(rag_block)) // 4
    max_tokens       = 150 if mode == "short" else 500
    fits             = (estimated_tokens + max_tokens) <= N_CTX

    report["steps"].append({
        "step": "3 — Prompt Build", "status": "OK" if fits else "WARNING — will truncate",
        "estimated_prompt_tokens": estimated_tokens,
        "max_tokens": max_tokens,
        "n_ctx": N_CTX,
        "fits_in_context": fits,
        "context_block_chars": len(context_block),
        "rag_block_chars": len(rag_block),
    })


    t0 = time.perf_counter()
    try:
        from rag.pipeline import rag_stream
        tokens = []
        for token in rag_stream(ctx, mode=mode):
            tokens.append(token)
        output = "".join(tokens)
        ms     = round((time.perf_counter() - t0) * 1000, 2)

        report["steps"].append({
            "step": "4 — LLM Inference", "status": "OK", "elapsed_ms": ms,
            "token_count": len(tokens),
            "output_chars": len(output),
            "output_preview": output[:300] + ("…" if len(output) > 300 else ""),
        })
        report["verdict"] = "SUCCESS"
        report["output"]  = output

    except Exception as e:
        ms = round((time.perf_counter() - t0) * 1000, 2)
        report["steps"].append({
            "step": "4 — LLM Inference", "status": "FAILED",
            "elapsed_ms": ms, "error": str(e),
        })
        report["verdict"] = f"FAILED at Step 4 — {str(e)}"

    return jsonify(report)


# ── Section data debug ────────────────────────────────────────────────────────

@bp.route("/debug/staff/<int:member_id>")
@login_required
def debug_staff(member_id):
    """
    Shows exactly what data is being returned for each section
    of a staff profile, including uid resolution status.
    Usage: /debug/staff/189
    """
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403

    year   = request.args.get("year", type=int) or __import__('datetime').date.today().year
    report = {"member_id": member_id, "year": year, "sections": {}}

    # ── UID resolution ────────────────────────────────────────────────────────
    t0  = time.perf_counter()
    uid = _get_uid_from_member(member_id)
    ms  = round((time.perf_counter() - t0) * 1000, 2)
    report["uid_resolution"] = {
        "slot_memberid": uid,
        "resolved": uid is not None,
        "elapsed_ms": ms,
    }

    # ── HR sections (no uid needed) ───────────────────────────────────────────
    def check(label, fn, *args):
        try:
            t0  = time.perf_counter()
            res = fn(*args)
            ms  = round((time.perf_counter() - t0) * 1000, 2)
            count = len(res) if isinstance(res, list) else (1 if res else 0)
            return {"status": "OK", "count": count, "elapsed_ms": ms,
                    "sample": str(res[0])[:120] if isinstance(res, list) and res else str(res)[:120] if res else None}
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    report["sections"]["person"]          = check("person",    get_person,           member_id)
    report["sections"]["attendance"]      = check("attendance",get_attendance_stats,  member_id, year)
    report["sections"]["equipment"]       = check("equipment",  get_equipment_stats,   member_id, year)
    report["sections"]["reservations"]    = check("reservations", get_staff_reservations, member_id, year)
    report["sections"]["system_owned"]    = check("system_owned", get_staff_system_owned, member_id)
    report["sections"]["owner_track"]     = check("owner_track", get_staff_owner_track, member_id)
    report["sections"]["tool_perms_rich"] = check("tool_perms", get_staff_tool_perms_rich, member_id)

    return jsonify(report)

@bp.route("/debug/lab/<int:memberid>")
@login_required
def debug_lab(memberid):
    """
    Shows exactly what data is returned for each section of a lab profile.
    Usage: /debug/lab/2506
    """
    if not is_full_access():
        return jsonify({"error": "Access restricted."}), 403

    import time
    from models.lab import (
        get_lab_user, get_lab_stats, get_lab_reservations,
        get_lab_equipment_requests, get_lab_access_log,
        get_lab_cancellations, get_lab_errors, get_lab_registration,
        get_session_reports, get_member_tool_permissions,
        get_system_owner_tools, get_system_owner_track
    )

    year   = request.args.get("year", type=int) or __import__('datetime').date.today().year
    report = {"memberid": memberid, "year": year, "sections": {}}

    def check(label, fn, *args):
        try:
            t0  = time.perf_counter()
            res = fn(*args)
            ms  = round((time.perf_counter() - t0) * 1000, 2)
            count = len(res) if isinstance(res, (list, tuple)) else (1 if res else 0)
            sample = None
            if isinstance(res, (list, tuple)) and res:
                sample = str(res[0])[:150]
            elif isinstance(res, dict) and res:
                sample = str(res)[:150]
            return {"status": "OK", "count": count, "elapsed_ms": ms, "sample": sample}
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    report["sections"]["user"]          = check("user",         get_lab_user,              memberid)
    report["sections"]["stats"]         = check("stats",        get_lab_stats,             memberid)
    report["sections"]["reservations"]  = check("reservations", get_lab_reservations,      memberid, year)
    report["sections"]["requests"]      = check("requests",     get_lab_equipment_requests,memberid, year)
    report["sections"]["lab_access"]    = check("lab_access",   get_lab_access_log,        memberid, year)
    report["sections"]["cancellations"] = check("cancellations",get_lab_cancellations,     memberid)
    report["sections"]["session_reports"]= check("sessions",    get_session_reports,       memberid)
    report["sections"]["tool_perms_rich"]= check("perms_rich",  get_member_tool_permissions, memberid)
    report["sections"]["system_owned"]  = check("system_owned", get_system_owner_tools,    memberid)
    report["sections"]["owner_track"]   = check("owner_track",  get_system_owner_track,    memberid)
    report["sections"]["registration"]  = check("registration", get_lab_registration,      memberid)
    report["sections"]["errors"]        = check("errors",       get_lab_errors,            memberid)

    return jsonify(report)

