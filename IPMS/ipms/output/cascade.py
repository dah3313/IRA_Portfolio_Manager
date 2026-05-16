"""
cascade.py — formatter for cascade_log.md.

Public exports:
    write_cascade_log(result, path) — produce cascade_log.md

See also: IPMS_SPECIFICATION.md §5.5 (cascade depth file contents),
    §4.3 (cascade-depth tracking — explicit requirement)


WHY THIS FILE EXISTS

The IPMV1 simulator did not track cascade depth at all. This was the
single biggest forensic gap when the operator needed to answer "how
close did we come to cascade exhaustion." The IPMS specification §4.3
makes cascade tracking explicit; cascade_log.md is the operator-facing
realization of that requirement.

The file has two sections:

  Section 1: per-tick cascade depth — wide table with every position's
      balance, distance-to-residual, and at_residual flag, plus the
      SGOV buffer's status (balance, target, pct of target, at_residual).
      One row per output-cadence tick. Wide deliberately — the spec
      §5.5 explicitly notes "the whole point is that the operator can
      scan one row and see the full cascade state at any given date."

  Section 2: cascade transition events — sparse table of moments when
      the cascade tier shifted (1→2, 2→3, 2→1 on recovery, etc.).
      Typically 0-4 rows for a 20-year clean run; many rows for a
      run with deep cascade activity.


WHAT THIS DOES IN v0.1.0

The IPMS v0.1.0 has no IRA PM, so no cascade activity actually happens
during runs. The frozen portfolio does not draw down assets toward
residual. The file produced by this formatter contains:

  Section 1: 20+ years of monthly rows where every asset is far above
    residual, every at_residual flag is false, and the cascade tier
    is always 2 (default FI tier).
  Section 2: empty — no cascade transitions to record.

This is correct behavior for v0.1.0. The schema is locked in now so
that when the IRA PM lands and starts producing real cascade activity,
the formatter just works without further code changes.

The test scaffold in checkpoint 6 will inject synthetic cascade events
to exercise this formatter against realistic data without an IRA PM.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from ipms.constants import (
    PER_POSITION_RESIDUAL_USD,
    SGOV_BUFFER_TARGET_USD,
)
from ipms.events import (
    AssetBalance,
    CascadeTier,
    CascadeTransition,
    Snapshot,
)
from ipms.output.formatting import (
    fmt_bool,
    fmt_date,
    fmt_dollars,
    fmt_weight,
)
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


# Asset column order. Same canonical order as balances.py — operator
# reading both files in parallel sees consistent column ordering.
# Spec §5.5 lists PYLD, JPIE, FBCG, AVUV, GBIL in this exact order.
_ASSET_COLUMN_ORDER = ["PYLD", "JPIE", "FBCG", "AVUV", "GBIL"]


def write_cascade_log(result: SimulationResult, path: Path) -> None:
    """
    Write cascade_log.md to `path`. Two sections per spec §5.5:

      Section 1 — per-tick cascade depth. One row per snapshot in
        result.snapshots. Always populated.
      Section 2 — cascade transition events. One row per
        CascadeTransition in result.cascade_transitions. Empty when
        no transitions occurred during the run.

    File header includes run window + generation timestamp + a brief
    explanation of how to read each section, so a future operator
    coming to this file cold doesn't need the spec.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_section_1(f, result)
        f.write("\n")
        _write_section_2(f, result)
        _write_footer(f)


# ============================================================================
# HEADER / FOOTER
# ============================================================================

def _write_header(f: TextIO, result: SimulationResult) -> None:
    """Top-of-file header block. Run identity + reading guide."""
    p = result.params
    md = result.metadata

    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# cascade_log.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")

    f.write(
        "Cascade activity log. Two sections: per-tick depth (Section 1) "
        "and transition events (Section 2). Section 1 is dense (one row "
        "per output-cadence tick); Section 2 is sparse (one row per "
        "tier transition; typically 0-4 for a clean 20-year run, more "
        "for stressed runs).\n\n"
    )

    f.write(
        f"Per-position residual: ${PER_POSITION_RESIDUAL_USD}. "
        f"SGOV target: ${SGOV_BUFFER_TARGET_USD}. "
        f"`at_residual` flags fire when a position's market_value drops "
        f"to or below the residual; cascade then walks to the next tier.\n\n"
    )


def _write_footer(f: TextIO) -> None:
    """End-of-file reading guide."""
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "**Section 1 (per-tick depth):** scan rows for any non-false "
        "`at_residual` flag. A `true` in any column is the moment a "
        "position became cascade-exhausted at its tier. SGOV's "
        "`pct_of_target` column shows how depleted the buffer was on "
        "each tick.\n"
    )
    f.write(
        "**Section 2 (transitions):** every row is a moment cascade "
        "decision-making changed source layer. `transition_type` "
        "names the shift (e.g., buffer→FI). `position_balances_at_"
        "transition` captures the full balance set at that moment so "
        "the operator can verify the transition fired against the "
        "correct residual condition.\n"
    )
    f.write(
        "**Distance-to-residual** is `market_value - per_position_residual`. "
        "Negative values mean the position is already at or below "
        "residual; positive values mean it is still drawable.\n"
    )
    f.write(
        "**Current cascade tier** is the source layer in effect for "
        "any withdrawal that would fire on this date: 1 (SGOV buffer), "
        "2 (FI bucket), 3 (Growth bucket).\n"
    )


# ============================================================================
# SECTION 1 — PER-TICK CASCADE DEPTH
# ============================================================================

def _section_1_columns() -> list[str]:
    """
    Column header list for Section 1. Defined as a function rather
    than a constant so the asset-order dependency is visible.

    Wide deliberately: the spec §5.5 calls this out as the design
    intent ("the whole point is that the operator can scan one row").
    """
    cols = [
        "date",
        "sgov_balance",
        "sgov_target",
        "sgov_pct_of_target",
        "sgov_at_residual",
    ]
    # Per-asset triple: balance, distance-to-residual, at_residual flag.
    # Order matches _ASSET_COLUMN_ORDER.
    for sym in _ASSET_COLUMN_ORDER:
        sym_lower = sym.lower()
        cols.append(f"{sym_lower}_balance")
        cols.append(f"{sym_lower}_distance_to_residual")
        cols.append(f"{sym_lower}_at_residual")
    cols.extend([
        "current_cascade_tier",
        "positions_at_residual_count",
    ])
    return cols


def _write_section_1(f: TextIO, result: SimulationResult) -> None:
    """
    Write Section 1: per-tick cascade depth table.

    One row per snapshot in result.snapshots. The full row must be
    self-contained — an operator reading any single row should be
    able to evaluate cascade health at that date without consulting
    other rows.
    """
    f.write("## Section 1: Per-tick cascade depth\n\n")

    columns = _section_1_columns()
    rows = [_section_1_row(snap) for snap in result.snapshots]

    if not rows:
        # Defensive — a SimulationResult with no snapshots is degenerate
        # but possible during testing. Emit the empty-table form rather
        # than the column header alone.
        f.write("(No snapshots collected — run produced no per-tick data.)\n")
        return

    write_md_table(f, columns, rows)


def _section_1_row(snap: Snapshot) -> list[str]:
    """
    Build one Section 1 data row from a Snapshot.

    Derives at_residual flags and distance_to_residual values from the
    Snapshot's existing AssetBalance objects. Spec §4.3 explicitly
    expects per-position depth to be tracked; the simulator stores
    enough on Snapshot (market_value per asset) to derive both at
    formatter time.
    """
    cells: list[str] = []

    cells.append(fmt_date(snap.timestamp))

    # SGOV buffer status. The buffer's "balance" is its market_value.
    # SGOV at_residual fires at the per-position residual same as any
    # other position — the buffer is special only in that it has its
    # own dollar target separate from cascade math.
    sgov_balance = snap.sgov_buffer_value
    cells.append(fmt_dollars(sgov_balance))
    cells.append(fmt_dollars(SGOV_BUFFER_TARGET_USD))
    if SGOV_BUFFER_TARGET_USD > 0:
        sgov_pct = sgov_balance / SGOV_BUFFER_TARGET_USD
    else:
        sgov_pct = Decimal(0)
    cells.append(fmt_weight(sgov_pct))
    cells.append(fmt_bool(_at_residual(sgov_balance)))

    # Per-asset triples. Snapshots from runs that don't include a
    # specific symbol (e.g., GBIL absent in early Phase 1 if the
    # registration is delayed) render that symbol's triple as zeros
    # with at_residual=true (a missing position is at residual by
    # definition — no value left to draw). This shouldn't happen in
    # the canonical Phase-1 setup but the defensive default keeps the
    # formatter robust.
    #
    # `positions_at_residual_count` filters out trivially-empty
    # positions (quantity == 0) like GBIL pre-Phase-2: those are
    # zero-by-design placeholders, not cascade-depleted positions.
    # Counting them would create persistent noise in the summary
    # column that would mask real cascade depletion. The per-asset
    # at_residual flag still renders the literal $0 ≤ $1,500 fact
    # for transparency.
    positions_at_residual = 0
    for sym in _ASSET_COLUMN_ORDER:
        if sym in snap.assets:
            ab = snap.assets[sym]
            balance = ab.market_value
            quantity = ab.quantity
        else:
            balance = Decimal(0)
            quantity = Decimal(0)

        distance = balance - PER_POSITION_RESIDUAL_USD
        is_at_residual = _at_residual(balance)
        # Only count toward the summary if the position is actually held
        # (quantity > 0). A zero-share placeholder is not cascade-depleted.
        if is_at_residual and quantity > 0:
            positions_at_residual += 1

        cells.append(fmt_dollars(balance))
        cells.append(fmt_dollars(distance))
        cells.append(fmt_bool(is_at_residual))

    # Trailing pair — current tier and count of at-residual positions.
    cells.append(str(snap.cascade_tier))
    cells.append(str(positions_at_residual))

    return cells


def _at_residual(market_value: Decimal) -> bool:
    """
    True if `market_value` is at or below the per-position residual.

    Defined as a small helper so the threshold semantics are unambiguous
    and the comparison rule (≤, not <) is consistent across all
    callers. The IRA PM's cascade selector uses ≤ (a position exactly
    at residual is excluded from candidate selection because selecting
    it would yield a zero-size trade after the residual cap applies),
    so the formatter mirrors that.
    """
    return market_value <= PER_POSITION_RESIDUAL_USD


# ============================================================================
# SECTION 2 — CASCADE TRANSITION EVENTS
# ============================================================================

def _section_2_columns() -> list[str]:
    """
    Column header list for Section 2.

    Per spec §5.5: date, transition type, trigger event, state of all
    positions at transition time, expected next-tier source asset,
    days-since-CB2-trigger.
    """
    return [
        "date",
        "transition_type",
        "trigger_event",
        "position_balances_at_transition",
        "expected_next_tier_source",
        "days_since_cb2_trigger",
    ]


def _write_section_2(f: TextIO, result: SimulationResult) -> None:
    """
    Write Section 2: cascade transition events table.

    Sparse table — typically empty for clean Phase 1 runs, populated
    when CB2 fires and cascade walks tiers. Each row records the
    moment a transition fired with enough state to forensically
    verify the transition was correct.
    """
    f.write("## Section 2: Cascade transition events\n\n")

    transitions = result.cascade_transitions
    if not transitions:
        f.write(
            "(No cascade transitions during this run. A clean Phase 1 "
            "run with no CB2 events typically produces zero rows here.)\n"
        )
        return

    columns = _section_2_columns()
    rows = [_section_2_row(trans) for trans in transitions]
    write_md_table(f, columns, rows)


def _section_2_row(trans: CascadeTransition) -> list[str]:
    """
    Build one Section 2 data row from a CascadeTransition event.

    The position-balances-at-transition cell is rendered as a compact
    `SYM=$value` semicolon-separated string rather than a nested table,
    so the row stays as a single Markdown line. Operators who need
    fully-tabular position data can cross-reference the Section 1 row
    for the same date.
    """
    cells: list[str] = []

    cells.append(fmt_date(trans.timestamp))
    cells.append(_format_transition_type(trans.from_tier, trans.to_tier))
    cells.append(trans.trigger_event or "")
    cells.append(_format_position_balances(trans.position_balances_at_transition))
    cells.append(_expected_next_tier_source(trans.to_tier))

    # days_since_cb2_trigger is Optional. Render empty for None
    # (transitions that fired pre-CB2 or in non-CB2 contexts).
    if trans.days_since_cb2_trigger is not None:
        cells.append(str(trans.days_since_cb2_trigger))
    else:
        cells.append("")

    return cells


def _format_transition_type(
    from_tier: CascadeTier, to_tier: CascadeTier
) -> str:
    """
    Render a tier shift in human-readable form.

    Examples:
        BUFFER → FIXED_INCOME → "buffer→FI"
        FIXED_INCOME → GROWTH → "FI→Growth"
        FIXED_INCOME → BUFFER → "FI→buffer (recovery)"
        GROWTH → FIXED_INCOME → "Growth→FI (recovery)"

    The "(recovery)" annotation marks downward transitions (operator's
    perspective: cascade pulling back toward less-deep sources). This
    is an interpretive layer — the from/to tiers themselves carry the
    raw fact; the annotation is for operator scanning.
    """
    name_map = {
        CascadeTier.BUFFER: "buffer",
        CascadeTier.FIXED_INCOME: "FI",
        CascadeTier.GROWTH: "Growth",
    }
    src = name_map.get(from_tier, str(from_tier))
    dst = name_map.get(to_tier, str(to_tier))

    # Recovery direction: from deeper tier (higher value) to less deep.
    # Tier values are 1=BUFFER, 2=FI, 3=GROWTH; deeper = lower
    # because buffer is the FIRST source during CB2 (it's "deep" in
    # the sense of operationally engaged before falling through to
    # FI/Growth). But the spec phrasing in §5.5 uses "buffer→FI" for
    # exhaustion of buffer, and "2→1 buffer-recovering" elsewhere — so
    # our "recovery" annotation tags the from_tier > to_tier case
    # because that is "moving toward shallower fallback" in the
    # IPMV1 sense and "the cascade re-engaging the buffer because
    # CB2 went active" in the new sense.
    #
    # Rather than litigate the semantic, just label moves toward
    # higher-numbered tier as "exhaustion" (cascade fell deeper) and
    # moves toward lower-numbered tier as "recovery" (cascade pulled
    # back). This matches operator intuition: "we fell into Growth"
    # is exhaustion-direction, "we recovered out of Growth" is
    # recovery-direction.
    if from_tier.value < to_tier.value:
        return f"{src}→{dst}"
    elif from_tier.value > to_tier.value:
        return f"{src}→{dst} (recovery)"
    else:
        return f"{src}→{dst} (no-op)"


def _format_position_balances(balances: dict[str, Decimal]) -> str:
    """
    Render a {symbol: balance} dict as a compact semicolon-separated
    string suitable for a single table cell.

    Example output:
        "PYLD=$1234.56; JPIE=$987.65; FBCG=$45000.00; AVUV=$32000.00; GBIL=$8000.00; SGOV=$0.00"

    Order follows _ASSET_COLUMN_ORDER for managed assets, then SGOV at
    end. Symbols not present in the dict are omitted (an empty dict
    produces an empty string — defensive default for malformed events).
    """
    if not balances:
        return ""

    parts: list[str] = []
    seen: set[str] = set()

    # Managed assets in canonical order
    for sym in _ASSET_COLUMN_ORDER:
        if sym in balances:
            parts.append(f"{sym}=${fmt_dollars(balances[sym])}")
            seen.add(sym)

    # SGOV always last if present
    if "SGOV" in balances:
        parts.append(f"SGOV=${fmt_dollars(balances['SGOV'])}")
        seen.add("SGOV")

    # Any other symbols (defensive — shouldn't happen with current
    # PROXY_MAP, but a stress test could inject extras)
    for sym, bal in balances.items():
        if sym not in seen:
            parts.append(f"{sym}=${fmt_dollars(bal)}")

    return "; ".join(parts)


def _expected_next_tier_source(to_tier: CascadeTier) -> str:
    """
    Human-readable hint about what asset the next withdrawal would
    source from given the new tier. Helps operators reading the
    transition row understand "what is the system about to do."

    Returns hint strings rather than asset symbols because the IRA PM
    selects most-overweight-in-class at each withdrawal time, which
    can vary month to month. A concrete symbol would be misleading.
    """
    if to_tier == CascadeTier.BUFFER:
        return "SGOV (buffer)"
    elif to_tier == CascadeTier.FIXED_INCOME:
        return "most-overweight FI position (PYLD or JPIE)"
    elif to_tier == CascadeTier.GROWTH:
        return "most-overweight Growth position (FBCG, AVUV, or GBIL)"
    else:
        return ""
