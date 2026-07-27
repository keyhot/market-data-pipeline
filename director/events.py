"""Director actions as append-only world events (Sprint 13): ``scene_switched``
and ``commentary_spoken`` join the world's permanent memory, so the log records
what the director did and can prove which real event each line reacted to.

Mirrors world/stream_events.py's JSONL-spool pattern — a Postgres outage never
loses director history. The director flushes the spool on its own tick (any
call drains the backlog) rather than waiting for the next external event.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from storage.postgres_store import append_world_events

logger = logging.getLogger(__name__)

# started/stopped are low-salience meta-events: severity 1.0 -> tier 0 under the
# generic cuts, so the director never reacts to its own actions.
SEVERITIES = {"scene_switched": 1.0, "commentary_spoken": 1.0}
DIRECTOR_EVENT_TYPES = frozenset(SEVERITIES)


def _default_spool() -> Path:
    return Path(
        os.environ.get("DIRECTOR_EVENT_SPOOL", "data/director_events.spool.jsonl")
    )


def build_scene_switched(scene, from_scene=None, occurred_at=None) -> dict:
    return {
        "occurred_at": occurred_at or datetime.now(timezone.utc),
        "event_type": "scene_switched",
        "severity": SEVERITIES["scene_switched"],
        "symbol": None,
        "payload": {"scene": scene, "from": from_scene},
    }


def build_commentary_spoken(
    character, text, event_id=None, symbol=None, occurred_at=None
) -> dict:
    return {
        "occurred_at": occurred_at or datetime.now(timezone.utc),
        "event_type": "commentary_spoken",
        "severity": SEVERITIES["commentary_spoken"],
        "symbol": symbol,
        # event_id ties the line back to the real event it reacted to.
        "payload": {"character": character, "text": text, "event_id": event_id},
    }


def record_director_events(events, spool_path=None) -> None:
    """Append director events; flush any spooled backlog first. On DB failure,
    spool the events to JSONL so a Postgres outage never loses them. Safe to
    call with an empty list — it just drains the backlog (flush-on-tick)."""
    spool_path = spool_path or _default_spool()
    events = events or []
    try:
        flushed = _flush_spool(spool_path)
        if events:
            append_world_events(events)
        if flushed:
            logger.info("Director event spool flushed", extra={"count": flushed})
    except Exception:
        if events:
            _spool(events, spool_path)
            logger.warning(
                "Postgres unreachable — director events spooled",
                extra={"count": len(events), "spool": str(spool_path)},
            )


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
