"""
run_directory.py — output directory and metadata-file generation.

Public exports:
    create_run_directory(result) — set up the directory, return its Path
    write_run_readme(result, path) — produce README.md
    write_parameters_md(result, path) — produce parameters.md
    auto_run_name(params) — generate a deterministic run name

See also: IPMS_SPECIFICATION.md §5.2 (directory layout), §5.9 (motivation)


DIRECTORY NAMING

Each run produces a directory under params.output_dir. The run name is
either operator-supplied (params.run_name) or auto-generated from a
parameter hash plus a human-readable label.

Auto-generated names take the form:
    <start_date>_<end_date>_<param_hash>

Where param_hash is a short deterministic hash of the parameter object.
This guarantees two distinct parameter sets do not collide on the same
output directory, and two identical runs DO collide (which is what we
want — the second run overwrites the first deterministically).

Files written:
    README.md       — what this directory contains, run metadata
    parameters.md   — exact parameter object that produced this run
    balances_*.md   — written by output/balances.py (separate module)
    (others)        — written by checkpoint 5 formatters
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ipms.params import SimulationParams
from ipms.proxy_map import PROXY_PROVENANCE
from ipms.result import SimulationResult


def create_run_directory(result: SimulationResult) -> Path:
    """
    Create and return the run's output directory under
    params.output_dir. Directory is created if it doesn't exist;
    overwrites contents if it does (deterministic re-runs).

    Returns the directory Path. Caller writes individual files into it.
    """
    if result.params is None:
        raise ValueError("Cannot create run directory without params")

    p = result.params
    run_name = p.run_name or auto_run_name(p)
    run_dir = p.output_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def auto_run_name(params: SimulationParams) -> str:
    """
    Generate a deterministic run name from parameters. Format:
        <start_year>-<end_year>_<6-char-hash>

    Hash is computed over a stable representation of all parameter
    fields that affect run output. Including dates, balance, withdrawal,
    CPI config, guardrail config, threshold overrides, etc. Excluding
    output-only fields (output_dir, run_name, logging_verbosity) since
    those don't affect the simulation result.
    """
    # Build a stable string representation. Field order is fixed here
    # so two equivalent param sets produce identical hashes regardless
    # of how they were constructed.
    parts = [
        str(params.start_date),
        str(params.end_date),
        str(params.starting_balance_usd),
        str(params.initial_monthly_withdrawal_usd),
        params.cpi_mode.value,
        str(params.cpi_fixed_rate),
        str(params.cpi_yearly_overrides) if params.cpi_yearly_overrides else "",
        params.guardrail_mode.value,
        str(params.guardrail_band_pct_override) if params.guardrail_band_pct_override else "",
        params.output_cadence.value,
        params.time_series_detail.value,
        str(params.output_points) if params.output_points else "",
        str(params.phase_2_anniversary_override) if params.phase_2_anniversary_override else "",
        str(params.phase_2_cessation),
        str(params.initial_sgov_buffer_usd) if params.initial_sgov_buffer_usd else "",
        str(params.initial_cash_usd) if params.initial_cash_usd else "",
        str(params.cb1_threshold_override) if params.cb1_threshold_override else "",
        str(params.cb2_threshold_override) if params.cb2_threshold_override else "",
        str(params.recovery_stage1_threshold_override) if params.recovery_stage1_threshold_override else "",
        str(params.recovery_stage2_threshold_override) if params.recovery_stage2_threshold_override else "",
        str(params.refill_rate_pct_per_month_override) if params.refill_rate_pct_per_month_override else "",
        str(params.disable_annual_review),
    ]
    blob = "|".join(parts).encode("utf-8")
    short_hash = hashlib.sha256(blob).hexdigest()[:6]

    return f"{params.start_date.year}-{params.end_date.year}_{short_hash}"


def write_run_readme(result: SimulationResult, path: Path) -> None:
    """
    Write README.md describing the run's output directory.

    Includes:
      - What this directory contains (file inventory)
      - Run window and headline numbers
      - Data provenance (which proxy files were used)
      - Pointer to parameters.md for full reproducibility info
    """
    p = result.params
    md = result.metadata

    with path.open("w") as f:
        f.write("# Simulator Run Output\n\n")
        if p:
            f.write(f"Window: {p.start_date} → {p.end_date}\n")
            f.write(f"Starting balance: ${p.starting_balance_usd}\n")
            f.write(f"Initial monthly withdrawal: ${p.initial_monthly_withdrawal_usd}\n")
        if md.run_finished_at:
            f.write(f"Generated: {md.run_finished_at.date()}\n")
        f.write(f"Simulator version: {md.simulator_version}\n")
        if md.ira_pm_version:
            f.write(f"IRA PM version: {md.ira_pm_version}\n")
        else:
            f.write("IRA PM version: (none — IPMS v0.1.0 runs without IRA PM)\n")
        f.write("\n")

        # File inventory
        f.write("## Files in this directory\n\n")
        f.write("| File | Description |\n")
        f.write("|---|---|\n")
        f.write("| `README.md` | This file |\n")
        f.write("| `parameters.md` | Exact parameter object that produced this run |\n")
        if "balances_monthly" in result.output_files:
            f.write("| `balances_monthly.md` | Per-month portfolio state |\n")
        if "balances_annual" in result.output_files:
            f.write("| `balances_annual.md` | Per-year portfolio state |\n")
        if "cascade_log" in result.output_files:
            f.write("| `cascade_log.md` | Per-tick cascade depth + transition events |\n")
        if "withdrawals" in result.output_files:
            f.write("| `withdrawals.md` | Per-withdrawal log (success, partial, halts) |\n")
        if "rebalances" in result.output_files:
            f.write("| `rebalances.md` | Per-trade log across all rebalance types |\n")
        if "cb_events" in result.output_files:
            f.write("| `cb_events.md` | Circuit breaker state transitions |\n")
        if "recovery_events" in result.output_files:
            f.write("| `recovery_events.md` | Recovery Stage 1 / Stage 2 transitions |\n")
        if "annual_reviews" in result.output_files:
            f.write("| `annual_reviews.md` | January annual review decisions |\n")
        if "phase_transition" in result.output_files:
            f.write("| `phase_transition.md` | Phase 2 transition record |\n")
        if "alerts" in result.output_files:
            f.write("| `alerts.md` | Alert events the IRA PM would have fired |\n")
        f.write("\n")

        # Headline metrics
        f.write("## Headline metrics\n\n")
        if result.terminal_aum is not None:
            f.write(f"- Terminal AUM: ${result.terminal_aum}\n")
        f.write(f"- Total withdrawn: ${result.total_withdrawn}\n")
        f.write(f"- Withdrawal records: {len(result.withdrawals)}\n")
        f.write(f"- CB1 triggers: {result.cb1_trigger_count}\n")
        f.write(f"- CB2 triggers: {result.cb2_trigger_count}\n")
        f.write(f"- Cascade tier-3 occurrences: {result.cascade_tier3_occurrences}\n")
        f.write(f"- Cascade-exhausted halts: {result.cascade_exhausted_halts}\n")
        if result.max_drawdown_pct is not None:
            dd_pct = result.max_drawdown_pct * 100
            f.write(f"- Max drawdown: {dd_pct:.2f}%\n")
        f.write(
            f"- Cash-flow attribution discrepancy (max): "
            f"${result.max_cash_flow_attribution_discrepancy}\n"
        )
        f.write("\n")

        # Data provenance — which proxy files supplied the price history.
        # Lists every symbol used regardless of phase to give the
        # operator a full picture.
        f.write("## Data provenance\n\n")
        f.write(
            "Historical price data sourced from the following proxy "
            "series. See data/README.md for stitching methodology.\n\n"
        )
        for sym, prov in PROXY_PROVENANCE.items():
            f.write(f"- **{sym}**: {prov}\n")
        f.write("\n")

        # Warnings
        if md.warnings:
            f.write("## Warnings emitted during run\n\n")
            for w in md.warnings:
                f.write(f"- {w}\n")
            f.write("\n")


def write_parameters_md(result: SimulationResult, path: Path) -> None:
    """
    Write parameters.md — the exact parameter set that produced this
    run. Operator should be able to take this file, six months from
    now, and reconstruct the run.

    Format is human-readable Markdown rather than YAML so it's
    self-documenting (sections grouped, key values explained where
    nonobvious). For a machine-readable parameter file, the operator
    has params.source_path if loaded from YAML.
    """
    p = result.params
    if p is None:
        with path.open("w") as f:
            f.write("# Parameters\n\n(No parameters attached to result)\n")
        return

    with path.open("w") as f:
        f.write("# Parameters\n\n")
        if p.source_path:
            f.write(f"Loaded from: `{p.source_path}`\n\n")
        else:
            f.write("Constructed programmatically (no source file).\n\n")

        f.write("## Run window\n\n")
        f.write(f"- start_date: {p.start_date}\n")
        f.write(f"- end_date: {p.end_date}\n")
        f.write(f"- years_in_window: {p.years_in_window}\n")
        f.write("\n")

        f.write("## Starting state\n\n")
        f.write(f"- starting_balance_usd: ${p.starting_balance_usd}\n")
        f.write(f"- initial_monthly_withdrawal_usd: ${p.initial_monthly_withdrawal_usd}\n")
        if p.initial_sgov_buffer_usd is not None:
            f.write(f"- initial_sgov_buffer_usd: ${p.initial_sgov_buffer_usd}\n")
        else:
            f.write("- initial_sgov_buffer_usd: (default — IRA PM ruleset target)\n")
        if p.initial_cash_usd is not None:
            f.write(f"- initial_cash_usd: ${p.initial_cash_usd}\n")
        else:
            f.write("- initial_cash_usd: (default — IRA PM Phase 1 cash buffer)\n")
        f.write("\n")

        f.write("## CPI configuration\n\n")
        f.write(f"- cpi_mode: {p.cpi_mode.value}\n")
        if p.cpi_mode.value == "fixed":
            f.write(f"- cpi_fixed_rate: {p.cpi_fixed_rate}\n")
        elif p.cpi_mode.value == "yearly_override":
            f.write(f"- cpi_yearly_overrides: {p.cpi_yearly_overrides}\n")
        f.write("\n")

        f.write("## Guardrail configuration\n\n")
        f.write(f"- guardrail_mode: {p.guardrail_mode.value}\n")
        if p.guardrail_band_pct_override is not None:
            f.write(f"- guardrail_band_pct_override: {p.guardrail_band_pct_override}\n")
        f.write(f"- disable_annual_review: {p.disable_annual_review}\n")
        f.write("\n")

        f.write("## Phase 2 configuration\n\n")
        f.write(f"- phase_2_anniversary: {p.phase_2_anniversary}")
        if p.phase_2_anniversary_override is not None:
            f.write(" (override)\n")
        else:
            f.write(" (auto: start_date + 8 years)\n")
        f.write(f"- phase_2_cessation: {p.phase_2_cessation}\n")
        f.write("\n")

        # Threshold overrides — only show if any are set, to avoid
        # cluttering the file with "default" lines.
        overrides = [
            ("cb1_threshold_override", p.cb1_threshold_override),
            ("cb2_threshold_override", p.cb2_threshold_override),
            (
                "recovery_stage1_threshold_override",
                p.recovery_stage1_threshold_override,
            ),
            (
                "recovery_stage2_threshold_override",
                p.recovery_stage2_threshold_override,
            ),
            (
                "refill_rate_pct_per_month_override",
                p.refill_rate_pct_per_month_override,
            ),
        ]
        active_overrides = [(k, v) for k, v in overrides if v is not None]
        if active_overrides:
            f.write("## IRA PM overrides\n\n")
            for k, v in active_overrides:
                f.write(f"- {k}: {v}\n")
            f.write("\n")

        f.write("## Output configuration\n\n")
        f.write(f"- output_dir: {p.output_dir}\n")
        f.write(f"- run_name: {p.run_name or '(auto-generated)'}\n")
        f.write(f"- output_cadence: {p.output_cadence.value}\n")
        f.write(f"- time_series_detail: {p.time_series_detail.value}\n")
        if p.output_points is not None:
            f.write(f"- output_points: {p.output_points}\n")
        else:
            f.write("- output_points: (none — using cadence)\n")
        f.write(f"- logging_verbosity: {p.logging_verbosity}\n")
