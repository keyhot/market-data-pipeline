-- C1 · the liquidations recorder: adds Kraken Futures as a venue.
-- Idempotent — safe to re-run. Fresh volumes get this from db/init.sql;
-- keep both in sync. Apply:
--   docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_018.sql
--
-- Kraken's public trade-history REST endpoint has no stable cross-poll
-- trade_id (it counts down within each 100-row window — see
-- ingestion/liquidations.py's parse_kraken_history), only a `uid`.
--
-- The OLD natural key (venue, symbol, event_ts, side, price, size) is NOT
-- safe for Kraken: a cascade sweeping one price level in one microsecond
-- produces several distinct real fills with identical event_ts/side/price/
-- size (confirmed in the real recorded fixture,
-- tests/fixtures/liquidations/kraken_trade_history_pf_xbtusd.json — see
-- test_kraken_natural_key_collides_but_uid_does_not in
-- tests/unit/test_liquidations.py). `ON CONFLICT DO NOTHING` with no target
-- fires on ANY unique-index violation, so the old natural key would have
-- silently dropped real Kraken rows that share a (venue, symbol, event_ts,
-- side, price, size) tuple. It is narrowed to NULL venue_trade_id rows
-- (every other venue); Kraken dedupes on (venue, venue_trade_id) only.
--
-- DEPLOY ORDER: this migration must land before the recorder image that
-- writes venue_trade_id starts — see Docs/kraken-trade-recorder-handoff.md.
BEGIN;

ALTER TABLE liquidations ADD COLUMN IF NOT EXISTS venue_trade_id TEXT;

DROP INDEX IF EXISTS uq_liquidations_natural;
CREATE UNIQUE INDEX IF NOT EXISTS uq_liquidations_natural
    ON liquidations (venue, symbol, event_ts, side, price, size)
    WHERE venue_trade_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_liquidations_venue_trade_id
    ON liquidations (venue, venue_trade_id)
    WHERE venue_trade_id IS NOT NULL;

-- K2: a compact per-minute aggregate of ALL Kraken trade prints (not just
-- the liquidation-flagged subset above), so R3-b can later check whether a
-- 1m candle's wick low/high was a real, tradeable print with real size.
-- Idempotent upsert merges partial minutes across polls additively; the
-- caller (ingestion/liquidation_recorder.py) dedupes by trade `uid` before
-- aggregating, so a minute seen in two polls is never double-counted here.
CREATE TABLE IF NOT EXISTS kraken_trade_minutes (
    symbol        TEXT        NOT NULL,   -- PF_XBTUSD | PF_ETHUSD
    minute        TIMESTAMPTZ NOT NULL,
    min_price     NUMERIC     NOT NULL,
    max_price     NUMERIC     NOT NULL,
    trade_count   INTEGER     NOT NULL,
    total_size    NUMERIC     NOT NULL,   -- base-asset units (see kraken_contracts)
    size_near_min NUMERIC     NOT NULL,   -- size within ~2bps of min_price
    size_near_max NUMERIC     NOT NULL,   -- size within ~2bps of max_price
    liq_count     INTEGER     NOT NULL,
    liq_size      NUMERIC     NOT NULL,
    PRIMARY KEY (symbol, minute)
);

-- Kraken's own coverage/gap record: per-symbol and gap-aware, unlike
-- liquidation_coverage above (per-venue, wall-clock liveness). `covered =
-- false` rows are written deliberately so a detected gap is visible rather
-- than merely absent; analyses that want "covered minutes only" filter on
-- `covered`.
CREATE TABLE IF NOT EXISTS liquidation_coverage_kraken (
    symbol        TEXT        NOT NULL,
    minute        TIMESTAMPTZ NOT NULL,
    covered       BOOLEAN     NOT NULL,
    rows_received INTEGER     NOT NULL,
    PRIMARY KEY (symbol, minute)
);

COMMIT;
