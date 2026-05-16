"""
recovery_events.py — formatter for recovery_events.md.

Public exports:
    write_recovery_events(result, path) — produce recovery_events.md

See also: IPMS_SPECIFICATION.md §5.6


WHY THIS FILE EXISTS

CB1/CB2 entry transitions live in cb_events.md (per spec §5.6). But
the recovery state machine — Stage 1 (resume rebalancing) and Stage 2
(resume withdrawals + dispatch refill) — has its own separate event
type (RecoveryEvent) and its own ownership boundary (recovery.py
owns EXIT from halted states, vs. circuit_breakers.py owning ENTRY).

Mirroring that ownership in the file output: recovery transitions get
their own file rather than mixing into cb_events.md. This makes
forensic queries cleaner ("when did Stage 2 confirm during the GFC
recovery?") and keeps each file's column shape simple.

Per the 2026-05-09 calibration, Stage 1 thresholds differ between
CB1 and CB2:
  - CB1 Stage 1: rolling return ≥ -6%
  - CB2 Stage 1: rolling return ≥ -10%
  - Stage 2 (both): rolling return ≥ 0%


WHAT THIS DOES IN v0.1.0

Empty file with column header.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from ipms.events import RecoveryEvent
from ipms.output.formatting import fmt_bool, fmt_date, fmt_optional, fmt_pct
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


def write_recovery_events(result: SimulationResult, path: Path) -> None:
    """
    Write recovery_events.md. One row per RecoveryEvent.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_table(f, result)
        _write_footer(f)


def _write_header(f: TextIO, result: SimulationResult) -> None:
    p = result.params
    md = result.metadata
    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# recovery_events.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")
    f.write(
        "Recovery state machine log. One row per Stage 1 / Stage 2\n"
        "transition. Stage 1 = resume rebalancing (CB1) or advance CB2 to\n"
        "stage_1_done. Stage 2 = resume withdrawals + dispatch SGOV refill.\n"
        "Each stage requires 2-week confirmation before firing.\n\n"
    )


def _columns() -> list[str]:
    return [
        "date",
        "cb_id",
        "stage",
        "confirmed",
        "rolling_return",
    ]


def _write_table(f: TextIO, result: SimulationResult) -> None:
    events = result.recovery_events
    if not events:
        f.write(
            "(No recovery events during this run. v0.1.0 runs without an IRA\n"
            "PM produce no recovery transitions; the schema below is locked\n"
            "in for future runs once the IRA PM is integrated.)\n\n"
        )
        write_md_table(f, _columns(), [])
        return

    rows = [_row(e) for e in events]
    write_md_table(f, _columns(), rows)


def _row(e: RecoveryEvent) -> list[str]:
    return [
        fmt_date(e.timestamp),
        e.cb_id.value,
        str(e.stage),
        fmt_bool(e.confirmed),
        fmt_optional(e.rolling_return, fmt_pct),
    ]


def _write_footer(f: TextIO) -> None:
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- `cb_id` identifies which CB is recovering: `cb1` or `cb2`. Each\n"
        "  has its own Stage 1 threshold (CB1: -6%, CB2: -10% per\n"
        "  2026-05-09 calibration). Stage 2 threshold is 0% for both.\n"
        "- `stage`: 1 (resume rebalancing for CB1; advance CB2 toward\n"
        "  stage_1_done) or 2 (resume withdrawals + schedule refill).\n"
        "- `confirmed` true means this row is the actual confirmation\n"
        "  (week 2 of the rolling return crossing threshold). False is the\n"
        "  pending-recovery-entry transition (week 1 — needs another week\n"
        "  to confirm).\n"
        "- `rolling_return` is the trigger signal at the moment of\n"
        "  transition — fractional, e.g. `0.0023` = +0.23%.\n"
    )
