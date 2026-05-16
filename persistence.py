"""
persistence.py — File-level persistence layer (§9.4).

PURPOSE:
    Provide atomic-write semantics for the operating state file plus
    append-only writers for the various logs (cycle log, CB transition
    log, annual review log, alert log, coordination log, token check
    log). All disk I/O for IRAPM-managed state files routes through
    this module.

DESIGN (§9.4.1):
    - Atomic writes: temp file in same directory, fsync, rename
      (POSIX guarantees rename is atomic on same filesystem).
    - Append-only logs use line-delimited JSON ('JSON Lines') so a
      partial write doesn't corrupt earlier entries.
    - Log rotation policy (§9.4.5) is the operator's responsibility
      (logrotate on Linux). This module does not rotate; it only
      appends.
    - All durable files live under a configurable `state_dir`. The
      operator configures the path in box.yaml (§15.8); the runtime
      passes it in via Paths.

FILE LAYOUT (relative to state_dir):
    state.json                — current OperatingState (atomic)
    cycle_attempt.json        — in-flight cycle identity (atomic;
                                 owned by cycle_attempt.py, not here)
    token_observation.json    — per-box current token observation
    logs/cycle.jsonl          — cycle log (append-only)
    logs/cb_transitions.jsonl — CB transition log (append-only)
    logs/annual_review.jsonl  — annual review log (append-only)
    logs/alerts.jsonl         — alert dispatch log (append-only)
    logs/coordination.jsonl   — master/slave coordination log
    logs/token_check.jsonl    — daily token observations
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from state_model import OperatingState
from tokens import TokenObservation


# --- Path container --------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    """Resolved paths for all IRAPM persistent files. Constructed once
    at process start from box.yaml configuration."""
    state_dir: Path

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def cycle_attempt_file(self) -> Path:
        return self.state_dir / "cycle_attempt.json"

    @property
    def token_observation_file(self) -> Path:
        return self.state_dir / "token_observation.json"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        """Operator-facing reports directory (REPORT_SPEC §2.2). Lives
        under state_dir so the reporter outputs ship alongside the
        event log they were generated from. Contains current_status.txt
        and per-year {YYYY}.txt files written by report.py.
        """
        return self.state_dir / "reports"

    def events_log(self) -> Path:
        """The IRAPM event log (EVENT_LOG_SPEC §2). Lives at state_dir
        directly, not under logs/ — it is architecturally distinct from
        the legacy per-subsystem logs: the system of record, not a
        per-subsystem diagnostic file. Written by event_log.py only.
        """
        return self.state_dir / "events.jsonl"

    def cycle_log(self) -> Path:
        return self.logs_dir / "cycle.jsonl"

    def cb_transition_log(self) -> Path:
        return self.logs_dir / "cb_transitions.jsonl"

    def annual_review_log(self) -> Path:
        return self.logs_dir / "annual_review.jsonl"

    def alert_log(self) -> Path:
        return self.logs_dir / "alerts.jsonl"

    def coordination_log(self) -> Path:
        return self.logs_dir / "coordination.jsonl"

    def token_check_log(self) -> Path:
        return self.logs_dir / "token_check.jsonl"

    def ensure_dirs(self) -> None:
        """Create state_dir, logs_dir, and reports_dir if they don't exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


# --- JSON encoder for our types --------------------------------------------

class _Encoder(json.JSONEncoder):
    """JSON encoder that handles Decimal, datetime, UUID, set."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, set):
            return sorted(obj)
        return super().default(obj)


def _json_dumps(data: Any) -> str:
    """Serialize a dict to JSON using our encoder."""
    return json.dumps(data, cls=_Encoder, indent=2, sort_keys=True)


# --- Atomic-write helper ---------------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: temp file → fsync → rename.

    Per §9.4.1 a crash during this sequence leaves the prior file
    intact. The temp file is created in the same directory so the
    rename stays on one filesystem (rename across filesystems is not
    atomic).
    """
    path = Path(path)
    dir_path = str(path.parent) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=dir_path,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- Append-only log helper ------------------------------------------------

def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append `record` as one JSON line to `path`. Creates the file if
    absent. The append is NOT atomic vs concurrent writers; IRAPM runs
    one cycle at a time so concurrent writers don't occur.

    Each log line is a single-line JSON object (compact, no indent) so
    log parsing tools can read line-by-line.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, cls=_Encoder, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


# --- Operating state I/O ---------------------------------------------------

def load_operating_state(paths: Paths) -> OperatingState:
    """Load and validate state.json. Raises pydantic.ValidationError
    on schema violation (I9: invalid config prevents startup; the
    caller is responsible for alerting and exiting cleanly)."""
    with paths.state_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return OperatingState.model_validate(data)


def save_operating_state(paths: Paths, state: OperatingState) -> None:
    """Write state.json atomically.

    Uses Pydantic's model_dump_json which handles Decimal and datetime
    serialization correctly per the state_model.py conventions.
    """
    json_text = state.model_dump_json(indent=2)
    atomic_write_text(paths.state_file, json_text)


def state_file_exists(paths: Paths) -> bool:
    return paths.state_file.exists()


# --- Token observation I/O -------------------------------------------------

def save_token_observation(paths: Paths, obs: TokenObservation) -> None:
    """Write the box's current token observation atomically.

    This file is the slave-writable artifact replicated master via
    a dedicated slave→master rsync (§9.4.2). The format is a single
    JSON object representing the latest observation.
    """
    record = {
        "box_id": obs.box_id,
        "timestamp": obs.timestamp.isoformat(),
        "phase3_count": obs.phase3_count,
        "stopincome_count": obs.stopincome_count,
        "status": obs.status.value,
        "error": obs.error,
    }
    atomic_write_text(paths.token_observation_file, _json_dumps(record))


def load_token_observation(path: Path) -> dict[str, Any] | None:
    """Load a token observation file (could be self's or peer's).
    Returns None if file does not exist.

    Returns a raw dict rather than reconstructing TokenObservation
    because we don't want to lose forward-compat for future fields;
    callers extract what they need.
    """
    p = Path(path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


# --- Log writers (thin convenience wrappers) -------------------------------

def append_cycle_log(paths: Paths, record: dict[str, Any]) -> None:
    """Append one cycle's full forensic record (§12.2.1)."""
    append_jsonl(paths.cycle_log(), record)


def append_cb_transition(paths: Paths, record: dict[str, Any]) -> None:
    """Append one CB state transition (§6.7 / §12.2.2). Used by the
    annual freeze evaluation; indefinite retention."""
    append_jsonl(paths.cb_transition_log(), record)


def append_annual_review(paths: Paths, record: dict[str, Any]) -> None:
    """Append one annual review record (§7.6, §12.2.3)."""
    append_jsonl(paths.annual_review_log(), record)


def append_alert_record(paths: Paths, record: dict[str, Any]) -> None:
    """Append one alert-dispatch record (§12.2.4)."""
    append_jsonl(paths.alert_log(), record)


def append_coordination(paths: Paths, record: dict[str, Any]) -> None:
    """Append one master/slave coordination event (§12.2.5)."""
    append_jsonl(paths.coordination_log(), record)


def append_token_check(paths: Paths, record: dict[str, Any]) -> None:
    """Append one daily-token-cycle observation (§12.2.6)."""
    append_jsonl(paths.token_check_log(), record)


# --- CB transition log reader (for annual freeze evaluation) ---------------

def read_cb_transitions_for_year(paths: Paths, year: int) -> list[dict[str, Any]]:
    """Read all CB transition records whose timestamp falls in calendar
    year `year`. Used by §7.6.1 freeze evaluation.

    Returns an empty list if the log file does not exist (fresh deploy,
    no transitions yet).
    """
    log_path = paths.cb_transition_log()
    if not log_path.exists():
        return []
    records: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Skip a single corrupted line; the log is append-only
                # so this is rare and recoverable.
                continue
            ts_str = rec.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if ts.year == year:
                records.append(rec)
    return records


__all__ = [
    "Paths",
    "atomic_write_text",
    "append_jsonl",
    "load_operating_state",
    "save_operating_state",
    "state_file_exists",
    "save_token_observation",
    "load_token_observation",
    "append_cycle_log",
    "append_cb_transition",
    "append_annual_review",
    "append_alert_record",
    "append_coordination",
    "append_token_check",
    "read_cb_transitions_for_year",
]
