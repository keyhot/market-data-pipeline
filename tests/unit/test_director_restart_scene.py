"""KI-034: on restart the director assumed the home scene instead of looking.

`run()` builds `DirectorState(current_scene=config.home_scene, ...)` — an
assumption, never an observation. OBS keeps running across a director restart,
so the program can be on any scene, and the two disagree from the first tick.

`tick()` returns `scene=scene if scene != dir_state.current_scene else None`,
so the damage runs both ways:

  * OBS is on `world-focus`, the director believes `chart-focus` and decides
    `chart-focus` -> it emits **no switch** and the program sits on the wrong
    scene until some unrelated event moves it;
  * when it does switch, `build_scene_switched(scene, dir_state.current_scene)`
    writes a `from` that never happened into an append-only log.

The log is its own witness: chaining `scene_switched` rows by id, six had a
`from` that disagreed with the previous row's `scene` in 9 days — every one of
them claiming `chart-focus`, and one at 2026-08-14 12:50:29 UTC, 62s after the
12:49:27 restart that also produced the KI-033 duplicate. The *stuck* cases
can't appear in that count at all: they write no event, by construction.

Same root cause family as [[KI-033]] — state rebuilt on restart from
assumptions rather than from the world.
"""

from datetime import datetime, timezone

import pytest

from director import service
from director.policy import DirectorAction, DirectorConfig

NOW = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)


class FakeObs:
    """Minimal OBS seam: reports a program scene and records switches."""

    def __init__(self, scene="world-focus", raises=False):
        self._scene = scene
        self._raises = raises
        self.switched_to = []

    def get_current_program_scene(self):
        if self._raises:
            raise RuntimeError("obs-websocket said no")
        return type("R", (), {"current_program_scene_name": self._scene})()

    def set_current_program_scene(self, name):
        self.switched_to.append(name)
        self._scene = name


def _run(obs, decide, max_ticks=1):
    """Drive the loop with a scripted policy decision, capturing world events."""
    recorded = []
    service.tick = decide  # module-level seam, restored by the fixture below
    code = service.run(
        fetch_state=lambda: {"recent": []},
        obs_client=obs,
        tts_runner=None,
        record_event=recorded.append,
        sleep_seconds=0,
        max_ticks=max_ticks,
    )
    return code, recorded


@pytest.fixture(autouse=True)
def restore_tick():
    original = service.tick
    yield
    service.tick = original


def _switch_events(recorded):
    return [
        e for batch in recorded for e in batch if e.get("event_type") == "scene_switched"
    ]


def test_it_switches_away_from_the_scene_obs_is_really_on():
    """The bug that strands the program: OBS on world-focus, the policy wants
    home, and the director stays silent because it thinks it is already there."""
    obs = FakeObs(scene="world-focus")
    home = DirectorConfig().home_scene

    _run(obs, lambda *a, **k: DirectorAction(scene=home, lines=[]))

    assert obs.switched_to == [home], (
        "the director believed it was already on home and left the program "
        "stranded on world-focus (KI-034)"
    )


def test_it_does_not_re_switch_to_the_scene_already_on_air():
    """The inverse: no redundant switch, and no false event, when OBS is
    already showing what the policy picked."""
    obs = FakeObs(scene="world-focus")

    _, recorded = _run(obs, lambda *a, **k: DirectorAction(scene="world-focus", lines=[]))

    assert obs.switched_to == [], "sent a switch to the scene already on air"
    assert _switch_events(recorded) == [], "recorded a switch that never happened"


def test_a_recorded_switch_names_the_scene_it_actually_came_from():
    """`from` goes into an append-only log; it has to be true."""
    obs = FakeObs(scene="event-focus")

    _, recorded = _run(obs, lambda *a, **k: DirectorAction(scene="world-focus", lines=[]))

    events = _switch_events(recorded)
    assert len(events) == 1
    assert events[0]["payload"]["from"] == "event-focus", (
        "wrote a `from` that never happened into an append-only log (KI-034)"
    )


def test_an_unreadable_scene_falls_back_to_home_without_crashing():
    """OBS answering badly must not stop the director from starting — the
    old assumption is still the safest fallback, it just isn't the default."""
    obs = FakeObs(raises=True)
    home = DirectorConfig().home_scene

    code, _ = _run(obs, lambda *a, **k: DirectorAction(scene=None, lines=[]))

    assert code == 0, "an unreadable program scene took the director down"
    _, recorded2 = _run(
        FakeObs(raises=True), lambda *a, **k: DirectorAction(scene=home, lines=[])
    )
    assert _switch_events(recorded2) == [], "fallback should assume home, as before"


def test_no_obs_client_still_starts_at_home():
    """The unit-test seam (obs_client=None) must keep working."""
    code, recorded = _run(None, lambda *a, **k: DirectorAction(scene=None, lines=[]))

    assert code == 0
    assert _switch_events(recorded) == []
