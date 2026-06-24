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
<img src="Architecture_1.png " width = "100%">
<img src="Architecture_2.png " width = "100%">
<img src="Architecture_3.png " width = "100%">
<img src="Architecture_4.png " width = "100%">

** The finetune.py file has been removed, so the fine tuning procedure can be done easily in Google Colab
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
pairs that teach the model to answer factual profile questions. That is achieved by running the generate_training_data.py program.

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

