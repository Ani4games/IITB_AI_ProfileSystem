"""
db.py — Connection pools and query convenience functions.

Performance fixes applied
─────────────────────────
1. Pool sizes raised from 5 → 10 (hr) / 12 (slots).  Under parallel profile
   loads each page fires 10-14 concurrent DB calls; the old pool of 5 caused
   threads to queue and wait for a free connection.

2. Removed the "SELECT 1" ping on every get_connection() call.
   The old code ran a round-trip to the DB *before* every real query just to
   check the connection was alive.  Instead we now do a lightweight
   conn.ping(reconnect=True) which is a local socket check — only actually
   reconnects when the connection is genuinely dead (rare).

3. min_connections raised from 1 → 3 so warm connections are available
   from the very first request after server start (avoids the cold-start
   penalty on login → admin).

4. connect_timeout added (2 s) so a bad DB host fails fast instead of
   hanging for 30 s.

5. read_timeout / write_timeout added (10 s) to prevent a slow query from
   blocking a thread forever.
"""
import time
import threading
from queue import Queue, Empty, Full
import pymysql
import pymysql.cursors
from config import DB_HR, DB_SLOTS


class SimpleConnectionPool:
    """Thread-safe database connection pool."""

    def __init__(self, db_config, max_connections=10, min_connections=3):
        self.db_config       = db_config
        self.max_connections = max_connections
        self._pool           = Queue(maxsize=max_connections)
        self._active         = 0
        self._lock           = threading.Lock()
        self._closed         = False
        # Pre-warm min_connections so the first page load doesn't pay the
        # connection-creation cost.
        for _ in range(min_connections):
            self._create_connection()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _make_conn(self):
        """Open a fresh pymysql connection with sensible timeouts."""
        return pymysql.connect(
            host            = self.db_config["host"],
            user            = self.db_config["user"],
            password        = self.db_config["password"],
            database        = self.db_config["database"],
            charset         = self.db_config.get("charset", "utf8mb4"),
            cursorclass     = pymysql.cursors.DictCursor,
            autocommit      = False,
            connect_timeout = 2,    # fail fast if DB is unreachable
            read_timeout    = 10,   # don't block forever on a slow query
            write_timeout   = 10,
        )

    def _create_connection(self):
        try:
            conn = self._make_conn()
            with self._lock:
                self._pool.put(conn, block=False)
                self._active += 1
            return True
        except Exception as e:
            print(f"[DB] Failed to create connection: {e}")
            return False

    # ── public API ────────────────────────────────────────────────────────────

    def get_connection(self, timeout=5):
        if self._closed:
            raise Exception("Connection pool is closed")

        try:
            conn = self._pool.get(block=True, timeout=timeout)
        except Empty:
            return self._create_new_connection()

        # Lightweight liveness check — pymysql ping() is a local socket op
        # (no DB round-trip) unless the socket is broken, in which case it
        # re-connects automatically.  This replaces the old "SELECT 1" ping.
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            # Socket is gone — decrement counter and open a fresh connection.
            with self._lock:
                self._active -= 1
            return self._create_new_connection()

    def _create_new_connection(self):
        with self._lock:
            if self._active < self.max_connections:
                try:
                    conn = self._make_conn()
                    self._active += 1
                    return conn
                except Exception as e:
                    raise Exception(f"Could not open new DB connection: {e}")
        # Pool is at capacity — wait for one to be returned.
        return self._pool.get(block=True, timeout=5)

    def return_connection(self, conn):
        if self._closed:
            try:
                conn.close()
            except Exception:
                pass
            return
        try:
            self._pool.put(conn, block=False)
        except Full:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._active -= 1

    def close_all(self):
        self._closed = True
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
                with self._lock:
                    self._active -= 1
            except Exception:
                pass


# ── Pool instances ─────────────────────────────────────────────────────────────
# Slots DB gets a larger pool because profile pages fire more parallel queries
# against it (reservations, equipment, permissions, system_owner, etc.).
hr_pool    = SimpleConnectionPool(DB_HR,    max_connections=10, min_connections=3)
slots_pool = SimpleConnectionPool(DB_SLOTS, max_connections=12, min_connections=3)


# ── Query helpers ──────────────────────────────────────────────────────────────
def execute_query(pool, sql, params=None, retry=1):
    """Execute a SELECT query with connection pooling and retry."""
    conn = None
    for attempt in range(retry + 1):
        try:
            conn = pool.get_connection(timeout=3)
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                result = cur.fetchall()
            pool.return_connection(conn)
            return result
        except Exception as e:
            print(f"[DB ERROR] Attempt {attempt + 1}: {e}")
            if conn:
                try:
                    pool.return_connection(conn)
                except Exception:
                    pass
                conn = None
            if attempt == retry:
                return []
            time.sleep(0.1 * (attempt + 1))
    return []


def execute_write(pool, sql, params=None):
    """Execute an INSERT/UPDATE/DELETE with commit."""
    conn = None
    try:
        conn = pool.get_connection()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            affected = cur.rowcount
            last_id  = cur.lastrowid
        conn.commit()
        pool.return_connection(conn)
        return {"ok": True, "affected": affected, "last_id": last_id}
    except Exception as e:
        if conn:
            try:
                conn.rollback()
                pool.return_connection(conn)
            except Exception:
                pass
        print(f"[DB WRITE ERROR] {e}")
        return {"ok": False, "error": str(e)}


# ── Convenience wrappers ───────────────────────────────────────────────────────
def hr_query(sql, params=None):      return execute_query(hr_pool,    sql, params)
def slots_query(sql, params=None):   return execute_query(slots_pool, sql, params)
def hr_execute(sql, params=None):    return execute_write(hr_pool,    sql, params)
def slots_execute(sql, params=None): return execute_write(slots_pool, sql, params)
