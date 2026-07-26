"""Scene selection is a pure policy over world state. The dwell guard (no
flapping under a burst) and decay-to-home (settle back to calm) are the
register's safety-critical pieces — both pinned here, the dwell mutation-checked."""

from datetime import datetime, timedelta, timezone

from director.policy import DirectorConfig, DirectorState
from director.scenes import choose_scene

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _ev(event_type, tier, minute=0, symbol="BTCUSDT"):
    return {
        "id": minute + 1,
        "event_type": event_type,
        "tier": tier,
        "symbol": symbol,
        "occurred_at": (BASE + timedelta(minutes=minute)).isoformat(),
        "payload": {},
    }


def _state(recent):
    return {"recent": recent, "symbols": {}, "stream": {"state": "live"}}


def test_high_tier_market_event_selects_chart_focus():
    ds = DirectorState(
        current_scene="world-focus", last_switch=BASE - timedelta(minutes=5)
    )
    scene = choose_scene(_state([_ev("big_move", 3)]), ds, BASE, DirectorConfig())
    assert scene == "chart-focus"


def test_model_or_trader_event_selects_world_focus():
    ds = DirectorState(
        current_scene="chart-focus", last_switch=BASE - timedelta(minutes=5)
    )
    scene = choose_scene(
        _state([_ev("signal_resolved", 3)]), ds, BASE, DirectorConfig()
    )
    assert scene == "world-focus"


def test_minimum_dwell_blocks_a_switch_before_the_dwell_elapses():
    # A dramatic event arrives 10s after the last switch; dwell is 60s.
    ds = DirectorState(current_scene="world-focus", last_switch=BASE)
    scene = choose_scene(
        _state([_ev("big_move", 3)]), ds, BASE + timedelta(seconds=10), DirectorConfig()
    )
    assert scene == "world-focus"  # held: dwell not elapsed


def test_switch_allowed_after_dwell_elapses():
    ds = DirectorState(current_scene="world-focus", last_switch=BASE)
    scene = choose_scene(
        _state([_ev("big_move", 3)]), ds, BASE + timedelta(seconds=60), DirectorConfig()
    )
    assert scene == "chart-focus"


def test_low_tier_event_does_not_interrupt():
    ds = DirectorState(
        current_scene="chart-focus", last_switch=BASE - timedelta(minutes=5)
    )
    # tier 1 is below the switch threshold — a calm ripple, not a swell.
    scene = choose_scene(_state([_ev("big_move", 1)]), ds, BASE, DirectorConfig())
    assert scene == "chart-focus"


def test_quiet_world_holds_briefly_then_decays_to_home():
    # Recently switched away from home + quiet -> hold (don't snap back instantly).
    ds = DirectorState(current_scene="event-focus", last_switch=BASE)
    assert (
        choose_scene(_state([]), ds, BASE + timedelta(seconds=30), DirectorConfig())
        == "event-focus"
    )
    # Lingered away from home + still quiet -> decay back to the calm home scene.
    assert (
        choose_scene(_state([]), ds, BASE + timedelta(seconds=180), DirectorConfig())
        == "chart-focus"
    )


def test_quiet_world_on_home_scene_stays_home():
    ds = DirectorState(
        current_scene="chart-focus", last_switch=BASE - timedelta(minutes=5)
    )
    assert choose_scene(_state([]), ds, BASE, DirectorConfig()) == "chart-focus"
