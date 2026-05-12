"""
SyntheticBroker — in-memory implementation of the Broker protocol.

Used by:
  - The IPMS simulator (historical backtests of IRAPM cycle behavior).
  - IRAPM unit tests that need a controllable broker without ib-async.

DESIGN CONTRACT

  - Pure function of (initial state, clock readings, method calls).
    No wall-clock dependencies, no randomness, no I/O. Two identical
    sequences of method calls against two identically-configured
    SyntheticBroker instances produce identical results.

  - Implements every method in broker_protocol.Broker. Returns the
    same plain data types from broker_types. Raises the same typed
    exceptions. Sim-vs-real testing exercises the same contract
    against this implementation and IBKRBroker.

  - Time comes from an injected Clock (from clock.py). IPMS injects
    an AdvancingClock and advances it between cycles; unit tests
    inject a FrozenClock. The broker never reads wall-clock time.

  - Default fill behavior: every order fills immediately at the
    market price currently set for that symbol (configured via
    set_market_prices()). Tests can override by installing a custom
    FillPolicy.

  - Account state evolves only when methods are called or when the
    test explicitly calls evolve_pending_state(). There is no
    background simulation.

WHAT THIS DELIBERATELY DOES NOT MODEL

  - Bid/ask spreads, slippage, partial fills due to liquidity, market
    impact of large orders.
  - IBKR-specific risk checks, day-trading buying-power rules,
    good-faith violations (IRA accounts don't have these anyway).
  - Network latency or TWS reconnection sequences. IBKRBroker-level
    error handling is tested with dedicated mocks, not via this
    implementation.

For higher-fidelity scenarios (e.g., "what if the post-placement
confirmation times out?"), use the fault-injection methods:
arm_next_rejection(), arm_next_inconsistency(),
arm_next_placement_to_not_confirm().
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from broker_protocol import (
    Broker,
    BrokerInconsistency,
    BrokerNotReady,
    BrokerRejection,
    BrokerUnreachable,
)
from broker_types import (
    AccountSummary,
    AchUpdateResult,
    ContractRef,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderStatusValue,
    OrderType,
    Position,
    Price,
    PriceStatus,
    RecentActivity,
    RecurringAchInfo,
    TimeInForce,
)
from clock import Clock, SystemClock


BROKER_IMPL_TAG = "SyntheticBroker"
"""Identifier stamped onto every ContractRef this broker mints. Used by
the protocol's broker-identity safety check: passing a ref minted by a
different broker implementation to place_order() raises
BrokerInconsistency."""

POST_PLACEMENT_CONFIRMATION_WINDOW_SEC = 5
"""Window within which place_order() must see the order in open orders
before it raises BrokerInconsistency. SyntheticBroker's normal path
populates open orders synchronously, so this is reached only via
arm_next_placement_to_not_confirm() fault injection."""

ACTIVITY_LOOKBACK_HOURS = 48
"""Maximum age of completed orders and fills returned by
get_recent_activity() and used for idempotent order rediscovery.
Mirrors the design's 48-hour lookback for operational_pause auto-resume."""


# =============================================================================
# Internal data classes (not exposed)
# =============================================================================

@dataclass
class _InternalOrder:
    """The broker-side record of an order. Distinct from OrderResult:
    this is the long-lived state that evolves over time as fills happen
    and the order moves through its lifecycle. OrderResult is a snapshot
    constructed for return to callers."""

    client_order_id: str
    broker_order_id: str
    symbol: str
    contract: ContractRef
    side: OrderSide
    order_type: OrderType
    quantity_ordered: Decimal
    limit_price: Optional[Decimal]
    time_in_force: TimeInForce
    submitted_at: datetime
    status: OrderStatusValue
    fills: list[Fill] = field(default_factory=list)
    rejection_reason: Optional[str] = None

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills), Decimal("0"))

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity_ordered - self.filled_quantity

    @property
    def average_fill_price(self) -> Optional[Decimal]:
        if not self.fills:
            return None
        total_value = sum((f.quantity * f.price for f in self.fills), Decimal("0"))
        total_qty = self.filled_quantity
        if total_qty == 0:
            return None
        return total_value / total_qty


# =============================================================================
# Fill policy: pluggable order-fill behavior
# =============================================================================

@dataclass
class FillPolicy:
    """Pluggable fill behavior for SyntheticBroker.

    Default behavior (immediate full fill at market price):
        FillPolicy()  # all defaults

    Custom behaviors are expressed by overriding the callable hooks.

    UNCERTAINTY: this is a simplistic model. Real fill behavior involves
    bid/ask, slippage, partial fills. Per the design intent
    (SyntheticBroker is not a market simulator), we don't model those —
    use higher-fidelity fixtures for tests that need them.
    """

    # If a custom function is supplied, it's called instead of the
    # default fill logic and should return a list of Fill objects.
    # First argument is the order, second is the symbol's current price.
    fill_fn: Optional[Callable[[_InternalOrder, Decimal, datetime], list[Fill]]] = None

    def compute_fills(
        self,
        order: _InternalOrder,
        market_price: Decimal,
        fill_time: datetime,
    ) -> list[Fill]:
        """Return the fills that result from `order` against the
        current `market_price` at `fill_time`.

        Default policy: full immediate fill at market price. For LMT
        orders, only fill if the limit is permissive enough:
          - BUY LMT fills only if market_price <= limit_price
          - SELL LMT fills only if market_price >= limit_price
        Otherwise the order stays WORKING (no fills yet).
        """
        if self.fill_fn is not None:
            return self.fill_fn(order, market_price, fill_time)

        # Default policy
        if order.order_type == OrderType.LMT:
            if order.limit_price is None:
                # Defensive: protocol should have prevented this, but if
                # we got here, the order is malformed — don't fill.
                return []
            if order.side == OrderSide.BUY and market_price > order.limit_price:
                return []  # Buy LMT not yet reachable
            if order.side == OrderSide.SELL and market_price < order.limit_price:
                return []  # Sell LMT not yet reachable

        # Full immediate fill at the appropriate price.
        fill_price = market_price if order.order_type == OrderType.MKT else order.limit_price  # type: ignore[assignment]
        assert fill_price is not None  # narrowing; LMT was checked above
        return [
            Fill(
                fill_id=f"{order.broker_order_id}-fill-1",
                fill_time=fill_time,
                quantity=order.quantity_ordered,
                price=fill_price,
                commission=Decimal("0"),
            )
        ]


# =============================================================================
# SyntheticBroker
# =============================================================================

class SyntheticBroker:
    """In-memory Broker for IPMS simulation and unit tests.

    Implements broker_protocol.Broker. Conforms structurally to the
    Protocol; no inheritance needed.

    Construction:
        broker = SyntheticBroker(
            account_id="SIM-12345678",
            initial_cash=Decimal("100000.00"),
            clock=AdvancingClock(start=date(2027, 1, 1)),
        )

    Seeding (optional):
        broker.seed_position(symbol="SGOV", quantity=Decimal("200"),
                             market_price=Decimal("100.50"))
        broker.seed_recurring_ach(amount_dollars=Decimal("3000.00"),
                                  destination_reference="CHASE ****1234")

    Use:
        broker.connect()
        positions = broker.get_positions()
        # ...
        broker.disconnect()
    """

    # -------------------------------------------------------------------------
    # Construction and seeding
    # -------------------------------------------------------------------------

    def __init__(
        self,
        *,
        account_id: str,
        initial_cash: Decimal,
        clock: Optional[Clock] = None,
        fill_policy: Optional[FillPolicy] = None,
        expected_account_id: Optional[str] = None,
    ) -> None:
        """Create an empty broker.

        Args:
          account_id: Identifier this broker reports via
            get_managed_account_id(). Mirrors an IBKR account number
            like "U1234567" for realism.
          initial_cash: Starting cash balance. Treated entirely as
            settled cash; tests that need unsettled-cash scenarios
            can mutate after construction.
          clock: Time source. If None, uses SystemClock() — fine for
            unit tests that don't care about time, but real simulations
            should inject an AdvancingClock.
          fill_policy: How orders are filled. Default: immediate full
            fill at market price.
          expected_account_id: If supplied, connect() raises
            BrokerInconsistency if account_id != expected_account_id.
            Use to test the startup account-mismatch defense.
        """
        # Identity
        self._account_id = account_id
        self._expected_account_id = expected_account_id

        # Time source — separate from wall clock per design contract.
        self._clock = clock or SystemClock()

        # Connection state — driven by connect()/disconnect()/is_ready().
        self._connected = False

        # Account state
        self._cash_settled = initial_cash
        self._cash_unsettled = Decimal("0")
        self._positions: dict[str, Position] = {}
        self._market_prices: dict[str, Price] = {}

        # Orders, indexed by client_order_id for fast idempotent lookup.
        # Includes orders in all states (open, completed, rejected).
        self._orders: dict[str, _InternalOrder] = {}

        # Recurring ACH state
        self._recurring_ach: Optional[RecurringAchInfo] = None

        # Fill policy
        self._fill_policy = fill_policy or FillPolicy()

        # Order-ID minting counter (so broker_order_id values look like
        # IBKR's monotonic integer order IDs).
        self._next_broker_order_id = 1000

        # Fault injection
        self._next_rejection_reason: Optional[str] = None
        self._next_inconsistency_msg: Optional[str] = None
        self._next_placement_does_not_confirm = False
        self._next_connect_raises: Optional[Exception] = None
        self._next_disconnect_raises: Optional[Exception] = None

    def seed_position(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        market_price: Decimal,
        avg_cost: Optional[Decimal] = None,
    ) -> None:
        """Pre-populate a position. Also sets the market price for the
        symbol so get_prices() returns it."""
        contract = ContractRef(broker_impl=BROKER_IMPL_TAG, symbol=symbol)
        market_value = quantity * market_price
        self._positions[symbol] = Position(
            contract=contract,
            symbol=symbol,
            quantity=quantity,
            market_value=market_value,
            avg_cost=avg_cost if avg_cost is not None else market_price,
        )
        self.set_market_price(symbol, market_price)

    def seed_recurring_ach(
        self,
        *,
        amount_dollars: Decimal,
        destination_reference: str,
        next_scheduled_date: Optional[datetime] = None,
    ) -> None:
        """Configure the recurring monthly ACH."""
        self._recurring_ach = RecurringAchInfo(
            is_configured=True,
            amount_dollars=amount_dollars,
            destination_reference=destination_reference,
            next_scheduled_date=next_scheduled_date,
        )

    def set_market_price(self, symbol: str, price: Decimal) -> None:
        """Set the current market price for a single symbol. The next
        get_prices() call will return this value (with as_of from the
        clock)."""
        self._market_prices[symbol] = Price(
            symbol=symbol,
            status=PriceStatus.OK,
            price=price,
            as_of=self._clock_now_utc(),
        )

    def set_market_prices(self, prices: dict[str, Decimal]) -> None:
        """Bulk-set market prices. Convenience for IPMS which receives
        a {symbol: price} dict per cycle."""
        for symbol, price in prices.items():
            self.set_market_price(symbol, price)

    def set_price_unavailable(self, symbol: str) -> None:
        """Mark a symbol as having no price (simulates UNAVAILABLE
        for that symbol on a particular cycle). Useful for testing
        the lookback signal's UNAVAILABLE handling and the avg_cost
        fallback path."""
        self._market_prices[symbol] = Price(
            symbol=symbol,
            status=PriceStatus.UNAVAILABLE,
            price=None,
            as_of=None,
        )

    # -------------------------------------------------------------------------
    # Fault injection (for testing the cycle's error handling)
    # -------------------------------------------------------------------------

    def arm_next_rejection(self, reason: str) -> None:
        """Cause the NEXT place_order() call to raise BrokerRejection
        with the given reason. One-shot: cleared after the next call."""
        self._next_rejection_reason = reason

    def arm_next_inconsistency(self, message: str) -> None:
        """Cause the NEXT place_order() call to raise
        BrokerInconsistency with the given message. One-shot."""
        self._next_inconsistency_msg = message

    def arm_next_placement_to_not_confirm(self) -> None:
        """Cause the NEXT place_order() call to "submit" the order but
        deliberately fail to add it to open orders, simulating the rare
        IBKR failure mode where placeOrder succeeds but the order never
        reaches the order book. The protocol contract requires
        place_order() to raise BrokerInconsistency in this case."""
        self._next_placement_does_not_confirm = True

    def arm_next_connect_raises(self, exc: Exception) -> None:
        """Cause the NEXT connect() call to raise the given exception.
        Use a BrokerUnreachable or BrokerInconsistency instance."""
        self._next_connect_raises = exc

    def arm_next_disconnect_raises(self, exc: Exception) -> None:
        """Cause the NEXT disconnect() call to raise — though the
        protocol says disconnect() should not raise. Used to verify
        callers handle a misbehaving broker robustly. Most callers
        should never hit this in practice."""
        self._next_disconnect_raises = exc

    # -------------------------------------------------------------------------
    # Clock helper (UTC, tz-aware — matches what IBKR returns)
    # -------------------------------------------------------------------------

    def _clock_now_utc(self) -> datetime:
        """Return current clock time as tz-aware UTC.

        The Clock seam exposes now() (naive) and now_et() (ET tz-aware).
        IRAPM types use UTC; convert from ET.
        """
        et = self._clock.now_et()
        return et.astimezone(timezone.utc)

    # =========================================================================
    # PROTOCOL IMPLEMENTATION
    # =========================================================================

    # -------------------------------------------------------------------------
    # Group 1: connection lifecycle
    # -------------------------------------------------------------------------

    def connect(self, *, timeout_sec: float = 30.0) -> None:
        # Fault injection: pre-armed connect failure.
        if self._next_connect_raises is not None:
            exc = self._next_connect_raises
            self._next_connect_raises = None
            raise exc

        # Account-mismatch check (mirrors IBKRBroker's startup defense).
        if (
            self._expected_account_id is not None
            and self._expected_account_id != self._account_id
        ):
            raise BrokerInconsistency(
                f"Connected to account {self._account_id} but expected "
                f"{self._expected_account_id}. Refusing to proceed."
            )

        self._connected = True

    def disconnect(self) -> None:
        # Fault injection: pre-armed disconnect failure.
        if self._next_disconnect_raises is not None:
            exc = self._next_disconnect_raises
            self._next_disconnect_raises = None
            raise exc

        # Idempotent: safe to call when already disconnected.
        self._connected = False

    def is_ready(self) -> bool:
        return self._connected

    # -------------------------------------------------------------------------
    # Group 2: state queries
    # -------------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerNotReady(
                "SyntheticBroker method called before connect(); call "
                "connect() (or use broker_session(...)) first."
            )

    def get_positions(self) -> list[Position]:
        self._require_connected()
        # Filter out zero-quantity positions per protocol contract.
        # Refresh market_value against current prices so callers see
        # up-to-date values even if prices were updated after seeding.
        result = []
        for symbol, position in self._positions.items():
            if position.quantity == 0:
                continue
            current_price = self._market_prices.get(symbol)
            if current_price is not None and current_price.status == PriceStatus.OK:
                refreshed = position.model_copy(update={
                    "market_value": position.quantity * current_price.price,  # type: ignore[operator]
                })
                result.append(refreshed)
            else:
                # No fresh price; keep last-known market value.
                result.append(position)
        return result

    def get_prices(self, symbols: list[str]) -> dict[str, Price]:
        self._require_connected()
        result: dict[str, Price] = {}
        for symbol in symbols:
            if symbol in self._market_prices:
                result[symbol] = self._market_prices[symbol]
            else:
                # Symbol not seeded — treat as UNAVAILABLE rather than
                # raising. Matches IBKRBroker behavior for un-subscribed
                # market data.
                result[symbol] = Price(
                    symbol=symbol,
                    status=PriceStatus.UNAVAILABLE,
                    price=None,
                    as_of=None,
                )
        return result

    def get_account_summary(self) -> AccountSummary:
        self._require_connected()
        total = self._cash_settled + self._cash_unsettled
        return AccountSummary(
            account_id=self._account_id,
            total_cash_value=total,
            settled_cash=self._cash_settled,
            unsettled_cash=self._cash_unsettled,
            buying_power=self._cash_settled,  # Cash account: BP == settled cash
        )

    def get_recent_activity(self, *, since: datetime) -> RecentActivity:
        self._require_connected()
        now = self._clock_now_utc()

        open_orders: list[OrderResult] = []
        recent_completed: list[OrderResult] = []
        recent_fills: list[Fill] = []

        # Bound the lookback at ACTIVITY_LOOKBACK_HOURS to match the
        # idempotency window even if caller passes an older `since`.
        effective_since = max(
            since,
            now - timedelta(hours=ACTIVITY_LOOKBACK_HOURS),
        )

        for order in self._orders.values():
            order_result = self._order_to_result(order)
            if order.status in (
                OrderStatusValue.PENDING,
                OrderStatusValue.WORKING,
                OrderStatusValue.PARTIAL,
            ):
                open_orders.append(order_result)
            elif order.submitted_at >= effective_since:
                # Completed (filled/cancelled/rejected) within the window
                recent_completed.append(order_result)

            # Collect this order's fills that fall in the window
            for fill in order.fills:
                if fill.fill_time >= effective_since:
                    recent_fills.append(fill)

        # Sort for protocol contract: open and completed by submitted_at
        # asc; fills by fill_time asc.
        open_orders.sort(key=lambda o: o.submitted_at)
        recent_completed.sort(key=lambda o: o.submitted_at)
        recent_fills.sort(key=lambda f: f.fill_time)

        return RecentActivity(
            as_of=now,
            open_orders=open_orders,
            recently_completed_orders=recent_completed,
            recent_fills=recent_fills,
        )

    # -------------------------------------------------------------------------
    # Group 3: order placement
    # -------------------------------------------------------------------------

    def place_order(
        self,
        *,
        contract: ContractRef,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType,
        limit_price: Optional[Decimal] = None,
        client_order_id: str,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> OrderResult:
        self._require_connected()

        # ---- Protocol-level argument validation ----
        if quantity <= 0:
            raise ValueError(
                f"quantity must be positive; got {quantity}"
            )
        if order_type == OrderType.LMT and limit_price is None:
            raise ValueError(
                "limit_price is required for LMT orders"
            )
        if order_type == OrderType.MKT and limit_price is not None:
            raise ValueError(
                "limit_price must be None for MKT orders"
            )
        if contract.broker_impl != BROKER_IMPL_TAG:
            raise BrokerInconsistency(
                f"ContractRef was minted by {contract.broker_impl!r} but "
                f"this broker is {BROKER_IMPL_TAG!r}. Cross-broker contract "
                f"refs are not permitted."
            )

        # ---- Idempotent rediscovery ----
        existing = self._orders.get(client_order_id)
        if existing is not None:
            # Already placed in a prior cycle attempt. Return its
            # current state with idempotent_rediscovery=True.
            return self._order_to_result(existing, idempotent=True)

        # ---- Fault injection: BrokerRejection ----
        if self._next_rejection_reason is not None:
            reason = self._next_rejection_reason
            self._next_rejection_reason = None
            raise BrokerRejection(
                f"Order rejected: {reason}",
                reason=reason,
            )

        # ---- Fault injection: BrokerInconsistency ----
        if self._next_inconsistency_msg is not None:
            msg = self._next_inconsistency_msg
            self._next_inconsistency_msg = None
            raise BrokerInconsistency(msg)

        # ---- Construct internal order ----
        now = self._clock_now_utc()
        broker_order_id = str(self._next_broker_order_id)
        self._next_broker_order_id += 1
        internal_order = _InternalOrder(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=contract.symbol,
            contract=contract,
            side=side,
            order_type=order_type,
            quantity_ordered=quantity,
            limit_price=limit_price,
            time_in_force=time_in_force,
            submitted_at=now,
            status=OrderStatusValue.PENDING,
        )

        # ---- Fault injection: post-placement non-confirmation ----
        if self._next_placement_does_not_confirm:
            self._next_placement_does_not_confirm = False
            # The order is "submitted" but NOT added to self._orders,
            # so a follow-up check for the order in open orders would
            # fail. Per protocol contract, raise BrokerInconsistency.
            raise BrokerInconsistency(
                "Order was submitted but did not appear in the broker's "
                f"open-orders table within {POST_PLACEMENT_CONFIRMATION_WINDOW_SEC}s "
                f"of submission (client_order_id={client_order_id})."
            )

        # ---- Default path: register the order ----
        self._orders[client_order_id] = internal_order

        # Try to fill against the current market price.
        symbol_price = self._market_prices.get(internal_order.symbol)
        if symbol_price is not None and symbol_price.status == PriceStatus.OK:
            fills = self._fill_policy.compute_fills(
                internal_order, symbol_price.price, now,  # type: ignore[arg-type]
            )
            if fills:
                for fill in fills:
                    internal_order.fills.append(fill)
                self._apply_fills_to_account(internal_order, fills)
                if internal_order.filled_quantity == internal_order.quantity_ordered:
                    internal_order.status = OrderStatusValue.FILLED
                else:
                    internal_order.status = OrderStatusValue.PARTIAL
            else:
                # No fills (e.g., LMT not yet reachable). Order stays
                # WORKING.
                internal_order.status = OrderStatusValue.WORKING
        else:
            # No price available — order stays WORKING, will fill once
            # a price is set (next evolve_pending_state()).
            internal_order.status = OrderStatusValue.WORKING

        return self._order_to_result(internal_order, idempotent=False)

    def get_order_status(self, client_order_id: str) -> OrderStatus:
        self._require_connected()
        order = self._orders.get(client_order_id)
        if order is None:
            return OrderStatus(
                client_order_id=client_order_id,
                broker_order_id=None,
                status=OrderStatusValue.UNKNOWN,
                filled_quantity=Decimal("0"),
                remaining_quantity=Decimal("0"),
            )
        return OrderStatus(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
            average_fill_price=order.average_fill_price,
            rejection_reason=order.rejection_reason,
        )

    def cancel_order(self, client_order_id: str) -> None:
        self._require_connected()
        order = self._orders.get(client_order_id)
        if order is None:
            # Absence is a legitimate state per protocol contract.
            return
        if order.status in (
            OrderStatusValue.FILLED,
            OrderStatusValue.CANCELLED,
            OrderStatusValue.REJECTED,
        ):
            # Already in a terminal state. No-op.
            return
        order.status = OrderStatusValue.CANCELLED

    # -------------------------------------------------------------------------
    # Group 4: ACH (recurring monthly transfer)
    # -------------------------------------------------------------------------

    def get_recurring_ach(self) -> RecurringAchInfo:
        self._require_connected()
        if self._recurring_ach is None:
            return RecurringAchInfo(
                is_configured=False,
                amount_dollars=None,
                destination_reference=None,
                next_scheduled_date=None,
            )
        return self._recurring_ach.model_copy()

    def update_recurring_ach(self, new_amount_dollars: Decimal) -> AchUpdateResult:
        self._require_connected()
        if self._recurring_ach is None or not self._recurring_ach.is_configured:
            # Per design, IRAPM never sets up an ACH from scratch (I11) —
            # the operator does that one-time-portal-setup. So an update
            # against an unconfigured recurring ACH is a rejection.
            return AchUpdateResult(
                success=False,
                requested_amount_dollars=new_amount_dollars,
                confirmed_amount_dollars=None,
                rejection_reason=(
                    "No recurring ACH is configured at the broker. "
                    "Operator must set up the recurring transfer via the "
                    "broker portal before IRAPM can update its amount."
                ),
            )
        # Update succeeds; reflect the new amount.
        self._recurring_ach = self._recurring_ach.model_copy(update={
            "amount_dollars": new_amount_dollars,
        })
        return AchUpdateResult(
            success=True,
            requested_amount_dollars=new_amount_dollars,
            confirmed_amount_dollars=new_amount_dollars,
            rejection_reason=None,
        )

    # -------------------------------------------------------------------------
    # Group 5: miscellaneous
    # -------------------------------------------------------------------------

    def get_managed_account_id(self) -> str:
        self._require_connected()
        return self._account_id

    def get_server_time(self) -> datetime:
        self._require_connected()
        return self._clock_now_utc()

    # =========================================================================
    # SIMULATOR-FACING METHODS (not part of the protocol; for IPMS use)
    # =========================================================================

    def evolve_pending_state(self) -> None:
        """Process any pending state transitions that depend on the
        clock. Called by the IPMS simulator between cycles.

        Specifically:
          - Open WORKING orders may now be fillable if prices have moved
            (e.g., a LMT BUY that wasn't reachable last cycle now is).
          - Recurring ACH disbursements may have happened (if a
            next_scheduled_date is in the past, decrement cash by
            amount_dollars and advance the next_scheduled_date).
          - Unsettled cash may now be settled (T+1 simulation —
            simplistic: all unsettled cash becomes settled in this
            call; IPMS controls when this gets called relative to
            "settlement date" by its own clock advancement).

        This method is intentionally NOT in the protocol — it's a
        simulator hook, not part of the broker contract."""
        # 1. Fill any WORKING orders that are now reachable.
        now = self._clock_now_utc()
        for order in self._orders.values():
            if order.status != OrderStatusValue.WORKING:
                continue
            symbol_price = self._market_prices.get(order.symbol)
            if symbol_price is None or symbol_price.status != PriceStatus.OK:
                continue
            fills = self._fill_policy.compute_fills(
                order, symbol_price.price, now,  # type: ignore[arg-type]
            )
            if fills:
                for fill in fills:
                    order.fills.append(fill)
                self._apply_fills_to_account(order, fills)
                if order.filled_quantity == order.quantity_ordered:
                    order.status = OrderStatusValue.FILLED
                else:
                    order.status = OrderStatusValue.PARTIAL

        # 2. Settle unsettled cash. Simplistic — caller controls when.
        if self._cash_unsettled > 0:
            self._cash_settled += self._cash_unsettled
            self._cash_unsettled = Decimal("0")

        # 3. Process recurring ACH disbursements whose next_scheduled_date
        # has elapsed. Each disbursement decrements settled cash and
        # advances the schedule by 1 month.
        if (
            self._recurring_ach is not None
            and self._recurring_ach.is_configured
            and self._recurring_ach.next_scheduled_date is not None
        ):
            while self._recurring_ach.next_scheduled_date is not None \
                    and self._recurring_ach.next_scheduled_date <= now:
                amount = self._recurring_ach.amount_dollars
                assert amount is not None
                self._cash_settled -= amount
                # Advance scheduled date by ~1 month (30 days for
                # simplicity; the simulator can override this by
                # re-seeding the ACH).
                self._recurring_ach = self._recurring_ach.model_copy(update={
                    "next_scheduled_date": (
                        self._recurring_ach.next_scheduled_date
                        + timedelta(days=30)
                    ),
                })

    def snapshot(self) -> dict:
        """Return a deep-copy snapshot of all broker state, useful for
        test fixtures and debugging. Not part of the protocol."""
        return {
            "account_id": self._account_id,
            "connected": self._connected,
            "cash_settled": str(self._cash_settled),
            "cash_unsettled": str(self._cash_unsettled),
            "positions": {
                symbol: pos.model_dump(mode="json")
                for symbol, pos in self._positions.items()
            },
            "market_prices": {
                symbol: price.model_dump(mode="json")
                for symbol, price in self._market_prices.items()
            },
            "orders": {
                coid: {
                    "broker_order_id": o.broker_order_id,
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "order_type": o.order_type.value,
                    "quantity_ordered": str(o.quantity_ordered),
                    "limit_price": str(o.limit_price) if o.limit_price else None,
                    "submitted_at": o.submitted_at.isoformat(),
                    "status": o.status.value,
                    "filled_quantity": str(o.filled_quantity),
                    "fill_count": len(o.fills),
                }
                for coid, o in self._orders.items()
            },
            "recurring_ach": (
                self._recurring_ach.model_dump(mode="json")
                if self._recurring_ach else None
            ),
        }

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _order_to_result(
        self,
        order: _InternalOrder,
        *,
        idempotent: bool = False,
    ) -> OrderResult:
        return OrderResult(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=order.status,
            submitted_at=order.submitted_at,
            fills=list(order.fills),
            filled_quantity=order.filled_quantity,
            average_fill_price=order.average_fill_price,
            rejection_reason=order.rejection_reason,
            idempotent_rediscovery=idempotent,
        )

    def _apply_fills_to_account(
        self,
        order: _InternalOrder,
        new_fills: list[Fill],
    ) -> None:
        """Update positions and cash based on a list of fills.

        Conventions:
          - BUY fill: positions[symbol].quantity += fill.quantity
                      cash_unsettled -= fill.quantity * fill.price + commission
          - SELL fill: positions[symbol].quantity -= fill.quantity
                       cash_unsettled += fill.quantity * fill.price - commission

        Cash goes to UNSETTLED. evolve_pending_state() promotes
        unsettled → settled (T+1 simulation simplified).
        """
        symbol = order.symbol
        for fill in new_fills:
            notional = fill.quantity * fill.price
            commission = fill.commission

            if order.side == OrderSide.BUY:
                # Cash out
                self._cash_unsettled -= (notional + commission)
                # Position up
                existing = self._positions.get(symbol)
                if existing is None:
                    contract = ContractRef(broker_impl=BROKER_IMPL_TAG, symbol=symbol)
                    self._positions[symbol] = Position(
                        contract=contract,
                        symbol=symbol,
                        quantity=fill.quantity,
                        market_value=notional,
                        avg_cost=fill.price,
                    )
                else:
                    new_qty = existing.quantity + fill.quantity
                    # New avg_cost = (old_qty*old_avg + fill.qty*fill.price) / new_qty
                    new_avg_cost = (
                        (existing.quantity * existing.avg_cost
                         + fill.quantity * fill.price)
                        / new_qty
                    )
                    self._positions[symbol] = existing.model_copy(update={
                        "quantity": new_qty,
                        "market_value": new_qty * fill.price,
                        "avg_cost": new_avg_cost,
                    })

            elif order.side == OrderSide.SELL:
                # Cash in
                self._cash_unsettled += (notional - commission)
                # Position down
                existing = self._positions.get(symbol)
                if existing is None:
                    # Selling something we don't own — protocol allows
                    # this to be detected by the cycle logic via account
                    # math, not by the broker. Synthesize a negative
                    # position so the inconsistency is visible.
                    contract = ContractRef(broker_impl=BROKER_IMPL_TAG, symbol=symbol)
                    self._positions[symbol] = Position(
                        contract=contract,
                        symbol=symbol,
                        quantity=-fill.quantity,
                        market_value=-notional,
                        avg_cost=Decimal("0"),
                    )
                else:
                    new_qty = existing.quantity - fill.quantity
                    if new_qty == 0:
                        # Position fully closed — remove it (per
                        # get_positions() contract: zero positions
                        # not returned)
                        del self._positions[symbol]
                    else:
                        self._positions[symbol] = existing.model_copy(update={
                            "quantity": new_qty,
                            "market_value": new_qty * fill.price,
                            # avg_cost unchanged on sells
                        })


__all__ = [
    "SyntheticBroker",
    "FillPolicy",
    "BROKER_IMPL_TAG",
]
