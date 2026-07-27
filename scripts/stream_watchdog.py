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


@dataclass
class WatchdogState:
    obs_up: bool = True
    streaming: bool = False
    last_restart_at: float | None = None
    down_since: float | None = None
    dropped_flagged: bool = False


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


def execute_actions(actions: list[tuple], config: WatchdogConfig) -> None:
    for action in actions:
        kind = action[0]
        try:
            if kind == "record":
                record_stream_event(action[1], action[2])
            elif kind == "relaunch_obs":
                logger.warning("OBS unreachable — relaunching")
                subprocess.Popen(
                    list(config.obs_command),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif kind == "rebuild_scene":
                stream_ctl.build_scene(stream_ctl.make_client())
                logger.info("Scene rebuilt after OBS recovery")
            elif kind == "start_stream":
                stream_ctl.start_stream(stream_ctl.make_client())
                logger.warning("Stream inactive — StartStream issued")
        except Exception:
            logger.exception("Watchdog action failed", extra={"action": kind})


def main() -> None:
    init_logging()
    config = WatchdogConfig()
    state = WatchdogState()
    logger.info(
        "Stream watchdog started", extra={"poll_seconds": config.poll_seconds}
    )
    while True:
        state, actions = tick(probe_obs(), state, config, now=time.time())
        execute_actions(actions, config)
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
