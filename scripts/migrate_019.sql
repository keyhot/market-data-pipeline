-- F10 (audit 5) · the Kraken recorder's folded-uid state, so a restart does not
-- count the trades its predecessor already flushed a second time.
-- Idempotent — safe to re-run. Fresh volumes get this from db/init.sql;
-- keep both in sync. Apply:
--   docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_019.sql
--
-- NOT APPLIED to any database by the change that added this file. Applying it
-- to the live database is the owner's decision.
--
-- WHY. kraken_trade_minutes is written by an ADDITIVE upsert (storage/
-- liquidation_store.py). The uids that stop a trade being folded twice lived
-- only in the recorder's memory, so after a restart the first history window
-- (the newest <=100 trades) was folded again and every trade the old process
-- had already flushed was counted twice. Nothing in the existing schema says
-- which trades were folded, so the set has to be stored.
--
-- WHAT. One row per symbol: the recorder's own uid set (<=400 uids, oldest
-- first, KRAKEN_SEEN_UID_CAP). The recorder writes it in the SAME transaction
-- as the minute upsert it describes and loads it before its first Kraken poll.
-- A trade the old process folded but never flushed is not in the stored set,
-- so the new process counts it: no undercount either.
--
-- DEPLOY ORDER: either order is safe. Recorder code that runs before this
-- migration skips the state write with a warning (inside a SAVEPOINT, so the
-- rest of the batch still lands) and starts with no state, which is the
-- behaviour from before this table existed. Apply this first and the double
-- count stops at the next recorder restart.
BEGIN;

CREATE TABLE IF NOT EXISTS kraken_fold_state (
    symbol     TEXT        PRIMARY KEY,           -- PF_XBTUSD | PF_ETHUSD
    uids       JSONB       NOT NULL,              -- JSON array of trade uids, oldest first
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- KI-091 ledger. Guarded: the live database may not have schema_migrations
-- yet (storage/migrations.py), and an unguarded INSERT would then abort this
-- whole file.
DO $$
BEGIN
    IF to_regclass('schema_migrations') IS NOT NULL THEN
        INSERT INTO schema_migrations (version) VALUES ('019')
        ON CONFLICT (version) DO NOTHING;
    END IF;
END $$;

COMMIT;
