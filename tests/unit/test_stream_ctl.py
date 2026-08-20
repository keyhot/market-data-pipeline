"""stream_ctl logic against a fake obsws client — CI has no OBS, so every
request the tool would issue is pinned here instead."""

import pytest

from scripts import stream_ctl, stream_scene


class FakeResp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    def __init__(
        self,
        scenes=(),
        inputs=(),
        streaming=False,
        skipped=0,
        total=0,
        transitions=("Cut", "Fade"),
    ):
        self.calls = []
        self._item_ids = {}
        self.transforms = {}
        self._scenes = list(scenes)
        self._inputs = list(inputs)
        self._streaming = streaming
        self._skipped, self._total = skipped, total
        self._transitions = list(transitions)
        self.current_program_scene = None

    # Present on the real client, so present here: a fake laxer than the thing
    # it stands in for is how every A4 seam bug stayed green.
    def get_scene_transition_list(self):
        return FakeResp(transitions=[{"transitionName": t} for t in self._transitions])

    def set_current_scene_transition(self, name):
        self._log("set_current_scene_transition", name)

    def set_current_scene_transition_duration(self, ms):
        self._log("set_current_scene_transition_duration", ms)

    def set_input_volume(self, name, vol_db=None):
        self._log("set_input_volume", name, vol_db)

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
        # Distinct per source: an ordering bug is invisible if every item in
        # the scene answers to the same id.
        self._item_ids.setdefault((scene, name), len(self._item_ids) + 1)
        return FakeResp(scene_item_id=self._item_ids[(scene, name)])

    def set_scene_item_transform(self, scene, item_id, transform):
        # The transform payload itself, not just the fact of the call: a fake
        # that forgets the arguments cannot catch a builder that pins position
        # and forgets scale (KI-026).
        self.transforms[(scene, item_id)] = dict(transform)
        self._log("set_scene_item_transform", scene, item_id)

    def set_scene_item_index(self, scene, item_id, item_index):
        self._log("set_scene_item_index", scene, item_id, item_index)

    def set_current_program_scene(self, name):
        self._log("set_current_program_scene", name)
        self.current_program_scene = name

    def start_stream(self):
        self._log("start_stream")

    def stop_stream(self):
        self._log("stop_stream")

    def get_version(self):
        return FakeResp(obs_version="32.1.2")

    def get_current_program_scene(self):
        scene = self.current_program_scene or "chart-focus"
        return FakeResp(current_program_scene_name=scene)

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

    def set_profile_parameter(self, category, name, value):
        self._log("set_profile_parameter", category, name, value)


def _creates(client):
    return [c for c in client.calls if c[0] in ("create_scene", "create_input")]


def test_build_creates_all_scenes_and_sources():
    client = FakeClient()
    result = stream_ctl.build_scene(client)
    assert result["scene"] == "chart-focus"  # rests on the home scene
    # standby (B10) is built like any other scene so it is ready the moment the
    # watchdog needs it — but it is never the scene we rest on.
    assert result["scenes"] == [
        "chart-focus", "world-focus", "event-focus", "standby",
    ]
    created = {c[1] for c in _creates(client)}
    assert {
        "chart-focus",
        "world-focus",
        "event-focus",
        "charts-1m",
        "world-room",
        "event-feed",
    } <= created


def test_build_single_spec_back_compat():
    client = FakeClient()
    from scripts import stream_scene

    result = stream_ctl.build_scene(client, stream_scene.scenes_spec()[1])
    assert result["scene"] == "world-focus"
    assert client.current_program_scene == "world-focus"


def test_switch_scene_sets_the_program_scene():
    client = FakeClient()
    stream_ctl.switch_scene(client, "world-focus")
    assert client.current_program_scene == "world-focus"


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


def test_screenshot_targets_program_scene(tmp_path):
    client = FakeClient()
    path = stream_ctl.screenshot(client, tmp_path / "shot.png")
    call = client.calls[-1]
    assert call[0] == "save_source_screenshot"
    assert call[1] == "chart-focus" and call[2] == "png"
    assert str(path).endswith("shot.png")


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


def test_configure_output_pins_encoder_bitrate(monkeypatch):
    # KI-009: without this, OBS keeps the profile's default bitrate (was 6000,
    # which YouTube rejected). configure_output must pin the Simple-mode encoder
    # bitrate, defaulting to a safe 2200.
    monkeypatch.setenv("OBS_STREAM_KEY", "sekrit")
    monkeypatch.delenv("OBS_STREAM_BITRATE", raising=False)
    client = FakeClient()
    stream_ctl.configure_output(client)
    assert ("set_profile_parameter", "SimpleOutput", "VBitrate", "2200") in client.calls


def test_configure_output_bitrate_from_env(monkeypatch):
    monkeypatch.setenv("OBS_STREAM_KEY", "sekrit")
    monkeypatch.setenv("OBS_STREAM_BITRATE", "3500")
    client = FakeClient()
    stream_ctl.configure_output(client)
    assert ("set_profile_parameter", "SimpleOutput", "VBitrate", "3500") in client.calls


def test_cli_exit_code_when_obs_unreachable(monkeypatch):
    def boom():
        raise stream_ctl.ObsUnreachable("no OBS")

    monkeypatch.setattr(stream_ctl, "make_client", boom)
    assert stream_ctl.main(["status"]) == 2


# --- B2: transitions and the audio-bed swell hook ----------------------------


def test_scene_switches_get_a_fade_rather_than_a_cut():
    """The director switches scenes on salience; a hard cut reads as a glitch
    on a stream whose whole register is calm-that-swells."""
    client = FakeClient()
    stream_ctl.set_transition(client)
    assert ("set_current_scene_transition", "Fade") in client.calls
    assert any(c[0] == "set_current_scene_transition_duration" for c in client.calls)


def test_a_missing_transition_is_skipped_rather_than_crashing_the_stream():
    """OBS profiles can be built without the stock transitions. Never crash the
    stream over a nicety — the cut still works."""
    client = FakeClient(transitions=("Cut",))
    stream_ctl.set_transition(client)
    assert not any(c[0] == "set_current_scene_transition" for c in client.calls)


def test_audio_bed_gain_ramps_with_tier_and_is_silent_by_default():
    """The swell hook: louder as the world gets dramatic. Tier 0 is the resting
    bed level, and the ramp must be monotonic or a bigger event would duck."""
    from scripts.stream_scene import audio_gain_db

    gains = [audio_gain_db(t) for t in range(4)]
    assert gains == sorted(gains), gains
    assert len(set(gains)) == 4
    assert audio_gain_db(-1) == gains[0] and audio_gain_db(9) == gains[3]


def test_setting_the_audio_gain_is_a_no_op_without_an_audio_bed():
    """The bed only exists when STREAM_AUDIO_DIR is set (Sprint 11). Addressing
    a source that isn't there must not raise on a live stream."""
    client = FakeClient()
    stream_ctl.set_audio_gain(client, tier=3, sources=[])
    assert not any(c[0] == "set_input_volume" for c in client.calls)

    stream_ctl.set_audio_gain(client, tier=3, sources=["audio-bed"])
    assert ("set_input_volume", "audio-bed", pytest.approx(0.0)) in client.calls


def test_build_puts_the_fade_in_place_before_the_director_starts_switching():
    """Every switch after `build` is the director's, so the transition has to be
    set during build or the first dozen scene changes are hard cuts."""
    client = FakeClient()
    stream_ctl.build_scene(client)
    assert ("set_current_scene_transition", "Fade") in client.calls


def test_build_pins_the_layer_order_instead_of_inheriting_it():
    """Found live on 2026-08-20: the events rail rendered perfectly as a source
    and was **invisible on air** — `charts-1m` (1920x840, the full frame) sat
    on top of it on the home scene. Stacking came from the order the inputs
    happened to be created in, and `charts-1m` was added three weeks after the
    rail (B13), so it landed on top and stayed there. `build` sets position and
    settings; it never set the one property that decides what you can see.

    Same shape as KI-009 (encoder bitrate inherited from whatever the profile
    had) and B2's transition: if the layout is not asserted, it is inherited."""
    client = FakeClient()
    stream_ctl.build_scene(client)

    spec = next(s for s in stream_scene.scenes_spec() if s["scene"] == "chart-focus")
    order = [src["name"] for src in spec["sources"]]
    ids = {name: client._item_ids[("chart-focus", name)] for name in order}
    indexes = {
        item_id: index
        for call, scene, item_id, index in
        [c for c in client.calls if c[0] == "set_scene_item_index"]
        if scene == "chart-focus"
    }

    # Spec order is bottom-to-top, and every source is placed by it.
    assert [indexes[ids[name]] for name in order] == list(range(len(order)))
    # The one that matters: the rail is above the chart that used to bury it.
    assert indexes[ids["overlay-events"]] > indexes[ids["charts-1m"]]


def test_no_two_sources_overlap_in_any_scene():
    """The geometry half of the same bug — stated the way it should have been
    stated the first time.

    KI-023 asserted that no source is *entirely* covered by one above it, and
    "entirely" is the word that cost us KI-025: the rail covers the right 480px
    of `charts-1m`, which is exactly where ETHUSDT's price scale, last-price
    label and newest candles live. A partial cover of the one strip carrying
    the numbers passed cleanly. So the layout does not overlap at all, and the
    test says so — every scene here tiles the canvas instead of stacking on it.
    """
    for spec in stream_scene.scenes_spec():
        boxes = [
            (src["name"], src["x"], src["y"],
             src["settings"]["width"], src["settings"]["height"])
            for src in spec["sources"] if src["kind"] == "browser_source"
        ]
        for lower, (name, x, y, w, h) in enumerate(boxes):
            for other, ox, oy, ow, oh in boxes[lower + 1:]:
                overlap_w = min(x + w, ox + ow) - max(x, ox)
                overlap_h = min(y + h, oy + oh) - max(y, oy)
                assert overlap_w <= 0 or overlap_h <= 0, (
                    f"{spec['scene']}: {other} overlaps {name} by "
                    f"{overlap_w}x{overlap_h}px"
                )


def test_build_pins_scale_so_a_resized_source_is_put_back():
    """KI-026: `event-chart` sat in live OBS at scale 1.509 — rendering
    1449x815 against a spec that declares 960x540 — with 489px of it lying
    across the event rail, shredding its headlines mid-word.

    `_build_one` set positionX/positionY and nothing else. Scale is a separate
    scene-item property, so `build` reported success, was idempotent by its own
    lights, and **could not repair the frame**: re-running it moved the source
    back to (0,0) and left it 1.5x too big. Third instance of one shape, after
    KI-009's bitrate and KI-023's stacking — if the layout is not asserted, it
    is inherited."""
    client = FakeClient()
    stream_ctl.build_scene(client)

    assert client.transforms, "the builder must set a transform per scene item"
    for (scene, item_id), transform in client.transforms.items():
        assert transform["scaleX"] == 1.0, f"{scene}/{item_id} x-scale unpinned"
        assert transform["scaleY"] == 1.0, f"{scene}/{item_id} y-scale unpinned"
        # A bounds fit would silently override scale, so the spec's "the source
        # renders at its own declared size" has to say that too.
        assert transform["boundsType"] == "OBS_BOUNDS_NONE"
        assert transform["cropLeft"] == 0 and transform["cropRight"] == 0
        assert transform["cropTop"] == 0 and transform["cropBottom"] == 0
        # positionX/Y mean different pixels under a different alignment.
        assert transform["alignment"] == stream_ctl.ALIGN_TOP_LEFT


def test_the_rendered_rect_of_every_source_is_the_rect_the_spec_declares():
    """The invariant KI-026 asks for, one level up from the call: with scale
    pinned to 1.0, what OBS draws is the spec's width/height at the spec's
    x/y — so the no-overlap proof above is a proof about the *frame*, not just
    about a dict."""
    client = FakeClient()
    stream_ctl.build_scene(client)

    for spec in stream_scene.scenes_spec():
        for src in spec["sources"]:
            if src["kind"] != "browser_source":
                continue
            item_id = client._item_ids[(spec["scene"], src["name"])]
            transform = client.transforms[(spec["scene"], item_id)]
            rendered = (
                src["settings"]["width"] * transform["scaleX"],
                src["settings"]["height"] * transform["scaleY"],
            )
            assert rendered == (
                float(src["settings"]["width"]),
                float(src["settings"]["height"]),
            ), f"{spec['scene']}/{src['name']} renders at {rendered}"
            assert transform["positionX"] == float(src["x"])
            assert transform["positionY"] == float(src["y"])
