"""
locustfile.py — System-wide load test for IITBNF.

Install:
    pip install locust

Run:
    locust -f locustfile.py --host=http://127.0.0.1:5000

Then open http://localhost:8089, set users/ramp-up, and watch:
    - This dashboard for p95 latency / RPS / failure rate
    - /debug/speed-dashboard for cache size + DB pool queue depth
    - /debug/timings?member_id=<id>&cold=1 for per-function cost under load

Headless run for CI / scripted runs:
    locust -f locustfile.py --host=http://127.0.0.1:5000 \
        --users 100 --spawn-rate 10 --run-time 5m --headless \
        --csv=results
"""

import random
from locust import HttpUser, task, between, events

# ── Fill these in with real IDs from your DB before running ──────────────────
STAFF_MEMBER_IDS = [189, 200, 245]      # hr_portal.profile.member_id values
LAB_MEMBER_IDS   = [2506, 2524, 2600]   # slotbooking.login.memberid values
YEARS            = [2023, 2024, 2025, 2026]

AI_QUESTIONS = [
    "What is the attendance this year?",
    "How many equipment requests were submitted?",
    "How many publications does this person have?",
    "What projects is this person associated with?",
    "Compare attendance in 2024 and 2025",
]


class IITBNFUser(HttpUser):
    wait_time = between(1, 3)   # think-time between actions, mimics real users

    def on_start(self):
        """
        Runs once per simulated user at session start.
        AUTH_DISABLED=True in auth.py currently bypasses login checks entirely,
        so this is harmless either way — but keep it so the test still works
        once you flip AUTH_DISABLED back to False for production.
        """
        self.client.post("/login", data={
            "email": "loadtest@iitb.ac.in",
            "password": "irrelevant-while-AUTH_DISABLED",
        })

    # ── Heaviest page: admin panel (get_all_members + get_all_lab_users) ─────
    @task(3)
    def admin_panel(self):
        self.client.get("/admin-panel/")

    # ── Staff profile page load ───────────────────────────────────────────────
    @task(5)
    def staff_profile(self):
        mid = random.choice(STAFF_MEMBER_IDS)
        self.client.get(f"/profile/{mid}")

    # ── Lab profile page load ─────────────────────────────────────────────────
    @task(5)
    def lab_profile(self):
        mid = random.choice(LAB_MEMBER_IDS)
        self.client.get(f"/lab/{mid}")

    # ── AJAX section calls — these hit @cached() functions in staff.py/lab.py.
    #    Under concurrent load this is where you find out if TTL caching is
    #    actually absorbing requests or if you're getting cache stampedes
    #    (many threads missing cache simultaneously and all hitting the DB). ──
    @task(8)
    def staff_attendance_section(self):
        mid = random.choice(STAFF_MEMBER_IDS)
        yr = random.choice(YEARS)
        self.client.get(f"/api/section/staff/{mid}/attendance?year={yr}")

    @task(6)
    def staff_slot_activity_section(self):
        mid = random.choice(STAFF_MEMBER_IDS)
        yr = random.choice(YEARS)
        self.client.get(f"/api/section/staff/{mid}/slot_activity?year={yr}")

    @task(3)
    def staff_logbook_section(self):
        mid = random.choice(STAFF_MEMBER_IDS)
        self.client.get(f"/api/section/staff/{mid}/logbook")

    @task(4)
    def lab_reservations_section(self):
        mid = random.choice(LAB_MEMBER_IDS)
        yr = random.choice(YEARS)
        self.client.get(f"/api/section/lab/{mid}/reservations?year={yr}")

    # ── PDF prefetch — fires a background thread per call. Under load this
    #    tells you if you're saturating Python threads / the DB pool with
    #    PDF jobs while normal traffic is also hitting it. ─────────────────────
    @task(1)
    def pdf_prefetch(self):
        mid = random.choice(STAFF_MEMBER_IDS)
        self.client.get(f"/profile/{mid}/pdf/prefetch")

    # ── AI stream — the SSE endpoint. Locust's HttpUser doesn't natively
    #    parse SSE, but issuing the GET and timing time-to-completion is
    #    still useful: it tells you connection setup + queueing time under
    #    load, even without parsing individual `data:` lines. ─────────────────
    @task(2)
    def ai_stream(self):
        mid = random.choice(STAFF_MEMBER_IDS)
        q = random.choice(AI_QUESTIONS)
        try:
            with self.client.get(
                f"/api/ai/stream?profile_type=staff&profile_id={mid}&message={q}",
                stream=True,
                catch_response=True,
                timeout=60,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"status={resp.status_code}")
                    return
                # Drain safely — raw can be None if connection was recycled
                if resp.raw is None:
                    resp.failure("raw socket is None — connection dropped")
                    return
                try:
                    for chunk in resp.iter_content(chunk_size=512):
                        if b"[DONE]" in chunk:
                            break
                except Exception as e:
                    resp.failure(f"stream read error: {e}")
        except Exception as e:
            # Don't crash the locust worker — just log and continue
            pass


# ── Print a reminder at test start so the operator checks the right dashboards
@events.test_start.add_listener
def _on_start(environment, **kwargs):
    print(
        "\n[load test] While this runs, also watch:\n"
        "  /debug/speed-dashboard   — cache size, hr_pool/slots_pool active+queue\n"
        "  /debug/timings?member_id=<id>&cold=1 — per-function cost\n"
        "  /debug/ping-analysis     — raw DB round-trip under contention\n"
    )