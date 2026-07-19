from unittest.mock import patch

import pandas as pd
import pytest

from config.exceptions import NoDataFoundError
from ingestion import factory
from scheduler.jobs import run_event_job, run_inference_job, run_ticker_job


@pytest.fixture(autouse=True)
def fresh_provider():
    factory.reset_default_provider()
    yield
    factory.reset_default_provider()


@pytest.fixture(autouse=True)
def market_always_open(monkeypatch):
    """Equity-job tests must not depend on the wall clock."""
    monkeypatch.setattr("scheduler.jobs.is_equity_market_open", lambda: True)


@patch("scheduler.jobs.save_csv")
@patch("ingestion.yfinance_provider.yf.Ticker")
def test_ticker_job_fetches_and_saves(mock_ticker, mock_save):
    mock_ticker.return_value.history.return_value = pd.DataFrame({"Close": [1, 2]})

    result = run_ticker_job("AAPL", "1d")

    assert result["rows"] == 2
    assert result["cached"] is False
    assert "file_path" in result
    assert mock_save.call_count == 1


@patch("scheduler.jobs.save_csv")
@patch("ingestion.yfinance_provider.yf.Ticker")
def test_ticker_job_skips_save_on_cache_hit(mock_ticker, mock_save):
    mock_ticker.return_value.history.return_value = pd.DataFrame({"Close": [1, 2]})

    run_ticker_job("AAPL", "1d")
    result = run_ticker_job("AAPL", "1d")

    assert result["cached"] is True
    assert "file_path" not in result
    assert mock_save.call_count == 1


@patch("scheduler.jobs.save_csv")
@patch("ingestion.yfinance_provider.yf.Ticker")
def test_event_job_fetches_and_saves(mock_ticker, mock_save):
    mock_ticker.return_value.dividends = pd.Series([1.0], name="dividends")

    result = run_event_job("AAPL", "dividends")

    assert result["events"] == 1
    assert result["cached"] is False
    assert mock_save.call_count == 1


@patch("scheduler.jobs.save_csv")
@patch("ingestion.yfinance_provider.yf.Ticker")
def test_ticker_job_propagates_domain_errors(mock_ticker, mock_save):
    mock_ticker.return_value.history.return_value = pd.DataFrame()

    with pytest.raises(NoDataFoundError):
        run_ticker_job("AAPL", "1d")

    mock_save.assert_not_called()


@patch("scheduler.jobs.save_csv")
def test_equity_job_skipped_when_market_closed(mock_save, monkeypatch):
    monkeypatch.setattr("scheduler.jobs.is_equity_market_open", lambda: False)

    result = run_ticker_job("AAPL", "1d")

    assert result["skipped"] == "market_closed"
    assert mock_save.call_count == 0


@patch("scheduler.jobs.save_csv")
def test_crypto_job_ignores_market_hours_and_uses_crypto_provider(
    mock_save, monkeypatch
):
    monkeypatch.setattr("scheduler.jobs.is_equity_market_open", lambda: False)

    class FakeCrypto:
        def peek_history(self, symbol, time_range):
            return None

        def get_history(self, symbol, time_range):
            return pd.DataFrame({"Close": [1.0, 2.0, 3.0]})

    monkeypatch.setattr("scheduler.jobs.get_crypto_provider", lambda: FakeCrypto())

    result = run_ticker_job("BTCUSDT", "1d", market="crypto")

    assert result["rows"] == 3
    assert result["cached"] is False


@patch("scheduler.jobs.save_csv")
def test_event_job_skipped_when_market_closed(mock_save, monkeypatch):
    monkeypatch.setattr("scheduler.jobs.is_equity_market_open", lambda: False)

    result = run_event_job("AAPL", "dividends")

    assert result["skipped"] == "market_closed"
    assert mock_save.call_count == 0


@patch("model.predict.predict")
def test_inference_job_writes_signal(mock_predict, monkeypatch):
    mock_predict.return_value = {"direction": "up", "probability": 0.7}

    result = run_inference_job("BTCUSDT", "1m", market="crypto")

    assert result["direction"] == "up"
    mock_predict.assert_called_once_with("BTCUSDT", "1m")


@patch("model.predict.predict")
def test_inference_job_skips_without_model(mock_predict):
    from model.predict import NoModelArtifact

    mock_predict.side_effect = NoModelArtifact("none")

    result = run_inference_job("DOGEUSDT", "1m", market="crypto")

    assert result["skipped"] == "no_model"


@patch("model.predict.predict")
def test_inference_job_skips_equity_off_hours(mock_predict, monkeypatch):
    monkeypatch.setattr("scheduler.jobs.is_equity_market_open", lambda: False)

    result = run_inference_job("AAPL", "1d", market="equity")

    assert result["skipped"] == "market_closed"
    mock_predict.assert_not_called()


@patch("model.predict.predict", return_value=None)
def test_inference_job_skips_on_short_history(mock_predict):
    result = run_inference_job("BTCUSDT", "1m", market="crypto")

    assert result["skipped"] == "no_data"
