"""
market.py — historical price data loader and date-indexed lookup.

Public exports:
    MarketSimulator — loads TSV/CSV price files, serves price_on() lookups
    PriceDataError — raised on file/format problems

See also: IPMS_SPECIFICATION.md §6.2 (synthetic IBKR client uses this
for price quotes), data/README.md (file format spec)


FILE FORMAT NOTES (inherited from IPMV1, validated against data/README.md)

Yahoo Finance TSV/CSV format:
    Date    Open    High    Low    Close    AdjClse    Volume
    Apr 13, 2026    10.89    10.95    10.89    10.95    10.95    -
    Apr 6, 2026     10.81    10.88    10.81    10.88    10.88    -
    Mar 31, 2026    0.048 Distribution
    Mar 30, 2026    10.74    10.82    10.74    10.82    10.77    -

Quirks the loader handles:
  - Tab-separated (Yahoo web copy-paste) OR comma-separated (Yahoo CSV
    download); delimiter sniffed from header.
  - Date format "Apr 13, 2026" (web copy) OR ISO "2026-04-13" (CSV
    download); ISO tried first, falls back to %b %d, %Y.
  - Header "AdjClse" (web copy, no 'o', no space) OR "Adj Close" (CSV
    download). Loader normalizes both to a common key.
  - Distribution rows have only two columns and a non-numeric value in
    the price slot; loader silently skips them.
  - Descending date order (newest first); loader sorts ascending after
    parse.
  - Comment lines starting with '#' (used by stitching utilities for
    headers); loader skips them.
  - Weekly cadence acceptable; price_on() returns most-recent-on-or-
    before for any query date, so weekend/holiday/missing-week queries
    resolve to the prior bar.

The loader reads ONLY Date and AdjClose columns. Other columns ignored.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ipms.proxy_map import PROXY_MAP, BUFFER_SYMBOL, BUFFER_PROXY


class PriceDataError(ValueError):
    """
    Raised on price-file problems: missing file, malformed header,
    no parseable rows, or window-coverage shortfall (run window
    extends past file's date coverage).
    """
    pass


class MarketSimulator:
    """
    Loads historical Adj Close prices and serves price_on() lookups
    by symbol and date.

    Construction loads every file referenced in PROXY_MAP plus the
    SGOV buffer file. Files are loaded once at construction; later
    price_on() calls are O(log n) binary search.

    `extra_proxies` allows stress-test drivers to add symbols outside
    the canonical PROXY_MAP (e.g., a side-swap into VTI) without
    modifying proxy_map.py. The dict format is the same as PROXY_MAP:
    {ira_pm_symbol: proxy_ticker}.

    The `start` and `end` parameters define the run window. The
    loader verifies every loaded file covers the entire window and
    raises PriceDataError if not. This is a fail-fast check that
    surfaces "you forgot to refresh the data" before the simulator
    runs to mid-window and crashes on a missing date.

    Prices are stored and returned as Decimal. The IPMV1 used float
    here; the IPMS uses Decimal for consistency with the IRA PM's
    money discipline. The conversion is explicit: file values parse
    as float for validation (positive, non-NaN), then convert to
    Decimal via str() to avoid float-binary-fraction artifacts.
    """

    def __init__(
        self,
        start: date,
        end: date,
        data_dir: Optional[Path] = None,
        extra_proxies: Optional[dict[str, str]] = None,
    ):
        self.start = start
        self.end = end
        # Resolve data_dir with this precedence:
        #   1. Explicit `data_dir` constructor kwarg (tests, harness).
        #   2. IPMS_DATA_DIR environment variable (cross-platform
        #      operator override; the harness sets this so scenarios
        #      stay portable between Windows and WSL/Linux without
        #      editing scenario YAMLs).
        #   3. C:/portfolio/data hardcoded default (operator's
        #      canonical Windows-side location).
        # Note: the existing IPMS_TEST_DATA_DIR override in
        # tests/conftest.py operates at the fixture level (it sets
        # `data_dir` via a monkey-patched __init__), so this env var
        # is purely additive — the test suite is unaffected.
        import os
        if data_dir is not None:
            self.data_dir = data_dir
        elif os.environ.get("IPMS_DATA_DIR"):
            self.data_dir = Path(os.environ["IPMS_DATA_DIR"])
        else:
            self.data_dir = Path("C:/portfolio/data")

        # Combine canonical PROXY_MAP with any extras. Extras override
        # canonical entries for the same symbol — useful when a stress
        # test wants to substitute a different proxy temporarily.
        self.proxy_map = dict(PROXY_MAP)
        self.proxy_map[BUFFER_SYMBOL] = BUFFER_PROXY
        if extra_proxies:
            self.proxy_map.update(extra_proxies)

        # Per-symbol storage. _series and _dates are parallel arrays
        # keyed by symbol; _dates is a separate sorted list for fast
        # binary search in price_on().
        self._series: dict[str, list[tuple[date, Decimal]]] = {}
        self._dates: dict[str, list[date]] = {}
        # Per-symbol coverage info for run-output README generation.
        self.proxy_info: dict[str, str] = {}

        self._load_all()

    # ------------------------------------------------------------------
    # LOADING
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """
        Load every (symbol → proxy) entry from proxy_map. Files that
        serve as proxies for multiple symbols (e.g., PIMIX_stitched
        for both PYLD and JPIE) are loaded once and shared.

        Verifies window coverage per file. Raises PriceDataError on
        missing files or window shortfall.
        """
        # Memoize file loads so a proxy used by multiple symbols only
        # parses once. `loaded_proxies` maps proxy ticker → series;
        # `loaded_paths` maps proxy ticker → Path for proxy_info display.
        loaded_proxies: dict[str, list[tuple[date, Decimal]]] = {}
        loaded_paths: dict[str, Path] = {}

        for symbol, proxy in self.proxy_map.items():
            if proxy not in loaded_proxies:
                loaded_proxies[proxy], loaded_paths[proxy] = self._load_file(proxy)

            self._series[symbol] = loaded_proxies[proxy]
            self._dates[symbol] = [d for d, _ in loaded_proxies[proxy]]
            self.proxy_info[symbol] = (
                f"{loaded_paths[proxy].name} "
                f"({len(loaded_proxies[proxy])} rows, "
                f"{loaded_proxies[proxy][0][0]} to "
                f"{loaded_proxies[proxy][-1][0]})"
            )

            # Window coverage check. Buffer asset gets relaxed treatment
            # because SGOV pre-launch periods are handled by the IRA PM
            # synthesizing the buffer as a flat dollar value rather than
            # querying the price series.
            first_d, _ = loaded_proxies[proxy][0]
            last_d, _ = loaded_proxies[proxy][-1]

            if symbol == BUFFER_SYMBOL:
                # Only validate end-coverage for buffer; pre-launch
                # buffer dates are tolerated.
                if self.end > last_d:
                    raise PriceDataError(
                        f"Run end {self.end} exceeds {loaded_paths[proxy].name} "
                        f"last row {last_d} (proxy for {symbol})"
                    )
            else:
                if self.start < first_d:
                    raise PriceDataError(
                        f"Run start {self.start} precedes "
                        f"{loaded_paths[proxy].name} first row {first_d} "
                        f"(proxy for {symbol})"
                    )
                if self.end > last_d:
                    raise PriceDataError(
                        f"Run end {self.end} exceeds "
                        f"{loaded_paths[proxy].name} last row {last_d} "
                        f"(proxy for {symbol})"
                    )

    def _load_file(self, proxy: str) -> tuple[list[tuple[date, Decimal]], Path]:
        """
        Load one TSV/CSV file. Returns (rows, path) where rows is a
        list of (date, Decimal_price) sorted ascending.

        Tries .tsv first (Yahoo web copy-paste default) then .csv
        (Yahoo download default). Raises PriceDataError if neither
        exists.
        """
        # Locate the file. Prefer .tsv since that's what's in the
        # operator's data dir today; .csv fallback covers re-download
        # workflows.
        path = None
        for ext in (".tsv", ".csv"):
            candidate = self.data_dir / f"{proxy}{ext}"
            if candidate.exists():
                path = candidate
                break
        if path is None:
            raise PriceDataError(
                f"Missing price file: {self.data_dir / proxy}.tsv (or .csv). "
                f"Drop a Yahoo-Finance-format file (Date, Open, High, Low, "
                f"Close, AdjClose, Volume) at that path."
            )

        # Read all lines; strip leading '#' comment lines that the
        # stitcher writes.
        with path.open() as fh:
            lines = [raw for raw in fh if not raw.lstrip().startswith("#")]
        if not lines:
            raise PriceDataError(f"{path}: empty file")

        # Sniff delimiter from header line. Tab-or-comma is the only
        # variation we handle.
        header = lines[0]
        delim = "\t" if "\t" in header else ","
        reader = csv.DictReader(lines, delimiter=delim)
        if reader.fieldnames is None:
            raise PriceDataError(f"{path}: no header row")

        # Find Adj Close column. Tolerate name variants:
        #   "Adj Close" (Yahoo CSV download)
        #   "AdjClose"  (alternate)
        #   "AdjClse"   (Yahoo web copy-paste — typo'd by Yahoo)
        #   "adj_close" (snake_case variant)
        adj_key = self._find_adj_close_column(reader.fieldnames, path)
        date_key = self._find_date_column(reader.fieldnames, path)

        # Parse rows. Skip distribution rows (non-numeric price slot)
        # and rows with empty fields. Raise only on truly malformed
        # files (no parseable rows at all).
        rows: list[tuple[date, Decimal]] = []
        for row in reader:
            parsed = self._parse_row(row, date_key, adj_key)
            if parsed is not None:
                rows.append(parsed)

        if not rows:
            raise PriceDataError(f"{path}: no data rows parsed")

        # Yahoo files arrive in descending date order; sort ascending
        # so binary search in price_on() works.
        rows.sort(key=lambda r: r[0])
        return rows, path

    @staticmethod
    def _find_adj_close_column(fieldnames: list[str], path: Path) -> str:
        """
        Locate the Adj Close header column among the variants Yahoo
        produces. Returns the actual fieldname key to use. Raises if
        no recognizable variant is present.
        """
        # Normalize: lowercase, strip spaces and underscores. Then
        # match against known forms.
        def norm(s: str) -> str:
            return s.lower().replace("_", "").replace(" ", "")

        for k in fieldnames:
            if norm(k) in ("adjclose", "adjclse"):
                return k
        raise PriceDataError(
            f"{path}: no Adj Close column (got {fieldnames})"
        )

    @staticmethod
    def _find_date_column(fieldnames: list[str], path: Path) -> str:
        """Locate the Date column. Case-insensitive match on 'date'."""
        for k in fieldnames:
            if k.lower() == "date":
                return k
        raise PriceDataError(f"{path}: no Date column (got {fieldnames})")

    @staticmethod
    def _parse_row(
        row: dict, date_key: str, adj_key: str
    ) -> Optional[tuple[date, Decimal]]:
        """
        Parse one CSV row. Returns (date, Decimal_price) or None for
        rows that should be skipped (distributions, blanks, malformed).

        `row` is a dict from csv.DictReader. `date_key` and `adj_key`
        are the actual fieldname strings determined at load time.
        """
        d_str = (row.get(date_key) or "").strip()
        p_str = (row.get(adj_key) or "").strip()

        # Empty date or price → distribution-style row or stray blank;
        # silently skip.
        if not d_str:
            return None
        if not p_str or p_str.lower() == "null":
            return None

        # Parse date. ISO format first (YYYY-MM-DD), then Yahoo's web
        # format (Apr 13, 2026). Anything else → distribution row that
        # landed in the date column; skip.
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            try:
                d = datetime.strptime(d_str, "%b %d, %Y").date()
            except ValueError:
                return None

        # Parse price as float for validation (positive, finite), then
        # convert to Decimal via str() to avoid binary-fraction artifacts.
        # Distribution rows where the dist value landed in the price
        # column produce non-numeric strings ("0.049 Distribution") that
        # float() rejects; skip.
        try:
            p_float = float(p_str)
        except ValueError:
            return None
        if p_float <= 0:
            return None

        # Decimal via str preserves the file's stated precision exactly.
        return (d, Decimal(p_str))

    # ------------------------------------------------------------------
    # PRICE LOOKUP
    # ------------------------------------------------------------------

    def price_on(self, symbol: str, d: date) -> Decimal:
        """
        Return the most recent Adj Close on or before `d` for `symbol`.

        Handles weekends/holidays/missing rows by walking back to the
        most recent available bar. If `d` is at or after the file's
        last row, returns the last row's price. If `d` precedes the
        first row, raises ValueError — callers should validate the
        run window before calling.

        O(log n) binary search per call.
        """
        if symbol not in self._series:
            raise KeyError(
                f"No price series loaded for {symbol}. If this is a "
                f"stress-test symbol, pass it via MarketSimulator's "
                f"`extra_proxies` constructor kwarg."
            )

        dates = self._dates[symbol]
        series = self._series[symbol]

        if d < dates[0]:
            raise ValueError(
                f"price_on({symbol}, {d}): date precedes first available "
                f"row {dates[0]}"
            )

        # Past the end → return last available. This handles trailing-
        # edge queries gracefully without forcing the caller to clamp.
        if d >= dates[-1]:
            return series[-1][1]

        # Binary search for rightmost date <= d. The standard "find
        # rightmost element ≤ target" pattern.
        lo, hi = 0, len(dates) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if dates[mid] <= d:
                lo = mid
            else:
                hi = mid - 1
        return series[lo][1]

    def has_symbol(self, symbol: str) -> bool:
        """True if `symbol` was loaded (canonical or via extra_proxies)."""
        return symbol in self._series

    def first_date(self, symbol: str) -> date:
        """First available date for `symbol`. Raises if symbol not loaded."""
        if symbol not in self._dates:
            raise KeyError(f"No price series loaded for {symbol}")
        return self._dates[symbol][0]

    def last_date(self, symbol: str) -> date:
        """Last available date for `symbol`. Raises if symbol not loaded."""
        if symbol not in self._dates:
            raise KeyError(f"No price series loaded for {symbol}")
        return self._dates[symbol][-1]
