"""A 24/7 autonomous director needs brakes that don't require a redeploy:
per-minute switch/line budgets (sliding 60s window), a global mute, and
counters. Budgets are enforced in the runner's _apply, keyed off tick's
proposed action — a runaway tick can't flap OBS or spam TTS."""

from datetime import datetime, timedelta, timezone

from director.policy import (
    DirectorAction,
    DirectorConfig,
    DirectorMetrics,
    DirectorState,
    within_line_budget,
    within_switch_budget,
)
from director.service import _apply, director_muted

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class _FakeObs:
    def __init__(self):
        self.scene = None

    def set_current_program_scene(self, name):
        self.scene = name


def _ds(**kw):
    base = {"current_scene": "chart-focus", "last_switch": BASE - timedelta(minutes=5)}
    base.update(kw)
    return DirectorState(**base)


def test_switch_budget_caps_within_the_minute():
    cfg = DirectorConfig(max_switches_per_minute=2)
    over = _ds(
        recent_switch_times=[BASE - timedelta(seconds=10), BASE - timedelta(seconds=20)]
    )
    assert within_switch_budget(over, BASE, cfg) is False  # 2 in last 60s, cap 2
    under = _ds(recent_switch_times=[BASE - timedelta(seconds=10)])
    assert within_switch_budget(under, BASE, cfg) is True


def test_old_switches_fall_out_of_the_window():
    cfg = DirectorConfig(max_switches_per_minute=2)
    ds = _ds(
        recent_switch_times=[
            BASE - timedelta(seconds=90),
            BASE - timedelta(seconds=120),
        ]
    )
    assert within_switch_budget(ds, BASE, cfg) is True  # both older than 60s


def test_line_budget_caps_within_the_minute():
    cfg = DirectorConfig(max_lines_per_minute=1)
    ds = _ds(recent_line_times=[BASE - timedelta(seconds=5)])
    assert within_line_budget(ds, BASE, cfg) is False


def test_apply_suppresses_switch_over_budget():
    cfg = DirectorConfig(max_switches_per_minute=1)
    ds = _ds(recent_switch_times=[BASE - timedelta(seconds=10)])
    m = DirectorMetrics()
    _apply(
        DirectorAction(scene="world-focus"),
        ds,
        BASE,
        _FakeObs(),
        lambda *a: True,
        lambda e: None,
        cfg,
        m,
    )
    assert ds.current_scene == "chart-focus"  # held — over budget
    assert m.switches_suppressed == 1 and m.scene_switches == 0


def test_apply_switches_when_within_budget():
    cfg = DirectorConfig()
    ds = _ds()
    obs, m = _FakeObs(), DirectorMetrics()
    _apply(
        DirectorAction(scene="world-focus"),
        ds,
        BASE,
        obs,
        lambda *a: True,
        lambda e: None,
        cfg,
        m,
    )
    assert ds.current_scene == "world-focus" and obs.scene == "world-focus"
    assert m.scene_switches == 1


def test_apply_caps_lines_and_counts_suppressed():
    cfg = DirectorConfig(max_lines_per_minute=1)
    ds = _ds(last_switch=BASE)
    m = DirectorMetrics()
    lines = [
        {"character": "optimist", "text": "a", "voice": "v", "event_id": 1},
        {"character": "anxious", "text": "b", "voice": "v", "event_id": 2},
    ]
    _apply(
        DirectorAction(lines=lines),
        ds,
        BASE,
        _FakeObs(),
        lambda *a: True,
        lambda e: None,
        cfg,
        m,
    )
    assert m.lines_spoken == 1 and m.lines_suppressed == 1


def test_tts_failure_is_counted_but_line_still_recorded():
    cfg = DirectorConfig()
    ds = _ds(last_switch=BASE)
    m = DirectorMetrics()
    lines = [{"character": "optimist", "text": "a", "voice": "v", "event_id": 1}]
    recorded = []
    _apply(
        DirectorAction(lines=lines),
        ds,
        BASE,
        _FakeObs(),
        lambda *a: False,
        recorded.extend,
        cfg,
        m,
    )
    assert m.tts_failures == 1 and m.lines_spoken == 1
    assert recorded and recorded[0]["event_type"] == "commentary_spoken"


def test_director_muted_env(monkeypatch):
    monkeypatch.delenv("DIRECTOR_MUTED", raising=False)
    assert director_muted() is False
    monkeypatch.setenv("DIRECTOR_MUTED", "1")
    assert director_muted() is True
