# tests/unit/test_writes.py
from unittest.mock import patch

import pandas as pd
import pytest

from config.exceptions import StorageWriteError
from storage import writes
from storage.writes import (
    POSTGRES_WRITE_ENABLED_ENV,
    postgres_status,
    postgres_write_enabled,
    write_events,
    write_metrics,
    write_news,
    write_price_bars,
)


@pytest.fixture(autouse=True)
def reset_counts():
    with writes._counts_lock:
        saved = dict(writes._counts)
        writes._counts.update(
            {"price_bars": 0, "corporate_events": 0, "news_items": 0, "errors": 0}
        )
    yield
    with writes._counts_lock:
        writes._counts.update(saved)


def _bars():
    return pd.DataFrame(
        {"Open": [1.0], "Close": [2.0]},
        index=pd.to_datetime(["2026-01-05"], utc=True),
    )


def test_flag_defaults_on_when_unset(monkeypatch):
    monkeypatch.delenv(POSTGRES_WRITE_ENABLED_ENV, raising=False)
    assert postgres_write_enabled() is True


def test_flag_explicit_off(monkeypatch):
    for value in ("0", "false", "no"):
        monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, value)
        assert postgres_write_enabled() is False


def test_write_is_noop_when_flag_disabled(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "0")

    with patch("storage.writes.postgres_store") as store:
        write_price_bars("AAPL", _bars())

    store.upsert_price_bars.assert_not_called()
    assert write_metrics()["price_bars"] == 0


def test_write_counts_rows_when_enabled(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "true")

    with patch("storage.writes.postgres_store") as store:
        store.upsert_price_bars.return_value = 5
        store.upsert_events_snapshot.return_value = 2
        store.upsert_news.return_value = 3
        write_price_bars("AAPL", _bars())
        write_events("AAPL", "dividends", _bars())
        write_news("AAPL", _bars())

    counts = write_metrics()
    assert counts["price_bars"] == 5
    assert counts["corporate_events"] == 2
    assert counts["news_items"] == 3
    assert counts["errors"] == 0


def test_write_failure_raises_storage_write_error(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "true")

    with patch("storage.writes.postgres_store") as store:
        store.upsert_price_bars.side_effect = RuntimeError("db down")
        with pytest.raises(StorageWriteError):
            write_price_bars("AAPL", _bars())

    counts = write_metrics()
    assert counts["errors"] == 1
    assert counts["price_bars"] == 0


def test_postgres_status_disabled_skips_ping(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "0")

    with patch("storage.writes.ping") as ping:
        status = postgres_status()

    ping.assert_not_called()
    assert status == {"enabled": False, "connected": None}


def test_postgres_status_enabled_reports_ping(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "1")

    with patch("storage.writes.ping", return_value=True):
        assert postgres_status() == {"enabled": True, "connected": True}


def test_postgres_readable_reuses_a_known_connected_result():
    # Writes on, ping already ran and succeeded inside postgres_status() —
    # readable() must not probe again (a second bounded connect-acquire wait
    # on the same request would blow the /health latency budget, KI-024).
    with patch("storage.writes.ping") as ping:
        assert writes.postgres_readable({"enabled": True, "connected": True}) is True
    ping.assert_not_called()


def test_postgres_readable_reuses_a_known_down_result():
    with patch("storage.writes.ping") as ping:
        assert writes.postgres_readable({"enabled": True, "connected": False}) is False
    ping.assert_not_called()


def test_postgres_readable_probes_when_writes_are_disabled():
    # This is MINOR-3: writes off means postgres_status() never pinged, so
    # `connected` is None — unprobed, not unreachable. A read-only deployment
    # (writes off, database perfectly readable) must still get an answer.
    with patch("storage.writes.ping", return_value=True) as ping:
        assert writes.postgres_readable({"enabled": False, "connected": None}) is True
    ping.assert_called_once()


def test_postgres_readable_reports_unreachable_when_disabled_and_down():
    with patch("storage.writes.ping", return_value=False):
        assert writes.postgres_readable({"enabled": False, "connected": None}) is False
