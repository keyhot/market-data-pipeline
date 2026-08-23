import logging
import os
import threading

import psycopg
from psycopg_pool import ConnectionPool

DATABASE_URL_ENV = "DATABASE_URL"
# Matches the docker-compose defaults so local dev works with no configuration.
_DEFAULT_DATABASE_URL = (
    "postgresql://market_data:market_data@localhost:5432/market_data"
)

# Which deployment this *process* believes it is. The database says the same
# thing about itself in `deployment_identity`; when the two disagree, the pool
# opens read-only. See read_only_required().
DEPLOY_ROLE_ENV = "DEPLOY_ROLE"
_DEFAULT_DEPLOY_ROLE = "dev"
_READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
# Deliberately short, and 2.0 is as short as it goes: libpq clamps
# connect_timeout to a 2-second minimum, so a smaller number here would be a
# comment that lies. The probe sits on the cold path `/health` answers with,
# and the stream watchdog gives that endpoint 5s before counting a content
# failure (KI-024) — a Postgres outage must not also manufacture false
# *content* outages. Measured against an unreachable host: 4.01s cold, all of
# it this probe plus ping's own 2s, so 1s of headroom. A Postgres that is
# actually there answers a loopback (or tunnelled) connect in milliseconds.
_ROLE_PROBE_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def deploy_role() -> str:
    """The role this process claims: 'dev' unless DEPLOY_ROLE says otherwise."""
    return (
        os.environ.get(DEPLOY_ROLE_ENV, _DEFAULT_DEPLOY_ROLE).strip().lower()
        or _DEFAULT_DEPLOY_ROLE
    )


def _database_role(url: str) -> str | None:
    """The role the database claims, or None if it doesn't claim one.

    A single short-lived connection, once per process, before the pool exists.
    Absent table (a volume older than the guard) and an unreachable database
    both read as None: the guard never turns a connectivity problem into a
    read-only surprise, it only acts on a positive contradiction.
    """
    try:
        with psycopg.connect(
            url, connect_timeout=_ROLE_PROBE_TIMEOUT_SECONDS
        ) as conn:
            row = conn.execute("SELECT role FROM deployment_identity").fetchone()
    except Exception as e:
        logger.warning(
            "Could not read deployment_identity; dev/prod guard inactive",
            extra={"error": f"{type(e).__name__}: {e}"},
        )
        return None
    if not row or not row[0]:
        return None
    return str(row[0]).strip().lower()


def read_only_required(expected_role: str, database_role: str | None) -> bool:
    """True when the process and the database disagree about which is which.

    An unmarked database (None) is not a contradiction — it predates the
    guard, so it is left writable rather than breaking existing installs.
    """
    if database_role is None:
        return False
    return database_role != expected_role


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
                expected = deploy_role()
                actual = _database_role(url)
                kwargs: dict[str, object] = {}
                if read_only_required(expected, actual):
                    # Enforced by Postgres, not by call sites: every write in
                    # every code path fails, including the append-only
                    # world_events writer that has no undo.
                    logger.error(
                        "Deployment role mismatch — opening Postgres read-only",
                        extra={
                            "process_role": expected,
                            "database_role": actual,
                        },
                    )
                    kwargs["options"] = _READ_ONLY_OPTIONS
                _pool = ConnectionPool(
                    url, kwargs=kwargs, min_size=1, max_size=4, open=True
                )
    return _pool


def ping(timeout_seconds: float = 2.0) -> bool:
    try:
        with get_pool().connection(timeout=timeout_seconds) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as e:
        # A health check that raises is useless, so the bool stays — but
        # `/health` reporting `connected: false` with the reason nowhere in
        # the log is how you end up bisecting a live service by hand.
        logger.warning(
            "Postgres ping failed", extra={"error": f"{type(e).__name__}: {e}"}
        )
        return False
