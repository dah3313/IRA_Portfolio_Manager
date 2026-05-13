"""
lookback_signal.py — Synthetic Growth Lookback computation (§5).

PURPOSE:
    Compute the single scalar signal that drives the CB state machine and
    the Phase 2 opportunistic swing: the fractional change of an
    equal-weighted Growth-bucket price index over the configured lookback
    window, computed from raw price files at every cycle.

DESIGN (per §5.1):
    - Flow-immune: built only from price changes, never from share counts.
    - Stateless: recomputed from scratch each cycle; no persisted index.
    - Allocation-agnostic: equal-weighted across Growth symbols.
    - Interpretable: returns a Decimal fractional change (-0.10 = -10%).
    - Deterministic: identical inputs produce identical outputs.

ALGORITHM (per §5.3):
    1. Load N+1 weekly Adj Close bars per Growth symbol from data files.
    2. Date-align: keep only dates where every symbol has a price.
    3. Staleness gate: latest aligned date must be within
       max_staleness_days of `as_of`.
    4. Coverage gate: aligned date count >= ceil((N+1) * min_coverage_rate).
    5. Per bar, compute per-symbol simple returns; skip the bar if any
       prior price is non-positive.
    6. Equal-weight average per-bar returns to get composite returns.
    7. Compound into a level series anchored at 100.0.
    8. Signal = (L[last] - L[0]) / L[0].

OUTPUT (§5.4):
    LookbackResult with either status=AVAILABLE and a Decimal value, or
    status=UNAVAILABLE. The state model's LookbackSignal mirrors this shape.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from price_data import (
    PriceBar,
    PriceDataError,
    PriceFileMalformed,
    PriceFileMissing,
    load_weekly_adj_close,
)
from state_model import LookbackStatus

logger = logging.getLogger(__name__)


# --- Result shape -----------------------------------------------------------

@dataclass(frozen=True)
class LookbackResult:
    """Computed lookback signal.

    `status == AVAILABLE`: `value` is a Decimal fractional change.
    `status == UNAVAILABLE`: `value` is None; `reason` explains why.

    `bars_used` is the count of aligned bars actually used in the
    computation (informational, surfaced in the weekly summary).
    `latest_bar_date` is the date of the most recent aligned bar
    (informational, used for staleness reporting).
    """

    status: LookbackStatus
    value: Decimal | None
    reason: str | None
    bars_used: int
    latest_bar_date: date | None

    @classmethod
    def unavailable(cls, reason: str, *,
                    latest: date | None = None,
                    bars_used: int = 0) -> "LookbackResult":
        return cls(
            status=LookbackStatus.UNAVAILABLE,
            value=None,
            reason=reason,
            bars_used=bars_used,
            latest_bar_date=latest,
        )

    @classmethod
    def available(cls, value: Decimal, *, bars_used: int,
                  latest: date) -> "LookbackResult":
        return cls(
            status=LookbackStatus.AVAILABLE,
            value=value,
            reason=None,
            bars_used=bars_used,
            latest_bar_date=latest,
        )


# --- Constants used in the algorithm ---------------------------------------

# Presentational anchor (§5.3.4). Output is a ratio so the anchor value
# does not change the result; 100.0 makes the level series readable in
# the weekly summary if ever surfaced.
_LEVEL_ANCHOR: Decimal = Decimal("100.0")


# --- Main computation -------------------------------------------------------

def compute_synthetic_growth_lookback(
    growth_symbols: list[str],
    *,
    data_dir: str | Path,
    lookback_window_weeks: int,
    max_staleness_days: int,
    min_bar_coverage_rate: Decimal,
    as_of: date,
) -> LookbackResult:
    """Compute the Synthetic Growth Lookback signal per §5.3.

    Args:
      growth_symbols: list of symbols that constitute the Growth bucket
        (e.g., ['FBCG', 'AVUV']). Must be non-empty per §5.5; an empty
        list returns UNAVAILABLE rather than raising.
      data_dir: directory containing per-symbol price files.
      lookback_window_weeks: N — the count of weekly bars in the window
        (signal compounds N composite returns; load N+1 bars per symbol).
      max_staleness_days: latest aligned bar must be within this many
        days of `as_of`; otherwise UNAVAILABLE.
      min_bar_coverage_rate: minimum fraction of N+1 expected bars
        that must survive alignment (Decimal in (0, 1]).
      as_of: the cycle's reference date (typically the captured
        decision_clock's date per I16).

    Returns:
      LookbackResult — never raises for data-driven failures; price-file
      missing or malformed conditions yield UNAVAILABLE with a reason
      string. Any other exception is an internal-consistency bug and is
      allowed to propagate.
    """
    # Guard: empty symbol list (§5.5)
    if not growth_symbols:
        return LookbackResult.unavailable("no growth symbols configured")

    # Step 1: load per-symbol bars
    bars_per_symbol: dict[str, list[PriceBar]] = {}
    expected_bars = lookback_window_weeks + 1
    for s in growth_symbols:
        try:
            bars = load_weekly_adj_close(s, data_dir, count=expected_bars)
        except PriceFileMissing as e:
            return LookbackResult.unavailable(f"missing price file: {e}")
        except PriceFileMalformed as e:
            # Malformed is also degrade-gracefully here; the lookback
            # consumer will alert. Per §9.2.2 the spec lists "malformed"
            # as Critical-blocking — but that severity is the alerter's
            # decision when it sees UNAVAILABLE plus this reason. The
            # signal itself simply reports UNAVAILABLE.
            return LookbackResult.unavailable(f"malformed price file: {e}")
        except PriceDataError as e:
            return LookbackResult.unavailable(f"price data error: {e}")
        if not bars:
            return LookbackResult.unavailable(f"no bars for {s}")
        bars_per_symbol[s] = bars

    # Build date_map[date][symbol] = price; keep only fully-aligned dates
    date_map: dict[date, dict[str, Decimal]] = {}
    for s, bars in bars_per_symbol.items():
        for bar in bars:
            slot = date_map.setdefault(bar.bar_date, {})
            slot[s] = bar.adj_close

    aligned_dates = sorted(
        d for d, by_sym in date_map.items()
        if all(sym in by_sym for sym in growth_symbols)
    )

    if not aligned_dates:
        return LookbackResult.unavailable(
            "no dates with all growth symbols priced"
        )

    latest = aligned_dates[-1]

    # Step 2: staleness gate (§5.3.2)
    staleness = (as_of - latest).days
    if staleness > max_staleness_days:
        return LookbackResult.unavailable(
            f"latest aligned bar {latest.isoformat()} is {staleness} days "
            f"stale (max {max_staleness_days})",
            latest=latest,
            bars_used=len(aligned_dates),
        )

    # Step 2 (cont.): coverage gate (§5.3.2)
    # Use math.ceil on the (Decimal * int) product, converted to float
    # for ceil — coverage_rate is a small Decimal like 0.80 and the
    # product is well within float precision.
    minimum_bars = math.ceil(float(min_bar_coverage_rate) * expected_bars)
    if len(aligned_dates) < minimum_bars:
        return LookbackResult.unavailable(
            f"insufficient aligned bars: {len(aligned_dates)} < "
            f"required {minimum_bars} (of {expected_bars} expected)",
            latest=latest,
            bars_used=len(aligned_dates),
        )

    # Step 3: per-bar composite returns (§5.3.3)
    n_symbols = Decimal(len(growth_symbols))
    composite_returns: list[Decimal] = []
    for i in range(1, len(aligned_dates)):
        d_prev = aligned_dates[i - 1]
        d_curr = aligned_dates[i]
        per_sym: list[Decimal] = []
        bar_valid = True
        for s in growth_symbols:
            p_prev = date_map[d_prev][s]
            p_curr = date_map[d_curr][s]
            if p_prev <= 0:
                logger.warning(
                    "non-positive prior price for %s at %s: %s",
                    s, d_curr.isoformat(), p_prev,
                )
                bar_valid = False
                break
            per_sym.append((p_curr / p_prev) - Decimal("1"))
        if bar_valid:
            composite_returns.append(sum(per_sym, Decimal("0")) / n_symbols)

    if not composite_returns:
        return LookbackResult.unavailable(
            "all bars skipped due to non-positive prior prices",
            latest=latest,
            bars_used=len(aligned_dates),
        )

    # Step 4: compound into level series (§5.3.4)
    levels: list[Decimal] = [_LEVEL_ANCHOR]
    for r in composite_returns:
        levels.append(levels[-1] * (Decimal("1") + r))

    # Step 5: signal (§5.3.5)
    signal = (levels[-1] - levels[0]) / levels[0]

    return LookbackResult.available(
        value=signal,
        bars_used=len(aligned_dates),
        latest=latest,
    )


# --- Convenience helpers ----------------------------------------------------

def signal_age_days(latest_bar: date | None, as_of: date) -> int | None:
    """Reporting convenience for the weekly summary: how stale is the
    latest aligned bar? Returns None if no latest bar.

    Note: 0 means same-day; 7 means a fresh weekly bar. The lookback's
    staleness gate uses `max_staleness_days` (default 14, so two missed
    weekly bars).
    """
    if latest_bar is None:
        return None
    return (as_of - latest_bar).days


__all__ = [
    "LookbackResult",
    "compute_synthetic_growth_lookback",
    "signal_age_days",
]
