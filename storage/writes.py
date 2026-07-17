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
from storage.db import ping

POSTGRES_WRITE_ENABLED_ENV = "POSTGRES_WRITE_ENABLED"

logger = logging.getLogger(__name__)

_counts = {"price_bars": 0, "corporate_events": 0, "news_items": 0, "errors": 0}
_counts_lock = threading.Lock()


def postgres_write_enabled() -> bool:
    raw = os.environ.get(POSTGRES_WRITE_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no"}


def postgres_status() -> dict:
    enabled = postgres_write_enabled()
    return {"enabled": enabled, "connected": ping() if enabled else None}


def write_metrics() -> dict:
    with _counts_lock:
        return dict(_counts)


def write_price_bars(symbol: str, bars: pd.DataFrame) -> None:
    _write(
        "price_bars",
        lambda: postgres_store.upsert_price_bars(
            symbol, postgres_store.BAR_INTERVAL, bars
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
