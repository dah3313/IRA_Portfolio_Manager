# IRAPM Changelog

Version history for the IRAPM specification. Each entry is a brief
statement of what changed. Design-decision rationale lives in
`DECISIONS.md`; current normative truth lives in
`IRAPM_SPECIFICATION.md`.

---

## v1.7 — Spec hygiene pass

**Resolved Questions sections removed.** All eight `### N.M Resolved
questions` subsections in the spec body were deleted: §7.10 (Decision
Logic), §8.5 (Action Layer), §9.7 (External Interfaces), §10.10
(Hardware Tokens), §11.5 (Failure Modes & Recovery), §12.7
(Observability), §13.8 (Testing Strategy), §15.13 (Broker Layer).
Every entry across these 34 entries was either a restatement of
normative rules elsewhere in the spec, a self-reference pointer to
the section that owns the rule, or a substantive design choice
already captured in DECISIONS.md. None required migration. Per
D-SPEC-7. 146 lines removed.

**Embedded changelog extracted.** Version-change blocks (v1.1
through v1.6) that previously lived in the spec body (lines 9–195
of the v1.6 file) were extracted to this CHANGELOG.md. The spec
header was reduced to Version, Status, pointers to CHANGELOG.md and
DECISIONS.md, and the Owner Preamble. Per D-SPEC-7. ~165 lines
removed from the spec.

**Spec-body residue cleanup.** Two `withdrawal_capacity_exhausted =
"permanent halt"` references in §6.7 and §11.1 corrected to match
D-SPEC-5 (indefinite halt with single auto-clear path). See
Candidate 2 in the simplify-quest queue for the full §11.3 cleanup
of this contradiction.

---

## v1.6 — CB2 entry-path consolidation

Consolidated FI-low and Portfolio-low independent guard state
machines into CB2 entry conditions.

(1) §3.10 rewritten: CB2 now has three independent entry paths
(signal, Portfolio-low, FI-low), each with 2-week confirmation; the
"independent guard states orthogonal to CB machine" concept is
removed. CB1 remains signal-based only (resource conditions bypass
CB1 and trigger CB2 directly, since they want SGOV cascade rather
than FI-sourcing). (2) §6.3 fully rewritten into six subsections:
entry conditions (§6.3.1), CB1↔CB_INACTIVE transitions (§6.3.2), CB2
exit conditions with per-path 10% resource recovery buffer and
ALL-conditions-clear semantics (§6.3.3), `cb2_entry_conditions`
persisted set tracking conditions active during the episode
(§6.3.4), CB1→CB2 timer mechanics with `signal` added to the entry
set on timer fire (§6.3.5), resource-condition suppression during
the Phase 3 latched-but-pending window (§6.3.6). (3) §6.4 reduced to
a 12-line redirect — guards no longer exist as independent states.
(4) §6.5 cycle evaluation order: step 7 (Guards) removed; remaining
steps renumbered; annual-review-day ordering note restated for CB2
resource evaluation. (5) §6.6 operating-mode tuple simplified from
6-tuple to 3-tuple `(phase, income_state, cb_state)`;
behavioral-effects table compressed with CB2 split into "signal
active" and "resource only" rows reflecting the deployment-mode
branch. (6) §6.7 state persistence: `Guards` field removed;
`cb2_entry_conditions` added; per-condition pending-confirmation
counters specified for each of the four entry/exit paths; CB
transition log's `trigger_reason` field documented as `{signal,
portfolio_low, fi_low, cb1_timer}` for entries and cleared-conditions
list for exits; resource condition *currently-holding* status noted
as recomputed each cycle, only counters and the cb2_entry_conditions
set persist. (7) §6.8 alert trigger list narrowed from "(Phase,
Income, CB, guard)" to "(Phase, Income, CB)". (8) §6.9 and §4.6
resolved-questions sections deleted (content either captured in
current-state rules or stale). (9) §7.3.2 withdrawal sourcing:
"(CB2, or Portfolio-low alert)" collapsed to "(CB2)";
consistency-violation branch messages updated to reference CB2 entry
paths. (10) §7.4 SGOV refill block condition simplified to "CB state
is not CB2"; §7.4.1 recovery delay trigger simplified to "after CB2
exits". (11) §7.5 / §7.5.1 explicit CB-state precondition added:
5/25 rebalancing suppressed during CB1 or CB2 (regardless of CB2
entry path); §7.5.2.b skip conditions cleaned of guard references.
(12) §7.7.1 large cash deployment: two deployment modes (Portfolio-low,
Portfolio-low and Portfolio-low) and the implicit guard-composition
logic) and the guard/CB orthogonality concept; cascade routing is
now exclusively a CB2 behavior; the bootstrap-Roth design case is
documented and works cleanly with target-weight-proportional
deployment in resource-only CB2.

See DECISIONS.md D-SPEC-6 for the full design discussion.

---

## v1.5 — Broker layer formalized

(1) New top-level §15 specifies the Broker Protocol (14 methods, 5
groups), per-cycle connection lifecycle, typed exception model
(BrokerNotReady/BrokerUnreachable/BrokerRejection/BrokerInconsistency),
idempotency model (cycle_uuid + decision_clock + client_order_id
deterministic format + pre-place orderRef lookup with 48h window +
post-placement confirmation), master/slave coordination via
IBKR-as-arbiter (defense layer 3 to the §9.4 state-file mechanism),
ContractRef opaque identifier, box.yaml schema, TWS/Gateway
deployment requirements, ACH conservative-failure design,
cycle_attempt.json file format, the five uncertainty flags requiring
paper-trading verification, and resolved-questions discussion of the
design choices. (2) New invariant I16 (captured decision_clock
within a cycle attempt) ensuring Plan determinism across restarts.
(3) §6.7 updated: `cycle_attempt.json` added as per-box,
not-replicated persistence file; `last_cycle_clientid` field added
to master/slave coordination state for split-brain detection.
(4) §9.1 slimmed to operator-deployment concerns (account model,
authentication, credential storage) with §15 as the pointer for the
broker-interface contract details; old §9.1.4 failure-modes table
retired (superseded by §11.2 broker subsections). (5) §11.2
extended: §11.2.1 updated to reference typed broker exceptions;
§11.2.11 reworked into two-layer coverage (Layer A state-file
tiebreak via §9.4.3, Layer B broker-level via §15.6); §11.2.14 added
(Broker inconsistency, NOT auto-resume); §11.2.15 added (External
account activity overlap, operator-acknowledgment required).
(6) §13.5 invariants list updated to include I15 and I16. (7) §14.4
runbook scope expanded: TWS/Gateway settings checklist, box.yaml
configuration documentation, paper-trading verification of the §15.12
uncertainty flags, operator pause-clearing procedures for the three
NOT-auto-resume pause reasons, broker library maintenance procedure.
(8) Spec body's existing "v1.4" references (§4 deferred Phase 3
transition behavior; §12 alert channel unification) preserved — those
changes were made in v1.4 but the header was not bumped at the time;
the v1.5 header bump catches both increments.

See DECISIONS.md D-BROKER-1 through D-BROKER-10 for the design
discussions.

---

## v1.4 — Cascade-routing consistency sweep (header bump deferred from v1.4 to v1.5)

I10 portfolio_low_alert cascade-routing scope clarification (the
alert routes withdrawals through cascade in addition to the prior
buffer-refill suspension and rebalance suspension behaviors);
withdrawal_capacity_exhausted indefinite halt with single auto-clear
on cash-replenish semantics formalized; ruleset.yaml §11
substantively corrected to match I10 (the prior language asserted
"no cascade routing is triggered by these alerts" which contradicted
I10). DECISIONS.md D-RY-12 amended and D-SPEC-5 added. The clock.py,
state_model.py, ruleset_model.py, and ruleset.yaml files swept for
residual historical-narrative comments during the same pass.

---

## v1.3 — External copyedit review fixes

Eight fixes from external copyedit review.

(1) §4.1.1.6 regime taxonomy rewritten to partition by the binding
clamp bound rather than by AND-conditions, closing a gap where
`sub_floor < I_sustainable < floor_T` fell through all four regimes.
(2) §10.5, §10.6.2 token-state validity model formalized: only
specific valid configurations are operative; invalid intermediate
states hold previous state and alert without aborting the grace
window. (3) §4.3 step 1 T-7 validation reworded to "last weekly
cycle 5+ trading days before transition" (the original T-7 was
unreachable by the weekly schedule). (4) §7.5.2 adds semi-annual
(Jan 15 / Jul 15) steady-state reallocation to 45/45/10 during
Phase 2, addressing GBIL dilution from dividend flow. (5) §6.5.1 and
§9.6 "subsume" vocabulary clarified to mean scope-inclusion, not
runtime cancellation. (6) §4.1.1.7 explicit precision rule for the
per-month payment ceiling: intermediates full-precision, final
payment cent-rounded. (7) §9.4.2 staleness threshold converted from
days to hours throughout the coordination subsystem; grace window
start point explicitly anchored to SLAVE_PROMOTION_PENDING entry.
(8) Glossary, §2.3, §3.8, §4.3 ancillary updates supporting the
above.

---

## v1.2 — Sustainable-withdrawal math import

Sustainable-withdrawal math imported from PHASE_3_DESIGN.md §5.1–§5.2
into IRAPM §4.1.1 (constants, bracket indexing, closed-form
`I_sustainable`, sub-floor protection, `I_0` clamping, regime
classification, per-month payment ceiling, implementation order).
IRAPM is now self-contained for Phase 3 implementation.
PHASE_3_DESIGN.md is **deprecated**; remaining references in the v1.2
spec body to it were historical-context-only (parameter-divergence
notes, prior-validation references). Re-validation requirement at the
new IRAPM parameter set (`INFLATION_PRE = 3.5%`,
brackets-continue-growing) added as §14.7.

---

## v1.1 — PHASE_3_DESIGN.md cross-check imports

Corrected token semantics inversion (Phase 3 tokens normally
inserted, STOP INCOME tokens normally removed); added physical
security model; added stuck-token alert; named failure-quiet and
reversible-where-possible operational principles; named
role-swap-not-failback property; replaced shared-state master/slave
model with hybrid rsync-replicated state + state-write-as-heartbeat;
added ACH manual fallback escalation; added resume-semantics worked
example; cross-referenced ratified validation results.

---

## Lineage note

Prior to v1.0, IRAPM was developed across:

- `SPECIFICATION1.md` through `SPECIFICATION6.md` — incremental drafts.
- `PHASE_3_DESIGN.md` — formerly authoritative for Phase 3 intent and
  math. Concepts imported into v1.1, math imported into v1.2. The
  file is **deprecated**; its validation results are retained as
  historical evidence that the original parameter set (3.0%
  pre-trigger, fixed brackets at trigger) produced 100% survival
  over the 2005–2025 sequence. Those results do **not** validate the
  IRAPM-current parameter set; see §14.7 of the spec for the
  re-validation requirement.
- `IPM v1` — the predecessor project, retired due to accumulated bug
  load and lost trust in the codebase.

v1.0 was the consolidated draft that superseded all of the above.
