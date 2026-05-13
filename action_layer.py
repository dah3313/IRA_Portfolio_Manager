"""
action_layer.py — Plan execution against the real Broker protocol (§8).

PURPOSE:
    Execute a Plan produced by decision_layer.py against any Broker
    implementation that satisfies broker_protocol.Broker. Dispatches
    BUY/SELL orders, waits for fills, fires ACH updates, persists
    state changes from Phase 3 activation, and dispatches alerts.

DESIGN (per §8 of IRAPM_SPECIFICATION.md):
    - One Plan is executed sequentially. Entries execute in listed
      order (§8.1).
    - Pre-flight defenses:
        a. Verify broker reports the expected managed account ID
           (§11.2.14 sub-case a — hard-broke if wrong).
        b. Call get_recent_activity to detect external operator
           activity (§11.2.15 split-brain defense layer 3).
    - Idempotent client_order_ids: every order's coid is minted via
      cycle_attempt.build_client_order_id(cycle_uuid, plan_entry_index,
      symbol, side). The broker rejects duplicates so a cycle that
      crashes mid-execution can be safely restarted; the broker's
      orderRef lookup returns the original order via
      OrderResult.idempotent_rediscovery=True.
    - Each successful submission appends to CycleAttempt.placed_orders
      for local forensics symmetry with the broker's record.
    - Per-entry atomicity: a failure mid-SELL in a multi-leg entry
      stops further legs in that entry; the next order-bearing entry
      is also skipped (halt classification). Alert/CB-log/ACH entries
      always proceed so the operator sees what happened.
    - Phase 3 activation computes I_0 from POST-SELL portfolio value
      (positions market_value + cash) via a real broker query, NOT
      from a pre-cycle snapshot. This is the §4.1.1 "P at the moment
      the transition cycle executes" requirement.

QUANTITY HANDLING (the dollar<->shares boundary):
    IRAPM internally reasons in dollars. The Broker protocol takes
    share quantities. This module bridges the two:
      1. Each SourceLine carries a share_count_estimate computed by
         the decision layer from the pre-cycle market price.
      2. Just before placing each order, we call broker.get_prices()
         to refresh the price and recompute the share quantity from
         the SourceLine's dollar_amount. This bounds the dollar-cost
         error to one tick-to-tick price move regardless of latency
         between decision and action.
      3. The recomputed quantity is what we send to place_order().
    For BUYs in particular, this protects against placing orders
    larger than the SELL proceeds available in the same plan.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from alerter import Alerter, DispatchOutcome
from broker_protocol import (
    Broker,
    BrokerError,
    BrokerInconsistency,
    BrokerNotReady,
    BrokerRejection,
    BrokerUnreachable,
    broker_session,
)
from broker_types import (
    AccountSummary,
    AchUpdateResult,
    ContractRef,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderStatusValue,
    OrderType,
    Position,
    PriceStatus,
    RecentActivity,
    TimeInForce,
)
from cycle_attempt import (
    CycleAttempt,
    PlacedOrderRecord,
    build_client_order_id,
)
from persistence import Paths
from plan_model import (
    ACHScheduleUpdateEntry,
    AlertEntry,
    BufferRefillEntry,
    CashRefillEntry,
    CBStateTransitionEntry,
    EntryKind,
    LargeCashDeploymentEntry,
    OrderEntry,
    PhaseTransitionEntry,
    Plan,
    SourceLine,
    WithdrawalEntry,
)
from phase_transitions import compute_phase3_i0
from ruleset_model import Ruleset
from state_model import (
    BufferState,
    OperatingState,
    Phase,
    ScheduleState,
    ScheduleStateInstance,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Result types
# =============================================================================

@dataclass
class EntryExecutionResult:
    """Per-entry execution outcome."""
    kind: EntryKind
    success: bool
    note: str = ""
    error: Optional[str] = None
    submitted_order_ids: list[str] = field(default_factory=list)
    side_effects: dict[str, str] = field(default_factory=dict)


@dataclass
class CycleExecutionResult:
    """Full cycle execution outcome."""
    new_state: OperatingState
    entry_results: list[EntryExecutionResult]
    halted_due_to_failure: bool = False
    halt_reason: Optional[str] = None
    preflight_failure: Optional[str] = None
    preflight_external_activity: bool = False
    # When preflight_failure is set, no plan entries executed and
    # entry_results is empty. The cycle driver inspects this to decide
    # whether to initiate an operational_pause.


# =============================================================================
# Pre-flight defenses
# =============================================================================

@dataclass(frozen=True)
class PreflightOutcome:
    """Pre-flight defense result."""
    ok: bool
    error: Optional[str] = None
    external_activity_detected: bool = False


def _preflight_checks(
    broker: Broker,
    *,
    expected_account_id: Optional[str],
    activity_lookback_hours: int,
) -> PreflightOutcome:
    """Run pre-flight defenses before any order placement.

    1. Account-ID verification (§11.2.14 sub-case a): if
       expected_account_id is configured, verify the connected broker
       reports the same ID. Mismatch is hard-broke.

    2. External-activity detection (§11.2.15 split-brain defense
       layer 3): query get_recent_activity(since=now-48h). Any
       order whose client_order_id doesn't start with 'cycle-'
       (the IRAPM namespace per build_client_order_id) indicates
       external operator activity through the broker portal; the
       cycle aborts WITHOUT initiating an operational_pause (the
       cycle's declination to act is the heal per D-SPEC-8).
    """
    # Defense 1: account ID
    if expected_account_id is not None:
        try:
            actual = broker.get_managed_account_id()
        except BrokerError as e:
            return PreflightOutcome(
                ok=False,
                error=f"failed to read managed_account_id: {type(e).__name__}: {e}",
            )
        if actual != expected_account_id:
            return PreflightOutcome(
                ok=False,
                error=(
                    f"account_id mismatch: broker reports {actual!r}, "
                    f"expected {expected_account_id!r}"
                ),
            )

    # Defense 2: external activity
    now_utc = datetime.now(timezone.utc)
    since = now_utc - timedelta(hours=activity_lookback_hours)
    try:
        activity: RecentActivity = broker.get_recent_activity(since=since)
    except BrokerError as e:
        return PreflightOutcome(
            ok=False,
            error=f"get_recent_activity failed: {type(e).__name__}: {e}",
        )

    external = []
    for order in list(activity.open_orders) + list(activity.recently_completed_orders):
        if not order.client_order_id.startswith("cycle-"):
            external.append(order.client_order_id)
    if external:
        sample = external[:5]
        more = len(external) - len(sample)
        suffix = f" (and {more} more)" if more > 0 else ""
        return PreflightOutcome(
            ok=False,
            external_activity_detected=True,
            error=f"external order(s) detected: {sample}{suffix}",
        )

    return PreflightOutcome(ok=True)


# =============================================================================
# ContractRef cache
# =============================================================================

class _ContractCache:
    """Per-cycle ContractRef cache.

    Built at execute_plan entry from broker.get_positions(). For
    symbols not in current positions (e.g., first-time GBIL purchase
    at Phase 1->Phase 2 transition), falls back to broker.resolve_symbol().
    """

    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self._cache: dict[str, ContractRef] = {}

    def seed_from_positions(self, positions: list[Position]) -> None:
        for pos in positions:
            self._cache[pos.symbol] = pos.contract

    def get(self, symbol: str) -> ContractRef:
        if symbol in self._cache:
            return self._cache[symbol]
        ref = self._broker.resolve_symbol(symbol)
        self._cache[symbol] = ref
        return ref


# =============================================================================
# Quantity refresh helper
# =============================================================================

def _refresh_quantity(
    broker: Broker,
    symbol: str,
    dollar_amount: Decimal,
    fallback_estimate: Decimal,
) -> Decimal:
    """Convert a dollar amount into a share quantity using a fresh price.

    Falls back to the SourceLine's stale estimate if the broker reports
    the price as UNAVAILABLE. Rounds to 4 dp ROUND_DOWN to never
    overshoot.
    """
    try:
        prices = broker.get_prices([symbol])
    except BrokerError as e:
        logger.warning(
            "get_prices(%s) failed: %s; using stale share estimate %s",
            symbol, e, fallback_estimate,
        )
        return _quantize_shares(fallback_estimate)
    price = prices.get(symbol)
    if (price is None or price.status != PriceStatus.OK
        or price.price is None or price.price <= 0):
        return _quantize_shares(fallback_estimate)
    shares = dollar_amount / price.price
    return _quantize_shares(shares)


def _quantize_shares(shares: Decimal) -> Decimal:
    """Round to 4 decimal places, ROUND_DOWN."""
    return shares.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


# =============================================================================
# Per-order primitive
# =============================================================================

def _place_one_order(
    *,
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    symbol: str,
    side: OrderSide,
    dollar_amount: Decimal,
    fallback_share_estimate: Decimal,
    order_type: OrderType,
    limit_price: Optional[Decimal],
    plan_entry_index: int,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
) -> tuple[Optional[OrderResult], Optional[str], list[str]]:
    """Submit one order, wait for terminal status, return outcome.

    Returns (result, error_message, [client_order_id]).
    """
    coid = build_client_order_id(
        cycle_uuid=attempt.cycle_uuid,
        plan_entry_index=plan_entry_index,
        symbol=symbol,
        side=side.value,
    )

    quantity = _refresh_quantity(
        broker, symbol, dollar_amount, fallback_share_estimate
    )
    if quantity <= 0:
        return None, f"quantity reduced to zero for {symbol}", [coid]

    try:
        contract = contracts.get(symbol)
    except BrokerError as e:
        return None, f"resolve_symbol({symbol}) failed: {e}", [coid]

    try:
        res: OrderResult = broker.place_order(
            contract=contract,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            client_order_id=coid,
            time_in_force=TimeInForce.DAY,
        )
    except BrokerRejection as e:
        return None, f"rejection: {e.reason or e}", [coid]
    except (BrokerUnreachable, BrokerNotReady) as e:
        return None, f"{type(e).__name__}: {e}", [coid]
    except BrokerInconsistency as e:
        return None, f"BrokerInconsistency: {e}", [coid]
    except BrokerError as e:
        return None, f"{type(e).__name__}: {e}", [coid]

    # Record submission in CycleAttempt
    try:
        attempt.append_placed_order(
            PlacedOrderRecord(
                client_order_id=coid,
                broker_order_id=res.broker_order_id,
                symbol=symbol,
                side=side.value,
                quantity=quantity,
                order_type=order_type.value,
                limit_price=limit_price,
                submitted_at=now,
                plan_entry_index=plan_entry_index,
            ),
            path=cycle_attempt_path,
        )
    except OSError as e:
        logger.warning("placed_orders append failed (non-fatal): %s", e)

    # Terminal status check
    if res.status == OrderStatusValue.FILLED:
        return res, None, [coid]
    if res.status in (OrderStatusValue.CANCELLED, OrderStatusValue.REJECTED):
        reason = res.rejection_reason or res.status.value
        return None, f"terminal non-fill: {reason}", [coid]

    # Poll for terminal status
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status: OrderStatus = broker.get_order_status(coid)
        except BrokerError as e:
            return None, f"get_order_status failed: {e}", [coid]
        if status.status == OrderStatusValue.FILLED:
            return res, None, [coid]
        if status.status in (OrderStatusValue.CANCELLED, OrderStatusValue.REJECTED):
            reason = status.rejection_reason or status.status.value
            return None, f"terminal non-fill: {reason}", [coid]
        time.sleep(0.5)

    try:
        broker.cancel_order(coid)
    except BrokerError:
        pass
    return None, f"timeout after {timeout_seconds}s", [coid]


def _place_source_lines(
    *,
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    sources: list[SourceLine],
    side: OrderSide,
    plan_entry_index: int,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
) -> tuple[list[str], Optional[str]]:
    """Submit a multi-leg group of orders (one per SourceLine).

    Returns (submitted_coids, error_message).
    """
    coids: list[str] = []
    for line in sources:
        if line.dollar_amount <= 0:
            continue
        res, err, oids = _place_one_order(
            broker=broker,
            attempt=attempt,
            contracts=contracts,
            symbol=line.symbol,
            side=side,
            dollar_amount=line.dollar_amount,
            fallback_share_estimate=line.share_count_estimate,
            order_type=OrderType.MKT,
            limit_price=None,
            plan_entry_index=plan_entry_index,
            timeout_seconds=timeout_seconds,
            cycle_attempt_path=cycle_attempt_path,
            now=now,
        )
        coids.extend(oids)
        if err is not None:
            return coids, err
    return coids, None


# =============================================================================
# Per-entry dispatchers
# =============================================================================

def _execute_order(
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    entry: OrderEntry,
    plan_entry_index: int,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
) -> EntryExecutionResult:
    """Execute a standalone OrderEntry."""
    res, err, coids = _place_one_order(
        broker=broker,
        attempt=attempt,
        contracts=contracts,
        symbol=entry.symbol,
        side=entry.side,
        dollar_amount=entry.dollar_amount,
        fallback_share_estimate=Decimal("0"),
        order_type=entry.order_type,
        limit_price=entry.limit_price,
        plan_entry_index=plan_entry_index,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path,
        now=now,
    )
    if err is None:
        return EntryExecutionResult(
            kind=EntryKind.ORDER, success=True,
            note=f"filled {entry.dollar_amount} {entry.symbol} {entry.side.value}",
            submitted_order_ids=coids,
        )
    return EntryExecutionResult(
        kind=EntryKind.ORDER, success=False,
        error=err, submitted_order_ids=coids,
    )


def _execute_withdrawal(
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    entry: WithdrawalEntry,
    plan_entry_index: int,
    timeout_seconds: int,
    dry_run: bool,
    cycle_attempt_path,
    now: datetime,
) -> EntryExecutionResult:
    """§8.2.2 — SELLs; broker-side recurring ACH wires the cash out."""
    coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.sources, side=OrderSide.SELL,
        plan_entry_index=plan_entry_index,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
    )
    if err:
        return EntryExecutionResult(
            kind=EntryKind.WITHDRAWAL, success=False,
            error=f"withdrawal SELL leg failed: {err}",
            submitted_order_ids=coids,
            note="partial_fills_in_cycle_log",
        )
    return EntryExecutionResult(
        kind=EntryKind.WITHDRAWAL, success=True,
        note=f"sold {entry.total_dollar_amount} for ACH on {entry.scheduled_ach_date}",
        submitted_order_ids=coids,
        side_effects={"ach_dollar_amount": str(entry.total_dollar_amount)},
    )


def _execute_buffer_refill(
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    entry: BufferRefillEntry,
    plan_entry_index: int,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
) -> EntryExecutionResult:
    """§8.2.3 — SELL Growth, then BUY SGOV."""
    coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.growth_sources, side=OrderSide.SELL,
        plan_entry_index=plan_entry_index,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
    )
    if err:
        return EntryExecutionResult(
            kind=EntryKind.BUFFER_REFILL, success=False,
            error=f"growth SELL leg failed: {err}",
            submitted_order_ids=coids,
        )
    res, err, buy_coids = _place_one_order(
        broker=broker, attempt=attempt, contracts=contracts,
        symbol="SGOV", side=OrderSide.BUY,
        dollar_amount=entry.sgov_buy_amount,
        fallback_share_estimate=Decimal("0"),
        order_type=OrderType.MKT, limit_price=None,
        plan_entry_index=plan_entry_index,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
    )
    coids.extend(buy_coids)
    if err:
        return EntryExecutionResult(
            kind=EntryKind.BUFFER_REFILL, success=False,
            error=f"SGOV BUY failed: {err}",
            submitted_order_ids=coids,
        )
    return EntryExecutionResult(
        kind=EntryKind.BUFFER_REFILL, success=True,
        note=f"refilled {entry.sgov_buy_amount} SGOV",
        submitted_order_ids=coids,
    )


def _execute_cash_refill(
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    entry: CashRefillEntry,
    plan_entry_index: int,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
) -> EntryExecutionResult:
    """Small cash adjustment — exactly one of SELL or BUY."""
    if entry.sell_source is not None:
        coids, err = _place_source_lines(
            broker=broker, attempt=attempt, contracts=contracts,
            sources=[entry.sell_source], side=OrderSide.SELL,
            plan_entry_index=plan_entry_index,
            timeout_seconds=timeout_seconds,
            cycle_attempt_path=cycle_attempt_path, now=now,
        )
        return EntryExecutionResult(
            kind=EntryKind.CASH_REFILL, success=(err is None),
            note="cash refill SELL" if err is None else "",
            submitted_order_ids=coids,
            error=err,
        )
    if entry.buy_target is not None:
        coids, err = _place_source_lines(
            broker=broker, attempt=attempt, contracts=contracts,
            sources=[entry.buy_target], side=OrderSide.BUY,
            plan_entry_index=plan_entry_index,
            timeout_seconds=timeout_seconds,
            cycle_attempt_path=cycle_attempt_path, now=now,
        )
        return EntryExecutionResult(
            kind=EntryKind.CASH_REFILL, success=(err is None),
            note="cash refill BUY" if err is None else "",
            submitted_order_ids=coids,
            error=err,
        )
    return EntryExecutionResult(
        kind=EntryKind.CASH_REFILL, success=True,
        note="no-op (no direction)",
    )


def _execute_large_cash_deployment(
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    entry: LargeCashDeploymentEntry,
    plan_entry_index: int,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
) -> EntryExecutionResult:
    """§8.2.4 — BUYs only (D11)."""
    coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.buys, side=OrderSide.BUY,
        plan_entry_index=plan_entry_index,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
    )
    if err:
        return EntryExecutionResult(
            kind=EntryKind.LARGE_CASH_DEPLOYMENT, success=False,
            error=f"BUY leg failed: {err}", submitted_order_ids=coids,
        )
    return EntryExecutionResult(
        kind=EntryKind.LARGE_CASH_DEPLOYMENT, success=True,
        note=f"deployed {entry.total_dollar_amount} ({entry.mode.value})",
        submitted_order_ids=coids,
    )


def _compute_post_sell_portfolio(broker: Broker) -> Decimal:
    """Compute portfolio value (positions + cash) for §4.1.1 I_0 calc.

    Used at Phase 3 activation: after SELLs have filled and before
    BUYs execute, cash includes SELL proceeds. The "current portfolio
    value at the moment the transition cycle executes" per §4.1.1.
    """
    positions = broker.get_positions()
    summary = broker.get_account_summary()
    pos_total = sum((p.market_value for p in positions), start=Decimal("0"))
    return pos_total + summary.total_cash_value


def _execute_phase_transition(
    broker: Broker,
    attempt: CycleAttempt,
    contracts: _ContractCache,
    entry: PhaseTransitionEntry,
    plan_entry_index: int,
    state: OperatingState,
    ruleset: Ruleset,
    timeout_seconds: int,
    now: datetime,
    cycle_attempt_path,
) -> tuple[EntryExecutionResult, OperatingState]:
    """§8.2.5 — SELLs, optionally compute I_0 from post-SELL portfolio, BUYs, persist."""
    sell_coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.sells, side=OrderSide.SELL,
        plan_entry_index=plan_entry_index,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
    )
    if err:
        return EntryExecutionResult(
            kind=EntryKind.PHASE_TRANSITION, success=False,
            error=f"phase transition SELL leg failed: {err}",
            submitted_order_ids=sell_coids,
            note="partial_phase_transition",
        ), state

    post_sell_portfolio: Optional[Decimal] = None
    if entry.is_phase3_activation:
        try:
            post_sell_portfolio = _compute_post_sell_portfolio(broker)
        except BrokerError as e:
            return EntryExecutionResult(
                kind=EntryKind.PHASE_TRANSITION, success=False,
                error=f"post-SELL portfolio query failed: {e}",
                submitted_order_ids=sell_coids,
                note="partial_phase_transition",
            ), state

    buy_coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.buys, side=OrderSide.BUY,
        plan_entry_index=plan_entry_index,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
    )
    all_coids = sell_coids + buy_coids
    if err:
        return EntryExecutionResult(
            kind=EntryKind.PHASE_TRANSITION, success=False,
            error=f"phase transition BUY leg failed: {err}",
            submitted_order_ids=all_coids,
            note="partial_phase_transition",
        ), state

    new_state = state
    if entry.to_phase != state.phase:
        new_state = new_state.model_copy(update={"phase": entry.to_phase})

    if entry.is_phase3_activation and post_sell_portfolio is not None:
        i_0 = compute_phase3_i0(
            portfolio_value=post_sell_portfolio,
            return_assumption=ruleset.phase3_i0_calc_return_assumption,
            inflation_assumption=ruleset.phase3_i0_calc_inflation_assumption,
            horizon_years=ruleset.phase3_i0_calc_horizon_years,
        )
        new_schedule = ScheduleState(
            phase1=new_state.schedule_state.phase1,
            phase3=ScheduleStateInstance(
                i_0_dollars=i_0,
                trigger_year=now.year,
                cpi_rate=ruleset.inflation_rate,
                frozen_years=[],
            ),
        )
        sgov_target = i_0 * ruleset.sgov_buffer_target_months
        refill_rate = sgov_target / Decimal("12")
        cash_target = i_0 + ruleset.cash_buffer_offset_dollars
        new_buffer = BufferState(
            sgov_target_dollars=sgov_target,
            monthly_refill_rate_dollars=refill_rate,
            cash_target_dollars=cash_target,
            recomputed_at=now,
            refill_delay_started_at=new_state.buffer_state.refill_delay_started_at,
            last_refill_at=new_state.buffer_state.last_refill_at,
        )
        new_state = new_state.model_copy(update={
            "schedule_state": new_schedule,
            "buffer_state": new_buffer,
        })

    return EntryExecutionResult(
        kind=EntryKind.PHASE_TRANSITION, success=True,
        note=f"transitioned {entry.from_phase.value} -> {entry.to_phase.value}",
        submitted_order_ids=all_coids,
        side_effects={"phase3_activation": str(entry.is_phase3_activation)},
    ), new_state


def _execute_cb_state_transition(
    entry: CBStateTransitionEntry,
    paths: Paths,
    now: datetime,
) -> EntryExecutionResult:
    """§8.2.6 — append to CB transition log."""
    from persistence import append_cb_transition
    record = {
        "timestamp": now.isoformat(),
        "from_state": entry.from_state.value,
        "to_state": entry.to_state.value,
        "trigger_reason": entry.trigger_reason,
        "cb2_entry_conditions_after": [
            c.value for c in entry.cb2_entry_conditions_after
        ],
    }
    try:
        append_cb_transition(paths, record)
        return EntryExecutionResult(
            kind=EntryKind.CB_STATE_TRANSITION, success=True,
            note="cb transition logged",
        )
    except OSError as e:
        return EntryExecutionResult(
            kind=EntryKind.CB_STATE_TRANSITION, success=False,
            error=f"log write failed: {e}",
        )


def _execute_ach_update(
    broker: Broker,
    entry: ACHScheduleUpdateEntry,
) -> EntryExecutionResult:
    """§8.2.7 — update broker-side recurring ACH amount."""
    try:
        result: AchUpdateResult = broker.update_recurring_ach(
            new_amount_dollars=entry.new_amount_dollars,
        )
    except BrokerError as e:
        return EntryExecutionResult(
            kind=EntryKind.ACH_SCHEDULE_UPDATE, success=False,
            error=f"{type(e).__name__}: {e}",
        )
    if result.success:
        return EntryExecutionResult(
            kind=EntryKind.ACH_SCHEDULE_UPDATE, success=True,
            note=f"ACH set to {entry.new_amount_dollars} ({entry.reason})",
        )
    return EntryExecutionResult(
        kind=EntryKind.ACH_SCHEDULE_UPDATE, success=False,
        error=result.rejection_reason or "broker reported failure",
    )


def _execute_alert(
    entry: AlertEntry,
    alerter: Alerter,
    default_context: dict[str, str],
    now: datetime,
) -> EntryExecutionResult:
    """§8.2.9 — dispatch via alerter."""
    outcome: DispatchOutcome = alerter.dispatch(
        entry, default_context=default_context, now=now,
    )
    success = outcome.email_ok or outcome.sms_ok or outcome.deduped
    if outcome.deduped:
        note = f"deduped ({entry.alert_id})"
    else:
        bits = []
        bits.append("email_ok" if outcome.email_ok else f"email_err={outcome.email_error}")
        bits.append("sms_ok" if outcome.sms_ok else f"sms_err={outcome.sms_error}")
        note = ", ".join(bits)
    return EntryExecutionResult(
        kind=EntryKind.ALERT, success=success,
        note=note,
        error=None if success else "both channels failed",
    )


# =============================================================================
# Top-level execute
# =============================================================================

_ORDER_BEARING_KINDS = frozenset({
    EntryKind.ORDER,
    EntryKind.WITHDRAWAL,
    EntryKind.BUFFER_REFILL,
    EntryKind.CASH_REFILL,
    EntryKind.LARGE_CASH_DEPLOYMENT,
    EntryKind.PHASE_TRANSITION,
})


def execute_plan(
    *,
    plan: Plan,
    state: OperatingState,
    ruleset: Ruleset,
    broker: Broker,
    alerter: Alerter,
    paths: Paths,
    attempt: CycleAttempt,
    now: datetime,
    expected_account_id: Optional[str] = None,
    activity_lookback_hours: int = 48,
    default_alert_context: dict[str, str] | None = None,
) -> CycleExecutionResult:
    """Execute a Plan end-to-end against the broker.

    Lifecycle:
      1. Open broker session.
      2. Run pre-flight defenses.
      3. Build the ContractRef cache.
      4. Iterate plan entries; halt order-bearing entries on failure.
      5. Return CycleExecutionResult.
    """
    results: list[EntryExecutionResult] = []
    new_state = state
    halted = False
    halt_reason: Optional[str] = None
    default_ctx = default_alert_context or {}
    ca_path = paths.cycle_attempt_file

    with broker_session(broker):
        preflight = _preflight_checks(
            broker,
            expected_account_id=expected_account_id,
            activity_lookback_hours=activity_lookback_hours,
        )
        if not preflight.ok:
            return CycleExecutionResult(
                new_state=state,
                entry_results=[],
                preflight_failure=preflight.error,
                preflight_external_activity=preflight.external_activity_detected,
            )

        contracts = _ContractCache(broker)
        try:
            current_positions = broker.get_positions()
        except BrokerError as e:
            return CycleExecutionResult(
                new_state=state,
                entry_results=[],
                preflight_failure=f"get_positions failed: {e}",
            )
        contracts.seed_from_positions(current_positions)

        for idx, entry in enumerate(plan.entries):
            if halted and entry.kind != EntryKind.ALERT:
                results.append(EntryExecutionResult(
                    kind=entry.kind, success=False,
                    note="skipped due to prior halt",
                    error=halt_reason,
                ))
                continue

            if entry.kind == EntryKind.ORDER:
                r = _execute_order(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    cycle_attempt_path=ca_path, now=now,
                )
            elif entry.kind == EntryKind.WITHDRAWAL:
                r = _execute_withdrawal(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    dry_run=ruleset.dry_run,
                    cycle_attempt_path=ca_path, now=now,
                )
            elif entry.kind == EntryKind.BUFFER_REFILL:
                r = _execute_buffer_refill(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    cycle_attempt_path=ca_path, now=now,
                )
            elif entry.kind == EntryKind.CASH_REFILL:
                r = _execute_cash_refill(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    cycle_attempt_path=ca_path, now=now,
                )
            elif entry.kind == EntryKind.LARGE_CASH_DEPLOYMENT:
                r = _execute_large_cash_deployment(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    cycle_attempt_path=ca_path, now=now,
                )
            elif entry.kind == EntryKind.PHASE_TRANSITION:
                r, new_state = _execute_phase_transition(
                    broker, attempt, contracts, entry, idx,
                    state=new_state, ruleset=ruleset,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    now=now, cycle_attempt_path=ca_path,
                )
            elif entry.kind == EntryKind.CB_STATE_TRANSITION:
                r = _execute_cb_state_transition(entry, paths, now)
            elif entry.kind == EntryKind.ACH_SCHEDULE_UPDATE:
                r = _execute_ach_update(broker, entry)
            elif entry.kind == EntryKind.ALERT:
                r = _execute_alert(entry, alerter, default_ctx, now)
            else:
                r = EntryExecutionResult(
                    kind=entry.kind, success=False,
                    error=f"unknown entry kind {entry.kind!r}",
                )
            results.append(r)

            if not r.success and entry.kind in _ORDER_BEARING_KINDS:
                halted = True
                halt_reason = r.error or "order failure"

    return CycleExecutionResult(
        new_state=new_state,
        entry_results=results,
        halted_due_to_failure=halted,
        halt_reason=halt_reason,
    )


__all__ = [
    "CycleExecutionResult",
    "EntryExecutionResult",
    "PreflightOutcome",
    "execute_plan",
]
