"""Archive-information features (experiment E12, vault
`Docs/archive-information-experiment.md`): open interest, long/short ratios,
taker ratio and coarse book depth from Binance USDⓈ-M daily archives on
data.binance.vision. Every feature at bar t reads only rows AVAILABLE by the
time bar t CLOSED.

Sources (verified 2026-09-28 by opening sample files):
`metrics` CSV: create_time, symbol, sum_open_interest,
sum_open_interest_value, count_toptrader_long_short_ratio,
sum_toptrader_long_short_ratio, count_long_short_ratio,
sum_taker_long_short_vol_ratio — 5-min rows, a header, and on some days
duplicated rows. `bookDepth` CSV: timestamp, percentage (−5..5, incl. ±0.2),
depth, notional — cumulative depth per band, one snapshot every ~30 s.
The archive carries ±1..±5% only: the ±0.2% band the spike note listed does
not exist in any 2023-2025 file (checked 2026-09-28), so ±1% is the finest.
"""

import io

import numpy as np
import pandas as pd

METRICS_FEATURES = (
    "oi_change_1",
    "oi_change_4",
    "oi_change_16",
    "oi_z_96",
    "top_ls",
    "top_ls_change_4",
    "global_ls",
    "global_ls_change_4",
    "taker_ls",
)
DEPTH_FEATURES = ("depth_imb_1", "depth_imb_2", "depth_change_4")

BAR = pd.Timedelta(minutes=15)
METRICS_STEP = pd.Timedelta(minutes=5)
METRICS_MAX_AGE = pd.Timedelta(minutes=15)
DEPTH_MAX_AGE = pd.Timedelta(minutes=10)
_OI_Z_WINDOW = 96  # one day of 15m bars
_OI_Z_MIN = 48  # so one missing row blacks out 48 bars, not 96 (A2)
_CHANGE_BARS = 4
_METRICS_COLUMNS = {
    "sum_open_interest": "oi",
    "sum_toptrader_long_short_ratio": "top_ratio",
    "count_long_short_ratio": "global_ratio",
    "sum_taker_long_short_vol_ratio": "taker_ratio",
}
_DEPTH_BANDS = {-1.0: "bid1", 1.0: "ask1", -2.0: "bid2", 2.0: "ask2"}


def parse_metrics(text: str) -> pd.DataFrame:
    """One daily `metrics` CSV → the four used columns, indexed by
    create_time (UTC), deduplicated and sorted. A column a particular 5-min
    row failed to fill stays NaN rather than dropping the WHOLE row (KI-093):
    most of 2022 is missing `sum_toptrader_long_short_ratio` and
    `sum_taker_long_short_vol_ratio` on most rows, and a caller reading only
    `oi` (`scripts/cross_section_report.py`'s `_fetch_oi`) must not lose it
    because an unrelated column was empty that stamp. Only a row with EVERY
    used column empty is dropped, since it carries nothing at all."""
    raw = pd.read_csv(io.StringIO(text))
    frame = raw[list(_METRICS_COLUMNS)].rename(columns=_METRICS_COLUMNS)
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame.index = pd.to_datetime(raw["create_time"], utc=True)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.dropna(how="all")


def metrics_coverage(frame: pd.DataFrame) -> pd.DataFrame:
    """Per calendar year: parsed rows, rows with every one of the four
    metrics columns present, and how many rows are missing each column —
    so a coverage loss (KI-093) is a number to check, not a silent drop.
    `frame` is `parse_metrics` output, or a concatenation of many days'."""
    columns = ["rows", "complete", *(f"missing_{c}" for c in frame.columns)]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    years = frame.index.year
    out = pd.DataFrame({"rows": frame.groupby(years).size(),
                        "complete": frame.notna().all(axis=1).groupby(years).sum()})
    for col in frame.columns:
        out[f"missing_{col}"] = frame[col].isna().groupby(years).sum()
    return out[columns]


def parse_depth(text: str) -> pd.DataFrame:
    """One daily `bookDepth` CSV → one row per snapshot with the ±1% and
    ±2% cumulative notionals. Snapshots missing a band are dropped."""
    raw = pd.read_csv(io.StringIO(text))
    raw = raw[raw["percentage"].astype(float).isin(list(_DEPTH_BANDS))]
    raw = raw.assign(band=raw["percentage"].astype(float).map(_DEPTH_BANDS))
    wide = raw.pivot_table(index="timestamp", columns="band", values="notional",
                           aggfunc="last")
    wide.index = pd.to_datetime(wide.index, utc=True)
    wide.columns.name = None
    return wide.reindex(columns=list(_DEPTH_BANDS.values())).dropna().sort_index()


def asof(frame: pd.DataFrame, available_at: pd.DatetimeIndex,
         bars: pd.DatetimeIndex, max_age: pd.Timedelta) -> pd.DataFrame:
    """For each bar (indexed by OPEN time T), the latest row whose
    availability time is <= the bar's close T + 15m — or NaN when there is
    none, or it is older than `max_age` at the close."""
    close = bars + BAR
    pos = available_at.searchsorted(close, side="right") - 1
    seen = pos >= 0
    picked = frame.iloc[np.maximum(pos, 0)].to_numpy(dtype=float)
    age = close - available_at[np.maximum(pos, 0)]
    ok = seen & (age <= max_age)
    picked[~ok] = np.nan
    return pd.DataFrame(picked, index=bars, columns=frame.columns)


def metrics_features(metrics: pd.DataFrame, bars: pd.DatetimeIndex,
                     delay: pd.Timedelta) -> pd.DataFrame:
    """The nine pre-registered metrics features on the bar grid. A row
    stamped s is available at s + `delay` (0 when stamps end their period,
    5 min when they start it — see `check_stamp_convention`)."""
    cols = ["oi", "top_ratio", "global_ratio", "taker_ratio"]
    valid = metrics[cols][(metrics[cols] > 0).all(axis=1)]  # 0 OI rows exist
    logs = np.log(valid)
    logs["taker_15"] = logs["taker_ratio"].rolling("15min").mean()
    joined = asof(logs, logs.index + delay, bars, METRICS_MAX_AGE)
    oi = joined["oi"]
    feats = pd.DataFrame(index=bars)
    for k in (1, 4, 16):
        feats[f"oi_change_{k}"] = oi - oi.shift(k)
    roll = oi.rolling(_OI_Z_WINDOW, min_periods=_OI_Z_MIN)
    feats["oi_z_96"] = (oi - roll.mean()) / roll.std()
    feats["top_ls"] = joined["top_ratio"]
    feats["top_ls_change_4"] = feats["top_ls"] - feats["top_ls"].shift(_CHANGE_BARS)
    feats["global_ls"] = joined["global_ratio"]
    feats["global_ls_change_4"] = (feats["global_ls"]
                                   - feats["global_ls"].shift(_CHANGE_BARS))
    feats["taker_ls"] = joined["taker_15"]
    return feats[list(METRICS_FEATURES)]


def depth_features(depth: pd.DataFrame, bars: pd.DatetimeIndex) -> pd.DataFrame:
    """The three pre-registered depth features from the latest snapshot at or
    before each bar's close."""
    snap = asof(depth, depth.index, bars, DEPTH_MAX_AGE)
    feats = pd.DataFrame(index=bars)
    feats["depth_imb_1"] = (snap.bid1 - snap.ask1) / (snap.bid1 + snap.ask1)
    feats["depth_imb_2"] = (snap.bid2 - snap.ask2) / (snap.bid2 + snap.ask2)
    total = np.log(snap.bid1 + snap.ask1)
    feats["depth_change_4"] = total - total.shift(_CHANGE_BARS)
    return feats


def check_stamp_convention(metrics: pd.DataFrame,
                           close: pd.Series) -> dict:
    """Does a metrics row's create_time END its 5-min period or START it?
    The taker ratio is a period aggregate, so it co-moves with the return of
    the period it covers. For each bar (open T, close T + 15m), correlate log
    taker ratio of the row stamped at the bar's CLOSE and at its OPEN with the
    bar's log return. Stamps end their period only if the close row wins;
    otherwise the conservative 5-min delay applies."""
    ret = np.log(close / close.shift(1))  # bar T's own return (close at T+15m)
    taker = np.log(metrics["taker_ratio"])
    frame = pd.DataFrame({"close_row": taker.reindex(close.index + BAR).to_numpy(),
                          "open_row": taker.reindex(close.index).to_numpy(),
                          "ret": ret.to_numpy()}, index=close.index)
    by_year = {int(y): (g.close_row.corr(g.ret), g.open_row.corr(g.ret))
               for y, g in frame.groupby(frame.index.year)
               if g.close_row.notna().sum() > 100}
    # The convention is a property of the whole sample only if EVERY year
    # agrees (Binance flipped it at 2024-01-01); any disagreement is
    # ambiguous, and ambiguous gets the conservative delay.
    ends = all(c > o for c, o in by_year.values())
    return {"corr_close_row": float(frame.close_row.corr(frame.ret)),
            "corr_open_row": float(frame.open_row.corr(frame.ret)),
            "by_year": by_year,
            "delay": pd.Timedelta(0) if by_year and ends else METRICS_STEP}


def with_archive_features(X: pd.DataFrame, y: pd.Series,
                          feats: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """X plus archive features. Rows without a complete feature row are
    dropped, never filled."""
    joined = X.join(feats, how="inner").dropna()
    return joined, y.loc[joined.index]


def leaked_control(feats: pd.DataFrame, close: pd.Series,
                   column: str) -> pd.DataFrame:
    """The positive control: `column` replaced by the NEXT bar's log return,
    carried through the same archive join. Never an arm."""
    leaked = feats.copy()
    next_return = np.log(close.shift(-1) / close)
    leaked[column] = next_return.reindex(feats.index)
    return leaked
