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

# KI-020. The director ran inert for 25 hours: every tick raised, the loop
# caught it, the process never exited, and systemd reported `active` with 0
# restarts while the show was over.
#
# 12 ticks at the default 5s cadence is ~1 minute of nothing working. Below
# that we still swallow — "a director hiccup must never take the stream down"
# was the right rule, it just had no upper bound. Above it we exit and let
# systemd's Restart=always rebuild every dependency, including the OBS client
# that `main` otherwise builds exactly once in the process's lifetime.
MAX_CONSECUTIVE_FAILURES = 12

# A liveness line roughly every 5 minutes at the default cadence. `systemctl
# status` said "active" throughout the outage, and the per-tick counters were
# DEBUG; this is the one line that would have shown a human the show stopped.
HEARTBEAT_TICKS = 60


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
    fetch_state,
    obs_client,
    tts_runner,
    record_event,
    config=None,
    sleep_seconds=5.0,
    max_ticks=None,
) -> int:
    """Injected dependencies keep this testable (fetch_state() -> dict,
    obs_client = stream_ctl seam, tts_runner(text, voice) [Task 6],
    record_event(events) [Task 7]). No globals -> unit-testable via tick().

    Loops forever unless ``max_ticks`` is set (the test seam, as in
    broadcast/service.py). Returns 2 if it gave up after sustained failure so
    systemd restarts it, 0 otherwise.
    """
    config = config or DirectorConfig()
    from scripts import stream_ctl

    # Read the program scene, don't assume it (KI-034). OBS outlives a director
    # restart, so `home_scene` was a guess that was wrong every time the show
    # wasn't already home: the director would then either strand the program on
    # a scene it thought it had left, or write a `from` that never happened into
    # an append-only log. None (no client, or OBS won't say) keeps the old
    # assumption as the fallback.
    on_air = stream_ctl.current_scene(obs_client)
    if on_air and on_air != config.home_scene:
        logger.info("director resuming on the scene already on air: %s", on_air)
    dir_state = DirectorState(
        current_scene=on_air or config.home_scene,
        last_switch=datetime.now(timezone.utc),
        muted=director_muted(),
    )
    metrics = DirectorMetrics()
    consecutive_failures = 0
    ticks = 0
    seeded = False
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        now = datetime.now(timezone.utc)
        try:
            state = fetch_state()
            if not seeded:
                # A director that just booted does not narrate history (KI-033).
                # dir_state is rebuilt on every process start, so
                # last_seen_event_id is None and lines_for_tick's `None or 0`
                # makes the whole `recent` window look new — the first tick
                # re-spoke up to a dozen events the previous process had
                # already covered. On air that re-announced a five-day-old
                # event as news after the KI-024 outage, because `recent`
                # still held pre-outage rows on recovery.
                #
                # Seed off the high-water mark of the first window we see and
                # say nothing about it. Seed exactly once: a later quiet tick
                # (`recent` empty) must not re-arm this and let the backlog
                # through a second time. An empty first window leaves the mark
                # at None, which is correct for a cold DB — the next real event
                # still clears `> 0`.
                seeded = True
                seen_ids = [e.get("id") or 0 for e in (state.get("recent") or [])]
                if seen_ids:
                    dir_state.last_seen_event_id = max(seen_ids)
                    logger.info(
                        "director starting from event %d — %d earlier event(s) "
                        "in the window are history, not news",
                        dir_state.last_seen_event_id,
                        len(seen_ids),
                    )
            # B10 hand-off: the watchdog can move the program between two of
            # our ticks — it raises the standby card on a genuine drop and
            # lowers it on recovery — and `dir_state.current_scene` is
            # otherwise only ever written by our own switches. Without looking,
            # the policy is asked about a scene the program left minutes ago.
            # Same lesson as KI-034, one restart later: read the program, don't
            # remember it. None means OBS wouldn't say; keep what we had.
            on_air = stream_ctl.current_scene(obs_client)
            if on_air and on_air != dir_state.current_scene:
                logger.info(
                    "the program moved without us: %s -> %s",
                    dir_state.current_scene,
                    on_air,
                )
                dir_state.current_scene = on_air
                # Someone else just changed the program; the dwell clock starts
                # from the change we noticed, not from our own last switch.
                dir_state.last_switch = now
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
            consecutive_failures = 0
        except Exception as exc:  # a director hiccup must never take the stream down
            consecutive_failures += 1
            # Name the thing. This used to be extra={"error": ...}, which the
            # formatter drops — 25 hours of identical warnings that said only
            # that something failed (KI-020).
            logger.warning(
                "Director tick failed (%d in a row): %s: %s",
                consecutive_failures,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "Director failed %d consecutive ticks — exiting so systemd "
                    "rebuilds its dependencies (last error %s: %s)",
                    consecutive_failures,
                    type(exc).__name__,
                    exc,
                )
                return 2
        if ticks % HEARTBEAT_TICKS == 0:
            # Not DEBUG: this is the line that says the show is still running.
            logger.info(
                "director alive: %d lines, %d switches, %d tts failures so far",
                metrics.lines_spoken,
                metrics.scene_switches,
                metrics.tts_failures,
            )
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
    return 0


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

    # Propagated, not discarded: `run` returns 2 when it gives up after
    # sustained failure, and the whole point is that the process ends so
    # systemd rebuilds the OBS client this function built exactly once.
    return run(
        fetch_state=lambda: _fetch_world_state(state_url),
        obs_client=obs_client,
        tts_runner=_silent_tts,
        record_event=record_director_events,
    )


if __name__ == "__main__":
    sys.exit(main())
