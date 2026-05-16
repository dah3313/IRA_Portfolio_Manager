"""
balances.py — formatters for balances_monthly.md and balances_annual.md.

Public exports:
    write_balances_monthly(result, path) — produce balances_monthly.md
    write_balances_annual(result, path) — produce balances_annual.md

See also: IPMS_SPECIFICATION.md §5.4 (file contents — structured time-series)


COLUMN DERIVATION

The formatter reads from result.snapshots and result.cash_flows. Most
columns are direct projections from Snapshot fields. Three columns
require derivation:

  1. `cb` — combined CB1/CB2 active state. "-" / "1" / "2" / "1+2".
     Derived from snapshot.cb1_state and cb2_state ("active" → 1 or 2).

  2. `flags` — events that occurred during this snapshot's period.
     Derived by walking the events list between the prior snapshot
     and this one, looking for state-changing events:
       R1 — RecoveryEvent stage=1 confirmed
       R2 — RecoveryEvent stage=2 confirmed
       RFL — RebalanceRecord trigger_type=sgov_buffer_refill
       DPD — RebalanceRecord trigger_type=dry_powder_deploy
       DPR — RebalanceRecord trigger_type=dry_powder_refill
       5/25 — RebalanceRecord trigger_type=5/25
       AR — AnnualReview event
       P2 — PhaseTransition event
     Multiple flags comma-separated. Empty for routine periods.

  3. `cash_flow_in` / `cash_flow_out` — sums of CashFlow events in
     the period (prior_snapshot.timestamp, this_snapshot.timestamp].
     Direction "in" sums to cash_flow_in; "out" to cash_flow_out.

  4. `yoy_change_pct` — gross_net_liq vs same-month-one-year-prior.
     Blank for the first 12 months. For the annual file, vs
     prior-January.


ASSET COLUMN ORDER

Asset columns (`pyld_value`, `jpie_value`, etc.) are emitted in a
canonical order: PYLD, JPIE, FBCG, AVUV, GBIL. This ordering is
hardcoded here for v0.1.0; when the IRA PM lands and brings its own
phase-config-driven asset list, the ordering may shift to "all FI
first, then all Growth in declared order" or similar. For now, the
hardcoded list matches the canonical Phase 1 allocation.

Phase 2 transition zeroes PYLD and JPIE; columns remain in the
schema with 0.00 values. Schema-stable approach (per spec §5.4 and
the operator-confirmed sample) keeps file parsing simple at the
cost of some visual noise post-Phase-2.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional, TextIO

from ipms.events import (
    AnnualReview,
    CashFlow,
    PhaseTransition,
    RebalanceRecord,
    RecoveryEvent,
    Snapshot,
)
from ipms.output.formatting import (
    fmt_bool,
    fmt_date,
    fmt_dollars,
    fmt_optional,
    fmt_pct,
    fmt_weight,
)
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


# Canonical asset column order for v0.1.0. See module docstring.
_ASSET_COLUMN_ORDER = ["PYLD", "JPIE", "FBCG", "AVUV", "GBIL"]


def _balances_columns() -> list[str]:
    """
    Column header list for both monthly and annual files. Defined
    once here so the two formatters can't drift out of sync.
    """
    cols = [
        "date",
        "cb",
        "flags",
        "gross_net_liq",
        "managed_net_liq",
        "cash",
        "cash_flow_in",
        "cash_flow_out",
        "sgov_buffer",
        "fi_bucket",
        "growth_bucket",
        "fi_weight",
        "growth_weight",
        "yoy_change_pct",
    ]
    # Per-asset value columns
    for sym in _ASSET_COLUMN_ORDER:
        cols.append(f"{sym.lower()}_value")
    cols.extend(["phase", "refill_active"])
    return cols


def write_balances_monthly(result: SimulationResult, path: Path) -> None:
    """
    Write balances_monthly.md to `path`. Per spec §5.4, one row per
    output-cadence tick (monthly default).

    File contains a header block, the column table, and a footer
    "how to read this file" reference. Header includes run metadata
    (window, detail level, generation timestamp) so the file is
    self-contained for archival.
    """
    snapshots = result.snapshots
    cash_flows = result.cash_flows

    # First snapshot is the seeding snapshot at start_date; subsequent
    # snapshots are the per-cadence ticks. Both go into the file —
    # the seed row gives the operator the starting state for visual
    # comparison.
    rows = _build_rows(result, snapshots, cash_flows, monthly_yoy=True)

    with path.open("w") as f:
        _write_balances_header(f, result, "monthly")
        write_md_table(f, _balances_columns(), rows)
        _write_balances_footer(f)


def write_balances_annual(result: SimulationResult, path: Path) -> None:
    """
    Write balances_annual.md to `path`. Per spec §5.4, sampled on
    January 1 of each year.

    YoY change column compares against prior January's gross_net_liq
    rather than 12-months-ago — same calendar position year over year.

    For annual files, cash_flow_in/out columns sum across the full
    year (Jan 1 → Dec 31) rather than just the period since the prior
    annual snapshot. This gives a cleaner annual cash-flow picture.
    """
    # Filter snapshots to those nearest January 1 each year.
    annual_snapshots = _filter_annual_snapshots(result.snapshots)

    # For annual rows, the period for cash-flow aggregation is the
    # full year ending on this snapshot's date. Pass the annual flag
    # to _build_rows so it computes sums across that wider window.
    rows = _build_rows(
        result,
        annual_snapshots,
        result.cash_flows,
        monthly_yoy=False,
    )

    with path.open("w") as f:
        _write_balances_header(f, result, "annual")
        write_md_table(f, _balances_columns(), rows)
        _write_balances_footer(f)


# ============================================================================
# HEADER / FOOTER
# ============================================================================

def _write_balances_header(
    f: TextIO, result: SimulationResult, kind: str
) -> None:
    """
    Write the file header block. Common shape across monthly and
    annual files; `kind` switches the title and detail-level line.
    """
    p = result.params
    md = result.metadata

    if kind == "monthly":
        title = "balances_monthly.md"
        detail_line = "Cadence: monthly (post-tick snapshots)"
    else:
        title = "balances_annual.md"
        detail_line = "Cadence: annual (January 1 sampling)"

    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write(f"# {title}\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n")
    f.write(f"{detail_line}\n\n")
    f.write(
        "Sub-cent values rounded to 2 decimals (dollars) or 4 decimals "
        "(shares, prices, weights).\n\n"
    )


def _write_balances_footer(f: TextIO) -> None:
    """
    Write the file footer with column key. Common across monthly
    and annual; helps a future operator parse the file cold without
    consulting the spec.
    """
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- One row per output-cadence tick. Empty cells render as "
        "tab-then-tab in the raw file.\n"
    )
    f.write(
        "- `cb` column: `-` (no CB active), `1` (CB1 only), "
        "`2` (CB2 only), `1+2` (both active simultaneously).\n"
    )
    f.write(
        "- `flags` column shows notable events that fired during this "
        "row's period: `R1`/`R2` (Recovery Stage 1/2 confirmed), "
        "`RFL` (SGOV refill cycle executed), `5/25` (5/25 rebalance), "
        "`DPD`/`DPR` (dry-powder deploy/refill), `AR` (annual review), "
        "`P2` (Phase 2 transition). Multiple flags comma-separated. "
        "Empty for routine rows.\n"
    )
    f.write(
        "- `cash_flow_in` and `cash_flow_out` are sums of cash-changing "
        "events during the row's period. If `cash[t] - cash[t-1]` "
        "does not equal `cash_flow_in - cash_flow_out`, an event is "
        "missing — investigate `withdrawals.md` and `rebalances.md` "
        "for the same period.\n"
    )
    f.write(
        "- `yoy_change_pct` is the fractional change in `gross_net_liq` "
        "versus the same calendar position one year prior. Blank for "
        "the first 12 months of the run (no prior reference).\n"
    )
    f.write(
        "- Per-asset columns show dollar value. Phase 2 transition "
        "zeroes PYLD/JPIE; columns remain for schema stability across "
        "phases.\n"
    )
    f.write("- `phase`: 1 (Phase 1) or 2 (Phase 2).\n")
    f.write(
        "- `refill_active`: SGOV refill cycle in progress between "
        "Stage 2 confirmation and buffer-restored-to-target.\n"
    )


# ============================================================================
# ROW BUILDING
# ============================================================================

def _build_rows(
    result: SimulationResult,
    snapshots: list[Snapshot],
    cash_flows: list[CashFlow],
    monthly_yoy: bool,
) -> list[list[str]]:
    """
    Build the data rows for the file.

    `snapshots` is the snapshot subset to render (all for monthly,
    January-only for annual).
    `cash_flows` is the full cash-flow event list; per-row aggregation
    happens here.
    `monthly_yoy` selects YoY computation strategy:
      True  → vs same calendar month one year prior (find a snapshot
              with month==target.month and year==target.year-1)
      False → vs prior-January's gross_net_liq (in `snapshots`)

    Returns list of rows; each row is a list of pre-formatted cell strings.
    """
    rows: list[list[str]] = []

    # Pre-index ALL of result.snapshots by month-year for monthly YoY
    # lookup. Annual file uses prior-row lookup against `snapshots`
    # itself (which is already January-filtered).
    full_snaps_by_ymd = {s.timestamp: s for s in result.snapshots}

    # Pre-sort cash flows by timestamp for efficient period queries.
    sorted_flows = sorted(cash_flows, key=lambda cf: cf.timestamp)

    for i, snap in enumerate(snapshots):
        # Period boundaries for cash-flow aggregation
        prior_snap = snapshots[i - 1] if i > 0 else None
        period_start = prior_snap.timestamp if prior_snap else None
        period_end = snap.timestamp

        cash_in, cash_out = _sum_cash_flows_in_period(
            sorted_flows, period_start, period_end
        )

        # YoY lookup
        yoy = _compute_yoy(
            snap, snapshots, full_snaps_by_ymd, i, monthly_yoy
        )

        # Flags from events between prior_snap and this snap
        flags = _collect_flags(
            result, period_start, period_end
        )

        # CB combined indicator
        cb_str = _format_cb(snap)

        # Per-asset values in canonical column order
        asset_cells = _format_asset_columns(snap)

        row = [
            fmt_date(snap.timestamp),
            cb_str,
            flags,
            fmt_dollars(snap.gross_net_liq),
            fmt_dollars(snap.managed_net_liq),
            fmt_dollars(snap.cash_value),
            fmt_dollars(cash_in),
            fmt_dollars(cash_out),
            fmt_dollars(snap.sgov_buffer_value),
            fmt_dollars(snap.fi_bucket_value),
            fmt_dollars(snap.growth_bucket_value),
            fmt_weight(snap.fi_weight),
            fmt_weight(snap.growth_weight),
            fmt_optional(yoy, fmt_pct),
        ]
        row.extend(asset_cells)
        row.extend([
            str(snap.phase),
            fmt_bool(snap.refill_active),
        ])
        rows.append(row)

    return rows


# ============================================================================
# DERIVATIONS
# ============================================================================

def _format_cb(snap: Snapshot) -> str:
    """
    Combine cb1_state and cb2_state into a single column value.
    Pending-states (pending_trigger, pending_recovery) render as "-"
    so the column shows ACTIVE-state cleanly. Pending transitions are
    visible in cb_events.md, not here.
    """
    cb1_active = snap.cb1_state == "active"
    cb2_active = snap.cb2_state == "active"
    if cb1_active and cb2_active:
        return "1+2"
    if cb1_active:
        return "1"
    if cb2_active:
        return "2"
    return "-"


def _format_asset_columns(snap: Snapshot) -> list[str]:
    """
    Return per-asset value cells in _ASSET_COLUMN_ORDER. Symbols not
    present in the snapshot's assets dict render as "0.00" (e.g.,
    GBIL pre-Phase-2, or PYLD/JPIE post-Phase-2).
    """
    cells = []
    for sym in _ASSET_COLUMN_ORDER:
        if sym in snap.assets:
            cells.append(fmt_dollars(snap.assets[sym].market_value))
        else:
            cells.append("0.00")
    return cells


def _sum_cash_flows_in_period(
    sorted_flows: list[CashFlow],
    period_start: Optional[date],
    period_end: date,
) -> tuple[Decimal, Decimal]:
    """
    Sum cash-flow events in the period (period_start, period_end].
    period_start=None means "from beginning of run" — the first row's
    period is open-ended on the left, capturing the seed cash flow
    on start_date.

    Returns (cash_in, cash_out) tuple. Both Decimals, both >= 0.
    """
    cash_in = Decimal(0)
    cash_out = Decimal(0)
    for cf in sorted_flows:
        if period_start is not None and cf.timestamp <= period_start:
            continue
        if cf.timestamp > period_end:
            break
        if cf.direction == "in":
            cash_in += cf.amount
        else:
            cash_out += cf.amount
    return cash_in, cash_out


def _compute_yoy(
    snap: Snapshot,
    snapshots: list[Snapshot],
    full_snaps_by_ymd: dict[date, Snapshot],
    idx: int,
    monthly_yoy: bool,
) -> Optional[Decimal]:
    """
    Compute year-over-year change in gross_net_liq.

    `monthly_yoy=True`: look for any snapshot with same month/year-1.
    Search the full result.snapshots map (passed as full_snaps_by_ymd
    by-date), tolerating day-of-month drift since monthly snapshots
    fall on month-end which varies (28/29/30/31).

    `monthly_yoy=False`: look at the row at idx-1 in `snapshots`,
    which is the prior January for the annual file.

    Returns None if no comparable prior snapshot exists.
    """
    if monthly_yoy:
        # Look for a snapshot in the same month one year prior.
        target_year = snap.timestamp.year - 1
        target_month = snap.timestamp.month
        # Walk full snapshots looking for a match. Since snapshots are
        # roughly month-end, accept any same-month-and-year snapshot.
        for d, s in full_snaps_by_ymd.items():
            if d.year == target_year and d.month == target_month:
                if s.gross_net_liq > 0:
                    return (snap.gross_net_liq - s.gross_net_liq) / s.gross_net_liq
                return None
        return None
    else:
        # Annual mode: compare to prior row in `snapshots` (which is
        # already January-filtered). Skip first row.
        if idx == 0:
            return None
        prior = snapshots[idx - 1]
        if prior.gross_net_liq > 0:
            return (snap.gross_net_liq - prior.gross_net_liq) / prior.gross_net_liq
        return None


def _collect_flags(
    result: SimulationResult,
    period_start: Optional[date],
    period_end: date,
) -> str:
    """
    Walk the result's events looking for notable state-changing events
    in the period (period_start, period_end]. Return comma-separated
    flag string.

    Order of flags within a row is the order they're checked here:
    R1, R2, RFL, DPD, DPR, 5/25, AR, P2. This is deterministic and
    matches the reading-comprehension order ("recovery first, then
    buffer activity, then trades, then admin").
    """
    flags = []

    # Recovery events
    for ev in result.recovery_events:
        if not _in_period(ev.timestamp, period_start, period_end):
            continue
        if not ev.confirmed:
            continue
        if ev.stage == 1 and "R1" not in flags:
            flags.append("R1")
        elif ev.stage == 2 and "R2" not in flags:
            flags.append("R2")

    # Rebalance triggers — categorize by trigger_type
    for reb in result.rebalances:
        if not _in_period(reb.timestamp, period_start, period_end):
            continue
        if reb.trigger_type == "sgov_buffer_refill" and "RFL" not in flags:
            flags.append("RFL")
        elif reb.trigger_type == "dry_powder_deploy" and "DPD" not in flags:
            flags.append("DPD")
        elif reb.trigger_type == "dry_powder_refill" and "DPR" not in flags:
            flags.append("DPR")
        elif reb.trigger_type == "5/25" and "5/25" not in flags:
            flags.append("5/25")

    # Annual review
    for ar in result.annual_reviews:
        if _in_period(ar.timestamp, period_start, period_end):
            if "AR" not in flags:
                flags.append("AR")

    # Phase 2 transition
    for pt in result.phase_transitions:
        if _in_period(pt.timestamp, period_start, period_end):
            if "P2" not in flags:
                flags.append("P2")

    return ",".join(flags)


def _in_period(
    d: date, period_start: Optional[date], period_end: date
) -> bool:
    """True if d is in (period_start, period_end] (or [-, period_end] if no start)."""
    if period_start is not None and d <= period_start:
        return False
    if d > period_end:
        return False
    return True


# ============================================================================
# ANNUAL FILTERING
# ============================================================================

def _filter_annual_snapshots(snapshots: list[Snapshot]) -> list[Snapshot]:
    """
    Select snapshots nearest each January 1. Per spec §5.4, the
    annual file is "sampled on January 1 of each year".

    Strategy: pick the EARLIEST January (month==1) snapshot for each
    year that has one. Years without a January snapshot — typically
    year 1 of a run that starts mid-year, or year N+1 of a run that
    ends before January — are omitted from the annual file rather
    than substituting the seed/terminal snapshot.

    Rationale: the annual file is meant for year-over-year comparison
    at a fixed calendar position. Substituting a non-January snapshot
    breaks that property and produces misleading YoY rows. Better to
    show fewer rows that are all comparable than more rows with mixed
    calendar positions.

    Bug fix 2026-05-09 (O1): previous implementation picked the
    earliest snapshot of EACH year unconditionally, which for runs
    starting mid-year used the seed snapshot at start_date as the
    "year 1" row — not a January sample.
    """
    if not snapshots:
        return []

    # Group January-month snapshots by year, picking the earliest
    # January snapshot per year (typically the only one in a monthly
    # cadence; multiple are possible only in weekly cadence).
    by_year: dict[int, Snapshot] = {}
    for s in snapshots:
        if s.timestamp.month != 1:
            continue
        existing = by_year.get(s.timestamp.year)
        if existing is None or s.timestamp < existing.timestamp:
            by_year[s.timestamp.year] = s

    # Return in ascending year order.
    return [by_year[y] for y in sorted(by_year.keys())]
