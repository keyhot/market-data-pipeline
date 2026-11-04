"""Endpoint tests patch api.routers.world.get_world_events — the projection itself is
covered by unit tests, so these assert wiring and the established error shape.

The route also enriches its state from two Postgres lookups (the model accuracy and
the latest prices). Offline those lookups hit the suite's database refusal and the
route swallows it, which would let any test that does not stub them pass whatever the
enrichment does to the state. The `lookups` fixture stubs both at the seam the route
imports them by, so the enrichment really runs."""

import copy
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def _event(event_id, event_type="big_move", severity=6.0, payload=None):
    return {
        "id": event_id,
        "occurred_at": f"2026-07-20T12:{event_id:02d}:00+00:00",
        "event_type": event_type,
        "symbol": "BTCUSDT",
        "severity": severity,
        "payload": payload if payload is not None else {"return": 0.03},
    }


# What get_model_accuracy returns (storage/postgres_store.py). The numbers differ from
# every event fold below, so a result that lands in the wrong field, or replaces one,
# cannot pass for the fold.
_ACCURACY = {
    "window": 20,
    "resolved": 20,
    "wins": 14,
    "losses": 6,
    "hit_rate": 0.7,
    "per_symbol": {"BTCUSDT": {"resolved": 20, "wins": 14, "hit_rate": 0.7}},
}


@pytest.fixture
def lookups(monkeypatch):
    """Stub the route's two Postgres lookups instead of leaning on the suite's DB
    refusal (which the route swallows). Returns the list of symbol lists the
    accuracy lookup was called with."""
    import api.routers.world as world

    accuracy_calls = []

    def fake_accuracy(symbols):
        accuracy_calls.append(list(symbols))
        return copy.deepcopy(_ACCURACY)  # fresh each call, like a real lookup

    monkeypatch.setattr(world, "get_model_accuracy", fake_accuracy)
    monkeypatch.setattr(
        world, "get_price_bars", lambda symbol, interval="1m", limit=1: []
    )
    return accuracy_calls


def test_world_state_returns_projection(lookups):
    with patch("api.routers.world.get_world_events", return_value=[_event(1)]):
        response = client.get("/world/state")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["event_count"] == 1
    assert "BTCUSDT" in data["symbols"]
    assert data["recent"][0]["reaction"]["animation"] == "jolt"
    assert len(lookups) == 1  # the enrichment ran, and left the projection alone


def test_world_state_includes_model_record(lookups, monkeypatch):
    import api.routers.world as world

    events = [
        _event(1, "signal_resolved", 0.8, {"outcome": "win"}),
        _event(2, "signal_resolved", 1.4, {"outcome": "loss"}),
    ]
    with patch("api.routers.world.get_world_events", return_value=events):
        data = client.get("/world/state").json()["data"]

    # The record folded from the events ...
    assert data["model"]["wins"] == 1
    assert data["model"]["losses"] == 1
    # ... carries the KI-012 accuracy next to it, looked up for the predicted symbols.
    assert data["model"]["accuracy"] == _ACCURACY
    assert len(lookups) == 1 and "BTCUSDT" in lookups[0]

    # Accuracy is enrichment: when its lookup fails the room still gets the folded
    # record, only without the accuracy block.
    def pg_down(symbols):
        raise RuntimeError("pg down")

    monkeypatch.setattr(world, "get_model_accuracy", pg_down)
    with patch("api.routers.world.get_world_events", return_value=events):
        degraded = client.get("/world/state")
    assert degraded.status_code == 200
    model = degraded.json()["data"]["model"]
    assert (model["wins"], model["losses"]) == (1, 1)
    assert "accuracy" not in model


def test_world_state_404_when_empty():
    with patch("api.routers.world.get_world_events", return_value=[]):
        response = client.get("/world/state")

    assert response.status_code == 404


def test_world_state_503_when_postgres_down():
    with patch(
        "api.routers.world.get_world_events", side_effect=RuntimeError("no pool")
    ):
        response = client.get("/world/state")

    assert response.status_code == 503


def test_world_state_rejects_out_of_range_limit():
    assert client.get("/world/state?limit=0").status_code == 422
    assert client.get("/world/state?limit=99999").status_code == 422


def test_world_state_is_stable_across_identical_calls(lookups):
    with patch(
        "api.routers.world.get_world_events", return_value=[_event(1), _event(2)]
    ):
        first = client.get("/world/state").json()["data"]
        second = client.get("/world/state").json()["data"]

    first.pop("generated_at")
    second.pop("generated_at")
    assert len(lookups) == 2  # both calls went through the enrichment
    assert first == second


def test_world_state_carries_latest_prices_for_the_now_band(lookups, monkeypatch):
    """B9: the always-on band shows a price, and the room already fetches
    /world/state — carrying prices here keeps it to ONE request rather than a
    second per symbol. Each of those would be another long-lived connection in
    a browser process that already hit its per-origin limit once (KI-013)."""
    import api.routers.world as world

    monkeypatch.setattr(
        world, "get_price_bars",
        lambda symbol, interval="1m", limit=1: [{"close": 118432.5}],
    )
    with patch("api.routers.world.get_world_events", return_value=[_event(1)]):
        body = client.get("/world/state").json()
    assert body["data"]["prices"]["BTCUSDT"] == 118432.5


def test_world_state_survives_a_price_lookup_failure(monkeypatch):
    """Enrichment must never break the room: no prices is a missing line in the
    band, not a 503 and a dead stream."""
    import api.routers.world as world

    def boom(symbol, interval="1m", limit=1):
        raise RuntimeError("pg down")

    monkeypatch.setattr(world, "get_price_bars", boom)
    with patch("api.routers.world.get_world_events", return_value=[_event(1)]):
        response = client.get("/world/state")
    assert response.status_code == 200
    assert response.json()["data"]["prices"] == {}
