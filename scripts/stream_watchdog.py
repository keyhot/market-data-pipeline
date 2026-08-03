"""Stream watchdog (Sprint 11): notices OBS or the stream dying and brings it
back without a human. Detector only — world/stream_events.py is the recorder.

Pure state machine (tick) + thin runner loop, so the logic is tested with no
OBS, no clock, and no DB. Compose restart policies already self-heal the
api/scheduler containers; this covers the host-side pieces compose can't.
"""

import logging
import subprocess
import sys
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
    obs_command: tuple = ("obs", "--startstreaming", "--minimize-to-tray")
    # KI-015: how long to wait for a relaunched OBS to answer its websocket
    # before calling the attempt failed. OBS takes a few seconds to boot and
    # load a scene collection full of browser sources.
    relaunch_verify_seconds: float = 25.0
    relaunch_poll_seconds: float = 2.0
    # Consecutive failed relaunches before the world log is told *why* the
    # stream isn't coming back. Once per streak — silence was the bug, spam
    # is not the fix.
    escalate_after_failed_relaunches: int = 3


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


def _obs_pids() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-x", "obs"], capture_output=True, text=True, check=False
    )
    return [int(pid) for pid in result.stdout.split() if pid.isdigit()]


def _terminate(pid: int) -> None:
    import os
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already gone — that's the outcome we wanted
    except PermissionError:
        logger.warning("Cannot signal OBS process", extra={"pid": pid})


def clear_wedged_obs(config: WatchdogConfig, list_pids=None, kill=None) -> int:
    """Terminate any OBS still running while its websocket is unreachable.

    This only runs on the relaunch path, which by definition means OBS is not
    answering. A half-dead instance holds the single-instance lock, so every
    relaunch silently no-ops against it — the watchdog kept "relaunching" into
    a wall. Clearing it first is what makes the retry mean anything.
    """
    list_pids = list_pids or _obs_pids
    kill = kill or _terminate
    pids = list_pids()
    for pid in pids:
        logger.warning("Clearing unresponsive OBS before relaunch",
                       extra={"pid": pid})
        kill(pid)
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
    launch(list(config.obs_command))

    waited = 0.0
    while waited < config.relaunch_verify_seconds:
        sleep(config.relaunch_poll_seconds)
        waited += config.relaunch_poll_seconds
        if probe().get("reachable"):
            logger.info("OBS relaunch verified", extra={"seconds": waited})
            return True
    logger.error(
        "OBS did not come up after relaunch",
        extra={"waited_seconds": waited},
    )
    return False


def _launch_obs(command: list[str]) -> None:
    subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


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
