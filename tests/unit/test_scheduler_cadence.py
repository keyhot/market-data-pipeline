"""SchedulerService cadence under a fake clock (audit-4 F1).

The real SchedulerService (start, _add_job, _run_job, the skip guard) runs on
APScheduler's real IntervalTrigger and its real job-processing loop. Only the
thread and the sleeping are replaced: a manual scheduler is driven tick by
tick, and datetime.now reads a clock the stub job moves forward. A 50-minute
window costs milliseconds.

Every job function is a stub. The scheduler registers exactly one job, the
ticker; the others (salience, resolver, trader mirror, inference, events) are
stubs that record an escape, and the helper asserts none ran. Nothing here
opens a socket or reads a database.

The production-config tests at the end (review F1-F3) start the service on the
real config/watchlist.yaml, so all 12 jobs exist. They pin what the cadence
rests on (the 300 s interval, the job set, the executor pool, misfire grace,
coalesce, max_instances, the start stagger). One of them runs the real
BackgroundScheduler on real threads, because the manual scheduler swaps the
executor out and cannot show a job waiting for a worker.
"""

import socket
import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from apscheduler.executors.debug import DebugExecutor
from apscheduler.schedulers.base import BaseScheduler

from scheduler.service import SchedulerService
from scheduler.watchlist import TickerJobSpec, Watchlist, load_watchlist

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
INTERVAL = 300
WINDOW = 3000  # 50 minutes: fires at t0 + 300 * k for k = 0..9
JOB_ID = "ticker:AAPL:1d"

# Every module that reads the wall clock on the scheduling path.
CLOCKED_MODULES = (
    "scheduler.service",
    "apscheduler.schedulers.base",
    "apscheduler.triggers.interval",
    "apscheduler.executors.base",
)
OTHER_JOBS = (
    "run_salience_job",
    "run_resolver_job",
    "run_trader_mirror_job",
    "run_inference_job",
    "run_event_job",
)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("test_scheduler_cadence opens no sockets")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def datetime_class(self):
        clock = self

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return clock.now.replace(tzinfo=None)
                return clock.now.astimezone(tz)

        return FrozenDatetime


class ManualScheduler(BaseScheduler):
    """APScheduler's scheduling logic with no thread and no waiting."""

    def __init__(self):
        super().__init__(timezone=UTC)
        # What the service asked for, by alias. The pool itself is swapped for
        # the inline DebugExecutor so the fake clock stays single-threaded.
        self.requested_executors = {}

    def add_executor(self, executor, alias="default", **executor_opts):
        self.requested_executors[alias] = executor
        super().add_executor(DebugExecutor(), alias, **executor_opts)

    def shutdown(self, wait=True):
        super().shutdown(wait)

    def wakeup(self):
        pass

    def _create_default_executor(self):
        return DebugExecutor()


class StubJob:
    """The ticker job. Takes `duration` fake seconds; `outcomes` is a list of
    exceptions-or-None consumed per call (empty means always succeed)."""

    def __init__(self, clock: FakeClock, duration: float = 0.0, outcomes=()):
        self.clock = clock
        self.duration = duration
        self.outcomes = list(outcomes)
        self.starts: list[datetime] = []

    def __call__(self, symbol, time_range, market="equity"):
        self.starts.append(self.clock.now)
        self.clock.advance(self.duration)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return {"rows": 1}

    @property
    def offsets(self) -> list[float]:
        """Start times in seconds after t0."""
        return [(s - T0).total_seconds() for s in self.starts]


class Run:
    def __init__(self, job, statuses, record_calls, service, registered):
        self.job = job
        self.statuses = statuses
        self.record_calls = record_calls
        self.service = service
        self.registered = registered  # the job as APScheduler holds it


def run_scheduler(
    monkeypatch,
    *,
    duration: float = 0.0,
    outcomes=(),
    lateness=(0.0,),
    seconds: int = WINDOW,
    seeded_success_age: float | None = None,
) -> Run:
    """Start a real SchedulerService on a fake clock and let `seconds` pass.

    `lateness` cycles per dispatch: how many seconds after its scheduled time
    each fire is picked up (kept under the service's misfire grace, 60 s).
    `seeded_success_age` stands in for the ingestion_runs row a previous
    process left: its start was that many seconds before this start."""
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    clock = FakeClock(T0)
    job = StubJob(clock, duration, outcomes)
    escaped: list[str] = []
    seed = {}
    if seeded_success_age is not None:
        seed = {JOB_ID: (T0 - timedelta(seconds=seeded_success_age)).isoformat()}
    watchlist = Watchlist(
        interval_seconds=INTERVAL, tickers=(TickerJobSpec("AAPL", "1d"),), events=()
    )

    with ExitStack() as stack:
        for module in CLOCKED_MODULES:
            stack.enter_context(patch(f"{module}.datetime", clock.datetime_class()))
        stack.enter_context(
            patch("scheduler.service.BackgroundScheduler", ManualScheduler)
        )
        stack.enter_context(patch("scheduler.service.salience_enabled", lambda: False))
        stack.enter_context(
            patch("scheduler.service.trader_mirror_enabled", lambda: False)
        )
        stack.enter_context(patch("scheduler.service.jobs.run_ticker_job", job))
        for name in OTHER_JOBS:
            stack.enter_context(
                patch(
                    f"scheduler.service.jobs.{name}",
                    lambda *a, _name=name, **k: escaped.append(_name) or {},
                )
            )
        stack.enter_context(
            patch("scheduler.service.latest_success_times", return_value=seed)
        )
        record = stack.enter_context(patch("scheduler.service.record_ingestion_run"))

        service = SchedulerService(watchlist=watchlist)
        service.start()
        try:
            assert set(service.status()["jobs"]) == {JOB_ID}
            registered = service._scheduler.get_job(JOB_ID)
            drive(service._scheduler, clock, T0 + timedelta(seconds=seconds), lateness)
        finally:
            service.shutdown()

    assert escaped == []
    statuses = [call.args[3] for call in record.call_args_list]
    return Run(job, statuses, record.call_args_list, service, registered)


def drive(scheduler, clock, end, lateness):
    """Process due jobs, then jump to the next scheduled fire (plus lateness)
    until `end`. The clock never moves backwards."""
    tick = 0
    while True:
        wait = scheduler._process_jobs()  # seconds until the next fire, or None
        if wait is None:
            break
        next_fire = clock.now + timedelta(seconds=wait)
        if next_fire >= end:
            break
        late = timedelta(seconds=lateness[tick % len(lateness)])
        tick += 1
        clock.now = max(clock.now, next_fire + late)
    clock.now = max(clock.now, end)


def gaps(offsets: list[float]) -> list[float]:
    return [b - a for a, b in zip(offsets, offsets[1:])]


@pytest.mark.parametrize("duration", [0.0, 2.0, 30.0, 299.0])
def test_300s_job_fires_ten_times_in_fifty_minutes(monkeypatch, duration):
    run = run_scheduler(monkeypatch, duration=duration)

    assert run.job.offsets == [300.0 * k for k in range(10)]
    assert run.statuses == ["success"] * 10


def test_dispatch_jitter_does_not_skip_a_fire(monkeypatch):
    # A fire picked up 0.2 s late followed by one on time: the next run starts
    # 299.8 s after the previous start. Stamping the start instead of the
    # completion would still skip it.
    run = run_scheduler(monkeypatch, lateness=(0.2, 0.0))

    assert len(run.job.starts) == 10
    assert "skipped" not in run.statuses
    assert min(gaps(run.job.offsets)) > INTERVAL - 1


def test_a_job_longer_than_the_interval_never_runs_closer_than_the_interval(
    monkeypatch,
):
    # 400 s job: APScheduler drops the fire it was too busy for (misfire), so
    # runs land on every second grid point, never closer than the interval.
    run = run_scheduler(monkeypatch, duration=400.0)

    assert run.job.offsets == [0.0, 600.0, 1200.0, 1800.0, 2400.0]
    assert min(gaps(run.job.offsets)) >= INTERVAL
    assert "skipped" not in run.statuses


def test_restart_guard_skips_the_first_fire_after_a_recent_success(monkeypatch):
    # A previous process succeeded 200 s before this start: the startup fire is
    # skipped, the next one (300 s after start, 500 s after that success) runs,
    # and nothing after it is skipped.
    run = run_scheduler(monkeypatch, seeded_success_age=200.0, duration=2.0)

    assert run.statuses == ["skipped"] + ["success"] * 9
    assert run.job.offsets == [300.0 * k for k in range(1, 10)]
    assert run.job.offsets[0] + 200.0 >= INTERVAL


def test_restart_guard_skips_only_the_startup_fire_for_a_future_dated_success(
    monkeypatch,
):
    # A clock stepped back leaves a success "in the future". The startup fire
    # is skipped as before, but the guard must not keep skipping every later
    # fire while that stamp is still less than an interval old.
    run = run_scheduler(monkeypatch, seeded_success_age=-1000.0, duration=2.0)

    assert run.statuses == ["skipped"] + ["success"] * 9
    assert run.job.offsets == [300.0 * k for k in range(1, 10)]


def test_job_is_registered_with_the_settings_the_cadence_rests_on(monkeypatch):
    # The guard no longer spaces runs; the fixed grid does, and a slow job
    # cannot overlap itself. The harness runs jobs inline, so it cannot show
    # an overlap: pin the settings that prevent one.
    run = run_scheduler(monkeypatch, seconds=1)

    assert run.registered.trigger.interval == timedelta(seconds=INTERVAL)
    assert run.registered.max_instances == 1
    assert run.registered.coalesce is True


def test_restart_guard_lets_the_first_fire_run_after_an_old_success(monkeypatch):
    run = run_scheduler(monkeypatch, seeded_success_age=400.0, duration=2.0)

    assert run.statuses == ["success"] * 10
    assert run.job.offsets == [300.0 * k for k in range(10)]


def test_failing_job_is_retried_every_interval_and_never_stamps_success(
    monkeypatch,
):
    # Existing behaviour, pinned: an error is recorded, not stamped as a
    # success, so the next fire runs again. Green before and after the fix.
    boom = [RuntimeError("boom")] * 10
    run = run_scheduler(monkeypatch, duration=2.0, outcomes=boom)

    assert run.job.offsets == [300.0 * k for k in range(10)]
    assert run.statuses == ["error"] * 10
    assert {c.kwargs["error"] for c in run.record_calls} == {"boom"}
    job_status = run.service.status()["jobs"][JOB_ID]
    assert job_status["last_error"] == "boom"
    assert "last_success" not in job_status
    assert "last_skipped" not in job_status


def test_a_failure_does_not_delay_the_retry_or_the_run_after_it(monkeypatch):
    run = run_scheduler(
        monkeypatch, duration=2.0, outcomes=[RuntimeError("boom")], seconds=900
    )

    assert run.statuses == ["error", "success", "success"]
    assert run.job.offsets == [0.0, 300.0, 600.0]


def test_a_fire_picked_up_30_s_late_still_runs_once(monkeypatch):
    # Review F1. APScheduler's default grace is 1 s: a fire that waited for a
    # worker, or for a stalled tick, was dropped with no ingestion_runs row.
    # Within the 60 s grace it runs, once, and the grid does not move.
    run = run_scheduler(monkeypatch, lateness=(30.0,))

    assert run.statuses == ["success"] * 10
    assert run.job.offsets == [0.0] + [300.0 * k + 30.0 for k in range(1, 10)]


# --- production config: the 12 jobs, the pool, the misfire settings (F1-F3) ---

EXPECTED_JOB_IDS = {
    "ticker:AAPL:1d",
    "ticker:MSFT:1d",
    "ticker:GOOG:1d",
    "ticker:BTCUSDT:1d",
    "ticker:ETHUSDT:1d",
    "salience:BTCUSDT:1m",
    "salience:ETHUSDT:1m",
    "resolver:signals",
    "trader:mirror",
    "inference:BTCUSDT:1m",
    "inference:ETHUSDT:1m",
    "events:AAPL:dividends",
}
ALL_JOB_FUNCTIONS = ("run_ticker_job", *OTHER_JOBS)


def pool_size(executor) -> int:
    return executor._pool._max_workers


@pytest.fixture
def production_service(monkeypatch):
    """SchedulerService on the real config/watchlist.yaml with every feature
    flag on, so all 12 jobs register. Started on the manual scheduler, never
    driven: the jobs are registered and none has fired."""
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    clock = FakeClock(T0)
    with ExitStack() as stack:
        for module in CLOCKED_MODULES:
            stack.enter_context(patch(f"{module}.datetime", clock.datetime_class()))
        stack.enter_context(
            patch("scheduler.service.BackgroundScheduler", ManualScheduler)
        )
        stack.enter_context(patch("scheduler.service.salience_enabled", lambda: True))
        stack.enter_context(
            patch("scheduler.service.trader_mirror_enabled", lambda: True)
        )
        for name in ALL_JOB_FUNCTIONS:
            stack.enter_context(
                patch(f"scheduler.service.jobs.{name}", lambda *a, **k: {})
            )
        stack.enter_context(
            patch("scheduler.service.latest_success_times", return_value={})
        )
        stack.enter_context(patch("scheduler.service.record_ingestion_run"))

        service = SchedulerService(watchlist=load_watchlist())
        service.start()
        try:
            yield service
        finally:
            service.shutdown()


def test_production_config_is_twelve_jobs_on_a_300_s_interval(production_service):
    status = production_service.status()
    scheduler = production_service._scheduler

    assert set(status["jobs"]) == EXPECTED_JOB_IDS
    assert len(status["jobs"]) == 12
    assert status["interval_seconds"] == 300
    for job_id in EXPECTED_JOB_IDS:
        assert scheduler.get_job(job_id).trigger.interval == timedelta(seconds=300)


def test_every_job_has_the_misfire_coalesce_and_instance_settings(production_service):
    # max_instances=1 is also what bounds the pool: one run per job at most.
    for job_id in EXPECTED_JOB_IDS:
        job = production_service._scheduler.get_job(job_id)
        assert job.max_instances == 1, job_id
        assert job.coalesce is True, job_id
        assert job.misfire_grace_time == 60, job_id


def test_the_executor_has_one_worker_per_job(production_service):
    # F1. APScheduler's default pool is 10 workers; with 12 jobs due in the
    # same pass the 11th and 12th waited in the queue, past the 1 s grace.
    requested = production_service._scheduler.requested_executors

    assert set(requested) == {"default"}
    assert pool_size(requested["default"]) == len(EXPECTED_JOB_IDS) == 12


def test_first_fires_are_spread_out_not_simultaneous(production_service):
    # F3. All 12 used to be due in the same millisecond, so every tick sent
    # 4 Yahoo + 2 Binance + 2 freqtrade requests at once. The interval grid
    # keeps whatever spread the first fires have.
    scheduler = production_service._scheduler
    first_fires = [
        (scheduler.get_job(job_id).next_run_time - T0).total_seconds()
        for job_id in production_service._job_ids
    ]

    assert len(first_fires) == 12
    assert first_fires == sorted(first_fires)
    assert min(b - a for a, b in zip(first_fires, first_fires[1:])) >= 1.0
    assert first_fires[0] == 0.0  # the first job still starts at startup
    assert first_fires[-1] <= 30.0  # and the last one soon after


def test_an_empty_watchlist_still_starts(monkeypatch):
    # The pool is sized from the job list; ThreadPoolExecutor rejects 0 workers.
    monkeypatch.delenv("POSTGRES_WRITE_ENABLED", raising=False)
    with (
        patch("scheduler.service.BackgroundScheduler", ManualScheduler),
        patch("scheduler.service.salience_enabled", lambda: False),
        patch("scheduler.service.trader_mirror_enabled", lambda: False),
    ):
        service = SchedulerService(
            watchlist=Watchlist(interval_seconds=INTERVAL, tickers=(), events=())
        )
        service.start()
        try:
            assert service.status()["jobs"] == {}
            requested = service._scheduler.requested_executors["default"]
            assert pool_size(requested) == 1
        finally:
            service.shutdown()


def test_twelve_jobs_due_together_all_run_on_the_real_scheduler(monkeypatch):
    # F1, end to end, real BackgroundScheduler, real threads, real pool. Every
    # job stub waits for the other 11 (a 12-party barrier), so the run only
    # completes if all 12 hold a worker at once. With a pool of 10 the first
    # ten wait out the barrier timeout and fail, and the last two never join.
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "1")
    monkeypatch.setattr("scheduler.service.START_STAGGER_SECONDS", 0)
    monkeypatch.setattr("scheduler.service.salience_enabled", lambda: True)
    monkeypatch.setattr("scheduler.service.trader_mirror_enabled", lambda: True)
    monkeypatch.setattr("scheduler.service.latest_success_times", lambda: {})
    monkeypatch.setattr("scheduler.service.record_ingestion_run", lambda *a, **k: None)
    barrier = threading.Barrier(12, timeout=5)

    def rendezvous(*args, **kwargs):
        barrier.wait()
        return {}

    for name in ALL_JOB_FUNCTIONS:
        monkeypatch.setattr(f"scheduler.service.jobs.{name}", rendezvous)

    service = SchedulerService(watchlist=load_watchlist())
    service.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            jobs = service.status()["jobs"]
            if len(jobs) == 12 and all("last_success" in j for j in jobs.values()):
                break
            time.sleep(0.05)
    finally:
        service.shutdown()

    jobs = service.status()["jobs"]
    assert {j for j, r in jobs.items() if "last_success" in r} == EXPECTED_JOB_IDS
    assert not any("last_error" in r for r in jobs.values())
