from abc import ABC, abstractmethod

import pandas as pd


class MarketDataProvider(ABC):
    @abstractmethod
    def get_history(self, ticker_symbol: str, time_range: str) -> pd.DataFrame:
        pass

    @abstractmethod
    def get_events(self, ticker_symbol: str, event_type: str) -> pd.DataFrame:
        pass

    def peek_history(self, ticker_symbol: str, time_range: str):
        return None

    def peek_events(self, ticker_symbol: str, event_type: str):
        return None

    def invalidate_history(self, ticker_symbol: str, time_range: str) -> None:
        pass

    def invalidate_events(self, ticker_symbol: str, event_type: str) -> None:
        pass
