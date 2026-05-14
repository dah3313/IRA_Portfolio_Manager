"""
test_price_data_as_of.py — Tests for as_of-aware bar loading.

Background: load_weekly_adj_close historically returned the last `count`
bars in the file regardless of any cycle date. In production this was
fine because `as_of` was always "now" and the file was always current.
In the IRAPM simulator harness (irapm_driver.py), `as_of` walks through
historical dates while the price file stays fixed, which caused the
Synthetic Growth Lookback to compute the same value every cycle —
specifically 0.154590... across 1044 cycles spanning 2005-2025 in the
baseline_20yr_phase3_2016 run, regardless of GFC/COVID/2022 drawdowns.

Fix: load_weekly_adj_close now accepts an optional `as_of` parameter.
When provided, bars with bar_date > as_of are filtered out BEFORE the
count-trim step, so the loader returns the last N bars on-or-before
the cycle's reference date.

These tests cover the three behavioral cases:
  1. as_of=None: default behavior unchanged (last N bars in file).
  2. as_of=<date inside data range>: last N bars on-or-before that date.
  3. as_of=<date before all data>: empty list (lookback signal will
     return UNAVAILABLE per its existing "no aligned dates" path).

Run with:  pytest test_price_data_as_of.py -v
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from price_data import load_weekly_adj_close


# ============================================================================
# HELPERS
# ============================================================================

def _write_sample_tsv(tmp_path: Path, symbol: str = "TEST") -> Path:
    """Write a 10-bar TSV file with dates 2025-01-01 through 2025-01-10
    and prices 100.00 through 109.00 (one per day, ascending).
    """
    rows = [
        "Date\tAdj Close",
    ]
    for i in range(10):
        bar_date = date(2025, 1, 1 + i)
        price = Decimal("100.00") + Decimal(i)
        rows.append(f"{bar_date.isoformat()}\t{price}")
    content = "\n".join(rows) + "\n"
    file_path = tmp_path / f"{symbol}.tsv"
    file_path.write_text(content, encoding="utf-8")
    return file_path


# ============================================================================
# TESTS
# ============================================================================

def test_no_as_of_returns_latest_n_bars(tmp_path):
    """Without as_of, behavior is unchanged: return the last `count`
    bars in the file by date.
    """
    _write_sample_tsv(tmp_path)
    bars = load_weekly_adj_close("TEST", tmp_path, count=5)
    assert len(bars) == 5
    assert bars[0].bar_date == date(2025, 1, 6)
    assert bars[-1].bar_date == date(2025, 1, 10)
    assert bars[-1].adj_close == Decimal("109.00")


def test_as_of_filters_to_on_or_before_date(tmp_path):
    """With as_of inside the data range, return the last `count`
    bars whose bar_date is <= as_of.
    """
    _write_sample_tsv(tmp_path)
    bars = load_weekly_adj_close(
        "TEST", tmp_path, count=5, as_of=date(2025, 1, 7),
    )
    assert len(bars) == 5
    assert bars[0].bar_date == date(2025, 1, 3)
    assert bars[-1].bar_date == date(2025, 1, 7)
    assert bars[-1].adj_close == Decimal("106.00")


def test_as_of_before_all_data_returns_empty(tmp_path):
    """With as_of before any bar in the file, return an empty list.
    The lookback signal will then return UNAVAILABLE via its existing
    'no aligned dates' path, which is the correct behavior for cycles
    in a period predating the data.
    """
    _write_sample_tsv(tmp_path)
    bars = load_weekly_adj_close(
        "TEST", tmp_path, count=5, as_of=date(2024, 12, 1),
    )
    assert bars == []


def test_as_of_after_all_data_returns_full_file(tmp_path):
    """With as_of after the latest bar (e.g., a production cycle where
    'today' is later than the most recent Yahoo bar), behavior matches
    no-as_of: return the last `count` bars in the file. This is the
    common production case and must continue to work.
    """
    _write_sample_tsv(tmp_path)
    bars = load_weekly_adj_close(
        "TEST", tmp_path, count=5, as_of=date(2030, 1, 1),
    )
    assert len(bars) == 5
    assert bars[0].bar_date == date(2025, 1, 6)
    assert bars[-1].bar_date == date(2025, 1, 10)


def test_as_of_equal_to_a_bar_date_includes_that_bar(tmp_path):
    """as_of comparison is <=, not <: if as_of falls exactly on a bar
    date, that bar is included. (Important: the cycle's decision_clock
    will often fall on a Wednesday that's a market data bar.)
    """
    _write_sample_tsv(tmp_path)
    bars = load_weekly_adj_close(
        "TEST", tmp_path, count=3, as_of=date(2025, 1, 5),
    )
    assert len(bars) == 3
    assert bars[-1].bar_date == date(2025, 1, 5)


def test_as_of_with_count_larger_than_available_returns_what_exists(tmp_path):
    """If as_of constrains the result to fewer bars than `count`,
    return whatever's available. The lookback signal's coverage gate
    will decide if that's enough.
    """
    _write_sample_tsv(tmp_path)
    bars = load_weekly_adj_close(
        "TEST", tmp_path, count=10, as_of=date(2025, 1, 3),
    )
    # Only 3 bars are on-or-before 2025-01-03.
    assert len(bars) == 3
    assert [b.bar_date for b in bars] == [
        date(2025, 1, 1),
        date(2025, 1, 2),
        date(2025, 1, 3),
    ]


def test_as_of_none_explicit_matches_default(tmp_path):
    """Passing as_of=None explicitly should be identical to omitting
    it. This guards against accidental != None vs is None mismatches.
    """
    _write_sample_tsv(tmp_path)
    bars_omitted = load_weekly_adj_close("TEST", tmp_path, count=5)
    bars_explicit_none = load_weekly_adj_close(
        "TEST", tmp_path, count=5, as_of=None,
    )
    assert len(bars_omitted) == len(bars_explicit_none) == 5
    assert [b.bar_date for b in bars_omitted] == [
        b.bar_date for b in bars_explicit_none
    ]
