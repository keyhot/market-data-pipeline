# Sprint 11 — Stream MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One boring, reliable OBS scene streaming the existing chart + overlay pages 24/7, code-controlled end to end, with the world remembering its own outages.

**Architecture:** Everything is a browser source pointed at the already-running FastAPI pages. A `SCENE_SPEC` module owns layout constants; `stream_ctl.py` (obsws-python) builds the scene idempotently and starts/stops the stream; a watchdog with a pure-function state machine detects OBS/stream death and recovers with backoff; lifecycle transitions become append-only `world_events` rows (with a JSONL spool for when Postgres itself is the outage).

**Tech Stack:** obsws-python (OBS websocket v5), existing FastAPI pages, Postgres `world_events`, pytest with fake obsws clients (no OBS in CI).

## Global Constraints

- Python 3.11, poetry-managed; new runtime dep: `obsws-python`.
- `world_events` is append-only: NEVER update/delete rows (docs/world-memory.md).
- Tests: no network, no OBS, no Postgres — mock at the obsws-client / postgres_store boundary (existing conftest discipline; `POSTGRES_WRITE_ENABLED=0` is forced by `tests/conftest.py`).
- Commit style: `Type: short description` — NEVER add `Co-Authored-By` lines.
- Secrets (`OBS_WS_PASSWORD`, `OBS_STREAM_KEY`) live in `.env` (gitignored); `.env.example` gets placeholder keys only.
- ruff clean (`ruff check .`), line length 88.
- After doc-visible changes: `graphify update .`.
- User-action steps (cannot be automated, surface at the end): enabling the OBS websocket server if disabled, platform choice + stream key, audio file downloads, the actual 24h soak window.

---

### Task 1: obsws-python dependency + env plumbing

**Files:**
- Modify: `pyproject.toml` (dependencies)
- Modify: `.env.example`

**Interfaces:**
- Produces: importable `obsws_python`; env contract `OBS_WS_URL`, `OBS_WS_PASSWORD`, `OBS_STREAM_SERVER`, `OBS_STREAM_KEY`, `STREAM_AUDIO_DIR`, `STREAM_EVENT_SPOOL`, `STREAM_PAGE_BASE` used by all later tasks.

- [ ] **Step 1: Add the dependency**

Run: `poetry add obsws-python`
Expected: resolves and installs; `pyproject.toml` gains `"obsws-python (>=1.8,<2.0)"` (version as resolved).

- [ ] **Step 2: Verify import**

Run: `poetry run python -c "import obsws_python; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Extend `.env.example`** (append):

```bash
# OBS websocket control (Sprint 11 stream tooling: scripts/stream_ctl.py)
OBS_WS_URL=ws://127.0.0.1:4455
OBS_WS_PASSWORD=changeme
# Base URL the OBS browser sources load pages from
STREAM_PAGE_BASE=http://localhost:8000
# RTMP output — the key comes from the platform and is NEVER committed
OBS_STREAM_SERVER=rtmp://a.rtmp.youtube.com/live2
OBS_STREAM_KEY=
# Directory of DMCA-safe audio loops wired into the scene (optional)
STREAM_AUDIO_DIR=
# Stream lifecycle events spool here when Postgres is unreachable
STREAM_EVENT_SPOOL=data/stream_events.spool.jsonl
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml poetry.lock .env.example
git commit -m "Ops: add obsws-python + stream env contract"
```

---

### Task 2: `scripts/stream_scene.py` — SCENE_SPEC as code

**Files:**
- Create: `scripts/stream_scene.py`
- Test: `tests/unit/test_stream_scene.py`

**Interfaces:**
- Produces: `scene_spec() -> dict` with keys `scene` (str), `canvas` (tuple), `sources` (list of `{"name","kind","settings","x","y"}`); constants `SCENE_NAME`, `CANVAS`. Source order in the list = z-order bottom→top. `settings` carries obsws `inputSettings` including `width`/`height` for browser sources.

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_stream_scene.py`:

```python
"""SCENE_SPEC shape: unique names, geometry inside the canvas, URLs that
resolve to real routes in api.main — the layout is code, so it gets tests."""

from urllib.parse import urlparse

from scripts import stream_scene


def _spec():
    return stream_scene.scene_spec()


def test_source_names_unique():
    names = [s["name"] for s in _spec()["sources"]]
    assert len(names) == len(set(names))


def test_geometry_within_canvas():
    spec = _spec()
    cw, ch = spec["canvas"]
    for src in spec["sources"]:
        settings = src["settings"]
        if "width" not in settings:  # audio sources have no geometry
            continue
        assert 0 <= src["x"] and src["x"] + settings["width"] <= cw, src["name"]
        assert 0 <= src["y"] and src["y"] + settings["height"] <= ch, src["name"]


def test_urls_hit_existing_routes():
    from api.main import app

    for src in _spec()["sources"]:
        url = src["settings"].get("url")
        if url is None:
            continue
        path = urlparse(url).path
        matched = any(
            getattr(route, "path_regex", None) is not None
            and route.path_regex.match(path)
            for route in app.routes
        )
        assert matched, f"{src['name']} points at unknown route {path}"


def test_audio_source_appears_only_with_dir(monkeypatch):
    monkeypatch.delenv("STREAM_AUDIO_DIR", raising=False)
    assert all(s["kind"] != "vlc_source" for s in _spec()["sources"])
    monkeypatch.setenv("STREAM_AUDIO_DIR", "/tmp/audio")
    audio = [s for s in _spec()["sources"] if s["kind"] == "vlc_source"]
    assert len(audio) == 1 and audio[0]["name"] == "audio-bed"


def test_browser_sources_keep_sse_alive():
    for src in _spec()["sources"]:
        if src["kind"] == "browser_source":
            assert src["settings"]["shutdown"] is False  # SSE must stay alive
            assert src["settings"]["fps"] == 30
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/unit/test_stream_scene.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.stream_scene` (add `scripts/__init__.py` if missing).

- [ ] **Step 3: Implement `scripts/stream_scene.py`:**

```python
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
                "width": 1920, "height": 840, **_BROWSER_DEFAULTS,
            },
            "x": 0, "y": 0,
        },
        {
            "name": "overlay-signals",
            "kind": "browser_source",
            "settings": {
                "url": f"{base}/overlay/signals",
                "width": 1920, "height": 120, **_BROWSER_DEFAULTS,
            },
            "x": 0, "y": 960,
        },
        {
            "name": "overlay-events",
            "kind": "browser_source",
            "settings": {
                "url": f"{base}/overlay/events",
                "width": 480, "height": 840, **_BROWSER_DEFAULTS,
            },
            "x": 1440, "y": 0,
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
            "x": 0, "y": 0,
        }
    ]


def scene_spec() -> dict:
    """Full scene description; source order is z-order, bottom to top."""
    return {
        "scene": SCENE_NAME,
        "canvas": CANVAS,
        "sources": browser_sources() + audio_sources(),
    }
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_stream_scene.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/stream_scene.py scripts/__init__.py tests/unit/test_stream_scene.py
git commit -m "Stream: SCENE_SPEC as code — layout constants for scene v1"
```

---

### Task 3: `scripts/stream_ctl.py` — build / start / stop / status / screenshot / configure-output

**Files:**
- Create: `scripts/stream_ctl.py`
- Test: `tests/unit/test_stream_ctl.py`

**Interfaces:**
- Consumes: `scripts.stream_scene.scene_spec()`.
- Produces (Sprint 13's director imports these — plain functions, client injected):
  - `make_client() -> obsws_python.ReqClient` (raises `ObsUnreachable`)
  - `build_scene(client, spec=None) -> dict` (`{"scene", "created": [names]}`, idempotent)
  - `start_stream(client) -> None`, `stop_stream(client) -> None`
  - `get_status(client) -> dict` (`obs_version, scene, streaming, timecode, skipped_frames, total_frames, dropped_ratio`)
  - `screenshot(client, path, width=1920, height=1080) -> path`
  - `configure_output(client) -> None` (from `OBS_STREAM_SERVER`/`OBS_STREAM_KEY`)
  - `ObsUnreachable(RuntimeError)`; CLI exit code 2 for unreachable OBS.

- [ ] **Step 1: Write failing tests** — `tests/unit/test_stream_ctl.py`:

```python
"""stream_ctl logic against a fake obsws client — CI has no OBS, so every
request the tool would issue is pinned here instead."""

import pytest

from scripts import stream_ctl


class FakeResp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    def __init__(self, scenes=(), inputs=(), streaming=False,
                 skipped=0, total=0):
        self.calls = []
        self._scenes = list(scenes)
        self._inputs = list(inputs)
        self._streaming = streaming
        self._skipped, self._total = skipped, total

    def _log(self, name, *args):
        self.calls.append((name, *args))

    def get_scene_list(self):
        return FakeResp(scenes=[{"sceneName": s} for s in self._scenes])

    def create_scene(self, name):
        self._log("create_scene", name)
        self._scenes.append(name)

    def get_input_list(self):
        return FakeResp(inputs=[{"inputName": n} for n in self._inputs])

    def create_input(self, scene, name, kind, settings, enabled):
        self._log("create_input", name)
        self._inputs.append(name)

    def set_input_settings(self, name, settings, overlay):
        self._log("set_input_settings", name)

    def get_scene_item_id(self, scene, name):
        return FakeResp(scene_item_id=7)

    def set_scene_item_transform(self, scene, item_id, transform):
        self._log("set_scene_item_transform", scene, item_id)

    def set_current_program_scene(self, name):
        self._log("set_current_program_scene", name)

    def start_stream(self):
        self._log("start_stream")

    def stop_stream(self):
        self._log("stop_stream")

    def get_version(self):
        return FakeResp(obs_version="32.1.2")

    def get_current_program_scene(self):
        return FakeResp(current_program_scene_name="market-world-v1")

    def get_stream_status(self):
        return FakeResp(
            output_active=self._streaming,
            output_timecode="00:10:00",
            output_skipped_frames=self._skipped,
            output_total_frames=self._total,
        )

    def save_source_screenshot(self, name, fmt, path, width, height, quality):
        self._log("save_source_screenshot", name, fmt, path)

    def set_stream_service_settings(self, ss_type, ss_settings):
        self._log("set_stream_service_settings", ss_type, ss_settings)


def _creates(client):
    return [c for c in client.calls if c[0] in ("create_scene", "create_input")]


def test_build_creates_scene_and_sources():
    client = FakeClient()
    result = stream_ctl.build_scene(client)
    assert result["scene"] == "market-world-v1"
    created = {c[1] for c in _creates(client)}
    assert {"market-world-v1", "chart-btcusdt-1m",
            "overlay-signals", "overlay-events"} <= created


def test_build_is_idempotent():
    client = FakeClient()
    stream_ctl.build_scene(client)
    client.calls.clear()
    stream_ctl.build_scene(client)
    assert _creates(client) == []  # second run must not create anything
    assert any(c[0] == "set_input_settings" for c in client.calls)


def test_status_parses_dropped_ratio():
    client = FakeClient(streaming=True, skipped=5, total=100)
    status = stream_ctl.get_status(client)
    assert status["streaming"] is True
    assert status["dropped_ratio"] == pytest.approx(0.05)
    assert status["obs_version"] == "32.1.2"


def test_status_zero_frames_no_division_error():
    status = stream_ctl.get_status(FakeClient())
    assert status["dropped_ratio"] == 0.0


def test_start_stop_sequences():
    client = FakeClient()
    stream_ctl.start_stream(client)
    stream_ctl.stop_stream(client)
    assert [c[0] for c in client.calls] == ["start_stream", "stop_stream"]


def test_configure_output_requires_key(monkeypatch):
    monkeypatch.delenv("OBS_STREAM_KEY", raising=False)
    with pytest.raises(ValueError):
        stream_ctl.configure_output(FakeClient())


def test_configure_output_sets_service(monkeypatch):
    monkeypatch.setenv("OBS_STREAM_KEY", "sekrit")
    monkeypatch.setenv("OBS_STREAM_SERVER", "rtmp://example/live")
    client = FakeClient()
    stream_ctl.configure_output(client)
    call = client.calls[-1]
    assert call[0] == "set_stream_service_settings"
    assert call[2] == {"server": "rtmp://example/live", "key": "sekrit"}


def test_cli_exit_code_when_obs_unreachable(monkeypatch):
    def boom():
        raise stream_ctl.ObsUnreachable("no OBS")

    monkeypatch.setattr(stream_ctl, "make_client", boom)
    assert stream_ctl.main(["status"]) == 2
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/unit/test_stream_ctl.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `scripts/stream_ctl.py`:**

```python
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
from urllib.parse import urlparse

from scripts import stream_scene

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
            spec["scene"], item_id,
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


def screenshot(client, path: str, width: int = 1920, height: int = 1080) -> str:
    scene = client.get_current_program_scene().current_program_scene_name
    client.save_source_screenshot(scene, "png", str(path), width, height, -1)
    return str(path)


def configure_output(client) -> None:
    """Point OBS at the platform ingest. The key comes from the environment
    and never touches the repo."""
    key = os.environ.get("OBS_STREAM_KEY")
    if not key:
        raise ValueError("OBS_STREAM_KEY is not set (see .env.example)")
    server = os.environ.get(
        "OBS_STREAM_SERVER", "rtmp://a.rtmp.youtube.com/live2"
    )
    client.set_stream_service_settings(
        "rtmp_custom", {"server": server, "key": key}
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OBS stream control")
    parser.add_argument(
        "command",
        choices=["build", "start", "stop", "status", "screenshot",
                 "configure-output"],
    )
    parser.add_argument("--path", default="data/stream_screenshot.png",
                        help="screenshot output path")
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
        _record("stream_stopped", {"via": "stream_ctl",
                                   **_uptime_payload(client)})
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
```

Note: `world.stream_events` arrives in Task 4 — the import is lazy and wrapped, so Task 3's tests pass without it (CLI start/stop paths aren't unit-tested until Task 4 exists; `main(["status"])` is).

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_stream_ctl.py tests/unit/test_stream_scene.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/stream_ctl.py tests/unit/test_stream_ctl.py
git commit -m "Stream: stream_ctl — obsws scene build, start/stop, status, screenshot"
```

---

### Task 4: Stream lifecycle world events + spool + overlay headlines

**Files:**
- Create: `world/stream_events.py`
- Modify: `world/salience.py` (KNOWN_EVENT_TYPES)
- Modify: `api/templates/overlay_events.html` (HEADLINES map)
- Test: `tests/unit/test_stream_events.py`

**Interfaces:**
- Consumes: `storage.postgres_store.append_world_events`, `latest_world_event_time`.
- Produces: `record_stream_event(event_type, payload=None, spool_path=None) -> dict | None` (None = cooldown-suppressed), `build_stream_event(event_type, payload=None, occurred_at=None) -> dict`, `flush_spool(spool_path) -> int`, `SEVERITIES`, `STREAM_EVENT_TYPES`. Events carry no `symbol` key (NULL symbol in the table).

- [ ] **Step 1: Write failing tests** — `tests/unit/test_stream_events.py`:

```python
"""Stream lifecycle events: severity mapping, dropped-event cooldown, and the
JSONL spool that keeps history through the exact failures it records."""

from datetime import datetime, timedelta, timezone

import pytest

from world import stream_events
from world.salience import KNOWN_EVENT_TYPES


def test_stream_types_registered_in_known_event_types():
    assert stream_events.STREAM_EVENT_TYPES <= KNOWN_EVENT_TYPES


def test_severity_semantics():
    assert stream_events.SEVERITIES == {
        "stream_started": 1.0,
        "stream_stopped": 2.0,
        "stream_dropped": 5.0,
    }


def test_build_event_shape():
    event = stream_events.build_stream_event("stream_dropped", {"reason": "x"})
    assert event["event_type"] == "stream_dropped"
    assert event["severity"] == 5.0
    assert event["payload"] == {"reason": "x"}
    assert "symbol" not in event  # stream events are symbol-less
    assert event["occurred_at"].tzinfo is not None


def test_build_rejects_unknown_type():
    with pytest.raises(ValueError):
        stream_events.build_stream_event("stream_exploded")


def test_record_appends(monkeypatch, tmp_path):
    written = []
    monkeypatch.setattr(stream_events, "append_world_events",
                        lambda evs: written.extend(evs))
    monkeypatch.setattr(stream_events, "latest_world_event_time",
                        lambda *a: None)
    event = stream_events.record_stream_event(
        "stream_started", spool_path=tmp_path / "spool.jsonl")
    assert written and written[0] is not None and event is not None


def test_dropped_cooldown_suppresses_flapping(monkeypatch, tmp_path):
    written = []
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(stream_events, "append_world_events",
                        lambda evs: written.extend(evs))
    monkeypatch.setattr(stream_events, "latest_world_event_time",
                        lambda *a: now - timedelta(minutes=1))
    result = stream_events.record_stream_event(
        "stream_dropped", spool_path=tmp_path / "spool.jsonl")
    assert result is None and written == []


def test_started_never_cooldown_suppressed(monkeypatch, tmp_path):
    written = []
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(stream_events, "append_world_events",
                        lambda evs: written.extend(evs))
    monkeypatch.setattr(stream_events, "latest_world_event_time",
                        lambda *a: now - timedelta(seconds=10))
    result = stream_events.record_stream_event(
        "stream_started", spool_path=tmp_path / "spool.jsonl")
    assert result is not None and len(written) == 1


def test_spool_on_db_failure_then_flush(monkeypatch, tmp_path):
    spool = tmp_path / "spool.jsonl"

    def broken(evs):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(stream_events, "append_world_events", broken)
    monkeypatch.setattr(stream_events, "latest_world_event_time",
                        lambda *a: None)
    event = stream_events.record_stream_event(
        "stream_dropped", {"reason": "obs_unreachable"}, spool_path=spool)
    assert event is not None
    assert spool.exists() and len(spool.read_text().strip().splitlines()) == 1

    written = []
    monkeypatch.setattr(stream_events, "append_world_events",
                        lambda evs: written.extend(evs))
    flushed = stream_events.flush_spool(spool)
    assert flushed == 1
    assert written[0]["event_type"] == "stream_dropped"
    assert written[0]["occurred_at"].tzinfo is not None
    assert not spool.exists()  # flushed spool is gone


def test_record_flushes_spool_first(monkeypatch, tmp_path):
    spool = tmp_path / "spool.jsonl"
    spool.write_text(
        '{"occurred_at": "2026-07-20T00:00:00+00:00", "event_type": '
        '"stream_dropped", "severity": 5.0, "payload": {}}\n'
    )
    written = []
    monkeypatch.setattr(stream_events, "append_world_events",
                        lambda evs: written.extend(evs))
    monkeypatch.setattr(stream_events, "latest_world_event_time",
                        lambda *a: None)
    stream_events.record_stream_event("stream_started", spool_path=spool)
    assert len(written) == 2  # spooled event + fresh event
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/unit/test_stream_events.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `world/stream_events.py`:**

```python
"""Stream lifecycle world events (Sprint 11): the world remembers its own
outages the same way it remembers market moves. Append-only like all world
events; a local JSONL spool keeps history through the exact failures it
records (Postgres unreachable while the stream is dying).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage.postgres_store import append_world_events, latest_world_event_time

logger = logging.getLogger(__name__)

# started 1.0 (routine), stopped 2.0 (intentional stops are notable),
# dropped 5.0 (an outage is high-salience — the world should visibly react).
SEVERITIES = {
    "stream_started": 1.0,
    "stream_stopped": 2.0,
    "stream_dropped": 5.0,
}
STREAM_EVENT_TYPES = frozenset(SEVERITIES)

# Only dropped events get a cooldown: a flapping stream shouldn't spam the
# world's memory, but every start/stop is real history.
DROPPED_COOLDOWN = timedelta(minutes=5)


def _default_spool() -> Path:
    return Path(os.environ.get("STREAM_EVENT_SPOOL",
                               "data/stream_events.spool.jsonl"))


def build_stream_event(
    event_type: str, payload: dict | None = None,
    occurred_at: datetime | None = None,
) -> dict:
    if event_type not in SEVERITIES:
        raise ValueError(f"unknown stream event type: {event_type}")
    return {
        "occurred_at": occurred_at or datetime.now(timezone.utc),
        "event_type": event_type,
        "severity": SEVERITIES[event_type],
        "payload": payload or {},
    }


def record_stream_event(
    event_type: str, payload: dict | None = None,
    spool_path: Path | None = None,
) -> dict | None:
    """Build, cooldown-filter, and append one lifecycle event. Returns the
    event, or None when cooldown-suppressed. On DB failure the event goes to
    the spool instead of being lost."""
    spool_path = spool_path or _default_spool()
    event = build_stream_event(event_type, payload)
    if event_type == "stream_dropped":
        last = _safe_latest(event_type)
        if last is not None and event["occurred_at"] - last < DROPPED_COOLDOWN:
            return None
    try:
        flush_spool(spool_path)
        append_world_events([event])
    except Exception:
        _spool(event, spool_path)
        logger.warning(
            "Postgres unreachable — stream event spooled",
            extra={"event_type": event_type, "spool": str(spool_path)},
        )
    return event


def flush_spool(spool_path: Path | None = None) -> int:
    """Replay spooled events into world_events; removes the spool on success."""
    spool_path = spool_path or _default_spool()
    if not spool_path.exists():
        return 0
    events = []
    for line in spool_path.read_text().strip().splitlines():
        raw = json.loads(line)
        raw["occurred_at"] = datetime.fromisoformat(raw["occurred_at"])
        events.append(raw)
    if events:
        append_world_events(events)
        logger.info("Stream event spool flushed", extra={"count": len(events)})
    spool_path.unlink()
    return len(events)


def _spool(event: dict, spool_path: Path) -> None:
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    row = {**event, "occurred_at": event["occurred_at"].isoformat()}
    with spool_path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")


def _safe_latest(event_type: str) -> datetime | None:
    try:
        return latest_world_event_time(event_type, None)
    except Exception:
        return None  # DB down — don't let the cooldown check kill recording
```

- [ ] **Step 4: Register the types in `world/salience.py`** — extend `KNOWN_EVENT_TYPES`:

```python
KNOWN_EVENT_TYPES = frozenset(
    {
        "big_move",
        "volatility_spike",
        "gap_open",
        "volume_anomaly",
        "streak",
        "signal_resolved",
        "model_losing_streak",
        # stream lifecycle (Sprint 11) — severities in world/stream_events.py
        "stream_started",
        "stream_stopped",
        "stream_dropped",
    }
)
```

- [ ] **Step 5: Add overlay headlines** — in `api/templates/overlay_events.html`, extend `HEADLINES` after the `model_losing_streak` line:

```js
      stream_started: (e) => e.payload.recovery_seconds != null
        ? `stream back — recovered in ${Math.round(e.payload.recovery_seconds)}s`
        : "stream went live",
      stream_stopped: (e) => "stream stopped",
      stream_dropped: (e) => "stream dropped — recovering",
```

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest tests/unit/test_stream_events.py tests/unit/test_salience.py tests/api/test_overlay_pages.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add world/stream_events.py world/salience.py api/templates/overlay_events.html tests/unit/test_stream_events.py
git commit -m "World: stream lifecycle events with JSONL spool + overlay headlines"
```

---

### Task 5: Stream watchdog — pure state machine + runner

**Files:**
- Create: `scripts/stream_watchdog.py`
- Test: `tests/unit/test_stream_watchdog.py`

**Interfaces:**
- Consumes: `stream_ctl.make_client/get_status/build_scene/start_stream`, `world.stream_events.record_stream_event`.
- Produces: `WatchdogConfig` (dataclass: `poll_seconds=30.0, dropped_ratio_threshold=0.05, restart_cooldown_seconds=300.0, obs_command=("obs", "--startstreaming", "--minimize-to-tray")`), `WatchdogState` (dataclass: `obs_up=True, streaming=False, last_restart_at=None, down_since=None, dropped_flagged=False`), `tick(probe, state, config, now) -> (WatchdogState, list[tuple])` where actions are `("record", event_type, payload) | ("relaunch_obs",) | ("rebuild_scene",) | ("start_stream",)`; `probe_obs() -> dict` (`{"reachable", "streaming", "dropped_ratio"}`), `execute_actions(actions, config)`, `main()` loop.

- [ ] **Step 1: Write failing tests** — `tests/unit/test_stream_watchdog.py`:

```python
"""Watchdog state machine — pure tick() function, no OBS, no clock, no DB."""

from scripts.stream_watchdog import WatchdogConfig, WatchdogState, tick

CFG = WatchdogConfig()


def _actions_of(actions, kind):
    return [a for a in actions if a[0] == kind]


def test_obs_down_records_drop_and_relaunches():
    state = WatchdogState(obs_up=True, streaming=True)
    state, actions = tick({"reachable": False}, state, CFG, now=1000.0)
    assert ("relaunch_obs",) in actions
    records = _actions_of(actions, "record")
    assert records and records[0][1] == "stream_dropped"
    assert state.obs_up is False and state.streaming is False


def test_backoff_suppresses_restart_storm():
    state = WatchdogState(obs_up=False, streaming=False,
                          last_restart_at=1000.0, down_since=990.0)
    state, actions = tick({"reachable": False}, state, CFG, now=1030.0)
    assert _actions_of(actions, "relaunch_obs") == []  # within cooldown
    state, actions = tick({"reachable": False}, state, CFG,
                          now=1000.0 + CFG.restart_cooldown_seconds)
    assert ("relaunch_obs",) in actions


def test_obs_recovery_rebuilds_scene_then_streaming_records_started():
    state = WatchdogState(obs_up=False, streaming=False, down_since=900.0)
    probe = {"reachable": True, "streaming": True, "dropped_ratio": 0.0}
    state, actions = tick(probe, state, CFG, now=1000.0)
    assert ("rebuild_scene",) in actions
    records = _actions_of(actions, "record")
    assert records[0][1] == "stream_started"
    assert records[0][2]["recovery_seconds"] == 100.0
    assert state.obs_up and state.streaming


def test_stream_stopped_underneath_us_restarts_and_records():
    state = WatchdogState(obs_up=True, streaming=True)
    probe = {"reachable": True, "streaming": False, "dropped_ratio": 0.0}
    state, actions = tick(probe, state, CFG, now=1000.0)
    records = _actions_of(actions, "record")
    assert records and records[0][1] == "stream_dropped"
    assert ("start_stream",) in actions
    assert state.streaming is False


def test_dropped_frames_records_event_but_never_restarts():
    state = WatchdogState(obs_up=True, streaming=True)
    probe = {"reachable": True, "streaming": True, "dropped_ratio": 0.09}
    state, actions = tick(probe, state, CFG, now=1000.0)
    records = _actions_of(actions, "record")
    assert records and records[0][1] == "stream_dropped"
    assert records[0][2]["reason"] == "dropped_frames"
    assert _actions_of(actions, "relaunch_obs") == []
    assert _actions_of(actions, "start_stream") == []
    # flag latches: no repeat while the ratio stays high
    state, actions = tick(probe, state, CFG, now=1030.0)
    assert _actions_of(actions, "record") == []
    # ratio recovers → latch resets
    ok = {"reachable": True, "streaming": True, "dropped_ratio": 0.0}
    state, _ = tick(ok, state, CFG, now=1060.0)
    state, actions = tick(probe, state, CFG, now=1090.0)
    assert _actions_of(actions, "record")


def test_cold_start_not_streaming_starts_stream():
    state = WatchdogState()  # obs_up=True, streaming=False
    probe = {"reachable": True, "streaming": False, "dropped_ratio": 0.0}
    state, actions = tick(probe, state, CFG, now=1000.0)
    assert ("start_stream",) in actions
    assert _actions_of(actions, "record") == []  # nothing died; nothing to record
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/unit/test_stream_watchdog.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `scripts/stream_watchdog.py`:**

```python
"""Stream watchdog (Sprint 11): notices OBS or the stream dying and brings it
back without a human. Detector only — world/stream_events.py is the recorder.

Pure state machine (tick) + thin runner loop, so the logic is tested with no
OBS, no clock, and no DB. Compose restart policies already self-heal the
api/scheduler containers; this covers the host-side pieces compose can't.
"""

import logging
import subprocess
import sys
import time
from dataclasses import dataclass

from config.logging import setup_logging
from scripts import stream_ctl
from world.stream_events import record_stream_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogConfig:
    poll_seconds: float = 30.0
    # High dropped-frame ratio is a bandwidth symptom: record it, never
    # restart (a restart makes congestion worse).
    dropped_ratio_threshold: float = 0.05
    # Max one restart attempt per cooldown window — no flapping.
    restart_cooldown_seconds: float = 300.0
    obs_command: tuple = ("obs", "--startstreaming", "--minimize-to-tray")


@dataclass
class WatchdogState:
    obs_up: bool = True
    streaming: bool = False
    last_restart_at: float | None = None
    down_since: float | None = None
    dropped_flagged: bool = False


def tick(
    probe: dict, state: WatchdogState, config: WatchdogConfig, now: float
) -> tuple[WatchdogState, list[tuple]]:
    """One evaluation: probe result in, (new state, actions) out. Pure."""
    actions: list[tuple] = []

    if not probe.get("reachable"):
        if state.obs_up:
            actions.append(("record", "stream_dropped",
                            {"reason": "obs_unreachable"}))
            state.down_since = now
        state.obs_up = False
        state.streaming = False
        if _restart_allowed(state, config, now):
            actions.append(("relaunch_obs",))
            state.last_restart_at = now
        return state, actions

    if not state.obs_up:
        actions.append(("rebuild_scene",))
        state.obs_up = True

    if probe.get("streaming"):
        if not state.streaming:
            payload = {}
            if state.down_since is not None:
                payload["recovery_seconds"] = round(now - state.down_since, 1)
                state.down_since = None
            actions.append(("record", "stream_started", payload))
        state.streaming = True
        ratio = probe.get("dropped_ratio", 0.0)
        if ratio >= config.dropped_ratio_threshold:
            if not state.dropped_flagged:
                actions.append(("record", "stream_dropped",
                                {"reason": "dropped_frames",
                                 "dropped_ratio": round(ratio, 4)}))
                state.dropped_flagged = True
        else:
            state.dropped_flagged = False
        return state, actions

    if state.streaming:
        actions.append(("record", "stream_dropped",
                        {"reason": "stream_inactive"}))
        state.down_since = now
        state.streaming = False
    if _restart_allowed(state, config, now):
        actions.append(("start_stream",))
        state.last_restart_at = now
    return state, actions


def _restart_allowed(
    state: WatchdogState, config: WatchdogConfig, now: float
) -> bool:
    return (
        state.last_restart_at is None
        or now - state.last_restart_at >= config.restart_cooldown_seconds
    )


def probe_obs() -> dict:
    try:
        client = stream_ctl.make_client()
        status = stream_ctl.get_status(client)
    except stream_ctl.ObsUnreachable:
        return {"reachable": False}
    except Exception:
        return {"reachable": False}
    return {
        "reachable": True,
        "streaming": status["streaming"],
        "dropped_ratio": status["dropped_ratio"],
    }


def execute_actions(actions: list[tuple], config: WatchdogConfig) -> None:
    for action in actions:
        kind = action[0]
        try:
            if kind == "record":
                record_stream_event(action[1], action[2])
            elif kind == "relaunch_obs":
                logger.warning("OBS unreachable — relaunching")
                subprocess.Popen(
                    list(config.obs_command),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif kind == "rebuild_scene":
                stream_ctl.build_scene(stream_ctl.make_client())
                logger.info("Scene rebuilt after OBS recovery")
            elif kind == "start_stream":
                stream_ctl.start_stream(stream_ctl.make_client())
                logger.warning("Stream inactive — StartStream issued")
        except Exception:
            logger.exception("Watchdog action failed", extra={"action": kind})


def main() -> None:
    setup_logging()
    config = WatchdogConfig()
    state = WatchdogState()
    logger.info("Stream watchdog started",
                extra={"poll_seconds": config.poll_seconds})
    while True:
        state, actions = tick(probe_obs(), state, config, now=time.time())
        execute_actions(actions, config)
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
```

(Check the actual name of the logging setup function in `config/logging.py` before writing — use whatever it exports.)

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_stream_watchdog.py -v`
Expected: 6 PASS

- [ ] **Step 5: Mutation sanity check** — invert the backoff condition (`>=` → `<`) in `_restart_allowed`, run the suite, confirm `test_backoff_suppresses_restart_storm` FAILS, revert.

- [ ] **Step 6: Commit**

```bash
git add scripts/stream_watchdog.py tests/unit/test_stream_watchdog.py
git commit -m "Ops: stream watchdog — pure state machine, backoff, auto-recover"
```

---

### Task 6: Soak-test tooling — `scripts/soak_report.py`

**Files:**
- Create: `scripts/soak_report.py`
- Test: `tests/unit/test_soak_report.py`

**Interfaces:**
- Consumes: `storage.postgres_store.get_world_events` (stream_* rows).
- Produces: `compute_uptime(events, window_start, window_end) -> dict` (`{"uptime_pct", "downtime_seconds", "outages": [{"start","end","duration_seconds","reason"}]}`) — pure, testable; CLI `python scripts/soak_report.py --hours 24` prints a markdown report.

- [ ] **Step 1: Write failing tests** — `tests/unit/test_soak_report.py`:

```python
"""Uptime math over stream_* world events — the log built this sprint IS the
measurement tool."""

from datetime import datetime, timezone

from scripts.soak_report import compute_uptime


def _ev(minute, event_type, payload=None):
    return {
        "occurred_at": datetime(2026, 7, 21, 0, minute,
                                tzinfo=timezone.utc).isoformat(),
        "event_type": event_type,
        "payload": payload or {},
    }


WINDOW = (datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc),
          datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc))


def test_no_events_means_unknown_full_uptime():
    result = compute_uptime([], *WINDOW)
    assert result["uptime_pct"] == 100.0 and result["outages"] == []


def test_single_outage_and_recovery():
    events = [
        _ev(0, "stream_started"),
        _ev(30, "stream_dropped", {"reason": "obs_unreachable"}),
        _ev(36, "stream_started", {"recovery_seconds": 360}),
    ]
    result = compute_uptime(events, *WINDOW)
    assert result["downtime_seconds"] == 360.0
    assert result["uptime_pct"] == 90.0
    assert len(result["outages"]) == 1
    assert result["outages"][0]["reason"] == "obs_unreachable"


def test_unrecovered_outage_runs_to_window_end():
    events = [_ev(0, "stream_started"), _ev(50, "stream_dropped")]
    result = compute_uptime(events, *WINDOW)
    assert result["downtime_seconds"] == 600.0
    assert result["uptime_pct"] == pytest.approx(83.33, abs=0.01)


def test_dropped_frames_reason_is_not_downtime():
    # dropped_frames means degraded, not down — stream stayed live
    events = [
        _ev(0, "stream_started"),
        _ev(30, "stream_dropped", {"reason": "dropped_frames"}),
    ]
    result = compute_uptime(events, *WINDOW)
    assert result["uptime_pct"] == 100.0
    assert result["outages"][0]["duration_seconds"] == 0


import pytest  # noqa: E402  (used by approx above; keep imports tidy in real file)
```

(Put the `import pytest` at the top in the real file — shown here for completeness.)

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/unit/test_soak_report.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `scripts/soak_report.py`:**

```python
"""Soak-test report (Sprint 11): summarize stream uptime from stream_* world
events plus stack health, as markdown. Failures found by the soak are the
SUCCESS output of the test — same truth-over-vanity rule as the backtest.
"""

import argparse
from datetime import datetime, timedelta, timezone


def compute_uptime(events: list[dict], window_start: datetime,
                   window_end: datetime) -> dict:
    """Fold stream_* events (any order) into uptime numbers. Outages open on
    stream_dropped (except reason=dropped_frames — degraded, not down) and
    close on the next stream_started."""
    ordered = sorted(events, key=lambda e: e["occurred_at"])
    outages: list[dict] = []
    open_outage: dict | None = None
    for event in ordered:
        occurred = datetime.fromisoformat(event["occurred_at"])
        etype = event["event_type"]
        payload = event.get("payload") or {}
        if etype == "stream_dropped":
            degraded = payload.get("reason") == "dropped_frames"
            if open_outage is None:
                open_outage = {
                    "start": occurred,
                    "end": occurred if degraded else None,
                    "reason": payload.get("reason", "unknown"),
                }
        elif etype == "stream_started" and open_outage is not None:
            if open_outage["end"] is None:
                open_outage["end"] = occurred
            outages.append(open_outage)
            open_outage = None
    if open_outage is not None:
        if open_outage["end"] is None:
            open_outage["end"] = window_end
        outages.append(open_outage)

    downtime = sum(
        (o["end"] - o["start"]).total_seconds() for o in outages
    )
    window = (window_end - window_start).total_seconds()
    uptime_pct = round(100.0 * (1 - downtime / window), 2) if window else 100.0
    return {
        "uptime_pct": uptime_pct,
        "downtime_seconds": downtime,
        "outages": [
            {
                "start": o["start"].isoformat(),
                "end": o["end"].isoformat(),
                "duration_seconds": (o["end"] - o["start"]).total_seconds(),
                "reason": o["reason"],
            }
            for o in outages
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream soak report")
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    from storage.postgres_store import get_world_events

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=args.hours)
    events = [
        e for e in get_world_events(limit=10_000, since=window_start)
        if e["event_type"].startswith("stream_")
    ]
    report = compute_uptime(events, window_start, window_end)
    print(f"# Soak report — {window_end.date()}\n")
    print(f"- Window: {window_start.isoformat()} → {window_end.isoformat()}")
    print(f"- **Uptime: {report['uptime_pct']}%** "
          f"({report['downtime_seconds']:.0f}s down)")
    print(f"- Outages: {len(report['outages'])}\n")
    if report["outages"]:
        print("| start | duration (s) | reason |")
        print("|---|---|---|")
        for o in report["outages"]:
            print(f"| {o['start']} | {o['duration_seconds']:.0f} "
                  f"| {o['reason']} |")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/unit/test_soak_report.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/soak_report.py tests/unit/test_soak_report.py
git commit -m "QA: soak_report — uptime math over stream lifecycle events"
```

---

### Task 7: Full-suite green + lint + graph update

- [ ] **Step 1:** Run: `pytest` — Expected: all tests pass (existing 239 + new).
- [ ] **Step 2:** Run: `ruff check .` — Expected: clean; fix anything it flags.
- [ ] **Step 3:** Run: `graphify update .`
- [ ] **Step 4:** Commit any fixes: `git commit -am "Style: lint fixes for stream tooling"` (only if changes).

---

### Task 8: Live verification against local OBS (host has OBS 32.1.2)

**Precondition:** OBS websocket server enabled. Check `~/.config/obs-studio/plugin_config/obs-websocket/config.json` — if `server_enabled` is false or the file is missing, this is the **user step**: OBS → Tools → WebSocket Server Settings → enable, copy password into `.env` (`OBS_WS_PASSWORD`). If the config file exists, wire its values into `.env` directly.

- [ ] **Step 1:** Ensure the stack is up: `docker compose up -d` then `curl -s localhost:8000/health`.
- [ ] **Step 2:** Launch OBS (if not running): `obs --minimize-to-tray &` (or ask the user if a GUI session complicates it).
- [ ] **Step 3:** `python scripts/stream_ctl.py build` — Expected: JSON with created sources.
- [ ] **Step 4:** Re-run build — Expected: `"created": []` (idempotence, live).
- [ ] **Step 5:** `python scripts/stream_ctl.py status` — Expected: obs_version, scene, streaming:false.
- [ ] **Step 6:** `python scripts/stream_ctl.py screenshot --path data/scene_v1.png` and view it — chart + both overlays visible.
- [ ] **Step 7:** Commit nothing (verification only); attach the screenshot to the sprint note.

---

### Task 9: Docs — streaming runbook, README, architecture statuses

**Files:**
- Create: `docs/streaming-runbook.md`
- Modify: `README.md` (Streaming section), `docs/architecture-vision.md` (broadcast plane status), `docs/roadmap.md` (S11 actuals), `CLAUDE.md` (local only — gitignored, don't commit)

**Runbook contents (write for future-you with zero context):** OBS install note (already at 32.1.2 via apt), websocket enablement + `.env` keys table, scene build (`stream_ctl build`), audio bed setup (`STREAM_AUDIO_DIR`, licensing section with per-track provenance — user fills sources), `configure-output` + go-live checklist (health green → bars fresh → OBS up → scene built → audio playing → bitrate 4500kbps@1080p30 or 2500@720p30 per measured upload → start → verify on platform dashboard → watchdog running → first stream unlisted), watchdog as systemd --user unit (unit file inline: `ExecStart=<venv-python> scripts/stream_watchdog.py`, `Restart=on-failure`, `WantedBy=default.target`, enable with `systemctl --user enable --now stream-watchdog` + `loginctl enable-linger`), soak procedure (`soak_report.py --hours 24`, one deliberate OBS kill mid-soak), troubleshooting (websocket auth failure, encoder overload → lower preset/bitrate, RTMP disconnect → watchdog handles, platform-side checks).

- [ ] **Step 1:** Write `docs/streaming-runbook.md` per outline above.
- [ ] **Step 2:** README Streaming section: cold start → live in one command sequence; link runbook.
- [ ] **Step 3:** Update architecture-vision broadcast plane + roadmap S11 actuals.
- [ ] **Step 4:** `graphify update .`
- [ ] **Step 5:** Commit: `git commit -m "Docs: streaming runbook + broadcast plane status"`

---

### Task 10: Sprint bookkeeping + user-step handoff

- [ ] Update Obsidian sprint note: tick completed tickets, set frontmatter `status: In Progress`, note OBS-already-installed discovery.
- [ ] Update `CLAUDE.md` (local): stream scripts, SCENE_SPEC location, new event types, watchdog, no-OBS test discipline.
- [ ] Write memory/.remember entries.
- [ ] Report remaining **user steps**: (1) enable OBS websocket + password into `.env` if not automatable, (2) platform choice + stream key into `.env`/OBS, (3) drop DMCA-safe audio files into `STREAM_AUDIO_DIR` + record their sources in the runbook licensing table, (4) schedule the 24h soak window. The go-live (RTMP test + soak) happens after those.

---

## Self-Review Notes

- Ticket coverage: scene spec (T2), stream_ctl (T3), lifecycle events + overlay (T4), OBS install/runbook (T1/T8/T9 — install already done), watchdog (T5), RTMP go-live (T3 `configure-output` + T9 checklist + user step), audio bed (T2 `audio_sources` + T9 + user step), docs (T9), soak (T6 tooling + user-scheduled run), tests (inside every task, TDD).
- Types consistent: action tuples in T5 tests match T5 impl; `record_stream_event(event_type, payload, spool_path)` used identically in T3/T5.
- Known deferred-to-execution checks: exact `config/logging.py` setup function name; obsws-python resolved version; whether OBS websocket config file exists.
