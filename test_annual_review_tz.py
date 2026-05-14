"""
test_annual_review_tz.py — Tests for the timezone-naive vs aware
handling in compute_cumulative_cb1_plus_days.

Background: the IRAPM clock seam (clock.py) documents `Clock.now()` as
returning NAIVE datetimes — "naive local datetime, used for record
timestamps and JSON serialization." Both SystemClock (production) and
AdvancingClock (simulation harness) honor that contract.

The CB transition log is written using `now.isoformat()` in
action_layer._execute_cb_state_transition. Naive datetimes produce ISO
strings WITHOUT timezone suffix.

Later, annual_review.compute_cumulative_cb1_plus_days reads these
records back via `datetime.fromisoformat(ts_str)`. The result is naive
when the original write was naive.

The function then compares those parsed timestamps against:

    year_start = datetime(year, 1, 1, tzinfo=timezone.utc)
    year_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)

`ts < year_start` on a naive vs aware pair raises TypeError. In our
2005-2025 baseline run this caused 9 of 20 annual reviews to crash —
specifically the years where the PRIOR year had any CB activity (the
only CB transitions for the freeze evaluation to read).

Fix: normalize parsed naive timestamps to UTC inside
compute_cumulative_cb1_plus_days. The freeze evaluation cares about
calendar-year membership and inter-event spans, not sub-day precision;
treating missing tz as UTC is correct because the system's clock is
modeled-as-UTC throughout the harness (AdvancingClock.now_et attaches
ET only at the boundary).

Run with:  pytest test_annual_review_tz.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from annual_review import compute_cumulative_cb1_plus_days


# ============================================================================
# RED-BASELINE TESTS (these fail before the fix, pass after)
# ============================================================================

def test_naive_timestamps_do_not_raise():
    """The production failure mode: all CB transition timestamps are
    naive (matching the AdvancingClock / SystemClock contract). The
    function must process them without raising TypeError.
    """
    records = [
        {
            "timestamp": "2008-01-09T00:00:00",  # NAIVE — no tz suffix
            "from_state": "CB_INACTIVE",
            "to_state": "CB1",
        },
        {
            "timestamp": "2008-04-16T00:00:00",  # NAIVE
            "from_state": "CB2",
            "to_state": "CB_INACTIVE",
        },
    ]
    # Should not raise.
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    # The span is Jan 9 → Apr 16: 98 days in CB1/CB2.
    assert days == 98


def test_naive_timestamps_with_year_boundary_clamping():
    """A CB1 state that started in the prior year and continued into
    the test year should be counted as in-state from year_start.
    """
    records = [
        {
            "timestamp": "2007-12-15T00:00:00",  # NAIVE, prior year
            "from_state": "CB_INACTIVE",
            "to_state": "CB1",
        },
        {
            "timestamp": "2008-03-10T00:00:00",  # NAIVE, current year
            "from_state": "CB1",
            "to_state": "CB_INACTIVE",
        },
    ]
    # In 2008, the state at year_start (2008-01-01) is CB1 (latched
    # in 2007), running until 2008-03-10. That's 69 days.
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    assert days == 69


def test_naive_timestamps_with_in_year_only_span():
    """A CB1 state that started AND ended within the test year.
    Validates the simplest production scenario.
    """
    records = [
        {
            "timestamp": "2008-01-09T00:00:00",  # NAIVE
            "from_state": "CB_INACTIVE",
            "to_state": "CB1",
        },
        {
            "timestamp": "2008-02-20T00:00:00",  # NAIVE
            "from_state": "CB1",
            "to_state": "CB_INACTIVE",
        },
    ]
    # Jan 9 → Feb 20 inside 2008 = 42 days.
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    assert days == 42


# ============================================================================
# REGRESSION TESTS (verify aware-timestamp behavior didn't change)
# ============================================================================

def test_aware_timestamps_still_work():
    """If the log somehow contains aware timestamps (alternative
    clock implementation, manual log edit, etc.), the function must
    continue to handle them correctly.
    """
    records = [
        {
            "timestamp": "2008-01-09T00:00:00+00:00",  # AWARE UTC
            "from_state": "CB_INACTIVE",
            "to_state": "CB1",
        },
        {
            "timestamp": "2008-04-16T00:00:00+00:00",  # AWARE UTC
            "from_state": "CB2",
            "to_state": "CB_INACTIVE",
        },
    ]
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    assert days == 98


def test_mixed_naive_and_aware_timestamps():
    """Defensive: a log that contains a mix of naive (legacy) and
    aware (post-fix) records should still produce correct results.
    Both are normalized to UTC for comparison.
    """
    records = [
        {
            "timestamp": "2008-01-09T00:00:00",  # NAIVE (legacy)
            "from_state": "CB_INACTIVE",
            "to_state": "CB1",
        },
        {
            "timestamp": "2008-04-16T00:00:00+00:00",  # AWARE
            "from_state": "CB2",
            "to_state": "CB_INACTIVE",
        },
    ]
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    assert days == 98


# ============================================================================
# UNCHANGED-BEHAVIOR TESTS (these must already pass)
# ============================================================================

def test_empty_records_returns_zero():
    """No CB activity → 0 days. Should already pass; included to ensure
    the empty-path early-return isn't broken by the fix.
    """
    assert compute_cumulative_cb1_plus_days([], year=2008) == 0


def test_records_only_before_test_year_returns_zero_if_clean():
    """Records exist but all happened BEFORE the test year, and the
    most recent ended in CB_INACTIVE. Test year sees no CB activity.
    """
    records = [
        {
            "timestamp": "2005-06-01T00:00:00",
            "from_state": "CB_INACTIVE",
            "to_state": "CB1",
        },
        {
            "timestamp": "2005-08-15T00:00:00",
            "from_state": "CB1",
            "to_state": "CB_INACTIVE",
        },
    ]
    assert compute_cumulative_cb1_plus_days(records, year=2008) == 0


def test_records_only_before_test_year_with_unclosed_state():
    """Records exist; the most recent BEFORE year-start latched into
    CB1 and never exited. The entire test year is CB1.
    """
    records = [
        {
            "timestamp": "2007-10-15T00:00:00",
            "from_state": "CB_INACTIVE",
            "to_state": "CB1",
        },
    ]
    # 2008 is a leap year — 366 days.
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    assert days == 366


def test_malformed_record_is_skipped():
    """A record missing required fields should be skipped, not crash."""
    records = [
        {"timestamp": "2008-01-09T00:00:00", "from_state": "CB_INACTIVE", "to_state": "CB1"},
        {"timestamp": "2008-02-15T00:00:00"},  # missing from_state/to_state
        {"timestamp": "2008-02-20T00:00:00", "from_state": "CB1", "to_state": "CB_INACTIVE"},
    ]
    # The middle record is skipped; the others define a Jan 9 → Feb 20
    # span = 42 days.
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    assert days == 42


def test_malformed_timestamp_string_is_skipped():
    """A record with an unparseable timestamp should be skipped."""
    records = [
        {"timestamp": "2008-01-09T00:00:00", "from_state": "CB_INACTIVE", "to_state": "CB1"},
        {"timestamp": "not-a-date", "from_state": "CB1", "to_state": "CB2"},
        {"timestamp": "2008-02-20T00:00:00", "from_state": "CB1", "to_state": "CB_INACTIVE"},
    ]
    days = compute_cumulative_cb1_plus_days(records, year=2008)
    assert days == 42
