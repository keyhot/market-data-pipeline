# Sprint 13 — Director & Personalities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The stream directs itself — salience-driven OBS scene switching and in-character commentary — with personalities expressed as policies with different thresholds over the same `world_events` stream. No LLM: commentary is deterministic phrase banks.

**Architecture:** A director service shaped exactly like `stream_watchdog.py` — a pure `tick(state, dir_state, now) -> DirectorAction` state machine plus a thin runner that polls `GET /world/state` (the Sprint 12 projection), decides a scene and zero-or-more commentary lines, and applies them through injected clients (OBS via `stream_ctl`, TTS via a subprocess runner). Every decision is a pure function of world state + director state, so all logic tests with no OBS, no Piper, no clock, no DB. The director's own actions (`scene_switched`, `commentary_spoken`) are appended to the append-only `world_events` log, so the world remembers what the director did.

**Tech Stack:** existing `scripts/stream_ctl.py` (obsws-python) extended to multi-scene; `scripts/stream_scene.py` extended to multiple scenes; the Sprint 12 `world/state.py` projection; Piper TTS (local, offline, free); Postgres `world_events`; pytest with fake OBS/Piper clients (no OBS, no Piper, no network in tests).

## Global Constraints

- Python 3.11, poetry-managed. **Run tests as `poetry run pytest`** — bare `pytest` hits the anaconda base env and is missing deps.
- Commit style: `Type: short description`. **NEVER add a `Co-Authored-By` line or any AI-attribution trailer.** Hard project rule.
- `ruff check .` clean before every commit; line length 88; imports sorted.
- Run `graphify update .` after code changes (AST-only, no API cost).
- **`world_events` is append-only** — the director only INSERTs (`postgres_store.append_world_events`); never UPDATE/DELETE.
- **No LLM, no API cost.** Commentary is hard-coded per-character/per-event/per-tier phrase banks. LLM commentary is a documented deferral (Task 10).
- **No test may touch OBS, Piper, Postgres, or the network.** Inject clients as arguments (the `stream_ctl.py` client-first-argument seam) or monkeypatch the module-level store reference. `tests/conftest.py` forces `POSTGRES_WRITE_ENABLED=0`.
- **Truthfulness invariant.** Commentary may be dramatic, but every number a character cites comes from the event payload. A character may misread the market; the data it quotes stays exact.
- **Determinism.** Every RNG is injected so tests are seeded and reproducible. `choose_scene` and `tick` are pure.
- Secrets (`OBS_WS_PASSWORD`, etc.) live in `.env`, never in the repo.

## Decisions taken before implementation (from Sprint 12/13 planning)

- **Commentary is deterministic, not LLM-generated** — no Claude API, no per-line cost while there's no budget ([[project-no-paid-services-yet]]). LLM commentary is deferred with adoption triggers (Task 10).
- **Multi-scene is an unlisted prerequisite** — `scene_spec()` returns exactly one scene and `stream_ctl.py` has no scene-switch function. Task 1 refactors both; it blocks Tasks 2–3.
- **Personalities are threshold policies over the same events** — frozen dataclasses shaped like `SalienceConfig`, differing in reaction tier and which event types they care about. Same events, different behavior; no scripted dialogue trees.
- **The director follows the `stream_watchdog.py` shape** — pure `tick()` + runner, so all logic is testable with no OBS/clock/DB.
- **The world state the director reads is the Sprint 12 `GET /world/state`** — `project_state` + `attach_reactions`, already deterministic, already carrying `severity_tier`, `recent`, per-symbol mood, model record, trader, history.

## Verified anchors (2026-07-24, against `main`)

- `scripts/stream_scene.py`: `scene_spec() -> dict` = `{"scene": SCENE_NAME, "canvas": CANVAS, "sources": [...]}`; `SCENE_NAME = "market-world-v1"`, `CANVAS = (1920, 1080)`.
- `scripts/stream_ctl.py`: functions take `client` first — `build_scene(client, spec)`, `start_stream(client)`, `stop_stream(client)`, `get_status(client)`, `screenshot(client, path, ...)`, `make_client(timeout)`. Scene switch uses `client.set_current_program_scene(name)`.
- `scripts/stream_watchdog.py`: `@dataclass(frozen=True)` config, `@dataclass WatchdogState`, pure `tick(...)`, `main()` runner. Copy this shape.
- `api/metrics.py`: `MetricsRegistry` with `record(...)`, `snapshot()`, `reset()`.
- `world/salience.py`: `KNOWN_EVENT_TYPES` frozenset (13 types incl. `trader_*`); a registry-invariant test pattern already exists (`tests/unit/test_reactions.py`).
- `world/stream_events.py`: the JSONL-spool pattern (`record_stream_event`, `_default_spool`) — reuse for director events.
- `/world` (Sprint 12) and `/overlay/signals`, `/overlay/events` are the Browser Source pages the three scenes compose.

---

### Task 1: Multi-scene refactor — `scenes_spec()` + `switch_scene()`

**Files:**
- Modify: `scripts/stream_scene.py`, `scripts/stream_ctl.py`
- Test: `tests/unit/test_stream_scene.py`, `tests/unit/test_stream_ctl.py`

**Interfaces:**
- Produces: `stream_scene.scenes_spec() -> list[dict]` — each `{"scene": name, "canvas": CANVAS, "sources": [...]}`. Three scenes: `chart-focus`, `world-focus`, `event-focus`. `scene_spec()` stays as a thin shim returning the first scene so nothing else breaks.
- Produces: `stream_ctl.switch_scene(client, scene_name: str) -> None` (client-first seam).
- Consumes: existing `build_scene`, the `/chart`, `/world`, `/overlay/events`, `/overlay/signals` pages.

- [ ] **Step 1: Write the failing tests** — extend `tests/unit/test_stream_scene.py`:

```python
def test_scenes_spec_has_three_named_scenes():
    from scripts import stream_scene
    names = [s["scene"] for s in stream_scene.scenes_spec()]
    assert names == ["chart-focus", "world-focus", "event-focus"]


def test_every_scene_geometry_is_within_canvas():
    from urllib.parse import urlparse
    from scripts import stream_scene
    for scene in stream_scene.scenes_spec():
        cw, ch = scene["canvas"]
        for src in scene["sources"]:
            s = src["settings"]
            if "width" not in s:
                continue
            assert 0 <= src["x"] and src["x"] + s["width"] <= cw, src["name"]
            assert 0 <= src["y"] and src["y"] + s["height"] <= ch, src["name"]


def test_every_scene_url_hits_a_real_route():
    from urllib.parse import urlparse
    from scripts import stream_scene
    from api.main import app
    for scene in stream_scene.scenes_spec():
        for src in scene["sources"]:
            url = src["settings"].get("url")
            if url is None:
                continue
            path = urlparse(url).path
            assert any(
                getattr(r, "path_regex", None) is not None
                and r.path_regex.match(path)
                for r in app.routes
            ), url


def test_scene_spec_shim_returns_the_first_scene():
    from scripts import stream_scene
    assert stream_scene.scene_spec() == stream_scene.scenes_spec()[0]
```

And extend `tests/unit/test_stream_ctl.py` (reuse the existing hand-rolled `FakeClient`):

```python
def test_switch_scene_sets_the_program_scene():
    from scripts import stream_ctl
    client = FakeClient()  # existing fake
    stream_ctl.switch_scene(client, "world-focus")
    assert client.current_program_scene == "world-focus"
```

Confirm the existing `FakeClient` records `set_current_program_scene`; if not, extend it (it already backs `build_scene`'s `set_current_program_scene` call).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/unit/test_stream_scene.py tests/unit/test_stream_ctl.py -q`
Expected: FAIL — `AttributeError: module 'scripts.stream_scene' has no attribute 'scenes_spec'` / `stream_ctl has no attribute 'switch_scene'`.

- [ ] **Step 3: Refactor `scripts/stream_scene.py`**

Keep `SCENE_NAME`/`CANVAS`. Extract the existing single scene's sources into named layouts and add:

```python
def scenes_spec() -> list[dict]:
    """The scenes the director switches between. chart-focus is the default
    (== the legacy single scene); world-focus foregrounds the /world room;
    event-focus foregrounds the world-event feed. All three reuse existing
    Browser Source pages — no new asset pipeline."""
    return [
        _chart_focus_scene(),
        _world_focus_scene(),
        _event_focus_scene(),
    ]


def scene_spec() -> dict:
    """Back-compat shim: the single-scene callers (watchdog, existing tests)
    get the default scene."""
    return scenes_spec()[0]
```

Define `_chart_focus_scene()` as the current `scene_spec()` body renamed to `"chart-focus"`. `_world_focus_scene()` and `_event_focus_scene()` reuse `CANVAS` with the `/world`, `/overlay/events`, `/overlay/signals`, `/chart/BTCUSDT?interval=1m` pages at different sizes/z-orders (world large + signals strip; event feed large + chart small). Every source `settings["url"]` must resolve to a real route (the test enforces it). Keep source names unique **within** each scene.

- [ ] **Step 4: Add `switch_scene` to `scripts/stream_ctl.py`**

After `build_scene` (client-first, matching the module's documented seam):

```python
def switch_scene(client, scene_name: str) -> None:
    """Set the active program scene. The director calls this; the CLI wraps it."""
    client.set_current_program_scene(scene_name)
```

`build_scene` must build **all** scenes idempotently: make `build_scene(client, spec=None)` loop over `scenes_spec()` when `spec` is None, creating each scene + its sources, refreshing settings on existing inputs (never duplicating). Preserve the single-`spec` path for callers that pass one. Add a CLI subcommand `switch <scene>` alongside the existing `build`/`start`/`stop`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `poetry run pytest tests/unit/test_stream_scene.py tests/unit/test_stream_ctl.py -q`
Expected: all pass, including the existing single-scene tests (the shim keeps them green).

- [ ] **Step 6: Idempotence check on the fake**

Add a test that `build_scene(client)` called twice creates each scene/input once (no duplicates) — assert the fake's create-counts are 1 per name. This is the property the real OBS relies on.

- [ ] **Step 7: Lint and commit**

```bash
ruff check scripts/stream_scene.py scripts/stream_ctl.py tests/unit/test_stream_scene.py tests/unit/test_stream_ctl.py
git add scripts/stream_scene.py scripts/stream_ctl.py tests/unit/test_stream_scene.py tests/unit/test_stream_ctl.py
git commit -m "Stream: multi-scene SCENE_SPEC + switch_scene on the client seam"
```

---

### Task 2: Director scaffold — pure `tick()` + runner + compose service

**Files:**
- Create: `director/__init__.py`, `director/policy.py`, `director/service.py`
- Modify: `config/exceptions.py`, `docker-compose.yml`, `.env.example`
- Test: `tests/unit/test_director_policy.py`

**Interfaces:**
- Produces: `director.policy.DirectorConfig` (frozen dataclass), `director.policy.DirectorState` (mutable dataclass), `director.policy.DirectorAction` (dataclass: `scene: str | None`, `lines: list[dict]`), `director.policy.tick(state: dict, dir_state: DirectorState, now: datetime, config: DirectorConfig) -> DirectorAction` (pure — no OBS, no clock, no DB).
- Produces: `director.service.run()` runner (polls `/world/state`, calls `tick`, applies via injected clients) + `main()`.
- Consumes: Task 1's `switch_scene`, Task 3's `choose_scene`, Task 4's commentary, the Sprint 12 `/world/state`.

Follow `scripts/stream_watchdog.py` **exactly**: pure `tick` + thin runner, `init_logging()` (note: `init_logging`, not `setup_logging`), config as a frozen dataclass.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_director_policy.py`:

```python
from datetime import datetime, timezone

from director.policy import DirectorConfig, DirectorState, tick

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _state(**kw):
    base = {"recent": [], "symbols": {}, "model": {}, "trader": None,
            "stream": {"state": "live"}, "history": {}}
    base.update(kw)
    return base


def test_tick_is_pure_no_side_effects():
    cfg = DirectorConfig()
    ds = DirectorState(current_scene="chart-focus", last_switch=BASE)
    a1 = tick(_state(), ds, BASE, cfg)
    a2 = tick(_state(), ds, BASE, cfg)
    assert a1 == a2  # same inputs -> same action
    assert ds.current_scene == "chart-focus"  # tick mutates nothing on dir_state


def test_tick_returns_no_scene_change_when_nothing_salient():
    cfg = DirectorConfig()
    ds = DirectorState(current_scene="chart-focus", last_switch=BASE)
    action = tick(_state(), ds, BASE, cfg)
    assert action.scene in (None, "chart-focus")
    assert action.lines == []
```

- [ ] **Step 2: Run to verify they fail** — `ModuleNotFoundError: No module named 'director'`.

- [ ] **Step 3: Write `director/policy.py`**

```python
"""Director decision layer (Sprint 13): pure functions over world state.

Shaped like scripts/stream_watchdog.py — tick() decides, the runner acts — so
every decision tests with no OBS, no Piper, no clock, and no DB.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DirectorConfig:
    min_dwell_seconds: int = 45        # scene must hold this long before switching
    max_switches_per_minute: int = 3
    max_lines_per_minute: int = 6
    anti_repeat_window: int = 8        # don't reuse a character's last N lines


@dataclass
class DirectorState:
    current_scene: str
    last_switch: datetime
    recent_switch_times: list = field(default_factory=list)
    recent_line_times: list = field(default_factory=list)
    recent_lines_by_character: dict = field(default_factory=dict)
    last_seen_event_id: int | None = None
    muted: bool = False


@dataclass
class DirectorAction:
    scene: str | None = None            # None => hold current scene
    lines: list = field(default_factory=list)  # [{"character","text","voice","event_id"}]


def tick(state, dir_state, now, config):
    """Pure: given world state + director state + clock, return the action.
    Mutates nothing. The runner applies the action and updates dir_state."""
    from director.scenes import choose_scene       # Task 3
    from director.commentary import lines_for_tick  # Task 4/5

    scene = choose_scene(state, dir_state, now, config)
    lines = [] if dir_state.muted else lines_for_tick(state, dir_state, now, config)
    return DirectorAction(
        scene=scene if scene != dir_state.current_scene else None,
        lines=lines,
    )
```

(`choose_scene`/`lines_for_tick` are stubbed to return `dir_state.current_scene` / `[]` until Tasks 3–4 land, so this task's tests pass first.)

- [ ] **Step 4: Write `director/service.py`** — the runner, mirroring `stream_watchdog.main()`:

```python
"""Director runner: poll /world/state, tick(), apply via injected clients.
All I/O lives here; director.policy stays pure."""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from director.policy import DirectorConfig, DirectorState, tick  # noqa: E402

logger = logging.getLogger(__name__)


def director_enabled() -> bool:
    raw = os.environ.get("DIRECTOR_ENABLED")
    return raw is None or raw.strip().lower() not in {"0", "false", "no"}


def run(fetch_state, obs_client, tts_runner, record_event,
        config=None, sleep_seconds=5.0) -> None:
    """Injected: fetch_state() -> dict, obs_client (stream_ctl seam),
    tts_runner (Task 6), record_event(events) (Task 7). No globals -> testable."""
    config = config or DirectorConfig()
    dir_state = DirectorState(current_scene="chart-focus",
                              last_switch=datetime.now(timezone.utc))
    while True:
        now = datetime.now(timezone.utc)
        try:
            state = fetch_state()
            action = tick(state, dir_state, now, config)
            _apply(action, dir_state, now, obs_client, tts_runner, record_event)
        except Exception as e:  # a director hiccup must never take the stream down
            logger.warning("Director tick failed", extra={"error": str(e)})
        time.sleep(sleep_seconds)
```

`_apply` performs the scene switch (`stream_ctl.switch_scene`), speaks lines (Task 6), appends `scene_switched`/`commentary_spoken` events (Task 7), updates `dir_state` (last_switch, recent_* windows, recent_lines_by_character), and feeds metrics (Task 8). Keep `_apply` thin; all *decisions* are in `tick`. `main()` wires the real clients and calls `run`.

- [ ] **Step 5: Add `DirectorUnreachableError` to `config/exceptions.py`** (503, matching the hierarchy).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `poetry run pytest tests/unit/test_director_policy.py -q`
Expected: pass.

- [ ] **Step 7: Add the compose service**

In `docker-compose.yml`, a `director` service (same image as api/scheduler, different command: `python director/service.py`), `DIRECTOR_ENABLED`, `OBS_WS_URL`/`OBS_WS_PASSWORD`, `STREAM_PAGE_BASE` from `.env`. No published port. Add `DIRECTOR_ENABLED=1` and the Piper env (Task 6) to `.env.example`.

- [ ] **Step 8: Lint and commit**

```bash
ruff check director/ tests/unit/test_director_policy.py
git add director/ config/exceptions.py docker-compose.yml .env.example tests/unit/test_director_policy.py
git commit -m "Ops: director scaffold — pure tick() + runner, compose service"
```

---

### Task 3: Salience-driven scene selection — `choose_scene` with minimum dwell

**Files:**
- Create: `director/scenes.py`
- Test: `tests/unit/test_director_scenes.py`

**Interfaces:**
- Produces: `director.scenes.choose_scene(state: dict, dir_state: DirectorState, now: datetime, config: DirectorConfig) -> str` — pure; returns the scene the director *should* be on (may equal the current one).
- Consumes: the Sprint 12 state's `recent` (each carries `tier`), `symbols`, `stream`; `DirectorState.current_scene` / `last_switch`.

**The minimum dwell time is the single most important property** — a burst of high-severity events must not cause seizure-inducing scene flapping. Mutation-check it (invert the dwell comparison, confirm a specific test fails, revert), the way the watchdog backoff was verified.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_director_scenes.py`:

```python
from datetime import datetime, timedelta, timezone

from director.policy import DirectorConfig, DirectorState
from director.scenes import choose_scene

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _ev(event_type, tier, minute=0, symbol="BTCUSDT"):
    return {"id": minute + 1, "event_type": event_type, "tier": tier,
            "symbol": symbol, "occurred_at": (BASE + timedelta(minutes=minute)).isoformat(),
            "payload": {}}


def _state(recent):
    return {"recent": recent, "symbols": {}, "stream": {"state": "live"}}


def test_high_tier_market_event_selects_chart_focus():
    ds = DirectorState(current_scene="world-focus", last_switch=BASE - timedelta(minutes=5))
    scene = choose_scene(_state([_ev("big_move", 3)]), ds, BASE, DirectorConfig())
    assert scene == "chart-focus"


def test_model_or_trader_event_selects_world_focus():
    ds = DirectorState(current_scene="chart-focus", last_switch=BASE - timedelta(minutes=5))
    scene = choose_scene(_state([_ev("signal_resolved", 3)]), ds, BASE, DirectorConfig())
    assert scene == "world-focus"


def test_minimum_dwell_blocks_a_switch_before_the_dwell_elapses():
    # A dramatic event arrives 10s after the last switch; dwell is 45s.
    ds = DirectorState(current_scene="world-focus", last_switch=BASE)
    scene = choose_scene(_state([_ev("big_move", 3)]), ds,
                         BASE + timedelta(seconds=10), DirectorConfig())
    assert scene == "world-focus"  # held: dwell not elapsed


def test_switch_allowed_after_dwell_elapses():
    ds = DirectorState(current_scene="world-focus", last_switch=BASE)
    scene = choose_scene(_state([_ev("big_move", 3)]), ds,
                         BASE + timedelta(seconds=60), DirectorConfig())
    assert scene == "chart-focus"


def test_quiet_world_holds_the_current_scene():
    ds = DirectorState(current_scene="event-focus", last_switch=BASE - timedelta(minutes=5))
    assert choose_scene(_state([]), ds, BASE, DirectorConfig()) == "event-focus"
```

- [ ] **Step 2: Run to verify they fail** — `ModuleNotFoundError`.

- [ ] **Step 3: Write `director/scenes.py`**

```python
"""Scene selection (Sprint 13): a pure policy over the world state. The dwell
guard is the safety-critical part — without it a burst of events flaps scenes."""

# Which scene each event class wants to foreground. Keyed on event type, not
# raw severity — tiers are the normalized scale (world/state.severity_tier).
_MARKET_TYPES = frozenset({"big_move", "volatility_spike", "gap_open",
                           "volume_anomaly", "streak"})
_MODEL_TYPES = frozenset({"signal_resolved", "model_losing_streak",
                          "trader_opened", "trader_closed", "trader_milestone"})

_SCENE_FOR_INTENT = {"market": "chart-focus", "model": "world-focus",
                     "event": "event-focus"}
_SWITCH_TIER = 2  # only tier >= this is worth interrupting the current scene


def _desired_scene(state):
    """The scene the most salient recent event points to, or None if quiet."""
    best = None
    for event in state.get("recent", []):
        if event.get("tier", 0) < _SWITCH_TIER:
            continue
        if event["event_type"] in _MARKET_TYPES:
            intent = "market"
        elif event["event_type"] in _MODEL_TYPES:
            intent = "model"
        else:
            intent = "event"
        # recent is newest-first, so the first qualifying event wins.
        best = _SCENE_FOR_INTENT[intent]
        break
    return best


def choose_scene(state, dir_state, now, config):
    desired = _desired_scene(state)
    if desired is None or desired == dir_state.current_scene:
        return dir_state.current_scene
    dwell = (now - dir_state.last_switch).total_seconds()
    if dwell < config.min_dwell_seconds:
        return dir_state.current_scene   # hold — dwell not elapsed
    return desired
```

- [ ] **Step 4: Run to verify they pass** — `5 passed`.

- [ ] **Step 5: Mutation-check the dwell guard**

Temporarily change `if dwell < config.min_dwell_seconds:` to `if dwell > config.min_dwell_seconds:`.
Run: `poetry run pytest tests/unit/test_director_scenes.py -q`
Expected: `test_minimum_dwell_blocks_a_switch_before_the_dwell_elapses` FAILS (a burst now flaps). **Revert.** If it still passes, the dwell test isn't constraining the guard — fix it before continuing.

- [ ] **Step 6: Wire `choose_scene` into `tick`** (replace the Task 2 stub import) and lint + commit.

```bash
git add director/scenes.py director/policy.py tests/unit/test_director_scenes.py
git commit -m "Stream: salience-driven scene selection with a mutation-checked dwell guard"
```

---

### Task 4: Phrase-bank commentary — `line_for` + anti-repetition

**Files:**
- Create: `director/phrases.py`, `director/commentary.py`
- Test: `tests/unit/test_commentary.py`

**Interfaces:**
- Produces: `director.phrases.PHRASES` (nested dict: character → event_type → tier → list[str]), `director.commentary.line_for(character: str, event: dict, tier: int, rng, recent: list[str]) -> str | None`, `director.commentary.lines_for_tick(state, dir_state, now, config) -> list[dict]`.
- Consumes: `KNOWN_EVENT_TYPES` + `trader_*` (registry invariant); the personalities from Task 5.

**Truthfulness rule:** phrases may be dramatic, but any number in a line comes from the event payload (via a format template applied to `event["payload"]`), never invented. **Anti-repetition:** never repeat a character's last `anti_repeat_window` lines.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_commentary.py`:

```python
import random

from director.commentary import line_for
from director.phrases import PHRASES
from world.salience import KNOWN_EVENT_TYPES

TRADER_TYPES = {"trader_opened", "trader_closed", "trader_milestone"}


def test_every_known_event_type_has_phrases_for_every_character():
    covered = set(KNOWN_EVENT_TYPES) | TRADER_TYPES
    for character, banks in PHRASES.items():
        missing = sorted(covered - set(banks))
        assert missing == [], f"{character} missing phrases for {missing}"


def test_line_is_deterministic_under_a_seeded_rng():
    ev = {"event_type": "big_move", "symbol": "BTCUSDT", "payload": {"sigmas": 8.0}}
    a = line_for("statistician", ev, 3, random.Random(1), [])
    b = line_for("statistician", ev, 3, random.Random(1), [])
    assert a == b and a is not None


def test_quoted_numbers_come_from_the_payload():
    ev = {"event_type": "big_move", "symbol": "BTCUSDT", "payload": {"sigmas": 8.0}}
    line = line_for("statistician", ev, 3, random.Random(0), [])
    assert "8.0" in line or "8" in line  # the real sigma, never a made-up one


def test_anti_repetition_avoids_recent_lines():
    ev = {"event_type": "streak", "symbol": "BTCUSDT",
          "payload": {"bars": 9, "direction": "up"}}
    rng = random.Random(3)
    first = line_for("optimist", ev, 1, rng, [])
    second = line_for("optimist", ev, 1, rng, [first])
    assert second != first  # not an immediate repeat


def test_returns_none_when_the_character_has_no_line_for_the_tier():
    # A character that only speaks above a high tier stays silent below it.
    ev = {"event_type": "signal_resolved", "symbol": "BTCUSDT",
          "payload": {"outcome": "win"}}
    assert line_for("statistician", ev, 0, random.Random(0), []) is None
```

- [ ] **Step 2: Run to verify they fail** — `ModuleNotFoundError`.

- [ ] **Step 3: Write `director/phrases.py`**

A nested `PHRASES` dict: `character -> event_type -> tier -> [templates]`. Templates are `str.format`-style over the event payload, e.g. `"{sigmas:.0f}σ. That is not noise."`. Cover every character × every event type in `KNOWN_EVENT_TYPES ∪ trader_*` (the registry test enforces it). A character may have an **empty list** for a tier it ignores (→ `line_for` returns None) — that is how the statistician stays quiet on routine events. Keep numbers in templates sourced only from payload keys.

- [ ] **Step 4: Write `director/commentary.py`**

```python
def line_for(character, event, tier, rng, recent):
    """One line for this character reacting to this event at this tier, or None.
    RNG injected for reproducibility; `recent` is the character's recent lines
    for anti-repetition. Numbers are formatted from the payload only."""
    banks = PHRASES.get(character, {}).get(event["event_type"], {})
    templates = banks.get(tier) or banks.get(min(tier, max(banks) if banks else 0), [])
    if not templates:
        return None
    choices = [t for t in templates if _render(t, event) not in recent] or templates
    template = rng.choice(sorted(choices))  # sorted -> seeded rng is deterministic
    return _render(template, event)


def _render(template, event):
    try:
        return template.format(symbol=event.get("symbol", "the market"),
                               **(event.get("payload") or {}))
    except (KeyError, ValueError):
        return template  # a template that references a missing key degrades to text
```

`lines_for_tick` iterates the personalities (Task 5) over `state["recent"]` newest events not yet seen (`dir_state.last_seen_event_id`), calls `line_for` per personality, and returns line dicts. It is where anti-repetition state (`dir_state.recent_lines_by_character`) is *read* (the runner writes it).

- [ ] **Step 5: Run to verify they pass** — `5 passed`.

- [ ] **Step 6: Prove the registry invariant bites** — delete one character's `big_move` bank, run the registry test, confirm it fails, restore.

- [ ] **Step 7: Lint and commit.**

```bash
git commit -m "World: deterministic phrase-bank commentary with anti-repetition + registry invariant"
```

---

### Task 5: Personalities as threshold policies

**Files:**
- Create: `director/personalities.py`
- Test: `tests/unit/test_personalities.py`

**Interfaces:**
- Produces: `director.personalities.Personality` (frozen dataclass: `name`, `voice`, `min_tier: int`, `event_types: frozenset[str]`, `phrase_character: str`), `director.personalities.PERSONALITIES: tuple[Personality, ...]`, `director.personalities.reacts_to(p, event, tier) -> bool`.
- Consumes: nothing (pure config), shaped like `world.salience.SalienceConfig`.

Three personalities: **optimist** (reacts to wins/up-streaks, shrugs at losses — low tier on positive events, ignores negatives), **statistician** (only speaks at high tier, cares about `signal_resolved`/`model_losing_streak`), **anxious** (reacts to `volatility_spike`/`gap_open`/`streak` at low tier). Same event stream, different thresholds → genuinely different behavior.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_personalities.py`:

```python
from director.personalities import PERSONALITIES, reacts_to, Personality


def test_three_frozen_personalities():
    assert len(PERSONALITIES) >= 3
    for p in PERSONALITIES:
        assert isinstance(p, Personality)
        # frozen dataclass -> not mutable
        import dataclasses
        assert dataclasses.is_dataclass(p)


def test_statistician_stays_silent_below_its_high_tier():
    stat = next(p for p in PERSONALITIES if p.name == "statistician")
    ev = {"event_type": "signal_resolved", "payload": {"outcome": "loss"}}
    assert reacts_to(stat, ev, 1) is False
    assert reacts_to(stat, ev, 3) is True


def test_optimist_reacts_to_wins_and_shrugs_at_losses():
    opt = next(p for p in PERSONALITIES if p.name == "optimist")
    win = {"event_type": "signal_resolved", "payload": {"outcome": "win"}}
    loss = {"event_type": "signal_resolved", "payload": {"outcome": "loss"}}
    assert reacts_to(opt, win, 1) is True
    assert reacts_to(opt, loss, 1) is False


def test_same_stream_different_behavior():
    """The point of the sprint: identical events, measurably different reactions."""
    events = [({"event_type": "volatility_spike", "payload": {}}, 1),
              ({"event_type": "signal_resolved", "payload": {"outcome": "win"}}, 3)]
    reactions = {p.name: [reacts_to(p, e, t) for e, t in events] for p in PERSONALITIES}
    # No two personalities react identically to the whole stream.
    assert len({tuple(v) for v in reactions.values()}) == len(reactions)
```

- [ ] **Step 2: Run to verify they fail.** **Step 3: Write `director/personalities.py`** (frozen dataclasses + `reacts_to` checking `event_types` membership, `tier >= min_tier`, and per-personality payload predicates like optimist's win-only rule). **Step 4: Run to verify pass.** **Step 5: commit.**

```bash
git commit -m "World: personalities as frozen threshold policies over one event stream"
```

Wire `lines_for_tick` (Task 4) to iterate `PERSONALITIES`, gate on `reacts_to`, and draw from each personality's `phrase_character` bank.

---

### Task 6: Local TTS via Piper

**Files:**
- Create: `director/tts.py`
- Modify: `docker-compose.yml`, `.env.example`
- Test: `tests/unit/test_tts.py`

**Interfaces:**
- Produces: `director.tts.synthesize(text: str, voice: str, out_path: Path, runner=subprocess.run) -> Path | None` — runner injected so tests never invoke the binary; returns the path on success, `None` on failure (never raises).
- Consumes: Piper (local, offline).

**Must degrade gracefully** — if Piper is missing or fails, the stream keeps running silently and the director records the failure (a metric + a log), never crashes.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_tts.py`:

```python
from pathlib import Path
from director.tts import synthesize


def test_synthesize_invokes_the_runner_with_voice_and_returns_path(tmp_path):
    calls = []
    def fake_runner(cmd, **kw):
        calls.append(cmd)
        Path(cmd[cmd.index("-f") + 1]).write_bytes(b"RIFF")  # pretend wav
        class R: returncode = 0
        return R()
    out = synthesize("hello", "en_US-amy", tmp_path / "l.wav", runner=fake_runner)
    assert out == tmp_path / "l.wav" and out.exists()
    assert any("amy" in part for part in calls[0])


def test_synthesize_returns_none_when_piper_fails(tmp_path):
    def failing_runner(cmd, **kw):
        raise FileNotFoundError("piper not installed")
    assert synthesize("hi", "v", tmp_path / "x.wav", runner=failing_runner) is None


def test_synthesize_returns_none_on_nonzero_exit(tmp_path):
    def bad_runner(cmd, **kw):
        class R: returncode = 1
        return R()
    assert synthesize("hi", "v", tmp_path / "x.wav", runner=bad_runner) is None
```

- [ ] **Step 2–4:** verify-fail, write `director/tts.py` (build the Piper command, `runner(cmd)`, catch every exception → return None, check returncode), verify-pass.

- [ ] **Step 5:** Compose — mount a Piper voices volume, `PIPER_BIN`/`PIPER_VOICE_DIR` env; audio routed into OBS as a media source (documented in Task 10, not automated). Commit.

```bash
git commit -m "Ops: local Piper TTS with an injected runner; degrades to silence on failure"
```

---

### Task 7: Commentary + scene changes as world events

**Files:**
- Modify: `world/salience.py` (`KNOWN_EVENT_TYPES`), `world/reactions.py` (`REACTIONS`), `api/templates/overlay_events.html` (`HEADLINES`), `api/templates/world.html` (`MOOD_COLOR` if new moods), `director/service.py`
- Create: `director/events.py`
- Test: `tests/unit/test_director_events.py`

**Interfaces:**
- Produces: `director.events.build_scene_switched(...)`, `director.events.build_commentary_spoken(...)`, `director.events.record_director_events(events, spool=...)` — reuse the **JSONL spool pattern** from `world/stream_events.py` so a Postgres outage never loses director history; flush on the director's own tick, not on the next external event.
- Registers `scene_switched`, `commentary_spoken` in `KNOWN_EVENT_TYPES` + all three registries (the Task 3-style invariant tests enforce coverage).

- [ ] **Step 1:** failing tests — the registry-subset invariant (`{"scene_switched","commentary_spoken"} <= KNOWN_EVENT_TYPES <= set(REACTIONS)`), a diff-to-events test, and a spool-on-DB-down test (monkeypatch `append_world_events` to raise → assert the JSONL spool file gets the rows).
- [ ] **Step 2–6:** register the types everywhere, write `director/events.py` (copy `world/stream_events.py`'s spool structure), wire `_apply` (Task 2) to record them, verify the reactions registry test still passes with the two new types, commit.

```bash
git commit -m "World: director actions (scene_switched, commentary_spoken) as spooled world events"
```

**Truthfulness note:** `commentary_spoken` payload stores the exact line text and the source `event_id`, so the log can later prove which real event each line reacted to.

---

### Task 8: Director observability + safety rails

**Files:**
- Modify: `director/service.py`, `api/main.py` (`/metrics`), `api/metrics.py` if needed
- Test: `tests/unit/test_director_rails.py`

**Interfaces:**
- Produces: rate-limit helpers `within_switch_budget(dir_state, now, config) -> bool`, `within_line_budget(dir_state, now, config) -> bool` (pure, sliding 60s window over `recent_switch_times`/`recent_line_times`); a global mute flag (`DirectorState.muted`, env `DIRECTOR_MUTED`); metrics counters for switches, lines spoken, lines suppressed (anti-repeat + budget), TTS failures.

A 24/7 autonomous system needs a brake that doesn't require a redeploy. The budgets are enforced in `_apply` (the runner), keyed off `tick`'s proposed action, so a runaway `tick` can't flap OBS or spam TTS.

- [ ] **Step 1:** failing tests — a burst of proposed switches within 60s is capped at `max_switches_per_minute`; a burst of lines capped at `max_lines_per_minute`; `muted=True` suppresses all lines; suppressed counts are recorded. Mutation-check the budget comparison the way Task 3 does the dwell.
- [ ] **Step 2–6:** implement the sliding-window budget helpers + counters, feed the existing `MetricsRegistry`, expose director counters under `/metrics`, commit.

```bash
git commit -m "Ops: director safety rails — per-minute switch/line budgets, global mute, metrics"
```

---

### Task 9: Integration test + director soak

**Files:**
- Create: `tests/integration/test_director.py`
- Modify: `scripts/soak_report.py`
- Test: `tests/unit/test_soak_report.py`

- [ ] **Step 1:** an end-to-end test with fakes throughout — a hand-built `/world/state` dict → `tick` → assert the chosen scene + the emitted lines, driving the real `choose_scene`/`lines_for_tick`/personalities/phrases (patch storage; no OBS/Piper/network). Include a burst case asserting the dwell + budget hold together.
- [ ] **Step 2:** extend `soak_report.py` to also report director activity from `scene_switched`/`commentary_spoken` events (lines/hour, switches/hour, suppression rate) alongside the existing uptime math; test it with the `_ev` helper.
- [ ] **Step 3:** run the whole suite (`poetry run pytest`), ruff, commit.

```bash
git commit -m "QA: director integration test + soak-report director activity"
```

---

### Task 10: Docs — director design + LLM-commentary deferral; sprint close

**Files:**
- Create: `docs/director.md`, `docs/llm-commentary-spike.md`
- Modify: `README.md`, `CLAUDE.md`, `docs/architecture-vision.md`, `docs/roadmap.md`
- Modify (vault): `Sprints/Sprint 13 — Director & Personalities.md`, `Sprint Tracker.md`

- [ ] **Step 1:** `docs/director.md` — the pure-`tick`/runner split, the three scenes, the dwell + budget rails, the personality-as-threshold-policy model, the truthfulness rule (numbers from payload), the spool. Note the OBS audio-source wiring for Piper and the `BrowserHWAccel=false` host gotcha.
- [ ] **Step 2:** `docs/llm-commentary-spike.md` — the deliberate deferral of Claude API commentary, following the Alpaca/freqtrade spike-and-defer format: the design considered, why deferred (no budget), explicit **adoption triggers**, and the **measured economics** so the call can be revisited on facts. At ~2K input + 120 output tokens/line and 50 lines/day: Opus 4.8 ≈ $0.013/line (~$20/mo), Sonnet 5 ≈ $0.005/line (~$8/mo at intro pricing through 2026-08-31), Haiku 4.5 ≈ $0.0026/line (~$4/mo). Caching caveat: prompt caching needs a ≥4096-token prefix on Opus 4.8 / Haiku 4.5, so a short persona prompt won't cache — the phrase-bank approach has zero marginal cost and the LLM approach's economics assume no caching benefit for short prompts.
- [ ] **Step 3:** update `README.md`/`CLAUDE.md` (director service, the `director/` package, the two new event types); correct any roadmap drift. `graphify update .`
- [ ] **Step 4:** full verification pass — `poetry run pytest` all green; `poetry run python -m scripts.stream_ctl build` creates three scenes and a second run reports no duplicates; commit.
- [ ] **Step 5:** update the Obsidian vault (tick tickets, `status: Completed`, `completion: 1`, retrospective, tracker row) — the standing sprint-finish cadence.
- [ ] **Step 6:** report the honest state: which tasks landed, test count, and the operational go-live steps that remain (Piper voices installed, OBS audio source wired, the director soak overlapping Sprint 11's still-pending 24h stream soak).

---

## Cross-cutting constraints

- **Append-only** — the director only appends `scene_switched`/`commentary_spoken`; never mutates `world_events`.
- **No LLM / no API cost** — phrase banks only; LLM is a documented deferral.
- **Determinism** — RNG injected everywhere; `tick`, `choose_scene`, budget/dwell helpers all pure.
- **No test touches OBS, Piper, Postgres, or the network** — inject clients (the `stream_ctl` seam) or monkeypatch the store.
- **Truthfulness** — numbers in a line come from the event payload; a character may misread, but quoted data is exact.
- **The dwell guard and the per-minute budgets are safety-critical** — both mutation-checked, the way the Sprint 11 watchdog backoff was.
- Commit style `Type: short description`, no `Co-Authored-By`. ruff clean. `graphify update .` after code changes.

## Verification

1. `poetry run pytest` — all green (roughly 360+ tests after the new suites).
2. `poetry run python -m scripts.stream_ctl build` builds three scenes; a second run reports no duplicate scenes/inputs (idempotence through the refactor).
3. Director running against live OBS: scenes switch on real salient events, the dwell holds under a burst (no flapping), lines are spoken with distinct Piper voices, and `DIRECTOR_MUTED=1` silences commentary without a redeploy.
4. `world_events` accumulates `scene_switched` + `commentary_spoken` rows — the world remembers what the director did.
5. `poetry run python scripts/soak_report.py` reports director activity alongside uptime.

Host gotcha (from Sprint 12): OBS on this machine needs `BrowserHWAccel=false` in `~/.config/obs-studio/global.ini`, or CEF's GPU process crashes when a browser-source scene loads.

## Risks

- **Content density is the real risk, not code.** The director makes quiet stretches less dead, but whether *a losing model + sparse events + scripted personalities* holds an audience is unproven and no test answers it. This is the sprint to get real broadcast evidence, not more features.
- **Scene flapping** is the most dangerous failure mode (seizure-inducing on a live stream) — the dwell guard is mutation-checked precisely because of this.
- **Piper voice files are large and not committed** — an operational install step, like the freqtrade strategy in Sprint 12.
- **The phrase banks are finite** — anti-repetition helps, but a 24/7 stream will exhaust them; the LLM deferral (Task 10) is the documented escape hatch when there's budget.
- **Depends on Sprint 12's `/world/state`** being live and the `/world` page rendering — carry over Sprint 12's go-live checklist (`docs/known-issues.md` and the Sprint 12 vault note).
