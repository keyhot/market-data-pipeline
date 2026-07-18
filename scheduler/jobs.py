import logging

from ingestion.event_fetcher import fetch_events
from ingestion.factory import get_crypto_provider, get_default_provider
from ingestion.fetcher import fetch_ticker
from scheduler.market_hours import is_equity_market_open
from storage.filesystem import csv_write_enabled, save_csv
from storage.naming import raw_data_path, raw_event_path
from storage.writes import write_events, write_price_bars

logger = logging.getLogger(__name__)


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
