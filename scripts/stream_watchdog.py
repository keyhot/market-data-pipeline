"""Stream watchdog (Sprint 11): notices OBS or the stream dying and brings it
back without a human. Detector only — world/stream_events.py is the recorder.

Pure state machine (tick) + thin runner loop, so the logic is tested with no
OBS, no clock, and no DB. Compose restart policies already self-heal the
api/scheduler containers; this covers the host-side pieces compose can't.
"""

import codecs
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from scripts import stream_ctl, stream_scene  # noqa: E402
from world.stream_events import record_stream_event  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogConfig:
    poll_seconds: float = 30.0
    # A sustained encoder-skip ratio is an ENCODING symptom, not a bandwidth
    # one (KI-032): it comes from output_skipped_frames. Record it, never
    # restart — a restart makes either fault worse.
    dropped_ratio_threshold: float = 0.05
    # KI-032: the network signal the skip rule was misread as. OBS derives
    # output_congestion (0.0-1.0) from actual RTMP drops, so this is the rule
    # that can genuinely see "insufficient bandwidth/connection stalls".
    congestion_threshold: float = 0.3
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
    # KI-021: a ratio needs a denominator. OBS's frame counters are PER OUTPUT
    # SESSION, so a reconnect zeroes them — and one skipped frame against a
    # 20-frame total reads as 5% and fires a spurious dropped_frames event on
    # every single reconnect. ~1 minute of video at 30fps before the rule may
    # speak.
    min_frames_for_ratio: int = 1800
    # KI-024: the health of the CONTENT is not the health of the OUTPUT. OBS can
    # stream a dead API's error pages for 12 hours at "100% uptime".
    content_health_url: str = "http://127.0.0.1:8000/health"
    content_timeout_seconds: float = 5.0
    # Debounce: a single 30s blip recorded as an outage makes the uptime number
    # less believable, which is the opposite of the point. Three consecutive
    # failures (~90s) is a real outage, not a restart.
    content_failures_before_drop: int = 3


@dataclass
class WatchdogState:
    obs_up: bool = True
    streaming: bool = False
    last_restart_at: float | None = None
    down_since: float | None = None
    dropped_flagged: bool = False
    # KI-032: tracked separately from dropped_flagged — an overloaded encoder
    # on a clean link and a clean encoder on a congested link are different
    # faults, and either can start or clear while the other persists.
    congestion_flagged: bool = False
    failed_relaunches: int = 0
    # B10: is the standby card currently on air? Switching is done on
    # *transitions* only — the director owns the scene while the stream is
    # healthy, and a watchdog that re-asserted a scene every tick would fight
    # it for control.
    on_standby: bool = False
    # KI-021: last seen frame counters, for detecting the reset that an RTMP
    # reconnect causes. A *decrease* is the one unambiguous signal that a new
    # output session began, and it needs nothing new from OBS.
    last_total_frames: int | None = None
    last_skipped_frames: int = 0
    reconnecting: bool = False
    # KI-024: content health is tracked separately from output health, because
    # they fail independently — that is the whole bug.
    content_ok: bool = True
    content_failures: int = 0
    content_down_since: float | None = None


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
        # KI-035: this is a *new* OBS process — the watchdog's own relaunch, or
        # any other restart — and its output counters start from zero. Diffing
        # them against the dead process's totals reads as an RTMP re-dial, which
        # is how the A6 soak recorded a reconnect 8ms after the recovery that
        # caused it, stamped with the pre-kill session's frame count. The
        # `obs_reconnecting` branch already forgets the counters for the same
        # reason; this path never learned to. Let the next probe seed them.
        state.last_total_frames = None
        state.last_skipped_frames = 0

    if probe.get("streaming"):
        if not state.streaming:
            payload = {}
            if state.down_since is not None:
                payload["recovery_seconds"] = round(now - state.down_since, 1)
                state.down_since = None
            actions.append(("record", "stream_started", payload))
        state.streaming = True
        if state.on_standby:
            # Back on air: hand the scene back to the director, once.
            actions.append(("switch_scene", stream_scene.SCENE_CHART))
            state.on_standby = False
        actions.extend(_note_reconnect(probe, state))
        actions.extend(_check_content(probe, state, config, now))
        ratio = probe.get("dropped_ratio", 0.0)
        total = int(probe.get("total_frames") or 0)
        # KI-021: the rule may only speak once its denominator means something.
        # Straight after a reconnect the counters restart from zero, so the
        # ratio it reads is "since the last reconnect" over a handful of frames.
        if total >= config.min_frames_for_ratio and ratio >= config.dropped_ratio_threshold:
            if not state.dropped_flagged:
                actions.append(
                    (
                        "record",
                        "stream_dropped",
                        {
                            # KI-032: named for what it measures. This ratio is
                            # built from output_skipped_frames — the encoder
                            # couldn't keep up — NOT from bandwidth loss. The
                            # old name for this was `dropped_frames`, which
                            # history keeps and STREAM_DEGRADED_REASONS still
                            # accepts.
                            "reason": "encoder_overloaded",
                            "dropped_ratio": round(ratio, 4),
                            "total_frames": total,
                            # Carried alongside so the two faults are always
                            # comparable in one row.
                            "congestion": round(
                                float(probe.get("congestion") or 0.0), 4
                            ),
                        },
                    )
                )
                state.dropped_flagged = True
        elif ratio < config.dropped_ratio_threshold:
            state.dropped_flagged = False

        # KI-032: the network rule the old one was mistaken for. Independent of
        # the encoder rule in both directions — either can fire, clear and
        # re-arm while the other persists — so it gets its own flag.
        congestion = float(probe.get("congestion") or 0.0)
        if congestion >= config.congestion_threshold:
            if not state.congestion_flagged:
                actions.append(
                    (
                        "record",
                        "stream_dropped",
                        {
                            "reason": "network_congested",
                            "congestion": round(congestion, 4),
                            "dropped_ratio": round(ratio, 4),
                            "total_frames": total,
                        },
                    )
                )
                state.congestion_flagged = True
        else:
            state.congestion_flagged = False
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
    # B10: OBS is answering but the output is not live, so put the standby card
    # up rather than leaving a frozen frame on screen while we restart. Note
    # this is deliberately outside the `state.streaming` branch above: a
    # watchdog that starts up into an already-dead stream must show the card
    # too, not only on the live->dead transition.
    if not state.on_standby:
        actions.append(("switch_scene", stream_scene.SCENE_STANDBY))
        state.on_standby = True
    if _restart_allowed(state, config, now):
        actions.append(("start_stream",))
        state.last_restart_at = now
    return state, actions


def _note_reconnect(probe: dict, state: WatchdogState) -> list[tuple]:
    """KI-021: notice an RTMP reconnect OBS never told anyone about. Pure.

    YouTube dropped the ingest three times in 26h. Each time OBS re-dialled in
    ~2.5s, kept `outputActive` **true** throughout, and reset the output's frame
    counters. So `tick` saw no live->inactive transition, no `stream_stopped`
    was recorded, and the report read 100% across a night with three real gaps.

    Two detectors for one event, because the poll is 30s and the gap is 2.5s:

    * `outputReconnecting` catches one that is in flight — the lucky case, and
      the best one, since the pre-reset counters are still readable;
    * a **decrease** in `output_total_frames` catches one that began and ended
      entirely between two polls, which is the usual case.

    The pre-reset numbers are stamped into the event payload rather than
    accumulated in `WatchdogState`: state is in-memory and a watchdog restart
    would zero it, which is the very problem being fixed. The append-only log is
    the durable record, and it is where soak_report reads from.
    """
    actions: list[tuple] = []
    total = probe.get("total_frames")
    skipped = int(probe.get("skipped_frames") or 0)
    prev_total, prev_skipped = state.last_total_frames, state.last_skipped_frames

    def _payload(detected: str) -> dict:
        body = {"detected": detected}
        if prev_total:
            body["frames_before"] = prev_total
            body["skipped_before"] = prev_skipped
            body["skipped_ratio_before"] = round(prev_skipped / prev_total, 4)
        return body

    reconnecting = bool(probe.get("reconnecting"))
    rising_edge = reconnecting and not state.reconnecting
    state.reconnecting = reconnecting

    if rising_edge:
        actions.append(("record", "stream_reconnected", _payload("obs_reconnecting")))
        # Forget the counters: they are about to reset, and the reset must not
        # then be recorded a second time as a separate reconnect.
        state.last_total_frames = None
        state.last_skipped_frames = 0
        return actions

    if total is not None:
        if prev_total is not None and total < prev_total:
            actions.append(
                ("record", "stream_reconnected", _payload("frame_counter_reset"))
            )
        state.last_total_frames = total
        state.last_skipped_frames = skipped
    return actions


def _check_content(
    probe: dict, state: WatchdogState, config: WatchdogConfig, now: float
) -> list[tuple]:
    """KI-024: assert the stream has something true to show, not just pixels.

    On 2026-08-19 the whole data stack was down for ~12h while OBS streamed
    continuously with `outputActive` true. Every measure this project had said
    the stream was fine, because every one of them measured the *output*: the
    watchdog read GetStreamStatus, and `compute_uptime` folds `stream_*` rows
    that could not be written because the database was the thing that was down
    — and no events in a window is not "unknown" to that fold, it is ~100%.

    So the content gets its own probe, and a failure is recorded as ordinary
    downtime (`stream_dropped`, NOT degraded — see
    `world.state.STREAM_DEGRADED_REASONS`). Note the recursive trap the KI names:
    the evidence lives in the database that goes down with everything else. It
    survives because `record_stream_event` spools to JSONL and flushes on
    recovery, which is why that spool's wedging bug had to be fixed first.

    Deliberately NOT a restart trigger: compose already restarts the containers,
    and an OBS relaunch cannot fix a dead Postgres. This records the truth; it
    does not thrash.
    """
    healthy = bool(probe.get("content_ok", True))
    if healthy:
        state.content_failures = 0
        if not state.content_ok:
            payload = {"reason": "content_restored"}
            if state.content_down_since is not None:
                payload["outage_seconds"] = round(now - state.content_down_since, 1)
                state.content_down_since = None
            state.content_ok = True
            # `stream_started` closes the outage in compute_uptime's fold. The
            # stream never stopped — but the fold's vocabulary is "dropped opens
            # downtime, started closes it", and inventing a third verb every
            # consumer must learn is how two sources of truth get born (KI-019).
            return [("record", "stream_started", payload)]
        return []

    state.content_failures += 1
    if state.content_ok and state.content_failures >= config.content_failures_before_drop:
        state.content_ok = False
        state.content_down_since = now
        return [
            (
                "record",
                "stream_dropped",
                {
                    "reason": "content_unreachable",
                    "detail": probe.get("content_detail", "unknown"),
                    "consecutive_failures": state.content_failures,
                },
            )
        ]
    return []


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
        # KI-021: the counters the reconnect detector reads.
        "total_frames": status.get("total_frames", 0),
        "skipped_frames": status.get("skipped_frames", 0),
        "reconnecting": status.get("reconnecting", False),
        "congestion": status.get("congestion", 0.0),
    }


def probe_content(config: WatchdogConfig, opener=None) -> dict:
    """KI-024: can the pages on screen actually reach their data?

    **`/health` answers HTTP 200 during a Postgres outage** — `status: 200` is
    hardcoded in the response body and the endpoint's job is to report, not to
    fail. A probe that checked the status code would pass for exactly the
    failure class this exists to catch, so the *body* is the check:
    `data.postgres.connected` must be `True`. `None` there means writes are
    disabled, which for a stream whose world log is the show is not healthy
    either.
    """
    opener = opener or urllib.request.urlopen
    try:
        with opener(config.content_health_url, timeout=config.content_timeout_seconds) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                return {"content_ok": False, "content_detail": f"http {status}"}
            body = json.loads(resp.read().decode())
    except Exception as exc:
        return {"content_ok": False, "content_detail": f"{type(exc).__name__}: {exc}"}
    postgres = ((body.get("data") or {}).get("postgres") or {})
    if postgres.get("connected") is not True:
        return {
            "content_ok": False,
            "content_detail": f"postgres connected={postgres.get('connected')!r}",
        }
    return {"content_ok": True}


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
            elif kind == "switch_scene":
                stream_ctl.switch_scene(stream_ctl.make_client(), action[1])
                logger.info("Scene switched", extra={"scene": action[1]})
        except Exception:
            logger.exception("Watchdog action failed", extra={"action": kind})
    return followups


def seed_state(probe: dict) -> WatchdogState:
    """The state a starting watchdog should already be in (KI-038). Pure.

    `WatchdogState` defaults to `streaming=False`, which is an assumption, not
    an observation — and OBS outlives a watchdog restart. So the first tick of
    a new process against a stream that had been live for 23 hours read as a
    live edge and wrote `stream_started` into an append-only log; it went out on
    the events rail as "stream went live". Uptime survives the ordinary case (a
    start with no open outage closes nothing), but a restart *during* a real
    outage closes that outage early and understates downtime — the direction
    that flatters.

    Only `streaming` is seeded. `obs_up` deliberately keeps its default: an OBS
    that will not answer is a true statement about now, whenever the outage
    began, and suppressing that would trade a false positive for a silence.

    Same family as KI-033 (the director re-announced its backlog) and KI-034
    (it assumed the scene it was on): state rebuilt on restart from an
    assumption rather than from the world.
    """
    return WatchdogState(
        streaming=bool(probe.get("reachable")) and bool(probe.get("streaming"))
    )


def main() -> None:
    init_logging()
    config = WatchdogConfig()
    # Look before assuming (KI-038): one probe seeds the state, so a restart
    # into a stream that never stopped announces nothing.
    state = seed_state(probe_obs())
    logger.info(
        "Stream watchdog started",
        extra={"poll_seconds": config.poll_seconds, "streaming": state.streaming},
    )
    while True:
        probe = probe_obs()
        # Only when OBS is up and pushing: during an OBS outage the stream is
        # already correctly counted as down, and a second overlapping outage
        # (plus the `stream_started` that closes it on recovery) would muddy
        # the fold rather than sharpen it.
        if probe.get("reachable") and probe.get("streaming"):
            probe.update(probe_content(config))
        state, actions = tick(probe, state, config, now=time.time())
        followups = execute_actions(actions, config, state)
        if followups:
            execute_actions(followups, config, state)
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
