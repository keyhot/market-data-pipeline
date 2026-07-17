from datetime import date, datetime

import pandas as pd

from storage.db import get_pool

EVENT_TYPES = ("dividends", "splits")

# Every current fetch produces daily bars; the API's TimeRange is the fetch
# range, not bar granularity (docs/postgres-schema-spike.md).
BAR_INTERVAL = "1d"

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


def upsert_events_snapshot(symbol: str, event_type: str, events: pd.DataFrame) -> int:
    """Upsert any fetcher-shaped events snapshot, decomposing 'actions'
    (Dividends + Stock Splits side by side) into the two stored types."""
    if event_type != "actions":
        return upsert_corporate_events(symbol, event_type, events)

    written = 0
    for column, single_type in (("Dividends", "dividends"), ("Stock Splits", "splits")):
        if column in events.columns:
            written += upsert_corporate_events(
                symbol, single_type, events[events[column] != 0][[column]]
            )
    return written


def get_price_bars(
    symbol: str, interval: str = BAR_INTERVAL, limit: int = 100
) -> list[dict]:
    """Latest `limit` bars for a symbol, oldest first (chart-friendly)."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT bar_timestamp, open, high, low, close, volume FROM price_bars"
            " WHERE symbol = %s AND interval = %s"
            " ORDER BY bar_timestamp DESC LIMIT %s",
            (symbol.upper(), interval, limit),
        ).fetchall()
    return [
        {
            "timestamp": ts.isoformat(),
            "open": _as_float(open_),
            "high": _as_float(high),
            "low": _as_float(low),
            "close": _as_float(close),
            "volume": volume,
        }
        for ts, open_, high, low, close, volume in reversed(rows)
    ]


def record_ingestion_run(
    job_id: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    rows_written: int | None = None,
    error: str | None = None,
) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO ingestion_runs"
            " (job_id, started_at, finished_at, status, rows_written, error)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (job_id, started_at, finished_at, status, rows_written, error),
        )


def latest_success_times() -> dict[str, str]:
    """job_id -> ISO timestamp of its newest successful ingestion run."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT job_id, max(started_at) FROM ingestion_runs"
            " WHERE status = 'success' GROUP BY job_id"
        ).fetchall()
    return {job_id: ts.isoformat() for job_id, ts in rows}


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


def get_corporate_events(
    symbol: str, event_type: str | None = None, limit: int = 100
) -> list[dict]:
    """Latest `limit` events for a symbol, oldest first. None = all types."""
    sql = (
        "SELECT event_date, event_type, value FROM corporate_events"
        " WHERE symbol = %s"
    )
    params: list = [symbol.upper()]
    if event_type is not None:
        sql += " AND event_type = %s"
        params.append(event_type)
    sql += " ORDER BY event_date DESC LIMIT %s"
    params.append(limit)

    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"date": event_date.isoformat(), "event_type": kind, "value": _as_float(value)}
        for event_date, kind, value in reversed(rows)
    ]


def get_latest_closes(symbols: list[str]) -> list[dict]:
    """Newest daily close per symbol; symbols without bars are absent."""
    if not symbols:
        return []
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (symbol) symbol, bar_timestamp, close"
            " FROM price_bars WHERE symbol = ANY(%s) AND interval = %s"
            " ORDER BY symbol, bar_timestamp DESC",
            ([s.upper() for s in symbols], BAR_INTERVAL),
        ).fetchall()
    return [
        {"symbol": symbol, "timestamp": ts.isoformat(), "close": _as_float(close)}
        for symbol, ts, close in rows
    ]


def get_news_items(symbol: str, limit: int = 20) -> list[dict]:
    """Latest `limit` news items for a symbol, newest first."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, title, publisher, url, published_at, summary"
            " FROM news_items WHERE symbol = %s"
            " ORDER BY published_at DESC NULLS LAST LIMIT %s",
            (symbol.upper(), limit),
        ).fetchall()
    return [
        {
            "id": id_,
            "title": title,
            "publisher": publisher,
            "url": url,
            "published_at": published_at.isoformat() if published_at else None,
            "summary": summary,
        }
        for id_, title, publisher, url, published_at, summary in rows
    ]
