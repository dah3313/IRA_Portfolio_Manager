"""
phase_transition.py — formatter for phase_transition.md.

Public exports:
    write_phase_transition(result, path) — produce phase_transition.md

See also: IPMS_SPECIFICATION.md §5.6 ("phase_transition.md")


WHY THIS FILE EXISTS

Phase 2 transition is a single, large, one-shot event. It liquidates
PYLD and JPIE, buys FBCG/AVUV/GBIL to Phase 2 weights, and disables
withdrawals. Either it fires once during the run window, or it doesn't
fire at all.

A standalone file for this event makes its handling explicit:
  - Empty file with note: Phase 2 didn't trigger (run window too short).
  - Single-row file: Phase 2 fired; the row records when, whether
    deferred by CB activity, the pre/post allocation, and links to
    the rebalances.md file for the actual trade list.


WHAT THIS DOES IN v0.1.0

Empty file with column header. The 8-year anniversary for the
default canonical run (start 2005-04-25) is 2013-04-25, well within
the 20-year window — so once IRA PM is integrated, this file will
populate for the canonical run.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TextIO

from ipms.events import PhaseTransition
from ipms.output.formatting import (
    fmt_bool,
    fmt_date,
    fmt_weight,
)
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


def write_phase_transition(result: SimulationResult, path: Path) -> None:
    """
    Write phase_transition.md. Zero or one row per simulator run.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_table(f, result)
        _write_footer(f)


def _write_header(f: TextIO, result: SimulationResult) -> None:
    p = result.params
    md = result.metadata
    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# phase_transition.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")
    f.write(
        "Phase 2 transition record. Single-row file when Phase 2 fired\n"
        "during the run window; empty (with note) when the window was too\n"
        "short to reach the 8-year anniversary or when the IRA PM deferred\n"
        "the transition past the run end.\n\n"
        "Trades executed at the transition are recorded in rebalances.md\n"
        "with trigger_type `phase_2_transition`. This file records the\n"
        "metadata only.\n\n"
    )


def _columns() -> list[str]:
    return [
        "scheduled_date",
        "execution_date",
        "deferred_by_cb",
        "forced_after_max_delay",
        "pre_allocation",
        "post_allocation",
    ]


def _write_table(f: TextIO, result: SimulationResult) -> None:
    transitions = result.phase_transitions
    if not transitions:
        f.write(
            "(Phase 2 did not trigger during this run. v0.1.0 runs without\n"
            "an IRA PM never advance phases; the schema below is locked in\n"
            "for future runs once the IRA PM is integrated.)\n\n"
        )
        write_md_table(f, _columns(), [])
        return

    # Typically one row, but a forced re-transition could produce more —
    # render all rows so the operator sees the full history.
    rows = [_row(t) for t in transitions]
    write_md_table(f, _columns(), rows)


def _row(t: PhaseTransition) -> list[str]:
    return [
        fmt_date(t.scheduled_date),
        fmt_date(t.timestamp),
        fmt_bool(t.deferred_by_cb),
        fmt_bool(t.forced_after_max_delay),
        _format_allocation(t.pre_allocation),
        _format_allocation(t.post_allocation),
    ]


def _format_allocation(alloc: dict[str, Decimal]) -> str:
    """
    Render a {symbol: weight} dict as compact `SYM=W%` semicolon-
    separated cell. Same posture as cascade Section 2's position
    balances cell.
    """
    if not alloc:
        return ""
    parts = []
    # Canonical asset order for stability across runs
    canonical = ["PYLD", "JPIE", "FBCG", "AVUV", "GBIL"]
    seen = set()
    for sym in canonical:
        if sym in alloc:
            parts.append(f"{sym}={fmt_weight(alloc[sym])}")
            seen.add(sym)
    # Any others (defensive — shouldn't happen)
    for sym, w in alloc.items():
        if sym not in seen:
            parts.append(f"{sym}={fmt_weight(w)}")
    return "; ".join(parts)


def _write_footer(f: TextIO) -> None:
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- `scheduled_date` is the 8-year anniversary of `start_date`.\n"
        "  `execution_date` is when the transition actually fired — same\n"
        "  date as scheduled if both CBs were inactive at the anniversary,\n"
        "  later if `deferred_by_cb` is true.\n"
        "- `deferred_by_cb` true means CB1 or CB2 was active at the\n"
        "  anniversary and the IRA PM waited for both to clear before\n"
        "  transitioning. `forced_after_max_delay` true means the IRA PM's\n"
        "  6-month max-delay backstop fired (transition went through\n"
        "  regardless of CB state).\n"
        "- `pre_allocation` / `post_allocation` are compact target-weight\n"
        "  maps: `SYM=weight; SYM=weight; ...`. For trade details, see\n"
        "  rebalances.md filtered to trigger_type `phase_2_transition`.\n"
    )
