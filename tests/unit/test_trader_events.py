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


def test_first_observation_seeds_a_flagged_baseline():
    """An empty previous state means we've never looked. We can't attest we
    witnessed these opens, so they're emitted flagged baseline:true (learned,
    not witnessed) rather than silently — staying silent would deadlock the
    pipeline and the trader character could never wake from a cold log."""
    events = diff_trader_state({}, {"open_trades": [_trade(1), _trade(2)],
                                    "profit_closed_percent": 3.0})
    assert [e["event_type"] for e in events] == ["trader_opened", "trader_opened"]
    assert all(e["payload"]["baseline"] is True for e in events)
    assert {e["payload"]["trade_id"] for e in events} == {1, 2}


def test_normal_opens_are_not_flagged_baseline():
    events = diff_trader_state(
        {"open_trade_ids": [], "profit_closed_percent": 0.0},
        {"open_trades": [_trade(5)], "profit_closed_percent": 0.0},
    )
    assert events[0]["event_type"] == "trader_opened"
    assert "baseline" not in events[0]["payload"]


def test_closed_event_carries_the_pair_from_the_remembered_open():
    """The closed trade is gone from `current`, so its pair must come from the
    remembered open — otherwise the overlay renders 'trader closed null'."""
    previous = {"open_trade_ids": [1], "open_pairs": {1: "ETH/USDT"},
                "profit_closed_percent": 0.0}
    events = diff_trader_state(previous, {"open_trades": [],
                                          "profit_closed_percent": -2.5})
    closed = [e for e in events if e["event_type"] == "trader_closed"]
    assert closed[0]["payload"]["pair"] == "ETH/USDT"


def test_load_previous_returns_empty_on_a_cold_log(monkeypatch):
    """The deadlock hinge: an empty log yields {} (never observed). The first
    record_trader_events must still escape it via the baseline path."""
    from world.trader_events import _load_previous

    monkeypatch.setattr("world.trader_events.get_world_events",
                        lambda limit=200, event_type=None: [])
    assert _load_previous() == {}


def test_load_previous_reconstructs_open_trades_and_pairs(monkeypatch):
    from world.trader_events import _load_previous

    rows_by_type = {
        "trader_opened": [
            {"id": 1, "occurred_at": "2026-07-20T12:00:00+00:00",
             "event_type": "trader_opened",
             "payload": {"trade_id": 1, "pair": "BTC/USDT"}},
            {"id": 2, "occurred_at": "2026-07-20T12:05:00+00:00",
             "event_type": "trader_opened",
             "payload": {"trade_id": 2, "pair": "ETH/USDT"}},
        ],
        "trader_closed": [
            {"id": 3, "occurred_at": "2026-07-20T12:10:00+00:00",
             "event_type": "trader_closed",
             "payload": {"trade_id": 1, "profit_pct": -2.5}},
        ],
        "trader_milestone": [],
    }
    monkeypatch.setattr(
        "world.trader_events.get_world_events",
        lambda limit=200, event_type=None: rows_by_type[event_type],
    )
    prev = _load_previous()
    assert prev["open_trade_ids"] == [2]
    assert prev["open_pairs"] == {2: "ETH/USDT"}
    assert prev["profit_closed_percent"] == -2.5


def test_record_trader_events_wakes_from_a_cold_log(monkeypatch):
    """The bug this closes: on a cold world_events log the trader must emit its
    first event or the character can never wake. Drives the REAL _load_previous
    (cold log -> {}) end to end — the coverage whose absence hid the deadlock."""
    written = []
    monkeypatch.setattr("world.trader_events.get_world_events",
                        lambda limit=200, event_type=None: [])
    monkeypatch.setattr(
        "world.trader_events.append_world_events",
        lambda events: written.extend(events) or len(events),
    )
    client = FakeClient({"open_trades": [_trade(1), _trade(2)]},
                        {"profit_closed_percent": 0.0})
    events = record_trader_events(client=client)

    assert [e["event_type"] for e in events] == ["trader_opened", "trader_opened"]
    assert all(e["payload"]["baseline"] is True for e in events)
    assert len(written) == 2  # persisted -> the next poll is no longer a cold start


def test_an_unreadable_log_skips_the_tick_instead_of_re_emitting_everything(
    monkeypatch,
):
    """`_load_previous` used to swallow a read failure and `return {}` — which
    is byte-for-byte the state a genuine cold start produces. So one Postgres
    blip made every open trade look newly opened and re-emitted the lot into a
    log that is append-only and cannot be corrected afterwards. Note the
    contrast with the cold-log test above: the *same* empty state must wake the
    character when the read succeeded, and must emit nothing when it didn't."""
    written = []

    def boom(limit=200, event_type=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("world.trader_events.get_world_events", boom)
    monkeypatch.setattr(
        "world.trader_events.append_world_events",
        lambda events: written.extend(events) or len(events),
    )
    client = FakeClient({"open_trades": [_trade(1), _trade(2)]},
                        {"profit_closed_percent": 0.0})

    assert record_trader_events(client=client) == []
    assert written == [], "a failed read fabricated events into an append-only log"


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
