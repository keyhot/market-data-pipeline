"""Director decision layer is a pure function of world state + director state —
no OBS, no Piper, no clock, no DB. These tests pin that purity."""

from datetime import datetime, timezone

from director.policy import DirectorConfig, DirectorState, tick

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _state(**kw):
    base = {
        "recent": [],
        "symbols": {},
        "model": {},
        "trader": None,
        "stream": {"state": "live"},
        "history": {},
    }
    base.update(kw)
    return base


def test_tick_is_pure_no_side_effects():
    cfg = DirectorConfig()
    ds = DirectorState(current_scene="chart-focus", last_switch=BASE)
    a1 = tick(_state(), ds, BASE, cfg)
    a2 = tick(_state(), ds, BASE, cfg)
    assert a1 == a2  # same inputs -> same action
    assert ds.current_scene == "chart-focus"  # tick mutates nothing on dir_state


def test_tick_returns_no_scene_change_when_nothing_salient():
    cfg = DirectorConfig()
    ds = DirectorState(current_scene="chart-focus", last_switch=BASE)
    action = tick(_state(), ds, BASE, cfg)
    assert action.scene in (None, "chart-focus")
    assert action.lines == []


def test_muted_director_emits_no_lines():
    cfg = DirectorConfig()
    ds = DirectorState(current_scene="chart-focus", last_switch=BASE, muted=True)
    action = tick(_state(), ds, BASE, cfg)
    assert action.lines == []
