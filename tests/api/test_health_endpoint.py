import inspect
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

from api.main import app

REPO = Path(__file__).resolve().parents[2]

client = TestClient(app)


def test_health_includes_scheduler_status():
    response = client.get("/health")

    assert response.status_code == 200
    scheduler = response.json()["data"]["scheduler"]
    assert scheduler["running"] is False
    assert "enabled" in scheduler
    assert "jobs" in scheduler


def test_health_includes_postgres_status():
    response = client.get("/health")

    assert response.status_code == 200
    postgres = response.json()["data"]["postgres"]
    # Flag is off in tests, so no ping is attempted.
    assert postgres == {"enabled": False, "connected": None}


_CONNECTED = {"enabled": True, "connected": True}


def test_health_includes_world_liveness():
    now = datetime.now(timezone.utc)
    with (
        patch("api.main.postgres_status", return_value=_CONNECTED),
        patch(
            "api.main.world_liveness_times",
            return_value=(now - timedelta(minutes=3), now - timedelta(minutes=1)),
        ),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()["data"]
    world = data["world"]
    assert world["stale"] is False
    # Wall-clock read inside the handler, so bound it rather than pin it.
    assert 175 <= world["signal_age_s"] <= 185
    assert 55 <= world["event_age_s"] <= 65
    # Healthy means no reason to explain (MINOR-8's discriminator).
    assert data["world_unavailable_reason"] is None


def test_health_degrades_world_to_none_on_database_error():
    # KI-057's own endpoint must never 500 — a reader failure (DB down, or
    # any other exception, including the aware/naive TypeError a careless
    # caller could trigger) degrades this one field, not the response code.
    # `postgres_status` must report connected here, or the read is skipped
    # before it ever reaches the mocked reader (see the latency test below)
    # and this would pass vacuously against a narrowed `except`.
    with (
        patch("api.main.postgres_status", return_value=_CONNECTED),
        patch(
            "api.main.world_liveness_times",
            side_effect=RuntimeError("connection lost"),
        ),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["world"] is None
    # MINOR-8: distinguishes "Postgres answered but the read itself broke"
    # from "the gate never opened" (the next test) — KI-057's alerting
    # ticket needs to tell "we don't know right now" from "this has been
    # broken since deploy" apart, and a bare null cannot.
    assert data["world_unavailable_reason"] == "reader_error"


def test_health_skips_the_world_read_when_postgres_is_not_readable():
    # KI-024: `world_liveness_times()` opens its own bounded connection.
    # Attempting it when Postgres isn't reachable stacks a second
    # connect-timeout on top of whatever `postgres_readable()` already paid,
    # and blows the 5s budget the stream watchdog gives `/health` for a
    # content-health verdict (see
    # `tests/unit/test_db_deploy_guard.py::TestRoleProbeLatency`). Gating on
    # `postgres_readable()` is what keeps this reader off that cold path.
    with (
        patch("api.main.postgres_readable", return_value=False),
        patch("api.main.world_liveness_times") as reader,
    ):
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["world"] is None
    assert data["world_unavailable_reason"] == "not_connected"
    reader.assert_not_called()


def test_health_reads_world_liveness_when_writes_are_disabled_but_db_is_reachable():
    # MINOR-3: the gate must key off *readability*, not the *write* flag.
    # `postgres_status()` reports `connected: None` (unprobed, not
    # unreachable) whenever POSTGRES_WRITE_ENABLED is off — exactly this
    # project's default test/read-only-guard posture — so a read-only
    # deployment with a perfectly reachable database must still get a
    # value, not a permanent `null`.
    now = datetime.now(timezone.utc)
    with (
        patch(
            "api.main.postgres_status",
            return_value={"enabled": False, "connected": None},
        ),
        patch("api.main.postgres_readable", return_value=True),
        patch(
            "api.main.world_liveness_times",
            return_value=(now - timedelta(minutes=3), now - timedelta(minutes=1)),
        ),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["world"] is not None
    assert data["world"]["stale"] is False
    assert data["world_unavailable_reason"] is None


def test_health_gives_the_world_read_only_what_the_budget_has_left():
    # KI-069: the reachability probe and the world read used to carry their
    # own independent bounds, which summed past the watchdog's 5s. One
    # deadline starts before the probe, and the reader gets the remainder.
    now = datetime.now(timezone.utc)

    def slow_status():
        time.sleep(0.3)
        return _CONNECTED

    with (
        patch("api.main._HEALTH_DB_BUDGET_SECONDS", 1.0),
        patch("api.main.postgres_status", side_effect=slow_status),
        patch(
            "api.main.world_liveness_times",
            return_value=(now - timedelta(minutes=3), now - timedelta(minutes=1)),
        ) as reader,
    ):
        response = client.get("/health")

    assert response.status_code == 200
    given = reader.call_args.kwargs["timeout_seconds"]
    # The upper bound is the half that catches a reader handed the whole
    # budget; the lower one only rules out nonsense, loosely, because this
    # box is also encoding a stream when the suite runs.
    assert 0.5 <= given <= 0.71


def _health_consumers() -> dict[str, float]:
    """Every process that polls /health, and how long each waits for it."""
    from scripts.stream_watchdog import WatchdogConfig

    consumers = {"watchdog": WatchdogConfig().content_timeout_seconds}
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    for name, service in compose["services"].items():
        check = service.get("healthcheck")
        if not check or "/health" not in " ".join(check["test"]):
            continue
        # The command's own urlopen timeout, and Docker's kill timeout around
        # the command — whichever is shorter is what this consumer waits.
        urlopen = float(re.search(r"timeout=(\d+(?:\.\d+)?)", check["test"][-1])[1])
        docker = float(check["timeout"].rstrip("s"))
        consumers[f"compose:{name}"] = min(urlopen, docker)
    return consumers


def test_health_db_budget_leaves_headroom_under_every_consumer():
    # The budget is only a fix if it is below what EVERY poller waits, with a
    # second for the rest of the handler. The watchdog is not the tightest:
    # compose's healthchecks gave /health 3s, and director/broadcast wait on
    # `api: service_healthy` — a degraded database would have marked the api
    # container unhealthy under a 4s budget.
    from api import main

    consumers = _health_consumers()
    assert {"watchdog", "compose:api", "compose:scheduler"} <= set(consumers)
    for name, waits in consumers.items():
        assert main._HEALTH_DB_BUDGET_SECONDS <= waits - 1.0, name


def test_health_db_budget_covers_a_cold_unreachable_database():
    # On a cold process the role probe runs before ping's pool exists. With
    # Postgres unreachable each is bounded by its connect timeout, so both
    # together must fit, or the gate cannot even answer inside the budget.
    # (Reachable-but-slow is bounded separately: the probe carries a
    # statement_timeout of the same length, see test_db_deploy_guard.py.)
    from api import main
    from storage import db

    ping_default = inspect.signature(db.ping).parameters["timeout_seconds"].default
    cold = db._ROLE_PROBE_TIMEOUT_SECONDS + ping_default
    assert cold <= main._HEALTH_DB_BUDGET_SECONDS


def test_metrics_includes_scheduler_status():
    response = client.get("/metrics")

    assert response.status_code == 200
    data = response.json()["data"]
    assert "scheduler" in data
    assert "routes" in data


def test_metrics_includes_postgres_write_counters():
    response = client.get("/metrics")

    writes = response.json()["data"]["postgres_writes"]
    assert set(writes) == {
        "price_bars",
        "corporate_events",
        "news_items",
        "signals",
        "errors",
    }
