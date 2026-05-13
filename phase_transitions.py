"""
phase_transitions.py — Phase transition reallocation plans (§7.2).

PURPOSE:
    Build the SELLs and BUYs that effect a phase transition:
      Phase 1 → Phase 2  (§7.2.1)
      Phase 1 → Phase 3  (§7.2.2)
      Phase 2 → Phase 3  (§7.2.3)

DESIGN:
    - Pure functions; return a PhaseTransitionEntry and matching alerts.
    - The action layer (§8.2.5) executes SELLs first, waits for fills,
      then executes BUYs against post-SELL cash.
    - Phase 3 activations compute I_0 from POST-SELL portfolio value
      using the §4.1.1 annuity formula. Because this module is pure
      and only knows pre-cycle positions, the I_0 calc happens at
      ACTION time, not at plan-construction time. We carry the
      `phase3_activation` flag and the calc-input ruleset values so
      action_layer.py can compute I_0 when it knows the post-SELL
      portfolio value.
    - I13 (liquidate-to-zero) applies to GBIL in Phase 2 → Phase 3:
      sell entire position with no residual floor.
    - I12 (residual floor) applies to PYLD/JPIE in Phase 1 → Phase 2:
      drawn down to residual, not to zero.
    - Per §7.2.4, the transition cycle suppresses all other decision
      steps. The decision_layer enforces this; this module is called
      only when the transition is THE work of the cycle.

OUTPUTS:
    A small helper class `PhaseTransitionDecision` wrapping the
    PhaseTransitionEntry and the standard large_rebalance alert
    (plus the phase3_activation alert when applicable).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from broker_types import Position
from plan_model import AlertEntry, PhaseTransitionEntry, SourceLine
from state_model import Phase


# =============================================================================
# Helpers
# =============================================================================

def _position_value(positions: dict[str, Position], symbol: str) -> Decimal:
    p = positions.get(symbol)
    return p.market_value if p is not None else Decimal("0")


def _share_estimate(positions: dict[str, Position], symbol: str,
                    dollars: Decimal) -> Decimal:
    p = positions.get(symbol)
    if p is None or p.quantity <= 0 or p.market_value <= 0:
        return Decimal("0")
    per_share = p.market_value / p.quantity
    if per_share <= 0:
        return Decimal("0")
    return dollars / per_share


def _make_sell_line(positions: dict[str, Position],
                    symbol: str,
                    dollars: Decimal) -> SourceLine:
    return SourceLine(
        symbol=symbol,
        dollar_amount=dollars,
        share_count_estimate=_share_estimate(positions, symbol, dollars),
    )


def _make_buy_line(positions: dict[str, Position],
                   symbol: str,
                   dollars: Decimal) -> SourceLine:
    return SourceLine(
        symbol=symbol,
        dollar_amount=dollars,
        share_count_estimate=_share_estimate(positions, symbol, dollars),
    )


# =============================================================================
# Result type
# =============================================================================

@dataclass(frozen=True)
class PhaseTransitionDecision:
    """The full plan slice produced by a phase transition decision."""
    entry: PhaseTransitionEntry
    large_rebalance_alert: AlertEntry
    phase3_activation_alert: Optional[AlertEntry] = None


# =============================================================================
# Phase 1 → Phase 2 (§7.2.1)
# =============================================================================

def build_phase1_to_phase2(
    *,
    positions: dict[str, Position],
    phase2_steady_weights: dict[str, Decimal],
    position_residual_minimum_dollars: Decimal,
) -> PhaseTransitionDecision:
    """Build the Phase 1 → Phase 2 plan.

    Steps (§7.2.1):
      1. SELL PYLD → residual (skip if at/below residual).
      2. SELL JPIE → residual.
      3. BUY GBIL to phase2_steady_weights['GBIL'] × post-liquidation core.
      4. Rebalance FBCG and AVUV to phase2_steady_weights × post-liquidation core.

    The post-liquidation core is the sum of (FBCG + AVUV current values)
    plus the proceeds of PYLD/JPIE liquidations. We compute it here
    rather than in the action layer because the SELL totals are
    deterministic given the residual floor.
    """
    residual = position_residual_minimum_dollars
    sells: list[SourceLine] = []
    buys: list[SourceLine] = []
    residual_skipped: list[str] = []

    # Step 1/2: liquidate PYLD and JPIE down to residual
    pyld_value = _position_value(positions, "PYLD")
    jpie_value = _position_value(positions, "JPIE")
    liquidation_proceeds = Decimal("0")
    for sym, mv in (("PYLD", pyld_value), ("JPIE", jpie_value)):
        if mv <= residual:
            residual_skipped.append(sym)
            continue
        sell_amount = mv - residual
        sells.append(_make_sell_line(positions, sym, sell_amount))
        liquidation_proceeds += sell_amount

    # Post-liquidation core: FBCG + AVUV current + liquidation proceeds
    # (PYLD/JPIE residuals are excluded from core total per §7.2.1).
    fbcg_value = _position_value(positions, "FBCG")
    avuv_value = _position_value(positions, "AVUV")
    gbil_value = _position_value(positions, "GBIL")  # typically 0 in Phase 1
    post_liq_core = fbcg_value + avuv_value + gbil_value + liquidation_proceeds

    # Steps 3 & 4: rebalance FBCG, AVUV, GBIL to phase2_steady_weights
    # Compute target value per symbol; the delta (target - current) is
    # the buy. If a symbol's current > target the surplus would need to
    # be SOLD, but with Phase 1→2 the only overweight Growth could be is
    # if FBCG or AVUV currently > 45% of post-liquidation core, which
    # is improbable. Handle the case anyway.
    for sym in ("FBCG", "AVUV", "GBIL"):
        target_w = phase2_steady_weights.get(sym, Decimal("0"))
        target_v = post_liq_core * target_w
        cur_v = _position_value(positions, sym)
        delta = target_v - cur_v
        if delta > 0:
            buys.append(_make_buy_line(positions, sym, delta))
        elif delta < 0:
            sells.append(_make_sell_line(positions, sym, -delta))
        # delta == 0: no-op

    entry = PhaseTransitionEntry(
        from_phase=Phase.PHASE_1,
        to_phase=Phase.PHASE_2,
        sells=sells,
        buys=buys,
        residual_skipped=residual_skipped,
        is_phase3_activation=False,
    )

    alert = AlertEntry(
        alert_id="large_rebalance",
        context={
            "from_phase": "PHASE_1",
            "to_phase": "PHASE_2",
            "sells_count": str(len(sells)),
            "buys_count": str(len(buys)),
            "residual_skipped": ",".join(residual_skipped),
        },
    )

    return PhaseTransitionDecision(
        entry=entry,
        large_rebalance_alert=alert,
        phase3_activation_alert=None,
    )


# =============================================================================
# Phase 1 → Phase 3 (§7.2.2)
# =============================================================================

def build_phase1_to_phase3(
    *,
    positions: dict[str, Position],
    phase3_weights: dict[str, Decimal],
    position_residual_minimum_dollars: Decimal,
) -> PhaseTransitionDecision:
    """Build the Phase 1 → Phase 3 plan.

    Steps (§7.2.2):
      - All four positions (PYLD, JPIE, FBCG, AVUV) remain in active core.
      - Rebalance to phase3_weights.
      - I_0 is computed by the action layer from post-SELL portfolio
        value using the §4.1.1 annuity formula.

    GBIL should not be present in Phase 1; we don't liquidate it here.
    If it somehow is (operator manual intervention), it will be
    flagged by the §11.2 reconciliation checks before this point.

    `position_residual_minimum_dollars` is not used for liquidations
    here (no symbol is being dropped), but is plumbed for consistency.
    """
    _ = position_residual_minimum_dollars  # unused; symbol retained for API parity

    sells: list[SourceLine] = []
    buys: list[SourceLine] = []
    core_symbols = list(phase3_weights.keys())
    core_total = sum(
        (_position_value(positions, s) for s in core_symbols),
        start=Decimal("0"),
    )

    for sym in core_symbols:
        target_w = phase3_weights.get(sym, Decimal("0"))
        target_v = core_total * target_w
        cur_v = _position_value(positions, sym)
        delta = target_v - cur_v
        if delta > 0:
            buys.append(_make_buy_line(positions, sym, delta))
        elif delta < 0:
            sells.append(_make_sell_line(positions, sym, -delta))

    entry = PhaseTransitionEntry(
        from_phase=Phase.PHASE_1,
        to_phase=Phase.PHASE_3,
        sells=sells,
        buys=buys,
        residual_skipped=[],
        is_phase3_activation=True,
    )

    large_rebalance = AlertEntry(
        alert_id="large_rebalance",
        context={
            "from_phase": "PHASE_1",
            "to_phase": "PHASE_3",
            "sells_count": str(len(sells)),
            "buys_count": str(len(buys)),
        },
    )

    activation = AlertEntry(
        alert_id="phase3_activation",
        context={
            "from_phase": "PHASE_1",
            "note": "I_0_computed_at_action_layer_post_sell",
        },
    )

    return PhaseTransitionDecision(
        entry=entry,
        large_rebalance_alert=large_rebalance,
        phase3_activation_alert=activation,
    )


# =============================================================================
# Phase 2 → Phase 3 (§7.2.3)
# =============================================================================

def build_phase2_to_phase3(
    *,
    positions: dict[str, Position],
    phase3_weights: dict[str, Decimal],
    position_residual_minimum_dollars: Decimal,
) -> PhaseTransitionDecision:
    """Build the Phase 2 → Phase 3 plan.

    Steps (§7.2.3):
      1. SELL GBIL FULLY (no residual, I13 — Phase 3 latches, GBIL
         appears in no reachable phase from Phase 3).
      2. Refill PYLD/JPIE from residual to phase3 FI weights.
      3. Rebalance FBCG/AVUV to phase3 weights.

    Note: PYLD/JPIE are at residual (~$1,500) coming out of Phase 2
    because they were drawn down at the Phase 1→2 transition; they
    represent a fraction of a percent of core, so the rebalance buys
    largely fund themselves from GBIL liquidation proceeds plus any
    Growth-overweight selling.

    I_0 is computed at the action layer post-SELL.
    """
    _ = position_residual_minimum_dollars  # GBIL is liquidated fully; reserved for parity

    sells: list[SourceLine] = []
    buys: list[SourceLine] = []

    # Step 1: liquidate GBIL fully
    gbil_value = _position_value(positions, "GBIL")
    if gbil_value > 0:
        sells.append(_make_sell_line(positions, "GBIL", gbil_value))

    # Steps 2 & 3: rebalance the four Phase 3 positions to phase3_weights.
    # Core total includes GBIL liquidation proceeds plus current values
    # of the four Phase 3 symbols (PYLD residuals included).
    phase3_symbols = list(phase3_weights.keys())
    current_total = sum(
        (_position_value(positions, s) for s in phase3_symbols),
        start=Decimal("0"),
    )
    post_liq_core = current_total + gbil_value  # liquidation proceeds added

    for sym in phase3_symbols:
        target_w = phase3_weights.get(sym, Decimal("0"))
        target_v = post_liq_core * target_w
        cur_v = _position_value(positions, sym)
        delta = target_v - cur_v
        if delta > 0:
            buys.append(_make_buy_line(positions, sym, delta))
        elif delta < 0:
            sells.append(_make_sell_line(positions, sym, -delta))

    entry = PhaseTransitionEntry(
        from_phase=Phase.PHASE_2,
        to_phase=Phase.PHASE_3,
        sells=sells,
        buys=buys,
        residual_skipped=[],
        is_phase3_activation=True,
    )

    large_rebalance = AlertEntry(
        alert_id="large_rebalance",
        context={
            "from_phase": "PHASE_2",
            "to_phase": "PHASE_3",
            "sells_count": str(len(sells)),
            "buys_count": str(len(buys)),
            "gbil_liquidated": str(gbil_value),
        },
    )

    activation = AlertEntry(
        alert_id="phase3_activation",
        context={
            "from_phase": "PHASE_2",
            "note": "I_0_computed_at_action_layer_post_sell",
        },
    )

    return PhaseTransitionDecision(
        entry=entry,
        large_rebalance_alert=large_rebalance,
        phase3_activation_alert=activation,
    )


# =============================================================================
# I_0 annuity formula (used by action_layer at execution time)
# =============================================================================

def compute_phase3_i0(
    *,
    portfolio_value: Decimal,
    return_assumption: Decimal,
    inflation_assumption: Decimal,
    horizon_years: int,
) -> Decimal:
    """Compute Phase 3 starting income `I_0` per §4.1.1.

        I_0 = P × (r - i) / (12 × (1 - ((1+i)/(1+r))^N))

    Used by action_layer.py at Phase 3 activation, when post-SELL
    portfolio value is known. Plumbed here because the formula is
    part of the §7.2 phase-transition logic.

    The Decimal exponentiation works only for integer exponents; horizon
    is an int. The base (1+i)/(1+r) is exact, the integer power is exact.
    """
    r = return_assumption
    i = inflation_assumption
    n = horizon_years
    base = (Decimal("1") + i) / (Decimal("1") + r)
    base_to_n = base ** n
    one_minus = Decimal("1") - base_to_n
    if one_minus <= 0:
        # Degenerate when i >= r; the spec assumes r > i. Defensive
        # caller behavior: return P * 0 (zero income) and let the
        # operator notice the absurdity in the activation alert.
        return Decimal("0")
    return portfolio_value * (r - i) / (Decimal("12") * one_minus)


__all__ = [
    "PhaseTransitionDecision",
    "build_phase1_to_phase2",
    "build_phase1_to_phase3",
    "build_phase2_to_phase3",
    "compute_phase3_i0",
]
