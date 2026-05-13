"""
cash_buffer.py — Cash buffer management and Large Cash Deployment (§7.7, §7.7.1).

PURPOSE:
    Two distinct decisions, both about the cash balance:

    1. CASH BUFFER REFILL / DRAWDOWN (§7.7):
       - The cash balance should track `cash_target_dollars` ±
         `cash_buffer_tolerance_dollars`.
       - On a deficit > tolerance: SELL from most-overweight core
         position to refill cash.
       - On a surplus ≤ tolerance + large-cash-deployment threshold:
         BUY into core to draw down to target.
       - On a surplus > large-cash-deployment threshold: the
         LargeCashDeployment path takes over (§7.7.1).

    2. LARGE CASH DEPLOYMENT (§7.7.1):
       - Trigger: cash_surplus > max(threshold_dollars, threshold_rate × portfolio)
       - Mode selection:
         * DEFENSIVE: in CB2 AND signal ≤ cb2_threshold → deploy into FI
           proportional to active-phase FI weights only.
         * TARGET_WEIGHT_PROPORTIONAL: otherwise → BUYs only,
           proportional to active-phase target weights (per D11).

DESIGN:
    Pure decision module: returns plan entries to be added by the
    decision layer. The caller decides whether to call this module
    based on phase and I15 gating.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from broker_types import OrderSide, OrderType, Position
from plan_model import (
    AlertEntry,
    CashRefillEntry,
    LargeCashDeploymentEntry,
    LargeCashDeploymentMode,
    SourceLine,
)
from state_model import CBEntryCondition, CBState, Phase


# =============================================================================
# Inputs / outputs
# =============================================================================

@dataclass(frozen=True)
class CashBufferInputs:
    """Inputs to the cash-buffer / large-cash-deployment decision."""
    phase: Phase
    cb_state: CBState
    cb2_entry_conditions: set[CBEntryCondition]

    # Cash balance and target
    cash_balance_dollars: Decimal
    cash_target_dollars: Decimal
    cash_buffer_tolerance_dollars: Decimal

    # Portfolio total (used for large-cash-deployment threshold rate)
    portfolio_total_dollars: Decimal  # Growth + FI + buffer (excludes cash)

    # Per-position values
    positions: dict[str, Position]
    target_weights: dict[str, Decimal]   # active-phase target weights
    growth_symbols: tuple[str, ...]
    fi_symbols: tuple[str, ...]
    buffer_symbol: str  # 'SGOV' — never BUY/SELL via cash-buffer logic

    # Lookback signal (for defensive-mode trigger)
    signal_available: bool
    signal_value: Optional[Decimal]

    # Tunables
    position_residual_minimum_dollars: Decimal
    large_cash_deployment_threshold_dollars: Decimal
    large_cash_deployment_threshold_rate: Decimal
    cb2_threshold_rate: Decimal


@dataclass(frozen=True)
class CashBufferDecision:
    """Outcome of the cash-buffer / large-cash-deployment decision.

    Exactly one of the three optional fields is populated, or all
    are None for a quiet cycle.
    """
    cash_refill_entry: Optional[CashRefillEntry] = None
    large_cash_deployment_entry: Optional[LargeCashDeploymentEntry] = None
    deployment_alert: Optional[AlertEntry] = None


# =============================================================================
# Helpers
# =============================================================================

def _position_value(positions: dict[str, Position], symbol: str) -> Decimal:
    p = positions.get(symbol)
    return p.market_value if p is not None else Decimal("0")


def _sellable(positions: dict[str, Position], symbol: str,
              residual: Decimal) -> Decimal:
    p = positions.get(symbol)
    if p is None or p.market_value <= 0:
        return Decimal("0")
    avail = p.market_value - residual
    return avail if avail > 0 else Decimal("0")


def _share_estimate(positions: dict[str, Position], symbol: str,
                    dollars: Decimal) -> Decimal:
    p = positions.get(symbol)
    if p is None or p.quantity <= 0 or p.market_value <= 0:
        return Decimal("0")
    per_share = p.market_value / p.quantity
    if per_share <= 0:
        return Decimal("0")
    return dollars / per_share


def _most_overweight_core_symbol(
    positions: dict[str, Position],
    target_weights: dict[str, Decimal],
) -> str | None:
    """Identify the most-overweight core symbol (largest positive drift).
    Tie-break: larger current $. Returns None if nothing is overweight.
    Same algorithm used by withdrawal.py — kept local here for clarity.
    """
    core_total = sum(
        (_position_value(positions, s) for s in target_weights),
        start=Decimal("0"),
    )
    if core_total <= 0:
        return None
    drift: dict[str, Decimal] = {}
    for s, w in target_weights.items():
        target_v = core_total * w
        drift[s] = _position_value(positions, s) - target_v
    candidates = [s for s, d in drift.items() if d > 0]
    if not candidates:
        return None
    candidates.sort(
        key=lambda s: (drift[s], _position_value(positions, s)),
        reverse=True,
    )
    return candidates[0]


def _large_cash_deployment_threshold(inputs: CashBufferInputs) -> Decimal:
    """§7.7.1 trigger threshold."""
    rate_floor = inputs.large_cash_deployment_threshold_rate * inputs.portfolio_total_dollars
    return max(inputs.large_cash_deployment_threshold_dollars, rate_floor)


def _is_defensive_mode(inputs: CashBufferInputs) -> bool:
    """Defensive-mode condition per §7.7.1: in CB2 AND signal ≤ cb2_threshold.

    The signal must be AVAILABLE for defensive mode to engage; otherwise
    fall back to target-weight-proportional (D-SPEC-7).
    """
    if inputs.cb_state != CBState.CB2:
        return False
    if not inputs.signal_available or inputs.signal_value is None:
        return False
    return inputs.signal_value <= inputs.cb2_threshold_rate


# =============================================================================
# Cash refill / drawdown (§7.7)
# =============================================================================

def _decide_cash_refill_or_drawdown(
    inputs: CashBufferInputs,
    *,
    delta: Decimal,
) -> Optional[CashRefillEntry]:
    """Small adjustment to keep cash near target.

    `delta` = cash_balance - cash_target.
    - delta < -tolerance: need to refill cash → SELL from most-overweight core.
    - delta > +tolerance and ≤ large_cash_deployment_threshold:
        draw down → BUY into most-underweight core.
    - |delta| ≤ tolerance: do nothing.

    Large surpluses are NOT handled here; they fall through to the
    LargeCashDeployment path in the top-level decide().

    SGOV is excluded from cash-buffer sourcing — that's the buffer
    refill subsystem's domain (§7.4), not this one.
    """
    pos = inputs.positions
    residual = inputs.position_residual_minimum_dollars
    tol = inputs.cash_buffer_tolerance_dollars

    # --- Deficit (cash too low) → SELL from most-overweight core ---
    if delta < -tol:
        need = -delta  # dollars to add to cash
        pick = _most_overweight_core_symbol(pos, inputs.target_weights)
        if pick is None:
            # No overweight position to source from. Fall back to the
            # largest core position by value, clamped at sellable.
            largest = max(
                inputs.target_weights.keys(),
                key=lambda s: _position_value(pos, s),
                default=None,
            )
            pick = largest
        if pick is None:
            return None
        avail = _sellable(pos, pick, residual)
        take = min(need, avail)
        if take <= 0:
            return None
        return CashRefillEntry(
            sell_source=SourceLine(
                symbol=pick,
                dollar_amount=take,
                share_count_estimate=_share_estimate(pos, pick, take),
            ),
        )

    # --- Small surplus (within large-cash threshold) → BUY into most-underweight core ---
    if delta > tol:
        # Check if surplus is small enough to handle here (large cash
        # deployment owns anything above the large-cash threshold).
        large_threshold = _large_cash_deployment_threshold(inputs)
        if delta > large_threshold:
            return None  # let LargeCashDeployment handle it
        deploy_dollars = delta  # drive cash back to target
        core_total = sum(
            (_position_value(pos, s) for s in inputs.target_weights),
            start=Decimal("0"),
        )
        if core_total <= 0:
            # Pick first core symbol if no positions yet.
            pick = next(iter(inputs.target_weights), None)
        else:
            # Most-underweight position (largest negative drift).
            drift = {}
            for s, w in inputs.target_weights.items():
                drift[s] = _position_value(pos, s) - (core_total * w)
            negatives = [(s, d) for s, d in drift.items() if d < 0]
            if negatives:
                negatives.sort(key=lambda kv: kv[1])  # most-negative first
                pick = negatives[0][0]
            else:
                # No underweight — just buy the smallest position.
                pick = min(
                    inputs.target_weights.keys(),
                    key=lambda s: _position_value(pos, s),
                )
        if pick is None:
            return None
        return CashRefillEntry(
            buy_target=SourceLine(
                symbol=pick,
                dollar_amount=deploy_dollars,
                share_count_estimate=_share_estimate(pos, pick, deploy_dollars),
            ),
        )

    # Within tolerance: no action.
    return None


# =============================================================================
# Large Cash Deployment (§7.7.1)
# =============================================================================

def _build_proportional_deployment(
    *,
    cash_to_deploy: Decimal,
    weights: dict[str, Decimal],
    positions: dict[str, Position],
) -> tuple[list[SourceLine], dict[str, Decimal]]:
    """BUYs only, proportional to provided weights. Returns (sources,
    snapshot). Symbols with weight 0 are omitted from sources but
    included in snapshot for audit clarity.
    """
    total_weight = sum(weights.values(), Decimal("0"))
    sources: list[SourceLine] = []
    if total_weight <= 0:
        return sources, dict(weights)
    for s, w in weights.items():
        if w <= 0:
            continue
        amount = cash_to_deploy * (w / total_weight)
        if amount <= 0:
            continue
        sources.append(SourceLine(
            symbol=s,
            dollar_amount=amount,
            share_count_estimate=_share_estimate(positions, s, amount),
        ))
    return sources, dict(weights)


def _build_defensive_deployment(
    *,
    cash_to_deploy: Decimal,
    target_weights: dict[str, Decimal],
    fi_symbols: tuple[str, ...],
    positions: dict[str, Position],
) -> tuple[list[SourceLine], dict[str, Decimal]]:
    """BUYs only, proportional to active-phase FI weights only (§7.7.1
    defensive mode). Growth is excluded entirely.

    If no FI weights are defined for the active phase (a Phase 2
    deployed-state config might have zero FI), we fall back to
    proportional across `target_weights`.
    """
    fi_weights = {s: w for s, w in target_weights.items() if s in fi_symbols and w > 0}
    if not fi_weights:
        # No FI portion in active phase → fall back to all weights.
        return _build_proportional_deployment(
            cash_to_deploy=cash_to_deploy,
            weights=target_weights,
            positions=positions,
        )
    return _build_proportional_deployment(
        cash_to_deploy=cash_to_deploy,
        weights=fi_weights,
        positions=positions,
    )


def _decide_large_cash_deployment(
    inputs: CashBufferInputs,
    *,
    cash_to_deploy: Decimal,
) -> CashBufferDecision:
    """Build a LargeCashDeploymentEntry plus its accompanying alert."""
    defensive = _is_defensive_mode(inputs)
    mode = (
        LargeCashDeploymentMode.DEFENSIVE
        if defensive
        else LargeCashDeploymentMode.TARGET_WEIGHT_PROPORTIONAL
    )

    if defensive:
        sources, snapshot = _build_defensive_deployment(
            cash_to_deploy=cash_to_deploy,
            target_weights=inputs.target_weights,
            fi_symbols=inputs.fi_symbols,
            positions=inputs.positions,
        )
    else:
        sources, snapshot = _build_proportional_deployment(
            cash_to_deploy=cash_to_deploy,
            weights=inputs.target_weights,
            positions=inputs.positions,
        )

    if not sources:
        return CashBufferDecision()

    actual_total = sum((s.dollar_amount for s in sources), Decimal("0"))

    entry = LargeCashDeploymentEntry(
        mode=mode,
        total_dollar_amount=actual_total,
        buys=sources,
        target_weights_snapshot=snapshot,
    )

    alert = AlertEntry(
        alert_id="large_cash_deployment",
        context={
            "mode": mode.value,
            "amount": str(actual_total),
            "trigger_amount_threshold": str(_large_cash_deployment_threshold(inputs)),
        },
    )

    return CashBufferDecision(
        large_cash_deployment_entry=entry,
        deployment_alert=alert,
    )


# =============================================================================
# Top-level decision
# =============================================================================

def decide_cash_buffer(inputs: CashBufferInputs) -> CashBufferDecision:
    """Decide cash-buffer adjustment for this cycle.

    Routing:
      1. delta = cash - target.
      2. If delta > large-cash-deployment threshold: deploy large cash.
      3. Else if |delta| > tolerance: small refill / drawdown.
      4. Else: nothing.
    """
    delta = inputs.cash_balance_dollars - inputs.cash_target_dollars
    large_threshold = _large_cash_deployment_threshold(inputs)

    # Step 1: Large cash deployment (only fires for surpluses)
    if delta > large_threshold:
        return _decide_large_cash_deployment(inputs, cash_to_deploy=delta)

    # Step 2: Small refill / drawdown
    cash_entry = _decide_cash_refill_or_drawdown(inputs, delta=delta)
    if cash_entry is not None:
        return CashBufferDecision(cash_refill_entry=cash_entry)

    # Step 3: Within tolerance
    return CashBufferDecision()


__all__ = [
    "CashBufferInputs",
    "CashBufferDecision",
    "decide_cash_buffer",
]
