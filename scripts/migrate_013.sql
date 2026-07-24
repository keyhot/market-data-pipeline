-- Sprint 12 follow-up migration: price_bars.volume BIGINT -> NUMERIC(28, 8).
-- Idempotent-ish (guarded on the current type) — safe to re-run.
-- Fresh volumes get NUMERIC from db/init.sql instead; keep both in sync.
-- Apply: docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_013.sql
--
-- Why: crypto base-asset volume is fractional (e.g. 0.4213 BTC), but the
-- column was BIGINT, so upsert_price_bars' int() truncated every fraction and
-- silently stored any sub-1-unit minute as volume=0 — corrupting the volume
-- series that feeds the volume_anomaly salience rule. This is a full-table
-- rewrite under an ACCESS EXCLUSIVE lock; run it during a quiet window.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'price_bars' AND column_name = 'volume'
          AND data_type = 'bigint'
    ) THEN
        ALTER TABLE price_bars ALTER COLUMN volume TYPE NUMERIC(28, 8);
    END IF;
END $$;
