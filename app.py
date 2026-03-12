"""
╔══════════════════════════════════════════════════════════════════════╗
║  AI-Based Personnel Profiling & Activity Analysis System            ║
║  IIT Bombay Nanofabrication Facility (IITBNF)                       ║
╚══════════════════════════════════════════════════════════════════════╝

app.py — Flask application factory. Registers blueprints and filters.
All logic lives in models/ and routes/.
"""
import atexit
from datetime import datetime
from flask import Flask
import config
from db import hr_pool, slots_pool

# ── Create app ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key                        = config.SECRET_KEY
app.config["SESSION_COOKIE_SECURE"]   = config.SESSION_COOKIE_SECURE
app.config["SESSION_COOKIE_HTTPONLY"] = config.SESSION_COOKIE_HTTPONLY
app.config["SESSION_COOKIE_SAMESITE"] = config.SESSION_COOKIE_SAMESITE
app.config["PERMANENT_SESSION_LIFETIME"] = config.PERMANENT_SESSION_LIFETIME

# ── Jinja filters ─────────────────────────────────────────────────────────────
@app.template_filter("datetimeformat")
def datetimeformat(ts):
    """Unix timestamp → readable string."""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return "—"


@app.template_filter("datetimeformat_input")
def datetimeformat_input(ts):
    """Unix timestamp → datetime-local input format."""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return ""


# ── Register blueprints ───────────────────────────────────────────────────────
from routes.auth_routes  import bp as auth_bp
from routes.dashboard    import bp as dashboard_bp
from routes.profile      import bp as profile_bp
from routes.lab_profile  import bp as lab_profile_bp
from routes.admin        import bp as admin_bp
from routes.ai_routes    import bp as ai_bp
from routes.announcements import bp as announcements_bp
from routes.debug        import bp as debug_bp

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(profile_bp)
app.register_blueprint(lab_profile_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(ai_bp)
app.register_blueprint(announcements_bp)
app.register_blueprint(debug_bp)

# ── Cleanup ───────────────────────────────────────────────────────────────────
@app.teardown_appcontext
def cleanup(exception=None):
    pass


@atexit.register
def shutdown():
    print("Shutting down — closing database connections...")
    hr_pool.close_all()
    slots_pool.close_all()


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True, use_reloader=False)
