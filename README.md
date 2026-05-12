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
- §14. Open Questions (incl. §14.7 Phase 3 re-validation requirement)
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
- Phase 1 withdrawal amount and inflation rate ($3000/mo in 2027 USD, 3% nominal)
- Phase 3 floor and ceiling brackets (`FLOOR_2026 = $3000`,
  `CEILING_2026 = $5000` in 2026 USD; `INFLATION_PRE = 3.5%`
  indexing rate, continues post-trigger; per §4.1.1.1)
- Phase 3 post-trigger CPI rate (`CPI_POST = 4.0%`)
- Phase 3 sustainable-withdrawal closed-form constants:
  `RETURN_NOM = 6.0%` (assumed nominal portfolio return);
  `HORIZON = 27 years`; `TARGET_TERMINAL = 0.50` (real terminal
  target as fraction of trigger P); `INFLATION_TERMINAL = 2.5%`
  (long-run inflation for terminal target); `SUB_FLOOR_RATE = 6.0%`
  (sustainable rate for sub_floor regime). Per §4.1.1.1.
- Phase 3 per-month payment ceiling
  (`phase3_monthly_payment_ceiling_pct = 0.075`; applied as
  `current_portfolio × pct / 12` per §4.1.1.7)
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
- FI-overweight suppression alert threshold
  (`fi_overweight_suppression_alert_weeks`, default 4, per §7.5.1).
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
`ruleset.yaml` (§14.3) and as the canonical map between parameter
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

**Rebalance suppression alerting:**

| Parameter | Default | Units | Section |
|---|---|---|---|
| `fi_overweight_suppression_alert_weeks` | 4 | weeks | §7.5.1 |

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
