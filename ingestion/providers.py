import asyncio
from abc import ABC, abstractmethod

import pandas as pd


class MarketDataProvider(ABC):
    @abstractmethod
    def get_history(self, ticker_symbol: str, time_range: str) -> pd.DataFrame:
        pass

    @abstractmethod
    def get_events(self, ticker_symbol: str, event_type: str) -> pd.DataFrame:
        pass

    async def get_history_async(
        self, ticker_symbol: str, time_range: str
    ) -> pd.DataFrame:
        return await asyncio.to_thread(self.get_history, ticker_symbol, time_range)

    async def get_events_async(
        self, ticker_symbol: str, event_type: str
    ) -> pd.DataFrame:
        return await asyncio.to_thread(self.get_events, ticker_symbol, event_type)

    def get_news(self, ticker_symbol: str) -> list[dict]:
        raise NotImplementedError(f"{type(self).__name__} does not provide news")

    def peek_history(self, ticker_symbol: str, time_range: str):
        return None

    def peek_news(self, ticker_symbol: str):
        return None

    def peek_events(self, ticker_symbol: str, event_type: str):
        return None

    def invalidate_history(self, ticker_symbol: str, time_range: str) -> None:
        pass

    def invalidate_events(self, ticker_symbol: str, event_type: str) -> None:
        pass
