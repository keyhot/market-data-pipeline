"""Director decision layer (Sprint 13): pure functions over world state.

Shaped like scripts/stream_watchdog.py — ``tick()`` decides, the runner acts —
so every decision tests with no OBS, no Piper, no clock, and no DB. Defaults are
tuned for the ambient "fireplace that swells" register: calm by default, a low
line rate, a home scene the director rests on and decays back to.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class DirectorConfig:
    # Tuned for the ambient "fireplace that swells" register — calm by default.
    home_scene: str = "chart-focus"     # the calm default the director rests on
    min_dwell_seconds: int = 60         # a scene must hold this long before switching
    return_to_home_seconds: int = 120   # after this long with nothing salient, go home
    max_switches_per_minute: int = 2
    max_lines_per_minute: int = 3       # LOW on purpose — silence is fine on ambient
    anti_repeat_window: int = 10        # don't reuse a character's last N lines


@dataclass
class DirectorState:
    current_scene: str
    last_switch: datetime
    recent_switch_times: list = field(default_factory=list)
    recent_line_times: list = field(default_factory=list)
    recent_lines_by_character: dict = field(default_factory=dict)
    last_seen_event_id: int | None = None
    muted: bool = False


@dataclass
class DirectorAction:
    scene: str | None = None                     # None => hold the current scene
    lines: list = field(default_factory=list)    # [{character, text, voice, event_id}]


def tick(state, dir_state, now, config):
    """Pure: given world state + director state + clock, return the action.
    Mutates nothing on ``dir_state``. The runner applies the action and updates
    ``dir_state``."""
    from director.commentary import lines_for_tick
    from director.scenes import choose_scene

    scene = choose_scene(state, dir_state, now, config)
    lines = [] if dir_state.muted else lines_for_tick(state, dir_state, now, config)
    return DirectorAction(
        scene=scene if scene != dir_state.current_scene else None,
        lines=lines,
    )


@dataclass
class DirectorMetrics:
    scene_switches: int = 0
    switches_suppressed: int = 0
    lines_spoken: int = 0
    lines_suppressed: int = 0
    tts_failures: int = 0


def _count_within_window(times, now, window_seconds: int = 60) -> int:
    cutoff = now - timedelta(seconds=window_seconds)
    return sum(1 for t in times if t > cutoff)


def within_switch_budget(dir_state, now, config) -> bool:
    """Pure: fewer than max_switches_per_minute scene switches in the last 60s."""
    return (
        _count_within_window(dir_state.recent_switch_times, now)
        < config.max_switches_per_minute
    )


def within_line_budget(dir_state, now, config) -> bool:
    """Pure: fewer than max_lines_per_minute lines spoken in the last 60s."""
    return (
        _count_within_window(dir_state.recent_line_times, now)
        < config.max_lines_per_minute
    )
