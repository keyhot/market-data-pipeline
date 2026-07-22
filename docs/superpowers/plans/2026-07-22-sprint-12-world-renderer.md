# Sprint 12 — World Renderer v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Living World renders for the first time — one room, inhabitants whose mood is a deterministic projection over the append-only `world_events` log, born with ~60 days of backfilled history, restoring identically on refresh.

**Architecture:** Three pure Python layers feed one dumb canvas. `world/state.py` folds events (oldest-first, explicit accumulator) into a state dict and normalizes each rule's severity onto a shared 0–3 tier; `world/reactions.py` maps `(event_type, tier, payload)` onto mood/animation descriptors; `GET /world/state` serves the composition; `api/templates/world.html` draws it with PixiJS and applies live deltas from the existing `/stream/world/events` SSE endpoint. A backfill script replays the same salience rules over historical Binance klines so the world has a past, flagged `backfilled: true`.

**Tech Stack:** PixiJS 8.19.0 (CDN + SRI, no build step), existing FastAPI + `_render_template` string substitution, Postgres `world_events`, pandas, pytest (no network / no OBS / no Postgres in tests), freqtrade `stable` image as a keyless dry-run sidecar.

## Global Constraints

- Python 3.11, poetry-managed. **Run tests as `poetry run pytest`** — bare `pytest` resolves to the anaconda base env and is missing project deps.
- `world_events` is append-only: application code NEVER updates or deletes rows (`docs/world-memory.md`). Task 7 adds a unique *index*, which constrains inserts without mutating any row.
- Tests must not touch Postgres, the network, OBS, or freqtrade. Inject clients as arguments or `patch` the module-level store reference. `tests/conftest.py` forces `POSTGRES_WRITE_ENABLED=0` repo-wide.
- Commit style: `Type: short description`. **NEVER add a `Co-Authored-By` line.**
- `ruff check .` clean before every commit; line length 88; imports sorted.
- Run `graphify update .` after code changes (AST-only, no API cost).
- No Jinja2 in this repo. HTML renders through `_render_template(name, replacements)` (`api/main.py:108`) doing `__PLACEHOLDER__` string replacement.
- No `StaticFiles` mount and no `static/` directory exist. Third-party JS loads from a **pinned CDN URL with an SRI `integrity` hash and `crossorigin="anonymous"`**, exactly like `chart.html:31-33`.
- **Truthfulness invariant:** visual exaggeration is fine, fabricated data never. Backfilled events are real bars through real rules, flagged so the world can distinguish what it *witnessed* from what it *learned*.
- Overlay/page visual convention: opaque `#131722` background, `color-scheme: dark`, inline `<style>`, `overflow: hidden`.
- Payload-derived strings go into `textContent`, never `innerHTML` (carried from `overlay_events.html:74`).

## Decisions taken before implementation

**Renderer = PixiJS 8.19.0.** Scored against the sprint's own criteria: the room is 2D, text carries most of v0's meaning, and it runs 30fps for 24/7 in an OBS Browser Source. Three.js is a 3D scene graph whose text rendering is painful and whose GPU cost is higher for what is fundamentally 2D compositing. Phaser was already ruled out. Task 1 records the decision; it is not left open.

**No renderer abstraction layer.** The `state dict → canvas` boundary already isolates PixiJS to `world.html`'s inline script. Adding a swap-layer would be YAGNI.

**Severity tiers are per-rule cut points**, because each rule's severity is in its own unit (see the table in Task 2). Observed maxima on live data: `big_move` 13.92, `streak` 11.0, `volume_anomaly` 7.23, `gap_open` 2.18, `model_losing_streak` 2.0, `signal_resolved` 1.93.

**Task severability.** Tasks 1–7 and 10 are the shippable spine. Tasks 8–9 (freqtrade sidecar + trader character) are cleanly severable if the sprint runs long — the renderer is this sprint's identity, the second inhabitant is not. `world.html` renders the trader slot as dormant when no `trader_*` events exist, so cutting 8–9 leaves no hole.

**Verified precondition (2026-07-22):** `SELECT count(*), count(DISTINCT (event_type, symbol, occurred_at)) FROM world_events` returns `785 | 785` — zero duplicate natural keys, so Task 7's unique index applies cleanly to the live volume. Task 7 re-checks anyway, because rows accumulate between now and execution.

---

### Task 1: Renderer stack decision doc

**Files:**
- Create: `docs/world-renderer-stack-decision.md`

**Interfaces:**
- Produces: the pinned CDN URL and SRI hash that Task 5 embeds verbatim —
  `https://unpkg.com/pixi.js@8.19.0/dist/pixi.min.js`,
  `sha384-brfu63ZHzOfumoqQXzA4Wo7k9kQOaJ68C/E7+Uc8lgQB42dOOjA+urOyy/sOnKPq`.

- [ ] **Step 1: Re-verify the SRI hash against the live CDN**

Run:
```bash
curl -sfL "https://unpkg.com/pixi.js@8.19.0/dist/pixi.min.js" \
  | openssl dgst -sha384 -binary | openssl base64 -A; echo
```
Expected: `brfu63ZHzOfumoqQXzA4Wo7k9kQOaJ68C/E7+Uc8lgQB42dOOjA+urOyy/sOnKPq`

If it differs, unpkg re-published the artifact — use the newly computed value everywhere in Task 5 and record it here instead. Never hand-write an SRI hash.

- [ ] **Step 2: Write `docs/world-renderer-stack-decision.md`**

```markdown
# World renderer stack decision

**Decision: PixiJS 8.19.0, loaded from a pinned CDN URL with an SRI hash.**

## Why

The world room is 2D: a floor line, two character sprites built from
procedural geometry, per-symbol mood pillars, and text labels carrying the
numbers. PixiJS is a 2D WebGL compositor built for exactly that shape.

| Criterion | PixiJS 8.19.0 | Three.js r17x |
|---|---|---|
| Dimensionality | 2D-native; scene is a display list | 3D scene graph; 2D means an orthographic camera and manual layout |
| Text quality | First-class `Text`/`BitmapText`, crisp at fixed DPR | Needs `TextGeometry` (font loading) or DOM/CSS overlays; both are awkward |
| CDN bundle (minified) | ~780 KB, single UMD `PIXI` global | Comparable, but a usable 2D setup needs extra loaders |
| 24/7 OBS CPU cost | Lower — no lighting, no depth pass, no per-frame matrix work we don't need | Higher for identical output |
| Sprint 14 headroom | Many small interacting sprites is Pixi's core case | Would be fighting the abstraction |
| Licence | MIT | MIT |

Both are MIT, so no attribution footer is required (unlike TradingView on
`chart.html`).

## Alternatives considered

- **Three.js** — rejected above. Revisit only if the world becomes genuinely
  volumetric.
- **Phaser** — ruled out at sprint planning: it is a game framework (physics,
  input, state machines) and we need a renderer.
- **Plain Canvas 2D** — viable for v0 and zero-dependency, but Sprint 14's
  "many small interacting systems" would mean writing a display list, a
  transform stack, and batching by hand. Deferred rather than dismissed: if
  PixiJS ever proves too heavy for the OBS source, this is the fallback.

## Constraints this inherits

- **No build step and no static-asset pipeline.** The repo has no
  `StaticFiles` mount; third-party JS loads from a pinned CDN URL with
  `integrity` + `crossorigin="anonymous"`, matching `chart.html`.
- **Offline behaviour:** if the CDN is unreachable, the page renders its
  static HTML shell and the status line reads "renderer unavailable". The
  stream degrades to a readable text state rather than a blank canvas.
- **Procedural geometry only in v0** — shapes, lines, and text, no external
  sprite or texture assets. Art becomes a later addition behind the same
  projection and reaction layers.

## How it's wired

`GET /world/state` returns the projection; `api/templates/world.html` draws it
and applies deltas from `EventSource('/stream/world/events')`. The renderer
never computes state — it only draws what `world/state.py` and
`world/reactions.py` produced.
```

- [ ] **Step 3: Commit**

```bash
git add docs/world-renderer-stack-decision.md
git commit -m "Docs: world renderer stack decision — PixiJS over Three.js"
```

---

### Task 2: `world/state.py` — severity tiers + deterministic projection

**Files:**
- Create: `world/state.py`
- Test: `tests/unit/test_world_state.py`

**Interfaces:**
- Produces:
  - `severity_tier(event_type: str, severity: float) -> int` — 0..3.
  - `empty_state() -> dict`
  - `fold_event(state: dict, event: dict) -> dict` — pure, returns a new dict; expects one `get_world_events`-shaped row.
  - `project_state(events: list[dict], now: datetime | None = None) -> dict` — sorts oldest-first, reduces `fold_event`, finalizes derived fields.
  - Constants `TIER_NAMES = ("routine", "notable", "major", "dramatic")`, `RECENT_LIMIT = 12`.
- Consumes: nothing. No DB access anywhere in this module — callers supply events.

Severity semantics read from `world/salience.py`, `world/resolver.py`, and `world/stream_events.py`:

| event_type | severity means | fires at |
|---|---|---|
| `big_move` | return / rolling σ | ≥ 4.0 |
| `volatility_spike` | σ_now / σ_prior_window | ≥ 3.0 |
| `gap_open` | gap ÷ 0.004 threshold | ≥ 1.0 |
| `volume_anomaly` | volume z-score | ≥ 4.0 |
| `streak` | consecutive same-direction bars | ≥ 7 |
| `signal_resolved` | (p − 0.5) × 2, ×2 again on a loss | 0 .. 2.0 |
| `model_losing_streak` | streak ÷ 3 | ≥ 1.0 |
| `stream_started` / `stream_stopped` / `stream_dropped` | fixed 1.0 / 2.0 / 5.0 | — |

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_world_state.py`:

```python
"""The projection is the world's single source of truth for what the room
shows. Two properties matter more than any individual field: identical input
gives byte-identical output, and folding a log in chunks equals folding it
whole — that is what makes "refresh restores the same world" true."""

import json
from datetime import datetime, timedelta, timezone
from functools import reduce

import pytest

from world.state import (
    RECENT_LIMIT,
    empty_state,
    fold_event,
    project_state,
    severity_tier,
)

BASE = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _event(event_id, event_type, severity, payload=None, symbol="BTCUSDT", minute=0):
    return {
        "id": event_id,
        "occurred_at": (BASE + timedelta(minutes=minute)).isoformat(),
        "event_type": event_type,
        "symbol": symbol,
        "severity": severity,
        "payload": payload or {},
    }


@pytest.mark.parametrize(
    "event_type,severity,expected",
    [
        # Each rule's own trigger value must land on tier 0, or the room shows
        # "dramatic" for every routine firing.
        ("big_move", 4.0, 0),
        ("big_move", 13.92, 3),
        ("volatility_spike", 3.0, 0),
        ("gap_open", 1.0, 0),
        ("gap_open", 2.18, 1),
        ("volume_anomaly", 4.0, 0),
        ("volume_anomaly", 7.23, 2),
        ("streak", 7.0, 0),
        ("streak", 11.0, 1),
        ("signal_resolved", 0.2, 0),
        ("signal_resolved", 1.93, 3),
        ("model_losing_streak", 1.0, 0),
        ("stream_started", 1.0, 0),
        ("stream_stopped", 2.0, 1),
        ("stream_dropped", 5.0, 3),
    ],
)
def test_severity_tier_is_comparable_across_rules(event_type, severity, expected):
    assert severity_tier(event_type, severity) == expected


def test_severity_tier_unknown_type_uses_generic_scale():
    # A type with no cut points of its own (including trader_* in Task 9) falls
    # back rather than raising — an unrendered event is worse than a rough tier.
    assert severity_tier("unregistered_rule", 0.5) == 0
    assert severity_tier("unregistered_rule", 11.0) == 3


def test_tier_is_monotonic_in_severity():
    tiers = [severity_tier("big_move", s) for s in (4.0, 6.0, 8.0, 12.0)]
    assert tiers == sorted(tiers)


def test_projection_is_deterministic():
    events = [_event(2, "big_move", 6.0, {"return": 0.03}, minute=1),
              _event(1, "streak", 9.0, {"bars": 9, "direction": "up"})]
    first = project_state(events, now=BASE)
    second = project_state(events, now=BASE)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_projection_ignores_input_ordering():
    events = [_event(1, "big_move", 6.0, {"return": 0.03}),
              _event(2, "streak", 9.0, {"bars": 9, "direction": "up"}, minute=1)]
    forward = project_state(events, now=BASE)
    backward = project_state(list(reversed(events)), now=BASE)
    assert forward == backward


def test_folding_in_halves_equals_folding_whole():
    events = [
        _event(i, "big_move", 5.0 + i * 0.1, {"return": 0.01 * (-1) ** i}, minute=i)
        for i in range(1, 11)
    ]
    whole = reduce(fold_event, events, empty_state())
    halves = reduce(fold_event, events[5:], reduce(fold_event, events[:5], empty_state()))
    assert whole == halves


def test_model_record_counts_wins_and_losses():
    events = [
        _event(1, "signal_resolved", 0.8, {"outcome": "win", "realized_return": 0.01}),
        _event(2, "signal_resolved", 1.2, {"outcome": "loss", "realized_return": -0.02},
               minute=1),
        _event(3, "signal_resolved", 1.4, {"outcome": "loss", "realized_return": -0.03},
               minute=2),
    ]
    model = project_state(events, now=BASE)["model"]
    assert model["wins"] == 1
    assert model["losses"] == 2
    assert model["hit_rate"] == pytest.approx(1 / 3)
    assert model["current_streak"] == 2
    assert model["streak_outcome"] == "loss"


def test_stream_state_tracks_last_lifecycle_event():
    events = [
        _event(1, "stream_started", 1.0, symbol=None),
        _event(2, "stream_dropped", 5.0, symbol=None, minute=5),
    ]
    stream = project_state(events, now=BASE)["stream"]
    assert stream["state"] == "down"
    assert stream["drops"] == 1


def test_symbol_mood_reflects_direction_and_agitation():
    up = project_state(
        [_event(1, "streak", 10.0, {"bars": 10, "direction": "up"})], now=BASE
    )["symbols"]["BTCUSDT"]
    down = project_state(
        [_event(1, "streak", 10.0, {"bars": 10, "direction": "down"})], now=BASE
    )["symbols"]["BTCUSDT"]
    assert up["pressure"] > 0 > down["pressure"]
    assert up["mood"] == "bullish"
    assert down["mood"] == "bearish"


def test_recent_events_are_newest_first_and_capped():
    events = [_event(i, "big_move", 5.0, {"return": 0.01}, minute=i) for i in range(30)]
    recent = project_state(events, now=BASE)["recent"]
    assert len(recent) == RECENT_LIMIT
    assert recent[0]["id"] == 29
    assert all("tier" in e for e in recent)


def test_empty_log_projects_an_empty_but_valid_state():
    state = project_state([], now=BASE)
    assert state["event_count"] == 0
    assert state["symbols"] == {}
    assert state["model"]["resolved"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/unit/test_world_state.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'world.state'`

- [ ] **Step 3: Write `world/state.py`**

```python
"""World-state projection (Sprint 12): the append-only world_events log
folded into what the room currently looks like. Pure — no database access,
no clock of its own — because "refresh restores the same world" is only true
if the same log always produces the same state.

Severity normalization lives here rather than in the canvas: every salience
rule scores in its own unit (sigmas, z-scores, bar counts, multiples of a
threshold), so a renderer mapping raw severity to visual weight would be
permanently dominated by big_move.
"""

from datetime import datetime, timezone

TIER_NAMES = ("routine", "notable", "major", "dramatic")
RECENT_LIMIT = 12

# Per-rule cut points onto tiers 0..3. Each rule's own trigger threshold must
# land on tier 0 — a rule firing at its minimum is by definition routine.
_TIER_CUTS: dict[str, tuple[float, float, float]] = {
    "big_move": (5.0, 7.0, 10.0),            # sigmas; fires at 4.0
    "volatility_spike": (4.0, 6.0, 9.0),     # sigma ratio; fires at 3.0
    "gap_open": (1.5, 2.5, 4.0),             # multiples of the gap threshold
    "volume_anomaly": (5.0, 7.0, 10.0),      # volume z-score; fires at 4.0
    "streak": (9.0, 12.0, 15.0),             # consecutive bars; fires at 7
    "signal_resolved": (0.6, 1.2, 1.8),      # confidence, doubled on a loss
    "model_losing_streak": (1.34, 2.0, 3.0), # streak/3: 4 losses, 6, 9
    "stream_started": (2.0, 3.0, 4.0),
    "stream_stopped": (2.0, 3.0, 4.0),
    "stream_dropped": (2.0, 3.0, 4.0),
}
_GENERIC_CUTS = (2.0, 5.0, 10.0)

# Pressure/agitation decay per event, so the room reflects the recent past
# without a clock. Chosen so ~7 quiet events halve an impression.
_DECAY = 0.9
_DIRECTIONLESS = frozenset({"volatility_spike", "volume_anomaly", "gap_open"})


def severity_tier(event_type: str, severity: float) -> int:
    """Map a rule-specific severity onto the shared 0..3 scale."""
    cuts = _TIER_CUTS.get(event_type, _GENERIC_CUTS)
    return sum(1 for cut in cuts if severity >= cut)


def empty_state() -> dict:
    return {
        "event_count": 0,
        "symbols": {},
        "model": {
            "wins": 0,
            "losses": 0,
            "resolved": 0,
            "hit_rate": None,
            "current_streak": 0,
            "streak_outcome": None,
        },
        "stream": {"state": "unknown", "drops": 0, "last_transition": None},
        "recent": [],
    }


def _empty_symbol() -> dict:
    return {
        "pressure": 0.0,
        "agitation": 0.0,
        "mood": "calm",
        "tier": 0,
        "event_counts": {},
        "last_event": None,
    }


def _pressure_delta(event_type: str, tier: int, payload: dict) -> float:
    weight = float(tier + 1)
    if event_type == "streak":
        return weight if payload.get("direction") == "up" else -weight
    if event_type == "big_move":
        return weight if float(payload.get("return", 0.0)) >= 0 else -weight
    return 0.0


def _mood(pressure: float, agitation: float) -> str:
    if agitation >= 6.0:
        return "panicked"
    if pressure >= 1.5:
        return "bullish"
    if pressure <= -1.5:
        return "bearish"
    return "calm"


def fold_event(state: dict, event: dict) -> dict:
    """Absorb one event. Pure: returns a new state, mutates nothing."""
    new = {
        "event_count": state["event_count"] + 1,
        "symbols": dict(state["symbols"]),
        "model": dict(state["model"]),
        "stream": dict(state["stream"]),
        "recent": state["recent"],
    }
    etype = event["event_type"]
    payload = event.get("payload") or {}
    tier = severity_tier(etype, float(event["severity"]))

    symbol = event.get("symbol")
    if symbol:
        prev = new["symbols"].get(symbol, _empty_symbol())
        pressure = prev["pressure"] * _DECAY + _pressure_delta(etype, tier, payload)
        agitation = prev["agitation"] * _DECAY + (
            float(tier + 1) if etype in _DIRECTIONLESS or etype == "big_move" else 0.0
        )
        counts = dict(prev["event_counts"])
        counts[etype] = counts.get(etype, 0) + 1
        new["symbols"][symbol] = {
            "pressure": round(pressure, 4),
            "agitation": round(agitation, 4),
            "mood": _mood(pressure, agitation),
            "tier": tier,
            "event_counts": counts,
            "last_event": {
                "event_type": etype,
                "occurred_at": event["occurred_at"],
                "tier": tier,
            },
        }

    if etype == "signal_resolved":
        outcome = payload.get("outcome")
        model = new["model"]
        model["resolved"] += 1
        if outcome == "win":
            model["wins"] += 1
        elif outcome == "loss":
            model["losses"] += 1
        if outcome in ("win", "loss"):
            if model["streak_outcome"] == outcome:
                model["current_streak"] += 1
            else:
                model["streak_outcome"] = outcome
                model["current_streak"] = 1

    if etype in ("stream_started", "stream_stopped", "stream_dropped"):
        stream = new["stream"]
        stream["state"] = "live" if etype == "stream_started" else "down"
        stream["last_transition"] = event["occurred_at"]
        if etype == "stream_dropped":
            stream["drops"] += 1

    # Newest-first, capped. Slicing a fresh list keeps fold_event pure.
    entry = {
        "id": event.get("id"),
        "occurred_at": event["occurred_at"],
        "event_type": etype,
        "symbol": symbol,
        "severity": float(event["severity"]),
        "tier": tier,
        "tier_name": TIER_NAMES[tier],
        "payload": payload,
    }
    new["recent"] = [entry, *state["recent"]][:RECENT_LIMIT]
    return new


def project_state(events: list[dict], now: datetime | None = None) -> dict:
    """Fold a world_events page (any order) into current world state."""
    ordered = sorted(events, key=lambda e: (e["occurred_at"], e.get("id") or 0))
    state = empty_state()
    for event in ordered:
        state = fold_event(state, event)

    resolved = state["model"]["resolved"]
    state["model"]["hit_rate"] = (
        state["model"]["wins"] / resolved if resolved else None
    )
    state["generated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    return state
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/unit/test_world_state.py -q`
Expected: `25 passed` (15 from the tier parametrize + 10 others)

- [ ] **Step 5: Mutation-check the fold purity**

Temporarily change `new["recent"] = [entry, *state["recent"]][:RECENT_LIMIT]` to
`state["recent"].insert(0, entry); new["recent"] = state["recent"][:RECENT_LIMIT]`.

Run: `poetry run pytest tests/unit/test_world_state.py -q`
Expected: `test_folding_in_halves_equals_folding_whole` FAILS (the shared list leaks between accumulators). **Revert the mutation.** If it passes, the determinism tests are not actually checking purity — fix them before continuing.

- [ ] **Step 6: Lint and commit**

```bash
ruff check world/state.py tests/unit/test_world_state.py
git add world/state.py tests/unit/test_world_state.py
git commit -m "World: deterministic state projection with per-rule severity tiers"
```

---

### Task 3: `world/reactions.py` — event → mood/animation descriptors

**Files:**
- Create: `world/reactions.py`
- Test: `tests/unit/test_reactions.py`

**Interfaces:**
- Consumes: `world.salience.KNOWN_EVENT_TYPES`; tier ints from Task 2 (passed in — this module does **not** import `world.state`, keeping the dependency acyclic).
- Produces:
  - `reaction_for(event_type: str, tier: int, payload: dict | None = None) -> dict` returning `{"mood", "animation", "intensity", "duration_ms"}`.
  - `attach_reactions(state: dict) -> dict` — returns a new state whose `recent` entries each carry a `reaction`, and whose `model`/`symbols` carry a `reaction` for their current condition.

`payload` is optional because two event types are genuinely bidirectional: `signal_resolved` (win vs loss) and `streak` (up vs down). Everything else ignores it.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_reactions.py`:

```python
"""Reactions are the only place event semantics become visuals. The registry
invariant is the point: adding a salience rule without a reaction must fail a
test rather than silently render nothing on a 24/7 stream."""

import pytest

from world.reactions import REACTIONS, attach_reactions, reaction_for
from world.salience import KNOWN_EVENT_TYPES


def test_every_known_event_type_has_a_reaction():
    missing = sorted(KNOWN_EVENT_TYPES - set(REACTIONS))
    assert missing == [], f"event types with no reaction: {missing}"


def test_unknown_event_type_falls_back_instead_of_raising():
    reaction = reaction_for("some_future_rule", 2)
    assert reaction["mood"] == "neutral"
    assert reaction["animation"] == "idle"


def test_intensity_and_duration_scale_with_tier():
    low = reaction_for("big_move", 0)
    high = reaction_for("big_move", 3)
    assert high["intensity"] > low["intensity"]
    assert high["duration_ms"] > low["duration_ms"]


def test_signal_resolved_splits_on_outcome():
    win = reaction_for("signal_resolved", 2, {"outcome": "win"})
    loss = reaction_for("signal_resolved", 2, {"outcome": "loss"})
    assert win["mood"] != loss["mood"]
    assert win["mood"] == "elated"
    assert loss["mood"] == "dejected"


def test_streak_splits_on_direction():
    up = reaction_for("streak", 1, {"direction": "up"})
    down = reaction_for("streak", 1, {"direction": "down"})
    assert up["mood"] == "eager"
    assert down["mood"] == "grim"


def test_missing_payload_never_raises():
    for event_type in sorted(KNOWN_EVENT_TYPES):
        assert reaction_for(event_type, 0) is not None


@pytest.mark.parametrize("tier", [0, 1, 2, 3])
def test_intensity_is_bounded(tier):
    assert 0.0 < reaction_for("big_move", tier)["intensity"] <= 1.0


def test_attach_reactions_enriches_recent_without_mutating_input():
    state = {
        "recent": [
            {
                "id": 1,
                "event_type": "signal_resolved",
                "tier": 3,
                "payload": {"outcome": "loss"},
            }
        ],
        "symbols": {"BTCUSDT": {"mood": "bearish", "tier": 2}},
        "model": {"streak_outcome": "loss", "current_streak": 4},
    }
    enriched = attach_reactions(state)
    assert enriched["recent"][0]["reaction"]["mood"] == "dejected"
    assert "reaction" not in state["recent"][0]
    assert enriched["model"]["reaction"]["animation"] == "slump"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/unit/test_reactions.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'world.reactions'`

- [ ] **Step 3: Write `world/reactions.py`**

```python
"""Character reactions v0 (Sprint 12): the mapping from "what happened" to
"what the room shows". Deliberately a data table, not logic — Sprint 13's
personalities differ by *thresholds* over these same reactions, so the
descriptors stay neutral about who is reacting.

The registry invariant (every KNOWN_EVENT_TYPES member has an entry) is
enforced by test, mirroring the subset check in tests/unit/test_stream_events.py.
"""

_FALLBACK = ("neutral", "idle")

# event_type -> (mood, animation)
REACTIONS: dict[str, tuple[str, str]] = {
    "big_move": ("startled", "jolt"),
    "volatility_spike": ("anxious", "shake"),
    "gap_open": ("surprised", "hop"),
    "volume_anomaly": ("alert", "pulse"),
    "streak": ("focused", "lean"),
    "signal_resolved": ("resigned", "shrug"),
    "model_losing_streak": ("dejected", "slump"),
    "stream_started": ("relieved", "wave"),
    "stream_stopped": ("idle", "sleep"),
    "stream_dropped": ("alarmed", "flicker"),
}

# Types whose meaning genuinely flips on payload content.
_SIGNAL_OUTCOMES = {
    "win": ("elated", "cheer"),
    "loss": ("dejected", "slump"),
}
_STREAK_DIRECTIONS = {
    "up": ("eager", "lean"),
    "down": ("grim", "lean"),
}


def reaction_for(event_type: str, tier: int, payload: dict | None = None) -> dict:
    """Mood/animation descriptor for one event at one severity tier."""
    payload = payload or {}
    mood, animation = REACTIONS.get(event_type, _FALLBACK)

    if event_type == "signal_resolved":
        mood, animation = _SIGNAL_OUTCOMES.get(payload.get("outcome"), (mood, animation))
    elif event_type == "streak":
        mood, animation = _STREAK_DIRECTIONS.get(
            payload.get("direction"), (mood, animation)
        )

    tier = max(0, min(int(tier), 3))
    return {
        "mood": mood,
        "animation": animation,
        "intensity": round((tier + 1) / 4, 2),
        "duration_ms": 800 + 400 * tier,
    }


def attach_reactions(state: dict) -> dict:
    """Enrich a projected state with reaction descriptors. Returns a new dict;
    the canvas should never have to compute any of this itself."""
    enriched = dict(state)
    enriched["recent"] = [
        {
            **event,
            "reaction": reaction_for(
                event["event_type"], event.get("tier", 0), event.get("payload")
            ),
        }
        for event in state.get("recent", [])
    ]

    model = dict(state.get("model", {}))
    if model:
        outcome = model.get("streak_outcome")
        model["reaction"] = reaction_for(
            "signal_resolved",
            min(model.get("current_streak", 0), 3),
            {"outcome": outcome} if outcome else None,
        )
    enriched["model"] = model

    enriched["symbols"] = {
        symbol: {**data, "reaction": reaction_for("streak", data.get("tier", 0),
                                                  {"direction": _direction(data)})}
        for symbol, data in state.get("symbols", {}).items()
    }
    return enriched


def _direction(symbol_state: dict) -> str:
    return "up" if symbol_state.get("pressure", 0.0) >= 0 else "down"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/unit/test_reactions.py -q`
Expected: `11 passed`

- [ ] **Step 5: Prove the registry invariant actually bites**

Temporarily delete the `"gap_open"` line from `REACTIONS`.

Run: `poetry run pytest tests/unit/test_reactions.py::test_every_known_event_type_has_a_reaction -q`
Expected: FAIL with `event types with no reaction: ['gap_open']`. **Restore the line.**

- [ ] **Step 6: Lint and commit**

```bash
ruff check world/reactions.py tests/unit/test_reactions.py
git add world/reactions.py tests/unit/test_reactions.py
git commit -m "World: character reactions v0 with registry invariant"
```

---

### Task 4: `GET /world/state` endpoint

**Files:**
- Modify: `api/main.py` (imports near line 39/48; new route after the `/world/events` handler ending at line 425)
- Test: `tests/api/test_world_state_endpoint.py`

**Interfaces:**
- Consumes: `project_state`, `attach_reactions`, existing `get_world_events`.
- Produces: `GET /world/state?limit=N` → `ApiResponse` with `data` = the enriched state. Task 5's page fetches this.

Error shape copied exactly from `world_events` (`api/main.py:410-422`): re-raise `BaseAppException`, wrap anything else as 503, `NoDataFoundError` on empty.

- [ ] **Step 1: Write the failing tests** — `tests/api/test_world_state_endpoint.py`:

```python
"""Endpoint tests patch api.main.get_world_events — the projection itself is
covered by unit tests, so these assert wiring and the established error shape."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def _event(event_id, event_type="big_move", severity=6.0, payload=None):
    return {
        "id": event_id,
        "occurred_at": f"2026-07-20T12:{event_id:02d}:00+00:00",
        "event_type": event_type,
        "symbol": "BTCUSDT",
        "severity": severity,
        "payload": payload if payload is not None else {"return": 0.03},
    }


def test_world_state_returns_projection():
    with patch("api.main.get_world_events", return_value=[_event(1)]):
        response = client.get("/world/state")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["event_count"] == 1
    assert "BTCUSDT" in data["symbols"]
    assert data["recent"][0]["reaction"]["animation"] == "jolt"


def test_world_state_includes_model_record():
    events = [
        _event(1, "signal_resolved", 0.8, {"outcome": "win"}),
        _event(2, "signal_resolved", 1.4, {"outcome": "loss"}),
    ]
    with patch("api.main.get_world_events", return_value=events):
        data = client.get("/world/state").json()["data"]

    assert data["model"]["wins"] == 1
    assert data["model"]["losses"] == 1


def test_world_state_404_when_empty():
    with patch("api.main.get_world_events", return_value=[]):
        response = client.get("/world/state")

    assert response.status_code == 404


def test_world_state_503_when_postgres_down():
    with patch("api.main.get_world_events", side_effect=RuntimeError("no pool")):
        response = client.get("/world/state")

    assert response.status_code == 503


def test_world_state_rejects_out_of_range_limit():
    assert client.get("/world/state?limit=0").status_code == 422
    assert client.get("/world/state?limit=99999").status_code == 422


def test_world_state_is_stable_across_identical_calls():
    with patch("api.main.get_world_events", return_value=[_event(1), _event(2)]):
        first = client.get("/world/state").json()["data"]
        second = client.get("/world/state").json()["data"]

    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/api/test_world_state_endpoint.py -q`
Expected: FAIL — `assert 404 == 200` on the first test (FastAPI has no `/world/state` route yet).

- [ ] **Step 3: Add the imports to `api/main.py`**

Next to the existing `from world.salience import KNOWN_EVENT_TYPES` (line 48):

```python
from world.reactions import attach_reactions
from world.state import project_state
```

- [ ] **Step 4: Add the route to `api/main.py`**, immediately after the `world_events` handler (which ends at line 425):

```python
@app.get("/world/state", response_model=ApiResponse)
def world_state(limit: int = Query(500, ge=1, le=2000)):
    """The world_events log folded into what the room currently shows.
    The renderer computes nothing — it draws this."""
    try:
        events = get_world_events(limit=limit)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)
    if not events:
        raise NoDataFoundError("No world events recorded yet")
    return ApiResponse(status=200, data=attach_reactions(project_state(events)))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `poetry run pytest tests/api/test_world_state_endpoint.py -q`
Expected: `6 passed`

- [ ] **Step 6: Run the whole API suite for regressions**

Run: `poetry run pytest tests/api/ -q`
Expected: all pass (no existing route or import broken).

- [ ] **Step 7: Lint and commit**

```bash
ruff check api/main.py tests/api/test_world_state_endpoint.py
git add api/main.py tests/api/test_world_state_endpoint.py
git commit -m "API: /world/state serving the world projection"
```

---

### Task 5: `/world` page — PixiJS room driven by state + SSE

**Files:**
- Create: `api/templates/world.html`
- Modify: `api/main.py` (route beside `/overlay/events`, line 504)
- Test: `tests/api/test_world_page.py`

**Interfaces:**
- Consumes: `GET /world/state`, `GET /stream/world/events` (SSE, already exists and already drives both overlays), `load_watchlist()`.
- Produces: `GET /world` returning HTML. Sprint 13 adds this page as a Browser Source in the `world-focus` scene.

**Do not drive `/stream/world/events` through TestClient** — the infinite SSE generator hangs on close (see `tests/api/test_stream_endpoint.py`). Page tests assert the route, the substitutions, and the security properties only.

- [ ] **Step 1: Write the failing tests** — `tests/api/test_world_page.py`:

```python
"""The canvas itself isn't unit-testable, so these tests pin the things that
silently break a 24/7 browser source: substitution, the SRI pin, and the
textContent-only rule that keeps event payloads from becoming markup."""

import re

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_world_page_renders():
    response = client.get("/world")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_no_unsubstituted_placeholders_remain():
    body = client.get("/world").text
    assert "__SYMBOLS__" not in body
    assert not re.search(r"__[A-Z_]+__", body)


def test_watchlist_symbols_are_embedded():
    body = client.get("/world").text
    assert "BTCUSDT" in body


def test_pixi_is_pinned_with_integrity():
    body = client.get("/world").text
    assert 'pixi.js@8.19.0' in body
    assert 'integrity="sha384-' in body
    assert 'crossorigin="anonymous"' in body


def test_page_uses_textcontent_not_innerhtml():
    body = client.get("/world").text
    assert "innerHTML" not in body


def test_page_consumes_state_and_sse_endpoints():
    body = client.get("/world").text
    assert "/world/state" in body
    assert "/stream/world/events" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/api/test_world_page.py -q`
Expected: FAIL — `assert 404 == 200`.

- [ ] **Step 3: Create `api/templates/world.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>The World — Market Data Pipeline</title>
  <style>
    :root { color-scheme: dark; --bg: #131722; }
    body { margin: 0; background: var(--bg); color: #d1d4dc;
           font-family: system-ui, sans-serif; overflow: hidden; }
    #room { width: 100vw; height: 100vh; }
    #fallback { position: absolute; top: 1rem; left: 1rem; color: #787b86;
                font-size: 0.85rem; }
  </style>
</head>
<body>
  <div id="room"></div>
  <div id="fallback">Waking the world…</div>
  <script src="https://unpkg.com/pixi.js@8.19.0/dist/pixi.min.js"
          integrity="sha384-brfu63ZHzOfumoqQXzA4Wo7k9kQOaJ68C/E7+Uc8lgQB42dOOjA+urOyy/sOnKPq"
          crossorigin="anonymous"></script>
  <script>
    const SYMBOLS = __SYMBOLS__;
    const fallback = document.getElementById("fallback");

    // Moods the projection can emit -> room palette. Unknown moods stay grey
    // rather than throwing, so a new salience rule degrades quietly on screen
    // (the loud failure is the registry test in tests/unit/test_reactions.py).
    const MOOD_COLOR = {
      bullish: 0x26a69a, bearish: 0xef5350, panicked: 0xf2a900, calm: 0x4a90d9,
      elated: 0x26a69a, dejected: 0xef5350, startled: 0xf2a900, anxious: 0xf2a900,
      surprised: 0x9c6ade, alert: 0x4a90d9, focused: 0x5b9cf6, eager: 0x26a69a,
      grim: 0xef5350, relieved: 0x26a69a, alarmed: 0xef5350, idle: 0x545862,
      resigned: 0x787b86, neutral: 0x787b86,
    };
    const color = (mood) => MOOD_COLOR[mood] ?? 0x787b86;

    // Declared here, constructed in boot(): character() needs app.screen and
    // the layer containers, neither of which exist until PIXI has initialised.
    let app, layers = {}, world = null, model = null, trader = null;

    async function boot() {
      if (typeof PIXI === "undefined") {
        fallback.textContent = "renderer unavailable — the world is still running";
        return;
      }
      app = new PIXI.Application();
      await app.init({ background: 0x131722, resizeTo: window, antialias: true });
      document.getElementById("room").appendChild(app.canvas);
      fallback.textContent = "";

      layers.room = new PIXI.Container();
      layers.chars = new PIXI.Container();
      layers.props = new PIXI.Container();
      app.stage.addChild(layers.room, layers.props, layers.chars);

      drawRoom();
      model = character("MODEL");
      trader = character("TRADER");
      await refresh();
      subscribe();
      setInterval(refresh, 60000);   // projection stays authoritative server-side
    }

    function label(text, x, y, size, tint) {
      const t = new PIXI.Text({ text, style: {
        fill: tint ?? 0xd1d4dc, fontFamily: "system-ui", fontSize: size,
      }});
      t.x = x; t.y = y;
      return t;
    }

    function drawRoom() {
      const floor = new PIXI.Graphics()
        .rect(0, app.screen.height * 0.72, app.screen.width, 3)
        .fill(0x2a2e39);
      layers.room.addChild(floor);
    }

    // One inhabitant: a body whose colour is its mood and a label carrying the
    // numbers. Procedural geometry only in v0 — no external assets.
    function character(name) {
      const container = new PIXI.Container();
      container.y = app.screen.height * 0.72;
      const body = new PIXI.Graphics().circle(0, -60, 44).fill(0x545862);
      const nameTag = label(name, -40, 10, 18);
      const stat = label("", -80, 34, 14, 0x787b86);
      const moodTag = label("", -40, -140, 16, 0x787b86);
      container.addChild(body, nameTag, stat, moodTag);
      layers.chars.addChild(container);
      return { container, body, stat, moodTag, baseY: container.y };
    }

    function positionCharacters() {
      model.container.x = app.screen.width * 0.32;
      trader.container.x = app.screen.width * 0.62;
      model.baseY = trader.baseY = app.screen.height * 0.72;
      model.container.y = trader.container.y = model.baseY;
    }

    function draw(state) {
      positionCharacters();
      const m = state.model ?? {};
      const mood = m.reaction?.mood ?? "neutral";
      model.body.tint = color(mood);
      model.moodTag.text = mood;
      const rate = m.hit_rate == null ? "—" : `${(m.hit_rate * 100).toFixed(1)}%`;
      model.stat.text = `${m.wins ?? 0}W ${m.losses ?? 0}L · ${rate}`;

      // The trader stays dormant until trader_* events exist, so the room is
      // complete whether or not the freqtrade sidecar is running.
      const traderState = state.trader ?? null;
      trader.body.tint = traderState ? color(traderState.mood) : 0x2a2e39;
      trader.moodTag.text = traderState ? traderState.mood : "";
      trader.stat.text = traderState
        ? `${traderState.open_trades} open · ${traderState.profit_pct}%`
        : "asleep";

      layers.props.removeChildren();
      let y = 24;
      for (const [symbol, data] of Object.entries(state.symbols ?? {})) {
        if (!SYMBOLS.includes(symbol)) continue;
        const height = 12 + Math.abs(data.pressure) * 14;
        const pillar = new PIXI.Graphics()
          .rect(app.screen.width - 220, y, 90, Math.min(height, 90))
          .fill(color(data.mood));
        layers.props.addChild(pillar, label(`${symbol} · ${data.mood}`,
          app.screen.width - 220, y + 96, 14, 0x787b86));
        y += 140;
      }

      const h = state.history ?? {};
      layers.props.addChild(label(historyLine(h), 24, app.screen.height - 40, 14,
        0x787b86));
    }

    function historyLine(h) {
      if (!h.total_events) return "";
      const parts = [`${h.total_events} events remembered`];
      if (h.longest_streak) parts.push(`longest streak ${h.longest_streak.bars} bars`);
      if (h.worst_loss) {
        parts.push(`worst loss ${(h.worst_loss.realized_return * 100).toFixed(2)}%`);
      }
      if (h.outages) parts.push(`${h.outages} outages`);
      return parts.join(" · ");         /* set via .text: payloads are data */
    }

    // A live event nudges the room immediately; the next refresh reconciles it
    // with the authoritative server-side projection.
    function react(event) {
      const target = event.event_type.startsWith("trader_") ? trader : model;
      const intensity = event.severity >= 5 ? 1 : 0.5;
      target.container.y = target.baseY - 18 * intensity;
      setTimeout(() => { target.container.y = target.baseY; }, 400);
    }

    async function refresh() {
      try {
        const response = await fetch("/world/state");
        if (!response.ok) return;
        world = (await response.json()).data;
        draw(world);
      } catch (e) {
        // A transient API blip must never blank the stream: keep the last frame.
      }
    }

    function subscribe() {
      const source = new EventSource("/stream/world/events");
      source.onmessage = (message) => react(JSON.parse(message.data));
    }

    boot();
  </script>
</body>
</html>
```

- [ ] **Step 4: Add the route to `api/main.py`**, immediately after `overlay_events` (line 507):

```python
@app.get("/world", response_class=HTMLResponse)
def world_page():
    """The Living World room — a Browser Source for the world-focus scene."""
    symbols = [
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.market == "crypto" and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
    ]
    deduped = list(dict.fromkeys(symbols))
    return HTMLResponse(
        _render_template("world.html", {"__SYMBOLS__": json.dumps(deduped)})
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `poetry run pytest tests/api/test_world_page.py -q`
Expected: `6 passed`

- [ ] **Step 6: Eyeball it against the live stack**

Run: `sg docker -c "docker compose up -d --build"` then open `http://localhost:8000/world`.
Expected: floor line, MODEL character tinted by mood with its W/L record, TRADER greyed and labelled "asleep", one pillar per crypto symbol. Refresh — the room comes back identical.

- [ ] **Step 7: Lint and commit**

```bash
ruff check api/main.py tests/api/test_world_page.py
git add api/templates/world.html api/main.py tests/api/test_world_page.py
git commit -m "Viz: /world room rendered with PixiJS over the state projection"
```

---

### Task 6: Environment persistence — the room carries its past

**Files:**
- Modify: `world/state.py`, `api/templates/world.html` (already reads `state.history`, added in Task 5 — no change needed if `historyLine` is present)
- Test: `tests/unit/test_world_state.py` (append)

**Interfaces:**
- Produces: `state["history"]` = `{"total_events": int, "first_seen": str|None, "worst_loss": dict|None, "longest_streak": dict|None, "biggest_move": dict|None, "downtime_seconds": float, "outages": int}`.
- Consumed by: `historyLine()` in `world.html`, and Sprint 13's director.

These are long-horizon counters folded from the whole log, not the recent window — this is the mechanic the "away for a month" test actually grades.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_world_state.py`:

```python
def test_history_records_worst_loss_and_longest_streak():
    events = [
        _event(1, "signal_resolved", 1.2, {"outcome": "loss", "realized_return": -0.02}),
        _event(2, "signal_resolved", 1.9,
               {"outcome": "loss", "realized_return": -0.08}, minute=1),
        _event(3, "streak", 9.0, {"bars": 9, "direction": "up"}, minute=2),
        _event(4, "streak", 14.0, {"bars": 14, "direction": "down"}, minute=3),
    ]
    history = project_state(events, now=BASE)["history"]
    assert history["worst_loss"]["realized_return"] == -0.08
    assert history["longest_streak"]["bars"] == 14
    assert history["total_events"] == 4


def test_history_accumulates_downtime_across_outages():
    events = [
        _event(1, "stream_started", 1.0, symbol=None),
        _event(2, "stream_dropped", 5.0, symbol=None, minute=10),
        _event(3, "stream_started", 1.0, symbol=None, minute=25),
        _event(4, "stream_stopped", 2.0, symbol=None, minute=40),
    ]
    history = project_state(events, now=BASE)["history"]
    assert history["downtime_seconds"] == pytest.approx(15 * 60)
    assert history["outages"] == 1


def test_history_first_seen_is_the_oldest_event():
    events = [_event(2, "big_move", 5.0, {"return": 0.01}, minute=30),
              _event(1, "big_move", 5.0, {"return": 0.01})]
    assert project_state(events, now=BASE)["history"]["first_seen"] == BASE.isoformat()


def test_a_month_of_history_differs_materially_from_a_fresh_log():
    """The month-away property: a world that has lived is not a world that
    just booted, even when the recent window looks the same."""
    aged = [
        _event(i, "signal_resolved", 1.5,
               {"outcome": "loss", "realized_return": -0.01}, minute=i * 60)
        for i in range(1, 200)
    ]
    # One bad day early on, long since out of the recent window. A world that
    # has lived still carries it; a freshly-booted one has never seen it.
    aged[10]["payload"]["realized_return"] = -0.35
    fresh = aged[-3:]
    aged_state = project_state(aged, now=BASE)
    fresh_state = project_state(fresh, now=BASE)

    assert aged_state["history"]["total_events"] > fresh_state["history"]["total_events"]
    assert (
        aged_state["history"]["worst_loss"]["realized_return"]
        < fresh_state["history"]["worst_loss"]["realized_return"]
    )
    assert aged_state["recent"] != []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/unit/test_world_state.py -q -k "history or month"`
Expected: FAIL with `KeyError: 'history'`.

- [ ] **Step 3: Extend `empty_state()` in `world/state.py`**

Add to the returned dict:

```python
        "history": {
            "total_events": 0,
            "first_seen": None,
            "worst_loss": None,
            "longest_streak": None,
            "biggest_move": None,
            "downtime_seconds": 0.0,
            "outages": 0,
        },
        "_down_since": None,
```

- [ ] **Step 4: Fold history in `fold_event`**

Add to the dict built at the top of `fold_event`:

```python
        "history": dict(state["history"]),
        "_down_since": state["_down_since"],
```

and insert this block just before the `recent` handling at the end:

```python
    history = new["history"]
    history["total_events"] += 1
    if history["first_seen"] is None:
        history["first_seen"] = event["occurred_at"]

    if etype == "signal_resolved" and payload.get("outcome") == "loss":
        realized = float(payload.get("realized_return", 0.0))
        worst = history["worst_loss"]
        if worst is None or realized < worst["realized_return"]:
            history["worst_loss"] = {
                "symbol": symbol,
                "realized_return": realized,
                "occurred_at": event["occurred_at"],
            }
    elif etype == "streak":
        bars = int(payload.get("bars", 0))
        longest = history["longest_streak"]
        if longest is None or bars > longest["bars"]:
            history["longest_streak"] = {
                "symbol": symbol,
                "bars": bars,
                "direction": payload.get("direction"),
                "occurred_at": event["occurred_at"],
            }
    elif etype == "big_move":
        sigmas = float(payload.get("sigmas", event["severity"]))
        biggest = history["biggest_move"]
        if biggest is None or sigmas > biggest["sigmas"]:
            history["biggest_move"] = {
                "symbol": symbol,
                "sigmas": sigmas,
                "occurred_at": event["occurred_at"],
            }

    # Downtime accrues between a stop/drop and the next start. An unclosed
    # outage stays open rather than being guessed at — the log is the record.
    if etype in ("stream_stopped", "stream_dropped"):
        if new["_down_since"] is None:
            new["_down_since"] = event["occurred_at"]
        if etype == "stream_dropped":
            history["outages"] += 1
    elif etype == "stream_started" and new["_down_since"] is not None:
        down = _parse(new["_down_since"])
        history["downtime_seconds"] = round(
            history["downtime_seconds"] + (_parse(event["occurred_at"]) - down)
            .total_seconds(),
            3,
        )
        new["_down_since"] = None
```

Add the parse helper near the top of the module:

```python
def _parse(timestamp: str | datetime) -> datetime:
    if isinstance(timestamp, datetime):
        return timestamp
    return datetime.fromisoformat(timestamp)
```

- [ ] **Step 5: Drop the private accumulator in `project_state`**

Before the `generated_at` line:

```python
    state.pop("_down_since", None)
```

- [ ] **Step 6: Run the full projection suite**

Run: `poetry run pytest tests/unit/test_world_state.py -q`
Expected: `29 passed` — including the chunk-invariance test from Task 2, which now also covers the history accumulators.

- [ ] **Step 7: Lint and commit**

```bash
ruff check world/state.py tests/unit/test_world_state.py
git add world/state.py tests/unit/test_world_state.py
git commit -m "World: long-horizon history counters in the projection"
```

---

### Task 7: Historical backfill — give the world a past

**Files:**
- Create: `scripts/migrate_012.sql`, `scripts/backfill_world_events.py`
- Modify: `ingestion/binance_provider.py`, `storage/postgres_store.py`
- Test: `tests/unit/test_backfill_world_events.py`

**Interfaces:**
- Produces:
  - `BinanceProvider.get_klines_paginated(symbol, interval, start_ms, end_ms, sleep_seconds=0.25) -> list[list]`
  - `postgres_store.append_world_events_backfill(events: list[dict]) -> int` — `ON CONFLICT DO NOTHING`, **backfill path only**. The live `append_world_events` stays byte-for-byte unchanged.
  - `scripts/backfill_world_events.py` with pure helpers `iter_windows`, `apply_cooldown`, `stamp_backfilled`.

Binance caps klines at 1000 per request, so 60 days of 1m data is ~87 paginated requests per symbol — the existing provider does single requests only.

- [ ] **Step 1: Check the live table for duplicate natural keys BEFORE writing the migration**

Run:
```bash
sg docker -c "docker compose exec -T postgres psql -U market_data -d market_data -c \
  \"SELECT event_type, symbol, occurred_at, count(*) FROM world_events \
    GROUP BY 1,2,3 HAVING count(*) > 1 ORDER BY 4 DESC LIMIT 20;\""
```
Expected: `(0 rows)` — verified 2026-07-22 against 785 rows.

If rows come back, **stop and resolve before proceeding**: `CREATE UNIQUE INDEX` fails outright on existing duplicates. Surface the rows to the user rather than deleting anything — `world_events` is append-only, so a dedup is a decision, not a mechanical fix.

- [ ] **Step 2: Write `scripts/migrate_012.sql`**

```sql
-- Sprint 12 migration: a natural-key unique index on world_events so the
-- historical backfill is re-runnable. Idempotent — safe to re-run.
-- Applies to EXISTING volumes; fresh volumes get this from db/init.sql.
-- Apply: docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_012.sql
--
-- This constrains INSERTs only. It does NOT weaken the append-only contract:
-- no row is ever updated or deleted (docs/world-memory.md).
--
-- NULLS NOT DISTINCT (Postgres 15+) matters: stream_* events carry symbol
-- NULL, and default NULL semantics would let them duplicate freely.

CREATE UNIQUE INDEX IF NOT EXISTS uq_world_events_natural
    ON world_events (event_type, occurred_at, symbol) NULLS NOT DISTINCT;
```

- [ ] **Step 3: Mirror the index into `db/init.sql`**

Append after line 92 (`idx_world_events_type_time`):

```sql
-- Natural key: the same rule firing for the same symbol at the same instant
-- IS the same event. Makes the Sprint 12 historical backfill re-runnable.
CREATE UNIQUE INDEX uq_world_events_natural
    ON world_events (event_type, occurred_at, symbol) NULLS NOT DISTINCT;
```

- [ ] **Step 4: Write the failing tests** — `tests/unit/test_backfill_world_events.py`:

```python
"""Backfill tests exercise pure helpers against a fake provider — no network,
no database. The one behavioural claim worth pinning is that the live append
path still tolerates a re-fire once the unique index exists."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ingestion.binance_provider import BinanceProvider
from scripts.backfill_world_events import (
    apply_cooldown,
    iter_windows,
    stamp_backfilled,
)

BASE = datetime(2026, 5, 20, tzinfo=timezone.utc)
START_MS = int(BASE.timestamp() * 1000)


class FakePagedBinance:
    """Serves 1m klines from a fixed span, honouring startTime and the
    1000-per-request cap — the real endpoint's shape, no network."""

    def __init__(self, total_bars):
        self.bars = [START_MS + i * 60_000 for i in range(total_bars)]
        self.requests = []

    def get_klines(self, ticker_symbol, interval, limit=1000, start_ms=None):
        self.requests.append(start_ms)
        available = [t for t in self.bars if start_ms is None or t >= start_ms]
        return [[t, "100", "101", "99", "100.5", "10"] for t in available[:limit]]


def test_pagination_walks_past_the_1000_candle_cap():
    provider = BinanceProvider()
    fake = FakePagedBinance(2500)
    provider.get_klines = fake.get_klines

    collected = provider.get_klines_paginated(
        "BTCUSDT", "1m", START_MS, START_MS + 2500 * 60_000, sleep_seconds=0
    )
    assert len(collected) == 2500
    assert len(fake.requests) == 3          # 1000 + 1000 + 500
    open_times = [k[0] for k in collected]
    assert open_times == sorted(set(open_times)), "pages overlapped or repeated"


def test_pagination_stops_at_end_ms():
    provider = BinanceProvider()
    provider.get_klines = FakePagedBinance(2500).get_klines
    end_ms = START_MS + 1500 * 60_000

    collected = provider.get_klines_paginated(
        "BTCUSDT", "1m", START_MS, end_ms, sleep_seconds=0
    )
    assert collected and all(k[0] < end_ms for k in collected)


def test_pagination_refuses_to_spin_when_the_api_stops_advancing():
    """A malformed response repeating the same candle must terminate rather
    than hammer Binance forever — 87 requests per symbol is already a lot."""

    class StuckBinance:
        def __init__(self):
            self.calls = 0

        def get_klines(self, ticker_symbol, interval, limit=1000, start_ms=None):
            self.calls += 1
            return [[START_MS, "1", "2", "0.5", "1.5", "10"]] * limit

    provider = BinanceProvider()
    stuck = StuckBinance()
    provider.get_klines = stuck.get_klines

    provider.get_klines_paginated(
        "BTCUSDT", "1m", START_MS, START_MS + 10**9, sleep_seconds=0
    )
    assert stuck.calls < 5, "no-forward-progress guard did not fire"


def _frame(rows):
    index = pd.to_datetime([BASE + timedelta(minutes=i) for i in range(rows)], utc=True)
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.0 + (i % 5) for i in range(rows)],
            "volume": [10.0] * rows,
        },
        index=index,
    )


def test_iter_windows_covers_the_frame_in_lookback_steps():
    frame = _frame(300)
    windows = list(iter_windows(frame, vol_window=60, lookback_bars=10))
    assert windows, "no windows produced"
    # Every window is long enough for detect_events to evaluate anything.
    assert all(len(w) >= 70 for w in windows)
    # The last window ends at the last bar, so recent history isn't dropped.
    assert windows[-1].index[-1] == frame.index[-1]


def test_iter_windows_yields_nothing_for_a_short_frame():
    assert list(iter_windows(_frame(30), vol_window=60, lookback_bars=10)) == []


def test_apply_cooldown_suppresses_same_type_within_the_window():
    events = [
        {"event_type": "big_move", "symbol": "BTCUSDT", "occurred_at": BASE},
        {"event_type": "big_move", "symbol": "BTCUSDT",
         "occurred_at": BASE + timedelta(minutes=5)},
        {"event_type": "big_move", "symbol": "BTCUSDT",
         "occurred_at": BASE + timedelta(minutes=45)},
    ]
    kept = apply_cooldown(events, cooldown_minutes=30)
    assert len(kept) == 2


def test_apply_cooldown_is_per_event_type():
    events = [
        {"event_type": "big_move", "symbol": "BTCUSDT", "occurred_at": BASE},
        {"event_type": "streak", "symbol": "BTCUSDT", "occurred_at": BASE},
    ]
    assert len(apply_cooldown(events, cooldown_minutes=30)) == 2


def test_apply_cooldown_is_per_symbol():
    events = [
        {"event_type": "big_move", "symbol": "BTCUSDT", "occurred_at": BASE},
        {"event_type": "big_move", "symbol": "ETHUSDT", "occurred_at": BASE},
    ]
    assert len(apply_cooldown(events, cooldown_minutes=30)) == 2


def test_stamp_backfilled_flags_every_event():
    events = [{"event_type": "big_move", "payload": {"sigmas": 4.2}}]
    stamped = stamp_backfilled(events)
    assert stamped[0]["payload"]["backfilled"] is True
    assert stamped[0]["payload"]["sigmas"] == 4.2
    assert "backfilled" not in events[0]["payload"], "input must not be mutated"


def test_stamp_backfilled_handles_a_missing_payload():
    assert stamp_backfilled([{"event_type": "streak"}])[0]["payload"] == {
        "backfilled": True
    }
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `poetry run pytest tests/unit/test_backfill_world_events.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.backfill_world_events'`

- [ ] **Step 6: Add pagination to `ingestion/binance_provider.py`**

Insert after `get_klines` (which ends at line 87):

```python
    def get_klines_paginated(
        self,
        ticker_symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        sleep_seconds: float = 0.25,
    ) -> list[list]:
        """Walk the 1000-candle page limit to cover a long span. Used by the
        Sprint 12 world backfill; the live paths still do single requests."""
        collected: list[list] = []
        cursor = start_ms
        while cursor < end_ms:
            page = self.get_klines(
                ticker_symbol, interval, limit=MAX_KLINES_LIMIT, start_ms=cursor
            )
            if not page:
                break
            collected.extend(k for k in page if k[0] < end_ms)
            next_cursor = page[-1][0] + 1
            if next_cursor <= cursor:
                break  # no forward progress: refuse to spin on the API
            cursor = next_cursor
            if len(page) < MAX_KLINES_LIMIT:
                break
            time.sleep(sleep_seconds)  # respect the weight limit over ~87 calls
        return collected
```

Add `import time` to the imports at the top of the file.

- [ ] **Step 7: Add the backfill writer to `storage/postgres_store.py`**

Immediately after `append_world_events` (which ends at line 353):

Add the SQL constant beside `_WORLD_EVENTS_SQL` (line 332) — written out in full rather than derived from it by string surgery, so the live statement can never be changed by accident:

```python
_WORLD_EVENTS_BACKFILL_SQL = """
    INSERT INTO world_events (occurred_at, event_type, symbol, severity, payload)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT DO NOTHING
"""
```

Then the writer:

```python
def append_world_events_backfill(events: list[dict]) -> int:
    """Backfill-only writer: ON CONFLICT DO NOTHING against the natural-key
    unique index, so replaying history is re-runnable. The live append path
    (append_world_events) is deliberately left untouched — its 30-minute
    cooldown is the live dedupe, and a surprise conflict there should surface
    rather than be swallowed."""
    import json as _json

    rows = [
        (
            _as_datetime(e["occurred_at"]),
            e["event_type"],
            e.get("symbol", "").upper() or None,
            float(e["severity"]),
            _json.dumps(e.get("payload", {})),
        )
        for e in events
    ]
    if not rows:
        return 0
    # NOT _executemany(): that helper returns len(rows), which would report a
    # re-run as having written everything and make the idempotence check
    # meaningless. Here the actual affected count is the whole point.
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(_WORLD_EVENTS_BACKFILL_SQL, rows)
            inserted = cur.rowcount
    return max(inserted, 0)
```

This is the one place in the module that deliberately bypasses `_executemany` (`storage/postgres_store.py:405`), because that helper returns `len(rows)` rather than the affected count — fine for upserts that always apply, wrong for a conflict-skipping insert.

- [ ] **Step 8: Write `scripts/backfill_world_events.py`**

```python
"""Replay the salience rules over historical Binance klines so the world has
a past (Sprint 12).

The events are genuinely real — real bars through the same deterministic
rules the live path uses. The `backfilled: true` payload flag marks that the
world *learned* them rather than *witnessed* them, which the renderer may
present differently. Nothing is fabricated.

Re-runnable: writes go through append_world_events_backfill, which relies on
the natural-key unique index from scripts/migrate_012.sql.

Usage:
    python scripts/backfill_world_events.py --days 60
    python scripts/backfill_world_events.py --days 7 --dry-run
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from ingestion.binance_provider import BinanceProvider, _klines_to_frame  # noqa: E402
from scheduler.watchlist import load_watchlist  # noqa: E402
from storage.postgres_store import append_world_events_backfill  # noqa: E402
from world.salience import SalienceConfig, detect_events  # noqa: E402

logger = logging.getLogger(__name__)


def iter_windows(
    frame: pd.DataFrame, vol_window: int, lookback_bars: int
) -> Iterator[pd.DataFrame]:
    """Slide detect_events' evaluation window across a long frame.

    detect_events only inspects the final `lookback_bars` rows and needs
    `vol_window` rows of warm-up before them, so replaying history means
    handing it overlapping slices rather than the whole frame at once.
    """
    span = vol_window + lookback_bars
    if len(frame) < span:
        return
    end = span
    while end < len(frame):
        yield frame.iloc[end - span:end]
        end += lookback_bars
    yield frame.iloc[len(frame) - span:]


def apply_cooldown(events: list[dict], cooldown_minutes: int) -> list[dict]:
    """In-memory equivalent of world/events.py's DB-backed cooldown. Keyed on
    (event_type, symbol) so a 60-day replay doesn't flood the log."""
    cooldown = timedelta(minutes=cooldown_minutes)
    last_seen: dict[tuple[str, str | None], datetime] = {}
    kept: list[dict] = []
    for event in sorted(events, key=lambda e: e["occurred_at"]):
        key = (event["event_type"], event.get("symbol"))
        previous = last_seen.get(key)
        if previous is not None and event["occurred_at"] - previous < cooldown:
            continue
        last_seen[key] = event["occurred_at"]
        kept.append(event)
    return kept


def stamp_backfilled(events: list[dict]) -> list[dict]:
    """Flag provenance without mutating the caller's events."""
    return [
        {**event, "payload": {**(event.get("payload") or {}), "backfilled": True}}
        for event in events
    ]


def backfill_symbol(
    symbol: str,
    days: int,
    provider: BinanceProvider,
    config: SalienceConfig | None = None,
) -> list[dict]:
    config = config or SalienceConfig()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    raw = provider.get_klines_paginated(
        symbol,
        interval="1m",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
    )
    frame = _klines_to_frame(raw)
    logger.info("Fetched klines", extra={"symbol": symbol, "bars": len(frame)})

    events: list[dict] = []
    for window in iter_windows(frame, config.vol_window, config.lookback_bars):
        events.extend(detect_events(symbol, window, config))

    # detect_events re-evaluates overlapping bars, so the same firing can
    # appear in several windows; the natural key dedupes exact repeats and the
    # cooldown thins near-repeats, exactly as the live path does.
    unique = {(e["event_type"], e["symbol"], e["occurred_at"]): e for e in events}
    return stamp_backfilled(
        apply_cooldown(list(unique.values()), config.cooldown_minutes)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--symbols", default=None,
                        help="comma-separated; defaults to crypto watchlist entries")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_logging()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(dict.fromkeys(
            spec.symbol.upper()
            for spec in load_watchlist().tickers
            if spec.market == "crypto"
        ))

    provider = BinanceProvider()
    total = 0
    for symbol in symbols:
        events = backfill_symbol(symbol, args.days, provider)
        logger.info(
            "Backfill candidates",
            extra={"symbol": symbol, "events": len(events),
                   "types": sorted({e["event_type"] for e in events})},
        )
        if args.dry_run:
            continue
        total += append_world_events_backfill(events)

    logger.info("Backfill complete", extra={"written": total, "dry_run": args.dry_run})
    print(f"{'would write' if args.dry_run else 'wrote'} {total} world events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `poetry run pytest tests/unit/test_backfill_world_events.py -q`
Expected: `10 passed`

- [ ] **Step 10: Verify the live append path still behaves under the new index**

This is the assertion the plan must not take on faith — the code is unchanged, but its *runtime behaviour* changes once a unique index exists. Add to `tests/integration/test_postgres_store.py`:

```python
def test_live_append_path_under_the_natural_key_index():
    """The live writer has no ON CONFLICT clause. Once uq_world_events_natural
    exists, a genuine re-fire of the same (type, symbol, occurred_at) raises
    instead of duplicating. Pinning the behaviour here means the world-memory
    writer can never start throwing 503s in production unnoticed."""
    import psycopg
    import pytest

    from storage.postgres_store import append_world_events

    event = {
        "occurred_at": datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc),
        "event_type": "big_move",
        "symbol": "TESTUSDT",
        "severity": 5.0,
        "payload": {"sigmas": 5.0},
    }
    append_world_events([event])
    with pytest.raises(psycopg.errors.UniqueViolation):
        append_world_events([event])
```

Run: `poetry run pytest tests/integration/test_postgres_store.py -q`
Expected: passes against the compose Postgres (auto-skips without it).

**If this surfaces as a real production risk** — i.e. the salience job's 30-minute cooldown can be bypassed by a restart racing the DB read — report it rather than papering over it. The correct fix would be making the *live* writer conflict-tolerant too, which is a decision for the user, not a silent edit.

- [ ] **Step 11: Apply the migration and run the backfill twice**

```bash
sg docker -c "docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_012.sql"
poetry run python scripts/backfill_world_events.py --days 7 --dry-run
COUNT_SQL="SELECT count(*) FROM world_events;"
sg docker -c "docker compose exec -T postgres psql -U market_data -d market_data -tAc \"$COUNT_SQL\""
poetry run python scripts/backfill_world_events.py --days 60
sg docker -c "docker compose exec -T postgres psql -U market_data -d market_data -tAc \"$COUNT_SQL\""
poetry run python scripts/backfill_world_events.py --days 60   # idempotence check
sg docker -c "docker compose exec -T postgres psql -U market_data -d market_data -tAc \"$COUNT_SQL\""
```
Expected: the second full run prints `wrote 0 world events`, **and** the row count is identical before and after it. Check both — the printed number and the table count are independent evidence, and only the table count is immune to a bug in the writer's return value.

Then confirm the provenance flag actually landed:
```bash
sg docker -c "docker compose exec -T postgres psql -U market_data -d market_data -c \
  \"SELECT count(*) FROM world_events WHERE payload->>'backfilled' = 'true';\""
```
Expected: a non-zero count.

- [ ] **Step 12: Lint and commit**

```bash
ruff check scripts/backfill_world_events.py ingestion/binance_provider.py \
  storage/postgres_store.py tests/unit/test_backfill_world_events.py
git add scripts/migrate_012.sql scripts/backfill_world_events.py db/init.sql \
  ingestion/binance_provider.py storage/postgres_store.py \
  tests/unit/test_backfill_world_events.py tests/integration/test_postgres_store.py
git commit -m "World: historical backfill with natural-key idempotence"
```

---

> **Severable boundary.** Tasks 8 and 9 add the second inhabitant. If the sprint is running long, cut them here and go straight to Task 10 — `world.html` already renders the trader as dormant, so nothing is left half-built.

### Task 8: freqtrade sidecar — keyless dry-run container

**Files:**
- Create: `config/freqtrade/config.json`
- Modify: `docker-compose.yml`, `.env.example`, `config/exceptions.py`

**Interfaces:**
- Produces: a `freqtrade` compose service reachable at `http://freqtrade:8080` on the implicit compose network; `FreqtradeUnreachableError` (503).

**The GPL boundary is absolute** (standing decision, `docs/roadmap.md:116`): separate container, REST-only integration, never imported, never vendored.

- [ ] **Step 1: Add the exception to `config/exceptions.py`**

```python
class FreqtradeUnreachableError(BaseAppException):
    def __init__(
        self, message: str = "freqtrade sidecar unreachable", status_code: int = 503
    ):
        super().__init__(message, status_code)
```

- [ ] **Step 2: Create `config/freqtrade/config.json`**

Dry-run, keyless, REST on. Note from `docs/freqtrade-sidecar-spike.md`: a short `jwt_secret_key` is a **fatal** config error.

```json
{
  "max_open_trades": 3,
  "stake_currency": "USDT",
  "stake_amount": 100,
  "tradable_balance_ratio": 0.99,
  "dry_run": true,
  "dry_run_wallet": 1000,
  "cancel_open_orders_on_exit": false,
  "timeframe": "5m",
  "exchange": {
    "name": "binance",
    "key": "",
    "secret": "",
    "ccxt_config": {},
    "ccxt_async_config": {},
    "pair_whitelist": ["BTC/USDT", "ETH/USDT"],
    "pair_blacklist": []
  },
  "entry_pricing": {
    "price_side": "same",
    "use_order_book": true,
    "order_book_top": 1,
    "price_last_balance": 0.0,
    "check_depth_of_market": { "enabled": false, "bids_to_ask_delta": 1 }
  },
  "exit_pricing": {
    "price_side": "same",
    "use_order_book": true,
    "order_book_top": 1
  },
  "pairlists": [{ "method": "StaticPairList" }],
  "api_server": {
    "enabled": true,
    "listen_ip_address": "0.0.0.0",
    "listen_port": 8080,
    "verbosity": "error",
    "enable_openapi": false,
    "jwt_secret_key": "replace-with-a-long-random-string-at-least-32-chars",
    "CORS_origins": [],
    "username": "worldwatcher",
    "password": "replace-me"
  },
  "bot_name": "world-trader",
  "initial_state": "running",
  "internals": { "process_throttle_secs": 5 }
}
```

- [ ] **Step 3: Move the credentials into `.env`**

Append to `.env.example`:

```bash
# freqtrade dry-run sidecar (Sprint 12) — the trader inhabitant.
# Never a real key: dry_run is true and the exchange block is keyless.
FREQTRADE_URL=http://freqtrade:8080
FREQTRADE_USERNAME=worldwatcher
FREQTRADE_PASSWORD=
# Must be long — freqtrade rejects short values as a fatal config error.
FREQTRADE_JWT_SECRET=
# Set to 0 to disable the trader-mirror scheduler job.
TRADER_MIRROR_ENABLED=1
```

Generate real local values (these go in `.env`, never the repo):
```bash
python -c "import secrets; print('FREQTRADE_JWT_SECRET=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('FREQTRADE_PASSWORD=' + secrets.token_urlsafe(18))"
```

- [ ] **Step 4: Add the compose service**

In `docker-compose.yml`, after the `scheduler` service and before `volumes:`. Note this is the official image as a **fourth service** — it does *not* follow the `api`/`scheduler` same-build pattern. No published port: the scheduler reaches it over the compose network.

```yaml
  freqtrade:
    image: freqtradeorg/freqtrade:stable
    container_name: market-data-freqtrade
    restart: unless-stopped
    # GPL boundary: separate container, REST-only, never imported or vendored.
    command: >
      trade
      --config /freqtrade/user_data/config.json
      --strategy SampleStrategy
    environment:
      FREQTRADE__API_SERVER__JWT_SECRET_KEY: ${FREQTRADE_JWT_SECRET}
      FREQTRADE__API_SERVER__USERNAME: ${FREQTRADE_USERNAME:-worldwatcher}
      FREQTRADE__API_SERVER__PASSWORD: ${FREQTRADE_PASSWORD}
    volumes:
      - ./config/freqtrade:/freqtrade/user_data:ro
      - freqtrade_data:/freqtrade/user_data/data
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:8080/api/v1/ping || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 60s
```

And add to the `volumes:` block at the bottom:

```yaml
  freqtrade_data:
```

- [ ] **Step 5: Add `FREQTRADE_URL` to the scheduler service environment**

Under `scheduler:` → `environment:`:

```yaml
      FREQTRADE_URL: ${FREQTRADE_URL:-http://freqtrade:8080}
      FREQTRADE_USERNAME: ${FREQTRADE_USERNAME:-worldwatcher}
      FREQTRADE_PASSWORD: ${FREQTRADE_PASSWORD}
      TRADER_MIRROR_ENABLED: ${TRADER_MIRROR_ENABLED:-1}
```

- [ ] **Step 6: Boot it and confirm it is alive**

```bash
sg docker -c "docker compose up -d freqtrade"
sg docker -c "docker compose ps"
sg docker -c "docker compose exec -T scheduler python -c \
  \"import httpx, os; print(httpx.get(os.environ['FREQTRADE_URL'] + '/api/v1/ping', timeout=5).json())\""
```
Expected: service `healthy`; the ping prints `{'status': 'pong'}`.

If the container exits immediately, check `sg docker -c "docker compose logs freqtrade"` — a short `jwt_secret_key` is the usual cause.

- [ ] **Step 7: Commit**

```bash
git add config/freqtrade/config.json docker-compose.yml .env.example config/exceptions.py
git commit -m "Provider: freqtrade dry-run sidecar as the fourth compose service"
```

---

### Task 9: Trader character — REST mirror → `trader_*` world events

**Files:**
- Create: `world/trader_events.py`
- Modify: `scheduler/jobs.py`, `scheduler/service.py`, `world/salience.py` (`KNOWN_EVENT_TYPES`), `world/reactions.py` (`REACTIONS`), `api/templates/overlay_events.html` (`HEADLINES`)
- Test: `tests/unit/test_trader_events.py`

**Interfaces:**
- Produces:
  - `world.trader_events.trader_mirror_enabled() -> bool`
  - `world.trader_events.diff_trader_state(previous: dict, current: dict) -> list[dict]` — pure
  - `world.trader_events.record_trader_events(client=None) -> list[dict]`
  - `scheduler.jobs.run_trader_mirror_job() -> dict`
  - New event types `trader_opened`, `trader_closed`, `trader_milestone`.
- Consumes: the sidecar's `/api/v1/status` and `/api/v1/profit`.

**The HTTP client is passed in as an argument** — the same seam `stream_ctl.py` uses for its OBS client — so tests never touch the sidecar. The trader is an *independent inhabitant*, not an executor of our signals: when it disagrees with the model, that disagreement is real, and it is content.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_trader_events.py`:

```python
"""The mirror is a pure diff plus a thin fetch. Tests drive the diff directly
and inject a fake client for the rest — no sidecar, no network."""

import pytest

from world.reactions import REACTIONS
from world.salience import KNOWN_EVENT_TYPES
from world.trader_events import (
    TRADER_EVENT_TYPES,
    diff_trader_state,
    record_trader_events,
)


class FakeClient:
    def __init__(self, status, profit):
        self._status = status
        self._profit = profit
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self._status if "status" in path else self._profit


def _trade(trade_id, pair="BTC/USDT", profit_pct=0.0):
    return {"trade_id": trade_id, "pair": pair, "profit_pct": profit_pct}


def test_new_event_types_are_registered_everywhere():
    assert TRADER_EVENT_TYPES <= KNOWN_EVENT_TYPES
    assert TRADER_EVENT_TYPES <= set(REACTIONS)


def test_opening_a_trade_emits_trader_opened():
    events = diff_trader_state(
        {"open_trade_ids": [], "profit_closed_percent": 0.0},
        {"open_trades": [_trade(1)], "profit_closed_percent": 0.0},
    )
    assert [e["event_type"] for e in events] == ["trader_opened"]
    assert events[0]["payload"]["pair"] == "BTC/USDT"


def test_closing_a_trade_emits_trader_closed():
    events = diff_trader_state(
        {"open_trade_ids": [1], "profit_closed_percent": 0.0},
        {"open_trades": [], "profit_closed_percent": -2.5},
    )
    types = [e["event_type"] for e in events]
    assert "trader_closed" in types


def test_no_change_emits_nothing():
    previous = {"open_trade_ids": [1], "profit_closed_percent": 1.0}
    current = {"open_trades": [_trade(1)], "profit_closed_percent": 1.0}
    assert diff_trader_state(previous, current) == []


def test_severity_grows_with_the_size_of_the_pnl_swing():
    small = diff_trader_state(
        {"open_trade_ids": [1], "profit_closed_percent": 0.0},
        {"open_trades": [], "profit_closed_percent": -0.5},
    )
    large = diff_trader_state(
        {"open_trade_ids": [1], "profit_closed_percent": 0.0},
        {"open_trades": [], "profit_closed_percent": -12.0},
    )
    assert large[0]["severity"] > small[0]["severity"]


def test_first_observation_does_not_replay_history():
    """An empty previous state means we've never looked — reporting every
    already-open trade as newly opened would fabricate events."""
    events = diff_trader_state({}, {"open_trades": [_trade(1), _trade(2)],
                                    "profit_closed_percent": 3.0})
    assert events == []


def test_record_trader_events_uses_the_injected_client(monkeypatch):
    written = []
    monkeypatch.setattr(
        "world.trader_events.append_world_events",
        lambda events: written.extend(events) or len(events),
    )
    monkeypatch.setattr(
        "world.trader_events._load_previous", lambda: {"open_trade_ids": []}
    )
    client = FakeClient(
        {"open_trades": [_trade(7)]}, {"profit_closed_percent": 0.0}
    )
    events = record_trader_events(client=client)

    assert any("status" in p for p in client.paths)
    assert [e["event_type"] for e in events] == ["trader_opened"]
    assert len(written) == 1


def test_unreachable_sidecar_is_a_logged_skip_not_a_crash():
    class DeadClient:
        def get(self, path):
            raise ConnectionError("no route to host")

    assert record_trader_events(client=DeadClient()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/unit/test_trader_events.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'world.trader_events'`

- [ ] **Step 3: Register the new types in `world/salience.py`**

Inside `KNOWN_EVENT_TYPES`, after the stream lifecycle block:

```python
        # trader inhabitant (Sprint 12) — severities in world/trader_events.py
        "trader_opened",
        "trader_closed",
        "trader_milestone",
```

- [ ] **Step 4: Add reactions in `world/reactions.py`**

Inside `REACTIONS`:

```python
    "trader_opened": ("decisive", "step"),
    "trader_closed": ("weighing", "turn"),
    "trader_milestone": ("proud", "cheer"),
```

Add the moods to `MOOD_COLOR` in `api/templates/world.html`:

```javascript
      decisive: 0x5b9cf6, weighing: 0x9c6ade, proud: 0x26a69a,
```

- [ ] **Step 5: Add headlines in `api/templates/overlay_events.html`**

Inside `HEADLINES`, after `stream_dropped`:

```javascript
      trader_opened: (e) => `trader opened ${e.payload.pair}`,
      trader_closed: (e) => `trader closed ${e.payload.pair} — ` +
        `${e.payload.profit_pct?.toFixed(2)}%`,
      trader_milestone: (e) => `trader P&L now ${e.payload.profit_pct?.toFixed(2)}%`,
```

- [ ] **Step 6: Write `world/trader_events.py`**

```python
"""The trader inhabitant (Sprint 12): the freqtrade dry-run sidecar mirrored
into world events.

REST-only by design — freqtrade is GPL, so it lives in its own container and
is never imported here. The trader is an *independent* inhabitant, not an
executor of our signals: when its positions disagree with the model's calls,
that disagreement is real and is content.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

from storage.postgres_store import append_world_events, get_world_events

logger = logging.getLogger(__name__)

TRADER_EVENT_TYPES = frozenset(
    {"trader_opened", "trader_closed", "trader_milestone"}
)
TRADER_MIRROR_ENABLED_ENV = "TRADER_MIRROR_ENABLED"
MILESTONE_STEP = 5.0  # percent of closed P&L between milestone events


def trader_mirror_enabled() -> bool:
    raw = os.environ.get(TRADER_MIRROR_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no"}


class FreqtradeClient:
    """Thin REST wrapper. Injected as an argument everywhere so tests never
    reach the sidecar."""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 5.0):
        self._base = (base_url or os.environ.get(
            "FREQTRADE_URL", "http://freqtrade:8080"
        )).rstrip("/")
        self._auth = (
            os.environ.get("FREQTRADE_USERNAME", "worldwatcher"),
            os.environ.get("FREQTRADE_PASSWORD", ""),
        )
        self._timeout = timeout_seconds

    def get(self, path: str) -> dict:
        response = httpx.get(
            f"{self._base}{path}", auth=self._auth, timeout=self._timeout
        )
        response.raise_for_status()
        return response.json()


def _event(event_type: str, severity: float, payload: dict) -> dict:
    return {
        "occurred_at": datetime.now(timezone.utc),
        "event_type": event_type,
        "symbol": (payload.get("pair") or "").replace("/", "") or None,
        "severity": round(severity, 4),
        "payload": payload,
    }


def diff_trader_state(previous: dict, current: dict) -> list[dict]:
    """Pure diff between the last observation and the current one.

    An empty `previous` means we have never looked; emitting every already-open
    trade as newly opened would fabricate history, so the first observation is
    silent by design.
    """
    if not previous:
        return []

    events: list[dict] = []
    was_open = set(previous.get("open_trade_ids") or [])
    now_open = {t["trade_id"]: t for t in current.get("open_trades") or []}

    for trade_id in sorted(set(now_open) - was_open):
        trade = now_open[trade_id]
        events.append(_event("trader_opened", 1.0, {
            "trade_id": trade_id, "pair": trade.get("pair"),
        }))

    closed_before = float(previous.get("profit_closed_percent") or 0.0)
    closed_now = float(current.get("profit_closed_percent") or 0.0)
    swing = abs(closed_now - closed_before)

    for trade_id in sorted(was_open - set(now_open)):
        # Severity tracks how much the closed P&L moved: a scratch exit is
        # routine, a large one is worth the room reacting to.
        events.append(_event("trader_closed", 1.0 + swing / 2.0, {
            "trade_id": trade_id,
            "pair": None,
            "profit_pct": round(closed_now, 4),
            "swing_pct": round(closed_now - closed_before, 4),
        }))

    # Truncating division, NOT floor: `-0.5 // 5` is -1, which would make a
    # trivial scratch loss look like it crossed a milestone.
    if int(closed_now / MILESTONE_STEP) != int(closed_before / MILESTONE_STEP):
        events.append(_event("trader_milestone", 2.0 + swing / 2.0, {
            "profit_pct": round(closed_now, 4),
        }))
    return events


def _load_previous() -> dict:
    """Reconstruct the last-seen state from the world's own memory rather than
    a state file — the log is already the durable record."""
    try:
        rows = get_world_events(limit=200)
    except Exception:
        return {}
    open_ids: set[int] = set()
    profit = None
    for row in reversed(rows):  # oldest first
        payload = row.get("payload") or {}
        if row["event_type"] == "trader_opened":
            open_ids.add(payload.get("trade_id"))
        elif row["event_type"] == "trader_closed":
            open_ids.discard(payload.get("trade_id"))
            profit = payload.get("profit_pct", profit)
        elif row["event_type"] == "trader_milestone":
            profit = payload.get("profit_pct", profit)
    if profit is None and not open_ids:
        return {}
    return {
        "open_trade_ids": sorted(i for i in open_ids if i is not None),
        "profit_closed_percent": profit or 0.0,
    }


def record_trader_events(client=None) -> list[dict]:
    """Poll the sidecar, diff, append. A dead sidecar is a logged skip — the
    world keeps running when one inhabitant is unavailable."""
    client = client or FreqtradeClient()
    try:
        status = client.get("/api/v1/status")
        profit = client.get("/api/v1/profit")
    except Exception as e:
        logger.warning("Trader mirror skipped", extra={"error": str(e)})
        return []

    open_trades = status if isinstance(status, list) else status.get("open_trades", [])
    current = {
        "open_trades": open_trades,
        "profit_closed_percent": (profit or {}).get("profit_closed_percent", 0.0),
    }
    events = diff_trader_state(_load_previous(), current)
    if events:
        append_world_events(events)
        logger.info("Trader events recorded", extra={"count": len(events)})
    return events
```

- [ ] **Step 7: Add the scheduler job to `scheduler/jobs.py`**

After `run_resolver_job` (which ends at line 121):

```python
def run_trader_mirror_job() -> dict:
    """Mirror the freqtrade dry-run sidecar into trader_* world events."""
    from world.trader_events import record_trader_events

    events = record_trader_events()
    result = {
        "events": len(events),
        "event_types": sorted({e["event_type"] for e in events}),
    }
    logger.info("Trader mirror complete", extra=result)
    return result
```

- [ ] **Step 8: Register it as a singleton in `scheduler/service.py`**

Following the `resolver:signals` precedent (line 69), inside the `if salience_enabled():` block, after the resolver registration:

```python
            if trader_mirror_enabled():
                self._add_job(
                    "trader:mirror",
                    jobs.run_trader_mirror_job,
                    (),
                    watchlist.interval_seconds,
                )
```

And add the import beside `from world.salience import salience_enabled` (line 14):

```python
from world.trader_events import trader_mirror_enabled
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `poetry run pytest tests/unit/test_trader_events.py tests/unit/test_reactions.py -q`
Expected: `19 passed` — the registry-invariant test from Task 3 now also covers the three `trader_*` types.

- [ ] **Step 10: Confirm a real event appears**

```bash
sg docker -c "docker compose up -d --build"
# wait for the sidecar to open its first dry-run trade (can take several minutes)
sg docker -c "docker compose exec -T postgres psql -U market_data -d market_data -c \
  \"SELECT occurred_at, event_type, symbol, severity FROM world_events \
    WHERE event_type LIKE 'trader_%' ORDER BY occurred_at DESC LIMIT 5;\""
```
Expected: at least one `trader_opened` row. Then reload `/world` — the TRADER character is no longer "asleep".

- [ ] **Step 11: Lint and commit**

```bash
ruff check world/trader_events.py scheduler/jobs.py scheduler/service.py \
  world/salience.py world/reactions.py tests/unit/test_trader_events.py
git add world/trader_events.py scheduler/jobs.py scheduler/service.py \
  world/salience.py world/reactions.py api/templates/world.html \
  api/templates/overlay_events.html tests/unit/test_trader_events.py
git commit -m "World: trader inhabitant mirrored from the freqtrade sidecar"
```

---

### Task 10: Integration test, docs, sprint close

**Files:**
- Create: `docs/world-renderer.md`, `tests/integration/test_world_renderer.py`
- Modify: `README.md`, `CLAUDE.md`, `docs/architecture-vision.md`, `docs/roadmap.md`
- Modify (vault): `~/Documents/Obsidian Vault/Market Data Pipeline/Sprints/Sprint 12 — World Renderer v0.md`, `Sprint Tracker.md`

Two false claims in the docs must be corrected here — they were verified wrong during planning:
- `docs/roadmap.md:59` and `docs/architecture-vision.md` describe the world page as WebSocket-driven. It is SSE (`/stream/world/events`), which already existed and already drove both overlays.
- `docs/roadmap.md:61` claims "~2 months of history". The live log held 3 days at planning time; Task 7's backfill is what actually creates the past, and those events are flagged `backfilled`.

- [ ] **Step 1: Write the integration test** — `tests/integration/test_world_renderer.py`:

```python
"""End-to-end over the projection chain: events -> state -> endpoint -> page.
Uses patched storage, so it runs without Postgres like the rest of tests/."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from world.state import project_state

client = TestClient(app)


def _log():
    return [
        {"id": 3, "occurred_at": "2026-07-20T12:30:00+00:00",
         "event_type": "signal_resolved", "symbol": "BTCUSDT", "severity": 1.9,
         "payload": {"outcome": "loss", "realized_return": -0.04}},
        {"id": 2, "occurred_at": "2026-07-20T12:10:00+00:00",
         "event_type": "big_move", "symbol": "BTCUSDT", "severity": 8.0,
         "payload": {"return": -0.05, "sigmas": 8.0}},
        {"id": 1, "occurred_at": "2026-07-20T12:00:00+00:00",
         "event_type": "stream_started", "symbol": None, "severity": 1.0,
         "payload": {}},
    ]


def test_events_flow_through_projection_to_endpoint():
    with patch("api.main.get_world_events", return_value=_log()):
        data = client.get("/world/state").json()["data"]

    assert data["event_count"] == 3
    assert data["stream"]["state"] == "live"
    assert data["model"]["losses"] == 1
    assert data["symbols"]["BTCUSDT"]["mood"] in {"bearish", "panicked", "calm"}
    assert data["history"]["worst_loss"]["realized_return"] == -0.04
    assert data["recent"][0]["reaction"]["mood"] == "dejected"


def test_endpoint_state_matches_the_pure_projection():
    """No logic may leak into the API layer — the endpoint is composition only."""
    with patch("api.main.get_world_events", return_value=_log()):
        served = client.get("/world/state").json()["data"]
    direct = project_state(_log())

    served.pop("generated_at")
    direct.pop("generated_at")
    for key in ("event_count", "symbols", "model", "stream", "history"):
        assert served[key] == direct[key]


def test_the_page_and_the_state_endpoint_agree_on_symbols():
    page = client.get("/world").text
    with patch("api.main.get_world_events", return_value=_log()):
        data = client.get("/world/state").json()["data"]
    for symbol in data["symbols"]:
        assert symbol in page or symbol not in ("BTCUSDT", "ETHUSDT")
```

- [ ] **Step 2: Run the whole suite**

Run: `poetry run pytest -q`
Expected: all green, roughly 300+ tests. Any failure here is a regression from this sprint — fix it before continuing.

- [ ] **Step 3: Write `docs/world-renderer.md`**

```markdown
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

## The two inhabitants

- **MODEL** — our own signal model. Its mood follows its win/loss record, and
  it is currently losing (45.3% hit rate at the time of writing). The room is
  deliberately not designed to look celebratory by default; a losing model is
  honest, and it is content.
- **TRADER** — the freqtrade dry-run sidecar, mirrored over REST into
  `trader_*` events. It is an independent inhabitant, not an executor of our
  signals. When the two disagree, the disagreement is real. If the sidecar is
  not running, the trader renders as dormant and the room is still complete.

## OBS integration

`/world` is a Browser Source at 1920×1080 with an opaque `#131722`
background. Sprint 13 adds it as the `world-focus` scene. Host gotcha: OBS on
this machine needs `BrowserHWAccel=false` in `~/.config/obs-studio/global.ini`,
or CEF's GPU process crashes and takes OBS down when a browser-source scene
loads.
```

- [ ] **Step 4: Correct the two false claims in `docs/roadmap.md`**

Verified 2026-07-22: the Sprint 12 block sits at `docs/roadmap.md:56-61`. Replace this exact text —

```markdown
- One room, one character, a handful of world-state variables — a PixiJS (or
  Phaser) canvas page driven by `world_events` over WebSocket, loaded as a
  Browser Source. World state = projection over the event log.
- First personality: one policy over the salience stream, with visible
  reactions. The world already has ~2 months of history to display.
```

with:

```markdown
- One room, two characters, a handful of world-state variables — a PixiJS
  canvas page driven by `world_events` over **SSE** (`/stream/world/events`,
  the transport the OBS overlays already use), loaded as a Browser Source.
  World state = a deterministic projection over the event log (`world/state.py`).
- First personality: one policy over the salience stream, with visible
  reactions. History comes from `scripts/backfill_world_events.py`, which
  replays the salience rules over ~60 days of klines — the live log itself
  only went back 3 days when Sprint 12 started, so backfilled events are
  flagged `backfilled: true` to keep learned and witnessed history distinct.
```

Both corrections matter: the WebSocket claim would send a future reader building
transport that already exists as SSE, and the "~2 months" claim was simply false.

- [ ] **Step 5: Correct the same WebSocket claim in `docs/architecture-vision.md`**

The world-page claim is at `docs/architecture-vision.md:111` (`driven by the world event stream over WebSocket`). Change `over WebSocket` to `over SSE`.

Leave the other matches alone — they are about different, accurate things: line 106 already says "WebSockets/SSE" for a future frontend, and lines 79-83, 119, 122, 148 refer to the Binance kline websocket and obs-websocket, both of which genuinely are websockets.

Run: `grep -n -i "websocket" docs/architecture-vision.md docs/roadmap.md`
Expected: no remaining match describes the *world page* as WebSocket-driven.

- [ ] **Step 6: Update `CLAUDE.md`**

Extend the `world/` bullet in the Architecture section:

```markdown
- `world/` — Living World memory (Sprint 9) + accountability loop (Sprint 10) + renderer projection (Sprint 12): `salience.py`, `events.py`, `resolver.py` (as before), plus `state.py` (pure `project_state()` fold + per-rule `severity_tier()` 0–3 normalization — determinism and chunk-invariance are property-tested), `reactions.py` (`reaction_for()` / `attach_reactions()`, registry-invariant over `KNOWN_EVENT_TYPES`), and `trader_events.py` (freqtrade sidecar mirrored to `trader_*` events over REST; GPL boundary — never imported). Pages: `/world` (PixiJS room, `docs/world-renderer.md`) alongside `/overlay/signals` and `/overlay/events`. `scripts/backfill_world_events.py` replays salience over ~60 days of klines (flagged `backfilled: true`, idempotent via `uq_world_events_natural` + `scripts/migrate_012.sql`).
```

Add to the Commands block:

```bash
# Give the world a past: replay salience over ~60 days of klines (re-runnable)
poetry run python scripts/backfill_world_events.py --days 60
```

- [ ] **Step 7: Update `README.md`**

Add `/world/state` and `/world` to the endpoint list, and the backfill script to the scripts section, matching the surrounding style.

- [ ] **Step 8: Refresh the knowledge graph**

Run: `graphify update .`
Expected: completes with no API cost (AST-only).

- [ ] **Step 9: Final verification pass**

```bash
poetry run pytest -q
ruff check .
sg docker -c "docker compose up -d --build"
sg docker -c "docker compose ps"
curl -s localhost:8000/world/state | head -c 400
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/world
```
Expected: all tests pass; ruff clean; four services healthy (three if Tasks 8–9 were cut); `/world/state` returns a populated projection; `/world` returns 200.

- [ ] **Step 10: Commit the code and docs**

```bash
git add docs/world-renderer.md docs/roadmap.md docs/architecture-vision.md \
  README.md CLAUDE.md tests/integration/test_world_renderer.py graphify-out/
git commit -m "Docs: world renderer design; correct SSE and history claims"
```

- [ ] **Step 11: Update the Obsidian vault and close the sprint**

In `~/Documents/Obsidian Vault/Market Data Pipeline/Sprints/Sprint 12 — World Renderer v0.md`:
- Tick every completed ticket and update the `**0/10 done**` counter.
- Set frontmatter `status: Completed` and `completion: 1` (or the honest fraction if Tasks 8–9 were cut).
- Add a `## Retrospective` section: what shipped, what was cut and why, and anything the implementation contradicted about the plan.

In `Sprint Tracker.md`, update the Sprint 12 row's Status and Done columns.

This is the standing cadence — the vault gets updated after every sprint planning and every sprint finish, without being asked.

- [ ] **Step 12: Report the honest state to the user**

State plainly: which of the ten tasks landed, whether the freqtrade pair was cut, the final test count, and whether the backfill's second run really wrote zero rows. If the live-append behaviour check in Task 7 Step 10 surfaced a production risk, lead with that.

---

## Risks

- **The sprint is genuinely full.** New renderer, new projection layer, a second inhabitant, and a backfill in one week. Tasks 8–9 are the designed cut line.
- **Binance pagination is new code** on a provider that has only ever done single requests. ~87 sequential calls per symbol; the `sleep_seconds` default of 0.25s exists to respect the weight limit, and the no-forward-progress guard exists so a malformed response can't spin forever.
- **The unique index changes live runtime behaviour**, not just backfill behaviour. Task 7 Step 10 pins this deliberately rather than asserting it.
- **The model is losing** — 178W/215L, 45.3% hit rate. Honest, and it is content, but the room must not be designed to look celebratory by default.
- **No CSP exists anywhere in the repo.** Not a blocker (the SRI pin is the active protection), but `/world` makes it a third page loading a third-party library, which raises the value of adding one.
- **`docs/postgres-schema-spike.md` is stale** — it predates Sprint 9 and documents neither `world_events` nor `signals`. `db/init.sql` is the real source of truth.
