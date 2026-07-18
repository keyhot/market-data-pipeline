import pytest

from scheduler.watchlist import (
    DEFAULT_WATCHLIST_PATH,
    EventJobSpec,
    TickerJobSpec,
    WatchlistError,
    load_watchlist,
)


def write_yaml(tmp_path, content):
    path = tmp_path / "watchlist.yaml"
    path.write_text(content)
    return path


def test_load_valid_watchlist(tmp_path):
    path = write_yaml(
        tmp_path,
        """
interval_seconds: 120
tickers:
  - symbol: aapl
    time_ranges: [1d, 5d]
events:
  - symbol: AAPL
    event_types: [dividends]
""",
    )

    watchlist = load_watchlist(path)

    assert watchlist.interval_seconds == 120
    assert watchlist.tickers == (
        TickerJobSpec("AAPL", "1d"),
        TickerJobSpec("AAPL", "5d"),
    )
    assert watchlist.events == (EventJobSpec("AAPL", "dividends"),)


def test_interval_defaults_when_omitted(tmp_path):
    path = write_yaml(tmp_path, "tickers:\n  - symbol: AAPL\n    time_ranges: [1d]\n")

    assert load_watchlist(path).interval_seconds == 300


def test_invalid_time_range_rejected(tmp_path):
    path = write_yaml(tmp_path, "tickers:\n  - symbol: AAPL\n    time_ranges: [7w]\n")

    with pytest.raises(WatchlistError, match="7w"):
        load_watchlist(path)


def test_invalid_event_type_rejected(tmp_path):
    path = write_yaml(tmp_path, "events:\n  - symbol: AAPL\n    event_types: [ipo]\n")

    with pytest.raises(WatchlistError, match="ipo"):
        load_watchlist(path)


def test_missing_symbol_rejected(tmp_path):
    path = write_yaml(tmp_path, "tickers:\n  - time_ranges: [1d]\n")

    with pytest.raises(WatchlistError, match="symbol"):
        load_watchlist(path)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(WatchlistError, match="not found"):
        load_watchlist(tmp_path / "nope.yaml")


def test_negative_interval_rejected(tmp_path):
    path = write_yaml(tmp_path, "interval_seconds: -5\n")

    with pytest.raises(WatchlistError, match="positive"):
        load_watchlist(path)


def test_checked_in_watchlist_is_valid():
    watchlist = load_watchlist(DEFAULT_WATCHLIST_PATH)

    assert watchlist.tickers


def test_crypto_market_parsed(tmp_path):
    path = write_yaml(
        tmp_path,
        "tickers:\n"
        "  - symbol: BTCUSDT\n"
        "    time_ranges: [1d]\n"
        "    market: crypto\n"
        "  - symbol: AAPL\n"
        "    time_ranges: [1d]\n",
    )

    watchlist = load_watchlist(path)

    assert watchlist.tickers[0].market == "crypto"
    assert watchlist.tickers[1].market == "equity"


def test_invalid_market_rejected(tmp_path):
    path = write_yaml(
        tmp_path,
        "tickers:\n"
        "  - symbol: BTCUSDT\n"
        "    time_ranges: [1d]\n"
        "    market: forex\n",
    )

    with pytest.raises(WatchlistError, match="market"):
        load_watchlist(path)
