"""World event recording (Sprint 9): the append-only gate between the
salience rules and the world_events table. The cooldown guard derives its
state from the database, so restarts can't cause double-firing sprees.
"""

import logging
from datetime import timedelta

import pandas as pd

from storage.postgres_store import append_world_events, latest_world_event_time
from world.salience import SalienceConfig, detect_events

logger = logging.getLogger(__name__)


def record_salient_events(
    symbol: str, bars: pd.DataFrame, config: SalienceConfig | None = None
) -> list[dict]:
    """Detect, cooldown-filter, and append. Returns the events written."""
    config = config or SalienceConfig()
    candidates = detect_events(symbol, bars, config)
    if not candidates:
        return []

    cooldown = timedelta(minutes=config.cooldown_minutes)
    accepted: list[dict] = []
    newest_per_type: dict[str, object] = {}
    for event in sorted(candidates, key=lambda e: e["occurred_at"]):
        etype = event["event_type"]
        last = newest_per_type.get(etype)
        if last is None:
            last = latest_world_event_time(etype, symbol)
        if last is not None and event["occurred_at"] - last < cooldown:
            continue
        accepted.append(event)
        newest_per_type[etype] = event["occurred_at"]

    if accepted:
        append_world_events(accepted)
        logger.info(
            "World events recorded",
            extra={"symbol": symbol, "count": len(accepted)},
        )
    return accepted


def record_model_events(
    symbol: str, interval: str = "1m", config: SalienceConfig | None = None
) -> list[dict]:
    """Check the model's track record for salient patterns (losing streaks)
    and append them — same DB-backed cooldown as market events."""
    from storage.postgres_store import get_signal_accuracy
    from world.salience import detect_model_events

    config = config or SalienceConfig()
    accuracy = get_signal_accuracy(symbol, interval)
    candidates = detect_model_events(symbol, accuracy, config)
    if not candidates:
        return []

    cooldown = timedelta(minutes=config.cooldown_minutes)
    accepted = []
    for event in candidates:
        last = latest_world_event_time(event["event_type"], symbol)
        if last is not None and event["occurred_at"] - last < cooldown:
            continue
        accepted.append(event)
    if accepted:
        append_world_events(accepted)
        logger.info(
            "Model events recorded",
            extra={"symbol": symbol, "count": len(accepted)},
        )
    return accepted
