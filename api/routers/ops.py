"""Process health and liveness: `/health`, `/metrics`, the renderer heartbeat
that `/health` reads, and the music credit line (#44, moved from `api/main.py`).

The heartbeat registry, the `/health` database probe and its deadline constants
are module state shared by the routes below, so they stay together in one module.
"""

import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from api.shared import logger, metrics_registry, scheduler_service
from schemas.responses import ApiResponse
from storage.postgres_store import get_now_playing, world_liveness_times
from storage.writes import (
    postgres_readable,
    postgres_status,
    postgres_write_enabled,
    write_metrics,
)
from world.liveness import world_liveness
from world.renderer_health import record_beat, renderer_status

router = APIRouter()


# KI-046: registry behind the renderer heartbeat. `record_beat`/`renderer_status`
# (world.renderer_health) are pure; this dict plus the lock are the only state
# and I/O around them. `/health` is a sync `def`, so FastAPI runs it — and
# `world_heartbeat` — in a threadpool: several OBS browser sources hit the
# heartbeat route concurrently, and `renderer_status` prunes (deletes) the
# store while iterating it, so unsynchronized access can race.
_RENDERER_BEATS: dict = {}
_RENDERER_BEATS_LOCK = threading.Lock()
# Captured at import, not in `lifespan`: Starlette only runs lifespan on
# context-manager entry, and this task's tests construct TestClient(app) at
# module level, so a lifespan-set value would be unset in every test.
_PROCESS_STARTED_AT = time.monotonic()
# KI-069: the world read gets whatever this deadline has left after the
# readability gate. The stream watchdog gives the whole request 5s
# (`WatchdogConfig.content_timeout_seconds`, KI-024) and compose's
# healthchecks 6s; 4s leaves a second under the tightest of them. It cannot go
# lower than the cold path's role probe plus ping (2s + 2s, `storage/db.py`),
# or an unreachable database would spend it before the gate has answered.
# Both bounds are asserted in tests/api/test_health_endpoint.py against the
# real constants and the real compose file.
_HEALTH_DB_BUDGET_SECONDS = 4.0


class RendererBeat(BaseModel):
    page: str
    frames: int


@router.post("/world/heartbeat")
def world_heartbeat(beat: RendererBeat, request: Request):
    """KI-046: the page says it is alive, and carries the frame count that
    stops a frozen page from being able to say so convincingly.

    Keyed by Host, because OBS's sources load 127.0.0.N shards and a developer
    tab on localhost must never cover for a dead on-air source.
    """
    with _RENDERER_BEATS_LOCK:
        record_beat(
            _RENDERER_BEATS,
            host=request.headers.get("host", "unknown"),
            page=beat.page,
            frames=beat.frames,
            now=time.monotonic(),
        )
    return ApiResponse(status=200, message="beat recorded", data={})


def _health_db_section(db_deadline: float, result: dict) -> None:
    """`/health`'s database section, run on `_health_db_probe`. It writes
    each answer into `result` as soon as it has it, so a caller that stops
    waiting still reports what was already known (see `health()`)."""
    postgres = postgres_status()
    result["postgres"] = postgres
    # KI-057: a drawing page is not proof the world is running — the model
    # went silent for three days while ingestion and every container stayed
    # green. This must degrade to None rather than ever fail the request:
    # `/health` answers 200 through a Postgres outage by design (the stream
    # watchdog probes it for content health, KI-024). Gate on
    # `postgres_readable()`, not on `postgres["connected"]` directly:
    # `connected` only reflects reachability when POSTGRES_WRITE_ENABLED is
    # on (`postgres_status()` skips its own ping otherwise), so a read-only
    # deployment — writes off, database perfectly readable — would see
    # `connected: None` forever and this field would never report.
    # `postgres_readable()` reuses that ping's result when it already ran and
    # probes on its own only when it didn't, so this still never stacks a
    # second bounded connection-acquire wait onto the same request when
    # writes are on and Postgres is down. When Postgres is UP the reader does
    # run, and it gets only what `db_deadline` has left (KI-069): measured at
    # 4.01s with the reader's tables locked, where the independent bounds
    # summed to ~8s. A stalled socket is bounded by the caller, not here:
    # see `health()` (KI-080).
    # `signal_timestamp` / `occurred_at` are TIMESTAMPTZ — an aware `now`
    # avoids the naive/aware TypeError, but the broad except is what
    # actually keeps the 200-through-an-outage guarantee even if a future
    # caller gets that wrong, or the reader fails for any other reason.
    # `world` and `world_unavailable_reason` are a pair, not two independent
    # fields: `null`/`null` means the field itself is healthy (see the dict
    # below), and whenever `world` is `null` the reason is exactly one of
    # "not_connected" (the gate never opened — this ping-equivalent failed,
    # or writes are off and the DB didn't answer either) or "reader_error"
    # (Postgres answered the gate but the read itself then raised — e.g. it
    # dropped between the two, it ran out of the KI-069 deadline, including
    # before it could start, or a bug like a naive/aware TypeError). KI-057's
    # own alerting ticket is the consumer of this: "we don't know right now"
    # and "this has been broken since deploy" are different alerts, and a
    # bare `null` cannot tell them apart.
    world = None
    world_unavailable_reason = None
    readable = postgres_readable(postgres)
    result["readable"] = readable
    if readable:
        try:
            latest_signal_at, latest_event_at = world_liveness_times(
                timeout_seconds=db_deadline - time.monotonic()
            )
            world = world_liveness(
                latest_signal_at, latest_event_at, datetime.now(timezone.utc)
            )
        except Exception as e:
            logger.warning(
                "world_liveness_times failed; /health's world field degrades to None",
                extra={"error": f"{type(e).__name__}: {e}"},
            )
            world = None
            world_unavailable_reason = "reader_error"
    else:
        world_unavailable_reason = "not_connected"
    result["world"] = world
    result["world_unavailable_reason"] = world_unavailable_reason
    result["done"] = True


# KI-080: everything under `_health_db_section` is bounded by the server or
# ends at the handshake. A socket that stops passing packets behind something
# that still ACKs locally — a userspace proxy, the near end of an SSH tunnel
# — waits on the client's TCP stack forever: measured hanging past 15s with
# 1s keepalives and `tcp_user_timeout` set. So the section runs on its own
# thread, and the request waits for it only until the deadline, plus a grace
# for the reader's own bound landing on it (measured 4.01s, KI-069). Single
# flight: while one probe is stuck, requests answer at once rather than park
# another thread (and, sync, another threadpool worker) behind it. A daemon
# thread, not an executor, so a stuck probe cannot hold up interpreter exit.
_HEALTH_DB_GRACE_SECONDS = 0.25
_health_db_lock = threading.Lock()
_health_db_probe: threading.Thread | None = None


def _health_db(db_deadline: float) -> tuple[dict, dict | None, str | None]:
    global _health_db_probe
    result: dict = {}
    with _health_db_lock:
        stuck = _health_db_probe is not None and _health_db_probe.is_alive()
        if not stuck:
            _health_db_probe = threading.Thread(
                target=_health_db_section,
                args=(db_deadline, result),
                name="health-db",
                daemon=True,
            )
            _health_db_probe.start()
            probe = _health_db_probe
    if not stuck:
        probe.join(max(0.0, db_deadline - time.monotonic()) + _HEALTH_DB_GRACE_SECONDS)
    answered = dict(result)
    if answered.get("done"):
        return (
            answered["postgres"],
            answered["world"],
            answered["world_unavailable_reason"],
        )
    logger.warning(
        "/health's database section did not answer in time",
        extra={
            "previous_probe_still_stuck": stuck,
            "gate_answered": "readable" in answered,
        },
    )
    # The gate answered and only the reader stalled: the database was
    # reachable, which is the existing "reader_error". Anything earlier is
    # not_connected — for the watchdog, a database that cannot answer in the
    # budget is an outage (KI-024).
    postgres = answered.get("postgres") or {
        "enabled": postgres_write_enabled(),
        "connected": False,
    }
    if answered.get("readable"):
        return postgres, None, "reader_error"
    return {**postgres, "connected": False}, None, "not_connected"


def _health_db_wait_idle(timeout: float = 5.0) -> None:
    """Tests: let a released probe finish before the next one starts."""
    probe = _health_db_probe
    if probe is not None:
        probe.join(timeout)


@router.get("/health")
def health():
    with _RENDERER_BEATS_LOCK:
        renderer = renderer_status(
            _RENDERER_BEATS, now=time.monotonic(), started_at=_PROCESS_STARTED_AT
        )
    # KI-069: one deadline for every database wait in this request. The probe
    # below and the world read each carried their own bound, and they summed
    # past the watchdog's timeout — the endpoint that exists to prove the
    # stack is alive could itself trip a content outage.
    db_deadline = time.monotonic() + _HEALTH_DB_BUDGET_SECONDS
    postgres, world, world_unavailable_reason = _health_db(db_deadline)
    return ApiResponse(
        status=200,
        message="API is healthy",
        data={
            "scheduler": scheduler_service.status(),
            "postgres": postgres,
            # `healthy` here folds EVERY page that posted, including developer
            # tabs, so it is advisory. The watchdog judges one host out of
            # `pages` — see Task 15. The API cannot know which source is on air.
            "renderer": renderer,
            "world": world,
            "world_unavailable_reason": world_unavailable_reason,
        },
    )


@router.get("/metrics")
def metrics():
    return ApiResponse(
        status=200,
        data={
            **metrics_registry.snapshot(),
            "scheduler": scheduler_service.status(),
            "postgres_writes": write_metrics(),
        },
    )


# How long after a track should have ended we keep crediting it. The runner
# polls every 5s and advances on ENDED, so a live bed's row is never more than
# about `duration + 5s` old; anything older means the runner is gone.
_CREDIT_GRACE_SECONDS = 30.0
# Fallback when a row carries no duration — defensive only (all shipped tracks
# parse), but an unbounded credit is exactly what this guard exists to prevent.
_CREDIT_MAX_SECONDS = 600.0


def _credit_expired(started_at, duration_seconds) -> bool:
    """Has this row outlived the track it describes?

    `music_now_playing` is an upsert, so the last track the runner started
    stays in the table forever. If the bed dies — a crash loop, a stopped
    unit, OBS relaunched under it — the row is still there and the strip would
    go on crediting a song over silence. A credit nobody asked us to give,
    naming a track that is not playing, is worse than no credit at all.
    """
    if started_at is None:
        return True
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    length = duration_seconds if duration_seconds else _CREDIT_MAX_SECONDS
    return elapsed > length + _CREDIT_GRACE_SECONDS


@router.get("/music/now-playing")
def music_now_playing():
    """The bed's current track, for the credit line on the signals strip.

    Read-only and forgiving: the bed is a courtesy (neither Mixkit's nor
    Pixabay's licence requires attribution), so a missing runner, a stale row
    or a DB blip all answer `{"playing": null}` rather than an error. The strip
    then shows nothing at all, which is the correct look for a stream with no
    music.
    """
    try:
        current = get_now_playing()
    except Exception:  # noqa: BLE001 - a DB blip must not 500 the overlay
        current = None
    if not current or _credit_expired(
        current.get("started_at"), current.get("duration_seconds")
    ):
        return ApiResponse(status=200, data={"playing": None})
    return ApiResponse(
        status=200,
        data={
            "playing": {
                "title": current["title"],
                "artist": current["artist"],
                "source": current["source"],
                "source_url": current["source_url"],
                "license": current["license"],
                "started_at": current["started_at"],
                "duration_seconds": current["duration_seconds"],
            }
        },
    )
