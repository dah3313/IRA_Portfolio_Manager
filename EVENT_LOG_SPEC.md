# IRAPM Event Log — Specification

**Status:** Complete first draft (all 8 sections)
**Last updated:** 2026-05-14
**Authoritative document:** This file is the contract for the IRAPM event log subsystem. Any code that writes or reads the event log must conform to what is described here. If implementation needs to diverge, update this document first.

---

## 1. Purpose and scope

### 1.1 What this log is

The IRAPM event log is a single, append-only, structured record of every operationally significant thing IRAPM does, over the full operational life of the system. It is the system of record for IRAPM.

The event log is **the data substrate that the reporter consumes** to produce human-readable text reports. The reports are the operator's primary interface for monitoring and reviewing IRAPM activity; the event log itself is rarely read directly. Forensic deep-dives into the raw JSON are an occasional, secondary use case typically performed off the production box (the operator exports the log to a workstation for analysis).

"System of record" means:

- IRAPM's own logs are the authoritative answer to "what did IRAPM do at time T." IBKR (or any other broker) is a counterparty whose records are used for reconciliation, not as the source of truth for IRAPM's own behavior.
- All reports, audits, reconciliations, and forensic investigations are pure functions of the event log. No reporter computes anything from in-memory state or transient sources.
- The log is preserved across crashes, restarts, and machine migrations. Losing the log would lose the ability to answer operationally significant questions about IRAPM's history.

The event log is NOT on the runtime critical path of either box. The slave box, when promoting after master's death, queries IBKR directly to reconstruct order state — it does not depend on the event log being present or current. The event log is operator-facing infrastructure (via the reporter), not a runtime input to IRAPM itself. This decoupling means event log replication between boxes is desirable but not load-bearing; replication design is a separate concern that can evolve independently.

### 1.2 What this log enables

The event log exists to serve, in priority order:

1. **Reporter input (primary).** The text reports the operator reads on a routine basis are rendered from this log. Every column in those reports, every flag, every aggregation — all of it is computed from event log entries. The log's correctness and completeness directly determine the reports' trustworthiness.

2. **Forensic deep-dive (secondary).** When an unusual situation requires investigation, the raw JSON event log can be exported and analyzed in detail. This is rare in normal operation and typically happens off the production box (operator copies the log to a workstation; tooling there parses and visualizes it).

The following downstream concerns are real but flow from the above — they're achievable BECAUSE the event log faithfully captures everything, not separate design drivers:

- **Reconciliation against IBKR.** Every order placed and every fill received is logged with enough detail to match against IBKR's confirms.
- **Long-horizon audit.** Years after a cycle ran, the operator can reconstruct why IRAPM made a given decision from the captured inputs.
- **Tax records.** Year-end withdrawal totals and related records are recoverable from the log.
- **Safe testing of system changes.** Simulator runs produce event logs of the same shape as production, supporting rigorous before/after comparison.

### 1.3 What this log replaces (eventually)

Two existing IRAPM log files become redundant once the event log is consumed by all downstream readers:

- **`cycle.jsonl`** — per-cycle metadata stream (state machine values, halt status). Replaced by the `cycle_started`, `cycle_completed`, and `cycle_halted` event types.
- **`cb_transitions.jsonl`** — CB state machine transitions. Replaced by the `cb_transition` event type.

Migration is staged: both files continue to be written during the initial rollout, alongside the new event log. Consumers (the harness's failure tracker, the expectations checker, the annual review's CB freeze counter) migrate to the event log one at a time across future sessions. When every consumer is migrated, the legacy files' write paths are deleted in a single explicit cleanup.

**Rule during migration:** the existing logs are frozen in capability. No new fields, no new event types, no new use cases. If something new is needed, it goes in the event log only. This prevents the migration from becoming a perpetual "two parallel systems" state.

### 1.4 What this log does NOT replace

- **`state.json`** (the current operating state file). State.json is the current snapshot — the latest values of every state machine and counter. It is overwritten each cycle. The event log captures the *history* of state changes; state.json captures the *current value*. Both exist for different reasons.
- **Internal diagnostic logs** (Python's standard logging output, stderr tracebacks). These remain as transient operator-facing diagnostics. They are not the system of record.
- **IRAPM internal queues** (cycle_attempt.json, token_observation.json, peer_token_observation.json). These are operational coordination files, not historical records. They continue to live in their current locations.

### 1.5 Scope of this specification

This document specifies:
- The on-disk file format and location
- The common envelope shared by every event
- The complete catalog of event types and their payload schemas (Section 4)
- The writer API contract (Section 5)
- Standard reader patterns (Section 6)
- The migration plan from existing logs (Section 7)

This document does NOT specify:
- The reporter's output format (covered in a separate REPORT_SPEC.md once the event log lands)
- IBKR reconciliation tooling (future work, builds on this log)
- Tax-reporting tooling (future work, builds on this log)

---

## 2. File format and physical layout

### 2.1 Format

**JSONL** — one JSON object per line, UTF-8 encoded, LF line terminators (not CRLF). Each line is a complete event record. Lines are appended; existing lines are never modified.

Rationale:
- Append-only writes are atomic at the OS level for line-sized payloads, simplifying crash safety
- JSONL is trivially streamable: readers can process events one at a time without loading the whole file
- Standard tooling (`jq`, `grep`, `awk`, Python's `json.loads`) reads JSONL natively
- Records are independently parseable: one corrupted line does not prevent reading the rest of the file
- Human-readable: in a crisis, the operator can `tail events.jsonl` and read it directly

### 2.2 Location

The event log lives at:

```
{paths.state_dir} / "events.jsonl"
```

Where `paths.state_dir` is the IRAPM state directory, configured by the operator (production) or set to a temp dir per scenario (simulation).

**Production:** one event log spanning the operational life of IRAPM. The file lives in the production state directory and is preserved across restarts. It is included in any backup of the state directory.

**Simulation:** one event log per scenario run. The file lives in the per-run temp state directory. If `persist_state: true` is set on the scenario, the event log is copied into `runs/{scenario}/_harness_state/` alongside the other harness state at run completion.

### 2.3 Atomicity and crash safety

**Append guarantee.** Each `append()` call writes exactly one event record followed by a single LF byte. The writer uses `open(path, "ab")` semantics — opens in append-mode, writes, calls `fsync` if configured, then closes. POSIX append-mode guarantees that concurrent appenders never interleave bytes within a single `write()` call below a small size threshold (PIPE_BUF, typically 4KB). All event records are designed to fit well under this limit.

**Crash behavior.** If IRAPM is killed mid-write, three outcomes are possible:

1. **The write completed before the kill.** The event is on disk, complete, terminated with LF. No special handling needed.
2. **The write was killed before any bytes hit disk.** No partial line exists. The previous-cycle state is intact.
3. **The write was killed during the syscall.** A partial line may exist at the end of the file, lacking the terminating LF. The next reader skips this partial line (any reader implementation following Section 6's reader contract handles this case explicitly).

In no case should a crashed write corrupt the prior event records. The reader contract requires tolerance for a partial final line; this is the only crash-time damage the format permits.

**No mid-file mutation.** The writer never seeks, never overwrites, never truncates. Events once written are immutable. This is a load-bearing property — every downstream invariant depends on it.

### 2.4 Performance budget

Event log writes happen on the critical path of IRAPM cycles. The writer must be fast enough that emission is unconditional — there is never a reason to skip logging an event for performance.

**Per-event write budget:** under 1ms on a modern SSD, including JSON serialization and disk flush. A 20-year baseline scenario emits roughly 50,000 events (1044 cycles × ~50 events per cycle worst-case during active rebalance weeks); the total writer cost for a full sim is well under 1 minute, dwarfed by other simulation overhead.

**No batching.** Events are flushed individually. Batching is a future optimization gated on real evidence of write-amplification problems, not preemptive design.

### 2.5 Retention

**Single file, append-only forever, no pruning, no compression.**

- **`{paths.state_dir} / events.jsonl`** — every event IRAPM ever emits, in chronological order, plain JSONL.

The decision to keep all events forever in a single file reflects three facts:

1. **Disk cost is trivial at the scales IRAPM operates.** Operational events run roughly 500 KB/year (cycle records, daily token events, weekly portfolio snapshots, monthly state snapshots, ~50 fills, ~250 alerts, plus assorted operational events). Thirty years of operation: ~15 MB. A single phone photograph is larger.

2. **The file is rarely read directly.** The reporter consumes the log programmatically and renders text reports the operator actually reads. Forensic analysis of the raw JSON is rare and typically performed off the production box. A 15 MB file is not a problem for any consumer; it's not a problem for `cat`, `tail`, `grep`, or Python.

3. **Replication-friendliness is preserved.** Because the file is purely append-only and never rewritten in place, it can be replicated to a peer box via append-mode rsync (or any similar mechanism) without consistency complications. Future replication design is unconstrained by the retention mechanism.

**No separate "permanent" file.** Earlier drafts of this spec separated long-retention events (withdrawals, annual reviews, phase transitions) into a second file for fast operator access. With the single-file forever design, that separation is unnecessary: every event is preserved, and the reporter handles operator-facing access. A consumer who wants to see "only permanent events" filters the single file at read time (e.g., `grep '"event_type":"withdrawal_executed"' events.jsonl`).

**Tax records.** IRAPM operates inside a tax-advantaged account (Roth IRA in the operator's case; Traditional IRA for other potential users). Trades inside the account do not generate taxable events. Cost basis is not tracked. The events that DO have long-horizon tax/legal relevance (`withdrawal_executed`, `annual_review_completed`, `phase_transition`) are preserved by default through the forever-retention policy; no special handling is required.

**Simulation mode.** Simulator runs produce a single `events.jsonl` for the run, regardless of duration. The file lives in the per-run temp state directory and is discarded or persisted as a unit at end of run via the existing `persist_state` mechanism. Simulator and production runs use exactly the same retention behavior (none required, since nothing is ever pruned).

### 2.6 Backup and restore

The event log is the most operationally important file in the IRAPM state directory. It should be included in any backup of the state directory and treated as critical data.

**Backup recommendation:** the entire state directory (including `events.jsonl`) should be backed up offline at least weekly, with at least one geographically separate copy. This is operator responsibility — IRAPM does not perform its own backups.

**Restore:** restoring from backup involves copying the state directory back into place. The event log resumes appending from where the backup left off; events written between the backup and the failure are lost, but the file structure remains valid.

---

## 3. Common event envelope

Every event written to the log shares a common envelope of required fields, regardless of event type. This envelope is the load-bearing contract — readers depend on these fields existing on every record.

### 3.1 Required envelope fields

```json
{
  "schema_version": "1.0",
  "event_id": "evt_a1b2c3d4e5f6...",
  "event_type": "cycle_completed",
  "timestamp": "2026-05-14T14:30:00+00:00",
  "emitted_at": "2026-05-14T14:30:00.123456+00:00",
  "source_cycle_id": "ecffd87f-4651-41dc-b761-0e36657cf8f6",
  "payload": { ... }
}
```

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Version of the event log schema this record conforms to. Currently `"1.0"`. See §3.4 for versioning policy. |
| `event_id` | string | Unique identifier for this event, format `evt_` + 32 hex characters (UUID4 minus dashes, prefixed). Globally unique across all events ever written. |
| `event_type` | string | One of the documented event types from Section 4. Determines the shape of `payload`. |
| `timestamp` | string (ISO 8601 with timezone) | The simulated/operational time the event occurred. In production, this equals `emitted_at`. In simulation, this is the in-sim calendar time, which may differ from wall-clock time. **All timestamps include timezone**; UTC is the canonical representation. |
| `emitted_at` | string (ISO 8601 with microsecond precision and timezone) | The actual wall-clock instant the event was written. In production equals `timestamp`. In simulation tracks real-time progress through the sim. Used for performance analysis. |
| `source_cycle_id` | string or null | The UUID of the IRAPM cycle that produced this event. Null for events emitted outside any cycle (e.g., archival events, manual operator actions). |
| `payload` | object | Event-type-specific data. Shape defined by `event_type`, documented in Section 4. |

### 3.2 Timezone handling

**Canonical representation: UTC with explicit timezone suffix (`+00:00` or `Z`).** Every timestamp in the event log carries timezone information. Naive timestamps (no timezone) are never written.

The reasoning: a previous IRAPM bug (annual_review.py timezone normalization, fixed 2026-05-13) was caused by ambiguity between naive and aware datetimes in the existing logs. The event log forecloses that entire bug class by mandating timezone-aware timestamps everywhere. Readers can safely parse with `datetime.fromisoformat()` and never face the naive-vs-aware comparison error.

Local-time conversion (for display) happens in reporters and operator-facing tools, never in the log itself. The log is canonical UTC.

### 3.3 Event ID generation

Event IDs are generated at emission time using a UUID4 generator. The `evt_` prefix exists to make IDs visually distinct from cycle IDs (which today are raw UUIDs without prefix) and from any other UUID-like identifier in IRAPM. A future migration could prefix cycle IDs similarly (`cyc_`), but is not in scope here.

**Uniqueness guarantee:** UUID4 collision probability is negligible at the scales IRAPM operates. No event ID will ever collide with another event ID across the entire operational life of IRAPM or across all simulator runs.

**Idempotency:** Event IDs are stable identifiers that can be referenced by other events (e.g., a `fill_received` event references the `event_id` of its corresponding `order_placed` event). Re-processing an event with the same ID is a no-op for any well-designed consumer; this supports replay-based recovery scenarios.

### 3.4 Schema versioning

The `schema_version` field exists to allow the event log format to evolve over IRAPM's operational lifetime.

**Version 1.0** (current): the schema as documented in this specification.

**Compatibility rules:**

- **Backward compatibility within a major version (1.x).** Any 1.x writer produces events that any 1.x reader can read. New event types can be added without bumping the major version. New optional fields can be added to existing event types' payloads without bumping the major version. Required fields cannot be added, types cannot be changed, and fields cannot be removed without a major version bump.
- **Major version bumps (2.0, 3.0, ...).** Reserved for genuinely breaking changes. Major version bumps require a migration plan: how do existing 1.x events get read by 2.x consumers? Where does the boundary in the log live? The expectation is that major version bumps are rare events.

**Multi-version log behavior:** a single `events.jsonl` can contain events at different schema versions. Each event's `schema_version` field is authoritative for that record. Readers must dispatch on `schema_version` when parsing payload fields that exist only in newer versions.

### 3.5 Payload shape

The `payload` field contains the event-type-specific data. Its shape is defined by `event_type`. Section 4 documents the payload schema for every event type.

Payload design principles:

- **Self-contained.** A payload contains everything needed to interpret the event, without requiring the reader to consult other events. Where one event references another (e.g., a fill referencing its order), the reference is by event_id, but the referenced event's key data is also denormalized into the referring event's payload for reading convenience. This trades modest disk space for reader simplicity.
- **No back-references in time.** A payload never describes "the state before this event." It describes only this event. Computing "before" state is the reader's job, done by replaying events in order.
- **Decimals as strings.** All monetary and quantity values are serialized as JSON strings (e.g., `"3000.50"`), not JSON numbers, to preserve Decimal precision through the JSON round-trip. JSON numbers are floats and lose precision for financial amounts; this is non-negotiable.
- **Booleans as booleans.** Standard JSON true/false. Not "yes"/"no" strings, not 1/0 integers.

### 3.6 Example: a complete minimal event

This is a `cycle_started` event, fully populated, illustrating the envelope shape. Section 4 defines what `cycle_started`'s payload actually contains; this example shows only the envelope structure.

```json
{
  "schema_version": "1.0",
  "event_id": "evt_a1b2c3d4e5f67890a1b2c3d4e5f67890",
  "event_type": "cycle_started",
  "timestamp": "2026-05-14T14:30:00+00:00",
  "emitted_at": "2026-05-14T14:30:00.123456+00:00",
  "source_cycle_id": "ecffd87f-4651-41dc-b761-0e36657cf8f6",
  "payload": {
    "cycle_type": "weekly",
    "is_restart": false
  }
}
```

In the actual log file, this event is one line (no pretty-printing). Pretty-printing here is for documentation only.

---

## 4. Event type catalog

This section defines every event type the log supports. Each entry specifies:
- **When emitted:** exact location in the code where this event is written
- **Payload schema:** field-by-field shape of the `payload` object
- **Writes to:** `events.jsonl` only, or both `events.jsonl` AND `events_permanent.jsonl`
- **Consumers:** what downstream readers depend on this event

All payload fields are required unless marked optional. Monetary values and quantities are JSON strings preserving Decimal precision (per §3.5). Timestamps are ISO 8601 with explicit timezone (per §3.2).

This section defines all 13 event types in the catalog.

---

### 4.1 `cycle_started`

**When emitted:** At the top of `run_weekly_cycle` and `run_daily_token_cycle`, immediately after `begin_cycle()` returns the CycleAttempt. This is the first event of any cycle. Emitting at this point means the event is written even if the cycle subsequently raises during input refresh or decision — we always have a record that the cycle was attempted.

**Writes to:** `events.jsonl` only.

**Payload schema:**

```json
{
  "cycle_type": "weekly",
  "is_restart": false,
  "box_id": "harness-box",
  "client_id": 11
}
```

| Field | Type | Description |
|---|---|---|
| `cycle_type` | string | One of `"weekly"`, `"daily-token"`. |
| `is_restart` | boolean | True iff `begin_cycle()` found and recovered an in-progress attempt from a prior interrupted run. Restart cycles continue work the previous attempt began. |
| `box_id` | string | Operator box identifier (e.g., `"harness-box"`, `"primary-box"`, `"secondary-box"`). |
| `client_id` | integer | Broker client ID (e.g., 11 for primary box, 12 for secondary box per spec). |

**Consumers:**
- Reporter: identifies cycle boundaries for time-series aggregation
- Forensic tools: confirms a cycle was attempted on a given date, distinguishing "cycle missing" (no event) from "cycle started but failed" (started event without completed event)
- Failure tracker (post-migration): replaces the current cycle.jsonl-based detection mechanism

---

### 4.2 `cycle_completed`

**When emitted:** At the very end of `run_weekly_cycle` and `run_daily_token_cycle`, immediately after `complete_cycle()` marks the attempt complete. This event is the marker of clean completion — if a cycle raises before this point, no `cycle_completed` event is written, and the absence of this event in the log is itself diagnostic information.

**Writes to:** `events.jsonl` only.

**Payload schema:**

```json
{
  "cycle_type": "weekly",
  "phase": "PHASE_1",
  "cb_state": "CB_INACTIVE",
  "income_state": "ACTIVE",
  "operational_pause": false,
  "withdrawal_capacity_exhausted": false,
  "lookback_status": "OK",
  "lookback_value": "-0.0834",
  "plan_entry_count": 5,
  "is_scheduled_withdrawal_day": false,
  "is_annual_review_day": false,
  "is_phase2_reallocation_day": false,
  "duration_ms": 487
}
```

| Field | Type | Description |
|---|---|---|
| `cycle_type` | string | One of `"weekly"`, `"daily-token"`. Matches the corresponding `cycle_started` event. |
| `phase` | string | Operating phase at cycle completion: `"PHASE_1"`, `"PHASE_2"`, or `"PHASE_3"`. |
| `cb_state` | string | CB state machine value at completion: `"CB_INACTIVE"`, `"CB1"`, `"CB2"`, `"CB1_RECOVERY_STAGE1"`, `"CB2_RECOVERY_STAGE1"`, `"CB1_RECOVERY_STAGE2"`, `"CB2_RECOVERY_STAGE2"`. |
| `income_state` | string | `"ACTIVE"` or `"PAUSED"`. |
| `operational_pause` | boolean | True iff `state.operational_pause.paused` is true. |
| `withdrawal_capacity_exhausted` | boolean | True iff `state.withdrawal_capacity_exhausted` is true. |
| `lookback_status` | string | Lookback signal status at this cycle: `"OK"`, `"UNAVAILABLE"`, `"STALE"`. Null for daily-token cycles (no lookback computed). |
| `lookback_value` | string (Decimal) or null | The signal value (e.g., `"-0.0834"` = -8.34% over the lookback window). Null when status ≠ OK or for daily-token cycles. |
| `plan_entry_count` | integer | Number of entries in the decision plan. 0 for daily-token cycles. |
| `is_scheduled_withdrawal_day` | boolean | True iff this cycle was a withdrawal-day cycle per the calendar predicate. False for daily-token cycles. |
| `is_annual_review_day` | boolean | True iff this cycle was an annual review cycle. False for daily-token cycles. |
| `is_phase2_reallocation_day` | boolean | True iff this cycle matched a Phase 2 reallocation date. False for daily-token cycles and for non-Phase-2 cycles. |
| `duration_ms` | integer | Wall-clock elapsed time from `cycle_started` to `cycle_completed` emission, in milliseconds. For performance monitoring. |

**Consumers:**
- Reporter: primary source for the cycle-row data
- Failure tracker (post-migration): detects missing `cycle_completed` after `cycle_started` (=== cycle raised)
- Performance monitoring: `duration_ms` trends

---

### 4.3 `cycle_halted`

**When emitted:** When a cycle reaches its action layer and the plan execution halts (e.g., zero-quantity order rejection, account_id mismatch, broker inconsistency during placement). Emitted from `cycle.py` line 11 (cycle log append), where `result.halted_due_to_failure` is currently captured to cycle.jsonl. The new event log captures the same information here.

A halt is distinct from a raise: a halt means the cycle reached the action layer, started executing the plan, and the action layer made a controlled stop. A raise means the cycle aborted before that (input refresh failure, decision-layer exception, etc.) and no `cycle_halted` event is written — the absence of `cycle_completed` is the marker.

**Writes to:** `events.jsonl` only.

**Payload schema:**

```json
{
  "cycle_type": "weekly",
  "halt_reason": "growth SELL leg failed: quantity reduced to zero for FBCG",
  "phase_at_halt": "PHASE_1",
  "cb_state_at_halt": "CB_INACTIVE",
  "plan_entry_count": 1,
  "completed_legs": 0,
  "failed_leg_index": 0
}
```

| Field | Type | Description |
|---|---|---|
| `cycle_type` | string | Always `"weekly"`. Daily-token cycles do not produce halts (their work is too minimal). |
| `halt_reason` | string | Human-readable explanation of why the cycle halted. Same text as the `result.halt_reason` field returned by action_layer. |
| `phase_at_halt` | string | Phase value at the moment of halt. |
| `cb_state_at_halt` | string | CB state at the moment of halt. |
| `plan_entry_count` | integer | Total entries in the plan being executed. |
| `completed_legs` | integer | Number of plan legs that successfully completed before the halt. Zero means the very first leg failed. |
| `failed_leg_index` | integer | Zero-based index of the leg that triggered the halt. |

**Followed by:** a `cycle_completed` event is STILL emitted after `cycle_halted` — the cycle completes its lifecycle in a controlled manner, just with a halt recorded. This preserves the invariant that every successful `cycle_started` is paired with a `cycle_completed`.

**Consumers:**
- Forensic tools: "what halted that cycle"
- Reporter: contributes to the `flags` column for the affected period (`HALT` flag)
- Operational monitoring: detect retry loops via repeated identical halt_reason values

---

### 4.4 `decision_made`

**When emitted:** In `run_weekly_cycle`, immediately after `decide()` returns and before the plan is executed (between steps 7 and 9 of the existing cycle code). This event captures the decision plan that IRAPM has determined for this cycle, BEFORE any execution takes place. Together with `order_placed` / `fill_received` / `cycle_halted` events, this enables full reconstruction of "what IRAPM decided to do vs. what actually happened."

The payload is the most complex in the catalog because it must serve Concern #3 (long-horizon audit — reconstruct why IRAPM made a given decision years later). Every input that shaped the decision is captured.

**Writes to:** `events.jsonl` only.

**Payload schema:**

```json
{
  "inputs": {
    "phase": "PHASE_1",
    "cb_state": "CB_INACTIVE",
    "income_state": "ACTIVE",
    "lookback_status": "OK",
    "lookback_value": "-0.0834",
    "is_scheduled_withdrawal_day": true,
    "is_annual_review_day": false,
    "is_phase2_reallocation_day": false,
    "combined_token_state": "phase3_present_stopincome_absent",
    "total_aum_dollars": "668500.00",
    "cash_dollars": "4000.00",
    "sgov_buffer_dollars": "72000.00",
    "position_values": {
      "FBCG": "167125.50",
      "AVUV": "167125.50",
      "PYLD": "167125.50",
      "JPIE": "167125.50",
      "GBIL": "0.00"
    }
  },
  "plan_entries": [
    {
      "entry_index": 0,
      "kind": "WITHDRAWAL",
      "summary": "$3,000 monthly Phase 1 withdrawal",
      "dollar_amount": "3000.00",
      "sources": [
        {"symbol": "SGOV", "dollar_amount": "3000.00"}
      ]
    },
    {
      "entry_index": 1,
      "kind": "BUFFER_REFILL",
      "summary": "Monthly SGOV refill from Growth",
      "dollar_amount": "6000.00",
      "sources": [
        {"symbol": "FBCG", "dollar_amount": "3000.00"},
        {"symbol": "AVUV", "dollar_amount": "3000.00"}
      ]
    }
  ],
  "should_skip_action_layer": false,
  "skip_reason": null
}
```

| Field | Type | Description |
|---|---|---|
| `inputs` | object | All inputs that fed into `decide()`. See sub-table below. |
| `plan_entries` | array of objects | The plan IRAPM decided on. Empty array if no work this cycle. See sub-table below. |
| `should_skip_action_layer` | boolean | True iff the decision says to dispatch alerts only, no broker activity. |
| `skip_reason` | string or null | Human-readable reason for skipping (e.g., `"operational_pause active"`). Null if not skipping. |

**`inputs` sub-object:**

| Field | Type | Description |
|---|---|---|
| `phase` | string | Operating phase as `decide()` saw it (BEFORE this cycle's transitions). |
| `cb_state` | string | CB state as `decide()` saw it. |
| `income_state` | string | `"ACTIVE"` or `"PAUSED"`. |
| `lookback_status` | string | Lookback signal status. |
| `lookback_value` | string (Decimal) or null | Signal value. |
| `is_scheduled_withdrawal_day` | boolean | Calendar predicate result. |
| `is_annual_review_day` | boolean | Calendar predicate result. |
| `is_phase2_reallocation_day` | boolean | Calendar predicate result. |
| `combined_token_state` | string | Combined two-box token state value (e.g., `"phase3_present_stopincome_absent"`, `"phase3_absent"`, `"stopincome_present"`, `"unavailable"`). |
| `total_aum_dollars` | string (Decimal) | Total assets under management as seen by `decide()`. |
| `cash_dollars` | string (Decimal) | Cash balance. |
| `sgov_buffer_dollars` | string (Decimal) | SGOV buffer value. |
| `position_values` | object: symbol → string (Decimal) | Per-symbol market value. Includes every position in `state.positions`. |

**`plan_entries[i]` sub-object:**

| Field | Type | Description |
|---|---|---|
| `entry_index` | integer | Zero-based index of this entry within the plan. |
| `kind` | string | Entry kind: one of `"WITHDRAWAL"`, `"BUFFER_REFILL"`, `"STANDARD_REBALANCE"`, `"PHASE2_TRANSITION"`, `"PHASE2_SWING"`, `"DRY_POWDER_DEPLOY"`, `"DRY_POWDER_REFILL"`, `"ALERT"`. |
| `summary` | string | Human-readable description (e.g., `"$3,000 monthly Phase 1 withdrawal"`). |
| `dollar_amount` | string (Decimal) | Total dollar amount of the entry (sum of sources). Zero for ALERT entries. |
| `sources` | array of `{symbol, dollar_amount}` objects | Per-symbol breakdown of the entry. Empty for ALERT entries. |

**Consumers:**
- Reporter: contributes to the `flags` column (entry kinds visible as flags); supports detailed per-cycle breakdown views
- Audit tools: the load-bearing event for Concern #3 — "why did IRAPM do X" is answered by reading the `inputs` field
- Reconciliation: the planned `sources` per entry are compared against the actual `fill_received` events for the same cycle to verify execution matched intent

---

### 4.5 `order_placed`

**When emitted:** In `action_layer._place_one_order`, immediately after `broker.place_order()` returns successfully (i.e., the broker has accepted the order and assigned a `broker_order_id`). Emitted BEFORE the action layer waits for terminal status. This means every order that reached the broker is logged — including orders that subsequently get rejected, cancelled, time out, or take a long time to fill.

A broker rejection during `place_order()` itself (a `BrokerRejection` exception) produces NO `order_placed` event — the order never made it to the broker's book. The cycle log records the rejection via `cycle_halted` instead.

**Writes to:** `events.jsonl` only.

**Payload schema:**

```json
{
  "client_order_id": "cycle-ecffd87f-0-FBCG-SELL",
  "broker_order_id": "123456789",
  "plan_entry_index": 0,
  "plan_entry_kind": "BUFFER_REFILL",
  "symbol": "FBCG",
  "side": "SELL",
  "order_type": "MKT",
  "quantity_shares": "17.9640",
  "limit_price_dollars": null,
  "time_in_force": "DAY",
  "intended_dollar_amount": "3000.00",
  "price_refresh_dollars": "167.00",
  "price_refresh_status": "OK",
  "used_fallback_estimate": false
}
```

| Field | Type | Description |
|---|---|---|
| `client_order_id` | string | The idempotent ID minted via `build_client_order_id(cycle_uuid, plan_entry_index, symbol, side)`. Format: `"cycle-{cycle_uuid}-{plan_entry_index}-{symbol}-{side}"`. Used as the primary key for cross-referencing with `fill_received` events. |
| `broker_order_id` | string | The broker's own identifier for this order, returned by `place_order()`. Used for IBKR-side reconciliation. |
| `plan_entry_index` | integer | Zero-based index of the plan entry this order belongs to. Links back to `decision_made.plan_entries[i]`. |
| `plan_entry_kind` | string | Kind of the parent plan entry: `"ORDER"`, `"WITHDRAWAL"`, `"BUFFER_REFILL"`, `"CASH_REFILL"`, `"LARGE_CASH_DEPLOYMENT"`, or `"PHASE_TRANSITION"`. Duplicates information available via `plan_entry_index` lookup, but makes single-event reading possible without cross-referencing. |
| `symbol` | string | Ticker symbol. |
| `side` | string | `"BUY"` or `"SELL"`. |
| `order_type` | string | `"MKT"` or `"LMT"`. (Other order types not currently used by IRAPM.) |
| `quantity_shares` | string (Decimal) | Share quantity submitted to the broker, quantized to 4 decimal places ROUND_DOWN. |
| `limit_price_dollars` | string (Decimal) or null | Limit price for LMT orders. Null for MKT orders. |
| `time_in_force` | string | Currently always `"DAY"` (per current action_layer behavior). Captured here so any future expansion is recorded. |
| `intended_dollar_amount` | string (Decimal) | The dollar amount from the SourceLine or OrderEntry that drove this order. The pre-execution intent; comparing to the sum of `fill_received` events gives the actual dollar amount executed. |
| `price_refresh_dollars` | string (Decimal) or null | The price returned by `broker.get_prices()` at quantity-refresh time, used to compute `quantity_shares`. Null if price refresh fell back to the stale estimate. |
| `price_refresh_status` | string | `"OK"`, `"UNAVAILABLE"`, or `"STALE"` from the price refresh call. |
| `used_fallback_estimate` | boolean | True iff price refresh failed or returned non-OK status and the SourceLine's `share_count_estimate` was used as the share quantity instead. |

**Notes on field selection:**

The `price_refresh_*` fields capture the price IRAPM saw at the moment of order placement. This matters for reconciliation: if IBKR's fill price differs significantly from `price_refresh_dollars`, that's either market movement during placement or a stale quote at refresh time. Either way, the discrepancy is now observable.

`intended_dollar_amount` is the load-bearing field for the "intent vs. execution" comparison. The reporter's `cash_flow_in`/`cash_flow_out` columns derive from `fill_received` events (the actual money that moved), but a reader who wants "what IRAPM tried to do this cycle" reads `intended_dollar_amount` per order.

**Consumers:**
- Reporter: not directly (the reporter uses `fill_received` for actual dollar flows), but the order set per cycle is summarized as an "orders" count or detailed-view appendix
- Reconciliation: this is the IRAPM-side record of "what we asked the broker to do." Cross-reference with IBKR's own order log to verify the broker received what we sent
- Forensic tools: when a fill doesn't arrive (broker outage, etc.), the `order_placed` event proves IRAPM did its part
- Crash recovery: on restart, the cycle_attempt records the order; this event corroborates that the broker accepted it before the crash

---

### 4.6 `fill_received`

**When emitted:** In `action_layer._place_one_order`, after the order reaches `OrderStatusValue.FILLED` (either immediately on `place_order()` return or after polling). One event is emitted per `Fill` object in the order's `OrderResult.fills` list. A single order can produce multiple fills (the broker may execute against multiple counterparties at different prices); each gets its own `fill_received` event.

Note: `OrderResult.fills` is the broker's record of what executed. The action layer doesn't currently iterate this list — it only checks the terminal status. Adding fill iteration here is a small additive change to action_layer; the existing logic is preserved.

If an order reaches FILLED status with an empty `fills` list (an edge case that shouldn't happen in well-behaved brokers but is observable in the OrderResult type), a single synthetic `fill_received` event is emitted using the order's total quantity and a price computed from `dollar_amount / quantity`. This shouldn't fire in production but protects against silent loss of fill records.

**Writes to:** `events.jsonl`. Per §2.5, the single-file forever-retention design means fills are preserved indefinitely without any special handling.

**Payload schema:**

```json
{
  "client_order_id": "cycle-ecffd87f-0-FBCG-SELL",
  "broker_order_id": "123456789",
  "fill_id": "fill-abc-001",
  "plan_entry_index": 0,
  "plan_entry_kind": "BUFFER_REFILL",
  "symbol": "FBCG",
  "side": "SELL",
  "quantity_shares": "17.9640",
  "price_dollars": "167.05",
  "fill_dollar_amount": "3001.06",
  "fill_time": "2026-05-14T14:30:01.234567+00:00",
  "fill_index": 0,
  "total_fills_for_order": 1
}
```

| Field | Type | Description |
|---|---|---|
| `client_order_id` | string | The originating order's client_order_id. Primary cross-reference to `order_placed`. |
| `broker_order_id` | string | The broker's order ID. |
| `fill_id` | string | The broker's unique identifier for this specific fill (from `Fill.fill_id`). Distinct from order ID; used to dedupe fills across replays. |
| `plan_entry_index` | integer | Zero-based index of the plan entry that produced this fill. Denormalized from order context for convenient querying. |
| `plan_entry_kind` | string | Kind of the parent plan entry. Denormalized for the same reason. |
| `symbol` | string | Ticker symbol. Denormalized from order. |
| `side` | string | `"BUY"` or `"SELL"`. Denormalized from order. |
| `quantity_shares` | string (Decimal) | Shares filled in THIS specific fill. May be less than the order's quantity if multiple fills are needed. |
| `price_dollars` | string (Decimal) | Actual fill price per share. |
| `fill_dollar_amount` | string (Decimal) | `quantity_shares * price_dollars`. The actual money that moved for this fill. Pre-computed for reader convenience; the multiplication is otherwise repeated on every read. |
| `fill_time` | string (ISO 8601 with timezone) | Wall-clock time the broker reports the fill occurred. From `Fill.fill_time`. |
| `fill_index` | integer | Zero-based index of this fill within the order's fill list. The first fill is `fill_index=0`. |
| `total_fills_for_order` | integer | Total number of fills the order produced. Available because the action layer emits all fill events for an order at once, after the order reaches FILLED status. |

**Notes on field selection:**

This is the central event for cash-flow accounting. Every dollar that moved into or out of IRAPM's portfolio is represented by a `fill_received` event. The reporter's `cash_flow_in` and `cash_flow_out` columns are computed by summing `fill_dollar_amount` for fills in the period, signed by `side` (BUY = cash out from the portfolio perspective, SELL = cash in).

The denormalization of `symbol`, `side`, `plan_entry_kind`, etc. into the fill event is deliberate (§3.5 principle: self-contained events). A reconciliation tool comparing IBKR fills to IRAPM fills doesn't need to consult the originating `order_placed` event — every fill has enough context to stand alone.

**Consumers:**
- Reporter: load-bearing for `cash_flow_in` / `cash_flow_out` columns. Without `fill_received` events, those columns are empty (which is the bug that motivated this entire architectural pass).
- Reconciliation: every IBKR fill must match a `fill_received` event; mismatches are alertable.
- Performance attribution: realized fill prices vs. price-at-decision-time (from `order_placed.price_refresh_dollars`) reveals execution slippage trends.
- Forensic tools: "what did IRAPM actually do in week X" is answered by replaying fills for that week.

### 4.7 `withdrawal_executed`

**When emitted:** In `action_layer._execute_withdrawal`, after every SELL leg of the withdrawal has reached terminal-filled status and the entry is about to return success. One `withdrawal_executed` event per withdrawal entry, regardless of how many SELL legs the withdrawal contained.

The actual ACH disbursement happens broker-side on a 2-business-day settlement schedule (IBKR pulls cash on the 15th). The `withdrawal_executed` event records IRAPM's portion of the withdrawal — the SELLs that fund it. The cash leaving the account via ACH is a downstream broker action observable in subsequent `portfolio_snapshot` events but is not itself a separate IRAPM event.

This is one of three event types with indefinite legal/tax relevance (per §2.5). The single-file forever-retention design preserves it indefinitely without special handling.

**Writes to:** `events.jsonl`.

**Payload schema:**

```json
{
  "withdrawal_dollar_amount": "3000.00",
  "scheduled_ach_date": "2026-05-15",
  "binding_ceiling": null,
  "scheduled_amount_dollars": "3000.00",
  "amount_paid_dollars": "3000.00",
  "was_capped": false,
  "sources": [
    {
      "symbol": "SGOV",
      "dollar_amount": "3000.00",
      "client_order_ids": ["cycle-ecffd87f-0-SGOV-SELL"]
    }
  ],
  "phase": "PHASE_1",
  "income_state_at_withdrawal": "ACTIVE"
}
```

| Field | Type | Description |
|---|---|---|
| `withdrawal_dollar_amount` | string (Decimal) | The amount IRAPM intended to send out this withdrawal. |
| `scheduled_ach_date` | string (ISO date) | The date the broker-side ACH disbursement is scheduled for (typically the 15th of the month). |
| `binding_ceiling` | string or null | If the withdrawal was capped, identifies which ceiling bound the payment: `"phase1_initial"`, `"phase3_floor"`, `"phase3_ceiling"`, `"recovery_freeze"`, etc. Null if the scheduled amount was paid in full. |
| `scheduled_amount_dollars` | string (Decimal) | What the withdrawal would have been before any cap. Equal to `amount_paid_dollars` if not capped. |
| `amount_paid_dollars` | string (Decimal) | The actual amount paid after applying any binding ceiling. Equal to `withdrawal_dollar_amount`; duplicated for explicit readability. |
| `was_capped` | boolean | True iff `binding_ceiling` is non-null and `amount_paid_dollars < scheduled_amount_dollars`. |
| `sources` | array of objects | Per-symbol breakdown of how the withdrawal was funded. Each source records the symbol, dollar amount, and the originating order IDs for cross-reference with `fill_received`. |
| `phase` | string | Phase at the time of withdrawal: `"PHASE_1"`, `"PHASE_2"`, or `"PHASE_3"`. |
| `income_state_at_withdrawal` | string | `"ACTIVE"` (withdrawals that fire are by definition during ACTIVE income state). Captured for schema completeness; future income-state-changing events could make this informative. |

**`sources[i]` sub-object:**

| Field | Type | Description |
|---|---|---|
| `symbol` | string | Source symbol (typically `"SGOV"` for Phase 1 withdrawals; can be Growth symbols for cascade tier-2/3 cases). |
| `dollar_amount` | string (Decimal) | Dollars sourced from this symbol toward the withdrawal. |
| `client_order_ids` | array of strings | The client_order_ids of orders placed to fund this source. Cross-reference with `order_placed` and `fill_received` events for execution details. |

**Notes on field selection:**

The `binding_ceiling` / `was_capped` / `scheduled_amount_dollars` / `amount_paid_dollars` fields together capture the full state of "what did the operator ask for vs. what got paid." This addresses the alert template bug noted in the handoff (`{binding_ceiling}` / `{amount_paid_dollars}` placeholders showing unresolved): the data exists at this event, so any downstream alert or report template can populate the fields from this event without guessing.

`sources` denormalizes the funding breakdown into the withdrawal event so a reader can answer "how was the May 15 2027 withdrawal funded?" without traversing back to `decision_made` or scanning `fill_received` events. The `client_order_ids` array provides the cross-reference for readers that DO want full fill detail.

**Consumers:**
- Reporter: contributes to `cash_flow_out` aggregation (alongside `fill_received` for non-withdrawal transactions); the `flags` column shows `W` for periods containing a `withdrawal_executed` event; capped withdrawals get a more specific flag.
- Tax-record review: year-end total withdrawn = sum of `amount_paid_dollars` over the year. Form 1099-R reconciliation reads this event.
- Operational monitoring: a withdrawal capped by `binding_ceiling = "phase3_floor"` (or similar) is a meaningful operational moment that may warrant operator review.
- Audit: years later, the operator can verify exactly which positions funded which withdrawal.

---

### 4.8 `cb_transition`

**When emitted:** In `action_layer._execute_cb_state_transition`, when the action layer logs a CB state change. This event replicates the information currently written to `cb_transitions.jsonl`; per §1.3, `cb_transitions.jsonl` continues to be written during migration but is retired once consumers migrate to this event.

**Writes to:** `events.jsonl`.

**Payload schema:**

```json
{
  "from_state": "CB_INACTIVE",
  "to_state": "CB1",
  "trigger_reason": "signal",
  "cb2_entry_conditions_after": ["signal"],
  "lookback_value_at_trigger": "-0.0834"
}
```

| Field | Type | Description |
|---|---|---|
| `from_state` | string | Prior CB state value. Values: `"CB_INACTIVE"`, `"CB1"`, `"CB2"`, `"CB1_RECOVERY_STAGE1"`, `"CB2_RECOVERY_STAGE1"`, `"CB1_RECOVERY_STAGE2"`, `"CB2_RECOVERY_STAGE2"`. |
| `to_state` | string | New CB state value (same enumeration as `from_state`). |
| `trigger_reason` | string | What caused the transition. Common values: `"signal"` (lookback signal crossed threshold), `"cb1_timer"` (90-day CB1 → CB2 promotion), `"exit_all_clear_to_inactive:signal"` (recovery confirmed), `"recovery_stage1_signal"`, `"recovery_stage2_signal"`. |
| `cb2_entry_conditions_after` | array of strings | The CB2 entry-conditions list after this transition. Used by the spec's "CB2 entry can occur via signal, cb1_timer, or both" semantics — captures which conditions are currently met. |
| `lookback_value_at_trigger` | string (Decimal) or null | The lookback signal value at the moment of the transition, if the trigger was signal-driven. Null for non-signal-driven transitions (cb1_timer, manual operator-initiated). |

**Consumers:**
- Reporter: contributes to the `cb` column (which displays current CB state) and the `flags` column (`CB1`/`CB2`/`R1`/`R2` markers for the period containing the transition).
- CB freeze evaluation (post-migration): replaces `read_cb_transitions_for_year()`. The annual review's CB freeze counter reads `cb_transition` events for the prior year from this log instead of `cb_transitions.jsonl`.
- Forensic analysis: reconstructing CB state machine behavior across the run.

---

### 4.9 `phase_transition`

**When emitted:** In `action_layer._execute_phase_transition`, after the BUY leg of the transition has completed successfully and the new state has been persisted. One event per transition.

This is one of three event types with indefinite legal/tax relevance (per §2.5). The single-file forever-retention design preserves it indefinitely.

**Writes to:** `events.jsonl`.

**Payload schema:**

```json
{
  "from_phase": "PHASE_1",
  "to_phase": "PHASE_2",
  "is_phase3_activation": false,
  "sells": [
    {"symbol": "PYLD", "dollar_amount": "84062.50", "client_order_ids": ["cycle-...-0-PYLD-SELL"]}
  ],
  "buys": [
    {"symbol": "GBIL", "dollar_amount": "84062.50", "client_order_ids": ["cycle-...-0-GBIL-BUY"]}
  ],
  "phase3_i0_dollars": null,
  "phase3_i0_inputs": null
}
```

| Field | Type | Description |
|---|---|---|
| `from_phase` | string | Phase before transition: `"PHASE_1"` or `"PHASE_2"`. |
| `to_phase` | string | Phase after transition: `"PHASE_2"` or `"PHASE_3"`. |
| `is_phase3_activation` | boolean | True iff this is the Phase 3 latch (the moment of permanent income-bearing transition). |
| `sells` | array of objects | Per-symbol SELL legs. Each entry: `symbol`, `dollar_amount`, `client_order_ids`. |
| `buys` | array of objects | Per-symbol BUY legs. Same shape as `sells`. |
| `phase3_i0_dollars` | string (Decimal) or null | The computed initial monthly withdrawal `I_0` for Phase 3, in dollars. Non-null only when `is_phase3_activation == true`. |
| `phase3_i0_inputs` | object or null | The inputs that produced `phase3_i0_dollars`. Non-null only when `is_phase3_activation == true`. See sub-table. |

**`phase3_i0_inputs` sub-object** (present only on Phase 3 activation):

| Field | Type | Description |
|---|---|---|
| `post_sell_portfolio_dollars` | string (Decimal) | Portfolio value (positions + cash) immediately after the transition's SELL legs and BEFORE the BUY legs. The "P" in the §4.1.1 I_0 calculation. |
| `return_assumption` | string (Decimal) | The assumed annualized return rate from `ruleset.phase3_i0_calc_return_assumption`. |
| `inflation_assumption` | string (Decimal) | The assumed annualized inflation rate from `ruleset.phase3_i0_calc_inflation_assumption`. |
| `horizon_years` | integer | The withdrawal horizon in years from `ruleset.phase3_i0_calc_horizon_years`. |

**Notes on field selection:**

The Phase 3 `I_0` calculation is the load-bearing decision of the entire system's long-term withdrawal trajectory. Capturing the full input set at transition makes the calculation re-derivable from the event log alone, decades later. If a future operator or auditor asks "why is the monthly withdrawal exactly $X?", the answer is in `phase3_i0_inputs` of the Phase 3 activation event.

**Consumers:**
- Reporter: `P2` or `P3` flag in the period containing the transition; the Phase column changes value from this point forward.
- Audit: long-horizon. The Phase 3 activation event is one of the most important records in the entire log — it sets the income trajectory.
- Tax-record review: phase transitions can correlate with significant operational moments (e.g., Roth conversion completion preceding Phase 2).

---

### 4.10 `annual_review_completed`

**When emitted:** When the annual review subroutine completes its inputs gathering and withdrawal-amount computation. Emitted before any plan-level changes are made, so a reader sees this event regardless of whether the review produced an ACH update.

This is one of three event types with indefinite legal/tax relevance (per §2.5). The single-file forever-retention design preserves it indefinitely.

**Writes to:** `events.jsonl`.

**Payload schema:**

```json
{
  "review_year": 2027,
  "phase_at_review": "PHASE_3",
  "cb_freeze_in_effect": false,
  "cb1_plus_days_prior_year": 12,
  "cumulative_cb1_plus_days": 88,
  "cpi_rate_applied": "0.0244",
  "prior_withdrawal_dollars": "3000.00",
  "computed_new_withdrawal_dollars": "3073.20",
  "guardrail_floor_dollars": "3000.00",
  "guardrail_ceiling_dollars": "3500.00",
  "binding_constraint": null,
  "issued_ach_update": true
}
```

| Field | Type | Description |
|---|---|---|
| `review_year` | integer | Calendar year of the review (the year being reviewed; the new withdrawal applies going forward). |
| `phase_at_review` | string | Phase at the moment of review: `"PHASE_1"`, `"PHASE_2"`, or `"PHASE_3"`. The withdrawal-recalculation formula differs by phase. |
| `cb_freeze_in_effect` | boolean | True iff the prior year's CB activity triggered a withdrawal freeze (no increase year-over-year). |
| `cb1_plus_days_prior_year` | integer | Number of days in the prior calendar year IRAPM was in CB1 or CB2. The freeze trigger threshold. |
| `cumulative_cb1_plus_days` | integer | Total CB1-or-worse days across IRAPM's operational life. Long-term operational metric. |
| `cpi_rate_applied` | string (Decimal) | The CPI rate used in this year's recalculation. Sourced from the ruleset (fixed or override). |
| `prior_withdrawal_dollars` | string (Decimal) | The monthly withdrawal in effect BEFORE this review. |
| `computed_new_withdrawal_dollars` | string (Decimal) | The new monthly withdrawal after applying CPI and any guardrails. May equal `prior_withdrawal_dollars` if freeze was in effect. |
| `guardrail_floor_dollars` | string (Decimal) or null | The lower bound on the new withdrawal under the active guardrail policy. Null in phases/configs where no guardrail floor applies. |
| `guardrail_ceiling_dollars` | string (Decimal) or null | The upper bound on the new withdrawal under the active guardrail policy. Null where no ceiling applies. |
| `binding_constraint` | string or null | Identifies which constraint, if any, bound the new withdrawal: `"cb_freeze"`, `"guardrail_floor"`, `"guardrail_ceiling"`, `"phase3_floor"`. Null if the unbounded CPI-adjusted value was used. |
| `issued_ach_update` | boolean | True iff the review's outcome differed from the prior monthly amount enough to warrant updating the broker-side recurring ACH. |

**Notes on field selection:**

This event captures the full input set and output of the year's withdrawal recalculation. Long-horizon audit ("why was my 2032 withdrawal exactly $X?") reads this event for 2031 (which sets 2032's amount) and reproduces the math.

`cb1_plus_days_prior_year` is captured separately from `cb_freeze_in_effect` because the freeze threshold is configurable; capturing the raw count lets a future auditor verify that the threshold was correctly applied.

**Consumers:**
- Reporter: `AR` flag in the period containing the review; the row's annual aggregations may reference this event.
- Audit: year-over-year withdrawal trajectory is fully reconstructable from this event stream alone.
- Tax-record review: ties to year-end totals.

---

### 4.11 `portfolio_snapshot`

**When emitted:** At the end of every weekly cycle, after `cycle_completed`. Captures the portfolio's complete balance-sheet state at that moment. This is the primary input for the reporter's per-row balance columns.

**Writes to:** `events.jsonl`.

**Payload schema:**

```json
{
  "total_aum_dollars": "3304555.43",
  "cash_dollars": "4250.18",
  "cash_unsettled_dollars": "0.00",
  "sgov_buffer_dollars": "71500.00",
  "fi_bucket_dollars": "1620000.00",
  "growth_bucket_dollars": "1608805.25",
  "fi_weight": "0.4912",
  "growth_weight": "0.4877",
  "positions": {
    "FBCG": {
      "quantity_shares": "1200.0000",
      "market_price_dollars": "670.00",
      "market_value_dollars": "804000.00"
    },
    "AVUV": {
      "quantity_shares": "9000.0000",
      "market_price_dollars": "89.42",
      "market_value_dollars": "804805.25"
    },
    "PYLD": {
      "quantity_shares": "0.0000",
      "market_price_dollars": "0.00",
      "market_value_dollars": "0.00"
    },
    "JPIE": {
      "quantity_shares": "150000.0000",
      "market_price_dollars": "10.80",
      "market_value_dollars": "1620000.00"
    },
    "GBIL": {
      "quantity_shares": "0.0000",
      "market_price_dollars": "0.00",
      "market_value_dollars": "0.00"
    },
    "SGOV": {
      "quantity_shares": "700.0000",
      "market_price_dollars": "102.14",
      "market_value_dollars": "71500.00"
    }
  }
}
```

| Field | Type | Description |
|---|---|---|
| `total_aum_dollars` | string (Decimal) | Total assets under management: positions market value + cash. |
| `cash_dollars` | string (Decimal) | Settled cash balance. |
| `cash_unsettled_dollars` | string (Decimal) | Unsettled cash from SELL fills awaiting T+1 settlement. |
| `sgov_buffer_dollars` | string (Decimal) | SGOV position's market value. Tracked separately because the buffer is dollar-targeted, not weight-targeted. |
| `fi_bucket_dollars` | string (Decimal) | Total fixed-income bucket value (sum of PYLD, JPIE in Phase 1; reallocates in Phase 2+). |
| `growth_bucket_dollars` | string (Decimal) | Total growth bucket value (FBCG, AVUV). |
| `fi_weight` | string (Decimal) | FI bucket value / total AUM. |
| `growth_weight` | string (Decimal) | Growth bucket value / total AUM. |
| `positions` | object: symbol → position object | Per-symbol balance-sheet entry. Symbols present in the portfolio with zero balance still appear with zero values, for schema stability. See sub-table. |

**`positions[symbol]` sub-object:**

| Field | Type | Description |
|---|---|---|
| `quantity_shares` | string (Decimal) | Share count, quantized to 4 dp. |
| `market_price_dollars` | string (Decimal) | Per-share market price at the moment of snapshot. |
| `market_value_dollars` | string (Decimal) | `quantity_shares * market_price_dollars`. Pre-computed for reader convenience. |

**Notes on field selection:**

This is the load-bearing event for the reporter's primary value columns (`gross_net_liq`, `cash`, `sgov_buffer`, per-symbol values, FI/Growth bucket totals, weights). Every column in the routine balance reports derives from `portfolio_snapshot`.

Including positions with zero balance is deliberate: it makes the schema stable across phases (PYLD/JPIE drop to zero post-Phase-2; GBIL is zero pre-Phase-2). The reporter renders zero columns as `0.00` rather than blank.

`cash_unsettled_dollars` is captured for forensics. If a fill happens but settlement is pending, the reporter could optionally show "x dollars pending settlement" as a flag. Most operators won't care, but the data is there.

**Consumers:**
- Reporter: primary input for the per-row balance columns. The reporter aggregates over snapshots in a period (or samples the latest within the period) to produce the row.
- Reconciliation: portfolio-level "did IBKR's position list match IRAPM's record at the end of week X" check.
- Forensic analysis: state at any past moment.

---

### 4.12 `state_snapshot`

**When emitted:** At meaningful state-boundary events plus a monthly heartbeat. Specifically:

- After every `cb_transition` event
- After every `phase_transition` event
- After every `annual_review_completed` event
- On the first weekly cycle of each calendar month (the "monthly heartbeat")

Captures a complete snapshot of the operating state — every state machine value, every counter, every pending timer. Distinct from `portfolio_snapshot` (balance sheet) in that this captures the *decision-state* of IRAPM.

The combination of event types up to time T plus the most recent `state_snapshot` before T allows reconstruction of operating state at any past moment without needing to replay all events from t=0.

**Writes to:** `events.jsonl`.

**Payload schema:**

```json
{
  "trigger": "monthly_heartbeat",
  "phase": "PHASE_3",
  "phase3_grace_window_start": null,
  "phase3_grace_pending_abort": null,
  "income_state": "ACTIVE",
  "income_state_changed_at": "2026-01-15T00:00:00+00:00",
  "cb_machine": {
    "state": "CB_INACTIVE",
    "cb1_entered_at": null,
    "cb2_entry_conditions": [],
    "stage1_pending_since": null,
    "stage2_pending_since": null
  },
  "lookback_signal": {
    "status": "OK",
    "value_pct": "-0.0234",
    "computed_at": "2027-03-10T10:00:00+00:00"
  },
  "schedule_state": {
    "phase1": null,
    "phase3": {
      "i_0_dollars": "4250.00",
      "trigger_year": 2025,
      "cpi_rate": "0.0244",
      "frozen_years": [2022]
    }
  },
  "buffer_state": {
    "sgov_target_dollars": "102000.00",
    "monthly_refill_rate_dollars": "8500.00",
    "cash_target_dollars": "5750.18",
    "recomputed_at": "2025-04-13T00:00:00+00:00",
    "refill_delay_started_at": null,
    "last_refill_at": "2027-03-04T10:00:00+00:00"
  },
  "operational_pause": {
    "paused": false,
    "reason": null,
    "started_at": null,
    "consecutive_escalation_count": 0
  },
  "withdrawal_capacity_exhausted": false
}
```

| Field | Type | Description |
|---|---|---|
| `trigger` | string | What caused this snapshot to be emitted: `"cb_transition"`, `"phase_transition"`, `"annual_review_completed"`, or `"monthly_heartbeat"`. |
| `phase` | string | Operating phase. |
| `phase3_grace_window_start` | string (ISO datetime) or null | When the Phase 3 grace window began, if applicable. |
| `phase3_grace_pending_abort` | string (ISO datetime) or null | When the grace window is set to abort (revert) if Phase 3 conditions cease to be met. |
| `income_state` | string | `"ACTIVE"` or `"PAUSED"`. |
| `income_state_changed_at` | string (ISO datetime with timezone) | When `income_state` last changed. |
| `cb_machine` | object | Full CB state machine snapshot. See sub-table. |
| `lookback_signal` | object | Most recent lookback signal value and computation timestamp. |
| `schedule_state` | object | Phase 1 and Phase 3 schedule sub-objects (one is null depending on current phase). |
| `buffer_state` | object | SGOV buffer target, refill rate, last refill timestamp. |
| `operational_pause` | object | Pause state machine: whether paused, why, when started, escalation count. |
| `withdrawal_capacity_exhausted` | boolean | True iff IRAPM has determined withdrawal capacity is exhausted (Phase 3 floor reached). |

**`cb_machine` sub-object:**

| Field | Type | Description |
|---|---|---|
| `state` | string | Current CB state value. |
| `cb1_entered_at` | string (ISO datetime) or null | When CB1 was last entered (used for the 90-day timer). |
| `cb2_entry_conditions` | array of strings | Active CB2 entry conditions (e.g., `["signal", "cb1_timer"]`). |
| `stage1_pending_since` | string (ISO datetime) or null | When stage 1 recovery was pending. |
| `stage2_pending_since` | string (ISO datetime) or null | When stage 2 recovery was pending. |

**Notes on field selection:**

The schema mirrors `OperatingState` (the in-memory state object IRAPM uses). The intent is one-to-one: any field in `OperatingState` that meaningfully drives decisions appears here. If `OperatingState` gains a field in the future, this event's schema gains it too.

The four `trigger` values correspond to the emission points. `monthly_heartbeat` is the "make sure we have a snapshot even when nothing notable happened this month" mechanism. The other three are tied to state-changing events.

`state_snapshot` is distinct from `portfolio_snapshot` because the two have different consumers and different change frequencies. Portfolio values change every cycle (prices move); state values change rarely (most months: nothing changed). Splitting them avoids logging unchanged state every week.

**Consumers:**
- Forensic analysis (primary): "what did IRAPM think the world looked like at time T" — read the most recent `state_snapshot` before T, then replay subsequent events.
- Operator review: monthly state snapshots provide a baseline view of "what's my system currently doing."
- Crash recovery (future, if needed): supplements broker-side reconstruction; not currently on the critical path.

---

### 4.13 `alert_emitted`

**When emitted:** When `alerter.dispatch()` is called, regardless of whether the alert's email/SMS channels succeed. The event records IRAPM's intent to alert plus the dispatch outcome.

This event replaces the current stdout-only alert log path. In production with a non-stdout alerter (email, SMS, webhook), the event log becomes the canonical record of which alerts fired when.

**Writes to:** `events.jsonl`.

**Payload schema:**

```json
{
  "alert_id": "withdrawal_executed",
  "context": {
    "binding_ceiling": "phase3_ceiling",
    "amount_paid_dollars": "3500.00",
    "scheduled_amount_dollars": "3650.00"
  },
  "email_ok": true,
  "email_error": null,
  "sms_ok": true,
  "sms_error": null,
  "deduped": false
}
```

| Field | Type | Description |
|---|---|---|
| `alert_id` | string | The alert template identifier (e.g., `"withdrawal_executed"`, `"cb1_triggered"`, `"operational_pause"`, `"external_activity_overlap"`). |
| `context` | object | The variables IRAPM passed to the alert template. Captures everything that fills in template placeholders. Field names and types vary by alert_id. |
| `email_ok` | boolean | True iff the email channel dispatch succeeded. |
| `email_error` | string or null | Error message if `email_ok == false`. Null on success. |
| `sms_ok` | boolean | True iff the SMS channel dispatch succeeded. |
| `sms_error` | string or null | Error message if `sms_ok == false`. Null on success. |
| `deduped` | boolean | True iff the alerter recognized this as a duplicate of a recent alert and suppressed the dispatch. |

**Notes on field selection:**

`context` is a free-form object. Different alert IDs have different template variables; capturing the dictionary directly preserves all of them without forcing a unified schema across all alert types.

The dual-channel success/error fields exist because email and SMS are independent. An alert can succeed on one channel and fail on the other (network blip, SMS gateway rate-limit, etc.). Capturing both states means a future audit can answer "did the operator definitely see this alert?" — yes if at least one channel succeeded; ambiguous if both failed; certain if `deduped == true` because a recent identical alert succeeded.

**Consumers:**
- Reporter: contributes to the `flags` column where alert types map to recognizable flags (`PAUSE` for operational_pause alerts, etc.).
- Operational monitoring: an `alert_emitted` with `email_ok == false AND sms_ok == false AND deduped == false` is a "the alert tried but failed" condition that itself warrants attention.
- Audit: "what alerts fired in year X" is answered by filtering this event type.

---

---

## 5. Writer API contract

### 5.1 Module and location

The event log writer lives at `event_log.py` at the repo root, as a peer of `cycle.py`, `action_layer.py`, and `irapm_driver.py`. This placement matches the import direction of the rest of IRAPM (decision layer / action layer / cycle driver all sit at repo root; modules deeper in the tree depend up, not the reverse) and avoids creating a new package boundary for a single small module.

The writer has no internal state and no class-level instance. It is a stateless module that exposes a single public function plus a small handful of helpers. Each call opens the file, appends one event, and closes the file.

### 5.2 Public API

```python
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

    Returns the event_id of the appended event (for cross-reference
    by callers that emit linked events).
    """
```

| Parameter | Type | Description |
|---|---|---|
| `paths` | `Paths` | The IRAPM paths object. The writer derives the events.jsonl location from `paths.state_dir`. |
| `event_type` | `str` | One of the documented event types from Section 4. Validated against the known set; an unknown type raises `ValueError` at call time (prevents silent typos). |
| `payload` | `dict` | The event-specific payload. Caller is responsible for constructing the right shape per Section 4's schemas. The writer does NOT validate payload structure beyond verifying it serializes to JSON. |
| `now` | `datetime` | The wall-clock instant of emission. Used as `emitted_at`. Must be timezone-aware (per §3.2); a naive datetime raises `ValueError`. |
| `source_cycle_id` | `Optional[str]` | The cycle UUID this event belongs to, or `None` for events emitted outside any cycle. |
| `in_sim_timestamp` | `Optional[datetime]` | The simulated/operational time the event occurred. In production, this equals `now`. In simulation, this is the in-sim calendar time. If `None`, defaults to `now`. Must be timezone-aware if provided. |

**Returns:** The generated `event_id` string. Callers that emit linked events (e.g., a `fill_received` event referencing its `order_placed`) can capture the returned ID and pass it in subsequent payloads.

### 5.3 Atomicity and crash safety

The writer's `append()` implementation:

1. Constructs the full event envelope (event_id, schema_version, event_type, timestamp, emitted_at, source_cycle_id, payload) as a Python dict.
2. Serializes the dict to a JSON string using a strict serializer that:
   - Encodes Decimals as JSON strings (not numbers — see §3.5)
   - Rejects NaN and Infinity (these have no valid round-trip)
   - Uses no indentation (single-line JSON)
3. Appends the JSON string followed by exactly one LF byte to `paths.state_dir / events.jsonl`.
4. Calls `fsync()` on the file descriptor before closing.
5. Returns the event_id.

**Append semantics.** The writer uses `open(path, "ab")` (append binary mode). On POSIX systems, this guarantees that the OS atomically appends to the file's current end-of-file, even across concurrent writers from other processes. The atomicity bound is `PIPE_BUF` bytes (typically 4096 on Linux, 512 on the POSIX minimum); all event records are designed to fit well under this bound.

**fsync policy.** Every `append()` calls `fsync()` before returning. This ensures the event is durable on disk before the next IRAPM operation proceeds. The cost is roughly 1-5ms per call on commodity SSDs. Over a 20-year baseline scenario (~50,000 events), the fsync overhead is well under 5 minutes total — negligible relative to other simulation work.

This policy is conservative: fsync on every event eliminates a class of edge cases where a crash between write and fsync would lose the event silently. The performance cost is small enough that the simplicity wins.

**Single-writer assumption.** IRAPM is a single-process application; only one IRAPM instance writes to a given state directory at a time. The harness's CycleAttempt mechanism guarantees this (`cycle_attempt.json` is a lockfile). The writer does not need to handle concurrent IRAPM writes to the same file.

The writer DOES tolerate concurrent reads while writing — readers per §6 are tolerant of partial trailing lines. A reader scanning the file mid-write may see N complete records and at most one in-progress (incomplete) trailing line; the reader's contract is to ignore the trailing partial line.

### 5.4 Error handling

The writer's error handling philosophy: **a logging failure must never propagate up to break a cycle.**

The reasoning: the event log is operator-facing infrastructure (per §1.1). It is not on the runtime critical path of IRAPM's decision and execution. If the disk fills, if file permissions break, if the filesystem hangs — the event log write fails, but IRAPM's cycle work must continue. Failing to log is regrettable; failing to execute a trade because we couldn't log a record about it is unacceptable.

Implementation:

- The `append()` function catches all `OSError` (disk full, permission denied, filesystem unmounted) and all `JSONEncodeError` (invalid payload) exceptions.
- On caught exception, it logs the failure to Python's standard `logging` module at `WARNING` level with the exception detail.
- It returns the event_id that *would* have been written, so callers don't need exception-handling boilerplate around every emission.
- The cycle proceeds normally.

The trade-off: a disk-full condition produces missing event records and a warning in stderr. The operator notices stderr warnings or, more likely, notices that the reports stop reflecting new activity. Both are recoverable. The alternative — letting the exception propagate — would mean a disk-full condition kills the running cycle, which is far worse.

Validation errors are different from runtime errors:

- Calling `append()` with an unknown `event_type` is a programmer error and raises `ValueError` synchronously. These are caught in testing, not at runtime.
- Calling `append()` with a naive (non-timezone-aware) `datetime` raises `ValueError` synchronously. Same rationale.
- Calling `append()` with a payload that contains a value the JSON serializer cannot handle (e.g., a `datetime` not pre-converted to ISO string, a `Decimal` not pre-converted to string) raises `TypeError` synchronously.

These programmer errors fail loudly during development and testing. They should never reach production. The "logging failure must not break a cycle" rule applies only to runtime conditions outside the programmer's control (disk, permissions, filesystem state).

### 5.5 Helper utilities

The writer module exposes a small number of helpers for common patterns:

```python
def append_cycle_started(paths: Paths, cycle_id: str, cycle_type: str,
                         is_restart: bool, box_id: str, client_id: int,
                         now: datetime) -> str:
    """Helper: emit a cycle_started event with the standard payload."""

def append_cycle_completed(paths: Paths, cycle_id: str, ...) -> str:
    """Helper: emit a cycle_completed event."""

# ... one per event type
```

These helpers exist to centralize the payload-shape construction. Callers in `cycle.py`, `action_layer.py`, etc. invoke these helpers rather than constructing payload dicts inline. This keeps the emission sites readable and ensures every emission of a given event type produces a consistently-shaped payload.

If a future event type's payload changes (a new optional field is added per §3.4's compatibility rules), the helper signature changes in exactly one place, and call sites either pass the new argument or leave it at its default.

### 5.6 Testing the writer

The writer module ships with focused unit tests covering:

- Round-trip: write an event, read the file, verify the parsed event matches what was written.
- Decimal preservation: write payload with various Decimal values; verify they round-trip without precision loss.
- Timezone enforcement: writing a naive datetime raises ValueError.
- Concurrent-read tolerance: simulate a partial line at end-of-file; verify the reader contract handles it.
- Disk-failure handling: monkey-patch `open()` to raise OSError; verify `append()` returns normally and logs a warning.

These tests live alongside other harness tests and run as part of the standard test suite.

---

## 6. Reader patterns

The event log is consumed by several distinct readers, each with different access patterns. This section documents the patterns and the invariants every reader must respect. A reference implementation of each pattern lives in the same `event_log.py` module that contains the writer.

### 6.1 Universal reader invariants

Every reader, regardless of access pattern, must:

1. **Tolerate a partial trailing line.** Per §2.3, a crashed write can leave one in-progress line at end-of-file lacking the terminating LF. Readers must detect this (parse failure on the last line) and silently skip it. The line is not data; it is corruption.

2. **Tolerate records with unknown event_types.** Per §3.4, future schema additions may introduce new event types. A reader written today must not crash when encountering an event_type it doesn't recognize — it should skip the record and continue.

3. **Tolerate records with newer schema versions.** Per §3.4, a 1.x reader processes 1.x events. If a 2.x event appears, the reader skips it with a warning. (At time of writing this is theoretical; only schema 1.0 exists.)

4. **Never modify the file.** Readers are pure consumers. The only process that writes to events.jsonl is `event_log.append()`. Any reader that mutates the file violates the system-of-record invariant.

### 6.2 Pattern: stream entire log

Used by: reporter generating a full simulation report, audit tooling regenerating year-end totals from scratch.

```python
def iter_events(paths: Paths) -> Iterator[dict]:
    """Yield every event in the log in append order. Skips partial
    trailing line and records that fail JSON parse."""
```

The reader opens the file, reads line-by-line, parses each line as JSON, and yields the result. JSON parse failures (corrupt line, partial trailing line) are logged at DEBUG level and silently skipped.

Performance: the reporter for a 20-year simulation reads ~50,000 events in well under a second on commodity hardware. No optimization needed at this scale.

### 6.3 Pattern: filter by event type

Used by: the reporter filtering for specific event types when computing per-row data, operator-facing "show me only X events" queries, audit-specific drill-downs.

```python
def iter_events_of_type(paths: Paths, event_type: str) -> Iterator[dict]:
    """Yield events matching the given event_type, in append order."""
```

This is a thin wrapper around `iter_events` with a type filter. It exists as a separate function for readability of the caller — `iter_events_of_type(paths, "withdrawal_executed")` is more self-documenting than the equivalent comprehension.

For multi-type filters (e.g., "all order_placed and fill_received events"), callers use `iter_events` with their own filter logic.

### 6.4 Pattern: reconstruct state at time T

Used by: forensic deep-dives ("what did IRAPM think on 2027-04-15?"), reporter rows that need a state context at a specific moment.

The reconstruction technique exploits the `state_snapshot` event type (§4.12). To compute the operating state at time T:

1. Find the most recent `state_snapshot` event with `timestamp <= T`.
2. Apply, in order, every event since that snapshot that mutates state (cb_transition, phase_transition, annual_review_completed, etc.) to produce the state at T.

```python
def state_at(paths: Paths, t: datetime) -> dict:
    """Return the reconstructed operating state at time t."""
```

The `state_snapshot` monthly heartbeat (§4.12) guarantees the snapshot is never more than one month behind any T, bounding reconstruction work. For the most common case (T is the current moment), the most recent snapshot is at most days old.

### 6.5 Pattern: tail / incremental read

Used by: future operator-facing live-monitoring tooling, replication-aware readers (when replication is built).

```python
def iter_events_from(paths: Paths, after_event_id: str) -> Iterator[dict]:
    """Yield events appended after `after_event_id`, in append order.

    The caller passes the event_id of the most recent event it has
    already processed. Returns events strictly after that point.
    """
```

This pattern enables incremental processing: a reader keeps track of the last event_id it has seen, and on the next call asks for everything since then. Useful for any consumer that wants to process new events as they arrive rather than re-reading the whole file.

The current implementation scans from the start looking for `after_event_id`, then yields subsequent events. For the file sizes we expect (≤15 MB), this is fast enough. If future scale changes require it, a positional index can be maintained as a sibling file.

### 6.6 Performance characteristics

All readers are linear in the size of the event log. No reader maintains an index, a cache, or any persistent state. This is deliberate: at the scales IRAPM operates (sub-megabyte to ~15 MB log files), linear scans are fast enough, and any index introduces its own consistency concerns.

If a future workload requires sub-linear reads, the right answer is to maintain a positional index file alongside `events.jsonl`. The design of that index is out of scope for this specification; the writer's append-only contract means any index design will work.

---

## 7. Migration plan

### 7.1 Migration philosophy

The event log is introduced **additively**. Existing logs (`cycle.jsonl` and `cb_transitions.jsonl`) continue to be written as they are today. The new event log starts being written alongside them. No existing consumer is forced to change immediately.

Consumers migrate to the event log one at a time, on schedules driven by other work that touches them. When every consumer has migrated and the existing logs have no active readers, the legacy log writers are deleted in a single explicit cleanup commit.

This staged approach avoids a "big bang" migration that could break working consumers and ensures the new event log is proven against real workloads before it becomes a hard dependency.

### 7.2 Phase 1: Land the event log writer (this session and the next)

Work in this phase:
- Implement `event_log.py` per Section 5
- Add emission calls at every integration point listed in Section 4
- Run the baseline scenario end-to-end; verify the event log contains plausible content
- Add focused unit tests for the writer
- Document the writer in inline comments and the spec

Acceptance criteria:
- A baseline scenario run produces an `events.jsonl` file with the expected event sequence
- Existing tests still pass (no regressions in cycle.jsonl or cb_transitions.jsonl)
- The event log is reviewable by hand for a few cycles to confirm payload shape

At the end of Phase 1, the event log exists but has no consumers. The reporter, expectations checker, failure tracker, and annual review's CB freeze counter still read the legacy logs.

### 7.3 Phase 2: Migrate the reporter (the session after Phase 1)

The reporter is the highest-value consumer. It is also greenfield code (per the operator's "delete IPMS output, build new reporter" decision). So the reporter is built against the event log from day one — it never reads the legacy logs.

Work in this phase:
- Draft `REPORT_SPEC.md` per the discussion in this session
- Implement the reporter against the event log
- Run the baseline scenario; verify the reporter produces a sensible text report
- Compare output against expectations and refine

Acceptance criteria:
- The reporter produces both simulation-mode output (single file, annual + monthly sections) and the production-mode skeleton (weekly snapshot file)
- The output contains correct values for `cash_flow_in` and `cash_flow_out` (the bug that motivated this whole architectural pass)
- The reporter has no dependency on cycle.jsonl, cb_transitions.jsonl, or any IPMS output module

### 7.4 Phase 3: Migrate the other consumers (future sessions)

Three consumers still read legacy logs after Phase 2:

1. **Harness failure tracker** (`irapm_driver.CycleFailureTracker`) — reads cycle.jsonl to detect in-cycle halts.
2. **Expectations checker** (`irapm_driver.check_expectations`) — reads cycle.jsonl to count cycles and detect halts.
3. **Annual review CB freeze counter** (`persistence.read_cb_transitions_for_year`) — reads cb_transitions.jsonl.

Each migrates independently. The migration pattern is:
- Switch the reader to use the event log via `iter_events_of_type()`
- Run tests to confirm behavior is unchanged
- Land the change

After all three consumers have migrated, the legacy logs have no remaining readers.

### 7.5 Phase 4: Delete legacy log writers

Once Phase 3 is complete:

- Remove the `append_cycle_log()` call site in `cycle.py`
- Remove the `append_cb_transition()` call site in `action_layer.py`
- Delete the now-unused `read_cb_transitions_for_year()` and any related helpers
- Update any documentation referring to cycle.jsonl or cb_transitions.jsonl

The legacy log files themselves are not deleted from existing run directories — they remain as historical artifacts of pre-migration runs. New runs simply don't produce them.

### 7.6 Capability freeze on legacy logs (during migration)

Between now and Phase 4, the legacy logs are **frozen in capability**:

- No new fields may be added to cycle.jsonl records
- No new event types may be added to cb_transitions.jsonl
- No new use cases may be built on either file

If a new capability is needed, it goes in the event log only. This rule prevents the migration from becoming a perpetual "two parallel systems that both keep growing" state.

The rule applies even within Phase 1, before any consumer has migrated. The discipline is: from the moment this spec lands, the legacy logs are end-of-life.

---

## 8. Open questions and decisions deferred

This section catalogs decisions intentionally not made by this specification, and questions that may need answers later.

### 8.1 Replication mechanism

The event log is designed to be replication-friendly (append-only, no in-place mutation, single file) but this specification does NOT define the replication mechanism between master and slave boxes.

**Why deferred:** Per §1.1, the event log is not on the runtime critical path. The slave box reconstructs order state from IBKR directly when it self-promotes. Replication is for the operator's benefit (forensic continuity, reports continue uninterrupted) rather than a system requirement. Designing replication can happen any time without blocking the event log itself.

**When to revisit:** When two-box operation is configured (estimated mid-2026 per the CL260 hardware schedule). At that point a replication design document specifies the mechanism (likely append-mode rsync over an SSH tunnel between boxes), the cadence, the failure-mode behavior, and any tooling for operator-side reconciliation.

### 8.2 Production vs. simulator emission differences

This specification treats production and simulator runs as emitting identical event types with identical schemas. This is correct in principle, but a few practical questions may arise:

- **Should simulator events carry a `simulation: true` flag?** Currently no — the events are identical, and the operator can distinguish simulation runs by which `paths.state_dir` they live in.
- **Should the simulator emit any events the production system doesn't?** Currently no — every event type is meaningful in both contexts.

**When to revisit:** If a future use case wants to filter simulator events out of cross-run analysis, adding a flag at that point is backward-compatible (per §3.4).

### 8.3 The reporter specification

This specification refers throughout to "the reporter" without defining what reports it produces. The reporter's output format — column set, fixed-width vs. tab-separated, file naming, retention in production — is intentionally out of scope here.

**Why deferred:** The event log spec was about getting the data substrate right. The reporter spec is about getting the operator's interface right. They are separable design problems.

**When to revisit:** Immediately after this specification lands. The next document is `REPORT_SPEC.md` per the discussion in this session.

### 8.4 Daily-token cycle event emission

The current draft of Section 4 emits `cycle_started` and `cycle_completed` for daily-token cycles, but does not emit any token-specific event type. Daily-token cycles run six days a week; in 30 years of operation they produce roughly 10,000 cycle events.

**Open question:** Should there be a `token_observation_recorded` event capturing the per-cycle token state, separate from the cycle envelope? Currently the answer is no — the token observation is internal coordination state, not operationally significant unless it changes (which is captured by `alert_emitted` for token state changes).

**When to revisit:** If a token-state forensic question arises that can't be answered from `cycle_completed` plus `alert_emitted` alone.

### 8.5 Recurring ACH state in events

The current draft does not include an event type for recurring ACH state changes (initial configuration, amount updates triggered by annual review, manual operator adjustments). The information is partly captured by `annual_review_completed.issued_ach_update` (boolean) but the actual ACH amount transition is not in any event.

**Open question:** Should there be an `ach_schedule_updated` event capturing every change to the broker-side recurring ACH amount, who/what triggered it, and the before/after values?

**Argument for adding it:** ACH amount is a high-stakes operational value. A long-horizon audit may want a clean event stream of "every change to the ACH amount over IRAPM's life."

**Argument against:** Most ACH updates flow from annual reviews; the annual_review_completed event already captures the input set and outcome.

**When to revisit:** Likely during the Phase 2 work when the reporter is built. If the reporter needs to display ACH history and finds the current data inadequate, a new event type would land then.

### 8.6 Operational pause event emission

The `state_snapshot` event captures the `operational_pause` field, but there is no dedicated `operational_pause_started` or `operational_pause_cleared` event. State changes are detectable by comparing consecutive `state_snapshot` events, but a dedicated event type would be more discoverable.

**Open question:** Add `operational_pause_started` and `operational_pause_cleared` event types?

**Argument for adding them:** Pauses are unusual operational events worth surfacing prominently in the reporter and in any monitoring system. A dedicated event type makes them trivially queryable.

**Argument against:** Pauses are accompanied by an `alert_emitted` event by current IRAPM behavior, and the state is in `state_snapshot`. Adding a dedicated event type is duplicative.

**When to revisit:** During the reporter work. If pauses need to surface in the `flags` column, a dedicated event type may simplify the reporter's logic.

---

