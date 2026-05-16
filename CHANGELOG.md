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

## 2026-05-16 — Phase 2 Session 2 (partial): reporter call-site wiring + test foundation

**Workstream 1 complete; Workstream 2 partial.** The reporter (`report.py`,
landed Session 1) is now invoked from production call sites in
`cycle.py` and `irapm_driver.py`. End-to-end smoke-validated against
`smoke_minimal.yaml` and the 20-year baseline `baseline_20yr_phase3_2016.yaml`.
Reporter call sites do not regress existing behavior — terminal AUM of
$3,518,770.59 matches Session 1 exactly. `test_report.py` foundation
added with helper infrastructure and the first golden-file test.

Workstream 2 paused mid-way: a cascade-rendering audit uncovered that
the operator's tiered concern levels (SGOV-engaged / FI-engaged /
Growth-engaged) are only partially represented in the alert system,
and the reporter does not surface cascade depth as an annotation. The
three-session plan to address this (A: spec, B: implementation, C:
reporter + tests) is queued. See `HANDOFF_PHASE2_SESSION2.md` for full
session detail.

### What (Workstream 1)

- **`persistence.py` (MODIFIED)**. `Paths` gained a `reports_dir`
  property returning `state_dir / "reports"`. `ensure_dirs()` now
  creates `reports_dir/` alongside `state_dir/` and `logs_dir/`.
  This is the directory the per-cycle reporter outputs land in:
  `current_status.txt` (overwritten every weekly cycle) and per-year
  `{YYYY}.txt` files (rebuilt monthly, closed annually). DEPLOYMENT
  NOTE: master/slave replication needs `reports_dir/` in the
  replication set.

- **`cycle.py` (MODIFIED)**. `run_weekly_cycle` invokes the reporter
  at end-of-cycle per REPORT_SPEC §7.3. Four call sites, all wrapped
  in `try/except` per §7.6 — reporter failures log at WARNING and the
  cycle continues:
    1. `write_current_status` every weekly cycle.
    2. `append_monthly_row_to_year_file` on the first weekly cycle
       of each month, for the prior month.
    3. `close_year_file` on the first weekly cycle of January, for
       the prior year.
    4. `prune_old_year_files(retain_years=8)` after the January
       close, removing year files older than the retention window.
  Added `import report` at module top. Prior-month calculation uses
  stdlib `timedelta` (no new `python-dateutil` dependency).

- **`irapm_driver.py` (MODIFIED)**. `run_scenario` invokes
  `write_simulation_report` once at end-of-run per REPORT_SPEC §7.4,
  writing to `sim_result.output_dir`. Wrapped in `try/except`. Filename
  pattern: `report_{YYYYMMDD_HHMMSS}_{scenario_name}.txt`. Skipped
  (info-logged, not warned) when IPMS produces no output directory —
  intentional in some test paths, not a failure.

### What (Workstream 2 partial)

- **`test_report.py` (NEW, ~870 lines)**. Pytest suite skeleton per
  REPORT_SPEC §9. Five sections planned (helpers, golden-file tests,
  edge cases, prune tests, slow integration); only sections 1–2 are
  populated. Includes:
    - `make_event` plus eight typed builders matching every event
      type the reporter consumes. Tests stay terse; payload-shape
      changes happen here, not scattered across test functions.
    - `build_events_log`, `build_paths` for fixture setup.
    - `load_golden`, `write_golden` for the standard golden-file
      workflow.
    - Four helper smoke tests (envelope shape, id stability, JSONL
      roundtrip via `event_log.iter_events`, `Paths.reports_dir`
      creation by `ensure_dirs()`).
    - First golden-file test: `test_write_current_status_phase2_cb1`
      against scenario in `_build_phase2_cb1_events()`.
    - `--regen-goldens` CLI for the standard
      generate-on-format-change workflow.

- **`fixtures/current_status_phase2_cb1.txt` (NEW, ~2.6 KB)**. Golden
  output for a 5-week scenario: Phase 1 cycle with withdrawal and
  transition to Phase 2 in week 1, three quiet Phase 2 weeks, then
  CB1 firing in week 4 with the entered-day-7-of-90 rendering in
  the header. Exercises Session 1 Fix #7 (cb1_active_timer_started_at)
  and the tz-aware/naive normalization fix.

### Tests

- `test_report.py::test_make_event_envelope_shape` — envelope fields
  and types.
- `test_report.py::test_make_event_id_is_stable` — deterministic
  event_id from inputs (required for golden-file diffability).
- `test_report.py::test_build_events_log_roundtrip` — helpers produce
  JSONL readable by `event_log.iter_events`.
- `test_report.py::test_build_paths_creates_reports_dir` — guards the
  `Paths.reports_dir` Workstream 1 addition.
- `test_report.py::test_write_current_status_phase2_cb1` — first
  golden-file test; byte-compares output (Generated line stripped)
  against committed fixture.

All five passing.

### Discoveries / lessons captured this session

1. **Timestamp convention for synthetic events.** All events for a
   single weekly cycle must share that cycle's timestamp exactly.
   Within-cycle ordering is JSONL line order, not timestamp ordering.
   Verified against the real 2005-2025 events.jsonl. The reporter's
   recent-activity windowing math (`(prev_ts + 1us, this_ts + 1us]`)
   depends on this convention; microsecond-bumping events to make
   them "distinct" puts them into the wrong cycle's window and
   produces shifted-by-one row content with mis-attached annotations.
   Captured as a docstring in `_build_phase2_cb1_events()` for
   future scenario builders.

2. **Reporter–driven runtime overhead in simulation.** The 20-year
   baseline runs ~2-3x slower with reporter wiring (from ~1:30-2:00
   to ~5:00). Each weekly cycle's reporter invocations re-parse the
   growing events.jsonl from scratch via
   `_load_events_indexed`. Accepted for this session; documented as
   a candidate optimization (process-local cache keyed by
   (path, size, mtime), internal to `report.py`, no API change) to
   be revisited after Workstream 2 tests provide a regression
   safety net. Production weekly cycles run once a week and don't
   experience this; the cost is simulation-only.

3. **Brand-new-install edge handling.** Original implementation
   added a split-boolean gating to skip `append_monthly_row_to_year_file`
   and `close_year_file` on the very first cycle of a brand-new
   install. Reverted after operator pushback: production deployment
   convention will pre-stage a seed `events.jsonl` matching the
   manual bootstrap (§2.9 — SGOV pre-fund, $28K core), so cycle 1
   never sees a true empty log. Simulator uses fresh state_dir each
   run; reporter is expected to handle empty-log inputs per its own
   contract. Both decisions captured as comments in `cycle.py`.

### What's deferred to next sessions

Three-session cascade plan queued before Workstream 2 resumes:

- **Session A (spec authorship)**: Define `cascade_engaged_sgov`
  (Notice) and `cascade_extended_fi` (Warning) alerts in
  `IRAPM_SPECIFICATION.md` §15 alert catalog and §7.3.2 step
  semantics. Decide on reporter rendering grammar in `REPORT_SPEC.md`
  §3.1 (current_status header) and §5.2 (CB2 annotation suffix).

- **Session B (implementation)**: Emit the two new tier alerts from
  `withdrawal.py::_source_cascade` and propagate via
  `decision_layer.py`. The cascade depth determination already
  exists; the alerts at SGOV-drained and FI-drained boundaries do
  not.

- **Session C (reporter + tests)**: Implement the new rendering in
  `report.py`. Resume Workstream 2 with cascade tiers covered
  natively in the mini-baseline scenario from the start.

Workstream 2 Steps 3-6 (mini-baseline + golden, edge-case tests,
`prune_old_year_files` tests, slow real-baseline integration test)
are deferred until after Session C — doing them earlier would lock
in golden output that omits cascade tiers.

---

## 2026-05-16 — Phase 2 Session 1: report.py implementation + smoke-test pass

**Phase 2 Session 1 complete.** `report.py` implemented per `REPORT_SPEC.md`,
smoke-tested end-to-end against the 20-year baseline event log. All five
public reporter functions produce correct output. Twelve bugs caught and
fixed during smoke-testing (eleven reporter fixes + one timezone
normalization fix). Reporter is ready for cycle.py integration in Session 2.

This entry summarizes the code change. Full session detail in
`HANDOFF_PHASE2_SESSION1.md`.

### What

- **`report.py` (NEW, ~71 KB, ~1800 lines)**. Five public functions:
  `write_current_status`, `append_monthly_row_to_year_file`,
  `close_year_file`, `prune_old_year_files`, `write_simulation_report`.
  Pure consumer of `events.jsonl`. Imports only `event_log` (read API)
  and `persistence` (Paths); `ruleset_model.Ruleset` read-only for the
  sim report's RULESET section per REPORT_SPEC §3.3.4. Internal
  `EventIndex` loads events once per public-function call and serves
  all helpers in-memory (replaces an earlier per-helper file-walk that
  would have done 1,400+ full-file passes per simulation report).

- **`event_log.py` (MODIFIED)**. Phase 1 writer preserved. Added three
  reader functions: `iter_events`, `iter_events_of_type`,
  `iter_events_in_window`. The reporter consumes events exclusively
  through these.

- **`smoke_report.py` (NEW)**. Driver that invokes all five reporter
  functions in sequence against an event log. Loads ruleset.yaml as
  raw YAML, patches `ach_destination = "SMOKE-TEST-PLACEHOLDER"`
  before Pydantic validation, samples December 2008 for the full-year
  append exercise. Writes outputs under
  `runs/{run_id}/_smoke_report_output/`.

- **`REPORT_SPEC.md` (NEW)**. Full §1-§10 implementation contract.
  `report.py` satisfies this spec. Three field-name drifts in §3.3.4
  noted in the handoff for later correction (code is right, spec is
  wrong; pure documentation drift, no behavior impact).

### Eleven reporter fixes landed (categorized)

Full narrative for each in `HANDOFF_PHASE2_SESSION1.md` under "Eleven
reporter fixes + one timezone fix."

- **Annual-row semantics (Fix #1 + #11)**: rows dated YYYY-12-31
  (or actual last-event date for the partial final year), built from
  the LAST snapshot of year Y with year-Y events as annotations.
  Eliminates the prior off-by-one between row label and row content.
- **Granularity-aware annotation filtering (Fix #2)**:
  `_extract_annotations_from_index` gained a `granularity` parameter.
  At annual scope, W (withdrawal) annotations suppressed; all summary
  markers (AR, CB1, CB2, REC, P2, P3, DPD, DPR, HALT, PAUSE) and RFL
  preserved.
- **SGOV split out of allocation (Fix #3)**: per IRAPM_SPECIFICATION I1,
  SGOV buffer is not part of core asset allocation. Asset-movement
  lines render SGOV with price return only; core symbols' allocation
  % uses `investable_aum = total - sgov - cash_buff`. Core five sum
  to ~100%.
- **Ruleset loading patched for smoke test (Fix #4)**: `smoke_report.py`
  patches the placeholder `ach_destination` to a Pydantic-acceptable
  value before validation, so RULESET section populates with actual
  ruleset values instead of falling back to the "(ruleset object not
  available)" message.
- **Recent activity filtered to weekly cycles (Fix #5)**: `cycle_type ==
  "weekly"` filter on `cycle_completed` events before taking the last
  4. Eliminates the prior output that showed 4 days of daily-token
  cycles as GAP rows.
- **GAP renders bare (Fix #6)**: `_render_annotation_line` short-circuits
  `code == "GAP"` to emit just `"GAP"` without the period-start date
  suffix that other annotations carry. GAP is a structural marker per
  REPORT_SPEC §5.2, not a dated event.
- **CB state field name correction (Fix #7)**: read
  `cb_machine.cb1_active_timer_started_at` from the state_snapshot
  payload (audited against an actual payload); the prior code read
  the non-existent `cb1_entered_at`. CB state line now shows the
  "entered YYYY-MM-DD, day N of 90" detail. (TODO: "of 90"
  denominator still hardcoded; should come from
  `ruleset.cb1_to_cb2_timer_days`.)
- **Historical-vs-live anchor heuristic (Fix #8)**: new
  `_anchor_now(index, wall_clock_now)` helper returns the latest
  event timestamp if the log is >7 days behind wall-clock; otherwise
  wall-clock. Threaded through CB days_elapsed, next-withdrawal date,
  and recent-alerts window. The header `Generated:` line still uses
  wall-clock — operators want to know when the file was produced.
- **DPD / DPR annotation detection (Fix #9)**: detected from
  `alert_emitted` events with `alert_id` of
  `phase2_opportunistic_deploy` or `phase2_opportunistic_recover`.
  No dedicated event type exists for these transitions; the alert is
  the canonical record. Now visible: `DPD:2016-01-13, DPR:2016-06-15`
  on the 2016 annual row of the smoke test.
- **LEGEND block in year files (Fix #10)**: `_build_year_file_content`
  appends the full LEGEND (shared body via `_render_legend_section`)
  after the annual summary. Year files are now self-contained
  reference documents. Date-column description also updated to
  reflect Fix #1's annual-row semantics.

### One timezone-normalization fix

- **`_parse_iso_datetime` normalizes naive strings to UTC-aware**:
  the IRAPM event log writes top-level event timestamps with explicit
  UTC suffix (`+00:00`), but state_snapshot payloads contain nested
  datetime fields written as naive strings (e.g.
  `cb1_active_timer_started_at = "2025-04-09T00:00:00"`). Subtraction
  between the two forms raised `TypeError: can't subtract offset-naive
  and offset-aware datetimes`. The parser now attaches
  `tzinfo=timezone.utc` when input is naive, since the whole system
  semantically treats stored times as UTC. Fixing at the parser
  eliminates an entire class of bug across every datetime-arithmetic
  site in the reporter.

### Why

The Phase 1 event log substrate landed 2026-05-15 (per
`HANDOFF_PHASE1_COMPLETE.md`). Phase 2 builds the operator-facing
reporter that consumes it. Session 1's deliverable is a working
reporter validated against a real workload; Session 2 wires it into
`cycle.py` and `ipms.engine` so production runs and simulation runs
both produce these reports.

### Smoke-test outputs (in `runs/2005-2025_fd1ad2/_smoke_report_output/`)

All four files validated post-fix, all timestamps fresh (~23:58 local):

- `current_status.txt` (2,645 bytes) — header, CB state with days
  elapsed, recent activity (4 weekly cycles 2025-03-26 / 04-02 /
  04-09 / 04-16), recent alerts.
- `2008.txt` (6,387 bytes) — full Phase 1 year through December,
  monthly rows + annual summary + LEGEND.
- `2010.txt` (6,270 bytes) — full year, SGOV split out, core 5
  symbols sum to ~100%, LEGEND.
- `report_smoke_baseline_2005-2025.txt` (60,065 bytes) — full
  20-year sim report with TOTALS / RULESET / ANNUAL / MONTHLY /
  LEGEND. 21 annual rows + 240 monthly rows.

Sanity numbers from the sim report match the underlying mechanics:

- Terminal AUM $3,518,770.59 vs starting $671,576.28 (+423.96% over
  20yr).
- 185 monthly withdrawals (240 calendar months - 55 suppressed by
  4.5yr Phase 2 stop-income).
- 105 dollar-cap-bound withdrawals (all 2016-08 onward, Phase 3).
- 3 CPI freezes (2009, 2021, 2023), each with `(F)` suffix on the
  AR annotation per `binding_constraint == "cpi_freeze"`.
- Dollar cap progression $4,917 (2016) → $6,701.40 (2025) =
  `$4000 × 1.035^15`, confirming Phase 3 indexing math.
- 8 CB1 triggers, 5 CB1→CB2 promotions, 7 recoveries, 9.5% of
  run-days in CB1+.
- Phase 2 transition 2012-01-04, Phase 3 latch 2016-08-03.

Note: these numbers are conditional on the same-day BACKTEST-HACK
ruleset edits (see the entry below). Pre-hack the sim was silently
lying about all three of: CPI raises, dollar-cap indexing, and
Phase 1→2 transition. The smoke test surfaced the hidden
production-anchor problem and motivated the hack.

### Tests

None permanent yet. The smoke-test driver (`smoke_report.py`) is
the only verification. REPORT_SPEC §9 specifies the test plan
(golden-file fixtures + assertions), deferred to Session 2 along
with unit tests for the new event_log reader functions.

### Files modified

- `report.py` — NEW, ~71 KB.
- `event_log.py` — MODIFIED (reader functions added).
- `smoke_report.py` — NEW.
- `REPORT_SPEC.md` — NEW.
- `HANDOFF_PHASE2_SESSION1.md` — NEW. Session 2 entry point.

### Pending work for next session

Four workstreams in priority order:

1. Wire `report.py` invocations into `cycle.py` (status every weekly
   cycle, monthly append on first weekly of each month, close + prune
   on first weekly of January) and into `ipms.engine` end-of-run
   (simulation report). Wrap each in try/except so reporter failure
   cannot halt a cycle.
2. Permanent test suite per REPORT_SPEC §9: golden-file fixtures for
   Phase 1 clean, Phase 3 capped, CB-active year, partial year. Plus
   unit tests for the new event_log reader functions.
3. Five smaller deferred items: DRIP-vs-Sweep decision + cash_in
   implementation, three field-name corrections in REPORT_SPEC §3.3.4,
   ruleset-threading for the hardcoded "of 90" CB denominator,
   permanent fix for the calendar-anchored ruleset fields
   (BACKTEST-HACK root cause), and SAR annotation wiring.
4. The misspecified starting balance: $668.5K used vs actual
   $100K + 7yr Roth conversion stream. Contribution-stream feature
   gap in IPMS harness, not a parameter fix. Tracked separately.

Full detail in `HANDOFF_PHASE2_SESSION1.md`.

---

## 2026-05-16 — ruleset_model.py validator scope: lower year bound to 1990

**Permanent code change** to `ruleset_model.py` Ruleset model. Two
field validators relaxed from `ge=2020` to `ge=1990`. This stays in
production; it is NOT part of the same-day backtest-hack entry below.

### What

- `phase1_trigger_year`: bound changed from `ge=2020, le=2100` to
  `ge=1990, le=2100`.
- `phase3_dollar_ceiling_base_year`: same change.

### Why

The original `ge=2020` bound encoded the implicit assumption that the
system would never be configured to model anything earlier than the
project's expected start year. The 2026-05-16 backtest-hack work
revealed this assumption was too tight: backtests, historical
what-if analyses, and any retrospective sim need to set these
years to pre-2020 values without tripping the validator.

The defense the bound provides — catching "operator typo'd a year"
like `199` or `21027` or `-2027` — is intact at `ge=1990`. The new
bound only admits values that are plausible calendar years; it
continues to reject obvious typos.

### Why now

The backtest-hack work above needed `phase1_trigger_year: 2005` and
`phase3_dollar_ceiling_base_year: 2010`. Both failed validation at
load time under the old bound. Lowering the bound was the right
correction: either the model permits historical-anchor values (in
which case all backtests work without touching the model) or it
doesn't (in which case the model is hostile to its own sim infra,
which is wrong). The first option is consistent with how clock.py
is designed (real wall-clock seam for production; sim-clock for
backtest, both honored equally).

### Tests

No new tests. The existing `Ruleset.from_yaml` path validates the
bound at load time; the 2026-05-16 backtest hack's three-field
shift is itself an end-to-end exercise of the relaxed bound
(loading `ruleset.yaml` with `phase1_trigger_year: 2005` must
succeed, which it does after this change).

---

## 2026-05-16 — ruleset.yaml backtest hack: shift 3 calendar-anchored fields to sim window

**NOT A PRODUCTION CHANGE.** Three ruleset.yaml values temporarily edited
to let the 2005-2025 baseline sim exercise mechanisms that are otherwise
inert when the sim window ends before the production anchor years.
Must be reverted before any production deployment.

### Why

The 2026-05-15 Phase 2 reporter smoke test surfaced that the 2005-2025
baseline sim had been silently lying about three calendar-anchored
mechanisms for the entire history of the project:

1. **Phase 1 CPI raises never applied** — 240 months of withdrawals
   all paid the unindexed $3000 base because `phase1_trigger_year=2027`
   and `schedule_state.n_raises_applied()` returns 0 when
   `current_year <= trigger_year`. Witnessed: every Phase 1 withdrawal
   from 2005 through 2016-07 paid exactly `"3000"`.
2. **Phase 3 dollar cap never indexed** — 105 capped months from
   2016-08 through 2025-04 paid exactly `"4000"` because
   `phase3_dollar_ceiling_base_year=2027` and `apply_phase3_ceilings()`
   clamps `years_since_base` to 0 when negative. The cap should have
   indexed to ~$6700/mo by 2025 at 3.5% inflation.
3. **Phase 1 → Phase 2 transition never fired** —
   `phase1_to_phase2_transition_date="2035-01-01"` is past the sim
   end date 2025-04-16, so `phase_manager.evaluate_phase_machine()`'s
   `today >= transition_date` check never trips. Phase 2 logic
   (opportunistic swing, semi-annual reallocation, GBIL liquidation
   in Phase 2→Phase 3) was completely untested by the sim.

The code is correct: each mechanism does exactly what the spec
requires given the configured anchor dates. The mismatch is that the
ruleset's calendar anchors are absolute production dates and the sim
window happens to land entirely before them. The clock.py seam
correctly swaps wall-clock for sim-clock, but ruleset constants are
intentionally absolute.

### What changed

`ruleset.yaml` only:

- `phase1_trigger_year`: `2027` → `2005`
  CPI raises now apply each year of the sim. By 2016 (Phase 3 latch):
  `$3000 * 1.035^11 ≈ $4380` base. By 2025: `$3000 * 1.035^20 ≈ $5970`.
- `phase1_to_phase2_transition_date`: `"2035-01-01"` → `"2012-01-01"`
  Phase 2 now fires in 2012, giving ~6.7 yrs Phase 1, ~4.5 yrs Phase 2,
  ~9 yrs Phase 3 (Phase 3 still latches via 2016-07-15 token removal
  per the scenario; the path is now Phase 2 → Phase 3 instead of
  Phase 1 → Phase 3, exercising additional code paths like GBIL
  liquidation).
- `phase3_dollar_ceiling_base_year`: `2027` → `2010`
  Dollar cap now indexes 15 years to ~$6710 by 2025. Still binds against
  the scheduled amount (which has grown via CPI raises to comparable
  values), demonstrating both indexing math AND clamp-vs-scheduled.

`phase3_dollar_ceiling_base_dollars` remains `4000` per operator
direction (sustainability check against portfolio balance at $4000
baseline is part of what this sim is meant to evaluate).

Each edited line carries a `[BACKTEST-HACK 2026-05-16]` marker and an
inline "REVERT TO X BEFORE PRODUCTION" instruction. Grep for
`BACKTEST-HACK` to find every site needing reversion.

### Revert procedure

Before any production deployment, restore these three lines to their
pre-hack values:

```yaml
phase1_trigger_year: 2027               # [SPEC] §3.13, §4.1
phase1_to_phase2_transition_date: "2035-01-01"  # [SPEC] §4.1, §4.2
phase3_dollar_ceiling_base_year: 2027       # [SPEC] §4.1.1.2
```

Verify with `grep -n BACKTEST-HACK ruleset.yaml` returning zero matches.

### Follow-up work captured for future

The systematic issue — ruleset anchored to absolute production dates
is hostile to backtesting — needs a real fix, not just this manual
edit. Two options surveyed (Option 2: sim wrapper auto-rewrites; Option
3: sentinel `"@sim_start"` syntax in ruleset). Deferred to a later
session. See 2026-05-16 session transcript for the analysis.

The 2005-2025 sim with $668.5K starting balance is also known to be
misspecified — operator's actual scenario is $100K initial + 7 years
of $130K Roth conversions. The contribution-stream feature is a gap
in the IPMS harness, not a parameter issue. Also deferred.

### Tests

None added. This is a configuration edit, not a code change. The
revert procedure above is the test: revert + re-run sim should produce
the pre-hack event log (all `phase1` withdrawals at $3000, all
`dollar_cap` bindings at $4000, no Phase 2 ever).

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
