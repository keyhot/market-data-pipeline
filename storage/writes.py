# storage/writes.py
"""Mandatory Postgres write path (Sprint 7 cutover).

Postgres is the source of truth: POSTGRES_WRITE_ENABLED defaults on, and a
write failure raises StorageWriteError instead of being swallowed. Set the
flag to 0/false/no only for offline development without a database.
"""

import logging
import os
import threading

import pandas as pd

from config.exceptions import StorageWriteError
from storage import postgres_store
from storage.db import DATABASE_URL_ENV, ping

POSTGRES_WRITE_ENABLED_ENV = "POSTGRES_WRITE_ENABLED"

logger = logging.getLogger(__name__)

_counts = {
    "price_bars": 0,
    "corporate_events": 0,
    "news_items": 0,
    "signals": 0,
    "errors": 0,
}
_counts_lock = threading.Lock()


def postgres_write_enabled() -> bool:
    raw = os.environ.get(POSTGRES_WRITE_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no"}


def postgres_status() -> dict:
    enabled = postgres_write_enabled()
    return {"enabled": enabled, "connected": ping() if enabled else None}


def postgres_readable(status: dict) -> bool:
    """Whether Postgres answers right now, independent of whether this
    process may *write* to it.

    `status["connected"]` (from `postgres_status()`) already reflects
    reachability when `POSTGRES_WRITE_ENABLED` is on — that call already paid
    for a `ping()`. When writes are off, `postgres_status()` skips its own
    ping and reports `connected: None` — unprobed, not unreachable — so a
    read-only deployment (writes off, database perfectly readable) would
    otherwise see this as "not connected" forever. Only probe here when the
    write path didn't already answer the question: reusing its result when
    available, rather than always pinging, avoids stacking a second bounded
    connection-acquire wait onto the same request when writes are on and
    Postgres is down (KI-024's 5s `/health` content-timeout budget — see
    `tests/unit/test_db_deploy_guard.py::TestRoleProbeLatency`).

    When `connected` is `None`, that still splits into two different
    situations, and only one of them is worth a network round trip:
    "database configured, writes just turned off" (a read-only deployment —
    exactly MINOR-3's case, must still be probed) versus "no database at
    all" (this module's own docstring: `POSTGRES_WRITE_ENABLED=0` is "for
    offline development without a database" — there is nothing to probe).
    `DATABASE_URL` being unset is the honest signal for the second case: every
    deployed config sets it explicitly (`docker-compose.yml`,
    `.env.example`); it is unset only in ad-hoc local/test runs with no
    database at all. Skipping the probe there restores the fast path that
    genuine offline development had before MINOR-3 — a real, measured
    regression that fix introduced (2ms-16ms → ~2s per `/health` call,
    every call) precisely because it could not tell the two situations
    apart."""
    connected = status.get("connected")
    if connected is not None:
        return bool(connected)
    if os.environ.get(DATABASE_URL_ENV) is None:
        return False
    return ping()


def write_metrics() -> dict:
    with _counts_lock:
        return dict(_counts)


def write_price_bars(
    symbol: str, bars: pd.DataFrame, interval: str | None = None
) -> None:
    _write(
        "price_bars",
        lambda: postgres_store.upsert_price_bars(
            symbol, interval or postgres_store.BAR_INTERVAL, bars
        ),
    )


def write_events(symbol: str, event_type: str, events: pd.DataFrame) -> None:
    _write(
        "corporate_events",
        lambda: postgres_store.upsert_events_snapshot(
            symbol, str(event_type), events
        ),
    )


def write_news(symbol: str, news: pd.DataFrame) -> None:
    _write("news_items", lambda: postgres_store.upsert_news(symbol, news))


def write_signals(signals: list[dict]) -> None:
    _write("signals", lambda: postgres_store.upsert_signals(signals))


def _write(table: str, write) -> None:
    if not postgres_write_enabled():
        return
    try:
        written = write()
    except Exception as e:
        with _counts_lock:
            _counts["errors"] += 1
        logger.error(
            "Postgres write failed", extra={"table": table, "error": str(e)}
        )
        raise StorageWriteError(f"Failed to persist {table}: {e}") from e
    with _counts_lock:
        _counts[table] += written
