"""
rebalances.py — formatter for rebalances.md.

Public exports:
    write_rebalances(result, path) — produce rebalances.md

See also: IPMS_SPECIFICATION.md §5.4 ("rebalances.md")


WHY THIS FILE EXISTS

The IPMV1 simulator did not capture the BUY leg of SGOV refills (the
core defect from the audit). One row per executed trade is the
formatter posture that would have surfaced that defect immediately:
the operator scanning rebalances.md and seeing only SELL rows for the
SGOV refill trigger would notice the missing BUY counterpart on first
glance.

In the IRA PM, every refill produces both a SELL leg (drawing from
the most-overweight managed asset) AND a BUY leg (purchasing SGOV
shares with the proceeds). Both must appear in rebalances.md.


WHAT THIS DOES IN v0.1.0

Empty file with column header. The IRA PM-driven version will populate
this file with one row per executed trade across all rebalance types.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from ipms.events import RebalanceRecord, TradeLine
from ipms.output.formatting import (
    fmt_date,
    fmt_dollars,
    fmt_price,
    fmt_shares,
)
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


def write_rebalances(result: SimulationResult, path: Path) -> None:
    """
    Write rebalances.md to `path`. One row per individual trade across
    all rebalance records. A single RebalanceRecord with multiple trades
    (e.g., SGOV refill SELL+BUY pair, Phase 2 transition with many
    trades) emits one row per trade, all sharing the same date and
    trigger_type.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_table(f, result)
        _write_footer(f)


def _write_header(f: TextIO, result: SimulationResult) -> None:
    p = result.params
    md = result.metadata
    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# rebalances.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")
    f.write(
        "Per-trade log across all rebalance types: 5/25 band rebalances,\n"
        "annual rebalance fallback, dry-powder deploy/refill (Phase 2),\n"
        "SGOV buffer refill (paired SELL+BUY), and the Phase 2 transition\n"
        "trade list. One row per executed trade; multi-trade rebalances\n"
        "produce multiple rows sharing date and trigger_type.\n\n"
    )


def _columns() -> list[str]:
    return [
        "date",
        "trigger_type",
        "trigger_reason",
        "symbol",
        "action",
        "shares",
        "price",
        "dollar_amount",
        "outcome",
    ]


def _write_table(f: TextIO, result: SimulationResult) -> None:
    rebalances = result.rebalances
    if not rebalances:
        f.write(
            "(No rebalances during this run. v0.1.0 runs without an IRA PM\n"
            "produce no trades; the schema below is locked in for future\n"
            "runs once the IRA PM is integrated.)\n\n"
        )
        write_md_table(f, _columns(), [])
        return

    # Flatten: one row per trade. Order follows the parent record order;
    # within a multi-trade record, trades render in the IRA PM's
    # canonical order (SELLs before BUYs per IRA PM trade-list discipline).
    rows: list[list[str]] = []
    for r in rebalances:
        if not r.trades:
            # Defensive — a record with no trades is pathological but
            # render an empty-trade row so the operator sees the trigger
            # fired without producing trades.
            rows.append([
                fmt_date(r.timestamp),
                r.trigger_type,
                r.trigger_reason or "",
                "",
                "",
                "",
                "",
                "",
                r.outcome,
            ])
            continue
        for t in r.trades:
            rows.append(_trade_row(r, t))

    write_md_table(f, _columns(), rows)


def _trade_row(r: RebalanceRecord, t: TradeLine) -> list[str]:
    return [
        fmt_date(r.timestamp),
        r.trigger_type,
        r.trigger_reason or "",
        t.symbol,
        t.action,
        fmt_shares(t.shares),
        fmt_price(t.price),
        fmt_dollars(t.dollar_amount),
        t.outcome,
    ]


def _write_footer(f: TextIO) -> None:
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- `trigger_type` taxonomy: `5/25` (band-trigger rebalance),\n"
        "  `annual_fallback` (annual rebalance when no 5/25 fired),\n"
        "  `dry_powder_deploy` / `dry_powder_refill` (Phase 2),\n"
        "  `sgov_buffer_refill` (paired SELL of overweight asset + BUY of\n"
        "  SGOV — both legs appear as separate rows), `phase_2_transition`\n"
        "  (one-shot at 8-year anniversary).\n"
        "- For sgov_buffer_refill triggers, every refill MUST produce two\n"
        "  rows: a SELL of the most-overweight managed asset and a BUY of\n"
        "  SGOV. A SELL without a paired BUY is the IPMV1 defect that\n"
        "  motivated this entire audit — if you ever see one in IRA PM\n"
        "  output, that is a regression.\n"
        "- `dollar_amount` is positive for both BUY and SELL (sign comes\n"
        "  from `action`). Use action+amount for cash-flow direction.\n"
        "- `outcome` taxonomy: `filled` (fully executed), `partial`\n"
        "  (size-capped by residual or other floor), `failed` (rejected by\n"
        "  broker — should never happen in simulator runs).\n"
    )
