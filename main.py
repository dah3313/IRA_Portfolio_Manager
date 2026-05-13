"""
main.py — IRAPM CLI entrypoint.

PURPOSE:
    Wire up the runtime: load ruleset, box config, instantiate broker,
    alerter, token detector, clock; then dispatch to the requested
    cycle type.

USAGE:
    python main.py weekly
    python main.py daily-token
    python main.py --dry-run weekly

CONFIG SOURCES:
    1. ruleset.yaml          — financial / decision tunables (canonical)
    2. box.yaml              — per-box identity, broker creds, alert creds
       (or environment variables for secrets — see §15.8)

DRY-RUN MODE:
    --dry-run flag swaps in SyntheticBroker + StdoutAlerter +
    MockTokenDetector. The decision layer is exercised identically;
    no real money moves and no real alerts go out. This is the IPMS
    simulator harness (c:\\portfolio\\IPMS\\) re-purposed inline.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Module-search path: this script lives in the IRAPM directory; the
# modules import each other by bare name (no package prefix).
sys.path.insert(0, str(Path(__file__).parent))

import yaml

from alerter import (
    GmailConfig,
    GmailTwilioAlerter,
    StdoutAlerter,
    TwilioConfig,
    load_templates,
)
from clock import SystemClock
from cycle import CycleConfig, run_daily_token_cycle, run_weekly_cycle
from persistence import Paths
from ruleset_model import Ruleset
from tokens import (
    LinuxUSBTokenDetector,
    MockTokenDetector,
    TokenConfig,
    TokenMatchRule,
    TokenType,
)


# =============================================================================
# Logging
# =============================================================================

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


# =============================================================================
# Box config loader
# =============================================================================

def _load_box_config(path: Path) -> dict:
    """Load box.yaml.

    Expected structure (operator fills in):
      box_id: "box-A"           # human-readable identifier
      client_id: 11             # broker client id (11/12 per box)
      broker:
        host: "127.0.0.1"
        port: 7497
        # account: stored in env IRAPM_BROKER_ACCOUNT
      alerter:
        gmail:
          username: "alerts@example.com"
          # app_password: stored in env IRAPM_GMAIL_APP_PASSWORD
          from_address: "alerts@example.com"
          to_addresses: ["operator@example.com", "survivor@example.com"]
        twilio:
          # account_sid: stored in env IRAPM_TWILIO_SID
          # auth_token: stored in env IRAPM_TWILIO_TOKEN
          from_number: "+14155550100"
          to_numbers: ["+14155550101", "+14155550102"]
      paths:
        state_dir: "C:/portfolio/IRAPM/state"
        log_dir:   "C:/portfolio/IRAPM/logs"
        data_dir:  "C:/portfolio/data"
      tokens:                   # required in production; ignored in dry_run
        phase3_rules:
          - vendor_id: "0951"           # hex USB Vendor ID (e.g. Kingston)
            product_id: "1666"          # hex USB Product ID
            label: "IRAPM-P3"           # optional FAT/exFAT filesystem label
            capacity_bytes_min: 1000000000     # ~1 GB lower bound
            capacity_bytes_max: 35000000000    # ~32 GB upper bound
        stopincome_rules:
          - vendor_id: "0781"           # hex VID (e.g. SanDisk)
            label: "IRAPM-STOP"
    """
    if not path.exists():
        # Provide a minimal default that runs in dry-run sim mode only.
        return {
            "box_id": "box-dev",
            "client_id": 11,
            "broker": {},
            "alerter": {"gmail": {}, "twilio": {}},
            "paths": {
                "state_dir": str(Path(__file__).parent / "state"),
                "log_dir":   str(Path(__file__).parent / "logs"),
            },
        }
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# =============================================================================
# Runtime construction
# =============================================================================

def _build_broker(dry_run: bool, box_cfg: dict, ruleset: Ruleset):
    """Instantiate the broker. SyntheticBroker for dry_run; IBKRBroker
    for production."""
    if dry_run:
        from synthetic_broker import SyntheticBroker
        return SyntheticBroker()
    from ibkr_broker import IBKRBroker
    bcfg = box_cfg.get("broker", {}) or {}
    return IBKRBroker(
        host=bcfg.get("host", "127.0.0.1"),
        port=int(bcfg.get("port", 7497)),
        client_id=int(box_cfg.get("client_id", 11)),
        account=os.environ.get("IRAPM_BROKER_ACCOUNT", bcfg.get("account", "")),
    )


def _build_alerter(dry_run: bool, box_cfg: dict, templates_path: Path):
    """Instantiate alerter. StdoutAlerter for dry_run; GmailTwilioAlerter
    for production."""
    templates = load_templates(templates_path)
    if dry_run:
        return StdoutAlerter(templates)
    acfg = box_cfg.get("alerter", {}) or {}
    gmail_cfg = acfg.get("gmail", {}) or {}
    twilio_cfg = acfg.get("twilio", {}) or {}
    gmail = GmailConfig(
        smtp_host=gmail_cfg.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(gmail_cfg.get("smtp_port", 465)),
        username=gmail_cfg.get("username", ""),
        app_password=os.environ.get(
            "IRAPM_GMAIL_APP_PASSWORD",
            gmail_cfg.get("app_password", "")),
        from_address=gmail_cfg.get("from_address", ""),
        to_addresses=tuple(gmail_cfg.get("to_addresses", [])),
    )
    twilio = TwilioConfig(
        account_sid=os.environ.get(
            "IRAPM_TWILIO_SID",
            twilio_cfg.get("account_sid", "")),
        auth_token=os.environ.get(
            "IRAPM_TWILIO_TOKEN",
            twilio_cfg.get("auth_token", "")),
        from_number=twilio_cfg.get("from_number", ""),
        to_numbers=tuple(twilio_cfg.get("to_numbers", [])),
    )
    return GmailTwilioAlerter(templates, gmail, twilio)


def _parse_token_rules(
    rules_yaml: list | None,
    token_type: TokenType,
) -> tuple[TokenMatchRule, ...]:
    """Build TokenMatchRule tuples from a YAML rules list.

    Each rule dict supports keys: vendor_id, product_id, vendor_name,
    product_name, label, capacity_bytes_min, capacity_bytes_max. All
    are optional individually, but each rule must have at least one
    matchable field (otherwise it would match every USB device).
    """
    if not rules_yaml:
        return ()
    out: list[TokenMatchRule] = []
    for idx, raw in enumerate(rules_yaml):
        if not isinstance(raw, dict):
            raise ValueError(
                f"box.yaml tokens.{token_type.value}_rules[{idx}] must be a mapping"
            )
        rule = TokenMatchRule(
            token_type=token_type,
            vendor_id=raw.get("vendor_id"),
            product_id=raw.get("product_id"),
            vendor_name=raw.get("vendor_name"),
            product_name=raw.get("product_name"),
            label=raw.get("label"),
            capacity_bytes_min=raw.get("capacity_bytes_min"),
            capacity_bytes_max=raw.get("capacity_bytes_max"),
        )
        # Defensive: refuse rules with NO matchable fields. Such a rule
        # would match every USB device on the bus.
        has_any = any([
            rule.vendor_id, rule.product_id, rule.vendor_name,
            rule.product_name, rule.label,
            rule.capacity_bytes_min is not None,
            rule.capacity_bytes_max is not None,
        ])
        if not has_any:
            raise ValueError(
                f"box.yaml tokens.{token_type.value}_rules[{idx}] has no "
                f"matchable fields; would match every USB device. "
                f"Specify at least one of: vendor_id, product_id, "
                f"vendor_name, product_name, label, capacity_bytes_min/max."
            )
        out.append(rule)
    return tuple(out)


def _build_token_detector(dry_run: bool, box_cfg: dict):
    """Instantiate token detector.

    - dry_run or Windows host: MockTokenDetector (the production
      detector requires pyudev, which is Linux-only).
    - Production on Linux: LinuxUSBTokenDetector with a TokenConfig
      assembled from `box.yaml -> tokens:`. A missing or empty rules
      list aborts startup loud (per I9: invalid config aborts).
    """
    if dry_run or sys.platform == "win32":
        return MockTokenDetector()

    tcfg = box_cfg.get("tokens", {}) or {}
    phase3_rules = _parse_token_rules(
        tcfg.get("phase3_rules"), TokenType.PHASE3,
    )
    stopincome_rules = _parse_token_rules(
        tcfg.get("stopincome_rules"), TokenType.STOPINCOME,
    )

    if not phase3_rules:
        raise ValueError(
            "box.yaml tokens.phase3_rules is required in production mode "
            "and must contain at least one rule. Pass --dry-run to use the "
            "mock detector for development."
        )
    if not stopincome_rules:
        raise ValueError(
            "box.yaml tokens.stopincome_rules is required in production "
            "mode and must contain at least one rule."
        )

    config = TokenConfig(
        phase3_rules=phase3_rules,
        stopincome_rules=stopincome_rules,
    )
    return LinuxUSBTokenDetector(config)


def _build_paths(box_cfg: dict) -> Paths:
    pcfg = box_cfg.get("paths", {}) or {}
    here = Path(__file__).parent
    return Paths(
        state_dir=Path(pcfg.get("state_dir", here / "state")),
        log_dir=Path(pcfg.get("log_dir", here / "logs")),
    )


# =============================================================================
# Entry point
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="IRAPM cycle runner")
    ap.add_argument(
        "cycle_type",
        choices=["weekly", "daily-token"],
        help="Which cycle to run",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Use SyntheticBroker + StdoutAlerter + MockTokenDetector",
    )
    ap.add_argument(
        "--ruleset", default=None,
        help="Path to ruleset.yaml (default: ./ruleset.yaml)",
    )
    ap.add_argument(
        "--box-config", default=None,
        help="Path to box.yaml (default: ./box.yaml)",
    )
    ap.add_argument(
        "--alert-templates", default=None,
        help="Path to alert_templates.yaml (default: ./alert_templates.yaml)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    _configure_logging(args.verbose)
    here = Path(__file__).parent

    ruleset_path = Path(args.ruleset) if args.ruleset else here / "ruleset.yaml"
    box_path = Path(args.box_config) if args.box_config else here / "box.yaml"
    templates_path = (Path(args.alert_templates) if args.alert_templates
                      else here / "alert_templates.yaml")

    # Load ruleset (Pydantic validation will raise on bad values per I9)
    ruleset = Ruleset.from_yaml(ruleset_path)
    if args.dry_run:
        # The Ruleset has a dry_run flag too; assert consistency.
        ruleset = ruleset.model_copy(update={"dry_run": True})

    box_cfg = _load_box_config(box_path)

    broker = _build_broker(ruleset.dry_run, box_cfg, ruleset)
    alerter = _build_alerter(ruleset.dry_run, box_cfg, templates_path)
    token_detector = _build_token_detector(ruleset.dry_run, box_cfg)
    paths = _build_paths(box_cfg)

    config = CycleConfig(
        ruleset=ruleset,
        broker=broker,
        alerter=alerter,
        token_detector=token_detector,
        paths=paths,
        clock=SystemClock(),
        box_id=str(box_cfg.get("box_id", "box-dev")),
        client_id=int(box_cfg.get("client_id", 11)),
    )

    logger = logging.getLogger("main")
    logger.info(
        "IRAPM cycle=%s dry_run=%s box=%s client_id=%d",
        args.cycle_type, ruleset.dry_run, config.box_id, config.client_id,
    )

    try:
        if args.cycle_type == "weekly":
            run_weekly_cycle(config)
        else:
            run_daily_token_cycle(config)
    except Exception:
        logger.exception("cycle aborted with unhandled exception")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
