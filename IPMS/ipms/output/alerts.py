"""
alerts.py — formatter for alerts.md.

Public exports:
    write_alerts(result, path) — produce alerts.md

See also: IPMS_SPECIFICATION.md §5.6 ("alerts.md")


WHY THIS FILE EXISTS

Every alert the IRA PM would have fired in production is captured by
the simulator (recorded, not transmitted — no SMS, no email). The
audit trail lets the operator verify that the IRA PM is firing the
right alerts at the right times.

Critical example: the audit's gap analysis surfaced that the IPMV1
had no `withdrawal_cascade_exhausted` alert for the moment the family
literally goes without monthly income. After the IRA PM repair adds
that alert, alerts.md will record every fire of it during simulator
runs — letting the operator verify the alert reaches operational
code paths in stress scenarios.


CTX_SNAPSHOT RENDERING

The IRA PM's AlertContext carries a small bag of key-value pairs (the
portfolio value at fire time, withdrawal amount, error message, etc.).
Spec §5.6 calls this "ctx-snapshot (key portfolio values at fire time)"
without specifying a render format. We use the same compact-cell
pattern as cascade Section 2 (`key=value; key=value`) so the row stays
on one Markdown line and downstream tools can split on `;` if needed.


WHAT THIS DOES IN v0.1.0

Empty file with column header.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from ipms.events import Alert
from ipms.output.formatting import fmt_date
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


def write_alerts(result: SimulationResult, path: Path) -> None:
    """
    Write alerts.md. One row per Alert event.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_table(f, result)
        _write_footer(f)


def _write_header(f: TextIO, result: SimulationResult) -> None:
    p = result.params
    md = result.metadata
    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# alerts.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")
    f.write(
        "Alert audit trail. Every alert the IRA PM would have fired in\n"
        "production, recorded by the simulator but NOT transmitted (no SMS,\n"
        "no email — those are production-only). Use this file to verify\n"
        "the IRA PM fires the right alerts at the right times during\n"
        "simulated stress scenarios.\n\n"
    )


def _columns() -> list[str]:
    return [
        "date",
        "event_name",
        "severity",
        "channels",
        "ctx_snapshot",
    ]


def _write_table(f: TextIO, result: SimulationResult) -> None:
    alerts = result.alerts
    if not alerts:
        f.write(
            "(No alerts during this run. v0.1.0 runs without an IRA PM\n"
            "produce no alert events; the schema below is locked in for\n"
            "future runs once the IRA PM is integrated.)\n\n"
        )
        write_md_table(f, _columns(), [])
        return

    rows = [_row(a) for a in alerts]
    write_md_table(f, _columns(), rows)


def _row(a: Alert) -> list[str]:
    return [
        fmt_date(a.timestamp),
        a.event_name,
        a.severity,
        ",".join(a.channels),
        _format_ctx(a.ctx_snapshot),
    ]


def _format_ctx(ctx: dict) -> str:
    """
    Render a context dict as compact `key=value; key=value` cell.

    Values are stringified directly; if the IRA PM puts Decimal or date
    objects in ctx, str() produces sensible output. Empty dict renders
    as empty string.
    """
    if not ctx:
        return ""
    return "; ".join(f"{k}={v}" for k, v in ctx.items())


def _write_footer(f: TextIO) -> None:
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- `event_name` is the IRA PM's alert event identifier — e.g.,\n"
        "  `cb1_triggered`, `cb2_triggered`, `withdrawal_buffer_engaged`,\n"
        "  `withdrawal_buffer_empty_using_FI`, `withdrawal_FI_empty_using_growth`,\n"
        "  `withdrawal_cascade_exhausted` (post-audit-repair),\n"
        "  `recovery_stage_1`, `withdrawal_crisis_over_recovering`.\n"
        "- `severity` is the IRA PM's classification: `info`, `warning`,\n"
        "  `critical`. Determines which alerts the heartbeat summarizes vs.\n"
        "  fires immediately.\n"
        "- `channels` lists which channels the alert would have fired on\n"
        "  in production: `sms`, `email`, or both. The simulator does NOT\n"
        "  actually transmit — these are the channels that WOULD have\n"
        "  received the alert.\n"
        "- `ctx_snapshot` is a compact key=value snapshot of the portfolio\n"
        "  state at fire time. Format: `key=value; key=value; ...`. Use\n"
        "  for cross-reference with same-date snapshots in\n"
        "  balances_monthly.md.\n"
    )
