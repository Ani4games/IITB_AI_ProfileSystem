"""
╔══════════════════════════════════════════════════════════════════════╗
║  AI-Based Personnel Profiling & Activity Analysis System             ║
║  IIT Bombay Nanofabrication Facility (IITBNF)                        ║
╚══════════════════════════════════════════════════════════════════════╝

app.py — Flask application factory. Registers blueprints and filters.
Redundant blueprints (hub, dashboard, admin) have been removed.
"""
import atexit
import time
from datetime import datetime, timedelta
from flask.json.provider import DefaultJSONProvider
import threading
from flask import Flask, session, redirect, url_for
import config
from db import hr_pool, slots_pool
from rag.ingest import init_rag

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# ── Custom JSON provider — must be set BEFORE Flask(__name__) ─────────────────
# Fixes "timedelta is not JSON serializable" errors in jsonify() responses.
# Defined here so it is available to all blueprints via the app instance.
class CustomJSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, timedelta):
            return str(o)
        return super().default(o)

app = Flask(__name__)
app.json_provider_class = CustomJSONProvider   # ← actually registers it
app.json = CustomJSONProvider(app)             # ← activates it on this instance

app.secret_key                           = config.SECRET_KEY
app.config["SESSION_COOKIE_SECURE"]      = config.SESSION_COOKIE_SECURE
app.config["SESSION_COOKIE_HTTPONLY"]    = config.SESSION_COOKIE_HTTPONLY
app.config["SESSION_COOKIE_SAMESITE"]   = config.SESSION_COOKIE_SAMESITE
app.config["PERMANENT_SESSION_LIFETIME"] = config.PERMANENT_SESSION_LIFETIME

# Disable template auto-reload in production — big speedup.
# Set to True only during active template development.
app.config["TEMPLATES_AUTO_RELOAD"] = False


# ── Jinja filters ─────────────────────────────────────────────────────────────

@app.template_filter("datetimeformat")
def datetimeformat(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return "—"


@app.template_filter("datetimeformat_input")
def datetimeformat_input(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return ""


# ── Register blueprints ───────────────────────────────────────────────────────

from routes.auth_routes   import bp as auth_bp
from routes.profile       import bp as profile_bp
from routes.lab_profile   import bp as lab_profile_bp
from routes.admin_panel   import bp as admin_panel_bp
from routes.ai_routes     import bp as ai_bp
from routes.announcements import bp as announcements_bp
from routes.debug         import bp as debug_bp
from routes.rag_routes    import bp as rag_bp
from routes.section_routes import bp as section_bp
from debug_ai             import bp as debug_ai_bp

app.register_blueprint(auth_bp)
app.register_blueprint(profile_bp)
app.register_blueprint(lab_profile_bp)
app.register_blueprint(admin_panel_bp)
app.register_blueprint(ai_bp)
app.register_blueprint(announcements_bp)
app.register_blueprint(debug_bp)
app.register_blueprint(rag_bp)
app.register_blueprint(section_bp)
app.register_blueprint(debug_ai_bp)


# ── RAG ingestion (background, daemon) ───────────────────────────────────────
threading.Thread(target=init_rag, daemon=True).start()


# ── Startup warmup tasks — staggered to avoid DB contention ──────────────────

def _startup_tasks():
    """
    Background warmup: populates the in-process cache so the first real
    requests don't pay cold-start DB costs.

    Steps
    ─────
    1. 5-second sleep — lets the server finish binding and handle any
       immediate health-check requests before we fire DB queries.
    2. Member list cache warmup — get_all_members + get_all_lab_users.
       These are the two most expensive queries on the admin panel; warming
       them means the first admin visit is instant.
    3. Composer warmup — loads the TF-IDF/LogReg model from disk so the
       first AI profile request doesn't pay the pickle-load cost.

    NOTE: The xhtml2pdf pre-warm that previously appeared here has been
    removed. On Windows it triggered a full font-directory scan that took
    ~88 seconds at startup for zero benefit — the first real PDF call
    initialises xhtml2pdf in ~600 ms on its own.
    """
    # Step 1: let the server settle
    time.sleep(5)

    # Step 2: member list cache
    try:
        from models.staff import get_all_members
        from models.lab   import get_all_lab_users
        get_all_members()
        get_all_lab_users()
        print("[warmup] Member cache populated.")
    except Exception as e:
        print(f"[warmup] Member cache failed (non-fatal): {e}")

    # Step 3: AI composer
    try:
        from rag.composer import warm_up
        warm_up()
        print("[warmup] Composer ready.")
    except Exception as e:
        print(f"[warmup] Composer warm-up failed (non-fatal): {e}")


threading.Thread(target=_startup_tasks, daemon=True).start()


# ── Cleanup ───────────────────────────────────────────────────────────────────

@app.teardown_appcontext
def cleanup(exception=None):
    pass


@atexit.register
def shutdown():
    print("Shutting down — closing database connections...")
    hr_pool.close_all()
    slots_pool.close_all()


@app.route("/clear-session")
def clear_session():
    session.clear()
    return redirect(url_for("auth.login"))


if __name__ == "__main__":
    # Dev server only — use serve.py (Waitress) for production.
    app.run(debug=True, port=5000, threaded=True, use_reloader=False)