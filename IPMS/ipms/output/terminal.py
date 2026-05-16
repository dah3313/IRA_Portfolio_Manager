"""
terminal.py — terminal-output formatter.

Public exports:
    print_terminal_summary(result, file=None) — write the summary to
        stdout (default) or any text-mode file-like object

See also: IPMS_SPECIFICATION.md §5.8


FORMAT POSTURE

Terminal output is the operator's "did the run succeed, what were the
headline numbers" surface. It is NOT a substitute for the structured
file output (those land in checkpoint 3+); it is a quick-look summary
for interactive runs.

Per spec §5.7, sub-cent suppression rules do NOT apply to terminal
output. Full Decimal precision is preserved here so the operator can
spot odd residuals when investigating a specific anomaly.
"""

from __future__ import annotations

import sys
from typing import TextIO

from ipms.result import SimulationResult


def print_terminal_summary(
    result: SimulationResult, file: TextIO | None = None
) -> None:
    """
    Write the canonical terminal summary for `result` to `file`
    (default stdout).

    Format mirrors spec §5.8 example:

        === SIMULATION COMPLETE ===
        Window:                  2005-04-25 → 2025-04-25 (20 years)
        Starting balance:        $668,500.00
        Ending balance:          $3,896,609.53
        ...
    """
    out = file or sys.stdout

    # Header
    print("=" * 60, file=out)
    print("=== IPMS SIMULATION COMPLETE ===", file=out)
    print("=" * 60, file=out)

    p = result.params
    if p is None:
        print("(no parameters attached to this result)", file=out)
        return

    # Window
    years = p.years_in_window
    print(
        f"Window:                  {p.start_date} → {p.end_date} "
        f"({years} years)",
        file=out,
    )

    # Starting balance — taken directly from params (the value the
    # operator requested), not derived from the first snapshot.
    print(f"Starting balance:        ${p.starting_balance_usd:,}", file=out)

    # Ending balance — taken from the last snapshot's gross_net_liq.
    if result.terminal_aum is not None:
        print(f"Ending balance:          ${result.terminal_aum:,}", file=out)
    else:
        print("Ending balance:          (no snapshots collected)", file=out)

    # Withdrawals
    print(f"Total withdrawn:         ${result.total_withdrawn:,}", file=out)
    print(f"Withdrawal records:      {len(result.withdrawals)}", file=out)
    if result.cascade_exhausted_halts > 0:
        # Family-safety metric. Highlighted because nonzero is
        # operationally significant — months the family received
        # no income.
        print(
            f"Cascade-exhausted halts: {result.cascade_exhausted_halts} "
            f"(FAMILY-SAFETY: nonzero is a regression signal)",
            file=out,
        )

    # CB events
    print(f"CB1 triggers:            {result.cb1_trigger_count}", file=out)
    print(f"CB2 triggers:            {result.cb2_trigger_count}", file=out)
    print(
        f"Cascade tier-3 hits:     {result.cascade_tier3_occurrences}",
        file=out,
    )

    # Drawdown / minimums
    if result.max_drawdown_pct is not None:
        # max_drawdown_pct is negative; format as percentage to 2dp
        # for readability.
        dd_pct = result.max_drawdown_pct * 100
        print(f"Max drawdown:            {dd_pct:.2f}%", file=out)
    if result.min_sgov_buffer is not None:
        print(f"Min SGOV buffer:         ${result.min_sgov_buffer:,}", file=out)

    # Phase 2 transition
    if result.phase_transitions:
        pt = result.phase_transitions[0]
        print(
            f"Phase 2 transition:      YES ({pt.timestamp})",
            file=out,
        )
        if pt.deferred_by_cb:
            print("                         (deferred by active CB)", file=out)
        if pt.forced_after_max_delay:
            print("                         (forced after max delay)", file=out)
    else:
        # Either the run window didn't reach the anniversary, or no
        # IRA PM is wired (v0.1.0 case).
        print("Phase 2 transition:      no", file=out)

    # Cash-flow attribution sanity check. This is the metric that
    # would have caught the IPMV1 SGOV buffer bug — it is highlighted
    # here so the operator sees it on every run.
    discrepancy = result.max_cash_flow_attribution_discrepancy
    print(
        f"Cash-flow attribution:   max discrepancy ${discrepancy}",
        file=out,
    )
    if discrepancy > 0:
        print(
            "                         (NONZERO: investigate — "
            "possible missing cash-flow event)",
            file=out,
        )

    # Run metadata
    md = result.metadata
    if md.run_started_at and md.run_finished_at:
        elapsed = (md.run_finished_at - md.run_started_at).total_seconds()
        print(
            f"Run duration:            {elapsed:.2f}s "
            f"(simulator v{md.simulator_version})",
            file=out,
        )

    # Output files (populated when file formatters land in checkpoint 3+)
    if result.output_dir:
        print(f"Output files:            {result.output_dir}", file=out)
    else:
        print("Output files:            (none — terminal-only run)", file=out)

    # Warnings emitted during the run, if any. Empty list = clean run.
    if md.warnings:
        print(file=out)
        print("Warnings:", file=out)
        for w in md.warnings:
            print(f"  - {w}", file=out)

    print("=" * 60, file=out)
