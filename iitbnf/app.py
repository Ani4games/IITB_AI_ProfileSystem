"""
╔══════════════════════════════════════════════════════════════════════╗
║  AI-Based Personnel Profiling & Activity Analysis System             ║
║  IIT Bombay Nanofabrication Facility (IITBNF)                        ║
╚══════════════════════════════════════════════════════════════════════╝

app.py — Flask application factory. Registers blueprints and filters.
Redundant blueprints (hub, dashboard, admin) have been removed.
"""
import atexit
from datetime import datetime, timedelta
from flask.json.provider import DefaultJSONProvider
import threading
from flask import Flask, session, redirect, url_for
import config
from db import hr_pool, slots_pool
# from cache import cache
from rag.ingest import init_rag

# cache.clear()
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
app = Flask(__name__)
app.secret_key                = config.SECRET_KEY
app.config["SESSION_COOKIE_SECURE"]   = config.SESSION_COOKIE_SECURE
app.config["SESSION_COOKIE_HTTPONLY"] = config.SESSION_COOKIE_HTTPONLY
app.config["SESSION_COOKIE_SAMESITE"] = config.SESSION_COOKIE_SAMESITE
app.config["PERMANENT_SESSION_LIFETIME"] = config.PERMANENT_SESSION_LIFETIME



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
# --- GLOBAL JSON FIX ---
class CustomJSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, timedelta):
            return str(o)  # Fixes "timedelta is not JSON serializable"
        return super().default(o)
# ── Register blueprints ───────────────────────────────────────────────────────
from routes.auth_routes      import bp as auth_bp
from routes.profile          import bp as profile_bp
from routes.lab_profile      import bp as lab_profile_bp
from routes.admin_panel      import bp as admin_panel_bp  # Replaces admin/hub/dashboard
from routes.ai_routes        import bp as ai_bp
from routes.announcements    import bp as announcements_bp
from routes.debug            import bp as debug_bp
from routes.rag_routes       import bp as rag_bp
from routes.section_routes   import bp as section_bp
from debug_ai         import bp as debug_ai_bp

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

# ── RAG ingestion (background) ───────────────────────────────────────────────
threading.Thread(target=init_rag, daemon=True).start()

# ── Cache warmup (background) ─────────────────────────────────────────────────
# Pre-populate the two most expensive cached functions so the first admin
# panel request after a server restart is fast instead of taking 20 s.
def _warmup_caches():
    try:
        from models.staff import get_all_members
        from models.lab   import get_all_lab_users
        get_all_members()
        get_all_lab_users()
    except Exception as e:
        print(f"[warmup] Cache warmup failed (non-fatal): {e}")

threading.Thread(target=_warmup_caches, daemon=True).start()

# In app.py, alongside init_rag():

def _startup_tasks():
    init_rag()
    try:
        from rag.composer import warm_up
        warm_up()
    except Exception as e:
        print(f"Composer warm-up failed (non-fatal): {e}")

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
    app.run(debug=True, port=5000, threaded=True, use_reloader=False)