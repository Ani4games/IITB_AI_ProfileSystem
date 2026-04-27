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
    return True  # AUTH DISABLED


def is_full_access():
    return True  # AUTH DISABLED


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)  # AUTH DISABLED
    return decorated


def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)  # AUTH DISABLED
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)  # AUTH DISABLED
    return decorated
