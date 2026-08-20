"""Scene layouts (Sprint 11 → multi-scene in Sprint 13): the stream's face,
versioned and code-reviewed instead of hand-clicked in OBS.

Three scenes the director switches between on salience (Sprint 13), all on a
1920x1080 canvas:

- **chart-focus** (default / home): the composite — live chart (1440x960) with
  the world-event rail beside it (480x960 at x=1440) and the signals strip
  pinned to the bottom (1920x120 at y=960). This is the calm home the director
  rests on and decays back to.
- **world-focus**: the /world room foregrounded (1920x960) with the signals
  strip — for model/trader moments.
- **event-focus**: the world-event feed large (960x1080) with a single-symbol
  chart and the strip on the left — for a burst of market events.

Every scene **tiles** the 1920x1080 canvas: the sources are disjoint and cover
it exactly, asserted by `test_every_scene_tiles_the_canvas_exactly`. Overlap is
how the events rail was invisible for three weeks (KI-023) and then how it
buried ETHUSDT's price scale once it was on top (KI-025); a gap is how 154px of
the home frame went out as pure black (KI-029). Neither is a thing to eyeball.

All scenes reuse existing Browser Source pages — no new asset pipeline. Each
scene carries its own uniquely-named browser sources: a handful of extra
browser instances kept alive (shutdown=False) for instant switching and live
SSE. Consolidating shared sources (one strip across scenes, scaled scene items)
is a later perf pass; v0 favours self-contained, idempotent scenes. The audio
bed, when configured, is one bed per scene (unique name) so audio is present in
every scene — a single global bed is a go-live OBS-routing optimization.
"""

import os
from urllib.parse import urlparse

CANVAS = (1920, 1080)

# KI-013. Every browser source lives in ONE shared obs-browser process, so they
# share one Chromium network stack — and Chromium allows at most **6 concurrent
# HTTP/1.1 connections per origin**. Each page holds one open forever for its
# SSE stream, and with `shutdown: False` every scene's sources stay alive at
# once. Eight sources on `http://localhost:8000` exhausted the budget, and the
# 7th+ request — `/world/state` — queued forever: the room rendered its boot
# frame and never drew any data. Measured on 2026-08-02: exactly 6 ESTAB
# connections from the obs-browser pid, and parking the other sources on
# about:blank made the room render completely with nothing else changed.
#
# Loopback is a /8, so every 127.x.y.z is the same server but a *different
# origin* to Chromium — one connection pool each. Sharding across them is the
# minimal fix that keeps `shutdown: False` (instant scene switches, no reload
# flicker mid-stream).
_SHARD_HOSTS = (
    "127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.4",
    "127.0.0.5", "127.0.0.6", "127.0.0.7", "127.0.0.8",
    "127.0.0.9", "127.0.0.10", "127.0.0.11", "127.0.0.12",
)
# Only loopback can be sharded this way; a real host (compose's `api`, a remote
# box) has one address and must be left alone.
_SHARDABLE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

SCENE_CHART = "chart-focus"
SCENE_WORLD = "world-focus"
SCENE_EVENT = "event-focus"
# B10: the graceful-degradation surface. Not a scene the director can choose —
# only the watchdog switches to it, and only while the stream is genuinely down.
SCENE_STANDBY = "standby"
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


# B2 step 5 — the music-swell hook, as a mapping rather than a behaviour.
# Tier 0 is the resting bed level and each step opens it up; monotonic by test,
# because a bigger event that ducked the music would inverse the whole point.
# Design + hook only: nothing calls this on a tick yet, because there are no
# DMCA-safe tracks in STREAM_AUDIO_DIR to swell. Enabling it is an ops step,
# documented in the runbook — the code side is ready and tested.
_AUDIO_GAIN_DB: tuple[float, ...] = (-18.0, -12.0, -6.0, 0.0)


def audio_gain_db(tier: int) -> float:
    """Audio-bed gain in dB for a severity ``tier``; clamps to [0, 3]."""
    clamped = max(0, min(int(tier), len(_AUDIO_GAIN_DB) - 1))
    return _AUDIO_GAIN_DB[clamped]


def audio_source_names() -> list[str]:
    """Every audio-bed input the scene spec would have created (one per scene,
    suffixed) — empty when STREAM_AUDIO_DIR is unset, which is the default."""
    names = []
    for scene in scenes_spec():
        names += [
            source["name"]
            for source in scene["sources"]
            if source.get("kind") == "vlc_source"
        ]
    return names


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
            # 1440 wide, not 1920: the rail starts at x=1440, and a chart
            # drawn under it loses exactly the strip that carries the numbers —
            # ETHUSDT's price scale, its last-price label and its newest
            # candles (KI-025). Both panes now live entirely left of the rail.
            _browser("charts-1m", "/charts?interval=1m", 1440, 960, 0, 0),
            _browser("overlay-signals", "/overlay/signals", 1920, 120, 0, 960),
            # 960 tall, meeting the strip: at 840 it left a 480x120 notch of
            # dead black under the rail (KI-029).
            _browser("overlay-events", "/overlay/events", 480, 960, 1440, 0),
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
            # `/charts?symbols=`, not `/chart/BTCUSDT`: the latter is the page a
            # human opens in a browser, and its `← dashboard` nav link, status
            # line and TradingView footer all went out on air (KI-027).
            # 960x960 fills the column down to the strip; it was 960x540, which
            # left the lower-left quarter of the frame empty and invited the
            # 1.5x upscale that shredded the rail's headlines (KI-026).
            _browser("event-chart", "/charts?interval=1m&symbols=BTCUSDT",
                     960, 960, 0, 0),
            _browser("event-signals", "/overlay/signals", 960, 120, 0, 960),
            *audio_sources("event"),
        ],
    }


def _standby() -> dict:
    """B10: what a viewer sees instead of a frozen or black frame.

    One full-canvas card — anything smaller would leave the dead scene visible
    around its edges. The page itself makes no network requests, so it keeps
    animating through exactly the outage it exists for.
    """
    return {
        "scene": SCENE_STANDBY,
        "canvas": CANVAS,
        "sources": [
            _browser("standby-card", "/standby?state=reconnecting",
                     CANVAS[0], CANVAS[1], 0, 0),
            *audio_sources("standby"),
        ],
    }


def _shard_hosts_available() -> bool:
    return urlparse(page_base()).hostname in _SHARDABLE_HOSTS


def _shard_url(url: str, index: int) -> str:
    """Rewrite `url`'s loopback host to the shard for `index` (KI-013)."""
    parsed = urlparse(url)
    if parsed.hostname not in _SHARDABLE_HOSTS:
        return url
    host = _SHARD_HOSTS[index % len(_SHARD_HOSTS)]
    port = f":{parsed.port}" if parsed.port else ""
    rest = url.split(parsed.netloc, 1)[1] if parsed.netloc in url else ""
    return f"{parsed.scheme}://{host}{port}{rest}"


def assign_connection_shards(scenes: list[dict]) -> list[dict]:
    """Give every browser source its own origin so each gets its own Chromium
    connection pool (KI-013).

    Assignment is by position across all scenes, so it's deterministic and
    collision-free — two sources sharing a shard would share the 6-connection
    budget, which is the bug. `test_every_browser_source_gets_its_own_origin`
    fails the moment there are more sources than shards.
    """
    index = 0
    for scene in scenes:
        for source in scene["sources"]:
            if source["kind"] != "browser_source":
                continue
            source["settings"]["url"] = _shard_url(source["settings"]["url"], index)
            index += 1
    return scenes


def scenes_spec() -> list[dict]:
    """All scenes the stream can be on. Order matters: the first entry is the
    default / home scene. Source order within a scene is z-order, bottom to top.

    The director switches between the first three on salience (Sprint 13);
    `standby` is last and deliberately outside its vocabulary — only the
    watchdog selects it, and only while the stream is genuinely down (B10)."""
    return assign_connection_shards(
        [_chart_focus(), _world_focus(), _event_focus(), _standby()]
    )


def scene_spec() -> dict:
    """Back-compat shim: single-scene callers (existing tests) get the default /
    home scene."""
    return scenes_spec()[0]
