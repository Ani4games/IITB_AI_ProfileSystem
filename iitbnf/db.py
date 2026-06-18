"""
db.py — Connection pools and query convenience functions.

Uses mysql-connector-python for Windows named pipe connections.
Falls back to pymysql with TCP_NODELAY for TCP connections.
"""
import time
import threading
import sys
from queue import Queue, Empty, Full

import pymysql
import pymysql.cursors
from config import DB_HR, DB_SLOTS


def _make_single_conn(db_config):
    """
    Create one DB connection.
    Tries named pipe first on Windows, falls back to TCP.
    """
    use_pipe = db_config.get("use_named_pipe", False)
    pipe_name = db_config.get("pipe_name", "MySQL")

    # ── Named pipe via mysql-connector-python (Windows) ───────────────────
    if use_pipe and sys.platform == "win32":
        try:
            import mysql.connector
            conn = mysql.connector.connect(
                unix_socket     = f"\\\\.\\pipe\\{pipe_name}",
                user            = db_config["user"],
                password        = db_config["password"],
                database        = db_config["database"],
                charset         = db_config.get("charset", "utf8mb4"),
                use_pure        = False,       # pure Python — no C extension needed
                autocommit      = True,
                connection_timeout = 2,
            )
            print(f"[DB] Connected via named pipe: \\\\.\\pipe\\{pipe_name}")
            return ("connector", conn)
        except Exception as e:
            print(f"[DB] Named pipe failed ({e}), falling back to TCP")

    # ── TCP via pymysql ───────────────────────────────────────────────────
    import socket as _socket
    conn = pymysql.connect(
        host            = db_config.get("host", "localhost"),
        port            = int(db_config.get("port", 3306)),
        user            = db_config["user"],
        password        = db_config["password"],
        database        = db_config["database"],
        charset         = db_config.get("charset", "utf8mb4"),
        cursorclass     = pymysql.cursors.DictCursor,
        autocommit      = False,
        connect_timeout = 2,
        read_timeout    = 10,
        write_timeout   = 10,
    )
    sock = getattr(conn, '_sock', None)
    if sock:
        try:
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except Exception:
            pass
    print(f"[DB] Connected via TCP: {db_config.get('host')}:3306")
    return ("pymysql", conn)


class PooledConnection:
    """
    Wrapper that gives a unified cursor interface regardless of
    whether the underlying connection is mysql-connector or pymysql.
    """
    def __init__(self, kind, conn, db_config):
        self._kind   = kind   # "connector" or "pymysql"
        self._conn   = conn
        self._config = db_config
        self._last_used = time.monotonic() #Track Last Used Time

    def ping(self, reconnect=True):
        # Only actually ping if idle for more than 30 seconds
        # Avoids paying 150ms ping cost on every pool checkout
        idle_seconds = time.monotonic() - self._last_used
        if idle_seconds < 60:
            return   # assume connection is fine if recently used
        try:
            if self._kind == "connector":
                self._conn.ping(reconnect=reconnect)
            else:
                self._conn.ping(reconnect=reconnect)
            self._last_used = time.monotonic()
        except Exception:
            if reconnect:
                self._reconnect()
            else:
                raise
    def cursor(self):
        self._last_used = time.monotonic() # update on every use
        if self._kind == "connector":
            return _DictCursorWrapper(self._conn.cursor(dictionary=True))
        else:
            return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


    def _reconnect(self):
        try:
            self._conn.close()
        except Exception:
            pass
        kind, conn = _make_single_conn(self._config)
        self._kind = kind
        self._conn = conn

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # Expose underlying connection for legacy code
    @property
    def host(self):
        return getattr(self._conn, 'host', None) or getattr(self._conn, 'server_host', None)

    @property
    def port(self):
        return getattr(self._conn, 'port', None) or getattr(self._conn, 'server_port', None)


class _DictCursorWrapper:
    """
    Wraps mysql-connector cursor to match pymysql DictCursor interface.
    Ensures fetchall/fetchone always return list of dicts.
    """
    def __init__(self, cursor):
        self._cur = cursor

    def execute(self, sql, args=None):
        return self._cur.execute(sql, args or ())

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall() or []

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def description(self):
        return self._cur.description        # ← add this

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._cur.close()
        except Exception:
            pass
class SimpleConnectionPool:
    """Thread-safe database connection pool."""

    def __init__(self, db_config, max_connections=10, min_connections=3):
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
            kind, raw_conn = _make_single_conn(self.db_config)
            conn = PooledConnection(kind, raw_conn, self.db_config)
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
        except Empty:
            return self._create_new_connection()

        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            with self._lock:
                self._active -= 1
            return self._create_new_connection()

    def _create_new_connection(self):
        with self._lock:
            if self._active < self.max_connections:
                try:
                    kind, raw_conn = _make_single_conn(self.db_config)
                    conn = PooledConnection(kind, raw_conn, self.db_config)
                    self._active += 1
                    return conn
                except Exception as e:
                    raise Exception(f"Could not open new DB connection: {e}")
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


# ── Pool instances ────────────────────────────────────────────────────────────
hr_pool    = SimpleConnectionPool(DB_HR,    max_connections=15, min_connections=10)
slots_pool = SimpleConnectionPool(DB_SLOTS, max_connections=20, min_connections=12)


# ── Keepalive ─────────────────────────────────────────────────────────────────
def _keepalive_worker(pool, interval=55):
    while True:
        time.sleep(interval)
        live = []
        while not pool._pool.empty():
            try:
                conn = pool._pool.get_nowait()
                try:
                    conn.ping(reconnect=True)
                    live.append(conn)
                except Exception:
                    with pool._lock:
                        pool._active -= 1
            except Exception:
                break
        for conn in live:
            try:
                pool._pool.put_nowait(conn)
            except Exception:
                pass

threading.Thread(target=_keepalive_worker, args=(hr_pool, 55), daemon=True).start()
threading.Thread(target=_keepalive_worker, args=(slots_pool, 55), daemon=True).start()


# ── Query helpers ─────────────────────────────────────────────────────────────
def execute_query(pool, sql, params=None, retry=1):
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


def _drain_and_refill_pool_local(pool):
    # Best-effort: try common close methods, otherwise try to empty internal queue
    try:
        if hasattr(pool, "close_all"):
            pool.close_all()
            return
        if hasattr(pool, "_pool") and hasattr(pool._pool, "queue"):
            q = pool._pool
            try:
                while not q.empty():
                    conn = q.get_nowait()
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass
# ── Convenience wrappers ──────────────────────────────────────────────────────
def hr_query(sql, params=None):      return execute_query(hr_pool,    sql, params)
def slots_query(sql, params=None):   return execute_query(slots_pool, sql, params)
def hr_execute(sql, params=None):    return execute_write(hr_pool,    sql, params)
def slots_execute(sql, params=None): return execute_write(slots_pool, sql, params)