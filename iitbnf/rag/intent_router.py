"""
rag/intent_router.py — Two-layer intent router
================================================
Layer 1: Regex rules  — fast, zero cost, handles ~80% of queries
Layer 2: MiniLM       — semantic fallback for paraphrases regex misses

Public API:
    classify_intent(query) → str  (intent label)
    warm_up()              → None (pre-loads MiniLM at startup)

Intent labels map to handlers in agent.py:
    "attendance"         → _attendance_year / _compare_attendance
    "compare_slot"       → _compare_slot_activity
    "compare_attend"     → _compare_attendance
    "equipment_count"    → _slot_activity_year
    "equipment_list"     → _tool_specific_usage
    "publication"        → _publications_year
    "project"            → _project_summary
    "training"           → _training_summary
    "cancellation"       → _cancellation_summary
    "permission"         → _list_permissions
    "system_owner"       → system ownership handler
    "monthly_report"     → monthly report handler
    "admin_stats"        → facility_router
    "general_profile"    → tier0 / general fallback
"""

import re
import logging
import threading
import numpy as np
# At the top of intent_router.py, BEFORE the sentence_transformers import:
import os
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
logger = logging.getLogger(__name__)

# ── Layer 1: Regex Rules ──────────────────────────────────────────────────────
# Order matters — more specific patterns first.
# Each tuple: (compiled_pattern, intent_label)

_RULES = [
    # ── Compare patterns (must come before single-year patterns) ──────────────
    # Replace the existing compare rule with two more specific ones:
    # ADD this BEFORE the existing attendance rule in _RULES:
    (re.compile(
        r"\b(attendance policy|leave policy|mandatory threshold|75 percent|75%|"
        r"working hour|working day of iitbnf|iitbnf hour|iitbnf time|iitbnf day|"
        r"iitbnf schedule|iitbnf timing|iitbnf work|"
        r"where is iitbnf|about iitbnf|what is iitbnf|iitbnf located|"
        r"how many (staff|user|member|lab user)|total (staff|user|member)|"
        r"active (staff|user)|headcount)\b",
        re.I
    ), "facility_info"),
    (re.compile(
        r"(\bcompare\b|\bvs\b|\bversus\b|\bdifference between\b|\bchange from\b)"
        r".{0,40}(attend|present|day|regular)",
        re.I
    ), "compare_attend"),

    (re.compile(
        r"(\bcompare\b|\bvs\b|\bversus\b|\bdifference between\b|\bchange from\b|\bmore regular\b|\bless regular\b)"
        r".{0,40}(slot|equipment|request|booking|reservation)",
        re.I
    ), "compare_slot"),

    # Generic compare fallback (no keyword after compare verb):
    (re.compile(
        r"(\bcompare\b|\bvs\b|\bversus\b|\bdifference between\b|\bchange from\b|\bmore regular\b|\bless regular\b)",
        re.I
    ), "compare"),

    # ── Year + activity (triggers query_router with year context) ─────────────
    (re.compile(
        r"\b20\d{2}\b.{0,40}(slot|equipment|request|booking|reservation|active|usage)",
        re.I
    ), "equipment_year"),
    (re.compile(
        r"(slot|equipment|request|booking|reservation|active|usage).{0,40}\b20\d{2}\b",
        re.I
    ), "equipment_year"),
    # -- Attendance patterns with month context (e.g. "January attendance") — must come before generic attendance rule:
    (re.compile(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\b.{0,30}(attend|present|days?)",
        re.I
    ), "attendance_monthly"),
    (re.compile(
        r"(attend|present|days?).{0,30}\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
        r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        re.I
    ), "attendance_monthly"),
    # -- Year + attendance patterns:
    (re.compile(
        r"\b20\d{2}\b.{0,40}(attend|present|days?|working day)",
        re.I
    ), "attendance_year"),
    (re.compile(
        r"(attend|present|days?|working day).{0,40}\b20\d{2}\b",
        re.I
    ), "attendance_year"),
    # ── Attendance ────────────────────────────────────────────────────────────
    (re.compile(
        r"\b(attend|present|absent|days? (present|in|at)|working day|mandatory|regular|irregular|punctual|came to|come to|how often)\b",
        re.I
    ), "attendance"),

    # ── Equipment list / tool usage ───────────────────────────────────────────
    # In _RULES, update equipment_list rule:
    (re.compile(
        r"\b(which (tool|machine|equipment)|what (tool|machine|equipment)|list.{0,10}(tool|machine|equipment)|most used|worked with|has used|used|top \d+|list \d+|show \d+)\b",
        re.I
    ), "equipment_list"),

    # ── Equipment count (without year — handled by tier0) ────────────────────
    (re.compile(
        r"\b(how many|total|count|number of).{0,20}(equipment|machine|slot|booking|request|reservation)\b",
        re.I
    ), "equipment_count"),

    # ── Publications ──────────────────────────────────────────────────────────
    (re.compile(
        r"\b(paper|publication|published|journal|research paper|article)\b",
        re.I
    ), "publication"),

    # ── Projects ─────────────────────────────────────────────────────────────
    (re.compile(
        r"\b(project|faculty project|research project|active project)\b",
        re.I
    ), "project"),

    # ── Training ─────────────────────────────────────────────────────────────
    (re.compile(
        r"\b(training|trained|training session)\b",
        re.I
    ), "training"),

    # ── Cancellations ─────────────────────────────────────────────────────────
    (re.compile(
        r"\b(cancel|cancellation|cancelled)\b",
        re.I
    ), "cancellation"),
    # In _RULES list, add after the existing cancellation rule:

    (re.compile(
        r"\b(session report|equipment report|usage report|session filed|report filed|lab report)\b",
        re.I
    ), "session_report"),

    (re.compile(
        r"\b(reservation|booked slot|slot reserved|how many slot|slots? (made|book))\b",
        re.I
    ), "reservation"),
    # ── Permissions ───────────────────────────────────────────────────────────
    (re.compile(
        r"\b(permission|authoris|authoriz|access permission|tool access)\b",
        re.I
    ), "permission"),

    # ── System ownership ──────────────────────────────────────────────────────
    (re.compile(
        r"\b(system owner|owns.{0,10}system|assigned.{0,10}tool|responsible for)\b",
        re.I
    ), "system_owner"),

    # ── Monthly reports ───────────────────────────────────────────────────────
    (re.compile(
        r"\b(monthly report|report submitted|report star|report rating)\b",
        re.I
    ), "monthly_report"),
    (re.compile(
        r"\b(where is|located|location|address|about iitbnf|what is iitbnf)\b",
        re.I
    ), "facility_info"),
    # ── Admin / facility-level stats ──────────────────────────────────────────
    (re.compile(
        r"\b(how many (staff|user|member|lab user)|total (staff|user|member)|active (staff|user)|headcount)\b",
        re.I
    ), "admin_stats"),

    # ── Leaves ───────────────────────────────────────────────────────────────
    (re.compile(
        r"\b(leaves?|casual leave|earned leave|sick leave|medical leave|days on leave)\b",
        re.I
    ), "leave"),
    # ── Logbook entries ─────────────────────────────────────────────────────
    (re.compile(
    r"\b(logbook|log book|session log|entries|filled up|filled in)\b",
    re.I
), "logbook"),
    # ── Identity / profile ────────────────────────────────────────────────────
    (re.compile(
        r"\b(who is|tell me about|describe|designation|role|position|department|team|qualification|joined|joining|tenure)\b",
        re.I
    ), "general_profile"),
]


def _regex_route(query: str) -> str | None:
    """Return intent label if any regex rule matches, else None."""
    for pattern, label in _RULES:
        if pattern.search(query):
            return label
    return None


# ── Layer 2: MiniLM Semantic Fallback ────────────────────────────────────────

# Anchor phrases — canonical examples per intent.
# These are the "training examples" for the semantic classifier.
# Keep them generic (no real names) so embeddings generalise.
_ANCHORS = {
    "attendance": [
        "how many days was X present",
        "attendance record for X",
        "was X regular",
        "how often did X come to work",
        "X's attendance this year",
        "how active was X in terms of attendance",
        "what is X attendance percentage",
        "X attendance percentage in 2026",
        "attendance percent for X",
        "what percentage did X attend",
    ],
    # ADD inside _ANCHORS dict:
    "attendance_monthly": [
        "attendance in January 2026",
        "how many days was X present in March",
        "share X attendance in Feb 2025",
        "attendance for January",
        "days present in December 2024",
        "how regular was X in June",
    ],
    "compare": [
        "compare X slot activity in 2025 and 2026",
        "how did X booking change from 2025 to 2026",
        "X vs 2025 equipment requests",
        "was X more regular in 2024 or 2025",
        "difference between X attendance 2024 2025",
        "compare X equipment usage 2024 vs 2025",
    ],
    "compare_attend": [
    "compare X attendance in 2024 and 2025",
    "was X more regular in 2024 or 2025",
    "how did X attendance change from 2024 to 2025",
    "difference in attendance between 2024 and 2025 for X",
    ],
    "compare_slot": [
        "compare X slot activity in 2024 and 2025",
        "how did X equipment usage change from 2024 to 2025",
        "X vs 2024 equipment requests",
        "difference in bookings between 2024 and 2025",
    ],
    "reservation": [
        "how many reservations does X have",
        "total slot bookings for X",
        "how many slots did X book",
        "X reservation count",
    ],
    "session_report": [
        "how many session reports has X filed",
        "session reports submitted by X",
        "equipment usage reports for X",
        "lab reports filed by X",
        "how many lab reports does X have",
        "report submission count for X",
        "how many usage reports did X submit",
    ],
    "equipment_year": [
        "how many equipment requests did X make in 2025",
        "slot activity for X in 2026",
        "X equipment usage 2025",
        "how active was X on equipment in 2025",
        "what was X slot booking in 2026",
    ],
    "equipment_list": [
        "which equipment does X use most",
        "list all machines X has worked with",
        "what tools did X request",
        "which machines has X used",
        "most used equipment by X",
    ],
    "equipment_count": [
        "how many equipment requests does X have",
        "total slot bookings for X",
        "number of requests submitted by X",
        "how many times did X request equipment",
    ],
    "publication": [
        "how many papers does X have",
        "publications by X",
        "research output of X",
        "how many journal articles did X publish",
    ],
    "project": [
        "what projects is X associated with",
        "how many projects does X have",
        "active projects for X",
        "faculty projects linked to X",
    ],
    "training": [
        "how many training sessions has X completed",
        "equipment training for X",
        "training record of X",
    ],
    "cancellation": [
        "how many cancellations does X have",
        "reservation cancellations by X",
        "how many times did X cancel",
    ],
    "permission": [
        "how many tool permissions does X have",
        "which tools is X authorised to use",
        "equipment access permissions for X",
    ],
    "system_owner": [
        "which systems does X own",
        "X is system owner of which tools",
        "how many tools is X responsible for",
    ],
    "monthly_report": [
        "how many monthly reports did X submit",
        "X monthly report rating",
        "report submission count for X",
    ],
    "admin_stats": [
        "how many staff are there",
        "total lab users registered",
        "how many members does IITBNF have",
        "headcount of staff",
    ],
    "leave": [
        "how many leave days did X take",
        "leave breakdown for X",
        "casual leave taken by X",
    ],
    "logbook": [
        "how many logbook entries does X have",
        "how many entries has X filled",
        "session log entries for X",
        "how many logs has X submitted",
        "entries filled by X",
    ],
    "general_profile": [
        "who is X",
        "tell me about X",
        "what is X designation",
        "which team does X belong to",
        "what department is X in",
    ],
    # a new intent:
    "facility_info": [
        "where is IITBNF located",
        "what is the address of IITBNF",
        "where is IIT Bombay nanofabrication facility",
        "location of IITBNF",
        "where is the facility",
        "what is IITBNF",
        "tell me about IITBNF",
        "about the facility",
        "what are the working days at iitbnf",
        "working days iitbnf",
        "how many days does iitbnf operate",
        "when is iitbnf open",
        "working hours iitbnf",
        "what time does iitbnf open",
        "iitbnf schedule",
        "iitbnf timings",
    ],
}

# Module-level singletons — loaded once at startup
_minilm_model   = None
_anchor_labels  = []
_anchor_vecs    = None
_minilm_lock    = threading.Lock()
_SIMILARITY_THRESHOLD = 0.40   # tuned for MiniLM on nanofab domain queries


def _load_minilm():
    """Load MiniLM once, thread-safe. Pre-compute anchor embeddings."""
    global _minilm_model, _anchor_labels, _anchor_vecs
    if _minilm_model is not None:
        return True
    with _minilm_lock:
        if _minilm_model is not None:
            return True
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("[IntentRouter] Loading all-MiniLM-L6-v2...")
            import os
            cache_dir = os.path.join(os.path.dirname(__file__), "..", "models", "minilm")
            _minilm_model = SentenceTransformer(
                "sentence-transformers/all-MiniLM-L6-v2",
                cache_folder=cache_dir if os.path.exists(cache_dir) else None,
            )

            labels, phrases = [], []
            for intent, anchor_phrases in _ANCHORS.items():
                for phrase in anchor_phrases:
                    labels.append(intent)
                    phrases.append(phrase)

            _anchor_labels = labels
            _anchor_vecs   = _minilm_model.encode(phrases, batch_size=64, show_progress_bar=False)
            logger.info(
                "[IntentRouter] MiniLM ready — %d anchor embeddings across %d intents",
                len(labels), len(_ANCHORS)
            )
            return True
        except ImportError:
            logger.warning(
                "[IntentRouter] sentence-transformers not installed — "
                "MiniLM fallback disabled. Install with: pip install sentence-transformers"
            )
        except Exception as e:
            logger.error("[IntentRouter] MiniLM load failed: %s", e)
    return False


def _minilm_route(query: str) -> tuple[str, float]:
    """Classify query via cosine similarity against anchor embeddings."""
    if _minilm_model is None:
        return "general_profile", 0.0

    from sklearn.metrics.pairwise import cosine_similarity as cos_sim
    q_vec = _minilm_model.encode([query])
    sims  = cos_sim(q_vec, _anchor_vecs)[0]
    best  = int(np.argmax(sims))
    score = float(sims[best])

    if score >= _SIMILARITY_THRESHOLD:
        return _anchor_labels[best], score
    return "general_profile", score


# ── Public API ────────────────────────────────────────────────────────────────

def classify_intent(query: str) -> tuple[str, str]:
    """
    Classify the intent of a natural language query.

    Returns:
        (intent_label, method)
        method is "regex" or "minilm" — useful for logging/debugging.

    Always returns a label — falls back to "general_profile" if nothing matches.
    """
    # Layer 1: regex (zero cost)
    label = _regex_route(query)
    if label:
        logger.debug("[IntentRouter] regex → %s : %r", label, query[:60])
        return label, "regex"

    # Layer 2: MiniLM (semantic)
    if _minilm_model is not None:
        label, score = _minilm_route(query)
        logger.debug(
            "[IntentRouter] minilm → %s (%.2f) : %r", label, score, query[:60]
        )
        return label, "minilm"

    # Hard fallback
    return "general_profile", "fallback"


def warm_up() -> None:
    """
    Pre-load MiniLM at server startup.
    Call from app.py _startup_tasks() — non-fatal if it fails.
    """
    _load_minilm()