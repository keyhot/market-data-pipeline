# World memory (Sprint 9)

The Living World's substrate: `world_events` is an append-only log of
"moments worth attention", and the world's future state (Sprint 12's
renderer) is a projection over it. Recording started 2026-07-19 — months
before the world first renders, because history only accumulates in real
time.

## The append-only contract

**Nothing in application code may UPDATE or DELETE `world_events` rows.**
Failures, embarrassments, and mistakes accumulate on purpose — "failure is
content" and "the world remembers" (see the project Obsidian vault →
`Docs/architecture-vision.md`). The only
sanctioned write is `storage/postgres_store.append_world_events`. The
`signals` table is the single exception in the model/world storage: its
`resolved_at`/`outcome` columns are updated once by the Sprint 10 resolver.

## Salience engine v0 (`world/salience.py`)

Deterministic rules — no LLM anywhere in this path. All thresholds live in
`SalienceConfig` because personalities (Sprint 13) are policies with
different thresholds over the same rules:

| rule | fires when | severity |
|---|---|---|
| `big_move` | single-bar \|return\| ≥ 4× rolling σ (prior bar) | sigmas |
| `volatility_spike` | rolling σ ≥ 3× the σ one window ago | ratio |
| `gap_open` | \|open / prev close − 1\| ≥ 0.4% | gap ÷ threshold |
| `volume_anomaly` | volume z-score ≥ 4 vs 60-bar window | z-score |
| `streak` | ≥ 7 consecutive same-direction closes | streak length |

Severity is the salience score: higher = more notable. The overlay pages
(Sprint 10) and renderer (Sprint 12) map severity → visual weight; keep
those thresholds consistent with this table.

## Cooldown

`world/events.record_salient_events` suppresses a repeat of the same
`(event_type, symbol)` within 30 minutes. The guard reads
`latest_world_event_time()` from the database — never process memory — so
restarts cannot cause double-firing sprees. Within one batch, later
duplicates are suppressed by the same rule.

## Operations

- The scheduler runs `salience:{SYMBOL}:1m` jobs for crypto watchlist
  symbols every `interval_seconds`, behind `SALIENCE_ENABLED` (default on;
  `0`/`false`/`no` disables).
- Backups: `scripts/backup_postgres.sh` nightly via cron (03:10), 14-day
  retention, restore drill documented in the script header. The world's
  memory is the one thing this project cannot re-fetch.
- First recorded memory: `volume_anomaly ETHUSDT severity 6.43` at
  2026-07-19T01:29Z.
