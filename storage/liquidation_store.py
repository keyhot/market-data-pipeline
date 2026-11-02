"""Postgres side of the liquidations recorder (C1). Insert-only.

`liquidations` and `liquidation_coverage` are append-only by convention, like
`world_events`: nothing here UPDATEs or DELETEs, and a replayed row is
dropped by the natural key rather than overwriting the first copy.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import psycopg
from psycopg.errors import UndefinedTable
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from ingestion.liquidations import KrakenMinuteAgg, Liquidation
from storage.db import get_pool

logger = logging.getLogger(__name__)

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
#
# The uid set that does the deduping is the recorder's own, so it has to
# outlive the process: a restarted recorder would otherwise fold again the
# trades its predecessor already flushed, and this upsert would ADD them
# (audit 5 F10). `kraken_fold_state` below carries the set across a restart.
# Replacing instead of adding is not a fix: a minute whose trades are split
# across two processes (or two polls) would be undercounted.
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

# The recorder's folded-uid set per symbol (scripts/migrate_019.sql), written
# in the SAME transaction as the minute upsert it vouches for: the aggregate
# and the set that says "these trades are already in it" land together or not
# at all. REPLACE, never merge: the recorder sends its whole set each time.
_KRAKEN_FOLD_STATE_SQL = """
    INSERT INTO kraken_fold_state (symbol, uids, updated_at)
    VALUES (%s, %s, now())
    ON CONFLICT (symbol) DO UPDATE SET
        uids = EXCLUDED.uids,
        updated_at = EXCLUDED.updated_at
"""


# Trouble with `kraken_fold_state` (a missing table, a missing privilege, a
# table of the wrong shape, a lock or statement timeout) must never cost a
# batch: the state is only the restart-dedupe extra, and without it the
# recorder falls back to its in-memory uid set, which accepts the old restart
# double count (<= one window, 100 trades per symbol). The warning is repeated
# at most this often per (action, error type): a poll loop would otherwise
# log it every few seconds for as long as the table stays broken.
FOLD_STATE_WARN_EVERY_S = 300.0
_monotonic = time.monotonic  # a seam: tests move the clock by hand
_fold_state_last_warned: dict[tuple[str, str], float] = {}


def _warn_fold_state_trouble(action: str, exc: psycopg.Error) -> None:
    key = (action, type(exc).__name__)
    now = _monotonic()
    last = _fold_state_last_warned.get(key)
    if last is not None and now - last < FOLD_STATE_WARN_EVERY_S:
        return
    _fold_state_last_warned[key] = now
    if isinstance(exc, UndefinedTable):
        what = (
            "kraken_fold_state is missing (scripts/migrate_019.sql is not applied)"
        )
    else:
        what = (
            f"kraken_fold_state could not be {action} "
            f"({type(exc).__name__}: {exc})"
        )
    if action == "written":
        effect = (
            "batch written without the restart-dedupe state: this process "
            "keeps deduping in memory, but a restart can still double count "
            "Kraken trades"
        )
    else:
        effect = (
            "starting with no restart-dedupe state: the first Kraken window "
            "of this process can be counted twice (<= 100 trades per symbol)"
        )
    logger.warning(
        f"{what}; {effect}; repeats are suppressed for "
        f"{FOLD_STATE_WARN_EVERY_S:.0f}s",
        extra={"error": f"{type(exc).__name__}: {exc}", "action": action},
    )


def _transaction_unusable(conn) -> bool:
    """True when nothing more can be trusted on this connection's transaction.

    `conn.transaction()` rolls back to its SAVEPOINT when the block raises,
    but psycopg only logs a rollback that itself failed and lets the original
    error bubble up. So after catching an error the connection may be gone
    (`closed`, which includes `broken`) or the transaction still aborted
    (INERROR). Carrying on would COMMIT an aborted transaction, which Postgres
    turns into a ROLLBACK without an error: the batch would be reported as
    written and be lost.
    """
    return conn.closed or conn.info.transaction_status in (
        TransactionStatus.INERROR,
        TransactionStatus.UNKNOWN,
    )


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
    kraken_seen: dict[str, list[str]] | None = None,
) -> None:
    """Liquidations, coverage, and the Kraken aggregate — one transaction.

    One transaction so a coverage row can never land without the rows it
    vouches for: a failure rolls everything back, and the caller keeps all
    four lists to retry. `coverage` is `(venue, minute_start_epoch_s,
    rows_received)`; `kraken_coverage` is `(symbol, minute_start_epoch_s,
    covered, rows_received)`.

    `kraken_seen` is `{symbol: uids already folded into the aggregate}`, the
    recorder's restart-dedupe state; it is stored in this same transaction so
    it can never describe trades the aggregate does not hold, or the other way
    round. If the state statement fails with ANY database error (the table
    does not exist yet because scripts/migrate_019.sql is not applied, a
    privilege or shape problem, a lock timeout) it is undone alone, by the
    SAVEPOINT, with a rate-limited warning, and the rest of the batch still
    lands, as it did before the table existed. The one exception is a
    transaction that is no longer usable (the connection died): that fails the
    whole batch for the caller to retry. Errors on the core upserts are never
    caught here.
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
            if kraken_seen:
                try:
                    with conn.transaction():  # a SAVEPOINT: only the state is undone
                        cur.executemany(
                            _KRAKEN_FOLD_STATE_SQL,
                            [
                                (symbol, Jsonb(list(uids)))
                                for symbol, uids in kraken_seen.items()
                            ],
                        )
                except psycopg.Error as exc:
                    if _transaction_unusable(conn):
                        raise  # not a state problem: fail the batch, retry whole
                    _warn_fold_state_trouble("written", exc)
            if kraken_coverage:
                cur.executemany(
                    _KRAKEN_COVERAGE_SQL,
                    [
                        (symbol, datetime.fromtimestamp(minute, tz=timezone.utc), covered, n)
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


def load_kraken_seen_uids() -> dict[str, list[str]]:
    """The uids each symbol's previous recorder process had folded, oldest first.

    Read once, before the first Kraken poll of a new process, so the first
    window is not folded a second time. `{}` when the SELECT itself fails with
    a database error (the table does not exist yet because migrate_019 is not
    applied, a privilege or shape problem): the recorder then dedupes in memory
    only, as it did before the table, and says so, because waiting would keep
    Kraken unpolled for as long as the table stays broken. A failure that is
    not about the table propagates, and the recorder waits rather than folding
    a window it cannot dedupe: no connection from the pool (Postgres not up
    yet), or a connection that died during the read.
    """
    with get_pool().connection() as conn:
        try:
            rows = conn.execute(
                "SELECT symbol, uids FROM kraken_fold_state"
            ).fetchall()
        except psycopg.Error as exc:
            if conn.closed:
                raise
            _warn_fold_state_trouble("read", exc)
            return {}
    return {symbol: [str(u) for u in uids] for symbol, uids in rows}
