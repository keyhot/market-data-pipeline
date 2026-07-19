# Spike: freqtrade dry-run sidecar for crypto — adopt or defer?

Decision (Sprint 10, 2026-07-19): **defer to Sprint 12+**, with the
integration path proven hands-on and documented below.

**GPL boundary (hard rule, restated):** freqtrade is GPL-3.0. It may only
ever run as a **separate container** talked to over its REST API. Never
import it, never copy its code into this repo. Custom strategy files placed
in its `user_data` are avoided too — we keep the integration purely at the
REST boundary.

## What the hands-on spike proved (scratch setup, torn down after)

- **Keyless dry-run works.** `freqtradeorg/freqtrade:stable` +
  `dry_run: true` + empty exchange keys trades paper against live Binance
  public data — no account, no secrets. It opened its first simulated trade
  within a minute (SampleStrategy, BTC/USDT + ETH/USDT, 1m timeframe;
  dry-run wallet visibly moved: 1000 → 999.58 USDT).
- **Full state over REST** with basic auth: `/api/v1/status` (open trades),
  `/api/v1/profit` (aggregates incl. best pair, CAGR), `/api/v1/balance`,
  `/api/v1/show_config`. Everything a world-mirror needs, poll-friendly.
- Gotchas for the adoption ticket: `jwt_secret_key` must be long (short
  values are a fatal config error); the API binds inside the container so
  compose networking suffices (no published port needed in production).
- Footprint: one container, no GPU, modest CPU/memory — acceptable sidecar.

## Recommended integration (when adopted)

A **REST-mirror world inhabitant**: a small scheduler job polls the sidecar's
`/status` + `/profit` and appends `trader_*` world_events (trade opened,
closed, P&L milestones) — the autonomous trader becomes a character whose
real dry-run wins and losses are truthful content ("failure is content").
Our signals stay independent; the trader is a *separate* inhabitant with its
own personality, not an executor of our model — on-screen disagreement
between our model's calls and the trader's positions is real, free drama.

Rejected alternative: custom strategy consuming our signals inside their
container — couples us to their strategy API and muddies the GPL boundary.

## Adoption trigger

Sprint 12 (World Renderer): adopt when the world can *show* the trader —
a character needs a room before it moves in. The compose service + mirror
job is roughly one ticket of work at that point.
