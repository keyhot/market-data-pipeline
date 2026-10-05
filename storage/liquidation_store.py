"""Postgres side of the liquidations recorder (C1). Insert-only.

`liquidations` and `liquidation_coverage` are append-only by convention, like
`world_events`: nothing here UPDATEs or DELETEs, and a replayed row is
dropped by the natural key rather than overwriting the first copy.
"""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from ingestion.liquidations import Liquidation
from storage.db import get_pool

_LIQUIDATIONS_SQL = """
    INSERT INTO liquidations
        (venue, symbol, event_ts, side, price, size, size_unit,
         qty_base, notional, source, raw, venue_trade_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
"""
# No explicit conflict target: Kraken rows are additionally deduplicated by
# (venue, venue_trade_id) (uq_liquidations_venue_trade_id, a partial unique
# index — see scripts/migrate_018.sql), since Kraken's own `trade_id` is not
# a stable cross-poll key but its `uid` is. ON CONFLICT DO NOTHING with no
# target catches a violation of either unique index, for every venue.

_COVERAGE_SQL = """
    INSERT INTO liquidation_coverage (venue, minute, rows_received)
    VALUES (%s, %s, %s)
    ON CONFLICT (venue, minute) DO NOTHING
"""


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
    liquidations: list[Liquidation], coverage: list[tuple[str, int, int]]
) -> None:
    """Liquidations, then the minutes they prove covered — one transaction.

    One transaction so a coverage row can never land without the rows it
    vouches for: a failure rolls both back, and the caller keeps both to
    retry. `coverage` is `(venue, minute_start_epoch_s, rows_received)`.
    """
    if not liquidations and not coverage:
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


def latest_coverage() -> dict[str, datetime]:
    """Newest covered minute per venue: the recorder's liveness signal."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT venue, max(minute) FROM liquidation_coverage GROUP BY venue"
        ).fetchall()
    return {venue: minute for venue, minute in rows}
