#!/usr/bin/env python3
"""Visual QA harness — the eval gate for everything aesthetics-shaped.

Aesthetics is the one thing no assertion grades, so B2/B3/B8 all need the same
thing and kept improvising it: a repeatable, *labelled* set of frames of the
real renderer, calm and mid-swell. This is that, made once.

Two decisions worth knowing:

**It photographs real events, never staged ones.** The plan floated injecting a
synthetic high-tier world event to force a swell. `world_events` is append-only
and its whole value is that it is true, so a fake row for a screenshot is not a
trade this project makes. `swell` sits on the live SSE feed and waits for a real
event at or above a tier instead — which is how the B1 animations and the B7
glance were actually caught on air.

**It gives the wheel back.** The director owns the scene while the stream is
healthy (B10), so `calm` restores whatever scene it found, and switching scenes
on a *live* stream is opt-in (`--take-control`) rather than a default: it is
visible to every viewer.

Usage:

    python scripts/capture_scenes.py calm                    # OBS idle
    python scripts/capture_scenes.py calm --take-control     # OBS live, on air
    python scripts/capture_scenes.py swell --min-tier 2      # wait for a real one
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.stream_ctl import ObsUnreachable, make_client  # noqa: E402
from scripts.stream_scene import scenes_spec  # noqa: E402
from world.state import severity_tier  # noqa: E402

EXIT_OBS_UNREACHABLE = 2
EXIT_NOTHING_CAPTURED = 3

DEFAULT_OUT = Path("data/visual-qa")
SWELL_FRAME_DELAY = 0.8      # spaced across a ~0.9-1.4s animation and a 2.8s glance
SETTLE_SECONDS = 1.5         # a just-activated browser source has to catch up

# Scenes that would tell a viewer something untrue if shown while the stream is
# fine. `standby` says "reconnecting…" — putting it on air during a healthy
# stream to take a screenshot is a small lie, and this project doesn't tell
# those about its own state. Capture it against an idle OBS instead.
MISLEADING_WHILE_LIVE = frozenset({"standby"})


class WouldDisturbTheStream(RuntimeError):
    """Refusing to switch scenes on a live stream without being told to."""


class SceneChangedUnderCapture(RuntimeError):
    """The program scene moved between the switch and the shutter (KI-030).

    `SETTLE_SECONDS` is also a window in which the director can switch on its
    own tick — which is guaranteed to be true in the documented `--take-control`
    usage, on a live stream. A frame named after one scene and showing another
    is worse than a missing frame, because it looks correct.
    """


def default_scenes() -> list[str]:
    """From the scene spec, never a second list here: a hard-coded set would
    silently skip a scene added later, and `standby` was exactly such a late
    addition (B10)."""
    return [scene["scene"] for scene in scenes_spec()]


def plan(scenes, current: str | None) -> list[str]:
    """The visiting order, ending back where we started.

    Leaving the stream parked on whatever was captured last would be a
    self-inflicted outage of the same family as KI-018 — the tool that is
    supposed to observe the system taking it over instead.
    """
    order = list(scenes)
    if current is not None:
        order.append(current)
    return order


def honest_scenes(scenes, streaming: bool) -> list[str]:
    """Drop scenes that would misrepresent the stream's own state, on air."""
    if not streaming:
        return list(scenes)
    return [scene for scene in scenes if scene not in MISLEADING_WHILE_LIVE]


def guard_live(streaming: bool, take_control: bool) -> None:
    if streaming and not take_control:
        raise WouldDisturbTheStream(
            "OBS is streaming: scene switching is visible on air. "
            "Re-run with --take-control if that is what you want."
        )


def tier_of(event: dict) -> int:
    """The one tier scale (`world.state.severity_tier`). A copy here would make
    the harness disagree with the room about whether a swell happened — which is
    precisely KI-019, one layer out."""
    return severity_tier(event.get("event_type", ""), event.get("severity", 0.0))


def _shoot(client, path: Path, expected: str | None = None) -> Path:
    """Photograph the program scene, refusing to file it under the wrong name.

    The scene is re-read here rather than trusted from the caller because an
    inactive scene's sources are frozen — but re-reading it is also what lets
    intent and reality be compared instead of merely diverging."""
    scene = client.get_current_program_scene().current_program_scene_name
    if expected is not None and scene != expected:
        raise SceneChangedUnderCapture(
            f"program scene is {scene!r}, not the {expected!r} this frame "
            f"would have been named after"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    client.save_source_screenshot(scene, "png", str(path), 1920, 1080, -1)
    return path


def _is_streaming(client) -> bool:
    return bool(client.get_stream_status().output_active)


def capture_calm(
    client,
    scenes,
    out: Path,
    take_control: bool = False,
    sleeper=time.sleep,
) -> list[Path]:
    """One labelled frame per scene, then the scene put back."""
    guard_live(_is_streaming(client), take_control)
    out = Path(out)
    current = client.get_current_program_scene().current_program_scene_name
    wanted, shot = set(scenes), set()
    written: list[Path] = []
    for scene in plan(scenes, current):
        client.set_current_program_scene(scene)
        if scene not in wanted or scene in shot:
            continue                              # the restore leg, or a repeat
        # OBS only ticks sources in the ACTIVE scene, so everything on a scene
        # we just switched to has been frozen — possibly since boot. Shooting
        # immediately captures that stale frame; this is the same fact that
        # made per-source screenshots of inactive scenes come back blank white.
        sleeper(SETTLE_SECONDS)
        shot.add(scene)
        written.append(_shoot_settled(client, out / f"calm-{scene}.png", scene, sleeper))
    return written


def _shoot_settled(client, path: Path, scene: str, sleeper) -> Path:
    """One shot, one retry, then fail loudly (KI-030).

    Losing the scene once is ordinary — the director is running and it is
    allowed to. Taking it back and re-settling is the honest response. Losing
    it twice means something else owns the wheel, and the harness that grades
    everything else is the last thing that may report something untrue about
    itself."""
    try:
        return _shoot(client, path, expected=scene)
    except SceneChangedUnderCapture:
        client.set_current_program_scene(scene)
        sleeper(SETTLE_SECONDS)
    return _shoot(client, path, expected=scene)


def capture_swell(
    client,
    events,
    out: Path,
    min_tier: int = 1,
    shots: int = 3,
    sleeper=time.sleep,
) -> list[Path]:
    """Burst-capture the first real event at or above `min_tier`.

    A single frame cannot show a reaction: the animation returns to rest inside
    ~1.4s and the observer glance runs ~3s, so the sequence is the evidence.
    """
    out = Path(out)
    for event in events:
        if tier_of(event) < min_tier:
            continue
        name = event.get("event_type", "event")
        written = []
        for index in range(shots):
            sleeper(SWELL_FRAME_DELAY)
            written.append(_shoot(client, out / f"swell-{name}-{index}.png"))
        return written
    return []


def iter_world_events(url: str, timeout: float):
    """The live SSE feed, as plain dicts. Impure on purpose and injected, so
    every test above runs on a list."""
    import httpx

    deadline = time.time() + timeout
    with httpx.stream("GET", url, timeout=None) as response:
        for line in response.iter_lines():
            if time.time() > deadline:
                return
            if not line.startswith("data:"):
                continue
            try:
                yield json.loads(line[5:].strip())
            except ValueError:
                continue


def write_index(out: Path, written: list[Path], mode: str) -> Path:
    """A labelled contact sheet in markdown — the captures are for a human to
    look at and for the sprint note to link, so they arrive named."""
    index = Path(out) / "index.md"
    lines = [f"# Visual QA — {mode}", ""]
    lines += [f"- `{path.name}`" for path in written]
    index.write_text("\n".join(lines) + "\n")
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visual QA capture harness")
    parser.add_argument("mode", choices=["calm", "swell"])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--scenes", help="comma-separated scene names")
    parser.add_argument(
        "--take-control",
        action="store_true",
        help="allow scene switching while OBS is streaming (visible on air)",
    )
    parser.add_argument("--min-tier", type=int, default=1)
    parser.add_argument("--shots", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)

    try:
        client = make_client()
    except ObsUnreachable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OBS_UNREACHABLE

    out = Path(args.out)
    if args.mode == "calm":
        scenes = args.scenes.split(",") if args.scenes else default_scenes()
        kept = honest_scenes(scenes, _is_streaming(client))
        for dropped in [s for s in scenes if s not in kept]:
            print(f"skipping {dropped}: it would misrepresent a healthy stream")
        scenes = kept
        try:
            written = capture_calm(client, scenes, out, args.take_control)
        except WouldDisturbTheStream as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        base = os.environ.get("WORLD_API_URL", "http://localhost:8000")
        print(f"waiting up to {args.timeout:.0f}s for a tier-{args.min_tier}+ event…")
        written = capture_swell(
            client,
            iter_world_events(f"{base}/stream/world/events", args.timeout),
            out,
            args.min_tier,
            args.shots,
        )

    if not written:
        print("nothing captured", file=sys.stderr)
        return EXIT_NOTHING_CAPTURED
    print(write_index(out, written, args.mode))
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
