"""
sgov_refill.py — SGOV buffer refill decisions (§7.4).

PURPOSE:
    Decide whether to plan a buffer refill batch this cycle, and if so,
    what to sell (Growth) and how much SGOV to buy.

DESIGN (per §7.4):
    All of the following must be true to plan a refill:
      1. Phase is 1, 2, or 3 (refill active in all phases).
      2. Buffer market value < buffer target.
      3. 60-day post-recovery delay window has elapsed (or never applied).
      4. CB state is not CB2 (I10: refill suspended in any CB2 state).
      5. Refill cadence (monthly): no refill batch this calendar month.

    When all conditions met:
      - Compute amount = min(monthly_refill_rate, current_deficit).
      - Source from most-overweight Growth position (tie-break by larger $).
      - If no Growth position is overweight, source proportionally from
        all Growth positions.
      - All Growth SELLs clamp at residual floor (I12). If a position
        would breach residual, reduce SELL and corresponding refill.
      - Refill is ALWAYS Growth-sourced, never FI-sourced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from broker_types import Position
from plan_model import BufferRefillEntry, SourceLine
from state_model import CBState, Phase


@dataclass(frozen=True)
class SGOVRefillInputs:
    """Inputs to the SGOV refill decision."""
    phase: Phase
    cb_state: CBState
    now: datetime

    # Buffer state
    buffer_target_dollars: Decimal
    monthly_refill_rate_dollars: Decimal
    buffer_current_value: Decimal  # SGOV market value from broker
    refill_delay_started_at: Optional[datetime]
    last_refill_at: Optional[datetime]

    # Positions for sourcing
    positions: dict[str, Position]
    growth_symbols: tuple[str, ...]
    buffer_symbol: str

    # Active-phase target weights (used only to detect "overweight Growth")
    target_weights: dict[str, Decimal]

    # Tunables
    position_residual_minimum_dollars: Decimal
    sgov_refill_post_recovery_delay_days: int


def _same_calendar_month(a: datetime, b: datetime) -> bool:
    """Whether two timestamps fall in the same calendar year+month."""
    return a.year == b.year and a.month == b.month


def _sellable(pos: Position, residual: Decimal) -> Decimal:
    avail = pos.market_value - residual
    return avail if avail > 0 else Decimal("0")


def decide_buffer_refill(inputs: SGOVRefillInputs) -> Optional[BufferRefillEntry]:
    """Return a BufferRefillEntry if all §7.4 conditions are met,
    else None.

    The returned entry contains the SELL breakdown (one or more Growth
    symbols) and the matching SGOV BUY amount. The action layer
    executes SELLs first, then the BUY (§8.2.3).
    """
    # (1) Phase: always active.

    # (4) CB state: refill suspended in CB2 (I10).
    if inputs.cb_state == CBState.CB2:
        return None

    # (2) Buffer below target?
    deficit = inputs.buffer_target_dollars - inputs.buffer_current_value
    if deficit <= 0:
        return None

    # (3) Post-recovery delay window
    if inputs.refill_delay_started_at is not None:
        elapsed = inputs.now - inputs.refill_delay_started_at
        if elapsed < timedelta(days=inputs.sgov_refill_post_recovery_delay_days):
            return None

    # (5) Refill cadence: once per calendar month
    if inputs.last_refill_at is not None and _same_calendar_month(inputs.now, inputs.last_refill_at):
        return None

    # Compute amount to refill this batch
    target_amount = min(inputs.monthly_refill_rate_dollars, deficit)
    if target_amount <= 0:
        return None

    # Source the refill from Growth
    residual = inputs.position_residual_minimum_dollars

    # Compute current Growth values
    growth_values: dict[str, Decimal] = {}
    for s in inputs.growth_symbols:
        p = inputs.positions.get(s)
        growth_values[s] = p.market_value if p is not None else Decimal("0")
    growth_total = sum(growth_values.values(), Decimal("0"))

    if growth_total <= 0:
        # No Growth to sell; can't refill this cycle.
        return None

    # Build a pool of available-to-sell amounts per Growth symbol.
    # Step 1: detect overweight Growth positions (vs target).
    core_total = sum(
        (inputs.positions[s].market_value if s in inputs.positions else Decimal("0")
         for s in inputs.target_weights),
        Decimal("0"),
    )
    overweight_growth: dict[str, Decimal] = {}
    for s in inputs.growth_symbols:
        w = inputs.target_weights.get(s)
        if w is None:
            continue
        target_v = core_total * w
        cur_v = growth_values[s]
        if cur_v > target_v:
            overweight_growth[s] = cur_v - target_v

    sources_dict: dict[str, Decimal] = {}

    if overweight_growth:
        # Source from overweight Growth positions only.
        # If only one is overweight → source entirely from it.
        # If multiple → source proportionally to surplus.
        total_surplus = sum(overweight_growth.values(), Decimal("0"))
        for s, surplus in overweight_growth.items():
            allocated = (surplus / total_surplus) * target_amount
            # Clamp at sellable (residual floor)
            p = inputs.positions[s]
            sellable_max = _sellable(p, residual)
            take = min(allocated, sellable_max)
            if take > 0:
                sources_dict[s] = take
    else:
        # No overweight: source proportionally from all Growth (§7.4 last
        # paragraph above the rule list).
        for s in inputs.growth_symbols:
            p = inputs.positions.get(s)
            if p is None or p.market_value <= 0:
                continue
            share = (p.market_value / growth_total) * target_amount
            sellable_max = _sellable(p, residual)
            take = min(share, sellable_max)
            if take > 0:
                sources_dict[s] = take

    actual_total = sum(sources_dict.values(), Decimal("0"))
    if actual_total <= 0:
        # Every Growth position is at residual; nothing to refill this cycle.
        return None

    # Build SourceLine entries
    sources_lines: list[SourceLine] = []
    for s, dollars in sources_dict.items():
        p = inputs.positions[s]
        if p.quantity > 0 and p.market_value > 0:
            per_share = p.market_value / p.quantity
            share_est = dollars / per_share if per_share > 0 else Decimal("0")
        else:
            share_est = Decimal("0")
        sources_lines.append(SourceLine(
            symbol=s,
            dollar_amount=dollars,
            share_count_estimate=share_est,
        ))

    return BufferRefillEntry(
        growth_sources=sources_lines,
        sgov_buy_amount=actual_total,
    )


__all__ = ["SGOVRefillInputs", "decide_buffer_refill"]
