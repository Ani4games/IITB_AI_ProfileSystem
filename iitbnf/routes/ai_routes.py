"""
routes/ai_routes.py
--------------------
POST /api/report  —  generate an on-demand written profile report via Ollama.

Expected JSON body:
    {
        "member_id": 123,           # for staff  (hr_portal)   — OR —
        "memberid":  456,           # for lab users (slotbooking)
        "audience":  "individual" | "management"
    }

Provide either member_id (staff) or memberid (lab user), not both.
Variable names match template usage:
  profile.html     → member_id
  lab_profile.html → memberid
"""

from flask import Blueprint, request, jsonify
from models.ai import generate_staff_report, generate_lab_report
from auth import login_required

bp = Blueprint("ai", __name__)


@bp.route("/api/report", methods=["POST"])
@login_required
def generate_report():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid request body."}), 400

    audience = data.get("audience", "management")
    if audience not in ("individual", "management"):
        audience = "management"

    member_id = data.get("member_id")   # staff
    memberid  = data.get("memberid")    # lab user

    if not member_id and not memberid:
        return jsonify({"success": False, "error": "Provide either member_id (staff) or memberid (lab user)."}), 400

    if member_id:
        result = generate_staff_report(
            member_id = int(member_id),
            audience  = audience
        )
    else:
        result = generate_lab_report(
            memberid = int(memberid),
            audience = audience
        )

    return jsonify(result)
