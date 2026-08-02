"""Broadcast lifecycle `tick()` (Sprint 14, Task A2).

A pure decision function over YouTube + OBS state — mirrors director/policy.py
so every decision tests with no network, no OAuth, no clock, no DB. The runner
(broadcast/service.py) calls `tick`, then *applies* the returned actions.

Decision order (each step short-circuits the next where appropriate):

  1. Compute `healthy` from the current YouTube broadcast lifecycle.
  2. If not healthy AND OBS is streaming AND backoff allows → emit
     `("create_and_bind",)` and stamp `last_create_at`. (Backoff stops a
     failing create from burning the YouTube daily quota.)
  3. Reconcile `stream_active_since` with current stream state.
  4. If lifecycle is `ready` / `testing` AND stream has been active past
     `grace_seconds` → emit `("transition_live", id)` (the explicit-fallback
     for the "stuck preparing" hang).
  5. Emit lifecycle-change `("record", event_type, payload)` actions by
     diffing the new lifecycle against `state.current_lifecycle`.

Mutation checks: `_backoff_ok` and the grace comparison both use `>=` so the
boundary case is a *pass*, not a hold — verified by
`test_create_at_exact_backoff_boundary_is_allowed` (a `>` mutation would
silently make that test fail).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# Lifecycle values the YouTube Data API returns for a persistent broadcast.
# "ready"/"testing" = bound, waiting for stream activity → eligible to go live.
# "live"           = public, streaming.
# "complete"       = ended; the persistent broadcast can be replaced.
# Anything else    = not currently usable; treated as "needs (re)creation".
_HEALTHY_LIFECYCLES = frozenset({"ready", "testing", "live"})


@dataclass(frozen=True)
class BroadcastConfig:
    """Tunables for the lifecycle `tick()`.

    `grace_seconds` is the explicit-transition fallback window: how long the
    stream must be active after a `ready` broadcast before we force
    `transition("live")` even if YouTube's auto-start hasn't fired.

    `create_backoff_seconds` is the quota guard: minimum gap between
    `insert_broadcast` attempts so a persistent failure can't burn the daily
    YouTube Data API quota.
    """

    grace_seconds: int = 30
    create_backoff_seconds: int = 120
    # `stream_title` must match the persistent ingest key's title in YouTube
    # Studio — that's how the manager finds the stream to bind to, and reusing
    # the existing key is what keeps the OBS side untouched.
    stream_title: str = "Market World"
    broadcast_title: str = "Market World — live"
    privacy: str = "public"


@dataclass
class BroadcastState:
    """Mutable, runner-owned state. `tick()` reads it and returns a *new*
    BroadcastState the runner swaps in — the caller's instance is never
    mutated (pinned by `test_tick_is_pure_no_side_effects`)."""

    current_broadcast_id: str | None
    current_lifecycle: str | None
    last_create_at: datetime | None
    stream_active_since: datetime | None


def _backoff_ok(state: BroadcastState, config: BroadcastConfig, now: datetime) -> bool:
    """`>=` on the backoff boundary — at exactly `create_backoff_seconds` a
    retry is permitted. (Plan mutation check: a `>` would silently break
    `test_create_at_exact_backoff_boundary_is_allowed`.)"""
    if state.last_create_at is None:
        return True
    return now - state.last_create_at >= timedelta(
        seconds=config.create_backoff_seconds
    )


def select_broadcast(broadcasts: list[dict], current_id: str | None) -> dict | None:
    """Pick the one broadcast `tick` reasons about out of the account's list.

    Order matters, and both rules exist to stop a specific failure:

    1. **Ours by id, whatever its lifecycle.** Including `complete` — dropping
       a completed broadcast here would hide the `broadcast_ended` transition,
       and the uptime fold would then count it live to the end of the window,
       overstating public uptime. An ending is never silently discarded.
    2. **Otherwise the first healthy one.** A cold start must not adopt a
       leftover `complete` broadcast: `tick` would read it as unhealthy and
       create a replacement every backoff window, burning the daily quota the
       backoff exists to protect.

    Returns None when nothing is usable — `tick` then creates one.
    """
    if current_id is not None:
        for broadcast in broadcasts:
            if broadcast.get("id") == current_id:
                return broadcast
    for broadcast in broadcasts:
        if broadcast.get("lifecycle") in _HEALTHY_LIFECYCLES:
            return broadcast
    return None


def _stream_active(yt_state: dict) -> bool:
    stream = yt_state.get("stream")
    return bool(stream) and stream.get("status") == "active"


def tick(
    yt_state: dict,
    obs_streaming: bool,
    state: BroadcastState,
    config: BroadcastConfig,
    now: datetime,
) -> tuple[BroadcastState, list[tuple]]:
    """Pure: given YouTube + OBS snapshot, return (new_state, actions).

    `actions` is a list of tuples. Three action shapes are emitted:

      - `("create_and_bind",)` — runner should call `insert_broadcast` then
        `bind_broadcast(stream_id)`.
      - `("transition_live", broadcast_id)` — runner should call
        `client.transition(id, "live")`.
      - `("record", event_type, payload)` — runner should append a
        `broadcast_*` world event via `broadcast/events.record_broadcast_events`
        (A3). `event_type` is one of `broadcast_created` / `broadcast_live` /
        `broadcast_ended`. `payload` is a plain dict.
    """
    actions: list[tuple] = []

    # Copy-into-new: `tick` must not mutate the caller's BroadcastState.
    new = BroadcastState(
        current_broadcast_id=state.current_broadcast_id,
        current_lifecycle=state.current_lifecycle,
        last_create_at=state.last_create_at,
        stream_active_since=state.stream_active_since,
    )

    broadcast = yt_state.get("broadcast")
    broadcast_lifecycle = broadcast.get("lifecycle") if broadcast else None
    broadcast_id = broadcast.get("id") if broadcast else None
    healthy = broadcast_lifecycle in _HEALTHY_LIFECYCLES

    stream_is_active = _stream_active(yt_state) and obs_streaming

    # --- (a) Reconcile stream-active timestamp ---
    if stream_is_active:
        if new.stream_active_since is None:
            new.stream_active_since = now
    else:
        new.stream_active_since = None

    # --- (b) Create / recreate if not healthy ---
    if not healthy and stream_is_active and _backoff_ok(state, config, now):
        actions.append(("create_and_bind",))
        new.last_create_at = now

    # --- (c) Grace-based explicit transition (fallback for stuck "ready") ---
    if (
        broadcast_lifecycle in {"ready", "testing"}
        and stream_is_active
        and new.stream_active_since is not None
        and now - new.stream_active_since >= timedelta(seconds=config.grace_seconds)
        and broadcast_id is not None
    ):
        actions.append(("transition_live", broadcast_id))

    # --- (d) Lifecycle-change recordings ---
    # The new broadcast is *known* once `create_and_bind` was emitted (above)
    # OR once YouTube has a usable broadcast. The state tracks
    # `current_lifecycle` so we only record on transitions.
    next_lifecycle = broadcast_lifecycle
    if next_lifecycle is not None and next_lifecycle != state.current_lifecycle:
        if next_lifecycle == "live":
            actions.append(("record", "broadcast_live", {"broadcast_id": broadcast_id}))
        elif next_lifecycle == "complete":
            actions.append(
                ("record", "broadcast_ended", {"broadcast_id": broadcast_id})
            )
        # A usable broadcast appearing where there wasn't one = just created.
        # `complete` counts as "wasn't one" alongside None: after a broadcast
        # ends, the state carries `complete`, and gating only on None would
        # record broadcast_created exactly once in the manager's lifetime —
        # every self-heal after the first would go unrecorded.
        elif (
            state.current_lifecycle in (None, "complete")
            and healthy
            and broadcast_id is not None
        ):
            actions.append(
                ("record", "broadcast_created", {"broadcast_id": broadcast_id})
            )

    new.current_broadcast_id = broadcast_id
    new.current_lifecycle = next_lifecycle

    return new, actions
