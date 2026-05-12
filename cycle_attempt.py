"""
IRAPM Cycle Attempt — the deterministic-identity layer underneath the
broker's idempotency contract.

CONCEPT

Every cycle execution begins by creating (or recovering) a
CycleAttempt: a small file that records:

  - cycle_uuid:        a UUID minted on first entry, persisted across
                       restarts. The same uuid identifies all retries
                       of "the same logical cycle".
  - decision_clock:    a captured timestamp (UTC) used by ALL
                       decision-affecting time queries within the
                       cycle. Restart of an existing cycle_uuid reuses
                       the captured value; this is invariant I15.
  - cycle_type:        'weekly' or 'daily-token'.
  - placed_orders:     append-only log of orders this cycle has
                       successfully submitted (mirrors what's at the
                       broker; used for cross-checking against
                       get_recent_activity() and for forensics).

The file lives at a well-known path on the box's local pSLC SSD,
separate from the operating state file (state.json). The operating
state file is the long-lived "what does the system look like"
record; cycle_attempt.json is the short-lived "what is the in-flight
cycle doing" record.

LIFECYCLE

  Start of cycle:
    1. Look for cycle_attempt.json.
    2. If present AND its `is_complete` flag is False, this is a
       RESTART of a previously-interrupted cycle.
         → Load the existing CycleAttempt. Reuse cycle_uuid and
           decision_clock. Re-execute the cycle from the beginning;
           idempotency in the broker layer ensures placed orders are
           rediscovered, not duplicated.
    3. If present AND `is_complete` is True, the prior cycle finished
       cleanly. The file is stale.
         → Generate a fresh CycleAttempt with a new cycle_uuid and a
           freshly-captured decision_clock. Overwrite the file.
    4. If absent, this is a clean fresh cycle.
         → Generate a fresh CycleAttempt; create the file.

  Mid-cycle:
    Every successful place_order() result is appended to
    placed_orders and the file is atomically rewritten. The file
    grows monotonically across the cycle's duration.

  End of cycle (success):
    is_complete is set to True; the file is rewritten. Subsequent
    cycles see the complete file and start fresh.

  End of cycle (failure):
    is_complete remains False. The next cycle attempt picks up the
    same cycle_uuid and re-executes.

WHY NOT IN state.json?

The operating state file is the canonical long-lived state and is
designed for atomic-write-once-per-cycle (§9.4.1). cycle_attempt.json
is written multiple times per cycle (once per placed order) and is
ephemeral — it has no value after the cycle completes. Keeping it
separate:
  - Avoids polluting the long-lived state with per-cycle scratch data
  - Lets the file format evolve independently of the state schema
  - Makes "stale cycle attempt detection" trivial (look for the file)

WHY NOT JUST RELY ON BROKER-SIDE IDEMPOTENCY?

The broker-side idempotency (orderRef lookup in place_order) handles
the "did we already place this order" question. cycle_attempt.json
adds two things the broker can't:
  - Captured decision_clock (broker has no concept of "the time IRAPM
    decided to act") — this is what makes Plan generation deterministic
    across restarts (I8 + I15).
  - Cycle-level continuity across multiple orders (broker knows about
    individual orders but not about "this cycle attempted these N
    orders").

CONCURRENCY

cycle_attempt.json is written only by the MASTER box's running IRAPM
cycle. It is NOT replicated to the slave (unlike state.json). If the
master dies mid-cycle and the slave promotes:
  - Slave's local cycle_attempt.json does not exist (it never ran
    cycles before promotion).
  - Slave creates a fresh CycleAttempt with a new cycle_uuid on its
    first post-promotion cycle.
  - Broker-side idempotency still catches duplicates against the
    dead master's orders via the orderRef + 48h lookback window.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# =============================================================================
# Constants
# =============================================================================

DEFAULT_CYCLE_ATTEMPT_PATH = Path("/var/lib/irapm/cycle_attempt.json")
"""Canonical location of the cycle attempt file. Configurable per
deployment via constructor argument; this default matches the runbook's
filesystem layout assumption.

NOTE: tests and the IPMS simulator MUST override this — the default is
intentionally absolute and root-owned in production deployment. Pass
an explicit path in non-production contexts."""


class CycleType(str, Enum):
    """Which kind of cycle is running. Drives a small number of
    scheduling decisions (e.g., daily-token cycles skip broker
    interaction except for connection-health verification)."""

    WEEKLY = "weekly"
    DAILY_TOKEN = "daily-token"


# =============================================================================
# Placed-order record
# =============================================================================

class PlacedOrderRecord(BaseModel):
    """A successful order placement during this cycle.

    Appended to CycleAttempt.placed_orders after each successful
    place_order() call. Mirrors the broker's record but is owned by
    IRAPM — useful when reconciling against the broker's report (the
    broker is truth, but this gives the cycle layer a parallel record
    for forensics).
    """

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(
        ...,
        description=(
            "The deterministic identifier supplied to place_order(). "
            "Same value carried in the broker's orderRef."
        ),
    )

    broker_order_id: Optional[str] = Field(
        default=None,
        description=(
            "Broker-assigned order ID (for IBKR: integer orderId, "
            "stringified). May be None when the broker doesn't return "
            "an ID synchronously."
        ),
    )

    symbol: str = Field(..., description="Canonical IRAPM symbol.")

    side: str = Field(
        ...,
        description="'BUY' or 'SELL'. String not Enum here to keep the "
        "file format Pydantic-version-portable.",
    )

    quantity: Decimal = Field(..., description="Shares ordered.")

    order_type: str = Field(
        ...,
        description="'MKT' or 'LMT'. String for portability (see side).",
    )

    limit_price: Optional[Decimal] = Field(
        default=None,
        description="LMT orders only; None for MKT.",
    )

    submitted_at: datetime = Field(
        ...,
        description="UTC timestamp the broker accepted the submission.",
    )

    plan_entry_index: int = Field(
        ...,
        description=(
            "Index of this order in the cycle's Plan. Used (with "
            "cycle_uuid) to verify client_order_id was generated from "
            "the expected (cycle_uuid, plan_entry_index, symbol, side) "
            "tuple."
        ),
    )

    idempotent_rediscovery: bool = Field(
        default=False,
        description=(
            "True if place_order() returned an existing order rather "
            "than submitting a new one. The order exists at the broker "
            "but was placed during a previous attempt of this same "
            "cycle_uuid."
        ),
    )

    @field_validator("quantity", "limit_price", mode="before")
    @classmethod
    def _parse_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            return Decimal(v)
        raise TypeError(
            f"PlacedOrderRecord numeric fields must be Decimal/int/str/None, "
            f"got {type(v).__name__}."
        )


# =============================================================================
# CycleAttempt
# =============================================================================

class CycleAttempt(BaseModel):
    """The in-flight cycle's identity and progress record.

    Round-trips through cycle_attempt.json on the local pSLC SSD. The
    file is rewritten atomically (write-temp-fsync-rename) on every
    mutation; see save() below.

    INVARIANT I15 (proposed for spec addition):
      Within a single cycle attempt (uniquely identified by cycle_uuid),
      all decision-affecting time queries return the same captured
      value (`decision_clock`). Restart of an existing cycle_uuid
      reuses the captured value.
    """

    model_config = ConfigDict(extra="forbid")

    cycle_uuid: uuid.UUID = Field(
        ...,
        description=(
            "Unique identifier for this cycle attempt. Persistent "
            "across restarts: once minted at the start of a cycle, "
            "the same value is reused by any subsequent restart of "
            "the same cycle (until it completes successfully and "
            "is_complete becomes True)."
        ),
    )

    decision_clock: datetime = Field(
        ...,
        description=(
            "Captured timestamp (UTC) used for all decision-affecting "
            "time queries within this cycle attempt. Restart of an "
            "existing cycle_uuid reuses this value, NOT the current "
            "wall-clock time, so that Plan generation remains "
            "deterministic (I8 + I15)."
        ),
    )

    cycle_type: CycleType = Field(
        ...,
        description="Type of cycle. See CycleType enum.",
    )

    started_at: datetime = Field(
        ...,
        description=(
            "Wall-clock time the cycle attempt was first created. "
            "Equals decision_clock for the FIRST attempt; differs for "
            "restarts (started_at = original start; decision_clock "
            "remains the original value). Used for diagnostic logging."
        ),
    )

    last_updated_at: datetime = Field(
        ...,
        description=(
            "Wall-clock time of the most recent save() call. Each "
            "appended placed_order or other mutation updates this."
        ),
    )

    is_complete: bool = Field(
        default=False,
        description=(
            "True when the cycle has finished cleanly (no error or "
            "abort). A False value at cycle startup signals 'restart "
            "of a previously-interrupted cycle' to the lifecycle code."
        ),
    )

    placed_orders: list[PlacedOrderRecord] = Field(
        default_factory=list,
        description=(
            "Append-only log of orders successfully submitted to the "
            "broker during this cycle. Each entry is appended after the "
            "broker confirms acceptance (per the post-placement "
            "confirmation rule in the protocol's place_order)."
        ),
    )

    box_id: str = Field(
        ...,
        description=(
            "Identifier of the box that started this cycle attempt. "
            "Read from box-local config. If the master dies mid-cycle "
            "and the slave promotes, the slave starts a FRESH "
            "CycleAttempt (different cycle_uuid, different box_id); "
            "the dead master's cycle_attempt.json is irrelevant to the "
            "slave since the file is not replicated. Broker-side "
            "idempotency catches duplicate orders via orderRef + 48h "
            "lookback."
        ),
    )

    client_id: int = Field(
        ...,
        description=(
            "Broker client ID this cycle is using (e.g., 11 for box-A, "
            "12 for box-B). Mirrors box_id but in the form the broker "
            "uses. Used for forensics: 'this cycle ran on client 11, "
            "so this client-id-stamped order is ours'."
        ),
    )

    # =========================================================================
    # File I/O
    # =========================================================================

    def save(self, path: Path | str = DEFAULT_CYCLE_ATTEMPT_PATH) -> None:
        """Atomically write this CycleAttempt to disk.

        Atomic-write semantics per §9.4.1: write to a temp file in the
        same directory, fsync the temp file, rename. The rename is
        atomic on POSIX filesystems; if the system crashes mid-write,
        the original file is unchanged.

        Updates last_updated_at to wall-clock-now before writing.
        """
        path = Path(path)
        # Refresh last_updated_at before serializing.
        self.last_updated_at = datetime.now(timezone.utc)

        path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize via Pydantic's JSON producer; ensure Decimal becomes
        # quoted-string and datetime becomes ISO 8601 with offset.
        payload = self.model_dump_json(indent=2)

        # Write to a temp file in the same directory, then rename.
        # NamedTemporaryFile with delete=False so the rename can move
        # it; we clean up manually on exception.
        fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path_obj = Path(tmp_path)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # Atomic rename.
            os.replace(tmp_path_obj, path)
        except Exception:
            # Clean up the temp file if rename didn't happen.
            if tmp_path_obj.exists():
                try:
                    tmp_path_obj.unlink()
                except OSError:
                    pass
            raise

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CYCLE_ATTEMPT_PATH) -> Optional["CycleAttempt"]:
        """Load a CycleAttempt from disk, or return None if absent.

        Raises:
          ValueError: file present but malformed (Pydantic validation
            error). This is a "halt and alert operator" condition;
            cycle code should propagate.
        """
        path = Path(path)
        if not path.exists():
            return None
        with path.open("r") as f:
            data = json.load(f)
        return cls.model_validate(data)

    def append_placed_order(
        self,
        record: PlacedOrderRecord,
        path: Path | str = DEFAULT_CYCLE_ATTEMPT_PATH,
    ) -> None:
        """Append a PlacedOrderRecord and persist atomically.

        Convenience: equivalent to `self.placed_orders.append(record);
        self.save(path)`. Keeping it as one method makes the intent
        ('record this order durably') explicit at call sites.
        """
        self.placed_orders.append(record)
        self.save(path)


# =============================================================================
# Lifecycle helpers
# =============================================================================

def begin_cycle(
    *,
    cycle_type: CycleType,
    box_id: str,
    client_id: int,
    now: datetime,
    path: Path | str = DEFAULT_CYCLE_ATTEMPT_PATH,
) -> tuple[CycleAttempt, bool]:
    """Start (or restart) a cycle attempt.

    Returns (CycleAttempt, is_restart). When is_restart is True, the
    caller should treat this as a continuation of a previously-
    interrupted cycle: the broker layer's idempotency will handle any
    duplicate-order detection.

    Logic:
      1. Try to load existing cycle_attempt.json.
      2. If present and is_complete == False:
           → RESTART. Return the loaded CycleAttempt, is_restart=True.
      3. If present and is_complete == True (or absent):
           → FRESH. Generate cycle_uuid, capture decision_clock,
             create CycleAttempt, save, return is_restart=False.

    The `now` parameter is the wall-clock time at cycle entry (from
    the clock seam). For FRESH cycles, decision_clock = now. For
    RESTARTS, decision_clock is whatever the previous attempt
    captured.
    """
    existing = CycleAttempt.load(path)

    if existing is not None and not existing.is_complete:
        # Restart of an interrupted cycle.
        return existing, True

    # Fresh cycle (either no file or the prior file is complete and stale).
    fresh = CycleAttempt(
        cycle_uuid=uuid.uuid4(),
        decision_clock=now,
        cycle_type=cycle_type,
        started_at=now,
        last_updated_at=now,
        is_complete=False,
        placed_orders=[],
        box_id=box_id,
        client_id=client_id,
    )
    fresh.save(path)
    return fresh, False


def complete_cycle(
    attempt: CycleAttempt,
    path: Path | str = DEFAULT_CYCLE_ATTEMPT_PATH,
) -> None:
    """Mark a cycle attempt complete and persist.

    Called at the end of a cycle's successful execution. Subsequent
    begin_cycle() calls will see is_complete=True and start fresh.

    On cycle failure, complete_cycle is NOT called — leaving
    is_complete=False so the next attempt picks up the same cycle_uuid.
    """
    attempt.is_complete = True
    attempt.save(path)


def build_client_order_id(
    *,
    cycle_uuid: uuid.UUID,
    plan_entry_index: int,
    symbol: str,
    side: str,
) -> str:
    """Construct the deterministic client_order_id string.

    Canonical format:
        cycle-{cycle_uuid}-{plan_entry_index}-{symbol}-{side}

    Example:
        cycle-550e8400-e29b-41d4-a716-446655440000-0-SGOV-SELL

    LENGTH CONSTRAINT: IBKR's orderRef field has historically supported
    strings up to 50 chars in some versions and 100 in others. The
    format above produces strings around 60-70 chars depending on
    symbol/side widths. UNCERTAINTY FLAG: verify orderRef length limit
    against the deployed TWS API version before production. If 50 is
    the actual limit, switch to a shortened cycle_uuid form (first 8
    chars + plan_entry_index + symbol + side, with collision risk
    bounded by 'within 48h on this account' which makes 32-bit
    collision space ample).
    """
    return f"cycle-{cycle_uuid}-{plan_entry_index}-{symbol}-{side}"


__all__ = [
    # Constants
    "DEFAULT_CYCLE_ATTEMPT_PATH",
    # Enums
    "CycleType",
    # Models
    "PlacedOrderRecord",
    "CycleAttempt",
    # Lifecycle
    "begin_cycle",
    "complete_cycle",
    "build_client_order_id",
]
