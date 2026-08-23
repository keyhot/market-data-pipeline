"""Fetch years of Binance klines into `price_bars` — the prerequisite for
every model experiment (vault `Docs/model-improvement-plan.md`, item 1).

Every published model figure is conditioned on **one 36-day window that rose
monotonically**, which is exactly the unchecked "holds across multiple regimes"
item on the pre-real-capital checklist. Until the store contains a real
drawdown, "beats buy-and-hold" cannot be distinguished from "was long during an
uptrend". This is a script, not a feature: `get_klines_paginated` already walks
the 1000-candle page limit, and the public kline endpoint needs no API key.

⚠️ **Bar counts, not minutes.** `model/features.py` windows
(`_RETURN_WINDOWS`, `_VOL_WINDOWS`, `_MOMENTUM_WINDOW`, `_VOLUME_WINDOW`) and
`BacktestConfig.horizon_bars` are all counted in **bars**. They are named after
the 1m interval they were written for, so on 15m bars `log_return_60` is a
15-hour return and `horizon_bars=15` is 3h45m, not 15 minutes. Nothing here
converts for you — say which interval a result came from, every time.

Idempotent: writes go through the `(symbol, bar_timestamp, interval)` upsert,
so re-running fills gaps rather than duplicating.

Usage:
    python scripts/backfill_history.py --symbols BTCUSDT,ETHUSDT \\
        --intervals 15m,1h --start 2020-01-01
    python scripts/backfill_history.py --symbols BTCUSDT --intervals 1h \\
        --start 2020-01-01 --dry-run
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from ingestion.binance_provider import BinanceProvider, _klines_to_frame  # noqa: E402
from storage.writes import postgres_write_enabled, write_price_bars  # noqa: E402

logger = logging.getLogger(__name__)

_UNIT_MS = {
    "m": 60 * 1000,
    "h": 60 * 60 * 1000,
    "d": 24 * 60 * 60 * 1000,
    "w": 7 * 24 * 60 * 60 * 1000,
}

# One span is one write and one progress line. 30 days is 2,880 candles at 15m
# (3 pages) — small enough to bound memory, large enough that a 6-year backfill
# is ~80 spans rather than thousands.
DEFAULT_SPAN_DAYS = 30


def interval_to_ms(interval: str) -> int:
    """'15m' -> 900000. Refuses anything it does not recognise, because a
    guessed bar size would mislabel every row written under it."""
    unit = interval[-1:]
    if unit not in _UNIT_MS or not interval[:-1].isdigit():
        raise ValueError(f"Unsupported interval: {interval}")
    return int(interval[:-1]) * _UNIT_MS[unit]


def closed_end_ms(now_ms: int, interval_ms: int) -> int:
    """Exclusive upper bound on kline *open* time that excludes the candle
    currently forming. The live 1m ingester stores closed candles only; a
    backfill that disagreed would write a half-formed bar that the next run
    silently overwrites with a different one."""
    return (now_ms // interval_ms) * interval_ms


def iter_spans(start_ms: int, end_ms: int, span_ms: int) -> Iterator[tuple[int, int]]:
    """Walk [start, end) in span-sized half-open chunks; the tail is short."""
    cursor = start_ms
    while cursor < end_ms:
        yield cursor, min(cursor + span_ms, end_ms)
        cursor += span_ms


def backfill_symbol(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    span_ms: int,
    fetch: Callable[[str, str, int, int], list[list]],
    write: Callable[[str, pd.DataFrame, str], None],
) -> int:
    """Fetch and store one symbol/interval span by span. Returns rows written."""
    total = 0
    for span_start, span_end in iter_spans(start_ms, end_ms, span_ms):
        raw = [k for k in fetch(symbol, interval, span_start, span_end)
               if span_start <= k[0] < span_end]
        if not raw:
            # Before the pair was listed, or a genuine venue gap. Neither is a
            # reason to abandon the rest of the range.
            continue
        frame = _klines_to_frame(raw)
        write(symbol, frame, interval)
        total += len(frame)
        logger.info(
            "Backfilled span",
            extra={
                "symbol": symbol,
                "interval": interval,
                "from": _iso(span_start),
                "bars": len(frame),
                "total": total,
            },
        )
    return total


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--intervals", default="15m,1h")
    parser.add_argument("--start", default="2020-01-01", help="UTC date, inclusive")
    parser.add_argument("--span-days", type=int, default=DEFAULT_SPAN_DAYS)
    parser.add_argument(
        "--sleep", type=float, default=0.25, help="seconds between kline pages"
    )
    parser.add_argument("--dry-run", action="store_true", help="fetch, do not write")
    args = parser.parse_args(argv)

    init_logging()

    if not args.dry_run and not postgres_write_enabled():
        # storage.writes._write returns silently when writes are off, so a full
        # backfill would report success having stored nothing.
        logger.error(
            "POSTGRES_WRITE_ENABLED is off — refusing to run a backfill that "
            "would write nothing and report success"
        )
        return 2

    start_ms = int(
        datetime.fromisoformat(args.start)
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )
    span_ms = args.span_days * _UNIT_MS["d"]
    provider = BinanceProvider()
    now_ms = int(time.time() * 1000)

    def fetch(symbol: str, interval: str, span_start: int, span_end: int) -> list[list]:
        return provider.get_klines_paginated(
            symbol, interval, span_start, span_end, sleep_seconds=args.sleep
        )

    def write(symbol: str, frame: pd.DataFrame, interval: str) -> None:
        write_price_bars(symbol, frame, interval=interval)

    grand_total = 0
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        for interval in [i.strip() for i in args.intervals.split(",") if i.strip()]:
            end_ms = closed_end_ms(now_ms, interval_to_ms(interval))
            logger.info(
                "Backfill starting",
                extra={
                    "symbol": symbol,
                    "interval": interval,
                    "from": _iso(start_ms),
                    "to": _iso(end_ms),
                },
            )
            rows = backfill_symbol(
                symbol,
                interval,
                start_ms,
                end_ms,
                span_ms,
                fetch,
                (lambda s, f, i: None) if args.dry_run else write,
            )
            grand_total += rows
            logger.info(
                "Backfill finished",
                extra={"symbol": symbol, "interval": interval, "bars": rows},
            )

    print(f"[backfill_history] {grand_total} bars"
          f"{' (dry run — nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
