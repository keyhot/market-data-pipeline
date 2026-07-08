from abc import ABC, abstractmethod

import pandas as pd

from storage.filesystem import save_csv
from storage.naming import raw_news_path


class NewsStore(ABC):
    @abstractmethod
    def save(self, ticker_symbol: str, news: pd.DataFrame) -> str:
        pass


class CsvNewsStore(NewsStore):
    def save(self, ticker_symbol: str, news: pd.DataFrame) -> str:
        path = raw_news_path(ticker_symbol)
        save_csv(path, news)
        return str(path)
