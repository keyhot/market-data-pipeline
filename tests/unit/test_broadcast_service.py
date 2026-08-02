"""The broadcast manager runner (Sprint 14, A4): a thin loop over the pure
`tick`, shaped like stream_watchdog / director. Everything is injected — no
network, no OAuth, no clock, no OBS — and the cardinal rule is that nothing
the YouTube API does can take the stream down.
"""

from datetime import datetime, timedelta, timezone

import pytest

from broadcast import service
from broadcast.policy import BroadcastConfig, BroadcastState

BASE = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
CFG = BroadcastConfig(grace_seconds=30, create_backoff_seconds=120)


class FakeYouTubeClient:
    """Stands in for YouTubeLiveClient: same method names, plain dicts, and a
    call log so tests assert on what the manager actually asked YouTube to do."""

    def __init__(self, broadcasts=None, stream=None):
        self.broadcasts = list(broadcasts or [])
        self.stream = stream or {"id": "s1", "status": "active", "title": "t"}
        self.calls = []
        self._next_id = 1

    def list_broadcasts(self):
        self.calls.append(("list_broadcasts",))
        return list(self.broadcasts)

    def find_stream(self, title):
        self.calls.append(("find_stream", title))
        return self.stream

    def insert_broadcast(self, title, privacy="public"):
        self.calls.append(("insert_broadcast", title, privacy))
        new = {"id": f"new-{self._next_id}", "lifecycle": "created"}
        self._next_id += 1
        self.broadcasts.append(new)
        return new

    def bind_broadcast(self, broadcast_id, stream_id):
        self.calls.append(("bind_broadcast", broadcast_id, stream_id))
        return {"id": broadcast_id, "bound_stream_id": stream_id}

    def transition(self, broadcast_id, status):
        self.calls.append(("transition", broadcast_id, status))
        for b in self.broadcasts:
            if b["id"] == broadcast_id:
                b["lifecycle"] = status
        return {"id": broadcast_id, "lifecycle": status}


def _state():
    return BroadcastState(None, None, None, None)


def test_fetch_state_shapes_the_dict_tick_expects():
    client = FakeYouTubeClient(broadcasts=[{"id": "b1", "lifecycle": "live"}])
    state = service.fetch_yt_state(client, _state(), CFG)
    assert state["broadcast"]["id"] == "b1"
    assert state["stream"]["status"] == "active"


def test_apply_creates_binds_and_adopts_the_new_broadcast_id():
    client = FakeYouTubeClient(broadcasts=[])
    state = _state()
    recorded = []
    service.apply(
        [("create_and_bind",)], state, client, recorded.append, CFG, BASE
    )
    kinds = [c[0] for c in client.calls]
    assert "insert_broadcast" in kinds and "bind_broadcast" in kinds
    # The runner owns this: without adopting the id, the next tick re-selects
    # the OLD broadcast and creates another one every backoff window.
    assert state.current_broadcast_id == "new-1"


def test_apply_transitions_live():
    client = FakeYouTubeClient(broadcasts=[{"id": "b1", "lifecycle": "ready"}])
    service.apply(
        [("transition_live", "b1")], _state(), client, lambda e: None, CFG, BASE
    )
    assert ("transition", "b1", "live") in client.calls


def test_apply_records_events_through_the_recorder():
    recorded = []
    service.apply(
        [("record", "broadcast_live", {"broadcast_id": "b1"})],
        _state(),
        FakeYouTubeClient(),
        lambda events: recorded.extend(events),
        CFG,
        BASE,
    )
    assert [e["event_type"] for e in recorded] == ["broadcast_live"]
    assert recorded[0]["payload"]["broadcast_id"] == "b1"


def test_apply_does_not_synthesize_created_on_create_and_bind():
    """`tick` records broadcast_created on the following pass (lifecycle
    None/complete -> ready). If the runner recorded one too, every create would
    write two rows."""
    recorded = []
    service.apply(
        [("create_and_bind",)],
        _state(),
        FakeYouTubeClient(),
        lambda events: recorded.extend(events),
        CFG,
        BASE,
    )
    assert [e["event_type"] for e in recorded] == []


def test_create_failure_is_swallowed_and_the_loop_survives():
    """A YouTube outage or an exhausted quota must never take the stream down —
    it logs, backs off, and records nothing false."""

    class Broken(FakeYouTubeClient):
        def insert_broadcast(self, title, privacy="public"):
            raise RuntimeError("quotaExceeded")

    state = _state()
    service.apply(
        [("create_and_bind",)], state, Broken(), lambda e: None, CFG, BASE
    )
    assert state.current_broadcast_id is None  # nothing false recorded


def test_run_ticks_then_stops_and_never_raises_on_api_failure():
    """One healthy tick, one exploding tick: `run` must complete both without
    propagating, and the OBS probe result must reach the decision."""
    ticks = {"n": 0}
    healthy = {"broadcast": None, "stream": {"id": "s1", "status": "active"}}

    def fetch(state):
        ticks["n"] += 1
        if ticks["n"] == 2:
            raise RuntimeError("api down")
        return healthy

    applied = []
    service.run(
        fetch_yt_state=fetch,
        obs_probe=lambda: True,
        apply_actions=lambda actions, state, now: applied.extend(actions),
        config=CFG,
        sleep_seconds=0,
        max_ticks=2,
    )
    assert ticks["n"] == 2  # the failing tick did not abort the loop
    assert ("create_and_bind",) in applied


def test_run_stops_creating_while_obs_is_not_streaming():
    """No point taking a broadcast public with no encoder pushing — and it
    would burn quota during every watchdog restart."""
    applied = []
    service.run(
        fetch_yt_state=lambda state: {
            "broadcast": None,
            "stream": {"id": "s1", "status": "inactive"},
        },
        obs_probe=lambda: False,
        apply_actions=lambda actions, state, now: applied.extend(actions),
        config=CFG,
        sleep_seconds=0,
        max_ticks=1,
    )
    assert applied == []


def test_run_survives_a_failing_obs_probe():
    """stream_ctl raises when OBS is down; that's the normal state during a
    watchdog restart, not a reason for the manager to die."""

    def broken_probe():
        raise RuntimeError("obs unreachable")

    service.run(
        fetch_yt_state=lambda state: {"broadcast": None, "stream": None},
        obs_probe=broken_probe,
        apply_actions=lambda actions, state, now: None,
        config=CFG,
        sleep_seconds=0,
        max_ticks=1,
    )  # must not raise


def test_state_persists_across_ticks_so_backoff_holds():
    """The runner owns BroadcastState between ticks — if it rebuilt it each
    time, last_create_at would reset and the backoff would never engage."""
    applied = []
    service.run(
        fetch_yt_state=lambda state: {
            "broadcast": None,
            "stream": {"id": "s1", "status": "active"},
        },
        obs_probe=lambda: True,
        apply_actions=lambda actions, state, now: applied.extend(actions),
        config=BroadcastConfig(create_backoff_seconds=3600),
        sleep_seconds=0,
        max_ticks=3,
    )
    assert applied.count(("create_and_bind",)) == 1


def test_main_exits_2_without_oauth(monkeypatch):
    """Fail-closed, mirroring the director's OBS-unreachable exit: systemd
    restarts it, and it comes up as soon as the secret is present."""
    for key in (
        "YOUTUBE_OAUTH_CLIENT_ID",
        "YOUTUBE_OAUTH_CLIENT_SECRET",
        "YOUTUBE_OAUTH_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BROADCAST_ENABLED", "1")
    assert service.main() == 2


def test_main_exits_0_when_disabled(monkeypatch):
    monkeypatch.setenv("BROADCAST_ENABLED", "0")
    assert service.main() == 0


@pytest.mark.parametrize("elapsed,expected", [(29, False), (31, True)])
def test_grace_window_gates_the_explicit_transition(elapsed, expected):
    """End-to-end through the runner: the stuck-'ready' fallback fires only
    after the grace window (the 2026-07-27 hang), not immediately."""
    applied = []
    now = {"t": BASE}
    yt = {
        "broadcast": {"id": "b1", "lifecycle": "ready"},
        "stream": {"id": "s1", "status": "active"},
    }
    state = BroadcastState("b1", "ready", None, BASE - timedelta(seconds=elapsed))
    service.tick_once(
        yt, True, state, CFG, now["t"], lambda actions, st, n: applied.extend(actions)
    )
    assert any(a[0] == "transition_live" for a in applied) is expected
