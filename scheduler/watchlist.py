from dataclasses import dataclass
from pathlib import Path

import yaml

from schemas.enums import EventType, TimeRange

DEFAULT_WATCHLIST_PATH = Path(__file__).parent.parent / "config" / "watchlist.yaml"
DEFAULT_INTERVAL_SECONDS = 300

_VALID_TIME_RANGES = {member.value for member in TimeRange}
_VALID_EVENT_TYPES = {member.value for member in EventType}


class WatchlistError(Exception):
    pass


VALID_MARKETS = {"equity", "crypto"}


@dataclass(frozen=True)
class TickerJobSpec:
    symbol: str
    time_range: str
    market: str = "equity"


@dataclass(frozen=True)
class EventJobSpec:
    symbol: str
    event_type: str


@dataclass(frozen=True)
class Watchlist:
    interval_seconds: int
    tickers: tuple[TickerJobSpec, ...]
    events: tuple[EventJobSpec, ...]


def load_watchlist(path: Path = DEFAULT_WATCHLIST_PATH) -> Watchlist:
    if not path.exists():
        raise WatchlistError(f"Watchlist file not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise WatchlistError("Watchlist must be a YAML mapping")

    interval = raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if not isinstance(interval, int) or interval <= 0:
        raise WatchlistError(
            f"interval_seconds must be a positive int, got {interval!r}"
        )

    tickers = []
    for entry in raw.get("tickers") or []:
        symbol = _read_symbol(entry)
        market = entry.get("market", "equity")
        if market not in VALID_MARKETS:
            raise WatchlistError(
                f"Invalid market {market!r} for {symbol}; "
                f"valid: {sorted(VALID_MARKETS)}"
            )
        for time_range in entry.get("time_ranges") or []:
            if time_range not in _VALID_TIME_RANGES:
                raise WatchlistError(
                    f"Invalid time_range {time_range!r} for {symbol}; "
                    f"valid: {sorted(_VALID_TIME_RANGES)}"
                )
            tickers.append(
                TickerJobSpec(symbol=symbol, time_range=time_range, market=market)
            )

    events = []
    for entry in raw.get("events") or []:
        symbol = _read_symbol(entry)
        for event_type in entry.get("event_types") or []:
            if event_type not in _VALID_EVENT_TYPES:
                raise WatchlistError(
                    f"Invalid event_type {event_type!r} for {symbol}; "
                    f"valid: {sorted(_VALID_EVENT_TYPES)}"
                )
            events.append(EventJobSpec(symbol=symbol, event_type=event_type))

    return Watchlist(
        interval_seconds=interval, tickers=tuple(tickers), events=tuple(events)
    )


def _read_symbol(entry) -> str:
    if not isinstance(entry, dict):
        raise WatchlistError(f"Watchlist entry must be a mapping, got {entry!r}")
    symbol = entry.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise WatchlistError(f"Missing or empty symbol in entry: {entry!r}")
    return symbol.strip().upper()
