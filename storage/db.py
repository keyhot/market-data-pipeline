import os
import threading

from psycopg_pool import ConnectionPool

DATABASE_URL_ENV = "DATABASE_URL"
# Matches the docker-compose defaults so local dev works with no configuration.
_DEFAULT_DATABASE_URL = (
    "postgresql://market_data:market_data@localhost:5432/market_data"
)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    global _pool
    # Double-checked lock: without it, two threads racing the first cold access
    # (e.g. concurrent API requests before the pool is warmed) both build a pool
    # with live connections, and the losing one is orphaned — leaking its
    # connections and background threads.
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                url = os.environ.get(DATABASE_URL_ENV, _DEFAULT_DATABASE_URL)
                _pool = ConnectionPool(url, min_size=1, max_size=4, open=True)
    return _pool


def ping(timeout_seconds: float = 2.0) -> bool:
    try:
        with get_pool().connection(timeout=timeout_seconds) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
