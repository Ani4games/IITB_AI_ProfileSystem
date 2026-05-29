"""
╔══════════════════════════════════════════════════════════════════════╗
║  AI-Based Personnel Profiling & Activity Analysis System             ║
║  IIT Bombay Nanofabrication Facility (IITBNF)                        ║
╚══════════════════════════════════════════════════════════════════════╝
 
app.py — Flask application factory. Registers blueprints and filters.
Redundant blueprints (hub, dashboard, admin) have been removed.
"""
import os
import atexit
import time
from datetime import datetime, timedelta
from flask.json.provider import DefaultJSONProvider
import threading
from flask import Flask, session, redirect, url_for
import config
from db import hr_pool, slots_pool
from rag.ingest import init_rag
from utils import start_pdf_cleanup
import logging
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
# ── RAG ingestion (background, daemon) ───────────────────────────────────────
threading.Thread(target=init_rag, daemon=True).start()
def _configure_xhtml2pdf():
    """
    Bypass ALL system font scanning by pre-registering fonts directly
    with ReportLab before xhtml2pdf's first call. Works on Windows,
    Linux, and any private server regardless of installed system fonts.
    """
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib.fonts import addMapping

        font_dir = os.path.join(os.getcwd(), "static", "fonts")
        if not os.path.isdir(font_dir):
            print("[xhtml2pdf] Font dir not found — system scan will occur")
            return

        # Register all four variants
        registrations = [
            ("DejaVuSans",             "DejaVuSans.ttf",             False, False),
            ("DejaVuSans-Bold",        "DejaVuSans-Bold.ttf",        True,  False),
            ("DejaVuSans-Oblique",     "DejaVuSans-Oblique.ttf",     False, True),
            ("DejaVuSans-BoldOblique", "DejaVuSans-BoldOblique.ttf", True,  True),
        ]

        registered = []
        for name, filename, bold, italic in registrations:
            path = os.path.join(font_dir, filename)
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont(name, path))
                registered.append(name)

        # Map the family so CSS font-family: DejaVuSans works correctly
        if len(registered) == 4:
            addMapping("DejaVuSans", 0, 0, "DejaVuSans")
            addMapping("DejaVuSans", 1, 0, "DejaVuSans-Bold")
            addMapping("DejaVuSans", 0, 1, "DejaVuSans-Oblique")
            addMapping("DejaVuSans", 1, 1, "DejaVuSans-BoldOblique")

        # --- THE KEY PART ---
        # Monkey-patch ReportLab's font search to prevent system scan
        try:
            from reportlab.rl_config import canvas as rl_canvas
        except ImportError:
            pass

        try:
            import reportlab.rl_config as rl_config
            # Tell ReportLab not to search system font directories
            rl_config.TTFSearchPath = [font_dir]
            print(f"[xhtml2pdf] TTFSearchPath restricted to project fonts.")
        except Exception:
            pass

        print(f"[xhtml2pdf] Registered {len(registered)} fonts from {font_dir}")

    except Exception as e:
        print(f"[xhtml2pdf] Font config failed (non-fatal): {e}")

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
    # Step 1: xhtml2pdf pre-warm with MINIMAL html (not a full profile)
    try:
        import io
        from xhtml2pdf import pisa
        buf = io.BytesIO()
        pisa.CreatePDF(
            src="<html><body><p>init</p></body></html>",
            dest=buf,
            encoding="utf-8",
        )
        print("[warmup] xhtml2pdf pre-warmed.")
    except Exception as e:
        print(f"[warmup] xhtml2pdf pre-warm failed: {e}")
    finally:
    # Always signal ready — even on failure, so PDFs aren't blocked forever
        try:
            from routes.profile import _xhtml2pdf_ready
            from routes.lab_profile import _xhtml2pdf_ready as _lab_xhtml2pdf_ready
            _xhtml2pdf_ready.set() # set anyway so PDFs aren't blocked forever 
            _lab_xhtml2pdf_ready.set() 
        except Exception:
            pass
    # Step 3: member list cache
    try:
        from models.staff import get_all_members
        from models.lab   import get_all_lab_users
        get_all_members()
        get_all_lab_users()
        print("[warmup] Member cache populated.")
    except Exception as e:
        print(f"[warmup] Member cache failed (non-fatal): {e}")
 
    # Step 4: AI composer
    try:
        from rag.composer import warm_up
        warm_up()
        print("[warmup] Composer ready.")
    except Exception as e:
        print(f"[warmup] Composer warm-up failed (non-fatal): {e}")
    
    # In app.py, _startup_tasks(), after the composer warmup:

    # Step 5: PDF cleanup (NEW)
    try:
        from routes.profile     import PDF_JOBS
        from routes.lab_profile import LAB_PDF_JOBS
        start_pdf_cleanup(
            jobs_dicts       = [PDF_JOBS, LAB_PDF_JOBS],
            tmp_dir          = os.path.join(os.getcwd(), "tmp"),
            max_age_seconds  = 3600,   # 1 hour
            interval_seconds = 1800,   # run every 30 minutes
        )
        print("[warmup] PDF cleanup thread started.")
    except Exception as e:
        print(f"[warmup] PDF cleanup failed to start (non-fatal): {e}")


threading.Thread(target=_startup_tasks, daemon=True).start()

# ── Custom JSON provider — must be set BEFORE Flask(__name__) ─────────────────
# Fixes "timedelta is not JSON serializable" errors in jsonify() responses.
# Defined here so it is available to all blueprints via the app instance.
class CustomJSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, timedelta):
            return str(o)
        return super().default(o)
# After imports, before app = Flask(__name__)
_configure_xhtml2pdf()
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
from routes.debug_ai      import bp as debug_ai_bp
 
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