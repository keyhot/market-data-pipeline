-- C1 · the liquidations recorder: forced-liquidation events plus the minutes
-- the recorder can PROVE it was listening. Idempotent — safe to re-run.
-- Fresh volumes get the same tables from db/init.sql; keep both in sync.
-- Apply: docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_017.sql
--
-- Append-only by convention, like world_events: no venue serves free
-- liquidation history, so a row lost here is lost for good. The writer only
-- INSERTs ... ON CONFLICT DO NOTHING.

CREATE TABLE IF NOT EXISTS liquidations (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue       TEXT        NOT NULL,             -- 'binance' | 'okx'
    symbol      TEXT        NOT NULL,             -- venue-native (BTCUSDT, BTC-USDT-SWAP)
    event_ts    TIMESTAMPTZ NOT NULL,             -- exchange time of the fill
    side        TEXT        NOT NULL CHECK (side IN ('long', 'short')),  -- position liquidated
    price       NUMERIC     NOT NULL,
    size        NUMERIC     NOT NULL,             -- venue-native units, see size_unit
    size_unit   TEXT        NOT NULL,             -- 'base' | 'contracts'
    qty_base    NUMERIC,                          -- NULL when the contract value was unknown
    notional    NUMERIC,                          -- quote currency; NULL likewise
    source      TEXT        NOT NULL,             -- 'ws' | 'rest'
    raw         JSONB       NOT NULL,             -- the venue frame, to recompute anything
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Natural key: the WS push and the REST window describe the same OKX fill
-- with the same (instId, ts, side, bkPx, sz), so the second copy is dropped.
CREATE UNIQUE INDEX IF NOT EXISTS uq_liquidations_natural
    ON liquidations (venue, symbol, event_ts, side, price, size);
CREATE INDEX IF NOT EXISTS idx_liquidations_symbol_time
    ON liquidations (symbol, event_ts);

-- One row per venue per whole minute during which liveness proofs arrived at
-- most 30s apart (ingestion/liquidations.py CoverageTracker). An absent minute
-- was not recorded; a present one with rows_received = 0 was quiet. Analysis
-- uses covered minutes only, and the newest row's age is the liveness signal.
CREATE TABLE IF NOT EXISTS liquidation_coverage (
    venue         TEXT        NOT NULL,
    minute        TIMESTAMPTZ NOT NULL,
    rows_received INTEGER     NOT NULL,
    PRIMARY KEY (venue, minute)
);
