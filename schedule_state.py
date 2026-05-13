"""
schedule_state.py — Inflation-adjusted withdrawal schedule math (§3.13).

PURPOSE:
    Pure function over the per-phase schedule_state tuple
    `(I_0, trigger_year, cpi, frozen_years)` to compute the scheduled
    monthly withdrawal for a given calendar year. This is the only place
    schedule arithmetic lives — every other module reads schedule values
    by calling these helpers.

WHY A SEPARATE MODULE:
    - The freeze logic is subtle: skipped raises do NOT stack (§7.6.1).
      Centralising it eliminates a class of "did we apply this year's
      raise?" bugs.
    - The Phase 1 vs Phase 3 distinction is just which ScheduleStateInstance
      to read from; the math is identical. Keeping it in one place
      preserves D8 (deterministic Plan given state).

§3.13 FORMULA:
    n_raises_applied = (Y - trigger_year)
                       - count(Y' in frozen_years where trigger_year+1 ≤ Y' ≤ Y)
    scheduled_monthly = I_0 × (1 + cpi)^n_raises_applied
"""

from __future__ import annotations

from decimal import Decimal

from state_model import Phase, ScheduleStateInstance


# --- Core helpers -----------------------------------------------------------

def n_raises_applied(instance: ScheduleStateInstance, current_year: int) -> int:
    """Count of CPI raises that have been applied as of `current_year`.

    Per §3.13: raises are scheduled for each year strictly greater than
    `trigger_year`, up to and including `current_year`. The frozen_years
    list permanently removes that year's raise from the count.

    Years before `trigger_year` produce 0 (the system was not yet running).
    Year == trigger_year produces 0 (I_0 is the starting amount;
    the first raise applies in trigger_year + 1).

    Frozen years outside the range are ignored — they cannot retroactively
    affect the schedule.
    """
    if current_year <= instance.trigger_year:
        return 0
    span_years = current_year - instance.trigger_year
    frozen_in_range = sum(
        1 for y in instance.frozen_years
        if instance.trigger_year + 1 <= y <= current_year
    )
    return span_years - frozen_in_range


def scheduled_monthly(
    instance: ScheduleStateInstance,
    current_year: int,
) -> Decimal:
    """Compute the scheduled monthly withdrawal for `current_year`.

    Pure function of the schedule_state tuple plus the year. Returns a
    Decimal rounded to no specific precision — callers that need cents
    can quantize at the boundary.

    Special case: current_year < trigger_year returns I_0 (system hasn't
    started yet; the value is informational only).
    """
    n = n_raises_applied(instance, current_year)
    if n == 0:
        return instance.i_0_dollars
    growth = (Decimal("1") + instance.cpi_rate) ** n
    return instance.i_0_dollars * growth


def is_year_frozen(instance: ScheduleStateInstance, year: int) -> bool:
    """Whether `year` appears in `frozen_years`."""
    return year in instance.frozen_years


def append_frozen_year(
    instance: ScheduleStateInstance,
    year: int,
) -> ScheduleStateInstance:
    """Return a new ScheduleStateInstance with `year` appended to
    frozen_years if not already present. Pure; does not mutate input.

    The annual review (§7.6) is the only caller that adds frozen years.
    """
    if year in instance.frozen_years:
        return instance
    new_frozen = sorted([*instance.frozen_years, year])
    return ScheduleStateInstance(
        i_0_dollars=instance.i_0_dollars,
        trigger_year=instance.trigger_year,
        cpi_rate=instance.cpi_rate,
        frozen_years=new_frozen,
    )


# --- Phase-aware dispatch ---------------------------------------------------

def active_schedule_instance(
    phase: Phase,
    schedule_state,
) -> ScheduleStateInstance | None:
    """Return the schedule_state instance for the active income phase.

    Phase 1 → schedule_state.phase1
    Phase 3 → schedule_state.phase3 (may be None during latched-but-
              pending window per I15; caller must handle this case)
    Phase 2 → None (no scheduled income exists in Phase 2)
    """
    if phase == Phase.PHASE_1:
        return schedule_state.phase1
    if phase == Phase.PHASE_3:
        return schedule_state.phase3
    # Phase 2: no scheduled income.
    return None


def current_monthly_withdrawal(
    phase: Phase,
    schedule_state,
    current_year: int,
) -> Decimal:
    """Convenience: the scheduled monthly withdrawal for the active phase
    at `current_year`. Returns Decimal('0') for Phase 2 or for the
    Phase 3 latched-but-pending window (I15) — callers that need to
    distinguish "no schedule" from "$0" should call
    `active_schedule_instance` and check for None.
    """
    instance = active_schedule_instance(phase, schedule_state)
    if instance is None:
        return Decimal("0")
    return scheduled_monthly(instance, current_year)


__all__ = [
    "n_raises_applied",
    "scheduled_monthly",
    "is_year_frozen",
    "append_frozen_year",
    "active_schedule_instance",
    "current_monthly_withdrawal",
]
