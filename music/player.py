"""When to change the record — the whole decision, with no OBS and no clock.

obs-websocket can tell you a media source's state, cursor and duration, and it
can tell you to play a file. What it cannot tell you, for any source kind, is
*which track* is playing — there is no "now playing" to read back. So the bed
does not hand OBS a playlist and ask later; it plays exactly one file at a time
and advances it itself. Now-playing is authoritative on this side because it is
the only side that knows.

Everything here is pure: `tick()` takes the state and OBS's answer and returns
the next state plus what to do. The runner in `scripts/music_bed.py` owns the
socket, the clock and the retries.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from music.library import Track, shuffled

# OBS media states that mean "this track is over, put on the next one". ERROR is
# in the list on purpose: an unreadable file must not wedge the bed forever, and
# skipping past it is strictly better than silence.
FINISHED_STATES = frozenset(
    {
        "OBS_MEDIA_STATE_NONE",
        "OBS_MEDIA_STATE_STOPPED",
        "OBS_MEDIA_STATE_ENDED",
        "OBS_MEDIA_STATE_ERROR",
    }
)
# States where a track is legitimately mid-flight and must be left alone.
BUSY_STATES = frozenset(
    {
        "OBS_MEDIA_STATE_PLAYING",
        "OBS_MEDIA_STATE_OPENING",
        "OBS_MEDIA_STATE_BUFFERING",
        "OBS_MEDIA_STATE_PAUSED",
    }
)

# How long past a track's own length we wait before overruling OBS. The status
# call is the seam we trust least (`vlc_source` did not even exist on the host
# this was written for), so the clock is the backstop: a bed that goes quiet
# because a status field changed shape is exactly the failure nobody notices.
OVERRUN_GRACE = timedelta(seconds=20)


@dataclass(frozen=True)
class MediaStatus:
    """What OBS reports about the bed input. All fields optional — a source
    that has never been given a file answers with nulls, not zeros."""

    state: str = "OBS_MEDIA_STATE_NONE"
    cursor_ms: float | None = None
    duration_ms: float | None = None


@dataclass(frozen=True)
class BedState:
    """The playlist and our place in it. `order` is files, not Tracks, so the
    state stays trivially comparable in tests."""

    order: tuple[str, ...] = ()
    index: int = -1
    current: str | None = None
    started_at: datetime | None = None
    seed: int = 0
    passes: int = 0


@dataclass(frozen=True)
class Action:
    """What the runner should do. `play=None` means leave the bed alone."""

    play: Track | None = None
    reason: str = "hold"

    @property
    def is_change(self) -> bool:
        return self.play is not None


def start_state(tracks: list[Track], seed: int) -> BedState:
    """A fresh playlist, shuffled but not yet playing anything."""
    order = tuple(t.file for t in shuffled(tracks, seed))
    return BedState(order=order, index=-1, current=None, started_at=None, seed=seed)


def _finished(state: BedState, status: MediaStatus, now: datetime) -> str | None:
    """Why the current track is over, or None if it isn't."""
    if state.current is None:
        return "first track"
    if status.state in FINISHED_STATES:
        return f"media {status.state.rsplit('_', 1)[-1].lower()}"
    if state.started_at is not None:
        # Prefer the track's real length as OBS measured it; fall back to the
        # manifest's when OBS has not reported one yet.
        duration = timedelta(milliseconds=status.duration_ms or 0)
        if duration and now - state.started_at > duration + OVERRUN_GRACE:
            return "overran its length"
    return None


def tick(
    state: BedState,
    status: MediaStatus,
    now: datetime,
    tracks: list[Track],
) -> tuple[BedState, Action]:
    """Advance the bed by one observation.

    Returns the new state and the action the runner should carry out. Called
    with the same inputs it returns the same outputs — the only nondeterminism
    (the shuffle) is seeded from state, and the only clock is the `now` passed in.
    """
    by_file = {t.file: t for t in tracks}
    if not by_file:
        return state, Action(reason="no playable tracks")

    # A manifest change between ticks must not strand us on a file that is gone.
    order = tuple(f for f in state.order if f in by_file)
    if not order:
        state = start_state(tracks, state.seed)
        order = state.order

    reason = _finished(replace(state, order=order), status, now)
    if reason is None:
        return replace(state, order=order), Action(reason="playing")

    index = state.index + 1
    passes = state.passes
    if index >= len(order):
        # Wrapped: reshuffle so the second hour is not the first hour again.
        passes += 1
        reshuffled = shuffled(list(by_file.values()), state.seed + passes, avoid_first=state.current)
        order = tuple(t.file for t in reshuffled)
        index = 0

    nxt = by_file[order[index]]
    return (
        BedState(
            order=order,
            index=index,
            current=nxt.file,
            started_at=now,
            seed=state.seed,
            passes=passes,
        ),
        Action(play=nxt, reason=reason),
    )
