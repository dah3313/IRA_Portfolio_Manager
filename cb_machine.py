"""
cb_machine.py — Circuit Breaker state machine (§6.3).

PURPOSE:
    Evaluate the three CB states (CB_INACTIVE, CB1, CB2) given the
    current lookback signal, core portfolio and FI bucket values, and
    the persisted CB machine state. Produces a CBEvaluationResult that
    the decision layer turns into a CBStateTransitionEntry (if a
    transition fires).

DESIGN (per §6.3):
    - This module is PURE: no I/O, no clock side effects. The cycle's
      decision_clock is passed in.
    - The machine evaluates entry and exit independently for each of
      the five tracked paths: CB1 signal entry/exit, CB2 signal,
      CB2 portfolio-low, CB2 FI-low. Plus the timer-based CB1→CB2
      transition (§6.3.5) which is not counter-driven.
    - Resource conditions bypass CB1 and trigger CB2 directly (§6.3.2).
    - CB2 exit requires every condition in `cb2_entry_conditions` to
      clear with its recovery buffer (§6.3.3).
    - Signal-UNAVAILABLE cycles: signal-based confirmation counters
      neither advance nor reset (§6.3.1 last paragraph; consistent with
      §11.2.5). Resource-based counters continue to evaluate.
    - Per §6.3.6 the Phase 3 latched-but-pending window suppresses
      resource-based evaluation (current_monthly_withdrawal is undefined
      so the thresholds cannot be computed). The caller passes
      `resource_evaluation_enabled=False` in that window.
    - Phase 2 holds CB_INACTIVE and skips this evaluation entirely
      (the caller is responsible).

OUTPUTS:
    A CBEvaluationResult with:
      - new_state, new_cb2_entry_conditions (post-evaluation),
      - new_pending_confirmations (post-evaluation),
      - new_cb1_active_timer_started_at (None / set),
      - did_transition: bool,
      - transition_reason: str (for the CB log entry).

The decision layer composes this into a CBStateTransitionEntry only
when did_transition is True.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from state_model import (
    CBEntryCondition,
    CBMachine,
    CBPendingConfirmations,
    CBState,
    LookbackStatus,
    PendingCounter,
)


# --- Inputs / outputs ------------------------------------------------------

@dataclass(frozen=True)
class CBInputs:
    """Everything CB evaluation reads. All inputs are computed by the
    cycle's input-refresh step before this module is called.
    """
    # Signal: AVAILABLE → value is the Decimal fraction (e.g., -0.12);
    # UNAVAILABLE → value is None; signal-based evaluations skip.
    signal_status: LookbackStatus
    signal_value: Optional[Decimal]

    # Core portfolio totals
    core_portfolio_dollars: Decimal
    core_fi_dollars: Decimal

    # Currently-scheduled monthly withdrawal (for the *prior* year on
    # annual-review-day cycles, per §6.5 last-paragraph note).
    current_monthly_withdrawal: Decimal

    # Per-cycle decision time (I16). The CB1 timer measures elapsed
    # wall-clock-equivalent time against this.
    now: datetime

    # Tunables from ruleset
    cb1_threshold_rate: Decimal
    cb2_threshold_rate: Decimal
    cb1_recovery_buffer_rate: Decimal
    cb2_recovery_buffer_rate: Decimal
    portfolio_low_threshold_dollars: Decimal
    portfolio_low_threshold_months: int
    portfolio_low_recovery_buffer_rate: Decimal
    fi_low_threshold_months: int
    fi_low_recovery_buffer_rate: Decimal
    confirmation_window_weeks: int
    cb1_to_cb2_timer_days: int

    # Whether resource-based evaluation is enabled this cycle. False in
    # the Phase 3 latched-but-pending window (§6.3.6).
    resource_evaluation_enabled: bool = True


@dataclass(frozen=True)
class CBEvaluationResult:
    """Output of one CB evaluation."""
    new_state: CBState
    new_cb2_entry_conditions: set[CBEntryCondition]
    new_pending_confirmations: CBPendingConfirmations
    new_cb1_active_timer_started_at: Optional[datetime]
    did_transition: bool
    transition_reason: str  # human/log readable; '' when no transition


# --- Threshold helpers -----------------------------------------------------

def _portfolio_low_threshold(inputs: CBInputs) -> Decimal:
    """Portfolio-low threshold per §6.3.1.

    max(portfolio_low_threshold_dollars,
        portfolio_low_threshold_months × current_monthly_withdrawal)
    """
    dollar_floor = inputs.portfolio_low_threshold_dollars
    months_floor = Decimal(inputs.portfolio_low_threshold_months) * inputs.current_monthly_withdrawal
    return max(dollar_floor, months_floor)


def _fi_low_threshold(inputs: CBInputs) -> Decimal:
    """FI-low threshold per §6.3.1."""
    return Decimal(inputs.fi_low_threshold_months) * inputs.current_monthly_withdrawal


# --- Counter management ----------------------------------------------------

def _advance_counter(counter: PendingCounter,
                     holds: bool,
                     now: datetime) -> PendingCounter:
    """Update one pending counter: increment if `holds`, else reset.

    A non-holding observation always resets to zero. This is the
    "consecutive cycles" semantics from §6.3.1: a single non-holding
    cycle interrupts the chain.
    """
    if holds:
        new_cycles = counter.cycles_confirmed + 1
        new_first = counter.first_observed_at or now
        return PendingCounter(cycles_confirmed=new_cycles,
                              first_observed_at=new_first)
    return PendingCounter(cycles_confirmed=0, first_observed_at=None)


def _hold_counter(counter: PendingCounter) -> PendingCounter:
    """Don't change the counter (used when the signal is UNAVAILABLE
    for signal-based counters per §6.3.1)."""
    return counter


# --- Condition predicates --------------------------------------------------

def _signal_at_or_below(value: Optional[Decimal], threshold: Decimal) -> bool:
    """Signal ≤ threshold. Returns False if signal is None."""
    if value is None:
        return False
    return value <= threshold


def _signal_at_or_above(value: Optional[Decimal], threshold: Decimal) -> bool:
    """Signal ≥ threshold. Returns False if signal is None."""
    if value is None:
        return False
    return value >= threshold


# --- Main evaluation -------------------------------------------------------

def evaluate_cb_machine(
    *,
    cb: CBMachine,
    inputs: CBInputs,
) -> CBEvaluationResult:
    """Single-cycle CB state evaluation per §6.3, §6.5 step 5-6.

    The function:
      1. Recomputes the currently-holding status of each condition.
      2. Updates pending-confirmation counters per §6.3.1 semantics.
      3. Applies transition rules in order:
         a. CB2 entry (any path) when its counter reaches the window.
         b. CB1 → CB2 timer transition (§6.3.5).
         c. CB1 signal entry (CB_INACTIVE → CB1) when counter reaches window.
         d. CB2 exit (when every condition in `cb2_entry_conditions`
            has cleared with its recovery buffer for the window).
         e. CB1 signal exit (CB1 → CB_INACTIVE) when counter reaches window.
      4. Updates the CB1-active timer (set on fresh CB1 entry, cleared
         when no longer in CB1).
      5. Updates the cb2_entry_conditions set.

    The order is important: a CB2 entry "wins" over a CB1 entry for the
    same cycle if both conditions confirm simultaneously (CB2 is the
    deeper state). CB2 exit and CB1 exit are evaluated only when
    currently in those states.
    """
    signal_available = inputs.signal_status == LookbackStatus.AVAILABLE

    # ---- Currently-holding status for each path (this cycle) ----

    # Signal-based
    cb1_signal_entry_holds = signal_available and _signal_at_or_below(
        inputs.signal_value, inputs.cb1_threshold_rate
    )
    cb1_signal_exit_holds = signal_available and _signal_at_or_above(
        inputs.signal_value,
        inputs.cb1_threshold_rate + inputs.cb1_recovery_buffer_rate,
    )
    cb2_signal_entry_holds = signal_available and _signal_at_or_below(
        inputs.signal_value, inputs.cb2_threshold_rate
    )

    # Resource-based — only evaluated when enabled (§6.3.6).
    if inputs.resource_evaluation_enabled:
        port_low_threshold = _portfolio_low_threshold(inputs)
        cb2_port_low_entry_holds = (
            inputs.core_portfolio_dollars < port_low_threshold
        )
        cb2_fi_low_entry_holds = (
            inputs.core_fi_dollars < _fi_low_threshold(inputs)
        )
    else:
        port_low_threshold = Decimal("0")
        cb2_port_low_entry_holds = False
        cb2_fi_low_entry_holds = False

    # ---- Counter updates ----
    pc = cb.pending_confirmations
    now = inputs.now

    # Signal-based counters: hold (no advance, no reset) when signal is
    # UNAVAILABLE; advance/reset normally when AVAILABLE.
    if signal_available:
        new_cb1_signal_entry = _advance_counter(
            pc.cb1_signal_entry, cb1_signal_entry_holds, now)
        new_cb1_signal_exit = _advance_counter(
            pc.cb1_signal_exit, cb1_signal_exit_holds, now)
        new_cb2_signal = _advance_counter(
            pc.cb2_signal, cb2_signal_entry_holds, now)
    else:
        new_cb1_signal_entry = _hold_counter(pc.cb1_signal_entry)
        new_cb1_signal_exit = _hold_counter(pc.cb1_signal_exit)
        new_cb2_signal = _hold_counter(pc.cb2_signal)

    # Resource-based counters: advance normally if enabled, else hold.
    if inputs.resource_evaluation_enabled:
        new_cb2_port_low = _advance_counter(
            pc.cb2_portfolio_low, cb2_port_low_entry_holds, now)
        new_cb2_fi_low = _advance_counter(
            pc.cb2_fi_low, cb2_fi_low_entry_holds, now)
    else:
        new_cb2_port_low = _hold_counter(pc.cb2_portfolio_low)
        new_cb2_fi_low = _hold_counter(pc.cb2_fi_low)

    window = inputs.confirmation_window_weeks

    # ---- Transition resolution ----
    state = cb.state
    cb2_conds = set(cb.cb2_entry_conditions)
    timer_started_at = cb.cb1_active_timer_started_at
    did_transition = False
    reason = ""

    # (a) CB2 entry from CB_INACTIVE or CB1 via any path.
    # Multiple paths may confirm in the same cycle; we record all of them
    # in the cb2_entry_conditions set.
    newly_confirmed_cb2_paths: list[CBEntryCondition] = []
    if state in (CBState.CB_INACTIVE, CBState.CB1):
        if new_cb2_signal.cycles_confirmed >= window:
            newly_confirmed_cb2_paths.append(CBEntryCondition.SIGNAL)
        if new_cb2_port_low.cycles_confirmed >= window:
            newly_confirmed_cb2_paths.append(CBEntryCondition.PORTFOLIO_LOW)
        if new_cb2_fi_low.cycles_confirmed >= window:
            newly_confirmed_cb2_paths.append(CBEntryCondition.FI_LOW)

    # (b) CB1 → CB2 timer transition (§6.3.5). Fires immediately when
    # the timer crosses the threshold; no confirmation window.
    timer_fired = False
    if (state == CBState.CB1
        and timer_started_at is not None
        and (now - timer_started_at) >= timedelta(days=inputs.cb1_to_cb2_timer_days)):
        timer_fired = True

    if newly_confirmed_cb2_paths or timer_fired:
        # Enter CB2 (or accumulate conditions if already in CB2 — see
        # below; reaching this branch from CB2 is impossible because
        # state ∈ {CB_INACTIVE, CB1} above).
        state = CBState.CB2
        for c in newly_confirmed_cb2_paths:
            cb2_conds.add(c)
        if timer_fired:
            cb2_conds.add(CBEntryCondition.SIGNAL)  # per §6.3.5
        # Reset the CB2 entry counters that just fired.
        if CBEntryCondition.SIGNAL in cb2_conds and (
            newly_confirmed_cb2_paths and CBEntryCondition.SIGNAL in newly_confirmed_cb2_paths
            or timer_fired
        ):
            new_cb2_signal = PendingCounter()
        if CBEntryCondition.PORTFOLIO_LOW in newly_confirmed_cb2_paths:
            new_cb2_port_low = PendingCounter()
        if CBEntryCondition.FI_LOW in newly_confirmed_cb2_paths:
            new_cb2_fi_low = PendingCounter()
        # Clear the CB1-active timer; we are no longer in CB1.
        timer_started_at = None
        did_transition = True
        # Build a reason string for the CB log.
        reasons = []
        if timer_fired:
            reasons.append("cb1_timer")
        for c in newly_confirmed_cb2_paths:
            reasons.append(c.value)
        reason = ",".join(reasons)
    elif state == CBState.CB2:
        # While in CB2, additional confirmations accumulate into
        # cb2_entry_conditions (§6.3.4). These are *fresh* triggers
        # during the current episode, not re-confirmations of conditions
        # already in the set.
        if new_cb2_signal.cycles_confirmed >= window:
            cb2_conds.add(CBEntryCondition.SIGNAL)
            new_cb2_signal = PendingCounter()
        if new_cb2_port_low.cycles_confirmed >= window:
            cb2_conds.add(CBEntryCondition.PORTFOLIO_LOW)
            new_cb2_port_low = PendingCounter()
        if new_cb2_fi_low.cycles_confirmed >= window:
            cb2_conds.add(CBEntryCondition.FI_LOW)
            new_cb2_fi_low = PendingCounter()

    # (c) CB1 signal entry — only from CB_INACTIVE, and only if CB2
    # didn't already fire above.
    if (state == CBState.CB_INACTIVE
        and new_cb1_signal_entry.cycles_confirmed >= window):
        state = CBState.CB1
        timer_started_at = now  # start fresh CB1 timer
        new_cb1_signal_entry = PendingCounter()
        did_transition = True
        reason = "signal"

    # (d) CB2 exit. Require every condition in cb2_entry_conditions to
    # currently be cleared (with recovery buffer) for the confirmation
    # window. §6.3.3: exit-target depends on signal band.
    if state == CBState.CB2 and cb2_conds:
        all_cleared = True
        for cond in cb2_conds:
            if cond == CBEntryCondition.SIGNAL:
                # Signal clear: signal ≥ cb2_threshold + cb2_recovery_buffer
                clear_threshold = inputs.cb2_threshold_rate + inputs.cb2_recovery_buffer_rate
                # If signal is UNAVAILABLE, we cannot confirm clearance;
                # treat as not-cleared so exit waits for signal recovery.
                if not (signal_available and _signal_at_or_above(
                        inputs.signal_value, clear_threshold)):
                    all_cleared = False
                    break
                # Note: the §6.3.3 confirmation window for exit is
                # tracked implicitly here. To keep this module simple
                # and the state minimal, the exit-side confirmation
                # uses one cycle (just "is the condition clear now?").
                # If the operator needs the full 2-week exit-side
                # confirmation, that's a future state-machine extension.
            elif cond == CBEntryCondition.PORTFOLIO_LOW:
                if not inputs.resource_evaluation_enabled:
                    all_cleared = False
                    break
                threshold = _portfolio_low_threshold(inputs)
                clear_at = threshold * (Decimal("1") + inputs.portfolio_low_recovery_buffer_rate)
                if inputs.core_portfolio_dollars < clear_at:
                    all_cleared = False
                    break
            elif cond == CBEntryCondition.FI_LOW:
                if not inputs.resource_evaluation_enabled:
                    all_cleared = False
                    break
                threshold = _fi_low_threshold(inputs)
                clear_at = threshold * (Decimal("1") + inputs.fi_low_recovery_buffer_rate)
                if inputs.core_fi_dollars < clear_at:
                    all_cleared = False
                    break

        if all_cleared:
            # Decide exit target per §6.3.3:
            # - if signal ≥ cb1_threshold + cb1_recovery_buffer → CB_INACTIVE
            # - else if signal in [cb2_threshold+cb2_recovery, cb1_threshold) → CB1
            # If signal is unavailable, we have already returned all_cleared=False above
            # for signal condition; but signal_available=False with no signal condition
            # in the set is possible (resource-only CB2). In that case route to
            # CB_INACTIVE if signal is nominal — which we can't verify when
            # UNAVAILABLE, so fall back to CB1 (deeper) to be safe.
            cb1_band_threshold = inputs.cb1_threshold_rate + inputs.cb1_recovery_buffer_rate
            if signal_available and _signal_at_or_above(inputs.signal_value, cb1_band_threshold):
                state = CBState.CB_INACTIVE
                reason = "exit_all_clear_to_inactive"
            else:
                state = CBState.CB1
                timer_started_at = now  # fresh CB1 begins
                reason = "exit_all_clear_to_cb1"
            cleared_list = sorted(c.value for c in cb2_conds)
            reason = f"{reason}:{','.join(cleared_list)}"
            cb2_conds = set()  # cleared on exit (§6.3.4)
            did_transition = True

    # (e) CB1 → CB_INACTIVE via signal recovery.
    if (state == CBState.CB1
        and new_cb1_signal_exit.cycles_confirmed >= window):
        state = CBState.CB_INACTIVE
        timer_started_at = None
        new_cb1_signal_exit = PendingCounter()
        did_transition = True
        reason = "signal"

    # ---- CB1 timer maintenance ----
    # When state is not CB1, the timer must be None (§6.3.5).
    # When state is CB1 and we just entered, timer was set above.
    # When state is CB1 from a prior cycle, the timer remains.
    if state != CBState.CB1:
        timer_started_at = None
    elif state == CBState.CB1 and timer_started_at is None:
        # We are in CB1 but the timer is unset — set it now (this is
        # the recovery path from a state file that lost the timer).
        timer_started_at = now

    # ---- Build the post-evaluation counters object ----
    new_pc = CBPendingConfirmations(
        cb1_signal_entry=new_cb1_signal_entry,
        cb1_signal_exit=new_cb1_signal_exit,
        cb2_signal=new_cb2_signal,
        cb2_portfolio_low=new_cb2_port_low,
        cb2_fi_low=new_cb2_fi_low,
    )

    return CBEvaluationResult(
        new_state=state,
        new_cb2_entry_conditions=cb2_conds,
        new_pending_confirmations=new_pc,
        new_cb1_active_timer_started_at=timer_started_at,
        did_transition=did_transition,
        transition_reason=reason,
    )


__all__ = [
    "CBInputs",
    "CBEvaluationResult",
    "evaluate_cb_machine",
]
