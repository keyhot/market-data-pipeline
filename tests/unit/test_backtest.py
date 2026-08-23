import numpy as np
import pandas as pd
import pytest

from model.backtest import BacktestConfig, run_backtest


def _bars(n=2600, seed=3, drift=0.0):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-05-01", periods=n, freq="1min", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.001, n)))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.001,
            "Low": close * 0.999,
            "Close": close,
            "Volume": rng.integers(100, 10_000, n).astype(float),
        },
        index=index,
    )


_FAST = BacktestConfig(train_rows=400, test_rows=100)


def test_backtest_is_deterministic():
    r1 = run_backtest(_bars(), _FAST)
    r2 = run_backtest(_bars(), _FAST)

    assert r1["strategy_total_return"] == r2["strategy_total_return"]
    assert r1["folds"] == r2["folds"]


def test_fees_reduce_returns():
    cheap = run_backtest(_bars(), BacktestConfig(
        train_rows=400, test_rows=100, fee_per_side=0.0, slippage=0.0))
    costly = run_backtest(_bars(), BacktestConfig(
        train_rows=400, test_rows=100, fee_per_side=0.005, slippage=0.001))

    if costly["total_positions"] > 0:
        assert costly["strategy_total_return"] < cheap["strategy_total_return"]


def test_insufficient_rows_rejected():
    with pytest.raises(ValueError, match="not enough rows"):
        run_backtest(_bars(n=300), _FAST)


def test_fold_accounting():
    results = run_backtest(_bars(), _FAST)

    assert results["n_folds"] >= 2
    for fold in results["folds"]:
        assert fold["rows"] == 100
        assert 0 <= fold["positions"] <= fold["rows"]


def test_leak_detector_oracle_feature_is_visible():
    """Inject the future answer into a feature-visible column (High encodes
    whether the next 15 bars go up). The harness MUST detect this as a huge
    hit-rate jump — proving that if the honest pipeline leaked, we'd see it,
    and that the honest run doesn't look like the leaked one."""
    bars = _bars(n=2600, seed=5)
    honest = run_backtest(bars, _FAST)

    close = bars["Close"]
    future_up = (close.shift(-15) > close).fillna(False).astype(float)
    leaky_bars = bars.copy()
    # hl_range = (high - low) / close becomes an oracle: wide when up-next.
    leaky_bars["High"] = close * (1.0 + 0.001 + 0.02 * future_up)

    leaky = run_backtest(leaky_bars, _FAST)

    assert leaky["overall_hit_rate"] is not None
    assert honest["overall_hit_rate"] is not None
    assert leaky["overall_hit_rate"] > 0.9
    assert leaky["overall_hit_rate"] > honest["overall_hit_rate"] + 0.2


def test_the_harness_charges_one_fee_per_position_not_per_bar():
    """KI-040 at the harness level. With a threshold of 0.0 every bar of every
    test window is in-market, so the whole walk-forward is ONE position, held
    throughout and billed one round trip. The old accounting billed a round
    trip per in-market bar and would have reported ~2000."""
    results = run_backtest(_bars(), BacktestConfig(
        train_rows=400, test_rows=100, entry_threshold=0.0))

    assert results["total_positions"] == 1
    assert results["fee_charges"] == results["total_positions"]


def test_no_two_positions_in_the_equity_path_are_open_at_once():
    """KI-001 at the harness level: `strategy_total_return` is a product, so
    it is only meaningful over one unit of capital. Computed from the
    positions actually taken — reverting the selection makes this fail."""
    results = run_backtest(_bars(), BacktestConfig(
        train_rows=400, test_rows=100, entry_threshold=0.5))

    assert results["max_concurrent_positions"] == 1
    assert results["accounting"] == "position-level"
