"""
decision_layer.py — Per-cycle decision composition (§7.1).

PURPOSE:
    Compose the per-subsystem decision modules (phase machine, CB
    machine, income machine, withdrawal, rebalance, refill, cash
    buffer, annual review, phase transitions) into a single Plan
    object for the cycle. Pure function from inputs to (new state,
    Plan).

DESIGN (per §7.1, D-SPEC-8, D8, D12):
    1. Pause evaluation gates entry (failure_handler.evaluate_pause).
    2. Capacity-exhausted re-evaluation may auto-clear the flag.
    3. Phase machine evaluation produces the new phase + events
       (calendar transition, grace start/pending-abort/abort, latch).
    4. CB machine evaluation produces the new CB state.
    5. Income machine evaluation produces the new income state.
    6. Phase 3 latched-but-pending (I15) routes to a Phase 3 transition
       plan ONLY — other decision steps are suppressed (D12).
    7. Normal cycle:
       a. Date-gated calendar events: annual review (if today is
          annual_review_date or later AND not yet done this year).
       b. Withdrawal (if scheduled day AND active phase has schedule
          AND income state ACTIVE).
       c. Cash buffer / large-cash-deployment.
       d. SGOV refill.
       e. Standard rebalance (5/25) or Phase 2 swing or Phase 2
          semi-annual reallocation, gated by phase + CB state.
    8. CB / phase / income transitions add CBStateTransitionEntry and
       ACHScheduleUpdateEntry to the plan.
    9. All alerts collected during decisions are appended LAST so the
       action layer dispatches them after order execution.

The decision_layer.py is intentionally long: it is the single place
in the system that knows the order of operations, the suppression
rules, and the cross-subsystem dependencies. Splitting it across
multiple files would obscure the orchestration logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Optional

from alert_catalog import AlertId
from annual_review import AnnualReviewDecision, perform_annual_review
from broker_types import AccountSummary, Position
from cash_buffer import CashBufferDecision, CashBufferInputs, decide_cash_buffer
from cb_machine import CBEvaluationResult, CBInputs, evaluate_cb_machine
from failure_handler import (
    PauseEvaluationResult,
    build_capacity_exhausted_alert,
    evaluate_capacity_flag,
    evaluate_pause,
)
from income_machine import IncomeEvaluationResult, evaluate_income_machine
from lookback_signal import LookbackResult, compute_synthetic_growth_lookback
from phase_manager import (
    PhaseEvaluationResult,
    evaluate_phase_machine,
    is_phase3_latched_but_pending,
)
from phase_transitions import (
    PhaseTransitionDecision,
    build_phase1_to_phase2,
    build_phase1_to_phase3,
    build_phase2_to_phase3,
)
from plan_model import (
    ACHScheduleUpdateEntry,
    AlertEntry,
    CBStateTransitionEntry,
    EntryKind,
    Plan,
    PlanEntry,
)
from rebalancer import (
    Phase2SemiAnnualInputs,
    Phase2SwingInputs,
    StandardRebalanceInputs,
    decide_phase2_semi_annual,
    decide_phase2_swing,
    decide_standard_rebalance,
)
from ruleset_model import Ruleset
from schedule_state import (
    active_schedule_instance,
    current_monthly_withdrawal,
    scheduled_monthly,
)
from sgov_refill import SGOVRefillInputs, decide_buffer_refill
from state_model import (
    CBState,
    IncomeState,
    LookbackStatus,
    OperatingState,
    Phase,
    Phase2SwingState,
)
from tokens import CombinedTokenState
from withdrawal import (
    WithdrawalCapacityExhausted,
    WithdrawalDecisionError,
    WithdrawalInputs,
    decide_withdrawal,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Decision inputs (everything the cycle's input refresh produced)
# =============================================================================

@dataclass(frozen=True)
class DecisionInputs:
    """Bundle of per-cycle inputs for the decision layer."""
    state: OperatingState
    ruleset: Ruleset
    now: datetime
    today: date

    # Broker-reported snapshot
    positions: dict[str, Position]
    account_summary: AccountSummary

    # Token observation (None if cycle is not refreshing tokens)
    combined_tokens: Optional[CombinedTokenState]

    # Lookback signal computed fresh this cycle
    lookback: LookbackResult

    # CB transition log records for the current and prior year (for
    # annual review's freeze evaluation)
    cb_transition_records: list[dict[str, Any]]

    # Whether today is the scheduled monthly withdrawal day (computed
    # by cycle.py from the monthly cadence rule)
    is_scheduled_withdrawal_day: bool

    # Whether today triggers annual review (computed by cycle.py)
    is_annual_review_day: bool

    # Whether today triggers a Phase 2 semi-annual reallocation
    is_phase2_reallocation_day: bool

    # Cycle identity
    cycle_id: str


# =============================================================================
# Decision output
# =============================================================================

@dataclass
class DecisionOutput:
    """Outcome of decision_layer evaluation."""
    new_state: OperatingState
    plan: Plan
    should_skip_action_layer: bool = False
    # When True, the cycle driver does not execute the plan; it just
    # persists state and dispatches alerts. Used during operational
    # pause, capacity exhaustion, and empty plans.


# =============================================================================
# Helpers
# =============================================================================

def _active_phase_classification(
    phase: Phase, ruleset: Ruleset
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, Decimal]]:
    """Return (growth_symbols, fi_symbols, target_weights) for the
    active phase. For Phase 2 the "steady" weights are used by default;
    callers that need deployed weights handle that separately.
    """
    if phase == Phase.PHASE_1:
        weights = ruleset.phase1_target_weights.weights
        growth = tuple(s for s in weights if s in ("FBCG", "AVUV"))
        fi = tuple(s for s in weights if s in ("PYLD", "JPIE"))
    elif phase == Phase.PHASE_2:
        weights = ruleset.phase2_steady_target_weights.weights
        growth = tuple(s for s in weights if s in ("FBCG", "AVUV"))
        fi = tuple(s for s in weights if s == "GBIL")
    else:  # PHASE_3
        weights = ruleset.phase3_target_weights.weights
        growth = tuple(s for s in weights if s in ("FBCG", "AVUV"))
        fi = tuple(s for s in weights if s in ("PYLD", "JPIE"))
    return growth, fi, dict(weights)


def _core_portfolio_value(positions: dict[str, Position],
                          target_weights: dict[str, Decimal]) -> Decimal:
    """Sum of market values of all symbols in the active-phase target
    weights (i.e., Growth + FI). Excludes buffer (SGOV) and cash."""
    return sum(
        (positions[s].market_value for s in target_weights if s in positions),
        start=Decimal("0"),
    )


def _core_fi_value(positions: dict[str, Position],
                   fi_symbols: tuple[str, ...]) -> Decimal:
    return sum(
        (positions[s].market_value for s in fi_symbols if s in positions),
        start=Decimal("0"),
    )


def _portfolio_total(positions: dict[str, Position],
                     buffer_symbol: str) -> Decimal:
    """All securities including the SGOV buffer; excludes cash."""
    return sum(
        (p.market_value for p in positions.values()),
        start=Decimal("0"),
    )


def _buffer_value(positions: dict[str, Position],
                  buffer_symbol: str) -> Decimal:
    p = positions.get(buffer_symbol)
    return p.market_value if p is not None else Decimal("0")


# =============================================================================
# Main entry point
# =============================================================================

def decide(inputs: DecisionInputs) -> DecisionOutput:
    """Compute the cycle's Plan and the post-cycle OperatingState.

    The function does not mutate `inputs.state`; it returns a new
    OperatingState via model_copy/update.

    The full pipeline (§7.1):
      1. Pause evaluation
      2. Capacity-exhausted re-evaluation
      3. Phase machine
      4. CB machine
      5. Income machine
      6. Decision branches (see §7.1)
    """
    state = inputs.state
    ruleset = inputs.ruleset
    now = inputs.now
    today = inputs.today
    plan = Plan(cycle_id=inputs.cycle_id, decision_clock=now)
    alerts: list[AlertEntry] = []  # accumulator; appended last

    # -------------------------------------------------------------------
    # Step 1: Pause evaluation
    # -------------------------------------------------------------------
    pause_result = evaluate_pause(
        current=state.operational_pause,
        now=now,
        pause_auto_resume_hours=ruleset.pause_auto_resume_hours,
    )
    new_state = state.model_copy(update={"operational_pause": pause_result.new_pause})
    if pause_result.alert is not None:
        alerts.append(pause_result.alert)
    if not pause_result.should_proceed_with_decisions:
        # Skip everything; persist state + alerts
        for a in alerts:
            plan.add(a)
        new_state = _bump_cycle_marker(new_state, now, inputs.cycle_id)
        return DecisionOutput(
            new_state=new_state, plan=plan, should_skip_action_layer=True,
        )

    # -------------------------------------------------------------------
    # Step 2: Capacity-exhausted re-evaluation
    # -------------------------------------------------------------------
    cap_growth, cap_fi, cap_weights = _active_phase_classification(
        new_state.phase, ruleset
    )
    cap_eval = evaluate_capacity_flag(
        state=new_state,
        current_portfolio_dollars=_portfolio_total(
            inputs.positions, "SGOV"),
        scheduled_monthly=current_monthly_withdrawal(
            new_state.phase, new_state.schedule_state, today.year),
        position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
        growth_symbol_count=len(cap_growth),
        fi_symbol_count=len(cap_fi),
    )
    if cap_eval.new_value != new_state.withdrawal_capacity_exhausted:
        new_state = new_state.model_copy(
            update={"withdrawal_capacity_exhausted": cap_eval.new_value}
        )
    if cap_eval.alert is not None:
        alerts.append(cap_eval.alert)

    # -------------------------------------------------------------------
    # Step 3: Phase machine
    # -------------------------------------------------------------------
    phase_result = evaluate_phase_machine(
        state=new_state,
        now=now,
        today=today,
        combined_tokens=inputs.combined_tokens,
        phase1_to_phase2_transition_date=ruleset.phase1_to_phase2_transition_date,
        phase3_grace_window_hours=ruleset.phase3_grace_window_hours,
    )
    new_state = _apply_phase_result(new_state, phase_result)
    _emit_phase_alerts(phase_result, alerts)

    # -------------------------------------------------------------------
    # Phase transition: when phase changed, build the transition plan
    # and SUPPRESS all other decision work (§7.2.4).
    # -------------------------------------------------------------------
    if phase_result.calendar_transition_fired:
        # Phase 1 → Phase 2
        decision = build_phase1_to_phase2(
            positions=inputs.positions,
            phase2_steady_weights=ruleset.phase2_steady_target_weights.weights,
            position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
        )
        plan.add(decision.entry)
        # Pause income (no Phase 2 withdrawals)
        plan.add(ACHScheduleUpdateEntry(
            new_amount_dollars=Decimal("0"),
            reason="phase1_to_phase2_transition",
        ))
        alerts.append(decision.large_rebalance_alert)
        return _finalize(new_state, plan, alerts, now, inputs.cycle_id)

    if phase_result.latch_fired:
        # Phase 3 latch — but transition plan does NOT run this same cycle.
        # The latch only sets phase=PHASE_3; schedule_state.phase3 stays None.
        # The next cycle sees is_phase3_latched_but_pending and builds the
        # transition plan (D12, I15).
        # Emit phase3_activation alert (latch event) here; the transition
        # cycle re-emits with calc details.
        alerts.append(AlertEntry(
            alert_id="phase3_activation",
            context={"event": "latch", "transition_plan_next_cycle": "true"},
        ))
        return _finalize(new_state, plan, alerts, now, inputs.cycle_id)

    # I15: Phase 3 latched but not yet transitioned → build transition plan now
    if is_phase3_latched_but_pending(new_state):
        prior_phase = _infer_prior_phase_for_phase3(new_state, inputs.positions)
        if prior_phase == Phase.PHASE_1:
            decision = build_phase1_to_phase3(
                positions=inputs.positions,
                phase3_weights=ruleset.phase3_target_weights.weights,
                position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
            )
        else:
            decision = build_phase2_to_phase3(
                positions=inputs.positions,
                phase3_weights=ruleset.phase3_target_weights.weights,
                position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
            )
        plan.add(decision.entry)
        alerts.append(decision.large_rebalance_alert)
        if decision.phase3_activation_alert:
            alerts.append(decision.phase3_activation_alert)
        return _finalize(new_state, plan, alerts, now, inputs.cycle_id)

    # -------------------------------------------------------------------
    # Step 4: CB machine
    # -------------------------------------------------------------------
    if new_state.phase != Phase.PHASE_2:
        cb_result = _evaluate_cb(new_state, inputs)
        new_state = _apply_cb_result(new_state, cb_result)
        if cb_result.did_transition:
            plan.add(CBStateTransitionEntry(
                from_state=state.cb_machine.state,
                to_state=cb_result.new_state,
                trigger_reason=cb_result.transition_reason,
                cb2_entry_conditions_after=sorted(
                    cb_result.new_cb2_entry_conditions,
                    key=lambda c: c.value,
                ),
            ))
            alerts.append(AlertEntry(
                alert_id="cb_transition",
                context={
                    "from_state": state.cb_machine.state.value,
                    "to_state": cb_result.new_state.value,
                    "reason": cb_result.transition_reason,
                },
            ))

    # -------------------------------------------------------------------
    # Step 5: Income machine
    # -------------------------------------------------------------------
    if inputs.combined_tokens is not None:
        income_result = evaluate_income_machine(
            current_state=new_state.income_state,
            combined=inputs.combined_tokens,
        )
        if income_result.did_transition:
            new_state = new_state.model_copy(update={
                "income_state": income_result.new_state,
                "income_state_changed_at": now,
            })
            # ACH update
            if income_result.new_state == IncomeState.PAUSED:
                ach_amount = Decimal("0")
                reason = "income_state_paused"
            else:
                ach_amount = current_monthly_withdrawal(
                    new_state.phase, new_state.schedule_state, today.year)
                reason = "income_state_resumed"
            plan.add(ACHScheduleUpdateEntry(
                new_amount_dollars=ach_amount,
                reason=reason,
            ))
            alerts.append(AlertEntry(
                alert_id="token_state_change",
                context={
                    "kind": "stopincome",
                    "from": ("ACTIVE" if income_result.new_state == IncomeState.PAUSED
                             else "PAUSED"),
                    "to": income_result.new_state.value,
                },
            ))

    # -------------------------------------------------------------------
    # Step 6: Annual review (only on annual_review_day)
    # -------------------------------------------------------------------
    if inputs.is_annual_review_day:
        ar = perform_annual_review(
            state=new_state,
            cb_transition_records=inputs.cb_transition_records,
            now=now,
            current_year=today.year,
            freeze_evaluation_threshold_days=ruleset.freeze_evaluation_threshold_days,
            sgov_buffer_target_months=ruleset.sgov_buffer_target_months,
            cash_buffer_offset_dollars=ruleset.cash_buffer_offset_dollars,
        )
        new_state = new_state.model_copy(update={
            "schedule_state": ar.new_schedule_state,
            "buffer_state": ar.new_buffer_state,
        })
        alerts.append(ar.completed_alert)
        if ar.freeze_alert is not None:
            alerts.append(ar.freeze_alert)
        # If the freeze decision (or unfrozen raise) changed the monthly,
        # push an ACHScheduleUpdate. Phase 2 is no-op already handled.
        if new_state.phase in (Phase.PHASE_1, Phase.PHASE_3):
            new_monthly = current_monthly_withdrawal(
                new_state.phase, new_state.schedule_state, today.year)
            plan.add(ACHScheduleUpdateEntry(
                new_amount_dollars=new_monthly,
                reason="annual_review",
            ))

    # -------------------------------------------------------------------
    # Step 7: Withdrawal (date-gated by is_scheduled_withdrawal_day)
    # -------------------------------------------------------------------
    if (inputs.is_scheduled_withdrawal_day
        and new_state.income_state == IncomeState.ACTIVE
        and new_state.phase in (Phase.PHASE_1, Phase.PHASE_3)
        and active_schedule_instance(new_state.phase, new_state.schedule_state) is not None
        and not new_state.withdrawal_capacity_exhausted):
        new_state, plan, alerts = _attempt_withdrawal(
            new_state, inputs, plan, alerts)

    # -------------------------------------------------------------------
    # Step 8: Cash buffer / large cash deployment
    # -------------------------------------------------------------------
    if new_state.phase != Phase.PHASE_2 or True:  # active in all phases
        cash_result = _evaluate_cash_buffer(new_state, inputs)
        if cash_result.cash_refill_entry is not None:
            plan.add(cash_result.cash_refill_entry)
        if cash_result.large_cash_deployment_entry is not None:
            plan.add(cash_result.large_cash_deployment_entry)
            if cash_result.deployment_alert is not None:
                alerts.append(cash_result.deployment_alert)

    # -------------------------------------------------------------------
    # Step 9: SGOV buffer refill
    # -------------------------------------------------------------------
    refill = _evaluate_sgov_refill(new_state, inputs)
    if refill is not None:
        plan.add(refill)

    # -------------------------------------------------------------------
    # Step 10: Rebalancing
    # -------------------------------------------------------------------
    new_state, rebalance_orders, rebalance_alerts = _evaluate_rebalance(
        new_state, inputs)
    for o in rebalance_orders:
        plan.add(o)
    alerts.extend(rebalance_alerts)

    # -------------------------------------------------------------------
    # Finalize: bump cycle marker, append alerts last
    # -------------------------------------------------------------------
    return _finalize(new_state, plan, alerts, now, inputs.cycle_id)


# =============================================================================
# Internal helpers (kept private to the orchestrator)
# =============================================================================

def _bump_cycle_marker(state: OperatingState, now: datetime,
                       cycle_id: str) -> OperatingState:
    """Update last_cycle_id / last_cycle_at after this cycle's work."""
    from uuid import UUID
    try:
        cid = UUID(cycle_id)
    except ValueError:
        # Fall back to a new UUID; the cycle_id string is meant to be
        # a UUID hex per cycle_attempt.py.
        from uuid import uuid4
        cid = uuid4()
    return state.model_copy(update={
        "last_cycle_id": cid,
        "last_cycle_at": now,
    })


def _finalize(state: OperatingState,
              plan: Plan,
              alerts: list[AlertEntry],
              now: datetime,
              cycle_id: str) -> DecisionOutput:
    """Append alerts last and bump the cycle marker on the new state."""
    for a in alerts:
        plan.add(a)
    state = _bump_cycle_marker(state, now, cycle_id)
    return DecisionOutput(
        new_state=state,
        plan=plan,
        should_skip_action_layer=plan.is_empty(),
    )


def _apply_phase_result(state: OperatingState,
                        result: PhaseEvaluationResult) -> OperatingState:
    """Apply the phase machine's output to OperatingState."""
    updates: dict[str, Any] = {}
    if result.new_phase != state.phase:
        updates["phase"] = result.new_phase
        # Phase 2 wipes phase2_opportunistic to fresh STEADY; Phase 3
        # discards pending counters (§4.3 step 2).
        if result.new_phase == Phase.PHASE_2:
            from state_model import Phase2Opportunistic, PendingCounter
            updates["phase2_opportunistic"] = Phase2Opportunistic()
    if result.new_grace_window_start != state.phase3_grace_window_start:
        updates["phase3_grace_window_start"] = result.new_grace_window_start
    if result.new_grace_pending_abort != state.phase3_grace_pending_abort:
        updates["phase3_grace_pending_abort"] = result.new_grace_pending_abort
    if updates:
        return state.model_copy(update=updates)
    return state


def _emit_phase_alerts(result: PhaseEvaluationResult,
                       alerts: list[AlertEntry]) -> None:
    """Append the appropriate Phase-related alerts based on events."""
    if result.grace_started:
        alerts.append(AlertEntry(
            alert_id="phase3_grace_started",
            context={"event": "grace_window_open"},
        ))
    if result.grace_pending_abort_observed:
        alerts.append(AlertEntry(
            alert_id="phase3_grace_pending_abort",
            context={"event": "re_insertion_observed_awaiting_confirmation"},
        ))
    if result.grace_aborted:
        alerts.append(AlertEntry(
            alert_id="phase3_grace_aborted",
            context={"event": "re_insertion_confirmed_grace_cleared"},
        ))
    # latch_fired is alerted at the call site (different alert content).
    # calendar_transition_fired ditto.


def _evaluate_cb(state: OperatingState, inputs: DecisionInputs) -> CBEvaluationResult:
    """Run the CB machine."""
    ruleset = inputs.ruleset
    growth, fi, weights = _active_phase_classification(state.phase, ruleset)
    core_total = _core_portfolio_value(inputs.positions, weights)
    core_fi = _core_fi_value(inputs.positions, fi)
    monthly = current_monthly_withdrawal(
        state.phase, state.schedule_state, inputs.today.year)
    cb_inputs = CBInputs(
        signal_status=inputs.lookback.status,
        signal_value=inputs.lookback.value,
        core_portfolio_dollars=core_total,
        core_fi_dollars=core_fi,
        current_monthly_withdrawal=monthly,
        now=inputs.now,
        cb1_threshold_rate=ruleset.cb1_threshold_rate,
        cb2_threshold_rate=ruleset.cb2_threshold_rate,
        cb1_recovery_buffer_rate=ruleset.cb1_recovery_buffer_rate,
        cb2_recovery_buffer_rate=ruleset.cb2_recovery_buffer_rate,
        portfolio_low_threshold_dollars=ruleset.portfolio_low_threshold_dollars,
        portfolio_low_threshold_months=ruleset.portfolio_low_threshold_months,
        portfolio_low_recovery_buffer_rate=ruleset.portfolio_low_recovery_buffer_rate,
        fi_low_threshold_months=ruleset.fi_low_threshold_months,
        fi_low_recovery_buffer_rate=ruleset.fi_low_recovery_buffer_rate,
        confirmation_window_weeks=ruleset.confirmation_window_weeks,
        cb1_to_cb2_timer_days=ruleset.cb1_to_cb2_timer_days,
        resource_evaluation_enabled=(
            not is_phase3_latched_but_pending(state)
        ),
    )
    return evaluate_cb_machine(cb=state.cb_machine, inputs=cb_inputs)


def _apply_cb_result(state: OperatingState,
                     result: CBEvaluationResult) -> OperatingState:
    """Update OperatingState with CB evaluation results."""
    from state_model import CBMachine
    new_cb = CBMachine(
        state=result.new_state,
        cb1_active_timer_started_at=result.new_cb1_active_timer_started_at,
        cb2_entry_conditions=result.new_cb2_entry_conditions,
        pending_confirmations=result.new_pending_confirmations,
    )
    updates: dict[str, Any] = {"cb_machine": new_cb}
    # If CB2 just exited (any condition cleared), start the post-recovery
    # delay timer on buffer_state (used by SGOV refill — §7.4).
    if (state.cb_machine.state == CBState.CB2
        and result.new_state != CBState.CB2):
        updates["buffer_state"] = state.buffer_state.model_copy(
            update={"refill_delay_started_at": result.new_cb1_active_timer_started_at
                    or state.buffer_state.refill_delay_started_at}
        )
    return state.model_copy(update=updates)


def _infer_prior_phase_for_phase3(
    state: OperatingState,
    positions: dict[str, Position],
) -> Phase:
    """During the I15 latched-but-pending window, decide whether the
    transition plan should be Phase 1 → 3 or Phase 2 → 3.

    DISCRIMINATOR: GBIL holdings.
        GBIL is the Phase 2 fixed-income position. It is NOT present
        in Phase 1 (which holds PYLD + JPIE for FI) and NOT present in
        Phase 3 (Phase 3 returns to the PYLD/JPIE pair, per §7.2.3).
        It only exists in Phase 2.

        Therefore:
          - GBIL position with material value → prior phase was 2
            → build Phase 2 → 3 plan (liquidate GBIL to zero).
          - No GBIL position (or zero/negligible value) → prior phase
            was 1 → build Phase 1 → 3 plan.

    "Material value" is anything above the position residual floor
    (which is the Phase 1→2 leftover threshold for PYLD/JPIE — but
    Phase 1→2 never created a GBIL residual; it bought into GBIL,
    so a present GBIL position will be much larger than the residual
    floor). A defensive threshold of 1 cent catches any GBIL holding.
    """
    gbil = positions.get("GBIL")
    if gbil is not None and gbil.market_value > Decimal("0.01"):
        return Phase.PHASE_2
    return Phase.PHASE_1


def _attempt_withdrawal(
    state: OperatingState,
    inputs: DecisionInputs,
    plan: Plan,
    alerts: list[AlertEntry],
) -> tuple[OperatingState, Plan, list[AlertEntry]]:
    """Compute and append the WithdrawalEntry. Handles capacity-
    exhausted and internal-consistency-violation outcomes."""
    ruleset = inputs.ruleset
    growth, fi, weights = _active_phase_classification(state.phase, ruleset)
    monthly = current_monthly_withdrawal(
        state.phase, state.schedule_state, inputs.today.year)
    if monthly <= 0:
        return state, plan, alerts

    w_inputs = WithdrawalInputs(
        phase=state.phase,
        cb_state=state.cb_machine.state,
        scheduled_monthly=monthly,
        current_portfolio_dollars=_core_portfolio_value(inputs.positions, weights),
        current_year=inputs.today.year,
        today=inputs.today,
        positions=inputs.positions,
        target_weights=weights,
        growth_symbols=growth,
        fi_symbols=fi,
        buffer_symbol="SGOV",
        position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
        phase3_monthly_payment_ceiling_rate=ruleset.phase3_monthly_payment_ceiling_rate,
        phase3_dollar_ceiling_base_dollars=ruleset.phase3_dollar_ceiling_base_dollars,
        phase3_dollar_ceiling_base_year=ruleset.phase3_dollar_ceiling_base_year,
        inflation_rate=ruleset.inflation_rate,
        ach_destination=ruleset.ach_destination,
        scheduled_ach_date=inputs.today,
    )
    try:
        decision = decide_withdrawal(inputs=w_inputs, cycle_id=inputs.cycle_id)
    except WithdrawalCapacityExhausted:
        # Set the flag; emit the Critical alert.
        state = state.model_copy(update={"withdrawal_capacity_exhausted": True})
        alerts.append(build_capacity_exhausted_alert(
            scheduled_monthly=monthly,
            portfolio_dollars=w_inputs.current_portfolio_dollars,
        ))
        return state, plan, alerts
    except WithdrawalDecisionError:
        # Internal consistency: initiate hard-broke pause.
        from failure_handler import initiate_pause
        from state_model import PauseReason
        ip = initiate_pause(
            current=state.operational_pause,
            reason=PauseReason.INTERNAL_CONSISTENCY_VIOLATION,
            now=inputs.now,
            consecutive_escalation_count=ruleset.pause_consecutive_escalation_count,
            detail_context={
                "subsystem": "withdrawal",
                "cycle_id": inputs.cycle_id,
            },
        )
        state = state.model_copy(update={"operational_pause": ip.new_pause})
        alerts.append(ip.alert)
        if ip.escalation_alert is not None:
            alerts.append(ip.escalation_alert)
        return state, plan, alerts

    if decision.entry is not None:
        plan.add(decision.entry)
        # withdrawal_executed alert (Info)
        alerts.append(AlertEntry(
            alert_id="withdrawal_executed",
            context={
                "amount": str(decision.entry.total_dollar_amount),
                "cb_state": state.cb_machine.state.value,
            },
        ))
    if decision.ceiling_alert is not None:
        alerts.append(decision.ceiling_alert)
    if decision.cascade_growth_alert is not None:
        alerts.append(decision.cascade_growth_alert)
    return state, plan, alerts


def _evaluate_cash_buffer(state: OperatingState,
                          inputs: DecisionInputs) -> CashBufferDecision:
    ruleset = inputs.ruleset
    growth, fi, weights = _active_phase_classification(state.phase, ruleset)
    cash = inputs.account_summary.total_cash_value
    # total_cash_value (settled + unsettled) is the right figure for
    # cash-bucket math per AccountSummary's own naming-note docstring
    # (§7.7). Using settled_cash would cause the buffer to over-refill
    # by ignoring SELL proceeds that are about to settle.
    # Was: inputs.account_summary.cash_balance — a non-existent field.
    # The bug was latent because no scenario exercised this path until
    # the IRAPM harness drove a full weekly cycle (2026-05-13 session).
    portfolio_total = _portfolio_total(inputs.positions, "SGOV")
    cb_inputs = CashBufferInputs(
        phase=state.phase,
        cb_state=state.cb_machine.state,
        cb2_entry_conditions=set(state.cb_machine.cb2_entry_conditions),
        cash_balance_dollars=cash,
        cash_target_dollars=state.buffer_state.cash_target_dollars,
        cash_buffer_tolerance_dollars=ruleset.cash_buffer_tolerance_dollars,
        portfolio_total_dollars=portfolio_total,
        positions=inputs.positions,
        target_weights=weights,
        growth_symbols=growth,
        fi_symbols=fi,
        buffer_symbol="SGOV",
        signal_available=inputs.lookback.status == LookbackStatus.AVAILABLE,
        signal_value=inputs.lookback.value,
        position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
        large_cash_deployment_threshold_dollars=ruleset.large_cash_deployment_threshold_dollars,
        large_cash_deployment_threshold_rate=ruleset.large_cash_deployment_threshold_rate,
        cb2_threshold_rate=ruleset.cb2_threshold_rate,
    )
    return decide_cash_buffer(cb_inputs)


def _evaluate_sgov_refill(state: OperatingState,
                          inputs: DecisionInputs):
    ruleset = inputs.ruleset
    growth, fi, weights = _active_phase_classification(state.phase, ruleset)
    buffer_value = _buffer_value(inputs.positions, "SGOV")
    refill_inputs = SGOVRefillInputs(
        phase=state.phase,
        cb_state=state.cb_machine.state,
        now=inputs.now,
        buffer_target_dollars=state.buffer_state.sgov_target_dollars,
        monthly_refill_rate_dollars=state.buffer_state.monthly_refill_rate_dollars,
        buffer_current_value=buffer_value,
        refill_delay_started_at=state.buffer_state.refill_delay_started_at,
        last_refill_at=state.buffer_state.last_refill_at,
        positions=inputs.positions,
        growth_symbols=growth,
        buffer_symbol="SGOV",
        target_weights=weights,
        position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
        sgov_refill_post_recovery_delay_days=ruleset.sgov_refill_post_recovery_delay_days,
    )
    return decide_buffer_refill(refill_inputs)


def _evaluate_rebalance(
    state: OperatingState,
    inputs: DecisionInputs,
) -> tuple[OperatingState, list[PlanEntry], list[AlertEntry]]:
    """Dispatch to the right rebalance flavor based on phase."""
    ruleset = inputs.ruleset
    orders: list[PlanEntry] = []
    alerts: list[AlertEntry] = []
    growth, fi, weights = _active_phase_classification(state.phase, ruleset)

    if state.phase == Phase.PHASE_2:
        # Phase 2 has two flavors: opportunistic swing (each cycle) and
        # semi-annual reallocation (date-gated).
        in_grace = state.phase3_grace_window_start is not None
        swing_inputs = Phase2SwingInputs(
            cb_state=state.cb_machine.state,
            swing_state=state.phase2_opportunistic.state,
            deploy_pending=state.phase2_opportunistic.deploy_pending,
            recover_pending=state.phase2_opportunistic.recover_pending,
            signal_available=inputs.lookback.status == LookbackStatus.AVAILABLE,
            signal_value=inputs.lookback.value,
            in_phase3_grace_window=in_grace,
            positions=inputs.positions,
            steady_weights=ruleset.phase2_steady_target_weights.weights,
            deployed_weights=ruleset.phase2_deployed_target_weights.weights,
            growth_symbols=growth,
            fi_symbols=fi,
            phase2_opportunistic_trigger_rate=ruleset.phase2_opportunistic_trigger_rate,
            phase2_opportunistic_recovery_rate=ruleset.phase2_opportunistic_recovery_rate,
            confirmation_window_weeks=ruleset.confirmation_window_weeks,
            position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
        )
        swing_result = decide_phase2_swing(swing_inputs, now=inputs.now)
        orders.extend(swing_result.orders)
        if swing_result.deploy_fired:
            alerts.append(AlertEntry(
                alert_id="phase2_opportunistic_deploy",
                context={"to": "deployed"},
            ))
        if swing_result.recover_fired:
            alerts.append(AlertEntry(
                alert_id="phase2_opportunistic_recover",
                context={"to": "steady"},
            ))
        # Update phase2_opportunistic state
        from state_model import Phase2Opportunistic
        state = state.model_copy(update={
            "phase2_opportunistic": Phase2Opportunistic(
                state=swing_result.new_swing_state,
                deploy_pending=swing_result.new_deploy_pending,
                recover_pending=swing_result.new_recover_pending,
            )
        })

        # Semi-annual reallocation
        if inputs.is_phase2_reallocation_day:
            sa_inputs = Phase2SemiAnnualInputs(
                swing_state=state.phase2_opportunistic.state,
                positions=inputs.positions,
                steady_weights=ruleset.phase2_steady_target_weights.weights,
                deployed_weights=ruleset.phase2_deployed_target_weights.weights,
                growth_symbols=growth,
                fi_symbols=fi,
                position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
            )
            sa_orders = decide_phase2_semi_annual(sa_inputs)
            orders.extend(sa_orders)
            if sa_orders:
                alerts.append(AlertEntry(
                    alert_id="phase2_semi_annual_reallocation",
                    context={"orders_count": str(len(sa_orders))},
                ))
        return state, orders, alerts

    # Phase 1 or 3: standard 5/25 rebalance, only in CB_INACTIVE
    if state.cb_machine.state != CBState.CB_INACTIVE:
        return state, orders, alerts
    sr_inputs = StandardRebalanceInputs(
        phase=state.phase,
        cb_state=state.cb_machine.state,
        positions=inputs.positions,
        target_weights=weights,
        growth_symbols=growth,
        fi_symbols=fi,
        rebalance_absolute_threshold_rate=ruleset.rebalance_absolute_threshold_rate,
        rebalance_relative_threshold_rate=ruleset.rebalance_relative_threshold_rate,
        position_residual_minimum_dollars=ruleset.position_residual_minimum_dollars,
    )
    sr_result = decide_standard_rebalance(sr_inputs)
    orders.extend(sr_result.orders)
    return state, orders, alerts


__all__ = [
    "DecisionInputs",
    "DecisionOutput",
    "decide",
]
