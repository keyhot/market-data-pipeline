from datetime import UTC, datetime
from pathlib import Path

_DATA_ROOT = Path(__file__).parent.parent / "data"


def utc_timestamp_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def sanitize_ticker(ticker: str) -> str:
    return ticker.upper().replace("^", "").replace("/", "_").replace(".", "_")


def raw_data_path(ticker, interval, timestamp=None) -> Path:
    ticker = sanitize_ticker(ticker)
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return _DATA_ROOT / "raw" / "tickers" / f"{ticker}_{interval}_{timestamp}.csv"


def raw_event_path(ticker, event_type, timestamp=None) -> Path:
    ticker = sanitize_ticker(ticker)
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return _DATA_ROOT / "raw" / "events" / f"{ticker}_{event_type}_{timestamp}.csv"
