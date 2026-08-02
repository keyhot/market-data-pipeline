"""Broadcast lifecycle `tick()` is a pure function over YouTube + OBS state.

The runner (broadcast/service.py) calls `tick`, then *applies* the returned
actions. No I/O here, no google imports, no clock — everything injected. These
tests pin that purity and cover the six cardinal cases (no broadcast →
create+bind; complete → recreate; backoff; ready→live grace; live→noop;
lifecycle transition recording).
"""

from datetime import datetime, timedelta, timezone

from broadcast.policy import (
    BroadcastConfig,
    BroadcastState,
    select_broadcast,
    tick,
)

BASE = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
CFG = BroadcastConfig(grace_seconds=30, create_backoff_seconds=120)
STREAM_ACTIVE = {"id": "s1", "status": "active"}


def _st(**kw):
    base = {
        "current_broadcast_id": None,
        "current_lifecycle": None,
        "last_create_at": None,
        "stream_active_since": None,
    }
    base.update(kw)
    return BroadcastState(**base)


def test_no_broadcast_and_streaming_creates_and_binds():
    yt = {"broadcast": None, "stream": STREAM_ACTIVE}
    _, actions = tick(yt, True, _st(), CFG, BASE)
    assert ("create_and_bind",) in actions


def test_complete_broadcast_is_recreated():
    yt = {
        "broadcast": {"id": "b1", "lifecycle": "complete", "bound_stream_id": "s1"},
        "stream": STREAM_ACTIVE,
    }
    _, actions = tick(yt, True, _st(current_broadcast_id="b1"), CFG, BASE)
    assert ("create_and_bind",) in actions


def test_create_respects_backoff():
    yt = {"broadcast": None, "stream": STREAM_ACTIVE}
    st = _st(last_create_at=BASE - timedelta(seconds=60))  # < 120 backoff
    _, actions = tick(yt, True, st, CFG, BASE)
    assert ("create_and_bind",) not in actions


def test_create_at_exact_backoff_boundary_is_allowed():
    """`>=` not `>` on the backoff — at exactly create_backoff_seconds, a
    retry is permitted (the mutation check the plan calls out)."""
    yt = {"broadcast": None, "stream": STREAM_ACTIVE}
    st = _st(last_create_at=BASE - timedelta(seconds=120))
    _, actions = tick(yt, True, st, CFG, BASE)
    assert ("create_and_bind",) in actions


def test_ready_broadcast_transitions_live_after_grace_while_active():
    yt = {
        "broadcast": {"id": "b1", "lifecycle": "ready", "bound_stream_id": "s1"},
        "stream": STREAM_ACTIVE,
    }
    st = _st(
        current_broadcast_id="b1",
        stream_active_since=BASE - timedelta(seconds=31),
    )
    _, actions = tick(yt, True, st, CFG, BASE)
    assert ("transition_live", "b1") in actions


def test_ready_broadcast_holds_within_grace():
    yt = {
        "broadcast": {"id": "b1", "lifecycle": "ready", "bound_stream_id": "s1"},
        "stream": STREAM_ACTIVE,
    }
    st = _st(
        current_broadcast_id="b1",
        stream_active_since=BASE - timedelta(seconds=10),
    )
    _, actions = tick(yt, True, st, CFG, BASE)
    assert not any(a[0] == "transition_live" for a in actions)


def test_live_and_active_is_noop():
    yt = {
        "broadcast": {"id": "b1", "lifecycle": "live", "bound_stream_id": "s1"},
        "stream": STREAM_ACTIVE,
    }
    _, actions = tick(
        yt, True, _st(current_broadcast_id="b1", current_lifecycle="live"), CFG, BASE
    )
    assert [a for a in actions if a[0] in ("create_and_bind", "transition_live")] == []


def test_going_live_records_broadcast_live_once():
    yt = {
        "broadcast": {"id": "b1", "lifecycle": "live", "bound_stream_id": "s1"},
        "stream": STREAM_ACTIVE,
    }
    st = _st(
        current_broadcast_id="b1",
        current_lifecycle="ready",  # was ready, now live
    )
    new, actions = tick(yt, True, st, CFG, BASE)
    assert ("record", "broadcast_live", {"broadcast_id": "b1"}) in actions
    assert new.current_lifecycle == "live"


def test_recreated_broadcast_records_created_after_a_completed_one():
    """The recreate path: after an ended broadcast the state carries
    current_lifecycle='complete', so a `broadcast_created` gated only on
    `current_lifecycle is None` would fire exactly once in the manager's life
    (first boot) and never again — every self-heal would go unrecorded."""
    yt = {
        "broadcast": {"id": "b2", "lifecycle": "ready", "bound_stream_id": "s1"},
        "stream": STREAM_ACTIVE,
    }
    st = _st(current_broadcast_id="b2", current_lifecycle="complete")
    _, actions = tick(yt, True, st, CFG, BASE)
    assert ("record", "broadcast_created", {"broadcast_id": "b2"}) in actions


# --- select_broadcast: list -> the one `tick` reasons about ---


def test_select_prefers_the_broadcast_we_created():
    ours = {"id": "b1", "lifecycle": "live"}
    other = {"id": "zz", "lifecycle": "ready"}
    assert select_broadcast([other, ours], "b1") == ours


def test_select_keeps_our_completed_broadcast_so_the_end_is_recorded():
    """Returning None for a completed broadcast would hide the broadcast_ended
    transition — and the uptime fold would then count it live to the end of
    the window, overstating public uptime. Never silently drop an ending."""
    ended = {"id": "b1", "lifecycle": "complete"}
    assert select_broadcast([ended], "b1") == ended


def test_select_ignores_stale_completed_broadcasts_on_a_cold_start():
    """A cold start with no known id must not pick a leftover `complete`
    broadcast — tick would read it as unhealthy and create a new one every
    backoff window, burning exactly the quota the backoff exists to protect."""
    stale = {"id": "old", "lifecycle": "complete"}
    healthy = {"id": "b1", "lifecycle": "live"}
    assert select_broadcast([stale, healthy], None) == healthy


def test_select_returns_none_when_nothing_is_usable():
    assert select_broadcast([{"id": "old", "lifecycle": "complete"}], None) is None
    assert select_broadcast([], None) is None


def test_select_requires_our_stream_binding_when_one_is_known():
    """The client lists ALL of the channel's broadcasts (a `persistent` filter
    can't see the ones we create — they carry a scheduledStartTime). So a
    healthy broadcast bound to some *other* stream must not be adopted: we'd
    transition a stranger's broadcast live. Ours is the one bound to our
    ingest stream."""
    theirs = {"id": "x", "lifecycle": "ready", "bound_stream_id": "other"}
    ours = {"id": "b1", "lifecycle": "ready", "bound_stream_id": "s1"}
    assert select_broadcast([theirs, ours], None, stream_id="s1") == ours
    assert select_broadcast([theirs], None, stream_id="s1") is None


def test_tick_is_pure_no_side_effects():
    """Same inputs → same actions; caller's state unchanged after the call."""
    yt = {"broadcast": None, "stream": STREAM_ACTIVE}
    st = _st()
    a1 = tick(yt, True, st, CFG, BASE)
    a2 = tick(yt, True, st, CFG, BASE)
    assert a1 == a2
    # `tick` must not mutate the caller's BroadcastState.
    assert st.current_broadcast_id is None
    assert st.last_create_at is None
