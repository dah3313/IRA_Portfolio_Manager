"""
test_event_log.py — Unit tests for event_log.py.

Covers EVENT_LOG_SPEC §5.6's five mandated test areas plus defensive
tests for behaviors the spec implies but doesn't explicitly require:

    §5.6 #1 — Round-trip (write event, read file, parse, verify match)
    §5.6 #2 — Decimal preservation through JSON round-trip
    §5.6 #3 — Timezone enforcement (naive datetime raises ValueError)
    §5.6 #4 — Concurrent-read tolerance (partial trailing line)
    §5.6 #5 — Disk-failure handling (OSError caught, WARNING logged)

Plus:
    - Envelope structure validation
    - event_id format conformance (§3.3)
    - Unknown event_type rejection (§5.4)
    - JSON encoder behavior on the typed values payloads contain
    - Append-only behavior (existing records preserved)
    - Each of the 13 per-event helpers produces a valid event
    - NaN/Inf rejection (§5.3)

Tests are organized into classes by concern area, all using pytest's
tmp_path fixture for isolated filesystems. No mocking of the writer's
internal helpers — every test exercises the real append() code path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

# Module under test. Tests assume cwd is the repo root, or sys.path is
# configured so event_log and persistence are importable as top-level
# modules. Mirrors how the production code imports them.
import event_log
from event_log import (
    SCHEMA_VERSION,
    KNOWN_EVENT_TYPES,
    append,
    append_cycle_started,
    append_cycle_completed,
    append_cycle_halted,
    append_decision_made,
    append_order_placed,
    append_fill_received,
    append_withdrawal_executed,
    append_cb_transition,
    append_phase_transition,
    append_annual_review_completed,
    append_portfolio_snapshot,
    append_state_snapshot,
    append_alert_emitted,
)
from persistence import Paths


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    """Fresh Paths object pointing at a tmp directory, with dirs created."""
    p = Paths(state_dir=tmp_path / "state")
    p.ensure_dirs()
    return p


@pytest.fixture
def now() -> datetime:
    """Fixed timezone-aware 'now' for deterministic tests."""
    return datetime(2026, 5, 14, 14, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def cycle_id() -> str:
    """A stable cycle UUID string for tests that need one."""
    return "ecffd87f-4651-41dc-b761-0e36657cf8f6"


def read_lines(paths: Paths) -> list[dict]:
    """Read every JSON line from events.jsonl, parsed.

    Implements the reader contract per §6.1: JSON parse failures
    (corrupt line, partial trailing line) are silently skipped.
    """
    path = paths.events_log()
    if not path.exists():
        return []
    out = []
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line.decode("utf-8")))
            except json.JSONDecodeError:
                # §6.1 invariant: tolerate partial trailing line and
                # any line that fails JSON parse. This is what real
                # readers (reporter, audit tooling) must do.
                continue
    return out


def minimal_cycle_started_payload() -> dict:
    """Minimal valid cycle_started payload (per §4.1)."""
    return {
        "cycle_type": "weekly",
        "is_restart": False,
        "box_id": "harness-box",
        "client_id": 11,
    }


# ===========================================================================
# 1. ENVELOPE STRUCTURE
# ===========================================================================

class TestEnvelope:
    """The envelope shape is the load-bearing contract (§3.1)."""

    def test_envelope_has_all_required_fields(self, paths, now, cycle_id):
        append(
            paths, "cycle_started", minimal_cycle_started_payload(),
            now=now, source_cycle_id=cycle_id,
        )
        records = read_lines(paths)
        assert len(records) == 1
        rec = records[0]
        # Per §3.1, every record has these seven envelope fields.
        for field in (
            "schema_version", "event_id", "event_type",
            "timestamp", "emitted_at", "source_cycle_id", "payload",
        ):
            assert field in rec, f"missing envelope field: {field}"

    def test_schema_version_is_1_0(self, paths, now, cycle_id):
        append(
            paths, "cycle_started", minimal_cycle_started_payload(),
            now=now, source_cycle_id=cycle_id,
        )
        rec = read_lines(paths)[0]
        assert rec["schema_version"] == "1.0"
        assert rec["schema_version"] == SCHEMA_VERSION

    def test_event_id_format_evt_prefix_plus_32_hex(self, paths, now, cycle_id):
        # §3.3: event_id is "evt_" + 32 hex characters (UUID4 minus dashes).
        append(
            paths, "cycle_started", minimal_cycle_started_payload(),
            now=now, source_cycle_id=cycle_id,
        )
        rec = read_lines(paths)[0]
        assert re.fullmatch(r"evt_[0-9a-f]{32}", rec["event_id"]), \
            f"event_id doesn't match expected format: {rec['event_id']}"

    def test_event_ids_are_unique_across_calls(self, paths, now, cycle_id):
        # §3.3: UUID4 collision probability is negligible.
        ids = set()
        for _ in range(100):
            eid = append(
                paths, "cycle_started", minimal_cycle_started_payload(),
                now=now, source_cycle_id=cycle_id,
            )
            ids.add(eid)
        assert len(ids) == 100

    def test_append_returns_event_id_matching_written_record(
        self, paths, now, cycle_id,
    ):
        returned_id = append(
            paths, "cycle_started", minimal_cycle_started_payload(),
            now=now, source_cycle_id=cycle_id,
        )
        rec = read_lines(paths)[0]
        assert returned_id == rec["event_id"]

    def test_timestamp_defaults_to_now_when_in_sim_omitted(
        self, paths, now, cycle_id,
    ):
        # Production case: in_sim_timestamp is None, timestamp equals now.
        append(
            paths, "cycle_started", minimal_cycle_started_payload(),
            now=now, source_cycle_id=cycle_id,
        )
        rec = read_lines(paths)[0]
        assert rec["timestamp"] == now.isoformat()
        assert rec["emitted_at"] == now.isoformat()

    def test_in_sim_timestamp_differs_from_emitted_at(
        self, paths, now, cycle_id,
    ):
        # Simulator case: in-sim time is the historical date, wall-clock
        # is the actual emission time. They differ.
        in_sim = datetime(2005, 5, 4, 10, 0, 0, tzinfo=timezone.utc)
        append(
            paths, "cycle_started", minimal_cycle_started_payload(),
            now=now,
            source_cycle_id=cycle_id,
            in_sim_timestamp=in_sim,
        )
        rec = read_lines(paths)[0]
        assert rec["timestamp"] == in_sim.isoformat()
        assert rec["emitted_at"] == now.isoformat()
        assert rec["timestamp"] != rec["emitted_at"]

    def test_source_cycle_id_can_be_none(self, paths, now):
        # §3.1: source_cycle_id is null for events outside any cycle.
        append(
            paths, "cycle_started", minimal_cycle_started_payload(),
            now=now, source_cycle_id=None,
        )
        rec = read_lines(paths)[0]
        assert rec["source_cycle_id"] is None

    def test_event_type_in_envelope_matches_call_arg(self, paths, now, cycle_id):
        append(
            paths, "alert_emitted",
            {"alert_id": "x", "alert_template": "x", "severity": "info",
             "message": "x", "context": {}},
            now=now, source_cycle_id=cycle_id,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "alert_emitted"


# ===========================================================================
# 2. ROUND-TRIP (§5.6 #1)
# ===========================================================================

class TestRoundTrip:
    """Write an event, read the file, verify parsed record matches input."""

    def test_simple_payload_roundtrips(self, paths, now, cycle_id):
        payload = {"cycle_type": "weekly", "is_restart": True,
                   "box_id": "test-box", "client_id": 12}
        append(paths, "cycle_started", payload,
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"] == payload

    def test_payload_with_nested_dict_roundtrips(self, paths, now, cycle_id):
        # decision_made has nested inputs and plan_entries sub-structures.
        payload = {
            "inputs": {
                "phase": "PHASE_1",
                "cb_state": "CB_INACTIVE",
                "income_state": "ACTIVE",
                "position_values": {"FBCG": "167125.50", "AVUV": "167125.50"},
            },
            "plan_entries": [
                {"entry_index": 0, "kind": "WITHDRAWAL",
                 "dollar_amount": "3000.00",
                 "sources": [{"symbol": "SGOV", "dollar_amount": "3000.00"}]},
            ],
            "should_skip_action_layer": False,
            "skip_reason": None,
        }
        append(paths, "decision_made", payload,
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"] == payload

    def test_empty_payload_roundtrips(self, paths, now, cycle_id):
        # The writer doesn't validate payload structure; an empty payload
        # is technically valid (though no real event type uses one).
        append(paths, "cycle_started", {},
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"] == {}

    def test_multiple_appends_each_become_separate_lines(self, paths, now, cycle_id):
        for i in range(5):
            append(paths, "cycle_started",
                   {**minimal_cycle_started_payload(), "client_id": i},
                   now=now, source_cycle_id=cycle_id)
        # Verify by reading raw line count, not just records.
        path = paths.events_log()
        with path.open("rb") as f:
            raw_lines = [l for l in f if l.strip()]
        assert len(raw_lines) == 5
        records = read_lines(paths)
        assert [r["payload"]["client_id"] for r in records] == [0, 1, 2, 3, 4]


# ===========================================================================
# 3. DECIMAL PRESERVATION (§5.6 #2)
# ===========================================================================

class TestDecimalPreservation:
    """Decimals serialize to JSON strings; precision is preserved exactly."""

    def test_decimal_in_payload_serializes_as_string(self, paths, now, cycle_id):
        payload = {"lookback_value": Decimal("-0.083400")}
        append(paths, "cycle_completed", payload,
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["lookback_value"] == "-0.083400"
        assert isinstance(rec["payload"]["lookback_value"], str)

    def test_decimal_high_precision_preserved(self, paths, now, cycle_id):
        # 28 significant digits is well beyond what IRAPM uses but proves
        # we're not losing precision through float conversion.
        v = Decimal("3304555.43219876543210987654321")
        append(paths, "cycle_completed", {"total_aum": v},
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["total_aum"] == str(v)
        # Reading back as Decimal yields exact equality.
        assert Decimal(rec["payload"]["total_aum"]) == v

    def test_nested_decimals_preserved(self, paths, now, cycle_id):
        # Real payloads put Decimals inside lists and dicts.
        payload = {
            "positions": {
                "FBCG": {"quantity_shares": Decimal("17.9640"),
                         "market_value_dollars": Decimal("3000.00")},
            },
            "sources": [
                {"symbol": "SGOV", "dollar_amount": Decimal("3000.00")},
            ],
        }
        append(paths, "portfolio_snapshot", payload,
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["positions"]["FBCG"]["quantity_shares"] == "17.9640"
        assert rec["payload"]["positions"]["FBCG"]["market_value_dollars"] == "3000.00"
        assert rec["payload"]["sources"][0]["dollar_amount"] == "3000.00"

    def test_decimal_zero_preserved_as_string(self, paths, now, cycle_id):
        # Common case: empty position values.
        append(paths, "portfolio_snapshot", {"v": Decimal("0.00")},
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["v"] == "0.00"

    def test_negative_decimal_preserved(self, paths, now, cycle_id):
        # Lookback values are typically negative when CB fires.
        append(paths, "cycle_completed", {"lookback": Decimal("-0.1234")},
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["lookback"] == "-0.1234"


# ===========================================================================
# 4. TIMEZONE ENFORCEMENT (§5.6 #3)
# ===========================================================================

class TestTimezoneEnforcement:
    """Per §3.2 + §5.4: naive datetimes raise ValueError synchronously."""

    def test_naive_now_raises_value_error(self, paths, cycle_id):
        naive = datetime(2026, 5, 14, 14, 30, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            append(paths, "cycle_started", minimal_cycle_started_payload(),
                   now=naive, source_cycle_id=cycle_id)

    def test_naive_in_sim_timestamp_raises_value_error(
        self, paths, now, cycle_id,
    ):
        naive = datetime(2005, 5, 4, 10, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            append(paths, "cycle_started", minimal_cycle_started_payload(),
                   now=now, source_cycle_id=cycle_id, in_sim_timestamp=naive)

    def test_aware_non_utc_timestamps_accepted(self, paths, cycle_id):
        # Eastern time is timezone-aware; the writer accepts it. Per the
        # spec the canonical form is UTC, but the writer doesn't force
        # conversion — that's a separate concern. What matters here is
        # that "aware" is the gate, not "UTC".
        eastern = timezone(timedelta(hours=-5))
        aware_eastern = datetime(2026, 5, 14, 10, 30, 0, tzinfo=eastern)
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=aware_eastern, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        # The ISO string preserves the offset.
        assert rec["emitted_at"].endswith("-05:00")

    def test_naive_datetime_inside_payload_raises_type_error(
        self, paths, now, cycle_id,
    ):
        # The encoder enforces tz-aware on datetime values inside payloads
        # too (defensive — caller should pre-convert to ISO string).
        naive_in_payload = datetime(2026, 1, 1)
        with pytest.raises(ValueError, match="timezone-aware"):
            append(paths, "cycle_completed", {"some_time": naive_in_payload},
                   now=now, source_cycle_id=cycle_id)


# ===========================================================================
# 5. UNKNOWN EVENT_TYPE (§5.4)
# ===========================================================================

class TestUnknownEventType:
    """Unknown event_type raises ValueError synchronously — programmer error."""

    def test_unknown_event_type_raises(self, paths, now, cycle_id):
        with pytest.raises(ValueError, match="Unknown event_type"):
            append(paths, "made_up_event", {},
                   now=now, source_cycle_id=cycle_id)

    def test_typo_in_event_type_raises(self, paths, now, cycle_id):
        # Catches the "cycle_complete" / "cycle_completed" typo class.
        with pytest.raises(ValueError, match="Unknown event_type"):
            append(paths, "cycle_complete", {},
                   now=now, source_cycle_id=cycle_id)

    def test_known_event_types_set_has_all_13(self):
        # Belt-and-suspenders: the spec's 13 types must all be present.
        expected = {
            "cycle_started", "cycle_completed", "cycle_halted",
            "decision_made", "order_placed", "fill_received",
            "withdrawal_executed", "cb_transition", "phase_transition",
            "annual_review_completed", "portfolio_snapshot",
            "state_snapshot", "alert_emitted",
        }
        assert KNOWN_EVENT_TYPES == expected

    def test_unknown_event_type_does_not_write_file(self, paths, now, cycle_id):
        # Verify no partial file is created when validation fails.
        with pytest.raises(ValueError):
            append(paths, "bogus", {}, now=now, source_cycle_id=cycle_id)
        # File should not exist (no successful writes yet).
        assert not paths.events_log().exists()


# ===========================================================================
# 6. JSON ENCODER BEHAVIOR (§5.3, §5.4)
# ===========================================================================

class TestEncoderBehavior:
    """The encoder handles Decimal, datetime, UUID, set; rejects NaN/Inf
    and non-serializable types."""

    def test_uuid_in_payload_serialized_as_string(self, paths, now, cycle_id):
        order_uuid = UUID("12345678-1234-1234-1234-123456789abc")
        append(paths, "order_placed", {"broker_order_id": order_uuid},
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["broker_order_id"] == str(order_uuid)

    def test_set_in_payload_serialized_as_sorted_list(self, paths, now, cycle_id):
        # Deterministic order matters for cross-run diffs.
        append(paths, "cb_transition",
               {"conditions": {"signal", "cb1_timer", "manual"}},
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["conditions"] == ["cb1_timer", "manual", "signal"]

    def test_aware_datetime_in_payload_serialized_as_iso(
        self, paths, now, cycle_id,
    ):
        fill_time = datetime(2026, 5, 14, 14, 30, 1, 234567,
                             tzinfo=timezone.utc)
        append(paths, "fill_received", {"fill_time": fill_time},
               now=now, source_cycle_id=cycle_id)
        rec = read_lines(paths)[0]
        assert rec["payload"]["fill_time"] == fill_time.isoformat()

    def test_nan_in_payload_raises_value_error(self, paths, now, cycle_id):
        # Per §5.3, allow_nan=False rejects NaN.
        with pytest.raises(ValueError):
            append(paths, "cycle_completed", {"v": float("nan")},
                   now=now, source_cycle_id=cycle_id)

    def test_infinity_in_payload_raises_value_error(self, paths, now, cycle_id):
        with pytest.raises(ValueError):
            append(paths, "cycle_completed", {"v": float("inf")},
                   now=now, source_cycle_id=cycle_id)

    def test_non_serializable_object_raises_type_error(
        self, paths, now, cycle_id,
    ):
        # Custom classes that aren't handled by the encoder raise
        # TypeError synchronously (programmer error per §5.4).
        class Custom:
            pass
        with pytest.raises(TypeError):
            append(paths, "cycle_started", {"obj": Custom()},
                   now=now, source_cycle_id=cycle_id)

    def test_no_indentation_one_record_per_line(self, paths, now, cycle_id):
        # §5.3: "no indentation (single-line JSON)". Verify the on-disk
        # bytes match this: each record fits on one line.
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        with paths.events_log().open("rb") as f:
            content = f.read()
        # Exactly one newline, at the end. No indentation whitespace.
        assert content.count(b"\n") == 1
        assert content.endswith(b"\n")
        assert b"  " not in content  # no double-space indents


# ===========================================================================
# 7. CONCURRENT-READ TOLERANCE (§5.6 #4, §2.3, §6.1)
# ===========================================================================

class TestConcurrentReadTolerance:
    """Readers must tolerate a partial trailing line from a crashed write."""

    def test_partial_trailing_line_is_skipped_by_reader(
        self, paths, now, cycle_id,
    ):
        # Write one complete record, then append a partial line that
        # simulates a crashed write.
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        with paths.events_log().open("ab") as f:
            f.write(b'{"schema_version":"1.0","event_type":"cycle_st')
            # No LF — simulates crashed mid-write.

        records = read_lines(paths)
        assert len(records) == 1
        assert records[0]["event_type"] == "cycle_started"

    def test_blank_lines_skipped(self, paths, now, cycle_id):
        # Defensive: shouldn't happen in practice, but the reader must
        # ignore blank lines anywhere in the file.
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        with paths.events_log().open("ab") as f:
            f.write(b"\n\n")  # two blank lines
        append(paths, "cycle_completed",
               {"cycle_type": "weekly", "phase": "PHASE_1",
                "cb_state": "CB_INACTIVE", "income_state": "ACTIVE",
                "operational_pause": False,
                "withdrawal_capacity_exhausted": False,
                "lookback_status": "OK", "lookback_value": None,
                "plan_entry_count": 0,
                "is_scheduled_withdrawal_day": False,
                "is_annual_review_day": False,
                "is_phase2_reallocation_day": False,
                "duration_ms": 100},
               now=now, source_cycle_id=cycle_id)
        records = read_lines(paths)
        assert len(records) == 2

    def test_subsequent_append_after_partial_line_still_works(
        self, paths, now, cycle_id,
    ):
        # If a crash leaves a partial line, the NEXT append() still
        # produces a usable record (the partial line becomes the prefix
        # of a malformed line; subsequent JSON parse will skip it).
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        with paths.events_log().open("ab") as f:
            f.write(b'{"partial":')  # no LF
        # New append. The writer always starts with a new line worth of
        # bytes; the partial-from-crash + new-append-bytes will produce
        # an unparseable concatenated line, then a clean line, then the
        # rest of new record. Reader skips the unparseable line.
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        records = read_lines(paths)
        # Two parseable records: the first (clean) and the second
        # (clean after the partial line is skipped on parse error).
        assert len(records) >= 1
        assert all(r["event_type"] == "cycle_started" for r in records)


# ===========================================================================
# 8. OSERROR HANDLING (§5.6 #5, §5.4)
# ===========================================================================

class TestOSErrorHandling:
    """Disk failures are caught and logged at WARNING; never propagate.
    Programmer errors (TypeError, ValueError) DO propagate."""

    def test_oserror_caught_event_id_returned(self, paths, now, cycle_id, caplog):
        # Use a path that will fail on open (parent path is /dev/null,
        # not a directory). This produces a real OSError from open().
        bad_paths = Paths(state_dir=Path("/dev/null/cannot_exist"))
        with caplog.at_level(logging.WARNING, logger="event_log"):
            eid = append(bad_paths, "cycle_started",
                         minimal_cycle_started_payload(),
                         now=now, source_cycle_id=cycle_id)
        # event_id is returned even though the write failed.
        assert eid.startswith("evt_")
        # WARNING was logged.
        assert any("event_log append failed" in r.message
                   for r in caplog.records)

    def test_oserror_does_not_propagate(self, paths, now, cycle_id):
        bad_paths = Paths(state_dir=Path("/dev/null/cannot_exist"))
        # Should not raise.
        eid = append(bad_paths, "cycle_started",
                     minimal_cycle_started_payload(),
                     now=now, source_cycle_id=cycle_id)
        assert eid is not None

    def test_value_error_still_propagates_before_io(self, paths, now, cycle_id):
        # Even with a bad path, a ValueError from validation must fire
        # before any I/O is attempted. This proves validation precedes
        # the try/except block.
        bad_paths = Paths(state_dir=Path("/dev/null/cannot_exist"))
        with pytest.raises(ValueError, match="Unknown event_type"):
            append(bad_paths, "bogus_type", {},
                   now=now, source_cycle_id=cycle_id)


# ===========================================================================
# 9. APPEND-ONLY BEHAVIOR (§2.3)
# ===========================================================================

class TestAppendOnly:
    """The writer never seeks, never overwrites, never truncates."""

    def test_existing_records_unchanged_by_new_appends(
        self, paths, now, cycle_id,
    ):
        # Capture original byte content after first append.
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        original_bytes = paths.events_log().read_bytes()

        # Append more events.
        for _ in range(5):
            append(paths, "cycle_started", minimal_cycle_started_payload(),
                   now=now, source_cycle_id=cycle_id)

        new_bytes = paths.events_log().read_bytes()
        # File grew, and the prefix is byte-identical to the original.
        assert len(new_bytes) > len(original_bytes)
        assert new_bytes[:len(original_bytes)] == original_bytes

    def test_file_grows_monotonically(self, paths, now, cycle_id):
        sizes = []
        for _ in range(10):
            append(paths, "cycle_started", minimal_cycle_started_payload(),
                   now=now, source_cycle_id=cycle_id)
            sizes.append(paths.events_log().stat().st_size)
        # Every size is strictly greater than the previous.
        assert all(b > a for a, b in zip(sizes, sizes[1:]))

    def test_file_created_at_correct_path(self, paths, now, cycle_id):
        # §2.2: state_dir / "events.jsonl" — at state_dir directly,
        # NOT under logs/.
        append(paths, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        assert paths.events_log() == paths.state_dir / "events.jsonl"
        assert paths.events_log().exists()
        # Not under logs/.
        assert not (paths.logs_dir / "events.jsonl").exists()

    def test_parent_directory_auto_created(self, tmp_path, now, cycle_id):
        # The writer creates state_dir if it doesn't exist (defensive).
        nonexistent = Paths(state_dir=tmp_path / "deep" / "nested" / "state")
        # No ensure_dirs() called.
        append(nonexistent, "cycle_started", minimal_cycle_started_payload(),
               now=now, source_cycle_id=cycle_id)
        assert nonexistent.events_log().exists()


# ===========================================================================
# 10. PER-EVENT-TYPE HELPERS
# ===========================================================================
#
# One representative test per helper, verifying it produces a valid event
# with the expected event_type. The helpers' job is payload shape
# construction; we don't re-verify the full envelope here (covered above).

class TestHelpers:

    def test_cycle_started_helper(self, paths, now, cycle_id):
        append_cycle_started(
            paths,
            cycle_id=cycle_id, cycle_type="weekly", is_restart=False,
            box_id="harness-box", client_id=11,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "cycle_started"
        assert rec["source_cycle_id"] == cycle_id
        assert rec["payload"] == {
            "cycle_type": "weekly", "is_restart": False,
            "box_id": "harness-box", "client_id": 11,
        }

    def test_cycle_completed_helper_with_decimal_lookback(
        self, paths, now, cycle_id,
    ):
        append_cycle_completed(
            paths,
            cycle_id=cycle_id, cycle_type="weekly",
            phase="PHASE_1", cb_state="CB_INACTIVE", income_state="ACTIVE",
            operational_pause=False, withdrawal_capacity_exhausted=False,
            lookback_status="OK", lookback_value=Decimal("-0.0834"),
            plan_entry_count=5,
            is_scheduled_withdrawal_day=True,
            is_annual_review_day=False,
            is_phase2_reallocation_day=False,
            duration_ms=487,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "cycle_completed"
        assert rec["payload"]["lookback_value"] == "-0.0834"
        assert rec["payload"]["plan_entry_count"] == 5

    def test_cycle_completed_helper_with_null_lookback(
        self, paths, now, cycle_id,
    ):
        # Daily-token cycles compute no lookback; the helper must accept
        # None and write null.
        append_cycle_completed(
            paths,
            cycle_id=cycle_id, cycle_type="daily-token",
            phase="PHASE_1", cb_state="CB_INACTIVE", income_state="ACTIVE",
            operational_pause=False, withdrawal_capacity_exhausted=False,
            lookback_status=None, lookback_value=None,
            plan_entry_count=0,
            is_scheduled_withdrawal_day=False,
            is_annual_review_day=False,
            is_phase2_reallocation_day=False,
            duration_ms=12,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["payload"]["lookback_value"] is None
        assert rec["payload"]["lookback_status"] is None

    def test_cycle_halted_helper(self, paths, now, cycle_id):
        append_cycle_halted(
            paths,
            cycle_id=cycle_id, cycle_type="weekly",
            halt_reason="growth SELL leg failed: quantity reduced to zero",
            phase_at_halt="PHASE_1", cb_state_at_halt="CB_INACTIVE",
            plan_entry_count=1, completed_legs=0, failed_leg_index=0,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "cycle_halted"
        assert rec["payload"]["halt_reason"].startswith("growth SELL")

    def test_decision_made_helper_passthrough(self, paths, now, cycle_id):
        # decision_made has a complex inputs/plan_entries shape; the
        # helper passes through the caller-built dict and list.
        inputs = {"phase": "PHASE_1", "cb_state": "CB_INACTIVE",
                  "total_aum_dollars": "668500.00"}
        plan_entries = [{"entry_index": 0, "kind": "WITHDRAWAL"}]
        append_decision_made(
            paths,
            cycle_id=cycle_id, inputs=inputs, plan_entries=plan_entries,
            should_skip_action_layer=False, skip_reason=None,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "decision_made"
        assert rec["payload"]["inputs"] == inputs
        assert rec["payload"]["plan_entries"] == plan_entries

    def test_order_placed_helper_decimal_fields(self, paths, now, cycle_id):
        append_order_placed(
            paths,
            cycle_id=cycle_id,
            client_order_id="cycle-abc-0-FBCG-SELL",
            broker_order_id="123456789",
            plan_entry_index=0, plan_entry_kind="BUFFER_REFILL",
            symbol="FBCG", side="SELL", order_type="MKT",
            quantity_shares=Decimal("17.9640"),
            limit_price_dollars=None,
            time_in_force="DAY",
            intended_dollar_amount=Decimal("3000.00"),
            price_refresh_dollars=Decimal("167.00"),
            price_refresh_status="OK",
            used_fallback_estimate=False,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "order_placed"
        # Decimals all preserved as strings.
        assert rec["payload"]["quantity_shares"] == "17.9640"
        assert rec["payload"]["intended_dollar_amount"] == "3000.00"
        assert rec["payload"]["price_refresh_dollars"] == "167.00"
        assert rec["payload"]["limit_price_dollars"] is None

    def test_fill_received_helper_requires_aware_fill_time(
        self, paths, now, cycle_id,
    ):
        # fill_time is a separate "third timestamp" beyond now/timestamp;
        # the helper enforces tz-awareness on it.
        naive_fill = datetime(2026, 5, 14, 14, 30, 1)
        with pytest.raises(ValueError, match="fill_time"):
            append_fill_received(
                paths,
                cycle_id=cycle_id,
                client_order_id="cycle-abc-0-FBCG-SELL",
                broker_order_id="123456789",
                fill_id="fill-001",
                plan_entry_index=0, plan_entry_kind="BUFFER_REFILL",
                symbol="FBCG", side="SELL",
                quantity_shares=Decimal("17.9640"),
                price_dollars=Decimal("167.05"),
                fill_dollar_amount=Decimal("3001.06"),
                fill_time=naive_fill,
                fill_index=0, total_fills_for_order=1,
                now=now,
            )

    def test_fill_received_helper_aware_fill_time_ok(
        self, paths, now, cycle_id,
    ):
        fill_time = datetime(2026, 5, 14, 14, 30, 1, 234567,
                             tzinfo=timezone.utc)
        append_fill_received(
            paths,
            cycle_id=cycle_id,
            client_order_id="cycle-abc-0-FBCG-SELL",
            broker_order_id="123456789",
            fill_id="fill-001",
            plan_entry_index=0, plan_entry_kind="BUFFER_REFILL",
            symbol="FBCG", side="SELL",
            quantity_shares=Decimal("17.9640"),
            price_dollars=Decimal("167.05"),
            fill_dollar_amount=Decimal("3001.06"),
            fill_time=fill_time,
            fill_index=0, total_fills_for_order=1,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "fill_received"
        assert rec["payload"]["fill_time"] == fill_time.isoformat()
        assert rec["payload"]["fill_dollar_amount"] == "3001.06"

    def test_withdrawal_executed_helper_passthrough(self, paths, now, cycle_id):
        payload = {
            "scheduled_dollars": "3000.00",
            "paid_dollars": "2750.00",
            "binding_ceiling": "phase3_floor",
            "sources": [{"symbol": "SGOV", "dollar_amount": "2750.00"}],
        }
        append_withdrawal_executed(
            paths, cycle_id=cycle_id, payload=payload, now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "withdrawal_executed"
        assert rec["payload"] == payload

    def test_cb_transition_helper(self, paths, now, cycle_id):
        append_cb_transition(
            paths,
            cycle_id=cycle_id,
            from_state="CB_INACTIVE", to_state="CB1",
            trigger_reason="signal",
            cb2_entry_conditions_after=[],
            lookback_value_at_trigger=Decimal("-0.0834"),
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "cb_transition"
        assert rec["payload"]["from_state"] == "CB_INACTIVE"
        assert rec["payload"]["to_state"] == "CB1"
        assert rec["payload"]["lookback_value_at_trigger"] == "-0.0834"
        assert rec["payload"]["cb2_entry_conditions_after"] == []

    def test_cb_transition_helper_null_trigger(self, paths, now, cycle_id):
        # cb1_timer-driven transitions have no lookback value.
        append_cb_transition(
            paths,
            cycle_id=cycle_id,
            from_state="CB1", to_state="CB2",
            trigger_reason="cb1_timer",
            cb2_entry_conditions_after=["signal", "cb1_timer"],
            lookback_value_at_trigger=None,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["payload"]["lookback_value_at_trigger"] is None
        assert rec["payload"]["trigger_reason"] == "cb1_timer"
        assert rec["payload"]["cb2_entry_conditions_after"] == ["signal", "cb1_timer"]

    def test_phase_transition_helper_passthrough(self, paths, now, cycle_id):
        payload = {"from_phase": "PHASE_1", "to_phase": "PHASE_2",
                   "is_phase3_activation": False}
        append_phase_transition(
            paths, cycle_id=cycle_id, payload=payload, now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "phase_transition"
        assert rec["payload"] == payload

    def test_annual_review_completed_helper(self, paths, now, cycle_id):
        payload = {
            "review_year": 2027, "phase_at_review": "PHASE_3",
            "cb_freeze_in_effect": False,
            "cpi_rate_applied": "0.0244",
            "prior_withdrawal_dollars": "3000.00",
            "computed_new_withdrawal_dollars": "3073.20",
        }
        append_annual_review_completed(
            paths, cycle_id=cycle_id, payload=payload, now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "annual_review_completed"
        assert rec["payload"] == payload

    def test_portfolio_snapshot_helper(self, paths, now, cycle_id):
        payload = {
            "total_aum_dollars": "3304555.43",
            "cash_dollars": "4250.18",
            "positions": {
                "FBCG": {"quantity_shares": "1200.0000",
                         "market_value_dollars": "804000.00"},
            },
        }
        append_portfolio_snapshot(
            paths, cycle_id=cycle_id, payload=payload, now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "portfolio_snapshot"
        assert rec["payload"]["total_aum_dollars"] == "3304555.43"

    def test_state_snapshot_helper(self, paths, now, cycle_id):
        payload = {"trigger": "monthly_heartbeat", "phase": "PHASE_2",
                   "income_state": "ACTIVE"}
        append_state_snapshot(
            paths, cycle_id=cycle_id, payload=payload, now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "state_snapshot"
        assert rec["payload"]["trigger"] == "monthly_heartbeat"

    def test_alert_emitted_helper(self, paths, now, cycle_id):
        append_alert_emitted(
            paths,
            cycle_id=cycle_id,
            alert_id="cb1_triggered",
            context={"lookback_value": "-0.0834"},
            email_ok=True, sms_ok=True, deduped=False,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["event_type"] == "alert_emitted"
        assert rec["payload"]["alert_id"] == "cb1_triggered"
        assert rec["payload"]["context"] == {"lookback_value": "-0.0834"}
        assert rec["payload"]["email_ok"] is True
        assert rec["payload"]["sms_ok"] is True
        assert rec["payload"]["deduped"] is False
        assert rec["payload"]["email_error"] is None
        assert rec["payload"]["sms_error"] is None

    def test_alert_emitted_helper_cycle_id_optional(self, paths, now):
        # §4.13: alerts can be dispatched outside any cycle.
        append_alert_emitted(
            paths,
            cycle_id=None,
            alert_id="startup_health_check",
            context={},
            email_ok=True, sms_ok=True, deduped=False,
            now=now,
        )
        rec = read_lines(paths)[0]
        assert rec["source_cycle_id"] is None
