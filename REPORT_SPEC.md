# IRAPM Report — Specification

**Status:** Draft complete (Phase 2, Session 1); cascade-tier rendering added Session A 2026-05-16 (pending implementation in Session C)
**Last updated:** 2026-05-16
**Authoritative document:** This file is the contract for the IRAPM reporter (`report.py`). Any code that produces report files must conform to what is described here. If implementation needs to diverge, update this document first.

**Related documents:**
- `EVENT_LOG_SPEC.md` — the data substrate this reporter consumes
- `REPORT_DECISIONS.md` — the design-rationale record this spec implements

---

## 1. Purpose and scope

### 1.1 What this reporter is

The IRAPM reporter is the operator's primary interface to the system. It consumes `events.jsonl` (per EVENT_LOG_SPEC.md) and produces human-readable text reports that describe what IRAPM is doing now and what it has done historically. SSH in, `cat current_status.txt`, see the system's current state. Open `{YYYY}.txt`, see the year's monthly history with annual summary. Open `report_*.txt`, see a complete simulator run.

The raw event log is occasionally read directly for forensic deep-dives, but routine monitoring happens through the reporter's output. The reporter is therefore the load-bearing piece of operator-facing infrastructure in IRAPM.

### 1.2 What this reporter is NOT

- **Not a runtime input to IRAPM.** Like the event log, the reporter is operator-facing infrastructure. IRAPM's cycle work does not depend on the reporter ever running.
- **Not a stateful service.** Each invocation reads the event log, produces output, and exits. The reporter has no in-memory state across invocations and no persistent state of its own.
- **Not a consumer of IRAPM's in-memory state.** With one explicit exception (the in-memory Ruleset for the simulator's RULESET section, per §3.3.4), the reporter consumes only `events.jsonl`. It does not import from `cycle.py`, `action_layer.py`, `decision_layer.py`, or any other IRAPM runtime module.
- **Not a writer of any file other than its declared outputs.** No index files, no caches, no temp files. Pure read-event-log, write-output-file.

### 1.3 Scope of this specification

This document specifies:
- The three output file types (current_status.txt, {YYYY}.txt, report_*.txt) and their content, layout, and lifecycles (§3)
- The column set used in all data tables (§4)
- The annotation system, including operator-authoritative symbols G/C/F (§5)
- Format conventions: column widths, alignment, numeric formats (§6)
- The reporter module API and invocation contract (§7)
- Error handling, missing-data behavior, and edge cases (§8)
- Testing approach (§9)
- Open questions and decisions deferred (§10)

This document does NOT specify:
- The exact implementation strategy inside `report.py` — that's the implementer's judgment, with the spec as the constraint
- The IPMS legacy output module — that is deleted in Phase 3 of the migration plan and is not part of this reporter
- Replication or backup of report files — covered separately
- Tooling for analyzing the JSON event log directly — that's a future operator-side concern

### 1.4 Authority and conflicts

The operator's direct direction overrides this specification at all times. Where this spec conflicts with the operator's stated preference (in conversation, in `REPORT_DECISIONS.md`, or anywhere else), the operator's preference wins and this spec is updated to match.

Where this spec uses domain vocabulary, the operator's vocabulary is authoritative:
- **G** (Guardrail) — Phase 3 portfolio-percent withdrawal clamp
- **C** (Cap) — Phase 3 inflation-indexed dollar ceiling
- **F** (Freeze) — CPI-increase freeze fired by prior-year CB activity

These three symbols and their meanings carry through the entire reporter. Where the event log uses `binding_ceiling: "guardrail"` / `binding_ceiling: "dollar_cap"` / `binding_constraint: "cpi_freeze"`, the reporter translates to G / C / F for display.

---

## 2. File format and physical layout

### 2.1 Format

**Plain text, UTF-8 encoding, LF line endings (not CRLF).** Every output file is rendered identically on Linux, on the operator's local workstation, and through SSH. UTF-8 box-drawing characters are used in annotation prefixes (`└─`); all modern terminals handle this correctly.

No alternative formats (no HTML, no JSON, no rich text). The reporter produces exactly one format, optimized for fixed-width terminal display.

### 2.2 File types

The reporter produces three distinct output file types, distinguished by purpose and lifecycle:

| File | Purpose | Lifecycle | Location |
|---|---|---|---|
| `current_status.txt` | Operator's "what is IRAPM doing right now" snapshot | Overwritten on every weekly cycle | Operator-configurable (production: `c:/portfolio/status/`) |
| `{YYYY}.txt` | Per-year historical record | Rebuilt on every monthly append (including running annual summary); retained 8 years rolling | Same directory as `current_status.txt` |
| `report_{YYYYMMDD_HHMMSS}_{scenario}.txt` | Simulator output for one scenario run | Written once at end of simulation; never modified | Inside the per-run output directory |

Detailed content and lifecycle for each is in §3.

### 2.3 Atomicity and crash safety

The reporter's writes use the standard pattern of "write to temp file, then atomic rename" for files that may be read concurrently. Specifically:

1. **`current_status.txt`** — written to `current_status.txt.tmp`, then renamed to `current_status.txt`. The rename is atomic on POSIX (and on Windows with the appropriate flags). A reader that opens `current_status.txt` sees either the old complete content or the new complete content; it never sees a partial write.

2. **`{YYYY}.txt`** — written to `{YYYY}.txt.tmp`, then renamed to `{YYYY}.txt`. Because the file is rebuilt from scratch on every monthly append (per §3.2), the same temp-rename pattern applies as for current_status.txt.

3. **`report_*.txt`** — written once at end of simulation, via the same temp-file-and-rename pattern. A simulation that crashes mid-write produces no report file (the temp file remains on disk and is the operator's call to investigate).

The reporter is read-only with respect to `events.jsonl`. It never modifies, truncates, or rewrites the event log.

### 2.4 No reporter state files

The reporter maintains no state of its own across invocations. It does not write index files, cache files, lock files, or progress files. Every invocation reads `events.jsonl` from start to finish (or filtered as needed by event type), constructs the output in memory, and writes it.

At the scales IRAPM operates (event log under ~15 MB over 30 years), linear scans are fast enough that no incremental processing is justified. If profiling later shows otherwise, an index can be added; pre-optimizing it now is unnecessary.

---

## 3. File types in detail

This section specifies the content layout for each of the three output file types.

### 3.1 `current_status.txt`

#### 3.1.1 Purpose and overall structure

The operator-facing snapshot of IRAPM's current state plus recent activity. Always reflects the most recent weekly cycle. Designed to be read in isolation — a single `cat current_status.txt` is sufficient to understand what IRAPM is doing right now.

The file has three blocks, separated by `=` rule lines (65 characters):

```
=================================================================
IRAPM Current Status
Generated: 2027-04-21 10:32:18 UTC  (last weekly cycle: 2027-04-21)
=================================================================
{header_block}

=================================================================
Recent activity (last 4 weeks)
=================================================================
{recent_activity_block}

=================================================================
Recent alerts (last 4 weeks)
=================================================================
{recent_alerts_block}

=================================================================
```

The trailing rule at the bottom is required.

#### 3.1.2 Header block

Plain text key-value pairs, left-aligned labels padded to align values at column 28. Field set:

```
Phase:                      PHASE_2
CB state:                   CB2 (entered 2027-04-09, day 21)
Cascade status:             SGOV+FI engaged (since 2027-04-09) — ATTENTION
Income state:               ACTIVE
Operational pause:          false
Withdrawal cap exhausted:   false

Current monthly withdrawal: $3,247.18
Last withdrawal:            $3,247.18 on 2027-04-15
Next scheduled withdrawal:  2027-05-17
```

| Field | Source | Notes |
|---|---|---|
| Phase | latest `cycle_completed.phase` | One of `PHASE_1`, `PHASE_2`, `PHASE_3` |
| CB state | latest `state_snapshot.cb_machine` | If `CB1` or higher, append `(entered YYYY-MM-DD, day N of 90)` computed from `cb1_entered_at`; otherwise just the state name |
| Cascade status | latest `state_snapshot.cascade_episode_state` | Render per the table below. Captures whether a CB2 cascade episode is active and its running depth. |
| Income state | latest `state_snapshot.income_state` | `ACTIVE` or `PAUSED` |
| Operational pause | latest `state_snapshot.operational_pause.paused` | `true` or `false` lowercase |
| Withdrawal cap exhausted | latest `state_snapshot.withdrawal_capacity_exhausted` | `true` or `false` lowercase |
| Current monthly withdrawal | derived: most recent `withdrawal_executed.scheduled_amount_dollars`, or `annual_review_completed.computed_new_withdrawal_dollars` if more recent | Dollar amount with `$` prefix, comma-thousands, 2 dp |
| Last withdrawal | most recent `withdrawal_executed`: `amount_paid_dollars` + scheduled_ach_date | If `was_capped: true`, append the cap symbol attached to cents (per §5.4): `$3,000.00G on 2027-08-15` |
| Next scheduled withdrawal | derived: the 15th of the next month (or the next business day if 15th is non-business) | Date only; the reporter does not predict cap behavior for the future withdrawal |

**Cascade status render rules** (per IRAPM §7.3.2 `cascade_episode_state`):

| Episode state | Rendered value |
|---|---|
| `episode_active = false` | `Normal` |
| `episode_active = true, sgov_engaged = false` | `CB2 active, no draw yet (since {episode_started_at})` |
| `sgov_engaged = true, fi_engaged = false` | `SGOV engaged (since {episode_started_at})` |
| `fi_engaged = true, growth_engaged = false` | `SGOV+FI engaged (since {episode_started_at}) — ATTENTION` |
| `growth_engaged = true` | `SGOV+FI+GROWTH engaged (since {episode_started_at}) — CRITICAL` |

The trailing `— ATTENTION` / `— CRITICAL` are intentional verbal weight matching the Warning / Critical severity of the underlying tier alerts (`cascade_extended_fi`, `cascade_growth_source`). They appear nowhere else in the reporter's output, so they read as exceptional when present.

If the latest data is not derivable (e.g., no `withdrawal_executed` events yet in a brand-new install), the value field shows `(none yet)`.

#### 3.1.3 Recent activity block

Four weekly data rows from the most recent four weekly cycles, with annotation lines for events occurring in each row's period. Column headers are repeated in this block.

Column set is the standard column set from §4. Each row corresponds to one weekly cycle; the row's date is the cycle's date. Column widths are computed mechanically per §6.2, independently for this block.

Rows are presented in **chronological order** (oldest first; newest at the bottom). This matches the year-file convention and reads naturally top-to-bottom as a time-series.

Annotation lines appear directly under their data row, indented and prefixed `└─ `, per §5. Events attributed to a row are events whose timestamp falls within the row's period (i.e., from the prior weekly cycle's date exclusive to this row's date inclusive).

If fewer than four weekly cycles have occurred (brand-new install), the block shows however many rows exist.

#### 3.1.4 Recent alerts block

Chronological list of `alert_emitted` events from the **last 4 weeks** (28 days), in **descending** order (newest first). Each entry is one line:

```
2027-04-15 15:30 UTC  withdrawal_executed  amount_paid_dollars=3247.18 binding_ceiling=null scheduled_amount_dollars=3247.18
2027-04-07 13:00 UTC  cb1_triggered        lookback=-0.0837
```

Format per entry:
- ISO-style timestamp with `UTC` suffix (the event log's UTC representation)
- Two spaces
- `alert_id` from the event payload, left-aligned, padded to the longest alert_id in this block
- Two spaces
- Compact representation of the `context` dict: `key=value` pairs, space-separated, in alphabetical order by key. String values are not quoted; null values are rendered `null`; decimal values render with their stored precision.

If no alerts fired in the last 4 weeks, the block contains a single line: `(no alerts in the last 4 weeks)`.

#### 3.1.5 Empty-state handling

The first weekly cycle of a brand-new IRAPM install produces a current_status.txt where:
- The header block shows whatever values exist; missing fields show `(none yet)`
- The recent activity block has one row (the just-completed cycle)
- The recent alerts block likely shows `(no alerts in the last 4 weeks)`

This is correct behavior, not an error.

### 3.2 `{YYYY}.txt`

#### 3.2.1 Purpose and overall structure

Per-year historical record. One file per calendar year of operation. The file accumulates monthly rows through year Y and includes a running annual summary that updates with each monthly append. At year-close (the first weekly cycle of January Y+1), the same rebuild logic runs once more with December's row included; the resulting file is the final closed-year record and the reporter does not write to it again.

Overall structure of the file at any point during year Y (and after closure):

```
=================================================================
IRAPM {YYYY}
=================================================================

date        cb   gross_net_liq  ...  phase
----------  ---  -------------  ...  -----
{january_row}
{january_annotations}
{february_row}
{february_annotations}
...
{latest_appended_month_row}
{latest_month_annotations}

=================================================================
{YYYY} ANNUAL SUMMARY (through {month_name} {YYYY})
=================================================================
{running_annual_summary_block}
=================================================================
```

After year-close, the summary header becomes `{YYYY} ANNUAL SUMMARY` (no "through" qualifier) and reflects the full year.

#### 3.2.2 File rebuild on monthly append

On the first weekly cycle of each month M, the reporter rebuilds `{Y}.txt` from scratch where Y is the year of month M-1. The rebuild logic:

1. Read all events for year Y from `events.jsonl`.
2. Construct rows for January through month M-1 of year Y, one per calendar month.
3. Construct the running annual summary covering January through month M-1.
4. Write the complete file via the temp-rename pattern (§2.3).

This rebuild approach is chosen for simplicity. At our scales (12 rows + a summary block, derived from at most ~1000 events per year), rebuild is trivially fast and avoids the complexity of partial-file appends.

Each monthly row reflects:
- Data values (gross_net_liq, balances, weights) from the **last `portfolio_snapshot` event whose timestamp falls within month M-1**
- Flow values (cash_in, cash_out) summed over `fill_received` events with `fill_time` in calendar month M-1
- Annotations from events in calendar month M-1

The row's `date` column shows the last day of month M-1 (e.g., `2027-04-30` for April's row).

If month M-1 had zero `portfolio_snapshot` events (production was down for the entire month), the row's data columns are blank and a `GAP` annotation is emitted; see §8.2.

#### 3.2.3 Year closure and retention

On the first weekly cycle of January Y+1:
1. The file `{Y}.txt` is rebuilt one final time, including December's row and a full-year annual summary
2. `prune_old_year_files()` runs, deleting `{YYYY}.txt` files where YYYY < (current year − 8)

After step 1, the year file is considered closed. The reporter does not write to it again in subsequent cycles.

The retention is rolling 8 years: at the start of 2028, files for 2019 and earlier are deleted; at the start of 2029, 2020 and earlier are deleted. The reporter does not back up deleted files. The event log itself is the long-term record; year files are convenience views. If long-retention year files are wanted, the operator copies them off-box before deletion.

#### 3.2.4 Annual summary block content

The annual summary block content is identical in shape whether the year is mid-progress or closed. Only the header changes: `{YYYY} ANNUAL SUMMARY (through {month_name} {YYYY})` during the year, `{YYYY} ANNUAL SUMMARY` after closure.

```
2027 ANNUAL SUMMARY
=================================================================
Year-end AUM:       $1,847,200.00  (start of year: $1,712,400.00)
Year-end change:    +$134,800.00 (+7.87%)

Total withdrawn:    $38,718.98
  - Monthly withdrawals × 12 (uncapped: 11, capped: 1)
  - Cap binding cause: guardrail (G) on 2027-08-15 (paid $3,000.00 of scheduled $3,247.18)

Asset percentage movement (Jan 1 → Dec 31):
  PYLD:    +4.20%    (18.5% → 18.4% allocation)
  JPIE:    +5.10%    (18.5% → 18.4%)
  FBCG:   +12.85%    (28.2% → 28.2%)
  AVUV:    +9.40%    (28.2% → 28.2%)
  GBIL:    +0.00%    ( 0.0% →  0.0%)
  SGOV:    +0.50%    ( 6.6% →  6.4%)

CB activity:
  CB1 triggered:     2 time(s)  (2027-04-07, 2027-08-04)
  CB2 triggered:     1 time(s)  (2027-04-09)
  Days in CB1+:      96 days
  Recovery confirmed: 2027-10-12

Cascade status (year-end):
  Current state:     Normal (last episode REC'd 2027-10-12)
  Episodes this year: 1
  Deepest tier reached: SGOV+FI (cascade_extended_fi fired 2027-05-21)

Phase transitions: none
Annual review:     2027-01-08 — CPI applied 0.0244, no freeze in effect
```

During-year version uses the same fields but reflects values through the most recent appended month. For example, a June rebuild shows "Total withdrawn: $19,483.08" (5 withdrawals so far) rather than the full-year value, and "Year-end AUM" becomes "Latest AUM" with the as-of date appended.

| Block | Source |
|---|---|
| Year-end AUM (or "Latest AUM" during year) | last `portfolio_snapshot.total_aum_dollars` in scope; "start of year" is the last snapshot of year Y-1 (or the first of year Y if Y-1 unavailable) |
| Year-end change | difference and percentage |
| Total withdrawn | sum of `withdrawal_executed.amount_paid_dollars` in scope |
| Capped count | count where `was_capped: true` |
| Cap binding cause | for each capped withdrawal: symbol (G/C/F per §5), scheduled_ach_date, amount_paid_dollars, scheduled_amount_dollars. Operator-authoritative vocabulary: "guardrail (G)", "dollar cap (C)" |
| Asset percentage movement | per-symbol price-return % and allocation-% change from first-of-period to last-of-period `portfolio_snapshot.positions` |
| CB activity | counts and dates from `cb_transition` events filtered to scope |
| Days in CB1+ | sum of days in CB1, CB2, or CB recovery stages during scope |
| Recovery confirmed | date(s) of all `cb_transition` events to `CB_INACTIVE` during scope, comma-separated. `(not confirmed in scope)` if CB was still active at end-of-scope |
| Cascade status (year-end) | three lines per the example above. `Current state` uses the same vocabulary as the current_status header field (§3.1.2), computed against the latest `state_snapshot` in scope. `Episodes this year` counts distinct CB2 episodes that started OR were active in the year (an episode straddling year boundaries counts in both years), derived from `cb_transition` events. `Deepest tier reached` is the highest cascade tier across all episodes in the year, derived from `alert_emitted` events with `alert_id` in `{cascade_engaged_sgov, cascade_extended_fi, cascade_growth_source, withdrawal_capacity_exhausted}`. If no cascade alerts fired, render `None`; otherwise the parenthetical names the deepest alert and its date. |
| Phase transitions | list of `phase_transition` events in scope, e.g., `2027-08-15: PHASE_2 → PHASE_3 (latch)`. `none` if no transitions |
| Annual review | `annual_review_completed` event in scope: timestamp + CPI applied + freeze status. If freeze was in effect, append "(F)": `2028-01-08 — CPI applied 0.0000, freeze in effect (F)` |

If a sub-block has nothing to report (e.g., zero CB activity in a clean year-to-date), it still appears with appropriate zero/none values for schema consistency.

### 3.3 `report_{YYYYMMDD_HHMMSS}_{scenario}.txt`

#### 3.3.1 Purpose and overall structure

Simulator output for a single scenario run. Written once at end of simulation, covering the full run duration. Filename includes timestamp so consecutive runs of the same scenario do not overwrite each other.

Section order:

```
================================================================================
IRAPM Simulation Report
{header_block}
================================================================================

=== TOTALS ===
{totals_block}

=== RULESET ===
{ruleset_block}

=== ANNUAL ===
{annual_table}

=== MONTHLY ===
{monthly_table}

=== LEGEND ===
{legend_block}

================================================================================
```

The outer rule is 80 `=` characters. Section header rules (`=== SECTION ===`) are on their own line with one blank line before and after.

#### 3.3.2 Header block

```
Scenario: baseline_20yr_phase3_2016
Run:      2026-05-14 16:32:18 UTC
Window:   2005-04-15 -> 2025-04-16
================================================================================
Starting balance:  $668,500.00
Terminal AUM:      $3,304,781.68
Final phase:       PHASE_3
Final CB state:    -
Total withdrawn:   $912,450.00
```

| Field | Source |
|---|---|
| Scenario | scenario name from caller (passed to `write_simulation_report`) |
| Run | wall-clock timestamp of when the report was generated, UTC |
| Window | first event timestamp -> last event timestamp |
| Starting balance | first `portfolio_snapshot.total_aum_dollars` in the log |
| Terminal AUM | last `portfolio_snapshot.total_aum_dollars` in the log |
| Final phase | last `cycle_completed.phase` |
| Final CB state | last `state_snapshot.cb_machine.state`, rendered as `-` / `1` / `2` / `1+2` |
| Total withdrawn | sum of all `withdrawal_executed.amount_paid_dollars` |

The header is intentionally a brief at-a-glance summary. The TOTALS section that follows expands this with operational counts; some overlap between the two is acceptable since the header serves as a quick scan and TOTALS serves as the structured detail.

#### 3.3.3 TOTALS section

Flat one-metric-per-line layout (REPORT_DECISIONS Option A):

```
=== TOTALS ===

Run window:            2005-04-15 -> 2025-04-16  (20 years, 0 months)
Starting AUM:          $668,500.00
Terminal AUM:          $3,304,781.68
Net change:            +$2,636,281.68 (+394.36%)
Total withdrawn:       $912,450.00  (240 monthly withdrawals)
Withdrawals capped:    8 total
  - Guardrail (G):     6
  - Dollar cap (C):    2
CPI freezes (F):       3   (2010, 2012, 2021)
CB1 triggers:          18
CB2 triggers:          9
CB1 -> CB2 promotions: 7
Recoveries confirmed:  17
Days in CB1+:          1,847 days (25.3% of run)
Cascade episodes:      9 total
  SGOV engaged:        9 (every CB2 episode)
  FI extended:         3 (2008, 2009, 2020)
  Growth extended:     1 (2009-03)
  Capacity exhausted:  0
Phase 2 transition:    2018-03-12
Phase 3 latch:         2021-07-09
```

Sources per line, all filtered to the run window:

| Line | Source |
|---|---|
| Run window | first event timestamp, last event timestamp, computed years-and-months span |
| Starting AUM, Terminal AUM, Net change | per §3.3.2; net change = terminal − starting, percentage included |
| Total withdrawn (count) | sum of `withdrawal_executed.amount_paid_dollars`; count = number of `withdrawal_executed` events |
| Withdrawals capped (total) | count where `withdrawal_executed.was_capped == true` |
| Guardrail (G) count | count where `binding_ceiling == "guardrail"` |
| Dollar cap (C) count | count where `binding_ceiling == "dollar_cap"` |
| CPI freezes (F) | count where `annual_review_completed.binding_constraint == "cpi_freeze"`; parenthetical lists the review years |
| CB1 triggers | count of `cb_transition` events with `to_state == "CB1"` and `from_state == "CB_INACTIVE"` |
| CB2 triggers | count of `cb_transition` events with `to_state == "CB2"` and `from_state == "CB_INACTIVE"` |
| CB1 -> CB2 promotions | count of `cb_transition` events with `from_state == "CB1"` and `to_state == "CB2"` |
| Recoveries confirmed | count of `cb_transition` events with `to_state == "CB_INACTIVE"` and `from_state` being a CB or recovery state |
| Days in CB1+ | total days the system was in any non-INACTIVE CB state (CB1, CB2, or any recovery stage). Computed by walking `cb_transition` events and summing intervals |
| Cascade episodes (total) | count of CB2-entry `cb_transition` events in scope (equivalent to `CB2 triggers` above; listed again here as the parent of the cascade-tier sub-lines for visual continuity) |
| SGOV engaged | count of `alert_emitted` events with `alert_id == "cascade_engaged_sgov"` in scope. In a clean run this equals `Cascade episodes total` (every CB2 episode draws SGOV in week 1). A divergence is a forensic signal — typically a CB2 episode that REC'd before its first scheduled withdrawal |
| FI extended | count of `alert_emitted` events with `alert_id == "cascade_extended_fi"`; parenthetical lists the deduped years in which the alert fired |
| Growth extended | count of `alert_emitted` events with `alert_id == "cascade_growth_source"`; parenthetical lists the year-month of each occurrence |
| Capacity exhausted | count of `alert_emitted` events with `alert_id == "withdrawal_capacity_exhausted"` in scope |
| Phase 2 transition | timestamp of `phase_transition` event with `to_phase == "PHASE_2"`. `(not reached)` if no such event |
| Phase 3 latch | timestamp of `phase_transition` event with `is_phase3_activation == true`. `(not reached)` if no such event |

Field labels are left-padded to align values at a consistent column. Subordinate lines (Guardrail, Dollar cap) indent under their parent.

#### 3.3.4 RULESET section

The tuning-relevant ruleset values used for this run. The reporter accepts a `Ruleset` object as input and selects the fields listed below for display. The full ruleset is copied verbatim to `ruleset_used.yaml` in the run directory by the simulator wrapper (§7); this section is a curated tuning-readable view.

The Ruleset Pydantic model is the **one IRAPM module the reporter touches** beyond `event_log.py`. Per §1.2 this is the only exception to the pure-consumer rule and is necessary because scenario-specific ruleset overrides exist only in the in-memory Ruleset; re-parsing `ruleset.yaml` would miss them.

Format: one field per line, label left-padded to align values. Groups separated by blank lines (no group sub-headers; the groupings are conventional rather than syntactic):

```
=== RULESET ===

Withdrawal mechanics:
  phase1_initial_monthly_withdrawal_dollars:       3000.00
  phase3_monthly_payment_ceiling_rate:             0.0500
  phase3_dollar_ceiling_base_dollars:              4000.00
  phase3_dollar_ceiling_base_year:                 2020
  inflation_rate:                                  0.0244

Phase 3 I_0 calculation:
  phase3_i0_calc_return_assumption:                0.0500
  phase3_i0_calc_inflation_assumption:             0.0244
  phase3_i0_calc_horizon_years:                    30

CB thresholds:
  cb1_signal_threshold:                            -0.0800
  cb2_signal_threshold:                            -0.1500
  freeze_evaluation_threshold_days:                30

Buffer mechanics:
  sgov_buffer_target_months:                       12
  cash_buffer_offset_dollars:                      750.00
```

If the Ruleset object is not provided to `write_simulation_report` (e.g., post-hoc regeneration from event log alone), the section is rendered as:

```
=== RULESET ===

(ruleset object not available for this run — see ruleset_used.yaml in the run directory)
```

#### 3.3.5 ANNUAL section

One row per calendar year of the run. Row date is `YYYY-01-01` for year Y; row data is the **first `portfolio_snapshot` of year Y** (or last of year Y-1, whichever is later — they're typically the same week). Annotations under the row are events that occurred in the **prior 12 months** (i.e., the 2009-01-01 row shows events from calendar year 2008).

This is the spec's authoritative interpretation of REPORT_DECISIONS' "annual events attributed to the prior 12 months ending at that snapshot." It is operator-confirmed.

The `cb` column reflects CB state **at the row's date** (2009-01-01), not the worst state during the prior year. If the operator wants to see in-year extremes, the annotation line surfaces them (CB1, CB2 transitions are listed).

**AR suppression at annual scope.** The `AR` annotation is suppressed in this section. Annual review fires every January 15; it is a monthly-scope event and appears on the appropriate row in the MONTHLY section. Rendering it here as well would duplicate the signal and crowd the annotation line. All other annotation codes per §5.2 are eligible at annual scope.

Column widths in this section are computed **jointly with the MONTHLY section** (§6.3), so the two tables align visually across the section boundary.

#### 3.3.6 MONTHLY section

One row per calendar month of the run. Row date is `YYYY-MM-{last_day_of_month}`. Row data is the **last `portfolio_snapshot` of the month**. Flow values aggregate over the calendar month. Annotations list events in the calendar month.

Cascade-tier annotations (per §5.2) and the CB2 suffix encoding (per §5.3) appear in this section on the rows where each cascade event occurred. The ANNUAL section (§3.3.5) does NOT render these — cascade behavior is a monthly-scope phenomenon and rolls up to the year via the TOTALS cascade summary (§3.3.3) instead.

Column widths shared with the ANNUAL section (§6.3).

#### 3.3.7 LEGEND section

Static block explaining columns, annotation codes, and cap symbols. Content:

```
=== LEGEND ===

cb column:     -      no CB active
               1      CB1 active
               2      CB2 active
               1+2    both CB1 and CB2 entry conditions active

annotations:   AR     annual review
               CB1    CB1 transition entered
               CB2    CB2 transition entered
               CSC-S  cascade engaged SGOV buffer
               CSC-F  cascade extended into FI bucket
               CSC-G  cascade reached Growth bucket
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

CB2 suffix:    (S)    cascade engaged SGOV during episode
               (SF)   cascade extended into FI during episode
               (SFG)  cascade reached Growth during episode
                      Appended to CB2 annotation: e.g., "CB2:2027-04-09 (SFG)"
                      Suffix shows the episode's terminal depth at
                      rebuild time; closed-year files are stable.

columns:
  date          row date (last day of month for monthly, Jan 1 for annual)
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
  phase         operating phase (1, 2, or 3)
```

---

## 4. Column set

Identical column set across all three file types and both sections of the simulator output (ANNUAL and MONTHLY). The exact order, left-to-right:

| # | Column | Type | Source |
|---|---|---|---|
| 1 | `date` | date | row date (semantics vary by file/section, defined in §3) |
| 2 | `cb` | string | `-`, `1`, `2`, or `1+2` per CB state at row date |
| 3 | `gross_net_liq` | dollar | `portfolio_snapshot.total_aum_dollars` |
| 4 | `cash_buff` | dollar | `portfolio_snapshot.cash_dollars` |
| 5 | `sgov_buffer` | dollar | `portfolio_snapshot.sgov_buffer_dollars` (i.e., positions["SGOV"].market_value_dollars) |
| 6 | `fi_bucket` | dollar | sum of positions PYLD + JPIE + GBIL market values |
| 7 | `growth_bucket` | dollar | sum of positions FBCG + AVUV market values |
| 8 | `fi_wt` | weight | `fi_bucket / investable_aum` |
| 9 | `gr_wt` | weight | `growth_bucket / investable_aum` |
| 10 | `cash_in` | dollar | sum of fill_received.fill_dollar_amount where side=="SELL" but originating from non-withdrawal entries (initial deposits, dividends), aggregated over row period |
| 11 | `cash_out` | dollar | sum of withdrawal_executed.amount_paid_dollars during row period; if any withdrawal in the period was capped, append the cap symbol(s) directly to the dollar value with no space |
| 12 | `pyld` | dollar | positions["PYLD"].market_value_dollars |
| 13 | `jpie` | dollar | positions["JPIE"].market_value_dollars |
| 14 | `fbcg` | dollar | positions["FBCG"].market_value_dollars |
| 15 | `avuv` | dollar | positions["AVUV"].market_value_dollars |
| 16 | `gbil` | dollar | positions["GBIL"].market_value_dollars |
| 17 | `phase` | integer | numeric form of `cycle_completed.phase` (1, 2, or 3) |

**Definitions:**

- **`investable_aum`** = `total_aum_dollars - sgov_buffer_dollars - cash_dollars`. This is the denominator for `fi_wt` and `gr_wt` so the weights reflect the bucket split of the investable portion of the portfolio, not of total AUM.

- **GBIL placement in fi_bucket** — GBIL is zero in Phase 1 (not yet held) and Phase 3 (sold off in the Phase 2→3 transition). It is non-zero only during Phase 2 where it is part of the FI allocation. Including it in `fi_bucket` always is the simplest convention and matches operator preference; it never distorts Phase 1 or Phase 3 weights because it's zero in those phases.

- **`cash_out` cap symbol attachment** — when one or more withdrawals in the row's period was capped, the cap symbol(s) attach directly to the cash_out cell value with no whitespace: `3,000.00G` (guardrail bind), `3,891.20C` (dollar cap bind). If multiple caps fire in the same period across multiple withdrawals, multiple symbols may attach: `5,891.20GC`. This is rare in practice (months have one scheduled withdrawal each) but the encoding supports it.

- **`cash_in` semantics** — Inflows into the portfolio that are not withdrawal-funding sells. Includes the initial deposit (a SELL-from-cash treated as inflow in the first cycle) and any dividend/interest credits that produce fill_received-like events. Capital-gains distributions are handled the same way. Buy-side activity (FBCG buys etc.) is not cash_in; it's a within-portfolio reallocation. Note: distinguishing dividend fills from rebalance sells is an open implementation question pending the DRIP-vs-Sweep portfolio-management decision (§10.1).

- **Empty positions** — Symbols with zero balance still appear with `0.00` values, not blank. This keeps the column structure stable across phases (PYLD/JPIE drop to zero post-Phase-2; GBIL is zero pre-Phase-2 and post-Phase-3-latch).

---

## 5. Annotation system

### 5.1 Annotation lines

When events occur during a row's period, they are listed on a separate line directly below the data row:

```
2008-01-31  1   845,210.00   4,330.00 ...
            └─ AR:2008-01-02, CB1:2008-01-09, W:2008-01-15
```

- Indented to align with the `date` column's content (not the column edge — the indent matches where the date string starts)
- Box-drawing prefix `└─ ` (UTF-8)
- Events in **chronological order** within the period (by event timestamp; events on the same day are ordered by event_id which is generation-order)
- Each event annotation is `EVENT_CODE:YYYY-MM-DD`
- Multiple annotations separated by `, ` (comma-space)
- Suffix flags (e.g., `(F)`) attach to the annotation with a single space: `AR:2010-01-06 (F)`

If a row's period contained no annotation-worthy events, no annotation line is emitted.

### 5.2 Annotation event codes

| Code | Meaning | Source event |
|---|---|---|
| `AR` | Annual review fired | `annual_review_completed` |
| `CB1` | CB1 transition entered | `cb_transition` with `to_state == "CB1"` and from CB_INACTIVE |
| `CB2` | CB2 transition entered | `cb_transition` with `to_state == "CB2"` |
| `CSC-S` | Cascade engaged SGOV (first cycle of episode drawing SGOV) | `alert_emitted` with `alert_id == "cascade_engaged_sgov"` |
| `CSC-F` | Cascade extended into FI (first cycle of episode drawing FI) | `alert_emitted` with `alert_id == "cascade_extended_fi"` |
| `CSC-G` | Cascade reached Growth (first cycle of episode drawing Growth) | `alert_emitted` with `alert_id == "cascade_growth_source"` |
| `REC` | Recovery confirmed (CB cleared) | `cb_transition` with `to_state == "CB_INACTIVE"` |
| `W` | Scheduled withdrawal executed | `withdrawal_executed` |
| `RFL` | SGOV buffer refill executed | `decision_made.plan_entries` where kind == "BUFFER_REFILL" and subsequent fill events confirm execution |
| `P2` | Phase 2 transition | `phase_transition` with `to_phase == "PHASE_2"` |
| `P3` | Phase 3 latch | `phase_transition` with `is_phase3_activation == true` |
| `DPD` | Dry powder deployed | future: when DRY_POWDER_DEPLOY entries are emitted |
| `DPR` | Dry powder refilled | future: when DRY_POWDER_REFILL entries are emitted |
| `HALT` | Cycle halted | `cycle_halted` |
| `PAUSE` | Operational pause activated | `alert_emitted` with alert_id == "operational_pause" (or future dedicated event) |
| `GAP` | No portfolio data for this period | derived: no `portfolio_snapshot` events in the row's period |

### 5.3 Suffix flags on annotations

A small set of flags attach to specific annotations as parenthetical suffixes:

| Flag | Attached to | Meaning |
|---|---|---|
| `(F)` | `AR` | Annual review applied CPI freeze (no withdrawal increase) |
| `(S)` | `CB2` | Cascade engaged SGOV in this episode (running latch) |
| `(SF)` | `CB2` | Cascade extended into FI in this episode |
| `(SFG)` | `CB2` | Cascade reached Growth in this episode |

When an `AR` annotation has a freeze in effect, it is rendered `AR:YYYY-MM-DD (F)`. Otherwise just `AR:YYYY-MM-DD`. Source: `annual_review_completed.binding_constraint == "cpi_freeze"`.

The **CB2 suffix** renders the episode's terminal cascade depth at rebuild time, derived from the engagement flags on `cascade_episode_state` (per IRAPM §7.3.2). Since the CB2 annotation appears only on the row where the CB2 transition occurred (CB2 entry is a one-time event), the suffix on that one annotation is rendered as the **deepest depth reached during the entire episode** when the year-file or sim report is rebuilt. For example: an April CB2 entry whose episode reaches FI in May and Growth in June renders `CB2:2027-04-09 (SFG)` once the June rebuild runs. Mid-episode rebuilds may show the suffix updating between cycles; closed-year files are stable. CSC-S/F/G annotations (§5.2) mark the *point-in-time* escalation events on their respective rows; the CB2 suffix is the *whole-episode* summary on the entry row.

Future flags may be added without breaking the format.

### 5.4 Cap symbols on cash_out cell

Distinct from annotation flags. Cap symbols (`G`, `C`) attach directly to the dollar amount in the cash_out column with **no whitespace**. This is the operator-preferred encoding to minimize column-width distortion.

| Symbol | Source | Rendering |
|---|---|---|
| `G` | `withdrawal_executed.binding_ceiling == "guardrail"` | `3,000.00G` |
| `C` | `withdrawal_executed.binding_ceiling == "dollar_cap"` | `3,891.20C` |

Multiple symbols on one cell are concatenated alphabetically: `5,891.20CG` (would only occur if multiple withdrawals capped in the same period; not expected in normal operation but the encoding supports it).

In the production `current_status.txt` header's "Last withdrawal" line, the same encoding is used: `$3,000.00G on 2027-08-15`.

The `F` (CPI freeze) symbol is **not** a cash_out cell attachment — it attaches to the AR annotation only (§5.3). This is because freezes affect future withdrawals (by suppressing the CPI increase that would have raised the monthly amount), not the withdrawal currently being executed.

---

## 6. Format conventions

### 6.1 Numeric and date formats

| Type | Format | Examples |
|---|---|---|
| Dollar amount | Comma-separated thousands, exactly 2 decimal places. No currency symbol within data tables; `$` prefix only in current_status.txt header. | `1,234,567.89`, `0.00`, `$3,247.18` |
| Weight (ratio 0–1) | Exactly 2 decimal places, no leading zero suppression. | `0.40`, `0.55`, `1.00` |
| Percentage | Signed, exactly 2 decimal places, `%` suffix. Used in annual summary only, not in data tables. | `+7.87%`, `-12.30%`, `+0.00%` |
| Date | ISO 8601 calendar date. | `2027-04-30` |
| Datetime | ISO-style with `UTC` suffix, no fractional seconds. | `2027-04-15 15:30 UTC` |
| Integer count | No thousands separator under 10,000; comma separator at 10,000 and above. | `42`, `1,847` |
| `cb` column | One of: `-`, `1`, `2`, `1+2`. | |
| `phase` column | Single digit. | `1`, `2`, `3` |

**Capped withdrawal cash_out** — dollar value followed immediately by cap symbol(s), no whitespace: `3,000.00G`, `3,891.20C`, `5,891.20CG`. The suffix is part of the cell's value for column-width calculation purposes (§6.2).

**Negative dollar amounts** — not expected in normal operation (cash_in and cash_out are non-negative by construction). If a negative value somehow arises, it is rendered with a leading minus: `-100.00`. This is a forensic signal that something is wrong; the reporter does not attempt to interpret it.

**Null or missing data** — rendered as blank in data tables, never as `null` or `N/A`. The annotation line (or a `GAP` annotation) carries the explanation. In key-value blocks (headers, summaries), missing data renders as `(none yet)` or a context-appropriate phrase.

### 6.2 Column width rule

For any data table in any file, column widths are computed mechanically:

```
column_width = max(len(header), max(len(cell)) for cell in column_data)
```

The cell value used in `len(cell)` is the rendered string per §6.1, **including any cap symbol suffix** on cash_out cells. So a cash_out column containing one capped value `3,000.00G` (9 chars) and many uncapped values `3,247.18` (8 chars) computes to width 9; the uncapped values are right-padded to 9 chars to align.

**Separator:** exactly 2 spaces between columns. This is wide enough that adjacent columns remain visually distinct even at the maximum width, and narrow enough that the overall table doesn't bloat unnecessarily.

**Header underline:** a row of `-` characters under the header, one per character of header width per column, separated by the same 2-space separator. Each column's underline is exactly the column's computed width:

```
date        cb  gross_net_liq  cash_buff
----------  --  -------------  ---------
```

**Per-table independence (default).** Each data table in a file computes its own widths. The recent-activity block in current_status.txt computes its own; each year file computes its own; the ANNUAL and MONTHLY tables in a simulator report **share** a computation per §6.3.

### 6.3 Joint width computation for simulator report

In the simulator report, the ANNUAL and MONTHLY sections are computed as a single combined width pass: the widest value in either section determines the column width for both. This ensures the two tables visually align when read sequentially.

Implementation: the reporter computes the row data for both sections, then computes column widths from the union of cells across both sections, then renders both tables using those shared widths.

### 6.4 Alignment

| Column type | Alignment |
|---|---|
| `date`, `cb`, `phase` | Left-aligned |
| All dollar columns | Right-aligned |
| `fi_wt`, `gr_wt` | Right-aligned |

Right-aligned columns pad with leading spaces. Left-aligned columns pad with trailing spaces.

Header text is left-aligned for all columns regardless of data alignment. This means in a right-aligned dollar column, the header sits at the left of the column width and the data values sit at the right. This is the conventional "header is a label" treatment and reads correctly under fixed-width display.

### 6.5 Rule lines

| Context | Width | Character |
|---|---|---|
| `current_status.txt` block separators | 65 | `=` |
| `{YYYY}.txt` block separators | 65 | `=` |
| Simulator report outer rules | 80 | `=` |
| Simulator report section headers | `=== {NAME} ===` literal | `=` |
| Header underlines (under column headers) | per-column computed width | `-` |

All rule lines stand on their own line with no trailing whitespace.

### 6.6 Blank lines

Between major sections (after a block separator rule, before the next data block or rule) there is exactly one blank line. Within a data table, no blank lines appear between rows (annotation lines are not blank lines).

After the annotation line for the final row in a table, one blank line precedes the next block separator.

---

## 7. Module API

### 7.1 Module location

The reporter lives at `report.py` at the repo root, peer to `cycle.py`, `event_log.py`, and `irapm_driver.py`. This placement matches the import direction of the rest of IRAPM: reporters and drivers at repo root; modules deeper in the tree are imported by them, not the reverse.

The reporter has no internal state and no class-level instance. It is a stateless module that exposes the five public functions in §7.2.

### 7.2 Public functions

```python
def write_current_status(
    state_dir: Path,
    output_path: Path,
) -> Path:
    """Generate current_status.txt content from the event log at
    state_dir/events.jsonl and write to output_path (overwriting via
    temp-rename). Returns output_path."""


def append_monthly_row_to_year_file(
    state_dir: Path,
    output_dir: Path,
    month: date,
) -> Path:
    """Rebuild {output_dir}/{month.year}.txt to include the row for
    the given month (and all prior months in that year) plus the
    running annual summary through that month. Creates the file if
    it does not exist. Returns the file path.

    The function rebuilds the whole file per §3.2.2; the name
    preserves the conceptual API from REPORT_DECISIONS even though
    the implementation is a full rebuild."""


def close_year_file(
    state_dir: Path,
    output_dir: Path,
    year: int,
) -> Path:
    """Rebuild {output_dir}/{year}.txt one final time with December's
    row included and a full-year (no 'through' qualifier) annual
    summary. After this call, the year file is considered closed and
    the reporter will not write to it again. Returns the file path."""


def prune_old_year_files(
    output_dir: Path,
    retain_years: int = 8,
) -> list[Path]:
    """Delete {YYYY}.txt files older than retain_years from output_dir.
    'Older' is determined by year-of-filename relative to the current
    calendar year. Returns the list of deleted file paths."""


def write_simulation_report(
    state_dir: Path,
    output_path: Path,
    scenario_name: str,
    ruleset: Optional[Ruleset] = None,
) -> Path:
    """Generate a full simulation report from the event log and write
    to output_path. scenario_name appears in the header and is
    expected to match the output_path's filename. If ruleset is
    provided, the RULESET section is rendered with curated values
    (§3.3.4); if None, the section is rendered as 'not available'
    placeholder text. Returns output_path."""
```

All five functions are pure consumers of `events.jsonl` (plus the Ruleset object for the simulator function). They read events sequentially, build output strings in memory, and write via the temp-rename pattern. They never read in-memory IRAPM state, never call into other IRAPM modules (except for the `Ruleset` import), and never depend on the broker or harness.

### 7.3 Invocation contract — production

The reporter is invoked from `cycle.py` at the end of each weekly cycle. Four invocations per weekly cycle, with conditions:

```python
# Always, on every weekly cycle:
write_current_status(state_dir, output_path=status_dir / "current_status.txt")

# First weekly cycle of each month — append prior month's row:
if is_first_weekly_cycle_of_month:
    prior_month = (now - relativedelta(months=1)).date().replace(day=1)
    append_monthly_row_to_year_file(state_dir, output_dir=status_dir, month=prior_month)

# First weekly cycle of January — close prior year, then prune:
if is_first_weekly_cycle_of_january:
    close_year_file(state_dir, output_dir=status_dir, year=now.year - 1)
    prune_old_year_files(output_dir=status_dir, retain_years=8)
```

The reporter is invoked from `cycle.py`'s post-cycle hooks, not from `event_log.py`. The event log writer emits events as IRAPM runs; the reporter consumes events when explicitly invoked. This separation keeps the writer focused and the reporter independently testable.

Order matters: in January, `append_monthly_row_to_year_file` for December's row runs first (depositing December into the prior year's file), then `close_year_file` for the prior year (which rebuilds the file one final time with the full-year summary header), then `prune_old_year_files`.

The reporter never raises exceptions to the cycle driver. A reporter failure logs at WARNING level and the cycle continues. See §8.1.

### 7.4 Invocation contract — simulation

The simulator harness invokes `write_simulation_report` exactly once at end-of-run, after all scenario cycles have completed and the event log has been flushed. The invocation:

```python
write_simulation_report(
    state_dir=run_state_dir,
    output_path=run_output_dir / f"report_{timestamp}_{scenario_name}.txt",
    scenario_name=scenario_name,
    ruleset=scenario.ruleset,
)
```

The simulator wrapper is responsible for:
- Creating the output directory if needed
- Computing the timestamp portion of the filename
- Passing the in-memory `Ruleset` object (which reflects any scenario-specific overrides)
- Copying the full `ruleset.yaml` to `{run_output_dir}/ruleset_used.yaml` (separate from the report)

The reporter is responsible only for reading the event log, formatting the report, and writing it to the path it was given.

### 7.5 Return values

Each public function returns the `Path` it wrote (or the list of paths it deleted, for `prune_old_year_files`). Callers may use these returned paths for logging or follow-up operations; the reporter itself does not log its own actions.

### 7.6 Caller error handling

Callers should wrap reporter invocations in a broad exception handler so a reporter failure cannot break a cycle:

```python
try:
    write_current_status(state_dir, output_path)
except Exception as e:
    logger.warning("Reporter write_current_status failed: %s", e)
```

This matches the event log writer's own "failure to log must never break a cycle" rule (EVENT_LOG_SPEC §5.4). The reporter has the same operational status: useful operator-facing infrastructure, never load-bearing.

---

## 8. Error handling and edge cases

### 8.1 Reporter-internal error handling philosophy

The reporter follows the same principle as the event log writer: **a reporter failure must never break a cycle**. Callers wrap invocations as shown in §7.6; the reporter itself raises only on programmer-error conditions (e.g., calling `close_year_file` with a non-integer year). All runtime failures — parse errors in events.jsonl, disk-full during write, permission errors on output_dir — are handled internally where possible and logged at WARNING level when not.

The reporter's posture: **produce partial output rather than no output.** A few corrupted events at the end of events.jsonl should not prevent the reporter from rendering the rows it can construct from the events that parsed cleanly.

### 8.2 GAP rendering for missing portfolio data

When a row's period contains zero `portfolio_snapshot` events, the row is rendered with:
- The `date` column populated normally (last day of month, or row date per file type)
- The `cb` column populated from the latest `state_snapshot` prior to the period (best-effort)
- The `phase` column populated from the latest `cycle_completed` prior to the period
- All other data columns rendered as blank (no value, no zero — empty fixed-width field)
- A `GAP` annotation: `└─ GAP`

The `GAP` annotation may co-exist with other annotations from the period (e.g., a CB transition occurred but no portfolio snapshot was emitted afterward); they are listed together in chronological order, with `GAP` placed first as a structural indicator:

```
2027-08-31  1
            └─ GAP, CB1:2027-08-04, W:2027-08-15
```

In annual summary blocks, GAP months are noted explicitly: `Months with GAP: 1 (August)`.

### 8.3 Partial GAP handling

A row's period may have **some** `portfolio_snapshot` events but be unusually sparse (e.g., production was down for 3 of 4 weeks). The reporter does not distinguish between full coverage and partial coverage — it uses the events that exist and renders the row normally. The annotation line will reflect whatever events fired during the period; if no annotations exist but the operator suspects coverage was thin, the event log itself is the authoritative source.

A future enhancement could add a `GAP-PARTIAL` annotation when fewer than the expected number of cycles occurred in a period; this is deferred (§10.3).

### 8.4 Parse errors in events.jsonl

Per the EVENT_LOG_SPEC §6.1 reader invariants:
- A partial trailing line (last line lacks LF terminator) is silently skipped
- A line that fails JSON parse is logged at DEBUG level and skipped
- A line with an unknown `event_type` is silently skipped (forward-compat)
- A line with a `schema_version` major-version newer than 1.x is logged at WARNING and skipped

The reporter never aborts on parse error. It produces output from whatever events parsed cleanly, which is preferable to producing nothing.

### 8.5 Missing optional fields in payloads

For forward-compatibility, if an event payload is missing a field the reporter would normally use (because the event was written by a future schema version that the reporter doesn't fully understand), the reporter substitutes a default value:
- Missing dollar value: treat as `0.00`
- Missing optional flag: treat as `false`
- Missing date: treat as `(unknown)`

The reporter does not log warnings for these substitutions; they are part of normal forward-compat operation.

### 8.6 Output directory missing or unwritable

If the output directory does not exist, the reporter creates it (with default permissions). If creation fails (parent directory unwritable, etc.), the function raises and the caller handles per §7.6.

If the output directory exists but the temp file or rename fails (disk full, filesystem read-only), the function raises with the OSError. The caller catches and logs; no output is produced for this invocation but the previous output (if any) remains intact on disk thanks to the temp-rename pattern.

### 8.7 Empty event log

If `events.jsonl` does not exist or is empty:
- `write_current_status` produces a minimal file with all fields showing `(none yet)` and a single line in each block indicating no data
- `append_monthly_row_to_year_file` produces a year file containing only the file header and a "(no events recorded)" placeholder for the month
- `write_simulation_report` produces a header block followed by "(no events recorded for this run)" and skips the TOTALS, ANNUAL, and MONTHLY sections (RULESET still renders if provided)

This is correct behavior, not an error. An empty event log on a real production install is itself a signal worth investigating, but the reporter's job is to faithfully render what exists.

### 8.8 Event log file locking

The reporter does not acquire any lock on `events.jsonl`. Per EVENT_LOG_SPEC §2.3, the writer's append-mode pattern is safe for concurrent reads, and readers tolerate partial trailing lines. A reporter invocation that races with an in-progress event log write will see at most one partial trailing line, which is silently skipped per §8.4.

### 8.9 Output file already exists

For `current_status.txt` and `{YYYY}.txt`, overwriting is expected behavior. The temp-rename pattern ensures atomicity.

For `report_*.txt`, the filename includes a timestamp to seconds, so collisions are unlikely. If a collision occurs (two runs of the same scenario started within the same second), the second run overwrites the first. This is a corner case acceptable for current operations; future enhancement could add a collision-detection sequence number if needed.

### 8.10 Time zone handling

All event log timestamps are UTC per EVENT_LOG_SPEC §3.2. The reporter renders timestamps in UTC by default. The "Generated:" line in `current_status.txt` is wall-clock UTC at the moment of reporter invocation.

Local-time display is not supported by the reporter. If the operator wants local time, they convert mentally or use a downstream tool. Keeping everything in UTC eliminates an entire class of timezone-conversion bugs in the reporter.

---

## 9. Testing

### 9.1 Test philosophy

The reporter is a pure function of its inputs (the event log and, for sims, the Ruleset). It produces deterministic text output given deterministic input. This makes it well-suited to **golden-file testing**: a known event log fixture produces a known output fixture, and any change to the output is either a deliberate format change (update the fixture) or a regression (fix the code).

Test event logs are constructed as JSONL strings within the tests themselves — they are not produced by running IRAPM. This keeps reporter tests fully independent of the rest of the IRAPM test suite.

### 9.2 Test file location

Tests live at `test_report.py` at the repo root, peer to `report.py` and `test_event_log.py`. They run as part of the standard IRAPM test suite via pytest.

### 9.3 Coverage targets

Each public function has at least:

| Function | Tests |
|---|---|
| `write_current_status` | Happy path; brand-new install (empty event log); CB active state; Phase 3 capped withdrawal in last-withdrawal line; recent-alerts ordering; longest-alert-id padding |
| `append_monthly_row_to_year_file` | First month of year (creates file); mid-year (rebuilds with running summary); month with GAP; month with capped withdrawal (G symbol in cash_out); annotation chronological ordering |
| `close_year_file` | Standard year-close; year with CB activity (multiple triggers, recovery date); year with phase transition; year with CPI freeze (F flag on AR annotation) |
| `prune_old_year_files` | Files within retention kept; files outside retention deleted; missing output_dir; empty output_dir; retain_years=0 (deletes all) |
| `write_simulation_report` | 20-year baseline scenario; scenario with Phase 3 latch; scenario with multiple G and C caps; missing Ruleset (placeholder rendering); joint width computation correctness |

### 9.4 Edge cases that must be covered

- Empty event log (each function)
- Event log with parse errors mid-stream (reporter recovers and continues)
- Event log with unknown event types (silently skipped)
- Event log with schema_version 2.0 entries mixed with 1.0 (2.0 entries skipped with warning)
- Month with zero portfolio_snapshot events (GAP annotation)
- Multiple capped withdrawals in same period (multi-symbol concatenation: `5,891.20CG`)
- Cap symbol attachment causes column-width recalculation
- Joint width computation across ANNUAL and MONTHLY sections
- Asset values crossing $1M boundary (column widens to accommodate)
- Annotation ordering when multiple events occur on same calendar date
- AR annotation with (F) suffix vs without
- Recovery confirmed but CB was still active at end-of-year (`(not confirmed in scope)`)
- Year-close in a year with zero CB activity (clean summary)
- Brand-new install: first weekly cycle produces a minimal but valid current_status.txt

### 9.5 Golden-file fixtures

Two golden-file fixtures are checked into the test suite:

1. **`fixtures/report_baseline_20yr.txt`** — the expected output for the `baseline_20yr_phase3_2016` scenario. Generated by hand from a representative event log fixture; updated when format changes are deliberate.

2. **`fixtures/current_status_phase2_cb1.txt`** — the expected current_status.txt for a Phase 2 install with CB1 active, one recent withdrawal, and a couple of recent alerts.

The fixtures are short enough to review by eye. When a format change requires a fixture update, the diff between old and new fixture is reviewable in the PR.

### 9.6 Test event log construction

Tests construct event logs as JSONL strings:

```python
def make_event(event_type: str, payload: dict, timestamp: str,
               source_cycle_id: str = "test-cycle") -> str:
    """Test helper: construct a complete event envelope."""
    return json.dumps({
        "schema_version": "1.0",
        "event_id": f"evt_{uuid4().hex}",
        "event_type": event_type,
        "timestamp": timestamp,
        "emitted_at": timestamp,
        "source_cycle_id": source_cycle_id,
        "payload": payload,
    })


def test_write_current_status_phase2_cb1(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    events_file = state_dir / "events.jsonl"

    events = [
        make_event("cycle_completed", {...}, "2027-04-21T10:30:00+00:00"),
        make_event("portfolio_snapshot", {...}, "2027-04-21T10:30:01+00:00"),
        # ...
    ]
    events_file.write_text("\n".join(events) + "\n")

    output_path = tmp_path / "current_status.txt"
    write_current_status(state_dir, output_path)

    assert output_path.read_text() == expected_text
```

Helper functions reduce boilerplate. The tests aim to be readable: the test's intent should be clear from the events it constructs and the assertions it makes.

---

## 10. Open questions and decisions deferred

This section catalogs decisions intentionally not made by this specification and questions that may need answers as implementation proceeds.

### 10.1 Dividend handling: DRIP vs Sweep

Status: under operator consideration as of 2026-05-15.

The reporter's `cash_in` column behavior depends on a portfolio-management decision the operator is currently weighing: whether dividends from FI positions (PYLD, JPIE) and SGOV reinvest at the brokerage (DRIP) or deposit as cash for IRAPM to sweep into the buffer.

**Under DRIP:** cash_in is near-zero for most rows (only initial deposit and rare external contributions). The column is dead weight.

**Under Sweep:** cash_in is an active line item showing portfolio yield, ~$2-2.5k/month on a $1.8M Phase 2 portfolio. The reporter has a real metric to display.

**Reporter implication:** if Sweep is chosen, the event log needs a way to distinguish dividend fills from rebalance sells. Currently a `fill_received` with side=SELL covers both. Likely path: add a `fill_source` field to `fill_received` (`"dividend"` / `"rebalance"` / `"withdrawal_funding"` / `"buffer_refill"`). This is a small additive change within EVENT_LOG_SPEC schema 1.x compatibility rules.

**When to revisit:** before Phase 2 implementation work begins on the reporter. The operator's decision determines whether the event log needs the `fill_source` enhancement.

### 10.2 ACH schedule events

Inherited from EVENT_LOG_SPEC §8.5. No dedicated event type for recurring ACH schedule changes exists; the information is partly captured by `annual_review_completed.issued_ach_update`.

**Reporter implication:** the current_status.txt header line "Next scheduled withdrawal" is computed by the reporter from calendar logic (the 15th of next month). If the actual ACH schedule diverges from this convention (operator-initiated adjustment, broker-side change), the reporter has no event to read and shows the convention. A future `ach_schedule_updated` event would let the reporter show the actual scheduled date.

**When to revisit:** if operator-initiated ACH adjustments become operationally important.

### 10.3 GAP-PARTIAL annotation

Per §8.3, the reporter does not currently distinguish between a row period with full coverage and one with partial coverage. If the operator finds it useful to flag partial-coverage periods (e.g., production was down 3 of 4 weeks in a month), a `GAP-PARTIAL` annotation could be added.

Implementation would require: knowing how many weekly cycles "should have" occurred in the period (52 per year for production, simulator-defined for sims), and counting actual cycles present.

**When to revisit:** if a real-world incident produces a partial-coverage period the operator wants flagged.

### 10.4 Operational pause event type

Inherited from EVENT_LOG_SPEC §8.6. The `state_snapshot` event carries `operational_pause.paused`, and `alert_emitted` fires for pause activation. Whether to add dedicated `operational_pause_started` / `operational_pause_cleared` events is open.

**Reporter implication:** the `PAUSE` annotation currently derives from `alert_emitted` with `alert_id == "operational_pause"`. If a dedicated event type lands, the source mapping in §5.2 updates trivially.

**When to revisit:** if operational pauses become frequent enough that the indirection through alerter events becomes a forensic obstacle.

### 10.5 Configurable retention

The 8-year retention for `{YYYY}.txt` files is hard-coded in §3.2.3. If an operator wants a different retention period (4 years, 12 years), the `retain_years` parameter of `prune_old_year_files` already supports it — the current question is just whether the cycle.py caller hard-codes 8 or reads from a config.

**Reporter implication:** none for the reporter module itself. The decision lives in cycle.py's invocation contract.

**When to revisit:** if retention requirements change.

### 10.6 Local-time display in operator reports

The reporter renders all timestamps in UTC (§8.10). The operator may eventually want local-time display in `current_status.txt` (the routine-monitoring file) for ease of reading "when did the last cycle run?" against wall-clock expectations.

**Trade-off:** local-time display requires timezone configuration somewhere, and conversion logic that's a bug surface. UTC everywhere keeps the reporter simple.

**When to revisit:** if reading UTC times becomes a routine operator friction.

### 10.7 Headline metrics duplication in simulator report

The simulator report header block (§3.3.2) shows headline metrics (starting/terminal AUM, total withdrawn) that also appear in the TOTALS section (§3.3.3). This is intentional duplication: the header serves at-a-glance reading, TOTALS serves structured detail.

If the duplication is later found to be more noise than value, the header could collapse to just scenario name and window, with all metrics living only in TOTALS.

**When to revisit:** after the first round of operator experience with the simulator report format.

---
