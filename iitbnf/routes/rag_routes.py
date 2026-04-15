"""
routes/rag_routes.py — RAG profile pages and chat API

Routes:
    GET  /rag/staff/<member_id>  — staff RAG page
    GET  /rag/lab/<memberid>     — lab RAG page
    POST /api/rag/chat           — chat endpoint (JSON)

Access: full_access gate on all routes.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash
from auth import login_required, is_full_access
from models.ai import _build_staff_context, _build_lab_context

bp = Blueprint("rag", __name__)


# ── Staff RAG page ─────────────────────────────────────────────────────────────

@bp.route("/rag/staff/<int:member_id>")
@login_required
def rag_staff(member_id):
    if not is_full_access():
        flash("Access restricted.", "error")
        return redirect(url_for("dashboard.dashboard"))

    ctx = _build_staff_context(member_id)
    if not ctx:
        return render_template("not_found.html", member_id=member_id), 404


    return render_template("rag_profile.html",
        ctx         = ctx,
        profile_type= "staff",
        profile_id  = member_id,
        full_access = is_full_access(),
    )


# ── Lab RAG page ───────────────────────────────────────────────────────────────

@bp.route("/rag/lab/<int:memberid>")
@login_required
def rag_lab(memberid):
    if not is_full_access():
        flash("Access restricted.", "error")
        return redirect(url_for("dashboard.dashboard"))

    ctx = _build_lab_context(memberid)
    if not ctx:
        return render_template("not_found.html", member_id=memberid), 404

    return render_template("rag_profile.html",
        ctx         = ctx,
        profile_type= "lab",
        profile_id  = memberid,
        full_access = is_full_access(),
    )
