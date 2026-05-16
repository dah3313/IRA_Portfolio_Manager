"""smoke_report.py — Run the reporter against the real 2005-2025 event log.

Not a permanent test. A one-shot script that exercises all five public
report.py functions against actual production-quality data, so we can
eyeball the output and surface any bugs before writing permanent tests.

Usage from the C:\\portfolio directory:
    python smoke_report.py

Outputs are written under runs/2005-2025_fd1ad2/_smoke_report_output/.
"""

from __future__ import annotations

import sys
import traceback
from datetime import date
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parent
    state_dir = repo / "runs" / "2005-2025_fd1ad2" / "_harness_state"
    output_dir = repo / "runs" / "2005-2025_fd1ad2" / "_smoke_report_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not (state_dir / "events.jsonl").exists():
        print(f"ERROR: events.jsonl not found at {state_dir}")
        return 1

    print(f"State dir: {state_dir}")
    print(f"Output dir: {output_dir}")
    print(f"Event log size: {(state_dir / 'events.jsonl').stat().st_size:,} bytes")
    print()

    import report

    # 1. write_current_status
    print("=" * 60)
    print("1. write_current_status")
    print("=" * 60)
    try:
        out = report.write_current_status(
            state_dir=state_dir,
            output_path=output_dir / "current_status.txt",
        )
        print(f"OK: wrote {out}  ({out.stat().st_size:,} bytes)")
    except Exception:
        print("FAILED:")
        traceback.print_exc()
    print()

    # 2. write_simulation_report
    print("=" * 60)
    print("2. write_simulation_report")
    print("=" * 60)
    try:
        # Load the ruleset for the RULESET section.
        # The production ruleset.yaml has ach_destination: "" which fails
        # Pydantic validation (operator hasn't configured their IBKR bank
        # reference). Patch it with a placeholder before validation, same
        # technique as irapm_driver.build_cycle_config. This is smoke-test
        # infrastructure only; the actual sim run is independent of this
        # load.
        ruleset = None
        try:
            import yaml
            from ruleset_model import Ruleset
            ruleset_yaml = repo / "ruleset.yaml"
            if ruleset_yaml.exists():
                with ruleset_yaml.open("r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                if not raw.get("ach_destination"):
                    raw["ach_destination"] = "SMOKE-TEST-PLACEHOLDER"
                ruleset = Ruleset.model_validate(raw)
                print(f"  Loaded ruleset from {ruleset_yaml}")
            else:
                print("  ruleset.yaml not found; passing None (placeholder render)")
        except Exception as exc:
            print(f"  Could not load ruleset ({exc}); passing None")

        out = report.write_simulation_report(
            state_dir=state_dir,
            output_path=output_dir / "report_smoke_baseline_2005-2025.txt",
            scenario_name="baseline_2005-2025",
            ruleset=ruleset,
        )
        print(f"OK: wrote {out}  ({out.stat().st_size:,} bytes)")
    except Exception:
        print("FAILED:")
        traceback.print_exc()
    print()

    # 3. append_monthly_row_to_year_file
    print("=" * 60)
    print("3. append_monthly_row_to_year_file (sample: December 2008 — full year)")
    print("=" * 60)
    try:
        out = report.append_monthly_row_to_year_file(
            state_dir=state_dir,
            output_dir=output_dir,
            month=date(2008, 12, 1),
        )
        print(f"OK: wrote {out}  ({out.stat().st_size:,} bytes)")
    except Exception:
        print("FAILED:")
        traceback.print_exc()
    print()

    # 4. close_year_file
    print("=" * 60)
    print("4. close_year_file (sample: 2010)")
    print("=" * 60)
    try:
        out = report.close_year_file(
            state_dir=state_dir,
            output_dir=output_dir,
            year=2010,
        )
        print(f"OK: wrote {out}  ({out.stat().st_size:,} bytes)")
    except Exception:
        print("FAILED:")
        traceback.print_exc()
    print()

    # 5. prune_old_year_files — exercise but pass huge retain so nothing deletes
    print("=" * 60)
    print("5. prune_old_year_files (retain=100, nothing should delete)")
    print("=" * 60)
    try:
        deleted = report.prune_old_year_files(
            output_dir=output_dir,
            retain_years=100,
        )
        print(f"OK: deleted {len(deleted)} files (expected 0)")
    except Exception:
        print("FAILED:")
        traceback.print_exc()
    print()

    print("=" * 60)
    print("Output files generated:")
    print("=" * 60)
    for f in sorted(output_dir.iterdir()):
        if f.is_file():
            print(f"  {f.name}  ({f.stat().st_size:,} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
