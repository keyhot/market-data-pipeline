import hashlib
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from model.spot_archive import (
    coverage,
    load_spot_klines,
    parse_klines,
    verify_checksum,
)

# Verbatim rows pulled from real archives (2026-09-29):
#   BTCUSDT-1d-2019-01.zip (no header, milliseconds, 13 digits)
_MS_ROW = ("1546300800000,3701.23000000,3810.16000000,3642.00000000,"
          "3797.14000000,23741.68703300,1546387199999,88149249.09230461,"
          "154227,12919.15589900,47973435.86685800,0")
#   BTCUSDT-1d-2025-01.zip (no header, microseconds, 16 digits)
_US_ROW = ("1735689600000000,93576.00000000,95151.15000000,92888.00000000,"
          "94591.79000000,10373.32613000,1735775999999999,975444194.13799830,"
          "1516556,5347.73648000,502914035.64059070,0")
_HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,"
          "count,taker_buy_volume,taker_buy_quote_volume,ignore")


def test_parses_a_millisecond_row_pre_2025():
    frame = parse_klines(_MS_ROW)
    assert len(frame) == 1
    assert frame.index[0] == pd.Timestamp("2019-01-01", tz="UTC")
    assert frame.index[0].year == 2019
    assert frame["close"].iloc[0] == pytest.approx(3797.14)


def test_parses_a_microsecond_row_2025_onward():
    """The 2025 switch from ms to µs is detected by magnitude (any stamp
    > 10**14), not a hardcoded date — this row alone would misparse to 1970
    under unit='ms' with no //1000, or misparse to a bogus future year under
    unit='us' with no downscale to ms."""
    frame = parse_klines(_US_ROW)
    assert len(frame) == 1
    assert frame.index[0] == pd.Timestamp("2025-01-01", tz="UTC")
    assert frame.index[0].year == 2025
    assert frame["open"].iloc[0] == pytest.approx(93576.0)


def test_skips_header_row_when_present():
    text = "\n".join([_HEADER, _MS_ROW])
    frame = parse_klines(text)
    assert len(frame) == 1


def test_parses_without_header_too():
    frame = parse_klines(_MS_ROW)
    assert len(frame) == 1


def test_dedupes_keeping_last_and_sorts():
    row_a = _MS_ROW.replace("3797.14000000", "1.00000000", 1)
    text = "\n".join([_MS_ROW, row_a])  # same open_time, second close wins
    frame = parse_klines(text)
    assert len(frame) == 1
    assert frame["close"].iloc[0] == pytest.approx(1.0)


def test_output_columns_are_ohlcv_only():
    frame = parse_klines(_MS_ROW)
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


# --- checksum ----------------------------------------------------------------

def test_verify_checksum_accepts_a_matching_hash():
    content = b"hello world"
    digest = hashlib.sha256(content).hexdigest()
    checksum_text = f"{digest}  BTCUSDT-1d-2017-08.zip\n"
    assert verify_checksum(content, checksum_text, "BTCUSDT-1d-2017-08.zip")


def test_verify_checksum_line_format_matches_the_real_archive():
    """Pinned against the live archive (2026-09-29,
    BTCUSDT-1d-2017-08.zip.CHECKSUM): the real line is `<64-hex>  <name>`,
    two spaces, no `*` prefix — the exact format `verify_checksum` parses."""
    real_line = ("c92970f74893163b848f117a37fa5e7110ee1d800c0d9ada949940f"
                "ccccfdd2f  BTCUSDT-1d-2017-08.zip")
    digest, name = real_line.split("  ")
    assert len(digest) == 64
    assert name == "BTCUSDT-1d-2017-08.zip"
    content = bytes.fromhex("00")  # arbitrary bytes, not the real archive
    checksum_text = f"{hashlib.sha256(content).hexdigest()}  {name}"
    assert verify_checksum(content, checksum_text, name)
    assert not verify_checksum(content, real_line, name)  # wrong body, real hash


def test_verify_checksum_rejects_a_bad_hash():
    content = b"hello world"
    checksum_text = "0" * 64 + "  BTCUSDT-1d-2017-08.zip"
    assert not verify_checksum(content, checksum_text, "BTCUSDT-1d-2017-08.zip")


def test_verify_checksum_rejects_a_filename_mismatch():
    content = b"hello world"
    digest = hashlib.sha256(content).hexdigest()
    checksum_text = f"{digest}  ETHUSDT-1d-2017-08.zip"
    assert not verify_checksum(content, checksum_text, "BTCUSDT-1d-2017-08.zip")


def test_verify_checksum_rejects_malformed_checksum_text():
    assert not verify_checksum(b"x", "not-a-valid-line", "BTCUSDT-1d-2017-08.zip")


# --- load_spot_klines ----------------------------------------------------------

def _write_zip(directory: Path, name: str, csv_text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(directory / name, "w") as archive:
        archive.writestr(name.replace(".zip", ".csv"), csv_text)


def test_load_spot_klines_concatenates_and_dedupes(tmp_path):
    directory = tmp_path / "BTCUSDT" / "1d"
    _write_zip(directory, "BTCUSDT-1d-2019-01.zip", _MS_ROW)
    _write_zip(directory, "BTCUSDT-1d-2025-01.zip", _US_ROW)
    frame = load_spot_klines(tmp_path, "BTCUSDT", "1d")
    assert len(frame) == 2
    assert list(frame.index.year) == [2019, 2025]


def test_load_spot_klines_empty_when_no_zips(tmp_path):
    frame = load_spot_klines(tmp_path, "BTCUSDT", "1h")
    assert frame.empty
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


# --- coverage ------------------------------------------------------------------

def _daily_frame(dates: list[str]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in dates])
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                         "volume": 1.0}, index=index)


def test_coverage_reports_no_missing_days_when_complete():
    dates = pd.date_range("2024-01-01", "2024-01-10", freq="D", tz="UTC")
    frame = _daily_frame([d.isoformat() for d in dates])
    cov = coverage(frame, "1d", "2024-01-01", "2024-01-10")
    assert cov.loc[2024, "expected"] == 10
    assert cov.loc[2024, "missing"] == 0


def test_coverage_counts_a_gap_in_the_middle():
    dates = pd.date_range("2024-01-01", "2024-01-10", freq="D", tz="UTC")
    dates = dates.delete([4, 5])  # drop two days
    frame = _daily_frame([d.isoformat() for d in dates])
    cov = coverage(frame, "1d", "2024-01-01", "2024-01-10")
    assert cov.loc[2024, "missing"] == 2


def test_coverage_reports_zero_missing_before_the_first_observed_bar():
    """A grid anchored at the symbol's true first bar (e.g. a mid-month
    listing) must not report the pre-listing days as missing — the caller is
    expected to pass that first bar as `start`, not a fixed calendar year
    start."""
    dates = pd.date_range("2017-08-17", "2017-08-31", freq="D", tz="UTC")
    frame = _daily_frame([d.isoformat() for d in dates])
    cov = coverage(frame, "1d", "2017-08-17", "2017-08-31")
    assert cov.loc[2017, "expected"] == 15
    assert cov.loc[2017, "missing"] == 0


def test_coverage_reports_a_trailing_gap_at_the_requested_end():
    """The grid must extend to the requested `end`, not stop at the last bar
    actually present — otherwise a truncated final month reports as
    complete."""
    dates = pd.date_range("2024-01-01", "2024-01-05", freq="D", tz="UTC")
    frame = _daily_frame([d.isoformat() for d in dates])
    cov = coverage(frame, "1d", "2024-01-01", "2024-01-10")
    assert cov.loc[2024, "expected"] == 10
    assert cov.loc[2024, "present"] == 5
    assert cov.loc[2024, "missing"] == 5


def test_coverage_splits_a_gap_across_a_year_boundary():
    dates = list(pd.date_range("2023-12-28", "2023-12-30", freq="D", tz="UTC"))
    dates += list(pd.date_range("2024-01-02", "2024-01-03", freq="D", tz="UTC"))
    frame = _daily_frame([d.isoformat() for d in dates])
    cov = coverage(frame, "1d", "2023-12-28", "2024-01-03")
    # missing: 2023-12-31, 2024-01-01
    assert cov.loc[2023, "expected"] == 4
    assert cov.loc[2023, "missing"] == 1
    assert cov.loc[2024, "expected"] == 3
    assert cov.loc[2024, "missing"] == 1


def test_coverage_hourly_grid():
    dates = pd.date_range("2024-01-01 00:00", "2024-01-01 05:00", freq="h", tz="UTC")
    dates = dates.delete([2])
    frame = _daily_frame([d.isoformat() for d in dates])
    cov = coverage(frame, "1h", "2024-01-01 00:00", "2024-01-01 05:00")
    assert cov.loc[2024, "expected"] == 6
    assert cov.loc[2024, "missing"] == 1
