import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from scheduler.service import SchedulerService, scheduler_enabled
from scheduler.watchlist import EventJobSpec, TickerJobSpec, Watchlist


def make_watchlist():
    return Watchlist(
        interval_seconds=300,
        tickers=(TickerJobSpec("AAPL", "1d"),),
        events=(EventJobSpec("AAPL", "dividends"),),
    )


def make_service(watchlist=None):
    return SchedulerService(watchlist=watchlist or make_watchlist())


def test_scheduler_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    assert scheduler_enabled() is False

    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    assert scheduler_enabled() is True

    monkeypatch.setenv("SCHEDULER_ENABLED", "0")
    assert scheduler_enabled() is False


def test_start_registers_jobs_and_runs_them_immediately():
    ran = {"ticker": threading.Event(), "events": threading.Event()}

    def fake_ticker_job(symbol, time_range):
        ran["ticker"].set()
        return {}

    def fake_event_job(symbol, event_type):
        ran["events"].set()
        return {}

    service = make_service()
    with (
        patch("scheduler.service.jobs.run_ticker_job", fake_ticker_job),
        patch("scheduler.service.jobs.run_event_job", fake_event_job),
    ):
        service.start()
        try:
            assert ran["ticker"].wait(timeout=5)
            assert ran["events"].wait(timeout=5)
            assert service.running
        finally:
            service.shutdown()

    assert not service.running

    status = service.status()
    assert "last_success" in status["jobs"]["ticker:AAPL:1d"]
    assert "last_success" in status["jobs"]["events:AAPL:dividends"]


def test_job_failure_recorded_not_fatal():
    ran = threading.Event()

    def failing_job(symbol, time_range):
        ran.set()
        raise RuntimeError("boom")

    watchlist = Watchlist(
        interval_seconds=300, tickers=(TickerJobSpec("AAPL", "1d"),), events=()
    )
    service = make_service(watchlist)
    with patch("scheduler.service.jobs.run_ticker_job", failing_job):
        service.start()
        try:
            assert ran.wait(timeout=5)
        finally:
            service.shutdown()

    job_status = service.status()["jobs"]["ticker:AAPL:1d"]
    assert job_status["last_error"] == "boom"
    assert "last_success" not in job_status


def test_start_skips_jobs_with_recent_ingestion_run(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    watchlist = Watchlist(
        interval_seconds=300, tickers=(TickerJobSpec("AAPL", "1d"),), events=()
    )
    ran = threading.Event()

    def fake_job(symbol, time_range):
        ran.set()
        return {}

    recent = {"ticker:AAPL:1d": datetime.now(UTC).isoformat()}
    with (
        patch("scheduler.service.jobs.run_ticker_job", fake_job),
        patch("scheduler.service.latest_success_times", return_value=recent),
        patch("scheduler.service.record_ingestion_run"),
    ):
        service = SchedulerService(watchlist=watchlist)
        service.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if "last_skipped" in service.status()["jobs"].get("ticker:AAPL:1d", {}):
                    break
                time.sleep(0.05)
        finally:
            service.shutdown()

    assert not ran.is_set()
    assert "last_skipped" in service.status()["jobs"]["ticker:AAPL:1d"]


def test_start_survives_unreachable_postgres(monkeypatch):
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    with (
        patch(
            "scheduler.service.latest_success_times",
            side_effect=RuntimeError("db down"),
        ),
        patch("scheduler.service.record_ingestion_run"),
        patch("scheduler.service.jobs.run_ticker_job", lambda *a: {}),
        patch("scheduler.service.jobs.run_event_job", lambda *a: {}),
    ):
        service = SchedulerService(watchlist=make_watchlist())
        service.start()
        try:
            assert service.running
        finally:
            service.shutdown()


def test_lifespan_does_not_start_scheduler_when_disabled(monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)

    from api.main import app, scheduler_service

    with TestClient(app):
        assert scheduler_service.running is False
