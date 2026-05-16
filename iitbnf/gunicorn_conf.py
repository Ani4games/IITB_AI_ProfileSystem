"""
gunicorn.conf.py — Gunicorn config for Linux/production deployment.

Run with:
    gunicorn -c gunicorn.conf.py serve:app

Install:
    pip install gunicorn
"""
import multiprocessing
import os

# ── Binding ───────────────────────────────────────────────────────────────────
bind    = os.getenv("BIND", "0.0.0.0:5000")

# ── Workers ───────────────────────────────────────────────────────────────────
# For CPU-bound work (PDF rendering): use (2 x CPU) + 1
# For I/O-bound work (DB queries):    use more workers
workers     = int(os.getenv("WEB_CONCURRENCY", (multiprocessing.cpu_count() * 2) + 1))
worker_class = "sync"        # sync is fine for Flask + blocking DB calls
threads      = 4             # threads per worker — handles parallel section loads

# ── Timeouts ─────────────────────────────────────────────────────────────────
timeout      = 120           # PDF generation can take up to 2 min
keepalive    = 5             # seconds to keep idle connections alive
graceful_timeout = 30

# ── Logging ──────────────────────────────────────────────────────────────────
accesslog    = "logs/access.log"
errorlog     = "logs/error.log"
loglevel     = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sμs'

# ── Process naming ────────────────────────────────────────────────────────────
proc_name    = "iitbnf"

# ── Worker lifecycle ──────────────────────────────────────────────────────────
# Restart workers after this many requests to prevent memory leaks
max_requests          = 1000
max_requests_jitter   = 100   # randomise so workers don't all restart at once

# ── Server hooks ─────────────────────────────────────────────────────────────
def on_starting(server):
    import os
    os.makedirs("logs", exist_ok=True)
    print("Gunicorn starting — IITBNF Personnel Profiling System")

def post_fork(server, worker):
    """Called in each worker after fork — re-initialise DB pools."""
    print(f"Worker {worker.pid} started")

def worker_exit(server, worker):
    """Clean up DB connections when a worker exits."""
    try:
        from db import hr_pool, slots_pool
        hr_pool.close_all()
        slots_pool.close_all()
        print(f"Worker {worker.pid} — DB connections closed")
    except Exception as e:
        print(f"Worker {worker.pid} cleanup error: {e}")