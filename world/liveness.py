"""Is the world running, as distinct from is the page drawing.

KI-057: between 2026-08-28 and 08-31 the model made no predictions for three
days. Ingestion never faltered — 2,880 bars a day, unbroken — every container
reported healthy, and the broadcast stayed live in front of a room where
nothing happened. The renderer heartbeat could not see it, because the page was
drawing correctly; it simply had no news.

Two ages, not one, and **both** must be fresh. During the outage the watchdog's
own `stream_dropped` events kept arriving, so a check on "any recent world
event" would have read healthy for all three days.

Pure: no DB, no clock. The caller supplies both.
"""

from __future__ import annotations

from datetime import datetime


def _age(then: datetime | None, now: datetime) -> float | None:
    """Seconds since `then`. Not clamped at zero: a negative result means
    `then` is in the future relative to `now` — a clock skew between the
    database and whatever produced `now`, not "brand new". Reported as-is
    (not hidden behind 0.0) so it stays legible in the payload; callers must
    not treat a negative age as healthy (see `world_liveness`)."""
    return None if then is None else (now - then).total_seconds()


def world_liveness(
    latest_signal_at: datetime | None,
    latest_event_at: datetime | None,
    now: datetime,
    *,
    stale_after_s: int = 5400,
) -> dict:
    """Ages of the newest signal and newest non-stream world event.

    `stale_after_s` defaults to 90 minutes: inference runs per interval and
    salience is bursty, so an hour of quiet is a calm market rather than a
    dead world. Three days is not.

    A timestamp from the future (age < 0) is never healthy, regardless of
    magnitude: it means the database's clock and this process's clock
    disagree, which is itself a reason not to trust "fresh". Without this,
    a `max(0.0, ...)` clamp would have reported such a world as `stale:
    False` with age `0.0` — the one false-healthy path in this module, and
    KI-057 recreated inside the very field built to catch it.
    """
    signal_age = _age(latest_signal_at, now)
    event_age = _age(latest_event_at, now)
    stale = (
        signal_age is None
        or event_age is None
        or signal_age > stale_after_s
        or event_age > stale_after_s
        or signal_age < 0
        or event_age < 0
    )
    return {"signal_age_s": signal_age, "event_age_s": event_age, "stale": stale}
