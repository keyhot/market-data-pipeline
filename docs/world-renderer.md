# World renderer (Sprint 12)

The Living World's first visual surface: one room whose contents are a pure
projection over the append-only `world_events` log.

## Data flow

```
world_events (append-only)
  -> world/state.py    project_state()   pure fold, oldest-first
                       severity_tier()   per-rule normalization
  -> world/reactions.py attach_reactions() mood/animation descriptors
  -> GET /world/state  ApiResponse envelope
  -> /world            PixiJS canvas + EventSource(/stream/world/events)
```

The renderer computes nothing. Every visual decision — mood, tier, animation,
intensity — is made in Python and covered by unit tests, because a browser
source running 24/7 is the worst possible place to debug logic.

## Why severity is tiered

Each salience rule scores in its own unit: `big_move` in sigmas (fires at 4),
`gap_open` in multiples of a 0.4% threshold (fires at 1), `streak` in bar
counts (fires at 7), `signal_resolved` in doubled confidence (caps near 2).
Observed live maxima ranged from 1.93 to 13.92. Mapping raw severity onto
visual weight would have let `big_move` drown out everything else, so
`severity_tier()` maps each rule onto a shared 0–3 scale with per-rule cut
points, and every rule's own trigger value lands on tier 0.

## Determinism

`project_state` sorts events oldest-first and folds them through `fold_event`,
which returns a new state and mutates nothing. Two properties are tested:
identical input gives byte-identical output, and folding a log in two chunks
equals folding it whole. Together these are what make "refresh restores the
same world" a guarantee rather than a hope.

## Historical backfill and truthfulness

`scripts/backfill_world_events.py` replays the *same* salience rules over
~60 days of historical Binance 1m klines. The events are real: real bars,
real deterministic rules. Every backfilled event carries `backfilled: true`
in its payload so the world can distinguish what it **witnessed** from what
it **learned**. Nothing is fabricated.

Re-runnability comes from the natural-key unique index
(`uq_world_events_natural`, `scripts/migrate_012.sql`) plus
`append_world_events_backfill`'s `ON CONFLICT DO NOTHING`. The live append
path is deliberately unchanged — its 30-minute DB-backed cooldown remains the
live dedupe, and a conflict there should surface rather than be swallowed.

The append-only contract is intact: the migration adds an index, and the
backfill only inserts.

### Downtime is also truthful, not just history

`history.outages` and `history.downtime_seconds` (folded in `world/state.py`)
follow the same rule `scripts/soak_report.py` uses for the soak report: a
`stream_dropped` event whose `payload.reason == "dropped_frames"` means the
stream **degraded while staying live** — the watchdog records it and
deliberately does not restart, so no `stream_started` follows it. Booking
that span as downtime would fabricate an outage, so `fold_event` only opens
an outage on a non-degraded drop (or an explicit `stream_stopped`) and closes
it on the next `stream_started`. A dropped-frame event still lands in
`recent` with its own reaction (`alarmed` / `flicker`) — it is visible, just
not counted as the stream being down.

## The two inhabitants

- **MODEL** — our own signal model. Its mood follows its win/loss record, and
  it is currently losing more often than winning at the time of writing. The
  room is deliberately not designed to look celebratory by default; a losing
  model is honest, and it is content.
- **TRADER** — the freqtrade dry-run sidecar, mirrored over REST into
  `trader_*` events (`world/trader_events.py`). It is an independent
  inhabitant, not an executor of our signals: when its positions disagree
  with the model's calls, that disagreement is real. If the sidecar is not
  running, the trader renders as dormant and the room is still complete.
  On its very first observation from a cold log, the trader has no prior
  state to diff against and cannot attest it witnessed the currently-open
  trades opening — so it emits `trader_opened` for each one flagged
  `baseline: true` rather than staying silent, the same "learned, not
  witnessed" honesty as a backfilled market event
  (`world/trader_events.py::diff_trader_state`). Staying silent instead
  would deadlock the mirror: with nothing ever persisted, every poll would
  keep looking like the first.

## Known limitation: history is windowed, not total

`GET /world/state` folds the newest 500 events. Every `history` field is
therefore scoped to that window: `worst_loss` means *worst loss in the last
500 events*, not worst ever. After the 60-day backfill the log is far larger
than 500 rows, so the gap is real, and it is why the page says "worst loss
**seen**" rather than "worst loss ever".

`history.window` carries the event count the projection actually saw, so a
consumer can always tell what the numbers cover.

The fix is a small set of aggregate queries over the whole table
(`min(realized_return)`, `max(bars)`, `sum` of downtime) folded in beside the
windowed projection. That is deliberately deferred rather than faked: a
number the world states about itself has to be true, and a wrong "worst loss
ever" is exactly the kind of quiet dishonesty the metrics culture here exists
to prevent.

## OBS integration

`/world` is a Browser Source at 1920×1080 with an opaque `#131722`
background. Sprint 13 adds it as the `world-focus` scene. Host gotcha: OBS on
this machine needs `BrowserHWAccel=false` in `~/.config/obs-studio/global.ini`,
or CEF's GPU process crashes and takes OBS down when a browser-source scene
loads.

The trader sidecar is fail-closed by design: `config/freqtrade/config.json`
ships with empty `exchange.key`/`exchange.secret` and an empty
`api_server.jwt_secret_key`/`password`, and `docker-compose.yml` requires
`FREQTRADE_JWT_SECRET` and `FREQTRADE_PASSWORD` via `${VAR:?required}` —
`docker compose up` refuses to start the sidecar without real secrets rather
than quietly running with working defaults. No credential the trader
inhabitant needs to function ships in the repo.
