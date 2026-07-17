import os

from psycopg_pool import ConnectionPool

DATABASE_URL_ENV = "DATABASE_URL"
# Matches the docker-compose defaults so local dev works with no configuration.
_DEFAULT_DATABASE_URL = (
    "postgresql://market_data:market_data@localhost:5432/market_data"
)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
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
