# Spike: TimescaleDB for intraday bars — adopt or defer?

Decision (Sprint 8, 2026-07-18): **defer**. Plain Postgres with the existing
`(symbol, bar_timestamp, interval)` primary key handles the current and
near-term load comfortably. Revisit when the adoption trigger below fires.

## Load math

The websocket ingester writes one 1m bar per crypto symbol per minute:

- 2 symbols (BTCUSDT, ETHUSDT) × 1440 bars/day ≈ **2.9k rows/day**, ~1M rows/year.
- Even at 20 symbols: ~29k rows/day, ~10M rows/year.

The schema spike (`postgres-schema-spike.md`) already concluded partitioning
is unnecessary below ~10M rows, and Sprint 7's read paths (`get_price_bars`,
`get_latest_closes`) are index-backed `ORDER BY bar_timestamp DESC LIMIT n`
lookups — exactly what the PK b-tree serves well at this scale.

## What TimescaleDB would buy, and when it matters

- **Hypertables + compression** — matters at hundreds of millions of rows or
  when disk becomes a cost concern. Not close.
- **Continuous aggregates** (1m → 5m/1h/1d rollups) — the first genuinely
  attractive feature: the chart pages and the future salience engine will
  want multi-resolution reads. At today's volume, rollups can be computed
  on-the-fly in SQL or cached in-process.
- **`time_bucket` queries** — convenient, not enabling; plain `date_trunc`
  works.

## Adoption trigger (re-run this spike when ANY fires)

1. `price_bars` exceeds **~10M rows** (check: `SELECT count(*) FROM price_bars`).
2. The watchlist grows past **~20 intraday symbols** or a tick-level
   (sub-minute) feed lands.
3. Multi-resolution rollups become a hot path (salience engine or charts
   computing 5m/1h aggregates on every request) and on-the-fly aggregation
   shows up in latency.
4. The `/bars` or dashboard queries stop being index-only lookups in
   `EXPLAIN ANALYZE`.

## Migration cost when we do adopt

Low and non-disruptive by design: TimescaleDB is a Postgres extension, so
`docker-compose.yml` swaps `postgres:16` for `timescale/timescaledb:latest-pg16`,
`create_hypertable('price_bars', 'bar_timestamp', migrate_data => true)` converts
in place, and every existing query/upsert keeps working unchanged. Nothing in
the application layer assumes vanilla Postgres.
