"""
report.py — IRAPM reporter (REPORT_SPEC.md).

PURPOSE:
    Consume events.jsonl (the IRAPM event log) and produce three kinds
    of human-readable text report:

      - current_status.txt    operator's "what's IRAPM doing right now"
      - {YYYY}.txt            per-year historical record with running
                              annual summary, rebuilt monthly
      - report_*.txt          simulator output for one scenario run

    The reporter is the operator's primary interface to IRAPM. Routine
    monitoring happens by reading these files; raw event-log inspection
    is a rare forensic activity. The reporter is therefore load-bearing
    operator-facing infrastructure.

DESIGN (REPORT_SPEC §1, §2):
    - Pure consumer of events.jsonl via event_log.iter_events. No
      reading of in-memory IRAPM state, no calls into cycle.py,
      action_layer.py, decision_layer.py, etc.
    - One IRAPM module exception: ruleset_model.Ruleset is read by
      write_simulation_report for the RULESET section (§3.3.4 of the
      spec). Read-only access to one Pydantic model.
    - Stateless. Each invocation reads events.jsonl, builds the output
      string in memory, writes via temp-file-and-rename. No caches,
      no indices, no persistent state.
    - Performance: load events.jsonl ONCE per public-function call
      via _load_events_indexed(), then filter the resulting in-memory
      index for every internal helper. This is materially faster than
      the prior implementation which re-walked the JSONL file once
      per helper call (witnessed: 240 monthly rows × ~6 helpers each
      = 1,400+ full-file passes for one simulation report).
    - Failure isolation. Callers wrap invocations in try/except so a
      reporter failure cannot break a cycle.

VOCABULARY (REPORT_SPEC §1.4):
    - G (Guardrail)  — Phase 3 portfolio-percent withdrawal clamp.
                       Translated from `binding_ceiling: "guardrail"`.
    - C (Cap)        — Phase 3 inflation-indexed dollar ceiling.
                       Translated from `binding_ceiling: "dollar_cap"`.
    - F (Freeze)     — CPI-increase freeze fired by prior-year CB activity.
                       Translated from `binding_constraint: "cpi_freeze"`.

KNOWN OPEN ITEMS:
    - cash_in dividend distinguishing (REPORT_SPEC §10.1, marked
      TODO[DRIP] in the code). Depends on operator's DRIP-vs-Sweep
      decision and possibly a future fill_source field on fill_received.
"""

from __future__ import annotations

import calendar
import logging
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from event_log import iter_events
from persistence import Paths

logger = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

COLUMN_ORDER: tuple[str, ...] = (
    "date", "cb", "gross_net_liq", "cash_buff", "sgov_buffer",
    "fi_bucket", "growth_bucket", "fi_wt", "gr_wt",
    "cash_in", "cash_out",
    "pyld", "jpie", "fbcg", "avuv", "gbil",
    "phase",
)

COLUMN_META: dict[str, dict[str, Any]] = {
    "date":          {"align": "L", "kind": "str"},
    "cb":            {"align": "L", "kind": "str"},
    "gross_net_liq": {"align": "R", "kind": "dollar"},
    "cash_buff":     {"align": "R", "kind": "dollar"},
    "sgov_buffer":   {"align": "R", "kind": "dollar"},
    "fi_bucket":     {"align": "R", "kind": "dollar"},
    "growth_bucket": {"align": "R", "kind": "dollar"},
    "fi_wt":         {"align": "R", "kind": "weight"},
    "gr_wt":         {"align": "R", "kind": "weight"},
    "cash_in":       {"align": "R", "kind": "dollar"},
    "cash_out":      {"align": "R", "kind": "dollar_with_suffix"},
    "pyld":          {"align": "R", "kind": "dollar"},
    "jpie":          {"align": "R", "kind": "dollar"},
    "fbcg":          {"align": "R", "kind": "dollar"},
    "avuv":          {"align": "R", "kind": "dollar"},
    "gbil":          {"align": "R", "kind": "dollar"},
    "phase":         {"align": "L", "kind": "str"},
}

COLUMN_SEPARATOR = "  "

RULE_NARROW = "=" * 65
RULE_WIDE = "=" * 80

CAP_SYMBOL_FROM_BINDING: dict[str, str] = {
    "guardrail": "G",
    "dollar_cap": "C",
}


# ============================================================================
# Event index: load once per public-function call, query in-memory
# ============================================================================

@dataclass
class EventIndex:
    """In-memory index of the event log, built once per public-function call.

    Holds:
      - all_events: complete list in append order
      - by_type:    dict mapping event_type -> list of events of that type
                    (preserves append order within each type)

    Internal helpers filter against by_type rather than re-iterating
    the file. At our scales (10-50 MB events.jsonl, 50K-200K events)
    this fits comfortably in memory and turns ~250 file passes into
    one pass plus a handful of list traversals.
    """
    all_events: list[dict]
    by_type: dict[str, list[dict]]


def _load_events_indexed(paths: Paths) -> EventIndex:
    """Load the entire event log and bucket by event_type."""
    all_events = list(iter_events(paths))
    by_type: dict[str, list[dict]] = defaultdict(list)
    for event in all_events:
        et = event.get("event_type")
        if et:
            by_type[et].append(event)
    return EventIndex(all_events=all_events, by_type=dict(by_type))


# ============================================================================
# Data classes for row construction
# ============================================================================

@dataclass
class DataRow:
    """One row of the standard column set, ready for rendering.

    Fields hold either rendered strings or sentinel `None` for blank
    cells. Cap symbols are pre-attached to cash_out for column-width
    calculation purposes (REPORT_SPEC §6.2).

    Annotations is the unrendered list of (event_code, date_str,
    suffix_flag) tuples for this row's period.
    """
    date_str: str
    cb_str: str
    gross_net_liq_str: Optional[str]
    cash_buff_str: Optional[str]
    sgov_buffer_str: Optional[str]
    fi_bucket_str: Optional[str]
    growth_bucket_str: Optional[str]
    fi_wt_str: Optional[str]
    gr_wt_str: Optional[str]
    cash_in_str: Optional[str]
    cash_out_str: Optional[str]
    pyld_str: Optional[str]
    jpie_str: Optional[str]
    fbcg_str: Optional[str]
    avuv_str: Optional[str]
    gbil_str: Optional[str]
    phase_str: str
    annotations: list[tuple[str, str, Optional[str]]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Optional[str]]:
        return {
            "date": self.date_str,
            "cb": self.cb_str,
            "gross_net_liq": self.gross_net_liq_str,
            "cash_buff": self.cash_buff_str,
            "sgov_buffer": self.sgov_buffer_str,
            "fi_bucket": self.fi_bucket_str,
            "growth_bucket": self.growth_bucket_str,
            "fi_wt": self.fi_wt_str,
            "gr_wt": self.gr_wt_str,
            "cash_in": self.cash_in_str,
            "cash_out": self.cash_out_str,
            "pyld": self.pyld_str,
            "jpie": self.jpie_str,
            "fbcg": self.fbcg_str,
            "avuv": self.avuv_str,
            "gbil": self.gbil_str,
            "phase": self.phase_str,
        }


# ============================================================================
# Numeric/string rendering helpers (REPORT_SPEC §6.1)
# ============================================================================

def _fmt_dollar(value: Decimal) -> str:
    return f"{value:,.2f}"


def _fmt_weight(value: Decimal) -> str:
    return f"{value:.2f}"


def _fmt_signed_pct(value: Decimal) -> str:
    """Signed percentage with 2 dp, % suffix. E.g. '+7.87%' / '-12.30%'."""
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.2f}%"


def _parse_decimal(s: Optional[str]) -> Optional[Decimal]:
    if s is None:
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _parse_iso_datetime(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string, returning a tz-aware UTC datetime.

    The IRAPM event log writes top-level event timestamps with an explicit
    UTC suffix (e.g. "2025-04-09T04:00:00+00:00"), but state_snapshot
    payloads contain nested datetime fields written as naive strings
    (e.g. cb1_active_timer_started_at = "2025-04-09T00:00:00"). The whole
    system semantically treats all stored times as UTC, so we normalize
    naive inputs to UTC-aware here rather than at every call site.

    This eliminates a class of "can't subtract offset-naive and
    offset-aware datetimes" TypeError bugs that would otherwise surface
    anywhere in the reporter that does datetime arithmetic mixing event
    timestamps with state-snapshot payload datetimes.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _render_cb_short(state: str) -> str:
    """Translate the long-form cb_state from the event log to column display.

    EVENT_LOG_SPEC §4.2: CB_INACTIVE / CB1 / CB2 / CB1_RECOVERY_STAGE* /
    CB2_RECOVERY_STAGE*. The reporter compresses to one of: '-', '1',
    '2'. The '1+2' value (both entry conditions active) is determined
    by inspecting cb_machine.cb2_entry_conditions; this function alone
    cannot produce '1+2' from a single state string. Callers needing
    '1+2' detection should examine the state_snapshot payload directly.
    """
    if state == "CB_INACTIVE":
        return "-"
    if state.startswith("CB1"):
        return "1"
    if state.startswith("CB2"):
        return "2"
    return "-"


# ============================================================================
# In-memory filtering helpers
# ============================================================================

def _filter_in_window(
    events: list[dict],
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Return events with timestamp in [start, end) — half-open."""
    result: list[dict] = []
    for event in events:
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        if start <= ts < end:
            result.append(event)
    return result


def _events_of_type(index: EventIndex, event_type: str) -> list[dict]:
    return index.by_type.get(event_type, [])


def _events_of_type_in_window(
    index: EventIndex,
    event_type: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    return _filter_in_window(_events_of_type(index, event_type), start, end)


def _latest_event_of_type(index: EventIndex, event_type: str) -> Optional[dict]:
    events = _events_of_type(index, event_type)
    return events[-1] if events else None


def _first_snapshot_in_window(
    index: EventIndex,
    start: datetime,
    end: datetime,
) -> Optional[dict]:
    snaps = _events_of_type_in_window(index, "portfolio_snapshot", start, end)
    return snaps[0] if snaps else None


def _last_snapshot_in_window(
    index: EventIndex,
    start: datetime,
    end: datetime,
) -> Optional[dict]:
    snaps = _events_of_type_in_window(index, "portfolio_snapshot", start, end)
    return snaps[-1] if snaps else None


def _latest_state_snapshot_before(
    index: EventIndex,
    end: datetime,
) -> Optional[dict]:
    """Latest state_snapshot with timestamp < end."""
    last: Optional[dict] = None
    for event in _events_of_type(index, "state_snapshot"):
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        if ts < end:
            last = event
        else:
            break
    return last


# ============================================================================
# Aggregations
# ============================================================================

def _aggregate_cash_out(withdrawal_events: list[dict]) -> tuple[Decimal, str]:
    """Sum amount_paid_dollars; concatenate cap-symbol suffix (alphabetical)."""
    total = Decimal("0")
    symbols: set[str] = set()
    for event in withdrawal_events:
        payload = event.get("payload", {})
        amount = _parse_decimal(payload.get("amount_paid_dollars")) or Decimal("0")
        total += amount
        if payload.get("was_capped"):
            binding = payload.get("binding_ceiling")
            sym = CAP_SYMBOL_FROM_BINDING.get(binding)
            if sym:
                symbols.add(sym)
    suffix = "".join(sorted(symbols))
    return total, suffix


def _aggregate_cash_in(fill_events: list[dict]) -> Decimal:
    """Sum the cash_in column total for a period.

    TODO[DRIP]: deferred per REPORT_SPEC §10.1. Returns 0.00 until
    DRIP-vs-Sweep decision and (if Sweep) fill_source field land.
    """
    return Decimal("0")


# ============================================================================
# Annotation extraction (REPORT_SPEC §5)
# ============================================================================

def _extract_annotations_from_index(
    index: EventIndex,
    start: datetime,
    end: datetime,
    snapshot_in_period: Optional[dict],
    granularity: str = "monthly",
) -> list[tuple[str, str, Optional[str]]]:
    """Walk events in [start, end) and produce the annotation list.

    granularity controls the filter for routine per-cycle events:
      - "monthly": include W (and RFL, and all summary markers). This
        is the default and matches the granularity of monthly rows in
        year files and the sim report's MONTHLY block.
      - "annual": suppress W (per-cycle withdrawal noise; the count is
        already visible in the row's cash_out total and the annual
        summary block). RFL stays because a buffer refill outside its
        usual cadence is signal worth seeing at annual scope.
        All summary markers (AR, CB1, CB2, REC, P2, P3, DPD, DPR,
        HALT, PAUSE) are preserved at every granularity.

    DPD / DPR are detected from alert_emitted events with the
    `phase2_opportunistic_deploy` / `phase2_opportunistic_recover`
    alert_id values. The underlying mechanism emits no dedicated
    structured event type — the alert is the canonical record. If a
    future Phase 1 writer change adds a dedicated event_type, this
    detection should switch to that.
    """
    annotations: list[tuple[str, datetime, Optional[str]]] = []

    if snapshot_in_period is None:
        annotations.append(("GAP", start, None))

    window_events = _filter_in_window(index.all_events, start, end)

    for event in window_events:
        et = event.get("event_type")
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        payload = event.get("payload", {})

        if et == "annual_review_completed":
            suffix = None
            if payload.get("binding_constraint") == "cpi_freeze":
                suffix = "F"
            annotations.append(("AR", ts, suffix))
        elif et == "cb_transition":
            from_state = payload.get("from_state", "")
            to_state = payload.get("to_state", "")
            if to_state == "CB1" and from_state == "CB_INACTIVE":
                annotations.append(("CB1", ts, None))
            elif to_state == "CB2":
                annotations.append(("CB2", ts, None))
            elif to_state == "CB_INACTIVE" and from_state != "CB_INACTIVE":
                annotations.append(("REC", ts, None))
        elif et == "withdrawal_executed":
            # Per-cycle event; suppress at annual scope where the
            # withdrawal count is already in the cash_out aggregate and
            # the annual summary block. Keep at monthly so individual
            # withdrawal dates are visible.
            if granularity != "annual":
                annotations.append(("W", ts, None))
        elif et == "phase_transition":
            to_phase = payload.get("to_phase", "")
            if payload.get("is_phase3_activation"):
                annotations.append(("P3", ts, None))
            elif to_phase == "PHASE_2":
                annotations.append(("P2", ts, None))
        elif et == "cycle_halted":
            annotations.append(("HALT", ts, None))
        elif et == "alert_emitted":
            alert_id = payload.get("alert_id")
            if alert_id == "operational_pause":
                annotations.append(("PAUSE", ts, None))
            elif alert_id == "phase2_opportunistic_deploy":
                annotations.append(("DPD", ts, None))
            elif alert_id == "phase2_opportunistic_recover":
                annotations.append(("DPR", ts, None))
        elif et == "decision_made":
            plan_entries = payload.get("plan_entries", [])
            if any(e.get("kind") == "BUFFER_REFILL" for e in plan_entries):
                annotations.append(("RFL", ts, None))

    annotations.sort(key=lambda t: t[1])
    return [(code, ts.date().strftime("%Y-%m-%d"), suffix)
            for (code, ts, suffix) in annotations]


# ============================================================================
# Row construction
# ============================================================================

def _build_row_from_snapshot(
    row_date: date,
    snapshot_event: Optional[dict],
    cb_state_at_row: str,
    phase_at_row: str,
    cash_in_total: Decimal,
    cash_out_total: Decimal,
    cash_out_suffix: str,
    annotations: list[tuple[str, str, Optional[str]]],
) -> DataRow:
    """Construct a DataRow from a portfolio_snapshot event plus aggregates."""
    date_str = row_date.strftime("%Y-%m-%d")

    if snapshot_event is None:
        return DataRow(
            date_str=date_str,
            cb_str=cb_state_at_row,
            gross_net_liq_str=None,
            cash_buff_str=None,
            sgov_buffer_str=None,
            fi_bucket_str=None,
            growth_bucket_str=None,
            fi_wt_str=None,
            gr_wt_str=None,
            cash_in_str=None,
            cash_out_str=None,
            pyld_str=None,
            jpie_str=None,
            fbcg_str=None,
            avuv_str=None,
            gbil_str=None,
            phase_str=phase_at_row,
            annotations=annotations,
        )

    payload = snapshot_event.get("payload", {})
    positions = payload.get("positions", {})

    def _pos_value(symbol: str) -> Decimal:
        p = positions.get(symbol, {})
        return _parse_decimal(p.get("market_value_dollars")) or Decimal("0")

    pyld = _pos_value("PYLD")
    jpie = _pos_value("JPIE")
    fbcg = _pos_value("FBCG")
    avuv = _pos_value("AVUV")
    gbil = _pos_value("GBIL")

    fi_bucket = pyld + jpie + gbil
    growth_bucket = fbcg + avuv

    total_aum = _parse_decimal(payload.get("total_aum_dollars")) or Decimal("0")
    cash_buff = _parse_decimal(payload.get("cash_dollars")) or Decimal("0")
    sgov_buffer = _parse_decimal(payload.get("sgov_buffer_dollars")) or Decimal("0")

    investable_aum = total_aum - sgov_buffer - cash_buff
    if investable_aum > 0:
        fi_wt = fi_bucket / investable_aum
        gr_wt = growth_bucket / investable_aum
    else:
        fi_wt = Decimal("0")
        gr_wt = Decimal("0")

    cash_out_str = _fmt_dollar(cash_out_total) + cash_out_suffix

    return DataRow(
        date_str=date_str,
        cb_str=cb_state_at_row,
        gross_net_liq_str=_fmt_dollar(total_aum),
        cash_buff_str=_fmt_dollar(cash_buff),
        sgov_buffer_str=_fmt_dollar(sgov_buffer),
        fi_bucket_str=_fmt_dollar(fi_bucket),
        growth_bucket_str=_fmt_dollar(growth_bucket),
        fi_wt_str=_fmt_weight(fi_wt),
        gr_wt_str=_fmt_weight(gr_wt),
        cash_in_str=_fmt_dollar(cash_in_total),
        cash_out_str=cash_out_str,
        pyld_str=_fmt_dollar(pyld),
        jpie_str=_fmt_dollar(jpie),
        fbcg_str=_fmt_dollar(fbcg),
        avuv_str=_fmt_dollar(avuv),
        gbil_str=_fmt_dollar(gbil),
        phase_str=phase_at_row,
        annotations=annotations,
    )


# ============================================================================
# Table rendering (REPORT_SPEC §6)
# ============================================================================

def _compute_column_widths(
    rows: Iterable[DataRow],
    columns: tuple[str, ...] = COLUMN_ORDER,
) -> dict[str, int]:
    widths: dict[str, int] = {col: len(col) for col in columns}
    for row in rows:
        cells = row.as_dict()
        for col in columns:
            value = cells.get(col)
            if value is None:
                continue
            widths[col] = max(widths[col], len(value))
    return widths


def _render_header_line(widths: dict[str, int],
                        columns: tuple[str, ...] = COLUMN_ORDER) -> str:
    parts = [col.ljust(widths[col]) for col in columns]
    return COLUMN_SEPARATOR.join(parts).rstrip()


def _render_underline(widths: dict[str, int],
                      columns: tuple[str, ...] = COLUMN_ORDER) -> str:
    parts = ["-" * widths[col] for col in columns]
    return COLUMN_SEPARATOR.join(parts)


def _render_data_row(row: DataRow, widths: dict[str, int],
                     columns: tuple[str, ...] = COLUMN_ORDER) -> str:
    cells = row.as_dict()
    parts: list[str] = []
    for col in columns:
        value = cells.get(col)
        if value is None:
            value = ""
        align = COLUMN_META[col]["align"]
        if align == "R":
            parts.append(value.rjust(widths[col]))
        else:
            parts.append(value.ljust(widths[col]))
    return COLUMN_SEPARATOR.join(parts).rstrip()


def _render_annotation_line(row: DataRow, widths: dict[str, int]) -> Optional[str]:
    if not row.annotations:
        return None
    indent = " " * widths["date"]
    parts: list[str] = []
    for code, date_str, suffix in row.annotations:
        # GAP is a structural marker (no associated event date) per
        # REPORT_SPEC §5.2. Other annotations render as CODE:YYYY-MM-DD.
        if code == "GAP":
            parts.append("GAP")
            continue
        s = f"{code}:{date_str}"
        if suffix:
            s = f"{s} ({suffix})"
        parts.append(s)
    return f"{indent}{COLUMN_SEPARATOR}└─ " + ", ".join(parts)


def _render_table(rows: list[DataRow]) -> str:
    if not rows:
        return "(no data)"
    widths = _compute_column_widths(rows)
    return _render_table_with_widths(rows, widths)


def _render_table_with_widths(rows: list[DataRow],
                              widths: dict[str, int]) -> str:
    if not rows:
        return "(no data)"
    out_lines: list[str] = []
    out_lines.append(_render_header_line(widths))
    out_lines.append(_render_underline(widths))
    for row in rows:
        out_lines.append(_render_data_row(row, widths))
        ann_line = _render_annotation_line(row, widths)
        if ann_line:
            out_lines.append(ann_line)
    return "\n".join(out_lines)


# ============================================================================
# Atomic write helper (REPORT_SPEC §2.3)
# ============================================================================

def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=False,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# Period math
# ============================================================================

def _month_window_utc(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _year_window_utc(year: int) -> tuple[datetime, datetime]:
    return (
        datetime(year, 1, 1, tzinfo=timezone.utc),
        datetime(year + 1, 1, 1, tzinfo=timezone.utc),
    )


def _last_day_of_month(year: int, month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


# ============================================================================
# Days-in-CB1+ (REPORT_SPEC §3.3.3, §3.2.4)
# ============================================================================

def _compute_days_in_cb1plus(
    index: EventIndex,
    period_start: datetime,
    period_end: datetime,
) -> int:
    """Sum days in any non-INACTIVE CB state during the period."""
    state_at_start = "CB_INACTIVE"
    state_changes: list[tuple[datetime, str]] = []

    for event in _events_of_type(index, "cb_transition"):
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        to_state = event.get("payload", {}).get("to_state", "CB_INACTIVE")
        if ts < period_start:
            state_at_start = to_state
        elif ts < period_end:
            state_changes.append((ts, to_state))
        else:
            break

    current_state = state_at_start
    current_ts = period_start
    total_cb_seconds = 0.0

    for change_ts, new_state in state_changes:
        if current_state != "CB_INACTIVE":
            total_cb_seconds += (change_ts - current_ts).total_seconds()
        current_state = new_state
        current_ts = change_ts

    if current_state != "CB_INACTIVE":
        total_cb_seconds += (period_end - current_ts).total_seconds()

    return int(total_cb_seconds // 86400)


# ============================================================================
# Public API: write_current_status (REPORT_SPEC §3.1)
# ============================================================================

def _anchor_now(index: EventIndex, wall_clock_now: datetime) -> datetime:
    """Return the effective "now" anchor for current_status rendering.

    PURPOSE:
    For a live production system, "now" is wall-clock. For a historical
    or simulation event log (the latest event is days/weeks/years in
    the past), wall-clock yields useless output: "Next scheduled
    withdrawal" anchored to today is meaningless, and "Recent alerts in
    last 4 weeks" matches nothing.

    HEURISTIC:
    If the latest event in the log is more than 7 days behind wall-clock,
    the log is treated as historical and the anchor returns the latest
    event timestamp. Otherwise (a live or recently-active system),
    return wall_clock_now.

    7 days is wide enough to absorb cycle-cadence jitter (a missed
    Wednesday) plus a multi-day outage, and short enough that any
    simulation or backtest will be safely on the historical side. There
    is no overlap region: "slightly historical" is not a state we care
    about distinguishing from "live but stale" — in both cases the
    operator sees the same anchor.
    """
    latest_ts: Optional[datetime] = None
    for event in index.all_events:
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
    if latest_ts is None:
        return wall_clock_now
    if (wall_clock_now - latest_ts) > timedelta(days=7):
        return latest_ts
    return wall_clock_now


def write_current_status(
    state_dir: Path,
    output_path: Path,
) -> Path:
    paths = _make_paths(state_dir)
    index = _load_events_indexed(paths)
    wall_clock_now = datetime.now(timezone.utc)
    # See _anchor_now docstring for the historical-vs-live heuristic.
    now_utc = _anchor_now(index, wall_clock_now)

    sections: list[str] = []

    latest_completed = _latest_event_of_type(index, "cycle_completed")
    last_cycle_ts = _parse_iso_datetime(
        latest_completed.get("timestamp") if latest_completed else None
    )
    last_cycle_str = (
        last_cycle_ts.strftime("%Y-%m-%d") if last_cycle_ts else "(none yet)"
    )
    sections.append(RULE_NARROW)
    sections.append("IRAPM Current Status")
    # The header timestamp is always the wall-clock generation time —
    # the operator wants to know when the file was produced. The
    # "now" anchor used for the body is separate (may be historical).
    sections.append(
        f"Generated: {wall_clock_now.strftime('%Y-%m-%d %H:%M:%S')} UTC  "
        f"(last weekly cycle: {last_cycle_str})"
    )
    sections.append(RULE_NARROW)

    sections.append(_render_status_header_block(index, latest_completed, now_utc))
    sections.append("")

    sections.append(RULE_NARROW)
    sections.append("Recent activity (last 4 weeks)")
    sections.append(RULE_NARROW)
    sections.append("")
    sections.append(_render_recent_activity_block(index))
    sections.append("")

    sections.append(RULE_NARROW)
    sections.append("Recent alerts (last 4 weeks)")
    sections.append(RULE_NARROW)
    sections.append(_render_recent_alerts_block(index, now_utc))
    sections.append("")
    sections.append(RULE_NARROW)

    content = "\n".join(sections) + "\n"
    _atomic_write_text(output_path, content)
    return output_path


def _render_status_header_block(
    index: EventIndex,
    latest_completed: Optional[dict],
    now_utc: datetime,
) -> str:
    latest_state = _latest_event_of_type(index, "state_snapshot")

    phase = "(none yet)"
    cb_state_display = "(none yet)"
    income_state = "(none yet)"
    operational_pause = "(none yet)"
    withdrawal_cap_exhausted = "(none yet)"

    if latest_completed:
        phase = latest_completed.get("payload", {}).get("phase", "(none yet)")

    if latest_state:
        sp = latest_state.get("payload", {})
        income_state = sp.get("income_state", "(none yet)")
        op_pause = sp.get("operational_pause", {})
        operational_pause = "true" if op_pause.get("paused") else "false"
        withdrawal_cap_exhausted = (
            "true" if sp.get("withdrawal_capacity_exhausted") else "false"
        )

        cb_machine = sp.get("cb_machine", {})
        cb_state_raw = cb_machine.get("state", "CB_INACTIVE")
        cb_short = _render_cb_short(cb_state_raw)
        if cb_short in ("1", "2"):
            # state_model.CBMachine.cb1_active_timer_started_at — that's
            # the canonical field per state_model.py (audited 2026-05-16
            # against an actual state_snapshot payload). The pre-fix code
            # incorrectly read `cb1_entered_at`, which never existed.
            # TODO: the "of 90" denominator is hardcoded; should come from
            # ruleset.cb1_to_cb2_timer_days. Deferred until ruleset is
            # threaded through write_current_status.
            entered_at = _parse_iso_datetime(
                cb_machine.get("cb1_active_timer_started_at")
            )
            if entered_at:
                # Use the same now_utc anchor as the rest of the block
                # (historical-vs-live aware) instead of wall-clock.
                days_elapsed = (now_utc - entered_at).days
                cb_state_display = (
                    f"{cb_short} (entered "
                    f"{entered_at.date().strftime('%Y-%m-%d')}, "
                    f"day {days_elapsed} of 90)"
                )
            else:
                cb_state_display = cb_short
        else:
            cb_state_display = cb_short

    current_withdrawal_str = _derive_current_monthly_withdrawal(index)
    last_withdrawal_str = _derive_last_withdrawal_display(index)
    next_withdrawal_str = _derive_next_scheduled_withdrawal(now_utc)

    lines = [
        f"Phase:                      {phase}",
        f"CB state:                   {cb_state_display}",
        f"Income state:               {income_state}",
        f"Operational pause:          {operational_pause}",
        f"Withdrawal cap exhausted:   {withdrawal_cap_exhausted}",
        "",
        f"Current monthly withdrawal: {current_withdrawal_str}",
        f"Last withdrawal:            {last_withdrawal_str}",
        f"Next scheduled withdrawal:  {next_withdrawal_str}",
    ]
    return "\n".join(lines)


def _derive_current_monthly_withdrawal(index: EventIndex) -> str:
    last_w = _latest_event_of_type(index, "withdrawal_executed")
    last_ar = _latest_event_of_type(index, "annual_review_completed")

    w_ts = _parse_iso_datetime(last_w.get("timestamp")) if last_w else None
    ar_ts = _parse_iso_datetime(last_ar.get("timestamp")) if last_ar else None

    use_ar = last_ar and (not w_ts or (ar_ts and ar_ts > w_ts))
    if use_ar:
        amount = _parse_decimal(
            last_ar.get("payload", {}).get("computed_new_withdrawal_dollars")
        )
    elif last_w:
        amount = _parse_decimal(
            last_w.get("payload", {}).get("scheduled_amount_dollars")
        )
    else:
        amount = None

    if amount is None:
        return "(none yet)"
    return f"${_fmt_dollar(amount)}"


def _derive_last_withdrawal_display(index: EventIndex) -> str:
    last_w = _latest_event_of_type(index, "withdrawal_executed")
    if not last_w:
        return "(none yet)"
    payload = last_w.get("payload", {})
    amount = _parse_decimal(payload.get("amount_paid_dollars"))
    if amount is None:
        return "(none yet)"
    ach_date = payload.get("scheduled_ach_date", "(unknown)")
    suffix = ""
    if payload.get("was_capped"):
        binding = payload.get("binding_ceiling")
        suffix = CAP_SYMBOL_FROM_BINDING.get(binding, "")
    return f"${_fmt_dollar(amount)}{suffix} on {ach_date}"


def _derive_next_scheduled_withdrawal(now_utc: datetime) -> str:
    """Next scheduled ACH date anchored to `now_utc` (caller-supplied).

    For a live system, `now_utc` is wall-clock and this returns today's
    or next month's 15th. For a historical event log, `now_utc` is the
    last event's timestamp and this returns the next 15th relative to
    that — still meaningless as a literal calendar prediction, but at
    least consistent with the rest of the rendered context.
    """
    today = now_utc.date()
    if today.day < 15:
        target = date(today.year, today.month, 15)
    else:
        if today.month == 12:
            target = date(today.year + 1, 1, 15)
        else:
            target = date(today.year, today.month + 1, 15)
    return target.strftime("%Y-%m-%d")


def _render_recent_activity_block(index: EventIndex) -> str:
    """Last 4 weekly cycles' rows + annotations (REPORT_SPEC §3.1.3).

    Filters to cycle_type == "weekly" because daily-token cycles also
    emit cycle_completed events but carry no portfolio snapshot, which
    would render as GAP rows here. Per the spec, this block shows the
    rolling weekly history.
    """
    all_completed = _events_of_type(index, "cycle_completed")
    completed_events = [
        e for e in all_completed
        if e.get("payload", {}).get("cycle_type") == "weekly"
    ]
    if not completed_events:
        return "(no weekly cycles recorded yet)"

    last_four = completed_events[-4:]

    if len(completed_events) >= 5:
        prev_ts: Optional[datetime] = _parse_iso_datetime(
            completed_events[-5].get("timestamp")
        )
    else:
        prev_ts = datetime(1970, 1, 1, tzinfo=timezone.utc)

    rows: list[DataRow] = []
    for completed in last_four:
        this_ts = _parse_iso_datetime(completed.get("timestamp"))
        if this_ts is None or prev_ts is None:
            prev_ts = this_ts
            continue
        period_start = prev_ts + timedelta(microseconds=1)
        period_end = this_ts + timedelta(microseconds=1)

        snapshot = _last_snapshot_in_window(index, period_start, period_end)
        withdrawals = _events_of_type_in_window(
            index, "withdrawal_executed", period_start, period_end,
        )
        fills = _events_of_type_in_window(
            index, "fill_received", period_start, period_end,
        )

        cash_out_total, cash_out_suffix = _aggregate_cash_out(withdrawals)
        cash_in_total = _aggregate_cash_in(fills)
        annotations = _extract_annotations_from_index(
            index, period_start, period_end, snapshot,
        )

        cb_state_raw = completed.get("payload", {}).get("cb_state", "CB_INACTIVE")
        phase = completed.get("payload", {}).get("phase", "")
        phase_num = phase.replace("PHASE_", "") if phase else ""

        row_date = this_ts.date()
        rows.append(_build_row_from_snapshot(
            row_date=row_date,
            snapshot_event=snapshot,
            cb_state_at_row=_render_cb_short(cb_state_raw),
            phase_at_row=phase_num,
            cash_in_total=cash_in_total,
            cash_out_total=cash_out_total,
            cash_out_suffix=cash_out_suffix,
            annotations=annotations,
        ))
        prev_ts = this_ts

    return _render_table(rows)


def _render_recent_alerts_block(
    index: EventIndex,
    now_utc: datetime,
) -> str:
    """Last 4 weeks of alerts in descending order (REPORT_SPEC §3.1.4)."""
    cutoff = now_utc - timedelta(weeks=4)
    alerts = _events_of_type_in_window(
        index, "alert_emitted", cutoff, now_utc + timedelta(microseconds=1),
    )
    if not alerts:
        return "(no alerts in the last 4 weeks)"

    alerts_sorted = sorted(
        alerts,
        key=lambda e: _parse_iso_datetime(e.get("timestamp"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    max_id_len = max(
        len(e.get("payload", {}).get("alert_id", "")) for e in alerts_sorted
    )

    lines: list[str] = []
    for event in alerts_sorted:
        ts = _parse_iso_datetime(event.get("timestamp"))
        ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "?"
        payload = event.get("payload", {})
        alert_id = payload.get("alert_id", "")
        context = payload.get("context", {}) or {}

        ctx_parts: list[str] = []
        for key in sorted(context.keys()):
            value = context[key]
            v_str = "null" if value is None else str(value)
            ctx_parts.append(f"{key}={v_str}")
        ctx_str = " ".join(ctx_parts)

        lines.append(
            f"{ts_str}  {alert_id.ljust(max_id_len)}  {ctx_str}".rstrip()
        )

    return "\n".join(lines)


# ============================================================================
# Public API: append_monthly_row_to_year_file (REPORT_SPEC §3.2)
# ============================================================================

def append_monthly_row_to_year_file(
    state_dir: Path,
    output_dir: Path,
    month: date,
) -> Path:
    paths = _make_paths(state_dir)
    index = _load_events_indexed(paths)
    year = month.year
    output_path = output_dir / f"{year}.txt"
    content = _build_year_file_content(
        index, year, through_month=month.month, closed=False,
    )
    _atomic_write_text(output_path, content)
    return output_path


# ============================================================================
# Public API: close_year_file (REPORT_SPEC §3.2.3)
# ============================================================================

def close_year_file(
    state_dir: Path,
    output_dir: Path,
    year: int,
) -> Path:
    paths = _make_paths(state_dir)
    index = _load_events_indexed(paths)
    output_path = output_dir / f"{year}.txt"
    content = _build_year_file_content(
        index, year, through_month=12, closed=True,
    )
    _atomic_write_text(output_path, content)
    return output_path


def _build_year_file_content(
    index: EventIndex,
    year: int,
    through_month: int,
    closed: bool,
) -> str:
    sections: list[str] = []
    sections.append(RULE_NARROW)
    sections.append(f"IRAPM {year}")
    sections.append(RULE_NARROW)
    sections.append("")

    rows: list[DataRow] = []
    for m in range(1, through_month + 1):
        rows.append(_build_monthly_row(index, year, m))

    sections.append(_render_table(rows))
    sections.append("")

    sections.append(RULE_NARROW)
    if closed:
        sections.append(f"{year} ANNUAL SUMMARY")
    else:
        month_name = calendar.month_name[through_month]
        sections.append(f"{year} ANNUAL SUMMARY (through {month_name} {year})")
    sections.append(RULE_NARROW)
    sections.append(_build_annual_summary_block(index, year, through_month))
    sections.append(RULE_NARROW)
    sections.append("")

    # Legend block: year files are read standalone (the operator may be
    # paging through 2010.txt without the sim report in front of them),
    # so the annotation/cap/column legend must travel with each file
    # rather than living only in the sim report. The legend body is
    # shared with the sim report via _render_legend_section.
    sections.append(RULE_NARROW)
    sections.append("LEGEND")
    sections.append(RULE_NARROW)
    sections.append(_render_legend_section())
    sections.append(RULE_NARROW)

    return "\n".join(sections) + "\n"


def _build_monthly_row(index: EventIndex, year: int, month: int) -> DataRow:
    """Build the row for a single calendar month (REPORT_SPEC §3.2.2)."""
    start, end = _month_window_utc(year, month)
    snapshot = _last_snapshot_in_window(index, start, end)
    withdrawals = _events_of_type_in_window(
        index, "withdrawal_executed", start, end,
    )
    fills = _events_of_type_in_window(index, "fill_received", start, end)

    cash_out_total, cash_out_suffix = _aggregate_cash_out(withdrawals)
    cash_in_total = _aggregate_cash_in(fills)
    annotations = _extract_annotations_from_index(index, start, end, snapshot)

    row_date = _last_day_of_month(year, month)

    cb_state_display = "-"
    state_event = _latest_state_snapshot_before(index, end)
    if state_event:
        cb_machine = state_event.get("payload", {}).get("cb_machine", {})
        cb_state_display = _render_cb_short(
            cb_machine.get("state", "CB_INACTIVE")
        )

    phase_num = ""
    cc_in_period = _events_of_type_in_window(
        index, "cycle_completed", start, end,
    )
    if cc_in_period:
        phase = cc_in_period[-1].get("payload", {}).get("phase", "")
        phase_num = phase.replace("PHASE_", "") if phase else ""

    return _build_row_from_snapshot(
        row_date=row_date,
        snapshot_event=snapshot,
        cb_state_at_row=cb_state_display,
        phase_at_row=phase_num,
        cash_in_total=cash_in_total,
        cash_out_total=cash_out_total,
        cash_out_suffix=cash_out_suffix,
        annotations=annotations,
    )


def _build_annual_summary_block(
    index: EventIndex,
    year: int,
    through_month: int,
) -> str:
    """Annual summary (REPORT_SPEC §3.2.4)."""
    year_start, year_end_full = _year_window_utc(year)
    if through_month == 12:
        scope_end = year_end_full
    else:
        _, scope_end = _month_window_utc(year, through_month)

    first_snap = _first_snapshot_in_window(index, year_start, scope_end)
    last_snap = _last_snapshot_in_window(index, year_start, scope_end)

    def _aum(snap: Optional[dict]) -> Decimal:
        if not snap:
            return Decimal("0")
        return _parse_decimal(
            snap.get("payload", {}).get("total_aum_dollars")
        ) or Decimal("0")

    start_aum = _aum(first_snap)
    end_aum = _aum(last_snap)
    net_change = end_aum - start_aum
    pct_change = (
        net_change / start_aum * Decimal("100")
        if start_aum > 0 else Decimal("0")
    )

    withdrawals = _events_of_type_in_window(
        index, "withdrawal_executed", year_start, scope_end,
    )
    total_withdrawn = sum(
        (_parse_decimal(e.get("payload", {}).get("amount_paid_dollars"))
         or Decimal("0"))
        for e in withdrawals
    )
    capped = [e for e in withdrawals if e.get("payload", {}).get("was_capped")]
    capped_count = len(capped)
    uncapped_count = len(withdrawals) - capped_count

    cap_lines: list[str] = []
    for cw in capped:
        p = cw.get("payload", {})
        binding = p.get("binding_ceiling", "")
        sym = CAP_SYMBOL_FROM_BINDING.get(binding, "?")
        name = (
            "guardrail" if binding == "guardrail"
            else "dollar cap" if binding == "dollar_cap"
            else binding
        )
        ach_date = p.get("scheduled_ach_date", "?")
        paid = _parse_decimal(p.get("amount_paid_dollars")) or Decimal("0")
        sched = _parse_decimal(p.get("scheduled_amount_dollars")) or Decimal("0")
        cap_lines.append(
            f"  - Cap binding cause: {name} ({sym}) on {ach_date} "
            f"(paid ${_fmt_dollar(paid)} of scheduled ${_fmt_dollar(sched)})"
        )

    asset_lines = _build_asset_movement_lines(first_snap, last_snap)

    cb_events = _events_of_type_in_window(
        index, "cb_transition", year_start, scope_end,
    )
    cb1_triggers = [e for e in cb_events
                    if e.get("payload", {}).get("to_state") == "CB1"
                    and e.get("payload", {}).get("from_state") == "CB_INACTIVE"]
    cb2_triggers = [e for e in cb_events
                    if e.get("payload", {}).get("to_state") == "CB2"
                    and e.get("payload", {}).get("from_state") == "CB_INACTIVE"]
    recoveries = [e for e in cb_events
                  if e.get("payload", {}).get("to_state") == "CB_INACTIVE"
                  and e.get("payload", {}).get("from_state") != "CB_INACTIVE"]

    cb1_dates = ", ".join(
        ts.date().strftime("%Y-%m-%d")
        for ts in (_parse_iso_datetime(e.get("timestamp")) for e in cb1_triggers)
        if ts
    )
    rec_dates = ", ".join(
        ts.date().strftime("%Y-%m-%d")
        for ts in (_parse_iso_datetime(e.get("timestamp")) for e in recoveries)
        if ts
    ) or "(not confirmed in scope)"

    days_in_cb1plus = _compute_days_in_cb1plus(index, year_start, scope_end)

    phase_transitions = _events_of_type_in_window(
        index, "phase_transition", year_start, scope_end,
    )
    if phase_transitions:
        pt_lines = []
        for pt in phase_transitions:
            pt_ts = _parse_iso_datetime(pt.get("timestamp"))
            pp = pt.get("payload", {})
            from_p = pp.get("from_phase", "?")
            to_p = pp.get("to_phase", "?")
            qual = " (latch)" if pp.get("is_phase3_activation") else ""
            ts_str = pt_ts.date().strftime("%Y-%m-%d") if pt_ts else "?"
            pt_lines.append(f"{ts_str}: {from_p} → {to_p}{qual}")
        phase_transitions_str = "; ".join(pt_lines)
    else:
        phase_transitions_str = "none"

    ar_events = _events_of_type_in_window(
        index, "annual_review_completed", year_start, scope_end,
    )
    if ar_events:
        ar = ar_events[-1]
        ar_ts = _parse_iso_datetime(ar.get("timestamp"))
        ar_payload = ar.get("payload", {})
        cpi = _parse_decimal(ar_payload.get("cpi_rate_applied")) or Decimal("0")
        freeze = ar_payload.get("binding_constraint") == "cpi_freeze"
        freeze_str = "freeze in effect (F)" if freeze else "no freeze in effect"
        ar_date_str = ar_ts.date().strftime("%Y-%m-%d") if ar_ts else "?"
        annual_review_line = (
            f"{ar_date_str} — CPI applied {cpi:.4f}, {freeze_str}"
        )
    else:
        annual_review_line = "(none in scope)"

    lines = [
        f"Year-end AUM:       ${_fmt_dollar(end_aum)}  "
        f"(start of year: ${_fmt_dollar(start_aum)})",
        f"Year-end change:    {_fmt_signed_pct(pct_change)} "
        f"(${_fmt_dollar(net_change)})",
        "",
        f"Total withdrawn:    ${_fmt_dollar(total_withdrawn)}",
        f"  - Monthly withdrawals × {len(withdrawals)} "
        f"(uncapped: {uncapped_count}, capped: {capped_count})",
    ]
    lines.extend(cap_lines)
    lines.append("")
    lines.append("Asset percentage movement (Jan 1 → period end):")
    lines.extend(asset_lines)
    lines.append("")
    lines.append("CB activity:")
    lines.append(
        f"  CB1 triggered:     {len(cb1_triggers)} time(s)"
        + (f"  ({cb1_dates})" if cb1_dates else "")
    )
    lines.append(f"  CB2 triggered:     {len(cb2_triggers)} times")
    lines.append(f"  Days in CB1+:      {days_in_cb1plus} days")
    lines.append(f"  Recovery confirmed: {rec_dates}")
    lines.append("")
    lines.append(f"Phase transitions: {phase_transitions_str}")
    lines.append(f"Annual review:     {annual_review_line}")
    return "\n".join(lines)


def _build_asset_movement_lines(
    first_snap: Optional[dict],
    last_snap: Optional[dict],
) -> list[str]:
    """Per-symbol price-return + allocation-of-investable-AUM lines.

    SGOV TREATMENT:
    Per IRAPM_SPECIFICATION I1, the SGOV buffer is NOT part of the
    core asset allocation — it has its own refill mechanism outside
    the rebalancer. We render SGOV with a price-return line only
    (no allocation %), and we use investable_aum = total_aum - sgov
    - cash_buff as the denominator for core symbols' allocation %
    so the five core figures sum to ~100%.

    The operator's reference AnnualSummary.txt example had SGOV with
    an allocation %; that was incorrect (the example was illustrative,
    not authoritative).
    """
    if not first_snap or not last_snap:
        return ["  (insufficient snapshots to compute asset movement)"]

    first_payload = first_snap.get("payload", {})
    last_payload = last_snap.get("payload", {})
    first_positions = first_payload.get("positions", {})
    last_positions = last_payload.get("positions", {})

    def _payload_dec(payload: dict, key: str) -> Decimal:
        return _parse_decimal(payload.get(key)) or Decimal("0")

    first_total = _payload_dec(first_payload, "total_aum_dollars")
    last_total = _payload_dec(last_payload, "total_aum_dollars")
    first_sgov = _payload_dec(first_payload, "sgov_buffer_dollars")
    last_sgov = _payload_dec(last_payload, "sgov_buffer_dollars")
    first_cash = _payload_dec(first_payload, "cash_dollars")
    last_cash = _payload_dec(last_payload, "cash_dollars")

    first_investable = first_total - first_sgov - first_cash
    last_investable = last_total - last_sgov - last_cash

    def _pos_dec(positions: dict, symbol: str, key: str) -> Decimal:
        return _parse_decimal((positions.get(symbol) or {}).get(key)) or Decimal("0")

    def _price_return(symbol: str) -> Decimal:
        first_price = _pos_dec(first_positions, symbol, "market_price_dollars")
        last_price = _pos_dec(last_positions, symbol, "market_price_dollars")
        if first_price > 0:
            return (last_price - first_price) / first_price * Decimal("100")
        return Decimal("0")

    lines: list[str] = []

    # Core symbols: allocation % uses investable AUM as denominator.
    for symbol in ("PYLD", "JPIE", "FBCG", "AVUV", "GBIL"):
        price_return = _price_return(symbol)
        first_val = _pos_dec(first_positions, symbol, "market_value_dollars")
        last_val = _pos_dec(last_positions, symbol, "market_value_dollars")
        first_alloc = (
            first_val / first_investable * Decimal("100")
            if first_investable > 0 else Decimal("0")
        )
        last_alloc = (
            last_val / last_investable * Decimal("100")
            if last_investable > 0 else Decimal("0")
        )
        lines.append(
            f"  {symbol}:    {_fmt_signed_pct(price_return)}    "
            f"({first_alloc:.1f}% → {last_alloc:.1f}% allocation)"
        )

    # SGOV: price return only — buffer is not part of allocation system.
    sgov_return = _price_return("SGOV")
    lines.append(
        f"  SGOV:    {_fmt_signed_pct(sgov_return)}    "
        f"(buffer, ${_fmt_dollar(first_sgov)} → ${_fmt_dollar(last_sgov)})"
    )
    return lines


# ============================================================================
# Public API: prune_old_year_files
# ============================================================================

def prune_old_year_files(
    output_dir: Path,
    retain_years: int = 8,
) -> list[Path]:
    if not output_dir.exists():
        return []
    current_year = datetime.now(timezone.utc).year
    cutoff_year = current_year - retain_years
    deleted: list[Path] = []
    for f in output_dir.glob("*.txt"):
        stem = f.stem
        if len(stem) == 4 and stem.isdigit():
            yr = int(stem)
            if yr < cutoff_year:
                try:
                    f.unlink()
                    deleted.append(f)
                except OSError as exc:
                    logger.warning(
                        "Failed to delete old year file %s: %s", f, exc,
                    )
    return deleted


# ============================================================================
# Public API: write_simulation_report (REPORT_SPEC §3.3)
# ============================================================================

def write_simulation_report(
    state_dir: Path,
    output_path: Path,
    scenario_name: str,
    ruleset: Optional[Any] = None,
) -> Path:
    paths = _make_paths(state_dir)
    index = _load_events_indexed(paths)
    now_utc = datetime.now(timezone.utc)

    sections: list[str] = []

    sections.append(RULE_WIDE)
    sections.append("IRAPM Simulation Report")
    sections.append(_render_sim_header_block(index, scenario_name, now_utc))
    sections.append(RULE_WIDE)
    sections.append("")

    sections.append("=== TOTALS ===")
    sections.append("")
    sections.append(_render_totals_section(index))
    sections.append("")

    sections.append("=== RULESET ===")
    sections.append("")
    sections.append(_render_ruleset_section(ruleset))
    sections.append("")

    annual_rows = _build_annual_rows(index)
    monthly_rows = _build_monthly_rows_for_sim(index)
    joint_widths = _compute_column_widths(annual_rows + monthly_rows)

    sections.append("=== ANNUAL ===")
    sections.append("")
    sections.append(_render_table_with_widths(annual_rows, joint_widths))
    sections.append("")

    sections.append("=== MONTHLY ===")
    sections.append("")
    sections.append(_render_table_with_widths(monthly_rows, joint_widths))
    sections.append("")

    sections.append("=== LEGEND ===")
    sections.append("")
    sections.append(_render_legend_section())
    sections.append("")
    sections.append(RULE_WIDE)

    content = "\n".join(sections) + "\n"
    _atomic_write_text(output_path, content)
    return output_path


def _render_sim_header_block(
    index: EventIndex,
    scenario_name: str,
    now_utc: datetime,
) -> str:
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    for event in index.all_events:
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts

    start_str = first_ts.date().strftime("%Y-%m-%d") if first_ts else "?"
    end_str = last_ts.date().strftime("%Y-%m-%d") if last_ts else "?"

    snaps = _events_of_type(index, "portfolio_snapshot")
    first_snap = snaps[0] if snaps else None
    last_snap = snaps[-1] if snaps else None

    start_aum = _parse_decimal(
        (first_snap or {}).get("payload", {}).get("total_aum_dollars")
    ) or Decimal("0")
    end_aum = _parse_decimal(
        (last_snap or {}).get("payload", {}).get("total_aum_dollars")
    ) or Decimal("0")

    final_cc = _latest_event_of_type(index, "cycle_completed")
    final_phase = (final_cc or {}).get("payload", {}).get("phase", "?")

    final_ss = _latest_event_of_type(index, "state_snapshot")
    final_cb_raw = (
        (final_ss or {}).get("payload", {}).get("cb_machine", {}).get(
            "state", "CB_INACTIVE",
        )
    )
    final_cb = _render_cb_short(final_cb_raw)

    total_withdrawn = sum(
        (_parse_decimal(e.get("payload", {}).get("amount_paid_dollars"))
         or Decimal("0"))
        for e in _events_of_type(index, "withdrawal_executed")
    )

    return "\n".join([
        f"Scenario: {scenario_name}",
        f"Run:      {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"Window:   {start_str} -> {end_str}",
        RULE_WIDE,
        f"Starting balance:  ${_fmt_dollar(start_aum)}",
        f"Terminal AUM:      ${_fmt_dollar(end_aum)}",
        f"Final phase:       {final_phase}",
        f"Final CB state:    {final_cb}",
        f"Total withdrawn:   ${_fmt_dollar(total_withdrawn)}",
    ])


def _render_totals_section(index: EventIndex) -> str:
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    for event in index.all_events:
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts

    if first_ts and last_ts:
        delta_days = (last_ts.date() - first_ts.date()).days
        years = delta_days // 365
        months = (delta_days % 365) // 30
        window_str = (
            f"{first_ts.date().strftime('%Y-%m-%d')} -> "
            f"{last_ts.date().strftime('%Y-%m-%d')}  "
            f"({years} years, {months} months)"
        )
    else:
        window_str = "(no events)"

    snaps = _events_of_type(index, "portfolio_snapshot")
    first_snap = snaps[0] if snaps else None
    last_snap = snaps[-1] if snaps else None

    start_aum = _parse_decimal(
        (first_snap or {}).get("payload", {}).get("total_aum_dollars")
    ) or Decimal("0")
    end_aum = _parse_decimal(
        (last_snap or {}).get("payload", {}).get("total_aum_dollars")
    ) or Decimal("0")
    net_change = end_aum - start_aum
    pct_change = (
        net_change / start_aum * Decimal("100")
        if start_aum > 0 else Decimal("0")
    )

    withdrawals = _events_of_type(index, "withdrawal_executed")
    total_withdrawn = sum(
        (_parse_decimal(e.get("payload", {}).get("amount_paid_dollars"))
         or Decimal("0"))
        for e in withdrawals
    )
    capped = [e for e in withdrawals if e.get("payload", {}).get("was_capped")]
    g_count = sum(1 for e in capped
                  if e.get("payload", {}).get("binding_ceiling") == "guardrail")
    c_count = sum(1 for e in capped
                  if e.get("payload", {}).get("binding_ceiling") == "dollar_cap")

    ar_events = _events_of_type(index, "annual_review_completed")
    freeze_events = [
        e for e in ar_events
        if e.get("payload", {}).get("binding_constraint") == "cpi_freeze"
    ]
    freeze_years = []
    for e in freeze_events:
        ts = _parse_iso_datetime(e.get("timestamp"))
        if ts:
            freeze_years.append(str(ts.year))
    freeze_years_str = ", ".join(freeze_years)

    cb_events = _events_of_type(index, "cb_transition")
    cb1_triggers = sum(
        1 for e in cb_events
        if e.get("payload", {}).get("to_state") == "CB1"
        and e.get("payload", {}).get("from_state") == "CB_INACTIVE"
    )
    cb2_triggers = sum(
        1 for e in cb_events
        if e.get("payload", {}).get("to_state") == "CB2"
        and e.get("payload", {}).get("from_state") == "CB_INACTIVE"
    )
    cb1_to_cb2 = sum(
        1 for e in cb_events
        if e.get("payload", {}).get("from_state") == "CB1"
        and e.get("payload", {}).get("to_state") == "CB2"
    )
    recoveries = sum(
        1 for e in cb_events
        if e.get("payload", {}).get("to_state") == "CB_INACTIVE"
        and e.get("payload", {}).get("from_state") != "CB_INACTIVE"
    )

    if first_ts and last_ts:
        days_cb1plus = _compute_days_in_cb1plus(
            index, first_ts, last_ts + timedelta(microseconds=1),
        )
        total_days = (last_ts.date() - first_ts.date()).days
        pct_cb = (days_cb1plus / total_days * 100) if total_days > 0 else 0
        cb_days_str = f"{days_cb1plus:,} days ({pct_cb:.1f}% of run)"
    else:
        cb_days_str = "0 days"

    p2_date = "(not reached)"
    p3_date = "(not reached)"
    for pt in _events_of_type(index, "phase_transition"):
        pp = pt.get("payload", {})
        ts = _parse_iso_datetime(pt.get("timestamp"))
        if ts is None:
            continue
        if pp.get("to_phase") == "PHASE_2":
            p2_date = ts.date().strftime("%Y-%m-%d")
        if pp.get("is_phase3_activation"):
            p3_date = ts.date().strftime("%Y-%m-%d")

    lines = [
        f"Run window:            {window_str}",
        f"Starting AUM:          ${_fmt_dollar(start_aum)}",
        f"Terminal AUM:          ${_fmt_dollar(end_aum)}",
        f"Net change:            {_fmt_signed_pct(pct_change)} "
        f"(${_fmt_dollar(net_change)})",
        f"Total withdrawn:       ${_fmt_dollar(total_withdrawn)}  "
        f"({len(withdrawals)} monthly withdrawals)",
        f"Withdrawals capped:    {len(capped)} total",
        f"  - Guardrail (G):     {g_count}",
        f"  - Dollar cap (C):    {c_count}",
        f"CPI freezes (F):       {len(freeze_events)}"
        + (f"   ({freeze_years_str})" if freeze_years_str else ""),
        f"CB1 triggers:          {cb1_triggers}",
        f"CB2 triggers:          {cb2_triggers}",
        f"CB1 -> CB2 promotions: {cb1_to_cb2}",
        f"Recoveries confirmed:  {recoveries}",
        f"Days in CB1+:          {cb_days_str}",
        f"Phase 2 transition:    {p2_date}",
        f"Phase 3 latch:         {p3_date}",
    ]
    return "\n".join(lines)


# Ruleset field names matched against actual ruleset_model.Ruleset
# (audited 2026-05-15).
_RULESET_TUNING_FIELDS: list[tuple[str, list[str]]] = [
    ("Withdrawal mechanics", [
        "phase1_initial_monthly_dollars",
        "phase3_monthly_payment_ceiling_rate",
        "phase3_dollar_ceiling_base_dollars",
        "phase3_dollar_ceiling_base_year",
        "inflation_rate",
    ]),
    ("Phase 3 I_0 calculation", [
        "phase3_i0_calc_return_assumption",
        "phase3_i0_calc_inflation_assumption",
        "phase3_i0_calc_horizon_years",
    ]),
    ("CB thresholds", [
        "cb1_threshold_rate",
        "cb2_threshold_rate",
        "freeze_evaluation_threshold_days",
    ]),
    ("Buffer mechanics", [
        "sgov_buffer_target_months",
        "cash_buffer_offset_dollars",
    ]),
]


def _render_ruleset_section(ruleset: Optional[Any]) -> str:
    if ruleset is None:
        return (
            "(ruleset object not available for this run — see "
            "ruleset_used.yaml in the run directory)"
        )

    blocks: list[str] = []
    all_fields = [f for _, fs in _RULESET_TUNING_FIELDS for f in fs]
    max_field_len = max(len(f) + 1 for f in all_fields)

    for group_title, field_names in _RULESET_TUNING_FIELDS:
        block_lines = [f"{group_title}:"]
        for fname in field_names:
            try:
                value = getattr(ruleset, fname)
            except AttributeError:
                value = "(not in ruleset)"
            if isinstance(value, Decimal):
                if abs(value) < 1:
                    val_str = f"{value:.4f}"
                else:
                    val_str = _fmt_dollar(value)
            else:
                val_str = str(value)
            label = (fname + ":").ljust(max_field_len)
            block_lines.append(f"  {label}       {val_str}")
        blocks.append("\n".join(block_lines))

    return "\n\n".join(blocks)


def _build_annual_rows(index: EventIndex) -> list[DataRow]:
    """One row per calendar year (REPORT_SPEC §3.3.5).

    SEMANTICS:
    Each row is dated YYYY-12-31 and represents the year-end terminal
    state for year Y. Numerics (gross_net_liq, allocations, etc.) come
    from the LAST portfolio_snapshot of year Y. Aggregates (cash_in,
    cash_out, annotations) cover year Y's events. The row label, the
    snapshot, and the annotations therefore all refer to the same
    year — no off-by-one confusion.

    For the partial final year (when the run ended mid-year), the row
    date is the actual last event date, and aggregates run from year
    start through that date. The row label distinguishes itself by
    its non-12-31 date.

    First-year edge case: if the run started mid-year, the first
    annual row's start is still Jan 1 of that year; the missing
    pre-start period contributes nothing to aggregates. This is the
    correct treatment: there were literally no events in that window,
    so reporting them as zero is accurate.
    """
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    for event in index.all_events:
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts

    if first_ts is None or last_ts is None:
        return []

    rows: list[DataRow] = []
    for year in range(first_ts.year, last_ts.year + 1):
        year_start, year_end = _year_window_utc(year)

        # For the partial final year, clip the scope to the actual
        # last-event timestamp so we don't pull non-existent future
        # events into the window.
        is_partial_final = (year == last_ts.year and last_ts < year_end - timedelta(microseconds=1))
        if is_partial_final:
            scope_end = last_ts + timedelta(microseconds=1)
            row_date = last_ts.date()
        else:
            scope_end = year_end
            row_date = date(year, 12, 31)

        last_snap = _last_snapshot_in_window(index, year_start, scope_end)
        if last_snap is None:
            continue

        annotations = _extract_annotations_from_index(
            index, year_start, scope_end, last_snap, granularity="annual",
        )

        year_withdrawals = _events_of_type_in_window(
            index, "withdrawal_executed", year_start, scope_end,
        )
        year_fills = _events_of_type_in_window(
            index, "fill_received", year_start, scope_end,
        )
        cash_out_total, cash_out_suffix = _aggregate_cash_out(year_withdrawals)
        cash_in_total = _aggregate_cash_in(year_fills)

        # Phase and CB at year-end derive from the LAST weekly cycle of
        # the year (matches the snapshot's time anchor).
        cc_in_period = _events_of_type_in_window(
            index, "cycle_completed", year_start, scope_end,
        )
        phase_num = ""
        cb_display = "-"
        if cc_in_period:
            last_cc = cc_in_period[-1]
            phase = last_cc.get("payload", {}).get("phase", "")
            phase_num = phase.replace("PHASE_", "") if phase else ""
            cb_display = _render_cb_short(
                last_cc.get("payload", {}).get("cb_state", "CB_INACTIVE")
            )

        rows.append(_build_row_from_snapshot(
            row_date=row_date,
            snapshot_event=last_snap,
            cb_state_at_row=cb_display,
            phase_at_row=phase_num,
            cash_in_total=cash_in_total,
            cash_out_total=cash_out_total,
            cash_out_suffix=cash_out_suffix,
            annotations=annotations,
        ))
    return rows


def _build_monthly_rows_for_sim(index: EventIndex) -> list[DataRow]:
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    for event in index.all_events:
        ts = _parse_iso_datetime(event.get("timestamp"))
        if ts is None:
            continue
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts

    if first_ts is None:
        return []

    rows: list[DataRow] = []
    year, month = first_ts.year, first_ts.month
    while (year, month) <= (last_ts.year, last_ts.month):
        rows.append(_build_monthly_row(index, year, month))
        if month == 12:
            month = 1
            year += 1
        else:
            month += 1
    return rows


def _render_legend_section() -> str:
    return """cb column:     -      no CB active
               1      CB1 active
               2      CB2 active
               1+2    both CB1 and CB2 entry conditions active

annotations:   AR     annual review
               CB1    CB1 transition entered
               CB2    CB2 transition entered
               REC    recovery confirmed (CB cleared to inactive)
               W      scheduled withdrawal executed
               RFL    SGOV buffer refill executed
               P2     phase 2 transition
               P3     phase 3 latch
               DPD    dry powder deployed
               DPR    dry powder refilled
               HALT   cycle halted
               PAUSE  operational pause activated
               GAP    no portfolio data for this period (production was down)

cap symbols:   G      withdrawal capped at guardrail (Phase 3 portfolio-% clamp)
                      Appended to cash_out cell: e.g., "3,500.00G"
               C      withdrawal capped at dollar cap (Phase 3 inflation-indexed)
                      Appended to cash_out cell: e.g., "3,891.20C"
               F      CPI-increase freeze fired (prior-year CB activity)
                      Appended to AR annotation: e.g., "AR:2010-01-06 (F)"

columns:
  date          row date (last day of month for monthly; Dec 31 for annual,
                or actual run-end date for the partial final year)
  cb            CB state at row date
  gross_net_liq total AUM = positions + cash buffer
  cash_buff     cash buffer (settled cash, excluded from FI/Growth)
  sgov_buffer   SGOV position (excluded from FI/Growth)
  fi_bucket     PYLD + JPIE + GBIL (GBIL is zero except in Phase 2)
  growth_bucket FBCG + AVUV
  fi_wt, gr_wt  bucket weight as fraction of investable AUM
                investable AUM = total AUM - SGOV - cash_buff
  cash_in       inflows during row period (initial deposit, dividends)
  cash_out      outflows during row period (withdrawals)
                Capped withdrawals append G/C/F suffix to amount
  pyld..gbil    per-symbol position market value
  phase         operating phase (1, 2, or 3)"""


# ============================================================================
# Paths helper
# ============================================================================

def _make_paths(state_dir: Path) -> Paths:
    try:
        return Paths(state_dir=state_dir)
    except TypeError:
        p = Paths()  # type: ignore
        try:
            p.state_dir = state_dir
        except AttributeError:
            pass
        return p


__all__ = [
    "write_current_status",
    "append_monthly_row_to_year_file",
    "close_year_file",
    "prune_old_year_files",
    "write_simulation_report",
]
