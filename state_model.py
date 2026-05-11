"""
IRAPM Operating State File — Pydantic model.

This module defines the typed Python representation of the operating state
file (§6.7). It mirrors the canonical JSON Schema at state.schema.json —
the schema is the source of truth; this module is the Python convenience
layer for code that reads, mutates, and writes the state file.

If schema and model ever diverge, fix the model to match the schema, not
the other way around.

Money discipline (§2.8): all dollar amounts are stored and validated as
Decimal-as-string in the file (regex-validated against a decimal pattern),
then parsed into `decimal.Decimal` at load time. Float arithmetic on
dollar amounts is a spec violation. Same convention for percentages where
precision matters (the lookback signal value, CPI rate).

Timestamps: ISO 8601 with timezone, parsed into timezone-aware
`datetime.datetime` objects at load time. Per §9.4.2, the master/slave
coordination timing requires unambiguous UTC timestamps.

Atomic write: callers are responsible for atomic-write semantics
(§9.4.1) — write to a temp file, fsync, rename. This module produces the
JSON-serializable dict; it does not own the file write mechanism.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    # Forward reference only — avoids importing ruleset_model at module
    # load time (no circular import risk; ruleset_model does not import
    # from state_model). The Ruleset class is referenced as a string
    # annotation in new_initial_state().
    from ruleset_model import Ruleset


# =============================================================================
# Enums
# =============================================================================

class Phase(str, Enum):
    """Current phase. PHASE_3 is a permanent latch."""
    PHASE_1 = "PHASE_1"
    PHASE_2 = "PHASE_2"
    PHASE_3 = "PHASE_3"


class IncomeState(str, Enum):
    """Orthogonal to phase. Controls whether scheduled withdrawals execute."""
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class CBState(str, Enum):
    """Three core CB states. CB1-extended is derived (CB1 + timer threshold)
    and is NOT a separate persisted state."""
    CB_INACTIVE = "CB_INACTIVE"
    CB1 = "CB1"
    CB2 = "CB2"


class LookbackStatus(str, Enum):
    """Status of the most recent Synthetic Growth Lookback computation."""
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class PauseReason(str, Enum):
    """Catalog of pause_reason values per §11.3."""
    PARTIAL_PHASE_TRANSITION = "partial_phase_transition"
    ORDER_REJECTION = "order_rejection"
    DISK_FULL = "disk_full"
    INTERNAL_CONSISTENCY_VIOLATION = "internal_consistency_violation"


# =============================================================================
# Nested models
# =============================================================================

class TokensObserved(BaseModel):
    """Token count observation per box (used in phase3_grace_pending_abort)."""
    model_config = ConfigDict(extra="forbid")

    box_a_count: int = Field(ge=0, le=2)
    box_b_count: int = Field(ge=0, le=2)


class Phase3GracePendingAbort(BaseModel):
    """Candidate abort observation during Phase 3 grace window. Set when a
    token re-insertion is observed; awaits one-cycle persistence
    confirmation (§10.6.2 Pattern A). If re-insertion persists on next
    daily-token cycle, abort commits and grace clears; if absent on
    next cycle, this field clears and grace continues."""
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    tokens_observed: TokensObserved


class CBPendingTransition(BaseModel):
    """One in-progress CB transition awaiting confirmation. Multiple may
    coexist (e.g., during oscillation near a threshold). Each is
    independently confirmed when its cycles_confirmed reaches
    confirmation_window_weeks."""
    model_config = ConfigDict(extra="forbid")

    from_state: CBState
    to_state: CBState
    cycles_confirmed: int = Field(ge=0)
    first_observed_at: datetime


class CBMachine(BaseModel):
    """Circuit breaker state plus the CB1-active timer and any pending
    transition counters.

    CB1 → CB2 has two trigger paths:
    1. Signal-based: signal ≤ cb2_threshold_rate for confirmation_window_weeks
       consecutive weekly cycles. Tracked via pending_transitions.
    2. Timer-based: cb1_active_timer_started_at is more than
       cb1_to_cb2_timer_days old. Fires immediately when crossed; does
       not use pending_transitions.

    The timer path replaces the prior CB1-extended derived state. There
    is no longer a separate CB1-extended status — long CB1 spans simply
    promote to CB2 directly.
    """
    model_config = ConfigDict(extra="forbid")

    state: CBState
    cb1_active_timer_started_at: Optional[datetime] = None
    pending_transitions: list[CBPendingTransition] = Field(default_factory=list)


class LookbackSignal(BaseModel):
    """Most recent Synthetic Growth Lookback computation. Persisted for
    reporting (weekly summary) and operator inspection only; CB
    transitions use the per-cycle freshly-computed value.

    value_pct is stored as Decimal-as-string for precision (e.g., '-2.34').
    """
    model_config = ConfigDict(extra="forbid")

    status: LookbackStatus
    value_pct: Optional[Decimal] = None
    computed_at: Optional[datetime] = None

    @field_validator("value_pct", mode="before")
    @classmethod
    def parse_decimal(cls, v):
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class ScheduleStateInstance(BaseModel):
    """A schedule_state per §3.13 — represents the inflation-adjusted
    withdrawal schedule for a phase.

    The tuple (i_0, trigger_year, cpi_rate, frozen_years) determines the
    scheduled monthly withdrawal for any given calendar year Y:

        n_raises_applied = count of years in
            range(trigger_year + 1, Y + 1) NOT in frozen_years
        scheduled_monthly = i_0 * (1 + cpi_rate) ** n_raises_applied
    """
    model_config = ConfigDict(extra="forbid")

    i_0_dollars: Decimal
    trigger_year: int = Field(ge=2020, le=2100)
    cpi_rate: Decimal
    frozen_years: list[int] = Field(default_factory=list)

    @field_validator("i_0_dollars", "cpi_rate", mode="before")
    @classmethod
    def parse_decimal(cls, v):
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    @field_validator("frozen_years")
    @classmethod
    def years_unique(cls, v: list[int]) -> list[int]:
        if len(v) != len(set(v)):
            raise ValueError("frozen_years must contain unique values")
        return v


class ScheduleState(BaseModel):
    """Per-phase schedule_state container. Phase 1 instance exists from
    program start; Phase 3 instance is null until Phase 3 triggers.
    Phase 2 has no scheduled withdrawals so no schedule_state."""
    model_config = ConfigDict(extra="forbid")

    phase1: Optional[ScheduleStateInstance] = None
    phase3: Optional[ScheduleStateInstance] = None


class BufferState(BaseModel):
    """Recomputed annual values for SGOV and cash buffers. Updated at
    phase transitions and annual reviews. Code reads from here rather
    than recomputing each cycle."""
    model_config = ConfigDict(extra="forbid")

    sgov_target_dollars: Decimal
    monthly_refill_rate_dollars: Decimal
    cash_target_dollars: Decimal
    recomputed_at: datetime
    refill_delay_started_at: Optional[datetime] = None
    last_refill_at: Optional[datetime] = None

    @field_validator(
        "sgov_target_dollars",
        "monthly_refill_rate_dollars",
        "cash_target_dollars",
        mode="before",
    )
    @classmethod
    def parse_decimal(cls, v):
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class OperationalPause(BaseModel):
    """Auto-resuming pause framework (§11.3). When paused == true, cycles
    still run but abort after input refresh without entering decision/
    action layers. Auto-resumes after pause_auto_resume_hours.

    consecutive_pause_count increments when the same pause_reason fires
    on consecutive pauses. At >= pause_consecutive_escalation_count,
    alert severity escalates from Notice to Warning.
    """
    model_config = ConfigDict(extra="forbid")

    paused: bool
    pause_reason: Optional[PauseReason] = None
    pause_started_at: Optional[datetime] = None
    consecutive_pause_count: int = Field(default=0, ge=0)


class Coordination(BaseModel):
    """Master/slave coordination state (§9.4.2). Written by master, read
    by slave. The local `role` (MASTER/SLAVE_SLEEPING/etc.) is per-box
    operational state and is NOT in this struct — it's not rsync'd."""
    model_config = ConfigDict(extra="forbid")

    master_box_id: str
    last_master_write_timestamp: datetime
    master_ipv4_last_octet: int = Field(ge=0, le=255)


# =============================================================================
# Root model
# =============================================================================

class OperatingState(BaseModel):
    """The operating state file root. Atomic-write JSON on master's pSLC
    SSD per §6.7 and §9.4.1; rsync'd to slave's local disk.

    SCOPE: This is current state only. Append-only logs (CB transition
    log, annual review log, cycle log, alert log, coordination log,
    token check log) live in SEPARATE files per Item 31 ruling and §6.7.
    Per the Item 5 / Item 17 ruling, the token observation file is also
    separate (slave-writable, dedicated slave→master rsync).

    SIZE TARGET: <10KB at any moment. The separation of logs from this
    file is what makes the size target sustainable across decades of
    operation.
    """
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    last_cycle_id: UUID
    last_cycle_at: datetime

    phase: Phase
    phase3_grace_window_start: Optional[datetime] = None
    phase3_grace_pending_abort: Optional[Phase3GracePendingAbort] = None

    income_state: IncomeState
    income_state_changed_at: datetime

    cb_machine: CBMachine
    lookback_signal: LookbackSignal
    schedule_state: ScheduleState = Field(default_factory=ScheduleState)
    buffer_state: BufferState
    operational_pause: OperationalPause
    withdrawal_capacity_exhausted: bool = False
    coordination: Coordination


# =============================================================================
# Convenience helpers
# =============================================================================

def load_state(path: str) -> OperatingState:
    """Load and validate the operating state file from disk. Raises
    pydantic.ValidationError if the file does not conform to the schema.

    Per I9 (§3.14): an invalid state file prevents system startup; the
    system never silently falls back to defaults. Callers should let the
    ValidationError propagate to the startup handler, which alerts and
    aborts.
    """
    import json
    with open(path, "r") as f:
        data = json.load(f)
    return OperatingState.model_validate(data)


def save_state(state: OperatingState, path: str) -> None:
    """Write the operating state to disk with atomic-write semantics
    (write to temp, fsync, rename) per §9.4.1.

    This helper produces the JSON bytes; the atomic-write file mechanics
    should be implemented in the persistence layer. Reference signature
    here for clarity on the data flow.
    """
    import json
    import os
    import tempfile

    # Decimal and datetime are not natively JSON-serializable; serialize
    # via Pydantic's model_dump_json which handles both correctly.
    json_text = state.model_dump_json(indent=2)

    # Atomic-write: temp file in same dir, fsync, rename.
    dir_path = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".json.tmp", dir=dir_path)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def new_initial_state(
    ruleset: "Ruleset",
    box_id: str,
    ipv4_last_octet: int,
) -> OperatingState:
    """Construct the initial state for a fresh IRAPM deployment at
    program start. All financial values are hydrated from `ruleset`
    (the validated `ruleset.yaml`) per §2.3's prohibition on magic
    numbers in code.

    The caller obtains the Ruleset via:

        from ruleset_model import Ruleset
        ruleset = Ruleset.from_yaml("ruleset.yaml")

    Any ValidationError on `from_yaml` aborts startup before this
    function is reached (per I9 / §3.14: invalid config prevents
    startup).

    Buffer values are computed from ruleset:
      sgov_target = phase1_initial_monthly_dollars × sgov_buffer_target_months
      refill_rate = sgov_target / 12
      cash_target = phase1_initial_monthly_dollars + cash_buffer_offset_dollars
    """
    from datetime import timezone
    from uuid import uuid4

    now = datetime.now(timezone.utc)

    # All values typed Decimal/int by the Ruleset model — no parsing here.
    i_0 = ruleset.phase1_initial_monthly_dollars
    cpi = ruleset.inflation_rate
    trigger_year = ruleset.phase1_trigger_year
    buffer_months = ruleset.sgov_buffer_target_months
    cash_offset = ruleset.cash_buffer_offset_dollars

    sgov_target = i_0 * buffer_months
    refill_rate = sgov_target / 12
    cash_target = i_0 + cash_offset

    return OperatingState(
        schema_version="1.0",
        last_cycle_id=uuid4(),
        last_cycle_at=now,
        phase=Phase.PHASE_1,
        income_state=IncomeState.ACTIVE,
        income_state_changed_at=now,
        cb_machine=CBMachine(state=CBState.CB_INACTIVE),
        lookback_signal=LookbackSignal(status=LookbackStatus.UNAVAILABLE),
        schedule_state=ScheduleState(
            phase1=ScheduleStateInstance(
                i_0_dollars=i_0,
                trigger_year=trigger_year,
                cpi_rate=cpi,
                frozen_years=[],
            ),
            phase3=None,
        ),
        buffer_state=BufferState(
            sgov_target_dollars=sgov_target,
            monthly_refill_rate_dollars=refill_rate,
            cash_target_dollars=cash_target,
            recomputed_at=now,
        ),
        operational_pause=OperationalPause(paused=False),
        withdrawal_capacity_exhausted=False,
        coordination=Coordination(
            master_box_id=box_id,
            last_master_write_timestamp=now,
            master_ipv4_last_octet=ipv4_last_octet,
        ),
    )
