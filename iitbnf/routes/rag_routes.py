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
        return redirect(url_for("admin_panel.index"))

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
        return redirect(url_for("admin_panel.index"))

    ctx = _build_lab_context(memberid)
    if not ctx:
        return render_template("not_found.html", member_id=memberid), 404

    return render_template("rag_profile.html",
        ctx         = ctx,
        profile_type= "lab",
        profile_id  = memberid,
        full_access = is_full_access(),
    )

@bp.route("/ai/staff/<int:member_id>")
@login_required
def ai_staff_page(member_id):
    if not is_full_access():
        flash("Access restricted.", "error")
        return redirect(url_for("admin_panel.index"))
    
    ctx = _build_staff_context(member_id)
    if not ctx:
        return render_template("not_found.html", member_id=member_id), 404
    
    from models.staff import get_person, get_available_years, _get_uid_from_member
    person = get_person(member_id)
    
    # Resolve slotbooking uid for year data
    uid = _get_uid_from_member(member_id)
    avail_result = get_available_years(member_id=member_id, memberid=uid)
    avail_years = avail_result[0] if avail_result else [2026]
    if not avail_years:
        avail_years = [2026]
    
    return render_template(
        "ai_page.html",
        ctx=ctx,
        profile_type="staff",
        profile_id=member_id,
        member_id=member_id,
        person=person,
        full_access=is_full_access(),
    )

@bp.route("/ai/lab/<int:memberid>")
@login_required  
def ai_lab_page(memberid):
    if not is_full_access():
        flash("Access restricted.", "error")
        return redirect(url_for("admin_panel.index"))
    
    from models.lab import get_lab_user
    ctx = _build_lab_context(memberid)
    if not ctx:
        return render_template("not_found.html", member_id=memberid), 404
    
    user = get_lab_user(memberid)
    
    # For lab users, pass memberid (slotbooking) not member_id (HR)
    from models.staff import get_available_years
    avail_result = get_available_years(memberid=memberid)
    avail_years = avail_result[0] if avail_result else [2026]
    if not avail_years:
        avail_years = [2026]
    
    return render_template(
        "ai_page.html",
        ctx=ctx,
        profile_type="lab",
        profile_id=memberid,
        member_id=memberid,
        user=user,
        avail_years=avail_years,
        selected_year=avail_years[0],
        full_access=is_full_access(),
    )