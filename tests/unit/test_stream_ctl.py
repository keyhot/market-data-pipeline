"""stream_ctl logic against a fake obsws client — CI has no OBS, so every
request the tool would issue is pinned here instead."""

import copy

import pytest

from scripts import stream_ctl
from stream import scene as stream_scene
from tests.unit.obs_real import OBSSDKRequestError, signature_diffs, wire

# obs-websocket's RequestStatus codes for the two refusals `build_scene` can meet.
_NOT_FOUND, _ALREADY_EXISTS = 600, 601


class FakeResp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    """OBS as far as `build_scene` depends on it: scenes, the inputs they are
    made of, and the scene items that join the two.

    Modelled on what obs-websocket does, because a fake that answers for
    anything is how a source created in the wrong scene stayed green:

    - a source name is unique across scenes AND inputs (CreateInput and
      CreateScene both answer "A source already exists by that ... name.");
    - `create_input` also creates the scene item, in the scene it is told, and
      the item is hidden unless `sceneItemEnabled` is true;
    - scene item ids are assigned per scene, from 1, and never reused;
    - `get_scene_item_id` finds only what is in THAT scene, so a source that
      lives in another scene is an error rather than an id;
    - a refused request is an `OBSSDKRequestError`, which is what the real
      client raises (`ReqClient.send`), not a `RuntimeError`.

    Held to the real client's signatures by
    `test_the_fake_takes_the_arguments_the_real_client_takes`.
    """

    def __init__(
        self,
        scenes=(),
        inputs=(),
        streaming=False,
        skipped=0,
        total=0,
        reconnecting=False,
        congestion=0.0,
        transitions=("Cut", "Fade"),
    ):
        self.calls = []
        self.transforms = {}
        # The requestData of each SetInputVolume, exactly as it goes on the wire.
        self.volumes = []
        self._scene_items = {name: [] for name in scenes}  # bottom-first
        self._next_item_id = {name: 1 for name in scenes}
        # Pre-existing inputs sit in no scene.
        self._inputs = {name: {"kind": None, "settings": {}} for name in inputs}
        self._streaming = streaming
        self._reconnecting = reconnecting
        self._congestion = congestion
        self._skipped, self._total = skipped, total
        self._transitions = list(transitions)
        self.current_program_scene = None

    # Present on the real client, so present here: a fake laxer than the thing
    # it stands in for is how every A4 seam bug stayed green.
    def get_scene_transition_list(self):
        return FakeResp(transitions=[{"transitionName": t} for t in self._transitions])

    def set_current_scene_transition(self, name):
        self._log("set_current_scene_transition", name)

    def set_current_scene_transition_duration(self, duration):
        self._log("set_current_scene_transition_duration", duration)

    def set_input_volume(self, name, vol_mul=None, vol_db=None):
        # The real client picks neither: the second positional is the MULTIPLIER,
        # and both keys go out, a None as a JSON null. Which one OBS honours when
        # both (or a null) arrive is the server's business, so none is chosen here.
        self.volumes.append(
            {"inputName": name, "inputVolumeMul": vol_mul, "inputVolumeDb": vol_db}
        )

    def press_input_properties_button(self, input_name, prop_name):
        self._log("press_input_properties_button", input_name, prop_name)

    def _log(self, name, *args):
        self.calls.append((name, *args))

    def _scene(self, request, name):
        if name not in self._scene_items:
            raise OBSSDKRequestError(
                request, _NOT_FOUND, f"No source was found by the name of `{name}`."
            )
        return self._scene_items[name]

    def _input(self, request, name):
        if name not in self._inputs:
            raise OBSSDKRequestError(
                request, _NOT_FOUND, f"No source was found by the name of `{name}`."
            )
        return self._inputs[name]

    def _item(self, request, scene, item_id):
        for item in self._scene(request, scene):
            if item["sceneItemId"] == item_id:
                return item
        raise OBSSDKRequestError(
            request, _NOT_FOUND, f"No scene items were found in scene `{scene}`."
        )

    def _place(self, scene, source, enabled):
        item_id = self._next_item_id[scene]
        self._next_item_id[scene] += 1
        self._scene_items[scene].append(
            {
                "sceneItemId": item_id,
                "sourceName": source,
                # OBS's default for an omitted flag is visible.
                "sceneItemEnabled": True if enabled is None else bool(enabled),
            }
        )
        return item_id

    def get_scene_list(self):
        return FakeResp(scenes=[{"sceneName": s} for s in self._scene_items])

    def create_scene(self, name):
        self._log("create_scene", name)
        if name in self._scene_items or name in self._inputs:
            raise OBSSDKRequestError(
                "CreateScene", _ALREADY_EXISTS,
                "A source already exists by that scene name.",
            )
        self._scene_items[name] = []
        self._next_item_id[name] = 1

    def get_input_list(self, kind=None):
        return FakeResp(
            inputs=[
                {"inputName": n, "inputKind": state["kind"]}
                for n, state in self._inputs.items()
                if kind is None or state["kind"] == kind
            ]
        )

    def get_input_settings(self, name):
        state = self._input("GetInputSettings", name)
        return FakeResp(
            input_settings=copy.deepcopy(state["settings"]), input_kind=state["kind"]
        )

    def create_input(
        self, sceneName, inputName, inputKind, inputSettings, sceneItemEnabled
    ):
        # Also puts a scene item in `sceneName`: the one place an input's scene
        # is decided, which is why it is modelled and not just logged.
        self._log("create_input", inputName)
        self._scene("CreateInput", sceneName)
        if inputName in self._inputs or inputName in self._scene_items:
            raise OBSSDKRequestError(
                "CreateInput", _ALREADY_EXISTS,
                "A source already exists by that input name.",
            )
        self._inputs[inputName] = {
            "kind": inputKind,
            "settings": copy.deepcopy(inputSettings or {}),
        }
        return FakeResp(
            scene_item_id=self._place(sceneName, inputName, sceneItemEnabled)
        )

    def set_input_settings(self, name, settings, overlay):
        self._log("set_input_settings", name)
        state = self._input("SetInputSettings", name)
        if overlay is False:
            state["settings"] = copy.deepcopy(settings)
        else:
            state["settings"].update(copy.deepcopy(settings))

    def get_scene_item_list(self, name):
        items = self._scene("GetSceneItemList", name)
        return FakeResp(
            scene_items=[dict(item, sceneItemIndex=i) for i, item in enumerate(items)]
        )

    def create_scene_item(self, scene_name, source_name, enabled=None):
        self._log("create_scene_item", scene_name, source_name)
        self._scene("CreateSceneItem", scene_name)
        if source_name not in self._inputs and source_name not in self._scene_items:
            raise OBSSDKRequestError(
                "CreateSceneItem", _NOT_FOUND,
                f"No source was found by the name of `{source_name}`.",
            )
        return FakeResp(scene_item_id=self._place(scene_name, source_name, enabled))

    def get_scene_item_id(self, scene_name, source_name, offset=None):
        items = self._scene("GetSceneItemId", scene_name)
        matches = [i for i in items if i["sourceName"] == source_name]
        try:
            item = matches[-1] if offset == -1 else matches[offset or 0]
        except IndexError:
            raise OBSSDKRequestError(
                "GetSceneItemId", _NOT_FOUND,
                "No scene items were found in the specified scene by that name "
                "or offset.",
            ) from None
        return FakeResp(scene_item_id=item["sceneItemId"])

    def set_scene_item_transform(self, scene_name, item_id, transform):
        # The transform payload itself, not just the fact of the call: a fake
        # that forgets the arguments cannot catch a builder that pins position
        # and forgets scale (KI-026).
        self._item("SetSceneItemTransform", scene_name, item_id)
        self.transforms[(scene_name, item_id)] = dict(transform)
        self._log("set_scene_item_transform", scene_name, item_id)

    def set_scene_item_index(self, scene_name, item_id, item_index):
        self._log("set_scene_item_index", scene_name, item_id, item_index)
        items = self._scene("SetSceneItemIndex", scene_name)
        item = self._item("SetSceneItemIndex", scene_name, item_id)
        items.remove(item)
        # Index 0 is the bottom. An index past the top is clamped here; what
        # OBS does with one is unverified, and `build_scene` never sends it.
        items.insert(max(0, min(item_index, len(items))), item)

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
            # KI-021: the reconnect signals. Pinned in the fake because a seam
            # whose fake is narrower than the real payload is how a field goes
            # missing without a test noticing.
            output_reconnecting=self._reconnecting,
            output_congestion=self._congestion,
        )

    def save_source_screenshot(
        self, name, img_format, file_path, width, height, quality
    ):
        self._log("save_source_screenshot", name, img_format, file_path)

    def set_stream_service_settings(self, ss_type, ss_settings):
        self._log("set_stream_service_settings", ss_type, ss_settings)

    def set_profile_parameter(self, category, name, value):
        self._log("set_profile_parameter", category, name, value)

    def get_video_settings(self):
        return FakeResp(
            base_width=1920, base_height=1080, output_width=1280, output_height=720,
            fps_numerator=self.fps_numerator, fps_denominator=self.fps_denominator,
        )

    fps_numerator, fps_denominator = 30, 1


def _stacking(client, scene):
    """What OBS draws in `scene`, bottom to top: the scene's item names."""
    return [i["sourceName"] for i in client.get_scene_item_list(scene).scene_items]


def _item_id(client, scene, source):
    return client.get_scene_item_id(scene, source).scene_item_id


def _layout(client):
    """Every scene's items (ids, order, visibility), as OBS would list them."""
    return {
        s["sceneName"]: client.get_scene_item_list(s["sceneName"]).scene_items
        for s in client.get_scene_list().scenes
    }


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
    from stream import scene as stream_scene

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
    layout = _layout(client)
    client.calls.clear()
    stream_ctl.build_scene(client)
    assert _creates(client) == []  # second run must not create anything
    assert any(c[0] == "set_input_settings" for c in client.calls)
    # ...and must not have moved, duplicated or re-numbered a single scene item.
    assert _layout(client) == layout


def _assert_obs_shows_the_spec(client):
    """What OBS would show after `build_scene`: each scene holds exactly its
    spec's sources, bottom-first, every one visible, of the spec's kind and
    carrying the spec's settings. (The fake keeps only the settings it was
    given, so this is a subset check; a real browser source also fills in its
    defaults.)"""
    for spec in stream_scene.scenes_spec():
        scene = spec["scene"]
        items = client.get_scene_item_list(scene).scene_items
        assert [i["sourceName"] for i in items] == [
            s["name"] for s in spec["sources"]
        ], f"{scene} does not hold its own sources, in spec order"
        hidden = [i["sourceName"] for i in items if not i["sceneItemEnabled"]]
        assert not hidden, f"{scene}: hidden scene items {hidden} are black on air"
        for src in spec["sources"]:
            live = client.get_input_settings(src["name"])
            assert live.input_kind == src["kind"], f"{src['name']} has the wrong kind"
            wrong = {
                k: v for k, v in src["settings"].items()
                if live.input_settings.get(k) != v
            }
            assert not wrong, f"{scene}/{src['name']} lacks settings {wrong}"


def test_build_leaves_every_scene_holding_its_own_visible_configured_sources():
    """The audit-2 F1 mutants, as a state check: a source created in the wrong
    scene, created hidden, created without its settings, or never created in a
    scene at all (an empty scene) each leave OBS showing something other than
    the spec. The old fake logged the calls and answered every id lookup, so none
    of them failed anything."""
    client = FakeClient()
    stream_ctl.build_scene(client)
    _assert_obs_shows_the_spec(client)


def test_a_rebuild_resends_the_spec_settings_over_whatever_drifted():
    """`build` is what the watchdog runs after every OBS recovery; "idempotent"
    must mean it puts the settings back, not that it called something."""
    client = FakeClient()
    stream_ctl.build_scene(client)
    for spec in stream_scene.scenes_spec():
        for src in spec["sources"]:
            client.set_input_settings(
                src["name"], {k: "drifted" for k in src["settings"]}, True
            )
    stream_ctl.build_scene(client)
    _assert_obs_shows_the_spec(client)


def test_a_rebuild_puts_a_scrambled_layer_order_back():
    """The stacking half of the same state check: sources that already exist in
    the wrong order are re-ordered by `build`, which a fresh build (created in
    spec order) cannot show."""
    client = FakeClient()
    for spec in stream_scene.scenes_spec():
        client.create_scene(spec["scene"])
        for src in reversed(spec["sources"]):
            client.create_input(
                spec["scene"], src["name"], src["kind"], src["settings"], True
            )
    assert any(
        _stacking(client, spec["scene"]) != [s["name"] for s in spec["sources"]]
        for spec in stream_scene.scenes_spec()
    ), "the seeded scenes must actually be scrambled"
    stream_ctl.build_scene(client)
    _assert_obs_shows_the_spec(client)


def test_build_with_the_music_bed_carries_one_input_into_every_scene(monkeypatch):
    """STREAM_AUDIO_DIR set: `audio-bed` is ONE input with a scene item in each
    scene (four inputs is four players), built through `get_scene_item_list`
    and `create_scene_item`, which the old fake did not have."""
    monkeypatch.setenv("STREAM_AUDIO_DIR", "/music")
    client = FakeClient()
    stream_ctl.build_scene(client)
    _assert_obs_shows_the_spec(client)

    scenes = [s["scene"] for s in stream_scene.scenes_spec()]
    assert all("audio-bed" in _stacking(client, s) for s in scenes)
    bed_inputs = client.get_input_list(kind="ffmpeg_source").inputs
    assert [i["inputName"] for i in bed_inputs] == ["audio-bed"]
    created = [c[1] for c in client.calls if c[0] == "create_input"]
    assert created.count("audio-bed") == 1
    assert [c for c in client.calls if c[0] == "create_scene_item"] == [
        ("create_scene_item", s, "audio-bed") for s in scenes[1:]
    ]


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


def _keyint(client):
    """The x264 keyframe interval configure_output pinned, in frames, and
    whether Simple mode will actually apply custom encoder settings."""
    params = {
        (c[1], c[2]): c[3] for c in client.calls if c[0] == "set_profile_parameter"
    }
    opts = dict(
        kv.split("=", 1)
        for kv in params.get(("SimpleOutput", "x264Settings"), "").split()
    )
    return int(opts["keyint"]), params.get(("SimpleOutput", "UseAdvanced"))


@pytest.mark.parametrize("num,den", [(30, 1), (60, 1), (30000, 1001), (24, 1)])
def test_configure_output_keyframes_within_youtubes_limit(monkeypatch, num, den):
    # KI-087: on rtmp_custom nothing recommends a GOP, so x264 kept its default
    # keyint=250 — 8.3 s at 30 fps, and YouTube flags > 4 s as an error
    # (gopSizeLong). Graded in SECONDS at the host's real frame rate, so it
    # holds for any fps rather than for today's 30.
    monkeypatch.setenv("OBS_STREAM_KEY", "sekrit")
    client = FakeClient()
    client.fps_numerator, client.fps_denominator = num, den
    stream_ctl.configure_output(client)
    keyint, use_advanced = _keyint(client)
    seconds = keyint * den / num
    assert 1.0 <= seconds <= 2.05
    # Simple mode ignores x264Settings unless UseAdvanced is on.
    assert use_advanced == "true"
    # ...and then reads Preset too: it must stay the one Simple mode used.
    preset = ("set_profile_parameter", "SimpleOutput", "Preset", "veryfast")
    assert preset in client.calls


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
    from stream.scene import audio_gain_db

    gains = [audio_gain_db(t) for t in range(4)]
    assert gains == sorted(gains), gains
    assert len(set(gains)) == 4
    assert audio_gain_db(-1) == gains[0] and audio_gain_db(9) == gains[3]


def test_setting_the_audio_gain_is_a_no_op_without_an_audio_bed():
    """The bed only exists when STREAM_AUDIO_DIR is set (Sprint 11). Addressing
    a source that isn't there must not raise on a live stream."""
    client = FakeClient()
    stream_ctl.set_audio_gain(client, tier=3, sources=[])
    assert client.volumes == []

    stream_ctl.set_audio_gain(client, tier=3, sources=["audio-bed"])
    assert [v["inputName"] for v in client.volumes] == ["audio-bed"]


def test_the_audio_gain_goes_out_as_decibels_with_a_null_multiplier():
    """Pins the call shape production sends today, and nothing more.

    `set_audio_gain` passes `vol_db=` by keyword, and the real client puts BOTH
    volume keys on the wire: the unused multiplier as a JSON null. Whether
    obs-websocket reads that null as "absent" or counts it as a second volume
    field ("You may only specify one volume field.") is UNVERIFIED, so this pins
    what is sent, not that OBS accepts it.

    The second positional parameter of the real `set_input_volume` is the
    MULTIPLIER, which is the slip this guards: a gain of -18.0 passed
    positionally is a negative multiplier, not -18 dB."""
    client = FakeClient()
    stream_ctl.set_audio_gain(client, tier=3, sources=["audio-bed"])
    (sent,) = client.volumes
    assert sent == {
        "inputName": "audio-bed",
        "inputVolumeMul": None,
        "inputVolumeDb": pytest.approx(stream_scene.audio_gain_db(3)),
    }


def test_the_audio_gain_reaches_the_music_bed_build_made_at_every_tier(monkeypatch):
    """`set_audio_gain` with the sources the SPEC names, not a hand-typed list:
    after a real `build_scene` with STREAM_AUDIO_DIR set, every input it
    addresses exists in OBS, the bed is addressed once per call, and what goes
    out is byte for byte what the real `set_input_volume` builds for that tier's
    dB (so a gain in the multiplier slot, or the tier's neighbour, would differ).
    """
    monkeypatch.setenv("STREAM_AUDIO_DIR", "/music")
    client = FakeClient()
    stream_ctl.build_scene(client)
    existing = {i["inputName"] for i in client.get_input_list().inputs}
    (bed,) = stream_scene.audio_source_names()  # the spec's one shared bed

    sent = []
    for tier in range(len(stream_scene._AUDIO_GAIN_DB)):
        client.volumes.clear()
        stream_ctl.set_audio_gain(client, tier=tier)
        gain = stream_scene.audio_gain_db(tier)
        assert client.volumes == [wire("set_input_volume", bed, vol_db=gain)[1]]
        assert {v["inputName"] for v in client.volumes} <= existing
        assert client.volumes[0]["inputVolumeDb"] == gain
        sent.append(gain)

    # The ramp is audible: every tier sent a gain of its own, rising.
    assert sent == sorted(set(sent)) and len(sent) == len(stream_scene._AUDIO_GAIN_DB)


def test_the_audio_gain_sends_nothing_when_the_spec_has_no_bed(monkeypatch):
    """STREAM_AUDIO_DIR unset: the spec has no bed, `audio_source_names()` is
    empty, and the default `sources=None` path sends no request at all."""
    monkeypatch.delenv("STREAM_AUDIO_DIR", raising=False)
    client = FakeClient()
    stream_ctl.build_scene(client)
    stream_ctl.set_audio_gain(client, tier=3)
    assert client.volumes == []


def test_the_fake_records_a_volume_request_as_the_real_client_would_send_it():
    """The second positional is the multiplier, both keys always go out, and a
    value given for both is sent for both: the client chooses nothing. Each
    expectation is checked against the library's own request builder, so the
    fake cannot drift from what ends up on the socket."""
    for args, kwargs in [
        (("bed", -18.0), {}),  # dB in the multiplier slot
        (("bed",), {"vol_db": -18.0}),
        (("bed",), {"vol_mul": 0.5}),
        (("bed", 0.5, -6.0), {}),  # both: nothing picks one
        (("bed",), {}),  # neither: nothing complains
    ]:
        client = FakeClient()
        client.set_input_volume(*args, **kwargs)
        request, data = wire("set_input_volume", *args, **kwargs)
        assert request == "SetInputVolume"
        assert client.volumes == [data], (args, kwargs)

    client = FakeClient()
    client.set_input_volume("bed", -18.0)
    assert client.volumes == [
        {"inputName": "bed", "inputVolumeMul": -18.0, "inputVolumeDb": None}
    ]


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
    ids = {name: _item_id(client, "chart-focus", name) for name in order}
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
            item_id = _item_id(client, spec["scene"], src["name"])
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


def test_status_surfaces_the_reconnect_signals():
    # KI-021: OBS keeps output_active TRUE across an RTMP reconnect, so these
    # two fields are the only way anything outside OBS can learn it happened.
    client = FakeClient(streaming=True, skipped=399, total=58_031,
                        reconnecting=True, congestion=0.42)
    status = stream_ctl.get_status(client)
    assert status["reconnecting"] is True
    assert status["congestion"] == 0.42
    assert status["streaming"] is True   # the whole problem, in one assertion


def _bed_placed_in_one_scene():
    """OBS where `audio-bed` exists but has only ever been placed in one scene."""
    client = FakeClient(scenes=("chart-focus", "world-focus"))
    client.create_input("chart-focus", "audio-bed", "ffmpeg_source", {}, True)
    client.calls.clear()
    return client


def test_a_shared_input_is_added_to_a_scene_it_is_missing_from():
    """One bed in four scenes means three of them need a scene item created,
    not a second input — four inputs is four players."""
    client = _bed_placed_in_one_scene()
    home = _item_id(client, "chart-focus", "audio-bed")
    assert stream_ctl._scene_item_id(client, "chart-focus", "audio-bed", True) == home
    assert client.calls == []  # already there: nothing to create

    placed = stream_ctl._scene_item_id(client, "world-focus", "audio-bed", True)
    assert client.calls == [("create_scene_item", "world-focus", "audio-bed")]
    assert placed == _item_id(client, "world-focus", "audio-bed")
    assert _stacking(client, "world-focus") == ["audio-bed"]
    assert [i["inputName"] for i in client.get_input_list().inputs] == ["audio-bed"]


def test_a_missing_non_shared_input_still_raises():
    """The 'each scene owns its inputs' invariant holds everywhere else — a
    browser source missing from its own scene is a bug, not a thing to paper
    over. It surfaces as the real client's `OBSSDKRequestError`."""
    client = _bed_placed_in_one_scene()
    with pytest.raises(OBSSDKRequestError):
        stream_ctl._scene_item_id(client, "world-focus", "audio-bed", shared=False)
    assert _stacking(client, "world-focus") == []


def test_the_fake_will_not_resolve_a_source_that_is_not_in_the_scene():
    """The F1 divergence itself. Real OBS answers `GetSceneItemId` for a source
    that lives in another scene with ResourceNotFound, and the library raises it
    (`ReqClient.send`). A fake that invents an id here is what let `build_scene`
    put sources in the wrong scene and stay green."""
    client = FakeClient(scenes=("chart-focus", "world-focus"))
    client.create_input("chart-focus", "charts-1m", "browser_source", {}, True)
    assert _item_id(client, "chart-focus", "charts-1m") == 1
    with pytest.raises(OBSSDKRequestError) as miss:
        client.get_scene_item_id("world-focus", "charts-1m")
    assert miss.value.code == 600
    with pytest.raises(OBSSDKRequestError):
        client.get_scene_item_id("no-such-scene", "charts-1m")


def test_the_fake_numbers_scene_items_per_scene_from_one():
    client = FakeClient(scenes=("a", "b"))
    for name in ("x", "y"):
        client.create_input("a", name, "browser_source", {}, True)
    client.create_input("b", "z", "browser_source", {}, True)
    client.create_scene_item("b", "x")
    assert [_item_id(client, "a", n) for n in ("x", "y")] == [1, 2]
    assert [_item_id(client, "b", n) for n in ("z", "x")] == [1, 2]


def test_the_fake_refuses_a_second_source_of_the_same_name():
    """Scenes and inputs share one namespace in OBS."""
    client = FakeClient(scenes=("a",))
    client.create_input("a", "x", "browser_source", {}, True)
    with pytest.raises(OBSSDKRequestError) as dup:
        client.create_input("a", "x", "browser_source", {}, True)
    assert dup.value.code == 601
    with pytest.raises(OBSSDKRequestError):
        client.create_scene("x")
    with pytest.raises(OBSSDKRequestError):
        client.create_input("a", "a", "browser_source", {}, True)


def test_the_fake_takes_the_arguments_the_real_client_takes():
    """Both sides of the seam, for every request the fake answers: the same
    parameter names, kinds and defaults as `obsws_python.ReqClient`, and no
    request the real client does not have. `set_input_volume` took dB second
    here while the real one takes a multiplier second, and nothing noticed."""
    assert signature_diffs(FakeClient) == {}


def test_refresh_browser_source_presses_the_browser_refresh_button():
    """obs-websocket has no first-class "reload input" request: the browser
    source's refresh lives as a button in its properties, and pressing it is the
    whole recovery (KI-046)."""
    client = FakeClient()
    stream_ctl.refresh_browser_source(client, "world-room")
    assert client.calls == [
        ("press_input_properties_button", "world-room", "refreshnocache")
    ]


def test_the_refresh_button_is_pressed_the_way_the_real_client_takes_it():
    """Both sides of the seam, pinned. A fake laxer than the thing it stands in
    for is how the A4 seam bugs stayed green — and `refreshnocache` is a string
    obs-browser owns, not one this project may choose."""
    import inspect

    obsws = pytest.importorskip("obsws_python")
    sig = inspect.signature(obsws.ReqClient.press_input_properties_button)
    assert list(sig.parameters) == ["self", "input_name", "prop_name"]
