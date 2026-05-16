# test_pdf_speed.py — run with: python test_pdf_speed.py
import time
import sys
sys.path.insert(0, '.')

from app import app

MEMBER_ID = 189  # change to any valid member
YEAR      = 2026

with app.app_context():
    from models.staff import (
        get_person, get_attendance_stats, get_slot_activity,
        get_permissions, get_staff_tool_perms_rich,
        _warmup_uid, _get_uid_from_member
    )
    from db import slots_query
    from utils import run_parallel, safe_dict

    print("=" * 55)
    print("  IITBNF PDF Speed Diagnostic")
    print("=" * 55)

    # ── Step 1: DB queries ────────────────────────────────────
    print("\n[1] DB Query time...")
    t0 = time.perf_counter()
    _warmup_uid(MEMBER_ID)
    uid = _get_uid_from_member(MEMBER_ID)

    def owned_counts():
        if not uid:
            return {"total": 0, "working": 0}
        rows = slots_query(
            "SELECT machid FROM system_owner WHERE memberid=%s", (uid,)
        ) or []
        all_ids = []
        for r in rows:
            raw = str(r.get("machid") or "")
            all_ids += [x for x in raw.split(",") if x.strip().isdigit()]
        if not all_ids:
            return {"total": 0, "working": 0}
        ph = ",".join(["%s"] * len(all_ids))
        working = slots_query(
            f"SELECT COUNT(*) AS cnt FROM resources "
            f"WHERE machid IN ({ph}) AND isworking=1", tuple(all_ids)
        )
        return {
            "total":   len(all_ids),
            "working": int((working or [{}])[0].get("cnt") or 0),
        }

    def track_counts():
        if not uid:
            return {"total": 0, "active": 0}
        rows = slots_query("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN action='create' THEN 1 ELSE 0 END) -
                SUM(CASE WHEN action='delete' THEN 1 ELSE 0 END) AS active
            FROM system_owner_track WHERE memberid=%s
        """, (uid,)) or [{}]
        total  = int(rows[0].get("total") or 0)
        active = max(0, int(rows[0].get("active") or 0))
        return {"total": total, "active": active}

    data = run_parallel({
        "person":          lambda: get_person(MEMBER_ID),
        "attendance":      lambda: get_attendance_stats(MEMBER_ID, YEAR),
        "slot_activity":   lambda: get_slot_activity(MEMBER_ID, YEAR),
        "permissions":     lambda: get_permissions(MEMBER_ID),
        "tool_perms_rich": lambda: get_staff_tool_perms_rich(MEMBER_ID),
        "owned_counts":    owned_counts,
        "track_counts":    track_counts,
    })
    db_ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"    DB queries done: {db_ms} ms")

    # ── Step 2: HTML rendering ────────────────────────────────
    print("\n[2] HTML template render time...")
    from flask import render_template
    from datetime import datetime
    from models.lab import safe_json

    raw_slot    = data.get("slot_activity") or {}
    slot_activity = {k: v for k, v in raw_slot.items() if k != "rows"}
    slot_activity["rows"] = []

    owned_c = data.get("owned_counts") or {"total": 0, "working": 0}
    track_c = data.get("track_counts") or {"total": 0, "active": 0}
    system_owned = (
        [{"isworking": 1}] * owned_c["working"] +
        [{"isworking": 0}] * (owned_c["total"] - owned_c["working"])
    )
    system_owner_track = (
        [{"is_active": True}]  * track_c["active"] +
        [{"is_active": False}] * (track_c["total"] - track_c["active"])
    )

    t0 = time.perf_counter()
    with app.test_request_context():
        html = render_template(
            "profile_pdf.html",
            person             = safe_dict(data["person"]),
            att                = safe_json(data.get("attendance", {})),
            slot_activity      = slot_activity,
            projects           = {},
            permissions        = data.get("permissions", []),
            system_owned       = system_owned,
            system_owner_track = system_owner_track,
            tool_perms_rich    = data.get("tool_perms_rich") or [],
            member_id          = MEMBER_ID,
            now                = datetime.now().strftime("%d %b %Y, %I:%M %p"),
            selected_year      = YEAR,
        )
    html_ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"    HTML render done: {html_ms} ms")
    print(f"    HTML size: {len(html):,} bytes")

    # ── Step 3: xhtml2pdf ─────────────────────────────────────
    print("\n[3] xhtml2pdf PDF conversion time...")
    import io
    from xhtml2pdf import pisa

    def link_callback(uri, rel):
        """Block all external resource fetching — prevents network timeout."""
        if uri.startswith("file://"):
            return uri
        return None

    t0  = time.perf_counter()
    buf = io.BytesIO()
    result = pisa.CreatePDF(
        src           = html,
        dest          = buf,
        encoding      = "utf-8",
        link_callback = link_callback,
    )
    pdf_ms = round((time.perf_counter() - t0) * 1000, 1)

    # ── Step 4: File write ────────────────────────────────────
    print("\n[4] File write time...")
    import os
    t0 = time.perf_counter()
    os.makedirs("tmp", exist_ok=True)
    pdf_bytes = buf.getvalue()
    with open("tmp/test_output.pdf", "wb") as f:
        f.write(pdf_bytes)
    write_ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"    File write done: {write_ms} ms")

    # ── Summary ───────────────────────────────────────────────
    total = db_ms + html_ms + pdf_ms + write_ms
    print("\n" + "=" * 55)
    print("  SUMMARY")
    print("=" * 55)
    print(f"  DB queries   : {db_ms:>8} ms")
    print(f"  HTML render  : {html_ms:>8} ms")
    print(f"  PDF convert  : {pdf_ms:>8} ms  ← likely bottleneck")
    print(f"  File write   : {write_ms:>8} ms")
    print(f"  TOTAL        : {total:>8} ms")
    print("=" * 55)
    print("\n  PDF saved to: tmp/test_output.pdf")
    print("  Open it to verify it looks correct.")