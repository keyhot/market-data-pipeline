"""Watchdog state machine — pure tick() function, no OBS, no clock, no DB."""

import logging
import signal

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
        WatchdogConfig(), list_procs=lambda: [], kill=_recorder(killed)
    ) == 0
    assert killed == []
    # gone after the first poll
    procs = iter([[(4242, "Sl"), (4243, "Sl")], []])
    assert clear_wedged_obs(
        WatchdogConfig(),
        list_procs=lambda: next(procs, []),
        kill=_recorder(killed),
        sleep=lambda _s: None,
    ) == 2
    assert [pid for pid, _sig in killed] == [4242, 4243]


# --- KI-017: the relaunched OBS inherits an environment with no display ---

from scripts.stream_watchdog import (  # noqa: E402
    DISPLAY_VARS,
    _launch_obs,
    desktop_env,
    live_obs_pids,
    parse_manager_environment,
)


def test_desktop_env_supplies_the_session_display():
    """KI-017, the whole bug. `Popen` hands OBS the watchdog's own environment.
    The watchdog is a user service started before the graphical session
    exported WAYLAND_DISPLAY/DISPLAY/XAUTHORITY, so every OBS it launched had
    no display to open a window on: Qt failed to init a platform plugin and the
    process died before it could even write an OBS log. 266 silent attempts."""
    watchdog_env = {"HOME": "/home/keyhot", "XDG_RUNTIME_DIR": "/run/user/1000"}
    session = {
        "WAYLAND_DISPLAY": "wayland-0",
        "DISPLAY": ":0",
        "XAUTHORITY": "/run/user/1000/.mutter-Xwaylandauth.XZXKT3",
    }
    env = desktop_env(watchdog_env, session)
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"].endswith(".mutter-Xwaylandauth.XZXKT3")
    assert env["HOME"] == "/home/keyhot", "must not drop the inherited env"


def test_desktop_env_keeps_inherited_values_when_the_session_is_silent():
    base = {"DISPLAY": ":0", "HOME": "/home/keyhot"}
    env = desktop_env(base, {})
    assert env["DISPLAY"] == ":0"


def test_manager_environment_unquotes_shell_quoted_values():
    """`systemctl --user show-environment` shell-quotes values that need it —
    this machine really does report `QT_IM_MODULES=$'wayland;ibus'`. A naive
    split hands OBS a literally-quoted value."""
    parsed = parse_manager_environment(
        "WAYLAND_DISPLAY=wayland-0\n"
        "QT_IM_MODULES=$'wayland;ibus'\n"
        "XAUTHORITY='/run/user/1000/.mutter Xwaylandauth'\n"
        "DISPLAY=:0\n"
    )
    assert parsed["WAYLAND_DISPLAY"] == "wayland-0"
    assert parsed["QT_IM_MODULES"] == "wayland;ibus"
    assert parsed["XAUTHORITY"] == "/run/user/1000/.mutter Xwaylandauth"
    assert parsed["DISPLAY"] == ":0"


def test_launch_passes_a_display_env_to_the_process():
    """The seam that was never exercised: the whole suite passed while the real
    launcher handed OBS an environment it could not start in."""
    calls = {}

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["env"] = kwargs.get("env")
        return object()

    _launch_obs(
        ["obs", "--startstreaming"],
        popen=fake_popen,
        read_manager_env=lambda: {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
    )
    assert calls["command"] == ["obs", "--startstreaming"]
    assert calls["env"] is not None, "must not inherit the watchdog's env blindly"
    assert calls["env"]["WAYLAND_DISPLAY"] == "wayland-0"


def test_display_vars_cover_what_qt_needs():
    for name in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"):
        assert name in DISPLAY_VARS


class _DeadProcess:
    """A launched OBS that exited immediately — KI-017's actual signature."""

    def poll(self):
        return 1

    def stderr_tail(self, limit=800):
        return 'qt.qpa.xcb: could not connect to display'


def test_relaunch_fails_fast_and_reports_why_when_obs_exits_immediately(caplog):
    """Waiting the full verify window for a process that is already dead turned
    a diagnosable crash into a blind 'did not come up' — for 12 hours."""
    slept = []
    with caplog.at_level(logging.ERROR):
        ok = relaunch_obs(
            WatchdogConfig(relaunch_verify_seconds=25, relaunch_poll_seconds=1),
            launch=lambda cmd: _DeadProcess(),
            probe=lambda: {"reachable": False},
            sleep=slept.append,
            clear=lambda config, **kw: 0,
        )
    assert ok is False
    assert len(slept) < 5, "must not wait out the window on a dead process"
    assert "could not connect to display" in caplog.text, (
        "OBS's own error is the diagnosis — it must reach the journal"
    )


def test_live_obs_pids_skips_zombies():
    """A zombie still matches `pgrep -x obs`, so the watchdog kept logging
    'Clearing unresponsive OBS' at a corpse that no signal can clear."""
    assert live_obs_pids([(844453, "Z"), (850210, "Sl")]) == [850210]
    assert live_obs_pids([(844453, "Z+")]) == []


def test_clear_wedged_obs_does_not_signal_a_zombie():
    killed = []
    procs = iter([[(844453, "Z"), (850210, "Sl")], [(844453, "Z")]])
    cleared = clear_wedged_obs(
        WatchdogConfig(),
        list_procs=lambda: next(procs, []),
        kill=_recorder(killed),
        sleep=lambda _s: None,
    )
    assert [pid for pid, _sig in killed] == [850210]
    assert cleared == 1


# --- KI-017: launching into a still-shutting-down OBS stalls on a modal ---


def _recorder(sink):
    return lambda pid, sig: sink.append((pid, sig))


def test_clear_waits_for_the_old_obs_to_actually_exit():
    """OBS's single-instance check is a MODAL DIALOG ('OBS is already
    running… Launch Anyway / Cancel'), not an instant exit. SIGTERM then
    launching immediately races the old instance's shutdown, and the new one
    stops dead waiting for a human — on an unattended 24/7 stream, forever."""
    killed, slept = [], []
    # alive, alive, then finally gone
    procs = iter([[(4242, "Sl")], [(4242, "Sl")], [(4242, "Sl")], []])
    cleared = clear_wedged_obs(
        WatchdogConfig(obs_terminate_grace_seconds=10, obs_terminate_poll_seconds=1),
        list_procs=lambda: next(procs, []),
        kill=_recorder(killed),
        sleep=slept.append,
    )
    assert cleared == 1
    assert slept, "must poll until the process is really gone"
    assert [sig for _pid, sig in killed] == [signal.SIGTERM]


def test_launch_clears_the_crash_sentinel_first(tmp_path):
    """OBS writes `.sentinel` at startup and removes it on a CLEAN exit. Left
    behind, the next launch shows a modal 'OBS Studio Crash Detected — Run in
    Safe Mode?' and waits for a human. Safe Mode disables WebSockets, so even
    the wrong answer removes the control plane entirely.

    Every unclean exit leaves it: the SIGKILL escalation above, a host reboot,
    and the soak's deliberate kill-OBS recovery drill. Without this the
    watchdog can never bring OBS back from the exact failure it exists for."""
    sentinel = tmp_path / ".sentinel"
    sentinel.mkdir()
    (sentinel / "run_ec84d699-2c48-4c0a-bfdd-9b35f467d3fb").write_text("")
    (sentinel / "run_867edf19-0d00-4085-924c-b9bcf70642b9").write_text("")
    order = []

    def fake_popen(command, **kwargs):
        order.append("launch")
        return object()

    _launch_obs(
        ["obs"],
        popen=fake_popen,
        read_manager_env=lambda: {"WAYLAND_DISPLAY": "wayland-0"},
        sentinel_path=str(sentinel),
    )
    assert list(sentinel.glob("run_*")) == [], (
        "stale run markers are what trigger the crash prompt"
    )
    assert sentinel.is_dir(), "OBS owns the directory — only its markers go"
    assert order == ["launch"]


def test_launch_leaves_unknown_files_in_the_sentinel_dir_alone(tmp_path):
    """Only OBS's own run markers are ours to remove."""
    sentinel = tmp_path / ".sentinel"
    sentinel.mkdir()
    (sentinel / "run_abc").write_text("")
    (sentinel / "something-else").write_text("keep me")
    _launch_obs(
        ["obs"],
        popen=lambda command, **kw: object(),
        read_manager_env=lambda: {"WAYLAND_DISPLAY": "wayland-0"},
        sentinel_path=str(sentinel),
    )
    assert (sentinel / "something-else").exists()


def test_launch_clears_a_plain_file_sentinel(tmp_path):
    """Older OBS layouts use a single file rather than a directory."""
    sentinel = tmp_path / ".sentinel"
    sentinel.write_text("")
    _launch_obs(
        ["obs"],
        popen=lambda command, **kw: object(),
        read_manager_env=lambda: {"WAYLAND_DISPLAY": "wayland-0"},
        sentinel_path=str(sentinel),
    )
    assert not sentinel.exists()


def test_launch_survives_a_missing_sentinel(tmp_path):
    """A clean previous exit leaves no sentinel — that is the normal case."""
    _launch_obs(
        ["obs"],
        popen=lambda command, **kw: object(),
        read_manager_env=lambda: {"WAYLAND_DISPLAY": "wayland-0"},
        sentinel_path=str(tmp_path / "absent"),
    )


def test_obs_command_suppresses_startup_dialogs():
    """Any modal on the startup path is a permanent stall for an unattended
    stream, not a slow start."""
    assert "--disable-missing-files-check" in WatchdogConfig().obs_command
    assert "--safe-mode" not in WatchdogConfig().obs_command, (
        "safe mode disables the websocket the whole control plane needs"
    )


def test_clear_escalates_to_sigkill_when_obs_ignores_sigterm():
    """A wedged OBS is exactly the case this function exists for, and a wedged
    process is the one most likely to ignore SIGTERM. Without escalation the
    launch proceeds into the modal dialog anyway."""
    killed = []
    cleared = clear_wedged_obs(
        WatchdogConfig(obs_terminate_grace_seconds=3, obs_terminate_poll_seconds=1),
        list_procs=lambda: [(4242, "Sl")],  # never dies
        kill=_recorder(killed),
        sleep=lambda _s: None,
    )
    assert cleared == 1
    assert (4242, signal.SIGKILL) in killed, "SIGKILL cannot be ignored"
