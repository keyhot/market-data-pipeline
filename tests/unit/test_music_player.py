"""The bed's decisions, with no OBS, no clock and no filesystem."""

from datetime import datetime, timedelta, timezone

import pytest

from music.library import Track, shuffled
from music.player import (
    BUSY_STATES,
    FINISHED_STATES,
    OVERRUN_GRACE,
    BedState,
    MediaStatus,
    start_state,
    tick,
)

T0 = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)


def _track(name: str, seconds: float = 100.0) -> Track:
    return Track(
        file=f"{name}.mp3",
        title=name.title(),
        artist="Test Artist",
        source="Mixkit",
        source_url="https://mixkit.co/free-stock-music/",
        license="Mixkit Stock Music Free License",
        license_url="https://mixkit.co/license/#musicFree",
        duration_seconds=seconds,
    )


@pytest.fixture
def tracks() -> list[Track]:
    return [_track(n) for n in ("alpha", "bravo", "charlie", "delta")]


def test_a_cold_bed_starts_playing_something(tracks):
    state, action = tick(BedState(), MediaStatus(), T0, tracks)
    assert action.is_change
    assert action.play.file in {t.file for t in tracks}
    assert state.current == action.play.file
    assert state.started_at == T0


def test_a_playing_track_is_left_alone(tracks):
    state, _ = tick(start_state(tracks, seed=1), MediaStatus(), T0, tracks)
    playing = MediaStatus("OBS_MEDIA_STATE_PLAYING", cursor_ms=5_000, duration_ms=100_000)
    after, action = tick(state, playing, T0 + timedelta(seconds=5), tracks)
    assert not action.is_change
    assert after.current == state.current
    assert after.started_at == state.started_at


@pytest.mark.parametrize("finished", sorted(FINISHED_STATES))
def test_every_finished_state_advances_the_track(tracks, finished):
    state, _ = tick(BedState(), MediaStatus(), T0, tracks)
    after, action = tick(state, MediaStatus(finished), T0 + timedelta(seconds=100), tracks)
    assert action.is_change
    assert after.current != state.current


@pytest.mark.parametrize("busy", sorted(BUSY_STATES))
def test_no_busy_state_interrupts_a_track(tracks, busy):
    state, _ = tick(BedState(), MediaStatus(), T0, tracks)
    status = MediaStatus(busy, cursor_ms=1_000, duration_ms=100_000)
    _, action = tick(state, status, T0 + timedelta(seconds=1), tracks)
    assert not action.is_change


def test_a_stuck_track_is_overruled_by_the_clock(tracks):
    """OBS insisting PLAYING forever must not mean one track forever."""
    state, _ = tick(BedState(), MediaStatus(), T0, tracks)
    stuck = MediaStatus("OBS_MEDIA_STATE_PLAYING", cursor_ms=100, duration_ms=100_000)

    _, held = tick(state, stuck, T0 + timedelta(seconds=100) + OVERRUN_GRACE / 2, tracks)
    assert not held.is_change, "must not cut a track short"

    _, advanced = tick(
        state, stuck, T0 + timedelta(seconds=100) + OVERRUN_GRACE + timedelta(seconds=1), tracks
    )
    assert advanced.is_change and advanced.reason == "overran its length"


def test_the_whole_playlist_plays_before_anything_repeats(tracks):
    state, action = tick(BedState(), MediaStatus(), T0, tracks)
    played = [action.play.file]
    now = T0
    for _ in range(len(tracks) - 1):
        now += timedelta(seconds=100)
        state, action = tick(state, MediaStatus("OBS_MEDIA_STATE_ENDED"), now, tracks)
        played.append(action.play.file)
    assert sorted(played) == sorted(t.file for t in tracks)


def test_wrapping_reshuffles_and_never_repeats_across_the_seam(tracks):
    """The one repeat a listener notices is the same song twice in a row."""
    state, action = tick(BedState(), MediaStatus(), T0, tracks)
    now, last = T0, action.play.file
    for _ in range(len(tracks) * 3):
        now += timedelta(seconds=100)
        state, action = tick(state, MediaStatus("OBS_MEDIA_STATE_ENDED"), now, tracks)
        assert action.play.file != last, "same track twice in a row"
        last = action.play.file
    assert state.passes >= 1, "should have wrapped at least once"


def test_a_track_dropped_from_the_manifest_is_not_played(tracks):
    state, _ = tick(BedState(), MediaStatus(), T0, tracks)
    survivors = [t for t in tracks if t.file != state.current]
    after, action = tick(state, MediaStatus("OBS_MEDIA_STATE_ENDED"), T0, survivors)
    assert action.play.file in {t.file for t in survivors}
    assert all(f in {t.file for t in survivors} for f in after.order)


def test_an_empty_library_is_a_hold_not_a_crash():
    state, action = tick(BedState(), MediaStatus(), T0, [])
    assert not action.is_change and state == BedState()


def test_tick_is_deterministic(tracks):
    a = tick(start_state(tracks, seed=7), MediaStatus("OBS_MEDIA_STATE_ENDED"), T0, tracks)
    b = tick(start_state(tracks, seed=7), MediaStatus("OBS_MEDIA_STATE_ENDED"), T0, tracks)
    assert a == b


def test_shuffle_keeps_every_track_exactly_once(tracks):
    order = shuffled(tracks, seed=3)
    assert sorted(t.file for t in order) == sorted(t.file for t in tracks)
