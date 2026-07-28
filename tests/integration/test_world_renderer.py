"""End-to-end over the projection chain: events -> state -> endpoint -> page.
Uses patched storage, so it runs without Postgres like the rest of tests/."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from world.reactions import attach_reactions
from world.state import project_state

client = TestClient(app)


def _log():
    return [
        {"id": 3, "occurred_at": "2026-07-20T12:30:00+00:00",
         "event_type": "signal_resolved", "symbol": "BTCUSDT", "severity": 1.9,
         "payload": {"outcome": "loss", "realized_return": -0.04}},
        {"id": 2, "occurred_at": "2026-07-20T12:10:00+00:00",
         "event_type": "big_move", "symbol": "BTCUSDT", "severity": 8.0,
         "payload": {"return": -0.05, "sigmas": 8.0}},
        {"id": 1, "occurred_at": "2026-07-20T12:00:00+00:00",
         "event_type": "stream_started", "symbol": None, "severity": 1.0,
         "payload": {}},
    ]


def test_events_flow_through_projection_to_endpoint():
    with patch("api.main.get_world_events", return_value=_log()):
        data = client.get("/world/state").json()["data"]

    assert data["event_count"] == 3
    assert data["stream"]["state"] == "live"
    assert data["model"]["losses"] == 1
    assert data["symbols"]["BTCUSDT"]["mood"] == "bearish"
    assert data["history"]["worst_loss"]["realized_return"] == -0.04
    assert data["recent"][0]["reaction"]["mood"] == "dejected"


def test_endpoint_state_matches_the_pure_projection():
    """No logic may leak into the API layer — the endpoint is composition only.

    The endpoint is `attach_reactions(project_state(events))`, so the direct
    baseline applies that same composition rather than the bare projection:
    attach_reactions deliberately enriches `model` and each `symbols` entry
    with a `reaction` descriptor too, not just `recent`
    (tests/unit/test_reactions.py::test_attach_reactions_enriches_recent_without_mutating_input
    asserts `enriched["model"]["reaction"]` directly), so comparing against
    the un-enriched projection would fail on a difference that isn't leaked
    API-layer logic.
    """
    # KI-012: the endpoint additionally attaches `model.accuracy` from the
    # signals table — a DB-derived enrichment the pure event projection cannot
    # produce (it comes from resolved signals, not the append-only event fold).
    # Patch it to a sentinel so this test stays Postgres-free (per the module
    # docstring) and deterministic: we assert the endpoint attached exactly what
    # `get_model_accuracy` returned, then exclude it from the equality. The
    # invariant this test guards — that no *event-projection* logic leaks into
    # the API layer — still holds.
    accuracy = {"window": 50, "resolved": 0, "wins": 0, "losses": 0,
                "hit_rate": None, "per_symbol": {}}
    with patch("api.main.get_world_events", return_value=_log()), \
            patch("api.main.get_model_accuracy", return_value=accuracy):
        served = client.get("/world/state").json()["data"]
    direct = attach_reactions(project_state(_log()))

    served.pop("generated_at")
    direct.pop("generated_at")
    assert served["model"].pop("accuracy", None) == accuracy
    for key in ("event_count", "symbols", "model", "stream", "history"):
        assert served[key] == direct[key]


def test_the_page_embeds_every_crypto_watchlist_symbol():
    """The page filters its pillars against an embedded symbol list, so a
    watchlist symbol missing from the page renders no pillar however much
    state the projection produces for it."""
    from scheduler.watchlist import load_watchlist

    page = client.get("/world").text
    expected = {
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.market == "crypto"
    }
    assert expected, "watchlist has no crypto symbols — fixture assumption broken"
    missing = sorted(s for s in expected if s not in page)
    assert missing == [], f"symbols absent from /world: {missing}"
