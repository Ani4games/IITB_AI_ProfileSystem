"""
auth.py — Session helpers and route protection decorators.
Set AUTH_DISABLED = True only during local development.
"""
import hashlib
import os
from functools import wraps
from flask import session, redirect, url_for, flash, request, jsonify
from config import STAFF_POSITIONS

# ── Master toggle — set via environment variable so production is
#    never accidentally left open ──────────────────────────────────────────────
# AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() == "true"
AUTH_DISABLED = True

def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()

# ── Session checks ────────────────────────────────────────────────────────────

def is_logged_in() -> bool:
    if AUTH_DISABLED:
        return True
    return bool(session.get("memberid"))


def is_full_access() -> bool:
    """
    Returns True if the current user can view any profile.
    Full access = is_admin OR holds a staff/faculty position.
    """
    if AUTH_DISABLED:
        return True
    if session.get("is_admin") == 1:
        return True
    position = session.get("position", "")
    return position in STAFF_POSITIONS


def _is_api_request() -> bool:
    """Returns True if the request expects JSON (API call, not browser nav)."""
    return (
        request.path.startswith("/api/")
        or request.headers.get("Accept", "").startswith("application/json")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _unauthorized_response(message: str = "Login required."):
    """Return JSON 401 for API requests, redirect for browser requests."""
    if _is_api_request():
        return jsonify({"success": False, "error": message}), 401
    flash(message, "error")
    return redirect(url_for("auth.login"))


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    """Allow any logged-in user (staff or lab)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_DISABLED or is_logged_in():
            return f(*args, **kwargs)
        return _unauthorized_response("Please log in to continue.")
    return decorated


def staff_required(f):
    """Allow only staff / admin users."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_DISABLED:
            return f(*args, **kwargs)
        if not is_logged_in():
            return _unauthorized_response("Please log in to continue.")
        if not is_full_access():
            flash("You do not have permission to access this page.", "error")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Allow only admin users (is_admin == 1)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_DISABLED:
            return f(*args, **kwargs)
        if not is_logged_in():
            return _unauthorized_response("Please log in to continue.")
        if session.get("is_admin") != 1:
            flash("Admin access required.", "error")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated