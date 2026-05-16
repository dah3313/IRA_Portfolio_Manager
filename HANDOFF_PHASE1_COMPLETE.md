# IRAPM Phase 1 — Complete

**Status:** Phase 1 of the IRAPM event-log migration is **complete**, including Gap 1 (withdrawal cap surfacing).
**Date:** 2026-05-15
**Baseline:** `baseline_20yr_phase3_2016` produces terminal AUM $3,304,781.68 with all expectations passing.

This handoff is the entry point for whoever (Claude or human) picks up Phase 2. Read it first.
Then read `REPORT_DECISIONS.md` (Phase 2 design contract) and `EVENT_LOG_SPEC.md` §4 (the data substrate). Those three documents are the full context — this conversation history is not required.

---

## What's done

All 13 event types from EVENT_LOG_SPEC §4 are emitting correctly. Confirmed counts from a clean 20-year baseline run (post-Gap-1 closure):

| Event | Count | Source |
|---|---|---|
| cycle_started | 7,306 | cycle.py (weekly + daily-token) |
| cycle_completed | 7,306 | cycle.py |
| cycle_halted | 0 | (absence is correct for clean baseline; spec §4.3) |
| decision_made | 1,044 | cycle.py, after decide() |
| order_placed | 566 | action_layer._place_one_order |
| fill_received | 566 | action_layer._place_one_order, per Fill |
| withdrawal_executed | 240 | action_layer._execute_withdrawal (now with real cap data) |
| cb_transition | 28 | action_layer._execute_cb_state_transition |
| phase_transition | 1 | action_layer._execute_phase_transition |
| annual_review_completed | 20 | cycle.py, after decide() when ar_decision set |
| portfolio_snapshot | 1,044 | cycle.py, end of weekly cycle |
| state_snapshot | 290 | 4 triggers: monthly_heartbeat (241), cb (28), phase (1), annual_review (20) |
| alert_emitted | 402 | cycle.py + action_layer alerter call sites |

Total: 18,813 events, 10.4 MB. Zero parse errors. All cycle_started / cycle_completed pairs balance.

## Files modified across Phase 1 (final state on disk at C:\portfolio\)

| File | Lines | Status |
|---|---|---|
| event_log.py | 774 | Writer + 13 helpers, spec-conformant payloads |
| persistence.py | (existing + events_log accessor) | Phase 1 Step 1 |
| test_event_log.py | (existing + updated tests) | 60 tests pass |
| clock.py | (existing + now_utc Protocol) | Phase 1 Step 2 (timezone seam) |
| decision_layer.py | 1,045 | CycleSnapshotData + AnnualReviewDecision surfaced on DecisionOutput |
| cycle.py | 902 | All cycle.py-side emissions + _dispatch_and_log_alert helper |
| action_layer.py | 1,430 | All action_layer-side emissions, _to_aware_utc helper, _emit_state_snapshot, _emit_fills, _QuantityRefresh refactor |
| plan_model.py | 353 | WithdrawalEntry gained binding_ceiling/scheduled_amount_dollars/was_capped (Gap 1) |
| withdrawal.py | 568 | CeilingResult widened, decide_withdrawal populates new fields + correct alert context keys (Gap 1) |
| EVENT_LOG_SPEC.md | 1,395 | Spec-vs-code alignment (G/C/F naming, combined_token_state booleans) |
| REPORT_DECISIONS.md | 285 | Phase 2 design decisions including simulation-report ruleset section |

## Architectural properties preserved

- **annual_review.py stays a pure function** — no I/O introduced. The decision is surfaced on DecisionOutput; cycle.py is the I/O seam.
- **decision_layer.decide() stays a pure function** — same reason.
- **alerter.py was NOT modified** — alerter is a pure dispatcher. Event log emission lives at the call sites (cycle.py and action_layer.py).
- **Existing logs (cycle.jsonl, cb_transitions.jsonl) still written in parallel** — per spec §1.3 migration plan. Phase 5 removes them after Phase 4 migrates the harness's failure tracker and check_expectations.

## Gap 1: CLOSED

The withdrawal cap data (binding_ceiling, scheduled_amount_dollars, was_capped) now flows from `decide_withdrawal` through `WithdrawalEntry` to the `withdrawal_executed` event. Concretely:

- **WithdrawalEntry** carries the **spec-form** binding_ceiling values: `"guardrail"`, `"dollar_cap"`, `"both"`, or `None`. This is what the event log records.
- **The alert context** uses the **template-form** values: `"portfolio_percent"`, `"dollar"`, `"both"`. Decide_withdrawal translates from spec form to template form when building the alert context. Both vocabularies preserved.
- **The broken SMS** that emitted literal `{binding_ceiling}` placeholders for multiple sessions is fixed. Confirmed in the post-Gap-1 baseline run: SMS now renders e.g. `IRAPM: Ph3 withdrawal capped at dollar ceiling. Paid $4000.00 (sched $7727.10). See email.`

**Symbol mapping for the Phase 2 reporter:**
- `binding_ceiling: "guardrail"` → **G** (Phase 3 portfolio-percent clamp — the spec's central Guardrail)
- `binding_ceiling: "dollar_cap"` → **C** (Phase 3 inflation-indexed dollar Cap)
- `binding_constraint: "cpi_freeze"` in annual_review_completed → **F** (CPI-increase freeze fired by prior-year CB days exceeding threshold)

## Gap 2: Still open (low priority)

One CB1→CB1 self-transition appears in the cb_transition stream. Not a halt, not a financial issue, but unusual. Could be a CB machine re-evaluation that should have been a no-op, or could be intentional (re-arming a confirmation counter). Worth investigating but not blocking.

---

## Phase 2 — Starting the next session

The data substrate is complete. Phase 2 builds the reporter that consumes events.jsonl. This section is the kickoff for a fresh chat.

### Read these three files first

In this order:

1. **This file** (`HANDOFF_PHASE1_COMPLETE.md`) — you've already read it. Status, what's done, what to build.
2. **`REPORT_DECISIONS.md`** — the Phase 2 design contract. Captures every decision already made about the reporter's column set, file types, lifecycles, simulation-report content (including the ruleset header section), and module API. Treat it as authoritative; if you find a need to deviate, surface the conflict to the operator rather than silently overriding.
3. **`EVENT_LOG_SPEC.md` §4** — the catalog of all 13 event types. Every field the reporter reads is documented there. This is the schema you parse from events.jsonl.

The conversation history that produced Phase 1 is not needed. The three documents above contain everything required.

### What to build

1. **Draft `REPORT_SPEC.md`** — convert REPORT_DECISIONS.md from "design rationale" into "implementation contract." Where REPORT_DECISIONS describes behavior in prose, REPORT_SPEC pins down: exact column widths, exact header format, exact annotation symbols and their meanings, exact file-naming rules, exact behavior at year boundaries, exact handling of partial data (incomplete months, missing snapshots), exact error behavior. Treat it the way EVENT_LOG_SPEC.md treats the event log — the contract that report.py implements.

2. **Implement `report.py`** — pure consumer of events.jsonl. Four public functions per REPORT_DECISIONS:
   - `write_current_status(state_dir, output_path)` — overwrites every weekly cycle
   - `append_monthly_row_to_year_file(state_dir, output_dir, month)` — adds month's row to the year file
   - `close_year_file(state_dir, output_dir, year)` — appends annual summary
   - `write_simulation_report(state_dir, output_path)` — single-file simulator output

3. **Wire the reporter into cycle.py and ipms.engine** — four invocations per weekly cycle in production (always status; first-of-month adds monthly row; first-of-January closes prior year + prunes); one invocation at end-of-run in simulation.

### Three things that matter for getting this right

These are lessons from Phase 1 worth carrying forward:

1. **Pure functions stay pure.** The reporter consumes events. It does not call into IRAPM's runtime modules. It does not import from cycle.py, action_layer.py, or decision_layer.py. The event log is the contract. (`Ruleset` is the one exception — the simulation report's ruleset section needs the in-memory Ruleset object to capture scenario-specific overrides. That's the only IRAPM module the reporter touches, and it's read-only.)

2. **Attribute audit before emission diffs.** Any new code that references module-level names from another module (a Pydantic field, an enum value, a class attribute) — grep that file's imports and confirm each name is reachable BEFORE running anything. Phase 1 lost a session to a missing `Phase` import we'd have caught in 30 seconds. The reporter will read many Pydantic models via `model_dump(mode="json")`; verify field names against the actual model definitions, not your memory of them.

3. **Spec terminology is authoritative.** Where the spec disagrees with the code (helper signatures, value names), the spec wins and the code gets aligned. Phase 1 had two such cases: append_cb_transition's payload missed cb2_entry_conditions_after, and append_alert_emitted's payload was alert_template/severity/message instead of email_ok/sms_ok/deduped. Both were fixed by aligning the code to the spec.

### The "What's left" picture

After Phase 2:

- **Phase 3** — delete the IPMS output package (the legacy parameters.md, balances_*.md, alerts.md, etc. in runs/{...}/). The new reporter produces a single combined report file plus the current_status.txt and per-year files. The old per-event MD files become redundant. Phase 3 is just a "rm" plus removing the IPMS output call site.

- **Phase 4** — migrate three remaining legacy-log consumers to events.jsonl: harness CycleFailureTracker, check_expectations, annual_review's CB freeze counter (`read_cb_transitions_for_year`). Each is independent; small and focused.

- **Phase 5** — delete the legacy cycle.jsonl and cb_transitions.jsonl write paths in cycle.py and action_layer.py. Only safe after Phase 4 closes all readers.

These are queued for after Phase 2 lands. Phase 2 is the load-bearing one — it's what finally produces operator-readable reports from the event log we just built.

---

## Honest assessment of this session's work

Three bugs caught during integration:

1. **Missing Phase import** in cycle.py's annual_review emission block. Caused 20 halted cycles in the first integration run. Root cause: I added new code that referenced `Phase` without checking the file's import list. Fix was one line.

2. **event_log.append_alert_emitted signature mismatch** with EVENT_LOG_SPEC §4.13. Helper had alert_template/severity/message; spec required email_ok/sms_ok/email_error/sms_error/deduped. Fixed by aligning helper to spec.

3. **Three-round error on binding_ceiling terminology** before getting it right. First called it "floor" (wrong direction), then "ceiling" (mechanically correct but missed that the system's name for it is "Guardrail"). The operator's mental model is the authoritative one; the spec's existing function names (`apply_phase3_ceilings`) used generic terminology that didn't carry the operator's domain knowledge. The reporter symbols (G/C/F) reflect the operator's vocabulary.

Discipline that emerged from these:
- Grep imports before running code that references module-level names from another module.
- Treat the spec as authoritative; when helpers and spec disagree, align helpers to spec.
- When naming things that exist in both code and operator UX, ask the operator what they call it before picking a name from the code.
