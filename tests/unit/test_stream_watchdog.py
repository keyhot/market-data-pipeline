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


# --- KI-015: a relaunch that produces no OBS must be noticed ---

from scripts.stream_watchdog import (  # noqa: E402
    clear_wedged_obs,
    note_relaunch_result,
    relaunch_obs,
)


def test_successful_relaunch_clears_the_failure_streak():
    state = WatchdogState(failed_relaunches=2)
    assert note_relaunch_result(state, True, WatchdogConfig()) == []
    assert state.failed_relaunches == 0


def test_repeated_failed_relaunches_escalate_exactly_once():
    """KI-015: the watchdog logged 'OBS unreachable — relaunching' every 5
    minutes for hours while no OBS ever appeared, and nothing in the world log
    said so — the soak report would show a permanent outage with no cause.
    Escalate once per streak: silence is the bug, spam is not the fix."""
    config = WatchdogConfig(escalate_after_failed_relaunches=3)
    state = WatchdogState()
    assert note_relaunch_result(state, False, config) == []   # 1
    assert note_relaunch_result(state, False, config) == []   # 2
    escalation = note_relaunch_result(state, False, config)   # 3 -> escalate
    assert escalation == [
        ("record", "stream_dropped",
         {"reason": "obs_relaunch_failing", "attempts": 3}),
    ]
    assert note_relaunch_result(state, False, config) == []   # 4, no repeat


def test_relaunch_reports_failure_when_obs_never_answers():
    """The whole bug: Popen returning is not evidence OBS started."""
    launched = []
    ok = relaunch_obs(
        WatchdogConfig(relaunch_verify_seconds=3, relaunch_poll_seconds=1),
        launch=lambda cmd: launched.append(cmd),
        probe=lambda: {"reachable": False},
        sleep=lambda _s: None,
        clear=lambda config, **kw: 0,
    )
    assert ok is False
    assert launched, "it should still have attempted the launch"


def test_relaunch_reports_success_once_obs_answers():
    answers = iter([{"reachable": False}, {"reachable": True}])
    ok = relaunch_obs(
        WatchdogConfig(relaunch_verify_seconds=10, relaunch_poll_seconds=1),
        launch=lambda cmd: None,
        probe=lambda: next(answers),
        sleep=lambda _s: None,
        clear=lambda config, **kw: 0,
    )
    assert ok is True


def test_relaunch_clears_a_wedged_obs_before_launching():
    """A previous OBS that is running but not answering makes every relaunch a
    no-op — OBS refuses to start a second instance. Clearing has to happen
    before the launch, not after."""
    order = []
    relaunch_obs(
        WatchdogConfig(relaunch_verify_seconds=1, relaunch_poll_seconds=1),
        launch=lambda cmd: order.append("launch"),
        probe=lambda: {"reachable": True},
        sleep=lambda _s: None,
        clear=lambda config, **kw: order.append("clear") or 1,
    )
    assert order[:2] == ["clear", "launch"]


def test_clear_wedged_obs_terminates_only_what_is_there():
    killed = []
    assert clear_wedged_obs(
        WatchdogConfig(), list_pids=lambda: [], kill=killed.append
    ) == 0
    assert killed == []
    assert clear_wedged_obs(
        WatchdogConfig(), list_pids=lambda: [4242, 4243], kill=killed.append
    ) == 2
    assert killed == [4242, 4243]
