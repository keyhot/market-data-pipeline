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

import logging

from director import service
from director.policy import DirectorAction, DirectorConfig, DirectorState


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
    def __init__(self, fail_switch=0):
        self._scene = "chart-focus"
        self.switches = []
        self.switch_attempts = 0
        self.fail_switch = fail_switch  # raise on the first N switch attempts; -1 = always

    def get_current_program_scene(self):
        return type("R", (), {"current_program_scene_name": self._scene})()

    def set_current_program_scene(self, name):
        self.switch_attempts += 1
        if self.fail_switch < 0 or self.switch_attempts <= self.fail_switch:
            raise RuntimeError("obs blew up")
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
        if self.fail_on == "always" or self.calls == self.fail_on:
            raise RuntimeError("tts blew up")
        self.spoken.append(text)
        return True


def _run(states, tts, record=None, ticks=None, obs=None, fetch=None):
    recorded = []
    seq = iter(states)
    last = states[-1]
    obs = obs or _Obs()
    rc = service.run(
        fetch_state=fetch or (lambda: next(seq, last)),
        obs_client=obs,
        tts_runner=tts,
        record_event=record or recorded.append,
        config=DirectorConfig(max_lines_per_minute=100),  # the budget must not mask a repeat
        sleep_seconds=0,
        max_ticks=ticks or len(states),
    )
    _run.rc = rc
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


def test_a_new_event_after_a_failed_tick_is_still_spoken_once():
    """The mark moved past the failed tick's events, not past the future: a
    later event is narrated normally, and exactly once."""
    later = {"recent": NEW["recent"] + [_event(4, "XRPUSDT")]}
    tts = _Tts(fail_on=1)
    _run([SEED, NEW, later, later], tts)

    # 1 call that raised (events 1-3) + the lines for event 4 only, once
    ev4_lines = _Tts()
    _run([{"recent": [_event(3, "SOLUSDT")]}, later], ev4_lines)
    assert tts.calls == 1 + ev4_lines.calls, (tts.calls, ev4_lines.calls)
    assert ev4_lines.calls >= 1


# --- Revision 2026-11-23 (owner: "narrow it"): the mark moves after the OBS
# switch block and before the first line, so a transient switch error is retried
# (nothing was spoken yet) while a line that may have been spoken is never repeated.


def test_an_obs_switch_error_speaks_nothing_and_the_next_tick_retries_once():
    """F1/F3. switch_scene raises on the first tick: no line is spoken, the mark
    has not moved, so the same window is retried; the retry switches and speaks
    each line exactly once (and a third offer of the window repeats nothing)."""
    lines = _happy_line_count()
    tts = _Tts()
    obs = _Obs(fail_switch=1)
    _, recorded = _run([SEED, NEW, NEW, NEW], tts, obs=obs)

    assert obs.switch_attempts == 2, "tick 1 fails, tick 2 retries, tick 3 has nothing new"
    assert len(obs.switches) == 1, obs.switches
    assert tts.calls == lines, f"{tts.calls} runner calls, expected {lines}"
    types = [e["event_type"] for batch in recorded for e in batch]
    assert types.count("scene_switched") == 1 and types.count("commentary_spoken") == lines, types


def test_an_obs_switch_error_does_not_speak_before_the_failure():
    """Nothing is spoken on the tick whose switch failed."""
    tts = _Tts()
    _run([SEED, NEW], tts, obs=_Obs(fail_switch=-1))
    assert tts.calls == 0, tts.spoken


def _apply_args(dir_state, obs, tts, lines, scene="main-stage"):
    from datetime import datetime, timezone

    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    action = DirectorAction(scene=scene, lines=lines)
    return (action, dir_state, now, obs, tts, lambda events: None, DirectorConfig(), service.DirectorMetrics())


def _line(i):
    return {"character": "A", "text": f"t{i}", "voice": "v", "event_id": i, "symbol": "BTCUSDT"}


def _state():
    return DirectorState(current_scene="chart-focus", last_switch=None)


def test_mark_position_unmoved_when_the_switch_raises():
    ds = _state()
    ds.last_seen_event_id = 5
    try:
        service._apply(*_apply_args(ds, _Obs(fail_switch=-1), _Tts(), [_line(1)]), seen_through=9)
    except RuntimeError:
        pass
    else:
        raise AssertionError("switch error must propagate")
    assert ds.last_seen_event_id == 5


def test_mark_position_advanced_before_the_first_line_is_spoken():
    ds = _state()
    ds.last_seen_event_id = 5
    seen = []

    def tts(text, voice):
        seen.append(ds.last_seen_event_id)  # what the mark is while line 1 is spoken
        raise RuntimeError("tts blew up")

    try:
        service._apply(*_apply_args(ds, _Obs(), tts, [_line(1), _line(2)]), seen_through=9)
    except RuntimeError:
        pass
    assert seen == [9]
    assert ds.last_seen_event_id == 9


def test_mark_position_advanced_even_when_there_are_no_lines():
    ds = _state()
    ds.last_seen_event_id = 5
    service._apply(*_apply_args(ds, _Obs(), _Tts(), [], scene=None), seen_through=9)
    assert ds.last_seen_event_id == 9


def test_mark_position_never_moves_backwards():
    ds = _state()
    ds.last_seen_event_id = 12
    service._apply(*_apply_args(ds, _Obs(), _Tts(), [], scene=None), seen_through=9)
    assert ds.last_seen_event_id == 12


def test_sustained_effect_failures_still_exit_with_code_2():
    """F2. A TTS runner that always raises must still count toward
    MAX_CONSECUTIVE_FAILURES and make `run` return 2 so systemd rebuilds the OBS
    client. Guards `raise` -> `return` in `_apply`'s except block."""
    tts = _Tts(fail_on="always")
    # the first fetch seeds history; every later fetch carries one fresh event
    n = {"i": 0}

    def fetch():
        n["i"] += 1
        return {"recent": [] if n["i"] == 1 else [_event(n["i"], "BTCUSDT")]}

    _run([SEED], tts, fetch=fetch, ticks=service.MAX_CONSECUTIVE_FAILURES + 5)
    assert _run.rc == 2
    assert n["i"] <= service.MAX_CONSECUTIVE_FAILURES + 1  # exited at the threshold, not later


def test_sustained_obs_switch_failures_exit_with_code_2():
    obs = _Obs(fail_switch=-1)
    _run([SEED, NEW], _Tts(), obs=obs, ticks=service.MAX_CONSECUTIVE_FAILURES + 5)
    assert _run.rc == 2
    assert obs.switch_attempts >= service.MAX_CONSECUTIVE_FAILURES - 1


def test_a_failing_partial_recorder_never_masks_the_original_error(caplog):
    """F4. TTS raises on line 2, so a partial batch is handed to the recorder; the
    recorder raises too. run() must survive, log the recorder failure, and the
    tick failure it reports must still be the TTS error, not the recorder's."""

    def bad_record(events):
        if events:
            raise RuntimeError("db down")

    tts = _Tts(fail_on=2)
    with caplog.at_level(logging.WARNING, logger="director.service"):
        _run([SEED, NEW], tts, record=bad_record)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("could not record a partial tick" in m for m in msgs), msgs
    failed = [m for m in msgs if "Director tick failed" in m]
    assert failed and all("tts blew up" in m and "db down" not in m for m in failed), failed
