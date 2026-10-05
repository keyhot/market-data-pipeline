"""R3-b Stage A0: wick-catch trigger purity, C1 protection, gaps, synthetic power."""
import numpy as np
import pandas as pd
import pytest

from model import wick_catch as wc


def _bars(n=200, start="2023-03-10 00:00", seed=0, price=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = price * np.exp(np.cumsum(rng.normal(0, 1e-3, n)))
    return pd.DataFrame({"open": close, "high": close * 1.0002, "low": close * 0.9998,
                         "close": close}, index=idx)


# ---- C1 protection -------------------------------------------------------

@pytest.mark.parametrize("ts,reserved", [
    ("2024-05-01 00:00", True), ("2024-05-01 23:59", True),
    ("2024-05-02 00:59", True), ("2024-05-02 01:00", False),
    ("2024-04-30 23:59", False), ("2026-09-29 03:16", False),
    ("2026-09-29 03:17", True), ("2026-10-03 12:00", True),
])
def test_c1_reserved(ts, reserved):
    idx = pd.DatetimeIndex([pd.Timestamp(ts, tz="UTC")])
    assert bool(wc.c1_reserved(idx)[0]) is reserved


def test_parse_drops_c1_minutes_before_returning():
    ms = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp() * 1000)
    raw = [{"time": ms(t), "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "3"}
           for t in ("2024-04-30 23:59", "2024-05-01 00:00", "2024-05-02 00:59", "2024-05-02 01:00")]
    df = wc.parse_candles(raw)
    assert list(df.index.strftime("%m-%d %H:%M")) == ["04-30 23:59", "05-02 01:00"]
    assert df["low"].dtype == float


def test_fetch_paginates_and_stops_at_end():
    start, end = pd.Timestamp("2023-01-03", tz="UTC"), pd.Timestamp("2023-01-03 00:10", tz="UTC")
    calls = []

    def getter(from_s, to_s):
        calls.append(from_s)
        t0 = pd.Timestamp(from_s, unit="s", tz="UTC")
        times = [t0 + pd.Timedelta(minutes=i) for i in range(4)]
        return [{"time": int(t.timestamp() * 1000), "open": 1, "high": 1, "low": 1,
                 "close": 1, "volume": 1} for t in times if t < end]

    df = wc.fetch_range(getter, start, end)
    assert len(df) == 10 and df.index.is_unique and df.index.max() < end
    assert len(calls) == 3


# ---- sigma and trigger purity -------------------------------------------

def test_sigma_never_uses_bar_t():
    b = _bars()
    s0 = wc.trailing_sigma(b["close"])
    b2 = b.copy()
    b2.iloc[100, b2.columns.get_loc("close")] *= 1.05
    s1 = wc.trailing_sigma(b2["close"])
    assert s0.iloc[100] == s1.iloc[100]        # bar t not in sigma_t
    assert s0.iloc[101] != s1.iloc[101]        # bar t is in sigma_{t+1}
    assert s0.iloc[:61].isna().all() and np.isfinite(s0.iloc[61])


def test_sigma_mutant_including_bar_t_is_caught():
    """The check above must fail for a sigma that includes bar t."""
    b = _bars()
    mutant = lambda c: np.log(c).diff().rolling(60).std()
    b2 = b.copy()
    b2.iloc[100, b2.columns.get_loc("close")] *= 1.05
    assert mutant(b["close"]).iloc[100] != mutant(b2["close"]).iloc[100]


def test_sigma_restarts_after_gap():
    b = _bars(300).drop(index=_bars(300).index[150])
    s = wc.trailing_sigma(b["close"])
    t = b.index
    after = s[(t > t[149]) & (t <= t[149] + pd.Timedelta(minutes=61))]
    assert after.isna().all()
    assert np.isfinite(s[t > t[149] + pd.Timedelta(minutes=62)].iloc[0])


def _with_wick(b, i, depth, side="low"):
    b = b.copy()
    c = b["close"].iloc[i - 1]
    if side == "low":
        b.iloc[i, b.columns.get_loc("low")] = c * (1 - depth)
    else:
        b.iloc[i, b.columns.get_loc("high")] = c * (1 + depth)
    return b


def test_fill_needs_trade_through_by_one_tick():
    b = _bars(200, price=100.0)
    sig = wc.trailing_sigma(b["close"]).iloc[120]
    bid = wc.floor_tick(b["close"].iloc[119] * np.exp(-4 * sig), 0.01)
    touch = b.copy(); touch.iloc[120, touch.columns.get_loc("low")] = bid
    thru = b.copy(); thru.iloc[120, thru.columns.get_loc("low")] = bid - 0.01
    assert wc.wick_fills(touch, k=4, tick=0.01).empty
    f = wc.wick_fills(thru, k=4, tick=0.01)
    assert list(f.index) == [b.index[120]] and f["side"].iloc[0] == "bid"
    assert wc.wick_fills(touch, k=4, tick=0.01, touch=True).shape[0] == 1


def test_ask_side_and_ambiguous_bar():
    b = _with_wick(_bars(200), 120, 0.05, "high")
    assert wc.wick_fills(b, k=4, tick=0.01)["side"].tolist() == ["ask"]
    both = _with_wick(b, 120, 0.05, "low")
    f = wc.wick_fills(both, k=4, tick=0.01)
    assert f.empty and f.attrs["ambiguous"] == 1


def test_one_position_per_symbol_hold_30():
    b = _with_wick(_with_wick(_bars(300), 120, 0.05), 140, 0.05)
    assert len(wc.wick_fills(b, k=4, tick=0.01)) == 1
    b = _with_wick(_with_wick(_bars(300), 120, 0.05), 151, 0.05)
    assert len(wc.wick_fills(b, k=4, tick=0.01)) == 2
    b = _with_wick(_with_wick(_bars(300), 120, 0.05), 150, 0.05)
    assert len(wc.wick_fills(b, k=4, tick=0.01)) == 1


def test_prefix_invariance_no_future_bar_moves_a_fill():
    b = _bars(400, seed=3)
    for i in (100, 180, 260):
        b = _with_wick(b, i, 0.03)
    base = wc.wick_fills(b, k=5, tick=0.01)
    cut = b.index[200]
    m = b.copy()
    m.loc[m.index > cut, ["open", "high", "low", "close"]] *= 1.7
    m.loc[m.index > cut, "low"] *= 0.5
    after = wc.wick_fills(m, k=5, tick=0.01)
    assert base[base.index <= cut].equals(after[after.index <= cut])
    assert len(base[base.index <= cut]) == 2


# ---- gaps, k choice, day tuples -----------------------------------------

def test_gap_share_excludes_c1_blocks():
    idx = pd.date_range("2024-04-30 00:00", "2024-05-03 00:00", freq="1min", tz="UTC", inclusive="left")
    keep = idx[~wc.c1_reserved(idx)]
    present = keep.delete([10, 20])
    g = wc.gap_share(present, idx[0], idx[-1] + pd.Timedelta(minutes=1))
    assert g["missing"] == 2 and g["expected"] == len(keep)


def test_choose_k_smallest_with_rate_at_most_target():
    rates = {2.0: 5.0, 2.25: 1.4, 2.5: 0.99, 2.75: 0.5}
    assert wc.choose_k(lambda k: rates[k], [2.0, 2.25, 2.5, 2.75], target=1.0) == 2.5
    assert wc.choose_k(lambda k: 9.0, [2.0], target=1.0) is None


def test_eligible_days_skip_first_of_month():
    assert wc.eligible_days("2025-01-01", "2025-02-28") == 59 - 2


def test_day_tuples_counts_and_cofills():
    t = lambda s: pd.Timestamp(s, tz="UTC")
    fx = pd.DatetimeIndex([t("2023-03-05 10:00"), t("2023-03-05 18:00")])
    fe = pd.DatetimeIndex([t("2023-03-05 10:02"), t("2023-03-06 09:00")])
    d = wc.day_tuples(fx, fe, "2023-03-05", "2023-03-07")
    assert d.tolist() == [[2, 1, 1], [0, 1, 0], [0, 0, 0]]


# ---- synthetic power ----------------------------------------------------

DAYS = np.array([[1, 1, 1], [0, 1, 0], [2, 0, 0], [1, 1, 0], [0, 0, 0]] * 4)


def test_plant_reaches_power_and_sign_mutant_fails():
    hi = wc.power(200, DAYS, n_days=400, icc=0.1, n_seeds=40, n_boot=199, seed=1)
    assert hi["joint"] >= 0.95
    neg = wc.power(-200, DAYS, n_days=400, icc=0.1, n_seeds=40, n_boot=199, seed=2)
    assert neg["joint"] <= 0.03


def test_null_size_and_iid_mutant_over_rejects():
    cl = wc.power(0, DAYS, n_days=300, icc=0.4, n_seeds=150, n_boot=199, seed=4)
    iid = wc.power(0, DAYS, n_days=300, icc=0.4, n_seeds=150, n_boot=199, seed=4, iid_bootstrap=True)
    assert cl["primary"] <= 0.12
    assert iid["primary"] > cl["primary"]


def test_scale_tuples_to_gating_rate():
    d = wc.scale_tuples(DAYS, per_day=3.0, rng=np.random.default_rng(0), n_days=5000)
    assert abs(d[:, :2].sum(1).mean() - 3.0) < 0.15


def test_mde_interpolates():
    assert wc.mde([0, 10, 20], [0.1, 0.6, 1.0]) == pytest.approx(15.0)
    assert wc.mde([0, 10], [0.1, 0.2]) is None
