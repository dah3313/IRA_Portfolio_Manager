"""
IRAPM Ruleset Configuration — Pydantic model.

This module defines the typed Python representation of the operator's
configuration file `ruleset.yaml` (§2.3, §2.3.1, §2.4). It is the
single point at which all financially-meaningful parameters enter the
running system; magic numbers anywhere else in the codebase are spec
violations.

Design properties (per §2.4 "Configuration loader and validator" as a
named subsystem):

- **Fail-loud at load time.** Every structural invariant is checked
  during model construction. An invalid `ruleset.yaml` prevents system
  startup; the system never silently falls back to defaults
  (consistent with I9 / §3.14 for the operating state file).

- **No magic numbers in callers.** Every consumer reads typed fields
  off this model. There is no `ruleset["inflation_rate"]`
  dict-lookup pattern in the runtime code — that pattern is reserved
  for the loader itself.

- **Strict schema.** `extra="forbid"` means an operator typo in
  `ruleset.yaml` (e.g., `phaze1_inflation_rate: 0.03`) is a startup
  failure, not a silent ignore.

- **Decimal discipline.** All rate-like and dollar-amount fields are
  stored as `Decimal`, never `float` (§2.8). YAML's native parse
  produces `float` for `0.03`-style values; the `mode="before"`
  validators route through `Decimal(str(v))` to avoid float-to-Decimal
  representation artifacts (e.g., `0.1` → `Decimal('0.1000000…')`).

- **Structural validation only.** Phase-specific allowlist policy
  (e.g., "Phase 1 weights must contain exactly PYLD/JPIE/FBCG/AVUV")
  is enforced at use-site, not here. The Ruleset model enforces that
  weights sum to 1.0 and use only allowlist symbols; it does not
  enforce which subset is appropriate for which phase.

The schema is the source of truth; the spec §2.3.1 parameter table is
the descriptive reference. If the two diverge, fix this model to
match the spec, not the other way around — and update DECISIONS.md.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# =============================================================================
# Constants used in validation
# =============================================================================

# Union of all allowlist symbols across all phases (§2.2). Phase-specific
# allowlists are enforced at use-site; here we only reject symbols that
# are not in any phase's allowlist.
_ALLOWLIST_UNION: frozenset[str] = frozenset({"FBCG", "AVUV", "PYLD", "JPIE", "GBIL"})

# Tolerance for "weights sum to 1.0" check. 0.001 = 0.1% of total,
# generous enough to accept reasonable rounding without admitting
# meaningful misallocation.
_WEIGHT_SUM_TOLERANCE: Decimal = Decimal("0.001")

# Regex patterns for time-of-day and weekday-time strings.
# Format: "HH:MM ET" — 24-hour time with explicit timezone abbreviation.
_TIME_ET_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d) ET$")

# Format: "<Weekday> HH:MM ET" — three-letter weekday plus time.
_WEEKDAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
_CYCLE_SCHEDULE_PATTERN = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([01]\d|2[0-3]):([0-5]\d) ET$"
)

# Format: "MM-DD" — month-day for recurring annual dates.
_MMDD_PATTERN = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

# Format: "YYYY-MM-DD" — full ISO date for one-time calendar events.
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


# =============================================================================
# Nested model: target weights
# =============================================================================

class TargetWeights(BaseModel):
    """A target-weight dict for one phase or sub-state. Keys are symbol
    strings restricted to the allowlist union; values are Decimal in
    [0, 1] summing to 1.0 (± tolerance).

    Phase-specific allowlist policy (which symbols are valid in which
    phase) is enforced at use-site, not here.
    """

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, Decimal]

    @field_validator("weights", mode="before")
    @classmethod
    def parse_decimals(cls, v):
        """Coerce values to Decimal via str() to avoid float artifacts."""
        if not isinstance(v, dict):
            raise ValueError(f"target weights must be a dict, got {type(v).__name__}")
        out: dict[str, Decimal] = {}
        for symbol, weight in v.items():
            if isinstance(weight, Decimal):
                out[symbol] = weight
            else:
                out[symbol] = Decimal(str(weight))
        return out

    @field_validator("weights")
    @classmethod
    def validate_symbols_and_range(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        """Each symbol is in the allowlist union; each value in [0, 1]."""
        if not v:
            raise ValueError("target weights dict cannot be empty")
        for symbol, weight in v.items():
            if symbol not in _ALLOWLIST_UNION:
                raise ValueError(
                    f"symbol {symbol!r} not in allowlist union "
                    f"{sorted(_ALLOWLIST_UNION)}"
                )
            if weight < Decimal("0") or weight > Decimal("1"):
                raise ValueError(
                    f"weight for {symbol} = {weight} out of range [0, 1]"
                )
        return v

    @model_validator(mode="after")
    def validate_sum(self) -> "TargetWeights":
        """Weights must sum to 1.0 ± _WEIGHT_SUM_TOLERANCE."""
        total = sum(self.weights.values(), Decimal("0"))
        if abs(total - Decimal("1")) > _WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"target weights sum to {total}, expected 1.0 "
                f"(tolerance ±{_WEIGHT_SUM_TOLERANCE})"
            )
        return self

    @classmethod
    def from_yaml_dict(cls, d: dict) -> "TargetWeights":
        """Construct from a raw YAML dict like {'FBCG': 0.25, 'AVUV': 0.25, ...}.

        The YAML loader gives us a dict at the position where a
        TargetWeights would go; this helper wraps it into the
        single-field model.
        """
        return cls(weights=d)


# =============================================================================
# Root model: Ruleset
# =============================================================================

class Ruleset(BaseModel):
    """The operator's tunable-parameter config, as loaded from
    `ruleset.yaml`. Single source of truth for all financially-
    meaningful constants (§2.3).

    SIZE: 65+ fields, mirroring the flat layout of `ruleset.yaml`
    (D-RY-1). The flat structure is intentional — nested models
    add attribute-access depth without adding type safety.
    """

    model_config = ConfigDict(extra="forbid")

    # -------------------------------------------------------------------------
    # §1. Allocation and rebalancing (5/25)
    # -------------------------------------------------------------------------
    rebalance_absolute_threshold_rate: Decimal
    rebalance_relative_threshold_rate: Decimal
    confirmation_window_weeks: int = Field(gt=0)
    position_residual_minimum_dollars: Decimal

    # -------------------------------------------------------------------------
    # §0. Global inflation rate
    # -------------------------------------------------------------------------
    inflation_rate: Decimal

    # -------------------------------------------------------------------------
    # §2. Circuit breakers (§3.10, §6.3)
    # -------------------------------------------------------------------------
    # Signal-based thresholds and hysteresis
    cb1_threshold_rate: Decimal
    cb2_threshold_rate: Decimal
    cb1_recovery_buffer_rate: Decimal
    cb2_recovery_buffer_rate: Decimal
    cb1_to_cb2_timer_days: int = Field(gt=0)
    # CB2 resource-trigger thresholds and recovery hysteresis (v1.6 — see
    # DECISIONS.md D-SPEC-6). Portfolio-low: dollar floor combined with a
    # months-of-withdrawal multiplier via max(). FI-low: months-of-
    # withdrawal threshold only. Both have an independent recovery buffer
    # rate (default 10%) — CB2 exit on a resource path requires the value
    # to clear `threshold × (1 + recovery_buffer_rate)` for the
    # confirmation window.
    portfolio_low_threshold_dollars: Decimal
    portfolio_low_threshold_months: int = Field(gt=0)
    portfolio_low_recovery_buffer_rate: Decimal
    fi_low_threshold_months: int = Field(gt=0)
    fi_low_recovery_buffer_rate: Decimal

    # -------------------------------------------------------------------------
    # §3. Synthetic Growth Lookback signal (§5)
    # -------------------------------------------------------------------------
    lookback_window_weeks: int = Field(gt=0)
    lookback_max_staleness_days: int = Field(gt=0)
    lookback_min_bar_coverage_rate: Decimal

    # -------------------------------------------------------------------------
    # §4. Phase 1 — Income Production (§4.1)
    # -------------------------------------------------------------------------
    phase1_initial_monthly_dollars: Decimal
    phase1_trigger_year: int = Field(ge=2020, le=2100)
    phase1_to_phase2_transition_date: str

    # -------------------------------------------------------------------------
    # §5. Phase 2 — Pure Growth (§4.1, §7.5.2)
    # -------------------------------------------------------------------------
    phase2_opportunistic_trigger_rate: Decimal
    phase2_opportunistic_recovery_rate: Decimal
    phase2_reallocation_dates: list[str]

    # -------------------------------------------------------------------------
    # §6. Phase 3 — Survivor Income (§4.1, §4.1.1)
    # -------------------------------------------------------------------------
    # I_0 calc inputs (one-time at latch): deliberately conservative
    # assumptions about return and inflation produce a sustainable I_0.
    phase3_i0_calc_return_assumption: Decimal
    phase3_i0_calc_inflation_assumption: Decimal
    phase3_i0_calc_horizon_years: int = Field(gt=0)
    # Per-cycle safety clamp: monthly payment ceiling as fraction of
    # current portfolio value.
    phase3_monthly_payment_ceiling_rate: Decimal

    # -------------------------------------------------------------------------
    # §7. Target allocation weights (§3.5, §4.1, §7.2)
    # -------------------------------------------------------------------------
    phase1_target_weights: TargetWeights
    phase2_steady_target_weights: TargetWeights
    phase2_deployed_target_weights: TargetWeights
    phase3_target_weights: TargetWeights

    # -------------------------------------------------------------------------
    # §8. SGOV buffer (§3.6, §7.4)
    # -------------------------------------------------------------------------
    sgov_buffer_target_months: int = Field(gt=0)
    sgov_refill_post_recovery_delay_days: int = Field(gt=0)

    # -------------------------------------------------------------------------
    # §9. Cash buffer (§3.7, §7.7)
    # -------------------------------------------------------------------------
    cash_buffer_offset_dollars: Decimal
    cash_buffer_tolerance_dollars: Decimal

    # -------------------------------------------------------------------------
    # §10. Large cash deployment (§7.7.1)
    # -------------------------------------------------------------------------
    large_cash_deployment_threshold_dollars: Decimal
    large_cash_deployment_threshold_rate: Decimal

    # -------------------------------------------------------------------------
    # §11. Annual review (§7.6)
    # -------------------------------------------------------------------------
    annual_review_date: str
    freeze_evaluation_threshold_days: int = Field(gt=0)

    # -------------------------------------------------------------------------
    # §12. Misc alert / order / ACH parameters
    # -------------------------------------------------------------------------
    fi_overweight_suppression_alert_weeks: int = Field(gt=0)
    order_fill_timeout_seconds: int = Field(gt=0)
    ach_update_warning_threshold_cycles: int = Field(gt=0)

    # -------------------------------------------------------------------------
    # §13. Hardware token configuration (§10.8)
    # -------------------------------------------------------------------------
    phase3_grace_window_hours: int = Field(gt=0)
    phase3_token_count_required: int = Field(gt=0)
    stopincome_token_count_required: int = Field(gt=0)
    stopincome_stuck_alert_months: int = Field(gt=0)
    stopincome_stuck_realert_months: int = Field(gt=0)
    token_mismatch_critical_cycles: int = Field(gt=0)

    # -------------------------------------------------------------------------
    # §14. Master/slave coordination (§9.4.2)
    # -------------------------------------------------------------------------
    master_heartbeat_time: str
    rsync_replication_time: str
    slave_check_time: str
    slave_healthy_threshold_hours: int = Field(gt=0)
    slave_wake_staleness_hours: int = Field(gt=0)
    slave_promotion_grace_hours: int = Field(gt=0)

    # -------------------------------------------------------------------------
    # §15. Operational pause framework (§11.3)
    # -------------------------------------------------------------------------
    pause_auto_resume_hours: int = Field(gt=0)
    pause_consecutive_escalation_count: int = Field(gt=0)

    # -------------------------------------------------------------------------
    # §16. Operator-configured operational parameters
    # -------------------------------------------------------------------------
    ach_destination: str
    dry_run: bool

    # -------------------------------------------------------------------------
    # §17. Scheduler (§9.6)
    # -------------------------------------------------------------------------
    cycle_schedule: str

    # =========================================================================
    # Decimal coercion for every Decimal-typed field. Pydantic v2's native
    # Decimal coercion accepts floats but introduces representation artifacts
    # (0.1 → Decimal('0.10000000000000000555…')). The mode="before" validator
    # routes through str() to get clean decimal-string round-trip.
    # =========================================================================

    @field_validator(
        "inflation_rate",
        "rebalance_absolute_threshold_rate",
        "rebalance_relative_threshold_rate",
        "position_residual_minimum_dollars",
        "cb1_threshold_rate",
        "cb2_threshold_rate",
        "cb1_recovery_buffer_rate",
        "cb2_recovery_buffer_rate",
        "portfolio_low_threshold_dollars",
        "portfolio_low_recovery_buffer_rate",
        "fi_low_recovery_buffer_rate",
        "lookback_min_bar_coverage_rate",
        "phase1_initial_monthly_dollars",
        "phase2_opportunistic_trigger_rate",
        "phase2_opportunistic_recovery_rate",
        "phase3_i0_calc_return_assumption",
        "phase3_i0_calc_inflation_assumption",
        "phase3_monthly_payment_ceiling_rate",
        "cash_buffer_offset_dollars",
        "cash_buffer_tolerance_dollars",
        "large_cash_deployment_threshold_dollars",
        "large_cash_deployment_threshold_rate",
        mode="before",
    )
    @classmethod
    def parse_decimal(cls, v):
        """Route Decimal coercion through str() to avoid float artifacts."""
        if v is None:
            return v
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    # =========================================================================
    # Coerce TargetWeights from raw YAML dicts. PyYAML gives us a plain dict
    # at each weights position; we wrap into the TargetWeights model.
    # =========================================================================

    @field_validator(
        "phase1_target_weights",
        "phase2_steady_target_weights",
        "phase2_deployed_target_weights",
        "phase3_target_weights",
        mode="before",
    )
    @classmethod
    def wrap_target_weights(cls, v):
        """Accept a raw dict and wrap into TargetWeights for validation."""
        if isinstance(v, TargetWeights):
            return v
        if isinstance(v, dict):
            # If already in {'weights': {...}} form, pass through.
            # Otherwise wrap a bare symbol→weight dict.
            if set(v.keys()) == {"weights"}:
                return v
            return {"weights": v}
        raise ValueError(
            f"target weights must be a dict, got {type(v).__name__}"
        )

    # =========================================================================
    # Validator philosophy: minimal per-field invariants.
    #
    # The operator authors ruleset.yaml; misconfigurations slip past
    # validators rarely, and when they do, they manifest in obvious ways
    # within one cycle (visibly-wrong math, no-op behavior) and the §11.3
    # operational pause framework catches the resulting failures at the
    # broker boundary. Defense in depth: type-level (Field(gt=0) on int
    # durations), parse_decimal (float-artifact defense for Decimal
    # fields), extra="forbid" (typo defense), and the §13.5 invariant
    # test suite at deployment time.
    #
    # Per-field invariants present in this class:
    #   - extra="forbid" (in model_config) — typo defense
    #   - Field(gt=0) on int durations (in declarations) — type-level
    #   - parse_decimal (above) — float-artifact defense for Decimal fields
    #   - ach_destination_non_empty — operator-deployment gotcha
    #   - Format validators for dates/times/cycle_schedule strings — catch
    #     "looks like a string but won't parse" failures at config load
    #     rather than at first scheduled-event time
    #   - TargetWeights weights-sum-to-1.0 (in TargetWeights model) —
    #     real arithmetic invariant
    #
    # The full design rationale for this validator scope lives in
    # DECISIONS.md (D-CODE-2).
    # =========================================================================

    @field_validator("ach_destination")
    @classmethod
    def ach_destination_non_empty(cls, v: str) -> str:
        """ACH destination must be set at deployment (D-RY-8). Empty string
        is the canonical 'not yet configured' value and prevents startup."""
        if not v or not v.strip():
            raise ValueError(
                "ach_destination is empty — operator must configure their "
                "IBKR-side bank reference at deployment time"
            )
        return v

    @field_validator("phase1_to_phase2_transition_date")
    @classmethod
    def iso_date_format(cls, v: str) -> str:
        """Must be YYYY-MM-DD and parse as a real date."""
        if not _ISO_DATE_PATTERN.match(v):
            raise ValueError(
                f"date {v!r} does not match YYYY-MM-DD format"
            )
        try:
            year, month, day = (int(x) for x in v.split("-"))
            date(year, month, day)
        except ValueError as e:
            raise ValueError(f"date {v!r} is not a valid calendar date: {e}")
        return v

    @field_validator("annual_review_date")
    @classmethod
    def mmdd_format(cls, v: str) -> str:
        """Annual review date is MM-DD; year-agnostic recurring."""
        if not _MMDD_PATTERN.match(v):
            raise ValueError(
                f"annual_review_date {v!r} does not match MM-DD format"
            )
        # Verify the month-day pair is a valid calendar date in a non-leap
        # year (so Feb 29 is rejected — it's not a reliable recurring date).
        try:
            month, day = (int(x) for x in v.split("-"))
            date(2025, month, day)  # non-leap reference year
        except ValueError as e:
            raise ValueError(
                f"annual_review_date {v!r} is not a valid month-day "
                f"(rejecting Feb 29 as unreliable): {e}"
            )
        return v

    @field_validator("phase2_reallocation_dates")
    @classmethod
    def reallocation_dates_valid(cls, v: list[str]) -> list[str]:
        """Each entry must be MM-DD; list non-empty."""
        if not v:
            raise ValueError("phase2_reallocation_dates cannot be empty")
        for entry in v:
            if not _MMDD_PATTERN.match(entry):
                raise ValueError(
                    f"reallocation date {entry!r} does not match MM-DD format"
                )
            try:
                month, day = (int(x) for x in entry.split("-"))
                date(2025, month, day)
            except ValueError as e:
                raise ValueError(
                    f"reallocation date {entry!r} is not a valid month-day: {e}"
                )
        return v

    @field_validator("master_heartbeat_time",
                     "rsync_replication_time",
                     "slave_check_time")
    @classmethod
    def time_et_format(cls, v: str) -> str:
        """Must be 'HH:MM ET' with valid 24-hour time."""
        if not _TIME_ET_PATTERN.match(v):
            raise ValueError(
                f"time {v!r} does not match 'HH:MM ET' format "
                f"(24-hour, US Eastern)"
            )
        return v

    @field_validator("cycle_schedule")
    @classmethod
    def cycle_schedule_format(cls, v: str) -> str:
        """Must be '<Weekday> HH:MM ET' (e.g., 'Wed 10:00 ET')."""
        if not _CYCLE_SCHEDULE_PATTERN.match(v):
            raise ValueError(
                f"cycle_schedule {v!r} does not match '<Weekday> HH:MM ET' "
                f"format (weekday is 3-letter, e.g. 'Wed 10:00 ET')"
            )
        return v

    # =========================================================================
    # Construction from YAML
    # =========================================================================

    @classmethod
    def from_yaml(cls, path: str) -> "Ruleset":
        """Load and validate ruleset.yaml from disk. Raises
        pydantic.ValidationError if the file does not conform to the schema.

        Per I9 (§3.14) and §2.3: an invalid ruleset prevents system startup;
        the system never silently falls back to defaults. Callers should let
        the ValidationError propagate to the startup handler, which alerts
        and aborts.
        """
        import yaml
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"ruleset YAML root must be a mapping, got "
                f"{type(data).__name__}"
            )
        return cls.model_validate(data)
