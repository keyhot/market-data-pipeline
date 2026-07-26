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
    canvas_w, canvas_h = spec["canvas"]
    for src in spec["sources"]:
        settings = src["settings"]
        if "width" not in settings:  # audio sources have no geometry
            continue
        assert 0 <= src["x"] and src["x"] + settings["width"] <= canvas_w, src["name"]
        assert 0 <= src["y"] and src["y"] + settings["height"] <= canvas_h, src["name"]


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


def test_scenes_spec_has_three_named_scenes():
    names = [s["scene"] for s in stream_scene.scenes_spec()]
    assert names == ["chart-focus", "world-focus", "event-focus"]


def test_scene_spec_shim_returns_the_first_scene():
    assert stream_scene.scene_spec() == stream_scene.scenes_spec()[0]


def test_scene_source_names_unique_within_each_scene():
    for scene in stream_scene.scenes_spec():
        names = [s["name"] for s in scene["sources"]]
        assert len(names) == len(set(names)), scene["scene"]


def test_every_scene_geometry_within_canvas():
    for scene in stream_scene.scenes_spec():
        canvas_w, canvas_h = scene["canvas"]
        for src in scene["sources"]:
            settings = src["settings"]
            if "width" not in settings:  # audio sources have no geometry
                continue
            x, y = src["x"], src["y"]
            w, h = settings["width"], settings["height"]
            assert 0 <= x and x + w <= canvas_w, src["name"]
            assert 0 <= y and y + h <= canvas_h, src["name"]


def test_every_scene_url_hits_a_real_route():
    from api.main import app

    for scene in stream_scene.scenes_spec():
        for src in scene["sources"]:
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
