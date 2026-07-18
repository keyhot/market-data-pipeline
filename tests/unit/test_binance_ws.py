import json
from datetime import datetime, timezone
from unittest.mock import patch

from ingestion.binance_ws import (
    BinanceKlineIngester,
    crypto_watchlist_symbols,
    ws_ingest_enabled,
)
from scheduler.watchlist import TickerJobSpec, Watchlist

KLINE_TS_MS = 1752787200000


def _kline_message(symbol="BTCUSDT", closed=True):
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@kline_1m",
            "data": {
                "e": "kline",
                "k": {
                    "t": KLINE_TS_MS,
                    "s": symbol,
                    "o": "1.0",
                    "h": "2.0",
                    "l": "0.5",
                    "c": "1.5",
                    "v": "10.0",
                    "x": closed,
                },
            },
        }
    )


def test_ws_ingest_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("WS_INGEST_ENABLED", raising=False)
    assert ws_ingest_enabled() is False
    monkeypatch.setenv("WS_INGEST_ENABLED", "true")
    assert ws_ingest_enabled() is True


def test_stream_url_combines_symbols():
    ingester = BinanceKlineIngester(["BTCUSDT", "ETHUSDT"])
    assert ingester.stream_url.endswith(
        "?streams=btcusdt@kline_1m/ethusdt@kline_1m"
    )


@patch("ingestion.binance_ws.write_price_bars")
def test_closed_kline_written_as_1m_bar(mock_write):
    ingester = BinanceKlineIngester(["BTCUSDT"])

    ingester.handle_message(_kline_message(closed=True))

    assert ingester.bars_written == 1
    (symbol, frame), kwargs = mock_write.call_args
    assert symbol == "BTCUSDT"
    assert kwargs["interval"] == "1m"
    assert frame.iloc[0]["Close"] == 1.5
    assert frame.index[0].tz is not None


@patch("ingestion.binance_ws.write_price_bars")
def test_open_kline_and_garbage_ignored(mock_write):
    ingester = BinanceKlineIngester(["BTCUSDT"])

    ingester.handle_message(_kline_message(closed=False))
    ingester.handle_message("not json")
    ingester.handle_message(_kline_message(symbol="DOGEUSDT", closed=True))

    assert mock_write.call_count == 0
    assert ingester.bars_written == 0


@patch("ingestion.binance_ws.write_price_bars")
@patch("ingestion.binance_ws.latest_bar_timestamp")
def test_backfill_requests_gap_since_last_bar(mock_latest, mock_write, monkeypatch):
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    last = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)
    mock_latest.return_value = last

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def get_klines(self, symbol, interval, limit=1000, start_ms=None):
            self.calls.append((symbol, interval, start_ms))
            # two closed candles + the still-open one
            return [
                [KLINE_TS_MS, "1", "2", "0.5", "1.5", "10", 0, 0, 0, 0, 0, 0],
                [KLINE_TS_MS + 60_000, "1", "2", "0.5", "1.6", "10", 0, 0, 0, 0, 0, 0],
                [KLINE_TS_MS + 120_000, "1", "2", "0.5", "1.7", "10", 0, 0, 0, 0, 0, 0],
            ]

    provider = FakeProvider()
    ingester = BinanceKlineIngester(["BTCUSDT"], provider=provider)

    ingester.backfill_gaps()

    assert provider.calls == [
        ("BTCUSDT", "1m", int(last.timestamp() * 1000) + 60_000)
    ]
    (symbol, frame), kwargs = mock_write.call_args
    assert symbol == "BTCUSDT" and kwargs["interval"] == "1m"
    # open candle excluded
    assert len(frame) == 2
    assert ingester.bars_written == 2


@patch("ingestion.binance_ws.write_price_bars")
def test_backfill_noop_when_postgres_disabled(mock_write, monkeypatch):
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "0")

    ingester = BinanceKlineIngester(["BTCUSDT"])
    ingester.backfill_gaps()

    assert mock_write.call_count == 0


def test_crypto_watchlist_symbols_filters_and_dedupes():
    watchlist = Watchlist(
        interval_seconds=300,
        tickers=(
            TickerJobSpec("AAPL", "1d"),
            TickerJobSpec("BTCUSDT", "1d", market="crypto"),
            TickerJobSpec("BTCUSDT", "5d", market="crypto"),
            TickerJobSpec("ETHUSDT", "1d", market="crypto"),
        ),
        events=(),
    )
    assert crypto_watchlist_symbols(watchlist) == ["BTCUSDT", "ETHUSDT"]
