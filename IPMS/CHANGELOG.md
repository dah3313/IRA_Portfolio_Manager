# CHANGELOG

All notable changes to the IPMS project are recorded here. One file
per project, no in-module changelogs (per spec §8.2).

Format conventions: Keep-a-Changelog. Entries are dated. Each version
section has subsections for Added / Changed / Fixed / Removed /
Deprecated as applicable. Major behavior changes warrant an entry.
Cosmetic refactors do not.

---

## [Unreleased]

### Fixed (post-checkpoint-6 triage sweep, 2026-05-09)

- 2026-05-09: Tier A bug fixes from `docs/IPMS_REVIEW_2026-05-09.md`.
  - **Engine E1 (month-end snapshots)**: `engine.py::_is_output_tick`
    monthly cadence now fires on the LAST day of the calendar month
    (today + 1 day in different month) rather than the first day of
    the new month. Per spec §5.4 "one row per month-end". Affects
    every row of every monthly-cadence run; previous output had
    month-start dates (e.g., 2005-05-01 instead of 2005-04-30).
  - **Output O1 (annual filter)**: `output/balances.py::_filter_annual_snapshots`
    now picks the earliest JANUARY (month==1) snapshot per year and
    omits years with no January snapshot from the annual file. Per
    spec §5.4 "sampled on January 1". Previous behavior picked the
    earliest snapshot of each year unconditionally, which for runs
    starting mid-year used the seed snapshot (e.g., 2005-04-25) as
    the year-1 row. Result: balances_annual.md may now have one
    fewer row for mid-year-start runs (year 1 dropped); rows that
    do appear are all comparable at the same calendar position.
  - **Engine E3 (SGOV pre-launch phantom price)**:
    `engine.py::_seed_initial_portfolio` and
    `_update_portfolio_prices` now track an `sgov_pre_launch` flag
    in the portfolio dict. While pre-launch, the buffer holds a
    flat synthetic $100 price and frozen quantity. On the first
    tick where real SGOV price data is available
    (`today >= market.first_date(BUFFER_SYMBOL)`), share count is
    REBASED so dollar value is preserved across the boundary — no
    phantom CashFlow generated. Previously, pre-launch synthetic
    price persisted in the portfolio dict but real prices started
    flowing through `_update_portfolio_prices`, producing a value
    jump (e.g., 720 × ($100 → $100.05) = +$36 without offsetting
    cash flow).
  - **Engine E4 (initial-allocation rounding residual)**:
    `engine.py::_seed_initial_portfolio` now accumulates the
    ROUND_DOWN residuals from each asset purchase plus the SGOV
    buffer, and credits them to `cash_value` so
    `gross_net_liq == starting_balance_usd` exactly on the first
    snapshot. Previously the residuals (typically sub-cent total)
    silently disappeared, leaving snapshot 0's gross_net_liq a few
    pennies short.
  - **Engine E5 (incomplete seed CashFlow)**:
    `engine.py::run` seed CashFlow `amount` is now
    `params.starting_balance_usd` (the full ~$668k initial wealth)
    rather than just the cash-buffer slice (~$4k). The seed
    represents all wealth flowing in from outside the simulator
    (the operator's bank-to-IBKR transfer); attributing only the
    cash slice left $664k+ of asset purchases and SGOV buffer seed
    unaccounted in the audit trail. The cash-flow attribution
    discrepancy metric (`result.py::max_cash_flow_attribution_discrepancy`)
    is unaffected because it walks consecutive snapshot pairs
    starting at index 1; the seed flow at index 0 is correctly
    excluded from inter-snapshot period accounting.

### Added (post-checkpoint-6 triage sweep, 2026-05-09)

- 2026-05-09: Tier B test gaps closed from
  `docs/IPMS_REVIEW_2026-05-09.md`.
  - `tests/test_params.py` (NEW) — exercises every branch of
    `SimulationParams.validate()` per spec §2.3 plus the YAML
    loader. Coverage: run window endpoints (end before/equal to
    start), starting balance positivity, withdrawal positivity
    and ≤ starting-balance, CPI YEARLY_OVERRIDE list presence and
    length, guardrail MODIFIED override presence and positivity,
    output_dir.parent existence, output_points within window,
    initial_*_usd non-negativity, logging_verbosity recognized
    levels (with case-insensitivity). YAML loader: minimal-load
    round-trip, missing-file error, unknown-key typo detection,
    validation-error path attribution, non-mapping rejection,
    quoted-vs-unquoted ISO date parity, bad-format date rejection.
    Plus convenience-property checks for `years_in_window` and
    `phase_2_anniversary` (default and override).
  - `tests/test_synthetic_events.py::TestResultAccessors` extended
    with B2 headline-metric tests: `terminal_aum` (last snapshot
    selection, None on empty), `total_withdrawn` (sum across
    successful and partial; halted contribute zero),
    `max_drawdown_pct` (worst peak-to-trough, zero for monotonic-up
    series, None on empty), `min_sgov_buffer`, `min_fi_bucket`,
    `terminal_cpi_rate` (12 × latest review post_target /
    terminal_aum, None when no reviews or no snapshots).
  - `tests/test_engine.py` (NEW) — engine-level end-to-end tests.
    `TestMonthEndSnapshots` asserts cadence-driven snapshots fall
    on the last day of their calendar month (B3 closure; would
    have caught E1) and asserts no months are skipped within a
    full-year window. `TestOutputPointsMode` asserts explicit-date
    mode produces snapshots only on listed dates, suppresses the
    trailing end_date snapshot, and takes precedence over
    `output_cadence`. `TestAnnualOnlyMode` asserts
    `time_series_detail=ANNUAL_ONLY` suppresses
    `balances_monthly.md` while keeping `balances_annual.md`.

### Provisional/known-issues remaining after this sweep

- Tier C items per `IPMS_REVIEW_2026-05-09.md` deferred per their
  rationale: skipped regression tests pending IRA PM (C1), spec
  §7.3 external cross-checks (C2), `summary.md` spec ambiguity (C3),
  README.md staleness (C4).
- README.md still says "checkpoint 1" — trivial fix not done in this
  sweep.
- `recovery_events.md` is produced but not in spec §5.2 file list —
  spec edit pending.

### Pending operator sign-off
- Run `pytest tests/` from `C:\portfolio\IPMS` to confirm:
  - All previously-passing tests still pass (35 active).
  - 7 regression tests still correctly skip.
  - New tests pass (`test_params.py` ~30 tests; `test_engine.py`
    ~8 tests; `test_synthetic_events.py::TestResultAccessors`
    +11 tests).
- Spot-check a canonical 20-year sim output:
  - `balances_monthly.md` first date column should show month-end
    dates (e.g., `2005-04-30`, `2005-05-31`, ...), not month-start.
  - `balances_monthly.md` first row's `gross_net_liq` should equal
    `starting_balance_usd` exactly (e.g., `668500.00`), not a few
    cents lower.
  - `balances_annual.md` row count: for runs starting mid-year, one
    fewer row than before (year 1 dropped).

### Added

- 2026-05-09: Project began. Checkpoint 1 (types and parameters)
  complete.
  - `events.py` — typed event taxonomy: Snapshot, CashFlow,
    WithdrawalRecord, CBEvent, RecoveryEvent, RebalanceRecord,
    CascadeTransition, AnnualReview, Alert, PhaseTransition,
    GrowthIndexUpdate. Plus discriminator enums (EventType,
    CashFlowSource, CascadeTier, CBId).
  - `params.py` — SimulationParams dataclass with all axes from
    spec §2.1 and §2.2. Validation per §2.3 raises
    ParameterValidationError. YAML loader per §2.4.
  - `result.py` — SimulationResult with full event log, parameter
    record, output-file paths, run metadata, and convenience
    accessors per §4.5 (terminal_aum, total_withdrawn,
    cb1/cb2_trigger_count, cascade_tier3_occurrences,
    cascade_exhausted_halts, max_drawdown_pct, min_sgov_buffer,
    min_fi_bucket, max_cash_flow_attribution_discrepancy,
    terminal_cpi_rate). Cached-property implementation so
    repeated access doesn't re-walk the event log.
  - `proxy_map.py` — PROXY_MAP carried over from IPMV1. Provenance
    narrative captured in module docstring and PROXY_PROVENANCE
    dict.
  - `__init__.py` — top-level convenience exports.

- 2026-05-09: Checkpoint 2 (vertical slice) complete.
  - `market.py` — MarketSimulator port from IPMV1's simulate.py.
    Loads TSV/CSV files from C:\portfolio\data, handles Yahoo's
    quirks (descending date order, distribution rows, AdjClse
    typo, %b %d, %Y date format alongside ISO). Decimal prices
    rather than IPMV1's float for consistency with the IRA PM's
    money discipline. price_on() with O(log n) binary search
    handles weekends/holidays/missing rows by walking back to the
    most-recent available bar. Buffer asset (SGOV) gets relaxed
    pre-launch coverage handling.
  - `clock.py` — Clock Protocol with AdvancingClock (driven by
    engine main loop) and SystemClock (interface completeness).
    Will be superseded by the IRA PM's eventual clock module
    when that exists; the small interface is forward-compatible.
  - `collector.py` — StateCollector. Append-only event list. No
    sorting, filtering, or summarization — engine emits in
    chronological order, formatters project later.
  - `engine.py` — `run(params) -> SimulationResult` seam.
    Initialization → seeding → daily loop → finalization. Truly
    minimal per spec §10.3 RESOLVED option (a): no IRA PM logic
    yet. Daily loop advances clock and emits snapshots on
    output-cadence ticks; portfolio is frozen (no withdrawals,
    rebalances, or CB events). Comments mark IRA PM integration
    points without declaring protocol shapes.
  - `output/terminal.py` — print_terminal_summary per spec §5.8.
    Highlights cascade_exhausted_halts (family-safety metric)
    and cash-flow attribution discrepancy (the bug-detection
    metric that would have caught the IPMV1 SGOV refill defect).
  - `output/__init__.py` — output sub-package.
  - `__main__.py` — CLI entry point. `python3 -m ipms params.yaml`.

### Pending checkpoint 2 sign-off
- Operator review of vertical slice end-to-end run.
- Smoke test on canonical 20-year window from C:\portfolio\data.
- Determinism check (two runs same params → byte-identical events).

### Added (checkpoint 3)

- 2026-05-09: Checkpoint 3 (file output — balances and run directory)
  complete.
  - `output/formatting.py` — sub-cent suppression utilities per
    spec §5.7. Dollars to 2dp, shares/prices/weights to 4dp,
    booleans as lowercase, dates as ISO. Optional-value helper for
    nullable columns.
  - `output/markdown_tables.py` — Markdown-with-tab-separated-tables
    writer per spec §5.3. Header / separator / data rows with the
    operator-confirmed `| cell\t| cell |` format.
  - `output/balances.py` — balances_monthly.md and
    balances_annual.md formatters per spec §5.4. All columns from
    the operator-confirmed schema: cb, flags, gross/managed_net_liq,
    cash, cash_flow_in/out, sgov_buffer, FI/Growth buckets,
    weights, yoy_change_pct, per-asset values, phase, refill_active.
    YoY computation: monthly file uses same-calendar-month-1yr-prior
    lookup; annual file compares against prior January row.
    Cash-flow aggregation: per-row sums of CashFlow events in the
    period (prior_snap, this_snap]. Flags column derives from
    Recovery/Rebalance/AnnualReview/PhaseTransition events in same
    period. CB column combines CB1+CB2 active state into single
    indicator. Per-asset columns in canonical PYLD/JPIE/FBCG/AVUV/
    GBIL order; Phase 2 zeros render as 0.00 for schema stability.
    File header includes run window + generation date; footer
    includes column reading guide for self-documentation.
  - `output/run_directory.py` — directory creation, README.md and
    parameters.md writers per spec §5.2 and §5.9. auto_run_name
    generates deterministic name from parameter hash so equivalent
    runs collide cleanly while distinct runs do not. README
    includes file inventory, headline metrics, full proxy
    provenance for archival. parameters.md is human-readable
    Markdown sections grouped by concern (window / starting state /
    CPI / guardrail / phase 2 / overrides / output).
  - `engine.py` updated — finalization stage now creates run
    directory, writes README + parameters, writes balances_monthly
    when time_series_detail=MONTHLY, always writes balances_annual.
    Imports kept inside the function so programmatic-only callers
    don't trigger output module loading.
  - `output/__init__.py` updated to export the new public names.

### Pending checkpoint 3 sign-off
- Operator review of actual file output in VS Code preview.
- Confirmation that Markdown-with-TSV format renders as expected
  with the actual 20-year canonical window data.

### Added (checkpoint 4)

- 2026-05-09: Checkpoint 4 (cascade tracking, cascade_log formatter,
  output_points mode) complete.
  - `constants.py` — IRA PM domain constants the simulator needs at
    formatter time: PER_POSITION_RESIDUAL_USD ($1,500), cash buffer
    targets per phase, SGOV buffer target ($72,000) and tolerance.
    These values mirror the IPMV1 ruleset 1.6.1 and the operator-
    confirmed audit clarifications (§3.4.10.1: residuals are
    dollar-only, never percentage-or-bucket-level). When the IRA PM
    lands, this file's role shifts from hardcoded constants to
    importing from the IRA PM's ruleset loader; the constant names
    stay stable.
  - `output/cascade.py` — cascade_log.md formatter per spec §5.5.
    Two sections:
      Section 1 (per-tick depth): wide table with date, SGOV
        balance/target/pct/at_residual, then per-asset triple of
        balance/distance_to_residual/at_residual for PYLD, JPIE,
        FBCG, AVUV, GBIL in canonical order, then current cascade
        tier and count of at-residual positions. One row per
        snapshot.
      Section 2 (transition events): sparse table with date,
        transition_type (e.g., "buffer→FI" or "FI→buffer (recovery)"),
        trigger_event, position_balances_at_transition (compact
        semicolon-separated cell), expected_next_tier_source hint,
        days_since_cb2_trigger. One row per CascadeTransition event.
    Distance-to-residual and at_residual flags are derived at
    formatter time from each AssetBalance.market_value vs
    PER_POSITION_RESIDUAL_USD; not stored on Snapshot (lossless
    collector posture per spec §4.4).
  - `engine.py` updated to call write_cascade_log unconditionally
    in finalization. Cascade file is short for clean Phase 1 runs
    (Section 2 empty), substantial for stress runs.
  - `engine.py` _is_output_tick updated to honor params.output_points
    when present — the explicit-date-list mode per spec §2.1.
    output_points takes precedence over output_cadence; the two are
    mutually exclusive per spec. Final-snapshot-at-end_date logic
    skipped in output_points mode so the operator gets exactly the
    requested dates with no extras.
  - `output/__init__.py` updated to export write_cascade_log.
  - `output/run_directory.py` README inventory updated to list
    cascade_log.md.

### Posture for v0.1.0 cascade output

The IPMS v0.1.0 has no IRA PM, so no cascade activity actually
happens during runs. The frozen portfolio does not draw down assets
toward residual. cascade_log.md for clean Phase 1 runs contains:
  - Section 1: 240+ rows where every position is far above residual,
    every at_residual flag is false, cascade tier is always 2.
  - Section 2: empty.
This is correct. Schema is locked in now so when IRA PM activity
lands, the formatter just works.

### Pending checkpoint 4 sign-off
- ✅ 2026-05-09: Checkpoint 4 signed off by operator. Cascade tracking,
  cascade_log formatter, and output_points mode confirmed.

### Added (checkpoint 5)

- 2026-05-09: Checkpoint 5 (event-stream file formatters) complete.
  - `output/withdrawals.py` — withdrawals.md formatter per spec §5.6.
    Per-withdrawal table: scheduled_date, execution_date,
    target_amount, filled_amount, source_asset, source_tier (1/2/3
    integer), tier_name (buffer/FI/Growth), halt_reason,
    trueup_balance_after, cumulative_withdrawn. Empty-state rendering
    notes "No withdrawals during this run" when the result has none
    (always true for v0.1.0 frozen-portfolio runs).
  - `output/rebalances.py` — rebalances.md formatter per spec §5.6.
    Two-level rendering: one block per RebalanceRecord with trigger
    metadata, then a per-trade table (symbol, action, shares, price,
    dollar_amount, outcome). SGOV refill records correctly render
    BOTH the SELL leg and the BUY leg as separate rows (the IPMV1
    SGOV-bug regression-prevention mechanism at the file-format
    level — if a future IRA PM regression were to drop the BUY leg,
    it would be visible in this file as a missing row).
  - `output/cb_events.py` — cb_events.md formatter per spec §5.6.
    One row per CBEvent: date, cb_id, from_state→to_state arrow,
    rolling_return at transition, trigger_portfolio_value (only at
    activated transition; empty for pending/recovered),
    confirmation_week_count.
  - `output/recovery_events.py` — recovery_events.md formatter per
    spec §5.6. One row per RecoveryEvent: date, cb_id, stage (1/2),
    confirmed (true/false), rolling_return.
  - `output/annual_reviews.py` — annual_reviews.md formatter per
    spec §5.6. One row per AnnualReview: date, outcome (seeded /
    cpi_raise / guardrail_cut / guardrail_bypassed), pre_target,
    post_target, effective_rate, reference_rate, guardrail bands,
    cpi_rate_applied.
  - `output/phase_transition.py` — phase_transition.md formatter per
    spec §5.6. Single record (or empty for runs that don't reach
    anniversary): scheduled_date, executed_date, deferred_by_cb,
    forced_after_max_delay, pre_allocation and post_allocation
    rendered as compact `SYM=W%; SYM=W%` cells.
  - `output/alerts.py` — alerts.md formatter per spec §5.6. One row
    per Alert: date, event_name, severity, channels (comma-separated),
    ctx_snapshot keys/values rendered compactly.
  - `engine.py` finalization extended: after cascade_log, calls
    write_withdrawals → write_rebalances → write_cb_events →
    write_recovery_events → write_annual_reviews → write_phase_transition
    → write_alerts unconditionally. README written last so its
    file inventory reflects all 11 produced files.
  - `output/__init__.py` updated to re-export all 7 new public names.
  - `output/run_directory.py` README inventory extended with rows
    for the 7 new files. Conditional inclusion based on
    result.output_files key presence so future formatters that
    fire only under specific conditions don't appear in the
    inventory of runs where they didn't fire.

### Posture for v0.1.0 event-stream files

For v0.1.0 runs (no IRA PM), all 7 new files contain only header +
column schema + "no events" empty-state note. The schemas are locked
in now; when the IRA PM lands and produces real withdrawals, trades,
CB events, recoveries, annual reviews, phase transitions, and alerts,
each file populates without any further code changes.

### Added (checkpoint 6)

- 2026-05-09: Checkpoint 6 (test scaffold and regression skeleton)
  complete.
  - `tests/__init__.py` — package marker.
  - `tests/conftest.py` — shared fixtures: `data_dir` (defaults to
    `C:/portfolio/data`, env-overridable via IPMS_TEST_DATA_DIR for
    portability across environments), `tmp_output_dir` (per-test
    isolated output dir under pytest's tmp_path), `canonical_params`
    (SimulationParams for the 20-year window), `patched_market`
    (monkey-patches MarketSimulator.__init__ to inject the test
    data_dir for tests that go through ipms.run()).
  - `tests/test_determinism.py` — 5 tests verifying spec §7.2's
    byte-equality property: strict byte-equal across all 10 data
    files, content-equal (modulo wall-clock and output_dir lines)
    for parameters.md and README.md, event-log equality, terminal
    AUM equality, cash-flow attribution discrepancy = 0.
  - `tests/test_market_loader.py` — 11 tests covering canonical
    file load, proxy_info population, window-exceeds-file
    PriceDataError, missing-data-dir error, Decimal price returns,
    weekend lookback, before-first ValueError, beyond-end clamping,
    pre-launch SGOV via a truncated_data_dir fixture (the canonical
    SGOV.tsv has been pre-extended back to 2005 so the fixture
    builds a date-truncated copy in tmp_path to actually exercise
    the relaxed-coverage code path), and extra_proxies kwarg.
  - `tests/test_synthetic_events.py` — 22 tests in 11 classes
    exercising every formatter against hand-crafted SimulationResult
    objects. Coverage includes successful and halted withdrawals,
    SGOV refill SELL+BUY pair rendering, 5/25 rebalance, CB1
    activation, Stage 2 recovery confirmation, CPI raise annual
    review, Phase 2 transition with allocations, CB2 alert with
    ctx_snapshot, cascade transition with position balances, the
    balances flags column for 5/25 and R1, cash-flow attribution
    zero/nonzero detection, and SimulationResult accessor filtering.
  - `tests/test_regression.py` — 7 skipped + 1 active test per
    spec §10.4 RESOLVED option (b). Skipped tests document what
    the regression suite WILL assert once the IRA PM is integrated:
    GFC/COVID/2022 drawdown depths, CB1 trigger count, CB2 trigger
    count, Phase 2 timing window, Phase 1 withdrawal-schedule count.
    Each carries a placeholder constant the operator can update at
    integration time. The single active test (cash-flow attribution
    discrepancy = 0) is not IRA-PM-dependent and runs immediately.

### Test execution

Operator runs the suite from project root:

```
cd C:\portfolio\IPMS
pytest tests\
```

Default `data_dir` resolves to `C:/portfolio/data` (the operator's
canonical path) so no env var is needed. Verified on Claude's Linux
test environment: 35 active tests pass + 7 regression tests correctly
skipped, full suite runs in ~1.3 seconds.

### Pending checkpoint 5 sign-off
- Operator review of the 7 new files in a canonical run output.
- Confirmation that empty-state notes render correctly in VS Code
  preview.

### Pending checkpoint 6 sign-off
- Operator runs `pytest tests\` from `C:\portfolio\IPMS` on Windows.
- Confirmation that all 35 active tests pass and 7 regression tests
  correctly skip.
