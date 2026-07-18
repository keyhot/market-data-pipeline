"""Binance market data provider (public REST, no API key).

Direct httpx against /api/v3/klines — decided over ccxt in the Sprint 8 plan:
the MarketDataProvider seam keeps a future swap contained, and the websocket
ingester must own reconnect/backfill logic either way.
"""

from datetime import date, datetime, timezone

import httpx
import pandas as pd

from config.exceptions import (
    BaseAppException,
    DataProviderError,
    UnsupportedEventTypeError,
)
from ingestion.providers import MarketDataProvider

BASE_URL = "https://api.binance.com"
KLINES_PATH = "/api/v3/klines"
MAX_KLINES_LIMIT = 1000

# TimeRange is the fetch range; Binance wants a bar count. Daily bars
# throughout — intraday comes from the websocket ingester, not this provider.
_RANGE_TO_DAILY_LIMIT = {
    "1d": 1,
    "5d": 5,
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
    "2y": 730,
    "5y": MAX_KLINES_LIMIT,
    "10y": MAX_KLINES_LIMIT,
    "max": MAX_KLINES_LIMIT,
}

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# Binance error code for an unknown symbol; treated as "no data", not an
# upstream failure, so the API surfaces 404 instead of 503.
_INVALID_SYMBOL_CODE = -1121


class BinanceProvider(MarketDataProvider):
    def __init__(self, timeout_seconds: float = 10.0):
        self._timeout = timeout_seconds

    def get_history(self, ticker_symbol: str, time_range: str) -> pd.DataFrame:
        limit = self._daily_limit(time_range)
        raw = self.get_klines(ticker_symbol, interval="1d", limit=limit)
        return _klines_to_frame(raw)

    def get_klines(
        self,
        ticker_symbol: str,
        interval: str,
        limit: int = MAX_KLINES_LIMIT,
        start_ms: int | None = None,
    ) -> list[list]:
        """Raw klines fetch; also used by the websocket ingester's gap backfill."""
        params: dict = {
            "symbol": ticker_symbol.upper(),
            "interval": interval,
            "limit": min(limit, MAX_KLINES_LIMIT),
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        try:
            response = httpx.get(
                f"{BASE_URL}{KLINES_PATH}", params=params, timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise DataProviderError(
                f"Binance request failed: {e}", status_code=503
            ) from e

        if response.status_code == 400:
            body = _safe_json(response)
            if body.get("code") == _INVALID_SYMBOL_CODE:
                return []
        if response.status_code in (418, 429):
            raise DataProviderError("Upstream rate limit exceeded", status_code=503)
        if response.status_code != 200:
            raise BaseAppException(
                f"Binance returned HTTP {response.status_code}", status_code=503
            )
        return response.json()

    def get_events(self, ticker_symbol: str, event_type: str) -> pd.DataFrame:
        raise UnsupportedEventTypeError(
            f"Binance has no corporate events (requested: {event_type})"
        )

    def _daily_limit(self, time_range: str) -> int:
        if time_range == "ytd":
            days = (datetime.now(timezone.utc).date() - date(
                datetime.now(timezone.utc).year, 1, 1
            )).days
            return max(1, min(days, MAX_KLINES_LIMIT))
        limit = _RANGE_TO_DAILY_LIMIT.get(time_range)
        if limit is None:
            raise BaseAppException(
                f"Unsupported time range for Binance: {time_range}", status_code=400
            )
        return limit


def _klines_to_frame(raw: list[list]) -> pd.DataFrame:
    """Shape raw klines like YFinanceProvider.get_history output:
    UTC datetime index, Open/High/Low/Close/Volume columns."""
    if not raw:
        return pd.DataFrame(columns=_COLUMNS)
    index = pd.to_datetime([k[0] for k in raw], unit="ms", utc=True)
    data = {
        "Open": [float(k[1]) for k in raw],
        "High": [float(k[2]) for k in raw],
        "Low": [float(k[3]) for k in raw],
        "Close": [float(k[4]) for k in raw],
        "Volume": [float(k[5]) for k in raw],
    }
    frame = pd.DataFrame(data, index=index)
    frame.index.name = "Date"
    return frame


def _safe_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}
