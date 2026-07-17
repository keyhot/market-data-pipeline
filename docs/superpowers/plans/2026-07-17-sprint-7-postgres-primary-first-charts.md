# Sprint 7 — Postgres Primary & First Charts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the L2 cutover — Postgres becomes the source of truth (mandatory writes, CSV optional, scheduler state in `ingestion_runs`) — and light up L3 with a candlestick chart page and a watchlist dashboard served straight from stored bars.

**Architecture:** The existing best-effort mirror (`storage/dual_write.py`) becomes a mandatory write path (`storage/writes.py`) that raises on failure; CSV writes move behind a `CSV_WRITE_ENABLED` flag. The scheduler's skip-logic reads last-success times from the `ingestion_runs` table instead of `data/scheduler_state.json`. Read endpoints (`/stored/events`, `/stored/news`) mirror the existing `/bars` pattern. L3 is two server-side HTML pages with zero build tooling: TradingView Lightweight Charts loaded from a CDN, data fetched client-side from `/bars`.

**Tech Stack:** Python 3.11, FastAPI, psycopg3 pool, APScheduler, pytest; TradingView Lightweight Charts (CDN standalone bundle) for L3. **No new Python dependencies.**

## Global Constraints

- Commit messages: `Type: short description` (existing types: `API:`, `Storage:`, `Scheduler:`, `QA:`, `Viz:`, `Docs:`, `CI:`, `Observability:`). **NEVER add a `Co-Authored-By: Claude` trailer** — user requirement, overrides any default.
- `CLAUDE.md` is **gitignored** in this repo — update it locally, never `git add` it.
- No new runtime dependencies; the chart library is a browser CDN script, not a Python package.
- All unit/API tests must pass offline (no Docker). Postgres-touching tests live in `tests/integration/` and auto-skip via `pytestmark = pytest.mark.skipif(not ping(), ...)`.
- `pytest` and `ruff check .` must pass at every commit.
- Env-flag parsing conventions in this codebase: existing opt-**in** flags treat `{"1","true","yes"}` as on. The two flags that **default on** after this sprint (`POSTGRES_WRITE_ENABLED`, `CSV_WRITE_ENABLED`) treat *unset* as on and `{"0","false","no"}` as off.

## Sprint tracking (Notion)

Each task below maps 1:1 to a ticket in the Sprint 7 tickets DB `7f689097-00af-83f0-bd4c-0138ec28551e` (auth: `$NOTION_TOKEN` in shell env). After a task's final commit, set its ticket Status → `Done`; set it to `In progress` when starting. Also set the Sprint 7 page (`3a089097-00af-81b4-b76a-df099eab2f9f`) Status → `In Progress` when Task 1 starts, and bump `Completion %` (done/10) as you go.

Find a ticket's page id:

```bash
curl -s -X POST "https://api.notion.com/v1/databases/7f689097-00af-83f0-bd4c-0138ec28551e/query" \
  -H "Authorization: Bearer $NOTION_TOKEN" -H "Notion-Version: 2022-06-28" \
  | python3 -c "import json,sys; [print(r['id'], ''.join(t['plain_text'] for t in r['properties']['Name']['title'])) for r in json.load(sys.stdin)['results']]"
```

Update a ticket:

```bash
curl -s -X PATCH "https://api.notion.com/v1/pages/<TICKET_PAGE_ID>" \
  -H "Authorization: Bearer $NOTION_TOKEN" -H "Notion-Version: 2022-06-28" \
  -H "Content-Type: application/json" \
  -d '{"properties": {"Status": {"status": {"name": "Done"}}}}'
```

Ticket ↔ task map:

| Task | Notion ticket |
|---|---|
| 1 | Parity check script: CSV snapshots vs Postgres rows, run before flipping defaults |
| 2 | Make Postgres writes mandatory for uncached fetches — drop best-effort, surface failures |
| 3 | Move CSV writes behind CSV_WRITE_ENABLED flag — Postgres becomes source of truth |
| 4 | Scheduler skip-logic reads ingestion_runs — retire data/scheduler_state.json |
| 5 | Stored-read endpoints for events and news from Postgres (mirror the /bars pattern) |
| 6 | Spike: choose the L3 charting stack, document the decision |
| 7 | Candlestick chart page GET /chart/{symbol} backed by stored bars |
| 8 | Watchlist dashboard page: latest close per symbol, links to charts |
| 9 | Tests for the cutover write path and chart endpoints |
| 10 | Update README and architecture docs for Postgres-primary + charts |

---

### Task 1: Parity check script (CSV vs Postgres)

Run-before-you-flip safety net: compares the latest CSV snapshot per symbol against `price_bars` rows so we know Postgres holds everything CSV holds before making it primary.

**Files:**
- Create: `scripts/check_parity.py`
- Test: `tests/unit/test_check_parity.py`

**Interfaces:**
- Consumes: `scripts.backfill_postgres._read_csv`, `_parse_name`, `_snapshots_oldest_first` (existing); `storage.postgres_store.get_price_bars(symbol, interval, limit)` (existing).
- Produces: `compare_bars(symbol: str, csv_df: pd.DataFrame, stored: list[dict]) -> list[str]` (pure, unit-tested) and a `main()` CLI. Exit 0 = parity, 1 = mismatches/unreachable.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_check_parity.py
import pandas as pd

from scripts.check_parity import compare_bars


def _csv_df():
    return pd.DataFrame(
        {"Open": [99.0, 100.5], "Close": [100.0, 101.0], "Volume": [1000, 2000]},
        index=pd.to_datetime(["2026-01-05", "2026-01-06"], utc=True),
    )


def _stored(close_2nd=101.0):
    return [
        {"timestamp": "2026-01-05T00:00:00+00:00", "close": 100.0, "volume": 1000},
        {"timestamp": "2026-01-06T00:00:00+00:00", "close": close_2nd, "volume": 2000},
    ]


def test_matching_data_reports_no_mismatches():
    assert compare_bars("AAPL", _csv_df(), _stored()) == []


def test_missing_row_is_reported():
    mismatches = compare_bars("AAPL", _csv_df(), _stored()[:1])
    assert len(mismatches) == 1
    assert "2026-01-06" in mismatches[0]
    assert "missing" in mismatches[0]


def test_close_drift_is_reported():
    mismatches = compare_bars("AAPL", _csv_df(), _stored(close_2nd=999.0))
    assert len(mismatches) == 1
    assert "close" in mismatches[0]


def test_tiny_float_noise_is_tolerated():
    assert compare_bars("AAPL", _csv_df(), _stored(close_2nd=101.0000001)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_check_parity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.check_parity'`

- [ ] **Step 3: Write the script**

```python
# scripts/check_parity.py
"""Compare the latest CSV snapshot per symbol against Postgres price_bars.

Run before flipping storage defaults (Sprint 7 cutover): exit 0 means every
bar in the newest CSV snapshot of each symbol exists in Postgres with the
same close and volume.

Usage: python scripts/check_parity.py [--data-root PATH]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_postgres import (  # noqa: E402
    _parse_name,
    _read_csv,
    _snapshots_oldest_first,
)
from storage import postgres_store  # noqa: E402
from storage.db import ping  # noqa: E402

_CLOSE_TOLERANCE = 1e-4


def compare_bars(symbol: str, csv_df: pd.DataFrame, stored: list[dict]) -> list[str]:
    """Every CSV bar must exist in `stored` with matching close and volume."""
    stored_by_ts = {pd.Timestamp(bar["timestamp"]): bar for bar in stored}
    mismatches = []
    for ts, row in csv_df.iterrows():
        bar = stored_by_ts.get(pd.Timestamp(ts))
        if bar is None:
            mismatches.append(f"{symbol} {ts.date()}: missing from Postgres")
            continue
        if abs(float(row["Close"]) - float(bar["close"])) > _CLOSE_TOLERANCE:
            mismatches.append(
                f"{symbol} {ts.date()}: close CSV={row['Close']} PG={bar['close']}"
            )
        elif int(row["Volume"]) != int(bar["volume"]):
            mismatches.append(
                f"{symbol} {ts.date()}: volume CSV={row['Volume']} PG={bar['volume']}"
            )
    return mismatches


def _latest_snapshot_per_symbol(tickers_dir: Path) -> dict[str, Path]:
    latest: dict[str, Path] = {}
    # oldest-first, so later assignments win = newest snapshot per symbol.
    for path in _snapshots_oldest_first(tickers_dir):
        symbol, _ = _parse_name(path)
        latest[symbol] = path
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "raw",
    )
    args = parser.parse_args()

    if not ping():
        print("Postgres is not reachable — is the container up? (docker compose up -d)")
        return 1

    all_mismatches: list[str] = []
    checked = 0
    for symbol, path in _latest_snapshot_per_symbol(args.data_root / "tickers").items():
        csv_df = _read_csv(path)
        if csv_df is None:
            print(f"skipped (not a valid snapshot): {path}")
            continue
        stored = postgres_store.get_price_bars(symbol, limit=len(csv_df) + 100)
        all_mismatches.extend(compare_bars(symbol, csv_df, stored))
        checked += 1

    for line in all_mismatches:
        print(f"MISMATCH: {line}")
    print(f"checked {checked} symbols, {len(all_mismatches)} mismatches")
    return 1 if all_mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_check_parity.py -v`
Expected: 4 PASS

- [ ] **Step 5: Full suite + lint**

Run: `pytest tests/unit tests/api && ruff check .`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add scripts/check_parity.py tests/unit/test_check_parity.py
git commit -m "QA: add CSV-vs-Postgres parity check script"
```

- [ ] **Step 7: If Docker is available locally, actually run it** (`docker compose up -d && python scripts/backfill_postgres.py && python scripts/check_parity.py`). Record the result in the final report. If Docker is unavailable, note that and rely on Task 9's integration pass.

---

### Task 2: Postgres writes become mandatory (drop best-effort)

`storage/dual_write.py` → `storage/writes.py`: the flag now **defaults on**, and a Postgres failure **raises** instead of being swallowed. CSV stops being the safety net.

**Files:**
- Rename: `storage/dual_write.py` → `storage/writes.py` (git mv, then edit)
- Modify: `config/exceptions.py` (add `StorageWriteError`)
- Modify: `api/main.py:19-27` (imports), `api/main.py:100-129` (`_fetch_and_store_ticker`), news/events endpoints' `mirror_*` calls
- Modify: `scheduler/jobs.py:6` (imports) and both `mirror_*` call sites
- Modify: `scheduler/service.py:13` (import of `postgres_write_enabled`)
- Rename+rewrite: `tests/unit/test_dual_write.py` → `tests/unit/test_writes.py`
- Create: `tests/conftest.py` (force flag off for the offline suite)

**Interfaces:**
- Produces (in `storage/writes.py`): `postgres_write_enabled() -> bool` (unset env → `True`), `postgres_status() -> dict`, `write_metrics() -> dict`, `write_price_bars(symbol: str, bars: pd.DataFrame) -> None`, `write_events(symbol: str, event_type: str, events: pd.DataFrame) -> None`, `write_news(symbol: str, news: pd.DataFrame) -> None`. The `write_*` functions raise `StorageWriteError` (503) on Postgres failure.
- Produces (in `config/exceptions.py`): `StorageWriteError(BaseAppException)`, default message "Failed to persist data", status 503.
- Consumed by: `api/main.py`, `scheduler/jobs.py`, `scheduler/service.py`, Tasks 3–4.

- [ ] **Step 1: Add the exception**

Append to `config/exceptions.py`:

```python
class StorageWriteError(BaseAppException):
    def __init__(self, message: str = "Failed to persist data", status_code: int = 503):
        super().__init__(message, status_code)
```

- [ ] **Step 2: Create `tests/conftest.py` so the offline suite keeps its old default (flag off)**

```python
# tests/conftest.py
import pytest


@pytest.fixture(autouse=True)
def postgres_writes_off_by_default(monkeypatch):
    """POSTGRES_WRITE_ENABLED defaults on in production; unit/API tests run
    offline, so force it off. Tests that exercise the write path re-enable it
    with monkeypatch.setenv."""
    monkeypatch.setenv("POSTGRES_WRITE_ENABLED", "0")
```

(Integration tests call `postgres_store` directly and never check the flag, so this is harmless there.)

- [ ] **Step 3: Rename the module and test file**

```bash
git mv storage/dual_write.py storage/writes.py
git mv tests/unit/test_dual_write.py tests/unit/test_writes.py
```

- [ ] **Step 4: Rewrite `tests/unit/test_writes.py` (the failing tests)**

```python
# tests/unit/test_writes.py
from unittest.mock import patch

import pandas as pd
import pytest

from config.exceptions import StorageWriteError
from storage import writes
from storage.writes import (
    POSTGRES_WRITE_ENABLED_ENV,
    postgres_status,
    postgres_write_enabled,
    write_events,
    write_metrics,
    write_news,
    write_price_bars,
)


@pytest.fixture(autouse=True)
def reset_counts():
    with writes._counts_lock:
        saved = dict(writes._counts)
        writes._counts.update(
            {"price_bars": 0, "corporate_events": 0, "news_items": 0, "errors": 0}
        )
    yield
    with writes._counts_lock:
        writes._counts.update(saved)


def _bars():
    return pd.DataFrame(
        {"Open": [1.0], "Close": [2.0]},
        index=pd.to_datetime(["2026-01-05"], utc=True),
    )


def test_flag_defaults_on_when_unset(monkeypatch):
    monkeypatch.delenv(POSTGRES_WRITE_ENABLED_ENV, raising=False)
    assert postgres_write_enabled() is True


def test_flag_explicit_off(monkeypatch):
    for value in ("0", "false", "no"):
        monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, value)
        assert postgres_write_enabled() is False


def test_write_is_noop_when_flag_disabled(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "0")

    with patch("storage.writes.postgres_store") as store:
        write_price_bars("AAPL", _bars())

    store.upsert_price_bars.assert_not_called()
    assert write_metrics()["price_bars"] == 0


def test_write_counts_rows_when_enabled(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "true")

    with patch("storage.writes.postgres_store") as store:
        store.upsert_price_bars.return_value = 5
        store.upsert_events_snapshot.return_value = 2
        store.upsert_news.return_value = 3
        write_price_bars("AAPL", _bars())
        write_events("AAPL", "dividends", _bars())
        write_news("AAPL", _bars())

    counts = write_metrics()
    assert counts["price_bars"] == 5
    assert counts["corporate_events"] == 2
    assert counts["news_items"] == 3
    assert counts["errors"] == 0


def test_write_failure_raises_storage_write_error(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "true")

    with patch("storage.writes.postgres_store") as store:
        store.upsert_price_bars.side_effect = RuntimeError("db down")
        with pytest.raises(StorageWriteError):
            write_price_bars("AAPL", _bars())

    counts = write_metrics()
    assert counts["errors"] == 1
    assert counts["price_bars"] == 0


def test_postgres_status_disabled_skips_ping(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "0")

    with patch("storage.writes.ping") as ping:
        status = postgres_status()

    ping.assert_not_called()
    assert status == {"enabled": False, "connected": None}


def test_postgres_status_enabled_reports_ping(monkeypatch):
    monkeypatch.setenv(POSTGRES_WRITE_ENABLED_ENV, "1")

    with patch("storage.writes.ping", return_value=True):
        assert postgres_status() == {"enabled": True, "connected": True}
```

- [ ] **Step 5: Run to verify failure**

Run: `pytest tests/unit/test_writes.py -v`
Expected: FAIL — imports of `write_price_bars` etc. don't exist yet

- [ ] **Step 6: Rewrite `storage/writes.py`**

```python
# storage/writes.py
"""Mandatory Postgres write path (Sprint 7 cutover).

Postgres is the source of truth: POSTGRES_WRITE_ENABLED defaults on, and a
write failure raises StorageWriteError instead of being swallowed. Set the
flag to 0/false/no only for offline development without a database.
"""

import logging
import os
import threading

import pandas as pd

from config.exceptions import StorageWriteError
from storage import postgres_store
from storage.db import ping

POSTGRES_WRITE_ENABLED_ENV = "POSTGRES_WRITE_ENABLED"

logger = logging.getLogger(__name__)

_counts = {"price_bars": 0, "corporate_events": 0, "news_items": 0, "errors": 0}
_counts_lock = threading.Lock()


def postgres_write_enabled() -> bool:
    raw = os.environ.get(POSTGRES_WRITE_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no"}


def postgres_status() -> dict:
    enabled = postgres_write_enabled()
    return {"enabled": enabled, "connected": ping() if enabled else None}


def write_metrics() -> dict:
    with _counts_lock:
        return dict(_counts)


def write_price_bars(symbol: str, bars: pd.DataFrame) -> None:
    _write(
        "price_bars",
        lambda: postgres_store.upsert_price_bars(
            symbol, postgres_store.BAR_INTERVAL, bars
        ),
    )


def write_events(symbol: str, event_type: str, events: pd.DataFrame) -> None:
    _write(
        "corporate_events",
        lambda: postgres_store.upsert_events_snapshot(
            symbol, str(event_type), events
        ),
    )


def write_news(symbol: str, news: pd.DataFrame) -> None:
    _write("news_items", lambda: postgres_store.upsert_news(symbol, news))


def _write(table: str, write) -> None:
    if not postgres_write_enabled():
        return
    try:
        written = write()
    except Exception as e:
        with _counts_lock:
            _counts["errors"] += 1
        logger.error(
            "Postgres write failed", extra={"table": table, "error": str(e)}
        )
        raise StorageWriteError(f"Failed to persist {table}: {e}") from e
    with _counts_lock:
        _counts[table] += written
```

- [ ] **Step 7: Update the three call-site modules**

In `api/main.py` replace the import block:

```python
from storage.writes import (
    postgres_status,
    write_events,
    write_metrics,
    write_news,
    write_price_bars,
)
```

and each call: `mirror_price_bars(...)` → `write_price_bars(...)` (in `_fetch_and_store_ticker`), `mirror_news(...)` → `write_news(...)` (in `news`), `mirror_events(...)` → `write_events(...)` (in `event`). **Move the Postgres write above the CSV write in each block** (source of truth writes first):

```python
    if not was_cached:
        write_price_bars(ticker_symbol, data)
        path = raw_data_path(ticker_symbol, time_range)
        save_csv(path, data)
        logger.info("Stored ticker data", extra={"file_path": path})
        result["file_path"] = str(path)
```

In `scheduler/jobs.py`: `from storage.writes import write_events, write_price_bars`, same call renames and same ordering (Postgres write before `save_csv`).

In `scheduler/service.py`: `from storage.writes import postgres_write_enabled`.

- [ ] **Step 8: Run the whole offline suite**

Run: `pytest tests/unit tests/api -v && ruff check .`
Expected: all PASS (the new `tests/conftest.py` keeps API/scheduler tests offline). If any test still patches `storage.dual_write` or `mirror_*` names (check `tests/unit/test_scheduler_jobs.py` with `grep -rn "mirror_\|dual_write" tests/`), update those patch targets to `scheduler.jobs.write_price_bars` / `scheduler.jobs.write_events` etc.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Storage: make Postgres writes mandatory — dual_write becomes writes, failures raise"
```

---

### Task 3: CSV writes behind `CSV_WRITE_ENABLED`

**Files:**
- Modify: `storage/filesystem.py` (add flag helper)
- Modify: `api/main.py` (`_fetch_and_store_ticker`, `news`, `event` — guard CSV block)
- Modify: `scheduler/jobs.py` (both jobs — guard CSV block)
- Test: `tests/unit/test_filesystem.py` (flag tests), `tests/api/test_ticker_endpoint.py` (one new endpoint test)

**Interfaces:**
- Produces: `csv_write_enabled() -> bool` and `CSV_WRITE_ENABLED_ENV = "CSV_WRITE_ENABLED"` in `storage/filesystem.py`. Unset → `True`; `0/false/no` → `False`.
- Behavior change: when the flag is off, no CSV file is written and responses/job results omit `file_path`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_filesystem.py`:

```python
from storage.filesystem import CSV_WRITE_ENABLED_ENV, csv_write_enabled


def test_csv_writes_default_on(monkeypatch):
    monkeypatch.delenv(CSV_WRITE_ENABLED_ENV, raising=False)
    assert csv_write_enabled() is True


def test_csv_writes_explicit_off(monkeypatch):
    for value in ("0", "false", "no"):
        monkeypatch.setenv(CSV_WRITE_ENABLED_ENV, value)
        assert csv_write_enabled() is False
```

Append to `tests/api/test_ticker_endpoint.py` (the file already imports `patch`, `pd`, and `client`):

```python
@patch("api.main.fetch_ticker_async")
def test_ticker_skips_csv_when_flag_disabled(mock_fetch, monkeypatch):
    monkeypatch.setenv("CSV_WRITE_ENABLED", "0")
    mock_fetch.return_value = pd.DataFrame({"Close": [100, 101]})

    with patch("api.main.save_csv") as save:
        response = client.get("/ticker/AAPL/1d")

    assert response.status_code == 200
    save.assert_not_called()
    assert "file_path" not in response.json()["data"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_filesystem.py tests/api/test_ticker_endpoint.py -v`
Expected: FAIL — `csv_write_enabled` doesn't exist

- [ ] **Step 3: Implement the flag**

Append to `storage/filesystem.py`:

```python
import os

CSV_WRITE_ENABLED_ENV = "CSV_WRITE_ENABLED"


def csv_write_enabled() -> bool:
    raw = os.environ.get(CSV_WRITE_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no"}
```

(Put the `import os` at the top of the file with the other imports, not inline.)

- [ ] **Step 4: Guard every CSV call site**

`api/main.py` — import `csv_write_enabled` from `storage.filesystem`; each uncached block becomes:

```python
    if not was_cached:
        write_price_bars(ticker_symbol, data)
        if csv_write_enabled():
            path = raw_data_path(ticker_symbol, time_range)
            save_csv(path, data)
            logger.info("Stored ticker data", extra={"file_path": path})
            result["file_path"] = str(path)
```

Same shape in `news` (guard `news_store.save(...)` + `file_path`) and `event`. `scheduler/jobs.py` — same guard in `run_ticker_job` and `run_event_job`.

- [ ] **Step 5: Run suite + lint**

Run: `pytest tests/unit tests/api && ruff check .`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Storage: put CSV writes behind CSV_WRITE_ENABLED — Postgres is the source of truth"
```

---

### Task 4: Scheduler skip-logic from `ingestion_runs`; retire the JSON state file

**Files:**
- Modify: `storage/postgres_store.py` (add `latest_success_times()`)
- Modify: `scheduler/service.py` (drop `state_path`/`load_state`/`save_state`/`DEFAULT_STATE_PATH`; load last-success from Postgres)
- Delete: `storage/state.py`, `tests/unit/test_state.py`, `data/scheduler_state.json` (if present)
- Test: `tests/unit/test_scheduler_service.py` (rework), `tests/integration/test_postgres_store.py` (one new test)

**Interfaces:**
- Produces: `postgres_store.latest_success_times() -> dict[str, str]` — job_id → ISO timestamp of the newest `status = 'success'` row in `ingestion_runs`.
- Changes: `SchedulerService.__init__(self, watchlist: Watchlist | None = None)` — the `state_path` parameter is **removed**. In-memory `self._state: dict[str, str]` is seeded from `latest_success_times()` at `start()` (empty on flag-off or Postgres failure — jobs then just run, which is safe because writes are idempotent).

- [ ] **Step 1: Add the store reader (integration test first)**

Append to `tests/integration/test_postgres_store.py`:

```python
def test_latest_success_times_returns_newest_success_per_job():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    postgres_store.record_ingestion_run(
        TEST_JOB_ID, now - timedelta(hours=2), now - timedelta(hours=2), "success"
    )
    postgres_store.record_ingestion_run(TEST_JOB_ID, now, now, "success")
    postgres_store.record_ingestion_run(TEST_JOB_ID, now, now, "error", error="x")

    times = postgres_store.latest_success_times()

    assert times[TEST_JOB_ID] == now.isoformat()
```

Append to `storage/postgres_store.py`:

```python
def latest_success_times() -> dict[str, str]:
    """job_id -> ISO timestamp of its newest successful ingestion run."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT job_id, max(started_at) FROM ingestion_runs"
            " WHERE status = 'success' GROUP BY job_id"
        ).fetchall()
    return {job_id: ts.isoformat() for job_id, ts in rows}
```

Run: `pytest tests/integration/ -v` — PASSES with Docker up, auto-SKIPs without (either is acceptable here; Task 9 guarantees a real run).

- [ ] **Step 2: Rework the scheduler service tests (failing first)**

In `tests/unit/test_scheduler_service.py`:
- `make_service` drops `tmp_path`/`state_path`: `def make_service(watchlist=None): return SchedulerService(watchlist=watchlist or make_watchlist())` — update all callers (`tmp_path` fixture args go away).
- Existing run/failure tests need `record_ingestion_run` patched out is *not* required — the conftest flag-off makes `_record_run` a no-op — but keep them updated for the new constructor.
- Replace `test_restart_skips_recently_fetched_jobs` with:

```python
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
```

Run: `pytest tests/unit/test_scheduler_service.py -v`
Expected: FAIL — constructor still requires/accepts `state_path`, `latest_success_times` not imported in `scheduler.service`

- [ ] **Step 3: Rewrite `scheduler/service.py` state handling**

- Delete imports of `Path`, `load_state`, `save_state`; delete `DEFAULT_STATE_PATH`.
- Import: `from storage.postgres_store import latest_success_times, record_ingestion_run`.
- Constructor: `def __init__(self, watchlist: Watchlist | None = None):` — drop `_state_path`.
- In `start()`, replace `self._state = load_state(self._state_path)` with `self._state = self._load_last_success()` and add:

```python
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
```

- In `_run_job` success branch, replace the save_state block with in-memory only:

```python
        now_iso = datetime.now(UTC).isoformat()
        self._record(job_id, "last_success", now_iso)
        with self._lock:
            self._state[job_id] = now_iso
```

- [ ] **Step 4: Delete the retired state layer**

```bash
git rm storage/state.py tests/unit/test_state.py
rm -f data/scheduler_state.json
```

- [ ] **Step 5: Run suite + lint**

Run: `pytest tests/unit tests/api && ruff check .`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Scheduler: seed skip-logic from ingestion_runs, retire scheduler_state.json"
```

---

### Task 5: Stored-read endpoints for events and news

Mirror the `/bars` pattern: `/stored/events/{symbol}/{event_type}` and `/stored/news/{symbol}` read straight from Postgres, 404 when empty, 503 when the DB is unreachable.

**Files:**
- Modify: `storage/postgres_store.py` (add `get_corporate_events`, `get_news_items`)
- Modify: `api/main.py` (two endpoints + imports)
- Test: `tests/api/test_stored_endpoints.py` (new), `tests/integration/test_postgres_store.py` (two new tests)

**Interfaces:**
- Produces: `get_corporate_events(symbol: str, event_type: str | None = None, limit: int = 100) -> list[dict]` — dicts `{"date": "2026-02-10", "event_type": "dividends", "value": 0.25}`, oldest first; `event_type=None` returns all types (used for `actions`).
- Produces: `get_news_items(symbol: str, limit: int = 20) -> list[dict]` — dicts `{"id", "title", "publisher", "url", "published_at" (ISO or None), "summary"}`, newest first.

- [ ] **Step 1: Write the failing API tests**

```python
# tests/api/test_stored_endpoints.py
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)

EVENTS = [{"date": "2026-02-10", "event_type": "dividends", "value": 0.25}]
NEWS = [
    {
        "id": "story-1",
        "title": "AAPL soars",
        "publisher": "Wire",
        "url": "https://example.com/1",
        "published_at": "2026-03-01T00:00:00+00:00",
        "summary": "s1",
    }
]


def test_stored_events_returns_rows():
    with patch("api.main.get_corporate_events", return_value=EVENTS) as reader:
        response = client.get("/stored/events/aapl/dividends?limit=50")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ticker"] == "AAPL"
    assert data["count"] == 1
    assert data["events"] == EVENTS
    reader.assert_called_once_with("aapl", event_type="dividends", limit=50)


def test_stored_events_actions_reads_all_types():
    with patch("api.main.get_corporate_events", return_value=EVENTS) as reader:
        response = client.get("/stored/events/AAPL/actions")

    assert response.status_code == 200
    reader.assert_called_once_with("AAPL", event_type=None, limit=100)


def test_stored_events_404_when_empty():
    with patch("api.main.get_corporate_events", return_value=[]):
        assert client.get("/stored/events/ZZ/dividends").status_code == 404


def test_stored_events_503_when_postgres_down():
    with patch("api.main.get_corporate_events", side_effect=RuntimeError("down")):
        assert client.get("/stored/events/AAPL/dividends").status_code == 503


def test_stored_events_rejects_unknown_type():
    assert client.get("/stored/events/AAPL/earnings").status_code == 422


def test_stored_news_returns_rows():
    with patch("api.main.get_news_items", return_value=NEWS) as reader:
        response = client.get("/stored/news/aapl?limit=5")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ticker"] == "AAPL"
    assert data["count"] == 1
    assert data["items"] == NEWS
    reader.assert_called_once_with("aapl", limit=5)


def test_stored_news_404_when_empty():
    with patch("api.main.get_news_items", return_value=[]):
        assert client.get("/stored/news/ZZ").status_code == 404


def test_stored_news_503_when_postgres_down():
    with patch("api.main.get_news_items", side_effect=RuntimeError("down")):
        assert client.get("/stored/news/AAPL").status_code == 503
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/api/test_stored_endpoints.py -v`
Expected: FAIL — cannot import `get_corporate_events` in `api.main`

- [ ] **Step 3: Implement the store readers**

Append to `storage/postgres_store.py`:

```python
def get_corporate_events(
    symbol: str, event_type: str | None = None, limit: int = 100
) -> list[dict]:
    """Latest `limit` events for a symbol, oldest first. None = all types."""
    sql = (
        "SELECT event_date, event_type, value FROM corporate_events"
        " WHERE symbol = %s"
    )
    params: list = [symbol.upper()]
    if event_type is not None:
        sql += " AND event_type = %s"
        params.append(event_type)
    sql += " ORDER BY event_date DESC LIMIT %s"
    params.append(limit)

    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {"date": event_date.isoformat(), "event_type": kind, "value": _as_float(value)}
        for event_date, kind, value in reversed(rows)
    ]


def get_news_items(symbol: str, limit: int = 20) -> list[dict]:
    """Latest `limit` news items for a symbol, newest first."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, title, publisher, url, published_at, summary"
            " FROM news_items WHERE symbol = %s"
            " ORDER BY published_at DESC NULLS LAST LIMIT %s",
            (symbol.upper(), limit),
        ).fetchall()
    return [
        {
            "id": id_,
            "title": title,
            "publisher": publisher,
            "url": url,
            "published_at": published_at.isoformat() if published_at else None,
            "summary": summary,
        }
        for id_, title, publisher, url, published_at, summary in rows
    ]
```

- [ ] **Step 4: Implement the endpoints**

In `api/main.py`, extend the postgres_store import:

```python
from storage.postgres_store import (
    BAR_INTERVAL,
    get_corporate_events,
    get_news_items,
    get_price_bars,
)
```

Add after the `bars` endpoint:

```python
@app.get("/stored/events/{ticker_symbol}/{event_type}", response_model=ApiResponse)
def stored_events(
    ticker_symbol: str,
    event_type: EventType,
    limit: int = Query(100, ge=1, le=1000),
):
    stored_type = None if event_type == EventType.ACTIONS else str(event_type)
    try:
        events = get_corporate_events(
            ticker_symbol, event_type=stored_type, limit=limit
        )
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    if not events:
        raise NoDataFoundError("No events stored for the given parameters")

    return ApiResponse(
        status=200,
        data={
            "ticker": ticker_symbol.upper(),
            "event_type": event_type,
            "count": len(events),
            "events": events,
        },
    )


@app.get("/stored/news/{ticker_symbol}", response_model=ApiResponse)
def stored_news(ticker_symbol: str, limit: int = Query(20, ge=1, le=100)):
    try:
        items = get_news_items(ticker_symbol, limit=limit)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    if not items:
        raise NoDataFoundError("No news stored for the given parameters")

    return ApiResponse(
        status=200,
        data={
            "ticker": ticker_symbol.upper(),
            "count": len(items),
            "items": items,
        },
    )
```

- [ ] **Step 5: Add integration tests for the readers**

Append to `tests/integration/test_postgres_store.py`:

```python
def test_get_corporate_events_filters_and_orders():
    events = pd.DataFrame(
        {"dividends": [0.25, 0.26]},
        index=pd.to_datetime(["2026-02-10", "2026-05-10"], utc=True),
    )
    postgres_store.upsert_corporate_events(BARS_SYMBOL, "dividends", events)

    stored = postgres_store.get_corporate_events(BARS_SYMBOL, event_type="dividends")

    assert [e["value"] for e in stored] == [0.25, 0.26]
    assert postgres_store.get_corporate_events(BARS_SYMBOL, event_type="splits") == []
    assert len(postgres_store.get_corporate_events(BARS_SYMBOL)) == 2


def test_get_news_items_newest_first():
    news = pd.DataFrame(
        {
            "id": ["story-1", "story-2"],
            "title": ["old", "new"],
            "publisher": ["Wire", "Wire"],
            "url": ["https://example.com/1", "https://example.com/2"],
            "published_at": pd.to_datetime(["2026-03-01", "2026-03-02"], utc=True),
            "summary": ["s1", "s2"],
        }
    )
    postgres_store.upsert_news(BARS_SYMBOL, news)

    items = postgres_store.get_news_items(BARS_SYMBOL)

    assert [i["title"] for i in items] == ["new", "old"]
```

- [ ] **Step 6: Run suite + lint**

Run: `pytest tests/unit tests/api tests/integration && ruff check .`
Expected: unit/API PASS; integration PASS or SKIP (no Docker)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "API: add /stored/events and /stored/news read endpoints from Postgres"
```

---

### Task 6: Charting-stack spike (decision doc)

Docs-only task. The architecture vision already names TradingView Lightweight Charts for L4 overlays — this spike confirms it for L3 and writes the decision down.

**Files:**
- Create: `docs/charting-stack-decision.md`

- [ ] **Step 1: Write the decision doc**

```markdown
# L3 Charting Stack Decision

**Decision: TradingView Lightweight Charts** (standalone CDN bundle), rendered
client-side in plain HTML pages served by FastAPI. Data comes from the existing
`/bars/{symbol}` JSON endpoint — no server-side templating engine, no build step,
no new Python dependencies.

## Why

- Candlestick-native: OHLC series are a first-class type, not a plugin.
- Tiny (~45 KB gzipped) and dependency-free; loads from a CDN `<script>` tag.
- Already the pick for the L4 stream overlays in docs/architecture-vision.md —
  choosing it now means L3 pages are directly reusable as stream scenes.
- Battle-tested at TradingView; handles pan/zoom/crosshair out of the box.

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Plotly.js | Great candlesticks, but ~3.5 MB and pulls a large API surface for two pages. |
| Chart.js + chartjs-chart-financial | Financial charts live in a plugin with sparse maintenance. |
| Apache ECharts | Capable, but heavier and the candlestick styling fights the defaults. |
| Server-rendered PNG (matplotlib/mplfinance) | No interactivity; dead end for the live L4 overlays. |

## Constraints

- License: Apache 2.0 **with an attribution requirement** — the TradingView
  attribution link must stay visible on chart pages.
- CDN script means chart pages need internet access in the browser. Acceptable
  for L3; revisit (vendor the file) when the L4 stream box goes always-on.
- Version pinned exactly (`lightweight-charts@4.2.0`) with a Subresource
  Integrity hash on the script tag, so a compromised or shifted CDN file
  fails closed instead of executing.

## How it's wired (Sprint 7)

`GET /chart/{symbol}` serves `api/templates/chart.html` with the symbol
substituted; the page fetches `/bars/{symbol}?limit=250` and feeds a
candlestick series. `GET /dashboard` is a server-rendered table (no JS library)
linking to per-symbol charts.
```

- [ ] **Step 2: Commit**

```bash
git add docs/charting-stack-decision.md
git commit -m "Docs: record L3 charting stack decision (Lightweight Charts via CDN)"
```

---

### Task 7: Candlestick chart page `GET /chart/{symbol}`

**Files:**
- Create: `api/templates/chart.html`
- Modify: `api/main.py` (endpoint + `HTMLResponse` import + symbol validation)
- Test: `tests/api/test_chart_pages.py` (new)

**Interfaces:**
- Consumes: `/bars/{symbol}` JSON endpoint (client-side fetch).
- Produces: `GET /chart/{ticker_symbol}` → `HTMLResponse`; invalid symbols (anything outside `A-Z 0-9 . ^ - =`, max 15 chars after uppercasing) → 400 via `BaseAppException`. Template placeholder contract: the literal string `__SYMBOL__` in `chart.html` is replaced server-side. Also produces `_render_template(name: str, replacements: dict[str, str]) -> str` and `_validated_symbol(raw: str) -> str` helpers reused by Task 8.

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_chart_pages.py
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_chart_page_serves_html_with_symbol():
    response = client.get("/chart/aapl")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AAPL" in response.text
    assert "/bars/AAPL" in response.text
    assert "__SYMBOL__" not in response.text


def test_chart_page_rejects_injection_attempts():
    assert client.get("/chart/%3Cscript%3E").status_code == 400
    assert client.get("/chart/AAPL%22%3E").status_code == 400


def test_chart_page_rejects_overlong_symbol():
    assert client.get("/chart/" + "A" * 16).status_code == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/api/test_chart_pages.py -v`
Expected: FAIL — 404, no `/chart` route

- [ ] **Step 3: Create the template**

```html
<!-- api/templates/chart.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__SYMBOL__ — Market Data Pipeline</title>
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; background: #131722; color: #d1d4dc;
           font-family: system-ui, sans-serif; }
    header { display: flex; align-items: baseline; gap: 1rem;
             padding: 1rem 1.5rem; }
    h1 { margin: 0; font-size: 1.4rem; }
    a { color: #5b9cf6; text-decoration: none; }
    #chart { height: 70vh; margin: 0 1.5rem; }
    #status { padding: 0.5rem 1.5rem; color: #787b86; }
    footer { padding: 1rem 1.5rem; font-size: 0.8rem; color: #787b86; }
  </style>
</head>
<body>
  <header>
    <h1>__SYMBOL__</h1>
    <a href="/dashboard">← dashboard</a>
  </header>
  <div id="chart"></div>
  <p id="status">Loading bars…</p>
  <footer>
    Charting by <a href="https://www.tradingview.com/">TradingView</a>
    Lightweight Charts
  </footer>
  <!-- integrity hash computed in Step 4 below — replace SRI_HASH_FROM_STEP_4 -->
  <script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
          integrity="sha384-SRI_HASH_FROM_STEP_4"
          crossorigin="anonymous"></script>
  <script>
    const SYMBOL = "__SYMBOL__";
    const status = document.getElementById("status");

    async function draw() {
      const response = await fetch(`/bars/${SYMBOL}?limit=250`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent =
          body.message || `No stored bars for ${SYMBOL} (HTTP ${response.status})`;
        return;
      }
      const { data } = await response.json();
      const candles = data.bars.map((bar) => ({
        time: bar.timestamp.slice(0, 10),
        open: bar.open, high: bar.high, low: bar.low, close: bar.close,
      }));

      const chart = LightweightCharts.createChart(
        document.getElementById("chart"),
        {
          layout: { background: { color: "#131722" }, textColor: "#d1d4dc" },
          grid: {
            vertLines: { color: "#1e222d" },
            horzLines: { color: "#1e222d" },
          },
          autoSize: true,
        }
      );
      chart.addCandlestickSeries().setData(candles);
      chart.timeScale().fitContent();
      status.textContent = `${data.count} daily bars from Postgres`;
    }

    draw().catch((err) => { status.textContent = `Failed to load: ${err}`; });
  </script>
</body>
</html>
```

- [ ] **Step 4: Fill in the Subresource Integrity hash**

SRI protects the page if the CDN is ever compromised; it requires the exact-pinned version (`4.2.0`, not `@4`). Compute the hash and replace `SRI_HASH_FROM_STEP_4` in `chart.html` with the output:

```bash
curl -s https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js \
  | openssl dgst -sha384 -binary | openssl base64 -A
```

Then verify in a browser (or via Task 9's smoke) that the script still loads — a console error `Failed to find a valid digest` means the hash was pasted wrong.

- [ ] **Step 5: Implement the endpoint**

In `api/main.py` add imports and helpers:

```python
import re
from pathlib import Path

from fastapi.responses import HTMLResponse, JSONResponse

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,15}$")


def _validated_symbol(raw: str) -> str:
    symbol = raw.upper()
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise BaseAppException(f"Invalid symbol: {raw!r}", status_code=400)
    return symbol


def _render_template(name: str, replacements: dict[str, str]) -> str:
    html = (_TEMPLATES_DIR / name).read_text()
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    return html
```

Add the endpoint:

```python
@app.get("/chart/{ticker_symbol}", response_class=HTMLResponse)
def chart(ticker_symbol: str):
    symbol = _validated_symbol(ticker_symbol)
    return HTMLResponse(_render_template("chart.html", {"__SYMBOL__": symbol}))
```

(The regex whitelist is what makes the string substitution safe — no user text outside `[A-Z0-9.^=-]` ever reaches the HTML.)

- [ ] **Step 6: Run suite + lint**

Run: `pytest tests/api && ruff check .`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Viz: add /chart/{symbol} candlestick page backed by stored bars"
```

---

### Task 8: Watchlist dashboard page `GET /dashboard`

**Files:**
- Modify: `storage/postgres_store.py` (add `get_latest_closes`)
- Create: `api/templates/dashboard.html`
- Modify: `api/main.py` (endpoint + `load_watchlist` import)
- Test: `tests/api/test_chart_pages.py` (extend), `tests/integration/test_postgres_store.py` (one new test)

**Interfaces:**
- Consumes: `_render_template` from Task 7; `scheduler.watchlist.load_watchlist() -> Watchlist` (existing; `watchlist.tickers` is a tuple of specs with `.symbol`).
- Produces: `postgres_store.get_latest_closes(symbols: list[str]) -> list[dict]` — one dict per symbol that has bars: `{"symbol": "AAPL", "timestamp": ISO, "close": 123.45}`. Endpoint `GET /dashboard` → HTML table, one row per watchlist symbol (symbols without bars show "—"), each linking to `/chart/{symbol}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_chart_pages.py`:

```python
from unittest.mock import patch

from scheduler.watchlist import TickerJobSpec, Watchlist


def _watchlist():
    return Watchlist(
        interval_seconds=300,
        tickers=(TickerJobSpec("AAPL", "1d"), TickerJobSpec("MSFT", "1d")),
        events=(),
    )


def test_dashboard_lists_watchlist_symbols_with_closes():
    closes = [
        {"symbol": "AAPL", "timestamp": "2026-07-16T00:00:00+00:00", "close": 231.5}
    ]
    with (
        patch("api.main.load_watchlist", return_value=_watchlist()),
        patch("api.main.get_latest_closes", return_value=closes) as reader,
    ):
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "231.5" in response.text
    assert 'href="/chart/AAPL"' in response.text
    # MSFT has no stored bars yet — still listed, with a placeholder.
    assert 'href="/chart/MSFT"' in response.text
    assert "—" in response.text
    reader.assert_called_once_with(["AAPL", "MSFT"])


def test_dashboard_503_when_postgres_down():
    with (
        patch("api.main.load_watchlist", return_value=_watchlist()),
        patch("api.main.get_latest_closes", side_effect=RuntimeError("down")),
    ):
        assert client.get("/dashboard").status_code == 503
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/api/test_chart_pages.py -v`
Expected: new tests FAIL — no `/dashboard` route

- [ ] **Step 3: Implement the store reader (+ integration test)**

Append to `storage/postgres_store.py`:

```python
def get_latest_closes(symbols: list[str]) -> list[dict]:
    """Newest daily close per symbol; symbols without bars are absent."""
    if not symbols:
        return []
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (symbol) symbol, bar_timestamp, close"
            " FROM price_bars WHERE symbol = ANY(%s) AND interval = %s"
            " ORDER BY symbol, bar_timestamp DESC",
            ([s.upper() for s in symbols], BAR_INTERVAL),
        ).fetchall()
    return [
        {"symbol": symbol, "timestamp": ts.isoformat(), "close": _as_float(close)}
        for symbol, ts, close in rows
    ]
```

Append to `tests/integration/test_postgres_store.py`:

```python
def test_get_latest_closes_returns_newest_bar_per_symbol():
    postgres_store.upsert_price_bars(BARS_SYMBOL, "1d", _bars())

    closes = postgres_store.get_latest_closes([BARS_SYMBOL, "ZZABSENT"])

    assert len(closes) == 1
    assert closes[0]["symbol"] == BARS_SYMBOL
    assert closes[0]["close"] == 101.0
```

- [ ] **Step 4: Create the template**

```html
<!-- api/templates/dashboard.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Watchlist — Market Data Pipeline</title>
  <style>
    :root { color-scheme: dark; }
    body { margin: 0; background: #131722; color: #d1d4dc;
           font-family: system-ui, sans-serif; }
    header { padding: 1rem 1.5rem; }
    h1 { margin: 0; font-size: 1.4rem; }
    table { border-collapse: collapse; margin: 0 1.5rem 1.5rem; min-width: 28rem; }
    th, td { text-align: left; padding: 0.5rem 1.25rem 0.5rem 0;
             border-bottom: 1px solid #1e222d; }
    th { color: #787b86; font-weight: 500; }
    td.num { font-variant-numeric: tabular-nums; }
    a { color: #5b9cf6; text-decoration: none; }
  </style>
</head>
<body>
  <header><h1>Watchlist</h1></header>
  <table>
    <thead>
      <tr><th>Symbol</th><th>Latest close</th><th>As of</th></tr>
    </thead>
    <tbody>
__ROWS__
    </tbody>
  </table>
</body>
</html>
```

- [ ] **Step 5: Implement the endpoint**

In `api/main.py` add `from scheduler.watchlist import load_watchlist` and extend the postgres_store import with `get_latest_closes`. Add:

```python
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    symbols = list(
        dict.fromkeys(spec.symbol.upper() for spec in load_watchlist().tickers)
    )
    try:
        closes = {row["symbol"]: row for row in get_latest_closes(symbols)}
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    rows = []
    for symbol in symbols:
        row = closes.get(symbol)
        close = f"{row['close']:.2f}" if row else "—"
        as_of = row["timestamp"][:10] if row else "—"
        rows.append(
            f'      <tr><td><a href="/chart/{symbol}">{symbol}</a></td>'
            f'<td class="num">{close}</td><td>{as_of}</td></tr>'
        )
    return HTMLResponse(
        _render_template("dashboard.html", {"__ROWS__": "\n".join(rows)})
    )
```

(Symbols come from `config/watchlist.yaml`, not user input, and were validated by the watchlist loader — but they still only contain `[A-Z0-9.^=-]`-safe characters, same class the chart route enforces.)

- [ ] **Step 6: Run suite + lint**

Run: `pytest tests/unit tests/api tests/integration && ruff check .`
Expected: unit/API PASS; integration PASS or SKIP

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Viz: add /dashboard watchlist page with latest closes and chart links"
```

---

### Task 9: QA sweep — cutover write path + chart pages, end to end

The per-task TDD above already covers the units; this task is the whole-system pass the QA ticket asks for: full suite, integration against real Postgres, parity, and eyes-on the pages.

**Files:**
- Possibly modify: any test gaps found; no planned source changes.

- [ ] **Step 1: Full offline suite + lint**

Run: `pytest && ruff check .`
Expected: all unit/API tests PASS; integration SKIP without Docker.

- [ ] **Step 2: Integration pass (only if Docker is available — otherwise state clearly in the final report that CI's Postgres service container is the integration gate and skip Steps 2–4)**

```bash
docker compose up -d
python scripts/backfill_postgres.py
pytest tests/integration/ -v
python scripts/check_parity.py
```

Expected: integration tests PASS (not skipped); parity exits 0.

- [ ] **Step 3: Live smoke of the cutover write path**

```bash
SCHEDULER_ENABLED=false uvicorn api.main:app --port 8000 &
sleep 3
curl -s localhost:8000/health | python3 -m json.tool          # postgres.connected: true
curl -s localhost:8000/ticker/AAPL/5d | python3 -m json.tool  # 200
curl -s localhost:8000/bars/AAPL | python3 -m json.tool       # bars present
curl -s localhost:8000/stored/news/AAPL | python3 -m json.tool
curl -s localhost:8000/metrics | python3 -m json.tool         # postgres_writes counters > 0
kill %1
```

- [ ] **Step 4: Eyes on the pages**

Open `http://localhost:8000/chart/AAPL` and `http://localhost:8000/dashboard` in a browser (use the claude-in-chrome tools if driving this autonomously): candlesticks render, dashboard rows link to charts, TradingView attribution visible.

- [ ] **Step 5: CI check after push**

After the sprint's commits are pushed, confirm the GitHub Actions run is green (integration job runs against the Postgres service container): `gh run watch` or `gh run list --limit 1`.

- [ ] **Step 6: Commit (only if gaps were fixed)**

```bash
git add -A
git commit -m "QA: close test gaps found in cutover + charts sweep"
```

---

### Task 10: Docs — README, architecture docs, CLAUDE.md

**Files:**
- Modify: `README.md` (endpoints table, storage flags, charts section)
- Modify: `docs/architecture-vision.md` (L2 status: done; L3: started)
- Modify: `CLAUDE.md` (**local only — gitignored, do not `git add`**)

- [ ] **Step 1: Update README.md**

Read the current README first and edit in its style. Content that must land:
- New endpoints: `GET /stored/events/{symbol}/{event_type}`, `GET /stored/news/{symbol}`, `GET /chart/{symbol}` (HTML), `GET /dashboard` (HTML).
- Flag semantics table:

```markdown
| Env var | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_WRITE_ENABLED` | on | Postgres is the source of truth; uncached fetches fail with 503 if the write fails. Set to `0` only for offline dev. |
| `CSV_WRITE_ENABLED` | on | Also snapshot fetches to `data/raw/*.csv`. Set to `0` to run Postgres-only. |
| `SCHEDULER_ENABLED` | off | Background watchlist ingestion. Skip-logic now reads the `ingestion_runs` table (the old `data/scheduler_state.json` is gone). |
```

- Parity check: `python scripts/check_parity.py` documented next to the backfill command.
- Charts: one short "First charts (L3)" section pointing at `/chart/{symbol}`, `/dashboard`, and `docs/charting-stack-decision.md`.

- [ ] **Step 2: Update `docs/architecture-vision.md`**

Mark the L2 milestone as completed (Postgres primary as of Sprint 7) and L3 as started (Lightweight Charts pages served from stored bars); keep edits surgical — status notes, not rewrites.

- [ ] **Step 3: Update `CLAUDE.md` (do not commit)**

Rewrite the stale bits: `storage/dual_write.py` → `storage/writes.py` (mandatory writes, `StorageWriteError`, flag default on), `CSV_WRITE_ENABLED`, scheduler state from `ingestion_runs` (`storage/state.py` deleted), new endpoints (`/stored/events`, `/stored/news`, `/chart`, `/dashboard`), `api/templates/`, `scripts/check_parity.py`, new test files.

- [ ] **Step 4: Commit (README + architecture doc only)**

```bash
git add README.md docs/architecture-vision.md
git commit -m "Docs: document Postgres-primary cutover, storage flags, and first chart pages"
```

- [ ] **Step 5: Close out the sprint in Notion**

Set the last tickets to `Done`, Sprint 7 page `Completion %` to 1, and — if all 10 are done — Status → `Completed` with a one-paragraph `Retrospective Notes` summary. Update the memory file `reference_notion.md` and `MEMORY.md` accordingly.

---

## Self-review notes

- Ticket coverage: all 10 Sprint 7 tickets map to Tasks 1–10 (table above).
- Ordering: parity script (T1) exists before defaults flip (T2/T3), matching the ticket's "run before flipping defaults". Chart pages (T7/T8) depend only on `/bars` (already live) and the spike (T6).
- Type consistency: `write_price_bars/write_events/write_news` (T2) are the names used by T3's guarded blocks; `_render_template`/`_validated_symbol` (T7) are reused in T8; `get_corporate_events/get_news_items` (T5) and `get_latest_closes` (T8) match their API call sites; `latest_success_times` (T4) is imported in `scheduler.service` and patched under that path in tests.
- Known risk: Docker may not be installed on this machine (true as of Sprint 5). Every Postgres-touching verification has an explicit no-Docker fallback (auto-skip + CI service container), and Task 9 requires reporting honestly which path ran.
