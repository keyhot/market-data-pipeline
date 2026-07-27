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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging import init_logging  # noqa: E402
from director.policy import (  # noqa: E402
    DirectorConfig,
    DirectorMetrics,
    DirectorState,
    tick,
)

logger = logging.getLogger(__name__)


def director_enabled() -> bool:
    raw = os.environ.get("DIRECTOR_ENABLED")
    return raw is None or raw.strip().lower() not in {"0", "false", "no"}


def director_muted() -> bool:
    """Global brake for commentary (env DIRECTOR_MUTED) — no redeploy needed."""
    raw = os.environ.get("DIRECTOR_MUTED")
    return raw is not None and raw.strip().lower() not in {"0", "false", "no", ""}


def _fetch_world_state(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = json.load(resp)
    # /world/state returns the ApiResponse envelope: {"data": {...}}.
    return body.get("data", body)


def run(
    fetch_state, obs_client, tts_runner, record_event, config=None, sleep_seconds=5.0
) -> None:
    """Injected dependencies keep this testable (fetch_state() -> dict,
    obs_client = stream_ctl seam, tts_runner(text, voice) [Task 6],
    record_event(events) [Task 7]). No globals -> unit-testable via tick()."""
    config = config or DirectorConfig()
    dir_state = DirectorState(
        current_scene=config.home_scene,
        last_switch=datetime.now(timezone.utc),
        muted=director_muted(),
    )
    metrics = DirectorMetrics()
    while True:
        now = datetime.now(timezone.utc)
        try:
            state = fetch_state()
            action = tick(state, dir_state, now, config)
            _apply(
                action,
                dir_state,
                now,
                obs_client,
                tts_runner,
                record_event,
                config,
                metrics,
            )
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
        logger.debug(
            "director metrics",
            extra={
                "scene_switches": metrics.scene_switches,
                "switches_suppressed": metrics.switches_suppressed,
                "lines_spoken": metrics.lines_spoken,
                "lines_suppressed": metrics.lines_suppressed,
                "tts_failures": metrics.tts_failures,
            },
        )
        time.sleep(sleep_seconds)


def _apply(
    action, dir_state, now, obs_client, tts_runner, record_event, config, metrics
) -> None:
    """Apply a decided action and advance dir_state — the only place with side
    effects. Per-minute budgets are enforced here (keyed off tick's proposed
    action) so a runaway tick can't flap OBS or spam TTS."""
    from director.events import build_commentary_spoken, build_scene_switched
    from director.policy import within_line_budget, within_switch_budget
    from scripts import stream_ctl

    events = []
    if action.scene and action.scene != dir_state.current_scene:
        if within_switch_budget(dir_state, now, config):
            stream_ctl.switch_scene(obs_client, action.scene)
            events.append(
                build_scene_switched(action.scene, dir_state.current_scene, now)
            )
            dir_state.current_scene = action.scene
            dir_state.last_switch = now
            dir_state.recent_switch_times.append(now)
            metrics.scene_switches += 1
        else:
            metrics.switches_suppressed += 1
    for i, line in enumerate(action.lines):
        if not within_line_budget(dir_state, now, config):
            metrics.lines_suppressed += 1
            continue
        # tts_runner returns True on success; a real Piper failure returns False
        # (the stream keeps running silently — see director/tts.py).
        if tts_runner is not None and not tts_runner(
            line.get("text", ""), line.get("voice", "")
        ):
            metrics.tts_failures += 1
        dir_state.recent_line_times.append(now)
        dir_state.recent_lines_by_character.setdefault(
            line.get("character", ""), []
        ).append(line.get("text", ""))
        events.append(
            build_commentary_spoken(
                line.get("character"),
                line.get("text"),
                event_id=line.get("event_id"),
                symbol=line.get("symbol"),
                # Several characters can speak about the same (symbol, event) in
                # one tick; without distinct sub-timestamps they'd share
                # (event_type, occurred_at, symbol) and collide on
                # uq_world_events_natural, failing the whole batch. They are
                # genuinely separate utterances, so order them microseconds apart.
                occurred_at=now + timedelta(microseconds=i),
            )
        )
        metrics.lines_spoken += 1
    # Always call the recorder (even with no events) so it drains the spool
    # backlog every tick — flush-on-tick, not only when the director acts.
    if record_event is not None:
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

    from director.events import record_director_events

    def _silent_tts(_text, _voice):
        return True  # no-op success; OBS media-source playback is a go-live step

    run(
        fetch_state=lambda: _fetch_world_state(state_url),
        obs_client=obs_client,
        tts_runner=_silent_tts,
        record_event=record_director_events,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
