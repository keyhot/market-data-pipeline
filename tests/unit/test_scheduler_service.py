import threading
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

    service = SchedulerService(watchlist=make_watchlist())
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
    service = SchedulerService(watchlist=watchlist)
    with patch("scheduler.service.jobs.run_ticker_job", failing_job):
        service.start()
        try:
            assert ran.wait(timeout=5)
        finally:
            service.shutdown()

    job_status = service.status()["jobs"]["ticker:AAPL:1d"]
    assert job_status["last_error"] == "boom"
    assert "last_success" not in job_status


def test_lifespan_does_not_start_scheduler_when_disabled(monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)

    from api.main import app, scheduler_service

    with TestClient(app):
        assert scheduler_service.running is False
