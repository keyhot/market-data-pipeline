import pandas as pd


def utc_timestamp_str():
    return pd.Timestamp.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')

def store_ticker_csv(df, ticker_name: str, time_range: str):
    timeframe_str = utc_timestamp_str()
    file_path = f"data/raw/{ticker_name}_{time_range}_{timeframe_str}.csv"
    df.to_csv(file_path)
    return file_path