# IITBNF Personnel Profiling & Activity Analysis System

**IIT Bombay Nanofabrication Facility — Internal Tool**

A Flask-based web application for HR analytics, staff and lab-user profile management,
equipment activity tracking, AI-generated narrative summaries, and PDF report generation.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Architecture Overview](#architecture-overview)
3. [Database Requirements](#database-requirements)
4. [Installation & Dependencies](#installation--dependencies)
5. [Starting the Server](#starting-the-server)
6. [SLM: Training Data, Download & Fine-Tuning](#slm-training-data-download--fine-tuning)
7. [HTML Pages](#html-pages)
8. [Backend Files](#backend-files)
9. [Performance Improvements](#performance-improvements)
10. [Limitations & Resolutions](#limitations--resolutions)
11. [Deployment: Connecting to Any Server or OS](#deployment-connecting-to-any-server-or-os)

---

## What It Does

- **Unified search hub** — find any staff member or lab user by name, ID, designation, or department
- **Staff profiles** — attendance trends, monthly leave breakdown, slot/equipment activity,
  system ownership history, tool permissions, logbook entries, publications, projects
- **Lab user profiles** — slot reservations, equipment request statuses, session reports,
  cancellations, registration details, ownership track
- **AI summaries** — streaming LLM-generated narrative reports using a local GGUF model
  (Qwen 2.5 0.5B, optionally fine-tuned) via llama-cpp-python, with a CAG
  (Context-Augmented Generation) layer and gated TF-IDF RAG for comparative/policy questions
- **PDF exports** — xhtml2pdf-rendered PDFs for full profiles, system owner reports, and
  ownership track reports, generated asynchronously in background threads
- **Admin panel** — announcement CRUD, unified people search, AI assistant
- **Debug tooling** — `/debug/timings`, `/debug/ai/full/<type>/<id>`,
  `/debug/staff/<id>` endpoints for diagnosing performance and AI pipeline failures

---

## Architecture Overview
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
│   ├── composer.py         ML-based template selector for instant summaries (no LLM)
│   ├── agent.py            Intent detection + tier-0/1/2 routing
│   ├── tier0.py            Zero-model factual lookup from context dict
│   ├── query_router.py     Structured DB query handler (year-specific, multi-year)
│   ├── facility_router.py  Facility knowledge base (static answers)
│   └── data_gatherer.py    Pre-fetches structured data for LLM formatting
├── db.py                   Thread-safe connection pools for hr_portal + slotbooking
├── cache.py                In-memory TTL cache + @cached decorator
├── auth.py                 Session helpers and route guards
├── utils.py                Parallel execution, holiday caching, name resolution
└── config.py               DB credentials, Flask settings, position constants

---

## Database Requirements

The application connects to **two MySQL/MariaDB databases**:

| Database      | Purpose |
|---------------|---------|
| `hr_portal`   | Staff records: profiles, attendance, leaves, monthly reports, holidays |
| `slotbooking` | Lab users, equipment reservations, permissions, system ownership, publications, projects |

Both databases must be accessible from the host running the Flask app.
Connection settings live in `config.py` and can be overridden via environment variables:

```bash
export DB_HOST=localhost
export DB_USER=root
export DB_PASS=yourpassword
```

---

## Installation & Dependencies

### Prerequisites

- Python 3.11+
- MySQL / MariaDB with `hr_portal` and `slotbooking` schemas populated
- Git

### Core Web Framework

```bash
pip install flask
pip install waitress            # Production WSGI server (Windows/Linux)
pip install gunicorn            # Alternative production server (Linux/macOS only)
```

### Database Connectivity

```bash
pip install pymysql             # Pure-Python MySQL driver (used as primary connector)
pip install mysql-connector-python  # Optional: C-extension driver for lower latency
```

### Data & ML (RAG pipeline)

```bash
pip install scikit-learn        # TF-IDF vectoriser, cosine similarity, logistic regression
pip install numpy               # Array math for hybrid scoring
pip install spacy               # Word vector backend for semantic retrieval
python -m spacy download en_core_web_md   # 50 MB spaCy model (required)

# Optional: GloVe word vectors (higher quality retrieval, ~400 MB download on first run)
pip install gensim
```

### PDF Generation

```bash
pip install xhtml2pdf           # HTML-to-PDF renderer (used for all PDF exports)
# Note: xhtml2pdf requires reportlab internally
pip install reportlab           # Usually installed automatically with xhtml2pdf
```

### Local LLM Inference

```bash
# llama-cpp-python — binds to llama.cpp for GGUF model inference
# CPU-only build (works everywhere):
pip install llama-cpp-python

# GPU-accelerated build (CUDA — significantly faster):
CMAKE_ARGS="-DLLAMA_CUBLAS=on" pip install llama-cpp-python --force-reinstall --no-cache-dir

# GPU-accelerated build (Metal — Apple Silicon):
CMAKE_ARGS="-DLLAMA_METAL=on" pip install llama-cpp-python --force-reinstall --no-cache-dir
```

### Fine-Tuning Dependencies (Google Colab / GPU machine only)

These are only needed to fine-tune the SLM. They are not required to run the web app.

```bash
pip install unsloth             # LoRA fine-tuning with 2x memory efficiency
pip install datasets            # HuggingFace datasets for loading JSONL
pip install transformers        # Model loading and tokenisation
pip install trl                 # SFTTrainer for supervised fine-tuning
pip install peft                # LoRA/QLoRA adapter support
pip install accelerate          # Multi-GPU and mixed-precision training
pip install bitsandbytes        # 4-bit quantisation for loading large models
```

### Full one-liner install (production server, no fine-tuning):

```bash
pip install flask waitress pymysql scikit-learn numpy spacy xhtml2pdf reportlab llama-cpp-python
python -m spacy download en_core_web_md
```

---

## Starting the Server

### Development (Flask built-in server)

```bash
python app.py
```

The app starts on `http://localhost:5000`. The built-in server is **single-threaded**
and not suitable for concurrent users — use only for local testing.

### Production (Waitress — recommended for Windows and cross-platform)

Waitress is a pure-Python WSGI server with no C dependencies, making it ideal for
Windows deployments and environments where Gunicorn is unavailable.

```bash
# Install
pip install waitress

# Start (from the project root directory)
waitress-serve --host=127.0.0.1 --port=5000 --threads=8 app:app
```

Key Waitress flags:

| Flag | Description |
|------|-------------|
| `--host=127.0.0.1` | Accept connections from all network interfaces |
| `--port=5000` | Port to listen on |
| `--threads=8` | Number of worker threads (set to 2× CPU core count) |
| `--connection-limit=200` | Max simultaneous connections |
| `--channel-timeout=120` | Seconds before idle connection is dropped |

Example for a 4-core machine:

```bash
waitress-serve --host=127.0.0.1 --port=8080 --threads=8 --connection-limit=200 app:app
```

### Production (Gunicorn — Linux/macOS only)

```bash
pip install gunicorn

gunicorn --workers=4 --threads=4 --bind=0.0.0.0:5000 \
         --timeout=120 --worker-class=gthread app:app
```

### Running behind a reverse proxy (Nginx)

If deploying behind Nginx, add this to the Nginx server block:

```nginx
location / {
    proxy_pass         http://127.0.0.1:5000;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_buffering    off;           # Required for SSE streaming (AI panel)
    proxy_read_timeout 300s;          # Required for long PDF generation
}
```

The `proxy_buffering off` line is critical — without it, the Server-Sent Events
stream used by the AI panel will not work (tokens will be held in Nginx's buffer
instead of being delivered incrementally).

---

## SLM: Training Data, Download & Fine-Tuning

The AI summary feature uses Qwen 2.5 0.5B Instruct in GGUF format. You can use the
pre-quantized base model or a fine-tuned version trained on IITBNF-specific data.

### Option A — Download the base model (no fine-tuning)

```python
# Run this in Python or a Jupyter cell
from huggingface_hub import hf_hub_download

model_path = hf_hub_download(
    repo_id  = "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
    filename = "qwen2.5-0.5b-instruct-q4_k_m.gguf",
    local_dir = "./models/",
)
print(f"Downloaded to: {model_path}")
```

Place the downloaded file at: models/qwen2.5-0.5b-instruct-q4_k_m.gguf

Update the path in `llm.py` (or `config.py`) to match.

### Option B — Fine-tune on IITBNF data (Google Colab T4)

The fine-tuning notebook (`finetune_qwen.ipynb`) uses Unsloth for memory-efficient
LoRA training on a free Colab T4 GPU.

#### Step 1 — Generate training data

Training data is a JSONL file where each line is a conversation in the ChatML format
used by Qwen. Generate it by querying your database and creating instruction–response
pairs that teach the model to answer factual profile questions.

Example JSONL record format:

```json
{
  "text": "<|im_start|>system\nYou are a helpful assistant for an institutional profile system. Answer questions accurately based on the member's data.<|im_end|>\n<|im_start|>user\nWhat is the attendance percentage of Dr. A Sharma?\nFacts:\n  name = Dr. A Sharma\n  attendance_pct = 88.5\n  days_present = 212\n  working_days = 239<|im_end|>\n<|im_start|>assistant\nDr. A Sharma has an attendance rate of 88.5% this year (212 of 239 working days).<|im_end|>"
}
```

Suggested question categories to cover in training data (aim for ~10,000 examples):

- Attendance percentage and threshold comparisons
- Leave breakdown by type (CL, EL, ML, RL)
- Equipment usage request counts and statuses
- Slot reservation counts and tools used
- System ownership (current and historical)
- Tool permissions count
- Publications and projects
- Designation, team, joining date, qualification
- Multi-year comparisons for attendance and equipment activity

Save the completed file as `training_data.jsonl`.

#### Step 2 — Open and run the Colab notebook

Upload `finetune_qwen.ipynb` to Google Colab (select T4 GPU runtime under
Runtime → Change runtime type → T4 GPU).

The notebook performs the following steps automatically:

1. Installs Unsloth and all fine-tuning dependencies
2. Loads `Qwen2.5-0.5B-Instruct` in 4-bit quantised form (saves ~1 GB VRAM)
3. Attaches LoRA adapters to attention and MLP layers (r=16, alpha=16)
4. Tokenises `training_data.jsonl` and runs 3 epochs of SFT training
5. Exports the merged model to GGUF format with Q4_K_M quantisation
6. Saves the output to Google Drive

Training time on T4: approximately **35–40 minutes** for 10,000 examples × 3 epochs.
Final training loss should reach ~0.08–0.09 (seen in test run: 0.0810 at step 1935).

#### Step 3 — Download and deploy

After training completes, the notebook saves the GGUF file to your Google Drive.
Download it and place it at: models/qwen2.5-0.5b-instruct-iitbnf-finetuned.gguf

Update the model path in `llm.py`:

```python
MODEL_PATH = "models/qwen2.5-0.5b-instruct-iitbnf-finetuned.gguf"
```

#### Step 4 — Register with Ollama (optional)

If you use Ollama to serve the model instead of llama-cpp-python directly:

```bash
# The Modelfile is generated automatically by the export step
ollama create iitbnf-qwen -f models/qwen_finetuned_gguf/Modelfile

# Verify it works
ollama run iitbnf-qwen "What is an IITBNF staff member?"
```

Then update `llm.py` to call the Ollama API endpoint instead of llama-cpp-python.

---

## HTML Pages

All templates are in the `templates/` directory and use Jinja2 templating.

### `index.html` — Landing Page

The public-facing entry point with an animated dark background featuring coloured
gradient blobs, a grid overlay, and a hero section describing the system.
Contains a **Sign In → Portal** button linking to the login page.
No authentication required.

### `login.html` — Authentication Page

Glassmorphism-styled login card with frosted glass effect, animated background
blobs, and an email + password form.
Submits to `/login` (POST). On success, routes to the admin panel (admins),
staff profile (IITBNF Staff positions), or lab profile (all other users).
Displays flash messages for invalid credentials.

### `base_profile.html` — Shared Profile Shell

The Jinja2 base template extended by both staff and lab profile pages.
Provides the full shared structure: Google Fonts, Chart.js CDN, shared CSS,
topbar with PDF split button and year selector, hero card (name, role badge,
meta items, about paragraph), the card grid slot (`{% block sections %}`),
PDF progress modal, AI panel include, and all shared JavaScript constants
(MEMBER_ID, year arrays, PDF URL bases).

Extending templates fill: `page_title`, `sections`, `modals`, `page_data`, `page_js`.

### `staff_sections.html` — Staff Profile Page

Extends `base_profile.html`. Renders the 8-card grid for a staff member:
Attendance (with year selector and trend chart), Slot Activity (request log),
System Owner (current assignments), Authorised Tools (permissions),
Sys Owner Track (history), Logbook Entries, Session Reports, Cancellations,
and Lab Access Log.

All card data is loaded via AJAX after page load — the initial render completes
in under 200 ms, then secondary sections populate progressively.
Includes modals for each card with detailed tables and metrics.

### `lab_sections.html` — Lab User Profile Page

Extends `base_profile.html`. Renders cards specific to lab users:
Slot Reservations (with booking status breakdown), Equipment Requests
(with approval/rejection pills), Lab Access Log or Cancellations,
Tool Permissions (with faculty incharge), Session Reports, Error Reports
(admin-only), Projects & Papers, and optionally System Ownership and Track.

Includes the AI compose tab that streams a profile summary using the
NarrativeComposer (no LLM required for the short mode).

### `admin_panel.html` — Admin Control Panel

The central hub for administrators. Features a JARVIS-inspired dark UI with
a live clock, sidebar navigation (Search Hub, Announcements), and a
unified search box that queries all staff and lab users simultaneously.
Results appear in a dropdown with colour-coded initials (gold for staff,
violet for lab users). Also includes an AI chat bubble (bottom-right)
that connects to `/api/ai/admin-chat` for general facility Q&A.

### `ai_panel.html` — AI Assistant Floating Panel

A reusable HTML fragment included in both profile templates.
Provides the "✦ AI Assistant" floating bubble, a picker menu with
"Profile Summary" and "Ask AI" options, a ChatGPT-style Q&A modal
with suggestion chips, and a streaming typewriter summary modal.
All AI communication goes through the `/api/ai/stream` SSE endpoint.

### `profile_pdf.html` — Staff Profile PDF Template

Minimal HTML with inline CSS using `DejaVuSans` font (required by xhtml2pdf).
Renders all tables with `table-layout: fixed` and explicit `<colgroup>` widths
to prevent the negative-width layout crash in xhtml2pdf.
Covers: identity header, attendance metrics, slot activity summary,
tool permissions table, system owner count, and system permissions chips.
Rendered server-side in a background thread; downloaded by the browser
after the async job completes.

### `lab_profile_pdf.html` — Lab User Profile PDF Template

Same xhtml2pdf-safe design as the staff PDF. Covers: identity with all
contact fields, activity summary KPIs, registration details, slot reservations
table (capped at 30 rows), equipment requests table, cancellations,
lab access log, tool permissions, session reports, system ownership,
ownership history, projects, and publications.

### `system_owner_pdf.html` — System Owner PDF

A standalone PDF listing all tools a staff member currently owns as system
owner, with tool name, calculated years of ownership (computed using
only string/int arithmetic — no Python date objects — to avoid xhtml2pdf
compatibility issues), and working status badge.

### `system_owner_track_pdf.html` — Ownership History PDF

Lists the complete create/delete ownership timeline for a staff member,
paired into ownership spans with duration computed similarly using
string arithmetic. Includes summary KPIs (total, active, removed) as a
metrics strip at the top.

### `not_found.html` — 404 Page

Animated glitch-effect 404 with dual-colour CSS split animation.
Shows the requested member ID, a back-to-hub button, and a go-back link.

### `login.html` — Login Page

(Described above under Authentication Page.)

---

## Backend Files

### `app.py` — Application Factory

Creates the Flask app, registers all blueprints, configures session settings,
registers Jinja2 custom filters (`datetimeformat`, `datetimeformat_input`),
and launches the RAG ingestion thread on startup via `init_rag()`.
Also pre-warms xhtml2pdf in a background thread so the first PDF request
does not pay the initialisation cost.

### `db.py` — Database Connection Pools

Implements a thread-safe connection pool (`ConnectionPool`) for both
`hr_portal` and `slotbooking` databases. Provides `hr_query()`,
`slots_query()`, and `slots_execute()` as the sole DB access functions
used throughout the codebase. Automatically pings and reconnects stale
connections. Supports both PyMySQL (TCP) and mysql-connector-python (named pipe).

### `cache.py` — In-Memory Cache

A simple TTL cache with a `@cached(ttl_seconds=N)` decorator.
Thread-safe using a `threading.Lock`. Used to cache expensive DB queries
(member lists, attendance stats, year lists, system owner data) to prevent
repeated round-trips on every AJAX dropdown change.

### `auth.py` — Authentication Helpers

Provides `login_required`, `staff_required`, and `is_full_access()` helpers.
Also contains the `md5()` helper for password verification (matching the
existing slotbooking password hash scheme).
Currently all route decorators are pass-through (authentication disabled for
internal use). To re-enable, restore the session checks in each decorator.

### `utils.py` — Shared Utilities

- `run_parallel(tasks)` — executes a dict of callables concurrently using
  `ThreadPoolExecutor`, returning results in the same keyed dict.
  Used on every profile page load to fire 8–12 DB queries simultaneously.
- `get_holidays()` — fetches and caches institute holidays from DB (1-hour TTL)
- `bulk_display_names()` — resolves member names from slotbooking in one query
- `get_display_name()` — single-member name resolution with email fallback
- `clean_role()` — normalises raw role strings for display
- `safe_dict()` — converts a DB row dict, serialising dates to ISO strings

### `config.py` — Configuration

Database connection parameters, Flask secret key, session lifetime,
`STAFF_POSITIONS` frozenset (controls routing after login), and
`FULL_ACCESS_POSITIONS` (controls which users see admin-level data).

### `models/ai.py` — Context Builders + Template Narratives

Builds the flat context dicts (`_build_staff_context`, `_build_lab_context`)
that power all AI and template-based reports. Each context dict contains
~30 fields covering identity, attendance, equipment activity, ownership,
training, publications, and projects — all fetched from DB in one pass
with best-effort fallbacks (one section failure does not abort the rest).

Also contains `_narrative_staff()` and `_narrative_lab()` which produce
structured dict-based reports (identity, attendance, activity, research)
from the context dict without any LLM call.

### `models/staff.py` — Staff Data Queries

All queries against `hr_portal` and `slotbooking` for staff profiles.
Key functions:
- `get_all_members()` — cached 1-hour member list with bulk name resolution
- `get_person()` — full profile row + UID resolution + slot account enrichment
- `get_attendance_stats()` — days present, mandatory days, attendance %, cached 2 min
- `get_attendance_trend()` — month-by-month breakdown in ONE query (not 12), cached 2 min
- `get_slot_activity()` — equipment requests + reservation join, cached 5 min
- `get_staff_logbook_stats()` — UNION ALL across all `t_<machid>` tables, cached 5 min
- `_resolve_uid_uncached()` — 4-step email/name/ID fallback, cached 30 min
- `_warmup_uid()` — pre-populates UID cache before parallel query fan-out

### `models/lab.py` — Lab User Data Queries

All queries against `slotbooking` for lab user profiles.
Key functions:
- `get_all_lab_users()` — cached 1-hour list of active (non-expired) users
- `get_lab_user()` — single user with supervisor name join, cached 5 min
- `get_lab_stats()` — 4 counts in parallel (reservations, requests, papers, projects)
- `get_system_owner_tools()` — parses comma-separated machid strings into tool list
- `get_system_owner_track()` — pairs create/delete events into ownership spans
- `get_member_tool_permissions()` — enriched with faculty name and grant date

### `rag/ingest.py` — TF-IDF Index Builder

Builds the search index from two sources:
1. `.sql` dump files in the project root (schema + config context)
2. Live DB serialisations: all staff profiles, equipment usage patterns,
   leave rules and holidays, all lab users

Chunks text into 400-word overlapping windows, classifies each chunk
(equipment_activity / attendance / leave_rule / staff_profile / general),
extracts year and staff ID metadata, then fits a TF-IDF vectoriser
(bigrams, 60,000 features, sublinear TF). Pickles the index to
`tfidf_index.pkl` for fast reload on subsequent starts.

Run automatically on Flask startup via `init_rag()` in a background thread.

### `rag/retrieve.py` — Hybrid Retrieval

Scores chunks as a weighted combination of:
- TF-IDF cosine similarity (weight 0.8) — exact term matching
- spaCy/GloVe word vector cosine similarity (weight 0.2) — semantic matching

Falls back to TF-IDF only if the word vector backend fails to load.
Chunk vectors are cached in-process and invalidated when the index changes.
Applies score bonuses for name matches (+0.30), ID matches (+0.40),
and year matches (+0.10).

### `rag/pipeline.py` — LLM Integration

Builds prompts and calls the local GGUF model via llama-cpp-python.
Key functions:
- `rag_generate()` — non-streaming, used for batch `/api/ai/report`
- `rag_stream()` — streaming generator for SSE endpoint
- `rag_chat()` — single-turn Q&A with minimal focused prompt
- `rag_stream_executive()` — streaming for the executive briefing mode
- `_build_executive_prompt()` — 4-paragraph structured prompt template
- `_build_chat_prompt()` — keyword-filtered minimal prompt (prevents
  the 0.5B model from being confused by 30+ irrelevant facts)

### `rag/composer.py` — Template-Based Narrative Composer

Produces instant profile summaries **without any LLM call** using:
- A curated library of ~40 sentence templates (STAFF_TEMPLATES, LAB_TEMPLATES, SHARED_TEMPLATES)
- TF-IDF cosine similarity for selecting the best template variant per section
- A LogisticRegression classifier (trained on synthetic examples) to decide
  which sections to include based on data density
- Pre-computed pluralisation and conditional clause keys via `_enrich_ctx()`

This is the primary backend for the "Profile Summary" button — it returns
in under 50 ms regardless of LLM availability.

### `rag/agent.py` — Intent Detection & Routing

The entry point for all chat interactions. Classifies the user message
and routes to the appropriate handler:
- Tier 0: `tier0.py` — exact dict lookup, 0 ms
- Tier 1: `query_router.py` — structured DB query, ~50–200 ms
- Tier 1.5: `facility_router.py` — static knowledge base answers
- Tier 2: `rag_chat()` in pipeline.py — LLM inference, 1–5 s

### `rag/tier0.py` — Zero-Model Factual Lookup

25 LOOKUP_RULES, each with regex patterns, required context fields,
and a formatter lambda. Covers attendance, leaves, equipment, reservations,
permissions, ownership, publications, projects, training, logbook, and more.
Returns answers in under 1 ms for any question directly answerable from
the context dict. No DB calls, no model calls.

### `rag/query_router.py` — Structured Query Handler

Handles questions requiring year-specific or multi-year DB queries:
attendance by year, equipment activity by year, multi-year comparisons,
monthly breakdowns, tool-specific usage, leave by year, publications by year,
cancellations, training, projects, and permissions. Returns formatted text
answers deterministically from the database.

### `rag/facility_router.py` — Facility Knowledge Base

Static answers for questions about IITBNF itself: what the facility is,
team roles, booking process, equipment categories, attendance policy,
contact guide, user categories. Also supports live DB queries for
current staff count, lab user count, equipment count, and announcements.

### `rag/data_gatherer.py` — Structured Data Pre-Fetcher

Detects what structured data a question needs and fetches it from DB
before passing to the LLM. The LLM only formats pre-fetched data —
it never queries the DB. Covers attendance comparisons, single-year
attendance, equipment comparisons, ownership, and leave breakdowns.

### `routes/profile.py` — Staff Profile Routes

`/profile/<id>` — serves the staff profile page with minimal initial data.
`/profile/<id>/pdf/prefetch` — starts background PDF generation silently on page load.
`/profile/<id>/pdf/start` — on-demand PDF start (reuses prefetch if available).
`/profile/pdf/status/<job_id>` — polling endpoint for PDF progress.
`/profile/pdf/download/<job_id>` — serves the completed PDF file.
Also handles system owner PDF and ownership track PDF with the same async pattern.

### `routes/lab_profile.py` — Lab Profile Routes

Same async PDF pattern as profile.py, applied to lab user profiles.
`/lab/<memberid>` — parallel-loads all 12 data sections using `run_parallel`.

### `routes/section_routes.py` — AJAX Section Endpoints

All `/api/section/staff/<id>/<section>` and `/api/section/lab/<id>/<section>`
endpoints returning JSON for individual profile cards.
Returns `Cache-Control: private, max-age=60` headers to prevent browser
re-fetching on rapid dropdown changes.
Includes `/api/section/tool/<machid>/session_log` for per-tool logbook data.
Includes `/api/section/staff/<id>/logbook` for the logbook entries card.

### `routes/ai_routes.py` — AI API Endpoints

`/api/ai/stream` — SSE endpoint for the floating chat bubble.
`/api/ai/compose` — SSE endpoint for the AI Profile tab (short and executive modes).
`/api/ai/session-digest` — SSE endpoint for per-tool session report summaries.
`/api/ai/admin-chat` — SSE endpoint for the admin panel AI assistant.
`/api/ai/logbook-explain` — SSE endpoint for AI explanation of logbook entries.
All use background threads + queues to avoid blocking the WSGI worker.

### `routes/debug.py` — Performance Diagnostics

`/debug/timings?member_id=<id>` — measures wall-clock time of every DB
function called during a profile page load. Accepts `&cold=1` to bypass
cache. Returns sorted JSON with per-function ms timings.
`/debug/db-test` — confirms both DB connections are alive.
`/debug/speed-dashboard` — shows cache key count and pool active/queue stats.
`/debug/ai/full/<type>/<id>` — runs the complete AI pipeline step by step
and reports success/failure at each stage.

---

## Performance Improvements

The following optimisations were implemented with measurable impact:

| Improvement | Before | After | Notes |
|---|---|---|---|
| Attendance stats cached (2 min) | ~1,500 ms per AJAX call | ~5 ms (cache hit) | Every year-dropdown change was re-running the full attendance query |
| Attendance trend cached (2 min) | ~6,000 ms per AJAX call | ~10 ms (cache hit) | Trend loops 12 months × `get_holidays()` — extremely expensive uncached |
| `get_available_years` cached (5 min) | ~800 ms | ~2 ms | 4 DISTINCT queries merged into 1 UNION query and cached |
| Parallel section loading (`run_parallel`) | ~12,000 ms sequential | ~1,500 ms | 8+ DB queries fired concurrently using `ThreadPoolExecutor` |
| `_warmup_uid()` before fan-out | 4 × ~300 ms UID resolution | 1 × ~300 ms, rest ~0 ms | Pre-populates in-process UID cache; subsequent tasks find it instantly |
| `get_all_members` cached (1 hour) + `bulk_display_names` | ~800 ms × every admin panel load | ~2 ms after first load | Was calling `get_display_name()` in a Python loop — replaced with a single batched slotbooking query |
| Slot activity cached (5 min) | ~2,000 ms per AJAX call | ~8 ms (cache hit) | Correlated subquery on reservations was expensive; now cached per (member_id, year) |
| Attendance trend: 1 query instead of 12 | 12 × ~500 ms | 1 × ~500 ms | `get_attendance_rows()` fetches all rows for the year; Python loops filter by month |
| Logbook stats: UNION ALL instead of N queries | N × ~200 ms (N = tools used) | 1 × ~600 ms | Single UNION ALL across all `t_<machid>` tables joined to reservations |
| `system_owner_track` N+1 replaced | N COUNT(*) queries | 1 GROUP BY query | Fixed in `get_staff_owner_track()` — single GROUP BY across all candidates |
| PDF pre-generation on page load | 5–30 s after user clicks | ~0 ms download (PDF already done) | `prefetch` endpoint fires on page load; user typically clicks well after completion |
| Tier-0 lookup gate | 1–5 s LLM inference for every question | ~1 ms for 70% of questions | 25 regex rules + lambda formatters handle most factual queries without model |
| NarrativeComposer (short summaries) | 3–8 s LLM generation | ~30–50 ms (no LLM) | TF-IDF + LogReg template selection; result is deterministic and instant |

---

## Limitations & Resolutions

### 1. In-memory cache not shared across processes

**Problem:** The `SimpleCache` in `cache.py` is per-process. Running Gunicorn
with `--workers=4` means 4 independent caches — each worker cold-starts
independently, and `get_all_members()` (800 ms) runs once per worker on first request.

**Resolution:** Replace `cache.py` with a Redis-backed cache:
```bash
pip install redis flask-caching
```
Configure `CACHE_TYPE = "RedisCache"` in `config.py` and update the `@cached`
decorator to use Flask-Caching. All workers will then share a single cache.

### 2. Temporary PDF files accumulate

**Problem:** PDF files written to `tmp/` are never deleted. On a busy server
this directory can grow to several GB over weeks.

**Resolution:** Add a cleanup routine in `app.py` that runs periodically:
```python
import threading, time, os, glob

def _cleanup_tmp():
    while True:
        time.sleep(3600)  # run every hour
        cutoff = time.time() - 7200  # delete files older than 2 hours
        for f in glob.glob("tmp/*.pdf"):
            if os.path.getmtime(f) < cutoff:
                os.remove(f)

threading.Thread(target=_cleanup_tmp, daemon=True).start()
```

### 3. LLM inference is serialised

**Problem:** `_inference_lock` in `llm.py` ensures only one LLM request runs
at a time. Concurrent AI requests queue behind each other — a busy period
with 5 simultaneous users asking the AI panel will result in the 5th user
waiting 5× the inference time.

**Resolution:** For higher concurrency, run the GGUF model as a separate
microservice (e.g., via Ollama or llama-cpp-python's OpenAI-compatible server)
and configure multiple model instances:
```bash
ollama serve  # starts on port 11434
# Or llama-cpp-python server:
python -m llama_cpp.server --model models/qwen.gguf --n_gpu_layers 35 --port 8080
```
Update `llm.py` to call the HTTP API instead of the in-process library,
allowing the OS to schedule multiple inference requests.

### 4. Authentication is disabled

**Problem:** All route guards in `auth.py` are pass-through. Any user with
network access can view any profile.

**Resolution:** Re-enable session checks in `auth.py`. Add to each decorator:
```python
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("memberid"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated
```
For HTTPS deployments also set `SESSION_COOKIE_SECURE = True` and
`SESSION_COOKIE_HTTPONLY = True` in `config.py`.

### 5. The 0.5B SLM hallucinates on complex questions

**Problem:** Qwen 2.5 0.5B is a very small model. Without Tier-0 and the
query router intercepting factual questions, it will sometimes fabricate
numbers not present in the context.

**Resolution:**
- The `_validate_response()` function in `pipeline.py` already strips sentences
  containing numbers not found in the context dict.
- For better general quality, upgrade to Qwen 2.5 1.5B or 3B (increase
  `n_gpu_layers` and `n_ctx` in `llm.py`).
- Extend the `LOOKUP_RULES` in `tier0.py` to cover more question patterns
  so fewer questions reach the LLM.

### 6. `sync_expiry.py` must be run manually

**Problem:** When a staff member's `leaving_date` is updated in `hr_portal`,
their slotbooking `expiry_date` is not automatically updated — they may still
appear as active lab users.

**Resolution:** Schedule `sync_expiry.py` as a cron job:
```bash
# Linux/macOS crontab — run daily at 2 AM
0 2 * * * cd /path/to/iitbnf && python sync_expiry.py >> logs/sync.log 2>&1

# Windows Task Scheduler
schtasks /create /tn "IITBNF Sync Expiry" /tr "python C:\iitbnf\sync_expiry.py" /sc daily /st 02:00
```

### 7. xhtml2pdf cannot handle complex CSS

**Problem:** xhtml2pdf does not support CSS flexbox, grid, or many modern
layout properties. Complex templates must be rewritten using HTML tables with
`table-layout: fixed` and explicit column widths.

**Resolution:** For higher-quality PDFs, replace xhtml2pdf with WeasyPrint:
```bash
pip install weasyprint
```
WeasyPrint supports modern CSS including flexbox. Update `_html_to_pdf()` in
`profile.py` and `lab_profile.py`:
```python
from weasyprint import HTML

def _html_to_pdf(html_string: str) -> bytes:
    return HTML(string=html_string).write_pdf()
```
Note: WeasyPrint requires system libraries (`libpango`, `libcairo`) that may
need separate installation on Linux via `apt-get install libpango-1.0-0 libcairo2`.

### 8. TF-IDF index does not update live

**Problem:** The RAG index is built once at startup from a DB snapshot.
New staff members or updated records are not reflected until the next restart.

**Resolution:** Add a scheduled re-ingestion task:
```python
# In app.py, after init_rag():
import schedule

def _reingest():
    from rag.ingest import init_rag
    init_rag(force=True)

schedule.every(6).hours.do(_reingest)

def _schedule_worker():
    while True:
        schedule.run_pending()
        time.sleep(60)

threading.Thread(target=_schedule_worker, daemon=True).start()
```

---

## Deployment: Connecting to Any Server or OS

### Windows (local or server)

Windows is the primary development target. The app runs natively with no
additional setup beyond Python and the dependencies listed above.

```bash
# Install Waitress (pure Python, no C compiler needed)
pip install waitress

# Start production server
waitress-serve --host=0.0.0.0 --port=5000 --threads=8 app:app

# Or run as a Windows Service using NSSM (Non-Sucking Service Manager):
# Download nssm.exe from https://nssm.cc
nssm install IITBNF "C:\Python311\python.exe" "-m waitress --host=0.0.0.0 --port=5000 --threads=8 app:app"
nssm set IITBNF AppDirectory "C:\path\to\iitbnf"
nssm start IITBNF
```

For GPU-accelerated LLM inference on Windows with an NVIDIA GPU:
1. Install CUDA Toolkit 12.x from nvidia.com
2. Reinstall llama-cpp-python with CUDA support:
```bash
$env:CMAKE_ARGS="-DLLAMA_CUBLAS=on"
pip install llama-cpp-python --force-reinstall --no-cache-dir
```

### Linux (Ubuntu / Debian)

```bash
# System dependencies for WeasyPrint (if used instead of xhtml2pdf)
sudo apt-get install libpango-1.0-0 libcairo2 libgdk-pixbuf2.0-0

# MariaDB client libraries (for mysql-connector-python C extension)
sudo apt-get install libmariadb-dev

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run with Gunicorn
gunicorn --workers=4 --threads=4 --bind=0.0.0.0:5000 \
         --timeout=120 --worker-class=gthread \
         --access-logfile logs/access.log app:app

# Or as a systemd service:
sudo tee /etc/systemd/system/iitbnf.service << EOF
[Unit]
Description=IITBNF Personnel Profiling System
After=network.target mariadb.service

[Service]
User=www-data
WorkingDirectory=/opt/iitbnf
ExecStart=/opt/iitbnf/venv/bin/gunicorn --workers=4 --threads=4 \
          --bind=0.0.0.0:5000 --timeout=120 app:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable iitbnf
sudo systemctl start iitbnf
```

### macOS

```bash
# Homebrew dependencies
brew install python@3.11 mariadb

# GPU inference (Apple Silicon M-series)
CMAKE_ARGS="-DLLAMA_METAL=on" pip install llama-cpp-python --force-reinstall

# Run with Waitress or Gunicorn
waitress-serve --host=0.0.0.0 --port=5000 --threads=8 app:app
```

### Docker

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libmariadb-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m spacy download en_core_web_md

COPY . .

EXPOSE 5000
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=8", "app:app"]
```

```bash
docker build -t iitbnf-app .
docker run -p 5000:5000 \
  -e DB_HOST=host.docker.internal \
  -e DB_USER=root \
  -e DB_PASS=yourpassword \
  -v $(pwd)/models:/app/models \
  iitbnf-app
```

### Connecting to a Remote Database

If the MariaDB server is on a different machine, update `config.py`:

```python
DB_HR = {
    "host":     "192.168.1.100",    # DB server IP or hostname
    "port":     3306,
    "user":     "iitbnf_user",
    "password": "secure_password",
    "database": "hr_portal",
    "charset":  "utf8mb4",
}
```

For SSL connections (recommended for remote DB):

```python
DB_HR = {
    "host": "db.example.com",
    "ssl":  {"ca": "/path/to/ca-cert.pem"},
    ...
}
```

### Connecting the GGUF Model via Ollama Server

To run the LLM on a separate GPU machine and serve it over the network:

**On the GPU machine:**
```bash
pip install ollama
ollama pull qwen2.5:0.5b   # or your fine-tuned model
OLLAMA_HOST=0.0.0.0 ollama serve
```

**In `llm.py` on the Flask server:**
```python
import requests

OLLAMA_URL = "http://gpu-machine-ip:11434/api/generate"

def llm_generate(prompt: str, max_tokens: int = 500) -> str:
    response = requests.post(OLLAMA_URL, json={
        "model":  "qwen2.5:0.5b",
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.2}
    }, timeout=120)
    return response.json().get("response", "")
```

This completely decouples the web server from the model — the Flask app
can run on a low-spec VM while a dedicated GPU machine handles inference.
