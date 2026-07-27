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
            # Float, not int: crypto base-asset volume is fractional and int()
            # truncated it (sub-1-unit minutes became 0). Column is NUMERIC.
            _as_float(bar.get("Volume")),
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
            # NUMERIC comes back as Decimal; coerce to float for JSON like OHLC.
            "volume": _as_float(volume),
        }
        for ts, open_, high, low, close, volume in reversed(rows)
    ]


def latest_bar_timestamp(symbol: str, interval: str = BAR_INTERVAL) -> datetime | None:
    """Newest stored bar for a symbol/interval; None when nothing is stored.
    Used by the websocket ingester to bound its reconnect gap backfill."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT max(bar_timestamp) FROM price_bars"
            " WHERE symbol = %s AND interval = %s",
            (symbol.upper(), interval),
        ).fetchone()
    return row[0] if row else None


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


_SIGNALS_SQL = """
    INSERT INTO signals
        (symbol, interval, signal_timestamp, model_version, horizon_bars,
         direction, probability)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (symbol, interval, signal_timestamp, model_version) DO UPDATE SET
        horizon_bars = EXCLUDED.horizon_bars,
        direction = EXCLUDED.direction,
        probability = EXCLUDED.probability
"""


def upsert_signals(signals: list[dict]) -> int:
    """Idempotent signal upsert; re-predicting the same bar never duplicates.
    Resolution columns (resolved_at, outcome) are owned by the Sprint 10
    resolver and deliberately untouched here."""
    rows = [
        (
            s["symbol"].upper(),
            s["interval"],
            _as_datetime(s["signal_timestamp"]),
            s["model_version"],
            int(s["horizon_bars"]),
            s["direction"],
            float(s["probability"]),
        )
        for s in signals
    ]
    return _executemany(_SIGNALS_SQL, rows)


def get_signals(
    symbol: str, interval: str = BAR_INTERVAL, limit: int = 50
) -> list[dict]:
    """Latest signals for a symbol, newest first, outcomes included."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT signal_timestamp, model_version, horizon_bars, direction,"
            " probability, resolved_at, outcome FROM signals"
            " WHERE symbol = %s AND interval = %s"
            " ORDER BY signal_timestamp DESC LIMIT %s",
            (symbol.upper(), interval, limit),
        ).fetchall()
    return [
        {
            "signal_timestamp": ts.isoformat(),
            "model_version": version,
            "horizon_bars": horizon,
            "direction": direction,
            "probability": probability,
            "resolved_at": resolved.isoformat() if resolved else None,
            "outcome": outcome,
        }
        for ts, version, horizon, direction, probability, resolved, outcome in rows
    ]


def get_signal_accuracy(
    symbol: str, interval: str = "1m", window: int = 50
) -> dict:
    """Rolling honesty: hit rate and streak over the last `window` resolved
    signals. Zero resolved signals is a defined empty result, not an error."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT outcome, probability FROM signals"
            " WHERE symbol = %s AND interval = %s AND resolved_at IS NOT NULL"
            " ORDER BY signal_timestamp DESC LIMIT %s",
            (symbol.upper(), interval, window),
        ).fetchall()
    outcomes = [outcome for outcome, _ in rows]
    wins = outcomes.count("win")
    streak = 0
    for outcome in outcomes:  # newest first; count the leading run
        if outcome != outcomes[0]:
            break
        streak += 1
    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "window": window,
        "resolved": len(outcomes),
        "wins": wins,
        "losses": len(outcomes) - wins,
        "hit_rate": wins / len(outcomes) if outcomes else None,
        "current_streak": streak,
        "streak_outcome": outcomes[0] if outcomes else None,
    }


def get_all_signal_accuracy(symbols: list[str], interval: str = "1m") -> list[dict]:
    """Accuracy summaries for many symbols (dashboard/overlay shape)."""
    return [get_signal_accuracy(symbol, interval) for symbol in symbols]


def get_unresolved_signals(limit: int = 500) -> list[dict]:
    """Pending signals, oldest first — the resolver's work queue."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT symbol, interval, signal_timestamp, model_version,"
            " horizon_bars, direction, probability FROM signals"
            " WHERE resolved_at IS NULL ORDER BY signal_timestamp LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "symbol": symbol,
            "interval": interval,
            "signal_timestamp": ts,
            "model_version": version,
            "horizon_bars": horizon,
            "direction": direction,
            "probability": probability,
        }
        for symbol, interval, ts, version, horizon, direction, probability in rows
    ]


def resolve_signal(
    symbol: str,
    interval: str,
    signal_timestamp: datetime,
    model_version: str,
    outcome: str,
) -> int:
    """The single sanctioned signals UPDATE (docs/world-memory.md): fill
    resolved_at/outcome exactly once — the resolved_at guard makes re-runs
    no-ops, so restarts can't double-resolve."""
    with get_pool().connection() as conn:
        cursor = conn.execute(
            "UPDATE signals SET resolved_at = now(), outcome = %s"
            " WHERE symbol = %s AND interval = %s AND signal_timestamp = %s"
            " AND model_version = %s AND resolved_at IS NULL",
            (outcome, symbol.upper(), interval, signal_timestamp, model_version),
        )
        return cursor.rowcount


def get_bar_close(
    symbol: str, interval: str, bar_timestamp: datetime
) -> float | None:
    """Close of one exact stored bar; None when that bar is missing."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT close FROM price_bars WHERE symbol = %s AND interval = %s"
            " AND bar_timestamp = %s",
            (symbol.upper(), interval, bar_timestamp),
        ).fetchone()
    return _as_float(row[0]) if row else None


_WORLD_EVENTS_SQL = """
    INSERT INTO world_events (occurred_at, event_type, symbol, severity, payload)
    VALUES (%s, %s, %s, %s, %s)
"""


def append_world_events(events: list[dict]) -> int:
    """Append-only by design: the world's memory has no update or delete
    path anywhere in application code (docs/world-memory.md)."""
    import json as _json

    rows = [
        (
            _as_datetime(e["occurred_at"]),
            e["event_type"],
            # `or ""` (not the .get default): symbol-less events set the key to
            # None explicitly (scene_switched, stream_*), so .get returns None and
            # None.upper() would crash — one such row fails the whole batch.
            (e.get("symbol") or "").upper() or None,
            float(e["severity"]),
            _json.dumps(e.get("payload", {})),
        )
        for e in events
    ]
    return _executemany(_WORLD_EVENTS_SQL, rows)


_WORLD_EVENTS_BACKFILL_SQL = """
    INSERT INTO world_events (occurred_at, event_type, symbol, severity, payload)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
"""


def append_world_events_backfill(events: list[dict]) -> int:
    """Backfill-only writer: ON CONFLICT DO NOTHING against the natural-key
    unique index, so replaying history is re-runnable. The live append path
    (append_world_events) is deliberately left untouched — its 30-minute
    cooldown is the live dedupe, and a surprise conflict there should surface
    rather than be swallowed."""
    import json as _json

    rows = [
        (
            _as_datetime(e["occurred_at"]),
            e["event_type"],
            (e.get("symbol") or "").upper() or None,  # symbol may be explicit None
            float(e["severity"]),
            _json.dumps(e.get("payload", {})),
        )
        for e in events
    ]
    if not rows:
        return 0
    # NOT _executemany(): that helper returns len(rows), which would report a
    # re-run as having written everything and make the idempotence check
    # meaningless. Here the actual affected count is the whole point.
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(_WORLD_EVENTS_BACKFILL_SQL, rows)
            inserted = cur.rowcount
    return max(inserted, 0)


def get_world_events(
    limit: int = 50,
    event_type: str | None = None,
    symbol: str | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """Latest world events, newest first."""
    clauses, params = [], []
    if event_type is not None:
        clauses.append("event_type = %s")
        params.append(event_type)
    if symbol is not None:
        clauses.append("symbol = %s")
        params.append(symbol.upper())
    if since is not None:
        clauses.append("occurred_at > %s")
        params.append(since)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, occurred_at, event_type, symbol, severity, payload"
            f" FROM world_events{where} ORDER BY occurred_at DESC, id DESC LIMIT %s",
            (*params, limit),
        ).fetchall()
    return [
        {
            "id": event_id,
            "occurred_at": occurred.isoformat(),
            "event_type": etype,
            "symbol": sym,
            "severity": severity,
            "payload": payload,
        }
        for event_id, occurred, etype, sym, severity, payload in rows
    ]


def latest_world_event_time(event_type: str, symbol: str | None) -> datetime | None:
    """Newest occurrence of an event type (per symbol) — the salience
    cooldown guard derives its state from here, not from process memory."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT max(occurred_at) FROM world_events"
            " WHERE event_type = %s AND symbol IS NOT DISTINCT FROM %s",
            (event_type, symbol.upper() if symbol else None),
        ).fetchone()
    return row[0] if row else None


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
