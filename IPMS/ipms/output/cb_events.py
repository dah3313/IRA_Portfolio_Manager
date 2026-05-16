"""
cb_events.py — formatter for cb_events.md.

Public exports:
    write_cb_events(result, path) — produce cb_events.md

See also: IPMS_SPECIFICATION.md §5.6 ("cb_events.md")


WHY THIS FILE EXISTS

CB1 and CB2 state-transition history is the operator's primary tool
for answering "did the circuit breakers behave correctly during the
2008 drawdown?" or "how often did CB1 false-trigger on flat-but-
withdrawing periods?" Each transition is a row, with the rolling-
return signal value at the moment of transition and (for activations)
the trigger portfolio value.

Recovery events get their own file (recovery_events.md) rather than
sharing this one — the spec uses cb_events.md exclusively for CB
transitions, and the asymmetric ownership (circuit_breakers owns
ENTRY into halted states, recovery owns EXIT) is mirrored here.


WHAT THIS DOES IN v0.1.0

Empty file with column header. Real CB events appear once IRA PM
integration lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from ipms.events import CBEvent
from ipms.output.formatting import (
    fmt_date,
    fmt_dollars,
    fmt_optional,
    fmt_pct,
)
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


def write_cb_events(result: SimulationResult, path: Path) -> None:
    """
    Write cb_events.md. One row per CBEvent. State transition values
    rendered as `from_state→to_state` for compact scannability.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_table(f, result)
        _write_footer(f)


def _write_header(f: TextIO, result: SimulationResult) -> None:
    p = result.params
    md = result.metadata
    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# cb_events.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")
    f.write(
        "Circuit breaker state transition log. One row per CB1/CB2 state\n"
        "change. CB transitions follow the uniform pattern:\n"
        "inactive → pending_trigger → active → pending_recovery → inactive.\n"
        "Each transition requires 2 consecutive weekly observations to\n"
        "confirm; pending states that don't confirm decay back to inactive.\n\n"
    )


def _columns() -> list[str]:
    return [
        "date",
        "cb_id",
        "transition",
        "rolling_return",
        "trigger_portfolio_value",
        "confirmation_week_count",
    ]


def _write_table(f: TextIO, result: SimulationResult) -> None:
    cb_events = result.cb_events
    if not cb_events:
        f.write(
            "(No circuit breaker events during this run. v0.1.0 runs without\n"
            "an IRA PM produce no CB transitions; the schema below is locked\n"
            "in for future runs once the IRA PM is integrated.)\n\n"
        )
        write_md_table(f, _columns(), [])
        return

    rows = [_row(e) for e in cb_events]
    write_md_table(f, _columns(), rows)


def _row(e: CBEvent) -> list[str]:
    return [
        fmt_date(e.timestamp),
        e.cb_id.value,
        f"{e.from_state}→{e.to_state}",
        fmt_optional(e.rolling_return, fmt_pct),
        fmt_optional(e.trigger_portfolio_value, fmt_dollars),
        str(e.confirmation_week_count),
    ]


def _write_footer(f: TextIO) -> None:
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- `cb_id`: `cb1` (halts rebalancing at -10% on growth synthetic\n"
        "  6-month rolling) or `cb2` (halts withdrawals at -20%, engages\n"
        "  cascade tier 1).\n"
        "- `transition` is `from_state→to_state`. A clean activation is\n"
        "  `inactive→pending_trigger` (week 1 below threshold) followed\n"
        "  next week by `pending_trigger→active` (week 2 confirms). A\n"
        "  whipsaw (week 1 below, week 2 above) shows as\n"
        "  `pending_trigger→inactive` with no `→active` row in between.\n"
        "- `rolling_return` is the trigger signal value at the moment of\n"
        "  the transition — fractional, e.g. `-0.1023` = -10.23%. Per the\n"
        "  2026-05-09 calibration, this is the equal-weight FBGRX+AVUV\n"
        "  6-month rolling return.\n"
        "- `trigger_portfolio_value` is non-blank only on the `→active`\n"
        "  transition, recording the portfolio value at the moment CB\n"
        "  fired. Used for post-incident analysis and (in IPMV1) for the\n"
        "  trigger-point-delta recovery rule that was retired in 1.5.0.\n"
        "- `confirmation_week_count` tracks the 2-week confirmation\n"
        "  buffer; resets to 0 on every state change.\n"
    )
