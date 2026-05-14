"""
annual_review.py — Annual review (§7.6).

PURPOSE:
    On the first cycle on or after `annual_review_date` (MM-DD,
    typically '01-15'), perform the combined annual event:

    1. FREEZE EVALUATION (§7.6.1): If the prior year had ≥
       `freeze_evaluation_threshold_days` (default 30) of cumulative
       CB1+ days, mark the current year as frozen — skip its CPI raise
       in the active phase's schedule_state. The freeze is permanent
       (§3.13 — skipped raises do not stack).

    2. BUFFER TARGET RECOMPUTE: Update buffer state from the
       newly-active monthly withdrawal:
         sgov_target  = current_monthly × sgov_buffer_target_months
         refill_rate  = sgov_target / 12
         cash_target  = current_monthly + cash_buffer_offset_dollars

DESIGN:
    Pure function: takes the operating state, ruleset, CB transition
    history, and decision_clock; returns a structured decision capturing
    the new schedule_state, new buffer_state, and the alerts to emit.

    Phase 2 reduces to a no-op (no scheduled income, no freeze).
    The caller is responsible for the date gate.

OUTPUTS:
    AnnualReviewDecision with:
      - new_schedule_state: the updated ScheduleState (may equal old).
      - new_buffer_state: the updated BufferState.
      - freeze_applied: did we freeze a year?
      - frozen_year: which year (if applicable).
      - alerts: annual_review_completed + optional freeze_decision.

Audit: append_annual_review() is the caller's job (persistence.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from plan_model import AlertEntry
from schedule_state import (
    append_frozen_year,
    current_monthly_withdrawal,
    scheduled_monthly,
)
from state_model import (
    BufferState,
    OperatingState,
    Phase,
    ScheduleState,
    ScheduleStateInstance,
)


# =============================================================================
# Result type
# =============================================================================

@dataclass(frozen=True)
class AnnualReviewDecision:
    """Outcome of the annual review event."""
    new_schedule_state: ScheduleState
    new_buffer_state: BufferState
    freeze_applied: bool
    frozen_year: Optional[int]
    cumulative_cb1_plus_days_prior_year: int
    completed_alert: AlertEntry
    freeze_alert: Optional[AlertEntry] = None


# =============================================================================
# Freeze evaluation
# =============================================================================

def compute_cumulative_cb1_plus_days(
    cb_transition_records: list[dict[str, Any]],
    *,
    year: int,
) -> int:
    """Compute cumulative days the system was in CB1 or CB2 during
    calendar `year`, from the CB transition log.

    Reads records of the form:
        { "timestamp": ISO-datetime,
          "from_state": "CB_INACTIVE" | "CB1" | "CB2",
          "to_state":   ... }

    Algorithm:
      1. Build a sorted list of (timestamp, new_state) within `year`,
         clamping a startup state at year's start (use the state at
         year's start, derived from the most recent transition before
         year start).
      2. Compute the spans; sum the time the state was in CB1 or CB2.
      3. Convert to days (fractional days truncated toward zero — a
         partial day still counts).

    The function is robust to missing entries: if there are no records
    at all, returns 0 (no CB activity in the year).

    Note: the algorithm assumes the CB transition log contains both
    entries AND exits (which §6.7 / §8.2.6 produce). If only entries
    are logged, the "currently in CB1/CB2 at year end" interval is
    not counted.
    """
    if not cb_transition_records:
        return 0

    # Parse into (ts, to_state) tuples
    parsed: list[tuple[datetime, str, str]] = []
    for rec in cb_transition_records:
        ts_str = rec.get("timestamp")
        to_state = rec.get("to_state")
        from_state = rec.get("from_state")
        if not (ts_str and to_state and from_state):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        # Normalize naive timestamps to UTC. The IRAPM clock seam
        # (clock.py) documents Clock.now() as returning NAIVE local
        # datetimes, and the CB transition log records timestamps via
        # `now.isoformat()` in action_layer._execute_cb_state_transition,
        # which produces ISO strings without a tz suffix. Re-parsing
        # those strings yields naive datetimes. We compare them to
        # year_start/year_end which are tz-aware UTC; without this
        # normalization the comparison raises TypeError.
        #
        # Treating missing tz as UTC is correct for the freeze
        # evaluation: the algorithm cares about calendar-year membership
        # and inter-event spans, not sub-day precision, and the system's
        # clock is modeled as UTC throughout the harness (AdvancingClock
        # attaches ET only at the now_et() boundary).
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((ts, from_state, to_state))

    if not parsed:
        return 0
    parsed.sort(key=lambda t: t[0])

    # Find the state at the start of `year` from records BEFORE year start
    year_start = datetime(year, 1, 1, tzinfo=timezone.utc)
    year_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)

    state_at_start = "CB_INACTIVE"
    for ts, _from, to in parsed:
        if ts < year_start:
            state_at_start = to
        else:
            break

    # Build a sequence of (ts, state) within [year_start, year_end]
    # bookended by year_start and year_end with the inferred states.
    events: list[tuple[datetime, str]] = [(year_start, state_at_start)]
    for ts, _from, to in parsed:
        if year_start <= ts < year_end:
            events.append((ts, to))
    events.append((year_end, events[-1][1]))  # closing marker

    total_seconds = 0.0
    for i in range(len(events) - 1):
        ts0, state0 = events[i]
        ts1, _state1 = events[i + 1]
        if state0 in ("CB1", "CB2"):
            total_seconds += (ts1 - ts0).total_seconds()

    # Truncate to whole days (any partial day still rounds down here;
    # the threshold is in whole days so this matches §7.6.1's intent).
    return int(total_seconds // (24 * 3600))


# =============================================================================
# Buffer recompute
# =============================================================================

def _recompute_buffer_state(
    *,
    new_monthly: Decimal,
    sgov_buffer_target_months: int,
    cash_buffer_offset_dollars: Decimal,
    previous: BufferState,
    now: datetime,
) -> BufferState:
    """Update buffer target / refill rate / cash target from new monthly.

    Preserves refill_delay_started_at and last_refill_at (they pertain
    to in-flight CB exit hysteresis and refill cadence, not to the
    annual target recompute).
    """
    sgov_target = new_monthly * sgov_buffer_target_months
    refill_rate = sgov_target / Decimal("12")
    cash_target = new_monthly + cash_buffer_offset_dollars
    return BufferState(
        sgov_target_dollars=sgov_target,
        monthly_refill_rate_dollars=refill_rate,
        cash_target_dollars=cash_target,
        recomputed_at=now,
        refill_delay_started_at=previous.refill_delay_started_at,
        last_refill_at=previous.last_refill_at,
    )


# =============================================================================
# Top-level decision
# =============================================================================

def perform_annual_review(
    *,
    state: OperatingState,
    cb_transition_records: list[dict[str, Any]],
    now: datetime,
    current_year: int,
    freeze_evaluation_threshold_days: int,
    sgov_buffer_target_months: int,
    cash_buffer_offset_dollars: Decimal,
) -> AnnualReviewDecision:
    """Execute the annual review event for `current_year`.

    Args:
      state: current OperatingState (read-only).
      cb_transition_records: CB transition log entries (any year).
      now: decision_clock (UTC tz-aware).
      current_year: the calendar year for which we are evaluating
        (the new year that just began). The freeze evaluation uses
        the prior year's CB1+ days; the new schedule_state applies
        starting now.
      freeze_evaluation_threshold_days, sgov_buffer_target_months,
      cash_buffer_offset_dollars: from ruleset.

    Returns AnnualReviewDecision. Phase 2 produces a no-op decision
    (no scheduled income, no freeze; buffer state copied through).
    """
    prior_year = current_year - 1

    # --- Phase 2: no-op (record-keeping only) ---
    if state.phase == Phase.PHASE_2:
        completed = AlertEntry(
            alert_id="annual_review_completed",
            context={
                "year": str(current_year),
                "phase": "PHASE_2",
                "note": "no_op_in_phase_2",
            },
        )
        return AnnualReviewDecision(
            new_schedule_state=state.schedule_state,
            new_buffer_state=state.buffer_state,
            freeze_applied=False,
            frozen_year=None,
            cumulative_cb1_plus_days_prior_year=0,
            completed_alert=completed,
        )

    # --- Phase 1 / Phase 3: freeze evaluation + buffer recompute ---
    # 1) Compute cumulative CB1+ days in the prior year
    cum_days = compute_cumulative_cb1_plus_days(
        cb_transition_records, year=prior_year
    )
    freeze = cum_days >= freeze_evaluation_threshold_days

    # 2) Update active phase's schedule_state if freezing this year
    new_schedule = state.schedule_state
    frozen_year_applied: Optional[int] = None
    if freeze:
        if state.phase == Phase.PHASE_1 and new_schedule.phase1 is not None:
            new_schedule = ScheduleState(
                phase1=append_frozen_year(new_schedule.phase1, current_year),
                phase3=new_schedule.phase3,
            )
            frozen_year_applied = current_year
        elif state.phase == Phase.PHASE_3 and new_schedule.phase3 is not None:
            new_schedule = ScheduleState(
                phase1=new_schedule.phase1,
                phase3=append_frozen_year(new_schedule.phase3, current_year),
            )
            frozen_year_applied = current_year
        # If active phase has no schedule_instance (e.g., Phase 3
        # latched-but-pending), freezing is meaningless and skipped.

    # 3) Recompute buffer state from the new monthly (post-freeze)
    new_monthly = current_monthly_withdrawal(
        state.phase, new_schedule, current_year
    )
    new_buffer = _recompute_buffer_state(
        new_monthly=new_monthly,
        sgov_buffer_target_months=sgov_buffer_target_months,
        cash_buffer_offset_dollars=cash_buffer_offset_dollars,
        previous=state.buffer_state,
        now=now,
    )

    # 4) Build alerts
    completed = AlertEntry(
        alert_id="annual_review_completed",
        context={
            "year": str(current_year),
            "phase": state.phase.value,
            "prior_year_cb1_plus_days": str(cum_days),
            "freeze_applied": "true" if freeze else "false",
            "new_monthly_withdrawal": str(new_monthly),
            "new_sgov_target": str(new_buffer.sgov_target_dollars),
            "new_cash_target": str(new_buffer.cash_target_dollars),
        },
    )

    freeze_alert: Optional[AlertEntry] = None
    if freeze:
        freeze_alert = AlertEntry(
            alert_id="freeze_decision",
            context={
                "year_frozen": str(frozen_year_applied) if frozen_year_applied else "",
                "prior_year_cb1_plus_days": str(cum_days),
                "threshold_days": str(freeze_evaluation_threshold_days),
            },
        )

    return AnnualReviewDecision(
        new_schedule_state=new_schedule,
        new_buffer_state=new_buffer,
        freeze_applied=freeze,
        frozen_year=frozen_year_applied,
        cumulative_cb1_plus_days_prior_year=cum_days,
        completed_alert=completed,
        freeze_alert=freeze_alert,
    )


__all__ = [
    "AnnualReviewDecision",
    "compute_cumulative_cb1_plus_days",
    "perform_annual_review",
]
