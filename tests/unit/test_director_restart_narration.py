"""KI-033: on restart the director re-narrated the whole `recent` window.

`DirectorState` is rebuilt fresh on every process start, so
`last_seen_event_id` is None, and `lines_for_tick` reads `None or 0` — which
makes *every* event still in `/world/state`'s `recent` window "new". The first
tick after a start therefore re-speaks up to a dozen events the previous
process had already covered.

Proven on air: event 8818 was spoken at 12:28:33 UTC, the unit restarted at
12:49:27, and the same event was spoken again at 12:49:28.88 — 1.4s later, on
the first tick. Event 8960 was spoken 2026-08-14 18:08 and re-announced
2026-08-19 22:27, a **five-day-old** event delivered as news, because the
KI-024 outage meant `recent` still held pre-outage rows on recovery.

KI-020's fix #2 (exit after 12 consecutive failures so systemd rebuilds the
dependencies) is what made restarts routine — 1,689 of them in 7 days — so a
rare glitch became a systematic one.

These tests pin the contract: a director that just booted does not narrate
history, but it does narrate everything that happens after it arrives.
"""


from director import service


def _event(event_id, symbol="BTCUSDT"):
    """A real `recent` row, shaped as /world/state emits it."""
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


def _spoken(recorded):
    """The event_ids the director actually said out loud."""
    return [
        e.get("payload", {}).get("event_id")
        for batch in recorded
        for e in batch
        if e.get("event_type") == "commentary_spoken"
    ]


def _run_capturing(states, max_ticks):
    """Drive the real tick/_apply path (no policy stubs — the seeding lives
    between them) over a scripted sequence of world states."""
    recorded = []
    seq = iter(states)
    last = states[-1]

    def fetch():
        return next(seq, last)

    code = service.run(
        fetch_state=fetch,
        obs_client=None,
        tts_runner=None,
        record_event=recorded.append,
        sleep_seconds=0,
        max_ticks=max_ticks,
    )
    return code, recorded


def test_a_fresh_director_does_not_narrate_what_happened_before_it_started():
    """The bug: every id in the first `recent` window was treated as new."""
    state = {"recent": [_event(i) for i in (8816, 8817, 8818)]}

    _, recorded = _run_capturing([state], max_ticks=1)

    assert _spoken(recorded) == [], (
        "the director narrated events that predate its own process start — "
        "this is the 1.4s-after-restart repeat from KI-033"
    )


def test_it_still_speaks_events_that_arrive_after_it_started():
    """Seeding must not mute the director — only the backlog is skipped."""
    before = {"recent": [_event(8818)]}
    after = {"recent": [_event(8818), _event(8819)]}

    _, recorded = _run_capturing([before, after], max_ticks=2)

    assert 8819 in _spoken(recorded), "a genuinely new event was never spoken"
    assert 8818 not in _spoken(recorded), "the pre-start backlog leaked through"


def test_a_cold_start_with_no_history_still_speaks_the_first_real_event():
    """Empty `recent` (fresh DB) must seed without raising on max() of [] —
    and must not swallow the first event that shows up afterwards."""
    empty = {"recent": []}
    first = {"recent": [_event(1)]}

    code, recorded = _run_capturing([empty, first], max_ticks=2)

    assert code == 0, "an empty first window crashed the loop"
    assert 1 in _spoken(recorded), "the first real event of a cold start was lost"


def test_seeding_happens_once_and_a_later_empty_window_does_not_reset_it():
    """A quiet stretch (`recent` briefly empty) must not re-arm the seed and
    let an already-covered backlog be narrated a second time."""
    states = [
        {"recent": [_event(8818)]},   # tick 1: seeds at 8818, says nothing
        {"recent": []},               # tick 2: quiet — must not re-seed
        {"recent": [_event(8818)]},   # tick 3: same old row returns
    ]

    _, recorded = _run_capturing(states, max_ticks=3)

    assert _spoken(recorded) == [], "a quiet window re-armed the seed (KI-033)"


def test_the_seed_is_the_high_water_mark_not_the_first_row():
    """`recent` is newest-first in /world/state; seeding off the wrong end
    would leave most of the backlog 'new'."""
    state = {"recent": [_event(9619), _event(9618), _event(9600)]}

    _, recorded = _run_capturing([state], max_ticks=1)

    assert _spoken(recorded) == [], "seeded off the wrong end of the window"
