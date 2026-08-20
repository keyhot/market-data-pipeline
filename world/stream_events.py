"""Stream lifecycle world events (Sprint 11): the world remembers its own
outages the same way it remembers market moves. Append-only like all world
events; a local JSONL spool keeps history through the exact failures it
records (Postgres unreachable while the stream is dying).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage.postgres_store import append_world_events, latest_world_event_time

logger = logging.getLogger(__name__)

# started 1.0 (routine), stopped 2.0 (intentional stops are notable),
# dropped 5.0 (an outage is high-salience — the world should visibly react).
# reconnected 3.0 (KI-021): more notable than a stop we chose, less than an
# outage — the ingest really broke, and it really fixed itself.
SEVERITIES = {
    "stream_started": 1.0,
    "stream_stopped": 2.0,
    "stream_dropped": 5.0,
    "stream_reconnected": 3.0,
}
STREAM_EVENT_TYPES = frozenset(SEVERITIES)

# Only dropped events get a cooldown: a flapping stream shouldn't spam the
# world's memory, but every start/stop is real history.
DROPPED_COOLDOWN = timedelta(minutes=5)


def _default_spool() -> Path:
    return Path(os.environ.get("STREAM_EVENT_SPOOL", "data/stream_events.spool.jsonl"))


def build_stream_event(
    event_type: str,
    payload: dict | None = None,
    occurred_at: datetime | None = None,
) -> dict:
    if event_type not in SEVERITIES:
        raise ValueError(f"unknown stream event type: {event_type}")
    return {
        "occurred_at": occurred_at or datetime.now(timezone.utc),
        "event_type": event_type,
        "severity": SEVERITIES[event_type],
        "payload": payload or {},
    }


def record_stream_event(
    event_type: str,
    payload: dict | None = None,
    spool_path: Path | None = None,
) -> dict | None:
    """Build, cooldown-filter, and append one lifecycle event. Returns the
    event, or None when cooldown-suppressed. On DB failure the event goes to
    the spool instead of being lost."""
    spool_path = spool_path or _default_spool()
    event = build_stream_event(event_type, payload)
    if event_type == "stream_dropped":
        last = _safe_latest(event_type)
        if last is not None and event["occurred_at"] - last < DROPPED_COOLDOWN:
            return None
    # KI-011's shape, one module over — and KI-024 made it load-bearing. The
    # flush and the fresh append were bundled in ONE try: a single un-appendable
    # spooled row threw, the fresh event was spooled behind it, and the spool
    # grew forever. That spool is now the only evidence that survives the
    # outage it records (an API/Postgres outage is exactly when it is written),
    # so a spool that can wedge is the mechanism failing silently.
    flushed = _flush_spool_isolated(spool_path)
    if flushed:
        logger.info("Stream event spool flushed", extra={"count": flushed})
    try:
        append_world_events([event])
    except Exception as e:
        _spool(event, spool_path)
        logger.warning(
            "stream_event_append_failed — spooled",
            extra={
                "stream_event_type": event_type,
                "spool": str(spool_path),
                "error": f"{type(e).__name__}: {e}",
            },
        )
    return event


def _flush_spool_isolated(spool_path: Path) -> int:
    """Drain the spool; on any failure log the truth and return 0. Never raises
    — the caller relies on this NOT blocking the fresh append."""
    try:
        return flush_spool(spool_path)
    except Exception as e:
        logger.warning(
            "stream_event_spool_flush_failed",
            extra={"spool": str(spool_path), "error": f"{type(e).__name__}: {e}"},
        )
        return 0


def flush_spool(spool_path: Path | None = None) -> int:
    """Replay spooled events into world_events; removes the spool on success."""
    spool_path = spool_path or _default_spool()
    if not spool_path.exists():
        return 0
    events = []
    for line in spool_path.read_text().strip().splitlines():
        raw = json.loads(line)
        raw["occurred_at"] = datetime.fromisoformat(raw["occurred_at"])
        events.append(raw)
    if events:
        append_world_events(events)
    spool_path.unlink()
    return len(events)


def _spool(event: dict, spool_path: Path) -> None:
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    row = {**event, "occurred_at": event["occurred_at"].isoformat()}
    with spool_path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")


def _safe_latest(event_type: str) -> datetime | None:
    try:
        return latest_world_event_time(event_type, None)
    except Exception as e:
        # KI-005, accepted by design: the cooldown check must not be what
        # stops a lifecycle event being recorded. Failing open is the choice;
        # failing open *silently* was not — a DB outage looked identical to a
        # cooldown that had simply expired.
        logger.warning(
            "Cooldown check unavailable, recording anyway",
            extra={"event_type": event_type, "error": f"{type(e).__name__}: {e}"},
        )
        return None
