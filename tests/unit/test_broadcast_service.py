"""The broadcast manager runner (Sprint 14, A4): a thin loop over the pure
`tick`, shaped like stream_watchdog / director. Everything is injected — no
network, no OAuth, no clock, no OBS — and the cardinal rule is that nothing
the YouTube API does can take the stream down.
"""

import inspect
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    def list_broadcasts(self, broadcast_id=None, max_pages=5):
        # Mirrors the real client's contract, including the id lookup — a fake
        # that's laxer than the thing it stands in for is how this project's
        # seam bugs stayed green.
        self.calls.append(("list_broadcasts", broadcast_id))
        if broadcast_id is not None:
            return [b for b in self.broadcasts if b["id"] == broadcast_id]
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
    client = FakeYouTubeClient(
        broadcasts=[{"id": "b1", "lifecycle": "live", "bound_stream_id": "s1"}]
    )
    state = service.fetch_yt_state(client, _state(), CFG)
    assert state["broadcast"]["id"] == "b1"
    assert state["stream"]["status"] == "active"


def test_missing_stream_title_is_logged_loudly(caplog):
    """A wrong BROADCAST_STREAM_TITLE is the likeliest setup mistake, and its
    symptom is total silence: find_stream returns None, _stream_active is
    False, tick never acts, and the 'no liveStream titled' error inside
    _create_and_bind is never reached. Name the title we looked for."""
    import logging

    class NoStream(FakeYouTubeClient):
        def find_stream(self, title):
            return None

    with caplog.at_level(logging.WARNING, logger="broadcast.service"):
        state = service.fetch_yt_state(NoStream(), _state(), CFG)
    assert state["stream"] is None
    messages = [r.getMessage() for r in caplog.records]
    assert any(CFG.stream_title in m for m in messages), (
        f"no diagnostic naming the title; got {messages}"
    )


def test_fetch_state_only_adopts_a_broadcast_bound_to_our_stream():
    client = FakeYouTubeClient(
        broadcasts=[{"id": "x", "lifecycle": "ready", "bound_stream_id": "other"}]
    )
    assert service.fetch_yt_state(client, _state(), CFG)["broadcast"] is None


def test_fetch_state_scans_for_a_live_duplicate_while_ours_waits():
    """KI-086: the exact id lookup can never see a second broadcast, so while
    ours sits in ready/testing the fetch must also scan — otherwise the
    adoption rule in select_broadcast is unreachable in production."""
    ours = {"id": "b1", "lifecycle": "ready", "bound_stream_id": "s1"}
    dup = {"id": "dup", "lifecycle": "live", "bound_stream_id": "s1"}
    client = FakeYouTubeClient(broadcasts=[ours, dup])
    state = BroadcastState("b1", "ready", None, None)
    assert service.fetch_yt_state(client, state, CFG)["broadcast"] == dup


def test_fetch_state_does_not_scan_while_ours_is_live():
    """Steady state stays one exact lookup — the quota guard."""
    ours = {"id": "b1", "lifecycle": "live", "bound_stream_id": "s1"}
    client = FakeYouTubeClient(broadcasts=[ours])
    state = BroadcastState("b1", "live", None, None)
    service.fetch_yt_state(client, state, CFG)
    assert ("list_broadcasts", None) not in client.calls


class Refusing(FakeYouTubeClient):
    def transition(self, broadcast_id, status):
        raise RuntimeError("invalidTransition")


def test_repeated_refused_transitions_log_one_traceback_then_one_error(caplog):
    """KI-086: 16k identical tracebacks buried the one fact that mattered. The
    first refusal keeps its traceback, repeats are counted quietly, and the
    streak escalates exactly once, at ERROR, naming the count."""
    state = _state()
    client = Refusing(broadcasts=[{"id": "b1", "lifecycle": "ready"}])
    with caplog.at_level("INFO", logger="broadcast.service"):
        for _ in range(service.REFUSED_TRANSITION_ALERT_AT + 5):
            service.apply(
                [("transition_live", "b1")], state, client, lambda e: None, CFG, BASE
            )
    ours = [r for r in caplog.records if r.name == "broadcast.service"]
    with_traceback = [r for r in ours if r.exc_info]
    errors = [r for r in ours if r.levelname == "ERROR"]
    assert len(with_traceback) == 1
    assert len(errors) == 1
    assert str(service.REFUSED_TRANSITION_ALERT_AT) in errors[0].getMessage()


def test_a_successful_transition_resets_the_refusal_streak(caplog):
    state = _state()

    def go_live(client):
        service.apply(
            [("transition_live", "b1")], state, client, lambda e: None, CFG, BASE
        )

    with caplog.at_level("INFO", logger="broadcast.service"):
        for _ in range(3):
            go_live(Refusing())
        go_live(FakeYouTubeClient())
        assert state.refused_transitions == 0
        go_live(Refusing())
    # A new streak is a new fact: it gets its traceback again.
    tracebacks = [
        r for r in caplog.records if r.name == "broadcast.service" and r.exc_info
    ]
    assert len(tracebacks) == 2


def test_a_go_live_by_auto_start_ends_the_refusal_streak(caplog):
    """Our own transition is almost always the refused one — auto-start takes
    the broadcast live. So the streak must end when a tick stops asking for a
    transition, or the count sticks after every healthy go-live and the next
    streak neither logs its traceback nor escalates on time."""
    state = _state()
    refusing = Refusing(broadcasts=[{"id": "b1", "lifecycle": "ready"}])

    def apply_with(client):
        return lambda actions, st, now: service.apply(
            actions, st, client, lambda e: None, CFG, now
        )

    waiting = {"broadcast": {"id": "b1", "lifecycle": "ready"}, "stream": {
        "id": "s1", "status": "active"}}
    live = {"broadcast": {"id": "b1", "lifecycle": "live"}, "stream": {
        "id": "s1", "status": "active"}}
    with caplog.at_level("INFO", logger="broadcast.service"):
        now = BASE
        service.tick_once(waiting, True, state, CFG, now, apply_with(refusing))
        for _ in range(5):
            now += timedelta(seconds=31)
            service.tick_once(waiting, True, state, CFG, now, apply_with(refusing))
        assert state.refused_transitions == 5
        service.tick_once(live, True, state, CFG, now, apply_with(refusing))
        assert state.refused_transitions == 0
        for _ in range(service.REFUSED_TRANSITION_ALERT_AT):
            service.apply(
                [("transition_live", "b2")], state, refusing, lambda e: None, CFG, now
            )
    ours = [r for r in caplog.records if r.name == "broadcast.service"]
    assert len([r for r in ours if r.exc_info]) == 2
    # Counted from the new streak's start: exactly one ERROR, at the threshold.
    assert len([r for r in ours if r.levelname == "ERROR"]) == 1


def test_the_alert_threshold_clears_the_observed_benign_settle():
    """51 refusals preceded healthy go-lives on 2026-08-18 and 2026-09-15."""
    assert service.REFUSED_TRANSITION_ALERT_AT > 51


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


def test_quiet_tick_still_reaches_the_recorder_so_the_spool_drains():
    """A healthy live broadcast produces ZERO actions on essentially every
    tick — that's the steady state the manager exists to maintain. If the
    recorder is only called when there are actions, a broadcast_live spooled
    during a Postgres blip sits there until the next action-producing tick,
    which may be the broadcast's *end* days later. The report would then say
    'not measured' while the stream was public — the lying number again, one
    layer up."""
    calls = []
    live = {
        "broadcast": {"id": "b1", "lifecycle": "live", "bound_stream_id": "s1"},
        "stream": {"id": "s1", "status": "active"},
    }
    state = BroadcastState("b1", "live", None, BASE)
    service.tick_once(
        live,
        True,
        state,
        CFG,
        BASE,
        lambda actions, st, now: calls.append(actions),
    )
    assert calls == [[]], "the applier must run on a quiet tick (flush-on-tick)"


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


# --- KI-086 alert half: the status the ops alerter reads ---------------------


def _read_status():
    import json
    import os

    with open(os.environ["BROADCAST_STATUS_PATH"]) as f:
        return json.load(f)


def test_run_publishes_its_status_every_tick_for_the_alerter():
    """An ERROR line is the channel that read green for six days; the alerter
    (scripts/ops_alert.py) reads this file instead."""

    def refuse(actions, state, now):
        # `_transition_live` is what counts refusals in production; stand in
        # for a streak it has already built.
        state.refused_transitions = 61

    service.run(
        fetch_yt_state=lambda state: {"broadcast": None, "stream": None},
        obs_probe=lambda: True,
        apply_actions=refuse,
        config=CFG,
        sleep_seconds=0,
        max_ticks=3,
    )
    status = _read_status()
    assert status["refused_transitions"] == 61
    assert status["consecutive_tick_failures"] == 0
    assert datetime.fromisoformat(status["updated_at"]).tzinfo is not None


def test_failing_ticks_are_counted_and_a_good_tick_resets_them():
    ticks = {"n": 0}

    def fetch(state):
        ticks["n"] += 1
        if ticks["n"] <= 2:
            raise RuntimeError("youtube down")
        return {"broadcast": None, "stream": None}

    seen = []

    def spy(**kwargs):
        seen.append(kwargs["consecutive_tick_failures"])

    service.run(
        fetch_yt_state=fetch,
        obs_probe=lambda: True,
        apply_actions=lambda actions, state, now: None,
        config=CFG,
        sleep_seconds=0,
        max_ticks=3,
        on_status=spy,
    )
    assert seen == [1, 2, 0]


def test_an_unwritable_status_never_stops_the_manager(monkeypatch, tmp_path, caplog):
    """Cardinal rule: nothing about alerting can take the stream down.

    `write_status` swallows every exception, so "did not raise" holds whatever
    the code does: a renamed env var (the write goes to the default path) or a
    `run` that returns before its first `on_status` both read green. What can
    fail is what the code *did*, so this watches it: both ticks reported, each
    write failed with an OSError at the given path, the failure was logged,
    and nothing was written to the default path. With the path under a plain
    file, the step that raises is the parent-directory `mkdir`; the file
    write itself is never reached."""
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    status_path = blocker / "status.json"
    monkeypatch.setenv("BROADCAST_STATUS_PATH", str(status_path))
    # The default path is relative: from tmp_path a write that ignores the env
    # var lands in tmp_path/data, never in the repo's data/.
    monkeypatch.chdir(tmp_path)

    # The file operations write_status makes, recorded as (operation, target,
    # error). Each wraps the real call, so the OSError is the real one.
    fs_calls = []

    def watch(name):
        real = getattr(Path, name)

        def spy(self, *args, **kwargs):
            try:
                result = real(self, *args, **kwargs)
            except OSError as exc:
                fs_calls.append((name, self, exc))
                raise
            fs_calls.append((name, self, None))
            return result

        monkeypatch.setattr(Path, name, spy)

    for name in ("mkdir", "write_text", "replace"):
        watch(name)

    # `run` binds on_status=write_status when it is defined, so the default
    # cannot be spied on from outside; pass a spy that calls through instead,
    # and check separately that the default is the real writer.
    assert (
        inspect.signature(service.run).parameters["on_status"].default
        is service.write_status
    )
    reported = []

    def on_status(**status):
        reported.append(status)
        service.write_status(**status)

    with caplog.at_level(logging.WARNING, logger="broadcast.service"):
        service.run(
            fetch_yt_state=lambda state: {"broadcast": None, "stream": None},
            obs_probe=lambda: True,
            apply_actions=lambda actions, state, now: None,
            config=CFG,
            sleep_seconds=0,
            max_ticks=2,
            on_status=on_status,
        )  # must not raise

    # Both ticks reported, and neither tick body failed (the status write is
    # outside run's try block, so this says nothing about the write itself).
    assert [s["consecutive_tick_failures"] for s in reported] == [0, 0]
    # Each tick's write reached the given path (its parent is the plain file)
    # and failed there with a real OSError; nothing under it was written.
    under_given = [c for c in fs_calls if blocker in (c[1], *c[1].parents)]
    assert [(op, target) for op, target, _exc in under_given] == [
        ("mkdir", blocker)
    ] * 2, f"one failed mkdir of {blocker} per tick: {under_given}"
    assert all(isinstance(exc, OSError) for _op, _target, exc in under_given)
    # Swallowed, but not silently.
    ours = [r for r in caplog.records if r.name == "broadcast.service"]
    assert [r.getMessage() for r in ours] == ["broadcast status not written"] * 2
    assert all(str(blocker) in r.error for r in ours)
    # Nothing was redirected to the default path.
    assert not (tmp_path / "data").exists()
