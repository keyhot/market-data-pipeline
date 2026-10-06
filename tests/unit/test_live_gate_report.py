"""TDD for scripts/live_gate_report.py's cache option (2026-10-06).

The on-air model is trained on native 15m bars, horizon=1 bar
(model/train.py / model/predict.py's SERVED_BY). live_gate_report.py already
reproduces that exact config honestly (same features.build_features, same
model._PARAMS/_NUM_ROUNDS, horizon=1, one-row purge, expanding per-year
training, 2026 excluded per the sealed-holdout rule E6) — it just never
cached its per-row OOS predictions. These tests cover the new cache path
(`collect_cached`) and the extracted sealed-holdout guard
(`exclude_sealed_holdout`), using a fake LightGBM so no real training happens.
"""

import numpy as np
import pandas as pd
import pytest

from scripts import live_gate_report as lgr


def _synthetic_bars(start="2019-06-01", end="2026-03-31") -> pd.DataFrame:
    """Daily OHLCV bars, deterministic (seeded), spanning the sealed-holdout
    boundary so exclude_sealed_holdout has 2026 rows to drop."""
    idx = pd.date_range(start, end, freq="1D", tz="UTC")
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1, len(idx)))
    return pd.DataFrame({
        "timestamp": idx,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": rng.uniform(100, 200, len(idx)),
    })


class _FakeDataset:
    """Stands in for lgb.Dataset: just remembers what it was given."""

    def __init__(self, data, label=None):
        self.data = data
        self.label = label


class _FakeBooster:
    def __init__(self, n_features):
        self.n_features = n_features

    def predict(self, X):
        return np.full(len(X), 0.5)


@pytest.fixture
def fake_lgbm(monkeypatch):
    """Replaces lgb.Dataset/lgb.train with deterministic fakes and records
    every training call's (data, label) for assertions. No real LightGBM
    fit ever runs."""
    calls = []

    def fake_train(params, dataset, num_boost_round):
        calls.append(dataset)
        return _FakeBooster(dataset.data.shape[1])

    monkeypatch.setattr(lgr.lgb, "Dataset", _FakeDataset)
    monkeypatch.setattr(lgr.lgb, "train", fake_train)
    return calls


def test_collect_purges_exactly_horizon_rows_before_each_test_fold(fake_lgbm):
    bars = _synthetic_bars(end="2025-12-31")
    oos = lgr.collect(bars, test_years=(2023, 2024))

    feats = lgr.build_features(bars, horizon_bars=lgr.HORIZON)
    X = feats.X
    years = X.index.year

    for year in (2023, 2024):
        test_idx = np.flatnonzero(years == year)
        train_end = test_idx[0] - lgr.HORIZON
        dataset = fake_lgbm[(2023, 2024).index(year)]
        # The fake recorded the exact training frame collect() built: its
        # last row must be the row immediately before the purge gap, and the
        # purged row (test_idx[0] - 1) must be absent from training.
        assert dataset.data.index[-1] == X.index[train_end - 1]
        assert X.index[test_idx[0] - 1] not in dataset.data.index


def test_collect_labels_fold_by_calendar_year(fake_lgbm):
    bars = _synthetic_bars(end="2025-12-31")
    oos = lgr.collect(bars, test_years=(2023, 2024))

    assert set(oos["fold"].unique()) == {2023, 2024}
    assert (oos.loc[oos.fold == 2023, "ts"].dt.year == 2023).all()
    assert (oos.loc[oos.fold == 2024, "ts"].dt.year == 2024).all()


def test_collect_output_round_trips_through_csv_for_calibration_report(
    fake_lgbm, tmp_path
):
    bars = _synthetic_bars(end="2025-12-31")
    oos = lgr.collect(bars, test_years=(2023, 2024))

    cache = tmp_path / "oos_BTCUSDT_15m_h1.csv"
    oos.to_csv(cache, index=False)
    reloaded = pd.read_csv(cache, parse_dates=["ts"])

    # Exactly the columns scripts/calibration_report.py's expanding_calibrate
    # and report() read: fold, y, p, ts.
    for col in ("ts", "fold", "y", "p"):
        assert col in reloaded.columns
    pd.testing.assert_series_equal(
        reloaded["p"], oos["p"].reset_index(drop=True), check_names=False
    )


def test_exclude_sealed_holdout_drops_everything_from_2026_on():
    bars = _synthetic_bars(start="2025-01-01", end="2026-03-31")
    kept = lgr.exclude_sealed_holdout(bars)
    stamps = pd.to_datetime(kept["timestamp"], utc=True)
    assert stamps.max() < pd.Timestamp("2026-01-01", tz="UTC")
    assert len(kept) < len(bars)


def test_collect_cached_writes_then_reuses_without_retraining(
    fake_lgbm, tmp_path
):
    bars = _synthetic_bars(end="2025-12-31")
    cache = tmp_path / "oos_BTCUSDT_15m_h1.csv"

    first = lgr.collect_cached(bars, cache, test_years=(2023, 2024))
    assert cache.exists()
    n_calls_after_first = len(fake_lgbm)
    assert n_calls_after_first > 0

    # Second call must reuse the cache, not retrain — the fake records a
    # fresh list reference, so if collect_cached called collect() again the
    # call count would grow.
    second = lgr.collect_cached(bars, cache, test_years=(2023, 2024))
    assert len(fake_lgbm) == n_calls_after_first
    pd.testing.assert_frame_equal(
        first.reset_index(drop=True), second.reset_index(drop=True),
        check_dtype=False,
    )


def test_collect_cached_refresh_retrains_even_when_cache_exists(
    fake_lgbm, tmp_path
):
    bars = _synthetic_bars(end="2025-12-31")
    cache = tmp_path / "oos_BTCUSDT_15m_h1.csv"
    lgr.collect_cached(bars, cache, test_years=(2023, 2024))
    n_calls = len(fake_lgbm)

    lgr.collect_cached(bars, cache, test_years=(2023, 2024), refresh=True)
    assert len(fake_lgbm) > n_calls
