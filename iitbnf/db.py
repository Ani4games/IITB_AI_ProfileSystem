"""
db.py — Connection pools and query convenience functions.
"""
import time
import threading
from queue import Queue, Empty, Full
import pymysql
import pymysql.cursors
from config import DB_HR, DB_SLOTS


class SimpleConnectionPool:
    """Windows-compatible database connection pool."""

    def __init__(self, db_config, max_connections=10, min_connections=2):
        self.db_config       = db_config
        self.max_connections = max_connections
        self._pool           = Queue(maxsize=max_connections)
        self._active         = 0
        self._lock           = threading.Lock()
        self._closed         = False
        for _ in range(min_connections):
            self._create_connection()

    def _create_connection(self):
        try:
            conn = pymysql.connect(
                host=self.db_config["host"],
                user=self.db_config["user"],
                password=self.db_config["password"],
                database=self.db_config["database"],
                charset=self.db_config.get("charset", "utf8mb4"),
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
            with self._lock:
                self._pool.put(conn, block=False)
                self._active += 1
            return True
        except Exception as e:
            print(f"[DB] Failed to create connection: {e}")
            return False

    def get_connection(self, timeout=5):
        if self._closed:
            raise Exception("Connection pool is closed")
        try:
            conn = self._pool.get(block=True, timeout=timeout)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                return conn
            except Exception:
                with self._lock:
                    self._active -= 1
                return self._create_new_connection()
        except Empty:
            return self._create_new_connection()

    def _create_new_connection(self):
        with self._lock:
            if self._active < self.max_connections:
                conn = pymysql.connect(
                    host=self.db_config["host"],
                    user=self.db_config["user"],
                    password=self.db_config["password"],
                    database=self.db_config["database"],
                    charset=self.db_config.get("charset", "utf8mb4"),
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                )
                self._active += 1
                return conn
            return self._pool.get(block=True, timeout=5)

    def return_connection(self, conn):
        if self._closed:
            conn.close()
            return
        try:
            self._pool.put(conn, block=False)
        except Full:
            conn.close()
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


# ── Pool instances ────────────────────────────────────────────────────────────
hr_pool    = SimpleConnectionPool(DB_HR,    max_connections=5, min_connections=1)
slots_pool = SimpleConnectionPool(DB_SLOTS, max_connections=5, min_connections=1)


# ── Query helpers ─────────────────────────────────────────────────────────────
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
            if attempt == retry:
                return []
            time.sleep(0.1 * (attempt + 1))


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


# ── Convenience wrappers ──────────────────────────────────────────────────────
def hr_query(sql, params=None):    return execute_query(hr_pool,    sql, params)
def slots_query(sql, params=None): return execute_query(slots_pool, sql, params)
def hr_execute(sql, params=None):  return execute_write(hr_pool,    sql, params)
def slots_execute(sql, params=None): return execute_write(slots_pool, sql, params)
