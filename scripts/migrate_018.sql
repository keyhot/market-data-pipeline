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

COMMIT;
