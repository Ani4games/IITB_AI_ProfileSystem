"""
rag/composer.py — TF-IDF + Logistic Regression narrative composer.
===================================================================
Replaces the LLM for summary generation with a retrieval-and-compose
approach:

  1. A curated sentence template library covers every data scenario.
  2. Each incoming profile context is vectorized with TF-IDF.
  3. Cosine similarity selects the best-matching sentence variant
     for each narrative slot.
  4. A LogisticRegression classifier (trained on synthetic examples)
     decides which sections are worth including given the data density.

No neural network, no GPU, no model file download.
Produces deterministic, professional, facility-specific summaries.

Template reduction (v2)
───────────────────────
The original ~100 templates are collapsed to ~40 by applying three rules:

  Rule 1 — Pluralization is handled by _enrich_ctx(), not by separate
            templates.  Every count field already has a pre-computed
            "*_word" and "*_verb" key, so a single template shell handles
            both "1 request has" and "5 requests have" without branching.

  Rule 2 — "Zero data" fallback templates are removed for sections where
            the SectionClassifier already gates on data > 0.  The only
            retained zero-fallback is "research" (genuinely informative
            for management) and "attendance" (absence of data is notable).

  Rule 3 — Staff/Lab templates that are conceptually identical are unified
            into SHARED_TEMPLATES and consumed by both selectors.  The
            shared templates use field names that exist in both contexts
            (equipment, ownership, training, research, projects).

The three attendance score-range templates are intentionally kept because
they produce meaningfully different tones: "exemplary", "meets threshold",
"below minimum" — not mere phrasing variants.

Public API (unchanged):
    compose_staff_summary(ctx)  → str
    compose_lab_summary(ctx)    → str
    warm_up()                   → None  (call once at startup)
"""

import re
import logging
import pickle
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE       = Path(__file__).parent.parent
_MODEL_PATH = _BASE / "models" / "composer_model.pkl"

# ── Module-level singletons ────────────────────────────────────────────────────
_composer: Optional["NarrativeComposer"] = None
_composer_lock                           = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS  (defined before templates so lambdas can reference them)
# ══════════════════════════════════════════════════════════════════════════════

def _att(ctx: dict) -> float:
    """Safe attendance percentage extraction."""
    v = ctx.get("attendance_pct", 0)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _eq(ctx: dict) -> int:
    """Safe equipment requests count."""
    try:
        return int(ctx.get("eq_requests", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _slot(ctx: dict) -> int:
    """Safe slot-booked count (works for both staff eq_slot_booked and lab approved_requests)."""
    try:
        return int(ctx.get("eq_slot_booked", 0) or ctx.get("approved_requests", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _pos(ctx: dict, key: str) -> int:
    """Return int value of ctx[key] if positive, else 0."""
    try:
        return max(0, int(ctx.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE LIBRARIES
# ══════════════════════════════════════════════════════════════════════════════
#
# Each template dict has:
#   text      : sentence with {placeholder} slots (filled by _enrich_ctx keys)
#   slot      : narrative section name
#   condition : lambda(ctx) → bool — when is this template applicable?
#   priority  : int, lower = preferred on tie
#   tags      : space-separated keywords for TF-IDF matching
#
# _enrich_ctx() pre-computes all "*_word" / "*_verb" / "*_plural" keys so
# templates never need Python expressions — just plain {placeholder} slots.


# ── STAFF-ONLY TEMPLATES ──────────────────────────────────────────────────────
# Covers fields that only exist in staff contexts:
#   designation, team, joining_date, appointment_type, qualification,
#   attendance, leaves, monthly_reports, pending requests, ownership_history.

STAFF_TEMPLATES = [

    # ── Identity ──────────────────────────────────────────────────────────────
    # Two variants: with-and-without designation.  Both are genuinely different
    # sentences, not phrasing variants — so they stay as separate templates.
    {
        "text":      "{name} holds the position of {designation} within the {team} team at the IIT Bombay Nanofabrication Facility.",
        "slot":      "identity",
        "condition": lambda c: bool(c.get("designation") and c.get("team")),
        "priority":  1,
        "tags":      "designation team role position iitbnf staff member",
    },
    {
        "text":      "{name} is a member of the {team} team at IITBNF.",
        "slot":      "identity",
        "condition": lambda c: bool(c.get("team") and not c.get("designation")),
        "priority":  2,
        "tags":      "team iitbnf member staff",
    },

    # ── Identity detail ───────────────────────────────────────────────────────
    # Single template: condition gates on both joining_date AND appointment_type
    # being available.  When appointment_type is missing the sentence is simply
    # skipped — no need for a second "joined on {date}" fallback because the
    # identity sentence above already anchors the person in context.
    {
        "text":      "They joined the facility on {joining_date} and are currently appointed on a {appointment_type} basis.",
        "slot":      "identity_detail",
        "condition": lambda c: (
            bool(c.get("joining_date") and c.get("joining_date") != "N/A")
            and bool(c.get("appointment_type") and c.get("appointment_type") != "N/A")
        ),
        "priority":  1,
        "tags":      "joining date appointment tenure basis",
    },
    # Fallback: only joining date available
    {
        "text":      "{name} has been with the facility since {joining_date}.",
        "slot":      "identity_detail",
        "condition": lambda c: (
            bool(c.get("joining_date") and c.get("joining_date") != "N/A")
            and not bool(c.get("appointment_type") and c.get("appointment_type") != "N/A")
        ),
        "priority":  2,
        "tags":      "joining date tenure since",
    },

    # ── Qualification ─────────────────────────────────────────────────────────
    {
        "text":      "Their academic qualification is recorded as {qualification}.",
        "slot":      "qualification",
        "condition": lambda c: bool(c.get("qualification") and c.get("qualification") != "N/A"),
        "priority":  1,
        "tags":      "qualification degree education academic",
    },

    # ── Attendance — THREE variants kept intentionally ────────────────────────
    # These produce meaningfully different tones (exemplary / meets / below)
    # and are not reducible to a single template with fill-in phrasing.
    {
        "text":      "{name} has maintained an attendance rate of {attendance_pct}% this year, present for {days_present} of {working_days} working days — an exemplary record well above the 75% threshold.",
        "slot":      "attendance",
        "condition": lambda c: _att(c) >= 90,
        "priority":  1,
        "tags":      "attendance excellent high present working days threshold exemplary",
    },
    {
        "text":      "Attendance stands at {attendance_pct}% for the current year ({days_present} days present out of {working_days}), comfortably meeting the required threshold.",
        "slot":      "attendance",
        "condition": lambda c: 75 <= _att(c) < 90,
        "priority":  1,
        "tags":      "attendance good meeting threshold present working days",
    },
    {
        "text":      "The current year attendance rate of {attendance_pct}% ({days_present} days present out of {working_days}) falls below the mandated 75% minimum and may require follow-up.",
        "slot":      "attendance",
        "condition": lambda c: 0 < _att(c) < 75,
        "priority":  1,
        "tags":      "attendance low below threshold warning follow-up days",
    },
    # Zero/unavailable: kept because absence of attendance data is notable for staff
    {
        "text":      "Attendance data for the current period is not available in the system.",
        "slot":      "attendance",
        "condition": lambda c: _att(c) == 0 and not c.get("days_present"),
        "priority":  3,
        "tags":      "attendance unavailable missing data",
    },

    # ── Leave ─────────────────────────────────────────────────────────────────
    # Single template: _enrich_ctx provides {leaves_taken_word} and
    # {leave_day_verb} so "1 day has" vs "5 days have" is already resolved.
    # The breakdown clause is included when present via the condition.
    # When leave_breakdown is absent the sentence still reads well without it.
    {
        "text":      "A total of {leaves_taken_word} leave {leave_day_verb} been recorded this year{leave_breakdown_clause}.",
        "slot":      "leave",
        "condition": lambda c: _pos(c, "leaves_taken") > 0,
        "priority":  1,
        "tags":      "leave days taken type breakdown annual casual period",
    },

    # ── Monthly reports ───────────────────────────────────────────────────────
    # Single template: the rating clause is conditional via a pre-computed key
    # injected by _enrich_ctx — see "monthly_rating_clause" below.
    {
        "text":      "{name} has submitted {monthly_reports_word} monthly {report_noun}{monthly_rating_clause}.",
        "slot":      "reports",
        "condition": lambda c: _pos(c, "monthly_reports_submitted") > 0,
        "priority":  1,
        "tags":      "monthly reports submitted rating stars average date",
    },

    # ── Pending requests ──────────────────────────────────────────────────────
    {
        "text":      "{eq_pending_word} {pending_remain_verb} pending approval.",
        "slot":      "pending",
        "condition": lambda c: _pos(c, "eq_pending") > 0,
        "priority":  2,
        "tags":      "pending approval requests awaiting",
    },

    # ── Ownership history (staff-only field) ──────────────────────────────────
    {
        "text":      "Over their tenure, {name} has served as system owner for {systems_owned_ever_word} {ever_tool_plural}, including {systems_removed_word} that have since been reassigned.",
        "slot":      "ownership_history",
        "condition": lambda c: _pos(c, "systems_owned_ever") > 0 and _pos(c, "systems_ownership_removed") > 0,
        "priority":  1,
        "tags":      "system owner history tenure tools ever assigned removed reassigned",
    },

    # ── Tool permissions (staff context uses "piece of equipment" phrasing) ───
    {
        "text":      "Equipment access permissions are held for {tool_permissions_word} {permission_piece_plural} of equipment.",
        "slot":      "permissions",
        "condition": lambda c: _pos(c, "tool_permissions_count") > 0,
        "priority":  2,
        "tags":      "permissions equipment access authorized tools",
    },

    # ── Slot reservations (staff uses "distinct" phrasing) ────────────────────
    {
        "text":      "Lab reservation records show {total_bookings_word} {reservation_plural} across {tools_used_word} distinct {piece_plural} of equipment.",
        "slot":      "reservations",
        "condition": lambda c: _pos(c, "total_bookings") > 0,
        "priority":  1,
        "tags":      "reservations slots bookings equipment lab distinct",
    },

    # ── Equipment usage (staff — three score-range variants) ──────────────────
    # High-volume gets qualitative commentary; low-volume gets a plain count.
    # The "no data" template is removed — the SectionClassifier excludes the
    # section when eq_requests == 0, so a "none recorded" sentence is never reached.
    {
        "text":      "{name} has submitted {eq_requests_word} equipment usage {eq_request_verb} this period, of which {eq_slot_booked_word} have been slot-booked, demonstrating consistent and active use of facility resources.",
        "slot":      "equipment",
        "condition": lambda c: _eq(c) >= 10 and _slot(c) > 0,
        "priority":  1,
        "tags":      "equipment usage requests slot booked active facility high volume",
    },
    {
        "text":      "Equipment usage records show {eq_requests_word} {eq_request_verb} submitted, with {eq_slot_booked_word} resulting in confirmed slot bookings.",
        "slot":      "equipment",
        "condition": lambda c: 1 <= _eq(c) < 10 and _slot(c) > 0,
        "priority":  1,
        "tags":      "equipment usage requests bookings confirmed slot moderate",
    },
    # Covers cases with requests but no slot bookings yet
    {
        "text":      "{eq_requests_word} equipment usage {eq_request_verb} been submitted during this period.",
        "slot":      "equipment",
        "condition": lambda c: _eq(c) >= 1 and _slot(c) == 0,
        "priority":  2,
        "tags":      "equipment usage request submitted period no booking",
    },
]


# ── LAB-ONLY TEMPLATES ────────────────────────────────────────────────────────
# Covers fields unique to lab contexts:
#   category, department, supervisor, research_area, registration,
#   approved_requests, session_reports, cancellations.

LAB_TEMPLATES = [

    # ── Identity — two variants (with/without department) ─────────────────────
    {
        "text":      "{name} is registered at IITBNF as a {category} user from the {department} department.",
        "slot":      "identity",
        "condition": lambda c: bool(c.get("category") and c.get("department") and c.get("department") != "N/A"),
        "priority":  1,
        "tags":      "registered user category department iitbnf",
    },
    {
        "text":      "{name} is a {category} user registered at the IIT Bombay Nanofabrication Facility.",
        "slot":      "identity",
        "condition": lambda c: bool(c.get("category") and (not c.get("department") or c.get("department") == "N/A")),
        "priority":  2,
        "tags":      "registered user category iitbnf",
    },

    # ── Supervisor ────────────────────────────────────────────────────────────
    {
        "text":      "They work under the supervision of {supervisor_name}.",
        "slot":      "supervisor",
        "condition": lambda c: bool(c.get("supervisor_name") and c.get("supervisor_name") != "N/A"),
        "priority":  1,
        "tags":      "supervisor supervision works under",
    },

    # ── Research area ─────────────────────────────────────────────────────────
    {
        "text":      "Research focus: {research_area}.",
        "slot":      "research_area",
        "condition": lambda c: bool(c.get("research_area") and c.get("research_area") not in ("N/A", "NA", "")),
        "priority":  1,
        "tags":      "research area focus topic",
    },

    # ── Registration ──────────────────────────────────────────────────────────
    {
        "text":      "Registration for {reg_course} is currently {reg_status}.",
        "slot":      "registration",
        "condition": lambda c: bool(c.get("reg_course") and c.get("reg_course") != "N/A"),
        "priority":  1,
        "tags":      "registration course status active",
    },

    # ── Slot reservations (lab phrasing — no "distinct") ─────────────────────
    {
        "text":      "{name} has made {total_bookings_word} {reservation_plural} across {tools_used_word} {piece_plural} of equipment.",
        "slot":      "reservations",
        "condition": lambda c: _pos(c, "total_bookings") > 0,
        "priority":  1,
        "tags":      "reservations bookings slots equipment pieces",
    },

    # ── Equipment requests (lab — uses approved_requests not eq_slot_booked) ──
    # Single template: _enrich_ctx injects {approved_clause} which is empty
    # when approved_requests == 0, so both "with approvals" and "without" cases
    # are handled by one sentence.
    {
        "text":      "{eq_requests_word} equipment usage {eq_request_verb} been submitted{approved_clause}.",
        "slot":      "equipment",
        "condition": lambda c: _eq(c) > 0,
        "priority":  1,
        "tags":      "equipment requests approved submitted usage facility",
    },

    # ── Session reports ───────────────────────────────────────────────────────
    {
        "text":      "{session_reports_word} equipment session {session_report_verb} been filed following equipment usage.",
        "slot":      "session_reports",
        "condition": lambda c: _pos(c, "session_reports") > 0,
        "priority":  1,
        "tags":      "session reports filed equipment usage",
    },

    # ── Cancellations ─────────────────────────────────────────────────────────
    {
        "text":      "{cancellations_word} reservation {cancellation_verb} been recorded.",
        "slot":      "cancellations",
        "condition": lambda c: _pos(c, "cancellations") > 0,
        "priority":  2,
        "tags":      "cancellations reservation recorded",
    },
]


# ── SHARED TEMPLATES ──────────────────────────────────────────────────────────
# These slots appear in both staff and lab section orders, and the sentences
# are conceptually identical — the field names are the same in both contexts.
# Both selectors receive a combined list of their own templates + these shared
# ones, so the TF-IDF vectorizer sees the full vocabulary.

SHARED_TEMPLATES = [

    # ── System ownership (current) ────────────────────────────────────────────
    # Staff phrasing: "holds responsibilities … overseeing maintenance"
    # Lab phrasing: "serves as system owner … at the facility"
    # They differ enough in tone to keep as two entries, but both are shared
    # because the underlying data field (systems_owned_current) is the same.
    {
        "text":      "{name} currently holds system ownership responsibilities for {systems_owned_current_word} {tool_plural}, overseeing their operational status and maintenance coordination.",
        "slot":      "ownership",
        "condition": lambda c: _pos(c, "systems_owned_current") > 0,
        "priority":  1,
        "tags":      "system owner equipment responsible operational maintenance current staff",
    },
    {
        "text":      "{name} currently serves as system owner for {systems_owned_current_word} {tool_plural} at the facility.",
        "slot":      "ownership",
        "condition": lambda c: _pos(c, "systems_owned_current") > 0,
        "priority":  2,
        "tags":      "system owner tools facility responsible current lab",
    },

    # ── Training ──────────────────────────────────────────────────────────────
    # Single template: pluralization handled by {training_session_verb}.
    # Staff variant appended a "reflecting commitment" clause — dropped because
    # it was editorial filler.  The sentence is equally informative without it.
    {
        "text":      "{trainings_word} equipment training {training_session_verb} been completed.",
        "slot":      "training",
        "condition": lambda c: _pos(c, "trainings") > 0,
        "priority":  1,
        "tags":      "training sessions equipment completed proficiency",
    },

    # ── Research publications ─────────────────────────────────────────────────
    # "No publications" retained: genuinely informative for management context.
    {
        "text":      "Research contributions include {papers_word} approved {publication_plural} associated with the facility.",
        "slot":      "research",
        "condition": lambda c: _pos(c, "papers") > 0,
        "priority":  1,
        "tags":      "research publications papers approved output contributions",
    },
    {
        "text":      "No research publications are currently on record.",
        "slot":      "research",
        "condition": lambda c: _pos(c, "papers") == 0,
        "priority":  3,
        "tags":      "research no publications none record",
    },

    # ── Projects ─────────────────────────────────────────────────────────────
    # Single template: _enrich_ctx injects {active_clause} — see below.
    # Handles both "N of which M are active" and plain "N projects" cases.
    {
        "text":      "{name} is associated with {projects_word} faculty {project_plural}{active_clause}.",
        "slot":      "projects",
        "condition": lambda c: _pos(c, "projects") > 0,
        "priority":  1,
        "tags":      "projects faculty active associated research linked facility",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION ORDERING
# ══════════════════════════════════════════════════════════════════════════════

STAFF_SECTION_ORDER = [
    "identity", "identity_detail", "qualification",
    "attendance", "leave",
    "equipment", "reservations", "pending",
    "ownership", "ownership_history", "permissions",
    "reports", "training",
    "research", "projects",
]

LAB_SECTION_ORDER = [
    "identity", "supervisor", "research_area", "registration",
    "reservations", "equipment", "session_reports", "cancellations",
    "ownership",
    "training", "research", "projects",
]


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════════

def _enrich_ctx(ctx: dict) -> dict:
    """
    Add pre-computed display strings so templates stay as plain {placeholder}
    strings with no Python logic inside them.

    New in v2
    ─────────
    • leave_breakdown_clause  — ", with breakdown: X" or "" when absent
    • monthly_rating_clause   — " with an average rating of X stars" or ""
    • approved_clause         — ", of which N have been approved" or ""
    • active_clause           — ", of which N are currently active" or ""

    These four conditional-clause keys allow the SHARED/LAB equipment,
    leave, reports and projects templates to collapse from 2 entries each
    down to 1 — the clause is simply empty when the optional data is absent.
    """
    c = dict(ctx)  # don't mutate the original

    def _int(key: str) -> int:
        try:
            return max(0, int(c.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    # ── Slot reservations ─────────────────────────────────────────────────────
    n = _int("total_bookings")
    c["total_bookings_word"] = str(n)
    c["reservation_plural"]  = "reservations" if n != 1 else "reservation"

    n = _int("tools_used")
    c["tools_used_word"] = str(n)
    c["piece_plural"]    = "pieces" if n != 1 else "piece"

    # ── Equipment requests ────────────────────────────────────────────────────
    n = _int("eq_requests")
    c["eq_requests_word"] = str(n)
    c["eq_request_verb"]  = "requests have" if n != 1 else "request has"

    n = _int("eq_slot_booked")
    c["eq_slot_booked_word"] = str(n)

    n = _int("eq_pending")
    c["eq_pending_word"]     = str(n)
    c["pending_remain_verb"] = "requests remain" if n != 1 else "request remains"

    # ── approved_clause (lab equipment template) ──────────────────────────────
    n_approved = _int("approved_requests")
    c["approved_requests_word"] = str(n_approved)
    if n_approved > 0:
        verb = "have" if n_approved != 1 else "has"
        c["approved_clause"] = f", of which {n_approved} {verb} been approved"
    else:
        c["approved_clause"] = ""

    # ── Leave ─────────────────────────────────────────────────────────────────
    n = _int("leaves_taken")
    c["leaves_taken_word"] = str(n)
    c["leave_day_verb"]    = "days have" if n != 1 else "day has"

    breakdown = (c.get("leave_breakdown") or "").strip()
    c["leave_breakdown_clause"] = f", with breakdown: {breakdown}" if breakdown else ""

    # ── Training ──────────────────────────────────────────────────────────────
    n = _int("trainings")
    c["trainings_word"]        = str(n)
    c["training_session_verb"] = "sessions have" if n != 1 else "session has"

    # ── Session reports ───────────────────────────────────────────────────────
    n = _int("session_reports")
    c["session_reports_word"] = str(n)
    c["session_report_verb"]  = "reports have" if n != 1 else "report has"

    # ── Cancellations ─────────────────────────────────────────────────────────
    n = _int("cancellations")
    c["cancellations_word"]  = str(n)
    c["cancellation_verb"]   = "cancellations have" if n != 1 else "cancellation has"

    # ── Monthly reports ───────────────────────────────────────────────────────
    n = _int("monthly_reports_submitted")
    c["monthly_reports_word"] = str(n)
    c["report_noun"]          = "reports" if n != 1 else "report"

    avg_stars = c.get("monthly_report_avg_stars")
    if avg_stars and str(avg_stars) not in ("N/A", "None", "0", ""):
        c["monthly_rating_clause"] = f" with an average rating of {avg_stars} stars"
    else:
        c["monthly_rating_clause"] = ""

    # ── System ownership ──────────────────────────────────────────────────────
    n = _int("systems_owned_current")
    c["systems_owned_current_word"] = str(n)
    c["tool_plural"]                = "tools" if n != 1 else "tool"

    n = _int("systems_owned_ever")
    c["systems_owned_ever_word"] = str(n)
    c["ever_tool_plural"]        = "tools" if n != 1 else "tool"

    c["systems_removed_word"] = str(_int("systems_ownership_removed"))

    # ── Tool permissions ──────────────────────────────────────────────────────
    n = _int("tool_permissions_count")
    c["tool_permissions_word"]   = str(n)
    c["permission_piece_plural"] = "pieces" if n != 1 else "piece"

    # ── Research ──────────────────────────────────────────────────────────────
    n = _int("papers")
    c["papers_word"]        = str(n)
    c["publication_plural"] = "publications" if n != 1 else "publication"

    # ── Projects ─────────────────────────────────────────────────────────────
    n = _int("projects")
    c["projects_word"]  = str(n)
    c["project_plural"] = "projects" if n != 1 else "project"

    n_active = _int("active_projects")
    c["active_projects_word"] = str(n_active)
    c["active_are_verb"]      = "are" if n_active != 1 else "is"
    if n_active > 0:
        c["active_clause"] = f", of which {n_active} {c['active_are_verb']} currently active"
    else:
        c["active_clause"] = ""

    # ── Appointment type — clean raw day-count if it crept in from DB ─────────
    appt = str(c.get("appointment_type") or "")
    if appt.strip().lstrip("0123456789").strip().lower() in ("days", "day", ""):
        try:
            days = int(appt.strip().split()[0])
            c["appointment_type"] = f"{days}-day contract"
        except (ValueError, IndexError):
            pass

    return c


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE FILL
# ══════════════════════════════════════════════════════════════════════════════

def _fill_template(template_text: str, ctx: dict) -> str:
    """Fill {placeholder} slots in a template. Missing keys become [key]."""
    def replacer(match):
        key = match.group(1)
        val = ctx.get(key)
        if val is None:
            return f"[{key}]"
        return str(val)
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replacer, template_text)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION INCLUSION CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════════

class SectionClassifier:
    """
    Logistic Regression classifier that decides whether a given section
    is worth including in the summary given the context data density.
    Trained on synthetic examples — no labeled dataset needed.
    """

    def __init__(self):
        self.vectorizer = TfidfVectorizer(
            ngram_range  = (1, 2),
            max_features = 5000,
            sublinear_tf = True,
        )
        self.clf      = LogisticRegression(max_iter=500, C=1.0)
        self._trained = False

    def _context_to_text(self, ctx: dict) -> str:
        parts = []
        for k, v in ctx.items():
            if v and str(v) not in ("N/A", "None", "0", ""):
                parts.append(f"{k} {v}")
        return " ".join(parts)

    def _generate_training_data(self):
        X, y = [], []

        def ctx(**kwargs):
            base = {k: 0 for k in [
                "attendance_pct", "days_present", "working_days",
                "leaves_taken", "eq_requests", "eq_slot_booked",
                "total_bookings", "tools_used", "papers", "projects",
                "active_projects", "trainings", "systems_owned_current",
                "systems_owned_ever", "monthly_reports_submitted",
                "session_reports", "cancellations", "approved_requests",
            ]}
            base.update(kwargs)
            return base

        # attendance — include when attendance_pct > 0
        for pct in [0, 45, 76, 92]:
            c    = ctx(attendance_pct=pct, days_present=pct, working_days=100)
            text = self._context_to_text(c)
            X.append(f"attendance {text}"); y.append(1 if pct > 0 else 0)

        # equipment — include when eq_requests > 0
        for eq in [0, 1, 5, 20]:
            c    = ctx(eq_requests=eq, eq_slot_booked=eq // 2)
            text = self._context_to_text(c)
            X.append(f"equipment {text}"); y.append(1 if eq > 0 else 0)

        # research — include when papers > 0
        for p in [0, 1, 3, 10]:
            c    = ctx(papers=p)
            text = self._context_to_text(c)
            X.append(f"research {text}"); y.append(1 if p > 0 else 0)

        # projects — include when projects > 0
        for proj in [0, 1, 4]:
            c    = ctx(projects=proj, active_projects=proj)
            text = self._context_to_text(c)
            X.append(f"projects {text}"); y.append(1 if proj > 0 else 0)

        # ownership — include when systems_owned_current > 0
        for so in [0, 1, 5]:
            c    = ctx(systems_owned_current=so)
            text = self._context_to_text(c)
            X.append(f"ownership {text}"); y.append(1 if so > 0 else 0)

        # training — include when trainings > 0
        for tr in [0, 2, 8]:
            c    = ctx(trainings=tr)
            text = self._context_to_text(c)
            X.append(f"training {text}"); y.append(1 if tr > 0 else 0)

        # monthly reports — include when submitted > 0
        for rp in [0, 3, 15]:
            c    = ctx(monthly_reports_submitted=rp)
            text = self._context_to_text(c)
            X.append(f"reports {text}"); y.append(1 if rp > 0 else 0)

        # session reports — include when > 0
        for sr in [0, 2, 10]:
            c    = ctx(session_reports=sr)
            text = self._context_to_text(c)
            X.append(f"session_reports {text}"); y.append(1 if sr > 0 else 0)

        # cancellations — include when > 0
        for cc in [0, 1, 5]:
            c    = ctx(cancellations=cc)
            text = self._context_to_text(c)
            X.append(f"cancellations {text}"); y.append(1 if cc > 0 else 0)

        # identity — always include
        for _ in range(5):
            c    = ctx(attendance_pct=80)
            text = self._context_to_text(c)
            X.append(f"identity {text}"); y.append(1)

        return X, y

    def train(self):
        X, y  = self._generate_training_data()
        X_vec = self.vectorizer.fit_transform(X)
        self.clf.fit(X_vec, y)
        self._trained = True
        logger.info("SectionClassifier trained on %d synthetic examples.", len(X))

    def should_include(self, section: str, ctx: dict) -> bool:
        if not self._trained:
            return True
        text  = f"{section} {self._context_to_text(ctx)}"
        X_vec = self.vectorizer.transform([text])
        prob  = self.clf.predict_proba(X_vec)[0][1]
        return prob >= 0.5


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE SELECTOR  (TF-IDF + cosine similarity)
# ══════════════════════════════════════════════════════════════════════════════

class TemplateSelector:
    """
    Given candidate templates for a section, selects the best match using
    TF-IDF cosine similarity against the incoming context.
    """

    def __init__(self, templates: list):
        self.templates   = templates
        self.vectorizer  = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
        self._fitted     = False
        self._tag_matrix = None

        tag_texts = [t["tags"] for t in templates]
        if tag_texts:
            self._tag_matrix = self.vectorizer.fit_transform(tag_texts)
            self._fitted     = True

    def select(self, section: str, ctx: dict) -> Optional[dict]:
        candidates = [
            (i, t) for i, t in enumerate(self.templates)
            if t["slot"] == section and t["condition"](ctx)
        ]

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][1]

        ctx_query = " ".join(
            f"{k} {v}"
            for k, v in ctx.items()
            if v and str(v) not in ("N/A", "None", "0", "")
        ) + f" {section}"

        if not self._fitted:
            return sorted(candidates, key=lambda x: x[1]["priority"])[0][1]

        q_vec  = self.vectorizer.transform([ctx_query])
        scores = {}
        for orig_idx, tmpl in candidates:
            sim              = cosine_similarity(q_vec, self._tag_matrix[orig_idx])[0][0]
            priority_bonus   = 1.0 / (tmpl["priority"] * 2)
            scores[orig_idx] = sim + priority_bonus

        best_idx = max(scores, key=scores.__getitem__)
        return self.templates[best_idx]


# ══════════════════════════════════════════════════════════════════════════════
# NARRATIVE COMPOSER
# ══════════════════════════════════════════════════════════════════════════════

class NarrativeComposer:
    """
    Composes a full narrative summary from a context dict without any LLM.
    Uses TF-IDF cosine similarity for template selection and logistic
    regression for section inclusion decisions.
    """

    def __init__(self):
        # Each selector gets its own templates + the shared pool so the
        # TF-IDF vocabulary covers the full sentence space.
        self.staff_selector     = TemplateSelector(STAFF_TEMPLATES + SHARED_TEMPLATES)
        self.lab_selector       = TemplateSelector(LAB_TEMPLATES   + SHARED_TEMPLATES)
        self.section_classifier = SectionClassifier()
        self.section_classifier.train()

    def _compose(
        self,
        ctx:           dict,
        section_order: list,
        selector:      TemplateSelector,
        profile_type:  str,
    ) -> str:
        ctx = _enrich_ctx(ctx)
        sentences = []

        for section in section_order:
            if not self.section_classifier.should_include(section, ctx):
                continue

            tmpl = selector.select(section, ctx)
            if tmpl is None:
                continue

            sentence = _fill_template(tmpl["text"], ctx)

            # Skip if a core placeholder is still unfilled
            if "[" in sentence and "]" in sentence:
                if section in ("identity",):
                    continue
                logger.debug("Unfilled placeholder in '%s': %s", section, sentence)

            sentences.append(sentence)

        if not sentences:
            name = ctx.get("name", "This member")
            return f"{name} is registered at IITBNF. Detailed activity data is not yet available."

        return self._paragraphize(sentences, section_order, profile_type)

    def _paragraphize(
        self, sentences: list, section_order: list, profile_type: str
    ) -> str:
        """Group sentences into 2–3 coherent paragraphs by index split."""
        n = len(sentences)
        if n <= 3:
            return " ".join(sentences)

        split1 = max(1, n // 4)
        split2 = max(split1 + 1, n * 3 // 4)

        para1 = " ".join(sentences[:split1])
        para2 = " ".join(sentences[split1:split2])
        para3 = " ".join(sentences[split2:])

        return "\n\n".join(p for p in [para1, para2, para3] if p.strip())

    def compose_staff(self, ctx: dict) -> str:
        return self._compose(ctx, STAFF_SECTION_ORDER, self.staff_selector, "staff")

    def compose_lab(self, ctx: dict) -> str:
        return self._compose(ctx, LAB_SECTION_ORDER, self.lab_selector, "lab")

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "staff_vectorizer":      self.staff_selector.vectorizer,
                "staff_tag_matrix":      self.staff_selector._tag_matrix,
                "lab_vectorizer":        self.lab_selector.vectorizer,
                "lab_tag_matrix":        self.lab_selector._tag_matrix,
                "classifier_vectorizer": self.section_classifier.vectorizer,
                "classifier":            self.section_classifier.clf,
            }, f)
        logger.info("Composer model saved to %s", path)

    @classmethod
    def load(cls, path: Path) -> "NarrativeComposer":
        composer = cls.__new__(cls)
        composer.staff_selector     = TemplateSelector(STAFF_TEMPLATES + SHARED_TEMPLATES)
        composer.lab_selector       = TemplateSelector(LAB_TEMPLATES   + SHARED_TEMPLATES)
        composer.section_classifier = SectionClassifier()

        with open(path, "rb") as f:
            data = pickle.load(f)

        composer.staff_selector.vectorizer      = data["staff_vectorizer"]
        composer.staff_selector._tag_matrix     = data["staff_tag_matrix"]
        composer.staff_selector._fitted         = True
        composer.lab_selector.vectorizer        = data["lab_vectorizer"]
        composer.lab_selector._tag_matrix       = data["lab_tag_matrix"]
        composer.lab_selector._fitted           = True
        composer.section_classifier.vectorizer  = data["classifier_vectorizer"]
        composer.section_classifier.clf         = data["classifier"]
        composer.section_classifier._trained    = True

        logger.info("Composer model loaded from %s", path)
        return composer


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def _get_composer() -> NarrativeComposer:
    global _composer
    if _composer is not None:
        return _composer
    with _composer_lock:
        if _composer is not None:
            return _composer
        if _MODEL_PATH.exists():
            try:
                _composer = NarrativeComposer.load(_MODEL_PATH)
                return _composer
            except Exception as e:
                logger.warning("Could not load composer from disk: %s — rebuilding.", e)
        _composer = NarrativeComposer()
        try:
            _composer.save(_MODEL_PATH)
        except Exception as e:
            logger.warning("Could not save composer model: %s", e)
    return _composer


def _format_context(ctx: dict) -> str:
    """Format context dict as key-value lines (used by pipeline.py)."""
    lines = []
    for key, value in ctx.items():
        if value is not None and str(value) not in ("N/A", "None", ""):
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def compose_staff_summary(ctx: dict) -> str:
    """Generate a staff profile narrative using TF-IDF template selection."""
    return _get_composer().compose_staff(ctx)


def compose_lab_summary(ctx: dict) -> str:
    """Generate a lab user narrative using TF-IDF template selection."""
    return _get_composer().compose_lab(ctx)


def warm_up():
    """
    Pre-initialise the composer at server startup.
    Call this from init_rag() or app.py so the first request
    doesn't pay the initialisation cost.
    """
    _get_composer()
    logger.info("NarrativeComposer warmed up.")
