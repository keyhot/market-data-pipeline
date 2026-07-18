# Sprint 9 — Model v0 + World Memory

Goal: the first trade-predictor baseline AND the Living World's memory —
salience rules turn the live bar stream into `world_events` from day one, so
the world is born with a past long before it renders (Sprint 12).
Notion sprint page `3a189097-00af-81f4-8796-d55d9e5f419c`; full ticket
descriptions live on the Notion tickets.

## Execution order (one commit per ticket, established pattern)

1. **FreqAI takeaways spike** — `docs/freqai-takeaways.md`: design rules for
   features/labels/walk-forward adopted from FreqAI's architecture. GPL-3.0:
   ideas only, zero code.
2. **Schema** — `signals` (upsert key `(symbol, interval, signal_timestamp,
   model_version)`, nullable `resolved_at`/`outcome` for Sprint 10) and
   append-only `world_events` (`occurred_at`, `event_type`, `symbol`,
   `severity`, `payload jsonb`). Both in `db/init.sql` for fresh volumes plus
   idempotent `scripts/migrate_009.sql` applied to the running volume
   (accumulating bars must survive). Store writers/readers follow
   `postgres_store` patterns.
3. **Feature pipeline** — `model/features.py`: shared by train and predict
   (no train/serve skew). Log returns, rolling vol, momentum, range, volume
   z-score; label = sign of forward N-bar return, strictly shifted.
4. **LightGBM baseline** — `model/train.py` CLI, artifact +
   `model_version` (date+sha), honest holdout vs always-up baseline.
5. **Walk-forward backtest** — `model/backtest.py`: rolling folds with purge
   gap, fees (0.1%/side default) + slippage, JSON results; leak regression
   test (shifted features must destroy performance).
6. **Signals writer** — `postgres_store.upsert_signals` + `writes.py`
   mandatory-write path with a `signals` metrics counter;
   `model/predict.py` one-shot CLI (cadence job is Sprint 10).
7. **Salience engine v0** — `world/salience.py` (vol z-score, gap, streak,
   volume anomaly, big move; injectable thresholds) + `world/events.py`
   append-only writer with DB-derived cooldown dedupe; scheduler job for
   crypto watchlist symbols behind `SALIENCE_ENABLED` (on in the scheduler
   container).
8. **Backups** — `scripts/backup_postgres.sh` (pg_dump -Fc, 14-day prune),
   host cron entry, one successful restore drill documented.
9. **Tests** — features no-lookahead property, backtest honesty + leak test,
   salience rules + cooldown, signals idempotence; integration round-trips
   auto-skip offline (existing conftest discipline).
10. **Docs + close** — README (model plane, world memory, flags, backups),
    `docs/model-plane.md` + `docs/world-memory.md`, architecture-vision
    status lines, CLAUDE.md (local), graphify update, Notion retrospective.

## Decisions carried in

Direct pandas + lightgbm, no TA-lib/vectorbt yet; boosted trees before deep
learning; costs always modeled; `world_events` append-only with no update
path in application code; salience v0 is deterministic — no LLM; truth over
vanity: a losing backtest number gets published, not hidden.

## Verification

Full suite + ruff green; fresh-volume boot creates all six tables; live
volume migrated with bar counts unchanged; `python -m model.train` +
`python -m model.predict` run end-to-end against the live stack's data;
after a live day `world_events` has real rows; backup restore drill passes.
