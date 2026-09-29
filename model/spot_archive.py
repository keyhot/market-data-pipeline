"""Binance SPOT kline archives from data.binance.vision, pure parsing and
coverage — the data prerequisite for the E16 trend and E15b vol-managed
experiments (not yet pre-registered; this module computes no return,
volatility or signal, only rows and coverage). The network fetch lives in
`scripts/fetch_spot_klines.py`; this module has no network code so it can be
imported and tested with tiny in-memory fixtures.

Spot kline CSVs (monthly or daily, one decoded from its zip) carry 12
columns: open_time, open, high, low, close, volume, close_time,
quote_volume, count, taker_buy_volume, taker_buy_quote_volume, ignore.
Older files (through ~2024) have no header row; files from Binance's 2025
re-export onward do. Open times are milliseconds in every file until
Binance's 2025 switch to microseconds — detected here per row by magnitude
(a real millisecond stamp never exceeds 10**14) rather than by a hardcoded
cutover date, since the exact boundary month is not worth pinning.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from pathlib import Path

import pandas as pd

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_US_THRESHOLD = 10**14  # no real millisecond open_time exceeds this
_FREQ = {"1d": "D", "1h": "h"}


def parse_klines(text: str) -> pd.DataFrame:
    """One decoded spot kline CSV -> a DataFrame indexed by UTC open time
    with float64 OHLCV columns, deduplicated (last wins) and sorted. Skips
    a leading header row (detected by content, not position) and blank
    lines."""
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if not rec or not rec[0].strip().isdigit():
            continue  # header row ("open_time,...") or a blank line
        open_time = int(rec[0])
        if open_time > _US_THRESHOLD:
            open_time //= 1000  # microseconds -> milliseconds
        stamp = pd.Timestamp(open_time, unit="ms", tz="UTC")
        rows.append((stamp, *(float(v) for v in rec[1:6])))
    frame = pd.DataFrame(rows, columns=["open_time", *OHLCV_COLUMNS])
    frame = frame.set_index("open_time")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.index.name = None
    return frame


def verify_checksum(content: bytes, checksum_text: str, filename: str) -> bool:
    """True iff `content`'s SHA-256 matches the `.CHECKSUM` file's hash AND
    the checksum file names this same `filename` (a `<hash>  <name>` line,
    matching `sha256sum`'s format) — checked, so a checksum for a different
    file can't silently pass."""
    parts = checksum_text.split()
    if len(parts) < 2 or parts[-1] != filename:
        return False
    return hashlib.sha256(content).hexdigest() == parts[0].lower()


def _read_zip(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(archive.namelist()[0]).decode()


def load_spot_klines(root: Path, symbol: str, interval: str) -> pd.DataFrame:
    """Every `{symbol}-{interval}-*.zip` under `root/symbol/interval/`
    (monthly and/or daily fallback zips, whichever were fetched), parsed,
    concatenated, deduplicated and sorted into one clean frame."""
    directory = Path(root) / symbol / interval
    zips = sorted(directory.glob(f"{symbol}-{interval}-*.zip"))
    if not zips:
        return pd.DataFrame(columns=list(OHLCV_COLUMNS))
    frame = pd.concat(parse_klines(_read_zip(p)) for p in zips)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def coverage(frame: pd.DataFrame, interval: str, start, end) -> pd.DataFrame:
    """Per calendar year over [`start`, `end`] inclusive, at `interval`'s
    grid: expected bars, present bars, and missing = expected - present.
    `start`/`end` are given explicitly by the caller (e.g. the symbol's
    first observed bar and the requested end-of-range) rather than inferred
    from `frame`, so a gap at either edge is reported instead of hidden by
    anchoring the grid on what is actually there."""
    freq = _FREQ[interval]
    start = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tz is None \
        else pd.Timestamp(start).tz_convert("UTC")
    end = pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tz is None \
        else pd.Timestamp(end).tz_convert("UTC")
    grid = pd.date_range(start, end, freq=freq)
    present = grid.isin(frame.index)
    years = grid.year
    expected = pd.Series(1, index=years).groupby(level=0).sum()
    have = pd.Series(present.astype(int), index=years).groupby(level=0).sum()
    out = pd.DataFrame({"expected": expected, "present": have})
    out["missing"] = out["expected"] - out["present"]
    out.index.name = "year"
    return out
