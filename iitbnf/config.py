"""
config.py — All environment variables, DB config, and constants.
"""
import os
from datetime import timedelta

# ── Database ──────────────────────────────────────────────────────────────────
DB_HR = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASS", "Ani4MariaDB"),
    "database": "hr_portal",
    "charset":  "utf8mb4",
    "use_named_pipe": True,
    "pipe_name":      "MySQL",    # matches your socket=MySQL in my.ini
}
DB_SLOTS = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASS", "Ani4MariaDB"),
    "database": "slotbooking",
    "charset":  "utf8mb4",
    "use_named_pipe": True,
    "pipe_name":      "MySQL",    # matches your socket=MySQL in my.ini
}

# ── AI ────────────────────────────────────────────────────────────────────────
AI_MODE      = "llamacpp"  # "ollama", "local", or "mock"
# OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
# OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
LOCAL_MODEL_NAME   = os.getenv("LOCAL_MODEL_NAME",   "Qwen/Qwen2.5-0.5B-Instruct")
LOCAL_MODEL_DEVICE = os.getenv("LOCAL_MODEL_DEVICE",  "cpu")
LOCAL_MODEL_TEMP   = float(os.getenv("LOCAL_MODEL_TEMP", "0.15"))
GGUF_MODEL_PATH = "qwen2.5-0.5b-instruct.Q4_K_M.gguf"
# ── Flask ─────────────────────────────────────────────────────────────────────
SECRET_KEY              = os.getenv("SECRET_KEY", "iitbnf-dev-secret-change-in-prod")
SESSION_COOKIE_SECURE = os.getenv("FLASK_ENV") == "production"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
PERMANENT_SESSION_LIFETIME = timedelta(hours=2)

# ── App constants ─────────────────────────────────────────────────────────────
STAFF_POSITIONS      = {"IITBNF Staff", "Faculty", "Institute Facility"}
SLOW_QUERY_THRESHOLD = 200  # ms

# ── Position context for AI prompts ──────────────────────────────────────────
POSITION_CONTEXT = {
    "Ph.D":          "a doctoral researcher working towards a PhD degree",
    "M.Tech":        "a postgraduate student pursuing an M.Tech degree",
    "M.Tech RA":     "an M.Tech student on a research assistantship",
    "B.Tech":        "an undergraduate student on a project internship",
    "INUP":          "a visiting researcher under the INUP national nanofabrication programme",
    "Faculty":       "a faculty member supervising research projects",
    "Project Staff": "a project staff member supporting research operations",
    "PDF":           "a postdoctoral fellow conducting research",
    "Industry User": "an industry user accessing facility equipment for commercial R&D",
    "IITBNF Staff":  "a core facility staff member managing lab operations",
}
