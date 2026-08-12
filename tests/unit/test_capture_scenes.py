"""The visual QA harness, against a fake OBS client and a fake event feed —
the harness exists to photograph a live stream, so nothing here may touch one."""

import pytest

from scripts import capture_scenes


class FakeResp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    def __init__(self, scene="chart-focus", streaming=False):
        self.calls = []
        self.scene = scene
        self.streaming = streaming

    def get_current_program_scene(self):
        return FakeResp(current_program_scene_name=self.scene)

    def set_current_program_scene(self, name):
        self.calls.append(("switch", name))
        self.scene = name

    def get_stream_status(self):
        return FakeResp(output_active=self.streaming)

    def save_source_screenshot(self, scene, fmt, path, w, h, quality):
        self.calls.append(("shot", scene, str(path)))


def shots(client):
    return [c for c in client.calls if c[0] == "shot"]


def switches(client):
    return [c[1] for c in client.calls if c[0] == "switch"]


# --- the harness must give the wheel back ------------------------------------


def test_plan_visits_every_scene_and_returns_to_where_it_started():
    """The director owns the scene while the stream is healthy (B10). A harness
    that wandered off and left the stream on whatever it captured last would be
    a self-inflicted outage of exactly the kind KI-018 was."""
    plan = capture_scenes.plan(["a", "b", "c"], current="b")
    assert plan == ["a", "b", "c", "b"]


def test_plan_without_a_known_current_scene_does_not_invent_a_restore():
    assert capture_scenes.plan(["a", "b"], current=None) == ["a", "b"]


def test_the_standby_card_is_never_put_on_air_during_a_healthy_stream():
    """`standby` says "reconnecting…". Cycling it in front of viewers to take a
    screenshot, while the stream is fine, is a small lie about the system's own
    state — the same rule that keeps `dropped_frames` out of the downtime
    number. Against an idle OBS it captures normally."""
    scenes = ["chart-focus", "world-focus", "standby"]
    assert capture_scenes.honest_scenes(scenes, streaming=True) == [
        "chart-focus",
        "world-focus",
    ]
    assert capture_scenes.honest_scenes(scenes, streaming=False) == scenes


def test_capturing_a_live_stream_needs_explicit_consent():
    """Switching scenes on air is visible to every viewer, so it is opt-in and
    never a default — the harness is normally run against an idle OBS."""
    with pytest.raises(capture_scenes.WouldDisturbTheStream):
        capture_scenes.guard_live(streaming=True, take_control=False)
    capture_scenes.guard_live(streaming=True, take_control=True)
    capture_scenes.guard_live(streaming=False, take_control=False)


def test_calm_capture_shoots_every_scene_and_restores(tmp_path):
    client = FakeClient(scene="world-focus")
    slept = []
    written = capture_scenes.capture_calm(
        client,
        ["chart-focus", "world-focus"],
        tmp_path,
        take_control=False,
        sleeper=slept.append,
    )
    assert switches(client) == ["chart-focus", "world-focus", "world-focus"]
    assert len(shots(client)) == 2, "the restore leg must not shoot a third frame"
    assert [p.name for p in written] == [
        "calm-chart-focus.png",
        "calm-world-focus.png",
    ]
    assert len(slept) == 2, "each scene needs to settle before it is shot"


def test_calm_capture_lets_a_just_activated_scene_catch_up(tmp_path):
    """OBS only ticks sources in the active scene, so everything on a scene we
    just switched to has been frozen — possibly since boot. Shooting straight
    away photographs that stale frame; it is the same fact that made per-source
    screenshots of inactive scenes come back blank white."""
    client = FakeClient(scene="chart-focus")
    order = []
    capture_scenes.capture_calm(
        client,
        ["world-focus"],
        tmp_path,
        sleeper=lambda _: order.append("settle"),
    )
    assert order == ["settle"]
    assert switches(client)[0] == "world-focus"
    # the settle happens between the switch and the shot, not after it
    assert client.calls[0] == ("switch", "world-focus")
    assert client.calls[1][0] == "shot"


def test_calm_capture_refuses_a_live_stream_without_consent(tmp_path):
    client = FakeClient(streaming=True)
    with pytest.raises(capture_scenes.WouldDisturbTheStream):
        capture_scenes.capture_calm(client, ["chart-focus"], tmp_path)
    assert client.calls == []


# --- swell: photograph a real event, never a staged one ----------------------


def test_swell_waits_for_a_real_event_at_or_above_the_tier(tmp_path):
    """The plan floated injecting a synthetic high-tier event. That would write
    a fake row into an append-only log whose whole point is that it is true —
    so the harness waits for a real one instead."""
    slept = []
    events = [
        {"event_type": "signal_resolved", "severity": 0.2},   # tier 0, ignored
        {"event_type": "signal_resolved", "severity": 1.3},   # tier 2, fires
        {"event_type": "signal_resolved", "severity": 1.9},   # never reached
    ]
    written = capture_scenes.capture_swell(
        FakeClient(), events, tmp_path, min_tier=1, shots=3, sleeper=slept.append
    )
    assert len(written) == 3
    assert [p.name for p in written] == [
        "swell-signal_resolved-0.png",
        "swell-signal_resolved-1.png",
        "swell-signal_resolved-2.png",
    ]
    assert len(slept) == 3, "the burst must space its frames across the animation"


def test_swell_gives_up_rather_than_hanging_when_nothing_fires(tmp_path):
    written = capture_scenes.capture_swell(
        FakeClient(), [], tmp_path, min_tier=1, shots=2, sleeper=lambda _: None
    )
    assert written == []


def test_the_harness_reads_tier_from_the_one_scale(tmp_path):
    """KI-019: the pages each invented an absolute 2/5/8 scale and pinned every
    `signal_resolved` to tier 0. A harness with its own copy would decide the
    swell never happened and quietly photograph nothing."""
    from world.state import severity_tier

    event = {"event_type": "signal_resolved", "severity": 0.7086}
    assert capture_scenes.tier_of(event) == severity_tier(*event.values()) == 1
    assert capture_scenes.tier_of({"event_type": "big_move", "severity": 0.7}) == 0


def test_scene_names_come_from_the_scene_spec():
    """A hard-coded list here would silently skip a scene added later — the
    standby card was exactly such an addition (B10)."""
    from scripts.stream_scene import scenes_spec

    assert capture_scenes.default_scenes() == [s["scene"] for s in scenes_spec()]
