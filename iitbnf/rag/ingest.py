"""
rag/ingest.py — TF-IDF ingestion pipeline
==========================================
Sources:
  1. .sql dump files in documents/  → schema + config context
  2. Live DB queries                → staff, equipment, leave data

Builds a TF-IDF index over all chunks and pickles it to disk.
No ChromaDB or embedding model required.

Auto-runs on Flask startup via init_rag() called from app.py.

"""

import re
import logging
import pickle
import time
from pathlib import Path
from collections import defaultdict
import re

from sklearn.feature_extraction.text import TfidfVectorizer

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DOCUMENTS_DIR = Path(__file__).parent.parent 
INDEX_PATH    = Path(__file__).parent.parent / "tfidf_index.pkl"
CHUNK_SIZE    = 400
CHUNK_OVERLAP = 50

# ── Singletons ────────────────────────────────────────────────────────────────
_vectorizer: TfidfVectorizer | None = None
_matrix                             = None   # scipy sparse (n_chunks × n_features)
_chunks: list[dict]                 = []     # [{"text", "source", "chunk_index"}]
def extract_year(text: str):
    year_match = re.search(r"\b(20\d{2})\b", text)
    if year_match:
        return year_match.group(1)
    return None 
def extract_staff_id(text: str):
    id_match = re.search(r"\bID[:\s]?(\d{3,6})\b", text, re.I)
    if id_match:
        return id_match.group(1)
    return None
def extract_staff_name(text: str):
    name_match = re.search(r"\bName[:\s]?([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b", text)
    if name_match:
        return name_match.group(1)
    return None
def classify_chunk_type(source: str, text: str) -> str:
    t = text.lower()

    if "equipment" in t or "requests" in t or "booking" in t:
        return "equipment_activity"

    if "attendance" in t or "present" in t or "%" in t:
        return "attendance"

    if "leave" in t or "pl:" in t or "el:" in t or "rl:" in t:
        return "leave_rule"

    if "designation" in t or "team" in t or "qualification" in t:
        return "staff_profile"

    return "general"

def should_skip_chunk(text: str) -> bool:
    t = text.lower()

    skip_patterns = [
        "phpmyadmin sql dump",
        "table structure for table",
        "dumping data for table",
        "create table",
        "insert into",
        "alter table",
        "primary key",
        "auto_increment",
        "engine=",
    ]

    if any(pattern in t for pattern in skip_patterns):
        return True

    if t.count("`") > 10:
        return True

    if len(t.split()) < 5:
        return True

    return False
# ── [FIX 1] Safe DB import — won't crash if db module is not on sys.path ──────
def _get_db_funcs():
    """
    Lazily import hr_query / slots_query from db.py.
    Returns (hr_query, slots_query) or (None, None) if the module is missing.
    This prevents an ImportError from killing the process at module load time
    when ingest.py is imported outside the normal Flask app context.
    """
    try:
        from db import hr_query, slots_query  # noqa: PLC0415
        return hr_query, slots_query
    except ImportError:
        logger.error(
            "Could not import 'db' module. Make sure you are running the app "
            "from the iitbnf/ root directory (not from iitbnf/rag/). "
            "Live DB serialization will be skipped."
        )
        return None, None

# ── Index accessor ────────────────────────────────────────────────────────────
def get_index():
    """
    Load the pickled TF-IDF index into memory (once per process).
    Returns (vectorizer, matrix, chunks) — always a 3-tuple.
    Returns (None, None, []) if the index has not been built yet.
    """
    global _vectorizer, _matrix, _chunks
    if _vectorizer is not None:
        return _vectorizer, _matrix, _chunks

    if INDEX_PATH.exists():
        logger.info("Loading TF-IDF index from %s", INDEX_PATH)
        t0 = time.perf_counter()
        try:
            with open(INDEX_PATH, "rb") as f:
                data = pickle.load(f)
            _vectorizer = data["vectorizer"]
            _matrix     = data["matrix"]
            _chunks     = data["chunks"]
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            logger.info("Index loaded: %d chunks in %s ms", len(_chunks), elapsed)
        except Exception as e:
            logger.error("Failed to load TF-IDF index from disk: %s — will rebuild.", e)
    else:
        logger.warning("TF-IDF index not found at %s — run init_rag() first.", INDEX_PATH)

    return _vectorizer, _matrix, _chunks

# ── [FIX 5] Canonical collection_size — correctly unpacks the 3-tuple ─────────
def collection_size() -> int:
    """
    Number of indexed chunks.
    Imported by pipeline.py and retrieve.py for health checks and guards.

    IMPORTANT: get_index() returns (vectorizer, matrix, chunks).
    The old copy in retrieve.py did `chunks = get_index()` then `len(chunks)`,
    which always returned 3 (length of the tuple) — so the index was always
    seen as non-empty, causing retrieve() to run on an unloaded index and
    return garbage or crash silently.
    """
    _, _, chunks = get_index()
    return len(chunks)

# ── [FIX 6] Chunking — skip blank texts ──────────────────────────────────────
def chunk_text(text: str, source: str) -> list[dict]:
    """Split text into overlapping word-count chunks. Skips empty inputs."""
    if not text or not text.strip():
        return []
    words  = text.split()
    chunks = []
    start  = 0
    idx    = 0
    while start < len(words):
        end   = min(start + CHUNK_SIZE, len(words))
        chunk = " ".join(words[start:end])
        if chunk.strip():
            if not should_skip_chunk(chunk):   # check the chunk itself, not full text
                chunk_type = classify_chunk_type(source, chunk)  # also use chunk here
                year       = extract_year(chunk)
                staff_id   = extract_staff_id(chunk)
                staff_name = extract_staff_name(chunk)
                chunks.append({
                    "text": chunk, "source": source, "chunk_index": idx,
                    "type": chunk_type, "staff_id": staff_id,
                    "staff_name": staff_name, "year": year,
                })
        start += CHUNK_SIZE - CHUNK_OVERLAP   # always advance — outside the if block
        idx   += 1
    return chunks

# ── [FIX 2] Build + persist — guard against empty chunk list ─────────────────
def build_index(all_chunks: list[dict]):
    """Fit TF-IDF on all chunks and pickle to disk."""
    global _vectorizer, _matrix, _chunks

    if not all_chunks:
        logger.error(
            "build_index() called with zero chunks — nothing to index. "
            "Check DB connectivity and documents/ directory."
        )
        return

    texts = [c["text"] for c in all_chunks]
    logger.info("Fitting TF-IDF on %d chunks...", len(texts))
    t0 = time.perf_counter()

    _vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),
        max_features=60_000,
        sublinear_tf=True,
        stop_words="english",
    )
    _matrix = _vectorizer.fit_transform(texts)
    _chunks = all_chunks

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    logger.info("TF-IDF matrix: %s in %s ms", str(_matrix.shape), elapsed)

    try:
        with open(INDEX_PATH, "wb") as f:
            pickle.dump({"vectorizer": _vectorizer, "matrix": _matrix, "chunks": _chunks}, f)
        logger.info("Index saved to %s", INDEX_PATH)
    except Exception as e:
        logger.error("Could not pickle index to disk: %s — index lives in memory only.", e)

# ════════════════════════════════════════════════════════════════════════════
# SOURCE 1 — SQL DUMP FILES
# ════════════════════════════════════════════════════════════════════════════
def extract_sql_text(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.upper().startswith(kw) for kw in [
            "SET ", "LOCK ", "UNLOCK ", "USE ", "DROP TABLE",
            "/*!",  "ENGINE=", "DEFAULT CHARSET",
            "AUTO_INCREMENT=", "COLLATE"
        ]):
            continue
        if stripped.startswith("--"):
            lines.append(stripped.lstrip("- ").strip())
            continue
        if "CREATE TABLE" in stripped.upper():
            table = re.search(r'CREATE TABLE `?(\w+)`?', stripped, re.I)
            if table:
                lines.append(f"Table: {table.group(1)}")
            continue
        col = re.match(r'`(\w+)`\s+(\w+)', stripped)
        if col:
            lines.append(f"Column {col.group(1)}: type {col.group(2)}")
            continue
        if stripped.upper().startswith("INSERT INTO"):
            table = re.search(r'INSERT INTO `?(\w+)`?', stripped, re.I)
            vals  = re.findall(r"\(([^)]+)\)", stripped)
            if table and vals:
                for v in vals[:20]:
                    lines.append(f"{table.group(1)} record: {v}")
            continue
    return "\n".join(lines)

def ingest_sql_files() -> list[dict]:
    sql_files = list(DOCUMENTS_DIR.glob("*.sql")) if DOCUMENTS_DIR.exists() else []
    if not sql_files:
        logger.info("No .sql files found in %s — skipping SQL ingestion.", DOCUMENTS_DIR)
        return []
    all_chunks = []
    for path in sql_files:
        logger.info("Ingesting SQL file: %s", path.name)
        try:
            raw  = path.read_text(encoding="utf-8", errors="ignore")
            text = extract_sql_text(raw)
            if not text.strip():
                logger.warning("Empty extraction from: %s", path.name)
                continue
            all_chunks.extend(chunk_text(text, source=path.name))
        except Exception as e:
            logger.error("Failed to ingest SQL file %s: %s", path.name, e)
    return all_chunks
def ingest_text_files() -> list[dict]:
    """
    Ingest plain text knowledge documents from documents/ directory.
    These provide facility-specific context that enriches RAG responses.
    """
    txt_files = list(DOCUMENTS_DIR.glob("**/*.txt")) if DOCUMENTS_DIR.exists() else []
    if not txt_files:
        logger.info("No .txt files found — skipping text ingestion.")
        return []
    
    all_chunks = []
    for path in txt_files:
        logger.info("Ingesting text file: %s", path.name)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                continue
            chunks = chunk_text(text, source=path.name)
            all_chunks.extend(chunks)
            logger.info("  → %d chunks from %s", len(chunks), path.name)
        except Exception as e:
            logger.error("Failed to ingest %s: %s", path.name, e)
    
    return all_chunks
# ════════════════════════════════════════════════════════════════════════════
# SOURCE 2 — LIVE DB QUERIES
# [FIX 7] Each serializer returns a safe fallback on DB error instead of
#          raising — a bad DB connection cannot abort the whole ingestion run.
# ════════════════════════════════════════════════════════════════════════════

def serialize_staff_profiles() -> str:
    hr_query, _ = _get_db_funcs()
    if hr_query is None:
        return ""
    try:
        rows = hr_query("""
            SELECT p.member_id, p.designation, p.team,
                   p.type_of_appointment, p.qualification, p.joining_date,
                   COALESCE(rm.role_name, 'Staff') AS role_name,
                   TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS display_name
            FROM profile p
            LEFT JOIN role r         ON r.memberid = p.member_id
            LEFT JOIN role_master rm ON rm.role_id = r.role
            LEFT JOIN slotbooking.login l ON l.memberid = p.member_id
            WHERE (p.leaving_date IS NULL OR p.leaving_date = '0000-00-00'
                   OR p.leaving_date >= CURDATE())
            ORDER BY p.member_id
        """)
    except Exception as e:
        logger.error("serialize_staff_profiles DB error: %s", e)
        return ""

    lines = ["SECTION: Staff Profiles\n"]
    for s in (rows or []):
        name = (s.get('display_name') or '').strip() or f"Member {s['member_id']}"
        line = (
            f"{name} (ID {s['member_id']}) is a {s['role_name'] or 'staff member'}"
            f"{' — ' + s['designation'] if s['designation'] else ''}."
            f"{' Team: ' + s['team'] + '.' if s['team'] else ''}"
            f"{' Appointment: ' + s['type_of_appointment'] + '.' if s['type_of_appointment'] else ''}"
            f"{' Qualification: ' + s['qualification'] + '.' if s['qualification'] else ''}"
            f" Joined: {s['joining_date'] or 'unknown'}."
        )
        lines.append(line)
    return "\n".join(lines)

def serialize_equipment_usage() -> str:
    _, slots_query = _get_db_funcs()
    if slots_query is None:
        return ""
    try:
        rows = slots_query("""
            SELECT l.memberid, l.fname, l.lname,
                   r.name AS tool_name,
                   COUNT(e.request_id) AS booking_count,
                   MAX(e.date_of_request) AS last_used
            FROM equipment_usage_approval e
            JOIN login l ON l.memberid = e.requestedby
            JOIN resources r ON r.machid = e.equipmentid
            GROUP BY l.memberid, r.machid
            ORDER BY l.memberid, booking_count DESC
        """)
    except Exception as e:
        logger.error("serialize_equipment_usage DB error: %s", e)
        return ""

    user_tools: dict = defaultdict(list)
    user_names: dict = {}
    for row in (rows or []):
        mid = row['memberid']
        user_names[mid] = f"{row['fname']} {row['lname']}".strip()
        user_tools[mid].append(
            f"{row['tool_name']} ({row['booking_count']} requests, last: {row['last_used']})"
        )
    lines = ["SECTION: Equipment Usage Patterns\n"]
    for memberid, tools in user_tools.items():
        name = user_names.get(memberid, f"User {memberid}")
        lines.append(f"{name} has used: {', '.join(tools)}.")
    return "\n".join(lines)

def serialize_leave_rules() -> str:
    hr_query, _ = _get_db_funcs()
    if hr_query is None:
        return ""
    try:
        max_leaves = hr_query("""
            SELECT ml.type_of_leave, ml.max_leaves,
                   COALESCE(rm.role_name, 'All staff') AS role_name
            FROM max_leaves ml
            LEFT JOIN role r         ON r.memberid = ml.memberid
            LEFT JOIN role_master rm ON rm.role_id = r.role
            ORDER BY role_name, ml.type_of_leave
        """)
        holidays = hr_query("""
            SELECT holiday_date, holiday_desc
            FROM institute_holidays
            ORDER BY holiday_date DESC
            LIMIT 50
        """)
    except Exception as e:
        logger.error("serialize_leave_rules DB error: %s", e)
        return ""

    lines = ["SECTION: Leave and Holiday Rules\n"]
    if max_leaves:
        lines.append("Leave Entitlements:")
        for ml in max_leaves:
            lines.append(
                f"{ml['role_name']} — "
                f"{ml['type_of_leave']}: max {ml['max_leaves']} days."
            )
    if holidays:
        lines.append("\nInstitute Holidays (recent):")
        for h in holidays:
            lines.append(f"{h['holiday_date']}: {h['holiday_desc']}")
    return "\n".join(lines)

def serialize_lab_users() -> str:
    """Serialize lab user profiles from slotbooking.login into RAG context."""
    _, slots_query = _get_db_funcs()
    if slots_query is None:
        return ""
    try:
        rows = slots_query("""
        SELECT l.memberid, l.fname, l.lname, l.email,
           l.position, l.department,
           COALESCE(ra.name, l.research_area) AS research_area,
           l.rollno,
           TRIM(CONCAT(COALESCE(s.fname,''), ' ', COALESCE(s.lname,''))) AS supervisor_name
        FROM login l
        LEFT JOIN login s ON s.memberid = l.supervisor
        LEFT JOIN research_areas ra ON ra.id = l.research_area
        WHERE (
            l.expiry_date IS NULL
            OR l.expiry_date = ''
            OR l.expiry_date = '0000-00-00'
            OR COALESCE(STR_TO_DATE(l.expiry_date, '%%m/%%d/%%Y'), CURDATE()) >= CURDATE()
        )
        ORDER BY l.memberid
        """)
    except Exception as e:
        logger.error("serialize_lab_users DB error: %s", e)
        return ""

    lines = ["SECTION: Lab Users\n"]
    for u in (rows or []):
        name = f"{u.get('fname','')} {u.get('lname','')}".strip() or f"User {u['memberid']}"
        line = f"{name} (ID {u['memberid']}) is a lab user"
        if u.get('position'):
            line += f" with position {u['position']}"
        if u.get('department'):
            line += f" in the {u['department']} department"
        if u.get('research_area') and u['research_area'] not in ('', 'NA', 'N/A'):
            line += f". Research area: {u['research_area']}"
        sup = (u.get('supervisor_name') or '').strip()
        if sup:
            line += f". Supervisor: {sup}"
        if u.get('rollno') and u['rollno'] not in ('', '0', 'NA'):
            line += f". Roll no: {u['rollno']}"
        line += "."
        lines.append(line)
    return "\n".join(lines)
# ════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════
_ingested = False

def _prewarm_vectors(chunks: list[dict]):
    """
    Pre-compute and cache spaCy / GloVe vectors for all indexed chunks.
    Called once at the end of init_rag(). Non-fatal if it fails.
    """
    try:
        from pathlib import Path
        import sys
        # Ensure rag/ is on sys.path regardless of where Flask was launched from
        rag_dir = str(Path(__file__).parent)
        if rag_dir not in sys.path:
            sys.path.insert(0, rag_dir)

        from retrieve import _get_chunk_vecs, WORD_VEC_BACKEND  # noqa: PLC0415
        if not chunks:
            logger.warning("Pre-warm skipped — no chunks provided.")
            return
        logger.info("Pre-warming word vectors (%s) for %d chunks...", WORD_VEC_BACKEND, len(chunks))
        _get_chunk_vecs(WORD_VEC_BACKEND, chunks)
        logger.info("Word vector pre-warm complete.")
    except Exception as e:
        logger.warning("Vector pre-warm failed (non-fatal, will warm on first request): %s", e)
def init_rag(force: bool = False):
    """
    Entry point called from app.py on Flask startup.
    Runs ingestion once per process. Set force=True to re-ingest.

    [FIX 3] Corrected the early-return logic:
      - If already ingested AND index is in memory  → return immediately.
      - If already ingested BUT index not in memory → load from disk (get_index).
      - If not yet ingested (or force=True)         → full re-ingestion run.
    """
    global _ingested

    if _ingested and not force:
        if _vectorizer is not None:
            # Index already in memory — nothing to do.
            return
        # Index was ingested in a previous call but got evicted or
        # this is a fresh worker process — load from disk.
        get_index()
        return

    _ingested = True

    logger.info("RAG ingestion starting (TF-IDF)...")
    all_chunks: list[dict] = []

    # Source 1 — SQL dump files
    all_chunks.extend(ingest_sql_files())
    # Source 2 — Text knowledge documents  
    all_chunks.extend(ingest_text_files())

    # Source 3 — Live DB serializations
    live_sources = {
        "live:staff_profiles":  serialize_staff_profiles,
        "live:equipment_usage": serialize_equipment_usage,
        "live:leave_rules":     serialize_leave_rules,
        "live:lab_users":       serialize_lab_users,
    }
    for source_key, fn in live_sources.items():
        logger.info("Serializing: %s", source_key)
        try:
            text = fn()
            if text:
                all_chunks.extend(chunk_text(text, source=source_key))
        except Exception as e:
            logger.error("Failed to serialize %s: %s", source_key, e)

    if not all_chunks:
        logger.error(
            "Ingestion produced zero chunks — index will not be built. "
            "Verify DB connectivity and that documents/ contains .sql files."
        )
        return

    logger.info("Total chunks: %d — building index...", len(all_chunks))
    build_index(all_chunks)
    # And in init_rag(), pass chunks after build_index():
    logger.info("RAG ingestion complete.")

    # [FIX 4] Pre-warm word vectors so the first real request is not slow.
    # This vectorises all chunks immediately after ingestion while the server
    # is still starting up, instead of blocking on the first user request.

    _prewarm_vectors(all_chunks)   # ✅ pass in-memory chunks directly
    logger.info("Final chunk count: %d", len(all_chunks))
    for c in all_chunks[:10]:
        logger.info("Chunk source=%s text=%s", c["source"], c["text"][:150])