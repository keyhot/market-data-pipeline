"""End-to-end director chain with fakes throughout — world state -> tick ->
scene + lines, driving the REAL choose_scene / lines_for_tick / personalities /
phrases, plus a burst that must not flap scenes or spam lines. No OBS, Piper,
Postgres, or network. (In tests/unit/ so it always runs — it has no DB dep,
unlike tests/integration/ which auto-skips without Postgres.)"""

from datetime import datetime, timedelta, timezone

from director.policy import DirectorConfig, DirectorMetrics, DirectorState, tick
from director.service import _apply

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class _FakeObs:
    def __init__(self):
        self.scene = None

    def set_current_program_scene(self, name):
        self.scene = name


def _ev(id_, event_type, tier, payload, symbol="BTCUSDT"):
    return {
        "id": id_,
        "event_type": event_type,
        "tier": tier,
        "symbol": symbol,
        "occurred_at": BASE.isoformat(),
        "payload": payload,
    }


def test_full_chain_win_switches_to_world_and_speaks():
    state = {
        "recent": [_ev(1, "signal_resolved", 3, {"outcome": "win"})],
        "symbols": {},
        "stream": {"state": "live"},
    }
    ds = DirectorState(
        current_scene="chart-focus", last_switch=BASE - timedelta(minutes=5)
    )
    action = tick(state, ds, BASE, DirectorConfig())
    assert action.scene == "world-focus"  # a model event -> world-focus
    chars = {ln["character"] for ln in action.lines}
    assert "optimist" in chars and "statistician" in chars
    assert "anxious" not in chars  # it's a win — the anxious one stays quiet


def test_burst_does_not_flap_scenes_or_spam_lines():
    # Short dwell so the per-minute BUDGET is the binding constraint, not dwell.
    cfg = DirectorConfig(
        min_dwell_seconds=1, max_switches_per_minute=2, max_lines_per_minute=3
    )
    ds = DirectorState(
        current_scene="chart-focus", last_switch=BASE - timedelta(minutes=5)
    )
    metrics = DirectorMetrics()
    obs = _FakeObs()
    recorded = []
    # Alternating dramatic market/model events every 5s across ~60s.
    for i in range(12):
        now = BASE + timedelta(seconds=5 * i)
        if i % 2 == 0:
            ev = _ev(100 + i, "big_move", 3, {"sigmas": 9.0})
        else:
            ev = _ev(100 + i, "signal_resolved", 3, {"outcome": "loss"})
        state = {"recent": [ev], "symbols": {}, "stream": {"state": "live"}}
        action = tick(state, ds, now, cfg)
        _apply(action, ds, now, obs, lambda *a: True, recorded.extend, cfg, metrics)
        ds.last_seen_event_id = 100 + i  # advance like the runner does

    # Dwell + budget held together: no seizure-inducing flapping / no spam.
    assert metrics.scene_switches <= cfg.max_switches_per_minute
    assert metrics.switches_suppressed >= 1  # the burst pushed past the switch budget
    assert metrics.lines_spoken <= cfg.max_lines_per_minute
    assert metrics.lines_suppressed >= 1  # and past the line budget
    assert obs.scene in ("chart-focus", "world-focus", "event-focus")
    # every recorded line links back to a real event id (truthfulness)
    for e in recorded:
        if e["event_type"] == "commentary_spoken":
            assert e["payload"]["event_id"] is not None
