"""
alert_catalog.py — Stable alert identifiers and severities (§12.6).

PURPOSE:
    A single place where every alert_id the system can emit is named and
    classified by severity. Code references alerts by AlertId; the alerter
    (§9.3) loads the human-facing template text from alert_templates.yaml
    keyed by the alert_id string.

DESIGN:
    - AlertId is an Enum whose values match the keys in
      alert_templates.yaml exactly. A spec violation (an alert_id in code
      with no template entry, or vice versa) is caught at alerter load.
    - Severity is the §11.1 four-level model. The "operator-relevance
      category" (Critical hard-broke / self-healed weird / normal-ops
      notice) is metadata on the Critical class only and lives in
      DEFAULT_SEVERITY's accompanying CRITICAL_CATEGORY map below.
    - Channels are not represented here — every alert dispatches on both
      Email and SMS per D-AT-1.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    """Four severity levels per §11.1."""

    INFO = "INFO"
    NOTICE = "NOTICE"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class CriticalCategory(str, Enum):
    """Operator-relevance category for Critical alerts only (§11.1).

    The three categories communicate what the operator should do with the
    alert, orthogonal to the severity level itself.
    """

    HARD_BROKE = "hard_broke"
    """System genuinely cannot proceed without external action. No
    auto-resume."""

    SELF_HEALED_WEIRD = "self_healed_weird"
    """Something unexpected happened but the system already recovered.
    Alert is for operator awareness, not action."""

    NORMAL_OPS_NOTICE = "normal_ops_notice"
    """Routine state-transition Critical alerts. System is in expected
    state; the alert is informational and unmissable."""


class AlertId(str, Enum):
    """Every alert the system can emit. Values match alert_templates.yaml
    top-level keys exactly (case-sensitive).
    """

    # Phase and transition alerts (§12.6)
    LARGE_REBALANCE = "large_rebalance"
    PHASE3_ACTIVATION = "phase3_activation"
    PHASE3_GRACE_STARTED = "phase3_grace_started"
    PHASE3_GRACE_PENDING_ABORT = "phase3_grace_pending_abort"
    PHASE3_GRACE_ABORTED = "phase3_grace_aborted"

    # Circuit breaker
    CB_TRANSITION = "cb_transition"

    # Phase 2 rebalancing
    PHASE2_OPPORTUNISTIC_DEPLOY = "phase2_opportunistic_deploy"
    PHASE2_OPPORTUNISTIC_RECOVER = "phase2_opportunistic_recover"
    PHASE2_SEMI_ANNUAL_REALLOCATION = "phase2_semi_annual_reallocation"

    # Withdrawals
    WITHDRAWAL_EXECUTED = "withdrawal_executed"
    WITHDRAWAL_FAILED = "withdrawal_failed"
    CASCADE_GROWTH_SOURCE = "cascade_growth_source"
    WITHDRAWAL_CAPACITY_EXHAUSTED = "withdrawal_capacity_exhausted"
    MONTHLY_PAYMENT_CEILING_BOUND = "monthly_payment_ceiling_bound"

    # Cash deployment
    LARGE_CASH_DEPLOYMENT = "large_cash_deployment"

    # Annual review
    ANNUAL_REVIEW_COMPLETED = "annual_review_completed"
    FREEZE_DECISION = "freeze_decision"

    # Tokens
    TOKEN_STATE_CHANGE = "token_state_change"
    TOKEN_INVALID_STATE = "token_invalid_state"
    TOKEN_UNAVAILABLE = "token_unavailable"
    STOPINCOME_STUCK_ALERT = "stopincome_stuck_alert"

    # Operational pause
    PAUSE_INITIATED = "pause_initiated"
    PAUSE_RE_ALERT = "pause_re_alert"
    PAUSE_AUTO_RESUMED = "pause_auto_resumed"
    PAUSE_CONSECUTIVE_ESCALATION = "pause_consecutive_escalation"
    INTERNAL_CONSISTENCY_VIOLATION = "internal_consistency_violation"
    BROKER_INCONSISTENCY = "broker_inconsistency"

    # Non-paused critical (already healed)
    SPLIT_BRAIN_DETECTED = "split_brain_detected"
    EXTERNAL_ACTIVITY_OVERLAP = "external_activity_overlap"
    BROKER_INCONSISTENCY_TRANSIENT = "broker_inconsistency_transient"

    # ACH and broker
    ACH_UPDATE_FAILED = "ach_update_failed"
    ACH_UPDATE_WARNING = "ach_update_warning"
    BROKER_DISCONNECT = "broker_disconnect"

    # Master/slave
    SLAVE_PROMOTION_PENDING = "slave_promotion_pending"
    SLAVE_PROMOTED = "slave_promoted"
    SLAVE_PROMOTION_CANCELLED = "slave_promotion_cancelled"
    SPLIT_BRAIN_RESOLVED = "split_brain_resolved"

    # Data / signal
    DATA_FILE_STALE = "data_file_stale"
    SIGNAL_RECOVERED = "signal_recovered"

    # Config / sanity
    ALERTER_FAILURE_RECOVERED = "alerter_failure_recovered"

    # Mandatory weekly
    WEEKLY_SUMMARY = "weekly_summary"


# Default severities per §12.6 catalog. Overrides at emit time are
# permitted for the small handful of alerts whose severity is condition-
# dependent (token_unavailable escalates per §10.4; rsync warning
# escalates per §11.2.4). The alerter accepts an optional severity arg
# that overrides the default.
DEFAULT_SEVERITY: dict[AlertId, Severity] = {
    # Phase and transition
    AlertId.LARGE_REBALANCE: Severity.NOTICE,
    AlertId.PHASE3_ACTIVATION: Severity.CRITICAL,
    AlertId.PHASE3_GRACE_STARTED: Severity.CRITICAL,
    AlertId.PHASE3_GRACE_PENDING_ABORT: Severity.NOTICE,
    AlertId.PHASE3_GRACE_ABORTED: Severity.INFO,

    # Circuit breaker
    AlertId.CB_TRANSITION: Severity.NOTICE,

    # Phase 2 rebalancing
    AlertId.PHASE2_OPPORTUNISTIC_DEPLOY: Severity.NOTICE,
    AlertId.PHASE2_OPPORTUNISTIC_RECOVER: Severity.NOTICE,
    AlertId.PHASE2_SEMI_ANNUAL_REALLOCATION: Severity.INFO,

    # Withdrawals
    AlertId.WITHDRAWAL_EXECUTED: Severity.INFO,
    AlertId.WITHDRAWAL_FAILED: Severity.CRITICAL,
    AlertId.CASCADE_GROWTH_SOURCE: Severity.CRITICAL,
    AlertId.WITHDRAWAL_CAPACITY_EXHAUSTED: Severity.CRITICAL,
    AlertId.MONTHLY_PAYMENT_CEILING_BOUND: Severity.NOTICE,

    # Cash deployment
    AlertId.LARGE_CASH_DEPLOYMENT: Severity.NOTICE,

    # Annual review
    AlertId.ANNUAL_REVIEW_COMPLETED: Severity.NOTICE,
    AlertId.FREEZE_DECISION: Severity.NOTICE,

    # Tokens
    AlertId.TOKEN_STATE_CHANGE: Severity.NOTICE,
    AlertId.TOKEN_INVALID_STATE: Severity.WARNING,
    AlertId.TOKEN_UNAVAILABLE: Severity.NOTICE,
    AlertId.STOPINCOME_STUCK_ALERT: Severity.NOTICE,

    # Operational pause
    AlertId.PAUSE_INITIATED: Severity.NOTICE,
    AlertId.PAUSE_RE_ALERT: Severity.NOTICE,
    AlertId.PAUSE_AUTO_RESUMED: Severity.NOTICE,
    AlertId.PAUSE_CONSECUTIVE_ESCALATION: Severity.WARNING,
    AlertId.INTERNAL_CONSISTENCY_VIOLATION: Severity.CRITICAL,
    AlertId.BROKER_INCONSISTENCY: Severity.CRITICAL,

    # Non-paused Critical (already healed)
    AlertId.SPLIT_BRAIN_DETECTED: Severity.CRITICAL,
    AlertId.EXTERNAL_ACTIVITY_OVERLAP: Severity.CRITICAL,
    AlertId.BROKER_INCONSISTENCY_TRANSIENT: Severity.CRITICAL,

    # ACH and broker
    AlertId.ACH_UPDATE_FAILED: Severity.NOTICE,
    AlertId.ACH_UPDATE_WARNING: Severity.WARNING,
    AlertId.BROKER_DISCONNECT: Severity.CRITICAL,

    # Master/slave
    AlertId.SLAVE_PROMOTION_PENDING: Severity.CRITICAL,
    AlertId.SLAVE_PROMOTED: Severity.CRITICAL,
    AlertId.SLAVE_PROMOTION_CANCELLED: Severity.INFO,
    AlertId.SPLIT_BRAIN_RESOLVED: Severity.NOTICE,

    # Data / signal
    AlertId.DATA_FILE_STALE: Severity.NOTICE,
    AlertId.SIGNAL_RECOVERED: Severity.INFO,

    # Config / sanity
    AlertId.ALERTER_FAILURE_RECOVERED: Severity.WARNING,

    # Mandatory weekly
    AlertId.WEEKLY_SUMMARY: Severity.INFO,
}


# Operator-relevance category for Critical alerts only (§11.1). Used by
# the alerter to render the right phrasing ("system halted — operator
# action required" vs "system self-healed — for your awareness").
CRITICAL_CATEGORY: dict[AlertId, CriticalCategory] = {
    AlertId.PHASE3_ACTIVATION: CriticalCategory.NORMAL_OPS_NOTICE,
    AlertId.PHASE3_GRACE_STARTED: CriticalCategory.NORMAL_OPS_NOTICE,

    AlertId.WITHDRAWAL_FAILED: CriticalCategory.HARD_BROKE,
    AlertId.CASCADE_GROWTH_SOURCE: CriticalCategory.NORMAL_OPS_NOTICE,
    AlertId.WITHDRAWAL_CAPACITY_EXHAUSTED: CriticalCategory.HARD_BROKE,

    AlertId.INTERNAL_CONSISTENCY_VIOLATION: CriticalCategory.HARD_BROKE,
    AlertId.BROKER_INCONSISTENCY: CriticalCategory.HARD_BROKE,

    AlertId.SPLIT_BRAIN_DETECTED: CriticalCategory.SELF_HEALED_WEIRD,
    AlertId.EXTERNAL_ACTIVITY_OVERLAP: CriticalCategory.SELF_HEALED_WEIRD,
    AlertId.BROKER_INCONSISTENCY_TRANSIENT: CriticalCategory.SELF_HEALED_WEIRD,

    AlertId.BROKER_DISCONNECT: CriticalCategory.SELF_HEALED_WEIRD,

    AlertId.SLAVE_PROMOTION_PENDING: CriticalCategory.NORMAL_OPS_NOTICE,
    AlertId.SLAVE_PROMOTED: CriticalCategory.NORMAL_OPS_NOTICE,
}


__all__ = [
    "AlertId", "Severity", "CriticalCategory",
    "DEFAULT_SEVERITY", "CRITICAL_CATEGORY",
]
