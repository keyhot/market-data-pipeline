"""Broadcast lifecycle `tick()` is a pure function over YouTube + OBS state.

The runner (broadcast/service.py) calls `tick`, then *applies* the returned
actions. No I/O here, no google imports, no clock — everything injected. These
tests pin that purity and cover the six cardinal cases (no broadcast →
create+bind; complete → recreate; backoff; ready→live grace; live→noop;
lifecycle transition recording).
"""

from datetime import datetime, timedelta, timezone

from broadcast.policy import BroadcastConfig, BroadcastState, tick

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
