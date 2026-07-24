"""Integration tests against the docker-compose Postgres.

Auto-skipped when the database is unreachable so the suite stays green
without Docker (and in CI until the service-container ticket lands).
"""

import pandas as pd
import pytest

from storage import postgres_store
from storage.db import get_pool, ping

pytestmark = pytest.mark.skipif(not ping(), reason="Postgres unavailable")

BARS_SYMBOL = "ZZTEST"
BACKFILL_SYMBOL = "ZZBF"
# TESTUSDT: used only by test_live_append_path_under_the_natural_key_index
# below; included here so the autouse cleanup fixture clears it too (a
# leftover row would make that test non-rerunnable — see world_events cleanup
# note there).
_TEST_SYMBOLS = [BARS_SYMBOL, BACKFILL_SYMBOL, "TESTUSDT"]


@pytest.fixture(autouse=True)
def clean_test_rows():
    _delete_test_rows()
    yield
    _delete_test_rows()


TEST_JOB_ID = "test:ZZTEST:1d"


def _delete_test_rows():
    with get_pool().connection() as conn:
        for table in ("price_bars", "corporate_events", "news_items", "signals"):
            conn.execute(
                f"DELETE FROM {table} WHERE symbol = ANY(%s)", (_TEST_SYMBOLS,)
            )
        conn.execute("DELETE FROM ingestion_runs WHERE job_id = %s", (TEST_JOB_ID,))
        conn.execute(
            "DELETE FROM world_events WHERE symbol = ANY(%s)", (_TEST_SYMBOLS,)
        )


def _count(table: str, symbol: str) -> int:
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {table} WHERE symbol = %s", (symbol,)
        ).fetchone()
    return row[0]


def _bars(close: float = 100.0) -> pd.DataFrame:
    index = pd.to_datetime(["2026-01-05", "2026-01-06"], utc=True)
    return pd.DataFrame(
        {
            "Open": [99.0, 100.5],
            "High": [101.0, 102.0],
            "Low": [98.0, 99.5],
            "Close": [close, close + 1],
            "Volume": [1000, 2000],
        },
        index=index,
    )


def test_price_bars_upsert_is_idempotent():
    assert postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars()) == 2
    assert postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars()) == 2

    assert _count("price_bars", BARS_SYMBOL) == 2


def test_price_bars_revised_fetch_wins():
    postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars(close=100.0))
    postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars(close=200.0))

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT close FROM price_bars"
            " WHERE symbol = %s ORDER BY bar_timestamp LIMIT 1",
            (BARS_SYMBOL,),
        ).fetchone()
    assert float(row[0]) == 200.0
    assert _count("price_bars", BARS_SYMBOL) == 2


def test_corporate_events_upsert_is_idempotent():
    events = pd.DataFrame(
        {"dividends": [0.25, 0.26]},
        index=pd.to_datetime(["2026-02-10", "2026-05-10"], utc=True),
    )

    postgres_store.upsert_corporate_events(BARS_SYMBOL, "dividends", events)
    postgres_store.upsert_corporate_events(BARS_SYMBOL, "dividends", events)

    assert _count("corporate_events", BARS_SYMBOL) == 2


def test_corporate_events_rejects_unknown_type():
    with pytest.raises(ValueError):
        postgres_store.upsert_corporate_events(
            BARS_SYMBOL, "actions", pd.DataFrame()
        )


def test_news_upsert_is_idempotent_and_skips_incomplete_rows():
    news = pd.DataFrame(
        {
            "id": ["story-1", None],
            "title": ["ZZTEST soars", "no id, dropped"],
            "publisher": ["Wire", "Wire"],
            "url": ["https://example.com/1", "https://example.com/2"],
            "published_at": pd.to_datetime(["2026-03-01", "2026-03-02"], utc=True),
            "summary": ["s1", "s2"],
        }
    )

    assert postgres_store.upsert_news(BARS_SYMBOL, news) == 1
    assert postgres_store.upsert_news(BARS_SYMBOL, news) == 1

    assert _count("news_items", BARS_SYMBOL) == 1


def test_get_price_bars_reads_back_oldest_first():
    postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars())

    bars = postgres_store.get_price_bars(BARS_SYMBOL, limit=10)

    assert [bar["close"] for bar in bars] == [100.0, 101.0]
    assert bars[0]["timestamp"] < bars[1]["timestamp"]
    assert bars[0]["volume"] == 1000


def test_get_price_bars_respects_limit_keeping_latest():
    postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars())

    bars = postgres_store.get_price_bars(BARS_SYMBOL, limit=1)

    assert len(bars) == 1
    assert bars[0]["close"] == 101.0


def test_record_ingestion_run_round_trips():
    from datetime import UTC, datetime

    started = datetime.now(UTC)
    postgres_store.record_ingestion_run(
        TEST_JOB_ID, started, datetime.now(UTC), "success", rows_written=7
    )

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, rows_written, error FROM ingestion_runs"
            " WHERE job_id = %s",
            (TEST_JOB_ID,),
        ).fetchone()
    assert row == ("success", 7, None)


def test_latest_success_times_returns_newest_success_per_job():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    postgres_store.record_ingestion_run(
        TEST_JOB_ID, now - timedelta(hours=2), now - timedelta(hours=2), "success"
    )
    postgres_store.record_ingestion_run(TEST_JOB_ID, now, now, "success")
    postgres_store.record_ingestion_run(TEST_JOB_ID, now, now, "error", error="x")

    times = postgres_store.latest_success_times()

    assert times[TEST_JOB_ID] == now.isoformat()


def test_backfill_is_rerunnable(tmp_path):
    from scripts.backfill_postgres import backfill

    for sub in ("tickers", "events", "news"):
        (tmp_path / sub).mkdir()

    _bars().rename_axis("Date").to_csv(
        tmp_path / "tickers" / f"{BACKFILL_SYMBOL}_5d_2026-01-07T00-00-00Z.csv"
    )
    pd.DataFrame(
        {"dividends": [0.5]}, index=pd.to_datetime(["2026-01-05"], utc=True)
    ).rename_axis("Date").to_csv(
        tmp_path / "events" / f"{BACKFILL_SYMBOL}_dividends_2026-01-07T00-00-00Z.csv"
    )
    pd.DataFrame(
        {
            "id": ["story-bf"],
            "title": ["backfilled"],
            "publisher": ["Wire"],
            "url": ["https://example.com/bf"],
            "published_at": pd.to_datetime(["2026-01-06"], utc=True),
            "summary": ["s"],
        }
    ).to_csv(tmp_path / "news" / f"{BACKFILL_SYMBOL}_news_2026-01-07T00-00-00Z.csv")
    # Malformed snapshot (RangeIndex, no dates) must be skipped, not ingested.
    pd.DataFrame({"Close": [100, 101]}).to_csv(
        tmp_path / "tickers" / f"{BACKFILL_SYMBOL}_1d_2026-01-08T00-00-00Z.csv"
    )

    backfill(tmp_path)
    backfill(tmp_path)

    assert _count("price_bars", BACKFILL_SYMBOL) == 2
    assert _count("corporate_events", BACKFILL_SYMBOL) == 1
    assert _count("news_items", BACKFILL_SYMBOL) == 1


def test_get_corporate_events_filters_and_orders():
    events = pd.DataFrame(
        {"dividends": [0.25, 0.26]},
        index=pd.to_datetime(["2026-02-10", "2026-05-10"], utc=True),
    )
    postgres_store.upsert_corporate_events(BARS_SYMBOL, "dividends", events)

    stored = postgres_store.get_corporate_events(BARS_SYMBOL, event_type="dividends")

    assert [e["value"] for e in stored] == [0.25, 0.26]
    assert postgres_store.get_corporate_events(BARS_SYMBOL, event_type="splits") == []
    assert len(postgres_store.get_corporate_events(BARS_SYMBOL)) == 2


def test_get_latest_closes_returns_newest_bar_per_symbol():
    postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars())

    closes = postgres_store.get_latest_closes([BARS_SYMBOL, "ZZABSENT"])

    assert len(closes) == 1
    assert closes[0]["symbol"] == BARS_SYMBOL
    assert closes[0]["close"] == 101.0


def test_get_news_items_newest_first():
    news = pd.DataFrame(
        {
            "id": ["story-1", "story-2"],
            "title": ["old", "new"],
            "publisher": ["Wire", "Wire"],
            "url": ["https://example.com/1", "https://example.com/2"],
            "published_at": pd.to_datetime(["2026-03-01", "2026-03-02"], utc=True),
            "summary": ["s1", "s2"],
        }
    )
    postgres_store.upsert_news(BARS_SYMBOL, news)

    items = postgres_store.get_news_items(BARS_SYMBOL)

    assert [i["title"] for i in items] == ["new", "old"]


def test_signals_upsert_is_idempotent():
    from datetime import datetime, timezone

    ts = datetime(2026, 7, 19, tzinfo=timezone.utc)
    signal = {
        "symbol": BARS_SYMBOL,
        "interval": "1m",
        "signal_timestamp": ts,
        "model_version": "test-v0",
        "horizon_bars": 15,
        "direction": "up",
        "probability": 0.55,
    }
    postgres_store.upsert_signals([signal])
    postgres_store.upsert_signals([{**signal, "probability": 0.6}])

    rows = postgres_store.get_signals(BARS_SYMBOL, "1m")

    assert len(rows) == 1
    assert rows[0]["probability"] == 0.6
    assert rows[0]["outcome"] is None


def test_world_events_append_and_read_back():
    from datetime import datetime, timezone

    ts = datetime(2026, 7, 19, tzinfo=timezone.utc)
    postgres_store.append_world_events(
        [
            {
                "occurred_at": ts,
                "event_type": "volatility_spike",
                "symbol": BARS_SYMBOL,
                "severity": 3.2,
                "payload": {"zscore": 3.2},
            }
        ]
    )

    events = postgres_store.get_world_events(symbol=BARS_SYMBOL)

    assert len(events) == 1
    assert events[0]["event_type"] == "volatility_spike"
    assert events[0]["payload"] == {"zscore": 3.2}
    assert postgres_store.latest_world_event_time(
        "volatility_spike", BARS_SYMBOL
    ) == ts


def test_signal_resolution_round_trip():
    """Full accountability loop against real Postgres: signal → realized
    bar → resolver → outcome + signal_resolved world event."""
    from datetime import datetime, timedelta, timezone

    from world.resolver import resolve_pending

    signal_ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars())
    postgres_store.upsert_signals(
        [
            {
                "symbol": BARS_SYMBOL,
                "interval": "1d",
                "signal_timestamp": signal_ts,
                "model_version": "test-v0",
                "horizon_bars": 1,
                "direction": "up",
                "probability": 0.7,
            }
        ]
    )

    resolutions = resolve_pending(now=signal_ts + timedelta(days=2))

    ours = [r for r in resolutions if r["symbol"] == BARS_SYMBOL]
    assert len(ours) == 1
    assert ours[0]["outcome"] == "win"  # _bars closes rise 100 -> 101
    stored = postgres_store.get_signals(BARS_SYMBOL, "1d")
    assert stored[0]["outcome"] == "win"
    events = postgres_store.get_world_events(
        event_type="signal_resolved", symbol=BARS_SYMBOL
    )
    assert len(events) == 1

    # idempotence: a second run resolves nothing new for this symbol
    again = resolve_pending(now=signal_ts + timedelta(days=2))
    assert [r for r in again if r["symbol"] == BARS_SYMBOL] == []


def test_live_append_path_under_the_natural_key_index():
    """The live writer has no ON CONFLICT clause. Once uq_world_events_natural
    exists, a genuine re-fire of the same (type, symbol, occurred_at) raises
    instead of duplicating. Pinning the behaviour here means the world-memory
    writer can never start throwing 503s in production unnoticed."""
    from datetime import datetime, timezone

    import psycopg
    import pytest

    from storage.postgres_store import append_world_events

    event = {
        "occurred_at": datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc),
        "event_type": "big_move",
        "symbol": "TESTUSDT",
        "severity": 5.0,
        "payload": {"sigmas": 5.0},
    }
    append_world_events([event])
    with pytest.raises(psycopg.errors.UniqueViolation):
        append_world_events([event])
