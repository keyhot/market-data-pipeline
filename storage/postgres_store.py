from datetime import date, datetime

import pandas as pd

from storage.db import get_pool

EVENT_TYPES = ("dividends", "splits")

# DO UPDATE (not DO NOTHING): Yahoo revises recent bars — the latest fetch
# wins, and the row count still can't grow.
_PRICE_BARS_SQL = """
    INSERT INTO price_bars
        (symbol, bar_timestamp, interval, open, high, low, close, volume)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (symbol, bar_timestamp, interval) DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        fetched_at = now()
"""

_CORPORATE_EVENTS_SQL = """
    INSERT INTO corporate_events (symbol, event_date, event_type, value)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (symbol, event_date, event_type) DO UPDATE SET
        value = EXCLUDED.value,
        fetched_at = now()
"""

_NEWS_SQL = """
    INSERT INTO news_items
        (id, symbol, title, publisher, url, published_at, summary)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id, symbol) DO UPDATE SET
        title = EXCLUDED.title,
        publisher = EXCLUDED.publisher,
        url = EXCLUDED.url,
        published_at = EXCLUDED.published_at,
        summary = EXCLUDED.summary,
        fetched_at = now()
"""


def upsert_price_bars(symbol: str, interval: str, bars: pd.DataFrame) -> int:
    """Upsert fetcher-shaped history: datetime index, Open/High/Low/Close/Volume."""
    rows = [
        (
            symbol.upper(),
            _as_datetime(ts),
            interval,
            _as_float(bar.get("Open")),
            _as_float(bar.get("High")),
            _as_float(bar.get("Low")),
            _as_float(bar.get("Close")),
            _as_int(bar.get("Volume")),
        )
        for ts, bar in bars.iterrows()
    ]
    return _executemany(_PRICE_BARS_SQL, rows)


def upsert_corporate_events(
    symbol: str, event_type: str, events: pd.DataFrame
) -> int:
    """Upsert fetcher-shaped events: datetime index, single value column."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unsupported event type: {event_type}")

    values = events.iloc[:, 0]
    rows = [
        (symbol.upper(), _as_date(ts), event_type, float(value))
        for ts, value in values.items()
        if not pd.isna(value)
    ]
    return _executemany(_CORPORATE_EVENTS_SQL, rows)


def upsert_news(symbol: str, news: pd.DataFrame) -> int:
    """Upsert fetcher-shaped news (NEWS_COLUMNS from ingestion.news_fetcher)."""
    rows = []
    for item in news.to_dict("records"):
        if pd.isna(item.get("id")) or pd.isna(item.get("title")):
            continue
        rows.append(
            (
                item["id"],
                symbol.upper(),
                item["title"],
                _as_str(item.get("publisher")),
                _as_str(item.get("url")),
                _as_datetime(item.get("published_at")),
                _as_str(item.get("summary")),
            )
        )
    return _executemany(_NEWS_SQL, rows)


def _executemany(sql: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
    return len(rows)


def _as_datetime(value) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime()


def _as_date(value) -> date:
    return pd.Timestamp(value).date()


def _as_float(value) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _as_int(value) -> int | None:
    return None if value is None or pd.isna(value) else int(value)


def _as_str(value) -> str | None:
    return None if value is None or pd.isna(value) else str(value)
