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
