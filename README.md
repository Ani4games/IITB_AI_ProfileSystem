# IITBNF Personnel Profiling & Activity Analysis System

**IIT Bombay Nanofabrication Facility — Internal Tool**

A Flask-based web application for HR analytics, staff and lab-user profile management, equipment activity tracking, AI-generated narrative summaries, and PDF report generation.

---

## What It Does

- **Unified search hub** — find any staff member or lab user by name, ID, designation, or department
- **Staff profiles** — attendance trends, monthly leave breakdown, slot/equipment activity, system ownership history, tool permissions, publications, projects
- **Lab user profiles** — slot reservations, equipment request statuses, session reports, cancellations, registration details
- **AI summaries** — streaming LLM-generated narrative reports using a local GGUF model (Qwen 2.5 1.5B) via llama-cpp-python, with a CAG (Context-Augmented Generation) layer and gated TF-IDF RAG for comparative/policy questions
- **PDF exports** — WeasyPrint-rendered PDFs for full profiles, system owner reports, and ownership track reports, generated asynchronously in background threads
- **Admin panel** — announcement CRUD, unified people search
- **AI voice assistant** — Web Speech API STT/TTS panel embedded in every profile page
- **Debug tooling** — `/debug/timings`, `/debug/ai/full/<type>/<id>`, `/debug/staff/<id>` endpoints for diagnosing performance and AI pipeline failures

---

## Architecture Overview

```
app.py                  Flask factory — registers blueprints, starts RAG ingestion thread
├── routes/
│   ├── auth_routes.py       Login / logout
│   ├── profile.py           Staff profile page + async PDF generation
│   ├── lab_profile.py       Lab user profile page + PDF
│   ├── admin_panel.py       Admin search hub + announcement management
│   ├── section_routes.py    AJAX per-section data endpoints (attendance, slots, etc.)
│   ├── ai_routes.py         /api/ai/report (batch) + /api/ai/stream (SSE)
│   ├── rag_routes.py        RAG profile pages + /api/rag/chat
│   ├── announcements.py     Legacy announcement CRUD routes
│   ├── debug.py             Speed dashboard, DB test, timing profiler
│   └── debug_ai.py         Step-by-step AI pipeline diagnostics
├── models/
│   ├── ai.py               Context builders (_build_staff_context, _build_lab_context)
│   │                       + template-based narrative fallback
│   ├── staff.py            All HR-portal queries (attendance, slot activity, etc.)
│   └── lab.py              All slotbooking queries (reservations, permissions, etc.)
├── rag/
│   ├── ingest.py           TF-IDF index builder (SQL dumps + live DB serialisations)
│   ├── retrieve.py         Hybrid TF-IDF + word-vector retrieval (spaCy / GloVe)
│   ├── pipeline.py         LLM prompt builder, rag_generate, rag_chat, rag_stream
│   └── agent.py            Intent detection layer (short vs executive mode)
├── db.py                   Thread-safe connection pools for hr_portal + slotbooking
├── cache.py                In-memory TTL cache + @cached decorator
├── auth.py                 Session helpers and route guards (currently disabled)
├── utils.py                Parallel execution, holiday caching, name resolution
└── config.py               DB credentials, Flask settings, position constants
```

---

## Database Requirements

The application connects to **two MySQL/MariaDB databases**:

| Database | Purpose |
|---|---|
| `hr_portal` | Staff records: profiles, attendance, leaves, monthly reports, holidays |
| `slotbooking` | Lab users, equipment reservations, permissions, system ownership, publications, projects |

Both databases must be accessible from the host running the Flask app. Connection settings live in `config.py` and can be overridden via environment variables:

```bash
export DB_HOST=localhost
export DB_USER=root
export DB_PASS=yourpassword
```

---

## Installation

### Prerequisites

- Python 3.11+
- MySQL / MariaDB with `hr_portal` and `slotbooking` schemas populated
- (Optional) A GGUF model file for local LLM inference

### Install Python dependencies

```bash
pip install flask pymysql scikit-learn weasyprint spacy
python -m spacy download en_core_web_md

# For local LLM inference:
pip install llama-cpp-python

# For GloVe word vectors (optional, higher quality RAG):
pip install gensim
```

### LLM model (optional but needed for AI summaries)

Download `qwen2.5-1.5b-instruct-q4_k_m.gguf` from HuggingFace and place it at:

```
models/qwen2.5-1.5b-instruct-q4_k_m.gguf
```

The app runs without the model — profile pages and PDFs work fully; only the streaming AI summary panel will show an error.

### Run

```bash
python app.py
```

The server starts on port 5000. On first start it spins up a background thread to build the TF-IDF RAG index from live DB data.

---

## Key Configuration (`config.py`)

| Setting | Default | Notes |
|---|---|---|
| `DB_HR` / `DB_SLOTS` | localhost, root | MariaDB connection dicts |
| `SECRET_KEY` | `iitbnf-dev-secret-…` | **Change before any production use** |
| `STAFF_POSITIONS` | `{"IITBNF Staff", "Faculty"}` | Determines staff vs lab routing on login |
| `SESSION_COOKIE_SECURE` | `False` | Set to `True` if serving over HTTPS |
| `PERMANENT_SESSION_LIFETIME` | 2 hours | Session expiry |

---

## Caching Strategy

The app uses an in-process `SimpleCache` (TTL-based, `cache.py`):

- `get_all_members` / `get_all_lab_users` — 5 min (shared across all users)
- `get_attendance_stats` / `get_attendance_trend` / `get_slot_activity` — 2 min per member
- `get_available_years` — 5 min
- `get_holidays()` — 1 hour (module-level, separate from SimpleCache)
- `calc_mandatory_days()` — per-year, cached for the calendar year

**Important:** This is a single-process in-memory cache. It does **not** survive restarts and is **not shared** between multiple worker processes.

---

## Authentication

`auth.py` currently has authentication **disabled** — all decorators (`@login_required`, `@staff_required`, `@admin_required`) are pass-through no-ops. This is intentional for internal/dev use. To re-enable, restore the session checks in `auth.py`.

---

## PDF Generation

PDFs are generated asynchronously using WeasyPrint:

1. Client hits `/profile/<id>/pdf/start` → server starts a background thread, returns `{job_id}`
2. Client polls `/profile/pdf/status/<job_id>` until `done`
3. Client downloads from `/profile/pdf/download/<job_id>`

Temporary PDF files are written to `tmp/` in the project root and are not cleaned up automatically.

---

## AI Pipeline

```
User message
    ↓
agent.py  (detect_intent: "short" or "executive")
    ↓
Is it a question? → rag_chat() in pipeline.py
                         ↓
                   CAG: full context dict always injected
                   Gated RAG: TF-IDF retrieval only for
                   comparative/policy keywords
                         ↓
                   _call_llm() → llama-cpp-python
                         ↓
                   SSE stream to browser

Is it a report? → rag_stream() in pipeline.py
                       ↓
                 Context-only (no RAG chunks — prevents cross-person hallucination)
                       ↓
                 Streaming tokens → SSE → browser voice readout (Web Speech API)
```

---

## Notable Design Decisions

**CAG over RAG for reports.** The TF-IDF index contains all staff members' data. When that data is injected into report prompts it caused the model to hallucinate names from other people's records. Reports now inject only the current person's context dict directly — the RAG retrieval layer is disabled for report generation and only fires for comparative/policy chat questions.

**Async PDF jobs.** WeasyPrint can take 5–30 seconds for large reports. Moving generation to a background thread with a polling endpoint prevents Gunicorn worker timeout and gives the browser a progress bar.

**UID resolution.** Staff exist in `hr_portal` and lab users in `slotbooking`. A 4-step fallback chain (`_resolve_uid_uncached` in `staff.py`) maps HR `member_id` → slotbooking `memberid` via email, email-prefix wildcard, name match, and numeric ID collision. Results are cached 30 minutes in-process. `_warmup_uid()` is called before `run_parallel()` on profile loads to prevent 4 parallel tasks from each triggering the expensive resolution simultaneously.

---

## File Structure

```
iitbnf/
├── app.py
├── auth.py
├── cache.py
├── config.py
├── db.py
├── debug_ai.py
├── sync_expiry.py          Utility: sync leaving_date from HR → slotbooking expiry_date
├── utils.py
├── models/
│   ├── ai.py
│   ├── lab.py
│   └── staff.py
├── rag/
│   ├── agent.py
│   ├── ingest.py
│   ├── pipeline.py
│   └── retrieve.py
├── routes/
│   ├── admin_panel.py
│   ├── ai_routes.py
│   ├── announcements.py
│   ├── auth_routes.py
│   ├── debug.py
│   ├── lab_profile.py
│   ├── profile.py
│   ├── rag_routes.py
│   └── section_routes.py
├── templates/
│   ├── admin_panel.html
│   ├── ai_panel.html
│   ├── index.html
│   ├── lab_profile.html
│   ├── lab_profile_pdf.html
│   ├── login.html
│   ├── not_found.html
│   ├── profile.html
│   ├── profile_pdf.html
│   ├── system_owner_pdf.html
│   └── system_owner_track_pdf.html
├── static/
│   ├── style.css
│   ├── style_admin.css
│   ├── style_profile.css
│   ├── style_profile_pdf.css
│   └── favicon.svg
├── models/                 (GGUF model files — not in repo)
│   └── qwen2.5-1.5b-instruct-q4_k_m.gguf
└── tmp/                    (Generated PDFs — not in repo)
```

---

## Known Limitations

- In-memory cache is not shared across multiple worker processes (incompatible with multi-process Gunicorn without an external cache like Redis)
- `tmp/` PDF files accumulate and are never cleaned up
- The LLM inference lock (`_inference_lock`) serialises all AI requests — concurrent AI usage will queue
- `sync_expiry.py` must be run manually when staff leave dates are updated in HR
- The debug routes (`/debug/*`) have no additional access control beyond `@staff_required` (which is currently disabled)
