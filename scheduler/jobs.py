import logging

import pandas as pd

from ingestion.event_fetcher import fetch_events
from ingestion.factory import get_crypto_provider, get_default_provider
from ingestion.fetcher import fetch_ticker
from scheduler.market_hours import is_equity_market_open
from storage.filesystem import csv_write_enabled, save_csv
from storage.naming import raw_data_path, raw_event_path
from storage.postgres_store import get_price_bars
from storage.writes import write_events, write_price_bars
from world.events import record_salient_events

logger = logging.getLogger(__name__)

# Rolling stats need vol_window + lookback; fetch with slack.
_SALIENCE_BARS_LIMIT = 120


def run_ticker_job(symbol: str, time_range: str, market: str = "equity") -> dict:
    if market == "equity" and not is_equity_market_open():
        result = {
            "symbol": symbol,
            "time_range": time_range,
            "skipped": "market_closed",
        }
        logger.info("Skipping equity fetch, market closed", extra=result)
        return result

    provider = get_crypto_provider() if market == "crypto" else get_default_provider()
    was_cached = provider.peek_history(symbol, time_range) is not None

    data = fetch_ticker(symbol, time_range, provider=provider)
    result = {
        "symbol": symbol,
        "time_range": time_range,
        "rows": len(data),
        "cached": was_cached,
    }

    if not was_cached:
        write_price_bars(symbol, data)
        if csv_write_enabled():
            path = raw_data_path(symbol, time_range)
            save_csv(path, data)
            result["file_path"] = str(path)

    logger.info("Scheduled ticker fetch complete", extra=result)
    return result


def run_event_job(symbol: str, event_type: str) -> dict:
    # Corporate events are an equity-only concept; skip off-hours like tickers
    # (yfinance fetches full history under the hood even for dividends).
    if not is_equity_market_open():
        result = {
            "symbol": symbol,
            "event_type": event_type,
            "skipped": "market_closed",
        }
        logger.info("Skipping equity event fetch, market closed", extra=result)
        return result

    was_cached = get_default_provider().peek_events(symbol, event_type) is not None

    events = fetch_events(symbol, event_type)
    result = {
        "symbol": symbol,
        "event_type": event_type,
        "events": len(events),
        "cached": was_cached,
    }

    if not was_cached:
        write_events(symbol, event_type, events)
        if csv_write_enabled():
            path = raw_event_path(symbol, event_type)
            save_csv(path, events)
            result["file_path"] = str(path)

    logger.info("Scheduled event fetch complete", extra=result)
    return result


def run_salience_job(symbol: str, interval: str = "1m") -> dict:
    """Evaluate salience rules over recent stored bars; append world events.
    Crypto-only for now (runs 24/7 off the websocket-ingested 1m stream)."""
    bars = pd.DataFrame(get_price_bars(symbol, interval, _SALIENCE_BARS_LIMIT))
    if bars.empty:
        result = {"symbol": symbol, "skipped": "no_stored_bars"}
        logger.info("Salience skipped, no bars", extra=result)
        return result

    events = record_salient_events(symbol, bars)
    result = {
        "symbol": symbol,
        "interval": interval,
        "events": len(events),
        "event_types": sorted({e["event_type"] for e in events}),
    }
    logger.info("Salience job complete", extra=result)
    return result


def run_resolver_job() -> dict:
    """Resolve signals past their horizon into public win/loss world events."""
    from world.resolver import resolve_pending

    resolutions = resolve_pending()
    result = {
        "resolved": len(resolutions),
        "outcomes": sorted({r["outcome"] for r in resolutions}),
    }
    logger.info("Resolver job complete", extra=result)
    return result


def run_inference_job(
    symbol: str, interval: str = "1m", market: str = "crypto"
) -> dict:
    """Write one model signal for the newest complete bar. Missing artifact
    is a logged skip, never a crash — training is a manual runbook step."""
    from model.predict import NoModelArtifact, predict

    if market == "equity" and not is_equity_market_open():
        result = {"symbol": symbol, "skipped": "market_closed"}
        logger.info("Skipping equity inference, market closed", extra=result)
        return result

    try:
        signal = predict(symbol, interval)
    except NoModelArtifact:
        result = {"symbol": symbol, "interval": interval, "skipped": "no_model"}
        logger.info("Skipping inference, no trained model", extra=result)
        return result

    if signal is None:
        result = {"symbol": symbol, "interval": interval, "skipped": "no_data"}
        logger.info("Skipping inference, not enough bars", extra=result)
        return result

    result = {
        "symbol": symbol,
        "interval": interval,
        "direction": signal["direction"],
        "probability": round(signal["probability"], 4),
    }
    logger.info("Inference complete", extra=result)
    return result
