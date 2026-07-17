import logging

from ingestion.event_fetcher import fetch_events
from ingestion.factory import get_default_provider
from ingestion.fetcher import fetch_ticker
from storage.dual_write import mirror_events, mirror_price_bars
from storage.filesystem import save_csv
from storage.naming import raw_data_path, raw_event_path

logger = logging.getLogger(__name__)


def run_ticker_job(symbol: str, time_range: str) -> dict:
    was_cached = get_default_provider().peek_history(symbol, time_range) is not None

    data = fetch_ticker(symbol, time_range)
    result = {
        "symbol": symbol,
        "time_range": time_range,
        "rows": len(data),
        "cached": was_cached,
    }

    if not was_cached:
        path = raw_data_path(symbol, time_range)
        save_csv(path, data)
        mirror_price_bars(symbol, data)
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
        path = raw_event_path(symbol, event_type)
        save_csv(path, events)
        mirror_events(symbol, event_type, events)
        result["file_path"] = str(path)

    logger.info("Scheduled event fetch complete", extra=result)
    return result
