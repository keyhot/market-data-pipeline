"""Code-controlled stream ops (Sprint 11): build the OBS scene, start/stop
the stream, report health. Sprint 13's director imports these functions —
the CLI is a thin wrapper, and every function takes the client as an
argument so tests (and the director) inject their own. Synchronous on
purpose: this is an ops tool, not a service.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import stream_scene  # noqa: E402

EXIT_OBS_UNREACHABLE = 2


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
    """Create or update the scene from SCENE_SPEC. Idempotent: existing
    inputs get settings refreshed, never duplicated."""
    spec = spec or stream_scene.scene_spec()
    created: list[str] = []
    scenes = {s["sceneName"] for s in client.get_scene_list().scenes}
    if spec["scene"] not in scenes:
        client.create_scene(spec["scene"])
        created.append(spec["scene"])
    existing = {i["inputName"] for i in client.get_input_list().inputs}
    for src in spec["sources"]:
        if src["name"] in existing:
            client.set_input_settings(src["name"], src["settings"], True)
        else:
            client.create_input(
                spec["scene"], src["name"], src["kind"], src["settings"], True
            )
            created.append(src["name"])
        item_id = client.get_scene_item_id(spec["scene"], src["name"]).scene_item_id
        client.set_scene_item_transform(
            spec["scene"],
            item_id,
            {"positionX": float(src["x"]), "positionY": float(src["y"])},
        )
    client.set_current_program_scene(spec["scene"])
    return {"scene": spec["scene"], "created": created}


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
        "dropped_ratio": (skipped / total) if total else 0.0,
    }


def screenshot(client, path, width: int = 1920, height: int = 1080) -> str:
    scene = client.get_current_program_scene().current_program_scene_name
    client.save_source_screenshot(scene, "png", str(path), width, height, -1)
    return str(path)


def configure_output(client) -> None:
    """Point OBS at the platform ingest. The key comes from the environment
    and never touches the repo."""
    key = os.environ.get("OBS_STREAM_KEY")
    if not key:
        raise ValueError("OBS_STREAM_KEY is not set (see .env.example)")
    server = os.environ.get("OBS_STREAM_SERVER", "rtmp://a.rtmp.youtube.com/live2")
    client.set_stream_service_settings("rtmp_custom", {"server": server, "key": key})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OBS stream control")
    parser.add_argument(
        "command",
        choices=["build", "start", "stop", "status", "screenshot", "configure-output"],
    )
    parser.add_argument(
        "--path", default="data/stream_screenshot.png", help="screenshot output path"
    )
    args = parser.parse_args(argv)
    try:
        client = make_client()
    except ObsUnreachable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OBS_UNREACHABLE
    if args.command == "build":
        print(json.dumps(build_scene(client)))
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
    except Exception:
        return {}


def _record(event_type: str, payload: dict) -> None:
    """World-event recording lives in the CLI layer so the importable
    functions stay side-effect free."""
    from world.stream_events import record_stream_event

    try:
        record_stream_event(event_type, payload)
    except Exception:  # recording must never block an ops command
        pass


if __name__ == "__main__":
    sys.exit(main())
