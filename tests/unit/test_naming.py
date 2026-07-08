from api.main import EventType, TimeRange
from storage.naming import raw_data_path, raw_event_path, sanitize_ticker


def test_sanitize_ticker():
    assert sanitize_ticker("^gspc") == "GSPC"
    assert sanitize_ticker("brk.b") == "BRK_B"


def test_raw_data_path_uses_enum_value_not_member_name():
    path = raw_data_path("AAPL", TimeRange.FIVE_DAYS, timestamp="2026-07-08T00-00-00Z")

    assert path.name == "AAPL_5d_2026-07-08T00-00-00Z.csv"


def test_raw_event_path_uses_enum_value_not_member_name():
    path = raw_event_path(
        "AAPL", EventType.DIVIDENDS, timestamp="2026-07-08T00-00-00Z"
    )

    assert path.name == "AAPL_dividends_2026-07-08T00-00-00Z.csv"
