"""
test_translate_tsvs.py — Smoke test for irapm_driver.translate_data_tsvs().

Verifies the date-format shim independently of the full scenario harness.
Run from the repo root:

    python test_translate_tsvs.py

Exits 0 on success, 1 on failure. Quick (< 1 second).
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

from irapm_driver import translate_data_tsvs


def main() -> int:
    here = Path(__file__).parent
    src = here / "data"

    with tempfile.TemporaryDirectory(prefix="tsv_translate_test_") as tmpdir:
        dst = Path(tmpdir) / "data"
        try:
            translate_data_tsvs(
                src_dir=src,
                dst_dir=dst,
                symbols=["FBCG", "AVUV"],
            )
        except Exception as e:
            print(f"[FAIL] translate_data_tsvs raised: {type(e).__name__}: {e}")
            return 1

        # Both files should exist.
        for symbol in ("FBCG", "AVUV"):
            p = dst / f"{symbol}.tsv"
            if not p.exists():
                print(f"[FAIL] expected output file missing: {p}")
                return 1
            print(f"[OK]   wrote {p}")

        # Spot-check FBCG: open, parse header, check first two data rows.
        with (dst / "FBCG.tsv").open("r", encoding="utf-8") as f:
            header = f.readline().strip()
            row1 = f.readline().strip()
            row2 = f.readline().strip()

        if header != "Date\tAdj Close":
            print(f"[FAIL] FBCG header is {header!r}, expected 'Date\\tAdj Close'")
            return 1
        print(f"[OK]   FBCG header: {header}")

        # First data row should have an ISO date in column 1.
        cells = row1.split("\t")
        if len(cells) != 2:
            print(f"[FAIL] first data row has {len(cells)} cells, expected 2: {row1!r}")
            return 1
        try:
            d = date.fromisoformat(cells[0])
        except ValueError as e:
            print(f"[FAIL] first data row date {cells[0]!r} is not ISO format: {e}")
            return 1
        print(f"[OK]   FBCG first row date parses as ISO: {d}")

        # Adj Close should be a valid decimal.
        from decimal import Decimal, InvalidOperation
        try:
            Decimal(cells[1])
        except InvalidOperation:
            print(f"[FAIL] first row adj_close {cells[1]!r} is not decimal")
            return 1
        print(f"[OK]   FBCG first row adj_close parses as Decimal: {cells[1]}")

        # Rows should be ascending by date (translator sorts).
        cells2 = row2.split("\t")
        d2 = date.fromisoformat(cells2[0])
        if d2 <= d:
            print(f"[FAIL] row 2 date {d2} not after row 1 date {d}")
            return 1
        print(f"[OK]   FBCG rows are date-sorted ascending: {d} < {d2}")

    print()
    print("=" * 60)
    print("ALL TRANSLATE TSV CHECKS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
