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
# The same bound applies to the probe's query (KI-069): connect_timeout ends
# at the handshake, and a database that accepts connections but answers slowly
# otherwise held this SELECT — and `_pool_lock` — for as long as it liked.
_ROLE_PROBE_TIMEOUT_SECONDS = 2.0
_ROLE_PROBE_OPTIONS = (
    f"-c statement_timeout={int(_ROLE_PROBE_TIMEOUT_SECONDS * 1000)}"
)

# KI-080: the bounds libpq enforces on the CLIENT, for a network path that
# drops silently mid-query — every other bound here is a server-side cancel
# or ends at the handshake. Keepalives find a dead peer on an idle socket
# (waiting for a reply), `tcp_user_timeout` one with unacknowledged data;
# on Linux the latter also ends keepalive probing, so it is set to exactly
# the keepalive detection time (10 + 5 * 3 = 25s). What these cannot see is
# a stall behind something that still ACKs locally — a userspace proxy, the
# local end of an SSH tunnel (measured: still hanging at 15s with 1s
# keepalives). That half is `/health`'s own deadline (api/main.py) and, for
# the tunnel, ssh's ServerAliveInterval.
_KEEPALIVE_IDLE_SECONDS = 10
_KEEPALIVE_INTERVAL_SECONDS = 5
_KEEPALIVE_COUNT = 3
_SOCKET_BOUNDS: dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": _KEEPALIVE_IDLE_SECONDS,
    "keepalives_interval": _KEEPALIVE_INTERVAL_SECONDS,
    "keepalives_count": _KEEPALIVE_COUNT,
    "tcp_user_timeout": (
        _KEEPALIVE_IDLE_SECONDS + _KEEPALIVE_INTERVAL_SECONDS * _KEEPALIVE_COUNT
    )
    * 1000,
}
# The pool's own connects run on its background workers, where libpq's
# default is to wait forever. Callers are bounded separately by the pool's
# acquire timeout, so this only has to stop a worker hanging.
_POOL_CONNECT_TIMEOUT_SECONDS = 5

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
    read-only surprise, it only acts on a positive contradiction. A probe that
    times out on a slow database is the same case and reads the same way.
    """
    try:
        with psycopg.connect(
            url,
            connect_timeout=_ROLE_PROBE_TIMEOUT_SECONDS,
            options=_ROLE_PROBE_OPTIONS,
            **_SOCKET_BOUNDS,
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
                kwargs: dict[str, object] = {
                    "connect_timeout": _POOL_CONNECT_TIMEOUT_SECONDS,
                    **_SOCKET_BOUNDS,
                }
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
            # The acquire timeout is spent once a warm connection is handed
            # over; without this a server too loaded to answer held
            # `SELECT 1` as long as it liked (KI-080). Transaction-local, so
            # it leaves with the rollback that returns the connection.
            # At least 1ms: `statement_timeout = 0` means no limit at all.
            bound_ms = max(1, int(timeout_seconds * 1000))
            conn.execute(f"SET LOCAL statement_timeout = {bound_ms}")
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
