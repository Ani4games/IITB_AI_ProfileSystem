"""
routes/announcements.py — Announcement CRUD routes.
"""
from datetime import datetime
from flask import Blueprint, request, redirect, url_for, flash, jsonify
from auth import staff_required, login_required
from db import slots_execute
from models.lab import get_announcements

bp = Blueprint("announcements", __name__)


@bp.route("/admin/announcement/add", methods=["POST"])
@staff_required
def admin_announcement_add():
    f         = request.form
    text      = f.get("announcement", "").strip()
    start_str = f.get("start_datetime", "")
    end_str   = f.get("end_datetime", "")
    if not text or not start_str or not end_str:
        flash("All fields are required.", "error")
        return redirect(url_for("admin.admin_panel"))
    try:
        start_ts = int(datetime.strptime(start_str, "%Y-%m-%dT%H:%M").timestamp())
        end_ts   = int(datetime.strptime(end_str,   "%Y-%m-%dT%H:%M").timestamp())
        slots_execute(
            "INSERT INTO announcements (announcement, start_datetime, end_datetime) VALUES (%s, %s, %s)",
            (text, start_ts, end_ts))
        flash("Announcement added.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/announcement/edit/<int:aid>", methods=["POST"])
@staff_required
def admin_announcement_edit(aid):
    f         = request.form
    text      = f.get("announcement", "").strip()
    start_str = f.get("start_datetime", "")
    end_str   = f.get("end_datetime", "")
    if not text or not start_str or not end_str:
        flash("All fields are required.", "error")
        return redirect(url_for("admin.admin_panel"))
    try:
        start_ts = int(datetime.strptime(start_str, "%Y-%m-%dT%H:%M").timestamp())
        end_ts   = int(datetime.strptime(end_str,   "%Y-%m-%dT%H:%M").timestamp())
        slots_execute(
            "UPDATE announcements SET announcement=%s, start_datetime=%s, end_datetime=%s WHERE announcementid=%s",
            (text, start_ts, end_ts, aid))
        flash("Announcement updated.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/announcement/delete/<int:aid>", methods=["POST"])
@staff_required
def admin_announcement_delete(aid):
    try:
        slots_execute("DELETE FROM announcements WHERE announcementid=%s", (aid,))
        flash("Announcement deleted.", "success")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/api/announcements")
@login_required
def api_announcements():
    return jsonify(get_announcements())
