"""R3-a (short new perp listings, days 1-30): events, windows, loader cut, A0 harness."""
import numpy as np
import pandas as pd
import pytest

from model import listing_short as ls


# --- token normalisation / universe -------------------------------------------------

@pytest.mark.parametrize("sym,tok", [
    ("BONKUSDT", "BONK"), ("1000BONKUSDT", "BONK"), ("1000000MOGUSDT", "MOG"),
    ("1MBABYDOGEUSDT", "BABYDOGE"), ("SOLUSDC", "SOL"), ("WLFIUSD1", "WLFI"),
    ("ETHBUSD", "ETH"), ("10000SATSUSDT", "SATS"),
])
def test_token_of_strips_quote_and_multiplier(sym, tok):
    assert ls.token_of(sym) == tok


@pytest.mark.parametrize("sym", ["BTCUSDT_230331", "BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT"])
def test_delivery_and_index_symbols_excluded(sym):
    assert ls.excluded_by_name(sym)


def test_plain_perp_not_excluded_by_name():
    assert not ls.excluded_by_name("SUIUSDT")


def _info(sym, onboard, ctype="PERPETUAL", utype="COIN"):
    return {"symbol": sym, "onboardDate": int(pd.Timestamp(onboard, tz="UTC").value // 10**6),
            "contractType": ctype, "underlyingType": utype}


def test_build_events_earliest_symbol_wins_and_tradfi_dropped():
    first_month = {"BTCUSDT": "2020-01", "SUIUSDT": "2023-05", "SUIUSDC": "2024-02",
                   "1000PEPEUSDT": "2023-05", "TSLAUSDT": "2026-02", "OLDUSDT": "2021-03"}
    info = [_info("SUIUSDT", "2023-05-03"), _info("SUIUSDC", "2024-02-10"),
            _info("1000PEPEUSDT", "2023-05-05"),
            _info("TSLAUSDT", "2026-02-01", ctype="TRADIFI_PERPETUAL", utype="EQUITY")]
    ev = ls.build_events(first_month, info, daily_first={})
    ev = ev.set_index("token")
    assert "TSLA" not in ev.index                         # tradfi perp
    assert "BTC" not in ev.index                          # BTC/ETH themselves
    assert ev.loc["SUI", "listing_date"] == pd.Timestamp("2023-05-03")
    assert ev.loc["SUI", "precision"] == "day"
    assert ev.loc["PEPE", "listing_date"] == pd.Timestamp("2023-05-05")
    # S3-only, no daily name -> month precision, first of month
    assert ev.loc["OLD", "precision"] == "month"
    assert ev.loc["OLD", "listing_date"] == pd.Timestamp("2021-03-01")


def test_build_events_left_censoring_uses_archive_start():
    first_month = {"BTCUSDT": "2020-01", "XRPUSDT": "2020-01", "NEWUSDT": "2020-02"}
    ev = ls.build_events(first_month, [], daily_first={}).set_index("token")
    assert ev.loc["XRP", "censored"]
    assert not ev.loc["NEW", "censored"]


def test_build_events_daily_first_name_gives_day_precision():
    ev = ls.build_events({"BTCUSDT": "2020-01", "ABCUSDT": "2024-07"}, [],
                         daily_first={"ABCUSDT": "2024-07-19"}).set_index("token")
    assert ev.loc["ABC", "listing_date"] == pd.Timestamp("2024-07-19")
    assert ev.loc["ABC", "precision"] == "day"


def test_renames_flagged_not_counted_as_listings():
    ev = ls.build_events({"BTCUSDT": "2020-01", "MATICUSDT": "2020-10", "POLUSDT": "2024-09"},
                         [], daily_first={}).set_index("token")
    assert ev.loc["POL", "rename"]
    assert not ev.loc["MATIC", "rename"]


@pytest.mark.parametrize("d,w", [
    ("2020-01-31", None), ("2020-02-01", "a"), ("2025-12-01", "a"), ("2025-12-02", "straddle"),
    ("2025-12-31", "straddle"), ("2026-01-01", "b"), ("2026-09-04", "b"), ("2026-09-05", None),
])
def test_assign_window_boundaries(d, w):
    assert ls.assign_window(pd.Timestamp(d)) == w


# --- K3: the loader cut -------------------------------------------------------------

def _bars(listing, days=45):
    t = pd.date_range(listing, periods=days, freq="D")
    return pd.DataFrame({"open_time": t, "close": np.arange(days, dtype=float)})


def test_event_rows_drops_day_31_and_later_for_every_frame_kind():
    listing = pd.Timestamp("2024-03-10")
    for kind in ("price", "funding", "btc_hedge"):
        out = ls.event_rows(_bars(listing), listing, kind=kind)
        assert out["open_time"].max() == listing + pd.Timedelta(days=30)
        assert (out["open_time"] < listing + pd.Timedelta(days=31)).all()
        assert len(out) == 31                              # days 0..30


def test_event_rows_keeps_day_30_bar_judged_by_open_time():
    listing = pd.Timestamp("2024-03-10")
    out = ls.event_rows(_bars(listing, 31), listing, kind="price")
    assert len(out) == 31


def test_event_rows_intraday_funding_last_slot_of_day_30_kept_day_31_dropped():
    listing = pd.Timestamp("2024-03-10")
    t = pd.date_range(listing, periods=8 * 3 * 33, freq="h")[::8]
    f = pd.DataFrame({"open_time": t, "rate": 0.0})
    out = ls.event_rows(f, listing, kind="funding")
    assert out["open_time"].max() == listing + pd.Timedelta(days=30, hours=16)


def test_mutant_without_filter_raises():
    listing = pd.Timestamp("2024-03-10")
    with pytest.raises(ls.LookaheadRowError):
        ls.assert_no_row_beyond(_bars(listing), listing)


def test_assert_rejects_rows_before_day0_too():
    listing = pd.Timestamp("2024-03-10")
    early = _bars(listing - pd.Timedelta(days=2), 5)
    with pytest.raises(ls.LookaheadRowError):
        ls.assert_no_row_beyond(early, listing)


def test_event_rows_rejects_unknown_kind():
    with pytest.raises(ValueError):
        ls.event_rows(_bars(pd.Timestamp("2024-01-01")), pd.Timestamp("2024-01-01"), kind="mark")


# --- K2: tradability ----------------------------------------------------------------

def test_kraken_open_share_counts_unmatched_as_not_tradable():
    ev = pd.DataFrame({"token": ["A", "B", "C", "D"],
                       "listing_date": pd.to_datetime(["2024-01-01"] * 4),
                       "precision": ["day", "day", "day", "month"]})
    kraken = {"A": pd.Timestamp("2024-01-01 18:00", tz="UTC"),   # day 0 -> ok
              "B": pd.Timestamp("2024-01-02 23:00", tz="UTC"),   # day 1 -> ok
              "C": pd.Timestamp("2024-01-03 00:00", tz="UTC")}   # day 2 -> late
    res = ls.kraken_open_share(ev, kraken)
    assert res["n_denominator"] == 3                      # month-precision excluded
    assert res["n_open_by_day1"] == 2
    assert res["share"] == pytest.approx(2 / 3)
    assert res["n_month_precision_excluded"] == 1


def test_kraken_token_normalisation():
    assert ls.kraken_token("PF_XBTUSD") == "BTC"
    assert ls.kraken_token("PF_1000BONKUSD") == "BONK"
    assert ls.kraken_token("PF_SUIUSD") == "SUI"
    assert ls.kraken_token("PI_XBTUSD") is None


# --- A0 harness ---------------------------------------------------------------------

def test_capped_short_return_never_below_minus_one():
    r = ls.short_return(np.array([-1.0, 0.0, 0.7, 5.0]))
    assert r.min() == -1.0
    assert r[1] == 0.0


def test_calibrated_drift_hits_target_net_mean():
    rng = np.random.default_rng(0)
    for target in (0.0, 0.10):
        m = ls.calibrate_drift(target, sigma=0.45, rng=rng)
        z = m + 0.45 * ls.t3(rng, 400_000)
        assert ls.short_return(z).mean() == pytest.approx(target, abs=0.003)


def test_cluster_rho_matches_se_inflation():
    rho = ls.cluster_rho(se_inflation=1.3, mean_cluster_size=11.0)
    assert 1 + (11 - 1) * rho == pytest.approx(1.69)
    assert ls.cluster_rho(1.0, 11.0) == 0.0


def test_cluster_lower_bound_positive_for_strong_plant_negative_for_null():
    rng = np.random.default_rng(1)
    month = np.repeat(np.arange(30), 10)
    lb_pos = ls.cluster_lower_bound(0.5 + 0.1 * rng.standard_normal(300), month, rng, n_boot=499)
    lb_null = ls.cluster_lower_bound(-0.5 + 0.1 * rng.standard_normal(300), month, rng, n_boot=499)
    assert lb_pos > 0 > lb_null


def test_power_harness_bites_plant_null_and_sign_mutant():
    kw = dict(n_events=300, n_months=60, placebo_per_month=10, sigma=0.45,
              se_inflation=1.3, n_seeds=60, n_boot=299, seed=3)
    assert ls.power(effect=0.40, **kw)["joint"] >= 0.95
    assert ls.power(effect=-0.40, **kw)["joint"] <= 0.02
    null = ls.power(effect=0.0, **kw)
    assert null["primary"] <= 0.15


def test_iid_mutant_over_rejects_clustered_null():
    kw = dict(n_events=300, n_months=30, placebo_per_month=10, sigma=0.45,
              se_inflation=2.0, n_seeds=150, n_boot=299, seed=5, effect=0.0)
    clustered = ls.power(**kw)["primary"]
    iid = ls.power(iid_bootstrap=True, **kw)["primary"]
    assert iid > clustered
