import yfinance as yf
import pandas as pd
import numpy as np

def fetch_ticker(ticker_symbol: str = "MSFT", time_range: str = "1d") -> pd.DataFrame:
    ticker = yf.Ticker(ticker_symbol)
    history = ticker.history(period=time_range)
    return pd.DataFrame(history)