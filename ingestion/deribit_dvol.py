"""Deribit DVOL (30-day implied volatility index) fetcher for E17.

Public, keyless endpoint `public/get_volatility_index_data`. Response shape
(pinned from a real call on 2026-10-04): `result.data` rows are
`[ts_ms, open, high, low, close]`, NEWEST FIRST, at most 1,000 per call;
`result.continuation` is the next `end_timestamp` to request (the oldest
row's ts - 1h), or null when the range is exhausted. Timestamps are candle
START times (each candle's open equals the previous close).

Pure apart from the injected `get_json(url, params) -> dict`, so the
pagination is tested with no network. The cache is a CSV under `data/`
(never committed); `merge_dvol` makes reruns idempotent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd

DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
DVOL_START_MS = 1_616_544_000_000  # 2021-03-24 00:00 UTC, first hourly point
RESOLUTION_S = 3600
COLUMNS = ["ts", "open", "high", "low", "close"]
MAX_PAGES = 1000  # ~50 are needed for 2021-03 -> 2026-09; this is a runaway guard

GetJson = Callable[[str, dict], dict]


def http_get_json(url: str, params: dict) -> dict:
    import requests

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _empty() -> pd.DataFrame:
    return pd.DataFrame({
        "ts": pd.Series([], dtype="datetime64[ns, UTC]"),
        "open": pd.Series([], dtype=float), "high": pd.Series([], dtype=float),
        "low": pd.Series([], dtype=float), "close": pd.Series([], dtype=float),
    })


def fetch_dvol(currency: str, start_ms: int, end_ms: int,
               get_json: GetJson = http_get_json) -> pd.DataFrame:
    """All hourly DVOL candles for `currency` with start time in
    [start_ms, end_ms], oldest first, following `continuation` backward."""
    rows: list[list] = []
    end = int(end_ms)
    for _ in range(MAX_PAGES):
        params = {"currency": currency, "start_timestamp": int(start_ms),
                  "end_timestamp": end, "resolution": RESOLUTION_S}
        payload = get_json(DVOL_URL, params)
        if "error" in payload or "result" not in payload:
            raise RuntimeError(
                f"Deribit DVOL error for {currency}: {payload.get('error')}"
            )
        result = payload["result"]
        rows.extend(result.get("data") or [])
        cont = result.get("continuation")
        if cont is None or not result.get("data"):
            break
        end = int(cont)
    else:
        raise RuntimeError("DVOL pagination did not terminate")
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts_ms", "open", "high", "low", "close"])
    df["ts"] = pd.to_datetime(df["ts_ms"].astype("int64"), unit="ms", utc=True)
    df = df[COLUMNS].astype({c: float for c in COLUMNS[1:]})
    return _tidy(df)


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    df = df.drop_duplicates("ts", keep="last").sort_values("ts")
    return df.reset_index(drop=True)


def merge_dvol(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Union on ts; on overlap the NEW row wins. Idempotent."""
    if existing is None or existing.empty:
        return _tidy(new[COLUMNS].copy())
    return _tidy(pd.concat([existing[COLUMNS], new[COLUMNS]], ignore_index=True))


def save_dvol_csv(df: pd.DataFrame, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[COLUMNS].copy()
    out["ts"] = out["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.to_csv(path, index=False)


def load_dvol_csv(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return _empty()
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return _tidy(df[COLUMNS].astype({c: float for c in COLUMNS[1:]}))
