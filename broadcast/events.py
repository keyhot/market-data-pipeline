"""YouTube broadcast lifecycle as append-only world events (Sprint 14, A3):
``broadcast_created`` / ``broadcast_live`` / ``broadcast_ended`` join the
world's permanent memory.

These three are what **public-broadcast uptime** is computed from
(``scripts/soak_report.compute_broadcast_uptime``). OBS pushing to the ingest
key proves nothing about whether anyone could watch — on 2026-07-27 the stream
pushed for ~4h with no active broadcast. This log is the difference between
"the encoder was up" and "the stream was public", and the soak report reports
both side by side rather than letting the flattering one stand in for the other.

Mirrors director/events.py's JSONL-spool pattern — including its post-KI-011
shape, where a poison spooled row can never block a fresh event from being
appended (a wedged spool must not be able to make an uptime report lie).

**Who emits these:** the `("record", event_type, payload)` actions returned by
``broadcast.policy.tick``, applied by the A4 runner. The runner must NOT
synthesize its own ``broadcast_created`` when it performs ``create_and_bind`` —
``tick`` records it on the following pass, when the lifecycle it observes goes
``None -> ready``. Doing both would write two rows per create.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from storage.postgres_store import append_world_events

logger = logging.getLogger(__name__)

# created/live are routine meta-events (severity 1.0 -> tier 0 under the generic
# cuts, so the room doesn't dramatize a healthy stream going public). An *ended*
# broadcast is the failure the whole sprint exists to prevent, so it outranks
# them and can reach the director's rails.
SEVERITIES = {
    "broadcast_created": 1.0,
    "broadcast_live": 1.0,
    "broadcast_ended": 2.0,
}
BROADCAST_EVENT_TYPES = frozenset(SEVERITIES)


def _default_spool() -> Path:
    return Path(
        os.environ.get("BROADCAST_EVENT_SPOOL", "data/broadcast_events.spool.jsonl")
    )


def _build(event_type: str, broadcast_id: str, occurred_at=None, **extra) -> dict:
    return {
        "occurred_at": occurred_at or datetime.now(timezone.utc),
        "event_type": event_type,
        "severity": SEVERITIES[event_type],
        # A broadcast belongs to the whole world, not to one ticker.
        "symbol": None,
        "payload": {"broadcast_id": broadcast_id, **extra},
    }


def build_broadcast_created(broadcast_id, occurred_at=None, **extra) -> dict:
    return _build("broadcast_created", broadcast_id, occurred_at, **extra)


def build_broadcast_live(broadcast_id, occurred_at=None, **extra) -> dict:
    return _build("broadcast_live", broadcast_id, occurred_at, **extra)


def build_broadcast_ended(broadcast_id, occurred_at=None, **extra) -> dict:
    return _build("broadcast_ended", broadcast_id, occurred_at, **extra)


BUILDERS = {
    "broadcast_created": build_broadcast_created,
    "broadcast_live": build_broadcast_live,
    "broadcast_ended": build_broadcast_ended,
}


def build_from_action(event_type: str, payload: dict, occurred_at=None) -> dict:
    """Turn a `("record", event_type, payload)` action from
    ``broadcast.policy.tick`` into a world event. Keeps the runner (A4) free of
    any knowledge of event shapes."""
    payload = dict(payload or {})
    broadcast_id = payload.pop("broadcast_id", None)
    return BUILDERS[event_type](broadcast_id, occurred_at=occurred_at, **payload)


def record_broadcast_events(events, spool_path=None) -> None:
    """Append broadcast events; flush any spooled backlog first. On DB failure,
    spool to JSONL so a Postgres outage never loses broadcast history (it would
    silently understate public uptime). Safe to call with an empty list — it
    just drains the backlog (flush-on-tick, like the director).

    The flush and the fresh-event append are isolated (KI-011): a poison row in
    the spool cannot block a fresh event from being persisted on the same tick.
    """
    spool_path = spool_path or _default_spool()
    events = events or []

    flushed = _flush_spool_isolated(spool_path)
    if flushed:
        logger.info("Broadcast event spool flushed", extra={"count": flushed})

    if not events:
        return

    try:
        append_world_events(events)
    except Exception:
        _spool(events, spool_path)
        logger.warning(
            "broadcast_event_append_failed",
            extra={
                "event_types": [e["event_type"] for e in events],
                "error_type": type(_last_exc()),
                "error_message": str(_last_exc()),
                "spool": str(spool_path),
            },
            exc_info=True,
        )


def _flush_spool_isolated(spool_path: Path) -> int:
    """Try to drain the spool; on any failure, log the truth and return 0.
    Never raises — the caller relies on this to NOT block the fresh append."""
    try:
        return _flush_spool(spool_path)
    except Exception:
        logger.warning(
            "broadcast_event_spool_flush_failed",
            extra={
                "error_type": type(_last_exc()),
                "error_message": str(_last_exc()),
                "spool": str(spool_path),
            },
            exc_info=True,
        )
        return 0


def _last_exc() -> BaseException:
    import sys

    return sys.exc_info()[1] or RuntimeError("unknown")


def _flush_spool(spool_path: Path) -> int:
    if not spool_path.exists():
        return 0
    rows = []
    for line in spool_path.read_text().strip().splitlines():
        raw = json.loads(line)
        raw["occurred_at"] = datetime.fromisoformat(raw["occurred_at"])
        rows.append(raw)
    if rows:
        append_world_events(rows)
    spool_path.unlink()
    return len(rows)


def _spool(events, spool_path: Path) -> None:
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    with spool_path.open("a") as handle:
        for event in events:
            row = {**event, "occurred_at": event["occurred_at"].isoformat()}
            handle.write(json.dumps(row) + "\n")
