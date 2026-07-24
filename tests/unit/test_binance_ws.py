import json
from datetime import datetime, timezone
from unittest.mock import patch

from ingestion.binance_provider import BinanceProvider
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
def test_backfill_paginates_the_whole_gap_and_excludes_forming_candle(
    mock_latest, mock_write, monkeypatch
):
    """A gap wider than one 1000-candle page must be fully paginated — a single
    request would leave a permanent hole. The still-forming current-minute
    candle is excluded by open time, not by position."""
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_minute_ms = (now_ms // 60_000) * 60_000
    gap_minutes = 1500  # > 1000, so pagination must loop
    last_ms = current_minute_ms - gap_minutes * 60_000
    mock_latest.return_value = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)

    class FakeProvider(BinanceProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        def get_klines(self, ticker_symbol, interval, limit=1000, start_ms=None):
            self.calls.append(start_ms)
            base = start_ms if start_ms is not None else last_ms + 60_000
            t = ((base + 59_999) // 60_000) * 60_000  # ceil to a minute boundary
            out = []
            # Binance returns closed candles plus the currently-forming one.
            while t <= current_minute_ms and len(out) < limit:
                out.append([t, "1", "2", "0.5", "1.5", "10", 0, 0, 0, 0, 0, 0])
                t += 60_000
            return out

    provider = FakeProvider()
    ingester = BinanceKlineIngester(["BTCUSDT"], provider=provider)

    ingester.backfill_gaps()

    # Pagination issued more than one request (the whole gap, not just 1000),
    # and the first request started exactly one minute after the last stored bar.
    assert len(provider.calls) >= 2
    assert provider.calls[0] == last_ms + 60_000
    expected_closed = gap_minutes - 1  # every closed minute; forming one excluded
    (symbol, frame), kwargs = mock_write.call_args
    assert symbol == "BTCUSDT" and kwargs["interval"] == "1m"
    assert len(frame) == expected_closed
    assert ingester.bars_written == expected_closed


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
