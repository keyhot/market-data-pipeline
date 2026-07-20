"""Scene v1 layout constants (Sprint 11): the stream's face, versioned and
code-reviewed instead of hand-clicked in OBS.

Geometry on a 1920x1080 canvas: live chart on top (1920x840), signals strip
pinned to the bottom edge (1920x120 at y=960), world-event rail overlapping
the chart's right edge (480x840). The 840-960 band stays empty on purpose —
headroom for a future ticker without re-laying the scene out.
"""

import os

SCENE_NAME = "market-world-v1"
CANVAS = (1920, 1080)

# 30fps is plenty for charts; shutdown=False keeps SSE connections alive
# when the source is hidden; pages are already dark, so no custom CSS.
_BROWSER_DEFAULTS = {"fps": 30, "shutdown": False, "restart_when_active": False}


def page_base() -> str:
    return os.environ.get("STREAM_PAGE_BASE", "http://localhost:8000").rstrip("/")


def browser_sources() -> list[dict]:
    base = page_base()
    return [
        {
            "name": "chart-btcusdt-1m",
            "kind": "browser_source",
            "settings": {
                "url": f"{base}/chart/BTCUSDT?interval=1m",
                "width": 1920,
                "height": 840,
                **_BROWSER_DEFAULTS,
            },
            "x": 0,
            "y": 0,
        },
        {
            "name": "overlay-signals",
            "kind": "browser_source",
            "settings": {
                "url": f"{base}/overlay/signals",
                "width": 1920,
                "height": 120,
                **_BROWSER_DEFAULTS,
            },
            "x": 0,
            "y": 960,
        },
        {
            "name": "overlay-events",
            "kind": "browser_source",
            "settings": {
                "url": f"{base}/overlay/events",
                "width": 480,
                "height": 840,
                **_BROWSER_DEFAULTS,
            },
            "x": 1440,
            "y": 0,
        },
    ]


def audio_sources() -> list[dict]:
    """VLC playlist looping over STREAM_AUDIO_DIR; absent when unset so the
    scene builds cleanly before any audio exists."""
    audio_dir = os.environ.get("STREAM_AUDIO_DIR")
    if not audio_dir:
        return []
    return [
        {
            "name": "audio-bed",
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


def scene_spec() -> dict:
    """Full scene description; source order is z-order, bottom to top."""
    return {
        "scene": SCENE_NAME,
        "canvas": CANVAS,
        "sources": browser_sources() + audio_sources(),
    }
