"""
collector.py — typed event log accumulating during a simulator run.

Public exports:
    StateCollector — append events during run; exposed as
        chronological list at run end

See also: IPMS_SPECIFICATION.md §4 (state collector)


DESIGN POSTURE

The collector is intentionally simple in v0.1.0:
  - Events are appended in the order produced.
  - The collector does not interpret events, validate consistency,
    or transform them.
  - At run end, the engine hands the events list to SimulationResult
    and the collector is discarded.

What the collector deliberately does NOT do:
  - Sort events by timestamp. Engine is responsible for emitting in
    chronological order; if events arrive out-of-order, that's an
    engine bug worth surfacing. Sorting in the collector would mask
    the bug.
  - Filter or summarize. All projection (per-month aggregation,
    cascade-depth derivation, etc.) happens at output-formatter
    time, reading from the immutable events list.
  - Persist to disk during the run. v0.1.0 is in-memory only.
    JSONL streaming is reserved for v2 (per spec §9.1).

The collector's only correctness obligation is "every event appended
ends up in the events list." This makes the collector itself trivial
to test.
"""

from __future__ import annotations

from ipms.events import AnyEvent


class StateCollector:
    """
    Append-only typed event log.

    Used by the engine throughout a run to record every captured
    event. At run end, the engine retrieves the events list and
    constructs a SimulationResult around it.
    """

    def __init__(self):
        # Single events list, chronological per engine contract.
        self._events: list[AnyEvent] = []

    def append(self, event: AnyEvent) -> None:
        """Record one event. No validation; engine owns ordering."""
        self._events.append(event)

    def extend(self, events: list[AnyEvent]) -> None:
        """Record multiple events at once. Order preserved."""
        self._events.extend(events)

    @property
    def events(self) -> list[AnyEvent]:
        """
        Read-only view of the accumulated events. Returns the
        underlying list directly (not a copy) — the engine consumes
        this once at finalization and the collector is then discarded,
        so aliasing is not a concern.
        """
        return self._events

    def __len__(self) -> int:
        """Number of events captured so far."""
        return len(self._events)
