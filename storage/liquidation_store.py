"""Postgres side of the liquidations recorder (C1). Insert-only.

`liquidations` and `liquidation_coverage` are append-only by convention, like
`world_events`: nothing here UPDATEs or DELETEs, and a replayed row is
dropped by the natural key rather than overwriting the first copy.
"""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from ingestion.liquidations import KrakenMinuteAgg, Liquidation
from storage.db import get_pool

_LIQUIDATIONS_SQL = """
    INSERT INTO liquidations
        (venue, symbol, event_ts, side, price, size, size_unit,
         qty_base, notional, source, raw, venue_trade_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
"""
# No explicit conflict target: ON CONFLICT DO NOTHING with no target catches
# a violation of either unique index. The two indexes partition by whether
# venue_trade_id is NULL (scripts/migrate_018.sql) precisely so they never
# both apply to the same row: Kraken rows (venue_trade_id set) dedupe only
# on (venue, venue_trade_id), since a cascade can produce several distinct
# real Kraken fills sharing one (venue, symbol, event_ts, side, price, size)
# tuple — the old natural key alone would have silently dropped them. Every
# other venue (venue_trade_id NULL) still dedupes on that natural key.

_COVERAGE_SQL = """
    INSERT INTO liquidation_coverage (venue, minute, rows_received)
    VALUES (%s, %s, %s)
    ON CONFLICT (venue, minute) DO NOTHING
"""

# Kraken's coverage is per-symbol and gap-aware (ingestion/liquidations.py
# kraken_coverage_minutes), a different grain than the WS venues' per-venue
# liveness tracker above, so it gets its own table rather than a forced fit
# into `liquidation_coverage`. `covered=false` rows are written deliberately
# (not just omitted) so a detected gap is visible, not merely absent.
_KRAKEN_COVERAGE_SQL = """
    INSERT INTO liquidation_coverage_kraken (symbol, minute, covered, rows_received)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (symbol, minute) DO NOTHING
"""

# Additive merge across polls: a minute's trades may arrive split across two
# or more polls (a busy window outrunning the 100-row cap). The caller
# (LiquidationRecorder.process_kraken_response) dedupes by uid before
# aggregating, so trade_count/total_size/size_near_min/size_near_max summing
# here never double-counts a trade. min/max price converge to the true
# minute extremes via LEAST/GREATEST regardless of poll order.
_KRAKEN_MINUTES_SQL = """
    INSERT INTO kraken_trade_minutes
        (symbol, minute, min_price, max_price, trade_count, total_size,
         size_near_min, size_near_max, liq_count, liq_size)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (symbol, minute) DO UPDATE SET
        min_price = LEAST(kraken_trade_minutes.min_price, EXCLUDED.min_price),
        max_price = GREATEST(kraken_trade_minutes.max_price, EXCLUDED.max_price),
        trade_count = kraken_trade_minutes.trade_count + EXCLUDED.trade_count,
        total_size = kraken_trade_minutes.total_size + EXCLUDED.total_size,
        size_near_min = kraken_trade_minutes.size_near_min + EXCLUDED.size_near_min,
        size_near_max = kraken_trade_minutes.size_near_max + EXCLUDED.size_near_max,
        liq_count = kraken_trade_minutes.liq_count + EXCLUDED.liq_count,
        liq_size = kraken_trade_minutes.liq_size + EXCLUDED.liq_size
"""


def _kraken_minute_row(agg: KrakenMinuteAgg) -> tuple:
    return (
        agg.symbol,
        datetime.fromtimestamp(agg.minute, tz=timezone.utc),
        agg.min_price,
        agg.max_price,
        agg.trade_count,
        agg.total_size,
        agg.size_near_min,
        agg.size_near_max,
        agg.liq_count,
        agg.liq_size,
    )


def _liquidation_row(liq: Liquidation) -> tuple:
    return (
        liq.venue,
        liq.symbol,
        liq.event_ts,
        liq.side,
        liq.price,
        liq.size,
        liq.size_unit,
        liq.qty_base,
        liq.notional,
        liq.source,
        Jsonb(liq.raw),
        liq.venue_trade_id,
    )


def write_liquidation_batch(
    liquidations: list[Liquidation],
    coverage: list[tuple[str, int, int]],
    kraken_minutes: list[KrakenMinuteAgg] = (),
    kraken_coverage: list[tuple[str, int, bool, int]] = (),
) -> None:
    """Liquidations, coverage, and the Kraken aggregate — one transaction.

    One transaction so a coverage row can never land without the rows it
    vouches for: a failure rolls everything back, and the caller keeps all
    four lists to retry. `coverage` is `(venue, minute_start_epoch_s,
    rows_received)`; `kraken_coverage` is `(symbol, minute_start_epoch_s,
    covered, rows_received)`.
    """
    if not liquidations and not coverage and not kraken_minutes and not kraken_coverage:
        return
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            if liquidations:
                cur.executemany(
                    _LIQUIDATIONS_SQL, [_liquidation_row(x) for x in liquidations]
                )
            if coverage:
                cur.executemany(
                    _COVERAGE_SQL,
                    [
                        (venue, datetime.fromtimestamp(minute, tz=timezone.utc), n)
                        for venue, minute, n in coverage
                    ],
                )
            if kraken_minutes:
                cur.executemany(
                    _KRAKEN_MINUTES_SQL, [_kraken_minute_row(a) for a in kraken_minutes]
                )
            if kraken_coverage:
                cur.executemany(
                    _KRAKEN_COVERAGE_SQL,
                    [
                        (
                            symbol,
                            datetime.fromtimestamp(minute, tz=timezone.utc),
                            covered,
                            n,
                        )
                        for symbol, minute, covered, n in kraken_coverage
                    ],
                )


def latest_coverage() -> dict[str, datetime]:
    """Newest covered minute per venue: the recorder's liveness signal."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT venue, max(minute) FROM liquidation_coverage GROUP BY venue"
        ).fetchall()
    return {venue: minute for venue, minute in rows}
