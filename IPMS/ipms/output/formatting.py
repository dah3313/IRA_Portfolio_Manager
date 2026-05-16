"""
formatting.py — shared sub-cent suppression and value rendering.

Public exports:
    fmt_dollars(value) — render Decimal as 2dp dollar amount string
    fmt_shares(value) — render Decimal as 4dp share quantity string
    fmt_price(value) — render Decimal as 4dp price string
    fmt_weight(value) — render Decimal as 4dp weight fraction string
    fmt_pct(value) — render Decimal as 4dp percentage-as-fraction string
    fmt_bool(value) — render bool as "true"/"false" string
    fmt_date(value) — render date as ISO YYYY-MM-DD string
    fmt_optional(value, fmt_fn) — apply fmt_fn or render empty string for None

See also: IPMS_SPECIFICATION.md §5.7 (sub-cent suppression rules)


SUB-CENT SUPPRESSION POLICY

Per spec §5.7, file output rounds:
  - Dollar amounts → 2 decimals (cents). $166,125.00, never $166125.0000.
  - Share quantities → 4 decimals. 58195.5440, matches IBKR precision.
  - Prices → 4 decimals. Same reasoning.
  - Weights → 4 decimals as fractions (0.2485), not percentages.
  - Rolling returns / pct values → 4 decimals as fractions.

Rounding is purely cosmetic. The simulator's underlying arithmetic
runs at full Decimal precision throughout the run; rounding is
applied only when emitting strings to file output. Terminal output
preserves full precision.

Numbers do NOT get thousand-separator commas in file output —
machine-parsing tooling expects raw numeric tokens. The terminal
formatter (output/terminal.py) does add commas for human readability
because it's human-only.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Optional, TypeVar


T = TypeVar("T")


# Quantize targets cached as module-level constants. Decimal.quantize
# accepts a "shape" Decimal (e.g., Decimal('0.01') for 2dp); using
# named constants makes intent explicit at call sites.
_TWO_DP = Decimal("0.01")
_FOUR_DP = Decimal("0.0001")


def fmt_dollars(value: Decimal) -> str:
    """
    Render a dollar amount to 2 decimal places. ROUND_HALF_UP matches
    standard accounting convention (.005 rounds to .01).

    No thousand-separator commas — file output is machine-parseable.
    The terminal formatter adds commas for human display separately.
    """
    return str(value.quantize(_TWO_DP, rounding=ROUND_HALF_UP))


def fmt_shares(value: Decimal) -> str:
    """
    Render a share quantity to 4 decimal places. Matches IBKR's
    typical reporting precision.
    """
    return str(value.quantize(_FOUR_DP, rounding=ROUND_HALF_UP))


def fmt_price(value: Decimal) -> str:
    """
    Render a price to 4 decimal places. Same precision as shares —
    Yahoo data typically arrives with 2-4 decimals; we normalize to 4.
    """
    return str(value.quantize(_FOUR_DP, rounding=ROUND_HALF_UP))


def fmt_weight(value: Decimal) -> str:
    """
    Render a weight as a 4dp fraction. 0.4459 means 44.59%. The
    formatter does not multiply by 100 — operators reading the file
    can multiply mentally; downstream tooling prefers fractions for
    aggregation.
    """
    return str(value.quantize(_FOUR_DP, rounding=ROUND_HALF_UP))


def fmt_pct(value: Decimal) -> str:
    """
    Render a percentage-as-fraction value. Same as fmt_weight but
    semantically distinct — used for YoY change, rolling returns,
    drawdowns. Negative values render with leading minus.
    """
    return str(value.quantize(_FOUR_DP, rounding=ROUND_HALF_UP))


def fmt_bool(value: bool) -> str:
    """
    Render a boolean as lowercase "true"/"false". Lowercase chosen
    for YAML/JSON compatibility — downstream tools that load these
    files expect canonical lowercase.
    """
    return "true" if value else "false"


def fmt_date(value: date) -> str:
    """ISO format YYYY-MM-DD. Matches Yahoo CSV download convention."""
    return value.isoformat()


def fmt_optional(value: Optional[T], fmt_fn: Callable[[T], str]) -> str:
    """
    Apply `fmt_fn` to `value` if non-None, otherwise return the empty
    string. Used for columns that legitimately may be missing on some
    rows (yoy_change_pct in the first 12 months of a run, halt_reason
    on successful withdrawals).

    Empty cells in tab-separated rows render as adjacent tabs
    (`\\t\\t`) which downstream tools handle correctly. The Markdown
    table renderer displays them as empty cells.
    """
    if value is None:
        return ""
    return fmt_fn(value)
