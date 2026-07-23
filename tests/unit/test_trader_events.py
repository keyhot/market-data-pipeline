"""The mirror is a pure diff plus a thin fetch. Tests drive the diff directly
and inject a fake client for the rest — no sidecar, no network."""

from world.reactions import REACTIONS
from world.salience import KNOWN_EVENT_TYPES
from world.trader_events import (
    TRADER_EVENT_TYPES,
    diff_trader_state,
    record_trader_events,
)


class FakeClient:
    def __init__(self, status, profit):
        self._status = status
        self._profit = profit
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self._status if "status" in path else self._profit


def _trade(trade_id, pair="BTC/USDT", profit_pct=0.0):
    return {"trade_id": trade_id, "pair": pair, "profit_pct": profit_pct}


def test_new_event_types_are_registered_everywhere():
    assert TRADER_EVENT_TYPES <= KNOWN_EVENT_TYPES
    assert TRADER_EVENT_TYPES <= set(REACTIONS)


def test_opening_a_trade_emits_trader_opened():
    events = diff_trader_state(
        {"open_trade_ids": [], "profit_closed_percent": 0.0},
        {"open_trades": [_trade(1)], "profit_closed_percent": 0.0},
    )
    assert [e["event_type"] for e in events] == ["trader_opened"]
    assert events[0]["payload"]["pair"] == "BTC/USDT"


def test_closing_a_trade_emits_trader_closed():
    events = diff_trader_state(
        {"open_trade_ids": [1], "profit_closed_percent": 0.0},
        {"open_trades": [], "profit_closed_percent": -2.5},
    )
    types = [e["event_type"] for e in events]
    assert "trader_closed" in types


def test_no_change_emits_nothing():
    previous = {"open_trade_ids": [1], "profit_closed_percent": 1.0}
    current = {"open_trades": [_trade(1)], "profit_closed_percent": 1.0}
    assert diff_trader_state(previous, current) == []


def test_severity_grows_with_the_size_of_the_pnl_swing():
    small = diff_trader_state(
        {"open_trade_ids": [1], "profit_closed_percent": 0.0},
        {"open_trades": [], "profit_closed_percent": -0.5},
    )
    large = diff_trader_state(
        {"open_trade_ids": [1], "profit_closed_percent": 0.0},
        {"open_trades": [], "profit_closed_percent": -12.0},
    )
    assert large[0]["severity"] > small[0]["severity"]


def test_first_observation_does_not_replay_history():
    """An empty previous state means we've never looked — reporting every
    already-open trade as newly opened would fabricate events."""
    events = diff_trader_state({}, {"open_trades": [_trade(1), _trade(2)],
                                    "profit_closed_percent": 3.0})
    assert events == []


def test_record_trader_events_uses_the_injected_client(monkeypatch):
    written = []
    monkeypatch.setattr(
        "world.trader_events.append_world_events",
        lambda events: written.extend(events) or len(events),
    )
    monkeypatch.setattr(
        "world.trader_events._load_previous", lambda: {"open_trade_ids": []}
    )
    client = FakeClient(
        {"open_trades": [_trade(7)]}, {"profit_closed_percent": 0.0}
    )
    events = record_trader_events(client=client)

    assert any("status" in p for p in client.paths)
    assert [e["event_type"] for e in events] == ["trader_opened"]
    assert len(written) == 1


def test_unreachable_sidecar_is_a_logged_skip_not_a_crash():
    class DeadClient:
        def get(self, path):
            raise ConnectionError("no route to host")

    assert record_trader_events(client=DeadClient()) == []


def test_trader_events_do_not_pollute_the_market_symbol_map():
    """A trader event carries its pair in the payload, never in symbol —
    otherwise the room would fold one inhabitant's trades into BTCUSDT's
    market mood and show trading activity as if it were price action."""
    from world.state import project_state

    events = diff_trader_state(
        {"open_trade_ids": [], "profit_closed_percent": 0.0},
        {"open_trades": [_trade(1)], "profit_closed_percent": 0.0},
    )
    state = project_state([{**e, "id": i, "occurred_at": e["occurred_at"].isoformat()}
                           for i, e in enumerate(events, start=1)])
    assert state["symbols"] == {}


def test_projection_wakes_the_trader_character():
    from world.state import project_state

    events = diff_trader_state(
        {"open_trade_ids": [], "profit_closed_percent": 0.0},
        {"open_trades": [_trade(1), _trade(2)], "profit_closed_percent": 0.0},
    )
    state = project_state([{**e, "id": i, "occurred_at": e["occurred_at"].isoformat()}
                           for i, e in enumerate(events, start=1)])
    assert state["trader"] is not None
    assert state["trader"]["open_trades"] == 2


def test_projection_leaves_the_trader_asleep_without_trader_events():
    from world.state import project_state

    assert project_state([])["trader"] is None
