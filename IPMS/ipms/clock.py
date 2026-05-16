"""
clock.py — time abstraction for the simulator.

Public exports:
    Clock (Protocol) — the interface every clock implementation honors
    AdvancingClock — driven by the simulator's main loop
    SystemClock — wall-clock implementation (for completeness;
        simulator runs use AdvancingClock)

See also: IPMS_SPECIFICATION.md §6.1 (Clock seam)


WHY THIS LIVES IN IPMS

The IRA PM specification will eventually own the Clock abstraction
in production code (per spec §6.1: "the IRA PM owns the clock
abstraction; the IPMS uses it"). Until that exists, the IPMS
provides its own minimal version. The interface is small enough
(today, today_aware, advance_one_day) that the IRA PM's eventual
version will be compatible.

When the IRA PM lands its own clock module, this file may be
replaced by a re-export of the IRA PM's clock, OR remain in place
as a simulator-internal implementation. That decision is deferred
to the IRA PM specification phase.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol


# ============================================================================
# PROTOCOL
# ============================================================================

class Clock(Protocol):
    """
    Minimal time interface. Modules that need 'now' accept a Clock via
    constructor injection rather than calling datetime.now() directly.

    Two methods only:
      today() — current date.
      now() — current datetime (date + 00:00 for AdvancingClock; real
        wall-clock for SystemClock).

    The IRA PM's eventual clock module will likely add safe_year_offset
    and other arithmetic helpers; the IPMS doesn't need them yet, so
    they're omitted here. Forward-compatible: when the IRA PM clock
    arrives, modules using this Protocol can pick up additional
    methods through the IRA PM clock without code changes here.
    """
    def today(self) -> date: ...
    def now(self) -> datetime: ...


# ============================================================================
# ADVANCING CLOCK
# ============================================================================

class AdvancingClock:
    """
    Clock driven by the simulator's main loop. Constructed at the run's
    start_date, advanced one day at a time by the engine.

    Determinism: this clock does not consult wall-clock time at all.
    Two simulator runs with the same parameters produce the same
    sequence of clock values, regardless of when the runs occur.
    """

    def __init__(self, start_date: date):
        self._today: date = start_date

    def today(self) -> date:
        return self._today

    def now(self) -> datetime:
        # Combine current date with midnight. The IRA PM modules that
        # ask for 'now' typically only care about the date portion;
        # the time-of-day matters only for production-runtime concerns
        # (cron scheduling) which the simulator doesn't exercise.
        return datetime.combine(self._today, datetime.min.time())

    def advance_one_day(self) -> None:
        """Move forward by exactly one day."""
        self._today = self._today + timedelta(days=1)

    def set_date(self, d: date) -> None:
        """
        Jump to a specific date. Used during test setup; should not
        be called from production engine logic (which uses
        advance_one_day for determinism).
        """
        self._today = d


# ============================================================================
# SYSTEM CLOCK
# ============================================================================

class SystemClock:
    """
    Wall-clock implementation. Returns real time-of-day. Provided for
    interface completeness — the simulator never uses this. Production
    IRA PM will own its own SystemClock; this stub is convenience for
    tests and example code that constructs Clock-typed objects.
    """

    def today(self) -> date:
        return date.today()

    def now(self) -> datetime:
        return datetime.now()
