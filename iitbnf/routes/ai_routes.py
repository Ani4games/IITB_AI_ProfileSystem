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
            "Connection":         "keep-alive",
        }
    )
