"""
serve.py — Production WSGI server entry point.
Run with: python serve.py

On Linux/production, replace with:
    gunicorn -w 4 -b 0.0.0.0:5000 --timeout 120 serve:app
"""
import os
import multiprocessing

# ── Import the Flask app ──────────────────────────────────────────────────────
from app import app

# ── Config ────────────────────────────────────────────────────────────────────
# HOST    = os.getenv("HOST", "0.0.0.0") # works for linux, but on Windows we need to bind to localhost
HOST   = os.getenv("HOST", "127.0.0.1")
PORT    = int(os.getenv("PORT", 5000))

# Waitress works best with threads rather than processes on Windows.
# Rule of thumb: 2x CPU cores + 1
THREADS = int(os.getenv("THREADS", (multiprocessing.cpu_count() * 2) + 1))

if __name__ == "__main__":
    from waitress import serve

    print(f"""
╔══════════════════════════════════════════════════╗
║  IITBNF Personnel Profiling System               ║
║  WSGI Server: Waitress                           ║
╠══════════════════════════════════════════════════╣
║  Host    : {HOST:<38}║
║  Port    : {str(PORT):<38}║
║  Threads : {str(THREADS):<38}║
╚══════════════════════════════════════════════════╝
    """)

    serve(
        app,
        host            = HOST,
        port            = PORT,
        threads         = THREADS,
        channel_timeout = 120,    # seconds before dropping idle connection
        cleanup_interval= 30,
        connection_limit= 5000,
    )