import yfinance as yf
import pandas as pd
import numpy as np
from config.exceptions import DataProviderError, NoDataFoundError, BaseAppException
from yfinance.exceptions import YFRateLimitError

def fetch_ticker(ticker_symbol: str = "MSFT", time_range: str = "1d") -> pd.DataFrame:
    try:
        ticker = yf.Ticker(ticker_symbol)
        history = ticker.history(period=time_range)
    except YFRateLimitError as e:
        raise DataProviderError("Upstream rate limit exceeded", status_code=503)
    except Exception as e:
        raise DataProviderError(f"Upstream provider error: {e}")
    
    if history.empty:
        raise NoDataFoundError("No data returned")
    
    return pd.DataFrame(history)