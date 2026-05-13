# IRAPM Changelog

Version history for the IRAPM specification. Each entry is a brief
statement of what changed. Design-decision rationale lives in
`DECISIONS.md`; current normative truth lives in
`IRAPM_SPECIFICATION.md`.

---

## v1.9.2 — Alert template alignment with v1.8 / v1.9 spec

Defect fix: `alert_templates.yaml` retained pre-v1.8 / pre-v1.9
content that no longer matched the spec. Templates were added,
removed, and updated to bring the file into alignment with the
§12.6 catalog at v1.9.1, plus one §12.6 catalog gap was closed.

**Templates deleted:**

- `fi_overweight_persistent_suppression` — mechanism removed in
  v1.9; no longer in §12.6 catalog.

**Templates added (5 new):**

- `internal_consistency_violation` — Hard-broke pause per
  D-SPEC-8; new in v1.8.
- `broker_inconsistency` — Hard-broke pause for §11.2.14 sub-
  cases (account-ID mismatch, malformed Trade data); new in v1.8.
- `split_brain_detected` — Critical (self-healed weird) post-hoc
  Layer B detection per D-SPEC-8; new in v1.8. Distinct from the
  existing `split_brain_resolved` (Layer A IP-tiebreak).
- `external_activity_overlap` — Critical (self-healed weird) per
  D-SPEC-8 (formerly a pause; now alerts-only). The cycle's
  declination to act is the heal.
- `broker_inconsistency_transient` — Critical (self-healed weird)
  for §11.2.14 transient sub-cases (post-placement timeout,
  pre-place query failure); new in v1.8.

**Templates updated:**

- `broker_disconnect` — "Critical-transient" wording replaced
  with "Critical (self-healed weird)" per v1.8 §11.1 framework.
- `pause_initiated` — body and SMS rewritten to handle both
  Self-healed-weird (48h auto-resume) and Hard-broke (no
  auto-resume) categories. New placeholder
  `pause_category` separates the two; `recovery_summary`
  provides category-appropriate guidance.
- `pause_re_alert` — body and SMS rewritten with conditional
  status text. New placeholder `time_remaining_or_status`
  replaces the previous hard-coded "{N}h until auto-resume"
  which broke for Hard-broke pauses.
- `pause_consecutive_escalation` — body expanded to cover both
  categories' recurrence semantics.
- `withdrawal_capacity_exhausted` — "PERMANENT HALT" language
  removed per D-SPEC-5; replaced with "INDEFINITE HALT —
  auto-clears when capacity returns" wording. Section reference
  updated from §11.3 to §11.3.2.
- `cascade_growth_source` — cross-reference to "permanent halt
  of withdrawals" corrected to "indefinite halt of withdrawals
  (NOT permanent; auto-clears when capacity returns)".

**Spec catalog gap closed (§12.6 Withdrawal alerts table):**

- `monthly_payment_ceiling_bound` — alert was referenced in
  §4.1.1.2 and §6.5.1 (and had a template in
  `alert_templates.yaml` already) but was missing from the §12.6
  catalog. Now added: Notice severity, both channels.

**No mechanism changes.** All edits are documentation,
classification labels, or template wording. No code path, schema
field, ruleset parameter, or design behavior changes.

**Verify-on-your-side note:** the `pause_initiated` and
`pause_re_alert` templates introduce new placeholders
(`pause_category`, `recovery_summary`,
`time_remaining_or_status`) that the alerter dispatch code must
populate. If the alerter currently only supplies the v1.8.1-era
placeholders, the new placeholders will appear as literal text
in dispatched messages until the dispatch code is updated to
populate them. Worth a quick check in the alerter implementation
when you next touch that code.

Clarity fix: §7.5.2.a (Phase 2 two-state opportunistic swing) and
§7.5.2.b (Phase 2 semi-annual reallocation) had overlapping-but-
inconsistent suspend / skip condition lists. §7.5.2.a previously
only mentioned the Phase 3 grace window explicitly, with Signal
UNAVAILABLE and CB1/CB2 conditions left implicit; §7.5.2.b made
all three explicit but in a different order from §7.5.2.a's
prose and without addressing the question of pending-counter
disposition. The condition sets are now expressed identically
across both subsections, in the same order, with explicit
pending-counter disposition.

**§7.5.2.a:** Single-paragraph "Phase 3 grace window suspension"
expanded to a full **Suspend conditions** bullet list covering
all three conditions:

1. Phase 3 grace window active — pending confirmations held but
   not advanced; discarded at Phase 3 latch.
2. Lookback signal UNAVAILABLE — pending confirmations held but
   not advanced (matches §6.3 CB confirmation-counter semantics
   under signal-UNAVAILABLE; the structural parallel with the CB
   machine is acknowledged without unifying the mechanisms — see
   Candidate 1 decision against full consolidation).
3. CB state is CB1 or CB2 — belt-and-suspenders for Phase 2, but
   explicitly covered for the case resource-based CB2 entry paths
   fire in Phase 2.

**§7.5.2.b:** Skip-condition list reordered to match §7.5.2.a's
order (Phase 3 grace, Signal UNAVAILABLE, CB1/CB2, then the
deployed-state condition specific to §7.5.2.b). Introductory
sentence clarifies the no-pending-counters asymmetry: the
semi-annual reallocation is calendar-driven and has no pending
state to preserve across skips, so a skipped occurrence is simply
not executed and the next scheduled date re-evaluates the same way.

No behavior change. No new abstraction introduced. The candidate
doc's "unified can_trade_now predicate" option was rejected as
premature factoring — two adjacent subsections with shared
conditions are well-served by aligned prose; a named predicate
would be over-engineering at the current scope.

Simplification: the §7.5.1 FI-overweight suppression alert
escalation machinery is removed entirely. The mechanism existed
to fire a Warning alert when the FI-sacrosanct (I5) rebalance
suppression persisted for more than
`fi_overweight_suppression_alert_weeks` (default 4). Per the
candidate doc, this was a single-purpose mini state-machine that
required persisting a consecutive-weeks counter — counter that
was never actually declared in `state_schema.json`,
`state_model.py`, or §6.7 (phantom field).

Rather than fix the phantom field, the mechanism is removed: the
weekly summary alert (§12.3) already exposes asset balances and
weights every cycle, so a persistent FI overweight is visible to
the operator without a dedicated alert. The Warning alert was
informational only (no operational consequence) and rare-condition
by design.

**§7.5.1 final paragraph rewritten.** The FI-sacrosanct
suppression rule itself stays — I5 isn't being weakened. The
suppression event is recorded in the cycle log alongside other
per-cycle decisions; no dedicated alert fires. The §12.3 weekly
summary remains the operator's visibility mechanism for FI
overweight.

**Removed:**

- `fi_overweight_persistent_suppression` alert (§12.6 catalog row).
- `fi_overweight_suppression_alert_weeks` parameter
  (`ruleset.yaml` §12 section deleted; spec §2.3 parameter
  listing and parameter table updated; `ruleset_model.py` field
  removed).
- §7.5.1's elevation paragraph (the one that referenced the
  threshold and named the alert).

**No state-field cleanup needed.** The phantom counter was never
persisted; nothing to remove from `state_schema.json` or
`state_model.py`. Candidate 5 dissolves rather than gets fixed.

**No behavior change at the rebalance layer.** The I5 suppression
itself fires exactly as before; only the alert escalation is
gone. The §12.3 weekly summary continues to expose current
allocation, which is what an operator would consult to see if FI
overweight is persistent.

No D-SPEC entry — this is mechanism removal, not a new design
decision. The candidate doc captured the analysis.

Defect fix: the broker layer modules retained pre-v1.8 commentary
describing `BrokerInconsistency` as a uniformly hard-broke
condition ("NOT eligible for 48h auto-resume; requires operator
review"). Per the v1.8 split, post-placement confirmation timeout
and pre-place query failure are now Self-healed-weird (alert only,
no pause), while account-ID mismatch and malformed Trade data
remain Hard-broke. Six docstring/comment blocks across three
broker files were rewritten to reflect the current behavior.

**Files updated:**

- `broker_protocol.py` — exception-model commentary block (the
  "action layer's response to each" mapping) and the
  `BrokerInconsistency` class docstring rewritten to describe the
  sub-case split. The exception itself is unchanged; only the
  documentation of how the action layer should respond.
- `broker_types.py` — `get_recent_activity()` docstring item 2
  (external-activity detector) rewritten: the cycle aborts and
  alerts but does not set `operational_pause` per §11.2.15.
- `ibkr_broker.py` — `POST_PLACEMENT_CONFIRMATION_WINDOW_SEC` and
  `POST_PLACEMENT_ACTIVITY_LOOKBACK_HOURS` comments rewritten;
  `place_order()` docstring's 48h-lookback rationale updated.
  The "matches the operational_pause auto-resume window"
  framing is replaced with the restart-recoverability and
  Self-healed-weird-pause-resume rationale that actually
  justifies the 48-hour value post-v1.8.

No code paths changed. No spec, schema, model, or ruleset changes.
The broker layer continues to raise `BrokerInconsistency` in the
same conditions as before; only the action-layer-response
documentation now reflects the v1.8 framework.

Design refinement: the §11 failure model is reorganized around
operator-relevance (system status) rather than timer behavior.
See `DECISIONS.md` D-SPEC-8 for the framework rationale.

**§11.1 severity model** replaces the prior three-bullet recovery-
category block (Critical transient / blocking / indefinite-halt)
with a three-category operator-relevance framework:

- **Critical (hard broke):** the system genuinely cannot proceed
  without external action. Retrying achieves nothing or could
  cause harm. Sets `operational_pause` with no auto-resume (or
  uses `withdrawal_capacity_exhausted` for the withdrawal-only
  variant).
- **Critical (self-healed weird):** the system already recovered
  by the time the alert fires. Either no pause is set, or the
  pause is one of the 48h-auto-resume Self-healed-weird reasons.
  The alert exists for operator awareness, not action.
- **Critical (normal-ops notice):** routine state transitions
  the operator should see for cadence reporting. No pause, no
  retry, no operator action.

The framework is operator-absence-aware (§1.4). Nothing leaves
the system permanently halted waiting for an operator who is not
returning, *except* the narrow Hard-broke cases where halting is
actively safer than continuing.

**§11.2 entries reclassified.** All Critical entries updated to
carry the new category:

- §11.2.1 broker connectivity loss: Critical (transient) → Critical (self-healed weird).
- §11.2.2 order rejection: Critical (blocking) → Critical (self-healed weird).
- §11.2.3 state file missing/corrupt: Critical (blocking) → Critical (hard broke).
- §11.2.7 cascade exhaustion: Critical (indefinite-halt) → Critical (hard broke, withdrawal-only).
- §11.2.8 partial PhaseTransition: Critical (blocking) → Critical (self-healed weird).
- §11.2.11 Layer B split-brain: Critical (NOT auto-resume) → Critical (self-healed weird). **Pause removed.**
- §11.2.12 configuration validation: Critical (blocking) → Critical (hard broke).
- §11.2.13 disk full: Critical (blocking) → Critical (self-healed weird).
- §11.2.14 broker inconsistency: **split** into Hard-broke sub-cases (account-ID mismatch, malformed Trade data — `operational_pause` set with no auto-resume) and Self-healed-weird sub-cases (post-placement timeout, pre-place query failure — alert only, no pause).
- §11.2.15 external account activity overlap: Critical (NOT auto-resume) → Critical (self-healed weird). **Pause removed.**

Three former pause types are now alerts-only with no operational
halt: §11.2.11 Layer B (`split_brain_detected`), §11.2.15
(`external_activity_overlap`), and two §11.2.14 sub-cases. By the
time these alerts fire, the system has already recovered via
other mechanisms (broker idempotency, IP-tiebreak, fresh-state
re-read on next cycle); the prior pause was protecting against
damage the system had already prevented.

**§11.3 restructured.** §11.3 framing updated to describe both
auto-resume and no-auto-resume pause behaviors (formerly only
auto-resume was contemplated). §11.3.1 pause-reason catalog
rewritten as the canonical 5-value set with a new Category
column:

| `pause_reason` | Category | Auto-resume? |
|---|---|---|
| `partial_phase_transition` | Self-healed weird | Yes (48h) |
| `order_rejection` | Self-healed weird | Yes (48h) |
| `disk_full` | Self-healed weird | Yes (48h) |
| `internal_consistency_violation` | Hard broke | No |
| `broker_inconsistency` | Hard broke | No |

The refuses-to-start entries (`state_file_corrupt`,
`configuration_validation`) are removed from the catalog — they
are §11.2 entries documenting refuse-to-start conditions, not
values the runtime ever writes to the state file.

**§12.6 alert catalog** gains new Critical alerts for the
hard-broke pauses (`internal_consistency_violation`,
`broker_inconsistency`) and the now-alerts-only events
(`split_brain_detected`, `external_activity_overlap`,
`broker_inconsistency_transient`).

**§6.7 Phase 2 swing field** expanded from a single-line bullet
naming the binary state to a full bullet specifying the
`phase2_opportunistic` struct with binary state plus two pending-
confirmation counters (`deploy_pending`, `recover_pending`). The
prior bullet was insufficient: §7.5.2.a requires 2-week
confirmation windows in both directions, but the v1.7.1 schema
and §6.7 had no field for the counters. This addresses the
Candidate-1 phantom-state-field finding.

**§14.4 runbook** operator pause-clearing procedures updated to
match the new two-pause-type model (`internal_consistency_violation`,
`broker_inconsistency`) and to note that three previously-paused
conditions now alert only.

**Supporting files updated:**

- `state_schema.json` (schema_version 1.2): `phase2_opportunistic`
  struct field added (required); `pause_reason` enum expanded to
  the canonical 5-value set; `withdrawal_capacity_exhausted`
  description rewritten per D-SPEC-5 (this back-port should have
  landed in v1.7.1's schema work but did not; corrected now).
- `state_model.py`: new `Phase2SwingState` enum, new
  `Phase2Opportunistic` model class, expanded `PauseReason` enum
  to match the schema. `OperatingState` root gains
  `phase2_opportunistic` field with default-factory
  initialization. `new_initial_state()` updated to construct the
  field; `schema_version` bumped to "1.2".
- `ruleset.yaml` §16 preamble rewritten to describe the
  Self-healed-weird / Hard-broke categorization rather than the
  pre-D-SPEC-8 single-permanent-halt framing.
- `DECISIONS.md` D-SPEC-8 appended.

No new tunable parameters in `ruleset.yaml`. The 48h
`pause_auto_resume_hours` continues to apply (now scoped to
Self-healed-weird pauses only).

**Behavior change.** This is a real behavior change, not just
documentation cleanup. Three §11.2 entries that previously paused
the system no longer do. Pre-v1.8, an absent operator returning
after a Layer-B split-brain, an external-activity-overlap event,
or a transient broker-inconsistency event would find the system
halted; post-v1.8 they find the system running, with the
relevant Critical alerts in the alert log as the forensic record
of what happened.

Defect fix: the v1.7 spec body retained eight occurrences of
"permanent halt" wording for `withdrawal_capacity_exhausted`,
contradicting D-SPEC-5 (which clarified the flag as an
indefinite halt with a single auto-clear path, set in v1.4).
This patch back-ports D-SPEC-5 to current truth throughout the
spec.

**§11.3 restructured** as "Halt mechanisms" with two subsections:
§11.3.1 "Operational pause (auto-resuming)" and §11.3.2
"Indefinite halt (`withdrawal_capacity_exhausted`)". The pause-
reason catalog no longer lists `withdrawal_capacity_exhausted`
(it is a sibling flag, not a `pause_reason` value). The
self-contradicting "intentionally permanent" / "operator never
edits the state file" paragraph was removed; the no-edit fact
is preserved as a one-line statement in §11.3.2.

**§11.2.7 (Cascade exhaustion)** rewritten: severity tag changed
from `Critical (blocking, **permanent halt**)` to `Critical
(indefinite-halt)`. The Recovery bullet no longer says "No
automatic recovery" immediately before describing the automatic
auto-clear; instead, it states the auto-clear directly. The
"external operator action is required" framing was softened to
"cash entering the managed account," which matches the §2.9
"cash appears, system reacts" mechanism that D-SPEC-5 endorses.

**§11.1 (Severity model)** gained a third recovery category:
**Critical (indefinite-halt)**, alongside the existing
**Critical (transient)** and **Critical (blocking)**. The third
category covers halts that limit a specific operation
(withdrawal-only, in the only current instance) rather than
halting the system entirely, and that auto-clear on a condition
rather than on a timer.

**Word-level fixes:** "permanent halt" → "indefinite halt" in
§6.7 state schema, §7.3.2 step 4, §12.6 alert catalog row, §13.3
integration test list, and §16 glossary (Cascade exhaustion).
The §16 glossary entries for `operational_pause` and
`withdrawal_capacity_exhausted` were rewritten to reflect the new
§11.3.1/§11.3.2 structure and the D-SPEC-5 auto-clear semantics.

No design changes. D-SPEC-5 (committed to DECISIONS.md in v1.4)
is the canonical record of the original decision; this patch is
strictly an editorial back-port.

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
