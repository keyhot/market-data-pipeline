"""Prediction-outcome resolver (Sprint 10): every signal past its horizon
gets publicly resolved against what the market actually did, and the
resolution — win or loss — becomes a permanent world event.

Severity design (pinned by tests): base = (probability − 0.5) × 2, doubled
for losses — a confident wrong call is the most interesting content the
model produces. Resolution uses bars as stored at resolution time; a
missing realized bar leaves the signal pending for the next run.
"""

import logging
from datetime import datetime, timedelta, timezone

from storage.postgres_store import (
    append_world_events,
    get_bar_close,
    get_unresolved_signals,
    resolve_signal,
)

# Bar-interval durations the resolver understands.
INTERVAL_DELTAS = {"1m": timedelta(minutes=1), "1d": timedelta(days=1)}

LOSS_SEVERITY_MULTIPLIER = 2.0

logger = logging.getLogger(__name__)


def resolve_pending(now: datetime | None = None) -> list[dict]:
    """Resolve every signal whose horizon has passed and whose bars exist.
    Returns the resolutions performed (empty when nothing was due)."""
    if now is None:
        now = datetime.now(timezone.utc)

    resolutions: list[dict] = []
    events: list[dict] = []
    for signal in get_unresolved_signals():
        delta = INTERVAL_DELTAS.get(signal["interval"])
        if delta is None:
            logger.warning(
                "Unknown interval, signal left pending",
                extra={"interval": signal["interval"]},
            )
            continue
        target_ts = signal["signal_timestamp"] + delta * signal["horizon_bars"]
        if target_ts > now:
            continue  # horizon not reached yet

        entry_close = get_bar_close(
            signal["symbol"], signal["interval"], signal["signal_timestamp"]
        )
        realized_close = get_bar_close(
            signal["symbol"], signal["interval"], target_ts
        )
        if entry_close is None or realized_close is None:
            continue  # bar gap — retry on a later run

        went_up = realized_close > entry_close
        predicted_up = signal["direction"] == "up"
        outcome = "win" if went_up == predicted_up else "loss"
        realized_return = realized_close / entry_close - 1.0

        updated = resolve_signal(
            signal["symbol"],
            signal["interval"],
            signal["signal_timestamp"],
            signal["model_version"],
            outcome,
        )
        if updated == 0:
            continue  # resolved concurrently — do not emit a second event

        confidence = (signal["probability"] - 0.5) * 2
        severity = round(
            confidence * (LOSS_SEVERITY_MULTIPLIER if outcome == "loss" else 1.0),
            4,
        )
        resolution = {
            "symbol": signal["symbol"],
            "signal_timestamp": signal["signal_timestamp"],
            "outcome": outcome,
            "realized_return": realized_return,
        }
        resolutions.append(resolution)
        events.append(
            {
                "occurred_at": target_ts,
                "event_type": "signal_resolved",
                "symbol": signal["symbol"],
                "severity": severity,
                "payload": {
                    "direction": signal["direction"],
                    "probability": signal["probability"],
                    "outcome": outcome,
                    "realized_return": round(realized_return, 6),
                    "model_version": signal["model_version"],
                    "horizon_bars": signal["horizon_bars"],
                },
            }
        )

    if events:
        append_world_events(events)
        logger.info("Signals resolved", extra={"count": len(events)})
    return resolutions
