"""Code-controlled stream ops (Sprint 11): build the OBS scene, start/stop
the stream, report health. Sprint 13's director imports these functions —
the CLI is a thin wrapper, and every function takes the client as an
argument so tests (and the director) inject their own. Synchronous on
purpose: this is an ops tool, not a service.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import stream_scene  # noqa: E402

logger = logging.getLogger(__name__)

EXIT_OBS_UNREACHABLE = 2

# OBS_ALIGN_TOP (1) | OBS_ALIGN_LEFT (4). positionX/positionY name a different
# pixel under a different alignment, so pinning position without pinning this
# pins nothing.
ALIGN_TOP_LEFT = 5


class ObsUnreachable(RuntimeError):
    """OBS websocket not reachable — its own type so the watchdog can tell
    'OBS is down' from 'the stream is down'."""


def make_client(timeout: float = 5.0):
    import obsws_python as obs

    url = os.environ.get("OBS_WS_URL", "ws://127.0.0.1:4455")
    parsed = urlparse(url)
    try:
        return obs.ReqClient(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 4455,
            password=os.environ.get("OBS_WS_PASSWORD", ""),
            timeout=timeout,
        )
    except Exception as exc:
        raise ObsUnreachable(f"cannot reach OBS websocket at {url}: {exc}") from exc


def build_scene(client, spec: dict | None = None) -> dict:
    """Create or update scenes from the SCENE_SPEC. Idempotent: existing inputs
    get settings refreshed, never duplicated. With an explicit ``spec``, builds
    just that scene and rests on it (back-compat). With ``spec=None``, builds
    every scene in ``scenes_spec()`` and rests on the first (home) scene — the
    director needs all scenes present to switch between them."""
    if spec is not None:
        return _build_one(client, spec, set_current=True)
    specs = stream_scene.scenes_spec()
    created: list[str] = []
    for one in specs:
        created.extend(_build_one(client, one, set_current=False)["created"])
    home = specs[0]["scene"]
    # Every switch after this one is the director's, so the transition has to be
    # in place before it starts (B2): a hard cut between scenes reads as a
    # glitch on a stream whose whole register is calm-that-swells.
    set_transition(client)
    client.set_current_program_scene(home)
    return {"scene": home, "scenes": [s["scene"] for s in specs], "created": created}


def _build_one(client, spec: dict, set_current: bool) -> dict:
    """Build a single scene idempotently. Each scene owns uniquely-named inputs,
    so an input lives in exactly one scene and get_scene_item_id always resolves —
    except the music bed, which is marked `shared` and carried into every scene
    as a second scene item on ONE input (a per-scene bed restarts the music on
    every director switch; see `stream_scene.audio_sources`). For a shared
    input, "already exists" no longer implies "already in this scene"."""
    created: list[str] = []
    scenes = {s["sceneName"] for s in client.get_scene_list().scenes}
    if spec["scene"] not in scenes:
        client.create_scene(spec["scene"])
        created.append(spec["scene"])
    existing = {i["inputName"] for i in client.get_input_list().inputs}
    for layer, src in enumerate(spec["sources"]):
        if src["name"] in existing:
            client.set_input_settings(src["name"], src["settings"], True)
        else:
            client.create_input(
                spec["scene"], src["name"], src["kind"], src["settings"], True
            )
            created.append(src["name"])
        item_id = _scene_item_id(client, spec["scene"], src["name"], shared=src.get("shared", False))
        if src.get("shared") and "width" not in src["settings"]:
            # Audio-only: no geometry to set, and no rectangle to fight over.
            # It is still indexed, at the bottom, so it can never cover a page.
            client.set_scene_item_index(spec["scene"], item_id, layer)
            continue
        # Every property that decides the rendered rect, not just the corner
        # it starts at. `event-chart` sat at scale 1.509 in live OBS —
        # rendering 1449x815 against a spec that says 960x540, with 489px of it
        # lying across the events rail — and `build` could not repair it:
        # it moved the source back to (0,0) and left it 1.5x too big (KI-026).
        client.set_scene_item_transform(
            spec["scene"],
            item_id,
            {
                "positionX": float(src["x"]),
                "positionY": float(src["y"]),
                "scaleX": 1.0,
                "scaleY": 1.0,
                # A bounds fit silently overrides scale, so "renders at its own
                # declared size" has to be said, not assumed.
                "boundsType": "OBS_BOUNDS_NONE",
                "alignment": ALIGN_TOP_LEFT,
                "cropLeft": 0,
                "cropRight": 0,
                "cropTop": 0,
                "cropBottom": 0,
            },
        )
        # Stacking is declared by the spec (bottom-first), never inherited from
        # the order the inputs happened to be created in. It was inherited
        # until 2026-08-20, and that is how `charts-1m` — added to chart-focus
        # three weeks after the events rail — ended up drawn OVER the full
        # height of it: a source rendering perfectly and visible to nobody.
        client.set_scene_item_index(spec["scene"], item_id, layer)
    if set_current:
        client.set_current_program_scene(spec["scene"])
    return {"scene": spec["scene"], "created": created}


def _scene_item_id(client, scene: str, source: str, shared: bool) -> int:
    """The scene item for `source` in `scene`, adding it when a shared input
    exists but has never been placed here.

    Asks what is in the scene rather than catching the miss: obs-websocket logs
    a full traceback for every failed request, so a try/except here printed
    three stack traces on every clean build of a working stream.
    """
    if shared:
        present = {
            item["sourceName"] for item in client.get_scene_item_list(scene).scene_items
        }
        if source not in present:
            return client.create_scene_item(scene, source).scene_item_id
    return client.get_scene_item_id(scene, source).scene_item_id


def switch_scene(client, scene_name: str) -> None:
    """Set the active program scene. The director calls this to swell to a scene
    on salience and decay back to the home scene; the CLI wraps it."""
    client.set_current_program_scene(scene_name)


def current_scene(client) -> str | None:
    """What OBS is *actually* showing right now, or None if it won't say.

    OBS outlives a director restart, so the program scene is something to be
    read, not assumed (KI-034). Returns None rather than raising: a director
    that cannot read the scene should still start, on the old home-scene
    assumption, instead of refusing to run the show.
    """
    if client is None:
        return None
    try:
        return client.get_current_program_scene().current_program_scene_name
    except Exception:  # OBS answering badly must not stop the director starting
        return None


def start_stream(client) -> None:
    client.start_stream()


def stop_stream(client) -> None:
    client.stop_stream()


def get_status(client) -> dict:
    version = client.get_version()
    scene = client.get_current_program_scene()
    stream = client.get_stream_status()
    total = stream.output_total_frames or 0
    skipped = stream.output_skipped_frames or 0
    return {
        "obs_version": version.obs_version,
        "scene": scene.current_program_scene_name,
        "streaming": bool(stream.output_active),
        "timecode": stream.output_timecode,
        "skipped_frames": skipped,
        "total_frames": total,
        # KI-021: OBS re-dials a dropped RTMP ingest on its own, keeping
        # output_active true the whole time. This flag and the frame counters
        # (which reset per output session) are the only way anything outside
        # OBS can know it happened.
        "reconnecting": bool(stream.output_reconnecting),
        # Note what dropped_ratio actually measures: output_skipped_frames is
        # the ENCODER falling behind, not bandwidth loss. OBS's own log counts
        # "dropped frames due to insufficient bandwidth" separately and
        # obs-websocket does not expose it; output_congestion is what OBS
        # derives from those drops, so it is the network signal here (KI-032).
        "congestion": float(stream.output_congestion or 0.0),
        "dropped_ratio": (skipped / total) if total else 0.0,
    }


def screenshot(client, path, width: int = 1920, height: int = 1080) -> str:
    scene = client.get_current_program_scene().current_program_scene_name
    client.save_source_screenshot(scene, "png", str(path), width, height, -1)
    return str(path)


def set_transition(client, name: str = "Fade", duration_ms: int = 320) -> bool:
    """Make the director's scene switches cross-fade instead of cutting.

    A hard cut reads as a glitch on a stream whose entire register is
    calm-that-swells. Best-effort by design: an OBS profile can be built without
    the stock transitions, and a missing nicety must never take the stream down
    — the cut still works.
    """
    listed = client.get_scene_transition_list().transitions
    available = {entry["transitionName"] for entry in listed}
    if name not in available:
        print(
            f"transition {name!r} not in this profile; leaving the cut as-is",
            file=sys.stderr,
        )
        return False
    client.set_current_scene_transition(name)
    client.set_current_scene_transition_duration(duration_ms)
    return True


def set_audio_gain(client, tier: int, sources=None) -> None:
    """Apply the tier's audio-bed gain (B2's music-swell hook).

    Silent no-op when there is no bed: the VLC source only exists when
    STREAM_AUDIO_DIR is set, and addressing an input that isn't there must not
    raise at 3am on a live stream.
    """
    names = stream_scene.audio_source_names() if sources is None else list(sources)
    gain = stream_scene.audio_gain_db(tier)
    for name in names:
        client.set_input_volume(name, vol_db=gain)


def configure_output(client) -> None:
    """Point OBS at the platform ingest and pin the encoder bitrate. The key
    comes from the environment and never touches the repo."""
    key = os.environ.get("OBS_STREAM_KEY")
    if not key:
        raise ValueError("OBS_STREAM_KEY is not set (see .env.example)")
    # Pin the Simple-mode encoder bitrate (KI-009). Without this OBS inherits the
    # profile default — it was 6000 and YouTube rejected the stream. 2200 kbps is
    # a safe ceiling for the free 720p/900p tiers; override via OBS_STREAM_BITRATE.
    # Advanced output mode uses a different parameter path (the runbook pins
    # Simple mode); best-effort here rather than probing the mode.
    bitrate = os.environ.get("OBS_STREAM_BITRATE", "2200")
    client.set_profile_parameter("SimpleOutput", "VBitrate", str(bitrate))
    server = os.environ.get("OBS_STREAM_SERVER", "rtmp://a.rtmp.youtube.com/live2")
    client.set_stream_service_settings("rtmp_custom", {"server": server, "key": key})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OBS stream control")
    parser.add_argument(
        "command",
        choices=[
            "build",
            "switch",
            "start",
            "stop",
            "status",
            "screenshot",
            "configure-output",
        ],
    )
    parser.add_argument(
        "--path", default="data/stream_screenshot.png", help="screenshot output path"
    )
    parser.add_argument("--scene", help="scene name for the switch command")
    args = parser.parse_args(argv)
    try:
        client = make_client()
    except ObsUnreachable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OBS_UNREACHABLE
    if args.command == "build":
        print(json.dumps(build_scene(client)))
    elif args.command == "switch":
        if not args.scene:
            print("error: switch requires --scene", file=sys.stderr)
            return 2
        switch_scene(client, args.scene)
        print(f"switched to {args.scene}")
    elif args.command == "start":
        start_stream(client)
        _record("stream_started", {"via": "stream_ctl"})
    elif args.command == "stop":
        _record("stream_stopped", {"via": "stream_ctl", **_uptime_payload(client)})
        stop_stream(client)
    elif args.command == "status":
        print(json.dumps(get_status(client), indent=2))
    elif args.command == "screenshot":
        print(screenshot(client, args.path))
    elif args.command == "configure-output":
        configure_output(client)
        print("stream service configured")
    return 0


def _uptime_payload(client) -> dict:
    try:
        return {"timecode": get_status(client)["timecode"]}
    except Exception as e:
        logger.warning(
            "Uptime payload unavailable", extra={"error": f"{type(e).__name__}: {e}"}
        )
        return {}


def _record(event_type: str, payload: dict) -> None:
    """World-event recording lives in the CLI layer so the importable
    functions stay side-effect free."""
    from world.stream_events import record_stream_event

    try:
        record_stream_event(event_type, payload)
    except Exception as e:
        # Recording must never block an ops command — but `stream_*` events
        # ARE the uptime number, and `record_stream_event` already spools to
        # JSONL when Postgres is down. So anything reaching here is a bug, and
        # a silently dropped `stream_stopped` makes soak_report overstate
        # uptime: the KI-014 / KI-021 family, one layer down. ERROR, loudly.
        logger.error(
            "Stream event LOST — uptime evidence is now incomplete",
            extra={"event_type": event_type, "error": f"{type(e).__name__}: {e}"},
            exc_info=True,
        )


if __name__ == "__main__":
    sys.exit(main())
