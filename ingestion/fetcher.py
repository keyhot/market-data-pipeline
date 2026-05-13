import pandas as pd

from config.exceptions import BaseAppException, DataTooLargeError, NoDataFoundError
from ingestion.providers import MarketDataProvider
from ingestion.yfinance_provider import YFinanceProvider

MAX_TICKER_ROWS = 5000


def fetch_ticker(
    ticker_symbol: str,
    time_range: str,
    provider: MarketDataProvider | None = None,
) -> pd.DataFrame:
    if provider is None:
        provider = YFinanceProvider()

    try:
        history = provider.get_history(ticker_symbol, time_range)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Failed to fetch ticker data: {e}", status_code=503)

    if history.empty:
        raise NoDataFoundError("No data returned")

    if len(history) > MAX_TICKER_ROWS:
        raise DataTooLargeError(
            f"Result too large: {len(history)} rows exceeds "
            f"the {MAX_TICKER_ROWS} row limit"
        )

    return history
