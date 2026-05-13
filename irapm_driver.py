"""
irapm_driver.py — Harness driver bridging IPMS market simulation with
IRAPM's cycle layer.

PURPOSE:
    Drive scenario-based end-to-end tests of IRAPM by:
      1. Loading a scenario YAML (SimulationParams fields + harness
         extras: name, tokens timeline, market_overrides, expectations).
      2. Translating C:/portfolio/data/ TSV files from Yahoo-format
         ("Apr 13, 2026") into IRAPM-format (ISO YYYY-MM-DD) in a
         per-run temp dir, so IRAPM's lookback_signal can read them.
      3. Constructing a SyntheticBroker seeded with starting positions
         per Phase 1 weights, plus a MockTokenDetector wrapped to mirror
         observations between boxes (single-box mode pending CL260 hardware).
      4. Invoking ipms.engine.run() with a daily_hook that dispatches
         IRAPM cycle.run_weekly_cycle() on the configured weekly
         cycle weekday (default Wednesday per spec §9.6) and
         cycle.run_daily_token_cycle() on every other day, reconciling
         the broker's state changes back into the IPMS portfolio dict
         after each cycle. The weekday is the module-level constant
         _WEEKLY_CYCLE_WEEKDAY (single source of truth).
      5. Asserting end-of-run expectations and producing a pass/fail
         exit code suitable for CI / smoke-test gating.

DESIGN PROPERTIES:
    - Lives at repo root as a peer of cycle.py rather than inside IPMS,
      so the harness is positioned ABOVE both packages rather than
      living inside one of them. This avoids a sys.path shim and
      preserves clean import direction.
    - Does not modify IRAPM's engine.py beyond the daily_hook seam
      already added (see IPMS_SPECIFICATION daily_hook docs).
    - Persists all per-run state under a tempfile.TemporaryDirectory
      so scenarios cannot cross-contaminate state.
    - Token mirroring is single-box "always agree" semantics pending
      CL260 hardware delivery; once both boxes exist this collapses
      to actual two-box AND-semantics by replacing
      MirroredMockTokenDetector with a network-aware variant.

CONSTRAINTS NOT YET ADDRESSED (documented for future sessions):
    - IBKR resolve_symbol() — ibkr_broker.py needs a reqContractDetails
      lookup before first real-broker connection. Not blocking the
      synthetic-harness path.
    - Alert template context audit — the smoke test surfaced unfilled
      placeholders in withdrawal_executed; decision_layer's withdrawal
      builder must populate amount_dollars, source_breakdown, and
      ach_settlement_date. Cross-template sweep deferred to a separate
      session.

USAGE:
    From the repo root, with the venv activated:

        python -m ipms.run_irapm scenarios/baseline.yaml

    Returns exit code 0 if all expectations passed, 1 otherwise. The
    CLI entrypoint at IPMS/ipms/run_irapm.py is a thin wrapper around
    irapm_driver.run_scenario() in this module.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import yaml

# IRAPM imports (at repo root, same directory as this module).
from action_layer import execute_plan  # noqa: F401  (kept for explicit dependency surfacing)
from alerter import StdoutAlerter, load_templates
from clock import AdvancingClock
from cycle import CycleConfig, run_daily_token_cycle, run_weekly_cycle
from persistence import Paths, load_operating_state, save_operating_state
from ruleset_model import Ruleset
from state_model import OperatingState, new_initial_state
from synthetic_broker import SyntheticBroker
from tokens import MockTokenDetector, TokenObservation

# IPMS imports.
from ipms.engine import run as ipms_run
from ipms.market import MarketSimulator
from ipms.params import SimulationParams, load_params_from_yaml
from ipms.proxy_map import BUFFER_SYMBOL
from ipms.result import SimulationResult


logger = logging.getLogger(__name__)


# ============================================================================
# SCENARIO YAML SCHEMA
# ============================================================================

@dataclass
class TokenEvent:
    """One scheduled change to the MockTokenDetector's counts.

    `action` values:
      - "insert"        — set both counts to (2 phase3, 0 stopincome)
      - "remove"        — set phase3 count to 0 (triggers Phase 3 latch)
      - "stop_income"   — set stopincome count to 1 (pauses withdrawals)
      - "resume_income" — set stopincome count to 0
    """
    date: date
    action: str


@dataclass
class MarketOverride:
    """One forced price on one date for one symbol. Bypasses the
    market simulator's actual price for that symbol on that day.
    Used to force CB-trigger scenarios where the historical data
    wouldn't naturally produce the drawdown the test requires.
    """
    symbol: str
    date: date
    price: Decimal


@dataclass
class Expectations:
    """End-of-run assertions. Any field set to non-None is checked;
    None-valued fields are skipped (no assertion against that metric).

    The set of supported expectations is intentionally small to start;
    add fields as scenarios reveal what's worth asserting.
    """
    final_phase: Optional[str] = None
    """Operating state's final phase ('PHASE_1', 'PHASE_2', 'PHASE_3')."""

    cb1_triggered_at_least_once: Optional[bool] = None
    """True iff the cycle log shows at least one CB1 entry transition."""

    cb2_triggered_at_least_once: Optional[bool] = None
    """True iff the cycle log shows at least one CB2 entry transition."""

    no_failed_cycles: Optional[bool] = None
    """True iff every cycle log entry has execution_halted == False."""

    final_balance_dollars_at_least: Optional[Decimal] = None
    """Terminal AUM (from IPMS SimulationResult) must be >= this value."""

    total_withdrawals_dollars_at_least: Optional[Decimal] = None
    """Sum of successful withdrawal dollar amounts from cycle log."""


@dataclass
class ScenarioConfig:
    """A loaded scenario. `sim_params` carries the entire SimulationParams
    block (start_date, end_date, balances, output config, etc.); the
    harness-specific extras (name, tokens, market_overrides, expectations)
    are sibling fields.

    The split lets the harness pass `sim_params` directly into
    `ipms_run()` while consuming the extras itself for hook construction
    and expectations checking.
    """
    name: str
    sim_params: SimulationParams
    tokens: list[TokenEvent] = field(default_factory=list)
    market_overrides: list[MarketOverride] = field(default_factory=list)
    expectations: Expectations = field(default_factory=Expectations)
    persist_state: bool = False
    """When True, copy the harness's per-run state directory (cycle log,
    token log, operating state JSON) into <output_dir>/<run_name>/
    _harness_state/ before the temp dir auto-cleans. Default False keeps
    the cheap path; flip on for baseline / debug runs where forensic
    inspection of the cycle log is needed."""


# ----------------------------------------------------------------------------
# Scenario loader
# ----------------------------------------------------------------------------

# Keys at the top level of a scenario YAML that the harness handles
# itself. Everything else gets passed through to load_params_from_yaml.
_HARNESS_KEYS = {"name", "tokens", "market_overrides", "expectations", "persist_state"}


def load_scenario(path: Path) -> ScenarioConfig:
    """Parse a scenario YAML into a ScenarioConfig.

    The YAML is read once and split: harness-specific keys are pulled
    out and validated here; the remainder is written to a temporary
    YAML file and handed to ipms.params.load_params_from_yaml(). This
    indirection lets us reuse IPMS's existing parameter validation
    without duplicating its conversion logic (date parsing, Decimal
    coercion, enum normalization).

    Raises ValueError for harness-specific schema problems and lets
    IPMS's ParameterValidationError propagate for sim_params issues.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(
            f"Scenario file must be a YAML mapping; got {type(raw).__name__}"
        )

    # Pull out harness keys.
    name = raw.pop("name", path.stem)
    raw_tokens = raw.pop("tokens", []) or []
    raw_overrides = raw.pop("market_overrides", []) or []
    raw_expectations = raw.pop("expectations", {}) or {}
    persist_state = bool(raw.pop("persist_state", False))

    # Validate token events.
    tokens: list[TokenEvent] = []
    for i, t in enumerate(raw_tokens):
        if not isinstance(t, dict):
            raise ValueError(f"tokens[{i}] must be a mapping; got {t!r}")
        if "date" not in t or "action" not in t:
            raise ValueError(
                f"tokens[{i}] must have 'date' and 'action' keys; got {t!r}"
            )
        d = _coerce_date(t["date"], f"tokens[{i}].date")
        action = str(t["action"])
        if action not in {"insert", "remove", "stop_income", "resume_income"}:
            raise ValueError(
                f"tokens[{i}].action must be one of "
                f"'insert', 'remove', 'stop_income', 'resume_income'; got {action!r}"
            )
        tokens.append(TokenEvent(date=d, action=action))

    # Validate market overrides.
    overrides: list[MarketOverride] = []
    for i, o in enumerate(raw_overrides):
        if not isinstance(o, dict):
            raise ValueError(f"market_overrides[{i}] must be a mapping; got {o!r}")
        if not {"symbol", "date", "price"}.issubset(o.keys()):
            raise ValueError(
                f"market_overrides[{i}] must have 'symbol', 'date', 'price'; "
                f"got {o!r}"
            )
        overrides.append(MarketOverride(
            symbol=str(o["symbol"]),
            date=_coerce_date(o["date"], f"market_overrides[{i}].date"),
            price=Decimal(str(o["price"])),
        ))

    # Validate expectations.
    expectations = Expectations(
        final_phase=raw_expectations.get("final_phase"),
        cb1_triggered_at_least_once=raw_expectations.get("cb1_triggered_at_least_once"),
        cb2_triggered_at_least_once=raw_expectations.get("cb2_triggered_at_least_once"),
        no_failed_cycles=raw_expectations.get("no_failed_cycles"),
        final_balance_dollars_at_least=(
            Decimal(str(raw_expectations["final_balance_dollars_at_least"]))
            if "final_balance_dollars_at_least" in raw_expectations else None
        ),
        total_withdrawals_dollars_at_least=(
            Decimal(str(raw_expectations["total_withdrawals_dollars_at_least"]))
            if "total_withdrawals_dollars_at_least" in raw_expectations else None
        ),
    )

    # Remaining keys go to IPMS's SimulationParams loader. Write them
    # to a temp YAML so we can call the existing loader unchanged.
    #
    # Sentinel-injection for output_dir: IPMS's default is
    # Path('C:/portfolio/runs') which fails validation on non-Windows
    # hosts (WSL, Linux dev, CI). To keep scenarios platform-agnostic,
    # the harness allows scenarios to OMIT output_dir entirely; we
    # inject a sentinel value here that's guaranteed to pass
    # validation (system temp dir's parent always exists), and
    # run_scenario() detects the sentinel post-load and rewrites it
    # to <repo_root>/runs/. The sentinel approach keeps IPMS's loader
    # unchanged (it just sees a valid path).
    if "output_dir" not in raw:
        raw["output_dir"] = str(Path(tempfile.gettempdir()) / "_irapm_harness_output_dir_sentinel")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        yaml.safe_dump(raw, tmp)
        tmp_path = Path(tmp.name)
    try:
        sim_params = load_params_from_yaml(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return ScenarioConfig(
        name=str(name),
        sim_params=sim_params,
        tokens=tokens,
        market_overrides=overrides,
        expectations=expectations,
        persist_state=persist_state,
    )


def _coerce_date(v: Any, field_name: str) -> date:
    """Accept yaml's native date, or 'YYYY-MM-DD' string."""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError as e:
            raise ValueError(f"{field_name}: bad ISO date {v!r}: {e}") from e
    raise ValueError(f"{field_name}: must be a date or ISO string, got {type(v).__name__}")


# ============================================================================
# TSV DATE-FORMAT SHIM
# ============================================================================

def translate_data_tsvs(
    src_dir: Path,
    dst_dir: Path,
    symbols: list[str],
) -> None:
    """Translate Yahoo-format TSVs into IRAPM-format ISO TSVs.

    SOURCE FORMAT (Yahoo Finance web copy-paste, what's in C:/portfolio/data/):
        Date              Open    High    Low     Close   Adj Close   Volume
        Apr 13, 2026      57.39   58.20   56.99   58.20   58.20       -
        Mar 30, 2026      0.197 Distribution       <-- skip these rows
        ...

    DESTINATION FORMAT (what IRAPM's price_data.py requires):
        Date         Adj Close
        2026-04-13   58.20
        2026-04-06   56.84
        ...
        (ISO date in column 1, Adj Close in column 2 by header name;
         distribution rows dropped; other columns optional/ignored.)

    The IPMS market loader is forgiving about Yahoo-format dates and
    distribution rows; the IRAPM lookback loader is not. The harness
    keeps IPMS reading from src_dir (canonical, untouched) while
    writing IRAPM-format copies into dst_dir for the lookback to read.

    SYMBOL MAPPING:
        The IRAPM cycle's lookback reads `<dst_dir>/<SYMBOL>.tsv`
        (e.g., FBCG.tsv, AVUV.tsv). The IPMS-side files use proxy
        names (FBGRX.tsv for FBCG, AVUV.tsv for AVUV). The mapping
        is in ipms.proxy_map.PROXY_MAP. This function looks up the
        proxy for each requested symbol, reads from
        <src_dir>/<PROXY>.tsv, and writes to <dst_dir>/<SYMBOL>.tsv.

    The destination headers are tab-separated to match IRAPM's
    preferred TSV detection.
    """
    from ipms.proxy_map import PROXY_MAP

    dst_dir.mkdir(parents=True, exist_ok=True)

    for symbol in symbols:
        proxy = PROXY_MAP.get(symbol, symbol)
        src_path = src_dir / f"{proxy}.tsv"
        if not src_path.exists():
            # Try .csv fallback (Yahoo download default; mirrors
            # MarketSimulator's resolution order).
            src_path = src_dir / f"{proxy}.csv"
        if not src_path.exists():
            raise FileNotFoundError(
                f"Source price file not found for symbol={symbol} "
                f"(proxy={proxy}): tried {src_dir}/{proxy}.tsv and .csv"
            )

        dst_path = dst_dir / f"{symbol}.tsv"
        _translate_one_tsv(src_path, dst_path)


def _translate_one_tsv(src: Path, dst: Path) -> None:
    """Translate one Yahoo-format TSV/CSV into IRAPM-format TSV.

    Read robustly: tab- or comma-delimited (sniffed from first line),
    Yahoo date format (%b %d, %Y) or ISO (%Y-%m-%d). Filter out
    distribution rows by requiring a parseable decimal Adj Close.
    Write tab-separated with ISO dates.
    """
    with src.open("r", encoding="utf-8") as f:
        first_line = f.readline()
        rest = f.read()

    # Sniff delimiter from header.
    delimiter = "\t" if "\t" in first_line else ","

    # Split header to find column indices.
    header_cells = [c.strip() for c in first_line.rstrip("\r\n").split(delimiter)]
    if "Date" not in header_cells:
        raise ValueError(f"{src}: missing 'Date' column in header: {header_cells!r}")
    # IRAPM expects 'Adj Close'; Yahoo sometimes emits 'AdjClse' or
    # 'AdjClose' (with no space). Accept any of these aliases.
    adj_close_aliases = {"Adj Close", "AdjClose", "AdjClse"}
    adj_close_col = None
    for alias in adj_close_aliases:
        if alias in header_cells:
            adj_close_col = header_cells.index(alias)
            break
    if adj_close_col is None:
        raise ValueError(
            f"{src}: missing Adj Close column in header (tried "
            f"{sorted(adj_close_aliases)}); got {header_cells!r}"
        )
    date_col = header_cells.index("Date")

    # Parse rows.
    rows_out: list[tuple[date, str]] = []  # (date, adj_close_str)
    for line in rest.splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(delimiter)]
        # Distribution rows are short (often 2 cells) and the second
        # cell contains text like '0.197 Distribution'. Skip any row
        # that doesn't have both columns or whose Adj Close cell
        # fails decimal parsing.
        if len(cells) <= max(date_col, adj_close_col):
            continue
        raw_date = cells[date_col]
        raw_price = cells[adj_close_col]
        try:
            parsed_date = _parse_yahoo_date(raw_date)
        except ValueError:
            continue
        try:
            # Validate it's a number; we keep the original string
            # representation (no float intermediate) to preserve
            # whatever precision the source file had.
            Decimal(raw_price)
        except (ValueError, ArithmeticError):
            continue
        rows_out.append((parsed_date, raw_price))

    # Sort ascending by date.
    rows_out.sort(key=lambda r: r[0])

    # Write.
    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["Date", "Adj Close"])
        for d, p in rows_out:
            writer.writerow([d.isoformat(), p])


def _parse_yahoo_date(s: str) -> date:
    """Parse 'Apr 13, 2026' or ISO 'YYYY-MM-DD'. Raises ValueError if
    neither format matches."""
    # ISO first (cheap to try; some Yahoo download paths emit it).
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    # Yahoo web copy-paste: 'Mon DD, YYYY'.
    return datetime.strptime(s, "%b %d, %Y").date()


# ============================================================================
# SINGLE-BOX MIRROR TOKEN MODE
# ============================================================================

class MirroredMockTokenDetector:
    """Wraps a MockTokenDetector to mirror every observation to the
    peer-token-observation slot, defeating two-box AND-semantics on a
    single-box harness setup.

    BACKGROUND:
        IRAPM's combine_observations() requires both boxes to agree
        before Phase 3 latches or stop-income pauses fire. Until the
        second box (CL260) is delivered, the harness only has one
        physical detector. This wrapper writes the SAME observation
        to both the box's own token_observation.json and the peer
        slot at state_dir/peer_token_observation.json, so the AND
        semantics are always satisfied by both files agreeing.

    WHEN CL260 ARRIVES:
        Replace this wrapper with a real two-box variant that pulls
        the peer observation from the network (rsync target on the
        master) rather than mirroring the local one. The cycle code
        does not need to change — it reads peer_token_observation.json
        either way.

    DETECTOR PROTOCOL:
        The class satisfies tokens.TokenDetector by delegating detect()
        to the wrapped MockTokenDetector. The mirroring side effect
        runs AFTER detect() — see implementation note in detect().
    """

    def __init__(
        self,
        base_detector: MockTokenDetector,
        paths: Paths,
    ) -> None:
        self._base = base_detector
        self._paths = paths

    # Pass-through controls so the harness can drive the wrapped
    # mock detector through scenario events without unwrapping it.
    def set_counts(self, *, phase3: int, stopincome: int) -> None:
        self._base.set_counts(phase3=phase3, stopincome=stopincome)

    def arm_unavailable(self, error_msg: str = "mock unavailable") -> None:
        self._base.arm_unavailable(error_msg)

    def detect(self, box_id: str, now: datetime) -> TokenObservation:
        """Delegate to the wrapped detector, then mirror the result.

        Mirroring happens AFTER detect() because the daily-token cycle
        writes the observation to disk via save_token_observation() —
        that file is what we copy to the peer slot. We can't mirror
        until the file exists. So the actual mirroring is a no-op here;
        the cycle's persistence layer writes the file, and we copy it
        in mirror_after_cycle() called from the daily_hook.
        """
        return self._base.detect(box_id=box_id, now=now)

    def mirror_after_cycle(self) -> None:
        """Copy the box's own token_observation.json to the peer slot.

        Called by the harness daily_hook after run_daily_token_cycle()
        (and after run_weekly_cycle(), since weekly cycles can also
        update token state). Idempotent and cheap; safe to call
        unconditionally each day.

        If the source file doesn't yet exist (very first cycle before
        any detect() has fired), this is a no-op — the cycle's
        load_token_observation() will return None and the combine
        function will treat it as 'unavailable', which is the correct
        state for that pre-first-detect moment.
        """
        own = self._paths.token_observation_file
        peer = self._paths.state_dir / "peer_token_observation.json"
        if own.exists():
            shutil.copyfile(own, peer)


# ============================================================================
# CYCLE CONFIG CONSTRUCTION
# ============================================================================

def build_cycle_config(
    scenario: ScenarioConfig,
    paths: Paths,
    clock: AdvancingClock,
    repo_root: Path,
    detector: MirroredMockTokenDetector,
) -> tuple[CycleConfig, Ruleset]:
    """Construct the CycleConfig that IRAPM's cycle driver expects.

    Loads ruleset.yaml from the repo root, applies harness-friendly
    overrides (dry_run=True, a placeholder ach_destination), then
    builds the SyntheticBroker seeded with starting positions per the
    scenario's Phase-1 allocation, plus an in-memory StdoutAlerter so
    alerts surface in test output without sending real Email/SMS.

    Returns the (CycleConfig, Ruleset) pair. The Ruleset is also
    needed by build_initial_state() so we expose it explicitly rather
    than tucking it inside CycleConfig (which already holds a reference).
    """
    # ---- Load and patch ruleset ----
    ruleset_path = repo_root / "ruleset.yaml"
    with ruleset_path.open("r", encoding="utf-8") as f:
        raw_ruleset = yaml.safe_load(f)
    if not raw_ruleset.get("ach_destination"):
        raw_ruleset["ach_destination"] = "HARNESS-DEV-PLACEHOLDER"
    raw_ruleset["dry_run"] = True
    ruleset = Ruleset.model_validate(raw_ruleset)

    # ---- Construct broker, seeded with Phase-1 positions ----
    broker = _build_seeded_broker(scenario, ruleset, clock)

    # ---- Construct alerter ----
    alerter = StdoutAlerter(load_templates(repo_root / "alert_templates.yaml"))

    # ---- Assemble CycleConfig ----
    cycle_config = CycleConfig(
        ruleset=ruleset,
        broker=broker,
        alerter=alerter,
        token_detector=detector,
        paths=paths,
        clock=clock,
        box_id="harness-box",
        client_id=11,
        expected_account_id="SIM-HARNESS",
    )
    return cycle_config, ruleset


def _build_seeded_broker(
    scenario: ScenarioConfig,
    ruleset: Ruleset,
    clock: AdvancingClock,
) -> SyntheticBroker:
    """Seed a SyntheticBroker so its positions and cash balance match
    a fresh Phase-1 allocation of scenario.sim_params.starting_balance_usd.

    Allocation follows the same math the IPMS engine uses in
    _seed_initial_portfolio(): subtract cash buffer + SGOV buffer from
    starting balance, allocate the remainder across Phase-1 weights at
    start-of-window prices. Prices come from a temporary MarketSimulator
    constructed against the canonical data dir — same source IPMS uses,
    so the synthetic broker's seeded positions match IPMS's initial
    portfolio dollar-for-dollar.

    The broker is also seeded with a recurring ACH for the scenario's
    monthly withdrawal amount so withdrawal cycles can use it without
    a separate setup step.
    """
    # Build a one-shot market simulator just to read start-date prices.
    market = MarketSimulator(
        start=scenario.sim_params.start_date,
        end=scenario.sim_params.end_date,
    )
    start = scenario.sim_params.start_date
    starting_balance = scenario.sim_params.starting_balance_usd
    cash_buffer = (
        scenario.sim_params.initial_cash_usd
        if scenario.sim_params.initial_cash_usd is not None
        else Decimal("4000.00")
    )
    sgov_target = (
        scenario.sim_params.initial_sgov_buffer_usd
        if scenario.sim_params.initial_sgov_buffer_usd is not None
        else Decimal("72000.00")
    )
    deployable = starting_balance - cash_buffer - sgov_target

    # Phase-1 weights (matches IPMS engine._PHASE_1_TARGETS).
    phase1_targets: dict[str, Decimal] = {
        "PYLD": Decimal("0.25"),
        "JPIE": Decimal("0.15"),
        "FBCG": Decimal("0.30"),
        "AVUV": Decimal("0.30"),
        # GBIL is pre-registered at 0% in Phase 1; seed a zero position
        # so the IRAPM rebalancer doesn't see "new symbol mid-flight"
        # when Phase 2 raises its weight.
        "GBIL": Decimal("0"),
    }

    broker = SyntheticBroker(
        account_id="SIM-HARNESS",
        initial_cash=cash_buffer,
        clock=clock,
        expected_account_id="SIM-HARNESS",
    )

    for symbol, weight in phase1_targets.items():
        target_dollars = deployable * weight
        price = market.price_on(symbol, start)
        if target_dollars > 0:
            shares = (target_dollars / price).quantize(
                Decimal("0.0001"), rounding="ROUND_DOWN"
            )
        else:
            shares = Decimal("0")
        broker.seed_position(
            symbol=symbol,
            quantity=shares,
            market_price=price,
        )

    # SGOV buffer. The IPMS engine handles pre-launch SGOV with a
    # synthetic $100 price; we mirror that here so behavior matches
    # if the scenario start date predates 2020-05-26.
    sgov_first = market.first_date(BUFFER_SYMBOL)
    if start < sgov_first:
        sgov_price = Decimal("100.00")
    else:
        sgov_price = market.price_on(BUFFER_SYMBOL, start)
    sgov_qty = (sgov_target / sgov_price).quantize(
        Decimal("0.0001"), rounding="ROUND_DOWN"
    )
    broker.seed_position(
        symbol=BUFFER_SYMBOL,
        quantity=sgov_qty,
        market_price=sgov_price,
    )

    # Recurring ACH for monthly withdrawal.
    broker.seed_recurring_ach(
        amount_dollars=scenario.sim_params.initial_monthly_withdrawal_usd,
        destination_reference="HARNESS-ACH",
        next_scheduled_date=None,  # cycle scheduler will set this on first withdrawal
    )

    return broker


# ============================================================================
# THE DAILY HOOK
# ============================================================================

# IRAPM weekly cycles fire on Wednesdays (weekday() == 2) per spec
# §9.6 "Time and scheduling interface", which fixes the default
# cycle_schedule at Wednesday 10:00 ET. Wednesday is not arbitrary:
# it satisfies §9.6.1 settlement separation — a Wed-cadence SELL
# settles T+1 (Thu), giving the IBKR ACH pull on the 15th the
# 2-trading-day settlement runway the spec requires. The cycle's
# own calendar predicates further gate whether the withdrawal step,
# annual review step, or Phase-2 reallocation step run within that
# Wednesday's cycle (see cycle.py:_is_scheduled_withdrawal_day and
# spec §6.5.1). The harness's job is to call the cycle on the right
# day; the cycle decides what work it does.
#
# SINGLE SOURCE OF TRUTH: every weekday-dependent decision in this
# module (dispatch in make_irapm_daily_hook, cycle-count assertion
# in check_expectations) must derive from this constant. Do NOT
# hardcode `2` or "Wednesday" elsewhere. If the spec changes the
# cycle weekday, only this constant should need to be updated.
_WEEKLY_CYCLE_WEEKDAY = 2  # Wednesday — per spec §9.6


# Map weekday() integer to human-readable name. Used by the
# expectations checker to render the cycle-count assertion message
# in terms of whichever weekday the harness is currently dispatching
# (so the failure message stays correct if _WEEKLY_CYCLE_WEEKDAY
# changes). Kept module-private since it's a one-call helper.
_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


# ----------------------------------------------------------------------------
# Cycle-failure handling
# ----------------------------------------------------------------------------
#
# A scenario run iterates the daily hook once per simulated day, and
# the hook dispatches one of two IRAPM cycle types. A weekly cycle has
# THREE possible outcomes, not two:
#
#   1. Success-clean: cycle.py reaches append_cycle_log with
#      execution_halted=False. Real work was done; counter resets.
#
#   2. Success-with-halt: cycle.py reaches append_cycle_log with
#      execution_halted=True. The cycle made a controlled stop
#      (e.g., zero-quantity order rejection, account_id mismatch)
#      and logged its own halt reason. From the operator's
#      perspective this is a FAILURE — same work was prevented —
#      and consecutive identical halts indicate a stuck state.
#
#   3. Raise: cycle.py raises before reaching append_cycle_log
#      (e.g., a pydantic validator inside execute_plan rejects an
#      input). The harness writes a synthetic halted record so the
#      cycle log doesn't have silent gaps.
#
# Daily-token cycles only have outcomes 1 and 3 (they don't append
# to cycle.jsonl; they have their own minimal token-check log).
#
# Naive policies are bad in opposite directions:
#
#   - Re-raise on the first exception: a single transient failure in
#     year 8 of 20 aborts the run, losing every later cycle's output.
#     Operator-hostile for diagnosis: you can only see one bug at a
#     time, and only the earliest-occurring one.
#
#   - Swallow forever (a tempting default): a stuck retry loop
#     (e.g., a pydantic validator rejecting a permanently-bad input
#     that every subsequent cycle re-attempts) silently burns
#     hundreds or thousands of cycles. The cycle log shows missing
#     entries with no halt_reason, and the operator has to dig
#     through stderr tracebacks to find out anything went wrong.
#
# The harness uses TWO complementary mechanisms:
#
#   A. Log-as-halted-record (ALWAYS, for outcome 3). Every cycle
#      exception writes a synthetic record into cycle.jsonl with
#      execution_halted=True and halt_reason="<exception class>: 
#      <first line of message>". This guarantees the cycle log
#      never has silent gaps; the expectations checker sees real
#      halts, not phantom count mismatches; forensic analysis only
#      needs the log, not stderr.
#
#   B. Bail-after-N-consecutive-identical (PATHOLOGY DETECTION,
#      for outcomes 2 and 3 together). The hook tracks the signature
#      of the most-recent failure per cycle type. If the same
#      signature fires _MAX_CONSECUTIVE_IDENTICAL_FAILURES times in
#      a row, the hook raises HarnessFailureError, which propagates
#      up through ipms_run() and terminates the scenario. A clean
#      success (outcome 1) of the same cycle type resets the
#      counter, so isolated transient failures don't accumulate
#      toward the bail threshold.
#
# Two design choices worth flagging:
#
# Per-cycle-type tracking. The bail counter is keyed by cycle_type
# ("weekly" or "daily-token"), not globally. The rationale: a
# successful daily-token cycle (which is just a token read; doesn't
# exercise the decision pipeline) tells us NOTHING about whether the
# weekly pipeline is healthy. If we shared the counter, then in the
# common pattern of one weekly failure per week between six healthy
# daily-token cycles, the daily successes would reset the counter
# and the weekly retry loop would never bail. (This is the actual
# bug an earlier iteration of this code had — the counter was global
# and the bail never fired.) Per-cycle-type fixes that.
#
# In-cycle halts count toward bail. Outcome 2 (cycle.py reaches
# append_cycle_log with execution_halted=True) is treated as a
# failure for bail purposes, with the cycle's recorded halt_reason
# as the signature. The detection mechanism is reading the cycle
# log incrementally after each weekly cycle returns: if a new record
# was appended and it has execution_halted=True, that's outcome 2.
# This requires reading cycle.jsonl from disk each weekly cycle, but
# the read is bounded (just the tail bytes since the last call) and
# the cost is negligible compared to the cycle pipeline itself.
#
# Known limitation: if run_weekly_cycle returns normally WITHOUT
# appending to cycle.jsonl (currently happens only on broker input
# refresh failure — the early `return` near the top of
# run_weekly_cycle), the bail counter sees "no new record" and
# treats it as a clean success. A permanently-broken broker would
# therefore never trigger the bail. In the synthetic-harness
# context this is theoretical; in production it would be caught by
# operational alerting at a different layer.

_MAX_CONSECUTIVE_IDENTICAL_FAILURES = 3


class HarnessFailureError(RuntimeError):
    """Raised by the daily hook when the same failure signature has
    occurred _MAX_CONSECUTIVE_IDENTICAL_FAILURES times in a row for a
    given cycle type.

    The harness intentionally allows individual cycle failures to be
    logged-and-continued (preserving the rest of the run for
    diagnosis), but a stuck retry loop is a pathological state that
    will keep producing identical failures until the simulated
    calendar runs out. Bailing protects operator time and surfaces
    the underlying bug at the first opportunity.

    run_scenario()'s existing `except Exception` block catches this
    and returns a non-zero exit code; the IPMS run output produced
    before the bail is preserved, and (if persist_state is set) so
    is the partial harness state directory.
    """
    pass


class CycleFailureTracker:
    """Encapsulates the harness's failure-handling state: per-cycle-type
    consecutive-failure counters, cycle.jsonl read offset, and the bail
    threshold check.

    Lives outside make_irapm_daily_hook's closure so the logic is
    testable in isolation. The closure simply instantiates one and
    delegates to record_exception() / record_normal_return() per cycle.

    State per instance:
      - One {last_signature, consecutive_count} dict per cycle_type
        ('weekly' and 'daily-token'). See the module-level comment
        block above _MAX_CONSECUTIVE_IDENTICAL_FAILURES for the
        per-cycle-type rationale.
      - One integer offset into cycle.jsonl, used to read only the
        newly-appended bytes after each weekly cycle returns. This
        is how we detect outcome 2 (success-with-halt) without
        re-parsing the entire log on every cycle.

    Invariants:
      - record_exception always advances the log offset past whatever
        was just written (by cycle.py or by our synthetic-halt write),
        so the next normal-return read doesn't see this cycle's halt
        record as if it were freshly appended by the next cycle.
      - record_normal_return on the weekly path always advances the
        log offset to the current file size, regardless of whether a
        halt was detected. Otherwise consecutive normal returns where
        only the first wrote a halt would mistakenly read the same
        halt record twice.
    """

    def __init__(
        self, paths: Paths,
        max_consecutive: int = _MAX_CONSECUTIVE_IDENTICAL_FAILURES,
    ) -> None:
        self._paths = paths
        self._max_consecutive = max_consecutive
        self._tracker: dict[str, dict[str, Any]] = {
            "weekly":      {"last_signature": None, "consecutive_count": 0},
            "daily-token": {"last_signature": None, "consecutive_count": 0},
        }
        self._previous_log_size = 0

    # -- public introspection (test-friendly; not used by the closure)

    def counter(self, cycle_type: str) -> int:
        """Return current consecutive_count for `cycle_type`. Useful
        in tests for asserting intermediate state."""
        return self._tracker[cycle_type]["consecutive_count"]

    def last_signature(self, cycle_type: str) -> Optional[str]:
        """Return current last_signature for `cycle_type`."""
        return self._tracker[cycle_type]["last_signature"]

    # -- public mutators (called by the closure per cycle)

    def record_exception(
        self, today: date, cycle_type: str, exc: BaseException,
    ) -> None:
        """Outcome 3 handler: cycle.py raised. Write a synthetic
        halted record to cycle.jsonl, advance log offset, then update
        the consecutive-failure counter and bail if at threshold.

        The synthetic record write is best-effort: if it fails, log
        and continue — we never want diagnostic logging to mask the
        real exception by raising a secondary one.
        """
        logger.exception("%s cycle raised at %s", cycle_type, today)
        try:
            _append_halted_cycle_record(self._paths, today, cycle_type, exc)
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to append halted-cycle record at %s", today,
            )
        # Advance log offset past whatever was just written. Otherwise
        # the next weekly cycle's normal-return path could read this
        # exception's record and mistake it for an in-cycle halt of
        # the NEXT cycle.
        try:
            self._previous_log_size = self._paths.cycle_log().stat().st_size
        except OSError:
            pass
        self._bail_if_threshold(
            cycle_type, _exception_signature(exc), today,
        )

    def record_normal_return(
        self, today: date, cycle_type: str,
    ) -> None:
        """Outcome 1 or 2 handler: cycle.py returned without raising.

        For weekly cycles: check whether cycle.py appended a record
        since the last call. If so and execution_halted=True, treat
        as outcome 2 and bail-check with the cycle's halt_reason. If
        the appended record is clean (or nothing was appended at all),
        reset the counter.

        For daily-token cycles: always treat as a clean success.
        Daily-token cycles don't write to cycle.jsonl, so there's
        nothing to check, and the absence of a write here doesn't
        imply a problem.
        """
        tracker = self._tracker[cycle_type]

        if cycle_type == "weekly":
            rec = self._read_last_appended_cycle_record()
            if rec is not None and rec.get("execution_halted"):
                signature = (
                    f"in_cycle_halt: {rec.get('halt_reason') or 'unknown'}"
                )
                self._bail_if_threshold(cycle_type, signature, today)
                return
            # Outcome 1 (clean) or the cycle returned without writing
            # (transient broker failure, etc.). Reset.
            tracker["last_signature"] = None
            tracker["consecutive_count"] = 0
        else:
            # daily-token: clean success.
            tracker["last_signature"] = None
            tracker["consecutive_count"] = 0

    # -- internals

    def _bail_if_threshold(
        self, cycle_type: str, signature: str, today: date,
    ) -> None:
        """Update the per-cycle-type counter with a new failure
        `signature` and raise HarnessFailureError if the consecutive
        count reaches `self._max_consecutive`.
        """
        tracker = self._tracker[cycle_type]
        if signature == tracker["last_signature"]:
            tracker["consecutive_count"] += 1
        else:
            tracker["last_signature"] = signature
            tracker["consecutive_count"] = 1
        if tracker["consecutive_count"] >= self._max_consecutive:
            raise HarnessFailureError(
                f"Bailing after {tracker['consecutive_count']} "
                f"consecutive identical {cycle_type} cycle failures. "
                f"Signature: {signature}. Last failing date: {today}. "
                f"See stderr tracebacks and cycle.jsonl halted records "
                f"for diagnostic detail."
            )

    def _read_last_appended_cycle_record(self) -> Optional[dict]:
        """Read bytes appended to cycle.jsonl since the previous call
        and return the LAST JSON record parsed from them, or None if
        the file is absent, didn't grow, or grew but contained no
        parseable JSON.

        Side effect: advances self._previous_log_size to the current
        file size, so subsequent calls see only newer appends.
        Reading is O(delta), bounded by the size of one cycle's
        appended record (typically <1KB), not O(total file size).
        """
        log_path = self._paths.cycle_log()
        if not log_path.exists():
            return None
        try:
            new_size = log_path.stat().st_size
        except OSError:
            return None
        if new_size <= self._previous_log_size:
            # No growth. Update tracker to current size in case the
            # file was truncated externally (defensive; shouldn't
            # happen for an append-only log).
            self._previous_log_size = new_size
            return None
        try:
            with log_path.open("rb") as f:
                f.seek(self._previous_log_size)
                new_bytes = f.read()
        except OSError:
            return None
        self._previous_log_size = new_size
        text = new_bytes.decode("utf-8", errors="replace")
        last_line = next(
            (ln for ln in reversed(text.splitlines()) if ln.strip()),
            None,
        )
        if last_line is None:
            return None
        try:
            return json.loads(last_line)
        except json.JSONDecodeError:
            return None


def _exception_signature(exc: BaseException) -> str:
    """Return a stable identity string for `exc` suitable for
    detecting "the same exception happened twice."

    Uses the exception class name plus the FIRST LINE of str(exc).
    Multi-line messages (pydantic ValidationError is the canonical
    example) usually have the most identifying information on the
    first line; subsequent lines often contain instance-specific
    detail (field paths, input values) that we WANT to be part of
    the signature to distinguish "validator A rejected field X"
    from "validator A rejected field Y".

    Conversely, using the full str(exc) would over-discriminate:
    pydantic includes the offending input value in its message, so
    "trigger_year=2016 rejected" and "trigger_year=2017 rejected"
    would look like different errors even though they're the same
    bug. First-line-only strikes the right balance for the failure
    modes we expect to see.
    """
    msg = str(exc)
    first_line = msg.splitlines()[0] if msg else ""
    return f"{type(exc).__name__}: {first_line}"


def _append_halted_cycle_record(
    paths: Paths,
    today: date,
    cycle_type: str,
    exc: BaseException,
) -> None:
    """Append a synthetic halted-cycle record to cycle.jsonl.

    Schema matches what cycle.py's append_cycle_log call produces,
    so check_expectations() and any other downstream cycle-log
    consumers don't have to special-case harness-written records.
    Fields that aren't recoverable post-exception use safe defaults:

      - cycle_id: pulled from cycle_attempt.json if cycle.py reached
        begin_cycle() before raising; otherwise a fresh uuid4().
      - phase, cb_state, income_state: best-effort read from the
        on-disk operating state. The state file may or may not have
        been updated this cycle (cycle.py writes it at step 9, AFTER
        decision but BEFORE action_layer); we report whatever is on
        disk as the latest-known state, which is still informative.
      - lookback_status: "UNAVAILABLE" (we didn't compute one).
      - lookback_value: null.
      - plan_entry_count: 0 (we never built a plan).
      - is_restart, is_scheduled_withdrawal_day, is_annual_review_day:
        False (we have no way to determine them post-exception).
      - execution_halted: True (the whole point of this record).
      - halt_reason: exception signature per _exception_signature().
    """
    # Try to recover the cycle UUID from the attempt file. If reading
    # fails for any reason (file absent, parse error, partial write),
    # generate a fresh one — the cycle_id field's contract is just
    # "unique identifier for this cycle attempt," not "matches the
    # cycle.py-assigned UUID."
    cycle_id = str(uuid.uuid4())
    if paths.cycle_attempt_file.exists():
        try:
            with paths.cycle_attempt_file.open("r", encoding="utf-8") as f:
                attempt_data = json.load(f)
            recovered = attempt_data.get("cycle_uuid")
            if recovered:
                cycle_id = str(recovered)
        except (OSError, json.JSONDecodeError):
            pass  # Fall through to the fresh UUID we already assigned.

    # Best-effort phase/cb/income from on-disk state. If load fails
    # (state file absent or unreadable), report "UNKNOWN" sentinels;
    # downstream consumers can filter these out if needed.
    phase = "UNKNOWN"
    cb_state = "UNKNOWN"
    income_state = "UNKNOWN"
    operational_pause = False
    withdrawal_capacity_exhausted = False
    try:
        state = load_operating_state(paths)
        phase = state.phase.value
        cb_state = state.cb_machine.state.value
        income_state = state.income_state.value
        operational_pause = state.operational_pause.paused
        withdrawal_capacity_exhausted = state.withdrawal_capacity_exhausted
    except Exception:  # noqa: BLE001 - best-effort, never block logging
        pass

    from persistence import append_cycle_log
    append_cycle_log(paths, {
        "cycle_id": cycle_id,
        "cycle_type": cycle_type,
        "decision_clock": datetime.combine(today, datetime.min.time()).isoformat(),
        "phase": phase,
        "cb_state": cb_state,
        "income_state": income_state,
        "operational_pause": operational_pause,
        "withdrawal_capacity_exhausted": withdrawal_capacity_exhausted,
        "lookback_status": "UNAVAILABLE",
        "lookback_value": None,
        "plan_entry_count": 0,
        "is_restart": False,
        "is_scheduled_withdrawal_day": False,
        "is_annual_review_day": False,
        "execution_halted": True,
        "halt_reason": _exception_signature(exc),
    })



def make_irapm_daily_hook(
    cycle_config: CycleConfig,
    scenario: ScenarioConfig,
    detector: MirroredMockTokenDetector,
):
    """Return a callable matching ipms.engine.DailyHook signature.

    Each simulated day, the hook:
      1. Syncs the AdvancingClock to today's date (the harness clock
         lives in cycle_config but IPMS owns the run loop, so the
         hook is responsible for keeping them in sync).
      2. Applies scenario.market_overrides for today by setting prices
         on the synthetic broker (overrides win over IPMS-derived prices).
      3. Reconciles IPMS's market prices into the synthetic broker for
         every symbol the broker holds, so IRAPM sees current prices.
      4. Applies scenario.tokens events for today by mutating the
         MockTokenDetector's pre-set counts.
      5. Calls broker.evolve_pending_state() so SETTLEMENT_CALENDAR_DAYS
         settlement and recurring ACH disbursement progress.
      6. Dispatches the appropriate cycle: weekly on the configured
         _WEEKLY_CYCLE_WEEKDAY (default Wednesday per spec §9.6),
         daily-token on every other day. The cycle's internal calendar
         predicates decide which subroutines run within a weekly cycle.
      7. Mirrors the box's token observation to the peer slot.
      8. Reconciles the broker's resulting state back into a fresh
         IPMS portfolio dict and returns it.

    The hook is the IPMS↔IRAPM bridge. Bugs in this function manifest
    as a divergence between what IPMS thinks the portfolio looks like
    and what the broker actually holds. The reconciliation step is
    where that synchronization happens; see _broker_to_portfolio for
    the per-symbol math.
    """
    broker: SyntheticBroker = cycle_config.broker  # type: ignore[assignment]
    clock: AdvancingClock = cycle_config.clock  # type: ignore[assignment]
    paths: Paths = cycle_config.paths  # type: ignore[assignment]

    # All bail-counter and cycle-log-offset state lives in this
    # tracker. See CycleFailureTracker docstring for the per-cycle-type
    # design rationale, and the module-level comment block above
    # _MAX_CONSECUTIVE_IDENTICAL_FAILURES for the policy.
    failure_tracker = CycleFailureTracker(paths)

    # Index tokens and overrides by date for O(1) per-day dispatch.
    tokens_by_date: dict[date, list[TokenEvent]] = {}
    for t in scenario.tokens:
        tokens_by_date.setdefault(t.date, []).append(t)
    overrides_by_date: dict[date, list[MarketOverride]] = {}
    for o in scenario.market_overrides:
        overrides_by_date.setdefault(o.date, []).append(o)

    def hook(
        today: date,
        portfolio: dict,
        market: MarketSimulator,
        collector,  # StateCollector; unused by harness today
    ) -> dict:
        # 1) Sync clock. IRAPM AdvancingClock uses set_to(), not
        # set_date() — see clock.py at repo root for the method names.
        clock.set_to(today)

        # 2) Apply market overrides (these win over IPMS's prices).
        applied_overrides: set[str] = set()
        for ov in overrides_by_date.get(today, []):
            broker.set_market_price(ov.symbol, ov.price)
            applied_overrides.add(ov.symbol)

        # 3) Reconcile IPMS prices into the broker for everything the
        # broker holds, except symbols already overridden. We update
        # via set_market_price() so the broker's get_prices() returns
        # current values for IRAPM's decision and execution paths.
        for symbol, pos in portfolio["assets"].items():
            if symbol in applied_overrides:
                continue
            price = pos["market_price"]
            broker.set_market_price(symbol, price)
        # Buffer too.
        if BUFFER_SYMBOL not in applied_overrides:
            broker.set_market_price(BUFFER_SYMBOL, portfolio["sgov_buffer_price"])

        # 4) Apply token events for today.
        for t in tokens_by_date.get(today, []):
            _apply_token_event(detector, t)

        # 5) Settlement and ACH progression.
        # The broker must be connected for evolve_pending_state to run;
        # it touches account state via the same fields that get queried
        # under connect(). We keep the broker disconnected outside the
        # cycle's own connect/disconnect lifecycle, but evolve doesn't
        # need a connection per its docstring — it's a simulator-only
        # method. Call it directly.
        broker.evolve_pending_state()

        # 6) Dispatch cycle. Failure handling per the comment block
        # above _MAX_CONSECUTIVE_IDENTICAL_FAILURES: exceptions are
        # logged + written as halted records; in-cycle halts on the
        # weekly path are detected by checking the appended record;
        # consecutive identical failures of the same cycle type bail
        # via HarnessFailureError.
        if today.weekday() == _WEEKLY_CYCLE_WEEKDAY:
            try:
                run_weekly_cycle(cycle_config)
            except Exception as exc:  # noqa: BLE001
                failure_tracker.record_exception(today, "weekly", exc)
            else:
                failure_tracker.record_normal_return(today, "weekly")
        else:
            try:
                run_daily_token_cycle(cycle_config)
            except Exception as exc:  # noqa: BLE001
                failure_tracker.record_exception(today, "daily-token", exc)
            else:
                failure_tracker.record_normal_return(today, "daily-token")

        # 7) Mirror token observation to peer slot for AND semantics.
        detector.mirror_after_cycle()

        # 8) Reconcile broker state back into IPMS portfolio dict.
        return _broker_to_portfolio(broker, portfolio, market, today)

    return hook


def _apply_token_event(
    detector: MirroredMockTokenDetector,
    event: TokenEvent,
) -> None:
    """Translate a scenario TokenEvent into a MockTokenDetector
    set_counts call.

    The four supported actions map to the four operationally relevant
    token states: full-presence (2/0), Phase-3-latch (0/0),
    stop-income-paused (2/1), and resume-income (2/0). These cover
    every scenario the harness expects to drive pre-CL260.
    """
    if event.action == "insert":
        detector.set_counts(phase3=2, stopincome=0)
    elif event.action == "remove":
        detector.set_counts(phase3=0, stopincome=0)
    elif event.action == "stop_income":
        detector.set_counts(phase3=2, stopincome=1)
    elif event.action == "resume_income":
        detector.set_counts(phase3=2, stopincome=0)
    else:
        raise ValueError(f"Unknown token event action: {event.action!r}")


def _broker_to_portfolio(
    broker: SyntheticBroker,
    prior_portfolio: dict,
    market: MarketSimulator,
    today: date,
) -> dict:
    """Rebuild the IPMS-style portfolio dict from the synthetic broker's
    current state.

    The IPMS portfolio dict shape (see IPMS engine._seed_initial_portfolio
    for the canonical definition):

        {
          "assets": {symbol: {quantity, market_price, market_value,
                              weight, asset_class}},
          "cash_value": Decimal,
          "sgov_buffer_value": Decimal,
          "sgov_buffer_quantity": Decimal,
          "sgov_buffer_price": Decimal,
          "sgov_pre_launch": bool,
        }

    Sources:
      - Quantities and prices come from broker.get_positions() (which
        we briefly connect/disconnect to call).
      - Cash comes from broker.get_account_summary().total_cash_value
        (combines settled + unsettled).
      - SGOV buffer position is pulled out of the assets dict because
        IPMS treats it specially.
      - 'weight' and 'asset_class' are carried forward from
        prior_portfolio so the snapshot formatter can preserve them
        (these are Phase-1-target metadata, not derived from broker
        state).

    Pre-launch SGOV handling matches IPMS engine logic: if today is
    still before SGOV's first market date, hold the synthetic price
    and quantity unchanged; on the launch boundary, the IPMS engine
    rebases. The hook runs AFTER engine._update_portfolio_prices(),
    so the rebase has already happened and we can read the post-rebase
    pre_launch flag off prior_portfolio.

    Symbols the broker now has but prior_portfolio didn't (e.g., a new
    position from a Phase-2 transition) get added with weight=0 and
    asset_class='growth' as a safe default — the next rebalance/
    snapshot will compute the correct values.
    """
    asset_class_defaults = {
        "PYLD": "fixed_income",
        "JPIE": "fixed_income",
        "FBCG": "growth",
        "AVUV": "growth",
        "GBIL": "growth",
    }

    # Pull broker positions (requires temporary connection).
    broker.connect()
    try:
        positions = broker.get_positions()
        account = broker.get_account_summary()
    finally:
        broker.disconnect()

    by_symbol = {p.symbol: p for p in positions}

    new_assets: dict[str, dict] = {}
    for symbol, prior in prior_portfolio["assets"].items():
        if symbol == BUFFER_SYMBOL:
            continue  # buffer handled separately below
        if symbol in by_symbol:
            pos = by_symbol[symbol]
            qty = pos.quantity
            price = pos.market_value / qty if qty != 0 else prior["market_price"]
        else:
            # Position closed in broker but still in prior dict; zero it out
            # but keep the slot so weight/asset_class metadata persists.
            qty = Decimal("0")
            price = prior["market_price"]
        new_assets[symbol] = {
            "quantity": qty,
            "market_price": price,
            "market_value": qty * price,
            "weight": prior["weight"],
            "asset_class": prior["asset_class"],
        }

    # Any broker symbols not in prior (new positions from rebalance/swing)?
    for symbol, pos in by_symbol.items():
        if symbol == BUFFER_SYMBOL:
            continue
        if symbol in new_assets:
            continue
        qty = pos.quantity
        price = pos.market_value / qty if qty != 0 else Decimal("0")
        new_assets[symbol] = {
            "quantity": qty,
            "market_price": price,
            "market_value": qty * price,
            "weight": Decimal("0"),  # will be set by next IPMS snapshot
            "asset_class": asset_class_defaults.get(symbol, "growth"),
        }

    # SGOV buffer.
    sgov_pos = by_symbol.get(BUFFER_SYMBOL)
    if sgov_pos is not None:
        sgov_qty = sgov_pos.quantity
        sgov_price = (
            sgov_pos.market_value / sgov_qty
            if sgov_qty != 0
            else prior_portfolio["sgov_buffer_price"]
        )
    else:
        sgov_qty = prior_portfolio["sgov_buffer_quantity"]
        sgov_price = prior_portfolio["sgov_buffer_price"]
    sgov_value = sgov_qty * sgov_price

    return {
        "assets": new_assets,
        "cash_value": account.total_cash_value,
        "sgov_buffer_value": sgov_value,
        "sgov_buffer_quantity": sgov_qty,
        "sgov_buffer_price": sgov_price,
        "sgov_pre_launch": prior_portfolio.get("sgov_pre_launch", False),
    }


# ============================================================================
# EXPECTATIONS CHECKER
# ============================================================================

def check_expectations(
    result: SimulationResult,
    final_state: OperatingState,
    paths: Paths,
    expectations: Expectations,
) -> tuple[bool, list[str]]:
    """Evaluate end-of-run expectations.

    Returns (all_passed, failure_messages). Each failed expectation
    becomes one human-readable line in failure_messages; passed
    expectations don't appear (the caller can subtract from the total
    if it wants a 'N of M passed' summary).

    The function is intentionally lenient about missing data: if the
    cycle log doesn't exist (run aborted before any cycle fired), CB
    and withdrawal expectations register as failures with explicit
    'no cycle log' messages rather than crashing.
    """
    failures: list[str] = []

    if expectations.final_phase is not None:
        if final_state.phase.value != expectations.final_phase:
            failures.append(
                f"final_phase: expected {expectations.final_phase}, "
                f"got {final_state.phase.value}"
            )

    if expectations.final_balance_dollars_at_least is not None:
        terminal_aum = result.terminal_aum or Decimal("0")
        if terminal_aum < expectations.final_balance_dollars_at_least:
            failures.append(
                f"final_balance_dollars_at_least: expected >= "
                f"{expectations.final_balance_dollars_at_least}, "
                f"got {terminal_aum}"
            )

    # CB and withdrawal checks read the cycle log JSONL.
    cycle_log_path = paths.cycle_log()
    cycle_records = _read_jsonl_records(cycle_log_path)

    if expectations.cb1_triggered_at_least_once is not None:
        saw_cb1 = any(r.get("cb_state") == "CB1" for r in cycle_records)
        if expectations.cb1_triggered_at_least_once and not saw_cb1:
            failures.append("cb1_triggered_at_least_once: no cycle ever recorded CB1")
        if not expectations.cb1_triggered_at_least_once and saw_cb1:
            failures.append("cb1_triggered_at_least_once: expected no CB1 but saw at least one")

    if expectations.cb2_triggered_at_least_once is not None:
        saw_cb2 = any(r.get("cb_state") == "CB2" for r in cycle_records)
        if expectations.cb2_triggered_at_least_once and not saw_cb2:
            failures.append("cb2_triggered_at_least_once: no cycle ever recorded CB2")
        if not expectations.cb2_triggered_at_least_once and saw_cb2:
            failures.append("cb2_triggered_at_least_once: expected no CB2 but saw at least one")

    if expectations.no_failed_cycles is True:
        halted = [r for r in cycle_records if r.get("execution_halted")]
        if halted:
            failures.append(
                f"no_failed_cycles: {len(halted)} cycle(s) halted "
                f"(first reason: {halted[0].get('halt_reason')})"
            )
        # Also assert the cycle log isn't vacuously empty. A weekly
        # cycle that raises inside decide() never reaches the
        # append_cycle_log call, so an empty log can hide complete
        # failures from the 'no halted records' check. Count the
        # cycle-weekdays in the run window and require at least that
        # many weekly-cycle records.
        #
        # IMPORTANT range semantic: the IPMS engine fires the daily
        # hook on every day AFTER start_date (the loop is
        # `while clock.today() < end_date: advance_one_day(); hook()`)
        # so start_date itself never gets a hook call — it's the seed
        # moment, not a cycle moment. The cycle-weekday count must
        # reflect this: cycle-weekdays in (start_date, end_date],
        # exclusive on the left, inclusive on the right.
        #
        # Weekday derives from _WEEKLY_CYCLE_WEEKDAY (the dispatch
        # constant at module scope) so this assertion stays correct
        # if the spec moves the cycle to a different weekday — there
        # is exactly one place to edit.
        if result.params is not None:
            start = result.params.start_date
            end = result.params.end_date
            from datetime import timedelta
            cycle_weekday_name = _WEEKDAY_NAMES[_WEEKLY_CYCLE_WEEKDAY]
            expected_cycles = 0
            d = start + timedelta(days=1)   # exclusive on the left
            while d <= end:
                if d.weekday() == _WEEKLY_CYCLE_WEEKDAY:
                    expected_cycles += 1
                d += timedelta(days=1)
            weekly_records = [r for r in cycle_records
                              if r.get("cycle_type") == "weekly"]
            if len(weekly_records) < expected_cycles:
                failures.append(
                    f"no_failed_cycles: expected at least {expected_cycles} "
                    f"weekly cycle log entr{'y' if expected_cycles == 1 else 'ies'} "
                    f"(one per {cycle_weekday_name} in window), got "
                    f"{len(weekly_records)}. Missing entries usually indicate "
                    f"a cycle that raised before reaching append_cycle_log \u2014 "
                    f"check stderr for tracebacks."
                )

    if expectations.total_withdrawals_dollars_at_least is not None:
        # Cycle log records "is_scheduled_withdrawal_day"; the actual
        # dollar amount is in the alert log or withdrawal entries.
        # Conservative implementation: count is_scheduled_withdrawal_day
        # entries that didn't halt, multiply by initial monthly amount.
        # A finer reading would parse plan_entry_count entries, but
        # this is the harness's minimum-viable assertion.
        successful_withdrawal_days = [
            r for r in cycle_records
            if r.get("is_scheduled_withdrawal_day") and not r.get("execution_halted")
        ]
        # Read withdrawal amount from final state's schedule.
        schedule = final_state.schedule_state.phase1
        if schedule is None:
            estimated_total = Decimal("0")
        else:
            estimated_total = schedule.i_0_dollars * len(successful_withdrawal_days)
        if estimated_total < expectations.total_withdrawals_dollars_at_least:
            failures.append(
                f"total_withdrawals_dollars_at_least: expected >= "
                f"{expectations.total_withdrawals_dollars_at_least}, "
                f"estimated {estimated_total} from "
                f"{len(successful_withdrawal_days)} withdrawal day(s)"
            )

    return (len(failures) == 0, failures)


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    """Read all JSON lines from `path`. Returns [] if file absent."""
    import json
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ============================================================================
# TOP-LEVEL ENTRYPOINT
# ============================================================================

# Symbols the IRAPM lookback signal needs price data for. Hardcoded
# because Phase-1 growth bucket is FBCG + AVUV; Phase-2/3 don't change
# this set (other growth symbols are buffer-related, not lookback
# inputs). Reading from ruleset would be more general but adds a
# Decimal-typed dependency we don't yet need.
_LOOKBACK_SYMBOLS = ["FBCG", "AVUV"]


def run_scenario(scenario_path: Path) -> int:
    """Top-level entry: load scenario, set up temp state dir, translate
    TSVs, build cycle config, run engine with daily hook, check
    expectations, print summary, return process exit code.

    Exit code: 0 if all expectations passed, 1 otherwise.

    The temp dir is cleaned up automatically; the IPMS run's output
    directory (params.output_dir) is NOT cleaned up — operator can
    inspect it post-run for debugging. To redirect IPMS output into
    the temp dir too, point sim_params.output_dir at the per-run temp
    dir before calling this function (we don't do this here because
    losing the IPMS output after a failure is operator-hostile).
    """
    scenario_path = Path(scenario_path)
    scenario = load_scenario(scenario_path)
    repo_root = Path(__file__).parent

    # Set IPMS_DATA_DIR so IPMS's MarketSimulator resolves price files
    # relative to the harness's repo root rather than the hardcoded
    # Windows path C:/portfolio/data. This is what makes scenarios
    # portable across Windows and WSL invocations without per-platform
    # YAML edits. Only override if the caller hasn't already set it
    # explicitly (so an operator who wants to point at a custom data
    # location can still do so).
    import os
    if "IPMS_DATA_DIR" not in os.environ:
        os.environ["IPMS_DATA_DIR"] = str(repo_root / "data")

    # Auto-resolve output_dir to <repo_root>/runs/ when the scenario
    # omitted it. load_scenario() injected a sentinel for that case;
    # we detect the sentinel here and rewrite to a platform-agnostic
    # location relative to the repo. Scenarios that DO specify
    # output_dir get their value honored unchanged.
    _SENTINEL_OUTPUT_DIR = Path(tempfile.gettempdir()) / "_irapm_harness_output_dir_sentinel"
    if scenario.sim_params.output_dir == _SENTINEL_OUTPUT_DIR:
        scenario.sim_params.output_dir = repo_root / "runs"
        # The runs/ dir may not exist yet; IPMS engine creates it on
        # demand via create_run_directory(). The validator only checks
        # the parent (repo_root), which always exists. No re-validation
        # needed since the new path passes the same checks.

    logging.basicConfig(
        level=getattr(logging, scenario.sim_params.logging_verbosity.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Per-run state directory. By default this lives under a
    # tempfile.TemporaryDirectory that auto-cleans when run_scenario
    # returns — fine for routine smoke tests where the IPMS output
    # files are the only artifact worth keeping. For baseline / debug
    # runs you want the cycle_log.jsonl, state.json, token logs, etc.
    # for forensic inspection: set `persist_state` on the scenario to
    # land them next to the IPMS output under <output_dir>/<run_name>/
    # _harness_state/. Default False keeps the cheap auto-clean path.
    persist_state = getattr(scenario, "persist_state", False)

    def _run_body(tmpdir: Path) -> int:
        state_dir = tmpdir / "state"
        data_dir = state_dir.parent / "data"  # cycle.py resolves data
                                              # as paths.state_dir.parent/data
        state_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        # Translate TSVs into temp data dir.
        translate_data_tsvs(
            src_dir=repo_root / "data",
            dst_dir=data_dir,
            symbols=_LOOKBACK_SYMBOLS,
        )

        # Set up persistence layer.
        paths = Paths(state_dir=state_dir)
        paths.ensure_dirs()

        # Clock. The IRAPM AdvancingClock takes `start` (not `start_date`)
        # and exposes `set_to()` (not `set_date()`) — see clock.py at
        # repo root. The harness uses this clock, not ipms.clock's.
        clock = AdvancingClock(start=scenario.sim_params.start_date)

        # Token detector (wrapped for single-box mirror mode).
        base_detector = MockTokenDetector(phase3_count=2, stopincome_count=0)
        detector = MirroredMockTokenDetector(base_detector, paths)

        # Build CycleConfig and Ruleset.
        cycle_config, ruleset = build_cycle_config(
            scenario=scenario,
            paths=paths,
            clock=clock,
            repo_root=repo_root,
            detector=detector,
        )

        # Seed initial operating state.
        initial_state = new_initial_state(
            ruleset=ruleset,
            box_id="harness-box",
            ipv4_last_octet=10,
        )
        save_operating_state(paths, initial_state)

        # Build the daily hook.
        hook = make_irapm_daily_hook(cycle_config, scenario, detector)

        # Run IPMS with the hook.
        logger.info("Starting scenario %r", scenario.name)
        try:
            sim_result = ipms_run(scenario.sim_params, daily_hook=hook)
        except Exception:
            logger.exception("IPMS run aborted")
            return 1

        # Load final state for expectations evaluation.
        try:
            final_state = load_operating_state(paths)
        except Exception:
            logger.exception("Could not load final operating state")
            return 1

        # Check expectations.
        all_passed, failures = check_expectations(
            sim_result, final_state, paths, scenario.expectations
        )

        # If state persistence was requested, copy harness state into
        # the IPMS output dir BEFORE the temp dir auto-cleans.
        # sim_result.output_dir is <output_dir>/<run_name>/, which
        # exists by now because IPMS created it at run finalize.
        if persist_state and sim_result.output_dir is not None:
            persistent_dst = sim_result.output_dir / "_harness_state"
            try:
                if persistent_dst.exists():
                    shutil.rmtree(persistent_dst)
                shutil.copytree(state_dir, persistent_dst)
                print(f"Harness state preserved at: {persistent_dst}")
            except Exception as e:
                logger.warning("could not preserve harness state: %s", e)

        # Print summary.
        _print_summary(scenario, sim_result, final_state, all_passed, failures)

        return 0 if all_passed else 1

    with tempfile.TemporaryDirectory(
        prefix=f"irapm_harness_{scenario.name}_"
    ) as tmpdir_str:
        return _run_body(Path(tmpdir_str))


def _fmt_dollars(value) -> str:
    """Format a Decimal (or None) as a dollar amount with 2 decimal
    places and thousands separators. Used by the summary printer so
    we don't emit raw Decimal precision like '3234969.34380488' that
    leaks computation artifacts into operator-facing output.

    Returns the empty string for None so the surrounding f-string
    doesn't print 'None'.
    """
    if value is None:
        return ""
    # Quantize to 2 decimal places, then format with thousands
    # separators. ROUND_HALF_EVEN is the default and matches what
    # financial code typically expects.
    quantized = Decimal(value).quantize(Decimal("0.01"))
    # f-string `,` separator works on Decimal in Python 3.10+.
    return f"{quantized:,.2f}"


def _print_summary(
    scenario: ScenarioConfig,
    result: SimulationResult,
    final_state: OperatingState,
    all_passed: bool,
    failures: list[str],
) -> None:
    """Print a concise pass/fail summary to stdout."""
    print("=" * 70)
    print(f"Scenario: {scenario.name}")
    print("=" * 70)
    print(f"Window:           {scenario.sim_params.start_date} → {scenario.sim_params.end_date}")
    print(f"Starting balance: ${_fmt_dollars(scenario.sim_params.starting_balance_usd)}")
    print(f"Terminal AUM:     ${_fmt_dollars(result.terminal_aum)}")
    print(f"Final phase:      {final_state.phase.value}")
    print(f"Final CB state:   {final_state.cb_machine.state.value}")
    print(f"Income state:     {final_state.income_state.value}")
    print(f"Op paused:        {final_state.operational_pause.paused}")
    print(f"Withdrawal cap exhausted: {final_state.withdrawal_capacity_exhausted}")
    print()
    if all_passed:
        print("EXPECTATIONS: ALL PASSED")
    else:
        print(f"EXPECTATIONS: {len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    print("=" * 70)


# ============================================================================
# CLI
# ============================================================================

def _cli() -> int:
    """Argument parser for direct invocation. The IPMS subpackage
    entrypoint (ipms.run_irapm) calls this same function so both
    invocation paths share argument handling."""
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <scenario.yaml>", file=sys.stderr)
        return 2
    return run_scenario(Path(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(_cli())


__all__ = [
    "TokenEvent",
    "MarketOverride",
    "Expectations",
    "ScenarioConfig",
    "load_scenario",
    "translate_data_tsvs",
    "MirroredMockTokenDetector",
    "build_cycle_config",
    "make_irapm_daily_hook",
    "check_expectations",
    "run_scenario",
]
