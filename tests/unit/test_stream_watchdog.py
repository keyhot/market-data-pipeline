"""Watchdog state machine — pure tick() function, no OBS, no clock, no DB."""

from scripts.stream_watchdog import WatchdogConfig, WatchdogState, tick

CFG = WatchdogConfig()


def _actions_of(actions, kind):
    return [a for a in actions if a[0] == kind]


def test_obs_down_records_drop_and_relaunches():
    state = WatchdogState(obs_up=True, streaming=True)
    state, actions = tick({"reachable": False}, state, CFG, now=1000.0)
    assert ("relaunch_obs",) in actions
    records = _actions_of(actions, "record")
    assert records and records[0][1] == "stream_dropped"
    assert state.obs_up is False and state.streaming is False


def test_backoff_suppresses_restart_storm():
    state = WatchdogState(
        obs_up=False, streaming=False, last_restart_at=1000.0, down_since=990.0
    )
    state, actions = tick({"reachable": False}, state, CFG, now=1030.0)
    assert _actions_of(actions, "relaunch_obs") == []  # within cooldown
    state, actions = tick(
        {"reachable": False}, state, CFG, now=1000.0 + CFG.restart_cooldown_seconds
    )
    assert ("relaunch_obs",) in actions


def test_obs_recovery_rebuilds_scene_then_streaming_records_started():
    state = WatchdogState(obs_up=False, streaming=False, down_since=900.0)
    probe = {"reachable": True, "streaming": True, "dropped_ratio": 0.0}
    state, actions = tick(probe, state, CFG, now=1000.0)
    assert ("rebuild_scene",) in actions
    records = _actions_of(actions, "record")
    assert records[0][1] == "stream_started"
    assert records[0][2]["recovery_seconds"] == 100.0
    assert state.obs_up and state.streaming


def test_stream_stopped_underneath_us_records_stopped_and_restarts():
    # KI-008: an unexpected live->inactive transition is recorded as a truthful
    # stream_stopped (world shows "down"/idle, not "dropped — recovering"),
    # exactly once, while the watchdog still tries to bring the stream back.
    state = WatchdogState(obs_up=True, streaming=True)
    probe = {"reachable": True, "streaming": False, "dropped_ratio": 0.0}
    state, actions = tick(probe, state, CFG, now=1000.0)
    records = _actions_of(actions, "record")
    assert [r[1] for r in records] == ["stream_stopped"]
    assert records[0][2]["reason"] == "output_inactive"
    assert ("start_stream",) in actions
    assert state.streaming is False
    # Once per transition: a second still-inactive tick records nothing new
    # (the world isn't told "stopped" over and over).
    _, again = tick(probe, state, CFG, now=1030.0)
    assert _actions_of(again, "record") == []


def test_dropped_frames_records_event_but_never_restarts():
    state = WatchdogState(obs_up=True, streaming=True)
    probe = {"reachable": True, "streaming": True, "dropped_ratio": 0.09}
    state, actions = tick(probe, state, CFG, now=1000.0)
    records = _actions_of(actions, "record")
    assert records and records[0][1] == "stream_dropped"
    assert records[0][2]["reason"] == "dropped_frames"
    assert _actions_of(actions, "relaunch_obs") == []
    assert _actions_of(actions, "start_stream") == []
    # flag latches: no repeat while the ratio stays high
    state, actions = tick(probe, state, CFG, now=1030.0)
    assert _actions_of(actions, "record") == []
    # ratio recovers → latch resets
    ok = {"reachable": True, "streaming": True, "dropped_ratio": 0.0}
    state, _ = tick(ok, state, CFG, now=1060.0)
    state, actions = tick(probe, state, CFG, now=1090.0)
    assert _actions_of(actions, "record")


def test_cold_start_not_streaming_starts_stream():
    state = WatchdogState()  # obs_up=True, streaming=False
    probe = {"reachable": True, "streaming": False, "dropped_ratio": 0.0}
    state, actions = tick(probe, state, CFG, now=1000.0)
    assert ("start_stream",) in actions
    assert _actions_of(actions, "record") == []  # nothing died; nothing to record
