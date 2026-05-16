# IRAPM — IRA Portfolio Manager

## Specification

**Version:** 1.10
**Status:** Implementation-ready pending ruleset.yaml drafting, operational
runbook, paper-trading verification of the §15.12 uncertainty-flagged items,
and re-validation per §14.6.

**Version history:** See `CHANGELOG.md`.
**Design rationale:** See `DECISIONS.md`.

This document is the **single source of truth** for IRAPM.

**Owner Preamble**

Each code module and block shall have a succinct comment prior on module/block
function and what the variables refer to. There shall be no changelogs inside
the modules; there will be a single changelog for IRAPM and if a change is
significant enough for a changelog entry it will go there with a brief
statement of the change. Furthermore there shall be no references to
historical or superseded data in the modules. The data and code in the
modules shall be treated as current truth with the only exception being
lookbacks to `IRAPM_SPECIFICATION.md` and `DECISIONS.md` for clarification
while coding. The coding philosophy shall be "Clean, Simple, Elegant,
Robust" as well as "Fail-Safe and Self-Heal". `ruleset.yaml` will contain
all tunable financial values, there shall be no hard-coded financial values
in the code blocks themselves. The only piece of the old IPM that may be
reusable is `clock.py` as it allows graceful regression tests. Each code
module shall be restricted to one functional area i.e. `rebalancer.py`
handles only rebalancing, `circuitbreaker.py` handles only circuit breaker
functions, etc.

---

## Table of Contents

- §1. Goals & Non-Goals (incl. §1.4 Operational principles)
- §2. Foundational Assumptions
- §3. Domain Model
- §4. Phase Model (incl. §4.1.1 Phase 3 starting income calculation — full math)
- §5. Synthetic Growth Lookback Signal
- §6. State Machine
- §7. Decision Logic
- §8. Action Layer
- §9. External Interfaces
- §10. Hardware Tokens (incl. §10.1.1 Physical security model)
- §11. Failure Modes & Recovery
- §12. Observability
- §13. Testing Strategy
- §14. Open Questions (incl. §14.6 Phase 3 re-validation requirement)
- §15. Broker Layer (incl. §15.5 idempotency model, §15.6 master/slave coordination via IBKR-as-arbiter)
- §16. Glossary

---

## §1. Goals & Non-Goals

### 1.1 What IRAPM is

IRAPM is an automated portfolio manager for a single tax-advantaged retirement
account (Traditional IRA or Roth IRA). It implements a **volatility-harvesting
strategy** designed to support withdrawal rates substantially above the
conventional 4–4.7% guideline by:

1. Holding a high-volatility Growth front-end (FBCG, AVUV) that produces the
   price movement the system harvests.
2. Holding a high-yield multi-sector Fixed Income back-end (PYLD, JPIE) whose
   dividends substantially cover ongoing withdrawals, reducing the rate at
   which Growth must be sold.
3. Continuously rebalancing the two buckets toward target weights via a 5/25
   rule, which mechanically transfers Growth volatility into FI accumulation.
4. Defending the portfolio against drawdown via a layered circuit-breaker
   system that progressively reduces exposure to forced selling as conditions
   worsen.
5. Maintaining a 24-month SGOV cash-equivalent buffer outside the portfolio
   accounting boundary, available to absorb extended unfavorable conditions
   without contaminating or being contaminated by the rebalancing signals.

The system operates in phases tied to the operator's life circumstances:

- **Phase 1** (years 0–8): income-production mode with active withdrawals.
- **Phase 2** (years 8+): pure-growth mode after Social Security filing
  establishes a non-portfolio income floor; withdrawals halted; allocation
  shifts to 90/10 Growth/FI with FI as opportunistic dry powder.
- **Phase 3** (event-triggered): survivor-income mode, structurally Phase 1
  reactivated with adaptive withdrawal calculation and a longer horizon.

### 1.2 What IRAPM is NOT

These are deliberate exclusions, not omissions:

- **Not a tax-aware system.** IRAPM operates only in tax-advantaged accounts.
  It does not track tax lots, harvest losses, avoid wash sales, or report
  realized gains. Deployment in a taxable account violates the system's
  economic premise.
- **Not a multi-account system.** It manages exactly one account. Cross-
  account coordination is out of scope.
- **Not a stock-picker.** The asset universe is fixed by configuration. The
  system never selects, evaluates, or rotates between holdings.
- **Not a market-timer.** Circuit breakers are damage-control mechanisms, not
  return-seeking signals. The system never attempts to predict market
  direction or enter/exit based on forecasts.
- **Not a financial advisor.** It executes a fixed strategy specified in
  configuration. It does not adapt strategy to changing personal
  circumstances; phase transitions are pre-planned, not opportunistic.
- **Not a survivor of arbitrarily long bear markets.** The 24-month SGOV
  buffer defines the system's worst-case endurance. Bear markets exceeding
  that duration require human intervention; the system's job is to buy time,
  not to guarantee indefinite survival.

### 1.3 Design priorities, in order

When design decisions involve trade-offs, this is the priority order:

1. **Correctness.** The system must do what the spec says, every time, with
   no silent failure modes.
2. **Trust.** Behavior must be inspectable, predictable, and explainable to
   the operator at any point.
3. **Robustness.** Failures (network, broker, data) must degrade gracefully,
   never act on stale or incomplete information, and never leave the
   portfolio in an inconsistent state.
4. **Simplicity.** Fewer moving parts beats more clever ones. Code complexity
   that doesn't directly serve correctness, trust, or robustness is a
   liability.
5. **Performance.** Effectively unconstrained. The system runs at most a few
   times per day; computational efficiency is irrelevant compared to
   correctness.

### 1.4 Operational principles

In addition to the implementation priorities above, the system commits to
two operator-facing properties:

- **Failure-quiet.** The system pays scheduled income reliably without
  requiring action from the operator or survivor. Routine operation does
  not generate operator decisions. Alerts fire on state transitions and
  failures, but the absence of operator action is the normal case. In the
  survivor scenario, the survivor inherits a running system, not a system
  awaiting instructions.

- **Reversible decisions where possible.** Mechanisms with operational
  impact prefer toggleable forms over committed-once forms. The STOP
  INCOME token (§4.4, §10) is the canonical example: a survivor can pause
  income and resume it freely, with the schedule continuing as if no pause
  had occurred. Non-reversible decisions are limited to those genuinely
  required by external systems (phase progression is monotone; Phase 3
  is permanent once latched) or by the nature of the action (executed
  trades cannot be unmade).

---

## §2. Foundational Assumptions

These assumptions are baked into the system's design. Violating any of them
invalidates the spec.

### 2.1 Account type

The managed account is an IRA (Traditional or Roth). All trading activity
is non-taxable at the transaction level. No tax-lot tracking, wash-sale
avoidance, short-term/long-term gain distinction, or 1099-B reporting is
required of the system.

### 2.2 Strict allowlist

The system operates on a fixed, configured list of symbols:

| Bucket | Symbols (Phase 1) | Symbols (Phase 2) | Symbols (Phase 3) | Purpose |
|---|---|---|---|---|
| Core Growth | FBCG, AVUV | FBCG, AVUV | FBCG, AVUV | Volatility source |
| Core Fixed Income | PYLD, JPIE | GBIL | PYLD, JPIE | Income engine + rebalance sink |
| Buffer | SGOV | SGOV | SGOV | Crisis-mode withdrawal source |
| Cash | (USD) | (USD) | (USD) | Transaction reserve + 1mo withdrawal float |

**Anything else held in the account is invisible to IRAPM.** The system
does not see, value, trade, or report on non-allowlisted positions.
This is a *correctness invariant*, not a feature: the operator may hold
unrelated positions in the same account, and IRAPM must not interfere
with them or be confused by them.

Phase 2 substitutes GBIL for PYLD/JPIE as the FI holding; Phase 3 reverts
to PYLD/JPIE (provisional, simulator-tunable per §14). Phase transitions
handle the swap mechanics (§4.3, §7.2).

### 2.3 Single source of tunable parameters

All financially meaningful parameters live in a single configuration
file (`ruleset.yaml` or equivalent). Magic numbers in code are spec
violations. This includes — non-exhaustively:

- Target allocation weights per phase
- 5/25 rebalance thresholds (the 5% absolute and 25% relative figures)
- Circuit-breaker activation thresholds (-10%, -20%)
- Circuit-breaker hysteresis buffers (+2%, +5%)
- Confirmation window durations (2 weeks)
- CB1 → CB2 timer (90 days, parameter `cb1_to_cb2_timer_days`))
- Lookback window (6 months / ~26 weekly bars)
- Lookback staleness gates (14 days max staleness, ≥80% bar coverage)
- Phase 1 withdrawal amount and inflation rate ($3000/mo in 2027 USD,
  3.5% canonical `inflation_rate` per §4.1.2)
- Phase 3 starting income calculation inputs:
  `phase3_i0_calc_return_assumption = 0.06`,
  `phase3_i0_calc_inflation_assumption = 0.04`,
  `phase3_i0_calc_horizon_years = 30` (per §4.1.1)
- Phase 3 per-month payment ceilings (both apply, whichever is tighter):
  - `phase3_monthly_payment_ceiling_rate = 0.075` (portfolio-%
    ceiling), applied as `current_portfolio × rate / 12`
  - `phase3_dollar_ceiling_base_dollars = 4000`,
    `phase3_dollar_ceiling_base_year = 2027` (dollar ceiling
    indexed annually at `inflation_rate`)
  Per §4.1.1.2.
- Annual review date (default January 15)
- Freeze evaluation threshold days (default 30 cumulative CB1+ days)
- SGOV buffer target (24 months × current monthly withdrawal)
- SGOV refill rate (target/12 per month, recomputed annually on Jan 15)
- SGOV refill startup delay (`sgov_refill_post_recovery_delay_days`,
  default 60 days post-recovery)
- Cash buffer target (`current_monthly_withdrawal + $1000` Phase 1/3, $1000 Phase 2)
- Cash buffer tolerance ($250)
- Portfolio-low CB2 trigger thresholds (default $50K floor, 18 months
  multiplier, 10% recovery buffer)
- FI-low CB2 trigger threshold (default 6 months × monthly withdrawal,
  10% recovery buffer)
- Position residual minimum ($1500 default)
- Phase 2 opportunistic rebalance threshold (signal ≤ -10%)
- Phase 2 opportunistic recovery threshold (signal ≥ +2%, absolute)
- Phase 1 → Phase 2 transition date (2035-01-01)
- ACHScheduleUpdate consecutive-failure Warning-escalation threshold
  (`ach_update_warning_threshold_cycles`, default 3). Failures
  generate Notice alerts until this threshold, then escalate to
  Warning severity; no halt is triggered (per §8.2.7)
- STOP INCOME stuck-token alert threshold and re-alert cadence
  (`stopincome_stuck_alert_months` default 12,
  `stopincome_stuck_realert_months` default 3)
- Master/slave coordination timing (`master_heartbeat_time` default
  06:00 ET, `rsync_replication_time` default 06:15 ET,
  `slave_check_time` default 06:30 ET,
  `slave_healthy_threshold_hours` default 24,
  `slave_wake_staleness_hours` default 72,
  `slave_promotion_grace_hours` default 48).
  All coordination timing thresholds are in hours for consistency
  (§9.4.2).
- Phase 2 semi-annual reallocation dates
  (`phase2_reallocation_dates`, default `[Jan 15, Jul 15]`; weekend/
  holiday shift to next trading day) — per §7.5.2.b.
- Pause auto-resume window (`pause_auto_resume_hours`, default 48,
  per §11.3).
- Consecutive same-reason pause escalation threshold
  (`pause_consecutive_escalation_count`, default 4, per §11.3).
- Large cash deployment trigger thresholds
  (`large_cash_deployment_threshold_dollars` default 25000;
  `large_cash_deployment_threshold_rate` default 5; trigger fires
  when cash surplus exceeds the max of the two — per §7.7.1).
- Order fill timeout (`order_fill_timeout_seconds`, default 60,
  per §8.2.1).
- Dry-run mode (`dry_run`, boolean, default false, per §13.7).

**Operator-configured operational parameters (not financial-strategy
parameters but still in ruleset.yaml because it is the operator's
only edited file):**

- ACH destination (`ach_destination`): operator's external bank
  account identifier (IBKR-side reference, not raw banking info).
  Set once at deployment; edited only via runbook procedure if the
  external bank changes. Read by IRAPM at runtime and included in
  the Withdrawal Plan entry per §7.3.3. Lives in ruleset.yaml rather
  than `.env` because `.env` is reserved for secrets (per §9.1.3)
  and the ACH destination is not a credential.
- Alerter contact info (operator email, operator SMS number, etc.).

The intent is that the operator can re-tune the strategy without
touching code, and that simulator runs can sweep parameter ranges
without modification.

#### 2.3.1 Consolidated parameter table

The §2.3 bullet list above is descriptive; this subsection collects
all parameters by name with canonical defaults, units, and governing
section. The intent is to serve as the single reference for drafting
`ruleset.yaml` (§14.2) and as the canonical map between parameter
names used elsewhere in the spec and their concrete defaults. Every
configurable value in the system has a row here; any value referenced
in code that lacks a row is a spec violation per §1's "magic numbers"
rule.

**Allocation and rebalancing:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `rebalance_absolute_threshold_rate` | 5 | percent | §7.5.1 |
| `rebalance_relative_threshold_rate` | 25 | percent | §7.5.1 |
| `confirmation_window_weeks` | 2 | weeks | §6.3 |
| `position_residual_minimum_dollars` | 1500 | dollars | §3.14 (I12) |

**Circuit breakers:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `cb1_threshold_rate` | -0.10 | decimal | §3.10, §6.3.2 |
| `cb1_recovery_buffer_rate` | 0.02 | decimal | §6.3.2 |
| `cb2_threshold_rate` | -0.20 | decimal | §3.10, §6.3.1 |
| `cb2_recovery_buffer_rate` | 0.05 | decimal | §6.3.3 |
| `cb1_to_cb2_timer_days` | 90 | days | §6.3.5 |
| `portfolio_low_threshold_dollars` | 50000 | dollars | §3.10, §6.3.1 |
| `portfolio_low_threshold_months` | 18 | months | §3.10, §6.3.1 |
| `portfolio_low_recovery_buffer_rate` | 0.10 | decimal | §6.3.3 |
| `fi_low_threshold_months` | 6 | months | §3.10, §6.3.1 |
| `fi_low_recovery_buffer_rate` | 0.10 | decimal | §6.3.3 |
| `confirmation_window_weeks` | 2 | weeks | §6.3.1, §6.3.2 |

**Lookback signal:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `lookback_window_weeks` | 26 | weeks (≈6 months) | §5 |
| `lookback_max_staleness_days` | 14 | days | §5 |
| `lookback_min_bar_coverage_rate` | 0.80 | decimal | §5 |

**Global inflation:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `inflation_rate` | 0.035 | decimal | §4.1, §4.1.2, §7.6 |

**Phase 1:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `phase1_initial_monthly_dollars` | 3000 | 2027 USD | §4.1 |
| `phase1_trigger_year` | 2027 | year | §3.13, §4.1 |
| `phase1_to_phase2_transition_date` | 2035-01-01 | ISO date | §4.1, §4.2 |

**Phase 2:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `phase2_opportunistic_trigger_rate` | -0.10 | decimal | §7.5.2.a |
| `phase2_opportunistic_recovery_rate` | 0.02 | decimal | §7.5.2.a |
| `phase2_reallocation_dates` | [Jan 15, Jul 15] | MM-DD list | §7.5.2.b |

**Phase 3 (per §4.1.1):**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `phase3_i0_calc_return_assumption` | 0.06 | decimal | §4.1.1 |
| `phase3_i0_calc_inflation_assumption` | 0.04 | decimal | §4.1.1 |
| `phase3_i0_calc_horizon_years` | 30 | years | §4.1.1 |
| `phase3_monthly_payment_ceiling_rate` | 0.075 | decimal | §4.1.1.2 |
| `phase3_dollar_ceiling_base_dollars` | 4000 | dollars | §4.1.1.2 |
| `phase3_dollar_ceiling_base_year` | 2027 | year | §4.1.1.2 |

**SGOV buffer:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `sgov_buffer_target_months` | 24 | months | §3.6, §7.4 |
| `sgov_refill_post_recovery_delay_days` | 60 | days | §7.4.1 |

**Cash buffer:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `cash_buffer_offset_dollars` | 1000 | dollars | §3.7, §7.7 |
| `cash_buffer_tolerance_dollars` | 250 | dollars | §7.7 |

**Large cash deployment (§7.7.1):**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `large_cash_deployment_threshold_dollars` | 25000 | dollars | §7.7.1 |
| `large_cash_deployment_threshold_rate` | 0.05 | decimal | §7.7.1 |

**Annual review:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `annual_review_date` | Jan 15 | MM-DD | §7.6 |
| `freeze_evaluation_threshold_days` | 30 | days | §7.6.1 |

**Action layer:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `order_fill_timeout_seconds` | 60 | seconds | §8.2.1 |
| `ach_update_warning_threshold_cycles` | 3 | cycles | §8.2.7 |

**Hardware tokens:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `phase3_grace_window_hours` | 24 | hours | §10.6.2 |
| `phase3_token_count_required` | 4 | tokens | §10.8 |
| `stopincome_token_count_required` | 2 | tokens | §10.8 |
| `stopincome_stuck_alert_months` | 12 | months | §10.7.1 |
| `stopincome_stuck_realert_months` | 3 | months | §10.7.1 |
| `token_mismatch_critical_cycles` | 2 | cycles | §10.5 |

**Master/slave coordination (all in hours per §9.4.2):**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `master_heartbeat_time` | 06:00 ET | clock | §9.4.2 |
| `rsync_replication_time` | 06:15 ET | clock | §9.4.2 |
| `slave_check_time` | 06:30 ET | clock | §9.4.2 |
| `slave_healthy_threshold_hours` | 24 | hours | §9.4.2 |
| `slave_wake_staleness_hours` | 72 | hours | §9.4.2 |
| `slave_promotion_grace_hours` | 48 | hours | §9.4.2 |

**Failure recovery (per §11.3):**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `pause_auto_resume_hours` | 48 | hours | §11.3 |
| `pause_consecutive_escalation_count` | 4 | re-pauses | §11.3 |

**Operational (operator-configured):**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `ach_destination` | (operator-specific) | IBKR bank ref | §2.3, §9.1.5 |
| `dry_run` | false | boolean | §13.7 |
| `cycle_schedule` | Wed 10:00 ET | clock | §9.6 |

### 2.4 Modularity principle

The system decomposes into separable concerns. Each subsystem has a
narrow, well-defined responsibility and communicates with others
through explicit interfaces. The original IPM's modular layout is the
*architectural inspiration*; the specific module breakdown for IRAPM
is derived fresh from this spec, not inherited.

The expected top-level subsystems (subject to refinement during
detailed design):

- Configuration loader and validator
- Market data fetcher (broker quotes; price-history files for lookback)
- Synthetic Growth Lookback signal generator
- Circuit-breaker state machine
- Phase manager
- Income state manager
- Rebalancer (5/25 enforcement, Phase 2 opportunistic swing)
- Withdrawal router (decides source per cycle)
- Schedule manager (annual review: freeze evaluation + buffer/refill recompute)
- SGOV buffer manager (drain via cascade, refill via Growth drawdown)
- Cash buffer manager
- Order executor (broker interface)
- Alerter (email + SMS)
- Persistence layer (state minimal — see §6.7)
- Master/slave coordination
- Token monitor
- Scheduler / orchestrator (runs cycles on cadence)
- Recovery / replay (restart-safety)

### 2.5 Decision/action separation

Decisions (pure functions of state) are computed separately from
actions (broker calls, alerts, persistence writes). This is a direct
response to a class of bugs in IPM v1 where decisions and actions
were conflated, allowing one condition to mistakenly govern two
behaviors. Concretely:

- A decision module reads state and emits a structured plan (e.g.,
  "rebalance: BUY 23 SGOV, SELL 12 FBCG"; "withdrawal: $3000 sourced
  via cascade — SGOV $2000, FI $1000").
- An action module executes the plan, with no decision authority.
- Plans are loggable, inspectable, and testable in isolation.

### 2.6 Determinism and reproducibility

Given the same input state (account positions, market prices, phase,
date), the system produces the same plan every time. Any non-determinism
(random tie-breaking, time-of-day sensitivity) is a spec violation and
must be eliminated.

Corollary: any historical decision can be re-derived from the inputs
that were available at the time. This is what makes the system
auditable and what allows the simulator to validate it against
historical data.

### 2.7 Fail-safe defaults

When the system cannot proceed safely — missing data, broker
unreachable, signal unavailable, ambiguous state — it does **nothing**
and alerts the operator. Specifically:

- Missing or stale price data → skip the cycle, alert.
- Broker disconnect during a cycle → abort the cycle cleanly, alert,
  recover on next attempt.
- Lookback signal unavailable → CB state remains as-is (no transitions),
  no rebalancing, withdrawals deferred if possible.
- Configuration invalid → refuse to start.

The system must never act on a partial picture. "Do nothing" is always
a valid action.

### 2.8 Money discipline

All monetary values (account balances, position values, withdrawal
amounts, buffer targets, drift calculations, order amounts) use the
`Decimal` type, **rounded to the nearest cent** ($0.01). Float
arithmetic is forbidden anywhere money is involved.

Rationale: float-binary-fraction artifacts produce results like
`$3000.00 - $2500.00 = $499.9999999999998`, which break equality
comparisons, drift the buffer accounting over time, and cause silent
penny-discrepancies between computed plans and broker fills. Decimal
arithmetic is exact, and rounding to the cent matches the broker's
own precision.

Implementation rules:
- Quantities (share counts) may be integer or Decimal depending on
  whether fractional shares are supported by the broker for the
  symbol; check per-symbol, do not assume.
- Percentages and ratios (allocation weights, drift fractions, signal
  values) use Decimal but are NOT rounded to two places — they retain
  meaningful precision.
- Conversions from float (e.g., from a third-party data source) go
  through `Decimal(str(value))` to preserve the source's stated
  precision without introducing binary-fraction artifacts.
- Rounding mode is `ROUND_HALF_EVEN` (banker's rounding) — the Python
  default and the financial-industry standard.

### 2.9 Deposits, conversions, and external cash flows are out of scope

IRAPM does not initiate, schedule, track, or coordinate any inflows
to the managed account. Specifically out of scope:

- Roth conversions from external 401(k) / IRA sources
- Rollover deposits
- Operator-initiated cash deposits
- Any other inflow

Operationally, Phase 1 has a significant inflow pattern: ~$200K
initial Roth conversion in 2027 plus annual conversions of ~$130K
in years 2–8. These happen at the brokerage level, outside IRAPM's
awareness or control.

When new cash appears in the managed account, IRAPM treats it as
ordinary cash. On the next cycle, the cash buffer is taken to target
first; remaining cash is deployed per the active phase's allocation
rules. Two deployment paths exist depending on inflow size:

- **Small inflows** (dividends, rounding residuals, modest deposits
  below the large-deployment threshold) deploy bite-by-bite via the
  cash buffer surplus rule (§7.7) — one position per cycle,
  most-underweight first.
- **Large inflows** (Phase 1 annual Roth conversions, bulk deposits
  exceeding `large_cash_deployment_threshold_dollars` or
  `large_cash_deployment_threshold_rate` of portfolio value) trigger
  an explicit multi-position deployment plan that restores all
  underweight positions to target weights in one cycle (§7.7.1).

No special handling, no deposit-detection logic, no reconciliation
against external records — both paths are emergent from cash being
above target.

**Bootstrap consideration.** Because the Year-1 portfolio after
house/car/expenses is small (~$100K), and grows incrementally via
annual conversions over years 2–8, early Phase 1 operates with a
core portfolio too small to support a full 24-month SGOV buffer
purely from organic refill. The recommended bootstrap regime is:

1. Before IRAPM startup, manually pre-fund the SGOV buffer to its
   24-month target (~$72K at $3K/mo) and place the remaining ~$28K
   evenly across the four core positions (Growth and FI).
2. Start IRAPM. The Portfolio-low CB2 entry path (§3.10, §6.3.1)
   fires within 2 weeks (core $28K < threshold $54K), enters CB2,
   and routes withdrawals through cascade (SGOV).
3. Year-1 withdrawals draw $36K from SGOV, leaving the core
   undisturbed.
4. Year-2 Roth conversion (~$130K) arrives. The large cash
   deployment in §7.7.1 deploys in target-weight proportional mode
   (signal is nominal, so CB2 is resource-only). The deployment
   clears the Portfolio-low condition; CB2 exits to CB_INACTIVE.
   Normal operation begins.
5. SGOV refill resumes after the 60-day post-recovery delay
   (§7.4.1), drawing $6K/mo from core back into SGOV. By mid-Year 3
   the buffer is restored to target.

The alternative — depositing the full $100K into IRAPM at once with
no manual SGOV pre-fill — also works (no CB2 trigger; SGOV fills
organically over Year 1) but produces more rebalance and refill
activity during Year 1 as ~$36K migrates from core to SGOV through
small rebalance trades.

The Portfolio-low and FI-low CB2 entry paths (§3.10) also protect
the Phase 3 Year-1 scenario where the survivor inherits a small
portfolio without the operator's bootstrap discipline.

---

## §3. Domain Model

This section defines the vocabulary used by the rest of the spec.
All later sections refer to entities and quantities defined here.

### 3.1 The account

The single tax-advantaged brokerage account managed by IRAPM. Contains:

- **Allowlisted positions** — the symbols in §2.2 that IRAPM operates on.
- **Allowlisted cash** — USD held within the cash buffer accounting.
- **External holdings** — anything else, invisible to IRAPM.

### 3.2 Position

A holding of one allowlisted symbol. Characterized by:

- **Symbol** (e.g., FBCG)
- **Share count**
- **Last observed market price**
- **Market value** = share count × last price
- **Bucket assignment** (Core Growth, Core FI, or Buffer)

### 3.3 Buckets

The system organizes positions into named buckets per active phase
(see §2.2 for the per-phase symbol assignments).

A bucket's **value** is the sum of market values of its member positions.

### 3.4 Core portfolio

The combined Growth + Fixed Income buckets. **The buffer and cash
buckets are explicitly NOT part of the core portfolio.** All percentage
calculations (5/25 rebalance, lookback signal, allocation drift) operate
on the core portfolio only.

This isolation is a deliberate design choice: the buffer and cash
buckets exist to absorb withdrawals and operational expenses without
contaminating the rebalancing signals. If they were included in core
calculations, withdrawing from the buffer would change the apparent
allocation of Growth and FI, polluting the 5/25 logic.

### 3.5 Target allocation

A configured percentage assigned to each core position, summing to 100%.
For example, in Phase 1: FBCG 25%, AVUV 25%, PYLD 25%, JPIE 25%
(illustrative — actual targets in ruleset).

The **target value** of a position is `core_portfolio_value × target_pct`.

The **drift** of a position is `current_value − target_value`, expressed
in dollars and as a percentage of the target value.

### 3.6 Buffer accounting

The SGOV buffer has its own internal accounting separate from market
value:

- **Buffer target** — `24 × current_monthly_withdrawal`. Recomputed at
  the annual review (§7.6) using the new year's monthly withdrawal
  amount. In Phase 2 (no scheduled withdrawals), the buffer target
  carries forward at its end-of-Phase-1 value (no recompute, since
  there is no current monthly withdrawal to reference).
- **Buffer market value** — current SGOV holdings × SGOV price.
- **Buffer surplus / deficit** — market value minus target.
- **Refill state** — one of: idle (buffer at target), draining
  (cascade active, refill suspended), delayed (within
  `sgov_refill_post_recovery_delay_days` post-recovery window),
  refilling (active batches running), exhausted (at residual
  floor). See §12.3.
- **Monthly refill rate** — `buffer_target / 12`. Recomputed at annual
  review. The same monthly rate applies to every refill batch in that
  year, regardless of how large the deficit is. Small deficits
  (1-2 months drained) refill quickly; large deficits (12+ months
  drained) refill at the annual rate.

The buffer value is **never** included in core portfolio calculations,
allocation drift, or lookback signals.

### 3.7 Cash accounting

The cash buffer has analogous accounting:

- **Cash target** —
  - Phase 1, Phase 3: `current_monthly_withdrawal + $1000`. Recomputed
    at annual review with the new year's withdrawal amount.
  - Phase 2: $1000 (transaction reserve only; no withdrawal float
    needed).
- **Cash actual** — current USD in account allocated to the cash buffer.
- **Cash deviation** — actual minus target.
- **Tolerance** — $250. Refill triggers when deviation exceeds tolerance
  in either direction.

Cash is also excluded from core portfolio calculations.

### 3.8 Cycle

A single execution of the system's main loop. Cycles are scheduled
by the OS (systemd timers) and consist of: input refresh → state
evaluation → decision → action → persistence → alert.

Two cycle types exist (§6.5, §9.6):

- **weekly** — full §6.5 evaluation. Default Wednesday 10:00 ET.
  Several steps within the weekly cycle are date-gated:
  monthly-withdrawal step (the weekly cycle on or before 4 business
  days before the 15th of the month), annual-review step (the weekly
  cycle on or after January 15), Phase 2 semi-annual reallocation
  step (the weekly cycle on or after each phase2_reallocation_dates
  entry, in Phase 2 only).
- **daily-token** — minimal: token state read + write to local state
  file + alerts. No broker interaction, no signal computation, no
  decision layer.

Cycle types run at different times of day on overlapping dates. The
weekly cycle is the only one that touches the broker, computes the
signal, or generates Plan entries.

### 3.9 Phase

A discrete operating mode of the system: Phase 1, Phase 2, or Phase 3.
Each phase has its own configuration (target allocation, withdrawal
behavior, refill behavior, applicable CB framework). Phase 1 → Phase 2
transitions on a configured calendar date; Phase 1/2 → Phase 3
transitions on a hardware-token event (see §4 and §10).

Phase progression is monotone (no PHASE_2 → PHASE_1; no exit from
PHASE_3).

### 3.10 Circuit Breaker (CB) states

The system maintains discrete CB states that govern withdrawal and
rebalance behavior. The CB framework is active in **Phase 1 and
Phase 3** (the income-producing phases). Phase 2 uses a single
opportunistic-rebalance threshold and does not maintain CB states.

**Three CB machine states:**

- **CB_INACTIVE** — Default state. No circuit breaker tripped.
  Rebalancing active; withdrawals sourced from most-overweight core
  position then proportionally from FI buckets.
- **CB1** — Growth lookback ≤ `cb1_threshold_rate` (default -10%),
  confirmed for `confirmation_window_weeks` (default 2). Rebalancing
  suspended. Withdrawals sourced from FI bucket only: most-overweight
  FI position first, then proportional from remaining FI buckets.
  Growth is never sold for withdrawals during CB1.
  - **CB1 → CB2 timer transition** — CB1 has been continuously
    active for `cb1_to_cb2_timer_days` (default 90), triggering an
    automatic transition to CB2 via timer (§6.3.5) without recovery.
    Sub-state of CB1 (not an independent state). Behaviorally
    equivalent to CB2 for withdrawals (cascade sourcing).
- **CB2** — Any of three independent conditions, each with 2-week
  confirmation:
  - **Signal-triggered:** Growth lookback ≤ `cb2_threshold_rate`
    (default -20%).
  - **Portfolio-low triggered:** core portfolio value (Growth + FI,
    excluding buffer and cash) < `max(portfolio_low_threshold_dollars,
    portfolio_low_threshold_months × current_monthly_withdrawal)`.
    Defaults: $50,000 / 18 months.
  - **FI-low triggered:** core FI bucket value <
    `fi_low_threshold_months × current_monthly_withdrawal`.
    Default: 6 months.

  In CB2, rebalancing is suspended and withdrawals divert to the
  SGOV cascade. CB2 exits only when ALL active entry conditions
  have cleared with their respective recovery buffers (§6.3.3).

The cascade target (SGOV → FI → Growth) is identical regardless of
which CB2 entry condition activated it.

The three CB2 entry paths exist because they protect against
distinct failure modes:

- **Signal path** protects against market drawdowns where Growth
  selling would lock in losses.
- **Portfolio-low path** protects the bootstrap and survivor
  scenarios where the core portfolio is too small to sustain
  withdrawals without external infusion (Roth conversions in
  early Phase 1; survivor inherits small portfolio in Phase 3
  Year 1; see §2.9).
- **FI-low path** protects against FI depletion outpacing
  rebalancing — withdrawals draining FI faster than Growth
  appreciation can replenish it via 5/25.

Each path emits a distinct trigger reason on the `cb_transition`
alert at entry (`signal`, `portfolio_low`, `fi_low`, or `cb1_timer`)
so the operator knows why CB2 activated.

### 3.10a Income state

Independent of CB state and Phase, the system maintains an **income
state** that controls whether scheduled withdrawals execute:

- **Active** — scheduled withdrawals execute per the active phase's
  withdrawal calculation.
- **Paused** — scheduled withdrawals are zero. All other system
  behavior continues normally (rebalancing, CB transitions, refill,
  alerts, annual reviews including freeze evaluation).

Income state is controlled by the STOP INCOME hardware token (§10),
with **AND semantics across both CL260 boxes** (both boxes must
report tokens inserted to enter PAUSED state; both must report
removed to return to ACTIVE; mismatched readings hold previous state
per §10.5). In Phase 2, the income state is irrelevant (no scheduled
income exists to pause); state changes are logged but produce no
behavior change.

When income resumes after a pause, the schedule continues as if it
had never paused — see §4.4 for resume semantics.

### 3.11 Signal: Synthetic Growth Lookback

Defined in detail in §5. A scalar value representing the fractional
change of a synthetic equal-weighted Growth-bucket price index over
the configured lookback window (default 6 months, weekly cadence).
Sign convention: negative = drawdown.

### 3.12 Plan

A structured, serializable description of what the system intends to
do this cycle. Plans contain zero or more entries of these types:

- **Order** — buy or sell N shares of symbol S in bucket B
- **Withdrawal** — deliver $X via ACH; includes source breakdown
  `[(symbol, dollar_amount, share_count), ...]`, ACH destination,
  scheduled settlement date (see §7.3.3)
- **BufferRefill** — buy $X SGOV, sourced from selling $X of symbol(s)
  S (Growth-only sourcing per §7.4)
- **CashRefill** — buy or sell $X to bring cash buffer toward target
- **LargeCashDeployment** — coordinated multi-position BUY batch
  deploying a large cash surplus across underweight core positions
  in one cycle. Triggered when cash surplus exceeds the
  large-deployment threshold (§7.7.1); typical case is Phase 1
  annual Roth conversion inflows. Distinct from CashRefill (small
  rounding) and PhaseTransition (allocation reset).
- **PhaseTransition** — coordinated batch of liquidations and rebalances
  to transition asset allocation (§7.2)
- **CBStateTransition** — change CB state (logged but not itself an
  external action; appended to CB transition log)
- **ACHScheduleUpdate** — update IBKR's recurring monthly ACH amount
  (e.g., $0 on STOP INCOME pause, restored on resume, updated annually
  at annual review)
- **Alert** — dispatch notification. Alert entries are dispatched at
  end-of-cycle, after the other entries have completed or failed
  (§8.2.8). The Alert entry's content can include actual outcomes
  (e.g., "BUY 23 SGOV — filled at $112.45"), and alert dispatch is
  *itself* not gated on action success — a cycle that failed
  mid-execution still dispatches its alerts so the operator learns
  about the failure.

A plan is generated by the decision layer and consumed by the action
layer. An empty plan ("do nothing this cycle") is the most common
outcome.

### 3.13 Schedule state

For each income-producing phase (1 and 3), the system maintains a
durable **schedule state** that fully determines scheduled monthly
income for any year:

```
schedule_state = {
    I_0:           Decimal,    # starting monthly income, base year USD
    trigger_year:  int,        # year I_0 was set
    cpi:           Decimal,    # annual CPI rate (0.03 P1, 0.04 P3)
    frozen_years:  list[int],  # years for which CPI raise was skipped
}
```

Phase 1 instance at program start: `(3000, 2027, 0.03, [])`.
Phase 3 instance at trigger: `(computed_I_0, trigger_year, 0.04, [])`.

Current scheduled monthly income for year Y is computed as:

```
n_raises_applied = (Y - trigger_year)
                   - count(Y' in frozen_years where trigger_year+1 ≤ Y' ≤ Y)
scheduled_monthly = I_0 × (1 + cpi)^n_raises_applied
```

The range `trigger_year+1 ≤ Y' ≤ Y` is **inclusive on both ends**.
This is a pure function of the immutable triplet plus calendar year.
The withdrawal layer reads the current scheduled value; it does not
re-derive freeze decisions.

### 3.14 Invariants

Properties that must always hold true. Tested in §13.5.

- **I1:** The buffer bucket is never included in core portfolio calculations.
- **I2:** The cash bucket is never included in core portfolio calculations.
- **I3:** Non-allowlisted holdings never appear in any IRAPM calculation,
  plan, or alert.
- **I4:** During CB1, CB2
  states, no rebalancing trade touches Growth.
- **I5:** FI is never sold to fund a Growth purchase (FI sacrosanct).
  This applies to **rebalancing trades**. It does not apply to
  withdrawals (see §7.3.2 for cascade semantics) or to cash refills
  (FI → cash is permitted regardless of size).
- **I6:** SGOV buffer is never used for anything other than
  cascade-sourced withdrawals or operator-authorized non-recurring
  expenses.
- **I7:** A withdrawal action and a buffer-refill action cannot coexist
  in the same plan in opposite directions on the same symbol.
- **I8:** Plan generation is deterministic given identical input state.
- **I9:** An invalid configuration prevents system startup; it never causes
  silent fallback to defaults.
- **I10:** SGOV buffer refill is suspended whenever any CB2 condition
  is active, including the Portfolio-low and FI-low resource-based
  paths. The system does not drain a depleted core portfolio to
  refill the buffer.
- **I11:** Inflows to the managed account (deposits, conversions,
  rollovers) are never initiated by IRAPM. The system only reacts to
  cash that appears.
- **I12:** Every position in the active phase's allowlist maintains a
  market value ≥ `position_residual_minimum_dollars`. The system
  never sells a position below this floor. Cascade and refill SELL
  decisions are clamped at the residual floor; if the floor is
  reached, the position is treated as exhausted-for-this-cycle and
  any remaining demand cascades to the next stage.
- **I13:** Phase transitions may liquidate a position to zero only if
  the position appears in NO allowlist of any future-reachable phase
  from the post-transition phase. Equivalently: liquidation is
  permitted iff the position is permanently retired across all
  reachable future system states. Positions that may be needed in a
  future phase (e.g., PYLD/JPIE during Phase 2, in case Phase 3
  activates) are held at `position_residual_minimum_dollars` instead
  of being liquidated. This is the deliberate, audited exception
  to I12 — and the rule for when the exception applies. See §7.2
  for the per-transition liquidation table.
- **I14:** During CB1 (not in CB2), withdrawals are sourced
  exclusively from the FI bucket. Growth is never sold for
  withdrawals during CB1. (This narrows I5: rebalancing trades are
  symmetrically constrained, withdrawals during CB1 are
  asymmetrically constrained against Growth.)
- **I15:** If `phase == PHASE_3` then either `schedule_state.phase3`
  is non-null, OR the Phase 3 transition cycle has not yet executed
  (the latched-but-pending window per §4.3). The decision layer
  treats this window as transition-pending and suppresses withdrawal
  and rebalance actions until the transition cycle runs. This
  invariant defends against the race condition where the Phase 3
  latch fires (writing `phase = PHASE_3` to state) but the next
  weekly cycle that initializes `schedule_state.phase3` has not yet
  run. Any cycle that fires in the latch-vs-transition window must
  detect this state and defer all withdrawal/rebalance work to the
  transition cycle. See §7.3 for the withdrawal suppression rule and
  §7.5.1 for the rebalance suppression rule.
- **I16:** Within a single cycle attempt (uniquely identified by
  `cycle_uuid`), all decision-affecting time queries return the
  same captured `decision_clock` value. Restart of an existing
  cycle_uuid reuses the captured value, NOT current wall-clock time.
  This invariant ensures that Plan generation (which I8 requires to
  be deterministic given identical inputs) remains deterministic
  across cycle restarts: a cycle that began at T+0 and is restarted
  at T+5 minutes uses the original T+0 decision_clock for all
  timing-sensitive decisions; the regenerated Plan is identical;
  and the deterministic `client_order_id`s match those of the
  original attempt, allowing the broker layer's idempotent
  rediscovery (§15.5) to find and reuse the prior attempt's orders.
  Persisted in `cycle_attempt.json` (§6.7, §15.11).

---

## §4. Phase Model

The system's operating mode is determined by **two independent
dimensions**:

1. **Phase** — Phase 1, Phase 2, or Phase 3. Determines asset
   allocation, withdrawal calculation, and which subsystems are active.
2. **Income state** — Active or Paused. Controlled by the STOP INCOME
   token mechanism (§10). Orthogonal to Phase: a Phase 1 system can
   have income paused; a Phase 3 system can have income paused.
   Income state is a no-op in Phase 2 (which has no income to pause).

This decoupling is deliberate. The original "Phase" concept conflated
several axes; separating them prevents the conflation-class bugs that
plagued IPM v1.

### 4.1 Phase definitions

#### Phase 1 — Income Production (2027-01-01 onward, until 2035-01-01 transition)

The system's primary income-production mode, active from program start
(operator's retirement date) until Social Security filing.

- **Active withdrawals:** governed by schedule_state
  `(phase1_initial_monthly_dollars, phase1_trigger_year, inflation_rate, [])`
  per §3.13. Defaults: initial monthly withdrawal $3000 in 2027 USD;
  raised 3% annually unless frozen by the annual review (§7.6).
- **Allocation:** Growth (FBCG, AVUV) + Fixed Income (PYLD, JPIE) per
  configured target weights.
- **Rebalancing:** 5/25 weekly check, active unless suppressed by CB
  state.
- **Withdrawal sourcing (CB_INACTIVE):** most-overweight core position first
  (any bucket), then proportionally from FI buckets.
- **Withdrawal sourcing (CB1):** most-overweight FI position first, then
  proportionally from remaining FI. Growth never sold for withdrawals
  during CB1.
- **Withdrawal sourcing (cascade):** SGOV → FI → Growth, when CB2 is
  active (any entry path per §6.3.1).
- **SGOV buffer target:** 24 × current monthly withdrawal (§3.6).
- **SGOV refill rate:** target / 12 per month (recomputed annually).
- **Cash buffer target:** `current_monthly_withdrawal + $1000` (§3.7).

#### Phase 2 — Pure Growth (2035-01-01 onward, until Phase 3 trigger or indefinitely)

After Social Security filing establishes a non-portfolio income floor
that covers all baseline expenses, the system transitions to pure
growth-accumulation mode. The portfolio no longer needs to fund living
expenses, so the FI back-end's role changes from income engine to
opportunistic dry powder.

- **Withdrawals:** halted. The system performs no scheduled withdrawals.
- **Allocation:** 90% Growth (FBCG, AVUV), 10% FI (GBIL).
- **FI substitution:** PYLD and JPIE are drawn down to
  `position_residual_minimum_dollars` (not liquidated, per I13);
  GBIL is bought as new position. See §7.2.1.
- **Rebalancing:** standard 5/25 does NOT apply. Rebalancing is
  triggered opportunistically when Growth lookback ≤ -10%, at which
  point GBIL is deployed to buy the most-underweight Growth holding
  (§7.5.2).
- **Circuit breakers:** the CB1/CB2 framework does not apply in Phase 2.
  The single trigger is the opportunistic-rebalance threshold above.
- **SGOV buffer:** maintained as emergency reserve. The buffer carries
  forward at its end-of-Phase-1 dollar value (no recompute since there
  is no current monthly withdrawal to reference). It is not redeployed
  at phase transition and not used for any scheduled purpose. It exists
  to absorb (a) Phase 3 activation if it occurs, and (b) major
  non-recurring expenses authorized by the operator (new roof, vehicle
  replacement, medical event). Refill is triggered whenever buffer
  market value < target (§7.4); refill rate stays frozen at the
  end-of-Phase-1 value (no annual recompute in Phase 2).
- **Cash buffer target:** $1000 (transaction reserve only; no
  withdrawal float needed).
- **STOP INCOME token state:** no-op. Token state changes are logged
  for audit but produce no behavior change.

#### Phase 3 — Survivor Income (event-triggered, indefinite duration)

Phase 3 is **structurally Phase 1 reactivated** with different asset
allocation, different withdrawal calculation, and a much longer
horizon. It activates only on physical removal of all four Phase 3
hardware tokens (see §10). Once activated and the 24-hour grace window
elapses, Phase 3 is permanent — there is no transition out.

**Inherits from Phase 1 (unchanged):**

- CB framework (CB_INACTIVE, CB1, CB2)
- Synthetic Growth Lookback signal (§5)
- 2-week CB confirmation; recovery buffers (CB1 +2%, CB2 +5% signal,
  +10% resource); 90-day CB1 → CB2 timer trigger
- SGOV buffer mechanism (target = 24 × current monthly withdrawal,
  recomputed at Phase 3 trigger and at each annual review)
- Cash buffer mechanism (target = `current_monthly_withdrawal + $1000`)
- Withdrawal sourcing logic (most-overweight first per CB state, then
  proportional FI; cascade when triggered; SGOV → FI → Growth identical
  sequence)
- Rebalancer / buffer refill / cash refill subsystem separation
- Schedule state structure (§3.13) and annual freeze evaluation (§7.6)
- Decision/action separation, determinism, money discipline,
  fail-safe defaults

**Overrides Phase 1 (replaces):**

- **Asset allocation:** 50% Growth / 50% Fixed Income, default
  25/25/25/25 across FBCG/AVUV/PYLD/JPIE (provisional, simulator-tunable
  per §14).
- **Withdrawal calculation:** adaptive starting income `I_0` computed
  once at Phase 3 trigger as a function of portfolio value, bounded by
  a floor and ceiling (see §4.1.1 below).
- **Schedule state:** `(I_0, trigger_year, 0.04, [])`. Post-trigger CPI
  raise is 4% annually, with freeze logic skipping raises in years where
  CB1+ was active for ≥ 30 days during the prior calendar year (§7.6).
- **Per-month payment ceiling:** monthly withdrawal is additionally
  clamped at `current_portfolio × 7.5% / 12` to prevent
  over-distribution during deep drawdowns. This clamp applies every
  month regardless of CPI scheduling. *Phase 3 only — Phase 1 has no
  such clamp; Phase 1 relies on the Portfolio-low CB2 entry path
  (§6.3.1) for the depletion case.*
- **Horizon:** designed for 27 years (vs Phase 1's 8). Affects
  validation choices but not runtime behavior.

**Adds to Phase 1 (new in Phase 3):**

- Token-based activation trigger with 24-hour grace window for
  accidental removal.
- Indefinite duration — Phase 3 has no scheduled exit.
- Permanent latch after grace window elapses; subsequent token
  manipulation has no effect.
- STOP INCOME token is meaningful (toggleable income pause —
  see §4.4).

#### 4.1.1 Phase 3 starting income calculation

When Phase 3 triggers in year `T`, the starting monthly income `I_0`
is computed via a 30-year finite-horizon annuity sustainability
formula using `P`, the current portfolio value at the moment the
transition cycle executes (post-cascade if applicable per §4.2):

```
I_0 = P × (r - i) / (12 × (1 - ((1 + i) / (1 + r)) ** N))
```

Where:
- `r = phase3_i0_calc_return_assumption` (default `0.06`)
- `i = phase3_i0_calc_inflation_assumption` (default `0.04`)
- `N = phase3_i0_calc_horizon_years` (default `30`)

These calculation-side parameters are **deliberately conservative**:
the assumed return is lower than the historical equity-heavy average
(~7%) and the assumed inflation is higher than the canonical
`inflation_rate` (3.5%). Using a pessimistic combination produces a
lower `I_0`, leaving the survivor cushion against actual conditions
diverging from assumptions.

The values of `I_0` and `trigger_year = T` are persisted to Phase 3
schedule state (§3.13) along with an empty `frozen_years` list.
Subsequent annual reviews (§7.6) may append to `frozen_years` but
never to `I_0`.

##### 4.1.1.1 Annual raises after activation

After latch, scheduled monthly withdrawal grows at the canonical
`inflation_rate` (default `0.035`) per year, applied annually at the
trigger anniversary. The Phase 3 freeze mechanism remains active
across the indefinite Phase 3 horizon — the annual review may decide
to freeze a year's raise based on prior-year CB1+ days
(`freeze_evaluation_threshold_days`, default 30). Frozen years are
appended to `frozen_years` and permanently excluded from the
`n_raises_applied` count.

The scheduled monthly withdrawal for any year `Y > T` is:

```
n_raises_applied = count of years in range(T + 1, Y + 1)
                   NOT in frozen_years
scheduled_monthly = I_0 × (1 + inflation_rate) ** n_raises_applied
```

##### 4.1.1.2 Per-cycle payment ceiling

Every Phase 3 cycle, the actual monthly withdrawal is clamped
against two independent ceilings — whichever is tighter binds:

```
portfolio_ceiling = current_portfolio_value × phase3_monthly_payment_ceiling_rate / 12
dollar_ceiling    = phase3_dollar_ceiling_base_dollars × (1 + inflation_rate) ** (current_year - phase3_dollar_ceiling_base_year)
actual_monthly    = min(scheduled_monthly, portfolio_ceiling, dollar_ceiling)
```

Where:
- `phase3_monthly_payment_ceiling_rate` defaults to `0.075` (7.5%
  annualized of current portfolio value).
- `phase3_dollar_ceiling_base_dollars` defaults to `4000` (the
  dollar ceiling in `phase3_dollar_ceiling_base_year` USD).
- `phase3_dollar_ceiling_base_year` defaults to `2027`.
- `inflation_rate` is the canonical 3.5% rate (§4.1.2), shared
  with the scheduled-monthly raise mechanism.

If either ceiling binds, the system pays the lesser ceiling and
emits a `monthly_payment_ceiling_bound` Notice alert identifying
which ceiling bound (portfolio-percentage, dollar, or both
simultaneously if they coincide within rounding). No other state is
affected: the schedule continues unchanged, the freeze evaluation is
not triggered, and the next month re-runs the same clamp against
the new portfolio value and the new year's indexed dollar ceiling.

The two ceilings protect against different failure modes:

- **Portfolio-percentage ceiling** binds when the portfolio shrinks
  relative to the schedule (drawdown post-latch, or inflation
  raises outstripping a slow-growing portfolio over decades). It
  scales down with the corpus, preserving sustainability.

- **Dollar ceiling** binds when `I_0` is calibrated against a large
  portfolio at latch (~$2M+) and would otherwise authorize
  withdrawals far above what the survivor's actual living-expense
  envelope requires. It prevents the corpus from being drained
  faster than need.

Sample dollar-ceiling values at default parameters
(`base = $4000`, `base_year = 2027`, `inflation = 0.035`):

| Year | Dollar ceiling (monthly) |
|---|---|
| 2027 | $4,000 |
| 2035 (Phase 1→2 boundary) | $5,266 |
| 2045 | $7,429 |
| 2057 (latest plausible Phase 3 trigger) | $11,210 |

In normal operation the ceilings do not bind. They are safety
clamps, not targets.

##### 4.1.1.3 Implementation summary

The Phase 3 transition cycle performs the following in order
(see §4.3 for the general transition mechanism):

1. Read portfolio value `P` after any cascade settlement.
2. Compute `I_0` from the annuity formula above using `P` and the
   three `phase3_i0_calc_*` parameters.
3. Persist Phase 3 schedule_state: `(I_0, trigger_year=T, cpi_rate=inflation_rate, frozen_years=[])`.
4. Recompute Phase 3 buffer target = `sgov_buffer_target_months × I_0`;
   refill rate = `target / 12`; cash target = `I_0 + cash_buffer_offset_dollars`.
   Persist via `buffer_state`.
5. Emit `phase3_activation` Critical alert including: `I_0`, `P`,
   the three calculation-input parameters, the resulting initial
   withdrawal rate (`12 × I_0 / P` as percentage of portfolio).

##### 4.1.1.4 Reference values (illustrative, not normative)

Using default parameters (`r=0.06`, `i=0.04`, `N=30`):

```
I_0 / P = (0.06 - 0.04) / (12 × (1 - (1.04/1.06) ** 30))
        = 0.02 / (12 × (1 - 0.5635))
        = 0.02 / 5.238
        ≈ 0.003818
```

So `I_0` is approximately `0.382%` of portfolio value per month
(`~4.58%` annualized initial withdrawal rate, well below the 7.5%
ceiling). Sample values:

| Portfolio at latch | `I_0` (monthly) | Year-30 nominal |
|---|---|---|
| $500,000 | $1,909 | $5,357 (≈$1,640 in 2026 USD at 3.5% inflation) |
| $750,000 | $2,864 | $8,035 |
| $1,000,000 | $3,818 | $10,714 |
| $1,500,000 | $5,727 | $16,071 |

The year-30 nominal column assumes no freezes (all 30 annual raises
apply). With freezes, the nominal stream is correspondingly lower.

#### 4.1.2 Inflation rate

A single canonical `inflation_rate` (default `0.035`) applies to all
annual withdrawal raises across Phase 1 and Phase 3.

The Phase 3 I_0 calculation uses a separate higher rate
(`phase3_i0_calc_inflation_assumption`, default `0.04`) as a
deliberately-pessimistic input to the one-time sustainability
formula at latch. This is not an inflation prediction; it is a
safety buffer that produces a lower I_0 than canonical inflation
would suggest, leaving room for actual inflation to exceed 3.5%.

### 4.2 Phase transition triggers

Phase transitions use different mechanisms depending on the
transition, reflecting their fundamentally different natures:

#### Phase 1 → Phase 2: calendar-date trigger

The transition occurs on the configured calendar date. The first
Phase 2 cycle runs on **2035-01-01** (exactly 8 years after
program start on 2027-01-01). The final Phase 1 cycle is the last
weekly cycle preceding 2035-01-01.

Calendar-date trigger (rather than state-based) is deliberate:
- **Predictability.** The operator knows exactly when the strategy
  changes.
- **Decoupling from market noise.** A bear market in late 2034 should
  not delay the filing of Social Security in 2035.
- **Auditability.** The transition is a single event with a single
  cause, easy to log and verify.

#### Phase 1 or Phase 2 → Phase 3: token-triggered (no scheduled date)

Phase 3 activation occurs on physical removal of all four Phase 3
hardware tokens, confirmed across both CL260 boxes (AND semantics).
After the 24-hour grace window elapses without re-insertion, Phase 3
**latches permanently** — the phase indicator is updated and the
`phase3_activation` Critical alert fires. The transition cycle
(asset reallocation, I_0 calculation) executes on the next weekly
cycle, **regardless of CB state**. The Phase 3
transition is a one-time allocation reset, not a rebalance, so
there is no reason to defer it for market conditions. If cascade
conditions are present at or after transition, Phase 3's withdrawal
sourcing (§7.3.2) handles them — the cascade machinery is
phase-agnostic in Phase 1 and Phase 3.

I_0 is computed against the portfolio value at the moment the
transition cycle executes, not at the moment of latch. Since the
transition cycle runs within at most 7 days of latch (typically
within 24-48 hours), the I_0 reflects the portfolio value at
transition time.

See §10 for the full token mechanism; §6 for phase machine semantics.

Phase 3 may be triggered from either Phase 1 or Phase 2:
- **Phase 1 → Phase 3:** operator died during the income-production
  years before SS filing.
- **Phase 2 → Phase 3:** operator died after SS filing but before any
  natural Phase 2 exit.

The transition mechanic is the same in both cases (see §4.3); only
the starting allocation differs (Phase 1 holdings vs Phase 2
holdings being liquidated and re-allocated to Phase 3 targets).

### 4.3 Phase transition mechanics

A phase transition is a planned, executable action. The general
sequence is:

1. **Pre-transition validation.** For Phase 1 → Phase 2 (calendar):
   on the last weekly cycle that falls at least 5 trading days
   before the transition date (typically 7-14 calendar days prior,
   depending on how the transition date falls relative to the
   weekly schedule), verify the transition can be executed cleanly
   (positions reconcilable, broker reachable, incoming-phase
   configuration valid). Alert operator on any failure. The
   validation runs at least 5 trading days before transition so the
   operator has time to respond to any failure surfaced. The
   validation does NOT pre-check cascade exhaustion; cascade
   exhaustion is handled separately as its own Critical-blocking
   failure (§11.2.7) and does not gate normal phase transition.
   Phase 3 transitions skip this step (no advance notice possible).
2. **Pending-confirmation discard.** Any pending state-machine
   confirmation in the outgoing phase (CB1 or CB2 mid-confirmation
   in Phase 1; Phase 2 opportunistic swing mid-confirmation) is
   discarded. The incoming phase starts with no inherited pending
   transitions.
3. **Final outgoing-phase cycle.** The last cycle under the outgoing
   phase's rules executes normally. Final state is logged in detail.
4. **Transition cycle.** On the transition date / activation event:
   - Liquidate or draw-to-residual positions per the per-transition
     liquidation table (§7.2).
   - Compute target allocation under the incoming phase.
   - Rebalance to incoming target allocation in a single coordinated
     plan. (Large Rebalance alert always fires.)
   - For Phase 3 transitions: compute and persist `I_0`,
     `trigger_year`, initial empty `frozen_years` list.
   - Update internal phase indicator and (for Phase 3) latch the
     phase as permanent.
5. **First incoming-phase cycle.** Run normally under new phase rules.

**Phase 3 transitions** have an additional pre-step: the **24-hour grace
window** (see §4.2 and §10.7). The transition cycle (step 4) runs
on the next weekly cycle after the grace window elapses without
re-insertion, regardless of CB state (§4.2).

**Mid-execution failures** during a transition cycle are handled by
the action layer per §8.3: no automatic rollback, next cycle re-reads
broker state and re-decides. A partially-transitioned state may
require operator review (Critical alert per §8.4.5).

Phase transitions are the single largest action the system performs.
They merit detailed pre-transition simulation. The spec does NOT
require operator confirmation at runtime (Phase 1→2 is automatic on
its date; Phase 3 activation is by physical token removal which is
itself the confirmation), but the pre-transition validation alert
performed at least 5 trading days before the transition (for calendar transitions) and the 24-hour grace window
(for Phase 3) provide intervention windows.

**Low-residual edge case at Phase 1 → Phase 2.** If PYLD or JPIE
positions are below the residual minimum at the moment of Phase 1 →
Phase 2 transition (e.g., due to Phase 1 cascade exhaustion), the
transition proceeds anyway. The transition plan flags the discrepancy
in its alert; the residual draw-down step becomes a no-op for any
already-below-residual position. The transition is not blocked.

### 4.4 The orthogonal income state dimension

Independent of Phase, the system has an **income state**:

- **Active** — scheduled withdrawals execute per the active phase's
  withdrawal calculation.
- **Paused** — scheduled withdrawals are zero. All other system
  behavior continues normally:
  - rebalancing
  - CB state machine
  - SGOV refill (subject to its own gates)
  - cash buffer maintenance
  - alerts
  - **annual reviews including freeze evaluation and buffer/refill
    target recomputation** (§7.6)

Income state is controlled by the STOP INCOME token (§10), which has
AND semantics across both CL260 boxes:

- Both boxes have STOP INCOME inserted → **Paused**
- Both boxes have STOP INCOME removed → **Active**
- Mismatch → hold previous state, alert on persistent mismatch

The STOP INCOME mechanism is meaningful in **Phase 1** and **Phase 3**
(the income-producing phases). In **Phase 2**, the token is a no-op
because there is no scheduled income to pause; state changes are
logged for audit but produce no behavior change.

When income resumes after a pause, the schedule continues as if it
had never paused. Specifically, the resumed monthly amount reflects
calendar-time elapsed since trigger, including any inflation freezes
recorded in `frozen_years` during the pause:

```
scheduled_monthly = I_0 × (1 + cpi)^n_raises_applied
```

where `n_raises_applied` counts non-frozen years from `trigger_year+1`
through the resume year (inclusive on both ends, per §3.13).

Pausing does not freeze the schedule; it merely zeroes the actual
withdrawal during the paused period. Freezes accumulate independently
based on CB1+ activity during the paused period, evaluated by the
annual review on its normal Jan 15 schedule.

**Worked example.** Phase 3 triggers in January 2027 with
`I_0 = $4,000/mo`, `cpi = 0.04`:

- January 2028: STOP INCOME inserted. Last paid month was December 2027
  at $4,000/mo.
- Pause continues through 2028, 2029, 2030, 2031.
- 2030 has a significant market drawdown. CB1+ is active for >30 days
  during 2030. The January 2031 annual review evaluates 2030's CB-day
  count and freezes 2031's CPI raise. `frozen_years` becomes `[2031]`.
- January 2032: STOP INCOME removed. Income resumes.

Resumed monthly calculation:
- Calendar years from `trigger_year+1 = 2028` through `resume_year = 2032`:
  `[2028, 2029, 2030, 2031, 2032]` = 5 years.
- Frozen years in that range: `[2031]` = 1.
- `n_raises_applied = 5 - 1 = 4`.
- `scheduled_monthly = $4,000 × (1.04)^4 = $4,679/mo`.

The resumed amount is **not** $4,866 (which would be 5 raises with no
freeze accounting), and **not** $4,000 (which would treat the pause as
freezing the schedule). The pause defers payment of income, not
accumulation of the schedule. The survivor's real purchasing power is
preserved across pauses — her cost of living continued to inflate
during the pause, and the resumed income reflects that.

### 4.5 Phase-aware CB and review behavior

| Phase | CB framework active? | Annual review on Jan 15? | Freeze applies? | Lookback signal used for |
|---|---|---|---|---|
| Phase 1 | Yes | Yes | Yes (3% raise) | Rebalance gating, withdrawal cascade triggers, annual freeze evaluation, buffer/refill target recompute |
| Phase 2 | No | Yes (refill recompute only — but moot, refill rate frozen) | N/A (no scheduled income) | Phase 2 opportunistic swing trigger only |
| Phase 3 | Yes | Yes | Yes (4% raise) | Rebalance gating, withdrawal cascade triggers, annual freeze evaluation, buffer/refill target recompute |

The CB *state machine itself* is unchanged across Phase 1 and Phase 3;
only the parameters of the schedule_state differ.

---

## §5. Synthetic Growth Lookback Signal

The Synthetic Growth Lookback is the single signal that drives the
Circuit Breaker state machine. It answers one question, evaluated each
weekly cycle:

> **By what fraction has the synthetic Growth-bucket price index
> changed over the configured lookback window, relative to its value
> at the start of the window?**

### 5.1 Design properties

The signal is constructed to satisfy these properties:

- **Flow-immune.** Adding or removing shares (via rebalancing,
  dividends, withdrawals) has zero effect on the signal. Only price
  changes move it. This prevents the system's own actions from
  contaminating the signal that governs them.
- **Stateless (computationally).** The signal is computed from raw
  price data files at every cycle. No persisted index file, no
  carryover state, no cold-start period. The signal at any historical
  date can be re-derived identically from the price data available at
  that date. (The persistent state file does retain the most recent
  signal value and timestamp for staleness reporting only — not for
  transition logic.)
- **Allocation-agnostic in construction.** The composite is
  equal-weighted across Growth symbols regardless of the operator's
  actual portfolio weights. The signal measures *market conditions in
  the Growth asset class*, not *the operator's specific Growth P&L*.
- **Interpretable.** The output is a simple fractional change over a
  named window. "-0.10" means the index is 10% below where it was 6
  months ago. No log-transformation, annualization, or smoothing
  obscures the meaning.
- **Deterministic.** Given identical price files, the signal returns
  identical values. No randomness, no time-of-day dependency.

### 5.2 Inputs

| Input | Default value | Configurable | Notes |
|---|---|---|---|
| Growth symbols | FBCG, AVUV | Yes (via allowlist) | Must be ≥ 1 symbol |
| Bar cadence | Weekly | No | Matches the system's primary cycle cadence |
| Window length | 26 weekly bars (~6 months) | Yes (`lookback_window_weeks`) | Stored as bar count, not days, to avoid weekend/holiday ambiguity |
| Price source | Per-symbol Adj Close from data files | No | Local files in `C:\portfolio\data\` (§9.2) |
| Staleness tolerance | 14 days | Yes (`lookback_max_staleness_days`) | Latest bar in any symbol's file must be within this many days of "now" |
| Coverage minimum | 80% of expected bars | Yes (`lookback_min_bar_coverage_rate`) | After date alignment, surviving bars must cover at least this fraction of the window |

### 5.3 Algorithm

#### 5.3.1 Step 1: Load and align price data

For each Growth symbol, load the price file and extract the most
recent N+1 weekly Adj Close bars, where N = `lookback_window_weeks`. Build a
date-keyed map of per-symbol prices:

```
date_map[date][symbol] = adj_close_price
```

Retain only **dates on which every Growth symbol has a price**. A
missing print for any symbol on a given date drops that date from
the window. Sort surviving dates ascending.

#### 5.3.2 Step 2: Staleness and coverage gates

Apply two availability checks:

- **Staleness check:** the latest surviving date must be within
  `lookback_max_staleness_days` of "now." If not, return UNAVAILABLE.
- **Coverage check:** the count of surviving dates must be at least
  `ceil((N+1) × min_bar_coverage_pct)`. If not, return UNAVAILABLE.

UNAVAILABLE is a distinct sentinel value, distinguishable from any
numerical signal value (including 0).

#### 5.3.3 Step 3: Compute per-bar composite returns

For each adjacent pair of surviving dates (t-1, t) in the window:

For each Growth symbol s, compute the per-symbol simple return:

```
r[s, t] = (price[s, t] / price[s, t-1]) - 1
```

If `price[s, t-1]` is non-positive for any symbol at bar t, **skip
the entire bar** across all symbols (do not partial-average). Log a
warning. This is a correctness guard: a partial set of symbols would
silently change the composite weighting.

If **all** symbols' per-symbol returns computed cleanly, compute the
equal-weighted composite return for that bar:

```
r_composite[t] = (1 / N_symbols) × Σ r[s, t]    for s in Growth symbols
```

#### 5.3.4 Step 4: Compound into a level series

Initialize the level series at an arbitrary anchor:

```
L[0] = Decimal("100.0")
```

For each bar t with a valid composite return:

```
L[t] = L[t-1] × (Decimal("1") + r_composite[t])
```

The choice of 100.0 is presentational; the signal output (a ratio) is
unaffected by the anchor value.

#### 5.3.5 Step 5: Compute the signal

```
signal = (L[last] - L[0]) / L[0]
```

This is the **point-to-point fractional change** of the synthetic
index over the window. Output as a Decimal per §2.8.

### 5.4 Output specification

The signal returns one of:

- A **Decimal** representing fractional change (e.g., `Decimal("-0.0750")`
  means the index has fallen 7.5% over the window).
- **UNAVAILABLE** sentinel, when staleness or coverage gates fail.

Sign convention:
- **Negative** = Growth bucket has fallen since the start of the window.
- **Zero** = unchanged.
- **Positive** = Growth bucket has risen.

The signal is **never** clipped, smoothed, or transformed.
Consumers (the CB state machine) apply their own thresholds and
confirmation logic.

### 5.5 Pseudocode

```
function synthetic_growth_lookback(
        growth_symbols,
        lookback_weeks = 26,
        max_staleness_days = 14,
        min_bar_coverage_pct = 0.80,
        as_of = today
    ):

    if growth_symbols is empty:
        return UNAVAILABLE

    # --- Step 1: Load and align ---
    bars_per_symbol = {}
    for s in growth_symbols:
        bars = load_weekly_adj_close(s, count = lookback_weeks + 1)
        if bars is empty:
            return UNAVAILABLE
        bars_per_symbol[s] = bars

    date_map = {}
    for s, bars in bars_per_symbol.items():
        for bar in bars:
            date_map[bar.date][s] = bar.adj_close

    aligned_dates = sorted(
        d for d in date_map.keys()
        if all(s in date_map[d] for s in growth_symbols)
    )

    # --- Step 2: Gates ---
    if (as_of - aligned_dates[last]).days > max_staleness_days:
        return UNAVAILABLE

    expected_bars = lookback_weeks + 1
    minimum_bars  = ceil(expected_bars * min_bar_coverage_pct)
    if length(aligned_dates) < minimum_bars:
        return UNAVAILABLE

    # --- Step 3: Per-bar composite returns ---
    N = length(growth_symbols)
    composite_returns = []

    for t in 1 .. length(aligned_dates) - 1:
        d_prev = aligned_dates[t - 1]
        d_curr = aligned_dates[t]

        per_symbol = []
        bar_valid = true
        for s in growth_symbols:
            p_prev = Decimal(date_map[d_prev][s])
            p_curr = Decimal(date_map[d_curr][s])
            if p_prev <= 0:
                log_warning("non-positive prior price for " + s + " at " + d_curr)
                bar_valid = false
                break
            per_symbol.append((p_curr / p_prev) - Decimal("1"))

        if bar_valid:
            composite_returns.append(sum(per_symbol) / Decimal(N))

    if composite_returns is empty:
        return UNAVAILABLE

    # --- Step 4: Compound ---
    levels = [Decimal("100.0")]
    for r in composite_returns:
        levels.append(levels[last] * (Decimal("1") + r))

    # --- Step 5: Signal ---
    return (levels[last] - levels[0]) / levels[0]
```

### 5.6 Worked example

Suppose Growth = {FBCG, AVUV}, `lookback_weeks = 4` (smaller for
illustration), and these aligned weekly bars:

| Date       | FBCG  | AVUV  | Per-symbol returns                  | Composite return | Level |
|------------|------:|------:|-------------------------------------|-----------------:|------:|
| 2026-04-04 | 100.00| 50.00 | (anchor)                            |              n/a | 100.00 |
| 2026-04-11 | 102.00| 50.50 | FBCG: +0.0200, AVUV: +0.0100        |          +0.0150 | 101.50 |
| 2026-04-18 | 101.00| 50.00 | FBCG: -0.0098, AVUV: -0.0099        |          -0.0099 | 100.50 |
| 2026-04-25 |  95.00| 48.00 | FBCG: -0.0594, AVUV: -0.0400        |          -0.0497 |  95.51 |
| 2026-05-02 |  93.00| 47.50 | FBCG: -0.0211, AVUV: -0.0104        |          -0.0157 |  94.01 |

Signal output: `(94.01 − 100.00) / 100.00 = -0.0599`

Interpretation: the synthetic Growth index has fallen ~6.0% over the
4-week window. CB1 (threshold -10%) would NOT activate; the system
would remain in CB_INACTIVE.

### 5.7 Properties and non-properties

**The signal IS:**
- Determined entirely by Growth symbol price data and configuration.
- Recomputed from scratch each cycle.
- Unaffected by the operator's actual portfolio positions, withdrawals,
  rebalancing trades, or dividend reinvestments.
- A pure function of (date, price files, configuration).

**The signal IS NOT:**
- An estimate of the operator's portfolio P&L.
- An attempt to predict future returns.
- A trading signal in itself (it has no buy/sell semantics; it only
  characterizes Growth-bucket conditions).
- Smoothed, filtered, or trend-fit. (Smoothing happens downstream in
  the CB confirmation window logic, not here.)
- Annualized. A -0.10 over 6 months is reported as -0.10, not as a
  -20% annualized rate.

### 5.8 Failure modes

| Failure | Detection | Response |
|---|---|---|
| Price file missing | Load step | UNAVAILABLE; alert operator (data refresh needed) |
| Price file stale | Staleness gate | UNAVAILABLE; alert operator |
| Insufficient aligned bars | Coverage gate | UNAVAILABLE; alert if persistent (>1 cycle) |
| Non-positive prior price for a symbol | Per-bar guard | Skip bar, log warning, continue |
| All bars skipped | Empty composite_returns check | UNAVAILABLE |
| Symbol added/removed from Growth allowlist mid-history | Configuration change | Recomputed from scratch (stateless); no migration needed |

When the signal is UNAVAILABLE, the CB state machine **does not
transition** — current CB state holds. The system does not "guess"
or use a stale value.

---

## §6. State Machine

This section formalizes every state the system maintains, every
transition between states, and the rules governing interaction
between independent state machines. The system maintains **three
parallel state machines** that together describe its complete
operating mode at any instant:

1. **Phase machine** — Phase 1 / Phase 2 / Phase 3
2. **Income state machine** — Active / Paused
3. **CB machine** — CB_INACTIVE / CB1 / CB2

These machines are **mostly independent** but with defined interactions
specified below. The complete operating state at any moment is the
3-tuple:

```
(phase, income_state, cb_state)
```

Master/slave coordination role (MASTER / SLAVE_SLEEPING /
SLAVE_PROMOTION_PENDING / STARTING) is per-box infrastructure and is
**not** part of the operating-mode tuple — two boxes with different
roles run identical strategy decisions.

### 6.1 Phase machine

States: `PHASE_1`, `PHASE_2`, `PHASE_3`.

Transitions:

| From | To | Trigger | Reversible? |
|---|---|---|---|
| PHASE_1 | PHASE_2 | Calendar date 2035-01-01 reached | No |
| PHASE_1 | PHASE_3 | Phase 3 token activation, 24-hour grace elapsed | No |
| PHASE_2 | PHASE_3 | Phase 3 token activation, 24-hour grace elapsed | No |
| PHASE_3 | (none) | (terminal) | N/A |

There is no PHASE_2 → PHASE_1 transition. There is no PHASE_3 →
anything transition. Phase progression is monotone.

Phase 3 transitions latch the phase indicator at grace expiry. The
transition cycle (asset reallocation, I_0 calculation) executes on
the next weekly cycle after latch, regardless of CB state
(§4.2, §10.6.3).

Phase transitions execute the mechanic specified in §4.3.

### 6.2 Income state machine

States: `ACTIVE`, `PAUSED`.

Transitions:

| From | To | Trigger | Reversible? |
|---|---|---|---|
| ACTIVE | PAUSED | Both boxes report STOP INCOME inserted (AND semantics) | Yes |
| PAUSED | ACTIVE | Both boxes report STOP INCOME removed (AND semantics) | Yes |

Mismatch behavior: if the two boxes disagree on STOP INCOME state,
the income state machine **holds its previous state** until the boxes
agree. See §10 for token mechanics and mismatch alerting.

Phase interaction: in `PHASE_2`, transitions of the income state
machine are recorded for audit but produce no behavioral change
(no scheduled income exists to pause or resume). The state machine
itself still tracks transitions correctly so that if Phase 3
activates while STOP INCOME is inserted, the system enters Phase 3
already in PAUSED state.

### 6.3 CB machine

States: `CB_INACTIVE`, `CB1`, `CB2`.

The CB machine is **only active in Phase 1 and Phase 3**. In Phase 2,
the CB machine is suspended (state held at CB_INACTIVE, no transitions
evaluated, no behavior dependent on it).

Per-cycle evaluation (when active):

1. Compute current Synthetic Growth Lookback signal (§5).
2. Read current core portfolio value and core FI bucket value from
   the input refresh (§6.5 step 1).
3. Evaluate the three CB2 entry conditions and the signal-based
   CB1 condition; apply transition rules below.
4. If signal is UNAVAILABLE: signal-based transitions do not
   evaluate this cycle (resource-based transitions continue to
   evaluate, as they do not depend on the signal).

#### 6.3.1 CB2 entry conditions

CB2 has three independent entry conditions. Each requires
`confirmation_window_weeks` (default 2) consecutive weekly cycles
of the condition holding before CB2 is entered. The three conditions
are evaluated independently — any one of them, after its own
confirmation, triggers CB2 entry.

| Entry path | Condition | Confirmation |
|---|---|---|
| Signal | Signal ≤ `cb2_threshold_rate` (default -0.20) | 2 consecutive weekly cycles |
| Portfolio-low | Core portfolio < `max(portfolio_low_threshold_dollars, portfolio_low_threshold_months × current_monthly_withdrawal)` | 2 consecutive weekly cycles |
| FI-low | Core FI < `fi_low_threshold_months × current_monthly_withdrawal` | 2 consecutive weekly cycles |

A "consecutive weekly cycle" means the most recent N successful
evaluations all met the threshold. For signal-based confirmation,
cycles where the signal was UNAVAILABLE neither advance nor reset
the count. Resource-based conditions evaluate the broker-reported
portfolio values, which are always available when the broker
connection succeeded (cycles that failed at input refresh per §6.5
step 1 do not evaluate any CB transitions).

Each entry path independently maintains a pending-confirmation
counter. The counter increments when the condition holds and resets
to zero when the condition fails to hold. CB2 entry fires on the
first cycle where any one counter reaches the threshold.

#### 6.3.2 CB1 entry and CB1↔CB_INACTIVE transitions

CB1 is signal-based only. Resource conditions trigger CB2 directly,
bypassing CB1, because resource-triggered conditions want SGOV
cascade rather than FI-only sourcing.

| From | To | Condition | Confirmation |
|---|---|---|---|
| CB_INACTIVE | CB1 | Signal ≤ `cb1_threshold_rate` (default -0.10) | 2 consecutive weekly cycles |
| CB1 | CB_INACTIVE | Signal ≥ `cb1_threshold_rate` + `cb1_recovery_buffer_rate` (default -0.08) | 2 consecutive weekly cycles |

The 2-point recovery buffer (`cb1_recovery_buffer_rate`, default
0.02) is hysteresis preventing CB1 from oscillating when the signal
sits near -10%.

#### 6.3.3 CB2 exit conditions

CB2 exits only when **ALL active entry conditions have cleared**.
"Active" means the condition was holding at the time of CB2 entry
or has held at any point during the current CB2 episode. Each
condition has its own clearing threshold with a recovery buffer:

| Entry path | Clearing condition | Confirmation |
|---|---|---|
| Signal | Signal ≥ `cb2_threshold_rate` + `cb2_recovery_buffer_rate` (default -0.15) | 2 consecutive weekly cycles |
| Portfolio-low | Core portfolio ≥ trigger threshold × (1 + `portfolio_low_recovery_buffer_rate`) (default 1.10) | 2 consecutive weekly cycles |
| FI-low | Core FI ≥ trigger threshold × (1 + `fi_low_recovery_buffer_rate`) (default 1.10) | 2 consecutive weekly cycles |

CB2 transitions out only when every condition that has been active
during this CB2 episode is currently clear. The exit target depends
on the signal:

- If signal ≥ `cb1_threshold_rate` + `cb1_recovery_buffer_rate`
  (default -0.08) for 2 consecutive cycles: CB2 → CB_INACTIVE.
- Else if signal ≥ `cb2_threshold_rate` + `cb2_recovery_buffer_rate`
  (default -0.15) for 2 consecutive cycles AND signal still ≤
  `cb1_threshold_rate` (default -0.10): CB2 → CB1.

The signal-band check applies even when CB2 was entered solely via
a resource trigger — once in CB2, exit semantics are uniform. In
the common resource-triggered case where the signal is nominal, the
signal-band check is automatically satisfied (signal well above
-0.08), and exit goes directly to CB_INACTIVE the moment the
resource condition clears.

#### 6.3.4 Tracking active entry conditions

The state file persists a `cb2_entry_conditions` set containing the
identifiers of conditions that have triggered during the current
CB2 episode: any subset of `{signal, portfolio_low, fi_low}`. The
set is initialized when CB2 is entered (containing the triggering
condition) and accumulates additional conditions if they
independently trigger during the same CB2 episode. The set is
cleared on CB2 exit.

This tracking allows CB2 exit to require clearance of every
condition that has been active, not merely the conditions currently
active at the moment of evaluation. Without this, a transient
signal recovery during a resource-triggered CB2 episode could
prematurely route exit through a path that ignores the still-active
resource condition.

#### 6.3.5 CB1 → CB2 timer transition

The CB1 → CB2 transition has two trigger paths:

1. **Signal-based path.** Signal ≤ `cb2_threshold_rate` for
   `confirmation_window_weeks` consecutive weekly cycles. Standard
   pending-transition logic applies (§6.3.1).

2. **Timer-based path.** When CB1 has been continuously active for
   ≥ `cb1_to_cb2_timer_days` (default 90), transition to CB2 fires
   immediately on the next cycle. No additional signal check; no
   confirmation window — the timer itself is the trigger. This
   path captures the "long grinding drawdown" scenario where the
   signal sits in CB1 territory for months without ever falling
   deep enough to trip the cb2 signal threshold.

**Timer mechanics.** The CB1-active timer is `now -
cb1_active_timer_started_at`. The timer resets to zero whenever
CB1 is freshly entered from CB_INACTIVE (does not accumulate
across episodes). The timer is cleared when state becomes
CB_INACTIVE or CB2.

**Persistence across phase transitions.** At Phase 1 → Phase 3
transition, the CB1 timer continues accumulating. The timer
measures market depression duration, which is a property of
market conditions independent of phase.

When the timer fires, `signal` is added to the
`cb2_entry_conditions` set (the timer is a signal-derived
mechanism, even though no fresh signal confirmation occurs).

#### 6.3.6 Resource-condition evaluation in Phase 3 latched-but-pending window

Per I15, the Phase 3 latched-but-pending window (phase = PHASE_3
but `schedule_state.phase3` is null) is a transition-pending state
in which the decision layer suppresses withdrawal and rebalance
work. Resource-based CB2 evaluation also suppresses in this window:
`current_monthly_withdrawal` is undefined, so neither the
Portfolio-low nor FI-low threshold can be computed. The signal-based
CB2 path continues to evaluate normally (signal depends on price
data, not schedule state).

The Phase 3 transition cycle initializes `schedule_state.phase3`,
making `current_monthly_withdrawal` defined. Resource-based CB2
evaluation begins on the cycle after the transition cycle (the
first "normal" Phase 3 cycle). If the survivor inherits a small
portfolio, Portfolio-low triggers on that cycle, the 2-week
confirmation accrues, and CB2 entry fires on the next-after-next
cycle (typically 7 days later). The first month's withdrawal may
execute under CB_INACTIVE sourcing if it falls in the
pre-confirmation window; this is bounded by the cash buffer (which
holds one month of withdrawal) and the SGOV buffer (which holds 24
months).

### 6.4 Low-resource conditions

Low-resource conditions (small core portfolio, depleted FI bucket)
are CB2 entry triggers, specified in §6.3.1. They are not
independent state machines; they have no behavior beyond their
contribution to the CB machine. Alerts fire on CB2 entry and exit
per the `cb_transition` alert (with trigger reason in the body); the
alert catalog is in §12.6.

The thresholds and recovery buffers are configured in ruleset.yaml
per §2.3.1.

### 6.5 Cycle evaluation order

Each cycle evaluates state machines in this order. Order matters
because some evaluations depend on outputs of earlier ones.

1. **Refresh inputs.** Account positions, prices, broker connectivity,
   token states. (Daily-token cycles refresh tokens only; see §6.5.1.)
2. **Phase machine.** Check for calendar transition or token
   activation. Execute transition mechanic (latch immediately at
   grace expiry; transition cycle runs on next weekly cycle
   regardless of cascade state, per §4.2). Subsequent steps see
   the new phase indicator.
3. **Income state machine.** Read STOP INCOME tokens; apply AND
   semantics; transition Income state if both boxes agree on a new
   value. (Mismatch holds previous state.) If Income state
   transitioned this cycle, queue an ACHScheduleUpdate plan entry
   for the action layer (§7.9).
4. **Synthetic Growth Lookback signal.** Compute per §5. May return
   UNAVAILABLE.
5. **CB machine.** Evaluate the three CB2 entry conditions and the
   CB1 condition per §6.3. Signal-based conditions skip evaluation
   if signal is UNAVAILABLE; resource-based conditions continue to
   evaluate. Update pending-confirmation counters and execute any
   transitions whose confirmation thresholds are met. Update the
   `cb2_entry_conditions` set on entry; clear it on exit.
6. **CB1 → CB2 timer transition.** If currently in CB1, increment
   timer; check threshold (§6.3.5). If not in CB1, reset timer to
   zero.
7. **Annual review evaluations** (annual-review cycle only — Jan 15).
   Run §7.6: freeze evaluation, schedule_state update, buffer-target
   recompute, refill-rate recompute, cash-target recompute. In
   Phase 2, this step is a no-op (all targets are constants or
   frozen-forward; see §7.6.3).
8. **Decision layer (§7).** Generate the cycle's plan as a pure
   function of the resulting state.
9. **Action layer.** Execute the plan.
10. **Persistence.** Record state transitions, decisions, and actions
    to logs and state file.
11. **Alerts.** Dispatch any alerts triggered by transitions or actions.

A failure at step 1 (cannot refresh inputs) aborts the cycle entirely.
A failure at step 4 (signal UNAVAILABLE) does not abort but constrains
later steps — signal-based CB transitions skip this cycle while
resource-based ones continue. A failure at step 9 (action execution)
is logged, alerts, and the cycle ends without persisting partial state
to the operating state file (cycle log retains the partial-execution
record).

**Note on annual-review-day ordering.** On Jan 15 annual review
cycles, step 5's resource-based CB2 evaluation uses the prior year's
`current_monthly_withdrawal` value (the new year's value is set in
step 7 and applies to subsequent cycles). This is a deliberate
one-cycle lag — CB2 resource triggers reflect last-year's withdrawal
rate on the transition day, and shift to current-year's rate starting
the next weekly cycle. The bounded risk is one cycle's worth of
threshold mismatch, which at typical year-over-year inflation
adjustments (~3-4%) shifts the threshold by ~$2K — well within the
hysteresis buffer.

#### 6.5.1 Cycle types and per-type scope

Two cycle types differ in which §6.5 steps they execute:

| Step | weekly | daily-token |
|---|:---:|:---:|
| 1. Refresh inputs (positions, prices, tokens) | full | tokens only |
| 2. Phase machine | yes | — |
| 3. Income state machine | yes | yes |
| 4. Lookback signal | yes | — |
| 5. CB machine (incl. resource conditions) | yes | — |
| 6. CB1 → CB2 timer evaluation | yes | — |
| 7. Annual review (date-gated: Jan 15) | yes | — |
| 8. Decision layer | yes | minimal (alert-only entries) |
| 9. Action layer | yes | yes (alerts only) |
| 10. Persistence | yes | yes (token state to shared store) |
| 11. Alerts | yes | yes |

**daily-token** cycles are deliberately minimal — they do not query
the broker, compute the signal, or run the decision layer. Their
purpose is read-tokens / write-shared-state / alert-on-mismatch. This
keeps daily token monitoring lightweight and resilient to broker or
data-file issues.

**weekly** cycles run on the configured `cycle_schedule` (default
Wed 10:00 ET) and are the only place portfolio analysis, CB
evaluation, rebalancing, deployment, withdrawals, annual review, or
Phase 2 reallocation happen. Several steps within the weekly cycle
are **date-gated** — they only execute on certain dates:

- **Monthly withdrawal step.** Executes on the weekly cycle that is
  the latest Wednesday ≤ (4 business days before the 15th of the
  current month). This is the same weekly cycle in all months; in
  months where the 15th is later in the week, the withdrawal may
  place its SELL up to 10 days early. The SELL settles T+1 and the
  cash sits in cash until the ACH pulls on the 15th — the few extra
  days in cash carry negligible cost.
- **Annual review step.** Executes on the weekly cycle on or after
  the configured `annual_review_date` (default 01-15).
- **Phase 2 semi-annual reallocation step.** Executes on the weekly
  cycle on or after each `phase2_reallocation_dates` entry (default
  Jan 15 and Jul 15) when phase is PHASE_2.
- **Pre-transition validation step.** Executes on the last weekly
  cycle falling at least 5 trading days before a scheduled Phase 1 →
  Phase 2 transition per §4.3.

**Cycle-type collisions.** The only cycle collision is daily-token
vs. weekly on Wednesdays. They run at different times (daily-token
early in day, weekly at the cycle_schedule time), so there is no
actual concurrency. Each cycle reads broker/token state fresh at
its start and writes state atomically at its end (§9.4.1).

### 6.6 Composite operating mode

The complete **effective operating mode** at any instant is the
3-tuple:

`(phase, income_state, cb_state)`

All three elements are independent state-machine values.
Master/slave coordination role (MASTER / SLAVE_SLEEPING /
SLAVE_PROMOTION_PENDING / STARTING) is per-box infrastructure and is
**not** part of the operating-mode tuple — two boxes with different
roles run identical strategy decisions.

Most tuple combinations are reachable; a few are precluded by phase
rules (e.g., CB1 and CB2 are unreachable in Phase 2; income state
is observationally inert in Phase 2). The behaviorally-meaningful
aspects of the operating mode are summarized below.

**Active subsystems by phase:**

| Subsystem | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Scheduled withdrawals | Yes (when ACTIVE) | No | Yes (when ACTIVE) |
| 5/25 rebalancing | Yes (gated by CB) | No | Yes (gated by CB) |
| Opportunistic rebalance | No | Yes (single threshold) | No |
| Synthetic Growth Lookback | Yes | Yes | Yes |
| CB machine | Yes | No (held CB_INACTIVE) | Yes |
| SGOV buffer maintenance | Yes (target dynamic) | Yes (refill-only, target frozen) | Yes (target dynamic) |
| Cash buffer maintenance | Yes (target dynamic) | Yes (target $1000 fixed) | Yes (target dynamic) |
| Annual review (Jan 15) | Yes (freeze + recomputes) | Yes (no-op — all targets are constants or frozen-forward) | Yes (freeze + recomputes) |

**Behavioral effects of CB state (Phase 1 and Phase 3):**

| CB state | Rebalancing | Withdrawal source | SGOV refill | Large cash deployment |
|---|---|---|---|---|
| CB_INACTIVE | Active | Most-overweight core, then proportional FI | Active when not in delay | Target-weight proportional |
| CB1 | Suspended | Most-overweight FI, then proportional FI (Growth never sold) | Active when not in delay | Target-weight proportional |
| CB2 (signal active) | Suspended | Cascade (SGOV → FI → Growth) | Suspended | Defensive (SGOV-first, then FI-only) |
| CB2 (resource only) | Suspended | Cascade (SGOV → FI → Growth) | Suspended | Target-weight proportional |
| Income state PAUSED | (unchanged from CB row) | No withdrawal scheduled | (unchanged from CB row) | (unchanged from CB row) |

"CB2 (signal active)" means the signal-based condition is currently
holding (signal ≤ `cb2_threshold_rate`), regardless of whether
resource conditions are also holding. "CB2 (resource only)" means
CB2 is active solely due to Portfolio-low or FI-low conditions
without the signal condition currently holding. The distinction
affects only the large cash deployment algorithm (§7.7.1) — all
other CB2 behavior is uniform across entry causes.

The Income-state PAUSED row is intentionally orthogonal: pausing
income does not change CB state, rebalancing, refill behavior, or
cash deployment behavior. The system continues all non-withdrawal
operations normally, **including the annual review's freeze
evaluation and recomputes**.

### 6.7 State persistence

The following state must persist across system restarts. Stored in
the local state file on the master's pSLC SSD (JSON, atomic-write),
replicated to the slave's local disk via the daily rsync per §9.4.2:

- **Phase machine:** Current phase. Phase 3 grace-window timer (if
  active).
- **Income state:** Current ACTIVE / PAUSED.
- **CB machine:** Current CB state. CB1-active timer (for timer-based
  CB1 → CB2 transition). Per-condition pending-confirmation counters
  for each of the four entry/exit paths (CB1 signal entry, CB1 signal
  exit, CB2 signal, CB2 portfolio-low, CB2 fi-low — each tracked
  independently per §6.3.1 and §6.3.3).
- **CB2 entry conditions set:** `cb2_entry_conditions`, a subset of
  `{signal, portfolio_low, fi_low}` containing every condition that
  has been active during the current CB2 episode. Updated on entry
  and whenever an additional condition triggers during the episode;
  cleared on CB2 exit. See §6.3.4.
- **Lookback signal:** Last successful signal value and timestamp
  (for staleness reporting only — not for transition logic).
- **CB transition log:** Append-only list of `(timestamp, from_state,
  to_state, trigger_reason, cycle_id)` entries. Used by the annual
  CPI freeze evaluation (§7.6) to compute days-in-CB1+ over the prior
  calendar year. The `trigger_reason` field records which CB2 entry
  path fired (`signal`, `portfolio_low`, `fi_low`, or `cb1_timer`)
  for entries; for exits, it records the conditions that cleared.
  Retained indefinitely (small data volume — typically <100 entries
  per year).
- **Schedule state:** `(I_0, trigger_year, cpi, frozen_years)` per
  §3.13. Phase 1 and Phase 3 each have their own instance (Phase 1
  initialized at program start; Phase 3 initialized at trigger).
- **Annual review record:** For each year, a record of
  `(year, frozen: bool, cb1_days, cb2_days, decided_at_timestamp,
  buffer_target, refill_rate, cash_target)`. Append-only. `cb2_days`
  counts calendar days CB2 was active for any part of the day,
  regardless of which entry path triggered.
- **Phase 2 opportunistic state:** Current state of the swing
  (`steady` / `deployed`). Initialized to `steady` at Phase 2
  entry. Two pending-confirmation counters tracked independently:
  `deploy_pending_weeks` (cycles consecutively at signal ≤
  `phase2_opportunistic_trigger_rate` while in `steady`) and
  `recover_pending_weeks` (cycles consecutively at signal ≥
  `phase2_opportunistic_recovery_rate` while in `deployed`). Both
  counters are reset to 0 whenever the signal falls back out of
  range, and discarded at Phase 3 latch. The binary state itself
  becomes moot at Phase 3 latch (Phase 2 is no longer the
  operating regime); no explicit clear is required.
- **Buffer & cash targets:** Current `buffer_target_dollars`,
  `monthly_refill_rate_dollars`, `cash_target_dollars`. Recomputed at
  annual review; recomputed values persisted here for cycle reads.
- **Operational pause:** `operational_pause` structure (paused bool,
  pause_reason str, pause_started_at timestamp,
  consecutive_pause_count int). Default
  `{paused: false, pause_reason: null, pause_started_at: null, consecutive_pause_count: 0}`.
  See §11.3.
- **Withdrawal capacity:** `withdrawal_capacity_exhausted` (boolean,
  default false). Indefinite-halt flag for cascade-exhausted state;
  see §11.3.2.
- **Master/slave coordination:** `master_box_id`,
  `last_master_write_timestamp`, `master_ipv4_last_octet`,
  `last_cycle_clientid` in the local state file (per §9.4.2,
  §9.4.3, and §15.6, replicated to peer via rsync); `role`
  (MASTER / SLAVE_SLEEPING / SLAVE_PROMOTION_PENDING / STARTING)
  in per-box local-only operational state. `last_cycle_clientid`
  is the broker `client_id` (e.g., 11 or 12) that the most recent
  cycle ran with; two distinct values appearing within a 24h
  window triggers the split-brain detector (§15.6, §11.2).
- **Token observation file** (per-box, `token_observation.json`,
  separate from the operating state file): each box's most recent
  `(box_id, timestamp, phase3_count, stopincome_count, status)`
  observation from its daily-token cycle (§10.3). Slave's observation
  file is replicated to master via dedicated slave→master rsync.
  Distinct from the operating state file precisely because slave
  writes it; the operating state file remains master-write-only.
- **Cycle attempt file** (per-box, `cycle_attempt.json`, separate
  from the operating state file): the in-flight cycle's
  `cycle_uuid`, captured `decision_clock` (invariant I16),
  cycle_type, started_at, last_updated_at, is_complete flag, and
  append-only `placed_orders` log. Written multiple times per
  cycle (each successful broker order placement appends a record
  and rewrites the file atomically); finalized at cycle end with
  `is_complete=true`. Master-only; NOT replicated via rsync —
  cross-box duplicate-order detection is handled by the broker
  layer's `orderRef` mechanism (§15.5), not by replicating this
  file. On the next cycle's start, an existing file with
  `is_complete=false` indicates a restart of a previously-
  interrupted cycle; the cycle reuses the persisted `cycle_uuid`
  and `decision_clock`. See §15.11 for the full file format.

**State that does NOT persist** (recomputed each cycle from inputs):

- Account positions and values.
- Synthetic Growth Lookback signal value (recomputed each cycle from
  price files, per §5.1).
- Plan content. Plans are written to the cycle log (§12.2.1) for
  audit, not to the operating state file.
- Allocation drift values.
- Resource condition evaluations (Portfolio-low, FI-low). The
  *currently-holding* status is recomputed each cycle from broker
  positions; only the pending-confirmation counters and the
  cb2_entry_conditions set persist.

The state file contains *current state* only (target <10KB at any
moment). Append-only logs (CB transition log, annual review log) are
separate files with indefinite retention (§9.4.5). The operating
state file holds the current snapshot needed to resume operation;
the logs hold the history needed for audit and freeze-evaluation
backreference. The state file is written atomically at end of cycle
(after successful action execution and alerting). On startup, if
the state file is missing or corrupt, the system refuses to start
and alerts the operator (§11.2.3).

### 6.8 State transition alerts

Every state transition (Phase, Income, CB) generates an alert. Alert
types, severities, channels, and message templates are defined in
the alert catalog (§12.6). The principle: **transitions are
visible**. The operator should always be able to reconstruct what
happened by reading alerts in chronological order.

---

## §7. Decision Logic

This section specifies the decision layer: pure functions that
consume the operating state (§6) and produce a structured **Plan**
(§3.12) without performing any action. The action layer (§8)
executes the plan.

The decision layer's design philosophy is **decisions = data**:
every decision the system makes corresponds to a serializable Plan
entry. This makes decisions inspectable (you can log a Plan and read
exactly what the system intended), testable (you can assert on Plans
without executing them), and auditable (you can replay historical
decisions from inputs and Plans).

### 7.1 Top-level decision sequence

Each cycle, after state evaluation (§6.5 steps 1-7), the decision
layer runs in this order (§6.5 step 8). Each step may add zero or
more entries to the Plan being built.

1. **Phase transition decisions** — large rebalance, position
   liquidation if a phase change just occurred this cycle.
2. **Annual review decisions** (annual-review cycle only) — schedule
   updates, target recomputes (§7.6).
3. **Cash buffer decisions** — if cash is outside tolerance, plan a
   refill or drawdown (§7.7).
4. **Withdrawal decisions** — if scheduled and Income state is
   ACTIVE, plan the withdrawal (source per §7.3).
5. **SGOV buffer refill decisions** — if buffer is below target, in
   the active-refill window, and not blocked by CB state, plan a
   refill batch (§7.4).
6. **Rebalancing decisions** — if 5/25 thresholds are crossed and
   rebalancing is not blocked by CB state, plan rebalancing trades
   (§7.5).

The order matters because earlier decisions consume cash that later
decisions might also want. Cash buffer comes before withdrawal so
the cash bucket is at target when the withdrawal is sourced.
Withdrawal comes before refill so the cycle's outgoing money is
accounted for before deciding how much Growth to convert to SGOV.
Rebalancing comes last so it sees the post-flow allocation, not the
pre-flow allocation.

A cycle's final Plan may be empty (no actions needed this cycle) or
contain many entries. Empty plans are the most common outcome in
quiet markets.

### 7.2 Phase transition decisions

When Phase machine state changed this cycle (§6.5 step 2), or a
previously-latched Phase 3 transition is now executable (cascade
cleared), the decision layer plans the transition. The plan
reallocates positions between the outgoing phase's holdings and the
incoming phase's holdings, applying the **liquidation rule** per I13:

- A position appearing in the incoming phase's allowlist is
  rebalanced to its incoming target weight.
- A position NOT in the incoming phase's allowlist but possibly
  needed in a future-reachable phase is **drawn down to
  `position_residual_minimum_dollars`** (not liquidated).
- A position NOT in the incoming phase's allowlist AND not in any
  future-reachable phase's allowlist is **fully liquidated** (no
  residual required).

**Per-transition liquidation table:**

| Position | Phase 1 | Phase 2 | Phase 3 | Phase 1→2 action | Phase 1→3 action | Phase 2→3 action |
|---|---|---|---|---|---|---|
| FBCG | core (Growth) | core (Growth) | core (Growth) | rebalance | rebalance | rebalance |
| AVUV | core (Growth) | core (Growth) | core (Growth) | rebalance | rebalance | rebalance |
| PYLD | core (FI) | not used | core (FI) | drop to residual | rebalance | refill from residual |
| JPIE | core (FI) | not used | core (FI) | drop to residual | rebalance | refill from residual |
| GBIL | not used | core (FI) | not used | new position (BUY) | n/a | **fully liquidate** |
| SGOV | buffer | buffer | buffer | (continues) | (continues) | (continues) |

Rationale for liquidation decisions:

- **PYLD/JPIE held at residual through Phase 2** because Phase 3
  reactivates them (Phase 3 holdings = Phase 1 holdings,
  provisionally). Phase 3 is reachable from Phase 2.
- **GBIL fully liquidated at Phase 2 → Phase 3** because Phase 3 is
  terminal/latching (no transition out). GBIL appears in NO reachable
  future phase from Phase 3, so no reason to maintain the position.

#### 7.2.1 Phase 1 → Phase 2 plan

- SELL PYLD down to `position_residual_minimum_dollars`. Skipped
  if already at or below residual (no-op). The skip is recorded in
  the cycle log (§12.2.1) and surfaced in the `large_rebalance`
  alert's residual-exceptions section (§12.6).
- SELL JPIE down to `position_residual_minimum_dollars` (same
  residual-skip semantics).
- Compute Phase 2 target allocation against post-liquidation core
  total (FBCG + AVUV + GBIL = 100% of core, with PYLD + JPIE
  residuals excluded from core total).
- BUY GBIL to reach 10% of core (Phase 2 FI target).
- Rebalance FBCG and AVUV to their Phase 2 target weights (default
  per ruleset; sum to 90% of core).
- Mark plan with `large_rebalance` flag.

#### 7.2.2 Phase 1 → Phase 3 plan

- (PYLD, JPIE, FBCG, AVUV all remain in active core. GBIL is not
  present.)
- Rebalance all four positions to their Phase 3 target weights
  (default per ruleset, provisionally 25/25/25/25 — to be tuned
  via simulator per §14).
- Compute Phase 3 starting income `I_0` per §4.1.1 from current
  portfolio value at the moment the transition cycle executes (not
  at the moment of token-removal grace expiry — see §4.2).
- Initialize Phase 3 schedule_state: `(I_0, trigger_year, inflation_rate, [])`.
  Persist to durable state.
- Recompute Phase 3 buffer target = 24 × I_0; refill rate = target/12;
  cash target = I_0 + $1000. Persist.
- Mark plan with `large_rebalance` and `phase3_activation` flags.

#### 7.2.3 Phase 2 → Phase 3 plan

- SELL GBIL fully (no residual — Phase 3 latches, GBIL never used
  again).
- Refill PYLD and JPIE from residual to Phase 3 FI target weights,
  funded by GBIL liquidation proceeds and (if needed) Growth.
- Rebalance FBCG and AVUV to Phase 3 target weights.
- Compute Phase 3 starting income `I_0` and initialize schedule_state
  as in §7.2.2.
- Recompute Phase 3 buffer target, refill rate, and cash target as in
  §7.2.2. (Buffer dollars carry forward from Phase 2; the new target
  is what they're measured against.)
- Mark plan with `large_rebalance` and `phase3_activation` flags.

#### 7.2.4 Common rules for all phase transitions

- The transition cycle suppresses all other decision steps. The
  transition is the cycle. Withdrawals, refills, and rebalances
  for the new phase begin on the next cycle.
- Order of operations within the plan: liquidate or draw-to-residual
  first (frees cash), then reallocate from cash. Action layer
  responsibility to interleave if order dependencies require, but
  plan expresses logical sequence.
- All transitions trigger the `large_rebalance` alert.
- Pending CB or swing confirmations from the outgoing phase are
  discarded (§4.3 step 2).

### 7.3 Withdrawal decisions

A withdrawal is planned each cycle when:
- Phase is 1 or 3, AND
- Income state is ACTIVE, AND
- Today is the scheduled withdrawal day for the current month
  (monthly withdrawal step within the weekly cycle, per §6.5.1), AND
- `active_phase.schedule_state` is non-null (defends against the
  Phase 3 latch-vs-transition window per §4.3 and I15; see also
  D12).

If the scheduled day falls on a market holiday or weekend, the
withdrawal cycle shifts to the **next trading day**. The IBKR ACH
date should be configured to allow ≥2 trading days after the SELL
fills for settlement (§9.6.1).

#### 7.3.1 Withdrawal amount

For both Phase 1 and Phase 3:

```
scheduled_monthly = compute_scheduled_monthly(
                        current_year,
                        active_phase.schedule_state)
```

per §3.13. For Phase 3 only, additionally apply the per-month payment
ceilings per §4.1.1.2 — the actual withdrawal is the MIN of the
schedule, the portfolio-% ceiling, and the indexed dollar ceiling:

```
portfolio_ceiling = current_portfolio × phase3_monthly_payment_ceiling_rate / 12
dollar_ceiling    = phase3_dollar_ceiling_base_dollars × (1 + inflation_rate) ** (current_year - phase3_dollar_ceiling_base_year)
monthly_withdrawal = min(scheduled_monthly, portfolio_ceiling, dollar_ceiling)
```

If either ceiling binds, emit `monthly_payment_ceiling_bound` Notice
alert identifying which ceiling bound (portfolio-%, dollar, or both).
No other state is affected by the ceiling binding.

Phase 1 has no portfolio clamp; the `portfolio_low_alert` provides
operator warning at the depletion threshold, and the operational
pause framework (§11.3) plus residual-floor (`position_residual_minimum_dollars`)
prevent unsustainable sales.

The `current_portfolio` in the Phase 3 clamp is the core portfolio
value (Growth + FI; excludes buffer and cash).

#### 7.3.2 Withdrawal source

The source depends on the operating-mode tuple. There are three
sourcing rules:

**CB_INACTIVE sourcing** (CB_INACTIVE):

1. Compute current core portfolio value.
2. Compute target value for each core position from current totals
   and configured target weights.
3. Compute drift dollars: `drift[s] = current_value[s] - target_value[s]`.
4. Identify the most-overweight position: `argmax_s(drift[s])` where
   `drift[s] > 0`. Tie-break: larger position by current $ value.
5. If the most-overweight position has surplus ≥ withdrawal amount,
   plan a SELL of that single position for the withdrawal amount,
   clamped at the residual floor (I12).
6. If the most-overweight position has surplus < withdrawal amount,
   sell all surplus from it (clamped at residual floor); remaining
   withdrawal sourced **proportionally from FI buckets** per their
   current values, each FI SELL clamped at residual floor.
7. If overall sourcing is insufficient even after applying steps 1–6:
   this state is unreachable in correct operation — the FI-low and
   Portfolio-low CB2 entry paths (§6.3.1) would have triggered CB2
   and routed withdrawals through cascade instead. Reaching this
   branch indicates a bug in CB evaluation or threshold
   configuration. Abort cycle, set `operational_pause` with
   `pause_reason: "internal_consistency_violation"`, alert Critical.
   This branch is NOT eligible for 48h auto-resume — internal logic
   inconsistencies require operator review before resumption.

**CB1 sourcing** (CB1):

Withdrawals are sourced from the FI bucket only. Growth is never
sold for withdrawals during CB1 (I14).

1. Identify the most-overweight FI position by drift dollars (tie-break
   by larger position $ value).
2. If the most-overweight FI position has surplus ≥ withdrawal amount,
   plan a SELL of that single FI position for the full amount, clamped
   at residual.
3. If the most-overweight FI position has surplus < withdrawal amount,
   sell all surplus from it (clamped at residual); remaining demand
   sourced proportionally from remaining FI positions, each clamped at
   residual.
4. If FI bucket cannot meet demand even at residuals: this state is
   unreachable in correct operation — the FI-low CB2 entry path
   (§6.3.1) would have triggered CB2 and routed withdrawals through
   cascade. Reaching this branch indicates a bug in CB evaluation or
   threshold configuration. Abort cycle, set `operational_pause` with
   `pause_reason: "internal_consistency_violation"`, alert Critical.
   This branch is NOT eligible for 48h auto-resume.

**Cascade sourcing** (CB2):

Source in this order, draining each stage to its residual floor
before moving to the next stage. The four cascade-tier alerts
(`cascade_engaged_sgov`, `cascade_extended_fi`, `cascade_growth_source`,
`withdrawal_capacity_exhausted`) follow fire-once-per-tier-escalation
semantics per §12.6 — emission is gated by the cycle's
`cascade_episode_state` flags (defined below).

1. **SGOV buffer.** Sell SGOV shares for the full withdrawal amount,
   clamped at residual floor. If `(SGOV market value −
   position_residual_minimum_dollars) ≥ withdrawal amount`, source
   100% from SGOV, done.

   If this is the first cycle of the current CB2 episode in which
   SGOV was drawn (i.e., `cascade_episode_state.sgov_engaged` was
   false entering this cycle), set `sgov_engaged = true` and emit
   `cascade_engaged_sgov` Notice alert. The alert payload includes
   the SGOV dollars drawn this cycle and the SGOV remaining after
   the draw (translated to approximate-months-of-withdrawals via
   the current scheduled monthly amount, for operator legibility).
2. **FI buckets.** If SGOV reached residual, source remaining from
   FI buckets proportionally, each clamped at residual floor.

   If this is the first cycle of the current CB2 episode in which
   FI was drawn (i.e., `cascade_episode_state.fi_engaged` was false
   entering this cycle), set `fi_engaged = true` and emit
   `cascade_extended_fi` Warning alert. Reaching this branch
   necessarily means SGOV was drawn to residual on this cycle or a
   prior cycle of the episode, so `sgov_engaged` is already true
   when this branch is taken.
3. **Growth buckets.** If both SGOV and FI reached residual, source
   remaining from Growth buckets proportionally, each clamped at
   residual floor. This is the deepest cascade level reached while
   still completing the withdrawal — Growth was sold to make up the
   demand SGOV and FI couldn't cover.

   If this is the first cycle of the current CB2 episode in which
   Growth was drawn (i.e., `cascade_episode_state.growth_engaged`
   was false entering this cycle), set `growth_engaged = true` and
   emit `cascade_growth_source` Critical alert. Distinct from
   cascade exhaustion (step 4): the withdrawal succeeded, but the
   portfolio is near absolute floor and the operator should review.
4. If all three stages have reached residual and demand is not yet
   met, abort cycle. Set `withdrawal_capacity_exhausted: true`
   in state (indefinite halt for withdrawals only, per §11.3.2 and
   §11.2.7). The portfolio has effectively reached its absolute
   floor. Emit `withdrawal_capacity_exhausted` Critical alert.

   The `withdrawal_capacity_exhausted` flag is a sibling state flag
   per §11.3.2 with its own clear semantics (auto-clears when
   capacity returns) and is distinct from the
   `cascade_episode_state` flags below.

The cascade is identical regardless of which CB2 entry path activated
it (signal-based, Portfolio-low, or FI-low).

**Cascade episode state.** The four-step cascade above references a
per-episode latch struct `cascade_episode_state`, a sibling of the
CB machine state with the following fields:

| Field | Type | Meaning |
|---|---|---|
| `episode_active` | bool | True between CB2 entry and the subsequent REC. False otherwise. |
| `episode_started_at` | date \| null | Date the current episode began (the CB2-entry cycle's date). Null when `episode_active` is false. |
| `sgov_engaged` | bool | Latched true on the first cycle of the current episode that drew from SGOV. |
| `fi_engaged` | bool | Latched true on the first cycle of the current episode that drew from FI (necessarily after `sgov_engaged` became true). |
| `growth_engaged` | bool | Latched true on the first cycle of the current episode that drew from Growth (necessarily after `fi_engaged` became true). |

**Episode lifecycle.**

- On `cb_transition` to CB2 (any entry path), set `episode_active = true`,
  `episode_started_at = cycle date`, and all three engagement flags to
  false. This runs in the same transaction as the CB state change.
- On `cb_transition` to CB_INACTIVE (REC), reset the entire struct:
  `episode_active = false`, `episode_started_at = null`, all engagement
  flags to false.
- The engagement flags are write-once-per-episode (set to true on first
  engagement, never reset to false except by a REC). A withdrawal that
  only needs SGOV after a previous one reached FI does NOT clear
  `fi_engaged`. This matches the operator-stated intent that "the
  episode reached FI" is a property of the whole episode, not of a
  single withdrawal within it.
- The struct is observable in every `state_snapshot` event payload
  (per EVENT_LOG_SPEC) under `cascade_episode_state`, alongside
  `cb_machine`.

**Worked example.** A CB2 episode runs for 9 weeks. Monthly withdrawal
demand is constant at the scheduled amount. SGOV starts at the
beginning of the episode with several months of headroom.

- Week 1: CB2 entered (signal-based). Monthly withdrawal sources from
  SGOV. `sgov_engaged` latches true → `cascade_engaged_sgov` fires.
- Weeks 2–4: subsequent weekly cycles run. Monthly withdrawal sources
  from SGOV (still above residual). No new engagement; no cascade alerts.
- Week 5: monthly withdrawal again sources entirely from SGOV. No new
  engagement; no alerts.
- Week 12: SGOV reaches residual mid-withdrawal. The remaining demand
  sources from FI. `fi_engaged` latches true → `cascade_extended_fi`
  fires.
- Week 16: Recovery confirmed → REC. `cascade_episode_state` resets:
  all three engagement flags back to false; `episode_active` back to
  false.
- Week 22: New CB2 episode begins. SGOV is being refilled but still
  partially drained from the prior episode; the first withdrawal of
  the new episode engages SGOV again → `cascade_engaged_sgov` fires
  again. (The fire-once latch is per-episode, not per-installation.)

#### 7.3.3 Withdrawal action shape

The withdrawal Plan entry contains:
- Total withdrawal amount (Decimal, cents).
- Source breakdown: `[(symbol, dollar_amount, share_count_to_sell), ...]`.
- ACH destination (operator's external bank, configured).
- Scheduling metadata (the calendar date the ACH should hit external
  account).

The action layer translates this into broker SELL orders and an ACH
transfer initiation (§8.2.2).

### 7.4 SGOV buffer refill decisions

A buffer refill batch is planned when **all of the following** are true:

- Phase is 1, 2, or 3 (refill subsystem active in all phases).
- Buffer market value < buffer target. (In Phase 2, the trigger is
  the same: any drift below target — whether from operator
  withdrawal, price effects, or otherwise.)
- The 60-day post-recovery delay window has elapsed (or never
  applied — initial setup, post-Phase-3-trigger, etc.).
- CB state is not CB2 (CB2 suspends refill regardless of entry
  path; CB_INACTIVE and CB1 permit refill).
- Refill cadence (monthly): no refill batch has been executed in the
  current calendar month.

When all conditions met:

- Compute monthly refill amount: `min(monthly_refill_rate, current_buffer_deficit)`.
  The `monthly_refill_rate` is the value persisted at the most recent
  annual review (§7.6); equals `buffer_target / 12` recomputed yearly.
  If the deficit is smaller (buffer nearly full), only do what's needed.
- **Source the refill from the most-overweight Growth position.** Tie-break:
  larger position by current $ value. If multiple Growth positions are
  overweight, source proportionally among them; if only one is overweight,
  source entirely from it.
- If no Growth position is overweight (i.e., Growth bucket is at-or-
  under target, possibly due to a recent drawdown), source proportionally
  from all Growth positions despite no overweight signal. (Refill
  operates outside the rebalancer; it does not require an overweight
  condition to act.)
- All refill SELLs are clamped at the residual floor (I12). If any
  Growth position would be drawn below `position_residual_minimum_dollars`,
  reduce the SELL amount to keep it at the floor; reduce the refill
  batch correspondingly. This is a transient, self-correcting condition:
  when the next rebalance cycle runs (CB permitting), drift will be
  resolved.
- Plan: SELL the appropriate Growth amount(s), BUY equivalent SGOV.

Refill is **always** Growth-sourced, never FI-sourced. The principle
is distinct from FI-sacrosanct (I5, which constrains rebalance
direction): the SGOV buffer exists to absorb withdrawal demand when
FI/Growth are stressed. Refilling SGOV from FI would compound the
underlying problem — drawing from the income-producing bucket to top
up the emergency buffer that exists *for* income-producing-bucket
stress events. Refilling from Growth is consistent with the broader
"Growth funds long-term solvency" design: the SGOV buffer is itself
a solvency reserve, so funding it from Growth is on-thesis. Refill
operates outside the rebalancer; it does not participate in 5/25
logic.

#### 7.4.1 Recovery delay timer

After CB2 exits (CB2 → CB1 or CB2 → CB_INACTIVE, regardless of which
entry conditions cleared), a 60-day "refill delay" timer starts.
Refill is suppressed during this window to give recovery time to
confirm.

If a new cascade event triggers during the delay window, the timer
resets when the new cascade clears. (i.e., the system always waits
60 days after the *most recent* cascade event clears.)

#### 7.4.2 Refill cadence and small-vs-large deficit behavior

Once active, refills run **once per calendar month** at the
`monthly_refill_rate` (or the deficit, whichever is smaller). Two
implications:

- **Small deficit (e.g., 2 months drained):** the deficit is smaller
  than the monthly rate; full refill happens in 1-2 months.
- **Large deficit (e.g., 12+ months drained):** refill happens at the
  annual rate over ~12 months, returning the buffer to full target
  by end of year.

The refill rate does not adjust mid-year based on deficit size. The
yearly recompute at annual review (§7.6) is the only mechanism that
changes the rate.

### 7.5 Rebalancing decisions

Rebalancing decisions are made each cycle when **all of the following**
are true:

- Phase is 1 or 3 (Phase 2 uses opportunistic rebalance instead, §7.5.2).
- CB state is CB_INACTIVE (CB1 and CB2 suspend rebalancing,
  regardless of CB2 entry path).

#### 7.5.1 Phase 1 / Phase 3 standard rebalancing (5/25)

**Pre-condition:** If `phase == PHASE_3 AND schedule_state.phase3 is null`
(the latched-but-pending window per §4.3, I15), suppress the
rebalance evaluation for this cycle. The Phase 3 transition cycle
that initializes `schedule_state.phase3` is the only Plan-generating
cycle permitted in this window (D12).

**CB-state precondition:** Rebalancing is suppressed when CB state
is CB1 or CB2 (regardless of which CB2 entry path is active).
Rebalancing is only evaluated when CB state is CB_INACTIVE.

For each core position s:
- `target_value[s] = core_total × target_weight[s]`
- `drift_dollars[s] = current_value[s] - target_value[s]`
- `drift_absolute_pct[s] = |current_value[s] - target_value[s]| / core_total`
- `drift_relative_pct[s] = |current_value[s] - target_value[s]| / target_value[s]`

A position triggers rebalance if **either**:
- `drift_absolute_pct[s] ≥ rebalance_absolute_threshold_rate` (default 0.05), OR
- `drift_relative_pct[s] ≥ rebalance_relative_threshold_rate` (default 0.25).

If any position triggers, plan a rebalance that brings **all**
positions back to target (not just the triggering one). The plan:

1. SELL each overweight position by its surplus.
2. BUY each underweight position by its deficit.
3. Net cash impact should be zero (modulo rounding); residual goes to
   cash buffer for next cycle.

**Constraint (FI-sacrosanct, I5):** rebalance trades cannot sell FI
to fund a Growth purchase. In practice this is naturally satisfied
when the trigger is "Growth overweight" (you sell Growth, buy FI —
fine). If the trigger is "FI overweight" (FI grew faster than Growth),
the natural plan would be to sell FI and buy Growth, which violates I5.

**Resolution:** If the rebalance plan would require selling FI to buy
Growth, **suppress the entire rebalance** for that cycle. Record the
suppression in the cycle log alongside other per-cycle decisions; no
dedicated alert fires. This is a rare condition (FI dramatically
outperforming Growth) and the right behavior is to wait — eventually
Growth catches up. Note that the conditions producing significant FI
overweight typically coincide with CB1/CB2 (Growth depressed),
where rebalancing is suspended anyway. The operator notices any
persistent FI overweight via the mandatory weekly summary alert
(§12.3), which exposes current asset balances and weights every
cycle; no separate alerting mechanism is needed.

#### 7.5.2 Phase 2 opportunistic rebalancing

In Phase 2, standard 5/25 does NOT apply — invoking the 5/25
framework in Phase 2 would resurrect Phase 1/3 rebalancing logic
that is deliberately left quiet during Phase 2. Phase 2 has its
own dedicated maintenance routine: a two-state opportunistic swing
on drawdown/recovery (§7.5.2.a), plus a semi-annual steady-state
reallocation that scrapes accumulated dividend drift back to target
weights (§7.5.2.b). GBIL serves as opportunistic dry powder rather
than a strategically defended FI position, but it is still
maintained at 10% of core through the semi-annual reallocation.

##### 7.5.2.a Two-state opportunistic swing

The primary Phase 2 mechanism is a **two-state allocation swing**
triggered by Growth lookback signal crossings.

**Two allocation states in Phase 2:**

- **Steady state:** 90% Growth (FBCG + AVUV per ruleset sub-weights),
  10% GBIL.
- **Deployed state:** Growth at ~100%, GBIL at
  `position_residual_minimum_dollars` (default $1500). Growth split
  between FBCG and AVUV per the same sub-weights as steady state.

The system swings between these two states based on the signal:

**Deployment trigger (steady → deployed):**

- Synthetic Growth Lookback signal ≤ `phase2_opportunistic_trigger_rate`
  (default -10%, using the same `cb1_threshold_rate` value).
- 2-week confirmation window (signal must remain at or below trigger
  for 2 consecutive weekly cycles).

**Deployment plan when triggered:**

- SELL GBIL down to `position_residual_minimum_dollars`.
- BUY FBCG and AVUV with the proceeds, split per the Phase 2 Growth
  sub-weights (e.g., if Phase 2 Growth split is FBCG 50% / AVUV 50%,
  the GBIL proceeds split 50/50).
- Mark plan with `phase2_opportunistic_deploy` flag → triggers alert.

**Recovery trigger (deployed → steady):**

- Synthetic Growth Lookback signal ≥
  `phase2_opportunistic_recovery_rate` (default **+2%**, an
  *absolute* signal value — meaning the synthetic Growth index is
  2% above its value 6 months ago, equivalent to a 12% rise from
  the -10% trigger point).
- 2-week confirmation window (signal must remain at or above
  recovery threshold for 2 consecutive weekly cycles).

**Recovery plan when triggered:**

- SELL FBCG and AVUV proportionally to their current overweight
  (which will be substantial since they're at ~100% allocation).
- BUY GBIL with the proceeds to reach 10% of post-rebalance core.
- Apply residual floor (I12) to FBCG and AVUV SELLs, though this
  is unlikely to bind given the volume involved.
- Mark plan with `phase2_opportunistic_recover` flag → triggers
  alert.

**Suspend conditions** (swing evaluation does not advance pending
counters and does not fire triggers when any of these hold):

- **Phase 3 token activation grace window active.** If the
  24-hour Phase 3 grace countdown is running, Phase 2 swing
  evaluation is suspended. Pending confirmations are held but
  not advanced. Once the grace window resolves (cancelled or
  expired/latched), swing evaluation resumes — or is permanently
  moot if Phase 3 latched. **Pending Phase 2 confirmations are
  discarded at Phase 3 latch.**
- **Lookback signal UNAVAILABLE.** The swing triggers are
  signal-based; if the signal is UNAVAILABLE for the cycle, no
  trigger evaluation occurs. Pending confirmations are held but
  not advanced (consistent with §6.3's CB confirmation-counter
  semantics under signal-UNAVAILABLE conditions).
- **CB state is CB1 or CB2.** Phase 2 normally holds CB at
  CB_INACTIVE, so this is mostly belt-and-suspenders — but if a
  CB transition did fire during Phase 2 (e.g., the resource-based
  CB2 entry paths in §6.3.1 are not phase-gated and remain
  evaluable in Phase 2), swing evaluation is suspended for the
  same reason rebalancing is suspended under CB1/CB2 (§7.5):
  drawdown-protection state implies the system is not in a
  posture where opportunistic allocation swings are appropriate.
  Pending confirmations are held but not advanced.

**Why the asymmetric recovery threshold?**

Phase 2 is the legacy-growth phase with no withdrawal-survival
concern. The wide gap between deploy trigger (-10%) and recovery
trigger (+2%) is what makes the swing economically meaningful: the
system buys Growth at -10% from 6-month-ago and sells at +2% above
6-month-ago, capturing a roughly 12-percentage-point market move
per complete swing cycle. This contrasts with Phase 1/3's CB1
recovery, which uses a tight 2-point hysteresis (trip at -10%,
clear at -8%) — but CB1 is a rebalance gate, not a swing strategy,
and that 2-point band is hysteresis to prevent thrashing, not a
profit-capture target. The two mechanisms serve different purposes;
the wider Phase 2 band is intentional and on-thesis for the
legacy-growth regime. The cost of the wider band is occasional
"stuck long" periods when markets recover modestly then drop
again — but in Phase 2 that's on-thesis (max-volatility growth)
rather than off-thesis.

**Single-state-machine constraint:**

The Phase 2 swing maintains its own binary state (steady /
deployed), persisted across cycles. The system enters Phase 2 in
steady state. Subsequent swings happen only when the corresponding
trigger fires with confirmation. The state cannot skip — deployed
must precede recovery, and vice versa.

This prevents serial opportunistic deploys during a slow grinding
drawdown (where the signal might oscillate around the -10% trigger):
once deployed, the system does NOT re-deploy until it has first
recovered through +2% and returned to steady state.

##### 7.5.2.b Semi-annual steady-state reallocation

The two-state swing in §7.5.2.a handles drawdown-and-recovery, but
during long quiet stretches with no swing events, Phase 2's
allocation drifts: Growth-side dividends, Growth appreciation, and
cash-buffer deployments (§7.7, Phase 2 routes cash surplus to
most-underweight Growth only) all flow Growth-ward and dilute
GBIL below its 10% steady-state share. Without a maintenance
mechanism, GBIL would degrade toward zero across multi-year
quiet periods, leaving no dry powder available when a swing
finally triggers.

To maintain the 10% GBIL allocation without resurrecting the 5/25
framework (which is deliberately quiet in Phase 2), the system
performs a **semi-annual steady-state reallocation** on configured
dates. Default schedule: **Jan 15 and Jul 15** (configurable:
`phase2_reallocation_dates`). The Jan 15 occurrence is naturally
combined with the annual-review cycle; the Jul 15 occurrence runs
as a scope extension of the weekly cycle on that date (§6.5.1).
Weekend/market-holiday shift: next trading day, same rules as
other date-anchored cycles.

**Reallocation behavior:**

- Trigger: cycle date matches `phase2_reallocation_dates` AND
  phase is PHASE_2 AND Phase 2 swing state is `steady` (NOT
  `deployed`). The reallocation is **skipped** during the
  `deployed` state — that allocation is intentional and managed
  by the swing-recovery trigger, not by the calendar.
- Action: SELL FBCG and AVUV (whichever are overweight against
  their post-reallocation targets) and BUY GBIL (or vice versa
  if GBIL has somehow accumulated above 10%) such that the
  post-reallocation core lands at:
  - FBCG: 45% of core
  - AVUV: 45% of core
  - GBIL: 10% of core
- The FBCG/AVUV 45/45 split inside Growth matches the deployed
  state's 50/50 inside the 100%-Growth allocation, applied here
  to the 90%-Growth slice of steady state.
- Apply residual floor (I12) to all SELLs. Position residuals are
  unlikely to bind given the volume involved, but the floor still
  applies for defense-in-depth.
- Mark plan with `phase2_semi_annual_reallocation` flag → triggers
  an Info-severity alert noting the drift that was scraped back.

**Skip conditions** (semi-annual reallocation does NOT run, even
on a scheduled date; the reallocation is calendar-driven and has
no pending-counter state to preserve across skips — a skipped
occurrence is simply not executed, and the next semi-annual date
re-evaluates the same way):

- **Phase 3 token activation grace window active.** Same suspend
  rationale as the §7.5.2.a swing triggers — defer the
  reallocation until the grace window resolves.
- **Lookback signal UNAVAILABLE.** The reallocation is not
  signal-gated for its trigger logic, but the operating
  environment must be observable enough to safely execute the
  trades. Defer to the next weekly cycle to retry.
- **CB state is CB1 or CB2.** In the unusual case CB was active
  in Phase 2 — Phase 2 normally holds CB at CB_INACTIVE so this
  is mostly belt-and-suspenders.
- **Phase 2 swing state is `deployed`** — the swing-recovery
  trigger is what returns the system to steady state, not the
  calendar. (This condition is specific to §7.5.2.b; the swing
  itself is the mechanism that handles the deployed state.)

**Why semi-annual, not 5/25 or annual:**

A twice-a-year cadence handles the dilution at a rate appropriate
to its accumulation. Quarterly would generate nuisance trades
from modest drift; annual would let GBIL drift far enough to
significantly reduce swing-deploy ammunition mid-year. The Jan 15
alignment with annual review keeps the operator's mental model
clean (one big maintenance event in January); Jul 15 sits
opposite on the calendar for the second occurrence.

This mechanism is **not** a 5/25 invocation: there is no
drift-threshold detection, no signal-gated activation, no
cross-cycle confirmation window. It is a calendar-driven
idempotent realignment that happens twice a year regardless of
intervening market conditions (subject to the skip conditions
above). Phase 1/3's 5/25 mechanics and Phase 2's semi-annual
mechanic share no code; they are conceptually different
maintenance regimes for different phases.

### 7.6 Annual review (Jan 15)

The annual review is a **single combined event** that runs once per
year on the configured `annual_review_date` (default January 15;
shifts to next trading day if weekend or market holiday). It executes
during the annual-review cycle (§3.8, §6.5.1) and performs three
distinct evaluations:

1. **Freeze evaluation** (Phase 1 and Phase 3 only)
2. **Buffer target and refill rate recompute** (Phase 1 and Phase 3;
   moot in Phase 2)
3. **Cash target recompute** (Phase 1 and Phase 3; Phase 2 stays at
   $1000 fixed)

These three evaluations always occur together; their results are
persisted atomically as a single annual-review record (§6.7).

#### 7.6.1 Freeze evaluation (Phase 1 and Phase 3)

**Active:** Phase 1 and Phase 3 only. Phase 2 has no scheduled income
to freeze.

**Decision algorithm:**

1. Read the persistent CB transition log (§6.7) for the prior calendar
   year (e.g., evaluating on 2032-01-15 reads 2031's log).
2. Compute total calendar days during the prior year on which **any
   of CB1 or CB2 was active for any part of the day**.
   Each calendar day counts at most once regardless of intra-day
   transitions. 3. If total days ≥ `freeze_evaluation_threshold_days` (default 30,
   configurable), the new year's CPI raise is **frozen** — the year
   is appended to the active phase's schedule_state.frozen_years list.
4. Otherwise, no append: the CPI raise applies normally per §3.13's
   formula.
5. Persist the decision (`year, frozen: bool, cb1_days, cb2_days,
   decided_at_timestamp`) to durable state.
6. Generate alert with the decision and the underlying CB-day counts.

**Compounding rule:** Skipped raises do not stack. If 2031's raise
was frozen and 2032's raise applies normally, 2032's monthly
withdrawal is computed via §3.13's formula counting non-frozen years
only. The freeze permanently removes that year's raise from the
schedule.

**STOP INCOME pause does not affect freeze evaluation.** The annual
review runs whether or not income is paused. CB1+ days during the
paused period count normally toward the threshold. On resume (per
§4.4), the resumed monthly amount reflects the schedule_state
including any freezes recorded during the pause.

#### 7.6.2 Buffer target and refill rate recompute

**Active:** Phase 1 and Phase 3.

```
buffer_target = 24 × current_monthly_withdrawal
monthly_refill_rate = buffer_target / 12
```

`current_monthly_withdrawal` is the new year's scheduled monthly
amount (post-freeze-evaluation, per §3.13's formula). Both values
persist; they govern all refill batches until the next annual review.

**Phase 2:** the buffer target and refill rate are NOT recomputed
during Phase 2's annual review cycle. They carry forward at their
end-of-Phase-1 frozen values. This is moot for refill triggering
(§7.4 still triggers on buffer < target) but means the Phase 2 system
uses static values for these fields throughout Phase 2's duration.

#### 7.6.3 Cash target recompute

**Active:** Phase 1 and Phase 3.

```
cash_target = current_monthly_withdrawal + 1000  (Phase 1, Phase 3)
cash_target = 1000  (Phase 2, unchanged)
```

The cash target persists and governs cash refill / drawdown decisions
(§7.7) until the next annual review.

#### 7.6.4 Schedule state representation

For each income-producing phase, the complete withdrawal schedule is
fully determined by:
- `I_0` — starting income at trigger (immutable after trigger)
- `trigger_year` — calendar year of phase activation (immutable)
- `cpi` — annual CPI rate (immutable per phase: 0.03 Phase 1, 0.04
  Phase 3)
- `frozen_years: list[int]` — years where the CPI raise was frozen
  (append-only)

Current scheduled monthly income for year Y is computed per §3.13.
The withdrawal layer reads the current scheduled value; it does not
re-derive freeze decisions.

#### 7.6.5 Why an annual event and not just-in-time?

A discrete annual event (rather than re-evaluating freeze on every
withdrawal cycle, or recomputing buffer/cash targets on every cycle)
provides:
- A single auditable timestamp for each year's decisions
- A natural alert moment for the operator/survivor
- Cheaper cycles (read stored values vs. recompute from CB log)
- Robustness against CB log corruption discovered later (the freeze
  decision is locked once made)
- Predictable buffer/refill behavior within a year (no mid-year rate
  shifts)

### 7.7 Cash buffer decisions

The cash buffer is maintained at target ± $250 (configurable:
`cash_buffer_target` per phase, `cash_buffer_tolerance`).

Each cycle:

- If cash > target + tolerance: plan a deployment of the surplus to
  the most-underweight core position (Phase 1, Phase 3) or
  most-underweight Growth (Phase 2). This is the mechanism that
  handles unexpected inflows (deposits, conversions, dividends).
- If cash < target - tolerance: plan a refill of the deficit by
  selling from the most-overweight core position. Tie-break: larger
  position by current $ value.
- If no position is overweight at refill time: source proportionally
  from the largest bucket by current $ value (Growth bucket vs FI
  bucket). This handles the rare balanced-market case.

**Note on FI-sacrosanct (I5):** I5 forbids only FI → Growth trades.
Selling FI to fund cash is permitted regardless of size — cash is not
Growth. The cash refill rule above is unconstrained by I5.

**Cash deployment vs. FI-sacrosanct (I5):** Deploying surplus cash to
an underweight Growth position is permitted regardless of how the
cash originated. I5 constrains *rebalance trades* (§3.14) —
specifically, a single-plan SELL FI → BUY Growth pair. Cash buffer
deployment is not a rebalance trade; it is operational disposition
of accumulated cash (from dividends, inflows, or rounding residuals).
Even if recent cycles included permitted FI → cash sales, the
resulting cash is fungible and can deploy where needed. I5 is a
single-plan-entry rule, not a cross-cycle history rule.

Cash buffer maintenance is independent of CB state — it runs even
during cascade conditions. The cash buffer is operational
infrastructure (transaction reserve), not an investment position;
its maintenance has different priorities than core portfolio
management.

#### 7.7.1 Large cash deployment

The §7.7 cash-surplus rule deploys small surpluses ($250 tolerance
above target) to the most-underweight core position one bite per
cycle. This works for dividends, rounding residuals, and small
incidental inflows. It does **not** work well for large annual
inflows — Phase 1's planned Roth conversion pattern (~$200K initial
in 2027, ~$130K annual for years 2–8 per §2.9) lands tens of
thousands of dollars of cash in a single deposit, and the
single-position-per-cycle rule would take 6+ weeks to restore
allocation, leaving large idle cash holdings during the deployment.

**Trigger.** A large cash deployment fires when cash surplus
exceeds the **max** of:

- `large_cash_deployment_threshold_dollars` (default $25,000)
- `large_cash_deployment_threshold_rate` × current portfolio value
  (default 5%)

The dollar floor protects small portfolios; the percentage floor
keeps the trigger meaningful as portfolio grows. When triggered,
the **entire surplus** above target is deployed in one cycle
(not just the portion above the threshold).

**Cadence.** Evaluated on every weekly cycle (the existing cash
buffer evaluation in §6.5 / §7.7). A Roth conversion landing
mid-week waits until the next Wednesday weekly cycle for deployment
— the bounded delay (up to 6 calendar days) is operationally
acceptable.

**Behavior by CB state and signal condition:**

The deployment algorithm has two modes:

- **Target-weight proportional** (default mode). For each position
  `s` in the active phase's `target_weights` dict:
  `buy_dollars[s] = cash_to_deploy × target_weight[s]`. No reading
  of current position values; no proportional-to-deficit math. Cash
  flows to each position in exact target-weight proportion,
  regardless of pre-inflow allocation. The total BUY dollars =
  `cash_to_deploy` (modulo rounding).

- **Defensive** (CB2-signal-active mode). Deploy all cash to **SGOV
  buffer** until the buffer is at target
  (`sgov_buffer_target_months × current_monthly_withdrawal`).
  Surplus beyond buffer-target deploys to **FI bucket only** (the
  most-underweight FI position per §7.7 main body). No Growth
  purchases. The operator's strategy during market-stressed
  conditions is runway extension, not Growth accumulation at
  uncertain prices.

Which mode applies depends on whether the signal-based CB2
condition is currently holding (signal ≤ `cb2_threshold_rate`),
re-evaluated each cycle:

| Phase | CB state | Signal currently ≤ -20%? | Deployment mode |
|---|---|---|---|
| Phase 1, Phase 3 | CB_INACTIVE | (n/a) | Target-weight proportional |
| Phase 1, Phase 3 | CB1 | No (CB1 means signal in [-20%, -10%]) | Target-weight proportional |
| Phase 1, Phase 3 | CB2 | Yes | Defensive |
| Phase 1, Phase 3 | CB2 | No (CB2 active only via resource trigger) | Target-weight proportional |
| Phase 2 | n/a (CB held inactive) | (n/a) | Target-weight proportional, against Phase 2 weights (see below) |

The CB2 resource-only case is the bootstrap-Roth scenario: the
portfolio is small, CB2 is engaged to draw withdrawals from SGOV,
the signal is nominal (no market drawdown), and a large cash
infusion arrives. Target-weight proportional deployment puts the
cash where it's needed to build the core portfolio toward its
target allocation — which is exactly what clears the Portfolio-low
condition.

The CB2 signal-active case is the market-drawdown scenario: defensive
deployment buys SGOV runway and avoids putting fresh cash into
Growth at depressed valuations. If both signal and resource
conditions are active simultaneously, signal takes precedence —
defensive deployment applies.

**Drift correction is delegated to existing mechanisms.** Any
post-inflow drift away from target weights is absorbed by:

- The next cycle's 5/25 rebalance evaluation (Phase 1 and Phase 3),
  which triggers a corrective rebalance if drift exceeds the 5/25
  thresholds.
- The next semi-annual reallocation (Phase 2; see §7.5.2.b),
  which scrapes accumulated drift back to target every 6 months.

This is a deliberate trade-off: post-inflow drift up to 5/25
tolerance levels persists until the next correction cycle. The cost
(slightly slower drift correction) is paid in exchange for
algorithmic simplicity and no dependence on current position values
during deployment.

**Phase 2:** Target-weight proportional deployment via the
`phase2_steady_target_weights` (or `phase2_deployed_target_weights`
if currently in the deployed state per §7.5.2.a). Phase 2 has no
withdrawal-survival mode; cascade logic does not apply. The
semi-annual reallocation handles routine drift; the large-deployment
mechanic handles bulk inflows.

**Plan entry shape.** The **LargeCashDeployment** plan entry
contains: total cash amount, per-position BUY breakdown (symbol,
dollar amount, share count), target weights reference, deployment
mode (`target_weight_proportional` or `defensive`), and an alert
flag. Action layer executes it as a coordinated multi-BUY batch,
similar to PhaseTransition but BUYs-only (no SELLs, no liquidations).
Alert: `large_cash_deployment` (Notice severity), per §12.6 alert
catalog.

**Cycle integration.** Runs between the §7.7 cash buffer maintenance
and the §7.5 rebalance check. If a large deployment fires this
cycle, the rebalance check skips this cycle (the deployment has
already moved cash into target positions; any residual drift is
sub-threshold and will be caught next cycle if it ever crosses 5/25).

**Idempotency.** Same broker-state-as-truth rule as everything else
(§8.3). If the LargeCashDeployment plan partially executed and the
cycle aborted, the next cycle re-reads positions, observes that
some cash has deployed and some remains, and produces a fresh plan
covering only the still-needed BUYs.

### 7.8 Decision layer invariants

These properties of the decision layer must always hold and become
test assertions (§13.5):

- **D1:** A Plan generated for state S, evaluated again immediately
  with no state change, produces an identical Plan.
- **D2:** The Plan's net cash impact is zero ± rounding tolerance
  (sales fund purchases). Exception: withdrawals (net negative cash
  out via ACH) and refill batches (net negative core, net positive
  buffer).
- **D3:** No Plan entry sells FI to fund a Growth purchase
  (FI-sacrosanct, I5).
- **D4:** No Plan entry rebalances during CB1 or CB2 state.
- **D5:** No Plan entry refills SGOV during CB2 state, or during the
  60-day post-recovery window.
- **D6:** No Plan entry initiates a deposit, conversion, or rollover
  (per I11).
- **D7:** Withdrawal Plan entries appear only when Income state is
  ACTIVE.
- **D8:** Phase transition cycles produce only the transition Plan;
  withdrawal, refill, and rebalance entries are suppressed.
- **D9:** No Plan entry SELLs an active-allowlist position below
  `position_residual_minimum_dollars`. Phase transition liquidations
  permitted by I13 are the only exception, and only for positions
  permanently retired (not in any future-reachable phase's allowlist).
- **D10:** During CB1 (not in CB2), no Withdrawal Plan entry has
  Growth in its source breakdown (per I14).
- **D11:** A LargeCashDeployment plan entry in `target_weight_proportional`
  mode contains only BUY orders. Per-position BUY dollars equal
  `cash_to_deploy × target_weight[symbol]` for each symbol in the
  active phase's target weights. Total BUY dollars equal
  `cash_to_deploy` (modulo rounding). No position appears in the
  plan with a BUY amount that would deviate from this
  target-weight-proportional rule.
- **D12:** No Withdrawal, Rebalance, or LargeCashDeployment Plan
  entry is generated when `phase == PHASE_3 AND schedule_state.phase3
  is null` (the Phase 3 latched-but-pending window per §4.3 and
  I15). The transition cycle that initializes `schedule_state.phase3`
  is the only Plan-generating cycle permitted in this window.

### 7.9 ACHScheduleUpdate decisions

ACHScheduleUpdate plan entries are generated by the decision layer
in three cases:

1. **Income state transition.** When Income state transitions
   ACTIVE → PAUSED (STOP INCOME tokens inserted), emit
   ACHScheduleUpdate to set ACH amount to $0. When Income state
   transitions PAUSED → ACTIVE, emit ACHScheduleUpdate to restore
   the broker-side amount to the current scheduled monthly
   withdrawal.
2. **Phase transition affecting withdrawal amount.** Phase 1 → Phase 3
   and Phase 2 → Phase 3 transitions change the withdrawal amount;
   the phase transition plan includes an ACHScheduleUpdate to the
   new amount. Phase 1 → Phase 2 transitions set ACH to $0 (no
   Phase 2 withdrawals).
3. **Annual review (Phase 1 and Phase 3).** When the annual review's
   freeze evaluation results in a CPI raise (or determines no
   freeze), the new annual amount is set via ACHScheduleUpdate,
   conditional on Income state being ACTIVE. If PAUSED, the ACH
   amount stays at $0 and the new amount is recorded in
   schedule_state for use when income resumes.

ACHScheduleUpdate retries on next cycle if rejected (§8.2.7).
Persistent failure escalates to Warning severity alerts but does
NOT halt the system (per §8.2.7 ruling): the system continues
operating at the prior ACH amount until the operator resolves the
IBKR-side issue. The §6.5 cycle evaluation order calls for
emitting an ACHScheduleUpdate entry whenever Income state
transitioned this cycle (between step 3 and step 4).

---

## §8. Action Layer

The action layer consumes a Plan (§3.12, §7) and executes it against
external systems: the broker, the ACH transfer mechanism, the
alerter, and the persistence layer. The action layer has **no
decision authority** — it only executes what the Plan specifies.

This separation is the structural defense against the IPM v1
C2-class bug. In that bug, decision logic and action logic were
intertwined: the condition that decided "withdrawals should pause"
also accidentally controlled "where withdrawals route to." A decision
layer that produces inspectable Plans, executed by a stateless action
layer, makes it structurally impossible for one condition to silently
control two effects.

### 8.1 Plan execution model

A Plan is a sequence of typed entries (§3.12). The action layer
executes entries in their listed order, with these properties:

- **Per-entry atomicity.** Each entry succeeds completely or fails
  completely. No partial-state commits within an entry.
- **Sequenced execution.** Later entries see the state changes from
  earlier entries (e.g., a BUY after a SELL sees the cash from the
  SELL).
- **Failure-stops execution.** If any entry fails, remaining entries
  are not attempted. The cycle ends with the failure logged.
- **No retry within a cycle.** Failed entries are not automatically
  retried in the same cycle. The next scheduled cycle re-evaluates
  state from scratch and produces a new Plan that reflects the
  partial-execution state.

The "no retry within a cycle" rule is deliberate: retry logic at the
action layer hides state inconsistencies from the decision layer.
Better to let the cycle end in a known-failed state and have the
next cycle observe the actual broker state and decide afresh.

### 8.2 Entry type execution

#### 8.2.1 Order entries (BUY / SELL)

For each Order entry:

1. Validate the entry against current broker state:
   - Symbol is in active-phase allowlist AND known to the broker
     (valid instrument).
   - Sufficient cash for BUY (else fail).
   - Sufficient shares for SELL (else fail).
   - Order would not breach `position_residual_minimum_dollars` for
     a SELL (else fail; this is a defense-in-depth check; the
     decision layer should have prevented this per D9).
2. Submit the order to the broker as a market order during regular
   trading hours.
3. Wait for fill confirmation, with a timeout (configurable:
   `order_fill_timeout_seconds`, default 60s).
4. On successful fill: record fill details (executed shares, average
   price, total cost, broker order ID) and proceed to next entry.
5. On timeout or fill rejection: record failure details, abort
   remaining entries, alert.

**Order timing.** All orders are submitted during regular US equity
market hours (09:30–16:00 ET). The cycle scheduler must arrange that
cycles requiring orders run during market hours. Cycles that fall
outside market hours and contain order entries are aborted with an
"after-hours-cycle-with-orders" alert; they will retry at the next
scheduled cycle within market hours.

**Market vs limit orders.** IRAPM uses market orders exclusively.
Limit orders introduce timing complexity (partial fills, expired
orders, order management state) that the cycle model isn't designed
to handle. The high-liquidity ETFs in the IRAPM allowlist (FBCG,
AVUV, PYLD, JPIE, GBIL, SGOV) have tight enough spreads that
market-order slippage is negligible for the typical order sizes
under normal market conditions.

**Slippage during cascade conditions.** Under CB2 cascade conditions
(§6.3), market stress can widen bid/ask spreads on the FI bucket
ETFs (PYLD, JPIE in particular) well beyond their normal range.
IRAPM continues to use market orders during cascade events; the
resulting wider slippage is an **accepted cost**. The primary goal
during cascade execution is survivor liquidity (drain SGOV → FI →
Growth per §7.3.2 to extend runway), not optimal execution pricing.
Introducing limit-order logic conditional on CB state would violate
the Simple pillar (the cycle model would need order-management
state for the precise market conditions in which failures are most
costly), and the alternative — delaying liquidations until spreads
tighten — is worse than paying spread, because cascade conditions
can persist for months.

**Fractional shares.** IBKR supports fractional shares for the
allowlisted ETFs. The action layer issues orders in dollar amounts
(IBKR converts to fractional shares) for all SELLs and BUYs. This
avoids the share-count rounding error that would otherwise drift
the buffer accounting over time.

**Settlement timing within a cycle.** US equity ETFs settle T+1.
Within a single cycle, SELL → BUY sequences (rebalancing, refills,
phase transitions) execute without artificial delay; IBKR allows
unsettled SELL proceeds to fund same-day BUY orders within an IRA.
The settlement constraint applies only to SELL → ACH withdrawal
sequences, which IRAPM handles at the scheduling layer (§9.6.1)
rather than within a cycle.

#### 8.2.2 Withdrawal entries

For each Withdrawal entry (always exactly one per cycle that
includes withdrawals):

1. Verify the source breakdown sums to the total withdrawal amount
   (defense in depth against decision-layer bugs).
2. Submit the underlying SELL orders per the source breakdown
   (using the Order entry execution mechanic above). All SELLs must
   succeed before proceeding.
3. After all SELLs fill, initiate the ACH transfer for the total
   amount via the broker's ACH withdrawal API.
4. Verify ACH initiation success (the broker returns a transfer
   reference ID). The actual ACH settlement happens 1-3 business
   days later, outside this cycle.
5. Record: SELL fills, ACH initiation reference, scheduled settlement
   date.
6. If any SELL fails, do NOT initiate the ACH transfer. The cycle
   ends in a partial-failure state (some SELLs may have filled).
   Alert with severity: Critical (blocking — see §11.3). Operator
   review required.

**ACH mechanism.** Withdrawals flow via IBKR's recurring ACH withdrawal
mechanism. IRAPM updates the recurring ACH amount on the broker side
to match the current scheduled monthly withdrawal. The actual transfer
is then triggered automatically by IBKR on the configured day of the
month. (Per the operator's stated mechanism: 2nd IBKR access account
with 2FA disabled, used for unattended ACH adjustment.)

This means the IRAPM action layer's "withdrawal" entry is actually
two distinct broker interactions:

- **Monthly:** SELL orders to fund the cash needed for the upcoming
  ACH transfer (one cycle, one or more SELLs).
- **Whenever schedule changes:** UPDATE the recurring ACH amount on
  the broker side, via the separate `ACHScheduleUpdate` plan entry
  (§8.2.7).

#### 8.2.3 BufferRefill entries

A BufferRefill entry is structurally a sequence of one or more SELL
orders (Growth → cash) followed by one BUY order (cash → SGOV).
The action layer executes them as such, with all SELLs completing
before the BUY. Net cash impact within the entry should be near zero.

#### 8.2.4 CashRefill entries

Symmetric to BufferRefill but in either direction (sell to add cash,
or buy to deploy excess cash). Single SELL or BUY order in most cases.

#### 8.2.5 PhaseTransition entries

Executed as a coordinated batch:

1. Execute all SELL orders (liquidations and drop-to-residuals) first.
   Skip any SELL whose target position is already at or below residual
   (no-op). The skip is recorded in the cycle log (§12.2.1) and
   surfaced in the `large_rebalance` alert's residual-exceptions
   section (§12.6).
2. Wait for all SELL fills (timeout same as Order entries, but
   applied to the batch).
3. Compute available cash post-SELLs.
4. Execute all BUY orders against the available cash.
5. Verify post-execution allocation matches the target within
   tolerance (defense-in-depth).
6. Update internal Phase indicator (durable persistence).
7. For Phase 3 transitions, also persist:
   - `I_0` (computed against the post-SELL portfolio value)
   - `trigger_year`
   - Initial empty `frozen_years` list
   - Buffer target = 24 × I_0
   - Refill rate = buffer target / 12
   - Cash target = I_0 + $1000

A PhaseTransition entry's failure leaves the system in a partially-
transitioned state requiring `operational_pause` with `pause_reason:
"partial_phase_transition"`. Alert severity: Critical (self-healed
weird) — the auto-resume cycle re-reads broker state and completes
the transition from wherever it left off. See §11.2.8.

#### 8.2.6 CBStateTransition entries

Pure record-keeping. The decision layer already evaluates and decides
state transitions; the CBStateTransition entry exists so the
transition is **logged** as part of the Plan rather than as a side
effect of decision logic. Execution:

1. Append `(timestamp, from_state, to_state, trigger_reason, cycle_id)`
   to the CB transition log (§6.7 persistence).
2. No external action.

CBStateTransition entries always succeed (modulo persistence layer
failure, which is its own catastrophic case).

#### 8.2.7 ACHScheduleUpdate entries

Updates the recurring monthly ACH amount on the broker side. Execution:

1. Submit the new amount via IBKR's ACH management API.
2. Verify the broker accepted the change.
3. On rejection (e.g., previous update still pending): log, alert
   (Notice severity), mark cycle as "partial — ACH not updated";
   subsequent cycles re-attempt.

ACHScheduleUpdate entries are emitted by the decision layer in these
cases:

- Phase 1 → Phase 2 transition (set to $0; no Phase 2 withdrawals)
- Phase 1/2 → Phase 3 transition (set to Phase 3 starting income)
- Phase 1 / Phase 3 annual review when freeze decision changes the
  scheduled amount or applies the CPI raise (set to new monthly amount)
- STOP INCOME ACTIVE → PAUSED (set to $0)
- STOP INCOME PAUSED → ACTIVE (set back to current schedule_state amount)

Failure of an ACHScheduleUpdate entry does not fail the cycle — the
decision is logged, alerted as Notice, and the next cycle re-attempts.
SELL orders for the cycle's withdrawal still execute against current
broker-side ACH amount (which may be stale by one cycle).

**Persistent-failure escalation.** ACHScheduleUpdate failures do NOT
trigger operational_pause. The system continues operating with the
broker-side ACH amount unchanged from its last successful update;
monthly withdrawals execute against the prior amount until the
operator manually fixes the IBKR-side issue. Alert severity
escalates with consecutive failures:

- First failure: Notice severity, `ach_update_failed` alert.
- After `ach_update_warning_threshold_cycles` consecutive failures
  (default 3): escalates to Warning severity. Alert content includes
  the target ACH amount, the IBKR Portal navigation path to the
  recurring-withdrawal management page, and a note that the operator
  must update the broker-side amount manually.
- Failures continue alerting at Warning severity each subsequent
  cycle; no further escalation. No state-file flag is set; no halt.

The rationale: ACHScheduleUpdate is an annual-cadence operation in
practice (only changes on phase transitions, annual review, or STOP
INCOME edge cases). The wrong-amount risk is bounded — the prior
amount continues, which is at worst slightly stale (last year's
amount instead of this year's CPI-adjusted amount, or the pre-pause
amount instead of $0 during STOP INCOME pauses). Halting the system
entirely because IBKR's API rejected an update would be worse than
running with a stale ACH amount until the operator resolves the
IBKR-side issue. The manual procedure is specified in detail in the
operational runbook (§14.3).

#### 8.2.8 LargeCashDeployment entries

Executed as a coordinated multi-BUY batch:

1. Read current broker positions and cash balance to confirm the
   surplus that triggered deployment is still present (broker state
   is the source of truth per §8.3).
2. Submit all BUY orders in the entry's per-position breakdown,
   tracking each by broker order ID.
3. Wait for fill confirmations on the full batch, with the same
   per-order timeout as §8.2.1 Order entries
   (`order_fill_timeout_seconds`).
4. On full success: log fills, emit `large_cash_deployment` Notice
   alert with per-position fill details.
5. On partial failure (some BUYs filled, some timed out or
   rejected): abort remaining BUYs, log the partial state. The
   next cycle re-reads broker state, observes the partial
   deployment, and produces a fresh LargeCashDeployment entry
   covering only the still-needed BUYs.

LargeCashDeployment does NOT set `operational_pause` on partial
failure — the partial state is benign (some cash deployed,
remainder waits for next cycle's plan), and the natural-retry
behavior of the planning layer resolves it without special handling.

#### 8.2.9 Alert entries

Alerts are structured messages dispatched to the alerter (§9.3).
Each Alert entry contains:

- Severity (Info, Notice, Warning, Critical) — see §11.1
- Channel (Email, SMS, Both) — typically Both per §9.3
- Subject (short)
- Body (structured with relevant context)
- Deduplication key (so repeated alerts of the same type within a
  cadence window collapse into one)

Alert dispatch happens at end-of-cycle, after all other entries have
either completed or failed. This ensures that the alert reflects the
actual outcome of the cycle, not the intended outcome.

Alert dispatch failure does NOT fail the cycle (the actions already
happened; the alert just didn't reach the operator). It does generate
a separate "alerter unreachable" durable record that the next cycle
picks up.

### 8.3 Idempotency and recovery

A cycle that fails partway through must leave the system in a
recoverable state. Specifically:

- **Broker state is the source of truth.** IRAPM's persisted state
  about positions is informational; the next cycle re-reads positions
  from the broker. A cycle that filled some orders before failing
  is not "lost" — the next cycle sees the actual broker state.
- **Plan persistence (cycle log only).** The Plan generated by each
  cycle is written to the cycle log (§12.2.1) at the start of the
  action layer's execution. The Plan is NOT written to the operating
  state file; the state file remains minimal. On restart after a
  crash mid-cycle, the operator can read the cycle log to understand
  what was attempted.
- **No duplicate execution.** The action layer does NOT replay
  previously-attempted Plans on restart. The next cycle re-evaluates
  from current state and produces a fresh Plan.

This means a partial cycle creates a transient anomaly (e.g., a SELL
filled but the matching BUY didn't, leaving cash in the account)
that the next cycle naturally resolves through cash-buffer logic
and rebalancer logic.

### 8.4 Action layer invariants

These properties must always hold and become test assertions (§13.5):

- **A1:** The action layer reads only the Plan and current broker
  state. It does NOT consult the operating state machines (CB,
  Phase, Income), which are decision-layer concerns.
- **A2:** The action layer never modifies the operating state
  machines. State transitions are recorded by CBStateTransition
  entries, which are computed by the decision layer.
- **A3:** Order entries are submitted as market orders, in dollar
  amounts (fractional-share-aware), during regular trading hours.
- **A4:** Withdrawal entries' SELL components must all succeed before
  the ACH transfer is initiated. Partial SELL execution prohibits
  ACH initiation.
- **A5:** PhaseTransition entries that fail mid-execution leave the
  system in a Critical-alert state requiring operator review.
- **A6:** The action layer does not retry within a cycle.
- **A7:** Alert dispatch failure does not fail the cycle.
- **A8:** ACHScheduleUpdate failures do not fail the cycle; they
  re-attempt on subsequent cycles.

---

## §9. External Interfaces

This section specifies the boundaries between IRAPM and the systems
it depends on. For each external dependency: what IRAPM expects, how
failures are handled, and what's deferred to implementation choice.

### 9.1 Broker interface (IBKR)

The broker interface itself — the Protocol contract, per-cycle
connection lifecycle, idempotency design, master/slave coordination
via IBKR-as-arbiter, exception model, and the ContractRef opaque
identifier — is specified in §15 (Broker Layer). This section retains
the operator-deployment concerns that frame how IRAPM's broker
process is authenticated and credentialed.

#### 9.1.1 Account model

IRAPM uses a **dedicated 2nd IBKR access account** with 2FA disabled,
per operator design. This account has trading and ACH withdrawal
permissions, but is otherwise isolated from the operator's primary
brokerage interaction. The recurring ACH destination is restricted
to the operator's known external bank, set up one-time via the IBKR
portal (per §15.10 the system does not initiate ACH destinations).

#### 9.1.2 Authentication and credential storage

The 2FA-disabled API access is an explicit operational design choice
to enable unattended operation. The risk is mitigated by:
- Account isolation from operator's primary brokerage account
- Trading restricted to the IRAPM allowlist (operator-set IBKR
  trading permissions)
- ACH destination restricted to the operator's known external bank
- Hardware-level access control on the CL260 boxes (physically
  secured, see §10.1.1)
- Box-local config (`box.yaml`, §15.8) holds `expected_account_id`;
  any drift from this fails connection with `BrokerInconsistency`
  rather than silently connecting to a wrong account.

Credentials (IBKR username/password) are stored in `.env` outside
the codebase and read at process start. Credential rotation is an
operator responsibility tracked in the runbook (§14.3).

#### 9.1.3 Pointer to §15

For the protocol contract, connection lifecycle, idempotency model,
typed exceptions, master/slave coordination, and deployment
requirements, see §15. The behavioral contract that the decision
and action layers depend on is fully specified there.


### 9.2 Price data interface

IRAPM reads price data for the Synthetic Growth Lookback signal
(§5) from local TSV/CSV files in `C:\portfolio\data\`. These files
follow the format used by the IPMS simulator (see IPMS_SPECIFICATION.md
or `data/README.md` for format details).

The data files are operator-maintained: the operator periodically
refreshes them via Yahoo Finance copy-paste. IRAPM does not fetch
data automatically. Staleness gates in the lookback signal (§5.2)
detect data refresh lag and trigger UNAVAILABLE.

#### 9.2.1 Required files

For Phase 1 / Phase 3 operation:
- `FBCG.tsv` (or `.csv`) — FBCG Adj Close history, weekly cadence
  acceptable
- `AVUV.tsv` — AVUV Adj Close history

For Phase 2 operation: same files (the Phase 2 opportunistic
rebalance uses the same Growth lookback).

The data interface does NOT require files for PYLD, JPIE, GBIL, or
SGOV — those positions are not used in lookback calculations.

#### 9.2.2 Failure modes

| Failure | IRAPM response | Category |
|---|---|---|
| Required data file missing | Lookback signal UNAVAILABLE; alert; CB state holds | Notice (auto-recovers when refreshed) |
| Data file stale (>14 days) | Lookback signal UNAVAILABLE; alert; CB state holds | Notice (auto-recovers when refreshed) |
| Data file malformed | PriceDataError raised; cycle aborts; alert | Critical-blocking |

Data file failures degrade gracefully — they prevent the lookback
signal from updating but do not prevent withdrawals, rebalancing, or
other actions that don't depend on the signal. The operating state
machines (CB, etc.) hold their last value when the signal is
unavailable.

### 9.3 Alerter interface

IRAPM dispatches alerts via two channels:

- **Email** via Gmail SMTP (operator-configured; credentials in `.env`).
- **SMS** via Twilio (operator-configured; credentials in `.env`).

**All alerts are dispatched on both channels** regardless of
severity. Rationale: SMS is the reliability layer (delivers even
during data plan outages or remote-area connectivity loss); email
is the detail layer (full structured content, easier to read on a
larger screen). The two channels carry the same information at
different abbreviation levels — email gets the full structured body;
SMS gets a concise summary with a directive to check email for
details.

Severity affects retry behavior on dispatch failure (§9.3.3) but
not channel selection.

#### 9.3.1 Alert content structure

All alerts include a structured body with:
- Cycle ID and timestamp
- Triggering condition (state transition, action result, etc.)
- Current operating state snapshot (Phase, CB state, Income state,
  CB2 entry conditions if CB2 active)
- Relevant numerical context (signal value, portfolio value, etc.)
- Plan summary (what the cycle did or attempted)

SMS messages are necessarily abbreviated; they include the most
essential info and direct the operator to check email for details.

#### 9.3.2 Deduplication

Alerts include a deduplication key. Within a configurable window
(default: 24 hours), repeated alerts with the same dedup key are
suppressed (only the first sends; subsequent occurrences increment
a counter that's reported in the next alert that does fire).

This prevents alert storms during persistent conditions (e.g., a
broker outage that fails every cycle for hours).

#### 9.3.3 Failure modes and retry behavior

| Failure | IRAPM response |
|---|---|
| Email send fails | Log; do not fail the cycle; record for retry next cycle |
| SMS send fails (Info, Notice, Warning severity) | Retry once; if still fails, log; do not fail the cycle |
| SMS send fails (Critical severity) | Retry up to 3 times with exponential backoff; if still fails, log to durable state with elevated visibility |
| Both channels fail simultaneously | Log to durable state; surfaced as Warning at next successful cycle (operator may have lost both connectivity types) |

The principle: **alert dispatch is best-effort and does not gate
system operation.** The operator can always inspect logs to discover
events that should have alerted but didn't reach them.

### 9.4 Persistence interface

IRAPM persists state to local files on the CL260 box's pSLC SSD.
The persistence layer provides:

- **State file** — current operating state (§6.7 persistent state list)
- **CB transition log** — append-only log per §6.7 / §7.6
- **Cycle log** — per-cycle record of (timestamp, plan, execution
  outcomes, alerts dispatched). Used for audit and recovery. Plans
  are written here, not to the state file.
- **Annual review log** — per-year record of freeze decisions and
  recompute results (§6.7).
- **Token check log** — daily token observations.
- **Alert log** — every dispatched alert (success, failure, dedup
  suppressions).
- **Coordination log** — heartbeat and role-transition events.

#### 9.4.1 Write semantics

- **Atomic writes.** State files are written atomically (write to
  temp file, fsync, rename). A crash during write leaves the prior
  state file intact.
- **End-of-cycle persistence.** The state file is written once per
  cycle, after action execution completes (or fails). A crash
  during action execution is recovered from the prior state file
  plus broker-side reconciliation on next cycle.
- **Append-only logs.** All log files are append-only. Old entries
  are never modified. Rotation policy per §9.4.5.

#### 9.4.2 Master/slave coordination

IRAPM runs on two CL260 boxes in a master/slave configuration. Both
boxes have IRAPM installed; at any given time, exactly one is acting
as **master** (executing cycles, making decisions, taking actions)
and the other as **slave** (running only the lightweight daily slave
check, not running full cycles). The architecture is a hybrid of two
patterns: each box maintains a complete **local** state file (no
shared network dependency), and **the master's state write is itself
the heartbeat** (no separate ping daemon, no liveness pings to
miss).

The combination resolves the central tension in two-box HA: a shared
state location creates a third point of failure (the share / NAS /
network mount), but a separate ping mechanism can miss
application-level failures (the box pings fine but IRAPM is hung or
broken). The hybrid retains the resilience of local-only state (each
box can operate from its own disk if the other is destroyed) while
keeping the elegance of "state-write proves IRAPM is functioning
end-to-end."

**Architecture:**

- Each box stores its operating state file **locally** on its own
  pSLC SSD. There is no shared mount, no NAS, no network share, no
  cross-host filesystem.
- Master writes its state file at end of each cycle, plus a daily
  idempotent heartbeat write at a configured time (default 06:00 ET)
  regardless of whether a cycle ran that day.
- A `cron`-driven `rsync` job on master replicates its state file to
  slave's local disk daily (default 06:15 ET, after master's
  heartbeat). The rsync direction always matches the role:
  master → slave. (When roles swap, the rsync direction reverses;
  see "Replacing a failed box" below.)
- Slave runs a lightweight daily "slave check" cycle (default 06:30
  ET, after rsync completes) reading its **local** copy of the state
  file. It inspects `master_box_id` and `last_master_write_timestamp`
  to assess master liveness.

**Role state machine (per-box):**

Each IRAPM instance maintains a `role` state: `MASTER`,
`SLAVE_SLEEPING`, `SLAVE_PROMOTION_PENDING`, or `STARTING`. Role is
per-box infrastructure and is **not** part of the operating-mode
tuple (§6).

Role transitions:

| From | To | Trigger |
|---|---|---|
| STARTING | MASTER | On startup, local state file's `master_box_id` matches this box (or absent/never-initialized — initial deployment) |
| STARTING | SLAVE_SLEEPING | On startup, local state file's `master_box_id` is the peer |
| SLAVE_SLEEPING | SLAVE_PROMOTION_PENDING | Slave check observes staleness ≥ `slave_wake_staleness_hours` (default 72h) with no fresh heartbeat from master (rsync brings no updated state, or rsync arrives but `last_master_write_timestamp` has not advanced). Staleness in `[slave_healthy_threshold_hours, slave_wake_staleness_hours)` logs a notice but does not transition |
| SLAVE_PROMOTION_PENDING | MASTER | 48 hours elapsed since promotion-pending started, and master heartbeat is still stale |
| SLAVE_PROMOTION_PENDING | SLAVE_SLEEPING | Fresh master heartbeat arrives via rsync during the 48-hour grace window (auto-cancels promotion) |
| MASTER | SLAVE_SLEEPING | On observing a peer state-file write (via reverse rsync after repair, see below) that claims master role and is more recent than this box's last write |

**State-file fields used for coordination:**

- `master_box_id` — OS hostname or auto-generated UUID of the box
  currently holding the master role.
- `last_master_write_timestamp` — wall-clock timestamp of the most
  recent master write (cycle write or heartbeat write).
- `master_ipv4_last_octet` — recorded in each state-file write for
  split-brain tiebreaking (see §9.4.3).

**Slave detection of master failure:**

The slave-check cycle runs daily and performs these steps. All
timestamp comparisons operate in hours; configuration is in hours
throughout to eliminate days/hours mixing:

1. Read local state file (which was synced from master via the most
   recent rsync, if rsync succeeded).
2. Inspect `last_master_write_timestamp` and compute staleness
   in hours from `now`.
3. If staleness is less than `slave_healthy_threshold_hours`
   (default 24): master is healthy. Remain in SLAVE_SLEEPING.
4. If staleness is between `slave_healthy_threshold_hours` and
   `slave_wake_staleness_hours` (default 24 to 72): log a
   notice; remain in SLAVE_SLEEPING. (Single missed heartbeat is
   tolerated — could be a 24-hour cron miss or rsync glitch.)
5. If staleness is greater than `slave_wake_staleness_hours`
   (default 72) and this box is in SLAVE_SLEEPING: transition to
   SLAVE_PROMOTION_PENDING and record `promotion_pending_started_at`
   timestamp in local state. Fire a Critical alert: "Primary box
   offline — secondary will auto-promote in
   [slave_promotion_grace_hours] hours unless primary returns.
   Last master heartbeat [N] hours ago at [timestamp]."
6. If this box is already in SLAVE_PROMOTION_PENDING: compute
   elapsed time since `promotion_pending_started_at`. If at least
   `slave_promotion_grace_hours` (default 48) have elapsed since
   the SLAVE_PROMOTION_PENDING transition, AND master heartbeat is
   still stale (per step 5 condition), proceed to promotion (see
   below). The grace clock anchors to the SLAVE_PROMOTION_PENDING
   transition timestamp, not to the master's last heartbeat — this
   means the grace window's wall-clock duration is precisely
   defined regardless of cycle-runtime drift.
7. If this box is in SLAVE_PROMOTION_PENDING and a fresh heartbeat
   has arrived (staleness less than `slave_healthy_threshold_hours`):
   transition back to SLAVE_SLEEPING; clear
   `promotion_pending_started_at`. Fire an Info alert: "Primary
   heartbeat recovered. Promotion cancelled."

The 48-hour grace window allows the operator (or in Phase 3, the
survivor) time to reboot, repair, or replace the failed primary
before automatic promotion. No explicit acknowledgment is required
to cancel: if primary returns and resumes writing, slave observes
the fresh timestamp on the next rsync and cancels promotion
automatically.

**Promotion mechanic:**

When slave promotes (72-hour staleness + 48-hour grace elapsed):

1. Confirm staleness one final time by re-reading the local state
   file (defense against clock skew or last-minute rsync arrival).
2. Atomically write a new local state file with `master_box_id` set
   to this box and `last_master_write_timestamp` set to now.
3. Generate a Critical alert: "Slave promoted to master. Previous
   master last heartbeat [N] days ago. Now operating as master."
4. Reverse the rsync direction in cron configuration: this box's
   cron now pushes state to peer (peer becomes the rsync receiver
   when it returns online).
5. Begin operating as master. The next scheduled IRAPM cycle runs
   from the most-recent local state file (which may be up to 72 hours
   stale on portfolio data, but the broker query at cycle start
   refreshes actual positions before any decision is made).

**Role-swap, not failback.** When a failed primary returns online,
the system does **not** automatically restore it to master. The
currently-running master continues. The returning box detects the
new master state via the next rsync (now pushed *from* the new
master to it) and transitions to SLAVE_SLEEPING. This is a
deliberate design choice: role swaps reduce state-transition surface
area (every failback would be an additional transition with its own
failure modes) and the operator does not need to predict "which box
is currently primary" — they can inspect either box's local state
file to see `master_box_id`.

If the operator explicitly wants to swap roles back (e.g., the
original primary is preferred hardware), they perform a manual swap:

1. Stop IRAPM on the current master.
2. Wait 120 hours (72 hours staleness + 48 hours grace) for the other box to auto-promote.
3. Bring the original primary back online; it observes the new
   master and becomes slave.

Alternatively, operator may use a documented manual override
procedure (per the operational runbook, §14.3) that atomically
swaps `master_box_id` and reverses rsync direction without waiting
through the auto-promotion sequence.

**Repaired-box reintegration:**

When a failed master is repaired and brought back online:

1. IRAPM starts on the repaired box.
2. It reads its local state file (which has not been updated since
   failure; `master_box_id` may still be this box's ID with a stale
   timestamp).
3. It checks for incoming rsync data from the peer. Two cases:
   - **Slave has not yet promoted** (returned within the 120-hour
     window): peer is still in SLAVE_SLEEPING (or SLAVE_PROMOTION_PENDING).
     Repaired box's heartbeat will arrive at peer via the original
     rsync direction (which is still master → slave from repaired
     box's perspective). The repaired box continues as MASTER. Peer
     observes the fresh heartbeat on its next slave check and cancels
     any pending promotion.
   - **Slave has already promoted** (returned after the 120-hour
     window): rsync from peer (new master → this box, reverse
     direction) has been arriving. The repaired box's local state
     file is being updated by incoming rsync. It reads
     `master_box_id` and sees the peer is now master.
4. If peer is master: repaired box transitions to SLAVE_SLEEPING.
   Generate Info alert: "Restarted as slave — peer is now master."
   Stop attempting to write state as master; let incoming rsync
   continue to populate state file.
5. If repaired box has been gone long enough that **its local cron
   was driving rsync in the wrong direction** (e.g., it failed
   without realizing it, and cron tried to push stale state to peer
   for some time during the partition), this is benign in the
   hybrid: incoming rsync from new master always wins by
   overwriting on the receiving side. No special cleanup needed.

**Replacing a failed box (full hardware replacement):**

When the operator physically replaces a failed box with new
hardware:

1. New box is configured with IRAPM installed.
2. New box's local state file is empty / nonexistent.
3. Operator configures rsync on **the running master**: master pushes
   state to the new box on its next cron cycle.
4. New box receives state file via incoming rsync. Reads it. Sees
   `master_box_id` is the peer.
5. New box transitions to SLAVE_SLEEPING.
6. Operator configures the new box's local cron to receive rsync
   (no outbound rsync from new box). Future promotion will reverse
   this if needed.

**Configuration:**

- `master_heartbeat_time` — daily time for master heartbeat write
  (default: 06:00 ET).
- `rsync_replication_time` — daily time for rsync push from master
  to slave (default: 06:15 ET).
- `slave_check_time` — daily time for slave to read local state and
  check freshness (default: 06:30 ET).
- `slave_healthy_threshold_hours` — staleness threshold below which
  master is considered healthy (default: 24). At or above this
  threshold, a notice is logged but no promotion is initiated.
- `slave_wake_staleness_hours` — staleness threshold for slave to
  enter SLAVE_PROMOTION_PENDING (default: 72).
- `slave_promotion_grace_hours` — grace window before auto-promotion
  completes, measured from SLAVE_PROMOTION_PENDING transition
  timestamp (default: 48).
- Per-box rsync configuration (source/dest paths, current direction)
  is operational, not in shared ruleset.

All coordination-subsystem timing parameters are in hours.

**Clock synchronization assumption.** The coordination sequence above
relies on absolute wall-clock times (06:00 ET heartbeat, 06:15 ET
rsync, 06:30 ET slave check) rather than chained process triggers,
so the 15-minute operational gaps depend on both boxes' clocks
remaining aligned. Each CL260 box **must** run `chrony` as its NTP
daemon. `chrony` is required (not `systemd-timesyncd`) because
`systemd-timesyncd` is a single-source SNTP client that cannot
combine multiple upstreams, cannot detect a falseticker, and cannot
step the clock cleanly on boot after a long power outage — all
capabilities that matter for an unattended decade-scale deployment.

The chrony configuration uses a tiered upstream hierarchy on both
boxes:

```
# Primary Anycast sources — smeared leap seconds, Seattle-edge latency
server time.google.com iburst prefer
server time.cloudflare.com iburst

# Fallback pool — chrony combines and discards outliers
pool 2.us.pool.ntp.org iburst maxsources 4

# Step the clock if offset > 1s, unlimited times — survives multi-day
# RTC drift on unattended reboot after hardware swap or power loss
makestep 1.0 -1

# Write sync back to hardware clock every 11 minutes
rtcsync

# Persist drift rate across reboots
driftfile /var/lib/chrony/chrony.drift

# Default but explicit: discard sources claiming >3s root distance
maxdistance 3.0
```

Rationale for the chosen upstreams:

- **Leap-second smearing.** Google and Cloudflare both serve
  smeared leap-second time (24-hour smear, mutually compatible).
  Traditional stepped leap seconds have historically broken
  timestamp-arithmetic systems; smeared time eliminates that
  failure mode for `last_master_write_timestamp` comparisons and
  the 48-hour grace window math.
- **`prefer` on the smeared source.** Forces both boxes to default
  to Google's smeared time when available, preventing the case
  where one box picks a smeared source and the other picks a
  stepped Pool source and they disagree by up to 1 second during
  a leap-second event.
- **Pool as fallback.** Provides survivability if Google or
  Cloudflare have a DNS or Anycast routing failure. chrony's
  combine-and-discard algorithm tolerates Pool jitter gracefully.
- **`makestep 1.0 -1`.** Unlimited step corrections, but only when
  offset exceeds 1 second. This lets the system self-heal from
  multi-day RTC drift across the decade (e.g., box powered off
  during hardware repair, returning with a several-minute clock
  error) without burning a finite step-allowance budget.

NTP UDP/123 egress must be permitted in any firewall configuration
on the deployment network.

Without active time synchronization, decade-scale clock drift on an
unattended fanless box can accumulate to multiple minutes,
eventually collapsing the heartbeat → rsync → slave-check gap and
producing spurious staleness or missed rsync windows. NTP failure
is non-catastrophic in the short term (the staleness thresholds are
in hours, not minutes), but is a slow corrosive that must be
detected and corrected by the operational runbook (§14.3) rather
than tolerated indefinitely. NTP daemon health is a
deployment-checklist item, not a runtime-enforced invariant — the
system does not self-monitor clock drift.

#### 9.4.3 Split-brain prevention and resolution

Two failure modes can produce a split-brain scenario where both
boxes believe they are master:

- **Network partition during slave promotion:** the boxes lose
  network connectivity to each other (rsync cannot flow); slave
  reaches the 120-hour threshold and promotes; meanwhile, the
  original master is actually fine but unreachable. When the
  network heals, both boxes are running cycles as master.
- **Simultaneous startup with stale state files:** unusual case
  where both boxes restart simultaneously and both local state
  files claim this-box-is-master (possible if both boxes failed,
  were restored to old backups independently, and then restarted).

The first scenario is the dangerous one. Without prevention, both
boxes would execute cycles against the same IBKR account
concurrently, potentially placing duplicate orders or conflicting
ACH updates. The hybrid model addresses both prevention and
resolution.

**Prevention: rsync direction enforces single-writer.**

Under normal operation, exactly one box is the rsync source and
exactly one is the receiver. Slave's role does NOT include writing
state — only reading its local state file (which is populated by
incoming rsync from master). If slave does not promote, no
split-brain can occur regardless of network state, because slave
never writes state.

Slave's promotion is gated on 72-hour staleness + 48-hour grace. This
is the only mechanism by which slave begins writing state and
acting as master. During the partition that causes the staleness,
the original master continues running cycles and writing its local
state — but its writes do not propagate to slave (rsync is failing).
When slave eventually promotes, its first action is to atomically
update its local state file and reverse rsync direction.

Once both boxes are running cycles as master, the next time the
network heals (rsync resumes in some direction), one of them sees
the other's state file and the resolution mechanic activates.

**Resolution: IPv4 last-octet tiebreak.**

When network heals and the boxes detect each other claiming master:

1. The first box to receive a peer state-file rsync (with peer
   claiming master role and a recent `last_master_write_timestamp`)
   triggers split-brain resolution.
2. Both boxes compare their own IPv4 last octet against the peer's
   (recorded in each state-file write via `master_ipv4_last_octet`).
3. **Higher last octet wins** — that box stays MASTER. Its rsync
   direction (outbound) is correct.
4. **Lower last octet** transitions to SLAVE_SLEEPING and reverses
   its rsync direction to incoming-only.
5. Both boxes generate Critical alerts: "Split-brain detected and
   resolved during partition recovery. This box is [winner / loser].
   Operator review required to identify any conflicting trades
   executed during partition."
6. The losing box's cycle log from the partition period is preserved
   and surfaced in the alert body for operator review.

**Tiebreak rule edge cases:**

- **Same last octet (different subnets):** out of scope. Both
  CL260 boxes must be deployed on the same subnet for the
  master/slave coordination model (rsync, heartbeat, state sync)
  to function. Deployment on different subnets is unsupported and
  would impair state synchronization independent of any tiebreak
  considerations. The tiebreak rule (last-octet comparison) is
  reliable on same-subnet deployments and is the only supported
  configuration.
- **Boxes never detect each other during partition:** if rsync never
  resumes (e.g., one box is destroyed, the other has been
  partitioned indefinitely), the promoted box continues operating
  alone. This is the expected steady state after permanent loss of
  the original primary. No alert until either: (a) repaired or
  replacement box brings rsync back, or (b) an operator-driven
  health check detects the asymmetric configuration.

**Why prevention is stronger here than in the shared-state model:**

In a shared-state model, network partition typically halts both
boxes (neither can write to shared state). Availability suffers.

In the hybrid rsync model, network partition halts replication but
neither box halts on its own. The original master continues
operating from local state. Availability is preserved at the cost
of accepting the possibility of split-brain at promotion time —
which is then resolved by tiebreak when the network heals.

This trade-off is appropriate for IRAPM's cadence: cycles run
weekly, withdrawals run monthly, and the chance of a single
partition lasting >5 days *and* coinciding with operationally
significant events is low. When it does happen, the tiebreak
resolves cleanly and the operator reviews the cycle log to confirm
no actual harm was done.

#### 9.4.4 Persistence failure modes

| Failure | IRAPM response | Category |
|---|---|---|
| Local state file write fails | Cycle ends in inconsistent state; abort | Critical-blocking |
| Local state file missing on startup | If never-initialized: bootstrap as MASTER on initial deployment, else refuse to start and alert | Critical-blocking (post-deployment) |
| Local state file corrupt on startup | Refuse to start; alert; operator restores from peer box's copy | Critical-blocking |
| Rsync push fails (master side) | Log; cycle does not fail; next day's cron retries | Warning (auto-recovers) |
| Rsync receive fails / no incoming data (slave side) | Slave check sees no fresh `last_master_write_timestamp`; staleness accumulates per §9.4.2 promotion logic | Notice → Critical on promotion threshold |
| Disk full | Cycle ends in failure | Critical-blocking |
| Split-brain detected | Apply IP-last-octet tiebreak; loser → SLAVE_SLEEPING; operator review of partition-period cycle log | Critical-blocking (both boxes alert) |
| Split-brain unresolvable (same last octet) | Both boxes refuse to operate; manual resolution required | Critical-blocking |

State persistence is the most failure-critical interface. The system
prioritizes refusing-to-run over running-with-bad-state.

#### 9.4.5 Log rotation

- **Daily rotation** for: cycle log, coordination log, alert log,
  token check log.
- **Retention 90 days** for cycle/coordination/alert/token logs.
- **Indefinite retention** for CB transition log and annual review
  log (small data volume; needed for freeze evaluation and audit).
- **Off-box backup of logs** is OUT OF SCOPE for IRAPM. The operator
  may use OS-level tools (rsync, NAS replication, cloud sync) to
  back up logs; IRAPM does not provide this functionality.

### 9.5 Hardware token interface

Detailed in §10. The tokens interact with IRAPM as a read-only state
that the cycle reads at step 1 (input refresh).

### 9.6 Time and scheduling interface

IRAPM cycles are scheduled by systemd timers (Linux) or equivalent.
The IRAPM process itself does not run a scheduler; it is invoked
once per cycle by the OS scheduler.

Scheduled cycles (per §3.8 and §6.5.1):

- **Weekly primary cycle** — runs on a configured day/time
  (default: Wednesday 10:00 ET). Performs full state evaluation,
  decision generation, and action execution.
- **Daily token check** — runs at 09:00 daily. Minimal scope: token
  state read, write to local state file, alert on mismatches.
- **Monthly withdrawal cycle** — runs on a configured day of month.
  Must be **at least 2 trading days before** the IBKR recurring ACH
  withdrawal date. Performs the standard weekly cycle work plus the
  monthly withdrawal SELLs to fund the upcoming IBKR ACH transfer.
- **Annual review cycle** — runs on the configured annual review
  date (default: January 15; shifts to next trading day if weekend
  or market holiday). Performs the standard weekly cycle work plus
  the annual review evaluations (§7.6).

The scheduler configuration lives outside IRAPM (in systemd unit
files). IRAPM is invoked with a `--cycle-type` argument that tells
it which evaluations and actions to run.

**Cycle-type collisions** are handled by precedence: annual-review >
monthly-withdrawal > weekly > daily-token. The more comprehensive
cycle type subsumes the less comprehensive when they fall on the same
day.

#### 9.6.1 Settlement separation between SELL and ACH

US equity ETFs settle T+1. ACH withdrawals require **settled** cash;
same-day SELL + ACH initiation will fail at the broker. IRAPM handles
this constraint at the **scheduling layer**, not in mid-cycle delays:

- The monthly withdrawal step within the weekly cycle's SELL orders fund cash that becomes
  withdrawable T+1 (next trading day after fill).
- The IBKR recurring ACH transfer happens on a separately-configured
  day of the month.
- Scheduling rule: the monthly withdrawal step within the weekly cycle must run **at least 2
  trading days before** the IBKR ACH date, providing a settlement
  buffer.

Example: if the IBKR ACH transfer runs on the 5th of each month
(assume the 5th is a Wednesday), the monthly withdrawal step within the weekly cycle should
run on or before Monday the 3rd (2 trading days back). SELLs fill
same-day; cash settles by Tuesday the 4th; the ACH transfer initiates
on Wednesday the 5th with settled cash available.

Holiday/weekend shift: if the scheduled withdrawal cycle day falls on
a market-closed day, it shifts to the **next trading day**. The
operator should configure the IBKR ACH date with enough buffer that
the +1-day shift still leaves ≥2 trading days of settlement runway.

For SELL + BUY operations within the same cycle (rebalancing,
buffer refills, phase transitions), no settlement separation is
needed: IBKR allows unsettled SELL proceeds to fund same-day BUY
orders within an IRA.

---

## §10. Hardware Tokens

IRAPM uses physical USB tokens to control two distinct system
behaviors:

- **Phase 3 activation** — four "Phase 3" tokens whose physical
  removal triggers the Phase 1/2 → Phase 3 transition.
- **Income pause** — one or more "STOP INCOME" tokens whose presence
  pauses scheduled withdrawals.

The token mechanism is the system's interface to **operator life
events**. It is the only mechanism by which the survivor (or the
operator themselves, in the income-pause case) communicates with the
running system without requiring login credentials, network access, or
software interaction.

### 10.1 Design principles

The hardware-token mechanism is governed by these principles:

- **Physical action, not logical.** The trigger is the physical
  presence (or absence) of a USB token. No GUI button, no API call,
  no command-line invocation, no software override can substitute.
  This is deliberate — the survivor scenario assumes the operator is
  unavailable to log in, the survivor lacks system credentials, and
  the survivor's tech-support resources cannot be assumed to include
  anyone with system-administration skill. **There is no software
  override path** for either token type; the hardware-only constraint
  is a load-bearing element of the system's survivor-safety model.
- **Two-box AND semantics.** Phase 3 activation and STOP INCOME both
  require **both** CL260 boxes to agree on the token state. A single
  box reading a state change is insufficient. This protects against
  hardware failures (one box's USB controller fails), single-token
  failures (one drive fails in-place), and accidental token
  disturbance affecting only one box.
- **Type+presence detection, not serial-number identification.** The
  system identifies tokens by their type and quantity, not by
  individual serial numbers. The cycle confirms "4 Phase 3 tokens
  present" or "1 STOP INCOME token present per box", not "tokens
  with serial numbers X, Y, Z, W are present." Rationale: replacing
  a failed token shouldn't require a configuration update; misreading
  a serial number shouldn't trigger a false negative.
- **Inverse normal-state semantics by token type.** The two token
  types have opposite normal states, each chosen to match its purpose:
  - **Phase 3 tokens are normally INSERTED.** All four Phase 3 tokens
    (2 per box) reside continuously in their USB ports throughout
    Phase 1 and Phase 2 operation. Phase 3 activation triggers when
    all four are physically **removed**.
  - **STOP INCOME tokens are normally REMOVED.** Each box has one
    STOP INCOME token slot kept empty during normal operation. The
    STOP INCOME tokens themselves reside in a powered USB charger
    next to the CL260 boxes (continuously powered, accessible but
    out of slot). Income pause triggers when STOP INCOME tokens
    are physically **inserted** into both boxes.
- **Rationale for inverse semantics.** Two design pressures drive
  this asymmetry:
  - **Bitrot prevention for Phase 3 tokens.** Industrial USB tokens
    held continuously inserted are kept alive by the host's normal
    USB I/O activity (periodic media-scan, error-correction read
    cycles, controller refresh). A token sitting unused in a safe
    deposit box or drawer for 10–20 years is at non-trivial risk of
    silent data corruption — the kind of failure that would manifest
    only at the moment of greatest need. Continuous insertion keeps
    Phase 3 tokens in-circuit and integrity-maintained indefinitely.
    The STOP INCOME tokens, by contrast, are used routinely (every
    pause/resume cycle) and don't accumulate the decade-scale unused
    period that drives bitrot risk. The charger slot keeps them
    powered (preventing flash-cell decay) without requiring an
    occupied USB slot on the CL260s.
  - **No retrieval-during-grief problem.** Phase 3 tokens being
    already present at trigger time means the survivor doesn't need
    to find a safe deposit box key, travel to a bank, or coordinate
    with anyone external to obtain the tokens. The activation action
    is a single physical removal that can happen at the boxes
    themselves with whatever support the survivor has on-site.
- **Grace window for accidental Phase 3 removal.** Phase 3
  activation has a 24-hour delay between detection and latch, giving
  the operator (or survivor, if removal was accidental) time to
  recover from incidental token disturbance — cleaning, dust, child
  or pet, contractor working in the area, etc.
- **No grace window for STOP INCOME.** STOP INCOME state changes are
  intentional operator actions. Insertion takes effect on the next
  daily-token cycle (≤24 hours latency, no grace window). Removal
  also takes effect on the next daily-token cycle.
- **Permanent latch for Phase 3.** Once the 24-hour grace window
  elapses without re-insertion, Phase 3 is latched. Subsequent
  re-insertion of the tokens does NOT exit Phase 3.
- **Reversible STOP INCOME.** STOP INCOME insertion and removal can
  toggle the income state any number of times during Phase 1 or
  Phase 3. This implements the reversible-where-possible operational
  principle (§1.4). In Phase 2, STOP INCOME is a no-op per §4.4.

### 10.1.1 Physical security model

The hardware-token mechanism's reliability depends on a physical
security configuration that is operator-maintained but assumed by
IRAPM. The system specification does not enforce these
configurations (there is no software check that the enclosure is
secured), but the threat model and operational behavior below
assume the configuration is in place.

**Enclosure.** Both CL260 boxes are housed in a single
expanded-metal cabinet with a hinged access door. The door is
**safety-wired shut** during normal operation. Defeating the
enclosure requires tools (wire cutters or pliers) and visible
disturbance — the access pattern is high-friction enough to rule
out casual or accidental tampering, while remaining accessible to
the operator (or survivor with on-site support) when intentional
access is needed.

**Token chaining.** All tokens are physically chained to their
respective CL260 boxes via short braided-steel lanyards. This
prevents:
- Loss during cleaning or other incidental contact with the cabinet
- Removal-and-pocketing during any access event
- Accidental disposal during operator-life-event chaos

**Phase 3 token layout.** Two Phase 3 tokens per box (4 total),
each chained to its respective box, each inserted continuously into
a USB port. The chain length permits removal-for-trigger but not
relocation.

**STOP INCOME token layout.** Each box has one STOP INCOME token,
chained to the box, residing in a powered USB charger slot adjacent
to the box during normal operation. To trigger pause, the token is
moved from the charger slot into a USB port on the CL260; the chain
length permits this. Spare STOP INCOME tokens (2 per box, also
chained) reside in adjacent charger slots; their function is
hot-spare replacement of the primary token if it fails. The chargers
keep all tokens powered to prevent the inverse-bitrot problem
(flash-cell decay in entirely unpowered tokens).

**Threat coverage:**

| Threat | Mitigation |
|---|---|
| Cats, incidental physical contact | Enclosure blocks contact entirely |
| Casual human tampering | Safety-wire makes access deliberately high-friction |
| Token loss (drop / displacement) | Chain attachment prevents loss |
| Phase 3 token bitrot over decades | Continuous insertion + USB controller activity |
| STOP INCOME token bitrot in storage | Powered charger slot keeps tokens alive |
| Single USB port hardware failure | AND-across-boxes semantics requires both boxes to fail |
| Single token hardware failure (Phase 3) | AND-across-boxes: peer box's token preserves correct state |
| Single token hardware failure (STOP INCOME) | Hot-spare tokens in chargers; operator swaps in spare |
| Sophisticated adversary with physical access and tools | **Not covered.** The system assumes operator-grade physical security, not adversarial security. |

The threat model is incidental and accidental, not adversarial. A
determined adversary with physical access, time, and tools can
defeat the enclosure; this is an accepted limitation, consistent
with the broader assumption that the boxes reside in a controlled
private space.

### 10.2 Token types and counts

The system uses two distinct USB token types, distinguishable by
device characteristics (vendor ID, product ID, capacity, filesystem
label, or similar properties detectable without inspecting serial
numbers).

| Token type | Normal state | Per-box active count | Per-box spares | Total active | Total physical | Purpose |
|---|---|---|---|---|---|---|
| Phase 3 token | INSERTED in USB port | 2 | 0 | 4 | 4 | Phase 3 activation (removal triggers) |
| STOP INCOME token | REMOVED, in adjacent charger | 1 | 2 | 2 | 6 | Income pause (insertion triggers) |

**Active count** is the number of tokens IRAPM expects to detect by
type during normal cycle operation. **Spare count** is additional
physical tokens stored powered-but-not-in-USB-port (STOP INCOME
chargers); these are not detected during normal cycle operation
and serve as hot-spares for token failure.

Detection logic per §10.3:

- **Phase 3 token detection** counts tokens-of-correct-type currently
  inserted in CL260 USB ports. Active count = 2 per box (4 total).
  Removal trigger fires when the inserted count drops to 0 on both
  boxes.
- **STOP INCOME token detection** counts tokens-of-correct-type
  currently inserted in CL260 USB ports. Active count = 1 per box
  (2 total) when income is paused, 0 per box when income is
  active. The spare tokens in chargers are not counted (they are
  not inserted in CL260 USB ports).

Each box has a USB hub configuration that physically separates the
two token types (different ports, labeled or color-coded). The
operator maintains spare STOP INCOME tokens in the charger slots
for replacement of failed units (per §10.1.1 physical security
model).

### 10.3 Token detection mechanism

Each box runs a daily-token cycle independently. Token observations
are exchanged between boxes via the same rsync mechanism that
replicates the operating state file (per §9.4.2). The current master
consults both boxes' observations to derive system-level token
state:

1. Each box runs its own daily-token cycle on its own schedule
   (default 09:00).
2. The cycle enumerates USB devices, classifies each as Phase 3 token
   / STOP INCOME token / other, and counts per-type.
3. The cycle writes its observation to a local **token observation
   file** (`token_observation.json`, separate from the operating
   state file): `(box_id, timestamp, phase3_count, stopincome_count, status)`
   where status is one of READABLE, UNAVAILABLE. The slave's
   observation file is replicated to master via a **dedicated
   slave→master rsync** of the observation file (separate from the
   master→slave operating-state-file replication described in
   §9.4.2), scheduled to run shortly after the slave's daily-token
   cycle completes. The operating state file remains master-write-only;
   the single-writer rationale for split-brain prevention (§9.4.3)
   is unaffected because the token observation file is not used for
   master/slave promotion decisions.
4. After both boxes' observations have been collected on master,
   master derives the system's token state:
   - Phase 3 tokens removed: BOTH boxes report `phase3_count == 0`.
     Either box reporting `phase3_count > 0` means tokens still
     present.
   - STOP INCOME inserted: BOTH boxes report `stopincome_count >= 1`.
   - STOP INCOME removed: BOTH boxes report `stopincome_count == 0`.
   - Mismatches: see §10.5.

Observation freshness gates this: master uses only slave
observations that are within the past 24 hours. Older observations
are treated as UNAVAILABLE for the affected box.

### 10.4 UNAVAILABLE state

A box's token observation can fail for various reasons (USB driver
issue, system call failure, file lock, etc.). When a box cannot read
its USB devices reliably, it reports `status: UNAVAILABLE` instead of
a count.

UNAVAILABLE handling:

- The system **holds its previous token state** while a box is
  UNAVAILABLE. No Phase 3 activation, no STOP INCOME state change.
- Generic alert (Notice severity): "Box [id] cannot read tokens."
  Generic — does NOT specify whether Phase 3 or STOP INCOME tokens
  are affected, since the box can't tell.
- If UNAVAILABLE persists for >2 daily cycles, alert escalates to
  Warning.
- If UNAVAILABLE persists for >7 daily cycles on the same box, alert
  escalates to Critical (Critical-blocking — operator must
  investigate).
- If both boxes report UNAVAILABLE simultaneously, immediate Critical
  alert (token monitoring functionally lost).

The conservative "hold previous state" rule prevents UNAVAILABLE from
being exploited as a Phase 3 activation path. A box with a broken
USB controller cannot trigger Phase 3 by failing to see the tokens.

### 10.5 Mismatch handling and token-state validity

The token system has a tightly-constrained notion of **valid
system-level states**. Anything outside these is treated as
invalid and triggers hold-previous-state with an alert.

**Valid Phase 3 token states** (system-level, across both boxes):

- **All-inserted:** Box A `phase3_count == 2` AND Box B
  `phase3_count == 2`. Total = 4. Normal operating state.
- **All-removed:** Box A `phase3_count == 0` AND Box B
  `phase3_count == 0`. Total = 0. Triggers Phase 3 activation
  flow per §10.6.

Any other Phase 3 token configuration — partial counts on either
box (1 per box, 1 on one and 2 on the other, etc.), asymmetric
states between boxes — is **invalid** in normal operation. Note
the exception: during the Phase 3 grace window (§10.6.2), partial
counts have explicit meaning — any nonzero token count on either
box is a candidate abort signal (with one-cycle persistence
confirmation) rather than an invalid state.

**Valid STOP INCOME token states** (system-level):

- **All-removed:** Box A `stopincome_count == 0` AND Box B
  `stopincome_count == 0`. Income state is ACTIVE.
- **All-inserted:** Box A `stopincome_count >= 1` AND Box B
  `stopincome_count >= 1`. Income state transitions to PAUSED.

Any other STOP INCOME configuration (one box inserted, other
removed) is **invalid**.

**System response to invalid states:**

- The system **holds its previous state** until a valid state is
  observed (per §10.5.1).
- Alert (Warning severity): "Token state invalid — Box A reports
  [counts], Box B reports [counts]. Expected valid state
  [all-inserted / all-removed]. Holding previous state."
- If the invalid state persists for >2 daily cycles, the alert
  escalates to Critical (configurable:
  `token_mismatch_critical_cycles`, default 2).

Invalid states are the expected pattern at the **start** of a
legitimate state change: one box's daily-token cycle runs before
the other's, so for some hours the boxes report different counts.
The 2-cycle escalation threshold allows for normal asynchronous
detection without nuisance escalation while ensuring genuine
persistent invalid states surface promptly. Hardware failures,
single-token failures, accidental partial removal, and tampering
attempts all manifest as invalid intermediate states.

For the special case where the invalid state occurs **during the
Phase 3 grace window**, see §10.6.2: the grace window uses its
own pattern logic (any-re-insertion aborts with one-cycle
persistence confirmation) rather than the general
hold-previous-state mismatch rule. The grace window is the only
context where partial / asymmetric token counts have an
explicit non-hold interpretation.

#### 10.5.1 Hold-previous-state as a named design principle

The hold-previous-state behavior on invalid token configurations
is a deliberate design choice with a specific safety property:
**state changes require positive confirmation from both boxes in a
specifically-defined valid configuration**. A single point of
failure — one failed USB token, one glitched box, one accidentally-
disturbed port — cannot produce a state change, because the
resulting partial / asymmetric reading is not a valid system-level
state. Combined with invalid-state alerts (§10.5), the system is
robust against single-token failures, single-box glitches, and
most accidental tampering scenarios.

The alternatives considered and rejected:

- **Debounce timer** — declare new state final after a stable
  observation period (e.g., 60 seconds of consistent readings).
  **Rejected.** Adds timer state and logic for negligible benefit:
  the hold-previous-state behavior already handles the transient
  mismatch case correctly, and the daily-token cycle cadence makes
  finer-grained timing impractical. A debounce timer also creates
  an ambiguous middle period ("which state are we in right now?")
  that hold-previous does not.
- **Apply most-recent change** — assume the latest box change is
  intentional and apply that state immediately. **Rejected.** A
  single box failure or glitch could trigger an unintended state
  change. This violates the single-point-of-failure-cannot-trigger
  property that is the entire reason for two-box AND semantics.
- **Apply most-recent change with confirmation** — apply latest box
  change but require peer confirmation within N hours, else revert.
  **Rejected.** More complex than hold-previous with no additional
  safety benefit, and creates a window during which the system has
  acted on unconfirmed input.

The hold-previous-state choice is consistent with the broader
fail-safe-defaults principle (§2.7): when the system cannot proceed
safely, it does nothing and alerts. Token mismatch is exactly such
a case — the correct action is to wait for the boxes to agree, and
to alert the operator while waiting.

### 10.6 Phase 3 activation flow

Phase 3 activation proceeds in stages:

#### 10.6.1 Stage 1: Detection

- A daily-token cycle observes `phase3_count == 0` on both boxes
  (AND semantics).
- The system records the **detection timestamp** in the master's
  state file as `phase3_grace_window_start` (replicated to slave
  via rsync per §9.4.2).
- Generates a Critical alert: "Phase 3 tokens removed on both boxes.
  24-hour grace window started. Re-insert tokens before [timestamp +
  24h] to abort activation."
- Phase indicator unchanged at this stage. System continues operating
  in current phase (Phase 1 or Phase 2).

#### 10.6.2 Stage 2: Grace window

For the next 24 hours, daily-token cycles continue to run. Each
cycle's observation is classified against just two patterns:

**Pattern A: Any token re-inserted (Box A `phase3_count >= 1` OR
Box B `phase3_count >= 1`).** Candidate abort. The system requires
**persistence across one full daily-token cycle** before committing
the abort, to defend against transient USB-read glitches:

- On first observation of `phase3_count >= 1` on either box: log a
  Notice alert ("Phase 3 grace: token re-insertion observed,
  pending confirmation on next daily cycle"). Grace window
  continues running.
- On the next daily-token cycle, re-evaluate:
  - If `phase3_count >= 1` still observed on at least one box:
    **abort is committed**. `phase3_grace_window_start` is cleared
    from the state file. Generate `phase3_grace_aborted` Info alert
    (per §12.6 catalog). System continues in previous phase.
  - If both boxes return to `phase3_count == 0`: candidate abort
    is discarded as a transient. Grace window continues normally
    toward latch.

This 2-cycle persistence rule typically adds ~24 hours of
additional grace-window time in the survivor scenario (one cycle
to observe, one cycle to confirm). The trade-off is intentional:
asymmetry of consequences strongly favors easy-to-abort. A false
latch is permanent and irrevocable; a false abort means the
survivor's tokens "didn't quite work the first time" — they
remove the tokens again and the grace window restarts cleanly on
the next daily-token cycle. The persistence requirement closes
the transient-glitch false-abort path while preserving the
asymmetry advantage.

**Pattern B: All-removed (Box A `phase3_count == 0` AND Box B
`phase3_count == 0`).** The grace window **continues** running:
this is the legitimate triggered state, persisting toward latch.

**Latch decision at +24h.** When the grace timer reaches 24 hours
with no committed abort: the system enters Stage 3 (latch) per
§10.6.3.

The grace window timer is calendar time (24 hours by clock), not
cycle count. A late-running cycle does not extend the window.

**Rationale for the any-re-insertion-aborts rule.** This is a
deliberate departure from the earlier all-four-required-to-abort
rule. The asymmetry of consequences drives it: a false Phase 3
latch is permanent, irrevocable, and reallocates the portfolio
to the wrong target weights with the wrong starting income; a
false abort costs 24 hours plus another removal attempt. Strongly
asymmetric stakes argue for the easier-to-abort rule. The audience
this matters most for is the survivor who may be confused, grieving,
or unfamiliar with the system; the rule that lowest-barrier-to-abort
is safest for that audience.

#### 10.6.3 Stage 3: Latch

At the end of the 24-hour grace window with tokens still removed:

1. Update the state file: `phase = PHASE_3` (latched, permanent).
2. Generate a `phase3_activation` Critical alert: "Phase 3 LATCHED
   at [timestamp]. Asset reallocation executing on next weekly cycle."
3. The next weekly cycle (which runs within at most 7 days, typically
   within 24-48 hours) executes the Phase 3 transition plan per
   §7.2.2 or §7.2.3, **regardless of CB state**.
   The system enters Phase 3 operational logic immediately upon
   transition execution. If cascade conditions are present at or
   after transition, Phase 3's withdrawal sourcing handles them
   per §7.3.2 (the cascade machinery is phase-agnostic in Phase 1
   and Phase 3).
4. Subsequent token states (re-insertion, removal, mismatch) have
   no effect on phase. The latch is permanent.

The Phase 3 transition cycle is not deferred when cascade is
active. The transition is a one-time allocation reset, not a
rebalance; the survivor needs the Phase 3 income calculation in
effect, and cascade sourcing protects the portfolio during the
transition cycle just as it does during normal operation. The
transition may trade smaller Growth volumes if CB1 is active (I14
still applies during transitions), but the transition itself
proceeds.

### 10.7 STOP INCOME flow

STOP INCOME has no grace window; it transitions on the next
daily-token cycle that observes consistent state across both boxes.

**Insertion (Active → Paused):**

1. Daily-token cycle observes `stopincome_count >= 1` on both boxes
   (AND).
2. Income state transitions to PAUSED.
3. Generate Notice alert: "Income paused — STOP INCOME tokens
   detected on both boxes."
4. Next monthly withdrawal step within the weekly cycle plans an `ACHScheduleUpdate` entry
   with amount $0 (per §8.2.7), and skips the SELL+ACH withdrawal
   actions for that month.

**Removal (Paused → Active):**

1. Daily-token cycle observes `stopincome_count == 0` on both boxes
   (AND).
2. Income state transitions to ACTIVE.
3. Generate Notice alert: "Income resumed — STOP INCOME tokens
   removed from both boxes. Schedule resumes at $[current scheduled
   amount]."
4. Next monthly withdrawal step within the weekly cycle plans an `ACHScheduleUpdate` entry
   restoring the schedule_state-derived monthly amount, and resumes
   normal SELL+ACH withdrawal actions.

In Phase 2, both flows still record state changes for audit purposes
but produce no behavioral effect (no withdrawals exist to pause or
resume).

#### 10.7.1 Stuck-token alert (long-duration pause)

If the STOP INCOME state has been PAUSED continuously for more than
12 months (configurable: `stopincome_stuck_alert_months`, default 12),
the daily-token cycle emits a Notice-severity alert at the next
boundary and at quarterly intervals thereafter while pause persists:

> "STOP INCOME has paused scheduled income for [N] months. Confirm
> intent: continue pausing, or remove tokens from both boxes to
> resume scheduled income."

This catches the "forgot STOP INCOME was inserted" failure mode —
particularly relevant for the survivor scenario where the survivor
inserted STOP INCOME early in Phase 3 to defer income while living
on SS + HYSA, and then either (a) forgot the deferral was active
or (b) circumstances changed (HYSA drawdown, increased expenses)
without prompting reconsideration of the pause.

The alert is informational and does **not** auto-resume income.
Failure to respond does not change system behavior; the alert
serves only to re-surface the paused state for operator review.

The 12-month threshold and quarterly cadence are chosen to be
infrequent enough to avoid nuisance, while frequent enough that an
operator who genuinely forgot will be reminded within a reasonable
window. The cadence is suppressed if income resumes (Paused →
Active) and resets if income re-pauses.

### 10.8 Configuration

Token-related configuration in ruleset.yaml:

- `phase3_grace_window_hours` — default 24
- `phase3_token_count_required` — default 4 (2 per box × 2 boxes); valid configurations are 4 (all-inserted) and 0 (all-removed) per §10.5
- `stopincome_token_count_required` — default 2 (1 per box × 2 boxes); valid configurations are 0 (income active) and 2 (income paused; one per box) per §10.5
- `stopincome_stuck_alert_months` — default 12 (threshold for
  long-duration-pause alert per §10.7.1)
- `stopincome_stuck_realert_months` — default 3 (re-alert cadence
  once stuck-token threshold is exceeded)
- `token_unavailable_warning_cycles` — default 2
- `token_unavailable_critical_cycles` — default 7
- `token_mismatch_critical_cycles` — default 2

Token type detection rules (vendor ID, product ID, etc.) live in
per-box configuration outside ruleset (since they're hardware-
specific).

### 10.9 Operational considerations

- **Spare tokens.** The operator maintains physical spares for both
  token types. Failed tokens are replaced by inserting a spare into
  the same port. The system detects the new token by type+presence
  and the count is restored.
- **Token failure during Phase 3 grace window.** If a Phase 3 token
  fails (reads as removed while inserted) on a single box, the
  asymmetric count is detected by §10.5's mismatch logic and fires
  a Warning alert (escalating to Critical after
  `token_mismatch_critical_cycles` daily cycles). The grace window
  does NOT start. Phase 3 activation only begins when **all four**
  Phase 3 tokens read as removed on both boxes simultaneously — a
  state that requires either deliberate operator action or
  simultaneous hardware failure of all four tokens, which the
  operator considers practically impossible (and which would
  coincide with conditions affecting the boxes themselves, addressed
  at the hardware infrastructure layer rather than within IRAPM).
- **Operator-initiated test of Phase 3 mechanism.** Periodically the
  operator should verify Phase 3 activation works end-to-end without
  actually triggering a transition. This is handled via the dry-run
  cycle mode (§13.7).

---

## §11. Failure Modes & Recovery

This section enumerates the system's failure modes and how the
operator (or survivor) restores normal operation. The principle:
**failures should never silently degrade the system.** Every failure
that prevents normal operation produces a visible alert, leaves the
system in a known state, and has a documented recovery procedure.

### 11.1 Severity model

Four severity levels:

| Severity | Definition | Channels | Retry behavior |
|---|---|---|---|
| Info | Routine state transition or success notification | Email + SMS | None |
| Warning | Non-blocking issue; system continues | Email + SMS | None on alert dispatch |
| Notice | Significant state change requiring operator awareness | Email + SMS | 1 retry on SMS dispatch failure |
| Critical | Issue that requires operator action; may or may not block operation | Email + SMS | 3 retries on SMS dispatch failure |

Critical alerts carry an additional **operator-relevance category**
orthogonal to severity. The category communicates the system's
actual status after detection — what the operator needs to do, if
anything — rather than describing timer behavior. Three named
categories:

- **Critical (hard broke):** the system genuinely cannot proceed
  without external action. Either retrying achieves nothing (the
  underlying condition cannot be fixed by waiting — a software
  bug, configuration error, broker-data invariant violation) or
  retrying could cause harm (acting against compromised state).
  Sets `operational_pause` per §11.3.1 with one of the two
  hard-broke pause reasons (`internal_consistency_violation`,
  `broker_inconsistency`); no auto-resume. The withdrawal-only
  variant uses the `withdrawal_capacity_exhausted` flag per
  §11.3.2 — same category, different state field.
- **Critical (self-healed weird):** something unexpected happened,
  but the system already recovered automatically by the time the
  alert fires. The recovery was performed by other mechanisms —
  the broker layer's pre-place idempotency lookup, the IP last-
  octet tiebreak, the next cycle's fresh-state re-read, or a 48h
  retry of a transient failure. The alert exists for operator
  awareness (forensics, runbook reference, trust calibration),
  not for operator action. Some self-healed-weird cases set
  `operational_pause` with auto-resume (the 48h-retry variants:
  `partial_phase_transition`, `order_rejection`, `disk_full`);
  others set no pause at all (the already-healed variants:
  `split_brain_detected`, `external_activity_overlap`, transient
  broker-layer cases). Either way, the system continues operating.
- **Critical (normal-ops notice):** routine state transitions
  the operator should see in their alert stream for cadence
  reporting — phase transitions, CB transitions, scheduled
  withdrawal execution. The system is in its expected state;
  the alert is informational. No pause; no retry; no operator
  action required.

The framework is operator-absence-aware (§1.4). Nothing leaves
the system permanently halted waiting for an operator who is not
returning, *except* the narrow Hard-broke cases where halting is
actively safer than continuing — software bugs that would re-fire
on retry, or broker-state violations where the broker layer cannot
trust its own data. See DECISIONS.md D-SPEC-8 for the framework
rationale.

Notation in the failure catalog: `Critical (hard broke)`,
`Critical (self-healed weird)`, or `Critical (normal-ops notice)`.
All three are Critical severity for alerting purposes; they differ
only in operator-relevance.

The severity name "Notice" is preferred over "Alert" to avoid
confusion with the Plan entry type `Alert` (which is the dispatch
mechanism; severity is metadata about that dispatch).

### 11.2 Failure catalog

#### 11.2.1 Broker connectivity loss

- **Symptom:** Broker layer raises `BrokerNotReady` or
  `BrokerUnreachable` (§15.4) — the TWS/Gateway is unreachable,
  the API handshake timed out, or a connection that was previously
  established has dropped. May happen at cycle start (most common)
  or mid-cycle (less common; the per-cycle connection model in
  §15.3 means each cycle reconnects fresh).
- **Severity:** Critical (self-healed weird).
- **System response:** Cycle ends; state file unchanged; alert
  dispatched. No `operational_pause` is set — broker connectivity
  is transient by nature and the next scheduled cycle will retry.
- **Recovery:** Next scheduled cycle attempts to reconnect. If the
  failing cycle was a monthly-withdrawal cycle, the SELL+ACH for
  that month may be delayed. The operator should check
  TWS/Gateway status (is the process running? did IBKR push a
  forced upgrade? does the box have network connectivity?) if
  the alert persists across multiple cycles. The §14.3 runbook
  has a diagnostic procedure.

#### 11.2.2 Order rejection (broker)

- **Symptom:** Order entry submitted; broker returns rejection
  (reason varies: insufficient buying power, instrument restricted,
  account flagged, etc.).
- **Severity:** Critical (self-healed weird).
- **System response:** Cycle aborts after rejection.
  `operational_pause` is set with `pause_reason: "order_rejection"`.
  Per §11.3.1, the pause auto-resumes after
  `pause_auto_resume_hours` (default 48); the next cycle re-reads
  broker state and produces a fresh plan.
- **Recovery:** Transient causes (insufficient buying power from a
  prior cycle's unexpected fill behavior, temporary broker flag)
  resolve naturally within 48h; the auto-resume cycle decides
  cleanly against fresh state. Persistent causes (instrument
  permanently restricted, account flag requiring operator action)
  re-pause; `consecutive_pause_count` increments. At N=4
  consecutive re-pauses, alert severity escalates to Warning via
  `pause_consecutive_escalation` so the returning operator sees an
  unmissable pattern. No manual state-file edit is required at any
  point; the operator's role is to resolve the broker-side issue
  (if any), after which the next auto-resume cycle succeeds and
  the pause clears naturally.

#### 11.2.3 State file missing or corrupt

- **Symptom:** Process startup fails to read or parse state file.
- **Severity:** Critical (hard broke).
- **System response:** Process refuses to start. Logs error to
  startup log. Alerts via email/SMS if alerter can be initialized
  without state file (which it should — credentials live in `.env`).
- **Recovery:** Operator restores state file from peer box's copy,
  from backup, or from cycle log replay. Detailed runbook in
  operational documentation.

#### 11.2.4 Rsync replication failure

- **Symptom:** Master's daily rsync push to slave fails (network
  unreachable, slave host down, ssh credentials expired, disk full
  on slave).
- **Severity:** Warning (auto-recovers when peer reachable). Escalates
  to Critical if persistent failures cross the slave promotion
  threshold (72 hours), since the slave will then begin its 48-hour
  promotion grace.
- **System response:** Master cycle does NOT fail (master writes its
  own local state successfully). Failed rsync attempts are logged on
  master side. Slave's local state file is not refreshed; slave's
  daily check observes stale `last_master_write_timestamp` and
  escalates per §9.4.2.
- **Recovery:** Operator restores network connectivity or fixes
  rsync configuration. Next successful rsync brings slave's local
  state file current; slave returns to SLAVE_SLEEPING and cancels
  any pending promotion.

#### 11.2.5 Lookback signal UNAVAILABLE

- **Symptom:** Synthetic Growth Lookback returns UNAVAILABLE due to
  stale or insufficient price data.
- **Severity:** Notice (auto-recovers when data refreshed).
- **System response:** CB state holds. Rebalancing and refill
  decisions continue per the held state. Withdrawal decisions
  continue (lookback is not a withdrawal gate). Phase 2 swing
  decisions are deferred (signal is required for both deploy and
  recover triggers).
- **Recovery:** Operator refreshes price data files. Next cycle's
  signal computation succeeds.

#### 11.2.6 Alerter dispatch failure

- **Symptom:** Email or SMS dispatch fails.
- **Severity:** Variable per channel and severity of the underlying
  alert (§9.3.3).
- **System response:** Cycle does NOT fail. Failed alerts are
  recorded for retry on next cycle.
- **Recovery:** Auto-recovers when network/credentials work. If
  persistent, a Warning alert fires when the alerter does eventually
  dispatch.

#### 11.2.7 Cascade exhaustion

- **Symptom:** Withdrawal sourcing reaches all three cascade stages
  at residual floor with demand still unmet.
- **Severity:** Critical (hard broke, withdrawal-only).
- **System response:** Withdrawal cycle aborts before SELL execution.
  No partial withdrawal. `withdrawal_capacity_exhausted: true` set
  in the state file. The system enters a withdrawal-only halt:
  future withdrawal attempts are suppressed, but all other system
  functions continue normally. The weekly summary re-alerts the
  `withdrawal_capacity_exhausted` condition every cycle.
- **Recovery:** The flag auto-clears when the cascade can again
  meet a hypothetical withdrawal demand at the next withdrawal
  cycle (i.e., when SGOV+FI+Growth above-residual ≥ current
  monthly withdrawal). This typically requires cash entering the
  managed account (Roth conversion, dividend accumulation, external
  deposit, etc.) — the §2.9 "cash appears, system reacts" mechanism
  picks it up automatically. There is no 48-hour timer-based
  auto-resume (auto-retrying the same impossible operation
  indefinitely would be pointless). See §11.3.2 for the full
  indefinite-halt semantics.

#### 11.2.8 Partial PhaseTransition execution

- **Symptom:** A PhaseTransition entry's SELL or BUY orders fail
  mid-execution, leaving the system with an asset allocation matching
  neither the outgoing nor the incoming phase.
- **Severity:** Critical (self-healed weird).
- **System response:** Cycle ends in inconsistent state. Phase
  indicator may have been updated (depending on where in the
  PhaseTransition execution the failure occurred — see §8.2.5).
  `operational_pause` is set with `pause_reason: "partial_phase_transition"`.
  Per §11.3.1, the pause auto-resumes after
  `pause_auto_resume_hours` (default 48). The 48-hour window
  allows any in-flight broker orders to settle to terminal states
  (filled or rejected) before the next attempt.
- **Recovery:** On auto-resume, the next cycle re-reads broker
  state (the source of truth per §8.3), computes the gap between
  current allocation and target-phase allocation, and produces a
  new plan that completes the transition from wherever it left
  off. The same plan-generation logic that produced the original
  PhaseTransition entry handles the partial state cleanly because
  it always plans from current state toward target. If the
  follow-up plan also fails, `consecutive_pause_count` increments
  and the system re-pauses; at N=4 the alert severity escalates
  via `pause_consecutive_escalation`. No manual state-file edit
  required.

#### 11.2.9 Token unreadable (single box)

- **Symptom:** Daily-token cycle fails to enumerate USB devices.
- **Severity:** Notice (escalates to Warning at >2 cycles, Critical
  at >7 cycles per §10.4).
- **System response:** Token state holds at previous value.
- **Recovery:** Operator investigates USB or driver issue on the
  affected box; reboot or hardware replacement as needed.

#### 11.2.10 Token mismatch (between boxes)

- **Symptom:** Two boxes report different token counts after daily
  reads.
- **Severity:** Warning (escalates to Critical at >2 daily cycles).
- **System response:** State holds at previous value (per §10.5).
- **Recovery:** Operator investigates. Most-likely cause is
  asynchronous detection during a real state change (resolves
  naturally within 2 cycles). Persistent mismatch indicates a
  hardware or configuration issue.

#### 11.2.11 Master/slave split-brain

Split-brain has two detection layers; they are independent and
serve different purposes.

**Layer A — State-file split-brain (§9.4.3):**

- **Symptom:** Both boxes detect themselves as master with conflicting
  recent writes to the operating state file.
- **Severity:** Notice (auto-recovered).
- **System response:** IP last-octet tiebreak applied (§9.4.3) — this
  runs automatically on detection, no operator action required.
  Loser transitions to SLAVE_SLEEPING. Loser's actions during the
  partition window are surfaced in the alert content for operator
  awareness.
- **Recovery:** No operator action required to resume operation.
  The tiebreak resolves the state-file conflict automatically.

**Layer B — Broker-level split-brain (§15.6):**

- **Symptom:** The `last_cycle_clientid` field in the operating state
  file shows two distinct values within a 24h window — meaning two
  different boxes' cycles both ran broker work within that span.
- **Severity:** Critical (self-healed weird).
- **System response:** Critical `split_brain_detected` alert
  dispatched. No `operational_pause` is set. By the time Layer B
  fires, the broker layer's pre-place lookup (§15.5, defense layer
  3) has already prevented duplicate execution, and Layer A's
  IP-tiebreak has resolved the state-file conflict; the system is
  already in a consistent state. The alert is the receipt that
  proves the system noticed.
- **Recovery:** No system action required — the system continues
  operating from the post-tiebreak state. The operator's role is
  forensic: inspect the cycle log and the IBKR account log to
  confirm no duplicate orders were placed (defense layer 3 should
  have prevented this; the inspection verifies), and resolve the
  underlying cause (network partition, rsync failure, host
  misconfiguration) so future occurrences are less likely.

The two layers complement each other: Layer A prevents state-file
divergence in the steady state; Layer B catches the case where
Layer A didn't fire fast enough to prevent both boxes from
contacting the broker. Layer B is post-hoc detection — by the
time it fires, the broker layer's defense layer 3 (§15.6) has
already prevented duplicate execution. The alert is the receipt
that proves the system noticed.

#### 11.2.12 Configuration validation failure

- **Symptom:** Process startup detects invalid `ruleset.yaml`
  (missing required field, type mismatch, internal inconsistency).
- **Severity:** Critical (hard broke).
- **System response:** Process refuses to start (per I9). Logs and
  alerts. This is a pre-condition failure, not a runtime failure;
  the operational_pause framework doesn't apply (the process
  cannot run at all).
- **Recovery:** Operator fixes the configuration file and restarts
  the process. The system never silently falls back to defaults.
  Slave promotion (§9.4.2) handles continuity if the failure is
  on master only.

#### 11.2.13 Disk full

- **Symptom:** State file write or log append fails with ENOSPC.
- **Severity:** Critical (self-healed weird).
- **System response:** Cycle aborts. `operational_pause` is set
  with `pause_reason: "disk_full"`. Per §11.3.1, auto-resume after
  48 hours retries; log rotation may have freed sufficient space
  by then.
- **Recovery:** Most cases resolve via the daily log rotation
  freeing space within the 48-hour auto-resume window. Persistent
  failures (e.g., very large state files, runaway cycle logs)
  re-pause; `consecutive_pause_count` increments; at N=4 the
  alert severity escalates via `pause_consecutive_escalation`
  prompting operator attention.

#### 11.2.14 Broker inconsistency

`BrokerInconsistency` is raised by the broker layer (§15.4) when
IBKR returns data that violates an invariant IRAPM depends on.
The classification depends on whether the broker layer can trust
its own state going forward — the sub-cases split into Hard-broke
and Self-healed-weird categories.

**Hard-broke sub-cases** — broker layer cannot trust IBKR's data:

- **Connected account ID doesn't match `expected_account_id`
  (§15.8) at connect time.** Defense against wrong-account
  connection (e.g., paper-vs-live TWS port confusion). The cycle
  cannot proceed; retrying would keep connecting to the wrong
  account.
- **A returned `Trade` carries data the broker layer can't
  reconcile** (impossible `execId` collision, wrong `client_id`
  attribution, malformed numeric values). Indicates ib_async /
  IBKR API version drift or a TWS bug; retrying executes the
  same broken code path against the same broker behavior.

System response: Cycle aborts. `operational_pause` is set with
`pause_reason: "broker_inconsistency"`. NOT eligible for
auto-resume. Severity: **Critical (hard broke)**.

Recovery: Operator reviews the alert content and the cycle log,
resolves the root cause (correct the TWS port configuration, or
verify and update the pinned ib_async version against the current
IBKR API), and explicitly clears the pause.

**Self-healed-weird sub-cases** — broker layer's state is
temporarily ambiguous but the next cycle's fresh broker query
resolves it:

- **Post-placement confirmation timed out (§15.5):** an order
  was submitted but did not appear in IBKR's open-orders table
  within `POST_PLACEMENT_CONFIRMATION_WINDOW_SEC` (default 15s).
  The order's true state is unknown at this instant — but the
  next cycle's pre-place lookup queries IBKR for recent
  activity (§15.6 defense layer 3) and either finds the order
  (use the existing fill) or doesn't (place anew, idempotently).
- **Pre-place query failed (§15.5):** `get_recent_activity()`
  raised, leaving the idempotency lookup blind. The cycle
  refused to place rather than risk duplicate execution. The
  next cycle retries the query; persistent failure escalates to
  `broker_connectivity_loss` (§11.2.1) which is itself
  self-healed-weird.

System response: Cycle aborts; alert dispatched. No
`operational_pause` is set — the next cycle's fresh broker
query resolves the ambiguity automatically via the established
idempotency mechanism. Severity: **Critical (self-healed weird)**.

Recovery: No system action required. The alert exists for
operator forensics — repeated occurrences may indicate a TWS or
network issue worth investigating, but the system is operating
correctly.

#### 11.2.15 External account activity overlap

- **Symptom:** `get_recent_activity()` returns orders with empty
  `client_order_id` (no `orderRef` stamped by IRAPM) on a symbol
  IRAPM is about to act on, OR — more rarely — on a symbol where
  IRAPM has an open order. This is detected at the action layer's
  pre-place check (§15.6 case 4).
- **Severity:** Critical (self-healed weird).
- **System response:** Cycle aborts before placing any orders. No
  `operational_pause` is set. The declination to act *is* the
  heal: the system has correctly refused to place orders against
  potentially-compromised state, and the next cycle re-reads
  broker state fresh and decides against post-manual-activity
  reality. Critical alert dispatched for operator awareness.
- **Recovery:** No system action required. The next cycle
  proceeds against the new positions as baseline. The alert
  exists for operator forensics — the operator reviews the
  activity in the IBKR portal to identify what was placed
  manually and why. If the activity was unintentional (account
  compromise, accidental order), operator takes separate
  IBKR-side action; IRAPM's continued operation against the new
  baseline is not the safety concern (the broker layer's
  idempotency mechanism handles all subsequent activity
  correctly).

  The system-continues-operating-against-fresh-state behavior is
  consistent with §1.4's failure-quiet principle: an
  absent-operator cannot acknowledge anything, and indefinite
  pause on every external-activity event would freeze the system
  on any unrelated account activity (legitimate dividend
  routing, residual cash sweeps, broker-side rebalances).

### 11.3 Halt mechanisms

IRAPM has two parallel halt mechanisms with different semantics.
Both can be active simultaneously (rare, but possible if a Critical
failure pauses the system before cascade exhaustion clears).

**Operational pause** (`operational_pause`) — generic halt applied
to Critical failures. The pause-reason determines whether auto-
resume applies: Self-healed-weird reasons auto-resume after a
timer; Hard-broke reasons do not auto-resume (only external action
can clear them, per the §11.1 framework and D-SPEC-8). The full
mechanism is specified in §11.3.1 below.

**Indefinite halt** (`withdrawal_capacity_exhausted`) — non-auto-
resuming, withdrawal-only halt applied when the portfolio cannot
fund a scheduled withdrawal at residual floors. The full mechanism
is specified in §11.3.2 below.

The two mechanisms use distinct state fields (`operational_pause`
struct vs `withdrawal_capacity_exhausted` boolean) and follow
distinct lifecycles. Neither requires operator state-file edits;
both clear automatically when their respective recovery conditions
are met (auto-resume timer for the Self-healed-weird pauses;
external action for the Hard-broke pauses and for
`withdrawal_capacity_exhausted`).

#### 11.3.1 Operational pause

`operational_pause` is a structure in the persistent state file:

```
operational_pause = {
    paused: bool,
    pause_reason: str,            # see catalog below
    pause_started_at: timestamp,
    consecutive_pause_count: int, # incremented on same-reason re-pause
}
```

When `paused == true`, scheduled cycles still run but **abort
immediately after step 1** (input refresh + state read), without
proceeding to decision or action layers. The cycle dispatches a
`pause_initiated` (first cycle) or `pause_re_alert` (subsequent
cycles) alert.

**Auto-resume.** For Self-healed-weird pause reasons
(`partial_phase_transition`, `order_rejection`, `disk_full`):
when `pause_started_at + pause_auto_resume_hours` (default 48)
has elapsed, the next cycle observes the threshold, clears the
pause structure (`paused: false`, `pause_reason: null`), emits a
`pause_auto_resumed` alert, and proceeds with normal cycle
execution. The cycle reads fresh broker state and produces a new
plan that reflects whatever happened during the pause window.

**No auto-resume for Hard-broke reasons.** Pauses with
`pause_reason` in `{internal_consistency_violation,
broker_inconsistency}` do not auto-resume. The system continues
running scheduled cycles (which abort after step 1, dispatching
`pause_re_alert` each time) until external action resolves the
underlying issue. There is no operator state-file edit path —
recovery comes from resolving the root cause (deploying a code
fix for `internal_consistency_violation`, or fixing the broker
configuration / library version for `broker_inconsistency`),
after which the next cycle finds the condition cleared and
resumes normal operation. See D-SPEC-8 for why these specific
pause types reject the failure-quiet preference of automatic
recovery.

If the next cycle's plan re-triggers the **same pause condition**,
`consecutive_pause_count` is incremented. When `consecutive_pause_count >= 4`,
the alert severity escalates to Warning with a
`pause_consecutive_escalation` alert (the system keeps trying, but
the alert pattern gets louder so the returning operator sees an
unmissable signal).

**Pause-reason catalog:**

| `pause_reason` | Source failure | Category | Auto-resume? | Notes |
|---|---|---|---|---|
| `partial_phase_transition` | §11.2.8 | Self-healed weird | Yes (48h) | Next cycle re-reads broker state and completes the transition |
| `order_rejection` | §11.2.2 | Self-healed weird | Yes (48h) | Next cycle re-decides against fresh state; consecutive-escalation at N=4 |
| `disk_full` | §11.2.13 | Self-healed weird | Yes (48h) | Disk may have freed via log rotation by next attempt |
| `internal_consistency_violation` | §7.3.2 step 4 | Hard broke | No | Assertion-failure semantics; same software bug will re-fire on retry — see D-SPEC-8 |
| `broker_inconsistency` | §11.2.14 (hard-broke sub-cases) | Hard broke | No | Account-ID mismatch or malformed Trade data; broker-state cannot be trusted — see D-SPEC-8 |

Each value's category column reflects the operator-relevance framework
of §11.1: auto-resuming pauses are Self-healed-weird (the system
resumes itself after a brief window of letting transient conditions
clear); non-auto-resuming pauses are Hard-broke (no automatic
recovery is safe or meaningful).

Conditions explicitly NOT setting `operational_pause` (alert only,
no halt):
- Broker disconnect (§11.2.1) — Self-healed weird; transient,
  auto-recovers on next cycle's reconnect.
- Layer B split-brain detected (§11.2.11) — Self-healed weird; by
  the time detection fires, IP-tiebreak and broker idempotency
  have already restored consistency.
- External account activity overlap (§11.2.15) — Self-healed
  weird; the system has correctly refused to act on suspicious
  data and the next cycle proceeds against fresh state.
- Broker inconsistency, transient sub-cases (§11.2.14) — Self-
  healed weird; post-placement confirmation timeout and pre-place
  query failure both resolve on the next cycle's fresh broker
  query.
- ACH update failure (§8.2.7) — system continues at prior ACH
  amount; IBKR-side operator action required when the operator
  returns, but the system itself is not halted.
- Signal UNAVAILABLE (§11.2.5) — CB state holds; system continues
  per §5's degraded-gracefully design.

Refuse-to-start conditions (`state_file_corrupt` per §11.2.3,
`configuration_validation` per §11.2.12) are not pause_reason
values — they prevent process startup, so no state file is ever
written with these values. Both are Hard-broke per §11.1, but
they manifest as process refusal rather than as runtime pauses.

Cascade exhaustion (§11.2.7) is also not in this catalog; it uses
the separate `withdrawal_capacity_exhausted` flag per §11.3.2.

#### 11.3.2 Indefinite halt (`withdrawal_capacity_exhausted`)

The `withdrawal_capacity_exhausted` boolean is a sibling state flag
to `operational_pause`. It is set when withdrawal sourcing reaches
all three cascade stages (SGOV, FI, Growth) at residual floor with
demand unmet (§7.3.2 step 4, §11.2.7).

**Scope.** The halt is **withdrawal-only**. While the flag is true:
- Scheduled withdrawal cycles abort before SELL execution.
- All other system functions continue normally: CB state machine,
  signal computation, weekly summary alerts, rebalancing if any
  value remains, daily-token monitoring, master/slave coordination.
- The weekly summary re-fires the `withdrawal_capacity_exhausted`
  condition every cycle.

**Auto-clear.** The flag has exactly one auto-clear path: at each
subsequent withdrawal cycle, the system evaluates whether
SGOV+FI+Growth above-residual ≥ current monthly withdrawal demand.
If yes, the flag clears that cycle and normal withdrawal sourcing
resumes. The trigger condition for setting the flag is its own
inverse — the same cascade evaluation that set it clears it.

There is **no 48-hour timer-based auto-resume** (unlike
`operational_pause`). There is **no operator state-file edit
path**; recovery happens through cash appearing in the managed
account (per the §2.9 "cash appears, system reacts" mechanism),
which the cascade re-evaluation picks up automatically on the next
withdrawal cycle.

The asymmetry vs `operational_pause` reflects the underlying
condition. `operational_pause` recovers because the failure was
transient — a 48-hour retry against fresh state is meaningful.
`withdrawal_capacity_exhausted` recovers because cash genuinely
returned to the portfolio — a timer-based retry would either
fire pointlessly (cash hasn't returned) or fire after it didn't
need to (cash already returned, and the next withdrawal cycle
catches it anyway).

### 11.4 Recovery alert cadence

When the system is in `operational_pause.paused == true` state:

- Each scheduled cycle generates a re-alert (Notice severity).
- The re-alert references the original failure event and timestamp.
- The cadence is the cycle cadence — typically one re-alert per
  weekly cycle, plus daily-token cycle re-alerts if token-related
  failure.
- Re-alerts are NOT deduplicated (the §9.3.2 dedup window is bypassed
  for paused-state re-alerts), since the operator needs persistent
  visibility that the system is not running.

---

## §12. Observability

This section specifies what the system makes visible to the operator
during normal operation, beyond the per-event alerts of §11. The
observability principle: **at any moment the operator should be able
to answer "what is the system doing right now and why?" by reading
recent alerts and logs alone.**

### 12.1 Per-cycle output

Every cycle (regardless of type) produces:

1. A cycle log entry (§9.4 / §12.2.1) recording the full cycle.
2. State file updates (per §6.7).
3. Zero or more alert dispatches.

### 12.2 Logs

#### 12.2.1 Cycle log

The cycle log is the primary forensic record. Each entry contains:

- Cycle ID (UUID), cycle type, timestamp start/end, box ID
- Input snapshot: positions, prices, broker connectivity status,
  token states
- State machine snapshot: all 6 elements of the operating-mode tuple
- Synthetic Growth Lookback signal value (or UNAVAILABLE)
- Annual review evaluations (annual-review cycle only): freeze
  decision, prior-year CB-day counts, recomputed buffer/refill/cash
  values
- Plan generated (full structured Plan)
- Action layer execution log: per-entry attempt result, broker
  response, fills, ACH references
- Alerts dispatched (severity, channel, dedup outcome)
- Final state-file write status

Cycle log entries are JSON for machine readability. Daily rotation,
90-day retention (per §9.4.5).

#### 12.2.2 CB transition log

Append-only, indefinite retention. Used by the annual review for
freeze evaluation. Format per §6.7.

#### 12.2.3 Annual review log

Append-only, indefinite retention. Per-year record of the freeze
decision, prior-year CB-day counts, and recomputed buffer/refill/cash
values. Format per §6.7.

#### 12.2.4 Alert log

Daily rotation, 90-day retention. Records every dispatch attempt
(success and failure) including dedup suppressions.

#### 12.2.5 Coordination log

Daily rotation, 90-day retention. Records role transitions,
heartbeat writes, slave-wake decisions, and split-brain resolutions.

#### 12.2.6 Token check log

Daily rotation, 90-day retention. Records each daily-token cycle's
observation per box.

### 12.3 Mandatory weekly summary alert

Every weekly cycle MUST generate a summary alert (Info severity,
Email + SMS) containing the following structured content:

1. **Portfolio state**
   - Core portfolio value (Growth + FI total, dollar)
   - Per-position values (FBCG, AVUV, PYLD/JPIE or GBIL by phase)
   - Allocation drift summary (largest absolute deviation)
2. **Circuit breaker state**
   - Current CB state (CB_INACTIVE/CB1/CB2)
   - CB1 → CB2 timer transition timer if in CB1 (days accumulated)
   - Any pending CB transitions and their confirmation progress
3. **Cascade status**
   - Current cascade state, one of:
     - `Normal` (no active CB2 episode)
     - `CB2 active, no draw yet` (CB2 entered but no withdrawal has
       drawn from cascade sources yet in this episode)
     - `SGOV engaged` (cascade currently uses SGOV buffer only)
     - `SGOV+FI engaged — ATTENTION` (cascade has extended into FI
       during this episode)
     - `SGOV+FI+GROWTH engaged — CRITICAL` (cascade has reached Growth
       during this episode)
   - If episode is active, the episode start date (CB2 entry date)
   - If episode is active, the depth at which the cascade has been
     operating (the running latch from `cascade_episode_state`, not
     just the current cycle's draw — an episode that drew FI once
     stays in `SGOV+FI` even if the current cycle only needed SGOV)
4. **Buffer state**
   - SGOV buffer market value
   - Buffer target value
   - Buffer condition tag: one of {idle, drawdown, exhausted,
     refilling}
   - Refill status if relevant (next batch amount, target completion
     date)
5. **Upcoming withdrawal**
   - Next scheduled withdrawal amount
   - Days until next monthly withdrawal step within the weekly cycle (calendar date)
   - Income state (ACTIVE / PAUSED) at time of summary
6. **Extraordinary events**
   - Any state transitions in the past week
   - Any failures that triggered Critical-transient alerts in the
     past week
   - Any tokens in UNAVAILABLE state on any box
   - Any persistent mismatches

The buffer condition tags are defined as:

- **idle:** buffer at or above target (within $250 tolerance); no
  refill activity needed.
- **draining:** buffer below target, active cascade in progress
  (CB2 active).
  Refill is suspended because cascade is active.
- **delayed:** buffer below target, no active cascade, within
  `sgov_refill_post_recovery_delay_days` post-recovery delay window.
  Refill is suspended because the recovery timer has not elapsed.
- **refilling:** buffer below target, no active cascade, recovery
  timer elapsed, refill batches actively running.
- **exhausted:** buffer at or near residual floor; cascade has
  drawn it down significantly. Separate from "draining" — this is
  the alarming subset where the buffer is no longer absorbing
  meaningful withdrawal demand.

The summary alert is **mandatory** — it runs even on weeks with no
state transitions or actions. It is the operator's regular touchpoint
with the system. A missed weekly summary alert is itself a signal
that something has gone wrong (no cycles ran, alerter is broken,
master is dead, etc.).

### 12.4 Alert content structure

Per §9.3.1. Every alert includes the cycle ID and timestamp so the
operator can locate the corresponding cycle log entry.

### 12.5 Operator-initiated inspection

The operator should be able to inspect the system without modifying
it via:

- Reading the state file directly (it is JSON; readable text format)
- Reading recent cycle logs
- Reading the CB transition log
- Reading the annual review log

No special command or tool is required for read-only inspection. The
state file format is documented in operational documentation.

### 12.6 Alert catalog

This is the canonical catalog of alert types the system emits.
Every alert dispatched via the §9.3 alerter has a row in this
catalog. Plan-flag references throughout the spec
(`large_rebalance`, `phase3_activation`, etc.) map to `alert_id`
values here.

Operator-facing message templates (Subject, Body, SMS) are
parameterized with `{placeholders}` for runtime values and should
live in a sibling YAML file `alert_templates.yaml`, edited
independently of `ruleset.yaml`. The two operator-edited files
have distinct scopes: `ruleset.yaml` is for financial / operational
parameters that affect system behavior; `alert_templates.yaml` is
for human-facing message wording that does not affect behavior.
Code references `alert_id` only; the human-facing text is loaded
from `alert_templates.yaml` at runtime.

**Catalog structure.** Each entry has:

- `alert_id` (stable identifier used by code)
- Trigger (the condition / plan flag / state transition that fires it)
- Default severity (Info / Notice / Warning / Critical)
- Channels (always Both — see channel policy below)
- Editable template fields (subject, body, SMS) in `alert_templates.yaml`
- Dedup-key pattern (per §9.3.2)

**Channel policy: every alert dispatches via BOTH email AND SMS.** The
operator may be in locations with cell coverage but no WiFi or data
plan (rural travel, cabins, cruise ships in port). SMS uses the
cellular voice/text network, which has fundamentally wider coverage
than internet-based email delivery. For a system whose purpose is to
keep operating unattended during long absences, "operator reachable
by cell but not internet" is a real and common scenario. The "Channels"
column in the tables below shows "Both" uniformly: every alert
reaches the operator under the cellular-only-coverage condition.

**Phase and transition alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `large_rebalance` | Any phase transition plan generated | Notice | Both |
| `phase3_activation` | Phase 3 latch event (24h grace expired) | Critical | Both |
| `phase3_grace_started` | All four Phase 3 tokens read as removed, grace window begins | Critical | Both |
| `phase3_grace_pending_abort` | Token re-insertion observed during grace; awaiting persistence confirmation | Notice | Both |
| `phase3_grace_aborted` | Token re-insertion persisted across one full cycle; abort committed | Info | Both |

**Circuit breaker alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `cb_transition` | Any CB state change (entry or exit of CB1, CB2) | Notice | Both |

The `cb_transition` alert body carries a `trigger_reason` field:
for CB2 entry, one of `{signal, portfolio_low, fi_low, cb1_timer}`;
for CB2 exit, a list of the conditions that cleared. CB1 entries
and exits are signal-based only.

**Phase 2 rebalancing alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `phase2_opportunistic_deploy` | Phase 2 swing trigger → deployed state | Notice | Both |
| `phase2_opportunistic_recover` | Phase 2 swing trigger → steady state | Notice | Both |
| `phase2_semi_annual_reallocation` | Phase 2 semi-annual realignment fires | Info | Both |

**Withdrawal alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `withdrawal_executed` | Monthly withdrawal completed successfully | Info | Both |
| `withdrawal_failed` | SELL or ACH failure during withdrawal cycle | Critical | Both |
| `cascade_engaged_sgov` | Cascade sourcing engaged SGOV stage on first cycle of CB2 episode drawing SGOV (§7.3.2 step 1) | Notice | Both |
| `cascade_extended_fi` | Cascade extended past SGOV into FI stage for the first time in episode (§7.3.2 step 2) | Warning | Both |
| `cascade_growth_source` | Withdrawal cascade reached Growth stage (§7.3.2 step 3) | Critical | Both |
| `withdrawal_capacity_exhausted` | Cascade exhaustion indefinite halt set (§11.2.7) | Critical | Both |
| `monthly_payment_ceiling_bound` | Phase 3 monthly payment clamped at portfolio-% or indexed dollar ceiling (§4.1.1.2, §7.3.1) | Notice | Both |

**Cascade alert dedup semantics.** The four cascade-tier alerts
(`cascade_engaged_sgov`, `cascade_extended_fi`, `cascade_growth_source`,
`withdrawal_capacity_exhausted`) follow a **fire-once-per-tier-
escalation-within-episode** policy. A "cascade episode" is the span
from CB2 entry to the next `cb_transition` to CB_INACTIVE (REC).
Within one episode:

- Each tier alert fires **once on first entry to that tier** in the
  episode.
- Returning to a shallower tier within the episode (a withdrawal that
  only needed SGOV after a previous one reached FI) does **not** clear
  the higher-tier latch — the episode is still active.
- A subsequent withdrawal that re-extends to a previously-reached
  tier does **not** re-fire that tier's alert.
- A withdrawal that newly extends to a tier deeper than any previous
  in the episode fires that tier's alert.
- On REC, all four tier latches reset. The next CB2 episode starts
  fresh.

The `weekly_summary` alert (§12.3) re-surfaces current cascade status
every cycle, so the operator is never blind to an ongoing cascade
between tier-escalation alerts.

**Cash deployment:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `large_cash_deployment` | §7.7.1 large-deployment plan executed | Notice | Both |

**Annual review:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `annual_review_completed` | Jan 15 annual review cycle completes | Notice | Both |
| `freeze_decision` | Annual review froze (or did not freeze) CPI raise | Notice | Both |

**Token alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `token_state_change` | Valid Phase 3 or STOP INCOME state transition | Notice | Both |
| `token_invalid_state` | Mismatch or partial-count state observed | Warning | Both |
| `token_unavailable` | Daily-token cycle fails to enumerate USB devices | Notice (escalating per §10.4) | Both |
| `stopincome_stuck_alert` | STOP INCOME paused >12 months threshold reached; quarterly re-alert | Notice | Both |

**Operational pause alerts (per §11.3.1):**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `pause_initiated` | `operational_pause` set on first cycle | Notice | Both |
| `pause_re_alert` | Subsequent cycle while paused | Notice | Both |
| `pause_auto_resumed` | Auto-resume cycle clears pause (Self-healed-weird only) | Notice | Both |
| `pause_consecutive_escalation` | `consecutive_pause_count` ≥ 4 same-reason | Warning | Both |
| `internal_consistency_violation` | §7.3.2 step 4 hard-broke pause set | Critical | Both |
| `broker_inconsistency` | §11.2.14 hard-broke sub-case pause set | Critical | Both |

**Non-paused Critical-event alerts (per D-SPEC-8; the system has
already healed, no `operational_pause` set):**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `split_brain_detected` | §11.2.11 Layer B post-hoc detection | Critical | Both |
| `external_activity_overlap` | §11.2.15 unrecognized broker activity detected | Critical | Both |
| `broker_inconsistency_transient` | §11.2.14 transient sub-case (post-placement timeout / pre-place query failure) | Critical | Both |

**ACH and broker alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `ach_update_failed` | Single ACHScheduleUpdate failure | Notice | Both |
| `ach_update_warning` | `ach_update_warning_threshold_cycles` consecutive failures | Warning | Both |
| `broker_disconnect` | Cycle aborts at input refresh (Critical transient) | Critical | Both |

**Master/slave alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `slave_promotion_pending` | Slave observes master staleness ≥ `slave_wake_staleness_hours`; 48h grace begins | Critical | Both |
| `slave_promoted` | Auto-promotion completed | Critical | Both |
| `slave_promotion_cancelled` | Master heartbeat returned during grace window | Info | Both |
| `split_brain_resolved` | Tiebreak completed automatically (§11.2.11) | Notice | Both |

**Data / signal alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `data_file_stale` | Lookback price files stale, signal UNAVAILABLE | Notice | Both |
| `signal_recovered` | Signal returns to available after UNAVAILABLE period | Info | Both |

**Configuration / sanity alerts:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `alerter_failure_recovered` | Post-hoc: alerter dispatch failed previously; channels recovered | Warning | Both |

**Mandatory weekly:**

| `alert_id` | Trigger | Severity | Channels |
|---|---|---|---|
| `weekly_summary` | Every weekly cycle (§12.3) | Info | Both |

The `large_rebalance` alert includes a **residual-exceptions
section** in its body listing any positions skipped during the
transition because they were already at or below residual
(§7.2.1, §8.2.5).

The catalog is not closed: additional alert types may be added as
the system evolves. Each addition follows the same pattern — pick
a stable `alert_id`, add a row to this catalog, edit
`alert_templates.yaml` to define the message wording.

---

## §13. Testing Strategy

This section specifies how IRAPM correctness is established before
production deployment and maintained over the system's lifetime. The
guiding principle: **the simulator (IPMS) is the primary correctness
authority for strategy logic**; unit and integration tests cover the
mechanical layers around it.

### 13.1 Test layers

| Layer | Purpose | Tool |
|---|---|---|
| Unit tests | Pure-function correctness (decision logic, signal computation, state transitions in isolation) | pytest |
| Integration tests | Subsystem interactions (decision → action sequence, state machine transitions across cycles) | pytest with fixture-based fake broker |
| Simulator tests (IPMS) | Strategy correctness over historical and synthetic market data | IPMS framework |
| Recovery tests | Failure recovery scenarios (state file restoration, slave wake, paused-recovery flow) | pytest with disk-state fixtures |
| Dry-run tests | End-to-end with real broker connection, no order execution | IRAPM dry-run mode (§13.7) |

### 13.2 Unit test coverage requirements

- Synthetic Growth Lookback signal: per-step correctness, edge cases
  (single bar, all stale, partial alignment, non-positive prices)
- Decision layer functions: tested for each branch in §7.3 (CB_INACTIVE, CB1,
  cascade), §7.4 (refill in each CB state, residual floor clamping),
  §7.5 (rebalance triggers, Phase 2 swing triggers), §7.6 (annual
  review)
- State machine transitions: each transition table row in §6.1, §6.2,
  §6.3, §6.4
- Schedule state computation (§3.13): inflation compounding, freeze
  list handling, range inclusivity
- Plan equality and serialization
- Money discipline: Decimal handling, rounding mode

### 13.3 Integration test scenarios

Multi-cycle scenarios where state evolves across cycles:

- Phase 1 → Phase 2 calendar transition with mid-confirmation CB
  pending
- Phase 1 → Phase 3 token-triggered transition while cascade is
  active (transition executes regardless of cascade state per §4.2;
  Phase 3 cascade machinery handles ongoing cascade after transition)
- CB1 confirmation → 90-day timer → CB2 (timer-based, withdrawal
  sourcing) → withdrawal via cascade
- **Bootstrap CB2 via Portfolio-low:** Year-1 startup with manual
  SGOV pre-fill; CB2 fires within 2 weeks of startup; SGOV drains
  through Year 1; Year-2 Roth conversion arrives, large cash
  deployment in target-weight-proportional mode clears Portfolio-low,
  CB2 exits to CB_INACTIVE.
- **CB2 via FI-low only:** signal nominal, portfolio adequate, but
  FI bucket drained by sustained CB1 episode; CB2 fires on FI-low
  path; cascade preserves FI from further depletion.
- **CB2 with overlapping conditions:** signal triggers CB2; during
  episode, Portfolio-low also triggers; signal recovers but
  Portfolio-low still active; CB2 remains active until Portfolio-low
  also clears.
- **CB2 deployment mode switching:** CB2 active via Portfolio-low;
  signal then drops below -20%; mode switches from
  target-weight-proportional to defensive on the cycle where signal
  condition begins holding; signal recovers above -20%; mode
  switches back to target-weight-proportional.
- Annual review with various prior-year CB-day patterns
- Cash buffer refill handling for various inflow patterns (deposit,
  conversion, dividend)
- Large cash deployment per §7.7.1 in each CB state and each phase
- Withdrawal sourcing across CB state transitions mid-month
- Operational pause auto-resume scenarios: partial-transition,
  order-rejection (single and N=4 consecutive); disk-full
- Cascade exhaustion → `withdrawal_capacity_exhausted` indefinite halt

### 13.4 Simulator tests (IPMS)

The IPMS simulator is the authoritative test environment for
end-to-end strategy correctness. It exercises:

- Multi-decade backtests with historical price data
- Sensitivity analysis (parameter sweeps over rebalance thresholds,
  buffer targets, etc.)
- Stress scenarios (1929, 1973-74, 2000-02, 2008-09, hypothetical
  worse)
- Phase 1 → Phase 3 trigger at various points in market history
- Phase 3 sustainability validation per §4.1.1 (annuity `I_0`
  calculation, scheduled raises, both per-month ceilings) under
  IRAPM parameter values; see §14.6 for the required re-validation
  sweep before production deployment.

The IPMS specification (separate document) defines the simulator's
own correctness criteria. IRAPM correctness depends on IPMS being
trustworthy.

**Open follow-up validation work** (not blocking IRAPM ratification):

1. Stressed historical windows (1929-style, 1966-style) to test
   design under worse sequences than 2005–2025.
2. Trigger years other than 2027 to verify pre-trigger inflation
   indexing produces consistent real outcomes at later triggers.
3. Forward Monte Carlo at the trigger portfolios to characterize
   distribution of outcomes beyond the single historical path.

### 13.5 Invariant assertions

The invariants in §3.14, §7.8, §8.4 become test assertions that run
in EVERY integration and simulator test. A test failure that violates
an invariant is a hard fail regardless of whether the test's specific
assertion passes.

The full invariant set:

- Domain: I1, I2, I3, I4, I5, I6, I7, I8, I9, I10, I11, I12, I13, I14, I15, I16
- Decision layer: D1, D2, D3, D4, D5, D6, D7, D8, D9, D10, D11, D12
- Action layer: A1, A2, A3, A4, A5, A6, A7, A8

### 13.6 Phase 3 starting-income and ceiling validation

The test suite must verify correct behavior of the Phase 3 income
calculation across the portfolio-size spectrum:

- **`I_0` calculation correctness.** For a range of portfolio
  values at trigger (e.g., $300K / $500K / $1M / $2M / $5M), verify
  the annuity formula in §4.1.1 produces the expected `I_0`.
  Independent test vectors generated by a numerical sustainability
  solver under the IRAPM parameter set serve as golden values for
  the closed-form path; equivalence within rounding tolerance
  demonstrates the closed-form implementation is correct.

- **Scheduled-monthly growth.** Verify that for any year Y > T,
  `scheduled_monthly` equals
  `I_0 × (1 + inflation_rate) ** n_raises_applied` where
  `n_raises_applied` correctly excludes any years in `frozen_years`
  per §4.1.1.1.

- **Portfolio-percentage ceiling binding.** Construct a scenario
  where `scheduled_monthly > portfolio × phase3_monthly_payment_ceiling_rate / 12`
  and verify the actual paid amount equals the portfolio-% ceiling,
  the `monthly_payment_ceiling_bound` alert fires identifying the
  portfolio-% ceiling as binding, and no other state changes.

- **Dollar ceiling binding.** Construct a scenario where
  `scheduled_monthly > phase3_dollar_ceiling_base_dollars × (1 + inflation_rate) ** (current_year - phase3_dollar_ceiling_base_year)`
  (e.g., a high-portfolio latch in 2030 producing an `I_0` above
  the year-2030 indexed dollar ceiling). Verify the actual paid
  amount equals the dollar ceiling, the alert fires identifying the
  dollar ceiling as binding, and no other state changes.

- **Both ceilings simultaneously below schedule.** Verify the
  `min(scheduled, portfolio_ceiling, dollar_ceiling)` rule selects
  the tighter ceiling and the alert identifies which one bound.

The high-portfolio latch case (≥ $2M at trigger) is the primary
motivating scenario for the dollar ceiling: at that scale the
annuity formula authorizes `I_0` well above any plausible
living-expense envelope, and the dollar ceiling is the mechanism
that prevents the corpus from being drained faster than the
survivor's actual need.

Test vector generation complements but does not substitute for
§14.6 end-to-end historical re-validation.

### 13.7 Dry-run cycle mode (mandatory)

**Definition.** Dry-run mode is a non-executing IRAPM cycle. The
system runs a complete cycle — connecting to the broker, querying
real positions, computing the signal from real price data, evaluating
state, generating a real Plan — but the action layer's
order-submitting, ACH-updating, and cash-moving operations are
intercepted and logged as "would execute X" rather than actually
invoked. Alerts ARE dispatched (so the operator sees what the system
would alert in real operation). State file is NOT written (preserving
pre-run state for the real cycle that follows).

Dry-run is **not paper trading**: there is no separate paper-trading
account at IBKR, and dry-run does not maintain a parallel simulated
portfolio. It is a single-cycle simulation against the real account,
with all external mutations suppressed. A dry-run cycle reflects what
*this* cycle would do *now* against current broker state.

Dry-run is also **not a duration-based mode** — it is a per-cycle
flag. Each invocation runs one cycle then exits. To validate behavior
over a longer period, the operator runs multiple dry-run cycles
spaced over multiple days; results are independent (no state carries
forward in dry-run mode).

Dry-run is invoked via `--dry-run` CLI flag or `dry_run: true` in
`ruleset.yaml`. The `ruleset.yaml` form is useful for
**default-to-dry-run** deployments: a fresh CL260 box can boot with
`dry_run: true` and run cycles harmlessly until the operator flips
the flag false to activate live trading.

**Mandatory use cases for dry-run mode:**

- **Pre-deployment validation:** run dry-run cycles for several weeks
  before activating live trading.
- **Post-pause verification:** when the operator returns to the
  system after one or more auto-resume cycles have occurred, run a
  dry-run cycle to verify the system is operating correctly. The
  dry-run will surface any state inconsistencies that the
  auto-resume retries glossed over. (Auto-resume retries are
  designed to be safe but cannot rule out subtle drift in long
  absences.)
- **Cascade-exhaustion review:** if `withdrawal_capacity_exhausted`
  is set, dry-run cycles can be used to confirm the portfolio's
  continued state and to test what the system would do if the
  operator manually injected funds (via the existing "cash appears,
  system reacts" mechanism in §2.9).
- **Periodic Phase 3 mechanism testing:** verify Phase 3 token
  detection, grace window, and transition Plan generation work
  end-to-end without triggering an actual transition.
- **Configuration change validation:** when changing `ruleset.yaml`,
  run dry-run cycles to verify the new configuration produces
  sensible Plans before going live.

---

## §14. Open Questions

This section lists items deliberately deferred from this specification
to subsequent work. Each is bounded — the spec is implementation-ready
without these resolved, but they should be addressed before
production deployment.

### 14.1 Simulator-tunable parameters

Several parameters are stated in this spec with provisional defaults,
to be tuned via IPMS simulator runs:

- **Phase 3 target weights.** Default 25/25/25/25 across
  FBCG/AVUV/PYLD/JPIE; simulator should validate or refine.
- **Phase 3 specific FI holdings.** Default PYLD+JPIE; simulator may
  evaluate alternative FI combinations.
- **CB1 → CB2 timer duration.** Default 90 days; simulator
  should evaluate sensitivity (30, 60, 90 day variants).
- **SGOV buffer target months.** Default 24; simulator should
  validate against severe historical scenarios.
- **Phase 2 opportunistic trigger and recovery thresholds.** Default
  -10% / +2%; simulator should evaluate alternative bands.
- **Annual review freeze threshold days.** Default 30 cumulative
  CB1+ days; simulator should evaluate.
- **Position residual minimum.** Default $1500; should be small
  enough to feel like a position but large enough to avoid
  accidental drift below; minimal simulator sensitivity expected.

### 14.2 Ruleset.yaml drafting

This specification defines the **structure** of configuration but
does not provide a complete ruleset.yaml file. The next implementation
step is to produce a draft ruleset.yaml containing all configurable
parameters with their default values, organized for operator
readability. The draft should be validated against the full
parameter list distributed across §2.3, §6, §7, §10, §13, and §14.1.

### 14.3 Operational runbook

This specification defines failure modes (§11) but does not provide
a complete operator runbook. The runbook should cover, at minimum:

- Initial deployment procedure, including:
  - **TWS / IB Gateway settings checklist** (per §15.9): API enabled,
    "Download open orders on connection" CHECKED, Trusted IP
    127.0.0.1, Memory Allocation ≥ 4096 MB, Read-only API mode OFF,
    Auto-restart enabled. Each setting verified visually before
    first cycle execution.
  - **box.yaml configuration** (per §15.8): `box_id`, broker
    `host` / `port` / `client_id` / `expected_account_id`,
    `state_file` and `cycle_attempt_file` paths. Per-box; the two
    boxes' `client_id` values must differ (11 for box-A, 12 for
    box-B) and all other fields must match.
  - **NTP daemon configuration** (per §9.4.2 clock synchronization
    assumption): verify `chrony` (specifically; not
    `systemd-timesyncd`) is installed, enabled, running, and using
    the upstream hierarchy specified in §9.4.2 (Google → Cloudflare
    → us.pool.ntp.org) on **both** boxes before first cycle.
    Verification command on each box: `chronyc tracking`. Expected
    output:
      - `Stratum`: ≤ 3
      - `Leap status`: `Normal` (not `Insert`, `Delete`, or `Not
        synchronised`)
      - `System time` offset from reference: within ±100 ms
      - `Reference ID` not blank or `00000000`
    Verify both boxes' system times agree to within ±1 second by
    comparing `date -u` output captured within the same minute.
    Confirm UDP/123 egress is open by `chronyc sources -v` showing
    at least one upstream with a `^*` (selected synchronisation
    source) marker. Annual operator dry-run repeats this
    verification and additionally scans `/var/log/syslog` (or
    `journalctl -u chrony`) for any `System clock wrong by` step
    events since last review — step events are normal and expected
    on power-on after extended offline periods, but unexpected
    step events during continuous operation indicate hardware-clock
    failure.
  - **Paper-trading verification** of the five UNCERTAINTY FLAG
    items in §15.12 before any live trading: order status mapping
    for TWS Inactive, `reqCompletedOrders` retention window,
    `reqMktData` snapshot semantics, account-summary cash tags,
    and `update_recurring_ach` behavior. Each verification
    produces a recorded test outcome appended to a deployment-
    verification log.
- Recovery procedures for each Critical-blocking failure in §11.2.
- Manual master/slave role-swap procedure (per §9.4.2, the no-wait
  alternative to letting auto-promotion run)
- Token replacement procedures
- Annual operator-initiated dry-run verification procedure
- Phase 3 latch verification (without triggering)
- Data file refresh schedule and procedure
- Backup and restore procedures (state file, logs)
- Hardware replacement procedures (failed CL260 box, failed token,
  failed pSLC SSD)
- **Manual IBKR ACH amount update procedure** (per §15.10 conservative-
  failure design): step-by-step instructions for logging into the
  IBKR Portal, navigating to the recurring-withdrawal management
  page, updating the recurring monthly amount, and verifying the
  change took effect. ACH update failures do NOT halt the system —
  IRAPM continues operating at the prior ACH amount until the
  operator resolves the IBKR-side issue. The procedure must be
  written for the survivor as audience — assume no programming or
  systems-admin skill, only basic web-browser familiarity.
- **Operator pause-clearing procedures.** Two §11.2 failure modes
  produce operational pauses that do not auto-resume and require
  external action before the next cycle succeeds (per D-SPEC-8):
  - `internal_consistency_violation` (§7.3.2 step 4) — assertion-
    failure semantics; indicates a software bug. Investigation
    procedure: review the cycle log to identify which sourcing
    branch fired the violation, compare against current CB state
    and resource conditions, deploy code fix or spec update.
    Pause clears automatically when the next cycle finds the
    bug-triggering condition no longer present (typically because
    the underlying state has changed or a code fix has been
    deployed).
  - `broker_inconsistency` hard-broke sub-cases (§11.2.14
    account-ID mismatch and malformed Trade data) — investigation
    procedure: verify TWS/Gateway configuration matches
    `expected_account_id`, check pinned ib_async version against
    current IBKR API. Pause clears automatically when the next
    cycle's broker connection succeeds without raising
    `BrokerInconsistency`.

  Three §11.2 failure modes that previously required operator
  clearance now alert only and do not pause (per D-SPEC-8):
  `split_brain_detected` (§11.2.11 Layer B), `external_activity_overlap`
  (§11.2.15), and `broker_inconsistency` transient sub-cases. The
  operator still reviews these for forensics, but the system
  continues operating.
- **Broker library maintenance procedure.** Pinned ib_async version
  in `requirements.txt`. Quarterly check for library updates;
  major-version upgrades go through paper-trading verification
  before deployment. If ib_async maintenance ever lapses (the
  community-fork risk discussed in DECISIONS.md D-BROKER-1), the
  Protocol abstraction (§15.2) makes migration to `ibapi` a
  1-2 day project for a competent Python developer rather than a
  rewrite. Designated technical contact (identified in deployment
  records) handles this.

The runbook is operator-facing documentation; this specification is
system-facing.

### 14.4 Simulator-vs-production drift testing

The IPMS simulator and IRAPM production system must remain behaviorally
equivalent for shared logic (decision layer, signal computation,
state machines). A test framework for verifying this equivalence on
identical inputs should be developed. Drift between IPMS and IRAPM is
a class of bug that the IPM v1 generation suffered from.

### 14.5 Phase 3 trigger-year computation edge cases

The schedule_state structure (§3.13) uses `trigger_year` as a fixed
integer. Edge cases at fiscal year boundaries (e.g., Phase 3 latches
on December 31 vs January 1) may produce off-by-one effects in
freeze evaluation or CPI compounding. Simulator should verify
behavior at these boundaries; if necessary, refine to use trigger
*date* with explicit calendar-year derivation rules.

### 14.6 Phase 3 re-validation

The Phase 3 design (§4.1.1 — annuity `I_0` + portfolio-% ceiling +
indexed dollar ceiling) has not been validated end-to-end against
the historical sequence at the current parameter set.

Required validation work before production deployment:

1. **Historical-sequence sweep.** Run the §4.1.1 design against the
   2005–2025 historical sequence (and stressed windows per §13.4
   follow-up) at a spread of trigger portfolios spanning the
   spectrum where each protection mechanism dominates:
   - Small portfolios (~$300K–$500K) where `I_0` from the annuity
     formula is the binding sustainability constraint.
   - Mid-range portfolios ($500K–$1.5M) where neither ceiling
     binds and the schedule runs unclamped against `I_0`.
   - Large portfolios ($2M+) where the indexed dollar ceiling
     binds at or near latch.

2. **Confirm survival** across the spread, or document shortfalls
   and decide whether parameter values need adjustment (the
   annuity-input pessimism in §4.1.1, the 7.5% portfolio ceiling
   rate, or the $4000 base / 2027 base-year of the dollar ceiling).

3. **Confirm ceiling-binding behavior** matches §13.6 expectations:
   small portfolios never bind either ceiling, large portfolios
   bind the dollar ceiling at or near latch, drawdown scenarios
   eventually bind the portfolio-% ceiling.

4. **Reconcile the implementation against the validation results.**
   Replace the illustrative reference values in §4.1.1.4 with the
   freshly-validated test-vector data.

This is the **only blocking open question** for production Phase 3
deployment. The other §14 items are tunable refinements or
implementation work (ruleset.yaml, runbook). §14.6 alone gates
production-deployment confidence for Phase 3.

---

## §15. Broker Layer

This section specifies the design of the broker layer — the subsystem
that mediates between IRAPM's decision/action code and the external
broker (Interactive Brokers). The broker layer is the most safety-
critical subsystem after the decision layer: a bug here can cause
duplicate trades, missed withdrawals, or wrong-account execution.
The design is structured around a strict separation between the
broker-library specifics (ib_async, IBKR's TWS API) and the IRAPM
cycle logic.

### 15.1 Design goals

1. **Library independence.** No part of IRAPM's decision or action
   layer imports or references the broker library (`ib_async`). All
   broker interaction goes through a Protocol (§15.2) whose types
   are plain IRAPM-owned dataclasses.

2. **Idempotency under restart.** A cycle that crashes after
   submitting orders, then restarts, must not double-submit. This
   property holds regardless of where in the cycle the crash
   occurred (§15.5).

3. **Split-brain safety.** If both master and slave somehow
   simultaneously execute cycles (network partition causes false
   slave promotion), the second cycle must detect the first's
   orders at IBKR and abort without duplicate execution (§15.6).

4. **Fail loud.** Every recognized failure mode raises a typed
   exception (§15.4) that maps to a specific operational pause
   reason (§11.2). The system never silently degrades.

5. **Auditable.** Every order placement is logged with its
   deterministic `client_order_id` and recorded in two places: the
   broker (as `orderRef`) and the local `cycle_attempt.json` file.
   Six months later, the operator can reconstruct what IRAPM did
   from either source.

### 15.2 The Broker Protocol

The contract between IRAPM's cycle code and any broker implementation
is the `Broker` Protocol (PEP 544 structural typing). Two concrete
implementations satisfy it:

- **IBKRBroker** — the production implementation, backed by ib_async
  talking to a local TWS or IB Gateway process.
- **SyntheticBroker** — the in-memory implementation used by IPMS
  and unit tests.

The Protocol has 14 methods organized into five groups:

| Group | Methods |
|---|---|
| Connection lifecycle | `connect`, `disconnect`, `is_ready` |
| State queries | `get_positions`, `get_prices`, `get_account_summary`, `get_recent_activity` |
| Order placement | `place_order`, `get_order_status`, `cancel_order` |
| Recurring ACH | `get_recurring_ach`, `update_recurring_ach` |
| Miscellaneous | `get_managed_account_id`, `get_server_time` |

**Data types crossing the protocol boundary** are plain Pydantic
models in `broker_types.py`: `Position`, `Price`, `AccountSummary`,
`OrderResult`, `OrderStatus`, `Fill`, `RecentActivity`,
`RecurringAchInfo`, `AchUpdateResult`, plus the `ContractRef` opaque
handle (§15.7) and the OrderSide / OrderType / TimeInForce /
OrderStatusValue / PriceStatus enums. Every numeric field is
`Decimal`; float values are rejected at parse time with `TypeError`.

**Why a Protocol rather than an abstract base class.** Protocols
are duck-typed by Python's type system, so an implementation need
not inherit from anything — it simply needs to provide the named
methods with compatible signatures. This keeps the implementations
loosely coupled and matches how `SyntheticBroker` (in the IPMS
codebase) and `IBKRBroker` (in IRAPM) can satisfy the same contract
without sharing a class hierarchy. The `@runtime_checkable`
decoration enables `isinstance(broker, Broker)` for one-time
startup verification.

### 15.3 Per-cycle connection lifecycle

IRAPM uses a **per-cycle connection model**: each cycle opens a
fresh broker connection at the start, performs all cycle work,
and closes the connection at the end. This is opposed to a
persistent long-running connection.

```
For each cycle:
  1. connect()        — TCP, API handshake, initial snapshots fetched
  2. ensure_ready     — verify account, verify state-of-readiness
  3. cycle work       — fetch positions, place orders, etc.
  4. disconnect()     — even on exception paths (finally block)
```

**Idempotent and self-healing connect.** `connect()` is idempotent
in the no-op-when-already-connected sense, but the implementation
cross-checks the underlying library's view (`isConnected()`) before
returning early. If the internal flag says \"connected\" but the
library disagrees — the typical case is TWS having restarted or the
socket having dropped between cycles — the broker resets its flag
and performs a real reconnect. This aligns `connect()`'s behavior
with `is_ready()`'s view of reality and supports IRAPM's fail-safe
operating posture: an operator (or the cycle launcher) calling
`connect()` on a stale-flag broker gets an actual fresh connection
rather than a silent no-op that leaves them in the broken state.

**Advantages of per-cycle over persistent:**

- **No stale-snapshot bugs.** Each cycle explicitly waits for the
  initial position/orders/account snapshot to arrive before reading.
  Persistent connections introduce a subtle "is the cache fresh
  enough?" question after every gap between cycles.

- **No background asyncio loop to leak.** ib_async's event loop is
  spun up at connect, torn down at disconnect. No long-running
  coroutine memory leaks.

- **Failure is isolated to one cycle.** A cycle that fails to
  connect aborts cleanly (§11.2.1 broker connectivity loss). The
  next cycle attempts fresh.

- **Reconciliation is implicit.** Every cycle re-fetches positions,
  open orders, executions. The system never trusts cached state
  across cycles. This matches the §6.5 cycle evaluation model and
  the idempotency design (§15.5).

The minor overhead (a few seconds of handshake per cycle) is
negligible against IRAPM's weekly schedule.

**Context manager.** The protocol provides `broker_session(broker)`
as a context manager that enforces the lifecycle. Cycle code always
goes through this; direct `connect()/disconnect()` calls are
discouraged because the disconnect-in-finally guarantee is the
context manager's job.

### 15.4 Typed exception model

The broker layer defines four typed exception classes; raw library
exceptions never escape:

| Exception | Cause | Action-layer response |
|---|---|---|
| `BrokerNotReady` | Method called before `connect()`, or connection lost mid-cycle | Cycle aborts at step 1 (§6.5). §11.2.1 broker_connectivity_loss. Next cycle retries; no operational_pause. |
| `BrokerUnreachable` | TCP/auth/timeout failure (subtype of `BrokerNotReady`) | Same as above; distinguished for log/alert clarity. |
| `BrokerRejection` | Broker accepted the request and explicitly refused (e.g., order rejected, insufficient buying power) | Cycle aborts. `operational_pause` set with `pause_reason='order_rejection'`. §11.2.2. 48h auto-resume. |
| `BrokerInconsistency` | Broker returned data violating an invariant (account mismatch, post-placement timeout, etc.) | Sub-case dependent (§11.2.14): hard-broke cases (account-ID mismatch, malformed Trade data) set `operational_pause` with `pause_reason='broker_inconsistency'`, no auto-resume; self-healed-weird cases (post-placement timeout, pre-place query failure) alert only, no pause. |

`BrokerInconsistency` is the most consequential category. It signals
"something is deeply wrong, halt and alert" — distinct from
`BrokerNotReady` (transient) and `BrokerRejection` (broker is healthy
but disagrees with us). The default 48h auto-resume does not apply;
the operator must explicitly clear the pause after investigation.

### 15.5 Idempotency model

Every cycle attempt has a unique identifier (`cycle_uuid`) and a
captured decision-clock value (`decision_clock`). These are
persisted in a small per-cycle file (`cycle_attempt.json`, §6.7)
on the master's local pSLC SSD before any broker interaction.

**Captured decision_clock — invariant I16:**

> Within a single cycle attempt (uniquely identified by `cycle_uuid`),
> all decision-affecting time queries return the same captured
> `decision_clock` value. Restart of an existing cycle_uuid reuses
> the captured value, NOT current wall-clock time.

This invariant ensures that Plan generation (which I8 requires to
be deterministic given identical inputs) remains deterministic
across cycle restarts. A cycle that begins at T+0 and is restarted
at T+5 minutes after a crash uses the original T+0 decision_clock
for all timing-sensitive decisions; the Plan is identical.

**Client_order_id format:**

```
cycle-{cycle_uuid}-{plan_entry_index}-{symbol}-{side}
```

Example: `cycle-550e8400-e29b-41d4-a716-446655440000-0-SGOV-SELL`

The format is deterministic from the cycle's Plan: same input
state → same cycle_uuid (on restart) and same plan_entry_index
ordering → same client_order_id.

**Pre-place lookup (the killer feature):**

Before submitting any order, `place_order()` calls the protocol's
own `get_recent_activity()` and searches for an order with matching
`orderRef` (the broker-side field where `client_order_id` is
stamped). The lookup window is 48 hours, matching the operational
pause auto-resume window. If found, the existing order is returned
with `idempotent_rediscovery=True`; no new submission occurs.

This single mechanism handles **six failure modes** that would
otherwise cause duplicate execution:

1. **Cycle crash after `placeOrder` returned but before state write.**
   The order is at IBKR; cycle_attempt.json doesn't know. Restart
   computes the same client_order_id, finds the existing order,
   returns it.

2. **Cycle crash mid-`placeOrder`.** Network blip leaves the
   order's true state unknown. Restart's pre-place lookup detects
   it if it landed; doesn't double-submit if it didn't.

3. **Slave promotion after master executed orders.** Newly promoted
   slave generates a fresh cycle_uuid, sees no matching orderRef
   in its lookup — but only because the dead master's orderRefs
   used a different cycle_uuid. If the slave's freshly-generated
   Plan attempts a similar action, it'll use its own client_order_id;
   IBKR will see two orders for the same logical action. This is
   why defense layer 3 (the broader split-brain check, §15.6) is
   needed in addition to the per-cycle idempotency lookup.

4. **PendingSubmit state across restart.** IBKR has the order
   queued but not yet routed; restart's lookup includes
   `reqAllOpenOrders` which returns PendingSubmit orders.

5. **External operator activity.** Operator manually placed an
   order via TWS portal. The recent_activity query returns it with
   empty `client_order_id` (no orderRef); the action layer treats
   this as `external_activity_overlap` (§11.2.15).

6. **Library-level retry.** ib_async retries `placeOrder` on a
   transient error and submits twice. IBKR sees two orders with
   the same orderRef; reqAllOpenOrders returns both; our pre-place
   lookup returns the first one and our submission of the second
   is suppressed.

**Post-placement confirmation:**

After `placeOrder` returns, the implementation waits up to
`POST_PLACEMENT_CONFIRMATION_WINDOW_SEC` (default 15 seconds) for
the order to appear in a recognized state (PendingSubmit /
Submitted / Filled / etc.). If it does not, the order's true state
is unknown — `BrokerInconsistency` is raised. The window is
deliberately generous: a false-positive `BrokerInconsistency` is the
only halt category NOT eligible for 48h auto-resume (requires
operator review), so a few extra seconds of headroom against IBKR
API-callback lag during heavy-load periods is cheap insurance.

If during the confirmation window the order transitions to
`Inactive` with a `whyHeld` reason, `BrokerRejection` is raised
with the reason.

**State file recording:**

After every successful `place_order` call, the placed order is
appended to `cycle_attempt.json`'s `placed_orders` list and the
file is atomically rewritten (write-temp-fsync-rename). This
provides a secondary record alongside IBKR's. Forensic
reconstruction can use either source.

### 15.6 Master/slave coordination via IBKR-as-arbiter

The §9.4.2 master/slave coordination design protects against most
scenarios where two boxes both believe they are master. Defense
layer 3 — IBKR as the arbiter — extends this with a broker-layer
mechanism.

**Each box has a fixed, distinct clientId:**

- Box A: `client_id = 11`
- Box B: `client_id = 12`

Never `client_id = 0` (TWS reserves this for "master client" mode
with different semantics). Each box's `client_id` is in box-local
config (`/etc/irapm/box.yaml`, §15.8), never in the shared ruleset.

**Every cycle queries `get_recent_activity()` before placing
orders.** The query returns three lists (open orders, recently-
completed orders, recent fills) and includes orders placed by
**all clients** on the account, not just this client. This is the
key property: a peer box's orders are visible.

**Conflict detection:**

Before `place_order()` submits, it searches the recent activity
for any order whose `orderRef` matches the proposed
`client_order_id`. The four cases:

1. **No match.** Submit normally. (Common case.)

2. **Match with our own current cycle_uuid.** Idempotent
   rediscovery (§15.5). Return the existing order.

3. **Match with a different cycle_uuid in the orderRef.** This
   would mean a prior cycle (possibly from the peer box) placed
   the same logical action under a different cycle. The plan_entry
   structure makes this extremely unlikely (the cycle_uuid is in
   position 1 of the four-tuple), but if it happened, treat as
   idempotent rediscovery to avoid duplication. Log Critical alert
   for investigation.

4. **Match with empty `orderRef` (no cycle_-prefix in orderRef).**
   External operator activity. Cycle aborts; `external_activity_overlap`
   Critical alert fires; no `operational_pause` is set per §11.2.15
   and D-SPEC-8 — the cycle's declination to act is the heal.

**Post-cycle split-brain detection:**

The cycle records its `last_cycle_clientid` (the client_id used
for the cycle) in the operating state file alongside other state.
If two different `last_cycle_clientid` values appear within a 24h
window, the alerter raises a Critical `split_brain_detected` alert
per §11.2.11 Layer B. This is post-hoc detection — the cycle
itself has completed without harm thanks to the pre-place lookup;
the alert is the receipt that proves the system noticed.

**What this design deliberately does NOT do:**

- It does not use a network filesystem lock to serialize the boxes.
  Network filesystems introduce a new failure mode (the lock
  service) that would expand the failure surface.

- It does not attempt to auto-resolve detected split-brain. Auto-
  resolution code can be wrong; routing through the human via
  `operational_pause` and Critical alert is correct.

- It does not depend on `clientId=0` "master client" mode. That
  mode's semantics (sees all clients' orders in event callbacks)
  are tempting but create different behavior on the two boxes,
  which makes testing harder.

### 15.7 ContractRef: opaque contract identifier

IRAPM cycle code refers to instruments by symbol strings ("FBCG",
"SGOV", etc.). IBKR refers to them by `Contract` objects with
fields like `conId`, `exchange`, `currency`, `secType`. The
broker layer maintains a `symbol → ContractRef` cache that
shields the cycle code from this asymmetry.

`ContractRef` is a Pydantic model with three fields:

- `broker_impl: str` — identifier of the broker implementation that
  minted this ref (`"IBKRBroker"` or `"SyntheticBroker"`). Used by
  the cross-broker safety check: passing a ContractRef minted by
  one broker to a different broker's `place_order()` raises
  `BrokerInconsistency`.

- `symbol: str` — the IRAPM-canonical symbol.

- `payload: dict[str, Any]` — broker-implementation-specific data.
  For IBKRBroker: `{"conId": 12345, "exchange": "SMART", "currency":
  "USD", "secType": "STK", "ib_contract": <ib_async Contract>}`.
  For SyntheticBroker: typically empty. **The cycle layer never
  reads `payload`** — it's preserved across round-trips for the
  broker's own use.

The cache is populated lazily: `get_positions()` and `get_prices()`
both add entries as they encounter symbols. By the time
`place_order()` needs a contract, it's almost always cached. The
cache lives only for the duration of one IBKRBroker instance
(typically one cycle); fresh on next cycle's reconnect.

### 15.8 Box-local configuration (box.yaml)

Per-box configuration that must not be in the shared `ruleset.yaml`:

```yaml
# /etc/irapm/box.yaml — example values
box_id: "box-a"
broker:
  host: "127.0.0.1"
  port: 4001                  # IB Gateway live
  client_id: 11               # 11 for box-A, 12 for box-B
  expected_account_id: "U1234567"
state_file: "/var/lib/irapm/state.json"
cycle_attempt_file: "/var/lib/irapm/cycle_attempt.json"
```

The fields are per-box because:

- `client_id` must differ between boxes (§15.6).
- `host`, `port` are deployment-local and may differ if one box
  uses TWS while the other uses Gateway (not recommended; same
  port on both is the design intent).
- `state_file` and `cycle_attempt_file` paths reflect the local
  filesystem layout.
- `expected_account_id` is the same on both boxes (both should
  connect to the same IBKR account), but it's a deployment-time
  setting rather than a runtime-tunable, so it lives here rather
  than in `ruleset.yaml`.

`box.yaml` is read at process start; mismatched values between
the two boxes (other than `client_id`) are operational errors
that the runbook procedure catches at deployment time.

### 15.9 TWS / IB Gateway deployment requirements

The IBKRBroker implementation assumes specific TWS/Gateway settings.
The runbook §14.3 verifies these at deployment; deviation produces
silent or noisy failure modes.

| Setting | Required value | Why |
|---|---|---|
| API enabled | Yes | Without this, no API connection possible. |
| **"Download open orders on connection"** | **Yes (CHECKED)** | **Critical: the idempotency lookup uses `reqAllOpenOrders`. Without this setting, the snapshot is incomplete and duplicate orders can occur.** |
| "Allow connections from localhost only" | Yes | We never connect from off-box. |
| Trusted IP 127.0.0.1 | Added | Required for the connection to be accepted. |
| Memory Allocation | ≥ 4096 MB | Per ib_async docs; prevents Gateway crashes on bulk data fetches. |
| Read-only API mode | NO (must be OFF) | We need to place orders. |
| Auto-restart | Enabled | The CL260 box may reboot for OS patches; TWS/Gateway should restart automatically. |

The runbook procedure walks the operator through verifying each
setting visually before first cycle execution.

### 15.10 ACH update: conservative-failure design

`update_recurring_ach()` is the most operationally-risky broker
method because:

- The IBKR API surface for ACH updates has historically been
  limited and changes with TWS releases.
- An incorrect update could redirect future monthly transfers to
  the wrong destination or wrong amount.
- ib_async does not currently expose a verified API path.

**The conservative design choice:**

Both `get_recurring_ach()` and `update_recurring_ach()` return
**failure states**:

- `get_recurring_ach()` returns `RecurringAchInfo(is_configured=False)`
  with a Warning log entry.
- `update_recurring_ach(amount)` returns `AchUpdateResult(success=False)`
  with a rejection_reason that the operator must update via the
  IBKR portal manually.

**The action layer treats success=False as a Warning, not a
Critical:**

- The system continues operating at the prior ACH amount (IBKR
  continues the existing recurring transfer untouched).
- An alert with the new amount is sent so the operator knows what
  to update.
- The runbook §14.3 has the manual portal procedure with
  screenshots, written for the survivor as audience.
- The cycle does NOT pause; subsequent cycles re-emit the ACH
  update plan entry, the operator handles it at their convenience.

**When this changes:** Once paper-trading verification establishes
a working ib_async API path for ACH updates, the implementation
replaces this conservative-failure behavior with real updates.
Until then, the conservative-failure design protects against the
single most consequential silent-failure mode in the system.

### 15.11 cycle_attempt.json file format

A small file on the master's local pSLC SSD, separate from the
operating state file. Written multiple times per cycle (every
successful order placement appends a record); finalized at cycle
end with `is_complete=true`.

```json
{
  "cycle_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "decision_clock": "2027-08-11T14:00:00Z",
  "cycle_type": "weekly",
  "started_at": "2027-08-11T14:00:00Z",
  "last_updated_at": "2027-08-11T14:00:15Z",
  "is_complete": false,
  "placed_orders": [
    {
      "client_order_id": "cycle-550e8400-...-0-SGOV-SELL",
      "broker_order_id": "4017",
      "symbol": "SGOV",
      "side": "SELL",
      "quantity": "143.250",
      "order_type": "MKT",
      "limit_price": null,
      "submitted_at": "2027-08-11T14:00:12Z",
      "plan_entry_index": 0,
      "idempotent_rediscovery": false
    }
  ],
  "box_id": "box-a",
  "client_id": 11
}
```

**Lifecycle states:**

- **Fresh:** No `cycle_attempt.json` exists, or the existing file
  has `is_complete=true`. The cycle generates a new `cycle_uuid`
  and captures `decision_clock` at this moment.
- **In-flight:** `cycle_attempt.json` exists with
  `is_complete=false`. This is the "we are running this cycle"
  state.
- **Restart:** Cycle starts and finds an in-flight file. The cycle
  **reuses** `cycle_uuid` and `decision_clock` from the file;
  this is invariant I16 holding.
- **Complete:** Cycle finishes; `is_complete` set to true and the
  file rewritten.

**Per-box; NOT replicated:**

Unlike `state.json` (replicated via rsync per §9.4.2),
`cycle_attempt.json` is master-only. If the master dies mid-cycle
and the slave promotes (§9.4.2 grace window), the slave starts a
fresh CycleAttempt with its own cycle_uuid — it has no visibility
into the dead master's in-flight cycle.

This is by design: broker-side idempotency (the orderRef + 48h
lookback) handles cross-box duplicate detection. The local
`cycle_attempt.json` is for same-box restart only.

### 15.12 Uncertainty flags

Five places in the implementation that need paper-trading
verification before production trust. Each is marked
`UNCERTAINTY FLAG` in the code; the deferred-verification list
covers:

1. **`OrderStatusValue` mapping for TWS `Inactive` status without
   `whyHeld`.** The implementation conservatively treats this as
   REJECTED with `reason='UNKNOWN_INACTIVE'`. Verification needs
   to confirm this is correct across MKT/LMT × cash/margin.

2. **`reqCompletedOrders` retention window.** TWS docs say "today
   and previous day" but real behavior varies. The 48h idempotency
   lookback is conservative against this uncertainty; verification
   confirms the actual window.

3. **`reqMktData` snapshot semantics.** Timeout behavior for
   symbols with no active subscription and whether `reqMktDataType(3)`
   (delayed data) is required for the IRA account configuration.

4. **`get_account_summary` cash tags.** The four tags chosen
   (TotalCashValue, SettledCash, BuyingPower, computed UnsettledCash)
   need verification that they sum/relate as expected for an IRA
   cash account.

5. **`update_recurring_ach` API surface.** The most consequential.
   No verified path exists in ib_async; the conservative-failure
   design (§15.10) handles this until verification establishes one.

The paper-trading verification procedure (a deployment-prerequisite)
exercises each of these explicitly before live use.

---

## §16. Glossary

**Action layer.** The subsystem that executes a Plan (§3.12, §8).
Has no decision authority; only consumes Plans from the decision layer.

**ACHScheduleUpdate.** A Plan entry type (§3.12, §8.2.7) that updates
IBKR's recurring monthly ACH withdrawal amount on the broker side.

**Active (income state).** The income state in which scheduled
withdrawals execute (§3.10a, §6.2). See **Income state**.

**Income state.** Orthogonal to phase. Either ACTIVE (withdrawals
execute per phase's withdrawal calculation) or PAUSED (withdrawals
zeroed), controlled by STOP INCOME tokens (§3.10a, §6.2). Income
state changes do not affect CB state, rebalancing, refill, or the
annual review.

**Allowlist.** The fixed set of symbols (§2.2) that IRAPM operates
on. Per phase: Phase 1/3 = FBCG/AVUV/PYLD/JPIE/SGOV; Phase 2 =
FBCG/AVUV/GBIL/SGOV. Anything outside is invisible to IRAPM (I3).

**Annual review.** A combined event run once per year on the
configured date (§7.6). Performs freeze evaluation and recomputes
buffer/refill/cash targets in Phase 1 and Phase 3 only. In Phase 2,
the annual review cycle runs but performs no recompute work (all
targets are constants or frozen-forward).

**box_id.** Identifier for one of the two CL260 boxes. Uses the
OS-level hostname or auto-generated UUID (§9.4.2). No separate
configuration field.

**Bucket.** A named grouping of one or more positions per active
phase: Core Growth, Core Fixed Income, Buffer (§3.3). A bucket's
value is the sum of its positions' market values.

**Buffer (SGOV).** The 24-month cash-equivalent reserve held in
SGOV outside the core portfolio, drawn during cascade conditions
to avoid forced selling at depressed prices.

**Buffer condition tag.** One of {idle, draining, delayed,
refilling, exhausted} reported in the weekly summary alert (§12.3).

**Cascade.** The withdrawal sourcing sequence (SGOV → FI → Growth)
triggered when CB2 is active, regardless of which entry path
activated CB2 (§7.3.2).

**CB (Circuit Breaker).** A discrete state that governs withdrawal
and rebalance behavior. CB1 transitions are signal-based; CB2 has
three independent entry paths (signal, Portfolio-low, FI-low) per
§3.10 and §6.3.

**CB_INACTIVE.** The default circuit-breaker state when no trip
threshold has been crossed. The CB subsystem is not currently
constraining rebalance or withdrawal behavior (§3.10, §6.3).

**CB1.** Lookback ≤ `cb1_threshold_rate` (default -10%), confirmed
for 2 weeks. Signal-based only. Rebalancing suspended; withdrawals
from FI bucket only (Growth never sold for withdrawals during CB1,
per I14).

**CB1 → CB2 timer.** A transition from CB1 to CB2 fires when CB1
has persisted ≥ `cb1_to_cb2_timer_days` (default 90). Behaves like
CB2 for withdrawals; reached via slow path (§3.10, §6.3.5). The
`signal` condition is recorded in `cb2_entry_conditions` when the
timer fires.

**CB2.** Circuit breaker state entered via any of three independent
conditions, each with 2-week confirmation: signal ≤
`cb2_threshold_rate` (default -20%); core portfolio < Portfolio-low
threshold; core FI < FI-low threshold. Rebalancing suspended;
withdrawals diverted to cascade; large cash deployment uses
defensive mode if signal condition currently holding, else
target-weight-proportional mode. Exits when ALL conditions that
have been active during the episode have cleared with their
respective recovery buffers.

**Cascade exhaustion.** Failure mode where withdrawal sourcing
reaches all three cascade stages (SGOV, FI, Growth) at residual
floor with demand still unmet (§7.3.2 step 4, §11.2.7). Triggers
the `withdrawal_capacity_exhausted` indefinite halt per §11.3.2.

**Cycle.** A single execution of the system's main loop (§3.8). Four
types: weekly, monthly-withdrawal, annual-review, daily-token.

**Decision layer.** The subsystem that consumes operating state and
produces Plans without performing any action (§7).

**Drift.** The difference between a position's current value and its
target value, expressed in dollars or as a percentage (§3.5).

**FI (Fixed Income).** The bonds-and-bills bucket of the core
portfolio. Phase 1/3: PYLD + JPIE. Phase 2: GBIL.

**FI-low.** A CB2 entry condition: core FI bucket below the
configured threshold of `fi_low_threshold_months ×
current_monthly_withdrawal`. Protects against FI depletion
outpacing rebalancing replenishment (§3.10, §6.3.1).

**FI-sacrosanct (I5).** The invariant that FI is never sold to fund
a Growth purchase. Applies to rebalancing trades only; cash refills
and withdrawal sourcing are not constrained by I5.

**Frozen years.** A list of calendar years in which the schedule's
CPI raise was skipped due to CB1+ days exceeding the freeze threshold
(§3.13, §7.6).

**Growth.** The equities bucket of the core portfolio. All phases:
FBCG + AVUV.

**I_0.** The starting monthly income for an income-producing phase
(§3.13). Phase 1: $3,000 (2027 USD). Phase 3: computed at trigger
per §4.1.1.

**Income state.** Orthogonal to phase. Either ACTIVE (withdrawals
execute) or PAUSED (withdrawals zero) (§3.10a, §6.2). Controlled by
STOP INCOME tokens.

**IPMS.** The IRAPM simulator (separate codebase at C:/portfolio/IPMS).
Authoritative test environment for strategy correctness.

**Lookback signal.** See Synthetic Growth Lookback.

**Master / slave.** The two-box coordination roles. Master runs
cycles and writes its local state file; slave runs only a daily
slave-check, reading its local state file (populated by rsync from
master). State-write IS the heartbeat: master's daily writes prove
end-to-end IRAPM functioning, replicated to slave via cron+rsync.
The hybrid local-state + rsync architecture has no shared dependency
to fail (§9.4.2). Role is per-box infrastructure, not part of the
operating-mode tuple.

**Role-swap, not failback.** Design property of the master/slave
coordination: when a failed master is repaired and returns online,
the current master continues running and the returned box becomes
slave. The system does not auto-restore the original master to
primacy. Role swaps reduce state-transition surface area and let
the operator inspect either box's local state file to discover the
current `master_box_id` (§9.4.2).

**Notice.** Severity level (§11.1) for significant state changes
requiring operator awareness; less urgent than Critical, more so
than Warning. Replaces ambiguous "Alert" terminology.

**Operating-mode tuple.** The 3-element effective operating mode at
any moment: (phase, income_state, cb_state). All three elements are
independent state-machine values. Master/slave role is excluded as
per-box infrastructure (§6).

**Operating state file.** The atomic-write JSON file on each box's
pSLC SSD persisting state across cycles (§6.7, §9.4.1). Master
writes; slave reads (populated by rsync). Distinct from the token
observation file (§10.3), which is slave-writable for token
observations only.

**operational_pause.** A structure in the state file that gates
cycle execution while paused. Set automatically by Critical-blocking
failures; auto-resumes after `pause_auto_resume_hours` (default 48)
(§11.3.1).

**withdrawal_capacity_exhausted.** An indefinite-halt flag in the
state file, set when cascade sourcing reaches all three stages at
residual floor with demand unmet (§7.3.2 step 4, §11.2.7).
Suppresses future withdrawal attempts but does not stop other
system functions. Unlike `operational_pause`, the flag has no
48-hour timer-based auto-resume; instead, it auto-clears at the
next withdrawal cycle when SGOV+FI+Growth above-residual ≥ current
monthly withdrawal demand. Recovery happens through cash appearing
in the managed account, picked up automatically via the §2.9 "cash
appears, system reacts" mechanism (§11.3.2).

**Phase.** One of PHASE_1, PHASE_2, PHASE_3 (§3.9, §4). Determines
asset allocation, withdrawal calculation, and active subsystems.

**Phase 3 grace window.** The 24-hour delay between detection of
Phase 3 token removal and Phase 3 latch (§10.6.2).

**Phase transition.** A coordinated reallocation of the portfolio
from one phase's target allocation to the next phase's. Triggered
by calendar date (Phase 1 → Phase 2) or token removal (Phase 1/2
→ Phase 3) (§7.2).

**Plan.** A structured, serializable description of what the system
intends to do this cycle (§3.12). Generated by the decision layer,
consumed by the action layer.

**Plan entry types.** The structured Plan (§3.12) contains zero or
more typed entries: Order, Withdrawal, BufferRefill, CashRefill,
LargeCashDeployment, PhaseTransition, CBStateTransition,
ACHScheduleUpdate, Alert. Each entry has its own action-layer
execution semantics (§8.2).

**LargeCashDeployment.** A plan entry type (§3.12, §7.7.1, §8.2.8)
that deploys a large cash surplus across underweight core positions
in a single cycle. Triggered when cash surplus exceeds the
large-deployment threshold; typical case is Phase 1 annual Roth
conversion inflows.

**Large cash deployment.** The §7.7.1 mechanic that handles bulk
cash inflows (Phase 1 Roth conversions, large deposits) by deploying
to all underweight core positions in a single cycle, distinct from
the bite-by-bite cash buffer surplus rule (§7.7).

**Portfolio-low.** A CB2 entry condition: core portfolio (Growth +
FI, excluding buffer and cash) below the configured threshold of
`max(portfolio_low_threshold_dollars,
portfolio_low_threshold_months × current_monthly_withdrawal)`.
Protects the bootstrap and survivor scenarios where the core
portfolio is too small to sustain withdrawals without external
infusion (§2.9, §3.10, §6.3.1).

**Position residual minimum.** The dollar floor below which a
position is treated as exhausted-for-this-cycle (default $1,500;
configurable). Defends against accidentally liquidating positions
that may be needed in future phases (I12, I13).

**Refill (SGOV).** The mechanism that returns the SGOV buffer to
target by selling Growth (§7.4). Operates outside the rebalancer.
Suspended during cascade conditions and the 60-day post-recovery
delay.

**Schedule state.** A 4-tuple `(I_0, trigger_year, cpi,
frozen_years)` that fully determines scheduled monthly income for
any year in an income-producing phase (§3.13).

**SGOV.** The buffer bucket. Treated as cash-equivalent; held outside
core portfolio accounting.

**STOP INCOME.** The hardware-token mechanism for pausing scheduled
withdrawals (§4.4, §10).

**Synthetic Growth Lookback.** The signal that drives the CB state
machine. Equal-weighted composite return of Growth-bucket symbols
over a 6-month window (§5).

**Pre-transition validation.** Validation performed on the last
weekly cycle falling at least 5 trading days before a Phase 1 →
Phase 2 calendar transition (§4.3 step 1). The 5+ trading days of
buffer gives the operator time to respond to any failure surfaced.

**Token state (valid / invalid).** Valid Phase 3 states are
all-inserted (4 tokens) or all-removed (0 tokens). Valid STOP
INCOME states are all-removed (income active) or all-inserted
(income paused; one token per box). All other configurations
are invalid; the system holds previous state and alerts (§10.5).

**Trigger year.** The calendar year in which a phase activated. For
Phase 1: 2027. For Phase 3: the year the token activation latched.
Immutable after activation (§3.13).

**UNAVAILABLE.** A sentinel value returned by the lookback signal
(§5.2) or token reading (§10.4) when computation cannot proceed. The
system holds previous state when components return UNAVAILABLE.

---

*End of IRAPM Specification.*
