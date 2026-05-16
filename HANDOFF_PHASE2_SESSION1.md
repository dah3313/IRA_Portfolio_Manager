# IRAPM Phase 2 — Session 1: Reporter Land + Smoke Test

**Status:** Phase 2 Session 1 is **complete**. `report.py` exists, implements all five public functions per `REPORT_SPEC.md`, and produces correct output end-to-end against the 20-year baseline event log. All five smoke-test outputs validated post-fix.
**Date:** 2026-05-16
**Baseline used:** `runs/2005-2025_fd1ad2/_harness_state/events.jsonl` (10.85 MB, 243 events of interest), produced from the temporarily-hacked ruleset described in the same-day CHANGELOG entries.

This handoff is the entry point for whoever (Claude or human) picks up Phase 2 Session 2. Read it first.
Then read `REPORT_SPEC.md` (the implementation contract that `report.py` satisfies) and `HANDOFF_PHASE1_COMPLETE.md` (the Phase 1 → 2 entry point this session sits inside). Those three documents are the full context — this session's chat history is not required.

---

## What's done

`report.py` is implemented per `REPORT_SPEC.md`. Five public functions, all working:

| Function | Purpose | Smoke-test confirms |
|---|---|---|
| `write_current_status(state_dir, output_path)` | Operator's "what's IRAPM doing right now" — overwritten weekly | ✓ |
| `append_monthly_row_to_year_file(state_dir, output_dir, month)` | Adds month's row to `{YYYY}.txt`, accumulating | ✓ |
| `close_year_file(state_dir, output_dir, year)` | Final close-out of a year file with annual summary | ✓ |
| `prune_old_year_files(output_dir, retain_years=8)` | Rolling retention of year files | ✓ |
| `write_simulation_report(state_dir, output_path, scenario_name, ruleset)` | Single-file simulator output with TOTALS / RULESET / ANNUAL / MONTHLY / LEGEND sections | ✓ |

Internal architecture as built:

- **`EventIndex`** — a load-once-per-public-call in-memory index that holds `all_events` (the full ordered event list) and `by_type` (a dict of event_type → list). Internal helpers query this index rather than re-walking events.jsonl. Materially faster than the pre-refactor version, which re-iterated the JSONL file once per helper call — at 240+ monthly rows × ~6 helper calls per row, that was 1,400+ full-file passes per simulation report.
- **`DataRow`** — pre-rendered row struct so column widths are computed once and the same widths are applied across annual + monthly tables in the sim report (their numeric ranges differ; uniform widths keep the two tables visually aligned).
- **`_atomic_write_text`** — temp-file-and-rename for all four output paths. No half-written files on partial failure.
- **`_anchor_now`** — heuristic that returns the latest event timestamp instead of wall-clock if the log is >7 days behind wall-clock, so historical/sim event logs produce useful current_status output (CB elapsed days, next-withdrawal date, recent-alerts window) instead of garbage anchored to "today."

Smoke driver (`smoke_report.py`) invokes all five functions in sequence against the 20-year baseline. All five steps print `OK: wrote ...`. Output files:

| Output | Size | Validates |
|---|---|---|
| `current_status.txt` | 2,645 bytes | header timestamp, CB state with days elapsed, recent activity (last 4 weekly cycles), recent alerts |
| `2008.txt` | 6,387 bytes | full-year monthly rows, annual summary, LEGEND block |
| `2010.txt` | 6,270 bytes | same shape; quieter year (no CB activity) |
| `report_smoke_baseline_2005-2025.txt` | 60,065 bytes | TOTALS / RULESET / ANNUAL / MONTHLY / LEGEND, 21 annual rows + 240 monthly rows |

Sanity numbers visible in the sim report match the underlying mechanics:
- Terminal AUM $3,518,770.59 vs starting $671,576.28 (+423.96% over 20yr)
- 185 monthly withdrawals (240 - 55 suppressed by 4.5yr Phase 2 stop-income)
- 105 dollar-cap-bound withdrawals (all 2016-08 onward, Phase 3)
- 3 CPI freezes (2009, 2021, 2023), each with `(F)` suffix on the AR annotation
- Dollar cap progression $4,917 (2016) → $6,701.40 (2025) = `$4000 × 1.035^15`
- 8 CB1 triggers, 5 CB1→CB2 promotions, 7 recoveries, 9.5% of run-days in CB1+
- Phase 2 transition 2012-01-04, Phase 3 latch 2016-08-03

## Files modified across Phase 2 Session 1 (final state on disk at C:\portfolio\)

| File | Status | Notes |
|---|---|---|
| `REPORT_SPEC.md` | NEW | Full §1-§10 spec; three known field-name drifts in §3.3.4 (see Deferred items) |
| `event_log.py` | MODIFIED | Phase 1 writer preserved; reader additions `iter_events`, `iter_events_of_type`, `iter_events_in_window` |
| `report.py` | NEW (~71KB, ~1800 lines) | Five public functions; EventIndex; all 11 reporter fixes + 1 tz fix from this session |
| `smoke_report.py` | NEW | Driver runs all 5 reporter functions; patches `ach_destination` for Pydantic; samples December 2008 for the full-year append exercise |
| `ruleset.yaml` | MODIFIED (temporary) | `[BACKTEST-HACK 2026-05-16]` markers on 3 lines — must revert before production |
| `ruleset_model.py` | MODIFIED (permanent) | Two validator bounds relaxed from `ge=2020` to `ge=1990` |
| `CHANGELOG.md` | MODIFIED | Three 2026-05-16 entries: this session, the backtest hack, the validator relax |

## Architectural properties preserved

- **`report.py` is a pure consumer.** It imports from `event_log` (read API) and `persistence` (Paths). It does not import from `cycle.py`, `action_layer.py`, `decision_layer.py`, or any other runtime module. The one in-memory exception is `ruleset_model.Ruleset`, which `write_simulation_report` reads to populate the RULESET section per `REPORT_SPEC §3.3.4`. Read-only.
- **Stateless.** Each public-function call loads the index, builds output, writes via temp-rename. No caches or persistent state.
- **Failure isolation.** Callers wrap invocations in try/except so a reporter failure cannot break a cycle. `smoke_report.py` demonstrates this — Session 1's tz bug caused `write_current_status` to fail, the driver caught it and continued through the other 4 steps without disruption.
- **Existing logs (cycle.jsonl, cb_transitions.jsonl) untouched.** Phase 5 deletes them after Phase 4 migrates the remaining legacy-log consumers.

## Eleven reporter fixes + one timezone fix landed this session

Each was caught by smoke-testing against the 20-year baseline. None were architectural; all were "the spec says X but the code did Y" or "this event payload field is named differently than I assumed." The fixes are listed by category, not by chronological discovery order.

### Fix #1 + #11 — Annual rows dated YYYY-12-31 with year-Y semantics

**Pre-fix**: annual rows were dated YYYY-01-01 and pulled the first portfolio_snapshot of year Y, but used year-(Y-1)'s events as annotations. Off-by-one between the row's apparent year and the year its data described.

**Fix**: each annual row dated YYYY-12-31 and built from the LAST portfolio_snapshot of year Y, with year-Y's events as annotations. Row label, snapshot, and annotations all refer to the same year. For the partial final year (run ended mid-year), the row date is the actual last event date (e.g. 2025-04-16 in this smoke run), and aggregates run from year start through that date. First-year edge case (run started mid-year) handled correctly: missing pre-start period contributes nothing to aggregates.

### Fix #2 — Annual granularity suppresses W annotations

**Pre-fix**: every annual row showed 12 `W:YYYY-MM-DD` annotations from the year's withdrawals, drowning the actual summary markers (AR, CB1, CB2, REC, P2, P3, etc.) in noise.

**Fix**: `_extract_annotations_from_index` gained a `granularity="monthly"|"annual"` parameter. At annual scope, W is suppressed (the count is already in cash_out and the annual summary block). RFL stays at annual scope because a buffer refill outside its usual cadence is worth seeing. All summary markers preserved at every granularity.

### Fix #3 — SGOV split out of allocation %

**Pre-fix**: SGOV's market value was rolled into the allocation percentages, dragging the FI bucket's apparent weight to ~24% even though it should be ~50% of investable AUM. The reference `AnnualSummary.txt` had this same bug — that example was illustrative, not authoritative.

**Fix**: per IRAPM_SPECIFICATION I1, SGOV buffer is NOT part of core asset allocation. `_build_asset_movement_lines` now renders SGOV as a price-return line only (`SGOV: +1.89% (buffer, $80,963 → $82,497)`) and computes core-symbol allocations against `investable_aum = total_aum - sgov_buffer - cash_buff`. The five core symbols (PYLD, JPIE, FBCG, AVUV, GBIL) now sum to ~100% as expected.

### Fix #4 — RULESET section populated for sim report

**Pre-fix**: `_render_ruleset_section(ruleset=None)` returned `"(ruleset object not available for this run — see ruleset_used.yaml in the run directory)"` because `smoke_report.py` was passing `None`. The Pydantic validator rejected the loaded ruleset because the `ach_destination` field was set to the placeholder `"HARNESS-DEV-PLACEHOLDER"`.

**Fix**: `smoke_report.py` loads `ruleset.yaml` as raw YAML, patches `ach_destination = "SMOKE-TEST-PLACEHOLDER"` (a more-honest placeholder that flags this is sim infra, not test data), then calls `Ruleset.from_yaml`. The RULESET section now populates with the four tunable-field groups documented in REPORT_SPEC §3.3.4.

### Fix #5 — Recent activity filters to cycle_type == "weekly"

**Pre-fix**: `_render_recent_activity_block` walked the last 4 `cycle_completed` events. Most cycles in events.jsonl are `cycle_type: daily-token`, which carry no portfolio snapshot. The output showed 4 consecutive days of GAP rows instead of 4 weeks of weekly cycle data.

**Fix**: filter cycle_completed events to `payload.cycle_type == "weekly"` before taking the last 4. Now shows actual weekly cycles at 7-day spacing.

### Fix #6 — GAP annotation renders bare (no date suffix)

**Pre-fix**: `_render_annotation_line` formatted every annotation as `{CODE}:{date}`, including GAP. But GAP is a structural marker (REPORT_SPEC §5.2) representing "no portfolio data for this period" — there is no associated event date, only the period itself. The output showed `GAP:2025-04-12` (the period_start date), which is meaningless and confusing.

**Fix**: short-circuit in `_render_annotation_line` for `code == "GAP"`, emit bare `"GAP"`. All other codes still render as `CODE:date`.

### Fix #7 — CB state field name `cb1_active_timer_started_at`

**Pre-fix**: `_render_status_header_block` read `cb_machine.cb1_entered_at` from the state_snapshot payload. That field doesn't exist. The canonical name per `state_model.CBMachine` is `cb1_active_timer_started_at`. Result: CB state line never showed the "entered YYYY-MM-DD, day N of 90" detail; just `1` or `2`.

**Fix**: read `cb1_active_timer_started_at` instead. Audited against an actual state_snapshot payload, not against my memory of the model.

**Outstanding TODO**: the "of 90" denominator is hardcoded. Should come from `ruleset.cb1_to_cb2_timer_days`. Deferred until ruleset is threaded through `write_current_status` (it currently takes only state_dir and output_path; either widen the signature or read the ruleset from `state_dir/ruleset_used.yaml`).

### Fix #8 — Historical-vs-live anchor for current_status

**Pre-fix**: every datetime calculation in `write_current_status` used `datetime.now(timezone.utc)` directly. For a live production system this is correct. For a historical event log (the 20-year baseline ends 2025-04-16 but wall-clock is 2026-05-16), this produced:
- CB days_elapsed = 400+ (anchored to wall-clock, not to the latest event)
- "Next scheduled withdrawal" = 2026-06-15 (today's next 15th, meaningless for a historical run)
- "Recent alerts in last 4 weeks" = empty (no alerts in May 2026)

**Fix**: new `_anchor_now(index, wall_clock_now)` helper. Heuristic: if the latest event in the log is >7 days behind wall-clock, return the latest event timestamp; otherwise return wall-clock. Threaded through `_render_status_header_block` (CB days_elapsed), `_derive_next_scheduled_withdrawal`, and `_render_recent_alerts_block`. The header `Generated:` line still uses wall-clock — operators want to know when the file was produced, distinct from the "now" the body is anchored to.

7-day threshold rationale: wide enough to absorb cycle-cadence jitter (a missed Wednesday) plus a multi-day outage; short enough that any simulation or backtest is safely on the historical side. No overlap region: "slightly historical" is not a state worth distinguishing from "live but stale" — both get the latest-event anchor.

### Fix #9 — DPD / DPR annotations from alert_emitted

**Pre-fix**: REPORT_SPEC documents DPD (dry powder deployed) and DPR (dry powder recovered) annotation codes, but the annotation extractor didn't produce them. The underlying mechanism (Phase 2 opportunistic deploy/recover) emits no dedicated event type — only `alert_emitted` events with `alert_id` of `phase2_opportunistic_deploy` or `phase2_opportunistic_recover`. The smoke test's 2016 annual row was missing both.

**Fix**: detect DPD/DPR from `alert_emitted` events keyed by alert_id. Now visible: `DPD:2016-01-13, DPR:2016-06-15` on the 2016 annual row.

If a future Phase 1 writer change adds a dedicated event_type for these transitions, this detection should switch to that. Comment in the extractor flags this.

### Fix #10 — LEGEND block included in year files

**Pre-fix**: year files contained only the table + annual summary. The LEGEND lived only in the sim report. Operators paging through `2010.txt` standalone had no in-file legend.

**Fix**: `_build_year_file_content` appends the full LEGEND block (shared body via `_render_legend_section`) after the annual summary. Year files are now self-contained reference documents.

Also updated the LEGEND text for the `date` column to read: "row date (last day of month for monthly; Dec 31 for annual, or actual run-end date for the partial final year)" — matches Fix #1's annual-row semantics.

### Timezone fix — `_parse_iso_datetime` normalizes naive strings to UTC-aware

**Pre-fix**: `_parse_iso_datetime` returned `datetime.fromisoformat(s)` directly. The IRAPM event log writes top-level event timestamps with an explicit UTC suffix (e.g. `"2025-04-09T04:00:00+00:00"`), but state_snapshot payloads contain nested datetime fields written as naive strings (e.g. `cb1_active_timer_started_at = "2025-04-09T00:00:00"`). Subtracting `(now_utc - entered_at)` raised `TypeError: can't subtract offset-naive and offset-aware datetimes`.

**Fix**: `_parse_iso_datetime` now normalizes naive results to UTC-aware (`dt.replace(tzinfo=timezone.utc)`) before returning. This is correct because the whole system semantically treats all stored times as UTC; the naive-vs-aware discrepancy is a serialization-format quirk, not a semantic difference.

Why this rather than a local fix at the one bug site: the naive-vs-aware mismatch could bite any other call site that mixes a `_parse_iso_datetime` result with another datetime. `_compute_days_in_cb1plus`, alert-window filtering, and several internal helpers all do this kind of arithmetic. Fixing the parser eliminates the entire class of bug.

Audited the rest of report.py for other arithmetic risks; all non-parsed datetimes in the file already construct themselves UTC-aware (`datetime.now(timezone.utc)`, `datetime(year, month, day, tzinfo=timezone.utc)`, etc.). No other call sites needed touching.

## Three deferred items affect the smoke-test results (and need eventual fixes)

These don't block Session 2 but the next person should know about them.

### Calendar-anchored ruleset fields hostile to backtesting

The pre-hack ruleset had three fields anchored to absolute production calendar dates (2027 trigger years, 2035 phase transition). The 2005-2025 sim window ends before all three, so those mechanisms were silently inert in every prior smoke test. See the same-day CHANGELOG entry "ruleset.yaml backtest hack" for full detail. The hack lets this session's smoke test exercise CPI raises, dollar-cap indexing, and the Phase 1→2 transition.

A permanent fix (sentinel `"@sim_start"` syntax in ruleset, or sim-wrapper auto-rewrite) is deferred. Until then, every backtest needs to manually shift these three fields (clearly marked `[BACKTEST-HACK]` in ruleset.yaml) and revert before any production work.

### REPORT_SPEC.md §3.3.4 field-name drifts

While auditing ruleset_model.py against REPORT_SPEC during this session, three field-name mismatches surfaced. The CODE is correct; the SPEC document is wrong:

| REPORT_SPEC §3.3.4 (wrong) | Actual ruleset_model.py field |
|---|---|
| `phase1_initial_monthly_withdrawal_dollars` | `phase1_initial_monthly_dollars` |
| `cb1_signal_threshold` | `cb1_threshold_rate` |
| `cb2_signal_threshold` | `cb2_threshold_rate` |

`report.py` uses the correct names (audited 2026-05-15 against the actual Pydantic model). REPORT_SPEC §3.3.4 still has the wrong names. A future REPORT_SPEC update should align these. Pure documentation-vs-code drift; no behavior impact.

### DRIP vs Sweep decision (cash_in column)

`_aggregate_cash_in` currently returns `Decimal("0")` with a `TODO[DRIP]` marker. Per REPORT_SPEC §10.1, the cash_in column should sum dividend reinvestments (DRIP) or dividend sweeps to cash (Sweep) depending on the operator's broker setup. The decision is deferred until the operator picks a treatment and (if Sweep) Phase 1 adds a `fill_source` field on `fill_received` events to distinguish dividend fills from rebalance fills.

Cash_in shows `0.00` everywhere in the smoke-test outputs as a result. This is honest — there's no signal lying there waiting to be reported, and the placeholder will obviously look wrong when the operator reviews. When DRIP/Sweep lands, this is a 5-line change.

## Smoke test discipline that emerged

Three things worth carrying forward:

1. **Audit payloads against an actual event, not against memory.** I lost two iterations to assuming `cb1_entered_at` was the field name. The correct name lived in the actual state_snapshot payload one `read_text_file` away. Reading the payload first would have saved time.
2. **Categorical bug-class fixes beat local fixes when the system has type asymmetries.** The timezone fix is the clearest example: a one-line normalize at the parser eliminated a whole category of `TypeError` that would otherwise lurk in every datetime-arithmetic site. The local fix was tempting (smaller blast radius) but every future hand-off would have to remember the trap.
3. **A run that "succeeded" still merits checking file mtimes.** The session contained an episode where `current_status.txt` was stale (pre-fix) while the other three outputs were fresh (post-fix), because `write_current_status` raised an exception and `smoke_report.py`'s try/except continued. The user's console log showed the FAILED step, but file-system inspection would have caught it too. For multi-output smoke tests, check that all expected outputs have current timestamps before declaring victory.

---

## Phase 2 Session 2 — Starting the next session

Phase 2 Session 1 produced the reporter. Phase 2 Session 2 (or whoever picks this up) has three workstreams, listed in priority order.

### Read these three files first

In this order:

1. **This file** (`HANDOFF_PHASE2_SESSION1.md`) — you've already read it.
2. **`REPORT_SPEC.md`** — the implementation contract for the reporter. Audit any work against this. Note the three §3.3.4 field-name drifts called out above.
3. **`HANDOFF_PHASE1_COMPLETE.md`** — the Phase 1 → Phase 2 entry point. Establishes the architectural commitments (reporter is pure consumer, etc.) that Session 1 inherited. Its "What's left" section is the forward-looking roadmap.

The conversation history that produced Session 1 is not needed. The handful of decisions are captured above; the eleven fix descriptions tell you how the reporter actually behaves at the edges.

### Workstream 1 (highest priority): Wire `report.py` invocations into cycle.py and ipms.engine

Per `HANDOFF_PHASE1_COMPLETE.md`, the reporter needs four production call sites:

1. **Every weekly cycle**: `report.write_current_status(state_dir, status_path)`.
2. **First weekly cycle of a calendar month**: also call `report.append_monthly_row_to_year_file(state_dir, output_dir, month=last_month)` for the just-ended month.
3. **First weekly cycle of January**: also call `report.close_year_file(state_dir, output_dir, year=prior_year)` and `report.prune_old_year_files(output_dir, retain_years=8)`.
4. **End of simulation run**: `report.write_simulation_report(state_dir, output_path, scenario_name, ruleset)`.

Production invocations live in `cycle.py` (steps 1-3) and `ipms.engine` or wherever IPMS finalizes a run (step 4). Wrap each in try/except so a reporter failure cannot halt the cycle. Log the exception to the existing logger.

The "what month/year is it" logic should use `clock.now()` and `clock.now().date()` — NOT `datetime.now()` — because the sim clock and production clock need the same seam here. clock.py's Protocol already provides what's needed.

Output paths: per REPORT_SPEC §2.2, `current_status.txt` and `{YYYY}.txt` files live under `paths.reports_dir` (or whatever `persistence.Paths` exposes as the operator-facing report directory). Simulation reports go to a sim-specific path passed by IPMS.

### Workstream 2: Permanent tests for event_log readers and report.py

`REPORT_SPEC.md §9` describes the test plan: golden-file fixtures (small synthetic event logs) checked into the repo, plus assertions against the produced output. Session 1 deferred this; the smoke test is the only verification currently.

Suggested fixtures:

- **`fixture_phase1_clean.jsonl`** — 6 months of Phase 1 with no CB activity, no freezes. Tests the happy path.
- **`fixture_phase3_capped.jsonl`** — 3 months of Phase 3 with dollar-cap-bound withdrawals. Tests cap-symbol rendering.
- **`fixture_cb_year.jsonl`** — one year with CB1 → CB2 → recovery sequence. Tests CB transition annotations and days-in-CB1+ counting.
- **`fixture_partial_year.jsonl`** — run ending mid-year. Tests partial-year annual-row date logic.

Each fixture has a corresponding `golden_*.txt` of expected output. Tests assert byte-equality (with platform-line-ending normalization).

Event log readers (`iter_events`, `iter_events_of_type`, `iter_events_in_window`) need their own unit tests. The Session 1 additions to event_log.py are untested as far as I know; smoke-tested in aggregate via report.py but not directly.

### Workstream 3: Address the deferred items

Each has a clear scope:

- **DRIP vs Sweep decision** (REPORT_SPEC §10.1). Discuss with operator; if Sweep, add `fill_source` field to `fill_received` events (Phase 1 writer change, minor). Then update `_aggregate_cash_in` to sum dividend fills. ~5 lines in report.py + maybe 1 day of Phase 1 writer work depending on broker integration depth.
- **Field-name corrections in REPORT_SPEC.md §3.3.4**. Three find-and-replace edits in the spec. Pure docs.
- **"of 90" denominator threading**. Widen `write_current_status` signature to accept `ruleset` (or read it from `state_dir/ruleset_used.yaml`), pass through to `_render_status_header_block`, replace hardcoded `90` with `ruleset.cb1_to_cb2_timer_days`. Small refactor, well-scoped.
- **Calendar-anchored ruleset fields** (the BACKTEST-HACK underlying issue). Real fix is either Option 2 (sim wrapper auto-rewrite) or Option 3 (sentinel `"@sim_start"` in ruleset). See the same-day CHANGELOG entry for option analysis. Bigger work; might warrant its own session.
- **SAR (semi-annual reallocation) annotation**. REPORT_SPEC documents this code but Session 1 didn't wire detection. Same mechanism as DPD/DPR — `alert_emitted` with `alert_id == "phase2_semi_annual_reallocation"`. One-line addition to `_extract_annotations_from_index`. Was deferred because it's not exercised in the smoke test's event window.

### What's left after Phase 2 Session 2

After Session 2 wires the reporter into cycle.py and adds the test suite, Phase 2 is complete. Then:

- **Phase 3** — delete the IPMS output package (`runs/{...}/balances_*.md`, `parameters.md`, etc.). Phase 2's reporter renders all of that; the old per-event MD files become redundant. Phase 3 is a `rm` plus removing the IPMS output call site.
- **Phase 4** — migrate three remaining legacy-log consumers (`CycleFailureTracker`, `check_expectations`, `annual_review.read_cb_transitions_for_year`) from `cycle.jsonl` and `cb_transitions.jsonl` over to `events.jsonl`. Each is independent.
- **Phase 5** — delete the legacy log write paths in `cycle.py` and `action_layer.py`. Only safe after Phase 4 closes all readers.

These are queued for after Phase 2 completes. Phase 2 is the load-bearing one — once cycle.py is wired up, the operator gets real reports.

---

## Honest assessment of this session's work

Eleven bugs caught during smoke-testing. None were architectural; all were "the code did X, the data has Y" mismatches. They divide into three categories:

**Spec ambiguity / spec-vs-code drift (4 bugs)**: Fixes #1+#11 (annual row date semantics), #2 (W-suppression at annual scope), #3 (SGOV out of allocation), #10 (LEGEND in year files). These are cases where REPORT_SPEC said something slightly differently than I implemented it, or didn't say it explicitly enough. Fixable by closer reading of the spec and operator's reference outputs; preventable by writing spec-first then implementation-against-spec rather than spec-and-implementation-iteratively.

**Payload-field name assumptions (3 bugs)**: Fix #7 (`cb1_entered_at` → `cb1_active_timer_started_at`), #9 (DPD/DPR from `alert_emitted` rather than dedicated event type), Fix #4 (`ach_destination` placeholder rejected by validator). Each was "I assumed the payload had field X; the actual payload has field Y." Preventable by reading an actual event before writing code that consumes it. This was the most common failure mode and the highest-leverage discipline to internalize.

**Live-vs-historical assumption (3 bugs)**: Fixes #5 (cycle_type filter), #6 (GAP rendering), #8 (anchor heuristic). These all stem from assuming the event log is a production log (recent timestamps, mostly weekly cycles) when it could be a historical/sim log (timestamps years in the past, lots of daily-token cycles). The smoke test against the 20-year baseline forced this distinction into the open. Lesson: if a feature has "live" and "historical" modes, build both at once or you'll catch it the painful way later.

**One timezone bug** that defies categorization: a subtle data-format inconsistency in the event log (top-level timestamps tz-aware, payload datetimes naive) that surfaced as a TypeError. The fix at the parser was a clear improvement over a local patch. Discipline: when the same data-shape mismatch could appear in N call sites, fix the producer or the parser, not the call site.

Three discipline-shifts that emerged or were reinforced:

1. **Read the actual data before writing code that consumes it.** Particularly for event-log payloads: the JSON structure is on disk, one tool call away. Saved time on Fix #9, lost time on Fix #7.
2. **Smoke test against a real workload, not a toy one.** The 20-year baseline exercised paths that a 1-week or 1-month toy would not: partial final year (Fix #1+#11), historical anchoring (Fix #8), DPD/DPR mechanics (Fix #9), CB freeze suffixes across multiple years (Fix #2). Toy fixtures would have shipped Session 1 with 6+ latent bugs.
3. **Check that all expected outputs are fresh before claiming victory.** The episode where `current_status.txt` was stale-but-passed-eyeball-inspection (because the OTHER three files were fresh) cost a back-and-forth. File mtimes are cheap to check.

The reporter as it stands handles every category of event-log content that the 20-year baseline produces, in both the historical and live anchoring modes. It is ready for cycle.py integration.
