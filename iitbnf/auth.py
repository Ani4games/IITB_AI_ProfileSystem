"""
auth.py — Session helpers and route protection decorators.
"""
import hashlib
from functools import wraps
from flask import session, redirect, url_for, flash
from config import STAFF_POSITIONS


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def is_logged_in():
    return "memberid" in session


def is_full_access():
    return (
        session.get("is_admin") == 1
        or session.get("position") in STAFF_POSITIONS
    )


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in to continue.", "warn")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in to continue.", "warn")
            return redirect(url_for("auth.login"))
        if not is_full_access():
            flash("You don't have permission to view this page.", "error")
            return redirect(url_for("dashboard.dashboard"))
        return f(*args, **kwargs)
    return decorated
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            flash("Please log in to continue.", "warn")
            return redirect(url_for("auth.login"))
        if session.get("is_admin") != 1:
            flash("You don't have permission to view this page.", "error")
            return redirect(url_for("dashboard.dashboard"))
        return f(*args, **kwargs)
    return decorated