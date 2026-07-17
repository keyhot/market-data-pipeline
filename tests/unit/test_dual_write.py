from unittest.mock import patch

import pandas as pd
import pytest

from storage import dual_write
from storage.dual_write import (
    POSTGRES_WRITE_ENABLED_ENV,
    mirror_events,
    mirror_news,
    mirror_price_bars,
    postgres_status,
    write_metrics,
)


@pytest.fixture(autouse=True)
def reset_counts():
    with dual_write._counts_lock:
        saved = dict(dual_write._counts)
        dual_write._counts.update(
            {"price_bars": 0, "corporate_events": 0, "news_items": 0, "errors": 0}
        )
    yield
    with dual_write._counts_lock:
        dual_write._counts.update(saved)


def _bars():
    return pd.DataFrame(
        {"Open": [1.0], "Close": [2.0]},
        index=pd.to_datetime(["2026-01-05"], utc=True),
    )


def test_mirror_is_noop_when_flag_disabled(monkeypatch):
    monkeypatch.delenv(POSTGRES_WRITE_ENABLED_ENV, raising=False)

    with patch("storage.dual_write.postgres_store") as store:
        mirror_price_bars("AAPL", _bars())

    store.upsert_price_bars.assert_not_called()
    assert write_metrics()["price_bars"] == 0


def test_mirror_counts_rows_when_enabled(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "true")

    with patch("storage.dual_write.postgres_store") as store:
        store.upsert_price_bars.return_value = 5
        store.upsert_events_snapshot.return_value = 2
        store.upsert_news.return_value = 3
        mirror_price_bars("AAPL", _bars())
        mirror_events("AAPL", "dividends", _bars())
        mirror_news("AAPL", _bars())

    counts = write_metrics()
    assert counts["price_bars"] == 5
    assert counts["corporate_events"] == 2
    assert counts["news_items"] == 3
    assert counts["errors"] == 0


def test_mirror_swallows_postgres_failure(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "true")

    with patch("storage.dual_write.postgres_store") as store:
        store.upsert_price_bars.side_effect = RuntimeError("db down")
        mirror_price_bars("AAPL", _bars())

    counts = write_metrics()
    assert counts["errors"] == 1
    assert counts["price_bars"] == 0


def test_postgres_status_disabled_skips_ping(monkeypatch):
    monkeypatch.delenv(POSTGRES_WRITE_ENABLED_ENV, raising=False)

    with patch("storage.dual_write.ping") as ping:
        status = postgres_status()

    ping.assert_not_called()
    assert status == {"enabled": False, "connected": None}


def test_postgres_status_enabled_reports_ping(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "1")

    with patch("storage.dual_write.ping", return_value=True):
        assert postgres_status() == {"enabled": True, "connected": True}
