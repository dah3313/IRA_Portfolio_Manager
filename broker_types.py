"""
IRAPM Broker Layer — public data types.

These are the plain data structures that cross the `Broker` protocol
boundary (see broker_protocol.py). The action layer and decision layer
work with these types exclusively; no broker-implementation type (no
`ib_async.Trade`, no `ib_async.Contract`, etc.) ever reaches the rest
of the codebase.

This file is part of the IRAPM/IPMS shared contract: SyntheticBroker
(in IPMS) and IBKRBroker (in IRAPM) both return these same types. If
something is missing here that one implementation needs to express,
the answer is to add it here, not to leak through.

DESIGN PRINCIPLES:

  - Money is `decimal.Decimal`, never `float`. Serialized to/from
    strings via Pydantic's Decimal handling.
  - Quantities (share counts) are also `Decimal`. Some symbols
    fractionalize (mutual funds historically did to 3 dp; ETF cash
    investing supports fractional shares); Decimal preserves precision
    where `float` would lose it.
  - Timestamps are timezone-aware `datetime` (UTC by convention; the
    broker implementation converts from broker-local time on the way
    in). Decisions in the cycle layer use the captured cycle clock
    (per cycle_attempt.py), not the timestamps in these types — these
    are for record-keeping and for the split-brain detection window.
  - `ContractRef` is intentionally opaque. The action layer obtains
    ContractRefs from `get_positions()` and passes them to
    `place_order()`; it never inspects them. Different broker
    implementations carry different payloads inside (IBKRBroker
    stores `(conId, exchange, currency)`; SyntheticBroker stores a
    symbol string). Keeping this opaque is what prevents IBKR-specific
    concepts from leaking through symbol-resolution logic.
  - `extra="forbid"` everywhere (typo defense, matches state_model.py
    convention).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# =============================================================================
# Opaque contract reference
# =============================================================================

class ContractRef(BaseModel):
    """Opaque handle that identifies a broker-side instrument.

    The action layer treats this as an opaque token: obtain it from
    `Broker.get_positions()` (or a future `resolve_symbol()` method),
    pass it back to `Broker.place_order()`, never inspect it.

    Different broker implementations populate `payload` with different
    structures. The protocol contract is only that round-tripping a
    ContractRef back to the same broker produces an equivalent result.

    Two ContractRefs are equal iff their (broker_impl, symbol) pair
    matches — this is the basis for cross-implementation diffing in
    sim-vs-real testing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    broker_impl: str = Field(
        ...,
        description=(
            "Identifier of the broker implementation that minted this ref "
            "(e.g., 'IBKRBroker', 'SyntheticBroker'). Used for safety check: "
            "passing a ref minted by one broker to a different broker's "
            "place_order() raises BrokerInconsistency."
        ),
    )

    symbol: str = Field(
        ...,
        description="Canonical IRAPM symbol (e.g., 'FBCG', 'SGOV', 'PYLD').",
    )

    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Broker-implementation-specific payload. IBKRBroker stores "
            "{'conId': int, 'exchange': str, 'currency': str}; "
            "SyntheticBroker may leave this empty. The action layer never "
            "reads this field; it's preserved across round-trips for the "
            "broker implementation's own use."
        ),
    )


# =============================================================================
# Position
# =============================================================================

class Position(BaseModel):
    """A single position held in the managed account.

    Returned by `Broker.get_positions()`. Zero-quantity positions are
    NOT returned (the broker layer filters them out before returning to
    avoid a class of confusing edge cases). If a symbol is not in the
    returned list, the account has no position in that symbol.
    """

    model_config = ConfigDict(extra="forbid")

    contract: ContractRef = Field(
        ...,
        description="Opaque handle for placing orders against this instrument.",
    )

    symbol: str = Field(
        ...,
        description="Canonical IRAPM symbol; convenience duplicate of contract.symbol.",
    )

    quantity: Decimal = Field(
        ...,
        description=(
            "Number of shares held. Always positive for IRAPM (long-only "
            "by spec invariant — short positions are out of scope). "
            "Fractional permitted; resolution depends on the instrument."
        ),
    )

    market_value: Decimal = Field(
        ...,
        description=(
            "Current market value in account currency (USD). Equals "
            "quantity × last_price as of the broker's most recent tick; "
            "may be stale by seconds. For the lookback signal and the "
            "decision layer, use get_prices() to fetch fresh prices."
        ),
    )

    avg_cost: Decimal = Field(
        ...,
        description=(
            "Average acquisition cost per share. Used only as a *fallback* "
            "price source by IRAPM when get_prices() returns UNAVAILABLE "
            "for this symbol AND the position is non-zero; this fallback "
            "is signal-degraded (the cycle still runs but logs a Warning). "
            "Never used as the primary price for decision-making."
        ),
    )

    @field_validator("quantity", "market_value", "avg_cost", mode="before")
    @classmethod
    def _parse_decimal(cls, v: object) -> Decimal:
        # Accept Decimal, int, or str; reject float (float→Decimal
        # conversion introduces representation artifacts). Mirrors the
        # parse_decimal pattern in ruleset_model.py.
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"Position numeric fields must be Decimal/int/str, got {type(v).__name__}. "
            "Float values are forbidden — they introduce representation artifacts. "
            "If the broker library returned a float, convert with Decimal(str(value))."
        )


# =============================================================================
# Price
# =============================================================================

class PriceStatus(str, Enum):
    """Outcome of a price query."""

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    """No price could be obtained (market closed for new instruments,
    feed subscription missing, etc.). The cycle treats this as a
    degraded-signal condition; the lookback signal halts CB transitions
    when prices are UNAVAILABLE."""


class Price(BaseModel):
    """A single price quote.

    The `as_of` timestamp is the broker's reported quote time. Stale
    quotes are not automatically rejected at the broker layer — staleness
    judgment is a decision-layer concern (e.g., the lookback signal's
    staleness gate in §5.4). The broker layer reports what it got.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="Canonical IRAPM symbol.")

    status: PriceStatus = Field(
        ...,
        description=(
            "OK if a usable price was obtained; UNAVAILABLE otherwise. "
            "Fields below are populated only when status == OK."
        ),
    )

    price: Optional[Decimal] = Field(
        default=None,
        description=(
            "Last-trade price if available, else mid-quote ((bid+ask)/2). "
            "Decimal; never float. Populated only when status == OK."
        ),
    )

    as_of: Optional[datetime] = Field(
        default=None,
        description=(
            "Timestamp (UTC, timezone-aware) of the price. Broker-reported; "
            "may differ from current wall-clock time. Populated only when "
            "status == OK."
        ),
    )

    @field_validator("price", mode="before")
    @classmethod
    def _parse_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"Price.price must be Decimal/int/str/None, got {type(v).__name__}."
        )


# =============================================================================
# Account summary
# =============================================================================

class AccountSummary(BaseModel):
    """Account-level cash and value figures.

    Returned by `Broker.get_account_summary()`. Reflects the broker's
    most recent snapshot; refreshed on every `connect()` per the
    per-cycle connection lifecycle.

    NAMING NOTE: IBKR uses several similar-sounding cash fields
    ('TotalCashValue', 'SettledCash', 'CashBalance', 'AvailableFunds',
    'BuyingPower'). This summary picks the four that map cleanly to
    IRAPM cycle decisions and ignores the rest. If a future need
    requires a field that's not here, add it explicitly with a docstring
    explaining what cycle decision uses it.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(
        ...,
        description=(
            "Broker's account identifier (e.g., IBKR account number 'U1234567'). "
            "Cross-checked at connect time against the configured expected "
            "account ID — connecting to the wrong account raises "
            "BrokerInconsistency at startup."
        ),
    )

    total_cash_value: Decimal = Field(
        ...,
        description=(
            "Total cash balance, settled + unsettled. This is the figure "
            "the cycle uses for cash-bucket math (§7.7). Note this includes "
            "SGOV buffer if buffer happens to be held as cash awaiting "
            "deployment (rare; normally SGOV is in shares)."
        ),
    )

    settled_cash: Decimal = Field(
        ...,
        description=(
            "Settled portion of cash (T+1 settlement complete). This is "
            "what's available for ACH withdrawal per §9.6.1's settlement "
            "rule. The 4-business-day gap between SELL and ACH ensures "
            "settled_cash ≥ withdrawal_amount by the ACH date."
        ),
    )

    unsettled_cash: Decimal = Field(
        ...,
        description=(
            "Cash from trades pending settlement. Informational. "
            "total_cash_value = settled_cash + unsettled_cash (subject to "
            "broker rounding; reconciliation tolerance per §7.7)."
        ),
    )

    buying_power: Decimal = Field(
        ...,
        description=(
            "Broker's reported buying power for new purchases. For an "
            "IRA account with cash-only trading, this equals settled_cash "
            "minus pending purchases. Used as a sanity check before "
            "BUY orders; an attempted BUY exceeding buying_power triggers "
            "BrokerRejection."
        ),
    )

    @field_validator(
        "total_cash_value", "settled_cash", "unsettled_cash", "buying_power",
        mode="before",
    )
    @classmethod
    def _parse_decimal(cls, v: object) -> Decimal:
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"AccountSummary numeric fields must be Decimal/int/str, "
            f"got {type(v).__name__}."
        )


# =============================================================================
# Order placement
# =============================================================================

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MKT = "MKT"
    """Market order. Fills at next available price. Used by IRAPM for
    withdrawal SELLs and refill BUYs where price-certainty is less
    important than execution-certainty."""

    LMT = "LMT"
    """Limit order with operator-specified price. IRAPM uses LMT for
    rebalance trades to bound execution price; the spec §7.5 details
    the limit-price calculation."""


class TimeInForce(str, Enum):
    DAY = "DAY"
    """Order expires at end of trading day. IRAPM default — unfilled
    orders are re-decided on the next cycle against fresh state, so
    GTC orders create stale-decision risk."""

    GTC = "GTC"
    """Good-til-cancelled. NOT used by current IRAPM cycle logic;
    listed for protocol completeness and future operational manual
    use. If the action layer ever generates GTC, it's a bug."""


class OrderStatusValue(str, Enum):
    """High-level order lifecycle states.

    These are IRAPM's abstraction over broker-specific states. The
    IBKRBroker implementation maps TWS states to these as follows:
        PENDING    ← PendingSubmit, PreSubmitted, ApiPending
        WORKING    ← Submitted, ApiCancel
        FILLED     ← Filled (with totalQty == filledQty)
        PARTIAL    ← Filled (with filledQty < totalQty)
        CANCELLED  ← Cancelled, ApiCancelled
        REJECTED   ← Inactive (with rejection reason)
        UNKNOWN    ← anything else (raises BrokerInconsistency in
                     callers that expect a known state)

    UNCERTAINTY FLAG: TWS occasionally reports 'Inactive' status with no
    rejection reason for orders the IBKR risk system silently filtered.
    The IBKRBroker implementation treats unexplained Inactive as
    REJECTED with reason='UNKNOWN_INACTIVE'; this needs paper-trading
    verification across order types (MKT, LMT) and account types
    (cash vs margin) before production trust.
    """

    PENDING = "PENDING"
    WORKING = "WORKING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class Fill(BaseModel):
    """A single execution against an order.

    A single order may produce multiple Fills (partial fills, multiple
    routes). Aggregating fills to compute the order's overall fill state
    is the broker implementation's job; the action layer reads
    OrderResult.filled_quantity and OrderResult.average_fill_price.
    """

    model_config = ConfigDict(extra="forbid")

    fill_id: str = Field(
        ...,
        description=(
            "Broker-side execution identifier. For IBKR this is the 'execId' "
            "field — unique across the account's history. Used for "
            "deduplication when reading executions across cycles."
        ),
    )

    fill_time: datetime = Field(
        ...,
        description="Timestamp of the fill (UTC, timezone-aware).",
    )

    quantity: Decimal = Field(
        ...,
        description="Shares filled in this execution.",
    )

    price: Decimal = Field(
        ...,
        description="Per-share execution price.",
    )

    commission: Decimal = Field(
        default=Decimal("0"),
        description=(
            "Broker commission charged for this fill. IBKR IRA accounts "
            "are typically commission-free for ETFs, so this is usually "
            "zero, but the field exists for completeness and to support "
            "broker implementations where it's non-zero."
        ),
    )

    @field_validator("quantity", "price", "commission", mode="before")
    @classmethod
    def _parse_decimal(cls, v: object) -> Decimal:
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"Fill numeric fields must be Decimal/int/str, got {type(v).__name__}."
        )


class OrderResult(BaseModel):
    """Result of a `Broker.place_order()` call.

    Returned in two scenarios:
      1. Fresh order: broker accepted the submission and confirmed the
         order is in its open-orders table.
      2. Idempotent re-discovery: broker found an existing order with
         the same client_order_id (from a previous cycle attempt) and
         returns its current state.

    The `idempotent_rediscovery` flag distinguishes these — useful for
    logging and split-brain forensics. A True value here means "we did
    NOT submit a new order in this call; we found an existing one."
    """

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(
        ...,
        description=(
            "The deterministic client-side identifier the caller supplied. "
            "Format expectation: 'cycle-{cycle_uuid}-{plan_entry_index}-"
            "{symbol}-{side}'. The broker implementation stores this in "
            "the broker-side order record (for IBKR: the orderRef field) "
            "so subsequent cycles can re-discover the order."
        ),
    )

    broker_order_id: Optional[str] = Field(
        default=None,
        description=(
            "Broker-side identifier (for IBKR: the integer orderId, "
            "stringified). May be None for synthetic implementations. "
            "Useful for operator forensics ('look up order #4017 in IBKR's "
            "trade log'); not used by cycle logic."
        ),
    )

    status: OrderStatusValue = Field(
        ...,
        description="Current order state at the time place_order() returned.",
    )

    submitted_at: datetime = Field(
        ...,
        description=(
            "Timestamp the order was submitted to the broker (UTC). For "
            "idempotent rediscoveries, this is the *original* submission "
            "timestamp, not the rediscovery time."
        ),
    )

    fills: list[Fill] = Field(
        default_factory=list,
        description=(
            "Executions accumulated against this order at the time of "
            "return. Empty if the order is PENDING or WORKING without "
            "any fills yet. Sorted by fill_time ascending."
        ),
    )

    filled_quantity: Decimal = Field(
        default=Decimal("0"),
        description="Sum of fill quantities. Convenience field; equals sum(f.quantity for f in fills).",
    )

    average_fill_price: Optional[Decimal] = Field(
        default=None,
        description=(
            "Volume-weighted average fill price across all fills. None if "
            "no fills yet."
        ),
    )

    rejection_reason: Optional[str] = Field(
        default=None,
        description=(
            "Free-form broker-supplied rejection reason. Populated only "
            "when status == REJECTED. Operator-readable; not parsed by "
            "cycle logic. For IBKR, this is the TWS errorMsg string from "
            "the error callback that accompanied the rejection."
        ),
    )

    idempotent_rediscovery: bool = Field(
        default=False,
        description=(
            "True if place_order() found an existing order with this "
            "client_order_id and returned it instead of submitting a new "
            "one. Indicates a previous cycle attempt placed this order; "
            "the current cycle is reconciling against that prior work."
        ),
    )

    @field_validator(
        "filled_quantity", "average_fill_price", mode="before",
    )
    @classmethod
    def _parse_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"OrderResult numeric fields must be Decimal/int/str/None, "
            f"got {type(v).__name__}."
        )


# =============================================================================
# Order status (lighter-weight than OrderResult; for status polling)
# =============================================================================

class OrderStatus(BaseModel):
    """Lightweight status snapshot of an order.

    Returned by `Broker.get_order_status(client_order_id)`. Distinct
    from OrderResult: this is for *polling* — repeated calls during a
    cycle to check whether an order has filled — and omits the full
    fills list to keep the response small.

    For full fill detail, call `get_recent_activity()` and find the
    order by client_order_id in its executions list.
    """

    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    broker_order_id: Optional[str] = None
    status: OrderStatusValue
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    average_fill_price: Optional[Decimal] = None
    rejection_reason: Optional[str] = None

    @field_validator(
        "filled_quantity", "remaining_quantity", "average_fill_price",
        mode="before",
    )
    @classmethod
    def _parse_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"OrderStatus numeric fields must be Decimal/int/str/None, "
            f"got {type(v).__name__}."
        )


# =============================================================================
# Recent activity (the split-brain defense queryable)
# =============================================================================

class RecentActivity(BaseModel):
    """Aggregate view of the account's recent order/execution activity.

    Returned by `Broker.get_recent_activity(since=)`. This is the
    canonical query used by:

      1. The pre-place-order conflict check (split-brain defense layer 3).
         The action layer queries this before placing any order and
         checks: does any open or recently-executed order overlap with
         what we're about to place?

      2. The external-activity detector. If the activity list contains
         orders whose client_order_id doesn't match the IRAPM-controlled
         namespace (`cycle-{uuid}-...`), the cycle treats this as
         external operator activity per §11.2.15: cycle aborts before
         placing any orders and an `external_activity_overlap` Critical
         alert fires (alert only, NO operational_pause — the cycle's
         declination to act is the heal, per D-SPEC-8).

      3. Idempotent order rediscovery inside IBKRBroker. The internal
         _find_order_by_ref helper aggregates these same data structures
         to determine whether place_order should submit a new order or
         return an existing one.

    The single-query design (returning open_orders + completed_orders +
    fills together) avoids race conditions between three separate queries
    that could see an order in different states.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: datetime = Field(
        ...,
        description=(
            "Broker's snapshot timestamp (UTC). All three lists below "
            "represent state at this moment. Subsequent broker activity "
            "(orders filling, new orders) is not reflected."
        ),
    )

    open_orders: list[OrderResult] = Field(
        default_factory=list,
        description=(
            "Orders currently open at the broker (PENDING, WORKING, or "
            "PARTIAL fills still active). Includes orders placed by IRAPM "
            "this cycle, previous IRAPM cycles, and any external source. "
            "Sorted by submitted_at ascending."
        ),
    )

    recently_completed_orders: list[OrderResult] = Field(
        default_factory=list,
        description=(
            "Orders completed (filled, cancelled, or rejected) since the "
            "`since` parameter passed to get_recent_activity(). Sorted "
            "by submitted_at ascending."
        ),
    )

    recent_fills: list[Fill] = Field(
        default_factory=list,
        description=(
            "Individual executions since `since`, across all orders. "
            "An order with three partial fills contributes three entries "
            "here. Sorted by fill_time ascending."
        ),
    )


# =============================================================================
# Recurring ACH
# =============================================================================

class RecurringAchInfo(BaseModel):
    """Broker-side state of the recurring monthly ACH transfer.

    Per §7.9: the recurring transfer is configured at the broker; IRAPM
    sends ACHScheduleUpdate plan entries to change the amount; the
    actual disbursement happens broker-side without IRAPM involvement.

    Returned by `Broker.get_recurring_ach()`. Compared to IRAPM's own
    record of "what we last set the amount to" to detect drift.
    """

    model_config = ConfigDict(extra="forbid")

    is_configured: bool = Field(
        ...,
        description=(
            "True if a recurring ACH is set up at the broker (regardless "
            "of amount). False if no recurring ACH exists at all; this "
            "is the unconfigured state at initial deployment and "
            "requires operator action (one-time portal setup)."
        ),
    )

    amount_dollars: Optional[Decimal] = Field(
        default=None,
        description=(
            "Current scheduled monthly transfer amount. None if "
            "is_configured == False."
        ),
    )

    destination_reference: Optional[str] = Field(
        default=None,
        description=(
            "Broker-side identifier of the destination bank account "
            "(NOT the full account number — IBKR shows a redacted form "
            "like 'CHASE ****1234'). For operator-readable display in "
            "alerts and runbook diagnostics. None if is_configured == False."
        ),
    )

    next_scheduled_date: Optional[datetime] = Field(
        default=None,
        description=(
            "Date (UTC midnight) of the next scheduled disbursement. None "
            "if is_configured == False. Used to verify the cycle's "
            "withdrawal step has placed its SELLs early enough for T+1 "
            "settlement per §9.6.1."
        ),
    )

    @field_validator("amount_dollars", mode="before")
    @classmethod
    def _parse_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"RecurringAchInfo.amount_dollars must be Decimal/int/str/None, "
            f"got {type(v).__name__}."
        )


class AchUpdateResult(BaseModel):
    """Result of `Broker.update_recurring_ach(new_amount)`.

    UNCERTAINTY FLAG: IBKR's API surface for updating the recurring
    ACH amount has historically been limited compared to the portal.
    The IBKRBroker implementation needs verification of:
      - Which exact API call (or sequence) sets the recurring amount
      - What rejection codes can be returned and what they mean
      - Whether updates are immediate or batch-processed
      - What happens if an update is submitted between disbursement
        date and 'today' (i.e., the gray zone)
    Until verified against paper trading, the IBKRBroker implementation
    of this method conservatively treats any non-success response as
    BrokerRejection requiring operator review.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(
        ...,
        description=(
            "True if the broker confirmed the amount change. False if "
            "the broker rejected or the update couldn't be verified."
        ),
    )

    requested_amount_dollars: Decimal = Field(
        ...,
        description="The amount IRAPM asked the broker to set.",
    )

    confirmed_amount_dollars: Optional[Decimal] = Field(
        default=None,
        description=(
            "The amount the broker confirmed is now in effect. Should "
            "equal requested_amount_dollars on success; may be None on "
            "failure or if the broker's response was ambiguous."
        ),
    )

    rejection_reason: Optional[str] = Field(
        default=None,
        description=(
            "Free-form rejection reason from the broker. Populated only "
            "when success == False. Operator-readable."
        ),
    )

    @field_validator(
        "requested_amount_dollars", "confirmed_amount_dollars",
        mode="before",
    )
    @classmethod
    def _parse_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"AchUpdateResult numeric fields must be Decimal/int/str/None, "
            f"got {type(v).__name__}."
        )


__all__ = [
    # Opaque handle
    "ContractRef",
    # Position/price/account
    "Position",
    "PriceStatus",
    "Price",
    "AccountSummary",
    # Orders
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "OrderStatusValue",
    "Fill",
    "OrderResult",
    "OrderStatus",
    # Activity
    "RecentActivity",
    # ACH
    "RecurringAchInfo",
    "AchUpdateResult",
]
