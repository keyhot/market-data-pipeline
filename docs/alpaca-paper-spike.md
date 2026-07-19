# Spike: Alpaca paper trading for equity signals — adopt or defer?

Decision (Sprint 10, 2026-07-19): **defer**. Design-only spike (no account —
account creation is the user's action, deliberately not taken this sprint;
source: docs.alpaca.markets, fetched 2026-07-19).

## What Alpaca paper trading offers

- Separate paper environment: `https://paper-api.alpaca.markets` (key/secret
  headers; keys are distinct from live keys, generated in the dashboard).
- Simulated fills against real-time NBBO quotes; random partial fills (~10%
  of orders at ~90% size); $100k default starting balance; P&L tracked in
  their dashboard for free.
- Explicitly NOT simulated: market impact, slippage, queue position,
  dividends, regulatory/borrow fees.

## The signal→order mapping (if/when adopted)

- Consumer: a `world/paper_trader.py` scheduler job over resolved-signal
  logic: enter long when a fresh equity signal has `probability ≥ threshold`
  (market order, flat position sizing v0: fixed $ per position); exit at
  horizon expiry (or on an opposing signal). Orders via `POST /v2/orders`,
  positions via `GET /v2/positions`, P&L via `GET /v2/account`.
- Keys via `.env` only (`ALPACA_KEY_ID`/`ALPACA_SECRET`), never committed;
  compose passes them to the scheduler container. Paper endpoints only.
- Trades mirrored into `world_events` (`paper_trade` event type) so the
  world sees them — same pattern as `signal_resolved`.

## Why defer

1. **Our resolver already produces the number that matters now.** Sprint 10's
   accountability loop scores every signal against realized bars; the
   overlays run on that. Alpaca would add execution simulation — but their
   paper engine models *neither slippage nor impact*, so its realism
   advantage over our resolver-based P&L is smaller than it looks.
2. **Equity signals barely exist yet.** Only crypto models are trained;
   equities are on daily bars with market-hours-gated inference. A paper
   broker for a signal stream this thin adds ops surface without content.
3. **The account is a user action** — parked until adoption is worth it.

## Adoption trigger (re-run this spike when ANY fires)

1. Overlay P&L needs execution realism the resolver can't fake (position
   sizing, partial fills, multi-day holds).
2. An equity model is trained and producing signals on a cadence.
3. Any conversation about real money starts — paper trading is the
   mandatory gate before that conversation (roadmap standing rule).

When adopted: user creates the account + keys, then a one-sprint ticket
covers `paper_trader.py`, compose env wiring, `paper_trade` world events,
and a kill switch (`PAPER_TRADING_ENABLED`, default off).
