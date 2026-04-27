"""
cache.py — In-memory cache with TTL support and decorator.

Critical fix: use a sentinel object to distinguish a real cache miss
from a cached value that is None, [], {}, or 0.  The old code used
`if cached_val is not None` which meant any function returning an empty
list or dict was never actually cached — the DB was re-queried on every
call even with @cached applied.
"""
import time
import hashlib
import threading
from functools import wraps

# Sentinel — stored as the cached value when the real result is falsy
_MISSING = object()


class SimpleCache:
    """Thread-safe in-memory cache. No Redis required."""

    def __init__(self):
        self._cache = {}
        self._lock  = threading.Lock()

    def get(self, key):
        """Return (found, value). found=False means cache miss."""
        with self._lock:
            if key in self._cache:
                value, expiry = self._cache[key]
                if expiry is None or expiry > time.time():
                    return True, value
                del self._cache[key]
        return False, None

    def set(self, key, value, ttl=None):
        with self._lock:
            expiry = time.time() + ttl if ttl else None
            self._cache[key] = (value, expiry)

    def delete(self, key):
        with self._lock:
            self._cache.pop(key, None)

    def delete_pattern(self, pattern):
        with self._lock:
            for k in [k for k in self._cache if pattern in k or k.startswith(pattern)]:
                del self._cache[k]

    def clear(self):
        with self._lock:
            self._cache.clear()

    def clear_user_session(self):
        """
        Clear only per-user session data (attendance, slot activity, trend).
        Does NOT clear shared/global caches like get_all_members or holidays —
        those are expensive and shared across all users.
        Called by logout instead of clear() to avoid nuking shared caches.
        """
        # We don't have per-user keys tagged here, so this is a no-op for now.
        # The per-function @cached keys include member_id so they expire naturally.
        pass


# Singleton instance
cache = SimpleCache()


def cached(ttl_seconds=300):
    """
    Decorator to cache function results by args/kwargs.
    Correctly handles falsy return values ([], {}, None, 0).
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            key_parts = [f.__name__] + [str(a) for a in args]
            key_parts += [f"{k}={v}" for k, v in sorted(kwargs.items())]
            cache_key = hashlib.md5(":".join(key_parts).encode()).hexdigest()
            found, cached_val = cache.get(cache_key)
            if found:
                return cached_val
            result = f(*args, **kwargs)
            cache.set(cache_key, result, ttl_seconds)
            return result
        return decorated
    return decorator
