"""
routes/auth_routes.py — /login and /logout.
"""
from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from db import slots_query
from auth import md5, is_logged_in
from cache import cache

bp = Blueprint("auth", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email  = request.form.get("email", "").strip()
        passwd = request.form.get("password", "")

        rows = slots_query("""
            SELECT memberid, email, fname, lname, position, is_admin
            FROM login WHERE email=%s AND password=%s LIMIT 1
        """, (email, md5(passwd)))

        if rows:
            u = rows[0]
            session.permanent = True

            def get_val(data, key, index):
                return data.get(key) if isinstance(data, dict) else data[index]

            try:
                session.update({
                    "memberid":  get_val(u, "memberid", 0),
                    "email":     get_val(u, "email", 1),
                    "fname":     get_val(u, "fname", 2),
                    "lname":     get_val(u, "lname", 3),
                    "position":  get_val(u, "position", 4),
                    "is_admin":  get_val(u, "is_admin", 5),
                })
                session["full_name"] = f"{session['fname']} {session['lname']}".strip()

                # ── ROUTING LOGIC ─────────────────────────────────────────
                is_admin = session.get("is_admin") == 1
                position = session.get("position", "")

                from config import STAFF_POSITIONS

                if is_admin:
                    # Admins go to the new admin panel
                    return redirect(url_for("admin_panel.index"))
                elif position in STAFF_POSITIONS:
                    return redirect(url_for("profile.profile",
                                            member_id=session["memberid"]))
                else:
                    return redirect(url_for("lab_profile.lab_profile",
                                            memberid=session["memberid"]))

            except Exception as e:
                error = f"Session Error: {str(e)}"
        else:
            error = "Invalid email or password."

    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    session.clear()
    # Do NOT call cache.clear() here — that wipes shared caches like
    # get_all_members and get_all_lab_users for every user simultaneously,
    # making the next admin panel visit re-run all expensive DB queries.
    # Per-user cached data expires naturally via TTL.
    return redirect(url_for("auth.login"))
