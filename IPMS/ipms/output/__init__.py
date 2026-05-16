"""
ipms.output — output formatters for SimulationResult.

Public exports (re-exported from submodules):
    print_terminal_summary — concise human-readable run summary
    write_balances_monthly — balances_monthly.md formatter
    write_balances_annual — balances_annual.md formatter
    write_cascade_log — cascade_log.md formatter
    write_withdrawals — withdrawals.md formatter
    write_rebalances — rebalances.md formatter
    write_cb_events — cb_events.md formatter
    write_recovery_events — recovery_events.md formatter
    write_annual_reviews — annual_reviews.md formatter
    write_phase_transition — phase_transition.md formatter
    write_alerts — alerts.md formatter
    create_run_directory — set up output directory for a run
    write_run_readme — directory README.md
    write_parameters_md — exact-params record file
    auto_run_name — deterministic run-name generator

See also: IPMS_SPECIFICATION.md §5
"""

from ipms.output.alerts import write_alerts
from ipms.output.annual_reviews import write_annual_reviews
from ipms.output.balances import (
    write_balances_annual,
    write_balances_monthly,
)
from ipms.output.cascade import write_cascade_log
from ipms.output.cb_events import write_cb_events
from ipms.output.phase_transition import write_phase_transition
from ipms.output.rebalances import write_rebalances
from ipms.output.recovery_events import write_recovery_events
from ipms.output.run_directory import (
    auto_run_name,
    create_run_directory,
    write_parameters_md,
    write_run_readme,
)
from ipms.output.terminal import print_terminal_summary
from ipms.output.withdrawals import write_withdrawals

__all__ = [
    "print_terminal_summary",
    "write_balances_monthly",
    "write_balances_annual",
    "write_cascade_log",
    "write_withdrawals",
    "write_rebalances",
    "write_cb_events",
    "write_recovery_events",
    "write_annual_reviews",
    "write_phase_transition",
    "write_alerts",
    "create_run_directory",
    "write_run_readme",
    "write_parameters_md",
    "auto_run_name",
]
