"""E16 trend report runner. Pre-registered in the vault,
`Docs/trend-experiment.md` -- every window, threshold and formula is fixed
there; this script applies them, never chooses them.

Stage A is plumbing only: it prints and persists NO candidate/control/null
statistic -- only the allowlisted A1-A5 checks. Stage B refuses to run
without a VALID stage-A result; stage C refuses to run without a stage-B
PASS.

    poetry run python scripts/trend_report.py --stage A [--archive-root PATH]
    poetry run python scripts/trend_report.py --stage B [--archive-root PATH]
    poetry run python scripts/trend_report.py --stage C [--archive-root PATH]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from model.cross_section import funding_by_day  # noqa: E402
from model.spot_archive import load_spot_klines  # noqa: E402
from model.trend import (  # noqa: E402
    COST_SIDE_PERP,
    COV_MONTHS,
    EWMA_COM,
    FIRST_HOLD,
    GROSS_CAP,
    LAST_HOLD_B,
    LOOKBACKS,
    TARGET_VOL,
    block_bootstrap_ci,
    book_returns,
    conditions,
    excess_over_null,
    hop_exposures,
    month_end_closes,
    monthly_returns,
    rebalance_exposures,
    signed_null,
    stage_a_valid,
)
import report_results  # noqa: E402
from report_results import write_result  # noqa: E402

SYMBOLS = ("BTCUSDT", "ETHUSDT")
CUTOFF_2026 = pd.Timestamp("2026-01-01", tz="UTC")

# 64 holding months, Sept 2020 .. Dec 2025 -> decision times (T_{h-1}) and
# the holding-month index (T_h) they produce, per the frozen note's T_m
# convention (see model/trend.py's module docstring).
DECISION_TIMES = pd.date_range("2020-09-01", "2025-12-01", freq="MS", tz="UTC")
HOLD_INDEX = DECISION_TIMES + pd.DateOffset(months=1)  # 2020-10-01 .. 2026-01-01

A4_MONTHS = ("2020-08", "2020-12", "2021-06", "2022-03", "2022-11",
             "2023-05", "2024-01", "2024-08", "2025-02", "2025-11")

STAGE_A_ALLOWLIST = {
    "a1_ci_lo_x", "a1_ci_lo_d",
    "a2_ci_hi_x", "a2_ci_hi_d",
    "a3_ok",
    "a4_mismatches",
    "a5_valid_months", "a5_missing_boundary_funding",
}


# --------------------------------------------------------------------------
# Loaders (DB-free; the runner's job per the module spec)
# --------------------------------------------------------------------------

def load_spot(root: Path, symbol: str, stage: str) -> pd.Series:
    """Daily spot close, shifted to CLOSE-time indexing (open + 1 day).
    Stage A/B never see a bar OPENING at or after 2026-01-01 (frozen note,
    Stages and outputs)."""
    raw = load_spot_klines(Path(root) / "binance_spot" / "klines", symbol, "1d")
    if stage in ("A", "B"):
        raw = raw[raw.index < CUTOFF_2026]
    close = raw["close"].astype(float).copy()
    close.index = close.index + pd.Timedelta(days=1)
    return close


def load_perp(root: Path, symbol: str, stage: str) -> pd.Series:
    """Daily UM perp close, close-time indexed, same truncation rule."""
    raw = load_spot_klines(Path(root) / "binance_um" / "monthly" / "klines", symbol, "1d")
    if stage in ("A", "B"):
        raw = raw[raw.index < CUTOFF_2026]
    close = raw["close"].astype(float).copy()
    close.index = close.index + pd.Timedelta(days=1)
    return close


def load_funding_events(root: Path, symbol: str, stage: str) -> pd.DataFrame:
    """Raw funding events (symbol, time, rate) from the local monthly
    fundingRate zips -- content-truncated the same way as klines, so a
    2026 file (if one is ever fetched) is excluded here regardless of what
    stage B's own month window later does."""
    directory = Path(root) / "binance_um" / "monthly" / "fundingRate" / symbol
    zips = sorted(directory.glob(f"{symbol}-fundingRate-*.zip"))
    frames = []
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            name = zf.namelist()[0]
            text = zf.read(name).decode()
        df = pd.read_csv(io.StringIO(text))
        frames.append(pd.DataFrame({
            "symbol": symbol,
            "time": pd.to_datetime(df["calc_time"], unit="ms", utc=True),
            "rate": df["last_funding_rate"].astype(float),
        }))
    events = (pd.concat(frames, ignore_index=True) if frames
              else pd.DataFrame(columns=["symbol", "time", "rate"]))
    if stage in ("A", "B"):
        events = events[events["time"] < CUTOFF_2026]
    return events.reset_index(drop=True)


# --------------------------------------------------------------------------
# Book construction (shared by A/B; stage A never persists its numbers)
# --------------------------------------------------------------------------

def _oracle_sign(row: pd.Series) -> pd.Series:
    return pd.Series(np.where(row.to_numpy() >= 0, 1.0, -1.0), index=row.index)


def _run_book(spot_daily, r_tilde, price_ret, cost_side, signal="trend", override_fn=None):
    """Run every decision in DECISION_TIMES, indexing the resulting
    exposure at the HOLDING month's end (decision T + 1 month) -- the
    convention `book_returns`/`r_tilde`/`price_ret` all share. Returns
    (e, book_returns_frame)."""
    symbols = list(spot_daily.columns)
    rows = {}
    for T in DECISION_TIMES:
        h = T + pd.DateOffset(months=1)
        override_S = override_fn(r_tilde.loc[h]) if override_fn is not None else None
        e, _capped = rebalance_exposures(spot_daily, T, signal=signal, override_S=override_S)
        rows[h] = e
    e_df = pd.DataFrame(rows).T[symbols].loc[HOLD_INDEX]
    br = book_returns(e_df, r_tilde, price_ret, cost_side)
    return e_df, br


def _load_all(root: Path, stage: str):
    spot_daily = pd.DataFrame({s: load_spot(root, s, stage) for s in SYMBOLS})[list(SYMBOLS)]
    perp_daily = pd.DataFrame({s: load_perp(root, s, stage) for s in SYMBOLS})[list(SYMBOLS)]
    events_by_symbol = {s: load_funding_events(root, s, stage) for s in SYMBOLS}
    events_all = pd.concat(events_by_symbol.values(), ignore_index=True)
    days = pd.date_range(perp_daily.index.min().normalize(), perp_daily.index.max().normalize(),
                         freq="D", tz="UTC")
    funding_daily = funding_by_day(events_all, days)[list(SYMBOLS)]

    perp_month_closes = pd.DataFrame(
        {s: month_end_closes(perp_daily[s]) for s in SYMBOLS}
    )[list(SYMBOLS)]
    price_ret = monthly_returns(perp_month_closes)
    from model.trend import holding_excess_returns
    r_tilde = holding_excess_returns(perp_daily, funding_daily)
    return spot_daily, perp_daily, perp_month_closes, events_by_symbol, funding_daily, price_ret, r_tilde


# --------------------------------------------------------------------------
# Stage A checks
# --------------------------------------------------------------------------

def _a1_a2(spot_daily, r_tilde, price_ret):
    e_plus, br_plus = _run_book(spot_daily, r_tilde, price_ret, COST_SIDE_PERP, signal="plus_one")

    e_o, br_o = _run_book(spot_daily, r_tilde, price_ret, COST_SIDE_PERP,
                          signal="trend", override_fn=_oracle_sign)
    n_o, _ = signed_null(e_o, r_tilde, br_o["kappa"])
    x_o, d_o = conditions(br_o["R"], n_o, br_plus["R"])
    _, a1_lo_x, _ = block_bootstrap_ci(x_o.to_numpy())
    _, a1_lo_d, _ = block_bootstrap_ci(d_o.to_numpy())

    e_ao, br_ao = _run_book(spot_daily, r_tilde, price_ret, COST_SIDE_PERP,
                            signal="trend", override_fn=lambda row: -_oracle_sign(row))
    n_ao, _ = signed_null(e_ao, r_tilde, br_ao["kappa"])
    x_ao, d_ao = conditions(br_ao["R"], n_ao, br_plus["R"])
    _, _, a2_hi_x = block_bootstrap_ci(x_ao.to_numpy())
    _, _, a2_hi_d = block_bootstrap_ci(d_ao.to_numpy())

    return a1_lo_x, a1_lo_d, a2_hi_x, a2_hi_d


def _a3_identities(spot_daily, r_tilde):
    symbols = list(spot_daily.columns)
    ok = True
    sample_T = DECISION_TIMES[::10]  # every 10th decision, cheap but real
    for T in sample_T:
        ones = pd.Series(1.0, index=symbols)
        e_override, c_override = rebalance_exposures(spot_daily, T, signal="trend", override_S=ones)
        e_plus, c_plus = rebalance_exposures(spot_daily, T, signal="plus_one")
        if not np.array_equal(e_override.to_numpy(), e_plus.to_numpy()) or c_override != c_plus:
            ok = False

    const_e = pd.DataFrame({s: 0.3 for s in symbols}, index=HOLD_INDEX)
    x_const = excess_over_null(const_e, r_tilde)
    if float(np.max(np.abs(x_const.to_numpy()))) > 1e-15:
        ok = False

    T0 = DECISION_TIMES[len(DECISION_TIMES) // 2]
    e_365, c_365 = rebalance_exposures(spot_daily, T0, signal="trend")
    from model.trend import mop_vol, signals as _signals, month_end_closes as _mec
    month_closes = pd.DataFrame({s: _mec(spot_daily[s]) for s in symbols})[symbols]
    sig = _signals(month_closes, T0)
    S = sig.mean(axis=1).reindex(symbols)
    sigma = pd.Series({s: mop_vol(spot_daily[s], T0) for s in symbols}).reindex(symbols)
    R = monthly_returns(month_closes)
    window = R.loc[(R.index >= T0 - pd.DateOffset(months=COV_MONTHS - 1)) & (R.index <= T0), symbols].dropna()
    cov = np.cov(window.to_numpy(), rowvar=False, ddof=1)
    e_scaled, _ = hop_exposures(S, sigma / 0.4, cov, TARGET_VOL, GROSS_CAP)
    if not np.allclose(e_365.to_numpy(), e_scaled.to_numpy(), rtol=1e-12):
        ok = False

    return ok


def _a4_truncation(spot_daily):
    """A4_MONTHS are CALENDAR-month labels m (matching the note's table);
    the T_m timestamp to test is the 1st of the NEXT month (the frozen
    "close of the bar opening on the last day of month m" convention), so
    e.g. label "2020-08" tests decision timestamp 2020-09-01 -- exactly
    DECISION_TIMES[0], the very first decision the run makes."""
    mismatches = 0
    for m in A4_MONTHS:
        T = pd.Timestamp(f"{m}-01", tz="UTC") + pd.DateOffset(months=1)
        full_e, full_c = rebalance_exposures(spot_daily, T, signal="trend")
        truncated = spot_daily.loc[spot_daily.index <= T]
        trunc_e, trunc_c = rebalance_exposures(truncated, T, signal="trend")
        if not np.array_equal(full_e.to_numpy(), trunc_e.to_numpy()) or full_c != trunc_c:
            mismatches += 1
    return mismatches


def _a5_validity(perp_month_closes, events_by_symbol):
    symbols = list(perp_month_closes.columns)
    valid_months = 0
    for T in DECISION_TIMES:
        h = T + pd.DateOffset(months=1)
        if all(pd.notna(perp_month_closes[s].loc[T]) and pd.notna(perp_month_closes[s].loc[h])
               for s in symbols):
            valid_months += 1

    first_day = pd.Timestamp(f"{FIRST_HOLD}-01", tz="UTC")
    last_day = pd.Timestamp(f"{LAST_HOLD_B}-01", tz="UTC") + pd.offsets.MonthEnd(0)
    days = pd.date_range(first_day, last_day, freq="D", tz="UTC")
    missing_boundary = {}
    for s in symbols:
        ev = events_by_symbol[s]
        holding_day = (ev["time"] - pd.Timedelta(minutes=1)).dt.floor("D")
        present = set(holding_day)
        missing_boundary[s] = int(sum(1 for d in days if d not in present))
    return valid_months, missing_boundary


def run_stage_a(archive_root: Path, results_dir=None):
    (spot_daily, perp_daily, perp_month_closes, events_by_symbol,
     funding_daily, price_ret, r_tilde) = _load_all(archive_root, "A")

    statistic = {}
    checks = {}
    inputs = {
        "symbols": list(SYMBOLS),
        "spot_first_bar": {s: str(spot_daily[s].dropna().index.min()) for s in SYMBOLS},
        "spot_last_bar": {s: str(spot_daily[s].dropna().index.max()) for s in SYMBOLS},
        "perp_first_bar": {s: str(perp_daily[s].dropna().index.min()) for s in SYMBOLS},
        "perp_last_bar": {s: str(perp_daily[s].dropna().index.max()) for s in SYMBOLS},
        "scored_holding_months": len(HOLD_INDEX),
    }

    a1_lo_x, a1_lo_d, a2_hi_x, a2_hi_d = _a1_a2(spot_daily, r_tilde, price_ret)
    statistic["a1_ci_lo_x"] = a1_lo_x
    statistic["a1_ci_lo_d"] = a1_lo_d
    checks["a1_x"] = bool(a1_lo_x > 0)
    checks["a1_d"] = bool(a1_lo_d > 0)

    statistic["a2_ci_hi_x"] = a2_hi_x
    statistic["a2_ci_hi_d"] = a2_hi_d
    checks["a2_x"] = bool(a2_hi_x < 0)
    checks["a2_d"] = bool(a2_hi_d < 0)

    a3_ok = _a3_identities(spot_daily, r_tilde)
    statistic["a3_ok"] = a3_ok
    checks["a3"] = a3_ok

    a4_mismatches = _a4_truncation(spot_daily)
    statistic["a4_mismatches"] = a4_mismatches
    checks["a4"] = a4_mismatches == 0

    a5_valid_months, a5_missing_boundary = _a5_validity(perp_month_closes, events_by_symbol)
    statistic["a5_valid_months"] = a5_valid_months
    statistic["a5_missing_boundary_funding"] = a5_missing_boundary
    checks["a5_months"] = a5_valid_months == len(HOLD_INDEX)
    checks["a5_boundary"] = all(v <= 1 for v in a5_missing_boundary.values())

    assert set(statistic.keys()) <= STAGE_A_ALLOWLIST, \
        f"stage A statistic keys outside the allowlist: {set(statistic) - STAGE_A_ALLOWLIST}"

    valid, failing = stage_a_valid(checks)
    verdict = "VALID" if valid else "VOID"

    path = write_result(
        "e16_stage_a",
        inputs=inputs,
        parameters={
            "lookbacks": list(LOOKBACKS), "ewma_com": EWMA_COM,
            "target_vol": TARGET_VOL, "cov_months": COV_MONTHS,
            "gross_cap": GROSS_CAP, "cost_side_perp": COST_SIDE_PERP,
            "first_hold": FIRST_HOLD, "last_hold_b": LAST_HOLD_B,
        },
        statistic=statistic,
        ci={},
        verdict=verdict,
        extra={"failing_checks": failing},
        results_dir=results_dir,
    )
    print(f"stage A: {verdict} (failing: {failing}) -> {path}")
    return verdict, failing, statistic


# --------------------------------------------------------------------------
# Stage B / C (guards + loaders only -- scoring bodies are stage B's own
# task once the user has answered the open "beats" question; see the
# vault's Handoff section)
# --------------------------------------------------------------------------

def run_stage_b(archive_root: Path, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    a_path = rd / "e16_stage_a.json"
    if not a_path.exists():
        raise RuntimeError("stage A result missing; run --stage A first")
    a_result = json.loads(a_path.read_text())
    if a_result.get("verdict") != "VALID":
        raise RuntimeError(f"stage A verdict is {a_result.get('verdict')!r}, not VALID; refusing")
    raise NotImplementedError(
        "stage B scoring is not built yet -- the vault's open question "
        "('what does beats mean') must be answered before it is written"
    )


def run_stage_c(archive_root: Path, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    b_path = rd / "e16_stage_b.json"
    if not b_path.exists():
        raise RuntimeError("stage B result missing; run --stage B first")
    b_result = json.loads(b_path.read_text())
    if b_result.get("verdict") != "PASS":
        raise RuntimeError(f"stage B verdict is {b_result.get('verdict')!r}, not PASS; refusing")
    raise NotImplementedError("stage C is not built yet")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["A", "B", "C"], required=True)
    parser.add_argument("--archive-root", default=str(ROOT / "data" / "archive"))
    args = parser.parse_args(argv)
    root = Path(args.archive_root)
    if args.stage == "A":
        run_stage_a(root)
    elif args.stage == "B":
        run_stage_b(root)
    else:
        run_stage_c(root)


if __name__ == "__main__":
    main()
