"""The director must not take the program back from the standby card (B10 -> B8).

B10's watchdog switches to `standby` on a genuine drop and hands the scene back
on recovery — **transition-only**, exactly one switch each way, because the
director owns the scene while the stream is healthy. But "the stream is down"
does not mean "OBS is unreachable": the card exists for the case where OBS is
answering and the *output* is not. So the director keeps ticking, keeps seeing
salient events, and can switch away from the card mid-outage — replacing the
one surface built to be honest about the outage with a room whose numbers are
frozen behind it.

Two halves, and the bug needs both to be fixed:

  * the policy must **hold** a scene it does not own, rather than treating the
    card as somewhere to decay away from;
  * the runner must **look** at what is on air each tick, or it never learns the
    watchdog moved the program at all — `dir_state.current_scene` is otherwise
    only ever written by the director's own switches (KI-034, one restart later).
"""

from datetime import datetime, timedelta, timezone

import pytest

from director import service
from director.policy import DirectorAction, DirectorConfig, DirectorState
from director.scenes import DIRECTOR_SCENES, choose_scene
from scripts.stream_scene import SCENE_STANDBY, scenes_spec

BASE = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def _ev(event_type, tier):
    return {
        "id": 1,
        "event_type": event_type,
        "tier": tier,
        "symbol": "BTCUSDT",
        "occurred_at": BASE.isoformat(),
        "payload": {},
    }


def _state(recent):
    return {"recent": recent, "symbols": {}, "stream": {"state": "down"}}


def test_a_salient_event_does_not_pull_the_program_off_the_standby_card():
    ds = DirectorState(
        current_scene=SCENE_STANDBY, last_switch=BASE - timedelta(minutes=5)
    )
    scene = choose_scene(_state([_ev("big_move", 3)]), ds, BASE, DirectorConfig())
    assert scene == SCENE_STANDBY


def test_the_standby_card_does_not_decay_to_home_either():
    """Decay-to-home is the other way out of a scene, and a long outage is
    exactly when it fires: nothing salient, and the dwell timer running."""
    ds = DirectorState(
        current_scene=SCENE_STANDBY, last_switch=BASE - timedelta(minutes=30)
    )
    assert choose_scene(_state([]), ds, BASE, DirectorConfig()) == SCENE_STANDBY


def test_every_built_scene_belongs_to_the_director_or_the_watchdog():
    """A scene added to the spec without a decision about who owns it defaults
    to nobody holding it — the registry-invariant shape used for reactions and
    headlines, applied to scene ownership."""
    built = {spec["scene"] for spec in scenes_spec()}
    assert DIRECTOR_SCENES | {SCENE_STANDBY} == built
    assert SCENE_STANDBY not in DIRECTOR_SCENES
    assert DirectorConfig().home_scene in DIRECTOR_SCENES


class FakeObs:
    def __init__(self, scene):
        self._scene = scene
        self.switched_to = []

    def get_current_program_scene(self):
        return type("R", (), {"current_program_scene_name": self._scene})()

    def set_current_program_scene(self, name):
        self.switched_to.append(name)
        self._scene = name


@pytest.fixture(autouse=True)
def restore_tick():
    original = service.tick
    yield
    service.tick = original


def _run(obs, decide, max_ticks=1):
    recorded = []
    service.tick = decide
    service.run(
        fetch_state=lambda: {"recent": []},
        obs_client=obs,
        tts_runner=None,
        record_event=recorded.append,
        sleep_seconds=0,
        max_ticks=max_ticks,
    )
    return recorded


def test_the_runner_notices_the_watchdog_moved_the_program():
    """The director started on chart-focus; the watchdog put the card up while
    it was running. Without re-reading, the policy is asked about a scene the
    program left minutes ago."""
    obs = FakeObs(scene="chart-focus")
    seen = []

    def decide(state, dir_state, now, config):
        seen.append(dir_state.current_scene)
        obs._scene = SCENE_STANDBY  # the watchdog, between two director ticks
        return DirectorAction(scene=None, lines=[])

    _run(obs, decide, max_ticks=2)
    assert seen == ["chart-focus", SCENE_STANDBY]


def test_the_director_resumes_once_the_watchdog_hands_the_scene_back():
    """The hand-back is the watchdog switching to chart-focus. From there the
    director owns the program again and an event moves it normally."""
    obs = FakeObs(scene=SCENE_STANDBY)

    def decide(state, dir_state, now, config):
        if dir_state.current_scene == SCENE_STANDBY:
            obs._scene = "chart-focus"  # watchdog hands back
            return DirectorAction(scene=None, lines=[])
        return DirectorAction(scene="world-focus", lines=[])

    _run(obs, decide, max_ticks=2)
    assert obs.switched_to == ["world-focus"]
