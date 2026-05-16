"""
event_log.py — IRAPM event log writer and readers (EVENT_LOG_SPEC §5, §6).

PURPOSE:
    The event log is IRAPM's system of record. Every operationally
    significant action — cycles, decisions, orders, fills, withdrawals,
    state-machine transitions, snapshots, alerts — is appended to a
    single events.jsonl file under paths.state_dir.

    This module owns both writing AND reading the event log. The
    writer is the only code in IRAPM that writes to events.jsonl.
    Readers (reporters, forensic tools, expectation checkers) are
    expected to use the iter_events* readers below rather than
    re-implementing JSONL parsing.

DESIGN (EVENT_LOG_SPEC §5, §6):
    - Stateless module. No classes, no module-level state beyond
      constants. Each append() opens the file, writes one line, closes.
      Each iter_events* call opens the file, streams it, closes.
    - Append-only JSONL. One JSON object per line, LF terminator,
      compact (no indentation). The atomicity bound for append() on
      POSIX is PIPE_BUF (≥ 512 bytes); all event records are designed
      to fit well under this bound.
    - fsync() on every append. Conservative; ~1-5ms per call. Over a
      20-year baseline (~50K events), well under 5 minutes total.
    - Single-writer assumption. The harness's cycle_attempt lockfile
      guarantees one IRAPM instance per state directory at a time.
      Readers tolerate a trailing partial line per §6.
    - All timestamps tz-aware UTC. Naive datetimes raise ValueError
      synchronously — this forecloses the bug class that motivated
      the 2026-05-13 annual_review tz fix.
    - All Decimals serialized as JSON strings to preserve precision.
    - schema_version = "1.0".

ERROR HANDLING (§5.4):
    Logging failures must never propagate. Catching OSError ensures
    that a disk-full or permission failure does not abort an IRAPM
    cycle. Caught errors are logged at WARNING; the event_id that
    would have been written is still returned so callers don't need
    exception-handling boilerplate around every emission.

    Programmer errors (unknown event_type, naive datetime, payload
    containing values json.dumps cannot serialize) raise ValueError
    or TypeError synchronously. These must be caught in testing.

PUBLIC API:
    append(paths, event_type, payload, *, now, source_cycle_id=None,
           in_sim_timestamp=None) -> str
        Generic appender. Returns the event_id.

    append_cycle_started(paths, *, cycle_id, cycle_type, is_restart,
                         box_id, client_id, now) -> str
    append_cycle_completed(paths, *, ...) -> str
    ... one helper per event type (13 total).
        Per-event-type helpers centralize payload shape construction
        so call sites in cycle.py, action_layer.py, etc. don't build
        payload dicts inline.

    iter_events(paths) -> Iterator[dict]
    iter_events_of_type(paths, event_type) -> Iterator[dict]
    iter_events_in_window(paths, start, end, *, event_type=None)
        -> Iterator[dict]
        Reader patterns per §6. Used by report.py and future Phase 4
        consumers (harness CycleFailureTracker, check_expectations,
        annual_review CB freeze counter).

FILE LOCATION:
    paths.state_dir / "events.jsonl"

    Note this is at state_dir directly, NOT under state_dir/logs/.
    The event log is architecturally distinct from the legacy logs
    in persistence.py: it is the system of record, not a per-subsystem
    diagnostic file.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterator, Optional
from uuid import UUID

from persistence import Paths

logger = logging.getLogger(__name__)


# --- Constants -------------------------------------------------------------

SCHEMA_VERSION = "1.0"

# The complete catalog of event types per EVENT_LOG_SPEC §4. Unknown
# event_type values passed to append() raise ValueError synchronously.
# Adding a new event type to this set requires a corresponding spec
# update per §3.4 (backward-compatible additions are allowed within 1.x).
KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    "cycle_started",
    "cycle_completed",
    "cycle_halted",
    "decision_made",
    "order_placed",
    "fill_received",
    "withdrawal_executed",
    "cb_transition",
    "phase_transition",
    "annual_review_completed",
    "portfolio_snapshot",
    "state_snapshot",
    "alert_emitted",
})


# --- JSON encoder ----------------------------------------------------------

class _EventEncoder(json.JSONEncoder):
    """JSON encoder for event log records.

    Handles the types IRAPM uses that json.dumps does not natively
    serialize:
    - Decimal -> string (preserves precision per §3.5)
    - datetime -> ISO 8601 string (callers should generally pre-convert,
      but tolerating datetime objects in payloads avoids one class of
      caller errors)
    - UUID -> string
    - set -> sorted list (deterministic ordering)

    Any other type falls through to JSONEncoder.default and raises
    TypeError. Per §5.4, that is a programmer error and propagates.
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            _require_aware(obj, "payload datetime")
            return obj.isoformat()
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, set):
            return sorted(obj)
        return super().default(obj)


def _require_aware(dt: datetime, label: str) -> None:
    """Raise ValueError if dt is naive (no tzinfo). Per §3.2."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            f"{label} must be timezone-aware (got naive datetime). "
            "Per EVENT_LOG_SPEC §3.2, every timestamp in the event log "
            "carries timezone information."
        )


def _new_event_id() -> str:
    """Generate a new event_id per §3.3: 'evt_' + uuid4 hex (no dashes)."""
    return "evt_" + uuid.uuid4().hex


# --- The public append() function ------------------------------------------

def append(
    paths: Paths,
    event_type: str,
    payload: dict,
    *,
    now: datetime,
    source_cycle_id: Optional[str] = None,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Append one event to events.jsonl.

    Constructs the standard envelope around the caller-supplied payload,
    serializes to one line of JSON, and appends to
    paths.state_dir / events.jsonl with fsync.

    Args:
        paths: IRAPM Paths object. The events.jsonl path is derived
            via paths.events_log().
        event_type: One of KNOWN_EVENT_TYPES. Unknown types raise
            ValueError before any I/O.
        payload: The event-type-specific payload. Caller is responsible
            for constructing the right shape per EVENT_LOG_SPEC §4.
            The writer does NOT validate payload structure beyond
            verifying it serializes to JSON.
        now: Wall-clock instant of emission. Used as `emitted_at`.
            Must be timezone-aware.
        source_cycle_id: The cycle UUID this event belongs to, as a
            string. None for events emitted outside any cycle.
        in_sim_timestamp: The simulated/operational time the event
            occurred. If None, defaults to `now`. Must be tz-aware
            if provided. In production this equals `now`; in
            simulation this is the in-sim calendar time.

    Returns:
        The generated event_id. Callers that emit linked events can
        capture this and pass it in subsequent payloads. The id is
        returned even when the underlying write fails (per §5.4),
        so callers don't need exception-handling boilerplate.

    Raises:
        ValueError: event_type is not in KNOWN_EVENT_TYPES, OR `now`
            or `in_sim_timestamp` is naive. Programmer errors, caught
            in testing.
        TypeError: payload contains a value the JSON encoder cannot
            handle. Programmer error.

    Does NOT raise:
        OSError from disk-level failures (disk full, permission denied,
        filesystem unmounted). Caught internally and logged at WARNING.
        IRAPM's cycle work must continue even when logging cannot
        proceed; failing to log is regrettable, failing to execute a
        trade because we couldn't log it is unacceptable.
    """
    # --- Validation (synchronous, before any I/O) --------------------------
    if event_type not in KNOWN_EVENT_TYPES:
        raise ValueError(
            f"Unknown event_type {event_type!r}. Known types: "
            f"{sorted(KNOWN_EVENT_TYPES)}"
        )
    _require_aware(now, "now")
    if in_sim_timestamp is not None:
        _require_aware(in_sim_timestamp, "in_sim_timestamp")

    timestamp = in_sim_timestamp if in_sim_timestamp is not None else now
    event_id = _new_event_id()

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": timestamp.isoformat(),
        "emitted_at": now.isoformat(),
        "source_cycle_id": source_cycle_id,
        "payload": payload,
    }

    # Serialize. TypeError propagates per §5.4 (programmer error);
    # ValueError from allow_nan=False (NaN/Inf in payload) also
    # propagates as a programmer error since NaN has no valid
    # round-trip per §5.3.
    line = json.dumps(
        envelope,
        cls=_EventEncoder,
        separators=(",", ":"),
        allow_nan=False,
    )

    # --- I/O (OSError caught) ----------------------------------------------
    try:
        path = paths.events_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append binary mode for POSIX atomic-append semantics (§5.3).
        with open(path, "ab") as f:
            f.write(line.encode("utf-8"))
            f.write(b"\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        logger.warning(
            "event_log append failed for event_type=%s event_id=%s: %s",
            event_type, event_id, exc,
        )

    return event_id


# --- Per-event-type helpers -----------------------------------------------
#
# These centralize payload-shape construction per EVENT_LOG_SPEC §5.5.
# Call sites in cycle.py, action_layer.py, annual_review.py, alerter.py
# invoke these rather than constructing payloads inline. This keeps
# emission sites readable and ensures payload shape consistency.
#
# Helper signatures use keyword-only arguments (after the leading
# positional `paths`) so call sites read like the spec's payload tables.
# `cycle_id` and `now` are required by every helper; other arguments
# match each event type's payload schema in EVENT_LOG_SPEC §4.


def append_cycle_started(
    paths: Paths,
    *,
    cycle_id: str,
    cycle_type: str,
    is_restart: bool,
    box_id: str,
    client_id: int,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a cycle_started event (EVENT_LOG_SPEC §4.1).

    Called at the top of run_weekly_cycle / run_daily_token_cycle,
    immediately after begin_cycle() returns.
    """
    payload = {
        "cycle_type": cycle_type,
        "is_restart": is_restart,
        "box_id": box_id,
        "client_id": client_id,
    }
    return append(
        paths, "cycle_started", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_cycle_completed(
    paths: Paths,
    *,
    cycle_id: str,
    cycle_type: str,
    phase: str,
    cb_state: str,
    income_state: str,
    operational_pause: bool,
    withdrawal_capacity_exhausted: bool,
    lookback_status: Optional[str],
    lookback_value: Optional[Decimal],
    plan_entry_count: int,
    is_scheduled_withdrawal_day: bool,
    is_annual_review_day: bool,
    is_phase2_reallocation_day: bool,
    duration_ms: int,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a cycle_completed event (EVENT_LOG_SPEC §4.2).

    Called at the end of run_weekly_cycle / run_daily_token_cycle,
    after complete_cycle() marks the attempt complete. Absence of
    this event after a cycle_started is itself diagnostic — it means
    the cycle raised before reaching this point.
    """
    payload = {
        "cycle_type": cycle_type,
        "phase": phase,
        "cb_state": cb_state,
        "income_state": income_state,
        "operational_pause": operational_pause,
        "withdrawal_capacity_exhausted": withdrawal_capacity_exhausted,
        "lookback_status": lookback_status,
        "lookback_value": str(lookback_value) if lookback_value is not None else None,
        "plan_entry_count": plan_entry_count,
        "is_scheduled_withdrawal_day": is_scheduled_withdrawal_day,
        "is_annual_review_day": is_annual_review_day,
        "is_phase2_reallocation_day": is_phase2_reallocation_day,
        "duration_ms": duration_ms,
    }
    return append(
        paths, "cycle_completed", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_cycle_halted(
    paths: Paths,
    *,
    cycle_id: str,
    cycle_type: str,
    halt_reason: str,
    phase_at_halt: str,
    cb_state_at_halt: str,
    plan_entry_count: int,
    completed_legs: int,
    failed_leg_index: int,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a cycle_halted event (EVENT_LOG_SPEC §4.3).

    Called from cycle.py when result.halted_due_to_failure is set,
    BEFORE the cycle_completed event for the same cycle (per §4.3,
    a halt is followed by cycle_completed to preserve the started↔
    completed invariant).
    """
    payload = {
        "cycle_type": cycle_type,
        "halt_reason": halt_reason,
        "phase_at_halt": phase_at_halt,
        "cb_state_at_halt": cb_state_at_halt,
        "plan_entry_count": plan_entry_count,
        "completed_legs": completed_legs,
        "failed_leg_index": failed_leg_index,
    }
    return append(
        paths, "cycle_halted", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_decision_made(
    paths: Paths,
    *,
    cycle_id: str,
    inputs: dict,
    plan_entries: list[dict],
    should_skip_action_layer: bool,
    skip_reason: Optional[str],
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a decision_made event (EVENT_LOG_SPEC §4.4).

    Called in run_weekly_cycle immediately after decide() returns and
    before the plan is executed. The load-bearing event for "why did
    IRAPM make this decision" audits.

    The `inputs` dict and `plan_entries` list are passed through
    verbatim — the caller is responsible for constructing the right
    shape per §4.4's sub-tables. This helper does not validate
    structure (matching the writer's general philosophy: payload
    shape is the caller's responsibility, the writer only ensures
    JSON-serializability).
    """
    payload = {
        "inputs": inputs,
        "plan_entries": plan_entries,
        "should_skip_action_layer": should_skip_action_layer,
        "skip_reason": skip_reason,
    }
    return append(
        paths, "decision_made", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_order_placed(
    paths: Paths,
    *,
    cycle_id: str,
    client_order_id: str,
    broker_order_id: str,
    plan_entry_index: int,
    plan_entry_kind: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity_shares: Decimal,
    limit_price_dollars: Optional[Decimal],
    time_in_force: str,
    intended_dollar_amount: Decimal,
    price_refresh_dollars: Optional[Decimal],
    price_refresh_status: str,
    used_fallback_estimate: bool,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit an order_placed event (EVENT_LOG_SPEC §4.5).

    Called in action_layer._place_one_order, immediately after
    broker.place_order() returns successfully. Broker rejections
    that prevent place_order() from returning do NOT produce this
    event — they produce cycle_halted instead.
    """
    payload = {
        "client_order_id": client_order_id,
        "broker_order_id": broker_order_id,
        "plan_entry_index": plan_entry_index,
        "plan_entry_kind": plan_entry_kind,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "quantity_shares": str(quantity_shares),
        "limit_price_dollars": str(limit_price_dollars) if limit_price_dollars is not None else None,
        "time_in_force": time_in_force,
        "intended_dollar_amount": str(intended_dollar_amount),
        "price_refresh_dollars": str(price_refresh_dollars) if price_refresh_dollars is not None else None,
        "price_refresh_status": price_refresh_status,
        "used_fallback_estimate": used_fallback_estimate,
    }
    return append(
        paths, "order_placed", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_fill_received(
    paths: Paths,
    *,
    cycle_id: str,
    client_order_id: str,
    broker_order_id: str,
    fill_id: str,
    plan_entry_index: int,
    plan_entry_kind: str,
    symbol: str,
    side: str,
    quantity_shares: Decimal,
    price_dollars: Decimal,
    fill_dollar_amount: Decimal,
    fill_time: datetime,
    fill_index: int,
    total_fills_for_order: int,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a fill_received event (EVENT_LOG_SPEC §4.6).

    Called in action_layer._place_one_order after the order reaches
    FILLED status. One event per Fill object in OrderResult.fills.
    Load-bearing for cash_in / cash_out aggregation in the reporter.

    `fill_time` is the broker-reported wall-clock time of the fill
    (from Fill.fill_time); must be tz-aware.
    """
    _require_aware(fill_time, "fill_time")
    payload = {
        "client_order_id": client_order_id,
        "broker_order_id": broker_order_id,
        "fill_id": fill_id,
        "plan_entry_index": plan_entry_index,
        "plan_entry_kind": plan_entry_kind,
        "symbol": symbol,
        "side": side,
        "quantity_shares": str(quantity_shares),
        "price_dollars": str(price_dollars),
        "fill_dollar_amount": str(fill_dollar_amount),
        "fill_time": fill_time.isoformat(),
        "fill_index": fill_index,
        "total_fills_for_order": total_fills_for_order,
    }
    return append(
        paths, "fill_received", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_withdrawal_executed(
    paths: Paths,
    *,
    cycle_id: str,
    payload: dict,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a withdrawal_executed event (EVENT_LOG_SPEC §4.7).

    Called in action_layer._execute_withdrawal after every SELL leg
    has reached terminal-filled status. The payload's binding_ceiling
    /scheduled/paid triple fixes the unfilled-alert-template
    misreporting bug.

    NOTE: Because §4.7's payload is complex (cascade source breakdown,
    multi-leg fill summaries, ceiling math), this helper accepts the
    payload dict pre-constructed by the caller rather than enumerating
    every field. The caller is responsible for the shape per §4.7.
    This pattern is also used for annual_review_completed (§4.10),
    phase_transition (§4.9), and the snapshot events.
    """
    return append(
        paths, "withdrawal_executed", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_cb_transition(
    paths: Paths,
    *,
    cycle_id: str,
    from_state: str,
    to_state: str,
    trigger_reason: str,
    cb2_entry_conditions_after: list[str],
    lookback_value_at_trigger: Optional[Decimal] = None,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a cb_transition event (EVENT_LOG_SPEC §4.8).

    Called from action_layer._execute_cb_state_transition. Mirrors
    the existing cb_transitions.jsonl write; both write paths run
    during the migration period (per HANDOFF_2026-05-14, Phase 5
    eventually removes the legacy write).

    `cb2_entry_conditions_after` is the list of CB2 entry-condition
    values active AFTER this transition (per CBStateTransitionEntry).
    `lookback_value_at_trigger` is captured when the transition was
    signal-driven; null for cb1_timer or other non-signal triggers.
    """
    payload = {
        "from_state": from_state,
        "to_state": to_state,
        "trigger_reason": trigger_reason,
        "cb2_entry_conditions_after": cb2_entry_conditions_after,
        "lookback_value_at_trigger": (
            str(lookback_value_at_trigger)
            if lookback_value_at_trigger is not None else None
        ),
    }
    return append(
        paths, "cb_transition", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_phase_transition(
    paths: Paths,
    *,
    cycle_id: str,
    payload: dict,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a phase_transition event (EVENT_LOG_SPEC §4.9).

    Called from action_layer._execute_phase_transition. The full
    Phase 3 I_0 input set is preserved for permanent reconstruction
    of the income-floor math.

    Payload is caller-constructed (complex shape per §4.9, including
    pre/post bucket allocations and I_0 inputs).
    """
    return append(
        paths, "phase_transition", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_annual_review_completed(
    paths: Paths,
    *,
    cycle_id: str,
    payload: dict,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit an annual_review_completed event (EVENT_LOG_SPEC §4.10).

    Called from annual_review.py after the review's withdrawal-recalc
    inputs (CPI, freeze status, new monthly amount, etc.) are settled.

    Payload is caller-constructed per §4.10.
    """
    return append(
        paths, "annual_review_completed", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_portfolio_snapshot(
    paths: Paths,
    *,
    cycle_id: str,
    payload: dict,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a portfolio_snapshot event (EVENT_LOG_SPEC §4.11).

    Called at the end of every weekly cycle, after cycle_completed.
    The primary input for the reporter's per-row balance columns
    (gross_net_liq, cash_buff, sgov_buffer, per-symbol market values,
    fi/growth bucket totals, weights).

    Payload is caller-constructed per §4.11 — the positions sub-object
    enumerates every symbol in state.positions including zeros.
    """
    return append(
        paths, "portfolio_snapshot", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_state_snapshot(
    paths: Paths,
    *,
    cycle_id: str,
    payload: dict,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit a state_snapshot event (EVENT_LOG_SPEC §4.12).

    Called at meaningful state-boundary events plus a monthly
    heartbeat:
    - After every cb_transition
    - After every phase_transition
    - After every annual_review_completed
    - First weekly cycle of each month (heartbeat)

    Payload is caller-constructed per §4.12 — captures the full
    operational state at the snapshot moment.
    """
    return append(
        paths, "state_snapshot", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


def append_alert_emitted(
    paths: Paths,
    *,
    cycle_id: Optional[str],
    alert_id: str,
    context: dict,
    email_ok: bool,
    sms_ok: bool,
    deduped: bool,
    email_error: Optional[str] = None,
    sms_error: Optional[str] = None,
    now: datetime,
    in_sim_timestamp: Optional[datetime] = None,
) -> str:
    """Emit an alert_emitted event (EVENT_LOG_SPEC §4.13).

    Called by the caller of `alerter.dispatch()` after dispatch returns
    a DispatchOutcome. The event records IRAPM's intent to alert plus
    the per-channel success/failure outcome.

    Payload fields map directly from DispatchOutcome:
      - `email_ok` / `sms_ok` — channel success booleans
      - `email_error` / `sms_error` — error strings on failure (null otherwise)
      - `deduped` — true if the alerter suppressed the dispatch as a
        recent duplicate (in which case email_ok/sms_ok are reported
        as true per the existing DedupTracker convention, and the
        original alert was previously delivered).

    `cycle_id` is Optional because alerts can be dispatched outside
    any cycle (e.g., startup health checks, manual operator-triggered
    alerts).
    """
    payload = {
        "alert_id": alert_id,
        "context": context,
        "email_ok": email_ok,
        "email_error": email_error,
        "sms_ok": sms_ok,
        "sms_error": sms_error,
        "deduped": deduped,
    }
    return append(
        paths, "alert_emitted", payload,
        now=now,
        source_cycle_id=cycle_id,
        in_sim_timestamp=in_sim_timestamp,
    )


# ============================================================================
# READERS (EVENT_LOG_SPEC §6)
# ============================================================================
#
# Three reader patterns sufficient for Phase 2 (the reporter) and the bulk
# of Phase 4 (filter-based consumers):
#   - iter_events: stream the entire log
#   - iter_events_of_type: filter by event_type
#   - iter_events_in_window: filter by time window (and optional event_type)
#
# Two additional readers from EVENT_LOG_SPEC §6 (state_at, iter_events_from)
# are deferred until a Phase 4 consumer needs them.
#
# All readers honor the universal invariants from §6.1:
#   1. Tolerate a partial trailing line (skip silently).
#   2. Tolerate records with unknown event_types (skip silently for
#      forward-compat; readers built today must not crash when newer
#      schemas add event types).
#   3. Tolerate records with newer schema_version major numbers (skip
#      with WARNING). Currently only "1.0" exists.
#   4. Never modify the file.
#
# Performance: linear scan of events.jsonl per call. At our scales
# (≤15 MB over 30 years), each call completes well under one second.
# No index, no cache, no persistent state. If profiling later shows
# this matters, a positional index can be added without changing the
# reader API.


def iter_events(paths: Paths) -> Iterator[dict]:
    """Yield every event in the log in append order.

    Skips:
    - A partial trailing line (no LF terminator).
    - Lines that fail JSON parse (logged at DEBUG).
    - Records with unknown event_type (silent, forward-compat).
    - Records with schema_version major newer than 1 (WARNING).

    Yields:
        Each event as a dict, with the full envelope: schema_version,
        event_id, event_type, timestamp, emitted_at, source_cycle_id,
        payload.

    If events.jsonl does not exist, yields nothing (no error).
    """
    path = paths.events_log()
    if not path.exists():
        return

    with open(path, "rb") as f:
        for raw_line_no, raw_bytes in enumerate(f, start=1):
            # A partial trailing line lacks the LF terminator. The bytes
            # iterator includes the LF in the line, so a complete line
            # ends with b"\n". A trailing partial line will not.
            if not raw_bytes.endswith(b"\n"):
                logger.debug(
                    "event_log: skipping partial trailing line at line %d",
                    raw_line_no,
                )
                continue

            line = raw_bytes.rstrip(b"\n").decode("utf-8", errors="replace")
            if not line.strip():
                continue  # blank line, ignore

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug(
                    "event_log: JSON parse failure at line %d: %s",
                    raw_line_no, exc,
                )
                continue

            # Schema version check (§6.1 invariant 3).
            sv = event.get("schema_version", "")
            if not _schema_version_compatible(sv):
                logger.warning(
                    "event_log: skipping event with incompatible "
                    "schema_version=%r at line %d", sv, raw_line_no,
                )
                continue

            # Unknown event_type check (§6.1 invariant 2 — forward-compat).
            et = event.get("event_type", "")
            if et not in KNOWN_EVENT_TYPES:
                # Silent skip; this is normal forward-compat behavior.
                continue

            yield event


def iter_events_of_type(paths: Paths, event_type: str) -> Iterator[dict]:
    """Yield events matching the given event_type, in append order.

    Thin wrapper around iter_events with a type filter. Exists as a
    separate function for caller readability:
        iter_events_of_type(paths, "withdrawal_executed")
    reads better than the equivalent comprehension.

    Args:
        paths: IRAPM Paths object.
        event_type: One of KNOWN_EVENT_TYPES. Unknown types yield
            nothing (no error — supports forward-compat where a caller
            might filter for a type that doesn't exist in this log).

    Yields:
        Each matching event as a dict.
    """
    for event in iter_events(paths):
        if event.get("event_type") == event_type:
            yield event


def iter_events_in_window(
    paths: Paths,
    start: datetime,
    end: datetime,
    *,
    event_type: Optional[str] = None,
) -> Iterator[dict]:
    """Yield events whose timestamp falls in [start, end), optionally
    filtered to a single event_type.

    The window is half-open: start is inclusive, end is exclusive. This
    matches Python's standard convention for date/time ranges and makes
    abutting windows (e.g., month-to-month) non-overlapping by construction.

    Args:
        paths: IRAPM Paths object.
        start: Inclusive lower bound. Must be tz-aware.
        end: Exclusive upper bound. Must be tz-aware.
        event_type: If provided, additionally filter to this type.

    Yields:
        Each matching event as a dict.

    Raises:
        ValueError: start or end is naive.
    """
    _require_aware(start, "start")
    _require_aware(end, "end")

    for event in iter_events(paths):
        if event_type is not None and event.get("event_type") != event_type:
            continue
        ts_str = event.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            # Malformed timestamp — skip silently. The event itself is
            # still in the log; this iteration just can't place it in time.
            continue
        if start <= ts < end:
            yield event


def _schema_version_compatible(sv: str) -> bool:
    """True if the schema_version is in the 1.x family.

    Per EVENT_LOG_SPEC §3.4: a 1.x reader processes 1.x events. A 2.x
    event would not be processed by this reader; the caller logs and
    skips.

    Defensive parsing — an unparseable schema_version is treated as
    incompatible (skip + warn rather than yield potentially garbled data).
    """
    if not isinstance(sv, str) or "." not in sv:
        return False
    try:
        major_str, _ = sv.split(".", 1)
        major = int(major_str)
    except (ValueError, AttributeError):
        return False
    return major == 1


__all__ = [
    "SCHEMA_VERSION",
    "KNOWN_EVENT_TYPES",
    "append",
    "append_cycle_started",
    "append_cycle_completed",
    "append_cycle_halted",
    "append_decision_made",
    "append_order_placed",
    "append_fill_received",
    "append_withdrawal_executed",
    "append_cb_transition",
    "append_phase_transition",
    "append_annual_review_completed",
    "append_portfolio_snapshot",
    "append_state_snapshot",
    "append_alert_emitted",
    "iter_events",
    "iter_events_of_type",
    "iter_events_in_window",
]
