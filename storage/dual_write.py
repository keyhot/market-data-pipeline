"""Best-effort Postgres mirroring behind POSTGRES_WRITE_ENABLED.

During the dual-write phase CSV stays the source of truth: a Postgres
failure is logged and counted, never raised to the caller.
"""

import logging
import os
import threading

import pandas as pd

from storage import postgres_store
from storage.db import ping

POSTGRES_WRITE_ENABLED_ENV = "POSTGRES_WRITE_ENABLED"

logger = logging.getLogger(__name__)

_counts = {"price_bars": 0, "corporate_events": 0, "news_items": 0, "errors": 0}
_counts_lock = threading.Lock()


def postgres_write_enabled() -> bool:
    raw = os.environ.get(POSTGRES_WRITE_ENABLED_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes"}


def postgres_status() -> dict:
    enabled = postgres_write_enabled()
    return {"enabled": enabled, "connected": ping() if enabled else None}


def write_metrics() -> dict:
    with _counts_lock:
        return dict(_counts)


def mirror_price_bars(symbol: str, bars: pd.DataFrame) -> None:
    _mirror(
        "price_bars",
        lambda: postgres_store.upsert_price_bars(
            symbol, postgres_store.BAR_INTERVAL, bars
        ),
    )


def mirror_events(symbol: str, event_type: str, events: pd.DataFrame) -> None:
    _mirror(
        "corporate_events",
        lambda: postgres_store.upsert_events_snapshot(
            symbol, str(event_type), events
        ),
    )


def mirror_news(symbol: str, news: pd.DataFrame) -> None:
    _mirror("news_items", lambda: postgres_store.upsert_news(symbol, news))


def _mirror(table: str, write) -> None:
    if not postgres_write_enabled():
        return
    try:
        written = write()
    except Exception as e:
        with _counts_lock:
            _counts["errors"] += 1
        logger.warning(
            "Postgres mirror write failed", extra={"table": table, "error": str(e)}
        )
        return
    with _counts_lock:
        _counts[table] += written
