import pandas as pd
from pathlib import Path

def utc_timestamp_str():
    return pd.Timestamp.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')

def raw_data_path(ticker, interval, timestamp = None) -> str:
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return Path("data/raw") / f"{ticker}_{interval}_{timestamp}.csv"

def processed_data_path(ticker, data_type, timestamp = None):
    if timestamp is None:
        timestamp = utc_timestamp_str()
    return Path("data/processed") / f"{ticker}_{data_type}_{timestamp}.csv"