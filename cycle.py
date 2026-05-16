"""
cycle.py — Top-level cycle orchestrator (§6.5, §9.6).

PURPOSE:
    Compose the per-cycle execution pipeline:
      1. Acquire CycleAttempt (fresh or restart).
      2. Input refresh: connect broker, fetch positions/account,
         compute lookback signal, read combined token observation.
      3. Decide: call decision_layer.decide().
      4. Execute: call action_layer.execute_plan().
      5. Persist: save new operating state, append to cycle log.
      6. Complete cycle attempt.

    Two cycle types are supported:
      - weekly: full decision pipeline.
      - daily-token: token observation only; updates the token file
        and dispatches token alerts. No CB/withdrawal/rebalance work.

DESIGN:
    - The cycle driver knows the CALENDAR rules (when to withdraw,
      when annual review fires, when Phase 2 reallocation fires); it
      passes the resulting flags into decision_layer.
    - The driver is the one place that owns the broker session: input
      refresh happens before decision; action layer reopens a fresh
      session via the broker_session context manager. (We could share
      one session, but keeping connect/disconnect symmetric is simpler
      and matches the per-phase atomicity intent.)
    - All persistent state writes are atomic via persistence.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from action_layer import execute_plan
from alerter import Alerter
from broker_protocol import Broker, BrokerError
from broker_types import Position
from clock import Clock
from cycle_attempt import CycleType, begin_cycle, complete_cycle
from decision_layer import DecisionInputs, decide
import event_log
from lookback_signal import compute_synthetic_growth_lookback
from persistence import (
    Paths,
    append_cycle_log,
    append_token_check,
    load_operating_state,
    save_operating_state,
    save_token_observation,
)
from plan_model import AlertEntry, EntryKind
import report
from ruleset_model import Ruleset
from tokens import (
    CombinedTokenState,
    TokenDetector,
    TokenObservation,
    combine_observations,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Time helpers
# =============================================================================

# Eastern Time — IRAPM treats the simulation's naive "wall-clock" as ET
# (matching market hours), then converts to UTC for the event log. Same
# modeling convention as AdvancingClock.now_et / now_utc in clock.py.
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")


def _to_aware_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC for event log emission.

    EVENT_LOG_SPEC §3.2 mandates tz-aware UTC for every timestamp.
    The Clock seam's now() returns naive datetimes by legacy contract
    (see clock.py docstring) and cycle_attempt's decision_clock may
    be naive (fresh) or aware (restart, persisted from prior attempt).
    This helper handles both cases: naive → treat as ET, convert to
    UTC; aware → convert to UTC.

    Used at cycle.py's event log emission boundary so the writer's
    tz-aware enforcement doesn't reject otherwise-valid timestamps.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ET).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


# =============================================================================
# Calendar helpers
# =============================================================================

def _is_scheduled_withdrawal_day(today: date) -> bool:
    """The monthly withdrawal step runs on the latest Wednesday on or
    before (15th - 4 business days). For simplicity here we use the
    Wednesday-of-month that is <= the 15th, which lines up with the
    IBKR ACH 2-business-day-settlement convention per §9.6.1.

    Concrete rule used: today is a Wednesday AND today.day is in
    [9, 15]. A 7-day window guarantees exactly one Wednesday falls
    within it regardless of where the 1st sits in the week, so this
    rule fires exactly once per month.

    History: an earlier version used [10, 15], which excluded day 9.
    When the 1st of a month was a Tuesday, the only candidate Wed
    landed on day 9 and the withdrawal silently skipped that month
    entirely. Over the 2005-2025 baseline run this caused ~35 missed
    monthly withdrawals (205 actual vs ~240 expected).

    The spec leaves operator-tunable precision here; this simple rule
    satisfies the "monthly cadence on a Wednesday near the 15th"
    description in §9.6.1 and can be refined via ruleset later.
    """
    if today.weekday() != 2:  # Wed = 2
        return False
    return 9 <= today.day <= 15


def _is_annual_review_day(today: date, annual_review_date_str: str) -> bool:
    """Annual review fires on the first Wednesday cycle on-or-after
    MM-DD each year. Cycles only run on Wednesdays, so a date-equality
    rule would miss most years (MM-DD is a Wednesday only ~1/7 of
    years). This implementation fires on exactly one Wednesday per
    year: the first Wed in the [MM-DD, MM-DD+6] window.

    Rule: today is a Wednesday AND this year's MM-DD (call it T) is
    in the 7-day window ending at today: T <= today <= T + 6 days.
    Equivalently, today - 6 <= T <= today. Since the window is exactly
    7 days long, exactly one Wednesday falls in it for any T.

    The cycle driver's broader machinery (year-already-reviewed checks
    via cycle log and schedule_state) handles idempotency if the
    function is consulted multiple times in the same window.

    History: an earlier version used `today.month == M and today.day
    == D`, which only fired when the cycle date exactly equaled MM-DD.
    Over the 2005-2025 baseline run this produced only 2 annual
    reviews vs the expected ~20.
    """
    if today.weekday() != 2:  # Wed = 2
        return False
    month, day = (int(p) for p in annual_review_date_str.split("-"))
    try:
        target = date(today.year, month, day)
    except ValueError:
        # Defensive: malformed config produces a no-fire result rather
        # than crashing the cycle. The ruleset validator should reject
        # invalid MM-DD strings at startup, so this branch should be
        # unreachable in practice.
        return False
    delta_days = (today - target).days
    return 0 <= delta_days <= 6


def _is_phase2_reallocation_day(today: date,
                                reallocation_dates: list[str]) -> bool:
    """True iff `today` matches any MM-DD in `reallocation_dates`."""
    for mmdd in reallocation_dates:
        m, d = (int(p) for p in mmdd.split("-"))
        if today.month == m and today.day == d:
            return True
    return False


# =============================================================================
# Inputs / outputs
# =============================================================================

@dataclass
class CycleConfig:
    """Per-cycle dependencies. Constructed once per process; reused
    across cycles."""
    ruleset: Ruleset
    broker: Broker
    alerter: Alerter
    token_detector: TokenDetector
    paths: Paths
    clock: Clock
    box_id: str
    client_id: int   # broker client id (e.g., 11/12 per box)
    expected_account_id: Optional[str] = None
    """If set, the action layer's pre-flight check verifies the broker
    reports this account ID before placing any orders. Mismatch is a
    hard-broke condition. None disables the check (dev/sim only)."""


# =============================================================================
# Weekly cycle
# =============================================================================

def _dispatch_and_log_alert(
    alerter,
    entry: "AlertEntry",
    paths: "Paths",
    cycle_id: Optional[str],
    default_context: dict[str, str],
    now: datetime,
    now_aware: datetime,
) -> None:
    """Dispatch an alert and emit the corresponding alert_emitted event.

    Wraps alerter.dispatch() so every call site here gets the event-log
    emission for free, with consistent error-suppression (matches the
    historical behavior — alerter failures don't break a cycle).

    Per EVENT_LOG_SPEC §4.13, the event is emitted regardless of whether
    email/SMS channels succeed — the DispatchOutcome's per-channel
    booleans are recorded in the payload. If dispatch() itself raises
    (rare: an alerter implementation bug), we log a warning and skip
    the event emission for this alert — there's nothing to record.
    """
    try:
        outcome = alerter.dispatch(entry, default_context=default_context, now=now)
    except Exception as e:
        logger.warning("alert dispatch failed: %s", e)
        return
    event_log.append_alert_emitted(
        paths,
        cycle_id=cycle_id,
        alert_id=outcome.rendered.alert_id,
        context=dict(entry.context),
        email_ok=outcome.email_ok,
        sms_ok=outcome.sms_ok,
        deduped=outcome.deduped,
        email_error=outcome.email_error,
        sms_error=outcome.sms_error,
        now=now_aware,
        in_sim_timestamp=now_aware,
    )


def run_weekly_cycle(config: CycleConfig) -> None:
    """Run one weekly cycle (the full decision pipeline)."""
    paths = config.paths
    paths.ensure_dirs()

    state = load_operating_state(paths)
    now = config.clock.now()
    # Tz-aware variants for the event log (EVENT_LOG_SPEC §3.2 mandates
    # tz-aware UTC). The legacy `now` stays naive for every existing
    # consumer; `now_aware` and `decision_clock_aware` are used only
    # by event_log.append_* calls in this function.
    now_aware = config.clock.now_utc()

    # 1) Begin (or restart) cycle attempt
    attempt, is_restart = begin_cycle(
        cycle_type=CycleType.WEEKLY,
        box_id=config.box_id,
        client_id=config.client_id,
        now=now,
        path=paths.cycle_attempt_file,
    )
    decision_clock = attempt.decision_clock  # captured per I16
    cycle_id = str(attempt.cycle_uuid)
    today = decision_clock.date()
    # Tz-aware variant of decision_clock for event log emissions.
    # decision_clock may be naive (fresh cycle, from config.clock.now())
    # or aware (restart, persisted from prior attempt — see cycle_attempt.py
    # docstring line 13). Normalize: naive → treat as ET, convert to UTC;
    # aware → convert to UTC. Same convention AdvancingClock.now_utc uses.
    decision_clock_aware = _to_aware_utc(decision_clock)

    logger.info("starting weekly cycle %s (restart=%s, decision_clock=%s)",
                cycle_id, is_restart, decision_clock.isoformat())

    # Capture wall-clock start for cycle_completed.duration_ms (§4.2).
    # Tz-aware so duration math composes cleanly with cycle_end_wall.
    cycle_start_wall = now_aware

    # Emit cycle_started (EVENT_LOG_SPEC §4.1). Done BEFORE input
    # refresh so a broker failure still leaves a "we attempted this
    # cycle" record; absence of cycle_completed afterward is the
    # marker that the cycle raised before completion (§4.2).
    event_log.append_cycle_started(
        paths,
        cycle_id=cycle_id,
        cycle_type="weekly",
        is_restart=is_restart,
        box_id=config.box_id,
        client_id=config.client_id,
        now=now_aware,
        in_sim_timestamp=decision_clock_aware,
    )

    # 2) Input refresh: broker
    try:
        config.broker.connect()
        positions_list = config.broker.get_positions()
        positions = {p.symbol: p for p in positions_list}
        account = config.broker.get_account_summary()
    except BrokerError as e:
        logger.error("broker input refresh failed: %s", e)
        # No new state to write; the next cycle will retry.
        return
    finally:
        try:
            config.broker.disconnect()
        except Exception:
            pass

    # 3) Lookback signal
    growth_symbols = _growth_symbols_for_phase(state.phase, config.ruleset)
    lookback = compute_synthetic_growth_lookback(
        growth_symbols=list(growth_symbols),
        data_dir=_data_dir_for_box(config),
        lookback_window_weeks=config.ruleset.lookback_window_weeks,
        max_staleness_days=config.ruleset.lookback_max_staleness_days,
        min_bar_coverage_rate=config.ruleset.lookback_min_bar_coverage_rate,
        as_of=today,
    )

    # 4) Combined token observation (from current persisted file)
    combined = _read_combined_tokens(config)

    # 5) Date-gated flags
    is_w_day = _is_scheduled_withdrawal_day(today)
    is_ar_day = _is_annual_review_day(
        today, config.ruleset.annual_review_date)
    is_p2_realloc = _is_phase2_reallocation_day(
        today, config.ruleset.phase2_reallocation_dates)

    # 6) CB transition records for the freeze evaluation
    cb_records = []
    if is_ar_day:
        from persistence import read_cb_transitions_for_year
        cb_records = read_cb_transitions_for_year(paths, today.year - 1)

    # 7) Decision
    decision_inputs = DecisionInputs(
        state=state,
        ruleset=config.ruleset,
        now=decision_clock,
        today=today,
        positions=positions,
        account_summary=account,
        combined_tokens=combined,
        lookback=lookback,
        cb_transition_records=cb_records,
        is_scheduled_withdrawal_day=is_w_day,
        is_annual_review_day=is_ar_day,
        is_phase2_reallocation_day=is_p2_realloc,
        cycle_id=cycle_id,
    )
    decision_output = decide(decision_inputs)
    new_state = decision_output.new_state
    plan = decision_output.plan

    # Emit decision_made (EVENT_LOG_SPEC §4.4). Captures the full
    # input set the decision saw plus the resulting plan. The
    # cycle_snapshot from decision_output supplies the dollar
    # totals (computed once during decide(), reused here — no
    # recomputation).
    #
    # plan_entries payload: each entry carries entry_index, kind,
    # and the entry's model_dump as `details`. The spec example
    # shows entry_index/kind/summary/dollar_amount/sources, but
    # plan entry types have heterogeneous shapes (CB transitions
    # have no dollar_amount; alerts have no sources). The kind
    # discriminator tells readers what shape `details` has;
    # forcing a uniform shape would be lossy.
    snap = decision_output.cycle_snapshot
    decision_inputs_payload = {
        "phase": state.phase.value,           # pre-decide state
        "cb_state": state.cb_machine.state.value,
        "income_state": state.income_state.value,
        "lookback_status": lookback.status.value,
        "lookback_value": (str(lookback.value)
                           if lookback.value is not None else None),
        "is_scheduled_withdrawal_day": is_w_day,
        "is_annual_review_day": is_ar_day,
        "is_phase2_reallocation_day": is_p2_realloc,
        "combined_token_state": (
            {
                "phase3_all_removed": combined.phase3_all_removed,
                "stopincome_active_on_both": combined.stopincome_active_on_both,
                "any_unavailable": combined.any_unavailable,
                "counts_match": combined.counts_match,
            }
            if combined is not None else None
        ),
        "total_aum_dollars": str(snap.total_aum) if snap else None,
        "cash_dollars": str(snap.cash) if snap else None,
        "sgov_buffer_dollars": str(snap.sgov_buffer) if snap else None,
        "position_values": (
            {sym: str(d["market_value_dollars"])
             for sym, d in snap.positions.items()}
            if snap else {}
        ),
    }
    plan_entries_payload = [
        {
            "entry_index": idx,
            "kind": entry.kind.value,
            "details": entry.model_dump(mode="json"),
        }
        for idx, entry in enumerate(plan.entries)
    ]
    event_log.append_decision_made(
        paths,
        cycle_id=cycle_id,
        inputs=decision_inputs_payload,
        plan_entries=plan_entries_payload,
        should_skip_action_layer=decision_output.should_skip_action_layer,
        skip_reason=None,  # decision_layer doesn't surface a reason today
        now=now_aware,
        in_sim_timestamp=decision_clock_aware,
    )

    # 8) Lookback signal: persist to state.lookback_signal
    from state_model import LookbackSignal
    new_state = new_state.model_copy(update={
        "lookback_signal": LookbackSignal(
            status=lookback.status,
            value_pct=lookback.value,
            computed_at=decision_clock,
        )
    })

    # 8a) Emit annual_review_completed (§4.10) + state_snapshot (§4.12,
    # trigger="annual_review_completed") when the cycle ran annual review.
    # The AnnualReviewDecision was surfaced on DecisionOutput by decide();
    # cycle.py is the I/O seam where event log writes happen.
    ar_decision = decision_output.annual_review_decision
    if ar_decision is not None:
        # Compute the prior monthly (what would have been paid before
        # this review's update) so the event captures the year-over-year
        # comparison. We re-derive from the pre-review state's schedule
        # (state, not new_state, since new_state already reflects the
        # post-review schedule). For Phase 2 (no-op review) both are equal.
        from schedule_state import current_monthly_withdrawal as _cmw
        from state_model import Phase
        prior_monthly = _cmw(state.phase, state.schedule_state, today.year - 1)
        new_monthly = _cmw(new_state.phase, new_state.schedule_state, today.year)
        cpi_rate = config.ruleset.inflation_rate if state.phase != Phase.PHASE_2 else Decimal("0")

        annual_review_payload = {
            "review_year": today.year,
            "phase_at_review": state.phase.value,
            "cb_freeze_in_effect": ar_decision.freeze_applied,
            "cb1_plus_days_prior_year": ar_decision.cumulative_cb1_plus_days_prior_year,
            # cumulative_cb1_plus_days (lifetime) is not tracked by
            # annual_review.py today; the prior-year count is the field
            # the freeze threshold uses. Emit prior-year value also as
            # cumulative for now; a future enhancement can track lifetime
            # separately if reporting needs it.
            "cumulative_cb1_plus_days": ar_decision.cumulative_cb1_plus_days_prior_year,
            "cpi_rate_applied": str(cpi_rate),
            "prior_withdrawal_dollars": str(prior_monthly),
            "computed_new_withdrawal_dollars": str(new_monthly),
            "binding_constraint": "cpi_freeze" if ar_decision.freeze_applied else None,
            "issued_ach_update": (
                state.phase in (Phase.PHASE_1, Phase.PHASE_3)
                and new_monthly != prior_monthly
            ),
        }
        event_log.append_annual_review_completed(
            paths,
            cycle_id=cycle_id,
            payload=annual_review_payload,
            now=now_aware,
            in_sim_timestamp=decision_clock_aware,
        )
        # state_snapshot per §4.12 trigger="annual_review_completed"
        state_snapshot_payload = {
            "trigger": "annual_review_completed",
            **new_state.model_dump(mode="json"),
        }
        event_log.append_state_snapshot(
            paths,
            cycle_id=cycle_id,
            payload=state_snapshot_payload,
            now=now_aware,
            in_sim_timestamp=decision_clock_aware,
        )

    # 9) Persist the new state BEFORE executing the plan (D-SPEC-9):
    # decision state and plan are durable; execution side effects are
    # not. If execution fails, the new decision state still reflects
    # CB/phase/income transitions evaluated this cycle. This matches
    # §9.4's "state first, then act" ordering — there's no harm in
    # rewriting state again after action_layer returns updated state
    # (most cycles produce no state change at execution time).
    save_operating_state(paths, new_state)

    # 10) Execute plan (if non-empty and not pause-skipped)
    if not decision_output.should_skip_action_layer and not plan.is_empty():
        default_ctx = {
            "cycle_id": cycle_id[:8],
            "box_id": config.box_id,
            "phase": new_state.phase.value,
            "cb_state": new_state.cb_machine.state.value,
            "timestamp": decision_clock.isoformat(),
        }
        result = execute_plan(
            plan=plan,
            state=new_state,
            ruleset=config.ruleset,
            broker=config.broker,
            alerter=config.alerter,
            paths=paths,
            attempt=attempt,
            now=decision_clock,
            expected_account_id=config.expected_account_id,
            default_alert_context=default_ctx,
        )
        # action_layer may have updated state (e.g., Phase 3 activation)
        if result.new_state != new_state:
            new_state = result.new_state
            save_operating_state(paths, new_state)
        # Pre-flight failure handling: external activity -> alert-only;
        # other failures -> operational_pause (cycle driver's call).
        if result.preflight_failure:
            if result.preflight_external_activity:
                # §11.2.15: alert-only, no pause (the cycle's declination
                # to act is the heal per D-SPEC-8).
                _dispatch_and_log_alert(
                    config.alerter,
                    AlertEntry(
                        alert_id="external_activity_overlap",
                        context={"detail": result.preflight_failure},
                    ),
                    paths, cycle_id, default_ctx, decision_clock, now_aware,
                )
            else:
                # Other pre-flight failures (account_id mismatch, broker
                # query failure) -- initiate a hard-broke pause via the
                # failure_handler so the next cycle re-evaluates.
                from failure_handler import initiate_pause
                from state_model import PauseReason
                ip = initiate_pause(
                    current=new_state.operational_pause,
                    reason=PauseReason.BROKER_INCONSISTENCY,
                    now=decision_clock,
                    consecutive_escalation_count=config.ruleset.pause_consecutive_escalation_count,
                    detail_context={"preflight_failure": result.preflight_failure},
                )
                new_state = new_state.model_copy(
                    update={"operational_pause": ip.new_pause}
                )
                save_operating_state(paths, new_state)
                _dispatch_and_log_alert(
                    config.alerter, ip.alert,
                    paths, cycle_id, default_ctx, decision_clock, now_aware,
                )
                if ip.escalation_alert is not None:
                    _dispatch_and_log_alert(
                        config.alerter, ip.escalation_alert,
                        paths, cycle_id, default_ctx, decision_clock, now_aware,
                    )
    else:
        # No plan or pause-skipped: still dispatch alerts (the plan
        # may contain alert entries even in skip mode).
        result = None
        skip_ctx = {
            "cycle_id": cycle_id[:8],
            "box_id": config.box_id,
            "phase": new_state.phase.value,
            "cb_state": new_state.cb_machine.state.value,
            "timestamp": decision_clock.isoformat(),
        }
        # Dispatch alert entries directly
        for entry in plan.entries:
            if entry.kind == EntryKind.ALERT:
                _dispatch_and_log_alert(
                    config.alerter, entry,
                    paths, cycle_id, skip_ctx, decision_clock, now_aware,
                )

    # 11) Cycle log
    append_cycle_log(paths, {
        "cycle_id": cycle_id,
        "cycle_type": "weekly",
        "decision_clock": decision_clock.isoformat(),
        "phase": new_state.phase.value,
        "cb_state": new_state.cb_machine.state.value,
        "income_state": new_state.income_state.value,
        "operational_pause": new_state.operational_pause.paused,
        "withdrawal_capacity_exhausted": new_state.withdrawal_capacity_exhausted,
        "lookback_status": lookback.status.value,
        "lookback_value": str(lookback.value) if lookback.value is not None else None,
        "plan_entry_count": len(plan.entries),
        "is_restart": is_restart,
        "is_scheduled_withdrawal_day": is_w_day,
        "is_annual_review_day": is_ar_day,
        "execution_halted": (result is not None and result.halted_due_to_failure)
            if result is not None else False,
        "halt_reason": (result.halt_reason if result is not None else None),
    })

    # 12) Mark cycle complete
    complete_cycle(attempt, path=paths.cycle_attempt_file)

    # End-of-cycle event log emissions (EVENT_LOG_SPEC §4.2, §4.3,
    # §4.11, §4.12). Capture wall-clock 'after' here for duration_ms;
    # all other data was computed earlier in the cycle.
    cycle_end_wall = config.clock.now_utc()
    duration_ms = int(
        (cycle_end_wall - cycle_start_wall).total_seconds() * 1000
    )

    # Emit cycle_halted (§4.3) BEFORE cycle_completed: §4.3 requires
    # halted to be followed by completed so the started↔completed
    # invariant holds. completed_legs / failed_leg_index are derived
    # from entry_results — count successful entries before the first
    # failure.
    if result is not None and result.halted_due_to_failure:
        completed_legs = 0
        failed_leg_index = -1
        for idx, er in enumerate(result.entry_results):
            if er.success:
                completed_legs += 1
            else:
                failed_leg_index = idx
                break
        event_log.append_cycle_halted(
            paths,
            cycle_id=cycle_id,
            cycle_type="weekly",
            halt_reason=result.halt_reason or "(no reason given)",
            phase_at_halt=new_state.phase.value,
            cb_state_at_halt=new_state.cb_machine.state.value,
            plan_entry_count=len(plan.entries),
            completed_legs=completed_legs,
            failed_leg_index=failed_leg_index,
            now=cycle_end_wall,
            in_sim_timestamp=decision_clock_aware,
        )

    # Emit cycle_completed (§4.2). Always paired with the matching
    # cycle_started; absence after a cycle_started signals "the cycle
    # raised before reaching this point" — that absence is the
    # diagnostic, no synthetic completed event is ever written.
    event_log.append_cycle_completed(
        paths,
        cycle_id=cycle_id,
        cycle_type="weekly",
        phase=new_state.phase.value,
        cb_state=new_state.cb_machine.state.value,
        income_state=new_state.income_state.value,
        operational_pause=new_state.operational_pause.paused,
        withdrawal_capacity_exhausted=new_state.withdrawal_capacity_exhausted,
        lookback_status=lookback.status.value,
        lookback_value=lookback.value,
        plan_entry_count=len(plan.entries),
        is_scheduled_withdrawal_day=is_w_day,
        is_annual_review_day=is_ar_day,
        is_phase2_reallocation_day=is_p2_realloc,
        duration_ms=duration_ms,
        now=cycle_end_wall,
        in_sim_timestamp=decision_clock_aware,
    )

    # Emit portfolio_snapshot (§4.11). AFTER cycle_completed per spec.
    # Uses the cycle_snapshot computed once during decide() — no
    # recomputation here.
    snap = decision_output.cycle_snapshot
    if snap is not None:
        positions_payload = {
            sym: {
                "quantity_shares": str(d["quantity_shares"]),
                "market_price_dollars": str(d["market_price_dollars"]),
                "market_value_dollars": str(d["market_value_dollars"]),
            }
            for sym, d in snap.positions.items()
        }
        event_log.append_portfolio_snapshot(
            paths,
            cycle_id=cycle_id,
            payload={
                "total_aum_dollars": str(snap.total_aum),
                "cash_dollars": str(snap.cash),
                "sgov_buffer_dollars": str(snap.sgov_buffer),
                "fi_bucket_dollars": str(snap.fi_bucket),
                "growth_bucket_dollars": str(snap.growth_bucket),
                "fi_weight": str(snap.fi_weight),
                "growth_weight": str(snap.growth_weight),
                "positions": positions_payload,
            },
            now=cycle_end_wall,
            in_sim_timestamp=decision_clock_aware,
        )

    # Emit state_snapshot (§4.12) if this is the first weekly cycle
    # of a new calendar month (the "monthly heartbeat"). The other
    # three trigger sources (cb_transition, phase_transition,
    # annual_review_completed) emit state_snapshot from their own
    # emission sites; cycle.py owns only the heartbeat.
    #
    # Compare against `state` (the PRE-decide state loaded at the
    # top of this cycle), not new_state — _bump_cycle_marker has
    # already updated last_cycle_at to `now` on new_state.
    is_first_cycle_of_month = (
        state.last_cycle_at is None
        or state.last_cycle_at.month != today.month
        or state.last_cycle_at.year != today.year
    )
    if is_first_cycle_of_month:
        state_snapshot_payload = {
            "trigger": "monthly_heartbeat",
            **new_state.model_dump(mode="json"),
        }
        event_log.append_state_snapshot(
            paths,
            cycle_id=cycle_id,
            payload=state_snapshot_payload,
            now=cycle_end_wall,
            in_sim_timestamp=decision_clock_aware,
        )

    # Reporter invocations (REPORT_SPEC §7.3). Wrapped per §7.6 —
    # a reporter failure must never break the cycle. Always write
    # current_status.txt; on the first weekly cycle of a month also
    # append the prior month's row to its year file; on the first
    # weekly cycle of January also close the prior year file and
    # prune old year files.
    #
    # Production bootstrap convention: IRAPM is started with a
    # pre-staged seed events.jsonl that matches the operator's
    # manual bootstrap (§2.9 — SGOV pre-fund, core seed). With a
    # seed file present, cycle 1 is never a true "system has no
    # history" event, and the reporter's monthly/year-close calls
    # operate on a populated log from the first cycle onward. The
    # simulator path (irapm_driver) does NOT pre-stage; the reporter
    # is expected to handle empty-log inputs per its own contract.
    reports_dir = paths.reports_dir
    try:
        report.write_current_status(
            paths.state_dir, reports_dir / "current_status.txt",
        )
    except Exception as e:
        logger.warning("reporter write_current_status failed: %s", e)

    if is_first_cycle_of_month:
        # Prior month = first day of the month before `today`. Use
        # stdlib arithmetic (no python-dateutil dependency): roll
        # back to the last day of the prior month, then snap to day 1.
        prior_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        try:
            report.append_monthly_row_to_year_file(
                paths.state_dir, reports_dir, month=prior_month,
            )
        except Exception as e:
            logger.warning(
                "reporter append_monthly_row_to_year_file failed: %s", e,
            )

        if today.month == 1:
            # First weekly cycle of January: close prior year, prune old.
            try:
                report.close_year_file(
                    paths.state_dir, reports_dir, year=today.year - 1,
                )
            except Exception as e:
                logger.warning("reporter close_year_file failed: %s", e)
            try:
                report.prune_old_year_files(reports_dir, retain_years=8)
            except Exception as e:
                logger.warning("reporter prune_old_year_files failed: %s", e)

    logger.info("weekly cycle %s complete", cycle_id)


# =============================================================================
# Daily-token cycle
# =============================================================================

def run_daily_token_cycle(config: CycleConfig) -> None:
    """Run one daily-token cycle (§9.6.2).

    Steps:
      1. Detect tokens via TokenDetector.
      2. Save token_observation.json (atomic).
      3. Append to token_check.jsonl.
      4. Dispatch token_state_change / token_unavailable alerts if
         status changed since previous observation.
    """
    paths = config.paths
    paths.ensure_dirs()
    now = config.clock.now()
    # Tz-aware variants for event log (EVENT_LOG_SPEC §3.2). Legacy
    # `now` stays naive for existing consumers.
    now_aware = config.clock.now_utc()

    # Begin cycle attempt
    attempt, _ = begin_cycle(
        cycle_type=CycleType.DAILY_TOKEN,
        box_id=config.box_id,
        client_id=config.client_id,
        now=now,
        path=paths.cycle_attempt_file,
    )
    cycle_id = str(attempt.cycle_uuid)
    decision_clock = attempt.decision_clock
    decision_clock_aware = _to_aware_utc(decision_clock)

    # Capture wall-clock start for cycle_completed.duration_ms.
    cycle_start_wall = now_aware

    # Load operating state for cycle_completed event payload. Daily-
    # token cycles don't mutate state, but the event must carry phase,
    # cb_state, income_state values per EVENT_LOG_SPEC §4.2 — load
    # the latest state (same values the next weekly cycle would see).
    state = load_operating_state(paths)

    # Emit cycle_started (EVENT_LOG_SPEC §4.1).
    event_log.append_cycle_started(
        paths,
        cycle_id=cycle_id,
        cycle_type="daily-token",
        is_restart=False,  # daily-token doesn't track restart status
        box_id=config.box_id,
        client_id=config.client_id,
        now=now_aware,
        in_sim_timestamp=decision_clock_aware,
    )

    # Detect
    obs: TokenObservation = config.token_detector.detect(
        box_id=config.box_id, now=decision_clock,
    )

    # Save current
    save_token_observation(paths, obs)

    # Append to log
    append_token_check(paths, {
        "cycle_id": cycle_id,
        "timestamp": decision_clock.isoformat(),
        "box_id": obs.box_id,
        "phase3_count": obs.phase3_count,
        "stopincome_count": obs.stopincome_count,
        "status": obs.status.value,
        "error": obs.error,
    })

    complete_cycle(attempt, path=paths.cycle_attempt_file)

    # Emit cycle_completed (EVENT_LOG_SPEC §4.2). Daily-token cycles
    # carry the SAME state values as the most recent weekly cycle
    # (daily-token doesn't mutate state), loaded above. lookback /
    # plan / calendar fields are null/False because the daily-token
    # cycle doesn't compute or evaluate any of them.
    cycle_end_wall = config.clock.now_utc()
    duration_ms = int(
        (cycle_end_wall - cycle_start_wall).total_seconds() * 1000
    )
    event_log.append_cycle_completed(
        paths,
        cycle_id=cycle_id,
        cycle_type="daily-token",
        phase=state.phase.value,
        cb_state=state.cb_machine.state.value,
        income_state=state.income_state.value,
        operational_pause=state.operational_pause.paused,
        withdrawal_capacity_exhausted=state.withdrawal_capacity_exhausted,
        lookback_status=None,
        lookback_value=None,
        plan_entry_count=0,
        is_scheduled_withdrawal_day=False,
        is_annual_review_day=False,
        is_phase2_reallocation_day=False,
        duration_ms=duration_ms,
        now=cycle_end_wall,
        in_sim_timestamp=decision_clock_aware,
    )


# =============================================================================
# Internal helpers
# =============================================================================

def _growth_symbols_for_phase(phase, ruleset: Ruleset) -> tuple[str, ...]:
    """Symbols that form the Growth bucket in `phase`."""
    from state_model import Phase
    if phase == Phase.PHASE_1:
        keys = ruleset.phase1_target_weights.weights.keys()
    elif phase == Phase.PHASE_2:
        keys = ruleset.phase2_steady_target_weights.weights.keys()
    else:
        keys = ruleset.phase3_target_weights.weights.keys()
    return tuple(s for s in keys if s in ("FBCG", "AVUV"))


def _data_dir_for_box(config: CycleConfig) -> str:
    """Resolve the price data directory. Convention: c:/portfolio/data
    on Windows, /var/lib/irapm/data on Linux deployment.

    For now we read from a path conventionally placed next to the
    state_dir under 'data'; the operator can symlink to wherever
    the Yahoo refresh script writes.
    """
    return str(config.paths.state_dir.parent / "data")


def _read_combined_tokens(config: CycleConfig) -> Optional[CombinedTokenState]:
    """Read both boxes' token_observation files and combine.

    Master box reads its own observation plus the slave's (rsync'd
    over the dedicated slave→master link). Slave reads only its own
    (it doesn't run cycles unless promoted).

    Path convention:
      - own:  state_dir/token_observation.json
      - peer: state_dir/peer_token_observation.json (rsync target)

    If either file is missing or stale, return None — the decision
    layer treats None as "no token observation this cycle" and holds
    Phase / Income state.
    """
    from persistence import load_token_observation
    own_path = config.paths.token_observation_file
    peer_path = config.paths.state_dir / "peer_token_observation.json"
    own = load_token_observation(own_path)
    peer = load_token_observation(peer_path)
    if own is None or peer is None:
        return None
    # Reconstruct TokenObservation-like dicts → use combine_observations.
    # We need TokenObservation objects; build them lightly.
    from tokens import ObservationStatus
    def _to_obs(d: dict) -> TokenObservation:
        return TokenObservation(
            box_id=d.get("box_id", ""),
            timestamp=datetime.fromisoformat(d["timestamp"]) if d.get("timestamp") else config.clock.now(),
            phase3_count=int(d.get("phase3_count", 0)),
            stopincome_count=int(d.get("stopincome_count", 0)),
            status=ObservationStatus(d.get("status", "ok")),
            error=d.get("error"),
        )
    try:
        a = _to_obs(own)
        b = _to_obs(peer)
    except (KeyError, ValueError) as e:
        logger.warning("token observation parse failed: %s", e)
        return None
    return combine_observations(
        a, b,
        stopincome_token_count_required=config.ruleset.stopincome_token_count_required,
    )


__all__ = [
    "CycleConfig",
    "run_weekly_cycle",
    "run_daily_token_cycle",
]
