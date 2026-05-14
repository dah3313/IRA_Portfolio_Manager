"""
test_harness_bail_logic.py — Unit tests for CycleFailureTracker.

The harness's failure-handling closure has been refactored into the
CycleFailureTracker class (in irapm_driver.py) precisely so the
behavior can be tested in isolation, without standing up a full
SyntheticBroker / IPMS engine / scenario YAML.

These tests cover the consecutive-failure counter and bail logic:
  1. Three consecutive identical exceptions of the same cycle_type
     trigger HarnessFailureError on the third call.
  2. A clean weekly success between exceptions resets the weekly
     counter, so the bail does NOT fire on what would otherwise be
     the third strike.
  3. Daily-token successes do NOT reset the weekly counter. (This is
     the bug an earlier iteration of the code had: the counter was
     global, daily-token resets were swallowing weekly retries.)
  4. An in-cycle halt (cycle.py returned with execution_halted=True)
     counts toward the bail with signature derived from halt_reason.
  5. A signature change resets the counter to 1 (not 0), so
     non-identical consecutive failures don't accumulate.

Run with:  pytest test_harness_bail_logic.py -v

These tests use real filesystem I/O on a pytest tmp_path because
CycleFailureTracker writes synthetic halted records to cycle.jsonl
during record_exception(). Each test gets a fresh tmp_path so they
cannot interfere with each other.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from irapm_driver import (
    CycleFailureTracker,
    HarnessFailureError,
    _exception_signature,
)
from persistence import Paths


# ============================================================================
# HELPERS
# ============================================================================

def _make_paths(tmp_path: Path) -> Paths:
    """Construct a real Paths instance rooted at the pytest tmp dir.
    Calls ensure_dirs() so the logs/ subdir exists for cycle.jsonl
    appends.
    """
    paths = Paths(state_dir=tmp_path / "state")
    paths.ensure_dirs()
    return paths


def _make_exc(message: str = "validation failure") -> Exception:
    """Construct a simple exception with a controlled __str__.

    _exception_signature uses `type(exc).__name__ + ': ' + str(exc).splitlines()[0]`,
    so the signature for any ValueError with first-line `message` is
    `'ValueError: <message>'`. This is sufficient for the bail tests
    without needing to import pydantic.
    """
    return ValueError(message)


def _write_in_cycle_halt_record(paths: Paths, halt_reason: str) -> None:
    """Append a synthetic cycle.jsonl record mimicking an outcome-2
    halt (cycle.py reached append_cycle_log with execution_halted=True).

    Schema matches what _append_halted_cycle_record writes and what
    cycle.py's append_cycle_log writes. The CycleFailureTracker reads
    `execution_halted` and `halt_reason` from the appended record.
    """
    record = {
        "cycle_id": "test-uuid-in-cycle-halt",
        "cycle_type": "weekly",
        "decision_clock": "2020-01-01T00:00:00",
        "phase": "PHASE_1",
        "cb_state": "CB_INACTIVE",
        "income_state": "ACTIVE",
        "operational_pause": False,
        "withdrawal_capacity_exhausted": False,
        "lookback_status": "AVAILABLE",
        "lookback_value": "0.10",
        "plan_entry_count": 1,
        "is_restart": False,
        "is_scheduled_withdrawal_day": False,
        "is_annual_review_day": False,
        "execution_halted": True,
        "halt_reason": halt_reason,
    }
    log_path = paths.cycle_log()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _write_clean_record(paths: Paths) -> None:
    """Append a synthetic cycle.jsonl record mimicking an outcome-1
    clean success (cycle.py reached append_cycle_log with
    execution_halted=False).
    """
    record = {
        "cycle_id": "test-uuid-clean",
        "cycle_type": "weekly",
        "decision_clock": "2020-01-08T00:00:00",
        "phase": "PHASE_1",
        "cb_state": "CB_INACTIVE",
        "income_state": "ACTIVE",
        "operational_pause": False,
        "withdrawal_capacity_exhausted": False,
        "lookback_status": "AVAILABLE",
        "lookback_value": "0.10",
        "plan_entry_count": 0,
        "is_restart": False,
        "is_scheduled_withdrawal_day": False,
        "is_annual_review_day": False,
        "execution_halted": False,
        "halt_reason": None,
    }
    log_path = paths.cycle_log()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ============================================================================
# TESTS — the five core scenarios
# ============================================================================

def test_three_consecutive_identical_exceptions_bails(tmp_path):
    """Three weekly cycles in a row raise the same exception.
    The third record_exception() call should raise HarnessFailureError.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)
    exc = _make_exc("trigger_year out of range")

    # First two should record and return normally.
    tracker.record_exception(date(2016, 8, 3), "weekly", exc)
    assert tracker.counter("weekly") == 1

    tracker.record_exception(date(2016, 8, 10), "weekly", exc)
    assert tracker.counter("weekly") == 2

    # Third should raise.
    with pytest.raises(HarnessFailureError) as excinfo:
        tracker.record_exception(date(2016, 8, 17), "weekly", exc)
    msg = str(excinfo.value)
    # Verify the error message has the diagnostic detail we expect.
    assert "Bailing after 3" in msg
    assert "weekly" in msg
    assert "ValueError" in msg
    assert "trigger_year out of range" in msg


def test_clean_weekly_success_resets_counter(tmp_path):
    """Two weekly exceptions, then a clean weekly success, then two
    more weekly exceptions. The bail should NOT fire — the counter
    resets after the clean success, so the post-success exceptions
    start counting from zero.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)
    exc = _make_exc("transient failure")

    tracker.record_exception(date(2010, 1, 6), "weekly", exc)
    tracker.record_exception(date(2010, 1, 13), "weekly", exc)
    assert tracker.counter("weekly") == 2

    # Clean weekly success: cycle.py appends a clean record, then we
    # tell the tracker the cycle returned normally.
    _write_clean_record(paths)
    tracker.record_normal_return(date(2010, 1, 20), "weekly")
    assert tracker.counter("weekly") == 0
    assert tracker.last_signature("weekly") is None

    # Two more exceptions. Should not bail because counter is fresh.
    tracker.record_exception(date(2010, 1, 27), "weekly", exc)
    tracker.record_exception(date(2010, 2, 3), "weekly", exc)
    assert tracker.counter("weekly") == 2
    # The 3rd would bail, but we stop here — the assertion is that
    # the 4th and 5th exceptions after a clean reset count as 1 and 2,
    # not 3 and 4.


def test_daily_token_successes_do_not_reset_weekly_counter(tmp_path):
    """The bug an earlier iteration had: a global counter would let
    daily-token successes reset the weekly retry counter, preventing
    the bail from firing. With per-cycle-type counters, two weekly
    exceptions plus six daily-token successes plus one more weekly
    exception should bail.

    This is the production scenario: between each weekly Wednesday
    cycle, six daily-token cycles run successfully.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)
    exc = _make_exc("trigger_year out of range")

    # Week 1 Wednesday: weekly fails.
    tracker.record_exception(date(2016, 8, 3), "weekly", exc)
    assert tracker.counter("weekly") == 1

    # Six days of daily-token successes (Thu-Tue).
    for day in [4, 5, 6, 7, 8, 9]:  # 2016-08-04 through 2016-08-09
        tracker.record_normal_return(date(2016, 8, day), "daily-token")
    # Daily-token counter exists but is reset; weekly counter UNTOUCHED.
    assert tracker.counter("daily-token") == 0
    assert tracker.counter("weekly") == 1

    # Week 2 Wednesday: weekly fails again.
    tracker.record_exception(date(2016, 8, 10), "weekly", exc)
    assert tracker.counter("weekly") == 2

    # Another six daily-token successes.
    for day in [11, 12, 13, 14, 15, 16]:
        tracker.record_normal_return(date(2016, 8, day), "daily-token")
    assert tracker.counter("weekly") == 2

    # Week 3 Wednesday: weekly fails. Should BAIL.
    with pytest.raises(HarnessFailureError):
        tracker.record_exception(date(2016, 8, 17), "weekly", exc)


def test_in_cycle_halt_counts_toward_bail(tmp_path):
    """Outcome 2: cycle.py returns normally but logs
    execution_halted=True. The tracker should detect this by reading
    the cycle.jsonl record and bail on three consecutive identical
    in-cycle halts.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)
    halt_reason = "growth SELL leg failed: quantity reduced to zero for FBCG"

    # Three consecutive weekly cycles each return normally but log
    # a halt with the same halt_reason.
    _write_in_cycle_halt_record(paths, halt_reason)
    tracker.record_normal_return(date(2005, 5, 4), "weekly")
    assert tracker.counter("weekly") == 1

    _write_in_cycle_halt_record(paths, halt_reason)
    tracker.record_normal_return(date(2005, 5, 11), "weekly")
    assert tracker.counter("weekly") == 2

    _write_in_cycle_halt_record(paths, halt_reason)
    with pytest.raises(HarnessFailureError) as excinfo:
        tracker.record_normal_return(date(2005, 5, 18), "weekly")
    msg = str(excinfo.value)
    assert "in_cycle_halt" in msg
    assert halt_reason in msg


def test_different_signatures_reset_counter_to_one(tmp_path):
    """Two ValidationError-like failures, then a TypeError-like
    failure. The counter should reset to 1 (not 0) on the third
    call because it's a NEW failure, not a reset event.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)

    exc_a = _make_exc("trigger_year out of range")
    # Different class to produce a different signature.
    class OtherError(Exception):
        pass
    exc_b = OtherError("some other problem")

    tracker.record_exception(date(2010, 1, 6), "weekly", exc_a)
    tracker.record_exception(date(2010, 1, 13), "weekly", exc_a)
    assert tracker.counter("weekly") == 2

    tracker.record_exception(date(2010, 1, 20), "weekly", exc_b)
    # New signature → counter resets to 1, not 0 (a failure happened).
    assert tracker.counter("weekly") == 1
    # And the signature is now exc_b's, not exc_a's.
    assert "OtherError" in tracker.last_signature("weekly")
    assert "some other problem" in tracker.last_signature("weekly")

    # Two more of exc_b would bail.
    tracker.record_exception(date(2010, 1, 27), "weekly", exc_b)
    assert tracker.counter("weekly") == 2
    with pytest.raises(HarnessFailureError):
        tracker.record_exception(date(2010, 2, 3), "weekly", exc_b)


# ============================================================================
# TESTS — sanity checks on the helper machinery
# ============================================================================

def test_exception_signature_is_stable_across_identical_exceptions(tmp_path):
    """_exception_signature should produce the SAME string for two
    exceptions with the same class and same first-line message,
    even if they're different objects.
    """
    exc1 = ValueError("same first line\nbut different details A")
    exc2 = ValueError("same first line\nbut different details B")
    assert _exception_signature(exc1) == _exception_signature(exc2)
    assert _exception_signature(exc1) == "ValueError: same first line"


def test_exception_signature_distinguishes_different_classes(tmp_path):
    """Different exception classes with the same message produce
    different signatures.
    """
    assert (
        _exception_signature(ValueError("foo"))
        != _exception_signature(TypeError("foo"))
    )


def test_tracker_writes_synthetic_halted_record(tmp_path):
    """record_exception() should write a record to cycle.jsonl with
    execution_halted=True and halt_reason matching the exception
    signature. This is the cycle-log-completeness half of the
    failure-handling design.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)
    exc = _make_exc("test_failure")

    tracker.record_exception(date(2016, 8, 3), "weekly", exc)

    # Read the cycle log directly.
    log_path = paths.cycle_log()
    assert log_path.exists()
    with log_path.open("r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["execution_halted"] is True
    assert rec["halt_reason"] == "ValueError: test_failure"
    assert rec["cycle_type"] == "weekly"
    assert rec["decision_clock"] == "2016-08-03T00:00:00"


# ============================================================================
# TESTS — production sequence simulation
# ============================================================================
#
# The unit tests above prove the tracker behaves correctly in
# isolation. These tests simulate the actual sequence of calls the
# tracker sees during a real 20-year baseline run, to prove the
# behavior holds over hundreds of calls with the same patterns the
# production hook produces.
#
# If these tests pass but the integration test still doesn't bail,
# the bug is in how the hook invokes the tracker, not in the
# tracker itself. That narrows the search space dramatically.

from datetime import timedelta as _timedelta


def _simulate_week_of_daily_tokens(tracker, week_start_thursday):
    """Simulate the six daily-token cycles between two Wednesday
    weekly cycles (Thu, Fri, Sat, Sun, Mon, Tue). Each calls
    record_normal_return with cycle_type='daily-token'.
    """
    for i in range(6):
        d = week_start_thursday + _timedelta(days=i)
        tracker.record_normal_return(d, "daily-token")


def test_production_sequence_baseline_full_replay(tmp_path):
    """Full replay of the 2005-04-20 → 2016-08-17 sequence the
    baseline run produces. ~593 clean weekly cycles, then the 3
    consecutive ValidationError exceptions starting 2016-08-03.

    Between every weekly cycle, 6 daily-token successes — the actual
    production cadence. If the tracker is correct, the 3rd weekly
    exception (2016-08-17) raises HarnessFailureError.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)

    # Walk forward Wednesday by Wednesday from 2005-04-20 through
    # 2016-07-27 (the last clean weekly cycle before the gap). Each
    # iteration: write a clean cycle.jsonl record, call
    # record_normal_return for the weekly, then simulate 6 daily-token
    # successes between this Wed and the next.
    current_wed = date(2005, 4, 20)
    end_clean_wed = date(2016, 7, 27)
    weekly_count = 0
    while current_wed <= end_clean_wed:
        _write_clean_record(paths)
        tracker.record_normal_return(current_wed, "weekly")
        assert tracker.counter("weekly") == 0, (
            f"counter should stay at 0 during clean preamble, got "
            f"{tracker.counter('weekly')} at {current_wed}"
        )
        _simulate_week_of_daily_tokens(tracker, current_wed + _timedelta(days=1))
        weekly_count += 1
        current_wed += _timedelta(days=7)

    # Sanity: we should have walked ~589 Wednesdays.
    assert 580 < weekly_count < 600, (
        f"expected ~589 weekly cycles in preamble, got {weekly_count}"
    )

    # 2016-08-03 (Wed): first ValidationError.
    exc = _make_exc("1 validation error for ScheduleStateInstance")
    tracker.record_exception(date(2016, 8, 3), "weekly", exc)
    assert tracker.counter("weekly") == 1

    # Thu → Tue daily-tokens.
    _simulate_week_of_daily_tokens(tracker, date(2016, 8, 4))
    assert tracker.counter("weekly") == 1, (
        "daily-token successes must not affect weekly counter"
    )

    # 2016-08-10 (Wed): second ValidationError.
    tracker.record_exception(date(2016, 8, 10), "weekly", exc)
    assert tracker.counter("weekly") == 2

    _simulate_week_of_daily_tokens(tracker, date(2016, 8, 11))
    assert tracker.counter("weekly") == 2

    # 2016-08-17 (Wed): third ValidationError → BAIL.
    with pytest.raises(HarnessFailureError) as excinfo:
        tracker.record_exception(date(2016, 8, 17), "weekly", exc)
    msg = str(excinfo.value)
    assert "Bailing after 3" in msg
    assert "2016-08-17" in msg


def test_production_sequence_with_isolated_outcome_2_halt_in_preamble(tmp_path):
    """Same as above but with the actual 2005-05-04 FBCG in-cycle
    halt in the preamble. Verifies that an isolated outcome-2 halt
    in the past (counter → 1 then reset to 0 next week) does not
    poison the counter for the failure cluster 11 years later.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)

    current_wed = date(2005, 4, 20)
    end_clean_wed = date(2016, 7, 27)
    fbcg_halt_date = date(2005, 5, 4)
    fbcg_halt_reason = "growth SELL leg failed: quantity reduced to zero for FBCG"

    while current_wed <= end_clean_wed:
        if current_wed == fbcg_halt_date:
            # Outcome 2: cycle.py logs execution_halted=True.
            _write_in_cycle_halt_record(paths, fbcg_halt_reason)
            tracker.record_normal_return(current_wed, "weekly")
            # Counter advances to 1 on this halt.
            assert tracker.counter("weekly") == 1
        else:
            _write_clean_record(paths)
            tracker.record_normal_return(current_wed, "weekly")
            # Counter is 0 again after the next clean cycle.
            assert tracker.counter("weekly") == 0
        _simulate_week_of_daily_tokens(tracker, current_wed + _timedelta(days=1))
        current_wed += _timedelta(days=7)

    # Now drive the three failures.
    exc = _make_exc("1 validation error for ScheduleStateInstance")
    tracker.record_exception(date(2016, 8, 3), "weekly", exc)
    _simulate_week_of_daily_tokens(tracker, date(2016, 8, 4))
    tracker.record_exception(date(2016, 8, 10), "weekly", exc)
    _simulate_week_of_daily_tokens(tracker, date(2016, 8, 11))
    with pytest.raises(HarnessFailureError):
        tracker.record_exception(date(2016, 8, 17), "weekly", exc)


def test_production_sequence_pydantic_style_multiline_signature(tmp_path):
    """Pydantic ValidationError's str() is multi-line. The signature
    is built from the FIRST line only, so three pydantic-style
    exceptions with the same first line but different details on
    later lines should still share a signature and bail at 3.

    This guards against a subtle failure mode: if the signature
    accidentally included instance-specific values (e.g., the
    'input_value=2016' line of a pydantic error), each cycle would
    have a different signature and the bail would never fire even
    though the underlying bug is identical.
    """
    paths = _make_paths(tmp_path)
    tracker = CycleFailureTracker(paths)

    # Build three exceptions whose str() shares only the first line.
    # The later lines differ in ways pydantic would naturally vary
    # (different field paths, different input values).
    def make_pydantic_like(suffix):
        msg = (
            "1 validation error for ScheduleStateInstance\n"
            "trigger_year\n"
            f"  Input should be greater than or equal to 2020 "
            f"[type=greater_than_equal, input_value={suffix}, input_type=int]"
        )
        return ValueError(msg)

    tracker.record_exception(date(2016, 8, 3), "weekly", make_pydantic_like(2016))
    tracker.record_exception(date(2016, 8, 10), "weekly", make_pydantic_like(2017))
    with pytest.raises(HarnessFailureError):
        tracker.record_exception(date(2016, 8, 17), "weekly", make_pydantic_like(2018))

