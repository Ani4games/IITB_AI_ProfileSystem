"""
routes/admin.py — /admin and all staff/lab CRUD routes.
"""
import traceback
from flask import Blueprint, render_template, request, redirect, url_for, flash
from auth import staff_required
from cache import cache
from db import hr_execute, slots_execute
from utils import run_parallel
from models.staff import get_all_members
from models.lab import get_all_lab_users, get_announcements_all

bp = Blueprint("admin", __name__)


@bp.route("/admin")
@staff_required
def admin_panel():
    try:
        results = run_parallel({"members": get_all_members, "lab_users": get_all_lab_users})
        members   = results.get("members",   [])
        lab_users = results.get("lab_users", [])
        return render_template("admin.html",
            members=members, members_count=len(members),
            lab_users=lab_users, lab_users_count=len(lab_users),
            announcements=get_announcements_all(),
        )
    except Exception as e:
        traceback.print_exc()
        return f"Error: {e}", 500


# ── Staff CRUD ────────────────────────────────────────────────────────────────
@bp.route("/admin/staff/add", methods=["POST"])
@staff_required
def admin_staff_add():
    f = request.form
    result = hr_execute("""
        INSERT INTO profile (designation, team, email, type_of_appointment,
                             qualification, joining_date, iitb_joining_date, p_project_code)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (f.get("designation","").strip(), f.get("team","").strip(), f.get("email","").strip(),
          f.get("type_of_appointment","").strip(), f.get("qualification","").strip(),
          f.get("joining_date") or None, f.get("iitb_joining_date") or None,
          f.get("p_project_code","").strip()))
    if result["ok"]:
        cache.delete_pattern("get_all_members")
        flash(f"Staff member added successfully (ID: {result['last_id']}).", "success")
    else:
        flash(f"Error adding staff: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-staff")


@bp.route("/admin/staff/edit/<int:member_id>", methods=["POST"])
@staff_required
def admin_staff_edit(member_id):
    f = request.form
    result = hr_execute("""
        UPDATE profile SET designation=%s, team=%s, email=%s,
            type_of_appointment=%s, qualification=%s,
            joining_date=%s, iitb_joining_date=%s, p_project_code=%s
        WHERE member_id=%s
    """, (f.get("designation","").strip(), f.get("team","").strip(), f.get("email","").strip(),
          f.get("type_of_appointment","").strip(), f.get("qualification","").strip(),
          f.get("joining_date") or None, f.get("iitb_joining_date") or None,
          f.get("p_project_code","").strip(), member_id))
    if result["ok"]:
        cache.delete_pattern("get_person")
        cache.delete_pattern("get_all_members")
        flash("Staff member updated successfully.", "success")
    else:
        flash(f"Error updating staff: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-staff")


@bp.route("/admin/staff/deactivate/<int:member_id>", methods=["POST"])
@staff_required
def admin_staff_deactivate(member_id):
    from datetime import date
    result = hr_execute(
        "UPDATE profile SET leaving_date=%s WHERE member_id=%s",
        (date.today(), member_id))
    if result["ok"]:
        cache.delete_pattern("get_all_members")
        flash("Staff member deactivated.", "success")
    else:
        flash(f"Error: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-staff")


@bp.route("/admin/staff/delete/<int:member_id>", methods=["POST"])
@staff_required
def admin_staff_delete(member_id):
    result = hr_execute("DELETE FROM profile WHERE member_id=%s", (member_id,))
    if result["ok"]:
        cache.delete_pattern("get_all_members")
        flash("Staff member deleted.", "success")
    else:
        flash(f"Error: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-staff")


# ── Lab user CRUD ─────────────────────────────────────────────────────────────
@bp.route("/admin/lab/add", methods=["POST"])
@staff_required
def admin_lab_add():
    f = request.form
    result = slots_execute("""
        INSERT INTO login (email, fname, lname, position, department,
                           supervisor, research_area, expiry_date, mobile, rollno)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (f.get("email","").strip(), f.get("fname","").strip(), f.get("lname","").strip(),
          f.get("position","").strip(), f.get("department","").strip(),
          f.get("supervisor","").strip() or None, f.get("research_area","").strip(),
          f.get("expiry_date","").strip(), f.get("mobile","").strip(),
          f.get("rollno","").strip()))
    if result["ok"]:
        cache.delete_pattern("get_all_lab_users")
        flash(f"Lab user added (ID: {result['last_id']}).", "success")
    else:
        flash(f"Error adding lab user: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-lab")


@bp.route("/admin/lab/edit/<int:memberid>", methods=["POST"])
@staff_required
def admin_lab_edit(memberid):
    f = request.form
    result = slots_execute("""
        UPDATE login SET email=%s, fname=%s, lname=%s, position=%s,
            department=%s, supervisor=%s, research_area=%s,
            expiry_date=%s, mobile=%s, rollno=%s
        WHERE memberid=%s
    """, (f.get("email","").strip(), f.get("fname","").strip(), f.get("lname","").strip(),
          f.get("position","").strip(), f.get("department","").strip(),
          f.get("supervisor","").strip() or None, f.get("research_area","").strip(),
          f.get("expiry_date","").strip(), f.get("mobile","").strip(),
          f.get("rollno","").strip(), memberid))
    if result["ok"]:
        cache.delete_pattern("get_lab_user")
        cache.delete_pattern("get_all_lab_users")
        flash("Lab user updated.", "success")
    else:
        flash(f"Error: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-lab")


@bp.route("/admin/lab/deactivate/<int:memberid>", methods=["POST"])
@staff_required
def admin_lab_deactivate(memberid):
    result = slots_execute(
        "UPDATE login SET expiry_date='01/01/2000' WHERE memberid=%s", (memberid,))
    if result["ok"]:
        cache.delete_pattern("get_all_lab_users")
        flash("Lab user deactivated.", "success")
    else:
        flash(f"Error: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-lab")


@bp.route("/admin/lab/delete/<int:memberid>", methods=["POST"])
@staff_required
def admin_lab_delete(memberid):
    result = slots_execute("DELETE FROM login WHERE memberid=%s", (memberid,))
    if result["ok"]:
        cache.delete_pattern("get_all_lab_users")
        flash("Lab user deleted.", "success")
    else:
        flash(f"Error: {result['error']}", "error")
    return redirect(url_for("admin.admin_panel") + "#tab-lab")


# ── Admin API helpers ─────────────────────────────────────────────────────────
@bp.route("/api/admin/staff/<int:member_id>")
@staff_required
def api_admin_staff(member_id):
    from flask import jsonify
    from models.staff import get_person
    return jsonify(get_person(member_id) or {})


@bp.route("/api/admin/lab/<int:memberid>")
@staff_required
def api_admin_lab(memberid):
    from flask import jsonify
    from models.lab import get_lab_user
    return jsonify(get_lab_user(memberid) or {})
