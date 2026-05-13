"""
rebalancer.py — Rebalancing decisions (§7.5).

PURPOSE:
    Three distinct rebalancing flavors live here, each gated by phase:
    - Phase 1 / Phase 3 standard 5/25 rebalance (§7.5.1).
    - Phase 2 opportunistic two-state swing on deploy/recover (§7.5.2.a).
    - Phase 2 semi-annual reallocation to target weights (§7.5.2.b).

DESIGN:
    - Pure functions; produce OrderEntry lists or None.
    - All rebalance trades clamp at residual floor (I12).
    - FI-sacrosanct (I5): rebalance plans that would require SELL FI →
      BUY Growth are SUPPRESSED (§7.5.1, return None).
    - Suspended during CB1 / CB2 (§6.6 table; §7.5.1 precondition).
    - Suspended during Phase 3 latched-but-pending window (I15, D12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from broker_types import OrderSide, OrderType, Position
from plan_model import AlertEntry, OrderEntry, SourceLine
from state_model import CBState, IncomeState, Phase, Phase2SwingState, PendingCounter


# =============================================================================
# Shared helpers
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


# =============================================================================
# Phase 1 / Phase 3 standard rebalance (§7.5.1)
# =============================================================================

@dataclass(frozen=True)
class StandardRebalanceInputs:
    """Inputs for 5/25 evaluation."""
    phase: Phase
    cb_state: CBState
    positions: dict[str, Position]
    target_weights: dict[str, Decimal]
    growth_symbols: tuple[str, ...]
    fi_symbols: tuple[str, ...]

    # Tunables
    rebalance_absolute_threshold_rate: Decimal
    rebalance_relative_threshold_rate: Decimal
    position_residual_minimum_dollars: Decimal


@dataclass(frozen=True)
class StandardRebalanceResult:
    """A standard rebalance plan plus a suppression flag for the cycle log."""
    orders: list[OrderEntry]
    suppressed_for_fi_sacrosanct: bool


def _drift_metrics(
    positions: dict[str, Position],
    target_weights: dict[str, Decimal],
    core_total: Decimal,
) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """For each core symbol return (drift_dollars, abs_pct, rel_pct).

    abs_pct = |delta| / core_total
    rel_pct = |delta| / target_value     (undefined / Decimal('Infinity') if target = 0)
    """
    out: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for s, w in target_weights.items():
        target_v = core_total * w
        cur_v = _position_value(positions, s)
        delta = cur_v - target_v
        abs_v = abs(delta)
        abs_pct = abs_v / core_total if core_total > 0 else Decimal("0")
        if target_v > 0:
            rel_pct = abs_v / target_v
        else:
            # Target zero — only an issue if cur_v > 0. Treat as
            # "fully out of band" by using a large rel_pct; the
            # caller's threshold check will trip.
            rel_pct = Decimal("999") if cur_v > 0 else Decimal("0")
        out[s] = (delta, abs_pct, rel_pct)
    return out


def decide_standard_rebalance(
    inputs: StandardRebalanceInputs,
) -> StandardRebalanceResult:
    """5/25 rebalance for Phase 1 / Phase 3 (§7.5.1).

    Returns an empty orders list if no rebalance is needed, or if the
    plan would violate I5 (FI → Growth). The latter case sets
    `suppressed_for_fi_sacrosanct=True` for cycle-log visibility.

    The caller (decision_layer.py) is responsible for:
      - Phase gating (only Phase 1 or 3)
      - CB gating (only CB_INACTIVE)
      - I15 gating (only when phase3.schedule_state non-null in Phase 3)
    """
    pos = inputs.positions
    weights = inputs.target_weights
    core_total = sum(
        (_position_value(pos, s) for s in weights),
        start=Decimal("0"),
    )
    if core_total <= 0:
        return StandardRebalanceResult(orders=[], suppressed_for_fi_sacrosanct=False)

    metrics = _drift_metrics(pos, weights, core_total)

    # Trigger if any symbol crosses either threshold
    abs_thresh = inputs.rebalance_absolute_threshold_rate
    rel_thresh = inputs.rebalance_relative_threshold_rate
    triggered = any(
        (m[1] >= abs_thresh) or (m[2] >= rel_thresh)
        for m in metrics.values()
    )
    if not triggered:
        return StandardRebalanceResult(orders=[], suppressed_for_fi_sacrosanct=False)

    # Build SELL of each overweight, BUY of each underweight.
    # Group by direction:
    sells: list[OrderEntry] = []
    buys: list[OrderEntry] = []
    sells_fi_total = Decimal("0")
    buys_growth_total = Decimal("0")
    residual = inputs.position_residual_minimum_dollars

    for s, (delta, _abs, _rel) in metrics.items():
        if delta > 0:  # overweight → SELL surplus
            sellable_max = _sellable(pos, s, residual)
            sell_dollars = min(delta, sellable_max)
            if sell_dollars > 0:
                sells.append(OrderEntry(
                    symbol=s,
                    side=OrderSide.SELL,
                    dollar_amount=sell_dollars,
                    order_type=OrderType.MKT,
                    note=f"rebalance overweight by {delta}",
                ))
                if s in inputs.fi_symbols:
                    sells_fi_total += sell_dollars
        elif delta < 0:  # underweight → BUY deficit
            buy_dollars = -delta
            if buy_dollars > 0:
                buys.append(OrderEntry(
                    symbol=s,
                    side=OrderSide.BUY,
                    dollar_amount=buy_dollars,
                    order_type=OrderType.MKT,
                    note=f"rebalance underweight by {-delta}",
                ))
                if s in inputs.growth_symbols:
                    buys_growth_total += buy_dollars

    # I5 check: would this plan sell FI to buy Growth?
    # The plan violates I5 iff there are FI sells AND Growth buys
    # whose dollar amounts overlap (we can't fund Growth buys from
    # FI sells without violating). We use the simplest sufficient
    # condition: if any FI is being sold AND any Growth is being
    # bought, the plan is suppressed.
    if sells_fi_total > 0 and buys_growth_total > 0:
        return StandardRebalanceResult(
            orders=[],
            suppressed_for_fi_sacrosanct=True,
        )

    return StandardRebalanceResult(
        orders=[*sells, *buys],
        suppressed_for_fi_sacrosanct=False,
    )


# =============================================================================
# Phase 2 opportunistic two-state swing (§7.5.2.a)
# =============================================================================

@dataclass(frozen=True)
class Phase2SwingInputs:
    """Inputs for Phase 2 swing evaluation."""
    cb_state: CBState
    swing_state: Phase2SwingState
    deploy_pending: PendingCounter
    recover_pending: PendingCounter

    signal_available: bool
    signal_value: Optional[Decimal]
    in_phase3_grace_window: bool  # suspend during grace

    positions: dict[str, Position]

    # Phase 2 weights
    steady_weights: dict[str, Decimal]      # FBCG/AVUV/GBIL
    deployed_weights: dict[str, Decimal]    # FBCG/AVUV only (GBIL → residual)
    growth_symbols: tuple[str, ...]
    fi_symbols: tuple[str, ...]              # ('GBIL',)

    # Tunables
    phase2_opportunistic_trigger_rate: Decimal
    phase2_opportunistic_recovery_rate: Decimal
    confirmation_window_weeks: int
    position_residual_minimum_dollars: Decimal


@dataclass(frozen=True)
class Phase2SwingResult:
    """Outcome of a Phase 2 swing evaluation."""
    orders: list[OrderEntry]
    new_swing_state: Phase2SwingState
    new_deploy_pending: PendingCounter
    new_recover_pending: PendingCounter
    deploy_fired: bool
    recover_fired: bool


def _advance(c: PendingCounter, holds: bool, now: datetime) -> PendingCounter:
    if holds:
        return PendingCounter(
            cycles_confirmed=c.cycles_confirmed + 1,
            first_observed_at=c.first_observed_at or now,
        )
    return PendingCounter()


def _hold(c: PendingCounter) -> PendingCounter:
    return c


def decide_phase2_swing(
    inputs: Phase2SwingInputs,
    *,
    now: datetime,
) -> Phase2SwingResult:
    """Evaluate the Phase 2 opportunistic swing per §7.5.2.a.

    Returns updated state and any orders to execute. Suspend conditions
    (signal UNAVAILABLE, CB1/CB2 active, Phase 3 grace) hold the
    counters and emit no orders.
    """
    suspended = (
        inputs.in_phase3_grace_window
        or not inputs.signal_available
        or inputs.cb_state in (CBState.CB1, CBState.CB2)
    )

    if suspended:
        return Phase2SwingResult(
            orders=[],
            new_swing_state=inputs.swing_state,
            new_deploy_pending=_hold(inputs.deploy_pending),
            new_recover_pending=_hold(inputs.recover_pending),
            deploy_fired=False,
            recover_fired=False,
        )

    sv = inputs.signal_value
    if sv is None:
        # Should be unreachable given signal_available check, but
        # defensive: hold counters.
        return Phase2SwingResult(
            orders=[],
            new_swing_state=inputs.swing_state,
            new_deploy_pending=_hold(inputs.deploy_pending),
            new_recover_pending=_hold(inputs.recover_pending),
            deploy_fired=False,
            recover_fired=False,
        )

    deploy_holds = sv <= inputs.phase2_opportunistic_trigger_rate
    recover_holds = sv >= inputs.phase2_opportunistic_recovery_rate

    new_deploy = _advance(inputs.deploy_pending, deploy_holds, now)
    new_recover = _advance(inputs.recover_pending, recover_holds, now)

    window = inputs.confirmation_window_weeks
    pos = inputs.positions
    residual = inputs.position_residual_minimum_dollars

    deploy_fired = False
    recover_fired = False
    orders: list[OrderEntry] = []
    new_state = inputs.swing_state

    if (inputs.swing_state == Phase2SwingState.STEADY
        and new_deploy.cycles_confirmed >= window):
        # Deploy: SELL GBIL to residual, BUY FBCG/AVUV per deployed_weights.
        gbil_sym = None
        for s in inputs.fi_symbols:
            gbil_sym = s
            break
        if gbil_sym is not None:
            gbil_sellable = _sellable(pos, gbil_sym, residual)
            if gbil_sellable > 0:
                orders.append(OrderEntry(
                    symbol=gbil_sym,
                    side=OrderSide.SELL,
                    dollar_amount=gbil_sellable,
                    order_type=OrderType.MKT,
                    note="phase2_opportunistic_deploy: GBIL → residual",
                ))
                # Distribute proceeds across deployed_weights (Growth only).
                proceeds = gbil_sellable
                gw_total = sum(
                    (w for s, w in inputs.deployed_weights.items()
                     if s in inputs.growth_symbols),
                    start=Decimal("0"),
                )
                if gw_total > 0:
                    for s, w in inputs.deployed_weights.items():
                        if s not in inputs.growth_symbols:
                            continue
                        buy_amount = proceeds * (w / gw_total)
                        if buy_amount > 0:
                            orders.append(OrderEntry(
                                symbol=s,
                                side=OrderSide.BUY,
                                dollar_amount=buy_amount,
                                order_type=OrderType.MKT,
                                note="phase2_opportunistic_deploy buy",
                            ))
        new_state = Phase2SwingState.DEPLOYED
        new_deploy = PendingCounter()
        deploy_fired = True

    elif (inputs.swing_state == Phase2SwingState.DEPLOYED
          and new_recover.cycles_confirmed >= window):
        # Recover: SELL Growth proportional to overweight, BUY GBIL to 10%.
        # Compute current values and target (steady_weights) values.
        core_total = sum(
            (_position_value(pos, s) for s in inputs.steady_weights),
            start=Decimal("0"),
        )
        if core_total > 0:
            for s, w in inputs.steady_weights.items():
                if s not in inputs.growth_symbols:
                    continue
                target_v = core_total * w
                cur_v = _position_value(pos, s)
                surplus = cur_v - target_v
                if surplus <= 0:
                    continue
                sellable_max = _sellable(pos, s, residual)
                sell_dollars = min(surplus, sellable_max)
                if sell_dollars > 0:
                    orders.append(OrderEntry(
                        symbol=s,
                        side=OrderSide.SELL,
                        dollar_amount=sell_dollars,
                        order_type=OrderType.MKT,
                        note="phase2_opportunistic_recover: Growth → target",
                    ))
            # Buy GBIL up to 10% of core (its steady weight)
            for s, w in inputs.steady_weights.items():
                if s in inputs.growth_symbols:
                    continue
                target_v = core_total * w
                cur_v = _position_value(pos, s)
                deficit = target_v - cur_v
                if deficit > 0:
                    orders.append(OrderEntry(
                        symbol=s,
                        side=OrderSide.BUY,
                        dollar_amount=deficit,
                        order_type=OrderType.MKT,
                        note="phase2_opportunistic_recover: GBIL → target",
                    ))
        new_state = Phase2SwingState.STEADY
        new_recover = PendingCounter()
        recover_fired = True

    return Phase2SwingResult(
        orders=orders,
        new_swing_state=new_state,
        new_deploy_pending=new_deploy,
        new_recover_pending=new_recover,
        deploy_fired=deploy_fired,
        recover_fired=recover_fired,
    )


# =============================================================================
# Phase 2 semi-annual reallocation (§7.5.2.b)
# =============================================================================

@dataclass(frozen=True)
class Phase2SemiAnnualInputs:
    """Inputs for Phase 2 semi-annual realign."""
    swing_state: Phase2SwingState
    positions: dict[str, Position]
    steady_weights: dict[str, Decimal]
    deployed_weights: dict[str, Decimal]
    growth_symbols: tuple[str, ...]
    fi_symbols: tuple[str, ...]
    position_residual_minimum_dollars: Decimal


def decide_phase2_semi_annual(
    inputs: Phase2SemiAnnualInputs,
) -> list[OrderEntry]:
    """Scrape accumulated drift back to target weights for the current
    Phase 2 sub-state. Phase 2 has no CB framework; this fires
    unconditionally on the configured semi-annual dates.

    Caller is responsible for the date gate (§6.5.1: the weekly cycle
    on or after each phase2_reallocation_dates entry).
    """
    weights = (
        inputs.deployed_weights
        if inputs.swing_state == Phase2SwingState.DEPLOYED
        else inputs.steady_weights
    )
    pos = inputs.positions
    residual = inputs.position_residual_minimum_dollars
    core_total = sum(
        (_position_value(pos, s) for s in weights),
        start=Decimal("0"),
    )
    if core_total <= 0:
        return []

    orders: list[OrderEntry] = []
    for s, w in weights.items():
        target_v = core_total * w
        cur_v = _position_value(pos, s)
        delta = cur_v - target_v
        if delta > 0:
            sellable_max = _sellable(pos, s, residual)
            sell_dollars = min(delta, sellable_max)
            if sell_dollars > 0:
                orders.append(OrderEntry(
                    symbol=s,
                    side=OrderSide.SELL,
                    dollar_amount=sell_dollars,
                    order_type=OrderType.MKT,
                    note="phase2_semi_annual: scrape overweight",
                ))
        elif delta < 0:
            buy_dollars = -delta
            if buy_dollars > 0:
                orders.append(OrderEntry(
                    symbol=s,
                    side=OrderSide.BUY,
                    dollar_amount=buy_dollars,
                    order_type=OrderType.MKT,
                    note="phase2_semi_annual: refill underweight",
                ))
    return orders


__all__ = [
    "StandardRebalanceInputs",
    "StandardRebalanceResult",
    "decide_standard_rebalance",
    "Phase2SwingInputs",
    "Phase2SwingResult",
    "decide_phase2_swing",
    "Phase2SemiAnnualInputs",
    "decide_phase2_semi_annual",
]
