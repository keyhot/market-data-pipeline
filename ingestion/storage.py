import pandas as pd


def store_ticker_csv(df, ticker_name: str, time_range: str):
    timeframe_str = pd.Timestamp.now().strftime('%Y-%m-%d')
    file_path = f"data/raw/{ticker_name}_{time_range}_{timeframe_str}.csv"
    df.to_csv(file_path)
    return file_path