# IRAPM Implementation Changelog

This file tracks production code changes. Spec-level versioning lives in
`SPEC_CHANGELOG.md`; design-decision rationale lives in `DECISIONS.md`;
this file is for code: what was fixed, when, what tests cover it.

Conventions:
- One section per dated update. Latest at top.
- Each entry: short title, what code changed (file paths), what tests
  cover it, what user-visible behavior changed, brief root-cause note
  when non-obvious.
- Tests are listed for verification continuity — if you can't find
  them later, they were probably renamed; check git log.
- No long-form rationale here. Rationale goes in DECISIONS.md
  (D-IMPL-N entries) or in the file's docstring.

---

## 2026-05-14 — Issue #4 fix + observability architecture (1 fix, 11 tests added, 2 specs drafted)

Resolves the outstanding halt from 2026-05-13's session (FBCG
zero-quantity SELL on 2005-05-04) and lands the design for a complete
rebuild of IRAPM's observability and reporting subsystems. The fix is
code; the architecture work is two design documents — no implementation
started yet for the new subsystems.

Pre-fix run state: baseline_20yr_phase3_2016.yaml ran end-to-end with
1 halt on 2005-05-04 (FBCG zero-quantity SELL absorbed by harness as
synthetic halted record; downstream cycles unaffected).

Post-fix run state: zero halts. Terminal AUM moved $3,304,555.43 →
$3,304,781.68 (the $226 delta reflects the previously-failed trades now
executing and compounding over the remaining 20-year window).

### Fixed

- **SGOV refill drift floors (Issue #4).**
  `sgov_refill.py`: two related bugs producing zero-share-quantity
  SourceLines at the action layer.

  Bug A (latent overweight branch): `decide_buffer_refill` classified
  any Growth position with `cur_v > target_v` as overweight without a
  meaningful-drift floor. Pennies of drift could produce near-zero
  refill allocations rounding to zero shares.

  Bug B (the actual 2005-05-04 producer): when the buffer deficit was
  a few cents (SGOV intraday price drift relative to target), the
  proportional-fallback split produced sub-cent SourceLines for each
  Growth position — same halt class.

  Fix A: meaningful-drift gate on overweight detection. A position
  counts as overweight only when its surplus ≥
  `core_total * rebalance_absolute_threshold_rate` (reuses the
  existing 5% drift yardstick from the 5/25 rebalancer; no new
  tunable).

  Fix B: noise-floor gate on buffer deficit itself. Refill is planned
  only when `deficit ≥ buffer_target_dollars * 0.02` (2% of target,
  hardcoded; scales with `buffer_target` which scales with CPI; well
  below the monthly refill rate of ~8.3% of target).

  `sgov_refill.py`: added `rebalance_absolute_threshold_rate` field to
  `SGOVRefillInputs`; added both drift-floor gates in `decide_buffer_refill`;
  updated docstring.
  `decision_layer.py`: pass `ruleset.rebalance_absolute_threshold_rate`
  through at the one call site.
  Tests: `test_sgov_refill_meaningful_drift.py` (11 tests covering both
  bug variants, boundary cases at the threshold edges, and the healthy
  cycle-3 scenario where refill should still fire).

  Lesson worth flagging: an earlier diagnosis on this bug was incorrect
  and a fix was implemented against the wrong root cause before being
  caught. Going forward: reproduce the bug in the sandbox before
  designing a fix.

### Design landed (no code yet)

Two specification documents drafted for the next session's implementation.
Neither is reflected in production code as of this changelog entry.

- **`EVENT_LOG_SPEC.md` (NEW, 1396 lines, ~88 KB).**
  Complete event log architecture. Single append-only `events.jsonl`
  at `paths.state_dir`, plain JSONL, no compression, no rotation, no
  pruning — kept indefinitely. 13 event types cataloged with full
  payload schemas. Writer API contract (single `append()` function in
  `event_log.py`), reader patterns, migration plan for retiring
  `cycle.jsonl` and `cb_transitions.jsonl`.

  Architectural commitment: IRAPM is the system of record. The slave
  box reconstructs from IBKR directly on self-promotion; the event log
  is operator-facing infrastructure (read by the reporter), not on the
  runtime critical path.

- **`REPORT_DECISIONS.md` (NEW, ~14 KB).**
  Reporter design captured as decisions rather than full spec. Three
  output file types: `current_status.txt` (overwritten weekly,
  current state + last 4 weeks' activity), `{YYYY}.txt` (per-year
  monthly rows, accumulating, with year-end summary appended at close,
  8-year rolling retention), `report_{timestamp}_{scenario}.txt`
  (simulator output, single file per run). Fixed-width format,
  annotation lines below data rows, column set documented.

  Reporter module is `report.py` at repo root, pure consumer of
  `events.jsonl`. Replaces the existing 12-file IPMS output package,
  which gets deleted in Phase 3 of the new-architecture rollout.

- **`HANDOFF_2026-05-14.md` (NEW, ~18 KB).**
  Detailed handoff for the next session's implementation work.
  Operator preferences, file-of-record list, phased implementation
  plan (writer → reporter → IPMS deletion → consumer migration →
  legacy writer deletion), and a list of "things to NOT do."

### Known issues (not addressed today)

- **Harness bail propagation.** Still unchanged from 2026-05-13.
  Counter increments correctly but `HarnessFailureError` raise at
  threshold doesn't terminate the run. Moot in current baseline since
  Issue #4 was the only recurring failure.

- **Deposit modeling (Path C).** Unchanged from 2026-05-13.
  Architectural decision still pending.

- **`test_ibkr_broker_phase2c.py` disrupts pytest collection.** Pre-existing
  issue. The file is a script with print statements rather than pytest
  test functions; pytest collection fails with `KeyError`. The 20 tests
  inside it pass via direct execution. Workaround:
  `pytest --ignore=test_ibkr_broker_phase2c.py test_*.py -v`. Fix is
  out of scope.

- **Gemini review items not addressed.** Same list as 2026-05-13:
  `action_layer.py` Phase 3 I_0 cash settlement race; `rebalancer.py`
  FI-sacrosanct suppression breadth.

### Pending work for next session

Implementation of the event log writer and the reporter per the two
new specs. Phase 1 (writer + emission sites + tests), Phase 2
(reporter + integration), Phase 3 (delete IPMS output), Phase 4
(migrate remaining consumers off cycle.jsonl and cb_transitions.jsonl),
Phase 5 (delete legacy writers). See `HANDOFF_2026-05-14.md` for full
detail.

---

## 2026-05-13 — Backtest viability fixes (6 fixes, 38 tests added)

Brings `baseline_20yr_phase3_2016.yaml` from "broken in multiple ways"
to "runs end-to-end with 1 isolated halt."

Pre-fix run state: 865 Sunday-only cycles, lookback frozen at one
value across all cycles, CB never fired, 178 silent ValidationError
halts in 2016-08 → 2019-12 window, annual review crashes on 9 of 20
expected runs.

Post-fix run state: 1044 Wednesday cycles, 1025 distinct lookback
values, 27 CB transitions tracking real market history (GFC, 2018Q4,
COVID, 2022 bear), 1 outstanding halt (Issue #4, action_layer
zero-quantity SELL — separate investigation).

### Fixed

- **Weekly cycle dispatch on Wednesdays per spec §9.6.**
  `irapm_driver.py`: `_WEEKLY_CYCLE_WEEKDAY` changed from 6 (Sun) to 2
  (Wed). T+1 settlement requires Wed cadence for the 15th ACH pull.
  Effect: 865 → 1044 cycles per run; 0 → 240 withdrawal days; 0 → 20
  annual review days.

- **CB transition log timezone normalization.**
  `annual_review.py`: `compute_cumulative_cb1_plus_days` now normalizes
  parsed naive timestamps to UTC before comparing against tz-aware
  year boundaries. Root cause: `clock.py` documents `Clock.now()` as
  naive; CB transitions record `now.isoformat()` (no tz suffix);
  re-parsing yields naive; comparison to `datetime(year, 1, 1,
  tzinfo=timezone.utc)` raised TypeError. Affected 9 of 20 annual
  reviews (the ones whose prior year had any CB activity).
  Tests: `test_annual_review_tz.py` (10 tests).

- **Calendar predicates for withdrawal and annual review.**
  `cycle.py`: `_is_scheduled_withdrawal_day` window expanded from
  `[10, 15]` to `[9, 15]` (a 7-day window guarantees one Wed lands
  in it for every month — the old window excluded day 9 and silently
  skipped any month whose 1st was Tuesday). `_is_annual_review_day`
  rewritten to fire on first Wed on-or-after MM-DD rather than only
  on exact MM-DD match (since cycles only run on Wed and MM-DD
  coincides with Wed in only ~1/7 of years).
  Effect: withdrawal days 205 → 240; annual reviews 2 → 20.
  Tests: `test_cycle_calendar.py` (10 tests).

- **Price-data as_of filter for backtest harness.**
  `price_data.py`: `load_weekly_adj_close` now accepts optional
  `as_of: date | None`. When set, filters bars to `bar_date <= as_of`
  before the count-trim. `lookback_signal.py`: passes `as_of=as_of`
  through. Root cause: the loader returned end-of-file bars regardless
  of cycle date; in the simulator harness where `as_of` walks historical
  dates against a fixed price file, this produced the same lookback
  signal every cycle (frozen at 0.1545...). In production where
  `as_of` is always "now" the filter is a no-op; backward-compatible.
  Effect: 1 distinct lookback value → 1025; CB transitions 0 → 27.
  Tests: `test_price_data_as_of.py` (7 tests).

- **`ScheduleStateInstance.trigger_year` validator range relaxed.**
  `state_model.py` and `state_schema.json`: range changed from
  `[2020, 2100]` to `[1900, 2200]`. Operational timeline expectations
  (Phase 1 starts 2027 per spec §4.1) enforced at higher layers
  (scenario loader, `new_initial_state`, ruleset validation); the
  schema validator is for structural correctness only. Required to
  support backtest scenarios with historical Phase 3 trigger years.
  Effect: removed 178 silent ValidationError halts in the
  2016-07-27 → 2019-12-25 Phase-3-active window.

- **CycleFailureTracker (refactor + bail-after-N policy).**
  `irapm_driver.py`: bail-counter and cycle.jsonl-offset state lifted
  out of the daily-hook closure into a `CycleFailureTracker` class.
  Per-cycle-type counters (weekly + daily-token, NOT shared) so a
  healthy daily-token success doesn't reset a stuck weekly retry
  counter. Synthetic halted-record write on every exception so
  cycle.jsonl is never silently incomplete.
  Tests: `test_harness_bail_logic.py` (11 tests).

### Known issues (not addressed today)

- **Issue #4 — FBCG zero-quantity SELL on 2005-05-04.** Single halt
  blocking the `no_failed_cycles` expectation. Action layer or
  rebalancer issue with small drift / small position; quantity
  rounds to zero before submission. Investigation start point:
  `action_layer._refresh_quantity` + `rebalancer.py` order
  construction.

- **Harness bail propagation.** `CycleFailureTracker` counter
  increments correctly at runtime (verified count=1, 2, 3 on
  consecutive identical failures, tracker_id stable) but the
  `HarnessFailureError` raise at threshold doesn't terminate the run.
  Unit tests prove the logic is correct in isolation. Moot in
  practice since no failures recur 3+ times in the current baseline.

- **Deposit modeling (Path C).** Current scenario is a $668,500
  lump-sum start; the operator's actual trajectory is ~$100K start
  + ~$130K/year deposits in years 2-8 (Phase 1 Roth conversions
  per spec §2.9). `ipms.SimulationParams` has no deposit mechanism.
  Architectural decision pending.

- **Gemini review items not addressed:**
  - `action_layer.py` Phase 3 I_0 cash settlement race condition
    (production-readiness for real broker; not blocking simulator)
  - `rebalancer.py` FI-sacrosanct suppression breadth (needs spec
    cross-check before deciding bug vs intentional design)
  - `withdrawal.py` Decimal fractional exponentiation (latent only)

---

## Pre-2026-05-13

This changelog was started 2026-05-13. Earlier code history is in git
(`git log`) and partially summarized in superseded HANDOFF docs.
Notable earlier landmarks:

- Broker Protocol Conformance Rewrite (2026-05-13 morning, see
  superseded `_REWRITE_2026-05-13_MANIFEST.md` if retained): full
  rewrite of `action_layer.py` to conform to the actual Broker
  Protocol; added `resolve_symbol()` to the protocol; new
  `smoke_test_action_layer.py`.

- Initial harness build (pre-2026-05-13): `irapm_driver.py`,
  `ScenarioConfig` YAML format, expectations checker, scenarios
  directory, IPMS daily-hook integration.

- IRAPM core modules (pre-2026-05-13): `cycle.py`, `decision_layer.py`,
  `action_layer.py`, `state_model.py`, `cb_machine.py`, `phase_manager.py`,
  `lookback_signal.py`, `annual_review.py`, `withdrawal.py`,
  `cash_buffer.py`, `sgov_refill.py`, `rebalancer.py`, and supporting
  files.

For dates before this changelog, run `git log --oneline --since=...`.
