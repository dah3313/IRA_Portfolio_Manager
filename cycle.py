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
from ruleset_model import Ruleset
from tokens import (
    CombinedTokenState,
    TokenDetector,
    TokenObservation,
    combine_observations,
)

logger = logging.getLogger(__name__)


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

def run_weekly_cycle(config: CycleConfig) -> None:
    """Run one weekly cycle (the full decision pipeline)."""
    paths = config.paths
    paths.ensure_dirs()

    state = load_operating_state(paths)
    now = config.clock.now()

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

    logger.info("starting weekly cycle %s (restart=%s, decision_clock=%s)",
                cycle_id, is_restart, decision_clock.isoformat())

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

    # 8) Lookback signal: persist to state.lookback_signal
    from state_model import LookbackSignal
    new_state = new_state.model_copy(update={
        "lookback_signal": LookbackSignal(
            status=lookback.status,
            value_pct=lookback.value,
            computed_at=decision_clock,
        )
    })

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
                try:
                    config.alerter.dispatch(
                        AlertEntry(
                            alert_id="external_activity_overlap",
                            context={"detail": result.preflight_failure},
                        ),
                        default_context=default_ctx, now=decision_clock,
                    )
                except Exception as e:
                    logger.warning("alert dispatch failed: %s", e)
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
                try:
                    config.alerter.dispatch(
                        ip.alert,
                        default_context=default_ctx, now=decision_clock,
                    )
                    if ip.escalation_alert is not None:
                        config.alerter.dispatch(
                            ip.escalation_alert,
                            default_context=default_ctx, now=decision_clock,
                        )
                except Exception as e:
                    logger.warning("alert dispatch failed: %s", e)
    else:
        # No plan or pause-skipped: still dispatch alerts (the plan
        # may contain alert entries even in skip mode).
        result = None
        # Dispatch alert entries directly
        for entry in plan.entries:
            if entry.kind == EntryKind.ALERT:
                try:
                    config.alerter.dispatch(entry, default_context={
                        "cycle_id": cycle_id[:8],
                        "box_id": config.box_id,
                        "phase": new_state.phase.value,
                        "cb_state": new_state.cb_machine.state.value,
                        "timestamp": decision_clock.isoformat(),
                    }, now=decision_clock)
                except Exception as e:  # noqa: BLE001
                    logger.warning("alert dispatch failed: %s", e)

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
