"""
engine.py — main simulator run loop.

Public exports:
    run(params) — the simulator's primary entry point

See also: IPMS_SPECIFICATION.md §3 (run engine)


CHECKPOINT 2 POSTURE — TRULY MINIMAL ENGINE

This engine is the vertical slice of the v0.1.0 IPMS. Its purpose is
to verify the wiring works end-to-end:
  - SimulationParams flows in
  - MarketSimulator loads price data
  - AdvancingClock drives a daily date loop
  - StateCollector accumulates per-tick snapshots
  - SimulationResult flows out with valid event log + metadata
  - Determinism holds (two runs same params = byte-identical output)

What this engine does NOT do (deferred until IRA PM exists per spec
§10.3 RESOLVED):
  - Run any IRA PM module. There is no IRA PM yet.
  - Evaluate circuit breakers, recovery, withdrawals, rebalances.
  - Execute trades, even synthetic ones.
  - Fire alerts.
  - Apply CPI raises or guardrail cuts.
  - Drive Phase 2 transitions.

Snapshots emitted by this engine reflect a frozen starting portfolio:
the parameter-specified initial allocations, prices that change daily
from the market data, and nothing else. The portfolio holds its
shares forever; no withdrawals leave, no rebalances fire, no buffer
moves. This is enough to validate the seam without locking in any
IRA PM design choices speculatively.

Comments in the daily loop mark the points where future IRA PM
integration will plug in. Those comments name the integration but
deliberately do not declare protocol shapes — protocols are an IRA
PM specification deliverable, not an IPMS implementation detail.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from ipms.clock import AdvancingClock
from ipms.collector import StateCollector
from ipms.events import (
    AssetBalance,
    CashFlow,
    CashFlowSource,
    Snapshot,
)
from ipms.market import MarketSimulator
from ipms.params import SimulationParams
from ipms.params import TimeSeriesDetail
from ipms.proxy_map import BUFFER_SYMBOL
from ipms.result import RunMetadata, SimulationResult


# ============================================================================
# DAILY HOOK TYPE
# ============================================================================
# Optional callback invoked once per simulated day by run(). When None
# (default), the engine runs in its original frozen-portfolio v0.1.0
# mode: positions never change, only re-pricing happens on output ticks.
#
# When a hook is supplied (e.g., by the IRAPM harness driver):
#   - The portfolio is re-priced EVERY day (not just on output ticks)
#     so the hook sees current values.
#   - The hook receives (today, portfolio, market, collector) and
#     returns a (possibly mutated) portfolio dict.
#   - The returned dict replaces the engine's live portfolio reference
#     for subsequent ticks, so trades, withdrawals, and rebalances
#     dispatched by the hook persist day-over-day.
#   - Determinism is preserved because the hook is called with the
#     simulator's deterministic clock + market state. Hooks that
#     introduce nondeterminism (wall-clock reads, randomness) break
#     the IPMS determinism contract; this is the hook author's
#     responsibility, not the engine's.
#
# This seam is the IPMS↔IRAPM bridge per the harness build plan; it
# replaces the placeholder "FUTURE (when IRA PM exists)" markers in
# the daily loop without changing the engine's checkpoint-2 behavior
# when hook is absent.
DailyHook = Callable[
    [date, dict, MarketSimulator, StateCollector],
    dict,
]


logger = logging.getLogger(__name__)


# ============================================================================
# DEFAULT PHASE 1 ALLOCATION
# ============================================================================
# Hardcoded for checkpoint 2 because the IRA PM doesn't exist yet to
# carry the canonical Phase 1 allocation. These values mirror the
# IPMV1 ruleset's phase_1_allocation as of v1.6.1 and the audit's
# documented operator-confirmed design (60/40 Growth/FI per
# IBKR_PM_CB_Final_Calibration.md Finding 5).
#
# When the IRA PM lands, this hardcoded constant goes away — the
# allocation comes from the IRA PM's ruleset/config, not the simulator.
# Until then, this is the simulator's only way to know how to seed the
# starting portfolio.
# ============================================================================

_PHASE_1_TARGETS: dict[str, tuple[Decimal, str]] = {
    # symbol: (target_weight, asset_class)
    "PYLD": (Decimal("0.25"), "fixed_income"),
    "JPIE": (Decimal("0.15"), "fixed_income"),
    "FBCG": (Decimal("0.30"), "growth"),
    "AVUV": (Decimal("0.30"), "growth"),
    "GBIL": (Decimal("0.00"), "growth"),  # pre-registered, 0% in Phase 1
}

# Default cash buffer for Phase 1, mirroring IPMV1 ruleset
# phase_1_cash_buffer_usd. Replaced by IRA PM config when IRA PM
# integration lands.
_PHASE_1_CASH_BUFFER_DEFAULT = Decimal("4000.00")

# Default SGOV buffer initial value, mirroring IPMV1
# withdrawal_buffer.target_value_usd. Replaced by IRA PM config when
# IRA PM integration lands.
_SGOV_BUFFER_TARGET_DEFAULT = Decimal("72000.00")


# ============================================================================
# RUN
# ============================================================================

def run(
    params: SimulationParams,
    *,
    daily_hook: Optional[DailyHook] = None,
) -> SimulationResult:
    """
    Per spec §3.2. Main entry point — runs the simulator end-to-end
    against `params` and returns a SimulationResult.

    Lifecycle (per spec §3.1):
      1. Initialization — validate params, load price data, construct
         clock and collector.
      2. Initial state seeding — build starting portfolio, snapshot.
      3. Main loop — advance clock day-by-day, emit snapshots on
         output-cadence ticks. When daily_hook is supplied, the hook
         runs once per simulated day inside the loop and may mutate
         the portfolio (this is how IRAPM cycle dispatch plugs in;
         see the IRAPM harness driver irapm_driver.py at the repo
         root). When daily_hook is None, the engine runs in its
         original frozen-portfolio v0.1.0 mode.
      4. Finalization — assemble SimulationResult, return.

    `logger` configuration is set per params.logging_verbosity. This
    is the only place the simulator changes global logging state;
    callers that don't want their own logging affected should save
    and restore the level externally.
    """
    # ----- 1. Initialization -----
    params.validate()

    logging.basicConfig(level=getattr(logging, params.logging_verbosity.upper()))
    logger.info(f"=== IPMS run starting: {params.start_date} → {params.end_date} ===")

    # Wall-clock timing for run metadata. Captured here so the
    # metadata reflects the actual run duration (load time + loop
    # time + finalize time).
    metadata = RunMetadata(
        simulator_version="0.1.0",
        ira_pm_version=None,  # populated when IRA PM integration lands
        run_started_at=datetime.now(),
    )

    # Load price data. May raise PriceDataError on coverage shortfall
    # or missing files; surface to caller without wrapping.
    market = MarketSimulator(start=params.start_date, end=params.end_date)
    logger.info(f"Loaded price data for {len(market.proxy_map)} symbols")

    # Clock and collector. These are owned by the engine for the
    # duration of the run; they do not outlive the run.
    clock = AdvancingClock(start_date=params.start_date)
    collector = StateCollector()

    # ----- 2. Initial state seeding -----
    initial_portfolio = _seed_initial_portfolio(params, market, clock.today())

    # Record the manual_seed cash flow as the FULL starting balance,
    # not just the cash-buffer slice. The seed represents all initial
    # wealth flowing in from outside the simulator (the operator's
    # bank-to-IBKR transfer that establishes the portfolio); recording
    # only the cash-buffer slice attributes ~$4k of a ~$668k inflow,
    # leaving the asset purchases and SGOV buffer seed unaccounted in
    # the cash-flow audit trail. Bug fix 2026-05-09 (A4/E5).
    #
    # The cash-flow attribution discrepancy metric (result.py) is
    # unaffected by this change because it walks consecutive snapshot
    # PAIRS starting at index 1; the seed flow at index 0 is correctly
    # excluded from any inter-snapshot period accounting.
    seed_cash_flow = CashFlow(
        timestamp=clock.today(),
        direction="in",
        amount=params.starting_balance_usd,
        source=CashFlowSource.MANUAL_SEED,
        description="Initial portfolio seeding by simulator (full starting balance)",
    )
    collector.append(seed_cash_flow)

    # First snapshot: starting state on start_date.
    initial_snapshot = _build_snapshot(
        clock.today(), initial_portfolio, market
    )
    collector.append(initial_snapshot)

    # ----- 3. Main loop -----
    # Advance the clock one day at a time through the run window.
    # On each day:
    #   - Re-price the portfolio at today's prices (mutation-free).
    #   - Invoke daily_hook if supplied. The hook may mutate the
    #     portfolio (trades, withdrawals, rebalances dispatched by
    #     IRAPM); its return value replaces the live reference.
    #   - Take a snapshot if today is an output-cadence tick.
    #
    # When daily_hook is None, the engine runs in its original
    # frozen-portfolio v0.1.0 mode: re-pricing is still cheap and
    # deterministic, snapshots use the re-priced state, output is
    # byte-identical to the pre-hook implementation.
    last_snapshot_date = clock.today()
    portfolio = initial_portfolio   # live reference, replaced each day

    while clock.today() < params.end_date:
        clock.advance_one_day()
        today = clock.today()

        # Re-price the portfolio at today's market prices. The function
        # is mutation-free so `initial_portfolio` stays stable for the
        # seed snapshot's CashFlow.MANUAL_SEED reference.
        portfolio = _update_portfolio_prices(portfolio, market, today)

        # Invoke the daily hook (IRAPM cycle dispatch lives here when
        # the harness driver supplies one). Returned portfolio
        # replaces the live reference; subsequent ticks see whatever
        # the hook did. When daily_hook is None, this branch is
        # skipped and the loop behaves as v0.1.0.
        if daily_hook is not None:
            portfolio = daily_hook(today, portfolio, market, collector)

        # Snapshot on output-cadence ticks. v0.1.0 supports the two
        # native cadences (monthly, weekly); explicit output_points
        # will land alongside the cadence work in checkpoint 3.
        if _is_output_tick(today, last_snapshot_date, params):
            snapshot = _build_snapshot(today, portfolio, market)
            collector.append(snapshot)
            last_snapshot_date = today

    # Final snapshot at end_date (in case the loop didn't land on a
    # tick exactly at end_date, the operator still wants to see the
    # terminal state). Skipped in output_points mode — if the operator
    # supplied an explicit date list, they get exactly those dates,
    # not extras.
    if last_snapshot_date != clock.today() and params.output_points is None:
        final_snapshot = _build_snapshot(clock.today(), portfolio, market)
        collector.append(final_snapshot)

    # ----- 4. Finalization -----
    metadata.run_finished_at = datetime.now()

    result = SimulationResult(
        events=collector.events,
        params=params,
        output_dir=None,        # populated below by file-output stage
        output_files={},        # populated below
        metadata=metadata,
    )

    # File output. Imports kept inside the function so a caller that
    # only wants programmatic access (e.g. a test or a future solver
    # wrapper) can still construct a SimulationResult-equivalent
    # without triggering output module imports unnecessarily. In
    # checkpoint 3, every run writes files; future versions may add
    # a `write_output_files=False` parameter for analysis-only runs.
    from ipms.output import (
        create_run_directory,
        write_balances_annual,
        write_balances_monthly,
        write_parameters_md,
        write_run_readme,
    )

    run_dir = create_run_directory(result)
    result.output_dir = run_dir

    # Parameters file is written first; it doesn't depend on the
    # output_files dict.
    params_path = run_dir / "parameters.md"
    write_parameters_md(result, params_path)
    result.output_files["parameters"] = params_path

    # balances_monthly.md only when time_series_detail == MONTHLY
    # (default). ANNUAL_ONLY mode suppresses it per spec §2.1 and
    # §5.4. balances_annual.md is always produced (cheap, useful).
    if params.time_series_detail == TimeSeriesDetail.MONTHLY:
        monthly_path = run_dir / "balances_monthly.md"
        write_balances_monthly(result, monthly_path)
        result.output_files["balances_monthly"] = monthly_path

    annual_path = run_dir / "balances_annual.md"
    write_balances_annual(result, annual_path)
    result.output_files["balances_annual"] = annual_path

    # cascade_log.md (per spec §5.5). Always produced — the file is
    # short for clean Phase 1 runs (Section 2 empty, Section 1 wide
    # but at_residual flags all false), substantial for runs with
    # CB2 events. Schema is locked in now so future IRA PM cascade
    # activity populates the file without code changes.
    from ipms.output import write_cascade_log
    cascade_path = run_dir / "cascade_log.md"
    write_cascade_log(result, cascade_path)
    result.output_files["cascade_log"] = cascade_path

    # Checkpoint 5 formatters (per spec §5.4 and §5.6). All produced
    # unconditionally. For v0.1.0 runs without an IRA PM these contain
    # only header + column schema + "no events" note, since the frozen
    # portfolio produces no withdrawals, trades, CB events, recoveries,
    # annual reviews, phase transitions, or alerts. The schemas are
    # locked in now so future IRA PM activity populates the files
    # without code changes.
    from ipms.output import (
        write_alerts,
        write_annual_reviews,
        write_cb_events,
        write_phase_transition,
        write_rebalances,
        write_recovery_events,
        write_withdrawals,
    )

    withdrawals_path = run_dir / "withdrawals.md"
    write_withdrawals(result, withdrawals_path)
    result.output_files["withdrawals"] = withdrawals_path

    rebalances_path = run_dir / "rebalances.md"
    write_rebalances(result, rebalances_path)
    result.output_files["rebalances"] = rebalances_path

    cb_events_path = run_dir / "cb_events.md"
    write_cb_events(result, cb_events_path)
    result.output_files["cb_events"] = cb_events_path

    recovery_events_path = run_dir / "recovery_events.md"
    write_recovery_events(result, recovery_events_path)
    result.output_files["recovery_events"] = recovery_events_path

    annual_reviews_path = run_dir / "annual_reviews.md"
    write_annual_reviews(result, annual_reviews_path)
    result.output_files["annual_reviews"] = annual_reviews_path

    phase_transition_path = run_dir / "phase_transition.md"
    write_phase_transition(result, phase_transition_path)
    result.output_files["phase_transition"] = phase_transition_path

    alerts_path = run_dir / "alerts.md"
    write_alerts(result, alerts_path)
    result.output_files["alerts"] = alerts_path

    # README written LAST so its file inventory reflects every file
    # actually produced. Writing the README first would miss any
    # conditional files (balances_monthly when ANNUAL_ONLY, future
    # checkpoint formatters that only fire under specific conditions).
    readme_path = run_dir / "README.md"
    write_run_readme(result, readme_path)
    result.output_files["readme"] = readme_path

    logger.info(
        f"=== IPMS run complete: {len(collector)} events captured, "
        f"terminal AUM ${result.terminal_aum}, output: {run_dir} ==="
    )

    return result


# ============================================================================
# INTERNAL HELPERS
# ============================================================================

def _seed_initial_portfolio(
    params: SimulationParams,
    market: MarketSimulator,
    start_date,
) -> dict:
    """
    Build the starting portfolio dict. Allocates starting_balance_usd
    across managed assets per Phase 1 weights, plus the cash buffer,
    plus the SGOV buffer.

    Returns a dict (not a typed object) because checkpoint 2 doesn't
    yet need a Portfolio type. The shape becomes a typed object when
    the IRA PM integration lands and shape questions stabilize.

    Dict shape:
      {
        "assets": {symbol: {"quantity", "market_price", "market_value",
                           "weight", "asset_class"}},
        "cash_value": Decimal,
        "sgov_buffer_value": Decimal,
        "sgov_buffer_quantity": Decimal,
        "sgov_buffer_price": Decimal,
      }
    """
    starting_balance = params.starting_balance_usd
    cash_buffer = (
        params.initial_cash_usd
        if params.initial_cash_usd is not None
        else _PHASE_1_CASH_BUFFER_DEFAULT
    )
    sgov_target = (
        params.initial_sgov_buffer_usd
        if params.initial_sgov_buffer_usd is not None
        else _SGOV_BUFFER_TARGET_DEFAULT
    )

    # Available for managed allocation = starting_balance - cash_buffer
    # - sgov_buffer. The SGOV buffer is held outside managed_net_liq
    # per the IRA PM design (audit Constraint 3: SGOV is not part of
    # main rebalancer's purview).
    deployable = starting_balance - cash_buffer - sgov_target
    if deployable <= 0:
        raise ValueError(
            f"Starting balance {starting_balance} insufficient to cover "
            f"cash buffer {cash_buffer} + SGOV buffer {sgov_target}"
        )

    # Allocate deployable across managed targets. Compute share count
    # from target dollar amount / market price on start_date. Quantize
    # shares to 4 decimals matching IBKR's typical reporting precision.
    # Residuals from ROUND_DOWN are accumulated and credited to cash
    # at the end so gross_net_liq matches starting_balance_usd exactly
    # (A5/E4 fix — was: residuals silently disappeared, leaving the
    # first snapshot's gross_net_liq slightly under starting_balance).
    assets = {}
    allocation_residual = Decimal(0)
    for symbol, (weight, asset_class) in _PHASE_1_TARGETS.items():
        target_dollars = deployable * weight
        if target_dollars <= 0:
            # Pre-registered symbols (GBIL in Phase 1 at 0%) get a
            # zero-share position. Hold the slot so the IRA PM doesn't
            # complain about "introducing new symbols mid-flight" when
            # Phase 2 transitions GBIL to 10%.
            assets[symbol] = {
                "quantity": Decimal(0),
                "market_price": market.price_on(symbol, start_date),
                "market_value": Decimal(0),
                "weight": Decimal(0),
                "asset_class": asset_class,
            }
            continue

        price = market.price_on(symbol, start_date)
        # Round-down on initial seeding to avoid creating fractional
        # shares that would slightly overspend the target dollar amount.
        shares = (target_dollars / price).quantize(
            Decimal("0.0001"), rounding="ROUND_DOWN"
        )
        actual_value = shares * price
        allocation_residual += target_dollars - actual_value
        assets[symbol] = {
            "quantity": shares,
            "market_price": price,
            "market_value": actual_value,
            "weight": weight,    # target weight; actual computed in snapshot
            "asset_class": asset_class,
        }

    # SGOV buffer. Use price_on for the buffer too — even though the
    # buffer is dollar-targeted, knowing the share count lets future
    # integration model SGOV's distributions correctly.
    sgov_first_date = market.first_date(BUFFER_SYMBOL)
    if start_date < sgov_first_date:
        # Pre-launch SGOV (start_date before SGOV's first available
        # row, typically 2020-05-26). Synthesize a flat $100 price
        # so quantity math works. The buffer's role is dollar-targeted,
        # so the price is arbitrary; on the day we cross into real
        # SGOV data we REBASE the share count so dollar value is
        # preserved (no phantom CashFlow). See _update_portfolio_prices.
        sgov_price = Decimal("100.00")
        sgov_pre_launch = True
    else:
        sgov_price = market.price_on(BUFFER_SYMBOL, start_date)
        sgov_pre_launch = False
    sgov_quantity = (sgov_target / sgov_price).quantize(
        Decimal("0.0001"), rounding="ROUND_DOWN"
    )

    # Account for SGOV-buffer rounding residual the same way as
    # managed assets (A4/E4 fix): credit any unallocated dollars to
    # cash so gross_net_liq matches starting_balance_usd exactly.
    sgov_actual_value = sgov_quantity * sgov_price
    sgov_residual = sgov_target - sgov_actual_value
    cash_value = cash_buffer + allocation_residual + sgov_residual

    return {
        "assets": assets,
        "cash_value": cash_value,
        "sgov_buffer_value": sgov_actual_value,
        "sgov_buffer_quantity": sgov_quantity,
        "sgov_buffer_price": sgov_price,
        "sgov_pre_launch": sgov_pre_launch,
    }


def _update_portfolio_prices(
    portfolio: dict, market: MarketSimulator, today
) -> dict:
    """
    Re-price the portfolio's positions at `today`'s market prices.
    Quantities are unchanged (no trades happen in v0.1.0); only
    prices and values move.

    Returns a new dict; does not mutate the input. Mutation-free
    so the initial_portfolio reference held by the engine stays
    stable across the loop (no accidental drift from re-pricing
    a stale reference).

    SGOV buffer pre-launch handling (A3/E3 fix 2026-05-09): when the
    portfolio is in pre-launch SGOV mode, the buffer holds a flat
    dollar value (synthetic $100 price). On the first tick where
    real SGOV price data is available, we REBASE the share count so
    the dollar value is preserved across the pre/post boundary. This
    avoids a phantom value jump when the synthetic-price era ends.
    Subsequent ticks track real SGOV prices normally.
    """
    new_assets = {}
    for symbol, pos in portfolio["assets"].items():
        new_price = market.price_on(symbol, today)
        new_value = pos["quantity"] * new_price
        new_assets[symbol] = {
            "quantity": pos["quantity"],
            "market_price": new_price,
            "market_value": new_value,
            "weight": pos["weight"],
            "asset_class": pos["asset_class"],
        }

    # SGOV buffer re-pricing with pre-launch handling.
    sgov_pre_launch = portfolio.get("sgov_pre_launch", False)
    sgov_first_date = market.first_date(BUFFER_SYMBOL)

    if sgov_pre_launch and today < sgov_first_date:
        # Still pre-launch — hold the synthetic flat price and
        # quantity. Buffer value remains the dollar target it was
        # seeded with (ignoring any cascade activity, which v0.1.0
        # doesn't model anyway).
        sgov_price = portfolio["sgov_buffer_price"]
        sgov_quantity = portfolio["sgov_buffer_quantity"]
        sgov_value = sgov_quantity * sgov_price
        new_pre_launch = True
    elif sgov_pre_launch and today >= sgov_first_date:
        # First tick on/after SGOV launch — REBASE quantity so the
        # current dollar value is preserved at the new real price.
        # No phantom CashFlow needed because the dollar value didn't
        # change; only the (price, quantity) split did.
        current_dollar_value = (
            portfolio["sgov_buffer_quantity"]
            * portfolio["sgov_buffer_price"]
        )
        sgov_price = market.price_on(BUFFER_SYMBOL, today)
        sgov_quantity = (current_dollar_value / sgov_price).quantize(
            Decimal("0.0001"), rounding="ROUND_DOWN"
        )
        # Any rounding residual from the rebase is small (sub-cent
        # range) and gets absorbed into the buffer's reported value
        # rather than synthesizing a phantom cash flow. Acceptable
        # because the buffer is dollar-targeted, not share-targeted.
        sgov_value = sgov_quantity * sgov_price
        new_pre_launch = False
    else:
        # Normal post-launch operation — re-price at today's real
        # market price.
        sgov_price = market.price_on(BUFFER_SYMBOL, today)
        sgov_quantity = portfolio["sgov_buffer_quantity"]
        sgov_value = sgov_quantity * sgov_price
        new_pre_launch = False

    return {
        "assets": new_assets,
        "cash_value": portfolio["cash_value"],
        "sgov_buffer_value": sgov_value,
        "sgov_buffer_quantity": sgov_quantity,
        "sgov_buffer_price": sgov_price,
        "sgov_pre_launch": new_pre_launch,
    }


def _build_snapshot(today, portfolio: dict, market: MarketSimulator) -> Snapshot:
    """
    Construct a Snapshot event from the current portfolio state.

    Computes managed_net_liq, gross_net_liq, FI/Growth bucket totals,
    weights as fractions of managed_net_liq.

    All CB/recovery state is "inactive" in v0.1.0 (no IRA PM to drive
    transitions). cascade_tier defaults to 2 (FI). phase defaults to 1.
    refill_active defaults to False.
    """
    # Per-asset balances → typed AssetBalance objects
    assets = {}
    fi_value = Decimal(0)
    growth_value = Decimal(0)
    for symbol, pos in portfolio["assets"].items():
        if pos["asset_class"] == "fixed_income":
            fi_value += pos["market_value"]
        else:
            growth_value += pos["market_value"]

    # managed_net_liq = managed positions + cash. SGOV buffer is
    # excluded by design (IRA PM Constraint 3 — buffer outside main
    # portfolio math).
    managed_value = sum(
        (pos["market_value"] for pos in portfolio["assets"].values()),
        start=Decimal(0),
    )
    managed_net_liq = managed_value + portfolio["cash_value"]

    # gross_net_liq adds the buffer. Used for "total wealth" display
    # and the YoY change calc.
    gross_net_liq = managed_net_liq + portfolio["sgov_buffer_value"]

    # Per-asset weights as fraction of managed_net_liq. Defensive:
    # if managed_net_liq somehow drops to zero, weights are zero
    # rather than ZeroDivisionError.
    for symbol, pos in portfolio["assets"].items():
        if managed_net_liq > 0:
            weight = pos["market_value"] / managed_net_liq
        else:
            weight = Decimal(0)
        assets[symbol] = AssetBalance(
            symbol=symbol,
            quantity=pos["quantity"],
            market_price=pos["market_price"],
            market_value=pos["market_value"],
            weight=weight,
            asset_class=pos["asset_class"],
        )

    fi_weight = (fi_value / managed_net_liq) if managed_net_liq > 0 else Decimal(0)
    growth_weight = (
        (growth_value / managed_net_liq) if managed_net_liq > 0 else Decimal(0)
    )

    return Snapshot(
        timestamp=today,
        assets=assets,
        cash_value=portfolio["cash_value"],
        sgov_buffer_value=portfolio["sgov_buffer_value"],
        sgov_buffer_quantity=portfolio["sgov_buffer_quantity"],
        sgov_buffer_price=portfolio["sgov_buffer_price"],
        gross_net_liq=gross_net_liq,
        managed_net_liq=managed_net_liq,
        fi_bucket_value=fi_value,
        growth_bucket_value=growth_value,
        fi_weight=fi_weight,
        growth_weight=growth_weight,
        cb1_state="inactive",
        cb2_state="inactive",
        refill_active=False,
        cascade_tier=2,
        phase=1,
        growth_synthetic_index=None,
    )


def _is_output_tick(today, last_snapshot_date, params: SimulationParams) -> bool:
    """
    True if `today` should produce an output-cadence snapshot.

    Three modes per spec §2.1:
      - output_points set → fire only on dates in the list (cadence
        is ignored, mutual exclusion enforced at param level).
      - output_cadence MONTHLY → fire on month-end (the last day of the
        calendar month). Per spec §5.4: "one row per month-end".
      - output_cadence WEEKLY → fire every 7 days from the previous tick.

    output_points takes precedence: if the operator supplied an
    explicit list, the simulator captures only on those dates and
    ignores cadence entirely. This matches spec §2.1's mutual-
    exclusion semantics.
    """
    # Output points mode — explicit date list takes precedence.
    if params.output_points is not None:
        return today in params.output_points

    # Cadence mode
    if params.output_cadence.value == "monthly":
        # Fire on the LAST day of the calendar month (month-end), per
        # spec §5.4. "Last day" = today + 1 day is in a different
        # month. This handles all month lengths (28/29/30/31)
        # uniformly. Bug fix 2026-05-09 (was: fired on first day of
        # new month, producing month-start snapshots).
        tomorrow = today + timedelta(days=1)
        return tomorrow.month != today.month
    elif params.output_cadence.value == "weekly":
        return (today - last_snapshot_date) >= timedelta(days=7)
    else:
        # Defensive fallback. Validation should prevent reaching here.
        return False
