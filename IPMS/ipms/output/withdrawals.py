"""
withdrawals.py — formatter for withdrawals.md.

Public exports:
    write_withdrawals(result, path) — produce withdrawals.md

See also: IPMS_SPECIFICATION.md §5.4 (file contents — structured time-series),
    spec column list under "withdrawals.md"


WHY THIS FILE EXISTS

The IPMV1 simulator inlined withdrawal information in its prose report
without a structured per-event log. That made it impossible to answer
forensic questions like "which months were halted, and for how long?"
or "what was the cumulative trueup balance trajectory?" without
re-running the simulator.

withdrawals.md exposes one row per withdrawal attempt — successes,
partials, and halts all on equal footing. The cumulative_withdrawn
column lets the operator visually verify "is income being delivered
on schedule?" with a single scan.


WHAT THIS DOES IN v0.1.0

The IPMS v0.1.0 has no IRA PM, so no withdrawals fire. The file
contains the header, column list, and a one-line note that no
withdrawals occurred. Schema is locked in now so that when the IRA PM
lands and starts producing real withdrawal records, the formatter
just works without further code changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from ipms.events import CascadeTier, WithdrawalRecord
from ipms.output.formatting import fmt_date, fmt_dollars, fmt_optional
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


# Tier-name mapping. The cascade selector uses tier numbers internally,
# but the file output benefits from human-readable names alongside.
_TIER_NAMES = {
    CascadeTier.BUFFER: "buffer",
    CascadeTier.FIXED_INCOME: "FI",
    CascadeTier.GROWTH: "Growth",
}


def write_withdrawals(result: SimulationResult, path: Path) -> None:
    """
    Write withdrawals.md to `path`. One row per WithdrawalRecord in
    chronological order. Empty file (header + note) when no withdrawals
    were captured.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_table(f, result)
        _write_footer(f)


def _write_header(f: TextIO, result: SimulationResult) -> None:
    """Top-of-file header block."""
    p = result.params
    md = result.metadata
    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# withdrawals.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")
    f.write(
        "Per-withdrawal log. One row per attempted monthly withdrawal —\n"
        "successes, partial fills, and halts. Cumulative-withdrawn column\n"
        "shows total dollars delivered to the operator's bank account\n"
        "across the run.\n\n"
    )


def _columns() -> list[str]:
    """Column header list per spec §5.4."""
    return [
        "scheduled_date",
        "execution_date",
        "target_amount",
        "filled_amount",
        "source_asset",
        "source_tier",
        "tier_name",
        "halt_reason",
        "trueup_balance_after",
        "cumulative_withdrawn",
    ]


def _write_table(f: TextIO, result: SimulationResult) -> None:
    """Write the withdrawal table."""
    withdrawals = result.withdrawals
    if not withdrawals:
        f.write(
            "(No withdrawals during this run. v0.1.0 runs without an IRA PM\n"
            "produce zero withdrawals; the schema below is locked in for\n"
            "future runs once the IRA PM is integrated.)\n\n"
        )
        # Still write the column header for schema clarity.
        write_md_table(f, _columns(), [])
        return

    rows = [_row(w) for w in withdrawals]
    write_md_table(f, _columns(), rows)


def _row(w: WithdrawalRecord) -> list[str]:
    """Build one row from a WithdrawalRecord."""
    tier_str = ""
    tier_name = ""
    if w.source_tier is not None:
        tier_str = str(w.source_tier.value)
        tier_name = _TIER_NAMES.get(w.source_tier, "")

    return [
        fmt_date(w.scheduled_date),
        fmt_date(w.timestamp),     # execution_date is the event timestamp
        fmt_dollars(w.target_amount),
        fmt_dollars(w.filled_amount),
        w.source_asset or "",
        tier_str,
        tier_name,
        w.halt_reason or "",
        fmt_dollars(w.trueup_balance_after),
        fmt_dollars(w.cumulative_withdrawn),
    ]


def _write_footer(f: TextIO) -> None:
    """End-of-file reading guide."""
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- `scheduled_date` is the month's nominal sell day (4 business days\n"
        "  before the 15th in production); `execution_date` is when the\n"
        "  withdrawal actually fired (may differ if the sell day landed on a\n"
        "  market holiday).\n"
        "- `target_amount` is the month's full requested withdrawal.\n"
        "  `filled_amount` is what the IRA PM actually delivered. Their\n"
        "  difference accrues to `trueup_balance_after` for next month.\n"
        "- `source_asset` and `source_tier` identify which position fed the\n"
        "  withdrawal. tier 1 = SGOV buffer, tier 2 = FI, tier 3 = Growth.\n"
        "- `halt_reason` is non-blank only on halted withdrawals. Per\n"
        "  operator design intent, the only legitimate halt reasons are\n"
        "  `cascade_exhausted` (every position at residual — catastrophic)\n"
        "  and `phase_2_cessation` (Phase 2 halts withdrawals by design).\n"
        "  Any other halt reason indicates an IRA PM defect.\n"
        "- `cumulative_withdrawn` is total filled amount across all months\n"
        "  in the run so far. Useful for visual verification that monthly\n"
        "  income is being delivered on schedule.\n"
    )
