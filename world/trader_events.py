"""The trader inhabitant (Sprint 12): the freqtrade dry-run sidecar mirrored
into world events.

REST-only by design — freqtrade is GPL, so it lives in its own container and
is never imported here. The trader is an *independent* inhabitant, not an
executor of our signals: when its positions disagree with the model's calls,
that disagreement is real and is content.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

from storage.postgres_store import append_world_events, get_world_events

logger = logging.getLogger(__name__)

TRADER_EVENT_TYPES = frozenset(
    {"trader_opened", "trader_closed", "trader_milestone"}
)
TRADER_MIRROR_ENABLED_ENV = "TRADER_MIRROR_ENABLED"
MILESTONE_STEP = 5.0  # percent of closed P&L between milestone events


def trader_mirror_enabled() -> bool:
    raw = os.environ.get(TRADER_MIRROR_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no"}


class FreqtradeClient:
    """Thin REST wrapper. Injected as an argument everywhere so tests never
    reach the sidecar."""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 5.0):
        self._base = (base_url or os.environ.get(
            "FREQTRADE_URL", "http://freqtrade:8080"
        )).rstrip("/")
        self._auth = (
            os.environ.get("FREQTRADE_USERNAME", "worldwatcher"),
            os.environ.get("FREQTRADE_PASSWORD", ""),
        )
        self._timeout = timeout_seconds

    def get(self, path: str) -> dict:
        response = httpx.get(
            f"{self._base}{path}", auth=self._auth, timeout=self._timeout
        )
        response.raise_for_status()
        return response.json()


def _event(event_type: str, severity: float, payload: dict) -> dict:
    # symbol stays NULL deliberately: the trader's pair belongs in the payload,
    # not in world_events.symbol. Writing "BTCUSDT" here would fold the trader's
    # activity into the *market's* per-symbol mood in world/state.py, making
    # the room show one inhabitant's trades as if they were price action.
    return {
        "occurred_at": datetime.now(timezone.utc),
        "event_type": event_type,
        "symbol": None,
        "severity": round(severity, 4),
        "payload": payload,
    }


def diff_trader_state(previous: dict, current: dict) -> list[dict]:
    """Pure diff between the last observation and the current one.

    An empty `previous` means we have never looked; emitting every already-open
    trade as newly opened would fabricate history, so the first observation is
    silent by design.
    """
    if not previous:
        return []

    events: list[dict] = []
    was_open = set(previous.get("open_trade_ids") or [])
    now_open = {t["trade_id"]: t for t in current.get("open_trades") or []}

    for trade_id in sorted(set(now_open) - was_open):
        trade = now_open[trade_id]
        events.append(_event("trader_opened", 1.0, {
            "trade_id": trade_id, "pair": trade.get("pair"),
        }))

    closed_before = float(previous.get("profit_closed_percent") or 0.0)
    closed_now = float(current.get("profit_closed_percent") or 0.0)
    swing = abs(closed_now - closed_before)

    for trade_id in sorted(was_open - set(now_open)):
        # Severity tracks how much the closed P&L moved: a scratch exit is
        # routine, a large one is worth the room reacting to.
        events.append(_event("trader_closed", 1.0 + swing / 2.0, {
            "trade_id": trade_id,
            "pair": None,
            "profit_pct": round(closed_now, 4),
            "swing_pct": round(closed_now - closed_before, 4),
        }))

    # Truncating division, NOT floor: `-0.5 // 5` is -1, which would make a
    # trivial scratch loss look like it crossed a milestone.
    if int(closed_now / MILESTONE_STEP) != int(closed_before / MILESTONE_STEP):
        events.append(_event("trader_milestone", 2.0 + swing / 2.0, {
            "profit_pct": round(closed_now, 4),
        }))
    return events


def _load_previous() -> dict:
    """Reconstruct the last-seen state from the world's own memory rather than
    a state file — the log is already the durable record.

    Reads are filtered PER EVENT TYPE, never a single newest-N over the whole
    log: trader events are sparse, and after the ~60-day backfill a blanket
    `get_world_events(limit=200)` would return nothing but market events,
    silently reconstructing an empty state and re-emitting every open trade.
    """
    rows: list[dict] = []
    try:
        for event_type in sorted(TRADER_EVENT_TYPES):
            rows.extend(get_world_events(limit=200, event_type=event_type))
    except Exception:
        return {}
    rows.sort(key=lambda r: (r["occurred_at"], r.get("id") or 0), reverse=True)

    open_ids: set[int] = set()
    profit = None
    for row in reversed(rows):  # oldest first
        payload = row.get("payload") or {}
        if row["event_type"] == "trader_opened":
            open_ids.add(payload.get("trade_id"))
        elif row["event_type"] == "trader_closed":
            open_ids.discard(payload.get("trade_id"))
            profit = payload.get("profit_pct", profit)
        elif row["event_type"] == "trader_milestone":
            profit = payload.get("profit_pct", profit)
    if profit is None and not open_ids:
        return {}
    return {
        "open_trade_ids": sorted(i for i in open_ids if i is not None),
        "profit_closed_percent": profit or 0.0,
    }


def record_trader_events(client=None) -> list[dict]:
    """Poll the sidecar, diff, append. A dead sidecar is a logged skip — the
    world keeps running when one inhabitant is unavailable."""
    client = client or FreqtradeClient()
    try:
        status = client.get("/api/v1/status")
        profit = client.get("/api/v1/profit")
    except Exception as e:
        logger.warning("Trader mirror skipped", extra={"error": str(e)})
        return []

    open_trades = status if isinstance(status, list) else status.get("open_trades", [])
    current = {
        "open_trades": open_trades,
        "profit_closed_percent": (profit or {}).get("profit_closed_percent", 0.0),
    }
    events = diff_trader_state(_load_previous(), current)
    if events:
        append_world_events(events)
        logger.info("Trader events recorded", extra={"count": len(events)})
    return events
