"""
routes/ai_routes.py — AI report endpoints.

Routes:
    POST /api/ai/report   — full report generation (non-streaming)
    GET  /api/ai/stream   — streaming SSE endpoint for floating chat bubble
"""

import json
import queue
import threading
from flask import Blueprint, request, jsonify, Response, stream_with_context
from auth import login_required, is_full_access
from models.ai import generate_llm_report, _build_staff_context, _build_lab_context

bp = Blueprint("ai", __name__)


@bp.route("/api/ai/report", methods=["POST"])
@login_required
def ai_report():
    if not is_full_access():
        return jsonify({"success": False, "error": "Access restricted."}), 403

    data         = request.get_json(silent=True) or {}
    profile_type = data.get("profile_type", "staff")
    profile_id   = data.get("profile_id")
    audience     = data.get("audience", "management")

    if not profile_id:
        return jsonify({"success": False, "error": "No profile ID provided."}), 400

    try:
        result = generate_llm_report(
            profile_type = profile_type,
            profile_id   = int(profile_id),
            audience     = audience,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ai/stream")
@login_required
def ai_stream():
    """
    Server-Sent Events endpoint for streaming AI summaries.
    Uses a background thread + queue so the SSE connection never
    blocks — a heartbeat keeps the connection alive while the
    model is loading the prompt into context.

    Query params:
        profile_type : "staff" or "lab"
        profile_id   : member ID
        message      : user's natural language request (NEW — replaces mode)
                       e.g. "give me a quick summary" / "executive briefing"
                       Falls back to ?mode=short if message is absent.
    """
    if not is_full_access():
        return Response("data: [ERROR] Access restricted.\n\n",
                        mimetype="text/event-stream"), 403

    profile_type = request.args.get("profile_type", "staff")
    profile_id   = request.args.get("profile_id",   type=int)
    message      = request.args.get("message",      "").strip()

    # ── Fallback: honour legacy ?mode= param if no message provided ──────────
    legacy_mode  = request.args.get("mode", "short")

    if not profile_id:
        return Response("data: [ERROR] No profile ID.\n\n",
                        mimetype="text/event-stream"), 400

    # Build context before entering the stream — fast DB call
    if profile_type == "lab":
        ctx = _build_lab_context(profile_id)
    else:
        ctx = _build_staff_context(profile_id)

    if not ctx:
        return Response("data: [ERROR] Could not load profile data.\n\n",
                        mimetype="text/event-stream"), 404

    # Queue for passing tokens from inference thread to SSE generator
    token_queue = queue.Queue()
    SENTINEL    = object()   # signals end of stream

    def inference_worker():
        """Runs in background thread — pushes tokens into the queue."""
        try:
            if message:
                # ── NEW: autonomous mode — agent detects intent from message ─
                from rag.agent import agent_stream
                for token in agent_stream(message, ctx):
                    token_queue.put(token)
            else:
                # ── LEGACY: mode passed directly from frontend ────────────────
                from rag.pipeline import rag_stream
                for token in rag_stream(ctx, mode=legacy_mode):
                    token_queue.put(token)
        except Exception as e:
            token_queue.put(f"[ERROR] {str(e)}")
        finally:
            token_queue.put(SENTINEL)

    # Start inference in background
    t = threading.Thread(target=inference_worker, daemon=True)
    t.start()

    def generate():
        """SSE generator — sends heartbeats while waiting for tokens."""
        try:
            while True:
                try:
                    # Wait up to 1 second for a token — send heartbeat if none
                    item = token_queue.get(timeout=1.0)
                except queue.Empty:
                    # SSE comment — keeps connection alive, invisible to client
                    yield ": heartbeat\n\n"
                    continue

                if item is SENTINEL:
                    yield "data: [DONE]\n\n"
                    break
                elif isinstance(item, str) and item.startswith("[ERROR]"):
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                else:
                    yield f"data: {json.dumps(item)}\n\n"
        except GeneratorExit:
            # Client disconnected — nothing to do, thread will finish naturally
            pass

    return Response(
        stream_with_context(generate()),
        mimetype = "text/event-stream",
        headers  = {
            "Cache-Control":      "no-cache",
            "X-Accel-Buffering":  "no",    # disable nginx buffering if behind proxy
            # "Transfer-Encoding": "chunked",   # add this line to ensure proper streaming
            # "connection":       "keep-alive", ensure connection stays open
        }
    )
@bp.route("/api/ai/compose")
@login_required
def ai_compose():
    """
    SSE endpoint for the AI Profile tab.
    
    Query params:
        profile_type : "staff" or "lab"
        profile_id   : member ID
        mode         : "short" (default) | "executive"
    """
    if not is_full_access():
        return Response("data: [ERROR] Access restricted.\n\n",
                        mimetype="text/event-stream"), 403

    profile_type = request.args.get("profile_type", "staff")
    profile_id   = request.args.get("profile_id", type=int)
    mode         = request.args.get("mode", "short")

    if not profile_id:
        return Response("data: [ERROR] No profile ID.\n\n",
                        mimetype="text/event-stream"), 400

    if profile_type == "lab":
        ctx = _build_lab_context(profile_id)
    else:
        ctx = _build_staff_context(profile_id)

    if not ctx:
        return Response("data: [ERROR] Could not load profile data.\n\n",
                        mimetype="text/event-stream"), 404

    # ── SHORT mode: composer only, instant ────────────────────────────────
    if mode == "short":
        def generate_short():
            try:
                from rag.composer import compose_staff_summary, compose_lab_summary
                is_lab  = profile_type == "lab"
                summary = (
                    compose_lab_summary(ctx) if is_lab
                    else compose_staff_summary(ctx)
                )
                if not summary:
                    yield "data: [ERROR] Could not generate summary.\n\n"
                    return
                import json
                yield f"data: {json.dumps({'type': 'text', 'content': summary})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: [ERROR] {str(e)}\n\n"

        return Response(
            stream_with_context(generate_short()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
                # "Connection":        "keep-alive",
            }
        )

    # ── EXECUTIVE mode: composer + LLM, streamed ──────────────────────────
    token_queue = queue.Queue()
    SENTINEL    = object()

    def inference_worker():
        try:
            from rag.pipeline import rag_stream_executive
            for token in rag_stream_executive(ctx, profile_type):
                token_queue.put(token)
        except Exception as e:
            token_queue.put(f"[ERROR] {str(e)}")
        finally:
            token_queue.put(SENTINEL)

    t = threading.Thread(target=inference_worker, daemon=True)
    t.start()

    def generate_executive():
        import json
        try:
            while True:
                try:
                    item = token_queue.get(timeout=1.0)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue

                if item is SENTINEL:
                    yield "data: [DONE]\n\n"
                    break
                elif isinstance(item, str) and item.startswith("[ERROR]"):
                    yield f"data: {json.dumps({'type': 'error', 'content': item})}\n\n"
                    break
                else:
                    # Stream tokens individually so typewriter works
                    yield f"data: {item}\n\n"
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate_executive()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            # "Connection":        "keep-alive",
        }
    )

@bp.route("/api/ai/session-digest")
@login_required
def ai_session_digest():
    """
    SSE endpoint — streams a 3-bullet digest of session reports for one tool.

    Query params:
        machid    : int  — the tool's machine ID
        tool_name : str  — display name (used in the prompt)

    Yields a [META] event first with report counts, then token stream.
    """
    if not is_full_access():
        return Response("data: [ERROR] Access restricted.\n\n",
                        mimetype="text/event-stream"), 403

    machid    = request.args.get("machid", type=int)
    tool_name = request.args.get("tool_name", "").strip() or "this tool"

    if not machid:
        return Response("data: [ERROR] machid required.\n\n",
                        mimetype="text/event-stream"), 400

    from db import slots_query

    rows = slots_query("""
        SELECT rp.report_details,
               FROM_UNIXTIME(rp.datetime) AS submitted_at
        FROM reporting rp
        WHERE rp.machid = %s
          AND rp.report_details IS NOT NULL
          AND TRIM(rp.report_details) != ''
        ORDER BY rp.datetime DESC
        LIMIT 50
    """, (machid,)) or []

    total  = len(rows)
    useful = sum(1 for r in rows)

    token_queue = queue.Queue()
    SENTINEL    = object()

    def inference_worker():
        try:
            from rag.pipeline import digest_session_reports_stream
            for token in digest_session_reports_stream(tool_name, rows):
                token_queue.put(token)
        except Exception as e:
            token_queue.put(f"[ERROR] {str(e)}")
        finally:
            token_queue.put(SENTINEL)

    threading.Thread(target=inference_worker, daemon=True).start()

    def generate():
        # First event: metadata so the UI can show accurate counts
        meta = json.dumps({"type": "meta", "total": total, "useful": useful})
        yield f"data: {meta}\n\n"
        try:
            while True:
                try:
                    item = token_queue.get(timeout=1.0)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if item is SENTINEL:
                    yield "data: [DONE]\n\n"
                    break
                elif isinstance(item, str) and item.startswith("[ERROR]"):
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                else:
                    yield f"data: {json.dumps(item)}\n\n"
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            # "Connection":        "keep-alive",
        }
    )
@bp.route("/api/ai/admin-chat")
@login_required
def admin_chat():
    """
    SSE endpoint for admin panel Q&A.
    No profile context — general facility questions, member lookups, policy queries.
    Query params:
        message: str
    """
    if not is_full_access():
        return Response("data: [ERROR] Access restricted.\n\n",
                        mimetype="text/event-stream"), 403

    message = request.args.get("message", "").strip()
    if not message:
        return Response("data: [ERROR] No message.\n\n",
                        mimetype="text/event-stream"), 400

    # Build a minimal context: facility summary only, no personal data
    ctx = {
        "facility": "IIT Bombay Nanofabrication Facility (IITBNF)",
        "system":   "Personnel and lab equipment management system",
        "location":     "IIT Bombay, Powai, Mumbai - 400076",
        "type":         "Class 100/1000 Cleanroom",
        "hours":        "Monday to Friday, 9:00 AM to 6:00 PM",
        "slot_uid":     None,
        "member_id":    None,
        "name":         "Facility",
    }

    token_queue = queue.Queue()
    SENTINEL    = object()

    def worker():
        try:
            from rag.agent import agent_stream
            for token in agent_stream(message, ctx):
                token_queue.put(token)
        except Exception as e:
            token_queue.put(f"[ERROR] {e}")
        finally:
            token_queue.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        try:
            while True:
                try:
                    item = token_queue.get(timeout=1.0)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if item is SENTINEL:
                    yield "data: [DONE]\n\n"
                    break
                elif isinstance(item, str) and item.startswith("[ERROR]"):
                    yield f"data: {json.dumps(item)}\n\n"
                    break
                else:
                    yield f"data: {json.dumps(item)}\n\n"
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
@bp.route("/api/ai/logbook-explain")
@login_required
def logbook_explain():
    """
    SSE: explains logbook entries for a staff member on a specific tool.
    Query params:
        member_id : int
        machid    : int
        tool_name : str
    """
    if not is_full_access():
        return Response("data: [ERROR] Access restricted.\n\n",
                        mimetype="text/event-stream"), 403

    member_id = request.args.get("member_id", type=int)
    machid    = request.args.get("machid",    type=int)
    tool_name = request.args.get("tool_name", "").strip() or "this tool"

    if not member_id or not machid:
        return Response("data: [ERROR] member_id and machid required.\n\n",
                        mimetype="text/event-stream"), 400

    from db import slots_query
    from models.staff import _get_uid_from_member

    uid = _get_uid_from_member(member_id)
    if not uid:
        return Response("data: [ERROR] Could not resolve member.\n\n",
                        mimetype="text/event-stream"), 404

    # Fetch logbook rows for this member + tool
    rows = slots_query(f"""
        SELECT lg.*, FROM_UNIXTIME(res.startdate) AS booking_start,
               FROM_UNIXTIME(res.enddate) AS booking_end
        FROM `t_{machid}` lg
        JOIN reservations res ON res.resid = lg.reservation_id
        WHERE res.memberid = %s
        ORDER BY lg.reservation_id DESC
        LIMIT 30
    """, (uid,)) or []

    if not rows:
        return Response("data: No logbook entries found for this member on this tool.\ndata: [DONE]\n\n",
                        mimetype="text/event-stream")

    # Format rows as readable text
    from datetime import datetime, date
    from decimal import Decimal
    def _safe(v):
        if v is None: return "—"
        if isinstance(v, (datetime, date)): return str(v)
        if isinstance(v, Decimal): return str(float(v))
        return str(v)

    formatted_rows = []
    for r in rows:
        parts = [f"{k}: {_safe(v)}" for k, v in r.items()
                 if k not in ("reservation_id",) and v is not None]
        formatted_rows.append("Entry: " + " | ".join(parts))

    token_queue = queue.Queue()
    SENTINEL    = object()

    def worker():
        try:
            from rag.pipeline import _call_ollama_stream, _build_executive_prompt
            prompt = (
                f"You are summarising instrument logbook entries for {tool_name} "
                f"at IIT Bombay Nanofabrication Facility for a specific user.\n\n"
                f"Logbook entries:\n" + "\n".join(formatted_rows[:20]) + "\n\n"
                f"Write 3-4 concise bullet points explaining: what process parameters "
                f"were used, any patterns or anomalies, and overall usage quality. "
                f"Be factual. Use plain text. No preamble."
            )
            for token in _call_ollama_stream(prompt, max_tokens=300):
                token_queue.put(token)
        except Exception as e:
            token_queue.put(f"[ERROR] {e}")
        finally:
            token_queue.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        try:
            while True:
                try:
                    item = token_queue.get(timeout=1.0)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if item is SENTINEL:
                    yield "data: [DONE]\n\n"
                    break
                else:
                    yield f"data: {json.dumps(item)}\n\n"
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )