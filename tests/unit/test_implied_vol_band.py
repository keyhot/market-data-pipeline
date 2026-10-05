"""E17 (DVOL for the range band) model-plane tests: the x_iv feature's
timing, the loader cut, the gap count, the A0 kill rules, the synthetic
IV DGP and the one-regressor extension of E15's walk-forward.

The frozen design lives in the vault note `Docs/e17-preregistration.md`
(DRAFT until the user freezes it)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from model import implied_vol_band as ivb
from model.range_forecast import hourly_grid, seasonal_dgp, walk_forward

UTC = "UTC"


def _dvol(ts, close):
    ts = pd.DatetimeIndex(ts)
    return pd.DataFrame({"ts": ts, "open": close, "high": close, "low": close, "close": close})


# ---------------------------------------------------------------- x_iv timing

def test_iv_feature_uses_the_candle_that_starts_one_hour_before_the_origin():
    ts = pd.date_range("2021-03-24 00:00", periods=3, freq="h", tz=UTC)
    dvol = _dvol(ts, [50.0, 99.0, 70.0])
    origins = pd.DatetimeIndex([ts[1], ts[2]])
    x = ivb.iv_feature(dvol, origins)
    assert x.iloc[0] == pytest.approx(np.log(50.0 ** 2 / (1e4 * 8760)))
    assert x.iloc[1] == pytest.approx(np.log(99.0 ** 2 / (1e4 * 8760)))


def test_iv_feature_is_invariant_to_dropping_candles_at_or_after_the_origin():
    rng = np.random.default_rng(0)
    ts = pd.date_range("2021-03-24", periods=300, freq="h", tz=UTC)
    dvol = _dvol(ts, 60 + rng.normal(0, 5, len(ts)))
    full = ivb.iv_feature(dvol, ts)
    for t in ts[5::37]:
        trunc = ivb.iv_feature(dvol[dvol["ts"] < t], pd.DatetimeIndex([t]))
        assert trunc.iloc[0] == full.loc[t]


def test_iv_feature_missing_candle_gives_nan_never_filled():
    ts = pd.date_range("2021-03-24", periods=5, freq="h", tz=UTC)
    dvol = _dvol(ts.delete(2), [50.0, 51.0, 53.0, 54.0])
    x = ivb.iv_feature(dvol, ts)
    assert np.isnan(x.loc[ts[3]])          # needs the candle at ts[2]: missing
    assert np.isnan(x.loc[ts[0]])          # needs a candle before the series
    assert np.isfinite(x.loc[ts[4]])


# ---------------------------------------------------------------- loader cut

def test_truncate_values_cuts_strictly_before_2026():
    ts = pd.DatetimeIndex(["2025-12-31 23:00", "2026-01-01 00:00", "2026-03-01"], tz=UTC)
    out = ivb.truncate_values(_dvol(ts, [1.0, 2.0, 3.0]))
    assert list(out["ts"]) == [ts[0]]
    assert ivb.SCORING_CUTOFF == pd.Timestamp("2026-01-01", tz=UTC)


# ---------------------------------------------------------------- gap count

def test_gap_share_counts_hours_by_timestamp_only():
    start = pd.Timestamp("2021-03-24", tz=UTC)
    ts = pd.date_range(start, periods=10, freq="h", tz=UTC)
    present = ts.delete([3, 7]).append(pd.DatetimeIndex([ts[0]]))  # a duplicate
    present = present.append(pd.DatetimeIndex([start + pd.Timedelta(hours=50)]))  # outside
    g = ivb.gap_share(present, start, start + pd.Timedelta(hours=10))
    assert g == {"expected": 10, "present": 8, "missing": 2, "share": 0.2}


# ---------------------------------------------------------------- verdict pieces

def test_primary_pass_is_strict():
    assert ivb.primary_pass(1e-9)
    assert not ivb.primary_pass(0.0)
    assert not ivb.primary_pass(-0.1)


def test_two_sided_reject():
    assert ivb.two_sided_reject(0.1, 0.5)
    assert ivb.two_sided_reject(-0.5, -0.1)
    assert not ivb.two_sided_reject(-0.1, 0.1)


def test_paired_contrast_positive_when_candidate_band_is_tighter_and_calibrated():
    idx = pd.date_range("2025-01-01", periods=2000, freq="h", tz=UTC)
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.01, len(idx))
    good = pd.DataFrame({"L80": -0.0128, "U80": 0.0128, "r": r}, index=idx)
    wide = pd.DataFrame({"L80": -0.03, "U80": 0.03, "r": r}, index=idx)
    mean, lo, hi, n = ivb.paired_contrast(wide, good)
    assert n == 2000 and lo > 0 and mean > 0


def test_dev_blocks_are_six_nine_month_blocks_from_2021_04_plus_a_stub():
    b = ivb.dev_blocks()
    assert len(b) == 6
    assert b[0] == (pd.Timestamp("2021-04-01", tz=UTC), pd.Timestamp("2022-01-01", tz=UTC))
    assert b[-1] == (pd.Timestamp("2025-01-01", tz=UTC), pd.Timestamp("2025-10-01", tz=UTC))
    for (s0, e0), (s1, _) in zip(b, b[1:]):
        assert e0 == s1
    assert ivb.DEV_STUB == (pd.Timestamp("2025-10-01", tz=UTC), pd.Timestamp("2026-01-01", tz=UTC))


def test_a0_kills_count_blocks_per_symbol_not_pooled():
    ok = dict(gaps={"BTC": 0.01, "ETH": 0.01}, power=0.6)
    # 10 of 12 pooled would be >= 80%, but ETH alone has 4 of 6
    assert ivb.a0_kills(block_fails={"BTCUSDT": 6, "ETHUSDT": 4}, **ok) == ["kill2_control_block_fail_rate"]
    assert ivb.a0_kills(block_fails={"BTCUSDT": 5, "ETHUSDT": 5}, **ok) == []


def test_a0_kills_gap_and_power_thresholds():
    fails = {"BTCUSDT": 6, "ETHUSDT": 6}
    assert ivb.a0_kills(gaps={"BTC": 0.051, "ETH": 0.0}, block_fails=fails, power=0.9) == ["kill1_dvol_gaps"]
    assert ivb.a0_kills(gaps={"BTC": 0.05, "ETH": 0.0}, block_fails=fails, power=0.5) == []
    assert ivb.a0_kills(gaps={"BTC": 0.0, "ETH": 0.0}, block_fails=fails, power=0.499) == ["kill3_power"]


def test_choose_sd_u_is_smallest_reaching_target():
    med = {0.1: 9.5, 0.2: 12.0, 0.3: 16.0, 0.4: 22.0}
    assert ivb.choose_sd_u(med, 15.0) == 0.3
    assert ivb.choose_sd_u(med, 9.0) == 0.1
    assert ivb.choose_sd_u(med, 50.0) is None


def test_is_monotone_tolerates_mc_noise_but_flags_real_dips():
    ok, dips = ivb.is_monotone([0.02, 0.2, 0.5, 0.49, 0.9], n=300)
    assert ok and dips == [3]
    ok, dips = ivb.is_monotone([0.02, 0.2, 0.5, 0.3, 0.9], n=300)
    assert not ok


def test_block_dq_rejects_planted_clustering_and_not_iid():
    idx = pd.date_range("2021-04-01", "2022-01-01", freq="h", tz=UTC, inclusive="left")
    rng = np.random.default_rng(2)
    r_iid = rng.normal(0, 1, len(idx))
    wf_iid = pd.DataFrame({"L80": -1.2816, "U80": 1.2816, "r": r_iid}, index=idx)
    # clustered: misses come in runs (a persistent vol regime the band ignores)
    regime = np.repeat(rng.choice([0.6, 1.8], size=len(idx) // 24 + 1), 24)[: len(idx)]
    wf_cl = wf_iid.assign(r=r_iid * regime)
    blocks = [(idx[0], idx[-1] + pd.Timedelta(hours=1))]
    assert ivb.block_dq(wf_cl, blocks)[0]["p"] < 1e-6
    assert ivb.block_dq(wf_iid, blocks)[0]["n"] > 6000


# ---------------------------------------------------------------- synthetic DGP

START = pd.Timestamp("2024-04-01", tz=UTC)
IV_START = pd.Timestamp("2024-10-01", tz=UTC)


@pytest.fixture(scope="module")
def dgp_pair():
    a = ivb.iv_dgp(seed=5, sd_u=0.4, kappa=1.0, start=START, years=0.6, iv_start=IV_START)
    b = ivb.iv_dgp(seed=5, sd_u=0.4, kappa=0.0, start=START, years=0.6, iv_start=IV_START)
    return a, b


def test_iv_dgp_common_random_numbers_across_kappa(dgp_pair):
    (bars1, _, u1), (bars0, _, u0) = dgp_pair
    pd.testing.assert_frame_equal(bars1, bars0)
    pd.testing.assert_series_equal(u1, u0)


def test_iv_dgp_kappa_one_is_the_lagged_latent_state_and_never_the_next_shock(dgp_pair):
    (_, x1, u), _ = dgp_pair
    x1 = x1.dropna()
    expected = (u.shift(1) / 0.4).loc[x1.index]
    np.testing.assert_allclose(x1.to_numpy(), expected.to_numpy())
    phi = 0.5 ** (1 / 24)
    innov = (u - phi * u.shift(1)).loc[x1.index]
    assert abs(np.corrcoef(x1, innov)[0, 1]) < 0.05


def test_iv_dgp_kappa_zero_is_noise_and_iv_starts_late(dgp_pair):
    _, (_, x0, u) = dgp_pair
    assert x0.loc[: IV_START - pd.Timedelta(hours=1)].isna().all()
    xs = x0.dropna()
    assert abs(np.corrcoef(xs, u.shift(1).loc[xs.index])[0, 1]) < 0.05


def test_seasonal_dgp_latent_none_is_unchanged():
    a = seasonal_dgp(9, 0.05)
    b = seasonal_dgp(9, 0.05, latent=None)
    pd.testing.assert_frame_equal(a, b)


# ---------------------------------------------------------------- walk-forward extension

def test_walk_forward_extra_none_reproduces_e15_snapshot():
    """Snapshot taken on main 1821c46 BEFORE `extra` existed: E15's
    output must not move by one ulp-scale relative amount."""
    b = seasonal_dgp(3, 1.6, start=pd.Timestamp("2024-07-01", tz=UTC))
    g = hourly_grid(b)
    snap = {
        "seasonal_har": (2208, 26.70770764857044, -32.762037355121215, 32.27629958905803),
        "plain_har": (2208, 26.675748078053825, -32.43293044554696, 32.00960506489888),
    }
    for m, (n, s, lo, hi) in snap.items():
        wf = walk_forward(g, 1, m, pd.Timestamp("2025-08-01", tz=UTC),
                          pd.Timestamp("2025-11-01", tz=UTC), extra=None)
        assert len(wf) == n
        np.testing.assert_allclose(
            [wf["sigma"].sum(), wf["L80"].sum(), wf["U80"].sum()], [s, lo, hi], rtol=1e-12)


def test_walk_forward_extra_rejects_non_har_models():
    b = seasonal_dgp(3, 1.2, start=pd.Timestamp("2024-07-01", tz=UTC))
    g = hourly_grid(b)
    with pytest.raises(ValueError):
        walk_forward(g, 1, "rolling", pd.Timestamp("2025-08-01", tz=UTC),
                     pd.Timestamp("2025-09-01", tz=UTC), extra=pd.Series(0.0, index=g.index))


def test_strong_planted_iv_beats_the_control_and_nan_iv_drops_rows():
    bars, x_iv, _ = ivb.iv_dgp(seed=11, sd_u=ivb.STRONG_SD_U, kappa=1.0, start=START,
                               years=1.45, iv_start=IV_START,
                               half_life=ivb.STRONG_HALF_LIFE_H)
    g = hourly_grid(bars)
    s, e = pd.Timestamp("2025-05-01", tz=UTC), pd.Timestamp("2025-09-01", tz=UTC)
    x = x_iv.copy()
    holes = x.loc[s:e].index[::50]
    x.loc[holes] = np.nan
    ctrl = walk_forward(g, 1, "seasonal_har", s, e)
    cand = walk_forward(g, 1, "seasonal_har", s, e, extra=x)
    assert not cand.index.isin(holes).any()
    assert ctrl.index.isin(holes).any()
    _, lo, _, _ = ivb.paired_contrast(ctrl, cand)
    assert ivb.primary_pass(lo)
