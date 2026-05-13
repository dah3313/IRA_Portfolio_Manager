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

**v1.6 extension.** D-SPEC-6 (CB2 entry-path consolidation) extends
this algorithm with a CB-state-dependent mode branch: the
target-weight-proportional algorithm is now the default mode used in
CB_INACTIVE, CB1, and CB2-resource-only; a `defensive` mode (SGOV-first
then FI-only) applies when CB2 is active with the signal condition
currently holding. D11 is scoped to the proportional mode in v1.6.

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

**SUPERSEDED by D-SPEC-6 (v1.6).** The "demote guards to alerts"
direction taken in v1.3 and the "stateless threshold checks preserve
the gate behaviors" clarification added in v1.4 are both superseded
by D-SPEC-6's CB2 entry-path consolidation. The Portfolio-low and
FI-low conditions are now CB2 entry paths with full state-machine
treatment (2-week confirmation, recovery hysteresis, persisted active-
conditions set). The original D-RY-12 entry below is preserved for
historical reference; D-SPEC-6 documents the current design.

**Decision (original, superseded).** Remove the FI-depleted and
Portfolio-depleted guard state machines entirely. Replace with two
simple Warning-severity alerts: `fi_low_alert` (FI bucket < 6 months
of withdrawals), `portfolio_low_alert` (total core < max($50K, 18
months)). Alerts re-fire weekly while condition persists and stop
firing automatically when condition clears (no separate deactivation
alert). Parameters renamed: `fi_depleted_threshold_months` →
`fi_low_alert_threshold_months`, `portfolio_depleted_floor_*` →
`portfolio_low_alert_floor_*`. The two `_clear_hysteresis_rate`
parameters are removed. The `guards` field is removed from
state_schema.json and state_model.py.

**Rationale (original, superseded).** The guards' two roles (cascade
routing + alerting) were partially redundant with existing machinery.
The §11.3 operational pause framework catches order rejection when
residual floors prevent sales (the actual depletion edge). The
`position_residual_minimum_dollars` floors prevent sales below
per-symbol residual independent of guards. The remaining role —
"alert the operator before residual floors engage" — is achieved by
the new simple alerts. Net effect: same safety properties, fewer
state machines, no hysteresis bookkeeping.

**v1.4 clarification (preserved behaviors) — also superseded.** The
v1.4 consistency sweep clarified that three behaviors from the prior
`portfolio_depleted` guard were preserved as stateless threshold
checks (no persisted state, no hysteresis), evaluated independently
at the relevant decision points, all codified under invariant I10:
(a) SGOV refill suspended when core portfolio is below the threshold;
(b) 5/25 rebalancing suspended when core portfolio is below the
threshold; (c) withdrawal routing diverts to SGOV-first cascade when
core portfolio is below the threshold, regardless of CB state. These
clauses are no longer separate stateless checks — under D-SPEC-6 they
follow from "CB2 is active via the Portfolio-low entry path" plus
the existing CB2-suppresses-refill, CB2-suppresses-rebalance, and
CB2-routes-cascade rules. The behavioral surface is the same; the
mechanism is now a single state machine rather than three independent
threshold checks at three decision points.

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


### D-BROKER-7: Post-placement confirmation window widened from 5s to 15s

**Context.** After `placeOrder()` returns, `IBKRBroker.place_order()`
waits for the order to appear in a recognized state (PendingSubmit /
Submitted / Filled / Inactive-with-whyHeld / etc.) before returning
an `OrderResult` to the caller. If the order does not surface within
the window, the order's true state is unknown and the broker raises
`BrokerInconsistency`. The original implementation used a 5-second
window. An external review (the GemBroker.txt code review,
2026-05-12) raised the concern that 5 seconds is tight against IBKR
API-callback lag during heavy-load periods (market open volatility,
FOMC announcement windows, etc.) — a false-positive
`BrokerInconsistency` would halt the cycle and require operator
review.

**Decision.** Widen `POST_PLACEMENT_CONFIRMATION_WINDOW_SEC` from
5.0 to 15.0 seconds.

**Reasoning.**

The asymmetry of costs drives this. False positives in this check
are uniquely expensive in IRAPM's failure-handling design:

- A `BrokerInconsistency` is the only halt category NOT eligible
  for the 48-hour `pause_auto_resume` (per §15.4, §11.2.14, and
  D-SPEC-5's broader logic about which halts can self-resolve).
  The operator must investigate and explicitly clear the pause.
- All other halt categories (order_rejection, partial_phase_transition,
  disk_full) auto-resume — false positives there cost at most one
  re-attempted cycle.
- Real-world IBKR API callback latency during heavy market
  periods can drift well beyond 5s for the open-orders snapshot
  to surface a freshly-placed order.

Three seconds of additional headroom against API-callback lag is
essentially free: in the success case, `_wait_for_order_recognition`
returns the moment the order surfaces (typically <1s), so the
wider ceiling is only relevant when something is genuinely wrong.
Fifteen seconds remains short enough that a real lost order is
detected within a single cycle's runtime; the cycle does not hang
indefinitely.

**Why not even wider (30s, 60s)?** Diminishing returns. Past ~10-15s
any lag is almost certainly a real connection problem, not transient
API-callback queue depth. Pushing the window into the tens of
seconds would just delay surfacing genuine inconsistencies. 15s
is the operationally-defensible point: generous enough to absorb
IBKR's worst-typical-case callback lag, short enough that a real
broker problem still produces a same-cycle alert.

**Policy framing.** The choice aligns with IRAPM's general posture
(per the broker-layer review session, 2026-05-12) of leaning hard
toward fail-safe and self-resolving. Where a halt costs more to
clear than to avoid in the first place, prefer the generous window.

**Implementation in v1.5 spec / code.** Constant
`POST_PLACEMENT_CONFIRMATION_WINDOW_SEC = 15.0` in
`ibkr_broker.py` module-level constants with expanded docstring
explaining the asymmetric-cost rationale. §11.2.14 reference
updated from "(default 5s)" to "(default 15s)". §15.5 post-placement
confirmation paragraph rewritten to reference the constant by name
and include the asymmetric-cost framing. Protocol-level docstrings
in `broker_protocol.py` (BrokerInconsistency class, exception-model
comment block, `place_order` POST-PLACEMENT CONFIRMATION section,
`place_order` Raises section) updated from "5 sec" to "15 sec".


### D-BROKER-8: SyntheticBroker T+1 settlement aging model

**Context.** The SyntheticBroker is used by IPMS (the simulator)
and by unit tests. Its `evolve_pending_state()` method promotes
unsettled SELL proceeds to settled cash between cycles. The
original implementation promoted all unsettled cash immediately
on every `evolve_pending_state()` call, regardless of how much
calendar time had elapsed since the SELL fill. In the IPMS
weekly-cycle simulation this is fine (a week elapses between
evolves, more than T+1), but it diverged from IBKR's real T+1
settlement behavior in ways that could mask a class of bugs:
specifically, any cycle logic that reads unsettled vs settled cash
balances mid-week would see different values in simulation vs
production.

The external review (GemBroker.txt) flagged this as a sim-vs-real
gap. Claude's deeper review identified the same issue plus a
related `get_recent_activity()` `since`-parameter divergence (see
D-BROKER-9 below).

**Decision.** Implement a calendar-day-based settlement-aging
model in the SyntheticBroker:

- A single `_cash_unsettled_since: Optional[datetime]` instance
  field tracks when the current unsettled-cash block began
  accumulating. The first SELL of a block sets the timestamp;
  subsequent SELLs do NOT reset it (earliest-wins semantics).
- `evolve_pending_state()` promotes unsettled→settled only when
  `≥ SETTLEMENT_CALENDAR_DAYS` (=1, a new module constant) of
  calendar time has elapsed since `_cash_unsettled_since`.
- On promotion, the timestamp clears so the next SELL starts a
  fresh block.
- Backward-compatibility fallback: if `_cash_unsettled_since` is
  None (direct test mutation of `unsettled_cash` without going
  through `_apply_fills_to_account`), promote immediately. This
  preserves existing test behavior while supporting the new
  fill-driven path.
- `snapshot()` exposes `cash_unsettled_since` for forensic
  inspection.

**Why Option B (single timestamp) over Option A (per-fill
tracking).** Two competing models:

- **Option A: per-fill timestamps.** Each fill carries its own
  `settled_at` date; `evolve_pending_state()` promotes individual
  fills whose dates have elapsed.
- **Option B: single block timestamp.** All unsettled cash is one
  block with one timestamp; promote-all-or-none.

Option B was chosen for simplicity and because it matches the
behavior the simulator actually needs: between weekly cycles, all
fills from the prior cycle settle together, so per-fill
granularity earns nothing in practice. The earliest-wins semantics
for the timestamp means SELLs added to a block don't extend the
block's settlement date — which matches "each SELL settles T+1
from its own fill date" to within the resolution that matters
(>=1 calendar day).

**Why calendar days, not business days.** IBKR's actual T+1 is
business days, but the simulator and test fixtures don't have a
holiday calendar. Calendar days are a conservative approximation
(real settlement is slower over weekends, so calendar-day
promotion is in the right direction). The simulator does not
place SELL+ACH sequences inside a single weekend, so the
approximation has no operational consequence.

**Why not zero (immediate promotion).** Because it masks the
bug class described in the Context section. The settlement-aging
model is correctness insurance against future cycle logic that
may read settled-vs-unsettled cash distinctions; matching the
production broker's behavior closes that gap proactively.

**Implementation in v1.5 spec / code.** Module constant
`SETTLEMENT_CALENDAR_DAYS = 1` and instance field
`_cash_unsettled_since` in `synthetic_broker.py`.
`_apply_fills_to_account()` records timestamp on first SELL of
block; `evolve_pending_state()` gates promotion on elapsed-time
threshold; backward-compat fallback for direct mutations.
`SETTLEMENT_CALENDAR_DAYS` added to `__all__`. Constant documented
with full rationale (calendar-vs-business-days choice,
why-not-zero motivation). No spec change required: the production
broker contract in §15.2 does not specify settlement timing
behavior; this is an IPMS-side fidelity improvement.


### D-BROKER-9: SyntheticBroker get_recent_activity() honors `since` verbatim

**Context.** Both broker implementations expose
`get_recent_activity(since: Optional[datetime]) -> RecentActivity`.
The IBKRBroker honors the caller's `since` value subject only to
IBKR's own retention windows (48h for completed orders per the
§15.5 idempotency lookback). The SyntheticBroker, however,
silently capped the caller's `since` at
`ACTIVITY_LOOKBACK_HOURS = 48` hours: a caller passing
`since=72h_ago` would silently receive only 48h of activity.

This was a contract divergence: the Protocol contract in §15.2
does not document any silent cap, and a test or simulator query
looking further back than 48h would get different results from
the two broker implementations.

**Decision.** Remove the silent 48h cap. `get_recent_activity()`
in the SyntheticBroker honors the caller's `since` value verbatim.
The `ACTIVITY_LOOKBACK_HOURS` constant is retained as documentary
only — its docstring clarifies that it documents IBKR's 48h
completed-orders retention window for reference, not as an
active limit in the synthetic implementation.

**Reasoning.**

The broker layer's value comes from contract uniformity across
implementations. Silent divergences are precisely the bug class
that the Protocol abstraction exists to prevent. If the test
suite or simulator looks back further than 48h, getting different
results from the two implementations defeats the substitutability
guarantee.

In production, the 48h horizon is enforced by IBKR's own
retention — IBKRBroker's `get_recent_activity()` cannot return
records older than that, so callers organically respect the
horizon. The SyntheticBroker holds all activity in memory and
thus *can* return arbitrarily old records; honoring the caller's
`since` simply means "return everything you have since that
timestamp". If a caller asks for too much, they get too much
— which is what they asked for.

**Why the constant was kept.** The 48h value is operationally
meaningful (it matches the idempotency lookback in §15.5 and the
operational_pause auto-resume window in §11.3). Retaining it as
a named constant with a docstring preserves the design-intent
documentation even though the synthetic implementation no longer
enforces it.

**Implementation in v1.5 spec / code.**
`SyntheticBroker.get_recent_activity()` in `synthetic_broker.py`
honors `since` verbatim. `ACTIVITY_LOOKBACK_HOURS` docstring
corrected to clarify its documentary-only role. No spec change
required: the Protocol contract is unaltered; the change brings
the synthetic implementation into compliance with the contract
as already specified.


### D-BROKER-10: IBKRBroker.connect() is idempotent and self-healing

**Context.** `IBKRBroker.connect()` maintains an internal
`_connected: bool` flag set on successful connection and cleared
in `disconnect()`. The original implementation treated this flag
as authoritative: if `_connected == True`, `connect()` returned
immediately as a no-op. This created a stale-flag failure mode:
between cycles, the underlying transport could drop (TWS
restarted by IBKR-pushed update, network blip, kill -9 of
Gateway, etc.) without the broker instance being notified. The
internal flag remained True; `is_ready()` would correctly
report "not ready" (because it queries the library), but a
cycle launcher calling `connect()` to recover would silently
succeed without actually reconnecting, leaving the broker in
a permanently-broken state until the process restarted.

**Decision.** When `_connected == True`, `connect()` cross-checks
the library's view (`ib_async.IB.isConnected()`) before treating
the call as a no-op. If the library disagrees, the broker:

1. Logs the stale-flag detection at INFO level (not Warning —
   this is expected when TWS has restarted between cycles).
2. Resets `_connected = False`.
3. Falls through to the normal reconnect path (lazy ib_async
   import, IB() construction, ib.connect(), account verification,
   etc.).

If `isConnected()` itself raises (extremely unusual but
possible), the exception is logged at Warning level and treated
as "not connected" — falling through to a fresh reconnect. The
worst case is one unneeded reconnect; the alternative (
propagating the exception) would leave the broker in a worse
state.

**Reasoning.**

This aligns `connect()`'s behavior with `is_ready()`'s view of
reality. The two methods queried the same underlying library
but reached different conclusions about connection state, which
is a recipe for cycle-level confusion ("why did is_ready() fail
immediately after connect() succeeded?"). With the self-healing
behavior, the two methods agree: if the library says
not-connected, both methods treat it as not-connected.

The broader policy framing (from the broker-review session,
2026-05-12): IRAPM should lean hard toward fail-safe and
self-resolving. A `connect()` that silently no-ops on a stale
flag is the opposite — it pretends to succeed while leaving
the operator in a broken state that requires manual
intervention. The self-healing version makes `connect()` a
reliable repair primitive: an operator (or the cycle launcher)
calling it gets either a working connection or a clear
`BrokerUnreachable` exception.

**Why not also do this in is_ready() or other methods.** Those
methods are read-only state queries — they should report
reality, not attempt repair. The repair semantics belong in
the one method whose purpose is to establish a connection.
Keeping the repair pathway concentrated in `connect()` matches
the Protocol's documented intent (per §15.3: "each cycle
opens a fresh broker connection at the start").

**Why not do this in SyntheticBroker too.** The SyntheticBroker
has no underlying transport to drop. Its `_connected` flag IS
the connection state. The change does not apply.

**Implementation in v1.5 spec / code.** `IBKRBroker.connect()`
in `ibkr_broker.py` performs the cross-check before declaring
no-op. Docstring updated with "IDEMPOTENCY & SELF-HEALING"
section describing the behavior. Protocol-level docstring in
`broker_protocol.py` updated to reflect the self-healing
semantics (was: "Idempotent: calling connect() when already
connected is a no-op"; now: "Idempotent and self-healing:
calling connect() when already connected is a no-op IF the
underlying transport is still alive"). §15.3 of the spec adds
an "Idempotent and self-healing connect" paragraph documenting
the behavior and the fail-safe policy framing.


---

## v1.6 design change: CB2 entry-path consolidation (2026-05)

### D-SPEC-6: CB2 entry-path consolidation (supersedes D-RY-12)

**Context.** Two specification problems were discovered during a
review of the "hysteresis" tunable parameters:

1. **Internal contradiction.** §3.10 described FI-low and
   Portfolio-low as guards with cascade routing and hysteresis;
   §6.4 described them as "just alerts" with no state and no
   cascade-routing side effects. §7.3.2 and I10 sided with the
   guard-with-cascade-routing description. The v1.4 "stateless
   threshold checks" clarification (D-RY-12 amendment) was meant
   to reconcile this but left the spec body with three different
   accounts of what these conditions actually did.

2. **Mismatch with operator intent.** The operator's stated
   bootstrap design was to manually pre-fund the SGOV buffer to
   $72K (24 months) and place the remaining ~$28K across the four
   core positions, then let the Portfolio-low condition trigger
   immediately on Day 1 to route withdrawals through cascade
   while annual ~$130K Roth conversions built the core. Under the
   §6.4 "just alerts" reading this would not happen — the
   withdrawals would chew through the small FI bucket until
   residual floors prevented sales, triggering operational_pause.
   Under the §3.10 / §7.3.2 reading it would work, but only
   because three independent stateless threshold checks at three
   decision points happened to agree.

**Decision.** Collapse the FI-low and Portfolio-low conditions
into CB2 entry paths with full state-machine treatment. CB2 now
has three independent entry conditions, each with 2-week
confirmation and per-condition recovery hysteresis:

- **Signal:** Growth lookback ≤ `cb2_threshold_rate` (default
  -0.20); recovery requires signal ≥ -0.15 (+5% buffer) for 2
  consecutive cycles.
- **Portfolio-low:** core portfolio < `max(portfolio_low_threshold_dollars,
  portfolio_low_threshold_months × current_monthly_withdrawal)`
  (defaults $50K / 18 months); recovery requires core ≥ trigger
  threshold × 1.10 (+10% buffer) for 2 consecutive cycles.
- **FI-low:** core FI bucket < `fi_low_threshold_months ×
  current_monthly_withdrawal` (default 6 months); recovery
  requires FI ≥ trigger threshold × 1.10 (+10% buffer) for 2
  consecutive cycles.

CB2 exits only when **ALL conditions that have been active during
the current CB2 episode** have cleared with their respective
recovery buffers. The state file persists a `cb2_entry_conditions`
set (subset of `{signal, portfolio_low, fi_low}`) tracking which
conditions have triggered during the episode; cleared on exit.

CB1 remains signal-based only; resource conditions bypass CB1 and
trigger CB2 directly (resource conditions want SGOV cascade, not
FI-only sourcing).

**Reasoning — bootstrap walk-through.** The clean way to describe
the design is to trace the operator's intended bootstrap:

- Day 0: pre-fund SGOV to $72K + $14K Growth + $14K FI + $4K cash.
- Day 1 cycle: core = $28K < $54K threshold → Portfolio-low
  condition holds → confirmation counter increments to 1.
- Day 8 cycle: condition still holds → counter increments to 2
  → CB2 entry fires with `cb2_entry_conditions = {portfolio_low}`.
- Day 8 onward: rebalancing suspended (CB2 rule); withdrawals
  source from SGOV cascade (CB2 rule); refill suspended (CB2
  rule). $3K/month flows out of SGOV.
- Year 2 (Roth conversion lands): ~$130K cash arrives. Large
  cash deployment (§7.7.1) fires. Mode selection: signal is
  nominal (not ≤ -0.20), so deployment uses target-weight
  proportional mode (not defensive). The ~$130K splits 25/25/25/25
  across FBCG/AVUV/PYLD/JPIE. Core jumps from ~$28K to ~$154K.
- Next cycle: Portfolio-low condition no longer holds; counter
  begins clearing. Two cycles later: CB2 exits to CB_INACTIVE.
- 60 days post-exit: refill resumes, drawing $6K/month from core
  back into SGOV until buffer reaches $72K target. By mid-Year 3
  SGOV is restored.

**Reasoning — why state machine, not stateless checks.** The
v1.4 stateless-threshold-checks formulation worked for the simple
case but had two structural problems:

1. **Three decision points, three threshold checks, no
   coordination.** §7.3.2's cascade-routing check, §7.4's
   refill-suspension check, and §7.5's rebalance-suspension check
   each re-evaluated the same Portfolio-low threshold
   independently. If the threshold was crossed in one direction
   mid-cycle (because the cycle's own actions moved portfolio
   value), the three checks could disagree about whether the
   condition was active. Bug-prone.

2. **No hysteresis means flicker risk.** Portfolio value
   oscillating near the threshold from monthly withdrawals
   followed by dividend inflows would cause the alert to toggle
   on/off, the refill to suspend/resume, and the rebalance to
   suspend/resume — each at slightly different times depending
   on when the evaluations ran. The 10% recovery buffer plus
   2-week confirmation eliminates this.

Making it a CB2 entry path means: one state machine, one
evaluation point per cycle (§6.5 step 5), one set of behavioral
consequences (the CB2 row in §6.6). The bug class disappears.

**Reasoning — deployment-mode branch.** §7.7.1 large cash
deployment now branches by whether CB2 is signal-active or
resource-only:

- **Signal-active CB2 (market drawdown):** defensive mode —
  deploy to SGOV first, then FI only, no Growth purchases. Don't
  buy Growth at depressed valuations.
- **Resource-only CB2 (bootstrap or survivor):** target-weight
  proportional mode (same as CB_INACTIVE / CB1). Market is fine,
  portfolio is just small; deploy proportionally to build it
  toward target allocation.

This branch is essential to the bootstrap design case. Without
it, a Roth conversion landing during a resource-triggered CB2
would route to SGOV+FI only — leaving Growth at $14K against
$108K FI, creating a structural FI-overweight that 5/25 could
not fix (I5 forbids selling FI to fund Growth). The current
branch makes the proportional deployment in resource-only CB2 the
mechanism that *clears* the Portfolio-low condition, since it
restores the core to target weights and (more importantly) raises
core total above the threshold + 10% recovery buffer.

The branch is evaluated each cycle from the current signal
value (not from any persisted entry cause), so the mode can
switch within an episode if conditions evolve: a CB2 entered
via Portfolio-low that subsequently sees the signal drop below
-20% switches to defensive mode automatically; if the signal
later recovers above -20%, mode switches back to proportional.

**Reasoning — rebalancing suspended in all CB2.** Considered
keeping rebalancing active during resource-only CB2 (since the
market is fine and drift correction is on-thesis), but suppressing
rebalancing in *all* CB2 was chosen for two reasons: (1) it keeps
"CB2 suspends rebalancing" as a uniform rule operators can
remember; (2) the bootstrap drift case is bounded (one year max,
until Roth conversion arrives) and the post-deployment rebalance
fires cleanly when CB2 exits. The operator-error case (manually
pouring $100K of fresh FI into the bucket while CB2 is active)
remains pathological under I5 — but that is an operator-discipline
issue rather than a safety failure, and the same I5 problem exists
under any model.

**Reasoning — eliminated state machines.** Three named state
machines disappear: the FI-low guard, the Portfolio-low guard, and
the implicit guard-composition logic that combined their effects
at decision points. The §6.6 operating-mode tuple shrinks from
6-element to 3-element `(phase, income_state, cb_state)`. The
guards-orthogonal-to-CB concept disappears entirely. Net
simplification per the operator's stated goal of "brutally
simplify IRAPM mechanics without losing function."

**Spec sections rewritten (v1.6 changelog enumerates all 24
sub-items):** §3.10 (CB states), §6.3 (full subsection rewrite
into §6.3.1-§6.3.6), §6.4 (reduced to redirect), §6.5 (cycle
evaluation order step 7 removed), §6.6 (tuple + behavioral
table), §6.7 (state persistence including `cb2_entry_conditions`),
§7.3.2 (withdrawal sourcing), §7.4 / §7.4.1 (refill block +
recovery delay), §7.5 / §7.5.1 (rebalancing CB-state precondition),
§7.7.1 (deployment-mode branch), §7.8 D11 (scoped to proportional
mode), §3.14 I10 (restated for any-CB2-condition), §2.3.1
(parameters: 5 new, "Low-resource alerts" subsection removed),
§2.4 (modules), §2.9 (documented bootstrap regime), §12.6 (alert
catalog: guard alerts removed, cb_transition trigger_reason
documented), §9 (alert snapshot), §10 (Phase 3 latch), §13.3
(integration scenarios), §13.5 (invariant list), §16 (glossary).
§4.6 and §6.9 resolved-questions sections deleted entirely
(content either captured in current-state rules or stale per the
no-historical-residue rule). Bonus: three historical-residue
paragraphs stripped during the sweep (Channels column rationale,
Pre-transition validation T-7 explainer, operational_pause v1.3-era
reference).

**Parameters added (5):**

- `portfolio_low_threshold_dollars: 50000` — replaces
  `portfolio_low_alert_floor_dollars`.
- `portfolio_low_threshold_months: 18` — replaces
  `portfolio_low_alert_floor_months`.
- `portfolio_low_recovery_buffer_rate: 0.10` — new (the 10%
  recovery hysteresis).
- `fi_low_threshold_months: 6` — replaces
  `fi_low_alert_threshold_months`.
- `fi_low_recovery_buffer_rate: 0.10` — new (the 10% recovery
  hysteresis).

The `confirmation_window_weeks: 2` parameter is added to the
Circuit breakers section of §2.3.1 to centralize the confirmation
window value that applies to all CB entry/exit paths.

**Parameters removed (3):** `portfolio_low_alert_floor_dollars`,
`portfolio_low_alert_floor_months`, `fi_low_alert_threshold_months`
(replaced as above).

**Persistent state added (1 field):** `cb2_entry_conditions` —
subset of `{signal, portfolio_low, fi_low}`. Per-condition
pending-confirmation counters are also tracked but live within
the existing CB-machine state structure.

**Alert changes:** `guard_activation` and `guard_deactivation`
removed from §12.6 alert catalog. The existing `cb_transition`
alert body carries a `trigger_reason` field with one of
`{signal, portfolio_low, fi_low, cb1_timer}` for CB2 entries and
a list of cleared conditions for CB2 exits.

**Plan invariant updates.** D11 (previously: LargeCashDeployment
plan entry contains only BUY orders proportional to target
weights) is scoped to the `target_weight_proportional` mode in
v1.6, since the `defensive` mode has a different plan shape
(SGOV-first then FI-only). D12 (no Plan generated during Phase 3
latched-but-pending window) is unchanged.

**§6.3.6 — Phase 3 latched-but-pending interaction.** Resource
conditions depend on `current_monthly_withdrawal`, which is
undefined in the latched-but-pending window (per I15). Resource-
based CB2 evaluation suppresses in this window; signal-based CB2
continues to evaluate normally. Resource-based evaluation begins
on the cycle after the Phase 3 transition cycle initializes
`schedule_state.phase3`. If the survivor inherits a small
portfolio, Portfolio-low triggers on that cycle and CB2 entry
fires after 2-week confirmation. The first month's withdrawal may
execute under CB_INACTIVE sourcing if it falls in the
pre-confirmation window; this is bounded by the cash buffer (one
month) and SGOV buffer (24 months).

**Implementation in v1.6 spec.** All sections listed above, plus
v1.6 changelog entry at the top of the document. Spec file size
grew from ~322KB to ~328KB (net +95 lines, 527 added / 432
removed). Code changes pending: ruleset.yaml parameter renames
(D-RY-12's three parameter names need superseding with the five
new ones above), state_model.py adds `cb2_entry_conditions` field,
plan-generation code adds the deployment-mode branch in
LargeCashDeployment, and `cb_transition` alert body construction
adds the `trigger_reason` field. The §13.3 integration tests
specify four new CB2-path scenarios to validate the implementation.


### D-SPEC-7: Spec hygiene pass — Resolved-Questions purge and embedded changelog extraction (v1.7)

**Context.** Two patterns of historical residue had accumulated in
the v1.6 spec body and were violating the operator's stated rule
that the spec holds only current truth:

1. **"Resolved Questions" subsections.** Eight `### N.M Resolved
   questions` subsections (§7.10, §8.5, §9.7, §10.10, §11.5,
   §12.7, §13.8, §15.13) accumulated as design-discussion residue
   from earlier revisions. A scan during the Simplify Quest
   continuation (Candidate 3 in `SIMPLIFY_QUEST_CANDIDATES.md`)
   classified all 34 entries across the eight sections. Every
   entry was either:
   - a restatement of a normative rule already stated elsewhere
     in the spec body,
   - a pure pointer ("see §X"), or
   - a "See DECISIONS.md D-...-N" reference whose substance was
     already captured.

   None represented substantive design rationale not captured
   elsewhere. The 146 lines were pure historical residue.

2. **Embedded version changelog.** Lines 9–195 of the v1.6 spec
   were six dense version-change blocks (v1.1 → v1.6). The
   spec's own Owner Preamble at line 200 stated: *"there will be
   a single changelog for IRAPM"*. The embedded changelog
   violated this rule; the Owner Preamble was written against
   modules but the spec is itself a module. The version-change
   blocks were also redundant with DECISIONS.md: every v1.5+
   change was captured by an existing D-BROKER-N or D-SPEC-N
   entry.

**Decision.** Two paired actions, landed together as v1.7:

1. **Delete all eight Resolved Questions subsections.** No
   migration to DECISIONS.md needed for any entry; each was
   verified to be a (c)-bucket "trivia/working-out/pointer" entry
   per the v1.7 classification pass. 131 lines removed from the
   spec body.

2. **Extract the embedded changelog to `CHANGELOG.md`.** New
   top-level file in the repo. The v1.1 through v1.6 entries
   were preserved verbatim (minor formatting normalization);
   a v1.7 entry was added describing this cleanup pass.
   ~180 lines removed from the spec header. Spec header
   collapsed to: Version, Status, pointers to `CHANGELOG.md`
   and `DECISIONS.md`, single-source-of-truth claim, Owner
   Preamble (preserved — it's normative coding guidance).

**Net effect.** Spec dropped from 6998 → 6687 lines, a 4.4%
reduction. No design changes; pure consolidation and residue
removal.

**Reasoning.**

The operator's stated principle is *"if it's not useful at
coding time, it goes"*. Applied to the Resolved Questions
sections: an entry like "**Dry-run mode:** mandatory; specified
in §13.7" tells a coder nothing they don't get from reading §13.7
itself (which is titled "Dry-run cycle mode (mandatory)" — the
pointer adds zero information). Applied to the embedded
changelog: "what changed between v1.4 and v1.5" is irrelevant to
writing v1.6 code; the coder needs the *current* rule, not the
edit history that produced it. DECISIONS.md captures the *why*
of significant changes; CHANGELOG.md now captures the *what*.

The Owner Preamble was preserved in-spec because it is normative
coding guidance — actionable rules a coder applies while writing
code — not history. Owner Preamble explicitly itself states the
single-changelog principle, so honoring that principle is also a
v1.7 deliverable.

**Bonus finds during the pass (deferred to Candidate 2).** While
classifying §11.5, two stale `withdrawal_capacity_exhausted =
"permanent halt"` references were surfaced in the spec body that
contradict D-SPEC-5 (which clarified this halt as indefinite with
a single auto-clear path, not strictly permanent):

- Line 2326 in v1.6 (§6.7 state schema description block).
- Line 4972 in v1.6 (§11.1 severity model).

A third contradictory reference at line 5383 (in the §11.5
Resolved Questions entry being deleted) was removed by the v1.7
purge itself. The remaining two will be fixed as part of
Candidate 2 (§11.3 catalog cleanup), which is the next
simplify-quest task.

**Implementation in v1.7 spec.** Spec header replaced (lines
1–212 of v1.6 → lines 1–33 of v1.7). Eight Resolved Questions
subsections deleted (each as a header-through-pre-separator
block, preserving the major-section `---` separator that
followed). New `CHANGELOG.md` file created. No code changes.


### D-SPEC-8: Three-category failure-classification framework (operator-relevance, not timer-based)

**Context.** The pre-v1.8 spec classified `operational_pause` recovery
behavior into two categories — auto-resuming (48h timer) and
"NOT eligible for auto-resume" (waits indefinitely for operator
to clear). The two-category model accumulated a sub-category in
v1.7.1 (Critical indefinite-halt for `withdrawal_capacity_exhausted`),
producing three categories whose distinguishing axis was *timer
behavior*: 48h, indefinite-with-condition-clear, or never-resume.

Reviewing the v1.7.1 spec in context of §1.4's failure-quiet
principle ("the survivor inherits a running system, not a system
awaiting instructions") surfaced a fundamental conflict. Three
§11.2 failure entries — §11.2.11 Layer B (split-brain detected),
§11.2.14 (broker inconsistency), §11.2.15 (external account
activity overlap) — set `operational_pause` with "NOT eligible
for auto-resume" semantics. In the absent-operator scenarios
the system is explicitly designed to survive (multi-week travel,
hospitalization, death), these three pause types would leave the
system permanently halted waiting for an operator who is not
returning.

The conflict is not local to those three entries. The deeper
issue is that the failure model was organized around *retry
behavior* when it should have been organized around *system
status*. A timer-based question ("after how long do we retry?")
hides the operator-relevant question ("is the system still
functioning, or genuinely broken?").

**Decision.** Replace the timer-based failure model with an
operator-relevance framework. Each failure mode is classified
into one of three named categories based on the system's actual
status after detection:

1. **Hard broke.** The system genuinely cannot proceed without
   external action. Either the underlying condition cannot be
   resolved by retrying (a software bug, configuration error,
   or broker-data invariant violation), or retrying could cause
   harm (placing duplicate orders, acting against compromised
   state). The system halts; auto-resume is absent because no
   automatic recovery is safe or meaningful.

2. **Self-healed weird.** Something unexpected happened, but
   the system already recovered automatically. The detection
   itself is the receipt that the system noticed; the recovery
   was already performed by other mechanisms (broker-layer
   idempotency, IP-tiebreak, fresh-state re-read on next cycle).
   The alert exists for operator awareness — forensics, trust
   calibration, runbook reference — not for operator action.
   The system continues running normally; no `operational_pause`
   is set.

3. **Normal-ops notice.** Routine state transitions, scheduled
   actions, or successful recoveries that the operator should
   see in their alert stream for cadence reporting. The system
   is in its expected state; the alert is informational. The
   pre-v1.8 spec handled this category implicitly via Notice
   and Info severity alerts; the framework names it explicitly
   so the catalog is complete.

**Rationale.**

The new framework honors §1.4's failure-quiet principle by
making the absent-operator case the design center. With timer-
based classification, the question "what happens if no operator
ever returns?" had different answers in different rows of the
§11.2 catalog. With status-based classification, the answer is
uniform: the system runs forever unless it is genuinely broken,
and "genuinely broken" is a deliberately narrow category that
the system cannot fix itself.

**Hard broke is intentionally narrow.** Only two pause causes
qualify post-v1.8: `internal_consistency_violation` (assertion-
failure semantics; the same software bug will re-fire on retry,
so retrying achieves nothing) and `broker_inconsistency` in the
narrow sub-cases where the broker layer cannot trust its own
state (account-ID mismatch at connect, malformed Trade data
indicating ib_async/IBKR version drift). The withdrawal-only
indefinite halt `withdrawal_capacity_exhausted` is structurally
its own field (not a `pause_reason` value); it sits in the
Hard-broke category conceptually but has a precise condition-
based auto-clear that the other Hard-broke cases lack.

**Self-healed weird is the right home for several v1.7.1
"NOT auto-resume" cases.** Layer B split-brain detection
(§11.2.11) fires *after* the IP-tiebreak has resolved the
state-file conflict and *after* the broker layer's pre-place
idempotency lookup has prevented duplicate orders. The pause
was protecting against damage that the system had already
prevented. Similarly, `external_activity_overlap` (§11.2.15)
fires when the system has already declined to act on
suspicious data — the heal is the declination, and the next
cycle's fresh-state read continues operation safely. The
transient sub-cases of `broker_inconsistency` (post-placement
confirmation timeout, pre-place query failure) recover on the
next cycle's broker query without operator involvement.

**Net effect on the failure catalog.** The §11.3.1 pause-reason
catalog post-v1.8 lists 5 values:
- Auto-resuming (Self-healed-weird category, after-the-fact
  recovery via 48h retry): `partial_phase_transition`,
  `order_rejection`, `disk_full`
- Hard-broke (no auto-resume): `internal_consistency_violation`,
  `broker_inconsistency`

The values `state_file_corrupt` and `configuration_validation`
that appeared in the v1.7.1 catalog under "N/A — refuses to
start" are removed; these are §11.2 entries documenting
refuse-to-start startup-time conditions, not values the runtime
ever writes to the state file as `pause_reason`.

Three former pause types are converted to alerts-only with no
pause: §11.2.11 Layer B (`split_brain_detected`), §11.2.15
(`external_activity_overlap`), and two of the four
§11.2.14 sub-cases (`broker_inconsistency` post-placement
timeout and pre-place query failed).

**Implementation in v1.8 spec.** §11.1 severity model gains the
named three-category framework. §11.2 entries each carry the
new classification in their Severity line. §11.3.1 catalog
contains the canonical 5-value `pause_reason` set. State schema
and Pydantic model updated to match. Detailed case-by-case
classification (which §11.2 entry falls in which category, and
why) is in the spec body §11 rather than duplicated here —
this entry captures the framework decision; the spec carries
current truth.
