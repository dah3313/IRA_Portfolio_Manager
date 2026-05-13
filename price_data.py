"""
price_data.py — Local price file reader for the Synthetic Growth Lookback (§9.2).

PURPOSE:
    Read per-symbol weekly Adj Close history from operator-maintained TSV/CSV
    files in a configured data directory. Used solely by lookback_signal.py
    to feed the §5 algorithm; no other module touches price files.

DESIGN:
    - Files are operator-maintained (manual Yahoo Finance refresh, §9.2).
      The reader does not fetch network data and does not write files.
    - File format: header row plus one row per bar. Required columns are
      `Date` (ISO YYYY-MM-DD) and `Adj Close` (decimal). Other columns are
      ignored. Both tab- and comma-separated files are accepted; the
      separator is auto-detected from the header line.
    - Output is a list of (date, Decimal) pairs sorted ascending by date,
      with one bar per weekly cadence in the source file. The lookback
      signal does the alignment across symbols; this module returns
      per-symbol bars only.
    - Failure modes per §9.2.2 are surfaced as PriceDataError subclasses;
      the lookback signal translates them into UNAVAILABLE per §5.3.
    - Decimal discipline (§2.8): prices flow through Decimal(str(...))
      to avoid float artifacts.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


# --- Exception hierarchy ----------------------------------------------------

class PriceDataError(Exception):
    """Base for all price-data failures (§9.2.2)."""


class PriceFileMissing(PriceDataError):
    """The expected price file does not exist."""


class PriceFileMalformed(PriceDataError):
    """The file exists but cannot be parsed (missing column, bad date,
    non-decimal price, encoding issues)."""


# --- Public data shape ------------------------------------------------------

class PriceBar:
    """A single weekly bar. Immutable; equality by (date, adj_close).

    Bars are dataclass-like but spelled as a tiny class so the file has
    no external imports beyond Python stdlib.
    """

    __slots__ = ("bar_date", "adj_close")

    def __init__(self, bar_date: date, adj_close: Decimal) -> None:
        self.bar_date = bar_date
        self.adj_close = adj_close

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PriceBar):
            return NotImplemented
        return (self.bar_date == other.bar_date
                and self.adj_close == other.adj_close)

    def __hash__(self) -> int:
        return hash((self.bar_date, self.adj_close))

    def __repr__(self) -> str:
        return f"PriceBar(date={self.bar_date.isoformat()}, adj_close={self.adj_close})"


# --- Reader -----------------------------------------------------------------

# Required header column names. The reader is tolerant of column ordering
# but strict about presence — these exact strings must appear in the
# header row.
_REQUIRED_DATE_COL = "Date"
_REQUIRED_PRICE_COL = "Adj Close"


def _detect_dialect(first_line: str) -> str:
    """Pick the separator. Tab wins if present in the header; otherwise
    fall back to comma. The format spec only requires one of these two."""
    if "\t" in first_line:
        return "\t"
    return ","


def _parse_decimal(raw: str, *, context: str) -> Decimal:
    """Parse a price string to Decimal, surfacing the field context on
    failure so the operator knows which row is bad."""
    cleaned = raw.strip()
    if not cleaned:
        raise PriceFileMalformed(f"empty value at {context}")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError) as e:
        raise PriceFileMalformed(
            f"non-decimal price {cleaned!r} at {context}: {e}"
        ) from e


def _parse_date(raw: str, *, context: str) -> date:
    """Parse an ISO YYYY-MM-DD date, surfacing context on failure."""
    cleaned = raw.strip()
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError as e:
        raise PriceFileMalformed(
            f"bad date {cleaned!r} at {context}: {e}"
        ) from e


def load_weekly_adj_close(
    symbol: str,
    data_dir: str | Path,
    *,
    count: int | None = None,
) -> list[PriceBar]:
    """Load the most recent weekly Adj Close bars for a symbol.

    Looks for `<symbol>.tsv` first, then `<symbol>.csv` in `data_dir`.
    Returns bars sorted ascending by date.

    If `count` is given, returns only the most recent `count` bars
    (after sort). If the file has fewer than `count` bars, returns
    them all — the lookback signal's coverage check (§5.3.2) decides
    whether the result is sufficient.

    Raises:
      PriceFileMissing: neither .tsv nor .csv exists.
      PriceFileMalformed: file present but unparseable per §9.2.2.
    """
    data_path = Path(data_dir)
    tsv_path = data_path / f"{symbol}.tsv"
    csv_path = data_path / f"{symbol}.csv"

    if tsv_path.exists():
        path = tsv_path
    elif csv_path.exists():
        path = csv_path
    else:
        raise PriceFileMissing(
            f"price file for {symbol} not found in {data_path} "
            f"(looked for {symbol}.tsv and {symbol}.csv)"
        )

    bars: list[PriceBar] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            first_line = f.readline()
            if not first_line:
                raise PriceFileMalformed(f"{path}: empty file")
            f.seek(0)
            sep = _detect_dialect(first_line)
            reader = csv.DictReader(f, delimiter=sep)

            if reader.fieldnames is None:
                raise PriceFileMalformed(f"{path}: no header row")
            missing = []
            if _REQUIRED_DATE_COL not in reader.fieldnames:
                missing.append(_REQUIRED_DATE_COL)
            if _REQUIRED_PRICE_COL not in reader.fieldnames:
                missing.append(_REQUIRED_PRICE_COL)
            if missing:
                raise PriceFileMalformed(
                    f"{path}: header missing required columns {missing}; "
                    f"found {reader.fieldnames}"
                )

            for row_idx, row in enumerate(reader, start=2):  # header is row 1
                ctx = f"{path}:row {row_idx}"
                raw_date = row.get(_REQUIRED_DATE_COL, "")
                raw_price = row.get(_REQUIRED_PRICE_COL, "")
                if raw_date is None or not raw_date.strip():
                    # Skip blank rows silently; Yahoo exports sometimes
                    # contain trailing blank lines.
                    continue
                bar_date = _parse_date(raw_date, context=ctx)
                adj_close = _parse_decimal(raw_price, context=ctx)
                bars.append(PriceBar(bar_date=bar_date, adj_close=adj_close))
    except (PriceDataError, OSError):
        raise
    except Exception as e:  # any other unexpected parse failure
        raise PriceFileMalformed(f"{path}: unexpected parse error: {e}") from e

    if not bars:
        raise PriceFileMalformed(f"{path}: no data rows")

    # Sort ascending by date and dedupe (later occurrence wins, since
    # Yahoo exports occasionally repeat the most recent week).
    by_date: dict[date, PriceBar] = {}
    for bar in bars:
        by_date[bar.bar_date] = bar
    bars_sorted = sorted(by_date.values(), key=lambda b: b.bar_date)

    if count is not None and count > 0 and len(bars_sorted) > count:
        bars_sorted = bars_sorted[-count:]
    return bars_sorted


def latest_bar_date(bars: list[PriceBar]) -> date | None:
    """Convenience: the latest date in a bar list, or None if empty."""
    if not bars:
        return None
    return bars[-1].bar_date


__all__ = [
    "PriceBar",
    "PriceDataError",
    "PriceFileMissing",
    "PriceFileMalformed",
    "load_weekly_adj_close",
    "latest_bar_date",
]
