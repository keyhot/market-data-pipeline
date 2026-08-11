"""Stream watchdog (Sprint 11): notices OBS or the stream dying and brings it
back without a human. Detector only — world/stream_events.py is the recorder.

Pure state machine (tick) + thin runner loop, so the logic is tested with no
OBS, no clock, and no DB. Compose restart policies already self-heal the
api/scheduler containers; this covers the host-side pieces compose can't.
"""

import codecs
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from scripts import stream_ctl  # noqa: E402
from world.stream_events import record_stream_event  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogConfig:
    poll_seconds: float = 30.0
    # High dropped-frame ratio is a bandwidth symptom: record it, never
    # restart (a restart makes congestion worse).
    dropped_ratio_threshold: float = 0.05
    # Max one restart attempt per cooldown window — no flapping.
    restart_cooldown_seconds: float = 300.0
    # KI-017: every modal on the startup path is a permanent stall for an
    # unattended stream. `--disable-missing-files-check` suppresses one of
    # them; the crash prompt is a file, not a flag (see clear_crash_sentinel).
    obs_command: tuple = (
        "obs",
        "--startstreaming",
        "--minimize-to-tray",
        "--disable-missing-files-check",
    )
    # KI-015: how long to wait for a relaunched OBS to answer its websocket
    # before calling the attempt failed. OBS takes a few seconds to boot and
    # load a scene collection full of browser sources.
    relaunch_verify_seconds: float = 25.0
    relaunch_poll_seconds: float = 2.0
    # Consecutive failed relaunches before the world log is told *why* the
    # stream isn't coming back. Once per streak — silence was the bug, spam
    # is not the fix.
    escalate_after_failed_relaunches: int = 3
    # KI-017: how long to wait for a terminated OBS to really be gone before
    # escalating to SIGKILL. Launching while one is still shutting down makes
    # OBS pop a modal "already running" dialog that waits for a human.
    obs_terminate_grace_seconds: float = 10.0
    obs_terminate_poll_seconds: float = 1.0


@dataclass
class WatchdogState:
    obs_up: bool = True
    streaming: bool = False
    last_restart_at: float | None = None
    down_since: float | None = None
    dropped_flagged: bool = False
    failed_relaunches: int = 0


def tick(
    probe: dict, state: WatchdogState, config: WatchdogConfig, now: float
) -> tuple[WatchdogState, list[tuple]]:
    """One evaluation: probe result in, (new state, actions) out. Pure."""
    actions: list[tuple] = []

    if not probe.get("reachable"):
        if state.obs_up:
            actions.append(("record", "stream_dropped", {"reason": "obs_unreachable"}))
            state.down_since = now
        state.obs_up = False
        state.streaming = False
        if _restart_allowed(state, config, now):
            actions.append(("relaunch_obs",))
            state.last_restart_at = now
        return state, actions

    if not state.obs_up:
        actions.append(("rebuild_scene",))
        state.obs_up = True

    if probe.get("streaming"):
        if not state.streaming:
            payload = {}
            if state.down_since is not None:
                payload["recovery_seconds"] = round(now - state.down_since, 1)
                state.down_since = None
            actions.append(("record", "stream_started", payload))
        state.streaming = True
        ratio = probe.get("dropped_ratio", 0.0)
        if ratio >= config.dropped_ratio_threshold:
            if not state.dropped_flagged:
                actions.append(
                    (
                        "record",
                        "stream_dropped",
                        {
                            "reason": "dropped_frames",
                            "dropped_ratio": round(ratio, 4),
                        },
                    )
                )
                state.dropped_flagged = True
        else:
            state.dropped_flagged = False
        return state, actions

    if state.streaming:
        # KI-008: an unexpected live->inactive transition (a stop on the
        # platform/OBS side the watchdog didn't issue) is recorded as a truthful
        # stream_stopped — so /world shows "down"/idle instead of "dropped —
        # recovering" — exactly once per transition. We still attempt a restart
        # below; if it comes back the next tick records stream_started.
        actions.append(("record", "stream_stopped", {"reason": "output_inactive"}))
        state.down_since = now
        state.streaming = False
    if _restart_allowed(state, config, now):
        actions.append(("start_stream",))
        state.last_restart_at = now
    return state, actions


def _restart_allowed(
    state: WatchdogState, config: WatchdogConfig, now: float
) -> bool:
    return (
        state.last_restart_at is None
        or now - state.last_restart_at >= config.restart_cooldown_seconds
    )


def note_relaunch_result(
    state: WatchdogState, ok: bool, config: WatchdogConfig
) -> list[tuple]:
    """Fold a relaunch outcome into the state (KI-015). Pure.

    The watchdog used to fire a `Popen` and assume it worked. On 2026-08-02 it
    logged "OBS unreachable — relaunching" every 5 minutes for hours while no
    OBS process ever appeared, and nothing said so anywhere a report could see:
    a soak would have recorded a permanent outage with no cause. A streak of
    failures now reaches the world log exactly once.
    """
    if ok:
        state.failed_relaunches = 0
        return []
    state.failed_relaunches += 1
    if state.failed_relaunches == config.escalate_after_failed_relaunches:
        return [
            (
                "record",
                "stream_dropped",
                {
                    "reason": "obs_relaunch_failing",
                    "attempts": state.failed_relaunches,
                },
            )
        ]
    return []


def _obs_processes() -> list[tuple[int, str]]:
    """(pid, state) for every process named `obs`. State matters: a zombie
    still matches a name lookup but no signal can clear it (KI-017)."""
    result = subprocess.run(
        ["ps", "-C", "obs", "-o", "pid=,stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    procs: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit():
            procs.append((int(fields[0]), fields[1]))
    return procs


def live_obs_pids(processes: list[tuple[int, str]]) -> list[int]:
    """Drop zombies. Pure.

    A defunct OBS is already dead — it holds no single-instance lock and
    cannot be terminated. Signalling it accomplishes nothing, and counting it
    made the watchdog report it was clearing a wedged OBS every 5 minutes when
    it was really looking at the corpse of the OBS it had just failed to start.
    """
    return [pid for pid, state in processes if not state.startswith("Z")]


def _terminate(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # already gone — that's the outcome we wanted
    except PermissionError:
        logger.warning("Cannot signal OBS process", extra={"pid": pid})


def clear_wedged_obs(
    config: WatchdogConfig, list_procs=None, kill=None, sleep=None
) -> int:
    """Terminate any OBS still running while its websocket is unreachable, and
    **wait until it is really gone**.

    This only runs on the relaunch path, which by definition means OBS is not
    answering. A half-dead instance holds the single-instance lock, so every
    relaunch silently no-ops against it — the watchdog kept "relaunching" into
    a wall. Clearing it first is what makes the retry mean anything.

    KI-017: signalling is not the same as gone. OBS's single-instance check is
    a **modal dialog** ("OBS is already running… Launch Anyway / Cancel"), so
    launching into a still-shutting-down instance doesn't fail — it stops and
    waits for a human, which on an unattended 24/7 stream means forever. A
    wedged OBS is also the process most likely to ignore SIGTERM, so escalate.
    """
    list_procs = list_procs or _obs_processes
    kill = kill or _terminate
    sleep = sleep or time.sleep

    pids = live_obs_pids(list_procs())
    if not pids:
        return 0

    for pid in pids:
        logger.warning("Clearing unresponsive OBS before relaunch",
                       extra={"pid": pid})
        kill(pid, signal.SIGTERM)

    waited = 0.0
    while waited < config.obs_terminate_grace_seconds:
        sleep(config.obs_terminate_poll_seconds)
        waited += config.obs_terminate_poll_seconds
        if not live_obs_pids(list_procs()):
            return len(pids)

    for pid in live_obs_pids(list_procs()):
        logger.error(
            "OBS ignored SIGTERM — killing it, or the relaunch stalls on the "
            "'already running' dialog",
            extra={"pid": pid, "waited_seconds": waited},
        )
        kill(pid, signal.SIGKILL)
    sleep(config.obs_terminate_poll_seconds)
    return len(pids)


def relaunch_obs(
    config: WatchdogConfig, launch=None, probe=None, sleep=None, clear=None
) -> bool:
    """Relaunch OBS and **verify** it actually came up. Returns success.

    `Popen` returning is not evidence OBS started — that assumption is KI-015.
    Seams are injected so the whole path is tested without OBS or subprocess.
    """
    launch = launch or _launch_obs
    probe = probe or probe_obs
    sleep = sleep or time.sleep
    clear = clear or clear_wedged_obs

    clear(config)
    logger.warning("OBS unreachable — relaunching")
    launched = launch(list(config.obs_command))

    waited = 0.0
    while waited < config.relaunch_verify_seconds:
        sleep(config.relaunch_poll_seconds)
        waited += config.relaunch_poll_seconds
        if probe().get("reachable"):
            logger.info("OBS relaunch verified", extra={"seconds": waited})
            return True
        # KI-017: a process that is already dead will never answer. Waiting out
        # the window turned a diagnosable crash into a blind "did not come up"
        # — for 12 hours. Its own stderr names the cause.
        exit_code = launched.poll() if launched is not None else None
        if exit_code is not None:
            logger.error(
                "OBS exited immediately after launch: %s",
                launched.stderr_tail() or "(no output captured)",
                extra={"exit_code": exit_code, "seconds": waited},
            )
            return False
    logger.error(
        "OBS did not come up after relaunch",
        extra={"waited_seconds": waited},
    )
    return False


# KI-017: what OBS needs to open a window. Without these Qt cannot initialize
# a platform plugin and OBS exits before it writes even its own log file.
DISPLAY_VARS = (
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "DBUS_SESSION_BUS_ADDRESS",
)


def _unquote(value: str) -> str:
    """Undo the shell quoting `systemctl show-environment` applies."""
    if value.startswith("$'") and value.endswith("'") and len(value) >= 3:
        return codecs.decode(value[2:-1], "unicode_escape")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_manager_environment(text: str) -> dict[str, str]:
    """Parse `systemctl --user show-environment` output. Pure.

    Values needing quotes come back quoted (this machine reports
    `QT_IM_MODULES=$'wayland;ibus'`), so a naive split would hand OBS a
    literally-quoted value.
    """
    env: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name and not name.startswith(" "):
            env[name.strip()] = _unquote(value)
    return env


def _manager_environment() -> dict[str, str]:
    """The graphical session's environment, read at launch time.

    Deliberately *not* taken from our own environment or pinned in the unit
    file: a logout/login mints a new WAYLAND_DISPLAY, and the watchdog is a
    long-lived service that would otherwise keep launching OBS at a display
    that no longer exists.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Cannot read the session environment",
                       extra={"error_type": type(exc).__name__})
        return {}
    return parse_manager_environment(result.stdout)


def desktop_env(base, session: dict[str, str]) -> dict[str, str]:
    """The environment OBS is launched with (KI-017). Pure.

    `Popen` hands the child the *watchdog's* environment. The watchdog is a
    systemd user service that started before the graphical session exported
    WAYLAND_DISPLAY/DISPLAY/XAUTHORITY, so every OBS it launched had no display
    to open a window on. It died instantly, 266 times over 12 hours, and
    because the launcher sent stderr to DEVNULL the reason was never recorded.
    """
    env = dict(base)
    for name in DISPLAY_VARS:
        value = session.get(name)
        if value:
            env[name] = value
    return env


class LaunchedObs:
    """A launched OBS we can actually ask about — exit status and its own
    stderr. `Popen` returning is not evidence OBS started (KI-015); a handle
    that answers `poll()` is how we find out *why* it didn't (KI-017)."""

    def __init__(self, process, stderr_path: str | None = None):
        self.process = process
        self.stderr_path = stderr_path

    def poll(self):
        return self.process.poll()

    def stderr_tail(self, limit: int = 800) -> str:
        if not self.stderr_path:
            return ""
        try:
            with open(self.stderr_path, errors="replace") as handle:
                return handle.read()[-limit:].strip()
        except OSError:
            return ""


OBS_SENTINEL = "~/.config/obs-studio/.sentinel"


def clear_crash_sentinel(path: str) -> bool:
    """Remove OBS's improper-shutdown marker before launching (KI-017).

    OBS writes `.sentinel` at startup and removes it on a *clean* exit. Left
    behind, the next launch shows a modal "OBS Studio Crash Detected — Run in
    Safe Mode?" and waits for a human; Safe Mode disables WebSockets, so even
    the wrong answer would remove the control plane.

    Every unclean exit leaves it: the SIGKILL escalation on the relaunch path,
    a host reboot, and the soak's deliberate kill-OBS recovery drill. Without
    clearing it the watchdog cannot recover from the exact class of failure it
    exists to handle. The crash signal isn't lost — it moves to this log.

    On OBS 32 `.sentinel` is a *directory* holding one empty `run_<uuid>` per
    instance, removed by that instance on a clean exit; older layouts use a
    single file. Only OBS's own markers are removed, never the directory.
    """
    target = os.path.expanduser(path)
    cleared = 0
    try:
        if os.path.isdir(target):
            for name in os.listdir(target):
                if name.startswith("run_"):
                    os.remove(os.path.join(target, name))
                    cleared += 1
        else:
            os.remove(target)
            cleared = 1
    except FileNotFoundError:
        return False  # clean shutdown last time — the normal case
    except OSError as exc:
        logger.warning("Could not clear the OBS crash sentinel",
                       extra={"error_type": type(exc).__name__,
                              "path": target})
        return False
    if not cleared:
        return False
    logger.warning(
        "Cleared OBS's crash sentinel — the last shutdown was unclean, and it "
        "would otherwise stall the relaunch on the Safe Mode prompt",
        extra={"markers": cleared},
    )
    return True


def _launch_obs(
    command: list[str], popen=None, read_manager_env=None, sentinel_path=None
):
    """Launch OBS with a usable display, keeping its stderr (KI-017).

    stderr goes to a file rather than a PIPE nobody drains — a full pipe
    buffer would block OBS itself.
    """
    popen = popen or subprocess.Popen
    read_manager_env = read_manager_env or _manager_environment

    clear_crash_sentinel(sentinel_path or OBS_SENTINEL)

    env = desktop_env(os.environ, read_manager_env())
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        logger.error(
            "No display in the session environment — OBS cannot start. "
            "Is the graphical session up?"
        )

    try:
        handle = tempfile.NamedTemporaryFile(
            prefix="obs-launch-", suffix=".log", delete=False
        )
        stderr_path = handle.name
    except OSError:
        handle, stderr_path = subprocess.DEVNULL, None

    process = popen(
        command, stdout=subprocess.DEVNULL, stderr=handle, env=env
    )
    return LaunchedObs(process, stderr_path)


def probe_obs() -> dict:
    try:
        client = stream_ctl.make_client()
        status = stream_ctl.get_status(client)
    except Exception:
        return {"reachable": False}
    return {
        "reachable": True,
        "streaming": status["streaming"],
        "dropped_ratio": status["dropped_ratio"],
    }


def execute_actions(
    actions: list[tuple], config: WatchdogConfig, state: WatchdogState | None = None
) -> list[tuple]:
    """Perform decided actions. Returns any follow-up actions produced by their
    *outcomes* — today only the KI-015 relaunch escalation, which the caller
    executes in turn."""
    followups: list[tuple] = []
    for action in actions:
        kind = action[0]
        try:
            if kind == "record":
                record_stream_event(action[1], action[2])
            elif kind == "relaunch_obs":
                ok = relaunch_obs(config)
                if state is not None:
                    followups.extend(note_relaunch_result(state, ok, config))
            elif kind == "rebuild_scene":
                stream_ctl.build_scene(stream_ctl.make_client())
                logger.info("Scene rebuilt after OBS recovery")
            elif kind == "start_stream":
                stream_ctl.start_stream(stream_ctl.make_client())
                logger.warning("Stream inactive — StartStream issued")
        except Exception:
            logger.exception("Watchdog action failed", extra={"action": kind})
    return followups


def main() -> None:
    init_logging()
    config = WatchdogConfig()
    state = WatchdogState()
    logger.info(
        "Stream watchdog started", extra={"poll_seconds": config.poll_seconds}
    )
    while True:
        state, actions = tick(probe_obs(), state, config, now=time.time())
        followups = execute_actions(actions, config, state)
        if followups:
            execute_actions(followups, config, state)
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
