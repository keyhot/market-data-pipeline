"""Replay the salience rules over historical Binance klines so the world has
a past (Sprint 12).

The events are genuinely real — real bars through the same deterministic
rules the live path uses. The `backfilled: true` payload flag marks that the
world *learned* them rather than *witnessed* them, which the renderer may
present differently. Nothing is fabricated.

Re-runnable: writes go through append_world_events_backfill, which relies on
the natural-key unique index from scripts/migrate_012.sql.

Usage:
    python scripts/backfill_world_events.py --days 60
    python scripts/backfill_world_events.py --days 7 --dry-run
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from ingestion.binance_provider import BinanceProvider, _klines_to_frame  # noqa: E402
from scheduler.watchlist import load_watchlist  # noqa: E402
from storage.postgres_store import append_world_events_backfill  # noqa: E402
from world.salience import SalienceConfig, detect_events  # noqa: E402

logger = logging.getLogger(__name__)


def iter_windows(
    frame: pd.DataFrame, vol_window: int, lookback_bars: int
) -> Iterator[pd.DataFrame]:
    """Slide detect_events' evaluation window across a long frame.

    detect_events only inspects the final `lookback_bars` rows and needs
    `vol_window` rows of warm-up before them, so replaying history means
    handing it overlapping slices rather than the whole frame at once.
    """
    span = vol_window + lookback_bars
    if len(frame) < span:
        return
    end = span
    while end < len(frame):
        yield frame.iloc[end - span:end]
        end += lookback_bars
    yield frame.iloc[len(frame) - span:]


def apply_cooldown(events: list[dict], cooldown_minutes: int) -> list[dict]:
    """In-memory equivalent of world/events.py's DB-backed cooldown. Keyed on
    (event_type, symbol) so a 60-day replay doesn't flood the log."""
    cooldown = timedelta(minutes=cooldown_minutes)
    last_seen: dict[tuple[str, str | None], datetime] = {}
    kept: list[dict] = []
    for event in sorted(events, key=lambda e: e["occurred_at"]):
        key = (event["event_type"], event.get("symbol"))
        previous = last_seen.get(key)
        if previous is not None and event["occurred_at"] - previous < cooldown:
            continue
        last_seen[key] = event["occurred_at"]
        kept.append(event)
    return kept


def stamp_backfilled(events: list[dict]) -> list[dict]:
    """Flag provenance without mutating the caller's events."""
    return [
        {**event, "payload": {**(event.get("payload") or {}), "backfilled": True}}
        for event in events
    ]


def backfill_symbol(
    symbol: str,
    days: int,
    provider: BinanceProvider,
    config: SalienceConfig | None = None,
) -> list[dict]:
    config = config or SalienceConfig()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    raw = provider.get_klines_paginated(
        symbol,
        interval="1m",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
    )
    frame = _klines_to_frame(raw)
    logger.info("Fetched klines", extra={"symbol": symbol, "bars": len(frame)})

    events: list[dict] = []
    for window in iter_windows(frame, config.vol_window, config.lookback_bars):
        events.extend(detect_events(symbol, window, config))

    # detect_events re-evaluates overlapping bars, so the same firing can
    # appear in several windows; the natural key dedupes exact repeats and the
    # cooldown thins near-repeats, exactly as the live path does.
    unique = {(e["event_type"], e["symbol"], e["occurred_at"]): e for e in events}
    return stamp_backfilled(
        apply_cooldown(list(unique.values()), config.cooldown_minutes)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--symbols", default=None,
                        help="comma-separated; defaults to crypto watchlist entries")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_logging()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(dict.fromkeys(
            spec.symbol.upper()
            for spec in load_watchlist().tickers
            if spec.market == "crypto"
        ))

    provider = BinanceProvider()
    total = 0
    for symbol in symbols:
        events = backfill_symbol(symbol, args.days, provider)
        logger.info(
            "Backfill candidates",
            extra={"symbol": symbol, "events": len(events),
                   "types": sorted({e["event_type"] for e in events})},
        )
        if args.dry_run:
            continue
        total += append_world_events_backfill(events)

    logger.info("Backfill complete", extra={"written": total, "dry_run": args.dry_run})
    print(f"{'would write' if args.dry_run else 'wrote'} {total} world events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
