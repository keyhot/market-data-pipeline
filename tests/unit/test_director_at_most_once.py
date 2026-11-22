"""KI-116 / R7 (audit 7): a failure part-way through `_apply` must not repeat
lines on air.

`last_seen_event_id` used to advance only after `_apply` returned. If anything
raised mid-way (a TTS runner on line 2, say), the lines already spoken were
generated again on the next tick, and the tick's local `events` list, including
the `scene_switched` for a switch OBS had already performed, was lost.

Owner decision Q06 (2026-11-18): at-most-once speech. The mark advances before
`_apply`, so a line that fails is dropped, never repeated (a dropped line beats
a repeated one on air). The rows for what *did* happen are still recorded.
"""

from director import service
from director.policy import DirectorConfig


def _event(event_id, symbol):
    """A real `recent` row, shaped as /world/state emits it (tier 3 so the
    first one also asks for a scene switch)."""
    return {
        "id": event_id,
        "occurred_at": "2026-08-20T23:12:00+00:00",
        "event_type": "signal_resolved",
        "symbol": symbol,
        "severity": 1.8,
        "tier": 3,
        "tier_name": "major",
        "payload": {
            "outcome": "loss",
            "direction": "up",
            "probability": 0.82,
            "horizon_bars": 15,
            "model_version": "20260719-346cadd",
            "realized_return": -0.004,
        },
    }


class _Obs:
    def __init__(self):
        self._scene = "chart-focus"
        self.switches = []

    def get_current_program_scene(self):
        return type("R", (), {"current_program_scene_name": self._scene})()

    def set_current_program_scene(self, name):
        self.switches.append(name)
        self._scene = name


SEED = {"recent": []}  # cold start: seeds nothing, so NEW is all news and the switch lands with the lines
NEW = {"recent": [_event(1, "BTCUSDT"), _event(2, "ETHUSDT"), _event(3, "SOLUSDT")]}


class _Tts:
    """Records each line it is asked to speak; raises on the Nth call."""

    def __init__(self, fail_on=None):
        self.spoken = []
        self.calls = 0
        self.fail_on = fail_on

    def __call__(self, text, voice):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("tts blew up")
        self.spoken.append(text)
        return True


def _run(states, tts, record=None, ticks=None):
    recorded = []
    seq = iter(states)
    last = states[-1]
    obs = _Obs()
    service.run(
        fetch_state=lambda: next(seq, last),
        obs_client=obs,
        tts_runner=tts,
        record_event=record or recorded.append,
        config=DirectorConfig(max_lines_per_minute=100),  # the budget must not mask a repeat
        sleep_seconds=0,
        max_ticks=ticks or len(states),
    )
    return obs, recorded


def _happy_line_count():
    """How many lines the NEW window yields when nothing fails."""
    tts = _Tts()
    _run([SEED, NEW], tts)
    assert tts.calls >= 2, "fixture must yield at least two lines on the new tick"
    return tts.calls


def test_a_tts_failure_on_line_two_does_not_repeat_line_one_next_tick():
    """The P4 repro: spoken used to be ['A', 'A', 'B']. Line 1 is spoken, line 2
    raises, and the same window is offered twice more: nothing is spoken again."""
    _happy_line_count()
    tts = _Tts(fail_on=2)
    _run([SEED, NEW, NEW, NEW], tts)

    assert tts.calls == 2, (
        f"lines were spoken again after a mid-apply failure: {tts.spoken} "
        f"({tts.calls} runner calls)"
    )
    assert len(tts.spoken) == 1


def test_the_tick_that_failed_is_not_retried_at_all():
    """At-most-once: the failing tick's events are consumed. The dropped line
    stays dropped; the TTS runner is not called again for the same events."""
    tts = _Tts(fail_on=1)
    _run([SEED, NEW, NEW, NEW], tts)

    assert tts.calls == 1, f"events of the failed tick were retried: {tts.calls} calls"
    assert tts.spoken == []


def test_the_scene_switch_that_obs_performed_is_still_recorded():
    """Advancing the mark first must not cost the `scene_switched` row: OBS did
    switch, so the log has to say so even though a later line failed. The line
    that was spoken before the failure is recorded too; the failed one is not."""
    tts = _Tts(fail_on=2)
    obs, recorded = _run([SEED, NEW], tts)

    assert obs.switches, "fixture must ask for a switch"
    types = [e["event_type"] for batch in recorded for e in batch]
    assert types == ["scene_switched", "commentary_spoken"], types


def test_a_recorder_failure_does_not_make_the_next_tick_speak_again():
    """`record_event` raising after the lines were spoken used to re-speak them
    next tick (the mark had not moved)."""
    lines = _happy_line_count()
    tts = _Tts()

    def bad_record(events):
        if any(e["event_type"] == "commentary_spoken" for e in events):
            raise RuntimeError("db down")

    _run([SEED, NEW, NEW, NEW], tts, record=bad_record)

    assert tts.calls == lines, f"{tts.calls} runner calls, expected {lines}"


def test_the_happy_path_still_speaks_each_new_event_once_and_records_it():
    lines = _happy_line_count()
    tts = _Tts()
    obs, recorded = _run([SEED, NEW, NEW], tts)

    types = [e["event_type"] for batch in recorded for e in batch]
    assert types.count("commentary_spoken") == tts.calls == lines, types
    assert types.count("scene_switched") == len(obs.switches) == 1, types
