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
    if is_logged_in():
        return redirect(url_for("dashboard.dashboard"))

    error = None
    if request.method == "POST":
        email  = request.form.get("email", "").strip()
        passwd = request.form.get("password", "")
        rows   = slots_query("""
            SELECT memberid, email, fname, lname, position, is_admin, expiry_date
            FROM login WHERE email=%s AND password=%s LIMIT 1
        """, (email, md5(passwd)))
        if rows:
            u = rows[0]
            session.permanent = True
            session.update({
                "memberid":  u["memberid"],
                "email":     u["email"],
                "fname":     u["fname"],
                "lname":     u["lname"],
                "position":  u["position"],
                "is_admin":  u["is_admin"],
                "full_name": f"{u['fname']} {u['lname']}".strip(),
            })
            return redirect(url_for("dashboard.dashboard"))
        error = "Invalid email or password."

    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    session.clear()
    cache.clear()  # Clear any cached data for the user
    return redirect(url_for("auth.login"))
