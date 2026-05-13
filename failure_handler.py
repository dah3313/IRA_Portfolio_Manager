"""
failure_handler.py — Operational pause + withdrawal capacity lifecycle (§11.3).

PURPOSE:
    Manage the §11.3 operational-pause framework — the system-level
    "soft halt" that stops new decisions and orders until the
    underlying condition clears.

    Two distinct halt mechanisms, defined separately:

    1. OPERATIONAL PAUSE (§11.3.1):
       Triggered by classified failure conditions; the pause carries a
       `pause_reason` enum value. Auto-resume behavior depends on
       category (D-SPEC-8):
         - Self-healed-weird (PARTIAL_PHASE_TRANSITION, ORDER_REJECTION,
           DISK_FULL): auto-resume after `pause_auto_resume_hours`.
         - Hard-broke (INTERNAL_CONSISTENCY_VIOLATION,
           BROKER_INCONSISTENCY): no auto-resume; the operator must
           intervene.
       Re-alert on each subsequent cycle while paused (§11.3.1).
       Consecutive-pause escalation: when the same pause_reason fires
       on consecutive pauses ≥ `pause_consecutive_escalation_count`,
       severity escalates from Notice to Warning.

    2. WITHDRAWAL CAPACITY EXHAUSTED (§11.3.2):
       Set by the withdrawal subsystem when the cascade reaches zero
       across all stages. Halts withdrawal-related work indefinitely
       BUT does NOT halt other cycle work (CB evaluation, signal
       refresh, alerts continue). Auto-clears the moment the cascade
       can plausibly succeed again (re-evaluated at the next cycle's
       input refresh).

DESIGN:
    - This module is PURE. The cycle driver calls these functions
      with the persisted OperatingState; the returned values become
      the new state on the way to save_state.
    - No I/O, no clock side effects. The decision_clock is passed in
      explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from plan_model import AlertEntry
from state_model import (
    IncomeState,
    OperatingState,
    OperationalPause,
    PauseReason,
    Phase,
)


# =============================================================================
# Pause categorization (D-SPEC-8)
# =============================================================================

_SELF_HEALED_WEIRD: frozenset[PauseReason] = frozenset({
    PauseReason.PARTIAL_PHASE_TRANSITION,
    PauseReason.ORDER_REJECTION,
    PauseReason.DISK_FULL,
})

_HARD_BROKE: frozenset[PauseReason] = frozenset({
    PauseReason.INTERNAL_CONSISTENCY_VIOLATION,
    PauseReason.BROKER_INCONSISTENCY,
})


def is_self_healed_weird(reason: PauseReason) -> bool:
    return reason in _SELF_HEALED_WEIRD


def is_hard_broke(reason: PauseReason) -> bool:
    return reason in _HARD_BROKE


# =============================================================================
# Pause initiation
# =============================================================================

@dataclass(frozen=True)
class PauseInitiationResult:
    """Result of initiating an operational pause."""
    new_pause: OperationalPause
    alert: AlertEntry
    escalation_alert: Optional[AlertEntry] = None


def initiate_pause(
    *,
    current: OperationalPause,
    reason: PauseReason,
    now: datetime,
    consecutive_escalation_count: int,
    detail_context: dict[str, str] | None = None,
) -> PauseInitiationResult:
    """Initiate a pause with the given reason.

    Increments `consecutive_pause_count` if the new pause has the same
    reason as the immediately-prior one; otherwise resets to 1. When
    the count crosses `consecutive_escalation_count`, an additional
    `pause_consecutive_escalation` alert is produced.
    """
    same_reason = (current.paused
                   and current.pause_reason == reason)
    if same_reason:
        new_count = current.consecutive_pause_count + 1
    elif current.paused:
        # Different reason: this is a new escalation chain
        new_count = 1
    else:
        new_count = 1

    new_pause = OperationalPause(
        paused=True,
        pause_reason=reason,
        pause_started_at=current.pause_started_at or now,
        consecutive_pause_count=new_count,
    )

    init_alert = AlertEntry(
        alert_id="pause_initiated",
        context={
            "reason": reason.value,
            "category": ("self_healed_weird" if is_self_healed_weird(reason)
                         else "hard_broke"),
            "consecutive_pause_count": str(new_count),
            **(detail_context or {}),
        },
    )

    escalation: Optional[AlertEntry] = None
    if new_count >= consecutive_escalation_count:
        escalation = AlertEntry(
            alert_id="pause_consecutive_escalation",
            context={
                "reason": reason.value,
                "consecutive_pause_count": str(new_count),
                "escalation_threshold": str(consecutive_escalation_count),
            },
        )

    return PauseInitiationResult(
        new_pause=new_pause,
        alert=init_alert,
        escalation_alert=escalation,
    )


# =============================================================================
# Pause evaluation at the start of each cycle
# =============================================================================

@dataclass(frozen=True)
class PauseEvaluationResult:
    """Result of evaluating whether to remain paused or resume.

    `should_proceed_with_decisions` tells the cycle driver whether to
    enter the decision/action layers this cycle. When False, the cycle
    exits after persisting the new pause state and the re-alert.
    """
    new_pause: OperationalPause
    should_proceed_with_decisions: bool
    alert: Optional[AlertEntry] = None  # re-alert or auto-resume


def evaluate_pause(
    *,
    current: OperationalPause,
    now: datetime,
    pause_auto_resume_hours: int,
) -> PauseEvaluationResult:
    """Evaluate the pause at cycle start (§6.5 step 0).

    Branches:
      - Not paused: proceed.
      - Paused, hard-broke reason: re-alert, do not proceed.
      - Paused, self-healed-weird, within window: re-alert, do not proceed.
      - Paused, self-healed-weird, window elapsed: clear pause, emit
        auto-resume alert, proceed.
    """
    if not current.paused:
        return PauseEvaluationResult(
            new_pause=current,
            should_proceed_with_decisions=True,
        )

    if current.pause_reason is None or current.pause_started_at is None:
        # State inconsistency; treat as still paused but log a re-alert.
        # The decision_layer's input-validation can flip this into a
        # hard-broke INTERNAL_CONSISTENCY_VIOLATION if needed.
        alert = AlertEntry(
            alert_id="pause_re_alert",
            context={"reason": "unknown", "note": "pause state malformed"},
        )
        return PauseEvaluationResult(
            new_pause=current,
            should_proceed_with_decisions=False,
            alert=alert,
        )

    reason = current.pause_reason

    if is_hard_broke(reason):
        # Hard-broke: re-alert, do not auto-resume.
        alert = AlertEntry(
            alert_id="pause_re_alert",
            context={
                "reason": reason.value,
                "category": "hard_broke",
                "consecutive_pause_count": str(current.consecutive_pause_count),
            },
        )
        return PauseEvaluationResult(
            new_pause=current,
            should_proceed_with_decisions=False,
            alert=alert,
        )

    # Self-healed-weird: check auto-resume window.
    elapsed = now - current.pause_started_at
    if elapsed >= timedelta(hours=pause_auto_resume_hours):
        new_pause = OperationalPause(
            paused=False,
            pause_reason=None,
            pause_started_at=None,
            consecutive_pause_count=current.consecutive_pause_count,
            # Preserve consecutive_pause_count across the auto-resume —
            # if another pause for the same reason fires after this
            # resume, the count continues to increment (D-SPEC-8).
        )
        alert = AlertEntry(
            alert_id="pause_auto_resumed",
            context={
                "reason": reason.value,
                "category": "self_healed_weird",
                "elapsed_hours": str(int(elapsed.total_seconds() // 3600)),
            },
        )
        return PauseEvaluationResult(
            new_pause=new_pause,
            should_proceed_with_decisions=True,
            alert=alert,
        )

    # Still within window: re-alert, do not proceed.
    alert = AlertEntry(
        alert_id="pause_re_alert",
        context={
            "reason": reason.value,
            "category": "self_healed_weird",
            "elapsed_hours": str(int(elapsed.total_seconds() // 3600)),
            "auto_resume_hours": str(pause_auto_resume_hours),
        },
    )
    return PauseEvaluationResult(
        new_pause=current,
        should_proceed_with_decisions=False,
        alert=alert,
    )


# =============================================================================
# Withdrawal capacity exhausted (§11.3.2)
# =============================================================================

@dataclass(frozen=True)
class CapacityEvaluationResult:
    """Result of re-evaluating the withdrawal_capacity_exhausted flag."""
    new_value: bool
    cleared_this_cycle: bool
    alert: Optional[AlertEntry] = None


def evaluate_capacity_flag(
    *,
    state: OperatingState,
    current_portfolio_dollars: Decimal,
    scheduled_monthly: Decimal,
    position_residual_minimum_dollars: Decimal,
    growth_symbol_count: int,
    fi_symbol_count: int,
) -> CapacityEvaluationResult:
    """Re-evaluate the capacity-exhausted flag at cycle input refresh.

    The flag was set by a prior cycle's withdrawal cascade that
    couldn't fully source the scheduled withdrawal. It clears when
    the cascade would now plausibly succeed — concretely, when the
    portfolio's sellable amount (excluding residual floors) exceeds
    the scheduled monthly withdrawal.

    The estimate of sellable amount uses residual × symbol_count as a
    pessimistic lower bound on the portion locked at residual.
    """
    if not state.withdrawal_capacity_exhausted:
        return CapacityEvaluationResult(
            new_value=False,
            cleared_this_cycle=False,
        )

    # Sellable estimate: portfolio minus the residual floor for every
    # potentially-source position. Add cash buffer's SGOV residual once
    # for the buffer symbol.
    residual_lockup = (
        position_residual_minimum_dollars
        * (growth_symbol_count + fi_symbol_count + 1)  # +1 for SGOV
    )
    sellable_estimate = current_portfolio_dollars - residual_lockup

    if sellable_estimate >= scheduled_monthly:
        # Conditions look survivable — let the next withdrawal attempt
        # discover this for real. Clearing the flag returns the
        # decision_layer to normal flow.
        alert = AlertEntry(
            alert_id="withdrawal_capacity_exhausted",
            severity_override="NOTICE",
            context={
                "status": "cleared",
                "portfolio": str(current_portfolio_dollars),
                "scheduled_monthly": str(scheduled_monthly),
                "sellable_estimate": str(sellable_estimate),
                "note": "capacity restored; resuming withdrawal evaluation",
            },
        )
        return CapacityEvaluationResult(
            new_value=False,
            cleared_this_cycle=True,
            alert=alert,
        )

    # Still exhausted. The CRITICAL re-alert behavior is owned by the
    # decision layer (which fires it once per cycle while flag is set).
    return CapacityEvaluationResult(
        new_value=True,
        cleared_this_cycle=False,
    )


def set_capacity_exhausted(state: OperatingState) -> OperatingState:
    """Helper: produce a new OperatingState with the flag set.

    Used by the decision_layer when a WithdrawalCapacityExhausted
    exception bubbles out of withdrawal.py.
    """
    return state.model_copy(update={"withdrawal_capacity_exhausted": True})


def build_capacity_exhausted_alert(
    *, scheduled_monthly: Decimal,
    portfolio_dollars: Decimal,
) -> AlertEntry:
    """Build the §12.6 withdrawal_capacity_exhausted alert (CRITICAL)."""
    return AlertEntry(
        alert_id="withdrawal_capacity_exhausted",
        context={
            "scheduled_monthly": str(scheduled_monthly),
            "portfolio": str(portfolio_dollars),
            "category": "hard_broke",
        },
    )


__all__ = [
    "PauseInitiationResult",
    "PauseEvaluationResult",
    "CapacityEvaluationResult",
    "is_self_healed_weird",
    "is_hard_broke",
    "initiate_pause",
    "evaluate_pause",
    "evaluate_capacity_flag",
    "set_capacity_exhausted",
    "build_capacity_exhausted_alert",
]
