"""Scene layouts (Sprint 11 → multi-scene in Sprint 13): the stream's face,
versioned and code-reviewed instead of hand-clicked in OBS.

Three scenes the director switches between on salience (Sprint 13), all on a
1920x1080 canvas:

- **chart-focus** (default / home): the composite — live chart on top
  (1920x840), signals strip pinned to the bottom (1920x120 at y=960),
  world-event rail on the chart's right edge (480x840). This is the calm home
  the director rests on and decays back to.
- **world-focus**: the /world room foregrounded (1920x960) with the signals
  strip — for model/trader moments.
- **event-focus**: the world-event feed large with a small chart and the strip
  — for a burst of market events.

All three reuse existing Browser Source pages — no new asset pipeline. Each
scene carries its own uniquely-named browser sources: a handful of extra
browser instances kept alive (shutdown=False) for instant switching and live
SSE. Consolidating shared sources (one strip across scenes, scaled scene items)
is a later perf pass; v0 favours self-contained, idempotent scenes. The audio
bed, when configured, is one bed per scene (unique name) so audio is present in
every scene — a single global bed is a go-live OBS-routing optimization.
"""

import os

CANVAS = (1920, 1080)

SCENE_CHART = "chart-focus"
SCENE_WORLD = "world-focus"
SCENE_EVENT = "event-focus"
SCENE_NAME = SCENE_CHART  # legacy alias: the default / home scene

# 30fps is plenty for charts; shutdown=False keeps SSE connections alive when a
# source is in a hidden scene; pages are already dark, so no custom CSS.
_BROWSER_DEFAULTS = {"fps": 30, "shutdown": False, "restart_when_active": False}


def page_base() -> str:
    return os.environ.get("STREAM_PAGE_BASE", "http://localhost:8000").rstrip("/")


def _browser(name: str, path: str, width: int, height: int, x: int, y: int) -> dict:
    return {
        "name": name,
        "kind": "browser_source",
        "settings": {
            "url": f"{page_base()}{path}",
            "width": width,
            "height": height,
            **_BROWSER_DEFAULTS,
        },
        "x": x,
        "y": y,
    }


def audio_sources(suffix: str = "") -> list[dict]:
    """VLC playlist looping over STREAM_AUDIO_DIR; absent when unset so scenes
    build cleanly before any audio exists. `suffix` keeps the input name unique
    per scene (the home scene keeps the bare 'audio-bed' name)."""
    audio_dir = os.environ.get("STREAM_AUDIO_DIR")
    if not audio_dir:
        return []
    name = "audio-bed" if not suffix else f"audio-bed-{suffix}"
    return [
        {
            "name": name,
            "kind": "vlc_source",
            "settings": {
                "playlist": [{"value": audio_dir, "hidden": False, "selected": False}],
                "loop": True,
                "shuffle": False,
            },
            "x": 0,
            "y": 0,
        }
    ]


def _chart_focus() -> dict:
    """Default / home: the chart-forward composite (the legacy single scene)."""
    return {
        "scene": SCENE_CHART,
        "canvas": CANVAS,
        "sources": [
            _browser("chart-btcusdt-1m", "/chart/BTCUSDT?interval=1m", 1920, 840, 0, 0),
            _browser("overlay-signals", "/overlay/signals", 1920, 120, 0, 960),
            _browser("overlay-events", "/overlay/events", 480, 840, 1440, 0),
            *audio_sources(),
        ],
    }


def _world_focus() -> dict:
    """The living-world room foregrounded, with the signals strip."""
    return {
        "scene": SCENE_WORLD,
        "canvas": CANVAS,
        "sources": [
            _browser("world-room", "/world", 1920, 960, 0, 0),
            _browser("world-signals", "/overlay/signals", 1920, 120, 0, 960),
            *audio_sources("world"),
        ],
    }


def _event_focus() -> dict:
    """The world-event feed large, with a small chart and the signals strip."""
    return {
        "scene": SCENE_EVENT,
        "canvas": CANVAS,
        "sources": [
            _browser("event-feed", "/overlay/events", 960, 1080, 960, 0),
            _browser("event-chart", "/chart/BTCUSDT?interval=1m", 960, 540, 0, 0),
            _browser("event-signals", "/overlay/signals", 960, 120, 0, 960),
            *audio_sources("event"),
        ],
    }


def scenes_spec() -> list[dict]:
    """All scenes the director switches between (Sprint 13). Order matters: the
    first entry is the default / home scene. Source order within a scene is
    z-order, bottom to top."""
    return [_chart_focus(), _world_focus(), _event_focus()]


def scene_spec() -> dict:
    """Back-compat shim: single-scene callers (existing tests) get the default /
    home scene."""
    return scenes_spec()[0]
