"""KI-046: does the room actually reach the screen?

Pure functions over a dict the API owns. No FastAPI, no clock, no I/O - the
same shape as `director.scenes` and `broadcast.policy`, so the whole rule is
testable without a browser.
"""
from __future__ import annotations

BeatStore = dict[str, dict]

GRACE_SECONDS = 120.0     # after process start, before absence means anything
STALE_AFTER = 45.0        # the page posts every 15s; three misses is dead
FROZEN_AFTER = 8.0        # frames unchanged across at least this long
PRUNE_AFTER = 600.0       # a page gone this long is gone, not failing


def record_beat(
    store: BeatStore, host: str, page: str, frames: int, now: float
) -> None:
    previous = store.get(host)
    store[host] = {
        "page": page,
        "frames": int(frames),
        "at": float(now),
        "previous_frames": previous["frames"] if previous else None,
        "previous_at": previous["at"] if previous else None,
    }


def _judge(beat: dict, now: float, stale_after: float) -> dict:
    age = round(now - beat["at"], 1)
    frozen = False
    if beat["previous_at"] is not None:
        span = beat["at"] - beat["previous_at"]
        # A stalled counter stays equal, never falls. A decrease means the page
        # reloaded (browser-source refresh, operator reload, or asset redeploy),
        # not that the renderer is broken. Only equality counts as a stall.
        frozen = span >= FROZEN_AFTER and beat["frames"] == beat["previous_frames"]
    return {
        "page": beat["page"],
        "frames": beat["frames"],
        "age_seconds": age,
        "frozen": frozen,
        "healthy": age <= stale_after and not frozen,
    }


def renderer_status(
    store: BeatStore,
    now: float,
    started_at: float,
    grace_seconds: float = GRACE_SECONDS,
    stale_after: float = STALE_AFTER,
    required_host: str | None = None,
) -> dict:
    # Forget pages that stopped posting long ago. Without this, a browser tab
    # closed ten minutes back sits in the store at a growing age and pins the
    # fleet unhealthy — recording an outage on a stream that is fine.
    for host in [h for h, b in store.items() if now - b["at"] > PRUNE_AFTER]:
        del store[host]

    pages = {host: _judge(beat, now, stale_after) for host, beat in store.items()}
    if required_host is not None:
        watched = {h: p for h, p in pages.items() if h == required_host}
    else:
        watched = pages

    if not watched:
        # Absence is only evidence once the process has had time to be found.
        healthy = (now - started_at) < grace_seconds
        detail = "no heartbeat yet" if healthy else "no heartbeat"
    else:
        healthy = all(p["healthy"] for p in watched.values())
        detail = "ok" if healthy else "; ".join(
            f"{h}: age {p['age_seconds']}s frozen={p['frozen']}"
            for h, p in watched.items() if not p["healthy"]
        )
    return {"healthy": healthy, "detail": detail, "pages": pages}
