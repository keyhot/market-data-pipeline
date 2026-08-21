"""KI-032: the `dropped_frames` rule did not read dropped frames.

`stream_ctl.get_status` builds `dropped_ratio` from `output_skipped_frames` —
OBS's **encoder-lag** counter. The "dropped due to insufficient
bandwidth/connection stalls" figure OBS prints in its own log is a *different*
counter that obs-websocket v5 never exposes. So the rule, its `reason` payload,
its threshold comment and CLAUDE.md all named a network fault and measured an
encoding one.

The rule was never useless — a sustained encoder-skip ratio is a real problem,
and "degraded, not downtime" is still the right handling. It was *misnamed*,
and a misnamed metric gets read as evidence of something it cannot see, which
this tracker calls its most repeated failure shape.

Fixed by saying what each number is: the skip rule reports
`encoder_overloaded`, and `output_congestion` — which OBS *does* derive from
real RTMP drops — gets its own `network_congested` rule. Both are degraded, not
downtime.

The load-bearing test here is the backward-compatible one. `world_events` is
append-only, so every historical row keeps `reason: "dropped_frames"` forever.
If the degraded set stopped accepting it, every past degradation would
silently reclassify as *downtime* and retroactively change the uptime numbers —
fixing a truthfulness bug by corrupting the record it is measured against.
"""

import pytest

from scripts import stream_watchdog
from scripts.stream_watchdog import WatchdogConfig, WatchdogState, tick
from world.state import STREAM_DEGRADED_REASONS, is_degraded_stream_event

CFG = WatchdogConfig()


def _live(**kw):
    probe = {
        "reachable": True,
        "streaming": True,
        "dropped_ratio": 0.0,
        "congestion": 0.0,
        "total_frames": 100_000,
        "skipped_frames": 0,
    }
    probe.update(kw)
    return probe


def _records(actions):
    return [a for a in actions if a[0] == "record"]


def _live_state():
    return WatchdogState(
        obs_up=True, streaming=True, last_total_frames=99_000, content_ok=True
    )


# --- the record must not be rewritten underneath us -------------------------


def test_the_historical_reason_is_still_degraded():
    """Append-only: every row written before this change says `dropped_frames`.
    Dropping it from the degraded set would turn past degradations into past
    *outages* and silently restate the uptime history."""
    assert "dropped_frames" in STREAM_DEGRADED_REASONS
    assert is_degraded_stream_event("stream_dropped", {"reason": "dropped_frames"})


def test_content_unreachable_is_still_not_degraded():
    """KI-024's line stays where it is: a stream in front of a dead stack is
    real downtime, not a degradation."""
    assert not is_degraded_stream_event(
        "stream_dropped", {"reason": "content_unreachable"}
    )


# --- the encoder rule, honestly named ---------------------------------------


def test_the_skip_rule_reports_an_encoder_fault_not_a_network_one():
    probe = _live(dropped_ratio=0.09, total_frames=100_000, congestion=0.0)

    _, actions = tick(probe, _live_state(), CFG, 1000.0)

    records = _records(actions)
    assert len(records) == 1
    assert records[0][2]["reason"] == "encoder_overloaded", (
        "the skip-ratio rule still claims to be a network fault (KI-032)"
    )
    assert is_degraded_stream_event("stream_dropped", records[0][2]), (
        "an encoder overload is degraded, not downtime"
    )


def test_the_encoder_rule_still_carries_the_network_signal_alongside():
    """congestion rides along so the two are always comparable in one row."""
    probe = _live(dropped_ratio=0.09, congestion=0.42)

    _, actions = tick(probe, _live_state(), CFG, 1000.0)

    payload = _records(actions)[0][2]
    assert payload["congestion"] == pytest.approx(0.42)


# --- the network rule, which is new ------------------------------------------


def test_sustained_congestion_is_recorded_as_a_network_fault():
    """The signal the old rule claimed to have and never did."""
    probe = _live(dropped_ratio=0.0, congestion=0.8)

    _, actions = tick(probe, _live_state(), CFG, 1000.0)

    records = _records(actions)
    assert len(records) == 1, "real RTMP congestion went unrecorded (KI-032)"
    assert records[0][2]["reason"] == "network_congested"
    assert records[0][2]["congestion"] == pytest.approx(0.8)
    assert is_degraded_stream_event("stream_dropped", records[0][2])


def test_congestion_never_triggers_a_restart():
    """Same handling as the encoder rule: a restart makes congestion worse."""
    probe = _live(congestion=0.9)

    _, actions = tick(probe, _live_state(), CFG, 1000.0)

    assert not [a for a in actions if a[0] in ("restart_obs", "launch_obs")]


def test_congestion_is_flagged_once_not_every_poll():
    state = _live_state()
    probe = _live(congestion=0.8)

    state, first = tick(probe, state, CFG, 1000.0)
    state, second = tick(probe, state, CFG, 1030.0)

    assert len(_records(first)) == 1
    assert _records(second) == [], "re-recorded the same ongoing congestion"


def test_congestion_rearms_once_it_clears():
    state = _live_state()

    state, _ = tick(_live(congestion=0.8), state, CFG, 1000.0)
    state, _ = tick(_live(congestion=0.0), state, CFG, 1030.0)
    state, again = tick(_live(congestion=0.8), state, CFG, 1060.0)

    assert len(_records(again)) == 1, "a second, separate congestion went unrecorded"


def test_calm_congestion_records_nothing():
    _, actions = tick(_live(congestion=0.05), _live_state(), CFG, 1000.0)

    assert _records(actions) == []


def test_the_two_faults_are_reported_separately_when_both_fire():
    """They fail independently — an overloaded encoder on a clean link, and a
    clean encoder on a congested one, are different problems."""
    probe = _live(dropped_ratio=0.09, congestion=0.8)

    _, actions = tick(probe, _live_state(), CFG, 1000.0)

    reasons = {r[2]["reason"] for r in _records(actions)}
    assert reasons == {"encoder_overloaded", "network_congested"}
