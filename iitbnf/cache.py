"""
cache.py — In-memory cache with TTL support and @cached decorator.
"""
import time
import hashlib
import threading
from functools import wraps


class SimpleCache:
    """Thread-safe in-memory cache. No Redis required."""

    def __init__(self):
        self._cache = {}
        self._lock  = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._cache:
                value, expiry = self._cache[key]
                if expiry is None or expiry > time.time():
                    return value
                del self._cache[key]
        return None

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


# Singleton instance
cache = SimpleCache()


def cached(ttl_seconds=300):
    """Decorator to cache function results by args/kwargs."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            key_parts = [f.__name__] + [str(a) for a in args]
            key_parts += [f"{k}={v}" for k, v in sorted(kwargs.items())]
            cache_key = hashlib.md5(":".join(key_parts).encode()).hexdigest()
            cached_val = cache.get(cache_key)
            if cached_val is not None:
                return cached_val
            result = f(*args, **kwargs)
            cache.set(cache_key, result, ttl_seconds)
            return result
        return decorated
    return decorator
