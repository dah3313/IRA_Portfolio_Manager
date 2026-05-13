"""
income_machine.py — Income state (ACTIVE / PAUSED) machine (§6.2).

PURPOSE:
    Translate combined STOP INCOME token observations into ACTIVE /
    PAUSED state per §6.2. Independent of Phase and CB machines.

DESIGN:
    - This module is PURE: a single function evaluating one cycle's
      transition given the current persisted state and the combined
      token observation.
    - AND semantics: both boxes must agree to change state.
    - Mismatch or UNAVAILABLE holds the previous state (§10.5).
    - In Phase 2 the state can transition (recorded for audit) but the
      ACHScheduleUpdate behavior is a no-op — Phase 2 already has no
      scheduled income. That distinction is handled by the decision
      layer; this module returns the same transitions regardless of
      phase.

OUTPUTS:
    IncomeEvaluationResult with:
      - new_state, did_transition, transition_reason.
    The decision layer turns a True did_transition into an
    ACHScheduleUpdate plan entry (§7.9).
"""

from __future__ import annotations

from dataclasses import dataclass

from state_model import IncomeState
from tokens import CombinedTokenState


@dataclass(frozen=True)
class IncomeEvaluationResult:
    """Outcome of an income-state evaluation."""
    new_state: IncomeState
    did_transition: bool
    transition_reason: str  # 'pause_inserted' | 'pause_removed' | 'hold_mismatch' | ''


def evaluate_income_machine(
    *,
    current_state: IncomeState,
    combined: CombinedTokenState,
) -> IncomeEvaluationResult:
    """One tick of the Income state machine (§6.5 step 3).

    Holds the previous state on any UNAVAILABLE or mismatch (§10.5).
    Otherwise applies AND semantics:
      - ACTIVE → PAUSED iff stopincome_active_on_both
      - PAUSED → ACTIVE iff NOT stopincome_active_on_both
    """
    # Hold on uncertain observations.
    if combined.any_unavailable or not combined.counts_match:
        return IncomeEvaluationResult(
            new_state=current_state,
            did_transition=False,
            transition_reason="hold_mismatch" if not combined.counts_match else "",
        )

    if combined.stopincome_active_on_both and current_state == IncomeState.ACTIVE:
        return IncomeEvaluationResult(
            new_state=IncomeState.PAUSED,
            did_transition=True,
            transition_reason="pause_inserted",
        )

    if (not combined.stopincome_active_on_both) and current_state == IncomeState.PAUSED:
        return IncomeEvaluationResult(
            new_state=IncomeState.ACTIVE,
            did_transition=True,
            transition_reason="pause_removed",
        )

    return IncomeEvaluationResult(
        new_state=current_state,
        did_transition=False,
        transition_reason="",
    )


__all__ = ["IncomeEvaluationResult", "evaluate_income_machine"]
