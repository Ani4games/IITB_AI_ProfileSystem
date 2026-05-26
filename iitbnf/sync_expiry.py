"""
sync_expiry.py — One-time sync script
======================================
Copies leaving_date from hr_portal.profile into expiry_date in
slotbooking.login for any staff member where the two are out of sync.

Run from the iitbnf/ project root:
    python sync_expiry.py

Safe to run multiple times — only updates rows that are actually mismatched.
Prints a summary of every change made.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from db import hr_query, slots_execute, slots_query
from datetime import date

DRY_RUN = "--dry-run" in sys.argv   # pass --dry-run to preview without writing


def main():
    print("=" * 60)
    print("  IITBNF Expiry Date Sync")
    print(f"  Mode: {'DRY RUN (no changes written)' if DRY_RUN else 'LIVE'}")
    print("=" * 60)

    # Fetch all staff with a leaving_date set in HR
    left = hr_query("""
        SELECT p.member_id, p.leaving_date,
               TRIM(CONCAT(COALESCE(l.fname,''), ' ', COALESCE(l.lname,''))) AS name
        FROM hr_portal.profile p
        LEFT JOIN slotbooking.login l ON l.memberid = p.member_id
        WHERE p.leaving_date IS NOT NULL
          AND p.leaving_date != '0000-00-00'
          AND p.leaving_date != ''
    """)

    if not left:
        print("No staff with leaving_date found in hr_portal.profile.")
        return

    print(f"\nFound {len(left)} staff with leaving_date in HR.\n")

    updated = 0
    skipped = 0
    not_found = 0

    for row in left:
        member_id   = row["member_id"]
        leaving_date = row["leaving_date"]   # date object
        name        = (row.get("name") or "").strip() or f"Member #{member_id}"

        # Check if they exist in slotbooking.login
        slot = slots_query(
            "SELECT memberid, expiry_date FROM login WHERE memberid = %s LIMIT 1",
            (member_id,)
        )

        if not slot:
            print(f"  [NOT FOUND] {name} (ID {member_id}) — not in slotbooking.login, skipping.")
            not_found += 1
            continue

        current_expiry = slot[0].get("expiry_date") or ""

        # Convert leaving_date to MM/DD/YYYY to match slotbooking format
        if isinstance(leaving_date, date):
            target_expiry = leaving_date.strftime("%m/%d/%Y")
        else:
            try:
                from datetime import datetime
                target_expiry = datetime.strptime(str(leaving_date), "%Y-%m-%d").strftime("%m/%d/%Y")
            except Exception:
                print(f"  [SKIP] {name} (ID {member_id}) — could not parse leaving_date: {leaving_date}")
                skipped += 1
                continue

        if current_expiry == target_expiry:
            skipped += 1
            continue

        print(f"  [UPDATE] {name} (ID {member_id}) — expiry_date: '{current_expiry}' → '{target_expiry}'")

        if not DRY_RUN:
            result = slots_execute(
                "UPDATE login SET expiry_date = %s WHERE memberid = %s",
                (target_expiry, member_id)
            )
            if not result.get("ok"):
                print(f"  [ERROR] {name} (ID {member_id}) — {result.get('error')}")
                updated += 1

    print(f"\n{'─' * 60}")
    print(f"  Updated  : {updated}")
    print(f"  Skipped  : {skipped} (already in sync or unparseable)")
    print(f"  Not found: {not_found} (not in slotbooking.login)")
    if DRY_RUN:
        print("\n  DRY RUN — no changes written. Remove --dry-run to apply.")
    else:
        print("\n  Sync complete.")


if __name__ == "__main__":
    main()
