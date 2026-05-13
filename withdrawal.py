"""
withdrawal.py — Monthly withdrawal decisions (§7.3).

PURPOSE:
    Compute the cycle's withdrawal Plan entry — total dollar amount and
    per-symbol source breakdown — given the operating state and broker-
    reported positions. Pure function; produces (WithdrawalEntry,
    optional AlertEntry) or None when no withdrawal is scheduled.

DESIGN (per §7.3):
    - Three sourcing rules depending on CB state:
      * CB_INACTIVE: most-overweight core position first, then
        proportional FI.
      * CB1: FI bucket only — Growth never sold (I14).
      * CB2: cascade SGOV → FI → Growth (§7.3.2). Identical regardless
        of which CB2 entry path activated.
    - Every SELL clamps at `position_residual_minimum_dollars` (I12).
    - Phase 3 applies per-month payment ceilings (§4.1.1.2): the actual
      monthly withdrawal is MIN(scheduled, portfolio%-ceiling, dollar-ceiling).
      Either ceiling binding emits a `monthly_payment_ceiling_bound` alert.
    - Cascade reaching Growth emits a `cascade_growth_source` Critical alert.
    - Three "should-not-happen" branches abort the cycle with
      `internal_consistency_violation`: CB_INACTIVE / CB1 sourcing
      insufficient (the FI-low / Portfolio-low CB2 paths should have
      activated cascade instead).

NON-DECISION:
    Whether today is the scheduled withdrawal day, and whether income
    state is ACTIVE — those gates are evaluated in decision_layer.py
    before this function is called. This module assumes the cycle has
    decided to withdraw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from broker_types import Position
from plan_model import AlertEntry, SourceLine, WithdrawalEntry
from state_model import CBState, Phase


# --- Failure case for the decision layer to handle -------------------------

class WithdrawalDecisionError(Exception):
    """Raised when withdrawal sourcing reaches an internally-inconsistent
    branch (§7.3.2 steps 4/7). The decision layer translates this into
    an `internal_consistency_violation` operational pause."""


class WithdrawalCapacityExhausted(Exception):
    """Cascade exhausted (§7.3.2 step 4, §11.2.7). The decision layer
    sets `withdrawal_capacity_exhausted: true` and emits the matching
    Critical alert; no withdrawal entry is produced."""


# --- Inputs ----------------------------------------------------------------

@dataclass(frozen=True)
class WithdrawalInputs:
    """Inputs the withdrawal computation needs. All come from the
    decision_layer's input refresh."""
    phase: Phase
    cb_state: CBState
    scheduled_monthly: Decimal       # from schedule_state math
    current_portfolio_dollars: Decimal  # Growth + FI (excl. buffer, cash)
    current_year: int
    today: date

    # Per-position market values by symbol. Includes core (Growth + FI),
    # buffer (SGOV), but cash/USD is separate (passed via positions
    # dict only when held as a position; cash buffer is read from
    # AccountSummary). Symbols not in the dict mean the position is
    # absent (zero quantity).
    positions: dict[str, Position]

    # Target weights for the active phase (so we can compute drift).
    target_weights: dict[str, Decimal]  # symbol → Decimal weight (sums to 1)

    # Active-phase symbol classification
    growth_symbols: tuple[str, ...]     # e.g., ('FBCG', 'AVUV')
    fi_symbols: tuple[str, ...]          # e.g., ('PYLD', 'JPIE') or ('GBIL',)
    buffer_symbol: str                   # 'SGOV'

    # Tunables
    position_residual_minimum_dollars: Decimal
    phase3_monthly_payment_ceiling_rate: Decimal
    phase3_dollar_ceiling_base_dollars: Decimal
    phase3_dollar_ceiling_base_year: int
    inflation_rate: Decimal

    # ACH config
    ach_destination: str
    scheduled_ach_date: date


# --- Helpers ---------------------------------------------------------------

def _position_value(positions: dict[str, Position], symbol: str) -> Decimal:
    p = positions.get(symbol)
    return p.market_value if p is not None else Decimal("0")


def _sellable(positions: dict[str, Position],
              symbol: str,
              residual: Decimal) -> Decimal:
    """Dollar amount that can be sold from `symbol` without breaching
    the residual floor. Zero if the position is at or below residual."""
    mv = _position_value(positions, symbol)
    avail = mv - residual
    return avail if avail > 0 else Decimal("0")


def _source_line(symbol: str, dollars: Decimal,
                 positions: dict[str, Position]) -> SourceLine:
    """Build a SourceLine for the plan. share_count_estimate is
    informational (the broker submits dollar-denominated orders, §8.2.1);
    we estimate from the position's market value and quantity for the
    cycle log."""
    p = positions.get(symbol)
    if p is None or p.quantity <= 0 or p.market_value <= 0:
        return SourceLine(symbol=symbol,
                          dollar_amount=dollars,
                          share_count_estimate=Decimal("0"))
    per_share = p.market_value / p.quantity
    if per_share <= 0:
        return SourceLine(symbol=symbol,
                          dollar_amount=dollars,
                          share_count_estimate=Decimal("0"))
    return SourceLine(
        symbol=symbol,
        dollar_amount=dollars,
        share_count_estimate=(dollars / per_share),
    )


def _drift_dollars(positions: dict[str, Position],
                   target_weights: dict[str, Decimal],
                   core_total: Decimal) -> dict[str, Decimal]:
    """drift[s] = current_value[s] - target_value[s] for each core symbol.

    Positive drift = overweight. Negative drift = underweight.
    Symbols not in target_weights are not considered core; they receive
    drift of 0 (they'll be ignored by callers that filter for positive
    drift).
    """
    out: dict[str, Decimal] = {}
    for s, w in target_weights.items():
        target_v = core_total * w
        cur_v = _position_value(positions, s)
        out[s] = cur_v - target_v
    return out


def _most_overweight(drift: dict[str, Decimal],
                     restrict_to: tuple[str, ...] | None,
                     positions: dict[str, Position]) -> str | None:
    """Identify the most-overweight position (largest positive drift).
    Tie-break: larger position by current $ value (§7.3.2 step 4).

    `restrict_to` limits the candidate symbols (e.g., FI bucket only
    for CB1 sourcing). None means consider all symbols in `drift`.

    Returns None if no symbol has positive drift.
    """
    candidates = [
        s for s, d in drift.items()
        if d > 0 and (restrict_to is None or s in restrict_to)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda s: (drift[s], _position_value(positions, s)),
        reverse=True,
    )
    return candidates[0]


# --- Phase 3 payment ceiling ----------------------------------------------

@dataclass(frozen=True)
class CeilingResult:
    actual_monthly: Decimal
    bound_by_portfolio_ceiling: bool
    bound_by_dollar_ceiling: bool


def apply_phase3_ceilings(
    *,
    scheduled: Decimal,
    current_portfolio: Decimal,
    current_year: int,
    monthly_payment_ceiling_rate: Decimal,
    dollar_ceiling_base: Decimal,
    dollar_ceiling_base_year: int,
    inflation_rate: Decimal,
) -> CeilingResult:
    """Apply the two Phase 3 ceilings per §4.1.1.2.

    portfolio_ceiling = current_portfolio × rate / 12
    dollar_ceiling    = base × (1 + inflation)^(year - base_year)
    actual            = min(scheduled, portfolio_ceiling, dollar_ceiling)

    Reports which ceiling(s) bound (may be both if they coincide
    within rounding).
    """
    portfolio_ceiling = current_portfolio * monthly_payment_ceiling_rate / Decimal("12")
    years_since_base = current_year - dollar_ceiling_base_year
    if years_since_base < 0:
        years_since_base = 0
    dollar_ceiling = dollar_ceiling_base * (Decimal("1") + inflation_rate) ** years_since_base

    actual = min(scheduled, portfolio_ceiling, dollar_ceiling)
    return CeilingResult(
        actual_monthly=actual,
        bound_by_portfolio_ceiling=(actual == portfolio_ceiling and actual < scheduled),
        bound_by_dollar_ceiling=(actual == dollar_ceiling and actual < scheduled),
    )


# --- Sourcing strategies ---------------------------------------------------

def _source_cb_inactive(
    demand: Decimal,
    inputs: WithdrawalInputs,
) -> list[SourceLine]:
    """CB_INACTIVE sourcing (§7.3.2): most-overweight core first, then
    proportional FI for the remainder.

    Raises WithdrawalDecisionError if sourcing cannot meet demand even
    at residual floors (this branch is reachable only via a CB
    evaluation bug, per §7.3.2 step 7).
    """
    residual = inputs.position_residual_minimum_dollars
    pos = inputs.positions
    core_total = sum(
        (_position_value(pos, s) for s in inputs.target_weights),
        start=Decimal("0"),
    )
    drift = _drift_dollars(pos, inputs.target_weights, core_total)

    sources: list[SourceLine] = []
    remaining = demand

    # 1) Most-overweight core position
    pick = _most_overweight(drift, restrict_to=None, positions=pos)
    if pick is not None:
        surplus = drift[pick]
        sellable = _sellable(pos, pick, residual)
        # We can sell up to min(surplus, sellable, remaining).
        avail = min(surplus, sellable)
        if avail >= remaining:
            sources.append(_source_line(pick, remaining, pos))
            return sources
        if avail > 0:
            sources.append(_source_line(pick, avail, pos))
            remaining -= avail

    # 2) Proportional FI for remainder
    if remaining > 0:
        fi_pool = {
            s: _sellable(pos, s, residual)
            for s in inputs.fi_symbols
        }
        # Skip already-picked symbol so we don't double-charge it
        if pick in fi_pool:
            # The overweight pick was an FI symbol; we already drew
            # its full sellable surplus. Remove from proportional pool.
            fi_pool[pick] = Decimal("0")
        total_avail = sum(fi_pool.values(), Decimal("0"))
        if total_avail < remaining:
            raise WithdrawalDecisionError(
                f"CB_INACTIVE sourcing insufficient: demand {remaining} > "
                f"available FI {total_avail}; CB resource paths should have "
                f"activated cascade"
            )
        # Distribute proportionally to current value
        # We use sellable amounts as weights (rather than market values)
        # so positions already at residual don't get counted.
        for s, avail in fi_pool.items():
            if avail <= 0:
                continue
            share = (avail / total_avail) * remaining
            # Clamp at the position's sellable amount
            take = min(share, avail)
            if take > 0:
                sources.append(_source_line(s, take, pos))
    return sources


def _source_cb1(demand: Decimal, inputs: WithdrawalInputs) -> list[SourceLine]:
    """CB1 sourcing (§7.3.2): FI bucket only — most-overweight FI first,
    then proportional FI for remainder. Growth never sold (I14).

    Raises WithdrawalDecisionError if FI cannot meet demand even at
    residuals (per §7.3.2 step 4 the FI-low path should have triggered).
    """
    residual = inputs.position_residual_minimum_dollars
    pos = inputs.positions
    core_total = sum(
        (_position_value(pos, s) for s in inputs.target_weights),
        start=Decimal("0"),
    )
    drift = _drift_dollars(pos, inputs.target_weights, core_total)

    sources: list[SourceLine] = []
    remaining = demand

    pick = _most_overweight(drift, restrict_to=inputs.fi_symbols, positions=pos)
    if pick is not None:
        surplus = drift[pick]
        sellable = _sellable(pos, pick, residual)
        avail = min(surplus, sellable)
        if avail >= remaining:
            sources.append(_source_line(pick, remaining, pos))
            return sources
        if avail > 0:
            sources.append(_source_line(pick, avail, pos))
            remaining -= avail

    if remaining > 0:
        fi_pool = {
            s: _sellable(pos, s, residual)
            for s in inputs.fi_symbols if s != pick
        }
        total_avail = sum(fi_pool.values(), Decimal("0"))
        if total_avail < remaining:
            raise WithdrawalDecisionError(
                f"CB1 sourcing insufficient: demand {remaining} > "
                f"FI available {total_avail}; FI-low CB2 path should have "
                f"activated cascade"
            )
        for s, avail in fi_pool.items():
            if avail <= 0:
                continue
            share = (avail / total_avail) * remaining
            take = min(share, avail)
            if take > 0:
                sources.append(_source_line(s, take, pos))
    return sources


@dataclass(frozen=True)
class CascadeResult:
    sources: list[SourceLine]
    reached_growth: bool


def _source_cascade(demand: Decimal, inputs: WithdrawalInputs) -> CascadeResult:
    """Cascade sourcing for CB2 (§7.3.2): SGOV → FI → Growth, each
    stage drained to residual before next.

    Raises WithdrawalCapacityExhausted if all three stages reach
    residual and demand remains unmet (§7.3.2 step 4, §11.2.7).
    """
    residual = inputs.position_residual_minimum_dollars
    pos = inputs.positions
    sources: list[SourceLine] = []
    remaining = demand
    reached_growth = False

    # Stage 1: SGOV
    sgov_avail = _sellable(pos, inputs.buffer_symbol, residual)
    if sgov_avail >= remaining:
        sources.append(_source_line(inputs.buffer_symbol, remaining, pos))
        return CascadeResult(sources=sources, reached_growth=False)
    if sgov_avail > 0:
        sources.append(_source_line(inputs.buffer_symbol, sgov_avail, pos))
        remaining -= sgov_avail

    # Stage 2: FI proportional
    fi_pool = {s: _sellable(pos, s, residual) for s in inputs.fi_symbols}
    fi_total = sum(fi_pool.values(), Decimal("0"))
    if fi_total >= remaining:
        for s, avail in fi_pool.items():
            if avail <= 0:
                continue
            share = (avail / fi_total) * remaining
            take = min(share, avail)
            if take > 0:
                sources.append(_source_line(s, take, pos))
        return CascadeResult(sources=sources, reached_growth=False)
    # Drain FI completely
    if fi_total > 0:
        for s, avail in fi_pool.items():
            if avail > 0:
                sources.append(_source_line(s, avail, pos))
        remaining -= fi_total

    # Stage 3: Growth proportional
    g_pool = {s: _sellable(pos, s, residual) for s in inputs.growth_symbols}
    g_total = sum(g_pool.values(), Decimal("0"))
    if g_total >= remaining:
        reached_growth = True
        for s, avail in g_pool.items():
            if avail <= 0:
                continue
            share = (avail / g_total) * remaining
            take = min(share, avail)
            if take > 0:
                sources.append(_source_line(s, take, pos))
        return CascadeResult(sources=sources, reached_growth=True)

    # Capacity exhausted
    raise WithdrawalCapacityExhausted(
        f"cascade exhausted: demand {remaining} > Growth available {g_total}"
    )


# --- Top-level entry point -------------------------------------------------

@dataclass(frozen=True)
class WithdrawalDecision:
    """Result of a withdrawal decision."""
    entry: Optional[WithdrawalEntry]
    ceiling_alert: Optional[AlertEntry]
    cascade_growth_alert: Optional[AlertEntry]


def decide_withdrawal(
    *,
    inputs: WithdrawalInputs,
    cycle_id: str,
) -> WithdrawalDecision:
    """Compute the WithdrawalEntry for this cycle.

    Returns WithdrawalDecision wrapping the entry plus any
    monthly_payment_ceiling_bound / cascade_growth_source alerts.

    Raises:
      WithdrawalDecisionError: CB_INACTIVE / CB1 sourcing reached an
        impossible branch → caller sets internal_consistency_violation.
      WithdrawalCapacityExhausted: cascade exhausted → caller sets
        withdrawal_capacity_exhausted=true (§11.3.2).
    """
    # 1) Apply Phase 3 ceilings if applicable
    actual_monthly = inputs.scheduled_monthly
    ceiling_alert: Optional[AlertEntry] = None

    if inputs.phase == Phase.PHASE_3:
        cres = apply_phase3_ceilings(
            scheduled=inputs.scheduled_monthly,
            current_portfolio=inputs.current_portfolio_dollars,
            current_year=inputs.current_year,
            monthly_payment_ceiling_rate=inputs.phase3_monthly_payment_ceiling_rate,
            dollar_ceiling_base=inputs.phase3_dollar_ceiling_base_dollars,
            dollar_ceiling_base_year=inputs.phase3_dollar_ceiling_base_year,
            inflation_rate=inputs.inflation_rate,
        )
        actual_monthly = cres.actual_monthly
        if cres.bound_by_portfolio_ceiling or cres.bound_by_dollar_ceiling:
            which = []
            if cres.bound_by_portfolio_ceiling:
                which.append("portfolio_percentage")
            if cres.bound_by_dollar_ceiling:
                which.append("dollar")
            ceiling_alert = AlertEntry(
                alert_id="monthly_payment_ceiling_bound",
                context={
                    "scheduled": str(inputs.scheduled_monthly),
                    "actual": str(actual_monthly),
                    "bound_by": ",".join(which),
                    "cycle_id": cycle_id,
                },
            )

    # 2) Source per CB state
    cascade_growth_alert: Optional[AlertEntry] = None
    if inputs.cb_state == CBState.CB_INACTIVE:
        sources = _source_cb_inactive(actual_monthly, inputs)
    elif inputs.cb_state == CBState.CB1:
        sources = _source_cb1(actual_monthly, inputs)
    else:  # CB2
        cresult = _source_cascade(actual_monthly, inputs)
        sources = cresult.sources
        if cresult.reached_growth:
            cascade_growth_alert = AlertEntry(
                alert_id="cascade_growth_source",
                context={
                    "amount": str(actual_monthly),
                    "cycle_id": cycle_id,
                },
            )

    # 3) Build entry
    entry = WithdrawalEntry(
        total_dollar_amount=actual_monthly,
        sources=sources,
        ach_destination=inputs.ach_destination,
        scheduled_ach_date=inputs.scheduled_ach_date,
        cb_state_at_decision=inputs.cb_state,
        cascade_growth_used=(cascade_growth_alert is not None),
    )

    return WithdrawalDecision(
        entry=entry,
        ceiling_alert=ceiling_alert,
        cascade_growth_alert=cascade_growth_alert,
    )


__all__ = [
    "WithdrawalInputs",
    "WithdrawalDecision",
    "WithdrawalDecisionError",
    "WithdrawalCapacityExhausted",
    "CeilingResult",
    "CascadeResult",
    "apply_phase3_ceilings",
    "decide_withdrawal",
]
