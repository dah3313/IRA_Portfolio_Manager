"""
test_cycle_calendar.py — Tests for the cycle.py calendar helpers.

Background: two bugs of the same class.

  1. WITHDRAWAL: _is_scheduled_withdrawal_day used `10 <= today.day <= 15`,
     which excludes day 9. If the 1st of a month falls on a Tuesday,
     the Wednesdays are 2, 9, 16, 23, 30 — none in [10,15] — and the
     withdrawal step silently skips that month entirely. Across 2005-2025
     this causes ~35 missed monthly withdrawals (we observe 205 actual vs
     ~240 expected).

  2. ANNUAL REVIEW: _is_annual_review_day used `today.month == M and
     today.day == D`, which only fires when the cycle date EXACTLY equals
     the configured MM-DD. Cycles run on Wednesdays. MM-DD falls on a
     Wednesday only ~1/7 of the time, so over 20 years we observe 2 annual
     reviews vs ~20 expected.

Fix: both predicates need to recognize "the first Wednesday on-or-after
the target." Withdrawal target is "the 15th minus T+1 settlement," but
the existing simple-day-range design treats it as "the Wed ≤ 15 of each
month." Annual review target is "the configured MM-DD."

For withdrawal, the corrected bound is `9 <= today.day <= 15` — a 7-day
window that guarantees exactly one Wednesday falls within it regardless
of where the 1st sits in the week. See test_withdrawal_fires_in_every_month
below for the exhaustive verification.

For annual review, "first Wed on-or-after MM-DD" expressed as:
  - today is a Wednesday
  - this year's MM-DD (call it T) satisfies T <= today <= T + 6 days
  - the previous Wednesday (today - 7) was strictly before T

The last clause ensures only ONE Wednesday per year fires (the first
qualifying one), not all Wednesdays in a 7-day window. Without it, two
Weds in a row could fire in years where MM-DD itself is a Wednesday.

Run with:  pytest test_cycle_calendar.py -v
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cycle import _is_scheduled_withdrawal_day, _is_annual_review_day


# ============================================================================
# WITHDRAWAL — first Wed-on-or-before-15th, exactly once per month
# ============================================================================

def _first_of_month_to_withdrawal_wed(year: int, month: int) -> date:
    """Helper: given a year+month, return the date of the Wednesday that
    SHOULD fire withdrawal. Defined as: the LATEST Wednesday whose
    day-of-month is <= 15.
    """
    # Find all Wednesdays in days 1..21 of the month, take the last one ≤ 15.
    weds = []
    for d in range(1, 22):
        try:
            candidate = date(year, month, d)
        except ValueError:
            break
        if candidate.weekday() == 2 and candidate.day <= 15:
            weds.append(candidate)
    assert weds, f"no Wed ≤ 15 in {year}-{month}"
    return weds[-1]


def test_withdrawal_fires_in_every_month_of_2016(tmp_path):
    """The Tuesday-1st bug specifically fails in months where the 1st
    is Tuesday. In 2016: February (1st was Mon → Wed are 3,10,17,24),
    March (1st was Tue → Wed are 2,9,16,23,30 — would skip with bug),
    November (1st was Tue → same bug). Run all 12 months; exactly one
    Wed must fire per month.
    """
    for month in range(1, 13):
        expected = _first_of_month_to_withdrawal_wed(2016, month)
        # Walk every day of the month.
        fired_dates = []
        d = date(2016, month, 1)
        while d.month == month:
            if _is_scheduled_withdrawal_day(d):
                fired_dates.append(d)
            d += timedelta(days=1)
        assert len(fired_dates) == 1, (
            f"2016-{month:02d}: expected exactly 1 withdrawal day, got "
            f"{len(fired_dates)}: {fired_dates}"
        )
        assert fired_dates[0] == expected, (
            f"2016-{month:02d}: expected {expected}, got {fired_dates[0]}"
        )


def test_withdrawal_fires_in_every_month_for_5_years(tmp_path):
    """Robustness check: walk 2014-2018 inclusive and verify exactly
    one withdrawal day fires in every month.
    """
    for year in range(2014, 2019):
        for month in range(1, 13):
            fired_dates = []
            d = date(year, month, 1)
            while d.month == month:
                if _is_scheduled_withdrawal_day(d):
                    fired_dates.append(d)
                d += timedelta(days=1)
            assert len(fired_dates) == 1, (
                f"{year}-{month:02d}: expected exactly 1 withdrawal day, "
                f"got {len(fired_dates)}: {fired_dates}"
            )


def test_withdrawal_never_fires_on_non_wednesday(tmp_path):
    """Sanity: the rule is Wed-specific. Walk every non-Wed day of
    January 2016; none should fire.
    """
    d = date(2016, 1, 1)
    while d.year == 2016 and d.month == 1:
        if d.weekday() != 2:
            assert not _is_scheduled_withdrawal_day(d), (
                f"{d} ({d.strftime('%A')}) should not fire — non-Wed"
            )
        d += timedelta(days=1)


def test_withdrawal_does_not_fire_outside_window(tmp_path):
    """A Wed past day 15 should not fire (those are late-month Weds
    targeting the following month's settlement). A Wed before day 9
    is too early (those are previous-month settlement). Spot-check:
    - 2016-01-20 (Wed, day 20): should NOT fire.
    - 2020-01-08 (Wed, day 8): should NOT fire.
    - 2016-01-13 (Wed, day 13): SHOULD fire.
    """
    assert not _is_scheduled_withdrawal_day(date(2016, 1, 20))
    assert not _is_scheduled_withdrawal_day(date(2020, 1, 8))
    assert _is_scheduled_withdrawal_day(date(2016, 1, 13))


# ============================================================================
# ANNUAL REVIEW — fires once per year on first Wed on-or-after MM-DD
# ============================================================================

def test_annual_review_fires_when_mmdd_is_wednesday(tmp_path):
    """01-15-2020 fell on a Wednesday. The function should return True
    on exactly 2020-01-15 and False on the previous Wed (2020-01-08)
    and following Wed (2020-01-22).
    """
    assert _is_annual_review_day(date(2020, 1, 15), "01-15")
    assert not _is_annual_review_day(date(2020, 1, 8), "01-15")
    assert not _is_annual_review_day(date(2020, 1, 22), "01-15")


def test_annual_review_fires_on_first_wed_after_mmdd(tmp_path):
    """01-15-2021 fell on a Friday. The next Wed is 2021-01-20. The
    function should return True on exactly that Wed and False on the
    Wed before (2021-01-13) and the Wed after (2021-01-27).
    """
    assert date(2021, 1, 15).weekday() == 4  # sanity: Fri
    assert not _is_annual_review_day(date(2021, 1, 13), "01-15")
    assert _is_annual_review_day(date(2021, 1, 20), "01-15")
    assert not _is_annual_review_day(date(2021, 1, 27), "01-15")


def test_annual_review_fires_on_first_wed_after_sunday_mmdd(tmp_path):
    """01-15-2023 fell on a Sunday. Next Wed is 2023-01-18.
    """
    assert date(2023, 1, 15).weekday() == 6  # sanity: Sun
    assert _is_annual_review_day(date(2023, 1, 18), "01-15")
    assert not _is_annual_review_day(date(2023, 1, 11), "01-15")
    assert not _is_annual_review_day(date(2023, 1, 25), "01-15")


def test_annual_review_fires_on_first_wed_after_saturday_mmdd(tmp_path):
    """01-15-2022 fell on a Saturday. Next Wed is 2022-01-19.
    """
    assert date(2022, 1, 15).weekday() == 5  # sanity: Sat
    assert _is_annual_review_day(date(2022, 1, 19), "01-15")
    assert not _is_annual_review_day(date(2022, 1, 12), "01-15")
    assert not _is_annual_review_day(date(2022, 1, 26), "01-15")


def test_annual_review_fires_exactly_once_per_year(tmp_path):
    """Walk every Wed of 2014-2024 and count fires. Should be exactly
    one per year.
    """
    for year in range(2014, 2025):
        fired = []
        d = date(year, 1, 1)
        while d.year == year:
            if d.weekday() == 2 and _is_annual_review_day(d, "01-15"):
                fired.append(d)
            d += timedelta(days=1)
        assert len(fired) == 1, (
            f"{year}: expected exactly 1 annual review day, got "
            f"{len(fired)}: {fired}"
        )


def test_annual_review_does_not_fire_on_non_wednesday(tmp_path):
    """Sanity: even on 01-15 itself, the function should only fire
    when that date is a Wednesday (cycles only run on Wed).
    """
    # 01-15-2021 was a Friday.
    assert not _is_annual_review_day(date(2021, 1, 15), "01-15")
    # 01-15-2022 was a Saturday.
    assert not _is_annual_review_day(date(2022, 1, 15), "01-15")
