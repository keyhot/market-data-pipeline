import pandas as pd

from scripts.check_parity import compare_bars


def _csv_df():
    return pd.DataFrame(
        {"Open": [99.0, 100.5], "Close": [100.0, 101.0], "Volume": [1000, 2000]},
        index=pd.to_datetime(["2026-01-05", "2026-01-06"], utc=True),
    )


def _stored(close_2nd=101.0):
    return [
        {"timestamp": "2026-01-05T00:00:00+00:00", "close": 100.0, "volume": 1000},
        {"timestamp": "2026-01-06T00:00:00+00:00", "close": close_2nd, "volume": 2000},
    ]


def test_matching_data_reports_no_mismatches():
    assert compare_bars("AAPL", _csv_df(), _stored()) == []


def test_missing_row_is_reported():
    mismatches = compare_bars("AAPL", _csv_df(), _stored()[:1])
    assert len(mismatches) == 1
    assert "2026-01-06" in mismatches[0]
    assert "missing" in mismatches[0]


def test_close_drift_is_reported():
    mismatches = compare_bars("AAPL", _csv_df(), _stored(close_2nd=999.0))
    assert len(mismatches) == 1
    assert "close" in mismatches[0]


def test_tiny_float_noise_is_tolerated():
    assert compare_bars("AAPL", _csv_df(), _stored(close_2nd=101.0000001)) == []
