# test_db_latency.py
import sys, time
sys.path.insert(0, '.')

from app import app

with app.app_context():
    from db import hr_query, slots_query, hr_pool

    print("Testing DB latency...\n")

    # Test 1: raw connection time
    t0   = time.perf_counter()
    conn = hr_pool.get_connection()
    conn_ms = round((time.perf_counter() - t0) * 1000, 1)
    hr_pool.return_connection(conn)
    print(f"Connection from pool : {conn_ms} ms")

    # Test 2: minimal query
    t0 = time.perf_counter()
    hr_query("SELECT 1")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"SELECT 1 (hr)        : {ms} ms")

    t0 = time.perf_counter()
    slots_query("SELECT 1")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"SELECT 1 (slots)     : {ms} ms")

    # Test 3: PK lookup
    t0 = time.perf_counter()
    hr_query("SELECT member_id FROM profile WHERE member_id = 189 LIMIT 1")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"PK lookup (hr)       : {ms} ms")

    t0 = time.perf_counter()
    slots_query("SELECT memberid FROM login WHERE memberid = 189 LIMIT 1")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    print(f"PK lookup (slots)    : {ms} ms")

    # Test 4: same query 5 times to see variance
    print("\nRepeated SELECT 1 (hr) x5:")
    for i in range(5):
        t0 = time.perf_counter()
        hr_query("SELECT 1")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        print(f"  run {i+1}: {ms} ms")

    print("\nRepeated SELECT 1 (slots) x5:")
    for i in range(5):
        t0 = time.perf_counter()
        slots_query("SELECT 1")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        print(f"  run {i+1}: {ms} ms")