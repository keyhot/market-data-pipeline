from unittest.mock import patch

import httpx
import pytest

from config.exceptions import (
    BaseAppException,
    DataProviderError,
    NoDataFoundError,
    UnsupportedEventTypeError,
)
from ingestion.binance_provider import BinanceProvider
from ingestion.fetcher import fetch_ticker

KLINE_ROW = [
    1752787200000, "117000.1", "117500.5", "116800.0", "117250.3", "123.45",
    1752787259999, "0", 0, "0", "0", "0",
]


def _response(status_code=200, json_body=None):
    return httpx.Response(
        status_code=status_code,
        json=json_body if json_body is not None else [KLINE_ROW],
        request=httpx.Request("GET", "https://api.binance.com/api/v3/klines"),
    )


@patch("ingestion.binance_provider.httpx.get")
def test_get_history_shapes_yfinance_like_frame(mock_get):
    mock_get.return_value = _response()

    frame = BinanceProvider().get_history("btcusdt", "1d")

    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.index.tz is not None
    assert frame.iloc[0]["Close"] == 117250.3
    params = mock_get.call_args.kwargs["params"]
    assert params["symbol"] == "BTCUSDT"
    assert params["interval"] == "1d"
    assert params["limit"] == 1


@patch("ingestion.binance_provider.httpx.get")
def test_time_range_maps_to_limits(mock_get):
    mock_get.return_value = _response()
    provider = BinanceProvider()

    for time_range, expected in [("5d", 5), ("1mo", 30), ("1y", 365)]:
        provider.get_history("BTCUSDT", time_range)
        assert mock_get.call_args.kwargs["params"]["limit"] == expected


def test_unsupported_time_range_rejected():
    with pytest.raises(BaseAppException) as exc:
        BinanceProvider().get_history("BTCUSDT", "42x")
    assert exc.value.status_code == 400


@patch("ingestion.binance_provider.httpx.get")
def test_invalid_symbol_becomes_no_data(mock_get):
    mock_get.return_value = _response(400, {"code": -1121, "msg": "Invalid symbol."})

    with pytest.raises(NoDataFoundError):
        fetch_ticker("NOTACOIN", "1d", provider=BinanceProvider())


@patch("ingestion.binance_provider.httpx.get")
def test_rate_limit_maps_to_provider_error(mock_get):
    mock_get.return_value = _response(429, {})

    with pytest.raises(DataProviderError):
        BinanceProvider().get_history("BTCUSDT", "1d")


@patch("ingestion.binance_provider.httpx.get")
def test_network_error_maps_to_provider_error(mock_get):
    mock_get.side_effect = httpx.ConnectError("boom")

    with pytest.raises(DataProviderError):
        BinanceProvider().get_history("BTCUSDT", "1d")


def test_get_events_unsupported():
    with pytest.raises(UnsupportedEventTypeError):
        BinanceProvider().get_events("BTCUSDT", "dividends")
