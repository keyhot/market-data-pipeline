"""Director runner (Sprint 13): poll /world/state, tick(), apply via injected
clients. All I/O lives here; director.policy stays pure. Shaped like
scripts/stream_watchdog.py — a thin loop over a pure decision.
"""

import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from director.policy import DirectorConfig, DirectorState, tick  # noqa: E402

logger = logging.getLogger(__name__)


def director_enabled() -> bool:
    raw = os.environ.get("DIRECTOR_ENABLED")
    return raw is None or raw.strip().lower() not in {"0", "false", "no"}


def _fetch_world_state(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = json.load(resp)
    # /world/state returns the ApiResponse envelope: {"data": {...}}.
    return body.get("data", body)


def run(fetch_state, obs_client, tts_runner, record_event,
        config=None, sleep_seconds=5.0) -> None:
    """Injected dependencies keep this testable (fetch_state() -> dict,
    obs_client = stream_ctl seam, tts_runner(text, voice) [Task 6],
    record_event(events) [Task 7]). No globals -> unit-testable via tick()."""
    config = config or DirectorConfig()
    dir_state = DirectorState(
        current_scene=config.home_scene,
        last_switch=datetime.now(timezone.utc),
    )
    while True:
        now = datetime.now(timezone.utc)
        try:
            state = fetch_state()
            action = tick(state, dir_state, now, config)
            _apply(action, dir_state, now, obs_client, tts_runner, record_event)
            # Advance past the events we've now reacted to, so lines aren't
            # re-spoken every tick (the runner owns this mutable bookkeeping).
            recent = state.get("recent") or []
            if recent:
                dir_state.last_seen_event_id = max(
                    dir_state.last_seen_event_id or 0,
                    max((e.get("id") or 0) for e in recent),
                )
        except Exception as exc:  # a director hiccup must never take the stream down
            logger.warning("Director tick failed", extra={"error": str(exc)})
        time.sleep(sleep_seconds)


def _apply(action, dir_state, now, obs_client, tts_runner, record_event) -> None:
    """Apply a decided action and advance dir_state. Decisions live in tick();
    this is the only place with side effects. Task 6 wires real TTS, Task 7
    replaces the inline event dicts with director/events builders + safety rails
    land in Task 8."""
    from scripts import stream_ctl

    events = []
    if action.scene and action.scene != dir_state.current_scene:
        stream_ctl.switch_scene(obs_client, action.scene)
        events.append({
            "event_type": "scene_switched", "severity": 1.0,
            "occurred_at": now, "symbol": None,
            "payload": {"scene": action.scene, "from": dir_state.current_scene},
        })
        dir_state.current_scene = action.scene
        dir_state.last_switch = now
        dir_state.recent_switch_times.append(now)
    for line in action.lines:
        if tts_runner is not None:
            tts_runner(line.get("text", ""), line.get("voice", ""))
        dir_state.recent_line_times.append(now)
        dir_state.recent_lines_by_character.setdefault(
            line.get("character", ""), []
        ).append(line.get("text", ""))
        events.append({
            "event_type": "commentary_spoken", "severity": 1.0,
            "occurred_at": now, "symbol": line.get("symbol"),
            "payload": {"character": line.get("character"),
                        "text": line.get("text"),
                        "event_id": line.get("event_id")},
        })
    if events and record_event is not None:
        record_event(events)


def main() -> int:
    init_logging()
    if not director_enabled():
        logger.info("Director disabled (DIRECTOR_ENABLED); exiting")
        return 0
    from scripts import stream_ctl

    base = os.environ.get("DIRECTOR_STATE_URL", "http://localhost:8000").rstrip("/")
    state_url = f"{base}/world/state"
    try:
        obs_client = stream_ctl.make_client()
    except stream_ctl.ObsUnreachable as exc:
        logger.error("Director cannot reach OBS: %s", exc)
        return 2

    def _noop(*_args, **_kwargs):
        return None

    run(
        fetch_state=lambda: _fetch_world_state(state_url),
        obs_client=obs_client,
        tts_runner=_noop,    # tts.synthesize() ready; OBS media playback is go-live
        record_event=_noop,  # Task 7 wires director/events
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
