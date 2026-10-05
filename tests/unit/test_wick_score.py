"""R3-b Stage A/B scorer: traded-minute sigma, fill-bar exit, control, bootstrap, plant."""
import numpy as np
import pandas as pd
import pytest

from model import wick_catch as wc

T0 = "2024-03-10 00:00"


def _bars(n=300, start=T0, seed=0, price=100.0, drift=0.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = price * np.exp(np.cumsum(rng.normal(drift, 1e-3, n)))
    return pd.DataFrame({"open": close, "high": close * 1.0002, "low": close * 0.9998,
                         "close": close}, index=idx)


# ---- flat minutes and traded-minute sigma -------------------------------

def test_flat_minute_is_high_eq_low_eq_prev_close():
    b = _bars(5)
    c1 = b["close"].iloc[1]
    b.iloc[2] = [c1, c1, c1, c1]                      # flat
    b.iloc[3] = [c1 * 1.01] * 4                       # single print at a new price: not flat
    f = wc.flat_minutes(b)
    assert list(f.to_numpy()) == [False, False, True, False, False]


def test_traded_sigma_skips_flat_minutes():
    b = _bars(200)
    b2 = b.copy()
    for i in range(100, 160, 2):                      # every other minute flat
        p = b2["close"].iloc[i - 1]
        b2.iloc[i] = [p, p, p, p]
    s = wc.trailing_sigma_traded(b2, window=60, min_traded=20)
    t = b2.index[170]
    r = np.log(b2["close"]).diff()
    win = r.loc[b2.index[110]:b2.index[169]]
    flat = wc.flat_minutes(b2).loc[win.index]
    assert s[t] == pytest.approx(win[~flat].std())
    assert s[t] > wc.trailing_sigma(b2["close"])[t]   # A0 sigma is shrunk by the zeros


def test_traded_sigma_needs_min_traded_and_no_gap():
    b = _bars(200)
    for i in range(100, 180):                         # long flat run
        p = b["close"].iloc[99]
        b.iloc[i] = [p, p, p, p]
    s = wc.trailing_sigma_traded(b, window=60, min_traded=30)
    assert np.isnan(s[b.index[175]])                  # < 30 traded minutes in window
    g = _bars(200).drop(_bars(200).index[120])
    s2 = wc.trailing_sigma_traded(g, window=60, min_traded=30)
    assert np.isnan(s2[g.index[150]]) and np.isfinite(s2[g.index[190]])


def test_traded_sigma_never_uses_bar_t():
    b = _bars(200)
    t = b.index[150]
    s0 = wc.trailing_sigma_traded(b)[t]
    b2 = b.copy()
    b2.loc[t, ["open", "high", "low", "close"]] = [1, 500, 0.5, 300]
    assert wc.trailing_sigma_traded(b2)[t] == s0


# ---- fills with the hold-present rule and limit recorded ------------------

def _wick(b, i, depth=0.05):
    b = b.copy()
    b.iloc[i, b.columns.get_loc("low")] = b["close"].iloc[i - 1] * (1 - depth)
    return b


def test_fill_records_limit_and_needs_hold_present():
    b = _wick(_bars(300), 150)
    sig = wc.trailing_sigma_traded(b)
    f = wc.wick_fills(b, k=8, tick=0.01, sigma=sig, require_hold_present=True)
    t = b.index[150]
    assert t in f.index and f.loc[t, "side"] == "bid"
    expect = wc.floor_tick(b["close"].iloc[149] * np.exp(-8 * sig[t]), 0.01)
    assert f.loc[t, "limit"] == pytest.approx(float(expect))
    gap = b.drop(b.index[170])                         # t+20 missing -> no order rests at t
    f2 = wc.wick_fills(gap, k=8, tick=0.01, sigma=wc.trailing_sigma_traded(gap),
                       require_hold_present=True)
    assert t not in f2.index
    end = b.iloc[:170]                                 # window ends before t+30
    f3 = wc.wick_fills(end, k=8, tick=0.01, sigma=wc.trailing_sigma_traded(end),
                       require_hold_present=True)
    assert t not in f3.index


# ---- scoring: exit at close t+30, sign, entry at limit, cost --------------

def _one_fill(side, limit, t):
    return pd.DataFrame({"side": [side], "limit": [limit], "through_ticks": [1.0]},
                        index=pd.DatetimeIndex([t]))


def test_score_long_exit_at_close_t_plus_30_entry_at_limit():
    b = _bars(300)
    t = b.index[150]
    b.loc[b.index[179], "close"] = 1.0                 # t+29 (exit-index mutant)
    b.loc[b.index[181], "close"] = 1.0                 # t+31
    b.loc[t, "close"] = 1.0                            # close_t (entry mutant)
    ex = b["close"].iloc[180]
    s = wc.score_fills(b, _one_fill("bid", 95.0, t), hold=30, cost_bps=7.5)
    assert s["exit"].iloc[0] == ex
    assert s["net_bps"].iloc[0] == pytest.approx((ex / 95.0 - 1) * 1e4 - 7.5)


def test_score_short_sign():
    b = _bars(300)
    t = b.index[150]
    ex = b["close"].iloc[180]
    s = wc.score_fills(b, _one_fill("ask", ex * 1.02, t), hold=30, cost_bps=7.5)
    assert s["net_bps"].iloc[0] == pytest.approx((ex * 1.02 - ex) / (ex * 1.02) * 1e4 - 7.5)
    assert s["net_bps"].iloc[0] > 0


# ---- control ---------------------------------------------------------------

def test_control_same_day_outside_hold_same_direction():
    b = _bars(1440, start="2024-03-10 00:00", drift=2e-4)   # strong up-day
    t = b.index[700]
    rng = np.random.default_rng(0)
    for side, sign in (("bid", 1), ("ask", -1)):
        f = _one_fill(side, b["close"].iloc[699], t)
        c, draws = wc.control_returns(b, f, m=10, hold=30, cost_bps=7.5, rng=rng, return_draws=True)
        u = draws[0]
        assert len(u) == 10
        assert all(x.normalize() == t.normalize() for x in u)
        assert all((x + pd.Timedelta(minutes=30) < t) or (x > t + pd.Timedelta(minutes=30)) for x in u)
        g = b["close"]
        exp = np.mean([sign * (g[x + pd.Timedelta(minutes=30)] - g[x]) / g[x] * 1e4 for x in u]) - 7.5
        assert c[0] == pytest.approx(exp)
        assert sign * (c[0] + 7.5) > 0


# ---- bootstrap, plant, placebo, window guard -------------------------------

def test_cluster_bootstrap_bounds():
    rng = np.random.default_rng(1)
    x = rng.normal(50, 10, 400)
    c = rng.normal(0, 10, 400)
    day = np.repeat(np.arange(100), 4)
    r = wc.cluster_bootstrap(x, c, day, n_boot=999, rng=rng)
    assert r["lb_primary"] > 0 and r["lb_paired"] > 0
    assert r["ci95_primary"][0] < r["mean_primary"] < r["ci95_primary"][1]
    assert r["lb_primary"] > r["ci95_primary"][0]
    r0 = wc.cluster_bootstrap(-x, c, day, n_boot=999, rng=rng)
    assert r0["lb_primary"] < 0 and not r0["pass"]


def test_plant_recovers_200_bps_through_the_scorer():
    b = _wick(_wick(_bars(600), 150), 300, depth=-0.0)  # second call no-op
    f = wc.wick_fills(b, k=8, tick=0.01, sigma=wc.trailing_sigma_traded(b), require_hold_present=True)
    assert len(f) >= 1
    s0 = wc.score_fills(b, f)
    s1 = wc.score_fills(wc.plant_exit(b, f, 200.0), f)
    d = (s1["net_bps"] - s0["net_bps"]).to_numpy()
    assert np.all(np.abs(d - 200) < 10)


def test_placebo_is_same_day_same_side_market_entry():
    b = _bars(1440)
    t = b.index[700]
    f = _one_fill("ask", 1.0, t)
    p = wc.placebo_events(b, f, rng=np.random.default_rng(3))
    u = p.index[0]
    assert u.normalize() == t.normalize() and p["side"].iloc[0] == "ask"
    assert p["limit"].iloc[0] == b.loc[u, "close"]


def test_window_guard_returns_only_window():
    idx = pd.date_range("2023-12-31 23:00", "2025-01-01 01:00", freq="1h", tz="UTC")
    b = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=idx)
    w = wc.slice_window(b, "2024-01-01", "2025-01-01")
    assert w.index.min() >= pd.Timestamp("2024-01-01", tz="UTC")
    assert w.index.max() < pd.Timestamp("2025-01-01", tz="UTC")


def test_data_gate_refuses_flat_or_gappy_tape():
    b = _bars(300)
    assert wc.data_gate(b, b.index.min(), b.index.max() + pd.Timedelta(minutes=1))["ok"]
    for i in range(0, 300, 5):
        p = b["close"].iloc[i - 1] if i else b["close"].iloc[0]
        b.iloc[i] = [p, p, p, p]
    gate = wc.data_gate(b, b.index.min(), b.index.max() + pd.Timedelta(minutes=1), max_flat=0.10)
    assert not gate["ok"] and gate["flat_share"] > 0.10
