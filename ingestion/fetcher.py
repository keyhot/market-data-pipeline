import yfinance as yf
import pandas as pd
import numpy as np
from .storage import store_ticker_csv

def fetch_ticker(ticker_name: str = "MSFT", time_range: str = "1d"):
    dat = yf.Ticker(ticker_name)
    history = dat.history(period=time_range)
    DF = pd.DataFrame(history)
    file_path = store_ticker_csv(DF, ticker_name, time_range)
    return {"rows_fetched": len(history), "saved_to": file_path}