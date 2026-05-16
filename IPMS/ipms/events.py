"""
events.py — typed event taxonomy captured by the state collector.

Public exports:
    Event (base class) — common timestamp + event-type machinery
    Snapshot — per-tick portfolio state
    CashFlow — every cash-balance-changing event with source attribution
    WithdrawalRecord — monthly withdrawal attempt (success, partial, halt)
    CBEvent — circuit breaker state transition
    RecoveryEvent — recovery stage transition
    RebalanceRecord — rebalance trigger and resulting trades
    CascadeTransition — cascade tier shift (1→2, 2→3, 2→1)
    AnnualReview — annual review decision
    Alert — alert that would have been emitted (recorded but not transmitted)
    PhaseTransition — Phase 2 transition execution
    GrowthIndexUpdate — synthetic price index sample

    EventType (enum) — discriminator for serialization
    CashFlowSource (enum) — typed source attribution for cash flows
    CascadeTier (enum) — 1=buffer, 2=FI, 3=growth
    CBId (enum) — cb1, cb2

See also: IPMS_SPECIFICATION.md §4.2 (state collector captured types)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


# ============================================================================
# DISCRIMINATOR ENUMS
# ============================================================================

class EventType(str, Enum):
    """
    Discriminator for serializing events. Each Event subclass declares its
    EventType so a heterogeneous list of events can be written to a single
    log file (JSONL or similar) and re-read with type-safe dispatch.

    String values rather than auto-numbered so output files are
    human-readable and tool-portable.
    """
    SNAPSHOT = "snapshot"
    CASH_FLOW = "cash_flow"
    WITHDRAWAL = "withdrawal"
    CB_EVENT = "cb_event"
    RECOVERY_EVENT = "recovery_event"
    REBALANCE = "rebalance"
    CASCADE_TRANSITION = "cascade_transition"
    ANNUAL_REVIEW = "annual_review"
    ALERT = "alert"
    PHASE_TRANSITION = "phase_transition"
    GROWTH_INDEX_UPDATE = "growth_index_update"


class CashFlowSource(str, Enum):
    """
    Typed source attribution for every cash-balance-changing event.

    Why this exists: cash-flow anomaly detection per spec §5.4. If the
    snapshot's cash-balance change does not equal the sum of attributed
    CashFlow events for the period, an event is missing — which is
    either an IRA PM bug or a simulator-collection bug. Either case
    needs to be visible.

    The seven attribution categories cover every legitimate cash mover
    in the IRA PM. New mechanisms that touch cash MUST add a category
    here or be classified under one of the existing ones; "miscellaneous
    untracked" is deliberately not an option.
    """
    WITHDRAWAL_DISBURSEMENT = "withdrawal_disbursement"      # money leaves IBKR to bank
    REFILL_SELL_PROCEEDS = "refill_sell_proceeds"            # SGOV refill SELL settles to cash
    REFILL_BUY_OUTFLOW = "refill_buy_outflow"                # SGOV refill BUY consumes cash
    REBALANCE_NET_IN = "rebalance_net_in"                    # rebalance produced net cash inflow
    REBALANCE_NET_OUT = "rebalance_net_out"                  # rebalance consumed net cash
    DIVIDEND = "dividend"                                    # if/when IRA PM models dividends
    PHASE_TRANSITION_NET = "phase_transition_net"            # Phase 2 transition cash delta
    MANUAL_SEED = "manual_seed"                              # initial portfolio seeding by simulator


class CascadeTier(int, Enum):
    """
    Withdrawal source layer. Matches IRA PM's tier numbering.
    Stored as int so output files render `1`/`2`/`3` not `tier_1_buffer`.
    """
    BUFFER = 1
    FIXED_INCOME = 2
    GROWTH = 3


class CBId(str, Enum):
    """
    Circuit breaker identifier. Used in CB and recovery events.
    """
    CB1 = "cb1"
    CB2 = "cb2"


# ============================================================================
# BASE EVENT
# ============================================================================

@dataclass(frozen=True)
class Event:
    """
    Base class for all collected events.

    Every event has a timestamp (as date — daily resolution is the
    finest granularity the IRA PM operates at) and declares its
    EventType for serialization dispatch.

    `event_type` is a class-level constant on each subclass; the
    base-class declaration is overridden by every concrete subclass.
    Frozen so events cannot be mutated after collection — once the
    collector captures it, it's immutable history.
    """
    timestamp: date

    # Subclasses override. Base class doesn't declare a sensible default;
    # any direct instantiation of Event would be a programming error.
    event_type: EventType = field(init=False)


# ============================================================================
# SNAPSHOT
# ============================================================================

@dataclass(frozen=True)
class AssetBalance:
    """
    Single asset's balance at a snapshot timestamp.

    `weight` is the asset's value as fraction of managed_net_liq, NOT
    of gross_net_liq. This matches the IRA PM's CB-trigger denominator.
    Weight as fraction (0.4459), not percentage (44.59%) — the file
    formatter handles human-display conversion.
    """
    symbol: str
    quantity: Decimal
    market_price: Decimal
    market_value: Decimal
    weight: Decimal
    asset_class: str       # "fixed_income" or "growth"


@dataclass(frozen=True)
class Snapshot(Event):
    """
    Per-tick portfolio state. The most-frequently-emitted event type;
    one per output-cadence tick (default monthly) plus one at every
    state-changing event regardless of cadence.

    Field set matches spec §4.2 captured-state requirements. CB and
    recovery state are captured as string enum values rather than
    typed enums to keep the snapshot serialization-friendly without
    pulling in the full IRA PM enum types.
    """
    event_type: EventType = field(init=False, default=EventType.SNAPSHOT)

    # Asset-level state
    assets: dict[str, AssetBalance] = field(default_factory=dict)

    # Cash and buffer
    cash_value: Decimal = Decimal(0)
    sgov_buffer_value: Decimal = Decimal(0)
    sgov_buffer_quantity: Decimal = Decimal(0)
    sgov_buffer_price: Decimal = Decimal(0)

    # Aggregate values
    gross_net_liq: Decimal = Decimal(0)         # managed + cash + buffer + unmanaged
    managed_net_liq: Decimal = Decimal(0)       # managed + cash (CB-trigger denominator)
    fi_bucket_value: Decimal = Decimal(0)
    growth_bucket_value: Decimal = Decimal(0)
    fi_weight: Decimal = Decimal(0)
    growth_weight: Decimal = Decimal(0)

    # State machine flags (string values, not typed enums, for serialization simplicity)
    cb1_state: str = "inactive"      # inactive / pending_trigger / active / pending_recovery
    cb2_state: str = "inactive"
    refill_active: bool = False
    cascade_tier: int = 2            # default tier 2 (FI) when no CB2

    # Phase
    phase: int = 1                   # 1 or 2

    # Growth synthetic price index value at this tick (used by dry powder
    # and, post-2026-05-09 patch, by CB triggers).
    growth_synthetic_index: Optional[Decimal] = None


# ============================================================================
# CASH FLOW
# ============================================================================

@dataclass(frozen=True)
class CashFlow(Event):
    """
    Every cash-balance-changing event. Captured separately from snapshots
    so the formatter can aggregate per-month inflows and outflows for
    `balances_monthly.md`'s cash_flow_in / cash_flow_out columns.

    `amount` is always positive; the sign is implicit in `direction`.
    This avoids ambiguity in summation ("did I forget to negate?").
    """
    event_type: EventType = field(init=False, default=EventType.CASH_FLOW)

    direction: str = "in"            # "in" or "out"
    amount: Decimal = Decimal(0)
    source: CashFlowSource = CashFlowSource.MANUAL_SEED
    description: Optional[str] = None    # free-text context if useful


# ============================================================================
# WITHDRAWAL
# ============================================================================

@dataclass(frozen=True)
class WithdrawalRecord(Event):
    """
    One per monthly withdrawal attempt. Includes successful, partial,
    and halted attempts.

    `halt_reason` is None for successful or partial withdrawals; for
    halts, it carries the IRA PM's halt-reason enum value as a string
    (e.g., "cb2_active", "cascade_exhausted", "phase_2_cessation").
    The IPMV1's defective halt reasons (CB2_ACTIVE without cascade
    routing, FI_FLOOR_BREACHED) should not appear in IRA PM output —
    if they do, that is a regression signal.
    """
    event_type: EventType = field(init=False, default=EventType.WITHDRAWAL)

    scheduled_date: date = field(default_factory=lambda: date(1970, 1, 1))
    target_amount: Decimal = Decimal(0)
    filled_amount: Decimal = Decimal(0)
    source_asset: Optional[str] = None
    source_tier: Optional[CascadeTier] = None
    halt_reason: Optional[str] = None
    trueup_balance_after: Decimal = Decimal(0)
    cumulative_withdrawn: Decimal = Decimal(0)


# ============================================================================
# CB EVENTS
# ============================================================================

@dataclass(frozen=True)
class CBEvent(Event):
    """
    Circuit breaker state transition. One per transition, including
    pending-set, activated, pending-recovery-set, and recovered.

    `from_state` and `to_state` are string state values. `rolling_return`
    is the trigger signal value at the moment of transition (the new
    growth-bucket synthetic 6-month signal, post-2026-05-09 patch).
    `trigger_portfolio_value` is recorded only at the activated
    transition — Optional for all others.
    """
    event_type: EventType = field(init=False, default=EventType.CB_EVENT)

    cb_id: CBId = CBId.CB1
    from_state: str = "inactive"
    to_state: str = "pending_trigger"
    rolling_return: Optional[Decimal] = None
    trigger_portfolio_value: Optional[Decimal] = None
    confirmation_week_count: int = 0


@dataclass(frozen=True)
class RecoveryEvent(Event):
    """
    Recovery stage transition. Stage 1 (resume rebalancing) and Stage 2
    (resume withdrawals + dispatch refill).

    `cb_id` indicates which CB is recovering. `stage` is 1 or 2.
    `confirmed` is True for the actual recovery confirmation; False for
    the pending-recovery-entry transition.
    """
    event_type: EventType = field(init=False, default=EventType.RECOVERY_EVENT)

    cb_id: CBId = CBId.CB1
    stage: int = 1
    confirmed: bool = False
    rolling_return: Optional[Decimal] = None


# ============================================================================
# REBALANCE
# ============================================================================

@dataclass(frozen=True)
class TradeLine:
    """
    One trade within a rebalance record. Mirrors the IRA PM's
    TradeInstruction shape but as an immutable record.
    """
    symbol: str
    action: str                # "BUY" or "SELL"
    shares: Decimal
    price: Decimal
    dollar_amount: Decimal
    outcome: str               # "filled", "partial", "failed"


@dataclass(frozen=True)
class RebalanceRecord(Event):
    """
    One per rebalance trigger. The trades list captures everything that
    fired as a result of this trigger; for SGOV refill, this includes
    BOTH the SELL leg and the BUY leg (the IRA PM correctly pairs them,
    unlike IPMV1).

    `trigger_type` taxonomy:
        - "5/25"                     standard 5/25 band rebalance
        - "annual_fallback"          annual rebalance when no 5/25 fired
        - "dry_powder_deploy"        Phase 2 dry-powder deploys
        - "dry_powder_refill"        Phase 2 dry-powder refills
        - "sgov_buffer_refill"       SGOV buffer refill (paired SELL+BUY)
        - "phase_2_transition"       one-shot at 8-year anniversary
    """
    event_type: EventType = field(init=False, default=EventType.REBALANCE)

    trigger_type: str = "5/25"
    trigger_reason: Optional[str] = None
    trades: list[TradeLine] = field(default_factory=list)
    outcome: str = "filled"        # joint outcome across all trades


# ============================================================================
# CASCADE TRANSITION
# ============================================================================

@dataclass(frozen=True)
class CascadeTransition(Event):
    """
    Cascade tier shift. Captures the moment cascade-source-selection
    moves between tiers. Used by `cascade_log.md` Section 2.

    Direction is encoded by `from_tier` and `to_tier`. Common cases:
        1 → 2: buffer empty, cascading to FI
        2 → 3: FI exhausted (per-position residual), cascading to Growth
        2 → 1: CB2 fired or recovered to buffer-engagement
        3 → 2: Growth recovered enough to leave residual, back to FI
                (rare but possible; recorded for completeness)
    """
    event_type: EventType = field(init=False, default=EventType.CASCADE_TRANSITION)

    from_tier: CascadeTier = CascadeTier.FIXED_INCOME
    to_tier: CascadeTier = CascadeTier.FIXED_INCOME
    trigger_event: Optional[str] = None
    days_since_cb2_trigger: Optional[int] = None

    # Snapshot of all positions at transition time, for forensic analysis.
    # Map symbol → balance. SGOV included as well as managed assets.
    position_balances_at_transition: dict[str, Decimal] = field(default_factory=dict)


# ============================================================================
# ANNUAL REVIEW
# ============================================================================

@dataclass(frozen=True)
class AnnualReview(Event):
    """
    January annual review decision. One per year while in Phase 1.
    Phase 2 skips review entirely (no withdrawals to adjust).

    `outcome` taxonomy:
        - "seeded"                first-ever review, captures reference rate
        - "cpi_raise"             default outcome, target raised by CPI
        - "guardrail_cut"         effective rate exceeded upper band, target cut 5%
        - "guardrail_bypassed"    effective rate exceeded but operator opted out

    `cpi_rate_applied` is None for guardrail_cut and guardrail_bypassed
    (no CPI raise applied that year).
    """
    event_type: EventType = field(init=False, default=EventType.ANNUAL_REVIEW)

    outcome: str = "seeded"
    pre_target: Decimal = Decimal(0)
    post_target: Decimal = Decimal(0)
    effective_rate: Optional[Decimal] = None
    reference_rate: Optional[Decimal] = None
    guardrail_band_low: Optional[Decimal] = None
    guardrail_band_high: Optional[Decimal] = None
    cpi_rate_applied: Optional[Decimal] = None


# ============================================================================
# PHASE TRANSITION
# ============================================================================

@dataclass(frozen=True)
class PhaseTransition(Event):
    """
    Phase 2 transition execution. Single event per simulator run (or
    zero events if the run window doesn't reach the anniversary).

    `deferred_by_cb` is True if the transition was scheduled at the
    anniversary but waited for CBs to clear; False if it fired on time.
    `forced_after_max_delay` is True if the IRA PM's max-delay backstop
    fired (production: 6 months past anniversary, force transition
    regardless of CB state).

    Trades are listed in the embedded RebalanceRecord (trigger_type
    "phase_2_transition") so this event records the metadata only.
    """
    event_type: EventType = field(init=False, default=EventType.PHASE_TRANSITION)

    scheduled_date: date = field(default_factory=lambda: date(1970, 1, 1))
    deferred_by_cb: bool = False
    forced_after_max_delay: bool = False

    # Pre/post allocation as symbol → target weight maps
    pre_allocation: dict[str, Decimal] = field(default_factory=dict)
    post_allocation: dict[str, Decimal] = field(default_factory=dict)


# ============================================================================
# ALERT
# ============================================================================

@dataclass(frozen=True)
class Alert(Event):
    """
    Alert that would have been emitted by the IRA PM. The simulator
    records these but does not transmit. Captured so post-run analysis
    can verify that the IRA PM fires the right alerts at the right
    times.

    `event_name` is the IRA PM's alert event identifier (e.g.,
    "cb2_triggered", "withdrawal_buffer_engaged"). `severity` is the
    IRA PM's severity level. `channels` lists which channels the alert
    would have fired on (typically subset of ["sms", "email"]).

    `ctx_snapshot` is a small dict of key portfolio values at fire time
    (portfolio value, withdrawal amount, etc.) — whatever the IRA PM's
    AlertContext carried.
    """
    event_type: EventType = field(init=False, default=EventType.ALERT)

    event_name: str = ""
    severity: str = "info"
    channels: list[str] = field(default_factory=list)
    ctx_snapshot: dict = field(default_factory=dict)


# ============================================================================
# GROWTH INDEX UPDATE
# ============================================================================

@dataclass(frozen=True)
class GrowthIndexUpdate(Event):
    """
    Synthetic price-index sample. Captured per-tick so the rolling-
    return signal driving CB triggers (post-2026-05-09 patch) and
    dry-powder triggers can be reconstructed exactly.

    Stored as the index value at the tick plus the per-asset prices
    that produced that value. The IRA PM's
    `_update_growth_price_index` produces these; the simulator
    captures them as it runs.
    """
    event_type: EventType = field(init=False, default=EventType.GROWTH_INDEX_UPDATE)

    index_value: Decimal = Decimal(0)
    component_prices: dict[str, Decimal] = field(default_factory=dict)


# ============================================================================
# TYPE UNION
# ============================================================================

# Type alias for "any event the collector might hold". Used in the
# collector's storage type hint and in functions that need to dispatch
# on event type. Keep this list in sync as new event types are added.
AnyEvent = (
    Snapshot
    | CashFlow
    | WithdrawalRecord
    | CBEvent
    | RecoveryEvent
    | RebalanceRecord
    | CascadeTransition
    | AnnualReview
    | Alert
    | PhaseTransition
    | GrowthIndexUpdate
)
