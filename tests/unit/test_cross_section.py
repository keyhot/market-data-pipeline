"""E13 cross-sectional ranking plumbing (vault
`Docs/cross-sectional-ranking-experiment.md`). Every timing test states a
RULE over all days/symbols, not one instance: perturb the future, and nothing
at or before d may move."""

import numpy as np
import pandas as pd
import pytest

from model.cross_section import (
    block_bootstrap_ci,
    cs_rank,
    daily_features,
    decile_weights,
    eligibility,
    forward_funding,
    forward_returns,
    funding_by_day,
    is_candidate,
    oi_at_close,
    portfolio_pnl,
    random_null_weights,
    slippage_depth,
    slippage_hourly,
    slippage_per_side,
    traded_run_length,
    training_days,
    walk_forward_scores,
)


def _days(n, start="2021-01-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


def _panel(n_days=400, n_sym=60, seed=0):
    rng = np.random.default_rng(seed)
    days = _days(n_days)
    cols = [f"S{i:02d}USDT" for i in range(n_sym)]
    logret = rng.normal(0, 0.04, (n_days, n_sym))
    close = pd.DataFrame(100 * np.exp(np.cumsum(logret, axis=0)), days, cols)
    qv = pd.DataFrame(rng.lognormal(16, 1, (n_days, n_sym)), days, cols)
    return close, qv


# ------------------------------------------------------------- universe ---

def test_candidate_filter():
    assert is_candidate("SOLUSDT") and is_candidate("1000PEPEUSDT")
    for bad in ("BTCUSDT_270326", "USDCUSDT", "BTCDOMUSDT", "ETHUSDC", "哈基米USDT"):
        assert not is_candidate(bad)


def test_run_length_resets_on_zero_volume_and_missing_days():
    vol = pd.DataFrame({"A": [1, 1, 0, 1, np.nan, 1, 1]}, index=_days(7))
    assert traded_run_length(vol)["A"].tolist() == [1, 2, 0, 1, 0, 1, 2]


def test_eligibility_is_point_in_time():
    """Rule: eligibility on day d is a function of rows <= d only. Rewrite
    everything after d (volume, liquidity) and nothing at or before d moves."""
    close, qv = _panel(200, 80)
    vol = qv.copy()
    base = eligibility(vol, qv, warmup=20, liquidity_window=10, top_n=15)
    rng = np.random.default_rng(1)
    for d in (40, 90, 150):
        vol2, qv2 = vol.copy(), qv.copy()
        qv2.iloc[d + 1:] = rng.lognormal(20, 3, qv2.iloc[d + 1:].shape)
        vol2.iloc[d + 1:] = rng.choice([0.0, 1.0], vol2.iloc[d + 1:].shape)
        again = eligibility(vol2, qv2, warmup=20, liquidity_window=10, top_n=15)
        pd.testing.assert_frame_equal(base.iloc[:d + 1], again.iloc[:d + 1])


def test_eligibility_needs_unbroken_warmup_and_drops_delisted_rows():
    n = 60
    vol = pd.DataFrame(1.0, index=_days(n), columns=["OLD", "NEW"])
    vol.loc[vol.index[40:], "OLD"] = 0.0          # delisted: flat zero-volume rows
    vol.loc[vol.index[:30], "NEW"] = 0.0          # listed on day 30
    vol.loc[vol.index[45], "NEW"] = 0.0           # a gap (ticker reuse) restarts it
    qv = vol * 1e7
    e = eligibility(vol, qv, warmup=5, liquidity_window=3, top_n=5)
    assert not e["OLD"].iloc[40:].any()
    assert e["OLD"].iloc[10:40].all()
    new = e["NEW"]
    first = new[new].index[0]
    assert first == vol.index[35]                 # day 30 + 5 prior traded days
    assert not new.iloc[45:51].any() and new.iloc[51]


def test_eligibility_takes_top_n_by_trailing_liquidity_with_floor():
    days = _days(20)
    vol = pd.DataFrame(1.0, index=days, columns=list("ABCD"))
    qv = pd.DataFrame({"A": 4e6, "B": 3e6, "C": 2e6, "D": 1e6}, index=days)
    e = eligibility(vol, qv, warmup=2, liquidity_window=3, top_n=2,
                    min_quote_volume=1.5e6)
    assert e.iloc[-1].tolist() == [True, True, False, False]
    e = eligibility(vol, qv, warmup=2, liquidity_window=3, top_n=4,
                    min_quote_volume=2.5e6)
    assert e.iloc[-1].tolist() == [True, True, False, False]


# ------------------------------------------------------------- funding ----

def test_funding_by_day_assigns_the_settlement_at_midnight_to_the_day_before():
    """Rule: holding day H owns events in (H 00:00, H+1 00:00]; stamps carry
    ms jitter."""
    days = _days(3)
    t = pd.to_datetime(["2021-01-02 00:00:00.004",   # closes day 1 -> day 1
                        "2021-01-02 08:00:00.000",   # day 2
                        "2021-01-03 00:00:00.000"],  # closes day 2 -> day 2
                       utc=True)
    ev = pd.DataFrame({"symbol": "A", "time": t, "rate": [0.1, 0.01, 0.001]})
    f = funding_by_day(ev, days)["A"]
    assert f.tolist() == pytest.approx([0.1, 0.011, 0.0])
    assert forward_funding(f.to_frame())["A"].iloc[0] == pytest.approx(0.011)


def test_funding_by_day_counts_every_event_at_any_interval():
    days = _days(2)
    hourly = pd.date_range("2021-01-01 01:00", periods=24, freq="h", tz="UTC")
    ev = pd.DataFrame({"symbol": "H", "time": hourly, "rate": 0.001})
    assert funding_by_day(ev, days)["H"].tolist() == pytest.approx([0.024, 0.0])


# ------------------------------------------------------------- returns ----

def test_forward_return_is_next_close_and_zero_when_the_next_row_is_missing():
    close = pd.DataFrame({"A": [100.0, 110.0, 99.0, np.nan]}, index=_days(4))
    assert forward_returns(close)["A"].tolist() == pytest.approx([0.1, -0.1, 0.0, 0.0])


def test_features_never_read_after_the_close():
    """Rule: features at d depend on rows <= d only."""
    close, _ = _panel(120, 10)
    fund = pd.DataFrame(0.0001, index=close.index, columns=close.columns)
    oi = close * 3
    base = daily_features(close, fund, oi)
    for d in (35, 70, 100):
        c2, f2, o2 = close.copy(), fund.copy(), oi.copy()
        c2.iloc[d + 1:] *= 5
        f2.iloc[d + 1:] = 0.9
        o2.iloc[d + 1:] *= 7
        again = daily_features(c2, f2, o2)
        for name, panel in base.items():
            pd.testing.assert_frame_equal(panel.iloc[:d + 1], again[name].iloc[:d + 1],
                                          obj=name)


def test_oi_at_close_respects_the_delay_and_skips_zero_rows():
    days = _days(1)                              # closes 2021-01-02 00:00
    close = days[0] + pd.Timedelta(days=1)
    idx = pd.DatetimeIndex([close - pd.Timedelta(minutes=10),
                            close - pd.Timedelta(minutes=5),
                            close - pd.Timedelta(minutes=4)])
    oi = pd.Series([1.0, 2.0, 3.0], index=idx)
    assert oi_at_close(oi, days).iloc[0] == 2.0   # the -4 min row is not yet out
    oi[idx[1]] = 0.0
    assert oi_at_close(oi, days).iloc[0] == 1.0
    assert np.isnan(oi_at_close(oi, days, max_age=pd.Timedelta(minutes=4)).iloc[0])


def test_slippage_is_floored_and_point_in_time():
    close, _ = _panel(80, 5)
    high, low = close * 1.03, close * 0.97
    s = slippage_per_side(high, low, close)
    assert (s.dropna() >= 0.0001).all().all()
    h2 = high.copy()
    h2.iloc[51:] *= 3
    pd.testing.assert_frame_equal(s.iloc[:51], slippage_per_side(h2, low, close).iloc[:51])


# ------------------------------------------------------------- model ------

def test_training_days_are_strictly_before_the_refit():
    days = _days(100)
    for refit in days[10::13]:
        train = training_days(days, refit)
        # decision at refit's close (refit + 1 day) must know every target:
        # a target of day d is known at d + 2 days.
        assert (train + pd.Timedelta(days=2) <= refit + pd.Timedelta(days=1)).all()
        assert train[-1] == refit - pd.Timedelta(days=1)


def test_walk_forward_scores_do_not_see_targets_from_the_month_they_score():
    """Rule: rewriting targets on or after month start R leaves every score
    in R's month unchanged."""
    close, qv = _panel(240, 30)
    elig = eligibility(qv, qv, warmup=31, liquidity_window=5, top_n=30)
    feats = daily_features(close, pd.DataFrame(0.0, index=close.index,
                                               columns=close.columns))
    target = forward_returns(close)
    start, end = pd.Timestamp("2021-05-01", tz="UTC"), pd.Timestamp("2021-08-15", tz="UTC")
    base = walk_forward_scores(feats, target, elig, start, end, min_train_days=60)
    for month in ("2021-06-01", "2021-07-01"):
        R = pd.Timestamp(month, tz="UTC")
        t2 = target.copy()
        t2.loc[t2.index >= R] = np.random.default_rng(3).normal(0, 1, t2.loc[t2.index >= R].shape)
        again = walk_forward_scores(feats, t2, elig, start, end, min_train_days=60)
        stop = R + pd.offsets.MonthBegin(1)
        span = (base.index >= R) & (base.index < stop)
        pd.testing.assert_frame_equal(base[span], again[span])


def test_cs_rank_is_neutral_for_missing_values_and_blank_outside_the_universe():
    days = _days(1)
    p = pd.DataFrame({"A": [1.0], "B": [np.nan], "C": [3.0], "D": [9.0]}, index=days)
    e = pd.DataFrame({"A": [True], "B": [True], "C": [True], "D": [False]}, index=days)
    r = cs_rank(p, e).iloc[0]
    assert r["A"] < 0 < r["C"] and r["B"] == 0 and np.isnan(r["D"])


# ------------------------------------------------------------- portfolio --

def test_decile_weights_are_market_neutral_with_the_decile_book():
    scores = pd.DataFrame([np.arange(50, dtype=float)], index=_days(1))
    w = decile_weights(scores).iloc[0]
    assert (w > 0).sum() == 5 and (w < 0).sum() == 5
    assert w.sum() == pytest.approx(0) and w[w > 0].sum() == pytest.approx(1)
    assert (w.iloc[-5:] > 0).all() and (w.iloc[:5] < 0).all()
    small = pd.DataFrame([np.arange(12, dtype=float)], index=_days(1))
    assert (decile_weights(small).iloc[0] > 0).sum() == 3      # min per side
    tiny = pd.DataFrame([np.arange(5, dtype=float)], index=_days(1))
    assert (decile_weights(tiny).iloc[0] == 0).all()


def test_costs_are_charged_on_what_trades_not_on_the_whole_book():
    days = _days(4)
    w = pd.DataFrame({"A": [0.5, 0.5, 0.5, 0.0], "B": [-0.5, -0.5, -0.5, 0.0]}, index=days)
    zero = pd.DataFrame(0.0, index=days, columns=w.columns)
    pnl = portfolio_pnl(w, zero, zero, fee_per_side=0.0005, slippage=0.0001)
    assert pnl["cost"].tolist() == pytest.approx([0.0006, 0, 0, 0.0006])
    assert pnl["turnover"].tolist() == pytest.approx([1.0, 0, 0, 1.0])


def test_per_symbol_slippage_and_funding_sign():
    days = _days(1)
    w = pd.DataFrame({"A": [1.0], "B": [-1.0]}, index=days)
    r = pd.DataFrame({"A": [0.02], "B": [-0.01]}, index=days)
    f = pd.DataFrame({"A": [0.001], "B": [0.001]}, index=days)   # both positive
    slip = pd.DataFrame({"A": [0.0001], "B": [0.0020]}, index=days)
    row = portfolio_pnl(w, r, f, 0.0005, slip).iloc[0]
    assert row["gross"] == pytest.approx(0.03)
    assert row["funding"] == pytest.approx(0.0)       # long pays, short receives
    assert row["cost"] == pytest.approx(0.0006 + 0.0025)
    f2 = pd.DataFrame({"A": [0.001], "B": [0.0]}, index=days)
    assert portfolio_pnl(w, r, f2, 0, 0).iloc[0]["funding"] == pytest.approx(-0.001)


def test_random_null_matches_exposure_and_turnover_per_day():
    close, qv = _panel(120, 60)
    elig = eligibility(qv, qv, warmup=5, liquidity_window=5, top_n=50)
    rng = np.random.default_rng(0)
    scores = pd.DataFrame(rng.normal(size=close.shape), close.index, close.columns).where(elig)
    scores = scores.rolling(20, min_periods=1).mean().where(elig)   # sticky -> partial retention
    w = decile_weights(scores)
    null = random_null_weights(w, elig, np.random.default_rng(1))
    zero = pd.DataFrame(0.0, index=w.index, columns=w.columns)
    model_t = portfolio_pnl(w, zero, zero, 0, 0)["turnover"]
    null_t = portfolio_pnl(null, zero, zero, 0, 0)["turnover"]
    active = (w != 0).any(axis=1)
    assert active.sum() > 50
    np.testing.assert_allclose((null > 0).sum(axis=1), (w > 0).sum(axis=1))
    np.testing.assert_allclose((null < 0).sum(axis=1), (w < 0).sum(axis=1))
    np.testing.assert_allclose(null.clip(lower=0).sum(axis=1)[active], 1.0)
    same = np.isclose(null_t[active], model_t[active], atol=1e-9)
    assert same.mean() > 0.9         # exact unless a retained null name left the universe
    assert null_t[active].mean() == pytest.approx(model_t[active].mean(), rel=0.05)
    assert not (null.where(~elig.fillna(False)).fillna(0) != 0).any().any()
    assert (null != w).any().any()


def test_block_bootstrap_ci_brackets_the_mean_and_is_seeded():
    x = np.random.default_rng(0).normal(0.01, 0.02, 500)
    m, lo, hi = block_bootstrap_ci(x)
    assert lo < m < hi and (lo, hi) == block_bootstrap_ci(x)[1:]


# ------------------------------------------------------------- control ----

def test_leaked_feature_gives_a_large_spread_and_the_random_null_gives_none():
    """The positive control of the pre-registration: a feature carrying the
    next-day return must produce a large, obviously-passing spread through
    the whole pipeline (walk-forward fit, weights, costs), and a random
    ranking with its turnover and exposure must score ~0."""
    close, qv = _panel(420, 60, seed=5)
    elig = eligibility(qv, qv, warmup=31, liquidity_window=5, top_n=50)
    fwd = forward_returns(close)
    fund = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    feats = daily_features(close, fund)
    leaked = dict(feats, ret_30=fwd)
    start = pd.Timestamp("2021-08-01", tz="UTC")
    end = close.index[-2]
    scores = walk_forward_scores(leaked, fwd, elig, start, end, min_train_days=150)
    w = decile_weights(scores)
    pnl = portfolio_pnl(w, fwd, forward_funding(fund), 0.0005, 0.0001)
    pnl = pnl[(pnl.index >= start) & (pnl.index <= end)]
    m, lo, hi = block_bootstrap_ci(pnl["net"])
    assert lo > 0.03                                  # >300 bps a day, net
    null_means = []
    for seed in range(20):
        nw = random_null_weights(w, elig, np.random.default_rng(seed))
        npnl = portfolio_pnl(nw, fwd, fund, 0.0, 0.0)
        null_means.append(npnl.loc[pnl.index, "gross"].mean())
    assert abs(np.mean(null_means)) < 0.002
    assert max(null_means) < lo / 5

    honest = walk_forward_scores(feats, fwd, elig, start, end, min_train_days=150)
    hp = portfolio_pnl(decile_weights(honest), fwd, fund, 0.0, 0.0)
    _, hlo, hhi = block_bootstrap_ci(hp.loc[pnl.index, "gross"])
    assert hlo < 0 < hhi                              # a random walk has no edge


# ------------------------------------------------ Amendment A1 costs -----

def _hourly(n_days=40, n_sym=3, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n_days * 24, freq="h", tz="UTC")
    cols = [f"S{i}USDT" for i in range(n_sym)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.005, (len(idx), n_sym)), 0)), idx, cols)
    high = close * np.exp(np.abs(rng.normal(0, 0.003, close.shape)))
    low = close * np.exp(-np.abs(rng.normal(0, 0.003, close.shape)))
    return high, low, close


def test_hourly_ar_recovers_a_pure_bid_ask_bounce():
    """Mid fixed, closes alternate ask/bid, each bar spans the spread: AR on
    the bars must return the half-spread exactly."""
    idx = pd.date_range("2023-01-01", periods=40 * 24, freq="h", tz="UTC")
    s = 0.002
    sign = np.where(np.arange(len(idx)) % 2 == 0, 1, -1)
    close = pd.DataFrame({"AUSDT": 100 * (1 + sign * s / 2)}, idx)
    high = pd.DataFrame({"AUSDT": 100 * (1 + s / 2)}, idx)
    low = pd.DataFrame({"AUSDT": 100 * (1 - s / 2)}, idx)
    days = pd.date_range("2023-01-01", periods=40, freq="D", tz="UTC")
    out = slippage_hourly(high, low, close, days)
    assert out["AUSDT"].iloc[20:].sub(s / 2).abs().max() < 2e-5
    assert out["AUSDT"].iloc[:14].isna().all()      # fewer than 360 bars


def test_hourly_ar_is_point_in_time_for_every_day():
    """RULE: rewriting every hourly bar that opens at or after d+1 00:00
    leaves the estimate at d, and every day before it, unchanged."""
    high, low, close = _hourly()
    days = pd.date_range("2023-01-01", periods=40, freq="D", tz="UTC")
    base = slippage_hourly(high, low, close, days)
    assert (base.dropna() >= 0.0001).all().all()
    for d in days[16::5]:
        after = high.index >= d + pd.Timedelta(days=1)
        h2 = high.copy()
        h2.loc[after] *= 1.5
        moved = slippage_hourly(h2, low, close, days)
        pd.testing.assert_frame_equal(base.loc[:d], moved.loc[:d])
        assert not base.loc[d + pd.Timedelta(days=1):].equals(moved.loc[d + pd.Timedelta(days=1):])


def test_depth_slippage_is_the_linear_book_formula_as_of_backward():
    days = pd.date_range("2023-01-01", periods=21, freq="D", tz="UTC")
    wed = pd.DatetimeIndex(["2023-01-04", "2023-01-11", "2023-01-18"], tz="UTC")
    d1 = pd.DataFrame({"AUSDT": [1e6, 2e6, 4e6]}, wed)
    out = slippage_depth(d1, days, ["AUSDT", "BUSDT"], order_usd=1e5)
    assert out["AUSDT"].loc[:"2023-01-03"].isna().all()     # before the first sample
    assert out["BUSDT"].isna().all()                        # never sampled
    assert out.loc["2023-01-04", "AUSDT"] == pytest.approx(1e-4 + 0.5 * 0.01 * 1e5 / 1e6)
    assert out.loc["2023-01-10", "AUSDT"] == pytest.approx(1e-4 + 0.5 * 0.01 * 1e5 / 1e6)
    assert out.loc["2023-01-11", "AUSDT"] == pytest.approx(1e-4 + 0.5 * 0.01 * 1e5 / 2e6)
    assert out.loc["2023-01-21", "AUSDT"] == pytest.approx(1e-4 + 0.5 * 0.01 * 1e5 / 4e6)


def test_depth_slippage_is_point_in_time_for_every_sample():
    """RULE: a sample taken after d never reaches d."""
    days = pd.date_range("2023-01-01", periods=60, freq="D", tz="UTC")
    wed = pd.date_range("2023-01-04", periods=8, freq="7D", tz="UTC")
    d1 = pd.DataFrame({"AUSDT": np.linspace(1e6, 8e6, 8)}, wed)
    base = slippage_depth(d1, days, ["AUSDT"], order_usd=1e5)
    for d in days[3:]:
        d2 = d1.copy()
        d2.loc[d2.index > d] *= 10
        pd.testing.assert_frame_equal(base.loc[:d], slippage_depth(d2, days, ["AUSDT"], order_usd=1e5).loc[:d])
