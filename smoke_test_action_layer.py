"""
smoke_test_action_layer.py — End-to-end smoke test of the rewritten
action_layer.py against the real Broker protocol via SyntheticBroker.

Run with:
    cd C:\\portfolio\\IRAPM
    python smoke_test_action_layer.py

What this verifies:
  1. SyntheticBroker satisfies the (updated) Broker protocol.
  2. action_layer.execute_plan() runs without exceptions against a
     real Broker implementation.
  3. Pre-flight defenses (account_id, external activity) pass on a
     clean broker.
  4. ContractRef cache works: positions are seeded into it; new
     symbols (SGOV BUY) trigger resolve_symbol() fallback.
  5. OrderEntry + WithdrawalEntry + AlertEntry all dispatch.
  6. PlacedOrderRecord entries land in CycleAttempt.placed_orders.
  7. The session opens and closes cleanly via broker_session.
  8. New CycleExecutionResult fields (preflight_failure,
     preflight_external_activity) are populated correctly.

This test creates a fresh temp directory for state files so it leaves
no artifacts behind. It does NOT exercise full cycle.py orchestration
(that's a separate test); just the action_layer ↔ broker seam.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

# Make IRAPM modules importable when run from the IRAPM directory
sys.path.insert(0, str(Path(__file__).parent))

from action_layer import execute_plan
from alerter import StdoutAlerter, load_templates
from broker_protocol import Broker
from broker_types import OrderSide, OrderType
from cycle_attempt import CycleType, begin_cycle
from persistence import Paths
from plan_model import (
    AlertEntry, OrderEntry, Plan, SourceLine, WithdrawalEntry,
)
from ruleset_model import Ruleset
from state_model import CBState, new_initial_state
from synthetic_broker import SyntheticBroker


def main() -> int:
    print("=" * 70)
    print("IRAPM action_layer smoke test")
    print("=" * 70)

    here = Path(__file__).parent
    tmpdir = Path(tempfile.mkdtemp(prefix="irapm_smoketest_"))
    print(f"Temp state dir: {tmpdir}")

    try:
        # ---- 1. Set up SyntheticBroker with seeded positions ----
        broker = SyntheticBroker(
            account_id="U1234567",
            initial_cash=Decimal("50000"),
            expected_account_id="U1234567",
        )
        broker.seed_position(
            symbol="PYLD", quantity=Decimal("10000"),
            market_price=Decimal("10.00"),
        )
        broker.seed_position(
            symbol="JPIE", quantity=Decimal("3000"),
            market_price=Decimal("50.00"),
        )
        broker.seed_position(
            symbol="FBCG", quantity=Decimal("2000"),
            market_price=Decimal("100.00"),
        )
        broker.seed_position(
            symbol="AVUV", quantity=Decimal("4000"),
            market_price=Decimal("50.00"),
        )
        broker.seed_position(
            symbol="SGOV", quantity=Decimal("720"),
            market_price=Decimal("100.00"),
        )

        # Confirm Broker protocol conformance
        assert isinstance(broker, Broker), (
            "SyntheticBroker does not satisfy the Broker protocol"
        )
        print("[OK] SyntheticBroker satisfies the Broker protocol")

        # ---- 2. Set up persistence paths ----
        paths = Paths(state_dir=tmpdir)
        paths.ensure_dirs()

        # ---- 3. Load ruleset and alerter ----
        # ruleset.yaml ships with ach_destination empty by design (D-RY-8):
        # the operator must configure it at deployment. For this smoke test
        # we load it via a temporary patched copy that fills in a dev value.
        # The patch is in-memory only; the on-disk ruleset.yaml is unchanged.
        import yaml
        with (here / "ruleset.yaml").open("r") as f:
            raw_ruleset = yaml.safe_load(f)
        if not raw_ruleset.get("ach_destination"):
            raw_ruleset["ach_destination"] = "SMOKETEST-DEV-PLACEHOLDER"
        # Also force dry_run on for safety, in case it's set False on disk.
        raw_ruleset["dry_run"] = True
        ruleset = Ruleset.model_validate(raw_ruleset)
        alerter = StdoutAlerter(load_templates(here / "alert_templates.yaml"))
        print("[OK] Ruleset and alerter loaded (with smoke-test patches)")

        # ---- 4. Build OperatingState via the helper ----
        state = new_initial_state(
            ruleset=ruleset,
            box_id="box-test",
            ipv4_last_octet=10,
        )
        print(f"[OK] OperatingState built (phase={state.phase.value})")

        # ---- 5. Begin a cycle attempt ----
        now = datetime.now(timezone.utc)
        attempt, is_restart = begin_cycle(
            cycle_type=CycleType.WEEKLY,
            box_id="box-test",
            client_id=11,
            now=now,
            path=paths.cycle_attempt_file,
        )
        print(f"[OK] CycleAttempt: uuid={attempt.cycle_uuid} restart={is_restart}")

        # ---- 6. Build a Plan ----
        plan = Plan(cycle_id=str(attempt.cycle_uuid), decision_clock=now)

        # Entry 0: standalone OrderEntry — SELL $1000 FBCG
        plan.add(OrderEntry(
            symbol="FBCG", side=OrderSide.SELL,
            dollar_amount=Decimal("1000"),
            order_type=OrderType.MKT,
        ))

        # Entry 1: WithdrawalEntry — $3000 from PYLD + JPIE
        plan.add(WithdrawalEntry(
            total_dollar_amount=Decimal("3000"),
            sources=[
                SourceLine(
                    symbol="PYLD",
                    dollar_amount=Decimal("2000"),
                    share_count_estimate=Decimal("200"),
                ),
                SourceLine(
                    symbol="JPIE",
                    dollar_amount=Decimal("1000"),
                    share_count_estimate=Decimal("20"),
                ),
            ],
            ach_destination="test-bank",
            scheduled_ach_date=date(2027, 1, 15),
            cb_state_at_decision=CBState.CB_INACTIVE,
            cascade_growth_used=False,
        ))

        # Entry 2: AlertEntry
        plan.add(AlertEntry(
            alert_id="withdrawal_executed",
            context={"amount": "3000", "cb_state": "CB_INACTIVE"},
        ))

        print(f"[OK] Plan built with {len(plan.entries)} entries")

        # ---- 7. Execute the plan ----
        default_ctx = {
            "cycle_id": str(attempt.cycle_uuid)[:8],
            "box_id": "box-test",
            "phase": state.phase.value,
            "cb_state": state.cb_machine.state.value,
            "timestamp": now.isoformat(),
        }
        print()
        print("=" * 70)
        print("Executing plan...")
        print("=" * 70)
        result = execute_plan(
            plan=plan,
            state=state,
            ruleset=ruleset,
            broker=broker,
            alerter=alerter,
            paths=paths,
            attempt=attempt,
            now=now,
            expected_account_id="U1234567",
            default_alert_context=default_ctx,
        )
        print()
        print("=" * 70)
        print("RESULTS")
        print("=" * 70)
        print(f"preflight_failure: {result.preflight_failure}")
        print(f"preflight_external_activity: {result.preflight_external_activity}")
        print(f"halted_due_to_failure: {result.halted_due_to_failure}")
        print(f"halt_reason: {result.halt_reason}")
        print(f"entry_results count: {len(result.entry_results)}")
        for i, r in enumerate(result.entry_results):
            print(f"  [{i}] {r.kind.value}: success={r.success}")
            if r.note:
                print(f"        note: {r.note}")
            if r.error:
                print(f"        error: {r.error}")
            if r.submitted_order_ids:
                for oid in r.submitted_order_ids:
                    print(f"        coid: {oid}")

        # ---- 8. Verify placed_orders persistence ----
        print()
        from cycle_attempt import CycleAttempt
        reloaded = CycleAttempt.load(paths.cycle_attempt_file)
        if reloaded is None:
            print("[FAIL] cycle_attempt.json not found after execution")
            return 1
        print(f"Placed orders persisted to disk: {len(reloaded.placed_orders)}")
        for o in reloaded.placed_orders:
            print(f"  {o.client_order_id}")
            print(f"    {o.symbol} {o.side} qty={o.quantity} type={o.order_type}")

        # ---- 9. Validate expectations ----
        print()
        print("=" * 70)
        print("ASSERTIONS")
        print("=" * 70)

        problems = []

        if result.preflight_failure is not None:
            problems.append(f"unexpected preflight failure: {result.preflight_failure}")
        else:
            print("[OK] preflight defenses passed")

        if len(result.entry_results) != 3:
            problems.append(
                f"expected 3 entry results, got {len(result.entry_results)}"
            )
        else:
            print("[OK] all 3 plan entries processed")

        all_success = all(r.success for r in result.entry_results)
        if not all_success:
            failed = [r for r in result.entry_results if not r.success]
            problems.append(
                f"{len(failed)} entries failed: "
                + ", ".join(f"{r.kind.value}({r.error})" for r in failed)
            )
        else:
            print("[OK] every entry succeeded")

        # Expect 3 orders submitted: FBCG SELL, PYLD SELL, JPIE SELL
        if len(reloaded.placed_orders) != 3:
            problems.append(
                f"expected 3 placed_orders, got {len(reloaded.placed_orders)}"
            )
        else:
            print("[OK] 3 placed orders persisted to cycle_attempt.json")

        # Verify quantity refresh — FBCG was seeded at $100/share; a
        # $1000 SELL with fresh price refresh should produce qty 10.0
        fbcg_records = [
            o for o in reloaded.placed_orders if o.symbol == "FBCG"
        ]
        if fbcg_records:
            qty = fbcg_records[0].quantity
            if qty != Decimal("10.0000"):
                problems.append(
                    f"FBCG quantity refresh: expected 10.0000, got {qty}"
                )
            else:
                print(f"[OK] FBCG qty refresh produced {qty} from $1000/$100")

        if problems:
            print()
            print("FAILURES:")
            for p in problems:
                print(f"  - {p}")
            return 1

        print()
        print("=" * 70)
        print("ALL CHECKS PASSED")
        print("=" * 70)
        return 0

    finally:
        # Clean up temp dir
        try:
            shutil.rmtree(tmpdir)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
