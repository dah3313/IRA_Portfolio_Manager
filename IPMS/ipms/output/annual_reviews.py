"""
annual_reviews.py — formatter for annual_reviews.md.

Public exports:
    write_annual_reviews(result, path) — produce annual_reviews.md

See also: IPMS_SPECIFICATION.md §5.6 ("annual_reviews.md")


WHY THIS FILE EXISTS

The Guyton-Klinger guardrail mechanism (added in IPMV1 ruleset 1.6.0,
carried into IRA PM) adjusts the monthly withdrawal target each
January based on:
  - CPI raise (default 3%) if effective rate within band
  - 5% cut if effective rate exceeds upper guardrail
  - bypassed (no change) if operator opted out

annual_reviews.md gives the operator visibility into each year's
decision. Without this file, "why is my withdrawal $X this year?"
required tracing back through the IPMV1 simulator's prose output for
the right year. With this file, it's one row.


WHAT THIS DOES IN v0.1.0

Empty file with column header. The IRA PM-driven version emits one
row per January review while in Phase 1.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from ipms.events import AnnualReview
from ipms.output.formatting import (
    fmt_date,
    fmt_dollars,
    fmt_optional,
    fmt_pct,
)
from ipms.output.markdown_tables import write_md_table
from ipms.result import SimulationResult


def write_annual_reviews(result: SimulationResult, path: Path) -> None:
    """
    Write annual_reviews.md. One row per AnnualReview event.
    """
    with path.open("w") as f:
        _write_header(f, result)
        _write_table(f, result)
        _write_footer(f)


def _write_header(f: TextIO, result: SimulationResult) -> None:
    p = result.params
    md = result.metadata
    gen_ts = md.run_finished_at.date() if md.run_finished_at else "unknown"

    f.write("# annual_reviews.md\n\n")
    if p:
        run_label = p.run_name or "(auto)"
        f.write(f"Run: `{run_label}`\n")
        f.write(f"Window: {p.start_date} → {p.end_date}\n")
    f.write(f"Generated: {gen_ts}\n\n")
    f.write(
        "Annual withdrawal-target review log. One row per January review\n"
        "while in Phase 1. Phase 2 skips review entirely (no withdrawals).\n"
        "Outcomes: `seeded` (first review captures reference rate),\n"
        "`cpi_raise` (default — target raised by CPI rate),\n"
        "`guardrail_cut` (effective rate exceeded upper band, target cut),\n"
        "`guardrail_bypassed` (over band but operator opted out of cut).\n\n"
    )


def _columns() -> list[str]:
    return [
        "date",
        "outcome",
        "pre_target",
        "post_target",
        "effective_rate",
        "reference_rate",
        "guardrail_band_low",
        "guardrail_band_high",
        "cpi_rate_applied",
    ]


def _write_table(f: TextIO, result: SimulationResult) -> None:
    reviews = result.annual_reviews
    if not reviews:
        f.write(
            "(No annual reviews during this run. v0.1.0 runs without an IRA\n"
            "PM produce no reviews; the schema below is locked in for future\n"
            "runs once the IRA PM is integrated.)\n\n"
        )
        write_md_table(f, _columns(), [])
        return

    rows = [_row(r) for r in reviews]
    write_md_table(f, _columns(), rows)


def _row(r: AnnualReview) -> list[str]:
    return [
        fmt_date(r.timestamp),
        r.outcome,
        fmt_dollars(r.pre_target),
        fmt_dollars(r.post_target),
        fmt_optional(r.effective_rate, fmt_pct),
        fmt_optional(r.reference_rate, fmt_pct),
        fmt_optional(r.guardrail_band_low, fmt_pct),
        fmt_optional(r.guardrail_band_high, fmt_pct),
        fmt_optional(r.cpi_rate_applied, fmt_pct),
    ]


def _write_footer(f: TextIO) -> None:
    f.write("\n---\n\n")
    f.write("## How to read this file\n\n")
    f.write(
        "- `pre_target` / `post_target` are the monthly withdrawal target\n"
        "  before and after the review's adjustment. Difference is the\n"
        "  effective change.\n"
        "- `effective_rate` is the annualized rate the review computed:\n"
        "  12 × current_monthly_target / core_balance. Compared against\n"
        "  the guardrail band to decide outcome.\n"
        "- `reference_rate` is the rate captured at the first-ever review\n"
        "  (`seeded` outcome). Subsequent reviews compare effective_rate\n"
        "  against `reference_rate × 1.20` (upper band) and\n"
        "  `reference_rate × 0.80` (lower band).\n"
        "- `cpi_rate_applied` is non-blank for `cpi_raise` outcomes only;\n"
        "  for guardrail outcomes the CPI raise is replaced by the cut\n"
        "  (or the bypass), so no CPI rate was applied that year.\n"
    )
