"""Q20/Q21: owner-approved values, pinned so a change is a decision, not drift."""


def test_the_dedupe_uid_cap_is_pinned_at_400():
    # Q20: 100 equals one API window and leaves no margin for an overlapping
    # poll after a stall; it also bounds the kraken_fold_state WAL.
    from ingestion.liquidation_recorder import KRAKEN_SEEN_UID_CAP

    assert KRAKEN_SEEN_UID_CAP == 400


def test_the_kraken_coverage_claim_is_capped_at_one_day():
    # Q21: bounds how much one call claims; minutes past a longer stall stay
    # honest gaps rather than coverage claimed for rows that were not fetched.
    from ingestion.liquidations import KRAKEN_COVERAGE_MAX_MINUTES

    assert KRAKEN_COVERAGE_MAX_MINUTES == 1440
