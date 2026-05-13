"""
plan_model.py — Plan and entry types (§3.12, §7).

PURPOSE:
    A Plan is a structured, serializable description of what the system
    intends to do this cycle. The decision layer produces a Plan; the
    action layer consumes it. Plans are written to the cycle log for
    audit (§8.3) and never written to the operating state file.

DESIGN (per §7 and §8):
    - Plans are sequences of typed entries; entries execute in listed
      order (§8.1).
    - Each entry is a tagged union expressed as a frozen Pydantic model
      with a `kind` discriminator. This makes the action layer's
      pattern-match dispatch trivially correct and the cycle log human-
      readable.
    - All monetary fields are Decimal (§2.8).
    - Per-position breakdowns store dollar amounts; the action layer
      submits dollar-denominated orders (§8.2.1 fractional-share rule).
    - `client_order_id` is deterministic per I16 and is minted by the
      action layer at submission time using the cycle_uuid and the
      entry's position within the plan.

ENTRY TAXONOMY (§3.12):
    - OrderEntry: a single BUY or SELL of $X of symbol S
    - WithdrawalEntry: monthly withdrawal with source breakdown
    - BufferRefillEntry: Growth → SGOV refill batch
    - CashRefillEntry: cash buffer top-up / drawdown
    - LargeCashDeploymentEntry: multi-position BUY for large inflow
    - PhaseTransitionEntry: coordinated reallocation across phases
    - CBStateTransitionEntry: pure record-keeping; appends to CB log
    - ACHScheduleUpdateEntry: change broker recurring-ACH amount
    - AlertEntry: dispatch an alert (always last in the plan, §8.2.9)

INVARIANTS (D1–D12, §7.8):
    - D2: net cash impact is zero ± rounding except for Withdrawal /
      Refill (this is enforced at decision-layer construction time, not
      here).
    - D11: LargeCashDeployment in target-weight-proportional mode is
      BUYs only.

This module defines the types and a thin Plan container; the actual
decision logic that constructs plan instances is in the per-subsystem
modules (withdrawal.py, rebalancer.py, etc.) and is composed in
decision_layer.py.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from broker_types import OrderSide, OrderType
from state_model import CBEntryCondition, CBState, Phase


# --- Entry kinds (discriminator) -------------------------------------------

class EntryKind(str, Enum):
    """Tag discriminating plan-entry types. Matches the §3.12 catalog."""
    ORDER = "order"
    WITHDRAWAL = "withdrawal"
    BUFFER_REFILL = "buffer_refill"
    CASH_REFILL = "cash_refill"
    LARGE_CASH_DEPLOYMENT = "large_cash_deployment"
    PHASE_TRANSITION = "phase_transition"
    CB_STATE_TRANSITION = "cb_state_transition"
    ACH_SCHEDULE_UPDATE = "ach_schedule_update"
    ALERT = "alert"


class LargeCashDeploymentMode(str, Enum):
    """Mode that determines which positions receive cash (§7.7.1)."""
    TARGET_WEIGHT_PROPORTIONAL = "target_weight_proportional"
    DEFENSIVE = "defensive"


# --- Shared building blocks ------------------------------------------------

class SourceLine(BaseModel):
    """One row of a multi-position source breakdown (withdrawal, refill,
    deployment). The action layer turns each line into a SELL or BUY
    order; the dollar_amount drives the order size.

    share_count_estimate is informational — the actual broker order is
    dollar-denominated (§8.2.1 fractional-share rule).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    dollar_amount: Decimal
    share_count_estimate: Decimal


# --- Concrete entry types --------------------------------------------------

class OrderEntry(BaseModel):
    """A single BUY or SELL submitted as a market order in dollar amount.

    Used internally by other entry types and standalone for rebalance
    trades. The action layer dispatches these via Broker.place_order().
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.ORDER
    symbol: str
    side: OrderSide
    dollar_amount: Decimal
    order_type: OrderType = OrderType.MKT
    limit_price: Optional[Decimal] = None  # only for LMT
    note: Optional[str] = None  # free-form context for the cycle log


class WithdrawalEntry(BaseModel):
    """Monthly withdrawal: SELLs from sources, then ACH out (§7.3.3, §8.2.2).

    The action layer executes the SELL orders first, waits for fills,
    then initiates the ACH transfer. The ACH amount equals
    `total_dollar_amount`; the SELL dollar amounts sum to the same value
    (modulo rounding).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.WITHDRAWAL
    total_dollar_amount: Decimal
    sources: list[SourceLine]
    ach_destination: str
    scheduled_ach_date: date
    cb_state_at_decision: CBState  # for cycle log forensics
    cascade_growth_used: bool = False  # set if cascade reached Growth (§7.3.2 step 3)


class BufferRefillEntry(BaseModel):
    """SGOV refill batch: SELL Growth → BUY SGOV (§7.4).

    `growth_sources` lists the Growth-side SELLs (sum to refill_amount);
    `sgov_buy_amount` is the matching BUY. Action layer executes SELLs
    first, then BUY (§8.2.3).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.BUFFER_REFILL
    growth_sources: list[SourceLine]
    sgov_buy_amount: Decimal


class CashRefillEntry(BaseModel):
    """Small cash buffer adjustment (§7.7).

    Direction is implicit in the orders: if `sell_source` is set, we are
    selling to add cash; if `buy_target` is set, we are deploying cash
    to that position. Exactly one of the two is populated.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.CASH_REFILL
    sell_source: Optional[SourceLine] = None
    buy_target: Optional[SourceLine] = None


class LargeCashDeploymentEntry(BaseModel):
    """Coordinated multi-position BUY of a large cash surplus (§7.7.1).

    `target_weights_snapshot` records the weights used so the cycle log
    explains how the BUY amounts were derived. `mode` is informational
    here — the action layer treats both modes identically (all BUYs);
    the difference is which positions are populated and is decided at
    plan-construction time.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.LARGE_CASH_DEPLOYMENT
    mode: LargeCashDeploymentMode
    total_dollar_amount: Decimal
    buys: list[SourceLine]
    target_weights_snapshot: dict[str, Decimal]


class PhaseTransitionEntry(BaseModel):
    """Coordinated reallocation across the phase boundary (§7.2, §8.2.5).

    The action layer:
      1. Executes all SELLs first (liquidations and drops-to-residual).
      2. Waits for fills.
      3. Executes all BUYs against post-SELL cash.
      4. Persists new phase state.

    `residual_skipped` records positions that were already at or below
    residual and therefore skipped (surfaced in the large_rebalance
    alert per §12.6).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.PHASE_TRANSITION
    from_phase: Phase
    to_phase: Phase
    sells: list[SourceLine]
    buys: list[SourceLine]
    residual_skipped: list[str] = Field(default_factory=list)
    # Phase 3 transitions carry the I_0 calc inputs (§4.1.1.3) for audit;
    # the action layer computes I_0 from post-SELL portfolio value and
    # persists schedule_state at execution time.
    is_phase3_activation: bool = False


class CBStateTransitionEntry(BaseModel):
    """Pure record-keeping; appends to the CB transition log (§8.2.6).

    The decision layer evaluates and decides the transition; this entry
    exists so the transition is recorded as part of the plan rather than
    as a side effect of decision logic.

    `trigger_reason` matches the §6.7 catalog:
      CB1 entry/exit: 'signal'
      CB2 entry: 'signal' | 'portfolio_low' | 'fi_low' | 'cb1_timer'
      CB2 exit: comma-separated list of conditions that cleared
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.CB_STATE_TRANSITION
    from_state: CBState
    to_state: CBState
    trigger_reason: str
    cb2_entry_conditions_after: list[CBEntryCondition] = Field(default_factory=list)


class ACHScheduleUpdateEntry(BaseModel):
    """Update broker-side recurring ACH amount (§7.9, §8.2.7).

    new_amount_dollars is the target. 0 means "pause income" (ACH-zero);
    any positive value means resume/update to that monthly amount.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.ACH_SCHEDULE_UPDATE
    new_amount_dollars: Decimal
    reason: str  # e.g., 'income_state_paused', 'annual_review_raise'


class AlertEntry(BaseModel):
    """Dispatch an alert (§8.2.9). Alerts dispatch at end-of-cycle,
    after other entries have completed or failed.

    `alert_id` matches an AlertId enum value (alert_catalog.py).
    `context` is a dict of placeholder→value used to render the
    template text loaded from alert_templates.yaml at dispatch time.
    `severity_override` allows the emitter to escalate (e.g., the
    token_unavailable cycle-count escalation per §10.4).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind = EntryKind.ALERT
    alert_id: str
    context: dict[str, str] = Field(default_factory=dict)
    severity_override: Optional[str] = None  # 'INFO' | 'NOTICE' | 'WARNING' | 'CRITICAL'
    dedup_key: Optional[str] = None  # overrides the alerter's default dedup-key derivation


# --- Discriminated entry union ---------------------------------------------

PlanEntry = Union[
    OrderEntry,
    WithdrawalEntry,
    BufferRefillEntry,
    CashRefillEntry,
    LargeCashDeploymentEntry,
    PhaseTransitionEntry,
    CBStateTransitionEntry,
    ACHScheduleUpdateEntry,
    AlertEntry,
]


# --- Plan container --------------------------------------------------------

class Plan(BaseModel):
    """The full plan for one cycle.

    `cycle_id` and `created_at` carry the cycle identity (per I16);
    `entries` are executed in listed order by the action layer.
    `decision_clock` is the captured per-cycle clock value from
    cycle_attempt.json — referenced here so the cycle log can prove
    determinism (D1, I8).

    A plan can be empty (the common case for quiet cycles).
    """
    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    decision_clock: datetime
    entries: list[PlanEntry] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.entries) == 0

    def add(self, entry: PlanEntry) -> None:
        # Convenience for builders. Note Pydantic models in the list are
        # frozen, but the list itself is mutable.
        self.entries.append(entry)

    def entries_of_kind(self, kind: EntryKind) -> list[PlanEntry]:
        return [e for e in self.entries if e.kind == kind]

    def has_phase_transition(self) -> bool:
        return any(e.kind == EntryKind.PHASE_TRANSITION for e in self.entries)


__all__ = [
    "EntryKind",
    "LargeCashDeploymentMode",
    "SourceLine",
    "OrderEntry",
    "WithdrawalEntry",
    "BufferRefillEntry",
    "CashRefillEntry",
    "LargeCashDeploymentEntry",
    "PhaseTransitionEntry",
    "CBStateTransitionEntry",
    "ACHScheduleUpdateEntry",
    "AlertEntry",
    "PlanEntry",
    "Plan",
]
