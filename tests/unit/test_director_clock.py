"""Q07 + Q05(b): the director loop takes an injectable clock and sleep, and
the boot seed no longer holds the program for a dwell it never served.

`run()` used to seed `last_switch = now`, so for 60 s after every start a
salient event could not switch the scene (and a resumed `world-focus` could not
decay home for 120 s). The owner decided the boot hold is not wanted: the seed
is `now - min_dwell_seconds`. Time is driven by a fake clock whose sleep
advances it, so no test waits and none reads the wall clock.
"""

from datetime import datetime, timedelta, timezone

from director import service
from director.policy import DirectorConfig

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
CFG = DirectorConfig()


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeObs:
    """OBS seam: reports a program scene and records when each switch landed."""

    def __init__(self, clock, scene):
        self._clock = clock
        self._scene = scene
        self.switches = []  # (seconds since T0, scene)

    def get_current_program_scene(self):
        return type("R", (), {"current_program_scene_name": self._scene})()

    def set_current_program_scene(self, name):
        self.switches.append(((self._clock() - T0).total_seconds(), name))
        self._scene = name


def _event(event_id, event_type, tier=3):
    return {"id": event_id, "event_type": event_type, "tier": tier}


def _drive(obs, clock, fetch_state, ticks):
    return service.run(
        fetch_state=fetch_state,
        obs_client=obs,
        tts_runner=None,
        record_event=lambda events: None,
        sleep_seconds=5.0,
        max_ticks=ticks,
        clock=clock,
        sleep=clock.sleep,
    )


def test_a_salient_event_switches_on_the_first_tick_after_boot():
    clock = FakeClock()
    obs = FakeObs(clock, scene="world-focus")
    # The boot seed is the window's high-water mark, so the event only
    # matters to the scene choice, which looks at the whole window.
    state = {"recent": [_event(7, "big_move")]}  # market -> chart-focus

    _drive(obs, clock, lambda: state, ticks=1)

    assert obs.switches == [(0.0, "chart-focus")], (
        "the director held the program for a dwell it never served after boot"
    )


def test_the_normal_dwell_still_holds_after_a_real_switch():
    clock = FakeClock()
    obs = FakeObs(clock, scene="world-focus")

    def fetch_state():
        elapsed = (clock() - T0).total_seconds()
        if elapsed < 5:
            return {"recent": [_event(7, "big_move")]}  # -> chart-focus
        # From the next tick on, the newest salient event wants world-focus.
        return {"recent": [_event(8, "trader_opened"), _event(7, "big_move")]}

    _drive(obs, clock, fetch_state, ticks=20)  # t = 0 .. 95

    assert obs.switches == [(0.0, "chart-focus"), (60.0, "world-focus")], (
        "the second switch must wait out min_dwell_seconds after the first"
    )


def test_a_resumed_off_home_scene_decays_home_after_the_remaining_dwell():
    # Same seed: dwell starts at min_dwell_seconds, so decay (needs
    # return_to_home_seconds) fires 60 s after boot, not 120 s. Pinned so the
    # coupling to the seed is visible, not accidental.
    clock = FakeClock()
    obs = FakeObs(clock, scene="world-focus")

    _drive(obs, clock, lambda: {"recent": []}, ticks=30)  # t = 0 .. 145

    expected = CFG.return_to_home_seconds - CFG.min_dwell_seconds
    assert obs.switches == [(float(expected), CFG.home_scene)]


def test_the_default_clock_and_sleep_are_the_real_ones(monkeypatch):
    slept = []
    monkeypatch.setattr(service.time, "sleep", slept.append)
    obs = FakeObs(FakeClock(), scene="chart-focus")

    code = service.run(
        fetch_state=lambda: {"recent": []},
        obs_client=obs,
        tts_runner=None,
        record_event=lambda events: None,
        sleep_seconds=3.5,
        max_ticks=2,
    )

    assert code == 0
    assert slept == [3.5, 3.5]


def test_the_default_clock_reads_the_wall_clock(monkeypatch):
    seen = []
    real_tick = service.tick

    def spy(state, dir_state, now, config):
        seen.append(now)
        return real_tick(state, dir_state, now, config)

    monkeypatch.setattr(service, "tick", spy)
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    before = datetime.now(timezone.utc)
    service.run(
        fetch_state=lambda: {"recent": []},
        obs_client=FakeObs(FakeClock(), scene="chart-focus"),
        tts_runner=None,
        record_event=lambda events: None,
        max_ticks=1,
    )
    after = datetime.now(timezone.utc)

    assert before <= seen[0] <= after
    assert seen[0].tzinfo is not None
