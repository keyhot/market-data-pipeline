"""KI-020: the director ran inert for 25 hours and logged nothing that said why.

Every tick raised, the loop caught it, the process never exited — so systemd
reported `active` with 0 restarts while the show was over. These tests pin the
three things that were missing: the exception in the log, an exit when failure
is sustained, and a liveness line that a human would actually see.
"""

import logging

import pytest

from director import service
from director.policy import DirectorAction


@pytest.fixture
def quiet_loop(monkeypatch):
    """Stub the decision and its side effects — these tests are about the
    loop's failure handling, not about policy."""
    monkeypatch.setattr(
        service, "tick", lambda *a, **k: DirectorAction(scene=None, lines=[])
    )
    monkeypatch.setattr(service, "_apply", lambda *a, **k: None)


def _run(fetch, **kw):
    return service.run(
        fetch_state=fetch,
        obs_client=None,
        tts_runner=None,
        record_event=lambda events: None,
        sleep_seconds=0,
        **kw,
    )


def test_a_failing_tick_names_the_exception(quiet_loop, caplog):
    """The whole reason the 25h outage cost an hour to diagnose: the exception
    went to `extra={"error": ...}`, which this formatter drops, so the log said
    "Director tick failed" 18,000 times and never once said what failed.
    `record_director_events` two files over already does this right."""
    def boom():
        raise RuntimeError("obs socket is closed")

    with caplog.at_level(logging.WARNING, logger="director.service"):
        _run(boom, max_ticks=1)

    text = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in text, text
    assert "obs socket is closed" in text, text


def test_a_failing_tick_keeps_the_traceback(quiet_loop, caplog):
    with caplog.at_level(logging.WARNING, logger="director.service"):
        _run(lambda: (_ for _ in ()).throw(ValueError("nope")), max_ticks=1)

    assert any(r.exc_info for r in caplog.records), "no traceback attached"


def test_one_hiccup_is_still_swallowed(quiet_loop):
    """"A director hiccup must never take the stream down" stays true — the
    rule was right, it just had no upper bound."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"recent": []}

    assert _run(flaky, max_ticks=4) == 0
    assert len(calls) == 4, "one failure must not stop the loop"


def test_sustained_failure_exits_so_systemd_rebuilds_everything(quiet_loop, caplog):
    """The director already exits 2 when OBS is unreachable *at startup*; this
    extends the same policy to runtime. Restarting rebuilds every dependency,
    including the OBS client that `main` only ever builds once."""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("dead socket")

    with caplog.at_level(logging.ERROR, logger="director.service"):
        code = _run(boom, max_ticks=500)

    assert code == 2
    assert len(calls) == service.MAX_CONSECUTIVE_FAILURES
    assert any("consecutive" in r.getMessage().lower() for r in caplog.records)


def test_intermittent_failures_never_accumulate_to_an_exit(quiet_loop):
    """Failing every other tick for a long time is a flaky dependency, not a
    dead one. The counter has to reset on success or the director would
    restart itself forever over a lossy connection."""
    calls = []

    def flapping():
        calls.append(1)
        if len(calls) % 2:
            raise RuntimeError("flap")
        return {"recent": []}

    ticks = service.MAX_CONSECUTIVE_FAILURES * 6
    assert _run(flapping, max_ticks=ticks) == 0
    assert len(calls) == ticks


def test_the_director_says_it_is_alive_where_someone_will_see_it(quiet_loop, caplog):
    """`systemctl status` said `active` for 25 hours of nothing, and the
    per-tick counters were DEBUG. A periodic INFO line is the signal that would
    have shown a human the show had stopped."""
    with caplog.at_level(logging.INFO, logger="director.service"):
        _run(lambda: {"recent": []}, max_ticks=service.HEARTBEAT_TICKS)

    beats = [r for r in caplog.records if "alive" in r.getMessage().lower()]
    assert beats, "no liveness line in a full heartbeat interval"
    assert "lines" in beats[0].getMessage()


def test_main_propagates_the_give_up_code(monkeypatch):
    """`main` used to `run(...)` and then `return 0` unconditionally. Exiting
    is the entire mechanism — swallowing the code would keep the dead process
    alive and put us straight back in the 25h hole."""
    monkeypatch.setenv("DIRECTOR_ENABLED", "1")
    monkeypatch.setattr(service, "run", lambda **kw: 2)
    monkeypatch.setattr(
        service, "_fetch_world_state", lambda url: {"recent": []}
    )
    from scripts import stream_ctl

    monkeypatch.setattr(stream_ctl, "make_client", lambda *a, **k: object())
    assert service.main() == 2
