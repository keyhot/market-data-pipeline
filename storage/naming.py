import pandas as pd
from pathlib import Path

def utc_timestamp_str():
    return pd.Timestamp.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')

def sanitize_ticker(ticker: str) -> str:
    return ticker.upper().replace("^", "").replace("/", "_").replace(".", "_")

def raw_data_path(ticker, interval, timestamp = None) -> Path:
    ticker = sanitize_ticker(ticker)
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return Path("data/raw/tickers") / f"{ticker}_{interval}_{timestamp}.csv"

def processed_data_path(ticker, data_type, timestamp = None) -> Path:
    ticker = sanitize_ticker(ticker)
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return Path("data/processed/tickers") / f"{ticker}_{data_type}_{timestamp}.csv"

def raw_event_path(ticker, event_type, timestamp=None) -> Path:
    ticker = sanitize_ticker(ticker)
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return Path("data/raw/events") / f"{ticker}_{event_type}_{timestamp}.csv"

def processed_event_path(ticker, event_type, timestamp=None) -> Path:
    ticker = sanitize_ticker(ticker)
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return Path("data/processed/events") / f"{ticker}_{event_type}_{timestamp}.csv"