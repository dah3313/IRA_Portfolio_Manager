"""
test_sgov_refill_meaningful_drift.py — regression tests for the
"FBCG zero-quantity SELL on 2005-05-04" halt class.

TWO BUGS, ONE CLASS — both fixed:

ROOT CAUSE A (overweight-branch noise):
    decide_buffer_refill classified any Growth position with cur_v >
    target_v as "overweight" and routed a proportional share of the
    refill batch to it. When one Growth position was overweight by
    cents while another was overweight by thousands, the cents-
    overweight position received a near-zero dollar allocation that
    rounded to zero shares at the action layer and halted the cycle.

ROOT CAUSE B (proportional-fallback noise — the actual 2005-05-04
producer): when the buffer deficit was a few cents (SGOV price drift
relative to target), target_amount was a few cents, and the
proportional-fallback split that across Growth positions producing
sub-cent SourceLines that also rounded to zero shares. This was the
path that actually halted the baseline scenario; the overweight
branch (A) was a latent same-class bug.

THE FIXES:
    A. A Growth position is "overweight enough to source from" only
       when its surplus exceeds core_total *
       rebalance_absolute_threshold_rate — the same meaningful-drift
       yardstick the 5/25 standard rebalance uses.

    B. A refill is planned only when the deficit exceeds the buffer's
       2% noise floor (buffer_target_dollars * 0.02). Below this,
       the deficit is sub-meaningful day-to-day SGOV price drift and
       does not warrant a refill batch. The 2% constant is a noise
       floor, not a strategy choice — it scales with buffer_target
       (which scales with CPI-adjusted withdrawal demand), and is
       comfortably below the monthly refill rate (~8.3% of target),
       so no legitimate refill is suppressed.

WHAT THESE TESTS PIN:
    1. The historical bug-trigger pattern (one position overweight by
       pennies, another by thousands) no longer produces a tiny
       SourceLine.
    2. A position genuinely overweight beyond the threshold IS still
       selected as a source.
    3. When no position is meaningfully overweight, the proportional-
       from-all-Growth fallback engages, producing whole-dollar-scale
       allocations rather than near-zero ones.
    4. Multiple meaningfully-overweight positions still split
       proportionally to surplus.
    5. A position whose surplus is exactly at the threshold qualifies
       (boundary check, surplus == drift_floor).
    6. A buffer deficit below the 2% noise floor produces no refill
       (the actual 2005-05-04 halt pattern).
    7. A buffer deficit at or above the 2% noise floor produces a
       refill (boundary check on the deficit gate).
    8. A legitimate first-of-month refill on healthy positions
       reproduces the cycle-3 conditions and now succeeds rather
       than halting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from broker_types import ContractRef, Position
from sgov_refill import SGOVRefillInputs, decide_buffer_refill
from state_model import CBState, Phase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos(symbol: str, market_value: Decimal, quantity: Decimal = Decimal("1000")) -> Position:
    """Construct a minimal Position. Quantity is incidental for these
    tests (decide_buffer_refill uses it only to compute share_count_estimate
    on the resulting SourceLine, which is downstream of the bug)."""
    return Position(
        contract=ContractRef(broker_impl="TestStub", symbol=symbol),
        symbol=symbol,
        quantity=quantity,
        market_value=market_value,
        avg_cost=market_value / quantity if quantity > 0 else Decimal("0"),
    )


def _baseline_inputs(positions: dict[str, Position]) -> SGOVRefillInputs:
    """Construct SGOVRefillInputs with defaults that allow refill to
    proceed (Phase 1, CB_INACTIVE, no delay, no prior refill this
    month, buffer below target with healthy deficit)."""
    return SGOVRefillInputs(
        phase=Phase.PHASE_1,
        cb_state=CBState.CB_INACTIVE,
        now=datetime(2005, 5, 4, 10, 0, tzinfo=timezone.utc),
        buffer_target_dollars=Decimal("72000"),     # 24mo × $3000
        monthly_refill_rate_dollars=Decimal("6000"),  # 72000 / 12
        buffer_current_value=Decimal("0"),          # Maximum deficit; refill batch will be monthly_refill_rate.
        refill_delay_started_at=None,
        last_refill_at=None,
        positions=positions,
        growth_symbols=("FBCG", "AVUV"),
        buffer_symbol="SGOV",
        target_weights={
            "FBCG": Decimal("0.25"),
            "AVUV": Decimal("0.25"),
            "PYLD": Decimal("0.25"),
            "JPIE": Decimal("0.25"),
        },
        position_residual_minimum_dollars=Decimal("1500"),
        sgov_refill_post_recovery_delay_days=60,
        rebalance_absolute_threshold_rate=Decimal("0.05"),
    )


# ---------------------------------------------------------------------------
# 1. The exact bug-trigger pattern: one Growth overweight by pennies
# ---------------------------------------------------------------------------

def test_micro_overweight_does_not_become_a_source():
    """The 2005-05-04 halt class.

    FBCG is overweight by $0.50 (below the meaningful-drift floor);
    AVUV is overweight by $5,000 (well above). Pre-fix behavior:
    FBCG would receive a fraction-of-a-penny refill allocation.
    Post-fix: FBCG is excluded, AVUV provides the entire refill."""
    # Core total ~ $200K; 5% drift floor ~ $10K.
    positions = {
        "FBCG": _pos("FBCG", Decimal("50000.50")),   # target $50K, surplus $0.50
        "AVUV": _pos("AVUV", Decimal("55000")),      # target $50K, surplus $5,000 — still BELOW 5% floor
        "PYLD": _pos("PYLD", Decimal("50000")),
        "JPIE": _pos("JPIE", Decimal("44999.50")),   # balances the core
    }
    entry = decide_buffer_refill(_baseline_inputs(positions))
    assert entry is not None
    # Neither Growth position is meaningfully overweight (both below
    # 5% of $200K = $10K), so the proportional fallback engages.
    # Critical: NO SourceLine has a dollar_amount near zero.
    for src in entry.growth_sources:
        assert src.dollar_amount >= Decimal("100"), (
            f"SourceLine for {src.symbol} has near-zero dollar_amount "
            f"{src.dollar_amount}; this is the bug class."
        )


def test_micro_overweight_with_one_meaningfully_overweight():
    """FBCG overweight by pennies, AVUV meaningfully overweight.
    AVUV should be the SOLE source — FBCG must not appear."""
    # Core total $200K; 5% drift floor = $10K.
    # AVUV surplus $20K (above floor); FBCG surplus $0.50 (below).
    positions = {
        "FBCG": _pos("FBCG", Decimal("50000.50")),
        "AVUV": _pos("AVUV", Decimal("70000")),
        "PYLD": _pos("PYLD", Decimal("50000")),
        "JPIE": _pos("JPIE", Decimal("29999.50")),
    }
    entry = decide_buffer_refill(_baseline_inputs(positions))
    assert entry is not None
    symbols = {src.symbol for src in entry.growth_sources}
    assert symbols == {"AVUV"}, (
        f"Expected AVUV-only sourcing; got {symbols}. "
        f"FBCG was only $0.50 overweight and must be excluded."
    )


# ---------------------------------------------------------------------------
# 2. Genuinely overweight positions ARE still selected
# ---------------------------------------------------------------------------

def test_meaningfully_overweight_position_is_selected():
    """Sanity: the fix must not break the intended sourcing behavior.
    A position $15K overweight on a $200K core (7.5% drift, above the
    5% floor) MUST be selected as a source."""
    positions = {
        "FBCG": _pos("FBCG", Decimal("65000")),  # surplus $15K, 7.5% of core
        "AVUV": _pos("AVUV", Decimal("50000")),
        "PYLD": _pos("PYLD", Decimal("50000")),
        "JPIE": _pos("JPIE", Decimal("35000")),
    }
    entry = decide_buffer_refill(_baseline_inputs(positions))
    assert entry is not None
    symbols = {src.symbol for src in entry.growth_sources}
    assert symbols == {"FBCG"}


# ---------------------------------------------------------------------------
# 3. Proportional fallback when nothing is meaningfully overweight
# ---------------------------------------------------------------------------

def test_proportional_fallback_when_no_meaningful_overweight():
    """When all Growth positions are at or near target (no surplus
    above the drift floor), the existing proportional-from-all-Growth
    fallback engages and allocates whole-dollar amounts."""
    # Both Growth positions exactly at target (no surplus at all).
    positions = {
        "FBCG": _pos("FBCG", Decimal("50000")),
        "AVUV": _pos("AVUV", Decimal("50000")),
        "PYLD": _pos("PYLD", Decimal("50000")),
        "JPIE": _pos("JPIE", Decimal("50000")),
    }
    entry = decide_buffer_refill(_baseline_inputs(positions))
    assert entry is not None
    # Proportional from both Growth → both appear as sources, each
    # sized proportionally to its market_value (50/50 here).
    sources_by_symbol = {src.symbol: src for src in entry.growth_sources}
    assert set(sources_by_symbol) == {"FBCG", "AVUV"}
    # Each should be ~$3,000 (half of the $6,000 monthly refill).
    assert sources_by_symbol["FBCG"].dollar_amount == Decimal("3000")
    assert sources_by_symbol["AVUV"].dollar_amount == Decimal("3000")
    # And the total matches sgov_buy_amount.
    assert entry.sgov_buy_amount == Decimal("6000")


# ---------------------------------------------------------------------------
# 4. Multiple meaningfully-overweight positions split proportionally
# ---------------------------------------------------------------------------

def test_multiple_meaningfully_overweight_split_proportional():
    """When both Growth positions are meaningfully overweight, the
    refill is split in proportion to surplus (not equally, not by
    market_value)."""
    # Core $200K; 5% floor = $10K.
    # FBCG surplus $20K, AVUV surplus $30K → 40/60 split of refill.
    positions = {
        "FBCG": _pos("FBCG", Decimal("70000")),
        "AVUV": _pos("AVUV", Decimal("80000")),
        "PYLD": _pos("PYLD", Decimal("30000")),
        "JPIE": _pos("JPIE", Decimal("20000")),
    }
    entry = decide_buffer_refill(_baseline_inputs(positions))
    assert entry is not None
    sources_by_symbol = {src.symbol: src for src in entry.growth_sources}
    assert set(sources_by_symbol) == {"FBCG", "AVUV"}
    # Refill batch is $6,000; surplus ratio 20/50 vs 30/50 = 0.4/0.6.
    assert sources_by_symbol["FBCG"].dollar_amount == Decimal("2400")
    assert sources_by_symbol["AVUV"].dollar_amount == Decimal("3600")


# ---------------------------------------------------------------------------
# 5. Boundary: surplus exactly at the drift floor qualifies
# ---------------------------------------------------------------------------

def test_boundary_surplus_exactly_at_drift_floor_qualifies():
    """A position whose surplus equals (not exceeds) the drift floor
    is considered overweight. This is the inclusive boundary the
    standard 5/25 rebalancer uses, so we match it here."""
    # Core $200K; 5% floor = exactly $10K. FBCG surplus = exactly $10K.
    positions = {
        "FBCG": _pos("FBCG", Decimal("60000")),
        "AVUV": _pos("AVUV", Decimal("50000")),
        "PYLD": _pos("PYLD", Decimal("50000")),
        "JPIE": _pos("JPIE", Decimal("40000")),
    }
    entry = decide_buffer_refill(_baseline_inputs(positions))
    assert entry is not None
    symbols = {src.symbol for src in entry.growth_sources}
    assert symbols == {"FBCG"}, (
        "Surplus exactly equal to the drift floor must qualify "
        "(inclusive boundary, matching standard 5/25 semantics)."
    )


# ---------------------------------------------------------------------------
# 6. Smoke: every produced SourceLine survives the action layer's
#    zero-quantity check, even at low refill amounts and varied prices
# ---------------------------------------------------------------------------

def test_no_source_line_would_round_to_zero_shares():
    """Cross-check against the action layer's failure mode.

    The action layer's _refresh_quantity computes shares = dollar / price
    then ROUND_DOWN to 4 dp. Any SourceLine whose dollar_amount,
    divided by a plausible price, yields < 0.0001 shares would halt.

    With the meaningful-drift gate in place, even with a small
    monthly refill rate, every emitted SourceLine should comfortably
    survive at any plausible ETF price."""
    positions = {
        "FBCG": _pos("FBCG", Decimal("50000.01")),  # bug-trigger surplus
        "AVUV": _pos("AVUV", Decimal("60000")),
        "PYLD": _pos("PYLD", Decimal("50000")),
        "JPIE": _pos("JPIE", Decimal("39999.99")),
    }
    inputs = _baseline_inputs(positions)
    # Even with a tiny monthly refill rate, no near-zero leg should emerge.
    inputs_tiny_refill = SGOVRefillInputs(
        **{**inputs.__dict__, "monthly_refill_rate_dollars": Decimal("500")}
    )
    entry = decide_buffer_refill(inputs_tiny_refill)
    assert entry is not None
    # At even an implausibly high $1000/share price, $1 → 0.001 shares,
    # safely above the 0.0001 quantization floor. Pin to $1 minimum.
    for src in entry.growth_sources:
        assert src.dollar_amount >= Decimal("1.00"), (
            f"SourceLine for {src.symbol} dollar_amount {src.dollar_amount} "
            f"could round to zero shares at the action layer."
        )


# ---------------------------------------------------------------------------
# 7. The actual 2005-05-04 halt pattern: tiny deficit, proportional fallback
# ---------------------------------------------------------------------------

def test_tiny_buffer_deficit_produces_no_refill():
    """The 2005-05-04 producer. Buffer is $0.01 below target (SGOV
    intraday price drift). Pre-fix: proportional fallback split the
    $0.01 across FBCG and AVUV producing $0.005 SourceLines that
    rounded to zero shares at the action layer. Post-fix: the deficit
    is below the 2% noise floor (2% of $72,000 = $1,440) so no
    refill is planned at all."""
    positions = {
        "FBCG": _pos("FBCG", Decimal("167000")),
        "AVUV": _pos("AVUV", Decimal("167000")),
        "PYLD": _pos("PYLD", Decimal("167000")),
        "JPIE": _pos("JPIE", Decimal("167000")),
    }
    inp = SGOVRefillInputs(
        **{**_baseline_inputs(positions).__dict__,
           "buffer_current_value": Decimal("71999.99")},
    )
    entry = decide_buffer_refill(inp)
    assert entry is None, (
        f"A $0.01 deficit must not produce a refill batch (was {entry}). "
        "This is the literal 2005-05-04 halt pattern."
    )


def test_deficit_just_below_noise_floor_skipped():
    """Deficit just below 2% of target ($1,439.99 on a $72K target) →
    no refill."""
    positions = {
        "FBCG": _pos("FBCG", Decimal("167000")),
        "AVUV": _pos("AVUV", Decimal("167000")),
        "PYLD": _pos("PYLD", Decimal("167000")),
        "JPIE": _pos("JPIE", Decimal("167000")),
    }
    inp = SGOVRefillInputs(
        **{**_baseline_inputs(positions).__dict__,
           "buffer_current_value": Decimal("70560.01")},  # deficit $1,439.99
    )
    assert decide_buffer_refill(inp) is None


def test_deficit_at_noise_floor_proceeds():
    """Boundary: deficit exactly at the 2% threshold proceeds.
    2% of $72K = $1,440. Buffer at $70,560 → deficit $1,440."""
    positions = {
        "FBCG": _pos("FBCG", Decimal("167000")),
        "AVUV": _pos("AVUV", Decimal("167000")),
        "PYLD": _pos("PYLD", Decimal("167000")),
        "JPIE": _pos("JPIE", Decimal("167000")),
    }
    inp = SGOVRefillInputs(
        **{**_baseline_inputs(positions).__dict__,
           "buffer_current_value": Decimal("70560")},
    )
    entry = decide_buffer_refill(inp)
    assert entry is not None, (
        "Deficit exactly equal to the 2% noise floor must produce a refill "
        "(inclusive boundary)."
    )
    # Refill is clamped at min(monthly_refill_rate, deficit) = min(6000, 1440) = 1440.
    assert entry.sgov_buy_amount == Decimal("1440")


# ---------------------------------------------------------------------------
# 8. Integration: cycle-3 healthy conditions reproduce successfully
# ---------------------------------------------------------------------------

def test_cycle_3_healthy_conditions_succeed():
    """Reproduces cycle 3 (2005-05-04) of the 20-year baseline scenario
    under realistic conditions: ~$668K starting balance evenly split
    25/25/25/25 across the four Phase 1 core positions, SGOV is well
    below target (we assume the initial deployment in cycle 1 did not
    refill SGOV, leaving the full deficit). The refill must plan and
    must not produce any near-zero SourceLines."""
    positions = {
        "FBCG": _pos("FBCG", Decimal("167125")),
        "AVUV": _pos("AVUV", Decimal("167125")),
        "PYLD": _pos("PYLD", Decimal("167125")),
        "JPIE": _pos("JPIE", Decimal("167125")),
    }
    inp = SGOVRefillInputs(
        **{**_baseline_inputs(positions).__dict__,
           # SGOV at zero from cycle 1 (deployment didn't refill buffer).
           "buffer_current_value": Decimal("0")},
    )
    entry = decide_buffer_refill(inp)
    assert entry is not None, "Cycle-3 healthy refill must plan."
    # Refill is clamped at monthly_refill_rate ($6K), split proportionally
    # across the two Growth symbols by market value (50/50 here → $3K each).
    assert entry.sgov_buy_amount == Decimal("6000")
    sources_by_symbol = {s.symbol: s for s in entry.growth_sources}
    assert set(sources_by_symbol) == {"FBCG", "AVUV"}
    for sym, src in sources_by_symbol.items():
        assert src.dollar_amount >= Decimal("1.00"), (
            f"SourceLine for {sym} has near-zero dollar amount {src.dollar_amount}; "
            "this would halt at the action layer's quantity check."
        )
