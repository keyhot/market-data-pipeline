import pandas as pd
import yfinance as yf
from config.exceptions import NoDataFoundError
from yfinance.exceptions import YFRateLimitError
from config.exceptions import DataProviderError, BaseAppException


def fetch_ticker(
    ticker_symbol: str,
    time_range: str,
    provider=None
) -> pd.DataFrame:
    if provider is None:
        ticker = yf.Ticker(ticker_symbol)
        try:
            history = ticker.history(period=time_range)
        except YFRateLimitError:
            raise DataProviderError("Upstream rate limit exceeded", status_code=503)
        except Exception as e:
            raise BaseAppException(f"Failed to fetch ticker data: {e}", status_code=503)
    else:
        history = provider.get_history(ticker_symbol, time_range)

    if history.empty:
        raise NoDataFoundError("No data returned")

    return pd.DataFrame(history)