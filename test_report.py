"""
test_report.py — pytest suite for report.py (REPORT_SPEC §9).

ORGANIZATION:
    - Section 1: Helper infrastructure. Envelope construction, typed
      per-event-type builders, fixture-load utilities, Paths setup.
      All tests in this file rely on these helpers; if a helper is
      wrong, many tests will be wrong in the same way, so review the
      helpers first when investigating failures.
    - Section 2: Golden-file tests. Byte-compare reporter output to
      committed fixture files in fixtures/.
    - Section 3: Edge-case unit tests. The 14 items from REPORT_SPEC
      Section 9.4, each focused on a specific reporter behavior.
    - Section 4: prune_old_year_files tests. Filesystem-only; no
      event log involved.
    - Section 5: Slow integration tests. Marked @pytest.mark.slow.
      Use the real 20-year baseline events.jsonl checked into
      fixtures/.

DESIGN PHILOSOPHY (REPORT_SPEC Section 9.1):
    The reporter is a pure function of (event log, ruleset). Given
    deterministic inputs, output is deterministic text. This makes
    golden-file tests the right primary tool: a known event log
    produces a known output file, and any change is either a
    deliberate format change (regenerate the golden) or a regression
    (fix the code).

    Edge-case tests use small in-test event logs (5-15 events) and
    assert on specific output features rather than whole-file
    byte-compare. This keeps individual tests readable and
    diagnostic when they fail.

REGENERATING GOLDENS:
    When a deliberate format change requires updating fixtures:
        python test_report.py --regen-goldens
    This regenerates every fixtures/*.txt file from the current code
    state. Review the diff carefully before committing - anything
    unexpected is a regression dressed as a format change.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pytest

from persistence import Paths


# ============================================================================
# Section 1: Helper infrastructure
# ============================================================================

# Fixed schema version - matches event_log.SCHEMA_VERSION. Tests should
# not import the constant directly so that a future schema bump in
# event_log.py is caught here (we'd update the fixtures deliberately).
SCHEMA_VERSION = "1.0"

# Default source_cycle_id when caller doesn't supply one. Real cycle IDs
# are UUIDs; we use a fixed string so test event logs are diff-friendly
# (different test runs produce the same bytes for the same logical
# events) and so output that references cycle_id remains stable.
DEFAULT_SOURCE_CYCLE_ID = "test-cycle-0001"


# ----------------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------------

def ts(year: int, month: int, day: int,
       hour: int = 4, minute: int = 0, second: int = 0) -> str:
    """Construct an ISO-8601 UTC timestamp string. Default time is
    04:00:00 UTC (matches the synthetic broker's cycle-start convention
    seen in production event logs: 00:00 ET == 04:00 UTC).
    """
    return datetime(
        year, month, day, hour, minute, second, tzinfo=timezone.utc,
    ).isoformat()


# ----------------------------------------------------------------------------
# Envelope construction
# ----------------------------------------------------------------------------

def make_event(
    event_type: str,
    payload: dict,
    timestamp: str,
    *,
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
    schema_version: str = SCHEMA_VERSION,
    event_id: Optional[str] = None,
) -> dict:
    """Construct a complete event envelope matching event_log.append()
    output exactly (EVENT_LOG_SPEC Section 4).

    Per REPORT_SPEC Section 9.6, test events set emitted_at == timestamp;
    real code distinguishes them (timestamp = decision clock, emitted_at
    = wall clock) but the reporter doesn't depend on the distinction,
    so test fixtures simplify by equating them.

    `event_id` defaults to a stable hash-derived value so the same
    logical event in the same test always produces the same id -
    keeps golden files diff-friendly. Pass an explicit `event_id`
    for tests that need control over id ordering.
    """
    if event_id is None:
        # Stable but distinguishing: hash of the inputs. Avoids the
        # nondeterminism of uuid4() while still making each event
        # unique within a test.
        h = hash((event_type, timestamp, json.dumps(payload, sort_keys=True, default=str)))
        event_id = f"evt_{h & 0xFFFFFFFFFFFFFFFF:016x}"
    return {
        "schema_version": schema_version,
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "emitted_at": timestamp,
        "source_cycle_id": source_cycle_id,
        "payload": payload,
    }


# ----------------------------------------------------------------------------
# Typed event builders
#
# One per event type. Each accepts only the fields the reporter actually
# reads, with documented defaults for the rest. Tests stay terse;
# changes to payload shape happen here, not scattered across 30 tests.
# ----------------------------------------------------------------------------

def make_cycle_started(
    timestamp: str,
    *,
    cycle_type: str = "weekly",
    is_restart: bool = False,
    box_id: str = "test-box",
    client_id: int = 11,
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """cycle_started (EVENT_LOG_SPEC Section 4.1). Reporter consumes
    cycle_type to filter weekly-vs-daily cycles."""
    return make_event(
        "cycle_started",
        {
            "cycle_type": cycle_type,
            "is_restart": is_restart,
            "box_id": box_id,
            "client_id": client_id,
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_cycle_completed(
    timestamp: str,
    *,
    cycle_type: str = "weekly",
    phase: str = "PHASE_1",
    cb_state: str = "CB_INACTIVE",
    income_state: str = "ACTIVE",
    operational_pause: bool = False,
    withdrawal_capacity_exhausted: bool = False,
    lookback_status: Optional[str] = None,
    lookback_value: Optional[str] = None,
    plan_entry_count: int = 0,
    duration_ms: int = 10,
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """cycle_completed (EVENT_LOG_SPEC Section 4.2). Reporter uses
    `cycle_type` to filter recent-activity to weekly cycles only
    (Fix #5 in Session 1), and reads phase/cb_state/income_state
    in current_status header rendering."""
    return make_event(
        "cycle_completed",
        {
            "cycle_type": cycle_type,
            "phase": phase,
            "cb_state": cb_state,
            "income_state": income_state,
            "operational_pause": operational_pause,
            "withdrawal_capacity_exhausted": withdrawal_capacity_exhausted,
            "lookback_status": lookback_status,
            "lookback_value": lookback_value,
            "plan_entry_count": plan_entry_count,
            "is_scheduled_withdrawal_day": False,
            "is_annual_review_day": False,
            "is_phase2_reallocation_day": False,
            "duration_ms": duration_ms,
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_portfolio_snapshot(
    timestamp: str,
    *,
    total_aum: str,
    cash: str = "4000.00",
    sgov_buffer: str = "72000.00",
    positions: Optional[dict[str, dict[str, str]]] = None,
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """portfolio_snapshot (EVENT_LOG_SPEC Section 4.11). The load-bearing
    event for per-row balance columns. Reporter reads:
      - total_aum_dollars (for gross_net_liq column)
      - cash_dollars (for cash_buff column)
      - sgov_buffer_dollars (for sgov_buffer column)
      - positions[symbol].market_value_dollars (per-symbol cells,
        and the source for fi_bucket/growth_bucket derivation)
      - positions[symbol].market_price_dollars (asset-movement
        section of annual summary only)

    fi_bucket, growth_bucket, fi_weight, growth_weight are NOT read
    from the payload - reporter recomputes them from positions
    (see report.py line ~518). Tests can omit those fields.

    Default positions: all five core symbols at zero, so callers
    only specify the symbols they care about. Override per-symbol
    by passing `positions={"FBCG": {...}}`.

    Each position dict needs three string-decimal fields:
        quantity_shares, market_price_dollars, market_value_dollars

    Helper merges caller's `positions` over the all-zeros default.
    """
    default_positions = {
        sym: {
            "quantity_shares": "0",
            "market_price_dollars": "0",
            "market_value_dollars": "0",
        }
        for sym in ("PYLD", "JPIE", "FBCG", "AVUV", "SGOV", "GBIL")
    }
    if positions:
        for sym, fields in positions.items():
            default_positions[sym] = {**default_positions.get(sym, {}), **fields}

    return make_event(
        "portfolio_snapshot",
        {
            "total_aum_dollars": total_aum,
            "cash_dollars": cash,
            "sgov_buffer_dollars": sgov_buffer,
            # fi_bucket_dollars / growth_bucket_dollars / weights are
            # written by cycle.py for forward-compat but the reporter
            # recomputes from positions[]. We mirror cycle.py's choice
            # and write them too, defaulted to "0" - the reporter
            # ignores these and derives its own values.
            "fi_bucket_dollars": "0",
            "growth_bucket_dollars": "0",
            "fi_weight": "0",
            "growth_weight": "0",
            "positions": default_positions,
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_withdrawal_executed(
    timestamp: str,
    *,
    amount: str,
    scheduled_amount: Optional[str] = None,
    binding_ceiling: Optional[str] = None,
    was_capped: bool = False,
    scheduled_ach_date: Optional[str] = None,
    phase: str = "PHASE_1",
    income_state: str = "ACTIVE",
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """withdrawal_executed (EVENT_LOG_SPEC Section 4.7). Reporter reads:
      - amount_paid_dollars (cash_out cell)
      - scheduled_amount_dollars (annual-summary cap detail)
      - binding_ceiling ("guardrail" -> G, "dollar_cap" -> C, "both",
        or None for uncapped - Phase 1 binding map per Gap 1 closure
        in HANDOFF_PHASE1_COMPLETE.md)
      - was_capped (drives cap-symbol suffix on cash_out)
      - scheduled_ach_date (referenced in current_status only)
      - phase, income_state (recent-alerts context)

    Defaults model an uncapped Phase 1 withdrawal. For capped Phase 3
    withdrawals, pass binding_ceiling="dollar_cap" + was_capped=True
    + scheduled_amount > amount.

    Note on vocabulary: `binding_ceiling` here is the SPEC form
    ("guardrail"/"dollar_cap"/"both"). The alert TEMPLATE form
    ("portfolio_percent"/"dollar"/"both") lives only in alert
    contexts. The reporter consumes the spec form. See
    HANDOFF_PHASE1_COMPLETE.md "Gap 1: CLOSED".
    """
    if scheduled_amount is None:
        scheduled_amount = amount
    if scheduled_ach_date is None:
        # Default ACH date: the timestamp's date.
        scheduled_ach_date = timestamp[:10]
    return make_event(
        "withdrawal_executed",
        {
            "withdrawal_dollar_amount": amount,
            "scheduled_ach_date": scheduled_ach_date,
            "binding_ceiling": binding_ceiling,
            "scheduled_amount_dollars": scheduled_amount,
            "amount_paid_dollars": amount,
            "was_capped": was_capped,
            "sources": [],
            "phase": phase,
            "income_state_at_withdrawal": income_state,
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_cb_transition(
    timestamp: str,
    *,
    from_state: str,
    to_state: str,
    reason: str = "signal",
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """cb_transition (EVENT_LOG_SPEC Section 4.8). Reporter reads from_state
    and to_state for CB1/CB2/REC annotation rendering. The annotation
    code is determined by direction:
      - CB_INACTIVE -> CB1: CB1 entered
      - CB1 -> CB2: CB2 promoted (also yields CB1 timer start)
      - CB1|CB2 -> CB_INACTIVE: REC (recovery confirmed)

    `cb2_entry_conditions_after` is a payload field the reporter
    doesn't currently consume; we include it as empty dict for
    schema completeness.
    """
    return make_event(
        "cb_transition",
        {
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "cb2_entry_conditions_after": {},
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_phase_transition(
    timestamp: str,
    *,
    from_phase: str,
    to_phase: str,
    trigger: str = "calendar",
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """phase_transition (EVENT_LOG_SPEC Section 4.9). Reporter reads to_phase
    to render P2/P3 annotations on the row corresponding to the
    transition date."""
    return make_event(
        "phase_transition",
        {
            "from_phase": from_phase,
            "to_phase": to_phase,
            "trigger": trigger,
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_annual_review_completed(
    timestamp: str,
    *,
    review_year: int,
    cpi_rate_applied: str = "0.0350",
    binding_constraint: Optional[str] = None,
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """annual_review_completed (EVENT_LOG_SPEC Section 4.10). Reporter reads
    `binding_constraint == "cpi_freeze"` to attach the (F) suffix to
    AR annotations and to count CPI freezes for the annual summary
    and TOTALS sections.

    For a normal annual review (no freeze), pass binding_constraint=None.
    For a freeze year, pass binding_constraint="cpi_freeze".
    """
    return make_event(
        "annual_review_completed",
        {
            "review_year": review_year,
            "cpi_rate_applied": cpi_rate_applied,
            "binding_constraint": binding_constraint,
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_state_snapshot(
    timestamp: str,
    *,
    trigger: str = "monthly_heartbeat",
    phase: str = "PHASE_1",
    cb_state: str = "CB_INACTIVE",
    cb1_active_timer_started_at: Optional[str] = None,
    income_state: str = "ACTIVE",
    operational_pause: bool = False,
    withdrawal_capacity_exhausted: bool = False,
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """state_snapshot (EVENT_LOG_SPEC Section 4.12). Reporter reads
    cb1_active_timer_started_at (Fix #7 from Session 1) to render
    "entered YYYY-MM-DD, day N of 90" in current_status header.

    The full state_snapshot payload is the OperatingState model_dump,
    which is large. This helper writes the fields the reporter
    actually consumes plus the minimum scaffolding needed for the
    payload to parse cleanly.
    """
    cb_machine_payload: dict[str, Any] = {
        "state": cb_state,
    }
    if cb1_active_timer_started_at is not None:
        # Naive ISO string per Session 1 Fix #7 - the reporter's
        # _parse_iso_datetime normalizes naive strings to UTC-aware.
        cb_machine_payload["cb1_active_timer_started_at"] = cb1_active_timer_started_at

    payload = {
        "trigger": trigger,
        "phase": phase,
        "cb_machine": cb_machine_payload,
        "income_state": income_state,
        "operational_pause": {"paused": operational_pause},
        "withdrawal_capacity_exhausted": withdrawal_capacity_exhausted,
    }
    return make_event(
        "state_snapshot", payload, timestamp,
        source_cycle_id=source_cycle_id,
    )


def make_alert_emitted(
    timestamp: str,
    *,
    alert_id: str,
    email_ok: bool = True,
    sms_ok: bool = True,
    email_error: Optional[str] = None,
    sms_error: Optional[str] = None,
    deduped: bool = False,
    context: Optional[dict] = None,
    source_cycle_id: str = DEFAULT_SOURCE_CYCLE_ID,
) -> dict:
    """alert_emitted (EVENT_LOG_SPEC Section 4.13). Reporter reads:
      - alert_id (recent-alerts column + DPD/DPR/SAR detection)
      - context (for context-key rendering in recent-alerts)
      - timestamp (4-week window filtering)

    The DPD/DPR/SAR detection (Fix #9 Session 1) checks
    alert_id values "phase2_opportunistic_deploy",
    "phase2_opportunistic_recover", and
    "phase2_semi_annual_reallocation" respectively.
    """
    return make_event(
        "alert_emitted",
        {
            "alert_id": alert_id,
            "email_ok": email_ok,
            "sms_ok": sms_ok,
            "email_error": email_error,
            "sms_error": sms_error,
            "deduped": deduped,
            "context": context or {},
        },
        timestamp,
        source_cycle_id=source_cycle_id,
    )


# ----------------------------------------------------------------------------
# Event log construction
# ----------------------------------------------------------------------------

def build_events_log(state_dir: Path, events: list[dict]) -> Path:
    """Write a list of event dicts to state_dir/events.jsonl as JSONL.

    Matches event_log.append()'s on-disk format: one JSON object per
    line, no trailing whitespace, UTF-8, terminating newline. Uses
    compact separators (no spaces) to mirror production output -
    consequential for fixture byte-equality if test events are ever
    converted to fixtures.

    Creates state_dir if it doesn't exist. Returns the events.jsonl
    path so callers can chain.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    events_path = state_dir / "events.jsonl"
    with events_path.open("w", encoding="utf-8", newline="") as f:
        for event in events:
            f.write(json.dumps(event, separators=(",", ":"), default=str))
            f.write("\n")
    return events_path


def build_paths(tmp_path: Path) -> Paths:
    """Construct a Paths object rooted at tmp_path/state. Calls
    ensure_dirs() so state_dir/logs_dir/reports_dir all exist.
    Standard fixture-setup boilerplate; every test that needs Paths
    starts with `paths = build_paths(tmp_path)`.
    """
    state_dir = tmp_path / "state"
    paths = Paths(state_dir=state_dir)
    paths.ensure_dirs()
    return paths


# ----------------------------------------------------------------------------
# Golden-file utilities
# ----------------------------------------------------------------------------

# Resolve fixtures/ at module import. Pinning here means tests can be
# invoked from any cwd as long as pytest finds this file.
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_golden(name: str) -> str:
    """Load a committed fixture file from fixtures/. Returns its
    text content. Use for byte-compare in golden-file tests:

        assert output_path.read_text() == load_golden("foo.txt")

    Trailing newlines are preserved as-is. If the fixture is missing
    (e.g., a new test added without committing its golden yet),
    raises FileNotFoundError with a clear message.
    """
    path = FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Golden fixture missing: {path}. "
            f"If this is a new test, regenerate goldens with: "
            f"python test_report.py --regen-goldens"
        )
    return path.read_text(encoding="utf-8")


def write_golden(name: str, content: str) -> Path:
    """Write content to fixtures/{name}. Used only by the
    --regen-goldens workflow; tests should never call this. Returns
    the path written for callers that want to print it.
    """
    path = FIXTURES_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ----------------------------------------------------------------------------
# Smoke test for helpers (proves the foundation works before any
# real test relies on it)
# ----------------------------------------------------------------------------

def test_make_event_envelope_shape():
    """Sanity: make_event produces all seven envelope fields with the
    expected types and values. If this breaks, every other test in
    this file is suspect - fix here first."""
    event = make_event(
        "cycle_completed",
        {"phase": "PHASE_1"},
        "2027-01-06T04:00:00+00:00",
    )
    assert event["schema_version"] == "1.0"
    assert event["event_id"].startswith("evt_")
    assert event["event_type"] == "cycle_completed"
    assert event["timestamp"] == "2027-01-06T04:00:00+00:00"
    assert event["emitted_at"] == "2027-01-06T04:00:00+00:00"
    assert event["source_cycle_id"] == DEFAULT_SOURCE_CYCLE_ID
    assert event["payload"] == {"phase": "PHASE_1"}


def test_make_event_id_is_stable():
    """Same logical event -> same event_id. Required for golden-file
    determinism across runs."""
    e1 = make_event("x", {"a": 1}, "2027-01-01T00:00:00+00:00")
    e2 = make_event("x", {"a": 1}, "2027-01-01T00:00:00+00:00")
    assert e1["event_id"] == e2["event_id"]


def test_build_events_log_roundtrip(tmp_path):
    """build_events_log writes valid JSONL that event_log.iter_events
    can read back. Catches integration breakage between the test
    helper and the production reader."""
    from event_log import iter_events
    paths = build_paths(tmp_path)
    events = [
        make_cycle_completed("2027-01-06T04:00:00+00:00"),
        make_portfolio_snapshot(
            "2027-01-06T04:00:01+00:00",
            total_aum="100000.00",
            positions={"FBCG": {"market_value_dollars": "50000.00"}},
        ),
    ]
    build_events_log(paths.state_dir, events)
    roundtripped = list(iter_events(paths))
    assert len(roundtripped) == 2
    assert roundtripped[0]["event_type"] == "cycle_completed"
    assert roundtripped[1]["event_type"] == "portfolio_snapshot"
    assert (
        roundtripped[1]["payload"]["positions"]["FBCG"]["market_value_dollars"]
        == "50000.00"
    )


def test_build_paths_creates_reports_dir(tmp_path):
    """build_paths must call ensure_dirs() so report.py invocations
    don't fail on missing reports_dir. This was a Phase 2 Session 2
    Workstream 1 addition; the test guards the wiring."""
    paths = build_paths(tmp_path)
    assert paths.state_dir.exists()
    assert paths.logs_dir.exists()
    assert paths.reports_dir.exists()


# ============================================================================
# Section 2: Golden-file tests
# ============================================================================

# --- Generated-line normalization ------------------------------------------

def _strip_generated_line(text: str) -> str:
    """Remove the volatile "Generated: ..." line from current_status
    output before byte-comparing to a golden. The Generated line
    embeds wall-clock-of-render time, which varies per test run and
    is the only nondeterministic content in current_status when the
    event log is dated safely in the past (so the _anchor_now
    heuristic engages for the body).

    Strips every line starting with "Generated:". Trailing newline
    is preserved if the input had one.
    """
    out = "\n".join(
        line for line in text.splitlines()
        if not line.startswith("Generated:")
    )
    if text.endswith("\n"):
        out += "\n"
    return out


# --- Phase 2 + CB1 scenario builder ----------------------------------------
#
# Module-level builder rather than inline-in-test so _regen_goldens()
# can call the same scenario when regenerating the golden file.
# Returns the event list ready for build_events_log().
#
# SCENARIO:
#   5 weekly cycles dated 2025-03-19 through 2025-04-16 (safely past,
#   anchor heuristic engages). Story: a Phase 1 install transitioning
#   to Phase 2 at the end of week 1, with CB1 firing in week 4.
#
#   Week 1 (2025-03-19): Phase 1 weekly cycle; one Phase 1 withdrawal
#                        ($3000); phase transition Phase 1 -> Phase 2.
#   Week 2 (2025-03-26): Phase 2, CB_INACTIVE.
#   Week 3 (2025-04-02): Phase 2, CB_INACTIVE.
#   Week 4 (2025-04-09): Phase 2; CB1 fires. Alert emitted.
#   Week 5 (2025-04-16): Phase 2, CB1 active (day 7 of 90). Second
#                        alert emitted. Latest state_snapshot drives
#                        the current_status header.
#
# COVERAGE:
#   - Phase 2 header rendering (Income state = HALTED, etc.)
#   - CB1 day-counter rendering ("entered 2025-04-09, day 7 of 90")
#     exercises Session 1 Fix #7 (cb1_active_timer_started_at) and
#     the tz-aware/naive normalization fix.
#   - Recent-activity block filtered to weekly cycles (Fix #5),
#     showing weeks 2-5 (last 4 weekly cycles).
#   - Last-withdrawal line showing the residual Phase 1 withdrawal
#     from before the transition.
#   - Recent-alerts block showing both alerts within the 4-week
#     window (the week-4 CB1 alert and the week-5 informational alert).

PHASE2_CB1_GOLDEN = "current_status_phase2_cb1.txt"


def _build_phase2_cb1_events() -> list[dict]:
    """Construct the event list for the current_status_phase2_cb1
    scenario. See module docstring for scenario detail.

    TIMESTAMP CONVENTION (matches production):
        All events for a single weekly cycle share that cycle's
        timestamp exactly. Within-cycle ordering is list order in
        this function, not timestamp ordering. Verified against the
        real 2005-2025 events.jsonl: every event for cycle
        2005-04-20 is timestamped 2005-04-20T04:00:00+00:00.

        The reporter's recent-activity windowing math
        (`(prev_ts + 1us, this_ts + 1us]`) depends on this
        convention - microsecond-bumping events to make them
        "distinct" puts them into the wrong cycle's window.
    """
    events: list[dict] = []

    # ---- Week 1: 2025-03-19, Phase 1 cycle + withdrawal + transition ----
    events.append(make_cycle_completed(
        ts(2025, 3, 19),
        cycle_type="weekly",
        phase="PHASE_1",
        cb_state="CB_INACTIVE",
        income_state="ACTIVE",
    ))
    events.append(make_portfolio_snapshot(
        ts(2025, 3, 19),
        total_aum="100000.00",
        cash="4000.00",
        sgov_buffer="72000.00",
        positions={
            "PYLD": {"market_value_dollars": "7000.00"},
            "JPIE": {"market_value_dollars": "7000.00"},
            "FBCG": {"market_value_dollars": "5000.00"},
            "AVUV": {"market_value_dollars": "5000.00"},
        },
    ))
    events.append(make_withdrawal_executed(
        ts(2025, 3, 19),
        amount="3000.00",
        scheduled_amount="3000.00",
        binding_ceiling=None,
        was_capped=False,
        scheduled_ach_date="2025-03-19",
        phase="PHASE_1",
    ))
    events.append(make_phase_transition(
        ts(2025, 3, 19),
        from_phase="PHASE_1",
        to_phase="PHASE_2",
        trigger="calendar",
    ))
    events.append(make_state_snapshot(
        ts(2025, 3, 19),
        trigger="phase_transition",
        phase="PHASE_2",
        cb_state="CB_INACTIVE",
        income_state="HALTED",
    ))

    # ---- Week 2: 2025-03-26, Phase 2 quiet ----
    events.append(make_cycle_completed(
        ts(2025, 3, 26),
        cycle_type="weekly",
        phase="PHASE_2",
        cb_state="CB_INACTIVE",
        income_state="HALTED",
    ))
    events.append(make_portfolio_snapshot(
        ts(2025, 3, 26),
        total_aum="101000.00",
        cash="4000.00",
        sgov_buffer="72100.00",
        positions={
            "FBCG": {"market_value_dollars": "12000.00"},
            "AVUV": {"market_value_dollars": "12000.00"},
            "GBIL": {"market_value_dollars": "900.00"},
        },
    ))

    # ---- Week 3: 2025-04-02, Phase 2 quiet ----
    events.append(make_cycle_completed(
        ts(2025, 4, 2),
        cycle_type="weekly",
        phase="PHASE_2",
        cb_state="CB_INACTIVE",
        income_state="HALTED",
    ))
    events.append(make_portfolio_snapshot(
        ts(2025, 4, 2),
        total_aum="102000.00",
        cash="4000.00",
        sgov_buffer="72200.00",
        positions={
            "FBCG": {"market_value_dollars": "12500.00"},
            "AVUV": {"market_value_dollars": "12300.00"},
            "GBIL": {"market_value_dollars": "1000.00"},
        },
    ))

    # ---- Week 4: 2025-04-09, CB1 fires + first alert ----
    events.append(make_cycle_completed(
        ts(2025, 4, 9),
        cycle_type="weekly",
        phase="PHASE_2",
        cb_state="CB1",
        income_state="HALTED",
    ))
    events.append(make_cb_transition(
        ts(2025, 4, 9),
        from_state="CB_INACTIVE",
        to_state="CB1",
        reason="signal",
    ))
    events.append(make_alert_emitted(
        ts(2025, 4, 9),
        alert_id="cb_transition",
        context={"from_state": "CB_INACTIVE", "to_state": "CB1", "reason": "signal"},
    ))
    events.append(make_portfolio_snapshot(
        ts(2025, 4, 9),
        total_aum="95000.00",
        cash="4000.00",
        sgov_buffer="72300.00",
        positions={
            "FBCG": {"market_value_dollars": "9500.00"},
            "AVUV": {"market_value_dollars": "9200.00"},
            "GBIL": {"market_value_dollars": "1000.00"},
        },
    ))
    # state_snapshot with cb1_active_timer_started_at - drives the
    # "entered YYYY-MM-DD, day N of 90" rendering. Naive ISO string
    # per Session 1 Fix #7's serialization convention.
    events.append(make_state_snapshot(
        ts(2025, 4, 9),
        trigger="cb_transition",
        phase="PHASE_2",
        cb_state="CB1",
        cb1_active_timer_started_at="2025-04-09T00:00:00",
        income_state="HALTED",
    ))

    # ---- Week 5: 2025-04-16, CB1 still active (day 7) + second alert ----
    events.append(make_cycle_completed(
        ts(2025, 4, 16),
        cycle_type="weekly",
        phase="PHASE_2",
        cb_state="CB1",
        income_state="HALTED",
    ))
    events.append(make_alert_emitted(
        ts(2025, 4, 16),
        alert_id="weekly_summary",
        context={"phase": "PHASE_2", "cb_state": "CB1"},
    ))
    events.append(make_portfolio_snapshot(
        ts(2025, 4, 16),
        total_aum="96500.00",
        cash="4000.00",
        sgov_buffer="72400.00",
        positions={
            "FBCG": {"market_value_dollars": "10000.00"},
            "AVUV": {"market_value_dollars": "10100.00"},
            "GBIL": {"market_value_dollars": "1000.00"},
        },
    ))
    # Final state_snapshot - this is the one the reporter reads for the
    # header (latest state_snapshot wins).
    events.append(make_state_snapshot(
        ts(2025, 4, 16),
        trigger="monthly_heartbeat",
        phase="PHASE_2",
        cb_state="CB1",
        cb1_active_timer_started_at="2025-04-09T00:00:00",
        income_state="HALTED",
    ))

    return events


def test_write_current_status_phase2_cb1(tmp_path):
    """Golden-file test: Phase 2 + CB1 active scenario produces output
    matching fixtures/current_status_phase2_cb1.txt byte-for-byte
    (after stripping the volatile "Generated:" line from both sides).

    Exercises: header rendering with Phase 2 + CB1 day-counter
    (Session 1 Fix #7 and tz fix), recent-activity weekly filter
    (Fix #5), last-withdrawal line, recent-alerts column padding.
    """
    import report

    paths = build_paths(tmp_path)
    events = _build_phase2_cb1_events()
    build_events_log(paths.state_dir, events)

    output_path = paths.reports_dir / "current_status.txt"
    report.write_current_status(paths.state_dir, output_path)

    actual = _strip_generated_line(output_path.read_text(encoding="utf-8"))
    expected = _strip_generated_line(load_golden(PHASE2_CB1_GOLDEN))
    assert actual == expected, (
        "current_status output differs from golden. "
        "If the change is intentional, regenerate with: "
        "python test_report.py --regen-goldens"
    )


# ----------------------------------------------------------------------------
# Section 3: Edge-case unit tests  (TODO: Workstream 2 Step 4)
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Section 4: prune_old_year_files tests  (TODO: Workstream 2 Step 5)
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Section 5: Slow integration tests  (TODO: Workstream 2 Step 6)
# ----------------------------------------------------------------------------


# ============================================================================
# CLI: regenerate goldens
# ============================================================================

def _regen_goldens() -> int:
    """Regenerate every fixtures/*.txt file from current code. Called
    by the --regen-goldens CLI workflow.

    Each new golden gets a dedicated entry here. The function must
    remain a deliberate, narrow set of generator calls so an
    accidental run can't silently corrupt every golden at once.
    """
    import tempfile
    import report

    # current_status_phase2_cb1
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        paths = build_paths(tmp)
        events = _build_phase2_cb1_events()
        build_events_log(paths.state_dir, events)
        output_path = paths.reports_dir / "current_status.txt"
        report.write_current_status(paths.state_dir, output_path)
        content = output_path.read_text(encoding="utf-8")
        written = write_golden(PHASE2_CB1_GOLDEN, content)
        print(f"Regenerated: {written}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--regen-goldens":
        sys.exit(_regen_goldens())
    print("Run via pytest:  pytest test_report.py")
    print("Regen goldens:   python test_report.py --regen-goldens")
    sys.exit(0)
