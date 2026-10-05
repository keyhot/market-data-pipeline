-- C1 · the liquidations recorder: adds Kraken Futures as a venue.
-- Idempotent — safe to re-run. Fresh volumes get this from db/init.sql;
-- keep both in sync. Apply:
--   docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_018.sql
--
-- Kraken's public trade-history REST endpoint has no stable cross-poll
-- trade_id (it counts down within each 100-row window — see
-- ingestion/liquidations.py's parse_kraken_history), only a `uid`. The
-- existing natural key (venue, symbol, event_ts, side, price, size) still
-- dedupes the other venues; venue_trade_id gives Kraken its own idempotent
-- key without disturbing it.

ALTER TABLE liquidations ADD COLUMN IF NOT EXISTS venue_trade_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_liquidations_venue_trade_id
    ON liquidations (venue, venue_trade_id)
    WHERE venue_trade_id IS NOT NULL;
