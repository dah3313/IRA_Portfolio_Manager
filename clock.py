"""
============================================================================
clock.py — Time Source Abstraction
============================================================================

PURPOSE:
    A single seam through which every module in the system reads "what
    time is it now".  Production uses SystemClock (real wall-clock time);
    backtests, simulations, and tests use FrozenClock or AdvancingClock
    to drive the system on synthetic dates without touching globals.

WHY THIS EXISTS:
    Before this module, ~40 sites across the codebase called
    date.today() and datetime.now() directly.  That made it impossible
    to test or simulate without monkey-patching the datetime module —
    a fragile workaround that didn't compose with the C-level callbacks
    in the IBKR API and could miss `from datetime import date` imports
    that captured the symbol before patching.

    With this seam, every module takes a Clock in its constructor (with
    a SystemClock default for backward compatibility) and reads time
    only through self._clock.  Tests inject a FrozenClock; the
    simulation harness injects an AdvancingClock that ticks once per
    weekly evaluation.

DESIGN:
    - Clock is a Protocol (structural typing) so test doubles don't
      need to subclass.
    - SystemClock is the production default; behavior is identical to
      pre-refactor direct calls.
    - FrozenClock is for unit tests where time should be a single
      pinned instant.
    - AdvancingClock is for backtests/simulations where time advances
      deterministically.

    All clocks expose three methods:
      today()  -> date           # local-day "today" used in business logic
      now()    -> datetime       # naive local datetime, used for record
                                   timestamps and JSON serialization
      now_et() -> datetime       # tz-aware ET datetime, used for IBKR
                                   maintenance window checks

SECURITY NOTES (ISSO perspective):
    - This module has no I/O, no network, no secrets.  It is pure code.
    - Production code path is unchanged when SystemClock is used: every
      call delegates to the same datetime/date functions used before.

============================================================================
"""

from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo


# Eastern Time zone — IBKR market hours and maintenance window are in ET.
# Defined here so all clock implementations share the same tzinfo object.
_ET = ZoneInfo("America/New_York")


@runtime_checkable
class Clock(Protocol):
    """
    The interface every time-source must satisfy.

    All three methods are required.  Modules read time only through these
    methods; they MUST NOT fall back to direct datetime/date calls because
    that would bypass test injection.
    """

    def today(self) -> date:
        """Return the current local date."""
        ...

    def now(self) -> datetime:
        """Return the current local datetime (naive)."""
        ...

    def now_et(self) -> datetime:
        """Return the current ET datetime (tz-aware)."""
        ...


class SystemClock:
    """
    Production clock.  Delegates to real wall-clock time.

    This is the default Clock for every module — when no clock is
    explicitly injected, modules fall back to SystemClock() so existing
    callers (and the smoke tests at the bottom of each module) keep
    working unchanged.
    """

    def today(self) -> date:
        return date.today()

    def now(self) -> datetime:
        return datetime.now()

    def now_et(self) -> datetime:
        return datetime.now(_ET)


class FrozenClock:
    """
    Pinned-instant clock.  Useful for unit tests where every call to
    today()/now() must return the same value, regardless of when in the
    test suite the call lands.

    Construct with either a date (in which case now() returns midnight
    of that date) or a datetime (in which case today() returns its date
    component).
    """

    def __init__(self, fixed):
        if isinstance(fixed, datetime):
            self._dt = fixed
            self._d = fixed.date()
        elif isinstance(fixed, date):
            self._d = fixed
            self._dt = datetime(fixed.year, fixed.month, fixed.day)
        else:
            raise TypeError(
                f"FrozenClock requires date or datetime; got {type(fixed)}"
            )

    def today(self) -> date:
        return self._d

    def now(self) -> datetime:
        return self._dt

    def now_et(self) -> datetime:
        # If the frozen datetime is naive, attach ET.  If it's already
        # tz-aware, convert.
        if self._dt.tzinfo is None:
            return self._dt.replace(tzinfo=_ET)
        return self._dt.astimezone(_ET)


class AdvancingClock:
    """
    Mutable clock for backtests and replay simulations.

    Caller is responsible for advancing the clock between evaluation
    ticks.  The clock has no auto-advance behavior — it only changes
    state when advance() or set_to() is called.

    Typical usage:
        clock = AdvancingClock(start=date(2005, 1, 1))
        scheduler = PortfolioScheduler(config, clock=clock)
        for week in range(52 * 20):
            scheduler.run_weekly_check()
            clock.advance(days=7)
    """

    def __init__(self, start):
        if isinstance(start, datetime):
            self._dt = start
        elif isinstance(start, date):
            self._dt = datetime(start.year, start.month, start.day)
        else:
            raise TypeError(
                f"AdvancingClock requires date or datetime; got {type(start)}"
            )

    def today(self) -> date:
        return self._dt.date()

    def now(self) -> datetime:
        return self._dt

    def now_et(self) -> datetime:
        # In a backtest, the simulated time is treated as ET wall-clock —
        # markets and IBKR maintenance windows are both in ET, so this is
        # the modeling choice that keeps simulation-and-production in sync.
        if self._dt.tzinfo is None:
            return self._dt.replace(tzinfo=_ET)
        return self._dt.astimezone(_ET)

    # Mutation interface — only AdvancingClock has these.

    def advance(self, days: int = 0, hours: int = 0,
                minutes: int = 0, seconds: int = 0) -> None:
        """Move the clock forward by the given amount."""
        self._dt = self._dt + timedelta(
            days=days, hours=hours, minutes=minutes, seconds=seconds
        )

    def set_to(self, when) -> None:
        """Jump the clock to a specific date or datetime."""
        if isinstance(when, datetime):
            self._dt = when
        elif isinstance(when, date):
            self._dt = datetime(when.year, when.month, when.day)
        else:
            raise TypeError(
                f"set_to requires date or datetime; got {type(when)}"
            )


# ============================================================================
# DATE ARITHMETIC HELPERS
# ============================================================================

def safe_year_offset(d: date, years: int) -> date:
    """
    Return d shifted forward (or backward) by `years` calendar years.

    Handles the leap-day case: if d is Feb 29 and the target year is not
    a leap year, returns Feb 28 of the target year instead of raising
    ValueError.  This matches the convention phase_manager.py uses for
    its own anniversary calculations.

    Examples:
        safe_year_offset(date(2032, 2, 29), 1)  -> date(2033, 2, 28)
        safe_year_offset(date(2032, 2, 29), 4)  -> date(2036, 2, 29)  (leap)
        safe_year_offset(date(2027, 1, 1), 8)   -> date(2035, 1, 1)

    Why this exists:
        Naive year arithmetic like
            anniversary = date(last.year + 1, last.month, last.day)
        crashes when `last` lands on Feb 29 of a leap year (e.g.,
        2032-02-29 falls on a Sunday, so the regular weekly check
        would record it).  This helper centralizes the defensive
        fallback so every site that does year arithmetic on persisted
        dates gets the same behavior.
    """
    try:
        return date(d.year + years, d.month, d.day)
    except ValueError:
        # The only way date() raises here is Feb 29 -> non-leap year.
        # Fall back to Feb 28 of the target year.
        return date(d.year + years, 2, 28)


__all__ = [
    "Clock", "SystemClock", "FrozenClock", "AdvancingClock",
    "safe_year_offset",
]
