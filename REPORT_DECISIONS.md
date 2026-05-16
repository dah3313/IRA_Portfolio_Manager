# IRAPM Report Format — Decisions Record

**Status:** Decisions captured. Ready for implementation.
**Last updated:** 2026-05-14
**Related documents:** `EVENT_LOG_SPEC.md` (the data substrate this reporter consumes)

---

## Purpose of this document

This is not a full specification — it's a concise record of the design decisions made during a long planning session, captured so they survive outside conversation context. The reporter's behavior is also documented inline in the eventual `report.py` module's docstring. This file is the design rationale; the code is the contract.

## Architectural context

The reporter is a pure consumer of `events.jsonl` (the IRAPM event log, specified in EVENT_LOG_SPEC.md). It reads no in-memory state, depends on no other IRAPM modules' internals, and has no side effects beyond writing its output files.

Per the architectural decisions made in the planning session:

- **IRAPM is system of record.** The event log is the authoritative source of truth.
- **The reporter is the operator's primary interface.** Routine monitoring happens via the text reports it produces; the raw JSON event log is for occasional forensic deep-dives, typically performed off the production box.
- **Production and simulation use the same reporter.** Both modes read the same event log format; they differ only in which output files they produce and when.

---

## File types and lifecycles

The reporter produces three kinds of files, distinguished by purpose and lifecycle.

### `current_status.txt`

**Purpose:** Operator's "what is IRAPM doing right now" file. SSH in, `cat current_status.txt`, see the current state of the system plus recent activity.

**Content:**
- Header block — phase, CB state, income state, operational pause status, current monthly withdrawal amount, last withdrawal date and amount, next scheduled withdrawal date
- "Recent activity (last 4 weeks)" — four weekly data rows plus their annotation lines (CB transitions, withdrawals, refills, etc.) that occurred in that window
- "Recent alerts (last 7 days)" — chronological list of alerts fired in the past week, with timestamp and key context

**Lifecycle:** Overwritten on every weekly cycle. The file is always the latest snapshot. No history retained in this file — history lives in the year files and the event log.

**Location:** `c:/portfolio/status/current_status.txt` (production) — or operator-configurable. The reporter does not invent the directory; the caller provides the output path.

### `{YYYY}.txt`

**Purpose:** Per-year historical record. One file per calendar year of operation.

**Content:**
- Monthly data rows accumulating through the year, one per calendar month
- Annotations under each row for events that occurred in that month (CB transitions, withdrawals, refills, phase transitions, annual review, etc.)
- Annual summary appended at year-end when the file closes

**Lifecycle:**
- The file for year Y is created on the first monthly append (which happens on the first weekly cycle of February Y, when the January-Y row gets written)
- Each monthly row is appended on the first weekly cycle of the following month (so January's row lands in early February)
- December's row is appended on the first weekly cycle of the next January, followed immediately by the annual summary
- After the annual summary appends, the file is closed (no further writes to that year's file)

**The "row lands in the next month" convention** is deliberate: it allows each monthly row to reflect complete monthly aggregates (e.g., the full month's cash_out total, not partial-through-last-Wednesday-of-month). The trade-off is that during the first ~7 days of any month, the current month's row is not yet visible in the year file. The `current_status.txt` file's "recent activity" section covers that gap.

**Annual summary content:** AUM start vs end with absolute dollar and percentage change; total withdrawn for the year (with capped-vs-uncapped count); per-asset percentage movement (both price return and allocation drift); CB activity (CB1/CB2 trigger counts, days at CB1+, recovery confirmation date); phase transitions if any; annual review record (date, CPI applied, freeze status).

**Retention:** 8 years rolling. `{YYYY}.txt` files older than 8 years are deleted automatically at the same time the new year's first file is created.

**Location:** `c:/portfolio/status/{YYYY}.txt`.

### `report_{YYYYMMDD_HHMMSS}_{scenario}.txt`

**Purpose:** Simulator output. One file per scenario run, covering the full run duration.

**Content:**
- Header block (scenario name, run timestamp, window, headline metrics)
- `=== RULESET ===` section displaying the tuning-relevant ruleset values used for this run (see below)
- `=== ANNUAL ===` section with one row per year of the run, annotations show all events from the prior 12 months
- `=== MONTHLY ===` section with one row per month, annotations show events that occurred in that month
- `=== LEGEND ===` footer explaining the column meanings and annotation codes

**Lifecycle:** Written once, at end of simulation run. Never modified. Filename includes a timestamp so consecutive runs of the same scenario do not overwrite each other.

**Ruleset section** (simulation report only — production `current_status.txt` and `{YYYY}.txt` do NOT include it):

The simulator is used for offline parameter tuning. When the operator changes ruleset values to compare scenarios, the report must show which values produced this run's result so two reports can be diffed unambiguously. The production reports do not include this section — production reads from a single live ruleset that doesn't change between cycles, so embedding it in every weekly status file would be noise.

The section shows tuning-relevant fields only, not the full ruleset. The full ruleset.yaml file is copied verbatim into the run's output directory (`runs/{...}/ruleset_used.yaml`) for diff-tooling; the on-report display is a curated tuning-readable summary.

Tuning-relevant fields (the on-report display):

- **Withdrawal mechanics:** `phase1_initial_monthly_withdrawal_dollars`, `phase3_monthly_payment_ceiling_rate`, `phase3_dollar_ceiling_base_dollars`, `phase3_dollar_ceiling_base_year`, `inflation_rate`
- **Phase 3 I_0 calc:** `phase3_i0_calc_return_assumption`, `phase3_i0_calc_inflation_assumption`, `phase3_i0_calc_horizon_years`
- **CB thresholds:** the cb-trigger lookback thresholds (e.g., `cb1_signal_threshold`, `cb2_signal_threshold`), `freeze_evaluation_threshold_days`
- **Buffer mechanics:** `sgov_buffer_target_months`, `cash_buffer_offset_dollars`

The implementing function (`report.py::_format_ruleset_section`) selects from the in-memory Ruleset object — not from re-parsing ruleset.yaml — since the operator may have applied scenario-specific overrides. The Ruleset Pydantic model is the authoritative source for "values that this run actually used."

The full-file copy (`ruleset_used.yaml`) happens at run-start, written by ipms.engine, capturing the post-override merged form. This is independent of report.py and lives in the simulator wrapper, not the reporter.

**Annual section event attribution:** Events listed in the annual row for year Y are events that fired during the 12 months ending at that snapshot (i.e., during year Y-1 for the row dated YYYY-01-01). This convention is operator-confirmed.

**Retention:** Not managed by the reporter. The simulator's per-run output directory keeps these files indefinitely; the operator decides what to keep.

**Location:** Inside the per-run output directory, alongside the harness state.

---

## Column set

Identical across all three file types and both sections of the simulator output:

| Column | Type | Description |
|---|---|---|
| `date` | date | Snapshot date for the row |
| `cb` | string | CB state at row time: `-`, `1`, `2`, `1+2` |
| `gross_net_liq` | dollar | Total AUM (positions + cash) |
| `cash_buff` | dollar | Cash buffer balance (settled cash) |
| `sgov_buffer` | dollar | SGOV position market value |
| `fi_bucket` | dollar | Sum of fixed-income holdings |
| `growth_bucket` | dollar | Sum of growth holdings |
| `fi_wt` | weight | FI bucket value / AUM |
| `gr_wt` | weight | Growth bucket value / AUM |
| `cash_in` | dollar | Sum of cash inflows during the row's period |
| `cash_out` | dollar | Sum of cash outflows during the row's period |
| `pyld` | dollar | PYLD position market value |
| `jpie` | dollar | JPIE position market value |
| `fbcg` | dollar | FBCG position market value |
| `avuv` | dollar | AVUV position market value |
| `gbil` | dollar | GBIL position market value |
| `phase` | integer | Operating phase: 1, 2, or 3 |

**Naming notes:**
- `cash_buff` (not `cash`) — distinguishes the cash buffer balance from the in/out flow columns. This was a specific operator preference.
- `gross_net_liq` — total AUM, matches the IPMS terminology the operator is already familiar with.
- `gr_wt` (not `growth_wt`) — kept short for alignment.

---

## Annotation lines

When events occur during a row's period, they are listed on a separate line directly below the data row, indented under the date column:

```
2008-01-31  1   845,210.00   4,330.00 ...
            └─ CB1:2008-01-09, AR:2008-01-02, W:2008-01-15
```

**Format:**
- Indentation is the width of the `date` column + 2 spaces (matching the column separator width)
- Box-drawing prefix `└─ ` (UTF-8, acceptable risk for SSH terminals — all modern Linux setups handle UTF-8)
- Events in chronological order within the period
- Each event annotation is `EVENT_CODE:YYYY-MM-DD`
- Multiple events separated by `, `

**Annotation event codes:**

| Code | Meaning |
|---|---|
| `AR` | Annual review fired |
| `CB1` | CB1 transition entered |
| `CB2` | CB2 transition entered |
| `REC` | Recovery confirmed (CB cleared back to inactive) |
| `W` | Scheduled withdrawal executed |
| `RFL` | SGOV buffer refill executed |
| `P2` | Phase 2 transition |
| `P3` | Phase 3 latch |
| `DPD` | Dry powder deployed |
| `DPR` | Dry powder refilled |
| `HALT` | Cycle halted |
| `PAUSE` | Operational pause activated |
| `CAP` | Withdrawal capped at binding ceiling |

If a row's period contained no annotation-worthy events, no annotation line is emitted for that row.

**Annual section flag attribution:** For annual rows, events listed are events that fired during the 12 months ending at that snapshot. For example, the 2009-01-01 row may show `CB1:2008-01-09, CB2:2008-04-09` because those transitions happened during calendar year 2008.

---

## Format conventions

- Plain text, UTF-8, LF line endings (not CRLF)
- Fixed-width columns
- Per-section column widths computed mechanically: each column's width is `max(len(header), max(len(cell) for cell in that column's data))`. This guarantees header-to-data alignment is exact regardless of run contents.
- Numbers right-aligned; date/cb/phase columns left-aligned
- Dollar amounts: comma-separated thousands, 2 decimal places (e.g., `1,234,567.89`)
- Weights: 2 decimal places (e.g., `0.40`)
- No currency symbol — every dollar column is dollars, suffixes would just be visual clutter
- Section headers: `=== SECTION_NAME ===` on its own line, blank line before and after
- Column separator: 2 spaces (allows the separator to remain visually distinct under various widths)

---

## Reporter module API

`report.py` lives at the repo root, peer to `cycle.py`, `event_log.py`, and `irapm_driver.py`.

Public functions:

```python
def write_current_status(
    state_dir: Path,
    output_path: Path,
) -> Path:
    """Generate current_status.txt content from the event log at
    state_dir/events.jsonl and write to output_path (overwriting).
    Returns output_path."""

def append_monthly_row_to_year_file(
    state_dir: Path,
    output_dir: Path,
    month: date,
) -> Path:
    """Append the row for the given month to {output_dir}/{month.year}.txt.
    Creates the file if it does not exist. Returns the file path."""

def close_year_file(
    state_dir: Path,
    output_dir: Path,
    year: int,
) -> Path:
    """Append the year-end summary to {output_dir}/{year}.txt and
    consider the file closed. Returns the file path."""

def prune_old_year_files(
    output_dir: Path,
    retain_years: int = 8,
) -> list[Path]:
    """Delete {YYYY}.txt files older than retain_years from output_dir.
    Returns the list of deleted file paths."""

def write_simulation_report(
    state_dir: Path,
    output_path: Path,
) -> Path:
    """Generate a full simulation report from the event log and write
    to output_path. Returns output_path."""
```

All functions are pure consumers of the event log. They open `state_dir/events.jsonl`, read events sequentially, build the output, and write. They never read in-memory state, never call into other IRAPM modules, never depend on the broker or harness.

---

## When the reporter runs

In production, four invocations per weekly cycle:

1. **Always**: `write_current_status()` — overwrites the current status file
2. **First weekly cycle of each month**: `append_monthly_row_to_year_file()` — adds the prior month's row to the current year's file
3. **First weekly cycle of January**: `close_year_file()` for the prior year (appends annual summary) — runs immediately after the December row is appended in step 2
4. **First weekly cycle of January** (after closing the prior year): `prune_old_year_files()` — deletes files older than 8 years

In simulation, one invocation at end of run: `write_simulation_report()`.

The reporter is invoked from the cycle driver, not the event log writer. The event log writer emits events as IRAPM runs; the reporter consumes events when explicitly invoked. This separation keeps the writer focused and the reporter independently testable.

---

## Implementation notes

**Column width computation.** The reporter computes column widths per section. Each section (annual table, monthly table, etc.) gets its own width computation. A wider value in the annual table does not force the monthly table to use wider columns.

**Data aggregation.** Each row in the output represents a period (a week, a month, a year). The data values (gross_net_liq, balances) come from the `portfolio_snapshot` event closest to the row's date — typically the snapshot from the row-period's final weekly cycle. The flow values (cash_in, cash_out) are summed over `fill_received` events within the period. Annotation events come from filtering the event log by event_type within the period.

**Performance.** Reading the full event log per invocation is fine at our scales (~15 MB max over 30 years). No incremental processing, no caching, no index files. Linear scans every time. If profiling later shows this matters, an index can be added; pre-optimizing it now is unnecessary.

**Error handling.** The reporter is a read-only consumer; the most aggressive thing it does is overwrite `current_status.txt`. If event log reading fails partway (parse error, truncated file), the reporter logs a warning and produces whatever output it can from the events it successfully read. Producing partial output is preferable to producing nothing.

**Testing.** Each public function has focused tests against synthetic event logs (fabricated by tests, not produced by running IRAPM). The test event logs are deterministic JSONL strings — the reporter is tested in isolation from the rest of IRAPM.

---

## What this document does NOT specify

- The exact pixel-level formatting of the annual summary block — that's a design judgment captured in the reporter's docstring with a sample, not pinned down here
- Implementation details (parsing strategies, data class shapes, etc.) — those live in the code
- Reader/error handling specifics — covered by the reporter's docstring and tests
- Replication/file-rotation race conditions — production rsync will copy whatever files exist at copy time; the reporter does not coordinate with rsync
- Tooling for analyzing the JSON event log directly — that's a separate future concern

---

## Open questions deferred

**1. Time-of-day on `current_status.txt`.** The header includes "Generated: 2027-04-21 10:32:18 UTC". Useful to confirm freshness. But the file is overwritten weekly, so the timestamp is also implicit from file mtime. Keep both? (Likely yes — explicit beats implicit.)

**2. Sort order of recent alerts.** Chronological ascending or descending? Operator-glance-friendly is probably "most recent first" so the latest is visible without scrolling. Likely descending. Confirm during implementation.

**3. Headers within the recent-activity table.** Should `current_status.txt`'s recent-activity section repeat the column headers, or omit them since they're identical to the year file? Likely repeat — file is meant to be read in isolation.

These are minor enough to decide during implementation; they don't warrant a separate decision pass now.
