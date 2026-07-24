import logging
import os
import threading
from datetime import UTC, datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scheduler import jobs
from scheduler.watchlist import Watchlist, load_watchlist
from storage.postgres_store import latest_success_times, record_ingestion_run
from storage.writes import postgres_write_enabled
from world.salience import salience_enabled
from world.trader_events import trader_mirror_enabled

SCHEDULER_ENABLED_ENV = "SCHEDULER_ENABLED"

logger = logging.getLogger(__name__)


def scheduler_enabled() -> bool:
    raw = os.environ.get(SCHEDULER_ENABLED_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes"}


class SchedulerService:
    def __init__(self, watchlist: Watchlist | None = None):
        self._watchlist = watchlist
        self._state: dict[str, str] = {}
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
        self._state = self._load_last_success()
        self._job_ids = []
        self._scheduler = BackgroundScheduler()

        for spec in watchlist.tickers:
            self._add_job(
                f"ticker:{spec.symbol}:{spec.time_range}",
                jobs.run_ticker_job,
                (spec.symbol, spec.time_range, spec.market),
                watchlist.interval_seconds,
            )
        if salience_enabled():
            crypto_symbols = dict.fromkeys(
                spec.symbol for spec in watchlist.tickers if spec.market == "crypto"
            )
            for symbol in crypto_symbols:
                self._add_job(
                    f"salience:{symbol}:1m",
                    jobs.run_salience_job,
                    (symbol, "1m"),
                    watchlist.interval_seconds,
                )
            if postgres_write_enabled():
                self._add_job(
                    "resolver:signals",
                    jobs.run_resolver_job,
                    (),
                    watchlist.interval_seconds,
                )
            if trader_mirror_enabled():
                self._add_job(
                    "trader:mirror",
                    jobs.run_trader_mirror_job,
                    (),
                    watchlist.interval_seconds,
                )
        predict_specs = dict.fromkeys(
            (spec.symbol, spec.market)
            for spec in watchlist.tickers
            if spec.predict
        )
        for symbol, market in predict_specs:
            # Crypto models run on the websocket 1m stream; equities on 1d.
            interval = "1m" if market == "crypto" else "1d"
            self._add_job(
                f"inference:{symbol}:{interval}",
                jobs.run_inference_job,
                (symbol, interval, market),
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

    def _load_last_success(self) -> dict[str, str]:
        """Seed skip-logic from ingestion_runs; on any failure start empty —
        re-running a job is safe because every write is an idempotent upsert."""
        if not postgres_write_enabled():
            return {}
        try:
            return latest_success_times()
        except Exception as e:
            logger.warning(
                "Could not load last-success times from Postgres",
                extra={"error": str(e)},
            )
            return {}

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
        started_at = datetime.now(UTC)

        if self._recently_succeeded(job_id):
            logger.info(
                "Skipping job, last success within interval",
                extra={"job_id": job_id},
            )
            self._record(job_id, "last_skipped", started_at.isoformat())
            self._record_run(job_id, started_at, "skipped")
            return

        try:
            result = func(*args)
        except Exception as e:
            self._record(job_id, "last_error", str(e))
            self._record(job_id, "last_error_at", datetime.now(UTC).isoformat())
            self._record_run(job_id, started_at, "error", error=str(e))
            logger.error(
                "Scheduled job failed", extra={"job_id": job_id, "error": str(e)}
            )
            return

        rows = result.get("rows", result.get("events")) if result else None
        self._record_run(job_id, started_at, "success", rows_written=rows)

        now_iso = datetime.now(UTC).isoformat()
        self._record(job_id, "last_success", now_iso)
        with self._lock:
            self._state[job_id] = now_iso

    def _record_run(
        self,
        job_id: str,
        started_at: datetime,
        status: str,
        rows_written: int | None = None,
        error: str | None = None,
    ) -> None:
        """Best-effort run history; never lets a Postgres hiccup fail the job."""
        if not postgres_write_enabled():
            return
        try:
            record_ingestion_run(
                job_id,
                started_at,
                datetime.now(UTC),
                status,
                rows_written=rows_written,
                error=error,
            )
        except Exception as e:
            logger.warning(
                "Failed to record ingestion run",
                extra={"job_id": job_id, "error": str(e)},
            )

    def _recently_succeeded(self, job_id: str) -> bool:
        with self._lock:
            raw = self._state.get(job_id)
        if not raw:
            return False
        try:
            last_success = datetime.fromisoformat(raw)
        except ValueError:
            return False
        elapsed = (datetime.now(UTC) - last_success).total_seconds()
        return elapsed < self._interval_seconds

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
