# IRAPM Implementation Decisions

This file records implementation decisions made during the production of
IRAPM artifacts (ruleset.yaml, alert_templates.yaml, state schema, IBKR
adapter design, code). Each entry captures a choice that was NOT strictly
mandated by the spec but had to be decided to produce a working artifact.

The purpose is twofold:
1. Future implementation sessions read this file first and inherit
   decisions rather than re-deciding them.
2. The operator (who is not a coder) has a human-readable log of "what
   was decided" without needing to read the code.

Each entry: date, artifact, decision, rationale, spec reference (if any).

---

## ruleset.yaml decisions (initial draft, 2026-05)

### D-RY-1: Flat keys with subsystem prefixes, not nested structure

**Decision:** ruleset.yaml uses flat top-level keys like `cb1_threshold_pct`
rather than nested structures like `circuit_breakers.cb1.threshold_pct`.

**Rationale:** Flat keys are more grep-able, match how parameters are
referenced in spec body text (always as backtick-quoted flat names), and
avoid YAML's deep-nesting fragility past 3 levels. Top-level grouping is
documentation only (via comment headers organizing the file into 18
subsystem sections).

**Exception:** Target weights (`phase1_target_weights`, etc.) are dicts
of symbol-to-weight. Nesting earns its keep here because flattening would
produce 16+ keys for what's naturally a 4-symbol dict.

**Spec reference:** No explicit guidance. Spec §2.3 / §2.3.1 lists
parameters by name; the file structure is implementation choice.

---

### D-RY-2: Unit suffixes added to parameter names where ambiguous

**Decision:** Parameters like `cb1_threshold` in spec body text become
`cb1_threshold_pct` in ruleset.yaml. Similar for `_dollars`, `_months`,
`_days`, `_hours`, `_weeks`, `_cycles`, `_seconds`.

**Rationale:** Spec body uses bare names (`cb1_threshold`) which are
unit-ambiguous (-10? -0.10? "0.9 of 6-month-ago"?). Adding the suffix
forces clarity in config. Trade-off: small naming mismatch between spec
body and ruleset (spec uses `cb1_threshold`, ruleset uses
`cb1_threshold_pct`). Clarity wins.

**Implication for spec back-port:** A future spec revision should
update the §2.3.1 parameter names to match ruleset.yaml's suffixed
names. Track as a spec-cleanup task.

---

### D-RY-3: Phase 2 deployed weights omit GBIL (Option 3 from review)

**Decision:** `phase2_deployed_target_weights` contains only FBCG and
AVUV, not GBIL. The omission rule is: any position not listed in a
target_weights dict is implicitly held at residual minimum
(`position_residual_minimum_dollars`).

**Rationale:** Cleaner than the alternatives (sentinel string "residual",
or explicit `GBIL: 0.00` with hidden-behavior comment). The omission
rule generalizes — any future state where a position is held at residual
just leaves it out of target_weights.

**Spec reference:** §7.5.2.a (deployed state).

**Coding implication:** Plan-generation logic must check "is this
allowlist symbol absent from current target_weights?" and treat absence
as residual-target.

---

### D-RY-4: Single cash buffer offset key instead of separate phase-specific keys

**Decision:** One key `cash_buffer_offset_dollars: 1000` instead of two
keys (`cash_buffer_target_phase1_offset_dollars` and
`cash_buffer_target_phase2_dollars`). Formula:
`target = current_monthly_withdrawal + cash_buffer_offset_dollars`.

**Rationale:** In Phase 2 `current_monthly_withdrawal` is 0, so the
formula naturally produces $1000 target — matching spec's Phase 2
transaction-reserve-only sizing. Single key avoids phase-conditional
logic in code; the math handles both cases.

**Spec implication:** §2.3.1's parameter table lists two separate keys.
This is a deliberate ruleset-vs-spec divergence. The spec body's prose
description ("Phase 2 target = $1000") still holds — it's just produced
from the formula rather than a dedicated parameter. Back-port to spec
in a future cleanup pass.

---

### D-RY-5: Provenance markers in comments

**Decision:** Every parameter's inline comment includes a provenance
marker:
- `[VALIDATED]` — carried from IPM v1 production use
- `[SPEC]` — specified in IRAPM spec body, authority but not necessarily
  IPMS-validated
- `[PROVISIONAL]` — reasonable starting value, should be IPMS-tuned
  before production deployment (per §14.2)
- `[LEGACY-DRIFT]` — differs from PHASE_3_DESIGN.md legacy parameters,
  requires §14.7 re-validation before production

**Rationale:** Distinguishes "known-good" values from "needs-tuning"
values. Without markers, a reader can't tell whether a default
represents accumulated experience or a placeholder. Future sessions
and operators can prioritize what to validate first.

**Implication:** When IPMS validation work completes for a parameter,
its marker upgrades. The §14.7 re-validation sweep specifically
addresses all `[LEGACY-DRIFT]` markers.

---

### D-RY-6: Dates as quoted YAML strings, not bare date literals

**Decision:** `phase1_to_phase2_transition_date: "2035-01-01"` and
`phase2_reallocation_dates: ["01-15", "07-15"]` and
`annual_review_date: "01-15"` all use quoted strings.

**Rationale:** YAML date parsing differs slightly across libraries
(some interpret `2035-01-01` as a date object, some as a string). Quoted
strings are unambiguous and portable. Code parses MM-DD strings as
month-day pairs and full ISO dates as full dates.

**Spec reference:** Spec body uses ISO date format in places (e.g.,
"2035-01-01") without specifying YAML type. Implementation choice.

---

### D-RY-7: Heavy comments retained (operator-of-record requested)

**Decision:** Every parameter has 1-3 sentences of comment explaining
what it does, which spec section it comes from, and any relevant
caveats. File grew to 662 lines vs ~150 lines for bare key:value.

**Rationale:** Operator explicitly requested heavy comments as a safety
net during development. Self-documenting file allows future sessions to
read just ruleset.yaml without needing the spec for context. The cost
(file length) is documentation density, not configuration complexity.

---

### D-RY-8: `ach_destination` initialized to empty string

**Decision:** `ach_destination: ""` with `[PROVISIONAL]` marker.

**Rationale:** Genuinely cannot have a default — this is operator-
specific (their actual IBKR bank reference). Empty string is the
canonical "not yet set" value; loader logic must reject empty string
at startup validation (per I9, invalid config prevents startup).

**Coding implication:** Add startup validation: `ach_destination` must
be non-empty before any cycle runs. This is the operator's deployment-
time setup step.

---

## alert_templates.yaml decisions (initial draft, 2026-05)

### D-AT-1: All alerts dispatch via BOTH email AND SMS

**Decision:** Every alert template in `alert_templates.yaml` has both
a `body` (email) and an `sms` field. There are no email-only or
SMS-only alerts.

**Rationale:** The operator regularly travels to locations with cell
coverage but no WiFi or data plan (rural travel, cabins, cruise ships
in port). Email delivery requires internet connectivity end-to-end;
SMS uses the cellular voice/text network, which has fundamentally
wider coverage. For a system whose entire purpose is to keep
operating unattended during long operator absences, "reachable by
cell but not internet" is a real and common scenario. Restricting
any alert to email-only would create a class of alerts the operator
could be silently unaware of for weeks at a time.

**SMS template requirement:** Each `sms` field must be self-sufficient
— a survivor or operator reading only the SMS should know what
happened and what (if anything) to do. The email body is rich-context
backup with the same information plus details. SMS ≤ 140 characters
where possible to fit one cellular segment.

**Spec changes:** §12.6 alert catalog updated. All "Channels" column
entries unified to "Both" (previously some alerts were "Email" only).
§12.6 intro paragraph added explaining the cellular-coverage rationale.

**Coding implication:** The alerter (§9.3) must dispatch both channels
for every alert. If one channel succeeds and the other fails, that's
a `alerter_failure_recovered` follow-up alert situation, not a
fail-the-cycle event.

---

## Spec changes during alert_templates.yaml drafting (2026-05)

### D-SPEC-1: Phase 3 grace-window abort rule changed (§10.6.2)

**Decision:** §10.6.2 Stage 2 logic rewritten. The earlier rule
required all four Phase 3 tokens re-inserted to abort the grace
window (Patterns 1/2/3). The new rule: **any token re-insertion on
either box aborts**, with one-cycle persistence confirmation to
defend against transient USB-read glitches. Reduces Stage 2 to two
patterns instead of three.

**Rationale:** Asymmetry of consequences strongly favors easy-to-abort.
A false Phase 3 latch is permanent, irrevocable, and reallocates the
portfolio to the wrong target weights with the wrong starting income.
A false abort means the survivor's tokens "didn't quite work" — they
remove tokens again and the grace window restarts cleanly. The
audience this matters most for is the survivor who may be confused
or grieving; lowest-barrier-to-abort is the safest rule.

The persistence requirement (re-insertion must hold across one full
daily-token cycle before the abort commits) closes the transient-
glitch false-abort path. Cost: ~24 hours of additional grace-window
time in the survivor scenario. Trade-off is worthwhile.

**Spec sections edited:**
- §10.6.2 Stage 2 patterns rewritten (Patterns 1/2/3 → Pattern A/B)
- §10.5 forward-reference to grace-window exception updated
- §10.5 valid-states paragraph notes the grace-window exception

**Coding implication:** State file needs to track a "pending abort"
flag during the grace window — the first observation of re-insertion
sets the pending flag and triggers the Notice alert; the second
consecutive cycle with re-insertion observed commits the abort. If
the second cycle reverts to all-removed, the pending flag clears and
grace continues normally.

**Alert wording reflects the new rule** in `phase3_grace_started` and
`phase3_grace_aborted` templates.

---

## 2026-05 batch edit decisions (Gemini findings + simplification pass)

This block records 11 decisions made during a single batch-edit session
that combined four defects found by an external reviewer (Gemini) with
seven simplification decisions arising from a "simple and elegant"
design review. The decisions are listed in the order they were resolved
during the session.

---

### D-SPEC-2: Phase 3 latch vs. monthly withdrawal race condition

**Decision:** Add invariant I15 to §3.14 plus guard clauses to §7.3 and
§7.5.1. When `phase == PHASE_3 AND schedule_state.phase3 is null`
(the window between the Phase 3 latch writing `phase=PHASE_3` to state
and the next weekly cycle initializing `schedule_state.phase3`), the
decision layer treats this as transition-pending and suppresses all
withdrawal and rebalance Plan generation. The Phase 3 transition cycle
is the only cycle permitted to generate a Plan in this window.

**Rationale:** Without this defense, a monthly-withdrawal cycle firing
in the latch-vs-transition window would read `phase=PHASE_3`,
`income_state=ACTIVE`, attempt to look up `schedule_state.phase3` (null),
and either crash or trigger `internal_consistency_violation`. The fix
chosen was Option B (decision-layer guard) over Option A (immediate
synchronous transition at latch) because B doesn't break the daily-
token cycle's "no broker queries / minimal work" property — the latch
step stays minimal, and the transition work happens in its proper
context (the next weekly cycle's full evaluation).

**Cost:** At most one missed monthly withdrawal in the rare survivor
scenario where tokens activate right before the 15th of the month.
The next weekly cycle's transition completes and the subsequent
monthly-withdrawal step pays normally.

**Plan invariant:** D12 codifies the rule as a test assertion.

---

### D-SPEC-3: Large cash deployment math — target-weight proportional algorithm

**Decision:** Replace the §7.7.1 "single-plan multi-position BUY
restoring all underweight core positions to their target weights in
proportion" phrasing with an explicit algorithm: for each position s
in the active phase's target_weights, `buy_dollars[s] = cash_to_deploy
× target_weight[s]`. No reading of current position values; no
proportional-to-deficit math.

**Rationale:** The original phrasing "in proportion" was ambiguous —
a coder reading "underweight positions in proportion" might naturally
implement "in proportion to current deficits," which produces
catastrophically wrong allocation for large inflows. The target-weight-
proportional algorithm is simpler, has no edge cases (no
divide-by-zero when zero positions are underweight, no overshoot when
inflow is large relative to portfolio), and is consistent with the
spec's design philosophy of accepting bounded drift between explicit
correction events (cf. §7.5.2.b semi-annual reallocation).

**Trade-off acknowledged:** Post-inflow drift up to 5/25 tolerance
levels persists until the next 5/25 evaluation. The operator's
operating pattern (Roth conversions land early January, right before
the Jan 15 annual review which recalibrates buffer targets) means the
pathological case (large inflow against a wildly-overweight position)
is not reachable.

**Plan invariant:** D11 codifies the algorithm as a test assertion.

---

### D-RY-9: Percentage scaling — `_pct` → `_rate` migration

**Decision:** Standardize all rate-like parameters in ruleset.yaml to
decimal (`0.05`, not `5`) with `_rate` suffix. 15 parameters renamed.
Three existing `_rate`-suffixed decimal parameters (phase1_inflation_rate,
phase3_sub_floor_rate, phase3_monthly_payment_ceiling_pct→_rate)
already followed the convention or were brought into it. Spec §2.3.1
parameter table updated to match; inline references throughout the
spec body propagated via bulk find-and-replace.

**Rationale:** Mixed scales (some `_pct` integer like `5`, some
`_pct` decimal like `0.075`) created the exact ambiguity Gemini
flagged: a coder seeing `phase3_monthly_payment_ceiling_pct: 0.075`
might apply `value / 100` (treating it as integer-pct), computing
0.00075 instead of 0.075 — a 100× error. Standardizing on decimals
with `_rate` suffix eliminates the entire class of scaling bugs
because the decimal value is exactly what gets multiplied. The
`_rate` suffix is semantically clearer than `_pct` for dimensionless
ratios.

---

### D-CODE-1: Typed Ruleset model + new_initial_state refactor

**Decision:** Create `ruleset_model.py` containing a Pydantic `Ruleset`
class that mirrors `ruleset.yaml` schema. `Ruleset.from_yaml(path)` is
the canonical loader. `state_model.py`'s `new_initial_state(ruleset,
box_id, ipv4_last_octet)` takes a typed Ruleset object and reads all
financial constants from it — no magic numbers in code.

**Rationale:** Spec §2.3 explicitly bans hardcoded financial values in
code. The earlier `new_initial_state` had seven magic numbers
(phase1_initial_monthly_dollars, phase1_inflation_rate, trigger_year,
buffer constants). Wrapping them in a typed model gives three
benefits: (1) compliance with §2.3; (2) startup fails loudly on
malformed ruleset.yaml rather than silently propagating garbage;
(3) the typed interface makes consumer code self-documenting.

**Module separation:** Per §2.4 (Modularity principle), config types
and state types live in separate modules. `ruleset_model.py` =
config types; `state_model.py` = runtime state types. Forward-
reference via TYPE_CHECKING avoids circular imports.

---

### D-CODE-2: Ruleset model validator trim

**Decision:** Trim ruleset_model.py validators to the minimum useful
set. Keep: `extra="forbid"`, `Field(gt=0)` type-level constraints on
integer durations, decimal coercion via `Decimal(str(v))`,
`ach_destination_non_empty`, date/time/cycle-schedule format
validators, TargetWeights weights-sum-to-1.0. Drop:
`drawdown_triggers_negative`, `hysteresis_positive`,
`fraction_in_unit_interval`, `coverage_in_unit_interval`,
`annual_rate_sane`, `dollar_amounts_positive`, `cb_severity_ordering`,
`phase3_bracket_real`.

**Rationale:** The operator authors ruleset.yaml; they will not write
`cb1_threshold_rate: 0.10` (positive when it should be negative) or
`cb2_threshold_rate: -0.05` (less severe than CB1). If they do, the
mistake manifests visibly within one cycle when CB2 fires before CB1
or CB1 never fires at all — and the operational pause framework
catches downstream consequences. The cross-field validators were
defending against typos a human author would not make and added
permanent maintenance surface area.

**Kept validators are the ones that protect against latent failures**:
ach_destination empty at deployment (silent — no error until first
withdrawal cycle, weeks later), bad date format (silent — calendar
math throws cryptic errors deep in code), weights not summing to 1.0
(silent — produces visibly-wrong allocation that may take time to
diagnose).

---

### D-RY-10: Single inflation_rate parameter

**Decision:** Collapse the three prior inflation rates
(`phase1_inflation_rate: 0.03`, `phase3_inflation_pre: 0.035`,
`phase3_cpi_post: 0.04`) plus the unused `phase3_inflation_terminal:
0.025` into one canonical `inflation_rate: 0.035` parameter applied
to all withdrawal-schedule raises across Phase 1 and Phase 3.

**Rationale:** Three rates produced bookkeeping complexity without
behaviorally-distinct effects, since the realized inflation
environment dominates whichever spec rate is chosen. The Phase 3 I_0
calculation retains a separate higher rate
(`phase3_i0_calc_inflation_assumption: 0.04`) as a deliberately-
conservative input to the one-time sustainability formula at latch
— this is not an inflation prediction but a safety buffer that
produces a lower I_0 than canonical inflation would suggest. The
3.5% rate is a compromise between the operator's prior conservative-
Phase 1 (3.0%) and conservative-survivor-Phase 3 (4.0%) choices.

---

### D-RY-11: CB1-extended → simple CB1→CB2 timer

**Decision:** Remove the CB1-extended derived sub-state. Replace with
a direct CB1→CB2 timer transition: when CB1 has been continuously
active for ≥ `cb1_to_cb2_timer_days` (default 90, up from the prior
60), transition to CB2 fires immediately. This is in addition to the
existing signal-based CB1→CB2 path (signal ≤ cb2_threshold_rate × 2
cycles). The parameter `cb1_extended_trigger_days` is renamed
`cb1_to_cb2_timer_days`.

**Rationale:** CB1-extended was a derived state with its own cascade-
routing behavior, but the routing it triggered was essentially
identical to CB2's. Two states with the same operational effect is
complexity without payoff. Promoting "long CB1" to CB2 directly
preserves the operational protection while removing: the derived-
state computation, the parallel cascade-routing logic, the
CB1-extended alert variant, and the conceptual baggage of "a state
that isn't really a state but is computed each cycle."

---

### D-RY-12: Guards demoted to alerts

**Decision:** Remove the FI-depleted and Portfolio-depleted guard
state machines entirely. Replace with two simple Warning-severity
alerts: `fi_low_alert` (FI bucket < 6 months of withdrawals),
`portfolio_low_alert` (total core < max($50K, 18 months)). Alerts
re-fire weekly while condition persists and stop firing
automatically when condition clears (no separate deactivation
alert). Parameters renamed: `fi_depleted_threshold_months` →
`fi_low_alert_threshold_months`, `portfolio_depleted_floor_*` →
`portfolio_low_alert_floor_*`. The two `_clear_hysteresis_rate`
parameters are removed. The `guards` field is removed from
state_schema.json and state_model.py.

**Rationale:** The guards' two roles (cascade routing + alerting)
were partially redundant with existing machinery. The §11.3
operational pause framework catches order rejection when residual
floors prevent sales (the actual depletion edge). The
`position_residual_minimum_dollars` floors prevent sales below
per-symbol residual independent of guards. The remaining role —
"alert the operator before residual floors engage" — is achieved by
the new simple alerts. Net effect: same safety properties, fewer
state machines, no hysteresis bookkeeping.

**v1.4 clarification (preserved behaviors).** The original decision
above understated the operational role of `portfolio_low_alert`'s
underlying threshold. The v1.4 consistency sweep clarified that
**three behaviors** from the prior `portfolio_depleted` guard are
preserved as stateless threshold checks (no persisted state, no
hysteresis), evaluated independently at the relevant decision
points, all codified under invariant I10:

  (a) **SGOV refill suspended** when core portfolio is below the
      threshold (§7.4 of spec) — don't drain a depleted portfolio.
  (b) **5/25 rebalancing suspended** when core portfolio is below
      the threshold (§7.5 of spec) — don't churn a depleted
      portfolio with trades disproportionate to its size.
  (c) **Withdrawal routing diverts to SGOV-first cascade** when
      core portfolio is below the threshold, regardless of CB state
      (§7.3.2 of spec) — extend runway by drawing on the buffer
      first; gives the core room to recover before more selling
      pressure.

These are *not* state-machine behaviors. There is no "guard active"
flag, no persisted hysteresis, no auto-clear-on-rise-above-threshold
event. Each decision point independently re-evaluates the same
threshold against current portfolio value. The alert and the gates
fire from the same condition but are otherwise independent
evaluations.

The `fi_low_alert` remains purely informational with no behavioral
effect; only `portfolio_low_alert` has the three preserved gates.

This clarification restates the truth about D-RY-12 without
reversing it: the safety properties of the prior guards are
preserved, just implemented as stateless checks rather than as
parallel state machines with hysteresis bookkeeping.

---

### D-RY-13: Phase 1 freeze mechanism removed

**Decision:** Remove the Phase 1 freeze mechanism entirely. Phase 1
withdrawal grows at `inflation_rate` per year without any prior-year
CB-activity freeze evaluation. `phase1_trigger_year` becomes a simple
anchor for the schedule formula:
`scheduled = phase1_initial_monthly_dollars × (1 + inflation_rate) ** (current_year - phase1_trigger_year)`.
The freeze mechanism remains intact for Phase 3.

**Rationale:** Phase 1's 8-year horizon means at most a handful of
freeze opportunities. The operator's annual contribution-and-
withdrawal pattern dominates whatever freezes would save —
approximately $90/month-equivalent in subsequent years per frozen
year, against an operator who can manually pause income if they
choose. Phase 3's multi-decade horizon is where freezes matter; the
mechanism is retained there.

**Implementation:** ScheduleStateInstance keeps its uniform shape
(i_0_dollars, trigger_year, cpi_rate, frozen_years) — Phase 1's
`frozen_years` is always empty list, `cpi_rate` always equals
`inflation_rate`. The schedule formula's freeze-aware computation
is no-op for Phase 1 since the list is always empty.

---

### D-RY-14: Phase 3 design overhaul — annuity I_0 + ceiling

**Decision:** Replace the entire §4.1.1 Phase 3 bracket math
(floor/ceiling indexing, four-regime classification, sub-floor
sustainable rate, indexed-terminal-target sustainability calc) with
a single 30-year finite-horizon annuity formula:

```
I_0 = portfolio_value × (r - i) / (12 × (1 - ((1+i)/(1+r)) ** N))
```

with `r=0.06`, `i=0.04`, `N=30`. After latch, schedule grows at
canonical `inflation_rate` (0.035) with Phase 3 freeze mechanism.
Each cycle, monthly payment clamped at `portfolio × 0.075 / 12`
(7.5% annualized ceiling); ceiling-bound emits Notice alert,
no other state effect.

**Parameters removed (9):** `phase3_floor_2026_dollars`,
`phase3_ceiling_2026_dollars`, `phase3_inflation_pre`,
`phase3_cpi_post`, `phase3_inflation_terminal`,
`phase3_target_terminal`, `phase3_sub_floor_rate`,
`phase3_return_nominal`, `phase3_horizon_years`.

**Parameters added (4):** `phase3_i0_calc_return_assumption: 0.06`
(historical-conservative, headroom against future returns trailing
historical), `phase3_i0_calc_inflation_assumption: 0.04` (above the
canonical 3.5%; the gap is survivor headroom against actual
inflation exceeding 3.5%), `phase3_i0_calc_horizon_years: 30`
(survivor age 68 + 30 = age 98), `phase3_monthly_payment_ceiling_rate:
0.075` (was previously phase3_monthly_payment_ceiling_pct, just
renamed and re-classified as the single Phase 3 safety clamp).

**Rationale:** The bracket-math scheme had 7 [LEGACY-DRIFT] parameters
that were never IPMS-validated, included a sub-floor regime that
required understanding four classification cases, and produced more
complexity than benefit. The annuity formula is one equation,
operator-verifiable across reasonable portfolio sizes ($500K → ~$1.9K
I_0; $1.5M → ~$5.7K I_0; ~4.58% annualized initial withdrawal rate
well below the 7.5% ceiling). The ceiling clamp simultaneously
defends against: I_0 calibrated against a temporarily-inflated
portfolio at latch, portfolio drawdown after latch, and compounding
raises outstripping corpus. Net reduction: ~280 lines of spec §4.1.1
collapse to ~130 lines.

**Survivor-cushion rationale:** The 4% calc-side inflation against
3.5% actual-raise inflation gives the survivor about 13% lower
year-30 nominal income than the pessimistic-inflation assumption
would have produced — but more critically, leaves portfolio headroom
if realized inflation runs above 3.5%. The 6% calc-side return
against historical ~7% gives similar headroom on the return side.

---

### D-SPEC-4: Two-cycle architecture with date-gated weekly steps

**Decision:** Collapse the prior 4-cycle architecture (daily-token,
weekly, monthly-withdrawal, annual-review) into 2 cycles:
daily-token and weekly. Monthly withdrawal, annual review, Phase 2
semi-annual reallocation, and pre-transition validation all become
**date-gated steps within the weekly cycle**. The withdrawal step
executes on the latest Wednesday ≤ (4 business days before the 15th
of the current month); annual review on the weekly cycle on or
after Jan 15; semi-annual reallocation on weekly cycles on or after
each `phase2_reallocation_dates` entry.

**Rationale:** The 4-cycle architecture introduced cycle-collision
logic, scheduler complexity, and parallel "what does each cycle
type do" tracking. Folding the special-cadence work into date-
gated weekly-cycle steps preserves the timing constraints (SELL
clears T+1 with adequate ACH headroom; annual review runs near
Jan 15) while reducing the architecture to: daily-fast (token
checks) + weekly-full (everything else). The withdrawal SELL now
places on Wednesday 10:00 ET during market hours (rather than the
prior pre-market 07:00 ET) — operationally equivalent or slightly
better fills.

**Timing analysis:** In every month, the latest Wednesday on or
before the withdrawal day gives the SELL at least 4 business days
of settlement+ACH headroom before the 15th. Some months the
withdrawal places up to 10 calendar days early (cash sits in cash
account before ACH pull); the interest cost is negligible.

---

### D-SPEC-5: `withdrawal_capacity_exhausted` clarified as indefinite-halt with single auto-clear path

**Context.** The pre-v1.4 spec contained two contradictory passages
on the semantics of the `withdrawal_capacity_exhausted` flag:

  - §11.2.7 (Cascade exhaustion) described an auto-clear: "if
    external action restores portfolio value, the flag auto-clears
    when the cascade can again meet a hypothetical withdrawal
    demand at the next withdrawal cycle."
  - §11.3 (Operational pause framework) said the halt was
    "intentionally permanent because there is no recovery IRAPM can
    perform on its own — the portfolio cannot meet demand, and only
    external action ... changes the situation."

The two passages also disagreed on whether operator state-file
edits were permissible (§11.2.7 mentioned "manual state-file edit
after consultation with the runbook"; §11.3 said "the operator
never edits the state file").

**Decision.** The flag is an **indefinite halt with a single
auto-clear path**, not a strictly permanent halt:

  - The halt persists indefinitely as long as the cascade cannot
    fund a withdrawal at residual floors.
  - The **only** auto-clear path: at each subsequent withdrawal
    cycle, the system evaluates whether SGOV+FI+Growth above-
    residual ≥ current monthly withdrawal. If yes, the flag clears
    and normal withdrawal sourcing resumes that cycle.
  - There is **no 48h timer-based auto-resume** (unlike
    `operational_pause`).
  - There is **no operator state-file edit path**. The system is
    self-managed; recovery happens through cash appearing in the
    managed account (per the §2.9 "cash appears, system reacts"
    mechanism), which the cascade re-evaluation picks up
    automatically on the next withdrawal cycle.

**Rationale.** The decision threads a needle between two
operational requirements:

  - **Don't churn a hopeless situation.** Cascade exhaustion means
    the portfolio at residual floors cannot meet demand. Repeated
    withdrawal attempts achieve nothing and just generate alert
    noise. The indefinite halt prevents that churn.
  - **Don't require operator intervention to recover.** IRAPM is
    designed to run unattended through long absences (operator
    travel, hospitalization, death). If withdrawal_capacity_exhausted
    were strictly permanent until manual state-file edit, an
    operator who returns to find the system halted faces an awkward
    repair task during a stressful time. The auto-clear when
    capacity returns lets external cash injection (Roth conversion,
    deposit, dividend accumulation past the threshold) restore
    normal operation without manual intervention.

The single auto-clear path is precise: cascade returning to
feasibility at a withdrawal cycle. Not "core rises above some
threshold." Not "operator clears the flag." The same condition that
*triggered* exhaustion (cascade can't meet demand at floors) is the
condition that *clears* it (cascade can meet demand at floors).

This makes the halt monotonic in a useful way: as long as the
portfolio actually cannot fund withdrawals, the system stops trying
to fund them; the moment it can again, it resumes. No operator
toggle, no timer, no race condition between "system thinks it's
recovered" and "operator confirms".

**Implementation in v1.4 spec.** §11.2.7 Recovery bullet, §11.3
"Indefinite halt: withdrawal_capacity_exhausted" block, §11.3
pause-reason catalog row, §11.5 resolved-questions auto-resume
bullet, and the glossary entry were all rewritten to express this
single coherent design.

---

### D-BROKER-1: Library choice — ib_async (with Protocol-based swap insurance)

**Context.** IRAPM v2 needs to integrate with Interactive Brokers'
TWS/Gateway API. Three realistic Python options exist:

- `ibapi` — the official IBKR Python library. Callback-based,
  low-level, every operation is "send request, wait for callback on
  a different thread." Correct code requires writing substantial
  request-tracking and completion-future plumbing for every method.
  No third-party dependency risk.
- `ib_insync` — the dominant async wrapper. Original author Ewald
  de Wit stopped maintaining it in early 2024. Still works but
  frozen.
- `ib_async` — community fork that took over after `ib_insync` was
  deprecated. Maintained by Matt Stancliff under the
  `ib-api-reloaded` org. Same API surface as `ib_insync` with
  renamed import.

**Decision.** Use `ib_async`, pinned exactly to a known version,
with the `Broker` Protocol abstraction (§15.2) as swap insurance.

**Reasoning.**

The synchronous-feeling API on top of an async event loop is a
major productivity win for IRAPM's sequential cycle logic ("fetch
positions → compute plan → place orders → reconcile fills"). With
raw `ibapi`, every operation requires custom request-tracking and
completion-future plumbing; with `ib_async`, the same logic reads
as straightforward sequential code.

Active maintenance matters for a system intended to run unattended
for years. `ib_insync` is a freeze that will eventually break against
an IBKR API update; `ibapi` is fine but transfers all the wrapper-
writing work to IRAPM. `ib_async`'s active maintenance covers the
sustaining work.

The community-fork risk is real but bounded. If `ib_async`
maintenance ever lapses, the failure mode is: a future IBKR-side
TWS/Gateway API change breaks the pinned `ib_async` version, the
connection refuses on next cycle, IRAPM detects this as
`BrokerUnreachable` (loud failure mode A from the operational
discussion), and operational_pause is set. The designated technical
contact handles migration to `ibapi`. Because IRAPM's Protocol
abstraction keeps IBKR-specific concepts inside one file
(`ibkr_broker.py`), this migration is a 1-2 day project for a
competent Python developer rather than a rewrite.

**Widow-survivability framing.** The library-staleness risk in a
multi-year operator-absence scenario manifests as Shape-A failure
(loud, connection-refused, system halts with clear alert) rather
than Shape-B (silent data corruption). The §14.4 runbook covers
the manual portal fallback for withdrawals if connectivity stays
broken long enough to require it; the migration to `ibapi` is the
permanent fix, tackled by the technical contact.

**Implementation in v1.5 spec.** §15.1 design goal "library
independence"; §15.2 Broker Protocol; §15.13 resolved-questions Q&A
on the library choice; module-level docstring in
`broker_protocol.py` records the library decision and the
abandonment-mitigation strategy.


### D-BROKER-2: Per-cycle connection lifecycle (not persistent)

**Context.** Two connection models are possible:

- **Persistent.** Single long-running TWS/Gateway connection
  established at IRAPM process start; held open between cycles;
  reconnected on transient failure.
- **Per-cycle.** Each cycle opens a fresh connection at start,
  performs all work, closes the connection at end (in a finally
  block).

**Decision.** Per-cycle. Each cycle: `connect()` → `ensure_ready`
→ cycle work → `disconnect()`.

**Reasoning.**

The single biggest source of TWS integration bugs is the
"snapshot freshness" question. After connecting to TWS, ib_async
needs to subscribe to positions / open orders / account values /
executions; during this subscription phase, reads return stale or
empty data. A persistent connection forces every cycle to ask
"is the cache fresh enough?" — and getting that judgment wrong
(e.g., reading positions immediately after a brief network hiccup
that ib_async logged but the cycle didn't notice) produces silent
stale-data bugs that can lead to massive incorrect orders.

The per-cycle model eliminates this category of bug by construction:
`connect()` blocks until snapshots arrive (ib_async's documented
behavior is "after the connection is made the client is fully
synchronized and ready to serve requests"). Each cycle then reads
fresh data and disconnects. No long-running asyncio loop, no
between-cycle state to debug.

Other benefits:

- **Failure isolation.** A cycle that fails to connect aborts
  cleanly (§11.2.1 broker_connectivity_loss); the next cycle
  attempts fresh against a clean state.
- **No event-loop leaks.** ib_async's asyncio loop is spun up at
  connect, torn down at disconnect. No long-running coroutine
  memory leaks to debug six months in.
- **Reconciliation is implicit.** Every cycle re-fetches positions,
  open orders, executions — implementing the "never trust cached
  state across cycles" principle naturally.

The minor cost (a few seconds of handshake per cycle) is
negligible against IRAPM's weekly schedule.

**What this rules out.** Long-running event-driven trade
management. IRAPM is batch by nature (one cycle decides, places,
reconciles, exits); the design choice cements that.

**Implementation in v1.5 spec.** §15.3 per-cycle connection
lifecycle section; `broker_session()` context manager in
`broker_protocol.py` enforces the disconnect-in-finally
guarantee.


### D-BROKER-3: Every cycle queries IBKR for recent activity (defense layer 3)

**Context.** The master/slave coordination model (§9.4) protects
against state-file split-brain via heartbeat timing and the IP
last-octet tiebreak (§9.4.3). But this protection operates at the
state-file level. A scenario where state-file coordination fails
or has a race window — both boxes briefly believing they are
master — could cause both boxes to contact the broker and place
orders.

**Decision.** Every cycle queries IBKR for recent activity (open
orders + recently-completed orders + recent fills) before placing
any order. The activity query includes orders placed by all
clients on the account, not just this client. Defense layer 3:
IBKR itself is the arbiter of "what orders exist on the account".

**Reasoning.**

The pre-place lookup converts split-brain from "potential
duplicate-execution disaster" into "second box detects the first's
work and aborts with a Critical alert". The IBKR account log is
the single source of truth that both boxes can independently
consult; using it for the per-order idempotency check costs one
extra API round-trip per cycle and eliminates an entire class of
race conditions.

**Why on every cycle, not just on suspected-split-brain cycles.**

Exercising the code path in the common case keeps it tested. A
conditional path that "only runs when split-brain might be active"
would rarely fire in production, would accumulate subtle bugs over
time, and would not be the path that fires when an actual split-
brain event happens. The unconditional design makes defense
layer 3 the same code path in all conditions — simpler, more
testable, and self-validating against the cycle's own placed
orders (the idempotency-on-restart property, §15.5).

**Implementation in v1.5 spec.** §15.6 master/slave coordination
via IBKR-as-arbiter; §11.2.11 Layer B coverage of the post-hoc
detection mechanism via `last_cycle_clientid`.


### D-BROKER-4: Idempotency via deterministic client_order_id + IBKR orderRef

**Context.** A cycle that crashes after submitting orders, then
restarts, must not double-submit. Six concrete failure modes
require this protection (catalogued in §15.5 design discussion):
crash-after-submit-before-state-write, network-blip-mid-placeOrder,
slave-promotion-after-master-executed, PendingSubmit-across-restart,
external-operator-activity, library-level retry.

**Decision.** Every order carries a deterministic
`client_order_id` of the form
`cycle-{cycle_uuid}-{plan_entry_index}-{symbol}-{side}`. The
broker layer stamps this on the broker side as `orderRef` (IBKR's
free-form per-order tag field). Before submitting any order,
`place_order()` queries IBKR for any order with matching
`orderRef` across open orders, completed orders (48h lookback),
and recent fills (48h lookback). If found, returns the existing
order's state with `idempotent_rediscovery=True`; no new
submission.

**Reasoning.**

This is the same pattern Stripe and other payment APIs use for
idempotency. Three properties make it work:

1. **Deterministic from inputs.** Same cycle state (same
   `cycle_uuid` from `cycle_attempt.json`, same Plan from the
   decision layer's I8 determinism, same plan_entry_index) →
   same `client_order_id`. A restart computes the exact same
   identifier.

2. **Persisted at the broker.** The `orderRef` field survives
   from order placement through filling, cancellation, and
   completion. It appears in `reqAllOpenOrders`,
   `reqCompletedOrders`, and `reqExecutions` responses. The
   broker is the authoritative store.

3. **Cross-validated by `cycle_attempt.json`.** Every successful
   placement is also appended to the local per-cycle file,
   providing a secondary record. Forensic reconstruction can use
   either source.

**Captured decision_clock — invariant I16 — is essential to this
design.** Plan determinism (I8) holds only if all decision-
affecting time queries within a cycle attempt return the same
value. Without I16, a cycle that started at T+0 and restarts at
T+5 could generate a different Plan (some decision branches are
clock-sensitive), produce different plan_entry_index orderings,
and generate different `client_order_id`s — defeating the
idempotency lookup.

**48-hour lookback window.** Matches the operational_pause auto-
resume window (§11.3). A cycle that crashed yesterday and is
restarting today is still within this window; any orders that
landed will be found. Beyond 48h, the cycle is being restarted
via different mechanisms (manual operator intervention) that
don't rely on automatic idempotency.

**Implementation in v1.5 spec.** §15.5 idempotency model
(detailed walk-through of the 6 failure modes); I16 invariant in
§3.14 and §13.5; §15.11 cycle_attempt.json file format.


### D-BROKER-5: Conservative-failure design for recurring ACH update

**Context.** The §8.2.7 ACHScheduleUpdate plan entry calls the
broker to change the recurring monthly ACH amount. This is the
most operationally-risky broker operation because:

- An incorrect amount could over-withdraw and breach buffer/cash
  targets, or under-withdraw and miss operator income needs.
- An incorrect destination could redirect funds (though IRAPM's
  design prevents this — the destination is set up once via
  portal and never changed by the system, per I11 and §15.10).
- The IBKR API surface for ACH updates is limited and changes
  with TWS releases. `ib_async` does not currently expose a
  verified API path for this.

**Decision.** The IBKRBroker implementation of
`update_recurring_ach()` returns `AchUpdateResult(success=False)`
with a rejection_reason that the operator must update via the
IBKR portal manually. The action layer treats this as a Warning
(not Critical), continues operating at the prior ACH amount, and
emits an alert with the new amount the system would have set.
The runbook §14.4 has the manual portal procedure with
screenshots, written for the survivor as audience.

**Reasoning.**

The cost of getting ACH wrong is high; the cost of failing-closed
is low. By design:

- IBKR's recurring transfer continues at the prior amount (no
  destination change, no amount change without operator action).
- The operator gets clear notice that an update is needed and
  what amount to set.
- The cycle does not halt; subsequent cycles emit the
  ACHScheduleUpdate again until the operator performs the portal
  update, at which point the next cycle's
  `get_recurring_ach()` reads the new amount and the system
  knows the update completed.
- No race condition between "system thinks it updated" and
  "IBKR's recurring transfer actually didn't change".

This conservative-failure design will be replaced with a real
implementation once paper-trading verification establishes the
correct `ib_async` API path for the update. Until then, the
manual-portal escalation protects against the most consequential
silent-failure mode in the system.

**Implementation in v1.5 spec.** §15.10 ACH update conservative-
failure design; §14.4 runbook manual procedure; module-level
docstring in `ibkr_broker.py` records the UNCERTAINTY FLAG and
the deferred-verification commitment.


### D-BROKER-6: Decimal everywhere, float forbidden at the protocol boundary

**Context.** Money and quantity precision matters. Python
`float` has well-known representation artifacts
(`0.1 + 0.2 == 0.30000000000000004`); `decimal.Decimal` preserves
declared precision. ib_async uses `float` for some numeric fields
(quantity, price); IRAPM uses `Decimal` everywhere.

**Decision.** Every numeric field crossing the Broker Protocol
boundary is `Decimal`. Pydantic `parse_decimal` validators reject
`float` at parse time with `TypeError`. The Decimal→float
conversion needed when calling `ib_async` happens inside the
IBKRBroker implementation and uses the `float(str(decimal_value))`
idiom to avoid representation artifacts.

**Reasoning.**

Float-via-Decimal conversion (`Decimal(0.1)`) produces
representation artifacts like `Decimal('0.1000000000000000055...')`.
This is correct behavior — Decimal preserves the binary float's
exact bits — but it propagates artifacts through subsequent
arithmetic. The safer idiom `Decimal(str(0.1))` produces
`Decimal('0.1')`. The same logic applies in reverse: when
converting Decimal→float for `ib_async`, `float(str(Decimal('143.250')))`
produces `143.25` exactly.

The strict "no float at the boundary" rule means any float
slipping into the protocol layer is a bug (caught immediately by
parse_decimal raising TypeError), not a silent precision-loss
event.

**Implementation in v1.5 spec.** §15.2 Broker Protocol mention of
Decimal-strict types; module-level docstring in `broker_types.py`
describes the Decimal-only invariant; `_to_decimal` helper in
`ibkr_broker.py` handles the safe float→Decimal conversion.
