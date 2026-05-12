# Simplify Quest — Candidate Findings (2026-05-12)

This document is a scan-output handoff to a future session continuing the
"brutally simplify IRAPM mechanics without losing function" work that
began with v1.6 (D-SPEC-6 in DECISIONS.md, the guard→CB2 consolidation).

Each candidate below follows the v1.6 template:
- **Pattern observed** — what the spec currently does
- **Why it's a candidate** — what makes it look like the v1.6 pattern
  (complicated set of instructions reusable via existing mechanics)
- **Reuse target** — which existing mechanism it might fold into
- **Probable cost** — what gets traded away
- **Open questions before committing** — what to decide with the operator
- **Confidence** — Claude's read of whether this is a real win or a mirage

The candidates are ranked highest-confidence first; the operator should
likely drill into them top-down rather than scanning all of them in a
single session. Each one merits a similar treatment to D-SPEC-6: design
discussion, decision document update, then spec/ruleset/state/code
landing.

A working principle from v1.6, restated: the spec body holds only
current truth; if you find something that's "almost" the v1.6 pattern
but with one structural difference, that difference is usually the
thing worth examining before deciding to consolidate.

---

## Candidate 1: Phase 2 swing state is structurally a second CB machine

**Confidence: HIGH.** This is the cleanest candidate — strong
isomorphism with existing CB machinery and one likely real benefit
(eliminating an undefined-persistence field).

**Pattern observed (§7.5.2.a).**

Phase 2 has a `steady` ↔ `deployed` binary state machine triggered by
Synthetic Growth Lookback signal crossings:

- Steady → Deployed: signal ≤ `phase2_opportunistic_trigger_rate`
  (-10%, same value as `cb1_threshold_rate`), confirmed for 2 cycles
  (same `confirmation_window_weeks` as CB machine).
- Deployed → Steady: signal ≥ `phase2_opportunistic_recovery_rate`
  (absolute +2%, NOT a hysteresis buffer above trigger), confirmed for
  2 cycles.

Per §7.5.2.a, the swing has its own:
- Binary state, persisted across cycles
- Pending-confirmation counters in both directions
- Suspend-during-Phase-3-grace rule
- Suspend-during-signal-UNAVAILABLE rule (implicit; signal-gated)
- "Cannot skip — deployed must precede recovery, and vice versa" rule
- Dedicated `phase2_opportunistic_deploy` / `phase2_opportunistic_recover`
  alerts

Meanwhile §6.6 says "CB held CB_INACTIVE in Phase 2" — i.e. the CB
machine is alive but quiescent during Phase 2, parallel to the active
swing machine using the same signal.

**Why it's a candidate.**

Every structural element of the Phase 2 swing has a 1:1 counterpart
in the CB machine. The swing is shaped like the CB machine with:
- Different threshold values
- A wider recovery hysteresis (12-point band vs CB1's 2-point)
- A different behavioral consequence (allocation swing vs withdrawal
  cascade routing)

Currently Phase 2 has *two* parallel state machines on the same
signal, both with confirmation counters, both with suspend rules. The
v1.4 reading of "FI-low/Portfolio-low are guards orthogonal to CB"
was structurally identical: parallel state machines on the same
domain. We folded that in v1.6 to good effect.

Additionally: §7.5.2.a says the swing state is "persisted across
cycles" but **the spec body's §6.7 state persistence section does not
include a field for it**, and the state_schema.json does not have one
either. This is a phantom field — either §6.7 is incomplete, or the
swing's persistence mechanism is undefined. Folding the swing into
the CB machine eliminates this ambiguity (the swing inherits the
already-specified `cb_machine` field structure).

**Reuse target.**

Treat Phase 2's swing as the Phase 2 manifestation of the CB machine:

- Phase 2 CB_INACTIVE = swing steady state.
- Phase 2 CB1 = swing deployed state. (CB1 is "active but not yet at
  cascade depth" — that maps naturally to "deployed dry powder.")
- Phase 2 doesn't reach CB2 (resource conditions don't apply: no
  scheduled withdrawals to compute Portfolio-low / FI-low against,
  and there's no cascade routing to perform).

The §6.6 operating-mode behavior table grows a Phase 2 column: in
Phase 2, CB1 means "deployed state" — Growth at ~100%, GBIL at
residual. The existing CB1↔CB_INACTIVE transition logic uses the
configured `cb1_threshold_rate` (-10%, same value as the current
`phase2_opportunistic_trigger_rate`) and `cb1_recovery_buffer_rate`
(currently 0.02, i.e. -8% recovery from -10% trigger) — but Phase 2
needs the wider +2% absolute recovery, not -8%.

**Resolution of the recovery-threshold mismatch.** Either:
- (a) Replace `cb1_recovery_buffer_rate` with a per-phase value
  (Phase 1/3: 0.02 = -8% absolute, Phase 2: 0.12 = +2% absolute). The
  recovery threshold computation becomes
  `cb1_threshold_rate + cb1_recovery_buffer_rate[phase]`. Tunable per
  phase via two new params; the old single param goes away.
- (b) Keep CB1 recovery semantics identical and introduce a Phase 2
  swing-recovery-only override. (Hybrid; weaker simplification win.)
- (c) Recognize the recovery thresholds are genuinely different
  mechanisms (drawdown-protection hysteresis vs profit-capture swing
  band) and accept the gap. In which case maybe these *shouldn't* be
  unified.

Claude's read: (a) is the cleanest. The widening from 0.02 to a Phase
2 value of 0.12 documents the design choice (Phase 2 trades for
profit; Phase 1/3 prevents thrashing) rather than hiding it in a
parallel state machine.

**Probable cost.**

- §6.6 behavior table needs a Phase 2 column that documents what CB1
  means in Phase 2 (deployed state, 100% Growth allocation). Currently
  the table assumes "CB1 = withdrawals from FI only" semantics which
  doesn't fit Phase 2.
- The `phase2_opportunistic_deploy` and `phase2_opportunistic_recover`
  alerts could collapse into the existing `cb_transition` alert with
  trigger_reason `signal_phase2_swing` (or kept separate for clearer
  operator messaging — the user's call).
- §7.5.2.b "skip when swing state is deployed" becomes "skip when CB
  state is CB1" (or however the unified design works).
- The "wider recovery band is for profit-capture not hysteresis"
  rationale needs to live somewhere; probably as a note next to the
  per-phase `cb1_recovery_buffer_rate` parameter in ruleset.yaml.

**Open questions before committing.**

1. Does the operator agree that the swing is structurally a CB? Or is
   the conceptual separation (CB = withdrawal-survival mechanism;
   swing = profit-capture mechanism) more valuable than the
   structural unification?
2. If yes: should `cb1_recovery_buffer_rate` become per-phase, or
   keep a separate param for the Phase 2 case?
3. Should `phase2_opportunistic_deploy`/`recover` alerts collapse into
   `cb_transition` with a new trigger_reason, or remain distinct (for
   clearer operator messaging)?

**Risk if the consolidation is wrong.**

Low. Even if it turns out the swing should remain conceptually
separate, the work of locating the Phase 2 swing state in the
state schema (currently undefined) and resolving §6.7's gap is worth
doing on its own.

---

## Candidate 2: §11.3 pause-reason catalog conflates two distinct mechanisms

**Confidence: HIGH.** Same kind of internal contradiction we fixed in
v1.6 for guards — the spec describes the same thing in two ways and
the two ways disagree.

**Pattern observed.**

§11.3 has a pause-reason catalog table listing six pause_reason
values, one of which is `withdrawal_capacity_exhausted` marked
"**NO — permanent halt**" for auto-resume. The §11.3 closing
paragraph says: "the `withdrawal_capacity_exhausted` halt is
intentionally permanent because there is no recovery IRAPM can perform
on its own."

But:

- D-SPEC-5 in DECISIONS.md (committed prior session) explicitly says
  `withdrawal_capacity_exhausted` is an **indefinite halt with a
  single auto-clear path** — when cascade returns to feasibility at
  a withdrawal cycle, the flag auto-clears. NOT strictly permanent.
- §11.2.7 says exhaustion is tracked by **its own separate flag
  `withdrawal_capacity_exhausted: true` in state**, NOT by setting
  operational_pause. The two mechanisms are independent. The §11.3
  catalog is listing it as if it were a pause_reason value, but it
  isn't a pause_reason value — it's a sibling flag.
- §11.3 says "the operator never edits the state file" and "no manual
  state-file edit is required at any point" but the §11.2.7
  pre-D-SPEC-5 text mentioned "manual state-file edit after
  consultation with the runbook."

So we have at least three different accounts of the same thing —
exactly the symptom we fixed for guards in v1.6.

**Why it's a candidate.**

This is structurally the v1.6 pattern: a behavior described
inconsistently across multiple sections of the spec, with a
"committed clarification" in DECISIONS.md (D-SPEC-5) that hasn't
been fully back-ported into the spec body.

**Reuse target.**

Don't add new mechanism. Just fix the spec body to reflect the
already-decided D-SPEC-5 design:

- Remove `withdrawal_capacity_exhausted` from the §11.3 pause-reason
  catalog (it isn't a pause_reason; it's a sibling flag).
- Restate §11.3 to describe two parallel halt mechanisms:
  (a) `operational_pause` (auto-resuming, applies to the listed
  pause_reasons), and
  (b) `withdrawal_capacity_exhausted` (indefinite-halt flag with
  single auto-clear path, applies only to cascade-exhausted
  withdrawals; all other system functions continue).
- Strip "intentionally permanent" language since D-SPEC-5
  superseded it; the auto-clear path makes it not strictly permanent.
- Strip the "operator never edits the state file" claim if it
  conflicts with anything; D-SPEC-5 says explicitly no operator edit
  path, which is correct, but the language should be consistent.

**Probable cost.**

Almost none. This is editing-pass work, not design work. The design
is already made (D-SPEC-5); the spec just needs to reflect it
cleanly.

**Open questions.**

None at the design level. At the writing level: should §11.3 grow
a small subsection on the cascade-exhaustion halt (mirroring the
pause-framework subsection structure), or should §11.2.7 own it
entirely and §11.3 just cross-reference?

**Risk if wrong.**

None — this is fixing an internal contradiction, not creating a new
mechanism.

---

## Candidate 3: Eight "Resolved questions" sections are pure historical residue

**Confidence: HIGH.** Every one of these sections is the same kind of
historical-residue accumulation we deleted from §4.6 and §6.9 in v1.6
under the user's no-historical-references rule.

**Pattern observed.**

Eight `### N.M Resolved questions` subsections exist in the spec body:

- §7.10 Resolved questions (Decision Logic)
- §8.5 Resolved questions (Action Layer)
- §9.7 Resolved questions (External Interfaces)
- §10.10 Resolved questions (Hardware Tokens)
- §11.5 Resolved questions (Failure Modes & Recovery)
- §12.7 Resolved questions (Observability)
- §13.8 Resolved questions (Testing Strategy)
- §15.13 Resolved questions (Broker Layer)

These are old design-discussion residue. Each one is a list of "we
debated X, here's the decision and rationale" — exactly what the
DECISIONS.md file exists for.

**Why it's a candidate.**

The user's stated rule (v1.6 design session): "spec body holds only
current truth; historical/superseded references confuse coding and
cause bug-chasing days later." We deleted §4.6 and §6.9 for this
exact reason. These eight are larger versions of the same thing.

**Reuse target.**

The DECISIONS.md file. Each resolved-question entry either:
- (a) Has already been captured in DECISIONS.md (in which case the
  spec body section can be deleted outright)
- (b) Has not been captured in DECISIONS.md (in which case migrate it
  before deleting from the spec)

For each section, the work is: read it, check if DECISIONS.md
already has the substance, migrate if needed, delete from spec body.

**Probable cost.**

Spec body shrinks meaningfully — likely 200-400 lines removed.
DECISIONS.md grows by a similar amount if migration is needed.

The cost of being wrong is small: a future reader who wonders "why
did we decide X" goes to DECISIONS.md instead of the spec body. Which
is where they should be looking anyway.

**Open questions.**

- For each section, do we migrate-then-delete, or just delete (trusting
  that the substance is already in DECISIONS.md from earlier batch
  decisions)?
- One operator-policy question: does the operator want DECISIONS.md to
  swallow all design history, or just the deliberately-recorded "we
  decided to do X non-obvious thing" decisions?

**Risk if wrong.**

Low. The risk is losing context for a past design decision, which is
mitigated by checking DECISIONS.md before deleting any section.

---

## Candidate 4: §7.3.2 internal-consistency-violation branches duplicate

**Confidence: MEDIUM-HIGH.** Real duplication; not sure the
consolidation is worth the slight loss of locality.

**Pattern observed.**

§7.3.2 has two near-identical "this branch is unreachable in correct
operation" paragraphs:

- CB_INACTIVE sourcing step 7: "If overall sourcing is insufficient
  even after applying steps 1–6: this state is unreachable in correct
  operation — the FI-low and Portfolio-low CB2 entry paths would have
  triggered CB2 and routed withdrawals through cascade instead.
  Reaching this branch indicates a bug in CB evaluation or threshold
  configuration. Abort cycle, set `operational_pause` with
  `pause_reason: 'internal_consistency_violation'`, alert Critical.
  This branch is NOT eligible for 48h auto-resume."

- CB1 sourcing step 4: "If FI bucket cannot meet demand even at
  residuals: this state is unreachable in correct operation — the
  FI-low CB2 entry path would have triggered CB2 and routed
  withdrawals through cascade. Reaching this branch indicates a bug
  in CB evaluation or threshold configuration. Abort cycle, set
  `operational_pause` with `pause_reason:
  'internal_consistency_violation'`, alert Critical. This branch is
  NOT eligible for 48h auto-resume."

Same mechanic, same pause_reason, same "not eligible for 48h
auto-resume" — but the §11.3 pause-reason catalog table does NOT
list `internal_consistency_violation` as a pause_reason. So:

- (a) These two branches reference a pause_reason that isn't in the
  catalog (spec contradiction)
- (b) They have identical structure, suggesting a single canonical
  rule that should live somewhere central rather than being inlined
  twice in §7.3.2

**Why it's a candidate.**

Same pattern as v1.6: a behavior described inline at decision points
when it should be a named mechanism with one canonical home.

**Reuse target.**

Either:
- (a) Add `internal_consistency_violation` to the §11.3 catalog as a
  pause_reason explicitly marked NOT eligible for auto-resume,
  alongside the existing `withdrawal_capacity_exhausted` exception.
  Then §7.3.2 just says "set internal_consistency_violation pause"
  in both places, no repeated explanation.
- (b) Generalize: §11.3 grows a "consistency-check violation" rule
  that covers any decision-layer "this should have been unreachable"
  branch with the same pause behavior.

**Probable cost.**

Small. Two paragraphs collapse to two one-line references.

**Open questions.**

Is the §11.3 catalog the right home, or does this belong in §3.14
invariants (since it's an invariant-violation response)?

**Risk if wrong.**

Low.

---

## Candidate 5: §7.5.1 FI-overweight suppression mini state-machine

**Confidence: MEDIUM.** Real complexity reduction available, but
the existing mechanic is small enough that the win may not be worth
the work.

**Pattern observed.**

§7.5.1 implements a "rebalance suppression duration" tracker for the
FI-sacrosanct (I5) constraint:

- When a rebalance plan would require selling FI to fund Growth
  purchase, suppress the rebalance.
- Track consecutive weeks suppressed.
- If suppression persists ≥ `fi_overweight_suppression_alert_weeks`
  (default 4), elevate to a `fi_overweight_persistent_suppression`
  Warning alert.

This is a dedicated counter + threshold + alert mechanism for one
specific alerting case.

**Why it's a candidate.**

Single-purpose mini state-machine. The alert re-fires-while-condition
pattern already exists for other alerts (the now-removed
`fi_low_alert` and `portfolio_low_alert` used it pre-v1.6; cascade
exhaustion uses it). The threshold-weeks-before-elevation is the
custom bit.

**Reuse target.**

If the spec adopts a general "alert escalation after N weeks while
condition holds" pattern, this becomes one instance of it. But that
pattern doesn't currently exist as a named mechanism — it's just
inline at each alert that uses it. So consolidating here may not be
worth introducing a new generic mechanic.

Alternative: just keep the inline counter but be explicit it's
state that needs persisting (does it? where? the spec doesn't say).
The audit work — checking where the counter actually lives —
might surface a phantom-state-field issue similar to Phase 2 swing.

**Probable cost.**

Low — small section.

**Open questions.**

Where is the suppression-weeks counter currently persisted? The
spec body doesn't say. If it's not in state_schema.json, this is
another phantom-state-field issue worth fixing on its own.

**Risk if wrong.**

Low.

---

## Candidate 6: §11.2 catalog repeats the same "auto-resume vs operator-clear" tags

**Confidence: MEDIUM.** Real repetition; consolidation requires a
small structural change.

**Pattern observed.**

§11.2 has 15 failure subsections (§11.2.1 through §11.2.15). Each
one has a paragraph specifying:
- Whether `operational_pause` is set
- Whether it's auto-resume-eligible
- What `pause_reason` (if any) is used
- The recovery mechanism

§11.3 has a `pause_reason` catalog table that should be the canonical
source — but several §11.2 entries describe `pause_reason` values that
don't appear in the catalog (e.g. `split_brain_detected` per §11.2.11
Layer B; `broker_inconsistency` per §11.2.14;
`external_activity_overlap` per §11.2.15), and the catalog lists
`pause_reason` values that don't have prominent §11.2 entries (e.g.
`state_file_corrupt`, `configuration_validation` — these are present
but labeled as "N/A — refuses to start").

So the §11.3 catalog is incomplete relative to §11.2 entries, and
the §11.2 entries each independently describe their pause behavior.

**Why it's a candidate.**

Same pattern as v1.6: scattered, partially-redundant descriptions
of the same mechanism. A canonical catalog with each failure mode
pointing to it would be cleaner.

**Reuse target.**

§11.3 owns the pause-reason catalog as the canonical source.
§11.2 entries reduce to: symptom, severity, **catalog entry
reference**, recovery summary. No re-description of pause/resume
behavior in each entry.

The catalog needs to be expanded to cover all pause_reason values
actually used in §11.2, including the ones currently missing
(`split_brain_detected`, `broker_inconsistency`,
`external_activity_overlap`, `internal_consistency_violation`).

**Probable cost.**

§11.2 entries shrink by roughly half. §11.3 catalog grows to ~10-12
rows. Net spec reduction probably 50-100 lines.

**Open questions.**

Is the catalog the right structural home for the auto-resume-eligible
flag, or should it live in a dedicated table separate from the
pause-reason enumeration? Currently they're conflated in one column.

**Risk if wrong.**

Low. The reorganization is reversible and the design isn't changing.

---

## Candidate 7: §7.5.2.b skip-conditions list isn't unified with §7.5.2.a

**Confidence: LOW-MEDIUM.** Smaller win; might be folded into
Candidate 1 if the swing-as-CB consolidation happens.

**Pattern observed.**

§7.5.2.b semi-annual reallocation lists four skip conditions:
1. Swing state is `deployed`
2. Phase 3 grace window active
3. Signal UNAVAILABLE
4. CB state is CB1 or CB2 (described as "belt-and-suspenders" since
   Phase 2 normally holds CB at CB_INACTIVE)

§7.5.2.a swing has its own list of suspend conditions that overlaps
but isn't identical. Two parallel "system isn't safe to trade right
now" predicates with different membership lists.

**Why it's a candidate.**

The skip conditions across §7.5.2.a, §7.5.2.b, and arguably the
withdrawal/refill/rebalance suspensions of CB2 all express the same
underlying concept ("system isn't in a safe-to-trade-this-thing
state"), with subtly different membership. A unified
"can-trade-now" predicate, parameterized by what kind of trade is
proposed, would centralize this.

**Reuse target.**

If Candidate 1 (swing-as-CB) lands, this largely dissolves —
§7.5.2.b's skip conditions reduce to "skip when CB is not at the
correct state for steady-state reallocation," which is the same
condition pattern used elsewhere.

If Candidate 1 doesn't land, this is a smaller standalone fix.

**Probable cost.**

Folded into Candidate 1: free.
Standalone: medium effort for small win.

**Open questions.**

Wait for Candidate 1 decision before tackling this.

**Risk if wrong.**

Low.

---

## Candidate 8: §10.5/§10.6 token-state pattern logic duplicates

**Confidence: LOW.** Specific to token system; less general-purpose
than other candidates. Listing for completeness.

**Pattern observed.**

§10.5 specifies "hold previous state on invalid mismatch" with one
exception: §10.6.2 Phase 3 grace window uses a different rule
(any-re-insertion aborts with one-cycle persistence). The exception
is documented in §10.5 as a forward reference.

The grace window's one-cycle-persistence check is structurally a
2-week-confirmation pattern with `weeks=1 daily cycle = 1 unit`. The
spec body inlines its logic instead of treating it as the CB-style
confirmation pattern at finer granularity.

**Why it's a candidate.**

If there's a generic "confirmation pattern" abstraction (signal
must persist for N cycles before commit), it could cover CB
transitions, the grace-window abort, and the token-mismatch escalation
all at once. Three places using the same underlying pattern with
different cycle types.

**Reuse target.**

A unified "transition confirmation" primitive applied to several
state machines:
- CB confirmations (2 weekly cycles)
- Phase 3 grace abort (1 daily-token cycle)
- Token-mismatch critical escalation (2 daily-token cycles)
- Slave-promotion grace (48 hours / N cycles)

**Probable cost.**

This abstraction sounds prettier than it is — the three contexts use
different cycle types (weekly vs daily-token) and different
event semantics (state change vs alert escalation). Forcing a single
primitive risks producing the same parallel-state-machine problem the
v1.6 cleanup just got rid of.

**Open questions.**

Probably defer or skip. The duplication is real but the abstraction
isn't obviously worth the unification cost.

**Risk if wrong.**

Medium. This is the kind of consolidation that *can* hide complexity
in a primitive that's used differently in different contexts. v1.6
showed that the right move is sometimes the opposite: separate
machines where the spec previously had a "general" abstraction.

---

## Cross-cutting notes for the next session

**Operator preferences carried over from v1.6:**
- Don't run more than 2 pages ahead without checkpointing
- Pause at decision branches; wait for explicit choices
- No historical/superseded references in spec body — spec is current
  truth; DECISIONS.md and changelog handle history
- Stage files in `/mnt/user-data/outputs/` for download; user commits
  via their GitHub repo
- Write directly to `c:/portfolio/IRAPM/` for canonical files like
  DECISIONS.md when requested

**Files in the IRAPM directory and what each owns:**
- `IRAPM_SPECIFICATION.md` — present-tense rules only (current truth)
- `DECISIONS.md` — design-rationale "why we chose X" entries with
  D-prefix IDs (D-SPEC-N, D-RY-N, D-CODE-N, D-BROKER-N, etc.)
- `ruleset.yaml` — operator-edited parameter file
- `ruleset_model.py` — Pydantic validation of ruleset.yaml
- `state_schema.json` — JSON Schema for operating state file
- `state_model.py` — Pydantic mirror of state schema, plus
  new_initial_state factory
- `alert_templates.yaml` — message wording for all alerts
- `broker_protocol.py` / `broker_types.py` / `ibkr_broker.py` /
  `synthetic_broker.py` / `cycle_attempt.py` / `clock.py` —
  broker abstraction layer

**Sources of truth for v1.6 design:**
- DECISIONS.md entry D-SPEC-6 (committed) — the canonical record of
  the guard→CB2 consolidation, including the bootstrap walk-through
- Spec v1.6 changelog (top of IRAPM_SPECIFICATION.md) — enumerates
  the 24 sub-edits
- D-SPEC-3 v1.6 extension note — large-cash-deployment mode branch

**Recommended drilling order for the next session:**

1. Pick from Candidate 1, 2, or 3 — these are HIGH-confidence wins
   with distinct flavors:
   - Candidate 1 (Phase 2 swing as CB) is the biggest structural
     simplification
   - Candidate 2 (withdrawal_capacity_exhausted contradiction) is
     pure cleanup of an already-decided design
   - Candidate 3 (Resolved Questions deletion) is the largest spec
     reduction by line count, and is mechanical work that can be
     done first to clear noise before tackling structural changes
2. Save Candidates 4-6 for a follow-up session
3. Defer or skip Candidates 7-8 unless related work surfaces them

Claude's recommendation: **start with Candidate 3** (Resolved Questions
purge). It's mechanical, low-risk, produces visible spec reduction,
and clears noise that would otherwise distract from the structural
candidates. **Then Candidate 2** (withdrawal_capacity_exhausted
cleanup) as a small warm-up on the patterns that surfaced in v1.6.
**Then Candidate 1** (Phase 2 swing as CB) as the main structural
work.

**Conversation context to bootstrap next session:**

The next Claude session should be told:
- v1.6 (CB2 entry-path consolidation) is committed to GitHub
- This document is the scan output for "Simplify Quest" continuation
- The operator likes the v1.6 template: design discussion → DECISIONS
  entry → spec edits → staged supporting-file updates
- Operator preferences include the "don't run more than 2 pages
  ahead" and "pause at decision branches" rules
- Filesystem MCP tools provide read/write access to
  c:/portfolio/IRAPM/

If this document and DECISIONS.md are both committed, the next
session has full context to continue without re-deriving anything.
