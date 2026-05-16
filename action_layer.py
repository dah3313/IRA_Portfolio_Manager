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
import event_log
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
# Time helpers
# =============================================================================

# Eastern Time — action_layer treats the harness/sim's naive "wall-clock"
# datetimes as ET, then converts to UTC for event log emission. Same
# convention as cycle.py and AdvancingClock.now_utc in clock.py.
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")


def _to_aware_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC for event log emission.

    EVENT_LOG_SPEC §3.2 mandates tz-aware UTC for every timestamp.
    Action_layer is called by cycle.py with `now=decision_clock`,
    which may be naive (legacy Clock contract). This helper handles
    both cases: naive → treat as ET, convert to UTC; aware → convert.

    Mirrors cycle.py._to_aware_utc; intentionally duplicated rather
    than imported to keep action_layer's dependency surface minimal.
    Six lines is a cost worth paying for module independence.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ET).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


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

@dataclass(frozen=True)
class _QuantityRefresh:
    """Outcome of the price-refresh + share-recompute step.

    Captures both the share quantity and the diagnostic detail
    needed to populate the `order_placed` event's price_refresh_*
    fields (EVENT_LOG_SPEC §4.5).
    """
    quantity: Decimal
    price_refresh_dollars: Optional[Decimal]  # None when fallback used
    price_refresh_status: str                 # "OK" or "UNAVAILABLE"
    used_fallback_estimate: bool              # True when price was unusable


def _refresh_quantity(
    broker: Broker,
    symbol: str,
    dollar_amount: Decimal,
    fallback_estimate: Decimal,
) -> _QuantityRefresh:
    """Convert a dollar amount into a share quantity using a fresh price.

    Falls back to the SourceLine's stale estimate if the broker reports
    the price as UNAVAILABLE. Rounds to 4 dp ROUND_DOWN to never
    overshoot.

    Returns _QuantityRefresh carrying both the quantity and the
    price-refresh diagnostic detail. Callers (_place_one_order) use
    the diagnostic fields to populate the order_placed event.
    """
    try:
        prices = broker.get_prices([symbol])
    except BrokerError as e:
        logger.warning(
            "get_prices(%s) failed: %s; using stale share estimate %s",
            symbol, e, fallback_estimate,
        )
        return _QuantityRefresh(
            quantity=_quantize_shares(fallback_estimate),
            price_refresh_dollars=None,
            price_refresh_status="UNAVAILABLE",
            used_fallback_estimate=True,
        )
    price = prices.get(symbol)
    if (price is None or price.status != PriceStatus.OK
        or price.price is None or price.price <= 0):
        return _QuantityRefresh(
            quantity=_quantize_shares(fallback_estimate),
            price_refresh_dollars=None,
            price_refresh_status=(
                price.status.value if price is not None else "UNAVAILABLE"
            ),
            used_fallback_estimate=True,
        )
    shares = dollar_amount / price.price
    return _QuantityRefresh(
        quantity=_quantize_shares(shares),
        price_refresh_dollars=price.price,
        price_refresh_status="OK",
        used_fallback_estimate=False,
    )


def _quantize_shares(shares: Decimal) -> Decimal:
    """Round to 4 decimal places, ROUND_DOWN."""
    return shares.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


# =============================================================================
# Per-order primitive
# =============================================================================

def _emit_fills(
    paths: Paths,
    cycle_id: str,
    coid: str,
    res: OrderResult,
    plan_entry_index: int,
    plan_entry_kind: str,
    symbol: str,
    side: OrderSide,
    quantity_submitted: Decimal,
    intended_dollar_amount: Decimal,
    now_aware: datetime,
) -> None:
    """Emit one fill_received event per Fill in OrderResult.fills.

    Per EVENT_LOG_SPEC §4.6: every dollar that moves into or out of
    the portfolio is represented by a fill_received event. This is
    the load-bearing event for the reporter's cash_flow_in /
    cash_flow_out columns.

    Edge case: if OrderResult.fills is empty but the order reached
    FILLED status (shouldn't happen with a well-behaved broker but
    is observable per the OrderResult type), emit a single synthetic
    fill_received using the submitted quantity and intended-dollar-
    derived price. Protects against silent loss of fill records.

    `now_aware` must be tz-aware UTC. `Fill.fill_time` is converted
    via _to_aware_utc since the broker may report naive or non-UTC
    datetimes.
    """
    fills = res.fills
    if not fills:
        # Synthetic single fill — broker reached FILLED but didn't
        # populate fills. Use submitted quantity at the implied price.
        synthetic_price = (
            intended_dollar_amount / quantity_submitted
            if quantity_submitted > 0 else Decimal("0")
        )
        event_log.append_fill_received(
            paths,
            cycle_id=cycle_id,
            client_order_id=coid,
            broker_order_id=res.broker_order_id or "",
            fill_id=f"synthetic-{coid}",
            plan_entry_index=plan_entry_index,
            plan_entry_kind=plan_entry_kind,
            symbol=symbol,
            side=side.value,
            quantity_shares=quantity_submitted,
            price_dollars=synthetic_price,
            fill_dollar_amount=intended_dollar_amount,
            fill_time=now_aware,
            fill_index=0,
            total_fills_for_order=1,
            now=now_aware,
            in_sim_timestamp=now_aware,
        )
        return

    total_fills = len(fills)
    for idx, fill in enumerate(fills):
        fill_dollars = fill.quantity * fill.price
        fill_time_aware = _to_aware_utc(fill.fill_time)
        event_log.append_fill_received(
            paths,
            cycle_id=cycle_id,
            client_order_id=coid,
            broker_order_id=res.broker_order_id or "",
            fill_id=fill.fill_id,
            plan_entry_index=plan_entry_index,
            plan_entry_kind=plan_entry_kind,
            symbol=symbol,
            side=side.value,
            quantity_shares=fill.quantity,
            price_dollars=fill.price,
            fill_dollar_amount=fill_dollars,
            fill_time=fill_time_aware,
            fill_index=idx,
            total_fills_for_order=total_fills,
            now=now_aware,
            in_sim_timestamp=now_aware,
        )


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
    plan_entry_kind: str,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
    paths: Paths,
    cycle_id: str,
) -> tuple[Optional[OrderResult], Optional[str], list[str]]:
    """Submit one order, wait for terminal status, return outcome.

    Returns (result, error_message, [client_order_id]).

    Emits an `order_placed` event (EVENT_LOG_SPEC §4.5) after
    broker.place_order() returns successfully — i.e., the broker
    accepted the order and assigned a broker_order_id. Subsequently
    emits one `fill_received` event per Fill in OrderResult.fills
    once the order reaches FILLED status.
    """
    coid = build_client_order_id(
        cycle_uuid=attempt.cycle_uuid,
        plan_entry_index=plan_entry_index,
        symbol=symbol,
        side=side.value,
    )

    refresh = _refresh_quantity(
        broker, symbol, dollar_amount, fallback_share_estimate
    )
    quantity = refresh.quantity
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

    # Emit order_placed event (EVENT_LOG_SPEC §4.5). Done AFTER the
    # broker accepted and BEFORE we wait for terminal status — every
    # order that reached the broker is logged, including ones that
    # subsequently time out or get cancelled.
    now_aware = _to_aware_utc(now)
    event_log.append_order_placed(
        paths,
        cycle_id=cycle_id,
        client_order_id=coid,
        broker_order_id=res.broker_order_id or "",
        plan_entry_index=plan_entry_index,
        plan_entry_kind=plan_entry_kind,
        symbol=symbol,
        side=side.value,
        order_type=order_type.value,
        quantity_shares=quantity,
        limit_price_dollars=limit_price,
        time_in_force=TimeInForce.DAY.value,
        intended_dollar_amount=dollar_amount,
        price_refresh_dollars=refresh.price_refresh_dollars,
        price_refresh_status=refresh.price_refresh_status,
        used_fallback_estimate=refresh.used_fallback_estimate,
        now=now_aware,
        in_sim_timestamp=now_aware,
    )

    # Terminal status check
    if res.status == OrderStatusValue.FILLED:
        _emit_fills(paths, cycle_id, coid, res, plan_entry_index,
                    plan_entry_kind, symbol, side, quantity, dollar_amount,
                    now_aware)
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
            # Re-fetch fills since the initial OrderResult predates terminal
            # status. The broker is responsible for populating OrderResult
            # with fills at FILLED transition; if the post-poll res still
            # has no fills, _emit_fills emits a synthetic single-fill event
            # per spec §4.6's empty-fills edge case.
            _emit_fills(paths, cycle_id, coid, res, plan_entry_index,
                        plan_entry_kind, symbol, side, quantity,
                        dollar_amount, now_aware)
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
    plan_entry_kind: str,
    timeout_seconds: int,
    cycle_attempt_path,
    now: datetime,
    paths: Paths,
    cycle_id: str,
) -> tuple[list[str], Optional[str]]:
    """Submit a multi-leg group of orders (one per SourceLine).

    Returns (submitted_coids, error_message). `plan_entry_kind` is the
    EntryKind.value of the parent plan entry (e.g., "withdrawal",
    "buffer_refill") — passed through to event_log emissions so each
    order_placed / fill_received can record its parent entry context.
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
            plan_entry_kind=plan_entry_kind,
            timeout_seconds=timeout_seconds,
            cycle_attempt_path=cycle_attempt_path,
            now=now,
            paths=paths,
            cycle_id=cycle_id,
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
    paths: Paths,
    cycle_id: str,
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
        plan_entry_kind=EntryKind.ORDER.value,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path,
        now=now,
        paths=paths,
        cycle_id=cycle_id,
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
    paths: Paths,
    cycle_id: str,
    state: OperatingState,
) -> EntryExecutionResult:
    """§8.2.2 — SELLs; broker-side recurring ACH wires the cash out.

    Emits a `withdrawal_executed` event (EVENT_LOG_SPEC §4.7) after
    every SELL leg reaches terminal-filled status. The event records
    the source breakdown (per-symbol dollar amounts + originating
    client_order_ids) for cross-reference with fill_received events.

    NOTE on binding_ceiling/scheduled_amount_dollars/was_capped fields:
    The WithdrawalEntry dataclass does not currently carry the
    pre-cap scheduled amount or the binding-ceiling identifier;
    decision_layer.apply_phase3_ceilings() computes these but doesn't
    surface them to the plan entry. For now this event is emitted with
    binding_ceiling=null, scheduled_amount_dollars=total_dollar_amount,
    was_capped=false. A focused follow-up will add these fields to
    WithdrawalEntry so the event payload becomes fully informative.
    The data path is otherwise complete.
    """
    coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.sources, side=OrderSide.SELL,
        plan_entry_index=plan_entry_index,
        plan_entry_kind=EntryKind.WITHDRAWAL.value,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
        paths=paths, cycle_id=cycle_id,
    )
    if err:
        return EntryExecutionResult(
            kind=EntryKind.WITHDRAWAL, success=False,
            error=f"withdrawal SELL leg failed: {err}",
            submitted_order_ids=coids,
            note="partial_fills_in_cycle_log",
        )

    # Emit withdrawal_executed (EVENT_LOG_SPEC §4.7). All SELL legs
    # filled at this point; ACH disbursement is broker-side and not
    # itself a separate IRAPM event.
    now_aware = _to_aware_utc(now)
    sources_payload: list[dict] = []
    for src in entry.sources:
        # Each source's contributing client_order_ids: derived by
        # filtering the all-legs coid list for the source's symbol.
        # The coid encodes symbol via build_client_order_id() format,
        # so we match by suffix containing the symbol+side.
        symbol_marker = f"-{src.symbol}-SELL"
        src_coids = [c for c in coids if symbol_marker in c]
        sources_payload.append({
            "symbol": src.symbol,
            "dollar_amount": str(src.dollar_amount),
            "client_order_ids": src_coids,
        })
    # Build the withdrawal_executed payload (EVENT_LOG_SPEC §4.7).
    # `scheduled_amount_dollars` defaults to total when not capped
    # (Phase 1 always; Phase 3 unbound case). `binding_ceiling` is
    # one of: "portfolio_percent", "dollar", "both" (mapping to
    # reporter symbols G, C, both), or None when not capped.
    scheduled_amount = (
        entry.scheduled_amount_dollars
        if entry.scheduled_amount_dollars is not None
        else entry.total_dollar_amount
    )
    withdrawal_payload = {
        "withdrawal_dollar_amount": str(entry.total_dollar_amount),
        "scheduled_ach_date": entry.scheduled_ach_date.isoformat(),
        "binding_ceiling": entry.binding_ceiling,
        "scheduled_amount_dollars": str(scheduled_amount),
        "amount_paid_dollars": str(entry.total_dollar_amount),
        "was_capped": entry.was_capped,
        "sources": sources_payload,
        "phase": state.phase.value,
        "income_state_at_withdrawal": state.income_state.value,
    }
    event_log.append_withdrawal_executed(
        paths,
        cycle_id=cycle_id,
        payload=withdrawal_payload,
        now=now_aware,
        in_sim_timestamp=now_aware,
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
    paths: Paths,
    cycle_id: str,
) -> EntryExecutionResult:
    """§8.2.3 — SELL Growth, then BUY SGOV."""
    coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.growth_sources, side=OrderSide.SELL,
        plan_entry_index=plan_entry_index,
        plan_entry_kind=EntryKind.BUFFER_REFILL.value,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
        paths=paths, cycle_id=cycle_id,
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
        plan_entry_kind=EntryKind.BUFFER_REFILL.value,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
        paths=paths, cycle_id=cycle_id,
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
    paths: Paths,
    cycle_id: str,
) -> EntryExecutionResult:
    """Small cash adjustment — exactly one of SELL or BUY."""
    if entry.sell_source is not None:
        coids, err = _place_source_lines(
            broker=broker, attempt=attempt, contracts=contracts,
            sources=[entry.sell_source], side=OrderSide.SELL,
            plan_entry_index=plan_entry_index,
            plan_entry_kind=EntryKind.CASH_REFILL.value,
            timeout_seconds=timeout_seconds,
            cycle_attempt_path=cycle_attempt_path, now=now,
            paths=paths, cycle_id=cycle_id,
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
            plan_entry_kind=EntryKind.CASH_REFILL.value,
            timeout_seconds=timeout_seconds,
            cycle_attempt_path=cycle_attempt_path, now=now,
            paths=paths, cycle_id=cycle_id,
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
    paths: Paths,
    cycle_id: str,
) -> EntryExecutionResult:
    """§8.2.4 — BUYs only (D11)."""
    coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.buys, side=OrderSide.BUY,
        plan_entry_index=plan_entry_index,
        plan_entry_kind=EntryKind.LARGE_CASH_DEPLOYMENT.value,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
        paths=paths, cycle_id=cycle_id,
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
    paths: Paths,
    cycle_id: str,
) -> tuple[EntryExecutionResult, OperatingState]:
    """§8.2.5 — SELLs, optionally compute I_0 from post-SELL portfolio, BUYs, persist.

    NOTE: this turn's pass threads paths/cycle_id through but does
    NOT yet emit the phase_transition event (EVENT_LOG_SPEC §4.9).
    The emission is in the next pass alongside cb_transition and the
    two state_snapshot triggers.
    """
    sell_coids, err = _place_source_lines(
        broker=broker, attempt=attempt, contracts=contracts,
        sources=entry.sells, side=OrderSide.SELL,
        plan_entry_index=plan_entry_index,
        plan_entry_kind=EntryKind.PHASE_TRANSITION.value,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
        paths=paths, cycle_id=cycle_id,
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
        plan_entry_kind=EntryKind.PHASE_TRANSITION.value,
        timeout_seconds=timeout_seconds,
        cycle_attempt_path=cycle_attempt_path, now=now,
        paths=paths, cycle_id=cycle_id,
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

    # Emit phase_transition event (EVENT_LOG_SPEC §4.9) + state_snapshot
    # (§4.12 trigger="phase_transition"). Done after state mutation
    # completes so the snapshot reflects the post-transition state.
    now_aware = _to_aware_utc(now)

    def _sources_with_coids(source_list, coid_list, side_marker: str) -> list[dict]:
        """Build per-symbol sources array with cross-ref client_order_ids."""
        out: list[dict] = []
        for src in source_list:
            symbol_marker = f"-{src.symbol}-{side_marker}"
            src_coids = [c for c in coid_list if symbol_marker in c]
            out.append({
                "symbol": src.symbol,
                "dollar_amount": str(src.dollar_amount),
                "client_order_ids": src_coids,
            })
        return out

    phase_transition_payload: dict = {
        "from_phase": entry.from_phase.value,
        "to_phase": entry.to_phase.value,
        "is_phase3_activation": entry.is_phase3_activation,
        "sells": _sources_with_coids(entry.sells, sell_coids, "SELL"),
        "buys": _sources_with_coids(entry.buys, buy_coids, "BUY"),
        "phase3_i0_dollars": None,
        "phase3_i0_inputs": None,
    }
    if entry.is_phase3_activation and post_sell_portfolio is not None:
        # i_0 was just computed above; capture both it and the inputs
        # that produced it for long-horizon audit (§4.9 notes).
        phase_transition_payload["phase3_i0_dollars"] = str(i_0)
        phase_transition_payload["phase3_i0_inputs"] = {
            "post_sell_portfolio_dollars": str(post_sell_portfolio),
            "return_assumption": str(ruleset.phase3_i0_calc_return_assumption),
            "inflation_assumption": str(ruleset.phase3_i0_calc_inflation_assumption),
            "horizon_years": ruleset.phase3_i0_calc_horizon_years,
        }
    event_log.append_phase_transition(
        paths,
        cycle_id=cycle_id,
        payload=phase_transition_payload,
        now=now_aware,
        in_sim_timestamp=now_aware,
    )
    _emit_state_snapshot(paths, cycle_id, new_state, "phase_transition", now_aware)

    return EntryExecutionResult(
        kind=EntryKind.PHASE_TRANSITION, success=True,
        note=f"transitioned {entry.from_phase.value} -> {entry.to_phase.value}",
        submitted_order_ids=all_coids,
        side_effects={"phase3_activation": str(entry.is_phase3_activation)},
    ), new_state


def _emit_state_snapshot(
    paths: Paths,
    cycle_id: str,
    state: OperatingState,
    trigger: str,
    now_aware: datetime,
) -> None:
    """Emit a state_snapshot event (EVENT_LOG_SPEC §4.12).

    Mirrors cycle.py's monthly_heartbeat emission pattern: dumps the
    current OperatingState via model_dump(mode="json") and prefixes
    with a trigger field. Used by:
      - _execute_cb_state_transition (trigger="cb_transition")
      - _execute_phase_transition (trigger="phase_transition")

    The third trigger source ("annual_review_completed") lives in
    annual_review.py; the fourth ("monthly_heartbeat") lives in
    cycle.py.
    """
    payload = {
        "trigger": trigger,
        **state.model_dump(mode="json"),
    }
    event_log.append_state_snapshot(
        paths,
        cycle_id=cycle_id,
        payload=payload,
        now=now_aware,
        in_sim_timestamp=now_aware,
    )


def _execute_cb_state_transition(
    entry: CBStateTransitionEntry,
    paths: Paths,
    now: datetime,
    state: OperatingState,
    cycle_id: str,
) -> EntryExecutionResult:
    """§8.2.6 — append to CB transition log.

    Writes BOTH the legacy cb_transitions.jsonl record AND emits the
    new `cb_transition` event (EVENT_LOG_SPEC §4.8). Both writes run
    in parallel during the migration period; Phase 5 removes the
    legacy write.

    Also emits a `state_snapshot` event with trigger="cb_transition"
    per §4.12, capturing the operating state at the moment of the
    transition (state passed in by execute_plan reflects the
    decision-layer's post-cb-evaluation update; the action layer is
    not mutating CB state here, just logging).
    """
    from persistence import append_cb_transition as _legacy_append_cb_transition
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
        _legacy_append_cb_transition(paths, record)
    except OSError as e:
        return EntryExecutionResult(
            kind=EntryKind.CB_STATE_TRANSITION, success=False,
            error=f"log write failed: {e}",
        )

    # Emit new event log entries (§4.8 cb_transition + §4.12 state_snapshot).
    # Event_log writes never raise per its error-handling contract
    # (§5.4), so wrapping in try/except is unnecessary; if disk fills
    # the write logs a warning and the cycle continues.
    now_aware = _to_aware_utc(now)
    lookback_value = state.lookback_signal.value_pct
    event_log.append_cb_transition(
        paths,
        cycle_id=cycle_id,
        from_state=entry.from_state.value,
        to_state=entry.to_state.value,
        trigger_reason=entry.trigger_reason,
        cb2_entry_conditions_after=[c.value for c in entry.cb2_entry_conditions_after],
        lookback_value_at_trigger=lookback_value,
        now=now_aware,
        in_sim_timestamp=now_aware,
    )
    _emit_state_snapshot(paths, cycle_id, state, "cb_transition", now_aware)

    return EntryExecutionResult(
        kind=EntryKind.CB_STATE_TRANSITION, success=True,
        note="cb transition logged",
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
    paths: Paths,
    cycle_id: str,
) -> EntryExecutionResult:
    """§8.2.9 — dispatch via alerter.

    Emits an `alert_emitted` event (EVENT_LOG_SPEC §4.13) after
    dispatch returns. The event records the alert intent plus per-
    channel success/failure from the DispatchOutcome.
    """
    outcome: DispatchOutcome = alerter.dispatch(
        entry, default_context=default_context, now=now,
    )

    # Emit alert_emitted (§4.13) regardless of dispatch outcome.
    now_aware = _to_aware_utc(now)
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
    # cycle_id for event log emissions — every per-entry helper that
    # emits events takes this. Derived from the attempt, which is
    # the canonical owner of the cycle UUID (cycle_attempt.py §1).
    cycle_id = str(attempt.cycle_uuid)

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
                    paths=paths, cycle_id=cycle_id,
                )
            elif entry.kind == EntryKind.WITHDRAWAL:
                r = _execute_withdrawal(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    dry_run=ruleset.dry_run,
                    cycle_attempt_path=ca_path, now=now,
                    paths=paths, cycle_id=cycle_id, state=new_state,
                )
            elif entry.kind == EntryKind.BUFFER_REFILL:
                r = _execute_buffer_refill(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    cycle_attempt_path=ca_path, now=now,
                    paths=paths, cycle_id=cycle_id,
                )
            elif entry.kind == EntryKind.CASH_REFILL:
                r = _execute_cash_refill(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    cycle_attempt_path=ca_path, now=now,
                    paths=paths, cycle_id=cycle_id,
                )
            elif entry.kind == EntryKind.LARGE_CASH_DEPLOYMENT:
                r = _execute_large_cash_deployment(
                    broker, attempt, contracts, entry, idx,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    cycle_attempt_path=ca_path, now=now,
                    paths=paths, cycle_id=cycle_id,
                )
            elif entry.kind == EntryKind.PHASE_TRANSITION:
                r, new_state = _execute_phase_transition(
                    broker, attempt, contracts, entry, idx,
                    state=new_state, ruleset=ruleset,
                    timeout_seconds=ruleset.order_fill_timeout_seconds,
                    now=now, cycle_attempt_path=ca_path,
                    paths=paths, cycle_id=cycle_id,
                )
            elif entry.kind == EntryKind.CB_STATE_TRANSITION:
                r = _execute_cb_state_transition(
                    entry, paths, now, state=new_state, cycle_id=cycle_id,
                )
            elif entry.kind == EntryKind.ACH_SCHEDULE_UPDATE:
                r = _execute_ach_update(broker, entry)
            elif entry.kind == EntryKind.ALERT:
                r = _execute_alert(
                    entry, alerter, default_ctx, now,
                    paths=paths, cycle_id=cycle_id,
                )
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
