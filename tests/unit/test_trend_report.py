"""Tests for `scripts/trend_report.py` -- the E16 runner. DB-free: builds
tiny real zip fixtures on disk (the archive format the loaders actually
read) rather than mocking `model.spot_archive`, so a loader that keeps 2026
data is caught the same way it would be caught on the real archive."""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import trend_report as tr  # noqa: E402


def _write_spot_zip(directory: Path, symbol: str, interval: str, month: str, start_ms: int, n_days: int):
    directory.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    ms_per_day = 86_400_000
    for i in range(n_days):
        open_time = start_ms + i * ms_per_day
        close = 100.0 + i
        w.writerow([open_time, close, close, close, close, 1.0,
                    open_time + ms_per_day - 1, 1.0, 1, 0.5, 0.5, 0])
    path = directory / f"{symbol}-{interval}-{month}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{symbol}-{interval}-{month}.csv", buf.getvalue())
    return path


def _write_funding_zip(directory: Path, symbol: str, month: str, start_ms: int, n_events: int):
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["calc_time,funding_interval_hours,last_funding_rate"]
    step_ms = 8 * 3_600_000
    for i in range(n_events):
        lines.append(f"{start_ms + i * step_ms},8,0.0001")
    path = directory / f"{symbol}-fundingRate-{month}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{symbol}-fundingRate-{month}.csv", "\n".join(lines))
    return path


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    dec_2025_ms = int(pd.Timestamp("2025-12-01", tz="UTC").timestamp() * 1000)
    jan_2026_ms = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    _write_spot_zip(root / "binance_spot" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2025-12", dec_2025_ms, 31)
    _write_spot_zip(root / "binance_spot" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2026-01", jan_2026_ms, 10)
    _write_spot_zip(root / "binance_um" / "monthly" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2025-12", dec_2025_ms, 31)
    _write_spot_zip(root / "binance_um" / "monthly" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2026-01", jan_2026_ms, 10)
    _write_funding_zip(root / "binance_um" / "monthly" / "fundingRate" / "BTCUSDT", "BTCUSDT",
                       "2025-12", dec_2025_ms, 93)
    _write_funding_zip(root / "binance_um" / "monthly" / "fundingRate" / "BTCUSDT", "BTCUSDT",
                       "2026-01", jan_2026_ms, 30)
    return root


def test_load_spot_stage_a_excludes_2026(archive):
    bars = tr.load_spot(archive, "BTCUSDT", "A")
    # the row closing exactly 2026-01-01 (opened 2025-12-31) is legitimately
    # kept -- it is December's own last bar; only bars OPENING in 2026 (here,
    # everything from the 2026-01 fixture beyond the first row) are excluded.
    assert bars.index.max() == pd.Timestamp("2026-01-01", tz="UTC")
    assert (bars.index <= pd.Timestamp("2026-01-01", tz="UTC")).all()


def test_load_spot_stage_c_keeps_2026(archive):
    bars = tr.load_spot(archive, "BTCUSDT", "C")
    assert (bars.index >= pd.Timestamp("2026-01-01", tz="UTC")).any()


def test_load_spot_stage_b_excludes_2026(archive):
    bars = tr.load_spot(archive, "BTCUSDT", "B")
    assert (bars.index <= pd.Timestamp("2026-01-01", tz="UTC")).all()


def test_load_perp_stage_b_excludes_2026(archive):
    bars = tr.load_perp(archive, "BTCUSDT", "B")
    assert (bars.index <= pd.Timestamp("2026-01-01", tz="UTC")).all()


def test_load_funding_events_stage_a_excludes_2026(archive):
    events = tr.load_funding_events(archive, "BTCUSDT", "A")
    assert (events["time"] < pd.Timestamp("2026-01-01", tz="UTC")).all()
    assert len(events) == 93  # only the December file


def test_load_funding_events_stage_c_keeps_2026(archive):
    events = tr.load_funding_events(archive, "BTCUSDT", "C")
    assert len(events) == 93 + 30


def test_load_spot_close_time_shift(archive):
    """Close-time indexing: the row's index is one day AFTER its open."""
    bars = tr.load_spot(archive, "BTCUSDT", "A")
    dec_2025_open = pd.Timestamp("2025-12-01", tz="UTC")
    assert (dec_2025_open + pd.Timedelta(days=1)) in bars.index


def test_stage_b_refuses_without_valid_stage_a(tmp_path):
    (tmp_path / "e16_stage_a.json").write_text(json.dumps({"verdict": "VOID"}))
    with pytest.raises(RuntimeError, match="not VALID"):
        tr.run_stage_b(tmp_path, results_dir=tmp_path)


def test_stage_b_refuses_without_any_stage_a(tmp_path):
    with pytest.raises(RuntimeError, match="missing"):
        tr.run_stage_b(tmp_path, results_dir=tmp_path)


def test_stage_c_refuses_without_stage_b_pass(tmp_path):
    (tmp_path / "e16_stage_b.json").write_text(json.dumps({"verdict": "FAIL"}))
    with pytest.raises(RuntimeError, match="not PASS"):
        tr.run_stage_c(tmp_path, results_dir=tmp_path)


def test_stage_a_allowlist_constant_matches_module_keys():
    assert tr.STAGE_A_ALLOWLIST == {
        "a1_ci_lo_x", "a1_ci_lo_d",
        "a2_ci_hi_x", "a2_ci_hi_d",
        "a3_ok",
        "a4_mismatches",
        "a5_valid_months", "a5_missing_boundary_funding",
    }


def test_hold_index_is_64_months_sept2020_to_dec2025():
    assert len(tr.DECISION_TIMES) == 64
    assert len(tr.HOLD_INDEX) == 64
    assert tr.DECISION_TIMES[0] == pd.Timestamp("2020-09-01", tz="UTC")
    assert tr.DECISION_TIMES[-1] == pd.Timestamp("2025-12-01", tz="UTC")
    assert tr.HOLD_INDEX[0] == pd.Timestamp("2020-10-01", tz="UTC")
    assert tr.HOLD_INDEX[-1] == pd.Timestamp("2026-01-01", tz="UTC")
