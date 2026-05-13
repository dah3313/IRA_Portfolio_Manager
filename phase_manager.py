"""
phase_manager.py — Phase state machine and transition triggers (§4, §6.1).

PURPOSE:
    Evaluate the Phase machine each cycle: detect calendar-based
    Phase 1 → Phase 2 transition, detect Phase 3 token activation /
    abort / latch, and report what just happened. The decision layer
    turns these events into PhaseTransition plan entries; the action
    layer executes them.

DESIGN (per §4.2, §6.1, §10.6):
    - This module is PURE: it computes the result of one Phase machine
      tick given the inputs (current phase state, calendar date, token
      observations). It does not write state files or issue alerts.
    - Phase progression is monotone (§4.2): once latched, no exit.
    - Phase 3 has a 24-hour grace window: tokens removed → grace starts;
      if tokens re-inserted during the window, grace aborts; if grace
      window expires with tokens still removed, Phase 3 latches.
    - The Phase 3 latched-but-pending window (I15) is captured: when
      `phase == PHASE_3` but `schedule_state.phase3 is None`, the
      decision layer knows the transition cycle has not run yet.

TWO-CYCLE ABORT PATTERN (§10.6.2 Pattern A):
    A re-insertion of any Phase 3 token during grace requires a SECOND
    daily-token cycle confirming the re-insertion before the abort
    commits. This guards against transient USB enumeration glitches.
    The `phase3_grace_pending_abort` field on OperatingState holds the
    interim observation; this module reads and may clear it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from clock import safe_year_offset
from state_model import (
    OperatingState,
    Phase,
    Phase3GracePendingAbort,
    TokensObserved,
)
from tokens import CombinedTokenState


# --- Event types -----------------------------------------------------------

@dataclass(frozen=True)
class PhaseEvaluationResult:
    """Outcome of a single phase-machine evaluation.

    Exactly one of the boolean flags will be True in any non-trivial
    evaluation; quiet cycles return all False.

    `new_phase` is the Phase the system should be in after this
    evaluation. For Phase 3 grace events, new_phase remains the
    pre-transition phase — the latch only happens when grace expires.

    `new_grace_window_start` and `new_grace_pending_abort` are the
    persisted values after this evaluation.

    `new_phase` and `latch_fired` together encode the transition state:
    - phase changed and latch_fired → Phase 3 latched this cycle.
    - phase changed and not latch_fired → Phase 1→2 calendar transition.
    - phase unchanged → grace events or quiet.
    """
    new_phase: Phase
    new_grace_window_start: Optional[datetime]
    new_grace_pending_abort: Optional[Phase3GracePendingAbort]

    calendar_transition_fired: bool  # PHASE_1 → PHASE_2
    grace_started: bool              # tokens just observed all removed
    grace_pending_abort_observed: bool  # re-insertion seen (one-cycle confirm pending)
    grace_aborted: bool              # second-cycle confirms re-insertion; clear grace
    latch_fired: bool                # grace window expired with tokens still removed


# --- Helpers ---------------------------------------------------------------

def _parse_phase1_to_phase2_date(s: str) -> date:
    """ruleset.yaml stores ISO date strings; convert to date for compare."""
    return date.fromisoformat(s)


# --- Main evaluation -------------------------------------------------------

def evaluate_phase_machine(
    *,
    state: OperatingState,
    now: datetime,
    today: date,
    combined_tokens: Optional[CombinedTokenState],
    phase1_to_phase2_transition_date: str,
    phase3_grace_window_hours: int,
) -> PhaseEvaluationResult:
    """One tick of the phase machine (§6.5 step 2).

    Args:
      state: persisted OperatingState (read-only here).
      now: cycle's captured decision_clock (UTC tz-aware).
      today: today's calendar date (typically now.date()).
      combined_tokens: combined two-box token observation, or None for
        cycles that don't refresh tokens (e.g., a weekly cycle running
        before today's daily-token cycle on a fresh box).
      phase1_to_phase2_transition_date: ruleset value (ISO string).
      phase3_grace_window_hours: ruleset value.

    Returns:
      PhaseEvaluationResult with the new phase indicator and grace state,
      plus event flags the decision/alerter layers consume.
    """
    current_phase = state.phase
    grace_start = state.phase3_grace_window_start
    grace_pending = state.phase3_grace_pending_abort

    # Default: no transition this cycle.
    result_phase = current_phase
    result_grace_start = grace_start
    result_pending = grace_pending
    cal_fired = False
    grace_started = False
    grace_pending_observed = False
    grace_aborted = False
    latch_fired = False

    # Phase 3 is terminal (§4.2). Return unchanged.
    if current_phase == Phase.PHASE_3:
        return PhaseEvaluationResult(
            new_phase=current_phase,
            new_grace_window_start=None,
            new_grace_pending_abort=None,
            calendar_transition_fired=False,
            grace_started=False,
            grace_pending_abort_observed=False,
            grace_aborted=False,
            latch_fired=False,
        )

    # 1) Phase 1 → Phase 2 calendar transition (only when in Phase 1).
    if current_phase == Phase.PHASE_1:
        transition_date = _parse_phase1_to_phase2_date(phase1_to_phase2_transition_date)
        if today >= transition_date:
            # Calendar transition fires. Pre-empts any Phase 3 grace
            # work for this cycle (rare overlap: calendar-day equals a
            # day with Phase 3 grace pending; calendar takes precedence
            # because the operator is alive and the date arrived).
            return PhaseEvaluationResult(
                new_phase=Phase.PHASE_2,
                new_grace_window_start=None,
                new_grace_pending_abort=None,
                calendar_transition_fired=True,
                grace_started=False,
                grace_pending_abort_observed=False,
                grace_aborted=False,
                latch_fired=False,
            )

    # 2) Phase 3 evaluation. Only meaningful when we have a valid
    # combined token observation. No observation → grace state holds.
    if combined_tokens is None:
        return PhaseEvaluationResult(
            new_phase=result_phase,
            new_grace_window_start=result_grace_start,
            new_grace_pending_abort=result_pending,
            calendar_transition_fired=False,
            grace_started=False,
            grace_pending_abort_observed=False,
            grace_aborted=False,
            latch_fired=False,
        )

    # Per §10.5 / §6.2: any UNAVAILABLE or mismatch holds previous state.
    if combined_tokens.any_unavailable or not combined_tokens.counts_match:
        return PhaseEvaluationResult(
            new_phase=result_phase,
            new_grace_window_start=result_grace_start,
            new_grace_pending_abort=result_pending,
            calendar_transition_fired=False,
            grace_started=False,
            grace_pending_abort_observed=False,
            grace_aborted=False,
            latch_fired=False,
        )

    tokens_all_removed = combined_tokens.phase3_all_removed

    # 2a) Currently in grace window.
    if grace_start is not None:
        grace_expiry = grace_start + timedelta(hours=phase3_grace_window_hours)

        if tokens_all_removed:
            # Tokens still removed.
            if now >= grace_expiry:
                # Grace expired → latch (§4.2, §10.6.3).
                result_phase = Phase.PHASE_3
                result_grace_start = None
                result_pending = None
                latch_fired = True
            else:
                # Still in grace; clear any pending-abort that was
                # logged but did not persist this cycle.
                if result_pending is not None:
                    result_pending = None
        else:
            # Tokens re-inserted somewhere → potential abort.
            if grace_pending is None:
                # First observation of re-insertion: log it; await
                # one-cycle persistence confirmation (Pattern A).
                result_pending = Phase3GracePendingAbort(
                    observed_at=now,
                    tokens_observed=TokensObserved(
                        # Map combined back to per-box counts. We
                        # don't have them directly here; record zeros
                        # (the operator inspects the daily token log
                        # files for the actual per-box numbers).
                        box_a_count=0,
                        box_b_count=0,
                    ),
                )
                grace_pending_observed = True
            else:
                # Second consecutive observation of re-insertion:
                # commit the abort.
                result_grace_start = None
                result_pending = None
                grace_aborted = True

        return PhaseEvaluationResult(
            new_phase=result_phase,
            new_grace_window_start=result_grace_start,
            new_grace_pending_abort=result_pending,
            calendar_transition_fired=cal_fired,
            grace_started=False,
            grace_pending_abort_observed=grace_pending_observed,
            grace_aborted=grace_aborted,
            latch_fired=latch_fired,
        )

    # 2b) Not currently in grace. Fresh removal → start grace.
    if tokens_all_removed:
        return PhaseEvaluationResult(
            new_phase=result_phase,
            new_grace_window_start=now,
            new_grace_pending_abort=None,
            calendar_transition_fired=False,
            grace_started=True,
            grace_pending_abort_observed=False,
            grace_aborted=False,
            latch_fired=False,
        )

    # No event this cycle.
    return PhaseEvaluationResult(
        new_phase=result_phase,
        new_grace_window_start=result_grace_start,
        new_grace_pending_abort=result_pending,
        calendar_transition_fired=False,
        grace_started=False,
        grace_pending_abort_observed=False,
        grace_aborted=False,
        latch_fired=False,
    )


# --- Helpers for the decision layer ----------------------------------------

def is_phase3_latched_but_pending(state: OperatingState) -> bool:
    """I15 helper: phase == PHASE_3 AND schedule_state.phase3 is None.

    During this window the decision layer suppresses withdrawal,
    rebalance, and large-cash-deployment work (§7.3, §7.5.1, §7.7.1)
    and emits only the Phase 3 transition plan (D12).
    """
    return (state.phase == Phase.PHASE_3
            and state.schedule_state.phase3 is None)


def phase_transition_just_executed(
    *, plan_has_phase_transition: bool,
    state_before: Phase,
    state_after: Phase,
) -> bool:
    """Decision-layer / alerter convenience: did this cycle just execute
    the phase reallocation (as opposed to merely latching)?"""
    return plan_has_phase_transition and state_before != state_after


__all__ = [
    "PhaseEvaluationResult",
    "evaluate_phase_machine",
    "is_phase3_latched_but_pending",
    "phase_transition_just_executed",
]
