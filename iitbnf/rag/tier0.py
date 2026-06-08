"""
tier0.py — Zero-model factual lookup gate
==========================================
Sits in front of rag_chat() in agent.py.

For any question that is directly answerable from the profile context dict,
this module returns an answer immediately — zero model calls, zero latency.

If the question cannot be answered here, returns None and the caller falls
through to the model (Tier 2).

Architecture
────────────
Each entry in LOOKUP_RULES is a dict:

    patterns  : list[str]
        Regex patterns (case-insensitive) matched against the cleaned question.
        ANY match triggers this rule.

    fields    : list[str]
        ctx keys that must be present and non-zero/non-empty for the rule
        to fire. If any required field is missing, the rule is skipped and
        the question falls through to the model.

    formatter : callable(ctx) → str
        Produces the human-readable answer string from ctx values.
        Must never raise — all field access should use .get() with defaults.

    intent    : str
        Short label used in logs and the returned metadata dict.
        Also exposed in the response so the UI can show "answered instantly".

Adding new rules
────────────────
Append a new dict to LOOKUP_RULES. No other file needs to change.
The rules are tried in order; the first match wins.

Public API
──────────
    lookup(question, ctx) → dict | None

    Returns:
        {
            "answer":   str,
            "intent":   str,    # e.g. "attendance"
            "tier":     0,
            "success":  True,
            "latency_ms": float
        }
    or None if no rule matched.
"""

import re
import time
import logging

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _int(ctx: dict, key: str, default: int = 0) -> int:
    try:
        return int(ctx.get(key) or default)
    except (TypeError, ValueError):
        return default


def _str(ctx: dict, key: str, default: str = "N/A") -> str:
    v = ctx.get(key)
    return str(v) if v and str(v) not in ("N/A", "None", "") else default


def _float(ctx: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(ctx.get(key) or default)
    except (TypeError, ValueError):
        return default


def _pct_qualifier(pct: float) -> str:
    if pct >= 90:
        return "excellent — well above the 75% mandatory threshold"
    if pct >= 75:
        return "acceptable — meets the 75% mandatory threshold"
    return "below the 75% mandatory threshold and may need attention"


def _pluralise(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or singular + "s")


# ══════════════════════════════════════════════════════════════════════════════
# LOOKUP RULES
# ══════════════════════════════════════════════════════════════════════════════

LOOKUP_RULES: list[dict] = [

    # ── Identity ──────────────────────────────────────────────────────────────

    {
        "intent":   "name",
        "patterns": [
            r"\bwhat.{0,10}(name|called)\b",
            r"\bwho (is|are) (this|the) (person|member|user|staff)\b",
            r"\bidentif(y|ication)\b",
        ],
        "fields":   ["name"],
        "formatter": lambda ctx: (
            f"This profile belongs to {_str(ctx, 'name')}."
        ),
    },

    {
        "intent":   "designation",
        "patterns": [
            r"\b(designation|title|position|role|post)\b",
            r"\bwhat.{0,10}(job|work|do)\b",
        ],
        "fields":   ["name"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} holds the designation of "
            f"{_str(ctx, 'designation')} in the {_str(ctx, 'team')} team."
        ),
    },

    {
        "intent":   "team",
        "patterns": [
            r"\b(which|what).{0,15}team\b",
            r"\bteam.{0,10}(belong|part of|member)\b",
        ],
        "fields":   ["team"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} is part of the {_str(ctx, 'team')} team."
        ),
    },

    {
        "intent":   "joining_date",
        "patterns": [
            r"\b(when|date).{0,15}(join|joined|start|started|onboard)\b",
            r"\bhow long.{0,15}(working|been here|with)\b",
            r"\bjoining date\b",
            r"\btenure\b",
        ],
        "fields":   ["joining_date"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} joined the facility on {_str(ctx, 'joining_date')}."
        ),
    },

    {
        "intent":   "email",
        "patterns": [
            r"\b(email|e-mail|contact|reach)\b",
        ],
        "fields":   ["name"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')}'s email is {_str(ctx, 'email', 'not on record')}."
        ),
    },

    # ── Attendance ────────────────────────────────────────────────────────────

    {
        "intent":   "attendance",
        "patterns": [
            r"\battendance\b",
            r"\b(how many|number of).{0,10}days.{0,10}(present|attended|came)\b",
            r"\b(present|absent|attendance).{0,10}(percent|%|rate|pct)\b",
            r"\b(attendance|present).{0,10}(this year|year|2\d{3})\b",
            r"\babove.{0,10}75\b",
            r"\bmandatory threshold\b",
        ],
        "fields":   ["attendance_pct", "days_present"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has an attendance rate of "
            f"{_float(ctx, 'attendance_pct'):.1f}% this year "
            f"({_int(ctx, 'days_present')} of {_int(ctx, 'working_days')} working days). "
            f"This is {_pct_qualifier(_float(ctx, 'attendance_pct'))}."
        ),
    },

    {
        "intent":   "leaves",
        "patterns": [
            r"\b(leave|leaves|leave taken|leave days|days on leave)\b",
            r"\b(annual|casual|sick|earned|medical).{0,10}leave\b",
            r"\bleave breakdown\b",
        ],
        "fields":   ["leaves_taken"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has taken {_int(ctx, 'leaves_taken')} "
            f"leave {_pluralise(_int(ctx, 'leaves_taken'), 'day')} this year"
            + (
                f". Breakdown: {_str(ctx, 'leave_breakdown')}."
                if ctx.get("leave_breakdown")
                else "."
            )
        ),
    },
    {
        "intent":   "attendance_regularity",
        "patterns": [
            r"\b(regular|irregular|consistent|punctual)\b",
            r"\bwas.{0,15}(regular|present|attending)\b",
            r"\bmore regular\b",
        ],
        "fields":   ["attendance_pct", "days_present"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} had {_float(ctx, 'attendance_pct'):.1f}% attendance "
            f"({_int(ctx, 'days_present')} of {_int(ctx, 'working_days')} working days). "
            + (
                "This meets the 75% mandatory threshold."
                if _float(ctx, 'attendance_pct') >= 75
                else "This is below the 75% mandatory threshold."
            )
        ),
    },
    # ── Equipment / Slot activity ─────────────────────────────────────────────

    {
        "intent":   "equipment_requests",
        "patterns": [
            r"\b(equipment|machine).{0,20}(request|booking|usage)",
            r"\bhow many.{0,20}(equipment|machine).{0,20}(request|booking|usage)",
            r"\bequipment usage\b",
            r"\bslot.{0,10}(booked|booking)",
            r"\btool.{0,15}(request|usage|booking)",
            r"\bhow many.{0,20}(request|booking).{0,10}(made|submitted|placed)",
            r"\b(machine|equipment).{0,15}(booked|booking|used)",
        ],
        "fields":   ["eq_requests"],
        "skip_if": lambda q, ctx: bool(re.search(r'\b20\d{2}\b', q)),
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has submitted {_int(ctx, 'eq_requests')} equipment usage "
            f"{_pluralise(_int(ctx, 'eq_requests'), 'request')}. "
            + (
                f"{_int(ctx, 'eq_slot_booked') or _int(ctx, 'approved_requests')} "
                f"have been approved or slot-booked."
                if (_int(ctx, 'eq_slot_booked') or _int(ctx, 'approved_requests'))
                else ""
            )
        ),
    },

    {
        "intent":   "reservations",
        "patterns": [
            r"\b(slot|lab).{0,15}(reservation|booked|booking)\b",
            r"\bhow many.{0,15}reservation",
            r"\btotal.{0,10}(booking|slot)\b",
            r"\bhow many.{0,15}slot",
        ],
        "fields":   ["total_bookings"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has made {_int(ctx, 'total_bookings')} slot "
            f"{_pluralise(_int(ctx, 'total_bookings'), 'reservation')} across "
            f"{_int(ctx, 'tools_used')} {_pluralise(_int(ctx, 'tools_used'), 'piece')} of equipment."
        ),
    },

    {
        "intent":   "tool_permissions",
        "patterns": [
            r"\b(tool|equipment).{0,15}(permission|access|authoris|authoriz)\b",
            r"\bhow many.{0,15}(tool|equipment).{0,15}(access|permission|authoris)\b",
            r"\b(permission|authoris|authoriz).{0,15}(tool|equipment)\b",
            r"\bhow many permissions",
            r"\btool permission",
        ],
        "fields":   ["tool_permissions_count"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} holds access permissions for "
            f"{_int(ctx, 'tool_permissions_count')} "
            f"{_pluralise(_int(ctx, 'tool_permissions_count'), 'piece')} of equipment."
        ),
    },

    # ── System ownership ──────────────────────────────────────────────────────

    {
        "intent":   "system_ownership",
        "patterns": [
            r"\b(system owner|system ownership|owns.{0,10}system)\b",
            r"\bhow many.{0,10}(system|tool).{0,10}(own|assigned|responsible)\b",
            r"\bassigned.{0,10}(system|tool)\b",
        ],
        "fields":   ["systems_owned_current"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} is currently assigned as system owner for "
            f"{_int(ctx, 'systems_owned_current')} "
            f"{_pluralise(_int(ctx, 'systems_owned_current'), 'tool')}."
            + (
                f" Over their tenure, they have owned {_int(ctx, 'systems_owned_ever')} tools in total."
                if _int(ctx, "systems_owned_ever") > _int(ctx, "systems_owned_current")
                else ""
            )
        ),
    },

    # ── Research output ───────────────────────────────────────────────────────

    {
        "intent":   "publications",
        "patterns": [
            r"\b(paper|publication|research output|publish|published)\b",
            r"\bhow many.{0,20}(paper|publication)\b",
            r"\btell me about.{0,20}(paper|publication)\b",
            r"\b(paper|publication).{0,10}(does|do|has|have)\b",
        ],
        "fields":   ["papers"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has {_int(ctx, 'papers')} approved research "
            f"{_pluralise(_int(ctx, 'papers'), 'publication')} associated with IITBNF."
        ),
    },

    {
        "intent":   "projects",
        "patterns": [
            r"\b(project|faculty project)",
            r"\bhow many.{0,20}project",
            r"\bproject.{0,10}(count|total|number)",
        ],
        "fields":   ["projects"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} is associated with {_int(ctx, 'projects')} "
            f"faculty {_pluralise(_int(ctx, 'projects'), 'project')}"
            + (
                f", of which {_int(ctx, 'active_projects')} "
                f"{_pluralise(_int(ctx, 'active_projects'), 'is', 'are')} currently active."
                if _int(ctx, "active_projects")
                else "."
            )
        ),
    },

    {
        "intent":   "training",
        "patterns": [
            r"\b(training|trained|training session)\b",
            r"\bhow many.{0,10}training\b",
        ],
        "fields":   ["trainings"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has completed {_int(ctx, 'trainings')} equipment "
            f"training {_pluralise(_int(ctx, 'trainings'), 'session')}."
        ),
    },
    {
    "intent":   "logbook",
    "patterns": [r"\b(logbook|log book|session log)\b"],
    "fields":   ["logbook_total_entries"],
    "formatter": lambda ctx: (
        f"{_str(ctx, 'name')} has {_int(ctx, 'logbook_total_entries')} logbook "
        f"{_pluralise(_int(ctx, 'logbook_total_entries'), 'entry', 'entries')} "
        f"across {_int(ctx, 'logbook_tools_count')} "
        f"{_pluralise(_int(ctx, 'logbook_tools_count'), 'tool')}."
    ),
    },

    # ── Lab-specific ──────────────────────────────────────────────────────────

    {
        "intent":   "supervisor",
        "patterns": [
            r"\b(supervisor|guide|adviser|advisor)\b",
            r"\bwho.{0,15}(supervise|guide)\b",
            r"\bunder whom\b",
            r"\bwho.{0,10}(is|are).{0,10}(his|her|their|the).{0,10}(supervisor|guide)\b",
            r"\bsupervises.{0,15}(this|the)\b",
        ],
        "fields":   ["supervisor_name"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')}'s supervisor is {_str(ctx, 'supervisor_name', 'not on record')}."
        ),
    },

    {
        "intent":   "research_area",
        "patterns": [
            r"\b(research area|research focus|research topic|working on|research interest)\b",
            r"\bwhat.{0,10}research\b",
        ],
        "fields":   ["research_area"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')}'s research focus is: {_str(ctx, 'research_area', 'not specified')}."
        ),
    },

    {
        "intent":   "department",
        "patterns": [
            r"\b(department|dept|which dept|which department)\b",
            r"\bwhich team\b",
            r"\bwhat team\b",
        ],
        "fields":   ["name"],   # changed from ["department"]
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} is from the "
            f"{_str(ctx, 'department') if ctx.get('department') and ctx.get('department') not in ('N/A','') else _str(ctx, 'team', 'N/A')} "
            f"{'department' if ctx.get('department') and ctx.get('department') not in ('N/A','') else 'team'}."
        ),
    },

    {
        "intent":   "cancellations",
        "patterns": [
            r"\b(cancel|cancellation|cancelled reservation)",
            r"\bhow many.{0,20}cancel",
        ],
        "fields":   ["cancellations"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has {_int(ctx, 'cancellations')} reservation "
            f"{_pluralise(_int(ctx, 'cancellations'), 'cancellation')} on record."
        ),
    },

    {
        "intent":   "session_reports",
        "patterns": [
            r"\b(session report|equipment report|usage report)",
            r"\bhow many.{0,20}session.{0,10}report",
            r"\bsession report.{0,10}filed",
        ],
        "fields":   ["session_reports"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has filed {_int(ctx, 'session_reports')} equipment "
            f"session {_pluralise(_int(ctx, 'session_reports'), 'report')}."
        ),
    },

    # ── Monthly reports (staff) ───────────────────────────────────────────────

    {
        "intent":   "monthly_reports",
        "patterns": [
            r"\bmonthly report",
            r"\bhow many.{0,20}report.{0,20}submit",
            r"\breport.{0,10}(star|rating|score)\b",
            r"\bhow many.{0,20}monthly.{0,20}report",
        ],
        "fields":   ["monthly_reports_submitted"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} has submitted {_int(ctx, 'monthly_reports_submitted')} "
            f"monthly {_pluralise(_int(ctx, 'monthly_reports_submitted'), 'report')}"
            + (
                f" with an average rating of {_str(ctx, 'monthly_report_avg_stars')} stars."
                if ctx.get("monthly_report_avg_stars") not in (None, "N/A", "")
                else "."
            )
        ),
    },

    # ── Appointment / qualification ───────────────────────────────────────────

    {
        "intent":   "appointment_type",
        "patterns": [
            r"\b(appointment|contract|basis)\b",
            r"\btype of appointment\b",
        ],
        "fields":   ["appointment_type"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')} is on a {_str(ctx, 'appointment_type')} appointment."
        ),
    },

    {
        "intent":   "qualification",
        "patterns": [
            r"\b(qualification|degree|education|academic)\b",
        ],
        "fields":   ["qualification"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')}'s qualification is recorded as {_str(ctx, 'qualification')}."
        ),
    },

    # ── Expiry (lab) ──────────────────────────────────────────────────────────

    {
        "intent":   "expiry",
        "patterns": [
            r"\b(expir|expire|expiry|access expir|valid till|valid until)\b",
        ],
        "fields":   ["expiry_date"],
        "formatter": lambda ctx: (
            f"{_str(ctx, 'name')}'s lab access expires on "
            f"{_str(ctx, 'expiry_date', 'not set or permanent')}."
        ),
    },
    # Add to LOOKUP_RULES:

{
    "intent":   "attendance_summary",
    "patterns": [
        r"\b(summarize|summary|overview).{0,20}attendance\b",
        r"\battendance.{0,20}(good|bad|poor|excellent|acceptable)\b",
        r"\bis.{0,10}attendance.{0,10}(good|ok|acceptable|above|below)\b",
    ],
    "fields":   ["attendance_pct", "days_present"],
    "formatter": lambda ctx: (
        f"{_str(ctx, 'name')} has {_float(ctx, 'attendance_pct'):.1f}% attendance "
        f"({_int(ctx, 'days_present')} of {_int(ctx, 'working_days')} working days). "
        + ("This meets the 75% mandatory threshold."
           if _float(ctx, 'attendance_pct') >= 75
           else "This is below the 75% mandatory threshold.")
    ),
},

{
    "intent":   "overall_profile",
    "patterns": [
        r"\b(tell me about|who is|describe|what do you know about)\b",
        r"\boverview\b",
        r"\bprofile (summary|overview)\b",
    ],
    "fields":   ["name"],
    "formatter": lambda ctx: (
        f"{_str(ctx, 'name')} is a {_str(ctx, 'category') or _str(ctx, 'designation')} "
        f"in the {_str(ctx, 'team')} team"
        f"in the {_str(ctx, 'department', '')} department"
        + (f", joined {_str(ctx, 'joining_date')}" if ctx.get('joining_date') else "")
        + f". Attendance: {_float(ctx, 'attendance_pct'):.1f}%"
        + (f", {_int(ctx, 'eq_requests')} equipment requests"
           if _int(ctx, 'eq_requests') > 0 else "")
        + f". Publications: {_int(ctx, 'papers')}."
        + f" Projects: {_int(ctx, 'projects')}."
    ),
},

{
    "intent":   "is_active",
    "patterns": [
        r"\b(is|are).{0,10}(active|still here|still working|still employed)\b",
        r"\bstill.{0,10}(working|active|with)\b",
    ],
    "fields":   ["name"],
    "formatter": lambda ctx: (
        f"{_str(ctx, 'name')} is an active member — their profile is in the system "
        f"with no clearance recorded."
    ),
},
]


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════
# If question contains a year + activity keyword, skip Tier 0
# Let query_router handle it with proper DB year filtering

def lookup(question: str, ctx: dict) -> dict | None:
    """
    Try to answer question directly from ctx without any model call.

    Args:
        question : cleaned natural language question string
        ctx      : profile context dict from _build_staff_context /
                   _build_lab_context

    Returns:
        Answer dict on match, None if no rule matched.

    The returned dict is compatible with rag_chat()'s return format so
    callers can use it as a drop-in:
        {
            "answer":     str,
            "intent":     str,
            "tier":       0,
            "success":    True,
            "latency_ms": float
        }
    """
    t0 = time.perf_counter()
    q  = question.lower().strip()

    YEAR_ACTIVITY_PATTERNS = (
        r"\b20\d{2}\b.{0,30}(slot|equipment|request|booking|reservation|attendance|present|active)",
        r"(slot|equipment|request|booking|reservation|attendance|present|active).{0,30}\b20\d{2}\b",
        r"(in|for|during)\s+20\d{2}",
        r"(compare|comparison|change|differ).{0,30}\b20\d{2}\b",
    )
    if any(re.search(p, q) for p in YEAR_ACTIVITY_PATTERNS):
        logger.debug("[Tier0] year-specific activity question — bypassing to query router: %r", q[:60])
        return None
    # Questions that ask for explanation, analysis, comparison, or trends
    # should always go to the model — never answer from a dict lookup.
    ANALYSIS_PREFIXES = (
        r"^explain\b",
        r"^analyse\b", r"^analyze\b",
        r"^why\b",
        r"^compare\b",
        r"^contrast\b",
        r"\bpattern\b",
        r"\btrend\b",
        r"\bover.{0,10}(time|year|month)\b",
        r"\bhistory of\b",
    )
    if any(re.search(p, q) for p in ANALYSIS_PREFIXES):
        logger.debug("[Tier0] analysis/trend question — bypassing to model: %r", q[:60])
        return None

    for rule in LOOKUP_RULES:
        # Check if any pattern matches
        matched = any(
            re.search(pattern, q, re.IGNORECASE)
            for pattern in rule["patterns"]
        )
        if not matched:
            continue

        # Check that required ctx fields exist and are meaningful
        fields_ok = all(
            ctx.get(f) not in (None, "", "N/A", "NA", 0)
            for f in rule["fields"]
        )
        if not fields_ok:
            # Field exists but is empty — give a "not on record" answer
            # rather than silently falling through to the model, which would
            # hallucinate a value.
            name = ctx.get("name", "This person")
            intent = rule["intent"]
            elapsed = round((time.perf_counter() - t0) * 1000, 2)
            logger.info(
                "[Tier0] intent=%s matched but fields empty — returning not-on-record (%.1fms)",
                intent, elapsed,
            )
            return {
                "answer":     f"{name}: no {intent.replace('_', ' ')} data is currently on record.",
                "intent":     intent,
                "tier":       0,
                "success":    True,
                "latency_ms": elapsed,
            }

        # Run the formatter — if it raises, skip and fall through to model
        try:
            answer  = rule["formatter"](ctx)
            elapsed = round((time.perf_counter() - t0) * 1000, 2)
            logger.info(
                "[Tier0] intent=%s answered in %.1fms: %s",
                rule["intent"], elapsed, answer[:80],
            )
            return {
                "answer":     answer,
                "intent":     rule["intent"],
                "tier":       0,
                "success":    True,
                "latency_ms": elapsed,
            }
        except Exception as exc:
            logger.warning("[Tier0] formatter for intent=%s raised: %s", rule["intent"], exc)
            continue

    elapsed = round((time.perf_counter() - t0) * 1000, 2)
    logger.debug("[Tier0] no rule matched for question=%r (%.1fms)", question[:60], elapsed)
    return None