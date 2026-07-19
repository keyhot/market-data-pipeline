# Sprint 10 — Accountability + Live Overlays

Goal: every prediction becomes a public, resolved world event — "failure is
content" as infrastructure, not editorial — and the first OBS-ready overlay
pages ship. The losing v0 model is the content engine: its calls get
resolved, scored, and displayed truthfully. Notion sprint page
`3a189097-00af-8116-9c80-c1691356d88d`; full specs on the tickets.

Decisions: Alpaca spike design-only (user's account to create, later, if
adopted; resolver P&L carries the overlays). freqtrade dry-run spike
hands-on in a scratch compose, torn down after (GPL: separate container
only, never imported). Inference cadence via `predict: true` watchlist
field. Model artifacts volume-mounted into the scheduler container so
retraining needs no rebuild.

## Order

1. Resolver (`world/resolver.py`) — outcomes vs realized bars; the one
   sanctioned signals UPDATE; `signal_resolved` world_events, severity
   ∝ |probability − 0.5| (confident wrong calls are the story).
2. Inference cadence — `run_inference_job`, watchlist `predict: true`,
   compose artifacts mount; equity inference respects market hours.
3. Accuracy readers (`get_signal_accuracy`) + losing-streak salience rule —
   the first rule fed by the model instead of the market.
4. Alpaca design-only spike → `docs/alpaca-paper-spike.md`.
5. freqtrade dry-run spike → `docs/freqtrade-sidecar-spike.md`.
6. API: `/world/events`, `/signals/{symbol}`, `/stream/world/events`
   (SSE generator pattern; never stream infinite endpoints via TestClient).
7. `/overlay/signals` strip (1920×120) — win/loss dots, hit rate, streak;
   P&L labeled signal-based simulation.
8. `/overlay/events` feed (480×1080) — severity-colored, XSS-safe.
9. Tests (resolver combos, cadence paths, accuracy math, endpoints, SSE
   generators, integration round-trip).
10. Docs (`docs/accountability-loop.md`, README), graphify, Notion close.

## Verification

Suite + ruff + CI green; live: signals accumulate on cadence, resolver
marks outcomes within horizon+interval, `signal_resolved` events appear;
overlays render live and tick (screenshots attached); severity ordering
pinned by unit test; scheduler restart causes no double resolution.
