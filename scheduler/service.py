import logging
import os
import threading
from datetime import UTC, datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scheduler import jobs
from scheduler.watchlist import Watchlist, load_watchlist

SCHEDULER_ENABLED_ENV = "SCHEDULER_ENABLED"

logger = logging.getLogger(__name__)


def scheduler_enabled() -> bool:
    raw = os.environ.get(SCHEDULER_ENABLED_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes"}


class SchedulerService:
    def __init__(self, watchlist: Watchlist | None = None):
        self._watchlist = watchlist
        self._scheduler: BackgroundScheduler | None = None
        self._interval_seconds: int | None = None
        self._job_ids: list[str] = []
        self._job_results: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        if self.running:
            return

        watchlist = self._watchlist or load_watchlist()
        self._interval_seconds = watchlist.interval_seconds
        self._job_ids = []
        self._scheduler = BackgroundScheduler()

        for spec in watchlist.tickers:
            self._add_job(
                f"ticker:{spec.symbol}:{spec.time_range}",
                jobs.run_ticker_job,
                (spec.symbol, spec.time_range),
                watchlist.interval_seconds,
            )
        for spec in watchlist.events:
            self._add_job(
                f"events:{spec.symbol}:{spec.event_type}",
                jobs.run_event_job,
                (spec.symbol, spec.event_type),
                watchlist.interval_seconds,
            )

        self._scheduler.start()
        logger.info(
            "Scheduler started",
            extra={
                "jobs": len(self._job_ids),
                "interval_seconds": watchlist.interval_seconds,
            },
        )

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=True)
            self._scheduler = None
            logger.info("Scheduler stopped")

    def _add_job(
        self, job_id: str, func: Callable, args: tuple, interval_seconds: int
    ) -> None:
        self._job_ids.append(job_id)
        self._scheduler.add_job(
            self._run_job,
            trigger=IntervalTrigger(seconds=interval_seconds),
            args=(job_id, func, args),
            id=job_id,
            coalesce=True,
            max_instances=1,
            # First run at startup instead of waiting a full interval.
            next_run_time=datetime.now(UTC),
        )

    def _run_job(self, job_id: str, func: Callable, args: tuple) -> None:
        try:
            func(*args)
        except Exception as e:
            self._record(job_id, "last_error", str(e))
            self._record(job_id, "last_error_at", datetime.now(UTC).isoformat())
            logger.error(
                "Scheduled job failed", extra={"job_id": job_id, "error": str(e)}
            )
            return

        self._record(job_id, "last_success", datetime.now(UTC).isoformat())

    def _record(self, job_id: str, key: str, value) -> None:
        with self._lock:
            self._job_results.setdefault(job_id, {})[key] = value

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": scheduler_enabled(),
                "running": self.running,
                "interval_seconds": self._interval_seconds,
                "jobs": {
                    job_id: dict(self._job_results.get(job_id, {}))
                    for job_id in self._job_ids
                },
            }
