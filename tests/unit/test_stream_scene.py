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


def test_every_browser_source_gets_its_own_origin():
    """KI-013, the invariant that would have caught it. All browser sources run
    in ONE obs-browser process sharing one Chromium network stack, which allows
    6 concurrent HTTP/1.1 connections **per origin**. Every page holds one open
    forever for SSE, and `shutdown: False` keeps all scenes' sources alive at
    once — so eight sources on a single origin starved `/world/state` forever
    and the room never drew any data.

    One origin per source keeps every page's SSE + fetches inside its own
    budget. If this fails, someone added a source without extending
    `_SHARD_HOSTS` — extend it rather than doubling up.
    """
    origins = []
    for scene in stream_scene.scenes_spec():
        for src in scene["sources"]:
            if src["kind"] != "browser_source":
                continue
            parsed = urlparse(src["settings"]["url"])
            origins.append((f"{parsed.scheme}://{parsed.netloc}", src["name"]))
    by_origin: dict[str, list[str]] = {}
    for origin, name in origins:
        by_origin.setdefault(origin, []).append(name)
    shared = {o: names for o, names in by_origin.items() if len(names) > 1}
    assert not shared, (
        f"browser sources sharing a 6-connection pool: {shared}. "
        f"Chromium allows 6 concurrent connections per origin and each page "
        f"holds one open for SSE — extend _SHARD_HOSTS."
    )


def test_non_loopback_page_base_is_left_alone(monkeypatch):
    """Sharding is a loopback trick (127/8 is all the same server). A real host
    has one address, so rewriting it would point every source at nothing."""
    monkeypatch.setenv("STREAM_PAGE_BASE", "http://api:8000")
    urls = [
        src["settings"]["url"]
        for scene in stream_scene.scenes_spec()
        for src in scene["sources"]
        if src["kind"] == "browser_source"
    ]
    assert urls and all(url.startswith("http://api:8000") for url in urls)


def test_scenes_spec_has_the_named_scenes():
    names = [s["scene"] for s in stream_scene.scenes_spec()]
    assert names == ["chart-focus", "world-focus", "event-focus", "standby"]


def test_home_scene_is_still_first():
    """`build_scene` rests on scenes_spec()[0] and the director decays to it —
    standby must never become the resting scene."""
    assert stream_scene.scenes_spec()[0]["scene"] == stream_scene.SCENE_CHART
    assert stream_scene.SCENE_NAME == stream_scene.SCENE_CHART


def test_standby_scene_is_a_single_full_canvas_card(monkeypatch):
    """B10: the surface a viewer sees instead of a frozen frame. One source
    covering the whole canvas — anything smaller shows the dead scene behind
    it."""
    monkeypatch.delenv("STREAM_AUDIO_DIR", raising=False)
    standby = next(
        s for s in stream_scene.scenes_spec()
        if s["scene"] == stream_scene.SCENE_STANDBY
    )
    browser = [s for s in standby["sources"] if s["kind"] == "browser_source"]
    assert len(browser) == 1
    settings = browser[0]["settings"]
    assert (settings["width"], settings["height"]) == stream_scene.CANVAS
    assert (browser[0]["x"], browser[0]["y"]) == (0, 0)


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
