import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from config.exceptions import DataProviderError


class YFinanceProvider:

    def get_history(self, ticker_symbol: str, time_range: str):
        try:
            ticker = yf.Ticker(ticker_symbol)
            return ticker.history(period=time_range)
        except YFRateLimitError:
            raise DataProviderError("Upstream rate limit exceeded", status_code=503)

    def get_events(self, ticker_symbol: str, event_type: str):
        try:
            ticker = yf.Ticker(ticker_symbol)

            if event_type == "dividends":
                return ticker.dividends
            elif event_type == "splits":
                return ticker.splits
            elif event_type == "actions":
                return ticker.actions

        except YFRateLimitError:
            raise DataProviderError("Upstream rate limit exceeded", status_code=503)